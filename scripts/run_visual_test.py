"""Visual smoke test that exercises the full startup path.

Run from the ``pipewire-launcher-dev`` Conda environment on a host with a
visible X11 session:

    python scripts/run_visual_test.py

The script imports and runs ``pipewire_launcher.__main__.main()`` instead of
constructing the window directly, so the real PipeWire health check from
``pipewire_health.py`` executes before anything else. When PipeWire is not
running, the health check asks the user whether to start it and aborts the
launcher (and therefore this test) when they decline or when startup fails.
When the check passes, the window opens with the "Audio Apps" tab selected so
the detected audio applications and their PipeWire enable toggles can be
validated visually. Diagnostics are printed to stdout before the Qt event loop
starts.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QTabWidget

from pipewire_launcher import __main__ as launcher_main
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


class _TrackingMainWindow(MainWindow):
    """MainWindow that reports diagnostics once it is shown.

    ``launcher_main.main()`` instantiates whatever name
    ``launcher_main.MainWindow`` points to, so the real PipeWire health check
    from ``pipewire_health.py`` has already passed before this constructor is
    reached. The count lets the test distinguish a successful run from a
    health-check abort.
    """

    windows = 0

    def __init__(self) -> None:
        super().__init__()
        type(self).windows += 1
        QTimer.singleShot(250, lambda: _report(self))


def main() -> int:
    print(
        "[visual-test] running pipewire_launcher.__main__.main() "
        "(real PipeWire health check will run first)",
        flush=True,
    )
    launcher_main.MainWindow = _TrackingMainWindow
    code = launcher_main.main()
    if not _TrackingMainWindow.windows:
        print(
            "[visual-test] PipeWire health check aborted the launcher; "
            "no main window was shown.",
            flush=True,
        )
        return 1
    return code


if __name__ == "__main__":
    raise SystemExit(main())
