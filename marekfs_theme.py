"""MarekFS theming: glossy/matte surfaces, dark/bright, plus purple,
diamond, gold, and rainbow palettes. Applies colors + surface style to a
ttk app. The Rainbow theme uses a LIVE RGB animator that cycles EVERY
color-bearing style through the spectrum (like a MAD DOG RGB mouse) —
backgrounds, surfaces, buttons, text, title bar, and minimize pucks all
shift hue together with per-zone offsets. No theme_use() is re-called on
ticks, so the layout never wiggles."""
import colorsys
import math
import tkinter as tk
from tkinter import ttk

THEMES = {
    "Glossy Dark": {
        "bg": "#10131c", "fg": "#e8ecf5", "accent": "#00d2ff",
        "surface": "#171c2b", "entry": "#0c0f17",
        "btn_bg": "#1f2740", "btn_fg": "#e8ecf5", "btn_active": "#2a3556",
        "glossy": True, "font": ("Segoe UI", 10),
    },
    "Glossy Bright": {
        "bg": "#f4f7fb", "fg": "#10131c", "accent": "#0a84ff",
        "surface": "#ffffff", "entry": "#ffffff",
        "btn_bg": "#e6edf7", "btn_fg": "#10131c", "btn_active": "#d4e0f2",
        "glossy": True, "font": ("Segoe UI", 10),
    },
    "Matte Dark": {
        "bg": "#1a1a1a", "fg": "#cfcfcf", "accent": "#7c7c7c",
        "surface": "#222222", "entry": "#161616",
        "btn_bg": "#2a2a2a", "btn_fg": "#cfcfcf", "btn_active": "#333333",
        "glossy": False, "font": ("Segoe UI", 10),
    },
    "Matte Bright": {
        "bg": "#eeeeee", "fg": "#222222", "accent": "#555555",
        "surface": "#f7f7f7", "entry": "#ffffff",
        "btn_bg": "#dddddd", "btn_fg": "#222222", "btn_active": "#cccccc",
        "glossy": False, "font": ("Segoe UI", 10),
    },
    "Purple": {
        "bg": "#160a2e", "fg": "#e6d6ff", "accent": "#a855f7",
        "surface": "#22113f", "entry": "#0e0620",
        "btn_bg": "#3a1e6b", "btn_fg": "#f0e6ff", "btn_active": "#4d2a8a",
        "glossy": True, "font": ("Segoe UI", 10),
    },
    "Diamond": {
        "bg": "#08161f", "fg": "#d6f5ff", "accent": "#7df9ff",
        "surface": "#0f2330", "entry": "#05101a",
        "btn_bg": "#16435a", "btn_fg": "#e6ffff", "btn_active": "#1c5a78",
        "glossy": True, "font": ("Segoe UI", 10),
    },
    "Gold": {
        "bg": "#160f02", "fg": "#ffe9b0", "accent": "#ffd700",
        "surface": "#241a06", "entry": "#0e0901",
        "btn_bg": "#5a4408", "btn_fg": "#fff3cf", "btn_active": "#7a5c0a",
        "glossy": True, "font": ("Segoe UI", 10),
    },
    "Rainbow": {
        "bg": "#0d0d12", "fg": "#f0f0f5", "accent": "#ff0044",
        "surface": "#16161f", "entry": "#0a0a10",
        "btn_bg": "#1e1e2e", "btn_fg": "#f0f0f5", "btn_active": "#2e2e42",
        "glossy": True, "font": ("Segoe UI", 10),
        "rainbow": True,
    },
}

THEME_NAMES = list(THEMES.keys())

# Default colors used when a key is missing from a theme.
_DEFAULT = {
    "rainbow": False,
    "glossy": True,
    "font": ("Segoe UI", 10),
}


def _rainbow_color(t: float, sat: float = 0.85, val: float = 1.0) -> str:
    """Return a hex color from the rainbow at position t (0..1)."""
    r, g, b = colorsys.hsv_to_rgb(t % 1.0, sat, val)
    return f"#{int(r*255):02x}{int(g*255):02x}{int(b*255):02x}"


