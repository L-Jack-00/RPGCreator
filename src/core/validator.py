# -*- coding: utf-8 -*-
"""
Headless Project Integrity Validator & Map Preview Renderer.
Verifies directional 4-bit tile passability, priority layer overlays,
conditional event page transitions, cross-map warps, and multi-map BFS reachability.
"""
from __future__ import annotations

import random
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from PIL import Image, ImageDraw

from .autotile import resolve_autotile_quads, subtile_pixel_rect
from .utils import load_json as _load_json

DIR_OUT_BIT = {
    (0, 1): 0x01,   # Down
    (-1, 0): 0x02,  # Left
    (1, 0): 0x04,   # Right
    (0, -1): 0x08,  # Up
}
DIR_IN_BIT = {
    (0, 1): 0x08,   # Entering from top -> check Up bit on dest
    (-1, 0): 0x04,  # Entering from right -> check Right bit on dest
    (1, 0): 0x02,   # Entering from left -> check Left bit on dest
    (0, -1): 0x01,  # Entering from bottom -> check Down bit on dest
}


@dataclass
class ValidationResult:
    """Structured integrity report for an IR project workspace."""
    passed: bool
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    stats: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "passed": self.passed,
            "errors": self.errors,
            "warnings": self.warnings,
            "stats": self.stats,
        }


def build_tileset_tables(
    tilesets_ir: List[Optional[Dict[str, Any]]]
) -> Tuple[Dict[int, List[int]], Dict[int, List[int]]]:
    """Returns (passages_by_ts, priorities_by_ts) indexed by tileset_id."""
    passages_by_ts: Dict[int, List[int]] = {}
    priorities_by_ts: Dict[int, List[int]] = {}
    for ts in tilesets_ir:
        if not ts:
            continue
        fields = ts["fields"]
        ts_id = int(fields["id"])
        passages_by_ts[ts_id] = list(fields["passages"]["data"])
        priorities_by_ts[ts_id] = list(fields["priorities"]["data"])
    return passages_by_ts, priorities_by_ts


def build_tileset_terrain_tables(
    tilesets_ir: List[Optional[Dict[str, Any]]]
) -> Dict[int, List[int]]:
    """Returns terrain_tags_by_ts indexed by tileset_id."""
    terrain_by_ts: Dict[int, List[int]] = {}
    for ts in tilesets_ir:
        if not ts:
            continue
        fields = ts["fields"]
        ts_id = int(fields["id"])
        terrain_by_ts[ts_id] = list(fields.get("terrain_tags", {}).get("data", []))
    return terrain_by_ts


def _check_tile_pass_flag(
    map_ir: Dict[str, Any],
    passages_by_ts: Dict[int, List[int]],
    priorities_by_ts: Dict[int, List[int]],
    x: int,
    y: int,
    bit: int,
    require_all: bool = False,
) -> bool:
    """Evaluates RMXP 3-layer stack passability against a bitmask."""
    w = int(map_ir["width"])
    h = int(map_ir["height"])
    if x < 0 or y < 0 or x >= w or y >= h:
        return False
    ts_id = int(map_ir["tileset_id"])
    passages = passages_by_ts.get(ts_id, [])
    priorities = priorities_by_ts.get(ts_id, [])

    layers = map_ir["layers"]
    for z in ("layer_2", "layer_1", "layer_0"):
        tid = int(layers[z][y][x])
        if tid == 0:
            continue
        p_flag = passages[tid] if tid < len(passages) else 0
        prio = priorities[tid] if tid < len(priorities) else 0
        blocked = (p_flag & bit) == bit if require_all else (p_flag & bit) != 0
        if blocked:
            return False
        if prio == 0:
            return True
    return True


def _tile_allows_bit(
    map_ir: Dict[str, Any],
    passages_by_ts: Dict[int, List[int]],
    priorities_by_ts: Dict[int, List[int]],
    x: int,
    y: int,
    bit: int,
) -> bool:
    """Evaluates RMXP 3-layer stack passability for a specific directional bitmask."""
    return _check_tile_pass_flag(map_ir, passages_by_ts, priorities_by_ts, x, y, bit, require_all=False)


def is_tile_standable(
    map_ir: Dict[str, Any],
    passages_by_ts: Dict[int, List[int]],
    priorities_by_ts: Dict[int, List[int]],
    x: int,
    y: int,
) -> bool:
    """Checks whether a tile at (x, y) is in bounds and not fully blocked (0x0F)."""
    return _check_tile_pass_flag(map_ir, passages_by_ts, priorities_by_ts, x, y, 0x0F, require_all=True)


def select_active_page(
    event_ir: Dict[str, Any],
    map_id: int,
    switches: Dict[int, bool],
    variables: Dict[int, int],
    self_switches: Dict[Tuple[int, int, str], bool],
) -> Optional[Dict[str, Any]]:
    """Returns the highest-indexed event page whose conditions are satisfied."""
    ev_id = int(event_ir["id"])
    for page in reversed(event_ir.get("pages", [])):
        cond_fields = page.get("condition", {}).get("fields", {})
        if cond_fields.get("switch1_valid") and not switches.get(int(cond_fields.get("switch1_id", 1)), False):
            continue
        if cond_fields.get("switch2_valid") and not switches.get(int(cond_fields.get("switch2_id", 1)), False):
            continue
        if cond_fields.get("variable_valid"):
            vid = int(cond_fields.get("variable_id", 1))
            req = int(cond_fields.get("variable_value", 0))
            if variables.get(vid, 0) < req:
                continue
        if cond_fields.get("self_switch_valid"):
            ch = str(cond_fields.get("self_switch_ch", "A"))
            if not self_switches.get((map_id, ev_id, ch), False):
                continue
        return page
    return None


def can_move_step(
    map_ir: Dict[str, Any],
    passages_by_ts: Dict[int, List[int]],
    priorities_by_ts: Dict[int, List[int]],
    blocking_coords: Set[Tuple[int, int]],
    x: int,
    y: int,
    dx: int,
    dy: int,
) -> bool:
    """Evaluates directional 4-bit RMXP movement from (x, y) by (dx, dy)."""
    out_bit = DIR_OUT_BIT.get((dx, dy))
    in_bit = DIR_IN_BIT.get((dx, dy))
    if out_bit is None or in_bit is None:
        return False
    nx, ny = x + dx, y + dy
    if (nx, ny) in blocking_coords:
        return False
    if not _tile_allows_bit(map_ir, passages_by_ts, priorities_by_ts, x, y, out_bit):
        return False
    if not _tile_allows_bit(map_ir, passages_by_ts, priorities_by_ts, nx, ny, in_bit):
        return False
    return True


def _blocking_coords_for_map(
    map_id: int,
    map_ir: Dict[str, Any],
    switches: Dict[int, bool],
    variables: Dict[int, int],
    self_switches: Dict[Tuple[int, int, str], bool],
) -> Set[Tuple[int, int]]:
    blocked: Set[Tuple[int, int]] = set()
    for ev in map_ir.get("events", []):
        active = select_active_page(ev, map_id, switches, variables, self_switches)
        if active is None:
            continue
        if not active.get("through", False) and active.get("trigger_code", 0) != 1:
            blocked.add((int(ev["x"]), int(ev["y"])))
    return blocked


def _skip_branch_to(cmds: List[Dict[str, Any]], start_idx: int, base_indent: int, stop_ops: Tuple[str, ...]) -> int:
    """Finds the matching sibling or closing branch command at base_indent."""
    j = start_idx + 1
    while j < len(cmds):
        c = cmds[j]
        if int(c.get("indent", 0)) == base_indent and c.get("op") in stop_ops:
            return j
        j += 1
    return len(cmds)


def eval_variable_cmp(lhs: int, rhs: int, cmp_op: int) -> bool:
    """Evaluates RMXP variable comparison codes (0:==, 1:>=, 2:<=, 3:>, 4:<, 5:!=)."""
    if cmp_op == 0:
        return lhs == rhs
    if cmp_op == 1:
        return lhs >= rhs
    if cmp_op == 2:
        return lhs <= rhs
    if cmp_op == 3:
        return lhs > rhs
    if cmp_op == 4:
        return lhs < rhs
    if cmp_op == 5:
        return lhs != rhs
    return False


def _apply_variable_op(cur: int, op_code: int, rhs: int) -> int:
    if op_code == 0:
        return rhs
    if op_code == 1:
        return cur + rhs
    if op_code == 2:
        return cur - rhs
    if op_code == 3:
        return cur * rhs
    if op_code == 4:
        return cur // rhs if rhs != 0 else cur
    if op_code == 5:
        return cur % rhs if rhs != 0 else cur
    return cur


