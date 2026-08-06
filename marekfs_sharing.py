"""MarekFS Sharing — Bluetooth device scanner/sender and WiFi Disk
server/client with MarekFSWiFiDisk:/ address scheme.

Bluetooth:
  * bt_enumerate_devices()   – PowerShell + ctypes fallback
  * BluetoothShareWindow     – pick device, send file; recipient gets Accept/Decline

WiFi Disks:
  * WiFiDiskServer           – UDP broadcast beacon + TCP file server
  * WiFiDisksWindow          – network scanner + MarekFSWiFiDisk:/NAME address bar
"""

import os
import re
import json
import time
import socket
import struct
import tempfile
import threading
import subprocess
import tkinter as tk
from tkinter import ttk, messagebox, filedialog

# ---------------------------------------------------------------------------
# Bluetooth – device enumeration
# ---------------------------------------------------------------------------

def _bt_enumerate_powershell():
    """Return [{name, address}] via PowerShell Get-PnpDevice (Windows 8+)."""
    try:
        ps = (
            "Get-PnpDevice -Class Bluetooth -Status OK 2>$null "
            "| Select-Object FriendlyName,InstanceId "
            "| ConvertTo-Json -Compress"
        )
        r = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
            capture_output=True, text=True, timeout=14,
            creationflags=0x08000000,   # CREATE_NO_WINDOW
        )
        if r.returncode != 0 or not r.stdout.strip():
            return []
        raw = json.loads(r.stdout.strip())
        if isinstance(raw, dict):
            raw = [raw]
        devices = []
        for item in raw:
            name = (item.get("FriendlyName") or "").strip()
            iid  = (item.get("InstanceId")   or "")
            if not name:
                continue
            # Skip host-adapter entries
            nl = name.lower()
            if ("adapter" in nl or "enumerator" in nl) and "bluetooth" in nl:
                continue
            # Extract BT address from InstanceId: BTHENUM\DEV_XXXXXXXXXXXX\...
            m = re.search(r"DEV_([0-9A-Fa-f]{12})", iid, re.IGNORECASE)
            addr = ""
            if m:
                h = m.group(1).upper()
                addr = ":".join(h[i:i+2] for i in range(0, 12, 2))
            devices.append({"name": name, "address": addr})
        return devices
    except Exception:
        return []


def _bt_enumerate_ctypes():
    """Return [{name, address}] via bthprops.dll (always available on Windows)."""
    try:
        import ctypes
        import ctypes.wintypes as wt
        bth = ctypes.WinDLL("bthprops.dll")
    except Exception:
        return []

    class _SYSTEMTIME(ctypes.Structure):
        _fields_ = [("data", ctypes.c_uint16 * 8)]

    class _BT_DEVICE_INFO(ctypes.Structure):
        _fields_ = [
            ("dwSize",          ctypes.wintypes.DWORD),
            ("Address",         ctypes.c_uint64),
            ("ulClassofDevice", ctypes.wintypes.ULONG),
            ("fConnected",      ctypes.wintypes.BOOL),
            ("fRemembered",     ctypes.wintypes.BOOL),
            ("fAuthenticated",  ctypes.wintypes.BOOL),
            ("stLastSeen",      _SYSTEMTIME),
            ("stLastUsed",      _SYSTEMTIME),
            ("szName",          ctypes.c_wchar * 248),
        ]

    class _BT_DEVICE_SEARCH_PARAMS(ctypes.Structure):
        _fields_ = [
            ("dwSize",               ctypes.wintypes.DWORD),
            ("fReturnAuthenticated", ctypes.wintypes.BOOL),
            ("fReturnRemembered",    ctypes.wintypes.BOOL),
            ("fReturnUnknown",       ctypes.wintypes.BOOL),
            ("fReturnConnected",     ctypes.wintypes.BOOL),
            ("fIssueInquiry",        ctypes.wintypes.BOOL),
            ("cTimeoutMultiplier",   ctypes.c_ubyte),
            ("hRadio",               ctypes.wintypes.HANDLE),
        ]

    params = _BT_DEVICE_SEARCH_PARAMS()
    params.dwSize = ctypes.sizeof(_BT_DEVICE_SEARCH_PARAMS)
    params.fReturnAuthenticated = True
    params.fReturnRemembered    = True
    params.fReturnUnknown       = False
    params.fReturnConnected     = True
    params.fIssueInquiry        = False
    params.cTimeoutMultiplier   = 4
    params.hRadio               = None

    info = _BT_DEVICE_INFO()
    info.dwSize = ctypes.sizeof(_BT_DEVICE_INFO)

    try:
        handle = bth.BluetoothFindFirstDevice(
            ctypes.byref(params), ctypes.byref(info)
        )
    except Exception:
        return []
    if not handle:
        return []

    devices = []
    try:
        while True:
            addr_int  = info.Address
            addr_bytes = struct.pack("<Q", addr_int)[:6]
            addr_str  = ":".join(f"{b:02X}" for b in reversed(addr_bytes))
            name = (info.szName or "").strip()
            if name:
                devices.append({"name": name, "address": addr_str})
            next_info = _BT_DEVICE_INFO()
            next_info.dwSize = ctypes.sizeof(_BT_DEVICE_INFO)
            if not bth.BluetoothFindNextDevice(handle, ctypes.byref(next_info)):
                break
            info = next_info
    finally:
        try:
            bth.BluetoothFindDeviceClose(handle)
        except Exception:
            pass
    return devices


