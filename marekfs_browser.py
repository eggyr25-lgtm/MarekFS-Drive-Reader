"""MarekFS Gecko browser launcher.

This module launches the locally installed Firefox/Firefox ESR with a separate
profile. The browser remains a real Gecko browser, so standard about: pages
such as about:preferences and about:downloads work normally. MarekFS does not
embed or execute web content itself.
"""
import json
import os
import shutil
import subprocess
import sys
import tkinter as tk
from tkinter import filedialog, messagebox
from tkinter import ttk

from ui_custom import theme_existing_window


APP_DIR = os.path.join(os.environ.get("PROGRAMDATA", os.path.expanduser("~")), "MarekFS")
PROFILE_DIR = os.path.join(APP_DIR, "gecko_profile")
CONFIG_PATH = os.path.join(APP_DIR, "gecko_browser.json")
DOWNLOAD_STAGING_DIR = os.path.join(APP_DIR, "browser_downloads")
DOWNLOAD_IMPORT_STATE_PATH = os.path.join(APP_DIR, "browser_download_imports.json")
# Kept as a compatibility alias; browser downloads no longer target host Downloads.
DOWNLOAD_DIR = DOWNLOAD_STAGING_DIR
BROWSER_ENVIRONMENT_NAME = "MarekFS Reader Browser Environment"
_PLATFORM_TOKEN = {"win32": "Windows", "darwin": "macOS"}.get(sys.platform, "Linux")
BROWSER_USER_AGENT = (f"Mozilla/5.0 ({BROWSER_ENVIRONMENT_NAME}; {_PLATFORM_TOKEN}) "
                      "Gecko/20100101 Firefox MarekFSReader/1.0")


def find_firefox():
    candidates = []
    if sys.platform == "win32":
        for root in (os.environ.get("PROGRAMFILES"), os.environ.get("PROGRAMFILES(X86)")):
            if root:
                candidates += [os.path.join(root, "Mozilla Firefox", "firefox.exe"),
                               os.path.join(root, "Firefox Developer Edition", "firefox.exe")]
    elif sys.platform == "darwin":
        candidates.append("/Applications/Firefox.app/Contents/MacOS/firefox")
    else:
        candidates += ["firefox", "firefox-esr"]
    for candidate in candidates:
        if os.path.isabs(candidate) and os.path.isfile(candidate):
            return candidate
        resolved = shutil.which(candidate)
        if resolved:
            return resolved
    return None


def load_config():
    config = {"download_dir": DOWNLOAD_STAGING_DIR, "virtual_download_root": "/Downloads",
              "profile_dir": PROFILE_DIR, "private_browsing": False, "homepage": "about:home"}
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            saved = json.load(f)
        if isinstance(saved, dict):
            config.update(saved)
    except Exception:
        pass
    # Virtual MarekFS downloads always use the private staging directory.
    config["download_dir"] = DOWNLOAD_STAGING_DIR
    config["virtual_download_root"] = "/Downloads"
    return config


def save_config(config):
    os.makedirs(APP_DIR, exist_ok=True)
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)
    os.replace(tmp, CONFIG_PATH)


def _preferences(config):
    os.makedirs(config["profile_dir"], exist_ok=True)
    os.makedirs(config["download_dir"], exist_ok=True)
    prefs = {
        "browser.download.folderList": 2,
        "browser.download.dir": os.path.abspath(config["download_dir"]),
        "browser.download.useDownloadDir": True,
        "browser.download.manager.showWhenStarting": False,
        "browser.download.alwaysOpenPanel": True,
        "browser.helperApps.alwaysAsk.force": False,
        "browser.shell.checkDefaultBrowser": False,
        "browser.startup.homepage": config.get("homepage", "about:home"),
        # Keep Firefox's normal compatibility tokens and append MarekFS Reader.
        "general.useragent.override": BROWSER_USER_AGENT,
        "general.appname.override": "MarekFS Reader",
        "general.appversion.override": "MarekFS Reader Browser Environment",
    }
    path = os.path.join(config["profile_dir"], "user.js")
    with open(path, "w", encoding="utf-8") as f:
        for key, value in prefs.items():
            encoded = json.dumps(value, ensure_ascii=False)
            f.write(f'user_pref({json.dumps(key)}, {encoded});\n')


