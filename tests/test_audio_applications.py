import tempfile
import unittest
from pathlib import Path

from pipewire_launcher.application_detection import ApplicationCandidate
from pipewire_launcher.audio_applications import (
    AudioApplicationManager,
    AudioApplicationStore,
    detect_audio_applications,
)


class AudioApplicationDetectionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.applications = self.root / "applications"
        self.applications.mkdir()
        self.executables = {
            "audacity": "/resolved/audacity",
            "carla": "/resolved/carla",
            "video-editor": "/resolved/video-editor",
            "unknown-app": "/resolved/unknown-app",
            "musescore4": "/resolved/musescore4",
        }

    def tearDown(self):
        self.temporary.cleanup()

    def resolver(self, name):
        return self.executables.get(name)

    def desktop(self, name, body, directory=None):
        target_directory = directory or self.applications
        target_directory.mkdir(parents=True, exist_ok=True)
        path = target_directory / name
        path.write_text("[Desktop Entry]\n" + body, encoding="utf-8")
        return path

    def test_audio_categories_are_detected_and_parsed(self):
        self.desktop(
            "audacity.desktop",
            "Type=Application\n"
            "Name=Audacity\n"
            "Exec=audacity %F\n"
            "Categories=AudioVideo;Audio;Recording;\n",
        )
        candidates = detect_audio_applications(
            (self.applications,),
            resolver=self.resolver,
        )
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].name, "Audacity")
        self.assertEqual(candidates[0].executable, "/resolved/audacity")
        self.assertEqual(candidates[0].categories, ("AudioVideo", "Audio", "Recording"))

    def test_known_audio_executable_without_categories_is_detected(self):
        self.desktop(
            "musescore.desktop",
            "Type=Application\nName=MuseScore\nExec=musescore4\n",
        )
        candidates = detect_audio_applications(
            (self.applications,),
            resolver=self.resolver,
        )
        self.assertEqual([candidate.name for candidate in candidates], ["MuseScore"])

    def test_non_audio_categories_are_rejected(self):
        self.desktop(
            "video.desktop",
            "Type=Application\n"
            "Name=Video Editor\n"
            "Exec=video-editor\n"
            "Categories=Video;\n",
        )
        self.assertEqual(
            detect_audio_applications((self.applications,), resolver=self.resolver),
            (),
        )

    def test_unknown_executable_without_audio_categories_is_rejected(self):
        self.desktop(
            "unknown.desktop",
            "Type=Application\nName=Unknown\nExec=unknown-app\nCategories=Utility;\n",
        )
        self.assertEqual(
            detect_audio_applications((self.applications,), resolver=self.resolver),
            (),
        )

    def test_hidden_entry_is_not_detected(self):
        self.desktop(
            "carla.desktop",
            "Type=Application\nName=Carla\nExec=carla\nCategories=AudioVideo;\nHidden=true\n",
        )
        self.assertEqual(
            detect_audio_applications((self.applications,), resolver=self.resolver),
            (),
        )

    def test_detection_is_sorted_and_deduplicated(self):
        self.desktop(
            "a-carla.desktop",
            "Type=Application\nName=Alpha Carla\nExec=carla\nCategories=Audio;\n",
        )
        self.desktop(
            "duplicate.desktop",
            "Type=Application\nName=Duplicate\nExec=audacity\nCategories=Audio;\n",
        )
        self.desktop(
            "z-audacity.desktop",
            "Type=Application\nName=Zulu Audacity\nExec=audacity\nCategories=Audio;\n",
        )
        candidates = detect_audio_applications(
            (self.applications,),
            resolver=self.resolver,
        )
        # The first-encountered (alphabetically sorted) duplicate wins.
        self.assertEqual(
            [candidate.name for candidate in candidates],
            ["Alpha Carla", "Duplicate"],
        )


class AudioApplicationStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / "state" / "audio_applications.json"

    def tearDown(self):
        self.temporary.cleanup()

    def test_round_trip_preserves_enabled_state(self):
        store = AudioApplicationStore(self.path)
        store.save({"audacity.desktop": False, "carla.desktop": True})
        self.assertEqual(
            store.load(),
            {"audacity.desktop": False, "carla.desktop": True},
        )

    def test_missing_file_loads_empty(self):
        self.assertEqual(AudioApplicationStore(self.path).load(), {})

    def test_invalid_file_raises(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text("{not json", encoding="utf-8")
        with self.assertRaises(ValueError):
            AudioApplicationStore(self.path).load()


class AudioApplicationManagerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.store_path = Path(self.temporary.name) / "audio_applications.json"
        self.candidates = (
            ApplicationCandidate(
                desktop_id="audacity.desktop",
                name="Audacity",
                executable="/usr/bin/audacity",
                arguments=("--new",),
                categories=("AudioVideo", "Audio"),
            ),
            ApplicationCandidate(
                desktop_id="carla.desktop",
                name="Carla",
                executable="/usr/bin/carla",
            ),
        )

    def tearDown(self):
        self.temporary.cleanup()

    def detect(self, _directories, *, resolver):
        del resolver
        return self.candidates

    def manager(self):
        return AudioApplicationManager(
            store=AudioApplicationStore(self.store_path),
            detect=self.detect,
        )

    def test_refresh_returns_and_keeps_candidates(self):
        manager = self.manager()
        self.assertEqual(manager.refresh(), self.candidates)
        self.assertEqual(manager.applications(), self.candidates)

    def test_enabled_defaults_true_and_is_persisted(self):
        manager = self.manager()
        manager.refresh()
        self.assertTrue(manager.is_enabled(self.candidates[0]))
        manager.set_enabled(self.candidates[0], False)
        self.assertFalse(manager.is_enabled(self.candidates[0]))
        reloaded = self.manager()
        reloaded.load_enabled()
        self.assertFalse(reloaded.is_enabled(self.candidates[0]))
        self.assertTrue(reloaded.is_enabled(self.candidates[1]))

    def test_launch_command_uses_pw_jack_wrapper(self):
        manager = self.manager()
        program, arguments = manager.launch_command(self.candidates[0])
        self.assertEqual(program, "pw-jack")
        self.assertEqual(arguments, ["--", "/usr/bin/audacity", "--new"])


if __name__ == "__main__":
    unittest.main()
