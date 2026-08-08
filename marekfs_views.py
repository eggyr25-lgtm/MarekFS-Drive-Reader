"""MarekFS view windows: editor, archive viewer, properties, DiskTest chart,
Visualize (raw sectors), MediaViewer (image/movie), and VirusScan."""
import os
import time
import atexit
import tempfile
import threading
import subprocess
import tkinter as tk
from tkinter import ttk, messagebox, simpledialog, filedialog
from tkinter.font import Font

from ui_custom import theme_existing_window, stable_widget_width

import shutil


def _locate_executable(name: str):
    """Locate an executable: try PATH, then search user's Documents/Downloads and common MinGW/MSYS folders.

    Returns full path or None.
    """
    # First, try shutil.which
    p = shutil.which(name)
    if p:
        return p

    # Prepare candidate directories to search (user convenience locations)
    candidates = []
    up = os.environ.get('USERPROFILE') or os.environ.get('HOME')
    if up:
        for sub in ('Documents', 'Downloads', 'Desktop'):
            candidates.append(os.path.join(up, sub))
        candidates.append(up)

    # Common MSYS/MinGW locations
    for base in (r"C:\msys64\mingw64\bin", r"C:\msys64\usr\bin", r"C:\mingw64\bin", r"C:\MinGW\bin"):
        candidates.append(base)

    # Search candidate dirs (non-recursive) for an executable
    pathext = os.environ.get('PATHEXT', '.EXE;.BAT;.CMD').split(';')
    names = [name]
    if os.name == 'nt' and not name.lower().endswith('.exe'):
        # try with PATHEXT variants
        names = [name + ext for ext in ['.exe', '.cmd', '.bat']]

    for d in candidates:
        try:
            if not d or not os.path.isdir(d):
                continue
            for nm in names:
                fp = os.path.join(d, nm)
                if os.path.isfile(fp) and os.access(fp, os.X_OK):
                    return fp
        except Exception:
            continue

    # As a last resort, do a shallow recursive search in Documents (depth-limited)
    try:
        doc = os.path.join(up, 'Documents') if up else None
        if doc and os.path.isdir(doc):
            for root, dirs, files in os.walk(doc):
                for nm in names:
                    if nm in files:
                        fp = os.path.join(root, nm)
                        if os.path.isfile(fp) and os.access(fp, os.X_OK):
                            return fp
                # limit depth by pruning dirs
                # stop if path depth exceeds 4 relative to doc
                rel = os.path.relpath(root, doc)
                if rel == '.':
                    depth = 0
                else:
                    depth = rel.count(os.sep) + 1
                if depth > 3:
                    del dirs[:]  # don't recurse deeper
    except Exception:
        pass

    return None

from marekfs_core import (
    format_bytes, create_marekfs_archive, parse_marekfs_archive,
    CACHE_START_SECTOR, CACHE_SECTORS, CACHE_MAGIC, CACHE_MAX_SIZE,
    DEFAULT_DIR_START_SECTOR, SECTOR_SIZE, DIRECTORY_ENTRY_SIZE, FILENAME_MAX_LEN,
    FILE_ATTR_DIRECTORY, FILE_ATTR_HIDDEN, FILE_ATTR_READONLY, FILE_ATTR_SYSTEM,
    FILE_ATTR_ARCHIVE, FILE_ATTR_COMPRESSED, FILE_ATTR_ENCRYPTED,
    IMAGE_EXTS, MOVIE_EXTS, open_drive, read_sectors, write_sectors,
    find_av_scanners, build_av_command,
    ensure_scanner_config, scan_bytes_with_builtin_scanner, update_scanner_rules,
    update_scanner_hashes, data_checksum,
    save_scanner_config, check_clamav_update, download_clamav_update,
)

_GLOBAL_EDITOR_INSTANCE = None


def emergency_crash_save():
    global _GLOBAL_EDITOR_INSTANCE
    try:
        if _GLOBAL_EDITOR_INSTANCE and getattr(_GLOBAL_EDITOR_INSTANCE, "is_open", False):
            content = _GLOBAL_EDITOR_INSTANCE.editor_box.get("1.0", "end-1c")
            with open("CRASH_RECOVERY.txt", "wb") as f:
                f.write(content.encode("utf-8"))
    except Exception:
        pass


atexit.register(emergency_crash_save)


def _surface(root):
    t = getattr(root, "_marekfs_theme", None)
    return (t or {}).get("surface", "#1e1e2e"), (t or {}).get("entry", "#0d1117"), (t or {}).get("fg", "#cdd6f4"), (t or {}).get("accent", "#00d2ff")


class ModernMarekFSArchiveViewer:
    def __init__(self, parent, archive_name, archive_dict, save_callback, on_close=None):
        self.archive_name = archive_name
        self.files = archive_dict if archive_dict is not None else {}
        self.save_callback = save_callback
        self.on_close = on_close
        self.win = tk.Toplevel(parent)
        theme_existing_window(self.win, parent, title=f"📦 MAREKARCHV Container Manager — {archive_name}")
        self.win.title(f"📦 MAREKARCHV Container Manager — {archive_name}")
        self.win.geometry("750x500")
        self.win.protocol("WM_DELETE_WINDOW", self._close)
        toolbar = ttk.Frame(self.win, padding=8); toolbar.pack(fill=tk.X)
        ttk.Button(toolbar, text="➕ Add File", command=self.add_file).pack(side=tk.LEFT, padx=4)
        ttk.Button(toolbar, text="📝 Edit File", command=self.edit_file).pack(side=tk.LEFT, padx=4)
        ttk.Button(toolbar, text="🗑️ Delete File", command=self.delete_file).pack(side=tk.LEFT, padx=4)
        ttk.Button(toolbar, text="💾 Save Archive", style="Accent.TButton", command=self.save_archive).pack(side=tk.RIGHT, padx=4)
        main_frame = ttk.Frame(self.win); main_frame.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)
        cols = ("Filename", "Size")
        self.tree = ttk.Treeview(main_frame, columns=cols, show="headings")
        self.tree.heading("Filename", text="Filename"); self.tree.heading("Size", text="Size")
        self.tree.column("Filename", width=450); self.tree.column("Size", width=150)
        self.tree.pack(fill=tk.BOTH, expand=True)
        self.tree.bind("<Double-1>", lambda e: self.edit_file())
        self.refresh_tree()

    def _close(self):
        if self.on_close: self.on_close()
        self.win.destroy()

    def refresh_tree(self):
        for i in self.tree.get_children(): self.tree.delete(i)
        for fname, content in self.files.items():
            self.tree.insert("", tk.END, values=(fname, format_bytes(len(content))))

    def add_file(self):
        fname = simpledialog.askstring("Add Inner File", "Enter internal file name:")
        if not fname: return
        content = simpledialog.askstring("File Content", "Enter initial text content:") or ""
        self.files[fname] = content.encode("utf-8")
        self.refresh_tree()

    def edit_file(self):
        sel = self.tree.selection()
        if not sel: return
        fname = self.tree.item(sel[0])['values'][0]
        raw = self.files[fname]
        text = raw.decode("utf-8", errors="ignore")
        def save_inner(new_text, *args):
            self.files[fname] = new_text.encode("utf-8")
            self.refresh_tree()
            messagebox.showinfo("Saved", f"Updated '{fname}' inside archive.")
        ModernMarekFSEditor(self.win, f"{self.archive_name} -> {fname}", text, save_inner)

    def delete_file(self):
        sel = self.tree.selection()
        if not sel: return
        fname = self.tree.item(sel[0])['values'][0]
        del self.files[fname]
        self.refresh_tree()

    def save_archive(self):
        new_binary = create_marekfs_archive(self.files)
        self.save_callback(new_binary)
        self._close()


