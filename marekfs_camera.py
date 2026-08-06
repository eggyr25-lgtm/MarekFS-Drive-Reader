"""MarekFS Camera Integration — live viewfinder with snapshot, timer,
burst mode, and direct save into the Photos/ folder on the active MarekFS disk.

Requirements (auto-installed by the main app):  opencv-python, Pillow
"""

import io
import os
import time
import threading
import tkinter as tk
from tkinter import ttk, messagebox

# ---------------------------------------------------------------------------
# PIL / cv2 lazy imports (they are auto-installed by the main app on start-up)
# ---------------------------------------------------------------------------

def _load_deps():
    """Try to import cv2 and PIL. Returns (cv2, Image, ImageTk) or Nones."""
    try:
        import cv2 as _cv2
    except ImportError:
        _cv2 = None
    try:
        from PIL import Image as _Img, ImageTk as _ITk
    except ImportError:
        _Img = _ITk = None
    return _cv2, _Img, _ITk


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _probe_cameras(cv2, max_idx=5):
    """Return list of available camera indices (0-based)."""
    available = []
    for i in range(max_idx):
        try:
            cap = cv2.VideoCapture(i, cv2.CAP_DSHOW)
            if cap.isOpened():
                ret, _ = cap.read()
                if ret:
                    available.append(i)
            cap.release()
        except Exception:
            pass
        if not available and i == 0:
            # One more try without DSHOW flag (Linux / macOS)
            try:
                cap = cv2.VideoCapture(0)
                if cap.isOpened():
                    ret, _ = cap.read()
                    if ret:
                        available.append(0)
                cap.release()
            except Exception:
                pass
    return available if available else [0]   # always offer at least index 0


# ---------------------------------------------------------------------------
# CameraWindow
# ---------------------------------------------------------------------------

