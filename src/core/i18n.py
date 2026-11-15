# -*- coding: utf-8 -*-
"""
Internationalization (i18n) Extraction & Locale Application for IR Workspaces.
Extracts dialogue, choice labels, map names, and database entries into
editable JSON translation tables and applies locale overrides safely.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .utils import load_json as _load_json, save_json as _save_json, set_map_info_name as _set_map_info_name

_DB_I18N_TABLES: Tuple[str, ...] = (
    "actors",
    "classes",
    "skills",
    "items",
    "weapons",
    "armors",
    "enemies",
    "states",
)


def _walk_command_strings(
    prefix: str,
    commands: List[Dict[str, Any]],
    emit: Any,
) -> None:
    for idx, cmd in enumerate(commands):
        op = str(cmd.get("op", ""))
        if op == "say":
            lines = [str(x) for x in cmd.get("lines", [])]
            emit(f"{prefix}.say.{idx}", "\n".join(lines), cmd)
        elif op == "show_choices":
            choices = list(cmd.get("choices", []))
            for c_idx, ch in enumerate(choices):
                emit(f"{prefix}.choice.{idx}.{c_idx}", str(ch), (cmd, c_idx))


def extract_i18n_strings(
    ir_dir: Path,
    out_path: Optional[Path] = None,
    default_locale: str = "ko",
) -> Dict[str, Any]:
    """Scans IR project metadata, database tables, maps, and common events into a translation catalog."""
    ir_dir = Path(ir_dir)
    target = Path(out_path) if out_path else (ir_dir / "locale" / "messages.json")
    existing_strings: Dict[str, Dict[str, str]] = {}
    if target.exists():
        try:
            loaded = _load_json(target)
            if isinstance(loaded, dict) and isinstance(loaded.get("strings"), dict):
                existing_strings = {
                    str(k): {str(lk): str(lv) for lk, lv in v.items()}
                    for k, v in loaded["strings"].items()
                    if isinstance(v, dict)
                }
        except Exception:
            existing_strings = {}

    strings: Dict[str, Dict[str, str]] = {}

    def _record(key: str, text: str, _target_obj: Any = None) -> None:
        if not text:
            return
        entry = dict(existing_strings.get(key, {}))
        entry.setdefault(default_locale, text)
        strings[key] = entry

    proj = _load_json(ir_dir / "ir_project.json")
    sys_fields = proj.get("system", {}).get("fields", {})
    _record("system.game_title", str(sys_fields.get("game_title", "")))

    db_dir = ir_dir / "database"
    for tbl in _DB_I18N_TABLES:
        p = db_dir / f"{tbl}.json"
        if not p.exists():
            continue
        for row in _load_json(p):
            if not row:
                continue
            f = row.get("fields", {})
            eid = int(f.get("id", 0))
            _record(f"db.{tbl}.{eid}.name", str(f.get("name", "")))
            if "description" in f and f.get("description"):
                _record(f"db.{tbl}.{eid}.description", str(f.get("description", "")))

    ce_path = db_dir / "common_events.json"
    if ce_path.exists():
        for ce in _load_json(ce_path):
            if ce:
                cid = int(ce.get("id", ce.get("fields", {}).get("id", 0)))
                cmds = ce.get("commands", ce.get("fields", {}).get("list", []))
                _walk_command_strings(f"ce.{cid}", cmds, _record)

    for mf in sorted((ir_dir / "maps").glob("map_*.json")):
        m = _load_json(mf)
        mid = int(m.get("map_id", 0))
        _record(f"map.{mid}.name", str(m.get("name", "")))
        for ev in m.get("events", []):
            eid = int(ev.get("id", 0))
            for p_idx, page in enumerate(ev.get("pages", [])):
                _walk_command_strings(f"map.{mid}.ev.{eid}.p{p_idx}", page.get("commands", []), _record)

    payload = {"default_locale": default_locale, "strings": strings}
    target.parent.mkdir(parents=True, exist_ok=True)
    _save_json(target, payload)
    locales = sorted({lk for v in strings.values() for lk in v.keys()})
    return {"output": str(target.resolve()), "key_count": len(strings), "locales": locales}


def apply_i18n_locale(
    ir_dir: Path,
    locale: str,
    strings_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Rewrites IR text fields in place using localized entries from locale/messages.json."""
    ir_dir = Path(ir_dir)
    src = Path(strings_path) if strings_path else (ir_dir / "locale" / "messages.json")
    if not src.exists():
        return {"locale": locale, "applied_count": 0}
    catalog = _load_json(src)
    strings: Dict[str, Dict[str, str]] = catalog.get("strings", {}) if isinstance(catalog, dict) else {}
    applied = 0

    def _tr(key: str) -> Optional[str]:
        entry = strings.get(key)
        if isinstance(entry, dict) and locale in entry and entry[locale] != "":
            return str(entry[locale])
        return None

    def _apply_cmds(prefix: str, cmds: List[Dict[str, Any]]) -> bool:
        nonlocal applied
        changed = False
        for idx, cmd in enumerate(cmds):
            op = str(cmd.get("op", ""))
            if op == "say":
                val = _tr(f"{prefix}.say.{idx}")
                if val is not None:
                    cmd["lines"] = val.split("\n")
                    applied += 1
                    changed = True
            elif op == "show_choices":
                choices = list(cmd.get("choices", []))
                for c_idx in range(len(choices)):
                    val = _tr(f"{prefix}.choice.{idx}.{c_idx}")
                    if val is not None:
                        choices[c_idx] = val
                        applied += 1
                        changed = True
                cmd["choices"] = choices
        return changed

    proj_path = ir_dir / "ir_project.json"
    proj = _load_json(proj_path)
    proj_dirty = False
    t_val = _tr("system.game_title")
    if t_val is not None:
        proj["system"]["fields"]["game_title"] = t_val
        applied += 1
        proj_dirty = True

    db_dir = ir_dir / "database"
    for tbl in _DB_I18N_TABLES:
        p = db_dir / f"{tbl}.json"
        if not p.exists():
            continue
        rows = _load_json(p)
        dirty = False
        for row in rows:
            if not row:
                continue
            f = row.get("fields", {})
            eid = int(f.get("id", 0))
            nv = _tr(f"db.{tbl}.{eid}.name")
            if nv is not None:
                f["name"] = nv
                applied += 1
                dirty = True
            dv = _tr(f"db.{tbl}.{eid}.description")
            if dv is not None:
                f["description"] = dv
                applied += 1
                dirty = True
        if dirty:
            _save_json(p, rows)

    ce_path = db_dir / "common_events.json"
    if ce_path.exists():
        ces = _load_json(ce_path)
        ce_dirty = False
        for ce in ces:
            if ce:
                cid = int(ce.get("id", ce.get("fields", {}).get("id", 0)))
                cmds = ce.get("commands", ce.get("fields", {}).get("list", []))
                if _apply_cmds(f"ce.{cid}", cmds):
                    ce_dirty = True
        if ce_dirty:
            _save_json(ce_path, ces)

    for mf in sorted((ir_dir / "maps").glob("map_*.json")):
        m = _load_json(mf)
        mid = int(m.get("map_id", 0))
        m_dirty = False
        mv = _tr(f"map.{mid}.name")
        if mv is not None:
            m["name"] = mv
            if _set_map_info_name(proj, mid, mv):
                proj_dirty = True
            applied += 1
            m_dirty = True
        for ev in m.get("events", []):
            eid = int(ev.get("id", 0))
            for p_idx, page in enumerate(ev.get("pages", [])):
                if _apply_cmds(f"map.{mid}.ev.{eid}.p{p_idx}", page.get("commands", [])):
                    m_dirty = True
        if m_dirty:
            _save_json(mf, m)

    if proj_dirty:
        _save_json(proj_path, proj)

    return {"locale": locale, "applied_count": applied}
