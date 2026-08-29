from __future__ import annotations

import subprocess

from apps.quant_agent.clipboard import capture_clipboard


class _Result:
    def __init__(self, stdout: str) -> None:
        self.stdout = stdout


def test_capture_clipboard_returns_converted_image_path() -> None:
    outputs = [
        _Result("IMG:C:\\Users\\HP\\AppData\\Local\\Temp\\bia-images\\bia-abc.png"),
        _Result("/mnt/c/Users/HP/AppData/Local/Temp/bia-images/bia-abc.png\n"),
    ]
    commands: list[list[str]] = []

    def run(command: list[str], **kwargs: object) -> _Result:
        del kwargs
        commands.append(command)
        return outputs[len(commands) - 1]

    kind, payload = capture_clipboard(run=run)
    assert kind == "image"
    assert payload == "/mnt/c/Users/HP/AppData/Local/Temp/bia-images/bia-abc.png"
    assert commands[0][0] == "powershell.exe" and commands[1][0] == "wslpath"


def test_capture_clipboard_falls_back_to_manual_path_mapping() -> None:
    def run(command: list[str], **kwargs: object) -> _Result:
        del kwargs
        if command[0] == "powershell.exe":
            return _Result("IMG:C:\\Temp\\bia-x.png")
        return _Result("")

    kind, payload = capture_clipboard(run=run)
    assert kind == "image" and payload == "/mnt/c/Temp/bia-x.png"


def test_capture_clipboard_falls_back_to_text() -> None:
    assert capture_clipboard(run=lambda cmd, **kw: _Result("TXT:hello")) == ("text", "hello")


def test_capture_clipboard_empty_on_timeout() -> None:
    def run(command: list[str], **kwargs: object) -> _Result:
        del command, kwargs
        raise subprocess.TimeoutExpired("powershell.exe", 15)

    assert capture_clipboard(run=run) == ("empty", None)


def test_capture_clipboard_empty_without_image_or_text() -> None:
    assert capture_clipboard(run=lambda cmd, **kw: _Result("TXT:")) == ("empty", None)
