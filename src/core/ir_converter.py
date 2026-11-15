# -*- coding: utf-8 -*-
"""
Bidirectional RMXP <-> AI-Native IR Converter.
Converts binary Ruby Marshal (.rxdata) and compressed RGSS scripts into
structured JSON workspaces, Semantic Event DSL commands, and 32x32 tileset manifests.
"""
from __future__ import annotations

import re
import zlib
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .rmxp_marshal import (
    RubyClassRef,
    RubyObject,
    RubyRegexp,
    RubySymbol,
    RubyUserDef,
    marshal_dumps,
    marshal_loads,
)
from .rmxp_types import RgssColor, RgssTable, RgssTone
from .utils import load_json as _read_json, save_json as _write_json

DIRECTION_INT_TO_NAME = {2: "down", 4: "left", 6: "right", 8: "up"}
DIRECTION_NAME_TO_INT = {v: k for k, v in DIRECTION_INT_TO_NAME.items()}
TRIGGER_NAMES = {
    0: "action_button",
    1: "player_touch",
    2: "event_touch",
    3: "autorun",
    4: "parallel",
}
TRIGGER_NAME_TO_INT = {v: k for k, v in TRIGGER_NAMES.items()}


def _strip_ivar_prefix(key: str) -> str:
    return key[1:] if key.startswith("@") else key


def _add_ivar_prefix(key: str) -> str:
    return key if key.startswith("@") else f"@{key}"


def ruby_to_ir(val: Any) -> Any:
    """Recursively converts Marshal wrapper objects into JSON-serializable IR dicts."""
    if val is None or isinstance(val, (bool, int, float, str)):
        return val
    if isinstance(val, bytes):
        return {"__type__": "bytes", "hex": val.hex()}
    if isinstance(val, RubySymbol):
        return {"__type__": "symbol", "name": val.name}
    if isinstance(val, RubyRegexp):
        return {"__type__": "regexp", "pattern": val.pattern, "options": val.options}
    if isinstance(val, RubyClassRef):
        return {"__type__": "class_ref", "kind": val.kind, "name": val.name}
    if isinstance(val, list):
        return [ruby_to_ir(x) for x in val]
    if isinstance(val, dict):
        return {
            "__type__": "int_map" if all(isinstance(k, int) for k in val.keys()) else "dict",
            "entries": [[ruby_to_ir(k), ruby_to_ir(v)] for k, v in val.items()],
        }
    if isinstance(val, RubyUserDef):
        if val.class_name == "Table":
            t = RgssTable.from_user_def(val)
            return {
                "__type__": "Table",
                "dims": t.dims,
                "xsize": t.xsize,
                "ysize": t.ysize,
                "zsize": t.zsize,
                "data": t.data,
            }
        if val.class_name == "Color":
            c = RgssColor.from_user_def(val)
            return {"__type__": "Color", "rgba": [c.red, c.green, c.blue, c.alpha]}
        if val.class_name == "Tone":
            t = RgssTone.from_user_def(val)
            return {"__type__": "Tone", "rgbg": [t.red, t.green, t.blue, t.gray]}
        return {"__type__": "user_def", "class": val.class_name, "hex": val.data.hex()}
    if isinstance(val, RubyObject):
        return {
            "__type__": "RubyObject",
            "class": val.class_name,
            "fields": {_strip_ivar_prefix(k): ruby_to_ir(v) for k, v in val.ivars.items()},
        }
    raise TypeError(f"Unsupported value for IR serialization: {type(val).__name__}")


def ir_to_ruby(val: Any) -> Any:
    """Reconstructs Ruby Marshal wrapper objects from JSON IR dicts."""
    if val is None or isinstance(val, (bool, int, float, str)):
        return val
    if isinstance(val, list):
        return [ir_to_ruby(x) for x in val]
    if isinstance(val, dict):
        tag = val.get("__type__")
        if tag == "bytes":
            return bytes.fromhex(val["hex"])
        if tag == "symbol":
            return RubySymbol(val["name"])
        if tag == "regexp":
            return RubyRegexp(val["pattern"], val["options"])
        if tag == "class_ref":
            return RubyClassRef(val["kind"], val["name"])
        if tag in ("int_map", "dict"):
            return {ir_to_ruby(k): ir_to_ruby(v) for k, v in val["entries"]}
        if tag == "Table":
            return RgssTable(
                dims=val["dims"],
                xsize=val["xsize"],
                ysize=val["ysize"],
                zsize=val["zsize"],
                data=list(val["data"]),
            ).to_user_def()
        if tag == "Color":
            r, g, b, a = val["rgba"]
            return RgssColor(r, g, b, a).to_user_def()
        if tag == "Tone":
            r, g, b, gr = val["rgbg"]
            return RgssTone(r, g, b, gr).to_user_def()
        if tag == "user_def":
            return RubyUserDef(val["class"], bytes.fromhex(val["hex"]))
        if tag == "RubyObject":
            return RubyObject(
                class_name=val["class"],
                ivars={_add_ivar_prefix(k): ir_to_ruby(v) for k, v in val["fields"].items()},
            )
        return {k: ir_to_ruby(v) for k, v in val.items()}
    raise TypeError(f"Unsupported IR value: {type(val).__name__}")


_MARKER_CODE_TO_OP: Dict[int, str] = {
    0: "end",
    112: "loop",
    113: "break_loop",
    115: "exit_event",
    116: "erase_event",
    210: "wait_for_move_completion",
    221: "fadeout_screen",
    222: "fadein_screen",
    247: "memorize_bgm",
    248: "restore_bgm",
    251: "stop_se",
    340: "abort_battle",
    351: "call_menu",
    352: "open_save_menu",
    353: "game_over",
    354: "return_to_title",
    403: "when_cancel",
    404: "end_choices",
    411: "else",
    412: "end_if",
    413: "end_loop",
    601: "when_win",
    602: "when_escape",
    603: "when_lose",
    604: "end_battle",
}
_MARKER_OP_TO_CODE: Dict[str, int] = {v: k for k, v in _MARKER_CODE_TO_OP.items()}

# Mapping: RMXP code <-> (semantic op, target_id field)
_DELTA_CODE_TO_SPEC: Dict[int, Tuple[str, str]] = {
    126: ("change_item", "item_id"),
    127: ("change_weapon", "weapon_id"),
    128: ("change_armor", "armor_id"),
}
_DELTA_OP_TO_SPEC: Dict[str, Tuple[int, str]] = {
    op: (code, id_field) for code, (op, id_field) in _DELTA_CODE_TO_SPEC.items()
}
_AUDIO_CODE_TO_OP: Dict[int, str] = {
    132: "change_battle_bgm",
    133: "change_battle_end_me",
    241: "play_bgm",
    245: "play_bgs",
    249: "play_me",
    250: "play_se",
}
_AUDIO_OP_TO_CODE: Dict[str, int] = {v: k for k, v in _AUDIO_CODE_TO_OP.items()}
_WEATHER_INT_TO_NAME: Dict[int, str] = {0: "none", 1: "rain", 2: "storm", 3: "snow"}
_WEATHER_NAME_TO_INT: Dict[str, int] = {v: k for k, v in _WEATHER_INT_TO_NAME.items()}
_VEHICLE_INT_TO_NAME: Dict[int, str] = {0: "boat", 1: "ship", 2: "airship"}
_VEHICLE_NAME_TO_INT: Dict[str, int] = {v: k for k, v in _VEHICLE_INT_TO_NAME.items()}
_MOVE_ROUTE_CODE_TO_NAME: Dict[int, str] = {
    1: "down", 2: "left", 3: "right", 4: "up",
    5: "down_left", 6: "down_right", 7: "up_left", 8: "up_right",
    9: "random", 15: "wait",
    16: "turn_down", 17: "turn_left", 18: "turn_right", 19: "turn_up",
    31: "walk_anime_on", 32: "walk_anime_off",
    33: "step_anime_on", 34: "step_anime_off",
    35: "dir_fix_on", 36: "dir_fix_off",
    37: "through_on", 38: "through_off",
    39: "always_on_top_on", 40: "always_on_top_off",
}
_MOVE_ROUTE_NAME_TO_CODE: Dict[str, int] = {v: k for k, v in _MOVE_ROUTE_CODE_TO_NAME.items()}


