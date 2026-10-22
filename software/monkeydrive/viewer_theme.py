"""Shared presentation for the separate Tk viewer processes.

Applied during construction only; never participates in image rendering or polling.
"""
import tkinter as tk
from tkinter import ttk, font as tkfont

from .console_theme import ConsoleTheme as Theme


def configure_viewer(window):
    # Keep fonts alive for this window without changing Tk's global named fonts.
    window._viewer_font = tkfont.nametofont('TkDefaultFont', root=window).copy()
    window._viewer_font.configure(size=11)
    window._viewer_heading_font = window._viewer_font.copy()
    window._viewer_heading_font.configure(size=17, weight='bold')
    window._viewer_section_font = window._viewer_font.copy()
    window._viewer_section_font.configure(size=11, weight='bold')
    style = ttk.Style(window)
    if style.theme_use() != 'clam':
        style.theme_use('clam')
    style.configure('Viewer.TButton', font=window._viewer_font, padding=(10, 6),
                    background=Theme.COLOR_ENTRY_BG, foreground=Theme.COLOR_FG,
                    bordercolor='#dce2e9', relief='flat')
    style.map('Viewer.TButton', background=[('active', Theme.COLOR_ACTIVE_BG)],
              foreground=[('disabled', '#929ba7')])
    style.configure('Primary.Viewer.TButton', background='#225da8', foreground='white')
    style.map('Primary.Viewer.TButton', background=[('active', '#194b89')],
              foreground=[('disabled', '#b7c6da'), ('!disabled', 'white')])
    style.configure('Viewer.TLabel', font=window._viewer_font,
                    background=Theme.COLOR_BG, foreground='#626e7d')
    style.configure('Heading.Viewer.TLabel', font=window._viewer_heading_font,
                    foreground=Theme.COLOR_FG)


def viewer_button(parent, **options):
    primary = options.get('text') in ('Save', 'Apply', 'Open NIfTI')
    options.setdefault('style', 'Primary.Viewer.TButton' if primary else 'Viewer.TButton')
    return ttk.Button(parent, **options)


def viewer_heading(parent, title, subtitle):
    ttk.Label(parent, text=title, style='Heading.Viewer.TLabel').pack(anchor='w')
    ttk.Label(parent, text=subtitle, style='Viewer.TLabel').pack(anchor='w', pady=(3, 16))


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
    elif isinstance(widget, tk.Entry):
        widget.configure(font=window._viewer_font, background='white', foreground=Theme.COLOR_FG,
                         insertbackground=Theme.COLOR_FG, readonlybackground=Theme.COLOR_BG,
                         relief='flat', borderwidth=0, highlightthickness=1,
                         highlightbackground='#dce2e9', highlightcolor='#225da8')
    elif isinstance(widget, tk.Scale):
        widget.configure(font=window._viewer_font, background=Theme.COLOR_BG,
                         foreground=Theme.COLOR_FG, activebackground=Theme.COLOR_ACTIVE_BG,
                         troughcolor='#dce2e9', highlightthickness=0, borderwidth=0,
                         sliderrelief='flat')
    # Canvas backgrounds and colors encode image/measurement information; leave intact.
    for child in widget.winfo_children():
        style_viewer(window, child)
