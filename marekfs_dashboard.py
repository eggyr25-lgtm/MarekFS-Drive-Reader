"""MarekFS Disks Dashboard: lists MarekFS disk images in a folder, showing
each disk's size (bytes + human: KB/MB/GB/TB/PB), detected file count, and
the 64-bit theoretical entry limit."""
import os
import struct
import threading
import tkinter as tk
from tkinter import ttk, filedialog
from tkinter.font import Font

from ui_custom import theme_existing_window

from marekfs_core import (
        format_bytes, MAX_FILE_COUNT, DEFAULT_DIR_START_SECTOR,
    SECTOR_SIZE, DIRECTORY_ENTRY_SIZE, FILENAME_MAX_LEN,
    open_drive, read_sectors,
)


def _human_breakdown(size):
    """Return a string showing size in bytes plus KB/MB/GB/TB/PB as applicable."""
    units = [("B", 1), ("KB", 1024), ("MB", 1024**2), ("GB", 1024**3),
             ("TB", 1024**4), ("PB", 1024**5)]
    parts = [f"{size:,} B"]
    for name, div in units[1:]:
        if size >= div:
            parts.append(f"{size/div:,.2f} {name}")
    return " | ".join(parts)


def _count_files(drive_path):
    """Best-effort count of non-empty directory entries in a MarekFS image."""
    try:
        fd = open_drive(drive_path, read_write=False)
        try:
                dir_sectors = (MAX_FILE_COUNT and 0) or 0  # noop
                # use the default pre-sized directory count
                from marekfs_core import DEFAULT_DIR_SECTORS_COUNT
                data = read_sectors(fd, DEFAULT_DIR_START_SECTOR, DEFAULT_DIR_SECTORS_COUNT)
        finally:
            os.close(fd)
        total = len(data) // DIRECTORY_ENTRY_SIZE
        count = 0
        for i in range(total):
            off = i * DIRECTORY_ENTRY_SIZE
            if off + 2 > len(data): break
            name_len = struct.unpack("<H", data[off:off+2])[0]
            if 0 < name_len <= FILENAME_MAX_LEN:
                count += 1
        return count
    except Exception:
        return None


class DashboardWindow:
    def __init__(self, parent):
        self.win = tk.Toplevel(parent)
        theme_existing_window(self.win, parent, title="🗄️ MarekFS Disks Dashboard")
        self.win.title("🗄️ MarekFS Disks Dashboard")
        self.win.geometry("1080x560")
        self.folder = tk.StringVar(value=os.getcwd())
        top = ttk.Frame(self.win, padding=8); top.pack(fill=tk.X)
        ttk.Label(top, text="Folder:").pack(side=tk.LEFT)
        ttk.Entry(top, textvariable=self.folder, width=70).pack(side=tk.LEFT, padx=6)
        ttk.Button(top, text="Browse…", command=self._browse).pack(side=tk.LEFT, padx=4)
        ttk.Button(top, text="🔄 Refresh", style="Accent.TButton", command=self.refresh).pack(side=tk.LEFT, padx=4)
        info = ttk.Frame(self.win, padding=4); info.pack(fill=tk.X)
        self.summary = tk.StringVar(value="")
        ttk.Label(info, textvariable=self.summary, font=("Segoe UI", 10, "bold")).pack(anchor=tk.W)
        body = ttk.Frame(self.win); body.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)
        cols = ("Disk", "Path", "Size (bytes + units)", "Files found", "Max entries (64-bit)")
        self.tree = ttk.Treeview(body, columns=cols, show="headings")
        for c, w in zip(cols, (220, 360, 300, 90, 300)):
            self.tree.heading(c, text=c); self.tree.column(c, width=w)
        sb = ttk.Scrollbar(body, command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        self.refresh()

    def _browse(self):
        d = filedialog.askdirectory(initialdir=self.folder.get())
        if d:
            self.folder.set(d)
            self.refresh()

    def refresh(self):
        for i in self.tree.get_children(): self.tree.delete(i)
        folder = self.folder.get()
        if not os.path.isdir(folder):
            self.summary.set("Invalid folder")
            return
        disks = []
        for f in os.listdir(folder):
            if f.lower().endswith((".img", ".marekfs", ".bin")) or f.lower().endswith(".marekarchv"):
                disks.append(os.path.join(folder, f))
        total = 0
        max_files_str = f"{MAX_FILE_COUNT:,}"
        for path in disks:
            try:
                size = os.path.getsize(path)
            except Exception:
                size = 0
            total += size
            self.tree.insert("", tk.END, values=(
                os.path.basename(path), path, _human_breakdown(size),
                "…", max_files_str,
            ))
        # count files in background
        for idx, path in enumerate(disks):
            threading.Thread(target=self._count_and_update, args=(path, idx), daemon=True).start()
        self.summary.set(f"Disks: {len(disks)} | Total size: {_human_breakdown(total)} | Theoretical max per disk: {max_files_str} files/folders (64-bit)")

    def _count_and_update(self, path, idx):
        count = _count_files(path)
        if count is None:
            disp = "—"
        else:
            disp = f"{count:,}"
        def upd():
            try:
                children = self.tree.get_children()
                if idx < len(children):
                    vals = list(self.tree.item(children[idx])['values'])
                    if len(vals) >= 4:
                        vals[3] = disp
                        self.tree.item(children[idx], values=vals)
            except Exception:
                pass
        self.win.after(0, upd)