def bt_enumerate_devices():
    """Enumerate nearby/paired Bluetooth devices. Returns [{name, address}]."""
    devices = _bt_enumerate_powershell()
    if not devices:
        devices = _bt_enumerate_ctypes()
    seen, out = set(), []
    for d in devices:
        key = d["address"] or d["name"]
        if key not in seen:
            seen.add(key)
            out.append(d)
    return out


def bt_send_file(parent_win, device_name, filename, data):
    """Extract *data* to a temp file then open the Windows Bluetooth File
    Transfer Wizard (fsquirt.exe).  The wizard shows an Accept/Decline prompt
    on the receiving device — the recipient MUST accept before any bytes
    arrive on their machine."""
    tmp_path = os.path.join(tempfile.gettempdir(), filename)
    try:
        with open(tmp_path, "wb") as fh:
            fh.write(data)
    except Exception as e:
        messagebox.showerror(
            "Bluetooth Send", f"Could not write temp file:\n{e}", parent=parent_win
        )
        return False
    try:
        subprocess.Popen(["fsquirt.exe", "/send"], shell=True)
    except Exception as e:
        messagebox.showerror(
            "Bluetooth Send",
            f"Could not launch the Bluetooth wizard:\n{e}\n\n"
            f"The file has been saved to:\n{tmp_path}",
            parent=parent_win,
        )
        return False
    messagebox.showinfo(
        "Bluetooth Send — Action Required",
        f"The Windows Bluetooth File Transfer wizard has opened.\n\n"
        f"  File : {filename}  ({len(data):,} bytes)\n"
        f"  Temp : {tmp_path}\n\n"
        f"In the wizard:\n"
        f"  1. Select \"{device_name}\" as the destination.\n"
        f"  2. The recipient will see an  Accept / Decline  prompt.\n"
        f"     File transfer only proceeds after they accept.",
        parent=parent_win,
    )
    return True


# ---------------------------------------------------------------------------
# Bluetooth – window
# ---------------------------------------------------------------------------

