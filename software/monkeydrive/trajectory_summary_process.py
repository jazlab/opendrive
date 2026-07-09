"""Subprocess entry point for the trajectory summary viewer."""

from __future__ import annotations

import argparse
import json
import queue
import sys
import threading
import traceback

import tkinter as tk

from .anatomy import AnatomyDependencyError, _import_numpy
from .models import MotorState
from .trajectory_summary_viewer import TrajectorySummaryViewer


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
    parser.add_argument("--anatomy-path", required=True)
    parser.add_argument("--motor-states-json", required=True)
    parser.add_argument("--orientation-actual", default="")
    parser.add_argument("--orientation-scanner", default="")
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

    def close():
        nonlocal closed
        if closed:
            return
        closed = True
        _emit({"type": "closed"})
        if viewer is not None:
            try:
                viewer.destroy()
            except tk.TclError:
                pass
        try:
            root.destroy()
        except tk.TclError:
            pass

    try:
        motor_states = [
            MotorState.from_dict(state)
            for state in json.loads(args.motor_states_json)
        ]
        viewer = TrajectorySummaryViewer(
            root,
            motor_states,
            args.anatomy_path,
            args.orientation_actual,
            args.orientation_scanner,
        )
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
                elif command_type == "motor_position":
                    viewer.update_motor_position(
                        int(command.get("motor_id", 0)),
                        float(command.get("position", 0.0)),
                    )
                elif command_type == "motor_state":
                    viewer.update_motor_state(MotorState.from_dict(command.get("motor_state", {})))
                elif command_type == "anatomy_orientation":
                    viewer.update_anatomy_orientation(
                        str(command.get("actual", "")),
                        str(command.get("scanner", "")),
                    )
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
