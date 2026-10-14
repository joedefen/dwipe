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
        hw_caps_state=ProbeState.PENDING, is_rotational=False,
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
