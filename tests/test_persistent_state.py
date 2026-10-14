"""Unit tests for dwipe.PersistentState (ids, persistence, migration, blocking)."""
import json
from types import SimpleNamespace

from dwipe.PersistentState import PersistentState


def make_part(name="sda1", uuid="", serial="", size_bytes=1000,
              label="lbl", fstype="ext4", parent="sda"):
    return SimpleNamespace(name=name, uuid=uuid, serial=serial,
                           size_bytes=size_bytes, label=label, fstype=fstype,
                           parent=parent)


# ---------------------------------------------------------------------------
# make_device_id priority
# ---------------------------------------------------------------------------
def test_make_device_id_prefers_uuid():
    assert PersistentState.make_device_id(make_part(uuid="U-1", serial="S-1")) == "uuid:U-1"


def test_make_device_id_falls_back_to_serial():
    assert PersistentState.make_device_id(make_part(uuid="", serial="S-1")) == "serial:S-1"


def test_make_device_id_fallback_hash():
    part = make_part(uuid="", serial="", name="sdb2", size_bytes=2048,
                     label="x", fstype="ext4")
    assert PersistentState.make_device_id(part) == "fallback:sdb2:2048:x:ext4"


# ---------------------------------------------------------------------------
# defaults + option round-trip
# ---------------------------------------------------------------------------
def test_defaults(tmp_config_state):
    state = tmp_config_state()
    assert state.state["wipe_mode"] == "+V"
    assert state.state["passes"] == 1
    assert state.state["verify_pct"] == 1
    assert state.state["port_serial"] == "Auto"
    assert state.state["devices"] == {}


def test_save_and_restore_updated_opts(tmp_config_state):
    state = tmp_config_state()
    opts = SimpleNamespace(theme="gruvbox", passes=4, untouched="keep")
    state.save_updated_opts(opts)
    assert state.state["theme"] == "gruvbox"
    assert state.state["passes"] == 4
    assert state.dirty is True

    # restore only overwrites attributes that already exist on opts
    state.state["passes"] = 7
    state.state["verify_pct"] = 30
    state.restore_updated_opts(opts)
    assert opts.passes == 7
    assert opts.untouched == "keep"
    assert not hasattr(opts, "verify_pct")  # not added because it was absent


def test_save_then_load_roundtrip(tmp_config_state):
    state = tmp_config_state()
    state.state["theme"] = "nord"
    state.state["passes"] = 2
    state.dirty = True
    state.save()

    reloaded = PersistentState(config_path=state.config_path)
    assert reloaded.state["theme"] == "nord"
    assert reloaded.state["passes"] == 2


# ---------------------------------------------------------------------------
# Loading: schema handling + migration
# ---------------------------------------------------------------------------
def test_load_ignores_unknown_keys_and_marks_dirty(tmp_config_state):
    path = tmp_config_state().config_path
    path.write_text(json.dumps({"theme": "nord", "obsolete_key": 42}))

    state = PersistentState(config_path=path)
    assert state.state["theme"] == "nord"
    assert "obsolete_key" not in state.state
    assert state.dirty is True  # cleanup should be triggered


def test_load_migrates_legacy_wipe_mode(tmp_config_state):
    path = tmp_config_state().config_path
    path.write_text(json.dumps({"wipe_mode": "Zero+V"}))
    state = PersistentState(config_path=path)
    assert state.state["wipe_mode"] == "+V"
    assert state.dirty is True


def test_load_migrates_legacy_wipe_mode_without_verify(tmp_config_state):
    path = tmp_config_state().config_path
    path.write_text(json.dumps({"wipe_mode": "Rand"}))
    state = PersistentState(config_path=path)
    assert state.state["wipe_mode"] == "-V"


def test_load_corrupt_json_keeps_defaults(tmp_config_state, capsys):
    path = tmp_config_state().config_path
    path.write_text("{ this is not json")
    state = PersistentState(config_path=path)
    assert state.state["wipe_mode"] == "+V"
    assert "Warning" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Blocking (a.k.a. locking)
# ---------------------------------------------------------------------------
def test_block_unblock_roundtrip(tmp_config_state):
    state = tmp_config_state()
    part = make_part(uuid="U-9")

    assert state.get_device_locked(part) is False
    state.set_device_locked(part, True)
    assert state.get_device_locked(part) is True

    state.set_device_locked(part, False)
    assert state.get_device_locked(part) is False


def test_legacy_locked_field_is_honored(tmp_config_state):
    state = tmp_config_state()
    part = make_part(uuid="U-9")
    state.state["devices"]["uuid:U-9"] = {"locked": True}
    assert state.get_device_locked(part) is True

    # Writing back migrates 'locked' -> 'blocked'
    state.set_device_locked(part, True)
    entry = state.state["devices"]["uuid:U-9"]
    assert entry["blocked"] is True
    assert "locked" not in entry


def test_set_device_locked_records_metadata(tmp_config_state):
    state = tmp_config_state()
    part = make_part(uuid="U-9", name="sdc1", size_bytes=777)
    state.set_device_locked(part, True)
    entry = state.state["devices"]["uuid:U-9"]
    assert entry["last_name"] == "sdc1"
    assert entry["size_bytes"] == 777
    assert "last_seen" in entry


# ---------------------------------------------------------------------------
# Device cleanup
# ---------------------------------------------------------------------------
def test_cleanup_old_devices_keeps_most_recent(tmp_config_state):
    state = tmp_config_state()
    state.max_devices = 3
    state.state["devices"] = {
        f"uuid:d{i}": {"last_seen": i, "last_name": f"d{i}"} for i in range(5)
    }
    state.dirty = True
    state.save()

    kept = state.state["devices"]
    assert len(kept) == 3
    # Highest last_seen values survive
    assert set(kept.keys()) == {"uuid:d4", "uuid:d3", "uuid:d2"}
