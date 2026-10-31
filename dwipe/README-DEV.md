# dwipe: working on the source tree

This is the developer companion to [`README.md`](README.md), which documents the
tool for its users. Here we document the two things a maintainer needs and a
user does not: the **test gate** and the **release gate** (`./bless`).

## Layout and single sources of truth

| Path | Owns |
| --- | --- |
| [`dwipe/__init__.py`](dwipe/__init__.py) | `__version__`, and nothing else. The build backend reads it, `dwipe --version` prints it, `bless` reads the line without importing the package. |
| [`pyproject.toml`](pyproject.toml) | Name, description, keywords, `requires-python`, runtime dependencies, the SPDX `license` and `license-files`, the `test`/`dev` extras, and the pytest, coverage and ruff configuration. |
| [`bless`](bless) | The release gate. Copied verbatim from `pwr-tray`/`my-snaps`; it holds no copy of this project's identity. |
| [`tests/inspect_dist.py`](tests/inspect_dist.py) | The distribution inspector. `bless` refuses to upload a wheel and sdist it has not inspected with this file. |
| [`runner`](runner) | Runs the tree from source (`python -m dwipe.main`) for debugging. |
| [`linter`](linter) | The advisory pylint run for one file, with the project root on `sys.path`. |
| [`.github/workflows/tests.yml`](.github/workflows/tests.yml) | CI: tests with the coverage floor on Python 3.10 -- 3.13, ruff plus the blocking pylint error classes, and a packaging job that builds, inspects and smoke-installs the wheel. |
| [`.gitignore`](.gitignore) | Keeps gate artifacts (`build/`, `dist/`, `.coverage*`, caches, `*.whl`, `*.tar.gz`) out of `git status`, because the gate refuses a dirty tree. |

The version lives in exactly one place. `pyproject.toml` declares
`dynamic = ["version"]`, so flit reads `dwipe/__init__.py`; a `X.Y.Z.99.N`
rehearsal rewrites that line only inside a throwaway copy.

No `[tool.bless]` table is needed: everything is derived from what is already
there -- `NAME = dwipe`, `MODULE = dwipe`, the console script from the first
`[project.scripts]` entry (`dwipe`), and the inspector at
`tests/inspect_dist.py`.

## The test gate

Install what the gate needs (this is the interpreter that must run `bless`,
too):

    python3 -m venv .venv
    .venv/bin/python -m pip install -e '.[dev]'

`bless` runs every stage with `sys.executable`, so it has to be *that*
interpreter -- the one holding `console-window` and the dev extra. Activate the
virtualenv first, or start the gate by hand:

    .venv/bin/python ./bless

Then the commands `bless` runs, in the same order, on the same interpreter:

    python -m ruff check .
    python -m compileall -q dwipe tests
    python -m pytest -q --cov=dwipe --cov-report=term-missing

The coverage floor (40%) is enforced both here, through
`[tool.coverage.report] fail_under`, and in CI through
`--cov-fail-under=40`, so the release gate and CI cannot enforce different
numbers.

The ruff selection is committed rather than inherited
(`[tool.ruff.lint] select = ["E4", "E7", "E9", "F", "I", "UP", "B"]`,
`target-version = "py310"`) so that no user or global configuration can change
what the gate enforces.

CI additionally runs `pylint --disable=all --enable=E0601,E0602,E0603,E1120,E1121 dwipe`,
which is *not* part of the bless gate: the gate is the release requirement, and
the full pylint run (`./linter <file>`) is the stricter house-style check.

`dwipe --version` is answered before the config file, the root re-run and the
curses screen are touched
([`main()`](dwipe/main.py:18)), because the smoke stage of the release gate runs
it as an ordinary user on a machine with no saved state.

## The release gate: `./bless`

`./bless` takes no arguments and has no flag that skips a stage. It always runs
everything, then prints what it has in hand and asks one question:

    ====> Enter test or publish or quit:

Only `test` and `publish` are accepted, spelled in full; anything else is asked
again. Enter, `quit` or Ctrl-C stops with nothing tagged, uploaded or pushed.

**Stages, in order**

1. **preflight** -- clean tree, the `vX.Y.Z` tag absent, `twine>=7` and
   `packaging>=26` present, and `main`/`dev` both local and on `origin`.
2. **index check** -- refuses if `__version__` is already on PyPI, printing
   every published version, before anything is built or uploaded.
3. **gates** -- the three commands above.
4. **build** -- into `build/bless-<version>/`, wiped first, so no older
   artifact can be sent by mistake.
5. **inspect** -- `tests/inspect_dist.py` against the wheel and the sdist.
6. **check** -- `twine check --strict` on both files.
7. **smoke** -- installs the built wheel into a throwaway venv and runs
   `dwipe --version`, which must print the version being released. This is why
   the console script answers `--version`
   ([`main()`](dwipe/main.py:18)).