def _encode_move_step(step_str: str) -> RubyObject:
    """Encodes a semantic move-route step string into an RPG::MoveCommand."""
    s = str(step_str)
    if s.startswith("jump:"):
        parts = s.split(":")
        dx = int(parts[1]) if len(parts) > 1 else 0
        dy = int(parts[2]) if len(parts) > 2 else 0
        return RubyObject("RPG::MoveCommand", {"@code": 14, "@parameters": [dx, dy]})
    if s.startswith("speed:"):
        return RubyObject("RPG::MoveCommand", {"@code": 29, "@parameters": [int(s.split(":", 1)[1])]})
    if s.startswith("freq:"):
        return RubyObject("RPG::MoveCommand", {"@code": 30, "@parameters": [int(s.split(":", 1)[1])]})
    if s.startswith("graphic:"):
        parts = s.split(":", 4)
        gname = parts[1] if len(parts) > 1 else ""
        hue = int(parts[2]) if len(parts) > 2 else 0
        gdir = int(parts[3]) if len(parts) > 3 else 2
        pat = int(parts[4]) if len(parts) > 4 else 0
        return RubyObject("RPG::MoveCommand", {"@code": 41, "@parameters": [gname, hue, gdir, pat]})
    if s.startswith("opacity:"):
        return RubyObject("RPG::MoveCommand", {"@code": 42, "@parameters": [int(s.split(":", 1)[1])]})
    if s.startswith("blend:"):
        return RubyObject("RPG::MoveCommand", {"@code": 43, "@parameters": [int(s.split(":", 1)[1])]})
    return RubyObject("RPG::MoveCommand", {"@code": _MOVE_ROUTE_NAME_TO_CODE.get(s, 15), "@parameters": []})


def _decode_move_command(mc: RubyObject) -> Optional[str]:
    """Decodes an RPG::MoveCommand into a semantic string step."""
    mc_code = int(mc.ivars.get("@code", 0))
    m_params = list(mc.ivars.get("@parameters", []))
    if mc_code == 14 and len(m_params) >= 2:
        return f"jump:{int(m_params[0])}:{int(m_params[1])}"
    if mc_code == 29 and m_params:
        return f"speed:{int(m_params[0])}"
    if mc_code == 30 and m_params:
        return f"freq:{int(m_params[0])}"
    if mc_code == 41 and len(m_params) >= 4:
        return f"graphic:{m_params[0]}:{int(m_params[1])}:{int(m_params[2])}:{int(m_params[3])}"
    if mc_code == 42 and m_params:
        return f"opacity:{int(m_params[0])}"
    if mc_code == 43 and m_params:
        return f"blend:{int(m_params[0])}"
    return _MOVE_ROUTE_CODE_TO_NAME.get(mc_code)


def _decode_conditional_branch(params: List[Any], indent: int) -> Dict[str, Any]:
    cond_type = int(params[0]) if params else 0
    if cond_type == 0 and len(params) >= 3:
        return {
            "op": "if_switch",
            "switch_id": int(params[1]),
            "expected": "ON" if int(params[2]) == 0 else "OFF",
            "indent": indent,
        }
    if cond_type == 1 and len(params) >= 5:
        return {
            "op": "if_variable",
            "variable_id": int(params[1]),
            "rhs_is_var": bool(params[2]),
            "rhs_value": int(params[3]),
            "cmp": int(params[4]),
            "indent": indent,
        }
    if cond_type == 2 and len(params) >= 3:
        return {
            "op": "if_self_switch",
            "channel": str(params[1]),
            "expected": "ON" if int(params[2]) == 0 else "OFF",
            "indent": indent,
        }
    if cond_type == 3 and len(params) >= 3:
        return {"op": "if_timer", "seconds": int(params[1]), "cmp": int(params[2]), "indent": indent}
    if cond_type == 6 and len(params) >= 2:
        return {"op": "if_vehicle", "vehicle": _VEHICLE_INT_TO_NAME.get(int(params[1]), "boat"), "indent": indent}
    if cond_type == 4 and len(params) >= 3:
        sub = int(params[2])
        aid = int(params[1])
        if sub == 0:
            return {"op": "if_actor_in_party", "actor_id": aid, "indent": indent}
        if sub == 1 and len(params) >= 4:
            return {"op": "if_actor_name", "actor_id": aid, "name": str(params[3]), "indent": indent}
        if sub == 4 and len(params) >= 4:
            return {"op": "if_actor_skill", "actor_id": aid, "skill_id": int(params[3]), "indent": indent}
        if sub == 5 and len(params) >= 4:
            return {"op": "if_actor_weapon", "actor_id": aid, "weapon_id": int(params[3]), "indent": indent}
        if sub == 6 and len(params) >= 4:
            return {"op": "if_actor_armor", "actor_id": aid, "armor_id": int(params[3]), "indent": indent}
        if sub == 7 and len(params) >= 4:
            return {"op": "if_actor_state", "actor_id": aid, "state_id": int(params[3]), "indent": indent}
    if cond_type == 7 and len(params) >= 3:
        return {
            "op": "if_gold",
            "amount": int(params[1]),
            "cmp": int(params[2]),
            "indent": indent,
        }
    if cond_type == 8 and len(params) >= 2:
        return {"op": "if_item", "item_id": int(params[1]), "indent": indent}
    if cond_type == 9 and len(params) >= 2:
        return {"op": "if_weapon", "weapon_id": int(params[1]), "indent": indent}
    if cond_type == 10 and len(params) >= 2:
        return {"op": "if_armor", "armor_id": int(params[1]), "indent": indent}
    return {"op": "if_raw", "parameters": ruby_to_ir(params), "indent": indent}


