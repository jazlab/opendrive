"""Subprocess entry point for the unified Trajectories window."""

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
from .models import MotorState, TrajectoryState
from .trajectories_viewer import TrajectoriesViewer


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
    parser.add_argument("--anatomy-path", required=True)
    parser.add_argument("--motor-states-json", required=True)
    parser.add_argument("--orientation-actual", default="")
    parser.add_argument("--orientation-scanner", default="")
    parser.add_argument("--explorer-json", default="{}")
    parser.add_argument("--revisions-json", default="{}")
    args = parser.parse_args()

    try:
        _preload_mri_dependencies()
    except AnatomyDependencyError as exc:
        _emit({"type": "error", "message": str(exc)})
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
    def callback_error(kind, value, tb):
        _emit({'type': 'error', 'message': str(value)})
        traceback.print_exception(kind, value, tb)
    root.report_callback_exception = callback_error
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
        viewer = TrajectoriesViewer(
            root,
            motor_states,
            args.anatomy_path,
            args.orientation_actual,
            args.orientation_scanner,
            explorer_state=json.loads(args.explorer_json),
            revisions=json.loads(args.revisions_json),
            on_save=lambda motor_id, trajectory, revision: _emit({'type': 'save', 'motor_id': motor_id, 'trajectory': trajectory.to_dict(), 'revision': revision}),
            on_explorer_change=lambda state: _emit({'type': 'explorer_state', 'state': state}),
            on_assign=lambda motor_id, state: _emit({'type': 'assign_trajectory', 'motor_id': motor_id, 'state': state}),
        )
        viewer.protocol("WM_DELETE_WINDOW", close)
        root.bind_all("<End>", lambda _event: (_emit({"type": "pause_requested"}), "break")[1])

        def poll_commands():
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
                    viewer.update_motor_position(
                        int(command.get("motor_id", 0)),
                        float(command.get("position", 0.0)),
                    )
                elif command_type == "motor_state":
                    viewer.update_motor_state(MotorState.from_dict(command.get("motor_state", {})), command.get("revision"))
                elif command_type == "anatomy_orientation":
                    viewer.update_anatomy(
                        command.get("path", args.anatomy_path),
                        str(command.get("actual", "")),
                        str(command.get("scanner", "")),
                    )
                elif command_type == 'motor_states':
                    viewer.set_motor_states([MotorState.from_dict(state) for state in command['motor_states']], command.get('revisions'))
                elif command_type == 'trajectory_state':
                    viewer.apply_trajectory(command['motor_id'], TrajectoryState.from_dict(command['trajectory']),
                                            command['revision'], command.get('select', False))
                elif command_type == 'select_drive':
                    viewer.select_tab(command['motor_id'])
                elif command_type == 'assignment_result':
                    viewer._explorer._assignment_status.set(command.get('message', ''))
                elif command_type == "close":
                    close()
                    return

            root.after(20, poll_commands)

        root.after(20, poll_commands)
        root.mainloop()
        return 0
    except Exception as exc:
        _emit({"type": "error", "message": str(exc)})
        traceback.print_exc(file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
