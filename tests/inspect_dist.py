"""Inspect the built wheel and sdist against this tree's own metadata.

One implementation, two callers: the packaging job in
``.github/workflows/tests.yml`` and ``./bless`` (which must never upload a
distribution it has not inspected).  Keeping the assertions here means the
command a maintainer runs and the command CI runs cannot drift apart.

The expected values are read from ``pyproject.toml`` and
``dwipe/__init__.py`` -- never from the artifact under test, which would make
every check tautological.

This project ships no runtime data files, so the wheel assertions are about the
package's Python modules, its metadata, and the absence of tests/bytecode.  The
sdist is deliberately lean: flit's default sdist (pyproject, README, LICENSE and
the package) is what is asserted; ``tests/`` is not required to ride along.

Run directly to inspect a built pair:

    python tests/inspect_dist.py dist/dwipe-3.2.0-py3-none-any.whl \\
                                dist/dwipe-3.2.0.tar.gz
"""

from __future__ import annotations

import re
import sys
import tarfile
import zipfile
from email.parser import Parser
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10, which `requires-python` still allows
    import tomli as tomllib

ROOT = Path(__file__).resolve().parent.parent
# This module is run as a script by CI (`python tests/inspect_dist.py`), so the
# repository root -- not tests/ -- must be importable before `import dwipe`.
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

VERSION_VALUE = re.compile(r"""^__version__ = ['"]([^'"]+)['"]""", re.MULTILINE)


class DistributionError(ValueError):
    """One or more assertions failed; the message lists all of them."""


def project() -> dict:
    """The `[project]` table of this tree's pyproject.toml."""
    with open(ROOT / 'pyproject.toml', 'rb') as handle:
        return tomllib.load(handle)['project']


def expected_version() -> str:
    """The single source of truth, not the artifact's own claim."""
    import dwipe

    return dwipe.__version__


def _requirement_name(requirement: str) -> str:
    match = re.match(r'[A-Za-z0-9_.-]+', requirement.strip())
    return (match.group(0) if match else requirement).lower()


def runtime_names(requirements) -> set[str]:
    """Names of the *runtime* requirements, ignoring any extras.

    The build backend writes every extra into `Requires-Dist` with an
    `extra == "..."` marker, so an unfiltered comparison would demand that a
    user install pytest to run dwipe.
    """
    return {_requirement_name(requirement) for requirement in requirements
            if 'extra ==' not in requirement}


def _keyword_set(value: str | None) -> set[str]:
    if not value:
        return set()
    return {part.strip() for part in value.split(',') if part.strip()}


def wheel_metadata(wheel: Path) -> dict:
    """Parse the fields this project asserts on out of a wheel's METADATA."""
    with zipfile.ZipFile(wheel) as archive:
        metadata_name = next(
            (name for name in archive.namelist()
             if name.endswith('.dist-info/METADATA')), None)
        if metadata_name is None:
            raise DistributionError(f'{wheel}: no .dist-info/METADATA')
        message = Parser().parsestr(
            archive.read(metadata_name).decode('utf-8'))

    return {
        'Name': message['Name'],
        'Version': message['Version'],
        'Summary': message['Summary'],
        'Requires-Python': message['Requires-Python'],
        'Requires-Dist': message.get_all('Requires-Dist') or [],
        'Keywords': _keyword_set(message['Keywords']),
        'License-Expression': message['License-Expression'],
        'License-File': message.get_all('License-File') or [],
    }


def _package_modules() -> set[str]:
    """Every `dwipe/*.py` in the tree, as wheel/sdist member names."""
    return {f'dwipe/{path.name}'
            for path in (ROOT / 'dwipe').glob('*.py')}


