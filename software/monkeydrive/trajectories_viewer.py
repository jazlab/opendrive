"""One window for exploration, overview, and lazily created drive views."""
import tkinter as tk
from tkinter import ttk

from .anatomy import AnatomyLoader, AnatomyOrientation
from .console_theme import ConsoleTheme as Theme
from .explorer_view import ExplorerView
from .models import MotorState, TrajectoryState
from .trajectory_viewer import TrajectoryPanel
from .trajectory_summary_viewer import TrajectorySummaryPanel
from .viewer_tasks import LatestWorker
from .viewer_theme import configure_viewer, style_viewer, viewer_heading


class TrajectoriesViewer(tk.Toplevel):
    def __init__(self, parent, motor_states, anatomy_path, orientation_actual='', orientation_scanner='',
                 explorer_state=None, revisions=None, on_save=None, on_explorer_change=None, on_assign=None):
        super().__init__(parent)
        configure_viewer(self)
        self._states = {state.motor_id: MotorState.from_dict(state.to_dict()) for state in motor_states}
        self._revisions = {int(key): int(value) for key, value in (revisions or {}).items()}
        self._on_save = on_save
        self._on_explorer_change = on_explorer_change
        self._on_assign = on_assign
        self._anatomy_path = anatomy_path
        self._orientation_actual_key = orientation_actual
        self._orientation_scanner_key = orientation_scanner
        self._original_volume = self._volume = None
        self._render_worker = LatestWorker(self)
        self._contrast_low_var = tk.DoubleVar(self, value=1.0)
        self._contrast_high_var = tk.DoubleVar(self, value=99.0)
        self._status_var = tk.StringVar(self, value='Loading anatomy')
        self._panels = {}
        self._pages = {}
        self._buttons = {}
        self._selected = None
        self.title('Trajectories')
        self.minsize(1200, 760)
        self.geometry(f'{min(1700, max(1200, self.winfo_screenwidth() - 80))}x{min(900, max(760, self.winfo_screenheight() - 100))}')
        main = tk.Frame(self, padx=20, pady=18)
        main.pack(fill=tk.BOTH, expand=True)
        viewer_heading(main, 'Trajectories', 'Explore anatomy, compare trajectories, and plan each drive.')
        navigation = tk.Frame(main)
        navigation.pack(fill=tk.X, pady=(0, 12))
        style = ttk.Style(self)
        style.configure('Trajectories.TNotebook', background=Theme.COLOR_BG, borderwidth=0)
        style.layout('Trajectories.TNotebook.Tab', [])
        self._notebook = ttk.Notebook(main, style='Trajectories.TNotebook')
        self._notebook.pack(fill=tk.BOTH, expand=True)
        self._explorer = ExplorerView(self._notebook, self, explorer_state, self._save_explorer,
                                      sorted(self._states), self._request_assignment)
        self._pages['explorer'] = self._explorer
        self._notebook.add(self._explorer, text='Explorer')
        self._pages['summary'] = tk.Frame(self._notebook)
        self._notebook.add(self._pages['summary'], text='Summary')
        for key, label in (('explorer', 'Explorer'), ('summary', 'Summary')):
            self._add_button(navigation, key, label)
        ttk.Separator(navigation, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=18)
        self._drive_navigation = tk.Frame(navigation)
        self._drive_navigation.pack(side=tk.LEFT)
        self._sync_drive_pages()
        tk.Label(main, textvariable=self._status_var, anchor='w').pack(fill=tk.X, pady=(8, 0))
        self._notebook.bind('<<NotebookTabChanged>>', self._on_tab_change)
        style_viewer(self)
        self._on_tab_change()
        self.after(0, self._load_anatomy)

    def _add_button(self, parent, key, label):
        button = ttk.Button(parent, text=label, width=9, style='Viewer.TButton', command=lambda: self.select_tab(key))
        button.pack(side=tk.LEFT)
        self._buttons[key] = button

    def select_tab(self, key):
        if key in self._pages:
            self._notebook.select(self._pages[key])
            self._on_tab_change()

    def _sync_drive_pages(self):
        for key in list(self._pages):
            if isinstance(key, int) and key not in self._states:
                self._notebook.forget(self._pages[key])
                self._pages.pop(key).destroy()
                self._panels.pop(key, None)
                self._buttons.pop(key).destroy()
        for motor_id in sorted(self._states):
            if motor_id not in self._pages:
                self._pages[motor_id] = tk.Frame(self._notebook)
                self._notebook.add(self._pages[motor_id], text=f'Drive {motor_id}')
                self._add_button(self._drive_navigation, motor_id, f'Drive {motor_id}')
        self._explorer.set_drive_ids(sorted(self._states))

    def _current_panel(self):
        return self._explorer if self._selected == 'explorer' else self._panels.get(self._selected)

    def _suspend(self, panel):
        if panel is not None:
            panel.set_active(False)

    def _on_tab_change(self, _event=None):
        selected = self._notebook.select()
        key = next((key for key, page in self._pages.items() if str(page) == selected), None)
        if key is None or key == self._selected:
            return
        self._suspend(self._current_panel())
        self._selected = key
        for name, button in self._buttons.items():
            button.configure(style='Selected.Viewer.TButton' if name == key else 'Viewer.TButton')
        if key != 'explorer' and key not in self._panels:
            if key == 'summary':
                panel = TrajectorySummaryPanel(self._pages[key], list(self._states.values()), self._anatomy_path,
                                               self._orientation_actual_key, self._orientation_scanner_key, managed=True)
            else:
                state = self._states[key]
                panel = TrajectoryPanel(self._pages[key], key, state.trajectory, state.absolute, self._anatomy_path,
                                        self._orientation_actual_key, self._orientation_scanner_key, self._save_drive, managed=True)
            self._panels[key] = panel
            panel.pack(fill=tk.BOTH, expand=True)
            if self._volume is not None:
                panel.set_volume(self._volume, self._orientation_actual_key, self._orientation_scanner_key)
        self._activate_current()

    def _activate_current(self):
        panel = self._current_panel()
        if self._volume is None:
            return
        if panel is not None:
            panel.set_active(True)

    def _load_anatomy(self):
        self._render_worker.invalidate()
        self._suspend(self._current_panel())
        self._volume = None
        self._status_var.set('Loading anatomy')
        if not self._anatomy_path:
            self._status_var.set('Load an MRI in Anatomy to begin exploring')
            return
        source, path = self._original_volume, self._anatomy_path
        actual, scanner = self._orientation_actual_key, self._orientation_scanner_key
        def compute():
            original = source if source is not None else AnatomyLoader.load(path)
            volume = AnatomyOrientation.correct_volume(original, actual, scanner) if actual and scanner else original
            return original, volume
        def complete(result, error):
            if error is not None:
                self._status_var.set(f'Could not load anatomy: {error}')
                return
            previous = self._explorer._volume
            self._original_volume, self._volume = result
            self._explorer.set_volume(self._volume, actual, scanner,
                                      reset=previous is not None and previous.source_path != self._volume.source_path)
            for panel in self._panels.values():
                panel.set_volume(self._volume, actual, scanner)
            self._status_var.set(self._volume.display_name)
            self._activate_current()
        self._render_worker.submit('load', compute, complete)

    def update_anatomy(self, path, actual='', scanner=''):
        if path != self._anatomy_path:
            self._original_volume = None
        self._anatomy_path = path
        self._orientation_actual_key, self._orientation_scanner_key = actual, scanner
        self._load_anatomy()

    def update_motor_position(self, motor_id, position):
        if motor_id not in self._states:
            return
        self._states[motor_id].absolute = float(position)
        if motor_id in self._panels:
            self._panels[motor_id].update_motor_position(position)
        if 'summary' in self._panels:
            self._panels['summary'].update_motor_position(motor_id, position)

    def update_motor_state(self, state, revision=None):
        if state.motor_id not in self._states:
            return
        if revision is not None and revision < self._revisions.get(state.motor_id, 0):
            return
        previous = self._states[state.motor_id]
        changed = previous.trajectory.to_dict() != state.trajectory.to_dict()
        self._states[state.motor_id] = MotorState.from_dict(state.to_dict())
        if revision is not None:
            self._revisions[state.motor_id] = revision
        panel = self._panels.get(state.motor_id)
        if panel:
            if changed:
                panel.update_trajectory(state.trajectory)
            panel.update_motor_position(state.absolute)
        if 'summary' in self._panels:
            self._panels['summary'].update_motor_state(state)

    def set_motor_states(self, states, revisions=None):
        changed = set(self._states) != {state.motor_id for state in states}
        for state in states:
            if state.motor_id in self._states:
                revision = (revisions or {}).get(str(state.motor_id))
                self.update_motor_state(state, revision)
        self._states = {state.motor_id: MotorState.from_dict(state.to_dict()) for state in states}
        self._revisions.update({int(key): int(value) for key, value in (revisions or {}).items()})
        if changed:
            self._sync_drive_pages()
            if 'summary' in self._panels:
                self._panels['summary'].set_motor_states(states)
            self._on_tab_change()

    def apply_trajectory(self, motor_id, trajectory, revision, select=False):
        if motor_id not in self._states:
            return
        state = MotorState.from_dict(self._states[motor_id].to_dict())
        state.trajectory = trajectory
        self.update_motor_state(state, revision)
        if select:
            self.select_tab(motor_id)

    def _save_drive(self, motor_id, trajectory):
        self._states[motor_id].trajectory = TrajectoryState.from_dict(trajectory.to_dict())
        if 'summary' in self._panels:
            self._panels['summary'].update_motor_state(self._states[motor_id])
        if self._on_save:
            self._on_save(motor_id, trajectory, self._revisions.get(motor_id, 0))

    def _save_explorer(self, state):
        if self._on_explorer_change:
            self._on_explorer_change(state)

    def _request_assignment(self, motor_id, state):
        if self._on_assign:
            self._on_assign(motor_id, state)

    def destroy(self):
        self._render_worker.close()
        super().destroy()
