#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Cleanup script to remove duplicate MAX_SCAN_FILE_SIZE definitions."""

import sys

filepath = r"c:\Users\Mayak\Downloads\.workspace\marekfs_scanner_monitor.py"

try:
    with open(filepath, 'r', encoding='utf-8') as f:
        content = f.read()
    
    lines = content.split('\n')
    clean = []
    seen_max = False
    
    for line in lines:
        if 'MAX_SCAN_FILE_SIZE' in line:
            if not seen_max:
                clean.append(line)
                seen_max = True
            continue
        else:
            clean.append(line)
    
    with open(filepath, 'w', encoding='utf-8') as f:
        f.write('\n'.join(clean))
    
    print(f"Successfully cleaned up duplicate MAX_SCAN_FILE_SIZE definitions")
    print(f"File now has {len([l for l in clean if 'MAX_SCAN_FILE_SIZE' in l])} MAX_SCAN_FILE_SIZE definition(s)")
    
except Exception as e:
    print(f"Error: {e}")
    sys.exit(1)