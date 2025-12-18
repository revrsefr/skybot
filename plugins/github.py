import json
import re
import time

from util import hook, http


API_BASE = "https://api.github.com"
DEFAULT_POLL_INTERVAL = 60
MAX_EVENTS_PER_POLL = 3
MAX_COMMITS_PER_PUSH = 3
MAX_REPOS_PER_POLL_NO_TOKEN = 4
RATE_LIMIT_WARN_COOLDOWN = 3600

_repo_re = re.compile(r"^(?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+)$")

_last_poll_by_conn = {}
_poll_cursor_by_conn = {}
_last_rate_limit_warn = {}
_poll_interval_hint_by_conn = {}


def _now():
    return int(time.time())


def _normalize_repo(repo):
    repo = (repo or "").strip()
    m = _repo_re.match(repo)
    if not m:
        raise ValueError("expected owner/repo")
    return f"{m.group('owner')}/{m.group('repo')}"


def _db_init(db):
    db.execute(
        "create table if not exists github_watches ("
        "chan text not null, "
        "repo text not null, "
        "last_id text, "
        "etag text, "
        "primary key (chan, repo)"
        ")"
    )
    db.commit()


def _github_token(bot):
    # Optional: set in config.json under api_keys.github
    bot_keys = (bot.config or {}).get("api_keys", {})
    return bot_keys.get("github")


def _github_headers(token=None, etag=None):
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "skybot-github-plugin",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if etag:
        headers["If-None-Match"] = etag
    return headers


def _github_get_json(url, token=None, etag=None):
    try:
        resp = http.open(url, headers=_github_headers(token=token, etag=etag))
        body = resp.read().decode("utf-8", "replace")
        new_etag = None
        poll_interval = None
        try:
            new_etag = resp.headers.get("ETag")
        except Exception:
            pass
        try:
            poll_interval = resp.headers.get("X-Poll-Interval")
        except Exception:
            poll_interval = None
        try:
            poll_interval = int(poll_interval) if poll_interval is not None else None
        except Exception:
            poll_interval = None
        return json.loads(body), new_etag, poll_interval
    except http.HTTPError as e:
        if getattr(e, "code", None) == 304:
            new_etag = None
            poll_interval = None
            try:
                new_etag = e.headers.get("ETag")
            except Exception:
                pass
            try:
                poll_interval = e.headers.get("X-Poll-Interval")
            except Exception:
                poll_interval = None
            try:
                poll_interval = int(poll_interval) if poll_interval is not None else None
            except Exception:
                poll_interval = None
            return None, (new_etag or etag), poll_interval
        raise


def _repo_short(repo_full):
    if not repo_full:
        return "unknown"
    return repo_full.split("/", 1)[-1]


def _compare_url(repo_full, before, head):
    if not (repo_full and before and head):
        return None
    return f"https://github.com/{repo_full}/compare/{before}...{head}"


def _compare_stats(repo_full, before, head, token=None):
    """Return (additions, deletions, files_changed, commit_count) using GitHub compare API."""

    if not (repo_full and before and head):
        return None
    url = f"{API_BASE}/repos/{repo_full}/compare/{before}...{head}"
    data, _, _ = _github_get_json(url, token=token)
    if not data:
        return None
    files = data.get("files") or []
    additions = 0
    deletions = 0
    for f in files:
        try:
            additions += int(f.get("additions") or 0)
            deletions += int(f.get("deletions") or 0)
        except Exception:
            continue
    commit_count = data.get("total_commits")
    if not isinstance(commit_count, int):
        commit_count = len(data.get("commits") or [])
    return additions, deletions, len(files), commit_count


def _short_sha(sha):
    if not sha:
        return ""
    return sha[:7]


def _commit_subject(message):
    if not message:
        return ""
    # Git commit messages can be multi-line; show the subject.
    subject = str(message).splitlines()[0].strip()
    subject = re.sub(r"\s+", " ", subject)
    return subject


