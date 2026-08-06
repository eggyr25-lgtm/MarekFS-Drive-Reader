"""MarekFS .extension package manager.

Extensions are declarative ZIP packages. They may add/replace files under the
MarekFS extension-data root, but their code is never executed by this manager.
Every write is backed up before installation and can be rolled back.
"""
import base64
import hashlib
import json
import os
import shutil
import tempfile
import time
import zipfile
import re
import hmac
import tkinter as tk
from tkinter import filedialog, messagebox
from tkinter import ttk

from ui_custom import theme_existing_window

try:
    from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
except Exception:
    ChaCha20Poly1305 = None


APP_DIR = os.path.join(os.environ.get("PROGRAMDATA", os.path.expanduser("~")), "MarekFS")
EXTENSION_DATA_ROOT = os.path.join(APP_DIR, "extension_data")
EXTENSION_BACKUP_ROOT = os.path.join(APP_DIR, "extension_backups")
EXTENSION_STATE_PATH = os.path.join(APP_DIR, "extensions.json")
APPROVER_AAD = b"MarekFS-approver-v1"
# Public encrypted verifier. The approval secret is never stored in plaintext.
APPROVER_VERIFIER = {
    "version": 1,
    "kdf": "PBKDF2-HMAC-SHA256",
    "iterations": 300000,
    "salt": "y2uAV5f1YhGtqkuUL+bkIA==",
    "nonce": "jeWZtU8lr1vKt9ly",
    "ciphertext": "z/pbSXxSqwFPZJeHOeuEnOs9EyRJeZxpX0pebHNRVheE9Jiq6qR4n3LPyA==",
    "token_sha256": "81f6d4f835e7ca786081a8d9c7edfc4662b68803bcf09a68f542c6017dc077eb",
}

ALLOWED_PERMISSIONS = {"read_files", "write_files", "create_files", "theme", "browser_features"}
WRITE_PERMISSIONS = {"write_files", "create_files", "theme", "browser_features"}


def verify_approver_key(candidate):
    """Verify the creator approval key through ChaCha20-Poly1305 decryption.

    This intentionally does not compare the entered value with a plaintext
    constant. If cryptography is unavailable, approval fails closed.
    """
    if ChaCha20Poly1305 is None or not isinstance(candidate, str) or not candidate:
        return False
    try:
        salt = base64.b64decode(APPROVER_VERIFIER["salt"])
        nonce = base64.b64decode(APPROVER_VERIFIER["nonce"])
        packed = base64.b64decode(APPROVER_VERIFIER["ciphertext"])
        key = hashlib.pbkdf2_hmac("sha256", candidate.encode("utf-8"), salt,
                                  APPROVER_VERIFIER["iterations"], 32)
        plaintext = ChaCha20Poly1305(key).decrypt(nonce, packed, APPROVER_AAD)
        actual = hashlib.sha256(plaintext).hexdigest().encode("ascii")
        expected = APPROVER_VERIFIER["token_sha256"].encode("ascii")
        return hmac.compare_digest(actual, expected)
    except Exception:
        return False


def _load_state():
    try:
        with open(EXTENSION_STATE_PATH, "r", encoding="utf-8") as f:
            state = json.load(f)
        return state if isinstance(state, dict) else {"installed": {}}
    except Exception:
        return {"installed": {}}


def _save_state(state):
    os.makedirs(APP_DIR, exist_ok=True)
    tmp = EXTENSION_STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, EXTENSION_STATE_PATH)


def _safe_member_path(root, member):
    if not member or member.startswith(("/", "\\")):
        raise ValueError("Extension contains an absolute path.")
    normalized = member.replace("\\", "/")
    if any(part in ("", ".", "..") for part in normalized.split("/")):
        raise ValueError(f"Unsafe extension path: {member}")
    target = os.path.realpath(os.path.join(root, *normalized.split("/")))
    root_real = os.path.realpath(root)
    if os.path.commonpath([root_real, target]) != root_real:
        raise ValueError(f"Extension path escapes the allowed root: {member}")
    return target


def inspect_extension(path):
    if not path.lower().endswith(".extension"):
        raise ValueError("Select a .extension package.")
    with zipfile.ZipFile(path, "r") as package:
        try:
            manifest = json.loads(package.read("manifest.json").decode("utf-8"))
        except Exception as e:
            raise ValueError(f"Missing or invalid manifest.json: {e}")
        required = ("id", "name", "version", "permissions")
        missing = [key for key in required if key not in manifest]
        if missing:
            raise ValueError("Manifest missing: " + ", ".join(missing))
        extension_id = str(manifest.get("id", ""))
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", extension_id):
            raise ValueError("Manifest id must contain only letters, numbers, dot, underscore, or hyphen.")
        manifest["id"] = extension_id
        permissions = set(manifest.get("permissions", []))
        unknown = permissions - ALLOWED_PERMISSIONS
        if unknown:
            raise ValueError("Unknown extension permissions: " + ", ".join(sorted(unknown)))
        members = []
        for info in package.infolist():
            if info.is_dir() or info.filename == "manifest.json":
                continue
            if not info.filename.startswith("files/"):
                raise ValueError(f"Only files/ payloads are allowed: {info.filename}")
            relative = info.filename[len("files/"):]
            _safe_member_path(EXTENSION_DATA_ROOT, relative)
            if info.file_size > 64 * 1024 * 1024:
                raise ValueError(f"Extension file is too large: {info.filename}")
            members.append({"member": info.filename, "relative": relative, "size": info.file_size})
        manifest["permissions"] = sorted(permissions)
        return {"manifest": manifest, "members": members, "path": os.path.abspath(path)}


