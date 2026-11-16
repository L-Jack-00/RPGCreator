# -*- coding: utf-8 -*-
"""
Atomic IR Workspace Editor.
Provides pure, schema-safe operations to create maps, paint tile regions,
add semantic events, and update database entries without manual JSON editing.
"""
from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .ir_converter import TRIGGER_NAME_TO_INT, pack_ir_to_rmxp, ruby_to_ir
from .rgssad import DEFAULT_RGSSAD_KEY, pack_rgssad
from .rmxp_marshal import RubyObject
from .rmxp_types import make_audio_file, make_event, make_event_page
from .utils import (
    load_json as _load_json,
    remove_map_info_entry as _remove_map_info,
    save_json as _save_json,
    set_map_info_name as _set_map_info_name,
    upsert_map_info_entry as _upsert_map_info,
)
from .validator import validate_ir_project, validate_project_assets


def _coerce_value(raw: Any) -> Any:
    if not isinstance(raw, str):
        return raw
    lower = raw.lower()
    if lower == "true":
        return True
    if lower == "false":
        return False
    if lower == "null" or lower == "none":
        return None
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        pass
    return raw


def create_map(
    ir_dir: Path,
    name: str,
    width: int = 20,
    height: int = 15,
    tileset_id: int = 1,
    fill_tile: int = 384,
    parent_id: int = 0,
    record_undo: bool = True,
) -> Dict[str, Any]:
    """Creates a new map JSON file and registers it in ir_project.json map_infos."""
    ir_dir = Path(ir_dir)
    proj_path = ir_dir / "ir_project.json"
    proj = _load_json(proj_path)
    snap = _snapshot_ir_files(ir_dir) if record_undo else None

    existing_ids = [
        int(p.stem.split("_")[1])
        for p in (ir_dir / "maps").glob("map_*.json")
        if "_" in p.stem
    ]
    new_id = (max(existing_ids) + 1) if existing_ids else 1

    layer0 = [[int(fill_tile) for _ in range(width)] for _ in range(height)]
    zeros = [[0 for _ in range(width)] for _ in range(height)]

    map_ir = {
        "map_id": new_id,
        "name": name,
        "tileset_id": int(tileset_id),
        "width": int(width),
        "height": int(height),
        "autoplay_bgm": False,
        "bgm": ruby_to_ir(make_audio_file()),
        "autoplay_bgs": False,
        "bgs": ruby_to_ir(make_audio_file()),
        "encounter_list": [],
        "encounter_step": 30,
        "layers": {"layer_0": layer0, "layer_1": zeros, "layer_2": zeros},
        "events": [],
    }
    map_path = ir_dir / "maps" / f"map_{new_id:03d}.json"
    _save_json(map_path, map_ir)

    info_obj = ruby_to_ir(RubyObject("RPG::MapInfo", {
        "@name": name,
        "@parent_id": int(parent_id),
        "@order": new_id,
        "@expanded": False,
        "@scroll_x": 0,
        "@scroll_y": 0,
    }))
    _upsert_map_info(proj, new_id, info_obj)
    _save_json(proj_path, proj)
    if snap is not None:
        _push_ir_history(ir_dir, f"create_map:{new_id}", snap)

    return {"map_id": new_id, "name": name, "width": width, "height": height, "path": str(map_path)}


def paint_map(
    ir_dir: Path,
    map_id: int,
    layer: int,
    tile_id: int,
    rect: Optional[Tuple[int, int, int, int]] = None,
    points: Optional[List[Tuple[int, int]]] = None,
    record_undo: bool = True,
) -> Dict[str, Any]:
    """Paints a rectangular region or coordinate list onto a specific map layer."""
    if layer not in (0, 1, 2):
        raise ValueError(f"Invalid layer {layer}; expected 0, 1, or 2.")
    ir_dir = Path(ir_dir)
    map_path = ir_dir / "maps" / f"map_{map_id:03d}.json"
    map_ir = _load_json(map_path)
    snap = _snapshot_ir_files(ir_dir) if record_undo else None
    w, h = int(map_ir["width"]), int(map_ir["height"])
    grid = map_ir["layers"][f"layer_{layer}"]

    painted = 0
    if rect is not None:
        rx, ry, rw, rh = rect
        for y in range(max(0, ry), min(h, ry + rh)):
            for x in range(max(0, rx), min(w, rx + rw)):
                grid[y][x] = int(tile_id)
                painted += 1

    if points:
        for px, py in points:
            if px < 0 or py < 0 or px >= w or py >= h:
                raise ValueError(f"Point ({px}, {py}) out of bounds for map {map_id} ({w}x{h}).")
            grid[py][px] = int(tile_id)
            painted += 1

    _save_json(map_path, map_ir)
    if snap is not None and painted > 0:
        _push_ir_history(ir_dir, f"paint_map:{map_id}", snap)
    return {"map_id": map_id, "layer": layer, "tile_id": tile_id, "tiles_painted": painted}


def _build_ir_page(page_spec: Dict[str, Any]) -> Dict[str, Any]:
    """Builds a normalized IR event page dictionary from a concise page specification."""
    trigger = str(page_spec.get("trigger", "action_button"))
    trig_code = TRIGGER_NAME_TO_INT.get(trigger)
    if trig_code is None:
        raise ValueError(f"Unknown trigger '{trigger}'; choose from {list(TRIGGER_NAME_TO_INT.keys())}")

    cond_spec = page_spec.get("condition", {})
    raw_cmds = page_spec.get("commands")
    cmd_list = list(raw_cmds) if raw_cmds is not None else []
    if not cmd_list or cmd_list[-1].get("op") != "end":
        cmd_list.append({"op": "end", "indent": 0})

    base_page = ruby_to_ir(
        make_event_page(
            graphic_name=str(page_spec.get("graphic", "")),
            trigger=trig_code,
            switch1_id=cond_spec.get("switch1_id"),
            switch2_id=cond_spec.get("switch2_id"),
            variable_id=cond_spec.get("variable_id"),
            variable_value=int(cond_spec.get("variable_value", 0)),
            self_switch_ch=cond_spec.get("self_switch_ch"),
            through=bool(page_spec.get("through", False)),
            always_on_top=bool(page_spec.get("always_on_top", False)),
        )
    )
    p_fields = base_page["fields"]
    return {
        "trigger": trigger,
        "trigger_code": trig_code,
        "through": bool(page_spec.get("through", False)),
        "always_on_top": bool(page_spec.get("always_on_top", False)),
        "walk_anime": bool(page_spec.get("walk_anime", True)),
        "step_anime": bool(page_spec.get("step_anime", False)),
        "direction_fix": bool(page_spec.get("direction_fix", False)),
        "move_type": int(page_spec.get("move_type", 0)),
        "move_speed": int(page_spec.get("move_speed", 3)),
        "move_frequency": int(page_spec.get("move_frequency", 3)),
        "move_route": p_fields["move_route"],
        "condition": p_fields["condition"],
        "graphic": p_fields["graphic"],
        "commands": cmd_list,
    }


def add_event(
    ir_dir: Path,
    map_id: int,
    name: str,
    x: int,
    y: int,
    trigger: str = "action_button",
    graphic: str = "",
    through: bool = False,
    commands: Optional[List[Dict[str, Any]]] = None,
    pages: Optional[List[Dict[str, Any]]] = None,
    record_undo: bool = True,
) -> Dict[str, Any]:
    """Adds a single-page or multi-page semantic event to the specified map."""
    ir_dir = Path(ir_dir)
    map_path = ir_dir / "maps" / f"map_{map_id:03d}.json"
    map_ir = _load_json(map_path)
    w, h = int(map_ir["width"]), int(map_ir["height"])
    if x < 0 or y < 0 or x >= w or y >= h:
        raise ValueError(f"Event position ({x}, {y}) out of bounds for map {map_id} ({w}x{h}).")

    existing_ids = [int(ev["id"]) for ev in map_ir.get("events", [])]
    new_ev_id = (max(existing_ids) + 1) if existing_ids else 1

    page_specs = pages if pages is not None else [{
        "trigger": trigger,
        "graphic": graphic,
        "through": through,
        "commands": commands,
    }]
    if not page_specs:
        raise ValueError("Event must contain at least one page.")

    snap = _snapshot_ir_files(ir_dir) if record_undo else None
    ev_ir = {
        "id": new_ev_id,
        "name": name,
        "x": int(x),
        "y": int(y),
        "pages": [_build_ir_page(ps) for ps in page_specs],
    }
    map_ir.setdefault("events", []).append(ev_ir)
    _save_json(map_path, map_ir)
    if snap is not None:
        _push_ir_history(ir_dir, f"add_event:{map_id}:{new_ev_id}", snap)
    return {"map_id": map_id, "event_id": new_ev_id, "name": name, "x": x, "y": y}


def _build_enemy_name_lookup(ir_dir: Path) -> Tuple[Dict[str, int], Set[str]]:
    """Loads unique enemy name -> ID mappings and duplicate names from enemies.json."""
    table: Dict[str, int] = {}
    dups: Set[str] = set()
    enemies_path = ir_dir / "database" / "enemies.json"
    if enemies_path.exists():
        for entry in _load_json(enemies_path):
            if isinstance(entry, dict) and "fields" in entry:
                flds = entry["fields"]
                if flds.get("name"):
                    _register_unique_name(table, dups, str(flds["name"]), int(flds["id"]))
    return table, dups


def _resolve_enemy_name(name: str, enemy_lookup: Tuple[Dict[str, int], Set[str]]) -> int:
    table, dups = enemy_lookup
    if name in dups:
        raise ValueError(f"Ambiguous duplicate enemy name in troop member: {name!r}")
    if name not in table:
        raise ValueError(f"Unknown enemy name in troop member: {name!r}")
    return table[name]


def _build_troop_members(
    raw_members: Any,
    is_enemy_ids: bool = False,
    is_enemy_names: bool = False,
    enemy_lookup: Optional[Tuple[Dict[str, int], Set[str]]] = None,
) -> List[Dict[str, Any]]:
    """Normalizes troop member specs, enemy ID lists, or enemy names into RPG::Troop::Member dicts."""
    if is_enemy_names:
        assert enemy_lookup is not None
        name_list = (
            [x.strip() for x in raw_members.split(",") if x.strip()]
            if isinstance(raw_members, str)
            else [str(x) for x in raw_members]
        )
        return [
            {
                "__type__": "RubyObject",
                "class": "RPG::Troop::Member",
                "fields": {
                    "enemy_id": _resolve_enemy_name(ename, enemy_lookup),
                    "x": 220 + (idx % 4) * 90,
                    "y": 240,
                    "hidden": False,
                    "immortal": False,
                },
            }
            for idx, ename in enumerate(name_list)
        ]

    if is_enemy_ids:
        if isinstance(raw_members, int):
            id_list = [raw_members]
        elif isinstance(raw_members, str):
            id_list = [int(x.strip()) for x in raw_members.split(",") if x.strip()]
        else:
            id_list = [int(x) for x in raw_members]
        return [
            {
                "__type__": "RubyObject",
                "class": "RPG::Troop::Member",
                "fields": {"enemy_id": eid, "x": 220 + (idx % 4) * 90, "y": 240, "hidden": False, "immortal": False},
            }
            for idx, eid in enumerate(id_list)
        ]

    out: List[Dict[str, Any]] = []
    for idx, mem in enumerate(raw_members):
        if isinstance(mem, dict) and mem.get("__type__") == "RubyObject":
            out.append(mem)
        elif isinstance(mem, dict):
            if mem.get("enemy_id") is not None:
                eid = int(mem["enemy_id"])
            elif "enemy_name" in mem and enemy_lookup is not None:
                eid = _resolve_enemy_name(str(mem["enemy_name"]), enemy_lookup)
            else:
                raise ValueError(f"Troop member missing enemy_id or enemy_name: {mem!r}")
            out.append({
                "__type__": "RubyObject",
                "class": "RPG::Troop::Member",
                "fields": {
                    "enemy_id": eid,
                    "x": int(mem.get("x", 220 + (idx % 4) * 90)),
                    "y": int(mem.get("y", 240)),
                    "hidden": bool(mem.get("hidden", False)),
                    "immortal": bool(mem.get("immortal", False)),
                },
            })
        else:
            out.append({
                "__type__": "RubyObject",
                "class": "RPG::Troop::Member",
                "fields": {"enemy_id": int(mem), "x": 220 + (idx % 4) * 90, "y": 240, "hidden": False, "immortal": False},
            })
    return out