def _decode_single_command(code: int, params: List[Any], indent: int) -> Dict[str, Any]:
    """Decodes one non-dialogue RMXP EventCommand into a semantic DSL entry."""
    marker_op = _MARKER_CODE_TO_OP.get(code)
    if marker_op is not None:
        return {"op": marker_op, "indent": indent}

    delta_spec = _DELTA_CODE_TO_SPEC.get(code)
    if delta_spec is not None and len(params) >= 4:
        op_name, id_field = delta_spec
        return {
            "op": op_name,
            id_field: int(params[0]),
            "mode": "add" if int(params[1]) == 0 else "sub",
            "operand_type": int(params[2]),
            "amount": int(params[3]),
            "indent": indent,
        }

    if code == 102:
        return {
            "op": "show_choices",
            "choices": list(params[0]) if params else [],
            "cancel_type": int(params[1]) if len(params) > 1 else 0,
            "indent": indent,
        }
    if code == 402:
        return {
            "op": "when_choice",
            "index": int(params[0]) if params else 0,
            "label": str(params[1]) if len(params) > 1 else "",
            "indent": indent,
        }
    if code == 111:
        return _decode_conditional_branch(params, indent)
    if code == 117 and len(params) >= 1:
        return {"op": "common_event", "common_event_id": int(params[0]), "indent": indent}
    if code == 118 and len(params) >= 1:
        return {"op": "label", "name": str(params[0]), "indent": indent}
    if code == 119 and len(params) >= 1:
        return {"op": "jump_to_label", "name": str(params[0]), "indent": indent}
    if code == 121 and len(params) >= 3:
        return {
            "op": "set_switch",
            "start_id": int(params[0]),
            "end_id": int(params[1]),
            "value": "ON" if int(params[2]) == 0 else "OFF",
            "indent": indent,
        }
    if code == 122 and len(params) >= 5:
        return {
            "op": "set_variable",
            "start_id": int(params[0]),
            "end_id": int(params[1]),
            "operation": int(params[2]),
            "operand_type": int(params[3]),
            "operand": ruby_to_ir(params[4:]),
            "indent": indent,
        }
    if code == 123 and len(params) >= 2:
        return {
            "op": "set_self_switch",
            "channel": str(params[0]),
            "value": "ON" if int(params[1]) == 0 else "OFF",
            "indent": indent,
        }
    if code == 125 and len(params) >= 3:
        return {
            "op": "change_gold",
            "mode": "add" if int(params[0]) == 0 else "sub",
            "operand_type": int(params[1]),
            "amount": int(params[2]),
            "indent": indent,
        }
    if code == 201 and len(params) >= 6:
        return {
            "op": "transfer_player",
            "var_mode": int(params[0]),
            "map_id": int(params[1]),
            "x": int(params[2]),
            "y": int(params[3]),
            "direction": DIRECTION_INT_TO_NAME.get(int(params[4]), "retain"),
            "fade_type": int(params[5]),
            "indent": indent,
        }
    if code == 301 and len(params) >= 3:
        return {
            "op": "battle",
            "troop_id": int(params[0]),
            "can_escape": bool(params[1]),
            "can_lose": bool(params[2]),
            "indent": indent,
        }
    if code == 129 and len(params) >= 3:
        return {
            "op": "change_party",
            "actor_id": int(params[0]),
            "mode": "add" if int(params[1]) == 0 else "remove",
            "initialize": bool(params[2]),
            "indent": indent,
        }
    if code == 311 and len(params) >= 5:
        return {
            "op": "change_hp",
            "actor_id": int(params[0]),
            "mode": "add" if int(params[1]) == 0 else "sub",
            "operand_type": int(params[2]),
            "amount": int(params[3]),
            "allow_knockout": bool(params[4]),
            "indent": indent,
        }
    if code == 312 and len(params) >= 4:
        return {
            "op": "change_sp",
            "actor_id": int(params[0]),
            "mode": "add" if int(params[1]) == 0 else "sub",
            "operand_type": int(params[2]),
            "amount": int(params[3]),
            "indent": indent,
        }
    if code == 313 and len(params) >= 3:
        return {
            "op": "change_state",
            "actor_id": int(params[0]),
            "mode": "add" if int(params[1]) == 0 else "remove",
            "state_id": int(params[2]),
            "indent": indent,
        }
    if code == 314 and len(params) >= 1:
        return {"op": "recover_all", "actor_id": int(params[0]), "indent": indent}
    if code == 315 and len(params) >= 4:
        return {
            "op": "change_exp",
            "actor_id": int(params[0]),
            "mode": "add" if int(params[1]) == 0 else "sub",
            "operand_type": int(params[2]),
            "amount": int(params[3]),
            "indent": indent,
        }
    if code == 316 and len(params) >= 4:
        return {
            "op": "change_level",
            "actor_id": int(params[0]),
            "mode": "add" if int(params[1]) == 0 else "sub",
            "operand_type": int(params[2]),
            "amount": int(params[3]),
            "indent": indent,
        }
    if code == 317 and len(params) >= 5:
        return {
            "op": "change_parameter",
            "actor_id": int(params[0]),
            "param_id": int(params[1]),
            "mode": "add" if int(params[2]) == 0 else "sub",
            "operand_type": int(params[3]),
            "amount": int(params[4]),
            "indent": indent,
        }
    if code == 320 and len(params) >= 2:
        return {"op": "change_actor_name", "actor_id": int(params[0]), "name": str(params[1]), "indent": indent}
    if code == 321 and len(params) >= 2:
        return {"op": "change_actor_class", "actor_id": int(params[0]), "class_id": int(params[1]), "indent": indent}
    if code == 322 and len(params) >= 5:
        return {
            "op": "change_actor_graphic",
            "actor_id": int(params[0]),
            "character_name": str(params[1]),
            "character_hue": int(params[2]),
            "battler_name": str(params[3]),
            "battler_hue": int(params[4]),
            "indent": indent,
        }
    if code == 318 and len(params) >= 3:
        return {
            "op": "change_skill",
            "actor_id": int(params[0]),
            "mode": "learn" if int(params[1]) == 0 else "forget",
            "skill_id": int(params[2]),
            "indent": indent,
        }
    if code == 331 and len(params) >= 4:
        return {
            "op": "change_enemy_hp",
            "enemy_index": int(params[0]),
            "mode": "add" if int(params[1]) == 0 else "sub",
            "operand_type": int(params[2]),
            "amount": int(params[3]),
            "allow_knockout": bool(params[4]) if len(params) >= 5 else False,
            "indent": indent,
        }
    if code == 332 and len(params) >= 4:
        return {
            "op": "change_enemy_sp",
            "enemy_index": int(params[0]),
            "mode": "add" if int(params[1]) == 0 else "sub",
            "operand_type": int(params[2]),
            "amount": int(params[3]),
            "indent": indent,
        }
    if code == 333 and len(params) >= 3:
        return {
            "op": "change_enemy_state",
            "enemy_index": int(params[0]),
            "mode": "add" if int(params[1]) == 0 else "remove",
            "state_id": int(params[2]),
            "indent": indent,
        }
    if code == 334 and len(params) >= 1:
        return {"op": "enemy_recover_all", "enemy_index": int(params[0]), "indent": indent}
    if code in _AUDIO_CODE_TO_OP and params:
        af = params[0]
        af_iv = af.ivars if isinstance(af, RubyObject) else {}
        return {
            "op": _AUDIO_CODE_TO_OP[code],
            "name": str(af_iv.get("@name", "")),
            "volume": int(af_iv.get("@volume", 80)),
            "pitch": int(af_iv.get("@pitch", 100)),
            "indent": indent,
        }
    if code == 106 and len(params) >= 1:
        return {"op": "wait_frames", "frames": int(params[0]), "indent": indent}
    if code == 103 and len(params) >= 2:
        return {"op": "input_number", "variable_id": int(params[0]), "digits": int(params[1]), "indent": indent}
    if code == 231 and len(params) >= 8:
        return {
            "op": "show_picture",
            "number": int(params[0]),
            "name": str(params[1]),
            "origin": int(params[2]),
            "var_mode": int(params[3]),
            "x": int(params[4]),
            "y": int(params[5]),
            "zoom_x": int(params[6]),
            "zoom_y": int(params[7]),
            "opacity": int(params[8]) if len(params) > 8 else 255,
            "indent": indent,
        }
    if code == 232 and len(params) >= 9:
        return {
            "op": "move_picture",
            "number": int(params[0]),
            "frames": int(params[1]),
            "origin": int(params[2]),
            "var_mode": int(params[3]),
            "x": int(params[4]),
            "y": int(params[5]),
            "zoom_x": int(params[6]),
            "zoom_y": int(params[7]),
            "opacity": int(params[8]),
            "indent": indent,
        }
    if code == 235 and len(params) >= 1:
        return {"op": "erase_picture", "number": int(params[0]), "indent": indent}
    if code == 124 and len(params) >= 1:
        op_mode = "start" if int(params[0]) == 0 else "stop"
        secs = int(params[1]) if len(params) > 1 else 0
        return {"op": "control_timer", "mode": op_mode, "seconds": secs, "indent": indent}
    if code == 131 and len(params) >= 1:
        return {"op": "change_windowskin", "windowskin_name": str(params[0]), "indent": indent}
    if code == 134 and len(params) >= 1:
        return {"op": "change_save_access", "enabled": int(params[0]) == 0, "indent": indent}
    if code == 135 and len(params) >= 1:
        return {"op": "change_menu_access", "enabled": int(params[0]) == 0, "indent": indent}
    if code == 136 and len(params) >= 1:
        return {"op": "change_encounter", "enabled": int(params[0]) == 0, "indent": indent}
    if code == 208 and len(params) >= 1:
        return {"op": "change_transparent_flag", "transparent": int(params[0]) == 0, "indent": indent}
    if code == 204 and len(params) >= 3:
        return {
            "op": "scroll_map",
            "direction": DIRECTION_INT_TO_NAME.get(int(params[0]), "down"),
            "distance": int(params[1]),
            "speed": int(params[2]),
            "indent": indent,
        }
    if code == 207 and len(params) >= 2:
        return {
            "op": "show_animation",
            "character_id": int(params[0]),
            "animation_id": int(params[1]),
            "indent": indent,
        }
    if code == 213 and len(params) >= 2:
        return {
            "op": "show_balloon",
            "character_id": int(params[0]),
            "balloon_id": int(params[1]),
            "indent": indent,
        }
    if code == 202 and len(params) >= 5:
        return {
            "op": "set_vehicle_location",
            "vehicle": _VEHICLE_INT_TO_NAME.get(int(params[0]), "boat"),
            "var_mode": int(params[1]),
            "map_id": int(params[2]),
            "x": int(params[3]),
            "y": int(params[4]),
            "indent": indent,
        }
    if code == 203 and len(params) >= 5:
        return {
            "op": "set_event_location",
            "event_id": int(params[0]),
            "var_mode": int(params[1]),
            "x": int(params[2]),
            "y": int(params[3]),
            "direction": DIRECTION_INT_TO_NAME.get(int(params[4]), "retain"),
            "indent": indent,
        }
    if code == 223 and len(params) >= 2:
        tone_ir = ruby_to_ir(params[0])
        rgbg = tone_ir.get("rgbg", [0, 0, 0, 0]) if isinstance(tone_ir, dict) else [0, 0, 0, 0]
        return {"op": "change_screen_tone", "rgbg": [int(x) for x in rgbg], "frames": int(params[1]), "indent": indent}
    if code == 224 and len(params) >= 2:
        col_ir = ruby_to_ir(params[0])
        rgba = col_ir.get("rgba", [255, 255, 255, 255]) if isinstance(col_ir, dict) else [255, 255, 255, 255]
        return {"op": "screen_flash", "rgba": [int(x) for x in rgba], "frames": int(params[1]), "indent": indent}
    if code == 225 and len(params) >= 3:
        return {
            "op": "screen_shake",
            "power": int(params[0]),
            "speed": int(params[1]),
            "frames": int(params[2]),
            "indent": indent,
        }
    if code == 236 and len(params) >= 3:
        return {
            "op": "set_weather",
            "weather": _WEATHER_INT_TO_NAME.get(int(params[0]), "none"),
            "power": int(params[1]),
            "frames": int(params[2]),
            "indent": indent,
        }
    if code == 242 and len(params) >= 1:
        return {"op": "fadeout_bgm", "seconds": int(params[0]), "indent": indent}
    if code == 246 and len(params) >= 1:
        return {"op": "fadeout_bgs", "seconds": int(params[0]), "indent": indent}
    if code == 104 and len(params) >= 2:
        return {
            "op": "change_text_options",
            "position": int(params[0]),
            "show_frame": int(params[1]) == 0,
            "indent": indent,
        }
    if code == 105 and len(params) >= 1:
        return {"op": "button_input_processing", "variable_id": int(params[0]), "indent": indent}
    if code == 205 and len(params) >= 2:
        t_iv = params[0].ivars if hasattr(params[0], "ivars") else {}
        return {
            "op": "change_fog_tone",
            "rgbg": [int(t_iv.get("@red", 0)), int(t_iv.get("@green", 0)), int(t_iv.get("@blue", 0)), int(t_iv.get("@gray", 0))],
            "frames": int(params[1]),
            "indent": indent,
        }
    if code == 206 and len(params) >= 2:
        return {"op": "change_fog_opacity", "opacity": int(params[0]), "frames": int(params[1]), "indent": indent}
    if code == 233 and len(params) >= 2:
        return {"op": "rotate_picture", "number": int(params[0]), "speed": int(params[1]), "indent": indent}
    if code == 234 and len(params) >= 3:
        t_iv = params[1].ivars if hasattr(params[1], "ivars") else {}
        return {
            "op": "change_picture_tone",
            "number": int(params[0]),
            "rgbg": [int(t_iv.get("@red", 0)), int(t_iv.get("@green", 0)), int(t_iv.get("@blue", 0)), int(t_iv.get("@gray", 0))],
            "frames": int(params[2]),
            "indent": indent,
        }
    if code == 303 and len(params) >= 2:
        return {"op": "name_input", "actor_id": int(params[0]), "max_chars": int(params[1]), "indent": indent}
    if code == 335 and len(params) >= 1:
        return {"op": "enemy_appear", "enemy_index": int(params[0]), "indent": indent}
    if code == 336 and len(params) >= 2:
        return {"op": "enemy_transform", "enemy_index": int(params[0]), "enemy_id": int(params[1]), "indent": indent}
    if code == 337 and len(params) >= 3:
        return {
            "op": "show_battle_animation",
            "target_type": int(params[0]),
            "target_index": int(params[1]),
            "animation_id": int(params[2]),
            "indent": indent,
        }
    if code == 338 and len(params) >= 4:
        return {
            "op": "deal_damage",
            "target_type": int(params[0]),
            "target_index": int(params[1]),
            "value_type": int(params[2]),
            "value": int(params[3]),
            "indent": indent,
        }
    if code == 339 and len(params) >= 5:
        return {
            "op": "force_action",
            "battler_type": int(params[0]),
            "battler_index": int(params[1]),
            "action_kind": int(params[2]),
            "basic_or_skill": int(params[3]),
            "target_index": int(params[4]),
            "forcing_timing": int(params[5]) if len(params) >= 6 else 0,
            "indent": indent,
        }
    if code == 319 and len(params) >= 3:
        return {
            "op": "change_equip",
            "actor_id": int(params[0]),
            "equip_type": int(params[1]),
            "equip_id": int(params[2]),
            "indent": indent,
        }
    return {"op": "raw", "code": code, "parameters": ruby_to_ir(params), "indent": indent}


