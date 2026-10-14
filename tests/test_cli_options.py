"""Tests for command-line option handling (dwipe.main -> DiskWipe)."""
from types import SimpleNamespace

from dwipe.PersistentState import PersistentState


def test_cli_mode_flag_overrides_wipe_mode(tmp_path):
    """`--mode -V` (argparse dest 'mode') must reach opts.wipe_mode."""
    from dwipe.DiskWipe import DiskWipe

    persistent = PersistentState(config_path=tmp_path / "state.json")
    opts = SimpleNamespace(mode="-V")

    DiskWipe(opts=opts, persistent_state=persistent)
    assert opts.wipe_mode == "-V"


def test_wipe_mode_defaults_when_mode_absent(tmp_path):
    from dwipe.DiskWipe import DiskWipe

    persistent = PersistentState(config_path=tmp_path / "state.json")
    opts = SimpleNamespace()

    DiskWipe(opts=opts, persistent_state=persistent)
    assert opts.wipe_mode == "+V"
