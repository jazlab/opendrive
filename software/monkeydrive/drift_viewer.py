"""Tk viewer for the first SpikeGLX live-data integration milestone."""

from __future__ import annotations

import queue
import threading
import time
import tkinter as tk
from tkinter import ttk
from typing import Callable, Optional

from .config import Config
from .spikeglx_client import (
    SpikeGLXConfig,
    SpikeGLXLiveClient,
    SpikeGLXStreamInfo,
    SpikeGLXSnapshot,
    SpikeGLXUnavailable,
    sdk_search_paths,
)


class DriftViewer(tk.Toplevel):
    """A read-only SpikeGLX connection and live-data diagnostics panel."""

    def __init__(self, master: tk.Misc, on_close: Optional[Callable[[], None]] = None):
        super().__init__(master)
        self.title("Neural Drift")
        self.configure(background=Config.COLOR_BG)
        self.resizable(False, False)
        self._on_close = on_close
        self._events: queue.Queue = queue.Queue()
        self._client: Optional[SpikeGLXLiveClient] = None
        self._worker_running = False
        self._stop_event = threading.Event()
        self._update_lock = threading.Lock()
        self._connect_generation = 0
        self._map_update_count = 0

        self._host_var = tk.StringVar(value="localhost")
        self._port_var = tk.StringVar(value="4142")
        self._probe_var = tk.StringVar(value="0")
        self._map_min_var = tk.StringVar(value="0.0")
        self._map_max_var = tk.StringVar(value="2.5")
        self._update_ms_var = tk.StringVar(value="500")
        self._update_interval_sec = 0.5
        self._status_var = tk.StringVar(value="Disconnected")
        self._detail_var = tk.StringVar(value="SpikeGLX is optional. Open it when you want live neural diagnostics.")
        self._fields: dict[str, tk.StringVar] = {}
        self._map_canvas: Optional[tk.Canvas] = None
        self._colorbar_canvas: Optional[tk.Canvas] = None
        self._map_items: list[int] = []
        self._map_height = 384
        self._map_width = 110

        self._build_ui()
        self._update_ms_var.trace_add("write", lambda *_args: self._update_update_interval())
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.after(100, self._poll_worker_events)
        self.after(250, self.connect_async)

    def close(self):
        """Stop any background reader and close the window."""
        self._stop_event.set()
        if self._on_close:
            self._on_close()
        try:
            self.destroy()
        except tk.TclError:
            pass

    def connect_async(self):
        """Start a non-blocking SpikeGLX connection attempt."""
        if self._worker_running:
            return
        self._connect_generation += 1
        self._stop_event.set()
        if self._client is not None:
            self._client.close()
            self._client = None

        self._stop_event = threading.Event()
        self._worker_running = True
        self._set_connecting()
        generation = self._connect_generation
        try:
            config = SpikeGLXConfig(
                host=self._host_var.get().strip() or "localhost",
                port=int(self._port_var.get().strip() or "4142"),
                ip=max(0, int(self._probe_var.get().strip() or "0")),
            )
        except ValueError:
            self._show_disconnected("Host, port, and probe must be valid before connecting.")
            self._worker_running = False
            return

        stop_event = self._stop_event
        thread = threading.Thread(target=self._worker_loop, args=(generation, config, stop_event), daemon=True)
        thread.start()

    def _build_ui(self):
        outer = tk.Frame(self, padx=14, pady=14, background=Config.COLOR_BG)
        outer.pack(fill=tk.BOTH, expand=True)

        top = tk.Frame(outer, background=Config.COLOR_BG)
        top.pack(fill=tk.X)
        tk.Label(
            top,
            text="Neural Drift",
            font=("Arial", 16, "bold"),
            background=Config.COLOR_BG,
            foreground=Config.COLOR_FG,
        ).pack(side=tk.LEFT)
        self._retry_btn = tk.Button(top, text="Retry", width=10, command=self.connect_async)
        self._retry_btn.pack(side=tk.RIGHT)

        status_frame = tk.Frame(outer, background=Config.COLOR_BG)
        status_frame.pack(fill=tk.X, pady=(10, 8))
        self._status_label = tk.Label(
            status_frame,
            textvariable=self._status_var,
            width=16,
            anchor="center",
            relief=tk.SOLID,
            borderwidth=1,
            background=Config.COLOR_READONLY_BG,
            foreground=Config.COLOR_FG,
            padx=8,
            pady=4,
        )
        self._status_label.pack(side=tk.LEFT)
        tk.Label(
            status_frame,
            textvariable=self._detail_var,
            wraplength=520,
            justify=tk.LEFT,
            background=Config.COLOR_BG,
            foreground=Config.COLOR_FG,
        ).pack(side=tk.LEFT, padx=(10, 0), fill=tk.X, expand=True)

        controls = tk.Frame(outer, background=Config.COLOR_BG)
        controls.pack(fill=tk.X, pady=(2, 10))
        self._labeled_entry(controls, "Host", self._host_var, 18).pack(side=tk.LEFT, padx=(0, 8))
        self._labeled_entry(controls, "Port", self._port_var, 8).pack(side=tk.LEFT, padx=(0, 8))
        self._labeled_entry(controls, "Probe", self._probe_var, 6).pack(side=tk.LEFT)

        scale_controls = tk.Frame(outer, background=Config.COLOR_BG)
        scale_controls.pack(fill=tk.X, pady=(0, 10))
        self._labeled_entry(scale_controls, "Map min mV", self._map_min_var, 8).pack(side=tk.LEFT, padx=(0, 8))
        self._labeled_entry(scale_controls, "Map max mV", self._map_max_var, 8).pack(side=tk.LEFT, padx=(0, 8))
        self._labeled_entry(scale_controls, "Update ms", self._update_ms_var, 8).pack(side=tk.LEFT, padx=(0, 8))
        tk.Label(
            scale_controls,
            text="dark = min, bright = max",
            background=Config.COLOR_BG,
            foreground=Config.COLOR_FG,
        ).pack(side=tk.LEFT)

        map_frame = tk.Frame(outer, background=Config.COLOR_BG)
        map_frame.pack(fill=tk.X, pady=(0, 10))
        map_labels = tk.Frame(map_frame, background=Config.COLOR_BG)
        map_labels.pack(side=tk.LEFT, fill=tk.Y, padx=(0, 8))
        tk.Label(
            map_labels,
            text="Ch 383",
            background=Config.COLOR_BG,
            foreground=Config.COLOR_FG,
        ).pack(anchor="e")
        tk.Label(
            map_labels,
            text="AP\npeak-peak mV",
            justify=tk.RIGHT,
            background=Config.COLOR_BG,
            foreground=Config.COLOR_FG,
        ).pack(expand=True, anchor="e")
        tk.Label(
            map_labels,
            text="Ch 0 deepest",
            background=Config.COLOR_BG,
            foreground=Config.COLOR_FG,
        ).pack(anchor="e")
        self._map_canvas = tk.Canvas(
            map_frame,
            width=self._map_width,
            height=self._map_height,
            background="#ffffff",
            highlightthickness=1,
            highlightbackground=Config.COLOR_READONLY_BG,
        )
        self._map_canvas.pack(side=tk.LEFT)

        colorbar_frame = tk.Frame(map_frame, background=Config.COLOR_BG)
        colorbar_frame.pack(side=tk.LEFT, padx=(18, 0), fill=tk.Y)
        tk.Label(
            colorbar_frame,
            text="max",
            background=Config.COLOR_BG,
            foreground=Config.COLOR_FG,
        ).pack()
        self._colorbar_canvas = tk.Canvas(
            colorbar_frame,
            width=42,
            height=180,
            background="#ffffff",
            highlightthickness=1,
            highlightbackground=Config.COLOR_READONLY_BG,
        )
        self._colorbar_canvas.pack()
        tk.Label(
            colorbar_frame,
            text="min",
            background=Config.COLOR_BG,
            foreground=Config.COLOR_FG,
        ).pack()
        self._draw_colorbar()

        grid = tk.Frame(outer, background=Config.COLOR_BG)
        grid.pack(fill=tk.X)
        for row, (key, label) in enumerate(
            [
                ("stream", "Stream"),
                ("version", "SpikeGLX"),
                ("sdk", "SDK path"),
                ("probes", "Probe count"),
                ("channels", "Channels"),
                ("sample_rate", "Sample rate"),
                ("source", "Data source"),
                ("sample_count", "Latest sample"),
                ("map_update", "Map update"),
                ("chunk_rate", "Sample flow"),
                ("fetched", "Fetch chunk"),
                ("rms", "RMS"),
                ("p2p", "AP peak-peak"),
                ("crossings", "Threshold crossings"),
                ("late", "Late/dropped chunks"),
            ]
        ):
            tk.Label(
                grid,
                text=label + ":",
                width=20,
                anchor="e",
                background=Config.COLOR_BG,
                foreground=Config.COLOR_FG,
            ).grid(row=row, column=0, sticky="e", padx=(0, 8), pady=2)
            var = tk.StringVar(value="-")
            self._fields[key] = var
            tk.Label(
                grid,
                textvariable=var,
                width=58,
                anchor="w",
                background=Config.COLOR_BG,
                foreground=Config.COLOR_FG,
            ).grid(row=row, column=1, sticky="w", pady=2)

        ttk.Separator(outer, orient=tk.HORIZONTAL).pack(fill=tk.X, pady=(12, 8))
        search = "\n".join(str(path) for path in sdk_search_paths())
        tk.Label(
            outer,
            text="SDK search paths:\n" + search,
            wraplength=640,
            justify=tk.LEFT,
            background=Config.COLOR_BG,
            foreground=Config.COLOR_FG,
        ).pack(fill=tk.X)

    def _labeled_entry(self, parent: tk.Misc, label: str, variable: tk.StringVar, width: int) -> tk.Frame:
        frame = tk.Frame(parent, background=Config.COLOR_BG)
        tk.Label(frame, text=label + ":", background=Config.COLOR_BG, foreground=Config.COLOR_FG).pack(side=tk.LEFT)
        tk.Entry(frame, textvariable=variable, width=width).pack(side=tk.LEFT, padx=(4, 0))
        return frame

    def _set_connecting(self):
        self._status_var.set("Connecting")
        self._detail_var.set("Looking for the SpikeGLX SDK and command server...")
        self._status_label.configure(background="#fff2b2")
        self._retry_btn.configure(state=tk.DISABLED)
        for var in self._fields.values():
            var.set("-")

    def _worker_loop(self, generation: int, config: SpikeGLXConfig, stop_event: threading.Event):
        client = None
        try:
            client = SpikeGLXLiveClient(config)
            info = client.connect()
            self._events.put(("connected", generation, client, info))
            while not stop_event.wait(self._current_update_interval()):
                try:
                    snapshot = client.fetch_snapshot()
                    self._events.put(("snapshot", generation, snapshot))
                except SpikeGLXUnavailable as exc:
                    self._events.put(("disconnected", generation, str(exc)))
                    break
                except Exception as exc:
                    self._events.put(("disconnected", generation, f"Live fetch failed: {exc}"))
                    break
        except Exception as exc:
            self._events.put(("failed", generation, str(exc)))
        finally:
            if client is not None:
                client.close()
            self._events.put(("worker_done", generation))

    def _poll_worker_events(self):
        while True:
            try:
                event = self._events.get_nowait()
            except queue.Empty:
                break
            self._handle_worker_event(event)
        if self.winfo_exists():
            self.after(100, self._poll_worker_events)

    def _handle_worker_event(self, event: tuple):
        kind = event[0]
        generation = event[1]
        if generation != self._connect_generation:
            return

        if kind == "connected":
            _kind, _generation, client, info = event
            self._client = client
            self._show_connected(info)
        elif kind == "snapshot":
            self._show_snapshot(event[2])
        elif kind in ("failed", "disconnected"):
            self._show_disconnected(event[2])
        elif kind == "worker_done":
            self._client = None
            self._worker_running = False
            self._retry_btn.configure(state=tk.NORMAL)

    def _show_connected(self, info: SpikeGLXStreamInfo):
        self._status_var.set("Connected")
        self._detail_var.set("Receiving live read-only diagnostics. Motor control is unaffected.")
        self._status_label.configure(background="#c8f7c5")
        self._fields["stream"].set(info.stream_label)
        self._fields["version"].set(info.version)
        self._fields["sdk"].set(info.source_path)
        self._fields["probes"].set(str(info.probe_count))
        self._fields["channels"].set(f"{info.channel_count} acquired, {info.ap_channel_count} AP")
        self._fields["sample_rate"].set(f"{info.sample_rate_hz:,.1f} Hz")
        self._fields["sample_count"].set(f"{info.sample_count:,}")

    def _show_snapshot(self, snapshot: SpikeGLXSnapshot):
        self._fields["sample_count"].set(f"{snapshot.sample_count:,}")
        self._fields["source"].set(snapshot.data_source)
        self._fields["chunk_rate"].set(f"{snapshot.chunk_rate_hz:,.1f} samples/sec")
        self._fields["fetched"].set(
            f"{snapshot.fetched_samples:,} samples ending near {snapshot.latest_head_sample:,}"
        )
        self._fields["rms"].set(f"{snapshot.rms_i16:,.1f} i16")
        self._show_probe_map(snapshot.peak_to_peak_mv)
        self._fields["crossings"].set(f"{snapshot.threshold_crossings:,} values above +/-750 i16")
        self._fields["late"].set(f"{snapshot.late_chunks} late, {snapshot.dropped_chunks} dropped")

    def _show_probe_map(self, values_mv: list[float]):
        if not values_mv or self._map_canvas is None:
            self._fields["p2p"].set("-")
            return

        self._map_update_count += 1
        self._fields["map_update"].set(f"{self._map_update_count} at {time.strftime('%H:%M:%S')}")

        if len(self._map_items) != len(values_mv):
            self._map_canvas.delete("all")
            self._map_items = []
            row_height = self._map_height / len(values_mv)
            for channel in range(len(values_mv)):
                display_row = len(values_mv) - 1 - channel
                y0 = display_row * row_height
                y1 = (display_row + 1) * row_height
                item = self._map_canvas.create_rectangle(
                    0,
                    y0,
                    self._map_width,
                    y1,
                    outline="",
                    fill="#ffffff",
                )
                self._map_items.append(item)

        map_min, map_max = self._map_limits(values_mv)
        for channel, value in enumerate(values_mv):
            normalized = max(0.0, min(1.0, (value - map_min) / (map_max - map_min)))
            color = self._colormap(normalized)
            self._map_canvas.itemconfigure(self._map_items[channel], fill=color)

        self._fields["p2p"].set(
            f"{min(values_mv):.3f} to {max(values_mv):.3f} mV across {len(values_mv)} channels "
            f"(map {map_min:.3f}-{map_max:.3f})"
        )

    def _map_limits(self, values_mv: list[float]) -> tuple[float, float]:
        try:
            map_min = float(self._map_min_var.get())
            map_max = float(self._map_max_var.get())
        except ValueError:
            map_min = min(values_mv)
            map_max = max(values_mv)

        if map_max <= map_min:
            map_max = map_min + 1.0
        return map_min, map_max

    def _draw_colorbar(self):
        if self._colorbar_canvas is None:
            return
        self._colorbar_canvas.delete("all")
        width = int(self._colorbar_canvas.cget("width"))
        height = int(self._colorbar_canvas.cget("height"))
        for row in range(height):
            normalized = 1.0 - (row / max(1, height - 1))
            self._colorbar_canvas.create_line(
                0,
                row,
                width,
                row,
                fill=self._colormap(normalized),
            )

    def _colormap(self, value: float) -> str:
        value = max(0.0, min(1.0, value))
        stops = [
            (0.00, (0, 0, 0)),
            (0.18, (0, 0, 120)),
            (0.35, (90, 0, 220)),
            (0.52, (210, 0, 150)),
            (0.68, (255, 60, 0)),
            (0.84, (255, 190, 0)),
            (1.00, (255, 255, 255)),
        ]
        for idx in range(1, len(stops)):
            left_value, left_color = stops[idx - 1]
            right_value, right_color = stops[idx]
            if value <= right_value:
                fraction = (value - left_value) / max(1e-9, right_value - left_value)
                red = int(left_color[0] + (right_color[0] - left_color[0]) * fraction)
                green = int(left_color[1] + (right_color[1] - left_color[1]) * fraction)
                blue = int(left_color[2] + (right_color[2] - left_color[2]) * fraction)
                return f"#{red:02x}{green:02x}{blue:02x}"
        return "#ffffff"

    def _update_update_interval(self):
        try:
            update_ms = float(self._update_ms_var.get())
        except ValueError:
            return
        update_ms = max(100.0, min(10000.0, update_ms))
        with self._update_lock:
            self._update_interval_sec = update_ms / 1000.0

    def _current_update_interval(self) -> float:
        with self._update_lock:
            return self._update_interval_sec

    def _show_disconnected(self, message: str):
        self._status_var.set("Disconnected")
        self._detail_var.set(message)
        self._status_label.configure(background="#ffd0d0")
        self._retry_btn.configure(state=tk.NORMAL)
