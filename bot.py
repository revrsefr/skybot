#!/usr/bin/env python3

from __future__ import annotations

import logging
import os
import queue
import sys
import traceback
import time
from pathlib import Path
from typing import Any, NoReturn


class Bot:
    def __init__(self):
        self.conns = {}
        self.persist_dir = str(BASE_DIR / "persist")
        Path(self.persist_dir).mkdir(parents=True, exist_ok=True)


BASE_DIR = Path(__file__).resolve().parent

bot = Bot()


# These are dynamically defined/overwritten by `core/reload.py` (and other core
# modules it loads). Stubs keep type-checkers happy and fail fast if called
# before bootstrapping.
def reload(*_args: Any, **_kwargs: Any) -> None:  # type: ignore[override]
    raise RuntimeError("reload() not bootstrapped yet")


def config(*_args: Any, **_kwargs: Any) -> None:  # type: ignore[override]
    raise RuntimeError("config() not bootstrapped yet")


def _configure_logging() -> None:
    # Default to INFO; override with env if desired.
    level_name = os.environ.get("SKYBOT_LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def _bootstrap_reloader() -> None:
    # Keep the existing dynamic exec-based loader semantics, but do it safely.
    reload_path = BASE_DIR / "core" / "reload.py"
    code = reload_path.read_text(encoding="utf-8")
    exec(compile(code, str(reload_path), "exec"), globals())


def run() -> NoReturn:
    _configure_logging()
    log = logging.getLogger("skybot")

    # Do stuff relative to the install directory.
    os.chdir(BASE_DIR)

    # Ensure core can `import hook` and friends.
    for rel in ("plugins", "lib"):
        path = str(BASE_DIR / rel)
        if path not in sys.path:
            sys.path.insert(0, path)

    log.info("Loading plugins")

    # bootstrap the reloader
    _bootstrap_reloader()
    reload(init=True)

    log.info("Connecting to IRC")

    try:
        config()
        if not hasattr(bot, "config"):
            raise RuntimeError("config() did not set bot.config")
    except Exception as exc:
        log.error("Malformed config file: %s", exc)
        traceback.print_exc()
        raise SystemExit(1)

    # Core defines a message-dispatch handler named `main` (loaded via reload()).
    dispatch = globals().get("main")
    if not callable(dispatch):
        log.error("Core did not define a callable main(conn, out)")
        raise SystemExit(1)

    log.info("Running main loop")

    while True:
        reload()  # these functions only do things
        config()  # if changes have occurred

        for conn in bot.conns.values():
            try:
                out = conn.out.get_nowait()
                dispatch(conn, out)
            except queue.Empty:
                pass

        while bot.conns and all(conn.out.empty() for conn in bot.conns.values()):
            time.sleep(0.1)


if __name__ == "__main__":
    run()
