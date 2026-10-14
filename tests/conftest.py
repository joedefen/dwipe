"""Shared pytest fixtures for the dwipe test-suite.

These tests are intentionally hermetic:
- They never touch ``/dev`` block devices, ``hdparm`` or ``nvme``.
- They redirect dwipe's config directory to a throw-away temp dir *before*
  any dwipe submodule that instantiates ``Tunables()`` at import time runs.
- They do NOT override ``HOME`` (doing so would hide user-site packages such
  as ``console_window`` which dwipe depends on).
"""
import atexit
import shutil
import sys
import tempfile
from pathlib import Path

# Make the project importable when pytest is invoked from the repo root.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Redirect the config directory *before* importing anything that eagerly
# creates files (e.g. ``SataTool.tunables = Tunables()`` runs at import time).
_TMP_CONFIG_DIR = Path(tempfile.mkdtemp(prefix="dwipe-test-cfg-"))

import dwipe.Utils as _utils_module  # noqa: E402  (import after sys.path fix)

_utils_module.Utils.get_config_dir = staticmethod(lambda: _TMP_CONFIG_DIR)

atexit.register(lambda: shutil.rmtree(_TMP_CONFIG_DIR, ignore_errors=True))


import pytest  # noqa: E402


@pytest.fixture
def config_dir():
    """The isolated config directory used by the whole session."""
    return _TMP_CONFIG_DIR


@pytest.fixture
def tmp_config_state(tmp_path):
    """Factory for PersistentState instances backed by a temp file."""
    from dwipe.PersistentState import PersistentState

    def _make(**kwargs):
        path = tmp_path / "state.json"
        return PersistentState(config_path=path)

    return _make
