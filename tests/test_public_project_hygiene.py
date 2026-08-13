import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class PublicProjectHygieneTests(unittest.TestCase):
    def read(self, relative_path):
        return (ROOT / relative_path).read_text(encoding="utf-8")

    def test_required_public_project_files_exist(self):
        for relative_path in (
            "CODE_OF_CONDUCT.md",
            "SUPPORT.md",
            ".github/dependabot.yml",
            ".github/ISSUE_TEMPLATE/config.yml",
        ):
            with self.subTest(path=relative_path):
                self.assertTrue((ROOT / relative_path).is_file())

    def test_dependabot_covers_python_and_github_actions(self):
        config = self.read(".github/dependabot.yml")
        ecosystems = re.findall(r"package-ecosystem:\s*([^\s]+)", config)
        self.assertEqual(ecosystems, ["pip", "github-actions"])
        self.assertEqual(config.count("interval: weekly"), 2)
        self.assertEqual(config.count("open-pull-requests-limit: 5"), 2)

    def test_issue_configuration_disables_blank_issues(self):
        config = self.read(".github/ISSUE_TEMPLATE/config.yml")
        self.assertIn("blank_issues_enabled: false", config)
        self.assertIn("/SUPPORT.md", config)
        self.assertIn("/security/advisories/new", config)

    def test_contributing_routes_conduct_and_support(self):
        contributing = self.read("CONTRIBUTING.md")
        self.assertIn("[CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md)", contributing)
        self.assertIn("[SUPPORT.md](SUPPORT.md)", contributing)

    def test_readme_routes_contributors_and_security_reports(self):
        readme = self.read("README.md")
        self.assertIn("[CONTRIBUTING.md](CONTRIBUTING.md)", readme)
        self.assertIn("[CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md)", readme)
        self.assertIn("[SUPPORT.md](SUPPORT.md)", readme)
        self.assertIn("[SECURITY.md](SECURITY.md)", readme)

    def test_support_keeps_vulnerabilities_out_of_public_issues(self):
        support = self.read("SUPPORT.md")
        security = self.read("SECURITY.md")
        self.assertIn("private process", support)
        self.assertIn("Never disclose exploitable details publicly", support)
        self.assertIn("private vulnerability reporting", security)


if __name__ == "__main__":
    unittest.main()