def backup_targets(package_info):
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    extension_id = package_info["manifest"]["id"]
    backup_dir = os.path.realpath(os.path.join(EXTENSION_BACKUP_ROOT, f"{extension_id}_{timestamp}"))
    backup_root = os.path.realpath(EXTENSION_BACKUP_ROOT)
    if os.path.commonpath([backup_root, backup_dir]) != backup_root:
        raise ValueError("Extension backup path escapes the backup root.")
    os.makedirs(backup_dir, exist_ok=True)
    backup_manifest = {"extension": extension_id, "created": timestamp, "files": []}
    for item in package_info["members"]:
        target = _safe_member_path(EXTENSION_DATA_ROOT, item["relative"])
        if os.path.isfile(target):
            backup_file = _safe_member_path(backup_dir, item["relative"])
            os.makedirs(os.path.dirname(backup_file), exist_ok=True)
            shutil.copy2(target, backup_file)
            backup_manifest["files"].append({"relative": item["relative"], "existed": True})
        else:
            backup_manifest["files"].append({"relative": item["relative"], "existed": False})
    with open(os.path.join(backup_dir, "backup.json"), "w", encoding="utf-8") as f:
        json.dump(backup_manifest, f, ensure_ascii=False, indent=2)
    return backup_dir


def _restore_backup(backup_dir):
    with open(os.path.join(backup_dir, "backup.json"), "r", encoding="utf-8") as f:
        info = json.load(f)
    for item in info.get("files", []):
        target = _safe_member_path(EXTENSION_DATA_ROOT, item["relative"])
        backup = _safe_member_path(backup_dir, item["relative"])
        if item.get("existed") and os.path.isfile(backup):
            os.makedirs(os.path.dirname(target), exist_ok=True)
            shutil.copy2(backup, target)
        elif os.path.exists(target):
            os.remove(target)


def install_extension(path, approver_key=None):
    package = inspect_extension(path)
    permissions = set(package["manifest"]["permissions"])
    if package["members"] and not verify_approver_key(approver_key or ""):
        raise PermissionError("Creator approval is required before any extension files are installed.")
    backup_dir = backup_targets(package)
    try:
        os.makedirs(EXTENSION_DATA_ROOT, exist_ok=True)
        with zipfile.ZipFile(path, "r") as source:
            for item in package["members"]:
                target = _safe_member_path(EXTENSION_DATA_ROOT, item["relative"])
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with source.open(item["member"], "r") as src, open(target, "wb") as dst:
                    shutil.copyfileobj(src, dst, length=1024 * 1024)
        state = _load_state()
        state.setdefault("installed", {})[package["manifest"]["id"]] = {
            "name": package["manifest"]["name"],
            "version": package["manifest"]["version"],
            "backup": backup_dir,
                "verified": bool(package["members"]),

            "installed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        _save_state(state)
        return {"manifest": package["manifest"], "backup": backup_dir,
                "verified_label": "Verified by MarekFS creator" if package["members"] else "Reviewed by MarekFS"}
    except Exception:
        _restore_backup(backup_dir)
        raise


def rollback_extension(extension_id):
    state = _load_state()
    record = state.get("installed", {}).get(extension_id)
    if not record or not record.get("backup"):
        raise ValueError("No backup is available for this extension.")
    _restore_backup(record["backup"])
    state["installed"].pop(extension_id, None)
    _save_state(state)
    return True


class ExtensionManagerWindow:
    def __init__(self, parent):
        self.win = tk.Toplevel(parent)
        theme_existing_window(self.win, parent, title="🧩 MarekFS Reader Extensions")
        self.win.geometry("760x520")
        self.win.title("🧩 MarekFS Reader Extensions")
        self.package = None
        top = ttk.Frame(self.win, padding=10); top.pack(fill=tk.X)
        ttk.Button(top, text="Open .extension", command=self.open_package).pack(side=tk.LEFT, padx=4)
        ttk.Button(top, text="Install approved", style="Accent.TButton", command=self.install).pack(side=tk.LEFT, padx=4)
        self.key = tk.StringVar()
        ttk.Label(top, text="Approver key:").pack(side=tk.LEFT, padx=(18, 4))
        ttk.Entry(top, textvariable=self.key, show="*", width=28).pack(side=tk.LEFT)
        body = ttk.Frame(self.win, padding=10); body.pack(fill=tk.BOTH, expand=True)
        self.info = tk.StringVar(value="No extension selected. Unapproved packages are never installed.")
        ttk.Label(body, textvariable=self.info, wraplength=700).pack(anchor=tk.W, pady=(0, 8))
        self.out = tk.Text(body, wrap=tk.WORD, height=20)
        self.out.pack(fill=tk.BOTH, expand=True)

    def open_package(self):
        path = filedialog.askopenfilename(parent=self.win, filetypes=[("MarekFS extension", "*.extension")])
        if not path: return
        try:
            self.package = inspect_extension(path)
            manifest = self.package["manifest"]
            self.info.set(f"{manifest['name']} {manifest['version']} · permissions: {', '.join(manifest['permissions'])}")
            self.out.delete("1.0", tk.END)
            self.out.insert(tk.END, json.dumps({"manifest": manifest, "files": self.package["members"]}, indent=2))
        except Exception as e:
            self.package = None
            messagebox.showerror("Extension", str(e), parent=self.win)

    def install(self):
        if not self.package:
            messagebox.showinfo("Extension", "Open an extension package first.", parent=self.win); return
        try:
            result = install_extension(self.package["path"], self.key.get())
            self.info.set(result["verified_label"] + " · installed with backup")
            messagebox.showinfo("Extension installed", result["verified_label"], parent=self.win)
        except Exception as e:
            messagebox.showerror("Extension blocked", str(e), parent=self.win)
