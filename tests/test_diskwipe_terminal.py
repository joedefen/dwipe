"""Tests for the shell-free terminal restore helper (audit item 5)."""
import pytest

from dwipe import DiskWipe


class _FakeStdout:
    def __init__(self):
        self.written = []
        self.flushed = False

    def write(self, text):
        self.written.append(text)

    def flush(self):
        self.flushed = True


def test_restore_terminal_writes_ansi_and_calls_stty(monkeypatch):
    out = _FakeStdout()
    monkeypatch.setattr(DiskWipe.sys, "stdout", out)
    called = {}
    monkeypatch.setattr(DiskWipe.subprocess, "run",
                        lambda *a, **k: called.setdefault("args", a))

    DiskWipe._restore_terminal()

    assert "".join(out.written) == "\033[2J\033[H"
    assert out.flushed is True
    assert called["args"][0] == ["stty", "sane"]


def test_restore_terminal_swallows_missing_stty(monkeypatch):
    out = _FakeStdout()
    monkeypatch.setattr(DiskWipe.sys, "stdout", out)

    def boom(*a, **k):
        raise OSError("stty not found")

    monkeypatch.setattr(DiskWipe.subprocess, "run", boom)
    DiskWipe._restore_terminal()  # must not raise on exit paths
