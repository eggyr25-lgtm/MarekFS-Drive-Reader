"""MarekFS Scriptr v2: restricted, preview-first disk automation DSL.

The engine NEVER evals/execs script text — every statement is parsed into a
bounded action plan, previewed, and only then executed with explicit
confirmation. Every modified file/sector is backed up under ProgramData
before it is touched, and dry-run previews simulate file operations
virtually so create -> write -> read scripts preview correctly.

Command families:
  control : print/echo, set/let, sleep/wait, help, version, printvars
  files   : createnewfile, createdir/mkdir, writetofile, appendtofile,
            prependtofile, deletefile, renamefile/movefile,
            copyfile/duplicatefile, clonefile, touchfile, truncatefile,
            fillfile, wipefile, deletedir/rmdir, setattributes,
            clearattributes, encryptfile, decryptfile, makearchive,
            addtoarchive, extractarchive, exportfile, importfile,
            makehexfile
  query   : QueryFileOnDiskAmount, listfiles, countfiles, fileexists,
            filesize, fileid, sectorof, readfile/cat, hexdump, checksum,
            verifyfile, findfirstbytes, findcontent, diskstats, freespace
  sector  : ifsectorhexdatamatches({HEX}) then zerofill | swapdatato |
            randomsector | wipe | fillwithpattern
  filter  : if file <expr> then <action>   (action applies to matches)
"""
import fnmatch
import hashlib
import json
import os
import random
import re
import shlex
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from ui_custom import theme_existing_window
from marekfs_core import (
    SECTOR_SIZE, PROGRAM_DATA_CONFIG_DIR, read_sectors, write_with_journal,
    open_drive, prepare_file_payload, data_checksum, save_file_metadata,
    file_id_for_record, save_file_id_database,
    create_marekfs_archive, parse_marekfs_archive,
    MAX_LOGICAL_FILENAME_CHARS, MAX_FILE_COUNT,
    FILE_ATTR_HIDDEN, FILE_ATTR_READONLY, FILE_ATTR_SYSTEM, FILE_ATTR_ARCHIVE,
    FILE_ATTR_COMPRESSED, FILE_ATTR_ENCRYPTED, FILE_ATTR_DIRECTORY,
)

MODULE_DIR = os.path.join(PROGRAM_DATA_CONFIG_DIR, "scriptr_modules")
BACKUP_DIR = os.path.join(PROGRAM_DATA_CONFIG_DIR, "scriptr_backups")
MAX_SCRIPT_BYTES, MAX_ACTIONS, MAX_WRITE_SECTORS = 256 * 1024, 1000, 4096

SCRIPTR_VERSION = "2.0"
EXPORT_MAX_BYTES = 512 * 1024 * 1024      # host export cap
IMPORT_MAX_BYTES = 256 * 1024 * 1024      # host import cap
MAX_READ_DISPLAY = 8192                   # readfile preview cap (chars)
MAX_HEX_DISPLAY = 4096                    # hexdump preview cap (bytes)
MAX_FIND_BYTES = 4 * 1024 * 1024          # findcontent per-file scan cap
MAX_SLEEP_MS = 10000                      # sleep cap (keeps scripts bounded)

class ScriptrError(ValueError): pass

def hex_bytes(value):
    value = value.strip().replace("0x", "").replace(" ", "")
    if not value or len(value) % 2 or not re.fullmatch(r"[0-9a-fA-F]+", value):
        raise ScriptrError("HEXDATA must contain an even number of hexadecimal digits.")
    return bytes.fromhex(value)

def uint64(value):
    try: value = int(value, 0)
    except Exception as e: raise ScriptrError(f"Invalid integer: {value}") from e
    if not 0 <= value <= (1 << 64) - 1: raise ScriptrError("Value must be unsigned 64-bit.")
    return value

def size_bytes(value):
    m = re.fullmatch(r"(\d+(?:\.\d+)?)(b|kb|mb|gb)?", value.lower())
    if not m: raise ScriptrError(f"Invalid size: {value}")
    return int(float(m.group(1)) * {None: 1, "b": 1, "kb": 1024, "mb": 1024**2, "gb": 1024**3}[m.group(2)])

def module_path(value):
    os.makedirs(MODULE_DIR, exist_ok=True)
    path = os.path.realpath(value if os.path.isabs(value) else os.path.join(MODULE_DIR, value.strip("{}")))
    root = os.path.realpath(MODULE_DIR)
    if not path.lower().endswith(".json") or os.path.commonpath([root, path]) != root:
        raise ScriptrError("usemodule accepts only JSON under ProgramData/MarekFS/scriptr_modules.")
    return path

COMMAND_REFERENCE = """MarekFS Scriptr v2 — command reference
=====================================
CONTROL
  print {text} | echo {text}          print text ({var} and $(var) expand)
  set {name} {value} | let            store a variable
  printvars                            list stored variables
  sleep({ms}) | wait({ms})             pause (capped at 10000 ms)
  help [topic]                        show this reference / one topic
  version                              show Scriptr version

FILES  (create / write / delete)
  createnewfile({name})                       create an empty file
  createnewfile({name})({content})            create a file with content
  createnewfile({content})                    single arg with no '.', '/' ->
                                               treated as content, auto-named
  mkdir({folder}) | createdir({folder})       create a folder
  writetofile({content})({targets})           overwrite matching files
  appendtofile({content})({targets})          append to matching files
  prependtofile({content})({targets})         prepend to matching files
  deletefile({targets}) | removefile({targets})  delete matching files
  deletedir({folder}) | rmdir({folder})       delete a folder
  renamefile({old})({new}) | movefile         rename / move a file
  copyfile({src})({dst}) | duplicatefile      copy a file
  clonefile({src})({count})                   make N numbered copies
  touchfile({name})                           create if missing
  truncatefile({name})({size})                resize (cuts or zero-pads)
  fillfile({name})({size})                    fill with 'A' pattern
  fillfile({name})({size})({HEX})             fill with a hex pattern
  wipefile({name})                            zero the payload (keeps entry)
  makehexfile({name})({HEXDATA})              create a binary file from hex
  importfile({host path})({name})             copy a host file into the disk
  exportfile({name})({host path})             write a disk file to the host

FILES  (attributes / security)
  setattributes({targets})({attrs})           set hidden,readonly,system,
                                               archive,compressed,directory
  clearattributes({targets})({attrs})         clear those attributes
  encryptfile({name})({password})             encrypt a file payload
  decryptfile({name})({password})             decrypt a file payload

FILES  (MAREKARCHV containers)
  makearchive({archive})({members})           pack members into an archive
  addtoarchive({archive})({members})          add members to an archive
  extractarchive({archive})                   unpack archive members as files

QUERY / INFO
  QueryFileOnDiskAmount({preds})              count matching files
  listfiles({preds}) | countfiles({preds})    list / count matching files
  fileexists({name})                          print True / False
  filesize({name}) | fileid({name})           print size / 64-bit FileID
  sectorof({name})                            print starting sector
  readfile({name}) | cat({name})              print text content
  hexdump({name})                             print hex content
  checksum({name})({algo})                    blake2b | sha256 | sha1 | md5
  verifyfile({name})                          compare payload to stored hash
  findfirstbytes({HEX})                       files starting with HEX
  findcontent({text})                         files containing text
  diskstats | driveinfo                       drive / file summary
  freespace                                   estimated free bytes

SECTOR (raw, condition required)
  ifsectorhexdatamatches({HEX}) then          match sectors starting with HEX
  if file <expr> then <action>                match files (see below)
  zerofill | wipe                             zero matched sectors
  swapdatato({HEX})                           overwrite matched sectors
  fillwithpattern({HEX})                      repeat pattern over matched
  randomsector(from1-50)                      write random data to 1 sector

TARGETS & PREDICATES
  targets: exact name | ext:.txt | *.txt | * (all files) | a.txt, b.md
  file expr: name=value, ext:.mp3, size:>1mb, size:>=2048, attr:hidden,
             id:123, sector:45, isdir:true, first:89504e47
  condition actions: deletefile, writetofile({c}), appendtofile({c}),
             setattributes({attrs}), clearattributes({attrs}),
             zerofill, wipe, swapdatato({HEX}), fillwithpattern({HEX}),
             randomsector(from1-50)

MODULES
  usemodule({name.json})                      splice lines from a JSON module
"""

SNIPPETS = [
    "# create a file with content",
    "createnewfile({hello.txt})({Hello, MarekFS!})",
    "# write to files by extension",
    "writetofile({updated})({ext:.txt})",
    "# variables + readback",
    "set name world\ncreatenewfile({greet.txt})({hi $(name)})\nreadfile({greet.txt})",
    "# archive it",
    "makearchive({bundle.MAREKARCHV})({greet.txt, hello.txt})",
    "# conditional attribute set",
    "if file ext:.tmp then\nsetattributes({hidden})",
    "# zero every MZ (PE) sector",
    "ifsectorhexdatamatches({4D5A}) then\nzerofill",
    "# count + checksum",
    "countfiles(ext:.txt)\nchecksum({hello.txt})({sha256})",
]

