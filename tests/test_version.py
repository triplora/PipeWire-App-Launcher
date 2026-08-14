import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class VersionTests(unittest.TestCase):
    def test_canonical_version_matches_pyproject(self):
        from pipewire_launcher import __version__

        pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        match = re.search(r'^version = "([^"]+)"$', pyproject, re.MULTILINE)

        self.assertIsNotNone(match)
        self.assertEqual(__version__, "0.1.4")
        self.assertEqual(match.group(1), __version__)

    def test_version_command_is_headless_and_exact(self):
        env = os.environ.copy()
        env.pop("DISPLAY", None)
        env.pop("WAYLAND_DISPLAY", None)

        with tempfile.TemporaryDirectory() as home:
            config_dir = Path(home) / "config"
            env["HOME"] = home
            env["XDG_CONFIG_HOME"] = str(config_dir)
            result = subprocess.run(
                [sys.executable, "-S", "-m", "pipewire_launcher", "--version"],
                cwd=ROOT,
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertFalse(config_dir.exists())

        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.rstrip("\n"), "PipeWire App Launcher 0.1.4")
        self.assertEqual(result.stderr, "")
