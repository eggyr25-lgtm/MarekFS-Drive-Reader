"""MarekFS low-level core: constants, sector I/O, journal, cache, locks,
payload (compress/encrypt), archive, and antivirus-scanner helpers.
Pure logic, no tkinter."""
import os
import sys
import glob
import zlib
import struct
import hashlib
import ctypes
import json
import random
import string
import subprocess
import zipfile
import io
import time
import re
import gzip
import tarfile
import urllib.request
import urllib.parse
from shutil import which

try:
    from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
except ImportError:
    ChaCha20Poly1305 = None

SECTOR_SIZE = 512
# On-disk directory entries remain 255 UTF-8 bytes for compatibility.
# Logical MarekFS names may be up to 450 Unicode characters; longer names are
# mapped through the FileID database while the physical entry stays bounded.
FILENAME_MAX_LEN = 255
MAX_LOGICAL_FILENAME_CHARS = 450
DIRECTORY_ENTRY_SIZE = 2 + FILENAME_MAX_LEN + 1 + 8 + 8 + 1  # 275 bytes

IO_CHUNK_SIZE = 64 * 1024 * 1024  # 64 MB per read()/write() syscall
# File payloads are committed to the image in fixed 512 MB chunks.
FILE_CHUNK_SIZE = 512 * 1024 * 1024

JOURNAL_START_SECTOR = 1
JOURNAL_MAGIC = b"MKJOURNAL1"
ARCHIVE_MAGIC = b"MAREKARCHV"

MAX_JOURNAL_PAYLOAD_SIZE = 72 * 1024 * 1024 * 1024  # 72 GB max per journaled write
JOURNAL_HEADER_SIZE = len(JOURNAL_MAGIC) + 8 + 4 + 1
JOURNAL_SECTORS = (JOURNAL_HEADER_SIZE + MAX_JOURNAL_PAYLOAD_SIZE + SECTOR_SIZE - 1) // SECTOR_SIZE

# The on-disk directory is pre-sized once for MAX_FILE_COUNT entries; growing
# it later would overwrite file data and lose the directory on reload. We keep
# a practical pre-size (65,536) for the actual layout, and expose the 64-bit
# theoretical ceiling separately for the dashboard display.
MAX_FILE_COUNT = 1 << 16
THEORETICAL_MAX_FILES_64BIT = (1 << 64) - 1  # displayed as the format's limit
DEFAULT_DIR_SECTORS_COUNT = (MAX_FILE_COUNT * DIRECTORY_ENTRY_SIZE + SECTOR_SIZE - 1) // SECTOR_SIZE

DEFAULT_DIR_START_SECTOR = JOURNAL_START_SECTOR + JOURNAL_SECTORS
DEFAULT_DATA_AREA_RESERVE = 72 * 1024 * 1024 * 1024

# --- Cache (volatile, buried deep) ----------------------------------------
CACHE_MAX_SIZE = 48 * 1024 * 1024 * 1024  # 48 GB hard cap
CACHE_MAGIC = b"MAREKCACHE"
CACHE_HEADER_SIZE = len(CACHE_MAGIC) + 8 + 1
CACHE_START_SECTOR = DEFAULT_DIR_START_SECTOR + DEFAULT_DIR_SECTORS_COUNT + (DEFAULT_DATA_AREA_RESERVE // SECTOR_SIZE)
CACHE_SECTORS = (CACHE_MAX_SIZE + CACHE_HEADER_SIZE + SECTOR_SIZE - 1) // SECTOR_SIZE

LOCKS_DIR = ".marekfs_locks"

FILE_ATTR_HIDDEN = 0x01
FILE_ATTR_READONLY = 0x02
FILE_ATTR_SYSTEM = 0x04
FILE_ATTR_ARCHIVE = 0x08
FILE_ATTR_COMPRESSED = 0x10
FILE_ATTR_ENCRYPTED = 0x20
FILE_ATTR_DIRECTORY = 0x40

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".pgm", ".ppm"}
MOVIE_EXTS = {".mp4", ".avi", ".mkv", ".mov", ".webm", ".m4v", ".flv", ".wmv", ".ts", ".ogv", ".3gp"}
MAREKVID_EXT = ".marekvid"
MAREKAUDIO_EXT = ".marekaudio"
MEDIA_CONTAINER_EXTS = {MAREKVID_EXT, MAREKAUDIO_EXT}
MEDIA_MANIFEST_NAME = "manifest.json"


def _media_zip(entries, media_type, description=""):
    """Build a portable multi-track/multi-variant Marek media container.

    Each entry is {name, data|path, language?, bitrate?, resolution?, kind?}.
    The manifest is deliberately JSON and the payloads are ordinary files, so
    the container remains inspectable and can be opened without MarekFS.
    """
    manifest = {"format": "marekvid" if media_type == "video" else "marekaudio",
                "version": 1, "description": description or "", "tracks": []}
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for idx, entry in enumerate(entries):
            name = os.path.basename(str(entry.get("name") or f"track_{idx}"))
            data = entry.get("data")
            if data is None and entry.get("path"):
                with open(entry["path"], "rb") as source:
                    data = source.read()
            data = bytes(data or b"")
            safe_name = f"tracks/{idx:04d}_{name}"
            zf.writestr(safe_name, data)
            item = {k: entry[k] for k in ("language", "title", "bitrate", "resolution", "kind") if entry.get(k) is not None}
            item.update({"name": name, "path": safe_name, "size": len(data)})
            manifest["tracks"].append(item)
        zf.writestr(MEDIA_MANIFEST_NAME, json.dumps(manifest, ensure_ascii=False, indent=2))
    return out.getvalue()


def create_marekvid(entries, description=""):
    return _media_zip(entries, "video", description)


def create_marekaudio(entries, description=""):
    return _media_zip(entries, "audio", description)


def parse_marek_media(data):
    """Return (manifest, track_bytes) for a Marekvid/Marekaudio container."""
    with zipfile.ZipFile(io.BytesIO(data), "r") as zf:
        manifest = json.loads(zf.read(MEDIA_MANIFEST_NAME).decode("utf-8"))
        tracks = []
        for item in manifest.get("tracks", []):
            copy = dict(item)
            copy["data"] = zf.read(item["path"])
            tracks.append(copy)
        return manifest, tracks


def is_marek_media(name, data=None):
    ext = os.path.splitext(name)[1].lower()
    if ext in MEDIA_CONTAINER_EXTS:
        return True
    if data:
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                return MEDIA_MANIFEST_NAME in zf.namelist()
        except Exception:
            pass
    return False

