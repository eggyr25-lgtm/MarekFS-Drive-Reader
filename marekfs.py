"""MarekFS main application: glossy/matte themes, dark/bright + purple/
diamond/gold, disks dashboard, image/movie viewing, and AV virus scanning
that extracts to temp and reports the scanner's own output. Includes
up to 360 partitions with random 5-char ids and a ProgramData config file
that stores the PreferredPartitionID."""
import os
import sys
import time
import tempfile
import shutil
import struct
import threading
import subprocess
import importlib
import traceback
import ctypes
import hashlib
import json
import re
import shlex
import tkinter as tk
from tkinter import ttk, messagebox, simpledialog, filedialog
from tkinter.font import Font

from marekfs_core import (
    SECTOR_SIZE, FILENAME_MAX_LEN, DIRECTORY_ENTRY_SIZE,
    JOURNAL_START_SECTOR, JOURNAL_MAGIC, ARCHIVE_MAGIC,
    MAX_JOURNAL_PAYLOAD_SIZE, JOURNAL_HEADER_SIZE, JOURNAL_SECTORS,
    MAX_FILE_COUNT, DEFAULT_DIR_SECTORS_COUNT,
    DEFAULT_DIR_START_SECTOR, DEFAULT_DATA_AREA_RESERVE, MAX_LOGICAL_FILENAME_CHARS,
    CACHE_MAX_SIZE, CACHE_MAGIC, CACHE_HEADER_SIZE, CACHE_START_SECTOR, CACHE_SECTORS,
    CACHE_DISK_FRACTION, cache_size_for_disk, partition_cache_region, get_drive_size,
    get_drive_size_bytes, is_marekfs_disk,
    LOCKS_DIR, FILE_ATTR_HIDDEN, FILE_ATTR_READONLY, FILE_ATTR_SYSTEM,
    FILE_ATTR_ARCHIVE, FILE_ATTR_COMPRESSED, FILE_ATTR_ENCRYPTED, FILE_ATTR_DIRECTORY,
    IMAGE_EXTS, MOVIE_EXTS,
    MAX_PARTITIONS, PARTITION_ID_LEN, PARTITION_MAGIC, PARTITION_TABLE_SECTORS,
    is_admin, get_attr_string, get_attr_icon, format_bytes, get_available_drives,
    create_marekfs_archive, parse_marekfs_archive, create_marekvid,
    load_file_metadata, save_file_metadata, file_id_for_record, data_checksum,
    load_file_id_database, save_file_id_database,
    prepare_file_payload, read_file_payload,
    open_drive, read_sectors, write_sectors,
    set_journal_status, write_with_journal, recovery_replay_journal,
    acquire_file_lock, release_file_lock, is_file_locked_by_other,
    generate_partition_id, read_partition_table, write_partition_table,
    init_default_partitions,
    load_programdata_config, save_programdata_config,
    get_preferred_partition_id, set_preferred_partition_id,
    PROGRAM_DATA_CONFIG_PATH, ensure_scanner_config, update_scanner_rules,
    check_clamav_update, CLAMAV_CHECK_INTERVAL_SECONDS,
    get_extended_fs_support, set_extended_fs_support, ALL_EXTENDED_FS,
)
from marekfs_core import (
    read_extended_filesystem, list_extended_fs_files, read_extended_fs_file,
)
from marekfs_theme import (
    THEMES, THEME_NAMES, apply_theme, rainbow_static_override,
    start_rainbow_animation, stop_rainbow_animation,
    register_rainbow_window, unregister_rainbow_window,
    register_theme_animation, unregister_theme_animation,
)
from marekfs_views import (
    ModernMarekFSArchiveViewer, ModernMarekFSProperties, ModernMarekFSEditor,
    DiskTestWindow, VisualizeWindow, MediaViewer, VirusScanWindow, ChecksumsWindow,
    ScannerUpdateProgressWindow, ClamAVUpdateSettingsWindow,
)
from marekfs_dashboard import DashboardWindow
from ui_custom import AddPartitionDialog, WallpaperManager, minimize_to_corner, theme_existing_window
from marekfs_core import crypt_partition, is_partition_encrypted
from marekfs_background import AnimatedBackground
from marekfs_media import (
    MusicPlayerWindow, VideoPlayerWindow, ImageViewerWindow, ImageEditorWindow,
    MarekFSVirtualMediaSource, open_media_for, cleanup_temp_files,
    AUDIO_EXTS, VIDEO_EXTS,
)
from marekfs_users_health import DiskUserStore, DiskUsersWindow, DiskHealthWindow, permission_allows, set_file_permission, collect_disk_health

from marekfs_translate import TranslatorWindow
from marekfs_ramcache import RAMCache, CHUNK_SIZE
from marekfs_browser import MarekFSBrowserWindow, DOWNLOAD_STAGING_DIR, DOWNLOAD_IMPORT_STATE_PATH
from marekfs_extensions import ExtensionManagerWindow
from marekfs_scriptr import ScriptrConsoleWindow, ScriptrDriveWriter
from marekfs_sharing import (
    BluetoothShareWindow, WiFiDisksWindow, WiFiDiskServer,
)
from marekfs_camera import CameraWindow
from marekfs_snapshots import SnapshotWindow
from marekfs_diskinfo import get_disk_info, format_disk_info
from marekfs_views import _locate_executable
from ui_custom import privileged_execute_confirm
import atexit


