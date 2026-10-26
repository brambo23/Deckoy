from __future__ import annotations

import os
import time

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QAction, QColor, QIcon, QPainter, QPixmap, QPolygonF
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon


def app_icon() -> QIcon:
    """Simple generated icon: a decoy duck on a dark tile (tools/make_icon.py draws the same shapes with Pillow)."""
    pm = QPixmap(64, 64)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setPen(Qt.NoPen)
    p.setBrush(QColor("#1c1f26"))
    p.drawRoundedRect(2, 2, 60, 60, 12, 12)
    p.setBrush(QColor("#f2c230"))
    p.drawEllipse(14, 30, 36, 24)          # body
    p.drawEllipse(22, 12, 22, 22)          # head
    p.setBrush(QColor("#e8782a"))
    p.drawPolygon(QPolygonF([QPointF(42, 20), QPointF(54, 24), QPointF(42, 28)]))    # beak
    p.setBrush(QColor("#1c1f26"))
    p.drawEllipse(34, 19, 4, 4)            # eye
    p.end()
    return QIcon(pm)


class Tray:
    def __init__(self, window, controller, app: QApplication):
        self.window, self.ctl, self.app = window, controller, app
        self.icon = QSystemTrayIcon(app_icon(), app)
        self.icon.setToolTip("Deckoy")
        menu = QMenu()
        self.show_action = QAction("Show / hide Deckoy", menu, triggered=self.toggle)
        self.reconnect_action = QAction("Reconnect", menu, triggered=controller.backend.reconnect)
        self.folder_action = QAction("Open config folder", menu, triggered=self.open_folder)
        self.request_quit = None      # set by app.py (the same single quit path the window uses)
        self.quit_action = QAction("Quit", menu, triggered=lambda: self._quit())
        self.drop_action = QAction("Test: drop the Elgato app's session now", menu,
                                   triggered=controller.backend.proxy_drop_app)
        self.status_actions = [QAction("", menu) for _ in range(4)]
        for a in self.status_actions:
            a.setEnabled(False)
            menu.addAction(a)
        self.status_sep = menu.addSeparator()
        for a in (self.show_action, self.reconnect_action, self.drop_action, self.folder_action):
            menu.addAction(a)
        menu.addSeparator()
        menu.addAction(self.quit_action)
        self._menu = menu
        self.icon.setContextMenu(menu)
        self.icon.activated.connect(self._activated)
        controller.backend.state_changed.connect(lambda s: self.icon.setToolTip(f"Deckoy: {s}"))
        controller.backend.proxy_status.connect(self.set_proxy_status)
        controller.backend.proxy_warning.connect(
            lambda m: self.icon.showMessage("Deckoy", m, QSystemTrayIcon.Warning, 15000))
        window.tray_available = QSystemTrayIcon.isSystemTrayAvailable()
        if window.tray_available:
            self.icon.show()

    def _quit(self) -> None:
        if self.request_quit is not None:
            self.request_quit("tray")
        else:
            self.window.quit_requested("tray")

    def set_proxy_status(self, snap: dict) -> None:
        last = time.strftime("%H:%M:%S", time.localtime(snap["last_drop"])) if snap.get("last_drop") else "never"
        lines = [f"Dock link: {'connected' if snap['upstream_connected'] else 'DOWN'}",
                 f"Elgato app: {'connected' if snap['app_connected'] else 'not connected'}",
                 f"Drops hidden today: {snap['hidden_today']}", f"Last dock drop: {last}"]
        for a, t in zip(self.status_actions, lines):
            a.setText(t)
        self.icon.setToolTip("Deckoy: " + "; ".join(lines[:3]))

    def toggle(self) -> None:
        if self.window.isVisible():
            self.window.hide()
        else:
            self.show_window()

    def show_window(self) -> None:
        self.window.show()
        self.window.raise_()
        self.window.activateWindow()

    def _activated(self, reason) -> None:
        if reason in (QSystemTrayIcon.Trigger, QSystemTrayIcon.DoubleClick):
            self.toggle()

    def open_folder(self) -> None:
        folder = os.path.dirname(self.ctl.config_path or "") or "."
        os.startfile(folder)
