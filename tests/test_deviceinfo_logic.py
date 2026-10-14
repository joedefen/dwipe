"""Unit tests for dwipe.DeviceInfo state machine and label cleaning.

These avoid device discovery (no /proc or /sys access) and focus on the pure
state-inference logic used to decide whether a device may be wiped.
"""
from types import SimpleNamespace

import pytest

from dwipe.DeviceInfo import DeviceInfo
from dwipe.DeviceWorker import ProbeState


def make_ns(name="sda", parent=None, minors=None, state="-", dflt="-", **kw):
    defaults = dict(
        name=name, parent=parent, minors=minors if minors is not None else [],
        state=state, dflt=dflt, job=None, fstype="", label="", uuid="",
        serial="", port="", mounts=[], size_bytes=1000, model="",
        hw_caps="", hw_caps_summary="", hw_nopes="",
        hw_caps_state=ProbeState.PENDING, is_rotational=False, in_use=False,
        is_container=False, auto_blocked=False,
    )
    defaults.update(kw)
    return SimpleNamespace(**defaults)


def make_info():
    return DeviceInfo(opts=SimpleNamespace())


# ---------------------------------------------------------------------------
# clean_partition_label
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "raw,expected",
    [
        ("", ""),
        ("Basic data partition", "MS-Data"),
        ("Microsoft reserved partition", "MS-Reserved"),
        ("EFI system partition", "EFI"),
        ("EFI System Partition", "EFI"),
        ("MY_DATA", "MY_DATA"),
    ],
)
def test_clean_partition_label(raw, expected):
    assert DeviceInfo.clean_partition_label(raw) == expected


# ---------------------------------------------------------------------------
# set_one_state
# ---------------------------------------------------------------------------
def test_set_one_state_starting_job_marks_disk_busy():
    disk = make_ns("sda")
    part = make_ns("sda1", parent="sda")
    disk.minors.append("sda1")
    nss = {"sda": disk, "sda1": part}

    assert DeviceInfo.set_one_state(nss, part, to="0%") is True
    assert part.state == "0%"
    assert disk.state == "Busy"


def test_set_one_state_stop_requires_job_state():
    disk = make_ns("sda")
    part = make_ns("sda1", parent="sda", state="-")
    disk.minors.append("sda1")
    nss = {"sda": disk, "sda1": part}

    # Cannot STOP a device that isn't in a running/stopped state
    assert DeviceInfo.set_one_state(nss, part, to="STOP") is False


def test_set_one_state_block_propagates_to_parent():
    disk = make_ns("sda")
    part = make_ns("sda1", parent="sda")
    disk.minors.append("sda1")
    nss = {"sda": disk, "sda1": part}

    assert DeviceInfo.set_one_state(nss, part, to="Blk") is True
    assert part.state == "Blk"
    assert disk.state == "iBlk"


def test_set_one_state_unblock_only_from_blocked():
    part = make_ns("sda1", parent="sda", state="Blk")
    nss = {"sda1": part}
    assert DeviceInfo.set_one_state(nss, part, to="Unbl") is True
    assert part.state == "Unbl"

    part2 = make_ns("sdb1", state="-")
    nss2 = {"sdb1": part2}
    assert DeviceInfo.set_one_state(nss2, part2, to="Unbl") is False


def test_set_one_state_mount_propagates_inherited_mount():
    disk = make_ns("sda")
    part = make_ns("sda1", parent="sda", state="Mnt")
    disk.minors.append("sda1")
    nss = {"sda": disk, "sda1": part}

    DeviceInfo.set_one_state(nss, part)
    assert disk.state == "iMnt"


def test_set_one_state_test_to_does_not_mutate():
    disk = make_ns("sda")
    part = make_ns("sda1", parent="sda")
    disk.minors.append("sda1")
    nss = {"sda": disk, "sda1": part}

    result = DeviceInfo.set_one_state(nss, part, test_to="0%")
    assert result is True
    assert part.state == "-"  # unchanged
    assert disk.state == "-"


# ---------------------------------------------------------------------------
# clear_inferred_states
# ---------------------------------------------------------------------------
def test_clear_inferred_states():
    busy = make_ns("sda", state="Busy", dflt="-")
    imnt = make_ns("sdb", state="iMnt", dflt="W")
    blk = make_ns("sdc", state="Blk", dflt="-")
    nss = {"sda": busy, "sdb": imnt, "sdc": blk}

    DeviceInfo.clear_inferred_states(nss)

    assert busy.state == "-"   # inferred -> reset to default
    assert imnt.state == "W"
    assert blk.state == "Blk"  # direct state preserved


# ---------------------------------------------------------------------------
# merge_dev_infos
# ---------------------------------------------------------------------------
def test_merge_returns_input_when_no_previous():
    info = make_info()
    nss = {"sda": make_ns("sda")}
    assert info.merge_dev_infos(nss, None) is nss


def test_merge_carries_job_forward():
    info = make_info()
    job = SimpleNamespace(done=False)
    new = make_ns("sdb", state="-", dflt="-")
    prev = make_ns("sdb", state="-", dflt="-", job=job)

    info.merge_dev_infos({"sdb": new}, {"sdb": prev})
    assert new.job is job


def test_merge_marks_newly_inserted_device():
    info = make_info()
    new = make_ns("sdc", state="-", dflt="-")
    prev = {"sda": make_ns("sda", state="-", dflt="-")}

    info.merge_dev_infos({"sdc": new}, prev)
    assert new.state == "^"
    assert new.newly_inserted is True


