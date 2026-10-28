# MonkeyDrive Lite

A minimal MonkeyDrive GUI and matching console firmware for controlling up to six microdrives. Use Lite for basic motor control, position tracking, notes, and CSV export. It does not include the primary codebase's MRI visualization, SpikeGLX integrations, and other advanced features.

The primary MonkeyDrive codebase remains the recommended starting point for the full system. Lite is a separate, self-contained alternative; use its GUI with the firmware in this directory.

## Install the GUI

Use a desktop Python installation with Tkinter (Python 3.12 is a suitable starting point). `python -m tkinter` should open a small test window. If Tkinter is missing, install the Tk package appropriate for your Python distribution; it is not installed by pip.

From the repository root:

```bash
cd lite/software
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python monkeydrive_gui.py
```

On Windows Command Prompt, replace the activation command with `.venv\Scripts\activate`. Use `python3` if that is your Python command. Keep this folder writable: the GUI automatically saves positions, settings, and notes to `software/monkeydrive_state.json` beside the script.

## Install the console firmware

1. Install the Arduino IDE. In Preferences/Settings, add `https://www.pjrc.com/teensy/package_teensy_index.json` to **Additional boards manager URLs**.
2. In Boards Manager, install the Teensy board support package.
3. Connect the console's Teensy 4.0 by USB. Select **Teensy 4.0** and its port in the IDE.
4. Open `lite/firmware/monkeydrive_firmware/monkeydrive_firmware.ino`, keeping it inside the folder of the same name.
5. Disconnect the GUI from the serial port, then upload the sketch. If needed, press the Teensy's programming button as described in the console manual.

The firmware targets the documented MonkeyDrive console wiring and motor mechanics. It uses Arduino/Teensy core facilities and requires no additional Arduino library in this sketch.

## Basic operation

1. With console power disconnected, connect the drives. Connect USB to the computer, then power the assembled console as described in the manual.
2. Launch the GUI, choose the Teensy port, and click **Connect**. Typical names are `COMx` on Windows and `/dev/cu.usbmodem…` on macOS. Reopening the port menu refreshes it.
3. Use **+ / −** to show the connected motor rows (up to six). Row numbers correspond to the console's numbered motor channels.
4. Check the carriage ruler. Double-click **Absolute**, enter its current position in millimeters, and press Enter. Confirm this value before every session; the system has no position encoder.
5. Select a motor's checkbox, enter **Speed** in mm/s and **Target** in absolute mm, then click **Run**. The default limits are 0–50 mm and 0.001–0.2 mm/s. Confirm the intended direction and travel with a short bench movement first.
6. **Pause** stops selected running motors. **Zero** sets selected motors' relative-position reference; it does not move the drive or change the absolute position. **Export** pauses movement and saves the current table to CSV. Notes and state are also saved automatically.

Read the [MonkeyDrive documentation](https://docs.google.com/document/d/1CwdIq8lv9Gtcey1GMihnH131U_tzeRZlZ0cmyOJbvFY/edit), especially the GUI Manual, Console Manual, and safety instructions. The software and firmware provide heartbeat checks and limits, but do not measure ground-truth position. Do not connect/disconnect drives while powered, let a drive hit a travel stop, or leave a running system unattended. Disconnect console power to stop movement in an emergency. Test the complete system on the bench before experimental use.

## Files and license

- `software/monkeydrive_gui.py`: Python/Tkinter GUI; `pyserial` is its external Python dependency.
- `firmware/monkeydrive_firmware/monkeydrive_firmware.ino`: matching Teensy 4.0 firmware.
- [Root MIT License](../LICENSE): covers the Lite GUI and firmware.
