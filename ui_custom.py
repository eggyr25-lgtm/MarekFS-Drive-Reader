"""MarekFS custom UI utilities: theming helpers, dialogs, and window chrome."""
import tkinter as tk
from tkinter import ttk, messagebox
from marekfs_theme import apply_theme, THEME_NAMES
from marekfs_core import format_bytes, get_drive_size_bytes


def theme_existing_window(win, parent=None, title=None, min_size=None):
    """Apply theme to an existing Toplevel window."""
    try:
        theme_name = "Glossy Dark"
        if parent is not None:
            try:
                theme_var = getattr(parent, "theme_var", None)
                if theme_var is not None:
                    theme_name = theme_var.get()
            except Exception:
                pass
        
        apply_theme(win, theme_name)
        
        if title:
            win.title(title)
        
        if min_size:
            win.minsize(*min_size)
    except Exception:
        pass


def stable_widget_width(widget, chars=20):
    """Return widget configured to a stable character-based width."""
    try:
        widget.configure(width=chars)
    except Exception:
        pass
    return widget


class AddPartitionDialog:
    """Dialog for adding a new partition with size selection.

    A big, maximizable window with a horizontal slider that ranges from 4 KiB
    up to the maximum capacity of the target drive (falls back to 16 TiB when the
    drive size can't be queried), plus an exact byte entry for precise sizes.
    The selected size (in bytes) is returned to the caller via the callback.
    """
    MIN_SIZE_BYTES = 4096  # 4 KiB
    DEFAULT_MAX_BYTES = 16 * (1024 ** 4)  # 16 TiB fallback

    def __init__(self, parent, index, callback, max_size_bytes=None):
        self.win = tk.Toplevel(parent)
        self.win.title(f"Add Partition #{index + 1}")
        self.win.resizable(True, True)
        # Big, maximizable window.
        self.win.geometry("760x440")
        try:
            self.win.state("zoomed")
        except tk.TclError:
            pass
        self.callback = callback

        if max_size_bytes is None:
            max_size_bytes = self._probe_drive_size(parent)
        self.max_size_bytes = max(self.MIN_SIZE_BYTES, int(max_size_bytes or self.DEFAULT_MAX_BYTES))

        theme_existing_window(self.win, parent)

        start_val = min(5 * 1024 ** 3, self.max_size_bytes)
        self._size_var = tk.IntVar(value=start_val)
        self._display_var = tk.StringVar()

        ttk.Label(self.win, text="Partition Size",
                  font=("Segoe UI", 14, "bold")).pack(pady=(22, 4))
        ttk.Label(self.win, text=f"Slide from 4 KiB up to {format_bytes(self.max_size_bytes)}.",
                  font=("Segoe UI", 9)).pack(pady=0)

        self._scale = ttk.Scale(self.win, orient=tk.HORIZONTAL,
                                from_=self.MIN_SIZE_BYTES, to=self.max_size_bytes,
                                variable=self._size_var, command=self._on_scale)
        self._scale.pack(fill=tk.X, padx=30, pady=(18, 8))

        entry_row = ttk.Frame(self.win)
        entry_row.pack(pady=6)
        ttk.Label(entry_row, text="Exact size (bytes):",
                  font=("Segoe UI", 9, "bold")).pack(side=tk.LEFT, padx=(0, 6))
        vcmd = (self.win.register(self._validate_bytes), "%P")
        self._entry_var = tk.StringVar()
        self._entry = ttk.Entry(entry_row, textvariable=self._entry_var, width=18,
                                validate="key", validatecommand=vcmd)
        self._entry.pack(side=tk.LEFT)
        self._entry.bind("<FocusOut>", self._on_entry_focus_out)
        ttk.Label(entry_row, textvariable=self._display_var,
                  font=("Segoe UI", 10, "bold"), foreground="#ffcc00").pack(side=tk.LEFT, padx=12)

        btn_row = ttk.Frame(self.win)
        btn_row.pack(side=tk.BOTTOM, fill=tk.X, pady=(0, 24))
        ttk.Button(btn_row, text="Create", command=self._create,
                   style="Accent.TButton").pack(side=tk.LEFT, padx=8)
        ttk.Button(btn_row, text="Cancel", command=self.win.destroy).pack(side=tk.LEFT, padx=8)

        self._sync_display()
        self._entry_var.set(str(self._size_var.get()))

        self.win.transient(parent)
        self.win.grab_set()

    def _probe_drive_size(self, parent):
        try:
            drive = getattr(parent, "drive_path", None)
            if drive:
                return get_drive_size_bytes(drive)
        except Exception:
            pass
        return None

    def _on_scale(self, *_):
        self._sync_display()
        self._entry_var.set(str(int(self._size_var.get())))

    def _sync_display(self):
        val = int(self._size_var.get())
        self._display_var.set(f"{format_bytes(val)} ({val:,} bytes)")

    def _validate_bytes(self, proposed):
        if proposed == "":
            return True
        try:
            v = int(proposed)
        except ValueError:
            return False
        return self.MIN_SIZE_BYTES <= v <= self.max_size_bytes

    def _on_entry_focus_out(self, *_):
        try:
            v = int(self._entry.get())
        except ValueError:
            v = self._size_var.get()
        v = max(self.MIN_SIZE_BYTES, min(self.max_size_bytes, v))
        self._size_var.set(v)
        self._sync_display()
        self._entry_var.set(str(v))

    def _create(self):
        size = max(self.MIN_SIZE_BYTES, min(self.max_size_bytes, int(self._size_var.get())))
        self.win.destroy()
        if self.callback:
            self.callback(size)