def _apply_reachable_command_mutations(
    cmds: List[Dict[str, Any]],
    map_id: int,
    event_id: int,
    ex: int,
    ey: int,
    switches_state: Dict[int, bool],
    variables_state: Dict[int, int],
    self_switches_state: Dict[Tuple[int, int, str], bool],
    items_owned: Set[int],
    warp_lookup: Dict[Tuple[int, int, int], Set[Tuple[int, int, int]]],
    valid_map_ids: Set[int],
    start_idx: int = 0,
    end_idx: Optional[int] = None,
    common_events_by_id: Optional[Dict[int, Dict[str, Any]]] = None,
    _ce_stack: Optional[Set[int]] = None,
) -> bool:
    """
    Walks semantic commands respecting conditional branches (if_switch, if_variable,
    if_item, show_choices/when_choice) and applies reachable state & warp mutations.
    Returns True if any switch, variable, self-switch, item, or warp unlocked.
    """
    changed = False
    limit = len(cmds) if end_idx is None else min(end_idx, len(cmds))
    i = start_idx

    while i < limit:
        cmd = cmds[i]
        op = cmd.get("op")
        indent = int(cmd.get("indent", 0))

        if op == "set_switch" and str(cmd.get("value", "ON")).upper() == "ON":
            for sid in range(int(cmd["start_id"]), int(cmd["end_id"]) + 1):
                if not switches_state.get(sid, False):
                    switches_state[sid] = True
                    changed = True
        elif op == "set_variable":
            operand = cmd.get("operand", [0])
            rhs = int(operand[0]) if operand and int(cmd.get("operand_type", 0)) == 0 else 0
            for vid in range(int(cmd["start_id"]), int(cmd["end_id"]) + 1):
                nxt = _apply_variable_op(variables_state.get(vid, 0), int(cmd.get("operation", 0)), rhs)
                if variables_state.get(vid, 0) != nxt:
                    variables_state[vid] = nxt
                    changed = True
        elif op == "set_self_switch" and str(cmd.get("value", "ON")).upper() == "ON":
            key = (map_id, event_id, str(cmd.get("channel", "A")))
            if not self_switches_state.get(key, False):
                self_switches_state[key] = True
                changed = True
        elif op == "change_item" and cmd.get("mode", "add") == "add":
            iid = int(cmd["item_id"])
            if iid not in items_owned:
                items_owned.add(iid)
                changed = True
        elif op == "transfer_player":
            tm, tx, ty = int(cmd["map_id"]), int(cmd["x"]), int(cmd["y"])
            if tm in valid_map_ids:
                dest = (tm, tx, ty)
                slot = warp_lookup.setdefault((map_id, ex, ey), set())
                if dest not in slot:
                    slot.add(dest)
                    changed = True
        elif op in ("if_switch", "if_item", "if_variable"):
            if op == "if_switch":
                cond_ok = switches_state.get(int(cmd["switch_id"]), False) == (str(cmd.get("expected", "ON")).upper() == "ON")
            elif op == "if_item":
                cond_ok = int(cmd["item_id"]) in items_owned
            else:
                lhs = variables_state.get(int(cmd["variable_id"]), 0)
                rhs = variables_state.get(int(cmd["rhs_value"]), 0) if cmd.get("rhs_is_var") else int(cmd.get("rhs_value", 0))
                cond_ok = eval_variable_cmp(lhs, rhs, int(cmd.get("cmp", 0)))

            else_or_end = _skip_branch_to(cmds, i, indent, ("else", "end_if"))
            end_pos = (
                _skip_branch_to(cmds, else_or_end, indent, ("end_if",))
                if else_or_end < limit and cmds[else_or_end].get("op") == "else"
                else else_or_end
            )
            if cond_ok:
                if _apply_reachable_command_mutations(
                    cmds, map_id, event_id, ex, ey, switches_state, variables_state,
                    self_switches_state, items_owned, warp_lookup, valid_map_ids,
                    i + 1, else_or_end, common_events_by_id, _ce_stack
                ):
                    changed = True
            elif else_or_end < limit and cmds[else_or_end].get("op") == "else":
                if _apply_reachable_command_mutations(
                    cmds, map_id, event_id, ex, ey, switches_state, variables_state,
                    self_switches_state, items_owned, warp_lookup, valid_map_ids,
                    else_or_end + 1, end_pos, common_events_by_id, _ce_stack
                ):
                    changed = True
            i = end_pos + 1
            continue
        elif op == "show_choices":
            end_pos = _skip_branch_to(cmds, i, indent, ("end_choices",))
            if _apply_reachable_command_mutations(
                cmds, map_id, event_id, ex, ey, switches_state, variables_state,
                self_switches_state, items_owned, warp_lookup, valid_map_ids,
                i + 1, end_pos, common_events_by_id, _ce_stack
            ):
                changed = True
            i = end_pos + 1
            continue
        elif op == "battle" and i + 1 < limit and cmds[i + 1].get("op") in ("when_win", "when_escape", "when_lose") and int(cmds[i + 1].get("indent", 0)) == indent:
            win_idx = _skip_branch_to(cmds, i, indent, ("when_win", "end_battle"))
            end_pos = _skip_branch_to(cmds, i, indent, ("end_battle",))
            if win_idx < end_pos and cmds[win_idx].get("op") == "when_win":
                win_end = _skip_branch_to(cmds, win_idx, indent, ("when_escape", "when_lose", "end_battle"))
                if _apply_reachable_command_mutations(
                    cmds, map_id, event_id, ex, ey, switches_state, variables_state,
                    self_switches_state, items_owned, warp_lookup, valid_map_ids,
                    win_idx + 1, win_end, common_events_by_id, _ce_stack
                ):
                    changed = True
            i = end_pos + 1
            continue
        elif op == "common_event" and common_events_by_id:
            cid = int(cmd.get("common_event_id", 0))
            ce_stack = _ce_stack or set()
            ce = common_events_by_id.get(cid)
            if ce is not None and cid not in ce_stack and len(ce_stack) < 16:
                if _apply_reachable_command_mutations(
                    ce.get("commands", []), map_id, event_id, ex, ey, switches_state, variables_state,
                    self_switches_state, items_owned, warp_lookup, valid_map_ids,
                    0, None, common_events_by_id, ce_stack | {cid}
                ):
                    changed = True

        i += 1
    return changed


def _load_valid_db_ids(ir_dir: Path, filename: str) -> Tuple[List[Any], Set[int]]:
    """Loads a 1-indexed IR database JSON file and returns (raw_entries, valid_id_set)."""
    path = ir_dir / "database" / filename
    entries = _load_json(path) if path.exists() else []
    valid_ids = {
        int(entry["fields"]["id"])
        for entry in entries
        if isinstance(entry, dict) and "fields" in entry and "id" in entry["fields"]
    }
    return entries, valid_ids


def _validate_command_list(
    commands: List[Dict[str, Any]],
    source_label: str,
    maps_by_id: Dict[int, Dict[str, Any]],
    passages_by_ts: Dict[int, List[int]],
    priorities_by_ts: Dict[int, List[int]],
    switches_def: List[Any],
    variables_def: List[Any],
    valid_db_ids: Dict[str, Set[int]],
    common_events_by_id: Dict[int, Dict[str, Any]],
    errors: List[str],
) -> None:
    """Validates static map, switch, variable, actor, item, weapon, armor, skill, troop, and common event IDs."""
    valid_actor_ids = valid_db_ids.get("actors", set())
    valid_item_ids = valid_db_ids.get("items", set())
    valid_weapon_ids = valid_db_ids.get("weapons", set())
    valid_armor_ids = valid_db_ids.get("armors", set())
    valid_skill_ids = valid_db_ids.get("skills", set())
    valid_troop_ids = valid_db_ids.get("troops", set())

    for cmd in commands:
        op = cmd.get("op")
        if op == "transfer_player":
            t_map = int(cmd["map_id"])
            tx, ty = int(cmd["x"]), int(cmd["y"])
            if t_map not in maps_by_id:
                errors.append(f"{source_label} transfers to missing map {t_map}.")
            elif not is_tile_standable(maps_by_id[t_map], passages_by_ts, priorities_by_ts, tx, ty):
                errors.append(
                    f"{source_label} transfers to impassable tile ({tx}, {ty}) on map {t_map}."
                )
        elif op == "if_switch":
            sid = int(cmd["switch_id"])
            if sid <= 0 or sid >= len(switches_def):
                errors.append(f"{source_label} references undefined switch {sid}.")
        elif op == "set_switch":
            for sid in range(int(cmd["start_id"]), int(cmd["end_id"]) + 1):
                if sid <= 0 or sid >= len(switches_def):
                    errors.append(f"{source_label} sets undefined switch {sid}.")
        elif op == "if_variable":
            vid = int(cmd["variable_id"])
            if variables_def and (vid <= 0 or vid >= len(variables_def)):
                errors.append(f"{source_label} references undefined variable {vid}.")
        elif op == "set_variable":
            if variables_def:
                for vid in range(int(cmd["start_id"]), int(cmd["end_id"]) + 1):
                    if vid <= 0 or vid >= len(variables_def):
                        errors.append(f"{source_label} sets undefined variable {vid}.")
        elif op in ("change_item", "if_item"):
            iid = int(cmd["item_id"])
            if valid_item_ids and iid not in valid_item_ids:
                errors.append(f"{source_label} references undefined item {iid}.")
        elif op == "change_weapon":
            wid = int(cmd["weapon_id"])
            if valid_weapon_ids and wid not in valid_weapon_ids:
                errors.append(f"{source_label} references undefined weapon {wid}.")
        elif op == "change_armor":
            aid = int(cmd["armor_id"])
            if valid_armor_ids and aid not in valid_armor_ids:
                errors.append(f"{source_label} references undefined armor {aid}.")
        elif op == "change_party":
            aid = int(cmd.get("actor_id", 0))
            if valid_actor_ids and aid not in valid_actor_ids:
                errors.append(f"{source_label} references undefined actor {aid}.")
        elif op == "change_skill":
            aid = int(cmd.get("actor_id", 1))
            if valid_actor_ids and aid > 0 and aid not in valid_actor_ids:
                errors.append(f"{source_label} references undefined actor {aid}.")
            sid = int(cmd["skill_id"])
            if valid_skill_ids and sid not in valid_skill_ids:
                errors.append(f"{source_label} references undefined skill {sid}.")
        elif op == "change_equip":
            aid = int(cmd.get("actor_id", 1))
            if valid_actor_ids and aid > 0 and aid not in valid_actor_ids:
                errors.append(f"{source_label} references undefined actor {aid}.")
            eq_type = int(cmd.get("equip_type", 0))
            eq_id = int(cmd.get("equip_id", 0))
            if eq_type not in (0, 1, 2, 3, 4):
                errors.append(f"{source_label} has invalid equip_type {eq_type}.")
            elif eq_id > 0:
                if eq_type == 0 and valid_weapon_ids and eq_id not in valid_weapon_ids:
                    errors.append(f"{source_label} equips undefined weapon {eq_id}.")
                elif eq_type in (1, 2, 3, 4) and valid_armor_ids and eq_id not in valid_armor_ids:
                    errors.append(f"{source_label} equips undefined armor {eq_id}.")
        elif op == "battle":
            tid = int(cmd.get("troop_id", 0))
            if valid_troop_ids and tid not in valid_troop_ids:
                errors.append(f"{source_label} references undefined troop {tid}.")
        elif op == "shop":
            goods = cmd.get("goods", [])
            if not goods:
                errors.append(f"{source_label} shop command has no goods.")
            shop_sets = {"item": valid_item_ids, "weapon": valid_weapon_ids, "armor": valid_armor_ids}
            for g in goods:
                g_type = str(g.get("type", "item"))
                gid = int(g.get("id", 0))
                if g_type not in shop_sets:
                    errors.append(f"{source_label} shop has invalid good type {g_type!r}.")
                elif shop_sets[g_type] and gid not in shop_sets[g_type]:
                    errors.append(f"{source_label} shop references undefined {g_type} {gid}.")
        elif op == "common_event":
            cid = int(cmd.get("common_event_id", 0))
            if common_events_by_id and cid not in common_events_by_id:
                errors.append(f"{source_label} references undefined common_event {cid}.")