def commands_to_semantic_dsl(commands: List[RubyObject]) -> List[Dict[str, Any]]:
    """
    Translates RMXP numeric RPG::EventCommand lists into readable semantic DSL ops.
    Guarantees lossless round-tripping via 'raw' fallback for uncommon opcodes.
    """
    dsl: List[Dict[str, Any]] = []
    i = 0
    total = len(commands)
    while i < total:
        cmd = commands[i]
        code = int(cmd.ivars.get("@code", 0))
        indent = int(cmd.ivars.get("@indent", 0))
        params = list(cmd.ivars.get("@parameters", []))

        ml_blocks = {101: (401, "say"), 108: (408, "comment"), 355: (655, "script")}
        if code in ml_blocks:
            cont_code, op_name = ml_blocks[code]
            lines = [str(params[0]) if params else ""]
            j = i + 1
            while j < total:
                nxt = commands[j]
                if int(nxt.ivars.get("@code", 0)) == cont_code and int(nxt.ivars.get("@indent", 0)) == indent:
                    nxt_params = nxt.ivars.get("@parameters", [])
                    lines.append(str(nxt_params[0]) if nxt_params else "")
                    j += 1
                else:
                    break
            dsl.append({"op": op_name, "lines": lines, "indent": indent})
            i = j
            continue

        if code == 209 and len(params) >= 2:
            target_id = int(params[0])
            route_obj = params[1]
            route_iv = route_obj.ivars if isinstance(route_obj, RubyObject) else {}
            steps: List[str] = []
            for mc in route_iv.get("@list", []):
                if isinstance(mc, RubyObject):
                    decoded = _decode_move_command(mc)
                    if decoded is not None:
                        steps.append(decoded)
            j = i + 1
            while j < total and int(commands[j].ivars.get("@code", 0)) == 509 and int(commands[j].ivars.get("@indent", 0)) == indent:
                j += 1
            dsl.append({
                "op": "set_move_route",
                "target_id": target_id,
                "steps": steps,
                "repeat": bool(route_iv.get("@repeat", False)),
                "skippable": bool(route_iv.get("@skippable", True)),
                "indent": indent,
            })
            i = j
            continue

        if code == 302:
            type_map = {0: "item", 1: "weapon", 2: "armor"}
            goods: List[Dict[str, Any]] = []
            if len(params) >= 2:
                goods.append({"type": type_map.get(int(params[0]), "item"), "id": int(params[1])})
            j = i + 1
            while j < total:
                nxt = commands[j]
                if int(nxt.ivars.get("@code", 0)) == 605 and int(nxt.ivars.get("@indent", 0)) == indent:
                    gp = list(nxt.ivars.get("@parameters", []))
                    if len(gp) >= 2:
                        goods.append({"type": type_map.get(int(gp[0]), "item"), "id": int(gp[1])})
                    j += 1
                else:
                    break
            dsl.append({"op": "shop", "goods": goods, "indent": indent})
            i = j
            continue

        dsl.append(_decode_single_command(code, params, indent))
        i += 1
    return dsl


