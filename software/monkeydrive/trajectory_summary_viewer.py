"""Summary window for all motor trajectory views."""

from __future__ import annotations

import math
from typing import Iterable

import tkinter as tk
from tkinter import messagebox
import numpy as np

from .anatomy import AnatomyDependencyError, AnatomyLoader, AnatomySliceRenderer, AnatomyVolume
from .config import Config
from .models import MotorState, TrajectoryState


class TrajectorySummaryViewer(tk.Toplevel):
    """Grid of trajectory views across all motors."""

    INLINE = 0
    INLINE_90 = 1

    def __init__(
        self,
        parent: tk.Tk,
        motor_states: Iterable[MotorState],
        anatomy_path: str,
    ):
        super().__init__(parent)

        self._motor_states = {
            state.motor_id: MotorState.from_dict(state.to_dict())
            for state in motor_states
        }
        self._anatomy_path = anatomy_path
        self._volume: AnatomyVolume | None = None
        self._status_var = tk.StringVar(value="Loading anatomy")
        self._view_vars: dict[int, tk.IntVar] = {}
        self._canvases: dict[int, tk.Canvas] = {}
        self._image_items: dict[int, int] = {}
        self._image_refs: dict[int, tk.PhotoImage] = {}

        self.title("Trajectory Summary")
        self.geometry("1180x760")
        self.minsize(640, 420)

        parent_x = parent.winfo_x()
        parent_y = parent.winfo_y()
        parent_width = parent.winfo_width()
        self.geometry(f"+{parent_x + parent_width + 20}+{parent_y + 30}")

        self._build_ui()
        self._style_widget_tree()
        self.after(100, self._load_anatomy)

    def _build_ui(self):
        container = tk.Frame(self, padx=12, pady=12)
        container.pack(fill=tk.BOTH, expand=True)

        self._grid_frame = tk.Frame(container)
        self._grid_frame.pack(fill=tk.BOTH, expand=True)

        motor_ids = sorted(self._motor_states)
        columns = max(1, math.ceil(math.sqrt(max(1, len(motor_ids)))))
        for index, motor_id in enumerate(motor_ids):
            row = index // columns
            column = index % columns
            tile = tk.Frame(self._grid_frame, relief=tk.SUNKEN, borderwidth=1, padx=6, pady=6)
            tile.grid(row=row, column=column, sticky="nsew", padx=4, pady=4)
            self._grid_frame.grid_rowconfigure(row, weight=1, uniform="summary_rows")
            self._grid_frame.grid_columnconfigure(column, weight=1, uniform="summary_cols")

            header = tk.Frame(tile)
            header.pack(fill=tk.X)
            tk.Label(header, text=f"Motor {motor_id}", font=("TkDefaultFont", 11, "bold")).pack(side=tk.LEFT)
            self._view_vars[motor_id] = tk.IntVar(value=self.INLINE)
            tk.Radiobutton(
                header, text="Inline", variable=self._view_vars[motor_id], value=self.INLINE,
                command=lambda mid=motor_id: self._render_motor(mid)
            ).pack(side=tk.LEFT, padx=(12, 2))
            tk.Radiobutton(
                header, text="Inline 90", variable=self._view_vars[motor_id], value=self.INLINE_90,
                command=lambda mid=motor_id: self._render_motor(mid)
            ).pack(side=tk.LEFT, padx=2)

            canvas = tk.Canvas(tile, width=310, height=260, background="#000000", highlightthickness=0)
            canvas.pack(fill=tk.BOTH, expand=True, pady=(4, 0))
            canvas.create_text(155, 130, text="Loading anatomy", fill="#ffffff", tags=("placeholder",))
            canvas.bind("<Configure>", lambda _event, mid=motor_id: self._render_motor(mid))
            self._canvases[motor_id] = canvas

        tk.Label(container, textvariable=self._status_var, anchor="w").pack(fill=tk.X, pady=(8, 0))

    def _load_anatomy(self):
        if not self._anatomy_path:
            self._status_var.set("No anatomy loaded")
            return

        try:
            self._volume = AnatomyLoader.load(self._anatomy_path)
        except AnatomyDependencyError as exc:
            messagebox.showwarning("Missing MRI Dependencies", str(exc), parent=self)
            self._status_var.set("Could not load anatomy")
            return
        except Exception as exc:
            messagebox.showerror("Trajectory Summary Load Failed", str(exc), parent=self)
            self._status_var.set("Could not load anatomy")
            return

        self._status_var.set(self._volume.display_name)
        self._render_all()

    def update_motor_position(self, motor_id: int, position: float):
        state = self._motor_states.get(int(motor_id))
        if not state:
            return
        state.absolute = round(float(position), 3)
        self._render_motor(state.motor_id)

    def update_motor_state(self, state: MotorState):
        self._motor_states[state.motor_id] = MotorState.from_dict(state.to_dict())
        self._render_motor(state.motor_id)

    def _render_all(self):
        for motor_id in list(self._canvases):
            self._render_motor(motor_id)

    def _render_motor(self, motor_id: int):
        if not self._volume:
            return
        canvas = self._canvases.get(motor_id)
        state = self._motor_states.get(motor_id)
        if not canvas or not state or not canvas.winfo_exists():
            return

        trajectory = state.trajectory
        try:
            center, horizontal, vertical, width_mm, height_mm = self._slice_geometry(motor_id, trajectory)
            image_data = AnatomySliceRenderer.oblique_slice_to_ppm(
                self._volume,
                center,
                horizontal,
                vertical,
                width_mm,
                height_mm,
                max_size=max(220, min(canvas.winfo_width(), canvas.winfo_height()) - 10),
                zoom=trajectory.zoom,
            )
            image_ref = tk.PhotoImage(data=image_data, format="PPM")
            self._draw_motor(canvas, motor_id, state, image_ref, height_mm)
            self._image_refs[motor_id] = image_ref
        except tk.TclError:
            return
        except Exception as exc:
            canvas.delete("all")
            canvas.create_text(
                canvas.winfo_width() // 2,
                canvas.winfo_height() // 2,
                text=f"Could not render:\n{exc}",
                fill="#ffffff",
            )

    def _slice_geometry(self, motor_id: int, trajectory: TrajectoryState):
        transverse_x, transverse_y, probe_axis = self._trajectory_basis(trajectory)
        origin = (trajectory.x, trajectory.y, 0.0)
        extent_x, extent_y, extent_z = self._world_extents_mm()

        view_axis = self._view_vars.get(motor_id)
        if not view_axis or view_axis.get() == self.INLINE:
            return origin, transverse_y, probe_axis, extent_y, extent_z
        return origin, transverse_x, probe_axis, extent_x, extent_z

    def _trajectory_basis(self, trajectory: TrajectoryState):
        x_axis = np.asarray([1.0, 0.0, 0.0])
        y_axis = np.asarray([0.0, 1.0, 0.0])
        z_axis = np.asarray([0.0, 0.0, 1.0])

        r_ap = AnatomySliceRenderer.rotation_matrix(tuple(x_axis), trajectory.ap_rotation)
        r_ml = AnatomySliceRenderer.rotation_matrix(tuple(y_axis), -trajectory.ml_rotation)
        r_tilt = r_ml @ r_ap
        probe_axis = r_tilt @ z_axis
        transverse_x = r_tilt @ x_axis
        transverse_y = r_tilt @ y_axis

        r_twist = AnatomySliceRenderer.rotation_matrix(tuple(probe_axis), -trajectory.twist_rotation)
        return r_twist @ transverse_x, r_twist @ transverse_y, probe_axis

    def _world_extents_mm(self):
        if not self._volume:
            return (100.0, 100.0, 100.0)
        if self._volume.voxel_sizes:
            return tuple(
                max(1.0, self._volume.shape[i] * float(self._volume.voxel_sizes[i]))
                for i in range(3)
            )
        return tuple(float(s) for s in self._volume.shape)

    def _draw_motor(self, canvas: tk.Canvas, motor_id: int, state: MotorState,
                    image_ref: tk.PhotoImage, height_mm: float):
        canvas.update_idletasks()
        width = max(1, canvas.winfo_width())
        height = max(1, canvas.winfo_height())
        image_width = image_ref.width()
        image_height = image_ref.height()
        left = (width - image_width) / 2
        top = (height - image_height) / 2
        center_x = left + (image_width / 2)
        current_y = top + self._offset_to_image_y(
            image_height, height_mm, self._active_depth_offset(state)
        )

        canvas.delete("placeholder")
        canvas.delete("summary_overlay")
        if motor_id in self._image_items:
            canvas.itemconfigure(self._image_items[motor_id], image=image_ref)
            canvas.coords(self._image_items[motor_id], width / 2, height / 2)
        else:
            self._image_items[motor_id] = canvas.create_image(
                width / 2, height / 2, image=image_ref, anchor=tk.CENTER
            )

        canvas.create_line(center_x, top, center_x, top + image_height, fill="#00cc33", width=1, tags=("summary_overlay",))
        canvas.create_line(left, current_y, left + image_width, current_y, fill="#00cc33", width=1, tags=("summary_overlay",))

        if state.trajectory.target_location_set:
            target_y = top + self._offset_to_image_y(
                image_height, height_mm, state.trajectory.target_offset
            )
            canvas.create_line(left, target_y, left + image_width, target_y, fill="#ff3333", width=2, tags=("summary_overlay",))

        if state.trajectory.probe_location_set:
            bottom_y = top + self._offset_to_image_y(
                image_height, height_mm, self._active_depth_offset(state)
            )
            canvas.create_line(center_x, top, center_x, bottom_y, fill="#1f6fff", width=4, tags=("summary_overlay",))

    def _active_depth_offset(self, state: MotorState) -> float:
        trajectory = state.trajectory
        if trajectory.probe_location_set:
            return trajectory.probe_offset - (state.absolute - trajectory.probe_motor_position)
        return trajectory.perpendicular_offset

    def _offset_to_image_y(self, image_height: int, height_mm: float, offset: float) -> float:
        half_height = max(float(height_mm) / 2.0, 1.0)
        normalized = (half_height - float(offset)) / (2.0 * half_height)
        normalized = max(0.0, min(1.0, normalized))
        return normalized * image_height

    def _style_widget_tree(self, widget=None):
        widget = widget or self

        try:
            if isinstance(widget, (tk.Tk, tk.Toplevel, tk.Frame)):
                widget.configure(background=Config.COLOR_BG)
            elif isinstance(widget, tk.Canvas):
                widget.configure(background="#000000", highlightthickness=0)
            elif isinstance(widget, tk.Label):
                widget.configure(background=Config.COLOR_BG, foreground=Config.COLOR_FG)
            elif isinstance(widget, (tk.Button, tk.Radiobutton)):
                widget.configure(
                    background=Config.COLOR_BG,
                    foreground=Config.COLOR_FG,
                    activebackground=Config.COLOR_ACTIVE_BG,
                    activeforeground=Config.COLOR_FG,
                    highlightbackground=Config.COLOR_BG,
                )
        except tk.TclError:
            return

        for child in widget.winfo_children():
            self._style_widget_tree(child)
