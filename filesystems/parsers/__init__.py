"""
Filesystem Parsers - Registry and factory for filesystem parsers.
"""
from typing import Dict, List, Optional, Type
from ..common.base_parser import BaseFilesystemParser
from ..common.sector_reader import SectorReader


# Registry of available parsers
_PARSERS: Dict[str, Type[BaseFilesystemParser]] = {}


def register_parser(name: str, parser_class: Type[BaseFilesystemParser]):
    """Register a filesystem parser."""
    _PARSERS[name.upper()] = parser_class


def get_parser(name: str) -> Optional[Type[BaseFilesystemParser]]:
    """Get parser class by name."""
    return _PARSERS.get(name.upper())


def list_supported_filesystems() -> List[str]:
    """Get list of all supported filesystems."""
    return sorted(_PARSERS.keys())


def detect_filesystem(sector_reader: SectorReader) -> Optional[Type[BaseFilesystemParser]]:
    """Auto-detect filesystem type from disk/image."""
    for name, parser_class in _PARSERS.items():
        try:
            parser = parser_class(sector_reader)
            if parser.detect():
                return parser_class
        except Exception:
            continue
    return None


def _load_parsers():
    """Load all available filesystem parsers."""
    parsers_to_load = [
        ("MarekFS", ".marekfs_parser", "MarekFSParser"),
        ("NTFS", ".ntfs_parser", "NTFSParser"),
        ("FAT32", ".fat32_parser", "FAT32Parser"),
        ("exFAT", ".exfat_parser", "ExFATParser"),
        ("FAT16", ".fat16_parser", "FAT16Parser"),
        ("EXT4", ".ext4_parser", "EXT4Parser"),
        ("EXT3", ".ext3_parser", "EXT3Parser"),
        ("EXT2", ".ext2_parser", "EXT2Parser"),
        ("EXT1", ".ext1_parser", "EXT1Parser"),
        ("HFS+", ".hfsp_parser", "HFSPParser"),
        ("HFS", ".hfs_parser", "HFSParser"),
        ("APFS", ".apfs_parser", "APFSParser"),
        ("BTRFS", ".btrfs_parser", "BTRFSParser"),
        ("RAMFS", ".ramfs_parser", "RAMFSParser"),
        ("TMPFS", ".tmpfs_parser", "TMPFSParser"),
    ]
    
    for fs_name, module_name, class_name in parsers_to_load:
        try:
            module = __import__(__name__ + module_name, fromlist=[class_name])
            parser_class = getattr(module, class_name)
            register_parser(fs_name, parser_class)
        except ImportError:
            pass
        except Exception:
            pass


_load_parsers()