class ModernMarekFSProperties:
    def __init__(self, parent, file_data, save_callback, description="", description_callback=None, access_mode="all", access_users=None, user_names=None, access_callback=None):
        self.file_data = file_data
        self.save_callback = save_callback
        self.description = description
        self.description_callback = description_callback
        self.access_mode = access_mode
        self.access_users = set(access_users or [])
        self.user_names = list(user_names or [])
        self.access_callback = access_callback
        self.win = tk.Toplevel(parent)
        theme_existing_window(self.win, parent, title=f"📊 Properties — {file_data['filename']}")
        self.win.title(f"📊 Properties — {file_data['filename']}")

        self.win.geometry("420x520")
        self.win.resizable(True, True)
        info_frame = ttk.LabelFrame(self.win, text=" Item Information ", padding=12)
        info_frame.pack(fill=tk.X, padx=15, pady=15)
        ttk.Label(info_frame, text=f"Filename: {file_data['filename']}").pack(anchor=tk.W, pady=2)
        ttk.Label(info_frame, text=f"Sector: {file_data['sector']}").pack(anchor=tk.W, pady=2)
        ttk.Label(info_frame, text=f"Size: {format_bytes(file_data['size'])} ({file_data['size']} bytes)").pack(anchor=tk.W, pady=2)
        ttk.Label(info_frame, text=f"Encrypted: {'Yes' if file_data['encrypted'] else 'No'}").pack(anchor=tk.W, pady=2)
        attr_frame = ttk.LabelFrame(self.win, text=" Attributes ", padding=12)
        attr_frame.pack(fill=tk.X, padx=15, pady=5)
        self.var_directory = tk.BooleanVar(value=bool(file_data['attributes'] & FILE_ATTR_DIRECTORY))
        self.var_hidden = tk.BooleanVar(value=bool(file_data['attributes'] & FILE_ATTR_HIDDEN))
        self.var_readonly = tk.BooleanVar(value=bool(file_data['attributes'] & FILE_ATTR_READONLY))
        self.var_system = tk.BooleanVar(value=bool(file_data['attributes'] & FILE_ATTR_SYSTEM))
        self.var_archive = tk.BooleanVar(value=bool(file_data['attributes'] & FILE_ATTR_ARCHIVE))
        self.var_compressed = tk.BooleanVar(value=bool(file_data['attributes'] & FILE_ATTR_COMPRESSED))
        ttk.Checkbutton(attr_frame, text="Folder / Directory", variable=self.var_directory).pack(anchor=tk.W, pady=2)
        ttk.Checkbutton(attr_frame, text="Hidden", variable=self.var_hidden).pack(anchor=tk.W, pady=2)
        ttk.Checkbutton(attr_frame, text="Read-only", variable=self.var_readonly).pack(anchor=tk.W, pady=2)
        ttk.Checkbutton(attr_frame, text="System", variable=self.var_system).pack(anchor=tk.W, pady=2)
        ttk.Checkbutton(attr_frame, text="Archive", variable=self.var_archive).pack(anchor=tk.W, pady=2)
        ttk.Checkbutton(attr_frame, text="Compressed", variable=self.var_compressed).pack(anchor=tk.W, pady=2)
        ttk.Label(self.win, text="Description").pack(anchor=tk.W, padx=15, pady=(8, 2))
        self.description_var = tk.StringVar(value=self.description)
        desc = ttk.Entry(self.win, textvariable=self.description_var, width=52)
        stable_widget_width(desc, 52).pack(fill=tk.X, padx=15)
        access = ttk.LabelFrame(self.win, text=" Access ", padding=10); access.pack(fill=tk.X, padx=15, pady=6)
        self.access_var = tk.StringVar(value=self.access_mode)
        ttk.Radiobutton(access, text="All disk users", variable=self.access_var, value="all").pack(anchor=tk.W)
        ttk.Radiobutton(access, text="Selected users", variable=self.access_var, value="users").pack(anchor=tk.W)
        self.user_list = tk.Listbox(access, selectmode=tk.MULTIPLE, height=min(5, max(2, len(self.user_names))))
        self.user_list.pack(fill=tk.X, pady=4)
        for index, name in enumerate(self.user_names):
            self.user_list.insert(tk.END, name)
            if name in self.access_users: self.user_list.selection_set(index)
        btn_frame = ttk.Frame(self.win); btn_frame.pack(fill=tk.X, padx=15, pady=20)
        ttk.Button(btn_frame, text="Apply & Save", style="Accent.TButton", command=self.apply_changes).pack(side=tk.RIGHT, padx=5)
        ttk.Button(btn_frame, text="Cancel", command=self.win.destroy).pack(side=tk.RIGHT, padx=5)

    def apply_changes(self):
        new_attrs = 0
        if self.var_directory.get(): new_attrs |= FILE_ATTR_DIRECTORY
        if self.var_hidden.get(): new_attrs |= FILE_ATTR_HIDDEN
        if self.var_readonly.get(): new_attrs |= FILE_ATTR_READONLY
        if self.var_system.get(): new_attrs |= FILE_ATTR_SYSTEM
        if self.var_archive.get(): new_attrs |= FILE_ATTR_ARCHIVE
        if self.var_compressed.get(): new_attrs |= FILE_ATTR_COMPRESSED
        if self.file_data['encrypted']: new_attrs |= FILE_ATTR_ENCRYPTED
        self.save_callback(new_attrs)
        if self.description_callback:
            self.description_callback(self.description_var.get())
        if self.access_callback:
            selected = [self.user_list.get(i) for i in self.user_list.curselection()]
            self.access_callback(self.access_var.get(), selected)
        self.win.destroy()