def semantic_dsl_to_commands(dsl: List[Dict[str, Any]]) -> List[RubyObject]:
    """Rebuilds RMXP RPG::EventCommand objects from semantic DSL instructions."""
    cmds: List[RubyObject] = []

    def push(code: int, indent: int, params: List[Any]) -> None:
        cmds.append(RubyObject("RPG::EventCommand", {
            "@code": code,
            "@indent": indent,
            "@parameters": params,
        }))

    for entry in dsl:
        op = entry["op"]
        indent = int(entry.get("indent", 0))

        marker_code = _MARKER_OP_TO_CODE.get(op)
        if marker_code is not None:
            push(marker_code, indent, [])
            continue

        delta_spec = _DELTA_OP_TO_SPEC.get(op)
        if delta_spec is not None:
            d_code, id_field = delta_spec
            mode = 0 if entry.get("mode", "add") == "add" else 1
            push(d_code, indent, [
                int(entry[id_field]),
                mode,
                int(entry.get("operand_type", 0)),
                int(entry.get("amount", 1)),
            ])
            continue

        elif op in ("say", "comment", "script"):
            head_code, cont_code = {"say": (101, 401), "comment": (108, 408), "script": (355, 655)}[op]
            lines = entry.get("lines") or [""]
            push(head_code, indent, [str(lines[0])])
            for extra in lines[1:]:
                push(cont_code, indent, [str(extra)])
        elif op == "show_choices":
            push(102, indent, [list(entry.get("choices", [])), int(entry.get("cancel_type", 0))])
        elif op == "when_choice":
            push(402, indent, [int(entry.get("index", 0)), str(entry.get("label", ""))])
        elif op == "if_switch":
            val_code = 0 if str(entry.get("expected", "ON")).upper() == "ON" else 1
            push(111, indent, [0, int(entry["switch_id"]), val_code])
        elif op == "if_variable":
            push(111, indent, [
                1,
                int(entry["variable_id"]),
                1 if entry.get("rhs_is_var") else 0,
                int(entry.get("rhs_value", 0)),
                int(entry.get("cmp", 0)),
            ])
        elif op == "if_self_switch":
            val_code = 0 if str(entry.get("expected", "ON")).upper() == "ON" else 1
            push(111, indent, [2, str(entry["channel"]), val_code])
        elif op == "if_timer":
            push(111, indent, [3, int(entry.get("seconds", 0)), int(entry.get("cmp", 0))])
        elif op == "if_vehicle":
            push(111, indent, [6, _VEHICLE_NAME_TO_INT.get(str(entry.get("vehicle", "boat")), 0)])
        elif op == "if_actor_in_party":
            push(111, indent, [4, int(entry["actor_id"]), 0])
        elif op == "if_actor_name":
            push(111, indent, [4, int(entry["actor_id"]), 1, str(entry.get("name", ""))])
        elif op == "if_actor_skill":
            push(111, indent, [4, int(entry["actor_id"]), 4, int(entry["skill_id"])])
        elif op == "if_actor_weapon":
            push(111, indent, [4, int(entry["actor_id"]), 5, int(entry["weapon_id"])])
        elif op == "if_actor_armor":
            push(111, indent, [4, int(entry["actor_id"]), 6, int(entry["armor_id"])])
        elif op == "if_actor_state":
            push(111, indent, [4, int(entry["actor_id"]), 7, int(entry["state_id"])])
        elif op == "if_gold":
            push(111, indent, [7, int(entry["amount"]), int(entry.get("cmp", 0))])
        elif op == "if_item":
            push(111, indent, [8, int(entry["item_id"])])
        elif op == "if_weapon":
            push(111, indent, [9, int(entry["weapon_id"])])
        elif op == "if_armor":
            push(111, indent, [10, int(entry["armor_id"])])
        elif op == "if_raw":
            push(111, indent, ir_to_ruby(entry["parameters"]))
        elif op == "common_event":
            push(117, indent, [int(entry["common_event_id"])])
        elif op == "label":
            push(118, indent, [str(entry["name"])])
        elif op == "jump_to_label":
            push(119, indent, [str(entry["name"])])
        elif op == "set_switch":
            val_code = 0 if str(entry.get("value", "ON")).upper() == "ON" else 1
            push(121, indent, [int(entry["start_id"]), int(entry["end_id"]), val_code])
        elif op == "set_variable":
            operand = ir_to_ruby(entry.get("operand", [0]))
            push(122, indent, [
                int(entry["start_id"]),
                int(entry["end_id"]),
                int(entry.get("operation", 0)),
                int(entry.get("operand_type", 0)),
                *operand,
            ])
        elif op == "set_self_switch":
            val_code = 0 if str(entry.get("value", "ON")).upper() == "ON" else 1
            push(123, indent, [str(entry["channel"]), val_code])
        elif op == "change_gold":
            mode = 0 if entry.get("mode", "add") == "add" else 1
            push(125, indent, [mode, int(entry.get("operand_type", 0)), int(entry.get("amount", 0))])
        elif op == "change_party":
            mode = 0 if entry.get("mode", "add") == "add" else 1
            push(129, indent, [int(entry["actor_id"]), mode, 1 if entry.get("initialize") else 0])
        elif op == "transfer_player":
            d_raw = entry.get("direction", "down")
            d_int = 0 if d_raw == "retain" else DIRECTION_NAME_TO_INT.get(str(d_raw), 2)
            push(201, indent, [
                int(entry.get("var_mode", 0)),
                int(entry["map_id"]),
                int(entry["x"]),
                int(entry["y"]),
                d_int,
                int(entry.get("fade_type", 0)),
            ])
        elif op == "set_move_route":
            move_cmds = [_encode_move_step(s) for s in entry.get("steps", [])]
            move_cmds.append(RubyObject("RPG::MoveCommand", {"@code": 0, "@parameters": []}))
            route_obj = RubyObject("RPG::MoveRoute", {
                "@repeat": bool(entry.get("repeat", False)),
                "@skippable": bool(entry.get("skippable", True)),
                "@list": move_cmds,
            })
            push(209, indent, [int(entry.get("target_id", 0)), route_obj])
            for mc in move_cmds[:-1]:
                push(509, indent, [mc])
        elif op == "battle":
            push(301, indent, [
                int(entry["troop_id"]),
                bool(entry.get("can_escape", False)),
                bool(entry.get("can_lose", False)),
            ])
        elif op == "shop":
            type_to_int = {"item": 0, "weapon": 1, "armor": 2}
            for idx, g in enumerate(entry.get("goods", [])):
                g_type = type_to_int.get(str(g.get("type", "item")), 0)
                push(302 if idx == 0 else 605, indent, [g_type, int(g["id"])])
        elif op == "change_hp":
            mode = 0 if entry.get("mode", "add") == "add" else 1
            push(311, indent, [
                int(entry.get("actor_id", 0)),
                mode,
                int(entry.get("operand_type", 0)),
                int(entry.get("amount", 0)),
                bool(entry.get("allow_knockout", False)),
            ])
        elif op == "change_sp":
            mode = 0 if entry.get("mode", "add") == "add" else 1
            push(312, indent, [int(entry.get("actor_id", 0)), mode, int(entry.get("operand_type", 0)), int(entry.get("amount", 0))])
        elif op == "change_state":
            mode = 0 if entry.get("mode", "add") == "add" else 1
            push(313, indent, [int(entry.get("actor_id", 0)), mode, int(entry["state_id"])])
        elif op == "recover_all":
            push(314, indent, [int(entry.get("actor_id", 0))])
        elif op == "change_exp":
            mode = 0 if entry.get("mode", "add") == "add" else 1
            push(315, indent, [int(entry.get("actor_id", 0)), mode, int(entry.get("operand_type", 0)), int(entry.get("amount", 0))])
        elif op == "change_level":
            mode = 0 if entry.get("mode", "add") == "add" else 1
            push(316, indent, [int(entry.get("actor_id", 0)), mode, int(entry.get("operand_type", 0)), int(entry.get("amount", 0))])
        elif op == "change_parameter":
            mode = 0 if entry.get("mode", "add") == "add" else 1
            push(317, indent, [
                int(entry.get("actor_id", 1)),
                int(entry.get("param_id", 0)),
                mode,
                int(entry.get("operand_type", 0)),
                int(entry.get("amount", 0)),
            ])
        elif op == "change_actor_name":
            push(320, indent, [int(entry.get("actor_id", 1)), str(entry.get("name", ""))])
        elif op == "change_actor_class":
            push(321, indent, [int(entry.get("actor_id", 1)), int(entry.get("class_id", 1))])
        elif op == "change_actor_graphic":
            push(322, indent, [
                int(entry.get("actor_id", 1)),
                str(entry.get("character_name", "")),
                int(entry.get("character_hue", 0)),
                str(entry.get("battler_name", "")),
                int(entry.get("battler_hue", 0)),
            ])
        elif op == "change_skill":
            mode = 0 if entry.get("mode", "learn") == "learn" else 1
            push(318, indent, [int(entry.get("actor_id", 1)), mode, int(entry["skill_id"])])
        elif op == "change_enemy_hp":
            mode = 0 if entry.get("mode", "add") == "add" else 1
            push(331, indent, [
                int(entry.get("enemy_index", 0)),
                mode,
                int(entry.get("operand_type", 0)),
                int(entry.get("amount", 0)),
                bool(entry.get("allow_knockout", False)),
            ])
        elif op == "change_enemy_sp":
            mode = 0 if entry.get("mode", "add") == "add" else 1
            push(332, indent, [
                int(entry.get("enemy_index", 0)),
                mode,
                int(entry.get("operand_type", 0)),
                int(entry.get("amount", 0)),
            ])
        elif op == "change_enemy_state":
            mode = 0 if entry.get("mode", "add") == "add" else 1
            push(333, indent, [int(entry.get("enemy_index", 0)), mode, int(entry["state_id"])])
        elif op == "enemy_recover_all":
            push(334, indent, [int(entry.get("enemy_index", 0))])
        elif op in _AUDIO_OP_TO_CODE:
            af_obj = RubyObject("RPG::AudioFile", {
                "@name": str(entry.get("name", "")),
                "@volume": int(entry.get("volume", 80)),
                "@pitch": int(entry.get("pitch", 100)),
            })
            push(_AUDIO_OP_TO_CODE[op], indent, [af_obj])
        elif op == "fadeout_bgm":
            push(242, indent, [int(entry.get("seconds", 2))])
        elif op == "fadeout_bgs":
            push(246, indent, [int(entry.get("seconds", 2))])
        elif op == "wait_frames":
            push(106, indent, [int(entry.get("frames", 20))])
        elif op == "input_number":
            push(103, indent, [int(entry.get("variable_id", 1)), int(entry.get("digits", 2))])
        elif op == "show_picture":
            push(231, indent, [
                int(entry.get("number", 1)),
                str(entry.get("name", "")),
                int(entry.get("origin", 0)),
                int(entry.get("var_mode", 0)),
                int(entry.get("x", 0)),
                int(entry.get("y", 0)),
                int(entry.get("zoom_x", 100)),
                int(entry.get("zoom_y", 100)),
                int(entry.get("opacity", 255)),
                0,
            ])
        elif op == "move_picture":
            push(232, indent, [
                int(entry.get("number", 1)),
                int(entry.get("frames", 20)),
                int(entry.get("origin", 0)),
                int(entry.get("var_mode", 0)),
                int(entry.get("x", 0)),
                int(entry.get("y", 0)),
                int(entry.get("zoom_x", 100)),
                int(entry.get("zoom_y", 100)),
                int(entry.get("opacity", 255)),
                0,
            ])
        elif op == "erase_picture":
            push(235, indent, [int(entry.get("number", 1))])
        elif op == "control_timer":
            t_mode = 0 if entry.get("mode", "start") == "start" else 1
            push(124, indent, [t_mode, int(entry.get("seconds", 0))])
        elif op == "change_windowskin":
            push(131, indent, [str(entry.get("windowskin_name", ""))])
        elif op == "change_save_access":
            push(134, indent, [0 if entry.get("enabled", True) else 1])
        elif op == "change_menu_access":
            push(135, indent, [0 if entry.get("enabled", True) else 1])
        elif op == "change_encounter":
            push(136, indent, [0 if entry.get("enabled", True) else 1])
        elif op == "change_transparent_flag":
            push(208, indent, [0 if entry.get("transparent", False) else 1])
        elif op == "scroll_map":
            d_int = DIRECTION_NAME_TO_INT.get(str(entry.get("direction", "down")), 2)
            push(204, indent, [d_int, int(entry.get("distance", 1)), int(entry.get("speed", 4))])
        elif op == "show_animation":
            push(207, indent, [int(entry.get("character_id", -1)), int(entry.get("animation_id", 1))])
        elif op == "show_balloon":
            push(213, indent, [int(entry.get("character_id", -1)), int(entry.get("balloon_id", 1))])
        elif op == "set_vehicle_location":
            v_int = _VEHICLE_NAME_TO_INT.get(str(entry.get("vehicle", "boat")), 0)
            push(202, indent, [
                v_int,
                int(entry.get("var_mode", 0)),
                int(entry.get("map_id", 1)),
                int(entry.get("x", 0)),
                int(entry.get("y", 0)),
            ])
        elif op == "set_event_location":
            d_raw = entry.get("direction", "retain")
            d_int = 0 if d_raw == "retain" else DIRECTION_NAME_TO_INT.get(str(d_raw), 0)
            push(203, indent, [
                int(entry.get("event_id", 0)),
                int(entry.get("var_mode", 0)),
                int(entry.get("x", 0)),
                int(entry.get("y", 0)),
                d_int,
            ])
        elif op == "set_weather":
            w_int = _WEATHER_NAME_TO_INT.get(str(entry.get("weather", "none")), 0)
            push(236, indent, [w_int, int(entry.get("power", 5)), int(entry.get("frames", 20))])
        elif op == "change_screen_tone":
            rgbg = [int(x) for x in entry.get("rgbg", [0, 0, 0, 0])]
            push(223, indent, [RgssTone(*rgbg).to_user_def(), int(entry.get("frames", 20))])
        elif op == "screen_flash":
            rgba = [int(x) for x in entry.get("rgba", [255, 255, 255, 255])]
            push(224, indent, [RgssColor(*rgba).to_user_def(), int(entry.get("frames", 20))])
        elif op == "screen_shake":
            push(225, indent, [int(entry.get("power", 5)), int(entry.get("speed", 5)), int(entry.get("frames", 20))])
        elif op == "change_equip":
            push(319, indent, [
                int(entry.get("actor_id", 1)),
                int(entry.get("equip_type", 0)),
                int(entry.get("equip_id", 0)),
            ])
        elif op == "change_text_options":
            push(104, indent, [int(entry.get("position", 2)), 0 if entry.get("show_frame", True) else 1])
        elif op == "button_input_processing":
            push(105, indent, [int(entry.get("variable_id", 1))])
        elif op == "change_fog_tone":
            rgbg = [int(x) for x in entry.get("rgbg", [0, 0, 0, 0])]
            push(205, indent, [RgssTone(*rgbg).to_user_def(), int(entry.get("frames", 20))])
        elif op == "change_fog_opacity":
            push(206, indent, [int(entry.get("opacity", 64)), int(entry.get("frames", 20))])
        elif op == "rotate_picture":
            push(233, indent, [int(entry.get("number", 1)), int(entry.get("speed", 0))])
        elif op == "change_picture_tone":
            rgbg = [int(x) for x in entry.get("rgbg", [0, 0, 0, 0])]
            push(234, indent, [int(entry.get("number", 1)), RgssTone(*rgbg).to_user_def(), int(entry.get("frames", 20))])
        elif op == "name_input":
            push(303, indent, [int(entry.get("actor_id", 1)), int(entry.get("max_chars", 8))])
        elif op == "enemy_appear":
            push(335, indent, [int(entry.get("enemy_index", 0))])
        elif op == "enemy_transform":
            push(336, indent, [int(entry.get("enemy_index", 0)), int(entry.get("enemy_id", 1))])
        elif op == "show_battle_animation":
            push(337, indent, [
                int(entry.get("target_type", 0)),
                int(entry.get("target_index", 0)),
                int(entry.get("animation_id", 1)),
            ])
        elif op == "deal_damage":
            push(338, indent, [
                int(entry.get("target_type", 0)),
                int(entry.get("target_index", 0)),
                int(entry.get("value_type", 0)),
                int(entry.get("value", 0)),
            ])
        elif op == "force_action":
            push(339, indent, [
                int(entry.get("battler_type", 0)),
                int(entry.get("battler_index", 0)),
                int(entry.get("action_kind", 0)),
                int(entry.get("basic_or_skill", 0)),
                int(entry.get("target_index", -1)),
                int(entry.get("forcing_timing", 0)),
            ])
        elif op == "raw":
            push(int(entry["code"]), indent, ir_to_ruby(entry.get("parameters", [])))
        else:
            raise ValueError(f"Unknown semantic DSL op: {op}")

    return cmds


