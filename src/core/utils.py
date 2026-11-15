# -*- coding: utf-8 -*-
"""
Shared file I/O helpers for IR workspace modules.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Dict


def load_json(path: Path) -> Any:
    """Reads and decodes a UTF-8 JSON file."""
    return json.loads(path.read_text(encoding="utf-8"))


def save_json(path: Path, data: Any) -> None:
    """Atomically writes data as UTF-8 formatted JSON using a temporary file and os.replace."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, ensure_ascii=False, indent=2)
    fd, tmp_name = tempfile.mkstemp(prefix=".tmp_", suffix=".json", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fp:
            fp.write(payload)
            fp.flush()
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def upsert_map_info_entry(proj: Dict[str, Any], map_id: int, info_obj: Dict[str, Any]) -> None:
    """Inserts or updates a map_infos entry, supporting both int_map entries and flat dict layouts."""
    mid = int(map_id)
    mi = proj.setdefault("map_infos", {"__type__": "int_map", "entries": []})
    entries = mi.get("entries")
    if isinstance(entries, list):
        for pair in entries:
            if isinstance(pair, list) and len(pair) >= 2 and int(pair[0]) == mid:
                pair[1] = info_obj
                return
        entries.append([mid, info_obj])
        return
    mi[str(mid)] = info_obj


def set_map_info_name(proj: Dict[str, Any], map_id: int, name: str) -> bool:
    """Updates a map's display name inside ir_project.json map_infos (entries list or flat dict)."""
    mi = proj.get("map_infos")
    if not isinstance(mi, dict):
        return False
    mid = int(map_id)
    entries = mi.get("entries")
    if isinstance(entries, list):
        for pair in entries:
            if isinstance(pair, list) and len(pair) >= 2 and int(pair[0]) == mid:
                info = pair[1]
                if isinstance(info, dict) and isinstance(info.get("fields"), dict):
                    info["fields"]["name"] = str(name)
                    return True
        return False
    key = str(mid)
    if key in mi and isinstance(mi[key], dict) and isinstance(mi[key].get("fields"), dict):
        mi[key]["fields"]["name"] = str(name)
        return True
    return False


def remove_map_info_entry(proj: Dict[str, Any], map_id: int) -> bool:
    """Removes a map entry from ir_project.json map_infos (entries list or flat dict)."""
    mi = proj.get("map_infos")
    if not isinstance(mi, dict):
        return False
    mid = int(map_id)
    entries = mi.get("entries")
    if isinstance(entries, list):
        before = len(entries)
        mi["entries"] = [
            pair for pair in entries
            if not (isinstance(pair, list) and len(pair) >= 1 and int(pair[0]) == mid)
        ]
        return len(mi["entries"]) != before
    return mi.pop(str(mid), None) is not None
