"""Shutdown helpers: bounded teardown steps with logging, and the last-resort hard exit.

Deckoy must always end the process quickly after Quit: the tray keeps `app.exec()` alive on purpose, so quitting is explicit
(app.request_quit), every teardown step is time-boxed and logged, and a watchdog / end-of-main check guarantees the process exit
even if a worker thread is stuck (for example in a waited command).
"""
from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from typing import Awaitable, Callable, List

log = logging.getLogger("deckoy.shutdown")


async def bounded(name: str, aw: Awaitable, timeout: float) -> bool:
    """Await a teardown step for at most `timeout` seconds and log how it went. Never raises (except cancellation)."""
    t0 = time.monotonic()
    try:
        await asyncio.wait_for(aw, timeout)
    except asyncio.TimeoutError:
        log.warning("shutdown: %s TIMED OUT after %d ms", name, (time.monotonic() - t0) * 1000)
        return False
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("shutdown: %s failed", name)
        return False
    log.info("shutdown: %s ok in %d ms", name, (time.monotonic() - t0) * 1000)
    return True


def alive_non_daemon_threads() -> List[threading.Thread]:
    return [t for t in threading.enumerate() if t is not threading.main_thread() and not t.daemon and t.is_alive()]


def describe_threads() -> str:
    names = [t.name for t in alive_non_daemon_threads()]
    return ", ".join(names) if names else "none"


def hard_exit(code: int = 0, reason: str = "") -> None:
    """os._exit after flushing the log. Only used as a last resort, after the config was saved and the lock released."""
    log.warning("forced exit (%s): non-daemon threads still alive: %s", reason, describe_threads())
    try:
        logging.shutdown()
    finally:
        os._exit(code)


def start_exit_watchdog(seconds: float = 8.0, exit_fn: Callable[[int, str], None] = hard_exit) -> threading.Timer:
    """If the process is still alive `seconds` after Quit was requested, force it to exit."""
    t = threading.Timer(seconds, lambda: exit_fn(0, f"watchdog after {seconds:g} s"))
    t.daemon = True
    t.name = "exit-watchdog"
    t.start()
    return t