def rainbow_at(phase: float) -> str:
    """Public helper: rainbow color at a given phase (0..1)."""
    return _rainbow_color(phase)


# Fixed hue offsets used by the static rainbow palette. Picking the accent,
# button and hover colors from three well-separated rainbow phases keeps the
# "rainbow" feel without any animation.
_RAINBOW_ACCENT_PHASE = 0.00   # red
_RAINBOW_BTN_PHASE = 0.30      # green
_RAINBOW_ACTIVE_PHASE = 0.58   # blue


def rainbow_static_override(theme=None):
    """Return a fixed rainbow override dict (accent / btn_bg / btn_active).

    The Rainbow theme applies this palette ONCE. Nothing re-applies it on a
    timer, so colors never change after the first paint and the layout cannot
    reflow ("wiggle"). `theme` is accepted for API compatibility with callers
    that hold the base theme dict.
    """
    return {
        "accent": _rainbow_color(_RAINBOW_ACCENT_PHASE),
        "btn_bg": _rainbow_color(_RAINBOW_BTN_PHASE),
        "btn_active": _rainbow_color(_RAINBOW_ACTIVE_PHASE),
    }


class RainbowAnimator:
    """Drives a periodic callback so the Rainbow theme cycles through the
    spectrum. Refreshes very fast (120ms) like an RGB mouse LED."""
    def __init__(self, root, callback, interval_ms=120):
        self.root = root
        self.callback = callback
        self.interval = interval_ms
        self.phase = 0.0
        self._id = None
        self._running = False

    def start(self):
        if self._running:
            return
        self._running = True
        self._tick()

    def stop(self):
        self._running = False
        if self._id:
            try:
                self.root.after_cancel(self._id)
            except Exception:
                pass
        self._id = None

    def _tick(self):
        if not self._running:
            return
        self.phase = (self.phase + 0.015) % 1.0
        try:
            self.callback(_rainbow_color(self.phase))
        except Exception:
            pass
        self._id = self.root.after(self.interval, self._tick)


# ---------------------------------------------------------------------------
# Global RGB sync registry
# ---------------------------------------------------------------------------
# Every window that participates in the Rainbow theme registers here so a
# single animator drives ALL of them in lock-step (same phase → same colors).
# Minimize pucks also register so they cycle like an RGB mouse LED.

_RAINBOW_WINDOWS = {}   # id(root) -> root
_RAINBOW_PUCKS = {}     # id(puck) -> puck
_RAINBOW_MASTER = None  # the master RainbowAnimator
_RAINBOW_MASTER_ROOT = None


def register_rainbow_window(root):
    """Register a Toplevel/root so it receives live RGB ticks."""
    if root is None:
        return
    _RAINBOW_WINDOWS[id(root)] = root
    _sync_master_animator(root)


def unregister_rainbow_window(root):
    """Remove a window from the live RGB sync set."""
    if root is None:
        return
    _RAINBOW_WINDOWS.pop(id(root), None)


def register_rainbow_puck(puck):
    """Register a minimize puck so it cycles colors like an RGB LED."""
    if puck is None:
        return
    _RAINBOW_PUCKS[id(puck)] = puck
    # Find any registered window to drive the master animator.
    for root in _RAINBOW_WINDOWS.values():
        _sync_master_animator(root)
        break
    # If no window is registered yet, try the puck's own parent chain.
    if _RAINBOW_MASTER is None:
        try:
            parent = getattr(puck, "master", None)
            while parent is not None:
                if getattr(parent, "_marekfs_theme", {}).get("rainbow"):
                    _sync_master_animator(parent)
                    break
                parent = getattr(parent, "master", None)
        except Exception:
            pass


def unregister_rainbow_puck(puck):
    """Remove a minimize puck from the live RGB sync set."""
    if puck is None:
        return
    _RAINBOW_PUCKS.pop(id(puck), None)


