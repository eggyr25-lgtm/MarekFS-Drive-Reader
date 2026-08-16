# MarekFS Real-Time Scanner Monitor

## Overview
Silent file scanning system with Windows toast notifications, automatic quarantine (base64-encoded), and whitelist support. Monitors both MarekFS virtual filesystem and host system files in real-time.

## Features
- Real-time file monitoring - Scans new/modified files automatically
- Windows toast notifications - Interactive notifications with action buttons
- Auto-quarantine - Infected files moved to C:\ProgramData\MarekFS\Quarantine as base64
- Whitelist support - SHA-256 hash whitelist to bypass scanning
- Host scanning toggle - OFF by default, can be enabled in settings
- YARA + ClamAV - Uses existing MarekFS scanner backend

## Installation

1. Add to your MarekFS application startup:
   from marekfs_scanner_monitor import start_scanner_monitor
   start_scanner_monitor(enable_host_scanning=False)

2. The scanner will automatically:
   - Monitor MarekFS virtual filesystem files
   - Monitor host system folders when enabled
   - Scan new/modified files silently
   - Auto-quarantine infected files
   - Show toast notifications with actions

## Toast Notification Actions
- Delete - Permanently delete the quarantined file
- Keep in Quarantine - Leave file in quarantine
- Restore - Restore file to original location
- Restore and Whitelist - Restore and add hash to whitelist

## Configuration
Settings stored in C:\ProgramData\MarekFS\scanner_monitor_config.json

## API Reference

### ScannerMonitor
    monitor = ScannerMonitor(enable_host_scanning=False)
    monitor.start()
    monitor.stop()
    monitor.set_host_scanning(True)
    status = monitor.get_status()

### QuarantineManager
    qm = QuarantineManager()
    qid = qm.quarantine_file("/path/to/file.exe", "Trojan.Test", result)
    success, msg = qm.restore_file(qid, add_to_whitelist=True)
    success, msg = qm.delete_quarantined(qid)
    files = qm.get_quarantine_list()

### WhitelistManager
    wm = WhitelistManager()
    wm.add_hash("sha256hash", "filename.exe")
    is_safe = wm.is_whitelisted("sha256hash")
    wm.remove_hash("sha256hash")

## Testing
Run the module directly to test quarantine and restore functionality:
    python marekfs_scanner_monitor.py

## Security Notes
- Quarantine folder is hidden by default on Windows
- Base64 encoding prevents accidental execution
- File integrity verified on restore via SHA-256 hash

## Limitations
- Windows-only (toast notifications)
- Host scanning disabled by default
- No real-time kernel-level protection