8. **branch plan** -- refuses a detached HEAD, a third branch, diverged
   `main`/`dev`, or a push that would not fast-forward.

**`test`** is the rehearsal and never spends the working version. It copies the
tree to `build/bless-test-<version>/src`, rewrites `__version__` to
`X.Y.Z.99.N` (99 keeps it clear of real releases; `N` is one past the highest
suffix TestPyPI already holds for that base), builds, inspects and smoke-tests
the copy, and uploads only to `test.pypi.org`. No tag, no branch, no push. It
prints the install line, which needs `python -m pip cache purge` first and
`--extra-index-url https://pypi.org/simple/`, because TestPyPI carries no
dependencies.

**`publish`** fast-forwards `main` and `dev` to one commit, tags `vX.Y.Z`,
uploads the two inspected files to PyPI, verifies the index, and pushes `main`,
`dev` and the tag.

**Credentials** are resolved per index -- `TWINE_PASSWORD` for PyPI,
`TWINE_TESTPYPI_PASSWORD` for TestPyPI, each overruling the `~/.pypirc` section
whose `repository` matches that index's upload URL -- so a PyPI token is never
spent on a rehearsal. A missing credential is a refusal before `twine` starts.

**Verification** reads the index back after an upload. The read is cache-busted
and patient (up to a quarter of an hour), because both indexes list new releases
through a CDN that lags; a release that is not listed yet is reported as lag
with the project page to check, never as a failed upload, since an accepted
upload cannot be withdrawn.

There is no CI publish job, on purpose: the upload is irreversible and is run by
the owner from the development tree, so the safety lives in one inspectable
command. CI stays a gate.

The branch model is part of the gate, not a convention: `bless` publishes only
from `main` or `dev` and refuses unless *both* exist locally and as
`origin/main` and `origin/dev`. `master` predates the gate; the release pair was
created once, from the commit that adopted the gate:

    git branch -m master main
    git branch dev
    git push -u origin main dev

`origin/master` is left where it was, so the URLs in `README.md` keep resolving.
Change the default branch on GitHub from `master` to `main`, then delete
`origin/master` when convenient; nothing in the gate depends on it.

## What the inspector asserts

[`tests/inspect_dist.py`](tests/inspect_dist.py) is the one implementation, used
by `bless` and by the CI packaging job; expectations are read from
`pyproject.toml` and the package, never from the artifact under test.

* **wheel** -- `Name`, `Version`, `Summary`, `Requires-Python`, `Keywords`,
  `License-Expression` and the *runtime* `Requires-Dist` names match
  `pyproject.toml`; every declared `license-files` entry is listed in
  `License-File`; every `dwipe/*.py` is present; a `LICENSE` is carried; no
  `tests/`, no `.venv`, no `__pycache__`, no `.pyc`.
* **sdist** -- carries `pyproject.toml`, `README.md`, `LICENSE`,
  `dwipe/__init__.py`, `dwipe/main.py`; the `__version__` inside the sdist
  equals the artifact version; no venv or bytecode. The sdist is deliberately
  lean: flit's default payload is enough and `tests/` need not ride along.

Run it by hand on any built pair:

    python tests/inspect_dist.py dist/dwipe-3.2.0-py3-none-any.whl \
                                dist/dwipe-3.2.0.tar.gz

## Releasing, in three steps

1. Bump `__version__` in [`dwipe/__init__.py`](dwipe/__init__.py) and commit.
2. Run `./bless`. The gate runs in full.
3. Answer `test` to rehearse on TestPyPI, then run `./bless` again and answer
   `publish` -- or go straight to `publish`.

`main` and `dev` must both exist and both track origin; `publish` leaves them at
the tagged commit. Everything else about the release is one of the stages above.

## Notes on this adoption

* [`bless`](bless) was copied byte-for-byte from `../pwr-tray/bless` (SHA-256
  `4f756337840b0619fbf6f077b00d4bb3fb9aee95ff18bbeb6a0521d39d52131e`) and is
  not edited here: reuse is "copy verbatim", and any later upstream change is
  adopted as a new copy.
* `dwipe` needed four things `pwr-tray` already had: one version location
  (`dynamic = ["version"]`), an inspector, the `dev` extra with `build`,
  `twine>=7` and `packaging>=26`, and a console script that answers
  `--version`. The metadata was moved to the PEP 639 `license` expression and
  `license-files` at the same time, which is why the build backend floor is
  `flit_core>=3.12`.
* The ruff rule set was added with the gate, so the whole tree was brought to a
  clean `ruff check .` once. `tests/test_bless.py` and
  `tests/test_repo_hygiene.py` keep the gate, the inspector and the single
  version location from drifting.
