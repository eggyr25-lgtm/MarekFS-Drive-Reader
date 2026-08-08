"""MarekFS custom UI utilities: theming helpers, dialogs, and window chrome."""
import tkinter as tk
from tkinter import ttk, messagebox
from marekfs_theme import apply_theme, THEME_NAMES


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
    """Dialog for adding a new partition with size selection."""
    def __init__(self, parent, index, callback):
        self.win = tk.Toplevel(parent)
        self.win.title(f"Add Partition #{index + 1}")
        self.win.geometry("400x200")
        self.win.resizable(False, False)
        self.callback = callback
        
        # Apply theme
        theme_existing_window(self.win, parent)
        
        ttk.Label(self.win, text="Partition Size:", font=("Segoe UI", 10, "bold")).pack(pady=10)
        
        # Size options in GB
        sizes = [
            ("1 GB", 1024 * 1024 * 1024),
            ("2 GB", 2 * 1024 * 1024 * 1024),
            ("5 GB", 5 * 1024 * 1024 * 1024),
            ("10 GB", 10 * 1024 * 1024 * 1024),
            ("20 GB", 20 * 1024 * 1024 * 1024),
            ("50 GB", 50 * 1024 * 1024 * 1024),
            ("100 GB", 100 * 1024 * 1024 * 1024),
        ]
        
        self.size_var = tk.StringVar(value="5 GB")
        size_frame = ttk.Frame(self.win)
        size_frame.pack(pady=10)
        
        for label, _ in sizes:
            ttk.Radiobutton(size_frame, text=label, variable=self.size_var, value=label).pack(anchor=tk.W)
        
        btn_frame = ttk.Frame(self.win)
        btn_frame.pack(pady=15)
        
        ttk.Button(btn_frame, text="Create", command=self._create, style="Accent.TButton").pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text="Cancel", command=self.win.destroy).pack(side=tk.LEFT, padx=5)
        
        self.win.transient(parent)
        self.win.grab_set()
    
    def _create(self):
        sizes = {
            "1 GB": 1024 * 1024 * 1024,
            "2 GB": 2 * 1024 * 1024 * 1024,
            "5 GB": 5 * 1024 * 1024 * 1024,
            "10 GB": 10 * 1024 * 1024 * 1024,
            "20 GB": 20 * 1024 * 1024 * 1024,
            "50 GB": 50 * 1024 * 1024 * 1024,
            "100 GB": 100 * 1024 * 1024 * 1024,
        }
        size = sizes.get(self.size_var.get(), 5 * 1024 * 1024 * 1024)
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
