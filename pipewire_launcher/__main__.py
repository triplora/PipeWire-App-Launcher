from __future__ import annotations

import sys

from PySide6.QtCore import QProcess, QProcessEnvironment, Qt
from PySide6.QtGui import QAction, QFontDatabase
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QFileDialog, QFormLayout, QHBoxLayout, QLabel,
    QLineEdit, QListWidget, QMainWindow, QMessageBox, QPlainTextEdit,
    QPushButton, QSplitter, QStatusBar, QToolBar, QVBoxLayout, QWidget,
)

from pipewire_launcher.core import Profile, ProfileStore, command_parts, command_preview, parse_arguments, parse_environment, validate_profile


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("PipeWire App Launcher")
        self.resize(1080, 680)
        self.store = ProfileStore()
        self.profiles: list[Profile] = []
        self.processes: dict[str, QProcess] = {}
        self.current_id: str | None = None
        self._build_ui()
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
        buttons = QHBoxLayout(); buttons.addWidget(self.run_button); buttons.addWidget(self.stop_button); buttons.addStretch()
        self.log = QPlainTextEdit(); self.log.setReadOnly(True); self.log.setFont(QFontDatabase.systemFont(QFontDatabase.FixedFont))
        right = QWidget(); rv = QVBoxLayout(right); rv.addLayout(form); rv.addLayout(buttons); rv.addWidget(QLabel("Process output")); rv.addWidget(self.log, 1)
        split = QSplitter(); split.addWidget(left); split.addWidget(right); split.setSizes([280, 800]); self.setCentralWidget(split)
        self.setStatusBar(QStatusBar()); self.statusBar().showMessage("Ready")

    def _load(self):
        try: self.profiles = self.store.load()
        except Exception as exc: QMessageBox.warning(self, "Profiles", f"Could not load profiles:\n{exc}")
        self.refresh_list();
        if not self.profiles: self.new_profile()

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
        self.name.setText(p.name); self.executable.setText(p.executable); self.arguments.setText(' '.join(__import__('shlex').quote(x) for x in p.arguments)); self.cwd.setText(p.working_directory); self.environment.setPlainText('\n'.join(f"{k}={v}" for k,v in p.environment.items())); self.notes.setPlainText(p.notes); self.enabled.setChecked(p.enabled); self.update_preview(); self._process_state()

    def profile_from_form(self):
        return Profile(name=self.name.text().strip(), executable=self.executable.text().strip(), arguments=parse_arguments(self.arguments.text()), working_directory=self.cwd.text().strip(), environment=parse_environment(self.environment.toPlainText()), notes=self.notes.toPlainText().strip(), enabled=self.enabled.isChecked(), id=self.current_id or __import__('uuid').uuid4().hex)

    def new_profile(self):
        p = Profile("New application", ""); self.profiles.append(p); self.current_id = p.id; self.refresh_list()
        for i in range(self.list.count()):
            if self.list.item(i).data(Qt.UserRole) == p.id: self.list.setCurrentRow(i); break

    def save_current(self):
        try: p = self.profile_from_form()
        except ValueError as exc: QMessageBox.warning(self, "Invalid profile", str(exc)); return
        idx = next((i for i,x in enumerate(self.profiles) if x.id == p.id), None)
        if idx is None: self.profiles.append(p)
        else: self.profiles[idx] = p
        try: self.store.save(self.profiles)
        except Exception as exc: QMessageBox.critical(self, "Save failed", str(exc)); return
        self.current_id = p.id; self.refresh_list(); self.statusBar().showMessage("Profile saved", 3000)

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
        self.save_current(); process = QProcess(self); env = QProcessEnvironment.systemEnvironment()
        for key, value in p.environment.items(): env.insert(key, value)
        process.setProcessEnvironment(env)
        if p.working_directory: process.setWorkingDirectory(p.working_directory)
        process.setProcessChannelMode(QProcess.MergedChannels); process.readyReadStandardOutput.connect(lambda pid=p.id: self.read_output(pid)); process.finished.connect(lambda code,status,pid=p.id: self.finished(pid,code,status)); process.errorOccurred.connect(lambda error,pid=p.id: self.log.appendPlainText(f"[error] {self.processes[pid].errorString()}"))
        program, args = command_parts(p); self.processes[p.id] = process; self.log.appendPlainText(f"$ {command_preview(p)}"); process.start(program, args); self._process_state()

    def read_output(self, profile_id):
        process = self.processes.get(profile_id)
        if process: self.log.appendPlainText(bytes(process.readAllStandardOutput()).decode(errors="replace").rstrip())

    def finished(self, profile_id, code, _status):
        self.log.appendPlainText(f"[finished: exit code {code}]"); self.processes.pop(profile_id, None); self._process_state()

    def stop_current(self):
        process = self.processes.get(self.current_id)
        if process: process.terminate(); self.statusBar().showMessage("Termination requested", 3000)

    def _process_state(self):
        running = self.current_id in self.processes and self.processes[self.current_id].state() != QProcess.NotRunning
        self.run_button.setEnabled(not running); self.stop_button.setEnabled(running); self.statusBar().showMessage("Running" if running else "Ready")

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
