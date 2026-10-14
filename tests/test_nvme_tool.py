"""Tests for NvmeTool command construction and capability parsing (item 6).

All ``nvme``/``subprocess`` interaction is monkeypatched; nothing touches real
hardware.
"""
import subprocess
from types import SimpleNamespace

import pytest

from dwipe.NvmeTool import NvmeTool
from dwipe.Utils import Utils


class _Res:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


# ---------------------------------------------------------------------------
# run_nvme_cmd
# ---------------------------------------------------------------------------
def test_run_nvme_cmd_parses_json_and_adds_json_flag(monkeypatch):
    seen = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        return _Res(stdout='{"a": 1}')

    monkeypatch.setattr(subprocess, "run", fake_run)
    t = NvmeTool("nvme0n1")
    assert t.run_nvme_cmd(["id-ctrl", "/dev/nvme0n1"]) == {"a": 1}
    assert seen["cmd"][0] == "nvme"
    assert "-o" in seen["cmd"] and "json" in seen["cmd"]


def test_run_nvme_cmd_without_json_returns_result(monkeypatch):
    res = _Res(stdout="raw")
    monkeypatch.setattr(subprocess, "run", lambda cmd, **k: res)
    t = NvmeTool("nvme0n1")
    assert t.run_nvme_cmd(["sanitize-log"], json_out=False) is res


def test_run_nvme_cmd_swallows_errors(monkeypatch):
    def boom(cmd, **k):
        raise OSError("nvme missing")

    monkeypatch.setattr(subprocess, "run", boom)
    assert NvmeTool("nvme0n1").run_nvme_cmd(["id-ctrl"]) is None


# ---------------------------------------------------------------------------
# refresh_capabilities / get_wipe_verdict
# ---------------------------------------------------------------------------
def test_refresh_capabilities_sets_flags(monkeypatch):
    monkeypatch.setattr(NvmeTool, "run_nvme_cmd",
                        lambda self, args, json_out=True: {"sanicap": 0, "oncs": 0x04, "fna": 0x04})
    monkeypatch.setattr(Utils, "parse_nvme_sanitize_flags",
                        staticmethod(lambda data: (True, True, False, True)))

    caps = NvmeTool("nvme0n1").refresh_capabilities()
    assert caps.has_sanitize is True
    assert caps.crypto_erase_supported is True
    assert caps.overwrite_supported is True
    assert caps.format_supported is True            # oncs bit 2
    assert caps.format_crypto_supported is True     # fna bit 2 AND format


def _caps(**kw):
    base = dict(has_sanitize=True, crypto_erase_supported=True, block_erase_supported=True,
                overwrite_supported=True, format_supported=True, format_crypto_supported=True)
    base.update(kw)
    return SimpleNamespace(**base)


@pytest.mark.parametrize("method,overrides,expected", [
    ("sanitize_block", {"block_erase_supported": False}, "Block Erase not supported"),
    ("sanitize_crypto", {"crypto_erase_supported": False}, "Crypto Erase not supported"),
    ("sanitize_overwrite", {"overwrite_supported": False}, "Overwrite not supported"),
    ("format_erase", {"format_supported": False}, "Format not supported"),
    ("format_crypto", {"format_supported": False}, "Format not supported"),
])
def test_get_wipe_verdict_unsupported(method, overrides, expected):
    t = NvmeTool("nvme0n1")
    t.caps = _caps(**overrides)
    assert t.get_wipe_verdict(method) == expected


def test_get_wipe_verdict_supported_and_unknown():
    t = NvmeTool("nvme0n1")
    t.caps = _caps()
    assert t.get_wipe_verdict("sanitize_block") == "OK"
    assert t.get_wipe_verdict("bogus").startswith("Unknown method")


# ---------------------------------------------------------------------------
# get_supported_lbaf
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("data,expected", [
    ({"flbas": {"format": 2}, "lbafs": [{}, {}, {}]}, 2),
    ({"flbas": 5, "lbafs": [{}, {}, {}]}, 0),   # 5 out of range -> first supported
    ({"flbas": 1}, 1),                          # no lbafs -> current
    (None, 0),                                  # no data -> default 0
])
def test_get_supported_lbaf(monkeypatch, data, expected):
    monkeypatch.setattr(NvmeTool, "run_nvme_cmd", lambda self, args, json_out=True: data)
    assert NvmeTool("nvme0n1").get_supported_lbaf() == expected


# ---------------------------------------------------------------------------
# get_sanitize_status
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("log,expected", [
    ({"sstat": 2, "sprog": 65535}, (2, 100.0)),
    ({"sstat": 1, "sprog": 0}, (1, 0.0)),
    (None, (None, 0)),
])
def test_get_sanitize_status(monkeypatch, log, expected):
    monkeypatch.setattr(NvmeTool, "run_nvme_cmd", lambda self, args, json_out=True: log)
    assert NvmeTool("nvme0n1").get_sanitize_status() == expected


# ---------------------------------------------------------------------------
# start_wipe command construction
# ---------------------------------------------------------------------------
def test_start_wipe_sanitize_routing(monkeypatch):
    seen = {}

    def fake_run(cmd, **k):
        seen["cmd"] = cmd
        return _Res()

    monkeypatch.setattr(subprocess, "run", fake_run)
    t = NvmeTool("/dev/nvme0n1")
    proc = t.start_wipe("sanitize_crypto")
    assert seen["cmd"][:3] == ["nvme", "sanitize", "-a"]
    assert seen["cmd"][3] == "4"
    assert proc.returncode == 0


def test_start_wipe_format_uses_controller_path_and_ses(monkeypatch):
    seen = {}

    def fake_run(cmd, **k):
        seen["cmd"] = cmd
        return _Res()

    monkeypatch.setattr(subprocess, "run", fake_run)
    t = NvmeTool("/dev/nvme0n1")
    t.start_wipe("format_crypto")
    assert seen["cmd"][0:2] == ["nvme", "format"]
    assert seen["cmd"][2] == "/dev/nvme0"           # controller path (n\d+$ stripped)
    assert "--ses=2" in seen["cmd"]


def test_start_wipe_fallback_on_all_namespace_rejection(monkeypatch):
    calls = []

    def fake_run(cmd, **k):
        calls.append(list(cmd))
        if len(calls) == 1:
            return _Res(returncode=1, stdout="0x410a invalid nsid")
        return _Res(returncode=0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    t = NvmeTool("/dev/nvme0n1")
    proc = t.start_wipe("format_crypto")
    assert len(calls) == 2
    assert calls[1][3] == "--namespace-id=1"
    assert proc.returncode == 0


def test_start_wipe_exception_wrapped_in_process(monkeypatch):
    def boom(cmd, **k):
        raise OSError("no nvme")

    monkeypatch.setattr(subprocess, "run", boom)
    proc = NvmeTool("/dev/nvme0n1").start_wipe("format_crypto")
    assert proc.returncode == 1
    assert "no nvme" in proc.stderr.read()