def auto_install_dependencies():
    for pkg in ("cryptography", "Pillow", "pygame", "opencv-python", "deep-translator"):
        try:
            importlib.import_module({"Pillow": "PIL", "opencv-python": "cv2", "deep-translator": "deep_translator"}.get(pkg, pkg))
        except ImportError:
            try:
                subprocess.check_call([sys.executable, "-m", "pip", "install", pkg],
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception:
                pass


auto_install_dependencies()


class ModernMarekFSApp:
    def __init__(self, root):
        self.root = root
        self.root.title("🚀 MAREKFS — Experimental Sector-Aligned Journaled Filesystem")
        self.root.geometry("1400x940")
        self.root.report_callback_exception = self.show_tkinter_error
        
        # Show safety warning on first run
        self.root.bind("<Map>", self._restore_window_decoration)
        self.scanner_config = ensure_scanner_config()

        # Never auto-select a raw physical disk. It must be entered manually
        # and explicitly confirmed by the user before any advanced operation.
        self.drive_path = "\\\\.\\PhysicalDrive1"
        self._physical_drive_confirmed = False
        # Don't auto-show disk info on every startup; opt-in via settings
        self._auto_show_disk_info = False

        self.files_data = []
        self.file_metadata = load_file_metadata(self.drive_path)
        self.disk_users = DiskUserStore(self.drive_path)
        self.current_user = None
        self.file_id_database = load_file_id_database()
        self.file_id_entries = {}
        self.current_folder = ""
        self.search_var = tk.StringVar(value="")
        self.search_var.trace_add("write", lambda *_: self._live_search())
        self.search_matches = set()
        self._search_animating = False
        self._drag_source = None
        self.dir_sectors_count = DEFAULT_DIR_SECTORS_COUNT
        self.next_free_sector = DEFAULT_DIR_START_SECTOR + self.dir_sectors_count
        self.status_var = tk.StringVar(value="Ready")
        self._download_import_state = self._load_download_import_state()
        # Defer starting background threads until the Tk mainloop is running.
        # Scheduling the thread start via `root.after` avoids calling
        # `root.after` from a background thread (which fails when the main
        # loop isn't active yet).
        try:
            self.root.after(100, lambda: threading.Thread(target=self._initialise_scanner_rules, daemon=True).start())
            self.root.after(200, lambda: self._start_clamav_checker())
        except Exception:
            # If scheduling fails (very unusual), fall back to starting them
            # immediately but guarded inside the functions.
            try:
                threading.Thread(target=self._initialise_scanner_rules, daemon=True).start()
            except Exception:
                pass
            try:
                self._start_clamav_checker()
            except Exception:
                pass
        self._clamav_prompt_open = False
        self.show_hidden = tk.BooleanVar(value=True)

        self.cache_blown = True
        self.cache_advanced = tk.BooleanVar(value=False)
        self.held_locks = set()
        self.disk_size_bytes = None  # total byte size of the current drive/image
        self._extended_fs = None  # current extended filesystem parser, if any
        self._extended_fs_name = None  # name of detected extended filesystem

        # --- Partitions ---
        self.partitions = []
        self.active_partition_index = 0
        self.partition_var = tk.StringVar(value="No partitions")

        self.theme_var = tk.StringVar(value="Glossy Dark")
        self.wallpaper = WallpaperManager()
        self.background = None  # AnimatedBackground (image / gif / mp4)
        self._win_maximized = False
        self._win_prev_geom = None

        # --- RAM cache: load the persistent 512 MB-chunk store into RAM
        #     BEFORE the reader initializes / scans the drive. ---
        self.ram_cache = RAMCache(store_dir=os.path.join(
            os.path.dirname(os.path.abspath(self.drive_path)) or ".", ".marekfs_cache"))
        try:
            self._preloaded_entries = self.ram_cache.preload()
        except Exception:
            self._preloaded_entries = 0

        # WiFi Disk server: start after disk is loaded (see scan_drive)
        self._wifi_server = None

        self._setup_custom_window()
        apply_theme(self.root, self.theme_var.get())
        self._apply_theme()
        self.setup_ui()
        self.load_partitions()
        self.scan_drive()
        self._update_ram_cache_status()
        self._schedule_download_import()

        # Ensure any lock files we created are cleaned up when the app exits
        try:
            atexit.register(self._cleanup_locks_on_exit)
        except Exception:
            pass

    def _initialise_scanner_rules(self):
        try:
            results = update_scanner_rules(self.scanner_config)
            updated = sum(1 for item in results if item.get("status") == "updated")
            self.root.after(0, lambda: self.status_var.set(f"Scanner ready · {updated} rule source(s) updated"))
        except Exception as e:
            self.root.after(0, lambda: self.status_var.set(f"Scanner ready · offline rules ({e})"))

    def _cleanup_locks_on_exit(self):
        # Remove any lock files that belong to this process PID, and
        # release any held locks tracked in-memory.
        try:
            pid = os.getpid()
            # release held locks first
            try:
                for full in list(getattr(self, 'held_locks', []) or []):
                    try:
                        self._release_lock(full)
                    except Exception:
                        pass
            except Exception:
                pass
            # Remove any lock files under LOCKS_DIR that contain our PID
            try:
                if os.path.isdir(LOCKS_DIR):
                    for fname in os.listdir(LOCKS_DIR):
                        p = os.path.join(LOCKS_DIR, fname)
                        try:
                            with open(p, 'r') as f:
                                content = f.read().strip()
                            if content and int(content) == pid:
                                try: os.remove(p)
                                except Exception: pass
                        except Exception:
                            try:
                                # if cannot read, attempt to remove as stale
                                os.remove(p)
                            except Exception:
                                pass
            except Exception:
                pass
        except Exception:
            pass

    # --- ClamAV database auto-updater ---------------------------------------
    def _start_clamav_checker(self):
        """Every 4 hours (in a background thread), check whether ClamAV's
        free public mirror has a newer database than the one MarekFS Scanner
        has installed. If so, ask the user before downloading anything."""
        def loop():
            time.sleep(20)  # let the app finish starting up first
            while True:
                try:
                    result = check_clamav_update(self.scanner_config)
                    if result.get("available"):
                        self.root.after(0, lambda r=result: self._prompt_clamav_update(r))
                except Exception:
                    pass
                time.sleep(CLAMAV_CHECK_INTERVAL_SECONDS)
        threading.Thread(target=loop, daemon=True).start()

    def _prompt_clamav_update(self, result):
        if self._clamav_prompt_open:
            return
        self._clamav_prompt_open = True
        try:
            install = messagebox.askyesno(
                "MarekFS Scanner Update",
                "A newer ClamAV database is available.\n\n"
                "Do you want to install new update for MarekFS Scanner?")
            if install:
                def on_done(update_result, error):
                    if error:
                        self.status_var.set(f"Scanner update failed: {error}")
                    else:
                        self.status_var.set(f"Scanner updated · {update_result['hashes']:,} ClamAV hashes")
                ScannerUpdateProgressWindow(self.root, self.scanner_config, on_done=on_done)
        finally:
            self._clamav_prompt_open = False

    def open_scanner_update_settings(self):
        def refreshed(update_result, error):
            if error:
                self.status_var.set(f"Scanner update failed: {error}")
            elif update_result:
                self.status_var.set(f"Scanner updated · {update_result['hashes']:,} ClamAV hashes")
        ClamAVUpdateSettingsWindow(self.root, self.scanner_config, on_check_now=refreshed)

    def _load_download_import_state(self):
        try:
            with open(DOWNLOAD_IMPORT_STATE_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _save_download_import_state(self):
        try:
            os.makedirs(os.path.dirname(DOWNLOAD_IMPORT_STATE_PATH), exist_ok=True)
            tmp = DOWNLOAD_IMPORT_STATE_PATH + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._download_import_state, f, ensure_ascii=False, indent=2)
            os.replace(tmp, DOWNLOAD_IMPORT_STATE_PATH)
        except Exception:
            pass

    def _schedule_download_import(self):
        try:
            self.import_browser_downloads()
        finally:
            self.root.after(1500, self._schedule_download_import)

    def import_browser_downloads(self):
        if not os.path.isdir(DOWNLOAD_STAGING_DIR):
            return
        for name in os.listdir(DOWNLOAD_STAGING_DIR):
            source = os.path.join(DOWNLOAD_STAGING_DIR, name)
            if not os.path.isfile(source) or name.endswith((".part", ".tmp")):
                continue
            try:
                stat = os.stat(source)
                if stat.st_size <= 0 or time.time() - stat.st_mtime < 1.0:
                    continue
                with open(source, "rb") as f:
                    data = f.read()
                digest = hashlib.sha256(data).hexdigest()
                if digest in self._download_import_state:
                    continue
                target_name = name
                index = 1
                while any(item["filename"] == f"Downloads/{target_name}" for item in self.files_data):
                    stem, ext = os.path.splitext(name)
                    target_name = f"{stem} ({index}){ext}"
                    index += 1
                if not any(item["filename"] == "Downloads" and item.get("is_dir") for item in self.files_data):
                    self.create_entry("Downloads", b"", FILE_ATTR_DIRECTORY, is_dir=True)
                self.create_entry(f"Downloads/{target_name}", data, 0)
                self._download_import_state[digest] = {"name": target_name, "size": len(data), "imported": time.time()}
                self._save_download_import_state()
                self.status_var.set(f"Imported browser download to /Downloads/{target_name}")
            except Exception:
                # Leave failed downloads in staging for retry and diagnosis.
                continue

    def show_tkinter_error(self, exc_type, exc_value, exc_tb):
        err_msg = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        messagebox.showerror("Error Encountered", f"An unexpected error occurred:\n\n{err_msg}")

    def _create_tooltip(self, widget, text):
        """Create a simple tooltip for a widget."""
        def show_tooltip(event):
            tooltip = tk.Toplevel(widget)
            tooltip.wm_overrideredirect(True)
            tooltip.wm_geometry(f"+{event.x_root+10}+{event.y_root+10}")
            label = ttk.Label(tooltip, text=text, background="#ffffe0", 
                            relief="solid", borderwidth=1, padding=5)
            label.pack()
            widget._tooltip = tooltip
        
        def hide_tooltip(event):
            if hasattr(widget, '_tooltip'):
                widget._tooltip.destroy()
                del widget._tooltip
        
        widget.bind("<Enter>", show_tooltip)
        widget.bind("<Leave>", hide_tooltip)

    def _setup_custom_window(self):
        """Remove native title bar for a frameless, modern look."""
        self.root.overrideredirect(True)
        self.root.geometry("1400x940+80+40")
        self.root.minsize(900, 500)
        self._drag_x = 0
        self._drag_y = 0

    def _apply_theme(self):
        """Apply the current theme and update the custom title bar.

        The Rainbow theme uses a LIVE animator that cycles the accent color
        in place (no style-tree rebuild, so the window never wiggles).
        """
        name = self.theme_var.get()
        t = apply_theme(self.root, name)
        
        # Stop all theme-specific animations before switching
        unregister_theme_animation(self.root)
        
        if t.get("rainbow"):
            override = rainbow_static_override(t)
            t = apply_theme(self.root, name, override=override)
            accent = override.get("accent", t.get("accent", "#ff0044"))
            try:
                self.title_bar.configure(bg=accent)
                self.title_label.configure(bg=accent, fg=t.get("bg", "#0d0d12"))
                self.btn_min.configure(bg=accent, fg=t.get("bg", "#0d0d12"))
                self.btn_max.configure(bg=accent, fg=t.get("bg", "#0d0d12"))
                self.btn_close.configure(bg=accent, fg=t.get("bg", "#0d0d12"))
            except Exception:
                pass
            # Register the main window for live RGB cycling and start the
            # master animator (MAD DOG RGB mouse style — everything cycles).
            register_rainbow_window(self.root)
            start_rainbow_animation(self.root)
        else:
            # Stop any running rainbow animator when leaving the theme.
            stop_rainbow_animation(self.root)
            unregister_rainbow_window(self.root)
            surface = t.get("surface", "#171c2b")
            fg = t.get("fg", "#e8ecf5")
            try:
                self.title_bar.configure(bg=surface)
                self.title_label.configure(bg=surface, fg=fg)
                self.btn_min.configure(bg=surface, fg=fg)
                self.btn_max.configure(bg=surface, fg=fg)
                self.btn_close.configure(bg=surface, fg=fg)
            except Exception:
                pass
        
        # Register and start theme-specific animations (fizzy, chocolate, lightning, demonic)
        canvas_widget = getattr(self, "wallpaper_canvas", None)
        file_list_widget = getattr(self, "tree", None)
        register_theme_animation(self.root, name, canvas_widget, file_list_widget)
        
        try:
            self.wallpaper_canvas.configure(bg=t.get("bg", "#10131c"))
        except Exception:
            pass
        if hasattr(self, "tree"):
            self.render_tree()

    def _start_win_drag(self, e):
        self._drag_x = e.x
        self._drag_y = e.y

    def _do_win_drag(self, e):
        if self._win_maximized:
            return
        x = self.root.winfo_x() + e.x - self._drag_x
        y = self.root.winfo_y() + e.y - self._drag_y
        self.root.geometry(f"+{x}+{y}")

    def _do_win_min(self):
        # A frameless (override-redirect) window can't be iconified into the
        # taskbar cleanly, so instead we hide it and drop a small, draggable
        # icon in the bottom-right corner. Clicking that icon restores it.
        if getattr(self, "_corner_minimizer", None):
            return
        def _restored():
            self._corner_minimizer = None
        try:
            self._corner_minimizer = minimize_to_corner(
                self.root, on_restore=_restored, icon_text="🚀",
                theme=getattr(self.root, "_marekfs_theme", None))
        except Exception:
            try: self.root.withdraw()
            except Exception: pass

    def _restore_window_decoration(self, _event=None):
        if not getattr(self, "_minimized_restore_decor", False):
            return
        try:
            if str(self.root.state()) == "normal":
                self.root.overrideredirect(True)
                self._minimized_restore_decor = False
                self.root.lift(); self.root.focus_force()
        except tk.TclError:
            pass

    def _do_win_max(self):
        if self._win_maximized:
            self.root.geometry(self._win_prev_geom)
            self._win_maximized = False
        else:
            self._win_prev_geom = self.root.geometry()
            sw = self.root.winfo_screenwidth()
            sh = self.root.winfo_screenheight()
            self.root.geometry(f"{sw-20}x{sh-60}+10+30")
            self._win_maximized = True

    def _do_win_close(self):
        try:
            cleanup_temp_files()
        except Exception:
            pass
        try:
            self.ram_cache.flush()
        except Exception:
            pass
        self.root.destroy()

    # --- RAM cache helpers -------------------------------------------------
    def _cache_key(self, filename):
        return f"{os.path.abspath(self.drive_path)}::{filename}"

    def _update_ram_cache_status(self):
        if not hasattr(self, "ram_cache_var"):
            return
        s = self.ram_cache.stats()
        self.ram_cache_var.set(
            f"{s['in_ram']}/{s['entries']} files in RAM · {format_bytes(s['bytes_in_ram'])} "
            f"of {format_bytes(s['ram_limit'])} · hits {s['hits']} / misses {s['misses']} · "
            f"{format_bytes(CHUNK_SIZE)} chunks")

    def flush_ram_cache(self):
        try:
            self.ram_cache.flush()
            self._update_ram_cache_status()
            messagebox.showinfo("RAM Cache", f"Cache flushed to {format_bytes(CHUNK_SIZE)} chunk files in\n{self.ram_cache.store_dir}")
        except Exception as e:
            messagebox.showerror("RAM Cache", str(e))

    def clear_ram_cache(self):
        if not messagebox.askyesno("RAM Cache", "Clear all cached files from RAM and the chunk store?"):
            return
        self.ram_cache.clear()
        self._update_ram_cache_status()


    def setup_ui(self):
        # --- Custom title bar ---
        t = getattr(self.root, "_marekfs_theme", {})
        surface = t.get("surface", "#171c2b")
        fg = t.get("fg", "#e8ecf5")
        accent = t.get("accent", "#00d2ff")
        self.title_bar = tk.Frame(self.root, bg=surface, height=38)
        self.title_bar.pack(fill=tk.X)
        self.title_label = tk.Label(self.title_bar, text="  🚀 MAREKFS — Experimental Sector-Aligned Journaled Filesystem",
                                    bg=surface, fg=fg, font=("Segoe UI", 10, "bold"))
        self.title_label.pack(side=tk.LEFT, padx=14)
        self.btn_close = tk.Label(self.title_bar, text="  ✕", bg=surface, fg=fg,
                                  font=("Segoe UI", 12), cursor="hand2", padx=10)
        self.btn_close.pack(side=tk.RIGHT, padx=2)
        self.btn_max = tk.Label(self.title_bar, text="  □", bg=surface, fg=fg,
                                font=("Segoe UI", 11), cursor="hand2", padx=10)
        self.btn_max.pack(side=tk.RIGHT, padx=2)
        self.btn_min = tk.Label(self.title_bar, text="  —", bg=surface, fg=fg,
                                font=("Segoe UI", 12), cursor="hand2", padx=10)
        self.btn_min.pack(side=tk.RIGHT, padx=2)

        # Expose the title-bar widgets directly on the root so the live RGB
        # animator (marekfs_theme._apply_chrome_tick) can find and cycle them
        # alongside every other window's chrome — otherwise the main title bar
        # buttons stay stuck at their initial static color.
        self.root.title_bar = self.title_bar
        self.root.title_label = self.title_label
        self.root.btn_close = self.btn_close
        self.root.btn_max = self.btn_max
        self.root.btn_min = self.btn_min

        def _btn_base_color():
            """Return the live RGB accent in Rainbow mode, static surface
            otherwise — so hover-leave restores the CURRENT color, not the
            initial one (fixes the 'uncolored' flash on the buttons)."""
            if (getattr(self.root, "_marekfs_theme", {}) or {}).get("rainbow"):
                return getattr(self.root, "_marekfs_current_accent", accent)
            return surface

        def _btn_base_fg():
            if (getattr(self.root, "_marekfs_theme", {}) or {}).get("rainbow"):
                return getattr(self.root, "_marekfs_current_on_accent", fg)
            return fg

        for w in (self.title_bar, self.title_label):
            w.bind("<Button-1>", lambda e: self._start_win_drag(e))
            w.bind("<B1-Motion>", lambda e: self._do_win_drag(e))
        self.btn_min.bind("<Button-1>", lambda e: self._do_win_min())
        self.btn_max.bind("<Button-1>", lambda e: self._do_win_max())
        self.btn_close.bind("<Button-1>", lambda e: self._do_win_close())
        self.btn_close.bind("<Enter>", lambda e: self.btn_close.config(bg="#e53935", fg="#ffffff"))
        self.btn_close.bind("<Leave>", lambda e: self.btn_close.config(bg=_btn_base_color(), fg=_btn_base_fg()))
        self.btn_max.bind("<Enter>", lambda e: self.btn_max.config(bg=t.get("btn_active", "#2a3556")))
        self.btn_max.bind("<Leave>", lambda e: self.btn_max.config(bg=_btn_base_color()))
        self.btn_min.bind("<Enter>", lambda e: self.btn_min.config(bg=t.get("btn_active", "#2a3556")))
        self.btn_min.bind("<Leave>", lambda e: self.btn_min.config(bg=_btn_base_color()))

        # --- Toolbar row ---
        header = ttk.Frame(self.root, height=50); header.pack(fill=tk.X)
        ttk.Label(header, text="🚀 MAREKFS", style="Title.TLabel").pack(side=tk.LEFT, padx=14, pady=6)

        drive_frame = ttk.Frame(header); drive_frame.pack(side=tk.RIGHT, padx=14, pady=6)
        
        # Drive selector dropdown
        self.drive_combo = ttk.Combobox(drive_frame, state="readonly", width=30)
        self.drive_combo['values'] = [d[0] for d in get_available_drives()]
        if self.drive_combo['values']:
            self.drive_combo.current(0)
        self.drive_combo.pack(side=tk.LEFT, padx=5)
        self.drive_combo.bind("<<ComboboxSelected>>", self._on_drive_selected)
        
        # Manual path entry (for advanced users)
        self.path_var = tk.StringVar(value=self.drive_path)
        path_entry = ttk.Entry(drive_frame, textvariable=self.path_var, width=18)
        path_entry.pack(side=tk.LEFT, padx=5)
        
        # Add tooltip for drive path format
        self._create_tooltip(path_entry, "Type C:\\\\ or D:\\\\ or \\\\.\\PhysicalDrive1")
        
        ttk.Button(drive_frame, text="🔄", command=self.scan_drive, width=3).pack(side=tk.LEFT, padx=2)
        ttk.Button(drive_frame, text="🔍", command=self.salvage_journal_data, width=3).pack(side=tk.LEFT, padx=2)
        ttk.Button(drive_frame, text="🧹", command=self.format_disk, width=3).pack(side=tk.LEFT, padx=2)
        ttk.Button(drive_frame, text="ℹ️", command=self.show_disk_info, width=3).pack(side=tk.LEFT, padx=2)

        theme_frame = ttk.Frame(self.root, padding=4); theme_frame.pack(fill=tk.X, padx=10)
        ttk.Label(theme_frame, text="Theme:").pack(side=tk.LEFT, padx=5)
        cb = ttk.Combobox(theme_frame, textvariable=self.theme_var, values=THEME_NAMES, width=18, state="readonly")
        cb.pack(side=tk.LEFT, padx=4)
        cb.bind("<<ComboboxSelected>>", lambda e: self._apply_theme())
        ttk.Button(theme_frame, text="🗄️ Dashboard", style="Accent.TButton", command=self.open_dashboard).pack(side=tk.LEFT, padx=12)
        ttk.Button(theme_frame, text="🖼️ Set Wallpaper", command=self.set_wallpaper).pack(side=tk.LEFT, padx=4)
        ttk.Button(theme_frame, text="🚫 Clear Wallpaper", command=self.clear_wallpaper).pack(side=tk.LEFT, padx=4)
        ttk.Label(theme_frame, text=f"Max entries: {MAX_FILE_COUNT:,}").pack(side=tk.LEFT, padx=12)

        # --- Partition selector bar ---
        part_frame = ttk.Frame(self.root, padding=4); part_frame.pack(fill=tk.X, padx=10)
        ttk.Label(part_frame, text="Partition:", font=("Segoe UI", 10, "bold")).pack(side=tk.LEFT, padx=5)
        self.part_combo = ttk.Combobox(part_frame, textvariable=self.partition_var, width=22, state="readonly")
        self.part_combo.pack(side=tk.LEFT, padx=4)
        self.part_combo.bind("<<ComboboxSelected>>", self.on_partition_selected)
        ttk.Button(part_frame, text="🔄 Refresh", command=self.refresh_partitions).pack(side=tk.LEFT, padx=4)
        ttk.Button(part_frame, text="➕ Add Partition", command=self.add_partition).pack(side=tk.LEFT, padx=4)
        ttk.Button(part_frame, text="⚙️ Settings", command=self.open_settings).pack(side=tk.LEFT, padx=4)
        ttk.Button(part_frame, text="👥 Disk Users", command=self.open_disk_users).pack(side=tk.LEFT, padx=4)
        ttk.Button(part_frame, text="💽 Disk Health", command=self.open_disk_health).pack(side=tk.LEFT, padx=4)
        ttk.Button(part_frame, text="🧮 Checksums", command=self.open_checksums).pack(side=tk.LEFT, padx=4)
        ttk.Button(part_frame, text="🔐 Encrypt Partition", command=lambda: self.crypt_active_partition(False)).pack(side=tk.LEFT, padx=4)
        ttk.Button(part_frame, text="🔓 Decrypt Partition", command=lambda: self.crypt_active_partition(True)).pack(side=tk.LEFT, padx=4)
        self.part_enc_var = tk.StringVar(value="")
        ttk.Label(part_frame, textvariable=self.part_enc_var, font=("Segoe UI", 10, "bold")).pack(side=tk.LEFT, padx=8)
        ttk.Label(part_frame, text=f"(up to {MAX_PARTITIONS} partitions)").pack(side=tk.LEFT, padx=10)

        path_bar = ttk.Frame(self.root, padding=4); path_bar.pack(fill=tk.X, padx=10)
        ttk.Label(path_bar, text="Location:", font=("Segoe UI", 10, "bold")).pack(side=tk.LEFT, padx=5)
        self.lbl_path = ttk.Label(path_bar, text="/", font=("Segoe UI", 10))
        self.lbl_path.pack(side=tk.LEFT)

        # Background banner (still image, animated GIF or MP4 video)
        self.wallpaper_canvas = tk.Canvas(self.root, height=110, highlightthickness=0)
        self.wallpaper_canvas.pack(fill=tk.X, padx=10, pady=(5, 0))
        self.background = AnimatedBackground(self.wallpaper_canvas)
        self.wallpaper_canvas.bind("<Configure>", lambda e: self._render_wallpaper())
        self.wallpaper_label_var = tk.StringVar(value="No background set — choose an image, GIF or MP4")
        ttk.Label(self.root, textvariable=self.wallpaper_label_var).pack(anchor=tk.W, padx=14, pady=(2, 0))

        main_frame = ttk.Frame(self.root); main_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)
        cols = ("Filename", "Type", "Sector", "Size", "Description", "Attributes", "Encrypted", "Lock")
        ttk.Label(main_frame, text="Search FileID/name:").pack(side=tk.TOP, anchor=tk.W)
        search_row = ttk.Frame(main_frame); search_row.pack(side=tk.TOP, fill=tk.X, pady=(0, 4))
        ttk.Entry(search_row, textvariable=self.search_var, width=34).pack(side=tk.LEFT)
        ttk.Button(search_row, text="🔍 Search", command=self.search_files).pack(side=tk.LEFT, padx=4)
        ttk.Button(search_row, text="Clear", command=lambda: (self.search_var.set(""), self.render_tree())).pack(side=tk.LEFT)
        cols = ("FileID",) + cols
        self.tree = ttk.Treeview(main_frame, columns=cols, show="headings", height=20)
        self.tree.tag_configure("search_match", background="#2a684f", foreground="#f0fff8")
        for c, w in zip(cols, (150, 360, 100, 100, 120, 220, 150, 80, 60)):
            self.tree.heading(c, text=c); self.tree.column(c, width=w)
        self.tree.column("Lock", anchor=tk.CENTER)
        self.tree.heading("Lock", text="🔒")
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        # Clicking the lock cell encrypts / decrypts that single file
        self.tree.bind("<Button-1>", self._on_tree_click)
        self.tree.bind("<ButtonPress-1>", self._begin_tree_drag)
        self.tree.bind("<B1-Motion>", self._update_tree_drag)
        self.tree.bind("<ButtonRelease-1>", self._finish_tree_drag)

        self.context_menu = tk.Menu(self.root, tearoff=0)
        self.context_menu.add_command(label="📝 Open / Explore", command=self.open_file)
        self.context_menu.add_command(label="➕ New File", command=self.new_file)
        self.context_menu.add_command(label="📁 New Folder", command=self.new_folder)
        self.context_menu.add_command(label="📦 New Archive", command=self.new_archive)
        self.context_menu.add_separator()
        self.context_menu.add_command(label="📊 Properties", command=self.show_properties)
        self.context_menu.add_command(label="🛡 Virus Scan", command=self.scan_selected_file)
        self.context_menu.add_separator()
        self.context_menu.add_command(label="🔒 Encrypt / Unlock file", command=self.toggle_file_encryption)
        self.context_menu.add_command(label="🎵 Play in Music Player", command=lambda: self.open_in_media("music"))
        self.context_menu.add_command(label="🎬 Play in Video Player", command=lambda: self.open_in_media("video"))
        self.context_menu.add_command(label="🖼️ Open in Image Viewer", command=lambda: self.open_in_media("image"))
        self.context_menu.add_command(label="✏️ Open in Image Editor", command=lambda: self.open_in_media("edit"))
        self.context_menu.add_command(label="🌍 Translate name & content", command=self.open_translator)
        self.context_menu.add_command(label="🔍 Search by FileID", command=self.search_selected_file_id)
        self.context_menu.add_command(label="📡 Send via Bluetooth", command=self.open_bluetooth_share)
        self.context_menu.add_command(label="✏️ Rename", command=self.rename_selected_file)
        # Execute submenu: run in VM (sandbox) or run in Privileged mode (dangerous)
        exec_menu = tk.Menu(self.context_menu, tearoff=0)
        exec_menu.add_command(label="Run (VM)", command=lambda: self.execute_selected_file(vm=True))
        exec_menu.add_command(label="Privileged Execute", command=lambda: self.execute_selected_file(vm=False))
        self.context_menu.add_cascade(label="⚙️ Execute", menu=exec_menu)
        self.context_menu.add_separator()
        self.context_menu.add_command(label="🗑️ Delete Item", command=self.delete_file)
        self.tree.bind("<Button-3>", self.show_context_menu)

        btn_frame = ttk.Frame(self.root); btn_frame.pack(fill=tk.X, padx=10, pady=5)
        ttk.Button(btn_frame, text="📝 Open", command=self.open_file).pack(side=tk.LEFT, padx=4)
        ttk.Button(btn_frame, text="➕ New File", command=self.new_file).pack(side=tk.LEFT, padx=4)
        ttk.Button(btn_frame, text="📁 New Folder", command=self.new_folder).pack(side=tk.LEFT, padx=4)
        ttk.Button(btn_frame, text="📦 New Archive", command=self.new_archive).pack(side=tk.LEFT, padx=4)
        ttk.Button(btn_frame, text="📊 Properties", command=self.show_properties).pack(side=tk.LEFT, padx=4)
        ttk.Button(btn_frame, text="🛡 Virus Scan", style="Accent.TButton", command=self.scan_selected_file).pack(side=tk.LEFT, padx=4)
        ttk.Button(btn_frame, text="🔒 Encrypt / Unlock", command=self.toggle_file_encryption).pack(side=tk.LEFT, padx=4)
        ttk.Button(btn_frame, text="🌍 Translate", command=self.open_translator).pack(side=tk.LEFT, padx=4)
        ttk.Button(btn_frame, text="🔍 File Search", command=self.search_files).pack(side=tk.LEFT, padx=4)
        ttk.Button(btn_frame, text="🗑️ Delete", command=self.delete_file).pack(side=tk.LEFT, padx=4)
        ttk.Button(btn_frame, text="📡 Bluetooth Share", command=self.open_bluetooth_share).pack(side=tk.LEFT, padx=4)
        ttk.Checkbutton(btn_frame, text="👁️ Show Hidden", variable=self.show_hidden, command=self.render_tree).pack(side=tk.LEFT, padx=15)

        tool_frame = ttk.Frame(self.root); tool_frame.pack(fill=tk.X, padx=10, pady=(0, 5))
        ttk.Button(tool_frame, text="👁️ Visualize Drive Data", command=self.visualize_drive_data).pack(side=tk.LEFT, padx=4)
        ttk.Button(tool_frame, text="⚡ Stress Test", command=self.run_stress_test).pack(side=tk.LEFT, padx=4)
        ttk.Button(tool_frame, text="🧪 Disk Test", command=self.run_disk_test).pack(side=tk.LEFT, padx=4)
        ttk.Button(tool_frame, text="🗑️ Blow Cache", command=self.blow_cache).pack(side=tk.LEFT, padx=4)
        ttk.Button(tool_frame, text="🛡 Scanner Updates", command=self.open_scanner_update_settings).pack(side=tk.LEFT, padx=4)
        ttk.Checkbutton(tool_frame, text="🔧 Cache editing (ADVANCED) — RWX", variable=self.cache_advanced).pack(side=tk.LEFT, padx=15)
        self.cache_status_var = tk.StringVar(value="Cache: BLOWN 🔴")
        ttk.Label(tool_frame, textvariable=self.cache_status_var, font=("Segoe UI", 10, "bold")).pack(side=tk.LEFT, padx=10)

        ram_frame = ttk.Frame(self.root); ram_frame.pack(fill=tk.X, padx=10, pady=(0, 5))
        ttk.Label(ram_frame, text="RAM cache:", font=("Segoe UI", 10, "bold")).pack(side=tk.LEFT, padx=5)
        self.ram_cache_var = tk.StringVar(value="loading…")
        ttk.Label(ram_frame, textvariable=self.ram_cache_var).pack(side=tk.LEFT, padx=6)
        ttk.Button(ram_frame, text="💾 Flush to chunk store", command=self.flush_ram_cache).pack(side=tk.LEFT, padx=4)
        ttk.Button(ram_frame, text="🧹 Clear RAM cache", command=self.clear_ram_cache).pack(side=tk.LEFT, padx=4)

        apps_frame = ttk.Frame(self.root); apps_frame.pack(fill=tk.X, padx=10, pady=(0, 5))
        ttk.Label(apps_frame, text="Built-in apps:", font=("Segoe UI", 10, "bold")).pack(side=tk.LEFT, padx=5)
        ttk.Button(apps_frame, text="🎵 Music Player · MarekFS Drive", command=self.open_music_player).pack(side=tk.LEFT, padx=4)
        ttk.Button(apps_frame, text="🎬 Video Player", command=self.open_video_player).pack(side=tk.LEFT, padx=4)
        ttk.Button(apps_frame, text="🎞️ Build Marekvid", command=self.create_marekvid_from_files).pack(side=tk.LEFT, padx=4)
        ttk.Button(apps_frame, text="🖼️ Image Viewer", command=lambda: self.open_in_media("image")).pack(side=tk.LEFT, padx=4)
        ttk.Button(apps_frame, text="✏️ New / Edit Image", command=self.open_image_editor).pack(side=tk.LEFT, padx=4)
        ttk.Button(apps_frame, text="🌍 Translator", command=self.open_translator).pack(side=tk.LEFT, padx=4)
        ttk.Button(apps_frame, text="🌐 MarekFS Browser", command=lambda: MarekFSBrowserWindow(self.root)).pack(side=tk.LEFT, padx=4)
        ttk.Button(apps_frame, text="🧩 Extensions", command=lambda: ExtensionManagerWindow(self.root)).pack(side=tk.LEFT, padx=4)
        ttk.Button(apps_frame, text="🧠 Scriptr Console", command=self.open_scriptr_console).pack(side=tk.LEFT, padx=4)
        ttk.Button(apps_frame, text="📶 WiFi Disks", command=self.open_wifi_disks).pack(side=tk.LEFT, padx=4)
        ttk.Button(apps_frame, text="📷 Camera", command=self.open_camera).pack(side=tk.LEFT, padx=4)
        ttk.Button(apps_frame, text="📸 Snapshots", command=self.open_snapshots).pack(side=tk.LEFT, padx=4)

        status_frame = ttk.Frame(self.root); status_frame.pack(fill=tk.X, padx=10, pady=5)
        ttk.Label(status_frame, textvariable=self.status_var).pack(side=tk.LEFT)
        self.tree.bind("<Double-1>", lambda e: self.open_file())

    def show_context_menu(self, event):
        item = self.tree.identify_row(event.y)
        if item:
            self.tree.selection_set(item)
            try: self.context_menu.tk_popup(event.x_root, event.y_root)
            finally: self.context_menu.grab_release()

    def execute_selected_file(self, vm=True):
        sel = self.tree.selection()
        if not sel: return
        values = self.tree.item(sel[0])['values']
        rel_name = values[1] if len(values) > 1 else values[0]
        full_path = f"{self.current_folder}/{rel_name}" if self.current_folder else rel_name
        record = next((r for r in self.files_data if r.get('filename') == full_path), None)
        if not record:
            messagebox.showerror("Execute Failed", "Selected file not found.")
            return
        if record.get('is_dir'):
            messagebox.showinfo("Execute", "Cannot execute a directory.")
            return
        # Privileged execute confirmation
        if not vm:
            ok = privileged_execute_confirm(self.root)
            if not ok:
                return

        # Export MarekFS into a sandbox root
        tmpdir = tempfile.mkdtemp(prefix="marekfs_exec_")
        export_root = os.path.join(tmpdir, "marekfs")
        try:
            os.makedirs(export_root, exist_ok=True)
            # export all files so relative includes work
            for rec in getattr(self, 'files_data', []) or []:
                fname = rec.get('filename')
                if not fname: continue
                outpath = os.path.join(export_root, *fname.split('/'))
                d = os.path.dirname(outpath)
                if d and not os.path.exists(d):
                    os.makedirs(d, exist_ok=True)
                if not rec.get('is_dir'):
                    try:
                        data = self._read_file_bytes(rec, "")
                        with open(outpath, 'wb') as of:
                            of.write(data or b"")
                    except Exception:
                        pass

            # Write the selected record to ensure it's present
            sel_path = os.path.join(export_root, *full_path.split('/'))
            sel_dir = os.path.dirname(sel_path)
            if sel_dir and not os.path.exists(sel_dir):
                os.makedirs(sel_dir, exist_ok=True)
            try:
                with open(sel_path, 'wb') as sf:
                    sf.write(self._read_file_bytes(record, "") or b"")
            except Exception:
                pass

            env = os.environ.copy()
            env['MAREKFS_ROOT'] = export_root

            # Decide how to run based on extension
            ext = os.path.splitext(record['filename'])[1].lower()
            # Executable directly
            if ext == '.exe':
                exe_path = sel_path
                cwd = export_root if vm else None
                subprocess.run([exe_path], cwd=cwd, env=env)
            elif ext in ('.c', '.cpp', '.cc'):
                # compile into sandbox and run
                src = sel_path
                exe = os.path.join(export_root, 'prog.exe')
                if ext == '.c':
                    comp_list = [("gcc", ["gcc", src, "-o", exe]), ("cc", ["cc", src, "-o", exe]), ("clang", ["clang", src, "-o", exe]), ("tcc", ["tcc", "-o", exe, src])]
                else:
                    comp_list = [("g++", ["g++", src, "-o", exe]), ("clang++", ["clang++", src, "-o", exe])]
                found = False
                for name, cmd in comp_list:
                    path = _locate_executable(cmd[0])
                    if path:
                        cmd0 = list(cmd)
                        cmd0[0] = path
                        subprocess.run(cmd0, cwd=export_root, env=env)
                        found = True
                        break
                if not found:
                    messagebox.showerror("Compile Failed", "No compiler found for source file.")
                else:
                    if os.path.exists(exe):
                        subprocess.run([exe], cwd=export_root if vm else None, env=env)
            elif ext == '.py':
                # run with python, using preamble when vm=True
                script = sel_path
                if vm:
                    # prepend a small preamble file that maps open -> MAREKFS_ROOT
                    runner = os.path.join(tmpdir, 'run_python_vm.py')
                    preamble = (
                        'import os, builtins\n'
                        'MAREKFS_ROOT = os.environ.get("MAREKFS_ROOT")\n'
                        'def _translate(p):\n'
                        '    if p is None: return p\n'
                        '    p = str(p)\n'
                        '    if os.path.isabs(p) or (len(p)>1 and p[1:2]==":"):\n'
                        '        rel = p.replace(":","" ).lstrip('"\\/"')\n'
                        '        return os.path.join(MAREKFS_ROOT, *rel.split("\\\\" if "\\\\" in rel else "/"))\n'
                        '    return os.path.join(MAREKFS_ROOT, p)\n'
                        'import builtins as _b\n'
                        '_orig_open = _b.open\n'
                        'def open(path, mode="r", *a, **kw):\n'
                        '    return _orig_open(_translate(path), mode, *a, **kw)\n'
                        '_b.open = open\n'
                        f'exec(compile(open(r"{script}").read(), r"{script}", "exec"), globals())\n'
                    )
                    try:
                        with open(runner, 'w', encoding='utf-8') as rf:
                            rf.write(preamble)
                        subprocess.run([sys.executable, runner], cwd=export_root, env=env)
                    except Exception as e:
                        messagebox.showerror("Run Failed", str(e))
                else:
                    subprocess.run([sys.executable, script], env=env)
            else:
                messagebox.showinfo("Execute", f"No execution handler for '{ext}' files.")

        finally:
            try:
                shutil.rmtree(tmpdir)
            except Exception:
                pass

    def rename_selected_file(self):
        sel = self.tree.selection()
        if not sel:
            return
        values = self.tree.item(sel[0])['values']
        rel_name = values[1] if len(values) > 1 else values[0]
        full_path = f"{self.current_folder}/{rel_name}" if self.current_folder else rel_name
        # Ask for new name (allow path components)
        new_name = simpledialog.askstring("Rename", f"Enter new name for '{rel_name}':", initialvalue=rel_name, parent=self.root)
        if not new_name:
            return
        # If user provided a relative path, resolve against current folder
        if '/' in new_name or '\\' in new_name:
            # normalize separators to forward slash
            new_full = new_name.replace('\\', '/').lstrip('/')
        else:
            new_full = f"{self.current_folder}/{new_name}" if self.current_folder else new_name
        # Ensure not colliding
        if any(i['filename'] == new_full for i in self.files_data):
            messagebox.showwarning("Rename", f"'{new_full}' already exists.")
            return
        # Call existing rename_entry
        self.rename_entry(full_path, new_full)

    # --- Partitions ------------------------------------------------------
    def load_partitions(self):
        if not self._ensure_drive_access(False):
            return
        try:

            fd = open_drive(self.drive_path, read_write=False)
            try:
                self.partitions, active_idx = read_partition_table(fd)
            finally:
                os.close(fd)
        except Exception:
            self.partitions, active_idx = [], 0

        if not self.partitions:
            self.active_partition_index = 0
            self.partition_var.set("No partitions")
            self._refresh_part_combo()
            return

        # Honor PreferredPartitionID from ProgramData config
        preferred = get_preferred_partition_id()
        match_idx = next((i for i, p in enumerate(self.partitions) if p["id"] == preferred), None)
        if match_idx is not None:
            self.active_partition_index = match_idx
        else:
            self.active_partition_index = min(active_idx, len(self.partitions) - 1)

        self._refresh_part_combo()

    def _refresh_part_combo(self):
        labels = []
        for i, p in enumerate(self.partitions):
            tag = " ★" if i == self.active_partition_index else ""
            labels.append(f"{p['id']}  ({format_bytes(p['size_bytes'])}){tag}")
        self.part_combo['values'] = labels
        if self.partitions:
            p = self.partitions[self.active_partition_index]
            self.partition_var.set(f"{p['id']}  ({format_bytes(p['size_bytes'])}) ★")
        else:
            self.partition_var.set("No partitions")
        self._update_part_enc_label()

    def on_partition_selected(self, event=None):
        idx = self.part_combo.current()
        if idx < 0 or idx >= len(self.partitions): return
        self.active_partition_index = idx
        # Persist selection as the preferred partition
        pid = self.partitions[idx]["id"]
        set_preferred_partition_id(pid)
        self._refresh_part_combo()
        self.scan_drive()

    def refresh_partitions(self):
        """Re-read the partition table from disk and re-apply the preferred
        partition from the ProgramData config, then rescan files."""
        self.load_partitions()
        self.scan_drive()
        self.status_var.set(f"🔄 Refreshed partitions: {len(self.partitions)} found. Preferred: {get_preferred_partition_id() or 'none'}")

    def add_partition(self):
        if len(self.partitions) >= MAX_PARTITIONS:
            messagebox.showwarning("Limit Reached", f"Maximum of {MAX_PARTITIONS} partitions reached.")
            return
        AddPartitionDialog(self.root, len(self.partitions), self._create_partition_with_size)

    def _create_partition_with_size(self, size_bytes):
        """Called by AddPartitionDialog with the user-chosen size in bytes."""
        if self.partitions:
            last = max(self.partitions, key=lambda p: p["start_sector"] + p["size_bytes"] // SECTOR_SIZE)
            start = last["start_sector"] + last["size_bytes"] // SECTOR_SIZE
        else:
            start = PARTITION_TABLE_SECTORS
        new_part = {
            "id": generate_partition_id(),
            "flags": 0,
            "start_sector": start,
            "size_bytes": size_bytes,
        }
        try:
            fd = open_drive(self.drive_path, read_write=True)
            try:
                self.partitions.append(new_part)
                write_partition_table(fd, self.partitions, self.active_partition_index)
                os.fsync(fd)
            finally:
                os.close(fd)
            self._refresh_part_combo()
            messagebox.showinfo("Partition Added", f"Created partition '{new_part['id']}' ({format_bytes(new_part['size_bytes'])}).")
        except Exception as e:
            messagebox.showerror("Add Partition Failed", str(e))

    def open_scriptr_console(self):
        def reader(record):
            try: return self._read_file_bytes(record, "")
            except Exception: return b""
        ScriptrConsoleWindow(self.root, self.drive_path, self.files_data,
                             reader=reader, writer=ScriptrDriveWriter(self))

    # ── WiFi Disk ────────────────────────────────────────────

    def _restart_wifi_server(self):
        """Stop any existing WiFi Disk server and start a fresh one for
        the currently loaded disk. Called every time scan_drive finishes."""
        if self._wifi_server:
            try:
                self._wifi_server.stop()
            except Exception:
                pass
        disk_name = os.path.basename(self.drive_path) or "MarekFSDisk"
        self._wifi_server = WiFiDiskServer(
            disk_name=disk_name,
            get_entries_fn=lambda: list(self.files_data),
            read_file_fn=self._wifi_read_file,
        )
        self._wifi_server.start()

    def _wifi_read_file(self, name):
        """Return raw bytes for *name* from the loaded disk (used by WiFiDiskServer)."""
        record = next((f for f in self.files_data if f.get("filename") == name), None)
        if record is None:
            return b""
        try:
            return self._read_file_bytes(record, "")
        except Exception:
            return b""

    def open_wifi_disks(self):
        WiFiDisksWindow(self.root, app=self)

    # ── Camera ──────────────────────────────────────────

    def open_camera(self):
        """Open the Camera window. Auto-creates Photos/ folder if absent."""
        # Ensure Photos/ directory exists on the disk
        has_photos = any(
            f.get("filename") == "Photos" and
            (f.get("is_dir") or f.get("attributes", 0) & FILE_ATTR_DIRECTORY)
            for f in self.files_data
        )
        if not has_photos:
            self.create_entry("Photos", b"", FILE_ATTR_DIRECTORY, is_dir=True)

        def save_photo(fname, raw_bytes):
            """Write a captured photo into the MarekFS disk."""
            # Avoid duplicate filenames
            base = os.path.basename(fname)
            stem, ext = os.path.splitext(base)
            target = fname
            counter = 1
            while any(f.get("filename") == target for f in self.files_data):
                target = f"Photos/{stem}_{counter}{ext}"
                counter += 1
            self.create_entry(target, raw_bytes, 0)

        def open_viewer(name, raw_bytes):
            from marekfs_media import ImageViewerWindow
            ImageViewerWindow(self.root, name, data=raw_bytes)

        CameraWindow(
            self.root,
            save_photo_fn=save_photo,
            open_viewer_fn=open_viewer,
        )

    def open_snapshots(self):
        SnapshotWindow(self)

    # ── Bluetooth Share ──────────────────────────────

    def open_bluetooth_share(self):
        """Open Bluetooth Share, pre-loading the file selected in the tree."""
        filename, file_data = "", b""
        try:
            sel = self.tree.selection()
            if sel:
                rel_name, record, raw = self._selected_file_record()
                if record and not record.get("is_dir") and raw:
                    filename  = os.path.basename(record["filename"])
                    file_data = raw
        except Exception:
            pass
        BluetoothShareWindow(self.root, filename=filename, file_data=file_data)

    def open_disk_users(self):
        DiskUsersWindow(self.root, self.disk_users, on_login=self._set_current_user)

    def _set_current_user(self, username):
        self.current_user = username
        self.status_var.set(f"Logged in as disk user: {username}")
        self.render_tree()

    def open_disk_health(self):
        DiskHealthWindow(self.root, self.drive_path)

    def open_checksums(self):
        ChecksumsWindow(self.root, self)

    def open_settings(self):
        SettingsWindow(self.root, self)

    # --- Cache helpers ---------------------------------------------------
    def _update_cache_status(self):
        if self.cache_blown: self.cache_status_var.set("Cache: BLOWN 🔴")
        elif self.cache_advanced.get(): self.cache_status_var.set("Cache: RWX (ADVANCED) 🟢")
        else: self.cache_status_var.set("Cache: read-only 🟡")

    def _active_cache_region(self):
        """Return (cache_start_sector, cache_sectors, cache_max_size) for the
        currently active partition, so the cache is partition-specific and sized
        to up to 25% of the disk. Falls back to the legacy global layout when
        there is no active partition (e.g. an unpartitioned disk)."""
        if self.partitions and 0 <= self.active_partition_index < len(self.partitions):
            total = self.disk_size_bytes or 0
            return partition_cache_region(self.partitions[self.active_partition_index], total)
        return CACHE_START_SECTOR, CACHE_SECTORS, CACHE_MAX_SIZE

    def blow_cache(self):
        if not messagebox.askyesno("Blow Cache", "Simulate power loss to the cache?\nThe cache will be marked BLOWN. Raw bytes remain until overwritten."):
            return
        try:
            cache_start, _, _ = self._active_cache_region()
            fd = open_drive(self.drive_path, read_write=True)
            try:
                hdr = CACHE_MAGIC + (0).to_bytes(8, "little") + b"\x00"
                write_sectors(fd, cache_start, hdr.ljust(SECTOR_SIZE, b"\x00"))
                os.fsync(fd)
            finally:
                os.close(fd)
            self.cache_blown = True
            self._update_cache_status()
            messagebox.showinfo("Cache Blown", "Cache is now BLOWN. Use 'Visualize Drive Data' to inspect the raw remains.")
        except Exception as e:
            messagebox.showerror("Blow Failed", str(e))

    def cache_write(self, data: bytes):
        if not self.cache_advanced.get():
            raise PermissionError("Cache is read-only for normal users. Enable 'Cache editing (ADVANCED)' for RWX.")
        cache_start, _, cache_max = self._active_cache_region()
        if len(data) > cache_max:
            raise ValueError(f"Cache cap is {format_bytes(cache_max)}; payload is {format_bytes(len(data))}.")
        fd = open_drive(self.drive_path, read_write=True)
        try:
            write_sectors(fd, cache_start + 1, data)
            hdr = CACHE_MAGIC + len(data).to_bytes(8, "little") + b"\x01"
            write_sectors(fd, cache_start, hdr.ljust(SECTOR_SIZE, b"\x00"))
            os.fsync(fd)
        finally:
            os.close(fd)
        self.cache_blown = False
        self._update_cache_status()

    def cache_read(self):
        if self.cache_blown: return None
        cache_start, _, cache_max = self._active_cache_region()
        fd = open_drive(self.drive_path, read_write=False)
        try:
            hdr = read_sectors(fd, cache_start, 1)
            if not hdr.startswith(CACHE_MAGIC):
                self.cache_blown = True; self._update_cache_status(); return None
            size = int.from_bytes(hdr[len(CACHE_MAGIC):len(CACHE_MAGIC)+8], "little")
            valid = hdr[len(CACHE_MAGIC)+8]
            if not valid or size <= 0 or size > cache_max:
                self.cache_blown = True; self._update_cache_status(); return None
            sectors = (size + SECTOR_SIZE - 1) // SECTOR_SIZE
            return read_sectors(fd, cache_start + 1, sectors)[:size]
        finally:
            os.close(fd)

    # --- Visualize / dashboard ------------------------------------------
    def visualize_drive_data(self):
        VisualizeWindow(self.root, self.drive_path, self.cache_blown, self.dir_sectors_count)

    def open_dashboard(self):
        DashboardWindow(self.root)

    # --- Stress test ----------------------------------------------------
    def run_stress_test(self):
        n = simpledialog.askinteger("Stress Test", "Number of files to create/read/delete:", initialvalue=200, minvalue=1, maxvalue=5000)
        if not n: return
        win = tk.Toplevel(self.root)
        win.title("⚡ Stress Test"); win.geometry("520x420"); win.resizable(True, True)
        out = tk.Text(win, font=Font(family="Consolas", size=10), bg="#0d1117", fg="#c9d1d9")
        out.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)
        out.insert(tk.END, f"Running stress test with {n} files...\n")
        win.update()

        def work():
            t0 = time.time()
            created, read_ok, deleted, errors = 0, 0, 0, 0
            err = None
            try:
                fd = open_drive(self.drive_path, read_write=True)
                try:
                    for i in range(n):
                        fname = f"_stress_{i}.tmp"
                        try:
                            payload, attrs = prepare_file_payload(f"stress payload {i}".encode(), "", 0)
                            sec = self.next_free_sector
                            self.next_free_sector += max(1, len(payload)//SECTOR_SIZE)
                            write_with_journal(fd, sec, payload)
                            self.files_data.append({"filename": fname, "is_dir": False, "sector": sec, "size": len(payload), "attributes": attrs, "encrypted": False})
                            created += 1
                        except Exception:
                            errors += 1
                    self.update_directory(fd); os.fsync(fd)
                    for f in self.files_data:
                        if not f["filename"].startswith("_stress_"): continue
                        try:
                            data = read_sectors(fd, f["sector"], max(1, (f["size"]+SECTOR_SIZE-1)//SECTOR_SIZE))
                            if data: read_ok += 1
                        except Exception:
                            errors += 1
                    self.files_data = [f for f in self.files_data if not f["filename"].startswith("_stress_")]
                    self.update_directory(fd); os.fsync(fd)
                    deleted = n
                finally:
                    os.close(fd)
            except Exception as e:
                errors += 1; err = str(e)
            elapsed = time.time() - t0
            def show():
                out.insert(tk.END, "\n--- Results ---\n")
                out.insert(tk.END, f"Files created : {created}\n")
                out.insert(tk.END, f"Files read    : {read_ok}\n")
                out.insert(tk.END, f"Files deleted : {deleted}\n")
                out.insert(tk.END, f"Errors        : {errors}\n")
                out.insert(tk.END, f"Elapsed       : {elapsed:.2f}s\n")
                if created and elapsed:
                    out.insert(tk.END, f"Throughput    : {created/elapsed:.1f} files/s\n")
                if err:
                    out.insert(tk.END, f"\nFatal: {err}\n")
            self.root.after(0, show)
            self.root.after(0, self.scan_drive)
        threading.Thread(target=work, daemon=True).start()

    # --- Disk Info --------------------------------------------------------
    def show_disk_info(self):
        """Show disk information: type, sub-type, speed, and NVMe slot info."""
        drive_path = self.path_var.get().strip()
        if not drive_path:
            messagebox.showinfo("Disk Info", "Select a disk first.")
            return

        win = tk.Toplevel(self.root)
        # Apply the app's custom theme to this dialog
        try:
            theme_existing_window(win, parent=self.root, title="💽 Disk Info", min_size=(520, 420))
        except Exception:
            try:
                win.title("💽 Disk Info")
                win.geometry("520x420")
                win.resizable(True, True)
                win.transient(self.root)
            except Exception:
                pass

        txt = tk.Text(win, font=("Consolas", 10), bg="#0d1117", fg="#c9d1d9",
                       wrap=tk.WORD, padx=10, pady=10)
        txt.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)
        txt.insert(tk.END, "Scanning disk... please wait.\n")
        txt.config(state=tk.DISABLED)
        win.update()

        def work():
            try:
                info = get_disk_info(drive_path)
                text = format_disk_info(info)
            except Exception as e:
                text = "Error: {}".format(e)
            # Append the last DiskTest summary if available on the app
            try:
                summary = getattr(self, 'last_disk_test_summary', None)
                if summary:
                    text = text + "\n\nDiskTest Summary:\n" + summary
            except Exception:
                pass

            def show():
                txt.config(state=tk.NORMAL)
                txt.delete("1.0", tk.END)
                txt.insert(tk.END, text)
                txt.config(state=tk.DISABLED)
            self.root.after(0, show)

        threading.Thread(target=work, daemon=True).start()

    # --- DiskTest -------------------------------------------------------
    def run_disk_test(self):
        # Pass a callback so the DiskTestWindow returns a summary when finished.
        def _disk_test_done(summary_text):
            try:
                # Store summary for later display in Disk Info
                self.last_disk_test_summary = summary_text
                # Update status briefly
                self.status_var.set(f"DiskTest complete · {summary_text}")
            except Exception:
                pass

        win = DiskTestWindow(self.root, on_finished=_disk_test_done)
        duration = win.duration_min.get() * 60
        chunk_size = 32 * 1024 * 1024
        block = bytes((i * 2654435761) & 0xff for i in range(256)) * (chunk_size // 256)
        test_data = block
        stop_evt = threading.Event()

        def work():
            t_start = time.time()
            try:
                fd = open_drive(self.drive_path, read_write=True)
            except Exception as e:
                self.root.after(0, lambda: messagebox.showerror("DiskTest", f"Cannot open drive: {e}"))
                return
            try:
                cache_start, _, cache_max = self._active_cache_region()
                write_target = cache_start + 1
                buf = test_data[:min(chunk_size, cache_max)]
                while not stop_evt.is_set() and (time.time() - t_start) < duration:
                    w0 = time.time()
                    try:
                        write_sectors(fd, write_target, buf)
                    except Exception:
                        break
                    w1 = time.time(); dt = w1 - w0
                    speed = (len(buf) / (1024*1024)) / dt if dt > 0 else 0
                    elapsed = w1 - t_start
                    self.root.after(0, lambda e=elapsed, s=speed: win.add_sample(e, s))
            finally:
                try: os.close(fd)
                except Exception: pass
                self.root.after(0, win.finished)

        def stop_and_evt():
            stop_evt.set(); win.stop()
        win.btn_stop.config(command=stop_and_evt)
        orig_close = win._on_close
        def close_and_evt():
            stop_evt.set(); orig_close()
        win._on_close = close_and_evt
        threading.Thread(target=work, daemon=True).start()

    # --- Disk operations ------------------------------------------------
    def format_disk(self):
        if not messagebox.askyesno("Confirm Format", f"Format '{self.drive_path}'?\nAll data will be lost."):
            return
        try:
            fd = open_drive(self.drive_path, read_write=True)
            try:
                disk_size = get_drive_size_bytes(self.drive_path)
                self.disk_size_bytes = disk_size
                # Single default partition sized to the MAXIMUM for this disk
                # (not a fixed 1 GB reserve), so both the journal and this
                # partition's cache have room to work.
                self.partitions = init_default_partitions(1, disk_size)
                write_partition_table(fd, self.partitions, 0)
                empty_dir = b"\x00" * (self.dir_sectors_count * SECTOR_SIZE)
                write_sectors(fd, DEFAULT_DIR_START_SECTOR, empty_dir)
                # Partition-specific cache header at the high end of the partition.
                cache_start, _, _ = partition_cache_region(self.partitions[0], disk_size)
                hdr = CACHE_MAGIC + (0).to_bytes(8, "little") + b"\x00"
                write_sectors(fd, cache_start, hdr.ljust(SECTOR_SIZE, b"\x00"))
            finally:
                os.close(fd)
            self.cache_blown = True; self._update_cache_status()
            self.active_partition_index = 0
            self._refresh_part_combo()
            self.scan_drive()
            messagebox.showinfo("Formatted", "Disk formatted with 1 default partition.")
        except Exception as e:
            messagebox.showerror("Format Error", str(e))

    def salvage_journal_data(self):
        salvaged_count = 0
        try:
            fd = open_drive(self.drive_path, read_write=True)
            try:
                header_sectors = (JOURNAL_HEADER_SIZE + SECTOR_SIZE - 1) // SECTOR_SIZE
                header_raw = read_sectors(fd, JOURNAL_START_SECTOR, header_sectors)
                if header_raw.startswith(JOURNAL_MAGIC):
                    idx = len(JOURNAL_MAGIC)
                    t_sec = int.from_bytes(header_raw[idx:idx+8], "little")
                    d_len = int.from_bytes(header_raw[idx+8:idx+12], "little")
                    if 0 < d_len <= MAX_JOURNAL_PAYLOAD_SIZE:
                        total_needed = JOURNAL_HEADER_SIZE + d_len
                        total_sectors = (total_needed + SECTOR_SIZE - 1) // SECTOR_SIZE
                        full_raw = read_sectors(fd, JOURNAL_START_SECTOR, total_sectors)
                        payload = full_raw[JOURNAL_HEADER_SIZE:JOURNAL_HEADER_SIZE + d_len]
                        target_sec = t_sec if t_sec >= DEFAULT_DIR_START_SECTOR else self.next_free_sector
                        
                        # Detect file type from content to preserve original extension
                        rec_fname = self._detect_salvaged_filename(payload, target_sec)
                        
                        already_present = any(f["sector"] == target_sec or f["filename"] == rec_fname for f in self.files_data)
                        if not already_present:
                            padded = payload.ljust(((len(payload) + SECTOR_SIZE - 1) // SECTOR_SIZE) * SECTOR_SIZE, b"\x00")
                            write_sectors(fd, target_sec, padded)
                            is_archive = rec_fname.endswith(".MAREKARCHV")
                            self.files_data.append({"filename": rec_fname, "is_dir": False, "sector": target_sec,
                                                    "size": len(payload), "attributes": FILE_ATTR_ARCHIVE if is_archive else 0, "encrypted": False})
                            salvaged_count += 1
                            sectors_needed = max(1, len(padded) // SECTOR_SIZE)
                            self.next_free_sector = max(self.next_free_sector, target_sec + sectors_needed)
                if salvaged_count > 0:
                    self.update_directory(fd); os.fsync(fd)
            finally:
                os.close(fd)
            if salvaged_count > 0:
                self.scan_drive()
                messagebox.showinfo("Salvage Complete", f"Successfully salvaged {salvaged_count} file(s) from the journal.")
            else:
                messagebox.showinfo("Salvage Result", "No recoverable file payload found in the journal region.")
        except Exception as e:
            messagebox.showerror("Salvage Error", str(e))
    
    def _detect_salvaged_filename(self, payload, target_sec):
        """Detect file type from payload content and generate appropriate filename."""
        # Check for MarekFS archive magic
        if payload.startswith(ARCHIVE_MAGIC):
            return f"salvaged_archive_{target_sec}.MAREKARCHV"
        
        # Try to detect file type from magic bytes
        ext = ".bin"  # default
        if len(payload) >= 4:
            # PNG
            if payload[:4] == b"\x89PNG":
                ext = ".png"
            # JPEG
            elif payload[:3] == b"\xff\xd8\xff":
                ext = ".jpg"
            # GIF
            elif payload[:6] in (b"GIF87a", b"GIF89a"):
                ext = ".gif"
            # BMP
            elif payload[:2] == b"BM":
                ext = ".bmp"
            # PDF
            elif payload[:4] == b"%PDF":
                ext = ".pdf"
            # ZIP (could be docx, xlsx, etc.)
            elif payload[:4] == b"PK\x03\x04":
                ext = ".zip"
            # RAR
            elif payload[:3] == b"Rar!":
                ext = ".rar"
            # 7z
            elif payload[:6] == b"7z\xbc\xaf\x27\x1c":
                ext = ".7z"
            # MP4/MOV
            elif payload[:4] in (b"\x00\x00\x00\x18ftypmp42", b"\x00\x00\x00\x18ftypisom", b"\x00\x00\x00\x18ftypM4V"):
                ext = ".mp4"
            # AVI
            elif payload[:4] == b"RIFF" and payload[8:12] == b"AVI ":
                ext = ".avi"
            # MKV
            elif payload[:4] == b"\x1a\x45\xdf\xa3":
                ext = ".mkv"
            # MP3
            elif payload[:3] == b"ID3" or (len(payload) >= 2 and payload[:2] == b"\xff\xfb"):
                ext = ".mp3"
            # WAV
            elif payload[:4] == b"RIFF" and payload[8:12] == b"WAVE":
                ext = ".wav"
            # OGG
            elif payload[:4] == b"OggS":
                ext = ".ogg"
            # FLAC
            elif payload[:4] == b"fLaC":
                ext = ".flac"
            # Text files - check if mostly printable ASCII
            elif len(payload) > 20:
                printable = sum(1 for b in payload[:min(512, len(payload))] if 32 <= b < 127 or b in (9, 10, 13))
                if printable / min(512, len(payload)) > 0.9:
                    ext = ".txt"
            # EXE/DLL
            elif payload[:2] == b"MZ":
                ext = ".exe"
            # ELF (Linux executables)
            elif payload[:4] == b"\x7fELF":
                ext = ".elf"
        
        return f"salvaged_file_{target_sec}{ext}"

    def _on_drive_selected(self, event=None):
        """Handle drive selection from dropdown."""
        selected_display = self.drive_combo.get()
        if not selected_display:
            return
        
        # Find the corresponding path
        drives = get_available_drives()
        for display, path in drives:
            if display == selected_display:
                self.path_var.set(path)
                self.drive_path = path
                self._physical_drive_confirmed = False
                self.scan_drive()
                break

    def _ensure_drive_access(self, write=False):
        if self.drive_path.lower().startswith("\\\\.\\") and not self._physical_drive_confirmed:
            self.status_var.set("PhysicalDrive1 is selected but locked until you confirm raw-device access.")
            answer = messagebox.askyesno("Confirm raw PhysicalDrive access", "This can read or modify a real disk. Continue?", parent=self.root)
            if not answer:
                return False
            self._physical_drive_confirmed = True
        return True

    def _detect_external_fs_name(self, boot_sector):
        """
        Detect filesystem type from boot sector.
        Returns filesystem name or None.
        """
        try:
            if len(boot_sector) < 90:
                return None
            # NTFS
            if boot_sector[3:11] == b"NTFS    ":
                return "NTFS"
            # FAT32
            if boot_sector[82:90] == b"FAT32   ":
                return "FAT32"
            # FAT16
            if boot_sector[82:90] == b"FAT16   ":
                return "FAT16"
            # exFAT
            if boot_sector[3:6] == b"EXF":
                return "exFAT"
            # EXT2/3/4
            import struct
            if len(boot_sector) >= 0x43A:
                magic = struct.unpack("<H", boot_sector[0x438:0x43A])[0]
                if magic == 0xEF53:
                    # Check features to distinguish EXT version
                    if len(boot_sector) >= 0x46C:
                        ext4_feat = struct.unpack("<I", boot_sector[0x464:0x468])[0]
                        if ext4_feat & 0x40:  # EXT4 feature
                            return "EXT4"
                    return "EXT3"
            # HFS/HFS+
            if boot_sector[0:2] in (b"H+", b"HX"):
                return "HFS+"
            if boot_sector[0:2] == b"BD":
                return "HFS"
            # APFS
            if boot_sector[0:4] == b"NXSB":
                return "APFS"
            # BTRFS
            if len(boot_sector) >= 0x68:
                if boot_sector[0x60:0x68] == b"_BHRfS_M":
                    return "BTRFS"
            return None
        except Exception:
            return None

    def _browse_extended_dir(self, fs_name, directory_path="/"):
        """
        Browse a directory in an extended filesystem.
        """
        try:
            files, error = list_extended_fs_files(self.drive_path, fs_name, directory_path)
            if error:
                messagebox.showerror("Extended FS Error", error)
                return []
            return files
        except Exception as e:
            messagebox.showerror("Extended FS Error", str(e))
            return []

    def scan_drive(self):
        new_drive_path = self.path_var.get().strip()
        if not new_drive_path: return
        if new_drive_path != self.drive_path:
            self._physical_drive_confirmed = False
            self.disk_users = DiskUserStore(new_drive_path)
            self.current_user = None
        self.drive_path = new_drive_path
        if not self._ensure_drive_access(False): return
        self.file_metadata = load_file_metadata(self.drive_path)
        self.files_data.clear()
        self.file_id_database = load_file_id_database()
        try:
            fd = open_drive(self.drive_path, read_write=True)
            self.disk_size_bytes = get_drive_size_bytes(self.drive_path)
            marekfs = is_marekfs_disk(fd)
            try:
                if marekfs:
                    recovery_replay_journal(fd)
                    os.lseek(fd, DEFAULT_DIR_START_SECTOR * SECTOR_SIZE, os.SEEK_SET)
                    dir_data = os.read(fd, self.dir_sectors_count * SECTOR_SIZE)
                else:
                    dir_data = None
            finally:
                os.close(fd)

            if marekfs:
                # Existing MarekFS logic
                max_sec = DEFAULT_DIR_START_SECTOR + self.dir_sectors_count
                total_slots = len(dir_data) // DIRECTORY_ENTRY_SIZE
                for i in range(total_slots):
                    off = i * DIRECTORY_ENTRY_SIZE
                    if off + DIRECTORY_ENTRY_SIZE > len(dir_data): break
                    entry = dir_data[off:off+DIRECTORY_ENTRY_SIZE]
                    name_len = struct.unpack("<H", entry[0:2])[0]
                    if name_len == 0 or name_len > FILENAME_MAX_LEN: continue
                    fname = entry[2:2+name_len].decode("utf-8", errors="ignore")
                    if not fname: continue
                    meta_offset = 2 + FILENAME_MAX_LEN
                    is_dir = entry[meta_offset]
                    sector = int.from_bytes(entry[meta_offset+1:meta_offset+9], "little")
                    size = int.from_bytes(entry[meta_offset+9:meta_offset+17], "little")
                    attributes = entry[meta_offset+17] if len(entry) > meta_offset+17 else 0
                    if is_dir: attributes |= FILE_ATTR_DIRECTORY
                    is_encrypted = bool(attributes & FILE_ATTR_ENCRYPTED)
                    if sector > 0:
                        prior = next((value for value in self.file_id_database.values() if value.get("name") == fname or int(value.get("sector", -1)) == sector), None)
                        file_id = int(prior.get("file_id")) if prior and 0 <= int(prior.get("file_id")) <= ((1 << 64) - 1) else sector
                        logical_name = prior.get("name", fname) if prior and len(str(prior.get("name", fname))) <= MAX_LOGICAL_FILENAME_CHARS else fname
                        record = {"filename": logical_name, "is_dir": bool(is_dir), "sector": sector,
                                  "file_id": file_id, "size": size, "attributes": attributes,
                                  "attributes_text": get_attr_string(attributes), "encrypted": is_encrypted}
                        self.files_data.append(record)
                        self.file_id_database[str(file_id)] = {"file_id": file_id, "name": logical_name, "sector": sector, "updated": time.time()}
                        logical_size = size + (44 if attributes & FILE_ATTR_ENCRYPTED else 0)
                        sectors_used = max(1, (logical_size + SECTOR_SIZE - 1) // SECTOR_SIZE)
                        max_sec = max(max_sec, sector + sectors_used)
                self.next_free_sector = max_sec + 1
                save_file_id_database(self.file_id_database)
                self._update_cache_status()
                self.render_tree()
                self._restart_wifi_server()
                if getattr(self, '_auto_show_disk_info', False):
                    self.root.after(200, self.show_disk_info)
            else:
                # Try extended filesystem
                boot_sector = read_sectors(self.drive_path, 0, 1)
                fs_name = self._detect_external_fs_name(boot_sector)
                if fs_name and fs_name in get_extended_fs_support():
                    self._extended_fs_name = fs_name
                    parser, error = read_extended_filesystem(self.drive_path, fs_name)
                    if parser and not error:
                        self._extended_fs = parser
                        files, error = list_extended_fs_files(self.drive_path, fs_name, "/")
                        if error:
                            messagebox.showwarning("Extended FS Error", error)
                        else:
                            self.files_data = []
                            for f in files:
                                self.files_data.append({
                                    "filename": f.get("path", "/" + f.get("name", "")),
                                    "is_dir": f.get("file_type") == "directory",
                                    "sector": 0,
                                    "file_id": 0,
                                    "size": f.get("size", 0),
                                    "attributes": 0,
                                    "attributes_text": "Extended FS",
                                    "encrypted": False,
                                })
                            self._update_cache_status()
                            self.render_tree()
                            self.status_var.set(f"Disk: '{self.drive_path}' | Extended FS: {fs_name} | Items: {len(files)}")
                            if getattr(self, '_auto_show_disk_info', False):
                                self.root.after(200, self.show_disk_info)
                    else:
                        messagebox.showwarning("Extended FS Error", error or f"Could not mount {fs_name}")
                else:
                    detected = fs_name if fs_name else "unknown"
                    messagebox.showwarning("Not MarekFS", f"'{self.drive_path}' is not a MarekFS disk.\nDetected: {detected}")
        except PermissionError as pe:
            messagebox.showwarning("Permission Error", str(pe))
            self.path_var.set("marekfs_disk.img")
            self.scan_drive()
        except Exception as e:
            self.status_var.set(f"❌ Scan error: {str(e)}")

    def render_tree(self):
        for i in self.tree.get_children(): self.tree.delete(i)
        self.lbl_path.config(text=f"/{self.current_folder}")
        if self.current_folder:
            self.tree.insert("", tk.END, values=("-", "..", "📁 Parent Folder", "-", "-", "", "Directory", "No", ""))
        visible_count = 0
        for f in self.files_data:
            fname = f["filename"]
            if self.current_folder:
                if not fname.startswith(self.current_folder + "/"): continue
                rel_name = fname[len(self.current_folder)+1:]
            else:
                rel_name = fname
            if "/" in rel_name:
                subfolder_name = rel_name.split("/")[0]
                if any(len(self.tree.item(item)['values']) > 1 and self.tree.item(item)['values'][1] == subfolder_name for item in self.tree.get_children()):
                    continue
                folder_record = next((item for item in self.files_data if item["filename"] == self._full_path(subfolder_name)), None)
                if folder_record and not permission_allows(self.file_metadata.get(folder_record["filename"], {}), self.current_user):
                    continue
                self.tree.insert("", tk.END, values=(str(file_id_for_record(folder_record)) if folder_record else "-", subfolder_name, "📁", "-", "-", self.file_metadata.get(self._full_path(subfolder_name), {}).get("description", ""), "Folder", "No", ""))
                visible_count += 1
                continue
            if (f["attributes"] & FILE_ATTR_HIDDEN) and not self.show_hidden.get(): continue
            full_path = f["filename"]
            if not permission_allows(self.file_metadata.get(full_path, {}), self.current_user):
                continue
            tags = ("search_match",) if full_path in self.search_matches else ()
            self.tree.insert("", tk.END, values=(
                str(file_id_for_record(f)), rel_name, get_attr_icon(f["attributes"], rel_name), f["sector"],
                format_bytes(f["size"]), self.file_metadata.get(full_path, {}).get("description", ""),
                get_attr_string(f["attributes"]),
                "Yes" if f["encrypted"] else "No",
                "🔒" if f["encrypted"] else "🔓"), tags=tags)

            visible_count += 1
        part_info = ""
        if self.partitions:
            p = self.partitions[self.active_partition_index]
            part_info = f" | Partition: {p['id']}"
        self.status_var.set(f"Disk: '{self.drive_path}'{part_info} | Items: {visible_count} | Next Sector: {self.next_free_sector} | Max: {MAX_FILE_COUNT:,}")

    def _file_search_match(self, record, query):
        """Match FileID, extension, size, description, prefix bytes and attributes.

        Examples: `ext:.mp3`, `size:>1mb`, `desc:music`, `first:a`,
        `bytes:89504e47`, `attr:hidden`, or plain text/FileID.
        """
        if not query: return True
        metadata = self.file_metadata.get(record.get("filename", ""), {})
        name = str(record.get("filename", "")); lower_name = name.lower()
        fid = str(file_id_for_record(record))
        attrs = get_attr_string(record.get("attributes", 0)).lower()
        try:
            raw = (self._read_file_bytes(record, "") if not record.get("is_dir") and permission_allows(metadata, self.current_user) else b"")
        except Exception:
            raw = b""
        for token in shlex.split(query):
            low = token.lower()
            if low.startswith("ext:") and not lower_name.endswith(low[4:]): return False
            if low.startswith("desc:") and low[5:] not in str(metadata.get("description", "")).lower(): return False
            if low.startswith("attr:") and low[5:] not in attrs: return False
            if low.startswith("first:") and not os.path.basename(name).lower().startswith(low[6:]): return False
            if low.startswith("bytes:"):
                try:
                    wanted = bytes.fromhex(low[6:].replace("0x", ""))
                except ValueError:
                    return False
                if not raw.startswith(wanted): return False
            if low.startswith("size:"):
                expr = low[5:]; m = re.match(r"([<>]=?)([0-9.]+)(kb|mb|gb|b)?$", expr)
                if not m: return False
                factor = {"b": 1, "kb": 1024, "mb": 1024**2, "gb": 1024**3, None: 1}[m.group(3)]
                target = float(m.group(2)) * factor; op = m.group(1); size = record.get("size", 0)
                if not {"<": size < target, "<=": size <= target, ">": size > target, ">=": size >= target}.get(op, False): return False
            elif not any(low in value for value in (fid.lower(), lower_name, attrs, str(metadata.get("description", "")).lower())):
                return False
        return True

    def _live_search(self):
        query = self.search_var.get().strip()
        self.search_matches = {r["filename"] for r in self.files_data if self._file_search_match(r, query)} if query else set()
        self.render_tree()
        if query and self.search_matches:
            self._focus_search_match()
        self._animate_search_matches()

    def _focus_search_match(self):
        target = next(iter(self.search_matches), None)
        if not target: return
        folder = target.rsplit("/", 1)[0] if "/" in target else ""
        if folder != self.current_folder:
            self.current_folder = folder
            self.render_tree()
        for item in self.tree.get_children():
            values = self.tree.item(item).get("values", [])
            if len(values) > 1 and self._full_path(values[1]) == target:
                self.tree.selection_set(item); self.tree.focus(item); self.tree.see(item); break

    def _animate_search_matches(self):
        if self._search_animating or not self.search_matches: return
        self._search_animating = True
        def pulse(step=0):
            try:
                self.tree.tag_configure("search_match", background=("#2a684f" if step % 2 == 0 else "#5b3470"), foreground="#f0fff8")
                if self.search_matches: self.root.after(420, lambda: pulse(step + 1))
                else: self._search_animating = False
            except Exception: self._search_animating = False
        pulse()

    def search_files(self):
        self._live_search()

    def search_selected_file_id(self):
        rel_name, _full_path, record = self._selected_record(need_file=False)
        if record:
            self.search_var.set(str(file_id_for_record(record)))
            self.search_files()

    def _full_path(self, rel_name):
        return f"{self.current_folder}/{rel_name}" if self.current_folder else rel_name

    def _begin_tree_drag(self, event):
        row = self.tree.identify_row(event.y)
        if row:
            values = self.tree.item(row).get("values", [])
            self._drag_source = values[1] if len(values) > 1 else None

    def _update_tree_drag(self, _event):
        # Kept intentionally lightweight; the target is resolved on release.
        return None

    def _finish_tree_drag(self, event):
        source = self._drag_source
        self._drag_source = None
        target_row = self.tree.identify_row(event.y)
        if not source or not target_row or source == "..":
            return
        target_values = self.tree.item(target_row).get("values", [])
        target = target_values[1] if len(target_values) > 1 else None
        if not target or target in (source, ".."):
            return
        old_path = self._full_path(source)
        target_path = self._full_path(target)
        source_record = next((item for item in self.files_data if item["filename"] == old_path), None)
        target_record = next((item for item in self.files_data if item["filename"] == target_path), None)
        if not source_record:
            return
        if target_record and target_path.upper().endswith(".MAREKARCHV"):
            try:
                archive_bytes = self._read_file_bytes(target_record, "")
                archive = parse_marekfs_archive(archive_bytes) or {}
                archive[os.path.basename(old_path)] = self._read_file_bytes(source_record, "")
                self._write_file_bytes(target_record, create_marekfs_archive(archive))
                self.delete_file_by_path(old_path)
                self.status_var.set(f"Added '{source}' to archive '{target}'.")
            except Exception as e:
                messagebox.showerror("Archive drop failed", str(e))
            return
        target_values = self.tree.item(target_row).get("values", [])
        if len(target_values) > 2 and str(target_values[2]).startswith("📁"):
            new_path = self._full_path(f"{target}/{source}")
            self.rename_entry(old_path, new_path)
            self.status_var.set(f"Moved '{source}' to '{target}'.")

    def update_directory(self, fd):
        if len(self.files_data) > MAX_FILE_COUNT:
            raise ValueError(f"Cannot exceed the {MAX_FILE_COUNT}-file directory capacity.")
        buffer = bytearray(self.dir_sectors_count * SECTOR_SIZE)
        for i, f in enumerate(self.files_data):
            off = i * DIRECTORY_ENTRY_SIZE
            fname_encoded = f["filename"].encode("utf-8")[:FILENAME_MAX_LEN]
            name_len = len(fname_encoded)
            fname_padded = fname_encoded.ljust(FILENAME_MAX_LEN, b"\x00")
            is_dir_val = 1 if (f.get("is_dir") or (f.get("attributes", 0) & FILE_ATTR_DIRECTORY)) else 0
            entry = (struct.pack("<H", name_len) + fname_padded + struct.pack("B", is_dir_val)
                     + int(f["sector"]).to_bytes(8, "little") + int(f["size"]).to_bytes(8, "little")
                     + struct.pack("B", int(f.get("attributes", 0))))
            buffer[off:off+DIRECTORY_ENTRY_SIZE] = entry.ljust(DIRECTORY_ENTRY_SIZE, b"\x00")
        write_with_journal(fd, DEFAULT_DIR_START_SECTOR, bytes(buffer))
        try: os.fsync(fd)
        except Exception: pass

    def new_file(self):
        fname = simpledialog.askstring("New File", "📝 Enter filename:")
        if not fname: return
        if len(fname) > MAX_LOGICAL_FILENAME_CHARS:
            messagebox.showerror("Filename too long", f"Use {MAX_LOGICAL_FILENAME_CHARS} characters or fewer.")
            return
        full_path = f"{self.current_folder}/{fname}" if self.current_folder else fname
        self.create_entry(full_path, b"", 0)

    def new_folder(self):
        foldername = simpledialog.askstring("New Folder", "📁 Enter folder name:")
        if not foldername: return
        full_path = f"{self.current_folder}/{foldername}" if self.current_folder else foldername
        self.create_entry(full_path, b"", FILE_ATTR_DIRECTORY, is_dir=True)

    def new_archive(self):
        archivename = simpledialog.askstring("New Archive", "📦 Enter archive name (.MAREKARCHV):")
        if not archivename: return
        if not archivename.endswith(".MAREKARCHV"): archivename += ".MAREKARCHV"
        full_path = f"{self.current_folder}/{archivename}" if self.current_folder else archivename
        self.create_entry(full_path, create_marekfs_archive({}), FILE_ATTR_ARCHIVE)

    def create_entry(self, fname, initial_data, attributes=0, is_dir=False):
        if len(fname) > MAX_LOGICAL_FILENAME_CHARS:
            messagebox.showerror("Filename too long", f"MarekFS filenames can be up to {MAX_LOGICAL_FILENAME_CHARS} characters.")
            return
        if len(self.files_data) >= MAX_FILE_COUNT:
            messagebox.showerror("Limit Reached", f"Maximum of {MAX_FILE_COUNT:,} files reached!")
            return
        padded_payload, updated_attrs = prepare_file_payload(initial_data, "", attributes)
        target_sector = self.next_free_sector
        new_file_record = {"filename": fname, "is_dir": is_dir, "sector": target_sector,
                           "size": len(initial_data), "attributes": updated_attrs, "encrypted": False}
        self.files_data.append(new_file_record)
        sectors_used = max(1, (len(padded_payload) + SECTOR_SIZE - 1) // SECTOR_SIZE)
        self.next_free_sector += sectors_used
        try:
            fd = open_drive(self.drive_path, read_write=True)
            try:
                write_with_journal(fd, target_sector, padded_payload)
                self.update_directory(fd); os.fsync(fd)
            finally:
                os.close(fd)
            if initial_data:
                metadata = self.file_metadata.setdefault(fname, {})
                metadata["checksum"] = data_checksum(initial_data)
                save_file_metadata(self.drive_path, self.file_metadata)
            self.scan_drive()
        except Exception as e:
            self.files_data.remove(new_file_record)
            messagebox.showerror("Create Failed", str(e))

    def _claim_lock(self, full_path):
        if full_path in self.held_locks:
            messagebox.showwarning("File Locked", f"'{full_path}' is already open for editing here. Close that editor first.")
            return False
        if is_file_locked_by_other(full_path):
            messagebox.showwarning("Access Denied — File Locked",
                f"Another app holds the write handle for '{full_path}'.\nIt will be writable only when that app closes the file.\n\n(Concurrent writes are denied to prevent corruption.)")
            return False
        if not acquire_file_lock(full_path):
            messagebox.showwarning("Access Denied — File Locked", f"Could not lock '{full_path}'.")
            return False
        self.held_locks.add(full_path)
        return True

    def _release_lock(self, full_path):
        self.held_locks.discard(full_path)
        release_file_lock(full_path)

    def _read_file_bytes(self, f, password):
        """Serve from RAM cache when possible; otherwise read the drive and cache it.

        When a stored checksum exists, the raw bytes are verified against it
        so silent corruption (bit rot) is detected on every read.
        """
        key = self._cache_key(f["filename"])
        cached = self.ram_cache.get(key)
        if cached is not None and len(cached) == f["size"]:
            self._update_ram_cache_status()
            return cached

        if getattr(self, "_extended_fs", None) and self._extended_fs_name:
            data, error = read_extended_fs_file(self.drive_path, f["filename"], self._extended_fs_name, size=f["size"])
            if error:
                raise OSError(error)
            return data

        fd = open_drive(self.drive_path, read_write=False)
        try:
            num_sectors = max(1, (f["size"] + SECTOR_SIZE - 1) // SECTOR_SIZE)
            data = read_sectors(fd, f["sector"], num_sectors)
        finally:
            os.close(fd)
        raw = read_file_payload(data, password, f["attributes"], f["size"])
        stored = self.file_metadata.get(f["filename"], {}).get("checksum")
        if stored:
            actual = data_checksum(raw)
            if actual != stored:
                raise OSError(f"Checksum mismatch for '{f['filename']}': stored {stored[:12]}…, found {actual[:12]}… (data may be corrupted)")
        # Encrypted files stay RAM-only — never mirrored to the on-disk chunk store.
        self.ram_cache.put(key, raw, persist=not bool(f.get("encrypted")))
        self._update_ram_cache_status()
        return raw

    def scan_selected_file(self):
        sel = self.tree.selection()
        if not sel: return
        values = self.tree.item(sel[0])['values']
        rel_name = values[1] if len(values) > 1 else values[0]
        if rel_name == "..": return
        full_path = f"{self.current_folder}/{rel_name}" if self.current_folder else rel_name
        f = next((item for item in self.files_data if item["filename"] == full_path), None)
        if not f or f.get("is_dir") or (f["attributes"] & FILE_ATTR_DIRECTORY):
            messagebox.showinfo("Virus Scan", "Select a file (not a folder) to scan.")
            return
        password = ""
        if f.get("encrypted", False):
            password = simpledialog.askstring("Password Required", "🔒 Enter password:", show="*")
            if password is None: return
        try:
            raw = self._read_file_bytes(f, password)
        except Exception as e:
            messagebox.showerror("Open Error", str(e)); return
        VirusScanWindow(self.root, rel_name, raw)

    def open_file(self):
        sel = self.tree.selection()
        if not sel: return
        values = self.tree.item(sel[0])['values']
        rel_name = values[1] if len(values) > 1 else values[0]
        if rel_name == "..":
            if "/" in self.current_folder:
                parent_folder = "/".join(self.current_folder.split("/")[:-1])
            else:
                parent_folder = ""
            self.current_folder = parent_folder
            self.render_tree()
            return
        full_path = f"{self.current_folder}/{rel_name}" if self.current_folder else rel_name
        f = next((item for item in self.files_data if item["filename"] == full_path), None)
        if f is None or f.get("is_dir") or (f["attributes"] & FILE_ATTR_DIRECTORY):
            self.current_folder = full_path
            self.render_tree()
            return
        password = ""
        if f.get("encrypted", False):
            password = simpledialog.askstring("Password Required", "🔒 Enter password:", show="*")
            if password is None: return
        try:
            raw_content = self._read_file_bytes(f, password)
        except Exception as e:
            messagebox.showerror("Open Error", str(e)); return

        ext = os.path.splitext(f["filename"])[1].lower()
        is_media = ext in IMAGE_EXTS or ext in MOVIE_EXTS or ext in {".marekvid", ".marekaudio"}

        if is_media or ext in AUDIO_EXTS:
            save_cb = (lambda new_bytes, rec=f, pwd=password: self._write_file_bytes(rec, new_bytes, pwd if rec.get("encrypted") else ""))
            if open_media_for(self.root, f["filename"], raw_content, save_callback=save_cb):
                return
            MediaViewer(self.root, f["filename"], raw_content)
            return

        if not self._claim_lock(full_path):
            return
        release_cb = (lambda fp: (lambda: self._release_lock(fp)))(full_path)

        def save_payload_to_disk(new_bytes, new_attributes, was_encrypted):
            if is_file_locked_by_other(full_path):
                messagebox.showerror("Save Denied — File Locked",
                    f"Another app now holds the write handle for '{full_path}'.\nSave aborted to prevent corruption.")
                return
            exact_size = len(new_bytes)
            padded_payload, final_attrs = prepare_file_payload(new_bytes, password if was_encrypted else "", new_attributes)
            try:
                wfd = open_drive(self.drive_path, read_write=True)
                try:
                    target_file = next((item for item in self.files_data if item["filename"] == full_path), None)
                    if not target_file:
                        raise RuntimeError(f"File '{full_path}' missing from disk index.")
                    old_logical_size = target_file["size"] + (44 if (target_file.get("encrypted", False) or (target_file.get("attributes", 0) & FILE_ATTR_ENCRYPTED)) else 0)
                    old_sectors = max(1, (old_logical_size + SECTOR_SIZE - 1) // SECTOR_SIZE)
                    new_sectors = max(1, len(padded_payload) // SECTOR_SIZE)
                    if new_sectors > old_sectors:
                        target_file["sector"] = self.next_free_sector
                        self.next_free_sector += new_sectors
                    write_with_journal(wfd, target_file["sector"], padded_payload)
                    target_file["size"] = exact_size
                    target_file["attributes"] = final_attrs
                    target_file["encrypted"] = bool(final_attrs & FILE_ATTR_ENCRYPTED)
                    self.update_directory(wfd); os.fsync(wfd)
                finally:
                    os.close(wfd)
                self.ram_cache.put(self._cache_key(full_path), new_bytes,
                                   persist=not bool(final_attrs & FILE_ATTR_ENCRYPTED))
                metadata = self.file_metadata.setdefault(full_path, {})
                metadata["checksum"] = data_checksum(new_bytes)
                save_file_metadata(self.drive_path, self.file_metadata)
                self._update_ram_cache_status()
                self.scan_drive()
                messagebox.showinfo("Saved", f"Successfully saved '{rel_name}' ({exact_size} bytes).")
            except Exception as e:
                messagebox.showerror("Save Failed", str(e))

        if raw_content.startswith(ARCHIVE_MAGIC) or f["filename"].endswith(".MAREKARCHV"):
            archive_dict = parse_marekfs_archive(raw_content) or {}
            def archive_save_callback(new_archive_binary):
                save_payload_to_disk(new_archive_binary, f["attributes"], f["encrypted"])
            ModernMarekFSArchiveViewer(self.root, f["filename"], archive_dict, archive_save_callback, on_close=release_cb)
        else:
            text = raw_content.decode("utf-8", errors="ignore")
            def text_save_callback(new_text, new_attrs, was_enc):
                save_payload_to_disk(new_text.encode("utf-8"), new_attrs, was_enc)
            ModernMarekFSEditor(self.root, f["filename"], text, text_save_callback, f["attributes"], f["encrypted"], on_close=release_cb, app=self)

    def show_properties(self):
        sel = self.tree.selection()
        if not sel: return
        values = self.tree.item(sel[0])['values']
        rel_name = values[1] if len(values) > 1 else values[0]
        if rel_name == "..": return
        full_path = f"{self.current_folder}/{rel_name}" if self.current_folder else rel_name
        f = next((item for item in self.files_data if item["filename"] == full_path), None)
        if not f: return
        def save_attributes(new_attrs):
            f["attributes"] = new_attrs
            f["is_dir"] = bool(new_attrs & FILE_ATTR_DIRECTORY)
            try:
                fd = open_drive(self.drive_path, read_write=True)
                try:
                    self.update_directory(fd); os.fsync(fd)
                finally: os.close(fd)
                self.scan_drive()
            except Exception as e:
                messagebox.showerror("Update Failed", str(e))
        access = self.file_metadata.get(full_path, {}).get("access", {"mode": "all", "users": []})
        def save_access(mode, users, fp=full_path):
            metadata = self.file_metadata.setdefault(fp, {})
            set_file_permission(metadata, mode, users)
            save_file_metadata(self.drive_path, self.file_metadata)
            self.render_tree()
        ModernMarekFSProperties(self.root, f, save_attributes,
                                description=self.file_metadata.get(full_path, {}).get("description", ""),
                                description_callback=lambda text, fp=full_path: self._save_description(fp, text),
                                access_mode=access.get("mode", "all"), access_users=access.get("users", []),
                                user_names=self.disk_users.list_users(), access_callback=save_access)

    def delete_file_by_path(self, full_path):
        self.files_data = [item for item in self.files_data if item["filename"] != full_path]
        fd = open_drive(self.drive_path, read_write=True)
        try:
            self.update_directory(fd); os.fsync(fd)
        finally:
            os.close(fd)
        self.ram_cache.invalidate(self._cache_key(full_path))
        self.scan_drive()

    def delete_file(self):
        sel = self.tree.selection()
        if not sel: return
        values = self.tree.item(sel[0])['values']
        rel_name = values[1] if len(values) > 1 else values[0]
        if rel_name == "..": return
        full_path = f"{self.current_folder}/{rel_name}" if self.current_folder else rel_name
        if is_file_locked_by_other(full_path) or full_path in self.held_locks:
            messagebox.showwarning("Delete Denied — File Locked",
                f"'{full_path}' is open in an editor. The write handle must be closed before it can be deleted.")
            return
        self.files_data = [item for item in self.files_data if item["filename"] != full_path]
        try:
            fd = open_drive(self.drive_path, read_write=True)
            try:
                self.update_directory(fd); os.fsync(fd)
            finally: os.close(fd)
            self.ram_cache.invalidate(self._cache_key(full_path))
            self._update_ram_cache_status()
            self.scan_drive()
        except Exception as e:
            messagebox.showerror("Delete Failed", str(e))

    # --- Selection helpers ------------------------------------------------
    def _selected_record(self, need_file=True):
        """Return (rel_name, full_path, record) for the selected row."""
        sel = self.tree.selection()
        if not sel:
            return None, None, None
        values = self.tree.item(sel[0])['values']
        rel_name = values[1] if len(values) > 1 else values[0]
        if rel_name == "..":
            return None, None, None
        full_path = f"{self.current_folder}/{rel_name}" if self.current_folder else rel_name
        f = next((item for item in self.files_data if item["filename"] == full_path), None)
        if need_file and (f is None or f.get("is_dir") or (f["attributes"] & FILE_ATTR_DIRECTORY)):
            return rel_name, full_path, None
        return rel_name, full_path, f

    def _write_file_bytes(self, f, new_bytes, password="", attributes=None):
        """Re-write a file's payload (re-allocating sectors when it grew)."""
        attrs = f["attributes"] if attributes is None else attributes
        padded_payload, final_attrs = prepare_file_payload(new_bytes, password, attrs)
        fd = open_drive(self.drive_path, read_write=True)
        try:
            old_logical = f["size"] + (44 if (f.get("encrypted") or (f.get("attributes", 0) & FILE_ATTR_ENCRYPTED)) else 0)
            old_sectors = max(1, (old_logical + SECTOR_SIZE - 1) // SECTOR_SIZE)
            new_sectors = max(1, len(padded_payload) // SECTOR_SIZE)
            if new_sectors > old_sectors:
                f["sector"] = self.next_free_sector
                self.next_free_sector += new_sectors
            write_with_journal(fd, f["sector"], padded_payload)
            f["size"] = len(new_bytes)
            f["attributes"] = final_attrs
            f["encrypted"] = bool(final_attrs & FILE_ATTR_ENCRYPTED)
            self.update_directory(fd)
            os.fsync(fd)
        finally:
            os.close(fd)
        # keep RAM + chunk store in sync with what just hit the platter
        self.ram_cache.put(self._cache_key(f["filename"]), new_bytes,
                           persist=not bool(f.get("encrypted")))
        metadata = self.file_metadata.setdefault(f["filename"], {})
        metadata["checksum"] = data_checksum(new_bytes)
        save_file_metadata(self.drive_path, self.file_metadata)
        self._update_ram_cache_status()
        self.scan_drive()

    # --- Per-file lock icon (encrypt / decrypt) ---------------------------
    def _on_tree_click(self, event):
        """A click on the 🔒 column toggles encryption for that file."""
        if self.tree.identify_region(event.x, event.y) != "cell":
            return
        if self.tree.identify_column(event.x) != "#9":
            return
        row = self.tree.identify_row(event.y)
        if not row:
            return
        self.tree.selection_set(row)
        self.toggle_file_encryption()
        return "break"

    def toggle_file_encryption(self):
        rel_name, full_path, f = self._selected_record()
        if f is None:
            messagebox.showinfo("Encrypt", "Select a file (not a folder) to lock or unlock.")
            return
        if is_file_locked_by_other(full_path) or full_path in self.held_locks:
            messagebox.showwarning("File Locked", f"'{full_path}' is open in an editor — close it first.")
            return

        if f.get("encrypted"):
            password = simpledialog.askstring("Unlock File", f"🔓 Password for '{rel_name}':", show="*")
            if password is None:
                return
            try:
                raw = self._read_file_bytes(f, password)
                self._write_file_bytes(f, raw, "", f["attributes"] & ~FILE_ATTR_ENCRYPTED)
            except Exception as e:
                messagebox.showerror("Unlock Failed", str(e))
                return
            self.status_var.set(f"🔓 '{rel_name}' decrypted.")
        else:
            password = simpledialog.askstring("Encrypt File", f"🔒 New password for '{rel_name}':", show="*")
            if not password:
                return
            confirm = simpledialog.askstring("Encrypt File", "🔒 Repeat the password:", show="*")
            if confirm != password:
                messagebox.showwarning("Encrypt", "The passwords did not match.")
                return
            try:
                raw = self._read_file_bytes(f, "")
                self._write_file_bytes(f, raw, password, f["attributes"] | FILE_ATTR_ENCRYPTED)
            except Exception as e:
                messagebox.showerror("Encrypt Failed", str(e))
                return
            self.status_var.set(f"🔒 '{rel_name}' encrypted — keep the password safe.")

    # --- Full partition encryption ----------------------------------------
    def _update_part_enc_label(self):
        if not hasattr(self, "part_enc_var"):
            return
        if not self.partitions:
            self.part_enc_var.set("")
            return
        p = self.partitions[self.active_partition_index]
        self.part_enc_var.set("🔐 ENCRYPTED" if is_partition_encrypted(self.drive_path, p) else "🔓 plain")

    def crypt_active_partition(self, decrypt):
        if not self.partitions:
            messagebox.showinfo("Partitions", "There are no partitions on this disk.")
            return
        part = self.partitions[self.active_partition_index]
        title = "Decrypt Partition" if decrypt else "Encrypt Partition"
        verb = "decrypt" if decrypt else "encrypt"
        if not messagebox.askyesno(title,
                f"{verb.capitalize()} partition '{part['id']}' ({format_bytes(part['size_bytes'])})?\n\n"
                "Every data sector of the partition is rewritten. Do not close the app "
                "while this runs — an interrupted run leaves the partition half converted."):
            return
        password = simpledialog.askstring(title, f"🔐 Partition password for '{part['id']}':", show="*")
        if not password:
            return
        if not decrypt:
            confirm = simpledialog.askstring(title, "🔐 Repeat the password:", show="*")
            if confirm != password:
                messagebox.showwarning(title, "The passwords did not match.")
                return

        prog_win = tk.Toplevel(self.root)
        prog_win.title(f"{title} — {part['id']}")
        prog_win.geometry("460x150")
        prog_win.resizable(True, True)
        ttk.Label(prog_win, text=f"{verb.capitalize()}ing partition {part['id']}…",
                  font=("Segoe UI", 11, "bold")).pack(pady=10)
        bar = ttk.Progressbar(prog_win, length=400, mode="determinate")
        bar.pack(pady=6)
        lbl = tk.StringVar(value="Starting…")
        ttk.Label(prog_win, textvariable=lbl).pack()
        cancelled = {"v": False}
        ttk.Button(prog_win, text="Cancel", command=lambda: cancelled.update(v=True)).pack(pady=6)

        def progress(done, total):
            pct = (done / total * 100.0) if total else 100.0
            self.root.after(0, lambda: (bar.configure(value=pct),
                                        lbl.set(f"{format_bytes(done)} / {format_bytes(total)}  ({pct:.1f}%)")))

        def work():
            try:
                crypt_partition(self.drive_path, part, password, decrypt=decrypt,
                                progress=progress, cancel=lambda: cancelled["v"])
                self.root.after(0, lambda: (prog_win.destroy(),
                                            messagebox.showinfo(title, f"Partition '{part['id']}' {verb}ed."),
                                            self._update_part_enc_label(),
                                            self.scan_drive()))
            except Exception as e:
                msg = str(e)
                self.root.after(0, lambda: (prog_win.destroy(), messagebox.showerror(title, msg)))

        threading.Thread(target=work, daemon=True).start()

    # --- Built-in apps ------------------------------------------------------
    def _load_selected_bytes(self):
        rel_name, full_path, f = self._selected_record()
        if f is None:
            messagebox.showinfo("Open", "Select a file first.")
            return None, None, None
        password = ""
        if f.get("encrypted"):
            password = simpledialog.askstring("Password Required", "🔒 Enter password:", show="*")
            if password is None:
                return None, None, None
        try:
            return rel_name, f, self._read_file_bytes(f, password)
        except Exception as e:
            messagebox.showerror("Open Error", str(e))
            return None, None, None

    def _media_source(self):
        def write_record(name, data):
            existing = next((r for r in self.files_data if r.get("filename") == name), None)
            if existing:
                self._write_file_bytes(existing, data)
            else:
                self.create_entry(name, data, 0)
            return "marekfs:/" + name
        source = MarekFSVirtualMediaSource(lambda: self.files_data, lambda record: self._read_file_bytes(record, ""), write_record)
        # Expose the virtual drive to media windows opened from the main app.
        try:
            self.root._marekfs_media_source = source
        except Exception:
            pass
        return source

    def open_music_player(self):
        MusicPlayerWindow(self.root, marekfs_source=self._media_source())

    def open_image_editor(self):
        rel_name, f, data = self._load_selected_bytes()
        if data is None:
            ImageEditorWindow(self.root, "New image.png", save_callback=None)
        else:
            ImageEditorWindow(self.root, rel_name, data=data, save_callback=lambda new_bytes, rec=f: self._write_file_bytes(rec, new_bytes))

    def open_in_media(self, mode):
        rel_name, f, data = self._load_selected_bytes()
        if data is None:
            return
        save_cb = (lambda new_bytes, rec=f: self._write_file_bytes(rec, new_bytes))
        if mode == "music":
            MusicPlayerWindow(self.root, rel_name, data, marekfs_source=self._media_source())
        elif mode == "video":
            VideoPlayerWindow(self.root, rel_name, data, marekfs_source=self._media_source())
        elif mode == "image":
            ImageViewerWindow(self.root, rel_name, data,
                              on_edit=lambda im: ImageEditorWindow(self.root, rel_name, image=im,
                                                                   save_callback=save_cb))
        elif mode == "edit":
            ImageEditorWindow(self.root, rel_name, data=data, save_callback=save_cb)

    def create_marekvid_from_files(self):
        paths = filedialog.askopenfilenames(title="Select video variants (many MP4s supported)",
            filetypes=[("Video", "*.mp4 *.mkv *.mov *.webm *.avi *.m4v *.ts *.ogv"), ("All files", "*.*")])
        if not paths:
            return
        target = filedialog.asksaveasfilename(defaultextension=".marekvid", filetypes=[("Marekvid", "*.marekvid")])
        if not target:
            return
        entries = []
        for path in paths:
            entries.append({"name": os.path.basename(path), "path": path,
                            "resolution": "source", "kind": "video"})
        try:
            with open(target, "wb") as out:
                out.write(create_marekvid(entries, "Video variants exported from MarekFS"))
            self.status_var.set(f"Created Marekvid with {len(entries)} video variant(s).")
        except Exception as e:
            messagebox.showerror("Marekvid", str(e))

    def open_video_player(self):
        rel_name, full_path, f = self._selected_record()
        if f is not None:
            self.open_in_media("video")
        else:
            VideoPlayerWindow(self.root, "External video", data=b"")

    # --- Filename & content translator -------------------------------------
    def open_translator(self):
        rel_name, f, data = self._load_selected_bytes()
        if data is None:
            return
        TranslatorWindow(self.root, f["filename"], data,
                         rename_callback=self.rename_entry,
                         save_callback=lambda new_bytes, rec=f: self._write_file_bytes(rec, new_bytes))

    def _save_description(self, full_path, description):
        self.file_metadata.setdefault(full_path, {})["description"] = description.strip()
        save_file_metadata(self.drive_path, self.file_metadata)
        self.render_tree()

    def rename_entry(self, old_full_path, new_full_path):
        """Rename a directory entry (used by the translator)."""
        target = next((item for item in self.files_data if item["filename"] == old_full_path), None)
        if not target:
            return
        if len(new_full_path) > MAX_LOGICAL_FILENAME_CHARS:
            messagebox.showerror("Rename", f"Names can be up to {MAX_LOGICAL_FILENAME_CHARS} characters.")
            return
        if any(i["filename"] == new_full_path for i in self.files_data):
            messagebox.showwarning("Rename", f"'{new_full_path}' already exists.")
            return
        target["filename"] = new_full_path
        file_id = str(file_id_for_record(target))
        self.file_id_database.setdefault(file_id, {})["name"] = new_full_path
        self.file_id_database[file_id]["file_id"] = int(file_id)
        self.file_id_database[file_id]["updated"] = time.time()
        save_file_id_database(self.file_id_database)
        if old_full_path in self.file_metadata:
            self.file_metadata[new_full_path] = self.file_metadata.pop(old_full_path)
            save_file_metadata(self.drive_path, self.file_metadata)
        try:
            fd = open_drive(self.drive_path, read_write=True)
            try:
                self.update_directory(fd); os.fsync(fd)
            finally:
                os.close(fd)
            self.scan_drive()
        except Exception as e:
            messagebox.showerror("Rename Failed", str(e))

    def set_wallpaper(self):
        """Choose a background: still image, animated .gif or a .mp4 video."""
        path = self.background.choose(self.root)
        if path:
            self.wallpaper_label_var.set(self.background.describe())
            self.status_var.set(f"🖼️ Background set: {os.path.basename(path)}")

    def clear_wallpaper(self):
        self.background.clear()
        self.wallpaper_label_var.set("No background set — choose an image, GIF or MP4")
        self.status_var.set("🚫 Background cleared.")

    def _render_wallpaper(self):
        if self.background:
            self.background.render_once()


# Filesystem entries shown in the Extended FS settings panel (two columns).
_FS_COLUMNS = [
    ("MarekFS",  "NTFS"),
    ("HFS",      "APFS"),
    ("HFS+",     "FAT16"),
    ("BTRFS",    "FAT32"),
    ("RAMFS",    "exFAT"),
    ("TMPFS",    None),
    ("EXT1",     "EXT2"),
    ("EXT3",     "EXT4"),
]


class SettingsWindow:
    """Settings window: PreferredPartitionID and partition table for the currently open disk."""
    def __init__(self, parent, app):
        self.app = app
        self.win = tk.Toplevel(parent)
        self.win.title("⚙️ MarekFS Settings")
        self.win.geometry("600x820")
        self.win.resizable(True, True)

        # ---- ProgramData Config ----------------------------------------
        cfg_frame = ttk.LabelFrame(self.win, text=" ProgramData Config ", padding=12)
        cfg_frame.pack(fill=tk.X, padx=15, pady=15)
        ttk.Label(cfg_frame, text=f"Config file: {PROGRAM_DATA_CONFIG_PATH}", wraplength=550).pack(anchor=tk.W, pady=2)
        ttk.Label(cfg_frame, text="PreferredPartitionID:").pack(anchor=tk.W, pady=(8, 2))

        self.pref_var = tk.StringVar(value=get_preferred_partition_id())
        self.pref_combo = ttk.Combobox(cfg_frame, textvariable=self.pref_var, width=24, state="readonly")
        self.pref_combo.pack(anchor=tk.W, pady=2)
        self._refresh_pref_combo()

        btn_frame = ttk.Frame(cfg_frame); btn_frame.pack(fill=tk.X, pady=8)
        ttk.Button(btn_frame, text="💾 Save Preferred", style="Accent.TButton", command=self.save_preferred).pack(side=tk.LEFT, padx=4)
        ttk.Button(btn_frame, text="🔄 Refresh from Disk", command=self.refresh_from_disk).pack(side=tk.LEFT, padx=4)

        # ---- Extended Filesystem Support --------------------------------
        fs_outer = ttk.LabelFrame(self.win, text=" Extended Filesystem Support ")
        fs_outer.pack(fill=tk.X, padx=15, pady=5)
        ttk.Label(
            fs_outer,
            text=("Select which filesystems MarekFS recognises and interacts with. "
                  "MarekFS is always enabled and cannot be disabled."),
            wraplength=550,
        ).pack(anchor=tk.W, pady=(0, 8))

        enabled_now = get_extended_fs_support()
        self._fs_vars = {}  # name -> BooleanVar
        grid = ttk.Frame(fs_outer)
        grid.pack(anchor=tk.W)
        for row, (left, right) in enumerate(_FS_COLUMNS):
            for col, name in enumerate([left, right]):
                if name is None:
                    continue
                var = tk.BooleanVar(value=(name in enabled_now))
                self._fs_vars[name] = var
                cb = ttk.Checkbutton(
                    grid, text=name, variable=var,
                    state="disabled" if name == "MarekFS" else "normal",
                    width=14,
                )
                cb.grid(row=row, column=col, sticky=tk.W, padx=8, pady=2)

        fs_btn = ttk.Frame(fs_outer)
        fs_btn.pack(fill=tk.X, pady=(10, 0))
        ttk.Button(
            fs_btn, text="💾 Save Filesystem Settings",
            style="Accent.TButton", command=self.save_fs_support,
        ).pack(side=tk.LEFT, padx=4)
        ttk.Button(fs_btn, text="Check All", command=self._fs_check_all).pack(side=tk.LEFT, padx=4)
        ttk.Button(fs_btn, text="Uncheck All", command=self._fs_uncheck_all).pack(side=tk.LEFT, padx=4)

        # ---- Partitions on this disk ------------------------------------
        part_frame = ttk.LabelFrame(self.win, text=" Partitions on this disk ", padding=12)
        part_frame.pack(fill=tk.BOTH, expand=True, padx=15, pady=5)
        cols = ("Index", "Partition ID", "Size")
        self.tree = ttk.Treeview(part_frame, columns=cols, show="headings", height=6)
        for c, w in zip(cols, (60, 160, 200)):
            self.tree.heading(c, text=c); self.tree.column(c, width=w)
        self.tree.pack(fill=tk.BOTH, expand=True)
        self._refresh_part_tree()

        ttk.Label(self.win, text=f"Maximum partitions supported: {MAX_PARTITIONS}").pack(anchor=tk.W, padx=15, pady=4)

    # -- Preferred partition helpers -------------------------------------
    def _refresh_pref_combo(self):
        ids = [p["id"] for p in self.app.partitions]
        self.pref_combo['values'] = ids
        if self.pref_var.get() not in ids and ids:
            self.pref_var.set(ids[0])

    def _refresh_part_tree(self):
        for i in self.tree.get_children(): self.tree.delete(i)
        for i, p in enumerate(self.app.partitions):
            self.tree.insert("", tk.END, values=(i, p["id"], format_bytes(p["size_bytes"])))

    def save_preferred(self):
        pid = self.pref_var.get().strip()
        if not pid:
            messagebox.showwarning("No Partition", "Select a partition id first.")
            return
        ok = set_preferred_partition_id(pid)
        if ok:
            messagebox.showinfo("Saved", f"PreferredPartitionID set to '{pid}'.\nClick Refresh in the main window to apply.")
        else:
            messagebox.showwarning("Permission Denied",
                f"Could not write to {PROGRAM_DATA_CONFIG_PATH}.\nRun as Administrator to change the global config.")

    def refresh_from_disk(self):
        self.app.refresh_partitions()
        self._refresh_pref_combo()
        self._refresh_part_tree()
        self.pref_var.set(get_preferred_partition_id())

    # -- Extended filesystem helpers ------------------------------------
    def _fs_check_all(self):
        for var in self._fs_vars.values():
            var.set(True)

    def _fs_uncheck_all(self):
        for name, var in self._fs_vars.items():
            if name != "MarekFS":  # always on
                var.set(False)

    def save_fs_support(self):
        enabled = [name for name, var in self._fs_vars.items() if var.get()]
        ok = set_extended_fs_support(enabled)
        if ok:
            messagebox.showinfo("Saved", "Extended Filesystem support settings saved.")
        else:
            messagebox.showwarning("Permission Denied",
                f"Could not write to {PROGRAM_DATA_CONFIG_PATH}.\nRun as Administrator to change the global config.")


if __name__ == "__main__":
    root = tk.Tk()
    app = ModernMarekFSApp(root)
    root.mainloop()