ICON_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "MarekFS Browser Icon.png")


def launch(url="about:home", config=None):
    config = dict(config or load_config())
    config["download_dir"] = DOWNLOAD_STAGING_DIR
    config["virtual_download_root"] = "/Downloads"
    executable = find_firefox()
    if not executable:
        raise FileNotFoundError("Firefox or Firefox ESR was not found. Install Firefox to use the Gecko browser.")
    _preferences(config)
    save_config(config)
    args = [executable, "-no-remote", "-profile", config["profile_dir"], url or "about:home"]
    return subprocess.Popen(args, close_fds=(sys.platform != "win32"))


class MarekFSBrowserWindow:
    """Small MarekFS control panel for a real Gecko browser process."""
    def __init__(self, parent):
        self.parent = parent
        self.config = load_config()
        self.win = tk.Toplevel(parent)
        theme_existing_window(self.win, parent, title="🌐 MarekFS Reader Browser")
        self.win.geometry("620x210")
        self.win.title("🌐 MarekFS Reader Browser")
        self.win.resizable(True, True)
        try:
            if os.path.isfile(ICON_PATH):
                self._icon = tk.PhotoImage(file=ICON_PATH)
                self.win.iconphoto(True, self._icon)
        except Exception:
            self._icon = None
        self.win.protocol("WM_DELETE_WINDOW", self.win.destroy)

        body = ttk.Frame(self.win, padding=14)
        body.pack(fill=tk.BOTH, expand=True)
        ttk.Label(body, text="MarekFS Reader Browser", style="Title.TLabel").pack(anchor=tk.W)
        ttk.Label(body, text=f"{BROWSER_ENVIRONMENT_NAME} · Firefox/Firefox ESR · separate profile").pack(anchor=tk.W, pady=(2, 10))
        row = ttk.Frame(body)
        row.pack(fill=tk.X)
        self.url = tk.StringVar(value="about:home")
        entry = ttk.Entry(row, textvariable=self.url)
        entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 6))
        entry.bind("<Return>", lambda _e: self.open_browser())
        ttk.Button(row, text="Open", style="Accent.TButton", command=self.open_browser).pack(side=tk.RIGHT)
        controls = ttk.Frame(body)
        controls.pack(fill=tk.X, pady=12)
        ttk.Button(controls, text="about:preferences", command=lambda: self.open_special("about:preferences")).pack(side=tk.LEFT, padx=(0, 5))
        ttk.Button(controls, text="about:downloads", command=lambda: self.open_special("about:downloads")).pack(side=tk.LEFT, padx=5)
        ttk.Button(controls, text="Open download staging", command=self.open_downloads).pack(side=tk.LEFT, padx=5)
        self.status = tk.StringVar(value="Virtual downloads: /Downloads")
        ttk.Label(body, textvariable=self.status).pack(anchor=tk.W)

    def open_special(self, page):
        self.url.set(page)
        self.open_browser()

    def open_browser(self):
        try:
            launch(self.url.get().strip() or "about:home", self.config)
            self.status.set("Opened in Gecko · downloads import to virtual /Downloads")
        except Exception as e:
            messagebox.showerror("Gecko Browser", str(e), parent=self.win)

    def open_downloads(self):
        os.makedirs(self.config["download_dir"], exist_ok=True)
        try:
            if sys.platform == "win32":
                os.startfile(self.config["download_dir"])
            elif sys.platform == "darwin":
                subprocess.Popen(["open", self.config["download_dir"]])
            else:
                subprocess.Popen(["xdg-open", self.config["download_dir"]])
        except Exception as e:
            messagebox.showerror("Downloads", str(e), parent=self.win)


def main():
    root = tk.Tk()
    root.withdraw()
    window = MarekFSBrowserWindow(root)
    window.win.protocol("WM_DELETE_WINDOW", lambda: (window.win.destroy(), root.destroy()))
    root.mainloop()


if __name__ == "__main__":
    main()
