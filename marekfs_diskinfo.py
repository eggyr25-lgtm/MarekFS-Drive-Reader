# marekfs_diskinfo.py

import os
import re


def _format_bytes(size_in_bytes: int) -> str:
    if size_in_bytes <= 0:
        return "0 B"
    units = ["B", "KB", "MB", "GB", "TB", "PB", "EB"]
    unit_idx = 0
    size = float(size_in_bytes)
    while size >= 1024.0 and unit_idx < len(units) - 1:
        size /= 1024.0
        unit_idx += 1
    return f"{int(size)} B" if unit_idx == 0 else f"{size:.2f} {units[unit_idx]}"


def _query_physical_drive_info(physical_path: str) -> dict:
    """Query Windows physical drive for vendor/product/serial and bus type.

    Returns a dict with keys: vendor, product, revision, serial, bus_type (int),
    bus_type_name (friendly string). Best-effort and non-fatal on errors.
    """
    info = {"vendor": None, "product": None, "revision": None, "serial": None,
            "bus_type": None, "bus_type_name": None, "error": None}
    try:
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.windll.kernel32

        # Constants
        GENERIC_READ = 0x80000000
        FILE_SHARE_READ = 0x00000001
        FILE_SHARE_WRITE = 0x00000002
        OPEN_EXISTING = 3
        # IOCTL_STORAGE_QUERY_PROPERTY
        IOCTL_STORAGE_QUERY_PROPERTY = 0x2D1400

        handle = kernel32.CreateFileW(physical_path, GENERIC_READ,
                                       FILE_SHARE_READ | FILE_SHARE_WRITE,
                                       None, OPEN_EXISTING, 0, None)
        if handle is None or handle == wintypes.HANDLE(-1).value:
            info["error"] = f"CreateFile failed for {physical_path}"
            return info

        try:
            # STORAGE_PROPERTY_QUERY: PropertyId (DWORD)=0 (StorageDeviceProperty), QueryType (DWORD)=0
            inbuf = (ctypes.c_ubyte * 8)()
            ctypes.memmove(inbuf, (0).to_bytes(4, 'little') + (0).to_bytes(4, 'little'), 8)
            out_size = 1024
            outbuf = (ctypes.c_ubyte * out_size)()
            bytes_returned = wintypes.DWORD()

            ok = kernel32.DeviceIoControl(handle,
                                           IOCTL_STORAGE_QUERY_PROPERTY,
                                           ctypes.byref(inbuf), ctypes.sizeof(inbuf),
                                           ctypes.byref(outbuf), out_size,
                                           ctypes.byref(bytes_returned), None)
            if not ok:
                info["error"] = "DeviceIoControl failed"
                return info

            raw = bytes(bytearray(outbuf))[:bytes_returned.value]
            if len(raw) < 36:
                return info

            import struct
            # Parse STORAGE_DEVICE_DESCRIPTOR-like layout
            # Offsets: 0:Version(4),4:Size(4),8:DeviceType(1),9:DeviceTypeModifier(1),10:Removable(1),11:CmdQueue(1)
            # 12:VendorIdOffset(4),16:ProductIdOffset(4),20:ProductRevisionOffset(4),24:SerialNumberOffset(4),28:BusType(4)
            try:
                vendor_off = struct.unpack_from('<I', raw, 12)[0]
                product_off = struct.unpack_from('<I', raw, 16)[0]
                revision_off = struct.unpack_from('<I', raw, 20)[0]
                serial_off = struct.unpack_from('<I', raw, 24)[0]
                bus_type = struct.unpack_from('<I', raw, 28)[0]
            except Exception:
                return info

            def _read_z(offset: int):
                if not offset or offset >= len(raw):
                    return None
                try:
                    s = raw[offset:raw.find(b'\x00', offset)]
                    return s.decode('utf-8', errors='ignore') if s else None
                except Exception:
                    return None

            info['vendor'] = _read_z(vendor_off)
            info['product'] = _read_z(product_off)
            info['revision'] = _read_z(revision_off)
            info['serial'] = _read_z(serial_off)
            info['bus_type'] = int(bus_type)

            bus_map = {
                0: 'Unknown', 1: 'Scsi', 2: 'Atapi', 3: 'Ata', 4: '1394', 5: 'Ssa',
                6: 'Fibre', 7: 'USB', 8: 'RAID', 9: 'iSCSI', 10: 'SAS', 11: 'SATA',
                12: 'SD', 13: 'MMC', 14: 'Virtual', 15: 'FileBackedVirtual', 16: 'Spaces',
                17: 'NVMe'
            }
            info['bus_type_name'] = bus_map.get(info['bus_type'], f'Unknown({info["bus_type"]})')
        finally:
            try:
                kernel32.CloseHandle(handle)
            except Exception:
                pass
    except Exception as e:
        info['error'] = str(e)
    return info


