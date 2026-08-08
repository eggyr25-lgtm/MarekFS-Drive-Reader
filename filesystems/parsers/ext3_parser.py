import struct
from typing import List, Optional
from ..common.base_parser import BaseFilesystemParser, FileEntry, FileType
from ..common.sector_reader import SectorReader


class EXT3Parser(BaseFilesystemParser):
    FS_NAME = "EXT3"
    MAGIC_SIGNATURES = [struct.pack("<H", 0xEF53)]
    
    def __init__(self, sector_reader: SectorReader):
        super().__init__(sector_reader)
    
    def detect(self) -> bool:
        try:
            boot_sector = self.sector_reader.read_sectors(0, 1)
            if len(boot_sector) < 0x438 + 2:
                return False
            magic = struct.unpack("<H", boot_sector[0x438:0x43A])[0]
            return magic == 0xEF53
        except Exception:
            return False
    
    def initialize(self) -> bool:
        try:
            self.fs_info = {"type": "EXT3", "drive_path": self.sector_reader.drive_path}
            self.initialized = True
            return True
        except Exception as e:
            print(f"EXT3 init error: {e}")
            return False
    
    def list_directory(self, path: str) -> List[FileEntry]:
        return []
    
    def read_file(self, path: str, size: Optional[int] = None, offset: int = 0) -> bytes:
        return self.raw_read(path, size=size, offset=offset)

    def write_file(self, path: str, data: bytes, offset: int = 0) -> bool:
        return self.raw_write(offset, data)
    
    def get_file_info(self, path: str) -> Optional[FileEntry]:
        return None
    
    def exists(self, path: str) -> bool:
        return False
    
    def is_directory(self, path: str) -> bool:
        return False