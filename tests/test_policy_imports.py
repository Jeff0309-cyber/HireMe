"""Hard rule 1: app.policy must never import from llm, agent or marketplace."""
import ast
from pathlib import Path

POLICY_DIR = Path(__file__).resolve().parents[1] / "backend" / "app" / "policy"
FORBIDDEN = ("llm", "agent", "marketplace")


def _imported_modules(path: Path) -> list[str]:
    names = []
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            names += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:  # relative import: resolve against app.policy
                parent = ["app", "policy"][: max(0, 2 - (node.level - 1))]
                base = ".".join(parent + ([base] if base else []))
            names.append(base)
            names += [f"{base}.{a.name}" for a in node.names]
    return names


def test_policy_package_exists():
    assert list(POLICY_DIR.glob("*.py")), "policy package not found"


def test_policy_never_imports_untrusted_layers():
    offenders = []
    for path in POLICY_DIR.rglob("*.py"):
        for mod in _imported_modules(path):
            parts = mod.split(".")
            if any(p in FORBIDDEN for p in parts) or "importlib" in parts or mod == "builtins.__import__":
                offenders.append(f"{path.name}: {mod}")
    assert not offenders, offenders


def test_detector_catches_relative_import(tmp_path):
    bad = tmp_path / "bad.py"
    bad.write_text("from ..llm import LLMClient\nfrom app.agent import run_agent\n")
    mods = _imported_modules(bad)
    assert "app.llm" in mods and "app.agent" in mods
