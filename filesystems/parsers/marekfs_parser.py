"""
MarekFS Parser - Wrapper for existing MarekFS format.
This provides a unified interface to the existing MarekFS implementation.
"""
from typing import List, Optional
from ..common.base_parser import BaseFilesystemParser, FileEntry, FileType
from ..common.sector_reader import SectorReader


class MarekFSParser(BaseFilesystemParser):
    """
    Parser for MarekFS custom filesystem format.
    Wraps existing marekfs_core functionality.
    """
    
    FS_NAME = "MarekFS"
    
    # MarekFS signature (MKJOURNAL1 in first sector)
    MAGIC_SIGNATURES = [b"MKJOURNAL1"]
    
    def __init__(self, sector_reader: SectorReader):
        super().__init__(sector_reader)
        self.archive_data = None
    
    def detect(self) -> bool:
        """
        Detect MarekFS by checking for journal magic.
        
        Returns:
            True if MarekFS signature found
        """
        try:
            # Read first sector (journal start)
            sector0 = self.sector_reader.read_sectors(1, 1)
            return b"MKJOURNAL1" in sector0 or b"MAREKARCHV" in sector0
        except Exception:
            return False
    
    def initialize(self) -> bool:
        """
        Initialize MarekFS parser.
        
        Returns:
            True if successful
        """
        try:
            # Import marekfs_core functions
            from .. import marekfs_core
            # Read entire disk/image
            data = self.sector_reader.read_bytes(0, self.sector_reader.total_sectors * 512)
            # Try to parse as MarekFS archive
            self.archive_data = marekfs_core.parse_marekfs_archive(data)
            if self.archive_data:
                self.fs_info = {
                    "type": "MarekFS",
                    "version": "1.0",
                    "sector_size": 512,
                    "total_sectors": self.sector_reader.total_sectors,
                }
                self.initialized = True
                return True
            
            return False
        except Exception as e:
            print(f"MarekFS initialization error: {e}")
            return False
    
    def list_directory(self, path: str) -> List[FileEntry]:
        """
        List directory contents.
        
        Args:
            path: Directory path
            
        Returns:
            List of FileEntry objects
        """
        if not self.initialized:
            return []
        
        try:
            from .. import marekfs_core
            
            entries = []
            # Use existing MarekFS listing logic
            if isinstance(self.archive_data, dict):
                for filename, filedata in self.archive_data.items():
                    if path == "/" or filename.startswith(path):
                        size = len(filedata) if isinstance(filedata, (bytes, bytearray)) else filedata.get("size", 0)
                        entry = FileEntry(
                            name=filename,
                            path=f"{path}/{filename}" if path != "/" else f"/{filename}",
                            size=size,
                            file_type=FileType.FILE,
                            modified=None,
                            attributes={}
                        )
                        entries.append(entry)
            
            return entries
        except Exception as e:
            print(f"Error listing directory: {e}")
            return []
    
    def read_file(self, path: str, size: Optional[int] = None, offset: int = 0) -> bytes:
        """
        Read file data.
        
        Args:
            path: File path
            size: Number of bytes to read
            offset: Starting offset
            
        Returns:
            File data
        """
        if not self.initialized:
            return b""
        
        try:
            from .. import marekfs_core
            
            # Use existing MarekFS read logic
            if self.archive_data:
                # Extract filename from path
                filename = path.split("/")[-1]
                if filename in self.archive_data:
                    filedata = self.archive_data[filename]
                    data = filedata if isinstance(filedata, (bytes, bytearray)) else filedata.get("data", b"")
                    if offset > 0:
                        data = data[offset:]
                    if size:
                        data = data[:size]
                    return data
            return b""
        except Exception as e:
            print(f"Error reading file: {e}")
            return b""
    
    def get_file_info(self, path: str) -> Optional[FileEntry]:
        """
        Get file information.
        
        Args:
            path: File path
            
        Returns:
            FileEntry or None
        """
        if not self.initialized:
            return None
        
        try:
            filename = path.split("/")[-1]
            size = 0
            if isinstance(self.archive_data, dict) and filename in self.archive_data:
                filedata = self.archive_data[filename]
                size = len(filedata) if isinstance(filedata, (bytes, bytearray)) else filedata.get("size", 0)
            return FileEntry(
                name=filename,
                path=path,
                size=size,
                file_type=FileType.FILE,
            )
        except Exception:
            return None
    
    def exists(self, path: str) -> bool:
        """Check if file exists."""
        if not self.initialized:
            return False
        
        try:
            if isinstance(self.archive_data, dict):
                filename = path.split("/")[-1]
                return filename in self.archive_data
            return False
        except Exception:
            return False
    
    def is_directory(self, path: str) -> bool:
        """Check if path is directory."""
        return path == "/" or path == ""

    def write_file(self, path: str, data: bytes, offset: int = 0) -> bool:
        """Write into the MarekFS archive and persist it back to the image."""
        if not self.initialized:
            return False
        try:
            filename = path.split("/")[-1]
            if not isinstance(self.archive_data, dict):
                return False

            existing = self.archive_data.get(filename, b"")
            if offset == 0:
                self.archive_data[filename] = data
            else:
                if isinstance(existing, (bytes, bytearray)):
                    new_data = bytearray(existing)
                    if offset + len(data) > len(new_data):
                        new_data.extend(b"\x00" * (offset + len(data) - len(new_data)))
                    new_data[offset:offset + len(data)] = data
                    self.archive_data[filename] = bytes(new_data)
                else:
                    self.archive_data[filename] = data

            from .. import marekfs_core
            archive_blob = marekfs_core.create_marekfs_archive(self.archive_data)
            return self.sector_reader.write_bytes(0, archive_blob)
        except Exception as e:
            print(f"Error writing MarekFS file: {e}")
            return False
