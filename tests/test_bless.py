"""Regression tests for the release gate and the shared distribution inspector.

`bless` is copied verbatim between projects, so these tests pin the one thing
that is *not* copied -- the identity it derives from this tree -- plus the
refusals a maintainer relies on, and the inspector assertions the gate and CI
share.  Anything that would let a wrong artifact reach an index is worth a test.
"""

import importlib.machinery
import importlib.util
import io
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest
import tomllib

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

def make_project(root, name, *, module=None):
    """A throwaway tree shaped the way `identity()` expects to find one."""
    root.mkdir(parents=True, exist_ok=True)
    package = module or name.replace('-', '_').replace('.', '_')
    (root / package).mkdir()
    (root / package / '__init__.py').write_text('__version__ = "1.2.3"\n')
    table = ['[project]', f'name = "{name}"', 'dynamic = ["version"]']
    if module:
        table += ['', '[tool.bless]', f'module = "{module}"']
    (root / 'pyproject.toml').write_text('\n'.join(table) + '\n')
    return root


def test_the_identity_is_read_from_the_project_table(tmp_path):
    told = bless.identity(make_project(tmp_path / 'plain', 'some-proj'))

    assert told['name'] == 'some-proj'
    # `-` becomes `_`: the name is written once, the package derived from it.
    assert told['module'] == 'some_proj'
    assert told['console'] is None
    assert told['inspector'] == 'tests/inspect_dist.py'


def test_the_tool_table_overrides_the_derivation(tmp_path):
    root = make_project(tmp_path / 'odd', 'some-proj', module='odd_name')
    assert bless.identity(root)['module'] == 'odd_name'


def test_a_missing_package_is_refused_with_both_remedies(tmp_path):
    (tmp_path / 'pyproject.toml').write_text('[project]\nname = "nope"\n')

    with pytest.raises(bless.BlessError) as caught:
        bless.identity(tmp_path)
    message = str(caught.value)
    assert 'nope/__init__.py' in message
    assert '[tool.bless]' in message and 'module = "the_package"' in message


def test_a_package_without_a_version_line_is_refused(tmp_path):
    (tmp_path / 'pyproject.toml').write_text('[project]\nname = "quiet"\n')
    (tmp_path / 'quiet').mkdir()
    (tmp_path / 'quiet' / '__init__.py').write_text('"""no version here"""\n')

    with pytest.raises(bless.BlessError, match='__version__'):
        bless.identity(tmp_path)


def test_the_version_is_read_without_importing_the_package(tmp_path):
    root = make_project(tmp_path / 'read', 'readable')
    assert bless.read_version(root, 'readable') == '1.2.3'


def test_this_tree_derives_its_own_identity():
    name = PROJECT['name']

    assert bless.PROJECT['name'] == name
    assert bless.PROJECT['module'] == name.replace('-', '_')
    assert bless.PROJECT['console'] in list(PROJECT['scripts'])
    assert bless.PROJECT['console'] == 'dwipe'
    assert bless.PROJECT['inspector'] == 'tests/inspect_dist.py'
    assert bless.PYPI_URL == f'https://pypi.org/pypi/{name}/json'
    assert bless.TESTPYPI_URL == f'https://test.pypi.org/pypi/{name}/json'
    assert bless.read_version() == VERSION


def test_the_console_script_reports_the_release_number(monkeypatch, capsys):
    """What `bless` runs in the smoke venv: `dwipe --version`, no root needed."""
    from dwipe.main import main

    monkeypatch.setattr(sys, 'argv', ['dwipe', '--version'])
    main()

    assert capsys.readouterr().out.strip() == f'dwipe {VERSION}'


# --- the index check: a published version is a refusal, before any build ---

def test_a_published_version_is_refused_with_the_whole_release_list():
    document = {'releases': {'2.0.1': [], '2.0.2': []}}

    with pytest.raises(bless.BlessError) as caught:
        bless.check_index('2.0.2', document)
    message = str(caught.value)
    assert 'dwipe 2.0.2' in message
    assert '2.0.1, 2.0.2' in message
    assert 'dwipe/__init__.py' in message


