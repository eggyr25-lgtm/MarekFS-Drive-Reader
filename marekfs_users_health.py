"""MarekFS disk users, per-file access metadata, and disk health reporting."""
import hashlib
import json
import os
import platform
import secrets
import shutil
import subprocess
import time
import tkinter as tk
from tkinter import messagebox
from tkinter import ttk

from ui_custom import theme_existing_window

APP_DIR = os.path.join(os.environ.get("PROGRAMDATA", os.path.expanduser("~")), "MarekFS")
USERS_PATH = os.path.join(APP_DIR, "disk_users.json")
PBKDF2_ROUNDS = 240_000


def _key_for_drive(path):
    return os.path.abspath(path).lower()


class DiskUserStore:
    def __init__(self, drive_path):
        self.drive_key = _key_for_drive(drive_path)
        self.data = self._load()

    def _load(self):
        try:
            with open(USERS_PATH, "r", encoding="utf-8") as f:
                root = json.load(f)
            return root.get(self.drive_key, {"users": {}}) if isinstance(root, dict) else {"users": {}}
        except Exception:
            return {"users": {}}

    def _save(self):
        os.makedirs(APP_DIR, exist_ok=True)
        try:
            with open(USERS_PATH, "r", encoding="utf-8") as f: root = json.load(f)
        except Exception:
            root = {}
        root[self.drive_key] = self.data
        tmp = USERS_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(root, f, ensure_ascii=False, indent=2)
        os.replace(tmp, USERS_PATH)

    @staticmethod
    def _password_record(password, salt=None):
        salt = salt or secrets.token_bytes(16)
        digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ROUNDS, 32)
        return {"salt": salt.hex(), "verifier": digest.hex(), "rounds": PBKDF2_ROUNDS}

    def list_users(self):
        return sorted(self.data.setdefault("users", {}).keys(), key=str.casefold)

    def create_user(self, name, password):
        name = str(name).strip()
        if not name or len(name) > 80 or any(c in name for c in "\\/\x00"):
            raise ValueError("User name must be 1–80 characters and cannot contain path separators.")
        if not password or len(password) < 8:
            raise ValueError("Use a password with at least 8 characters.")
        if name in self.data.setdefault("users", {}):
            raise ValueError("That user already exists.")
        self.data["users"][name] = {**self._password_record(password), "created": time.time()}
        self._save()
        return name

    def verify(self, name, password):
        record = self.data.get("users", {}).get(name)
        if not record: return False
        salt = bytes.fromhex(record["salt"])
        actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, int(record.get("rounds", PBKDF2_ROUNDS)), 32).hex()
        return secrets.compare_digest(actual, record.get("verifier", ""))


def permission_allows(metadata, username):
    access = metadata.get("access", {}) if isinstance(metadata, dict) else {}
    if not isinstance(access, dict):
        return True
    if access.get("mode", "all") == "all": return True
    users = access.get("users", [])
    if not isinstance(users, (list, tuple, set)):
        return True
    return bool(username) and username in set(str(user) for user in users)


def set_file_permission(metadata, mode, users):
    metadata["access"] = {"mode": "users" if mode == "users" else "all", "users": sorted(set(users)) if mode == "users" else []}
    return metadata


def collect_disk_health(path):
    """Return honest CrystalDiskInfo-style fields; unavailable values stay Unknown."""
    result = {"model": "Unknown", "serial": "Unknown", "firmware": "Unknown", "capacity": "Unknown", "health": "Unknown", "temperature": "Unknown", "reallocated_sectors": "Unknown", "power_on_hours": "Unknown", "source": "Unavailable"}
    try:
        result["capacity"] = f"{os.path.getsize(path) / (1024 ** 3):.2f} GiB"
    except Exception: pass
    if not str(path).startswith("\\\\.\\"):
        result.update({"model": os.path.basename(path) or "MarekFS virtual image", "health": "Healthy (virtual image)", "source": "File image; SMART unavailable"})
        return result
    if platform.system() == "Windows":
        smartctl = shutil.which("smartctl")
        if smartctl:
            try:
                proc = subprocess.run([smartctl, "-a", path], capture_output=True, text=True, timeout=12, check=False)
                text = proc.stdout + "\n" + proc.stderr
                patterns = {"model": r"Device Model:\s*(.+)", "serial": r"Serial Number:\s*(.+)", "firmware": r"Firmware Version:\s*(.+)", "temperature": r"Temperature_Celsius.*?\s(\d+)\s*$", "reallocated_sectors": r"Reallocated_Sector_Ct.*?\s(\d+)\s*$", "power_on_hours": r"Power_On_Hours.*?\s(\d+)\s*$"}
                import re
                for key, pattern in patterns.items():
                    match = re.search(pattern, text, re.M)
                    if match: result[key] = match.group(1).strip()
                result["health"] = "Good" if "PASSED" in text.upper() else "Needs review"
                result["source"] = "smartctl"
            except Exception: pass
    return result


