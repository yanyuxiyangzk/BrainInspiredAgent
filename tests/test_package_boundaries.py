import ast
import re
import unittest
from importlib import import_module
from pathlib import Path

ROOT = Path(__file__).parents[1]
LOCAL_PACKAGES = {"brain_kernel", "active_agent_platform", "domain_sdk", "apps"}
ALLOWED_IMPORTS = {
    "brain_kernel": set(),
    "active_agent_platform": {"brain_kernel"},
    "domain_sdk": {"brain_kernel", "active_agent_platform"},
    "apps": {"brain_kernel", "active_agent_platform", "domain_sdk"},
}
DOMAIN_TERMS = {"quant", "market", "stock", "factor", "backtest", "qlib", "tdx"}


class PackageBoundaryTests(unittest.TestCase):
    def test_four_layer_packages_are_importable(self) -> None:
        for package in ("brain_kernel", "active_agent_platform", "domain_sdk", "apps.quant_agent"):
            self.assertIsNotNone(import_module(package))

    def test_imports_only_point_toward_lower_layers(self) -> None:
        violations: list[str] = []
        for owner, allowed in ALLOWED_IMPORTS.items():
            for path in (ROOT / owner).rglob("*.py"):
                tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
                imported_roots = {
                    name.split(".", maxsplit=1)[0]
                    for node in ast.walk(tree)
                    for name in self._imported_names(node)
                }
                forbidden = (imported_roots & LOCAL_PACKAGES) - allowed - {owner}
                if forbidden:
                    violations.append(f"{path.relative_to(ROOT)}: {sorted(forbidden)}")
        self.assertEqual([], violations, "layer dependency violations found")

    def test_generic_layers_do_not_contain_quant_domain_terms(self) -> None:
        violations: list[str] = []
        for owner in ("brain_kernel", "active_agent_platform"):
            for path in (ROOT / owner).rglob("*.py"):
                source = path.read_text(encoding="utf-8").lower()
                words = {
                    part
                    for token in re.findall(r"[a-z][a-z0-9_]*", source)
                    for part in token.split("_")
                }
                found = sorted(DOMAIN_TERMS & words)
                if found:
                    violations.append(f"{path.relative_to(ROOT)}: {found}")
        self.assertEqual([], violations, "domain terms leaked into generic layers")

    @staticmethod
    def _imported_names(node: ast.AST) -> tuple[str, ...]:
        if isinstance(node, ast.Import):
            return tuple(alias.name for alias in node.names)
        if isinstance(node, ast.ImportFrom) and node.module:
            return (node.module,)
        return ()
