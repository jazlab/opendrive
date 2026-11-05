"""Free MRI exploration inside the Trajectories workspace."""
import math
from concurrent.futures import ThreadPoolExecutor
import tkinter as tk
from tkinter import ttk

import numpy as np
from PIL import Image, ImageTk

from .models import TrajectoryState
from .reslice import ObliqueReslicer
from .trajectory_viewer import TrajectoryViewer
from .viewer_tasks import LatestWorker
from .viewer_theme import style_viewer, viewer_button, viewer_zoom


class ExplorerView(tk.Frame):
    """Three oblique slices sharing an editable origin and rotation basis."""

    PLANES = TrajectoryViewer.PLANES
    _trajectory_basis = TrajectoryViewer._trajectory_basis
    _slice_geometry = TrajectoryViewer._slice_geometry
    _oriented_slice_geometry = TrajectoryViewer._oriented_slice_geometry
    _orientation_world_matrix = TrajectoryViewer._orientation_world_matrix
    _world_extents_mm = TrajectoryViewer._world_extents_mm

    def __init__(self, parent, owner, initial_state=None, on_change=None, drive_ids=None, on_assign=None):
        super().__init__(parent, padx=12, pady=12)
        self._owner = owner
        self._viewer_font = owner._viewer_font
        self._viewer_section_font = owner._viewer_section_font
        self._on_change = on_change
        self._on_assign = on_assign
        self._drive_ids = drive_ids or []
        self._drive_var = tk.StringVar(self)
        self._assignment_status = tk.StringVar(self)
        self._active = False
        self._volume = None
        self._orientation_actual_key = ''
        self._orientation_scanner_key = ''
        state = initial_state or {}
        def number(key, default=0.0):
            try:
                value = float(state.get(key, default))
                return value if math.isfinite(value) else default
            except (TypeError, ValueError):
                return default
        self._origin = np.array([number(key) for key in ('x', 'y', 'z')])
        self._has_origin = all(key in state for key in ('x', 'y', 'z'))
        self._trajectory = TrajectoryState(
            ap_rotation=max(-180, min(180, number('ap_rotation'))),
            ml_rotation=max(-180, min(180, number('ml_rotation'))),
            twist_rotation=max(-180, min(180, number('twist_rotation'))))
        self._zoom_var = tk.DoubleVar(self, value=max(0.5, min(10, number('zoom', 1))))
        self._contrast_low_var = owner._contrast_low_var
        self._contrast_high_var = owner._contrast_high_var
        self._status_var = tk.StringVar(self, value='Load anatomy to explore')
        self._origin_vars = [tk.StringVar(self, value=f'{v:.3f}') for v in self._origin]
        self._angle_vars = {}
        self._angle_scales = {}
        self._render_worker = LatestWorker(self)
        self._slice_pool = ThreadPoolExecutor(max_workers=3, thread_name_prefix='MRI plane')
        self._reslicer = None
        self._frame_in_flight = False
        self._frame_dirty = False
        self._render_job = None
        self._render_signatures = {}
        self._source_signatures = {}
        self._source_images = {}
        self._image_refs = {}
        self._image_items = {}
        self._canvases = {}
        self._pane_collapsed = {}
        self._build_ui()
        self.set_drive_ids(self._drive_ids)
        style_viewer(self)

    def _build_ui(self):
        toolbar = tk.Frame(self)
        toolbar.pack(fill=tk.X, pady=(0, 12))
        tk.Label(toolbar, text='Zoom:').pack(side=tk.LEFT, padx=(0, 6))
        viewer_button(toolbar, text='-', width=3, command=lambda: self._adjust_zoom(-.1)).pack(side=tk.LEFT)
        viewer_zoom(toolbar, self._zoom_var, self._schedule_render, maximum=10.0, length=180).pack(side=tk.LEFT, padx=4)
        viewer_button(toolbar, text='+', width=3, command=lambda: self._adjust_zoom(.1)).pack(side=tk.LEFT)
        tk.Label(toolbar, textvariable=self._zoom_var, width=4).pack(side=tk.LEFT)
        tk.Label(toolbar, text='Scroll to move through the hovered plane').pack(side=tk.LEFT, padx=18)
        body = tk.Frame(self)
        body.pack(fill=tk.BOTH, expand=True)
        sidebar = tk.Frame(body, width=190)
        sidebar.pack(side=tk.LEFT, fill=tk.Y, padx=(0, 14))
        sliders = tk.Frame(sidebar)
        sliders.pack(fill=tk.X)
        style = ttk.Style(self)
        style.configure('Explorer.Vertical.TScale', background='white', troughcolor='#c5c9ce',
                        bordercolor='#dce2e9', lightcolor='white', darkcolor='white')
        style.map('Explorer.Vertical.TScale', background=[('active', 'white')])
        for column, (label, key) in enumerate((('AP', 'ap_rotation'), ('Lat', 'ml_rotation'), ('Twist', 'twist_rotation'))):
            value = getattr(self._trajectory, key)
            scale_var = tk.DoubleVar(self, value=value)
            text_var = tk.StringVar(self, value=f'{value:.1f}')
            self._angle_vars[key] = text_var
            self._angle_scales[key] = scale_var
            tk.Label(sliders, text=label).grid(row=0, column=column, pady=(0, 6))
            slider = ttk.Scale(sliders, from_=180, to=-180, orient=tk.VERTICAL,
                               variable=scale_var, length=260, style='Explorer.Vertical.TScale',
                               command=lambda value, name=key: self._rotate(name, value))
            slider.grid(row=1, column=column, padx=17)
            for arrow, delta in (('Up', .1), ('Down', -.1)):
                slider.bind(f'<{arrow}>', lambda event, name=key, step=delta: self._nudge_angle(name, step))
            entry = tk.Entry(sliders, textvariable=text_var, width=6, justify='center')
            entry.grid(row=2, column=column, padx=2, pady=(8, 16))
            entry.bind('<Return>', lambda event, name=key: self._commit_angle(name))
            entry.bind('<FocusOut>', lambda event, name=key: self._commit_angle(name))
        tk.Label(sidebar, text='Crosshairs origin (mm)', font=self._viewer_section_font).pack(anchor='w', pady=(8, 8))
        for label, variable in zip(('X:', 'Y:', 'Z:'), self._origin_vars):
            row = tk.Frame(sidebar)
            row.pack(fill=tk.X, pady=3)
            tk.Label(row, text=label, width=3, anchor='w').pack(side=tk.LEFT)
            entry = tk.Entry(row, textvariable=variable, width=11)
            entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
            entry.bind('<Return>', self._commit_origin)
            entry.bind('<FocusOut>', self._commit_origin)
        viewer_button(sidebar, text='Center origin', command=self._center_origin).pack(fill=tk.X, pady=(12, 0))
        tk.Label(sidebar, text='Assign trajectory', font=self._viewer_section_font).pack(anchor='w', pady=(16, 6))
        self._drive_combo = ttk.Combobox(sidebar, textvariable=self._drive_var, state='readonly', width=16,
                                         font=self._viewer_font)
        self._drive_combo.pack(fill=tk.X)
        self._assign_button = viewer_button(sidebar, text='Assign to drive', command=self._assign_to_drive,
                                            style='Primary.Viewer.TButton')
        self._assign_button.pack(fill=tk.X, pady=(6, 0))
        tk.Label(sidebar, textvariable=self._assignment_status, wraplength=185, justify='left').pack(fill=tk.X, pady=(6, 0))
        images = tk.Frame(body)
        images.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        images.grid_rowconfigure(0, weight=1)
        for label, axis, _rotation in self.PLANES:
            pane = tk.Frame(images, padx=6, pady=6, relief=tk.SUNKEN, borderwidth=1)
            pane.grid(row=0, column=axis, sticky='nsew', padx=4)
            images.grid_columnconfigure(axis, weight=1, uniform='explorer')
            tk.Label(pane, text=label, font=self._viewer_section_font).pack(anchor='w', pady=(0, 6))
            canvas = tk.Canvas(pane, width=250, height=360, background='black', highlightthickness=0)
            canvas.pack(fill=tk.BOTH, expand=True)
            canvas.create_text(125, 180, text='Load anatomy', fill='white', tags='placeholder')
            canvas.bind('<Configure>', lambda event, plane=axis: self._render_plane(plane))
            for wheel in ('<MouseWheel>', '<Button-4>', '<Button-5>'):
                canvas.bind(wheel, lambda event, plane=axis: self._scroll_origin(plane, event))
            self._canvases[axis] = canvas
        tk.Label(self, textvariable=self._status_var, anchor='w').pack(fill=tk.X, pady=(10, 0))

    def set_volume(self, volume, actual='', scanner='', reset=False):
        self._invalidate_frame()
        self._render_signatures.clear()
        self._source_signatures.clear()
        self._source_images.clear()
        self._volume = volume
        self._reslicer = ObliqueReslicer(volume)
        self.set_drive_ids(self._drive_ids)
        self._orientation_actual_key, self._orientation_scanner_key = actual, scanner
        if reset or not self._has_origin:
            self._center_origin()
        self._status_var.set(volume.display_name)
        self._schedule_render()

    def set_drive_ids(self, drive_ids):
        self._drive_ids = list(drive_ids)
        names = [f'Drive {motor_id}' for motor_id in self._drive_ids]
        self._drive_combo.configure(values=names)
        if self._drive_var.get() not in names:
            self._drive_var.set(names[0] if names else '')
        self._assign_button.configure(state='normal' if names and self._volume is not None and self._on_assign else 'disabled')

    def _assign_to_drive(self):
        if self._volume is None or self._on_assign is None:
            return
        try:
            motor_id = int(self._drive_var.get().split()[-1])
            if motor_id not in self._drive_ids:
                raise ValueError
            origin = [float(var.get()) for var in self._origin_vars]
            angles = [float(var.get()) for var in self._angle_vars.values()]
            if not all(math.isfinite(value) for value in origin + angles) or any(abs(value) > 180 for value in angles):
                raise ValueError
        except (ValueError, IndexError):
            self._assignment_status.set('Check the drive, origin, and angles before assigning.')
            return
        self._commit_origin()
        for key in self._angle_vars:
            self._commit_angle(key)
        self._assignment_status.set(f'Assigning to Drive {motor_id}...')
        self._on_assign(motor_id, self.get_state())

    def _center_origin(self):
        if self._volume is None:
            return
        voxel = np.array([*(0.5 * (np.array(self._volume.shape) - 1)), 1.0])
        affine = self._volume.affine if self._volume.affine is not None else np.eye(4)
        center = (affine @ voxel)[:3]
        matrix = self._orientation_world_matrix()
        self._origin = np.linalg.solve(matrix, center) if matrix is not None else center
        self._has_origin = True
        self._sync_origin_fields()
        self._schedule_render()

    def _trajectory_origin(self):
        return tuple(self._origin)

    def _slice_center_world(self, _probe_axis):
        return self._trajectory_origin()

    def _render_plane(self, axis):
        if self._active and axis in self._image_refs:
            self._draw_plane(axis, self._image_refs[axis])
        elif self._active:
            self._render_all()

    def _invalidate_frame(self):
        self._render_worker.invalidate()
        self._frame_in_flight = False
        self._frame_dirty = False
        if self._render_job is not None:
            self.after_cancel(self._render_job)
            self._render_job = None

    def set_active(self, active):
        self._active = active
        if active:
            self._render_all()
        else:
            self._invalidate_frame()

    def _render_all(self):
        self._frame_dirty = True
        if self._active and self._volume is not None and not self._frame_in_flight and self._render_job is None:
            self._render_job = self.after_idle(self._flush_render)

    def _draw_plane(self, axis, image_ref):
        canvas = self._canvases[axis]
        x, y = canvas.winfo_width() / 2, canvas.winfo_height() / 2
        canvas.delete('placeholder')
        canvas.delete('crosshair')
        if axis in self._image_items:
            canvas.itemconfigure(self._image_items[axis], image=image_ref)
            canvas.coords(self._image_items[axis], x, y)
        else:
            self._image_items[axis] = canvas.create_image(x, y, image=image_ref)
        w, h = image_ref.width() / 2, image_ref.height() / 2
        canvas.create_line(x - w, y, x + w, y, fill='#00cc33', tags='crosshair')
        canvas.create_line(x, y - h, x, y + h, fill='#00cc33', tags='crosshair')

    def _rotate(self, key, value):
        value = round(max(-180, min(180, float(value))), 1)
        setattr(self._trajectory, key, value)
        self._angle_scales[key].set(value)
        self._angle_vars[key].set(f'{value:.1f}')
        self._schedule_render()

    def _nudge_angle(self, key, delta):
        value = max(-180, min(180, getattr(self._trajectory, key) + delta))
        self._angle_scales[key].set(value)
        self._rotate(key, value)
        return 'break'

    def _commit_angle(self, key):
        try:
            value = float(self._angle_vars[key].get())
            if not math.isfinite(value) or not -180 <= value <= 180:
                raise ValueError
        except ValueError:
            self._status_var.set('Angles must be finite values between -180 and 180 degrees')
            self._angle_vars[key].set(f'{getattr(self._trajectory, key):.1f}')
            return 'break'
        self._angle_scales[key].set(value)
        self._rotate(key, value)
        return 'break'

    def _commit_origin(self, _event=None):
        try:
            origin = np.array([float(var.get()) for var in self._origin_vars])
            if not np.isfinite(origin).all():
                raise ValueError
        except ValueError:
            self._status_var.set('Origin coordinates must be finite numbers')
            self._sync_origin_fields()
            return 'break'
        self._origin = origin
        self._has_origin = True
        self._sync_origin_fields()
        self._schedule_render()
        return 'break'

    def _scroll_origin(self, axis, event):
        if self._volume is None:
            return 'break'
        delta = getattr(event, 'delta', 0)
        num = getattr(event, 'num', None)
        if not delta and num not in (4, 5):
            return 'break'
        direction = 1 if num == 4 or delta > 0 else -1
        # The rotated transverse axes are the normals of the two inline planes;
        # the probe axis is the perpendicular-plane normal. Work in the same
        # coordinate frame as the origin; rendering applies orientation correction
        # to both the origin and basis together.
        normal = self._trajectory_basis()[axis]
        self._origin = self._origin + direction * .5 * normal
        self._has_origin = True
        self._sync_origin_fields()
        self._schedule_render()
        return 'break'

    def _sync_origin_fields(self):
        for var, value in zip(self._origin_vars, self._origin):
            var.set(f'{value:.3f}')

    def _adjust_zoom(self, delta):
        self._zoom_var.set(max(.5, min(10, round(self._zoom_var.get() + delta, 1))))
        self._schedule_render()

    def _schedule_render(self):
        if self._on_change and self._has_origin:
            self._on_change(self.get_state())
        self._render_all()

    def _flush_render(self):
        self._render_job = None
        if not self._active or self._volume is None:
            return
        self._frame_dirty = False
        low, high = self._contrast_low_var.get(), self._contrast_high_var.get()
        zoom = max(.5, min(10., self._zoom_var.get()))
        geometries = [self._slice_geometry(axis) for axis in range(3)]
        bases = [(id(self._volume), low, high, *(tuple(v) if hasattr(v, '__iter__') else v for v in geometry))
                 for geometry in geometries]
        signatures = {axis: (bases[axis], zoom) for axis in range(3)}
        if signatures == self._render_signatures:
            return
        cached = [self._source_images.get(axis) if self._source_signatures.get(axis) == bases[axis] else None
                  for axis in range(3)]
        reslicer, pool = self._reslicer, self._slice_pool
        self._frame_in_flight = True

        def render(axis):
            source = cached[axis]
            if source is None:
                source = Image.fromarray(reslicer.grayscale(*geometries[axis], contrast_low=low, contrast_high=high))
            scaled = source if abs(zoom - 1.) < .01 else source.resize(
                (max(1, round(source.width * zoom)), max(1, round(source.height * zoom))), Image.Resampling.NEAREST)
            return source, scaled

        def compute():
            futures = [pool.submit(render, axis) for axis in range(3)]
            return [future.result() for future in futures]

        def complete(result, error):
            self._frame_in_flight = False
            if error is not None:
                self._status_var.set(f'Could not render Explorer: {error}')
            else:
                # Prepare every Tk image before replacing any visible plane.
                refs = {axis: ImageTk.PhotoImage(scaled, master=self) for axis, (_, scaled) in enumerate(result)}
                self._source_images = {axis: source for axis, (source, _) in enumerate(result)}
                self._source_signatures = dict(enumerate(bases))
                self._render_signatures = signatures
                self._image_refs = refs
                for axis in range(3):
                    self._draw_plane(axis, refs[axis])
                self._status_var.set(self._volume.display_name)
            if self._frame_dirty:
                self._render_all()

        self._render_worker.submit('frame', compute, complete)

    def get_state(self):
        return dict(zip(('x', 'y', 'z'), map(float, self._origin)),
                    ap_rotation=self._trajectory.ap_rotation,
                    ml_rotation=self._trajectory.ml_rotation,
                    twist_rotation=self._trajectory.twist_rotation,
                    zoom=self._zoom_var.get())

    def destroy(self):
        if self._render_job is not None:
            self.after_cancel(self._render_job)
        self._render_worker.close()
        self._slice_pool.shutdown(wait=False, cancel_futures=True)
        super().destroy()