def test_an_unpublished_version_is_accepted_and_sorted_numerically():
    document = {'releases': {'2.0.10': [], '2.0.9': []}}

    assert bless.check_index('2.0.11', document) == ['2.0.9', '2.0.10']


def test_the_test_version_starts_at_99_0():
    assert bless.next_test_version('2.0.2', {'releases': {}}) == '2.0.2.99.0'


def test_the_test_version_is_one_past_the_highest_suffix():
    document = {'releases': {'2.0.2.99.0': [], '2.0.2.99.4': [],
                             '2.0.2.99.1': []}}

    assert bless.next_test_version('2.0.2', document) == '2.0.2.99.5'


def test_the_test_counter_belongs_to_its_own_base_version():
    document = {'releases': {'2.0.1.99.7': []}}
    assert bless.next_test_version('2.0.2', document) == '2.0.2.99.0'


# --- the prompt: the only irreversible question is spelled out ------------

def answer(monkeypatch, *replies):
    prompts = []

    def fake_input(prompt=''):
        prompts.append(prompt)
        try:
            return replies[len(prompts) - 1]
        except IndexError:
            raise EOFError from None

    monkeypatch.setattr('builtins.input', fake_input)
    return prompts


def test_a_stray_answer_is_asked_again_instead_of_deciding(monkeypatch, capsys):
    answer(monkeypatch, 'pub', 'publish')

    assert bless.ask_mode() == 'publish'
    assert "'pub' is not test, publish or quit" in capsys.readouterr().out


@pytest.mark.parametrize('ending', ['', 'quit'])
def test_an_empty_answer_or_quit_stops(monkeypatch, ending):
    answer(monkeypatch, ending)
    assert bless.ask_mode() == ''


def test_ctrl_c_and_the_end_of_input_stop(monkeypatch):
    def interrupted(prompt=''):
        raise KeyboardInterrupt

    def exhausted(prompt=''):
        raise EOFError

    monkeypatch.setattr('builtins.input', interrupted)
    assert bless.ask_mode() == ''

    monkeypatch.setattr('builtins.input', exhausted)
    assert bless.ask_mode() == ''


def test_bless_takes_no_arguments_and_says_where_the_choice_is(capsys):
    assert bless.main(['--nope']) == 1
    printed = capsys.readouterr().err
    assert '--nope' in printed
    assert 'test or publish' in printed


# --- the gate commands follow the derived package -------------------------

def test_the_gate_compiles_and_covers_the_derived_package(monkeypatch):
    commands = []

    def fake_run(command, **kwargs):
        commands.append([str(part) for part in command])
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(bless, 'run', fake_run)
    bless.gates()

    compiling = next(argv for argv in commands if 'compileall' in argv)
    assert bless.MODULE in compiling
    testing = next(argv for argv in commands if 'pytest' in argv)
    assert f'--cov={bless.MODULE}' in testing


# --- the branch plan: main and dev must end on one commit -----------------

def test_branch_plan_refuses_a_detached_head_and_a_third_branch():
    assert 'detached HEAD' in bless.branch_plan(None, {}, lambda a, b: False)['refusal']
    plan = bless.branch_plan('topic', {'main': 'a', 'dev': 'a'},
                             lambda a, b: False)
    assert 'topic' in plan['refusal']


def test_branch_plan_refuses_diverged_branches():
    tips = {'main': 'a', 'dev': 'b', 'origin/main': 'a', 'origin/dev': 'b'}

    plan = bless.branch_plan('main', tips, lambda older, newer: False)
    assert 'diverged' in plan['refusal']


def test_branch_plan_fast_forwards_the_lagging_branch():
    tips = {'main': 'a', 'dev': 'b', 'origin/main': 'a', 'origin/dev': 'b'}

    def is_ancestor(older, newer):
        # `a` is behind `b`; every commit is its own ancestor.
        return older == 'a' or older == newer

    plan = bless.branch_plan('dev', tips, is_ancestor)
    assert plan == {'align': 'main'}


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
