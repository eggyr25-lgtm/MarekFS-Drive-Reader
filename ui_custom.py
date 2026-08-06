"""MarekFS custom UI widgets: a frameless rounded window with a custom
title bar (drag to move, buttons for minimize / maximize / close), a
partition-creation dialog with custom size selection, and an explorer
wallpaper picker that tiles an image behind the file list."""
import os
import math
import tkinter as tk
from tkinter import ttk, filedialog, simpledialog, messagebox
from tkinter.font import Font

from marekfs_theme import (
    apply_theme, theme_color, rainbow_at, THEMES,
    register_rainbow_puck, unregister_rainbow_puck,
    register_rainbow_window, unregister_rainbow_window,
)
from marekfs_core import (
    SECTOR_SIZE, format_bytes, generate_partition_id,
    MAX_PARTITIONS, DEFAULT_DATA_AREA_RESERVE,
)


def theme_existing_window(win, parent=None, title=None, min_size=None):
    """Apply the shared MarekFS theme AND custom frameless chrome to a feature
    window.

    Every feature module funnels its Toplevels through this helper, so wiring
    the custom title bar (drag to move, minimize-to-corner, maximize, close)
    here means the whole app uses the same custom UI the main window uses —
    not the plain native Tk title bar.
    """
    source = parent or win
    name = getattr(source, "theme_var", None)
    name = name.get() if name is not None else "Glossy Dark"
    try:
        theme = apply_theme(win, name)
    except Exception:
        theme = getattr(win, "_marekfs_theme", None) or dict(THEMES["Glossy Dark"])
    if min_size:
        try: win.minsize(*min_size)
        except Exception: pass
    try:
        win.configure(bg=(getattr(win, "_marekfs_theme", {}) or {}).get("bg", "#10131c"))
    except Exception:
        pass
    try:
        add_custom_chrome(win, parent=parent, title=title, theme=theme)
    except Exception:
        # If the custom chrome cannot be applied on this platform, fall back to
        # the native title bar so the window is still usable.
        if title:
            try: win.title(title)
            except Exception: pass
    # Register for live RGB cycling if the window is on the Rainbow theme
    if (getattr(win, "_marekfs_theme", {}) or {}).get("rainbow"):
        try:
            register_rainbow_window(win)
        except Exception:
            pass
    return win


# ---------------------------------------------------------------------------
# Custom window chrome (frameless title bar) + minimize-to-corner puck
# ---------------------------------------------------------------------------

def _resolve_theme(theme, win):
    if isinstance(theme, dict) and theme:
        return theme
    return getattr(win, "_marekfs_theme", None) or dict(THEMES["Glossy Dark"])


