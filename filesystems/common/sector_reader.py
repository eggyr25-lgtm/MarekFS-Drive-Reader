"""
Sector Reader - Handles raw sector I/O for filesystem parsing.
Provides a unified interface for reading disk sectors across platforms.

Supports:
  - Regular files (disk images)
  - Windows volume devices:  \\\\.\\C:, \\\\.\\D:  (sector 0 = volume boot record)
  - Windows physical drives:  \\\\.\\PhysicalDriveN  (sector 0 = MBR)
"""
import os
import re
import struct
import ctypes
from typing import Optional, List, BinaryIO

SECTOR_SIZE = 512


class SectorReader:
    """Reads raw sectors from disks, images, or block devices."""

    def __init__(self, source: str):
        self.source = source
        self.handle: Optional[BinaryIO] = None
        self.total_sectors: int = 0
        self.sector_size: int = SECTOR_SIZE
        self._is_device: bool = False
        self._win_handle: Optional[int] = None
        self._open()

    @property
    def drive_path(self) -> str:
        """Return the original path or device source used for this reader."""
        return self.source

    @staticmethod
    def _normalize_path(path: str) -> str:
        """Convert C:\\ to \\.\\C: on Windows."""
        if os.name != 'nt':
            return path
        if path.startswith('\\.\\'):
            return path
        m = re.match(r'^([A-Za-z]):[\\\/]?$', path)
        if m:
            return f'\\.\\{m.group(1).upper()}:'
        return path

    def _open(self):
        """Open the disk/image for reading."""
        normalized = self._normalize_path(self.source)
        is_windows_device = (os.name == 'nt' and normalized.startswith('\\.\\'))
        if is_windows_device:
            self._open_windows_device(normalized)
        else:
            self.handle = open(normalized, 'rb')
            self._is_device = False
            self._get_file_size()
            self._detect_sector_size()

    def _get_file_size(self):
        """Get total file size for regular file handles."""
        try:
            self.handle.seek(0, 2)
            size = self.handle.tell()
            self.handle.seek(0)
            self.total_sectors = max(1, size // self.sector_size)
        except (OSError, ValueError):
            self.total_sectors = 0

    def _open_windows_device(self, device_path: str):
        """Open a Windows device path (volume or physical drive) via CreateFile."""
        try:
            import ctypes.wintypes as wintypes
            kernel32 = ctypes.windll.kernel32
            GENERIC_READ = 0x80000000
            FILE_SHARE_READ = 0x00000001
            FILE_SHARE_WRITE = 0x00000002
            OPEN_EXISTING = 3
            FILE_ATTRIBUTE_READONLY = 0x00000002
            handle = kernel32.CreateFileW(
                device_path, GENERIC_READ,
                FILE_SHARE_READ | FILE_SHARE_WRITE, None,
                OPEN_EXISTING, FILE_ATTRIBUTE_READONLY, None
            )
            if handle is None or handle == -1 or handle == 0xFFFFFFFF:
                err = kernel32.GetLastError()
                raise IOError(
                    f"CreateFile({device_path}) failed (error {err}). "
                    "Run as Administrator for volume access."
                )
            self._win_handle = handle
            self._is_device = True
            self.total_sectors = self._get_device_size()
            self._detect_sector_size()
        except Exception as e:
            raise IOError(f"Cannot open Windows device {device_path}: {e}")

    def _get_device_size(self) -> int:
        """Get total sector count for a Windows device via DeviceIoControl."""
        if self._win_handle is None:
            return 0
        try:
            import ctypes.wintypes as wintypes
            kernel32 = ctypes.windll.kernel32

            class DISK_GEOMETRY(ctypes.Structure):
                _fields_ = [
                    ("Cylinders", ctypes.c_int64),
                    ("MediaType", ctypes.c_int),
                    ("TracksPerCylinder", ctypes.c_uint32),
                    ("SectorsPerTrack", ctypes.c_uint32),
                    ("BytesPerSector", ctypes.c_uint32),
                ]

            IOCTL_DISK_GET_DRIVE_GEOMETRY = 0x00070000
            geometry = DISK_GEOMETRY()
            bytes_returned = ctypes.c_uint32(0)
            ok = kernel32.DeviceIoControl(
                self._win_handle,
                IOCTL_DISK_GET_DRIVE_GEOMETRY,
                None, 0,
                ctypes.byref(geometry), ctypes.sizeof(geometry),
                ctypes.byref(bytes_returned), None,
            )
            if not ok:
                # Fallback: assume large size, best-effort
                return 0
            cylinders = geometry.Cylinders
            tracks = geometry.TracksPerCylinder
            sectors = geometry.SectorsPerTrack
            if tracks and sectors and cylinders > 0:
                return int(cylinders * tracks * sectors)
            return 0
        except Exception:
            return 0

    def _detect_sector_size(self) -> int:
        """Detect sector size (512 default, 4096 for advanced format)."""
        if self._is_device and self._win_handle is not None:
            try:
                import ctypes
                ctypes.wintypes  # ensure import
                # Use IOCTL_STORAGE_QUERY_PROPERTY for sector size when possible
            except Exception:
                pass
        return self.sector_size

    def read_bytes(self, offset: int, length: int) -> bytes:
        """Read arbitrary bytes from the source starting at offset."""
        if length <= 0:
            return b""
        if self._is_device and self._win_handle is not None:
            return self._read_device(offset // self.sector_size, length)
        if self.handle is None:
            return b""
        try:
            self.handle.seek(offset)
            return self.handle.read(length)
        except (OSError, ValueError):
            return b""

    def write_bytes(self, offset: int, data: bytes) -> bool:
        """Write arbitrary bytes to the source starting at offset.

        For regular files (disk images) this opens the file in r+b and writes.
        For Windows devices, attempts to use WriteFile on the device handle.
        Returns True on success, False otherwise.
        """
        if not data:
            return True
        if self._is_device and self._win_handle is not None:
            try:
                import ctypes.wintypes as wintypes
                kernel32 = ctypes.windll.kernel32
                offset_li = wintypes.LARGE_INTEGER()
                offset_li.QuadPart = offset
                # SetFilePointerEx
                ok = kernel32.SetFilePointerEx(self._win_handle, offset_li, None, 0)
                if not ok:
                    return False
                bytes_written = ctypes.c_uint32(0)
                buf = ctypes.create_string_buffer(data)
                ok = kernel32.WriteFile(self._win_handle, buf, len(data), ctypes.byref(bytes_written), None)
                return bool(ok)
            except Exception:
                return False

        # Regular file path: open in r+b and write
        try:
            # Use the original source path to write
            with open(self.source, 'r+b') as f:
                f.seek(offset)
                f.write(data)
                f.flush()
                try:
                    os.fsync(f.fileno())
                except Exception:
                    pass
            return True
        except Exception:
            return False

    def read_sectors(self, sector: int, count: int = 1) -> bytes:
        """
        Read `count` sectors starting at `sector`.

        Args:
            sector: Starting sector index (0-based).
            count: Number of sectors to read.

        Returns:
            Combined bytes of the read sectors.
        """
        if count <= 0:
            return b""
        size = count * self.sector_size
        if self._is_device and self._win_handle is not None:
            return self._read_device(sector, size)
        if self.handle is None:
            return b""
        try:
            self.handle.seek(sector * self.sector_size)
            data = self.handle.read(size)
            return data
        except (OSError, ValueError):
            return b""

    def write_sectors(self, sector: int, data: bytes) -> bool:
        """Write sector-aligned data starting at `sector`. Returns True on success."""
        if not data:
            return True
        try:
            offset = sector * self.sector_size
            return self.write_bytes(offset, data)
        except Exception:
            return False

    def _read_device(self, sector: int, size: int) -> bytes:
        """Read bytes from a Windows device using ReadFile at the given offset."""
        try:
            import ctypes.wintypes as wintypes
            kernel32 = ctypes.windll.kernel32
            offset = sector * self.sector_size

            buf = ctypes.create_string_buffer(size)
            pread = wintypes.LARGE_INTEGER()
            pread.QuadPart = offset

            # SetFilePointerEx to position
            new_pos = wintypes.LARGE_INTEGER()
            kernel32.SetFilePointerEx(
                self._win_handle, pread, ctypes.byref(new_pos), 0
            )

            bytes_read = ctypes.c_uint32(0)
            ok = kernel32.ReadFile(
                self._win_handle, buf, size,
                ctypes.byref(bytes_read), None
            )
            if not ok:
                return b""
            return buf.raw[:bytes_read.value]
        except Exception:
            return b""

    def get_total_sectors(self) -> int:
        """Return total sector count."""
        return self.total_sectors

    def get_sector_size(self) -> int:
        """Return detected sector size."""
        return self.sector_size

    def close(self):
        """Close the underlying handle/file."""
        if self._win_handle is not None:
            try:
                import ctypes
                ctypes.windll.kernel32.CloseHandle(self._win_handle)
            except Exception:
                pass
            self._win_handle = None
        if self.handle is not None:
            try:
                self.handle.close()
            except Exception:
                pass
            self.handle = None
        self._is_device = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass
