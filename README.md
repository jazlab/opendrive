# OpenDrive

**OpenDrive: An open-source microdrive for primate neurophysiology**

This repository contains the OpenDrive software and firmware. The OpenDrive system controls motorized microdrives for positioning neural probes during neuroscience experiments. Its desktop interface supports up to six drives, with tools for movement programs, MRI-based trajectory planning, and SpikeGLX integration.

## Project resources

- [Project website](https://jazlab.github.io/monkeydrive/): overview of the OpenDrive system and project highlights.
- **Paper**: write-up on the system design and validation.
- [Paper reproducibility](https://github.com/jazlab/monkeydrive_paper): data and code to reproduce the figures in the paper.
- [OpenDrive chatbot](https://notebook.google.com/notebook/b31beb9a-0dcf-4763-a446-20673b686bbd/preview): ask questions about building and using OpenDrive. Google sign-in required.
- [Documentation](https://docs.google.com/document/d/1CwdIq8lv9Gtcey1GMihnH131U_tzeRZlZ0cmyOJbvFY/edit?tab=t.0): user manuals, hardware files, assembly instructions, bill of materials, and quality testing.

## What you need

- **For the desktop app:** Windows or macOS, Python 3.10 or newer with Tkinter,
  and Git to clone the repository.
- **For motor control:** an OpenDrive control unit connected by USB, compatible
  microdrives, and the matching firmware. The firmware in this repository targets
  a Teensy 4.0.
- **For anatomy and trajectory viewing:** your MRI data as a NIfTI file
  (`.nii` or `.nii.gz`).

MRI data is optional for motor control and is not included in the repository.

## Install and launch

Clone the repository, then run the commands for your operating system from the
repository folder.

```sh
git clone https://github.com/jazlab/monkeydrive.git opendrive
cd opendrive
```

### Windows — PowerShell

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r software/requirements.txt
.\.venv\Scripts\python.exe software/opendrive_gui.py
```

### macOS — Terminal

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r software/requirements.txt
.venv/bin/python software/opendrive_gui.py
```

To open OpenDrive again, return to the repository folder and run the last
command for your operating system. No environment activation is required.

If Python reports that `tkinter` or `_tkinter` is missing, use a Python
installation that includes Tk support and recreate the virtual environment.

## First connection and movement

Before using a probe, check movement direction, position calibration, travel
limits, and stopping behavior with your hardware in a safe setup. The current
software and firmware use an absolute travel range of **0–50 mm**.

1. Connect the control unit by USB. Choose its serial port in the upper-right
   dropdown and click **Connect**. Close other applications using that port,
   such as the Arduino Serial Monitor.
2. Use **+ / −** above the drive rows to show the drives you need. Removing a row
   pauses that drive and retains its positions, notes, program, and trajectory;
   adding it back restores that state, including after restarting or loading a
   config. Check the box
   beside each drive you intend to control; movement buttons apply to selected
   drives.
3. Choose a movement mode:

   | Mode | Use it to | Basic controls |
   | --- | --- | --- |
   | **Target** | Move to an absolute position | Enter a target in mm and speed in mm/s, then click **Run Selected**. |
   | **Step** | Move by a fixed increment | Set the shared step size and speed, then click **Step Up** or **Step Down**. Up moves toward zero; Down moves away from zero. |
   | **Program** | Run a sequence of movements | Edit a drive's instructions, select the drive, and click **Run Selected**. Programs can be saved and loaded. |

4. Click **Pause Selected**, or press **End**, to pause selected running drives.

In Target and Program modes, **Ctrl+Enter** (Windows) or **Cmd+Enter** (macOS)
also triggers **Run Selected**. Plain Enter only confirms field edits.

Choose **View → Compact View** for a small position monitor with live absolute
and relative readouts, drive selection, **Run Selected**, and **Pause All**.
Run Selected uses the targets or program configured in the full console and is
disabled in Step mode. Click **Expand** to return to the full console.
The view choice is saved with the workspace. In Compact View, Run/jog
shortcuts are disabled and **End** pauses all drives; existing movements and
programs continue unless paused.

**Pause All** is always available below the movement controls. It sends a
controller-wide stop regardless of drive selection or displayed running status.
In Step mode, holding **+ / −** jogs the original selected drives. Releasing the
key stops the jog; changing selection or mode, leaving the control window, or
minimizing also cancels it. After a stop, release the key before starting again.

Each drive shows **Running** while the app tracks a commanded movement.

### Positions and zeroing

**Absolute** position is the drive position used for target commands. **Relative**
position is measured from a zero point you choose.

- **Zero** at the end of a drive row resets only that drive's relative position,
  regardless of which drives are selected.
- **Zero Selected** resets the relative positions of all selected drives.
- Zeroing does **not** move a motor or change its absolute position.

Double-click a position or note to edit it. **Settings** contains speed bounds
and backlash compensation; check these against your hardware before moving.

## Anatomy and trajectory planning

Start with **Anatomy** in the main window. Click **Open NIfTI** to load your MRI, then inspect the sagittal, coronal, and axial slices. Scroll over a view
to change slices, and adjust zoom and contrast as needed. Verify anatomical
orientation before planning a trajectory.

Once anatomy is loaded, click **Trajectories**. This window contains:

- **Explorer:** plan a path using the Inline, Inline 90, and Perpendicular planes.
  The vertical **AP**, **Lat**, and **Twist** sliders rotate the path. Scroll over
  a plane to move through it, or enter the X/Y/Z origin directly.
  Choose a drive and click **Assign to drive** to copy the planned trajectory to it.
- **Summary:** compare the trajectories and probe/target overlays for all drives.
- **Drive tabs:** adjust an individual drive's trajectory, set its probe location
  relative to the motor position, and set a target location. Blue lines indicate
  probe depth; red lines indicate target depth.

**Assigning a trajectory does not move the drive.** Verify the relationship
between the MRI coordinates, physical probe, and motor position before using
the overlays to guide movement.

## Saved settings and data

OpenDrive automatically saves its workspace and settings to
`software/opendrive_state.json`, including drive state, trajectories, and the
last anatomy path. MRI files stay in their original location. The state file is
local to your checkout and is excluded from Git.

Use **File → Save Config…** to save a named JSON configuration, and **File →
Load Config…** to restore it later. Configurations include drive positions and
relative zero points, programs, notes, trajectories, Explorer settings, controller
settings, and the anatomy path and orientation. MRI data is referenced at its
existing location rather than copied into the config.

Loading replaces the current workspace and disconnects the controller first.
It does not move motors or resume programs. Open Anatomy and Trajectories windows
are refreshed with the loaded configuration. Automatic startup restore continues
to use the latest workspace, including any config you load.

Saved positions are not a substitute for checking the physical drive position
after reconnecting or restarting hardware. Use **Export** to save a CSV snapshot
of the drive data.

## Firmware and development

If your controller is not already programmed, the firmware is in
[`firmware/opendrive_firmware/opendrive_firmware.ino`](firmware/opendrive_firmware/opendrive_firmware.ino).
Open it in the Arduino IDE with Teensy support, select the matching board and
port, and upload it. Confirm the pin assignments and motor configuration match
your control unit before use.

On connection, the GUI checks the controller's firmware version and verifies
that it matches the application. If it detects an incompatible variant or
version, or older firmware that cannot identify itself, it shows **Firmware
Update Required** and prevents movement. Upload the matching sketch from this
checkout and reconnect.

The firmware requires a valid heartbeat before accepting movement commands.
After more than one second without one, it stops motion before issuing further
step pulses and rejects new movement commands until heartbeats return. A returning
heartbeat does not resume the interrupted movement; a new Run command is required.

The desktop entry point is
[`software/opendrive_gui.py`](software/opendrive_gui.py); supporting modules
are in [`software/opendrive/`](software/opendrive/).

For the code layout, control flow, and test commands, see the
[developer guide](docs/development.md).

## License

The [MIT License](LICENSE) covers the OpenDrive project code in this repository,
including the GUI, firmware, and website source.
Third-party dependencies and externally hosted assets retain their own licenses.
