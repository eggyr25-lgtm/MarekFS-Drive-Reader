"""MarekFS manual snapshots.

Point-in-time, user-triggered snapshots for four scopes:

  * disk       — every file/folder currently loaded on the disk
  * partition  — every file/folder on the active partition
  * folder     — one folder and everything beneath it
  * file       — a single file

A snapshot is a portable ``.mareksnap`` container (a ZIP): a ``manifest.json``
describing every captured entry plus one payload blob per file under
``payloads/``. Snapshots capture the *logical decrypted* content, so a restore
recreates readable files through the normal MarekFS write path (journaled,
checksummed, RAM-cached). Encryption is intentionally NOT carried across a
restore — the user can re-encrypt afterwards — which keeps restore password
free and always openable.

Snapshots live in ProgramData/MarekFS/snapshots and can also be exported to /
imported from anywhere for backup or transfer between disks.
"""
import io
import os
import json
import time
import zipfile
import tkinter as tk
from tkinter import ttk, messagebox, simpledialog, filedialog

from ui_custom import theme_existing_window
from marekfs_core import (
    format_bytes, data_checksum, FILE_ATTR_DIRECTORY, FILE_ATTR_ENCRYPTED,
    PROGRAM_DATA_CONFIG_DIR,
)

SNAPSHOT_DIR = os.path.join(PROGRAM_DATA_CONFIG_DIR, "snapshots")
SNAPSHOT_EXT = ".mareksnap"
SNAPSHOT_FORMAT = "mareksnap"
SNAPSHOT_VERSION = 1

SCOPE_LABELS = {
    "disk": "Whole disk",
    "partition": "Partition",
    "folder": "Folder",
    "file": "File",
}


class SnapshotSkip(Exception):
    """Raised to skip a single entry during capture (e.g. password cancelled)."""


def _slug(text):
    keep = "-_.() "
    cleaned = "".join(c if (c.isalnum() or c in keep) else "_" for c in str(text))
    return cleaned.strip().strip(".")[:60] or "snapshot"


def ensure_snapshot_dir():
    os.makedirs(SNAPSHOT_DIR, exist_ok=True)
    return SNAPSHOT_DIR


def build_snapshot(entries, read_bytes, scope, target, drive="", partition_id="",
                   description="", partitions=None):
    """Build a ``.mareksnap`` archive in memory.

    Parameters
    ----------
    entries      : list[dict]  — file/folder records (filename, is_dir, size,
                   attributes, encrypted).
    read_bytes   : callable(record) -> bytes  — logical content for files. May
                   raise SnapshotSkip to omit a single file.
    scope/target : snapshot scope and its human-readable target.
    """
    manifest = {
        "format": SNAPSHOT_FORMAT,
        "version": SNAPSHOT_VERSION,
        "scope": scope,
        "target": target,
        "drive": drive,
        "partition_id": partition_id,
        "description": description or "",
        "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "created_local": time.strftime("%Y-%m-%d %H:%M:%S"),
        "entries": [],
        "partitions": partitions or [],
    }
    skipped = []
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        payload_index = 0
        for rec in entries:
            name = rec.get("filename", "")
            is_dir = bool(rec.get("is_dir") or (rec.get("attributes", 0) & FILE_ATTR_DIRECTORY))
            # Strip the encrypted flag: content is stored decrypted.
            attrs = int(rec.get("attributes", 0)) & ~FILE_ATTR_ENCRYPTED
            item = {
                "filename": name,
                "is_dir": is_dir,
                "attributes": attrs,
                "encrypted_original": bool(rec.get("encrypted")),
            }
            if is_dir:
                item["size"] = 0
                manifest["entries"].append(item)
                continue
            try:
                data = bytes(read_bytes(rec) or b"")
            except SnapshotSkip:
                skipped.append(name)
                continue
            payload_name = f"payloads/{payload_index:05d}.bin"
            zf.writestr(payload_name, data)
            item.update({
                "size": len(data),
                "checksum": data_checksum(data),
                "payload": payload_name,
            })
            manifest["entries"].append(item)
            payload_index += 1
        zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
    return out.getvalue(), manifest, skipped


def read_manifest(path):
    with zipfile.ZipFile(path, "r") as zf:
        manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
    if manifest.get("format") != SNAPSHOT_FORMAT:
        raise ValueError("Not a MarekFS snapshot container.")
    return manifest