def _map_to_ir(map_id: int, map_name: str, map_obj: RubyObject) -> Dict[str, Any]:
    iv = map_obj.ivars
    width = int(iv["@width"])
    height = int(iv["@height"])
    table = RgssTable.from_user_def(iv["@data"])
    layers: Dict[str, List[List[int]]] = {}
    for z in range(table.zsize):
        rows: List[List[int]] = []
        for y in range(height):
            rows.append([table.get(x, y, z) for x in range(width)])
        layers[f"layer_{z}"] = rows

    events_ir: List[Dict[str, Any]] = []
    raw_events: Dict[int, RubyObject] = iv.get("@events", {})
    for ev_id in sorted(raw_events.keys()):
        ev = raw_events[ev_id]
        ev_iv = ev.ivars
        pages_ir: List[Dict[str, Any]] = []
        for page in ev_iv.get("@pages", []):
            p_iv = page.ivars
            cond = ruby_to_ir(p_iv["@condition"])
            graphic = ruby_to_ir(p_iv["@graphic"])
            trig = int(p_iv.get("@trigger", 0))
            pages_ir.append({
                "trigger": TRIGGER_NAMES.get(trig, str(trig)),
                "trigger_code": trig,
                "through": bool(p_iv.get("@through", False)),
                "always_on_top": bool(p_iv.get("@always_on_top", False)),
                "walk_anime": bool(p_iv.get("@walk_anime", True)),
                "step_anime": bool(p_iv.get("@step_anime", False)),
                "direction_fix": bool(p_iv.get("@direction_fix", False)),
                "move_type": int(p_iv.get("@move_type", 0)),
                "move_speed": int(p_iv.get("@move_speed", 3)),
                "move_frequency": int(p_iv.get("@move_frequency", 3)),
                "move_route": ruby_to_ir(p_iv["@move_route"]),
                "condition": cond,
                "graphic": graphic,
                "commands": commands_to_semantic_dsl(p_iv.get("@list", [])),
            })
        events_ir.append({
            "id": int(ev_iv["@id"]),
            "name": str(ev_iv["@name"]),
            "x": int(ev_iv["@x"]),
            "y": int(ev_iv["@y"]),
            "pages": pages_ir,
        })

    return {
        "map_id": map_id,
        "name": map_name,
        "tileset_id": int(iv["@tileset_id"]),
        "width": width,
        "height": height,
        "autoplay_bgm": bool(iv.get("@autoplay_bgm", False)),
        "bgm": ruby_to_ir(iv.get("@bgm")),
        "autoplay_bgs": bool(iv.get("@autoplay_bgs", False)),
        "bgs": ruby_to_ir(iv.get("@bgs")),
        "encounter_list": ruby_to_ir(iv.get("@encounter_list", [])),
        "encounter_step": int(iv.get("@encounter_step", 30)),
        "layers": layers,
        "events": events_ir,
    }