class FloatingMinimizer:
    """Hides a window and drops a small, draggable icon in the bottom-right
    corner of the screen. Clicking the icon (without dragging) restores the
    window; dragging repositions the icon. In Rainbow theme the puck cycles
    through the RGB spectrum like a mouse LED."""

    def __init__(self, win, parent=None, on_restore=None, icon_text="🚀", theme=None):
        self.win = win
        self.on_restore = on_restore
        t = _resolve_theme(theme, win)
        accent = t.get("accent", "#00d2ff")
        surface = t.get("surface", "#171c2b")
        fg = t.get("fg", "#e8ecf5")

        try:
            self.win.withdraw()
        except Exception:
            pass

        self.puck = tk.Toplevel(parent or win)
        self.puck.overrideredirect(True)
        # Register for RGB cycling if the parent window is on the Rainbow theme
        if t.get("rainbow"):
            register_rainbow_puck(self)
        try:
            self.puck.attributes("-topmost", True)
        except Exception:
            pass
        size = 56
        try:
            sw = self.puck.winfo_screenwidth()
            sh = self.puck.winfo_screenheight()
        except Exception:
            sw, sh = 1280, 720
        x = max(0, sw - size - 28)
        y = max(0, sh - size - 64)
        self.puck.geometry(f"{size}x{size}+{x}+{y}")

        self.frame = tk.Frame(self.puck, bg=accent, highlightthickness=2,
                              highlightbackground=fg, highlightcolor=fg)
        self.frame.pack(fill=tk.BOTH, expand=True)
        self.label = tk.Label(self.frame, text=icon_text or "🚀", bg=accent, fg=surface,
                              font=("Segoe UI", 20, "bold"), cursor="hand2")
        self.label.pack(fill=tk.BOTH, expand=True)

        self._press = None
        self._moved = False
        for w in (self.frame, self.label):
            w.bind("<Button-1>", self._down)
            w.bind("<B1-Motion>", self._drag)
            w.bind("<ButtonRelease-1>", self._up)
            w.bind("<Enter>", lambda _e: self.frame.configure(bg=fg) or self.label.configure(bg=fg))
            w.bind("<Leave>", lambda _e: self._restore_puck_color())

    def _restore_puck_color(self):
        """Restore the puck to its CURRENT RGB color (Rainbow mode) or the
        initial accent otherwise — so hover-leave doesn't stick to a stale
        color while the spectrum is cycling underneath."""
        if (getattr(self.win, "_marekfs_theme", {}) or {}).get("rainbow"):
            try:
                puck = self
                # Read the last color the animator applied
                accent = getattr(puck, "_last_accent", None)
                on_accent = getattr(puck, "_last_on_accent", None)
                if accent and on_accent:
                    self.frame.configure(bg=accent)
                    self.label.configure(bg=accent, fg=on_accent)
                    return
            except Exception:
                pass
        try:
            self.frame.configure(bg=accent)
            self.label.configure(bg=accent, fg=surface)
        except Exception:
            pass

    def _down(self, e):
        self._press = (e.x_root, e.y_root, self.puck.winfo_x(), self.puck.winfo_y())
        self._moved = False

    def _drag(self, e):
        if not self._press:
            return
        dx = e.x_root - self._press[0]
        dy = e.y_root - self._press[1]
        if abs(dx) > 3 or abs(dy) > 3:
            self._moved = True
        self.puck.geometry(f"+{self._press[2] + dx}+{self._press[3] + dy}")

    def _up(self, _e):
        was_click = not self._moved
        self._press = None
        if was_click:
            self.restore()

    def restore(self):
        try:
            unregister_rainbow_puck(self)
        except Exception:
            pass
        try:
            self.puck.destroy()
        except Exception:
            pass
        try:
            self.win.deiconify()
            self.win.lift()
            try:
                self.win.attributes("-topmost", True)
                self.win.after(250, lambda: self._drop_topmost())
            except Exception:
                pass
            self.win.focus_force()
        except Exception:
            pass
        if self.on_restore:
            try:
                self.on_restore()
            except Exception:
                pass

    def _drop_topmost(self):
        try:
            self.win.attributes("-topmost", False)
        except Exception:
            pass


def minimize_to_corner(win, parent=None, on_restore=None, icon_text="🚀", theme=None):
    """Convenience wrapper returning a FloatingMinimizer for `win`."""
    return FloatingMinimizer(win, parent=parent, on_restore=on_restore,
                             icon_text=icon_text, theme=theme)


def _invoke_close(win):
    handler = getattr(win, "_chrome_close_handler", None)
    if callable(handler):
        try:
            handler()
            return
        except Exception:
            pass
    try:
        unregister_rainbow_window(win)
    except Exception:
        pass
    try:
        win.destroy()
    except Exception:
        pass


