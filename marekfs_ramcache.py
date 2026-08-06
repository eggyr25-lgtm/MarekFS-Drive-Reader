"""MarekFS RAM + persistent cache.

Every file opened through the explorer is kept in RAM and mirrored to a
persistent on-disk cache store. The store is written in fixed 512 MB chunks
(one chunk file per 512 MB slice of a cached file), and the whole store is
loaded back into RAM *before* the MarekFS reader initializes, so a re-open of
the same image serves reads straight from memory.

Pure logic, no tkinter.
"""
import os
import io
import json
import time
import hashlib
import threading

# Files are persisted in fixed 512 MB chunks.
CHUNK_SIZE = 512 * 1024 * 1024

# Soft ceiling for how much file data we hold in RAM (LRU eviction above it).
DEFAULT_RAM_LIMIT = 4 * 1024 * 1024 * 1024

INDEX_NAME = "index.json"


def _key_id(key: str) -> str:
    return hashlib.sha1(key.encode("utf-8", "ignore")).hexdigest()


def chunk_count(size: int) -> int:
    return max(1, (size + CHUNK_SIZE - 1) // CHUNK_SIZE)


def iter_chunks(data: bytes):
    """Yield (index, offset, chunk) slices of exactly CHUNK_SIZE (last may be smaller)."""
    if not data:
        yield 0, 0, b""
        return
    view = memoryview(data)
    for i in range(0, len(view), CHUNK_SIZE):
        yield i // CHUNK_SIZE, i, bytes(view[i:i + CHUNK_SIZE])


class RAMCache:
    """In-memory file cache with a chunked, persistent backing store."""

    def __init__(self, store_dir=".marekfs_cache", ram_limit=DEFAULT_RAM_LIMIT):
        self.store_dir = store_dir
        self.ram_limit = ram_limit
        self._lock = threading.RLock()
        self._mem = {}        # key -> bytes
        self._meta = {}       # key -> {"size", "chunks", "atime", "id", "persisted"}
        self.bytes_in_ram = 0
        self.hits = 0
        self.misses = 0
        self.loaded = False
        self.last_error = None

    # -- paths -------------------------------------------------------------
    def _index_path(self):
        return os.path.join(self.store_dir, INDEX_NAME)

    def _chunk_path(self, cid, index):
        return os.path.join(self.store_dir, f"{cid}.{index:05d}.chunk")

    # -- startup preload ---------------------------------------------------
    def preload(self, progress=None):
        """Load the persistent store back into RAM. Call before the reader starts."""
        with self._lock:
            self._mem.clear(); self._meta.clear(); self.bytes_in_ram = 0
            index = {}
            try:
                with open(self._index_path(), "r", encoding="utf-8") as f:
                    index = json.load(f)
            except Exception:
                index = {}

            total = len(index) or 1
            for n, (key, meta) in enumerate(index.items(), 1):
                cid = meta.get("id") or _key_id(key)
                size = int(meta.get("size", 0))
                buf = io.BytesIO()
                ok = True
                for i in range(chunk_count(size)):
                    path = self._chunk_path(cid, i)
                    try:
                        with open(path, "rb") as cf:
                            buf.write(cf.read())
                    except Exception as e:
                        self.last_error = f"{key}: {e}"
                        ok = False
                        break
                data = buf.getvalue()
                if not ok or len(data) != size:
                    continue
                if self.bytes_in_ram + size > self.ram_limit:
                    # keep metadata, leave the bytes on disk for lazy load
                    self._meta[key] = {"size": size, "chunks": chunk_count(size),
                                       "atime": meta.get("atime", 0), "id": cid,
                                       "persisted": True}
                    continue
                self._mem[key] = data
                self.bytes_in_ram += size
                self._meta[key] = {"size": size, "chunks": chunk_count(size),
                                   "atime": meta.get("atime", 0), "id": cid,
                                   "persisted": True}
                if progress:
                    try: progress(n, total, key)
                    except Exception: pass
            self.loaded = True
            return len(self._mem)

    # -- reads -------------------------------------------------------------
    def get(self, key):
        with self._lock:
            if key in self._mem:
                self.hits += 1
                self._meta.setdefault(key, {})["atime"] = time.time()
                return self._mem[key]
            meta = self._meta.get(key)
            if meta and meta.get("persisted"):
                data = self._read_from_store(meta)
                if data is not None and len(data) == meta["size"]:
                    self._mem[key] = data
                    self.bytes_in_ram += len(data)
                    self._evict_if_needed()
                    self.hits += 1
                    return data
            self.misses += 1
            return None

    def _read_from_store(self, meta):
        buf = io.BytesIO()
        for i in range(meta.get("chunks", 1)):
            try:
                with open(self._chunk_path(meta["id"], i), "rb") as cf:
                    buf.write(cf.read())
            except Exception as e:
                self.last_error = str(e)
                return None
        return buf.getvalue()

    # -- writes ------------------------------------------------------------
    def put(self, key, data: bytes, persist=True, async_persist=True):
        """Store bytes in RAM (and, by default, in the 512 MB-chunked store)."""
        if data is None:
            return
        data = bytes(data)
        with self._lock:
            if key in self._mem:
                self.bytes_in_ram -= len(self._mem[key])
            self._mem[key] = data
            self.bytes_in_ram += len(data)
            cid = (self._meta.get(key) or {}).get("id") or _key_id(key)
            self._meta[key] = {"size": len(data), "chunks": chunk_count(len(data)),
                               "atime": time.time(), "id": cid, "persisted": False}
            self._evict_if_needed()
        if persist:
            if async_persist:
                threading.Thread(target=self._persist, args=(key,), daemon=True).start()
            else:
                self._persist(key)

    def _persist(self, key):
        try:
            with self._lock:
                data = self._mem.get(key)
                meta = self._meta.get(key)
                if data is None or meta is None:
                    return
                cid = meta["id"]
            os.makedirs(self.store_dir, exist_ok=True)
            written = 0
            for i, _off, chunk in iter_chunks(data):
                path = self._chunk_path(cid, i)
                tmp = path + ".tmp"
                with open(tmp, "wb") as f:
                    f.write(chunk)
                    f.flush()
                    try: os.fsync(f.fileno())
                    except Exception: pass
                os.replace(tmp, path)
                written = i + 1
            # drop stale chunks from a previously larger version
            i = written
            while os.path.exists(self._chunk_path(cid, i)):
                try: os.remove(self._chunk_path(cid, i))
                except Exception: break
                i += 1
            with self._lock:
                self._meta[key]["persisted"] = True
            self._save_index()
        except Exception as e:
            self.last_error = str(e)

    def invalidate(self, key):
        with self._lock:
            if key in self._mem:
                self.bytes_in_ram -= len(self._mem.pop(key))
            meta = self._meta.pop(key, None)
        if meta:
            i = 0
            while os.path.exists(self._chunk_path(meta["id"], i)):
                try: os.remove(self._chunk_path(meta["id"], i))
                except Exception: break
                i += 1
            self._save_index()

    def clear(self):
        with self._lock:
            keys = list(self._meta.keys())
        for k in keys:
            self.invalidate(k)
        with self._lock:
            self._mem.clear(); self._meta.clear(); self.bytes_in_ram = 0
        self._save_index()

    def flush(self):
        """Persist everything still marked dirty (call on shutdown)."""
        with self._lock:
            dirty = [k for k, m in self._meta.items() if not m.get("persisted")]
        for k in dirty:
            self._persist(k)
        self._save_index()

    def _save_index(self):
        try:
            os.makedirs(self.store_dir, exist_ok=True)
            with self._lock:
                index = {k: dict(v) for k, v in self._meta.items() if v.get("persisted")}
            tmp = self._index_path() + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(index, f)
            os.replace(tmp, self._index_path())
        except Exception as e:
            self.last_error = str(e)

    def _evict_if_needed(self):
        """LRU-evict RAM copies (persisted bytes stay in the chunk store)."""
        if self.bytes_in_ram <= self.ram_limit:
            return
        order = sorted(self._mem.keys(), key=lambda k: (self._meta.get(k) or {}).get("atime", 0))
        for k in order:
            if self.bytes_in_ram <= self.ram_limit:
                break
            meta = self._meta.get(k) or {}
            if not meta.get("persisted"):
                continue  # never drop bytes we have not written out yet
            self.bytes_in_ram -= len(self._mem.pop(k))

    # -- info --------------------------------------------------------------
    def stats(self):
        with self._lock:
            return {
                "entries": len(self._meta),
                "in_ram": len(self._mem),
                "bytes_in_ram": self.bytes_in_ram,
                "ram_limit": self.ram_limit,
                "hits": self.hits,
                "misses": self.misses,
                "chunk_size": CHUNK_SIZE,
                "store_dir": self.store_dir,
            }

    def keys(self):
        with self._lock:
            return list(self._meta.keys())
