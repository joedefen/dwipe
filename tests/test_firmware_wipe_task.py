"""Unit tests for dwipe.FirmwareWipeTask result/summary handling.

Construction is safe: no subprocess is executed for tasks that use sanitize
methods or the base class. Firmware erase commands are never run.
"""
from types import SimpleNamespace

from dwipe.FirmwareWipeTask import (FirmwareWipeTask, NvmeWipeTask,
                                    SataWipeTask)


def make_base(wipe_name="Test", command_args="cmd"):
    return FirmwareWipeTask("/dev/loop0", 1000, SimpleNamespace(),
                            command_args, wipe_name)


# ---------------------------------------------------------------------------
# FirmwareWipeTask (base)
# ---------------------------------------------------------------------------
def test_base_estimate_duration_default():
    assert make_base().estimated_duration == 60


def test_check_completion_without_process():
    task = make_base()
    task.process = None
    assert task._check_completion() is None


def test_check_completion_success_and_failure():
    task = make_base()
    task.process = SimpleNamespace(poll=lambda: 0, returncode=0)
    assert task._check_completion() is True

    task.process = SimpleNamespace(poll=lambda: 0, returncode=1)
    assert task._check_completion() is False


def test_get_status_shape():
    task = make_base()
    task.total_size = 1000
    task.total_written = 500
    elapsed, pct, rate, eta, more = task.get_status()
    assert pct == "50%"
    assert rate == "FW"
    assert isinstance(elapsed, str)


def test_base_summary_completed():
    task = make_base(wipe_name="Crypto")
    task.total_size = 1000
    task.total_written = 1000
    task.elapsed_secs = 7
    task.actual_command = ["nvme", "sanitize", "-a", "4", "/dev/loop0"]
    task.return_code = 0

    summary = task.get_summary_dict()
    assert summary["step"] == "firmware Crypto /dev/loop0"
    assert summary["command"] == "nvme sanitize -a 4 /dev/loop0"
    assert summary["return_code"] == 0
    assert summary["result"] == "completed"
    assert "stderr" not in summary


def test_base_summary_partial_with_stderr():
    task = make_base()
    task.total_size = 1000
    task.total_written = 250
    task.actual_command = None
    task.return_code = 1
    task.stderr_output = "device busy\n"

    summary = task.get_summary_dict()
    assert summary["result"] == "partial"
    assert summary["command"] == "N/A"
    assert summary["stderr"] == "device busy"


# ---------------------------------------------------------------------------
# NvmeWipeTask
# ---------------------------------------------------------------------------
def make_nvme(method="sanitize_crypto", name="Crypto"):
    task = NvmeWipeTask("/dev/nvme0n1", 1000, SimpleNamespace(), method, name)
    task.tool.job.est_secs = 0  # force the method-based fallback
    return task


def test_nvme_estimate_duration_by_method():
    assert make_nvme("sanitize_crypto")._estimate_duration() == 10
    assert make_nvme("sanitize_block")._estimate_duration() == 30
    assert make_nvme("sanitize_overwrite")._estimate_duration() == 120
    assert make_nvme("format_erase")._estimate_duration() == 30


def test_nvme_estimate_duration_uses_tool_hint():
    task = NvmeWipeTask("/dev/nvme0n1", 1000, SimpleNamespace(),
                        "sanitize_crypto", "Crypto")
    task.tool.job.est_secs = 42
    assert task._estimate_duration() == 42


# ---------------------------------------------------------------------------
# SataWipeTask (sanitize path avoids invoking hdparm)
# ---------------------------------------------------------------------------
def test_sata_sanitize_estimate_duration():
    task = SataWipeTask("/dev/sda", 1000, SimpleNamespace(),
                        "sanitize_crypto", "SCrypto")
    assert task.sanitize_method == "sanitize_crypto"
    assert task._estimate_duration() == 120


def test_sata_summary_includes_command_sequence():
    task = SataWipeTask("/dev/sda", 1000, SimpleNamespace(),
                        "sanitize_block", "SBlock")
    task.total_size = 1000
    task.total_written = 1000
    task.actual_command = ["hdparm", "--sanitize-block-erase", "/dev/sda"]
    task.return_code = 0
    task.commands_executed = [
        {"cmd": "hdparm --security-set-pass NULL /dev/sda", "returncode": 0,
         "sleep_after": 1.0},
    ]

    summary = task.get_summary_dict()
    assert summary["commands_executed"] == task.commands_executed
    assert summary["result"] == "completed"