class DiskUsersWindow:
    def __init__(self, parent, store, on_login=None):
        self.store, self.on_login = store, on_login
        self.win = tk.Toplevel(parent); theme_existing_window(self.win, parent, title="👥 MarekFS Disk Users"); self.win.geometry("560x430")
        ttk.Label(self.win, text="Disk users", style="Title.TLabel").pack(anchor=tk.W, padx=12, pady=10)
        body = ttk.Frame(self.win, padding=10); body.pack(fill=tk.BOTH, expand=True)
        self.users = tk.Listbox(body, height=10); self.users.pack(fill=tk.BOTH, expand=True)
        form = ttk.Frame(body); form.pack(fill=tk.X, pady=8)
        self.name = tk.StringVar(); self.password = tk.StringVar()
        ttk.Label(form, text="User").grid(row=0, column=0, sticky="w"); ttk.Entry(form, textvariable=self.name).grid(row=0, column=1, sticky="ew")
        ttk.Label(form, text="Password").grid(row=1, column=0, sticky="w"); ttk.Entry(form, textvariable=self.password, show="*").grid(row=1, column=1, sticky="ew")
        form.columnconfigure(1, weight=1)
        buttons = ttk.Frame(body); buttons.pack(fill=tk.X)
        ttk.Button(buttons, text="Create user", style="Accent.TButton", command=self.create).pack(side=tk.LEFT, padx=3)
        ttk.Button(buttons, text="Log in selected", command=self.login).pack(side=tk.LEFT, padx=3)
        ttk.Button(buttons, text="Close", command=self.win.destroy).pack(side=tk.RIGHT, padx=3)
        self.refresh()
    def refresh(self):
        self.users.delete(0, tk.END)
        for name in self.store.list_users(): self.users.insert(tk.END, name)
    def create(self):
        try: self.store.create_user(self.name.get(), self.password.get()); self.name.set(""); self.password.set(""); self.refresh()
        except Exception as e: messagebox.showerror("Create user", str(e), parent=self.win)
    def login(self):
        sel = self.users.curselection()
        if not sel: return
        name = self.users.get(sel[0])
        if self.store.verify(name, self.password.get()):
            if self.on_login: self.on_login(name)
            self.win.destroy()
        else: messagebox.showerror("Login failed", "The password is incorrect.", parent=self.win)


class DiskHealthWindow:
    def __init__(self, parent, drive_path):
        self.drive_path = drive_path; self.win = tk.Toplevel(parent); theme_existing_window(self.win, parent, title="💽 MarekFS Disk Health"); self.win.geometry("620x500")
        ttk.Label(self.win, text="Disk health / SMART-style information", style="Title.TLabel").pack(anchor=tk.W, padx=12, pady=10)
        self.out = tk.Text(self.win, state="disabled"); self.out.pack(fill=tk.BOTH, expand=True, padx=12, pady=8)
        ttk.Button(self.win, text="Refresh", command=self.refresh).pack(pady=8); self.refresh()
    def refresh(self):
        info = collect_disk_health(self.drive_path); self.out.configure(state="normal"); self.out.delete("1.0", tk.END)
        for key, value in info.items(): self.out.insert(tk.END, f"{key.replace('_', ' ').title()}: {value}\n")
        self.out.configure(state="disabled")
