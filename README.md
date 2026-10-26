# MonkeyDrive

MonkeyDrive controls motorized microdrives for positioning electrodes and neural
probes during neuroscience experiments. Its desktop interface supports up to six
drives, with tools for viewing MRI anatomy and planning probe trajectories.

You can move drives to a target, advance them in small steps, or run a sequence of
movements. MRI viewing and trajectory planning are available without connecting
hardware.

## What you need

- **For the desktop app:** Windows or macOS, Python 3.10 or newer with Tkinter,
  and Git to clone the repository.
- **For motor control:** a MonkeyDrive control unit connected by USB, compatible
  microdrives, and the matching firmware. The firmware in this repository targets
  a Teensy 4.0.
- **For anatomy and trajectory viewing:** your MRI data as a NIfTI file
  (`.nii` or `.nii.gz`), a DICOM file, or a DICOM series folder.

MRI data is optional for motor control and is not included in the repository.

## Install and launch

Clone the repository, then run the commands for your operating system from the
repository folder. Access to this private repository requires an authorized
GitHub account.

```sh
git clone https://github.com/jazlab/MonkeyDrive.git
cd MonkeyDrive
```

### Windows — PowerShell

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r software/requirements.txt
.\.venv\Scripts\python.exe software/monkeydrive_gui.py
```

### macOS — Terminal

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r software/requirements.txt
.venv/bin/python software/monkeydrive_gui.py
```

To open MonkeyDrive again, return to the repository folder and run the last
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
2. Use **+ / −** above the drive rows to show the drives you need. Check the box
   beside each drive you intend to control; movement buttons apply to selected
   drives.
3. Choose a movement mode:

   | Mode | Use it to | Basic controls |
   | --- | --- | --- |
   | **Target** | Move to an absolute position | Enter a target in mm and speed in mm/s, then click **Run**. |
   | **Step** | Move by a fixed increment | Set the shared step size and speed, then click **Step Up** or **Step Down**. Up moves toward zero; Down moves away from zero. |
   | **Program** | Run a sequence of movements | Edit a drive's instructions, select the drive, and click **Run**. Programs can be saved and loaded. |

4. Click **Pause**, or press **End**, to pause selected running drives.

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

Start with **Anatomy** in the main window. Load your MRI using the NIfTI or DICOM
controls, then inspect the sagittal, coronal, and axial slices. Scroll over a view
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

MonkeyDrive automatically saves its workspace and settings to
`software/monkeydrive_state.json`, including drive state, trajectories, and the
last anatomy path. MRI files stay in their original location. The state file is
local to your checkout and is excluded from Git.

Saved positions are not a substitute for checking the physical drive position
after reconnecting or restarting hardware. Use **Export** to save a CSV snapshot
of the drive data.

## Firmware and development

If your controller is not already programmed, the firmware is in
[`firmware/monkeydrive_firmware/monkeydrive_firmware.ino`](firmware/monkeydrive_firmware/monkeydrive_firmware.ino).
Open it in the Arduino IDE with Teensy support, select the matching board and
port, and upload it. Confirm the pin assignments and motor configuration match
your control unit before use.

The desktop entry point is
[`software/monkeydrive_gui.py`](software/monkeydrive_gui.py); supporting modules
are in [`software/monkeydrive/`](software/monkeydrive/).