class ModernMarekFSEditor:
    def __init__(self, parent, filename, content, save_callback, attributes=0, is_encrypted=False, on_close=None, app=None):
        global _GLOBAL_EDITOR_INSTANCE
        self.active_filename = filename
        self.save_callback = save_callback
        self.attributes = attributes
        self.is_encrypted = is_encrypted
        self.is_open = True
        self.on_close = on_close
        _GLOBAL_EDITOR_INSTANCE = self
        self.win = tk.Toplevel(parent)
        theme_existing_window(self.win, parent, title=f"📝 MarekFS Text Editor — {filename[:40]}")
        self.win.title(f"📝 MarekFS Text Editor — {filename[:40]}")

        self.win.geometry("900x700")
        self.win.protocol("WM_DELETE_WINDOW", self._close)

        # Toolbar with file and run controls
        toolbar = ttk.Frame(self.win, padding=8); toolbar.pack(fill=tk.X)
        ttk.Button(toolbar, text="➕ New Tab", command=self._new_tab).pack(side=tk.LEFT, padx=4)
        ttk.Button(toolbar, text="📂 Open...", command=self._open_local).pack(side=tk.LEFT, padx=4)
        ttk.Button(toolbar, text="💾 Save", style="Accent.TButton", command=self.do_save).pack(side=tk.LEFT, padx=4)
        ttk.Button(toolbar, text="💾 Save As...", command=self._save_as_local).pack(side=tk.LEFT, padx=4)
        ttk.Button(toolbar, text="🗑 Close Tab", command=self._close_tab).pack(side=tk.LEFT, padx=4)

        ttk.Label(toolbar, text="Run:").pack(side=tk.LEFT, padx=(12,4))
        self._run_lang = tk.StringVar(value="python")
        lang_menu = ttk.Combobox(toolbar, textvariable=self._run_lang, values=("python","c","cpp","java","csharp"), width=8, state="readonly")
        lang_menu.pack(side=tk.LEFT, padx=4)
        ttk.Button(toolbar, text="▶ Run", command=self._run_current).pack(side=tk.LEFT, padx=4)

        # Preview window controls
        ttk.Label(toolbar, text="Preview:").pack(side=tk.LEFT, padx=(10,4))
        self._preview_show = tk.BooleanVar(value=False)
        ttk.Checkbutton(toolbar, text="Show", variable=self._preview_show).pack(side=tk.LEFT, padx=4)
        self._preview_corner = tk.StringVar(value="top-right")
        ttk.Combobox(toolbar, textvariable=self._preview_corner, values=("top-left","top-right","bottom-left","bottom-right"), width=10, state="readonly").pack(side=tk.LEFT, padx=4)
        self._preview_frac = tk.StringVar(value="1/4")
        ttk.Combobox(toolbar, textvariable=self._preview_frac, values=("1/2","1/3","1/4","1/8"), width=5, state="readonly").pack(side=tk.LEFT, padx=4)
        self._preview_res = tk.StringVar(value="720p")
        ttk.Combobox(toolbar, textvariable=self._preview_res, values=("360p","720p","1080p","1440p","4k"), width=6, state="readonly").pack(side=tk.LEFT, padx=4)

        # Notebook for tabs
        frame = ttk.Frame(self.win); frame.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)
        self.notebook = ttk.Notebook(frame)
        self.notebook.pack(fill=tk.BOTH, expand=True, side=tk.TOP)

        # Output pane
        out_frame = ttk.Frame(frame); out_frame.pack(fill=tk.X, side=tk.BOTTOM)
        ttk.Label(out_frame, text="Output:").pack(anchor=tk.W)
        self.output_box = tk.Text(out_frame, height=10, font=Font(family="Consolas", size=10), bg="#0d1117", fg="#c9d1d9")
        self.output_box.pack(fill=tk.X)

        # internal tab state: list of (tab_id, text_widget, local_path or None)
        self._tabs = []
        # create first tab with given content
        self._create_tab(title=filename, content=content, local_path=None)
        # optional reference to main app (used for sandbox exports)
        self.app = app

    def _close(self):
        self.is_open = False
        if self.on_close: self.on_close()
        self.win.destroy()

    # --- Tab management ---
    def _create_tab(self, title="untitled", content="", local_path=None):
        frame = ttk.Frame(self.notebook)
        text = tk.Text(frame, font=Font(family="Consolas", size=11), wrap=tk.WORD,
                       bg="#1e1e2e", fg="#cdd6f4", insertbackground="#ffffff")
        scroll = ttk.Scrollbar(frame, command=text.yview)
        text.configure(yscrollcommand=scroll.set)
        text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)
        text.insert("1.0", content)
        # Add the frame as a new notebook tab. `add` returns None, so use the
        # frame widget's string id as the tab identifier for internal tracking.
        self.notebook.add(frame, text=title)
        tab_id = str(frame)
        self._tabs.append((tab_id, text, local_path))
        try:
            self.notebook.select(tab_id)
        except Exception:
            pass

    def _create_preview_window(self):
        # Create and position a preview window according to toolbar settings
        frac_map = {"1/2": 0.5, "1/3": 1.0/3.0, "1/4": 0.25, "1/8": 0.125}
        res_map = {"360p": (640, 360), "720p": (1280, 720), "1080p": (1920, 1080), "1440p": (2560, 1440), "4k": (3840, 2160)}
        frac = frac_map.get(self._preview_frac.get(), 0.25)
        res_w, res_h = res_map.get(self._preview_res.get(), (1280, 720))
        screen_w = self.win.winfo_screenwidth()
        screen_h = self.win.winfo_screenheight()
        # Limit preview size by both resolution and fraction of screen
        max_w = int(screen_w * frac)
        max_h = int(screen_h * frac)
        w = min(res_w, max_w)
        h = min(res_h, max_h)
        corner = self._preview_corner.get() or "top-right"
        if corner == "top-left":
            x = 0; y = 0
        elif corner == "top-right":
            x = screen_w - w; y = 0
        elif corner == "bottom-left":
            x = 0; y = screen_h - h
        else:
            x = screen_w - w; y = screen_h - h
        # Create Toplevel preview
        try:
            if getattr(self, '_preview_win', None):
                try: self._preview_win.destroy()
                except Exception: pass
            self._preview_win = tk.Toplevel(self.win)
            theme_existing_window(self._preview_win, self.win)
            self._preview_win.title("Run Preview")
            self._preview_win.geometry(f"{w}x{h}+{x}+{y}")
            text = tk.Text(self._preview_win, height=10, font=Font(family="Consolas", size=10), bg="#0d1117", fg="#c9d1d9")
            text.pack(fill=tk.BOTH, expand=True)
            return text
        except Exception:
            return None

    def _get_current_tab(self):
        sel = self.notebook.select()
        for (tid, text, path) in self._tabs:
            if tid == sel:
                return tid, text, path
        return None, None, None

    def _new_tab(self):
        self._create_tab(title="untitled", content="", local_path=None)

    def _close_tab(self):
        sel = self.notebook.select()
        if not sel:
            return
        # remove from internal list
        self._tabs = [t for t in self._tabs if t[0] != sel]
        try:
            self.notebook.forget(sel)
        except Exception:
            pass

    def _open_local(self):
        from tkinter import filedialog
        path = filedialog.askopenfilename(parent=self.win)
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read()
            name = os.path.basename(path)
            self._create_tab(title=name, content=content, local_path=path)
        except Exception as e:
            messagebox.showerror("Open Failed", str(e))

    def _save_as_local(self):
        from tkinter import filedialog
        tid, text, local = self._get_current_tab()
        if text is None:
            return
        path = filedialog.asksaveasfilename(parent=self.win, defaultextension=".txt")
        if not path:
            return
        try:
            data = text.get("1.0", "end-1c").encode("utf-8")
            with open(path, "wb") as f:
                f.write(data)
            # update tab title and local path
            idx = self.notebook.index(tid)
            self.notebook.tab(idx, text=os.path.basename(path))
            for i, (ttid, ttext, tpath) in enumerate(self._tabs):
                if ttid == tid:
                    self._tabs[i] = (ttid, ttext, path)
                    break
        except Exception as e:
            messagebox.showerror("Save As Failed", str(e))

    def do_save(self):
        # Save current tab back to MarekFS via provided callback
        tid, text, local = self._get_current_tab()
        if text is None:
            return
        data = text.get("1.0", "end-1c")
        try:
            # preserve original attributes/encryption flags for the save
            # The original save_callback expects text (string), not bytes.
            self.save_callback(data, self.attributes, self.is_encrypted)
            messagebox.showinfo("Saved", "Saved to MarekFS")
        except Exception as e:
            messagebox.showerror("Save Failed", str(e))

    # --- Code execution ---
    def _run_current(self):
        tid, text, local = self._get_current_tab()
        if text is None:
            return
        code = text.get("1.0", "end-1c")
        lang = (self._run_lang.get() or "python").lower()
        self.output_box.delete("1.0", "end")
        import shutil, sys
        tmpdir = tempfile.mkdtemp(prefix="marekfs_run_")
        preview_widget = None
        # Prepare a sandbox export of the MarekFS drive under tmpdir/marekfs
        marekfs_export_root = os.path.join(tmpdir, "marekfs")
        try:
            os.makedirs(marekfs_export_root, exist_ok=True)
            if getattr(self, 'app', None):
                try:
                    # Export all files from the active MarekFS view into the sandbox
                    for rec in getattr(self.app, 'files_data', []) or []:
                        fname = rec.get('filename')
                        if not fname: continue
                        # create directories as needed
                        outpath = os.path.join(marekfs_export_root, *fname.split('/'))
                        d = os.path.dirname(outpath)
                        if d and not os.path.exists(d):
                            os.makedirs(d, exist_ok=True)
                        if not rec.get('is_dir'):
                            try:
                                data = self.app._read_file_bytes(rec, "")
                                with open(outpath, 'wb') as of:
                                    of.write(data or b"")
                            except Exception:
                                # skip unreadable files
                                pass
                except Exception:
                    pass
        except Exception:
            pass
        # Prepare environment for executed processes (after export root exists)
        env = os.environ.copy()
        env['MAREKFS_ROOT'] = marekfs_export_root
        try:
            if lang == "python":
                # For Python, prepend a sandbox preamble that redirects common file APIs
                script_path = os.path.join(tmpdir, "script.py")
                preamble = f'''import os, builtins, sys
MAREKFS_ROOT = os.environ.get('MAREKFS_ROOT', r"{marekfs_export_root}")
def _translate(p):
    if p is None: return p
    p = str(p)
    # preserve marekfs: scheme
    if p.startswith('marekfs:/'):
        rel = p[len('marekfs:/'):].lstrip('/\\')
        return os.path.join(MAREKFS_ROOT, *rel.split('/'))
    # absolute paths -> map into marekfs root (strip drive/leading slashes)
    try:
        if os.path.isabs(p) or (len(p) > 1 and p[1:2] == ':'):
            rel = p.replace(':', '').lstrip('\\/')
            return os.path.join(MAREKFS_ROOT, *rel.split('\\\\' if '\\\\' in rel else '/'))
    except Exception:
        pass
    # relative paths -> under marekfs root
    return os.path.join(MAREKFS_ROOT, p)

# Monkeypatch builtin open
_orig_open = builtins.open
def open(path, mode='r', *a, **kw):
    return _orig_open(_translate(path), mode, *a, **kw)
builtins.open = open

# Monkeypatch os functions commonly used
import os as _os
_orig_listdir = _os.listdir
def listdir(p='.'):
    return _orig_listdir(_translate(p))
_os.listdir = listdir

_orig_exists = _os.path.exists
def exists(p):
    return _orig_exists(_translate(p))
_os.path.exists = exists

'''
                # write combined script
                with open(script_path, 'w', encoding='utf-8') as f:
                    f.write(preamble)
                    f.write('\n')
                    f.write(code)
                cmd = [sys.executable, script_path]
                self._exec_cmd(cmd, cwd=tmpdir, out_widget=preview_widget, env=env)
            elif lang in ("c",):
                src = os.path.join(tmpdir, "prog.c")
                # compile output into the exported MarekFS sandbox so
                # relative file operations by the native binary land inside it
                exe = os.path.join(marekfs_export_root, "prog.exe")
                with open(src, "w", encoding="utf-8") as f: f.write(code)
                # Try compilers in order: gcc/cc, clang, cl (MSVC), tcc
                compilers = [("gcc", ["gcc", src, "-o", exe]), ("cc", ["cc", src, "-o", exe]),
                             ("clang", ["clang", src, "-o", exe]), ("cl", ["cl", "/nologo", f"/Fe:{exe}", src]),
                             ("tcc", ["tcc", "-o", exe, src])]
                found = False
                for name, cmd in compilers:
                    exe_path = _locate_executable(cmd[0])
                    if exe_path:
                        # Replace the program name with absolute path
                        cmd0 = list(cmd)
                        cmd0[0] = exe_path
                        self.output_box.insert("end", f"Using compiler: {name} ({exe_path})\n")
                        # compile with working dir = sandbox root so output goes there
                        self._exec_cmd(cmd0, cwd=marekfs_export_root, out_widget=preview_widget, env=env)
                        found = True
                        break
                if not found:
                    self.output_box.insert("end", "No C compiler found (tried gcc/cc/clang/cl/tcc).\n")
                else:
                    if os.path.exists(exe):
                        # run compiled binary inside sandbox working dir; set MAREKFS_ROOT
                        self._exec_cmd([exe], cwd=marekfs_export_root, out_widget=preview_widget, env=env)
            elif lang in ("cpp", "c++"):
                src = os.path.join(tmpdir, "prog.cpp")
                exe = os.path.join(marekfs_export_root, "prog.exe")
                with open(src, "w", encoding="utf-8") as f: f.write(code)
                # Try g++, c++, clang++, cl
                compilers = [("g++", ["g++", src, "-o", exe]), ("c++", ["c++", src, "-o", exe]),
                             ("clang++", ["clang++", src, "-o", exe]), ("cl", ["cl", "/nologo", f"/Fe:{exe}", src])]
                found = False
                for name, cmd in compilers:
                    exe_path = _locate_executable(cmd[0])
                    if exe_path:
                        cmd0 = list(cmd)
                        cmd0[0] = exe_path
                        self.output_box.insert("end", f"Using C++ compiler: {name} ({exe_path})\n")
                        self._exec_cmd(cmd0, cwd=marekfs_export_root, out_widget=preview_widget, env=env)
                        found = True
                        break
                if not found:
                    self.output_box.insert("end", "No C++ compiler found (tried g++/c++/clang++/cl).\n")
                else:
                    if os.path.exists(exe):
                        self._exec_cmd([exe], cwd=marekfs_export_root, out_widget=preview_widget, env=env)
            elif lang == "java":
                src = os.path.join(tmpdir, "Main.java")
                with open(src, "w", encoding="utf-8") as f: f.write(code)
                javac = shutil.which("javac")
                java = shutil.which("java")
                if not javac or not java:
                    self.output_box.insert("end", "Java compiler/runtime not found (javac/java).\n")
                else:
                    self._exec_cmd([javac, src], cwd=tmpdir, out_widget=preview_widget, env=env)
                    # run Main
                    self._exec_cmd([java, "-cp", tmpdir, "Main"], cwd=tmpdir, out_widget=preview_widget, env=env)
            elif lang in ("csharp", "cs"):
                src = os.path.join(tmpdir, "Program.cs")
                exe = os.path.join(tmpdir, "prog.exe")
                with open(src, "w", encoding="utf-8") as f: f.write(code)
                csc = shutil.which("csc") or shutil.which("mcs")
                if csc:
                    self._exec_cmd([csc, "/out:" + exe, src], cwd=tmpdir, out_widget=preview_widget, env=env)
                    if os.path.exists(exe):
                        self._exec_cmd([exe], cwd=tmpdir, out_widget=preview_widget, env=env)
                else:
                    dotnet = shutil.which("dotnet")
                    if dotnet:
                        # try dotnet script run via temporary project (best-effort)
                        self.output_box.insert("end", "dotnet found but automatic C# compilation is not implemented here.\n")
                    else:
                        self.output_box.insert("end", "C# compiler not found (csc/mcs/dotnet).\n")
            else:
                self.output_box.insert("end", f"Unknown language: {lang}\n")
        finally:
            # Before cleaning up the tempdir, sync any files created in the
            # MarekFS export sandbox back into the live MarekFS volume.
            try:
                if getattr(self, 'app', None):
                    try:
                        export_root = marekfs_export_root
                        if os.path.isdir(export_root):
                            for root_dir, dirs, files in os.walk(export_root):
                                for fname in files:
                                    fpath = os.path.join(root_dir, fname)
                                    rel = os.path.relpath(fpath, export_root)
                                    marekfs_name = rel.replace(os.path.sep, '/')
                                    try:
                                        with open(fpath, 'rb') as rf:
                                            data = rf.read()
                                    except Exception:
                                        continue
                                    # If file exists in MarekFS, overwrite; otherwise create
                                    existing = next((r for r in self.app.files_data if r.get('filename') == marekfs_name), None)
                                    try:
                                        if existing:
                                            self.app._write_file_bytes(existing, data, password="")
                                        else:
                                            # create at root with that name
                                            self.app.create_entry(marekfs_name, data, 0)
                                    except Exception:
                                        pass
                    except Exception:
                        pass
            finally:
                try:
                    shutil.rmtree(tmpdir)
                except Exception:
                    pass

    def _exec_cmd(self, cmd, cwd=None, out_widget=None, env=None):
        try:
            line = f"$ {' '.join(cmd)}\n"
            self.output_box.insert("end", line)
            if out_widget:
                try: out_widget.insert("end", line)
                except Exception: pass
            self.output_box.see("end")
            # Use Popen so we can terminate the whole process tree on timeout (Windows taskkill)
            proc = subprocess.Popen(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, creationflags=getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0), env=env)
            try:
                out, err = proc.communicate(timeout=120)
                if out:
                    self.output_box.insert("end", out)
                    if out_widget:
                        try: out_widget.insert("end", out)
                        except Exception: pass
                if err:
                    self.output_box.insert("end", err)
                    if out_widget:
                        try: out_widget.insert("end", err)
                        except Exception: pass
                final = f"Exit {proc.returncode}\n"
                self.output_box.insert("end", final)
                if out_widget:
                    try: out_widget.insert("end", final)
                    except Exception: pass
                self.output_box.see("end")
            except subprocess.TimeoutExpired:
                # Attempt to kill the process tree reliably on Windows
                try:
                    subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                except Exception:
                    pass
                msg = "Process timed out and was terminated\n"
                self.output_box.insert("end", msg)
                if out_widget:
                    try: out_widget.insert("end", msg)
                    except Exception: pass
                self.output_box.see("end")
        except Exception as e:
            self.output_box.insert("end", f"Execution failed: {e}\n")
            if out_widget:
                try: out_widget.insert("end", f"Execution failed: {e}\n")
                except Exception: pass