class CameraWindow:
    """Live camera viewfinder with snapshot, countdown timer, burst mode,
    and a recent-photos strip.  Photos are saved straight into the MarekFS
    Photos/ folder via *save_photo_fn*.

    Parameters
    ----------
    parent         : tk.Widget   — parent window / root
    save_photo_fn  : callable(filename: str, data: bytes) → None
                     Called when a photo is captured.  *filename* is the full
                     MarekFS path (e.g. "Photos/photo_20260806_123456_001.jpg").
    open_viewer_fn : callable(name: str, data: bytes) → None  (optional)
                     Opens the given image in the MarekFS Image Viewer.
    """

    RESOLUTIONS = [
        ("640 × 480  (VGA)",    640,  480),
        ("1280 × 720  (HD)",   1280,  720),
        ("1920 × 1080  (FHD)", 1920, 1080),
    ]
    TIMERS   = [("Off", 0), ("3 s", 3), ("5 s", 5), ("10 s", 10)]
    STRIP_H  = 80   # px height of the thumbnail strip
    STRIP_W  = 100  # px width per thumbnail cell
    MAX_STRIP = 12  # max recent photos shown

    def __init__(self, parent, save_photo_fn, open_viewer_fn=None):
        self.parent        = parent
        self.save_photo_fn = save_photo_fn
        self.open_viewer_fn = open_viewer_fn

        self._cv2, self._Image, self._ImageTk = _load_deps()
        if self._cv2 is None or self._Image is None:
            messagebox.showerror(
                "Camera",
                "opencv-python and Pillow are required.\n"
                "They will be installed automatically — please restart MarekFS.",
                parent=parent,
            )
            return

        self._cap          = None
        self._running      = False
        self._after_id     = None
        self._current_frame = None   # raw BGR frame from cv2
        self._photo_img    = None    # PhotoImage kept alive
        self._strip_imgs   = []      # [(PhotoImage, bytes, filename)] for strip
        self._photo_count  = 0
        self._countdown    = 0
        self._countdown_id = None
        self._burst_thread = None

        # ── Window ───────────────────────────────────────────────────────────
        self.win = tk.Toplevel(parent)
        try:
            from ui_custom import theme_existing_window
            theme_existing_window(self.win, parent, title="📷 MarekFS Camera")
        except Exception:
            pass
        self.win.title("📷 MarekFS Camera")
        self.win.geometry("860x720")
        self.win.resizable(True, True)
        self.win.protocol("WM_DELETE_WINDOW", self._close)

        self._build_ui()
        self.win.after(100, self._discover_cameras)

    # ── UI construction ───────────────────────────────────────────────────────

    def _build_ui(self):
        # Top toolbar
        toolbar = ttk.Frame(self.win, padding=(8, 6))
        toolbar.pack(fill=tk.X)

        ttk.Label(toolbar, text="📷 Camera", style="Title.TLabel").pack(
            side=tk.LEFT, padx=(0, 16)
        )

        # Camera selector
        ttk.Label(toolbar, text="Camera:").pack(side=tk.LEFT)
        self._cam_var = tk.StringVar(value="0")
        self._cam_combo = ttk.Combobox(
            toolbar, textvariable=self._cam_var, width=10, state="readonly"
        )
        self._cam_combo["values"] = ["0"]
        self._cam_combo.pack(side=tk.LEFT, padx=4)
        self._cam_combo.bind("<<ComboboxSelected>>", self._on_camera_change)

        ttk.Separator(toolbar, orient=tk.VERTICAL).pack(
            side=tk.LEFT, fill=tk.Y, padx=8
        )

        # Resolution selector
        ttk.Label(toolbar, text="Resolution:").pack(side=tk.LEFT)
        self._res_var = tk.StringVar(value=self.RESOLUTIONS[1][0])
        res_combo = ttk.Combobox(
            toolbar, textvariable=self._res_var,
            values=[r[0] for r in self.RESOLUTIONS], width=18, state="readonly",
        )
        res_combo.pack(side=tk.LEFT, padx=4)
        res_combo.bind("<<ComboboxSelected>>", self._on_res_change)

        ttk.Separator(toolbar, orient=tk.VERTICAL).pack(
            side=tk.LEFT, fill=tk.Y, padx=8
        )

        # Timer selector
        ttk.Label(toolbar, text="Timer:").pack(side=tk.LEFT)
        self._timer_var = tk.IntVar(value=0)
        for label, val in self.TIMERS:
            ttk.Radiobutton(
                toolbar, text=label, variable=self._timer_var, value=val
            ).pack(side=tk.LEFT, padx=2)

        # Viewfinder canvas
        self.canvas = tk.Canvas(self.win, bg="#0d1117", highlightthickness=0)
        self.canvas.pack(fill=tk.BOTH, expand=True, padx=8, pady=(4, 0))

        # Control row
        ctrl = ttk.Frame(self.win, padding=(8, 4))
        ctrl.pack(fill=tk.X)

        self._capture_btn = ttk.Button(
            ctrl, text="📷  Capture",
            style="Accent.TButton", command=self._trigger_capture,
        )
        self._capture_btn.pack(side=tk.LEFT, padx=4)

        ttk.Separator(ctrl, orient=tk.VERTICAL).pack(
            side=tk.LEFT, fill=tk.Y, padx=8
        )

        # Burst mode
        ttk.Label(ctrl, text="Burst:").pack(side=tk.LEFT)
        self._burst_var = tk.IntVar(value=1)
        ttk.Spinbox(
            ctrl, from_=1, to=20, width=4, textvariable=self._burst_var,
        ).pack(side=tk.LEFT, padx=2)
        ttk.Label(ctrl, text="photos").pack(side=tk.LEFT)

        ttk.Separator(ctrl, orient=tk.VERTICAL).pack(
            side=tk.LEFT, fill=tk.Y, padx=8
        )

        # Flip
        self._flip_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(ctrl, text="↔ Flip H", variable=self._flip_var).pack(
            side=tk.LEFT, padx=4
        )

        # Status
        self.status_var = tk.StringVar(value="Initialising camera…")
        ttk.Label(
            ctrl, textvariable=self.status_var, wraplength=440
        ).pack(side=tk.LEFT, padx=12)

        ttk.Button(ctrl, text="🔄 Restart Camera", command=self._restart_camera).pack(
            side=tk.RIGHT, padx=4
        )

        # Recent photos strip
        strip_outer = ttk.LabelFrame(self.win, text=" Recent photos (Photos/ folder) ", padding=4)
        strip_outer.pack(fill=tk.X, padx=8, pady=(4, 8))

        self._strip_frame = ttk.Frame(strip_outer)
        self._strip_frame.pack(fill=tk.X)

        self._strip_canvases = []   # list of tk.Canvas cells

    # ── Camera lifecycle ─────────────────────────────────────────────────────

    def _discover_cameras(self):
        """Probe for available cameras in a background thread."""
        self.status_var.set("Probing cameras…")

        def probe():
            indices = _probe_cameras(self._cv2, max_idx=5)
            def done():
                labels = [f"Camera {i}" for i in indices]
                self._cam_combo["values"] = labels
                self._cam_var.set(labels[0])
                self._start_camera(indices[0])
            self.win.after(0, done)

        threading.Thread(target=probe, daemon=True).start()

    def _start_camera(self, index):
        """Open cv2.VideoCapture for *index* and start the frame loop."""
        self._stop_frame_loop()
        if self._cap:
            try:
                self._cap.release()
            except Exception:
                pass
            self._cap = None

        # Try DirectShow first (Windows), then default backend
        cap = self._cv2.VideoCapture(index, self._cv2.CAP_DSHOW)
        if not cap.isOpened():
            cap = self._cv2.VideoCapture(index)
        if not cap.isOpened():
            self.status_var.set(
                f"Camera {index} could not be opened.  "
                "Check that it is connected and not used by another app."
            )
            return

        # Apply chosen resolution
        res_label = self._res_var.get()
        rw, rh = 1280, 720   # default HD
        for label, w, h in self.RESOLUTIONS:
            if label == res_label:
                rw, rh = w, h
                break
        cap.set(self._cv2.CAP_PROP_FRAME_WIDTH,  rw)
        cap.set(self._cv2.CAP_PROP_FRAME_HEIGHT, rh)
        cap.set(self._cv2.CAP_PROP_FPS, 30)

        actual_w = int(cap.get(self._cv2.CAP_PROP_FRAME_WIDTH))
        actual_h = int(cap.get(self._cv2.CAP_PROP_FRAME_HEIGHT))
        fps      = cap.get(self._cv2.CAP_PROP_FPS) or 30

        self._cap     = cap
        self._running = True
        self.status_var.set(
            f"Camera {index} — {actual_w}×{actual_h} @ {fps:.0f} fps  |  "
            "Click Capture to take a photo."
        )
        self._schedule_frame()

    def _stop_frame_loop(self):
        self._running = False
        if self._after_id is not None:
            try:
                self.win.after_cancel(self._after_id)
            except Exception:
                pass
            self._after_id = None

    def _restart_camera(self):
        label = self._cam_var.get()
        try:
            idx = int(label.replace("Camera", "").strip())
        except ValueError:
            idx = 0
        self._start_camera(idx)

    def _on_camera_change(self, _event=None):
        label = self._cam_var.get()
        try:
            idx = int(label.replace("Camera", "").strip())
        except ValueError:
            idx = 0
        self._start_camera(idx)

    def _on_res_change(self, _event=None):
        self._restart_camera()

    def _close(self):
        self._stop_frame_loop()
        if self._countdown_id:
            try:
                self.win.after_cancel(self._countdown_id)
            except Exception:
                pass
        if self._cap:
            try:
                self._cap.release()
            except Exception:
                pass
        self.win.destroy()

    # ── Frame rendering ───────────────────────────────────────────────────────

    def _schedule_frame(self):
        self._after_id = self.win.after(33, self._update_frame)   # ≈30 fps

    def _update_frame(self):
        if not self._running or not self._cap:
            return
        try:
            ret, frame = self._cap.read()
        except Exception:
            ret, frame = False, None

        if ret and frame is not None:
            if self._flip_var.get():
                frame = self._cv2.flip(frame, 1)
            self._current_frame = frame

            cw = max(self.canvas.winfo_width(),  320)
            ch = max(self.canvas.winfo_height(), 240)
            rgb  = self._cv2.cvtColor(frame, self._cv2.COLOR_BGR2RGB)
            img  = self._Image.fromarray(rgb)
            img.thumbnail((cw, ch), self._Image.NEAREST)
            self._photo_img = self._ImageTk.PhotoImage(img)
            self.canvas.delete("all")
            self.canvas.create_image(cw // 2, ch // 2, image=self._photo_img)

            # Countdown overlay
            if self._countdown > 0:
                self.canvas.create_text(
                    cw // 2, ch // 2,
                    text=str(self._countdown),
                    font=("Segoe UI", 96, "bold"),
                    fill="#FFD700",
                )

        self._schedule_frame()

    # ── Capture ───────────────────────────────────────────────────────────────

    def _trigger_capture(self):
        """Start countdown (if set) then shoot *burst* photos."""
        delay = self._timer_var.get()
        if delay > 0:
            self._countdown = delay
            self._capture_btn.configure(state="disabled")
            self._countdown_tick()
        else:
            self._do_burst()

    def _countdown_tick(self):
        if self._countdown <= 0:
            self._capture_btn.configure(state="normal")
            self.status_var.set("📷 Capturing…")
            self._do_burst()
            return
        self.status_var.set(f"📷 Taking photo in {self._countdown}…")
        self._countdown -= 1
        self._countdown_id = self.win.after(1000, self._countdown_tick)

    def _do_burst(self):
        """Shoot *burst* photos (≥1) with a short inter-frame delay."""
        count = max(1, min(20, self._burst_var.get()))
        threading.Thread(
            target=self._burst_worker, args=(count,), daemon=True
        ).start()

    def _burst_worker(self, count):
        saved = []
        for i in range(count):
            frame = self._current_frame
            if frame is None:
                time.sleep(0.1)
                continue
            if count > 1:
                time.sleep(0.08)   # small gap between burst shots
                frame = self._current_frame  # re-grab latest frame
            data, fname = self._encode_frame(frame)
            if data:
                saved.append((fname, data))
        def done():
            for fname, data in saved:
                self._persist_photo(fname, data)
            if saved:
                self.status_var.set(
                    f"✔ {len(saved)} photo(s) saved to Photos/"
                )
            self._capture_btn.configure(state="normal")
        self.win.after(0, done)

    def _encode_frame(self, frame):
        """Encode *frame* (BGR ndarray) to JPEG bytes and pick a unique filename."""
        self._photo_count += 1
        ts    = time.strftime("%Y%m%d_%H%M%S")
        fname = f"Photos/photo_{ts}_{self._photo_count:04d}.jpg"
        try:
            ok, buf = self._cv2.imencode(
                ".jpg", frame,
                [self._cv2.IMWRITE_JPEG_QUALITY, 95],
            )
            if not ok:
                return None, fname
            return bytes(buf), fname
        except Exception as e:
            self.win.after(
                0,
                lambda: self.status_var.set(f"Encode error: {e}"),
            )
            return None, fname

    def _persist_photo(self, fname, data):
        """Save *data* via the callback and add thumbnail to the strip."""
        try:
            self.save_photo_fn(fname, data)
        except Exception as e:
            messagebox.showerror(
                "Save Error",
                f"Could not save photo to MarekFS:\n{e}",
                parent=self.win,
            )
            return
        self._add_strip_thumb(fname, data)

    # ── Recent-photos strip ───────────────────────────────────────────────────

    def _add_strip_thumb(self, fname, data):
        """Add a thumbnail cell to the recent-photos strip."""
        try:
            img = self._Image.open(io.BytesIO(data))
            img.thumbnail((self.STRIP_W - 4, self.STRIP_H - 4), self._Image.LANCZOS)
            photo = self._ImageTk.PhotoImage(img)
        except Exception:
            photo = None

        # Keep the most recent MAX_STRIP entries
        self._strip_imgs.insert(0, (photo, data, fname))
        if len(self._strip_imgs) > self.MAX_STRIP:
            self._strip_imgs = self._strip_imgs[:self.MAX_STRIP]

        self._rebuild_strip()

    def _rebuild_strip(self):
        """Re-render all strip thumbnails after a change."""
        for widget in self._strip_frame.winfo_children():
            widget.destroy()
        self._strip_canvases = []

        for idx, (photo, data, fname) in enumerate(self._strip_imgs):
            cell = ttk.Frame(self._strip_frame, borderwidth=2, relief="groove")
            cell.pack(side=tk.LEFT, padx=2, pady=2)

            c = tk.Canvas(
                cell, width=self.STRIP_W, height=self.STRIP_H,
                bg="#0d1117", highlightthickness=0, cursor="hand2",
            )
            c.pack()
            if photo:
                c.create_image(
                    self.STRIP_W // 2, self.STRIP_H // 2, image=photo
                )
            # Tooltip label
            short = os.path.basename(fname)
            ttk.Label(cell, text=short, font=("Segoe UI", 7), wraplength=96).pack()

            # Click → open in viewer
            _data = data    # capture for closure
            _name = fname
            c.bind(
                "<Button-1>",
                lambda _e, d=_data, n=_name: self._open_in_viewer(n, d),
            )
            c.tag_bind("all", "<Button-1>",
                lambda _e, d=_data, n=_name: self._open_in_viewer(n, d))
            self._strip_canvases.append(c)

    def _open_in_viewer(self, fname, data):
        """Open a captured photo in the MarekFS Image Viewer (if available)."""
        if self.open_viewer_fn:
            try:
                self.open_viewer_fn(os.path.basename(fname), data)
            except Exception as e:
                messagebox.showerror(
                    "Viewer Error", str(e), parent=self.win
                )
        else:
            # Fallback: use PIL's own show
            try:
                img = self._Image.open(io.BytesIO(data))
                img.show(title=os.path.basename(fname))
            except Exception as e:
                messagebox.showerror(
                    "Cannot open image", str(e), parent=self.win
                )
