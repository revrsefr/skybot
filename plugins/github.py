import json
import re
import time
import hashlib
import hmac
import threading
import os
import inspect


try:
    import flask
    Flask = flask.Flask
except Exception:
    flask = None
    Flask = None

from util import hook, http


API_BASE = "https://api.github.com"
DEFAULT_POLL_INTERVAL = 60
MAX_EVENTS_PER_POLL = 3
MAX_COMMITS_PER_PUSH = 3
MAX_REPOS_PER_POLL_NO_TOKEN = 4
RATE_LIMIT_WARN_COOLDOWN = 3600

DEFAULT_WEBHOOK_HOST = "0.0.0.0"
DEFAULT_WEBHOOK_PORT = 9001
DEFAULT_WEBHOOK_PATH = "/github/webhook"
DEFAULT_WEBHOOK_EVENTS = ["push", "pull_request", "issues"]

# IRC formatting controls (mIRC-style)
IRC_COLOR = "\x03"
IRC_RESET = "\x0f"
IRC_UNDERLINE = "\x1f"

DEFAULT_IRC_COLORS = {
    # Matches the examples in the request.
    "repo": "02",
    "actor": "07",
    "sha": "03",
    "ref": "03",
    "url": "22",
}

_repo_re = re.compile(r"^(?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+)$")

_last_poll_by_conn = {}
_poll_cursor_by_conn = {}
_last_rate_limit_warn = {}
_poll_interval_hint_by_conn = {}

_webhook_lock = threading.Lock()
_webhook_html_cache = None


def _ignored_event_types(bot):
    """Return a set of GitHub Events API `type` strings to ignore."""

    cfg = (getattr(bot, "config", None) or {}).get("github", {})
    ignored = cfg.get("ignore_event_types")
    if ignored is None:
        # Default: reduce noise. Forks and stars are very high volume.
        ignored = ["WatchEvent", "ForkEvent"]
    if isinstance(ignored, str):
        ignored = [ignored]
    try:
        return {str(x) for x in (ignored or []) if x}
    except Exception:
        return {"WatchEvent", "ForkEvent"}


def _github_color_config(bot):
    cfg = (getattr(bot, "config", None) or {}).get("github", {})
    colorize = cfg.get("colorize")
    if colorize is None:
        colorize = True
    try:
        colorize = bool(colorize)
    except Exception:
        colorize = True

    colors = dict(DEFAULT_IRC_COLORS)
    custom = cfg.get("colors") or {}
    if isinstance(custom, dict):
        for k, v in custom.items():
            if v is None:
                continue
            colors[str(k)] = str(v)
    return colorize, colors


def _irc_colorize(text, color=None, enabled=True):
    if not enabled:
        return str(text) if text is not None else ""
    if text is None:
        return ""
    text = str(text)
    if not text:
        return ""
    if not color:
        return text

    c = str(color).strip()
    # Normalize to two-digit color codes when possible.
    try:
        c_int = int(c)
        if 0 <= c_int <= 99:
            c = f"{c_int:02d}"
    except Exception:
        pass

    return f"{IRC_COLOR}{c}{text}{IRC_RESET}"


def _irc_url(url, color=None, enabled=True, underline=True):
    if not enabled:
        return str(url) if url is not None else ""
    if not url:
        return ""
    u = str(url)
    prefix = ""
    suffix = IRC_RESET
    if color:
        prefix += f"{IRC_COLOR}{str(color).strip()}"
    if underline:
        prefix += IRC_UNDERLINE
        suffix = f"{IRC_UNDERLINE}{IRC_RESET}"
    return f"{prefix}{u}{suffix}"


def _fmt_repo_tag(repo_tag, enabled=True, colors=None):
    if colors is None:
        colors = DEFAULT_IRC_COLORS
    inner = _irc_colorize(repo_tag, colors.get("repo"), enabled=enabled)
    return f"[{inner}]"


def _is_ignored_event(event, bot):
    etype = (event or {}).get("type")
    return etype in _ignored_event_types(bot)


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


def _db_init_private(db):
    db.execute(
        "create table if not exists github_watches_private ("
        "chan text not null, "
        "repo text not null, "
        "primary key (chan, repo)"
        ")"
    )
    db.commit()


def _github_token(bot):
    # Optional: set in config.json under api_keys.github
    bot_keys = (bot.config or {}).get("api_keys", {})
    return bot_keys.get("github")


