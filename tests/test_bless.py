"""Tests for this project's release identity and its distribution inspector.

`bless` is emitted from `~/Projects/blessd/bless` by that repository's `build.py`
and is never edited here, so the gate's policy -- the branch plan, the prompt,
the index check, the build, the emitter -- is tested there, once.  What stays
here is the half only this tree can answer: the identity the gate derives from
this `pyproject.toml`, the console script that reports the version, and the
inspector the release uploads behind.
"""

import importlib.machinery
import importlib.util
import io
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10, which `requires-python` still allows
    import tomli as tomllib

ROOT = Path(__file__).resolve().parent.parent


def load_module(name, path):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    loader.exec_module(module)
    return module


bless = load_module('bless_gate', ROOT / 'bless')
inspect_dist = load_module('inspect_dist', ROOT / 'tests' / 'inspect_dist.py')

PROJECT = tomllib.loads((ROOT / 'pyproject.toml').read_text(encoding='utf-8'))['project']
VERSION = (ROOT / 'dwipe' / '__init__.py').read_text(encoding='utf-8')
VERSION = next(line.split('"')[1] for line in VERSION.splitlines()
               if line.startswith('__version__'))


# --- identity: read from this tree, never written into the tool ------------

def test_this_tree_derives_its_own_identity():
    assert bless.PROJECT['name'] == PROJECT['name']
    # `-` and `.` become `_`: the name is written once, the package derived.
    assert bless.PROJECT['module'] == PROJECT['name'].replace('-', '_')
    assert bless.PROJECT['console'] == next(iter(PROJECT['scripts']))
    assert bless.PROJECT['inspector'] == 'tests/inspect_dist.py'


def test_the_branch_model_is_the_one_this_project_declares():
    """There is no `[tool.bless]` table, so the gate's default stands."""
    manifest = tomllib.loads((ROOT / 'pyproject.toml').read_text(encoding='utf-8'))
    declared = ((manifest.get('tool') or {}).get('bless') or {}).get('branches')
    assert bless.PROJECT['branches'] == tuple(declared or ('main', 'dev'))


def test_the_version_is_read_without_importing_the_package():
    assert bless.read_version() == VERSION


def test_the_console_script_reports_the_release_number(monkeypatch, capsys):
    """What `bless` runs in the smoke venv: `dwipe --version`, no root needed."""
    from dwipe.main import main

    monkeypatch.setattr(sys, 'argv', ['dwipe', '--version'])
    main()

    assert capsys.readouterr().out.strip() == f'dwipe {VERSION}'


# --- the shared inspector ------------------------------------------------

def make_wheel(path, *, summary=None, dependencies=None, include_tests=False,
               modules=True):
    metadata = [
        'Metadata-Version: 2.4',
        'Name: dwipe',
        f'Version: {VERSION}',
        f'Summary: {summary or PROJECT["description"]}',
        f'Requires-Python: {PROJECT["requires-python"]}',
        *[f'Requires-Dist: {dependency}' for dependency in
          (dependencies if dependencies is not None
           else PROJECT['dependencies'])],
        f'Keywords: {", ".join(PROJECT.get("keywords", []))}',
        'License-Expression: MIT',
        'License-File: LICENSE',
        '',
        'A description body.',
    ]
    names = ([f'dwipe/{module.name}'
              for module in (ROOT / 'dwipe').glob('*.py')] if modules else [])
    names.append(f'dwipe-{VERSION}.dist-info/licenses/LICENSE')
    if include_tests:
        names.append('tests/test_smoke.py')
    with zipfile.ZipFile(path, 'w') as archive:
        archive.writestr(f'dwipe-{VERSION}.dist-info/METADATA',
                         '\n'.join(metadata))
        for name in names:
            archive.writestr(name, '')
    return path


def make_sdist(path, *, version=VERSION, include_tests=False):
    base = f'dwipe-{version}'
    members = {
        f'{base}/pyproject.toml': '',
        f'{base}/README.md': '',
        f'{base}/LICENSE': '',
        f'{base}/dwipe/__init__.py': f'__version__ = "{version}"\n',
        f'{base}/dwipe/main.py': '',
    }
    if include_tests:
        members[f'{base}/tests/test_smoke.py'] = ''
    with tarfile.open(path, 'w:gz') as archive:
        for name, text in members.items():
            payload = text.encode('utf-8')
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))
    return path


def test_a_correct_wheel_and_sdist_pass(tmp_path):
    inspect_dist.check_all(make_wheel(tmp_path / 'ok.whl'),
                           make_sdist(tmp_path / 'ok.tar.gz'))


def test_the_inspector_rejects_a_summary_that_is_not_this_project(tmp_path):
    wheel = make_wheel(tmp_path / 'summary.whl',
                       summary='A GTK tray applet for power management')
    with pytest.raises(inspect_dist.DistributionError) as caught:
        inspect_dist.check_wheel(wheel)
    assert 'Summary' in str(caught.value)


def test_the_inspector_rejects_an_undeclared_runtime_dependency(tmp_path):
    wheel = make_wheel(tmp_path / 'extra.whl',
                       dependencies=[*PROJECT['dependencies'], 'pydbus>=0.6.0'])
    with pytest.raises(inspect_dist.DistributionError) as caught:
        inspect_dist.check_wheel(wheel)
    assert 'Requires-Dist' in str(caught.value)


def test_the_inspector_keeps_wheel_contents_honest(tmp_path):
    with_tests = make_wheel(tmp_path / 'tests.whl', include_tests=True)
    with pytest.raises(inspect_dist.DistributionError, match='tests/'):
        inspect_dist.check_wheel(with_tests)

    bare = make_wheel(tmp_path / 'bare.whl', modules=False)
    with pytest.raises(inspect_dist.DistributionError, match='missing'):
        inspect_dist.check_wheel(bare)


def test_the_sdist_must_carry_the_package_and_its_version(tmp_path):
    mismatch = make_sdist(tmp_path / 'old.tar.gz', version='1.0.0')
    with pytest.raises(inspect_dist.DistributionError, match='1.0.0'):
        inspect_dist.check_sdist(mismatch)

    # The rehearsal overrides the version, and only that.
    inspect_dist.check_sdist(make_sdist(tmp_path / 'old.tar.gz',
                                        version='3.2.0.99.0'),
                             version='3.2.0.99.0')