# --- Partitions -----------------------------------------------------------
MAX_PARTITIONS = 4800
PARTITION_ID_LEN = 5
PARTITION_MAGIC = b"MAREKPART"
# Partition table lives in sector 0 (boot/superblock). Layout:
#   magic (9) + version (1) + partition_count (2) + active_index (2)
#   followed by up to MAX_PARTITIONS partition entries of PARTITION_ENTRY_SIZE.
PARTITION_ENTRY_SIZE = PARTITION_ID_LEN + 1 + 8 + 8  # id + flags + start_sector + size_bytes
PARTITION_TABLE_SIZE = len(PARTITION_MAGIC) + 1 + 2 + 2 + (MAX_PARTITIONS * PARTITION_ENTRY_SIZE)
PARTITION_TABLE_SECTORS = (PARTITION_TABLE_SIZE + SECTOR_SIZE - 1) // SECTOR_SIZE

# Windows-wide config in ProgramData that stores the preferred partition.
PROGRAM_DATA_CONFIG_DIR = os.path.join(os.environ.get("PROGRAMDATA", r"C:\ProgramData"), "MarekFS")
PROGRAM_DATA_CONFIG_PATH = os.path.join(PROGRAM_DATA_CONFIG_DIR, "marekfs_config.json")
SCANNER_CONFIG_PATH = os.path.join(PROGRAM_DATA_CONFIG_DIR, "scanner_config.json")
SCANNER_DB_PATH = os.path.join(PROGRAM_DATA_CONFIG_DIR, "scanner_hashes.json")
SCANNER_RULE_DIR = os.path.join(PROGRAM_DATA_CONFIG_DIR, "yara_rules")
FILEID_DB_PATH = os.path.join(PROGRAM_DATA_CONFIG_DIR, "file_ids.json")
FILEID_MAX = (1 << 64) - 1
SCANNER_MAX_RULE_BYTES = 16 * 1024 * 1024
SCANNER_MAX_FILE_BYTES = 256 * 1024 * 1024
SCANNER_RULE_SOURCES = {
    "YARA Forge Core": "https://github.com/YARAHQ/yara-forge/releases/latest/download/yara-forge-rules-core.zip",
    "YARA Forge Extended": "https://github.com/YARAHQ/yara-forge/releases/latest/download/yara-forge-rules-extended.zip",
}
EICAR_SIGNATURE = b"X5O!P%@AP[4\\PZX54(P^)7CC)7}$EICAR-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*"
EICAR_SHA256 = "275a021bbfb6489e54d471899f7db9d1663fc695ec2fe2a2c4538aabf651fd0f"

# ClamAV official virus database (CVD) auto-updater. No API key or account is
# needed — database.clamav.net is ClamAV's public, unauthenticated mirror
# (the same source `freshclam` uses). Only the signed hash-signature (.hsb)
# files bundled inside the CVD are extracted; MarekFS never downloads or
# executes malware samples.
CLAMAV_MIRROR_HOST = "database.clamav.net"
CLAMAV_DATABASES = {"daily": "daily.cvd", "main": "main.cvd"}
CLAMAV_HEADER_SIZE = 512
CLAMAV_CHECK_INTERVAL_SECONDS = 4 * 60 * 60  # 4 hours
CLAMAV_MAX_BYTES = 256 * 1024 * 1024


def is_admin():
    try:
        return ctypes.windll.shell32.IsUserAnAdmin()
    except Exception:
        return False


def get_attr_string(attr_byte):
    attrs = []
    if attr_byte & FILE_ATTR_DIRECTORY: attrs.append("Folder")
    if attr_byte & FILE_ATTR_HIDDEN: attrs.append("Hidden")
    if attr_byte & FILE_ATTR_READONLY: attrs.append("ReadOnly")
    if attr_byte & FILE_ATTR_SYSTEM: attrs.append("System")
    if attr_byte & FILE_ATTR_ARCHIVE: attrs.append("Archive")
    if attr_byte & FILE_ATTR_COMPRESSED: attrs.append("Compressed")
    if attr_byte & FILE_ATTR_ENCRYPTED: attrs.append("Encrypted")
    return ", ".join(attrs) if attrs else "Normal"


def get_attr_icon(attr_byte, filename=""):
    if attr_byte & FILE_ATTR_DIRECTORY: return "📁"
    if filename.endswith(".MAREKARCHV") or (attr_byte & FILE_ATTR_ARCHIVE): return "📦"
    icons = []
    if attr_byte & FILE_ATTR_HIDDEN: icons.append("👁️")
    if attr_byte & FILE_ATTR_READONLY: icons.append("🔒")
    if attr_byte & FILE_ATTR_SYSTEM: icons.append("⚙️")
    if attr_byte & FILE_ATTR_COMPRESSED: icons.append("🗜️")
    if attr_byte & FILE_ATTR_ENCRYPTED: icons.append("🔐")
    return " ".join(icons) if icons else "📄"


def data_checksum(data):
    """BLAKE2b-256 checksum of raw file bytes (fast, strong bit-rot detector)."""
    return hashlib.blake2b(data, digest_size=32).hexdigest()


def file_id_for_record(record):
    """Return the stable unsigned 64-bit ID represented by a directory sector."""
    value = int(record.get("file_id", record.get("sector", 0)))
    if value < 0 or value > FILEID_MAX:
        raise ValueError("FileID must be an unsigned 64-bit integer.")
    return value


def load_file_id_database():
    try:
        with open(FILEID_DB_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_file_id_database(data):
    try:
        os.makedirs(PROGRAM_DATA_CONFIG_DIR, exist_ok=True)
        tmp = FILEID_DB_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, FILEID_DB_PATH)
        return True
    except Exception:
        return False


def _metadata_path(drive_path):
    return os.path.abspath(drive_path) + ".metadata.json"