def _webhook_secret(bot):
    cfg = (getattr(bot, "config", None) or {}).get("github", {})
    secret = cfg.get("webhook_secret")
    if secret is None:
        return None
    secret = str(secret).strip()
    return secret or None


def _webhook_events(bot):
    cfg = (getattr(bot, "config", None) or {}).get("github", {})
    events = cfg.get("webhook_events") or DEFAULT_WEBHOOK_EVENTS
    if isinstance(events, str):
        events = [events]
    try:
        return {str(x).strip().lower() for x in events if x}
    except Exception:
        return set(DEFAULT_WEBHOOK_EVENTS)


def _webhook_listen_config(bot):
    cfg = (getattr(bot, "config", None) or {}).get("github", {})
    host = cfg.get("webhook_host", DEFAULT_WEBHOOK_HOST)
    port = cfg.get("webhook_port", DEFAULT_WEBHOOK_PORT)
    path = cfg.get("webhook_path", DEFAULT_WEBHOOK_PATH)
    enabled = cfg.get("webhook_enabled")
    if enabled is None:
        enabled = True
    try:
        enabled = bool(enabled)
    except Exception:
        enabled = True
    try:
        port = int(port)
    except Exception:
        port = DEFAULT_WEBHOOK_PORT
    path = "/" + str(path).lstrip("/") if path else DEFAULT_WEBHOOK_PATH
    return enabled, str(host), port, path


def _verify_webhook_signature(secret, payload, signature_header):
    if not secret or not signature_header:
        return False
    sig = str(signature_header)
    if not sig.startswith("sha256="):
        return False
    given = sig.split("=", 1)[1]
    mac = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(given, mac)


def _webhook_payload(request_obj):
    data = None
    try:
        data = request_obj.get_json(silent=True)
    except Exception:
        data = None
    if data is None:
        try:
            raw = request_obj.form.get("payload")
        except Exception:
            raw = None
        if raw:
            try:
                data = json.loads(raw)
            except Exception:
                data = None
    return data


def _webhook_event_to_event(event_name, payload):
    repo = ((payload or {}).get("repository") or {}).get("full_name")
    sender = ((payload or {}).get("sender") or {}).get("login")
    if not repo:
        return None

    if event_name == "push":
        commits = payload.get("commits") or []
        event_commits = []
        for c in commits:
            sha = (c or {}).get("id")
            msg = (c or {}).get("message")
            if sha or msg:
                event_commits.append({"sha": sha, "message": msg})
        return {
            "type": "PushEvent",
            "actor": {"login": sender or (payload.get("pusher") or {}).get("name")},
            "repo": {"name": repo},
            "payload": {
                "ref": payload.get("ref"),
                "size": payload.get("size"),
                "commits": event_commits,
                "head": payload.get("after"),
                "before": payload.get("before"),
            },
        }

    if event_name == "pull_request":
        return {
            "type": "PullRequestEvent",
            "actor": {"login": sender},
            "repo": {"name": repo},
            "payload": {
                "action": payload.get("action"),
                "number": payload.get("number"),
                "pull_request": payload.get("pull_request"),
            },
        }

    if event_name == "issues":
        return {
            "type": "IssuesEvent",
            "actor": {"login": sender},
            "repo": {"name": repo},
            "payload": {
                "action": payload.get("action"),
                "issue": payload.get("issue"),
            },
        }

    return None


def _webhook_targets(bot, repo_full):
    targets = []
    if not bot or not repo_full:
        return targets
    for conn in getattr(bot, "conns", {}).values():
        try:
            db = bot.get_db_connection(conn)
        except Exception:
            continue
        try:
            _db_init_private(db)
            rows = db.execute(
                "select chan from github_watches_private where repo=? order by chan",
                (repo_full,),
            ).fetchall()
        except Exception:
            rows = []
        for row in rows or []:
            try:
                chan = row[0]
            except Exception:
                continue
            if chan:
                targets.append((conn, chan))
    return targets


def _webhook_status(bot):
    existing = getattr(bot, "_github_webhook_server", None) if bot is not None else None
    if isinstance(existing, dict):
        return bool(existing.get("running")), existing.get("error")
    return False, None


