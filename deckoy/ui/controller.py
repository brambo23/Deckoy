"""Qt-side owner of the settings: debounced autosave and pushing changes to the backend."""
from __future__ import annotations

import logging
from typing import Optional

from PySide6.QtCore import QObject, QTimer, Signal

from .. import model as m
from ..model import Settings

log = logging.getLogger("deckoy.ui")


class Controller(QObject):
    _save_requested = Signal()

    def __init__(self, settings: Settings, backend, config_path: Optional[str] = None, save_delay_ms: int = 400,
                 parent=None):
        super().__init__(parent)
        self.settings = settings
        self.backend = backend
        self.config_path = config_path
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(save_delay_ms)
        self._save_timer.timeout.connect(self.save_now)
        self._save_requested.connect(self.schedule_save)  # queued: lets the dock thread request a save

    def schedule_save_threadsafe(self) -> None:
        self._save_requested.emit()

    def schedule_save(self) -> None:
        if self.config_path:
            self._save_timer.start()

    def save_now(self) -> None:
        if not self.config_path:
            return
        try:
            m.save_settings(self.settings, self.config_path)
        except OSError as ex:
            log.error("saving settings failed: %s", ex)

    def settings_changed(self) -> None:
        self.backend.apply_settings()
        self.schedule_save()