def check_wheel(wheel: Path, project_table: dict | None = None,
                version: str | None = None) -> None:
    """Assert the wheel's metadata and contents match this tree.

    Raises `DistributionError` naming every failure, rather than the first, so
    a bad build is fixed in one pass.  `version` overrides
    `expected_version()`, which is what the rehearsal needs: it builds a
    throwaway `X.Y.Z.99.N` on purpose, and that one field is the whole
    difference to assert against.
    """
    project_table = project_table or project()
    expected = {
        'Name': project_table['name'],
        'Version': version or expected_version(),
        'Summary': project_table['description'],
        'Requires-Python': project_table['requires-python'],
    }
    if isinstance(project_table.get('license'), str):
        expected['License-Expression'] = project_table['license']
    problems: list[str] = []

    metadata = wheel_metadata(Path(wheel))
    for field, wanted in expected.items():
        found = metadata[field]
        if found != wanted:
            problems.append(f'{field}: expected {wanted!r}, found {found!r}')

    declared = runtime_names(project_table['dependencies'])
    built = runtime_names(metadata['Requires-Dist'])
    if built != declared:
        problems.append(
            'Requires-Dist: expected exactly '
            f'{sorted(declared)}, found {sorted(built)}')

    if metadata['Keywords'] != set(project_table.get('keywords', [])):
        problems.append(
            'Keywords: expected '
            f'{sorted(project_table.get("keywords", []))}, '
            f'found {sorted(metadata["Keywords"])}')

    for declared in project_table.get('license-files') or []:
        if Path(declared).name not in metadata['License-File']:
            problems.append(
                f'License-File: {declared!r} is declared in license-files '
                f'but the wheel lists {metadata["License-File"]}')

    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()

    for module in sorted(_package_modules()):
        if module not in names:
            problems.append(f'missing from the wheel: {module}')

    if not any(name.endswith(('/licenses/LICENSE', '/LICENSE'))
               for name in names):
        problems.append('the wheel carries no LICENSE file')
    if any(name.startswith('tests/') for name in names):
        problems.append('the wheel contains tests/, which it must not')
    if any('.venv' in name or '__pycache__' in name or name.endswith('.pyc')
           for name in names):
        problems.append('the wheel contains a virtualenv or bytecode cache')

    if problems:
        raise DistributionError(f'{wheel}: ' + '; '.join(problems))


def check_sdist(sdist: Path, version: str | None = None) -> None:
    """Assert the sdist describes this tree and carries the package.

    The sdist policy is deliberately lean (see the module docstring): only
    flit's default payload is required.  `version` is overridden for the
    rehearsal, exactly as for the wheel.
    """
    with tarfile.open(sdist) as archive:
        names = archive.getnames()
        wanted_version = version or expected_version()
        package_file = next(
            (member for member in archive.getmembers()
             if member.name.endswith('dwipe/__init__.py')), None)
        version_found = None
        if package_file is not None:
            extracted = archive.extractfile(package_file)
            if extracted is not None:
                match = VERSION_VALUE.search(
                    extracted.read().decode('utf-8'))
                version_found = match.group(1) if match else None

    problems: list[str] = []
    for suffix in ('pyproject.toml', 'README.md', 'LICENSE',
                   'dwipe/__init__.py', 'dwipe/main.py'):
        if not any(name.endswith(suffix) for name in names):
            problems.append(f'missing from the sdist: {suffix}')
    if package_file is None:
        problems.append('the sdist carries no dwipe/__init__.py')
    elif version_found != wanted_version:
        problems.append(
            f'the sdist package version is {version_found!r}, '
            f'expected {wanted_version!r}')
    if any('.venv' in name or '__pycache__' in name or name.endswith('.pyc')
           for name in names):
        problems.append('the sdist contains a virtualenv or bytecode cache')

    if problems:
        raise DistributionError(f'{sdist}: ' + '; '.join(problems))


def check_all(wheel: Path, sdist: Path, version: str | None = None) -> None:
    check_wheel(Path(wheel), version=version)
    check_sdist(Path(sdist), version=version)


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__.strip().splitlines()[-1], file=sys.stderr)
        print('usage: inspect_dist.py <wheel> <sdist>', file=sys.stderr)
        return 2
    wheel, sdist = argv
    try:
        check_all(Path(wheel), Path(sdist))
    except DistributionError as exc:
        print(f'inspect_dist: {exc}', file=sys.stderr)
        return 1
    print(f'distributions OK: {wheel} and {sdist}')
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
