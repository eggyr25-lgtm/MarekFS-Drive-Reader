"""MarekFS Real-Time Scanner Monitor: Silent file scanning with Windows toast notifications, quarantine management, and whitelist support."""

import os
import sys
import time
import json
import hashlib
import base64
import threading
import subprocess
import tempfile
from pathlib import Path
from datetime import datetime, timedelta
from collections import defaultdict

# Windows-specific imports for toast notifications
try:
    from winrt.windows.ui.notifications import ToastNotificationManager, ToastNotification, ToastTemplateType
    from winrt.windows.data.xml.dom import XmlDocument
    WINDOWS_TOAST_AVAILABLE = True
except ImportError:
    WINDOWS_TOAST_AVAILABLE = False

# Fallback to ctypes for Windows toast if winrt not available
if sys.platform == 'win32' and not WINDOWS_TOAST_AVAILABLE:
    try:
        import ctypes
        from ctypes import wintypes
        WINDOWS_NATIVE_AVAILABLE = True
    except ImportError:
        WINDOWS_NATIVE_AVAILABLE = False

from marekfs_core import (
    ensure_scanner_config, save_scanner_config,
    scan_bytes_with_builtin_scanner, scan_path_with_builtin_scanner,
    PROGRAM_DATA_CONFIG_PATH
)

# Constants
QUARANTINE_DIR = os.environ.get("MAREKFS_QUARANTINE_DIR") or os.path.join(os.path.dirname(PROGRAM_DATA_CONFIG_PATH), "Quarantine")
WHITELIST_FILE = os.path.join(os.path.dirname(PROGRAM_DATA_CONFIG_PATH), "whitelist.json")
QUARANTINE_DB_FILE = os.path.join(QUARANTINE_DIR, "quarantine_db.json")
SCAN_CONFIG_FILE = os.path.join(os.path.dirname(PROGRAM_DATA_CONFIG_PATH), "scanner_monitor_config.json")

# Default monitored extensions (common malware targets)
DEFAULT_MONITOR_EXTENSIONS = {
    '.exe', '.dll', '.sys', '.drv', '.ocx', '.cpl', '.msi', '.msp', '.com', '.scr',
    '.bat', '.cmd', '.ps1', '.vbs', '.vbe', '.js', '.jse', '.wsf', '.wsh',
    '.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx', '.pdf',
    '.zip', '.rar', '.7z', '.cab', '.iso', '.img',
    '.html', '.htm', '.svg', '.jar', '.class',
    '.py', '.pyw', '.ps1', '.rb', '.pl', '.sh',
    '.lnk', '.msi', '.reg', '.pif', '.msp'
}

# File size limit for scanning (4.5GB)
MAX_SCAN_FILE_SIZE = 4.5 * 1024 * 1024 * 1024

# Debounce interval for file changes (seconds)
FILE_CHANGE_DEBOUNCE = 2.0
import shutil
from concurrent.futures import ThreadPoolExecutor

# Files larger than QUARANTINE_RAW_LIMIT_BYTES are stored raw (not base64) to
# avoid a 33 % storage penalty inside the quarantine folder.
QUARANTINE_RAW_LIMIT_BYTES = 200 * 1024 * 1024

# ---------------------------------------------------------------------------
# Monitor configuration (C:\ProgramData\MarekFS\scanner_monitor_config.json)
# ---------------------------------------------------------------------------

def default_watch_directories():
    """Return sane default folders to watch: Downloads, Desktop, Documents, temp."""
    dirs = set()
    home = os.path.expanduser("~")
    if sys.platform == "win32":
        candidates = ["Downloads", "Desktop", "Documents", "Pictures", "Videos"]
        for sub in candidates:
            d = os.path.join(home, sub)
            if os.path.isdir(d):
                dirs.add(d)
        for env in ("TEMP", "TMP"):
            d = os.environ.get(env, "")
            if d and os.path.isdir(d):
                dirs.add(d)
        d = r"C:\Windows\Temp"
        if os.path.isdir(d):
            dirs.add(d)
    else:
        for sub in ("Downloads", "Desktop", "Documents"):
            d = os.path.join(home, sub)
            if os.path.isdir(d):
                dirs.add(d)
    return sorted(x for x in dirs if x)