def set_db_entry(
    ir_dir: Path,
    table: str,
    entry_id: int,
    fields: Dict[str, Any],
    record_undo: bool = True,
) -> Dict[str, Any]:
    """Updates or inserts a database entry (switches, variables, actors, items, enemies, troops, tilesets, system)."""
    ir_dir = Path(ir_dir)
    snap = _snapshot_ir_files(ir_dir) if record_undo else None

    if table == "system":
        proj_path = ir_dir / "ir_project.json"
        proj = _load_json(proj_path)
        sys_fields = proj.setdefault("system", {}).setdefault("fields", {})
        updated_keys: List[str] = []
        for key, raw_val in fields.items():
            sys_fields[key] = _coerce_value(raw_val)
            updated_keys.append(key)
        _save_json(proj_path, proj)
        if snap is not None:
            _push_ir_history(ir_dir, f"set_db:{table}:{entry_id}", snap)
        return {"table": table, "id": 1, "updated_fields": updated_keys}

    if entry_id <= 0:
        raise ValueError("Database entry ID must be >= 1 (RMXP 1-indexed convention).")

    if table in ("switches", "variables"):
        proj_path = ir_dir / "ir_project.json"
        proj = _load_json(proj_path)
        arr: List[Any] = proj["system"]["fields"][table]
        while len(arr) <= entry_id:
            arr.append("")
        arr[entry_id] = str(fields.get("name", arr[entry_id]))
        _save_json(proj_path, proj)
        if snap is not None:
            _push_ir_history(ir_dir, f"set_db:{table}:{entry_id}", snap)
        return {"table": table, "id": entry_id, "name": arr[entry_id]}

    supported_tables = {
        "actors": ("actors.json", "RPG::Actor"),
        "classes": ("classes.json", "RPG::Class"),
        "skills": ("skills.json", "RPG::Skill"),
        "items": ("items.json", "RPG::Item"),
        "weapons": ("weapons.json", "RPG::Weapon"),
        "armors": ("armors.json", "RPG::Armor"),
        "enemies": ("enemies.json", "RPG::Enemy"),
        "troops": ("troops.json", "RPG::Troop"),
        "states": ("states.json", "RPG::State"),
        "tilesets": ("tilesets.json", "RPG::Tileset"),
    }
    if table not in supported_tables:
        raise ValueError(f"Unsupported database table: {table}")

    filename, class_name = supported_tables[table]
    db_path = ir_dir / "database" / filename
    entries: List[Any] = _load_json(db_path) if db_path.exists() else [None]

    while len(entries) <= entry_id:
        entries.append(None)

    if entries[entry_id] is None:
        template = next((item for item in entries[1:] if isinstance(item, dict)), None)
        if template is not None:
            new_obj = json.loads(json.dumps(template))
            new_obj["fields"]["id"] = entry_id
            if "name" in new_obj["fields"]:
                new_obj["fields"]["name"] = f"{table}_{entry_id}"
        else:
            default_fields: Dict[str, Any] = {"id": entry_id, "name": f"{table}_{entry_id}"}
            if table == "troops":
                default_fields["members"] = []
                default_fields["pages"] = []
            new_obj = {"__type__": "RubyObject", "class": class_name, "fields": default_fields}
        entries[entry_id] = new_obj

    target_fields = entries[entry_id]["fields"]
    updated_keys = []
    enemy_lookup = _build_enemy_name_lookup(ir_dir) if table == "troops" else None
    for key, raw_val in fields.items():
        if table == "troops" and key == "enemy_ids":
            target_fields["members"] = _build_troop_members(raw_val, is_enemy_ids=True)
            updated_keys.append("members")
        elif table == "troops" and key == "enemy_names":
            target_fields["members"] = _build_troop_members(raw_val, is_enemy_names=True, enemy_lookup=enemy_lookup)
            updated_keys.append("members")
        elif table == "troops" and key == "members":
            target_fields["members"] = _build_troop_members(raw_val, enemy_lookup=enemy_lookup)
            updated_keys.append("members")
        else:
            target_fields[key] = _coerce_value(raw_val)
            updated_keys.append(key)

    _save_json(db_path, entries)
    if snap is not None:
        _push_ir_history(ir_dir, f"set_db:{table}:{entry_id}", snap)
    return {"table": table, "id": entry_id, "updated_fields": updated_keys}


def _snapshot_ir_files(ir_dir: Path) -> Dict[Path, bytes]:
    """Captures raw bytes of project metadata, database, and map JSON files for atomic rollback."""
    snap: Dict[Path, bytes] = {}
    proj_file = ir_dir / "ir_project.json"
    if proj_file.exists():
        snap[proj_file] = proj_file.read_bytes()
    for folder_name in ("database", "maps"):
        folder = ir_dir / folder_name
        if folder.exists():
            for p in folder.glob("*.json"):
                snap[p] = p.read_bytes()
    return snap


def _restore_ir_files(ir_dir: Path, snapshot: Dict[Path, bytes]) -> None:
    """Restores snapshotted files and removes any newly created JSON files."""
    for folder_name in ("database", "maps"):
        folder = ir_dir / folder_name
        if folder.exists():
            for p in folder.glob("*.json"):
                if p not in snapshot:
                    p.unlink()
    proj_file = ir_dir / "ir_project.json"
    if proj_file.exists() and proj_file not in snapshot:
        proj_file.unlink()
    for path, raw in snapshot.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)


def _compute_ir_diff(ir_dir: Path, before: Dict[Path, bytes], after: Dict[Path, bytes]) -> Dict[str, List[str]]:
    """Computes relative added, modified, and removed IR files between two byte snapshots."""
    b_rel = {p.relative_to(ir_dir).as_posix(): raw for p, raw in before.items()}
    a_rel = {p.relative_to(ir_dir).as_posix(): raw for p, raw in after.items()}
    added = sorted(k for k in a_rel if k not in b_rel)
    removed = sorted(k for k in b_rel if k not in a_rel)
    modified = sorted(k for k in a_rel if k in b_rel and a_rel[k] != b_rel[k])
    return {"added": added, "modified": modified, "removed": removed}


def _push_ir_history(
    ir_dir: Path,
    label: str,
    snapshot: Dict[Path, bytes],
    max_entries: int = 30,
    clear_redo: bool = True,
) -> None:
    """Appends an IR file snapshot onto the bounded .ir_history ring buffer."""
    hist_file = ir_dir / ".ir_history" / "undo_stack.json"
    stack: List[Dict[str, Any]] = []
    if hist_file.exists():
        try:
            loaded = _load_json(hist_file)
            if isinstance(loaded, list):
                stack = loaded
        except Exception:
            stack = []
    encoded = {
        p.relative_to(ir_dir).as_posix(): base64.b64encode(raw).decode("ascii")
        for p, raw in snapshot.items()
    }
    stack.append({"label": label, "files": encoded})
    stack = stack[-max(1, int(max_entries)):]
    hist_file.parent.mkdir(parents=True, exist_ok=True)
    _save_json(hist_file, stack)
    if clear_redo:
        redo_file = ir_dir / ".ir_history" / "redo_stack.json"
        if redo_file.exists():
            redo_file.unlink(missing_ok=True)


def _push_redo_history(
    ir_dir: Path,
    label: str,
    snapshot: Dict[Path, bytes],
    max_entries: int = 10,
) -> None:
    redo_file = ir_dir / ".ir_history" / "redo_stack.json"
    stack: List[Dict[str, Any]] = []
    if redo_file.exists():
        try:
            loaded = _load_json(redo_file)
            if isinstance(loaded, list):
                stack = loaded
        except Exception:
            stack = []
    encoded = {
        p.relative_to(ir_dir).as_posix(): base64.b64encode(raw).decode("ascii")
        for p, raw in snapshot.items()
    }
    stack.append({"label": label, "files": encoded})
    stack = stack[-max(1, int(max_entries)):]
    redo_file.parent.mkdir(parents=True, exist_ok=True)
    _save_json(redo_file, stack)


def capture_undo_snapshot(ir_dir: Path) -> Dict[Path, bytes]:
    """Captures the current IR workspace files for explicit undo recording."""
    return _snapshot_ir_files(Path(ir_dir))


def push_undo_snapshot(
    ir_dir: Path,
    label: str,
    snapshot: Optional[Dict[Path, bytes]] = None,
    max_entries: int = 30,
) -> None:
    """Records an explicit undo frame onto .ir_history/undo_stack.json."""
    ir_path = Path(ir_dir)
    snap = snapshot if snapshot is not None else _snapshot_ir_files(ir_path)
    _push_ir_history(ir_path, label, snap, max_entries=max_entries, clear_redo=True)


def restore_undo_snapshot(ir_dir: Path, snapshot: Dict[Path, bytes]) -> None:
    """Restores a previously captured IR snapshot to disk cleanly (used when discarding edits)."""
    _restore_ir_files(Path(ir_dir), snapshot)


def undo_last_edit(ir_dir: Path) -> Dict[str, Any]:
    """Restores the most recent snapshotted IR state from .ir_history/undo_stack.json."""
    ir_dir = Path(ir_dir)
    hist_file = ir_dir / ".ir_history" / "undo_stack.json"
    if not hist_file.exists():
        return {"restored": False, "label": None, "remaining": 0}
    stack = _load_json(hist_file)
    if not isinstance(stack, list) or not stack:
        return {"restored": False, "label": None, "remaining": 0}
    cur_snap = _snapshot_ir_files(ir_dir)
    entry = stack.pop()
    label = str(entry.get("label", "edit"))
    snap: Dict[Path, bytes] = {}
    for rel_str, b64 in dict(entry.get("files", {})).items():
        snap[ir_dir / rel_str] = base64.b64decode(b64.encode("ascii"))
    _restore_ir_files(ir_dir, snap)
    _save_json(hist_file, stack)
    _push_redo_history(ir_dir, label, cur_snap)
    return {
        "restored": True,
        "label": label,
        "files_restored": len(snap),
        "remaining": len(stack),
    }


def redo_last_edit(ir_dir: Path) -> Dict[str, Any]:
    """Reapplies the most recently undone IR edit from .ir_history/redo_stack.json."""
    ir_dir = Path(ir_dir)
    redo_file = ir_dir / ".ir_history" / "redo_stack.json"
    if not redo_file.exists():
        return {"restored": False, "label": None, "remaining": 0}
    stack = _load_json(redo_file)
    if not isinstance(stack, list) or not stack:
        return {"restored": False, "label": None, "remaining": 0}
    cur_snap = _snapshot_ir_files(ir_dir)
    entry = stack.pop()
    label = str(entry.get("label", "edit"))
    snap: Dict[Path, bytes] = {}
    for rel_str, b64 in dict(entry.get("files", {})).items():
        snap[ir_dir / rel_str] = base64.b64decode(b64.encode("ascii"))
    _restore_ir_files(ir_dir, snap)
    _save_json(redo_file, stack)
    _push_ir_history(ir_dir, label, cur_snap, clear_redo=False)
    return {
        "restored": True,
        "label": label,
        "files_restored": len(snap),
        "remaining": len(stack),
    }


