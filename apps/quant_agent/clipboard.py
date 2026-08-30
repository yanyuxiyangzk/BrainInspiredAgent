"""Windows clipboard capture for the bia input box, via PowerShell interop.

The WSL side cannot read the Windows clipboard directly; ``powershell.exe``
(reachable through WSL interop) saves a clipboard image to the Windows temp
directory, which is then mapped back to a ``/mnt/...`` path with ``wslpath``.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable

Run = Callable[..., subprocess.CompletedProcess[str]]

PS_SCRIPT = (
    "Add-Type -AssemblyName System.Windows.Forms;"
    "Add-Type -AssemblyName System.Drawing;"
    "$image=[System.Windows.Forms.Clipboard]::GetImage();"
    "if ($image) {"
    "$dir=Join-Path $env:TEMP 'bia-images';"
    "New-Item -ItemType Directory -Force -Path $dir | Out-Null;"
    "$path=Join-Path $dir ('bia-'+[guid]::NewGuid().ToString('N').Substring(0,8)+'.png');"
    "$image.Save($path,[System.Drawing.Imaging.ImageFormat]::Png);"
    "Write-Output ('IMG:' + $path)"
    "} else {"
    "Write-Output ('TXT:' + [System.Windows.Forms.Clipboard]::GetText())"
    "}"
)


def _to_wsl_path(windows_path: str, run: Run) -> str:
    converted = run(["wslpath", "-u", windows_path], capture_output=True, text=True, check=False)
    text = (converted.stdout or "").strip()
    if text:
        return text
    posix = windows_path.replace("\\", "/")
    if len(posix) > 2 and posix[1] == ":":
        return f"/mnt/{posix[0].lower()}{posix[2:]}"
    return windows_path


def capture_clipboard(run: Run = subprocess.run) -> tuple[str, str | None]:
    """Return ``(kind, payload)`` for the clipboard contents.

    Kinds: ``image`` (payload is a WSL-side PNG path), ``text`` (clipboard
    text), ``empty`` (nothing usable or PowerShell unavailable).
    """
    try:
        result = run(
            ["powershell.exe", "-NoProfile", "-STA", "-Command", PS_SCRIPT],
            capture_output=True, text=True, timeout=15, check=False,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "empty", None
    output = (result.stdout or "").strip()
    if output.startswith("IMG:") and output[4:].strip():
        return "image", _to_wsl_path(output[4:].strip(), run)
    if output.startswith("TXT:"):
        text = output[4:]
        return ("text", text) if text else ("empty", None)
    return "empty", None