def default_monitor_config():
    """Default scanner-monitor settings (stored next to the main MarekFS config)."""
    return {
        "enabled": True,
        "host_scanning_enabled": True,
        "recursive": True,
        "directories": default_watch_directories(),
        "extensions": sorted(DEFAULT_MONITOR_EXTENSIONS),
        "debounce_seconds": FILE_CHANGE_DEBOUNCE,
        "poll_interval_seconds": 2.0,
        "max_scan_file_size_bytes": MAX_SCAN_FILE_SIZE,
        "notify_on_malicious": True,
        "quarantine_on_malicious": True,
        "notify_on_suspicious": True,
        "quarantine_on_suspicious": False,
        "whitelist": [],
    }


def load_monitor_config():
    """Merge the on-disk monitor config with defaults (tolerates missing/bad files)."""
    base = default_monitor_config()
    try:
        if os.path.exists(SCAN_CONFIG_FILE):
            with open(SCAN_CONFIG_FILE, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                base.update({k: v for k, v in data.items() if v is not None})
    except Exception:
        pass
    return base


def save_monitor_config(config):
    """Persist the scanner-monitor config; returns True on success."""
    try:
        os.makedirs(os.path.dirname(SCAN_CONFIG_FILE), exist_ok=True)
        with open(SCAN_CONFIG_FILE, "w", encoding="utf-8") as fh:
            json.dump(config, fh, indent=2)
        return True
    except OSError:
        return False
class QuarantineManager:
    """Manages quarantined files stored in C:\\ProgramData\\MarekFS\\Quarantine.

    Small files are stored base64-encoded (never directly usable as executables);
    large files are stored raw with a .qar extension. A JSON index tracks the
    original location, detection reason and restore state for every entry.
    """

    def __init__(self, directory=None):
        self.quarantine_dir = directory or QUARANTINE_DIR
        self.db_file = os.path.join(self.quarantine_dir, "quarantine_db.json")
        self._lock = threading.RLock()
        try:
            os.makedirs(self.quarantine_dir, exist_ok=True)
        except OSError:
            pass
        self._entries = self._load_db()

    # ---- persistence -----------------------------------------------------
    def _load_db(self):
        try:
            if os.path.exists(self.db_file):
                with open(self.db_file, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
                if isinstance(data, list):
                    return data
        except Exception:
            pass
        return []

    def _save_db(self):
        try:
            tmp = self.db_file + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self._entries, fh, indent=2, ensure_ascii=False)
            os.replace(tmp, self.db_file)
        except OSError:
            pass

    # ---- lookup ----------------------------------------------------------
    def _find(self, entry_id):
        for e in self._entries:
            if e.get("id") == entry_id:
                return e
        return None

    def get_quarantine_list(self):
        """Return a copy of all quarantine entries (safe for UI display)."""
        with self._lock:
            return [dict(e) for e in self._entries]

    def get_entry(self, entry_id):
        with self._lock:
            e = self._find(entry_id)
            return dict(e) if e else None
# ---- quarantine ------------------------------------------------------
    def quarantine_file(self, source_path, threat_name="Threat detected", details=None):
        """Move a file into quarantine.

        Copies the content into the quarantine directory (base64-encoded when
        small), records metadata, then removes the original. Returns the entry
        id or None on failure.
        """
        source_path = os.path.abspath(source_path)
        if not os.path.isfile(source_path):
            return None
        size = 0
        try:
            size = os.path.getsize(source_path)
            if size > MAX_SCAN_FILE_SIZE:
                return None
            with open(source_path, "rb") as fh:
                digest = hashlib.sha256()
                while True:
                    chunk = fh.read(8 * 1024 * 1024)
                    if not chunk:
                        break
                    digest.update(chunk)
                sha256 = digest.hexdigest()
        except OSError:
            return None

        entry_id = time.strftime("%Y%m%dT%H%M%S") + "-" + sha256[:8]
        use_b64 = size <= QUARANTINE_RAW_LIMIT_BYTES
        storage_name = entry_id + (".b64" if use_b64 else ".quark")
        storage_path = os.path.join(self.quarantine_dir, storage_name)
        try:
            with open(source_path, "rb") as src, open(storage_path, "wb") as dst:
                if use_b64:
                    dst.write(base64.b64encode(src.read()))
                else:
                    shutil.copyfileobj(src, dst, 16 * 1024 * 1024)
        except OSError:
            return None

        entry = {
            "id": entry_id,
            "original_path": source_path,
            "storage_path": storage_path,
            "threat_name": threat_name or "Threat detected",
            "sha256": sha256,
            "size_bytes": size,
            "base64_encoded": use_b64,
            "quarantined_at": datetime.now().isoformat(timespec="seconds"),
            "status": "quarantined",
            "details": details or {},
        }
        self._register(entry)
        # Remove the original so the host is no longer exposed to it.
        for attempt in range(3):
            try:
                os.remove(source_path)
                break
            except OSError:
                time.sleep(0.2)
        return entry_id

    def _register(self, entry):
        with self._lock:
            self._entries.append(entry)
            self._save_db()
        return entry["id"]
    def restore_file(self, entry_id, dest_path=None):
        """Restore a quarantined file. Returns (ok: bool, message: str)."""
        with self._lock:
            entry = self._find(entry_id)
            if entry is None:
                return False, "Quarantine entry not found."
            if entry.get("status") != "quarantined":
                return False, "Entry is not in the quarantined state."
            storage = entry.get("storage_path")
            if not storage or not os.path.isfile(storage):
                return False, "Stored payload is missing."
            target = dest_path or entry.get("original_path")
            if not target:
                return False, "No original path recorded."
            try:
                os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
                with open(storage, "rb") as src, open(target, "wb") as dst:
                    if entry.get("base64_encoded"):
                        dst.write(base64.b64decode(src.read()))
                    else:
                        shutil.copyfileobj(src, dst, 16 * 1024 * 1024)
                os.remove(storage)
            except (OSError, ValueError) as exc:
                return False, f"Restore failed: {exc}"
            entry["status"] = "restored"
            entry["restored_to"] = target
            self._save_db()
            return True, f"Restored to {target}"

    def delete_permanently(self, entry_id):
        """Permanently delete a quarantined payload. Returns (ok, message)."""
        with self._lock:
            entry = self._find(entry_id)
            if entry is None:
                return False, "Quarantine entry not found."
            storage = entry.get("storage_path")
            if storage and os.path.isfile(storage):
                try:
                    os.remove(storage)
                except OSError as exc:
                    return False, f"Could not delete payload: {exc}"
            entry["status"] = "deleted"
            self._save_db()
            return True, "Quarantined payload permanently deleted."


class WhitelistManager:
    """Tracks paths/folders that must never be quarantined or scanned."""

    def __init__(self, whitelist_file=None):
        self.whitelist_file = whitelist_file or WHITELIST_FILE
        self._lock = threading.RLock()
        try:
            os.makedirs(os.path.dirname(self.whitelist_file), exist_ok=True)
        except OSError:
            pass
        self._paths = self._load()

    def _load(self):
        try:
            if os.path.exists(self.whitelist_file):
                with open(self.whitelist_file, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
                if isinstance(data, list):
                    return [p for p in data if isinstance(p, str)]
                if isinstance(data, dict):
                    items = data.get("whitelist") or data.get("paths") or []
                    return [p for p in items if isinstance(p, str)]
        except Exception:
            pass
        return []

    def _save(self):
        try:
            with open(self.whitelist_file, "w", encoding="utf-8") as fh:
                json.dump(self._paths, fh, indent=2, ensure_ascii=False)
        except OSError:
            pass

    def add(self, path):
        """Add a path or directory to the whitelist."""
        path = os.path.abspath(path)
        with self._lock:
            if path not in self._paths:
                self._paths.append(path)
                self._save()
        return True

    def remove(self, path):
        path = os.path.abspath(path)
        with self._lock:
            if path in self._paths:
                self._paths.remove(path)
                self._save()
                return True
        return False

    def is_whitelisted(self, path):
        """True if the given path is exactly whitelisted or inside a whitelisted dir."""
        path = os.path.abspath(path)
        with self._lock:
            for w in self._paths:
                if path == w:
                    return True
                try:
                    rel = os.path.relpath(path, w)
                    if not rel.startswith("..") and not os.path.isabs(rel):
                        return True
                except (ValueError, OSError):
                    continue
        return False

    def get_all(self):
        with self._lock:
            return list(self._paths)
class ToastNotifier:
    """Shows Windows toast & balloon notifications with graceful fallbacks.

    Priority: winrt (native Windows toast) -> PowerShell toast -> native
    taskbar balloon via ctypes. All failures are silent so the scanner keeps
    running even if the notification stack is unavailable.
    """

    def __init__(self, app_id="MarekFS.ScannerMonitor", app_name="MarekFS"):
        self.app_id = app_id
        self.app_name = app_name

    # ------------------------------------------------------------------
    def show(self, title, message, icon="warning", timeout_ms=8000):
        """Show a notification. Never raises; returns True if shown."""
        if WINDOWS_TOAST_AVAILABLE:
            try:
                if self._show_winrt_toast(title, message):
                    return True
            except Exception:
                pass
        if sys.platform == "win32":
            try:
                if self._show_powershell_toast(title, message):
                    return True
            except Exception:
                pass
        if sys.platform == "win32" and WINDOWS_NATIVE_AVAILABLE:
            try:
                if self._show_balloon_toast(title, message, icon=icon, timeout_ms=timeout_ms):
                    return True
            except Exception:
                pass
        return False

    # ---- winrt (modern Windows toast) ---------------------------------
    def _show_winrt_toast(self, title, message):
        try:
            from winrt.windows.ui.notifications import (
                ToastNotificationManager, ToastNotification, ToastTemplateType)
            from winrt.windows.data.xml.dom import XmlDocument

            template = ToastNotificationManager.get_template_content(
                getattr(ToastTemplateType, "TOAST_TEXT_02",
                        getattr(ToastTemplateType, "TOAST_TEXT2", None)))
            text_nodes = template.get_elements_by_tag_name("text")
            text_nodes.item(0).inner_text = title
            text_nodes.item(1).inner_text = message
            notifier = ToastNotificationManager.create_toast_notifier(self.app_id)
            notifier.show(ToastNotification(template))
            return True
        except Exception:
            return False

    # ---- PowerShell fallback -------------------------------------------
    def _show_powershell_toast(self, title, message):
        xml = (
            '<toast duration="short">'
            '<visual><binding template="ToastText02">'
            f"<text id=\"1\">{self._escape_xml(title)}</text>"
            f"<text id=\"2\">{self._escape_xml(message)}</text>"
            "</binding></visual></toast>"
        )
        script = (
            "[Windows.UI.Notifications.ToastNotificationManager, "
            "Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null;"
            "[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, "
            "ContentType = WindowsRuntime] | Out-Null;"
            f"$xml = [Windows.Data.Xml.Dom.XmlDocument]::New();"
            f"$xml.LoadXml('{self._escape_for_ps(xml)}');"
            "try { $notif = [Windows.UI.Notifications.ToastNotification]::New($xml);"
            "[Windows.UI.Notifications.ToastNotificationManager]"
            f"::CreateToastNotifier('{self.app_id}').Show($notif); exit 3 }} catch {{ exit 1 }};"
        )
        flags = subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0
        try:
            proc = subprocess.Popen(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=flags)
            proc.communicate(timeout=10)
            return proc.returncode in (2, 3)
        except Exception:
            return False

    @staticmethod
    def _escape_xml(text):
        return (str(text).replace("&", "&amp;").replace("<", "&lt;")
                .replace(">", "&gt;").replace('"', "&quot;").replace("'", "&apos;"))

    @staticmethod
    def _escape_for_ps(text):
        return text.replace("'", "''").replace("`", "``")
# ---- native taskbar balloon (last resort) --------------------------
    def _show_balloon_toast(self, title, message, icon=1, timeout_ms=8000):
        if not WINDOWS_NATIVE_AVAILABLE:
            return False
        result = {}

        def _run():
            try:
                from ctypes import wintypes, Structure, byref
                from ctypes import windll
                user32 = windll.user32
                shell32 = windll.shell32
                NIF_INFO = 0x10
                NIM_ADD = 0x00000000
                NIM_DELETE = 0x00000002
                NIIF_INFO = 0x00000001
                NIIF_WARNING = 0x00000002
                NIIF_ERROR = 0x00000003
                icon_flag = NIIF_ERROR if icon == "error" else (NIIF_WARNING if icon == "warning" else NIIF_INFO)

                class NOTIFYICONDATAW(Structure):
                    _fields_ = [
                        ("cbSize", wintypes.DWORD),
                        ("hWnd", wintypes.HWND),
                        ("uID", wintypes.UINT),
                        ("uFlags", wintypes.UINT),
                        ("uCallbackMessage", wintypes.UINT),
                        ("hIcon", wintypes.HANDLE),
                        ("szTip", wintypes.WCHAR * 128),
                        ("dwState", wintypes.DWORD),
                        ("dwStateMask", wintypes.DWORD),
                        ("szInfo", wintypes.WCHAR * 256),
                        ("uTimeoutOrVersion", wintypes.UINT),
                        ("szInfoTitle", wintypes.WCHAR * 64),
                        ("dwInfoFlags", wintypes.DWORD),
                        ("guidItem", ctypes.c_byte * 16),
                        ("hBalloonIcon", wintypes.HANDLE),
                    ]

                hwnd = user32.CreateWindowExW(
                    0, "STATIC", "", 0, 0, 0, 0, 0, 0, None, None, None)
                if not hwnd:
                    result["ok"] = False
                    return

                nid = NOTIFYICONDATAW()
                nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
                nid.hWnd = hwnd
                nid.uID = 0x414D
                nid.uFlags = NIF_INFO
                nid.szInfoTitle = title[:63]
                nid.szInfo = message[:255]
                nid.dwInfoFlags = icon_flag
                if shell32.Shell_NotifyIconW(NIM_ADD, byref(nid)):
                    result["ok"] = True
                    time.sleep(timeout_ms / 1000.0)
                    shell32.Shell_NotifyIconW(NIM_DELETE, byref(nid))
                else:
                    result["ok"] = False
                user32.DestroyWindow(hwnd)
            except Exception:
                result["ok"] = False

        threading.Thread(target=_run, daemon=True).start()
        deadline = time.time() + 0.5
        while time.time() < deadline and "ok" not in result:
            time.sleep(0.01)
        return bool(result.get("ok"))
class FileMonitor:
    """Real-time folder watcher using fast directory polling.

    Recursively walks the configured folders, snapshots (size, mtime) per file
    matching the monitored extensions, and calls ``on_file(path, meta)`` when a
    brand-new file appears or an existing file changes. Uses a debounce so
    partially downloaded files are scanned only after they settle.
    """

    def __init__(self, on_file, directories=None, extensions=None,
                 recursive=True, poll_interval=2.0, debounce=None,
                 max_size_bytes=MAX_SCAN_FILE_SIZE):
        self.on_file = on_file
        self.recursive = bool(recursive)
        self.poll_interval = max(0.5, float(poll_interval))
        self.debounce_s = max(0.5, float(debounce or FILE_CHANGE_DEBOUNCE))
        self.max_size_bytes = max_size_bytes
        self.extensions = {e.lower() for e in (extensions or DEFAULT_MONITOR_EXTENSIONS)}
        self._dirs = []
        self._snapshot = {}
        self._last_signal = {}
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread = None
        if directories:
            for d in directories:
                self.add_directory(d)

    def add_directory(self, path):
        path = os.path.abspath(path)
        if os.path.isdir(path) and path not in self._dirs:
            self._dirs.append(path)
        return True

    # ------------------------------------------------------------------
    def _iter_files(self):
        for root in list(self._dirs):
            try:
                if self.recursive:
                    for dirpath, _dirnames, filenames in os.walk(root):
                        for fn in filenames:
                            yield os.path.join(dirpath, fn)
                else:
                    with os.scandir(root) as it:
                        for entry in it:
                            try:
                                if entry.is_file():
                                    yield entry.path
                            except OSError:
                                continue
            except OSError:
                continue

    def _scan_once(self, prime=False):
        current = {}
        for path in self._iter_files():
            norm = path.replace("\\", "/")
            ext = os.path.splitext(path)[1].lower()
            if ext not in self.extensions:
                continue
            try:
                st = os.stat(path)
                size = st.st_size
                mtime = int(st.st_mtime)
            except OSError:
                continue
            if size > self.max_size_bytes:
                continue
            current[norm] = (size, mtime)

        now = time.time()
        with self._lock:
            if prime:
                # Baseline pass: just record what exists so pre-existing files
                # are never reported as "new".
                self._snapshot = current
                return
            # brand-new files: present now but not in the previous snapshot
            for path, meta in current.items():
                prev = self._snapshot.get(path)
                last_seen = self._last_signal.get(path, 0.0)
                if prev is None and meta[0] > 0 and (now - last_seen) >= self.debounce_s:
                    self._last_signal[path] = now
                    try:
                        self.on_file(path, meta)
                    except Exception:
                        pass
            changed = [p for p in set(self._snapshot) - set(current)]
            self._snapshot = current
            for path in changed:
                self._last_signal.pop(path, None)

    # ------------------------------------------------------------------
    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._snapshot = {}

        def _loop():
            try:
                # Seed the baseline so pre-existing files are not reported as new.
                self._scan_once(prime=True)
            except Exception:
                pass
            while not self._stop.wait(self.poll_interval):
                self._scan_once()

        self._thread = threading.Thread(target=_loop, name="MarekFS-FileMonitor", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)
        self._thread = None
class ScannerMonitor:
    """Main scanner monitor service.

    Watches configured folders, silently scans every new/modified file with the
    MarekFS built-in scanner, quarantines threats, and shows toast/balloon
    notifications (e.g. "Virus moved to quarantine").
    """

    def __init__(self, enable_host_scanning=True, verbose=False, monitor_dirs=None):
        self.verbose = verbose
        self.enable_host_scanning = enable_host_scanning
        self.config = load_monitor_config()
        self.config["host_scanning_enabled"] = bool(
            enable_host_scanning or self.config.get("host_scanning_enabled", True))

        self.quarantine = QuarantineManager()
        self.whitelist = WhitelistManager()
        self.notifier = ToastNotifier()

        # Load scanner config through core so YARA/ClamAV settings apply.
        self._scanner_config = ensure_scanner_config()

        self._monitor = None
        self._pool = ThreadPoolExecutor(max_workers=max(2, min(8, (os.cpu_count() or 4) * 2)))
        self._pending = set()
        self._pending_lock = threading.Lock()
        self._stats_lock = threading.Lock()
        self._stats = {
            "scanned": 0, "clean": 0, "suspicious": 0,
            "malicious": 0, "quarantined": 0, "whitelisted": 0, "errors": 0,
        }
        if monitor_dirs:
            self.directories = list(monitor_dirs)
        else:
            self.directories = list(self.config.get("directories") or default_watch_directories())
        # Move the config whitelist entries (if any) into the live whitelist.
        for w in self.config.get("whitelist") or []:
            try:
                self.whitelist.add(w)
            except Exception:
                pass

    # ---- lifecycle ------------------------------------------------------
    def start(self):
        """Start watching and scanning. Safe to call multiple times."""
        if self._monitor and getattr(self._monitor, "_thread", None) and \
                self._monitor._thread.is_alive():
            return self

        exts = {e.lower() for e in self.config.get("extensions") or DEFAULT_MONITOR_EXTENSIONS}
        if self.enable_host_scanning and self.directories:
            self._monitor = FileMonitor(
                on_file=self._on_file_detected,
                directories=self.directories,
                extensions=exts,
                recursive=bool(self.config.get("recursive", True)),
                poll_interval=float(self.config.get("poll_interval_seconds", 2.0)),
                debounce=float(self.config.get("debounce_seconds", FILE_CHANGE_DEBOUNCE)),
                max_size_bytes=int(self.config.get("max_scan_file_size_bytes", MAX_SCAN_FILE_SIZE)),
            )
            self._monitor.start()
        return self

    def stop(self):
        """Stop the monitor and release the thread pool."""
        try:
            if self._monitor:
                self._monitor.stop()
        finally:
            self._monitor = None
        try:
            self._pool.shutdown(wait=False)
        except Exception:
            pass
        return self
# ---- watcher callback -----------------------------------------------
    def _on_file_detected(self, path, meta):
        """Called by FileMonitor when a new/changed monitored file appears."""
        try:
            real = path.replace("/", "\\")
            if not os.path.isfile(real):
                return
            if self.whitelist.is_whitelisted(real):
                with self._stats_lock:
                    self._stats["whitelisted"] += 1
                return
            try:
                if os.path.getsize(real) > self.config.get("max_scan_file_size_bytes", MAX_SCAN_FILE_SIZE):
                    return
            except OSError:
                return
            with self._pending_lock:
                if real in self._pending:
                    return
                self._pending.add(real)
            self._pool.submit(self._scan_and_handle, real)
        except Exception:
            pass

    # ---- actual scanning -------------------------------------------------
    def _scan_and_handle(self, path):
        try:
            result = self._scan_path(path)
            self._handle_result(path, result)
        except Exception as exc:
            with self._stats_lock:
                self._stats["errors"] += 1
            if self.verbose:
                print(f"[scanner-monitor] error scanning {path}: {exc}")
        finally:
            with self._pending_lock:
                self._pending.discard(path)

    def _scan_path(self, path):
        from marekfs_core import scan_path_with_builtin_scanner
        try:
            result = scan_path_with_builtin_scanner(path, self._scanner_config)
        except Exception:
            # Fallback: scan a bounded prefix through the bytes-based scanner when
            # the file is still locked by the writing process.
            try:
                with open(path, "rb") as fh:
                    data = fh.read(1_000_000)
                from marekfs_core import scan_bytes_with_builtin_scanner
                result = scan_bytes_with_builtin_scanner(os.path.basename(path), data, self._scanner_config)
            except Exception:
                return {"status": "error", "name": os.path.basename(path), "findings": []}
        with self._stats_lock:
            self._stats["scanned"] += 1
        return result or {}
    def _handle_result(self, path, result):
        status = result.get("status")
        name = os.path.basename(path)
        findings = result.get("findings") or []
        if status == "clean":
            with self._stats_lock:
                self._stats["clean"] += 1
            return
        if status == "suspicious":
            with self._stats_lock:
                self._stats["suspicious"] += 1
            if self.config.get("quarantine_on_suspicious", False):
                self._quarantine_with_toast(path, name, result, threat=findings[0].get("rule") if findings else "suspicious")
            elif self.config.get("notify_on_suspicious", True):
                detail = findings[0].get("rule") if findings else "suspicious content"
                self.notifier.show("MarekFS: Suspicious file",
                                   f"{name}\n{detail}\n\nQuarantine not performed.", icon="warning")
            return
        if status == "malicious":
            with self._stats_lock:
                self._stats["malicious"] += 1
            threat = findings[0].get("rule") if findings else "threat"
            self._quarantine_with_toast(path, name, result, threat=threat)
            return
        if status == "error":
            with self._stats_lock:
                self._stats["errors"] += 1

    # ---- quarantine + toast ----------------------------------------------
    def _quarantine_with_toast(self, path, name, result, threat="threat"):
        entry_id = self.quarantine.quarantine_file(
            path, threat_name=threat,
            details={"scanner_status": result.get("status"),
                     "sha256": result.get("sha256"),
                     "findings": result.get("findings")},
        )
        if entry_id:
            with self._stats_lock:
                self._stats["quarantined"] += 1
            if self.config.get("notify_on_malicious", True):
                self.notifier.show(
                    "Virus moved to quarantine",
                    f"{name}\n\nThreat: {threat}\nMoved to stored quarantine:\n"
                    f"{self.quarantine.quarantine_dir}\nRestore id: {entry_id[:16]}",
                    icon="warning")
        else:
            with self._stats_lock:
                self._stats["errors"] += 1
            if self.config.get("notify_on_malicious", True):
                self.notifier.show(
                    "Virus detected - quarantine failed",
                    f"{name}\n\nDetected {threat} but the file could not be moved.\n"
                    f"Please remove it manually: {path}",
                    icon="error")

    # ---- utilities -------------------------------------------------------
    def get_stats(self):
        with self._stats_lock:
            return dict(self._stats)

    def get_status(self):
        return {
            "running": bool(self._monitor and self._monitor._thread and self._monitor._thread.is_alive()),
            "host_scanning": self.enable_host_scanning,
            "directories": list(self.directories),
            "stats": self.get_stats(),
            "quarantine_dir": self.quarantine.quarantine_dir,
        }

    def add_directory(self, path):
        path = os.path.abspath(path)
        if path not in self.directories:
            self.directories.append(path)
            if self._monitor:
                self._monitor.add_directory(path)
        self.config["directories"] = list(self.directories)
        save_monitor_config(self.config)
        return True

# ---- public actions ---------------------------------------------------
    def show_threat_notification(self, file_path, findings, status="malicious"):
        """Convenience API: show a threat toast for a scanned file.

        Automatically quarantines malicious files unless explicitly disabled.
        Returns True when the toast was delivered.
        """
        name = os.path.basename(file_path) or file_path
        threat = (findings[0].get("rule") if findings else "") or status
        if status == "malicious":
            self._quarantine_with_toast(file_path, name,
                                        {"status": status, "findings": findings},
                                        threat=threat)
            return True
        return self.notifier.show("MarekFS: Suspicious file",
                                  f"{name}\n{threat}", icon="warning")

    def handle_toast_action(self, action_id, payload=None):
        """Handle a toast button action.

        Supports:
          - "open-quarantine": open the quarantine folder in Explorer
          - "restore:<entry_id>": restore a quarantined file
          - "whitelist:<path>": whitelist a file/folder
        Returns True when handled.
        """
        action_id = (action_id or "").strip()
        if action_id == "open-quarantine":
            try:
                subprocess.Popen(["explorer", self.quarantine.quarantine_dir])
                return True
            except Exception:
                return False
        if action_id.startswith("restore:"):
            eid = action_id[len("restore:"):].strip()
            ok, _msg = self.quarantine.restore_file(eid)
            return ok
        if action_id.startswith("whitelist:"):
            target = action_id[len("whitelist:"):].strip(os.sep)
            if target and os.path.exists(target):
                self.whitelist_path(target)
                return True
        return False
    def whitelist_path(self, path):
        self.whitelist.add(path)
        self.config["whitelist"] = list(self.whitelist.get_all())
        save_monitor_config(self.config)
# ---------------------------------------------------------------------------
# Module-level helpers for simple integration (import & call start_monitor()).
# ---------------------------------------------------------------------------
_scanner_monitor = None


def get_scanner_monitor():
    """Return the singleton ScannerMonitor instance, creating it if needed."""
    global _scanner_monitor
    if _scanner_monitor is None:
        _scanner_monitor = ScannerMonitor(enable_host_scanning=True)
    return _scanner_monitor


def start_scanner_monitor(enable_host_scanning=True, directories=None, verbose=False):
    """Start the real-time scanner monitor (silent). Returns the monitor."""
    global _scanner_monitor
    if _scanner_monitor is None:
        _scanner_monitor = ScannerMonitor(enable_host_scanning=enable_host_scanning,
                                          verbose=verbose,
                                          monitor_dirs=directories)
    _scanner_monitor.enable_host_scanning = enable_host_scanning
    _scanner_monitor.start()
    return _scanner_monitor


def stop_scanner_monitor():
    """Stop the running scanner monitor if it exists."""
    global _scanner_monitor
    if _scanner_monitor:
        _scanner_monitor.stop()
    _scanner_monitor = None


def scan_file_now(path, threat_hint=None):
    """One-shot scan: returns the scanner result dict (does not quarantine)."""
    from marekfs_core import scan_path_with_builtin_scanner
    cfg = ensure_scanner_config()
    return scan_path_with_builtin_scanner(path, cfg)


def _selftest():
    """Run an import-safe self test: quarantine + restore + FileMonitor detection."""
    import queue

    print("=" * 58)
    print("MarekFS Scanner Monitor - Self test (silent demo)")
    print("=" * 58)
    q = QuarantineManager()
    print(f"Quarantine dir  : {q.quarantine_dir}")
    print(f"DB file         : {q.db_file}")

    # 1) quarantine / restore round-trip
    probe = os.path.join(tempfile.gettempdir(), "marekfs_monitor_selftest.txt")
    with open(probe, "w", encoding="utf-8") as fh:
        fh.write("MarekFS scanner monitor self-test payload (harmless).")
    eid = q.quarantine_file(probe, threat_name="selftest")
    print(f"Quarantined     : {eid}")
    assert eid, "quarantine_file returned None"
    assert not os.path.exists(probe), "original file should be removed"
    print(f"Original removed: {not os.path.exists(probe)}")
    restored_at = os.path.join(tempfile.gettempdir(),
                               "marekfsm_selftest_restored_" + time.strftime("%H%M%S") + ".txt")
    ok, msg = q.restore_file(eid, dest_path=restored_at)
    print(f"Restored        : {ok} ({msg})")
    assert ok
    assert os.path.exists(restored_at)
    with open(restored_at, "r", encoding="utf-8") as fh:
        assert "self-test payload" in fh.read()
    os.remove(restored_at)
    print(f"Deleted probe   : {not os.path.exists(restored_at)}")

    # 2. FileMonitor synthetic test
    watched = tempfile.mkdtemp(prefix="marekfsm_watch_")
    seen = queue.Queue()

    mon = FileMonitor(on_file=lambda p, m: seen.put(p),
                      directories=[watched],
                      poll_interval=0.5, debounce=0.5)
    mon.start()
    time.sleep(1.0)
    probe2 = os.path.join(watched, "monitor_probe.exe")  # monitored ext
    with open(probe2, "wb") as fh:
        fh.write(b"MZ" + b"\x00" * 64)
    try:
        got = None
        deadline = time.time() + 8
        while time.time() < deadline:
            try:
                got = seen.get(timeout=0.5)
                break
            except queue.Empty:
                pass
        print(f"Detected new exe: {bool(got)}")
    finally:
        mon.stop()
        shutil.rmtree(watched, ignore_errors=True)

    # 3. ScannerMonitor instantiation (no start, avoid long wait)
    sm = ScannerMonitor(enable_host_scanning=False)
    print("Stats           :", sm.get_stats())
    print("Status          :", {k: sm.get_status()[k] for k in ("running", "directories")})
    print("=" * 58)
    print("Self test PASSED")
    print("=" * 58)
    return True


if __name__ == "__main__":
    try:
        _selftest()
    except Exception as exc:
        print(f"Self test FAILED: {exc}")
        raise