def add_custom_chrome(win, parent=None, title=None, theme=None):
    """Give an existing Toplevel a frameless custom title bar matching the
    main MarekFS window: draggable, with minimize-to-corner, maximize/restore
    and close controls. Content packed by the caller lands below the bar."""
    if getattr(win, "_marekfs_chrome", None):
        # Already themed once; just refresh the colors and title.
        _refresh_chrome_colors(win, theme)
        if title:
            try: win.title(title)
            except Exception: pass
        return win

    t = _resolve_theme(theme, win)
    surface = t.get("surface", "#171c2b")
    fg = t.get("fg", "#e8ecf5")
    accent = t.get("accent", "#00d2ff")
    bg = t.get("bg", "#10131c")

    try:
        win.overrideredirect(True)
    except Exception:
        return win

    initial_title = title or ""
    try:
        if not initial_title:
            current = win.wm_title()
            initial_title = "" if current in ("tk", "") else current
    except Exception:
        pass

    title_var = tk.StringVar(value=initial_title)

    bar = tk.Frame(win, bg=surface, height=34)
    bar.pack(fill=tk.X, side=tk.TOP)
    bar.pack_propagate(False)

    title_label = tk.Label(bar, textvariable=title_var, bg=surface, fg=fg,
                           font=("Segoe UI", 10, "bold"))
    title_label.pack(side=tk.LEFT, padx=12)

    btn_close = tk.Label(bar, text="✕", bg=surface, fg=fg, font=("Segoe UI", 11),
                        cursor="hand2", padx=10)
    btn_close.pack(side=tk.RIGHT, padx=2)
    btn_max = tk.Label(bar, text="□", bg=surface, fg=fg, font=("Segoe UI", 10),
                      cursor="hand2", padx=10)
    btn_max.pack(side=tk.RIGHT, padx=2)
    btn_min = tk.Label(bar, text="—", bg=surface, fg=fg, font=("Segoe UI", 11),
                      cursor="hand2", padx=10)
    btn_min.pack(side=tk.RIGHT, padx=2)

    state = {"drag_x": 0, "drag_y": 0, "maximized": False, "prev_geom": None}

    def _start_drag(e):
        state["drag_x"] = e.x
        state["drag_y"] = e.y

    def _do_drag(e):
        if state["maximized"]:
            return
        try:
            x = win.winfo_x() + e.x - state["drag_x"]
            y = win.winfo_y() + e.y - state["drag_y"]
            win.geometry(f"+{x}+{y}")
        except Exception:
            pass

    def _do_max():
        try:
            if state["maximized"]:
                if state["prev_geom"]:
                    win.geometry(state["prev_geom"])
                state["maximized"] = False
            else:
                state["prev_geom"] = win.geometry()
                sw = win.winfo_screenwidth()
                sh = win.winfo_screenheight()
                win.geometry(f"{sw - 20}x{sh - 60}+10+30")
                state["maximized"] = True
        except Exception:
            pass

    def _do_min():
        minimize_to_corner(win, parent=parent, icon_text="🚀",
                           theme=getattr(win, "_marekfs_theme", None) or t)

    for w in (bar, title_label):
        w.bind("<Button-1>", _start_drag)
        w.bind("<B1-Motion>", _do_drag)

    def _btn_base_color():
        """Return the live RGB accent in Rainbow mode, static surface
        otherwise — so hover-leave restores the CURRENT color."""
        if t.get("rainbow"):
            return getattr(win, "_marekfs_current_accent", accent)
        return surface

    def _btn_base_fg():
        if t.get("rainbow"):
            return getattr(win, "_marekfs_current_on_accent", fg)
        return fg

    btn_min.bind("<Button-1>", lambda _e: _do_min())
    btn_max.bind("<Button-1>", lambda _e: _do_max())
    btn_close.bind("<Button-1>", lambda _e: _invoke_close(win))
    btn_close.bind("<Enter>", lambda _e: btn_close.config(bg="#e53935", fg="#ffffff"))
    btn_close.bind("<Leave>", lambda _e: btn_close.config(bg=_btn_base_color(), fg=_btn_base_fg()))
    for b in (btn_max, btn_min):
        b.bind("<Enter>", lambda _e, w=b: w.config(bg=t.get("btn_active", "#2a3556")))
        b.bind("<Leave>", lambda _e, w=b: w.config(bg=_btn_base_color()))

    # Capture the caller's WM_DELETE_WINDOW handler so the close button runs the
    # window's own cleanup (stopping playback, releasing locks, saving, etc.).
    _orig_protocol = win.protocol

    def _protocol(name=None, func=None):
        if name == "WM_DELETE_WINDOW" and func is not None:
            win._chrome_close_handler = func
        try:
            return _orig_protocol(name, func)
        except Exception:
            return None

    win.protocol = _protocol

    # Keep the custom bar in sync when callers set win.title(...) after theming.
    _orig_wm_title = win.wm_title

    def _title(text=None):
        if text is None:
            return _orig_wm_title()
        try:
            title_var.set(text)
        except Exception:
            pass
        try:
            return _orig_wm_title(text)
        except Exception:
            return None

    win.title = _title

    win._marekfs_chrome = {
        "bar": bar, "title_label": title_label, "title_var": title_var,
        "btn_close": btn_close, "btn_max": btn_max, "btn_min": btn_min,
    }
    # Register for live RGB cycling if the window is on the Rainbow theme
    if t.get("rainbow"):
        try:
            register_rainbow_window(win)
        except Exception:
            pass
    return win