class DiskTestWindow:
    """Live 'stock-chart' style write-speed graph for the DiskTest stress run."""
    def __init__(self, parent, on_finished=None):
        self.win = tk.Toplevel(parent)
        theme_existing_window(self.win, parent)
        self.win.title("🧪 DiskTest — sustained cache write benchmark")
        self.win.geometry("900x560")
        self.win.protocol("WM_DELETE_WINDOW", self._on_close)
        self.samples = []
        self.max_speed = 0.0
        self.running = True
        self.start_time = time.time()
        self.on_finished = on_finished
        top = ttk.Frame(self.win, padding=8); top.pack(fill=tk.X)
        self.lbl_status = ttk.Label(top, text="Starting…", font=("Segoe UI", 11, "bold"))
        self.lbl_status.pack(side=tk.LEFT)
        self.lbl_max = ttk.Label(top, text="Max: 0 MB/s", font=("Segoe UI", 11, "bold"), foreground="#e53935")
        self.lbl_max.pack(side=tk.RIGHT)
        self.lbl_now = ttk.Label(top, text="Now: 0 MB/s", font=("Segoe UI", 11, "bold"), foreground="#43a047")
        self.lbl_now.pack(side=tk.RIGHT, padx=15)
        self.canvas = tk.Canvas(self.win, bg="#0d1117", highlightthickness=0)
        self.canvas.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)
        bot = ttk.Frame(self.win, padding=8); bot.pack(fill=tk.X)
        self.duration_min = tk.IntVar(value=10)
        ttk.Label(bot, text="Duration (min):").pack(side=tk.LEFT)
        ttk.Spinbox(bot, from_=1, to=60, width=5, textvariable=self.duration_min).pack(side=tk.LEFT, padx=6)
        self.btn_stop = ttk.Button(bot, text="⏹ Stop", command=self.stop)
        self.btn_stop.pack(side=tk.RIGHT)
        self.win.update_idletasks()
        self._poll_id = None
        self._schedule_poll()

    def add_sample(self, elapsed, speed):
        self.samples.append((elapsed, speed))
        if speed > self.max_speed: self.max_speed = speed

    def stop(self): self.running = False

    def _on_close(self):
        self.running = False
        if self._poll_id:
            try: self.win.after_cancel(self._poll_id)
            except Exception: pass
        self.win.destroy()

    def _schedule_poll(self):
        self._poll_id = self.win.after(400, self._poll)

    def _poll(self):
        self._draw()
        if self.running:
            self.lbl_status.config(text=f"Running… {int(time.time()-self.start_time)}s")
            self.lbl_now.config(text=f"Now: {self.samples[-1][1]:.0f} MB/s" if self.samples else "Now: 0 MB/s")
            self.lbl_max.config(text=f"Max: {self.max_speed:.0f} MB/s")
            self._schedule_poll()

    def finished(self):
        self.running = False
        # Compute summary statistics for the recorded samples
        speeds = [s for (_, s) in self.samples if s and s > 0]
        summary = None
        if speeds:
            import statistics, collections
            avg = statistics.mean(speeds)
            med = statistics.median(speeds)
            # Determine the most common (mode) by rounding to nearest integer
            rounded = [int(round(x)) for x in speeds]
            most_common = collections.Counter(rounded).most_common(1)[0][0]
            summary = f"Avg: {avg:.1f} MB/s · Median: {med:.1f} MB/s · Most common: {most_common} MB/s"
            # Append summary to status label
            try:
                self.lbl_status.config(text=f"Finished — max {self.max_speed:.0f} MB/s · {summary}")
            except Exception:
                self.lbl_status.config(text=f"Finished — max {self.max_speed:.0f} MB/s")
        else:
            self.lbl_status.config(text=f"Finished — max {self.max_speed:.0f} MB/s")

        # Notify caller (app) with the summary so it can be shown in Disk Info
        try:
            if self.on_finished and summary:
                try:
                    self.on_finished(summary)
                except Exception:
                    pass
        except Exception:
            pass

    def _draw(self):
        c = self.canvas; c.delete("all")
        w = int(c.winfo_width()) or 880; h = int(c.winfo_height()) or 420
        left, right, top, bottom = 50, w-10, 12, h-30
        c.create_line(left, bottom, right, bottom, fill="#444")
        c.create_line(left, top, left, bottom, fill="#444")
        c.create_text(left-10, top-2, text="MB/s", anchor="nw", fill="#9aa")
        duration = max(1, self.duration_min.get()) * 60
        t_max = max(duration, 1)
        s_max = max(self.max_speed * 1.1, 10.0)
        for frac in (0.25, 0.5, 0.75, 1.0):
            y = bottom - frac * (bottom - top)
            c.create_line(left, y, right, y, fill="#222")
            c.create_text(left-6, y, text=f"{int(s_max*frac)}", anchor="e", fill="#777")
        if self.max_speed > 0:
            y = bottom - (self.max_speed / s_max) * (bottom - top)
            c.create_line(left, y, right, y, fill="#e53935", dash=(4, 4))
            c.create_text(right-4, y-2, text=f"max {self.max_speed:.0f}", anchor="e", fill="#e53935")
        if len(self.samples) >= 2:
            pts = []
            for t, sp in self.samples:
                x = left + (t / t_max) * (right - left)
                y = bottom - (sp / s_max) * (bottom - top)
                pts.extend([x, y])
            c.create_line(*pts, fill="#43a047", width=2)
        elif len(self.samples) == 1:
            t, sp = self.samples[0]
            x = left + (t / t_max) * (right - left)
            y = bottom - (sp / s_max) * (bottom - top)
            c.create_oval(x-2, y-2, x+2, y+2, fill="#43a047")
        for frac in (0, 0.25, 0.5, 0.75, 1.0):
            x = left + frac * (right - left)
            c.create_text(x, bottom+6, text=f"{int(frac*duration)}s", fill="#777")


