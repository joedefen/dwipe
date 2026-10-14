"""Unit tests for dwipe.DrivePreChecker (ranking, summaries, capability parsing)."""
import json
from types import SimpleNamespace

import pytest

import dwipe.DrivePreChecker as dpc
from dwipe.DrivePreChecker import DrivePreChecker, PreCheckResult


# ---------------------------------------------------------------------------
# sort_modes_by_rank
# ---------------------------------------------------------------------------
def test_sort_modes_ssd_worst_to_best_with_star():
    modes = ["Crypto", "Block", "Ovwr", "Rand", "Zero"]
    result = DrivePreChecker.sort_modes_by_rank(modes, is_rotational=False)
    assert result == ["Zero", "Rand", "Ovwr", "Block", "Crypto*"]


def test_sort_modes_hdd_prefers_software():
    modes = ["Rand", "Zero", "Enhanced", "Erase"]
    result = DrivePreChecker.sort_modes_by_rank(modes, is_rotational=True)
    # HDD ranks: Rand=1, Zero=2, Enhanced=3, Erase=4 -> Rand recommended
    assert result == ["Erase", "Enhanced", "Zero", "Rand*"]


def test_sort_modes_unknown_ranks_last():
    modes = ["Mystery", "Zero"]
    result = DrivePreChecker.sort_modes_by_rank(modes, add_star=False)
    # Unknown modes sort as rank 99 and therefore appear first (worst)
    assert result[0] == "Mystery"


def test_sort_modes_empty():
    assert DrivePreChecker.sort_modes_by_rank([]) == []


# ---------------------------------------------------------------------------
# get_fw_caps_summary
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "modes,expected",
    [
        ([], ""),
        (["Crypto"], "ϟCrypto"),
        (["Block", "Erase"], "ϟErase"),   # best rank 2 -> Erase
        (["Ovwr"], "ϟOverwrite"),
        (["Enhanced"], "ϟErase"),
    ],
)
def test_get_fw_caps_summary(modes, expected):
    assert DrivePreChecker.get_fw_caps_summary(modes) == expected


# ---------------------------------------------------------------------------
# get_wipe_command_args
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "mode,expected",
    [
        ("Crypto", "sanitize_crypto"),
        ("Block", "sanitize_block"),
        ("Ovwr", "sanitize_overwrite"),
        ("FCrypto", "format_crypto"),
        ("FErase", "format_erase"),
        ("SCrypto", "sanitize_crypto"),
        ("SBlock", "sanitize_block"),
        ("SOverwrite", "sanitize_overwrite"),
        ("Enhanced", "enhanced"),
        ("Erase", "normal"),
        ("Unknown", ""),
    ],
)
def test_get_wipe_command_args(mode, expected):
    assert DrivePreChecker.get_wipe_command_args(mode) == expected


# ---------------------------------------------------------------------------
# NVMe capability parsing
# ---------------------------------------------------------------------------
def make_proc(returncode=0, stdout=""):
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr="")


def test_check_nvme_drive_full_capabilities(monkeypatch):
    payload = {"sanicap": 0x07, "oncs": 0x04, "fna": 0x04}
    monkeypatch.setattr(dpc.subprocess, "run",
                        lambda *a, **k: make_proc(0, json.dumps(payload)))

    result = DrivePreChecker().check_nvme_drive("/dev/nvme0n1")
    assert isinstance(result, PreCheckResult)
    assert {"Crypto", "Block", "Ovwr", "FCrypto", "FErase"} <= set(result.modes)
    assert result.issues == {}


def test_check_nvme_drive_no_format_support(monkeypatch):
    payload = {"sanicap": 0x01, "oncs": 0x00, "fna": 0x00}
    monkeypatch.setattr(dpc.subprocess, "run",
                        lambda *a, **k: make_proc(0, json.dumps(payload)))

    result = DrivePreChecker().check_nvme_drive("/dev/nvme0n1")
    assert result.modes == {"Crypto": "sanitize_crypto"}


def test_check_nvme_drive_unresponsive(monkeypatch):
    monkeypatch.setattr(dpc.subprocess, "run", lambda *a, **k: make_proc(1, ""))
    result = DrivePreChecker().check_nvme_drive("/dev/nvme0n1")
    assert "Unresponsive" in result.issues


def test_check_nvme_drive_unsupported_when_no_modes(monkeypatch):
    payload = {"sanicap": 0x00, "oncs": 0x00, "fna": 0x00}
    monkeypatch.setattr(dpc.subprocess, "run",
                        lambda *a, **k: make_proc(0, json.dumps(payload)))
    result = DrivePreChecker().check_nvme_drive("/dev/nvme0n1")
    assert result.modes == {}
    assert "Unsupported" in result.issues


# ---------------------------------------------------------------------------
# ATA capability parsing (SataTool faked out)
# ---------------------------------------------------------------------------
class _FakeSata:
    def __init__(self, device, verdict="OK", **secures):
        defaults = dict(enhanced_erase_supported=True, sanitize_supported=True,
                        sanitize_crypto_supported=True, sanitize_block_supported=False,
                        sanitize_overwrite_supported=True)
        defaults.update(secures)
        self.verdict = verdict
        self.secures = SimpleNamespace(**defaults)

    def get_wipe_verdict(self):
        return self.verdict


def test_check_ata_drive_ok(monkeypatch):
    monkeypatch.setattr(dpc, "SataTool", _FakeSata)
    result = DrivePreChecker().check_ata_drive("/dev/sda")
    assert {"Enhanced", "Erase", "SCrypto", "SOverwrite"} <= set(result.modes)
    assert "SBlock" not in result.modes
    assert result.issues == {}


def test_check_ata_drive_dumb_device(monkeypatch):
    monkeypatch.setattr(dpc, "SataTool",
                        lambda dev: _FakeSata(dev, verdict="DumbDevice"))
    result = DrivePreChecker().check_ata_drive("/dev/sda")
    assert result.modes == {}
    assert result.issues == {}


def test_check_ata_drive_frozen(monkeypatch):
    monkeypatch.setattr(dpc, "SataTool",
                        lambda dev: _FakeSata(dev, verdict="Frozen"))
    result = DrivePreChecker().check_ata_drive("/dev/sda")
    assert result.issues.get("Frozen") == "Frozen"
    assert result.modes == {}
