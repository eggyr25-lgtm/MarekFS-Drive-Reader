"""MarekFS media players and custom audio file selector.

All playback is handled in-app — no Windows Media Player, no system player.
Audio uses pygame.mixer with a waveform visualizer; video uses OpenCV with
a seek bar and frame stepping.
"""
import io
import json
import os
import random
import string
import struct
import sys
import tempfile
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

from ui_custom import theme_existing_window, minimize_to_corner
from marekfs_core import (
    format_bytes, IMAGE_EXTS, MOVIE_EXTS, MAREKVID_EXT, MAREKAUDIO_EXT,
    parse_marek_media, create_marekaudio, create_marekvid,
)

AUDIO_EXTS = {".mp3", ".wav", ".ogg", ".flac", ".m4a", ".aac", ".wma", ".opus", ".aiff", ".ape"}
VIDEO_EXTS = MOVIE_EXTS | {MAREKVID_EXT}
BOOKMARKS_PATH = os.path.join(os.path.expanduser("~"), ".marekfs_media_bookmarks.json")
_TEMP_FILES = []


def _spill_to_temp(name, data):
    ext = os.path.splitext(name)[1] or ".bin"
    fd, path = tempfile.mkstemp(prefix="marekfs_", suffix=ext)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    _TEMP_FILES.append(path)
    return path


def cleanup_temp_files():
    for path in list(_TEMP_FILES):
        try:
            os.remove(path)
        except Exception:
            pass
        try:
            _TEMP_FILES.remove(path)
        except ValueError:
            pass


def _pil():
    try:
        from PIL import Image, ImageTk, ImageEnhance, ImageFilter, ImageOps
        return Image, ImageTk, ImageEnhance, ImageFilter, ImageOps
    except Exception:
        return (None,) * 5


def _pygame():
    try:
        import pygame
        return pygame
    except Exception:
        return None


def _add_resize_grip(win, icon="◢"):
    """Add a draggable resize grip to the bottom-right corner of a frameless window."""
    t = getattr(win, "_marekfs_theme", {})
    fg = t.get("accent", "#00d2ff")
    grip = tk.Label(win, text=icon, cursor="sizing", bg=win.cget("bg"), fg=fg,
                    font=("Segoe UI", 12, "bold"))
    grip.place(relx=1.0, rely=1.0, anchor="se", x=-2, y=-2)

    state = {"x": 0, "y": 0, "w": 0, "h": 0}

    def _start(e):
        state["x"] = e.x_root
        state["y"] = e.y_root
        state["w"] = win.winfo_width()
        state["h"] = win.winfo_height()

    def _drag(e):
        dx = e.x_root - state["x"]
        dy = e.y_root - state["y"]
        try:
            min_w, min_h = win.minsize()
        except Exception:
            min_w, min_h = 400, 300
        new_w = max(min_w, state["w"] + dx)
        new_h = max(min_h, state["h"] + dy)
        win.geometry(f"{new_w}x{new_h}")

    grip.bind("<Button-1>", _start)
    grip.bind("<B1-Motion>", _drag)
    return grip


def _bind_minimize_on_focus_out(win, icon_text="🎵"):
    """When the window loses focus to another app, minimize it to the corner."""
    def _on_focus_out(_event):
        win.after(200, _check_focus)

    def _check_focus():
        try:
            if str(win.state()) == "withdrawn":
                return
            focused = win.focus_get()
            if focused is not None:
                toplevel = focused.winfo_toplevel()
                if toplevel is win:
                    return  # Focus is within our window
                # Check if it's a child dialog (messagebox, file selector, etc.)
                parent = toplevel.master
                while parent is not None:
                    if parent is win:
                        return  # It's a child dialog of our window
                    parent = parent.master
            # Focus is in another app or on an unrelated window → minimize to corner
            minimize_to_corner(win, parent=win, icon_text=icon_text,
                               theme=getattr(win, "_marekfs_theme", None))
        except Exception:
            pass

    win.bind("<FocusOut>", _on_focus_out)


