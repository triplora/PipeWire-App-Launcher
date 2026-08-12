from __future__ import annotations

import sys

from pipewire_launcher import __version__


if "--version" in sys.argv[1:]:
    print(f"PipeWire App Launcher {__version__}")
    raise SystemExit(0)


import time

from PySide6.QtCore import QProcess, QProcessEnvironment, QTimer, Qt
from PySide6.QtGui import QAction, QFontDatabase
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QFileDialog, QFormLayout, QHBoxLayout, QLabel,
    QLineEdit, QListWidget, QMainWindow, QMessageBox, QPlainTextEdit,
    QPushButton, QSplitter, QStatusBar, QToolBar, QTreeWidget, QTreeWidgetItem,
    QVBoxLayout, QWidget,
)

from pipewire_launcher.core import Profile, ProfileStore, command_parts, command_preview, parse_arguments, parse_environment, validate_profile
from pipewire_launcher.process_supervision import ProcessExecution, ProcessRegistry, ProcessState, ProcessTerminator
from pipewire_launcher.pipewire_discovery_runner import (
    PipeWireDiscoveryFailure,
    PipeWireDiscoveryRunner,
    PipeWireDumpResult,
    RunnerState,
)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("PipeWire App Launcher")
        self.resize(1080, 680)
        self.store = ProfileStore()
        self.profiles: list[Profile] = []
        self.registry = ProcessRegistry()
        self.terminator = ProcessTerminator()
        self.stop_timers: dict[tuple[str, int], QTimer] = {}
        self.close_timer: QTimer | None = None
        self.close_requested = False
        self.close_deadline: float | None = None
        self.close_force_deadline: float | None = None
        self.current_id: str | None = None
        self._closing_started = False
        self._discovery_request_id: str | None = None
        self.discovery_runner = PipeWireDiscoveryRunner(self)
        self._build_ui()
        self._connect_discovery_signals()
        self._load()

    def _build_ui(self):
        toolbar = QToolBar("Profiles")
        self.addToolBar(toolbar)
        for text, slot in (("New", self.new_profile), ("Save", self.save_current), ("Duplicate", self.duplicate), ("Delete", self.delete_current)):
            action = QAction(text, self); action.triggered.connect(slot); toolbar.addAction(action)
        toolbar.addSeparator()
        for text, slot in (("Import", self.import_profiles), ("Export", self.export_profiles)):
            action = QAction(text, self); action.triggered.connect(slot); toolbar.addAction(action)

        self.search = QLineEdit(); self.search.setPlaceholderText("Search profiles…"); self.search.textChanged.connect(self.refresh_list)
        self.list = QListWidget(); self.list.currentItemChanged.connect(self.select_item)
        left = QWidget(); lv = QVBoxLayout(left); lv.addWidget(self.search); lv.addWidget(self.list)

        self.name = QLineEdit(); self.executable = QLineEdit(); browse = QPushButton("Browse…"); browse.clicked.connect(self.browse_executable)
        exe_row = QWidget(); eh = QHBoxLayout(exe_row); eh.setContentsMargins(0,0,0,0); eh.addWidget(self.executable); eh.addWidget(browse)
        self.arguments = QLineEdit(); self.arguments.setPlaceholderText('--project "My Session"')
        self.cwd = QLineEdit(); cwd_browse = QPushButton("Browse…"); cwd_browse.clicked.connect(self.browse_cwd)
        cwd_row = QWidget(); ch = QHBoxLayout(cwd_row); ch.setContentsMargins(0,0,0,0); ch.addWidget(self.cwd); ch.addWidget(cwd_browse)
        self.environment = QPlainTextEdit(); self.environment.setPlaceholderText("KEY=value\n# one variable per line"); self.environment.setMaximumHeight(95)
        self.notes = QPlainTextEdit(); self.notes.setMaximumHeight(80)
        self.enabled = QCheckBox("Profile enabled"); self.enabled.setChecked(True)
        self.preview = QLineEdit(); self.preview.setReadOnly(True); self.preview.setFont(QFontDatabase.systemFont(QFontDatabase.FixedFont))
        for field in (self.executable, self.arguments): field.textChanged.connect(self.update_preview)
        self.environment.textChanged.connect(self.update_preview)
        form = QFormLayout(); form.addRow("Name", self.name); form.addRow("Executable", exe_row); form.addRow("Arguments", self.arguments); form.addRow("Working directory", cwd_row); form.addRow("Environment", self.environment); form.addRow("Notes", self.notes); form.addRow("", self.enabled); form.addRow("Command preview", self.preview)
        self.run_button = QPushButton("Run through PipeWire"); self.run_button.clicked.connect(self.run_current)
        self.stop_button = QPushButton("Stop"); self.stop_button.clicked.connect(self.stop_current); self.stop_button.setEnabled(False)
        self.clear_log_button = QPushButton("Clear Log"); self.clear_log_button.clicked.connect(self.clear_current_log)
        buttons = QHBoxLayout(); buttons.addWidget(self.run_button); buttons.addWidget(self.stop_button); buttons.addWidget(self.clear_log_button); buttons.addStretch()
        self.process_info = QLabel("State: stopped")
        self.process_info.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.log = QPlainTextEdit(); self.log.setReadOnly(True); self.log.setMaximumBlockCount(20000); self.log.setFont(QFontDatabase.systemFont(QFontDatabase.FixedFont))
        self.discovery_refresh_button = QPushButton("Refresh PipeWire"); self.discovery_refresh_button.clicked.connect(self.refresh_discovery)
        self.discovery_cancel_button = QPushButton("Cancel"); self.discovery_cancel_button.clicked.connect(self.cancel_discovery)
        self.discovery_state = QLabel("Not queried")
        self.discovery_tree = QTreeWidget(); self.discovery_tree.setHeaderLabels(["Name", "Type", "Application", "PID", "Media class", "ID"]); self.discovery_tree.setRootIsDecorated(True); self.discovery_tree.setUniformRowHeights(True)
        discovery_buttons = QHBoxLayout(); discovery_buttons.addWidget(self.discovery_refresh_button); discovery_buttons.addWidget(self.discovery_cancel_button); discovery_buttons.addStretch()
        right = QWidget(); rv = QVBoxLayout(right); rv.addLayout(form); rv.addLayout(buttons); rv.addWidget(self.process_info); rv.addWidget(QLabel("Process output")); rv.addWidget(self.log, 1); rv.addWidget(QLabel("PipeWire Discovery")); rv.addLayout(discovery_buttons); rv.addWidget(self.discovery_state); rv.addWidget(self.discovery_tree)
        split = QSplitter(); split.addWidget(left); split.addWidget(right); split.setSizes([280, 800]); self.setCentralWidget(split)
        self.setStatusBar(QStatusBar()); self.statusBar().showMessage("Ready")
        self._update_discovery_controls()

    def _connect_discovery_signals(self):
        self.discovery_runner.state_changed.connect(self._discovery_state_changed)
        self.discovery_runner.succeeded.connect(self._discovery_succeeded)
        self.discovery_runner.failed.connect(self._discovery_failed)
        self.discovery_runner.request_rejected.connect(self._discovery_request_rejected)
        self.discovery_runner.finished.connect(self._discovery_finished)

    def _update_discovery_controls(self):
        active = self.discovery_runner.state.active
        self.discovery_refresh_button.setEnabled(bool(self.current_id) and not active and not self._closing_started)
        self.discovery_cancel_button.setEnabled(active and not self._closing_started)

    def _discovery_state_changed(self, state: RunnerState):
        if self._closing_started or self._discovery_request_id != self.current_id:
            return
        if state not in {RunnerState.STARTING, RunnerState.RUNNING, RunnerState.CANCELLING}:
            return
        labels = {
            RunnerState.STARTING: "Starting discovery…",
            RunnerState.RUNNING: "Discovering PipeWire nodes…",
            RunnerState.CANCELLING: "Cancelling…",
        }
        self.discovery_state.setText(labels[state])
        self._update_discovery_controls()

    def _discovery_succeeded(self, result: PipeWireDumpResult):
        if (
            self._closing_started
            or result.request_id != self.current_id
            or result.request_id != self._discovery_request_id
        ):
            return
        snapshot = result.snapshot
        self.discovery_tree.clear()
        for node in snapshot.nodes:
            node_item = QTreeWidgetItem([
                node.name, "Node", node.application_name or "",
                "" if node.process_id is None else str(node.process_id),
                node.media_class or "", str(node.object_id),
            ])
            self.discovery_tree.addTopLevelItem(node_item)
            for port in node.ports:
                node_item.addChild(QTreeWidgetItem([
                    port.name, port.direction.value.title(), "", "", "", str(port.object_id),
                ]))
        count = len(snapshot.nodes)
        self.discovery_state.setText(f"{count} nodes discovered" if count else "No PipeWire nodes found")
        self._update_discovery_controls()

    def _discovery_failed(self, failure: PipeWireDiscoveryFailure):
        if (
            self._closing_started
            or failure.request_id != self.current_id
            or failure.request_id != self._discovery_request_id
        ):
            return
        if failure.category.value == "timeout":
            self.discovery_state.setText("Discovery timed out")
        elif failure.category.value == "cancelled":
            self.discovery_state.setText("Discovery cancelled")
        else:
            message = " ".join(str(failure.message).split())[:240]
            detail = f"{failure.category.value}: {message}" if message else failure.category.value
            self.discovery_state.setText(f"Discovery unavailable — {detail}")
        self._update_discovery_controls()

    def _discovery_request_rejected(self, failure: PipeWireDiscoveryFailure):
        if (
            self._closing_started
            or failure.request_id != self.current_id
            or failure.request_id != self._discovery_request_id
        ):
            return
        message = " ".join(str(failure.message).split())[:240]
        detail = f": {message}" if message else ""
        self.statusBar().showMessage(f"PipeWire discovery request rejected{detail}")
        self._update_discovery_controls()

    def _discovery_finished(self):
        if not self._closing_started:
            self._update_discovery_controls()

    def refresh_discovery(self):
        if self._closing_started or not self.current_id or self.discovery_runner.state.active:
            self._update_discovery_controls()
            return False
        request_id = self.current_id
        started = self.discovery_runner.start(request_id=request_id)
        if started:
            self._discovery_request_id = request_id
            state = self.discovery_runner.state
            if state in {RunnerState.STARTING, RunnerState.RUNNING, RunnerState.CANCELLING}:
                self._discovery_state_changed(state)
            else:
                result = getattr(self.discovery_runner, "result", None)
                failure = getattr(self.discovery_runner, "error", None)
                if result is not None:
                    self._discovery_succeeded(result)
                elif failure is not None:
                    self._discovery_failed(failure)
        self._update_discovery_controls()
        return started

    def cancel_discovery(self):
        if self._closing_started or not self.discovery_runner.state.active:
            self._update_discovery_controls()
            return False
        cancelled = self.discovery_runner.cancel()
        self._update_discovery_controls()
        return cancelled

    def _load(self):
        try: self.profiles = self.store.load()
        except Exception as exc: QMessageBox.warning(self, "Profiles", f"Could not load profiles:\n{exc}")
        self.refresh_list();
        if not self.profiles:
            self.new_profile()
        elif self.list.count() and self.list.currentRow() < 0:
            self.list.setCurrentRow(0)

    def refresh_list(self):
        selected = self.current_id; query = self.search.text().casefold(); self.list.clear()
        for profile in self.profiles:
            if query and query not in (profile.name + " " + profile.executable + " " + profile.notes).casefold(): continue
            self.list.addItem(("● " if profile.enabled else "○ ") + profile.name); item = self.list.item(self.list.count()-1); item.setData(Qt.UserRole, profile.id)
            if profile.id == selected: self.list.setCurrentItem(item)

    def selected_profile(self):
        return next((p for p in self.profiles if p.id == self.current_id), None)

    def select_item(self, current, _previous):
        if not current: return
        self.current_id = current.data(Qt.UserRole); p = self.selected_profile()
        if not p: return
        self.name.setText(p.name); self.executable.setText(p.executable); self.arguments.setText(' '.join(__import__('shlex').quote(x) for x in p.arguments)); self.cwd.setText(p.working_directory); self.environment.setPlainText('\n'.join(f"{k}={v}" for k,v in p.environment.items())); self.notes.setPlainText(p.notes); self.enabled.setChecked(p.enabled); self.update_preview(); self._refresh_process_view(); self._update_discovery_controls()

    def profile_from_form(self):
        return Profile(name=self.name.text().strip(), executable=self.executable.text().strip(), arguments=parse_arguments(self.arguments.text()), working_directory=self.cwd.text().strip(), environment=parse_environment(self.environment.toPlainText()), notes=self.notes.toPlainText().strip(), enabled=self.enabled.isChecked(), id=self.current_id or __import__('uuid').uuid4().hex)

    def new_profile(self):
        p = Profile("New application", ""); self.profiles.append(p); self.current_id = p.id; self.refresh_list()
        for i in range(self.list.count()):
            if self.list.item(i).data(Qt.UserRole) == p.id: self.list.setCurrentRow(i); break

    def save_current(self):
        try: p = self.profile_from_form()
        except ValueError as exc: QMessageBox.warning(self, "Invalid profile", str(exc)); return False
        idx = next((i for i,x in enumerate(self.profiles) if x.id == p.id), None)
        if idx is None: self.profiles.append(p)
        else: self.profiles[idx] = p
        try: self.store.save(self.profiles)
        except Exception as exc: QMessageBox.critical(self, "Save failed", str(exc)); return False
        self.current_id = p.id; self.refresh_list(); self.statusBar().showMessage("Profile saved", 3000)
        return True

    def duplicate(self):
        p = self.selected_profile()
        if not p: return
        copy = Profile.from_dict({**p.__dict__, "id": __import__('uuid').uuid4().hex, "name": p.name + " (copy)"}); self.profiles.append(copy); self.current_id = copy.id; self.refresh_list()

    def delete_current(self):
        p = self.selected_profile()
        if not p or QMessageBox.question(self, "Delete profile", f"Delete {p.name!r}?") != QMessageBox.Yes: return
        self.profiles = [x for x in self.profiles if x.id != p.id]; self.current_id = None; self.store.save(self.profiles); self.refresh_list();
        if self.profiles: self.list.setCurrentRow(0)
        else: self.new_profile()

    def browse_executable(self):
        path, _ = QFileDialog.getOpenFileName(self, "Select application executable", "/usr/bin")
        if path: self.executable.setText(path)

    def browse_cwd(self):
        path = QFileDialog.getExistingDirectory(self, "Select working directory")
        if path: self.cwd.setText(path)

    def update_preview(self):
        try: self.preview.setText(command_preview(self.profile_from_form()))
        except ValueError as exc: self.preview.setText(f"Invalid input: {exc}")

    def run_current(self):
        try: p = self.profile_from_form()
        except ValueError as exc: QMessageBox.warning(self, "Invalid profile", str(exc)); return
        errors = validate_profile(p)
        if not p.enabled: errors.insert(0, "This profile is disabled.")
        if errors: QMessageBox.warning(self, "Cannot launch", "\n".join(f"• {x}" for x in errors)); return
        if self.registry.active(p.id):
            self._refresh_process_view()
            return
        if not self.save_current():
            return
        process = QProcess(self); env = QProcessEnvironment.systemEnvironment()
        for key, value in p.environment.items(): env.insert(key, value)
        process.setProcessEnvironment(env)
        if p.working_directory: process.setWorkingDirectory(p.working_directory)
        record = self.registry.start(p.id, process)
        process.setProcessChannelMode(QProcess.SeparateChannels)
        process.started.connect(lambda pid=p.id, generation=record.generation, proc=process: self._started(pid, generation, proc))
        process.readyReadStandardOutput.connect(lambda pid=p.id, generation=record.generation, proc=process: self._read_output(pid, generation, proc, True))
        process.readyReadStandardError.connect(lambda pid=p.id, generation=record.generation, proc=process: self._read_output(pid, generation, proc, False))
        process.finished.connect(lambda code, status, pid=p.id, generation=record.generation, proc=process: self._finished(pid, generation, proc, code, status))
        process.errorOccurred.connect(lambda error, pid=p.id, generation=record.generation, proc=process: self._error(pid, generation, proc, error))
        self.registry.append_output(p.id, record.generation, process, stdout=f"$ {command_preview(p)}\n".encode())
        self._refresh_process_view()
        program, args = command_parts(p); process.start(program, args)

    def _started(self, profile_id: str, generation: int, process: QProcess) -> None:
        if self.registry.set_running(profile_id, generation, process, int(process.processId())):
            self._refresh_process_view()

    def _read_output(self, profile_id: str, generation: int, process: QProcess, stdout: bool) -> None:
        data = bytes(process.readAllStandardOutput() if stdout else process.readAllStandardError())
        if not data or not self.registry.is_current(profile_id, generation, process):
            return
        if stdout:
            self.registry.append_output(profile_id, generation, process, stdout=data)
            prefix = "[stdout] "
        else:
            self.registry.append_output(profile_id, generation, process, stderr=data)
            prefix = "[stderr] "
        if profile_id == self.current_id:
            self._append_log(prefix + data.decode("utf-8", errors="replace"))

    def _drain_output(self, record: ProcessExecution) -> None:
        self._read_output(record.profile_id, record.generation, record.process, True)
        self._read_output(record.profile_id, record.generation, record.process, False)

    def _error(self, profile_id: str, generation: int, process: QProcess, _error) -> None:
        terminal_errors = {QProcess.FailedToStart, QProcess.Crashed}
        if _error not in terminal_errors:
            if self.registry.note_error(profile_id, generation, process, process.errorString()):
                self.registry.append_event(profile_id, generation, process, f"[process error: {_error.name}]\n")
            self._refresh_process_view()
            return
        if self.registry.fail(profile_id, generation, process, process.errorString()):
            self._stop_timer(profile_id, generation)
            record = self.registry.get(profile_id)
            if record:
                self.registry.emit_final_message(profile_id, generation, process, f"[finished: failed, exit code unavailable; {_error.name}]\n")
            self._refresh_process_view()

    def _finished(self, profile_id: str, generation: int, process: QProcess, code: int, _status) -> None:
        record = self.registry.get(profile_id)
        if not record or not self.registry.is_current(profile_id, generation, process):
            return
        self._drain_output(record)
        if self.registry.finish(profile_id, generation, process, int(code)):
            self._stop_timer(profile_id, generation)
            self.registry.emit_final_message(profile_id, generation, process, f"[finished: {record.state.value}, exit code {code}]\n")
            self._refresh_process_view()

    def stop_current(self):
        record = self.registry.get(self.current_id) if self.current_id else None
        if not record or not record.active or not self.registry.request_stop(record.profile_id, record.generation, record.process):
            return
        self.terminator.graceful(record.process)
        timer = QTimer(self); timer.setSingleShot(True); timer.timeout.connect(lambda: self._force_stop(record.profile_id, record.generation, record.process)); self.stop_timers[(record.profile_id, record.generation)] = timer; timer.start(2000)
        self.statusBar().showMessage("Termination requested", 3000); self._refresh_process_view()

    def _force_stop(self, profile_id: str, generation: int, process: QProcess) -> None:
        record = self.registry.get(profile_id)
        if record and record.active and self.registry.request_force_kill(profile_id, generation, process):
            self.terminator.force(process)

    def _stop_timer(self, profile_id: str, generation: int) -> None:
        timer = self.stop_timers.pop((profile_id, generation), None)
        if timer:
            timer.stop(); timer.deleteLater()

    def _append_log(self, text: str) -> None:
        if text and self.current_id:
            self.log.insertPlainText(text)
            self.log.ensureCursorVisible()

    def _refresh_process_view(self) -> None:
        record = self.registry.get(self.current_id) if self.current_id else None
        state = record.state if record else ProcessState.STOPPED
        details = [f"State: {state.value}"]
        if record and record.pid: details.append(f"PID: {record.pid}")
        if record and record.started_at: details.append(f"Started: {record.started_at.isoformat()}")
        if record and record.finished_at: details.append(f"Finished: {record.finished_at.isoformat()}")
        if record and record.exit_code is not None: details.append(f"Exit code: {record.exit_code}")
        if record and record.error_message: details.append(f"Error: {record.error_message}")
        self.process_info.setText(" | ".join(details))
        self.run_button.setEnabled(state in {ProcessState.STOPPED, ProcessState.EXITED, ProcessState.FAILED})
        self.stop_button.setEnabled(state in {ProcessState.STARTING, ProcessState.RUNNING, ProcessState.STOPPING})
        self.clear_log_button.setEnabled(record is not None)
        if record:
            self.log.setPlainText(f"[launcher]\n{record.event_log.text()}[stdout]\n{record.stdout_log.text()}\n[stderr]\n{record.stderr_log.text()}")
        else:
            self.log.clear()
        self.statusBar().showMessage("Running" if state in {ProcessState.STARTING, ProcessState.RUNNING, ProcessState.STOPPING} else "Ready")

    def clear_current_log(self):
        if self.current_id and self.registry.clear_logs(self.current_id):
            self._refresh_process_view()

    def _process_state(self):
        self._refresh_process_view()

    def closeEvent(self, event):
        active = [record for record in self.registry.records() if record.active]
        if not active:
            if not self._closing_started:
                self._closing_started = True
                self.discovery_runner.shutdown()
            event.accept(); return
        if not self.close_requested:
            answer = QMessageBox.question(self, "Processes running", "Stop all running processes before closing?", QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if answer != QMessageBox.Yes:
                event.ignore(); return
            self.close_requested = True
            self._closing_started = True
            self.discovery_runner.shutdown()
            for record in active:
                if self.registry.request_stop(record.profile_id, record.generation, record.process):
                    self.terminator.graceful(record.process)
            self.close_deadline = time.monotonic() + 2.5
            self.close_force_deadline = None
            self.close_timer = QTimer(self); self.close_timer.timeout.connect(self._finish_close); self.close_timer.start(100)
            event.ignore(); return
        event.ignore()

    def _finish_close(self):
        active = [record for record in self.registry.records() if record.active]
        if active:
            now = time.monotonic()
            if self.close_force_deadline is not None and now >= self.close_force_deadline:
                for record in active:
                    if self.registry.request_force_kill(record.profile_id, record.generation, record.process):
                        self.terminator.force(record.process)
                    if self.registry.fail(record.profile_id, record.generation, record.process, "close deadline exceeded"):
                        self._stop_timer(record.profile_id, record.generation)
                        self.registry.emit_final_message(record.profile_id, record.generation, record.process, "[finished: failed, exit code unavailable; close deadline exceeded]\n")
                return
            if self.close_deadline is not None and now >= self.close_deadline:
                for record in active:
                    if self.registry.request_force_kill(record.profile_id, record.generation, record.process):
                        self.terminator.force(record.process)
                self.close_force_deadline = now + 1.0
            return
        if self.close_timer:
            self.close_timer.stop(); self.close_timer.deleteLater(); self.close_timer = None
        self.close_deadline = None
        self.close_force_deadline = None
        self.close()

    def export_profiles(self):
        path, _ = QFileDialog.getSaveFileName(self, "Export profiles", "pipewire-profiles.json", "JSON (*.json)")
        if path:
            try: ProfileStore(__import__('pathlib').Path(path)).save(self.profiles)
            except Exception as exc: QMessageBox.critical(self, "Export failed", str(exc))

    def import_profiles(self):
        path, _ = QFileDialog.getOpenFileName(self, "Import profiles", "", "JSON (*.json)")
        if not path: return
        try:
            incoming = ProfileStore(__import__('pathlib').Path(path)).load(); known = {p.id for p in self.profiles}; self.profiles.extend(p for p in incoming if p.id not in known); self.store.save(self.profiles); self.refresh_list(); self.statusBar().showMessage(f"Imported {len(incoming)} profiles", 3000)
        except Exception as exc: QMessageBox.critical(self, "Import failed", str(exc))


def main():
    app = QApplication(sys.argv); app.setApplicationName("PipeWire App Launcher"); app.setOrganizationName("R. Brothers Studio"); window = MainWindow(); window.show(); return app.exec()


if __name__ == "__main__": raise SystemExit(main())
