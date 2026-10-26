"""Process-level quit tests: run the real app (python -m deckoy, or the frozen exe) against the mock dock and make sure the
process EXITS (exit code 0, quickly, lock released, shutdown logged). Uses the test-only hook DECKOY_TEST_QUIT_AFTER_MS,
which makes the app call its own request_quit("test") after that many milliseconds."""
import asyncio
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tools.mock_dock import MockDockService

from .test_backend import MockThread

ROOT = Path(__file__).resolve().parent.parent
EXE = ROOT / "dist" / "Deckoy" / "Deckoy.exe"
# The frozen-exe tests exercise the BUILT exe, so they only make sense right after a build: build.ps1 sets this and runs them
# after PyInstaller (running them before the build would test the previous build and could block building the fix).
FROZEN = os.environ.get("DECKOY_TEST_FROZEN") == "1" and EXE.exists()


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def launch(home: Path, quit_ms: int, exe: Path = None) -> subprocess.Popen:
    env = dict(os.environ, DECKOY_HOME=str(home), QT_QPA_PLATFORM="offscreen", DECKOY_TEST_QUIT_AFTER_MS=str(quit_ms))
    cmd = [str(exe)] if exe else [sys.executable, "-m", "deckoy"]
    return subprocess.Popen(cmd, cwd=str(ROOT), env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def write_config(home: Path, settings: dict) -> None:
    home.mkdir(parents=True, exist_ok=True)
    (home / "settings.json").write_text(json.dumps({"version": 1, **settings}))


def read_log(home: Path) -> str:
    p = home / "logs" / "deckoy.log"
    return p.read_text(encoding="utf-8", errors="replace") if p.exists() else ""


def lock_is_free(home: Path) -> bool:
    from PySide6.QtCore import QLockFile
    lk = QLockFile(str(home / "deckoy.lock"))
    ok = lk.tryLock(500)
    if ok:
        lk.unlock()
    return ok


@pytest.fixture
def mock():
    mt = MockThread()
    svc = asyncio.run_coroutine_threadsafe(MockDockService(keepalive_interval=0.3).start(), mt.loop).result(5)
    mt.svc = svc
    yield mt
    asyncio.run_coroutine_threadsafe(svc.stop(), mt.loop).result(5)
    mt.close()


def wait_until(pred, timeout):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.05)
    return False


def proxy_settings(mock):
    return {"dock_host": "127.0.0.1", "dock_port": mock.mock.port, "upstream_dock_port": mock.svc.port,
            "proxy_bind": "127.0.0.1", "proxy_deck_port": free_port(), "proxy_dock_port": free_port()}


def assert_clean_exit(p, home, started, quit_ms, margin=6.0):
    try:
        rc = p.wait(timeout=quit_ms / 1000 + margin + 8)
    finally:
        if p.poll() is None:
            p.kill()
    took = time.monotonic() - started
    log = read_log(home)
    assert rc == 0, f"exit code {rc}\n{log[-1500:]}"
    assert took < quit_ms / 1000 + margin + 3, f"took {took:.1f}s\n{log[-1500:]}"     # startup variance allowed for
    assert "quit requested (test)" in log and "Deckoy exiting (code 0)" in log, log[-1500:]
    assert "shutdown: backend stopped in" in log
    assert "forced exit" not in log, log[-1500:]          # a normal quit must not need the hard exit
    assert lock_is_free(home), "single-instance lock was not released"
    return log


def test_proxy_process_quits_and_releases_the_lock(tmp_path, mock):
    home = tmp_path / "home"
    write_config(home, proxy_settings(mock))
    t0 = time.monotonic()
    p = launch(home, 3500)
    assert wait_until(lambda: "dock 5343 service session up" in read_log(home), 15), read_log(home)[-1500:]
    log = assert_clean_exit(p, home, t0, 3500)
    assert "shutdown: proxy ok in" in log and "shutdown: deck server ok in" in log


@pytest.mark.skipif(not FROZEN, reason="frozen-exe test: run by build.ps1 after the build (DECKOY_TEST_FROZEN=1)")
def test_frozen_exe_quits_and_releases_the_lock(tmp_path, mock):
    home = tmp_path / "home"
    write_config(home, proxy_settings(mock))
    t0 = time.monotonic()
    p = launch(home, 4000, exe=EXE)
    assert wait_until(lambda: "dock 5343 service session up" in read_log(home), 20), read_log(home)[-1500:]
    assert_clean_exit(p, home, t0, 4000)
