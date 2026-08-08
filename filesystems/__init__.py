"""
MarekFS Extended Filesystem Support
Provides parsers for multiple filesystem formats beyond MarekFS.
"""
__version__ = "1.0.0"

from .common.sector_reader import SectorReader
from .common.base_parser import BaseFilesystemParser, FileEntry
from .parsers import get_parser, list_supported_filesystems, detect_filesystem

__all__ = [
    "SectorReader",
    "BaseFilesystemParser",
    "FileEntry",
    "get_parser",
    "list_supported_filesystems",
    "detect_filesystem",
]
