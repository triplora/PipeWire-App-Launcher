"""Qt panel for listing and controlling installed audio applications."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from PySide6.QtCore import QProcess, QProcessEnvironment, QTimer, Qt
from PySide6.QtWidgets import (
    QHeaderView,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from pipewire_launcher.application_detection import ApplicationCandidate
from pipewire_launcher.audio_applications import AudioApplicationManager
from pipewire_launcher.core import Profile, command_parts, command_preview, validate_profile
from pipewire_launcher.process_supervision import (
    ProcessRegistry,
    ProcessState,
    ProcessTerminator,
)


_STATE_LABELS = {
    ProcessState.STOPPED: "Stopped",
    ProcessState.STARTING: "Starting",
    ProcessState.RUNNING: "Running",
    ProcessState.STOPPING: "Stopping",
    ProcessState.EXITED: "Exited",
    ProcessState.FAILED: "Failed",
}

_STOP_ESCALATION_MS = 2000


class AudioApplicationsPanel(QWidget):
    """List installed audio applications and launch/stop them via PipeWire."""

    def __init__(
        self,
        manager: AudioApplicationManager,
        registry: ProcessRegistry,
        terminator: ProcessTerminator | None = None,
        *,
        process_factory: Callable[..., Any] | None = None,
        timer_factory: Callable[..., Any] | None = None,
        message_box: Any = None,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self.manager = manager
        self.registry = registry
        self.terminator = terminator or ProcessTerminator()
        self.process_factory = process_factory or (lambda _parent: QProcess())
        self.timer_factory = timer_factory or (lambda _parent: QTimer())
        self.message_box = message_box or QMessageBox
        self.stop_timers: dict[tuple[str, int], Any] = {}
        self._items: dict[str, QTreeWidgetItem] = {}
        self._building = False
        self._build_ui()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)

        self.refresh_button = QPushButton("Scan for audio apps")
        self.refresh_button.clicked.connect(self.refresh)
        self.start_button = QPushButton("Start selected")
        self.start_button.clicked.connect(self.start_selected)
        self.stop_button = QPushButton("Stop selected")
        self.stop_button.clicked.connect(self.stop_selected)
        controls = QHBoxLayout()
        controls.addWidget(self.refresh_button)
        controls.addWidget(self.start_button)
        controls.addWidget(self.stop_button)
        controls.addStretch()
        layout.addLayout(controls)

        self.summary = QLabel("No applications listed yet. Click 'Scan for audio apps'.")
        layout.addWidget(self.summary)

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Enabled", "Name", "Executable", "Command", "Status"])
        self.tree.setRootIsDecorated(False)
        self.tree.setUniformRowHeights(True)
        self.tree.itemChanged.connect(self._item_changed)
        self.tree.itemSelectionChanged.connect(self._selection_changed)
        header = self.tree.header()
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.ResizeToContents)
        layout.addWidget(self.tree, 1)

        self._update_controls()

    def refresh(self) -> bool:
        try:
            self.manager.load_enabled()
            applications = self.manager.refresh()
        except Exception as exc:
            self.message_box.critical(
                self,
                "Audio apps",
                f"Could not scan applications:\n{exc}",
            )
            return False
        self._rebuild_tree(applications)
        return True

    def _rebuild_tree(self, applications: tuple[ApplicationCandidate, ...]) -> None:
        self._building = True
        try:
            self.tree.clear()
            self._items.clear()
            for application in applications:
                item = QTreeWidgetItem()
                item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
                item.setCheckState(
                    0,
                    Qt.Checked if self.manager.is_enabled(application) else Qt.Unchecked,
                )
                item.setText(1, application.name)
                item.setText(2, application.executable)
                item.setText(3, command_preview(self._profile_for(application)))
                item.setText(4, _STATE_LABELS[ProcessState.STOPPED])
                item.setData(0, Qt.UserRole, application)
                self.tree.addTopLevelItem(item)
                self._items[application.desktop_id] = item
        finally:
            self._building = False
        count = len(applications)
        self.summary.setText(
            f"{count} audio application(s) found."
            if count
            else "No audio applications found. Check Categories/Exec in its .desktop file."
        )
        self._refresh_rows()

    def selected_application(self) -> ApplicationCandidate | None:
        item = self.tree.currentItem()
        if item is None:
            return None
        return item.data(0, Qt.UserRole)

    def start_selected(self) -> bool:
        application = self.selected_application()
        if application is None:
            return False
        if not self.manager.is_enabled(application):
            self.message_box.information(
                self,
                "Audio apps",
                f"{application.name!r} is disabled. Enable it first.",
            )
            return False
        profile = self._profile_for(application)
        errors = validate_profile(profile)
        if errors:
            self.message_box.warning(
                self,
                "Cannot launch",
                "\n".join(f"• {x}" for x in errors),
            )
            return False
        if self.registry.active(profile.id):
            self._refresh_rows()
            return False
        process = self.process_factory(self)
        environment = QProcessEnvironment.systemEnvironment()
        for key, value in profile.environment.items():
            environment.insert(key, value)
        process.setProcessEnvironment(environment)
        if profile.working_directory:
            process.setWorkingDirectory(profile.working_directory)
        record = self.registry.start(profile.id, process)
        process.setProcessChannelMode(QProcess.SeparateChannels)
        process.started.connect(
            lambda pid=profile.id, generation=record.generation, proc=process: self._started(pid, generation, proc)
        )
        process.readyReadStandardOutput.connect(
            lambda pid=profile.id, generation=record.generation, proc=process: self._read_output(pid, generation, proc, True)
        )
        process.readyReadStandardError.connect(
            lambda pid=profile.id, generation=record.generation, proc=process: self._read_output(pid, generation, proc, False)
        )
        process.finished.connect(
            lambda code, status, pid=profile.id, generation=record.generation, proc=process: self._finished(pid, generation, proc, code, status)
        )
        process.errorOccurred.connect(
            lambda error, pid=profile.id, generation=record.generation, proc=process: self._error(pid, generation, proc, error)
        )
        self.registry.append_output(
            profile.id,
            record.generation,
            process,
            stdout=f"$ {command_preview(profile)}\n".encode(),
        )
        program, arguments = command_parts(profile)
        process.start(program, arguments)
        self._refresh_rows()
        self._update_controls()
        return True

    def stop_selected(self) -> bool:
        application = self.selected_application()
        if application is None:
            return False
        return self._stop(application.desktop_id)

    def _stop(self, profile_id: str) -> bool:
        record = self.registry.get(profile_id)
        if not record or not record.active:
            return False
        if not self.registry.request_stop(record.profile_id, record.generation, record.process):
            return False
        self.terminator.graceful(record.process)
        timer = self.timer_factory(self)
        timer.setSingleShot(True)
        timer.timeout.connect(
            lambda pid=record.profile_id, generation=record.generation, proc=record.process: self._force_stop(pid, generation, proc)
        )
        self.stop_timers[(record.profile_id, record.generation)] = timer
        timer.start(_STOP_ESCALATION_MS)
        self._refresh_rows()
        self._update_controls()
        return True

    def _force_stop(self, profile_id: str, generation: int, process: Any) -> None:
        record = self.registry.get(profile_id)
        if record and record.active and self.registry.request_force_kill(profile_id, generation, process):
            self.terminator.force(process)

    def _stop_timer(self, profile_id: str, generation: int) -> None:
        timer = self.stop_timers.pop((profile_id, generation), None)
        if timer is not None:
            timer.stop()
            timer.deleteLater()

    def _started(self, profile_id: str, generation: int, process: Any) -> None:
        if self.registry.set_running(profile_id, generation, process, int(process.processId())):
            self._refresh_rows()

    def _read_output(self, profile_id: str, generation: int, process: Any, stdout: bool) -> None:
        data = bytes(
            process.readAllStandardOutput() if stdout else process.readAllStandardError()
        )
        if not data or not self.registry.is_current(profile_id, generation, process):
            return
        if stdout:
            self.registry.append_output(profile_id, generation, process, stdout=data)
        else:
            self.registry.append_output(profile_id, generation, process, stderr=data)

    def _finished(self, profile_id: str, generation: int, process: Any, code: int, _status) -> None:
        record = self.registry.get(profile_id)
        if not record or not self.registry.is_current(profile_id, generation, process):
            return
        if self.registry.finish(profile_id, generation, process, int(code)):
            self._stop_timer(profile_id, generation)
            self.registry.emit_final_message(
                profile_id,
                generation,
                process,
                f"[finished: {record.state.value}, exit code {code}]\n",
            )
        self._refresh_rows()
        self._update_controls()

    def _error(self, profile_id: str, generation: int, process: Any, error) -> None:
        terminal_errors = {QProcess.FailedToStart, QProcess.Crashed}
        if error not in terminal_errors:
            if self.registry.note_error(profile_id, generation, process, process.errorString()):
                self.registry.append_event(
                    profile_id,
                    generation,
                    process,
                    f"[process error: {error.name}]\n",
                )
            self._refresh_rows()
            return
        record = self.registry.get(profile_id)
        if (
            error == QProcess.Crashed
            and record
            and self.registry.is_current(profile_id, generation, process)
            and record.stop_requested
        ):
            self.registry.append_event(
                profile_id,
                generation,
                process,
                "[stop requested; process termination reported]\n",
            )
            self._refresh_rows()
            return
        if self.registry.fail(profile_id, generation, process, process.errorString()):
            self._stop_timer(profile_id, generation)
            record = self.registry.get(profile_id)
            if record:
                self.registry.emit_final_message(
                    profile_id,
                    generation,
                    process,
                    f"[finished: failed, exit code unavailable; {error.name}]\n",
                )
            self._refresh_rows()
        self._update_controls()

    def _item_changed(self, item: QTreeWidgetItem, column: int) -> None:
        if column != 0 or self._building:
            return
        application = item.data(0, Qt.UserRole)
        if application is None:
            return
        try:
            self.manager.set_enabled(application, item.checkState(0) == Qt.Checked)
        except Exception as exc:
            self.message_box.warning(
                self,
                "Audio apps",
                f"Could not save preference:\n{exc}",
            )
        self._update_controls()

    def _selection_changed(self) -> None:
        self._update_controls()

    def _update_controls(self) -> None:
        application = self.selected_application()
        record = self.registry.get(application.desktop_id) if application else None
        state = record.state if record else ProcessState.STOPPED
        self.start_button.setEnabled(
            application is not None
            and self.manager.is_enabled(application)
            and state in {ProcessState.STOPPED, ProcessState.EXITED, ProcessState.FAILED}
        )
        self.stop_button.setEnabled(
            state in {ProcessState.STARTING, ProcessState.RUNNING, ProcessState.STOPPING}
        )

    def _refresh_rows(self) -> None:
        for desktop_id, item in self._items.items():
            record = self.registry.get(desktop_id)
            state = record.state if record else ProcessState.STOPPED
            item.setText(4, _STATE_LABELS.get(state, state.value))
        self._update_controls()

    def _profile_for(self, application: ApplicationCandidate) -> Profile:
        profile = application.to_profile()
        profile.id = application.desktop_id
        return profile

    def shutdown(self) -> None:
        for (profile_id, generation), timer in list(self.stop_timers.items()):
            timer.stop()
            timer.deleteLater()
        self.stop_timers.clear()
