# MonkeyDrive

MonkeyDrive is a motorized microdrive control system for lowering electrodes or probes during neuroscience experiments. The system uses a stepper motor and lead screw to translate each drive carriage, with a Python GUI for control and Teensy/Arduino firmware for low-level motor control.

This repository contains:

- `software/monkeydrive_gui.py`: the main MonkeyDrive control GUI
- `software/monkeydrive/`: GUI support modules, serial protocol, anatomy viewers, and trajectory viewers
- `firmware/monkeydrive_firmware/monkeydrive_firmware.ino`: Teensy/Arduino firmware
- `software/stress_gui_responsiveness.py`: GUI stress-test tool

## Safety Notes

MonkeyDrive controls physical hardware. Before using it near electrodes, probes, animals, or tissue:

- Confirm the correct firmware is uploaded to the controller.
- Verify the selected serial port is the MonkeyDrive controller.
- Test `Pause`, `End`, disconnect, and GUI close behavior in a safe setup.
- Confirm direction conventions and depth changes before attaching a probe.
- Keep the system within the configured `0-50 mm` software/firmware travel range.

The GUI and firmware include heartbeat/connection-loss handling. If communication is lost, the GUI and firmware are designed to stop motion, but this should still be tested with your hardware.

### Connection responsiveness

Serial opening, reading, writing, and closing run in one background worker. The
GUI queues commands; a successful enqueue does not mean the firmware has
acknowledged execution. Writes have a 200 ms timeout, and no blocking serial
flush is used. Pause commands take priority and discard pending movement for
the affected motors; an already in-flight command cannot be recalled.

The main GUI renews a responsiveness lease every 20 ms. If it stops doing so
for 1 second, the worker discards pending commands, attempts `PAUSE_ALL`, stops
heartbeats, and closes the connection. Movement does not automatically resume
when the GUI recovers. The firmware heartbeat timeout remains the fallback if
the stop cannot be delivered; its 1-second timeout is checked every 500 ms, so
this is not an instantaneous-stop guarantee. Native dialogs that prevent Tk
timers from running can also trigger this disconnect.

Connections open asynchronously, and reconnect is blocked until the previous
worker finishes closing its port. Closing the GUI gives the worker up to one
second to finish without blocking the interface. An unresponsive OS driver can
outlast this deadline; the firmware timeout remains the fallback.

Hardware-free regression tests (from the repository root):

```bash
cd software
python -m unittest discover -s tests -v
```

The tests simulate stalled writes, unplugging, missing heartbeats, GUI stalls,
stop priority, and reconnect races. Tk integration tests require a working
desktop/Tk installation and skip when unavailable. They do not open a real
serial device or read/write your saved application state.

## Software Setup

