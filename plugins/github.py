import json
import re
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

from util import hook


MAX_COMMITS_PER_PUSH = 3
MAX_EVENTS_PER_WEBHOOK = 3

DEFAULT_WEBHOOK_HOST = "0.0.0.0"
DEFAULT_WEBHOOK_PORT = 9001
DEFAULT_WEBHOOK_PATH = "/github/webhook"
DEFAULT_WEBHOOK_EVENTS = ["push", "pull_request", "issues"]

# IRC formatting controls (mIRC-style)
IRC_COLOR = "\x03"
IRC_RESET = "\x0f"
IRC_UNDERLINE = "\x1f"

DEFAULT_IRC_COLORS = {
    "repo": "02",
    "actor": "07",
    "sha": "03",
    "ref": "03",
    "url": "22",
}

_repo_re = re.compile(r"^(?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+)$")

_webhook_lock = threading.Lock()
_webhook_html_cache = None


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

def _ignored_event_types(bot):
    cfg = (getattr(bot, "config", None) or {}).get("github", {})
    ignored = cfg.get("ignore_event_types")
    if ignored is None:
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


# ---------------------------------------------------------------------------
# IRC formatting helpers
# ---------------------------------------------------------------------------

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


def _repo_short(repo_full):
    if not repo_full:
        return "unknown"
    return repo_full.split("/", 1)[-1]


def _compare_url(repo_full, before, head):
    if not (repo_full and before and head):
        return None
    return f"https://github.com/{repo_full}/compare/{before}...{head}"


def _short_sha(sha):
    if not sha:
        return ""
    return sha[:7]


def _commit_subject(message):
    if not message:
        return ""
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


def _extract_event_url(payload):
    if not isinstance(payload, dict):
        return None
    for key in ("comment", "review", "pull_request", "issue", "release", "forkee"):
        obj = payload.get(key) or {}
        if isinstance(obj, dict):
            url = obj.get("html_url")
            if url:
                return url
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


# ---------------------------------------------------------------------------
# Event formatting
# ---------------------------------------------------------------------------

def format_event(event, bot=None):
    """Format a GitHub webhook event into a short IRC-friendly line."""

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
        commits = payload.get("commits") or []
        size = payload.get("size")
        count = size if isinstance(size, int) and size > 0 else len(commits)
        head = payload.get("head")
        before = payload.get("before")
        compare = _compare_url(repo, before, head)
        sha_disp = _irc_colorize(_short_sha(head), colors.get("sha"), enabled=colorize)
        ref_disp = _irc_colorize(ref, colors.get("ref"), enabled=colorize)

        bits = [f"{repo_disp} {actor_disp} pushed {count} commit(s)"]
        bits.append(f"to {ref_disp}" if ref else f"to {repo_tag}")
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
            bits.append(f'"{title}"')
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
        url = (review.get("html_url") if isinstance(review, dict) else None) or pr.get("html_url")
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
            bits.append(f'"{title}"')
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
        url = (comment.get("html_url") if isinstance(comment, dict) else None) or pr.get("html_url")
        if not url:
            url = _pr_html_url(repo, number)
        bits = [f"{repo_disp} {actor_disp} {action} on PR review"]
        if number is not None:
            bits[-1] += f" #{number}"
        if title:
            bits.append(f'"{title}"')
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
            bits.append(f'"{title}"')
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


def format_event_lines(event, bot=None):
    """Return one or more IRC-friendly lines for an event.

    For PushEvent, emit commit summary lines (short SHA + subject).
    All data comes from the webhook payload — no GitHub API calls are made.
    """

    header = format_event(event, bot=bot)
    lines = [header] if header else []

    etype = event.get("type")
    if etype != "PushEvent":
        return lines

    actor = (event.get("actor") or {}).get("login") or "someone"
    repo = (event.get("repo") or {}).get("name") or "unknown/repo"
    repo_tag = _repo_short(repo)
    payload = event.get("payload") or {}

    colorize, colors = _github_color_config(bot)
    commits = payload.get("commits") or []
    size = payload.get("size")
    total = size if isinstance(size, int) and size > 0 else len(commits)

    commit_lines = _format_commit_lines(
        repo_tag,
        actor,
        commits,
        total_count=total,
        enabled=colorize,
        colors=colors,
    )
    lines.extend(commit_lines)
    return lines


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

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


def _db_init_legacy(db):
    """Legacy private-watches table — kept for backward compatibility."""
    db.execute(
        "create table if not exists github_watches_private ("
        "chan text not null, "
        "repo text not null, "
        "primary key (chan, repo)"
        ")"
    )
    db.commit()


def _normalize_repo(repo):
    repo = (repo or "").strip()
    m = _repo_re.match(repo)
    if not m:
        raise ValueError("expected owner/repo")
    return f"{m.group('owner')}/{m.group('repo')}"