def _format_commit_lines(repo_tag, actor, commits, total_count=None):
    if not commits:
        return []

    show = commits[-MAX_COMMITS_PER_PUSH:]
    lines = []
    for c in show:
        sha = _short_sha((c or {}).get("sha"))
        subject = _commit_subject((c or {}).get("message"))
        if sha and subject:
            lines.append(f"[{repo_tag}] {actor} {sha} - {subject}")
        elif sha:
            lines.append(f"[{repo_tag}] {actor} {sha}")
        elif subject:
            lines.append(f"[{repo_tag}] {actor} - {subject}")

    if isinstance(total_count, int) and total_count > len(show):
        more = total_count - len(show)
        if more > 0:
            lines.append(f"[{repo_tag}] (+{more} more commits)")

    return lines


def _compare_commits(repo_full, before, head, token=None):
    """Return a list of commit dicts: {sha, message} from the compare API."""

    if not (repo_full and before and head):
        return []
    url = f"{API_BASE}/repos/{repo_full}/compare/{before}...{head}"
    data, _, _ = _github_get_json(url, token=token)
    if not data:
        return []

    commits = []
    for c in data.get("commits") or []:
        sha = (c or {}).get("sha")
        message = ((c or {}).get("commit") or {}).get("message")
        if sha or message:
            commits.append({"sha": sha, "message": message})
    return commits


def _extract_event_url(payload):
    """Best-effort URL extraction for unknown event types."""

    if not isinstance(payload, dict):
        return None

    # Common nested objects that often carry html_url
    for key in (
        "comment",
        "review",
        "pull_request",
        "issue",
        "release",
        "forkee",
    ):
        obj = payload.get(key) or {}
        if isinstance(obj, dict):
            url = obj.get("html_url")
            if url:
                return url

    # Some payloads put a URL at the top level.
    url = payload.get("html_url")
    if url:
        return url
    url = payload.get("url")
    if url and isinstance(url, str) and url.startswith("https://"):
        return url

    return None


