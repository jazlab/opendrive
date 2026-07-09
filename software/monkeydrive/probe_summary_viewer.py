"""Simple live SpikeGLX probe-map viewer."""

from __future__ import annotations

import queue
import threading
import time

import tkinter as tk

from .config import Config
from .probe_map_colors import color_from_lut, load_shank_lut
from .spikeglx_client import SpikeGLXConfig, SpikeGLXLiveClient, SpikeGLXUnavailable


class ProbeSummaryViewer(tk.Toplevel):
    """Show one live probe map using the same data path as the trajectory viewer."""

    def __init__(self, parent: tk.Tk):
        super().__init__(parent)

        self._events: queue.Queue = queue.Queue()
        self._stop_events: dict[int, threading.Event] = {}
        self._generation = 0
        self._worker_running = False
        self._active_update_ms: int | None = None
        self._probe_count = 0
        self._started_probes: set[int] = set()
        self._values_by_probe: dict[int, list[float]] = {}
        self._x_by_probe: dict[int, list[float]] = {}
        self._depths_by_probe: dict[int, list[float]] = {}
        self._update_counts: dict[int, int] = {}
        self._last_status_time = 0.0
        self._lut = load_shank_lut()

        self._min_var = tk.StringVar(value="0")
        self._max_var = tk.StringVar(value="2.5")
        self._update_ms_var = tk.StringVar(value="500")
        self._status_var = tk.StringVar(value="Probe map off")
        self._data_label_var = tk.StringVar(value="")

        self.title("Probe Summary")
        self.geometry("900x820")
        self.minsize(520, 520)

        parent_x = parent.winfo_x()
        parent_y = parent.winfo_y()
        parent_width = parent.winfo_width()
        self.geometry(f"+{parent_x + parent_width + 20}+{parent_y + 60}")

        self._build_ui()
        self._style_widget_tree()
        self.after(100, self._poll_events)
        self.after(150, self._restart)

    def destroy(self):
        self._stop_worker()
        try:
            super().destroy()
        except tk.TclError:
            pass

    def _build_ui(self):
        container = tk.Frame(self, padx=12, pady=12)
        container.pack(fill=tk.BOTH, expand=True)

        toolbar = tk.Frame(container)
        toolbar.pack(fill=tk.X, pady=(0, 8))

        self._add_field(toolbar, "Min mV:", self._min_var, width=7)
        self._add_field(toolbar, "Max mV:", self._max_var, width=7)
        self._add_field(toolbar, "Update ms:", self._update_ms_var, width=7)
        tk.Button(toolbar, text="Apply", command=self._apply_settings, width=8).pack(side=tk.LEFT, padx=(6, 0))
        tk.Button(toolbar, text="Retry", command=self._restart, width=8).pack(side=tk.LEFT, padx=(6, 0))

        self._canvas = tk.Canvas(container, width=760, height=680, background="#dddddd", highlightthickness=0)
        self._canvas.pack(fill=tk.BOTH, expand=True)
        self._canvas.create_text(140, 340, text="Waiting for data", fill="#333333", tags=("probe_map",))
        self._canvas.bind("<Configure>", lambda _event: self._draw_probe())

        tk.Label(container, textvariable=self._data_label_var, anchor="w").pack(fill=tk.X, pady=(8, 0))
        tk.Label(container, textvariable=self._status_var, anchor="w").pack(fill=tk.X, pady=(8, 0))

    def _add_field(self, parent: tk.Frame, label: str, variable: tk.StringVar, width: int):
        tk.Label(parent, text=label).pack(side=tk.LEFT)
        entry = tk.Entry(parent, textvariable=variable, width=width)
        entry.pack(side=tk.LEFT, padx=(4, 8))
        entry.bind("<Return>", lambda _event: (self._apply_settings(), "break")[1])
        entry.bind("<KP_Enter>", lambda _event: (self._apply_settings(), "break")[1])

    def _apply_settings(self):
        update_ms = self._update_ms()
        self._draw_probe()
        if self._worker_running and self._active_update_ms == update_ms:
            return
        self._restart()

    def _restart(self):
        self._stop_worker()
        self._start_worker()

    def _start_worker(self):
        self._values_by_probe = {}
        self._x_by_probe = {}
        self._depths_by_probe = {}
        self._update_counts = {}
        self._probe_count = 0
        self._started_probes = set()
        self._data_label_var.set("")
        self._draw_probe()

        update_ms = self._update_ms()
        self._generation += 1
        generation = self._generation
        self._worker_running = True
        self._active_update_ms = update_ms
        self._status_var.set("Connecting to SpikeGLX filtered AP probes...")
        self._start_probe_worker(0, generation, update_ms)

    def _start_probe_worker(self, probe: int, generation: int, update_ms: int):
        if probe in self._started_probes:
            return
        self._started_probes.add(probe)
        stop_event = threading.Event()
        self._stop_events[probe] = stop_event
        update_interval = max(0.1, update_ms / 1000.0)
        config = SpikeGLXConfig(
            ip=probe,
            samples_per_fetch=300,
            prefer_filtered=True,
            include_geometry=True,
            geometry_refresh_s=0.0,
            compute_diagnostics=False,
            use_numpy=False,
        )
        thread = threading.Thread(
            target=self._worker_loop,
            args=(generation, stop_event, config, update_interval),
            daemon=True,
        )
        thread.start()

    def _stop_worker(self):
        if self._worker_running:
            self._generation += 1
        for stop_event in self._stop_events.values():
            stop_event.set()
        self._stop_events.clear()
        self._worker_running = False

    def _worker_loop(
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
            self._events.put(("connected", generation, config.ip, info.probe_count))
            snapshot = client.fetch_snapshot()
            self._events.put(("snapshot", generation, config.ip, snapshot))
            while not stop_event.wait(update_interval):
                snapshot = client.fetch_snapshot()
                self._events.put(("snapshot", generation, config.ip, snapshot))
        except SpikeGLXUnavailable as exc:
            self._events.put(("error", generation, config.ip, str(exc)))
        except Exception as exc:
            self._events.put(("error", generation, config.ip, f"Probe map failed: {exc}"))
        finally:
            if client is not None:
                client.close()
            self._events.put(("done", generation, config.ip))

    def _poll_events(self):
        latest_snapshots: dict[int, tuple] = {}
        while True:
            try:
                event = self._events.get_nowait()
            except queue.Empty:
                break
            if event[0] == "snapshot":
                latest_snapshots[int(event[2])] = event
                continue
            self._handle_event(event)

        drew_snapshot = False
        for event in latest_snapshots.values():
            if event[1] == self._generation:
                self._handle_event(event, draw=False)
                drew_snapshot = True
        if drew_snapshot:
            self._update_data_label()
            self._draw_probe()
        if self.winfo_exists():
            self.after(100, self._poll_events)

    def _handle_event(self, event: tuple, draw: bool = True):
        kind = event[0]
        generation = event[1]
        if generation != self._generation:
            return
        if kind == "connected":
            probe = int(event[2])
            probe_count = max(1, int(event[3]))
            if probe_count > self._probe_count:
                self._probe_count = probe_count
            self._status_var.set(f"Direct SpikeGLX filtered AP: {len(self._started_probes)}/{self._probe_count} probes")
            if probe == 0:
                for next_probe in range(1, self._probe_count):
                    self._start_probe_worker(next_probe, self._generation, self._active_update_ms or self._update_ms())
            if draw:
                self._draw_probe()
        elif kind == "snapshot":
            probe = int(event[2])
            snapshot = event[3]
            values = list(snapshot.peak_to_peak_mv)
            self._values_by_probe[probe] = values
            x_positions = list(getattr(snapshot, "channel_x_mm", []))
            depths = list(getattr(snapshot, "channel_depths_mm", []))
            if x_positions:
                self._x_by_probe[probe] = x_positions
            if depths:
                self._depths_by_probe[probe] = depths
            self._update_counts[probe] = self._update_counts.get(probe, 0) + 1
            now = time.monotonic()
            if not values:
                self._status_var.set(f"{snapshot.data_source}: probe {probe} fetched 0 channels")
            elif now - self._last_status_time >= 1.0:
                self._last_status_time = now
                self._status_var.set(f"{snapshot.data_source}: update at {time.strftime('%H:%M:%S')}")
            if draw:
                self._update_data_label()
                self._draw_probe()
        elif kind == "error":
            probe = int(event[2])
            self._values_by_probe.pop(probe, None)
            self._data_label_var.set("")
            self._status_var.set(f"Probe {probe}: {event[3]}")
            if draw:
                self._draw_probe()
        elif kind == "done":
            probe = int(event[2])
            self._stop_events.pop(probe, None)
            if not self._stop_events:
                self._worker_running = False

    def _update_data_label(self):
        if not self._values_by_probe:
            self._data_label_var.set("")
            return
        parts = []
        for probe in sorted(self._values_by_probe):
            values = self._values_by_probe[probe]
            if values:
                parts.append(f"P{probe}: {len(values)} ch, {min(values):.3f}-{max(values):.3f} mV")
        self._data_label_var.set("   ".join(parts))

    def _draw_probe(self):
        try:
            if not self._canvas.winfo_exists():
                return
            width = max(1, self._canvas.winfo_width())
            height = max(1, self._canvas.winfo_height())
            self._canvas.delete("probe_map")
            probe_count = max(
                1,
                self._probe_count,
                (max(self._values_by_probe) + 1) if self._values_by_probe else 0,
            )
            column_width = width / probe_count
            for probe in range(probe_count):
                column_left = probe * column_width
                column_right = column_left + column_width
                values = self._values_by_probe.get(probe, [])
                self._draw_probe_body(column_left, column_right, height, probe)
                if values:
                    self._draw_vertical_heatmap(
                        column_left,
                        column_right,
                        height,
                        values,
                        self._x_by_probe.get(probe, []),
                        self._depths_by_probe.get(probe, []),
                    )
                    self._canvas.create_text(
                        (column_left + column_right) / 2,
                        16,
                        text=f"P{probe}  {len(values)} ch",
                        fill="#111111",
                        tags=("probe_map",),
                    )
                else:
                    self._canvas.create_text(
                        (column_left + column_right) / 2,
                        height / 2,
                        text=f"P{probe}\nWaiting",
                        fill="#333333",
                        justify=tk.CENTER,
                        tags=("probe_map",),
                    )

            if not self._values_by_probe:
                self._canvas.create_text(
                    width / 2,
                    height - 18,
                    text="Waiting for data",
                    fill="#333333",
                    tags=("probe_map",),
                )
        except tk.TclError:
            return

    def _draw_probe_body(self, column_left: float, column_right: float, height: int, probe: int):
        column_width = max(1.0, column_right - column_left)
        center_x = (column_left + column_right) / 2.0
        body_width = min(120.0, max(54.0, column_width * 0.48))
        top = 16.0
        bottom = height - 34.0
        left = center_x - body_width / 2.0
        right = left + body_width
        self._canvas.create_polygon(
            left,
            top,
            right,
            top,
            right,
            bottom,
            center_x,
            height - 10.0,
            left,
            bottom,
            fill="#b5b5b5",
            outline="#777777",
            tags=("probe_map",),
        )
        self._canvas.create_line(left - 18, top + 5, right + 18, top + 5, fill="#777777", tags=("probe_map",))

    def _draw_vertical_heatmap(
        self,
        column_left: float,
        column_right: float,
        height: int,
        values: list[float],
        x_positions: list[float],
        depths: list[float],
    ):
        map_min, map_max = self._color_limits()
        top = 34.0
        bottom = height - 44.0
        available_height = max(1.0, bottom - top)
        column_width = max(1.0, column_right - column_left)
        map_width = min(92.0, max(44.0, column_width * 0.44))
        left = (column_left + column_right - map_width) / 2.0
        right = left + map_width
        x_values = x_positions[: len(values)] if len(x_positions) >= len(values) else []
        depth_values = depths[: len(values)] if len(depths) >= len(values) else []
        if x_values:
            min_x = min(x_values)
            max_x = max(x_values)
            x_span = max(1e-9, max_x - min_x)
        else:
            min_x = 0.0
            x_span = 1.0
        if depth_values:
            min_depth = min(depth_values)
            max_depth = max(depth_values)
            depth_span = max(1e-9, max_depth - min_depth)
            channel_height = max(1.0, available_height / max(1, len(values)))
        else:
            row_height = max(1.0, available_height / max(1, len(values)))
            channel_height = row_height
        unique_x = sorted(set(round(value, 6) for value in x_values)) if x_values else []
        if len(unique_x) > 1:
            contact_width = min(18.0, max(5.0, map_width / (len(unique_x) * 1.8)))
        else:
            contact_width = map_width

        for channel, value in enumerate(values):
            if depth_values:
                y_center = bottom - ((depth_values[channel] - min_depth) / depth_span) * available_height
                y0 = max(top, y_center - channel_height / 2.0)
                y1 = min(bottom, y_center + channel_height / 2.0)
            else:
                y1 = bottom - channel * row_height
                y0 = max(top, y1 - row_height)
                if y1 < top:
                    break
            if x_values:
                x_center = left + ((x_values[channel] - min_x) / x_span) * (right - left)
                x0 = max(left, x_center - contact_width / 2.0)
                x1 = min(right, x_center + contact_width / 2.0)
            else:
                x0 = left
                x1 = right
            color = color_from_lut(self._lut, value, map_min, map_max)
            self._canvas.create_rectangle(
                x0,
                y0,
                x1,
                y1,
                fill=color,
                outline=color,
                tags=("probe_map",),
            )

    def _color_limits(self) -> tuple[float, float]:
        try:
            min_value = float(self._min_var.get())
            max_value = float(self._max_var.get())
        except ValueError:
            min_value, max_value = 0.0, 2.5
        if max_value <= min_value:
            max_value = min_value + 1.0
        return min_value, max_value

    def _probe(self) -> int:
        try:
            return max(0, int(float(self._probe_var.get())))
        except ValueError:
            return 0

    def _update_ms(self) -> int:
        try:
            return max(100, int(float(self._update_ms_var.get())))
        except ValueError:
            return 500

    def _style_widget_tree(self, widget=None):
        widget = widget or self
        try:
            if isinstance(widget, (tk.Tk, tk.Toplevel, tk.Frame)):
                widget.configure(background=Config.COLOR_BG)
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
        except tk.TclError:
            return

        for child in widget.winfo_children():
            self._style_widget_tree(child)
