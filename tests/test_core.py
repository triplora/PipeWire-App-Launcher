import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pipewire_launcher.core import Profile, ProfileStore, command_parts, command_preview, parse_arguments, parse_environment, validate_profile


class CoreTests(unittest.TestCase):
    def test_arguments_and_preview_preserve_spaces(self):
        p = Profile("Ardour", "/opt/Ardour 8/bin/ardour", parse_arguments('--name "My Session"'))
        self.assertEqual(command_parts(p), ("pw-jack", ["--", "/opt/Ardour 8/bin/ardour", "--name", "My Session"]))
        self.assertEqual(command_preview(p), "pw-jack -- '/opt/Ardour 8/bin/ardour' --name 'My Session'")

    def test_environment_parser(self):
        self.assertEqual(parse_environment("# audio\nPIPEWIRE_LATENCY=256/48000\nMODE=studio"), {"PIPEWIRE_LATENCY":"256/48000", "MODE":"studio"})
        with self.assertRaises(ValueError): parse_environment("BROKEN")

    def test_store_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "profiles.json"; store = ProfileStore(path); profiles = [Profile("Carla", "carla", ["--no-gui"], environment={"PIPEWIRE_LATENCY":"128/48000"})]; store.save(profiles); loaded = store.load()
            self.assertEqual(loaded[0], profiles[0]); self.assertEqual(os.stat(path).st_mode & 0o777, 0o600); self.assertEqual(json.loads(path.read_text())["version"], 1)

    @patch("pipewire_launcher.core.shutil.which")
    def test_validation_reports_missing_pw_jack(self, which):
        which.side_effect = lambda name: "/usr/bin/true" if name == "true" else None
        self.assertIn("pw-jack", " ".join(validate_profile(Profile("Test", "true"))))


if __name__ == "__main__": unittest.main()
