"""Subprocess entry point for the anatomy viewer."""

from __future__ import annotations

import argparse
import json
import queue
import sys
import threading
import traceback

import tkinter as tk

from .anatomy import AnatomyDependencyError, _import_numpy
from .anatomy_viewer import AnatomyViewer


def _emit(message: dict):
    print(json.dumps(message), flush=True)


def _preload_mri_dependencies():
    """Import MRI libraries before starting subprocess helper threads."""
    _import_numpy()
    try:
        import nibabel  # noqa: F401
        import pydicom  # noqa: F401
    except ImportError as exc:
        raise AnatomyDependencyError(
            "MRI support requires numpy, nibabel, and pydicom. "
            "Install the packages in software/requirements.txt."
        ) from exc


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--initial-path", default="")
    args = parser.parse_args()

    try:
        _preload_mri_dependencies()
    except AnatomyDependencyError as exc:
        _emit({"type": "error", "message": str(exc)})
        return 1

    commands: queue.Queue = queue.Queue()

    def reader():
        for line in sys.stdin:
            try:
                commands.put(json.loads(line))
            except json.JSONDecodeError:
                continue

    threading.Thread(target=reader, daemon=True).start()

    root = tk.Tk()
    root.withdraw()
    viewer = None
    closed = False

    def on_path_change(path: str):
        _emit({"type": "path_change", "path": path})

    def close():
        nonlocal closed
        if closed:
            return
        closed = True
        _emit({"type": "closed"})
        try:
            root.destroy()
        except tk.TclError:
            pass

    try:
        viewer = AnatomyViewer(root, initial_path=args.initial_path, on_path_change=on_path_change)
        viewer.protocol("WM_DELETE_WINDOW", close)
        root.bind_all("<End>", lambda _event: (_emit({"type": "pause_requested"}), "break")[1])

        def poll_commands():
            while True:
                try:
                    command = commands.get_nowait()
                except queue.Empty:
                    break

                command_type = command.get("type")
                if command_type == "focus" and viewer.winfo_exists():
                    viewer.lift()
                    viewer.focus_force()
                elif command_type == "close":
                    close()
                    return

            root.after(50, poll_commands)

        root.after(50, poll_commands)
        root.mainloop()
        return 0
    except Exception as exc:
        _emit({"type": "error", "message": str(exc)})
        traceback.print_exc(file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
