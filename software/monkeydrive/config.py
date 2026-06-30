"""Configuration constants for MonkeyDrive."""

class Config:
    """Global configuration parameters for the MonkeyDrive system."""

    # Serial communication
    BAUD_RATE: int = 115200
    SERIAL_TIMEOUT: float = 0.1

    # Motor limits
    MAX_MOTORS: int = 6
    MIN_ABSOLUTE_POS: float = 0.0      # mm
    MAX_ABSOLUTE_POS: float = 50.0     # mm
    MIN_SPEED: float = 0.001           # mm/s (inclusive)
    MAX_SPEED: float = 0.2             # mm/s (inclusive)
    MIN_TARGET_POS: float = 0.0        # mm
    MAX_TARGET_POS: float = 50.0       # mm
    DEFAULT_BACKLASH_MM: float = 0.058

    # Timing intervals (seconds)
    HEARTBEAT_INTERVAL: float = 0.5
    HEARTBEAT_TIMEOUT: float = 1.0
    POSITION_UPDATE_INTERVAL: float = 0.2
    TRAJECTORY_LIVE_UPDATE_INTERVAL_MS: int = 750
    AUTO_SAVE_INTERVAL: float = 5.0
    SERIAL_READ_INTERVAL: float = 0.02

    # Data persistence
    DATA_FILE: str = "monkeydrive_state.json"

    # GUI Layout
    WINDOW_TITLE: str = "MonkeyDrive"
    DECIMAL_PLACES: int = 3
    ENTRY_WIDTH: int = 12
    MODE_TARGET: str = "target"
    MODE_STEP: str = "step"
    MODE_PROGRAM: str = "program"
    DEFAULT_STEP_SPEED: float = 0.01
    DEFAULT_STEP_SIZE: float = 0.05
    SPEED_KEY_INCREMENT: float = 0.001
    STEP_SPEED_KEY_INCREMENT: float = 0.01
    TARGET_KEY_INCREMENT: float = 0.1
    STEP_SIZE_KEY_INCREMENT: float = 0.01
    STEP_JOG_HOLD_DELAY_MS: int = 300
    STEP_KEY_RELEASE_DEBOUNCE_MS: int = 100
    PROGRAM_TARGET_TOLERANCE_MM: float = 0.002
    COLOR_BG: str = "#f0f0f0"
    COLOR_FG: str = "#000000"
    COLOR_ENTRY_BG: str = "#ffffff"
    COLOR_READONLY_BG: str = "#d9d9d9"
    COLOR_ACTIVE_BG: str = "#e5e5e5"
    COLOR_SELECT_BG: str = "#4a90e2"
    COLOR_ROW_SELECTED_BG: str = "#cfe3ff"
    COLOR_MODE_TAB_BG: str = "#d9d9d9"

    # Column Weights and Min Sizes for Grid Layout
    # Col 0: Buttons/ID | Col 1: Speed | Col 2: Target | Col 3: Abs | Col 4: Rel | Col 5: Notes
    COL_MIN_WIDTHS: tuple = (205, 100, 100, 100, 100, 150)
    STEP_COL_MIN_WIDTHS: tuple = (205, 120, 120, 150)
    PROGRAM_COL_MIN_WIDTHS: tuple = (205, 140, 80, 100, 100, 100, 100, 150)
    ROW_PAD_Y: int = 2
    HEADER_PAD_Y: tuple = (5, 10)