class VisualizeWindow:
    """Raw-sector viewer including the (possibly blown) cache region."""
    def __init__(self, parent, drive_path, cache_blown, dir_sectors_count):
        self.drive_path = drive_path
        self.cache_blown = cache_blown
        self.dir_sectors_count = dir_sectors_count
        self.win = tk.Toplevel(parent)
        theme_existing_window(self.win, parent)
        self.win.title("👁️ Visualize Drive Data (raw sectors)")
        self.win.geometry("820x560")
        ctrl = ttk.Frame(self.win, padding=8); ctrl.pack(fill=tk.X)
        self._region = tk.StringVar(value="cache")
        ttk.Radiobutton(ctrl, text="Cache region (raw, even if blown)", variable=self._region, value="cache").pack(side=tk.LEFT, padx=4)
        ttk.Radiobutton(ctrl, text="Directory table", variable=self._region, value="dir").pack(side=tk.LEFT, padx=4)
        ttk.Radiobutton(ctrl, text="Custom sector range", variable=self._region, value="custom").pack(side=tk.LEFT, padx=4)
        self._sector = tk.IntVar(value=CACHE_START_SECTOR)
        self._count = tk.IntVar(value=16)
        ttk.Label(ctrl, text="Start:").pack(side=tk.LEFT, padx=(10,2))
        ttk.Entry(ctrl, textvariable=self._sector, width=14).pack(side=tk.LEFT)
        ttk.Label(ctrl, text="Sectors:").pack(side=tk.LEFT, padx=(6,2))
        ttk.Entry(ctrl, textvariable=self._count, width=6).pack(side=tk.LEFT)
        ttk.Button(ctrl, text="🔍 Read", style="Accent.TButton", command=self._read).pack(side=tk.LEFT, padx=8)
        info = ttk.Frame(self.win, padding=4); info.pack(fill=tk.X)
        self._info = tk.StringVar(value="")
        ttk.Label(info, textvariable=self._info, font=("Consolas", 10)).pack(anchor=tk.W)
        body = ttk.Frame(self.win); body.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)
        self._text = tk.Text(body, font=Font(family="Consolas", size=10), bg="#0d1117", fg="#c9d1d9", wrap=tk.NONE)
        sb = ttk.Scrollbar(body, command=self._text.yview)
        self._text.configure(yscrollcommand=sb.set)
        self._text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        self._read()

    def _read(self):
        region = self._region.get()
        if region == "cache":
            start = CACHE_START_SECTOR
            count = min(int(self._count.get()), CACHE_SECTORS)
            label = f"Cache region @ sector {start} ({count} sectors) — {'BLOWN (raw)' if self.cache_blown else 'valid'}"
        elif region == "dir":
            start = DEFAULT_DIR_START_SECTOR
            count = min(int(self._count.get()), self.dir_sectors_count)
            label = f"Directory table @ sector {start} ({count} sectors)"
        else:
            start = int(self._sector.get()); count = int(self._count.get())
            if count <= 0: count = 1
            label = f"Custom @ sector {start} ({count} sectors)"
        try:
            fd = open_drive(self.drive_path, read_write=False)
            try:
                data = read_sectors(fd, start, count)
            finally:
                os.close(fd)
        except Exception as e:
            self._info.set(f"Read error: {e}")
            return
        self._info.set(label + f" — {len(data)} bytes")
        self._text.delete("1.0", tk.END)
        for off in range(0, len(data), 16):
            chunk = data[off:off+16]
            hexpart = " ".join(f"{b:02x}" for b in chunk).ljust(48)
            asciipart = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
            self._text.insert(tk.END, f"{start*SECTOR_SIZE+off:012x}  {hexpart}  {asciipart}\n")


