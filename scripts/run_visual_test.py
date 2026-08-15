"""Visual smoke test: open the launcher and focus the Audio Apps tab.

Run from the ``pipewire-launcher-dev`` Conda environment on a host with a
visible X11 session:

    python scripts/run_visual_test.py

The window opens on screen with the "Audio Apps" tab selected so the detected
audio applications and their PipeWire enable toggles can be validated visually.
Diagnostics are printed to stdout before the Qt event loop starts.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QTabWidget

from pipewire_launcher.__main__ import MainWindow
from pipewire_launcher.application_detection import ApplicationCandidate


def _report(window: MainWindow) -> None:
    panel = window.audio_panel
    applications: tuple[ApplicationCandidate, ...] = panel.manager.applications()
    print(f"[visual-test] detected audio applications: {len(applications)}", flush=True)
    for application in applications:
        enabled = panel.manager.is_enabled(application)
        state = "enabled" if enabled else "disabled"
        print(
            f"[visual-test]   [{state}] {application.name} "
            f"({application.executable})",
            flush=True,
        )
    tabs: QTabWidget = window.centralWidget()
    audio_index = tabs.indexOf(panel)
    print(f"[visual-test] 'Audio Apps' tab index: {audio_index}", flush=True)
    tabs.setCurrentWidget(panel)


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("PipeWire App Launcher (visual test)")
    app.setOrganizationName("R. Brothers Studio")

    window = MainWindow()
    window.show()
    QTimer.singleShot(250, lambda: _report(window))
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