class BluetoothShareWindow:
    """Pick a Bluetooth device and send a MarekFS file to it.
    The receiving device must explicitly accept the incoming transfer."""

    def __init__(self, parent, filename=None, file_data=None):
        self.parent    = parent
        self.filename  = filename  or ""
        self.file_data = file_data or b""
        self._devices  = []

        self.win = tk.Toplevel(parent)
        try:
            from ui_custom import theme_existing_window
            theme_existing_window(self.win, parent, title="📡 Bluetooth Share")
        except Exception:
            pass
        self.win.title("📡 Bluetooth Share")
        self.win.geometry("580x500")
        self.win.resizable(True, True)

        body = ttk.Frame(self.win, padding=14)
        body.pack(fill=tk.BOTH, expand=True)

        ttk.Label(body, text="Bluetooth Share", style="Title.TLabel").pack(anchor=tk.W)
        ttk.Label(
            body,
            text=(
                "Select a Bluetooth device below and click Send File.\n"
                "The recipient will see an  Accept / Decline  prompt and must accept."
            ),
            wraplength=530,
        ).pack(anchor=tk.W, pady=(4, 12))

        # File info
        ff = ttk.LabelFrame(body, text=" File to send ", padding=8)
        ff.pack(fill=tk.X, pady=(0, 8))
        self.file_var = tk.StringVar(value=self.filename or "(no file selected)")
        ttk.Label(ff, textvariable=self.file_var, wraplength=510).pack(anchor=tk.W)
        size_txt = f"{len(self.file_data):,} bytes" if self.file_data else "—"
        ttk.Label(ff, text=f"Size: {size_txt}").pack(anchor=tk.W)

        # Device list
        df = ttk.LabelFrame(body, text=" Bluetooth devices ", padding=8)
        df.pack(fill=tk.BOTH, expand=True)

        cols = ("Device name", "Bluetooth address", "Status")
        self.tree = ttk.Treeview(df, columns=cols, show="headings", height=8)
        for c, w in zip(cols, (240, 160, 120)):
            self.tree.heading(c, text=c)
            self.tree.column(c, width=w)
        sb = ttk.Scrollbar(df, command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        self.tree.bind("<<TreeviewSelect>>", self._on_select)

        self.status_var = tk.StringVar(value="Click Scan to discover paired Bluetooth devices.")
        ttk.Label(body, textvariable=self.status_var, wraplength=530).pack(
            anchor=tk.W, pady=(8, 0)
        )

        btns = ttk.Frame(body)
        btns.pack(fill=tk.X, pady=(10, 0))
        ttk.Button(btns, text="🔍 Scan for Devices", command=self._scan).pack(
            side=tk.LEFT, padx=4
        )
        self.send_btn = ttk.Button(
            btns, text="📡 Send File", style="Accent.TButton",
            command=self._send, state="disabled",
        )
        self.send_btn.pack(side=tk.LEFT, padx=4)
        ttk.Button(btns, text="Close", command=self.win.destroy).pack(
            side=tk.RIGHT, padx=4
        )

    # -- helpers --

    def _scan(self):
        self.status_var.set("Scanning… please wait.")
        self.send_btn.configure(state="disabled")
        for i in self.tree.get_children():
            self.tree.delete(i)
        threading.Thread(target=self._do_scan, daemon=True).start()

    def _do_scan(self):
        devices = bt_enumerate_devices()

        def done():
            self._devices = devices
            for d in devices:
                self.tree.insert("", tk.END, values=(
                    d["name"],
                    d["address"] or "—",
                    "Paired / Available",
                ))
            if devices:
                self.status_var.set(
                    f"Found {len(devices)} device(s).  Select one and click Send File."
                )
            else:
                self.status_var.set(
                    "No Bluetooth devices found.  Make sure devices are paired and nearby."
                )

        self.win.after(0, done)

    def _on_select(self, _event):
        has_file = bool(self.filename and self.file_data)
        has_sel  = bool(self.tree.selection())
        self.send_btn.configure(state="normal" if (has_file and has_sel) else "disabled")

    def _send(self):
        sel = self.tree.selection()
        if not sel:
            messagebox.showwarning("No Device", "Select a device first.", parent=self.win)
            return
        if not self.filename or not self.file_data:
            messagebox.showwarning("No File", "No file loaded to send.", parent=self.win)
            return
        idx    = self.tree.index(sel[0])
        device = self._devices[idx] if idx < len(self._devices) else {}
        bt_send_file(
            self.win, device.get("name", "Unknown"),
            self.filename, self.file_data,
        )


# ---------------------------------------------------------------------------
# WiFi Disk – constants & protocol helpers
# ---------------------------------------------------------------------------

WIFI_DISK_DISCOVERY_PORT = 57234
WIFI_DISK_PROTOCOL       = "MAREKFS_WIFI_1.0"


def _wifi_parse_address(address: str):
    """Parse a MarekFSWiFiDisk address string.

    Accepted forms
    --------------
    MarekFSWiFiDisk:/DiskName
    MarekFSWiFiDisk://host:port/DiskName
    Just a plain disk name (no scheme)

    Returns a dict with keys ``name``, ``host`` (may be None), ``port`` (may be None).
    """
    s = address.strip()
    # Full form with host:port
    m = re.match(r"(?i)marekfswifidisk://([^:/]+):(\d+)/(.+)", s)
    if m:
        return {"host": m.group(1), "port": int(m.group(2)), "name": m.group(3)}
    # Name-only form
    m = re.match(r"(?i)marekfswifidisk:/(.+)", s)
    if m:
        return {"host": None, "port": None, "name": m.group(1)}
    # Bare name (no scheme prefix)
    if s and not s.startswith("/"):
        return {"host": None, "port": None, "name": s}
    return None


def _wifi_connect(host, port, command, timeout=15.0):
    """Send *command* to a WiFi-disk TCP server and return the raw response bytes."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect((host, port))
        s.sendall((command + "\n").encode("utf-8"))
        # LIST and INFO: 4-byte big-endian length prefix
        if command in ("LIST", "INFO"):
            hdr = _recv_exact(s, 4)
            size = int.from_bytes(hdr, "big")
            return _recv_exact(s, size)
        # GET <name>: 8-byte big-endian length prefix
        if command.startswith("GET "):
            hdr = _recv_exact(s, 8)
            size = int.from_bytes(hdr, "big")
            return _recv_exact(s, size)
    finally:
        try:
            s.close()
        except Exception:
            pass
    return b""


def _recv_exact(sock, n):
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(min(65536, n - len(buf)))
        if not chunk:
            raise ConnectionError("WiFi Disk: connection closed before all bytes arrived")
        buf += chunk
    return buf


def _wifi_discover_disks(timeout=3.0):
    """Listen for UDP beacons; return [{name, host, port, hostname}]."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind(("", WIFI_DISK_DISCOVERY_PORT))
    except Exception:
        sock.close()
        return []
    sock.settimeout(0.25)
    deadline = time.monotonic() + timeout
    seen = {}
    while time.monotonic() < deadline:
        try:
            data, addr = sock.recvfrom(4096)
        except socket.timeout:
            continue
        except Exception:
            break
        text = data.decode("utf-8", "ignore")
        if not text.startswith(WIFI_DISK_PROTOCOL):
            continue
        d = {}
        for line in text.splitlines()[1:]:
            if ":" in line:
                k, _, v = line.partition(":")
                d[k.strip()] = v.strip()
        name     = d.get("NAME", "")
        host     = d.get("HOST", addr[0])
        port_str = d.get("PORT", "0")
        hostname = d.get("HOSTNAME", host)
        try:
            port = int(port_str)
        except ValueError:
            continue
        if name and port:
            key = f"{host}:{port}"
            seen[key] = {
                "name": name, "host": host,
                "port": port, "hostname": hostname,
            }
    sock.close()
    return list(seen.values())


# ---------------------------------------------------------------------------
# WiFi Disk – server
# ---------------------------------------------------------------------------

class WiFiDiskServer:
    """Advertise a MarekFS disk over the LAN and serve files on request.

    Discovery : UDP broadcast on WIFI_DISK_DISCOVERY_PORT every 5 seconds.
    Transfer  : length-prefixed JSON/binary protocol over a TCP socket on a
                dynamically chosen port.

    Parameters
    ----------
    disk_name        : str — human-readable name advertised on the network.
    get_entries_fn   : callable() → list[dict]
                       Each dict must have at least ``name`` and ``size``.
    read_file_fn     : callable(name: str) → bytes | None
                       Return the file's raw bytes by name, or None on error.
    """

    def __init__(self, disk_name, get_entries_fn, read_file_fn):
        self.disk_name    = disk_name
        self.get_entries  = get_entries_fn
        self.read_file    = read_file_fn
        self._running     = False
        self._tcp_port    = 0

    def start(self):
        if self._running:
            return
        self._running = True
        threading.Thread(target=self._tcp_server, daemon=True).start()

    def stop(self):
        self._running = False

    # -- internal --

    def _tcp_server(self):
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("", 0))
        self._tcp_port = srv.getsockname()[1]
        srv.listen(8)
        srv.settimeout(1.0)
        threading.Thread(target=self._udp_beacon, daemon=True).start()
        while self._running:
            try:
                conn, addr = srv.accept()
            except socket.timeout:
                continue
            except Exception:
                break
            threading.Thread(
                target=self._handle, args=(conn, addr), daemon=True
            ).start()
        srv.close()

    def _udp_beacon(self):
        hostname = socket.gethostname()
        try:
            local_ip = socket.gethostbyname(hostname)
        except Exception:
            local_ip = "127.0.0.1"
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.settimeout(1.0)
        while self._running:
            if self._tcp_port:
                msg = (
                    f"{WIFI_DISK_PROTOCOL}\n"
                    f"NAME:{self.disk_name}\n"
                    f"PORT:{self._tcp_port}\n"
                    f"HOST:{local_ip}\n"
                    f"HOSTNAME:{hostname}\n"
                ).encode("utf-8")
                try:
                    sock.sendto(msg, ("<broadcast>", WIFI_DISK_DISCOVERY_PORT))
                except Exception:
                    pass
            # 5-second sleep in small chunks so stop() is responsive
            for _ in range(50):
                if not self._running:
                    break
                time.sleep(0.1)
        sock.close()

    def _handle(self, conn, _addr):
        try:
            conn.settimeout(30.0)
            raw = b""
            while b"\n" not in raw and len(raw) < 8192:
                chunk = conn.recv(512)
                if not chunk:
                    break
                raw += chunk
            line = raw.split(b"\n")[0].decode("utf-8", "ignore").strip()

            if line == "LIST":
                entries = []
                try:
                    for e in (self.get_entries() or []):
                        entries.append({"name": e.get("name", e.get("filename", "")),
                                        "size": e.get("size", 0)})
                except Exception:
                    pass
                resp = json.dumps({"entries": entries}).encode("utf-8")
                conn.sendall(len(resp).to_bytes(4, "big") + resp)

            elif line == "INFO":
                info = {
                    "name":     self.disk_name,
                    "protocol": WIFI_DISK_PROTOCOL,
                    "files":    len(self.get_entries() or []),
                }
                resp = json.dumps(info).encode("utf-8")
                conn.sendall(len(resp).to_bytes(4, "big") + resp)

            elif line.startswith("GET "):
                fname = line[4:].strip()
                try:
                    data = self.read_file(fname) or b""
                except Exception:
                    data = b""
                conn.sendall(len(data).to_bytes(8, "big") + data)
        except Exception:
            pass
        finally:
            try:
                conn.close()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# WiFi Disk – window