class MediaViewer:
    """Image viewing in-app; movie playback in-app via the MarekFS video
    player (OpenCV). Never opens the system default player."""
    def __init__(self, parent, filename, raw_bytes):
        self.filename = filename
        self.raw = raw_bytes
        self.temp = None
        self.parent = parent
        self.win = tk.Toplevel(parent)
        theme_existing_window(self.win, parent)
        self.win.title(f"🎬 Media Viewer — {filename}")
        self.win.geometry("780x620")
        self.win.protocol("WM_DELETE_WINDOW", self._close)
        ext = os.path.splitext(filename)[1].lower()
        if ext in IMAGE_EXTS:
            self._show_image()
        elif ext in MOVIE_EXTS:
            self._show_movie()
        else:
            ttk.Label(self.win, text="Unsupported media type.").pack(pady=20)

    def _close(self):
        try:
            if self.temp and os.path.exists(self.temp):
                os.remove(self.temp)
        except Exception:
            pass
        self.win.destroy()

    def _write_temp(self):
        ext = os.path.splitext(self.filename)[1] or ".bin"
        fd, path = tempfile.mkstemp(suffix=ext)
        try:
            os.write(fd, self.raw)
        finally:
            os.close(fd)
        self.temp = path
        return path

    def _show_image(self):
        path = self._write_temp()
        try:
            from PIL import Image, ImageTk
        except ImportError:
            # fallback to tk native (gif/pgm/ppm, png on newer Tk)
            try:
                img = tk.PhotoImage(file=path)
                self._render_tkphoto(img)
                return
            except Exception as e:
                messagebox.showerror("Image", f"Cannot display image (Pillow not installed):\n{e}")
                return
        try:
            im = Image.open(path)
            # fit to window
            maxw, maxh = 760, 520
            im.thumbnail((maxw, maxh), Image.LANCZOS)
            self._pil_im = ImageTk.PhotoImage(im)
            canv = tk.Label(self.win, image=self._pil_im, bg="#0d1117")
            canv.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)
            ttk.Label(self.win, text=f"{self.filename} — {format_bytes(len(self.raw))}").pack(pady=4)
        except Exception as e:
            messagebox.showerror("Image", f"Cannot display image:\n{e}")

    def _render_tkphoto(self, img):
        self._tk_img = img
        lbl = tk.Label(self.win, image=self._tk_img, bg="#0d1117")
        lbl.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)

    def _show_movie(self):
        path = self._write_temp()
        try:
            from marekfs_media import VideoPlayerWindow
        except Exception as e:
            top = ttk.Frame(self.win, padding=12); top.pack(fill=tk.X)
            ttk.Label(top, text=f"🎬 {self.filename} — {format_bytes(len(self.raw))}", font=("Segoe UI", 12, "bold")).pack(anchor=tk.W)
            ttk.Label(top, text=f"In-app player unavailable: {e}").pack(anchor=tk.W, pady=2)
            self.status = tk.StringVar(value="Install opencv-python:  pip install opencv-python")
            ttk.Label(self.win, textvariable=self.status).pack(anchor=tk.W, padx=12, pady=4)
            return
        # Hand off to the in-app MarekFS video player (OpenCV-based).
        try:
            self.win.destroy()
            VideoPlayerWindow(self.parent, self.filename, data=self.raw)
        except Exception as e:
            self.status = tk.StringVar(value=f"In-app player error: {e}")
            ttk.Label(self.parent, textvariable=self.status).pack(anchor=tk.W, padx=12, pady=4)


