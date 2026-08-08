import ctypes
from typing import List, Optional
from ..common.base_parser import BaseFilesystemParser, FileEntry, FileType
from ..common.sector_reader import SectorReader


class FAT16Parser(BaseFilesystemParser):
    FS_NAME = "FAT16"
    MAGIC_SIGNATURES = [b"FAT16   "]
    
    def __init__(self, sector_reader: SectorReader):
        super().__init__(sector_reader)
    
    def detect(self) -> bool:
        try:
            boot_sector = self.sector_reader.read_sectors(0, 1)
            if len(boot_sector) < 90:
                return False
            return boot_sector[82:90] == b"FAT16   "
        except Exception:
            return False
    
    def initialize(self) -> bool:
        try:
            drive_path = self.sector_reader.drive_path
            if drive_path.startswith(r"\\.\PhysicalDrive"):
                volume_path = r"\\.\C:"
            elif drive_path.startswith(r"\\.\\"):
                volume_path = drive_path
            else:
                volume_path = drive_path
            self.fs_info = {"type": "FAT16", "drive_path": drive_path}
            self.initialized = True
            return True
        except Exception as e:
            print(f"FAT16 init error: {e}")
            return False
    
    def _get_volume_path(self) -> str:
        drive_path = self.sector_reader.drive_path
        if drive_path.startswith(r"\\.\PhysicalDrive"):
            return r"\\.\C:"
        elif drive_path.startswith(r"\\.\\"):
            return drive_path
        return drive_path
    
    def list_directory(self, path: str) -> List[FileEntry]:
        if not self.initialized:
            return []
        entries = []
        try:
            import ctypes.wintypes
            from ctypes import wintypes
            class WIN32_FIND_DATAW(ctypes.Structure):
                _fields_ = [
                    ("dwFileAttributes", wintypes.DWORD),
                    ("ftCreationTime", wintypes.FILETIME),
                    ("ftLastAccessTime", wintypes.FILETIME),
                    ("ftLastWriteTime", wintypes.FILETIME),
                    ("nFileSizeHigh", wintypes.DWORD),
                    ("nFileSizeLow", wintypes.DWORD),
                    ("dwReserved0", wintypes.DWORD),
                    ("dwReserved1", wintypes.DWORD),
                    ("cFileName", wintypes.WCHAR * 260),
                    ("cAlternateFileName", wintypes.WCHAR * 14),
                ]
            kernel32 = ctypes.windll.kernel32
            find_data = WIN32_FIND_DATAW()
            search_path = self._get_volume_path() + "\\*" if path == "/" else self._get_volume_path() + "\\" + path.lstrip("/") + "\\*"
            hFind = kernel32.FindFirstFileW(search_path, ctypes.byref(find_data))
            if hFind == -1:
                return entries
            try:
                while True:
                    name = find_data.cFileName
                    if name not in (".", ".."):
                        is_dir = bool(find_data.dwFileAttributes & 0x10)
                        size = (find_data.nFileSizeHigh << 32) | find_data.nFileSizeLow
                        entries.append(FileEntry(name=name, path=f"{path}/{name}" if path != "/" else f"/{name}", size=size, file_type=FileType.DIRECTORY if is_dir else FileType.FILE))
                    if not kernel32.FindNextFileW(hFind, ctypes.byref(find_data)):
                        break
            finally:
                kernel32.FindClose(hFind)
            return entries
        except Exception as e:
            print(f"Error listing FAT16 dir: {e}")
            return []
    
    def read_file(self, path: str, size: Optional[int] = None, offset: int = 0) -> bytes:
        if not self.initialized:
            return b""
        try:
            if path.startswith("/"):
                path = path[1:]
            full_path = self._get_volume_path() + "\\" + path
            handle = ctypes.windll.kernel32.CreateFileW(full_path, 0x80000000, 0x00000001, None, 3, 0, None)
            if handle == -1:
                return b""
            try:
                if offset > 0:
                    distance_high = ctypes.c_ulong(0)
                    distance_low = ctypes.c_ulong(offset & 0xFFFFFFFF)
                    ctypes.windll.kernel32.SetFilePointer(handle, distance_low, ctypes.byref(distance_high), 0)
                buffer = ctypes.create_string_buffer(size if size else 65536)
                bytes_read = ctypes.c_ulong(0)
                ctypes.windll.kernel32.ReadFile(handle, buffer, len(buffer), ctypes.byref(bytes_read), None)
                return buffer.raw[:bytes_read.value]
            finally:
                ctypes.windll.kernel32.CloseHandle(handle)
        except Exception as e:
            print(f"Error reading FAT16 file: {e}")
            return b""
    
    def get_file_info(self, path: str) -> Optional[FileEntry]:
        entries = self.list_directory("/".join(path.split("/")[:-1]) or "/")
        filename = path.split("/")[-1]
        for entry in entries:
            if entry.name == filename:
                return entry
        return None
    
    def exists(self, path: str) -> bool:
        return self.get_file_info(path) is not None
    
    def is_directory(self, path: str) -> bool:
        info = self.get_file_info(path)
        return info is not None and info.file_type == FileType.DIRECTORY

    def write_file(self, path: str, data: bytes, offset: int = 0) -> bool:
        if not self.initialized:
            return False
        try:
            if path.startswith("/"):
                path = path[1:]
            full_path = self._get_volume_path() + "\\" + path
            GENERIC_WRITE = 0x40000000
            FILE_SHARE_READ = 0x00000001
            FILE_SHARE_WRITE = 0x00000002
            OPEN_EXISTING = 3
            handle = ctypes.windll.kernel32.CreateFileW(full_path, GENERIC_WRITE, FILE_SHARE_READ | FILE_SHARE_WRITE, None, OPEN_EXISTING, 0, None)
            if handle == -1:
                return False
            try:
                if offset > 0:
                    distance_high = ctypes.c_ulong(0)
                    distance_low = ctypes.c_ulong(offset & 0xFFFFFFFF)
                    ctypes.windll.kernel32.SetFilePointer(handle, distance_low, ctypes.byref(distance_high), 0)
                bytes_written = ctypes.c_ulong(0)
                ctypes.windll.kernel32.WriteFile(handle, data, len(data), ctypes.byref(bytes_written), None)
                return bytes_written.value == len(data)
            finally:
                ctypes.windll.kernel32.CloseHandle(handle)
        except Exception:
            return False