def _ir_to_map(map_ir: Dict[str, Any]) -> RubyObject:
    width = int(map_ir["width"])
    height = int(map_ir["height"])
    layers = map_ir["layers"]
    table = RgssTable.create(width, height, 3, dims=3)
    for z in range(3):
        rows = layers[f"layer_{z}"]
        for y in range(height):
            for x in range(width):
                table.set(x, y, z, int(rows[y][x]))

    events_map: Dict[int, RubyObject] = {}
    for ev_ir in map_ir.get("events", []):
        ev_id = int(ev_ir["id"])
        pages_rb: List[RubyObject] = []
        for p_ir in ev_ir.get("pages", []):
            trig_code = int(p_ir.get("trigger_code", TRIGGER_NAME_TO_INT.get(str(p_ir.get("trigger", "action_button")), 0)))
            pages_rb.append(RubyObject("RPG::Event::Page", {
                "@condition": ir_to_ruby(p_ir["condition"]),
                "@graphic": ir_to_ruby(p_ir["graphic"]),
                "@move_type": int(p_ir.get("move_type", 0)),
                "@move_speed": int(p_ir.get("move_speed", 3)),
                "@move_frequency": int(p_ir.get("move_frequency", 3)),
                "@move_route": ir_to_ruby(p_ir["move_route"]),
                "@walk_anime": bool(p_ir.get("walk_anime", True)),
                "@step_anime": bool(p_ir.get("step_anime", False)),
                "@direction_fix": bool(p_ir.get("direction_fix", False)),
                "@through": bool(p_ir.get("through", False)),
                "@always_on_top": bool(p_ir.get("always_on_top", False)),
                "@trigger": trig_code,
                "@list": semantic_dsl_to_commands(p_ir.get("commands", [])),
            }))
        events_map[ev_id] = RubyObject("RPG::Event", {
            "@id": ev_id,
            "@name": str(ev_ir["name"]),
            "@x": int(ev_ir["x"]),
            "@y": int(ev_ir["y"]),
            "@pages": pages_rb,
        })

    return RubyObject("RPG::Map", {
        "@tileset_id": int(map_ir["tileset_id"]),
        "@width": width,
        "@height": height,
        "@autoplay_bgm": bool(map_ir.get("autoplay_bgm", False)),
        "@bgm": ir_to_ruby(map_ir.get("bgm")),
        "@autoplay_bgs": bool(map_ir.get("autoplay_bgs", False)),
        "@bgs": ir_to_ruby(map_ir.get("bgs")),
        "@encounter_list": ir_to_ruby(map_ir.get("encounter_list", [])),
        "@encounter_step": int(map_ir.get("encounter_step", 30)),
        "@data": table.to_user_def(),
        "@events": events_map,
    })


def build_tileset_manifest(tilesets_rb: List[Optional[RubyObject]]) -> Dict[str, Any]:
    """Builds an AI-friendly 32x32 tile catalog with passability and autotile metadata."""
    manifest_entries: List[Dict[str, Any]] = []
    for ts in tilesets_rb:
        if ts is None:
            continue
        iv = ts.ivars
        ts_id = int(iv["@id"])
        passages = RgssTable.from_user_def(iv["@passages"])
        priorities = RgssTable.from_user_def(iv["@priorities"])
        terrain_tags = RgssTable.from_user_def(iv["@terrain_tags"])

        autotiles = []
        for slot, name in enumerate(iv.get("@autotile_names", [])):
            base_id = 48 + slot * 48
            p_flag = passages.get(base_id) if base_id < passages.xsize else 0
            autotiles.append({
                "slot": slot,
                "name": name,
                "base_tile_id": base_id,
                "passable": (p_flag & 0x0F) == 0,
                "passage_bits": p_flag,
                "terrain_tag": terrain_tags.get(base_id) if base_id < terrain_tags.xsize else 0,
            })

        regular_tiles: List[Dict[str, Any]] = []
        for tile_id in range(384, passages.xsize):
            rel = tile_id - 384
            col = rel % 8
            row = rel // 8
            p_flag = passages.get(tile_id)
            prio = priorities.get(tile_id) if tile_id < priorities.xsize else 0
            terr = terrain_tags.get(tile_id) if tile_id < terrain_tags.xsize else 0
            if p_flag != 0 or prio != 0 or terr != 0 or tile_id < 416:
                regular_tiles.append({
                    "tile_id": tile_id,
                    "sheet_col": col,
                    "sheet_row": row,
                    "pixel_rect": [col * 32, row * 32, 32, 32],
                    "passable": (p_flag & 0x0F) == 0,
                    "passage_bits": p_flag,
                    "priority": prio,
                    "terrain_tag": terr,
                })

        manifest_entries.append({
            "tileset_id": ts_id,
            "name": str(iv["@name"]),
            "tileset_image": str(iv["@tileset_name"]),
            "tile_size_px": 32,
            "sheet_columns": 8,
            "autotiles": autotiles,
            "tiles": regular_tiles,
        })
    return {"format": "rmxp_tileset_manifest_v1", "tilesets": manifest_entries}