def _refresh_chrome_colors(win, theme):
    chrome = getattr(win, "_marekfs_chrome", None)
    if not chrome:
        return
    t = _resolve_theme(theme, win)
    # On Rainbow theme the live RGB ticks drive the chrome colors; just
    # make sure the window is registered for the sync set.
    if t.get("rainbow"):
        try:
            register_rainbow_window(win)
        except Exception:
            pass
        return
    surface = t.get("surface", "#171c2b")
    fg = t.get("fg", "#e8ecf5")
    for key in ("bar", "title_label", "btn_close", "btn_max", "btn_min"):
        widget = chrome.get(key)
        if widget is None:
            continue
        try:
            widget.configure(bg=surface)
            if key != "bar":
                widget.configure(fg=fg)
        except Exception:
            pass


def stable_widget_width(widget, width):
    """Reserve a stable width for text/status controls during theme changes."""
    try:
        widget.configure(width=width)
    except Exception:
        pass
    return widget


# ---------------------------------------------------------------------------
# Rounded window helpers
# ---------------------------------------------------------------------------

def _round_rect_path(canvas, x0, y0, x1, y1, radius):
    """Draw a rounded rectangle outline on a canvas and return the item id."""
    points = []
    # top edge
    points.extend([x0 + radius, y0, x1 - radius, y0])
    # top-right corner
    points.extend([x1 - radius, y0, x1, y0, x1, y0 + radius])
    # right edge
    points.extend([x1, y0 + radius, x1, y1 - radius])
    # bottom-right corner
    points.extend([x1, y1 - radius, x1, y1, x1 - radius, y1])
    # bottom edge
    points.extend([x1 - radius, y1, x0 + radius, y1])
    # bottom-left corner
    points.extend([x0 + radius, y1, x0, y1, x0, y1 - radius])
    # left edge
    points.extend([x0, y1 - radius, x0, y0 + radius])
    # top-left corner
    points.extend([x0, y0 + radius, x0, y0, x0 + radius, y0])
    return canvas.create_polygon(*points, smooth=True, outline="", fill="")


