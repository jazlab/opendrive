#!/usr/bin/env python3
"""Stress-test OpenDrive GUI responsiveness without commanding hardware.

This script opens the normal GUI, optionally opens trajectory viewers and the
trajectory summary viewer, then simulates high-rate serial position updates.
It measures whether the main Tk event loop stalls while MRI viewers render in
subprocesses.
"""

from __future__ import annotations

import argparse
import math
import queue
import statistics
import threading
import time
from unittest.mock import patch

from opendrive_gui import OpenDriveApp


def percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round((pct / 100.0) * (len(ordered) - 1))))
    return ordered[index]


class GuiStressTest:
    """Run a timed GUI responsiveness stress test."""

    def __init__(self, args: argparse.Namespace):
        self.args = args
        # Suppress the constructor's initial save as well as subsequent saves.
        with patch.object(OpenDriveApp, '_save_state', lambda _self: None):
            self.app = OpenDriveApp()
        self.root = self.app.root
        self.stop_event = threading.Event()
        self.tick_gaps_ms: list[float] = []
        self.urgent_latencies_ms: list[float] = []
        self.position_updates = 0
        self._positions = {}
        self._positions_lock = threading.Lock()
        self._urgent_events = queue.Queue(maxsize=1)
        self._last_tick = time.perf_counter()

        # Do not let simulated positions overwrite the user's real saved state.
        self.app._save_state = lambda: None

    def run(self) -> int:
        self.root.title("OpenDrive Stress Test")
        self.root.after(250, self._open_viewers)
        self.root.after(self.args.tick_ms, self._tick)
        self.root.after(int(self.args.duration * 1000), self._finish)
        self.root.after(5, self._drain_simulated_events)

        motor_ids = [state.motor_id for state in self.app.motor_states]
        threading.Thread(target=self._position_update_loop, args=(motor_ids,), daemon=True).start()
        threading.Thread(target=self._urgent_callback_loop, daemon=True).start()

        print(
            f"Stress test running for {self.args.duration:.1f}s: "
            f"{self.args.update_hz:.1f} position updates/s/motor, "
            f"trajectory_viewers={self.args.trajectory_viewers}, "
            f"summary={not self.args.no_summary}",
            flush=True,
        )
        self.app.run()
        return self._report()

    def _open_viewers(self):
        if not self.args.no_summary:
            self.app._on_trajectory_summary()

        if self.args.trajectory_viewers <= 0:
            return

        opened = 0
        for state in self.app.motor_states:
            if opened >= self.args.trajectory_viewers:
                break
            self.app._on_trajectory_click(state.motor_id)
            opened += 1

    def _tick(self):
        now = time.perf_counter()
        self.tick_gaps_ms.append((now - self._last_tick) * 1000.0)
        self._last_tick = now
        if not self.stop_event.is_set():
            self.root.after(self.args.tick_ms, self._tick)

    def _position_update_loop(self, motor_ids):
        period = 1.0 / max(self.args.update_hz, 0.1)
        start = time.perf_counter()

        while not self.stop_event.is_set():
            elapsed = time.perf_counter() - start
            for motor_id in motor_ids:
                phase = elapsed * self.args.motion_hz + motor_id
                position = 25.0 + 20.0 * math.sin(phase)
                with self._positions_lock:
                    self._positions[motor_id] = position
                self.position_updates += 1
            time.sleep(period)

    def _urgent_callback_loop(self):
        period = self.args.urgent_interval_ms / 1000.0
        while not self.stop_event.is_set():
            scheduled = time.perf_counter()
            try:
                self._urgent_events.put_nowait(scheduled)
            except queue.Full:
                pass
            time.sleep(period)

    def _drain_simulated_events(self):
        with self._positions_lock:
            positions = list(self._positions.items())
            self._positions.clear()
        for motor_id, position in positions:
            self.app._on_position_update(motor_id, position)
        try:
            self._record_urgent_latency(self._urgent_events.get_nowait())
        except queue.Empty:
            pass
        if not self.stop_event.is_set():
            self.root.after(5, self._drain_simulated_events)

    def _record_urgent_latency(self, scheduled: float):
        self.urgent_latencies_ms.append((time.perf_counter() - scheduled) * 1000.0)

    def _finish(self):
        self.stop_event.set()
        self.root.after(250, self.app._on_close)

    def _report(self) -> int:
        tick_gaps = self.tick_gaps_ms[1:] if len(self.tick_gaps_ms) > 1 else self.tick_gaps_ms
        urgent = self.urgent_latencies_ms
        worst_tick = max(tick_gaps, default=0.0)
        worst_urgent = max(urgent, default=0.0)
        p95_tick = percentile(tick_gaps, 95)
        p95_urgent = percentile(urgent, 95)
        mean_tick = statistics.mean(tick_gaps) if tick_gaps else 0.0
        mean_urgent = statistics.mean(urgent) if urgent else 0.0
        failures = []

        if worst_tick > self.args.max_tick_gap_ms:
            failures.append(
                f"worst Tk heartbeat gap {worst_tick:.1f} ms > "
                f"{self.args.max_tick_gap_ms:.1f} ms"
            )
        if worst_urgent > self.args.max_urgent_latency_ms:
            failures.append(
                f"worst urgent callback latency {worst_urgent:.1f} ms > "
                f"{self.args.max_urgent_latency_ms:.1f} ms"
            )

        print("\nStress test results")
        print(f"  position updates scheduled: {self.position_updates}")
        print(f"  Tk heartbeat gap: mean={mean_tick:.1f} ms, p95={p95_tick:.1f} ms, worst={worst_tick:.1f} ms")
        print(
            "  urgent callback latency: "
            f"mean={mean_urgent:.1f} ms, p95={p95_urgent:.1f} ms, worst={worst_urgent:.1f} ms"
        )

        if failures:
            print("  RESULT: FAIL")
            for failure in failures:
                print(f"    - {failure}")
            return 1

        print("  RESULT: PASS")
        return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--duration", type=float, default=60.0, help="Test duration in seconds.")
    parser.add_argument("--update-hz", type=float, default=25.0, help="Position updates per second per motor.")
    parser.add_argument("--motion-hz", type=float, default=2.0, help="Frequency of simulated motor motion.")
    parser.add_argument("--trajectory-viewers", type=int, default=6, help="Number of trajectory windows to open.")
    parser.add_argument("--no-summary", action="store_true", help="Do not open the trajectory summary window.")
    parser.add_argument("--tick-ms", type=int, default=20, help="Tk heartbeat interval in milliseconds.")
    parser.add_argument("--urgent-interval-ms", type=int, default=100, help="Urgent callback probe interval.")
    parser.add_argument("--max-tick-gap-ms", type=float, default=250.0, help="Failure threshold for main-loop gaps.")
    parser.add_argument(
        "--max-urgent-latency-ms",
        type=float,
        default=250.0,
        help="Failure threshold for urgent callback latency.",
    )
    return parser.parse_args()


def main() -> int:
    return GuiStressTest(parse_args()).run()


if __name__ == "__main__":
    raise SystemExit(main())
