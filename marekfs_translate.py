"""MarekFS filename & content translator.

Translates a file's name and/or its text content between languages. Uses
`deep_translator` or `googletrans` when installed, otherwise falls back to a
direct HTTPS call to the public Google translate endpoint. Everything degrades
to a clear error message when no engine is reachable.
"""
import json
import os
import urllib.parse
import urllib.request
import threading
import tkinter as tk
from tkinter import ttk, messagebox

from ui_custom import theme_existing_window, stable_widget_width

LANGUAGES = {
    "auto": "Detect language",
    "en": "English", "pl": "Polish", "de": "German", "fr": "French",
    "es": "Spanish", "it": "Italian", "pt": "Portuguese", "nl": "Dutch",
    "sv": "Swedish", "no": "Norwegian", "da": "Danish", "fi": "Finnish",
    "cs": "Czech", "sk": "Slovak", "uk": "Ukrainian", "ru": "Russian",
    "tr": "Turkish", "el": "Greek", "ro": "Romanian", "hu": "Hungarian",
    "ar": "Arabic", "he": "Hebrew", "hi": "Hindi", "ja": "Japanese",
    "ko": "Korean", "zh-CN": "Chinese (Simplified)", "vi": "Vietnamese",
    "id": "Indonesian", "th": "Thai",
}

_MAX_CHUNK = 4500


def _translate_http(text, source, target):
    """Free Google translate endpoint (no API key)."""
    url = ("https://translate.googleapis.com/translate_a/single?client=gtx"
           f"&sl={urllib.parse.quote(source)}&tl={urllib.parse.quote(target)}"
           "&dt=t&q=" + urllib.parse.quote(text))
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 MarekFS"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        payload = json.loads(resp.read().decode("utf-8", errors="ignore"))
    return "".join(part[0] for part in payload[0] if part and part[0])


def translate_text(text, source="auto", target="en"):
    """Translate a string with the first engine that works."""
    if not text.strip():
        return text
    chunks = [text[i:i + _MAX_CHUNK] for i in range(0, len(text), _MAX_CHUNK)]
    errors = []

    try:
        from deep_translator import GoogleTranslator
        src = "auto" if source == "auto" else source
        return "".join(GoogleTranslator(source=src, target=target).translate(c) or "" for c in chunks)
    except Exception as e:
        errors.append(f"deep_translator: {e}")

    try:
        return "".join(_translate_http(c, source, target) for c in chunks)
    except Exception as e:
        errors.append(f"http: {e}")

    raise RuntimeError("No translation engine available.\n" + "\n".join(errors))


def translate_filename(filename, source="auto", target="en"):
    """Translate the name part of a path, keeping folders and the extension."""
    folder, base = os.path.split(filename)
    stem, ext = os.path.splitext(base)
    words = stem.replace("_", " ").replace("-", " ").strip()
    if not words:
        return filename
    translated = translate_text(words, source, target).strip()
    safe = "".join(c for c in translated if c not in '\\/:*?"<>|').strip() or stem
    new_base = safe + ext
    return f"{folder}/{new_base}" if folder else new_base


class TranslatorWindow:
    """Translate a MarekFS file's name and text content."""

    def __init__(self, parent, filename, content_bytes, rename_callback=None, save_callback=None):
        self.filename = filename
        self.rename_callback = rename_callback
        self.save_callback = save_callback
        try:
            self.original_text = content_bytes.decode("utf-8", errors="ignore")
        except Exception:
            self.original_text = ""

        self.win = tk.Toplevel(parent)
        theme_existing_window(self.win, parent)
        self.win.title(f"🌍 MarekFS Translator — {filename}")
        self.win.geometry("980x680")

        codes = list(LANGUAGES.keys())
        labels = [f"{LANGUAGES[c]} ({c})" for c in codes]
        self._codes = codes

        bar = ttk.Frame(self.win, padding=8)
        bar.pack(fill=tk.X)
        ttk.Label(bar, text="From:").pack(side=tk.LEFT, padx=(4, 2))
        self.src = ttk.Combobox(bar, values=labels, state="readonly", width=26)
        self.src.current(0)
        self.src.pack(side=tk.LEFT, padx=4)
        ttk.Label(bar, text="To:").pack(side=tk.LEFT, padx=(12, 2))
        self.dst = ttk.Combobox(bar, values=labels, state="readonly", width=26)
        self.dst.current(codes.index("en"))
        self.dst.pack(side=tk.LEFT, padx=4)
        ttk.Button(bar, text="🌍 Translate", style="Accent.TButton",
                   command=self.run_translation).pack(side=tk.LEFT, padx=14)
        self.busy = tk.StringVar(value="")
        ttk.Label(bar, textvariable=self.busy).pack(side=tk.LEFT, padx=8)

        name_frame = ttk.LabelFrame(self.win, text=" Filename ", padding=8)
        name_frame.pack(fill=tk.X, padx=12, pady=6)
        ttk.Label(name_frame, text=filename).pack(anchor=tk.W)
        self.new_name = tk.StringVar(value=filename)
        ttk.Entry(name_frame, textvariable=self.new_name, width=80).pack(anchor=tk.W, pady=4)
        self.rename_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(name_frame, text="Translate the filename too", variable=self.rename_var).pack(anchor=tk.W)

        panes = ttk.Frame(self.win)
        panes.pack(fill=tk.BOTH, expand=True, padx=12, pady=6)
        left = ttk.LabelFrame(panes, text=" Original content ", padding=6)
        left.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(0, 6))
        right = ttk.LabelFrame(panes, text=" Translated content ", padding=6)
        right.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.src_box = tk.Text(left, wrap="word", font=("Consolas", 10))
        self.src_box.pack(fill=tk.BOTH, expand=True)
        self.src_box.insert("1.0", self.original_text)
        self.dst_box = tk.Text(right, wrap="word", font=("Consolas", 10))
        self.dst_box.pack(fill=tk.BOTH, expand=True)

        foot = ttk.Frame(self.win, padding=8)
        foot.pack(fill=tk.X)
        ttk.Button(foot, text="💾 Apply to file", style="Accent.TButton",
                   command=self.apply).pack(side=tk.LEFT, padx=4)
        ttk.Button(foot, text="Close", command=self.win.destroy).pack(side=tk.RIGHT, padx=4)

    def _code(self, combo):
        return self._codes[combo.current()]

    def run_translation(self):
        source = self._code(self.src)
        target = self._code(self.dst)
        text = self.src_box.get("1.0", "end-1c")
        self.busy.set("Translating…")

        def work():
            try:
                translated = translate_text(text, source, target) if text.strip() else ""
                new_name = (translate_filename(self.filename, source, target)
                            if self.rename_var.get() else self.filename)
            except Exception as e:
                self.win.after(0, lambda: (self.busy.set(""), messagebox.showerror("Translation failed", str(e))))
                return

            def done():
                self.dst_box.delete("1.0", tk.END)
                self.dst_box.insert("1.0", translated)
                self.new_name.set(new_name)
                self.busy.set("Done ✅")
            self.win.after(0, done)

        threading.Thread(target=work, daemon=True).start()

    def apply(self):
        translated = self.dst_box.get("1.0", "end-1c")
        if self.save_callback and translated.strip():
            self.save_callback(translated.encode("utf-8"))
        new_name = self.new_name.get().strip()
        if self.rename_callback and new_name and new_name != self.filename:
            self.rename_callback(self.filename, new_name)
        messagebox.showinfo("Translator", "Changes applied to the file.")
        self.win.destroy()
