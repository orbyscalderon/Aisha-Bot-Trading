import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FORBIDDEN = ("adapters", "interfaces", "backtest")


def _forbidden_imports(package: str) -> list[str]:
    found: list[str] = []
    for py in (ROOT / package).rglob("*.py"):
        tree = ast.parse(py.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                targets = [node.module] if node.module and node.level == 0 else []
            elif isinstance(node, ast.Import):
                targets = [a.name for a in node.names]
            else:
                continue
            for target in targets:
                if target.split(".")[0] in FORBIDDEN:
                    found.append(f"{py.relative_to(ROOT)}: {target}")
    return found


def test_core_does_not_import_adapters():
    # spec §3.2: las dependencias apuntan siempre hacia dentro
    assert _forbidden_imports("core") == []


def test_ports_do_not_import_adapters():
    assert _forbidden_imports("ports") == []


def test_architecture_check_detects_violation(tmp_path, monkeypatch):
    # Guarda contra un test vacuamente verde: el detector debe fallar ante un import prohibido.
    import tests.test_architecture as me

    pkg = tmp_path / "fakecore"
    pkg.mkdir()
    (pkg / "bad.py").write_text("from adapters.cex import x\nimport backtest.engine\n", encoding="utf-8")
    monkeypatch.setattr(me, "ROOT", tmp_path)
    assert len(me._forbidden_imports("fakecore")) == 2