def format_event(event, token=None):
    """Format a GitHub Events API item into a short IRC-friendly line."""

    etype = event.get("type")
    actor = (event.get("actor") or {}).get("login") or "someone"
    repo = (event.get("repo") or {}).get("name") or "unknown/repo"
    payload = event.get("payload") or {}

    if etype == "PushEvent":
        ref = (payload.get("ref") or "").replace("refs/heads/", "")
        size = payload.get("size")
        commits = payload.get("commits") or []
        head = payload.get("head")
        before = payload.get("before")
        verb = "pushed"
        count = size if isinstance(size, int) else len(commits)
        repo_tag = _repo_short(repo)

        compare_base = before
        compare_head = head
        # Compare API calls are expensive and will quickly burn through the
        # unauthenticated rate limit. Only attempt them when a token is set.
        stats = None
        if token:
            try:
                stats = _compare_stats(repo, compare_base, compare_head, token=token)
            except Exception:
                stats = None

        # If the forward compare shows no changes, try the reverse direction.
        # This happens with force-pushes / rewrites where the public Events API
        # sometimes yields a "before/head" pair that produces an empty compare.
        if stats:
            additions, deletions, files_changed, compare_commit_count = stats
            if isinstance(compare_commit_count, int) and compare_commit_count > 0:
                count = compare_commit_count

            if (
                (not isinstance(compare_commit_count, int) or compare_commit_count <= 0)
                and additions == 0
                and deletions == 0
                and files_changed == 0
                and before
                and head
            ):
                rev = None
                if token:
                    try:
                        rev = _compare_stats(repo, head, before, token=token)
                    except Exception:
                        rev = None

                if rev:
                    r_add, r_del, r_files, r_commits = rev
                    if (
                        (isinstance(r_commits, int) and r_commits > 0)
                        or r_add
                        or r_del
                        or r_files
                    ):
                        compare_base = head
                        compare_head = before
                        stats = rev
                        if isinstance(r_commits, int) and r_commits > 0:
                            count = r_commits

        compare = _compare_url(repo, compare_base, compare_head)

        bits = [f"[{repo_tag}] {actor} {verb} {count} commit(s)"]
        if ref:
            bits.append(f"to {ref}")
        else:
            bits.append(f"to {repo_tag}")

        if stats:
            additions, deletions, files_changed, _compare_commit_count = stats
            bits.append(f"[+{additions}/-{deletions}/\u00b1{files_changed}]")

        if compare:
            bits.append(compare)
        elif head:
            bits.append(_short_sha(head))

        return " ".join(bits)

    if etype == "PullRequestEvent":
        action = payload.get("action") or "updated"
        pr = payload.get("pull_request") or {}
        number = pr.get("number") or payload.get("number")
        title = (pr.get("title") or "").strip()
        url = pr.get("html_url")
        bits = [f"[{_repo_short(repo)}] {actor} {action} PR"]
        if number is not None:
            bits[-1] += f" #{number}"
        if title:
            bits.append(f"\"{title}\"")
        bits.append(f"in {repo}")
        if url:
            bits.append(url)
        return " ".join(bits)

    if etype == "PullRequestReviewEvent":
        action = payload.get("action") or "reviewed"
        pr = payload.get("pull_request") or {}
        number = pr.get("number") or payload.get("pull_request_number")
        title = (pr.get("title") or "").strip()
        review = payload.get("review") or {}
        url = (review.get("html_url") if isinstance(review, dict) else None) or pr.get(
            "html_url"
        )
        state = None
        if isinstance(review, dict):
            state = (review.get("state") or "").lower() or None
        bits = [f"[{_repo_short(repo)}] {actor} {action} PR review"]
        if number is not None:
            bits[-1] += f" #{number}"
        if state:
            bits.append(f"({state})")
        if title:
            bits.append(f"\"{title}\"")
        bits.append(f"in {repo}")
        if url:
            bits.append(url)
        return " ".join(bits)

    if etype == "PullRequestReviewCommentEvent":
        action = payload.get("action") or "commented"
        pr = payload.get("pull_request") or {}
        number = pr.get("number") or payload.get("pull_request_number")
        title = (pr.get("title") or "").strip()
        comment = payload.get("comment") or {}
        url = (comment.get("html_url") if isinstance(comment, dict) else None) or pr.get(
            "html_url"
        )
        bits = [f"[{_repo_short(repo)}] {actor} {action} on PR review"]
        if number is not None:
            bits[-1] += f" #{number}"
        if title:
            bits.append(f"\"{title}\"")
        bits.append(f"in {repo}")
        if url:
            bits.append(url)
        return " ".join(bits)

    if etype == "IssuesEvent":
        action = payload.get("action") or "updated"
        issue = payload.get("issue") or {}
        number = issue.get("number")
        title = (issue.get("title") or "").strip()
        url = issue.get("html_url")
        bits = [f"[{_repo_short(repo)}] {actor} {action} issue"]
        if number is not None:
            bits[-1] += f" #{number}"
        if title:
            bits.append(f"\"{title}\"")
        bits.append(f"in {repo}")
        if url:
            bits.append(url)
        return " ".join(bits)

    if etype == "IssueCommentEvent":
        action = payload.get("action") or "commented"
        issue = payload.get("issue") or {}
        number = issue.get("number")
        url = (payload.get("comment") or {}).get("html_url") or issue.get("html_url")
        bits = [f"[{_repo_short(repo)}] {actor} {action} on issue"]
        if number is not None:
            bits[-1] += f" #{number}"
        bits.append(f"in {repo}")
        if url:
            bits.append(url)
        return " ".join(bits)

    if etype == "ReleaseEvent":
        action = payload.get("action") or "published"
        release = payload.get("release") or {}
        tag = release.get("tag_name")
        url = release.get("html_url")
        bits = [f"[{_repo_short(repo)}] {actor} {action} release"]
        if tag:
            bits[-1] += f" {tag}"
        bits.append(f"in {repo}")
        if url:
            bits.append(url)
        return " ".join(bits)

    if etype == "CreateEvent":
        ref_type = payload.get("ref_type") or "ref"
        ref = payload.get("ref")
        bits = [f"[{_repo_short(repo)}] {actor} created {ref_type}"]
        if ref:
            bits[-1] += f" {ref}"
        bits.append(f"in {repo}")
        return " ".join(bits)

    if etype == "DeleteEvent":
        ref_type = payload.get("ref_type") or "ref"
        ref = payload.get("ref")
        bits = [f"[{_repo_short(repo)}] {actor} deleted {ref_type}"]
        if ref:
            bits[-1] += f" {ref}"
        bits.append(f"in {repo}")
        return " ".join(bits)

    if etype == "ForkEvent":
        forkee = payload.get("forkee") or {}
        full_name = forkee.get("full_name")
        url = forkee.get("html_url")
        bits = [f"[{_repo_short(repo)}] {actor} forked {repo}"]
        if full_name:
            bits.append(f"to {full_name}")
        if url:
            bits.append(url)
        return " ".join(bits)

    if etype == "WatchEvent":
        action = payload.get("action") or "starred"
        return f"[{_repo_short(repo)}] {actor} {action} {repo}"

    # Fallback
    url = _extract_event_url(payload)
    if url:
        return f"[{_repo_short(repo)}] {actor} did {etype or 'something'} in {repo} {url}"
    return f"[{_repo_short(repo)}] {actor} did {etype or 'something'} in {repo}"


