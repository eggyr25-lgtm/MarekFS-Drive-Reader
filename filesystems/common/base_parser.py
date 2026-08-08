"""
Base Parser - Abstract base class for all filesystem parsers.
Defines the interface that all filesystem parsers must implement.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import List, Optional, Dict, Any
from datetime import datetime
from enum import Enum


class FileType(Enum):
    """File type enumeration."""
    FILE = "file"
    DIRECTORY = "directory"
    SYMLINK = "symlink"
    HIDDEN = "hidden"
    SYSTEM = "system"


@dataclass
class FileEntry:
    """
    Represents a file or directory entry from any filesystem.
    Normalized across different filesystem formats.
    """
    name: str
    path: str
    size: int
    file_type: FileType
    created: Optional[datetime] = None
    modified: Optional[datetime] = None
    accessed: Optional[datetime] = None
    attributes: Dict[str, Any] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "name": self.name,
            "path": self.path,
            "size": self.size,
            "file_type": self.file_type.value,
            "created": self.created.isoformat() if self.created else None,
            "modified": self.modified.isoformat() if self.modified else None,
            "accessed": self.accessed.isoformat() if self.accessed else None,
            "attributes": self.attributes,
            "metadata": self.metadata,
        }
    
    def __repr__(self):
        return f"FileEntry({self.name}, {self.file_type.value}, {self.size} bytes)"


class BaseFilesystemParser(ABC):
    """
    Abstract base class for all filesystem parsers.
    Each filesystem implementation must inherit from this class.
    """
    
    # Filesystem name (e.g., "NTFS", "FAT32", "APFS")
    FS_NAME: str = "base"
    
    # Filesystem signature/magic bytes (optional, for detection)
    MAGIC_SIGNATURES: List[bytes] = []
    
    # Supported sector sizes
    SUPPORTED_SECTOR_SIZES: List[int] = [512, 4096]
    
    def __init__(self, sector_reader):
        """
        Initialize parser with sector reader.
        
        Args:
            sector_reader: SectorReader instance for disk I/O
        """
        self.sector_reader = sector_reader
        self.initialized = False
        self.fs_info: Dict[str, Any] = {}
    
    @abstractmethod
    def detect(self) -> bool:
        """
        Detect if this filesystem is present at the current position.
        
        Returns:
            True if filesystem is detected, False otherwise
        """
        pass
    
    @abstractmethod
    def initialize(self) -> bool:
        """
        Initialize parser and read filesystem metadata.
        
        Returns:
            True if initialization successful, False otherwise
        """
        pass
    
    @abstractmethod
    def list_directory(self, path: str) -> List[FileEntry]:
        """
        List contents of a directory.
        
        Args:
            path: Directory path (filesystem-specific format)
            
        Returns:
            List of FileEntry objects
        """
        pass
    
    @abstractmethod
    def read_file(self, path: str, size: Optional[int] = None, offset: int = 0) -> bytes:
        """
        Read file data.
        
        Args:
            path: File path
            size: Number of bytes to read (None = entire file)
            offset: Starting offset in file
            
        Returns:
            File data as bytes
        """
        pass
    
    @abstractmethod
    def get_file_info(self, path: str) -> Optional[FileEntry]:
        """
        Get information about a file or directory.
        
        Args:
            path: File/directory path
            
        Returns:
            FileEntry object or None if not found
        """
        pass
    
    @abstractmethod
    def exists(self, path: str) -> bool:
        """
        Check if file or directory exists.
        
        Args:
            path: File/directory path
            
        Returns:
            True if exists, False otherwise
        """
        pass
    
    @abstractmethod
    def is_directory(self, path: str) -> bool:
        """
        Check if path is a directory.
        
        Args:
            path: Path to check
            
        Returns:
            True if directory, False otherwise
        """
        pass
    
    def get_filesystem_info(self) -> Dict[str, Any]:
        """
        Get filesystem metadata (label, size, etc.).
        
        Returns:
            Dictionary with filesystem information
        """
        return self.fs_info
    
    def mount(self) -> bool:
        """
        Mount/activate the filesystem for reading.
        
        Returns:
            True if mount successful
        """
        if not self.initialized:
            if not self.initialize():
                return False
        self.initialized = True
        return True
    
    def unmount(self):
        """Unmount/cleanup the filesystem."""
        self.initialized = False
        self.fs_info.clear()
    
    def __repr__(self):
        status = "mounted" if self.initialized else "unmounted"
        return f"{self.FS_NAME}Parser({status})"

    # --- Convenience raw helpers ---
    def raw_read(self, path: str, size: Optional[int] = None, offset: int = 0) -> bytes:
        """Read raw bytes from the underlying sector reader. This provides a
        simple fallback for parsers that do not implement fine-grained file
        parsing. If `size` is None, reads to the end of the image."""
        try:
            total = 0
            try:
                total = self.sector_reader.get_total_sectors() * self.sector_reader.get_sector_size()
            except Exception:
                total = 0
            if size is None:
                size = max(0, total - offset) if total > 0 else None
            if size is None:
                return b""
            return self.sector_reader.read_bytes(offset, size)
        except Exception:
            return b""

    def raw_write(self, offset: int, data: bytes) -> bool:
        """Write raw bytes to the underlying sector reader. Returns True on
        success."""
        try:
            if hasattr(self.sector_reader, 'write_bytes'):
                return bool(self.sector_reader.write_bytes(offset, data))
            return False
        except Exception:
            return False