def make_rounded_window(parent, title, width, height, theme_name="Glossy Dark",
                         on_close=None, min_size=(400, 300)):
    """Create a Toplevel with a custom title bar and rounded corners.

    Returns (toplevel, content_frame, title_bar, win_state_dict) where
    content_frame is where the caller packs its widgets.
    """
    win = tk.Toplevel(parent)
    win.overrideredirect(True)  # remove native title bar / border
    win.geometry(f"{width}x{height}")
    win.minsize(*min_size)
    win.configure(bg=parent.cget("bg"))

    state = {
        "maximized": False,
        "prev_geom": None,
        "drag_x": 0,
        "drag_y": 0,
        "theme": theme_name,
        "radius": 14,
        "on_close": on_close,
        "rainbow_anim": None,
        "rainbow_items": [],
    }

    t = apply_theme(win, theme_name)
    bg = t["bg"]
    accent = t["accent"]
    fg = t["fg"]
    surface = t["surface"]

    # The rounded mask is achieved by placing a canvas border behind.
    # On platforms where overrideredirect + transparency is not available
    # we still get a clean custom title bar; the corners just won't be
    # physically clipped. We draw a rounded frame as a visual cue.
    border_canvas = tk.Canvas(win, bg=bg, highlightthickness=0)
    border_canvas.place(x=0, y=0, relwidth=1, relheight=1)

    # Rounded outline
    radius = state["radius"]
    state["round_item"] = border_canvas.create_polygon(
        0, 0, 0, 0, smooth=True, outline=accent, fill=surface, width=2
    )

    def _draw_rounded():
        w = win.winfo_width()
        h = win.winfo_height()
        r = radius
        border_canvas.coords(state["round_item"],
            r, 0, w - r, 0, w, 0, w, r, w, h - r, w, h, w - r, h, r, h, 0, h, 0, h - r, 0, r, 0, 0)
        border_canvas.itemconfig(state["round_item"], smooth=True)

    # Title bar
    title_bar = tk.Frame(win, bg=surface, height=40)
    title_bar.place(x=radius, y=2, width=width - 2 * radius, height=36)

    title_label = tk.Label(title_bar, text=title, bg=surface, fg=fg,
                           font=("Segoe UI", 10, "bold"))
    title_label.pack(side=tk.LEFT, padx=12)

    # Window control buttons
    btn_close = tk.Label(title_bar, text="\u2715", bg=surface, fg=fg,
                         font=("Segoe UI", 11), cursor="hand2", padx=8)
    btn_close.pack(side=tk.RIGHT, padx=4)
    btn_max = tk.Label(title_bar, text="\u25a1", bg=surface, fg=fg,
                       font=("Segoe UI", 11), cursor="hand2", padx=8)
    btn_max.pack(side=tk.RIGHT, padx=4)
    btn_min = tk.Label(title_bar, text="\u2013", bg=surface, fg=fg,
                       font=("Segoe UI", 11), cursor="hand2", padx=8)
    btn_min.pack(side=tk.RIGHT, padx=4)

    # Content frame — caller packs here
    content = tk.Frame(win, bg=bg)
    content.place(x=radius, y=42, width=width - 2 * radius, height=height - 42 - radius)

    # --- Behavior ---
    def _start_drag(e):
        state["drag_x"] = e.x
        state["drag_y"] = e.y

    def _do_drag(e):
        if state["maximized"]:
            return
        dx = e.x - state["drag_x"]
        dy = e.y - state["drag_y"]
        x = win.winfo_x() + dx
        y = win.winfo_y() + dy
        win.geometry(f"+{x}+{y}")

    def _do_min():
        # Temporarily restore native window management before iconifying;
        # Tk rejects iconify() while override-redirect is enabled.
        try:
            win.overrideredirect(False)
            win.iconify()
        except tk.TclError:
            try: win.withdraw()
            except Exception: pass

    def _do_max():
        if state["maximized"]:
            win.geometry(state["prev_geom"])
            state["maximized"] = False
        else:
            state["prev_geom"] = win.geometry()
            sw = win.winfo_screenwidth()
            sh = win.winfo_screenheight()
            win.geometry(f"{sw-20}x{sh-60}+10+30")
            state["maximized"] = True
        win.after(50, _draw_rounded)

    def _do_close():
        if state["on_close"]:
            state["on_close"]()
        win.destroy()

    for w in (title_bar, title_label):
        w.bind("<Button-1>", _start_drag)
        w.bind("<B1-Motion>", _do_drag)

    btn_min.bind("<Button-1>", lambda e: _do_min())
    btn_max.bind("<Button-1>", lambda e: _do_max())
    btn_close.bind("<Button-1>", lambda e: _do_close())

    # Hover effects on buttons
    def _hover(lbl, active_color):
        lbl.bind("<Enter>", lambda e: lbl.config(bg=active_color))
        lbl.bind("<Leave>", lambda e: lbl.config(bg=surface))
    _hover(btn_close, "#e53935")
    _hover(btn_max, t["btn_active"])
    _hover(btn_min, t["btn_active"])

    win.bind("<Configure>", lambda e: win.after(10, _draw_rounded))
    win.after(50, _draw_rounded)

    # Rainbow accent — static palette so the frame never wiggles. No animator
    # re-paints the border on a timer (that caused visible flicker/relayout).
    if t.get("rainbow"):
        try:
            from marekfs_theme import rainbow_static_override
            rb = rainbow_static_override(t)
            accent = rb.get("accent", accent)
        except Exception:
            accent = t.get("accent", "#ff0044")
        try:
            border_canvas.itemconfig(state["round_item"], outline=accent)
            title_label.config(bg=accent, fg=t.get("bg", "#0d0d12"))
            title_bar.config(bg=accent)
        except Exception:
            pass

    state["title_label"] = title_label
    state["title_bar"] = title_bar
    state["btn_close"] = btn_close
    state["btn_max"] = btn_max
    state["btn_min"] = btn_min
    state["border_canvas"] = border_canvas

    return win, content, title_bar, state