def _sync_master_animator(root):
    """Ensure a master animator exists if any window is on the Rainbow theme."""
    global _RAINBOW_MASTER, _RAINBOW_MASTER_ROOT
    theme = getattr(root, "_marekfs_theme", {}) or {}
    if not theme.get("rainbow"):
        return
    if _RAINBOW_MASTER is not None:
        return
    try:
        _RAINBOW_MASTER_ROOT = root
        _RAINBOW_MASTER = RainbowAnimator(root, _master_tick, interval_ms=120)
        _RAINBOW_MASTER.start()
    except Exception:
        _RAINBOW_MASTER = None


def _master_tick(color):
    """One RGB tick: push the same phase color to every registered window
    and puck. Each element gets a hue offset so the whole UI looks like an
    RGB mouse with multiple zones cycling together."""
    global _RAINBOW_MASTER
    if _RAINBOW_MASTER is None:
        return
    phase = _RAINBOW_MASTER.phase
    for root in list(_RAINBOW_WINDOWS.values()):
        try:
            _apply_full_rainbow_tick(root, phase)
        except Exception:
            pass
    for puck in list(_RAINBOW_PUCKS.values()):
        try:
            _apply_puck_tick(puck, phase)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Per-zone hue offsets (MAD DOG RGB mouse style)
# ---------------------------------------------------------------------------
# Each UI zone gets a fixed hue offset so the whole window shows multiple
# colors at once — like the multi-zone LEDs on an RGB mouse — while all
# zones drift through the spectrum together.

_ZONE_BG = 0.00        # window background
_ZONE_SURFACE = 0.05   # panels / title bar
_ZONE_ENTRY = 0.10     # input fields
_ZONE_ACCENT = 0.15    # accent buttons / selection / headings
_ZONE_BTN = 0.25       # normal buttons
_ZONE_BTN_ACTIVE = 0.35
_ZONE_FG = 0.50        # text (complementary-ish, high contrast)
_ZONE_BTN_FG = 0.55
_ZONE_TROUGH = 0.08    # scrollbar trough / notebook bg


def _zone_color(phase, offset, sat=0.85, val=1.0):
    return _rainbow_color(phase + offset, sat=sat, val=val)


def _zone_dark(phase, offset, sat=0.85, val=0.12):
    """Dark variant of a zone color (for text-on-accent contrast)."""
    return _rainbow_color(phase + offset, sat=sat, val=val)


