"""★ Compliance test: every Alpaca call goes through the MCP server.

The hackathon requires projects to use Alpaca's MCP server or its CLI tools.
Importing `alpaca-py` anywhere under `agent/` silently reintroduces the direct
SDK dependency that fails that requirement — and would do so without breaking a
single behavioural test, which is exactly why this check exists.

If this fails, the fix is to route the call through `agent.mcp_client.MCPClient`,
not to relax the test.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
AGENT_ROOT = REPO_ROOT / "agent"

FORBIDDEN_ROOTS = {"alpaca", "alpaca_py"}


def _python_files() -> list[Path]:
    return sorted(AGENT_ROOT.rglob("*.py"))


def test_agent_path_has_python_files():
    """Guard against the glob matching nothing and passing vacuously."""
    assert _python_files(), f"No Python files found under {AGENT_ROOT}"


@pytest.mark.parametrize("path", _python_files(), ids=lambda p: p.name)
def test_module_does_not_import_the_alpaca_sdk(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] in FORBIDDEN_ROOTS:
                    offenders.append(f"import {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            # Relative imports of our own modules are fine; only absolute
            # imports of the `alpaca` package are not.
            if node.level == 0 and node.module and node.module.split(".")[0] in FORBIDDEN_ROOTS:
                offenders.append(f"from {node.module} import ...")

    assert not offenders, (
        f"{path.relative_to(REPO_ROOT)} imports the alpaca-py SDK: {offenders}. "
        "All Alpaca access must go through agent.mcp_client.MCPClient."
    )


def test_requirements_do_not_declare_the_sdk():
    for name in ("requirements.txt", "pyproject.toml"):
        path = REPO_ROOT / name
        if not path.exists():
            continue
        # Strip comments: these files explain *why* the SDK is absent, which
        # necessarily mentions it by name.
        code = "\n".join(
            line for line in path.read_text(encoding="utf-8").splitlines()
            if not line.strip().startswith("#")
        )
        assert "alpaca-py" not in code, f"{name} still declares an alpaca-py dependency"


def test_the_sdk_is_not_even_installed():
    """Belt and braces: it cannot be imported by accident if it is not present.

    A soft check — a shared virtualenv could legitimately carry it — so this
    reports rather than fails, and the AST test above remains the enforcement.
    """
    import importlib.util

    if importlib.util.find_spec("alpaca") is not None:
        pytest.skip("alpaca-py is present in this environment; the AST check still governs")
