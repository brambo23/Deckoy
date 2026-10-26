"""The Deckoy status page: dock link and Elgato app state, counters, recent events."""
from __future__ import annotations

import time
from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QFrame, QGridLayout, QGroupBox, QHBoxLayout, QLabel, QListWidget, QPushButton,
                               QVBoxLayout, QWidget)

GOOD, WARN, BAD, DIM = "#2e9e4f", "#d69e00", "#c0392b", "#888888"

DEFAULTS = {"upstream_connected": False, "app_connected": False, "hidden_today": 0, "drops_today": 0, "last_drop": None,
            "downtime_s": 0.0, "app_disconnects": 0, "app_drop_count": 0, "last_app_drop": None, "reannounces": 0,
            "answered_local": 0, "forwarded": 0, "blocked": 0, "app_drops_recent": [], "dock_drops_recent": []}


def _t(epoch: Optional[float], fmt: str = "%H:%M:%S") -> str:
    return time.strftime(fmt, time.localtime(epoch)) if epoch else "never"


def _dot(color: str) -> str:
    return f"<span style='color:{color}; font-size:18px'>●</span>"


class ProxyStatusPage(QWidget):
    open_settings = Signal()
    reconnect = Signal()
    drop_test = Signal()
    open_folder = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._state = "Disconnected"
        self._info = ""
        self._snap: Optional[dict] = None
        self._hint = ""

        title = QLabel("Deckoy")
        title.setStyleSheet("font-size:22px; font-weight:600;")
        sub = QLabel("Deckoy is the only program talking to your Network Dock and relays the Elgato Stream Deck app. "
                     "It keeps the dock connection healthy and hides dock drops.")
        sub.setWordWrap(True)
        sub.setStyleSheet("color:#9aa0a6;")

        # ---- connections
        self.dock_state = QLabel()
        self.dock_info = QLabel()
        self.app_state = QLabel()
        self.advertise = QLabel()
        for w in (self.dock_state, self.dock_info, self.app_state, self.advertise):
            w.setTextFormat(Qt.RichText)
            w.setWordWrap(True)
        conn = QGroupBox("Connections")
        cg = QGridLayout(conn)
        cg.addWidget(QLabel("Real dock"), 0, 0)
        cg.addWidget(self.dock_state, 0, 1)
        cg.addWidget(self.dock_info, 1, 1)
        cg.addWidget(QLabel("Elgato app"), 2, 0)
        cg.addWidget(self.app_state, 2, 1)
        cg.addWidget(self.advertise, 3, 1)
        cg.setColumnStretch(1, 1)

        # ---- counters
        self.counters = QLabel()
        self.counters.setTextFormat(Qt.RichText)
        cnt = QGroupBox("What Deckoy has handled")
        QVBoxLayout(cnt).addWidget(self.counters)

        # ---- recent events
        self.events = QListWidget()
        self.events.setMaximumHeight(150)
        ev = QGroupBox("Recent events")
        QVBoxLayout(ev).addWidget(self.events)

        self.hint = QLabel()
        self.hint.setWordWrap(True)
        self.hint.setStyleSheet("color:#d69e00;")

        # ---- actions
        self.settings_btn = QPushButton("Settings...")
        self.reconnect_btn = QPushButton("Reconnect to dock")
        self.drop_btn = QPushButton("Test: drop the Elgato app's session")
        self.folder_btn = QPushButton("Open config / log folder")
        self.settings_btn.clicked.connect(self.open_settings)
        self.reconnect_btn.clicked.connect(self.reconnect)
        self.drop_btn.clicked.connect(self.drop_test)
        self.folder_btn.clicked.connect(self.open_folder)
        row = QHBoxLayout()
        for b in (self.settings_btn, self.reconnect_btn, self.drop_btn, self.folder_btn):
            row.addWidget(b)
        row.addStretch(1)

        foot = QLabel("Use the Elgato Stream Deck app for your buttons. Deckoy only relays it.")
        foot.setWordWrap(True)
        foot.setStyleSheet("color:#9aa0a6;")

        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 18, 24, 12)
        lay.setSpacing(12)
        lay.addWidget(title)
        lay.addWidget(sub)
        top = QHBoxLayout()
        top.addWidget(conn, 1)
        top.addWidget(cnt, 1)
        lay.addLayout(top)
        lay.addWidget(ev)
        lay.addWidget(self.hint)
        lay.addLayout(row)
        lay.addStretch(1)
        lay.addWidget(foot)
        self._render()

    # ---------------------------------------------------------------- inputs
    def set_state(self, state: str) -> None:
        self._state = state
        if state != "Connected":
            self._info = ""
        self._render()

    def set_info(self, text: str) -> None:
        self._info = text
        self._render()

    def set_snapshot(self, snap: dict) -> None:
        self._snap = snap
        self._render()

    def set_hint(self, text: str) -> None:
        self._hint = text
        self.hint.setText(text)

    # ---------------------------------------------------------------- rendering
    def _render(self) -> None:
        s = {**DEFAULTS, **self._snap} if self._snap is not None else None
        up = s["upstream_connected"] if s else self._state == "Connected"
        color = GOOD if up else (WARN if self._state == "Connecting" else BAD)
        word = "connected" if up else ("connecting..." if self._state == "Connecting" else "DOWN (reconnecting)")
        self.dock_state.setText(f"{_dot(color)} <b>{word}</b>")
        self.dock_info.setText(self._info or "<span style='color:#888'>no dock information yet</span>")
        if s is None:
            self.app_state.setText(f"{_dot(DIM)} waiting...")
            self.advertise.setText("")
            self.counters.setText("Starting...")
            return
        app = s["app_connected"]
        self.app_state.setText(f"{_dot(GOOD if app else WARN)} <b>{'connected' if app else 'not connected'}</b>"
                               + ("" if app else " &nbsp; <span style='color:#888'>open Add New Device in the Elgato app "
                                                "and pick the (Deckoy) dock</span>"))
        self.advertise.setText("<span style='color:#888'>Presented to the Elgato app as a Network Dock via mDNS</span>")
        self.counters.setText(
            "<table cellspacing='6'>"
            f"<tr><td>Dock-link drops hidden today</td><td><b>{s['hidden_today']}</b> of {s['drops_today']}</td></tr>"
            f"<tr><td>Last dock drop</td><td><b>{_t(s['last_drop'])}</b></td></tr>"
            f"<tr><td>Dock down in total</td><td><b>{s['downtime_s']:.0f} s</b></td></tr>"
            f"<tr><td>Elgato app sessions ended</td><td><b>{s.get('app_drop_count', 0)}</b> "
            f"(last {_t(s.get('last_app_drop'))})</td></tr>"
            f"<tr><td>mDNS re-announces</td><td><b>{s.get('reannounces', 0)}</b></td></tr>"
            f"<tr><td>Requests answered by Deckoy</td><td><b>{s['answered_local']}</b></td></tr>"
            f"<tr><td>Unknown commands forwarded / blocked</td><td><b>{s['forwarded']}</b> / <b>{s['blocked']}</b></td></tr>"
            "</table>")
        self.events.clear()
        def app_text(d: dict) -> str:
            text = f"Elgato app session ended after {d['duration']} s"
            if d.get("by_us"):
                text += " (test drop made by Deckoy)"
            return text + "; Deckoy re-announced the deck service" if s.get("reannounces") else text

        items = ([(t, "Dock link dropped; Deckoy reconnected to it") for t in s.get("dock_drops_recent", [])]
                 + [(d["t"], app_text(d)) for d in s.get("app_drops_recent", [])])
        for t, text in sorted(items, reverse=True)[:12]:
            self.events.addItem(f"{_t(t, '%Y-%m-%d %H:%M:%S')}   {text}")
        if not items:
            self.events.addItem("No drops so far.")