def unpack_rmxp_to_ir(rmxp_dir: Path, ir_dir: Path, encoding: str = "utf-8") -> Dict[str, Any]:
    """Converts an RMXP project directory into an AI-native IR workspace."""
    rmxp_dir = Path(rmxp_dir)
    ir_dir = Path(ir_dir)
    data_dir = rmxp_dir / "Data"
    if not data_dir.is_dir():
        raise FileNotFoundError(f"Missing RMXP Data directory: {data_dir}")

    rxproj_file = next(rmxp_dir.glob("*.rxproj"), None)
    rxproj_text = rxproj_file.read_text(encoding=encoding, errors="replace").strip() if rxproj_file else "RPGXP 1.02"

    system_rb = marshal_loads((data_dir / "System.rxdata").read_bytes(), encoding=encoding)
    map_infos_rb = marshal_loads((data_dir / "MapInfos.rxdata").read_bytes(), encoding=encoding)
    tilesets_rb = marshal_loads((data_dir / "Tilesets.rxdata").read_bytes(), encoding=encoding)

    ir_dir.mkdir(parents=True, exist_ok=True)
    _write_json(ir_dir / "ir_project.json", {
        "schema_version": "1.0.0",
        "source_engine": "RMXP",
        "source_encoding": encoding,
        "rxproj_header": rxproj_text,
        "system": ruby_to_ir(system_rb),
        "map_infos": ruby_to_ir(map_infos_rb),
    })

    # Database files (full RMXP database suite)
    db_dir = ir_dir / "database"
    _write_json(db_dir / "tilesets.json", ruby_to_ir(tilesets_rb))
    standard_db_files = (
        "Actors",
        "Classes",
        "Skills",
        "Items",
        "Weapons",
        "Armors",
        "Enemies",
        "Troops",
        "States",
        "Animations",
        "CommonEvents",
    )
    for db_name in standard_db_files:
        rx_path = data_dir / f"{db_name}.rxdata"
        if not rx_path.exists():
            continue
        loaded = marshal_loads(rx_path.read_bytes(), encoding=encoding)
        if db_name == "CommonEvents":
            ce_ir: List[Any] = []
            for ce in loaded:
                if ce is None:
                    ce_ir.append(None)
                else:
                    ce_iv = ce.ivars
                    ce_ir.append({
                        "id": int(ce_iv["@id"]),
                        "name": str(ce_iv.get("@name", "")),
                        "trigger": int(ce_iv.get("@trigger", 0)),
                        "switch_id": int(ce_iv.get("@switch_id", 1)),
                        "commands": commands_to_semantic_dsl(ce_iv.get("@list", [])),
                    })
            _write_json(db_dir / "common_events.json", ce_ir)
        else:
            _write_json(db_dir / f"{db_name.lower()}.json", ruby_to_ir(loaded))

    # Tileset manifest
    _write_json(ir_dir / "assets" / "tileset_manifest.json", build_tileset_manifest(tilesets_rb))

    # Maps
    maps_dir = ir_dir / "maps"
    maps_dir.mkdir(parents=True, exist_ok=True)
    map_count = 0
    for map_file in sorted(data_dir.glob("Map[0-9][0-9][0-9].rxdata")):
        match = re.match(r"Map(\d+)\.rxdata$", map_file.name, re.IGNORECASE)
        if not match:
            continue
        map_id = int(match.group(1))
        map_obj = marshal_loads(map_file.read_bytes(), encoding=encoding)
        info_obj = map_infos_rb.get(map_id)
        map_name = str(info_obj.ivars.get("@name", f"Map{map_id:03d}")) if info_obj else f"Map{map_id:03d}"
        _write_json(maps_dir / f"map_{map_id:03d}.json", _map_to_ir(map_id, map_name, map_obj))
        map_count += 1

    # Scripts
    scripts_path = data_dir / "Scripts.rxdata"
    script_count = 0
    if scripts_path.exists():
        scripts_rb = marshal_loads(scripts_path.read_bytes(), decode_strings=False, encoding=encoding)
        scripts_dir = ir_dir / "scripts"
        scripts_dir.mkdir(parents=True, exist_ok=True)
        index_entries = []
        for idx, entry in enumerate(scripts_rb):
            sid, title_raw, compressed = entry
            title = title_raw.decode(encoding, errors="replace") if isinstance(title_raw, bytes) else str(title_raw)
            safe_title = re.sub(r"[^A-Za-z0-9_\-가-힣]+", "_", title).strip("_") or f"Script_{idx:03d}"
            filename = f"{idx:03d}_{safe_title}.rb"
            raw_code = zlib.decompress(compressed) if compressed else b""
            if encoding.lower() != "utf-8":
                raw_code = raw_code.decode(encoding, errors="replace").encode("utf-8")
            (scripts_dir / filename).write_bytes(raw_code)
            index_entries.append({"id": int(sid), "title": title, "file": filename})
            script_count += 1
        _write_json(scripts_dir / "_index.json", index_entries)

    return {
        "rmxp_dir": str(rmxp_dir),
        "ir_dir": str(ir_dir),
        "encoding": encoding,
        "maps": map_count,
        "scripts": script_count,
    }


def pack_ir_to_rmxp(ir_dir: Path, rmxp_dir: Path, encoding: str = "utf-8") -> Dict[str, Any]:
    """Compiles an AI-native IR workspace back into standard RMXP .rxdata files."""
    ir_dir = Path(ir_dir)
    rmxp_dir = Path(rmxp_dir)
    data_dir = rmxp_dir / "Data"
    data_dir.mkdir(parents=True, exist_ok=True)

    proj = _read_json(ir_dir / "ir_project.json")
    (rmxp_dir / "Game.rxproj").write_text(proj.get("rxproj_header", "RPGXP 1.02"), encoding="utf-8")

    (data_dir / "System.rxdata").write_bytes(marshal_dumps(ir_to_ruby(proj["system"]), encoding=encoding))
    (data_dir / "MapInfos.rxdata").write_bytes(marshal_dumps(ir_to_ruby(proj["map_infos"]), encoding=encoding))

    db_dir = ir_dir / "database"
    (data_dir / "Tilesets.rxdata").write_bytes(marshal_dumps(ir_to_ruby(_read_json(db_dir / "tilesets.json")), encoding=encoding))
    standard_db_files = (
        ("Actors", "actors.json"),
        ("Classes", "classes.json"),
        ("Skills", "skills.json"),
        ("Items", "items.json"),
        ("Weapons", "weapons.json"),
        ("Armors", "armors.json"),
        ("Enemies", "enemies.json"),
        ("Troops", "troops.json"),
        ("States", "states.json"),
        ("Animations", "animations.json"),
        ("CommonEvents", "common_events.json"),
    )
    for db_name, json_name in standard_db_files:
        json_path = db_dir / json_name
        if not json_path.exists():
            continue
        raw_ir = _read_json(json_path)
        if db_name == "CommonEvents":
            ce_rb: List[Any] = []
            for entry in raw_ir:
                if entry is None:
                    ce_rb.append(None)
                elif isinstance(entry, dict) and entry.get("__type__") == "RubyObject":
                    ce_rb.append(ir_to_ruby(entry))
                else:
                    raw_t = entry.get("trigger", 0)
                    t_int = {"none": 0, "autorun": 1, "parallel": 2}.get(str(raw_t).lower(), raw_t)
                    ce_rb.append(RubyObject("RPG::CommonEvent", {
                        "@id": int(entry["id"]),
                        "@name": str(entry.get("name", "")),
                        "@trigger": int(t_int),
                        "@switch_id": int(entry.get("switch_id", 1)),
                        "@list": semantic_dsl_to_commands(entry.get("commands", [])),
                    }))
            (data_dir / "CommonEvents.rxdata").write_bytes(marshal_dumps(ce_rb, encoding=encoding))
        else:
            (data_dir / f"{db_name}.rxdata").write_bytes(marshal_dumps(ir_to_ruby(raw_ir), encoding=encoding))

    map_count = 0
    for map_json in sorted((ir_dir / "maps").glob("map_*.json")):
        map_ir = _read_json(map_json)
        map_id = int(map_ir["map_id"])
        map_rb = _ir_to_map(map_ir)
        (data_dir / f"Map{map_id:03d}.rxdata").write_bytes(marshal_dumps(map_rb, encoding=encoding))
        map_count += 1

    scripts_index = ir_dir / "scripts" / "_index.json"
    script_count = 0
    if scripts_index.exists():
        entries = _read_json(scripts_index)
        packed_scripts = []
        for item in entries:
            code_bytes = (ir_dir / "scripts" / item["file"]).read_bytes()
            if encoding.lower() != "utf-8":
                code_bytes = code_bytes.decode("utf-8", errors="replace").encode(encoding, errors="replace")
            packed_scripts.append([int(item["id"]), str(item["title"]), zlib.compress(code_bytes)])
            script_count += 1
        (data_dir / "Scripts.rxdata").write_bytes(marshal_dumps(packed_scripts, encoding=encoding))

    return {
        "ir_dir": str(ir_dir),
        "rmxp_dir": str(rmxp_dir),
        "encoding": encoding,
        "maps": map_count,
        "scripts": script_count,
    }


def inspect_scripts(ir_dir: Path) -> List[Dict[str, Any]]:
    """Returns structured metadata (id, title, file, line_count) for packed RGSS scripts."""
    ir_dir = Path(ir_dir)
    idx_path = ir_dir / "scripts" / "_index.json"
    if not idx_path.is_file():
        return []
    entries = _read_json(idx_path)
    out: List[Dict[str, Any]] = []
    for item in entries:
        fpath = ir_dir / "scripts" / item.get("file", "")
        line_cnt = len(fpath.read_text(encoding="utf-8", errors="replace").splitlines()) if fpath.is_file() else 0
        out.append({
            "id": int(item.get("id", 0)),
            "title": str(item.get("title", "")),
            "file": str(item.get("file", "")),
            "line_count": line_cnt,
        })
    return out