def list_maps(ir_dir: Path) -> List[Dict[str, Any]]:
    """Lists maps defined under <ir_dir>/maps with dimensions and event counts."""
    ir_dir = Path(ir_dir)
    out: List[Dict[str, Any]] = []
    for mf in sorted((ir_dir / "maps").glob("map_*.json")):
        m = _load_json(mf)
        out.append({
            "map_id": int(m.get("map_id", 0)),
            "name": str(m.get("name", "")),
            "width": int(m.get("width", 0)),
            "height": int(m.get("height", 0)),
            "events": len(m.get("events", [])),
        })
    return out


def delete_map(ir_dir: Path, map_id: int) -> Dict[str, Any]:
    """Deletes a non-start map JSON file and removes its map_infos entry with undo support."""
    ir_dir = Path(ir_dir)
    proj_path = ir_dir / "ir_project.json"
    proj = _load_json(proj_path)
    start_mid = int(proj.get("system", {}).get("fields", {}).get("start_map_id", 1))
    if int(map_id) == start_mid:
        raise ValueError(f"Cannot delete start map {map_id}.")
    mpath = ir_dir / "maps" / f"map_{int(map_id):03d}.json"
    if not mpath.is_file():
        return {"deleted": False, "map_id": int(map_id)}
    snap = _snapshot_ir_files(ir_dir)
    mpath.unlink()
    _remove_map_info(proj, int(map_id))
    _save_json(proj_path, proj)
    _push_ir_history(ir_dir, f"delete_map:{map_id}", snap)
    return {"deleted": True, "map_id": int(map_id)}


def list_events(ir_dir: Path, map_id: int) -> List[Dict[str, Any]]:
    """Lists events on a map with coordinates and page counts."""
    ir_dir = Path(ir_dir)
    m = _load_json(ir_dir / "maps" / f"map_{int(map_id):03d}.json")
    return [
        {
            "id": int(ev.get("id", 0)),
            "name": str(ev.get("name", "")),
            "x": int(ev.get("x", 0)),
            "y": int(ev.get("y", 0)),
            "pages": len(ev.get("pages", [])),
        }
        for ev in m.get("events", [])
    ]


def delete_event(ir_dir: Path, map_id: int, event_id: int) -> Dict[str, Any]:
    """Removes an event by ID from the specified map with undo support."""
    ir_dir = Path(ir_dir)
    mpath = ir_dir / "maps" / f"map_{int(map_id):03d}.json"
    m = _load_json(mpath)
    evs = list(m.get("events", []))
    kept = [ev for ev in evs if int(ev.get("id", -1)) != int(event_id)]
    if len(kept) == len(evs):
        return {"deleted": False, "map_id": int(map_id), "event_id": int(event_id)}
    snap = _snapshot_ir_files(ir_dir)
    m["events"] = kept
    _save_json(mpath, m)
    _push_ir_history(ir_dir, f"delete_event:{map_id}:{event_id}", snap)
    return {"deleted": True, "map_id": int(map_id), "event_id": int(event_id)}


def update_map_properties(
    ir_dir: Path,
    map_id: int,
    props: Dict[str, Any],
    record_undo: bool = True,
) -> Dict[str, Any]:
    """Updates map metadata (name, dimensions, tileset, audio, parallax, encounters) with undo support."""
    ir_dir = Path(ir_dir)
    mpath = ir_dir / "maps" / f"map_{int(map_id):03d}.json"
    map_ir = _load_json(mpath)
    proj_path = ir_dir / "ir_project.json"
    proj = _load_json(proj_path)
    snap = _snapshot_ir_files(ir_dir) if record_undo else None

    old_w, old_h = int(map_ir.get("width", 20)), int(map_ir.get("height", 15))
    new_w = max(1, int(props.get("width", old_w)))
    new_h = max(1, int(props.get("height", old_h)))
    if new_w != old_w or new_h != old_h:
        layers = map_ir.setdefault("layers", {})
        for idx_l in range(3):
            key = f"layer_{idx_l}"
            old_grid = layers.get(key, [[0] * old_w for _ in range(old_h)])
            fill_v = 384 if idx_l == 0 else 0
            new_grid: List[List[int]] = []
            for y in range(new_h):
                row: List[int] = []
                for x in range(new_w):
                    if y < len(old_grid) and x < len(old_grid[y]):
                        row.append(int(old_grid[y][x]))
                    else:
                        row.append(fill_v)
                new_grid.append(row)
            layers[key] = new_grid
        map_ir["width"] = new_w
        map_ir["height"] = new_h

    for field in (
        "name",
        "tileset_id",
        "autoplay_bgm",
        "bgm",
        "autoplay_bgs",
        "bgs",
        "parallax_name",
        "parallax_loop_x",
        "parallax_loop_y",
        "parallax_sx",
        "parallax_sy",
        "encounter_list",
        "encounter_step",
    ):
        if field in props:
            map_ir[field] = props[field]

    _save_json(mpath, map_ir)
    if "name" in props:
        _set_map_info_name(proj, int(map_id), str(props["name"]))
        _save_json(proj_path, proj)

    if snap is not None:
        _push_ir_history(ir_dir, f"update_map:{map_id}", snap)
    return {
        "map_id": int(map_id),
        "name": map_ir.get("name", ""),
        "width": int(map_ir["width"]),
        "height": int(map_ir["height"]),
        "tileset_id": int(map_ir.get("tileset_id", 1)),
    }


def save_event(
    ir_dir: Path,
    map_id: int,
    event_data: Dict[str, Any],
    record_undo: bool = True,
) -> Dict[str, Any]:
    """Updates an existing event by ID or appends a new normalized event on a map with undo support."""
    ir_dir = Path(ir_dir)
    mpath = ir_dir / "maps" / f"map_{int(map_id):03d}.json"
    map_ir = _load_json(mpath)
    w, h = int(map_ir["width"]), int(map_ir["height"])
    ex = int(event_data.get("x", 0))
    ey = int(event_data.get("y", 0))
    if ex < 0 or ey < 0 or ex >= w or ey >= h:
        raise ValueError(f"Event position ({ex}, {ey}) out of bounds for map {map_id} ({w}x{h}).")

    evs: List[Dict[str, Any]] = list(map_ir.get("events", []))
    raw_id = event_data.get("id")
    existing_ids = [int(ev["id"]) for ev in evs if "id" in ev]
    ev_id = int(raw_id) if raw_id is not None and int(raw_id) > 0 else ((max(existing_ids) + 1) if existing_ids else 1)

    pages = list(event_data.get("pages", []))
    if not pages:
        pages = [_build_ir_page({})]

    normalized_ev = {
        "id": ev_id,
        "name": str(event_data.get("name", f"EV{ev_id:03d}")),
        "x": ex,
        "y": ey,
        "pages": pages,
    }

    snap = _snapshot_ir_files(ir_dir) if record_undo else None
    replaced = False
    for idx, cur in enumerate(evs):
        if int(cur.get("id", -1)) == ev_id:
            evs[idx] = normalized_ev
            replaced = True
            break
    if not replaced:
        evs.append(normalized_ev)
    map_ir["events"] = evs
    _save_json(mpath, map_ir)
    if snap is not None:
        _push_ir_history(ir_dir, f"save_event:{map_id}:{ev_id}", snap)
    return {"map_id": int(map_id), "event_id": ev_id, "replaced": replaced, "pages": len(pages)}


def get_db_entry(ir_dir: Path, table: str, entry_id: int) -> Any:
    """Returns a single database entry (or switch/variable name/system) from IR storage."""
    ir_dir = Path(ir_dir)
    if table == "system":
        proj = _load_json(ir_dir / "ir_project.json")
        return {"table": "system", "id": 1, "fields": proj.get("system", {}).get("fields", {})}
    if table in ("switches", "variables"):
        proj = _load_json(ir_dir / "ir_project.json")
        arr = proj["system"]["fields"].get(table, [])
        return {"table": table, "id": int(entry_id), "name": arr[int(entry_id)] if 0 <= int(entry_id) < len(arr) else None}
    db_path = ir_dir / "database" / f"{table}.json"
    if not db_path.is_file():
        return None
    entries = _load_json(db_path)
    if 0 <= int(entry_id) < len(entries):
        return entries[int(entry_id)]
    return None


def delete_db_entry(ir_dir: Path, table: str, entry_id: int) -> Dict[str, Any]:
    """Clears a database entry slot (resets to None or empty string) with undo support."""
    ir_dir = Path(ir_dir)
    if int(entry_id) <= 0:
        raise ValueError("Database entry ID must be >= 1.")
    snap = _snapshot_ir_files(ir_dir)
    if table in ("switches", "variables"):
        proj_path = ir_dir / "ir_project.json"
        proj = _load_json(proj_path)
        arr = proj["system"]["fields"][table]
        if int(entry_id) >= len(arr):
            return {"deleted": False, "table": table, "id": int(entry_id)}
        arr[int(entry_id)] = ""
        _save_json(proj_path, proj)
        _push_ir_history(ir_dir, f"delete_db:{table}:{entry_id}", snap)
        return {"deleted": True, "table": table, "id": int(entry_id)}
    db_path = ir_dir / "database" / f"{table}.json"
    if not db_path.is_file():
        return {"deleted": False, "table": table, "id": int(entry_id)}
    entries = _load_json(db_path)
    if int(entry_id) >= len(entries) or entries[int(entry_id)] is None:
        return {"deleted": False, "table": table, "id": int(entry_id)}
    entries[int(entry_id)] = None
    _save_json(db_path, entries)
    _push_ir_history(ir_dir, f"delete_db:{table}:{entry_id}", snap)
    return {"deleted": True, "table": table, "id": int(entry_id)}


def _register_unique_name(table: Dict[str, int], duplicates: Set[str], name: str, item_id: int) -> None:
    """Tracks name-to-ID entries while recording any duplicate names for ambiguity detection."""
    if name in table and table[name] != item_id:
        duplicates.add(name)
    else:
        table[name] = item_id


def _load_db_name_table(
    ir_dir: Path,
    filename: str,
    dup_set: Set[str],
) -> Dict[str, int]:
    """Populates a name-to-ID mapping from a 1-indexed database IR file."""
    table: Dict[str, int] = {}
    db_path = ir_dir / "database" / filename
    if not db_path.exists():
        return table
    for entry in _load_json(db_path):
        if isinstance(entry, dict) and "fields" in entry:
            flds = entry["fields"]
            if flds.get("name"):
                _register_unique_name(table, dup_set, str(flds["name"]), int(flds["id"]))
    return table


def _build_blueprint_lookups(
    ir_dir: Path,
    map_name_to_id: Dict[str, int],
    map_duplicates: Optional[Set[str]] = None,
) -> Dict[str, Any]:
    """Builds name-to-ID lookup tables (and duplicate sets) for blueprint entities."""
    proj = _load_json(ir_dir / "ir_project.json")
    sys_fields = proj.get("system", {}).get("fields", {})
    dup_map: Dict[str, Set[str]] = {
        "maps": set(map_duplicates or set()),
        "switches": set(),
        "variables": set(),
        "actors": set(),
        "items": set(),
        "weapons": set(),
        "armors": set(),
        "skills": set(),
        "troops": set(),
        "common_events": set(),
    }

    switches: Dict[str, int] = {}
    for idx, name in enumerate(sys_fields.get("switches", [])):
        if isinstance(name, str) and name and idx > 0:
            _register_unique_name(switches, dup_map["switches"], name, idx)

    variables: Dict[str, int] = {}
    for idx, name in enumerate(sys_fields.get("variables", [])):
        if isinstance(name, str) and name and idx > 0:
            _register_unique_name(variables, dup_map["variables"], name, idx)

    common_events: Dict[str, int] = {}
    ce_path = ir_dir / "database" / "common_events.json"
    if ce_path.exists():
        for entry in _load_json(ce_path):
            if isinstance(entry, dict) and entry.get("name") and "id" in entry:
                _register_unique_name(common_events, dup_map["common_events"], str(entry["name"]), int(entry["id"]))

    return {
        "maps": map_name_to_id,
        "switches": switches,
        "variables": variables,
        "actors": _load_db_name_table(ir_dir, "actors.json", dup_map["actors"]),
        "items": _load_db_name_table(ir_dir, "items.json", dup_map["items"]),
        "weapons": _load_db_name_table(ir_dir, "weapons.json", dup_map["weapons"]),
        "armors": _load_db_name_table(ir_dir, "armors.json", dup_map["armors"]),
        "skills": _load_db_name_table(ir_dir, "skills.json", dup_map["skills"]),
        "troops": _load_db_name_table(ir_dir, "troops.json", dup_map["troops"]),
        "common_events": common_events,
        "_duplicates": dup_map,
    }