def _load_webhook_html():
    global _webhook_html_cache
    if _webhook_html_cache is not None:
        return _webhook_html_cache
    try:
        base_dir = os.path.dirname(os.path.abspath(__file__))
    except Exception:
        src = inspect.getsourcefile(_load_webhook_html)
        base_dir = os.path.dirname(os.path.abspath(src)) if src else os.getcwd()
    html_path = os.path.join(base_dir, "github_webhook.html")
    try:
        with open(html_path, "r", encoding="utf-8") as fh:
            _webhook_html_cache = fh.read()
            return _webhook_html_cache
    except Exception:
        _webhook_html_cache = (
            "<!doctype html>"
            "<html><head><title>GitHub Webhook</title></head>"
            "<body>"
            "<h1>GitHub Webhook Endpoint</h1>"
            "<p>This URL only accepts signed POST requests from GitHub.</p>"
            "</body></html>"
        )
        return _webhook_html_cache


def _ensure_webhook_server(bot):
    if bot is None:
        return False
    if Flask is None or flask is None:
        bot._github_webhook_server = {"running": False, "error": "flask_missing"}
        return False
    enabled, host, port, path = _webhook_listen_config(bot)
    if not enabled:
        bot._github_webhook_server = {"running": False, "error": "disabled"}
        return False
    if not _webhook_secret(bot):
        bot._github_webhook_server = {"running": False, "error": "missing_secret"}
        return False

    existing = getattr(bot, "_github_webhook_server", None)
    if isinstance(existing, dict) and existing.get("running"):
        return True

    with _webhook_lock:
        existing = getattr(bot, "_github_webhook_server", None)
        if isinstance(existing, dict) and existing.get("running"):
            return True

        app = Flask("skybot_github_webhook")

        @app.post(path)
        def github_webhook():
            cfg_bot = bot
            secret = _webhook_secret(cfg_bot)
            if not secret:
                return flask.abort(401)

            signature = flask.request.headers.get("X-Hub-Signature-256")
            payload_bytes = flask.request.get_data() or b""
            if not _verify_webhook_signature(secret, payload_bytes, signature):
                return flask.abort(401)

            event_name = (flask.request.headers.get("X-GitHub-Event") or "").lower()
            if event_name == "ping":
                return "ok", 200

            payload = _webhook_payload(flask.request)
            if payload is None:
                return flask.abort(400)

            if event_name not in _webhook_events(cfg_bot):
                return "", 204

            repo_full = ((payload or {}).get("repository") or {}).get("full_name")
            if not repo_full:
                return "", 204

            event = _webhook_event_to_event(event_name, payload)
            if not event:
                return "", 204

            targets = _webhook_targets(cfg_bot, repo_full)
            if not targets:
                return "", 204

            for line in format_event_lines(event, token=None, bot=cfg_bot):
                for conn, chan in targets:
                    _post_announcement(conn, chan, cfg_bot, line)

            return "ok", 200

        @app.get(path)
        def github_webhook_info():
            # Friendly HTML for browsers hitting the webhook URL directly.
            html = _load_webhook_html()
            return html, 200, {"Content-Type": "text/html; charset=utf-8"}

        def _run():
            app.run(host=host, port=port, threaded=True, use_reloader=False)

        thread = threading.Thread(target=_run, name="github-webhook", daemon=True)
        bot._github_webhook_server = {
            "running": True,
            "thread": thread,
            "host": host,
            "port": port,
            "path": path,
        }
        thread.start()
        return True

    return False


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


def _format_commit_lines(repo_tag, actor, commits, total_count=None, *, enabled=True, colors=None):
    if not commits:
        return []

    if colors is None:
        colors = DEFAULT_IRC_COLORS

    repo_disp = _fmt_repo_tag(repo_tag, enabled=enabled, colors=colors)
    actor_disp = _irc_colorize(actor, colors.get("actor"), enabled=enabled)

    show = commits[-MAX_COMMITS_PER_PUSH:]
    lines = []
    for c in show:
        sha = _short_sha((c or {}).get("sha"))
        subject = _commit_subject((c or {}).get("message"))
        sha_disp = _irc_colorize(sha, colors.get("sha"), enabled=enabled)
        if sha and subject:
            lines.append(f"{repo_disp} {actor_disp} {sha_disp} - {subject}")
        elif sha:
            lines.append(f"{repo_disp} {actor_disp} {sha_disp}")
        elif subject:
            lines.append(f"{repo_disp} {actor_disp} - {subject}")

    if isinstance(total_count, int) and total_count > len(show):
        more = total_count - len(show)
        if more > 0:
            lines.append(f"{repo_disp} (+{more} more commits)")

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


