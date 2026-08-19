import unittest
from pathlib import Path


class ProjectMetadataTests(unittest.TestCase):
    def test_project_layout_is_present(self) -> None:
        root = Path(__file__).parents[1]
        self.assertTrue((root / "pyproject.toml").is_file())
        for package in ("brain_kernel", "active_agent_platform", "domain_sdk", "apps"):
            self.assertTrue((root / package / "__init__.py").is_file())
