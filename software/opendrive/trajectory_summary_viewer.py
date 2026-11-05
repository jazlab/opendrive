"""Summary window for all motor trajectory views."""

from __future__ import annotations

import io
import math
import queue
import threading
from typing import Iterable

import tkinter as tk
import numpy as np
from PIL import Image, ImageTk

from .viewer_tasks import LatestWorker

from .anatomy import (
    AnatomyLoader,
    AnatomyOrientation,
    AnatomySliceRenderer,
    AnatomyVolume,
)
from .config import Config
from .console_theme import ConsoleTheme as Theme
from .viewer_theme import configure_viewer, style_viewer, viewer_button, viewer_heading
from .models import MotorState, TrajectoryState
from .probe_map_colors import load_shank_lut
from .probe_overlay import ProbeOverlayRenderer, probe_body_points
from .spikeglx_client import SpikeGLXConfig, SpikeGLXLiveClient, SpikeGLXUnavailable


class _TrajectorySummaryView:
    """Grid of trajectory views across all motors."""

    INLINE = 0
    INLINE_90 = 1

    def __init__(
        self,
        parent: tk.Tk,
        motor_states: Iterable[MotorState],
        anatomy_path: str,
        orientation_actual: str = "",
        orientation_scanner: str = "",
        managed: bool = False,
    ):
        super().__init__(parent)
        configure_viewer(self)
        self._active = not managed

        self._motor_states = {
            state.motor_id: MotorState.from_dict(state.to_dict())
            for state in motor_states
        }
        self._anatomy_path = anatomy_path
        self._orientation_actual_key = orientation_actual
        self._orientation_scanner_key = orientation_scanner
        self._original_volume = None
        self._volume: AnatomyVolume | None = None
        self._render_worker = LatestWorker(self)
        self._render_signatures = {}
        self._source_images = {}
        self._source_signatures = {}
        self._status_var = tk.StringVar(value="Loading anatomy")
        self._view_vars: dict[int, tk.IntVar] = {}
        self._canvases: dict[int, tk.Canvas] = {}
        self._image_items: dict[int, int] = {}
        self._image_refs: dict[int, tk.PhotoImage] = {}
        self._image_heights_mm: dict[int, float] = {}
        self._probe_map_events: queue.Queue = queue.Queue()
        self._probe_map_stop_events: dict[tuple[int, int], threading.Event] = {}
        self._probe_map_generations: dict[tuple[int, int], int] = {}
        self._probe_map_intervals_ms: dict[tuple[int, int], int] = {}
        self._probe_map_values_mv: dict[tuple[int, int], list[float]] = {}
        self._probe_map_depths_mm: dict[tuple[int, int], list[float]] = {}
        self._probe_map_x_mm = {}
        self._probe_map_shanks = {}
        self._probe_map_image_refs = {}
        self._probe_map_items = {}
        self._probe_map_layout_keys = {}
        self._probe_map_renderers = {}
        self._probe_map_pending = set()
        self._probe_map_draw_job = None
        self._probe_map_lut = load_shank_lut()
        self._probe_map_channel_pitch_mm = 0.01

        if isinstance(self, tk.Toplevel):
            self.title("Trajectory Summary")
            self.geometry("1180x760")
            self.minsize(640, 420)

            parent_x = parent.winfo_x()
            parent_y = parent.winfo_y()
            parent_width = parent.winfo_width()
            self.geometry(f"+{parent_x + parent_width + 20}+{parent_y + 30}")

        self._build_ui()
        self._style_widget_tree()
        self._probe_poll_job = self.after(16, self._poll_probe_map_events)
        self._sync_probe_map_workers()
        if not managed:
            self.after(100, self._load_anatomy)

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
            self._sync_probe_map_workers()
            self._render_all()
        else:
            self._render_worker.invalidate()
            self._render_signatures.clear()
            self._stop_all_probe_map_workers()

    def set_motor_states(self, states):
        self._cancel_probe_map_draw()
        self._probe_map_image_refs.clear()
        self._probe_map_items.clear()
        self._probe_map_layout_keys.clear()
        self._probe_map_renderers.clear()
        self._render_worker.invalidate()
        self._stop_all_probe_map_workers()
        self._motor_states = {state.motor_id: MotorState.from_dict(state.to_dict()) for state in states}
        for child in self.winfo_children():
            child.destroy()
        self._view_vars.clear()
        self._canvases.clear()
        self._image_items.clear()
        self._image_refs.clear()
        self._render_signatures.clear()
        self._source_signatures.clear()
        self._source_images.clear()
        self._build_ui()
        self._style_widget_tree()
        self._sync_probe_map_workers()
        self._render_all()

    def destroy(self):
        self._cancel_probe_map_draw()
        if getattr(self, '_probe_poll_job', None) is not None:
            self.after_cancel(self._probe_poll_job)
            self._probe_poll_job = None
        self._render_worker.close()
        self._stop_all_probe_map_workers()
        try:
            super().destroy()
        except tk.TclError:
            pass

    def _build_ui(self):
        container = tk.Frame(self, padx=20, pady=18)
        container.pack(fill=tk.BOTH, expand=True)
        viewer_heading(container, 'Summary', 'Compare probe positions across drives.')

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
            tk.Label(header, text=f"Drive {motor_id}", font=("TkDefaultFont", 11, "bold")).pack(side=tk.LEFT)
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

    def update_motor_position(self, motor_id: int, position: float):
        state = self._motor_states.get(int(motor_id))
        if not state:
            return
        state.absolute = round(float(position), 3)
        self._redraw_motor_overlay(state.motor_id, refresh_probe_map=False)

    def update_motor_state(self, state: MotorState):
        self._motor_states[state.motor_id] = MotorState.from_dict(state.to_dict())
        self._sync_probe_map_workers()
        self._render_motor(state.motor_id)

    @staticmethod
    def _probe_stream_key(state):
        trajectory = state.trajectory
        return max(0, int(trajectory.probe_map_probe)), max(100, int(trajectory.probe_map_update_ms) // 100 * 100)

    def _sync_probe_map_workers(self):
        if not self._active:
            return
        # Share only identical probe/window pairs. Different requested windows
        # must not silently use the fastest drive's sample window.
        wanted = {self._probe_stream_key(state) for state in self._motor_states.values()
                  if state.trajectory.probe_map_enabled}
        for stream in list(self._probe_map_stop_events):
            if stream not in wanted:
                self._stop_probe_map_worker(stream)
        for stream in wanted:
            if stream not in self._probe_map_stop_events:
                self._start_probe_map_worker(stream, stream[1])

    def _start_probe_map_worker(self, stream, update_ms):
        self._stop_probe_map_worker(stream)
        generation = self._probe_map_generations.get(stream, 0) + 1
        self._probe_map_generations[stream] = generation
        self._probe_map_intervals_ms[stream] = update_ms
        stop_event = threading.Event()
        self._probe_map_stop_events[stream] = stop_event
        threading.Thread(target=self._probe_map_worker_loop,
            args=(stream, generation, stop_event, update_ms / 1000.), daemon=True).start()

    def _stop_probe_map_worker(self, stream):
        self._probe_map_generations[stream] = self._probe_map_generations.get(stream, 0) + 1
        stop_event = self._probe_map_stop_events.pop(stream, None)
        if stop_event is not None:
            stop_event.set()
        self._probe_map_intervals_ms.pop(stream, None)
        for cache in (self._probe_map_values_mv, self._probe_map_depths_mm,
                      self._probe_map_x_mm, self._probe_map_shanks):
            cache.pop(stream, None)

    def _stop_all_probe_map_workers(self):
        for stream in list(self._probe_map_stop_events):
            self._stop_probe_map_worker(stream)

    def _probe_map_worker_loop(self, stream, generation, stop_event, update_interval):
        client = None
        try:
            client = SpikeGLXLiveClient(SpikeGLXConfig(ip=stream[0], prefer_filtered=True, compute_diagnostics=False))
            client.connect()
            for snapshot in client.iter_heatmaps(stop_event, round(update_interval * 1000)):
                self._probe_map_events.put(('snapshot', stream, generation, snapshot))
        except Exception as exc:
            self._probe_map_events.put(('error', stream, generation, f'Probe map failed: {exc}'))
        finally:
            if client is not None:
                client.close()

    def _poll_probe_map_events(self):
        latest = {}
        while True:
            try:
                event = self._probe_map_events.get_nowait()
            except queue.Empty:
                break
            kind, stream, generation = event[:3]
            if generation != self._probe_map_generations.get(stream):
                continue
            if kind == 'snapshot':
                latest[stream] = event
            elif kind == 'error':
                latest.pop(stream, None)
                self._status_var.set(str(event[3]))
                for cache in (self._probe_map_values_mv, self._probe_map_depths_mm,
                              self._probe_map_x_mm, self._probe_map_shanks):
                    cache.pop(stream, None)
                self._render_probe_map_motors(stream)
        for stream, event in latest.items():
            snapshot = event[3]
            self._probe_map_values_mv[stream] = list(snapshot.peak_to_peak_mv)
            self._probe_map_depths_mm[stream] = list(getattr(snapshot, 'channel_depths_mm', []))
            self._probe_map_x_mm[stream] = list(getattr(snapshot, 'channel_x_mm', []))
            self._probe_map_shanks[stream] = list(getattr(snapshot, 'channel_shanks', []))
            self._render_probe_map_motors(stream)
        if self.winfo_exists():
            self._probe_poll_job = self.after(16, self._poll_probe_map_events)

    def _render_probe_map_motors(self, stream):
        for motor_id, state in self._motor_states.items():
            if state.trajectory.probe_map_enabled and self._probe_stream_key(state) == stream:
                self._schedule_probe_map_draw(motor_id)

    def _probe_map_layout_key(self, motor_id):
        state = self._motor_states[motor_id]
        trajectory = state.trajectory
        canvas = self._canvases[motor_id]
        return (id(self._image_refs.get(motor_id)), canvas.winfo_width(), canvas.winfo_height(),
                trajectory.probe_location_set, trajectory.probe_map_enabled, self._probe_stream_key(state),
                trajectory.probe_width_px, trajectory.probe_map_smooth, trajectory.probe_map_smoothing_um,
                trajectory.probe_map_min_mv, trajectory.probe_map_max_mv)

    def _schedule_probe_map_draw(self, motor_id):
        if not self._active:
            return
        self._probe_map_pending.add(motor_id)
        if self._probe_map_draw_job is None:
            self._probe_map_draw_job = self.after(0, self._flush_probe_map_draw)

    def _cancel_probe_map_draw(self):
        if self._probe_map_draw_job is not None:
            self.after_cancel(self._probe_map_draw_job)
            self._probe_map_draw_job = None
        self._probe_map_pending.clear()

    def _flush_probe_map_draw(self):
        self._probe_map_draw_job = None
        pending, self._probe_map_pending = sorted(self._probe_map_pending), set()
        if not self._active:
            return
        prepared = {}
        for motor_id in pending:
            state = self._motor_states.get(motor_id)
            canvas, mri = self._canvases.get(motor_id), self._image_refs.get(motor_id)
            height_mm = self._image_heights_mm.get(motor_id)
            if state is None or canvas is None or mri is None or height_mm is None:
                continue
            stream = self._probe_stream_key(state)
            values = self._probe_map_values_mv.get(stream, [])
            if not (state.trajectory.probe_map_enabled and state.trajectory.probe_location_set and values):
                prepared[motor_id] = (None, 0, 0)
                continue
            renderer = self._probe_map_renderers.get(motor_id)
            if renderer is None:
                renderer = self._probe_map_renderers[motor_id] = ProbeOverlayRenderer(self._probe_map_lut)
            bottom = self._offset_to_image_y(mri.height(), height_mm, self._active_depth_offset(state))
            raster = renderer.render(state.trajectory, values, self._probe_map_depths_mm.get(stream, []),
                self._probe_map_x_mm.get(stream, []), self._probe_map_shanks.get(stream, []),
                mri.height(), height_mm, bottom)
            prepared[motor_id] = (ImageTk.PhotoImage(raster, master=self),
                canvas.winfo_width()/2-raster.width/2, (canvas.winfo_height()-mri.height())/2)
        previous_refs = self._probe_map_image_refs
        self._probe_map_image_refs = {key: value for key, value in previous_refs.items() if key not in prepared}
        self._probe_map_image_refs.update({key: frame[0] for key, frame in prepared.items() if frame[0] is not None})
        for motor_id, (photo, x, y) in prepared.items():
            canvas = self._canvases[motor_id]
            self._probe_map_layout_keys[motor_id] = self._probe_map_layout_key(motor_id)
            if photo is None:
                canvas.delete('summary_probe_map')
                self._probe_map_items.pop(motor_id, None)
                continue
            item = self._probe_map_items.get(motor_id)
            if item is None:
                self._probe_map_items[motor_id] = canvas.create_image(x, y, image=photo, anchor='nw', tags='summary_probe_map')
            else:
                canvas.itemconfigure(item, image=photo)
                canvas.coords(item, x, y)
            canvas.tag_raise('summary_probe_map')

    def _render_all(self):
        for motor_id in list(self._canvases):
            self._render_motor(motor_id)

    def _render_motor(self, motor_id: int):
        if not self._active:
            return
        if not self._volume:
            return
        canvas = self._canvases.get(motor_id)
        state = self._motor_states.get(motor_id)
        if not canvas or not state or not canvas.winfo_exists():
            return

        trajectory = state.trajectory
        geometry = self._slice_geometry(motor_id, trajectory)
        volume = self._volume
        base_signature = (id(volume), *(tuple(v) if hasattr(v, '__iter__') else v for v in geometry))
        size = max(220, min(canvas.winfo_width(), canvas.winfo_height()) - 10)
        zoom = max(0.5, min(10.0, trajectory.zoom))
        signature = (base_signature, size, zoom)
        if self._render_signatures.get(motor_id) == signature:
            self._redraw_motor_overlay(motor_id)
            return
        self._render_signatures[motor_id] = signature
        cached = self._source_images.get(motor_id) if self._source_signatures.get(motor_id) == base_signature else None

        def compute():
            source = cached
            if source is None:
                data = AnatomySliceRenderer.oblique_slice_to_ppm(volume, *geometry, max_size=430, zoom=1.0)
                source = Image.open(io.BytesIO(data)).copy()
            scale = size * zoom / 430.0
            scaled = source.resize((max(1, round(source.width * scale)), max(1, round(source.height * scale))), Image.Resampling.NEAREST)
            return source, scaled

        def complete(result, error):
            if error is not None:
                self._render_signatures.pop(motor_id, None)
                self._status_var.set(f"Could not render Motor {motor_id}: {error}")
                return
            source, scaled = result
            image_ref = ImageTk.PhotoImage(scaled, master=self)
            self._source_images[motor_id] = source
            self._source_signatures[motor_id] = base_signature
            self._image_refs[motor_id] = image_ref
            self._image_heights_mm[motor_id] = geometry[4]
            # Read the newest state here, not the position when rendering started.
            self._draw_motor(canvas, motor_id, self._motor_states[motor_id], image_ref, geometry[4])

        self._render_worker.submit(('plane', motor_id), compute, complete)

    def _redraw_motor_overlay(self, motor_id: int, refresh_probe_map=True):
        if not self._active:
            return
        canvas = self._canvases.get(motor_id)
        state = self._motor_states.get(motor_id)
        image_ref = self._image_refs.get(motor_id)
        height_mm = self._image_heights_mm.get(motor_id)
        if not canvas or not state or not image_ref or height_mm is None:
            return
        try:
            if canvas.winfo_exists():
                self._draw_motor(canvas, motor_id, state, image_ref, height_mm, refresh_probe_map)
        except tk.TclError:
            return

    def _slice_geometry(self, motor_id: int, trajectory: TrajectoryState):
        transverse_x, transverse_y, probe_axis = self._trajectory_basis(trajectory)
        origin = self._trajectory_origin(trajectory)
        extent_x, extent_y, extent_z = self._world_extents_mm()

        view_axis = self._view_vars.get(motor_id)
        if not view_axis or view_axis.get() == self.INLINE:
            return self._oriented_slice_geometry(origin, transverse_y, probe_axis, extent_y, extent_z)
        return self._oriented_slice_geometry(origin, transverse_x, probe_axis, extent_x, extent_z)

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

    def _trajectory_origin(self, trajectory: TrajectoryState):
        return (trajectory.x, trajectory.y, trajectory.z)

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

        r_twist = AnatomySliceRenderer.rotation_matrix(tuple(probe_axis), trajectory.twist_rotation)
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
                    image_ref: tk.PhotoImage, height_mm: float, refresh_probe_map=True):
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
            if bottom_y > top:
                canvas.create_polygon(*probe_body_points(center_x, top, bottom_y, state.trajectory.probe_width_px,
                    min(6., .4 * image_height / max(float(height_mm), 1.))),
                    fill='#b5b5b5', outline='#777777', tags=('summary_overlay', 'probe_body'))
        canvas.tag_raise('summary_probe_map')
        if refresh_probe_map and self._probe_map_layout_keys.get(motor_id) != self._probe_map_layout_key(motor_id):
            self._schedule_probe_map_draw(motor_id)

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
        style_viewer(self, widget)


class TrajectorySummaryViewer(_TrajectorySummaryView, tk.Toplevel):
    """Standalone summary window."""


class TrajectorySummaryPanel(_TrajectorySummaryView, tk.Frame):
    """Embeddable summary for the shared Trajectories window."""