class ScriptrEngine:
    def __init__(self, drive_path, records=None, reader=None, writer=None):
        # Keep the caller's list object (even when empty) so in-place
        # mutations by the writer are visible to the engine.
        self.drive_path = drive_path
        self.records = records if records is not None else []
        self.reader, self.writer = reader, writer
        self.output = []
        self.vars = {}          # set/let variables
        self.virtual = {}       # dry-run preview layer: name -> record
        self.deleted = set()    # names marked deleted during preview
        self._auto_index = 0
        self._backup_path = None

    def log(self, value): self.output.append(str(value))

    # ------------------------------------------------------------------ parse
    def parse(self, source, stack=None):
        if len(source.encode()) > MAX_SCRIPT_BYTES: raise ScriptrError("Script is too large.")
        stack = stack or []; result = []
        for line in source.splitlines():
            line = line.strip()
            if not line or line.startswith("#"): continue
            if line.lower().startswith("usemodule"):
                path = module_path(line[len("usemodule"):].strip().strip('"'))
                if path in stack: raise ScriptrError("Circular usemodule detected.")
                try:
                    with open(path, encoding="utf-8") as f: data = json.load(f)
                except Exception as e: raise ScriptrError(f"Cannot load module: {e}")
                lines = data.get("script", data.get("lines", [])) if isinstance(data, dict) else data
                if not isinstance(lines, list) or not all(isinstance(x, str) for x in lines): raise ScriptrError("Module must contain string lines.")
                result.extend(self.parse("\n".join(lines), stack + [path])); continue
            result.append(line)
        if len(result) > MAX_ACTIONS: raise ScriptrError("Too many script actions.")
        return result

    def match(self, record, expr):
        m = re.match(r"^(size)(>=|<=|>|<|=)(.+)$", expr, re.I)
        if m: field, op, value = m.group(1).lower(), m.group(2), m.group(3)
        elif "=" in expr: field, value, op = expr.split("=", 1)[0].lower(), expr.split("=", 1)[1], "="
        elif ":" in expr: field, value, op = expr.split(":", 1)[0].lower(), expr.split(":", 1)[1], ":"
        else: return expr.lower() in str(record.get("filename", "")).lower()
        value = value.strip().strip('"'); name = str(record.get("filename", "")); low = name.lower()
        if field in ("name", "filename", "path"): return value.lower() in low if op == ":" else low == value.lower()
        if field in ("id", "fileid"): return int(record.get("file_id", record.get("sector", 0))) == uint64(value)
        if field in ("sector", "startingsector", "startsector"): return int(record.get("sector", 0)) == uint64(value)
        if field in ("ext", "extension"): return low.endswith(value.lower() if value.startswith(".") else "." + value.lower())
        if field == "size":
            current, target = int(record.get("size", 0)), size_bytes(value)
            return {":": current == target, "=": current == target, ">": current > target, ">=": current >= target, "<": current < target, "<=": current <= target}.get(op, False)
        if field in ("attr", "attribute", "attributes"):
            text = str(record.get("attributes_text", record.get("attributes", ""))).lower()
            return value.lower() in text
        if field in ("first", "firstbytes", "bytes"):
            try: return bool(self.reader) and self.reader(record).startswith(hex_bytes(value))
            except Exception: return False
        if field in ("isdir", "directory"): return bool(record.get("is_dir")) == value.lower() in ("1", "true", "yes")
        return False

    # ------------------------------------------------------------ record view
    def _all_records(self):
        return [r for r in self.records if r["filename"] not in self.deleted] + list(self.virtual.values())

    def _find(self, name):
        for r in self.records:
            if r["filename"] == name and name not in self.deleted: return r
        return self.virtual.get(name)

    def query(self, predicates): return [r for r in self._all_records() if all(self.match(r, p) for p in predicates)]

    # -------------------------------------------------------------- helpers
    @staticmethod
    def _brace(line): return re.findall(r"\{([^{}]*)\}", line)

    @staticmethod
    def _paren(line): return re.findall(r"\(([^()]*)\)", line)

    def _paren_text(self, line):
        p = self._paren(line)
        return p[0].strip() if p else ""

    def _args(self, line):
        """Argument groups for a command. Braces are the canonical syntax
        (createnewfile({name})({content})); plain parentheses are accepted
        as a fallback and may be mixed with braces (clonefile({src})(2)).
        Paren groups that merely re-wrap brace args are skipped."""
        b = self._brace(line)
        if not b:
            return self._paren(line)
        p = [g for g in self._paren(line) if "{" not in g and "}" not in g]
        return b + p

    def _targets_from(self, groups, line):
        """Target tokens for commands whose first group is content/name:
        extra brace groups, else the paren groups (writetofile({c})(a, b))."""
        targets = []
        for g in groups[1:]:
            targets.extend(self._split_targets(g))
        if not targets:
            for g in self._paren(line):
                targets.extend(self._split_targets(g))
        return targets

    def _expand(self, text):
        """Substitute $(var) and {var} (exact name) references."""
        if not text: return text
        def sub_var(m):
            return str(self.vars.get(m.group(1), m.group(0)))
        text = re.sub(r"\$\(([A-Za-z_][A-Za-z0-9_]*)\)", sub_var, text)
        return re.sub(r"\{([A-Za-z_][A-Za-z0-9_]*)\}", sub_var, text)

    def _split_targets(self, text):
        parts = [p.strip() for p in text.split(",") if p.strip()]
        if not parts: raise ScriptrError("Target list is empty.")
        return parts

    ATTR_ALIASES = {
        "hidden": FILE_ATTR_HIDDEN, "h": FILE_ATTR_HIDDEN,
        "readonly": FILE_ATTR_READONLY, "ro": FILE_ATTR_READONLY,
        "system": FILE_ATTR_SYSTEM, "sys": FILE_ATTR_SYSTEM,
        "archive": FILE_ATTR_ARCHIVE, "a": FILE_ATTR_ARCHIVE,
        "compressed": FILE_ATTR_COMPRESSED, "zip": FILE_ATTR_COMPRESSED,
        "encrypted": FILE_ATTR_ENCRYPTED, "enc": FILE_ATTR_ENCRYPTED,
        "directory": FILE_ATTR_DIRECTORY, "dir": FILE_ATTR_DIRECTORY,
    }

    def _attr_mask(self, text):
        mask = 0
        for part in re.split(r"[,\s]+", text.strip().lower()):
            if not part: continue
            if part not in self.ATTR_ALIASES: raise ScriptrError(f"Unknown attribute: {part}")
            mask |= self.ATTR_ALIASES[part]
        return mask

    @staticmethod
    def _require_pending(pending, name):
        if not pending: raise ScriptrError(f"{name} requires a preceding condition.")

    @staticmethod
    def _require_file_pending(pending, name):
        ScriptrEngine._require_pending(pending, name)
        if any(k == "sector" for k, _ in pending):
            raise ScriptrError(f"{name} needs a file condition (ifsectorhexdatamatches targets sectors only).")

    @staticmethod
    def _file_preds(pending):
        preds = []
        for k, v in pending:
            if k == "file": preds.extend(v)
        return preds

    @staticmethod
    def _looks_like_filename(text):
        t = text.strip()
        return bool(re.search(r"[\\/]", t) or re.search(r"\.\w{1,12}$", t))

    def _auto_name(self):
        while True:
            self._auto_index += 1
            name = f"scriptr_file_{self._auto_index}.txt"
            if self._find(name) is None: return name

    def _target_records(self, spec):
        """Resolve target tokens (names / ext: / globs / '*') to records."""
        matched, seen = [], set()
        for token in spec:
            token = self._expand(token).strip()
            if not token: continue
            pool = self._all_records()
            if token in ("*", "all"):
                hits = [r for r in pool if not r.get("is_dir")]
            elif token.lower().startswith("ext:"):
                ext = token[4:].lower()
                if ext and not ext.startswith("."): ext = "." + ext
                hits = [r for r in pool if not r.get("is_dir") and str(r["filename"]).lower().endswith(ext)]
            elif any(ch in token for ch in "*?["):
                hits = [r for r in pool if not r.get("is_dir") and fnmatch.fnmatch(str(r["filename"]).lower(), token.lower())]
            else:
                exact = self._find(token)
                hits = [exact] if exact else [r for r in pool if not r.get("is_dir") and token.lower() in str(r["filename"]).lower()]
            for r in hits:
                if r and r["filename"] not in seen:
                    seen.add(r["filename"]); matched.append(r)
        return matched

    def _resolve_action_targets(self, action):
        """Conditional ops ('records' key) may match zero files; explicit
        target lists must match at least one."""
        if "records" in action:
            return self.query(action["records"])
        targets = action.get("targets") or []
        records = self._target_records(targets)
        if not records: raise ScriptrError(f"No files matched: {', '.join(targets)}")
        return records

    def _read_record(self, rec, password=""):
        if "_data" in rec: return rec["_data"]
        try:
            if password and self.writer is not None and hasattr(self.writer, "read"):
                return self.writer.read(rec, password)
            if self.reader is not None: return self.reader(rec)
        except Exception as e:
            raise ScriptrError(f"Cannot read '{rec.get('filename')}': {e}")
        raise ScriptrError(f"No reader available for '{rec.get('filename')}'.")

    def _stored_checksum(self, name):
        try:
            if self.writer is not None and hasattr(self.writer, "metadata_get"):
                md = self.writer.metadata_get(name)
                return (md or {}).get("checksum") if isinstance(md, dict) else None
        except Exception:
            pass
        return None

    # ------------------------------------------------------------------- plan
    def plan(self, source):
        lines, actions, pending = self.parse(source), [], []
        for line in lines:
            low = line.lower()

            # --- conditions -------------------------------------------------
            if low.startswith("ifsectorhexdatamatches"):
                m = re.search(r"\{([^}]*)\}", line)
                if not m: raise ScriptrError("Use ifsectorhexdatamatches({HEXDATA}) then")
                pending.append(("sector", hex_bytes(m.group(1)))); continue
            if low.startswith("if file") or low.startswith("iffile"):
                expr = re.split(r"then", line, flags=re.I, maxsplit=1)[0]
                expr = re.sub(r"^if\s*file", "", expr, flags=re.I).strip()
                pending.append(("file", shlex.split(expr.replace(" and ", " ")))); continue

            # --- legacy sector writes (need a condition) --------------------
            if low in ("zerofill", "zero-fill"):
                self._require_pending(pending, "zerofill")
                actions.append({"kind": "write", "op": "zerofill", "conditions": pending}); pending = []; continue
            if low.startswith("swapdatato"):
                m = re.search(r"\{([^}]*)\}", line)
                if not m: raise ScriptrError("Use swapdatato({HEXDATA}).")
                self._require_pending(pending, "swapdatato")
                actions.append({"kind": "write", "op": "swapdatato", "data": hex_bytes(m.group(1)), "conditions": pending}); pending = []; continue
            if low.startswith("randomsector"):
                m = re.search(r"\((?:from\s*)?(\d+)\s*(?:-|,)\s*(\d+)\)", line, re.I)
                if not m:
                    m = re.search(r"\((\d+)\)", line)
                    if not m: raise ScriptrError("randomsector requires randomsector(from1-50) or randomsector(12).")
                    start = end = uint64(m.group(1))
                else: start, end = uint64(m.group(1)), uint64(m.group(2))
                if end < start or end - start + 1 > MAX_WRITE_SECTORS: raise ScriptrError("randomsector range is too large.")
                self._require_pending(pending, "randomsector")
                actions.append({"kind": "write", "op": "randomsector", "start": start, "end": end, "conditions": pending}); pending = []; continue

            # --- new sector writes (need a condition) -----------------------
            if low == "wipe":
                self._require_pending(pending, "wipe")
                actions.append({"kind": "write", "op": "zerofill", "conditions": pending}); pending = []; continue
            if low.startswith("fillwithpattern"):
                groups = self._brace(line)
                if not groups: raise ScriptrError("Use fillwithpattern({HEXDATA}).")
                self._require_pending(pending, "fillwithpattern")
                actions.append({"kind": "write", "op": "fillwithpattern", "data": hex_bytes(groups[0]), "conditions": pending}); pending = []; continue

            # --- conditional-capable file actions ---------------------------
            if low.startswith("deletefile") or low.startswith("removefile"):
                groups = self._args(line)
                if pending:
                    if groups: raise ScriptrError("deletefile cannot combine a condition with explicit targets.")
                    self._require_file_pending(pending, "deletefile")
                    actions.append({"kind": "fileop", "op": "delete", "records": self._file_preds(pending), "targets": []}); pending = []; continue
                if not groups: raise ScriptrError("Use deletefile({filename / ext:ext / *})")
                actions.append({"kind": "fileop", "op": "delete", "targets": self._split_targets(groups[0])}); continue
            if low.startswith("writetofile"):
                groups = self._args(line)
                if not groups: raise ScriptrError("Use writetofile({content})({filename / ext: / *})")
                content = groups[0]
                targets = self._targets_from(groups, line)
                if pending:
                    if targets: raise ScriptrError("writetofile cannot combine a condition with explicit targets.")
                    self._require_file_pending(pending, "writetofile")
                    actions.append({"kind": "fileop", "op": "write", "content": content, "records": self._file_preds(pending)}); pending = []; continue
                if not targets: raise ScriptrError("writetofile needs targets or a preceding file condition.")
                actions.append({"kind": "fileop", "op": "write", "content": content, "targets": targets}); continue
            if low.startswith("appendtofile"):
                groups = self._args(line)
                if not groups: raise ScriptrError("Use appendtofile({content})({targets})")
                targets = self._targets_from(groups, line)
                if pending:
                    if targets: raise ScriptrError("appendtofile cannot combine a condition with explicit targets.")
                    self._require_file_pending(pending, "appendtofile")
                    actions.append({"kind": "fileop", "op": "append", "content": groups[0], "records": self._file_preds(pending)}); pending = []; continue
                if not targets: raise ScriptrError("appendtofile needs targets or a preceding file condition.")
                actions.append({"kind": "fileop", "op": "append", "content": groups[0], "targets": targets}); continue
            if low.startswith("prependtofile"):
                groups = self._args(line)
                if not groups: raise ScriptrError("Use prependtofile({content})({targets})")
                targets = self._targets_from(groups, line)
                if pending:
                    if targets: raise ScriptrError("prependtofile cannot combine a condition with explicit targets.")
                    self._require_file_pending(pending, "prependtofile")
                    actions.append({"kind": "fileop", "op": "prepend", "content": groups[0], "records": self._file_preds(pending)}); pending = []; continue
                if not targets: raise ScriptrError("prependtofile needs targets or a preceding file condition.")
                actions.append({"kind": "fileop", "op": "prepend", "content": groups[0], "targets": targets}); continue
            if low.startswith("setattributes") or low.startswith("clearattributes"):
                groups = self._args(line)
                set_flag = low.startswith("setattributes")
                verb = "set" if set_flag else "clear"
                if pending:
                    if len(groups) != 1: raise ScriptrError(f"{verb}attributes with a condition takes only ({{attrs}}).")
                    self._require_file_pending(pending, "attributes")
                    actions.append({"kind": "fileop", "op": "setattr", "set": set_flag, "mask": self._attr_mask(groups[0]), "records": self._file_preds(pending)}); pending = []; continue
                if len(groups) != 2: raise ScriptrError(f"Use {verb}attributes({{targets}})({{attrs}})")
                actions.append({"kind": "fileop", "op": "setattr", "set": set_flag, "mask": self._attr_mask(groups[1]), "targets": self._split_targets(groups[0])}); continue

            # --- standalone file ops ----------------------------------------
            if low.startswith(("createnewfile", "createfile", "newfile")):
                groups = self._args(line)
                if not groups: raise ScriptrError("Use createnewfile({filename}) or createnewfile({filename})({content})")
                name, content = groups[0].strip(), None
                if len(groups) > 1:
                    content = groups[1]
                elif self._looks_like_filename(name):
                    content = ""
                else:  # single arg without a filename shape -> content, auto-named
                    name, content = self._auto_name(), groups[0]
                actions.append({"kind": "fileop", "op": "create", "name": name, "data": content, "is_dir": False}); continue
            if low.startswith("mkdir") or low.startswith("createdir"):
                groups = self._args(line)
                if not groups: raise ScriptrError("Use mkdir({foldername})")
                actions.append({"kind": "fileop", "op": "create", "name": groups[0].strip(), "data": "", "is_dir": True}); continue
            if low.startswith("deletedir") or low.startswith("rmdir"):
                groups = self._args(line)
                if not groups: raise ScriptrError("Use deletedir({foldername})")
                actions.append({"kind": "fileop", "op": "delete", "targets": [groups[0].strip()]}); continue
            if low.startswith("renamefile") or low.startswith("movefile"):
                groups = self._args(line)
                if len(groups) != 2: raise ScriptrError("Use renamefile({oldname})({newname})")
                actions.append({"kind": "fileop", "op": "rename", "old": groups[0].strip(), "new": groups[1].strip()}); continue
            if low.startswith("copyfile") or low.startswith("duplicatefile"):
                groups = self._args(line)
                if len(groups) != 2: raise ScriptrError("Use copyfile({src})({dst})")
                actions.append({"kind": "fileop", "op": "copy", "src": groups[0].strip(), "dst": groups[1].strip()}); continue
            if low.startswith("clonefile"):
                groups = self._args(line)
                if len(groups) != 2: raise ScriptrError("Use clonefile({src})({count})")
                actions.append({"kind": "fileop", "op": "clone", "src": groups[0].strip(), "count": uint64(groups[1].strip())}); continue
            if low.startswith("touchfile"):
                groups = self._args(line)
                if not groups: raise ScriptrError("Use touchfile({filename})")
                actions.append({"kind": "fileop", "op": "touch", "name": groups[0].strip()}); continue
            if low.startswith("truncatefile"):
                groups = self._args(line)
                if len(groups) != 2: raise ScriptrError("Use truncatefile({filename})({size})")
                actions.append({"kind": "fileop", "op": "truncate", "name": groups[0].strip(), "size": size_bytes(groups[1].strip())}); continue
            if low.startswith("fillfile"):
                groups = self._args(line)
                if len(groups) < 2: raise ScriptrError("Use fillfile({filename})({size}) or fillfile({filename})({size})({HEX})")
                actions.append({"kind": "fileop", "op": "fill", "name": groups[0].strip(), "size": size_bytes(groups[1].strip()),
                                "pattern": hex_bytes(groups[2]) if len(groups) > 2 else None}); continue
            if low.startswith("wipefile"):
                groups = self._args(line)
                if not groups: raise ScriptrError("Use wipefile({filename})")
                actions.append({"kind": "fileop", "op": "wipe", "targets": [groups[0].strip()]}); continue
            if low.startswith("encryptfile"):
                groups = self._args(line)
                if len(groups) != 2: raise ScriptrError("Use encryptfile({filename})({password})")
                actions.append({"kind": "fileop", "op": "encrypt", "targets": [groups[0].strip()], "password": groups[1]}); continue
            if low.startswith("decryptfile"):
                groups = self._args(line)
                if len(groups) != 2: raise ScriptrError("Use decryptfile({filename})({password})")
                actions.append({"kind": "fileop", "op": "decrypt", "targets": [groups[0].strip()], "password": groups[1]}); continue
            if low.startswith("makearchive"):
                groups = self._args(line)
                if len(groups) < 2: raise ScriptrError("Use makearchive({archive name})({member, member, ...})")
                actions.append({"kind": "fileop", "op": "makearchive", "name": groups[0].strip(), "targets": self._split_targets(groups[1])}); continue
            if low.startswith("addtoarchive"):
                groups = self._args(line)
                if len(groups) != 2: raise ScriptrError("Use addtoarchive({archive})({member, member, ...})")
                actions.append({"kind": "fileop", "op": "addtoarchive", "name": groups[0].strip(), "targets": self._split_targets(groups[1])}); continue
            if low.startswith("extractarchive"):
                groups = self._args(line)
                if not groups: raise ScriptrError("Use extractarchive({archive name})")
                actions.append({"kind": "fileop", "op": "extractarchive", "name": groups[0].strip()}); continue
            if low.startswith("exportfile"):
                groups = self._args(line)
                if len(groups) != 2: raise ScriptrError("Use exportfile({filename})({host path})")
                actions.append({"kind": "fileop", "op": "export", "name": groups[0].strip(), "path": groups[1].strip()}); continue
            if low.startswith("importfile"):
                groups = self._args(line)
                if len(groups) != 2: raise ScriptrError("Use importfile({host path})({filename})")
                actions.append({"kind": "fileop", "op": "import", "path": groups[0].strip(), "name": groups[1].strip()}); continue
            if low.startswith("makehexfile"):
                groups = self._args(line)
                if len(groups) != 2: raise ScriptrError("Use makehexfile({filename})({HEXDATA})")
                actions.append({"kind": "fileop", "op": "create", "name": groups[0].strip(), "data": hex_bytes(groups[1]), "is_dir": False}); continue

            # --- queries ------------------------------------------------------
            if low.startswith("queryfileondiskamount"):
                inside = self._paren_text(line)
                actions.append({"kind": "query", "predicates": shlex.split(inside)}); continue
            if low.startswith("listfiles"):
                inside = self._paren_text(line)
                actions.append({"kind": "info", "op": "list", "predicates": shlex.split(inside) if inside else []}); continue
            if low.startswith("countfiles"):
                inside = self._paren_text(line)
                actions.append({"kind": "info", "op": "count", "predicates": shlex.split(inside) if inside else []}); continue
            if low.startswith("fileexists"):
                groups = self._args(line)
                if not groups: raise ScriptrError("Use fileexists({filename})")
                actions.append({"kind": "info", "op": "exists", "name": groups[0].strip()}); continue
            if low.startswith("filesize"):
                groups = self._args(line)
                if not groups: raise ScriptrError("Use filesize({filename})")
                actions.append({"kind": "info", "op": "size", "name": groups[0].strip()}); continue
            if low.startswith("fileid"):
                groups = self._args(line)
                if not groups: raise ScriptrError("Use fileid({filename})")
                actions.append({"kind": "info", "op": "fileid", "name": groups[0].strip()}); continue
            if low.startswith("sectorof"):
                groups = self._args(line)
                if not groups: raise ScriptrError("Use sectorof({filename})")
                actions.append({"kind": "info", "op": "sector", "name": groups[0].strip()}); continue
            if low.startswith("readfile") or low.startswith("cat"):
                groups = self._args(line)
                if not groups: raise ScriptrError("Use readfile({filename})")
                actions.append({"kind": "info", "op": "read", "name": groups[0].strip()}); continue
            if low.startswith("hexdump"):
                groups = self._args(line)
                if not groups: raise ScriptrError("Use hexdump({filename})")
                actions.append({"kind": "info", "op": "hexdump", "name": groups[0].strip()}); continue
            if low.startswith("checksum"):
                groups = self._args(line)
                if not groups: raise ScriptrError("Use checksum({filename}) or checksum({filename})({blake2b|sha256|md5|sha1})")
                actions.append({"kind": "info", "op": "checksum", "name": groups[0].strip(),
                                "algo": groups[1].strip().lower() if len(groups) > 1 else "blake2b"}); continue
            if low.startswith("verifyfile"):
                groups = self._args(line)
                if not groups: raise ScriptrError("Use verifyfile({filename})")
                actions.append({"kind": "info", "op": "verify", "name": groups[0].strip()}); continue
            if low.startswith("findfirstbytes"):
                groups = self._args(line)
                if not groups: raise ScriptrError("Use findfirstbytes({HEXDATA})")
                actions.append({"kind": "info", "op": "findfirst", "needle": hex_bytes(groups[0])}); continue
            if low.startswith("findcontent"):
                groups = self._args(line)
                if not groups: raise ScriptrError("Use findcontent({text})")
                actions.append({"kind": "info", "op": "findcontent", "needle": groups[0]}); continue
            if low.startswith("diskstats") or low.startswith("driveinfo"):
                actions.append({"kind": "info", "op": "diskstats"}); continue
            if low.startswith("freespace"):
                actions.append({"kind": "info", "op": "freespace"}); continue

            # --- control --------------------------------------------------------
            if low == "printvars" or low == "vars":
                actions.append({"kind": "doc", "op": "printvars"}); continue
            if low.startswith("print ") or low.startswith("echo "):
                actions.append({"kind": "output", "text": line.split(" ", 1)[1]}); continue
            if low.startswith("set ") or low.startswith("let "):
                rest = line.split(" ", 2)
                if len(rest) < 3 or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", rest[1]):
                    raise ScriptrError("Use set {varname} {value}")
                actions.append({"kind": "set", "name": rest[1], "value": rest[2]}); continue
            if low.startswith("sleep") or low.startswith("wait"):
                m = re.search(r"(\d+)", line)
                if not m: raise ScriptrError("Use sleep({milliseconds})")
                actions.append({"kind": "sleep", "ms": min(int(m.group(1)), MAX_SLEEP_MS)}); continue
            if low == "help" or low.startswith("help "):
                topic = line[5:].strip().lower() if low.startswith("help ") else ""
                actions.append({"kind": "doc", "op": "help", "topic": topic}); continue
            if low == "version":
                actions.append({"kind": "doc", "op": "version"}); continue
            raise ScriptrError(f"Unknown Scriptr statement: {line}")
        if pending: raise ScriptrError("Condition has no following action.")
        return actions

    # --------------------------------------------------------------- execute
    def sector_hits(self, fd, needle):
        size = os.fstat(fd).st_size // SECTOR_SIZE; result = []
        for sector in range(max(0, size - 1)):
            if read_sectors(fd, sector, 1).startswith(needle): result.append(sector)
        return result

    def execute(self, source, dry_run=True, confirmed=False):
        plan, results = self.plan(source), []
        if not dry_run and not confirmed:
            raise PermissionError("Explicit confirmation required for writes.")
        self.output = []
        self.vars, self.virtual, self.deleted = {}, {}, set()
        self._auto_index, self._backup_path = 0, None
        if not dry_run and self.writer is not None and any(a["kind"] == "fileop" for a in plan):
            self._preflight_backup(plan, results)
        for action in plan:
            kind = action["kind"]
            if kind == "output":
                self.log(self._expand(action["text"])); results.append(action); continue
            if kind == "set":
                value = self._expand(action["value"])
                self.vars[action["name"]] = value
                self.log(f"set {action['name']} = {value}")
                results.append(action); continue
            if kind == "sleep":
                ms = action["ms"]
                self.log(f"sleep {ms} ms")
                results.append(action)
                if not dry_run: time.sleep(ms / 1000.0)
                continue
            if kind == "doc":
                self._doc(action, results); continue
            if kind == "query":
                matches = self.query(action["predicates"])
                item = {"kind": "query", "count": len(matches), "files": [r.get("filename") for r in matches]}
                results.append(item); self.log(f"QueryFileOnDiskAmount: {len(matches)}"); continue
            if kind == "info":
                self._info(action, results); continue
            if kind == "fileop":
                if dry_run: self._fileop_preview(action, results)
                else: self._fileop_apply(action, results)
                continue
            # sector write actions (legacy + new)
            conditions, targets = action["conditions"], []
            for ckind, value in conditions:
                if ckind == "sector":
                    fd = os.open(self.drive_path, os.O_RDONLY)
                    try: targets.extend(self.sector_hits(fd, value))
                    finally: os.close(fd)
                else: targets.extend(int(r.get("sector", 0)) for r in self.query(value))
            if action["op"] == "randomsector": targets = [random.randint(action["start"], action["end"])]
            targets = sorted(set(t for t in targets if t >= 0))[:MAX_WRITE_SECTORS]
            item = {"kind": "write", "op": action["op"], "sectors": targets, "count": len(targets), "dry_run": dry_run}
            results.append(item); self.log(("PREVIEW" if dry_run else "WRITE") + f" {action['op']} sectors={targets}")
            if not dry_run and targets:
                targets = [s for s in targets if s * SECTOR_SIZE < os.path.getsize(self.drive_path)]
                os.makedirs(BACKUP_DIR, exist_ok=True)
                backup_path = os.path.join(BACKUP_DIR, os.path.basename(self.drive_path) + f".{time.time_ns()}.json")
                backup = {"drive": os.path.abspath(self.drive_path), "sectors": {}}
                with open(self.drive_path, "rb") as src:
                    for s in targets: src.seek(s * SECTOR_SIZE); backup["sectors"][str(s)] = src.read(SECTOR_SIZE).hex()
                with open(backup_path, "w") as out: json.dump(backup, out); item["backup"] = backup_path
                fd = os.open(self.drive_path, os.O_RDWR)
                try:
                    for s in targets:
                        if action["op"] == "fillwithpattern":
                            pat = action.get("data", b"\x00")
                            data = (pat * (SECTOR_SIZE // len(pat) + 1))[:SECTOR_SIZE] if pat else b"\0" * SECTOR_SIZE
                        else:
                            data = b"\0" * SECTOR_SIZE if action["op"] == "zerofill" else action.get("data", os.urandom(SECTOR_SIZE)).ljust(SECTOR_SIZE, b"\0")[:SECTOR_SIZE]
                        write_with_journal(fd, s, data)
                finally: os.close(fd)
        if not dry_run and self.writer is not None and self.writer.ops:
            try: self.writer.commit()
            except Exception as e: raise ScriptrError(f"Failed to commit directory: {e}")
        return {"plan": plan, "results": results, "output": self.output, "dry_run": dry_run}

    def _doc(self, action, results):
        op = action["op"]
        if op == "version":
            text = f"MarekFS Scriptr v{SCRIPTR_VERSION} — restricted preview-first DSL (use 'help' for the command reference)"
            self.log(text); results.append({"kind": "doc", "op": "version", "text": text})
        elif op == "printvars":
            lines = [f"{k} = {v}" for k, v in sorted(self.vars.items())]
            text = "\n".join(lines) if lines else "(no variables set)"
            self.log(text); results.append({"kind": "doc", "op": "printvars", "vars": dict(self.vars)})
        else:
            topic = action.get("topic", "")
            text = self.reference(topic)
            self.log(text); results.append({"kind": "doc", "op": "help", "topic": topic})

    def reference(self, topic=""):
        if topic:
            lines = [ln for ln in COMMAND_REFERENCE.splitlines() if topic.lower() in ln.lower()]
            return "\n".join(lines) if lines else f"No help found for '{topic}'. Use 'help' for the full reference."
        return COMMAND_REFERENCE

    def _info(self, action, results):
        op = action["op"]
        if op in ("list", "count"):
            matches = self.query(action["predicates"])
            if op == "count":
                self.log(f"countfiles: {len(matches)}")
                results.append({"kind": "info", "op": "count", "count": len(matches)})
            else:
                for r in sorted(matches, key=lambda r: r["filename"]):
                    self.log(f"{r['filename']}  ({r.get('size', 0)} bytes)")
                self.log(f"listfiles: {len(matches)} file(s)")
                results.append({"kind": "info", "op": "list", "count": len(matches), "files": [r["filename"] for r in matches]})
            return
        if op == "exists":
            rec = self._find(self._expand(action["name"]))
            self.log(f"fileexists({action['name']}): {bool(rec)}")
            results.append({"kind": "info", "op": "exists", "name": action["name"], "exists": bool(rec)}); return
        if op in ("size", "fileid", "sector"):
            rec = self._find(self._expand(action["name"]))
            if rec is None: raise ScriptrError(f"File not found: {action['name']}")
            if op == "size":
                self.log(f"filesize({action['name']}): {rec.get('size', 0)} bytes")
                results.append({"kind": "info", "op": "size", "name": action["name"], "size": rec.get("size", 0)})
            elif op == "fileid":
                fid = rec.get("file_id")
                if fid is None or fid < 0:  # preview (virtual) records have no sector yet
                    fid = int(hashlib.sha256(rec["filename"].encode("utf-8")).hexdigest()[:16], 16)
                self.log(f"fileid({action['name']}): {fid}")
                results.append({"kind": "info", "op": "fileid", "name": action["name"], "fileid": fid})
            else:
                self.log(f"sectorof({action['name']}): {rec.get('sector', 0)}")
                results.append({"kind": "info", "op": "sector", "name": action["name"], "sector": rec.get("sector", 0)})
            return
        if op == "read":
            rec = self._find(self._expand(action["name"]))
            if rec is None: raise ScriptrError(f"File not found: {action['name']}")
            data = self._read_record(rec)
            text = data.decode("utf-8", errors="ignore")
            shown = text[:MAX_READ_DISPLAY]
            self.log(f"readfile({action['name']}) [{len(data)} bytes]:")
            self.log(shown + ("\n… (truncated)" if len(text) > MAX_READ_DISPLAY else ""))
            results.append({"kind": "info", "op": "read", "name": action["name"], "size": len(data)}); return
        if op == "hexdump":
            rec = self._find(self._expand(action["name"]))
            if rec is None: raise ScriptrError(f"File not found: {action['name']}")
            data = self._read_record(rec)[:MAX_HEX_DISPLAY]
            hexed = data.hex(" ")
            for i in range(0, len(hexed), 96):
                self.log(hexed[i:i + 96])
            self.log(f"hexdump({action['name']}): {len(data)} bytes shown of {rec.get('size', 0)}")
            results.append({"kind": "info", "op": "hexdump", "name": action["name"], "bytes": len(data)}); return
        if op == "checksum":
            rec = self._find(self._expand(action["name"]))
            if rec is None: raise ScriptrError(f"File not found: {action['name']}")
            data = self._read_record(rec); algo = action["algo"]
            if algo == "blake2b": digest = data_checksum(data)
            elif algo in ("sha256", "sha1", "md5"): digest = hashlib.new(algo, data).hexdigest()
            else: raise ScriptrError(f"Unknown checksum algorithm: {algo}")
            self.log(f"checksum({action['name']}) [{algo}]: {digest}")
            results.append({"kind": "info", "op": "checksum", "name": action["name"], "algo": algo, "digest": digest}); return
        if op == "verify":
            rec = self._find(self._expand(action["name"]))
            if rec is None: raise ScriptrError(f"File not found: {action['name']}")
            data = self._read_record(rec); actual = data_checksum(data)
            stored = self._stored_checksum(rec["filename"])
            if stored is None:
                self.log(f"verifyfile({action['name']}): no stored checksum on record")
                results.append({"kind": "info", "op": "verify", "name": action["name"], "status": "no-record"})
            elif stored == actual:
                self.log(f"verifyfile({action['name']}): OK ({actual[:12]}…)")
                results.append({"kind": "info", "op": "verify", "name": action["name"], "status": "ok"})
            else:
                self.log(f"verifyfile({action['name']}): MISMATCH stored {stored[:12]}… found {actual[:12]}…")
                results.append({"kind": "info", "op": "verify", "name": action["name"], "status": "mismatch"})
            return
        if op == "findfirst":
            needle = action["needle"]
            hits = [r["filename"] for r in self._all_records()
                    if not r.get("is_dir") and self._read_record(r)[:len(needle)] == needle]
            self.log(f"findfirstbytes: {len(hits)} file(s) start with {needle.hex()}: {', '.join(hits) or 'none'}")
            results.append({"kind": "info", "op": "findfirst", "files": hits}); return
        if op == "findcontent":
            needle = action["needle"].encode("utf-8", errors="ignore")
            hits = []
            for r in self._all_records():
                if r.get("is_dir") or r.get("size", 0) > MAX_FIND_BYTES: continue
                try:
                    if needle in self._read_record(r): hits.append(r["filename"])
                except ScriptrError:
                    continue
            self.log(f"findcontent({action['needle']}): {len(hits)} file(s): {', '.join(hits) or 'none'}")
            results.append({"kind": "info", "op": "findcontent", "files": hits}); return
        if op == "diskstats":
            files = [r for r in self._all_records() if not r.get("is_dir")]
            dirs = [r for r in self._all_records() if r.get("is_dir")]
            encrypted = [r for r in files if r.get("encrypted") or (r.get("attributes", 0) & FILE_ATTR_ENCRYPTED)]
            size = 0
            try: size = os.path.getsize(self.drive_path)
            except Exception: pass
            lines = [f"drive: {self.drive_path}", f"size: {size} bytes",
                     f"files: {len(files)}  folders: {len(dirs)}  encrypted: {len(encrypted)}",
                     f"next free sector: {self._next_free_sector()}"]
            for ln in lines: self.log(ln)
            results.append({"kind": "info", "op": "diskstats", "size": size, "files": len(files), "dirs": len(dirs)}); return
        if op == "freespace":
            used_end = 0
            for r in self._all_records():
                used_end = max(used_end, int(r.get("sector", 0)) + max(1, (int(r.get("size", 0)) + SECTOR_SIZE - 1) // SECTOR_SIZE))
            size = 0
            try: size = os.path.getsize(self.drive_path)
            except Exception: pass
            free = max(0, size - used_end * SECTOR_SIZE)
            self.log(f"freespace: {free} bytes free (used up to sector {used_end})")
            results.append({"kind": "info", "op": "freespace", "free": free}); return
        raise ScriptrError(f"Unknown info op: {op}")

    def _next_free_sector(self):
        if self.writer is not None and hasattr(self.writer, "next_free_sector"):
            return self.writer.next_free_sector()
        best = 0
        for r in self._all_records():
            best = max(best, int(r.get("sector", 0)) + max(1, (int(r.get("size", 0)) + SECTOR_SIZE - 1) // SECTOR_SIZE))
        return best + 1

    # ------------------------------------------------------ file ops: preview
    def _fileop_preview(self, action, results):
        """Simulate file operations on the virtual layer so a dry run shows
        exactly what a confirmed run would do (create -> write -> read works)."""
        for key in ("name", "old", "new", "src", "dst", "path", "content", "data", "password"):
            if isinstance(action.get(key), str):
                action[key] = self._expand(action[key])
        op = action["op"]
        if op == "create":
            name, data = action["name"], action.get("data", "")
            if isinstance(data, str): data = data.encode("utf-8")
            if self._find(name) is not None: raise ScriptrError(f"'{name}' already exists.")
            self.virtual[name] = {"filename": name, "is_dir": bool(action.get("is_dir")), "sector": -1,
                                  "size": len(data), "attributes": int(action.get("attributes", 0)),
                                  "encrypted": False, "_data": data}
            self.log(f"PREVIEW create {name} ({len(data)} bytes)")
            results.append({"kind": "fileop", "op": "create", "name": name, "size": len(data), "dry_run": True}); return
        if op == "delete":
            records = self._resolve_action_targets(action)
            for rec in records:
                if rec["filename"] in self.virtual: del self.virtual[rec["filename"]]
                else: self.deleted.add(rec["filename"])
                self.log(f"PREVIEW delete {rec['filename']}")
            results.append({"kind": "fileop", "op": "delete", "count": len(records),
                            "files": [r["filename"] for r in records], "dry_run": True}); return
        if op in ("write", "append", "prepend"):
            records = self._resolve_action_targets(action)
            content = action["content"].encode("utf-8")
            for rec in records:
                if "_data" in rec:
                    if op == "append": rec["_data"] = rec["_data"] + content
                    elif op == "prepend": rec["_data"] = content + rec["_data"]
                    else: rec["_data"] = content
                    rec["size"] = len(rec["_data"])
            names = ", ".join(r["filename"] for r in records[:5]) + ("…" if len(records) > 5 else "")
            self.log(f"PREVIEW {op} on {len(records)} file(s): {names}")
            results.append({"kind": "fileop", "op": op, "count": len(records), "dry_run": True}); return
        if op == "setattr":
            records = self._resolve_action_targets(action)
            mask, set_flag = action["mask"], action["set"]
            if set_flag and (mask & FILE_ATTR_ENCRYPTED):
                raise ScriptrError("Use encryptfile({name})({password}) to set the encrypted attribute.")
            for rec in records:
                if "_data" in rec:
                    rec["attributes"] = (rec["attributes"] | mask) if set_flag else (rec["attributes"] & ~mask)
            self.log(f"PREVIEW {'set' if set_flag else 'clear'}attributes on {len(records)} file(s)")
            results.append({"kind": "fileop", "op": "setattr", "count": len(records), "dry_run": True}); return
        if op == "rename":
            old, new = self._expand(action["old"]), self._expand(action["new"])
            rec = self._find(old)
            if rec is None: raise ScriptrError(f"File not found: {old}")
            if self._find(new) is not None: raise ScriptrError(f"'{new}' already exists.")
            if "_data" in rec:
                rec["filename"] = new
                self.virtual[new] = self.virtual.pop(old)
            else:
                self.deleted.add(old)
                virt = dict(rec); virt["filename"] = new
                virt["_data"] = self._read_record(rec); virt["sector"] = -1
                self.virtual[new] = virt
            self.log(f"PREVIEW rename {old} -> {new}")
            results.append({"kind": "fileop", "op": "rename", "old": old, "new": new, "dry_run": True}); return
        if op == "copy":
            src, dst = self._expand(action["src"]), self._expand(action["dst"])
            rec = self._find(src)
            if rec is None: raise ScriptrError(f"File not found: {src}")
            if self._find(dst) is not None: raise ScriptrError(f"'{dst}' already exists.")
            data = self._read_record(rec)
            self.virtual[dst] = {"filename": dst, "is_dir": False, "sector": -1, "size": len(data),
                                 "attributes": rec.get("attributes", 0) & ~FILE_ATTR_ENCRYPTED,
                                 "encrypted": False, "_data": data}
            self.log(f"PREVIEW copy {src} -> {dst} ({len(data)} bytes)")
            results.append({"kind": "fileop", "op": "copy", "src": src, "dst": dst, "dry_run": True}); return
        if op == "clone":
            src = self._expand(action["src"]); count = action["count"]
            if count < 1 or count > 256: raise ScriptrError("clone count must be 1..256")
            rec = self._find(src)
            if rec is None: raise ScriptrError(f"File not found: {src}")
            data = self._read_record(rec)
            stem, ext = os.path.splitext(src); names = []
            for i in range(1, count + 1):
                name = f"{stem} ({i}){ext}"
                if self._find(name) is not None: raise ScriptrError(f"'{name}' already exists.")
                self.virtual[name] = {"filename": name, "is_dir": False, "sector": -1, "size": len(data),
                                      "attributes": rec.get("attributes", 0) & ~FILE_ATTR_ENCRYPTED,
                                      "encrypted": False, "_data": data}
                names.append(name)
            self.log(f"PREVIEW clone {src} -> {', '.join(names)}")
            results.append({"kind": "fileop", "op": "clone", "src": src, "count": count, "dry_run": True}); return
        if op == "touch":
            name = action["name"]
            if self._find(name) is None:
                self.virtual[name] = {"filename": name, "is_dir": False, "sector": -1, "size": 0,
                                      "attributes": 0, "encrypted": False, "_data": b""}
                self.log(f"PREVIEW touch {name} (created)")
            else:
                self.log(f"PREVIEW touch {name} (already exists)")
            results.append({"kind": "fileop", "op": "touch", "name": name, "dry_run": True}); return
        if op == "truncate":
            name = action["name"]; rec = self._find(name)
            if rec is None: raise ScriptrError(f"File not found: {name}")
            data = self._read_record(rec); size = action["size"]
            new = data[:size] if len(data) >= size else data + b"\0" * (size - len(data))
            if "_data" in rec: rec["_data"] = new; rec["size"] = len(new)
            self.log(f"PREVIEW truncate {name} to {size} bytes (from {rec.get('size', 0)})")
            results.append({"kind": "fileop", "op": "truncate", "name": name, "size": size, "dry_run": True}); return
        if op == "fill":
            name = action["name"]; rec = self._find(name)
            if rec is None: raise ScriptrError(f"File not found: {name}")
            pattern = action["pattern"] or b"A"; size = action["size"]
            new = (pattern * (size // len(pattern) + 1))[:size]
            if "_data" in rec: rec["_data"] = new; rec["size"] = len(new)
            self.log(f"PREVIEW fill {name} with {size} bytes")
            results.append({"kind": "fileop", "op": "fill", "name": name, "size": size, "dry_run": True}); return
        if op == "wipe":
            records = self._resolve_action_targets(action)
            for rec in records:
                if "_data" in rec: rec["_data"] = b"\0" * rec.get("size", 0)
            self.log(f"PREVIEW wipe {len(records)} file(s)")
            results.append({"kind": "fileop", "op": "wipe", "count": len(records), "dry_run": True}); return
        if op in ("encrypt", "decrypt"):
            records = self._resolve_action_targets(action)
            for rec in records:
                if "_data" in rec:
                    rec["attributes"] = (rec["attributes"] | FILE_ATTR_ENCRYPTED) if op == "encrypt" else (rec["attributes"] & ~FILE_ATTR_ENCRYPTED)
                    rec["encrypted"] = op == "encrypt"
            self.log(f"PREVIEW {op} {len(records)} file(s)")
            results.append({"kind": "fileop", "op": op, "count": len(records), "dry_run": True}); return
        if op == "makearchive":
            name = action["name"]
            if not name.lower().endswith(".marekarchv"): name += ".MAREKARCHV"
            if self._find(name) is not None: raise ScriptrError(f"'{name}' already exists.")
            members = self._resolve_action_targets(action)
            archive = {os.path.basename(r["filename"]): self._read_record(r) for r in members if not r.get("is_dir")}
            data = create_marekfs_archive(archive)
            self.virtual[name] = {"filename": name, "is_dir": False, "sector": -1, "size": len(data),
                                  "attributes": FILE_ATTR_ARCHIVE, "encrypted": False, "_data": data}
            self.log(f"PREVIEW makearchive {name} with {len(archive)} member(s)")
            results.append({"kind": "fileop", "op": "makearchive", "name": name, "members": len(archive), "dry_run": True}); return
        if op == "addtoarchive":
            name = action["name"]; rec = self._find(name)
            if rec is None: raise ScriptrError(f"Archive not found: {name}")
            members = self._resolve_action_targets(action)
            existing = parse_marekfs_archive(self._read_record(rec)) or {}
            for r in members:
                if not r.get("is_dir"): existing[os.path.basename(r["filename"])] = self._read_record(r)
            if "_data" in rec:
                rec["_data"] = create_marekfs_archive(existing); rec["size"] = len(rec["_data"])
            self.log(f"PREVIEW addtoarchive {name} (+{len(members)} member(s))")
            results.append({"kind": "fileop", "op": "addtoarchive", "name": name, "added": len(members), "dry_run": True}); return
        if op == "extractarchive":
            name = action["name"]; rec = self._find(name)
            if rec is None: raise ScriptrError(f"Archive not found: {name}")
            archive = parse_marekfs_archive(self._read_record(rec)) or {}
            extracted = 0
            for member_name, data in archive.items():
                if self._find(member_name) is not None:
                    self.log(f"PREVIEW extract: skip existing {member_name}"); continue
                self.virtual[member_name] = {"filename": member_name, "is_dir": False, "sector": -1,
                                             "size": len(data), "attributes": 0, "encrypted": False, "_data": data}
                extracted += 1
            self.log(f"PREVIEW extractarchive {name} ({extracted} member(s))")
            results.append({"kind": "fileop", "op": "extractarchive", "name": name, "extracted": extracted, "dry_run": True}); return
        if op == "export":
            name = action["name"]; rec = self._find(name)
            if rec is None: raise ScriptrError(f"File not found: {name}")
            data = self._read_record(rec)
            self.log(f"PREVIEW export {name} -> {action['path']} ({len(data)} bytes)")
            results.append({"kind": "fileop", "op": "export", "name": name, "path": action["path"], "size": len(data), "dry_run": True}); return
        if op == "import":
            path, name = action["path"], action["name"]
            if not os.path.isfile(path): raise ScriptrError(f"Host file not found: {path}")
            size = os.path.getsize(path)
            if size > IMPORT_MAX_BYTES: raise ScriptrError("Host file too large to import.")
            if self._find(name) is not None: raise ScriptrError(f"'{name}' already exists.")
            with open(path, "rb") as f: data = f.read()
            self.virtual[name] = {"filename": name, "is_dir": False, "sector": -1, "size": len(data),
                                  "attributes": 0, "encrypted": False, "_data": data}
            self.log(f"PREVIEW import {path} -> {name} ({size} bytes)")
            results.append({"kind": "fileop", "op": "import", "name": name, "path": path, "size": size, "dry_run": True}); return
        raise ScriptrError(f"Unknown file op: {op}")

    # ------------------------------------------------------ file ops: apply
    def _fileop_apply(self, action, results):
        if self.writer is None:
            raise ScriptrError("File operations need a writer — open Scriptr from the MarekFS app.")
        for key in ("name", "old", "new", "src", "dst", "path", "content", "data", "password"):
            if isinstance(action.get(key), str):
                action[key] = self._expand(action[key])
        op = action["op"]
        if op == "create":
            name, data = action["name"], action.get("data", "")
            if isinstance(data, str): data = data.encode("utf-8")
            self.writer.create(name, data, int(action.get("attributes", 0)), bool(action.get("is_dir")))
            self.log(f"CREATED {name} ({len(data)} bytes)")
            results.append({"kind": "fileop", "op": "create", "name": name, "size": len(data), "dry_run": False}); return
        if op == "delete":
            records = self._resolve_action_targets(action)
            for rec in records:
                self.writer.delete(rec)
                self.log(f"DELETED {rec['filename']}")
            if not records: self.log("deletefile: 0 matching files")
            results.append({"kind": "fileop", "op": "delete", "count": len(records),
                            "files": [r["filename"] for r in records], "dry_run": False}); return
        if op in ("write", "append", "prepend"):
            records = self._resolve_action_targets(action)
            content = action["content"].encode("utf-8")
            for rec in records:
                if rec.get("is_dir"): self.log(f"skip directory {rec['filename']}"); continue
                if op == "write": data = content
                elif op == "append": data = self._read_record(rec) + content
                else: data = content + self._read_record(rec)
                self.writer.write(rec, data)
                self.log(f"{op.upper()} {rec['filename']} ({len(data)} bytes)")
            if not records: self.log(f"{op}: 0 matching files")
            results.append({"kind": "fileop", "op": op, "count": len(records), "dry_run": False}); return
        if op == "setattr":
            records = self._resolve_action_targets(action)
            for rec in records:
                self.writer.set_attributes(rec, action["mask"], action["set"])
                self.log(f"{'SET' if action['set'] else 'CLEARED'} attributes on {rec['filename']}")
            if not records: self.log(f"{'set' if action['set'] else 'clear'}attributes: 0 matching files")
            results.append({"kind": "fileop", "op": "setattr", "count": len(records), "dry_run": False}); return
        if op == "rename":
            old, new = self._expand(action["old"]), self._expand(action["new"])
            rec = self._find(old)
            if rec is None: raise ScriptrError(f"File not found: {old}")
            self.writer.rename(rec, new)
            self.log(f"RENAMED {old} -> {new}")
            results.append({"kind": "fileop", "op": "rename", "old": old, "new": new, "dry_run": False}); return
        if op == "copy":
            src, dst = self._expand(action["src"]), self._expand(action["dst"])
            rec = self._find(src)
            if rec is None: raise ScriptrError(f"File not found: {src}")
            data = self._read_record(rec)
            self.writer.create(dst, data, rec.get("attributes", 0) & ~FILE_ATTR_ENCRYPTED, False)
            self.log(f"COPIED {src} -> {dst} ({len(data)} bytes)")
            results.append({"kind": "fileop", "op": "copy", "src": src, "dst": dst, "dry_run": False}); return
        if op == "clone":
            src = self._expand(action["src"]); count = action["count"]
            if count < 1 or count > 256: raise ScriptrError("clone count must be 1..256")
            rec = self._find(src)
            if rec is None: raise ScriptrError(f"File not found: {src}")
            data = self._read_record(rec)
            stem, ext = os.path.splitext(src); names = []
            for i in range(1, count + 1):
                name = f"{stem} ({i}){ext}"
                self.writer.create(name, data, rec.get("attributes", 0) & ~FILE_ATTR_ENCRYPTED, False)
                names.append(name)
            self.log(f"CLONED {src} -> {', '.join(names)}")
            results.append({"kind": "fileop", "op": "clone", "src": src, "count": count, "dry_run": False}); return
        if op == "touch":
            name = action["name"]
            if self._find(name) is None:
                self.writer.create(name, b"", 0, False)
                self.log(f"CREATED {name} (touch)")
            else:
                self.log(f"touch {name} (already exists)")
            results.append({"kind": "fileop", "op": "touch", "name": name, "dry_run": False}); return
        if op == "truncate":
            name = action["name"]; rec = self._find(name)
            if rec is None: raise ScriptrError(f"File not found: {name}")
            data = self._read_record(rec); size = action["size"]
            new = data[:size] if len(data) >= size else data + b"\0" * (size - len(data))
            self.writer.write(rec, new)
            self.log(f"TRUNCATED {name} to {size} bytes")
            results.append({"kind": "fileop", "op": "truncate", "name": name, "size": size, "dry_run": False}); return
        if op == "fill":
            name = action["name"]; rec = self._find(name)
            if rec is None: raise ScriptrError(f"File not found: {name}")
            pattern = action["pattern"] or b"A"; size = action["size"]
            if not pattern: raise ScriptrError("Fill pattern is empty.")
            new = (pattern * (size // len(pattern) + 1))[:size]
            self.writer.write(rec, new)
            self.log(f"FILLED {name} with {size} bytes")
            results.append({"kind": "fileop", "op": "fill", "name": name, "size": size, "dry_run": False}); return
        if op == "wipe":
            records = self._resolve_action_targets(action)
            for rec in records:
                self.writer.write(rec, b"\0" * rec.get("size", 0))
                self.log(f"WIPED {rec['filename']}")
            if not records: self.log("wipefile: 0 matching files")
            results.append({"kind": "fileop", "op": "wipe", "count": len(records), "dry_run": False}); return
        if op in ("encrypt", "decrypt"):
            records = self._resolve_action_targets(action)
            password = action["password"]
            if not password: raise ScriptrError("A password is required.")
            for rec in records:
                data = self._read_record(rec, password if op == "decrypt" else "")
                if op == "encrypt":
                    self.writer.write(rec, data, password=password)
                else:
                    self.writer.write(rec, data, password="", attributes=rec.get("attributes", 0) & ~FILE_ATTR_ENCRYPTED)
                self.log(f"{op.upper()} {rec['filename']}")
            if not records: self.log(f"{op}: 0 matching files")
            results.append({"kind": "fileop", "op": op, "count": len(records), "dry_run": False}); return
        if op == "makearchive":
            name = action["name"]
            if not name.lower().endswith(".marekarchv"): name += ".MAREKARCHV"
            if self._find(name) is not None: raise ScriptrError(f"'{name}' already exists.")
            members = self._resolve_action_targets(action)
            archive = {}
            for r in members:
                if r.get("is_dir"): continue
                archive[os.path.basename(r["filename"])] = self._read_record(r)
            data = create_marekfs_archive(archive)
            self.writer.create(name, data, FILE_ATTR_ARCHIVE, False)
            self.log(f"ARCHIVED {len(archive)} member(s) -> {name}")
            results.append({"kind": "fileop", "op": "makearchive", "name": name, "members": len(archive), "dry_run": False}); return
        if op == "addtoarchive":
            name = action["name"]; rec = self._find(name)
            if rec is None: raise ScriptrError(f"Archive not found: {name}")
            members = self._resolve_action_targets(action)
            existing = parse_marekfs_archive(self._read_record(rec)) or {}
            added = 0
            for r in members:
                if r.get("is_dir"): continue
                existing[os.path.basename(r["filename"])] = self._read_record(r); added += 1
            self.writer.write(rec, create_marekfs_archive(existing))
            self.log(f"ADDED {added} member(s) to {name}")
            results.append({"kind": "fileop", "op": "addtoarchive", "name": name, "added": added, "dry_run": False}); return
        if op == "extractarchive":
            name = action["name"]; rec = self._find(name)
            if rec is None: raise ScriptrError(f"Archive not found: {name}")
            archive = parse_marekfs_archive(self._read_record(rec)) or {}
            extracted = 0
            for member_name, data in archive.items():
                if self._find(member_name) is not None:
                    self.log(f"skip existing {member_name}"); continue
                self.writer.create(member_name, data, 0, False); extracted += 1
            self.log(f"EXTRACTED {extracted} member(s) from {name}")
            results.append({"kind": "fileop", "op": "extractarchive", "name": name, "extracted": extracted, "dry_run": False}); return
        if op == "export":
            name = action["name"]; rec = self._find(name)
            if rec is None: raise ScriptrError(f"File not found: {name}")
            data = self._read_record(rec)
            if len(data) > EXPORT_MAX_BYTES: raise ScriptrError("File too large to export.")
            path = action["path"]
            os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
            with open(path, "wb") as f: f.write(data)
            self.log(f"EXPORTED {name} -> {path} ({len(data)} bytes)")
            results.append({"kind": "fileop", "op": "export", "name": name, "path": path, "size": len(data), "dry_run": False}); return
        if op == "import":
            path, name = action["path"], action["name"]
            if not os.path.isfile(path): raise ScriptrError(f"Host file not found: {path}")
            size = os.path.getsize(path)
            if size > IMPORT_MAX_BYTES: raise ScriptrError("Host file too large to import.")
            with open(path, "rb") as f: data = f.read()
            self.writer.create(name, data, 0, False)
            self.log(f"IMPORTED {path} -> {name} ({size} bytes)")
            results.append({"kind": "fileop", "op": "import", "name": name, "path": path, "size": size, "dry_run": False}); return
        raise ScriptrError(f"Unknown file op: {op}")

    def _preflight_backup(self, plan, results):
        """Snapshot every real file a fileop will touch BEFORE any change."""
        files = {}
        for action in plan:
            if action["kind"] != "fileop": continue
            records = []
            try:
                if "records" in action:
                    records = self.query(action["records"])
                elif action["op"] in ("rename", "truncate", "fill", "export", "extractarchive"):
                    rec = self._find(action.get("old") or action.get("name"))
                    records = [rec] if rec else []
                elif action["op"] in ("copy", "clone"):
                    rec = self._find(action.get("src")); records = [rec] if rec else []
                elif action["op"] == "addtoarchive":
                    rec = self._find(action["name"])
                    records = ([rec] if rec else []) + self._target_records(action["targets"])
                elif action["op"] == "makearchive":
                    records = self._target_records(action["targets"])
                elif action["op"] in ("write", "append", "prepend", "delete", "setattr", "wipe", "encrypt", "decrypt"):
                    records = self._resolve_action_targets(action)
            except Exception:
                continue
            for rec in records:
                if rec is None or rec.get("is_dir") or rec["filename"] in files: continue
                try:
                    data = self.reader(rec) if self.reader else self._read_record(rec)
                    files[rec["filename"]] = data.hex()
                except Exception:
                    continue
        if not files: return
        os.makedirs(BACKUP_DIR, exist_ok=True)
        backup_path = os.path.join(BACKUP_DIR, os.path.basename(self.drive_path) + f".{time.time_ns()}.json")
        try:
            with open(backup_path, "w", encoding="utf-8") as out:
                json.dump({"drive": os.path.abspath(self.drive_path), "files": files}, out, indent=2)
        except Exception as e:
            raise ScriptrError(f"Cannot create backup: {e}")
        self._backup_path = backup_path
        self.log(f"Backup written: {backup_path} ({len(files)} file(s))")
        results.append({"kind": "backup", "path": backup_path, "files": len(files)})


class ScriptrDriveWriter:
    """Bridges Scriptr file operations to the running MarekFS app.

    All ops mutate app state in place (files_data, next_free_sector,
    metadata, RAM cache, file-id database) and defer the on-disk directory
    rewrite to commit(), which runs once after the whole script. Raises
    ScriptrError instead of popping message boxes.
    """

    def __init__(self, app):
        self.app = app
        self.ops = 0

    # --- introspection -------------------------------------------------
    def find(self, filename):
        return next((r for r in self.app.files_data if r["filename"] == filename), None)

    def next_free_sector(self):
        return int(getattr(self.app, "next_free_sector", 0))

    def metadata_get(self, name):
        try: return self.app.file_metadata.get(name)
        except Exception: return None

    def read(self, record, password=""):
        return self.app._read_file_bytes(record, password)

    # --- mutations -----------------------------------------------------
    def create(self, name, data=b"", attributes=0, is_dir=False):
        if len(name) > MAX_LOGICAL_FILENAME_CHARS:
            raise ScriptrError(f"Filenames can be up to {MAX_LOGICAL_FILENAME_CHARS} characters.")
        if len(self.app.files_data) >= MAX_FILE_COUNT:
            raise ScriptrError(f"Maximum of {MAX_FILE_COUNT:,} files reached.")
        if self.find(name):
            raise ScriptrError(f"'{name}' already exists.")
        data = bytes(data)
        padded, attrs = prepare_file_payload(data, "", attributes)
        record = {"filename": name, "is_dir": bool(is_dir), "sector": self.app.next_free_sector,
                  "size": len(data), "attributes": attrs, "encrypted": bool(attrs & FILE_ATTR_ENCRYPTED)}
        self.app.files_data.append(record)
        self.app.next_free_sector += max(1, len(padded) // SECTOR_SIZE)
        try:
            fd = open_drive(self.app.drive_path, read_write=True)
            try: write_with_journal(fd, record["sector"], padded)
            finally: os.close(fd)
        except Exception as e:
            self.app.files_data.remove(record)
            raise ScriptrError(f"Create failed: {e}")
        if data:
            md = self.app.file_metadata.setdefault(name, {})
            md["checksum"] = data_checksum(data)
            save_file_metadata(self.app.drive_path, self.app.file_metadata)
        self.ops += 1
        return record

    def write(self, record, data, password="", attributes=None):
        attrs = record.get("attributes", 0) if attributes is None else attributes
        data = bytes(data)
        padded, final = prepare_file_payload(data, password, attrs)
        old_logical = record.get("size", 0) + (44 if (record.get("encrypted") or (record.get("attributes", 0) & FILE_ATTR_ENCRYPTED)) else 0)
        old_sectors = max(1, (old_logical + SECTOR_SIZE - 1) // SECTOR_SIZE)
        new_sectors = max(1, len(padded) // SECTOR_SIZE)
        try:
            fd = open_drive(self.app.drive_path, read_write=True)
            try:
                if new_sectors > old_sectors:
                    record["sector"] = self.app.next_free_sector
                    self.app.next_free_sector += new_sectors
                write_with_journal(fd, record["sector"], padded)
            finally:
                os.close(fd)
        except Exception as e:
            raise ScriptrError(f"Write failed for '{record['filename']}': {e}")
        record["size"] = len(data)
        record["attributes"] = final
        record["encrypted"] = bool(final & FILE_ATTR_ENCRYPTED)
        try:
            self.app.ram_cache.put(self.app._cache_key(record["filename"]), data,
                                   persist=not bool(final & FILE_ATTR_ENCRYPTED))
            md = self.app.file_metadata.setdefault(record["filename"], {})
            md["checksum"] = data_checksum(data)
            save_file_metadata(self.app.drive_path, self.app.file_metadata)
        except Exception:
            pass
        self.ops += 1
        return record

    def set_attributes(self, record, mask, set_flag=True):
        if set_flag and (mask & FILE_ATTR_ENCRYPTED):
            raise ScriptrError("Use encryptfile({name})({password}) to set the encrypted attribute.")
        record["attributes"] = (record.get("attributes", 0) | mask) if set_flag else (record.get("attributes", 0) & ~mask)
        if mask & FILE_ATTR_DIRECTORY:
            record["is_dir"] = bool(record["attributes"] & FILE_ATTR_DIRECTORY)
        self.ops += 1
        return record

    def delete(self, record):
        try:
            self.app.files_data.remove(record)
        except ValueError:
            raise ScriptrError(f"'{record['filename']}' is not in the directory.")
        try:
            self.app.ram_cache.invalidate(self.app._cache_key(record["filename"]))
        except Exception:
            pass
        self.ops += 1

    def rename(self, record, new_name):
        if len(new_name) > MAX_LOGICAL_FILENAME_CHARS:
            raise ScriptrError(f"Filenames can be up to {MAX_LOGICAL_FILENAME_CHARS} characters.")
        if self.find(new_name):
            raise ScriptrError(f"'{new_name}' already exists.")
        old_name = record["filename"]
        record["filename"] = new_name
        try:
            fid = str(file_id_for_record(record))
            self.app.file_id_database.setdefault(fid, {})
            self.app.file_id_database[fid].update({"name": new_name, "file_id": int(fid), "updated": time.time()})
            save_file_id_database(self.app.file_id_database)
        except Exception:
            pass
        try:
            if old_name in self.app.file_metadata:
                self.app.file_metadata[new_name] = self.app.file_metadata.pop(old_name)
                save_file_metadata(self.app.drive_path, self.app.file_metadata)
        except Exception:
            pass
        self.ops += 1

    def commit(self):
        if self.ops <= 0:
            return
        fd = open_drive(self.app.drive_path, read_write=True)
        try:
            self.app.update_directory(fd)
            try: os.fsync(fd)
            except Exception: pass
        finally:
            os.close(fd)
        try:
            self.app.scan_drive()
        except Exception:
            pass
        self.ops = 0


def sample_script():
    return """# MarekFS Scriptr v2 — preview first. Writes need explicit confirmation.
print MarekFS Scriptr v2 sample

# --- variables ---
set author Marek

# --- create files ---
createnewfile({readme.txt})({Welcome to MarekFS, from $(author)!})
createnewfile({system.log})({boot ok})
createnewfile({notes.txt})({these are my notes})

# --- writetofile({content})({filename / ext: / *}) ---
writetofile({UPDATED by Scriptr v2})({readme.txt, system.log})
appendtofile({ - appended by scriptr})({notes.txt})

# --- queries (these work in preview too) ---
readfile({readme.txt})
filesize({notes.txt})
checksum({readme.txt})({sha256})
listfiles(ext:.txt)

# --- targets by extension ---
writetofile({scriptr touched})({ext:.txt})

# --- conditional sector wipe: zero sectors starting with MZ ---
ifsectorhexdatamatches({4D5A}) then
zerofill

# --- conditional file action: hide every .tmp file ---
if file ext:.tmp then
setattributes({hidden})

# --- archive + extract ---
makearchive({demo.MAREKARCHV})({readme.txt, notes.txt})
extractarchive({demo.MAREKARCHV})

print Done — preview shows everything that would happen.
"""

class ScriptrConsoleWindow:
    def __init__(self, parent, drive_path, records, reader=None, writer=None):
        self.engine = ScriptrEngine(drive_path, records, reader, writer)
        self.win = tk.Toplevel(parent); theme_existing_window(self.win, parent, title="🧠 MarekFS Scriptr Console"); self.win.geometry("960x680")
        ttk.Label(self.win, text="MarekFS Scriptr v2 · preview-first console", style="Title.TLabel").pack(anchor=tk.W, padx=10, pady=8)
        self.script = tk.Text(self.win, height=16, wrap=tk.NONE); self.script.pack(fill=tk.BOTH, expand=True, padx=10); self.script.insert("1.0", sample_script())
        bar = ttk.Frame(self.win, padding=8); bar.pack(fill=tk.X)
        ttk.Button(bar, text="▶ Preview / Dry-run", style="Accent.TButton", command=self.preview).pack(side=tk.LEFT, padx=4)
        ttk.Button(bar, text="⚡ Execute writes…", command=self.execute).pack(side=tk.LEFT, padx=4)
        ttk.Button(bar, text="Load script", command=self.load_script).pack(side=tk.LEFT, padx=4)
        ttk.Button(bar, text="📖 Command reference", command=self.show_reference).pack(side=tk.LEFT, padx=4)
        ttk.Label(bar, text="Insert:").pack(side=tk.LEFT, padx=(16, 2))
        self.snippet_box = ttk.Combobox(bar, values=SNIPPETS, width=34, state="readonly")
        self.snippet_box.pack(side=tk.LEFT, padx=2)
        self.snippet_box.bind("<<ComboboxSelected>>", self._insert_snippet)
        self.output = tk.Text(self.win, height=10, state="disabled"); self.output.pack(fill=tk.BOTH, expand=True, padx=10)
        ttk.Label(self.win, text="File writes create a ProgramData backup before changing anything.").pack(anchor=tk.W, padx=10, pady=6)

    def show(self, text):
        self.output.configure(state="normal"); self.output.delete("1.0", tk.END); self.output.insert(tk.END, text); self.output.configure(state="disabled")

    def _insert_snippet(self, _event=None):
        snippet = self.snippet_box.get()
        if not snippet: return
        self.script.insert("insert", snippet + "\n")
        self.snippet_box.set("")

    def preview(self):
        try:
            r = self.engine.execute(self.script.get("1.0", tk.END), dry_run=True)
            self.show("DRY-RUN ONLY — nothing was written\n" + "\n".join(r["output"]) + "\n\n" + json.dumps(r["results"], indent=2))
        except Exception as e:
            self.show(f"Scriptr error: {e}")

    def execute(self):
        if not messagebox.askyesno("Confirm Scriptr execution",
                                   "This can modify files and sectors in the selected MarekFS image.\n"
                                   "A ProgramData backup is written before changes.\nContinue?", parent=self.win):
            return
        try:
            r = self.engine.execute(self.script.get("1.0", tk.END), dry_run=False, confirmed=True)
            self.show("EXECUTED\n" + "\n".join(r["output"]) + "\n\n" + json.dumps(r["results"], indent=2))
        except Exception as e:
            self.show(f"Scriptr blocked/error: {e}")

    def show_reference(self):
        self.show(self.engine.reference())

    def load_script(self):
        path = filedialog.askopenfilename(parent=self.win, filetypes=[("MarekFS scripts", "*.mfscr *.txt"), ("All files", "*.*")])
        if not path: return
        with open(path, encoding="utf-8") as f: data = f.read(MAX_SCRIPT_BYTES + 1)
        if len(data.encode()) > MAX_SCRIPT_BYTES: messagebox.showerror("Scriptr", "Script is too large.", parent=self.win); return
        self.script.delete("1.0", tk.END); self.script.insert("1.0", data)