def format_event_lines(event, token=None):
    """Return one or more IRC-friendly lines for an event.

    For PushEvent, includes commit summary lines (short SHA + subject).
    """

    header = format_event(event, token=token)
    lines = [header] if header else []

    etype = event.get("type")
    if etype != "PushEvent":
        return lines

    actor = (event.get("actor") or {}).get("login") or "someone"
    repo = (event.get("repo") or {}).get("name") or "unknown/repo"
    repo_tag = _repo_short(repo)
    payload = event.get("payload") or {}

    size = payload.get("size")
    total = size if isinstance(size, int) else len(payload.get("commits") or [])

    commits = payload.get("commits") or []
    commit_lines = _format_commit_lines(repo_tag, actor, commits, total_count=total)

    # Some GitHub Events payloads omit commits; fall back to compare API.
    # Only do this when authenticated to avoid burning the low anonymous quota.
    if not commit_lines and token:
        before = payload.get("before")
        head = payload.get("head")
        if before and head:
            cmp_commits = _compare_commits(repo, before, head, token=token)
            if not cmp_commits:
                # Force-pushes can produce an empty forward compare.
                cmp_commits = _compare_commits(repo, head, before, token=token)

            if cmp_commits:
                if not isinstance(total, int) or total <= 0:
                    total = len(cmp_commits)
                commit_lines = _format_commit_lines(
                    repo_tag,
                    actor,
                    cmp_commits,
                    total_count=total,
                )

    lines.extend(commit_lines)
    return lines


def _poll_interval(bot):
    cfg = (bot.config or {}).get("github", {})
    try:
        interval = int(cfg.get("poll_interval", DEFAULT_POLL_INTERVAL))
    except Exception:
        interval = DEFAULT_POLL_INTERVAL
    # GitHub unauthenticated API rate limit is very low; avoid hammering.
    if not _github_token(bot):
        interval = max(interval, 300)
    return max(15, interval)


def _poll_due(conn, bot):
    key = (id(conn), getattr(conn, "server_host", None), getattr(conn, "nick", None))
    now = time.time()
    last = _last_poll_by_conn.get(key, 0)
    interval = _poll_interval(bot)
    hint = _poll_interval_hint_by_conn.get(key)
    if isinstance(hint, int) and hint > 0:
        interval = max(interval, hint)
    if now - last < interval:
        return False
    _last_poll_by_conn[key] = now
    return True


def _fetch_repo_events(repo, token=None, etag=None):
    url = f"{API_BASE}/repos/{repo}/events"
    return _github_get_json(url, token=token, etag=etag)


def _post(conn, chan, text):
    if not text:
        return
    # IRC line safety (hard limit also enforced by core)
    text = str(text).replace("\n", " ").replace("\r", " ")
    conn.msg(chan, text[:450])


def _safe_one_line(text, limit=450):
    if text is None:
        return ""
    text = str(text).replace("\n", " ").replace("\r", " ")
    return text[:limit]


def _post_announcement(conn, chan, bot, text):
    # No embedded timestamps and no pseudo-nick prefix.
    _post(conn, chan, text)


