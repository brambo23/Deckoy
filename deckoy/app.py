"""Deckoy entry point: single instance, logging, Qt app + proxy backend thread."""
from __future__ import annotations

import logging
import logging.handlers
import os
import sys

from . import model as m
from . import shutdown


def setup_logging(app_dir: str) -> None:
    logdir = os.path.join(app_dir, "logs")
    os.makedirs(logdir, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(os.path.join(logdir, "deckoy.log"), maxBytes=512 * 1024,
                                                   backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    if sys.stderr is not None:
        root.addHandler(logging.StreamHandler())


EXIT_WATCHDOG_SECONDS = 8.0


def make_request_quit(app, window, tray, ctl, log, watchdog_seconds=None):
    """Build the single quit path used by the tray Quit, File > Quit / Ctrl+Q and (without a tray) the window close button.

    The tray keeps app.exec() alive on purpose (quit-on-last-window-closed is off), so nothing ends the process unless
    QApplication.quit() is called: this is that call, plus saving, hiding the window and the tray icon (no ghost icon) and
    starting the hard-exit watchdog. Idempotent.
    """
    state = {"requested": False, "watchdog": None}

    def request_quit(source: str = "app") -> None:
        if state["requested"]:
            return
        state["requested"] = True
        log.info("quit requested (%s)", source)
        window.quitting = True
        ctl.save_now()
        window.hide()
        tray.icon.hide()
        state["watchdog"] = shutdown.start_exit_watchdog(
            EXIT_WATCHDOG_SECONDS if watchdog_seconds is None else watchdog_seconds)
        app.quit()

    request_quit.state = state
    return request_quit


def main(argv=None) -> int:
    argv = list(sys.argv if argv is None else argv)
    minimized = "--minimized" in argv
    app_dir = m.app_data_dir()
    os.makedirs(app_dir, exist_ok=True)
    setup_logging(app_dir)
    log = logging.getLogger("deckoy")

    from PySide6.QtCore import QLockFile, QTimer
    from PySide6.QtWidgets import QApplication, QMessageBox

    from .backend import Backend
    from .ui.controller import Controller
    from .ui.main_window import MainWindow
    from .ui.tray import Tray, app_icon

    app = QApplication(argv)
    app.setApplicationName("Deckoy")
    app.setQuitOnLastWindowClosed(False)
    app.setWindowIcon(app_icon())

    lock = QLockFile(os.path.join(app_dir, "deckoy.lock"))
    lock.setStaleLockTime(0)
    from PySide6.QtNetwork import QLocalServer, QLocalSocket
    pipe = "Deckoy-" + (os.environ.get("USERNAME") or "user")
    if not lock.tryLock(100):
        # Already running: ask that instance to show its window, then quit quietly.
        sock = QLocalSocket()
        sock.connectToServer(pipe)
        if sock.waitForConnected(1000):
            sock.write(b"show")
            sock.waitForBytesWritten(1000)
            sock.disconnectFromServer()
        else:
            QMessageBox.information(None, "Deckoy", "Deckoy is already running (look in the system tray).")
        return 0

    config_path = os.path.join(app_dir, "settings.json")
    settings = m.load_settings(config_path)

    backend = Backend(settings, app_dir)
    ctl = Controller(settings, backend, config_path)
    window = MainWindow(ctl)
    tray = Tray(window, ctl, app)
    QLocalServer.removeServer(pipe)
    server = QLocalServer(app)
    server.newConnection.connect(lambda: (server.nextPendingConnection(), tray.show_window()))
    server.listen(pipe)
    backend.start()
    log.info("Deckoy started (settings %s)", config_path)

    # ---- quit: the tray keeps app.exec() alive on purpose (quit-on-last-window-closed is off), so quitting must be explicit.
    request_quit = make_request_quit(app, window, tray, ctl, log)
    window.request_quit = request_quit
    tray.request_quit = request_quit

    def teardown() -> None:
        ctl.save_now()
        backend.stop()                                     # bounded: ~2.5 s at most, each step logged
        lock.unlock()

    app.aboutToQuit.connect(teardown)
    test_ms = os.environ.get("DECKOY_TEST_QUIT_AFTER_MS")   # test-only hook: quit by itself (process-level tests)
    if test_ms and test_ms.isdigit():
        QTimer.singleShot(int(test_ms), lambda: request_quit("test"))

    if not (minimized and window.tray_available):
        window.show()
    code = app.exec()
    log.info("Deckoy exiting (code %s)", code)
    # Last resort: a non-daemon thread still alive would keep the process running after main() returns. The config is saved
    # and the lock is released by now, so ending the process here loses nothing.
    if shutdown.alive_non_daemon_threads():
        shutdown.hard_exit(code, "non-daemon threads alive after exec()")
    return code


if __name__ == "__main__":
    sys.exit(main())
