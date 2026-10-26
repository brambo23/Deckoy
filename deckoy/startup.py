"""'Start with Windows' = a shortcut in the user's Startup folder (no registry, no admin)."""
from __future__ import annotations

import os
import subprocess
import sys
from typing import List, Optional, Tuple

SHORTCUT_NAME = "Deckoy.lnk"


def startup_dir() -> str:
    return os.path.join(os.environ.get("APPDATA", ""), "Microsoft", "Windows", "Start Menu", "Programs", "Startup")


def shortcut_path() -> str:
    return os.path.join(startup_dir(), SHORTCUT_NAME)


def launch_target() -> Tuple[str, str]:
    """(exe, arguments) that start Deckoy: the packaged exe, or pythonw -m deckoy when running from source."""
    if getattr(sys, "frozen", False):
        return sys.executable, "--minimized"
    exe = sys.executable
    pyw = os.path.join(os.path.dirname(exe), "pythonw.exe")
    return (pyw if os.path.exists(pyw) else exe), "-m deckoy --minimized"


def is_enabled() -> bool:
    return os.path.exists(shortcut_path())


def _ps(script: str) -> None:
    subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script], check=True,
                   capture_output=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def set_enabled(enabled: bool, path: Optional[str] = None) -> None:
    lnk = path or shortcut_path()
    if not enabled:
        try:
            os.remove(lnk)
        except FileNotFoundError:
            pass
        return
    exe, args = launch_target()
    os.makedirs(os.path.dirname(lnk), exist_ok=True)
    q = lambda s: s.replace("'", "''")
    _ps("$s=(New-Object -ComObject WScript.Shell).CreateShortcut('%s');$s.TargetPath='%s';$s.Arguments='%s';"
        "$s.WorkingDirectory='%s';$s.Save()" % (q(lnk), q(exe), q(args), q(os.path.dirname(exe))))