def _compute_tile_pass_masks(
    map_ir: Dict[str, Any],
    passages_by_ts: Dict[int, List[int]],
    priorities_by_ts: Dict[int, List[int]],
) -> List[int]:
    """Precomputes 4-bit directional allowance masks for every tile on a map in one pass."""
    w, h = int(map_ir["width"]), int(map_ir["height"])
    ts_id = int(map_ir.get("tileset_id", 1))
    passages = passages_by_ts.get(ts_id, [])
    priorities = priorities_by_ts.get(ts_id, [])
    n_pass = len(passages)
    n_prio = len(priorities)
    layers = map_ir["layers"]
    l2, l1, l0 = layers["layer_2"], layers["layer_1"], layers["layer_0"]
    masks = [0x0F] * (w * h)
    for y in range(h):
        r2, r1, r0 = l2[y], l1[y], l0[y]
        row_off = y * w
        for x in range(w):
            unresolved = 0x0F
            allowed = 0
            for tid in (int(r2[x]), int(r1[x]), int(r0[x])):
                if tid == 0:
                    continue
                p_flag = passages[tid] if tid < n_pass else 0
                prio = priorities[tid] if tid < n_prio else 0
                if prio == 0:
                    allowed |= unresolved & ~p_flag
                    unresolved = 0
                    break
                unresolved &= ~p_flag
                if unresolved == 0:
                    break
            masks[row_off + x] = allowed | unresolved
    return masks


def _build_map_static_edges(
    map_ir: Dict[str, Any],
    passages_by_ts: Dict[int, List[int]],
    priorities_by_ts: Dict[int, List[int]],
) -> Dict[Tuple[int, int], List[Tuple[int, int]]]:
    """Precomputes valid static tile-to-tile directional transitions for fast BFS sweeps."""
    w, h = int(map_ir["width"]), int(map_ir["height"])
    masks = _compute_tile_pass_masks(map_ir, passages_by_ts, priorities_by_ts)
    edges: Dict[Tuple[int, int], List[Tuple[int, int]]] = {}
    for y in range(h):
        row_off = y * w
        for x in range(w):
            m_src = masks[row_off + x]
            if m_src == 0:
                continue
            nbrs: List[Tuple[int, int]] = []
            if (m_src & 0x08) and y > 0 and (masks[row_off - w + x] & 0x01):
                nbrs.append((x, y - 1))
            if (m_src & 0x01) and y + 1 < h and (masks[row_off + w + x] & 0x08):
                nbrs.append((x, y + 1))
            if (m_src & 0x02) and x > 0 and (masks[row_off + x - 1] & 0x04):
                nbrs.append((x - 1, y))
            if (m_src & 0x04) and x + 1 < w and (masks[row_off + x + 1] & 0x02):
                nbrs.append((x + 1, y))
            if nbrs:
                edges[(x, y)] = nbrs
    return edges


def _run_bfs_reachability(
    maps_by_id: Dict[int, Dict[str, Any]],
    passages_by_ts: Dict[int, List[int]],
    priorities_by_ts: Dict[int, List[int]],
    common_events_by_id: Dict[int, Dict[str, Any]],
    start_map_id: int,
    start_x: int,
    start_y: int,
    total_events: int,
) -> Tuple[Set[Tuple[int, int, int]], Dict[int, bool]]:
    """Runs state-aware multi-map BFS and returns (visited_coords, unlocked_switches)."""
    switches_state: Dict[int, bool] = {}
    variables_state: Dict[int, int] = {}
    self_switches_state: Dict[Tuple[int, int, str], bool] = {}
    items_owned: Set[int] = set()
    visited: Set[Tuple[int, int, int]] = set()
    valid_map_ids = set(maps_by_id.keys())
    warp_lookup: Dict[Tuple[int, int, int], Set[Tuple[int, int, int]]] = {}
    static_edges_cache: Dict[int, Dict[Tuple[int, int], List[Tuple[int, int]]]] = {}
    prev_blocked_by_map: Dict[int, Set[Tuple[int, int]]] = {}
    frontier: List[Tuple[int, int, int]] = []

    if is_tile_standable(maps_by_id[start_map_id], passages_by_ts, priorities_by_ts, start_x, start_y):
        start_coord = (start_map_id, start_x, start_y)
        visited.add(start_coord)
        frontier.append(start_coord)

    visited_maps: Set[int] = {mid for mid, _vx, _vy in visited}
    prev_warp_edges = 0
    max_passes = max(8, total_events + len(common_events_by_id) + 2)
    for pass_idx in range(max_passes):
        blocked_cache: Dict[int, Set[Tuple[int, int]]] = {}
        for mid in list(visited_maps):
            blocked = _blocking_coords_for_map(
                mid, maps_by_id[mid], switches_state, variables_state, self_switches_state
            )
            blocked_cache[mid] = blocked
            unblocked = prev_blocked_by_map.get(mid, set()) - blocked
            if unblocked:
                for bx, by in unblocked:
                    for dx, dy in ((0, -1), (0, 1), (-1, 0), (1, 0)):
                        adj = (mid, bx + dx, by + dy)
                        if adj in visited:
                            frontier.append(adj)
        cur_warp_edges = sum(len(v) for v in warp_lookup.values())
        if pass_idx > 0 and cur_warp_edges != prev_warp_edges:
            for src in warp_lookup:
                if src in visited:
                    frontier.append(src)
        prev_warp_edges = cur_warp_edges

        queue: deque[Tuple[int, int, int]] = deque(frontier)
        frontier.clear()
        while queue:
            mid, x, y = queue.popleft()
            visited_maps.add(mid)
            if mid not in static_edges_cache:
                static_edges_cache[mid] = _build_map_static_edges(maps_by_id[mid], passages_by_ts, priorities_by_ts)
            if mid not in blocked_cache:
                blocked_cache[mid] = _blocking_coords_for_map(
                    mid, maps_by_id[mid], switches_state, variables_state, self_switches_state
                )
            blocked = blocked_cache[mid]
            for nx, ny in static_edges_cache[mid].get((x, y), ()):
                if (nx, ny) not in blocked:
                    nxt = (mid, nx, ny)
                    if nxt not in visited:
                        visited.add(nxt)
                        queue.append(nxt)
            for dest in warp_lookup.get((mid, x, y), ()):
                if dest not in visited:
                    visited.add(dest)
                    queue.append(dest)

        prev_blocked_by_map.update(blocked_cache)
        state_changed = False
        for mid in sorted(visited_maps):
            m = maps_by_id[mid]
            for ev in m.get("events", []):
                ex, ey = int(ev["x"]), int(ev["y"])
                active = select_active_page(ev, mid, switches_state, variables_state, self_switches_state)
                if active is None:
                    continue
                is_auto_page = active.get("trigger_code", 0) in (3, 4)
                can_reach = (mid, ex, ey) in visited or any(
                    (mid, ex + dx, ey + dy) in visited for dx, dy in ((0, -1), (0, 1), (-1, 0), (1, 0))
                )
                if not (can_reach or is_auto_page):
                    continue
                if _apply_reachable_command_mutations(
                    active.get("commands", []),
                    mid,
                    int(ev["id"]),
                    ex,
                    ey,
                    switches_state,
                    variables_state,
                    self_switches_state,
                    items_owned,
                    warp_lookup,
                    valid_map_ids,
                    common_events_by_id=common_events_by_id,
                ):
                    state_changed = True
                for dest in warp_lookup.get((mid, ex, ey), ()):
                    if dest not in visited:
                        visited.add(dest)
                        visited_maps.add(dest[0])
                        frontier.append(dest)
                        state_changed = True

        for cid, ce in sorted(common_events_by_id.items()):
            if ce.get("trigger", 0) not in (1, 2, "autorun", "parallel"):
                continue
            if not switches_state.get(int(ce.get("switch_id", 1)), False):
                continue
            if _apply_reachable_command_mutations(
                ce.get("commands", []),
                start_map_id,
                0,
                start_x,
                start_y,
                switches_state,
                variables_state,
                self_switches_state,
                items_owned,
                warp_lookup,
                valid_map_ids,
                common_events_by_id=common_events_by_id,
                _ce_stack={cid},
            ):
                state_changed = True
            for dest in warp_lookup.get((start_map_id, start_x, start_y), ()):
                if dest not in visited:
                    visited.add(dest)
                    visited_maps.add(dest[0])
                    frontier.append(dest)
                    state_changed = True
        if not state_changed:
            break

    return visited, switches_state