def load_file_metadata(drive_path):
    try:
        with open(_metadata_path(drive_path), "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_file_metadata(drive_path, metadata):
    path = _metadata_path(drive_path)
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(metadata, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
        return True
    except Exception:
        try:
            if os.path.exists(tmp): os.remove(tmp)
        except Exception: pass
        return False


def format_bytes(size_in_bytes: int) -> str:
    if size_in_bytes <= 0: return "0 B"
    units = ["B", "KB", "MB", "GB", "TB", "PB", "EB"]
    unit_idx = 0
    size = float(size_in_bytes)
    while size >= 1024.0 and unit_idx < len(units) - 1:
        size /= 1024.0
        unit_idx += 1
    return f"{int(size)} B" if unit_idx == 0 else f"{size:.2f} {units[unit_idx]}"


def derive_key(password: str, salt: bytes) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 100000, 32)


def create_marekfs_archive(files_dict: dict) -> bytes:
    payload = bytearray(ARCHIVE_MAGIC)
    for fname, content in files_dict.items():
        name_bytes = fname.encode("utf-8")
        payload += struct.pack("<H", len(name_bytes)) + name_bytes
        payload += struct.pack("<I", len(content)) + content
    return bytes(payload)


def parse_marekfs_archive(data: bytes) -> dict:
    if not data.startswith(ARCHIVE_MAGIC):
        return None
    idx = len(ARCHIVE_MAGIC)
    extracted = {}
    while idx < len(data):
        if idx + 2 > len(data): break
        nlen = struct.unpack("<H", data[idx:idx+2])[0]
        idx += 2
        if idx + nlen > len(data): break
        fname = data[idx:idx+nlen].decode("utf-8", errors="ignore")
        idx += nlen
        if idx + 4 > len(data): break
        flen = struct.unpack("<I", data[idx:idx+4])[0]
        idx += 4
        if idx + flen > len(data): break
        extracted[fname] = data[idx:idx+flen]
        idx += flen
    return extracted


def prepare_file_payload(raw_bytes: bytes, password: str = "", attributes: int = 0):
    payload = raw_bytes
    if (attributes & FILE_ATTR_COMPRESSED) and len(raw_bytes) > 0:
        compressed = zlib.compress(raw_bytes, level=9)
        if len(compressed) < len(raw_bytes):
            payload = compressed
        else:
            attributes &= ~FILE_ATTR_COMPRESSED

    if password:
        if ChaCha20Poly1305 is None: raise RuntimeError("cryptography required for encryption")
        salt = os.urandom(16)
        nonce = os.urandom(12)
        key = derive_key(password, salt)
        cipher = ChaCha20Poly1305(key)
        payload = salt + nonce + cipher.encrypt(nonce, payload, None)
        attributes |= FILE_ATTR_ENCRYPTED
    else:
        attributes &= ~FILE_ATTR_ENCRYPTED

    if len(payload) == 0:
        padded_payload = b"\x00" * SECTOR_SIZE
    else:
        remainder = len(payload) % SECTOR_SIZE
        padded_payload = payload + (b"\x00" * (SECTOR_SIZE - remainder) if remainder else b"")
    return padded_payload, attributes


def read_file_payload(data: bytes, password: str = "", attributes: int = 0, raw_size: int = 0):
    if raw_size == 0 and not (attributes & FILE_ATTR_ENCRYPTED):
        return b""
    payload = data
    if attributes & FILE_ATTR_ENCRYPTED:
        if len(payload) < 28: return b""
        salt = payload[:16]; nonce = payload[16:28]; ciphertext = payload[28:]
        key = derive_key(password, salt)
        cipher = ChaCha20Poly1305(key)
        try:
            payload = cipher.decrypt(nonce, ciphertext, None)
        except Exception:
            raise ValueError("Incorrect password or corrupted ciphertext.")
    if attributes & FILE_ATTR_COMPRESSED:
        try: payload = zlib.decompress(payload)
        except Exception: pass
    return payload[:raw_size]


def open_drive(path: str, read_write: bool = False):
    if not path.startswith("\\\\.\\") and not os.path.exists(path):
        total_sectors = DEFAULT_DIR_START_SECTOR + DEFAULT_DIR_SECTORS_COUNT
        total_size = total_sectors * SECTOR_SIZE + DEFAULT_DATA_AREA_RESERVE + CACHE_MAX_SIZE
        with open(path, "wb") as f:
            f.seek(total_size - 1)
            f.write(b"\x00")
    flags = (os.O_RDWR if read_write else os.O_RDONLY)
    if hasattr(os, "O_BINARY"): flags |= os.O_BINARY
    try:
        return os.open(path, flags)
    except PermissionError:
        raise PermissionError(f"Access Denied for '{path}'. Run Python as Administrator to write to physical drives!")


def read_sectors(fd, start_sector: int, num_sectors: int) -> bytes:
    os.lseek(fd, start_sector * SECTOR_SIZE, os.SEEK_SET)
    total_bytes = num_sectors * SECTOR_SIZE
    chunks = []; remaining = total_bytes
    while remaining > 0:
        chunk = os.read(fd, min(IO_CHUNK_SIZE, remaining))
        if not chunk: break
        chunks.append(chunk); remaining -= len(chunk)
    return b"".join(chunks)


def write_sectors(fd, start_sector: int, data: bytes, progress=None):
    """Write data at start_sector, committing one 512 MB chunk at a time.

    Each 512 MB chunk is flushed with fsync before the next one starts, so a
    huge payload lands as a sequence of durable, sector-aligned chunks.
    `progress(bytes_done, total_bytes)` is called after every chunk.
    """
    os.lseek(fd, start_sector * SECTOR_SIZE, os.SEEK_SET)
    remainder = len(data) % SECTOR_SIZE
    if remainder:
        data = bytes(data) + b"\x00" * (SECTOR_SIZE - remainder)
    view = memoryview(data); total_written = 0; total_len = len(view)
    while total_written < total_len:
        chunk_end = min(total_written + FILE_CHUNK_SIZE, total_len)
        chunk_done = total_written
        while chunk_done < chunk_end:
            piece = view[chunk_done:min(chunk_done + IO_CHUNK_SIZE, chunk_end)]
            n = os.write(fd, piece)
            if n <= 0: raise IOError("write() made no progress -- device full or I/O error")
            chunk_done += n
        total_written = chunk_done
        try: os.fsync(fd)
        except Exception: pass
        if progress:
            try: progress(total_written, total_len)
            except Exception: pass
    try: os.fsync(fd)
    except Exception: pass


def read_sectors_chunked(fd, start_sector: int, num_sectors: int, progress=None) -> bytes:
    """Read in 512 MB chunks (same chunking as write_sectors)."""
    os.lseek(fd, start_sector * SECTOR_SIZE, os.SEEK_SET)
    total_bytes = num_sectors * SECTOR_SIZE
    chunks = []; done = 0
    while done < total_bytes:
        want = min(FILE_CHUNK_SIZE, total_bytes - done)
        got = 0
        while got < want:
            piece = os.read(fd, min(IO_CHUNK_SIZE, want - got))
            if not piece:
                return b"".join(chunks)
            chunks.append(piece); got += len(piece)
        done += got
        if progress:
            try: progress(done, total_bytes)
            except Exception: pass
    return b"".join(chunks)


def set_journal_status(fd, j_padded_buffer: bytearray, status_byte: bytes):
    status_offset = len(JOURNAL_MAGIC) + 12
    j_padded_buffer[status_offset] = status_byte[0]
    sector_index = status_offset // SECTOR_SIZE
    sector_start = sector_index * SECTOR_SIZE
    sector_data = bytes(j_padded_buffer[sector_start:sector_start + SECTOR_SIZE])
    write_sectors(fd, JOURNAL_START_SECTOR + sector_index, sector_data)


def write_with_journal(fd, target_sector: int, data: bytes):
    if len(data) > MAX_JOURNAL_PAYLOAD_SIZE:
        raise ValueError(f"This save is {format_bytes(len(data))}, larger than the {format_bytes(MAX_JOURNAL_PAYLOAD_SIZE)} journaled-write limit.")
    j_payload = bytearray(JOURNAL_MAGIC)
    j_payload += target_sector.to_bytes(8, "little")
    j_payload += len(data).to_bytes(4, "little")
    j_payload += b"\x01"
    j_payload += data
    j_sectors_needed = (len(j_payload) + SECTOR_SIZE - 1) // SECTOR_SIZE
    j_padded = bytearray(bytes(j_payload).ljust(j_sectors_needed * SECTOR_SIZE, b"\x00"))
    write_sectors(fd, JOURNAL_START_SECTOR, bytes(j_padded))
    set_journal_status(fd, j_padded, b"\x02")
    write_sectors(fd, target_sector, data)
    set_journal_status(fd, j_padded, b"\x00")


def recovery_replay_journal(fd):
    try:
        header_sectors = (JOURNAL_HEADER_SIZE + SECTOR_SIZE - 1) // SECTOR_SIZE
        header_raw = read_sectors(fd, JOURNAL_START_SECTOR, header_sectors)
        if not header_raw.startswith(JOURNAL_MAGIC): return
        idx = len(JOURNAL_MAGIC)
        target_sector = int.from_bytes(header_raw[idx:idx+8], "little")
        data_len = int.from_bytes(header_raw[idx+8:idx+12], "little")
        status = header_raw[idx+12]
        if status != 0x02 or target_sector <= 0 or data_len <= 0 or data_len > MAX_JOURNAL_PAYLOAD_SIZE:
            return
        total_needed = JOURNAL_HEADER_SIZE + data_len
        total_sectors = (total_needed + SECTOR_SIZE - 1) // SECTOR_SIZE
        full_raw = read_sectors(fd, JOURNAL_START_SECTOR, total_sectors)
        payload = full_raw[JOURNAL_HEADER_SIZE:JOURNAL_HEADER_SIZE + data_len]
        write_sectors(fd, target_sector, payload)
        j_padded = bytearray(full_raw)
        set_journal_status(fd, j_padded, b"\x00")
    except Exception:
        pass


# --- File locking --------------------------------------------------------
def _pid_alive(pid):
    try:
        if sys.platform == "win32":
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            h = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if h:
                ctypes.windll.kernel32.CloseHandle(h)
                return True
            return False
        else:
            os.kill(pid, 0)
            return True
    except Exception:
        return False


def _lock_path(filename):
    os.makedirs(LOCKS_DIR, exist_ok=True)
    safe = "".join(c if c.isalnum() else "_" for c in filename) + ".lock"
    return os.path.join(LOCKS_DIR, safe)


def acquire_file_lock(filename):
    path = _lock_path(filename)
    if os.path.exists(path):
        try:
            with open(path, "r") as f: pid = int(f.read().strip())
            if pid != os.getpid() and _pid_alive(pid): return False
        except Exception:
            pass
    with open(path, "w") as f: f.write(str(os.getpid()))
    return True


def release_file_lock(filename):
    path = _lock_path(filename)
    try:
        if os.path.exists(path):
            with open(path) as f: pid = f.read().strip()
            if str(pid) == str(os.getpid()) or not _pid_alive(int(pid)):
                os.remove(path)
    except Exception:
        pass


def is_file_locked_by_other(filename):
    path = _lock_path(filename)
    if not os.path.exists(path): return False
    try:
        with open(path) as f: pid = int(f.read().strip())
        return pid != os.getpid() and _pid_alive(pid)
    except Exception:
        return False


# --- Antivirus scanner detection / invocation -----------------------------
def _scanner_default_config():
    return {"version": 1, "enabled": True, "yara_enabled": True,
            "heuristics_enabled": True, "hash_enabled": True,
            "rule_sources": dict(SCANNER_RULE_SOURCES),
            "hash_sources": {}, "last_update": None,
            # ClamAV CVD auto-update state (no API key required).
            "clamav_enabled": False,
            "clamav_database": "daily",
            "clamav_last_check": None,
            "clamav_last_version": None,
            "clamav_last_installed": None,
            "clamav_last_hash_count": 0}


def load_scanner_config():
    try:
        with open(SCANNER_CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        base = _scanner_default_config(); base.update(cfg if isinstance(cfg, dict) else {})
        return base
    except Exception:
        return _scanner_default_config()


def save_scanner_config(config):
    try:
        os.makedirs(PROGRAM_DATA_CONFIG_DIR, exist_ok=True)
        tmp = SCANNER_CONFIG_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(config, f, ensure_ascii=False, indent=2)
        os.replace(tmp, SCANNER_CONFIG_PATH)
        return True
    except Exception:
        return False


def ensure_scanner_config():
    cfg = load_scanner_config()
    if not os.path.exists(SCANNER_CONFIG_PATH):
        save_scanner_config(cfg)
    return cfg


def _safe_download(url, destination, max_bytes=SCANNER_MAX_RULE_BYTES):
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or parsed.netloc not in {"github.com", "raw.githubusercontent.com", "github.com"}:
        raise ValueError("Scanner rule sources must use HTTPS GitHub URLs.")
    req = urllib.request.Request(url, headers={"User-Agent": "MarekFS-Scanner/1.0"})
    with urllib.request.urlopen(req, timeout=30) as response:
        content_length = int(response.headers.get("Content-Length") or 0)
        if content_length > max_bytes:
            raise ValueError("Downloaded rule file exceeds the safety size limit.")
        data = response.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise ValueError("Downloaded rule file exceeds the safety size limit.")
    if not data.strip():
        raise ValueError("Downloaded rule file is empty.")
    os.makedirs(os.path.dirname(destination), exist_ok=True)
    tmp = destination + ".tmp"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, destination)
    return {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}


def update_scanner_rules(config=None):
    cfg = config or ensure_scanner_config()
    results = []
    for name, url in cfg.get("rule_sources", {}).items():
        dest = os.path.join(SCANNER_RULE_DIR, re.sub(r"[^A-Za-z0-9_.-]+", "_", name))
        try:
            info = _safe_download(url, dest)
            if url.lower().endswith(".zip"):
                with zipfile.ZipFile(dest, "r") as archive:
                    members = [m for m in archive.infolist() if not m.is_dir() and m.filename.lower().endswith((".yar", ".yara"))]
                    total = sum(int(m.file_size) for m in members)
                    if total > SCANNER_MAX_RULE_BYTES * 8:
                        raise ValueError("Extracted rule package exceeds the safety size limit.")
                    package_dir = dest + "_rules"
                    os.makedirs(package_dir, exist_ok=True)
                    extracted = 0
                    for member in members:
                        safe_name = os.path.basename(member.filename)
                        if not safe_name:
                            continue
                        target = os.path.join(package_dir, safe_name)
                        with archive.open(member, "r") as src, open(target, "wb") as out:
                            out.write(src.read(SCANNER_MAX_RULE_BYTES + 1))
                        if os.path.getsize(target) <= SCANNER_MAX_RULE_BYTES:
                            extracted += 1
                    results.append({"source": name, "path": package_dir, "status": "updated", "files": extracted, **info})
            else:
                results.append({"source": name, "path": dest, "status": "updated", **info})
        except Exception as e:
            results.append({"source": name, "path": dest, "status": "error", "error": str(e)})
    cfg["last_update"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    save_scanner_config(cfg)
    return results


def update_scanner_hashes(config=None):
    """Download optional hash feeds only when explicitly configured.

    No malware samples are downloaded. Each configured feed must be HTTPS and
    is parsed as one SHA-256 per line, with a strict size limit.
    """
    cfg = config or ensure_scanner_config()
    db = load_scanner_hashes()
    results = []
    for name, url in cfg.get("hash_sources", {}).items():
        try:
            parsed = urllib.parse.urlparse(url)
            if parsed.scheme != "https":
                raise ValueError("Hash feeds must use HTTPS.")
            req = urllib.request.Request(url, headers={"User-Agent": "MarekFS-Scanner/1.0"})
            with urllib.request.urlopen(req, timeout=30) as response:
                raw = response.read(SCANNER_MAX_RULE_BYTES + 1)
            if len(raw) > SCANNER_MAX_RULE_BYTES:
                raise ValueError("Hash feed exceeds the safety size limit.")
            count = 0
            for line in raw.decode("utf-8", "ignore").splitlines():
                value = line.strip().split(",", 1)[0].lower()
                if re.fullmatch(r"[0-9a-f]{64}", value):
                    db[value] = {"severity": "high", "detail": f"Matched configured hash feed: {name}"}
                    count += 1
            results.append({"source": name, "status": "updated", "hashes": count})
        except Exception as e:
            results.append({"source": name, "status": "error", "error": str(e)})
    save_scanner_hashes(db)
    return results


def load_scanner_hashes():
    try:
        with open(SCANNER_DB_PATH, "r", encoding="utf-8") as f:
            db = json.load(f)
        return db if isinstance(db, dict) else {}
    except Exception:
        return {}


def save_scanner_hashes(db):
    try:
        os.makedirs(PROGRAM_DATA_CONFIG_DIR, exist_ok=True)
        tmp = SCANNER_DB_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(db, f, ensure_ascii=False, indent=2)
        os.replace(tmp, SCANNER_DB_PATH)
        return True
    except Exception:
        return False


# --- ClamAV CVD hash-database updater (no API key) -------------------------
def _clamav_url(cfg):
    db_key = cfg.get("clamav_database", "daily")
    filename = CLAMAV_DATABASES.get(db_key, CLAMAV_DATABASES["daily"])
    url = f"https://{CLAMAV_MIRROR_HOST}/{filename}"
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or parsed.netloc != CLAMAV_MIRROR_HOST:
        raise ValueError("ClamAV mirror URL failed validation.")
    return url


def _parse_cvd_header(header_bytes):
    """CVD files start with a 512-byte, colon-delimited ASCII header:
    ClamAV-VDB:build_time:version:sig_count:functionality_level:MD5:...
    Return the version field (a monotonically increasing int as a string),
    which is all we need to detect "is this newer than what I have"."""
    text = header_bytes.decode("ascii", "ignore").rstrip("\x00").strip()
    fields = text.split(":")
    if len(fields) < 3 or fields[0] != "ClamAV-VDB":
        raise ValueError("Unrecognized ClamAV database header.")
    return fields[2]


def check_clamav_update(config=None):
    """Cheap check: fetch only the first 512 bytes (a Range request) of the
    configured .cvd file and compare its embedded version number against the
    last one installed. No API key needed — database.clamav.net is ClamAV's
    public update mirror. Safe to call on a timer (e.g. every 4 hours)."""
    cfg = config or ensure_scanner_config()
    result = {"available": False, "version": None, "error": None,
              "checked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    if not cfg.get("clamav_enabled"):
        result["error"] = "ClamAV auto-updates are disabled."
        return result
    try:
        url = _clamav_url(cfg)
        # No Range header -- database.clamav.net returns 403 for Range requests.
        # Connection: close prevents urllib from putting this partial-read
        # socket back into the keep-alive pool, which would corrupt the
        # subsequent full download request with a stale response.
        req = urllib.request.Request(url, headers={
            "User-Agent": "CVDUPDATE/1.0",
            "Connection": "close",
        })
        with urllib.request.urlopen(req, timeout=20) as response:
            header = response.read(CLAMAV_HEADER_SIZE)
        version = _parse_cvd_header(header)
        result["version"] = version
        result["available"] = version != cfg.get("clamav_last_version")
    except Exception as e:
        result["error"] = str(e)
    cfg["clamav_last_check"] = result["checked_at"]
    save_scanner_config(cfg)
    return result


def _extract_cvd_hashes(cvd_bytes):
    """CVD body (after the 512-byte header) is a gzip-compressed tar archive
    containing the database's component files. We only care about the
    hash-signature members (.hsb = SHA-256 full-file hashes, .hdb = MD5),
    each line formatted 'hash:size:malware_name'. SHA-256 entries are used
    directly; MD5-only entries are skipped since MarekFS's local database is
    keyed by SHA-256."""
    body = cvd_bytes[CLAMAV_HEADER_SIZE:]
    hashes = {}
    with gzip.GzipFile(fileobj=io.BytesIO(body)) as gz:
        tar_bytes = gz.read()
    with tarfile.open(fileobj=io.BytesIO(tar_bytes)) as tar:
        for member in tar.getmembers():
            if not member.isfile() or not member.name.lower().endswith(".hsb"):
                continue
            extracted = tar.extractfile(member)
            if not extracted:
                continue
            text = extracted.read().decode("utf-8", "ignore")
            for line in text.splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split(":")
                if len(parts) < 2:
                    continue
                candidate = parts[0].lower()
                if not re.fullmatch(r"[0-9a-f]{64}", candidate):
                    continue
                name = parts[2] if len(parts) > 2 else "ClamAV signature match"
                hashes[candidate] = {"severity": "high",
                                     "detail": f"Matched the ClamAV database (signature: {name})."}
    return hashes


def download_clamav_update(config=None, progress=None, max_bytes=CLAMAV_MAX_BYTES):
    """Download the configured ClamAV .cvd database and merge its SHA-256
    hash signatures into the local scanner hash database.

    `progress`, if given, is called as progress(bytes_read, total_bytes) after
    every chunk (total_bytes is 0 if the server did not send Content-Length).
    """
    cfg = config or ensure_scanner_config()
    url = _clamav_url(cfg)
    req = urllib.request.Request(url, headers={"User-Agent": "CVDUPDATE/1.0"})
    read = 0
    chunks = []
    with urllib.request.urlopen(req, timeout=120) as response:
        total = int(response.headers.get("Content-Length") or 0)
        if progress:
            try:
                progress(0, total)
            except Exception:
                pass
        while True:
            block = response.read(65536)
            if not block:
                break
            chunks.append(block)
            read += len(block)
            if read > max_bytes:
                raise ValueError("ClamAV database exceeds the configured safety size limit.")
            if progress:
                try:
                    progress(read, total)
                except Exception:
                    pass
    data = b"".join(chunks)
    if len(data) <= CLAMAV_HEADER_SIZE:
        raise ValueError("ClamAV database download was incomplete or empty.")

    version = _parse_cvd_header(data[:CLAMAV_HEADER_SIZE])
    new_hashes = _extract_cvd_hashes(data)

    db = load_scanner_hashes()
    db.update(new_hashes)
    save_scanner_hashes(db)

    cfg["clamav_last_version"] = version
    cfg["clamav_last_installed"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    cfg["clamav_last_hash_count"] = len(new_hashes)
    save_scanner_config(cfg)
    return {"hashes": len(new_hashes), "bytes": len(data), "version": version}


def _ascii_strings(data, minimum=4):
    return [x.decode("ascii", "ignore") for x in re.findall(rb"[ -~]{%d,}" % minimum, data)]


def _is_windows_pe(data):
    if len(data) < 64 or data[:2] != b"MZ":
        return False
    pe_offset = int.from_bytes(data[0x3c:0x40], "little")
    return 0 <= pe_offset <= len(data) - 4 and data[pe_offset:pe_offset + 4] == b"PE\x00\x00"


def _has_pe_signature(data):
    """Best-effort Authenticode presence check; it does not validate trust."""
    if not _is_windows_pe(data):
        return False
    pe_offset = int.from_bytes(data[0x3c:0x40], "little")
    coff = pe_offset + 4
    if coff + 20 > len(data):
        return False
    opt_size = int.from_bytes(data[coff + 16:coff + 18], "little")
    opt = coff + 20
    if opt + opt_size > len(data):
        return False
    magic = int.from_bytes(data[opt:opt + 2], "little")
    directory_offset = 112 if magic == 0x20B else 96 if magic == 0x10B else -1
    if directory_offset < 0 or opt + directory_offset + 8 > len(data):
        return False
    cert_offset = int.from_bytes(data[opt + directory_offset:opt + directory_offset + 4], "little")
    cert_size = int.from_bytes(data[opt + directory_offset + 4:opt + directory_offset + 8], "little")
    return cert_offset > 0 and cert_size > 0


def _heuristic_scan(name, data):
    """Conservative static heuristics; never executes or modifies the sample."""
    text = "\n".join(_ascii_strings(data)).lower()
    ext = os.path.splitext(name)[1].lower()
    findings = []
    is_executable = ext in {".exe", ".dll", ".sys", ".scr", ".com", ".cpl", ".msi"} or _is_windows_pe(data)
    unsigned = is_executable and not _has_pe_signature(data)
    delete_terms = ("deletefile", "remove-directory", "rmdir", "del /f", "erase ", "shutil.rmtree", "remove-item")
    protected_paths = ("\\program files", "\\programdata", "\\windows", "/program files", "/programdata", "/windows")
    encryption_terms = ("cryptencrypt", "cryptprotectdata", "encrypting file system", "ransom", "aes_encrypt", "chacha20", "file encryption")
    if unsigned:
        findings.append({"severity": "warning", "rule": "unsigned-executable", "detail": "Executable appears unsigned; signature absence alone is not proof of malware."})
    if any(term in text for term in delete_terms) and any(path in text for path in protected_paths):
        findings.append({"severity": "high" if unsigned else "warning", "rule": "protected-path-delete", "detail": "Static strings suggest deletion of protected Windows/application paths."})
    if unsigned and any(term in text for term in encryption_terms):
        findings.append({"severity": "high", "rule": "unsigned-file-encryption", "detail": "Unsigned executable contains file-encryption indicators."})
    if any(term in text for term in ("powershell -enc", "frombase64string", "invoke-expression", "regsvr32", "rundll32")):
        findings.append({"severity": "warning", "rule": "suspicious-loader-string", "detail": "Contains a common script/proxy execution indicator."})
    return findings


def scan_path_with_builtin_scanner(path, config=None):
    with open(path, "rb") as f:
        data = f.read(SCANNER_MAX_FILE_BYTES + 1)
    return scan_bytes_with_builtin_scanner(os.path.basename(path), data, config)


def scan_bytes_with_builtin_scanner(name, data, config=None):
    cfg = config or ensure_scanner_config()
    if len(data) > SCANNER_MAX_FILE_BYTES:
        return {"status": "error", "name": name, "sha256": hashlib.sha256(data).hexdigest(),
                "findings": [{"severity": "error", "rule": "file-too-large", "detail": "File exceeds the built-in scanner size limit."}]}
    digest = hashlib.sha256(data).hexdigest()
    db = load_scanner_hashes() if cfg.get("hash_enabled", True) else {}
    findings = []
    eicar_normalized = data.rstrip(b"\r\n\x00 \t")
    if (data == EICAR_SIGNATURE or digest == EICAR_SHA256 or
            eicar_normalized == EICAR_SIGNATURE or
            hashlib.sha256(eicar_normalized).hexdigest() == EICAR_SHA256):
        findings.append({"severity": "high", "rule": "eicar-test-file", "detail": "EICAR anti-malware test signature detected; this is a harmless test marker, not live malware."})
    known = db.get(digest)
    if known:
        findings.append({"severity": known.get("severity", "high"), "rule": "known-hash", "detail": known.get("detail", "Hash is present in the local malware database.")})
    if cfg.get("heuristics_enabled", True):
        findings.extend(_heuristic_scan(name, data))
    yara_status = "disabled"
    if cfg.get("yara_enabled", True):
        try:
            import yara
            rules = [p for ext in ("*.yar", "*.yara") for p in glob.glob(os.path.join(SCANNER_RULE_DIR, "**", ext), recursive=True) if os.path.getsize(p) <= SCANNER_MAX_RULE_BYTES]
            if rules:
                usable = 0
                for rule_path in rules:
                    try:
                        compiled = yara.compile(filepath=rule_path)
                        for match in compiled.match(data=data, timeout=10):
                            findings.append({"severity": "high", "rule": match.rule, "detail": "Matched a cached YARA rule."})
                        usable += 1
                    except Exception:
                        continue
                yara_status = f"active ({usable}/{len(rules)} rule files)"
            else:
                yara_status = "no cached rules"
        except ImportError:
            yara_status = "unavailable"
        except Exception as e:
            yara_status = f"error: {e}"
    high = any(x.get("severity") == "high" for x in findings)
    return {"status": "malicious" if high else ("suspicious" if findings else "clean"),
            "name": name, "sha256": digest, "yara": yara_status, "findings": findings}


def find_av_scanners():
    """Return list of (name, exe_path) for any installed AV command-line
    scanner. Best-effort: Windows Defender, Bitdefender, Avast, AVG; ClamAV
    on Linux/mac. CyberGhost is a VPN (not an AV) but is detected and
    flagged so the UI can explain it can't scan files."""
    found = []
    seen = set()
    if sys.platform == "win32":
        wd = r"C:\Program Files\Windows Defender\MpCmdRun.exe"
        if os.path.exists(wd) and "Windows Defender" not in seen:
            found.append(("Windows Defender", wd)); seen.add("Windows Defender")
        bases = [r"C:\Program Files", r"C:\Program Files (x86)"]
        for base in bases:
            for p in glob.glob(os.path.join(base, "Bitdefender", "**", "bdscan.exe"), recursive=True):
                if "Bitdefender" not in seen: found.append(("Bitdefender", p)); seen.add("Bitdefender")
                break
            for p in glob.glob(os.path.join(base, "Bitdefender", "**", "bdcmd.exe"), recursive=True):
                if "Bitdefender" not in seen: found.append(("Bitdefender", p)); seen.add("Bitdefender")
                break
            for p in glob.glob(os.path.join(base, "AVAST Software", "Avast", "ashCmd.exe"), recursive=True):
                if "Avast" not in seen: found.append(("Avast", p)); seen.add("Avast")
                break
            for p in glob.glob(os.path.join(base, "AVG", "Av", "avgscanx.exe"), recursive=True):
                if "AVG" not in seen: found.append(("AVG", p)); seen.add("AVG")
                break
            for p in glob.glob(os.path.join(base, "CyberGhost*", "CyberGhost.exe"), recursive=True):
                if "CyberGhost" not in seen: found.append(("CyberGhost (VPN - not an antivirus)", p)); seen.add("CyberGhost")
                break
    else:
        for exe in ("clamscan",):
            w = which(exe)
            if w and "ClamAV" not in seen:
                found.append(("ClamAV", w)); seen.add("ClamAV")
    return found


def build_av_command(name, exe, target):
    n = name.lower()
    if "windows defender" in n: return [exe, "-Scan", "-ScanType", "3", "-File", target]
    if "bitdefender" in n: return [exe, target]
    if "avast" in n: return [exe, target]
    if "avg" in n: return [exe, f"/SCAN={target}"]
    if "clam" in n: return [exe, target]
    return [exe, target]


def open_in_system_player(path):
    if sys.platform == "win32":
        os.startfile(path)
    elif sys.platform == "darwin":
        subprocess.Popen(["open", path])
    else:
        subprocess.Popen(["xdg-open", path])


# --- Partitions -----------------------------------------------------------
def generate_partition_id():
    """Random 5-char partition id like 'jh49g'."""
    alphabet = string.ascii_lowercase + string.digits
    return "".join(random.choice(alphabet) for _ in range(PARTITION_ID_LEN))


def _partition_entry_to_bytes(entry):
    pid = entry["id"].encode("ascii")[:PARTITION_ID_LEN].ljust(PARTITION_ID_LEN, b"\x00")
    flags = struct.pack("B", int(entry.get("flags", 0)))
    start = int(entry.get("start_sector", 0)).to_bytes(8, "little")
    size = int(entry.get("size_bytes", 0)).to_bytes(8, "little")
    return pid + flags + start + size


def _bytes_to_partition_entry(buf):
    pid = buf[0:PARTITION_ID_LEN].rstrip(b"\x00").decode("ascii", errors="ignore")
    flags = buf[PARTITION_ID_LEN]
    start = int.from_bytes(buf[PARTITION_ID_LEN+1:PARTITION_ID_LEN+9], "little")
    size = int.from_bytes(buf[PARTITION_ID_LEN+9:PARTITION_ID_LEN+17], "little")
    return {"id": pid, "flags": flags, "start_sector": start, "size_bytes": size}


def read_partition_table(fd):
    """Read and parse the partition table from sector 0. Returns
    (partitions list, active_index) or ([], 0) if no valid table."""
    try:
        data = read_sectors(fd, 0, PARTITION_TABLE_SECTORS)
    except Exception:
        return [], 0
    if not data.startswith(PARTITION_MAGIC):
        return [], 0
    idx = len(PARTITION_MAGIC)
    version = data[idx]; idx += 1
    count = struct.unpack("<H", data[idx:idx+2])[0]; idx += 2
    active_index = struct.unpack("<H", data[idx:idx+2])[0]; idx += 2
    partitions = []
    for i in range(min(count, MAX_PARTITIONS)):
        off = idx + i * PARTITION_ENTRY_SIZE
        if off + PARTITION_ENTRY_SIZE > len(data): break
        entry = _bytes_to_partition_entry(data[off:off+PARTITION_ENTRY_SIZE])
        if entry["id"]:
            partitions.append(entry)
    return partitions, active_index


def write_partition_table(fd, partitions, active_index):
    """Write the partition table to sector 0."""
    count = min(len(partitions), MAX_PARTITIONS)
    header = bytearray(PARTITION_MAGIC)
    header += struct.pack("B", 1)  # version
    header += struct.pack("<H", count)
    header += struct.pack("<H", min(active_index, count - 1) if count else 0)
    for i in range(count):
        header += _partition_entry_to_bytes(partitions[i])
    padded = bytes(header).ljust(PARTITION_TABLE_SECTORS * SECTOR_SIZE, b"\x00")
    write_sectors(fd, 0, padded)


def init_default_partitions(num=4):
    """Create `num` default partitions with random ids. Partition 0 starts
    right after the partition table; each gets an equal slice of the
    data area reserve."""
    partitions = []
    per_partition_sectors = DEFAULT_DATA_AREA_RESERVE // SECTOR_SIZE // max(num, 1)
    start = PARTITION_TABLE_SECTORS
    for i in range(num):
        partitions.append({
            "id": generate_partition_id(),
            "flags": 0,
            "start_sector": start,
            "size_bytes": per_partition_sectors * SECTOR_SIZE,
        })
        start += per_partition_sectors
    return partitions


# --- ProgramData config (PreferredPartitionID) ---------------------------
def load_programdata_config():
    """Read the global MarekFS config from ProgramData. Returns a dict; if
    the file is missing or unreadable, returns an empty dict."""
    try:
        with open(PROGRAM_DATA_CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_programdata_config(config):
    """Persist the global MarekFS config to ProgramData. Creates the folder
    if needed. May require admin rights on some systems."""
    try:
        os.makedirs(PROGRAM_DATA_CONFIG_DIR, exist_ok=True)
        with open(PROGRAM_DATA_CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2)
        return True
    except PermissionError:
        return False
    except Exception:
        return False


def get_preferred_partition_id():
    cfg = load_programdata_config()
    return cfg.get("PreferredPartitionID", "")


def set_preferred_partition_id(partition_id):
    cfg = load_programdata_config()
    cfg["PreferredPartitionID"] = partition_id
    return save_programdata_config(cfg)


# --- Extended filesystem support (ProgramData config) --------------------
ALL_EXTENDED_FS = (
    "MarekFS", "NTFS", "HFS", "APFS", "HFS+", "FAT16", "BTRFS", "FAT32",
    "RAMFS", "exFAT", "TMPFS", "EXT1", "EXT2", "EXT3", "EXT4",
)


def get_extended_fs_support():
    """Return the list of enabled extended filesystem names."""
    cfg = load_programdata_config()
    enabled = cfg.get("ExtendedFilesystemSupport")
    if not isinstance(enabled, list):
        return list(ALL_EXTENDED_FS)
    valid = [name for name in enabled if name in ALL_EXTENDED_FS]
    if "MarekFS" not in valid:
        valid.insert(0, "MarekFS")
    return valid


def set_extended_fs_support(enabled_names):
    """Persist enabled extended filesystem names. MarekFS is always kept on."""
    enabled = []
    seen = set()
    for name in enabled_names or []:
        if name in ALL_EXTENDED_FS and name not in seen:
            enabled.append(name)
            seen.add(name)
    if "MarekFS" not in seen:
        enabled.insert(0, "MarekFS")
    cfg = load_programdata_config()
    cfg["ExtendedFilesystemSupport"] = enabled
    return save_programdata_config(cfg)


# ===========================================================================
# Full-partition encryption (size preserving ChaCha20 stream cipher)
# ===========================================================================
PARTITION_ENC_MAGIC = b"MAREKPENC1"
PARTITION_ENC_CHUNK = 4 * 1024 * 1024  # 4 MiB per crypt chunk


def _chacha_stream(key: bytes, chunk_index: int, data: bytes) -> bytes:
    """Size-preserving ChaCha20 (no Poly1305 tag) over one chunk."""
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms
    nonce = chunk_index.to_bytes(16, "little")
    cipher = Cipher(algorithms.ChaCha20(key, nonce), mode=None)
    enc = cipher.encryptor()
    return enc.update(data) + enc.finalize()


def read_partition_enc_header(fd, start_sector: int):
    """Return (encrypted: bool, salt: bytes|None, verifier: bytes|None)."""
    try:
        raw = read_sectors(fd, start_sector, 1)
    except Exception:
        return False, None, None
    if not raw.startswith(PARTITION_ENC_MAGIC):
        return False, None, None
    idx = len(PARTITION_ENC_MAGIC)
    salt = raw[idx:idx + 16]
    verifier = raw[idx + 16:idx + 48]
    return True, salt, verifier


def _write_partition_enc_header(fd, start_sector: int, salt: bytes, verifier: bytes):
    hdr = PARTITION_ENC_MAGIC + salt + verifier
    write_sectors(fd, start_sector, hdr.ljust(SECTOR_SIZE, b"\x00"))


def _clear_partition_enc_header(fd, start_sector: int):
    write_sectors(fd, start_sector, b"\x00" * SECTOR_SIZE)


def _partition_body_range(partition):
    """Data sectors of a partition, skipping its encryption header sector."""
    start = int(partition["start_sector"]) + 1
    total_sectors = max(1, int(partition["size_bytes"]) // SECTOR_SIZE)
    return start, max(0, total_sectors - 1)


def crypt_partition(drive_path: str, partition: dict, password: str,
                    decrypt: bool = False, progress=None, cancel=None):
    """Encrypt or decrypt every data sector of a partition in place.

    `progress(done_bytes, total_bytes)` is called between chunks.
    Raises ValueError on a wrong password when decrypting.
    """
    if ChaCha20Poly1305 is None:
        raise RuntimeError("The 'cryptography' package is required for partition encryption.")
    if not password:
        raise ValueError("A password is required.")

    fd = open_drive(drive_path, read_write=True)
    try:
        start_sector = int(partition["start_sector"])
        already, salt, verifier = read_partition_enc_header(fd, start_sector)

        if decrypt:
            if not already:
                raise ValueError("This partition is not encrypted.")
            key = derive_key(password, salt)
            if hashlib.sha256(key).digest() != verifier:
                raise ValueError("Incorrect password for this partition.")
        else:
            if already:
                raise ValueError("This partition is already encrypted.")
            salt = os.urandom(16)
            key = derive_key(password, salt)
            verifier = hashlib.sha256(key).digest()

        body_start, body_sectors = _partition_body_range(partition)
        total_bytes = body_sectors * SECTOR_SIZE
        chunk_sectors = PARTITION_ENC_CHUNK // SECTOR_SIZE
        done = 0
        index = 0
        remaining = body_sectors
        sector = body_start
        while remaining > 0:
            if cancel is not None and cancel():
                raise RuntimeError("Cancelled — the partition is now in a partially converted state.")
            n = min(chunk_sectors, remaining)
            data = read_sectors(fd, sector, n)
            if not data:
                break
            out = _chacha_stream(key, index, data)
            write_sectors(fd, sector, out)
            sector += n
            remaining -= n
            index += 1
            done += len(out)
            if progress:
                progress(done, total_bytes)

        if decrypt:
            _clear_partition_enc_header(fd, start_sector)
        else:
            _write_partition_enc_header(fd, start_sector, salt, verifier)
        try:
            os.fsync(fd)
        except Exception:
            pass
    finally:
        os.close(fd)
    return True


def is_partition_encrypted(drive_path: str, partition: dict) -> bool:
    try:
        fd = open_drive(drive_path, read_write=False)
        try:
            enc, _, _ = read_partition_enc_header(fd, int(partition["start_sector"]))
            return enc
        finally:
            os.close(fd)
    except Exception:
        return 
