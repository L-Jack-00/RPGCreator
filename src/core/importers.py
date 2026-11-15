# -*- coding: utf-8 -*-
"""
Multi-Engine Importers for rpgcreator_cli IR Workspaces.
Converts RPG Maker MV/MZ JSON projects, Tiled (.tmj/.tmx) maps, and
RPG Maker VX Ace (.rvdata2/.rxdata) projects into normalized IR format.
"""
from __future__ import annotations

import copy
import shutil
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Dict, List, Optional

from .ir_converter import commands_to_semantic_dsl, ruby_to_ir
from .rmxp_marshal import marshal_loads
from .rmxp_types import RubyObject, make_audio_file, make_event, make_event_command, make_event_page
from .sample_generator import generate_sample_project
from .utils import (
    load_json as _load_json,
    save_json as _save_json,
    upsert_map_info_entry as _upsert_map_info,
)

_TILE_BASE_GRASS = 384


def _bootstrap_ir_workspace(out_ir_dir: Path, title: str) -> None:
    """Initializes a schema-valid IR directory cloned from the cleanroom starter template."""
    out_ir_dir = Path(out_ir_dir)
    if out_ir_dir.exists():
        shutil.rmtree(out_ir_dir)
    with tempfile.TemporaryDirectory() as tmp:
        tmp_ws = Path(tmp) / "ws"
        generate_sample_project(tmp_ws)
        shutil.copytree(tmp_ws / "ir", out_ir_dir)
    proj_path = out_ir_dir / "ir_project.json"
    proj = _load_json(proj_path)
    proj["rxproj_header"] = title
    proj["system"]["fields"]["game_title"] = title
    _save_json(proj_path, proj)