# ---------------------------------------------------------------------------
# Webhook helpers
# ---------------------------------------------------------------------------

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
                "size": len(event_commits) or payload.get("size"),
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
    """Return (conn, chan) pairs across ALL bot connections that watch repo_full."""
    targets = []
    if not bot or not repo_full:
        return targets
    for conn in getattr(bot, "conns", {}).values():
        try:
            db = bot.get_db_connection(conn)
        except Exception:
            continue
        # Primary watch table
        try:
            _db_init(db)
            rows = db.execute(
                "select chan from github_watches where repo=? order by chan",
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
        # Legacy private-watches table (backward compat)
        try:
            _db_init_legacy(db)
            legacy_rows = db.execute(
                "select chan from github_watches_private where repo=? order by chan",
                (repo_full,),
            ).fetchall()
        except Exception:
            legacy_rows = []
        for row in legacy_rows or []:
            try:
                chan = row[0]
            except Exception:
                continue
            if chan and (conn, chan) not in targets:
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

            for line in format_event_lines(event, bot=cfg_bot):
                for conn, chan in targets:
                    _post_announcement(conn, chan, cfg_bot, line)

            return "ok", 200

        @app.get(path)
        def github_webhook_info():
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


def _post(conn, chan, text):
    if not text:
        return
    text = str(text).replace("\n", " ").replace("\r", " ")
    conn.msg(chan, text[:450])


def _post_announcement(conn, chan, bot, text):
    _post(conn, chan, text)


# ---------------------------------------------------------------------------
# Watch management commands
# ---------------------------------------------------------------------------

def _git_watch_list(rest, chan="", db=None):
    _db_init(db)
    _db_init_legacy(db)

    parts = (rest or "").split()
    target = chan
    if parts and parts[0].startswith("#"):
        target = parts[0]
    if not (target and target.startswith("#")):
        raise ValueError("please specify a channel (e.g. #mychan)")

    rows = db.execute(
        "select repo from github_watches where chan=? order by repo", (target,)
    ).fetchall()
    legacy_rows = db.execute(
        "select repo from github_watches_private where chan=? order by repo", (target,)
    ).fetchall()

    if not rows and not legacy_rows:
        return f"no watches for {target}"

    entries = [str(r[0]) for r in rows]
    entries.extend(f"{r[0]} (legacy)" for r in legacy_rows)
    return f"watches for {target}: " + ", ".join(entries)


def _git_watch_add(rest, chan="", db=None, bot=None):
    _db_init(db)

    parts = (rest or "").split()
    if len(parts) < 1:
        raise ValueError("usage: .git add owner/repo [#channel]")

    repo = _normalize_repo(parts[0])
    target = chan
    for tok in parts[1:]:
        if tok.startswith("#"):
            target = tok
    if not (target and target.startswith("#")):
        raise ValueError("please specify a channel (e.g. #mychan)")

    exists = db.execute(
        "select 1 from github_watches where chan=? and repo=? limit 1",
        (target, repo),
    ).fetchone()
    if exists:
        return f"already watching {repo} in {target}"

    db.execute(
        "insert or ignore into github_watches(chan, repo) values(?,?)",
        (target, repo),
    )
    db.commit()

    running, err = _webhook_status(bot)
    if running:
        return f"ok, watching {repo} in {target} (webhook active)"
    if err == "flask_missing":
        return f"ok, watching {repo} in {target} (webhook not running: Flask missing)"
    if err == "missing_secret":
        return f"ok, watching {repo} in {target} (webhook not running: set github.webhook_secret in config)"
    if err == "disabled":
        return f"ok, watching {repo} in {target} (webhook not running: github.webhook_enabled=false)"
    return f"ok, watching {repo} in {target} (webhook not running)"


def _git_watch_remove(rest, chan="", db=None):
    _db_init(db)
    _db_init_legacy(db)

    parts = (rest or "").split()
    if len(parts) < 1:
        raise ValueError("usage: .git remove owner/repo [#channel]")

    repo = _normalize_repo(parts[0])
    target = parts[1] if len(parts) >= 2 and parts[1].startswith("#") else chan
    if not (target and target.startswith("#")):
        raise ValueError("please specify a channel (e.g. #mychan)")

    cur = db.execute("delete from github_watches where chan=? and repo=?", (target, repo))
    cur_leg = db.execute(
        "delete from github_watches_private where chan=? and repo=?", (target, repo)
    )
    db.commit()
    if (getattr(cur, "rowcount", 0) or 0) or (getattr(cur_leg, "rowcount", 0) or 0):
        return f"ok, stopped watching {repo} in {target}"
    return f"not watching {repo} in {target}"


# ---------------------------------------------------------------------------
# Bot command hook
# ---------------------------------------------------------------------------

@hook.command("git")
def git(inp, chan="", db=None, bot=None, nick="", input=None):
    """GitHub webhook watch management.

    Usage:
            .git add owner/repo [#channel]
            .git remove owner/repo [#channel]
            .git list [#channel]

    Requires github.webhook_secret to be set in config.json.
    Point your GitHub repo webhook at http://<host>:<port>/github/webhook.
    """

    parts = (inp or "").split(None, 1)
    if not parts:
        return None

    _ensure_webhook_server(bot)

    sub = parts[0].lower()
    rest = parts[1] if len(parts) > 1 else ""

    try:
        if sub in ("add", "watch"):
            return _git_watch_add(rest, chan=chan, db=db, bot=bot)

        if sub in ("remove", "rm", "del", "unwatch"):
            return _git_watch_remove(rest, chan=chan, db=db)

        if sub in ("list", "ls"):
            return _git_watch_list(rest, chan=chan, db=db)
    except ValueError as e:
        return str(e)

    return "unknown subcommand (try: add, remove, list)"


# ---------------------------------------------------------------------------
# Auto-start webhook server on IRC activity
# ---------------------------------------------------------------------------

@hook.event("*")
def github_webhook_autostart(inp, conn=None, bot=None):
    """Ensure the webhook listener is running whenever the bot is active."""
    if bot is None:
        return
    existing = getattr(bot, "_github_webhook_server", None)
    if isinstance(existing, dict) and existing.get("running"):
        return  # already up, fast path
    _ensure_webhook_server(bot)