_CATEGORY_SINGULAR = {
    "maps": "map",
    "switches": "switch",
    "variables": "variable",
    "actors": "actor",
    "items": "item",
    "weapons": "weapon",
    "armors": "armor",
    "skills": "skill",
    "troops": "troop",
    "common_events": "common_event",
}


def _lookup_named_id(lookups: Dict[str, Any], category: str, name: str, context: str) -> int:
    label = _CATEGORY_SINGULAR.get(category, category)
    dups = lookups.get("_duplicates", {}).get(category, set())
    if name in dups:
        raise ValueError(f"Ambiguous duplicate {label} name in {context}: {name!r}")
    table = lookups.get(category, {})
    if name not in table:
        raise ValueError(f"Unknown {label} name in {context}: {name!r}")
    return table[name]


# Mapping: op -> (id_field, name_field, lookup_category, mirror_to_end_id)
_CMD_SIMPLE_NAME_REFS: Dict[str, Tuple[str, str, str, bool]] = {
    "transfer_player": ("map_id", "map_name", "maps", False),
    "if_switch": ("switch_id", "switch_name", "switches", False),
    "set_switch": ("start_id", "switch_name", "switches", True),
    "if_variable": ("variable_id", "variable_name", "variables", False),
    "set_variable": ("start_id", "variable_name", "variables", True),
    "if_item": ("item_id", "item_name", "items", False),
    "change_item": ("item_id", "item_name", "items", False),
    "change_weapon": ("weapon_id", "weapon_name", "weapons", False),
    "change_armor": ("armor_id", "armor_name", "armors", False),
    "change_skill": ("skill_id", "skill_name", "skills", False),
    "change_party": ("actor_id", "actor_name", "actors", False),
    "battle": ("troop_id", "troop_name", "troops", False),
    "common_event": ("common_event_id", "common_event_name", "common_events", False),
}


def _resolve_command_refs(
    commands: Optional[List[Dict[str, Any]]],
    lookups: Dict[str, Any],
) -> Optional[List[Dict[str, Any]]]:
    """Resolves named map, switch, variable, actor, item, gear, skill, troop, and common_event refs."""
    if commands is None:
        return None
    resolved: List[Dict[str, Any]] = []
    for raw_cmd in commands:
        cmd = dict(raw_cmd)
        op = cmd.get("op")
        if op in ("recover_all", "change_skill", "change_equip") and cmd.get("actor_id") is None and "actor_name" in cmd:
            cmd["actor_id"] = _lookup_named_id(lookups, "actors", str(cmd.pop("actor_name")), str(op))
        spec = _CMD_SIMPLE_NAME_REFS.get(str(op))
        if spec is not None:
            id_key, name_key, cat, mirror_end = spec
            if cmd.get(id_key) is None and name_key in cmd:
                val = _lookup_named_id(lookups, cat, str(cmd.pop(name_key)), str(op))
                cmd[id_key] = val
                if mirror_end:
                    cmd.setdefault("end_id", val)
        elif op == "change_equip" and cmd.get("equip_id") is None:
            eq_type = int(cmd.get("equip_type", 0))
            if eq_type == 0 and "weapon_name" in cmd:
                cmd["equip_id"] = _lookup_named_id(lookups, "weapons", str(cmd.pop("weapon_name")), "change_equip")
            elif eq_type in (1, 2, 3, 4) and "armor_name" in cmd:
                cmd["equip_id"] = _lookup_named_id(lookups, "armors", str(cmd.pop("armor_name")), "change_equip")
            elif "equip_name" in cmd:
                cat = "weapons" if eq_type == 0 else "armors"
                cmd["equip_id"] = _lookup_named_id(lookups, cat, str(cmd.pop("equip_name")), "change_equip")
        elif op == "shop":
            type_to_cat = {"item": "items", "weapon": "weapons", "armor": "armors"}
            resolved_goods: List[Dict[str, Any]] = []
            for raw_g in cmd.get("goods", []):
                g = dict(raw_g)
                if g.get("id") is None:
                    for name_key, g_type in (("item_name", "item"), ("weapon_name", "weapon"), ("armor_name", "armor")):
                        if name_key in g:
                            g["type"] = g_type
                            g["id"] = _lookup_named_id(lookups, type_to_cat[g_type], str(g.pop(name_key)), "shop")
                            break
                    else:
                        if "name" in g:
                            g_type = str(g.get("type", "item"))
                            cat = type_to_cat.get(g_type, "items")
                            g["id"] = _lookup_named_id(lookups, cat, str(g.pop("name")), "shop")
                g.setdefault("type", "item")
                resolved_goods.append(g)
            cmd["goods"] = resolved_goods
        resolved.append(cmd)
    return resolved


def _resolve_page_refs(page_spec: Dict[str, Any], lookups: Dict[str, Dict[str, int]]) -> Dict[str, Any]:
    """Resolves named conditions and commands inside a blueprint event page."""
    out = dict(page_spec)
    cond = dict(out.get("condition", {}))
    if cond.get("switch1_id") is None and "switch1_name" in cond:
        cond["switch1_id"] = _lookup_named_id(lookups, "switches", str(cond.pop("switch1_name")), "page.condition.switch1")
    if cond.get("switch2_id") is None and "switch2_name" in cond:
        cond["switch2_id"] = _lookup_named_id(lookups, "switches", str(cond.pop("switch2_name")), "page.condition.switch2")
    if cond.get("variable_id") is None and "variable_name" in cond:
        cond["variable_id"] = _lookup_named_id(lookups, "variables", str(cond.pop("variable_name")), "page.condition.variable")
    if cond:
        out["condition"] = cond
    if "commands" in out:
        out["commands"] = _resolve_command_refs(out.get("commands"), lookups)
    return out


def _find_event_index(events: List[Dict[str, Any]], selector: Any, map_id: int) -> int:
    """Finds the unique index of an existing event by integer ID or string name."""
    if isinstance(selector, int) or (isinstance(selector, dict) and "id" in selector):
        target_id = int(selector["id"] if isinstance(selector, dict) else selector)
        for idx, ev in enumerate(events):
            if int(ev["id"]) == target_id:
                return idx
        raise ValueError(f"Event ID {target_id} not found on map {map_id}.")
    target_name = str(selector["name"] if isinstance(selector, dict) else selector)
    matches = [idx for idx, ev in enumerate(events) if str(ev.get("name")) == target_name]
    if not matches:
        raise ValueError(f"Event name {target_name!r} not found on map {map_id}.")
    if len(matches) > 1:
        raise ValueError(f"Ambiguous duplicate event name {target_name!r} on map {map_id}.")
    return matches[0]


def _patch_single_event(
    ev: Dict[str, Any],
    pe: Dict[str, Any],
    map_id: int,
    w: int,
    h: int,
    active_lookups: Dict[str, Any],
) -> None:
    """Mutates one event IR dictionary in place according to a patch specification."""
    if "new_name" in pe:
        ev["name"] = str(pe["new_name"])
    if "x" in pe:
        nx = int(pe["x"])
        if nx < 0 or nx >= w:
            raise ValueError(f"Patched event x={nx} out of bounds on map {map_id}.")
        ev["x"] = nx
    if "y" in pe:
        ny = int(pe["y"])
        if ny < 0 or ny >= h:
            raise ValueError(f"Patched event y={ny} out of bounds on map {map_id}.")
        ev["y"] = ny
    if pe.get("pages") is not None:
        resolved_pages = [_resolve_page_refs(ps, active_lookups) for ps in pe["pages"]]
        ev["pages"] = [_build_ir_page(ps) for ps in resolved_pages]
        return
    if not any(k in pe for k in ("commands", "trigger", "graphic", "through")):
        return
    base_page = dict(ev["pages"][0]) if ev.get("pages") else _build_ir_page({})
    if "trigger" in pe:
        trig = str(pe["trigger"])
        trig_code = TRIGGER_NAME_TO_INT.get(trig)
        if trig_code is None:
            raise ValueError(f"Unknown trigger '{trig}'")
        base_page["trigger"] = trig
        base_page["trigger_code"] = trig_code
    if "through" in pe:
        base_page["through"] = bool(pe["through"])
    if "graphic" in pe:
        base_page.setdefault("graphic", {}).setdefault("fields", {})["character_name"] = str(pe["graphic"])
    if "commands" in pe:
        cmds = _resolve_command_refs(pe["commands"], active_lookups) or []
        if not cmds or cmds[-1].get("op") != "end":
            cmds.append({"op": "end", "indent": 0})
        base_page["commands"] = cmds
    if ev.get("pages"):
        ev["pages"][0] = base_page
    else:
        ev["pages"] = [base_page]


def _apply_map_patches(
    ir_dir: Path,
    map_id: int,
    spec: Dict[str, Any],
    lookups: Optional[Dict[str, Any]] = None,
) -> Tuple[int, int, int, int]:
    """Applies tile paints, event additions, event patches, and event removals to a map."""
    painted_total = 0
    events_added = 0
    events_patched = 0
    events_removed = 0
    active_lookups = lookups or {"maps": {}, "switches": {}, "variables": {}, "items": {}, "troops": {}}
    for p_spec in spec.get("paint", []):
        rect = tuple(p_spec["rect"]) if p_spec.get("rect") is not None else None
        pts = [tuple(pt) for pt in p_spec["points"]] if p_spec.get("points") is not None else None
        res = paint_map(
            ir_dir,
            map_id=map_id,
            layer=int(p_spec.get("layer", 0)),
            tile_id=int(p_spec["tile_id"]),
            rect=rect,  # type: ignore[arg-type]
            points=pts,  # type: ignore[arg-type]
            record_undo=False,
        )
        painted_total += int(res["tiles_painted"])

    patch_specs = spec.get("patch_events", [])
    remove_specs = spec.get("remove_events", [])
    if patch_specs or remove_specs:
        map_path = ir_dir / "maps" / f"map_{map_id:03d}.json"
        map_ir = _load_json(map_path)
        w, h = int(map_ir["width"]), int(map_ir["height"])
        ev_list: List[Dict[str, Any]] = map_ir.setdefault("events", [])
        for pe in patch_specs:
            _patch_single_event(ev_list[_find_event_index(ev_list, pe, map_id)], pe, map_id, w, h, active_lookups)
            events_patched += 1
        for rem in remove_specs:
            ev_list.pop(_find_event_index(ev_list, rem, map_id))
            events_removed += 1
        _save_json(map_path, map_ir)

    for e_spec in spec.get("events", []):
        resolved_pages = (
            [_resolve_page_refs(ps, active_lookups) for ps in e_spec["pages"]]
            if e_spec.get("pages") is not None
            else None
        )
        cmds = _resolve_command_refs(e_spec.get("commands"), active_lookups)
        add_event(
            ir_dir,
            map_id=map_id,
            name=str(e_spec["name"]),
            x=int(e_spec["x"]),
            y=int(e_spec["y"]),
            trigger=str(e_spec.get("trigger", "action_button")),
            graphic=str(e_spec.get("graphic", "")),
            through=bool(e_spec.get("through", False)),
            commands=cmds,
            pages=resolved_pages,
            record_undo=False,
        )
        events_added += 1
    return painted_total, events_added, events_patched, events_removed


