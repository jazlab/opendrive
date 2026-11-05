"""Simple live SpikeGLX probe-map viewer."""

from __future__ import annotations

import queue
import threading
import time

# Load on the GUI thread before starting SDK workers. A cold NumPy import in
# a worker can contend with Tk's event loop during standalone viewer startup.
import numpy  # noqa: F401

import tkinter as tk
from tkinter import ttk

from .config import Config
from .console_theme import ConsoleTheme as Theme
from .viewer_theme import configure_viewer, style_viewer, viewer_button
from .probe_map_colors import color_from_lut, load_shank_lut
from .probe_map_layout import probe_map_layout
from .probe_view_settings import probe_view_settings
from .spikeglx_client import SpikeGLXConfig, SpikeGLXLiveClient, SpikeGLXUnavailable


class ProbeSummaryViewer(tk.Toplevel):
    """Show one live probe map using the same data path as the trajectory viewer."""

    def __init__(self, parent: tk.Tk, initial_settings=None, on_settings_change=None):
        super().__init__(parent)
        configure_viewer(self)

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
        self._shanks_by_probe: dict[int, list[int]] = {}
        self._update_counts: dict[int, int] = {}
        self._last_status_time = 0.0
        self._lut = load_shank_lut()

        self._current_settings = settings = probe_view_settings(initial_settings)
        self._on_settings_change = on_settings_change
        self._min_var = tk.StringVar(value=f"{settings['min_mv']:g}")
        self._max_var = tk.StringVar(value=f"{settings['max_mv']:g}")
        self._update_ms_var = tk.StringVar(value=str(settings['update_ms']))
        self._layout_settings = tuple(settings[key] for key in ('horizontal_pixels', 'vertical_pixels', 'spacing'))
        self._horizontal_pixels_var = tk.StringVar(value=str(settings['horizontal_pixels']))
        self._vertical_pixels_var = tk.StringVar(value=str(settings['vertical_pixels']))
        self._spacing_var = tk.StringVar(value=str(settings['spacing']))
        self._view_settings_window = None
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
        self._remember_settings()
        self._stop_worker()
        try:
            super().destroy()
        except tk.TclError:
            pass

    def _build_ui(self):
        container = tk.Frame(self)
        container.pack(fill=tk.BOTH, expand=True)
        self._sidebar = tk.Frame(container, padx=14, pady=14)
        self._sidebar.pack(side=tk.LEFT, fill=tk.Y)
        self._add_field(self._sidebar, "Min mV", self._min_var, width=12)
        self._add_field(self._sidebar, "Max mV", self._max_var, width=12)
        self._add_field(self._sidebar, "Update ms", self._update_ms_var, width=12)
        for label, command in (('Apply', self._apply_settings), ('Retry', self._restart),
                               ('View Settings', self._on_view_settings)):
            viewer_button(self._sidebar, text=label, command=command).pack(fill=tk.X, pady=(0, 8))
        # Connection errors remain visible beside the controls without a footer
        # or constantly changing status text taking space from the probe.
        self._error_label = tk.Label(self._sidebar, textvariable=self._status_var,
                                     wraplength=150, justify='left', anchor='w')

        map_frame = tk.Frame(container)
        map_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        map_frame.grid_rowconfigure(0, weight=1)
        map_frame.grid_columnconfigure(0, weight=1)
        self._canvas = tk.Canvas(map_frame, width=760, height=680, background="#dddddd",
                                 highlightthickness=0, yscrollincrement=12)
        self._canvas.grid(row=0, column=0, sticky='nsew')
        scrollbar = ttk.Scrollbar(map_frame, orient=tk.VERTICAL, command=self._canvas.yview)
        scrollbar.grid(row=0, column=1, sticky='ns')
        self._canvas.configure(yscrollcommand=scrollbar.set)
        horizontal_scrollbar = self._horizontal_scrollbar = ttk.Scrollbar(
            map_frame, orient=tk.HORIZONTAL, command=self._canvas.xview)
        horizontal_scrollbar.grid(row=1, column=0, sticky='ew')
        self._canvas.configure(xscrollcommand=horizontal_scrollbar.set)
        self._canvas.create_text(140, 340, text="Waiting for data", fill="#333333", tags=("probe_map",))
        self._canvas.bind("<Configure>", lambda _event: self._draw_probe())
        for event in ('<MouseWheel>', '<Button-4>', '<Button-5>'):
            self._canvas.bind(event, self._scroll_map)

    def _add_field(self, parent: tk.Frame, label: str, variable: tk.StringVar, width: int):
        tk.Label(parent, text=label, anchor='w').pack(fill=tk.X, pady=(0, 4))
        entry = tk.Entry(parent, textvariable=variable, width=width)
        entry.pack(fill=tk.X, pady=(0, 14))
        entry.bind("<Return>", lambda _event: (self._apply_settings(), "break")[1])
        entry.bind("<KP_Enter>", lambda _event: (self._apply_settings(), "break")[1])

    def _apply_layout(self, _event=None):
        variables = (self._horizontal_pixels_var, self._vertical_pixels_var, self._spacing_var)
        try:
            settings = tuple(int(variable.get()) for variable in variables)
            if not all(1 <= value <= 256 for value in settings[:2]) or not 0 <= settings[2] <= 256:
                raise ValueError
        except ValueError:
            for variable, value in zip(variables, self._layout_settings):
                variable.set(str(value))
            return 'break'
        self._layout_settings = settings
        self._remember_settings()
        self._draw_probe()
        return 'break'

    def get_settings(self):
        return probe_view_settings(dict(min_mv=self._min_var.get(), max_mv=self._max_var.get(),
            update_ms=self._update_ms_var.get(), horizontal_pixels=self._horizontal_pixels_var.get(),
            vertical_pixels=self._vertical_pixels_var.get(), spacing=self._spacing_var.get()),
            fallback=self._current_settings)

    def _remember_settings(self):
        self._current_settings = self.get_settings()
        if self._on_settings_change:
            self._on_settings_change(dict(self._current_settings))

    def _on_view_settings(self):
        window = self._view_settings_window
        if window is not None and window.winfo_exists():
            window.lift()
            window.focus_set()
            return
        window = self._view_settings_window = tk.Toplevel(self)
        window.title('Probe View Settings')
        window.transient(self)
        window.resizable(False, False)
        body = tk.Frame(window, padx=20, pady=18)
        body.pack(fill=tk.BOTH, expand=True)
        tk.Label(body, text='Channel appearance', font=self._viewer_section_font).grid(
            row=0, column=0, columnspan=2, sticky='w', pady=(0, 12))
        for row, (label, variable, minimum) in enumerate((
                ('Horizontal pixels', self._horizontal_pixels_var, 1),
                ('Vertical pixels', self._vertical_pixels_var, 1),
                ('Spacing (pixels)', self._spacing_var, 0)), start=1):
            tk.Label(body, text=label).grid(row=row, column=0, sticky='w', padx=(0, 18), pady=5)
            entry = ttk.Spinbox(body, textvariable=variable, from_=minimum, to=256,
                                width=6, command=self._apply_layout)
            entry.grid(row=row, column=1, pady=5)
            for event in ('<Return>', '<KP_Enter>', '<FocusOut>'):
                entry.bind(event, self._apply_layout)
        tk.Label(body, text='Use arrows, Enter, or Tab to update the map.', anchor='w').grid(
            row=4, column=0, columnspan=2, pady=(12, 8), sticky='w')
        def close():
            self._apply_layout()
            window.destroy()
            self._view_settings_window = None
        viewer_button(body, text='Done', command=close).grid(row=5, column=0, columnspan=2, sticky='e')
        window.protocol('WM_DELETE_WINDOW', close)
        style_viewer(self, window)

    def _apply_settings(self):
        update_ms = self._update_ms()
        self._remember_settings()
        self._draw_probe()
        if self._worker_running and self._active_update_ms == update_ms:
            return
        self._restart()

    def _restart(self):
        self._remember_settings()
        self._error_label.pack_forget()
        self._stop_worker()
        self._start_worker()

    def _start_worker(self):
        self._values_by_probe = {}
        self._x_by_probe = {}
        self._depths_by_probe = {}
        self._shanks_by_probe = {}
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
            prefer_filtered=True,
            include_geometry=True,
            geometry_refresh_s=0.0,
            compute_diagnostics=False,
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
            for snapshot in client.iter_heatmaps(stop_event, round(update_interval * 1000)):
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
            if event[1] != self._generation:
                continue
            if event[0] == "snapshot":
                latest_snapshots[int(event[2])] = event
                continue
            if event[0] == 'error':
                latest_snapshots.pop(int(event[2]), None)
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
            self.after(16, self._poll_events)

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
            self._shanks_by_probe[probe] = list(getattr(snapshot, 'channel_shanks', []))
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
            self._error_label.pack(fill=tk.X, pady=(8, 0))
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
            content_height = height
            # Reserve each probe's natural width; scrolling handles overflow
            # instead of clipping, shrinking pads, or overlapping other probes.
            for probe in range(probe_count):
                values = self._values_by_probe.get(probe, [])
                bodies, _ = probe_map_layout(len(values), self._x_by_probe.get(probe, []),
                    self._depths_by_probe.get(probe, []), self._shanks_by_probe.get(probe, []),
                    0, 1, height, *self._layout_settings)
                column_width = max(column_width, bodies[-1][2] - bodies[0][1] + 24)
            for probe in range(probe_count):
                column_left = probe * column_width
                column_right = column_left + column_width
                values = self._values_by_probe.get(probe, [])
                bodies, pads = probe_map_layout(len(values), self._x_by_probe.get(probe, []),
                    self._depths_by_probe.get(probe, []), self._shanks_by_probe.get(probe, []),
                    column_left, column_right, height, *self._layout_settings)
                probe_bottom = max(body[4] for body in bodies)
                for shank, left, right, top, bottom in bodies:
                    center = (left + right) / 2
                    self._canvas.create_polygon(left, top, right, top, right, bottom,
                        center, bottom + 14, left, bottom, fill='#b5b5b5', outline='#777777',
                        tags=('probe_map', 'shank'))
                    content_height = max(content_height, bottom + 18)
                if values:
                    self._canvas.create_text((column_left + column_right) / 2, probe_bottom + 27,
                        text=f'imec{probe}', fill='#333333', tags=('probe_map', 'probe_label'))
                    content_height = max(content_height, probe_bottom + 40)
                if values:
                    map_min, map_max = self._color_limits()
                    for channel, rectangle in pads:
                        color = color_from_lut(self._lut, values[channel], map_min, map_max)
                        self._canvas.create_rectangle(*rectangle, fill=color, outline='',
                            tags=('probe_map', 'contact', f'probe_{probe}_channel_{channel}'))
                else:
                    self._canvas.create_text(
                        (column_left + column_right) / 2,
                        height / 2,
                        text=f"P{probe}\nWaiting",
                        fill="#333333",
                        justify=tk.CENTER,
                        tags=("probe_map",),
                    )

            content_width = max(width, column_width * probe_count)
            self._canvas.configure(scrollregion=(0, 0, content_width, content_height))
            if content_width > width:
                self._horizontal_scrollbar.grid()
            else:
                self._horizontal_scrollbar.grid_remove()
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

    def _scroll_map(self, event):
        delta = getattr(event, 'delta', 0)
        number = getattr(event, 'num', None)
        if delta or number in (4, 5):
            direction = -1 if delta > 0 or number == 4 else 1
            steps = max(1, abs(int(delta / 120))) if delta else 1
            self._canvas.yview_scroll(direction * steps * 3, 'units')
        return 'break'

    def _color_limits(self) -> tuple[float, float]:
        settings = probe_view_settings({'min_mv': self._min_var.get(), 'max_mv': self._max_var.get()},
                                       fallback=self._current_settings)
        return settings['min_mv'], settings['max_mv']

    def _probe(self) -> int:
        try:
            return max(0, int(float(self._probe_var.get())))
        except ValueError:
            return 0

    def _update_ms(self) -> int:
        try:
            value = max(100, int(float(self._update_ms_var.get())) // 100 * 100)
            self._update_ms_var.set(str(value))
            return value
        except (ValueError, OverflowError):
            return self._current_settings['update_ms']

    def _style_widget_tree(self, widget=None):
        style_viewer(self, widget)