def _apply_full_rainbow_tick(root, phase):
    """Update EVERY color-bearing style in place for one RGB tick.

    CRITICAL: every s.configure() call must include ALL geometry-affecting
    options (padding, borderwidth, relief, font, rowheight, anchor, etc.)
    exactly as apply_theme() set them. If we pass only color options, Tk
    resets the omitted options to defaults → buttons/entries resize → the
    window visibly "wiggles" on every tick.
    """
    try:
        s = ttk.Style(root)
    except Exception:
        return
    try:
        t = getattr(root, "_marekfs_theme", {}) or {}
        glossy = t.get("glossy", True)
        relief = "raised" if glossy else "flat"
        bw = 2 if glossy else 0
        font = t.get("font", ("Segoe UI", 10))

        # --- Zone colors ---
        bg = _zone_color(phase, _ZONE_BG, sat=0.45, val=0.12)
        surface = _zone_color(phase, _ZONE_SURFACE, sat=0.50, val=0.18)
        entry = _zone_color(phase, _ZONE_ENTRY, sat=0.40, val=0.08)
        accent = _zone_color(phase, _ZONE_ACCENT, sat=0.90, val=1.0)
        btn = _zone_color(phase, _ZONE_BTN, sat=0.65, val=0.32)
        btn_active = _zone_color(phase, _ZONE_BTN_ACTIVE, sat=0.70, val=0.42)
        fg = _zone_color(phase, _ZONE_FG, sat=0.25, val=0.96)
        btn_fg = _zone_color(phase, _ZONE_BTN_FG, sat=0.20, val=0.96)
        trough = _zone_color(phase, _ZONE_TROUGH, sat=0.40, val=0.14)
        on_accent = _zone_dark(phase, _ZONE_ACCENT, sat=0.90, val=0.12)

        # --- Root background ---
        try:
            root.configure(bg=bg)
        except Exception:
            pass

        # --- Frames / labels (include font so it never resets) ---
        s.configure("TFrame", background=bg)
        s.configure("Panel.TFrame", background=surface)
        s.configure("TLabel", background=bg, foreground=fg, font=font)
        s.configure("Title.TLabel", background=bg, foreground=accent, font=("Segoe UI", 16, "bold"))
        s.configure("Status.TLabel", background=surface, foreground=fg, font=font)
        s.configure("TLabelframe", background=bg, foreground=accent, borderwidth=bw, relief=relief)
        s.configure("TLabelframe.Label", background=bg, foreground=accent, font=font)

        # --- Buttons (include padding / border / relief / font / anchor) ---
        s.configure("TButton", background=btn, foreground=btn_fg,
                    borderwidth=0, relief="flat", font=font, padding=(8, 4),
                    anchor="center")
        s.map("TButton",
              background=[("active", btn_active), ("pressed", btn_active)],
              foreground=[("active", btn_fg), ("pressed", btn_fg)])
        s.configure("Accent.TButton", background=accent, foreground=on_accent,
                    borderwidth=0, relief="flat", font=font, padding=(10, 5),
                    anchor="center")
        s.map("Accent.TButton",
              background=[("active", accent), ("pressed", accent)],
              foreground=[("active", on_accent), ("pressed", on_accent)])

        # --- Entries / spinbox / combobox (include borderwidth / relief) ---
        s.configure("TEntry", fieldbackground=entry, foreground=fg,
                    insertcolor=fg, bordercolor=accent, lightcolor=accent,
                    darkcolor=accent, borderwidth=bw, relief=relief)
        s.configure("TSpinbox", fieldbackground=entry, foreground=fg,
                    arrowcolor=accent, borderwidth=bw, relief=relief)
        s.configure("TCombobox", fieldbackground=entry, foreground=fg,
                    background=btn, arrowcolor=accent, borderwidth=bw, relief=relief)
        s.map("TCombobox", fieldbackground=[("readonly", entry)],
              foreground=[("readonly", fg)], background=[("active", btn_active)])

        # --- Check / radio ---
        s.configure("TCheckbutton", background=bg, foreground=fg, focuscolor=accent)
        s.map("TCheckbutton", background=[("active", bg)])
        s.configure("TRadiobutton", background=bg, foreground=fg)
        s.map("TRadiobutton", background=[("active", bg)])

        # --- Treeview (include rowheight / borderwidth / relief) ---
        s.configure("Treeview", background=entry, fieldbackground=entry,
                    foreground=fg, rowheight=22, borderwidth=0, relief="flat")
        s.map("Treeview",
              background=[("selected", accent)],
              foreground=[("selected", on_accent)])
        s.configure("Treeview.Heading", background=surface, foreground=accent,
                    font=("Segoe UI", 10, "bold"), relief="flat")
        s.map("Treeview.Heading", background=[("active", btn_active)])

        # --- Notebook (include padding) ---
        s.configure("TNotebook", background=bg, borderwidth=0)
        s.configure("TNotebook.Tab", background=surface, foreground=fg, padding=(10, 4))
        s.map("TNotebook.Tab",
              background=[("selected", accent)],
              foreground=[("selected", on_accent)])

        # --- Scrollbar / progressbar (include borderwidth / relief) ---
        s.configure("TScrollbar", background=accent, troughcolor=trough,
                    borderwidth=0, relief="flat")
        s.map("TScrollbar", background=[("active", btn_active)])
        s.configure("TProgressbar", background=accent, troughcolor=trough, borderwidth=0)

        # --- Custom chrome (title bar + buttons) ---
        _apply_chrome_tick(root, phase, surface, accent, bg, on_accent, fg)
    except Exception:
        pass