class ChecksumsWindow:
    """Scans all MarekFS files and verifies stored checksums (bit-rot scrub)."""
    def __init__(self, parent, app):
        self.app = app
        self.win = tk.Toplevel(parent)
        theme_existing_window(self.win, parent, title="🧮 MarekFS Checksums / Scrub")
        self.win.geometry("760x520")
        ttk.Label(self.win, text="Checksum scrub", style="Title.TLabel").pack(anchor=tk.W, padx=12, pady=8)
        bar = ttk.Frame(self.win, padding=6); bar.pack(fill=tk.X)
        ttk.Button(bar, text="▶ Scan all files", style="Accent.TButton", command=self.scan).pack(side=tk.LEFT, padx=4)
        ttk.Button(bar, text="💾 Export report", command=self.export).pack(side=tk.LEFT, padx=4)
        self.summary = tk.StringVar(value="Ready. Files are verified with BLAKE2b-256 checksums.")
        ttk.Label(self.win, textvariable=self.summary, wraplength=720).pack(anchor=tk.W, padx=12, pady=4)
        self.out = tk.Text(self.win, state="disabled"); self.out.pack(fill=tk.BOTH, expand=True, padx=12, pady=8)
        self.report = []

    def _show(self, text):
        self.out.configure(state="normal"); self.out.delete("1.0", tk.END); self.out.insert(tk.END, text); self.out.configure(state="disabled")

    def scan(self):
        self.summary.set("Scanning…"); self.report = []
        def work():
            matched = mismatched = missing = skipped = 0
            for record in self.app.files_data:
                if record.get("is_dir") or (record.get("attributes", 0) & 0x10):
                    skipped += 1; continue
                stored = self.app.file_metadata.get(record.get("filename", ""), {}).get("checksum")
                if not stored:
                    missing += 1; self.report.append((record["filename"], "NO CHECKSUM", "")); continue
                try:
                    raw = self.app._read_file_bytes(record, "")
                    actual = data_checksum(raw)
                    if actual == stored: matched += 1
                    else: mismatched += 1; self.report.append((record["filename"], "MISMATCH", stored[:12] + "…"))
                except Exception as e:
                    mismatched += 1; self.report.append((record["filename"], "MISMATCH/ERROR", str(e)))
            self.win.after(0, lambda: self._finish(matched, mismatched, missing, skipped))
        threading.Thread(target=work, daemon=True).start()

    def _finish(self, matched, mismatched, missing, skipped):
        self.summary.set(f"Matched: {matched} · Mismatch/error: {mismatched} · No checksum: {missing} · Folders skipped: {skipped}")
        lines = [f"{name}  [{status}] {detail}" for name, status, detail in self.report] or ["No problems found."]
        self._show("\n".join(lines))

    def export(self):
        path = filedialog.asksaveasfilename(defaultextension=".txt", filetypes=[("Text", "*.txt")], parent=self.win)
        if not path: return
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(f"{name} [{status}] {detail}" for name, status, detail in self.report) or "No problems found.\n")


class VirusScanWindow:
    """Extracts the selected file to temp, runs available AV scanners in a
    background thread, and reports each scanner's output live in the GUI."""
    def __init__(self, parent, filename, raw_bytes):
        self.filename = filename
        self.raw = raw_bytes
        self.temp = None
        self.win = tk.Toplevel(parent)
        theme_existing_window(self.win, parent)
        self.win.title(f"🛡 Virus Scan — {filename}")
        self.win.geometry("760x520")
        self.win.protocol("WM_DELETE_WINDOW", self._close)
        self.scanner_config = ensure_scanner_config()
        self.scan_summary = tk.StringVar(value="Built-in scanner: preparing…")
        top = ttk.Frame(self.win, padding=8); top.pack(fill=tk.X)
        self.lbl = ttk.Label(top, text="Extracting file to temp…", font=("Segoe UI", 11, "bold"))
        self.lbl.pack(side=tk.LEFT)
        ttk.Label(top, textvariable=self.scan_summary).pack(side=tk.LEFT, padx=12)
        ttk.Button(top, text="Update rules", command=self.update_rules).pack(side=tk.RIGHT, padx=4)
        ttk.Button(top, text="Update hashes", command=self.update_hashes).pack(side=tk.RIGHT, padx=4)
        ttk.Label(top, text=f"{format_bytes(len(raw_bytes))}").pack(side=tk.RIGHT)
        body = ttk.Frame(self.win); body.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)
        self.out = tk.Text(body, font=Font(family="Consolas", size=10), bg="#0d1117", fg="#c9d1d9", wrap=tk.WORD)
        sb = ttk.Scrollbar(body, command=self.out.yview)
        self.out.configure(yscrollcommand=sb.set)
        self.out.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        threading.Thread(target=self._work, daemon=True).start()

    def update_hashes(self):
        self.scan_summary.set("Updating hash database…")
        def work():
            results = update_scanner_hashes(self.scanner_config)
            ok = sum(1 for item in results if item.get("status") == "updated")
            self.win.after(0, lambda: self.scan_summary.set(f"Hash feeds updated: {ok}/{len(results)}"))
        threading.Thread(target=work, daemon=True).start()

    def update_rules(self):
        self.scan_summary.set("Updating YARA rules…")
        def work():
            results = update_scanner_rules(self.scanner_config)
            ok = sum(1 for item in results if item.get("status") == "updated")
            self.win.after(0, lambda: self.scan_summary.set(f"Rules updated: {ok}/{len(results)}"))
        threading.Thread(target=work, daemon=True).start()

    def _close(self):
        try:
            if self.temp and os.path.exists(self.temp):
                os.remove(self.temp)
        except Exception:
            pass
        self.win.destroy()

    def _log(self, msg):
        self.out.insert(tk.END, msg + "\n")
        self.out.see(tk.END)

    def _set(self, msg):
        self.lbl.config(text=msg)

    def _work(self):
        def ui_log(m): self.win.after(0, lambda: self._log(m))
        def ui_set(m): self.win.after(0, lambda: self._set(m))
        try:
            ext = os.path.splitext(self.filename)[1] or ".bin"
            fd, path = tempfile.mkstemp(suffix=ext)
            try:
                os.write(fd, self.raw)
            finally:
                os.close(fd)
            self.temp = path
        except Exception as e:
            ui_log(f"Failed to extract: {e}")
            ui_set("Error")
            return
        ui_set("Extracted — running built-in scanner…")
        try:
            builtin = scan_bytes_with_builtin_scanner(self.filename, self.raw, self.scanner_config)
            ui_log(f"Built-in scanner: {builtin['status']} | SHA-256: {builtin['sha256']}")
            ui_log(f"YARA: {builtin.get('yara', 'not used')}")
            for finding in builtin.get("findings", []):
                ui_log(f"  [{finding['severity'].upper()}] {finding['rule']}: {finding['detail']}")
            ui_set(f"Built-in scanner: {builtin['status']}")
            self.win.after(0, lambda s=builtin["status"]: self.scan_summary.set(f"Built-in: {s}"))
        except Exception as e:
            ui_log(f"Built-in scanner error: {e}")
        ui_set("Extracted — locating antivirus…")
        scanners = find_av_scanners()
        if not scanners:
            ui_log("No supported antivirus scanner found.")
            ui_log("Install Bitdefender / Avast / AVG / Windows Defender (or ClamAV) to enable scanning.")
            ui_set("No scanner")
            return
        ui_log(f"Found {len(scanners)} scanner(s): " + ", ".join(n for n, _ in scanners))
        for name, exe in scanners:
            ui_set(f"Scanning with {name}…")
            ui_log(f"\n=== {name} ===")
            if "VPN" in name:
                ui_log("  CyberGhost is a VPN, not an antivirus — it cannot scan files. Skipping.")
                continue
            cmd = build_av_command(name, exe, path)
            ui_log("  $ " + " ".join(f'"{a}"' if " " in a else a for a in cmd))
            try:
                proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
                if proc.stdout:
                    for line in proc.stdout.splitlines():
                        ui_log("  " + line)
                if proc.stderr:
                    ui_log("  [stderr]")
                    for line in proc.stderr.splitlines():
                        ui_log("  " + line)
                ui_log(f"  [exit code: {proc.returncode}]")
            except FileNotFoundError:
                ui_log("  Scanner executable not found at runtime.")
            except subprocess.TimeoutExpired:
                ui_log("  Scan timed out after 300s.")
            except Exception as e:
                ui_log(f"  Scan error: {e}")
        ui_set("Scan complete")
        ui_log("\nScan complete.")


