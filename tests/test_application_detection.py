import tempfile
import unittest
from pathlib import Path

from pipewire_launcher.application_detection import (
    ApplicationCandidate,
    application_directories,
    detect_jack_applications,
    parse_application_candidate,
    profiles_from_candidates,
)
from pipewire_launcher.core import Profile


class ApplicationDetectionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.applications = self.root / "applications"
        self.applications.mkdir()
        self.executables = {
            "ardour": "/resolved/ardour",
            "audacity": "/resolved/audacity",
            "carla": "/resolved/carla",
            "calfjackhost": "/resolved/calfjackhost",
            "gmidimonitor": "/resolved/gmidimonitor",
            "xjadeo": "/resolved/xjadeo",
            "raysession": "/resolved/raysession",
            "env": "/usr/bin/env",
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

    def test_xdg_directories_use_expected_precedence(self):
        directories = application_directories(
            {
                "XDG_DATA_HOME": "/user/data",
                "XDG_DATA_DIRS": "/local/data:/system/data",
            },
            Path("/unused"),
        )
        self.assertEqual(
            directories,
            (
                Path("/user/data/applications"),
                Path("/local/data/applications"),
                Path("/system/data/applications"),
            ),
        )

    def test_ardour_is_parsed_and_field_code_is_removed(self):
        path = self.desktop(
            "ardour.desktop",
            "Type=Application\n"
            "Name=Ardour\n"
            "Exec=ardour %f\n"
            "Categories=AudioVideo;Audio;\n",
        )
        candidate = parse_application_candidate(
            path,
            self.applications,
            resolver=self.resolver,
        )
        self.assertEqual(candidate.name, "Ardour")
        self.assertEqual(candidate.executable, "/resolved/ardour")
        self.assertEqual(candidate.arguments, ())

    def test_env_prefix_is_preserved_without_becoming_the_executable(self):
        path = self.desktop(
            "audacity.desktop",
            "Type=Application\n"
            "Name=Audacity\n"
            "Exec=env GDK_BACKEND=x11 audacity %F\n",
        )
        candidate = parse_application_candidate(
            path,
            self.applications,
            resolver=self.resolver,
        )
        self.assertEqual(candidate.executable, "/resolved/audacity")
        self.assertEqual(candidate.environment, (("GDK_BACKEND", "x11"),))
        self.assertEqual(candidate.arguments, ())

    def test_hidden_and_no_display_entries_are_ignored(self):
        hidden = self.desktop(
            "hidden.desktop",
            "Type=Application\nName=Carla\nExec=carla\nHidden=true\n",
        )
        no_display = self.desktop(
            "no-display.desktop",
            "Type=Application\nName=Carla\nExec=carla\nNoDisplay=true\n",
        )
        self.assertIsNone(
            parse_application_candidate(
                hidden,
                self.applications,
                resolver=self.resolver,
            )
        )
        self.assertIsNone(
            parse_application_candidate(
                no_display,
                self.applications,
                resolver=self.resolver,
            )
        )

    def test_generic_audio_application_is_not_assumed_to_support_jack(self):
        self.executables["video-editor"] = "/resolved/video-editor"
        path = self.desktop(
            "video.desktop",
            "Type=Application\n"
            "Name=Video Editor\n"
            "Exec=video-editor\n"
            "Categories=AudioVideo;Audio;\n",
        )
        self.assertIsNone(
            parse_application_candidate(
                path,
                self.applications,
                resolver=self.resolver,
            )
        )

    def test_gmidimonitor_requires_explicit_jack_mode(self):
        alsa = self.desktop(
            "gmidimonitor-alsa.desktop",
            "Type=Application\n"
            "Name=Gmidimonitor ALSA\n"
            "Exec=gmidimonitor --alsa\n",
        )
        jack = self.desktop(
            "gmidimonitor-jack.desktop",
            "Type=Application\n"
            "Name=Gmidimonitor JACK\n"
            "Exec=gmidimonitor --jack\n",
        )
        self.assertIsNone(
            parse_application_candidate(
                alsa,
                self.applications,
                resolver=self.resolver,
            )
        )
        self.assertIsNotNone(
            parse_application_candidate(
                jack,
                self.applications,
                resolver=self.resolver,
            )
        )

    def test_missing_try_exec_rejects_entry(self):
        path = self.desktop(
            "carla.desktop",
            "Type=Application\n"
            "Name=Carla\n"
            "TryExec=missing-carla\n"
            "Exec=carla\n",
        )
        self.assertIsNone(
            parse_application_candidate(
                path,
                self.applications,
                resolver=self.resolver,
            )
        )

    def test_shell_syntax_and_unknown_field_codes_are_rejected(self):
        shell = self.desktop(
            "shell.desktop",
            "Type=Application\nName=Carla\nExec=carla; touch /tmp/file\n",
        )
        field_code = self.desktop(
            "field.desktop",
            "Type=Application\nName=Carla\nExec=carla %Z\n",
        )
        self.assertIsNone(
            parse_application_candidate(
                shell,
                self.applications,
                resolver=self.resolver,
            )
        )
        self.assertIsNone(
            parse_application_candidate(
                field_code,
                self.applications,
                resolver=self.resolver,
            )
        )

    def test_higher_precedence_hidden_entry_masks_system_entry(self):
        user = self.root / "user"
        system = self.root / "system"
        self.desktop(
            "carla.desktop",
            "Type=Application\nName=Carla\nExec=carla\nHidden=true\n",
            user,
        )
        self.desktop(
            "carla.desktop",
            "Type=Application\nName=Carla\nExec=carla\n",
            system,
        )
        self.assertEqual(
            detect_jack_applications(
                (user, system),
                resolver=self.resolver,
            ),
            (),
        )

    def test_detection_is_sorted_and_deduplicated(self):
        self.desktop(
            "z-carla.desktop",
            "Type=Application\nName=Zulu Carla\nExec=carla\n",
        )
        self.desktop(
            "duplicate-carla.desktop",
            "Type=Application\nName=Duplicate\nExec=carla\n",
        )
        self.desktop(
            "ardour.desktop",
            "Type=Application\nName=Ardour\nExec=ardour\n",
        )
        candidates = detect_jack_applications(
            (self.applications,),
            resolver=self.resolver,
        )
        self.assertEqual(
            [candidate.name for candidate in candidates],
            ["Ardour", "Duplicate"],
        )

    def test_profile_creation_preserves_environment_and_skips_existing(self):
        existing = Profile("Existing", "/resolved/ardour")
        candidates = (
            ApplicationCandidate(
                "ardour.desktop",
                "Ardour",
                "/resolved/ardour",
            ),
            ApplicationCandidate(
                "audacity.desktop",
                "Audacity",
                "/resolved/audacity",
                environment=(("GDK_BACKEND", "x11"),),
            ),
        )
        profiles = profiles_from_candidates(candidates, (existing,))
        self.assertEqual(len(profiles), 1)
        self.assertEqual(profiles[0].name, "Audacity")
        self.assertEqual(
            profiles[0].environment,
            {"GDK_BACKEND": "x11"},
        )
        self.assertIn("audacity.desktop", profiles[0].notes)