def _apply_chrome_tick(root, phase, surface, accent, bg, on_accent, fg):
    """Update custom title-bar chrome + plain-tk widgets in place."""
    # Store the live RGB colors on the root so hover-leave handlers can
    # restore the CURRENT color (fixes 'uncolored' flash on title buttons).
    try:
        root._marekfs_current_accent = accent
        root._marekfs_current_on_accent = on_accent
    except Exception:
        pass

    # Custom chrome from ui_custom.add_custom_chrome
    chrome = getattr(root, "_marekfs_chrome", None)
    if chrome:
        bar = chrome.get("bar")
        title_label = chrome.get("title_label")
        if bar is not None:
            try: bar.configure(bg=accent)
            except Exception: pass
        if title_label is not None:
            try: title_label.configure(bg=accent, fg=on_accent)
            except Exception: pass
        for key in ("btn_close", "btn_max", "btn_min"):
            btn = chrome.get(key)
            if btn is not None:
                try: btn.configure(bg=accent, fg=on_accent)
                except Exception: pass

    # Main app's manually-built title bar (marekfs.py) and other window attrs
    for attr in ("title_bar", "btn_close", "btn_max", "btn_min"):
        widget = getattr(root, attr, None)
        if widget is None:
            continue
        try:
            if attr == "title_bar":
                widget.configure(bg=accent)
            else:
                widget.configure(bg=accent, fg=on_accent)
        except Exception:
            pass

    # Main title label (has fg too)
    title_label = getattr(root, "title_label", None)
    if title_label is not None:
        try: title_label.configure(bg=accent, fg=on_accent)
        except Exception: pass

    # Wallpaper canvas (plain tk widget with its own bg)
    try:
        wallpaper_canvas = getattr(root, "wallpaper_canvas", None)
        if wallpaper_canvas is not None:
            wallpaper_canvas.configure(bg=bg)
    except Exception:
        pass

    # Search-match tag — cycle so even the matches glow
    try:
        tree = getattr(root, "tree", None)
        if tree is not None:
            match_bg = _zone_color(phase, _ZONE_ACCENT, sat=0.70, val=0.32)
            match_fg = _zone_color(phase, _ZONE_FG, sat=0.20, val=0.98)
            tree.tag_configure("search_match", background=match_bg, foreground=match_fg)
    except Exception:
        pass

    # Context menu (plain tk.Menu may not fully support colors, but try)
    try:
        menu = getattr(root, "context_menu", None)
        if menu is not None:
            menu.configure(bg=surface, fg=accent,
                           activebackground=accent, activeforeground=on_accent)
    except Exception:
        pass


def _apply_puck_tick(puck, phase):
    """Cycle a minimize-puck's colors like an RGB mouse LED.

    The puck gets an accent color cycling through the spectrum while the
    text keeps high contrast. The border also follows with a hue offset.
    """
    try:
        frame = getattr(puck, "frame", None)
        label = getattr(puck, "label", None)
        if frame is None and label is None:
            return
        accent = _zone_color(phase, _ZONE_ACCENT, sat=0.90, val=1.0)
        on_accent = _zone_dark(phase, _ZONE_ACCENT, sat=0.90, val=0.12)
        border = _zone_color(phase, _ZONE_BTN_ACTIVE, sat=0.80, val=1.0)
        # Store the last colors so hover-leave can restore the CURRENT color
        try:
            puck._last_accent = accent
            puck._last_on_accent = on_accent
        except Exception:
            pass
        if frame is not None:
            try:
                frame.configure(bg=accent, highlightbackground=border, highlightcolor=border)
            except Exception:
                pass
        if label is not None:
            try:
                label.configure(bg=accent, fg=on_accent)
            except Exception:
                pass
    except Exception:
        pass


