#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Verification script for marekfs_scanner_monitor.py"""

import sys
import os

# Read the file and check for key elements
filepath = r"c:\Users\Mayak\Downloads\.workspace\marekfs_scanner_monitor.py"

try:
    with open(filepath, 'r', encoding='utf-8') as f:
        content = f.read()
    
    checks = {
        "4.5GB limit": "MAX_SCAN_FILE_SIZE = 4.5 * 1024 * 1024 * 1024" in content,
        "Single MAX_SCAN_FILE_SIZE definition": content.count("MAX_SCAN_FILE_SIZE = 4.5 * 1024 * 1024 * 1024") == 1,
        "FILE_CHANGE_DEBOUNCE": "FILE_CHANGE_DEBOUNCE = 2.0" in content,
        "Single FILE_CHANGE_DEBOUNCE": content.count("FILE_CHANGE_DEBOUNCE = 2.0") == 1,
        "QuarantineManager class": "class QuarantineManager:" in content,
        "WhitelistManager class": "class WhitelistManager:" in content,
        "ToastNotifier class": "class ToastNotifier:" in content,
        "FileMonitor class": "class FileMonitor:" in content,
        "ScannerMonitor class": "class ScannerMonitor:" in content,
        "show_threat_notification": "show_threat_notification" in content,
        "quarantine_file": "quarantine_file" in content,
        "handle_toast_action": "handle_toast_action" in content,
        "Windows toast": "Windows toast" in content or "MarekFS Threat Detected" in content,
    }
    
    print("=" * 60)
    print("MarekFS Scanner Monitor - Verification Report")
    print("=" * 60)
    
    all_pass = True
    for check_name, result in checks.items():
        status = "PASS" if result else "FAIL"
        print(f"{status}: {check_name}")
        if not result:
            all_pass = False
    
    print("=" * 60)
    if all_pass:
        print("All checks passed")
    else:
        print("Some checks failed")
    
    print("=" * 60)
    
    # Check for duplicate lines issue
    max_scan_count = content.count("MAX_SCAN_FILE_SIZE")
    print(f"MAX_SCAN_FILE_SIZE occurrences: {max_scan_count}")
    
    debounce_count = content.count("FILE_CHANGE_DEBOUNCE = 2.0")
    print(f"FILE_CHANGE_DEBOUNCE = 2.0 occurrences: {debounce_count}")
    
except Exception as e:
    print(f"Error: {e}")
    sys.exit(1)