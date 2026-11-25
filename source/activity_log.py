"""Append-only local activity journal; Qwen draft reviewed for byte-safe bounded reads."""
import json
import os
import threading
import math
import uuid
from datetime import datetime, timezone


class ActivityLog:
    """
    Thread-safe, append-only activity log stored as JSONL.
    - Constructor creates activity.jsonl in the supplied folder.
    - append(level, message, context=None) writes a JSON line with UTC ISO timestamp and opaque event ID.
    - list_recent(limit=200, level=None) returns newest-first rows, bounded 1..1000.
    - Tolerates corrupt lines.
    - Scans last at most 2 MiB to avoid loading entire file into memory.
    - Prevents newline injection via json serialization.
    - Context restricted to scalar keys: operation, asset_id, dataset, index.
    """

    MAX_MESSAGE_LEN = 2000
    MAX_SCAN_BYTES = 2 * 1024 * 1024  # 2 MiB
    ALLOWED_CONTEXT_KEYS = {"operation", "asset_id", "dataset", "index"}
    VALID_LEVELS = {"info", "warning", "error"}

    def __init__(self, folder):
        self._folder = folder
        self._log_path = os.path.join(folder, "activity.jsonl")
        self._lock = threading.Lock()
        # Ensure folder exists
        os.makedirs(folder, exist_ok=True)
        # Create file if not exists
        if not os.path.exists(self._log_path):
            with open(self._log_path, "a", encoding="utf-8") as f:
                pass

    @property
    def path(self):
        return self._log_path

    def _sanitize_context(self, context):
        """Restrict context to allowed scalar keys, drop secrets/transcripts."""
        if context is None:
            return None
        if not isinstance(context, dict):
            return None
        sanitized = {}
        for key in self.ALLOWED_CONTEXT_KEYS:
            if key in context:
                val = context[key]
                # Only allow scalar types: str, int, float, bool, None
                if isinstance(val, (str, int, float, bool)) or val is None:
                    if isinstance(val,float) and not math.isfinite(val):continue
                    sanitized[key] = val[:256] if isinstance(val,str) else val
        return sanitized if sanitized else None

    def append(self, level, message, context=None):
        """
        Append a log entry. Thread-safe.
        level: 'info', 'warning', or 'error'
        message: str, truncated to 2000 chars
        context: optional dict, restricted to allowed scalar keys
        """
        if level not in self.VALID_LEVELS:
            raise ValueError(f"Invalid level: {level}. Must be one of {self.VALID_LEVELS}")
        if not isinstance(message, str):
            message = str(message)
        message = message[:self.MAX_MESSAGE_LEN]
        ctx = self._sanitize_context(context)
        event_id = str(uuid.uuid4())
        timestamp = datetime.now(timezone.utc).isoformat()
        entry = {
            "event_id": event_id,
            "timestamp": timestamp,
            "level": level,
            "message": message,
        }
        if ctx is not None:
            entry["context"] = ctx
        # json.dumps ensures proper escaping, preventing newline injection
        line = json.dumps(entry, ensure_ascii=False) + "\n"
        with self._lock:
            with open(self._log_path, "ab+") as f:
                f.seek(0,2)
                if f.tell():
                    f.seek(-1,2)
                    if f.read(1)!=b'\n':f.write(b'\n')
                f.write(line.encode('utf-8'));f.flush()
        return entry

    def list_recent(self, limit=200, level=None):
        """
        Return newest-first log entries, bounded 1..1000.
        Scans last at most 2 MiB of the file.
        Tolerates corrupt lines.
        """
        if type(limit) is not int:raise ValueError('Log limit must be an integer.')
        if level is not None and level not in self.VALID_LEVELS:raise ValueError('Unknown log level.')
        if limit < 1:
            limit = 1
        if limit > 1000:
            limit = 1000

        if not os.path.exists(self._log_path):
            return []

        with self._lock:
            with open(self._log_path,'rb') as f:
                f.seek(0,2);file_size=f.tell()
                start_pos=max(0,file_size-self.MAX_SCAN_BYTES)
                f.seek(start_pos);raw=f.read(self.MAX_SCAN_BYTES)
        if start_pos>0:raw=raw.partition(b'\n')[2]
        lines=raw.decode('utf-8',errors='replace').splitlines()

        entries=[]
        # Process lines in reverse (newest first)
        for line in reversed(lines):
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
                # Basic validation
                if not isinstance(entry, dict):
                    continue
                if "level" not in entry or "message" not in entry:
                    continue
                if level is not None and entry.get("level") != level:
                    continue
                entries.append(entry)
                if len(entries) >= limit:
                    break
            except (json.JSONDecodeError, UnicodeDecodeError):
                # Tolerate corrupt lines
                continue

        return entries