def _find_unreachable_events(
    maps_by_id: Dict[int, Dict[str, Any]],
    visited: Set[Tuple[int, int, int]],
) -> List[str]:
    """Identifies events unreachable from the player start (excluding map-local auto controllers on reachable maps)."""
    reachable_maps = {m_id for m_id, _, _ in visited}
    unreachable: List[str] = []
    for mid, m in maps_by_id.items():
        map_is_reachable = mid in reachable_maps
        for ev in m.get("events", []):
            ex, ey = int(ev["x"]), int(ev["y"])
            reachable = (mid, ex, ey) in visited or any(
                (mid, ex + dx, ey + dy) in visited for dx, dy in ((0, -1), (0, 1), (-1, 0), (1, 0))
            )
            pages = ev.get("pages", [])
            has_auto_page = any(int(p.get("trigger_code", 0)) in (3, 4) for p in pages)
            non_auto_has_logic = any(
                int(p.get("trigger_code", 0)) not in (3, 4)
                and any(c.get("op") != "end" for c in p.get("commands", []))
                for p in pages
            )
            # Global autorun/parallel map controllers (e.g. corner (0,0) events) need map reachability, not tile adjacency.
            if has_auto_page and not non_auto_has_logic and map_is_reachable:
                reachable = True
            if not reachable:
                unreachable.append(f"Map {mid} event {ev['id']} ({ev['name']}) at ({ex}, {ey})")
    return unreachable


def _validate_battle_formula_settings(ir_dir: Path, proj: Dict[str, Any]) -> List[str]:
    """Checks the project's battle_formula preset/overrides and per-skill damage_formula fields."""
    from .battle_formulas import BattleFormula, FormulaError, combatant_stats

    errs: List[str] = []
    try:
        bf = BattleFormula.from_project(proj)
        bf.validate()
    except (FormulaError, TypeError, ValueError) as exc:
        errs.append(f"Invalid battle_formula settings: {exc}")
        return errs
    skills_path = Path(ir_dir) / "database" / "skills.json"
    if skills_path.exists():
        hero = combatant_stats(is_actor=1, atk=20, str=20, pdef=10, mdef=10)
        foe = combatant_stats(atk=20, str=20, pdef=10, mdef=10)
        for sk in _load_json(skills_path):
            sf = (sk or {}).get("fields", {}) if isinstance(sk, dict) else {}
            if isinstance(sf.get("damage_formula"), str) and sf["damage_formula"].strip():
                try:
                    bf.skill_damage(hero, foe, sf, strict=True)
                except (FormulaError, TypeError, ValueError) as exc:
                    errs.append(f"Skill {sf.get('id')} ({sf.get('name', '')}) has invalid damage_formula: {exc}")
    return errs


def validate_ir_project(ir_dir: Path) -> ValidationResult:
    """Runs static reference checks and state-aware multi-map BFS reachability checks."""
    ir_dir = Path(ir_dir)
    errors: List[str] = []
    warnings: List[str] = []

    proj_path = ir_dir / "ir_project.json"
    ts_path = ir_dir / "database" / "tilesets.json"
    if not proj_path.exists() or not ts_path.exists():
        return ValidationResult(
            passed=False,
            errors=[f"Missing core IR files under {ir_dir}"],
        )

    proj = _load_json(proj_path)
    sys_fields = proj["system"]["fields"]
    errors.extend(_validate_battle_formula_settings(ir_dir, proj))
    switches_def = sys_fields.get("switches", [])
    variables_def = sys_fields.get("variables", [])
    start_map_id = int(sys_fields.get("start_map_id", 1))
    start_x = int(sys_fields.get("start_x", 0))
    start_y = int(sys_fields.get("start_y", 0))

    _, valid_actor_ids = _load_valid_db_ids(ir_dir, "actors.json")
    _, valid_item_ids = _load_valid_db_ids(ir_dir, "items.json")
    _, valid_weapon_ids = _load_valid_db_ids(ir_dir, "weapons.json")
    _, valid_armor_ids = _load_valid_db_ids(ir_dir, "armors.json")
    _, valid_skill_ids = _load_valid_db_ids(ir_dir, "skills.json")
    enemies_ir, valid_enemy_ids = _load_valid_db_ids(ir_dir, "enemies.json")
    troops_ir, valid_troop_ids = _load_valid_db_ids(ir_dir, "troops.json")
    valid_db_ids = {
        "actors": valid_actor_ids,
        "items": valid_item_ids,
        "weapons": valid_weapon_ids,
        "armors": valid_armor_ids,
        "skills": valid_skill_ids,
        "troops": valid_troop_ids,
    }
    for en in enemies_ir:
        if not isinstance(en, dict) or "fields" not in en:
            continue
        ef = en["fields"]
        eid = int(ef.get("id", 0))
        ename = str(ef.get("name", ""))
        drop_iid = int(ef.get("item_id", 0))
        if drop_iid > 0 and valid_item_ids and drop_iid not in valid_item_ids:
            warnings.append(f"Enemy {eid} ({ename}) references undefined drop item {drop_iid}.")
        drop_wid = int(ef.get("weapon_id", 0))
        if drop_wid > 0 and valid_weapon_ids and drop_wid not in valid_weapon_ids:
            warnings.append(f"Enemy {eid} ({ename}) references undefined drop weapon {drop_wid}.")
        drop_aid = int(ef.get("armor_id", 0))
        if drop_aid > 0 and valid_armor_ids and drop_aid not in valid_armor_ids:
            warnings.append(f"Enemy {eid} ({ename}) references undefined drop armor {drop_aid}.")
        for act in ef.get("actions", []):
            af = act.get("fields", act) if isinstance(act, dict) else {}
            if int(af.get("kind", 0)) == 1:
                sk_id = int(af.get("skill_id", 0))
                if sk_id > 0 and valid_skill_ids and sk_id not in valid_skill_ids:
                    warnings.append(f"Enemy {eid} ({ename}) AI action references undefined skill {sk_id}.")
    for tr in troops_ir:
        if not isinstance(tr, dict) or "fields" not in tr:
            continue
        t_fields = tr["fields"]
        t_id = int(t_fields.get("id", 0))
        for mem in t_fields.get("members", []):
            eid = int(mem.get("fields", {}).get("enemy_id", 0)) if isinstance(mem, dict) else 0
            if valid_enemy_ids and eid not in valid_enemy_ids:
                errors.append(f"Troop {t_id} ({t_fields.get('name', '')}) references undefined enemy {eid}.")

    tilesets_ir = _load_json(ts_path)
    passages_by_ts, priorities_by_ts = build_tileset_tables(tilesets_ir)

    ce_path = ir_dir / "database" / "common_events.json"
    common_events_by_id: Dict[int, Dict[str, Any]] = {}
    if ce_path.exists():
        for entry in _load_json(ce_path):
            if isinstance(entry, dict) and "id" in entry:
                common_events_by_id[int(entry["id"])] = entry

    maps_by_id: Dict[int, Dict[str, Any]] = {}
    for map_file in sorted((ir_dir / "maps").glob("map_*.json")):
        m = _load_json(map_file)
        maps_by_id[int(m["map_id"])] = m

    if start_map_id not in maps_by_id:
        errors.append(f"Start map ID {start_map_id} does not exist.")
        return ValidationResult(passed=False, errors=errors, warnings=warnings)

    start_map = maps_by_id[start_map_id]
    if not is_tile_standable(start_map, passages_by_ts, priorities_by_ts, start_x, start_y):
        errors.append(f"Player start ({start_x}, {start_y}) on map {start_map_id} is impassable or out of bounds.")

    total_events = 0
    for mid, m in maps_by_id.items():
        w, h = int(m["width"]), int(m["height"])
        for ev in m.get("events", []):
            total_events += 1
            ex, ey = int(ev["x"]), int(ev["y"])
            if ex < 0 or ey < 0 or ex >= w or ey >= h:
                errors.append(f"Map {mid} event {ev['id']} ({ev['name']}) out of bounds at ({ex}, {ey}).")

            for p_idx, page in enumerate(ev.get("pages", [])):
                _validate_command_list(
                    page.get("commands", []),
                    f"Map {mid} event {ev['id']} page {p_idx}",
                    maps_by_id,
                    passages_by_ts,
                    priorities_by_ts,
                    switches_def,
                    variables_def,
                    valid_db_ids,
                    common_events_by_id,
                    errors,
                )

    for cid, ce in sorted(common_events_by_id.items()):
        trig = ce.get("trigger", 0)
        if trig in (1, 2, "autorun", "parallel"):
            sw_id = int(ce.get("switch_id", 1))
            if sw_id <= 0 or sw_id >= len(switches_def):
                errors.append(f"CommonEvent {cid} ({ce.get('name', '')}) references undefined trigger switch {sw_id}.")
        _validate_command_list(
            ce.get("commands", []),
            f"CommonEvent {cid} ({ce.get('name', '')})",
            maps_by_id,
            passages_by_ts,
            priorities_by_ts,
            switches_def,
            variables_def,
            valid_db_ids,
            common_events_by_id,
            errors,
        )

    visited, switches_state = _run_bfs_reachability(
        maps_by_id,
        passages_by_ts,
        priorities_by_ts,
        common_events_by_id,
        start_map_id,
        start_x,
        start_y,
        total_events,
    )
    unreachable_events = _find_unreachable_events(maps_by_id, visited)
    if unreachable_events:
        errors.extend([f"Unreachable event: {desc}" for desc in unreachable_events])

    # Detect non-start maps entered via warp that have no outgoing transfer back out (dead-end warning)
    outgoing_warps: Dict[int, Set[int]] = {mid: set() for mid in maps_by_id}
    incoming_warps: Set[int] = set()
    for mid, m in maps_by_id.items():
        for ev in m.get("events", []):
            for page in ev.get("pages", []):
                for cmd in page.get("commands", []):
                    if cmd.get("op") == "transfer_player":
                        tm = int(cmd.get("map_id", 0))
                        if tm in maps_by_id and tm != mid:
                            outgoing_warps[mid].add(tm)
                            incoming_warps.add(tm)
    for mid in sorted(incoming_warps):
        if mid != start_map_id and not outgoing_warps.get(mid):
            warnings.append(f"Map {mid} ({maps_by_id[mid].get('name', '')}) has incoming warps but no outgoing transfer_player.")

    return ValidationResult(
        passed=len(errors) == 0,
        errors=errors,
        warnings=warnings,
        stats={
            "maps": len(maps_by_id),
            "events": total_events,
            "reachable_tiles": len(visited),
            "unlocked_switches": sorted([sid for sid, val in switches_state.items() if val]),
        },
    )


