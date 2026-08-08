import hashlib
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "build-appimage.sh"


class BuildAppImageContractTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.tool = self.base / "validated-appimagetool"
        self.runtime = self.base / "runtime-x86_64"
        self.xcb_cursor_lib = self.base / "libxcb-cursor.so.0"
        self.xcb_cursor_lib.write_bytes(b"fake libxcb-cursor.so.0")
        self.tool.write_text(
            "#!/bin/sh\n"
            "if [ \"$1\" = \"--version\" ]; then echo 'appimagetool 1.9.1-test'; exit 0; fi\n"
            "printf '%s\\n' \"$@\" > \"$APPIMAGETOOL_LOG\"\n"
            "printf '%s\\n' 'fake AppImage' > \"$4\"\n"
        )
        self.tool.chmod(0o755)
        self.runtime.write_bytes(b"validated runtime")
        self.tool_digest = hashlib.sha256(self.tool.read_bytes()).hexdigest()
        self.runtime_digest = hashlib.sha256(self.runtime.read_bytes()).hexdigest()

    def tearDown(self):
        self.tmp.cleanup()

    def run_build(self, *args, extra_env=None):
        env = os.environ.copy()
        env["APPIMAGETOOL_LOG"] = str(self.base / "tool.log")
        env["LDCONFIG_LOG"] = str(self.base / "ldconfig.log")
        env["XCB_CURSOR_LIBRARY"] = str(self.xcb_cursor_lib)
        fake_bin = self.base / "bin"
        fake_bin.mkdir(exist_ok=True)
        env["PATH"] = f"{fake_bin}:{env['PATH']}"
        fake_python = fake_bin / "python3"
        fake_python.write_text(
            "#!/bin/sh\n"
            "if [ \"$1\" = \"-m\" ] && [ \"$2\" = \"venv\" ]; then\n"
            "  mkdir -p \"$3/bin\"\n"
            "  printf '#!/bin/sh\\nexport PATH=\"%s/bin:/usr/bin:/bin\"\\n' \"$3\" > \"$3/bin/activate\"\n"
            "  printf '%s\\n' '#!/bin/sh' "
            "'if [ \"$1\" = \"-m\" ] && [ \"$2\" = \"PyInstaller\" ]; then' "
            "'mkdir -p dist/pipewire-app-launcher/_internal/PySide6/Qt/plugins/platforms' "
            "'printf fake > dist/pipewire-app-launcher/_internal/PySide6/Qt/plugins/platforms/libqxcb.so' "
            "'fi' > \"$3/bin/python\"\n"
            "  chmod +x \"$3/bin/python\"\n"
            "  printf '#!/bin/sh\\nexit 0\\n' > \"$3/bin/ldd\"\n"
            "  chmod +x \"$3/bin/ldd\"\n"
            "  exit 0\n"
            "fi\n"
            "exit 0\n"
        )
        fake_python.chmod(0o755)
        fake_ldd = fake_bin / "ldd"
        fake_ldd.write_text("#!/bin/sh\nexit 0\n")
        fake_ldd.chmod(0o755)
        fake_ldconfig = fake_bin / "ldconfig"
        fake_ldconfig.write_text(
            "#!/bin/sh\n"
            "if [ \"$1\" = \"-p\" ]; then\n"
            "  printf 'executable=%s\\npath=%s\\nlibrary=%s\\n' \"$0\" \"$PATH\" \"$XCB_CURSOR_LIBRARY\" > \"$LDCONFIG_LOG\"\n"
            "  printf '%s\\n' \"\\tlibxcb-cursor.so.0 (libc6,x86-64) => $XCB_CURSOR_LIBRARY\"\n"
            "  exit 0\n"
            "fi\n"
            "exit 1\n"
        )
        fake_ldconfig.chmod(0o755)
        env.update(extra_env or {})
        return subprocess.run(
            [
                "bash", str(SCRIPT), "--appimagetool", str(self.tool),
                "--appimagetool-sha256", self.tool_digest,
                "--runtime-file", str(self.runtime), "--runtime-sha256",
                self.runtime_digest, *args,
            ],
            cwd=ROOT, env=env, text=True, capture_output=True,
        )

    def test_rejects_forbidden_work_dirs_and_symlink(self):
        for value in ("", "/", str(ROOT)):
            result = self.run_build("--work-dir", value)
            self.assertNotEqual(result.returncode, 0)
        link = self.base / "work-link"
        link.symlink_to(self.base)
        result = self.run_build("--work-dir", str(link))
        self.assertNotEqual(result.returncode, 0)

    def test_rejects_missing_tool_and_bad_checksum(self):
        result = subprocess.run(
            [
                "bash", str(SCRIPT), "--appimagetool", str(self.base / "missing"),
                "--appimagetool-sha256", "0" * 64,
            ],
            cwd=ROOT, text=True, capture_output=True,
        )
        self.assertNotEqual(result.returncode, 0)
        result = self.run_build(
            "--work-dir", str(self.base / "work"),
            "--appimagetool-sha256", "0" * 64,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("SHA-256 mismatch", result.stderr)

    def test_isolates_build_and_preserves_external_output(self):
        work = self.base / "work"
        output = self.base / "output"
        result = self.run_build("--work-dir", str(work), "--output-dir", str(output))
        self.assertEqual(result.returncode, 0, result.stderr)
        artifact = output / "PipeWire-App-Launcher-x86_64.AppImage"
        self.assertTrue(artifact.is_file())
        self.assertGreater(artifact.stat().st_size, 0)
        self.assertFalse((ROOT / ".build-venv").exists())
        self.assertFalse((ROOT / "build").exists())
        self.assertFalse((ROOT / "dist").exists())
        self.assertFalse((work / "run").exists())
        ldconfig_log = (self.base / "ldconfig.log").read_text()
        self.assertIn(f"executable={self.base / 'bin' / 'ldconfig'}", ldconfig_log)
        self.assertIn(f"path={self.base / 'bin'}:", ldconfig_log)
        self.assertIn(f"library={self.xcb_cursor_lib}", ldconfig_log)
        self.assertTrue(str(self.xcb_cursor_lib).startswith(str(self.base) + os.sep))
        self.assertIn("AppDir", (self.base / "tool.log").read_text())
        second = self.run_build("--work-dir", str(work), "--output-dir", str(output))
        self.assertNotEqual(second.returncode, 0)
        self.assertIn("refusing to overwrite", second.stderr)

    def test_rejects_unsupported_architecture(self):
        uname = self.base / "bin" / "uname"
        uname.parent.mkdir(exist_ok=True)
        uname.write_text("#!/bin/sh\necho aarch64\n")
        uname.chmod(0o755)
        result = self.run_build("--work-dir", str(self.base / "work"))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unsupported architecture", result.stderr)


if __name__ == "__main__":
    unittest.main()