def apply_theme(root, theme_name, override=None):
    t = THEMES.get(theme_name, THEMES["Glossy Dark"])
    # Merge defaults so every key exists
    t = dict(t)
    for k, v in _DEFAULT.items():
        if k not in t:
            t[k] = v
    if override:
        t.update(override)
    glossy = t["glossy"]
    relief = "raised" if glossy else "flat"
    bw = 2 if glossy else 0

    root.configure(bg=t["bg"])
    s = ttk.Style(root)
    try:
        # Only switch ttk engines when needed. Re-calling theme_use() on an
        # engine that is already active forces Tk to tear down and rebuild the
        # layout of every widget — that relayout is what makes the window
        # visibly "wiggle" when a theme is re-applied (e.g. animated rainbow
        # ticks or apply_theme() called from many Toplevels).
        if str(s.theme_use()) != "clam":
            s.theme_use("clam")
    except Exception:
        pass

    s.configure("TFrame", background=t["bg"])
    s.configure("Panel.TFrame", background=t["surface"])
    s.configure("TLabel", background=t["bg"], foreground=t["fg"], font=t["font"])
    s.configure("Title.TLabel", background=t["bg"], foreground=t["accent"], font=("Segoe UI", 16, "bold"))
    s.configure("Status.TLabel", background=t["surface"], foreground=t["fg"], font=t["font"])
    s.configure("TLabelframe", background=t["bg"], foreground=t["accent"], borderwidth=bw, relief=relief)
    s.configure("TLabelframe.Label", background=t["bg"], foreground=t["accent"], font=t["font"])

    # Keep geometry invariant across palettes: color changes must not alter
    # padding, borders, row height, or font metrics.
    s.configure("TButton", background=t["btn_bg"], foreground=t["btn_fg"],
                borderwidth=0, relief="flat", font=t["font"], padding=(8, 4),
                anchor="center")
    s.map("TButton",
          background=[("active", t["btn_active"]), ("pressed", t["btn_active"])],
          foreground=[("active", t["btn_fg"])])

    s.configure("Accent.TButton", background=t["accent"], foreground=t["bg"],
                borderwidth=0, relief="flat", font=t["font"], padding=(10, 5),
                anchor="center")
    s.map("Accent.TButton", background=[("active", t["btn_active"]), ("pressed", t["btn_active"])])

    s.configure("TEntry", fieldbackground=t["entry"], foreground=t["fg"],
                insertcolor=t["fg"], bordercolor=t["accent"], lightcolor=t["accent"],
                darkcolor=t["accent"], borderwidth=bw, relief=relief)
    s.configure("TSpinbox", fieldbackground=t["entry"], foreground=t["fg"],
                arrowcolor=t["accent"], borderwidth=bw, relief=relief)
    s.configure("TCheckbutton", background=t["bg"], foreground=t["fg"], focuscolor=t["accent"])
    s.map("TCheckbutton", background=[("active", t["bg"])])
    s.configure("TRadiobutton", background=t["bg"], foreground=t["fg"])
    s.map("TRadiobutton", background=[("active", t["bg"])])

    s.configure("Treeview", background=t["entry"], fieldbackground=t["entry"],
                foreground=t["fg"], rowheight=22, borderwidth=0, relief="flat")
    s.map("Treeview",
          background=[("selected", t["accent"])],
          foreground=[("selected", t["bg"])])
    s.configure("Treeview.Heading", background=t["surface"], foreground=t["accent"],
                font=("Segoe UI", 10, "bold"), relief="flat")
    s.map("Treeview.Heading", background=[("active", t["btn_active"])])

    s.configure("TCombobox", fieldbackground=t["entry"], foreground=t["fg"],
                background=t["btn_bg"], arrowcolor=t["accent"], borderwidth=bw, relief=relief)
    s.map("TCombobox", fieldbackground=[("readonly", t["entry"])],
          foreground=[("readonly", t["fg"])], background=[("active", t["btn_active"])])

    s.configure("TNotebook", background=t["bg"], borderwidth=0)
    s.configure("TNotebook.Tab", background=t["surface"], foreground=t["fg"], padding=(10, 4))
    s.map("TNotebook.Tab", background=[("selected", t["accent"])], foreground=[("selected", t["bg"])])

    s.configure("TScrollbar", background=t["surface"], troughcolor=t["bg"], borderwidth=0, relief="flat")
    s.map("TScrollbar", background=[("active", t["btn_active"])])

    s.configure("TProgressbar", background=t["accent"], troughcolor=t["surface"], borderwidth=0)

    # Canvas / Text use raw colors — store for widgets that need them.
    root._marekfs_theme = t
    return t