def _pr_html_url(repo_full, number):
    if not repo_full or number is None:
        return None
    try:
        n = int(number)
    except Exception:
        return None
    if n <= 0:
        return None
    return f"https://github.com/{repo_full}/pull/{n}"


def _issue_html_url(repo_full, number):
    if not repo_full or number is None:
        return None
    try:
        n = int(number)
    except Exception:
        return None
    if n <= 0:
        return None
    return f"https://github.com/{repo_full}/issues/{n}"


def format_event(event, token=None, bot=None):
    """Format a GitHub Events API item into a short IRC-friendly line."""

    etype = event.get("type")
    actor = (event.get("actor") or {}).get("login") or "someone"
    repo = (event.get("repo") or {}).get("name") or "unknown/repo"
    payload = event.get("payload") or {}

    colorize, colors = _github_color_config(bot)
    repo_tag = _repo_short(repo)
    repo_disp = _fmt_repo_tag(repo_tag, enabled=colorize, colors=colors)
    actor_disp = _irc_colorize(actor, colors.get("actor"), enabled=colorize)

    if etype == "PushEvent":
        ref = (payload.get("ref") or "").replace("refs/heads/", "")
        size = payload.get("size")
        commits = payload.get("commits") or []
        head = payload.get("head")
        before = payload.get("before")
        verb = "pushed"
        count = size if isinstance(size, int) else len(commits)

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

        sha_disp = _irc_colorize(_short_sha(head), colors.get("sha"), enabled=colorize)
        ref_disp = _irc_colorize(ref, colors.get("ref"), enabled=colorize)

        bits = [f"{repo_disp} {actor_disp} {verb} {count} commit(s)"]
        if ref:
            bits.append(f"to {ref_disp}")
        else:
            bits.append(f"to {repo_tag}")

        if stats:
            additions, deletions, files_changed, _compare_commit_count = stats
            bits.append(f"[+{additions}/-{deletions}/\u00b1{files_changed}]")

        if compare:
            bits.append(_irc_url(compare, colors.get("url"), enabled=colorize, underline=True))
        elif head:
            bits.append(sha_disp)

        return " ".join(bits)

    if etype == "PullRequestEvent":
        action = payload.get("action") or "updated"
        pr = payload.get("pull_request") or {}
        number = pr.get("number") or payload.get("number")
        title = (pr.get("title") or "").strip()
        url = pr.get("html_url") or _pr_html_url(repo, number)
        bits = [f"{repo_disp} {actor_disp} {action} PR"]
        if number is not None:
            bits[-1] += f" #{number}"
        if title:
            bits.append(f"\"{title}\"")
        bits.append(f"in {repo}")
        if url:
            bits.append(_irc_url(url, colors.get("url"), enabled=colorize, underline=True))
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
        if not url:
            url = _pr_html_url(repo, number)
        state = None
        if isinstance(review, dict):
            state = (review.get("state") or "").lower() or None
        bits = [f"{repo_disp} {actor_disp} {action} PR review"]
        if number is not None:
            bits[-1] += f" #{number}"
        if state:
            bits.append(f"({state})")
        if title:
            bits.append(f"\"{title}\"")
        bits.append(f"in {repo}")
        if url:
            bits.append(_irc_url(url, colors.get("url"), enabled=colorize, underline=True))
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
        if not url:
            url = _pr_html_url(repo, number)
        bits = [f"{repo_disp} {actor_disp} {action} on PR review"]
        if number is not None:
            bits[-1] += f" #{number}"
        if title:
            bits.append(f"\"{title}\"")
        bits.append(f"in {repo}")
        if url:
            bits.append(_irc_url(url, colors.get("url"), enabled=colorize, underline=True))
        return " ".join(bits)

    if etype == "IssuesEvent":
        action = payload.get("action") or "updated"
        issue = payload.get("issue") or {}
        number = issue.get("number")
        title = (issue.get("title") or "").strip()
        url = issue.get("html_url") or _issue_html_url(repo, number)
        bits = [f"{repo_disp} {actor_disp} {action} issue"]
        if number is not None:
            bits[-1] += f" #{number}"
        if title:
            bits.append(f"\"{title}\"")
        bits.append(f"in {repo}")
        if url:
            bits.append(_irc_url(url, colors.get("url"), enabled=colorize, underline=True))
        return " ".join(bits)

    if etype == "IssueCommentEvent":
        action = payload.get("action") or "commented"
        issue = payload.get("issue") or {}
        number = issue.get("number")
        url = (
            (payload.get("comment") or {}).get("html_url")
            or issue.get("html_url")
            or _issue_html_url(repo, number)
        )
        bits = [f"{repo_disp} {actor_disp} {action} on issue"]
        if number is not None:
            bits[-1] += f" #{number}"
        bits.append(f"in {repo}")
        if url:
            bits.append(_irc_url(url, colors.get("url"), enabled=colorize, underline=True))
        return " ".join(bits)

    if etype == "ReleaseEvent":
        action = payload.get("action") or "published"
        release = payload.get("release") or {}
        tag = release.get("tag_name")
        url = release.get("html_url")
        bits = [f"{repo_disp} {actor_disp} {action} release"]
        if tag:
            bits[-1] += f" {tag}"
        bits.append(f"in {repo}")
        if url:
            bits.append(_irc_url(url, colors.get("url"), enabled=colorize, underline=True))
        return " ".join(bits)

    if etype == "CreateEvent":
        ref_type = payload.get("ref_type") or "ref"
        ref = payload.get("ref")
        bits = [f"{repo_disp} {actor_disp} created {ref_type}"]
        if ref:
            bits[-1] += f" {ref}"
        bits.append(f"in {repo}")
        return " ".join(bits)

    if etype == "DeleteEvent":
        ref_type = payload.get("ref_type") or "ref"
        ref = payload.get("ref")
        bits = [f"{repo_disp} {actor_disp} deleted {ref_type}"]
        if ref:
            bits[-1] += f" {ref}"
        bits.append(f"in {repo}")
        return " ".join(bits)

    if etype == "ForkEvent":
        forkee = payload.get("forkee") or {}
        full_name = forkee.get("full_name")
        url = forkee.get("html_url")
        bits = [f"{repo_disp} {actor_disp} forked {repo}"]
        if full_name:
            bits.append(f"to {full_name}")
        if url:
            bits.append(_irc_url(url, colors.get("url"), enabled=colorize, underline=True))
        return " ".join(bits)

    if etype == "WatchEvent":
        action = payload.get("action") or "starred"
        return f"{repo_disp} {actor_disp} {action} {repo}"

    # Fallback
    url = _extract_event_url(payload)
    if url:
        u = _irc_url(url, colors.get("url"), enabled=colorize, underline=True)
        return f"{repo_disp} {actor_disp} did {etype or 'something'} in {repo} {u}"
    return f"{repo_disp} {actor_disp} did {etype or 'something'} in {repo}"