def ghevent(inp, bot=None, input=None):
    """Show the latest public event for a repo.

    Use: `.git event owner/repo`
    """

    repo = _normalize_repo(inp)
    token = _github_token(bot)

    try:
        events, _, _ = _fetch_repo_events(repo, token=token)
    except http.HTTPError as e:
        code = getattr(e, "code", None)
        if code == 403:
            return "GitHub API rate limit exceeded (add api_keys.github token or increase github.poll_interval)"
        if code is not None:
            return f"GitHub API error (HTTP {code})"
        return "GitHub API error"
    except Exception:
        return "GitHub API error"
    if not events:
        return "no recent events"

    # If invoked from a command context, emit the same multi-line output we use
    # for channel announcements (header + optional commit lines).
    # Returning strings with newlines won't work: core will only send the first
    # line (see send_loop splitlines()[0]).
    if input is not None:
        for line in format_event_lines(events[0], token=token):
            input.reply(_safe_one_line(line))
        return None

    return format_event(events[0], token=token)


def _git_watch_list(rest, chan="", db=None):
    _db_init(db)

    parts = (rest or "").split()

    # Default to current channel if called in-channel; otherwise require explicit.
    target = chan
    if parts and parts[0].startswith("#"):
        target = parts[0]
    if not (target and target.startswith("#")):
        raise ValueError("please specify a channel (e.g. #mychan)")

    rows = db.execute(
        "select repo from github_watches where chan=? order by repo", (target,)
    ).fetchall()
    if not rows:
        return f"no watches for {target}"
    return f"watches for {target}: " + ", ".join(r[0] for r in rows)


def _git_watch_add(rest, chan="", db=None):
    _db_init(db)

    parts = (rest or "").split()
    if len(parts) < 1:
        raise ValueError("usage: .git add owner/repo [#channel]")

    repo = _normalize_repo(parts[0])
    target = parts[1] if len(parts) >= 2 and parts[1].startswith("#") else chan
    if not (target and target.startswith("#")):
        raise ValueError("please specify a channel (e.g. #mychan)")

    # Avoid confusing duplicates: tell the user if it's already being watched.
    exists = db.execute(
        "select 1 from github_watches where chan=? and repo=? limit 1",
        (target, repo),
    ).fetchone()
    if exists:
        return f"already watching {repo} in {target}"

    db.execute(
        "insert or ignore into github_watches(chan, repo, last_id, etag) values(?,?,NULL,NULL)",
        (target, repo),
    )
    db.commit()
    return f"ok, watching {repo} in {target}"


def _git_watch_remove(rest, chan="", db=None):
    _db_init(db)

    parts = (rest or "").split()
    if len(parts) < 1:
        raise ValueError("usage: .git remove owner/repo [#channel]")

    repo = _normalize_repo(parts[0])
    target = parts[1] if len(parts) >= 2 and parts[1].startswith("#") else chan
    if not (target and target.startswith("#")):
        raise ValueError("please specify a channel (e.g. #mychan)")

    cur = db.execute("delete from github_watches where chan=? and repo=?", (target, repo))
    db.commit()
    if cur.rowcount:
        return f"ok, stopped watching {repo} in {target}"
    return f"not watching {repo} in {target}"


@hook.command("git")
def git(inp, chan="", db=None, bot=None, nick="", input=None):
    """GitHub helper commands (subcommand-style).

    Usage:
            .git event owner/repo
            .git add owner/repo [#channel]
            .git remove owner/repo [#channel]
            .git list [#channel]

    Notes:
      - Set token in config.json: api_keys.github
      - Poll interval: github.poll_interval (seconds)
    """

    parts = (inp or "").split(None, 1)
    if not parts:
        return None

    sub = parts[0].lower()
    rest = parts[1] if len(parts) > 1 else ""

    try:
        if sub in ("event", "events", "latest"):
            return ghevent(rest, bot=bot, input=input)

        if sub in ("add", "watch"):
            return _git_watch_add(rest, chan=chan, db=db)

        if sub in ("remove", "rm", "del", "unwatch"):
            return _git_watch_remove(rest, chan=chan, db=db)

        if sub in ("list", "ls"):
            return _git_watch_list(rest, chan=chan, db=db)
    except ValueError as e:
        return str(e)

    return "unknown subcommand (try: event, add, remove, list)"