def get_disk_info(drive_path):
    """Collect drive or image metadata for display."""
    info = {
        "requested_path": drive_path,
        "normalized_path": drive_path,
        "type": "unknown",
        "size_bytes": 0,
        "size": "0 B",
        "volume_label": None,
        "filesystem": None,
        "serial": None,
        "error": None,
    }

    try:
        import marekfs_core
    except Exception as e:
        info["error"] = f"Unable to import marekfs_core: {e}"
        return info

    try:
        normalized = marekfs_core._normalize_windows_drive_path(drive_path)
        info["normalized_path"] = normalized
        size_bytes = marekfs_core.get_drive_size_bytes(drive_path)
        info["size_bytes"] = size_bytes
        info["size"] = _format_bytes(size_bytes)

        if os.name == "nt":
            info["path_type"] = "windows"
            lp = normalized.lower() if isinstance(normalized, str) else ""
            # detect physical drive by common substring (e.g. \\\\.\\PhysicalDrive1)
            if "physicaldrive" in lp:
                info["type"] = "physical_drive"
                # Query additional physical drive details
                drv_info = _query_physical_drive_info(normalized)
                if drv_info:
                    info.update({
                        "vendor": drv_info.get("vendor"),
                        "product": drv_info.get("product"),
                        "firmware": drv_info.get("revision"),
                        "serial": drv_info.get("serial") or info.get("serial"),
                        "bus_type": drv_info.get("bus_type"),
                        "bus_type_name": drv_info.get("bus_type_name"),
                        "drv_error": drv_info.get("error"),
                    })
            # drive letter like C:\ or normalized volume path '\\.\C:'
            elif re.match(r'^[A-Za-z]:', drive_path) or re.match(r'^\\\\.\\[A-Za-z]:', normalized, re.IGNORECASE):
                info["type"] = "volume"
            else:
                info["type"] = "image_file"

            drive_letter_match = re.match(r"^([A-Za-z]):[\\/]?$", drive_path)
            if drive_letter_match:
                root_path = f"{drive_letter_match.group(1).upper()}:\\"
                try:
                    import ctypes
                    from ctypes import wintypes
                    volume_name = ctypes.create_unicode_buffer(260)
                    fs_name = ctypes.create_unicode_buffer(260)
                    serial_number = wintypes.DWORD()
                    max_component_len = wintypes.DWORD()
                    file_system_flags = wintypes.DWORD()

                    ok = ctypes.windll.kernel32.GetVolumeInformationW(
                        root_path,
                        volume_name,
                        ctypes.sizeof(volume_name),
                        ctypes.byref(serial_number),
                        ctypes.byref(max_component_len),
                        ctypes.byref(file_system_flags),
                        fs_name,
                        ctypes.sizeof(fs_name),
                    )
                    if ok:
                        info["volume_label"] = volume_name.value or None
                        info["filesystem"] = fs_name.value or None
                        info["serial"] = f"{serial_number.value:04X}"
                except Exception:
                    pass
        else:
            # Non-Windows: classify image files and normal paths.
            if os.path.exists(drive_path):
                if os.path.isdir(drive_path):
                    info["type"] = "directory"
                elif os.path.isfile(drive_path):
                    info["type"] = "file"
                else:
                    info["type"] = "path"
            else:
                info["type"] = "image_file"
    except Exception as exc:
        info["error"] = str(exc)

    return info
def format_disk_info(info):
    """Format the disk info dictionary into user-facing text."""
    if not isinstance(info, dict):
        return str(info)

    if info.get("error"):
        return f"Error obtaining disk info:\n{info['error']}"

    lines = [
        f"Requested Path: {info.get('requested_path')}",
        f"Normalized Path: {info.get('normalized_path')}",
        f"Type: {info.get('type')}",
        f"Size: {info.get('size')} ({info.get('size_bytes', 0)} bytes)",
    ]

    if info.get("volume_label") is not None:
        lines.append(f"Volume Label: {info.get('volume_label')}")
    if info.get("filesystem") is not None:
        lines.append(f"Filesystem: {info.get('filesystem')}")
    if info.get("serial") is not None:
        lines.append(f"Serial: {info.get('serial')}")

    # Device identity
    vendor = info.get('vendor')
    product = info.get('product')
    firmware = info.get('firmware')
    serial = info.get('serial')
    bus_name = info.get('bus_type_name')
    bus_id = info.get('bus_type')

    if vendor or product:
        name = " ".join([x for x in (vendor, product) if x])
        lines.append(f"Drive Name: {name}")
    if vendor:
        lines.append(f"Vendor: {vendor}")
    if product:
        lines.append(f"Product: {product}")
    if firmware:
        lines.append(f"Firmware: {firmware}")
    if serial:
        lines.append(f"Serial: {serial}")

    if bus_name or bus_id is not None:
        # Friendly interface descriptions
        friendly_map = {
            'Sata': 'SATA', 'SATA': 'SATA', 'Ata': 'ATA/PATA', 'Ata': 'ATA/PATA',
            'NVMe': 'NVMe (PCIe)', 'USB': 'USB', 'Scsi': 'SCSI', 'Atapi': 'ATAPI',
            'SAS': 'SAS', 'Fibre': 'Fibre Channel', 'iSCSI': 'iSCSI', 'Virtual': 'Virtual Device'
        }
        friendly = None
        if isinstance(bus_name, str):
            friendly = friendly_map.get(bus_name, bus_name)
        elif bus_id is not None:
            friendly = f"BusType {bus_id}"
        if friendly:
            lines.append(f"Interface: {friendly}")
        else:
            lines.append(f"Interface: {bus_name or bus_id}")

    return "\n".join(lines)

