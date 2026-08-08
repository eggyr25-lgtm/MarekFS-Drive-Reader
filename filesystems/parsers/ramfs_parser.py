from typing import List, Optional
from ..common.base_parser import BaseFilesystemParser, FileEntry, FileType
from ..common.sector_reader import SectorReader

class RAMFSParser(BaseFilesystemParser):
    FS_NAME = "RAMFS"
    MAGIC_SIGNATURES = []
    
    def __init__(self, sector_reader: SectorReader):
        super().__init__(sector_reader)
    
    def detect(self) -> bool:
        return False
    
    def initialize(self) -> bool:
        self.fs_info = {"type": "RAMFS", "drive_path": self.sector_reader.drive_path}
        self.initialized = True
        return True
    
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
