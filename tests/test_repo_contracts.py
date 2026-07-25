"""Structural contracts for the two script directories.

Fast, no network, no API keys, no PDF work. These catch the failure mode where
a module stops being loadable or starts resolving paths against the working
directory — both of which are invisible until someone runs the pipeline.
"""

import importlib
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_DIRS = ("extraction", "pipeline")


def _modules(dirname):
    return sorted(
        p.stem for p in (REPO_ROOT / dirname).glob("*.py") if not p.stem.startswith("_")
    )


ALL_MODULES = [(d, m) for d in SCRIPT_DIRS for m in _modules(d)]


@pytest.mark.parametrize("dirname,module", ALL_MODULES, ids=[f"{d}/{m}" for d, m in ALL_MODULES])
def test_module_imports(dirname, module):
    """Every script must be importable on its own directory's path.

    The scripts are standalone rather than packaged, so each resolves its
    siblings via its own directory. Importing in a subprocess keeps the two
    directories from polluting each other's namespace, which matters because
    both would otherwise compete for names like `judge`.
    """
    code = f"import sys; sys.path.insert(0, {str(REPO_ROOT / dirname)!r}); import {module}"
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert r.returncode == 0, f"{dirname}/{module}.py failed to import:\n{r.stderr}"


def test_path_constants_are_repo_root_anchored():
    """Data and output paths must not depend on the working directory.

    Before the directory restructure every module resolved `data/` and
    `compare_output/` against the CWD, so running a script from anywhere but
    the repo root read nothing or wrote to the wrong place — silently, in both
    directions.
    """
    offenders = []
    for dirname in SCRIPT_DIRS:
        for path in sorted((REPO_ROOT / dirname).glob("*.py")):
            for lineno, line in enumerate(path.read_text().splitlines(), 1):
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue
                for prefix in ('Path("data', 'Path("compare_output', 'Path("verify_output'):
                    if prefix in stripped:
                        offenders.append(f"{dirname}/{path.name}:{lineno}: {stripped}")
    assert not offenders, "CWD-relative paths found:\n" + "\n".join(offenders)


@pytest.mark.parametrize("dirname", SCRIPT_DIRS)
def test_repo_root_resolves_to_repo_root(dirname):
    """The REPO_ROOT each directory computes must be the actual repo root."""
    module = "match" if dirname == "pipeline" else "judge"
    code = (
        f"import sys; sys.path.insert(0, {str(REPO_ROOT / dirname)!r}); "
        f"import {module}; print({module}.REPO_ROOT)"
    )
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert Path(r.stdout.strip()) == REPO_ROOT


def test_no_console_script_is_declared():
    """`uv run cmra` never worked; the entry point was removed, not fixed.

    uv does not install scripts for unpackaged projects, so a
    [project.scripts] entry here is a trap: it reads as a working command and
    is not one. Add real packaging config before adding the entry back.
    """
    assert "[project.scripts]" not in (REPO_ROOT / "pyproject.toml").read_text()