def _resolve_graphics_dir(ir_dir: Path, graphics_dir: Optional[Path]) -> Optional[Path]:
    """Locates the Graphics directory if provided or present alongside the IR workspace."""
    if graphics_dir is not None:
        g = Path(graphics_dir)
        return g if g.exists() else None
    for cand in (ir_dir.parent / "rmxp" / "Graphics", ir_dir.parent / "Graphics", ir_dir / "Graphics"):
        if cand.exists():
            return cand
    return None


def _paste_rgba(canvas: Image.Image, sheet: Image.Image, crop_box: Tuple[int, int, int, int], dest: Tuple[int, int]) -> None:
    """Alpha-composites a cropped sprite/tile region onto the overview canvas."""
    x0, y0, x1, y1 = crop_box
    if x0 < 0 or y0 < 0 or x1 > sheet.width or y1 > sheet.height or x1 <= x0 or y1 <= y0:
        return
    patch = sheet.crop((x0, y0, x1, y1))
    canvas.alpha_composite(patch, dest=dest)


def _paste_character_marker(
    img: Image.Image,
    draw: ImageDraw.ImageDraw,
    sheet: Optional[Image.Image],
    tx: int,
    ty: int,
    tile_px: int,
    is_circle: bool,
    fallback_color: Tuple[int, int, int, int],
) -> None:
    """Composites the first frame of a character sheet or draws a geometric fallback marker."""
    dx, dy = tx * tile_px, ty * tile_px
    if sheet is not None and sheet.width >= 4 and sheet.height >= 4:
        cw, ch = sheet.width // 4, sheet.height // 4
        _paste_rgba(img, sheet, (0, 0, cw, ch), (dx + (tile_px - cw) // 2, dy + tile_px - ch))
        return
    pad = 6 if is_circle else 8
    box = [dx + pad, dy + pad, dx + tile_px - 1 - pad, dy + tile_px - 1 - pad]
    if is_circle:
        draw.ellipse(box, fill=fallback_color)
    else:
        draw.rectangle(box, fill=fallback_color)


def _render_png_preview(
    ir_dir: Path,
    map_ir: Dict[str, Any],
    sys_fields: Dict[str, Any],
    tilesets_ir: List[Any],
    passages_by_ts: Dict[int, List[int]],
    priorities_by_ts: Dict[int, List[int]],
    active_events_by_coord: Dict[Tuple[int, int], Tuple[Dict[str, Any], Dict[str, Any]]],
    start_pos: Optional[Tuple[int, int]],
    png_out: Path,
    graphics_dir: Optional[Path],
) -> None:
    """Renders a 32x32 sprite sheet PNG overview (or 16x16 schematic fallback) of a map."""
    w, h = int(map_ir["width"]), int(map_ir["height"])
    gfx_root = _resolve_graphics_dir(ir_dir, graphics_dir)
    img_cache: Dict[str, Optional[Image.Image]] = {}

    def load_gfx(subpath: str) -> Optional[Image.Image]:
        if gfx_root is None or not subpath:
            return None
        if subpath in img_cache:
            return img_cache[subpath]
        cand = gfx_root / f"{subpath}.png"
        if cand.exists():
            with Image.open(cand) as raw_img:
                loaded = raw_img.convert("RGBA")
            img_cache[subpath] = loaded
            return loaded
        img_cache[subpath] = None
        return None

    ts_id = int(map_ir.get("tileset_id", 1))
    ts_fields = (
        tilesets_ir[ts_id].get("fields", {})
        if 0 <= ts_id < len(tilesets_ir) and isinstance(tilesets_ir[ts_id], dict)
        else {}
    )
    ts_sheet = load_gfx(f"Tilesets/{ts_fields.get('tileset_name', '')}")
    auto_names = ts_fields.get("autotile_names", [])
    has_real_sprites = ts_sheet is not None or any(
        load_gfx(f"Autotiles/{an}") is not None for an in auto_names if an
    )

    if has_real_sprites:
        tile_px = 32
        img = Image.new("RGBA", (w * tile_px, h * tile_px), (18, 20, 26, 255))
        draw = ImageDraw.Draw(img)
        quad_offsets = ((0, 0), (16, 0), (0, 16), (16, 16))
        for z in ("layer_0", "layer_1", "layer_2"):
            layer_grid = map_ir["layers"][z]
            for y in range(h):
                for x in range(w):
                    tid = int(layer_grid[y][x])
                    if tid == 0:
                        continue
                    dx, dy = x * tile_px, y * tile_px
                    if 48 <= tid < 384:
                        slot = (tid - 48) // 48
                        aname = auto_names[slot] if slot < len(auto_names) else ""
                        asheet = load_gfx(f"Autotiles/{aname}")
                        if asheet is not None:
                            for sub_id, (ox, oy) in zip(resolve_autotile_quads(layer_grid, x, y, tid), quad_offsets):
                                sx, sy, sw, sh = subtile_pixel_rect(sub_id)
                                _paste_rgba(img, asheet, (sx, sy, sx + sw, sy + sh), (dx + ox, dy + oy))
                        elif z == "layer_0":
                            draw.rectangle([dx, dy, dx + tile_px - 1, dy + tile_px - 1], fill=(48, 122, 196, 255))
                    elif tid >= 384 and ts_sheet is not None:
                        rel = tid - 384
                        sx, sy = (rel % 8) * tile_px, (rel // 8) * tile_px
                        _paste_rgba(img, ts_sheet, (sx, sy, sx + tile_px, sy + tile_px), (dx, dy))

        for (ex, ey), (_ev, active) in active_events_by_coord.items():
            gname = str(active.get("graphic", {}).get("fields", {}).get("character_name", ""))
            _paste_character_marker(
                img, draw, load_gfx(f"Characters/{gname}"), ex, ey, tile_px, True, (245, 210, 65, 255)
            )
        if start_pos is not None:
            hero_char = ""
            actors_path = ir_dir / "database" / "actors.json"
            if actors_path.exists():
                actors_ir = _load_json(actors_path)
                party = sys_fields.get("party_members", [1])
                aid = int(party[0]) if party else 1
                if 0 <= aid < len(actors_ir) and isinstance(actors_ir[aid], dict):
                    hero_char = str(actors_ir[aid].get("fields", {}).get("character_name", ""))
            _paste_character_marker(
                img, draw, load_gfx(f"Characters/{hero_char}"), start_pos[0], start_pos[1], tile_px, False, (70, 165, 255, 255)
            )
    else:
        tile_px = 16
        img = Image.new("RGBA", (w * tile_px, h * tile_px), (24, 26, 32, 255))
        draw = ImageDraw.Draw(img)
        for y in range(h):
            for x in range(w):
                passable = is_tile_standable(map_ir, passages_by_ts, priorities_by_ts, x, y)
                l0 = int(map_ir["layers"]["layer_0"][y][x])
                if not passable:
                    col = (180, 65, 65, 255) if 48 <= l0 < 96 else (50, 75, 55, 255)
                elif l0 == 387:
                    col = (150, 105, 60, 255)
                elif l0 == 385:
                    col = (175, 145, 105, 255)
                else:
                    col = (85, 155, 85, 255)
                x0, y0 = x * tile_px, y * tile_px
                draw.rectangle([x0, y0, x0 + tile_px - 1, y0 + tile_px - 1], fill=col)
                if (x, y) in active_events_by_coord:
                    draw.ellipse([x0 + 3, y0 + 3, x0 + tile_px - 4, y0 + tile_px - 4], fill=(245, 210, 65, 255))
                if start_pos == (x, y):
                    draw.rectangle([x0 + 4, y0 + 4, x0 + tile_px - 5, y0 + tile_px - 5], fill=(70, 165, 255, 255))

    png_out = Path(png_out)
    png_out.parent.mkdir(parents=True, exist_ok=True)
    img.save(png_out)


def render_map_preview(
    ir_dir: Path,
    map_id: int,
    png_out: Optional[Path] = None,
    graphics_dir: Optional[Path] = None,
    switches_state: Optional[Dict[int, bool]] = None,
) -> str:
    """Renders an ASCII passability/event map and optionally exports a PNG overview."""
    ir_dir = Path(ir_dir)
    map_ir = _load_json(ir_dir / "maps" / f"map_{map_id:03d}.json")
    proj = _load_json(ir_dir / "ir_project.json")
    tilesets_ir = _load_json(ir_dir / "database" / "tilesets.json")
    passages_by_ts, priorities_by_ts = build_tileset_tables(tilesets_ir)

    w, h = int(map_ir["width"]), int(map_ir["height"])
    active_switches = switches_state or {}
    active_events_by_coord: Dict[Tuple[int, int], Tuple[Dict[str, Any], Dict[str, Any]]] = {}
    for ev in map_ir.get("events", []):
        active_page = select_active_page(ev, map_id, active_switches, {}, {})
        if active_page is not None:
            active_events_by_coord[(int(ev["x"]), int(ev["y"]))] = (ev, active_page)
    sys_fields = proj["system"]["fields"]
    start_pos = (
        (int(sys_fields["start_x"]), int(sys_fields["start_y"]))
        if int(sys_fields["start_map_id"]) == map_id
        else None
    )

    ts_passages = passages_by_ts.get(int(map_ir.get("tileset_id", 1)), [])
    lines: List[str] = [f"Map {map_id}: {map_ir['name']} ({w}x{h})"]
    for y in range(h):
        row_chars: List[str] = []
        for x in range(w):
            l0 = int(map_ir["layers"]["layer_0"][y][x])
            l1 = int(map_ir["layers"]["layer_1"][y][x])
            l2 = int(map_ir["layers"]["layer_2"][y][x])
            if start_pos == (x, y):
                row_chars.append("P")
            elif (x, y) in active_events_by_coord:
                row_chars.append("E")
            elif not is_tile_standable(map_ir, passages_by_ts, priorities_by_ts, x, y):
                row_chars.append("~" if 48 <= l0 < 96 else "#")
            elif l0 == 387:
                row_chars.append("=")
            elif l0 == 385:
                row_chars.append(":")
            elif any(0 <= tid < len(ts_passages) and (ts_passages[tid] & 0x40) for tid in (l2, l1, l0)):
                row_chars.append(",")
            else:
                row_chars.append(".")
        lines.append("".join(row_chars))
    lines.append("Legend: P=Start, E=Event, ~=Water, #=Blocked, ,=Bush, ==Bridge, :=Path, .=Passable")

    if png_out is not None:
        _render_png_preview(
            ir_dir,
            map_ir,
            sys_fields,
            tilesets_ir,
            passages_by_ts,
            priorities_by_ts,
            active_events_by_coord,
            start_pos,
            png_out,
            graphics_dir,
        )

    return "\n".join(lines)


def _find_asset_dir(ir_dir: Path, folder_name: str, explicit: Optional[Path] = None) -> Optional[Path]:
    if explicit is not None:
        ep = Path(explicit)
        if (ep / folder_name).is_dir():
            return ep / folder_name
        return ep if ep.is_dir() else None
    for cand in (
        ir_dir / "assets" / folder_name,
        ir_dir / folder_name,
        ir_dir.parent / "rmxp" / folder_name,
        ir_dir.parent / folder_name,
    ):
        if cand.is_dir():
            return cand
    return None


def _asset_exists(root: Optional[Path], subdir: str, stem: str, exts: Tuple[str, ...]) -> bool:
    if root is None or not stem:
        return False
    base = root / subdir
    if not base.is_dir():
        return False
    direct = base / stem
    if direct.is_file():
        return True
    return any((base / f"{stem}{ext}").is_file() for ext in exts)


def _collect_cmd_assets(
    cmds: List[Dict[str, Any]],
    gfx_refs: Set[Tuple[str, str]],
    aud_refs: Set[Tuple[str, str]],
) -> None:
    for c in cmds:
        op = str(c.get("op", ""))
        if op == "change_picture" and c.get("name"):
            gfx_refs.add(("Pictures", str(c["name"])))
        elif op in ("play_bgm", "play_bgs", "play_se", "play_me") and c.get("name"):
            sub = {"play_bgm": "BGM", "play_bgs": "BGS", "play_se": "SE", "play_me": "ME"}[op]
            aud_refs.add((sub, str(c["name"])))
        elif op == "change_fog" and c.get("name"):
            gfx_refs.add(("Fogs", str(c["name"])))
        elif op == "change_transition" and c.get("name"):
            gfx_refs.add(("Transitions", str(c["name"])))
        elif op == "change_map_settings":
            if c.get("panorama"):
                gfx_refs.add(("Panoramas", str(c["panorama"])))
            if c.get("fog"):
                gfx_refs.add(("Fogs", str(c["fog"])))
            if c.get("battleback"):
                gfx_refs.add(("Battlebacks", str(c["battleback"])))


def validate_project_assets(
    ir_dir: Path,
    graphics_dir: Optional[Path] = None,
    audio_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    """Audits all referenced and unreferenced Graphics/ and Audio/ files across maps, events, and DB."""
    ir_dir = Path(ir_dir)
    gfx_root = _find_asset_dir(ir_dir, "Graphics", graphics_dir)
    aud_root = _find_asset_dir(ir_dir, "Audio", audio_dir)
    gfx_refs: Set[Tuple[str, str]] = set()
    aud_refs: Set[Tuple[str, str]] = set()

    proj = _load_json(ir_dir / "ir_project.json")
    sys_f = proj.get("system", {}).get("fields", {})
    for key, sub in (
        ("windowskin_name", "Windowskins"),
        ("title_name", "Titles"),
        ("gameover_name", "Gameovers"),
        ("battleback_name", "Battlebacks"),
    ):
        val = str(sys_f.get(key, "") or "").strip()
        if val:
            gfx_refs.add((sub, val))
    for key, sub in (
        ("title_bgm", "BGM"),
        ("battle_bgm", "BGM"),
        ("battle_end_me", "ME"),
        ("gameover_me", "ME"),
        ("cursor_se", "SE"),
        ("decision_se", "SE"),
        ("cancel_se", "SE"),
        ("buzzer_se", "SE"),
        ("equip_se", "SE"),
        ("save_se", "SE"),
        ("load_se", "SE"),
        ("shop_se", "SE"),
        ("battle_start_se", "SE"),
        ("escape_se", "SE"),
    ):
        af = sys_f.get(key, {})
        aname = str(af.get("fields", af).get("name", "") or "").strip() if isinstance(af, dict) else ""
        if aname:
            aud_refs.add((sub, aname))

    db_dir = ir_dir / "database"
    for ts in _load_json(db_dir / "tilesets.json"):
        if not ts:
            continue
        f = ts.get("fields", {})
        for k, sub in (("tileset_name", "Tilesets"), ("panorama_name", "Panoramas"), ("fog_name", "Fogs"), ("battleback_name", "Battlebacks")):
            if f.get(k):
                gfx_refs.add((sub, str(f[k])))
        for aname in f.get("autotile_names", []):
            if aname:
                gfx_refs.add(("Autotiles", str(aname)))

    for tbl in ("actors", "enemies"):
        p = db_dir / f"{tbl}.json"
        if p.exists():
            for row in _load_json(p):
                if not row:
                    continue
                f = row.get("fields", {})
                if f.get("character_name"):
                    gfx_refs.add(("Characters", str(f["character_name"])))
                if f.get("battler_name"):
                    gfx_refs.add(("Battlers", str(f["battler_name"])))

    for tbl in ("skills", "items", "weapons", "armors"):
        p = db_dir / f"{tbl}.json"
        if p.exists():
            for row in _load_json(p):
                if not row:
                    continue
                f = row.get("fields", {})
                if f.get("icon_name"):
                    gfx_refs.add(("Icons", str(f["icon_name"])))
                mse = f.get("menu_se", {})
                mname = str(mse.get("fields", mse).get("name", "") or "").strip() if isinstance(mse, dict) else ""
                if mname:
                    aud_refs.add(("SE", mname))

    anims_p = db_dir / "animations.json"
    if anims_p.exists():
        for row in _load_json(anims_p):
            if row and row.get("fields", {}).get("animation_name"):
                gfx_refs.add(("Animations", str(row["fields"]["animation_name"])))

    ce_p = db_dir / "common_events.json"
    if ce_p.exists():
        for row in _load_json(ce_p):
            if row:
                _collect_cmd_assets(row.get("commands", []), gfx_refs, aud_refs)

    tr_p = db_dir / "troops.json"
    if tr_p.exists():
        for row in _load_json(tr_p):
            if not row:
                continue
            for page in row.get("pages", []):
                _collect_cmd_assets(page.get("commands", []), gfx_refs, aud_refs)

    for mf in sorted((ir_dir / "maps").glob("map_*.json")):
        m = _load_json(mf)
        px_name = str(m.get("parallax_name", "") or "").strip()
        if px_name:
            gfx_refs.add(("Panoramas", px_name))
        for ev in m.get("events", []):
            for page in ev.get("pages", []):
                gname = str(page.get("graphic", {}).get("fields", {}).get("character_name", "") or "").strip()
                if gname:
                    gfx_refs.add(("Characters", gname))
                _collect_cmd_assets(page.get("commands", []), gfx_refs, aud_refs)

    gfx_exts = (".png", ".jpg", ".jpeg", ".bmp")
    aud_exts = (".ogg", ".wav", ".mp3")
    missing_gfx = sorted(
        f"{sub}/{stem}" for sub, stem in gfx_refs if not _asset_exists(gfx_root, sub, stem, gfx_exts)
    )
    missing_aud = sorted(
        f"{sub}/{stem}" for sub, stem in aud_refs if not _asset_exists(aud_root, sub, stem, aud_exts)
    )
    gfx_ref_stems = {(sub, stem.lower()) for sub, stem in gfx_refs}
    aud_ref_stems = {(sub, stem.lower()) for sub, stem in aud_refs}
    orphan_gfx: List[str] = []
    if gfx_root is not None and gfx_root.is_dir():
        for fp in sorted(gfx_root.rglob("*")):
            if fp.is_file() and fp.suffix.lower() in gfx_exts:
                sub = fp.parent.name
                if (sub, fp.stem.lower()) not in gfx_ref_stems:
                    orphan_gfx.append(f"{sub}/{fp.name}")
    orphan_aud: List[str] = []
    if aud_root is not None and aud_root.is_dir():
        for fp in sorted(aud_root.rglob("*")):
            if fp.is_file() and fp.suffix.lower() in aud_exts:
                sub = fp.parent.name
                if (sub, fp.stem.lower()) not in aud_ref_stems:
                    orphan_aud.append(f"{sub}/{fp.name}")

    return {
        "passed": len(missing_gfx) == 0 and len(missing_aud) == 0,
        "graphics_root": str(gfx_root) if gfx_root else None,
        "audio_root": str(aud_root) if aud_root else None,
        "referenced_graphics": len(gfx_refs),
        "referenced_audio": len(aud_refs),
        "missing_graphics": missing_gfx,
        "missing_audio": missing_aud,
        "orphan_graphics": orphan_gfx,
        "orphan_audio": orphan_aud,
    }


def simulate_battle_balance(
    ir_dir: Path,
    troop_id: Optional[int] = None,
    trials_per_troop: int = 20,
) -> Dict[str, Any]:
    """Runs Monte Carlo combat simulations (skills, statuses, defense, AGI order) to surface balance outliers."""
    from .battle_engine import build_battle_enemies, enemy_formula_stats
    from .battle_formulas import BattleFormula, combatant_stats

    ir_dir = Path(ir_dir)
    proj = _load_json(ir_dir / "ir_project.json")
    formula = BattleFormula.from_project(proj)
    db_dir = ir_dir / "database"
    actors_db = {int(a["fields"]["id"]): a for a in _load_json(db_dir / "actors.json") if a}
    classes_db = {int(c["fields"]["id"]): c for c in _load_json(db_dir / "classes.json") if c} if (db_dir / "classes.json").exists() else {}
    skills_db = {int(s["fields"]["id"]): s for s in _load_json(db_dir / "skills.json") if s} if (db_dir / "skills.json").exists() else {}
    states_db = {int(s["fields"]["id"]): s for s in _load_json(db_dir / "states.json") if s} if (db_dir / "states.json").exists() else {}
    weapons_db = {int(w["fields"]["id"]): w for w in _load_json(db_dir / "weapons.json") if w}
    armors_db = {int(a["fields"]["id"]): a for a in _load_json(db_dir / "armors.json") if a}
    enemies_db = {int(e["fields"]["id"]): e for e in _load_json(db_dir / "enemies.json") if e}
    troops_raw = [t for t in _load_json(db_dir / "troops.json") if t]
    if troop_id is not None:
        troops_raw = [t for t in troops_raw if int(t.get("fields", {}).get("id", 0)) == int(troop_id)]

    party_ids = [int(aid) for aid in proj.get("system", {}).get("fields", {}).get("party_members", [1]) if int(aid) in actors_db]
    if not party_ids and actors_db:
        party_ids = [min(actors_db.keys())]

    def _actor_snapshot(aid: int) -> Dict[str, Any]:
        af = actors_db[aid]["fields"]
        lv = max(1, int(af.get("initial_level", 1)))
        p_tbl = af.get("parameters", {}) or {}
        tbl = p_tbl.get("data", []) if isinstance(p_tbl, dict) else []
        # RMXP Actor#parameters is Table(6, 100): [param_kind, level] -> data[kind + level * xsize]
        # (same layout the in-game simulator reads in HeadlessGameSession._build_actor_state).
        p_xsize = int(p_tbl.get("xsize", 6) or 6) if isinstance(p_tbl, dict) else 6

        def _param(kind: int, fallback: int) -> int:
            idx = kind + lv * p_xsize
            val = int(tbl[idx]) if idx < len(tbl) else 0
            return val if val > 0 else fallback

        cls_f = classes_db.get(int(af.get("class_id", 1) or 1), {}).get("fields", {})
        cp_raw = cls_f.get("class_params")
        cb = {k: int((cp_raw or {}).get(k, 0) or 0) for k in ("mhp", "msp", "atk", "str", "agi", "int", "pdef", "mdef")} if isinstance(cp_raw, dict) else {k: 0 for k in ("mhp", "msp", "atk", "str", "agi", "int", "pdef", "mdef")}

        wp = weapons_db.get(int(af.get("weapon_id", 0)), {}).get("fields", {})
        arm_pdef = 0
        arm_mdef = 0
        arm_eva = 0
        gear = [wp]
        for ak in ("armor1_id", "armor2_id", "armor3_id", "armor4_id"):
            ar = armors_db.get(int(af.get(ak, 0)), {}).get("fields", {})
            gear.append(ar)
            arm_pdef += int(ar.get("pdef", 0))
            arm_mdef += int(ar.get("mdef", 0))
            arm_eva += int(ar.get("eva", 0))

        def _plus(key: str) -> int:
            return sum(int(g.get(key, 0) or 0) for g in gear)
        # Mirrors the in-game formulas (simulator._actor_attack_power / _actor_defense).
        mhp = max(1, _param(0, 150) + cb["mhp"])
        msp = max(0, _param(1, 40) + cb["msp"])
        atk = max(1, _param(2, 14) + cb["str"] + cb["atk"] + int(wp.get("atk", 0)))
        pdef = max(0, 8 + cb["pdef"] + int(wp.get("pdef", 0)) + arm_pdef)
        agi = max(1, _param(4, 10) + cb["agi"])
        int_stat = max(1, _param(5, 10) + cb["int"])
        mdef = max(0, cb["mdef"] + int(wp.get("mdef", 0)) + arm_mdef)
        fstats = combatant_stats(
            is_actor=1,
            level=lv,
            hp=mhp,
            mhp=mhp,
            sp=msp,
            msp=msp,
            atk=int(wp.get("atk", 0)) + cb["atk"],
            str=_param(2, 14) + cb["str"] + _plus("str_plus"),
            dex=_param(3, 10) + _plus("dex_plus"),
            agi=agi + _plus("agi_plus"),
            int=int_stat + _plus("int_plus"),
            pdef=pdef - 8,
            mdef=mdef,
            eva=arm_eva,
            legacy_atk=atk,
            legacy_def=pdef,
        )

        learned_ids: List[int] = []
        for lrn in cls_f.get("learnings", []):
            lf = lrn.get("fields", lrn) if isinstance(lrn, dict) else {}
            if int(lf.get("level", 1) or 1) <= lv:
                sid = int(lf.get("skill_id", 0) or 0)
                if sid in skills_db and sid not in learned_ids:
                    learned_ids.append(sid)
        heal_skills: List[Dict[str, Any]] = []
        atk_skills: List[Dict[str, Any]] = []
        for sid in learned_ids:
            sf = skills_db[sid].get("fields", {})
            pwr = int(sf.get("power", 0) or 0)
            cost = max(0, int(sf.get("sp_cost", 0) or 0))
            entry = {
                "id": sid,
                "power": pwr,
                "sp_cost": cost,
                "atk_f": int(sf.get("atk_f", 0) or 0),
                "int_f": int(sf.get("int_f", 100) or 0),
                "plus_states": [int(x) for x in sf.get("plus_state_set", []) if int(x) in states_db],
                "fields": sf,
            }
            if pwr < 0:
                heal_skills.append(entry)
            elif pwr > 0:
                atk_skills.append(entry)

        return {
            "kind": "hero",
            "id": aid,
            "level": lv,
            "max_hp": mhp,
            "hp": mhp,
            "max_sp": msp,
            "sp": msp,
            "atk": atk,
            "pdef": pdef,
            "agi": agi,
            "int": int_stat,
            "mdef": mdef,
            "fstats": fstats,
            "heal_skills": sorted(heal_skills, key=lambda s: abs(s["power"]), reverse=True),
            "atk_skills": sorted(atk_skills, key=lambda s: s["power"], reverse=True),
            "states": set(),
        }

    def _is_incapacitated(unit: Dict[str, Any]) -> bool:
        for st_id in unit.get("states", ()):
            rest = int(states_db.get(st_id, {}).get("fields", {}).get("restriction", 0) or 0)
            if rest in (2, 3, 4):
                return True
        return False

    def _apply_slip_damage(unit: Dict[str, Any]) -> None:
        if unit["hp"] <= 0:
            return
        for st_id in list(unit.get("states", ())):
            sf = states_db.get(st_id, {}).get("fields", {})
            if sf.get("slip_damage"):
                dot = max(1, int(unit.get("max_hp", 100) * 0.1))
                unit["hp"] = max(1, unit["hp"] - dot)

    rng = random.Random(42)
    reports: List[Dict[str, Any]] = []
    trials = max(1, int(trials_per_troop))
    for tr in troops_raw:
        tf = tr.get("fields", {})
        tid = int(tf.get("id", 0))
        tname = str(tf.get("name", f"Troop {tid}"))
        wins = 0
        total_turns = 0
        for _t in range(trials):
            party = [_actor_snapshot(aid) for aid in party_ids]
            _tn, enemies = build_battle_enemies(tr, tid, enemies_db)
            for foe in enemies:
                ef = enemies_db.get(int(foe.get("enemy_id", 0)), {}).get("fields", {})
                foe["kind"] = "enemy"
                foe.setdefault("agi", max(1, int(ef.get("agi", 10) or 10)))
                foe.setdefault("max_hp", max(1, int(foe.get("max_hp", foe.get("hp", 80)))))
                foe.setdefault("states", set())
                foe["actions"] = [
                    act.get("fields", act)
                    for act in ef.get("actions", [])
                    if isinstance(act, dict)
                ]
            if not enemies:
                wins += 1
                continue
            turns = 0
            while turns < 40 and any(p["hp"] > 0 for p in party) and any(e["hp"] > 0 for e in enemies):
                turns += 1
                order: List[Tuple[int, Dict[str, Any]]] = [
                    (int(u.get("agi", 10)) + rng.randint(0, 4), u)
                    for u in (party + enemies)
                    if u["hp"] > 0
                ]
                order.sort(key=lambda pair: pair[0], reverse=True)
                for _spd, unit in order:
                    if unit["hp"] <= 0 or _is_incapacitated(unit):
                        continue
                    _apply_slip_damage(unit)
                    if unit["kind"] == "hero":
                        live_e = [e for e in enemies if e["hp"] > 0]
                        if not live_e:
                            break
                        wounded = [p for p in party if p["hp"] > 0 and p["hp"] < p["max_hp"] * 0.5]
                        heal_pick = next((sk for sk in unit["heal_skills"] if unit["sp"] >= sk["sp_cost"]), None)
                        if wounded and heal_pick is not None:
                            unit["sp"] -= heal_pick["sp_cost"]
                            target_ally = min(wounded, key=lambda p: p["hp"])
                            heal_amt = max(15, abs(heal_pick["power"]) + unit["int"] // 2)
                            target_ally["hp"] = min(target_ally["max_hp"], target_ally["hp"] + heal_amt)
                            continue
                        foe = live_e[0]
                        atk_pick = next((sk for sk in unit["atk_skills"] if unit["sp"] >= sk["sp_cost"]), None)
                        if atk_pick is not None and rng.random() < 0.45:
                            unit["sp"] -= atk_pick["sp_cost"]
                            base = formula.skill_damage(unit["fstats"], enemy_formula_stats(foe), atk_pick["fields"])
                            sv = formula.skill_variance_for(atk_pick["fields"])
                            foe["hp"] = max(0, foe["hp"] - max(0, int(round(base * rng.uniform(1 - sv / 100.0, 1 + sv / 100.0)))))
                            for pst in atk_pick["plus_states"]:
                                if rng.random() < 0.5:
                                    foe["states"].add(pst)
                        else:
                            base = formula.attack_damage(unit["fstats"], enemy_formula_stats(foe))
                            av = formula.attack_variance / 100.0
                            mult = rng.uniform(1 - av, 1 + av)
                            if rng.random() < 0.08:
                                mult *= formula.critical_multiplier
                            foe["hp"] = max(0, foe["hp"] - max(0, int(round(base * mult))))
                    else:
                        live_p = [p for p in party if p["hp"] > 0]
                        if not live_p:
                            break
                        target = rng.choice(live_p)
                        skill_acts = [a for a in unit.get("actions", []) if int(a.get("kind", 0)) == 1 and int(a.get("skill_id", 0)) in skills_db]
                        if skill_acts and rng.random() < 0.35:
                            act = rng.choice(skill_acts)
                            sf = skills_db[int(act["skill_id"])].get("fields", {})
                            base = max(1, formula.skill_damage(enemy_formula_stats(unit), target["fstats"], sf))
                            for pst in sf.get("plus_state_set", []):
                                if int(pst) in states_db and rng.random() < 0.35:
                                    target["states"].add(int(pst))
                        else:
                            base = max(1, formula.attack_damage(enemy_formula_stats(unit), target["fstats"]))
                        av = formula.attack_variance / 100.0
                        mult = rng.uniform(1 - av, 1 + av)
                        target["hp"] = max(0, target["hp"] - max(1, int(round(base * mult))))
            if any(p["hp"] > 0 for p in party) and not any(e["hp"] > 0 for e in enemies):
                wins += 1
            total_turns += turns
        win_rate = round(wins / float(trials), 3)
        avg_turns = round(total_turns / float(trials), 2)
        base_lv = party_ids and _actor_snapshot(party_ids[0])["level"] or 1
        rec_lv = base_lv if win_rate >= 0.7 else (base_lv + (2 if win_rate >= 0.35 else 4))
        reports.append({
            "troop_id": tid,
            "name": tname,
            "win_rate": win_rate,
            "avg_turns": avg_turns,
            "recommended_level": rec_lv,
            "status": "balanced" if win_rate >= 0.6 else ("hard" if win_rate >= 0.25 else "overpowered"),
        })
    return {
        "balanced": all(r["win_rate"] >= 0.5 for r in reports),
        "battle_formula": formula.preset,
        "trials_per_troop": trials,
        "troops": reports,
    }


def export_flow_graph(ir_dir: Path, fmt: str = "mermaid") -> str:
    """Exports cross-map warps and switch unlocks as a Mermaid or Graphviz DOT flowchart."""
    ir_dir = Path(ir_dir)
    proj = _load_json(ir_dir / "ir_project.json")
    start_mid = int(proj.get("system", {}).get("fields", {}).get("start_map_id", 1))
    edges: List[Tuple[int, int, str]] = []
    map_names: Dict[int, str] = {}

    for mf in sorted((ir_dir / "maps").glob("map_*.json")):
        m = _load_json(mf)
        mid = int(m.get("map_id", 0))
        map_names[mid] = str(m.get("name", f"Map {mid}"))
        for ev in m.get("events", []):
            ev_name = str(ev.get("name", f"EV{ev.get('id', 0)}"))
            for p_idx, page in enumerate(ev.get("pages", [])):
                cond = page.get("condition", {}).get("fields", page.get("condition", {}))
                sw_tag = f" [SW{cond['switch1_id']}]" if cond.get("switch1_valid") else ""
                for cmd in page.get("commands", []):
                    if cmd.get("op") == "transfer_player" and int(cmd.get("var_mode", 0)) == 0:
                        dst_mid = int(cmd.get("map_id", mid))
                        edges.append((mid, dst_mid, f"{ev_name}#P{p_idx + 1}{sw_tag}"))

    if fmt.lower() == "dot":
        out = ["digraph RPGFlow {", '  rankdir="LR";']
        for mid, mname in sorted(map_names.items()):
            mark = " (Start)" if mid == start_mid else ""
            safe_lbl = mname.replace('"', "'")
            out.append(f'  M{mid} [label="#{mid}: {safe_lbl}{mark}"];')
        for src, dst, lbl in edges:
            safe_e = lbl.replace('"', "'")
            out.append(f'  M{src} -> M{dst} [label="{safe_e}"];')
        out.append("}")
        return "\n".join(out)

    out = ["flowchart LR"]
    for mid, mname in sorted(map_names.items()):
        mark = " (Start)" if mid == start_mid else ""
        safe_lbl = mname.replace('"', "'")
        out.append(f'  M{mid}["#{mid}: {safe_lbl}{mark}"]')
    for src, dst, lbl in edges:
        safe_e = lbl.replace('"', "'")
        out.append(f'  M{src} -->|"{safe_e}"| M{dst}')
    return "\n".join(out)
