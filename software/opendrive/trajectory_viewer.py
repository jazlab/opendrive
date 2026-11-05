"""Trajectory planning viewer for motor-specific MRI views."""

from __future__ import annotations

import io
import math
import queue
import threading
import time
from typing import Callable

import tkinter as tk
from tkinter import messagebox, ttk
import numpy as np
from PIL import Image, ImageTk

from .anatomy import (
    AnatomyLoader,
    AnatomyOrientation,
    AnatomySliceRenderer,
    AnatomyVolume,
)
from .anatomy_viewer import ContrastWindowControl
from .config import Config
from .console_theme import ConsoleTheme as Theme
from .viewer_theme import configure_viewer, style_viewer, viewer_button, viewer_heading, viewer_zoom
from .viewer_tasks import LatestWorker
from .models import TrajectoryState
from .probe_map_colors import color_from_lut, load_shank_lut
from .probe_map_smoothing import SpatialActivitySmoother
from .probe_overlay import ProbeOverlayRenderer, probe_body_points, contact_activity_image
from .spikeglx_client import SpikeGLXConfig, SpikeGLXLiveClient, SpikeGLXUnavailable


class _TrajectoryView:
    """Motor-specific trajectory viewer."""

    PLANES = (
        ("Inline", 0, "ap_rotation"),
        ("Inline 90", 1, "ml_rotation"),
        ("Perpendicular", 2, "twist_rotation"),
    )

    def __init__(
        self,
        parent: tk.Tk,
        motor_id: int,
        trajectory: TrajectoryState,
        motor_position: float,
        anatomy_path: str,
        orientation_actual: str,
        orientation_scanner: str,
        on_save: Callable[[int, TrajectoryState], None],
        managed: bool = False,
    ):
        """Initialize a trajectory viewer."""
        super().__init__(parent)
        configure_viewer(self)
        self._active = not managed

        self._motor_id = motor_id
        self._trajectory = TrajectoryState.from_dict(trajectory.to_dict())
        self._motor_position = float(motor_position)
        self._anatomy_path = anatomy_path
        self._orientation_actual_key = orientation_actual
        self._orientation_scanner_key = orientation_scanner
        self._on_save = on_save
        self._original_volume = None
        self._volume: AnatomyVolume | None = None
        self._render_worker = LatestWorker(self)
        self._render_signatures = {}
        self._source_signatures = {}

        self._image_refs: dict[int, tk.PhotoImage] = {}
        self._source_images: dict[int, Image.Image] = {}
        self._image_items: dict[int, int] = {}
        self._canvases: dict[int, tk.Canvas] = {}
        self._pane_frames: dict[int, tk.Frame] = {}
        self._pane_bodies: dict[int, tk.Frame] = {}
        self._pane_buttons: dict[int, tk.Button] = {}
        self._pane_titles: dict[int, tk.Label] = {}
        self._pane_header_extras: dict[int, tk.Frame] = {}
        self._pane_columns: dict[int, int] = {}
        self._pane_collapsed: dict[int, bool] = {}
        self._image_row: tk.Frame | None = None
        self._contrast_low_var = tk.DoubleVar(value=1.0)
        self._contrast_high_var = tk.DoubleVar(value=99.0)
        self._zoom_var = tk.DoubleVar(value=self._trajectory.zoom)
        self._status_var = tk.StringVar(value="No anatomy loaded")
        self._probe_map_status_var = tk.StringVar(value="Probe map off")
        self._field_warning_active = False
        self._live_update_job = None
        self._probe_map_events: queue.Queue = queue.Queue()
        self._probe_map_stop_event = threading.Event()
        self._probe_map_worker_running = False
        self._probe_map_generation = 0
        self._probe_map_values_mv: list[float] = []
        self._probe_map_depths_mm: list[float] = []
        self._probe_map_x_mm: list[float] = []
        self._probe_map_shanks: list[int] = []
        self._probe_map_update_count = 0
        self._probe_map_lut = load_shank_lut()
        self._probe_overlay_renderer = ProbeOverlayRenderer(self._probe_map_lut)
        self._probe_smoothing_geometry = None
        self._probe_smoother = None
        self._probe_smoothed_key = None
        self._probe_smoothed_profiles = []
        self._probe_map_channel_pitch_mm = 0.01
        self._probe_map_items: dict[int, int] = {}
        self._probe_map_image_refs = {}
        self._probe_map_layout_keys = {}
        self._probe_map_palette_key = None
        self._probe_map_colors = ()
        self._probe_map_worker_settings = None
        self._probe_discovery_worker = None
        self._probe_discovery_pending = False
        self._available_probe_count = None
        self._probe_map_last_status_time = 0.0
        self._probe_map_draw_job = None
        self._zoom_render_job = None

        self._x_var = tk.StringVar(value=f"{self._trajectory.x:g}")
        self._y_var = tk.StringVar(value=f"{self._trajectory.y:g}")
        self._z_var = tk.StringVar(value=f"{self._trajectory.z:g}")
        self._ap_var = tk.StringVar(value=f"{self._trajectory.ap_rotation:g}")
        self._ml_var = tk.StringVar(value=f"{self._trajectory.ml_rotation:g}")
        self._twist_var = tk.StringVar(value=f"{self._trajectory.twist_rotation:g}")
        self._offset_var = tk.StringVar(value=f"{self._trajectory.perpendicular_offset:g}")
        self._probe_map_enabled_var = tk.BooleanVar(value=self._trajectory.probe_map_enabled)
        self._probe_map_probe_var = tk.StringVar(value=f"{self._trajectory.probe_map_probe:g}")
        self._probe_map_probe_label_var = tk.StringVar(value=f'imec{self._trajectory.probe_map_probe}')
        self._probe_map_min_var = tk.StringVar(value=f"{self._trajectory.probe_map_min_mv:g}")
        self._probe_map_max_var = tk.StringVar(value=f"{self._trajectory.probe_map_max_mv:g}")
        self._probe_map_update_ms_var = tk.StringVar(value=f"{self._trajectory.probe_map_update_ms:g}")
        self._probe_map_smooth_var = tk.BooleanVar(value=self._trajectory.probe_map_smooth)
        self._probe_map_smoothing_var = tk.StringVar(value=f'{self._trajectory.probe_map_smoothing_um:g}')
        self._probe_width_var = tk.StringVar(value=str(self._trajectory.probe_width_px))
        self._probe_button: tk.Button | None = None
        self._target_button: tk.Button | None = None
        self._target_editing = False

        if isinstance(self, tk.Toplevel):
            self.title(f"Motor {motor_id} Trajectory")
            self.geometry("1700x860")
            self.minsize(900, 680)

            parent_x = parent.winfo_x()
            parent_y = parent.winfo_y()
            parent_width = parent.winfo_width()
            self.geometry(f"+{parent_x + parent_width + 10}+{parent_y}")

        self._build_ui()
        self._style_widget_tree()
        self._probe_poll_job = self.after(16, self._poll_probe_map_events)
        if not managed:
            self.after(100, self._load_anatomy)
            self.after(150, self._refresh_available_probes)
        if self._active and self._trajectory.probe_map_enabled:
            self.after(250, self._restart_probe_map_worker)

    def set_volume(self, volume, actual='', scanner=''):
        self._render_worker.invalidate()
        self._render_signatures.clear()
        self._source_signatures.clear()
        self._source_images.clear()
        self._volume = volume
        self._orientation_actual_key, self._orientation_scanner_key = actual, scanner
        self._status_var.set(volume.display_name)
        self._render_all()

    def set_active(self, active):
        self._active = active
        if active:
            self._refresh_available_probes()
            self.update_motor_position(self._motor_position)
            self._render_all()
            if self._trajectory.probe_map_enabled:
                self._restart_probe_map_worker()
        else:
            self._render_worker.invalidate()
            self._render_signatures.clear()
            self._stop_probe_map_worker()
            self._cancel_probe_map_overlay_draw()

    def destroy(self):
        """Stop live probe-map work before destroying this window."""
        if self._probe_discovery_worker:
            self._probe_discovery_worker.close()
        if getattr(self, '_probe_poll_job', None) is not None:
            self.after_cancel(self._probe_poll_job)
            self._probe_poll_job = None
        if self._live_update_job is not None:
            self.after_cancel(self._live_update_job)
            self._live_update_job = None
        self._render_worker.close()
        self._stop_probe_map_worker()
        self._cancel_probe_map_overlay_draw()
        self._cancel_zoom_render()
        try:
            super().destroy()
        except tk.TclError:
            pass

    def _build_ui(self):
        container = tk.Frame(self, padx=20, pady=18)
        container.pack(fill=tk.BOTH, expand=True)
        viewer_heading(container, f"Drive {self._motor_id} trajectory", 'Align the probe and target across three anatomical planes.')

        toolbar = tk.Frame(container)
        toolbar.pack(fill=tk.X, pady=(0, 16))

        tk.Label(toolbar, text="Zoom:").pack(side=tk.LEFT, padx=(0, 6))
        viewer_button(toolbar, text="-", command=lambda: self._adjust_zoom(-0.1), width=3).pack(side=tk.LEFT)
        viewer_zoom(toolbar, self._zoom_var, self._on_zoom_change,
                    maximum=10.0, length=220).pack(side=tk.LEFT, padx=4)
        viewer_button(toolbar, text="+", command=lambda: self._adjust_zoom(0.1), width=3).pack(side=tk.LEFT)
        tk.Label(toolbar, textvariable=self._zoom_var, width=4, anchor="center").pack(side=tk.LEFT, padx=(6, 0))
        viewer_button(toolbar, text="Save", command=self._save_fields, width=8).pack(side=tk.LEFT, padx=(18, 0))
        self._probe_button = viewer_button(
            toolbar, text=self._probe_button_text(),
            command=self._toggle_probe_location
        )
        self._probe_button.pack(side=tk.LEFT, padx=(6, 0))
        self._target_button = viewer_button(
            toolbar, text=self._target_button_text(),
            command=self._toggle_target_location
        )
        self._target_button.pack(side=tk.LEFT, padx=(6, 0))

        viewer_frame = tk.Frame(container)
        viewer_frame.pack(fill=tk.BOTH, expand=True)

        contrast_frame = tk.Frame(viewer_frame, width=132)
        contrast_frame.pack_propagate(False)
        contrast_frame.pack(side=tk.LEFT, fill=tk.Y, padx=(0, 8))
        tk.Label(contrast_frame, text="Contrast").pack(anchor='n')
        ContrastWindowControl(
            contrast_frame,
            low_var=self._contrast_low_var,
            high_var=self._contrast_high_var,
            command=self._render_all
        ).pack(anchor='n')

        parameter_frame = tk.Frame(contrast_frame)
        parameter_frame.pack(anchor='n', pady=(14, 0), fill=tk.X)
        tk.Label(parameter_frame, text="Trajectory").pack(anchor='w')
        self._add_field(parameter_frame, "X (mm):", self._x_var)
        self._add_field(parameter_frame, "Y (mm):", self._y_var)
        self._add_field(parameter_frame, "Z (mm):", self._z_var)
        self._add_field(parameter_frame, "A-P rot:", self._ap_var)
        self._add_field(parameter_frame, "M-L rot:", self._ml_var)
        self._add_field(parameter_frame, "Twist:", self._twist_var)

        image_row = tk.Frame(viewer_frame)
        self._image_row = image_row
        image_row.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        probe_map_frame = tk.Frame(viewer_frame, padx=8, width=190)
        probe_map_frame.pack_propagate(False)
        probe_map_frame.pack(side=tk.RIGHT, fill=tk.Y, padx=(8, 0))
        tk.Label(probe_map_frame, text="SpikeGLX Map").pack(anchor='w')
        tk.Label(probe_map_frame, text='AP peak-to-peak (mV)\n1 mV = 1000 µV', justify='left').pack(anchor='w', pady=(4, 6))
        tk.Checkbutton(
            probe_map_frame,
            text="Show on probe",
            variable=self._probe_map_enabled_var,
            command=self._on_probe_map_settings_change,
        ).pack(anchor='w')
        tk.Label(probe_map_frame, text='Probe:', anchor='w').pack(fill=tk.X)
        self._probe_map_combo = ttk.Combobox(probe_map_frame, textvariable=self._probe_map_probe_label_var,
            values=(), state='readonly', width=12, postcommand=self._refresh_available_probes)
        self._probe_map_combo.pack(fill=tk.X, pady=(0, 4))
        self._probe_map_combo.bind('<<ComboboxSelected>>', self._select_probe_map_probe)
        self._add_probe_map_field(probe_map_frame, "Min mV:", self._probe_map_min_var, restart=False)
        self._add_probe_map_field(probe_map_frame, "Max mV:", self._probe_map_max_var, restart=False)
        self._add_probe_map_field(probe_map_frame, "Update ms (100 ms steps):", self._probe_map_update_ms_var, restart=True)
        width_row = tk.Frame(probe_map_frame)
        width_row.pack(fill=tk.X, pady=2)
        tk.Label(width_row, text='Probe width (pixels):', anchor='w').pack(fill=tk.X)
        self._probe_width_spinbox = ttk.Spinbox(width_row, textvariable=self._probe_width_var,
            from_=6, to=256, increment=1, width=9, command=self._on_probe_map_settings_change)
        self._probe_width_spinbox.pack(fill=tk.X)
        for event in ('<Return>', '<KP_Enter>', '<FocusOut>'):
            self._probe_width_spinbox.bind(event, self._commit_probe_map_redraw_from_event)
        tk.Checkbutton(probe_map_frame, text='Smooth activity', variable=self._probe_map_smooth_var,
                       command=self._on_probe_map_settings_change).pack(anchor='w', pady=(6, 0))
        self._add_probe_map_field(probe_map_frame, 'Smoothing distance (µm):', self._probe_map_smoothing_var, restart=False)
        tk.Label(probe_map_frame, text='Larger values blend activity\nover a greater distance.',
                 justify=tk.LEFT, anchor='w').pack(fill=tk.X, pady=(0, 4))
        viewer_button(
            probe_map_frame,
            text="Retry map",
            command=self._restart_probe_map_worker,
            width=10,
        ).pack(anchor='w', pady=(4, 0))
        tk.Label(
            probe_map_frame,
            textvariable=self._probe_map_status_var,
            anchor='w',
            justify=tk.LEFT,
            wraplength=166,
            width=20,
        ).pack(fill=tk.X, pady=(8, 0))

        for column, (label, axis, _rotation_attr) in enumerate(self.PLANES):
            pane = tk.Frame(image_row, relief=tk.SUNKEN, borderwidth=1, padx=6, pady=6)
            pane.grid(row=0, column=column, sticky='nsew', padx=4)
            image_row.grid_columnconfigure(column, weight=1, uniform="trajectory")
            image_row.grid_rowconfigure(0, weight=1)
            self._pane_frames[axis] = pane
            self._pane_columns[axis] = column
            self._pane_collapsed[axis] = False

            header = tk.Frame(pane)
            header.pack(fill=tk.X)
            collapse_button = viewer_button(
                header, text="<", width=2,
                command=lambda plane_axis=axis: self._toggle_pane(plane_axis)
            )
            collapse_button.pack(side=tk.LEFT)
            self._pane_buttons[axis] = collapse_button
            title_label = tk.Label(header, text=label, font=('TkDefaultFont', 11, 'bold'))
            title_label.pack(side=tk.LEFT, anchor='w', padx=(4, 0))
            self._pane_titles[axis] = title_label
            extra_frame = tk.Frame(header)
            extra_frame.pack(side=tk.LEFT)
            self._pane_header_extras[axis] = extra_frame
            if axis == 2:
                tk.Label(extra_frame, text="Perp (mm):").pack(side=tk.LEFT, padx=(16, 4))
                entry = tk.Entry(extra_frame, textvariable=self._offset_var, width=8)
                entry.pack(side=tk.LEFT, padx=(0, 6))
                entry.bind('<Return>', self._commit_fields_from_event)
                entry.bind('<KP_Enter>', self._commit_fields_from_event)
                entry.bind('<FocusOut>', self._commit_fields_from_event)
            body = tk.Frame(pane)
            body.pack(fill=tk.BOTH, expand=True)
            self._pane_bodies[axis] = body
            canvas = tk.Canvas(
                body, width=340, height=420, background="#000000",
                highlightthickness=0
            )
            canvas.pack(fill=tk.BOTH, expand=True, pady=(4, 4))
            canvas.create_text(170, 210, text="Loading anatomy", fill="#ffffff", tags=("placeholder",))
            canvas.bind("<Configure>", lambda _event, plane_axis=axis: self._render_plane(plane_axis))
            if axis == 2:
                self._bind_perpendicular_scroll(canvas)
            else:
                self._bind_target_scroll(canvas)
            self._canvases[axis] = canvas

        tk.Label(container, textvariable=self._status_var, anchor='w').pack(fill=tk.X, pady=(10, 0))

    def _toggle_pane(self, axis: int):
        collapsed = not self._pane_collapsed.get(axis, False)
        self._pane_collapsed[axis] = collapsed

        body = self._pane_bodies.get(axis)
        button = self._pane_buttons.get(axis)
        title = self._pane_titles.get(axis)
        extra = self._pane_header_extras.get(axis)
        pane = self._pane_frames.get(axis)
        if body:
            if collapsed:
                body.pack_forget()
            else:
                body.pack(fill=tk.BOTH, expand=True)
        if button:
            button.configure(text=">" if collapsed else "<")
        if title:
            title.configure(text="" if collapsed else self._pane_label(axis))
        if extra:
            if collapsed:
                extra.pack_forget()
            else:
                extra.pack(side=tk.LEFT)
        if pane:
            pane.configure(padx=2 if collapsed else 6, pady=6)

        self._update_pane_column_weights()
        if not collapsed:
            self._render_plane(axis)

    def _pane_label(self, axis: int) -> str:
        for label, plane_axis, _rotation_attr in self.PLANES:
            if plane_axis == axis:
                return label
        return ""

    def _update_pane_column_weights(self):
        if not self._image_row:
            return

        for _label, axis, _rotation_attr in self.PLANES:
            column = self._pane_columns[axis]
            if self._pane_collapsed.get(axis, False):
                self._image_row.grid_columnconfigure(column, weight=0, minsize=34, uniform="")
            else:
                self._image_row.grid_columnconfigure(column, weight=1, minsize=260, uniform="trajectory")

    def _add_field(self, parent: tk.Frame, label: str, variable: tk.StringVar):
        row = tk.Frame(parent)
        row.pack(fill=tk.X, pady=2)
        tk.Label(row, text=label, anchor='w').pack(fill=tk.X)
        entry = tk.Entry(row, textvariable=variable, width=9)
        entry.pack(fill=tk.X)
        entry.bind('<Return>', self._commit_fields_from_event)
        entry.bind('<KP_Enter>', self._commit_fields_from_event)
        entry.bind('<FocusOut>', self._commit_fields_from_event)

    def _add_probe_map_field(self, parent: tk.Frame, label: str, variable: tk.StringVar, restart: bool):
        row = tk.Frame(parent)
        row.pack(fill=tk.X, pady=2)
        tk.Label(row, text=label, anchor='w').pack(fill=tk.X)
        entry = tk.Entry(row, textvariable=variable, width=9)
        entry.pack(fill=tk.X)
        handler = (
            self._commit_probe_map_restart_from_event
            if restart else self._commit_probe_map_redraw_from_event
        )
        entry.bind('<Return>', handler)
        entry.bind('<KP_Enter>', handler)
        entry.bind('<FocusOut>', handler)

    def _commit_fields_from_event(self, _event=None):
        self._apply_fields(show_warning=True)
        return "break"

    def _commit_probe_map_restart_from_event(self, _event=None):
        self._on_probe_map_settings_change()
        return "break"

    def _select_probe_map_probe(self, _event=None):
        label = self._probe_map_probe_label_var.get()
        if label not in self._probe_map_combo.cget('values'):
            return
        self._probe_map_probe_var.set(label[4:])
        self._on_probe_map_settings_change()

    def _set_available_probes(self, count):
        self._available_probe_count = max(0, int(count))
        self._probe_map_combo.configure(values=tuple(f'imec{index}' for index in range(self._available_probe_count)))

    def _refresh_available_probes(self):
        if not self._active or self._probe_discovery_pending:
            return
        if self._probe_discovery_worker is None:
            self._probe_discovery_worker = LatestWorker(self)
        self._probe_discovery_pending = True
        def discover():
            client = SpikeGLXLiveClient(SpikeGLXConfig(ip=0, include_geometry=False, compute_diagnostics=False))
            try:
                return client.connect().probe_count
            finally:
                client.close()
        def complete(count, error):
            self._probe_discovery_pending = False
            if error is not None:
                if not self._probe_map_worker_running:
                    self._probe_map_status_var.set(f'Could not list SpikeGLX probes: {error}')
                return
            self._set_available_probes(count)
        self._probe_discovery_worker.submit('probes', discover, complete)

    def _commit_probe_map_redraw_from_event(self, _event=None):
        self._on_probe_map_settings_change()
        return "break"

    def _load_anatomy(self):
        if not self._anatomy_path:
            self._status_var.set("No anatomy loaded")
            return
        self._render_worker.invalidate()
        self._render_signatures.clear()
        self._volume = None
        self._status_var.set("Loading anatomy")
        path = self._anatomy_path
        original = self._original_volume
        actual, scanner = self._orientation_actual_key, self._orientation_scanner_key

        def compute():
            source = original if original is not None else AnatomyLoader.load(path)
            volume = AnatomyOrientation.correct_volume(source, actual, scanner) if actual and scanner else source
            return source, volume

        def complete(result, error):
            if error is not None:
                self._status_var.set(f"Could not load anatomy: {error}")
                return
            self._original_volume, self._volume = result
            self._status_var.set(self._volume.display_name)
            self._render_all()

        self._render_worker.submit('load', compute, complete)

    def update_anatomy_orientation(self, actual_key: str, scanner_key: str):
        """Update orientation correction and reload/re-render anatomy."""
        self._orientation_actual_key = actual_key
        self._orientation_scanner_key = scanner_key
        self._load_anatomy()

    def _apply_fields(self, show_warning: bool = True) -> bool:
        try:
            previous_geometry = self._trajectory_render_signature()
            probe_location_set = self._trajectory.probe_location_set
            probe_offset = self._trajectory.probe_offset
            probe_motor_position = self._trajectory.probe_motor_position
            target_location_set = self._trajectory.target_location_set
            target_offset = self._trajectory.target_offset
            probe_map_enabled = self._trajectory.probe_map_enabled
            probe_map_probe = self._trajectory.probe_map_probe
            probe_map_min_mv = self._trajectory.probe_map_min_mv
            probe_map_max_mv = self._trajectory.probe_map_max_mv
            probe_map_update_ms = self._trajectory.probe_map_update_ms
            probe_map_smooth = self._trajectory.probe_map_smooth
            probe_map_smoothing_um = self._trajectory.probe_map_smoothing_um
            probe_width_px = self._trajectory.probe_width_px
            if not all(np.isfinite(float(variable.get())) for variable in (
                    self._x_var, self._y_var, self._z_var, self._ap_var,
                    self._ml_var, self._twist_var, self._offset_var, self._zoom_var)):
                raise ValueError('Trajectory fields must be finite')
            self._trajectory = TrajectoryState(
                x=float(self._x_var.get()),
                y=float(self._y_var.get()),
                z=float(self._z_var.get()),
                ap_rotation=float(self._ap_var.get()),
                ml_rotation=float(self._ml_var.get()),
                twist_rotation=float(self._twist_var.get()),
                perpendicular_offset=float(self._offset_var.get()),
                zoom=float(self._zoom_var.get()),
                probe_location_set=probe_location_set,
                probe_offset=probe_offset,
                probe_motor_position=probe_motor_position,
                target_location_set=target_location_set,
                target_offset=target_offset,
                probe_map_enabled=probe_map_enabled,
                probe_map_probe=probe_map_probe,
                probe_map_min_mv=probe_map_min_mv,
                probe_map_max_mv=probe_map_max_mv,
                probe_map_update_ms=probe_map_update_ms,
                probe_map_smooth=probe_map_smooth,
                probe_map_smoothing_um=probe_map_smoothing_um,
                probe_width_px=probe_width_px,
            )
        except ValueError:
            if show_warning and not self._field_warning_active:
                self._field_warning_active = True
                try:
                    messagebox.showwarning("Invalid Trajectory", "Trajectory fields must be finite numbers.", parent=self)
                finally:
                    self._field_warning_active = False
            return False

        if self._trajectory_render_signature() != previous_geometry:
            self._render_all()
        return True

    def _trajectory_render_signature(self) -> tuple[float, ...]:
        return (
            self._trajectory.x,
            self._trajectory.y,
            self._trajectory.z,
            self._trajectory.ap_rotation,
            self._trajectory.ml_rotation,
            self._trajectory.twist_rotation,
            self._trajectory.perpendicular_offset,
            self._trajectory.zoom,
        )

    def _save_fields(self):
        if self._apply_fields(show_warning=True):
            self._on_save(self._motor_id, self._trajectory)

    def update_trajectory(self, trajectory: TrajectoryState):
        """Apply an externally assigned trajectory without echoing a save."""
        self._trajectory = TrajectoryState.from_dict(trajectory.to_dict())
        for variable, key in ((self._x_var, 'x'), (self._y_var, 'y'), (self._z_var, 'z'),
                              (self._ap_var, 'ap_rotation'), (self._ml_var, 'ml_rotation'),
                              (self._twist_var, 'twist_rotation'), (self._offset_var, 'perpendicular_offset')):
            variable.set(f'{getattr(self._trajectory, key):g}')
        self._zoom_var.set(self._trajectory.zoom)
        self._probe_map_probe_label_var.set(f'imec{self._trajectory.probe_map_probe}')
        for variable, key in ((self._probe_map_enabled_var, 'probe_map_enabled'),
                              (self._probe_map_probe_var, 'probe_map_probe'),
                              (self._probe_map_min_var, 'probe_map_min_mv'),
                              (self._probe_map_max_var, 'probe_map_max_mv'),
                              (self._probe_map_update_ms_var, 'probe_map_update_ms'),
                              (self._probe_map_smooth_var, 'probe_map_smooth'),
                              (self._probe_map_smoothing_var, 'probe_map_smoothing_um'),
                              (self._probe_width_var, 'probe_width_px')):
            value = getattr(self._trajectory, key)
            variable.set(value if isinstance(value, bool) else f'{value:g}')
        self._sync_probe_map_worker()
        self._render_all()

    def _on_probe_map_settings_change(self):
        previous_width = self._trajectory.probe_width_px
        if not self._apply_probe_map_fields(show_warning=True):
            return
        if previous_width != self._trajectory.probe_width_px:
            self._redraw_inline_depth_lines()
        self._on_save(self._motor_id, self._trajectory)
        self._sync_probe_map_worker()

    def _sync_probe_map_worker(self):
        if self._trajectory.probe_map_enabled:
            settings = (self._trajectory.probe_map_probe, self._trajectory.probe_map_update_ms)
            if not self._probe_map_worker_running or settings != self._probe_map_worker_settings:
                self._restart_probe_map_worker()
            self._schedule_probe_map_overlay_draw()
        else:
            self._stop_probe_map_worker()
            self._probe_map_values_mv = []
            self._probe_map_status_var.set("Probe map off")
            self._clear_probe_map_overlay()

    def _restart_probe_map_worker(self):
        if not self._active:
            return
        if not self._apply_probe_map_fields(show_warning=False):
            return
        self._stop_probe_map_worker()
        if not self._trajectory.probe_map_enabled:
            self._probe_map_status_var.set("Probe map off")
            return

        if self._available_probe_count is not None and self._trajectory.probe_map_probe >= self._available_probe_count:
            self._probe_map_status_var.set(f'imec{self._trajectory.probe_map_probe} is unavailable. Choose an available probe.')
            self._refresh_available_probes()
            return

        self._probe_map_generation += 1
        generation = self._probe_map_generation
        stop_event = threading.Event()
        self._probe_map_stop_event = stop_event
        self._probe_map_worker_running = True
        self._probe_map_status_var.set(
            f"Connecting to SpikeGLX filtered AP probe {self._trajectory.probe_map_probe}..."
        )
        update_interval = max(0.1, self._trajectory.probe_map_update_ms / 1000.0)
        self._probe_map_worker_settings = (self._trajectory.probe_map_probe, self._trajectory.probe_map_update_ms)
        config = SpikeGLXConfig(
            ip=self._trajectory.probe_map_probe,
            prefer_filtered=True,
            compute_diagnostics=False,
        )
        thread = threading.Thread(
            target=self._probe_map_worker_loop,
            args=(generation, stop_event, config, update_interval),
            daemon=True,
        )
        thread.start()

    def _apply_probe_map_fields(self, show_warning: bool = True) -> bool:
        try:
            enabled = bool(self._probe_map_enabled_var.get())
            probe = max(0, int(self._probe_map_probe_var.get()))
            minimum = float(self._probe_map_min_var.get())
            maximum = float(self._probe_map_max_var.get())
            interval = max(100, int(float(self._probe_map_update_ms_var.get())) // 100 * 100)
            smooth = bool(self._probe_map_smooth_var.get())
            smoothing_um = float(self._probe_map_smoothing_var.get())
            probe_width = int(self._probe_width_var.get())
            if not 6 <= probe_width <= 256:
                raise ValueError('Probe width must be between 6 and 256 pixels.')
            if not math.isfinite(smoothing_um) or not 10 <= smoothing_um <= 5000:
                raise ValueError('Smoothing width must be between 10 and 5000 µm.')
            if not math.isfinite(minimum) or not math.isfinite(maximum) or maximum <= minimum:
                raise ValueError('Invalid amplitude range')
        except (ValueError, OverflowError):
            if show_warning and not self._field_warning_active:
                self._field_warning_active = True
                try:
                    messagebox.showwarning(
                        "Invalid Probe Map",
                        "Enter finite probe-map settings with maximum greater than minimum, "
                        "smoothing width between 10 and 5000 µm, and probe width between 6 and 256 pixels.",
                        parent=self,
                    )
                finally:
                    self._field_warning_active = False
            return False
        self._trajectory.probe_map_enabled = enabled
        self._trajectory.probe_map_probe = probe
        self._probe_map_probe_label_var.set(f'imec{probe}')
        self._trajectory.probe_map_min_mv = minimum
        self._trajectory.probe_map_max_mv = maximum
        self._trajectory.probe_map_update_ms = interval
        self._trajectory.probe_map_smooth = smooth
        self._trajectory.probe_map_smoothing_um = smoothing_um
        self._trajectory.probe_width_px = probe_width
        self._probe_map_update_ms_var.set(f'{interval:g}')
        return True

    def _stop_probe_map_worker(self):
        self._probe_map_generation += 1
        self._probe_map_stop_event.set()
        self._probe_map_worker_running = False

    def _probe_map_worker_loop(
        self,
        generation: int,
        stop_event: threading.Event,
        config: SpikeGLXConfig,
        update_interval: float,
    ):
        client = None
        try:
            client = SpikeGLXLiveClient(config)
            info = client.connect()
            self._probe_map_events.put(("connected", generation, info.probe_count))
            for snapshot in client.iter_heatmaps(stop_event, round(update_interval * 1000)):
                self._probe_map_events.put(("snapshot", generation, snapshot))
        except SpikeGLXUnavailable as exc:
            self._probe_map_events.put(("error", generation, str(exc)))
        except Exception as exc:
            self._probe_map_events.put(("error", generation, f"Probe map failed: {exc}"))
        finally:
            if client is not None:
                client.close()
            self._probe_map_events.put(("done", generation))

    def _poll_probe_map_events(self):
        latest_snapshot = None
        latest_snapshot_generation = None
        while True:
            try:
                event = self._probe_map_events.get_nowait()
            except queue.Empty:
                break
            if event[1] != self._probe_map_generation:
                continue
            if event[0] == "snapshot":
                latest_snapshot = event
                latest_snapshot_generation = event[1]
                continue
            if event[0] == 'error':
                latest_snapshot = None
            self._handle_probe_map_event(event)
        if latest_snapshot is not None and latest_snapshot_generation == self._probe_map_generation:
            self._handle_probe_map_event(latest_snapshot)
        if self.winfo_exists():
            self._probe_poll_job = self.after(16, self._poll_probe_map_events)

    def _handle_probe_map_event(self, event: tuple):
        kind = event[0]
        generation = event[1]
        if generation != self._probe_map_generation:
            return
        if kind == "connected":
            self._set_available_probes(event[2])
            self._probe_map_status_var.set(
                f"SpikeGLX filtered AP probe {self._trajectory.probe_map_probe} connected"
            )
        elif kind == "snapshot":
            snapshot = event[2]
            depths = list(getattr(snapshot, "channel_depths_mm", []))
            xs = list(getattr(snapshot, 'channel_x_mm', []))
            shanks = list(getattr(snapshot, 'channel_shanks', []))
            if (depths != self._probe_map_depths_mm or xs != self._probe_map_x_mm
                    or shanks != self._probe_map_shanks):
                self._clear_probe_map_overlay()
                self._probe_map_depths_mm = depths
                self._probe_map_x_mm = xs
                self._probe_map_shanks = shanks
            self._probe_map_values_mv = list(snapshot.peak_to_peak_mv)
            self._probe_map_update_count += 1
            now = time.monotonic()
            if self._probe_map_values_mv and now - self._probe_map_last_status_time >= 1.0:
                self._probe_map_last_status_time = now
                self._probe_map_status_var.set(
                    f"{snapshot.data_source}: update {self._probe_map_update_count} at "
                    f"{time.strftime('%H:%M:%S')}, "
                    f"{min(self._probe_map_values_mv):.3f}-"
                    f"{max(self._probe_map_values_mv):.3f} mV"
                )
            self._schedule_probe_map_overlay_draw()
        elif kind == "error":
            self._probe_map_status_var.set(str(event[2]))
            self._probe_map_values_mv = []
            self._probe_map_depths_mm = []
            self._clear_probe_map_overlay()
        elif kind == "done":
            self._probe_map_worker_running = False

    def _probe_button_text(self) -> str:
        return "Edit probe location" if self._trajectory.probe_location_set else "Set probe location"

    def _target_button_text(self) -> str:
        if self._target_editing:
            return "Finish target location"
        return "Edit target location" if self._trajectory.target_location_set else "Set target location"

    def _toggle_probe_location(self):
        if self._trajectory.probe_location_set:
            self._trajectory.probe_location_set = False
        else:
            if not self._apply_fields(show_warning=True):
                return
            self._trajectory.probe_location_set = True
            self._trajectory.probe_offset = self._trajectory.perpendicular_offset
            self._trajectory.probe_motor_position = self._motor_position

        if self._probe_button:
            self._probe_button.configure(text=self._probe_button_text())
        if self._target_button:
            self._target_button.configure(text=self._target_button_text())
        self._offset_var.set(f"{self._active_perpendicular_offset():g}")
        self._render_all()
        self._on_save(self._motor_id, self._trajectory)

    def _toggle_target_location(self):
        if self._target_editing:
            self._target_editing = False
        else:
            if not self._apply_fields(show_warning=True):
                return
            was_set = self._trajectory.target_location_set
            self._trajectory.target_location_set = True
            if not was_set:
                self._trajectory.target_offset = self._active_perpendicular_offset()
            self._target_editing = True

        if self._target_button:
            self._target_button.configure(text=self._target_button_text())
        self._redraw_inline_depth_lines()
        self._on_save(self._motor_id, self._trajectory)

    def _render_all(self):
        for _label, axis, _rotation_attr in self.PLANES:
            self._render_plane(axis)

    def _render_plane(self, axis: int):
        if not self._active:
            return
        if not self._volume:
            return
        if self._pane_collapsed.get(axis, False):
            return
        canvas = self._canvases.get(axis)
        if not canvas or not canvas.winfo_exists():
            return

        geometry = self._slice_geometry(axis)
        volume = self._volume
        low, high = self._contrast_low_var.get(), self._contrast_high_var.get()
        zoom = max(0.5, min(10.0, float(self._zoom_var.get())))
        base_signature = (id(volume), low, high, *(tuple(v) if hasattr(v, '__iter__') else v for v in geometry))
        signature = (base_signature, zoom)
        if self._render_signatures.get(axis) == signature:
            image_ref = self._image_refs.get(axis)
            if image_ref:
                self._draw_plane(axis, image_ref)
            return
        self._render_signatures[axis] = signature
        cached = self._source_images.get(axis) if self._source_signatures.get(axis) == base_signature else None

        def compute():
            source = cached
            if source is None:
                data = AnatomySliceRenderer.oblique_slice_to_ppm(
                    volume, *geometry, contrast_low=low, contrast_high=high, zoom=1.0)
                source = Image.open(io.BytesIO(data)).copy()
            scaled = source if abs(zoom - 1.0) < .01 else source.resize(
                (max(1, round(source.width * zoom)), max(1, round(source.height * zoom))),
                Image.Resampling.NEAREST)
            return source, scaled

        def complete(result, error):
            if error is not None:
                self._render_signatures.pop(axis, None)
                self._status_var.set(f"Could not render trajectory: {error}")
                return
            source, scaled = result
            image_ref = ImageTk.PhotoImage(scaled, master=self)
            self._source_images[axis] = source
            self._source_signatures[axis] = base_signature
            self._image_refs[axis] = image_ref
            self._draw_plane(axis, image_ref)

        self._render_worker.submit(('plane', axis), compute, complete)

    def _slice_geometry(self, axis: int):
        transverse_x, transverse_y, probe_axis = self._trajectory_basis()
        origin = self._trajectory_origin()
        center = self._slice_center_world(probe_axis) if axis == 2 else origin
        extent_x, extent_y, extent_z = self._world_extents_mm()

        if axis == 0:
            return self._oriented_slice_geometry(center, transverse_y, probe_axis, extent_y, extent_z)
        if axis == 1:
            return self._oriented_slice_geometry(center, transverse_x, probe_axis, extent_x, extent_z)
        return self._oriented_slice_geometry(center, transverse_x, transverse_y, extent_x, extent_y)

    def _oriented_slice_geometry(self, center, horizontal, vertical, width_mm, height_mm):
        matrix = self._orientation_world_matrix()
        if matrix is None:
            return center, horizontal, vertical, width_mm, height_mm

        center = tuple(matrix @ np.asarray(center, dtype=float))
        horizontal = tuple(matrix @ np.asarray(horizontal, dtype=float))
        vertical = tuple(matrix @ np.asarray(vertical, dtype=float))
        return center, horizontal, vertical, width_mm, height_mm

    def _orientation_world_matrix(self):
        if not (self._orientation_actual_key and self._orientation_scanner_key):
            return None
        return AnatomyOrientation.world_from_corrected_matrix(
            self._orientation_actual_key,
            self._orientation_scanner_key,
        )

    def _slice_center_world(self, probe_axis):
        origin = np.asarray(self._trajectory_origin(), dtype=float)
        return tuple(origin + (probe_axis * self._active_perpendicular_offset()))

    def _trajectory_origin(self):
        return (self._trajectory.x, self._trajectory.y, self._trajectory.z)

    def _active_perpendicular_offset(self) -> float:
        if self._trajectory.probe_location_set:
            return self._probe_depth_offset()
        return self._trajectory.perpendicular_offset

    def _probe_depth_offset(self) -> float:
        if self._trajectory.probe_location_set:
            motor_delta = self._motor_position - self._trajectory.probe_motor_position
            return self._trajectory.probe_offset - motor_delta
        return self._trajectory.perpendicular_offset

    def update_motor_position(self, motor_position: float):
        """Update the linked motor position and redraw probe-dependent views."""
        self._motor_position = round(float(motor_position), 3)
        if not self._active:
            return
        if self._trajectory.probe_location_set:
            self._offset_var.set(f"{self._active_perpendicular_offset():g}")
            self._redraw_inline_depth_lines()
            if self._live_update_job is None:
                self._live_update_job = self.after(
                    Config.TRAJECTORY_LIVE_UPDATE_INTERVAL_MS,
                    self._run_live_update
                )

    def _run_live_update(self):
        """Render the live-updating trajectory pane at a throttled rate."""
        self._live_update_job = None
        try:
            if self._trajectory.probe_location_set:
                self._render_plane(2)
        except tk.TclError:
            return

    def update_target_offset(self, target_offset: float):
        """Update the MRI target line from the main GUI target value."""
        self._trajectory.target_location_set = True
        self._trajectory.target_offset = round(float(target_offset), 3)
        if self._target_button:
            self._target_button.configure(text=self._target_button_text())
        self._redraw_inline_depth_lines()

    def _trajectory_basis(self):
        x_axis = np.asarray([1.0, 0.0, 0.0])
        y_axis = np.asarray([0.0, 1.0, 0.0])
        z_axis = np.asarray([0.0, 0.0, 1.0])

        # Rotation signs follow this screen convention:
        # +AP rotates inline clockwise, +ML rotates inline 90 counterclockwise,
        # and +twist rotates the perpendicular/axial view clockwise.
        r_ap = AnatomySliceRenderer.rotation_matrix(tuple(x_axis), self._trajectory.ap_rotation)
        r_ml = AnatomySliceRenderer.rotation_matrix(tuple(y_axis), -self._trajectory.ml_rotation)
        r_tilt = r_ml @ r_ap
        probe_axis = r_tilt @ z_axis
        transverse_x = r_tilt @ x_axis
        transverse_y = r_tilt @ y_axis

        r_twist = AnatomySliceRenderer.rotation_matrix(tuple(probe_axis), self._trajectory.twist_rotation)
        transverse_x = r_twist @ transverse_x
        transverse_y = r_twist @ transverse_y
        return transverse_x, transverse_y, probe_axis

    def _world_extents_mm(self):
        if not self._volume:
            return (100.0, 100.0, 100.0)
        if self._volume.voxel_sizes:
            return tuple(
                max(1.0, self._volume.shape[i] * float(self._volume.voxel_sizes[i]))
                for i in range(3)
            )
        return tuple(float(s) for s in self._volume.shape)

    def _draw_plane(self, axis: int, image_ref: tk.PhotoImage, refresh_probe_map=True):
        canvas = self._canvases[axis]
        width = max(1, canvas.winfo_width())
        height = max(1, canvas.winfo_height())
        image_width = image_ref.width()
        image_height = image_ref.height()
        left = (width - image_width) / 2
        top = (height - image_height) / 2

        canvas.delete("placeholder")
        canvas.delete("trajectory")
        if axis in (0, 1) and refresh_probe_map:
            layout_key = (id(image_ref), width, height, self._trajectory.probe_location_set)
            if self._probe_map_layout_keys.get(axis) != layout_key:
                self._schedule_probe_map_overlay_draw()
        if axis in self._image_items:
            canvas.itemconfigure(self._image_items[axis], image=image_ref)
            canvas.coords(self._image_items[axis], width / 2, height / 2)
        else:
            self._image_items[axis] = canvas.create_image(
                width / 2, height / 2, image=image_ref, anchor=tk.CENTER
            )

        color = "#00cc33"
        center_x = left + (image_width / 2)
        center_y = top + (image_height / 2)
        if axis in (0, 1):
            _center, _horizontal, _vertical, _width_mm, height_mm = self._slice_geometry(axis)
            center_y = top + self._offset_to_image_y(image_height, height_mm)
        if axis in (0, 1):
            canvas.create_line(center_x, top, center_x, top + image_height, fill=color, width=1, tags=("trajectory",))
            canvas.create_line(left, center_y, left + image_width, center_y, fill=color, width=1, tags=("trajectory",))
            self._draw_target_line(canvas, left, top, image_width, image_height, height_mm)
            self._draw_probe_line(canvas, axis, center_x, top, image_height, height_mm)
            # Movement redraws the shank, but the last activity frame must
            # remain intact until the next configured sampling-window update.
            canvas.tag_raise('probe_map')
        else:
            canvas.create_line(left, center_y, left + image_width, center_y, fill=color, width=1, tags=("trajectory",))
            canvas.create_line(center_x, top, center_x, top + image_height, fill=color, width=1, tags=("trajectory",))

    def _draw_target_line(self, canvas: tk.Canvas, left: float, top: float,
                          image_width: int, image_height: int, height_mm: float):
        if not self._trajectory.target_location_set:
            return
        y = top + self._offset_to_image_y(image_height, height_mm, self._trajectory.target_offset)
        canvas.create_line(
            left, y, left + image_width, y,
            fill="#ff3333", width=2, tags=("trajectory",)
        )

    def _draw_probe_line(
        self,
        canvas: tk.Canvas,
        axis: int,
        center_x: float,
        top: float,
        image_height: int,
        height_mm: float,
    ):
        if not self._trajectory.probe_location_set:
            return
        bottom_y = top + self._offset_to_image_y(image_height, height_mm, self._probe_depth_offset())
        if bottom_y > top:
            canvas.create_polygon(*probe_body_points(center_x, top, bottom_y, self._trajectory.probe_width_px,
                min(6., .4 * image_height / max(float(height_mm), 1.))),
                fill='#b5b5b5', outline='#777777', tags=('trajectory', 'probe_body'))
    def _probe_map_image(self, image_height, height_mm, bottom, colors):
        return contact_activity_image(colors, self._probe_map_depths_mm, self._probe_map_x_mm,
            self._probe_map_shanks, image_height, height_mm, bottom, self._probe_map_channel_pitch_mm)

    def _probe_map_limits(self) -> tuple[float, float]:
        try:
            map_min = float(self._probe_map_min_var.get())
            map_max = float(self._probe_map_max_var.get())
        except ValueError:
            map_min = self._trajectory.probe_map_min_mv
            map_max = self._trajectory.probe_map_max_mv
        if map_max <= map_min:
            map_max = map_min + 1.0
        return map_min, map_max

    def _smoothed_probe_activity(self):
        geometry = (len(self._probe_map_values_mv), tuple(self._probe_map_depths_mm),
                    tuple(self._probe_map_shanks), self._trajectory.probe_map_smoothing_um)
        if geometry != self._probe_smoothing_geometry:
            self._probe_smoother = SpatialActivitySmoother(*geometry)
            self._probe_smoothing_geometry = geometry
            self._probe_smoothed_key = None
        key = tuple(self._probe_map_values_mv)
        if key != self._probe_smoothed_key:
            self._probe_smoothed_profiles = self._probe_smoother.smooth(key)
            self._probe_smoothed_key = key
        return self._probe_smoothed_profiles

    def _redraw_probe_map_overlay_only(self):
        """Build both overlays first, then present the same activity frame together."""
        self._probe_map_draw_job = None
        if not self._active:
            return
        if not (self._trajectory.probe_map_enabled and self._trajectory.probe_location_set
                and self._probe_map_values_mv):
            self._clear_probe_map_overlay()
            return
        palette_key = (tuple(self._probe_map_values_mv), self._probe_map_limits())
        if palette_key != self._probe_map_palette_key:
            self._probe_map_colors = tuple(color_from_lut(self._probe_map_lut, value, *palette_key[1])
                                           for value in palette_key[0])
            self._probe_map_palette_key = palette_key
        prepared = {}
        profiles = self._smoothed_probe_activity() if self._trajectory.probe_map_smooth else None
        offset = self._probe_depth_offset()
        for axis in (0, 1):
            canvas, image_ref = self._canvases.get(axis), self._image_refs.get(axis)
            if canvas is None or image_ref is None or not canvas.winfo_exists():
                continue
            image_height = image_ref.height()
            height_mm = self._slice_geometry(axis)[4]
            bottom = self._offset_to_image_y(image_height, height_mm, offset)
            raster = self._probe_overlay_renderer.render(self._trajectory, self._probe_map_values_mv,
                self._probe_map_depths_mm, self._probe_map_x_mm, self._probe_map_shanks,
                image_height, height_mm, bottom, profiles=profiles, colors=self._probe_map_colors,
                limits=self._probe_map_limits())
            photo = ImageTk.PhotoImage(raster, master=self)
            prepared[axis] = (photo, canvas.winfo_width() / 2 - raster.width / 2,
                              (canvas.winfo_height() - image_height) / 2)
        # No Tk event processing between swaps; all references are ready first.
        previous_refs = self._probe_map_image_refs
        self._probe_map_image_refs = {**previous_refs, **{axis: frame[0] for axis, frame in prepared.items()}}
        for axis, (photo, x, y) in prepared.items():
            canvas = self._canvases[axis]
            self._probe_map_layout_keys[axis] = (id(self._image_refs[axis]), max(1, canvas.winfo_width()),
                                                max(1, canvas.winfo_height()), self._trajectory.probe_location_set)
            item = self._probe_map_items.get(axis)
            if item is None:
                self._probe_map_items[axis] = canvas.create_image(x, y, image=photo, anchor='nw', tags='probe_map')
            else:
                canvas.itemconfigure(item, image=photo)
                canvas.coords(item, x, y)
            canvas.tag_raise('probe_map')

    def _schedule_probe_map_overlay_draw(self):
        if not self._active:
            return
        if self._probe_map_draw_job is not None:
            return
        try:
            self._probe_map_draw_job = self.after(0, self._redraw_probe_map_overlay_only)
        except tk.TclError:
            self._probe_map_draw_job = None

    def _cancel_probe_map_overlay_draw(self):
        if self._probe_map_draw_job is None:
            return
        try:
            self.after_cancel(self._probe_map_draw_job)
        except tk.TclError:
            pass
        self._probe_map_draw_job = None

    def _clear_probe_map_overlay(self):
        self._cancel_probe_map_overlay_draw()
        for canvas in self._canvases.values():
            try:
                if canvas.winfo_exists():
                    canvas.delete("probe_map")
            except tk.TclError:
                continue
        self._probe_map_items.clear()
        self._probe_map_image_refs.clear()
        self._probe_map_layout_keys.clear()

    def _offset_to_image_y(self, image_height: int, height_mm: float, offset: float = None) -> float:
        half_height = max(float(height_mm) / 2.0, 1.0)
        if offset is None:
            offset = self._active_perpendicular_offset()
        normalized = (half_height - offset) / (2.0 * half_height)
        normalized = max(0.0, min(1.0, normalized))
        return normalized * image_height

    def _bind_perpendicular_scroll(self, widget: tk.Widget):
        widget.bind("<MouseWheel>", self._on_perpendicular_scroll)
        widget.bind("<Button-4>", self._on_perpendicular_scroll)
        widget.bind("<Button-5>", self._on_perpendicular_scroll)

    def _bind_target_scroll(self, widget: tk.Widget):
        widget.bind("<MouseWheel>", self._on_target_scroll)
        widget.bind("<Button-4>", self._on_target_scroll)
        widget.bind("<Button-5>", self._on_target_scroll)

    def _on_perpendicular_scroll(self, event):
        if not self._volume:
            return "break"
        if self._trajectory.probe_location_set:
            return "break"

        if getattr(event, "num", None) == 4 or getattr(event, "delta", 0) > 0:
            step = 0.5
        else:
            step = -0.5

        self._trajectory.perpendicular_offset = round(self._trajectory.perpendicular_offset + step, 3)
        self._offset_var.set(f"{self._trajectory.perpendicular_offset:g}")
        self._render_plane(2)
        self._redraw_inline_depth_lines()
        self._on_save(self._motor_id, self._trajectory)
        return "break"

    def _on_target_scroll(self, event):
        if not self._target_editing or not self._trajectory.target_location_set:
            return None

        if getattr(event, "num", None) == 4 or getattr(event, "delta", 0) > 0:
            step = 0.5
        else:
            step = -0.5

        self._trajectory.target_offset = round(self._trajectory.target_offset + step, 3)
        self._redraw_inline_depth_lines()
        self._on_save(self._motor_id, self._trajectory)
        return "break"

    def _redraw_inline_depth_lines(self):
        if not self._active:
            return
        for axis in (0, 1):
            image_ref = self._image_refs.get(axis)
            canvas = self._canvases.get(axis)
            try:
                if not image_ref or not canvas or not canvas.winfo_exists():
                    continue
                self._draw_plane(axis, image_ref, refresh_probe_map=False)
            except tk.TclError:
                continue

    def _adjust_zoom(self, delta: float):
        zoom = max(0.5, min(10.0, round(float(self._zoom_var.get()) + delta, 1)))
        self._zoom_var.set(zoom)
        self._on_zoom_change()

    def _on_zoom_change(self, _value=None):
        self._trajectory.zoom = round(float(self._zoom_var.get()), 1)
        self._schedule_zoom_render()

    def _schedule_zoom_render(self):
        self._cancel_zoom_render()
        try:
            self._zoom_render_job = self.after(40, self._render_zoom_change)
        except tk.TclError:
            self._zoom_render_job = None

    def _cancel_zoom_render(self):
        if self._zoom_render_job is None:
            return
        try:
            self.after_cancel(self._zoom_render_job)
        except tk.TclError:
            pass
        self._zoom_render_job = None

    def _render_zoom_change(self):
        self._zoom_render_job = None
        self._rescale_rendered_planes()
        self._on_save(self._motor_id, self._trajectory)

    def _rescale_rendered_planes(self):
        self._render_all()

    def _style_widget_tree(self, widget=None):
        style_viewer(self, widget)


class TrajectoryViewer(_TrajectoryView, tk.Toplevel):
    """Standalone trajectory window, also used by regression and stress checks."""


class TrajectoryPanel(_TrajectoryView, tk.Frame):
    """Embeddable drive view for the shared Trajectories window."""
