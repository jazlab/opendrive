"""Non-blocking helpers for GUI viewer subprocesses."""

from __future__ import annotations

import json
import logging
import queue
import subprocess
import sys
import threading
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class JsonViewerProcess:
    """Manage a viewer subprocess that communicates with JSON lines."""

    def __init__(self, module: str, args: list[str], cwd: Path):
        command = [sys.executable, "-m", module, *args]
        self._process = subprocess.Popen(
            command,
            cwd=str(cwd),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=None,
            text=True,
            bufsize=1,
        )
        self._events: queue.Queue = queue.Queue()
        self._commands: queue.Queue = queue.Queue()
        self._latest_position: Optional[float] = None
        self._position_lock = threading.Lock()
        self._stopped = False

        self._reader = threading.Thread(target=self._reader_loop, daemon=True)
        self._reader.start()
        self._writer = threading.Thread(target=self._writer_loop, daemon=True)
        self._writer.start()

    @property
    def pid(self) -> Optional[int]:
        """Return process id."""
        return self._process.pid if self._process else None

    def is_running(self) -> bool:
        """Return whether the subprocess is still running."""
        return self._process.poll() is None

    def poll_events(self) -> list[dict]:
        """Return all pending events from the subprocess."""
        events = []
        while True:
            try:
                events.append(self._events.get_nowait())
            except queue.Empty:
                break
        return events

    def send(self, message: dict):
        """Queue a command for the subprocess without blocking the GUI."""
        if self.is_running():
            self._commands.put(message)

    def send_focus(self):
        """Ask the subprocess window to raise itself."""
        self.send({"type": "focus"})

    def send_motor_position(self, position: float):
        """Send only the latest motor position to the subprocess."""
        if not self.is_running():
            return
        with self._position_lock:
            self._latest_position = float(position)

    def send_target_offset(self, target_offset: float):
        """Send an updated trajectory target offset to the subprocess."""
        self.send({"type": "target_offset", "target_offset": float(target_offset)})

    def close(self):
        """Ask the subprocess to close."""
        self.send({"type": "close"})

    def terminate(self):
        """Terminate the subprocess if it is still running."""
        self._stopped = True
        if self.is_running():
            try:
                self.close()
                self._process.terminate()
            except OSError:
                pass

    def _reader_loop(self):
        try:
            if not self._process.stdout:
                return
            for line in self._process.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    self._events.put(json.loads(line))
                except json.JSONDecodeError:
                    logger.warning("Viewer emitted non-JSON output: %s", line)
        finally:
            self._events.put({"type": "exited", "returncode": self._process.poll()})

    def _writer_loop(self):
        while not self._stopped:
            if not self.is_running():
                return

            message = None
            try:
                message = self._commands.get(timeout=0.05)
            except queue.Empty:
                with self._position_lock:
                    if self._latest_position is not None:
                        message = {
                            "type": "motor_position",
                            "position": self._latest_position,
                        }
                        self._latest_position = None

            if message is None:
                continue

            try:
                if not self._process.stdin:
                    return
                self._process.stdin.write(json.dumps(message) + "\n")
                self._process.stdin.flush()
            except (BrokenPipeError, OSError, ValueError):
                return
