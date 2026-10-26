"""Shared presentation for the separate Tk viewer processes.

Applied during construction only; never participates in image rendering or polling.
"""
import tkinter as tk
from tkinter import ttk, font as tkfont

from .console_theme import ConsoleTheme as Theme


def configure_viewer(window):
    # Panels share styles, so retain their fonts for the interpreter's lifetime.
    root = window._root()
    if not hasattr(root, '_viewer_fonts'):
        normal = tkfont.nametofont('TkDefaultFont', root=window).copy()
        normal.configure(size=11)
        heading = normal.copy()
        heading.configure(size=17, weight='bold')
        section = normal.copy()
        section.configure(size=11, weight='bold')
        root._viewer_fonts = normal, heading, section
    window._viewer_font, window._viewer_heading_font, window._viewer_section_font = root._viewer_fonts
    style = ttk.Style(window)
    if style.theme_use() != 'clam':
        style.theme_use('clam')
    style.configure('Viewer.TButton', font=window._viewer_font, padding=(10, 6),
                    background=Theme.COLOR_ENTRY_BG, foreground=Theme.COLOR_FG,
                    bordercolor='#dce2e9', relief='flat')
    style.map('Viewer.TButton', background=[('active', Theme.COLOR_ACTIVE_BG)],
              foreground=[('disabled', '#929ba7')])
    style.configure('Selected.Viewer.TButton', background='#eaf1fa', foreground='#225da8')
    style.configure('Primary.Viewer.TButton', background='#225da8', foreground='white')
    style.map('Primary.Viewer.TButton', background=[('active', '#194b89')],
              foreground=[('disabled', '#b7c6da'), ('!disabled', 'white')])
    style.configure('Viewer.TLabel', font=window._viewer_font,
                    background=Theme.COLOR_BG, foreground='#626e7d')
    style.configure('Heading.Viewer.TLabel', font=window._viewer_heading_font,
                    foreground=Theme.COLOR_FG)
    # Separate flat images let the handle be white without recoloring the track
    # or adding a border to the control.
    root = window._root()
    if not hasattr(root, '_zoom_slider_images'):
        track = tk.PhotoImage(master=root, width=1, height=15)
        track.put('#dce2e9', to=(0, 0, 1, 15))
        thumb = tk.PhotoImage(master=root, width=30, height=15)
        thumb.put('#ffffff', to=(0, 0, 30, 15))
        root._zoom_slider_images = (track, thumb)
        style.element_create('PlainZoom.trough', 'image', track, sticky='ew')
        style.element_create('PlainZoom.slider', 'image', thumb)
        style.layout('PlainZoom.Horizontal.TScale', [
            ('PlainZoom.trough', {'sticky': 'ew', 'children': [
                ('PlainZoom.slider', {'side': 'left', 'sticky': ''})]})])
    style.configure('PlainZoom.Horizontal.TScale', background=Theme.COLOR_BG)


def viewer_button(parent, **options):
    primary = options.get('text') in ('Save', 'Apply', 'Open NIfTI')
    options.setdefault('style', 'Primary.Viewer.TButton' if primary else 'Viewer.TButton')
    return ttk.Button(parent, **options)


def viewer_heading(parent, title, subtitle):
    ttk.Label(parent, text=title, style='Heading.Viewer.TLabel').pack(anchor='w')
    ttk.Label(parent, text=subtitle, style='Viewer.TLabel').pack(anchor='w', pady=(3, 16))


def viewer_zoom(parent, variable, command, maximum, length):
    """Flat gray track and white handle, retaining tenth-step zoom changes."""
    def changed(value):
        zoom = max(0.5, min(maximum, round(float(value), 1)))
        if abs(variable.get() - zoom) > 1e-9:
            variable.set(zoom)
        command()

    slider = ttk.Scale(parent, from_=0.5, to=maximum, orient=tk.HORIZONTAL,
                       variable=variable, command=changed, length=length,
                       style='PlainZoom.Horizontal.TScale')

    def nudge(delta):
        changed(variable.get() + delta)
        return 'break'

    for key, delta in (('Left', -0.1), ('Down', -0.1), ('Right', 0.1), ('Up', 0.1)):
        slider.bind(f'<{key}>', lambda _event, step=delta: nudge(step))
    return slider


def style_viewer(window, widget=None):
    widget = widget or window
    if isinstance(widget, (tk.Toplevel, tk.Frame)):
        widget.configure(background=Theme.COLOR_BG)
        if isinstance(widget, tk.Frame) and str(widget.cget('relief')) == 'sunken':
            widget.configure(relief='flat', borderwidth=0, highlightthickness=1,
                             highlightbackground='#dce2e9')
    elif isinstance(widget, tk.Label):
        original = tkfont.Font(root=window, font=widget.cget('font'))
        font = window._viewer_section_font if original.actual('weight') == 'bold' else window._viewer_font
        widget.configure(background=Theme.COLOR_BG, foreground=Theme.COLOR_FG, font=font)
    elif isinstance(widget, (tk.Checkbutton, tk.Radiobutton)):
        widget.configure(font=window._viewer_font, background=Theme.COLOR_BG,
                         foreground=Theme.COLOR_FG, activebackground=Theme.COLOR_ACTIVE_BG,
                         activeforeground=Theme.COLOR_FG, selectcolor='white', highlightthickness=0)
    elif isinstance(widget, tk.Entry) and not isinstance(widget, ttk.Entry):
        widget.configure(font=window._viewer_font, background='white', foreground=Theme.COLOR_FG,
                         insertbackground=Theme.COLOR_FG, readonlybackground=Theme.COLOR_BG,
                         relief='flat', borderwidth=0, highlightthickness=1,
                         highlightbackground='#dce2e9', highlightcolor='#225da8')
    elif isinstance(widget, tk.Scale) and not isinstance(widget, ttk.Scale):
        widget.configure(font=window._viewer_font, background=Theme.COLOR_BG,
                         foreground=Theme.COLOR_FG, activebackground=Theme.COLOR_ACTIVE_BG,
                         troughcolor='#dce2e9', highlightthickness=0, borderwidth=0,
                         sliderrelief='flat')
    # Canvas backgrounds and colors encode image/measurement information; leave intact.
    for child in widget.winfo_children():
        style_viewer(window, child)
