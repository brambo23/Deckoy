"""What the proxy remembers about the real dock between runs (handshake replies, mDNS records)."""
from __future__ import annotations

import json
import logging
import os
from typing import Dict, Optional

log = logging.getLogger("deckoy.proxy")


class ProxyState:
    """Persisted: raw replies (so the Elgato app can be answered before the dock is reachable) and the dock's mDNS TXT."""

    def __init__(self, path: Optional[str] = None):
        self.path = path
        self.deck_replies: Dict[int, bytes] = {}      # feature id (5, 6, 0x0b) -> 32-byte payload
        self.dock_replies: Dict[str, bytes] = {}      # 5343 request id hex ("0383") -> 512-byte payload
        self.dock_txt: Dict[str, str] = {}
        self.deck_txt: Dict[str, str] = {}
        self.dock_name = ""
        self.deck_name = ""
        self._load()

    def _load(self) -> None:
        if not self.path or not os.path.exists(self.path):
            return
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                d = json.load(f)
            self.deck_replies = {int(k): bytes.fromhex(v) for k, v in (d.get("deck_replies") or {}).items()}
            self.dock_replies = {k: bytes.fromhex(v) for k, v in (d.get("dock_replies") or {}).items()}
            self.dock_txt = dict(d.get("dock_txt") or {})
            self.deck_txt = dict(d.get("deck_txt") or {})
            self.dock_name = str(d.get("dock_name") or "")
            self.deck_name = str(d.get("deck_name") or "")
        except Exception as ex:
            log.warning("proxy state unreadable, ignoring: %s", ex)

    def save(self) -> None:
        if not self.path:
            return
        try:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"deck_replies": {str(k): v.hex() for k, v in self.deck_replies.items()},
                           "dock_replies": {k: v.hex() for k, v in self.dock_replies.items()},
                           "dock_txt": self.dock_txt, "deck_txt": self.deck_txt,
                           "dock_name": self.dock_name, "deck_name": self.deck_name}, f, indent=1)
            os.replace(tmp, self.path)
        except OSError as ex:
            log.warning("saving proxy state failed: %s", ex)
