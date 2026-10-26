from __future__ import annotations

import logging
import os

from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import QLabel, QMainWindow, QMessageBox

from .. import __version__, startup
from .proxy_page import ProxyStatusPage
from .settings_dialog import SettingsDialog

log = logging.getLogger("deckoy.ui")

STATE_COLORS = {"Connected": "#2e9e4f", "Connecting": "#d69e00", "Disconnected": "#c0392b"}
REPO_URL = "https://github.com/brambo23/Deckoy"


class MainWindow(QMainWindow):
    def __init__(self, controller, parent=None):
        super().__init__(parent)
        self.ctl = controller
        self.quitting = False
        self.request_quit = None        # set by app.py: the single quit path (saves, hides, quits the QApplication)
        self.setWindowTitle("Deckoy")
        self.resize(820, 560)

        self.proxy_page = ProxyStatusPage()
        self.proxy_page.open_settings.connect(self.open_settings)
        self.proxy_page.reconnect.connect(self.ctl.backend.reconnect)
        self.proxy_page.drop_test.connect(self.ctl.backend.proxy_drop_app)
        self.proxy_page.open_folder.connect(self.open_config_folder)
        self.setCentralWidget(self.proxy_page)

        # status bar
        self.state_label = QLabel("Disconnected")
        self.info_label = QLabel("")
        self.hint_label = QLabel("")
        self.hint_label.setStyleSheet("color:#d69e00;")
        sb = self.statusBar()
        sb.addWidget(self.state_label)
        sb.addWidget(self.info_label, 1)
        sb.addPermanentWidget(self.hint_label)
        self.set_state("Disconnected")

        # menus
        settings_action = QAction("Settings...", self, triggered=self.open_settings)
        reconnect = QAction("Reconnect to dock", self, triggered=self.ctl.backend.reconnect)
        folder_action = QAction("Open config / log folder", self, triggered=self.open_config_folder)
        quit_action = QAction("Quit Deckoy", self, shortcut=QKeySequence("Ctrl+Q"),
                              triggered=lambda: self.quit_requested("File > Quit / Ctrl+Q"))
        about_action = QAction("About Deckoy", self, triggered=self.show_about)
        m = self.menuBar()
        fm = m.addMenu("&File")
        fm.addAction(settings_action)
        fm.addAction(reconnect)
        fm.addAction(folder_action)
        fm.addSeparator()
        fm.addAction(quit_action)
        hm = m.addMenu("&Help")
        hm.addAction(about_action)

        backend = self.ctl.backend
        backend.state_changed.connect(self.set_state)
        backend.info_changed.connect(self.info_label.setText)
        backend.hint.connect(self.hint_label.setText)
        backend.state_changed.connect(self.proxy_page.set_state)
        backend.info_changed.connect(self.proxy_page.set_info)
        backend.hint.connect(self.proxy_page.set_hint)
        backend.proxy_status.connect(self.set_proxy_status)
        backend.proxy_warning.connect(self.show_proxy_warning)

    # ---------------------------------------------------------------- status
    def set_state(self, state: str) -> None:
        color = STATE_COLORS.get(state, "#888")
        self.state_label.setText(f"<span style='color:{color}'>●</span> {state}")
        if state != "Connected":
            self.info_label.setText("")

    def set_proxy_status(self, snap: dict) -> None:
        self.proxy_page.set_snapshot(snap)

    def show_proxy_warning(self, msg: str) -> None:
        self.hint_label.setText(msg)
        self.proxy_page.set_hint(msg)
        QMessageBox.warning(self, "Deckoy", msg)

    def open_config_folder(self) -> None:
        folder = os.path.dirname(self.ctl.config_path or "") or "."
        try:
            os.startfile(folder)
        except (AttributeError, OSError) as ex:
            log.warning("could not open %s: %s", folder, ex)

    # ---------------------------------------------------------------- actions
    def show_about(self) -> None:
        QMessageBox.about(
            self, "About Deckoy",
            f"<b>Deckoy {__version__}</b><br>A decoy Network Dock that keeps the Elgato Stream Deck app connected."
            f"<br><br><a href='{REPO_URL}'>{REPO_URL}</a><br><br>"
            "Deckoy is not affiliated with or endorsed by Elgato or Corsair. Stream Deck is a trademark of its owner. "
            "Use at your own risk.")

    def open_settings(self) -> None:
        s = self.ctl.settings
        dlg = SettingsDialog(s, self)
        if dlg.exec():
            was = s.start_with_windows
            dlg.apply_to(s)
            if s.start_with_windows != was or startup.is_enabled() != s.start_with_windows:
                try:
                    startup.set_enabled(s.start_with_windows)
                except Exception as ex:
                    s.start_with_windows = startup.is_enabled()
                    QMessageBox.warning(self, "Start with Windows", f"Could not change the startup shortcut:\n{ex}")
            self.ctl.settings_changed()

    def quit_requested(self, source: str = "menu") -> None:
        self.quitting = True
        if self.request_quit is not None:
            self.request_quit(source)
        else:                      # no application wired in (unit tests): just close
            self.close()

    def closeEvent(self, e):
        tray_ok = getattr(self, "tray_available", False)
        if not self.quitting and tray_ok:      # the X button hides to the tray
            e.ignore()
            self.hide()
            return
        self.ctl.save_now()
        e.accept()
        if not self.quitting and self.request_quit is not None:
            # No tray to live in: closing the window is quitting (previously the process was left running)
            self.request_quit("window close (no tray)")