@hook.singlethread
@hook.event("*")
def github_poll(inp, conn=None, db=None, bot=None):
    # Poll periodically on incoming server traffic (PINGs, joins, chat, etc.)
    if conn is None or db is None or bot is None:
        return

    if not _poll_due(conn, bot):
        return

    _db_init(db)
    token = _github_token(bot)

    watches = db.execute(
        "select chan, repo, last_id, etag from github_watches order by chan, repo"
    ).fetchall()
    if not watches:
        return

    # With PostgreSQL, watches often persist across restarts, so it's common to
    # have more repos watched. Anonymous GitHub API rate limits are very low.
    # To avoid silently rate-limiting and appearing "broken", shard polling
    # across cycles when unauthenticated.
    if not token and len(watches) > MAX_REPOS_PER_POLL_NO_TOKEN:
        key = (
            id(conn),
            getattr(conn, "server_host", None),
            getattr(conn, "nick", None),
        )
        start = int(_poll_cursor_by_conn.get(key, 0)) % len(watches)
        subset = []
        for i in range(MAX_REPOS_PER_POLL_NO_TOKEN):
            subset.append(watches[(start + i) % len(watches)])
        _poll_cursor_by_conn[key] = start + MAX_REPOS_PER_POLL_NO_TOKEN
        watches = subset

    for chan, repo, last_id, etag in watches:
        try:
            events, new_etag, poll_interval = _fetch_repo_events(
                repo, token=token, etag=etag
            )
            if poll_interval:
                key = (
                    id(conn),
                    getattr(conn, "server_host", None),
                    getattr(conn, "nick", None),
                )
                prev = _poll_interval_hint_by_conn.get(key)
                if not isinstance(prev, int) or poll_interval > prev:
                    _poll_interval_hint_by_conn[key] = poll_interval
        except http.HTTPError as e:
            # Avoid spamming the channel on API errors, but do give a periodic
            # hint when we're rate-limited (otherwise it looks like watches are
            # broken).
            code = getattr(e, "code", None)
            if code == 403:
                warn_key = (chan, repo)
                now = _now()
                last_warn = int(_last_rate_limit_warn.get(warn_key, 0) or 0)
                if now - last_warn >= RATE_LIMIT_WARN_COOLDOWN:
                    _last_rate_limit_warn[warn_key] = now
                    _post_announcement(
                        conn,
                        chan,
                        bot,
                        "GitHub API rate limit hit; set api_keys.github token or reduce watched repos / increase github.poll_interval",
                    )
            continue
        except Exception:
            continue

        if events is None:
            # 304 Not Modified
            continue

        if not events:
            continue

        newest_id = events[0].get("id")

        # First time: set cursor but don't spam the channel.
        if not last_id:
            db.execute(
                "update github_watches set last_id=?, etag=? where chan=? and repo=?",
                (newest_id, new_etag, chan, repo),
            )
            db.commit()
            continue

        # Collect events after last_id (chronological).
        new_events = []
        collecting = False
        for ev in reversed(events):
            ev_id = ev.get("id")
            if ev_id == last_id:
                collecting = True
                continue
            if collecting:
                new_events.append(ev)

        # If last_id fell out of the window, just move the cursor.
        if not collecting:
            db.execute(
                "update github_watches set last_id=?, etag=? where chan=? and repo=?",
                (newest_id, new_etag, chan, repo),
            )
            db.commit()
            continue

        if not new_events:
            db.execute(
                "update github_watches set last_id=?, etag=? where chan=? and repo=?",
                (newest_id, new_etag, chan, repo),
            )
            db.commit()
            continue

        overflow = max(0, len(new_events) - MAX_EVENTS_PER_POLL)
        if overflow:
            new_events = new_events[-MAX_EVENTS_PER_POLL:]

        for ev in new_events:
            for line in format_event_lines(ev, token=token):
                _post_announcement(conn, chan, bot, line)

        if overflow:
            _post_announcement(conn, chan, bot, f"(+{overflow} more events)")

        db.execute(
            "update github_watches set last_id=?, etag=? where chan=? and repo=?",
            (newest_id, new_etag, chan, repo),
        )
        db.commit()