def format_event_lines(event, token=None, bot=None):
    """Return one or more IRC-friendly lines for an event.

    For PushEvent, includes commit summary lines (short SHA + subject).
    """

    header = format_event(event, token=token, bot=bot)
    lines = [header] if header else []

    etype = event.get("type")
    if etype != "PushEvent":
        return lines

    actor = (event.get("actor") or {}).get("login") or "someone"
    repo = (event.get("repo") or {}).get("name") or "unknown/repo"
    repo_tag = _repo_short(repo)
    payload = event.get("payload") or {}

    colorize, colors = _github_color_config(bot)

    size = payload.get("size")
    total = size if isinstance(size, int) else len(payload.get("commits") or [])

    commits = payload.get("commits") or []
    commit_lines = _format_commit_lines(
        repo_tag,
        actor,
        commits,
        total_count=total,
        enabled=colorize,
        colors=colors,
    )

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
                    enabled=colorize,
                    colors=colors,
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


def _request_interval(bot):
    cfg = (getattr(bot, "config", None) or {}).get("github", {})
    try:
        return max(0, int(cfg.get("request_interval", 0)))
    except Exception:
        return 0


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

    # Skip noisy/ignored event types (e.g. WatchEvent/ForkEvent).
    show_event = None
    for ev in events:
        if not _is_ignored_event(ev, bot):
            show_event = ev
            break
    if show_event is None:
        return "no recent events"

    # If invoked from a command context, emit the same multi-line output we use
    # for channel announcements (header + optional commit lines).
    # Returning strings with newlines won't work: core will only send the first
    # line (see send_loop splitlines()[0]).
    if input is not None:
        for line in format_event_lines(show_event, token=token):
            input.reply(_safe_one_line(line))
        return None

    return format_event(show_event, token=token, bot=bot)


