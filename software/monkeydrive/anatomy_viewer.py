"""Anatomy viewer window for MRI volumes."""

from __future__ import annotations

import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from pathlib import Path
from typing import Callable, Optional

from .anatomy import (
    AnatomyDependencyError,
    AnatomyLoader,
    AnatomyOrientation,
    AnatomySliceRenderer,
    AnatomyVolume,
)
from .config import Config


class AnatomyViewer(tk.Toplevel):
    """Window for loading and inspecting anatomical MRI volumes."""

    PLANES = (
        ("Sagittal", 0),
        ("Coronal", 1),
        ("Axial", 2),
    )

    def __init__(
        self,
        parent: tk.Tk,
        initial_path: str = "",
        initial_orientation_actual: str = "",
        initial_orientation_scanner: str = "",
        on_path_change: Optional[Callable[[str], None]] = None,
        on_orientation_change: Optional[Callable[[str, str], None]] = None,
    ):
        """Initialize the anatomy viewer."""
        super().__init__(parent)

        self._on_path_change = on_path_change
        self._on_orientation_change = on_orientation_change
        self._initial_path = initial_path
        self._orientation_actual_key = initial_orientation_actual
        self._orientation_scanner_key = initial_orientation_scanner
        self._original_volume: AnatomyVolume | None = None
        self._volume: AnatomyVolume | None = None
        self._image_refs: dict[int, tk.PhotoImage] = {}
        self._image_canvases: dict[int, tk.Canvas] = {}
        self._image_items: dict[int, int] = {}
        self._slice_vars: dict[int, tk.IntVar] = {}
        self._slice_scales: dict[int, tk.Scale] = {}
        self._contrast_low_var = tk.DoubleVar(value=1.0)
        self._contrast_high_var = tk.DoubleVar(value=99.0)
        self._zoom_var = tk.DoubleVar(value=1.0)
        self._suppress_slice_callback: bool = False
        self._status_var = tk.StringVar(value="No anatomy loaded")
        self._details_var = tk.StringVar(value="")
        self._orientation_status: str = ""

        self.title("Anatomy Viewer")
        self.geometry("1180x720")
        self.minsize(960, 600)

        parent_x = parent.winfo_x()
        parent_y = parent.winfo_y()
        parent_width = parent.winfo_width()
        self.geometry(f"+{parent_x + parent_width + 10}+{parent_y}")

        self._build_ui()
        self._style_widget_tree()
        if self._initial_path and Path(self._initial_path).exists():
            self.after(100, lambda: self._load_path(self._initial_path, remember=False))

    def _build_ui(self):
        container = tk.Frame(self, padx=12, pady=12)
        container.pack(fill=tk.BOTH, expand=True)

        toolbar = tk.Frame(container)
        toolbar.pack(fill=tk.X, pady=(0, 10))

        tk.Button(toolbar, text="Open NIfTI", command=self._open_nifti).pack(side=tk.LEFT, padx=(0, 6))
        tk.Button(toolbar, text="Open DICOM File", command=self._open_dicom_file).pack(side=tk.LEFT, padx=(0, 6))
        tk.Button(toolbar, text="Open DICOM Folder", command=self._open_dicom_folder).pack(side=tk.LEFT, padx=(0, 6))
        tk.Button(toolbar, text="Orientation", command=self._open_orientation_dialog).pack(side=tk.LEFT, padx=(0, 18))

        tk.Label(toolbar, text="Zoom:").pack(side=tk.LEFT, padx=(0, 6))
        tk.Button(toolbar, text="-", command=lambda: self._adjust_zoom(-0.1), width=3).pack(side=tk.LEFT)
        tk.Scale(
            toolbar, from_=0.5, to=3.0, resolution=0.1, orient=tk.HORIZONTAL,
            variable=self._zoom_var, command=self._on_zoom_change,
            showvalue=True, length=180
        ).pack(side=tk.LEFT, padx=4)
        tk.Button(toolbar, text="+", command=lambda: self._adjust_zoom(0.1), width=3).pack(side=tk.LEFT)

        viewer_frame = tk.Frame(container)
        viewer_frame.pack(fill=tk.BOTH, expand=True)

        contrast_frame = tk.Frame(viewer_frame)
        contrast_frame.pack(side=tk.LEFT, fill=tk.Y, padx=(0, 8))
        tk.Label(contrast_frame, text="Contrast").pack(anchor='n')
        self._contrast_control = ContrastWindowControl(
            contrast_frame,
            low_var=self._contrast_low_var,
            high_var=self._contrast_high_var,
            command=self._on_contrast_change
        )
        self._contrast_control.pack(anchor='n')

        image_row = tk.Frame(viewer_frame)
        image_row.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        for column, (label, axis) in enumerate(self.PLANES):
            pane = tk.Frame(image_row, relief=tk.SUNKEN, borderwidth=1, padx=6, pady=6)
            pane.grid(row=0, column=column, sticky='nsew', padx=4)
            image_row.grid_columnconfigure(column, weight=1, uniform="anatomy")
            image_row.grid_rowconfigure(0, weight=1)

            tk.Label(pane, text=label, font=('TkDefaultFont', 11, 'bold')).pack(anchor='w')

            image_canvas = tk.Canvas(
                pane, width=340, height=360, background="#000000",
                highlightthickness=0
            )
            image_canvas.pack(fill=tk.BOTH, expand=True, pady=(4, 4))
            self._image_canvases[axis] = image_canvas
            image_canvas.create_text(170, 180, text="Load anatomy", fill="#ffffff", tags=("placeholder",))
            self._bind_slice_scroll(image_canvas, axis)
            image_canvas.bind("<Configure>", lambda _event, plane_axis=axis: self._render_plane(plane_axis))
            self._bind_slice_scroll(pane, axis)

            slice_var = tk.IntVar(value=0)
            self._slice_vars[axis] = slice_var
            scale = tk.Scale(
                pane, from_=0, to=0, orient=tk.HORIZONTAL,
                variable=slice_var,
                command=lambda _value, plane_axis=axis: self._on_slice_change(plane_axis),
                showvalue=True
            )
            scale.pack(fill=tk.X)
            self._slice_scales[axis] = scale

        tk.Label(container, textvariable=self._status_var, anchor='w').pack(fill=tk.X, pady=(10, 0))
        tk.Label(container, textvariable=self._details_var, anchor='w').pack(fill=tk.X)

    def _open_nifti(self):
        path = filedialog.askopenfilename(
            parent=self,
            title="Open NIfTI anatomy",
            filetypes=[
                ("NIfTI files", ("*.nii", "*.gz")),
                ("All files", "*.*"),
            ],
        )
        if path:
            self._load_path(path)

    def _open_dicom_file(self):
        path = filedialog.askopenfilename(
            parent=self,
            title="Open DICOM anatomy file",
            filetypes=[
                ("DICOM files", "*.dcm *.ima *"),
                ("All files", "*.*"),
            ],
        )
        if path:
            self._load_path(path)

    def _open_dicom_folder(self):
        path = filedialog.askdirectory(parent=self, title="Open DICOM series folder")
        if path:
            self._load_path(path)

    def _load_path(self, path: str, remember: bool = True):
        try:
            volume = AnatomyLoader.load(path)
        except AnatomyDependencyError as exc:
            messagebox.showwarning("Missing MRI Dependencies", str(exc), parent=self)
            return
        except Exception as exc:
            messagebox.showerror("Anatomy Load Failed", str(exc), parent=self)
            return

        self._original_volume = volume
        self._volume = volume
        self._orientation_status = ""
        if remember:
            self._orientation_actual_key = ""
            self._orientation_scanner_key = ""
            if self._on_orientation_change:
                self._on_orientation_change("", "")
        else:
            self._apply_saved_orientation_if_available()
        self._update_volume_labels()
        self._set_all_slices_to_middle()
        self._render_all_planes()
        if remember and self._on_path_change:
            self._on_path_change(path)

    def _update_volume_labels(self):
        if not self._volume:
            self._status_var.set("No anatomy loaded")
            self._details_var.set("")
            return

        self._status_var.set(self._volume.display_name)

        voxel_text = ""
        if self._volume.voxel_sizes:
            voxel_text = " | voxel size: " + ", ".join(f"{v:g} mm" for v in self._volume.voxel_sizes)
        codes_text = ""
        if self._volume.orientation_codes:
            codes_text = " | orientation: " + "".join(self._volume.orientation_codes)
        orientation_text = f" | {self._orientation_status}" if self._orientation_status else ""
        self._details_var.set(f"shape: {self._volume.shape}{voxel_text}{codes_text}{orientation_text}")

    def _open_orientation_dialog(self):
        if not self._original_volume:
            messagebox.showinfo("No Anatomy Loaded", "Load an MRI volume before correcting orientation.", parent=self)
            return

        OrientationWindow(
            self,
            on_apply=self._apply_orientation_correction,
            on_restore=self._restore_original_orientation,
        )

    def _apply_orientation_correction(self, actual_key: str, scanner_key: str):
        if not self._original_volume:
            return

        try:
            # Never modify source MRI files or the original loaded volume. Orientation
            # correction is applied only to this viewer's in-memory display copy.
            self._volume = AnatomyOrientation.correct_volume(
                self._original_volume, actual_key, scanner_key
            )
        except Exception as exc:
            messagebox.showerror("Orientation Correction Failed", str(exc), parent=self)
            return

        self._orientation_actual_key = actual_key
        self._orientation_scanner_key = scanner_key
        actual_label = AnatomyOrientation.label(actual_key)
        scanner_label = AnatomyOrientation.label(scanner_key)
        self._orientation_status = f"orientation corrected: actual {actual_label}, scanner {scanner_label}"
        self._update_volume_labels()
        self._set_all_slices_to_middle()
        self._render_all_planes()
        if self._on_orientation_change:
            self._on_orientation_change(actual_key, scanner_key)

    def _restore_original_orientation(self):
        if not self._original_volume:
            return

        self._volume = self._original_volume
        self._orientation_status = ""
        self._orientation_actual_key = ""
        self._orientation_scanner_key = ""
        self._update_volume_labels()
        self._set_all_slices_to_middle()
        self._render_all_planes()
        if self._on_orientation_change:
            self._on_orientation_change("", "")

    def _apply_saved_orientation_if_available(self):
        if not self._original_volume:
            return
        if not (self._orientation_actual_key and self._orientation_scanner_key):
            return

        try:
            self._volume = AnatomyOrientation.correct_volume(
                self._original_volume,
                self._orientation_actual_key,
                self._orientation_scanner_key,
            )
        except Exception:
            self._orientation_actual_key = ""
            self._orientation_scanner_key = ""
            self._volume = self._original_volume
            self._orientation_status = ""
            return

        actual_label = AnatomyOrientation.label(self._orientation_actual_key)
        scanner_label = AnatomyOrientation.label(self._orientation_scanner_key)
        self._orientation_status = f"orientation corrected: actual {actual_label}, scanner {scanner_label}"

    def _set_all_slices_to_middle(self):
        if not self._volume:
            for axis, scale in self._slice_scales.items():
                scale.configure(from_=0, to=0)
                self._slice_vars[axis].set(0)
            return

        for _label, axis in self.PLANES:
            max_index = max(0, self._volume.shape[axis] - 1)
            self._slice_scales[axis].configure(from_=0, to=max_index)
            self._slice_vars[axis].set(max_index // 2)

    def _render_all_planes(self):
        for _label, axis in self.PLANES:
            self._render_plane(axis)

    def _render_plane(self, axis: int):
        if not self._volume:
            return
        canvas = self._image_canvases.get(axis)
        if not canvas or not canvas.winfo_exists():
            return

        try:
            low, high = self._contrast_limits()
            image_data = AnatomySliceRenderer.slice_to_ppm(
                self._volume,
                axis,
                self._slice_vars[axis].get(),
                contrast_low=low,
                contrast_high=high,
                zoom=self._zoom_var.get(),
            )
            image_ref = tk.PhotoImage(data=image_data, format="PPM")
            self._draw_plane(axis, image_ref)
            self._image_refs[axis] = image_ref
        except tk.TclError:
            return
        except Exception as exc:
            canvas.delete("all")
            canvas.create_text(
                canvas.winfo_width() // 2,
                canvas.winfo_height() // 2,
                text=f"Could not render slice:\n{exc}",
                fill="#ffffff",
            )

    def _draw_plane(self, axis: int, image_ref: tk.PhotoImage):
        canvas = self._image_canvases[axis]
        canvas.update_idletasks()
        width = max(1, canvas.winfo_width())
        height = max(1, canvas.winfo_height())
        image_width = image_ref.width()
        image_height = image_ref.height()
        left = (width - image_width) / 2
        top = (height - image_height) / 2

        canvas.delete("placeholder")
        canvas.delete("crosshair")
        if axis in self._image_items:
            canvas.itemconfigure(self._image_items[axis], image=image_ref)
            canvas.coords(self._image_items[axis], width / 2, height / 2)
        else:
            self._image_items[axis] = canvas.create_image(
                width / 2, height / 2, image=image_ref, anchor=tk.CENTER
            )
        self._draw_crosshairs(canvas, axis, left, top, image_width, image_height)

    def _redraw_crosshairs(self, axis: int):
        image_ref = self._image_refs.get(axis)
        canvas = self._image_canvases.get(axis)
        if not image_ref or not canvas or not canvas.winfo_exists():
            return

        canvas.update_idletasks()
        width = max(1, canvas.winfo_width())
        height = max(1, canvas.winfo_height())
        image_width = image_ref.width()
        image_height = image_ref.height()
        left = (width - image_width) / 2
        top = (height - image_height) / 2

        canvas.delete("crosshair")
        self._draw_crosshairs(canvas, axis, left, top, image_width, image_height)

    def _redraw_all_crosshairs(self, skip_axis: int | None = None):
        for _label, axis in self.PLANES:
            if axis != skip_axis:
                self._redraw_crosshairs(axis)

    def _draw_crosshairs(self, canvas: tk.Canvas, axis: int, left: float, top: float, width: int, height: int):
        if not self._volume:
            return

        x_axis, y_axis = self._display_axes_for_plane(axis)
        x_index = self._slice_vars[x_axis].get()
        y_index = self._slice_vars[y_axis].get()
        x = left + self._axis_fraction(x_axis, x_index) * width
        y = top + (1.0 - self._axis_fraction(y_axis, y_index)) * height
        color = "#00cc33"

        canvas.create_line(left, y, left + width, y, fill=color, width=1, tags=("crosshair",))
        canvas.create_line(x, top, x, top + height, fill=color, width=1, tags=("crosshair",))

    def _display_axes_for_plane(self, axis: int) -> tuple[int, int]:
        if axis == 0:
            return 1, 2
        if axis == 1:
            return 0, 2
        return 0, 1

    def _axis_fraction(self, axis: int, index: int) -> float:
        if not self._volume:
            return 0.5
        max_index = max(1, self._volume.shape[axis] - 1)
        return max(0.0, min(1.0, index / max_index))

    def _contrast_limits(self) -> tuple[float, float]:
        low = float(self._contrast_low_var.get())
        high = float(self._contrast_high_var.get())
        if high <= low:
            high = min(100.0, low + 1.0)
            self._contrast_high_var.set(high)
        return low, high

    def _on_contrast_change(self, _value=None):
        self._contrast_limits()
        self._render_all_planes()

    def _on_slice_change(self, _axis: int):
        if self._suppress_slice_callback:
            return
        self._render_plane(_axis)
        self._redraw_all_crosshairs(skip_axis=_axis)

    def _on_zoom_change(self, _value=None):
        self._render_all_planes()

    def _adjust_zoom(self, delta: float):
        zoom = max(0.5, min(3.0, round(float(self._zoom_var.get()) + delta, 1)))
        self._zoom_var.set(zoom)
        self._render_all_planes()

    def _bind_slice_scroll(self, widget: tk.Widget, axis: int):
        widget.bind("<MouseWheel>", lambda event, plane_axis=axis: self._on_mouse_wheel(event, plane_axis))
        widget.bind("<Button-4>", lambda event, plane_axis=axis: self._on_mouse_wheel(event, plane_axis))
        widget.bind("<Button-5>", lambda event, plane_axis=axis: self._on_mouse_wheel(event, plane_axis))

    def _on_mouse_wheel(self, event, axis: int):
        if not self._volume:
            return "break"

        if getattr(event, "num", None) == 4 or getattr(event, "delta", 0) > 0:
            step = 1
        else:
            step = -1

        max_index = max(0, self._volume.shape[axis] - 1)
        current = int(self._slice_vars[axis].get())
        next_index = max(0, min(max_index, current + step))
        if next_index != current:
            try:
                self._suppress_slice_callback = True
                self._slice_vars[axis].set(next_index)
            finally:
                self._suppress_slice_callback = False
            self._render_plane(axis)
            self._redraw_all_crosshairs(skip_axis=axis)
        return "break"

    def _style_widget_tree(self, widget=None):
        widget = widget or self

        try:
            if isinstance(widget, (tk.Tk, tk.Toplevel, tk.Frame)):
                widget.configure(background=Config.COLOR_BG)
            elif isinstance(widget, tk.Canvas):
                if widget not in self._image_canvases.values():
                    widget.configure(background=Config.COLOR_BG, highlightthickness=0)
            elif isinstance(widget, tk.Label):
                widget.configure(background=Config.COLOR_BG, foreground=Config.COLOR_FG)
            elif isinstance(widget, tk.Button):
                widget.configure(
                    background=Config.COLOR_ENTRY_BG,
                    foreground=Config.COLOR_FG,
                    activebackground=Config.COLOR_ACTIVE_BG,
                    activeforeground=Config.COLOR_FG,
                    highlightbackground=Config.COLOR_BG,
                )
            elif isinstance(widget, tk.Radiobutton):
                widget.configure(
                    background=Config.COLOR_BG,
                    foreground=Config.COLOR_FG,
                    activebackground=Config.COLOR_ACTIVE_BG,
                    activeforeground=Config.COLOR_FG,
                    selectcolor=Config.COLOR_ENTRY_BG,
                )
            elif isinstance(widget, tk.Scale):
                widget.configure(
                    background=Config.COLOR_BG,
                    foreground=Config.COLOR_FG,
                    activebackground=Config.COLOR_ACTIVE_BG,
                    highlightbackground=Config.COLOR_BG,
                )
        except tk.TclError:
            pass

        for child in widget.winfo_children():
            self._style_widget_tree(child)


class ContrastWindowControl(tk.Canvas):
    """Vertical two-handle contrast control for MRI display windows."""

    WIDTH = 74
    HEIGHT = 210
    BAR_LEFT = 29
    BAR_RIGHT = 43
    BAR_TOP = 12
    BAR_BOTTOM = 188

    def __init__(self, parent, low_var: tk.DoubleVar, high_var: tk.DoubleVar, command):
        super().__init__(
            parent,
            width=self.WIDTH,
            height=self.HEIGHT,
            background=Config.COLOR_BG,
            highlightthickness=0,
        )
        self._low_var = low_var
        self._high_var = high_var
        self._command = command
        self._dragging = None

        self.bind("<Button-1>", self._on_press)
        self.bind("<B1-Motion>", self._on_drag)
        self.bind("<ButtonRelease-1>", self._on_release)
        self._draw()

    def _draw(self):
        self.delete("all")

        for y in range(self.BAR_TOP, self.BAR_BOTTOM + 1):
            fraction = 1.0 - ((y - self.BAR_TOP) / (self.BAR_BOTTOM - self.BAR_TOP))
            value = int(fraction * 255)
            color = f"#{value:02x}{value:02x}{value:02x}"
            self.create_line(self.BAR_LEFT, y, self.BAR_RIGHT, y, fill=color)

        self.create_rectangle(
            self.BAR_LEFT,
            self.BAR_TOP,
            self.BAR_RIGHT,
            self.BAR_BOTTOM,
            outline="#c8c8c8",
        )
        self._draw_handle("high", self._value_to_y(self._high_var.get()), side="right")
        self._draw_handle("low", self._value_to_y(self._low_var.get()), side="left")

    def _draw_handle(self, tag: str, y: float, side: str):
        if side == "left":
            points = [
                self.BAR_LEFT - 2, y,
                self.BAR_LEFT - 14, y - 10,
                self.BAR_LEFT - 14, y + 10,
            ]
        else:
            points = [
                self.BAR_RIGHT + 2, y,
                self.BAR_RIGHT + 14, y - 10,
                self.BAR_RIGHT + 14, y + 10,
            ]

        self.create_polygon(points, fill="#ffffff", outline="#cccccc", tags=(tag,))

    def _on_press(self, event):
        low_y = self._value_to_y(self._low_var.get())
        high_y = self._value_to_y(self._high_var.get())
        self._dragging = "low" if abs(event.y - low_y) <= abs(event.y - high_y) else "high"
        self._set_drag_value(event.y)

    def _on_drag(self, event):
        self._set_drag_value(event.y)

    def _on_release(self, _event):
        self._dragging = None

    def _set_drag_value(self, y: float):
        if not self._dragging:
            return

        value = self._y_to_value(y)
        if self._dragging == "low":
            value = max(0.0, min(value, self._high_var.get() - 1.0))
            self._low_var.set(round(value, 1))
        else:
            value = min(100.0, max(value, self._low_var.get() + 1.0))
            self._high_var.set(round(value, 1))

        self._draw()
        self._command()

    def _value_to_y(self, value: float) -> float:
        value = max(0.0, min(float(value), 100.0))
        fraction = value / 100.0
        return self.BAR_BOTTOM - (fraction * (self.BAR_BOTTOM - self.BAR_TOP))

    def _y_to_value(self, y: float) -> float:
        y = max(self.BAR_TOP, min(float(y), self.BAR_BOTTOM))
        fraction = (self.BAR_BOTTOM - y) / (self.BAR_BOTTOM - self.BAR_TOP)
        return fraction * 100.0


class OrientationWindow(tk.Toplevel):
    """Dialog for correcting mismatched actual and scanner orientations."""

    def __init__(self, parent: AnatomyViewer, on_apply, on_restore):
        super().__init__(parent)

        self._on_apply = on_apply
        self._on_restore = on_restore
        self._actual_var = tk.StringVar(value=AnatomyOrientation.SUPINE_HEAD_FIRST)
        self._scanner_var = tk.StringVar(value=AnatomyOrientation.SUPINE_HEAD_FIRST)

        self.title("Orientation Correction")
        self.resizable(False, False)

        parent_x = parent.winfo_x()
        parent_y = parent.winfo_y()
        parent_width = parent.winfo_width()
        self.geometry(f"+{parent_x + parent_width + 10}+{parent_y}")

        frame = tk.Frame(self, padx=20, pady=16)
        frame.pack(fill=tk.BOTH, expand=True)

        actual_frame = tk.Frame(frame)
        actual_frame.grid(row=0, column=0, sticky='nw', padx=(0, 28))
        scanner_frame = tk.Frame(frame)
        scanner_frame.grid(row=0, column=1, sticky='nw')

        tk.Label(actual_frame, text='"Actual" Orientation', font=('TkDefaultFont', 12, 'bold')).pack(anchor='w')
        for spec in AnatomyOrientation.actual_options():
            tk.Radiobutton(
                actual_frame, text=spec.label, variable=self._actual_var,
                value=spec.key, command=self._refresh_apply_state
            ).pack(anchor='w', pady=1)

        tk.Label(scanner_frame, text='"Scanner" Orientation', font=('TkDefaultFont', 12, 'bold')).pack(anchor='w')
        for spec in AnatomyOrientation.scanner_options():
            tk.Radiobutton(
                scanner_frame, text=spec.label, variable=self._scanner_var,
                value=spec.key, command=self._refresh_apply_state
            ).pack(anchor='w', pady=1)

        button_frame = tk.Frame(frame)
        button_frame.grid(row=1, column=0, columnspan=2, pady=(16, 0))

        tk.Button(
            button_frame, text="Restore Original", command=self._restore, width=16
        ).pack(side=tk.LEFT, padx=(0, 10))
        self._apply_button = tk.Button(
            button_frame, text="Apply", command=self._apply, width=12
        )
        self._apply_button.pack(side=tk.LEFT)

        help_text = (
            'Set the "actual" orientation of the subject as physically positioned in the scanner. '
            'Set the "scanner" orientation as specified by the scanner operator. Click "Apply" '
            'to correct the displayed dataset. Click "Restore Original" to return to the loaded data.'
        )
        tk.Label(
            frame, text=help_text, wraplength=520, justify=tk.LEFT, anchor='w'
        ).grid(row=2, column=0, columnspan=2, sticky='ew', pady=(20, 0))

        self._style_widget_tree()
        self._refresh_apply_state()

    def _refresh_apply_state(self):
        state = tk.NORMAL
        if self._actual_var.get() == self._scanner_var.get():
            state = tk.DISABLED
        self._apply_button.configure(state=state)

    def _apply(self):
        self._on_apply(self._actual_var.get(), self._scanner_var.get())
        self.destroy()

    def _restore(self):
        self._on_restore()
        self.destroy()

    def _style_widget_tree(self, widget=None):
        widget = widget or self

        try:
            if isinstance(widget, (tk.Tk, tk.Toplevel, tk.Frame)):
                widget.configure(background=Config.COLOR_BG)
            elif isinstance(widget, tk.Label):
                widget.configure(background=Config.COLOR_BG, foreground=Config.COLOR_FG)
            elif isinstance(widget, tk.Button):
                widget.configure(
                    background=Config.COLOR_ENTRY_BG,
                    foreground=Config.COLOR_FG,
                    activebackground=Config.COLOR_ACTIVE_BG,
                    activeforeground=Config.COLOR_FG,
                    highlightbackground=Config.COLOR_BG,
                )
            elif isinstance(widget, tk.Radiobutton):
                widget.configure(
                    background=Config.COLOR_BG,
                    foreground=Config.COLOR_FG,
                    activebackground=Config.COLOR_ACTIVE_BG,
                    activeforeground=Config.COLOR_FG,
                    selectcolor=Config.COLOR_ENTRY_BG,
                )
        except tk.TclError:
            pass

        for child in widget.winfo_children():
            self._style_widget_tree(child)
