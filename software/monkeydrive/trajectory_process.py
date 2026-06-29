"""Subprocess entry point for a motor trajectory viewer."""

from __future__ import annotations

import argparse
import json
import queue
import sys
import threading
import traceback

import tkinter as tk

from .models import TrajectoryState
from .trajectory_viewer import TrajectoryViewer


def _emit(message: dict):
    print(json.dumps(message), flush=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--motor-id", type=int, required=True)
    parser.add_argument("--motor-position", type=float, required=True)
    parser.add_argument("--anatomy-path", required=True)
    parser.add_argument("--trajectory-json", required=True)
    args = parser.parse_args()

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

    def on_save(motor_id: int, trajectory: TrajectoryState):
        _emit({
            "type": "save",
            "motor_id": motor_id,
            "trajectory": trajectory.to_dict(),
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
            on_save,
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
                    viewer.update_motor_position(float(command.get("position", 0.0)))
                elif command_type == "target_offset":
                    viewer.update_target_offset(float(command.get("target_offset", 0.0)))
                elif command_type == "close":
                    close()
                    return

            root.after(50, poll_commands)

        root.after(50, poll_commands)
        root.mainloop()
        return 0
    except Exception as exc:
        _emit({"type": "error", "motor_id": args.motor_id, "message": str(exc)})
        traceback.print_exc(file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
