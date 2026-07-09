"""Subprocess entry point for the live probe summary viewer."""

from __future__ import annotations

import argparse
import json
import queue
import sys
import threading
import traceback

import tkinter as tk

from .probe_summary_viewer import ProbeSummaryViewer


def _emit(message: dict):
    print(json.dumps(message), flush=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.parse_args()

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
        viewer = ProbeSummaryViewer(root)
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
