import json
import os

from deckoy import model as m


def test_defaults_roundtrip(tmp_path):
    p = str(tmp_path / "settings.json")
    s = m.Settings()
    s.dock_host, s.brightness, s.proxy_naming = "192.168.1.50", 40, "same"
    m.save_settings(s, p)
    data = json.load(open(p))
    assert data["version"] == 1 and data["dock_host"] == "192.168.1.50"
    assert m.load_settings(p) == s
    assert not os.path.exists(p + ".tmp")


def test_missing_file_gives_defaults(tmp_path):
    assert m.load_settings(str(tmp_path / "nope.json")) == m.Settings()


def test_corrupt_file_is_kept_as_bak_and_defaults_load(tmp_path):
    p = tmp_path / "settings.json"
    p.write_text("{ not json")
    assert m.load_settings(str(p)) == m.Settings()
    baks = list(tmp_path.glob("settings.json.*.bak"))
    assert len(baks) == 1 and baks[0].read_text() == "{ not json"
    p.write_text("[1, 2]")                                  # valid JSON but not an object
    assert m.load_settings(str(p)) == m.Settings()


def test_clamping_and_unknown_keys_and_wrong_types():
    s = m.Settings.from_dict({"brightness": 500, "sleep_seconds": -5, "proxy_reannounce_delay_ms": 10 ** 9,
                              "proxy_deck_port": 70000, "proxy_dock_port": 0, "proxy_naming": "x", "proxy_reannounce": "y",
                              "dock_host": 5, "start_with_windows": 1, "mode": "standalone", "flip": False,
                              "something_else": [1]})
    assert s.brightness == 100 and s.sleep_seconds == 0 and s.proxy_reannounce_delay_ms == 30000
    assert s.proxy_deck_port == 20002 and s.proxy_dock_port == 5343
    assert s.proxy_naming == "distinct" and s.proxy_reannounce == "deck"
    assert s.dock_host == "" and s.start_with_windows is False          # wrong types keep the default
    assert not hasattr(s, "mode") and "flip" not in s.to_dict()


def test_saved_file_has_no_deckpad_only_fields(tmp_path):
    p = str(tmp_path / "settings.json")
    m.save_settings(m.Settings(), p)
    keys = set(json.load(open(p)))
    assert not keys & {"mode", "jpeg_quality", "flip", "hold_ms", "profiles"}


def test_app_data_dir_override(monkeypatch, tmp_path):
    monkeypatch.setenv("DECKOY_HOME", str(tmp_path))
    assert m.app_data_dir() == str(tmp_path)
    monkeypatch.delenv("DECKOY_HOME")
    monkeypatch.setenv("APPDATA", str(tmp_path))
    assert m.app_data_dir() == os.path.join(str(tmp_path), "Deckoy")