class WallpaperManager:
    """Manages wallpaper images for the application."""
    def __init__(self):
        self.current = None
        self.path = None
    
    def choose(self, parent):
        """Open file dialog to choose wallpaper."""
        from tkinter import filedialog
        path = filedialog.askopenfilename(
            parent=parent,
            title="Choose Wallpaper",
            filetypes=[
                ("Images", "*.png *.jpg *.jpeg *.gif *.bmp"),
                ("Videos", "*.mp4 *.avi *.mkv"),
                ("All", "*.*")
            ]
        )
        if path:
            self.path = path
            self.current = path
            return path
        return None
    
    def describe(self):
        if self.path:
            return f"Wallpaper: {self.path}"
        return "No wallpaper set"


def minimize_to_corner(window, on_restore=None, icon_text="M", theme=None):
    """Minimize window to a small draggable icon in the corner."""
    # Create a small puck window
    puck = tk.Toplevel(window)
    puck.overrideredirect(True)
    puck.attributes("-topmost", True)
    
    # Get theme colors
    bg = "#171c2b"
    fg = "#e8ecf5"
    if theme and isinstance(theme, dict):
        bg = theme.get("surface", bg)
        fg = theme.get("fg", fg)
    
    puck.configure(bg=bg)
    
    # Small draggable icon
    label = tk.Label(puck, text=icon_text, bg=bg, fg=fg, 
                     font=("Segoe UI", 10, "bold"), padx=8, pady=4)
    label.pack()
    
    # Position at bottom-right
    screen_w = window.winfo_screenwidth()
    screen_h = window.winfo_screenheight()
    puck.geometry(f"+{screen_w - 80}+{screen_h - 80}")
    
    # Hide main window
    window.withdraw()
    
    # Dragging
    def start_drag(e):
        puck._drag_x = e.x
        puck._drag_y = e.y
    
    def do_drag(e):
        x = e.x_root - puck._drag_x
        y = e.y_root - puck._drag_y
        puck.geometry(f"+{x}+{y}")
    
    def restore(_):
        if on_restore:
            on_restore()
        window.deiconify()
        puck.destroy()
    
    label.bind("<Button-1>", start_drag)
    label.bind("<B1-Motion>", do_drag)
    label.bind("<Double-Button-1>", restore)
    
    return puck


def privileged_execute_confirm(parent=None):
    """Show a prominent privileged-execute warning. Returns True if user accepts.

    Uses the app theme for styling and displays an explicit warning message.
    """
    try:
        msg = (
            "!!!WARNING!!! THIS DISABLES THE BUILT IN VIRTUAL MACHINE\n\n"
            "You are about to run code in Privileged Execute mode. This WILL allow the script\n"
            "to access the host system outside of the MarekFS virtual disk.\n\n"
            "Proceed only if you understand the risk: the script may modify your Windows\n"
            "installation, delete files, or perform other destructive actions.\n\n"
            "Do you wish to continue?"
        )
        # Use a themed Toplevel to show the warning prominently
        win = tk.Toplevel(parent) if parent is not None else tk.Toplevel()
        theme_existing_window(win, parent)
        win.title("!!! PRIVILEGED EXECUTE WARNING !!!")
        win.geometry("600x260")
        win.resizable(False, False)
        frm = ttk.Frame(win, padding=12)
        frm.pack(fill=tk.BOTH, expand=True)
        lbl = tk.Label(frm, text=msg, justify=tk.LEFT, fg="#ffdddd", bg=win.cget('bg'), font=("Segoe UI", 10, "bold"))
        lbl.pack(fill=tk.BOTH, expand=True)
        btns = ttk.Frame(frm)
        btns.pack(fill=tk.X, pady=(8,0))
        result = {'ok': False}
        def yes():
            result['ok'] = True
            win.destroy()
        def no():
            result['ok'] = False
            win.destroy()
        ttk.Button(btns, text="Proceed (I accept the risk)", style="Accent.TButton", command=yes).pack(side=tk.LEFT, padx=6)
        ttk.Button(btns, text="Cancel", command=no).pack(side=tk.LEFT, padx=6)
        win.transient(parent)
        win.grab_set()
        parent.wait_window(win) if parent is not None else win.wait_window()
        return bool(result['ok'])
    except Exception:
        # Fallback to a simple confirm
        return messagebox.askokcancel("Privileged Execute — Confirm",
                                      "Run in Privileged Execute mode? This may modify your system.")
