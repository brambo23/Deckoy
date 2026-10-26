import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtGui import QAction
from PySide6.QtWidgets import QApplication

from deckoy import model as m
from deckoy.backend import NullBackend
from deckoy.ui.controller import Controller
from deckoy.ui.main_window import MainWindow
from deckoy.ui.settings_dialog import SettingsDialog
from deckoy.ui.tray import Tray, app_icon


@pytest.fixture
def ctl(qtbot, tmp_path):
    return Controller(m.Settings(), NullBackend(), str(tmp_path / "settings.json"), save_delay_ms=20)


def test_settings_dialog_applies(qtbot):
    s = m.Settings()
    dlg = SettingsDialog(s)
    qtbot.addWidget(dlg)
    assert dlg.proxy_box.isEnabled()                        # the proxy options are always available
    dlg.host.setText(" 192.168.1.50 ")
    dlg.port.setValue(20003)
    dlg.brightness.setValue(80)
    dlg.sleep.setValue(120)
    dlg.startup.setChecked(True)
    dlg.apply_to(s)
    assert (s.dock_host, s.dock_port, s.brightness, s.sleep_seconds, s.start_with_windows) == \
        ("192.168.1.50", 20003, 80, 120, True)
    assert not hasattr(dlg, "mode") and not hasattr(dlg, "quality")


def test_settings_dialog_proxy_options(qtbot):
    s = m.Settings()
    dlg = SettingsDialog(s)
    qtbot.addWidget(dlg)
    dlg.deck_port.setValue(20005)
    dlg.forward_unknown.setChecked(True)
    dlg.naming.setCurrentIndex(1)
    dlg.apply_to(s)
    assert (s.proxy_deck_port, s.proxy_forward_unknown, s.proxy_naming) == (20005, True, "same")
    assert m.Settings.from_dict({"proxy_deck_port": 0}).proxy_deck_port == 20002


def test_settings_dialog_reannounce_options(qtbot):
    s = m.Settings()
    dlg = SettingsDialog(s)
    qtbot.addWidget(dlg)
    assert dlg.reannounce.currentData() == "deck" and dlg.new_port.isChecked()
    dlg.reannounce.setCurrentIndex(2)
    dlg.new_port.setChecked(False)
    dlg.reannounce_delay.setValue(2500)
    dlg.apply_to(s)
    assert (s.proxy_reannounce, s.proxy_reannounce_new_port, s.proxy_reannounce_delay_ms) == ("off", False, 2500)
    assert m.Settings.from_dict({"proxy_reannounce": "weird"}).proxy_reannounce == "deck"


def test_main_window_status_and_close_to_tray(qtbot, ctl):
    w = MainWindow(ctl)
    qtbot.addWidget(w)
    w.show()
    assert w.windowTitle() == "Deckoy" and w.centralWidget() is w.proxy_page
    ctl.backend.state_changed.emit("Connected")
    ctl.backend.info_changed.emit("TESTDECK0001  fw 1.01.000  192.168.1.50:20003")
    assert "Connected" in w.state_label.text() and "TESTDECK0001" in w.info_label.text()
    w.tray_available = True
    w.close()
    assert not w.isVisible()                  # hidden to the tray, not quit
    w.quitting = True
    w.show()
    w.close()
    assert not w.isVisible()


def test_menus(qtbot, ctl):
    w = MainWindow(ctl)
    qtbot.addWidget(w)
    top = w.menuBar().actions()
    assert [a.text() for a in top] == ["&File", "&Help"]
    assert [a.text() for a in top[0].menu().actions() if a.text()] == [
        "Settings...", "Reconnect to dock", "Open config / log folder", "Quit Deckoy"]
    assert [a.text() for a in top[1].menu().actions()] == ["About Deckoy"]
    next(a for a in w.findChildren(QAction) if a.text() == "Reconnect to dock").trigger()
    assert ("reconnect",) in ctl.backend.calls


def test_status_page(qtbot, ctl):
    w = MainWindow(ctl)
    qtbot.addWidget(w)
    w.show()
    ctl.backend.state_changed.emit("Connected")
    ctl.backend.info_changed.emit("TESTDECK0001  fw 1.01.000  192.168.1.50:20003")
    now = 1_800_000_000.0
    ctl.backend.proxy_status.emit({
        "upstream_connected": True, "app_connected": True, "hidden_today": 3, "drops_today": 4, "last_drop": now,
        "downtime_s": 12.4, "app_disconnects": 1, "app_drop_count": 2, "last_app_drop": now - 60, "reannounces": 2,
        "answered_local": 1234, "forwarded": 0, "blocked": 1,
        "app_drops_recent": [{"t": now - 60, "duration": 125.4, "by_us": False}], "dock_drops_recent": [now]})
    page = w.proxy_page
    assert "connected" in page.dock_state.text() and "TESTDECK0001" in page.dock_info.text()
    assert "connected" in page.app_state.text()
    txt = page.counters.text()
    assert "<b>3</b> of 4" in txt and "<b>1234</b>" in txt and "<b>2</b>" in txt and "Deckoy" in txt
    assert page.events.count() == 2 and "Elgato app session ended after 125.4 s" in page.events.item(1).text() + page.events.item(0).text()
    ctl.backend.proxy_status.emit({"upstream_connected": False, "app_connected": False, "hidden_today": 0, "drops_today": 0,
                                   "last_drop": None, "downtime_s": 0, "app_disconnects": 0, "answered_local": 0,
                                   "forwarded": 0, "blocked": 0})
    assert "DOWN" in page.dock_state.text() and "not connected" in page.app_state.text()
    assert page.events.item(0).text() == "No drops so far."
    ctl.backend.hint.emit("The dock is not answering")
    assert page.hint.text() == "The dock is not answering"
    page.drop_btn.click()
    page.reconnect_btn.click()
    assert ("proxy_drop_app",) in ctl.backend.calls and ("reconnect",) in ctl.backend.calls


def test_settings_changed_applies_and_saves(qtbot, ctl, tmp_path):
    ctl.settings.brightness = 21
    ctl.settings_changed()
    assert ("apply_settings",) in ctl.backend.calls
    qtbot.waitUntil(lambda: os.path.exists(tmp_path / "settings.json"), timeout=2000)
    assert m.load_settings(str(tmp_path / "settings.json")).brightness == 21


def test_tray_proxy_lines_and_drop_action(qtbot, ctl):
    w = MainWindow(ctl)
    qtbot.addWidget(w)
    tray = Tray(w, ctl, QApplication.instance())
    assert tray.icon.toolTip() == "Deckoy"
    assert not hasattr(tray, "pause_action")
    ctl.backend.proxy_status.emit({"upstream_connected": False, "app_connected": True, "hidden_today": 2,
                                   "last_drop": None})
    assert tray.status_actions[0].text() == "Dock link: DOWN"
    assert tray.status_actions[2].text() == "Drops hidden today: 2"
    assert tray.drop_action.isVisible()
    tray.drop_action.trigger()
    assert ("proxy_drop_app",) in ctl.backend.calls


def test_app_icon_is_drawn(qtbot):
    pm = app_icon().pixmap(64, 64)
    assert not pm.isNull() and pm.toImage().pixelColor(30, 40).alpha() == 255
