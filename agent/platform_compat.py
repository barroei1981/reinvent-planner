"""OS-specific bits (temp dir, Chrome profile paths, notifications, stale-process cleanup).

macOS keeps its original behaviour; Windows (and Linux, where cheap) are added alongside it.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

IS_WINDOWS = sys.platform == "win32"
IS_MACOS = sys.platform == "darwin"


def tmp_dir() -> Path:
    """Where status/alert/log files live. Stays /tmp on macOS/Linux (the dashboard server serves it)."""
    return Path(tempfile.gettempdir()) if IS_WINDOWS else Path("/tmp")


def chrome_user_data_dir() -> Path:
    if IS_WINDOWS:
        return Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData/Local"))) / "Google/Chrome/User Data"
    if IS_MACOS:
        return Path.home() / "Library/Application Support/Google/Chrome"
    return Path.home() / ".config/google-chrome"


def chrome_profile1_cookies() -> Path:
    profile = chrome_user_data_dir() / "Profile 1"
    if IS_WINDOWS and (profile / "Network/Cookies").exists():
        return profile / "Network/Cookies"  # Chrome 96+ on Windows keeps cookies under Network/
    return profile / "Cookies"


def notify_text(text: str, title: str = "re:Invent seats") -> None:
    """Best-effort desktop notification; the alert log / status events are the record."""
    if IS_MACOS:
        cmd = ["osascript", "-e", f'display notification {json.dumps(text)} with title {json.dumps(title)} sound name "Glass"']
    elif IS_WINDOWS:
        def esc(s: str) -> str:
            return s.replace("'", "''")

        ps = (
            "Add-Type -AssemblyName System.Windows.Forms; "
            "$n = New-Object System.Windows.Forms.NotifyIcon; $n.Icon = [System.Drawing.SystemIcons]::Information; "
            f"$n.Visible = $true; $n.ShowBalloonTip(10000, '{esc(title)}', '{esc(text)}', 'Info'); "
            "Start-Sleep -Seconds 11; $n.Dispose()"
        )
        cmd = ["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", ps]
    else:
        return
    try:
        if IS_WINDOWS:
            subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)  # balloon needs ~11s; don't block
        else:
            subprocess.run(cmd, capture_output=True, timeout=10, check=False)
    except (OSError, subprocess.SubprocessError):
        pass


def profile_lock_files(profile_dir: Path) -> list[Path]:
    names = ("lockfile",) if IS_WINDOWS else ("SingletonLock", "SingletonSocket", "SingletonCookie")
    return [profile_dir / n for n in names]


def kill_processes_using(profile_dir: Path) -> None:
    """Kill leftover processes whose command line mentions the profile dir."""
    if IS_WINDOWS:
        ps = (
            "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*"
            + str(profile_dir).replace("'", "''")
            + "*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"
        )
        cmd = ["powershell", "-NoProfile", "-Command", ps]
    else:
        cmd = ["pkill", "-9", "-f", str(profile_dir)]
    subprocess.run(cmd, capture_output=True, timeout=15, check=False)