def list_snapshots():
    """Return [{path, manifest, size, mtime}] for every stored snapshot."""
    ensure_snapshot_dir()
    rows = []
    for name in os.listdir(SNAPSHOT_DIR):
        if not name.lower().endswith(SNAPSHOT_EXT):
            continue
        path = os.path.join(SNAPSHOT_DIR, name)
        try:
            manifest = read_manifest(path)
            rows.append({
                "path": path,
                "manifest": manifest,
                "size": os.path.getsize(path),
                "mtime": os.path.getmtime(path),
            })
        except Exception:
            continue
    rows.sort(key=lambda r: r["mtime"], reverse=True)
    return rows


def save_snapshot_bytes(blob, scope, target):
    ensure_snapshot_dir()
    stamp = time.strftime("%Y%m%d_%H%M%S")
    fname = f"{stamp}_{scope}_{_slug(target)}{SNAPSHOT_EXT}"
    path = os.path.join(SNAPSHOT_DIR, fname)
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(blob)
    os.replace(tmp, path)
    return path


def iter_payloads(path):
    """Yield (entry, data) for every file entry; dirs yield (entry, None)."""
    with zipfile.ZipFile(path, "r") as zf:
        manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
        for entry in manifest.get("entries", []):
            if entry.get("is_dir") or not entry.get("payload"):
                yield entry, None
            else:
                yield entry, zf.read(entry["payload"])


