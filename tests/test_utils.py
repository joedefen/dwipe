"""Unit tests for dwipe.Utils (formatting, NVMe capability parsing, logging)."""
from types import SimpleNamespace

import pytest

from dwipe.Utils import Utils


# ---------------------------------------------------------------------------
# Utils.human
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "value,expected",
    [
        (0, "0.0KB"),
        (999, "1.0KB"),
        (1500, "1.5KB"),
        (1_500_000, "1.5MB"),
        (2_000_000_000, "2.0GB"),
        (3_000_000_000_000, "3.0TB"),
    ],
)
def test_human(value, expected):
    assert Utils.human(value) == expected


def test_human_is_decimal_not_binary():
    # 1024 bytes should read as ~1.0KB (decimal), not 1.0KiB
    assert Utils.human(1024) == "1.0KB"


# ---------------------------------------------------------------------------
# Utils.ago_str
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "secs,expected",
    [
        (0, "0s"),
        (30, "30s"),
        (450, "7m30s"),
        (3600, "1h0m"),
        (90000, "1d1h"),
    ],
)
def test_ago_str_seconds(secs, expected):
    assert Utils.ago_str(secs) == expected


def test_ago_str_milliseconds_range():
    assert Utils.ago_str(0.5) == "500ms"
    assert Utils.ago_str(0.75) == "750ms"


def test_ago_str_signed():
    assert Utils.ago_str(-450, signed=True) == "-7m30s"
    assert Utils.ago_str(450, signed=True) == "7m30s"


# ---------------------------------------------------------------------------
# NVMe sanitize capability parsing
# ---------------------------------------------------------------------------
def test_parse_nvme_sanitize_capabilities_all_bits():
    modes = Utils.parse_nvme_sanitize_capabilities({"sanicap": 0x07})
    assert modes == {
        "Crypto": "sanitize_crypto",
        "Block": "sanitize_block",
        "Ovwr": "sanitize_overwrite",
    }


def test_parse_nvme_sanitize_capabilities_crypto_only():
    assert Utils.parse_nvme_sanitize_capabilities({"sanicap": 0x01}) == {
        "Crypto": "sanitize_crypto"
    }


def test_parse_nvme_sanitize_capabilities_none():
    assert Utils.parse_nvme_sanitize_capabilities({"sanicap": 0}) == {}
    assert Utils.parse_nvme_sanitize_capabilities({}) == {}


def test_parse_nvme_sanitize_flags():
    has_san, crypto, block, ovwr = Utils.parse_nvme_sanitize_flags({"sanicap": 0x05})
    assert (has_san, crypto, block, ovwr) == (True, True, False, True)


def test_parse_nvme_sanitize_flags_absent():
    has_san, crypto, block, ovwr = Utils.parse_nvme_sanitize_flags({})
    assert (has_san, crypto, block, ovwr) == (False, False, False, False)


# ---------------------------------------------------------------------------
# Log trimming
# ---------------------------------------------------------------------------
def test_trim_log_if_needed(tmp_path):
    log_path = tmp_path / "log.txt"
    log_path.write_text("\n".join(f"line{i}" for i in range(10)) + "\n")

    Utils.trim_log_if_needed(log_path, max_lines=5)

    lines = log_path.read_text().splitlines()
    # Keeps newest 2/3 of the original 10 lines
    assert len(lines) == 6
    assert lines[-1] == "line9"


def test_trim_log_noop_below_threshold(tmp_path):
    log_path = tmp_path / "log.txt"
    log_path.write_text("a\nb\n")
    Utils.trim_log_if_needed(log_path, max_lines=1000)
    assert log_path.read_text() == "a\nb\n"


def test_trim_log_missing_file_is_safe(tmp_path):
    # Should not raise for a non-existent path
    Utils.trim_log_if_needed(tmp_path / "does-not-exist.txt")


# ---------------------------------------------------------------------------
# Utils.get_device_dict
# ---------------------------------------------------------------------------
def test_get_device_dict_partition_with_disk_parent():
    disk = SimpleNamespace(name="sda", model="Samsung SSD", serial="S123",
                           port="SATA:1", parent=None)
    part = SimpleNamespace(name="sda1", size_bytes=1_000_000, uuid="abcd-1234",
                           type="part", fstype="ext4", label="data",
                           parent="sda")
    partitions = {"sda": disk, "sda1": part}

    info = Utils.get_device_dict(partitions, part)

    assert info["name"] == "sda1"
    assert info["path"] == "/dev/sda1"
    assert info["uuid"] == "abcd-1234"
    assert info["fstype"] == "ext4"
    assert info["label"] == "data"
    # Disk-level fields come from the parent disk
    assert info["model"] == "Samsung SSD"
    assert info["serial"] == "S123"
    assert info["port"] == "SATA:1"


def test_get_device_dict_whole_disk():
    disk = SimpleNamespace(name="sdb", model="WD", serial="W1", port="SATA:2",
                           uuid="", type="disk", fstype="", label="",
                           size_bytes=1_000_000_000, parent=None)
    partitions = {"sdb": disk}

    info = Utils.get_device_dict(partitions, disk)

    assert info["name"] == "sdb"
    assert info["size"].endswith("B")
    # Optional keys are omitted when empty
    assert "fstype" not in info
    assert "label" not in info


# ---------------------------------------------------------------------------
# Utils.log_wipe (legacy text log)
# ---------------------------------------------------------------------------
def test_log_wipe_writes_line(config_dir, monkeypatch):
    log_path = config_dir / "log.txt"
    if log_path.exists():
        log_path.unlink()

    Utils.log_wipe("sdb1", 1_000_000, "Zero", "completed", elapsed_time=65,
                   uuid="deadbeef")

    content = log_path.read_text()
    assert "sdb1" in content
    assert "Zero" in content
    assert "100%" in content