def _git_watch_list(rest, chan="", db=None):
    _db_init(db)
    _db_init_private(db)

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
    private_rows = db.execute(
        "select repo from github_watches_private where chan=? order by repo", (target,)
    ).fetchall()
    if not rows and not private_rows:
        return f"no watches for {target}"
    entries = [str(r[0]) for r in rows]
    entries.extend(f"{r[0]} (private)" for r in private_rows)
    return f"watches for {target}: " + ", ".join(entries)


def _git_watch_add(rest, chan="", db=None, bot=None):
    _db_init(db)
    _db_init_private(db)

    parts = (rest or "").split()
    if len(parts) < 1:
        raise ValueError("usage: .git add owner/repo [#channel] [private]")

    repo = _normalize_repo(parts[0])
    target = chan
    private = False
    for token in parts[1:]:
        if token.startswith("#"):
            target = token
        elif token.lower() == "private":
            private = True
    if not (target and target.startswith("#")):
        raise ValueError("please specify a channel (e.g. #mychan)")

    if private:
        _ensure_webhook_server(bot)
        running, err = _webhook_status(bot)
        exists = db.execute(
            "select 1 from github_watches_private where chan=? and repo=? limit 1",
            (target, repo),
        ).fetchone()
        if exists:
            return f"already watching {repo} in {target} (private)"
        db.execute(
            "insert or ignore into github_watches_private(chan, repo) values(?,?)",
            (target, repo),
        )
        db.commit()
        if running:
            return f"ok, watching {repo} in {target} (private via webhook)"
        if err == "flask_missing":
            return (
                f"ok, watching {repo} in {target} (private via webhook; listener not running: Flask missing)"
            )
        if err == "missing_secret":
            return (
                f"ok, watching {repo} in {target} (private via webhook; listener not running: github.webhook_secret not set)"
            )
        if err == "disabled":
            return (
                f"ok, watching {repo} in {target} (private via webhook; listener not running: github.webhook_enabled=false)"
            )
        return f"ok, watching {repo} in {target} (private via webhook; listener not running)"

    # Public watch: keep original behavior.
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
    _db_init_private(db)

    parts = (rest or "").split()
    if len(parts) < 1:
        raise ValueError("usage: .git remove owner/repo [#channel]")

    repo = _normalize_repo(parts[0])
    target = parts[1] if len(parts) >= 2 and parts[1].startswith("#") else chan
    if not (target and target.startswith("#")):
        raise ValueError("please specify a channel (e.g. #mychan)")

    cur = db.execute("delete from github_watches where chan=? and repo=?", (target, repo))
    cur_private = db.execute(
        "delete from github_watches_private where chan=? and repo=?", (target, repo)
    )
    db.commit()
    if (getattr(cur, "rowcount", 0) or 0) or (getattr(cur_private, "rowcount", 0) or 0):
        return f"ok, stopped watching {repo} in {target}"
    return f"not watching {repo} in {target}"


@hook.command("git")
def git(inp, chan="", db=None, bot=None, nick="", input=None):
    """GitHub helper commands (subcommand-style).

    Usage:
            .git event owner/repo
            .git add owner/repo [#channel] [private]
            .git remove owner/repo [#channel]
            .git list [#channel]

    Notes:
      - Set token in config.json: api_keys.github
        - Poll interval: github.poll_interval (seconds)
            - Ignore noisy event types: github.ignore_event_types (default: ["WatchEvent", "ForkEvent"])
        - Private repos: .git add owner/repo private + github.webhook_secret
    """

    parts = (inp or "").split(None, 1)
    if not parts:
        return None

    _ensure_webhook_server(bot)

    sub = parts[0].lower()
    rest = parts[1] if len(parts) > 1 else ""

    try:
        if sub in ("event", "events", "latest"):
            return ghevent(rest, bot=bot, input=input)

        if sub in ("add", "watch"):
            return _git_watch_add(rest, chan=chan, db=db, bot=bot)

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

    _ensure_webhook_server(bot)

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

    req_delay = _request_interval(bot)
    for i, (chan, repo, last_id, etag) in enumerate(watches):
        if i > 0 and req_delay > 0:
            time.sleep(req_delay)
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
            if _is_ignored_event(ev, bot):
                continue
            for line in format_event_lines(ev, token=token, bot=bot):
                _post_announcement(conn, chan, bot, line)

        if overflow:
            _post_announcement(conn, chan, bot, f"(+{overflow} more events)")

        db.execute(
            "update github_watches set last_id=?, etag=? where chan=? and repo=?",
            (newest_id, new_etag, chan, repo),
        )
        db.commit()