class SnapshotWindow:
    """Create, restore, export and delete manual MarekFS snapshots."""

    def __init__(self, app):
        self.app = app
        self.win = tk.Toplevel(app.root)
        theme_existing_window(self.win, app.root, title="📸 MarekFS Snapshots")
        self.win.title("📸 MarekFS Snapshots")
        self.win.geometry("980x600")
        self._pw_cache = {}
        self._build_ui()
        self.refresh()

    # ── UI ────────────────────────────────────────────────────────────────
    def _build_ui(self):
        header = ttk.Frame(self.win, padding=10)
        header.pack(fill=tk.X)
        ttk.Label(header, text="Manual Snapshots", style="Title.TLabel").pack(side=tk.LEFT)
        ttk.Label(
            header,
            text="Point-in-time copies you trigger yourself. Restores recreate readable files.",
            wraplength=560,
        ).pack(side=tk.LEFT, padx=12)

        create = ttk.LabelFrame(self.win, text=" Create a snapshot ", padding=8)
        create.pack(fill=tk.X, padx=10, pady=(0, 6))
        ttk.Button(create, text="💽 Whole Disk", style="Accent.TButton",
                   command=lambda: self.create_snapshot("disk")).pack(side=tk.LEFT, padx=4)
        ttk.Button(create, text="🧩 This Partition",
                   command=lambda: self.create_snapshot("partition")).pack(side=tk.LEFT, padx=4)
        ttk.Button(create, text="📁 Selected Folder",
                   command=lambda: self.create_snapshot("folder")).pack(side=tk.LEFT, padx=4)
        ttk.Button(create, text="📄 Selected File",
                   command=lambda: self.create_snapshot("file")).pack(side=tk.LEFT, padx=4)
        ttk.Button(create, text="📥 Import…", command=self.import_snapshot).pack(side=tk.RIGHT, padx=4)

        body = ttk.Frame(self.win, padding=(10, 0))
        body.pack(fill=tk.BOTH, expand=True)
        cols = ("Scope", "Target", "Created", "Items", "Size", "Disk")
        self.tree = ttk.Treeview(body, columns=cols, show="headings", selectmode="browse")
        for c, w in zip(cols, (110, 300, 160, 70, 100, 180)):
            self.tree.heading(c, text=c)
            self.tree.column(c, width=w)
        sb = ttk.Scrollbar(body, command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sb.pack(side=tk.RIGHT, fill=tk.Y)

        self.replace_var = tk.BooleanVar(value=False)
        foot = ttk.Frame(self.win, padding=10)
        foot.pack(fill=tk.X)
        ttk.Button(foot, text="♻️ Restore", style="Accent.TButton",
                   command=self.restore_selected).pack(side=tk.LEFT, padx=4)
        ttk.Checkbutton(foot, text="Wipe scope before restoring (delete matching files first)",
                        variable=self.replace_var).pack(side=tk.LEFT, padx=8)
        ttk.Button(foot, text="📤 Export…", command=self.export_selected).pack(side=tk.RIGHT, padx=4)
        ttk.Button(foot, text="🗑️ Delete", command=self.delete_selected).pack(side=tk.RIGHT, padx=4)

        self.status = tk.StringVar(value="")
        ttk.Label(self.win, textvariable=self.status, padding=(12, 0, 12, 8)).pack(anchor=tk.W)

    # ── data helpers ────────────────────────────────────────────────────────
    def _active_partition_id(self):
        try:
            parts = self.app.partitions
            return parts[self.app.active_partition_index]["id"] if parts else ""
        except Exception:
            return ""

    def _read_record_bytes(self, rec):
        """Logical bytes for a record, prompting once per password for
        encrypted files (cached for the rest of this capture)."""
        password = ""
        if rec.get("encrypted"):
            key = rec["filename"]
            if key in self._pw_cache:
                password = self._pw_cache[key]
            else:
                password = simpledialog.askstring(
                    "Password Required",
                    f"🔒 Password to snapshot:\n{rec['filename']}",
                    show="*", parent=self.win)
                if password is None:
                    raise SnapshotSkip()
                self._pw_cache[key] = password
        return self.app._read_file_bytes(rec, password)

    def _entries_for_scope(self, scope):
        """Return (entries, target) for the chosen scope, or (None, None)."""
        files = self.app.files_data
        if scope in ("disk", "partition"):
            target = ("*" if scope == "disk"
                      else self._active_partition_id() or "partition")
            return list(files), target
        if scope == "folder":
            rel, full, _ = self.app._selected_record(need_file=False)
            folder = full if full else self.app.current_folder
            if not folder:
                messagebox.showinfo("Snapshot",
                                    "Select a folder (or open one) first.", parent=self.win)
                return None, None
            subset = [r for r in files
                      if r["filename"] == folder or r["filename"].startswith(folder + "/")]
            if not subset:
                messagebox.showinfo("Snapshot", "That folder is empty.", parent=self.win)
                return None, None
            return subset, folder
        if scope == "file":
            rel, full, rec = self.app._selected_record(need_file=True)
            if not rec:
                messagebox.showinfo("Snapshot", "Select a single file first.", parent=self.win)
                return None, None
            return [rec], full
        return None, None

    # ── actions ──────────────────────────────────────────────────────────────
    def create_snapshot(self, scope):
        entries, target = self._entries_for_scope(scope)
        if entries is None:
            return
        description = simpledialog.askstring(
            "Snapshot label",
            f"Optional note for this {SCOPE_LABELS[scope].lower()} snapshot:",
            parent=self.win) or ""
        self._pw_cache.clear()
        self.status.set("Capturing…")
        self.win.update_idletasks()
        try:
            blob, manifest, skipped = build_snapshot(
                entries, self._read_record_bytes, scope, target,
                drive=self.app.drive_path, partition_id=self._active_partition_id(),
                description=description,
                partitions=[dict(p) for p in getattr(self.app, "partitions", [])],
            )
            path = save_snapshot_bytes(blob, scope, target)
        except Exception as e:
            self.status.set("")
            messagebox.showerror("Snapshot failed", str(e), parent=self.win)
            return
        self.refresh()
        note = f" · skipped {len(skipped)} locked file(s)" if skipped else ""
        self.status.set(f"✔ Saved {os.path.basename(path)} "
                        f"({len(manifest['entries'])} items, {format_bytes(len(blob))}){note}")

    def _selected_path(self):
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo("Snapshot", "Select a snapshot from the list.", parent=self.win)
            return None
        return self.tree.item(sel[0])["tags"][0]

    def restore_selected(self):
        path = self._selected_path()
        if not path:
            return
        try:
            manifest = read_manifest(path)
        except Exception as e:
            messagebox.showerror("Snapshot", str(e), parent=self.win)
            return
        scope = manifest.get("scope", "file")
        count = len(manifest.get("entries", []))
        wipe = self.replace_var.get()
        msg = (f"Restore this {SCOPE_LABELS.get(scope, scope).lower()} snapshot?\n\n"
               f"Target: {manifest.get('target', '?')}\n"
               f"Items: {count}\n"
               f"Created: {manifest.get('created_local', manifest.get('created', '?'))}\n\n"
               + ("Files matching the snapshot scope will be DELETED first, then "
                  "recreated from the snapshot.\n" if wipe else
                  "Existing files with the same names will be overwritten; other "
                  "files are left untouched.\n")
               + "Restored files are recreated UNENCRYPTED.")
        if not messagebox.askyesno("Confirm restore", msg, parent=self.win):
            return

        self.status.set("Restoring…")
        self.win.update_idletasks()
        try:
            restored, made_dirs = self._do_restore(path, manifest, wipe)
        except Exception as e:
            self.status.set("")
            messagebox.showerror("Restore failed", str(e), parent=self.win)
            return
        self.status.set(f"✔ Restored {restored} file(s) and {made_dirs} folder(s).")
        messagebox.showinfo("Restore complete",
                            f"Restored {restored} file(s) and {made_dirs} folder(s) "
                            f"from the snapshot.", parent=self.win)

    def _do_restore(self, path, manifest, wipe):
        app = self.app
        # Optional wipe of the snapshot's scope.
        if wipe:
            targets = set()
            for entry in manifest.get("entries", []):
                targets.add(entry["filename"])
            for name in list(targets):
                if any(r["filename"] == name for r in app.files_data):
                    try:
                        app.delete_file_by_path(name)
                    except Exception:
                        pass

        made_dirs = 0
        restored = 0
        # Directories first so parents exist before files land in them.
        for entry, data in iter_payloads(path):
            name = entry["filename"]
            if entry.get("is_dir"):
                if self._ensure_dir(name):
                    made_dirs += 1
                continue
            made_dirs += self._ensure_parents(name)
            # Overwrite: remove an existing record at this path first.
            if any(r["filename"] == name for r in app.files_data):
                try:
                    app.delete_file_by_path(name)
                except Exception:
                    pass
            attrs = int(entry.get("attributes", 0)) & ~FILE_ATTR_ENCRYPTED
            app.create_entry(name, data or b"", attrs, is_dir=False)
            restored += 1
        return restored, made_dirs

    def _ensure_dir(self, path):
        if any(r["filename"] == path and (r.get("is_dir") or
               (r.get("attributes", 0) & FILE_ATTR_DIRECTORY)) for r in self.app.files_data):
            return False
        self._ensure_parents(path)
        self.app.create_entry(path, b"", FILE_ATTR_DIRECTORY, is_dir=True)
        return True

    def _ensure_parents(self, path):
        made = 0
        parts = path.split("/")[:-1]
        cur = ""
        for p in parts:
            cur = f"{cur}/{p}" if cur else p
            exists = any(r["filename"] == cur and (r.get("is_dir") or
                         (r.get("attributes", 0) & FILE_ATTR_DIRECTORY))
                         for r in self.app.files_data)
            if not exists:
                self.app.create_entry(cur, b"", FILE_ATTR_DIRECTORY, is_dir=True)
                made += 1
        return made

    def export_selected(self):
        path = self._selected_path()
        if not path:
            return
        dest = filedialog.asksaveasfilename(
            parent=self.win, defaultextension=SNAPSHOT_EXT,
            initialfile=os.path.basename(path),
            filetypes=[("MarekFS snapshot", "*" + SNAPSHOT_EXT)])
        if not dest:
            return
        try:
            with open(path, "rb") as src, open(dest, "wb") as out:
                out.write(src.read())
            self.status.set(f"✔ Exported to {dest}")
        except Exception as e:
            messagebox.showerror("Export failed", str(e), parent=self.win)

    def import_snapshot(self):
        src = filedialog.askopenfilename(
            parent=self.win, filetypes=[("MarekFS snapshot", "*" + SNAPSHOT_EXT)])
        if not src:
            return
        try:
            read_manifest(src)  # validate
            ensure_snapshot_dir()
            dest = os.path.join(SNAPSHOT_DIR, os.path.basename(src))
            base, ext = os.path.splitext(dest)
            n = 1
            while os.path.exists(dest):
                dest = f"{base}_{n}{ext}"
                n += 1
            with open(src, "rb") as s, open(dest, "wb") as o:
                o.write(s.read())
            self.refresh()
            self.status.set(f"✔ Imported {os.path.basename(dest)}")
        except Exception as e:
            messagebox.showerror("Import failed", str(e), parent=self.win)

    def delete_selected(self):
        path = self._selected_path()
        if not path:
            return
        if not messagebox.askyesno("Delete snapshot",
                                   f"Delete snapshot file:\n{os.path.basename(path)}?",
                                   parent=self.win):
            return
        try:
            os.remove(path)
        except Exception as e:
            messagebox.showerror("Delete failed", str(e), parent=self.win)
            return
        self.refresh()
        self.status.set("Snapshot deleted.")

    def refresh(self):
        for i in self.tree.get_children():
            self.tree.delete(i)
        for row in list_snapshots():
            m = row["manifest"]
            entries = m.get("entries", [])
            file_count = sum(1 for e in entries if not e.get("is_dir"))
            self.tree.insert(
                "", tk.END,
                values=(
                    SCOPE_LABELS.get(m.get("scope"), m.get("scope", "?")),
                    m.get("target", "?"),
                    m.get("created_local", m.get("created", "?")),
                    f"{file_count}/{len(entries)}",
                    format_bytes(row["size"]),
                    os.path.basename(m.get("drive", "")) or m.get("partition_id", ""),
                ),
                tags=(row["path"],),
            )