_CE_TRIGGER_MAP: Dict[str, int] = {"none": 0, "autorun": 1, "parallel": 2}


def _build_common_event_entry(
    cid: int,
    name: str,
    trigger: str | int = 0,
    switch_id: int = 1,
    commands: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Normalizes and builds a single RMXP common_events entry."""
    if cid <= 0:
        raise ValueError("CommonEvent ID must be >= 1.")
    if isinstance(trigger, str) and trigger.lower() in _CE_TRIGGER_MAP:
        trig = _CE_TRIGGER_MAP[trigger.lower()]
    else:
        trig = int(trigger)
    if trig not in (0, 1, 2):
        raise ValueError(f"Invalid common event trigger: {trigger}")
    cmds = list(commands) if commands else []
    if not cmds or cmds[-1].get("op") != "end":
        cmds.append({"op": "end", "indent": 0})
    return {
        "id": cid,
        "name": str(name),
        "trigger": trig,
        "switch_id": int(switch_id),
        "commands": cmds,
    }


def set_common_event(
    ir_dir: Path,
    entry_id: int,
    name: str,
    trigger: str | int = "none",
    switch_id: int = 1,
    commands: Optional[List[Dict[str, Any]]] = None,
    record_undo: bool = True,
) -> Dict[str, Any]:
    """Creates or updates a common event entry atomically in database/common_events.json."""
    ir_dir = Path(ir_dir)
    ce_path = ir_dir / "database" / "common_events.json"
    entries: List[Any] = _load_json(ce_path) if ce_path.exists() else [None]
    cid = int(entry_id)
    if cid <= 0:
        raise ValueError("CommonEvent ID must be >= 1.")
    snap = _snapshot_ir_files(ir_dir) if record_undo else None
    while len(entries) <= cid:
        entries.append(None)
    entry = _build_common_event_entry(cid, name, trigger, switch_id, commands)
    entries[cid] = entry
    _save_json(ce_path, entries)
    if snap is not None:
        _push_ir_history(ir_dir, f"set_common_event:{cid}", snap)
    return {
        "status": "updated",
        "id": cid,
        "name": entry["name"],
        "trigger": entry["trigger"],
        "switch_id": entry["switch_id"],
        "command_count": len(entry["commands"]),
    }


def list_common_events(ir_dir: Path) -> List[Dict[str, Any]]:
    """Returns a concise summary list of all defined common events."""
    ce_path = ir_dir / "database" / "common_events.json"
    if not ce_path.exists():
        return []
    entries = _load_json(ce_path)
    rev_trigger = {0: "none", 1: "autorun", 2: "parallel"}
    result: List[Dict[str, Any]] = []
    for ce in entries:
        if not isinstance(ce, dict):
            continue
        trig = int(ce.get("trigger", 0))
        result.append(
            {
                "id": int(ce.get("id", 0)),
                "name": str(ce.get("name", "")),
                "trigger": rev_trigger.get(trig, str(trig)),
                "switch_id": int(ce.get("switch_id", 1)),
                "command_count": len(ce.get("commands", [])),
            }
        )
    return result


def _apply_common_events_blueprint(
    ir_dir: Path,
    specs: List[Dict[str, Any]],
    lookups: Dict[str, Any],
) -> int:
    """Applies declarative common_events entries and registers their names in lookups."""
    if not specs:
        return 0
    ce_path = ir_dir / "database" / "common_events.json"
    entries: List[Any] = _load_json(ce_path) if ce_path.exists() else [None]
    dups = lookups.setdefault("_duplicates", {}).setdefault("common_events", set())
    ce_table = lookups.setdefault("common_events", {})

    pending_entries: List[Tuple[int, Dict[str, Any]]] = []
    for ce_spec in specs:
        raw_id = ce_spec.get("id")
        cid = int(raw_id) if raw_id is not None else len(entries)
        if cid <= 0:
            raise ValueError("CommonEvent ID must be >= 1.")
        while len(entries) <= cid:
            entries.append(None)
        ce_name = str(ce_spec.get("name", f"CommonEvent_{cid}"))
        _register_unique_name(ce_table, dups, ce_name, cid)
        pending_entries.append((cid, ce_spec))

    for cid, ce_spec in pending_entries:
        if ce_spec.get("switch_id") is not None:
            sw_id = int(ce_spec["switch_id"])
        elif "switch_name" in ce_spec:
            sw_id = _lookup_named_id(lookups, "switches", str(ce_spec["switch_name"]), "common_event.switch")
        else:
            sw_id = 1
        cmds = _resolve_command_refs(ce_spec.get("commands", []), lookups) or []
        entries[cid] = _build_common_event_entry(
            cid=cid,
            name=str(ce_spec.get("name", f"CommonEvent_{cid}")),
            trigger=ce_spec.get("trigger", 0),
            switch_id=sw_id,
            commands=cmds,
        )

    _save_json(ce_path, entries)
    return len(pending_entries)


def apply_blueprint(
    ir_dir: Path,
    blueprint: Dict[str, Any],
    validate: bool = True,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Applies a declarative JSON blueprint atomically, with optional dry-run diff preview and undo history."""
    ir_dir = Path(ir_dir)
    snapshot = _snapshot_ir_files(ir_dir)
    created_maps: List[Dict[str, Any]] = []
    patched_maps: List[int] = []
    db_updates = 0
    ce_updates = 0
    tiles_painted = 0
    events_added = 0
    events_patched = 0
    events_removed = 0

    try:
        db_specs = list(blueprint.get("db", []))
        # Apply non-troop database entries first so troops can reference newly defined enemies by name
        db_specs.sort(key=lambda item: 1 if str(item.get("table")) == "troops" else 0)
        for db_spec in db_specs:
            set_db_entry(
                ir_dir,
                table=str(db_spec["table"]),
                entry_id=int(db_spec["id"]),
                fields=dict(db_spec.get("fields", {})),
                record_undo=False,
            )
            db_updates += 1

        proj_path = ir_dir / "ir_project.json"
        name_to_id: Dict[str, int] = {}
        map_dups: Set[str] = set()
        maps_dir = ir_dir / "maps"
        if maps_dir.exists():
            for mf in sorted(maps_dir.glob("map_*.json")):
                m_obj = _load_json(mf)
                if "name" in m_obj and "map_id" in m_obj:
                    _register_unique_name(name_to_id, map_dups, str(m_obj["name"]), int(m_obj["map_id"]))

        pending_new_maps: List[Tuple[int, Dict[str, Any]]] = []
        for m_spec in blueprint.get("maps", []):
            m_name = str(m_spec["name"])
            created = create_map(
                ir_dir,
                name=m_name,
                width=int(m_spec.get("width", 20)),
                height=int(m_spec.get("height", 15)),
                tileset_id=int(m_spec.get("tileset_id", 1)),
                fill_tile=int(m_spec.get("fill_tile", 384)),
                record_undo=False,
            )
            mid = int(created["map_id"])
            _register_unique_name(name_to_id, map_dups, m_name, mid)
            created_maps.append({"map_id": mid, "name": m_name})
            pending_new_maps.append((mid, m_spec))

        for pm_spec in blueprint.get("patch_maps", []):
            mid = int(pm_spec["map_id"])
            if "name" in pm_spec:
                new_name = str(pm_spec["name"])
                map_path = ir_dir / "maps" / f"map_{mid:03d}.json"
                map_ir = _load_json(map_path)
                map_ir["name"] = new_name
                _save_json(map_path, map_ir)
                proj = _load_json(proj_path)
                if _set_map_info_name(proj, mid, new_name):
                    _save_json(proj_path, proj)
                _register_unique_name(name_to_id, map_dups, new_name, mid)

        lookups = _build_blueprint_lookups(ir_dir, name_to_id, map_duplicates=map_dups)
        ce_updates = _apply_common_events_blueprint(ir_dir, blueprint.get("common_events", []), lookups)

        for mid, m_spec in pending_new_maps:
            p_cnt, e_cnt, ep_cnt, er_cnt = _apply_map_patches(ir_dir, mid, m_spec, lookups=lookups)
            tiles_painted += p_cnt
            events_added += e_cnt
            events_patched += ep_cnt
            events_removed += er_cnt

        for pm_spec in blueprint.get("patch_maps", []):
            mid = int(pm_spec["map_id"])
            p_cnt, e_cnt, ep_cnt, er_cnt = _apply_map_patches(ir_dir, mid, pm_spec, lookups=lookups)
            tiles_painted += p_cnt
            events_added += e_cnt
            events_patched += ep_cnt
            events_removed += er_cnt
            patched_maps.append(mid)

        bf_spec = blueprint.get("battle_formula")
        if bf_spec is not None:
            set_battle_formula(ir_dir, preset=None, overrides=None, replace_config=bf_spec, record_undo=False)

        start_spec = blueprint.get("start")
        if start_spec is not None:
            proj = _load_json(proj_path)
            sys_fields = proj["system"]["fields"]
            if "map_id" in start_spec:
                sys_fields["start_map_id"] = int(start_spec["map_id"])
            elif "map_name" in start_spec:
                sys_fields["start_map_id"] = _lookup_named_id(
                    lookups, "maps", str(start_spec["map_name"]), "start.map_name"
                )
            if "x" in start_spec:
                sys_fields["start_x"] = int(start_spec["x"])
            if "y" in start_spec:
                sys_fields["start_y"] = int(start_spec["y"])
            _save_json(proj_path, proj)

        validation_stats: Dict[str, Any] = {}
        if validate:
            val_res = validate_ir_project(ir_dir)
            if not val_res.passed:
                raise ValueError("Blueprint validation failed: " + "; ".join(val_res.errors))
            validation_stats = val_res.stats

        after_snap = _snapshot_ir_files(ir_dir)
        diff = _compute_ir_diff(ir_dir, snapshot, after_snap)
        if dry_run:
            _restore_ir_files(ir_dir, snapshot)
        else:
            _push_ir_history(ir_dir, "apply_blueprint", snapshot)

        return {
            "db_updates": db_updates,
            "common_events_updated": ce_updates,
            "created_maps": created_maps,
            "patched_maps": patched_maps,
            "tiles_painted": tiles_painted,
            "events_added": events_added,
            "events_patched": events_patched,
            "events_removed": events_removed,
            "validated": validate,
            "dry_run": dry_run,
            "diff": diff,
            "validation_stats": validation_stats,
        }
    except Exception:
        _restore_ir_files(ir_dir, snapshot)
        raise


def inspect_ir_project(ir_dir: Path) -> Dict[str, Any]:
    """Returns a structured authoring summary of maps, events, and RMXP database tables."""
    ir_dir = Path(ir_dir)
    proj = _load_json(ir_dir / "ir_project.json")
    sys_fields = proj.get("system", {}).get("fields", {})
    maps_summary: List[Dict[str, Any]] = []
    events_total = 0
    for mf in sorted((ir_dir / "maps").glob("map_*.json")):
        m = _load_json(mf)
        ev_cnt = len(m.get("events", []))
        events_total += ev_cnt
        maps_summary.append(
            {
                "id": int(m.get("map_id", 0)),
                "name": str(m.get("name", "")),
                "width": int(m.get("width", 0)),
                "height": int(m.get("height", 0)),
                "events": ev_cnt,
            }
        )
    db_dir = ir_dir / "database"
    counts: Dict[str, int] = {}
    for tbl in ("actors", "classes", "skills", "items", "weapons", "armors", "enemies", "troops", "states", "common_events"):
        tpath = db_dir / f"{tbl}.json"
        if tpath.is_file():
            entries = _load_json(tpath)
            counts[tbl] = sum(1 for row in entries if isinstance(row, dict)) if isinstance(entries, list) else 0
        else:
            counts[tbl] = 0
    switches = [s for s in sys_fields.get("switches", []) if isinstance(s, str) and s.strip()]
    variables = [v for v in sys_fields.get("variables", []) if isinstance(v, str) and v.strip()]
    return {
        "title": str(proj.get("rxproj_header", "")),
        "start": {
            "map_id": int(sys_fields.get("start_map_id", 1)),
            "x": int(sys_fields.get("start_x", 0)),
            "y": int(sys_fields.get("start_y", 0)),
        },
        "maps": maps_summary,
        "events_total": events_total,
        "switches_named": len(switches),
        "variables_named": len(variables),
        **counts,
    }


def export_release_bundle(
    ir_dir: Path,
    out_dir: Path,
    use_rgssad: bool = False,
    lang: Optional[str] = None,
    launcher: bool = False,
    build_exe: bool = False,
    strict_assets: bool = False,
    rgssad_key: Optional[int] = None,
    rgssad_version: int = 3,
    icon_path: Optional[Path] = None,
    create_zip: bool = False,
    strict_save: bool = False,
) -> Dict[str, Any]:
    """Validates an IR project and exports a clean commercial release bundle with SHA-256 manifest and optional launchers/EXE."""
    from .i18n import apply_i18n_locale

    if build_exe:
        launcher = True
    rg_ver = int(rgssad_version)
    if rg_ver not in (1, 2, 3):
        raise ValueError(f"Unsupported RGSSAD version: {rgssad_version}")
    arch_ext = {1: "Game.rgssad", 2: "Game.rgss2a", 3: "Game.rgss3a"}[rg_ver]
    ir_dir = Path(ir_dir)
    out_dir = Path(out_dir)
    eff_rgssad_key = DEFAULT_RGSSAD_KEY if rgssad_key is None else (int(rgssad_key) & 0xFFFFFFFF)
    val_res = validate_ir_project(ir_dir)
    if not val_res.passed:
        raise ValueError("Release validation failed: " + "; ".join(val_res.errors))
    asset_audit = validate_project_assets(ir_dir)
    if strict_assets and not asset_audit["passed"]:
        missing_all = asset_audit["missing_graphics"] + asset_audit["missing_audio"]
        raise ValueError("Release strict asset check failed: " + ", ".join(missing_all[:8]))

    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    asset_roots = [ir_dir / "assets", ir_dir, ir_dir.parent / "rmxp", ir_dir.parent]
    with tempfile.TemporaryDirectory() as tmp_stage:
        pack_source_ir = ir_dir
        if lang:
            loc_ir = Path(tmp_stage) / "ir_localized"
            shutil.copytree(ir_dir, loc_ir, ignore=shutil.ignore_patterns(".ir_history", "save_slot*", "save_autosave*", "__pycache__"))
            apply_i18n_locale(loc_ir, str(lang))
            pack_source_ir = loc_ir

        stage_dir = Path(tmp_stage) / "stage"
        stage_dir.mkdir(parents=True, exist_ok=True)
        pack_ir_to_rmxp(pack_source_ir, stage_dir)

        for folder_name in ("Graphics", "Audio", "Fonts"):
            for candidate_base in asset_roots:
                src_sub = candidate_base / folder_name
                if src_sub.is_dir():
                    dst_sub = stage_dir / folder_name
                    if not dst_sub.exists():
                        shutil.copytree(src_sub, dst_sub)
                    break

        # Strip save files, caches, and backup artifacts before packaging
        for trash in list(stage_dir.rglob("*")):
            if not trash.exists():
                continue
            if trash.is_dir() and trash.name == "__pycache__":
                shutil.rmtree(trash, ignore_errors=True)
            elif trash.is_file() and (
                trash.name.startswith("save_slot")
                or trash.name.startswith("save_autosave")
                or trash.suffix in (".pyc", ".bak", ".tmp")
            ):
                trash.unlink(missing_ok=True)

        if use_rgssad:
            rxproj_file = stage_dir / "Game.rxproj"
            if rxproj_file.is_file():
                shutil.copy2(rxproj_file, out_dir / "Game.rxproj")
            archive_src = stage_dir
            pack_rgssad(archive_src, out_dir / arch_ext, initial_key=eff_rgssad_key, version=rg_ver)
        else:
            for item in stage_dir.iterdir():
                dst = out_dir / item.name
                if item.is_dir():
                    shutil.copytree(item, dst)
                else:
                    shutil.copy2(item, dst)

        if launcher:
            ir_bundle = out_dir / "ir"
            shutil.copytree(
                pack_source_ir,
                ir_bundle,
                ignore=shutil.ignore_patterns(".ir_history", "save_slot*", "save_autosave*", "__pycache__"),
            )
            src_pkg = Path(__file__).resolve().parent.parent
            if src_pkg.is_dir():
                shutil.copytree(src_pkg, out_dir / "src", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            proj_meta = _load_json(pack_source_ir / "ir_project.json")
            game_title = str(proj_meta.get("system", {}).get("fields", {}).get("game_title", "RPGGame") or "RPGGame")
            (out_dir / "launch_player.py").write_text(
                "#!/usr/bin/env python3\n"
                "# -*- coding: utf-8 -*-\n"
                "import sys\n"
                "from pathlib import Path\n\n"
                "ROOT = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent))\n"
                "if str(ROOT) not in sys.path:\n"
                "    sys.path.insert(0, str(ROOT))\n\n"
                "def _find_archive() -> Path | None:\n"
                "    for fname in ('Game.rgss3a', 'Game.rgss2a', 'Game.rgssad'):\n"
                "        cand = ROOT / fname\n"
                "        if cand.is_file():\n"
                "            return cand\n"
                "    return None\n\n"
                "def _ensure_rgss3a_unpacked() -> Path:\n"
                "    archive = _find_archive()\n"
                "    runtime_cache = Path(getattr(sys, 'executable', __file__)).resolve().parent / '_runtime_assets' if getattr(sys, 'frozen', False) else (ROOT / '_runtime_assets')\n"
                "    if archive is not None:\n"
                "        from src.core.rgssad import compute_rgssad_stamp, unpack_rgssad\n"
                "        stamp_file = runtime_cache / '.rgss3a_stamp'\n"
                "        cur_stamp = compute_rgssad_stamp(archive)\n"
                "        prev_stamp = stamp_file.read_text(encoding='utf-8').strip() if stamp_file.is_file() else ''\n"
                "        if prev_stamp != cur_stamp:\n"
                f"            unpack_rgssad(archive, runtime_cache, initial_key={eff_rgssad_key})\n"
                "            runtime_cache.mkdir(parents=True, exist_ok=True)\n"
                "            stamp_file.write_text(cur_stamp, encoding='utf-8')\n"
                "    return runtime_cache\n\n"
                "def _resolve_asset_dir(folder_name: str) -> Path | None:\n"
                "    for cand in (ROOT / folder_name, ROOT / 'ir' / folder_name):\n"
                "        if cand.is_dir():\n"
                "            return cand\n"
                "    return None\n\n"
                "def main() -> int:\n"
                "    from src.harness.player_gui import launch_player\n"
                "    rgssad_arch = _find_archive()\n"
                f"    return launch_player(ROOT / 'ir', graphics_dir=_resolve_asset_dir('Graphics'), audio_dir=_resolve_asset_dir('Audio'), start_on_title=True, lang={repr(lang)}, use_os_save_dir=True, rgssad_archive=rgssad_arch, rgssad_key={eff_rgssad_key}, strict_save={bool(strict_save)!r})\n\n"
                "if __name__ == '__main__':\n"
                "    raise SystemExit(main())\n",
                encoding="utf-8",
            )
            (out_dir / "play_game.bat").write_text(
                "@echo off\nchcp 65001 > nul\npython \"%~dp0launch_player.py\"\n",
                encoding="utf-8",
            )
            sh_path = out_dir / "play_game.sh"
            sh_path.write_text(
                "#!/usr/bin/env sh\nDIR=\"$(cd \"$(dirname \"$0\")\" && pwd)\"\npython3 \"$DIR/launch_player.py\"\n",
                encoding="utf-8",
            )
            try:
                sh_path.chmod(0o755)
            except Exception:
                pass
            exe_name = game_title.replace(" ", "_") or "RPGGame"
            spec_datas: List[Tuple[str, str]] = [("ir", "ir"), ("src", "src")]
            if use_rgssad and (out_dir / arch_ext).is_file():
                spec_datas.append((arch_ext, "."))
            else:
                for af in ("Graphics", "Audio", "Fonts"):
                    if (out_dir / af).is_dir():
                        spec_datas.append((af, af))
            bundled_icon: Optional[str] = None
            if icon_path is not None:
                ip = Path(icon_path)
                if ip.is_file():
                    shutil.copy2(ip, out_dir / ip.name)
                    bundled_icon = ip.name
            hidden_mods = ["pygame", "PIL", "PIL.Image", "src.harness.player_gui", "src.core.rgssad", "src.core.simulator"]
            icon_arg = f", icon={repr(bundled_icon)}" if bundled_icon else ""
            (out_dir / "build_game.spec").write_text(
                "# -*- mode: python ; coding: utf-8 -*-\n"
                f"a = Analysis(['launch_player.py'], datas={spec_datas!r}, hiddenimports={hidden_mods!r})\n"
                "pyz = PYZ(a.pure)\n"
                f"exe = EXE(pyz, a.scripts, a.binaries, a.zipfiles, a.datas, [], name={repr(exe_name)}, console=False{icon_arg})\n",
                encoding="utf-8",
            )
            if build_exe:
                if importlib.util.find_spec("PyInstaller") is None:
                    raise RuntimeError(
                        "PyInstaller가 설치되어 있지 않아 EXE 빌드를 실행할 수 없습니다. 'pip install pyinstaller' 후 다시 시도하세요."
                    )
                subprocess.run(
                    [sys.executable, "-m", "PyInstaller", "--noconfirm", "build_game.spec"],
                    cwd=str(out_dir),
                    check=True,
                )

    files_meta: Dict[str, Dict[str, Any]] = {}
    for fpath in sorted(p for p in out_dir.rglob("*") if p.is_file()):
        rel = fpath.relative_to(out_dir).as_posix()
        digest = hashlib.sha256(fpath.read_bytes()).hexdigest()
        files_meta[rel] = {"size": fpath.stat().st_size, "sha256": digest}

    manifest_path = out_dir / "manifest.json"
    assets_summary = {
        "missing_graphics": len(asset_audit["missing_graphics"]),
        "missing_audio": len(asset_audit["missing_audio"]),
        "passed": bool(asset_audit["passed"]),
    }
    manifest_payload = {
        "schema_version": "1.0.0",
        "archive_mode": {1: "rgssad", 2: "rgss2a", 3: "rgss3a"}[rg_ver] if use_rgssad else "raw",
        "rgssad_version": rg_ver if use_rgssad else None,
        "locale": lang,
        "launcher": bool(launcher),
        "validation": val_res.stats,
        "assets_audit": assets_summary,
        "file_count": len(files_meta),
        "files": files_meta,
    }
    _save_json(manifest_path, manifest_payload)
    zip_out_path: Optional[str] = None
    if create_zip:
        zpath = out_dir.parent / f"{out_dir.name}.zip"
        with zipfile.ZipFile(zpath, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for fpath in sorted(p for p in out_dir.rglob("*") if p.is_file()):
                zf.write(fpath, arcname=fpath.relative_to(out_dir).as_posix())
        zip_out_path = str(zpath.resolve())
    return {
        "output_dir": str(out_dir.resolve()),
        "archive_mode": manifest_payload["archive_mode"],
        "rgssad_version": rg_ver if use_rgssad else None,
        "rgssad_key": f"0x{eff_rgssad_key:08X}" if use_rgssad else None,
        "zip_archive": zip_out_path,
        "locale": lang,
        "launcher": bool(launcher),
        "file_count": len(files_meta),
        "manifest": str(manifest_path.resolve()),
        "validation": val_res.stats,
        "assets_audit": assets_summary,
    }


def list_animations(ir_dir: Path) -> List[Dict[str, Any]]:
    """Lists configured animations in <ir_dir>/database/animations.json."""
    ir_dir = Path(ir_dir)
    anim_path = ir_dir / "database" / "animations.json"
    if not anim_path.is_file():
        return []
    out: List[Dict[str, Any]] = []
    for row in _load_json(anim_path):
        if isinstance(row, dict):
            af = row.get("fields", row)
            out.append({
                "id": int(af.get("id", 0)),
                "name": str(af.get("name", "")),
                "animation_name": str(af.get("animation_name", "")),
                "frame_max": int(af.get("frame_max", len(af.get("frames", [])))),
                "position": int(af.get("position", 1)),
            })
    return out


def set_animation(
    ir_dir: Path,
    anim_id: int,
    name: str,
    animation_name: str = "",
    position: int = 1,
    frame_max: int = 6,
    pattern_start: int = 0,
    zoom: int = 100,
    se_name: str = "",
    flash_color: Optional[List[int]] = None,
    cells: Optional[List[Dict[str, Any]]] = None,
    timings: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Creates or updates an RMXP Animation entry with single or multi-cell frames and timing cues.

    ``cells`` accepts three shapes:
    - a list of cell dicts: the same cells are used for every frame (``frame_max`` frames)
    - a list of per-frame cell lists: ``[[cell, ...], [cell, ...]]`` (frame count = list length)
    - a list of RPG::Animation::Frame IR objects: kept as-is (studio JSON round-trip)
    """
    ir_dir = Path(ir_dir)
    aid = max(1, int(anim_id))
    fmax = max(1, min(60, int(frame_max)))
    anim_path = ir_dir / "database" / "animations.json"
    rows: List[Any] = _load_json(anim_path) if anim_path.is_file() else [None]
    snap = _snapshot_ir_files(ir_dir)
    while len(rows) <= aid:
        rows.append(None)

    def _cell_props(cspec: Dict[str, Any], f_idx: int) -> List[int]:
        return [
            int(cspec.get("pattern", int(pattern_start) + (f_idx % 5))),
            int(cspec.get("x", 0)),
            int(cspec.get("y", 0)),
            int(cspec.get("zoom", zoom)),
            int(cspec.get("angle", 0)) % 360,
            1 if cspec.get("mirror") else 0,
            max(0, min(255, int(cspec.get("opacity", 255)))),
            int(cspec.get("blend_type", 1)),
        ]

    def _frame_obj(cell_rows: List[List[int]]) -> Dict[str, Any]:
        # RMXP stores cell_data as Table(cell_max, 8): data[cell + prop * cell_max]
        n = len(cell_rows)
        data = [cell_rows[c][p] for p in range(8) for c in range(n)]
        return {
            "__type__": "RubyObject",
            "class": "RPG::Animation::Frame",
            "fields": {
                "cell_max": n,
                "cell_data": {"__type__": "Table", "dims": 2, "xsize": n, "ysize": 8, "zsize": 1, "data": data},
            },
        }

    def _is_ir_frame(obj: Any) -> bool:
        return isinstance(obj, dict) and isinstance(obj.get("fields"), dict) and "cell_data" in obj["fields"]

    def _normalize_ir_frame(obj: Dict[str, Any]) -> Dict[str, Any]:
        ff = obj["fields"]
        tbl = ff.get("cell_data") if isinstance(ff.get("cell_data"), dict) else {}
        data = [int(v) for v in tbl.get("data", [])] if isinstance(tbl.get("data"), list) else []
        cell_max = max(0, int(ff.get("cell_max", tbl.get("xsize", 0)) or 0))
        xsize = max(1, int(tbl.get("xsize", cell_max) or cell_max or 1))
        ysize = max(1, int(tbl.get("ysize", 8) or 8))
        return {
            "__type__": "RubyObject",
            "class": "RPG::Animation::Frame",
            "fields": {
                "cell_max": cell_max,
                "cell_data": {"__type__": "Table", "dims": 2, "xsize": xsize, "ysize": ysize, "zsize": 1, "data": data},
            },
        }

    def _default_cell(f_idx: int, count: int) -> List[int]:
        z_scale = max(40, int(zoom) + f_idx * 10)
        op = max(80, 255 - f_idx * (140 // max(1, count)))
        return [int(pattern_start) + (f_idx % 5), 0, 0, z_scale, (f_idx * 15) % 360, 0, op, 1]

    frames: List[Dict[str, Any]] = []
    cell_list = list(cells) if isinstance(cells, list) else []
    if cell_list and all(_is_ir_frame(c) for c in cell_list):
        # Full RPG::Animation::Frame objects (e.g. studio JSON round-trip): keep every frame as-is.
        frames = [_normalize_ir_frame(c) for c in cell_list][:60]
    elif cell_list and all(isinstance(c, list) for c in cell_list):
        # Per-frame cell lists: [[cell, cell], [cell], ...]
        for f_idx, frame_cells in enumerate(cell_list[:60]):
            rows_c = [_cell_props(c, f_idx) for c in frame_cells if isinstance(c, dict)]
            frames.append(_frame_obj(rows_c or [_default_cell(f_idx, len(cell_list))]))
    else:
        specs = [c for c in cell_list if isinstance(c, dict)]
        for f_idx in range(fmax):
            rows_c = [_cell_props(c, f_idx) for c in specs] if specs else [_default_cell(f_idx, fmax)]
            frames.append(_frame_obj(rows_c))
    if frames:
        fmax = len(frames)

    def _color(raw: Any, default_alpha: int = 180) -> Dict[str, Any]:
        vals = raw.get("rgba") if isinstance(raw, dict) else raw
        rgba = [max(0, min(255, int(c))) for c in (vals if isinstance(vals, list) else [255, 255, 255, default_alpha])[:4]]
        while len(rgba) < 4:
            rgba.append(255 if len(rgba) < 3 else default_alpha)
        return {"__type__": "Color", "rgba": rgba}

    def _audio(raw: Any, name: str = "", volume: int = 85, pitch: int = 100) -> Any:
        if isinstance(raw, dict):
            af_se = raw.get("fields", raw)
            name = str(af_se.get("name", name) or "")
            volume = int(af_se.get("volume", volume))
            pitch = int(af_se.get("pitch", pitch))
        return ruby_to_ir(make_audio_file(name, volume, pitch))

    def _timing_obj(frame: int, se: Any, scope: int, color: Any, duration: int, condition: int) -> Dict[str, Any]:
        return {
            "__type__": "RubyObject",
            "class": "RPG::Animation::Timing",
            "fields": {
                "frame": max(0, int(frame)),
                "se": se,
                "flash_scope": int(scope),
                "flash_color": _color(color),
                "flash_duration": int(duration),
                "condition": int(condition),
            },
        }

    built_timings: List[Dict[str, Any]] = []
    if timings:
        for tspec in timings:
            if not isinstance(tspec, dict):
                continue
            if isinstance(tspec.get("fields"), dict):
                # Existing RPG::Animation::Timing object (studio JSON round-trip)
                tf = tspec["fields"]
                built_timings.append(_timing_obj(
                    int(tf.get("frame", 0)),
                    _audio(tf.get("se")),
                    int(tf.get("flash_scope", 0)),
                    tf.get("flash_color"),
                    int(tf.get("flash_duration", 0)),
                    int(tf.get("condition", 0)),
                ))
                continue
            has_flash = bool(tspec.get("flash_color"))
            built_timings.append(_timing_obj(
                int(tspec.get("frame", 0)),
                _audio(tspec.get("se"), str(tspec.get("se_name", "") or ""), int(tspec.get("volume", 85)), int(tspec.get("pitch", 100))),
                int(tspec.get("flash_scope", 1 if has_flash else 0)),
                tspec.get("flash_color"),
                int(tspec.get("flash_duration", 6 if has_flash else 0)),
                int(tspec.get("condition", 0)),
            ))
    elif se_name or flash_color:
        built_timings.append(_timing_obj(
            0,
            _audio(None, se_name, 85, 100),
            1 if flash_color else 0,
            flash_color,
            6 if flash_color else 0,
            0,
        ))

    prev_fields = rows[aid].get("fields", {}) if isinstance(rows[aid], dict) else {}
    rows[aid] = {
        "__type__": "RubyObject",
        "class": "RPG::Animation",
        "fields": {
            "id": aid,
            "name": str(name),
            "animation_name": str(animation_name),
            "animation_hue": int(prev_fields.get("animation_hue", 0) or 0),
            "position": max(0, min(3, int(position))),
            "frame_max": fmax,
            "frames": frames,
            "timings": built_timings,
        },
    }
    _save_json(anim_path, rows)
    _push_ir_history(ir_dir, f"set_animation:{aid}", snap)
    return {"id": aid, "name": str(name), "animation_name": str(animation_name), "frame_max": fmax}


def get_battle_formula(ir_dir: Path) -> Dict[str, Any]:
    """Returns the project's stored battle_formula config, the effective formulas and all presets."""
    from .battle_formulas import BattleFormula, list_presets

    proj = _load_json(Path(ir_dir) / "ir_project.json")
    cfg = proj.get("battle_formula")
    bf = BattleFormula.from_project(proj)
    return {"config": cfg if cfg is not None else None, "effective": bf.describe(), "presets": list_presets()}


def set_battle_formula(
    ir_dir: Path,
    preset: Optional[str] = None,
    overrides: Optional[Dict[str, Any]] = None,
    clear_overrides: bool = False,
    replace_config: Optional[Dict[str, Any]] = None,
    dry_run: bool = False,
    record_undo: bool = True,
) -> Dict[str, Any]:
    """Changes the project's battle damage formula preset and/or overrides (validated, undoable).

    ``overrides`` values of ``""`` remove that override. ``replace_config`` replaces the whole setting.
    """
    from .battle_formulas import OVERRIDE_KEYS, BattleFormula, resolve_preset_name

    ir_dir = Path(ir_dir)
    proj_path = ir_dir / "ir_project.json"
    proj = _load_json(proj_path)
    if replace_config is not None:
        if not isinstance(replace_config, dict):
            raise ValueError("battle_formula must be an object")
        new_cfg: Dict[str, Any] = dict(replace_config)
    else:
        cur = proj.get("battle_formula") if isinstance(proj.get("battle_formula"), dict) else {}
        new_cfg = {"preset": cur.get("preset", "rmxp")}
        if not clear_overrides:
            new_cfg.update({k: v for k, v in cur.items() if k in OVERRIDE_KEYS})
        if preset:
            new_cfg["preset"] = preset
        for key, val in (overrides or {}).items():
            if key not in OVERRIDE_KEYS:
                raise ValueError(f"Unknown battle_formula option {key!r}; choose from {list(OVERRIDE_KEYS)}")
            if val is None:
                continue
            if val == "":
                new_cfg.pop(key, None)
            else:
                new_cfg[key] = val
    unknown = [k for k in new_cfg if k != "preset" and k not in OVERRIDE_KEYS]
    if unknown:
        raise ValueError(f"Unknown battle_formula option(s) {unknown}; choose from {list(OVERRIDE_KEYS)}")
    new_cfg["preset"] = resolve_preset_name(new_cfg.get("preset"))
    bf = BattleFormula.from_config(new_cfg)
    bf.validate()
    if not dry_run:
        snap = _snapshot_ir_files(ir_dir) if record_undo else None
        proj["battle_formula"] = new_cfg
        _save_json(proj_path, proj)
        if snap is not None:
            _push_ir_history(ir_dir, f"battle_formula:{new_cfg['preset']}", snap)
    return {"battle_formula": new_cfg, "effective": bf.describe(), "dry_run": bool(dry_run)}


def calc_battle_damage(
    ir_dir: Path,
    enemy_id: int,
    actor_id: Optional[int] = None,
    skill_id: Optional[int] = None,
    enemy_skill_id: Optional[int] = None,
    presets: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Computes base damage between an actor and an enemy under the project formula (or other presets).

    Values are before element, critical and variance. ``presets`` previews other presets without saving.
    """
    from .battle_engine import build_single_enemy, enemy_formula_stats
    from .battle_formulas import BattleFormula, resolve_preset_name
    from .simulator import HeadlessGameSession

    sess = HeadlessGameSession(Path(ir_dir))
    aid = int(actor_id) if actor_id is not None else int(sess.lead_actor_id)
    enemy = build_single_enemy(int(enemy_id), sess.enemies)
    if enemy is None:
        raise ValueError(f"Enemy {enemy_id} not found")
    a_stats = sess._actor_formula_stats(aid)
    e_stats = enemy_formula_stats(enemy)
    hero_skill = sess.skills_db.get(int(skill_id), {}).get("fields") if skill_id else None
    if skill_id and hero_skill is None:
        raise ValueError(f"Skill {skill_id} not found")
    foe_skill = sess.skills_db.get(int(enemy_skill_id), {}).get("fields") if enemy_skill_id else None
    if enemy_skill_id and foe_skill is None:
        raise ValueError(f"Skill {enemy_skill_id} not found")

    formulas = [sess.battle_formula] if not presets else [BattleFormula(resolve_preset_name(p)) for p in presets]
    rows: List[Dict[str, Any]] = []
    for bf in formulas:
        row: Dict[str, Any] = {
            "preset": bf.preset,
            "actor_attack": bf.attack_damage(a_stats, e_stats),
            "enemy_attack": bf.attack_damage(e_stats, a_stats),
            "critical_multiplier": bf.critical_multiplier,
            "attack_variance": bf.attack_variance,
        }
        if hero_skill is not None:
            row["actor_skill"] = bf.skill_damage(a_stats, e_stats, hero_skill)
        if foe_skill is not None:
            row["enemy_skill"] = bf.skill_damage(e_stats, a_stats, foe_skill)
        best_hero = max(row["actor_attack"], row.get("actor_skill", 0))
        row["actor_hits_to_defeat_enemy"] = -(-int(e_stats["mhp"]) // best_hero) if best_hero > 0 else None
        best_foe = max(row["enemy_attack"], row.get("enemy_skill", 0))
        row["enemy_hits_to_defeat_actor"] = -(-int(a_stats["mhp"]) // best_foe) if best_foe > 0 else None
        rows.append(row)
    return {
        "actor": {"id": aid, "name": sess._get_actor_state(aid).get("name"), "stats": a_stats},
        "enemy": {"id": int(enemy_id), "name": enemy["name"], "stats": e_stats},
        "skill_id": skill_id,
        "enemy_skill_id": enemy_skill_id,
        "results": rows,
    }


def batch_tune_balance(
    ir_dir: Path,
    enemy_hp_mult: float = 1.0,
    enemy_atk_mult: float = 1.0,
    exp_mult: float = 1.0,
    gold_mult: float = 1.0,
    skill_power_mult: float = 1.0,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Scales enemy stats/rewards and skill power in bulk with atomic undo support."""
    ir_dir = Path(ir_dir)
    db_dir = ir_dir / "database"
    snap = _snapshot_ir_files(ir_dir)
    enemies_path = db_dir / "enemies.json"
    skills_path = db_dir / "skills.json"
    enemies = _load_json(enemies_path) if enemies_path.is_file() else []
    skills = _load_json(skills_path) if skills_path.is_file() else []
    changes: List[str] = []
    enemies_tuned = 0
    skills_tuned = 0

    for row in enemies:
        if not isinstance(row, dict):
            continue
        ef = row.get("fields", row)
        old_hp = int(ef.get("maxhp", 50))
        new_hp = max(1, int(round(old_hp * float(enemy_hp_mult))))
        old_atk = int(ef.get("atk", 10))
        new_atk = max(1, int(round(old_atk * float(enemy_atk_mult))))
        old_exp = int(ef.get("exp", 0))
        new_exp = max(0, int(round(old_exp * float(exp_mult))))
        old_gold = int(ef.get("gold", 0))
        new_gold = max(0, int(round(old_gold * float(gold_mult))))
        if (new_hp, new_atk, new_exp, new_gold) != (old_hp, old_atk, old_exp, old_gold):
            ef["maxhp"] = new_hp
            ef["atk"] = new_atk
            ef["exp"] = new_exp
            ef["gold"] = new_gold
            enemies_tuned += 1
            changes.append(f"Enemy#{ef.get('id')}: HP {old_hp}->{new_hp}, ATK {old_atk}->{new_atk}")

    for row in skills:
        if not isinstance(row, dict):
            continue
        sf = row.get("fields", row)
        old_pwr = int(sf.get("power", 0))
        if old_pwr != 0 and abs(float(skill_power_mult) - 1.0) > 1e-6:
            sign = 1 if old_pwr > 0 else -1
            new_pwr = sign * max(1, int(round(abs(old_pwr) * float(skill_power_mult))))
            if new_pwr != old_pwr:
                sf["power"] = new_pwr
                skills_tuned += 1
                changes.append(f"Skill#{sf.get('id')}: PWR {old_pwr}->{new_pwr}")

    if not dry_run and (enemies_tuned > 0 or skills_tuned > 0):
        if enemies_path.is_file():
            _save_json(enemies_path, enemies)
        if skills_path.is_file():
            _save_json(skills_path, skills)
        _push_ir_history(ir_dir, "batch_tune_balance", snap)

    return {
        "ir_dir": str(ir_dir.resolve()),
        "enemies_tuned": enemies_tuned,
        "skills_tuned": skills_tuned,
        "dry_run": bool(dry_run),
        "changes": changes,
    }


def _tokenize_workbench_line(line: str) -> List[str]:
    """Tokenizes a workbench command line, preserving Windows backslashes and mid-token quotes."""
    tokens: List[str] = []
    buf: List[str] = []
    in_quote: Optional[str] = None
    has_token = False
    for ch in line:
        if in_quote is not None:
            if ch == in_quote:
                in_quote = None
            else:
                buf.append(ch)
        elif ch in ('"', "'"):
            in_quote = ch
            has_token = True
        elif ch.isspace():
            if has_token or buf:
                tokens.append("".join(buf))
                buf.clear()
                has_token = False
        else:
            buf.append(ch)
            has_token = True
    if in_quote is not None:
        raise ValueError("닫히지 않은 따옴표가 있습니다.")
    if has_token or buf:
        tokens.append("".join(buf))
    return tokens


def run_interactive_workbench(
    ir_dir: Path,
    stdin_lines: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Runs a lightweight terminal authoring workbench loop for inspecting and editing IR workspaces."""
    ir_dir = Path(ir_dir)
    logs: List[str] = []
    commands_run = 0

    def _next_cmd() -> Optional[str]:
        if stdin_lines is not None:
            return stdin_lines.pop(0) if stdin_lines else None
        try:
            return input("rpgc> ")
        except EOFError:
            return None

    while True:
        raw = _next_cmd()
        if raw is None:
            break
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        try:
            tokens = _tokenize_workbench_line(line)
        except ValueError as exc:
            logs.append(f"[ERROR] {exc}")
            continue
        if not tokens:
            continue
        op = tokens[0].lower()
        if op in ("quit", "exit", "q"):
            logs.append("[workbench] 종료")
            break
        commands_run += 1
        try:
            if op == "help":
                logs.append("명령: inspect | maps | events <map_id> | paint <map_id> <tile_id> <x> <y> | add-event <map_id> <name> <x> <y> <text> | delete-event <map_id> <event_id> | set-db <table> <id> <k=v> | anim list | anim set <id> <name> [sheet] | balance-tune <hp_mult> <atk_mult> | export-release <out_dir> | undo | redo | check | quit")
            elif op == "inspect":
                info = inspect_ir_project(ir_dir)
                logs.append(f"{info['title']} | maps={len(info['maps'])} | events={info['events_total']}")
            elif op == "maps":
                info = inspect_ir_project(ir_dir)
                logs.append(", ".join(f"#{m['id']}:{m['name']}({m['width']}x{m['height']})" for m in info["maps"]))
            elif op == "events" and len(tokens) >= 2:
                evs = list_events(ir_dir, int(tokens[1]))
                logs.append(", ".join(f"EV{e['id']}:{e['name']}({e['x']},{e['y']})" for e in evs))
            elif op == "paint" and len(tokens) >= 5:
                res = paint_map(ir_dir, map_id=int(tokens[1]), layer=0, tile_id=int(tokens[2]), points=[(int(tokens[3]), int(tokens[4]))])
                logs.append(f"painted={res['tiles_painted']}")
            elif op == "add-event" and len(tokens) >= 6:
                res = add_event(
                    ir_dir,
                    map_id=int(tokens[1]),
                    name=tokens[2],
                    x=int(tokens[3]),
                    y=int(tokens[4]),
                    commands=[{"op": "say", "lines": [" ".join(tokens[5:])]}],
                )
                logs.append(f"added_event=#{res['event_id']}")
            elif op == "delete-event" and len(tokens) >= 3:
                res = delete_event(ir_dir, map_id=int(tokens[1]), event_id=int(tokens[2]))
                logs.append(f"deleted_event={res['deleted']}")
            elif op == "set-db" and len(tokens) >= 4:
                kv_pairs: Dict[str, Any] = {}
                for tok in tokens[3:]:
                    if "=" in tok:
                        k, v = tok.split("=", 1)
                        kv_pairs[k] = _coerce_value(v)
                set_db_entry(ir_dir, table=tokens[1], entry_id=int(tokens[2]), fields=kv_pairs)
                logs.append(f"updated {tokens[1]}#{tokens[2]}")
            elif op == "anim" and len(tokens) >= 2 and tokens[1] == "list":
                logs.append(", ".join(f"#{a['id']}:{a['name']}" for a in list_animations(ir_dir)))
            elif op == "anim" and len(tokens) >= 4 and tokens[1] == "set":
                sheet = tokens[4] if len(tokens) >= 5 else ""
                res = set_animation(ir_dir, anim_id=int(tokens[2]), name=tokens[3], animation_name=sheet)
                logs.append(f"anim_set=#{res['id']}:{res['name']}")
            elif op == "balance-tune" and len(tokens) >= 2:
                hp_m = float(tokens[1])
                atk_m = float(tokens[2]) if len(tokens) >= 3 else 1.0
                res = batch_tune_balance(ir_dir, enemy_hp_mult=hp_m, enemy_atk_mult=atk_m)
                logs.append(f"tuned_enemies={res['enemies_tuned']}")
            elif op == "export-release" and len(tokens) >= 2:
                res = export_release_bundle(ir_dir, Path(tokens[1]), use_rgssad=True, launcher=True)
                logs.append(f"released={res['file_count']} -> {res['output_dir']}")
            elif op == "undo":
                res = undo_last_edit(ir_dir)
                logs.append(f"undo={res['restored']}")
            elif op == "redo":
                res = redo_last_edit(ir_dir)
                logs.append(f"redo={res['restored']}")
            elif op == "check":
                chk = validate_ir_project(ir_dir)
                logs.append(f"check passed={chk.passed} errors={len(chk.errors)}")
            else:
                logs.append(f"알 수 없는 명령 또는 인자 부족: {line}")
        except Exception as exc:
            logs.append(f"[ERROR] {exc}")

    return {"ir_dir": str(ir_dir.resolve()), "commands_run": commands_run, "logs": logs}