From the repository root:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r software/requirements.txt
```

If you are using the existing local conda environment on this machine, launch with:

```bash
.venv-conda/bin/python software/monkeydrive_gui.py
```

Otherwise, with an activated environment:

```bash
python software/monkeydrive_gui.py
```

## Firmware Upload

Open this file in the Arduino IDE:

```text
firmware/monkeydrive_firmware/monkeydrive_firmware.ino
```

Use the correct Teensy board/port settings for the MonkeyDrive control unit, then click **Verify** and **Upload**.

The firmware currently supports:

- up to 6 motors
- serial commands with checksums
- runtime speed/backlash settings from the GUI
- heartbeat timeout stopping
- backlash compensation during direction changes
- travel limits from `0-50 mm`

## Launching The GUI

Start the GUI:

```bash
python software/monkeydrive_gui.py
```

The GUI remembers the previously used serial port and preselects it the next time it opens. The port dropdown is still available if the controller appears under a different port name.

Click **Connect** to connect to the control unit.

## Main Control Modes

### Target Mode

Target mode is for moving selected drives to explicit absolute positions.

For each selected motor:

1. Enter `Speed (mm/s)`.
2. Enter `Target (mm)`.
3. Click **Run** or press `Command+Enter` on macOS / `Control+Enter` on Windows/Linux.

Useful controls:

- **Pause**: pauses selected running motors
- **End** key: pause shortcut
- **Zero**: sets the relative zero point for selected motors
- **Clear**: clears selected motor state
- **Export**: exports current state to CSV

### Step Mode

Step mode is for moving selected drives by relative increments.

Controls:

- One shared `Step speed (mm/s)`
- One shared `Step size (mm)`
- **Step Up** moves selected drives toward `0`
- **Step Down** moves selected drives farther from `0`

Keyboard shortcuts in step mode:

- Number keys select a single motor and deselect the others
- Numpad `+` triggers Step Down
- Numpad `-` triggers Step Up
- Holding `+` or `-` jogs continuously until key release
- `End` pauses


## Settings

Click **Settings** to adjust runtime controller settings:

- minimum speed
- maximum speed
- backlash compensation

Settings are sent to the controller after connecting and are saved locally.

## Anatomy Viewer

Click **Anatomy** to load MRI anatomy data.

Supported formats:

- NIfTI: `.nii`, `.nii.gz`
- DICOM file
- DICOM folder


The anatomy viewer supports:

- sagittal/coronal/axial views
- contrast adjustment
- zoom
- mouse-wheel slice scrolling
- green crosshairs
- automatic reloading of the last anatomy dataset

## Trajectory Viewer

Each motor row has a **Trajectory** button. It is enabled once anatomy data has been loaded.

A trajectory is defined in MRI/world coordinates by:

- `X (mm)`
- `Y (mm)`
- A-P rotation
- M-L rotation
- twist rotation

Trajectory views:

- **Inline**
- **Inline 90**
- **Perpendicular**


Trajectory tools:

- **Set probe location**: locks the current perpendicular plane to the current motor position
- **Edit probe location**: unlocks the probe location for adjustment
- **Set target location**: adds a red target-depth line to inline views
- **Edit target location**: allows mouse-wheel adjustment of the red target line over inline views

The blue probe line updates as the linked motor position changes. The red target line is linked to the main GUI target value.

## Trajectory Summary

Click **Trajectory Summary** to open a grid of trajectory views across all drives.

Each tile shows one drive with:

- per-drive Inline / Inline 90 toggle
- saved trajectory zoom
- blue probe line
- red target line

The summary viewer runs in a separate subprocess so MRI rendering does not block the main control GUI.

Anatomy loading and slice computation also run on a background worker inside
each viewer. Obsolete render results are discarded, and zoom uses cached slices.
Probe-position updates redraw overlays without resampling the summary MRI.
Viewer communication keeps the latest position for each motor instead of
replaying a backlog, and scrolling an unlocked probe plane updates the summary.
The live perpendicular plane is refreshed at most every 100 ms; actual refresh
time also depends on rendering time and controller updates.

## GUI Stability Stress Test

To stress-test GUI responsiveness without commanding hardware:

```bash
python software/stress_gui_responsiveness.py --duration 60 --update-hz 30 --trajectory-viewers 6
```

This opens trajectory viewers and the summary viewer, simulates high-rate position updates, and reports main Tk event-loop latency.

Example passing output:

```text
Stress test results
  position updates scheduled: 5515
  Tk heartbeat gap: mean=21.1 ms, p95=22.5 ms, worst=82.9 ms
  urgent callback latency: mean=4.3 ms, p95=14.8 ms, worst=105.6 ms
  RESULT: PASS
```

This does not prove the GUI can never freeze, but it directly tests whether viewer rendering and high-rate position updates stall the main control window.

## Recommended V1.0 Hardware Checklist

Before routine use:

1. Upload the latest firmware.
2. Launch the GUI and connect to the control unit.
3. Confirm `Pause` and `End` stop selected running motors.
4. Confirm closing the GUI sends a stop command.
5. Confirm unplugging USB during movement causes motion to stop.
6. Verify target-mode movement.
7. Verify step-mode movement does not overwrite target-mode targets.
8. Verify backlash compensation with rapid direction reversals.
9. Load anatomy and confirm trajectory/probe/target overlays.
10. Run the GUI stress test for at least several minutes.