# ---------------------------------------------------------------------------
# Regression tests for previously-reported defects
# ---------------------------------------------------------------------------
def test_compute_field_widths_handles_none_label():
    info = make_info()
    ns = make_ns("sda", label=None, fstype="")
    info.compute_field_widths({"sda": ns})  # must not raise
    assert info.head_str is not None


def test_tunables_get_singleton_returns_instance():
    from dwipe.Tunables import Tunables
    Tunables.singleton = None
    first = Tunables.get_singleton()
    second = Tunables.get_singleton()
    assert isinstance(first, Tunables)
    assert first is second


# ---------------------------------------------------------------------------
# holder (LUKS/LVM/RAID) in-use detection
# ---------------------------------------------------------------------------
def test_mark_block_usage_propagates_mount_to_backing(monkeypatch):
    """A mount on a dm holder reaches the LUKS backing partition."""
    info = make_info()
    sda = make_ns("sda")
    sda1 = make_ns("sda1", parent="sda")
    dm0 = make_ns("dm-0", mounts=["/data"])
    entries = {"sda": sda, "sda1": sda1, "dm-0": dm0}

    def fake_ls(path):
        if path.endswith("/sda1/holders"):
            return ["dm-0"]
        if path.endswith("/dm-0/slaves"):
            return ["sda1"]
        return []

    monkeypatch.setattr(DeviceInfo, "_read_sysfs_dir", staticmethod(fake_ls))
    info._mark_block_usage(entries, set())

    assert sda1.mounts == ["/data"]
    assert sda1.in_use is True


def test_mark_block_usage_flags_open_unmounted_holder(monkeypatch):
    """An open-but-unmounted mapping still marks the backing device in use."""
    info = make_info()
    sda1 = make_ns("sda1")
    dm0 = make_ns("dm-0")  # open mapping, nothing mounted on it
    entries = {"sda1": sda1, "dm-0": dm0}

    def fake_ls(path):
        return ["dm-0"] if path.endswith("/sda1/holders") else []

    monkeypatch.setattr(DeviceInfo, "_read_sysfs_dir", staticmethod(fake_ls))
    info._mark_block_usage(entries, set())

    assert sda1.in_use is True
    assert sda1.mounts == []


def test_mark_block_usage_swap_flags_device(monkeypatch):
    info = make_info()
    sda2 = make_ns("sda2")
    entries = {"sda2": sda2}

    monkeypatch.setattr(DeviceInfo, "_read_sysfs_dir", staticmethod(lambda path: []))
    info._mark_block_usage(entries, {"sda2"})

    assert sda2.in_use is True


# ---------------------------------------------------------------------------
# part_str rendering: \busy vs mountpoint
# ---------------------------------------------------------------------------
def test_part_str_renders_busy_for_open_holder():
    info = make_info()
    ns = make_ns("sda1", parent="sda", state="Mnt", fstype="crypto_LUKS")
    ns.type = "part"
    ns.is_usb = False
    ns.in_use = True
    text, _ = info.part_str(ns)
    assert "\\busy" in text


def test_part_str_shows_mountpoint_when_mounted():
    info = make_info()
    ns = make_ns("sda1", parent="sda", state="Mnt", fstype="ext4", mounts=["/data"])
    ns.type = "part"
    ns.is_usb = False
    text, _ = info.part_str(ns)
    assert "/data" in text
    assert "\\busy" not in text


# ---------------------------------------------------------------------------
# device_in_use (last-instant wipe guard)
# ---------------------------------------------------------------------------
def test_device_in_use_true_when_holder_present(monkeypatch):
    monkeypatch.setattr(
        DeviceInfo, "_read_sysfs_dir",
        staticmethod(lambda path: ["dm-0"] if path.endswith("/sda1/holders") else []),
    )
    assert DeviceInfo.device_in_use("sda1") is True


def test_device_in_use_false_for_plain_device(monkeypatch):
    monkeypatch.setattr(DeviceInfo, "_read_sysfs_dir", staticmethod(lambda path: []))
    assert DeviceInfo.device_in_use("zzfake0") is False


# ---------------------------------------------------------------------------
# container classification (auto-block source)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "fstype,expected",
    [
        ("crypto_LUKS", True),
        ("LVM2_member", True),
        ("linux_raid_member", True),
        ("isw_raid_member", True),
        ("swap", True),
        ("crypto_plain", True),
        ("ext4", False),
        ("vfat", False),
        ("", False),
    ],
)
def test_is_container_fstype(fstype, expected):
    assert DeviceInfo.is_container_fstype(fstype) is expected


def test_mark_block_usage_classifies_container(monkeypatch):
    info = make_info()
    luks = make_ns("sda1", fstype="crypto_LUKS")
    plain = make_ns("sdb1", fstype="ext4")
    entries = {"sda1": luks, "sdb1": plain}

    monkeypatch.setattr(DeviceInfo, "_read_sysfs_dir", staticmethod(lambda path: []))
    info._mark_block_usage(entries, set())

    assert luks.is_container is True
    assert plain.is_container is False


def test_part_str_renders_auto_blocked():
    info = make_info()
    ns = make_ns("sda1", parent="sda", state="Blk", fstype="crypto_LUKS")
    ns.type = "part"
    ns.is_usb = False
    ns.auto_blocked = True
    text, _ = info.part_str(ns)
    assert "auto-blocked" in text
