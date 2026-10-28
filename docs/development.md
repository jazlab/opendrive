# Working on MonkeyDrive

Start the application with `software/monkeydrive_gui.py`. It creates one Tk root
and connects the application, console view, keyboard controller, motion
controller, and serial worker.

## Where to make a change

| Responsibility | File |
| --- | --- |
| Tk event routing, program file dialogs, workspace loading, and viewer coordination | `software/monkeydrive_gui.py` — `MonkeyDriveApp` |
| Full/compact layouts, widgets, Tk variables, styling, and numeric field navigation | `software/monkeydrive/console_view.py` — `ConsoleView` |
| Enter shortcuts, drive-selection keys, held-key jogging, focus loss, and gesture timers | `software/monkeydrive/console_shortcuts.py` — `ConsoleShortcuts` |
| Movement validation, Run/Step/Jog commands, pauses, transient destinations, and program progression | `software/monkeydrive/motion_controller.py` — `MotionController` |
| Serial connection, command queue, heartbeats, and background device I/O | `software/monkeydrive/serial_manager.py` — `SerialManager` |
| Serializable drive, trajectory, and program data | `software/monkeydrive/models.py` |
| Config format, validation, and atomic file replacement | `software/monkeydrive/configuration.py` |
| Controller-side command handling and step generation | `firmware/monkeydrive_firmware/monkeydrive_firmware.ino` |

`app.view` owns the console widgets. `app.shortcuts` owns keyboard gesture state.
`app.motion` owns movement rules and the transient destinations used by running
indicators. It accepts drive models and ordinary values, with no Tk dependency.
The application calls these objects explicitly; there are no mixins or implicit
attribute forwarding between them. The view and shortcuts call application
actions explicitly through their application reference.

The application still contains several responsibilities. Future extractions can
separate workspace operations, program file handling, and viewer coordination while
keeping their existing behavior. Avoid introducing new movement behavior in the
same change as a structural refactor.

## Following a movement through the code

For a target move:

1. `ConsoleView` connects the **Run Selected** button to `MonkeyDriveApp._on_run`.
2. The application reads the selected drives and fields from the view, wraps
   them in `TargetMove` values, and calls `MotionController.run_targets`.
3. The controller validates every selected drive before constructing any RUN
   command. A `MotionError` is raised for the application to show as a dialog; the
   controller does not create dialogs or read widgets.
4. `MotionController.queue_motion` submits each command to the supplied sender,
   `SerialManager.send`. Acceptance means queued, not acknowledged or physically
   executed. Transient destinations track that distinction for the Running badge.
5. The serial worker writes it. Later, the Tk poller delivers incoming positions
   to `_on_position_update`.
6. The application passes the position to the motion controller, then updates
   console readouts and open viewers. For an active program, the controller checks
   target tolerance and advances the instruction or marks the program complete.

Program notifications run after model changes. The application uses them to
refresh program rows and synchronize trajectory targets. The motion controller
does not own viewers or the serial port. Keyboard jogging uses the same motion
controller while `ConsoleShortcuts` retains ownership of key/focus cancellation.

Full and compact readouts share the same Tk position and selection variables.
Rebuilding rows replaces those widgets together. Switching views only changes
the layout; it does not create a second set of drive models.

For keyboard motion, start at `ConsoleShortcuts`. A held gesture remembers its
original drive IDs. Cancellation pauses those IDs, independently of the current
selection, and blocks key repeats until release. Keep these behaviors together
when changing keyboard handling.

## Threads and processes

- The application, console view, shortcuts, motion controller, and serial callbacks run on Tk's
  main thread. Widget access belongs on this thread.
- `SerialManager` owns the serial port on one worker thread. GUI actions queue
  commands and do not wait for device I/O.
- Anatomy and trajectory windows run in subprocesses managed by
  `JsonViewerProcess`. Their messages are polled by the main application.
- MRI computation and named-config file I/O use background workers; completion
  callbacks return to the owning Tk thread.

## Running checks

From the repository root, using the virtual environment created in the README,
change into `software` so its modules are on Python's import path:

```powershell
# Windows
cd software
..\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

```sh
# macOS
cd software
../.venv/bin/python -m unittest discover -s tests -v
```

Tests simulate controller I/O and use synthetic MRI data. The GUI integration
tests require a desktop/Tk installation and use the real view and shortcut
controller; they do not open hardware or write the user's saved state.
The firmware test compiles the actual sketch against simulated Arduino I/O when
a desktop C++ compiler is available; otherwise that test is skipped.

`test_motion_controller.py` tests movement rules with plain models and a command
recorder. It needs neither a desktop nor a serial controller. To run just those
tests from `software`, add `-p test_motion_controller.py` to discovery.

The tests inspect widgets through `app.view` and keyboard state through
`app.shortcuts`. Changing their owner should not require deleting behavioral
assertions. A layout-only change should leave movement commands, saved file
formats, and the firmware unchanged.
