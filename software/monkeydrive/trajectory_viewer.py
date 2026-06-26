"""Trajectory planning viewer for motor-specific MRI views."""

from __future__ import annotations

from pathlib import Path
from typing import Callable

import tkinter as tk
from tkinter import messagebox
import numpy as np

from .anatomy import AnatomyDependencyError, AnatomyLoader, AnatomySliceRenderer, AnatomyVolume
from .anatomy_viewer import ContrastWindowControl
from .config import Config
from .models import TrajectoryState


class TrajectoryViewer(tk.Toplevel):
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
        on_save: Callable[[int, TrajectoryState], None],
    ):
        """Initialize a trajectory viewer."""
        super().__init__(parent)

        self._motor_id = motor_id
        self._trajectory = TrajectoryState.from_dict(trajectory.to_dict())
        self._motor_position = float(motor_position)
        self._anatomy_path = anatomy_path
        self._on_save = on_save
        self._volume: AnatomyVolume | None = None

        self._image_refs: dict[int, tk.PhotoImage] = {}
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
        self._field_warning_active = False
        self._live_update_job = None

        self._x_var = tk.StringVar(value=f"{self._trajectory.x:g}")
        self._y_var = tk.StringVar(value=f"{self._trajectory.y:g}")
        self._ap_var = tk.StringVar(value=f"{self._trajectory.ap_rotation:g}")
        self._ml_var = tk.StringVar(value=f"{self._trajectory.ml_rotation:g}")
        self._twist_var = tk.StringVar(value=f"{self._trajectory.twist_rotation:g}")
        self._offset_var = tk.StringVar(value=f"{self._trajectory.perpendicular_offset:g}")
        self._probe_button: tk.Button | None = None
        self._target_button: tk.Button | None = None
        self._target_editing = False

        self.title(f"Motor {motor_id} Trajectory")
        self.geometry("1550x760")
        self.minsize(520, 520)

        parent_x = parent.winfo_x()
        parent_y = parent.winfo_y()
        parent_width = parent.winfo_width()
        self.geometry(f"+{parent_x + parent_width + 10}+{parent_y}")

        self._build_ui()
        self._style_widget_tree()
        self.after(100, self._load_anatomy)

    def _build_ui(self):
        container = tk.Frame(self, padx=12, pady=12)
        container.pack(fill=tk.BOTH, expand=True)

        toolbar = tk.Frame(container)
        toolbar.pack(fill=tk.X, pady=(0, 10))

        tk.Label(toolbar, text="Zoom:").pack(side=tk.LEFT, padx=(0, 6))
        tk.Button(toolbar, text="-", command=lambda: self._adjust_zoom(-0.1), width=3).pack(side=tk.LEFT)
        tk.Scale(
            toolbar, from_=0.5, to=3.0, resolution=0.1, orient=tk.HORIZONTAL,
            variable=self._zoom_var, command=self._on_zoom_change,
            showvalue=True, length=160
        ).pack(side=tk.LEFT, padx=4)
        tk.Button(toolbar, text="+", command=lambda: self._adjust_zoom(0.1), width=3).pack(side=tk.LEFT)
        tk.Button(toolbar, text="Save", command=self._save_fields, width=8).pack(side=tk.LEFT, padx=(18, 0))
        self._probe_button = tk.Button(
            toolbar, text=self._probe_button_text(),
            command=self._toggle_probe_location
        )
        self._probe_button.pack(side=tk.LEFT, padx=(6, 0))
        self._target_button = tk.Button(
            toolbar, text=self._target_button_text(),
            command=self._toggle_target_location
        )
        self._target_button.pack(side=tk.LEFT, padx=(6, 0))

        viewer_frame = tk.Frame(container)
        viewer_frame.pack(fill=tk.BOTH, expand=True)

        contrast_frame = tk.Frame(viewer_frame)
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
        self._add_field(parameter_frame, "A-P rot:", self._ap_var)
        self._add_field(parameter_frame, "M-L rot:", self._ml_var)
        self._add_field(parameter_frame, "Twist:", self._twist_var)

        image_row = tk.Frame(viewer_frame)
        self._image_row = image_row
        image_row.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

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
            collapse_button = tk.Button(
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

    def _commit_fields_from_event(self, _event=None):
        self._apply_fields(show_warning=True)
        return "break"

    def _load_anatomy(self):
        if not self._anatomy_path or not Path(self._anatomy_path).exists():
            self._status_var.set("Open the Anatomy Viewer and load an MRI before using trajectories.")
            return

        try:
            self._volume = AnatomyLoader.load(self._anatomy_path)
        except AnatomyDependencyError as exc:
            messagebox.showwarning("Missing MRI Dependencies", str(exc), parent=self)
            return
        except Exception as exc:
            messagebox.showerror("Trajectory Load Failed", str(exc), parent=self)
            return

        self._status_var.set(self._volume.display_name)
        self._render_all()

    def _apply_fields(self, show_warning: bool = True) -> bool:
        try:
            probe_location_set = self._trajectory.probe_location_set
            probe_offset = self._trajectory.probe_offset
            probe_motor_position = self._trajectory.probe_motor_position
            target_location_set = self._trajectory.target_location_set
            target_offset = self._trajectory.target_offset
            self._trajectory = TrajectoryState(
                x=float(self._x_var.get()),
                y=float(self._y_var.get()),
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
            )
        except ValueError:
            if show_warning and not self._field_warning_active:
                self._field_warning_active = True
                try:
                    messagebox.showwarning("Invalid Trajectory", "Trajectory fields must be numeric.", parent=self)
                finally:
                    self._field_warning_active = False
            return False

        self._render_all()
        return True

    def _save_fields(self):
        if self._apply_fields(show_warning=True):
            self._on_save(self._motor_id, self._trajectory)

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
        if not self._volume:
            return
        if self._pane_collapsed.get(axis, False):
            return
        canvas = self._canvases.get(axis)
        if not canvas or not canvas.winfo_exists():
            return

        try:
            center, horizontal, vertical, width_mm, height_mm = self._slice_geometry(axis)
            image_data = AnatomySliceRenderer.oblique_slice_to_ppm(
                self._volume,
                center,
                horizontal,
                vertical,
                width_mm,
                height_mm,
                contrast_low=self._contrast_low_var.get(),
                contrast_high=self._contrast_high_var.get(),
                zoom=self._zoom_var.get(),
            )
            image_ref = tk.PhotoImage(data=image_data, format="PPM")
            self._draw_plane(axis, image_ref)
            self._image_refs[axis] = image_ref
        except tk.TclError:
            return
        except Exception as exc:
            canvas.delete("all")
            canvas.create_text(
                canvas.winfo_width() // 2,
                canvas.winfo_height() // 2,
                text=f"Could not render trajectory:\n{exc}",
                fill="#ffffff",
            )

    def _slice_geometry(self, axis: int):
        transverse_x, transverse_y, probe_axis = self._trajectory_basis()
        origin = (self._trajectory.x, self._trajectory.y, 0.0)
        center = self._slice_center_world(probe_axis) if axis == 2 else origin
        extent_x, extent_y, extent_z = self._world_extents_mm()

        if axis == 0:
            return center, transverse_y, probe_axis, extent_y, extent_z
        if axis == 1:
            return center, transverse_x, probe_axis, extent_x, extent_z
        return center, transverse_x, transverse_y, extent_x, extent_y

    def _slice_center_world(self, probe_axis):
        origin = np.asarray([self._trajectory.x, self._trajectory.y, 0.0], dtype=float)
        return tuple(origin + (probe_axis * self._active_perpendicular_offset()))

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
                self._redraw_inline_depth_lines()
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

        # Sign choices follow the requested screen convention:
        # positive A-P rotates the inline view clockwise, positive M-L and twist
        # rotate their views counterclockwise.
        r_ap = AnatomySliceRenderer.rotation_matrix(tuple(x_axis), self._trajectory.ap_rotation)
        r_ml = AnatomySliceRenderer.rotation_matrix(tuple(y_axis), -self._trajectory.ml_rotation)
        r_tilt = r_ml @ r_ap
        probe_axis = r_tilt @ z_axis
        transverse_x = r_tilt @ x_axis
        transverse_y = r_tilt @ y_axis

        r_twist = AnatomySliceRenderer.rotation_matrix(tuple(probe_axis), -self._trajectory.twist_rotation)
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

    def _draw_plane(self, axis: int, image_ref: tk.PhotoImage):
        canvas = self._canvases[axis]
        canvas.update_idletasks()
        width = max(1, canvas.winfo_width())
        height = max(1, canvas.winfo_height())
        image_width = image_ref.width()
        image_height = image_ref.height()
        left = (width - image_width) / 2
        top = (height - image_height) / 2

        canvas.delete("placeholder")
        canvas.delete("trajectory")
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
            self._draw_probe_line(canvas, center_x, top, image_height, height_mm)
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

    def _draw_probe_line(self, canvas: tk.Canvas, center_x: float, top: float, image_height: int, height_mm: float):
        if not self._trajectory.probe_location_set:
            return
        bottom_y = top + self._offset_to_image_y(image_height, height_mm, self._probe_depth_offset())
        canvas.create_line(
            center_x, top, center_x, bottom_y,
            fill="#1f6fff", width=4, tags=("trajectory",)
        )

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
        for axis in (0, 1):
            image_ref = self._image_refs.get(axis)
            canvas = self._canvases.get(axis)
            try:
                if not image_ref or not canvas or not canvas.winfo_exists():
                    continue
                self._draw_plane(axis, image_ref)
            except tk.TclError:
                continue

    def _adjust_zoom(self, delta: float):
        zoom = max(0.5, min(3.0, round(float(self._zoom_var.get()) + delta, 1)))
        self._zoom_var.set(zoom)
        self._on_zoom_change()

    def _on_zoom_change(self, _value=None):
        self._trajectory.zoom = round(float(self._zoom_var.get()), 1)
        self._render_all()
        self._on_save(self._motor_id, self._trajectory)

    def _style_widget_tree(self, widget=None):
        widget = widget or self

        try:
            if isinstance(widget, (tk.Tk, tk.Toplevel, tk.Frame)):
                widget.configure(background=Config.COLOR_BG)
            elif isinstance(widget, tk.Canvas):
                if widget not in self._canvases.values():
                    widget.configure(background=Config.COLOR_BG, highlightthickness=0)
            elif isinstance(widget, tk.Label):
                widget.configure(background=Config.COLOR_BG, foreground=Config.COLOR_FG)
            elif isinstance(widget, tk.Button):
                widget.configure(
                    background=Config.COLOR_ENTRY_BG,
                    foreground=Config.COLOR_FG,
                    activebackground=Config.COLOR_ACTIVE_BG,
                    activeforeground=Config.COLOR_FG,
                    highlightbackground=Config.COLOR_BG,
                )
            elif isinstance(widget, tk.Entry):
                widget.configure(
                    background=Config.COLOR_ENTRY_BG,
                    foreground=Config.COLOR_FG,
                    insertbackground=Config.COLOR_FG,
                )
            elif isinstance(widget, tk.Scale):
                widget.configure(
                    background=Config.COLOR_BG,
                    foreground=Config.COLOR_FG,
                    activebackground=Config.COLOR_ACTIVE_BG,
                    highlightbackground=Config.COLOR_BG,
                )
        except tk.TclError:
            pass

        for child in widget.winfo_children():
            self._style_widget_tree(child)