class ScannerUpdateProgressWindow:
    """Live-progress downloader for a ClamAV database update.

    Shows a determinate progress bar plus a precise "XX.XXX%" readout that
    updates on every chunk. Blocks its own close button until the download
    finishes (success or failure) so the operation can't be abandoned
    mid-write.
    """
    def __init__(self, parent, scanner_config, on_done=None):
        self.scanner_config = scanner_config
        self.on_done = on_done
        self._finished = False

        self.win = tk.Toplevel(parent)
        theme_existing_window(self.win, parent)
        self.win.title("🛡 MarekFS Scanner Update")
        self.win.geometry("480x190")
        self.win.resizable(True, True)
        self.win.protocol("WM_DELETE_WINDOW", self._on_close_attempt)

        body = ttk.Frame(self.win, padding=16)
        body.pack(fill=tk.BOTH, expand=True)
        ttk.Label(body, text="MarekFS Scanner Update", style="Title.TLabel").pack(anchor=tk.W)
        self.status_var = tk.StringVar(value="Connecting to the ClamAV database mirror…")
        ttk.Label(body, textvariable=self.status_var, wraplength=440).pack(anchor=tk.W, pady=(4, 12))

        self.progress = ttk.Progressbar(body, mode="determinate", maximum=100000, value=0)
        self.progress.pack(fill=tk.X)

        row = ttk.Frame(body)
        row.pack(fill=tk.X, pady=(8, 0))
        self.bytes_var = tk.StringVar(value="")
        ttk.Label(row, textvariable=self.bytes_var).pack(side=tk.LEFT)
        self.pct_var = tk.StringVar(value="00.000%")
        ttk.Label(row, textvariable=self.pct_var, font=("Consolas", 11, "bold")).pack(side=tk.RIGHT)

        self.close_btn = ttk.Button(body, text="Downloading…", state="disabled", command=self.win.destroy)
        self.close_btn.pack(anchor=tk.E, pady=(14, 0))

        threading.Thread(target=self._run, daemon=True).start()

    def _on_close_attempt(self):
        if self._finished:
            self.win.destroy()
        # Ignore attempts to close while the download is in progress.

    def _on_progress(self, read, total):
        if total:
            percent = min(100.0, (read / total) * 100.0)
            bytes_label = f"{format_bytes(read)} / {format_bytes(total)}"
        else:
            # Server didn't send Content-Length: show bytes moved, cap the
            # bar's visual fill so it doesn't look complete prematurely.
            percent = min(99.999, (read / (128 * 1024 * 1024)) * 100.0)
            bytes_label = format_bytes(read)

        def update_ui():
            self.progress["value"] = percent * 1000  # maximum=100000 -> 3 decimal places
            self.pct_var.set(f"{percent:06.3f}%")
            self.bytes_var.set(bytes_label)
            self.status_var.set("Downloading ClamAV database update…")
        self.win.after(0, update_ui)

    def _run(self):
        try:
            result = download_clamav_update(self.scanner_config, progress=self._on_progress)
        except Exception as e:
            self.win.after(0, lambda: self._finish(None, error=str(e)))
            return
        self.win.after(0, lambda: self._finish(result))

    def _finish(self, result, error=None):
        self._finished = True
        self.win.protocol("WM_DELETE_WINDOW", self.win.destroy)
        self.close_btn.configure(text="Close", state="normal")
        if error:
            self.progress.configure(value=0)
            self.status_var.set(f"Update failed: {error}")
            self.pct_var.set("--.---%")
            messagebox.showerror("MarekFS Scanner Update", f"Update failed:\n{error}", parent=self.win)
        else:
            self.progress["value"] = 100000
            self.pct_var.set("100.000%")
            self.status_var.set(f"Installed — {result['hashes']:,} ClamAV signature hashes now active "
                                f"(database version {result['version']}).")
        if self.on_done:
            try:
                self.on_done(result, error)
            except Exception:
                pass


class ClamAVUpdateSettingsWindow:
    """Lets the user enable automatic ClamAV database updates. No account or
    API key is needed — database.clamav.net is ClamAV's public, free update
    mirror (the same one `freshclam` uses). Also supports a manual check."""
    def __init__(self, parent, scanner_config, on_check_now=None):
        self.scanner_config = scanner_config
        self.on_check_now = on_check_now

        self.win = tk.Toplevel(parent)
        theme_existing_window(self.win, parent)
        self.win.title("🛡 MarekFS Scanner Updates")
        self.win.geometry("520x340")

        body = ttk.Frame(self.win, padding=16)
        body.pack(fill=tk.BOTH, expand=True)
        ttk.Label(body, text="ClamAV Database Auto-Update", style="Title.TLabel").pack(anchor=tk.W)
        ttk.Label(body, wraplength=470, text=(
            "MarekFS Scanner can automatically check ClamAV's free public database "
            "mirror every 4 hours for signature updates. No account or API key is "
            "required. Only SHA-256 hash signatures are extracted — never any "
            "malware samples."
        )).pack(anchor=tk.W, pady=(4, 12))

        self.enabled_var = tk.BooleanVar(value=bool(scanner_config.get("clamav_enabled")))
        ttk.Checkbutton(body, text="Automatically check every 4 hours",
                        variable=self.enabled_var).pack(anchor=tk.W)

        db_row = ttk.Frame(body)
        db_row.pack(fill=tk.X, pady=(10, 12))
        ttk.Label(db_row, text="Database:").pack(side=tk.LEFT)
        self.db_var = tk.StringVar(value=scanner_config.get("clamav_database", "daily"))
        ttk.Combobox(db_row, textvariable=self.db_var, state="readonly", width=20,
                     values=["daily", "main"]).pack(side=tk.LEFT, padx=6)
        ttk.Label(db_row, text="(daily = frequent updates, main = full base signatures)").pack(side=tk.LEFT, padx=6)

        self.status_var = tk.StringVar(value=self._status_text())
        ttk.Label(body, textvariable=self.status_var, wraplength=470).pack(anchor=tk.W)

        buttons = ttk.Frame(body)
        buttons.pack(fill=tk.X, pady=(16, 0))
        ttk.Button(buttons, text="Save", style="Accent.TButton", command=self.save).pack(side=tk.LEFT, padx=4)
        ttk.Button(buttons, text="Check now", command=self.check_now).pack(side=tk.LEFT, padx=4)
        ttk.Button(buttons, text="Close", command=self.win.destroy).pack(side=tk.RIGHT, padx=4)

    def _status_text(self):
        cfg = self.scanner_config
        last_check = cfg.get("clamav_last_check") or "never"
        last_installed = cfg.get("clamav_last_installed") or "never"
        count = cfg.get("clamav_last_hash_count", 0)
        return (f"Last checked: {last_check}\n"
                f"Last installed: {last_installed}  ·  {count:,} hashes")

    def _apply_fields(self):
        self.scanner_config["clamav_enabled"] = bool(self.enabled_var.get())
        self.scanner_config["clamav_database"] = self.db_var.get()

    def save(self):
        self._apply_fields()
        save_scanner_config(self.scanner_config)
        self.status_var.set(self._status_text())
        messagebox.showinfo("MarekFS Scanner Updates", "Settings saved.", parent=self.win)

    def check_now(self):
        self._apply_fields()
        save_scanner_config(self.scanner_config)
        self.status_var.set("Checking the ClamAV database mirror…")

        def work():
            result = check_clamav_update(self.scanner_config)

            def done():
                self.status_var.set(self._status_text())
                if result.get("error"):
                    messagebox.showerror("MarekFS Scanner Updates", result["error"], parent=self.win)
                elif result.get("available"):
                    if messagebox.askyesno(
                            "MarekFS Scanner Update",
                            "A new update is available.\n\n"
                            "Do you want to install new update for MarekFS Scanner?",
                            parent=self.win):
                        def refreshed(res, err):
                            self.status_var.set(self._status_text())
                            if self.on_check_now:
                                self.on_check_now(res, err)
                        ScannerUpdateProgressWindow(self.win, self.scanner_config, on_done=refreshed)
                else:
                    messagebox.showinfo("MarekFS Scanner Updates", "Already up to date.", parent=self.win)
            self.win.after(0, done)

        threading.Thread(target=work, daemon=True).start()