def _mv_tile_to_rmxp(gid: int) -> int:
    """Maps RPG Maker MV/MZ A1-A5 and B-E tile IDs into distinct RMXP autotile/tileset bands."""
    val = int(gid or 0)
    if val <= 0:
        return 0
    if 2048 <= val < 2816:
        return 48 + (((val - 2048) // 48) % 7) * 48
    if 2816 <= val < 4352:
        return 48 + (((val - 2816) // 48) % 7) * 48
    if 1536 <= val < 2048:
        return _TILE_BASE_GRASS + ((val - 1536) % 64)
    if 0 < val < 1536:
        return 384 + (val % 256)
    return _TILE_BASE_GRASS + (val % 64)


def _convert_mv_commands(raw_list: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    raw_cmds: List[RubyObject] = []
    idx = 0
    items = [c for c in (raw_list or []) if isinstance(c, dict)]
    while idx < len(items):
        c = items[idx]
        code = int(c.get("code", 0))
        indent = int(c.get("indent", 0))
        params = list(c.get("parameters", []))
        # MV/MZ code 101 stores [faceName, faceIndex, background, positionType] followed by code 401 lines
        if code == 101 and len(params) >= 4 and isinstance(params[1], int):
            face_name = str(params[0] or "")
            face_idx = int(params[1] or 0)
            lines_401: List[str] = []
            nxt = idx + 1
            while nxt < len(items) and int(items[nxt].get("code", 0)) == 401:
                lines_401.append(str((items[nxt].get("parameters") or [""])[0]))
                nxt += 1
            if lines_401:
                first_line = f"\\F[{face_name}:{face_idx}]{lines_401[0]}" if face_name else lines_401[0]
                raw_cmds.append(make_event_command(101, indent, [first_line]))
                for cont in lines_401[1:]:
                    raw_cmds.append(make_event_command(401, indent, [cont]))
            idx = nxt
            continue
        raw_cmds.append(make_event_command(code, indent, params))
        idx += 1
    return commands_to_semantic_dsl(raw_cmds)


def _create_empty_layers(w: int, h: int, fill_tile: int = _TILE_BASE_GRASS) -> Dict[str, List[List[int]]]:
    return {
        "layer_0": [[fill_tile for _ in range(w)] for _ in range(h)],
        "layer_1": [[0 for _ in range(w)] for _ in range(h)],
        "layer_2": [[0 for _ in range(w)] for _ in range(h)],
    }


def _make_map_info_ir(name: str) -> Dict[str, Any]:
    return ruby_to_ir(RubyObject("RPG::MapInfo", {"@name": name, "@parent_id": 0, "@order": 1, "@expanded": False, "@scroll_x": 0, "@scroll_y": 0}))


def _patch_table_from_mv(
    db_path: Path,
    mv_rows: List[Any],
    field_mapper: Any,
) -> None:
    existing = _load_json(db_path)
    template = copy.deepcopy(existing[1]) if len(existing) > 1 and existing[1] else {"__ruby_class__": "RPG::Actor", "fields": {}}
    out: List[Any] = [None]
    for item in mv_rows:
        if not item:
            continue
        eid = int(item.get("id", len(out)))
        while len(out) <= eid:
            out.append(None)
        rec = copy.deepcopy(template)
        rec["fields"]["id"] = eid
        field_mapper(rec["fields"], item)
        out[eid] = rec
    if len(out) > 1:
        _save_json(db_path, out)


def _apply_audio_spec_fields(sf: Dict[str, Any], key: str, raw_aud: Any) -> None:
    if not isinstance(raw_aud, dict):
        return
    src = raw_aud.get("fields", raw_aud)
    if not isinstance(src, dict):
        return
    name = str(src.get("name", "") or "")
    if not name:
        return
    sf[key] = {
        "__ruby_class__": "RPG::AudioFile",
        "fields": {
            "name": name,
            "volume": int(src.get("volume", 80) or 80),
            "pitch": int(src.get("pitch", 100) or 100),
        },
    }


def _apply_tile_flags_to_tilesets(
    db_dir: Path,
    tilesets_meta: List[Any],
    is_ruby_rows: bool = False,
) -> int:
    ts_path = db_dir / "tilesets.json"
    if not ts_path.exists() or not isinstance(tilesets_meta, list):
        return 0
    ts_db = _load_json(ts_path)
    updated = 0
    for ts_raw in tilesets_meta:
        if not ts_raw:
            continue
        ts_obj = ruby_to_ir(ts_raw).get("fields", {}) if is_ruby_rows else ts_raw
        if not isinstance(ts_obj, dict):
            continue
        ts_id = int(ts_obj.get("id", 0) or 0)
        if ts_id <= 0:
            continue
        while len(ts_db) <= ts_id:
            ts_db.append(copy.deepcopy(ts_db[1]) if len(ts_db) > 1 and ts_db[1] else None)
        entry = ts_db[ts_id]
        if not isinstance(entry, dict):
            continue
        tf = entry.setdefault("fields", {})
        if ts_obj.get("name"):
            tf["name"] = str(ts_obj["name"])
        flags = ts_obj.get("flags")
        if isinstance(flags, dict):
            flags = flags.get("data", [])
        if isinstance(flags, list) and flags:
            passages = tf.setdefault("passages", {"data": []}).setdefault("data", [])
            priorities = tf.setdefault("priorities", {"data": []}).setdefault("data", [])
            for mv_gid, f_val in enumerate(flags):
                if mv_gid <= 0:
                    continue
                rmxp_tid = _mv_tile_to_rmxp(mv_gid)
                if rmxp_tid <= 0:
                    continue
                while len(passages) <= rmxp_tid:
                    passages.append(0)
                while len(priorities) <= rmxp_tid:
                    priorities.append(0)
                fv = int(f_val or 0)
                passages[rmxp_tid] = fv & 0x0F
                priorities[rmxp_tid] = 5 if (fv & 0x10) else (1 if (fv & 0x0F) else 0)
            updated += 1
    if updated > 0:
        _save_json(ts_path, ts_db)
    return updated


def import_rpgmaker_mv_mz(src_dir: Path, out_ir_dir: Path) -> Dict[str, Any]:
    """Converts an RPG Maker MV or MZ project directory into a normalized IR workspace."""
    src_dir = Path(src_dir)
    data_dir = src_dir / "data" if (src_dir / "data").is_dir() else src_dir
    out_ir_dir = Path(out_ir_dir)
    sys_json = _load_json(data_dir / "System.json")
    title = str(sys_json.get("gameTitle", "MV/MZ Imported RPG") or "MV/MZ Imported RPG")
    _bootstrap_ir_workspace(out_ir_dir, title)

    proj_path = out_ir_dir / "ir_project.json"
    proj = _load_json(proj_path)
    sf = proj["system"]["fields"]
    sf["start_map_id"] = int(sys_json.get("startMapId", 1) or 1)
    sf["start_x"] = int(sys_json.get("startX", 0) or 0)
    sf["start_y"] = int(sys_json.get("startY", 0) or 0)
    if sys_json.get("partyMembers"):
        sf["party_members"] = [int(x) for x in sys_json["partyMembers"]]
    if sys_json.get("switches"):
        sf["switches"] = list(sys_json["switches"])
    if sys_json.get("variables"):
        sf["variables"] = list(sys_json["variables"])
    for mv_k, ir_k in (
        ("titleBgm", "title_bgm"),
        ("battleBgm", "battle_bgm"),
        ("battleEndMe", "battle_end_me"),
        ("gameoverMe", "gameover_me"),
    ):
        _apply_audio_spec_fields(sf, ir_k, sys_json.get(mv_k))

    db_dir = out_ir_dir / "database"
    if (data_dir / "Tilesets.json").exists():
        _apply_tile_flags_to_tilesets(db_dir, _load_json(data_dir / "Tilesets.json"), is_ruby_rows=False)
    if (data_dir / "Actors.json").exists():
        def _map_actor(f: Dict[str, Any], a: Dict[str, Any]) -> None:
            f["name"] = str(a.get("name", ""))
            f["class_id"] = int(a.get("classId", 1) or 1)
            f["initial_level"] = int(a.get("initialLevel", 1) or 1)
            f["character_name"] = str(a.get("characterName", ""))
            f["battler_name"] = str(a.get("battlerName", ""))

        _patch_table_from_mv(db_dir / "actors.json", _load_json(data_dir / "Actors.json"), _map_actor)

    for fname, tbl, mapper in (
        ("Classes.json", "classes.json", lambda f, r: f.update({"name": str(r.get("name", ""))})),
        ("Skills.json", "skills.json", lambda f, r: f.update({"name": str(r.get("name", "")), "description": str(r.get("description", "")), "sp_cost": int(r.get("mpCost", 0) or 0)})),
        ("Items.json", "items.json", lambda f, r: f.update({"name": str(r.get("name", "")), "description": str(r.get("description", "")), "price": int(r.get("price", 0) or 0), "consumable": bool(r.get("consumable", True))})),
        ("Weapons.json", "weapons.json", lambda f, r: f.update({"name": str(r.get("name", "")), "price": int(r.get("price", 0) or 0), "atk": int((r.get("params") or [0] * 8)[2])})),
        ("Armors.json", "armors.json", lambda f, r: f.update({"name": str(r.get("name", "")), "price": int(r.get("price", 0) or 0), "pdef": int((r.get("params") or [0] * 8)[3]), "mdef": int((r.get("params") or [0] * 8)[5])})),
        ("Enemies.json", "enemies.json", lambda f, r: f.update({"name": str(r.get("name", "")), "battler_name": str(r.get("battlerName", "")), "maxhp": int((r.get("params") or [80])[0]), "atk": int((r.get("params") or [80, 10, 14])[2]), "pdef": int((r.get("params") or [80, 10, 14, 6])[3]), "exp": int(r.get("exp", 10) or 10), "gold": int(r.get("gold", 15) or 15)})),
        ("States.json", "states.json", lambda f, r: f.update({"name": str(r.get("name", "")), "restriction": int(r.get("restriction", 0) or 0)})),
    ):
        fpath = data_dir / fname
        if fpath.exists():
            _patch_table_from_mv(db_dir / tbl, _load_json(fpath), mapper)

    if (data_dir / "CommonEvents.json").exists():
        ce_out: List[Any] = [None]
        for ce in _load_json(data_dir / "CommonEvents.json"):
            if ce:
                ce_out.append({
                    "id": int(ce["id"]),
                    "name": str(ce.get("name", "")),
                    "trigger": int(ce.get("trigger", 0) or 0),
                    "switch_id": int(ce.get("switchId", 1) or 1),
                    "commands": _convert_mv_commands(ce.get("list", [])),
                })
        if len(ce_out) > 1:
            _save_json(db_dir / "common_events.json", ce_out)

    map_infos_path = data_dir / "MapInfos.json"
    map_infos_mv = _load_json(map_infos_path) if map_infos_path.exists() else []
    mv_names = {int(mi["id"]): str(mi.get("name", f"Map {mi['id']}")) for mi in map_infos_mv if mi}

    proj["map_infos"] = {}
    maps_dir = out_ir_dir / "maps"
    for existing in maps_dir.glob("map_*.json"):
        existing.unlink()

    imported_maps = 0
    for mp in sorted(data_dir.glob("Map[0-9][0-9][0-9].json")):
        mid = int(mp.stem[3:])
        mdata = _load_json(mp)
        w = max(1, int(mdata.get("width", 20)))
        h = max(1, int(mdata.get("height", 15)))
        raw_grid = list(mdata.get("data", []))
        layers = _create_empty_layers(w, h)
        if raw_grid:
            for z in range(min(3, max(1, len(raw_grid) // max(1, w * h)))):
                off = z * w * h
                lgrid = layers[f"layer_{z}"]
                for y in range(h):
                    for x in range(w):
                        idx = off + y * w + x
                        gid = int(raw_grid[idx]) if idx < len(raw_grid) else 0
                        lgrid[y][x] = _mv_tile_to_rmxp(gid) if (gid > 0 or z > 0) else _TILE_BASE_GRASS
        events_out: List[Dict[str, Any]] = []
        for ev in mdata.get("events", []):
            if not ev:
                continue
            pages_out: List[Dict[str, Any]] = []
            for pg in ev.get("pages", []):
                img = pg.get("image", {})
                cond = pg.get("conditions", {})
                pages_out.append({
                    "condition": ruby_to_ir({
                        "switch1_valid": bool(cond.get("switch1Valid", False)),
                        "switch1_id": int(cond.get("switch1Id", 1) or 1),
                        "switch2_valid": bool(cond.get("switch2Valid", False)),
                        "switch2_id": int(cond.get("switch2Id", 1) or 1),
                        "variable_valid": bool(cond.get("variableValid", False)),
                        "variable_id": int(cond.get("variableId", 1) or 1),
                        "variable_value": int(cond.get("variableValue", 0) or 0),
                        "self_switch_valid": bool(cond.get("selfSwitchValid", False)),
                        "self_switch_ch": str(cond.get("selfSwitchCh", "A") or "A"),
                    }),
                    "graphic": ruby_to_ir({
                        "character_name": str(img.get("characterName", "")),
                        "character_hue": 0,
                        "direction": int(img.get("direction", 2) or 2),
                        "pattern": int(img.get("pattern", 0) or 0),
                        "opacity": 255,
                        "blend_type": 0,
                    }),
                    "trigger": int(pg.get("trigger", 0) or 0),
                    "through": bool(pg.get("through", False)),
                    "walk_anime": bool(pg.get("walkAnime", True)),
                    "step_anime": bool(pg.get("stepAnime", False)),
                    "direction_fix": bool(pg.get("directionFix", False)),
                    "always_on_top": int(pg.get("priorityType", 1)) == 2,
                    "move_type": int(pg.get("moveType", 0) or 0),
                    "move_speed": int(pg.get("moveSpeed", 3) or 3),
                    "move_frequency": int(pg.get("moveFrequency", 3) or 3),
                    "commands": _convert_mv_commands(pg.get("list", [])),
                })
            events_out.append({
                "id": int(ev["id"]),
                "name": str(ev.get("name", f"EV{ev['id']}")),
                "x": int(ev.get("x", 0)),
                "y": int(ev.get("y", 0)),
                "pages": pages_out,
            })
        mname = mv_names.get(mid, str(mdata.get("displayName", "") or f"Map {mid}"))
        enc_list: List[int] = []
        enc_weights: List[Dict[str, Any]] = []
        for enc in mdata.get("encounterList", []):
            if isinstance(enc, dict) and int(enc.get("troopId", 0) or 0) > 0:
                tid = int(enc.get("troopId", 1) or 1)
                enc_list.append(tid)
                enc_weights.append({
                    "troop_id": tid,
                    "weight": max(1, int(enc.get("weight", 10) or 10)),
                    "region_set": list(enc.get("regionSet", [])),
                })
        map_bgm = mdata.get("bgm") if isinstance(mdata.get("bgm"), dict) else {}
        map_bgs = mdata.get("bgs") if isinstance(mdata.get("bgs"), dict) else {}
        map_ir = {
            "map_id": mid,
            "name": mname,
            "width": w,
            "height": h,
            "tileset_id": max(1, int(mdata.get("tilesetId", 1) or 1)),
            "autoplay_bgm": bool(mdata.get("autoplayBgm", False)),
            "bgm": ruby_to_ir(make_audio_file(
                str(map_bgm.get("name", "") or ""),
                int(map_bgm.get("volume", 80) or 80),
                int(map_bgm.get("pitch", 100) or 100),
            )),
            "autoplay_bgs": bool(mdata.get("autoplayBgs", False)),
            "bgs": ruby_to_ir(make_audio_file(
                str(map_bgs.get("name", "") or ""),
                int(map_bgs.get("volume", 80) or 80),
                int(map_bgs.get("pitch", 100) or 100),
            )),
            "parallax_name": str(mdata.get("parallaxName", "") or ""),
            "parallax_loop_x": bool(mdata.get("parallaxLoopX", False)),
            "parallax_loop_y": bool(mdata.get("parallaxLoopY", False)),
            "layers": layers,
            "events": events_out,
            "encounter_list": enc_list,
            "encounter_weights": enc_weights,
            "encounter_step": int(mdata.get("encounterStep", 30) or 30),
        }
        _save_json(maps_dir / f"map_{mid:03d}.json", map_ir)
        proj["map_infos"][str(mid)] = _make_map_info_ir(mname)
        imported_maps += 1

    _save_json(proj_path, proj)
    for src_sub, dst_sub in (("img", "Graphics"), ("audio", "Audio")):
        sdir = src_dir / src_sub
        ddir = out_ir_dir / "assets" / dst_sub
        if sdir.is_dir() and not ddir.exists():
            shutil.copytree(sdir, ddir)

    return {"ir_dir": str(out_ir_dir.resolve()), "title": title, "maps_imported": imported_maps}


def import_tiled_map(
    tiled_path: Path,
    ir_dir: Path,
    map_id: Optional[int] = None,
    tileset_id: int = 1,
) -> Dict[str, Any]:
    """Imports a Tiled JSON (.tmj/.json) or XML (.tmx) map file into an existing IR workspace."""
    tiled_path = Path(tiled_path)
    ir_dir = Path(ir_dir)
    proj_path = ir_dir / "ir_project.json"
    proj = _load_json(proj_path)

    if map_id is None:
        existing_ids = [int(p.stem.split("_")[1]) for p in (ir_dir / "maps").glob("map_*.json") if "_" in p.stem]
        map_id = (max(existing_ids) + 1) if existing_ids else 1

    tile_layers: List[List[int]] = []
    objects: List[Dict[str, Any]] = []
    tile_rules: Dict[int, Dict[str, Any]] = {}

    def _collect_tsx_tile_rules(tsx_root: ET.Element, first_gid: int) -> None:
        for t_el in tsx_root.findall("tile"):
            tid_loc = int(t_el.attrib.get("id", 0) or 0)
            gid = first_gid + tid_loc
            pel = t_el.find("properties")
            if pel is None:
                continue
            pdict: Dict[str, Any] = {}
            for pr in pel.findall("property"):
                pname = str(pr.attrib.get("name", ""))
                pval = pr.attrib.get("value", "")
                ptype = str(pr.attrib.get("type", ""))
                if ptype == "bool" or str(pval).lower() in ("true", "false"):
                    pdict[pname] = str(pval).lower() == "true"
                elif ptype == "int":
                    pdict[pname] = int(pval or 0)
                else:
                    pdict[pname] = pval
            if pdict:
                tile_rules[gid] = pdict

    if tiled_path.suffix.lower() in (".tmj", ".json"):
        doc = _load_json(tiled_path)
        w = max(1, int(doc.get("width", 20)))
        h = max(1, int(doc.get("height", 15)))
        tw = max(1, int(doc.get("tilewidth", 32)))
        th = max(1, int(doc.get("tileheight", 32)))
        for ts_ref in doc.get("tilesets", []):
            fg = int(ts_ref.get("firstgid", 1) or 1)
            src_rel = str(ts_ref.get("source", "") or "")
            if src_rel and (tiled_path.parent / src_rel).is_file():
                _collect_tsx_tile_rules(ET.parse(str(tiled_path.parent / src_rel)).getroot(), fg)
            for t_row in ts_ref.get("tiles", []):
                gid = fg + int(t_row.get("id", 0) or 0)
                pdict = {str(p.get("name")): p.get("value") for p in t_row.get("properties", []) if isinstance(p, dict)}
                if pdict:
                    tile_rules[gid] = pdict
        for layer in doc.get("layers", []):
            ltype = layer.get("type")
            if ltype == "tilelayer" and isinstance(layer.get("data"), list):
                tile_layers.append([int(v) for v in layer["data"]])
            elif ltype == "objectgroup":
                for obj in layer.get("objects", []):
                    props = {str(p.get("name")): p.get("value") for p in obj.get("properties", []) if isinstance(p, dict)}
                    objects.append({
                        "name": str(obj.get("name", f"Obj{len(objects) + 1}")),
                        "gx": max(0, min(w - 1, int(round(float(obj.get("x", 0)) / tw)))),
                        "gy": max(0, min(h - 1, int(round(float(obj.get("y", 0)) / th)))),
                        "say": props.get("say"),
                    })
        mname = str(doc.get("name", tiled_path.stem))
    else:
        root = ET.parse(str(tiled_path)).getroot()
        w = max(1, int(root.attrib.get("width", 20)))
        h = max(1, int(root.attrib.get("height", 15)))
        tw = max(1, int(root.attrib.get("tilewidth", 32)))
        th = max(1, int(root.attrib.get("tileheight", 32)))
        mname = tiled_path.stem
        for ts_el in root.findall("tileset"):
            fg = int(ts_el.attrib.get("firstgid", 1) or 1)
            src_rel = str(ts_el.attrib.get("source", "") or "")
            if src_rel and (tiled_path.parent / src_rel).is_file():
                _collect_tsx_tile_rules(ET.parse(str(tiled_path.parent / src_rel)).getroot(), fg)
            _collect_tsx_tile_rules(ts_el, fg)
        for layer in root.findall("layer"):
            data_el = layer.find("data")
            if data_el is not None and data_el.text:
                vals = [int(x.strip()) for x in data_el.text.replace("\n", ",").split(",") if x.strip()]
                tile_layers.append(vals)
        for ogrp in root.findall("objectgroup"):
            for obj in ogrp.findall("object"):
                props: Dict[str, Any] = {}
                pel = obj.find("properties")
                if pel is not None:
                    for pr in pel.findall("property"):
                        props[str(pr.attrib.get("name", ""))] = pr.attrib.get("value", "")
                objects.append({
                    "name": str(obj.attrib.get("name", f"Obj{len(objects) + 1}")),
                    "gx": max(0, min(w - 1, int(round(float(obj.attrib.get("x", 0)) / tw)))),
                    "gy": max(0, min(h - 1, int(round(float(obj.attrib.get("y", 0)) / th)))),
                    "say": props.get("say"),
                })

    gid_to_tid: Dict[int, int] = {}
    layers = _create_empty_layers(w, h)
    for z, flat in enumerate(tile_layers[:3]):
        grid = layers[f"layer_{z}"]
        for y in range(h):
            for x in range(w):
                idx = y * w + x
                gid = flat[idx] if idx < len(flat) else 0
                if gid == 0:
                    grid[y][x] = _TILE_BASE_GRASS if z == 0 else 0
                else:
                    rmxp_tid = gid if gid >= 48 else (_TILE_BASE_GRASS + gid - 1)
                    grid[y][x] = rmxp_tid
                    gid_to_tid[gid] = rmxp_tid

    rules_applied = 0
    ts_db_path = ir_dir / "database" / "tilesets.json"
    if tile_rules and ts_db_path.exists():
        ts_db = _load_json(ts_db_path)
        ts_idx = int(tileset_id)
        if 0 < ts_idx < len(ts_db) and isinstance(ts_db[ts_idx], dict):
            ts_f = ts_db[ts_idx].get("fields", {})
            passages = ts_f.get("passages", {}).get("data", [])
            priorities = ts_f.get("priorities", {}).get("data", [])
            for gid, rprops in tile_rules.items():
                rmxp_tid = gid_to_tid.get(gid, gid if gid >= 48 else (_TILE_BASE_GRASS + gid - 1))
                while len(passages) <= rmxp_tid:
                    passages.append(0)
                while len(priorities) <= rmxp_tid:
                    priorities.append(0)
                if "passable" in rprops:
                    passages[rmxp_tid] = 0 if bool(rprops["passable"]) else 15
                    rules_applied += 1
                if "priority" in rprops:
                    priorities[rmxp_tid] = max(0, min(5, int(rprops["priority"])))
                    rules_applied += 1
            _save_json(ts_db_path, ts_db)

    events_out: List[Dict[str, Any]] = []
    for e_idx, obj in enumerate(objects, start=1):
        cmds = [{"op": "say", "lines": str(obj["say"]).split("|"), "indent": 0}] if obj.get("say") else []
        ev_ruby = make_event(e_idx, obj["name"], obj["gx"], obj["gy"], [make_event_page()])
        ev_ir = ruby_to_ir(ev_ruby)
        events_out.append({
            "id": e_idx,
            "name": obj["name"],
            "x": obj["gx"],
            "y": obj["gy"],
            "pages": [{
                "condition": ev_ir["fields"]["pages"][0]["fields"]["condition"],
                "graphic": ev_ir["fields"]["pages"][0]["fields"]["graphic"],
                "trigger": 0,
                "through": False,
                "walk_anime": True,
                "step_anime": False,
                "direction_fix": False,
                "always_on_top": False,
                "move_type": 0,
                "move_speed": 3,
                "move_frequency": 3,
                "commands": cmds,
            }],
        })

    map_ir = {
        "map_id": int(map_id),
        "name": mname,
        "width": w,
        "height": h,
        "tileset_id": int(tileset_id),
        "layers": layers,
        "events": events_out,
        "encounter_list": [],
        "encounter_step": 30,
    }
    _save_json(ir_dir / "maps" / f"map_{int(map_id):03d}.json", map_ir)
    _upsert_map_info(proj, int(map_id), _make_map_info_ir(mname))
    _save_json(proj_path, proj)
    return {
        "map_id": int(map_id),
        "name": mname,
        "width": w,
        "height": h,
        "events": len(events_out),
        "tileset_rules_applied": rules_applied,
    }


def _load_ruby_data_table(data_dir: Path, base_name: str) -> Optional[List[Any]]:
    for ext in (".rvdata2", ".rxdata"):
        cand = data_dir / f"{base_name}{ext}"
        if cand.exists():
            raw = marshal_loads(cand.read_bytes())
            return raw if isinstance(raw, list) else None
    return None


def _patch_table_from_ruby(
    db_path: Path,
    ruby_rows: List[Any],
    field_mapper: Any,
) -> int:
    existing = _load_json(db_path)
    template = copy.deepcopy(existing[1]) if len(existing) > 1 and existing[1] else {"__ruby_class__": "RPG::Actor", "fields": {}}
    out: List[Any] = [None]
    count = 0
    for idx, item in enumerate(ruby_rows):
        if item is None:
            continue
        ir_item = ruby_to_ir(item)
        src_f = ir_item.get("fields", {}) if isinstance(ir_item, dict) else {}
        eid = int(src_f.get("id", idx) or idx)
        if eid <= 0:
            continue
        while len(out) <= eid:
            out.append(None)
        rec = copy.deepcopy(template)
        rec["fields"]["id"] = eid
        field_mapper(rec["fields"], src_f)
        out[eid] = rec
        count += 1
    if len(out) > 1:
        _save_json(db_path, out)
    return count


def import_vxace_rxdata(vxace_dir: Path, out_ir_dir: Path) -> Dict[str, Any]:
    """Converts RPG Maker VX Ace (.rvdata2/.rxdata) maps, DB tables, and common events into an IR workspace."""
    vxace_dir = Path(vxace_dir)
    if not vxace_dir.is_dir():
        raise FileNotFoundError(f"VX Ace project directory not found: {vxace_dir}")
    data_dir = vxace_dir / "Data" if (vxace_dir / "Data").is_dir() else vxace_dir
    out_ir_dir = Path(out_ir_dir)

    has_sys = (data_dir / "System.rvdata2").is_file() or (data_dir / "System.rxdata").is_file()
    has_maps = bool(list(data_dir.glob("Map[0-9][0-9][0-9].rvdata2")) or list(data_dir.glob("Map[0-9][0-9][0-9].rxdata")))
    if not (has_sys or has_maps):
        raise ValueError(f"No valid VX Ace/XP data files (System/Map .rvdata2/.rxdata) found in: {data_dir}")

    sys_path = data_dir / "System.rvdata2"
    if not sys_path.exists():
        sys_path = data_dir / "System.rxdata"
    sys_obj = marshal_loads(sys_path.read_bytes()) if sys_path.exists() else None
    sys_iv = getattr(sys_obj, "ivars", {}) if sys_obj is not None else {}
    title = str(sys_iv.get("@game_title", "VX Ace Imported RPG") or "VX Ace Imported RPG")
    _bootstrap_ir_workspace(out_ir_dir, title)

    proj_path = out_ir_dir / "ir_project.json"
    proj = _load_json(proj_path)
    sf = proj["system"]["fields"]
    if "@start_map_id" in sys_iv:
        sf["start_map_id"] = int(sys_iv["@start_map_id"])
    if "@start_x" in sys_iv:
        sf["start_x"] = int(sys_iv["@start_x"])
    if "@start_y" in sys_iv:
        sf["start_y"] = int(sys_iv["@start_y"])
    if isinstance(sys_iv.get("@party_members"), list) and sys_iv["@party_members"]:
        sf["party_members"] = [int(x) for x in sys_iv["@party_members"]]
    if isinstance(sys_iv.get("@switches"), list) and sys_iv["@switches"]:
        sf["switches"] = list(sys_iv["@switches"])
    if isinstance(sys_iv.get("@variables"), list) and sys_iv["@variables"]:
        sf["variables"] = list(sys_iv["@variables"])

    db_dir = out_ir_dir / "database"
    db_counts: Dict[str, int] = {}

    def _map_vx_enemy(f: Dict[str, Any], s: Dict[str, Any]) -> None:
        params = s.get("params", [])
        f["name"] = str(s.get("name", f.get("name", "")))
        f["battler_name"] = str(s.get("battler_name", f.get("battler_name", "")))
        if isinstance(params, list) and len(params) >= 4:
            f["maxhp"] = int(params[0] or f.get("maxhp", 80))
            f["atk"] = int(params[2] or f.get("atk", 14))
            f["pdef"] = int(params[3] or f.get("pdef", 6))
        else:
            for k in ("maxhp", "atk", "pdef", "str"):
                if k in s:
                    f[k] = int(s[k])
        f["exp"] = int(s.get("exp", f.get("exp", 10)) or 10)
        f["gold"] = int(s.get("gold", f.get("gold", 15)) or 15)

    def _map_vx_weapon(f: Dict[str, Any], s: Dict[str, Any]) -> None:
        params = s.get("params", [])
        f["name"] = str(s.get("name", f.get("name", "")))
        f["price"] = int(s.get("price", f.get("price", 0)) or 0)
        f["atk"] = int(params[2]) if isinstance(params, list) and len(params) > 2 else int(s.get("atk", f.get("atk", 0)) or 0)

    def _map_vx_armor(f: Dict[str, Any], s: Dict[str, Any]) -> None:
        params = s.get("params", [])
        f["name"] = str(s.get("name", f.get("name", "")))
        f["price"] = int(s.get("price", f.get("price", 0)) or 0)
        f["pdef"] = int(params[3]) if isinstance(params, list) and len(params) > 3 else int(s.get("pdef", f.get("pdef", 0)) or 0)
        f["mdef"] = int(params[5]) if isinstance(params, list) and len(params) > 5 else int(s.get("mdef", f.get("mdef", 0)) or 0)

    def _map_vx_troop(f: Dict[str, Any], s: Dict[str, Any]) -> None:
        f["name"] = str(s.get("name", f.get("name", "")))
        if isinstance(s.get("members"), list) and s["members"]:
            f["members"] = copy.deepcopy(s["members"])

    table_specs = (
        ("Actors", "actors.json", lambda f, s: f.update({"name": str(s.get("name", "")), "class_id": int(s.get("class_id", 1) or 1), "initial_level": int(s.get("initial_level", 1) or 1), "character_name": str(s.get("character_name", "")), "battler_name": str(s.get("battler_name", ""))})),
        ("Classes", "classes.json", lambda f, s: f.update({"name": str(s.get("name", ""))})),
        ("Skills", "skills.json", lambda f, s: f.update({"name": str(s.get("name", "")), "description": str(s.get("description", "")), "sp_cost": int(s.get("mp_cost", s.get("sp_cost", 0)) or 0), "power": int(s.get("power", 0) or 0)})),
        ("Items", "items.json", lambda f, s: f.update({"name": str(s.get("name", "")), "description": str(s.get("description", "")), "price": int(s.get("price", 0) or 0), "consumable": bool(s.get("consumable", True)), "recover_hp": int(s.get("recover_hp", 0) or 0), "recover_sp": int(s.get("recover_sp", 0) or 0)})),
        ("Weapons", "weapons.json", _map_vx_weapon),
        ("Armors", "armors.json", _map_vx_armor),
        ("Enemies", "enemies.json", _map_vx_enemy),
        ("Troops", "troops.json", _map_vx_troop),
        ("States", "states.json", lambda f, s: f.update({"name": str(s.get("name", "")), "restriction": int(s.get("restriction", 0) or 0)})),
    )
    for base_name, json_name, mapper in table_specs:
        rows = _load_ruby_data_table(data_dir, base_name)
        if rows is not None:
            db_counts[base_name.lower()] = _patch_table_from_ruby(db_dir / json_name, rows, mapper)

    for aud_k in ("title_bgm", "battle_bgm", "battle_end_me", "gameover_me"):
        if f"@{aud_k}" in sys_iv:
            _apply_audio_spec_fields(sf, aud_k, ruby_to_ir(sys_iv[f"@{aud_k}"]))

    ts_rows = _load_ruby_data_table(data_dir, "Tilesets")
    if ts_rows is not None:
        db_counts["tilesets"] = _apply_tile_flags_to_tilesets(db_dir, ts_rows, is_ruby_rows=True)

    ce_rows = _load_ruby_data_table(data_dir, "CommonEvents")
    if ce_rows is not None:
        ce_out: List[Any] = [None]
        for idx, ce_obj in enumerate(ce_rows):
            if ce_obj is None:
                continue
            ce_f = ruby_to_ir(ce_obj).get("fields", {})
            eid = int(ce_f.get("id", idx) or idx)
            if eid <= 0:
                continue
            while len(ce_out) <= eid:
                ce_out.append(None)
            raw_cmds = [c.get("fields", c) for c in ce_f.get("list", []) if isinstance(c, dict)]
            ce_out[eid] = {
                "id": eid,
                "name": str(ce_f.get("name", "")),
                "trigger": int(ce_f.get("trigger", 0) or 0),
                "switch_id": int(ce_f.get("switch_id", 1) or 1),
                "commands": _convert_mv_commands(raw_cmds),
            }
        _save_json(db_dir / "common_events.json", ce_out)
        db_counts["common_events"] = sum(1 for c in ce_out[1:] if c)

    minfo_path = data_dir / "MapInfos.rvdata2"
    if not minfo_path.exists():
        minfo_path = data_dir / "MapInfos.rxdata"
    vx_names: Dict[int, str] = {}
    if minfo_path.exists():
        mi_raw = marshal_loads(minfo_path.read_bytes())
        if isinstance(mi_raw, dict):
            for k, v in mi_raw.items():
                if hasattr(v, "ivars"):
                    vx_names[int(k)] = str(v.ivars.get("@name", f"Map {k}"))

    proj["map_infos"] = {}
    maps_dir = out_ir_dir / "maps"
    for existing in maps_dir.glob("map_*.json"):
        existing.unlink()

    imported = 0
    for mp in sorted(list(data_dir.glob("Map[0-9][0-9][0-9].rvdata2")) + list(data_dir.glob("Map[0-9][0-9][0-9].rxdata"))):
        mid = int(mp.stem[3:])
        m_obj = marshal_loads(mp.read_bytes())
        m_iv = getattr(m_obj, "ivars", {})
        w = max(1, int(m_iv.get("@width", 20)))
        h = max(1, int(m_iv.get("@height", 15)))
        m_ir_raw = ruby_to_ir(m_obj)
        layers = _create_empty_layers(w, h)
        tbl = m_ir_raw.get("fields", {}).get("data", {})
        raw_vals = tbl.get("data", []) if isinstance(tbl, dict) else []
        if raw_vals:
            for z in range(min(3, int(tbl.get("zsize", 3) or 3))):
                lgrid = layers[f"layer_{z}"]
                for y in range(h):
                    for x in range(w):
                        idx = (z * h + y) * w + x
                        if idx < len(raw_vals):
                            v = int(raw_vals[idx])
                            lgrid[y][x] = _mv_tile_to_rmxp(v) if v > 0 else (_TILE_BASE_GRASS if z == 0 else 0)
        events_out: List[Dict[str, Any]] = []
        raw_evs = m_ir_raw.get("fields", {}).get("events", {})
        if isinstance(raw_evs, dict):
            ev_items = raw_evs.get("entries", raw_evs.items())
            if isinstance(ev_items, dict):
                ev_pairs = [(k, v) for k, v in ev_items.items() if str(k).isdigit()]
            elif isinstance(ev_items, list):
                ev_pairs = [(int(pair[0]), pair[1]) for pair in ev_items if isinstance(pair, (list, tuple)) and len(pair) == 2]
            else:
                ev_pairs = [(k, v) for k, v in raw_evs.items() if str(k).isdigit()]
            for eid_k, ev_val in sorted(ev_pairs, key=lambda kv: int(kv[0])):
                ef = ev_val.get("fields", {})
                pages_out: List[Dict[str, Any]] = []
                for pg in ef.get("pages", []):
                    pf = pg.get("fields", {})
                    raw_cmds = [c.get("fields", c) for c in pf.get("list", []) if isinstance(c, dict)]
                    pages_out.append({
                        "condition": pf.get("condition", ruby_to_ir(make_event_page().ivars["@condition"])),
                        "graphic": pf.get("graphic", ruby_to_ir(make_event_page().ivars["@graphic"])),
                        "trigger": int(pf.get("trigger", 0) or 0),
                        "through": bool(pf.get("through", False)),
                        "walk_anime": bool(pf.get("walk_anime", True)),
                        "step_anime": bool(pf.get("step_anime", False)),
                        "direction_fix": bool(pf.get("direction_fix", False)),
                        "always_on_top": int(pf.get("priority_type", 1)) == 2,
                        "move_type": int(pf.get("move_type", 0) or 0),
                        "move_speed": int(pf.get("move_speed", 3) or 3),
                        "move_frequency": int(pf.get("move_frequency", 3) or 3),
                        "commands": _convert_mv_commands(raw_cmds),
                    })
                events_out.append({
                    "id": int(eid_k),
                    "name": str(ef.get("name", f"EV{eid_k}")),
                    "x": int(ef.get("x", 0)),
                    "y": int(ef.get("y", 0)),
                    "pages": pages_out,
                })
        mname = vx_names.get(mid, str(m_ir_raw.get("fields", {}).get("display_name", "") or f"Map {mid}"))
        m_fields = m_ir_raw.get("fields", {})
        vx_enc_list: List[int] = []
        vx_enc_weights: List[Dict[str, Any]] = []
        for enc in m_fields.get("encounter_list", []):
            ef_e = enc.get("fields", enc) if isinstance(enc, dict) else {}
            if isinstance(ef_e, dict) and int(ef_e.get("troop_id", 0) or 0) > 0:
                tid = int(ef_e.get("troop_id", 1) or 1)
                vx_enc_list.append(tid)
                vx_enc_weights.append({
                    "troop_id": tid,
                    "weight": max(1, int(ef_e.get("weight", 10) or 10)),
                    "region_set": list(ef_e.get("region_set", [])),
                })
        vx_bgm_raw = m_fields.get("bgm", {})
        vx_bgm = vx_bgm_raw.get("fields", vx_bgm_raw) if isinstance(vx_bgm_raw, dict) else {}
        vx_bgs_raw = m_fields.get("bgs", {})
        vx_bgs = vx_bgs_raw.get("fields", vx_bgs_raw) if isinstance(vx_bgs_raw, dict) else {}
        map_ir = {
            "map_id": mid,
            "name": mname,
            "width": w,
            "height": h,
            "tileset_id": max(1, int(m_fields.get("tileset_id", 1) or 1)),
            "autoplay_bgm": bool(m_fields.get("autoplay_bgm", False)),
            "bgm": ruby_to_ir(make_audio_file(
                str(vx_bgm.get("name", "") or ""),
                int(vx_bgm.get("volume", 80) or 80),
                int(vx_bgm.get("pitch", 100) or 100),
            )),
            "autoplay_bgs": bool(m_fields.get("autoplay_bgs", False)),
            "bgs": ruby_to_ir(make_audio_file(
                str(vx_bgs.get("name", "") or ""),
                int(vx_bgs.get("volume", 80) or 80),
                int(vx_bgs.get("pitch", 100) or 100),
            )),
            "parallax_name": str(m_fields.get("parallax_name", "") or ""),
            "parallax_loop_x": bool(m_fields.get("parallax_loop_x", False)),
            "parallax_loop_y": bool(m_fields.get("parallax_loop_y", False)),
            "layers": layers,
            "events": events_out,
            "encounter_list": vx_enc_list,
            "encounter_weights": vx_enc_weights,
            "encounter_step": int(m_iv.get("@encounter_step", 30) or 30),
        }
        _save_json(maps_dir / f"map_{mid:03d}.json", map_ir)
        proj["map_infos"][str(mid)] = _make_map_info_ir(mname)
        imported += 1

    _save_json(proj_path, proj)
    return {
        "ir_dir": str(out_ir_dir.resolve()),
        "title": title,
        "maps_imported": imported,
        "tables_imported": db_counts,
    }
