"""What a bare install pulls in."""

import importlib
import pkgutil
import subprocess
import sys
from importlib.metadata import requires

import pytest
from packaging.requirements import Requirement

import aox_agent_core

PYTEST_PLUGIN = "aox_agent_core.testing.pytest_plugin"
ALL_MODULES = sorted(
    module.name
    for module in pkgutil.walk_packages(aox_agent_core.__path__, prefix="aox_agent_core.")
)


def test_bare_install_has_exactly_three_dependencies() -> None:
    requirements = [Requirement(line) for line in requires("aox-agent-core") or []]
    core = sorted(requirement.name for requirement in requirements if requirement.marker is None)

    assert core == ["anthropic", "opentelemetry-api", "pydantic"]


def test_extras_are_declared() -> None:
    requirements = [Requirement(line) for line in requires("aox-agent-core") or []]
    extras = {
        extra
        for requirement in requirements
        if requirement.marker is not None
        for extra in ("bedrock", "postgres", "otel", "testing")
        if requirement.marker.evaluate({"extra": extra})
    }

    assert extras == {"bedrock", "postgres", "otel", "testing"}


@pytest.mark.parametrize("module_name", ALL_MODULES)
def test_every_module_imports(module_name: str) -> None:
    importlib.import_module(module_name)


def test_importing_the_library_does_not_import_pytest() -> None:
    library_modules = [name for name in ALL_MODULES if name != PYTEST_PLUGIN]
    script = (
        "import importlib, sys\n"
        f"for name in {library_modules!r}:\n"
        "    importlib.import_module(name)\n"
        "sys.exit(1 if 'pytest' in sys.modules else 0)\n"
    )

    completed = subprocess.run([sys.executable, "-c", script], check=False)

    assert completed.returncode == 0, "a library module imports pytest"
