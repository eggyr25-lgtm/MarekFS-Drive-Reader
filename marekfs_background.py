"""Animated MarekFS background: supports static images, animated .gif and
video (.mp4/.webm/.mkv/.avi/.mov) wallpapers rendered behind the explorer.

Static images and GIFs use Pillow; video frames use OpenCV when available.
Falls back silently to no background when a decoder is missing.
"""
import os
import tkinter as tk
from tkinter import filedialog

GIF_EXTS = {".gif"}
VIDEO_EXTS = {".mp4", ".webm", ".mkv", ".avi", ".mov", ".m4v"}
STATIC_EXTS = {".png", ".jpg", ".jpeg", ".bmp", ".webp"}
ALL_EXTS = GIF_EXTS | VIDEO_EXTS | STATIC_EXTS


class AnimatedBackground:
    """Renders a still / GIF / video wallpaper onto a tk.Canvas."""

    def __init__(self, canvas: tk.Canvas):
        self.canvas = canvas
        self.path = None
        self.kind = None            # "static" | "gif" | "video"
        self.frames = []            # PhotoImage list for gif/static
        self.frame_index = 0
        self.delay = 80
        self._job = None
        self._cap = None
        self._cv2 = None
        self._photo = None
        self._size = (0, 0)
        self.overlay_text = "MarekFS Explorer"
        self.error = None

    # -- selection ---------------------------------------------------------
    def choose(self, _parent=None):
        path = filedialog.askopenfilename(
            title="Select background (image, GIF or video)",
            filetypes=[
                ("All backgrounds", "*.png *.jpg *.jpeg *.bmp *.webp *.gif *.mp4 *.webm *.mkv *.avi *.mov"),
                ("Animated GIF", "*.gif"),
                ("Video", "*.mp4 *.webm *.mkv *.avi *.mov"),
                ("Images", "*.png *.jpg *.jpeg *.bmp *.webp"),
                ("All files", "*.*"),
            ])
        if not path:
            return None
        self.load(path)
        return path

    def load(self, path):
        self.stop()
        self.path = path
        self.error = None
        ext = os.path.splitext(path)[1].lower()
        if ext in VIDEO_EXTS:
            self.kind = "video"
            self._open_video()
        elif ext in GIF_EXTS:
            self.kind = "gif"
        else:
            self.kind = "static"
        self._size = (0, 0)   # force reload at current canvas size
        self.start()

    def clear(self):
        self.stop()
        self.path = None
        self.kind = None
        self.frames = []
        self._photo = None
        try:
            self.canvas.delete("all")
        except Exception:
            pass

    # -- decoding ----------------------------------------------------------
    def _open_video(self):
        try:
            import cv2
            self._cv2 = cv2
            self._cap = cv2.VideoCapture(self.path)
            fps = self._cap.get(cv2.CAP_PROP_FPS) or 25.0
            self.delay = max(25, int(1000 / max(1.0, fps)))
        except Exception as e:
            self._cap = None
            self.error = f"Video backgrounds need opencv-python ({e})"

    def _load_frames(self, w, h):
        """(Re)build the PhotoImage frames for the current canvas size."""
        try:
            from PIL import Image, ImageSequence, ImageTk
        except Exception as e:
            self.error = f"Pillow is required for backgrounds ({e})"
            return
        try:
            im = Image.open(self.path)
        except Exception as e:
            self.error = str(e)
            return
        frames = []
        if self.kind == "gif":
            for frame in ImageSequence.Iterator(im):
                f = frame.convert("RGBA").resize((w, h), Image.LANCZOS)
                frames.append(ImageTk.PhotoImage(f))
            self.delay = max(30, im.info.get("duration", 80))
        else:
            frames.append(ImageTk.PhotoImage(im.convert("RGBA").resize((w, h), Image.LANCZOS)))
        self.frames = frames
        self.frame_index = 0
        self._size = (w, h)

    def _next_video_photo(self, w, h):
        if not self._cap:
            return None
        ok, frame = self._cap.read()
        if not ok:
            self._cap.set(self._cv2.CAP_PROP_POS_FRAMES, 0)
            ok, frame = self._cap.read()
            if not ok:
                return None
        try:
            from PIL import Image, ImageTk
        except Exception:
            return None
        frame = self._cv2.cvtColor(frame, self._cv2.COLOR_BGR2RGB)
        im = Image.fromarray(frame).resize((w, h), Image.LANCZOS)
        return ImageTk.PhotoImage(im)

    # -- rendering ---------------------------------------------------------
    def render_once(self):
        """Draw the current frame; safe to call on canvas <Configure>."""
        if not self.path:
            return
        w = max(1, self.canvas.winfo_width())
        h = max(1, int(self.canvas.winfo_height()) or 1)
        if self.kind == "video":
            photo = self._next_video_photo(w, h)
            if photo is None:
                return
            self._photo = photo
        else:
            if (w, h) != self._size or not self.frames:
                self._load_frames(w, h)
            if not self.frames:
                return
            self._photo = self.frames[self.frame_index % len(self.frames)]
            self.frame_index = (self.frame_index + 1) % len(self.frames)
        self.canvas.delete("all")
        self.canvas.create_image(0, 0, anchor=tk.NW, image=self._photo)
        if self.overlay_text:
            self.canvas.create_text(w // 2, h // 2, text=self.overlay_text,
                                    font=("Segoe UI", 14, "bold"), fill="#ffffff")

    def _tick(self):
        self.render_once()
        animated = self.kind in ("gif", "video") and (len(self.frames) > 1 or self.kind == "video")
        if animated:
            self._job = self.canvas.after(self.delay, self._tick)
        else:
            self._job = None

    def start(self):
        self.stop()
        if self.path:
            self._tick()

    def stop(self):
        if self._job:
            try:
                self.canvas.after_cancel(self._job)
            except Exception:
                pass
            self._job = None
        if self._cap:
            try:
                self._cap.release()
            except Exception:
                pass
            self._cap = None

    def describe(self):
        if not self.path:
            return "No background set — pick an image, GIF or MP4"
        if self.error:
            return f"Background: {os.path.basename(self.path)} — {self.error}"
        return f"Background ({self.kind}): {os.path.basename(self.path)}"