# ---------------------------------------------------------------------------
# Partition creation dialog with size selection
# ---------------------------------------------------------------------------

class AddPartitionDialog:
    """A dialog that lets the user pick a size (in MB/GB) for a new partition
    before creating it. Calls back with the chosen size in bytes."""

    def __init__(self, parent, existing_count, on_create):
        self.parent = parent
        self.on_create = on_create
        self.win = tk.Toplevel(parent)
        self.win.title("➕ Create New Partition")
        self.win.geometry("440x340")
        self.win.resizable(True, True)
        self.win.configure(bg=parent.cget("bg"))

        t = getattr(parent, "_marekfs_theme", None) or {}
        bg = t.get("bg", "#10131c")
        surface = t.get("surface", "#171c2b")
        accent = t.get("accent", "#00d2ff")
        fg = t.get("fg", "#e8ecf5")

        header = tk.Frame(self.win, bg=surface, height=50)
        header.pack(fill=tk.X)
        tk.Label(header, text="➕ Create New Partition", bg=surface, fg=accent,
                 font=("Segoe UI", 14, "bold")).pack(side=tk.LEFT, padx=16, pady=12)

        body = tk.Frame(self.win, bg=bg)
        body.pack(fill=tk.BOTH, expand=True, padx=20, pady=16)

        remaining = MAX_PARTITIONS - existing_count
        tk.Label(body, text=f"Partitions: {existing_count}/{MAX_PARTITIONS} used  ({remaining} remaining)",
                 bg=bg, fg=fg, font=("Segoe UI", 10)).pack(anchor=tk.W, pady=(0, 12))

        tk.Label(body, text="Partition size:", bg=bg, fg=fg,
                 font=("Segoe UI", 10, "bold")).pack(anchor=tk.W)
        size_frame = tk.Frame(body, bg=bg)
        size_frame.pack(fill=tk.X, pady=(4, 8))

        self.size_var = tk.DoubleVar(value=1.0)
        self.unit_var = tk.StringVar(value="GB")
        self.size_entry = tk.Entry(size_frame, textvariable=self.size_var, width=10,
                                    font=("Segoe UI", 12), bg=t.get("entry", "#0c0f17"),
                                    fg=fg, insertbackground=fg, relief="flat", bd=2)
        self.size_entry.pack(side=tk.LEFT, padx=(0, 6))
        unit_combo = ttk.Combobox(size_frame, textvariable=self.unit_var, width=6,
                                   values=["MB", "GB", "TB"], state="readonly")
        unit_combo.pack(side=tk.LEFT)

        # Quick presets
        preset_frame = tk.Frame(body, bg=bg)
        preset_frame.pack(fill=tk.X, pady=(4, 12))
        tk.Label(preset_frame, text="Quick presets:", bg=bg, fg=fg,
                 font=("Segoe UI", 9)).pack(anchor=tk.W)
        presets_row = tk.Frame(preset_frame, bg=bg)
        presets_row.pack(fill=tk.X, pady=4)
        for label, val, unit in [("512 MB", 512, "MB"), ("1 GB", 1, "GB"),
                                  ("5 GB", 5, "GB"), ("10 GB", 10, "GB"),
                                  ("50 GB", 50, "GB")]:
            b = tk.Button(presets_row, text=label, relief="flat", bd=2,
                          bg=t.get("btn_bg", "#1f2740"), fg=fg,
                          activebackground=t.get("btn_active", "#2a3556"),
                          font=("Segoe UI", 9),
                          command=lambda v=val, u=unit: self._set_preset(v, u))
            b.pack(side=tk.LEFT, padx=3)

        # Slider
        self.slider_var = tk.DoubleVar(value=1.0)
        slider_label_frame = tk.Frame(body, bg=bg)
        slider_label_frame.pack(fill=tk.X)
        tk.Label(slider_label_frame, text="Fine-tune:", bg=bg, fg=fg,
                 font=("Segoe UI", 9)).pack(side=tk.LEFT)
        self.slider_val_lbl = tk.Label(slider_label_frame, text="1.0 GB", bg=bg, fg=accent,
                                        font=("Segoe UI", 9, "bold"))
        self.slider_val_lbl.pack(side=tk.RIGHT)
        self.slider = ttk.Scale(body, from_=0.1, to=100.0, orient=tk.HORIZONTAL,
                                variable=self.slider_var, command=self._on_slider)
        self.slider.pack(fill=tk.X, pady=(2, 12))

        # Preview label
        self.preview_var = tk.StringVar(value="New partition: 1.00 GB (1,073,741,824 bytes)")
        tk.Label(body, textvariable=self.preview_var, bg=bg, fg=fg,
                 font=("Segoe UI", 9)).pack(anchor=tk.W, pady=(0, 8))

        # Buttons
        btn_frame = tk.Frame(body, bg=bg)
        btn_frame.pack(fill=tk.X, pady=(4, 0))
        tk.Button(btn_frame, text="Cancel", relief="flat", bd=2,
                  bg=t.get("btn_bg", "#1f2740"), fg=fg, font=("Segoe UI", 10),
                  command=self.win.destroy).pack(side=tk.RIGHT, padx=4)
        tk.Button(btn_frame, text="✓ Create Partition", relief="flat", bd=2,
                  bg=accent, fg=bg, font=("Segoe UI", 10, "bold"),
                  command=self._do_create).pack(side=tk.RIGHT, padx=4)

        self._update_preview()

    def _set_preset(self, val, unit):
        self.unit_var.set(unit)
        if unit == "MB":
            self.size_var.set(float(val))
            self.slider_var.set(val / 1024.0)
        elif unit == "GB":
            self.size_var.set(float(val))
            self.slider_var.set(float(val))
        else:
            self.size_var.set(float(val))
            self.slider_var.set(float(val))
        self._update_preview()

    def _on_slider(self, val):
        v = float(val)
        self.size_var.set(round(v, 2))
        self._update_preview()

    def _compute_bytes(self):
        try:
            size = float(self.size_var.get())
        except Exception:
            size = 1.0
        unit = self.unit_var.get()
        mult = {"MB": 1024 * 1024, "GB": 1024**3, "TB": 1024**4}.get(unit, 1024**3)
        return int(size * mult)

    def _update_preview(self):
        b = self._compute_bytes()
        unit = self.unit_var.get()
        try:
            sv = float(self.size_var.get())
        except Exception:
            sv = 1.0
        self.preview_var.set(f"New partition: {sv:.2f} {unit} ({b:,} bytes)")
        self.slider_val_lbl.config(text=f"{sv:.1f} {unit}")

    def _do_create(self):
        size_bytes = self._compute_bytes()
        if size_bytes < SECTOR_SIZE:
            messagebox.showwarning("Too Small", "Partition must be at least 512 bytes (1 sector).")
            return
        self.win.destroy()
        self.on_create(size_bytes)


# ---------------------------------------------------------------------------
# Explorer wallpaper
# ---------------------------------------------------------------------------

class WallpaperManager:
    """Manages an optional background image tiled behind the file list.
    Stores the path in-memory (not persisted)."""

    def __init__(self):
        self.path = None
        self.photo = None  # holds PhotoImage ref

    def choose(self, parent):
        path = filedialog.askopenfilename(
            title="Select Explorer Wallpaper",
            filetypes=[("Images", "*.png *.jpg *.jpeg *.gif *.bmp *.webp"), ("All files", "*.*")],
        )
        if not path:
            return None
        self.path = path
        self._load_photo(parent)
        return path

    def _load_photo(self, parent):
        try:
            from PIL import Image, ImageTk
            im = Image.open(self.path)
            # We tile at a moderate size
            im.thumbnail((256, 256), Image.LANCZOS)
            self.photo = ImageTk.PhotoImage(im)
        except Exception:
            try:
                self.photo = tk.PhotoImage(file=self.path)
            except Exception:
                self.photo = None

    def clear(self):
        self.path = None
        self.photo = None
