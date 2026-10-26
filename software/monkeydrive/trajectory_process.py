"""Subprocess entry point for a motor trajectory viewer."""

from __future__ import annotations

import argparse
import json
import queue
import sys
import threading
import traceback

import tkinter as tk

from .viewer_tasks import ViewerCommands

from .anatomy import AnatomyDependencyError, _import_numpy
from .models import TrajectoryState
from .trajectory_viewer import TrajectoryViewer


def _emit(message: dict):
    print(json.dumps(message), flush=True)


def _preload_mri_dependencies():
    """Import MRI libraries before starting subprocess helper threads."""
    _import_numpy()
    try:
        import scipy.ndimage  # noqa: F401
        import nibabel  # noqa: F401
        import pydicom  # noqa: F401
    except ImportError as exc:
        raise AnatomyDependencyError(
            "MRI support requires numpy, scipy, nibabel, and pydicom. "
            "Install the packages in software/requirements.txt."
        ) from exc


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--motor-id", type=int, required=True)
    parser.add_argument("--motor-position", type=float, required=True)
    parser.add_argument("--revision", type=int, default=0)
    parser.add_argument("--anatomy-path", required=True)
    parser.add_argument("--trajectory-json", required=True)
    parser.add_argument("--orientation-actual", default="")
    parser.add_argument("--orientation-scanner", default="")
    args = parser.parse_args()

    try:
        _preload_mri_dependencies()
    except AnatomyDependencyError as exc:
        _emit({"type": "error", "message": str(exc), "motor_id": args.motor_id})
        return 1

    commands = ViewerCommands()

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
    revision = args.revision

    def on_save(motor_id: int, trajectory: TrajectoryState):
        _emit({
            "type": "save",
            "motor_id": motor_id,
            "trajectory": trajectory.to_dict(),
            "revision": revision,
        })

    def close():
        nonlocal closed
        if closed:
            return
        closed = True
        _emit({"type": "closed", "motor_id": args.motor_id})
        try:
            root.destroy()
        except tk.TclError:
            pass

    try:
        trajectory = TrajectoryState.from_dict(json.loads(args.trajectory_json))
        viewer = TrajectoryViewer(
            root,
            args.motor_id,
            trajectory,
            args.motor_position,
            args.anatomy_path,
            args.orientation_actual,
            args.orientation_scanner,
            on_save,
        )
        viewer.protocol("WM_DELETE_WINDOW", close)
        root.bind_all("<End>", lambda _event: (_emit({"type": "pause_requested"}), "break")[1])

        def poll_commands():
            nonlocal revision
            for _ in range(16):
                try:
                    command = commands.get_nowait()
                except queue.Empty:
                    break

                command_type = command.get("type")
                if command_type == "focus" and viewer.winfo_exists():
                    viewer.lift()
                    viewer.focus_force()
                elif command_type == "motor_position":
                    viewer.update_motor_position(float(command.get("position", 0.0)))
                elif command_type == "target_offset":
                    viewer.update_target_offset(float(command.get("target_offset", 0.0)))
                elif command_type == "trajectory_state":
                    revision = int(command['revision'])
                    viewer.update_trajectory(TrajectoryState.from_dict(command['trajectory']))
                elif command_type == "anatomy_orientation":
                    viewer.update_anatomy_orientation(
                        str(command.get("actual", "")),
                        str(command.get("scanner", "")),
                    )
                elif command_type == "close":
                    close()
                    return

            root.after(20, poll_commands)

        root.after(20, poll_commands)
        root.mainloop()
        return 0
    except Exception as exc:
        _emit({"type": "error", "motor_id": args.motor_id, "message": str(exc)})
        traceback.print_exc(file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