# ---------------------------------------------------------------------------

class WiFiDisksWindow:
    """Browse MarekFS WiFi Disks available on the local network.

    Type a disk name (e.g. ``MyDisk``) or a full address
    (``MarekFSWiFiDisk:/MyDisk`` or ``MarekFSWiFiDisk://192.168.1.5:57291/MyDisk``)
    in the address bar to connect directly.
    """

    def __init__(self, parent, app=None):
        self.parent      = parent
        self.app         = app
        self._disks      = []
        self._cur_disk   = None
        self._entries    = []

        self.win = tk.Toplevel(parent)
        try:
            from ui_custom import theme_existing_window
            theme_existing_window(self.win, parent, title="📶 MarekFS WiFi Disks")
        except Exception:
            pass
        self.win.title("📶 MarekFS WiFi Disks")
        self.win.geometry("860x620")
        self.win.resizable(True, True)

        body = ttk.Frame(self.win, padding=12)
        body.pack(fill=tk.BOTH, expand=True)

        ttk.Label(body, text="WiFi Disks", style="Title.TLabel").pack(anchor=tk.W)
        ttk.Label(
            body,
            text="Discover MarekFS disks sharing on your local network, or type an address directly.",
            wraplength=820,
        ).pack(anchor=tk.W, pady=(2, 10))

        # ── Address bar ──────────────────────────────────────────────────
        addr_frame = ttk.LabelFrame(body, text=" Address ", padding=8)
        addr_frame.pack(fill=tk.X, pady=(0, 8))

        addr_row = ttk.Frame(addr_frame)
        addr_row.pack(fill=tk.X)
        ttk.Label(addr_row, text="MarekFSWiFiDisk:/").pack(side=tk.LEFT)
        self.addr_var = tk.StringVar()
        addr_entry = ttk.Entry(addr_row, textvariable=self.addr_var, width=42)
        addr_entry.pack(side=tk.LEFT, padx=4, fill=tk.X, expand=True)
        addr_entry.bind("<Return>", lambda _e: self._on_go())
        ttk.Button(
            addr_row, text="Go ▶", style="Accent.TButton", command=self._on_go
        ).pack(side=tk.LEFT, padx=4)

        ttk.Label(
            addr_frame,
            text='Examples:  "MyDisk"  ·  "MarekFSWiFiDisk:/MyDisk"  ·  "MarekFSWiFiDisk://192.168.1.10:57291/MyDisk"',
            foreground="gray",
        ).pack(anchor=tk.W, pady=(4, 0))

        # ── Split pane: disk list | file list ────────────────────────────
        panes = ttk.PanedWindow(body, orient=tk.HORIZONTAL)
        panes.pack(fill=tk.BOTH, expand=True)

        # Left – available disks
        left = ttk.LabelFrame(panes, text=" Available Disks ", padding=6)
        panes.add(left, weight=1)

        dcols = ("Disk Name", "Host", "Port")
        self.disk_tree = ttk.Treeview(
            left, columns=dcols, show="headings", height=14
        )
        for c, w in zip(dcols, (190, 160, 65)):
            self.disk_tree.heading(c, text=c)
            self.disk_tree.column(c, width=w)
        dsb = ttk.Scrollbar(left, command=self.disk_tree.yview)
        self.disk_tree.configure(yscrollcommand=dsb.set)
        self.disk_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        dsb.pack(side=tk.RIGHT, fill=tk.Y)
        self.disk_tree.bind("<<TreeviewSelect>>", self._on_disk_select)

        scan_row = ttk.Frame(left)
        scan_row.pack(fill=tk.X, pady=(6, 0))
        ttk.Button(
            scan_row, text="🔍 Scan Network", command=self._scan_network
        ).pack(side=tk.LEFT, padx=2)

        # Right – files on selected disk
        right = ttk.LabelFrame(panes, text=" Files on Disk ", padding=6)
        panes.add(right, weight=2)

        self.disk_label_var = tk.StringVar(value="(no disk connected)")
        ttk.Label(
            right, textvariable=self.disk_label_var,
            font=("Segoe UI", 10, "bold"),
        ).pack(anchor=tk.W, pady=(0, 4))

        fcols = ("Filename", "Size")
        self.file_tree = ttk.Treeview(
            right, columns=fcols, show="headings", height=14
        )
        for c, w in zip(fcols, (370, 120)):
            self.file_tree.heading(c, text=c)
            self.file_tree.column(c, width=w)
        fsb = ttk.Scrollbar(right, command=self.file_tree.yview)
        self.file_tree.configure(yscrollcommand=fsb.set)
        self.file_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        fsb.pack(side=tk.RIGHT, fill=tk.Y)

        file_btns = ttk.Frame(right)
        file_btns.pack(fill=tk.X, pady=(6, 0))
        ttk.Button(
            file_btns, text="⬇️ Download File",
            style="Accent.TButton", command=self._download_file,
        ).pack(side=tk.LEFT, padx=2)
        ttk.Button(
            file_btns, text="🔄 Refresh Files", command=self._refresh_files,
        ).pack(side=tk.LEFT, padx=2)

        # ── Status bar ───────────────────────────────────────────────────
        self.status_var = tk.StringVar(
            value="Click 'Scan Network' to discover available WiFi disks."
        )
        ttk.Label(
            body, textvariable=self.status_var, wraplength=820
        ).pack(anchor=tk.W, pady=(8, 0))

    # -- network scan --

    def _scan_network(self):
        self.status_var.set("Scanning network for MarekFS WiFi Disks (3 s)…")
        self.win.update_idletasks()
        threading.Thread(target=self._do_scan, daemon=True).start()

    def _do_scan(self):
        disks = _wifi_discover_disks(timeout=3.0)

        def done():
            self._disks = disks
            for i in self.disk_tree.get_children():
                self.disk_tree.delete(i)
            for d in disks:
                self.disk_tree.insert(
                    "", tk.END,
                    values=(d["name"], d["host"], d["port"]),
                )
            if disks:
                self.status_var.set(
                    f"Found {len(disks)} WiFi disk(s).  Select one to browse its files."
                )
            else:
                self.status_var.set(
                    "No WiFi disks found.  "
                    "Make sure a MarekFS disk is open and sharing on this network."
                )

        self.win.after(0, done)

    # -- address bar --

    def _on_go(self):
        raw = self.addr_var.get().strip()
        if not raw:
            return
        # Prepend scheme if the user just typed a name
        if not raw.lower().startswith("marekfswifidisk"):
            full = "MarekFSWiFiDisk:/" + raw
        else:
            full = raw
        parsed = _wifi_parse_address(full)
        if not parsed:
            messagebox.showwarning(
                "Invalid Address",
                "Use one of:\n"
                '  "DiskName"\n'
                '  "MarekFSWiFiDisk:/DiskName"\n'
                '  "MarekFSWiFiDisk://host:port/DiskName"',
                parent=self.win,
            )
            return
        # Direct connect when host:port provided
        if parsed.get("host") and parsed.get("port"):
            disk = {
                "name": parsed["name"],
                "host": parsed["host"],
                "port": parsed["port"],
                "hostname": parsed["host"],
            }
            self._connect(disk)
            return
        # Scan and match by name
        name_target = parsed["name"].lower()
        self.status_var.set(f"Scanning for disk '{parsed['name']}'…")

        def find():
            disks = _wifi_discover_disks(timeout=3.0)
            match = next(
                (d for d in disks if d["name"].lower() == name_target), None
            )

            def done():
                self._disks = disks
                for i in self.disk_tree.get_children():
                    self.disk_tree.delete(i)
                for d in disks:
                    self.disk_tree.insert(
                        "", tk.END,
                        values=(d["name"], d["host"], d["port"]),
                    )
                if match:
                    self._connect(match)
                else:
                    self.status_var.set(
                        f"Disk '{parsed['name']}' not found on the network.  "
                        f"Make sure it is open and sharing."
                    )

            self.win.after(0, done)

        threading.Thread(target=find, daemon=True).start()

    # -- disk / file helpers --

    def _on_disk_select(self, _event):
        sel = self.disk_tree.selection()
        if not sel:
            return
        idx = self.disk_tree.index(sel[0])
        if idx < len(self._disks):
            self._connect(self._disks[idx])

    def _connect(self, disk):
        self._cur_disk = disk
        self.disk_label_var.set(
            f"{disk['name']}  ({disk['host']}:{disk['port']})"
        )
        self.status_var.set(f"Connecting to {disk['name']} ({disk['host']})…")
        threading.Thread(
            target=self._load_files, args=(disk,), daemon=True
        ).start()

    def _load_files(self, disk):
        try:
            raw     = _wifi_connect(disk["host"], disk["port"], "LIST")
            entries = json.loads(raw.decode("utf-8")).get("entries", [])
        except Exception as e:
            self.win.after(
                0,
                lambda: self.status_var.set(f"Error connecting to {disk['name']}: {e}"),
            )
            return

        def done():
            self._entries = entries
            for i in self.file_tree.get_children():
                self.file_tree.delete(i)
            for e in entries:
                size_bytes = e.get("size", 0)
                try:
                    from marekfs_core import format_bytes
                    size_str = format_bytes(size_bytes)
                except Exception:
                    size_str = f"{size_bytes:,} B"
                self.file_tree.insert(
                    "", tk.END,
                    values=(e.get("name", ""), size_str),
                )
            self.status_var.set(
                f"Connected — {len(entries)} file(s) on {disk['name']}."
            )

        self.win.after(0, done)

    def _refresh_files(self):
        if self._cur_disk:
            self._connect(self._cur_disk)

    def _download_file(self):
        if not self._cur_disk:
            messagebox.showwarning(
                "No Disk", "Connect to a disk first.", parent=self.win
            )
            return
        sel = self.file_tree.selection()
        if not sel:
            messagebox.showwarning(
                "No File", "Select a file to download.", parent=self.win
            )
            return
        idx = self.file_tree.index(sel[0])
        if idx >= len(self._entries):
            return
        fname = self._entries[idx].get("name", "file")
        disk  = self._cur_disk

        save_path = filedialog.asksaveasfilename(
            parent=self.win, initialfile=fname, title="Save file as"
        )
        if not save_path:
            return

        self.status_var.set(f"Downloading {fname}…")

        def do_dl():
            try:
                data = _wifi_connect(disk["host"], disk["port"], f"GET {fname}")
                with open(save_path, "wb") as fh:
                    fh.write(data)
                self.win.after(
                    0,
                    lambda: self.status_var.set(
                        f"✔ Downloaded {fname}  ({len(data):,} bytes) → {save_path}"
                    ),
                )
            except Exception as e:
                self.win.after(
                    0,
                    lambda: messagebox.showerror(
                        "Download Error", str(e), parent=self.win
                    ),
                )

        threading.Thread(target=do_dl, daemon=True).start()