def theme_color(root, key):
    t = getattr(root, "_marekfs_theme", None)
    return t.get(key) if t else None


# ---------------------------------------------------------------------------
# Live RGB animation (MAD DOG RGB mouse style)
# ---------------------------------------------------------------------------
# The Rainbow theme animates by updating ALL color-bearing style options in
# place (backgrounds, surfaces, buttons, text, title-bar chrome, minimize
# pucks) using per-zone hue offsets. No theme_use(), no structural changes →
# the layout stays rock solid while every color cycles through the spectrum.

def start_rainbow_animation(root):
    """Start the live RGB master animator for `root`.

    Only fires when the currently applied theme is the Rainbow theme. The
    animator updates ALL colors in place so the window never wiggles or
    relayouts — it just glows like an RGB mouse.
    """
    theme = getattr(root, "_marekfs_theme", {}) or {}
    if not theme.get("rainbow"):
        stop_rainbow_animation()
        return None

    # Register this root and sync the master animator.
    register_rainbow_window(root)
    _sync_master_animator(root)

    global _RAINBOW_MASTER
    if _RAINBOW_MASTER is not None:
        return _RAINBOW_MASTER

    # Fallback: per-window animator if master creation failed.
    def _fallback_tick(color):
        if _RAINBOW_MASTER is not None:
            _apply_full_rainbow_tick(root, _RAINBOW_MASTER.phase)
        else:
            _apply_full_rainbow_tick(root, 0.0)

    anim = RainbowAnimator(root, _fallback_tick, interval_ms=120)
    root._marekfs_rainbow_anim = anim
    anim.start()
    return anim


def stop_rainbow_animation(root=None):
    """Stop the master RGB animator and detach all registrations.

    `root` is accepted for API compatibility; the master animator and all
    registered windows/pucks are cleaned up together.
    """
    global _RAINBOW_MASTER, _RAINBOW_MASTER_ROOT
    if _RAINBOW_MASTER is not None:
        try:
            _RAINBOW_MASTER.stop()
        except Exception:
            pass
    _RAINBOW_MASTER = None
    _RAINBOW_MASTER_ROOT = None
    _RAINBOW_WINDOWS.clear()
    _RAINBOW_PUCKS.clear()

    # Also stop any legacy per-window animators.
    if root is not None:
        anim = getattr(root, "_marekfs_rainbow_anim", None)
        if anim is not None:
            try:
                anim.stop()
            except Exception:
                pass
        try:
            root._marekfs_rainbow_anim = None
        except Exception:
            pass


def theme_existing_window(win, parent=None, title=None, min_size=None):
    """Convenience: theme a feature window and register it for RGB sync.

    This bridges ui_custom.theme_existing_window so feature windows get the
    same live RGB treatment as the main window.
    """
    name = "Glossy Dark"
    if parent is not None:
        try:
            v = getattr(parent, "theme_var", None)
            if v is not None:
                name = v.get()
        except Exception:
            pass
    try:
        t = apply_theme(win, name)
    except Exception:
        t = getattr(win, "_marekfs_theme", None) or dict(THEMES["Glossy Dark"])
    if t.get("rainbow"):
        register_rainbow_window(win)
        _sync_master_animator(win)
    return win