def _extract_waveform(path, max_points=400):
    """Extract a waveform from an audio file.

    Returns a list of (min_amp, max_amp) pairs in 16-bit signed range,
    or None if the format cannot be decoded. Runs in a background thread
    by the caller so the UI never freezes.
    """
    try:
        pygame = _pygame()
        if pygame is None:
            return None
        sound = pygame.mixer.Sound(path)
        raw = sound.get_raw()
        if not raw:
            return None
        # pygame.mixer default format: 16-bit signed, interleaved stereo.
        n_samples = len(raw) // 2
        samples = struct.unpack(f"<{n_samples}h", raw[:n_samples * 2])
        # Collapse stereo to mono.
        mono = []
        for i in range(0, len(samples) - 1, 2):
            mono.append((samples[i] + samples[i + 1]) // 2)
        if not mono:
            return None
        # Downsample to max_points buckets.
        step = max(1, len(mono) // max_points)
        points = []
        for i in range(0, len(mono), step):
            chunk = mono[i:i + step]
            if chunk:
                points.append((min(chunk), max(chunk)))
        return points
    except Exception:
        return None


class MarekFSVirtualMediaSource:
    """Adapter allowing the custom selector to browse the active MarekFS disk."""
    def __init__(self, list_records, read_record, write_record=None):
        self.list_records = list_records
        self.read_record = read_record
        self.write_record = write_record

    def list_files(self, current="marekfs:/", extensions=None):
        prefix = current.removeprefix("marekfs:/").strip("/")
        rows = []
        folders = set()
        allowed = {x.lower() for x in extensions} if extensions else None
        for record in self.list_records():
            name = record.get("filename", "")
            if prefix and not name.startswith(prefix + "/"):
                continue
            rel = name[len(prefix) + 1:] if prefix else name
            if "/" in rel:
                folders.add(rel.split("/", 1)[0])
                continue
            if not record.get("is_dir") and (allowed is None or os.path.splitext(rel)[1].lower() in allowed):
                rows.append((rel, "marekfs:/" + name, False))
        rows.extend((folder, "marekfs:/" + (prefix + "/" if prefix else "") + folder, True) for folder in sorted(folders))
        return rows

    def list_audio(self, current="marekfs:/"):
        return self.list_files(current, AUDIO_EXTS)

    def read_bytes(self, source):
        name = source.removeprefix("marekfs:/")
        record = next(r for r in self.list_records() if r.get("filename") == name)
        return self.read_record(record)

    def write_bytes(self, source, data):
        if not source.startswith("marekfs:/"):
            raise ValueError("Virtual media destination must use marekfs:/")
        if not self.write_record:
            raise RuntimeError("MarekFS virtual drive is read-only in this window.")
        return self.write_record(source.removeprefix("marekfs:/").strip("/"), bytes(data))


class MusicPlayerWindow:
    """In-app music player with waveform visualizer, click-to-seek,
    auto-advance and keyboard shortcuts. Never opens the system player."""

    def __init__(self, parent, initial_name=None, initial_bytes=None, marekfs_source=None):
        self.win = tk.Toplevel(parent)
        theme_existing_window(self.win, parent, title="🎵 MarekFS Reader Music Player")
        self.win.geometry("760x620")
        self.win.protocol("WM_DELETE_WINDOW", self.close)
        self.marekfs_source = marekfs_source
        self.playlist = []
        self.virtual_export_path = None
        self.audio_tracks = []
        self.description = ""
        self.index = -1
        self.playing = False
        self.paused = False
        self.length = 0.0
        self._was_playing = False
        self._waveform = None
        self._waveform_loading = False
        self._wave_click_seek = False
        self.mixer = self._init_mixer()
        if self.mixer is None:
            messagebox.showerror(
                "pygame.mixer unavailable",
                "MarekFS Music Player needs pygame.mixer for in-app playback.\n\n"
                "Run:  pip install pygame\n\n"
                "The system player is intentionally NOT used.",
                parent=self.win,
            )
            self.win.after(10, self.close)
            return

        t = getattr(self.win, "_marekfs_theme", {})
        self._accent = t.get("accent", "#00d2ff")
        self._bg = t.get("bg", "#10131c")
        self._surface = t.get("surface", "#171c2b")
        self._fg = t.get("fg", "#e8ecf5")

        top = ttk.Frame(self.win, padding=8); top.pack(fill=tk.X)
        ttk.Button(top, text="➕ Add tracks…", command=self.add_tracks).pack(side=tk.LEFT, padx=4)
        ttk.Button(top, text="📦 Open Marekaudio…", command=self.open_container).pack(side=tk.LEFT, padx=4)
        ttk.Button(top, text="📂 Open from MarekFS", command=self.open_from_marekfs).pack(side=tk.LEFT, padx=4)
        ttk.Button(top, text="💾 Export Marekaudio…", command=self.export_container).pack(side=tk.LEFT, padx=4)
        ttk.Button(top, text="💾 Export to MarekFS", command=self.export_to_marekfs).pack(side=tk.LEFT, padx=4)
        ttk.Button(top, text="🗑️ Clear", command=self.clear).pack(side=tk.LEFT, padx=4)
        self.engine_var = tk.StringVar(value="Engine: pygame.mixer · in-app")
        ttk.Label(top, textvariable=self.engine_var).pack(side=tk.RIGHT, padx=6)

        # Playlist
        self.listbox = tk.Listbox(self.win, height=8, activestyle="dotbox", selectmode=tk.SINGLE,
                                  bg=self._bg, fg=self._fg, selectbackground=self._accent,
                                  selectforeground=self._bg, highlightthickness=0, bd=0)
        self.listbox.pack(fill=tk.BOTH, expand=True, padx=10, pady=6)
        self.listbox.bind("<Double-1>", lambda _e: self.play_selected())

        # Now playing + waveform
        self.now_var = tk.StringVar(value="Nothing playing")
        ttk.Label(self.win, textvariable=self.now_var, font=("Segoe UI", 11, "bold")).pack(anchor=tk.W, padx=12)

        self.wave_canvas = tk.Canvas(self.win, height=90, bg=self._bg, highlightthickness=0)
        self.wave_canvas.pack(fill=tk.X, padx=10, pady=(4, 0))
        self.wave_canvas.bind("<Button-1>", self._on_wave_click)
        self.wave_canvas.bind("<B1-Motion>", self._on_wave_click)

        # Seek bar
        seek = ttk.Frame(self.win, padding=(10, 4)); seek.pack(fill=tk.X)
        self.pos_var = tk.DoubleVar(value=0)
        self.seek_bar = ttk.Scale(seek, from_=0, to=100, variable=self.pos_var, command=self._on_seek)
        self.seek_bar.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.seek_bar.bind("<ButtonRelease-1>", lambda _e: self._commit_seek())
        self.time_var = tk.StringVar(value="00:00 / 00:00")
        ttk.Label(seek, textvariable=self.time_var, width=16).pack(side=tk.RIGHT, padx=8)

        # Controls
        controls = ttk.Frame(self.win, padding=8); controls.pack(fill=tk.X)
        for text, fn in (("⏮", self.prev_track), ("▶ Play", self.play_selected),
                         ("⏸ Pause", self.toggle_pause), ("⏹ Stop", self.stop),
                         ("⏭", self.next_track)):
            ttk.Button(controls, text=text, command=fn).pack(side=tk.LEFT, padx=3)
        ttk.Label(controls, text="Volume").pack(side=tk.LEFT, padx=(18, 4))
        self.vol_var = tk.DoubleVar(value=80)
        ttk.Scale(controls, from_=0, to=100, variable=self.vol_var,
                  command=lambda _v: self._apply_volume()).pack(side=tk.LEFT, fill=tk.X, expand=True)

        # Keyboard shortcuts
        self.win.bind("<space>", lambda _e: self.toggle_pause())
        self.win.bind("<Left>", lambda _e: self._seek_relative(-5))
        self.win.bind("<Right>", lambda _e: self._seek_relative(5))
        self.win.bind("<Up>", lambda _e: self._change_volume(5))
        self.win.bind("<Down>", lambda _e: self._change_volume(-5))
        self.win.bind("<n>", lambda _e: self.next_track())
        self.win.bind("<p>", lambda _e: self.prev_track())
        self.win.bind("<s>", lambda _e: self.stop())

        # Resize grip + minimize-to-corner on app switch
        self.win.minsize(600, 400)
        _add_resize_grip(self.win)
        _bind_minimize_on_focus_out(self.win, icon_text="🎵")
        self.win.focus_force()

        self._tick()
        if initial_name and initial_bytes is not None:
            if os.path.splitext(initial_name)[1].lower() == MAREKAUDIO_EXT:
                self._load_container_bytes(initial_bytes)
            else:
                self.add_bytes(initial_name, initial_bytes, autoplay=True)

    def _init_mixer(self):
        try:
            import pygame
            pygame.mixer.init()
            return pygame.mixer
        except Exception:
            return None

    def add_bytes(self, name, data, autoplay=False):
        path = _spill_to_temp(name, data)
        self.playlist.append((name, path))
        self.audio_tracks.append({"name": name, "path": path, "bitrate": None})
        self.listbox.insert(tk.END, f"🎵 {name} ({format_bytes(len(data))})")
        if autoplay:
            self.listbox.selection_set(tk.END); self.play_selected()

    def add_tracks(self):
        MediaFileSelector(self.win, self._add_selected_media, self.marekfs_source)

    def _add_selected_media(self, selected):
        for name, data, source in selected:
            if source.startswith("marekfs:"):
                self.add_bytes(name, data)
            else:
                self.playlist.append((name, source))
                self.audio_tracks.append({"name": name, "path": source, "bitrate": None})
                self.listbox.insert(tk.END, f"🎵 {name}")

    def _load_container_bytes(self, data):
        try:
            manifest, tracks = parse_marek_media(data)
            self.description = manifest.get("description", "")
            for track in tracks:
                self.add_bytes(track.get("name", "track"), track.get("data", b""))
                self.audio_tracks[-1].update({k: track[k] for k in ("bitrate", "language", "title") if k in track})
        except Exception as e:
            messagebox.showerror("Marekaudio", str(e), parent=self.win)

    def open_from_marekfs(self):
        if not self.marekfs_source:
            messagebox.showinfo("MarekFS", "Open Music Player from MarekFS Reader to connect the virtual drive.", parent=self.win); return
        MediaFileSelector(self.win, self._open_virtual_container, self.marekfs_source, extensions={MAREKAUDIO_EXT})

    def _open_virtual_container(self, selected):
        for name, data, source in selected[:1]:
            if data is not None:
                self.clear(); self._load_container_bytes(data); self.virtual_export_path = source

    def open_container(self):
        path = filedialog.askopenfilename(title="Open Marekaudio", filetypes=[("Marekaudio", "*.marekaudio")])
        if not path: return
        with open(path, "rb") as f: data = f.read()
        self.clear(); self._load_container_bytes(data)

    def _build_container_bytes(self):
        if not self.playlist:
            raise ValueError("Add at least one track first.")
        entries = []
        for i, (name, track_path) in enumerate(self.playlist):
            meta = self.audio_tracks[i] if i < len(self.audio_tracks) else {}
            entries.append({"name": name, "path": track_path, **{k: meta[k] for k in ("bitrate", "language", "title") if meta.get(k) is not None}})
        return create_marekaudio(entries, self.description)

    def export_to_marekfs(self):
        if not self.marekfs_source:
            messagebox.showinfo("MarekFS", "Open Music Player from MarekFS Reader to connect the virtual drive.", parent=self.win); return
        MediaFileSelector(self.win, self._save_virtual_container, self.marekfs_source, extensions={MAREKAUDIO_EXT}, folder_mode=True, suggested_name="audio.marekaudio")

    def _save_virtual_container(self, destination):
        try:
            data = self._build_container_bytes()
            self.marekfs_source.write_bytes(destination, data)
            messagebox.showinfo("Marekaudio", f"Exported to {destination}", parent=self.win)
        except Exception as e:
            messagebox.showerror("Marekaudio", str(e), parent=self.win)

    def export_container(self):
        if not self.playlist:
            messagebox.showinfo("Marekaudio", "Add at least one track first.", parent=self.win); return
        path = filedialog.asksaveasfilename(defaultextension=MAREKAUDIO_EXT, filetypes=[("Marekaudio", "*.marekaudio")])
        if not path: return
        entries = []
        for i, (name, track_path) in enumerate(self.playlist):
            meta = self.audio_tracks[i] if i < len(self.audio_tracks) else {}
            entries.append({"name": name, "path": track_path, **{k: meta[k] for k in ("bitrate", "language", "title") if meta.get(k) is not None}})
        with open(path, "wb") as f: f.write(create_marekaudio(entries, self.description))

    def clear(self):
        self.stop(); self.playlist.clear(); self.audio_tracks.clear(); self.listbox.delete(0, tk.END); self.index = -1
        self._waveform = None
        self._draw_waveform()

    def play_selected(self):
        selection = self.listbox.curselection()
        self.play_index(selection[0] if selection else (self.index if self.index >= 0 else 0))

    def play_index(self, index):
        if not 0 <= index < len(self.playlist): return
        self.index = index; name, path = self.playlist[index]
        self.listbox.selection_clear(0, tk.END); self.listbox.selection_set(index)
        self.now_var.set(f"▶ {name}")
        if not self.mixer:
            messagebox.showerror("Playback unavailable", "pygame.mixer is not available.\nThe system player is intentionally NOT used.", parent=self.win)
            return
        try:
            self.mixer.music.load(path); self._apply_volume(); self.mixer.music.play()
            self.playing = True; self.paused = False; self._was_playing = False
            self.length = self._probe_length(path)
            self._load_waveform_async(path)
        except Exception as e:
            messagebox.showerror("Playback failed", str(e), parent=self.win)

    def _probe_length(self, path):
        try: return self.mixer.Sound(path).get_length()
        except Exception: return 0.0

    def _load_waveform_async(self, path):
        if self._waveform_loading:
            return
        self._waveform_loading = True
        self._waveform = None
        self._draw_waveform()

        def work():
            points = _extract_waveform(path)
            self.win.after(0, lambda: self._waveform_done(points))

        threading.Thread(target=work, daemon=True).start()

    def _waveform_done(self, points):
        self._waveform_loading = False
        self._waveform = points
        self._draw_waveform()

    def _draw_waveform(self):
        try:
            self.wave_canvas.delete("all")
            w = self.wave_canvas.winfo_width() or 700
            h = self.wave_canvas.winfo_height() or 90
            mid = h // 2
            if self._waveform:
                n = len(self._waveform)
                if n > 0:
                    for i, (lo, hi) in enumerate(self._waveform):
                        x = i * w / n
                        x2 = (i + 1) * w / n
                        y1 = mid - (hi / 32768.0) * (h // 2 - 6)
                        y2 = mid - (lo / 32768.0) * (h // 2 - 6)
                        self.wave_canvas.create_rectangle(x, y1, x2, y2,
                                                          fill=self._accent, outline="")
            else:
                # Animated placeholder visualizer
                n = 48
                bar_w = w / n
                for i in range(n):
                    if self.playing and not self.paused:
                        amp = random.randint(4, h // 2 - 6)
                    else:
                        amp = 4
                    x = i * bar_w
                    self.wave_canvas.create_rectangle(x, mid - amp, x + bar_w * 0.7, mid + amp,
                                                      fill=self._accent, outline="")
        except Exception:
            pass

    def _on_wave_click(self, event):
        if not self.playing or self.length <= 0:
            return
        w = self.wave_canvas.winfo_width() or 700
        if w <= 0:
            return
        frac = max(0.0, min(1.0, event.x / w))
        self._wave_click_seek = True
        try:
            self.mixer.music.play(start=frac * self.length)
            self.pos_var.set(frac * 100)
        except Exception:
            pass

    def toggle_pause(self):
        if not self.mixer or not self.playing: return
        if self.paused:
            self.mixer.music.unpause(); self.paused = False
        else:
            self.mixer.music.pause(); self.paused = True

    def stop(self):
        if self.mixer:
            try: self.mixer.music.stop()
            except Exception: pass
        self.playing = False; self.paused = False; self._was_playing = False
        self.pos_var.set(0); self.now_var.set("Nothing playing")
        self.time_var.set("00:00 / 00:00")
        self._draw_waveform()

    def next_track(self):
        if self.playlist: self.play_index((self.index + 1) % len(self.playlist))

    def prev_track(self):
        if self.playlist: self.play_index((self.index - 1) % len(self.playlist))

    def _seek_relative(self, seconds):
        if not self.playing or self.length <= 0:
            return
        pos = max(0, self.mixer.music.get_pos() / 1000)
        new_pos = max(0.0, min(self.length, pos + seconds))
        try:
            self.mixer.music.play(start=new_pos)
        except Exception:
            pass

    def _change_volume(self, delta):
        new = max(0, min(100, int(self.vol_var.get()) + delta))
        self.vol_var.set(new)
        self._apply_volume()

    def _apply_volume(self):
        if self.mixer:
            try: self.mixer.music.set_volume(self.vol_var.get() / 100)
            except Exception: pass

    def _on_seek(self, _value):
        # Live preview while dragging; actual seek happens on release.
        pass

    def _commit_seek(self):
        if not self.mixer or not self.playing or self.length <= 0:
            return
        try:
            self.mixer.music.play(start=self.pos_var.get() / 100 * self.length)
        except Exception:
            pass

    def _tick(self):
        try:
            if self.mixer and self.playing and not self.paused:
                # Auto-advance when the current track finishes.
                if self.mixer.music.get_busy():
                    self._was_playing = True
                elif self._was_playing:
                    self._was_playing = False
                    self.win.after(50, self.next_track)
                pos = max(0, self.mixer.music.get_pos() / 1000)
                total = self.length or 0
                if total:
                    self.pos_var.set(min(100, pos / total * 100))
                self.time_var.set(f"{self._fmt(pos)} / {self._fmt(total)}")
            self._draw_waveform()
            self.win.after(200, self._tick)
        except Exception:
            pass

    @staticmethod
    def _fmt(seconds):
        seconds = int(max(0, seconds)); return f"{seconds // 60:02d}:{seconds % 60:02d}"

    def close(self):
        try:
            self.stop()
        except Exception:
            # The window may not have been fully initialised when the mixer
            # failed to start; stop() can reference attributes that never
            # got created, so swallow cleanly.
            pass
        try:
            self.win.destroy()
        except Exception:
            pass


class MediaFileSelector:
    def __init__(self, parent, on_select, marekfs_source=None, extensions=None, folder_mode=False, suggested_name=None):
        self.on_select = on_select; self.marekfs_source = marekfs_source; self.extensions = {x.lower() for x in (extensions or AUDIO_EXTS)}; self.folder_mode = folder_mode; self.suggested_name = suggested_name or ""
        self.current = "marekfs:/" if marekfs_source else os.path.expanduser("~")
        self.bookmarks = self._load_bookmarks(); self.entries = []; self.win = tk.Toplevel(parent)
        theme_existing_window(self.win, parent, title="🎵 Add audio tracks · MarekFS Reader")
        self.win.geometry("980x600"); self.path_var = tk.StringVar(value=self.current); self.filter_var = tk.StringVar()
        top = ttk.Frame(self.win, padding=8); top.pack(fill=tk.X)
        ttk.Label(top, text="MarekFS Reader media selector", style="Title.TLabel").pack(side=tk.LEFT)
        ttk.Entry(top, textvariable=self.path_var).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=8)
        ttk.Button(top, text="Go", command=self.go_path).pack(side=tk.LEFT); ttk.Button(top, text="Add bookmark", command=self.add_bookmark).pack(side=tk.LEFT, padx=4)
        body = ttk.Panedwindow(self.win, orient=tk.HORIZONTAL); body.pack(fill=tk.BOTH, expand=True, padx=8, pady=4)
        left = ttk.Frame(body, padding=6); right = ttk.Frame(body, padding=6); body.add(left, weight=1); body.add(right, weight=4)
        ttk.Label(left, text="Bookmarks / drives").pack(anchor=tk.W)
        ttk.Label(left, text="MarekFS Drive is always listed first", foreground="#00d2ff").pack(anchor=tk.W)
        self.bookmark_list = tk.Listbox(left, height=20); self.bookmark_list.pack(fill=tk.BOTH, expand=True, pady=5); self.bookmark_list.bind("<Double-1>", self.open_bookmark)
        self._refresh_bookmarks()
        controls = ttk.Frame(right); controls.pack(fill=tk.X); ttk.Button(controls, text="⬆ Parent", command=self.parent_dir).pack(side=tk.LEFT); ttk.Button(controls, text="🔄 Refresh", command=self.refresh).pack(side=tk.LEFT, padx=4)
        ttk.Label(controls, text="Filter:").pack(side=tk.LEFT, padx=(16, 4)); ttk.Entry(controls, textvariable=self.filter_var, width=22).pack(side=tk.LEFT); self.filter_var.trace_add("write", lambda *_: self.refresh())
        ttk.Label(right, text=("Choose a MarekFS destination folder and filename" if self.folder_mode else "Select one or more media files; double-click folders to browse")).pack(anchor=tk.W, pady=4)
        self.filename_var = tk.StringVar(value=self.suggested_name)
        if self.folder_mode:
            ttk.Entry(right, textvariable=self.filename_var).pack(fill=tk.X, pady=(0, 4))
        self.tree = ttk.Treeview(right, columns=("name", "type", "size", "source"), show="headings", selectmode="extended")
        for col, width in (("name", 280), ("type", 90), ("size", 110), ("source", 360)): self.tree.heading(col, text=col.title()); self.tree.column(col, width=width)
        self.tree.pack(fill=tk.BOTH, expand=True); self.tree.bind("<Double-1>", self.open_selected_folder)
        bottom = ttk.Frame(self.win, padding=8); bottom.pack(fill=tk.X); self.status = tk.StringVar(); ttk.Label(bottom, textvariable=self.status).pack(side=tk.LEFT)
        ttk.Button(bottom, text="Cancel", command=self.win.destroy).pack(side=tk.RIGHT, padx=4); ttk.Button(bottom, text="Add selected", style="Accent.TButton", command=self.accept).pack(side=tk.RIGHT)
        self.refresh()

    def _load_bookmarks(self):
        try:
            with open(BOOKMARKS_PATH, "r", encoding="utf-8") as f: values = json.load(f)
            return [p for p in values if isinstance(p, str) and os.path.isdir(p)]
        except Exception: return []

    def _save_bookmarks(self):
        try:
            with open(BOOKMARKS_PATH, "w", encoding="utf-8") as f: json.dump(self.bookmarks, f, indent=2)
        except Exception: pass

    def _refresh_bookmarks(self):
        self.bookmark_list.delete(0, tk.END); self._bookmark_values = []; seen = set()
        # Always show the virtual drive entry. When opened from MarekFS
        # Reader it is backed by the active disk adapter; otherwise clicking
        # it gives a clear message instead of silently hiding the option.
        defaults = [("MarekFS Drive /", "marekfs:/"), ("Home", os.path.expanduser("~")), ("Downloads", os.path.join(os.path.expanduser("~"), "Downloads")), ("Desktop", os.path.join(os.path.expanduser("~"), "Desktop"))]
        if sys.platform == "win32": defaults += [(f"Drive {letter}:", f"{letter}:\\") for letter in string.ascii_uppercase if os.path.exists(f"{letter}:\\")]
        for label, path in defaults + [(os.path.basename(p) or p, p) for p in self.bookmarks]:
            if path in seen or (not path.startswith("marekfs:") and not os.path.isdir(path)): continue
            seen.add(path); self._bookmark_values.append(path); self.bookmark_list.insert(tk.END, f"{label} — {path}")

    def add_bookmark(self):
        path = self.path_var.get().strip()
        if os.path.isdir(path) and path not in self.bookmarks: self.bookmarks.append(path); self._save_bookmarks(); self._refresh_bookmarks()

    def open_bookmark(self, _event=None):
        selection = self.bookmark_list.curselection()
        if not selection:
            return
        value = self._bookmark_values[selection[0]]
        if value == "marekfs:/" and not self.marekfs_source:
            self.status.set("Open Music Player from MarekFS Reader to browse the virtual MarekFS Drive.")
            return
        self.current = value; self.path_var.set(self.current); self.refresh()

    def go_path(self):
        value = self.path_var.get().strip()
        if value == "marekfs:/" and self.marekfs_source: self.current = value; self.refresh()
        elif os.path.isdir(value): self.current = os.path.abspath(value); self.refresh()
        else: self.status.set("Folder not found")

    def parent_dir(self):
        if not self.current.startswith("marekfs:"): self.current = os.path.dirname(self.current); self.path_var.set(self.current); self.refresh()

    def refresh(self):
        for item in self.tree.get_children(): self.tree.delete(item)
        self.entries = []; query = self.filter_var.get().lower().strip()
        if self.current.startswith("marekfs:") and self.marekfs_source: rows = self.marekfs_source.list_files(self.current, self.extensions)

        elif self.current.startswith("marekfs:"):
            self.status.set("Open Music Player from MarekFS Reader to connect the virtual drive.")
            return
        else:
            try: rows = [(n, os.path.join(self.current, n), os.path.isdir(os.path.join(self.current, n))) for n in os.listdir(self.current)]
            except Exception as e: self.status.set(str(e)); return
        for name, source, is_dir in sorted(rows, key=lambda x: str(x[0]).lower()):
            if query and query not in str(name).lower(): continue
            if not is_dir and os.path.splitext(str(name))[1].lower() not in self.extensions: continue
            size = "Folder" if is_dir else ("Virtual" if str(source).startswith("marekfs:") else format_bytes(os.path.getsize(source)))
            iid = self.tree.insert("", tk.END, values=(name, "Folder" if is_dir else "Audio", size, source)); self.entries.append((iid, name, source, is_dir))
        self.status.set(f"{len(self.entries)} entries · {self.current}")

    def open_selected_folder(self, _event=None):
        selection = self.tree.selection()
        if selection:
            row = next((x for x in self.entries if x[0] == selection[0]), None)
            if row and row[3]: self.current = row[2]; self.path_var.set(self.current); self.refresh()

    def accept(self):
        if self.folder_mode:
            if not self.current.startswith("marekfs:"):
                self.status.set("Choose a folder under MarekFS Drive /"); return
            filename = self.filename_var.get().strip()
            if not filename:
                self.status.set("Enter a destination filename"); return
            if not filename.lower().endswith(tuple(self.extensions)):
                self.status.set("Use a supported MarekFS container filename"); return
            self.on_select(self.current.rstrip("/") + "/" + filename); self.win.destroy(); return
        chosen = []
        for iid in self.tree.selection():
            row = next((x for x in self.entries if x[0] == iid), None)
            if not row or row[3]: continue
            _, name, source, _ = row
            chosen.append((name, self.marekfs_source.read_bytes(source) if str(source).startswith("marekfs:") else None, source))
        if not chosen: self.status.set("Select at least one audio file"); return
        self.on_select(chosen); self.win.destroy()


class VideoPlayerWindow:
    """In-app video player with seek bar, frame stepping and keyboard
    shortcuts. Uses OpenCV for decoding — never opens the system player."""

    def __init__(self, parent, name, data=None, path=None, marekfs_source=None):
        self.name = name; self.marekfs_source = marekfs_source; self.raw_data = data or (open(path, "rb").read() if path else b""); self.path = path or _spill_to_temp(name, self.raw_data); self.win = tk.Toplevel(parent)
        theme_existing_window(self.win, parent, title=f"🎬 MarekFS Video Player — {name}"); self.win.geometry("960x680"); self.win.protocol("WM_DELETE_WINDOW", self.close)
        self.canvas = tk.Canvas(self.win, bg="#000000", highlightthickness=0); self.canvas.pack(fill=tk.BOTH, expand=True)

        # Seek bar
        seek = ttk.Frame(self.win, padding=(10, 4)); seek.pack(fill=tk.X)
        self.seek_var = tk.DoubleVar(value=0)
        self.seek_bar = ttk.Scale(seek, from_=0, to=100, variable=self.seek_var, command=self._on_seek)
        self.seek_bar.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.seek_bar.bind("<ButtonRelease-1>", lambda _e: self._commit_seek())
        self.time_var = tk.StringVar(value="00:00 / 00:00")
        ttk.Label(seek, textvariable=self.time_var, width=16).pack(side=tk.RIGHT, padx=8)

        bar = ttk.Frame(self.win, padding=8); bar.pack(fill=tk.X)
        ttk.Button(bar, text="▶ Play", command=self.play).pack(side=tk.LEFT)
        ttk.Button(bar, text="⏸ Pause", command=self.pause).pack(side=tk.LEFT)
        ttk.Button(bar, text="⏹ Stop", command=self.stop).pack(side=tk.LEFT)
        ttk.Button(bar, text="⏪ -1 frame", command=lambda: self._step_frame(-1)).pack(side=tk.LEFT, padx=(8, 0))
        ttk.Button(bar, text="⏩ +1 frame", command=lambda: self._step_frame(1)).pack(side=tk.LEFT)
        ttk.Button(bar, text="📂 Open from MarekFS", command=self.open_from_marekfs).pack(side=tk.LEFT, padx=4)
        ttk.Button(bar, text="💾 Export to MarekFS", command=self.export_to_marekfs).pack(side=tk.LEFT, padx=4)
        self.status = tk.StringVar(value="Opening video…")
        ttk.Label(self.win, textvariable=self.status).pack(anchor=tk.W, padx=10)

        # Keyboard shortcuts
        self.win.bind("<space>", lambda _e: self.toggle_play_pause())
        self.win.bind("<Left>", lambda _e: self._step_frame(-1))
        self.win.bind("<Right>", lambda _e: self._step_frame(1))
        self.win.bind("<Up>", lambda _e: self._seek_relative(10))
        self.win.bind("<Down>", lambda _e: self._seek_relative(-10))

        # Resize grip + minimize-to-corner on app switch
        self.win.minsize(640, 400)
        _add_resize_grip(self.win)
        _bind_minimize_on_focus_out(self.win, icon_text="🎬")
        self.win.focus_force()

        self._cap = None; self._cv2 = None; self._playing = False; self._photo = None
        self._total = 0; self._fps = 25; self._seeking = False; self._variants = []
        self._open()

    def open_from_marekfs(self):
        if not self.marekfs_source:
            messagebox.showinfo("MarekFS", "Open Video Player from MarekFS Reader to connect the virtual drive.", parent=self.win); return
        MediaFileSelector(self.win, self._load_virtual_video, self.marekfs_source, extensions={MAREKVID_EXT})

    def _load_virtual_video(self, selected):
        if not selected: return
        name, data, _source = selected[0]
        if data is not None:
            self.name = name; self.raw_data = data; self.path = _spill_to_temp(name, data); self._open()

    def export_to_marekfs(self):
        if not self.marekfs_source:
            messagebox.showinfo("MarekFS", "Open Video Player from MarekFS Reader to connect the virtual drive.", parent=self.win); return
        MediaFileSelector(self.win, self._save_virtual_video, self.marekfs_source, extensions={MAREKVID_EXT}, folder_mode=True, suggested_name="video.marekvid")

    def _save_virtual_video(self, destination):
        try:
            self.marekfs_source.write_bytes(destination, self.raw_data)
            messagebox.showinfo("Marekvid", f"Exported to {destination}", parent=self.win)
        except Exception as e: messagebox.showerror("Marekvid", str(e), parent=self.win)

    def _open(self):
        try:
            import cv2; self._cv2 = cv2; self._cap = cv2.VideoCapture(self.path)
            self._total = int(self._cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
            self._fps = self._cap.get(cv2.CAP_PROP_FPS) or 25
            self.status.set(f"OpenCV in-app decoder · {self._total} frames · {self._fps:.1f} fps")
            self._show_frame()
            self._update_seek()
        except Exception:
            self.status.set("No in-app decoder available. Install opencv-python:  pip install opencv-python")

    def _show_frame(self):
        if not self._cap: return False
        ok, frame = self._cap.read()
        if not ok: return False
        Image, ImageTk, *_ = _pil()
        if Image is None: return False
        frame = self._cv2.cvtColor(frame, self._cv2.COLOR_BGR2RGB)
        image = Image.fromarray(frame)
        image.thumbnail((self.canvas.winfo_width() or 920, self.canvas.winfo_height() or 560), Image.LANCZOS)
        self._photo = ImageTk.PhotoImage(image)
        self.canvas.delete("all")
        self.canvas.create_image((self.canvas.winfo_width() or 920)//2, (self.canvas.winfo_height() or 560)//2, image=self._photo)
        return True

    def play(self):
        if not self._cap:
            self.status.set("No in-app decoder available. Install opencv-python:  pip install opencv-python")
            return
        self._playing = True
        self._loop()

    def pause(self):
        self._playing = False

    def toggle_play_pause(self):
        if self._playing:
            self.pause()
        else:
            self.play()

    def stop(self):
        self._playing = False
        if self._cap:
            self._cap.set(self._cv2.CAP_PROP_POS_FRAMES, 0)
            self._show_frame()
            self.seek_var.set(0)
            self.time_var.set(f"00:00 / {self._fmt(self._total / max(1, self._fps))}")

    def _loop(self):
        if self._playing and self._show_frame():
            self.win.after(int(1000 / max(1, self._fps)), self._loop)
        else:
            self._playing = False

    def _step_frame(self, delta):
        if not self._cap:
            return
        self._playing = False
        pos = self._cap.get(self._cv2.CAP_PROP_POS_FRAMES)
        new_pos = max(0, min(self._total - 1, pos + delta))
        self._cap.set(self._cv2.CAP_PROP_POS_FRAMES, new_pos)
        self._show_frame()
        self._update_seek()

    def _seek_relative(self, seconds):
        if not self._cap:
            return
        self._playing = False
        pos = self._cap.get(self._cv2.CAP_PROP_POS_FRAMES)
        new_pos = max(0, min(self._total - 1, pos + seconds * self._fps))
        self._cap.set(self._cv2.CAP_PROP_POS_FRAMES, new_pos)
        self._show_frame()
        self._update_seek()

    def _on_seek(self, _value):
        # Live preview while dragging; actual seek happens on release.
        pass

    def _commit_seek(self):
        if not self._cap:
            return
        self._playing = False
        total = self._total or 1
        frame = int(self.seek_var.get() / 100 * total)
        self._cap.set(self._cv2.CAP_PROP_POS_FRAMES, frame)
        self._show_frame()
        self._update_seek()

    def _update_seek(self):
        try:
            if self._cap:
                pos = self._cap.get(self._cv2.CAP_PROP_POS_FRAMES)
                total = self._total or 1
                if not self._seeking:
                    self.seek_var.set(min(100, pos / total * 100))
                self.time_var.set(f"{self._fmt(pos / max(1, self._fps))} / {self._fmt(total / max(1, self._fps))}")
            self.win.after(200, self._update_seek)
        except Exception:
            pass

    @staticmethod
    def _fmt(seconds):
        seconds = int(max(0, seconds)); return f"{seconds // 60:02d}:{seconds % 60:02d}"

    def close(self):
        self._playing = False
        if self._cap:
            try: self._cap.release()
            except Exception: pass
        self.win.destroy()


class ImageViewerWindow:
    def __init__(self, parent, name, data=None, path=None, on_edit=None):
        Image, ImageTk, *_ = _pil()
        if Image is None: messagebox.showerror("Pillow missing", "Install Pillow to view images.", parent=parent); return
        try: self.image = Image.open(io.BytesIO(data)) if data is not None else Image.open(path); self.image.load()
        except Exception as e: messagebox.showerror("Cannot open image", str(e), parent=parent); return
        self.ImageTk = ImageTk; self.name = name; self.on_edit = on_edit; self.win = tk.Toplevel(parent); theme_existing_window(self.win, parent, title=f"🖼️ MarekFS Image Viewer — {name}"); self.win.geometry("900x680"); self.win.minsize(500, 400); self.canvas = tk.Canvas(self.win, bg="#101010"); self.canvas.pack(fill=tk.BOTH, expand=True); self.canvas.bind("<Configure>", lambda _e: self.render()); _add_resize_grip(self.win, icon="◢"); _bind_minimize_on_focus_out(self.win, icon_text="🖼️"); self.win.focus_force(); self.win.after(80, self.render)
    def render(self):
        image = self.image.copy(); image.thumbnail((self.canvas.winfo_width() or 880, self.canvas.winfo_height() or 600)); self._photo = self.ImageTk.PhotoImage(image); self.canvas.delete("all"); self.canvas.create_image((self.canvas.winfo_width() or 880)//2, (self.canvas.winfo_height() or 600)//2, image=self._photo)


class ImageEditorWindow(ImageViewerWindow):
    def __init__(self, parent, name, data=None, image=None, save_callback=None):
        self.save_callback = save_callback; super().__init__(parent, name, data=data, image=image)


def open_media_for(parent, name, data, save_callback=None):
    ext = os.path.splitext(name)[1].lower()
    if ext in AUDIO_EXTS or ext == MAREKAUDIO_EXT: MusicPlayerWindow(parent, name, data); return True
    if ext in VIDEO_EXTS: VideoPlayerWindow(parent, name, data, marekfs_source=getattr(parent, "_marekfs_media_source", None)); return True
    if ext in IMAGE_EXTS: ImageViewerWindow(parent, name, data, on_edit=lambda im: ImageEditorWindow(parent, name, image=im, save_callback=save_callback)); return True
    return False