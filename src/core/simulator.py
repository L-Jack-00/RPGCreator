# -*- coding: utf-8 -*-
"""
Deterministic Headless RPG Runtime Engine.
Executes movement, directional passability, conditional event pages,
semantic event DSL commands, choices, map warps, and turn-based battles
in memory without requiring a display server.
"""
from __future__ import annotations

import ast
from dataclasses import dataclass, field
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import traceback
from typing import Any, Dict, List, Optional, Tuple

from .battle_engine import (
    RMXP_ELEMENT_MULTS as _RMXP_ELEMENT_MULTS,
    apply_state_sets as _be_apply_state_sets,
    apply_variance as _be_apply_variance,
    build_battle_enemies as _be_build_battle_enemies,
    build_single_enemy as _be_build_single_enemy,
    compute_element_multiplier as _be_element_multiplier,
    compute_guard_element_multiplier as _be_guard_element_multiplier,
    enemy_formula_stats as _be_enemy_formula_stats,
    evaluate_attack_roll as _be_evaluate_attack_roll,
    execute_enemy_turn_action as _be_execute_enemy_turn_action,
    get_combatant_restriction as _be_get_restriction,
    select_enemy_action as _be_select_enemy_action,
    shock_release_states as _be_shock_release_states,
    tick_states_on_combatant as _be_tick_states_on_combatant,
)
from .battle_formulas import BattleFormula, combatant_stats as _bf_combatant_stats
from .utils import load_json as _load_json, save_json as _save_json
from .validator import (
    _apply_variable_op,
    build_tileset_tables,
    build_tileset_terrain_tables,
    can_move_step,
    eval_variable_cmp,
    select_active_page,
)

DIR_VECTORS = {
    "up": (0, -1),
    "down": (0, 1),
    "left": (-1, 0),
    "right": (1, 0),
}

DIAG_VECTORS: Dict[str, Tuple[int, int, str, str]] = {
    "up_left": (-1, -1, "left", "up"),
    "up_right": (1, -1, "right", "up"),
    "down_left": (-1, 1, "left", "down"),
    "down_right": (1, 1, "right", "down"),
}


MAX_COMMON_EVENT_DEPTH = 16
# RMXP plays animations at 20 frames/sec; the player runs at 60 ticks/sec.
ANIMATION_TICKS_PER_FRAME = 3


def format_play_time(play_time_ms: float) -> str:
    """Formats accumulated session milliseconds into MM:SS or H:MM:SS text."""
    total_sec = max(0, int(play_time_ms // 1000))
    hours, rem = divmod(total_sec, 3600)
    mins, secs = divmod(rem, 60)
    if hours > 0:
        return f"{hours}:{mins:02d}:{secs:02d}"
    return f"{mins:02d}:{secs:02d}"


def _exp_for_level(level: int, basis: int = 30, inflation: int = 30) -> int:
    """Computes cumulative EXP threshold for the target level using the RMXP growth curve."""
    if level <= 1:
        return 0
    pow_val = 2.4 + inflation / 100.0
    total = 0
    denom = 5.0 ** pow_val
    for lv in range(2, level + 1):
        total += max(1, round(basis * (((lv + 3) ** pow_val) / denom)))
    return total


_SAVE_SCHEMA_VERSION = 2
_ESCAPE_TOKEN_RE = re.compile(r"\\([Cc])\[(\d+)\]|\\([.|])")
_FACE_TAG_RE = re.compile(r"(?:\[FACE:([^:\]]+)(?::(\d+))?\]|\\F\[([^:\]]+)(?::(\d+))?\])\s*", re.IGNORECASE)


def parse_message_segments(text: str) -> Tuple[List[Tuple[str, int]], int]:
    """Parses RMXP message escape codes (\\C[n], \\., \\|) into ([(segment, color_index)], extra_wait_frames)."""
    segments: List[Tuple[str, int]] = []
    cur_color = 0
    extra_wait = 0
    pos = 0
    for m in _ESCAPE_TOKEN_RE.finditer(text):
        if m.start() > pos:
            segments.append((text[pos:m.start()], cur_color))
        if m.group(1):
            cur_color = max(0, min(7, int(m.group(2))))
        elif m.group(3) == ".":
            extra_wait += 15
        elif m.group(3) == "|":
            extra_wait += 60
        pos = m.end()
    if pos < len(text) or not segments:
        segments.append((text[pos:], cur_color))
    return [(seg, c) for seg, c in segments if seg] or [("", cur_color)], extra_wait


def _extract_face_tag(lines: List[str]) -> Tuple[List[str], str, int]:
    """Extracts optional [FACE:Name:idx] or \\F[Name:idx] directive from dialogue lines."""
    if not lines:
        return lines, "", 0
    m = _FACE_TAG_RE.match(lines[0])
    if not m:
        return lines, "", 0
    fname = str(m.group(1) or m.group(3) or "").strip()
    fidx = int(m.group(2) or m.group(4) or 0)
    cleaned_first = lines[0][m.end():]
    out_lines = [cleaned_first] + lines[1:]
    if not cleaned_first and len(out_lines) > 1:
        out_lines = out_lines[1:]
    return out_lines, fname, fidx


def _compute_save_checksum(payload: Dict[str, Any]) -> str:
    """Computes deterministic SHA-256 checksum over canonical save state JSON excluding checksum."""
    clean = {k: v for k, v in payload.items() if k != "checksum"}
    blob = json.dumps(clean, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def migrate_save_state(state: Dict[str, Any]) -> Dict[str, Any]:
    """Migrates legacy v1 save dictionaries to the current save schema cleanly."""
    out = dict(state)
    ver = int(out.get("save_version", 1) or 1)
    if ver < 2:
        out.setdefault("pictures", {})
        out.setdefault("fog_state", {"name": "", "opacity": 64, "tone": [0, 0, 0, 0], "sx": 0, "sy": 0, "scroll_x": 0.0, "scroll_y": 0.0})
        out.setdefault("text_options", {"position": 2, "show_frame": True})
        out.setdefault("memorized_bgm", None)
        out.setdefault("memorized_bgs", None)
        out["save_version"] = _SAVE_SCHEMA_VERSION
    return out


def resolve_user_save_dir(app_name: str = "rpgcreator_cli", fallback_dir: Optional[Path] = None) -> Path:
    """Resolves OS user save directory (Windows AppData/Local or POSIX XDG_DATA_HOME) with env override."""
    env_override = os.environ.get("RPGCREATOR_SAVE_DIR", "").strip()
    if env_override:
        base = Path(env_override)
    elif os.name == "nt":
        local_app = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        base = (Path(local_app) / app_name / "saves") if local_app else (fallback_dir or Path.cwd())
    else:
        xdg = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
        base = Path(xdg) / app_name / "saves"
    return base


def write_crash_report(
    exc: BaseException,
    crash_dir: Path,
    context: Optional[Dict[str, Any]] = None,
) -> Path:
    """Writes a timestamped traceback report and sanitized runtime snapshot to crash_dir."""
    target_dir = Path(crash_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    out_path = target_dir / f"crash_{stamp}.log"
    tb_text = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    ctx_json = json.dumps(context or {}, ensure_ascii=False, indent=2)
    out_path.write_text(
        f"=== Crash Report ({stamp}) ===\n{tb_text}\n=== Context ===\n{ctx_json}\n",
        encoding="utf-8",
    )
    return out_path


_AST_BIN_OPS: Dict[Any, Any] = {
    ast.Add: lambda a, b: a + b,
    ast.Sub: lambda a, b: a - b,
    ast.Mult: lambda a, b: a * b,
    ast.FloorDiv: lambda a, b: a // b if b != 0 else 0,
    ast.Div: lambda a, b: int(a // b) if b != 0 else 0,
    ast.Mod: lambda a, b: a % b if b != 0 else 0,
}


def _safe_eval_arithmetic(expr_str: str, variables: Dict[int, int]) -> int:
    """Safely evaluates integer arithmetic AST supporting $game_variables[n] without exec/eval."""
    normalized = re.sub(r"\$game_variables\[(\d+)\]", r"_GV[\1]", expr_str.strip())
    normalized = normalized.replace("true", "True").replace("false", "False")
    tree = ast.parse(normalized, mode="eval")

    def _eval(node: ast.AST) -> int:
        if isinstance(node, ast.Expression):
            return _eval(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, bool)):
            return int(node.value)
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            v = _eval(node.operand)
            return v if isinstance(node.op, ast.UAdd) else -v
        if isinstance(node, ast.BinOp) and type(node.op) in _AST_BIN_OPS:
            return int(_AST_BIN_OPS[type(node.op)](_eval(node.left), _eval(node.right)))
        if (
            isinstance(node, ast.Subscript)
            and isinstance(node.value, ast.Name)
            and node.value.id == "_GV"
        ):
            idx_node = node.slice
            vid = _eval(idx_node)
            return int(variables.get(vid, 0))
        raise ValueError(f"Unsupported AST expression node: {type(node).__name__}")

    return int(_eval(tree))


@dataclass
class PendingDialogue:
    """Active dialogue, choice prompt, or shop awaiting player confirmation."""
    lines: List[str]
    choices: List[str] = field(default_factory=list)
    cancel_type: int = 0
    commands: List[Dict[str, Any]] = field(default_factory=list)
    resume_index: int = 0
    map_id: int = 1
    event_id: int = 0
    stop_after_choice_branch: Optional[int] = None
    call_stack: List[Tuple[List[Dict[str, Any]], int, Optional[int]]] = field(default_factory=list)
    shop_goods: List[Dict[str, Any]] = field(default_factory=list)
    shop_mode: str = "buy"
    shop_sell_items: List[Dict[str, Any]] = field(default_factory=list)
    shop_qty: int = 1
    battle_state: Optional[Dict[str, Any]] = None
    number_input: Optional[Dict[str, Any]] = None
    input_name: Optional[Dict[str, Any]] = None
    button_input: Optional[Dict[str, Any]] = None
    save_menu: bool = False
    page_offset: int = 0
    face_name: str = ""
    face_index: int = 0
    text_position: int = 2
    show_frame: bool = True


class HeadlessGameSession:
    """Self-contained playable RPG runtime state decoupled from GUI rendering."""

    def __init__(
        self,
        ir_dir: Path,
        enable_random_encounters: bool = False,
        interactive_battle: bool = False,
        lang: Optional[str] = None,
        save_dir: Optional[Path] = None,
        use_os_save_dir: bool = False,
        wait_pause_enabled: bool = False,
        strict_checksum: bool = False,
    ) -> None:
        self.ir_dir = Path(ir_dir)
        self.enable_random_encounters = bool(enable_random_encounters)
        self.interactive_battle = bool(interactive_battle)
        self.locale: str = str(lang) if lang else "ko"
        self.save_dir_override: Optional[Path] = Path(save_dir) if save_dir is not None else None
        self.use_os_save_dir: bool = bool(use_os_save_dir)
        self.wait_pause_enabled: bool = bool(wait_pause_enabled)
        self.strict_checksum: bool = bool(strict_checksum)
        self.reload()
        if lang:
            self.set_locale(str(lang))

    def set_locale(self, locale: str) -> int:
        """Applies translations from locale/messages.json onto live runtime session tables."""
        self.locale = str(locale)
        src = self.ir_dir / "locale" / "messages.json"
        if not src.exists():
            return 0
        cat = _load_json(src)
        strings = cat.get("strings", {}) if isinstance(cat, dict) else {}
        if not isinstance(strings, dict):
            return 0
        applied = 0

        def _tr(key: str) -> Optional[str]:
            ent = strings.get(key)
            if isinstance(ent, dict) and self.locale in ent and ent[self.locale] != "":
                return str(ent[self.locale])
            return None

        t_val = _tr("system.game_title")
        if t_val is not None:
            self.title = t_val
            applied += 1

        table_attrs = (
            ("actors", self.actors),
            ("classes", self.classes_db),
            ("skills", self.skills_db),
            ("items", self.items),
            ("weapons", self.weapons),
            ("armors", self.armors),
            ("enemies", self.enemies),
            ("states", self.states_db),
        )
        for tbl_name, table in table_attrs:
            for eid, row in table.items():
                f = row.get("fields", {})
                nv = _tr(f"db.{tbl_name}.{eid}.name")
                if nv is not None:
                    f["name"] = nv
                    applied += 1
                dv = _tr(f"db.{tbl_name}.{eid}.description")
                if dv is not None:
                    f["description"] = dv
                    applied += 1

        def _patch_cmds(prefix: str, cmds: List[Dict[str, Any]]) -> None:
            nonlocal applied
            for idx, cmd in enumerate(cmds):
                op = str(cmd.get("op", ""))
                if op == "say":
                    val = _tr(f"{prefix}.say.{idx}")
                    if val is not None:
                        cmd["lines"] = val.split("\n")
                        applied += 1
                elif op == "show_choices":
                    chs = list(cmd.get("choices", []))
                    for c_idx in range(len(chs)):
                        val = _tr(f"{prefix}.choice.{idx}.{c_idx}")
                        if val is not None:
                            chs[c_idx] = val
                            applied += 1
                    cmd["choices"] = chs

        for cid, ce in self.common_events.items():
            _patch_cmds(f"ce.{cid}", ce.get("commands", []))
        for mid, m in self.maps.items():
            mv = _tr(f"map.{mid}.name")
            if mv is not None:
                m["name"] = mv
                applied += 1
            for ev in m.get("events", []):
                eid = int(ev.get("id", 0))
                for p_idx, pg in enumerate(ev.get("pages", [])):
                    _patch_cmds(f"map.{mid}.ev.{eid}.p{p_idx}", pg.get("commands", []))
        return applied

    def reload(self) -> None:
        """Loads or hot-reloads the IR workspace into runtime state."""
        proj = _load_json(self.ir_dir / "ir_project.json")
        sys_fields = proj["system"]["fields"]
        self._battle_formula_warnings: List[str] = []
        self.battle_formula: BattleFormula = BattleFormula.safe_from_project(proj, self._battle_formula_warnings)
        self.title: str = str(proj.get("rxproj_header", "rpgcreator_cli"))
        self.switches_names: List[Optional[str]] = list(sys_fields.get("switches", []))
        self.map_id: int = int(sys_fields.get("start_map_id", 1))
        self.x: int = int(sys_fields.get("start_x", 0))
        self.y: int = int(sys_fields.get("start_y", 0))
        self.direction: str = "down"

        tilesets_ir = _load_json(self.ir_dir / "database" / "tilesets.json")
        self.tilesets_db: Dict[int, Dict[str, Any]] = {
            int(t["fields"]["id"]): t["fields"]
            for t in tilesets_ir
            if isinstance(t, dict) and isinstance(t.get("fields"), dict) and "id" in t["fields"]
        }
        self.passages_by_ts, self.priorities_by_ts = build_tileset_tables(tilesets_ir)
        self.terrain_tags_by_ts = build_tileset_terrain_tables(tilesets_ir)

        self.maps: Dict[int, Dict[str, Any]] = {}
        for mf in sorted((self.ir_dir / "maps").glob("map_*.json")):
            m = _load_json(mf)
            self.maps[int(m["map_id"])] = m
        self.last_map_banner: str = str(self.maps.get(self.map_id, {}).get("name", ""))

        self.actors = self._load_db("actors.json")
        self.classes_db = self._load_db("classes.json")
        self.skills_db = self._load_db("skills.json")
        self.items = self._load_db("items.json")
        self.weapons = self._load_db("weapons.json")
        self.armors = self._load_db("armors.json")
        self.enemies = self._load_db("enemies.json")
        self.troops = self._load_db("troops.json")
        self.states_db = self._load_db("states.json")
        self.animations_db = self._load_db("animations.json")
        ce_path = self.ir_dir / "database" / "common_events.json"
        self.common_events: Dict[int, Dict[str, Any]] = {}
        if ce_path.exists():
            for entry in _load_json(ce_path):
                if isinstance(entry, dict) and "id" in entry:
                    self.common_events[int(entry["id"])] = entry

        self.switches: Dict[int, bool] = {}
        self.variables: Dict[int, int] = {}
        self.self_switches: Dict[Tuple[int, int, str], bool] = {}
        self.gold: int = 0
        self.inventory: Dict[int, int] = {}
        self.weapons_owned: Dict[int, int] = {}
        self.armors_owned: Dict[int, int] = {}

        # Initialize hero & party combat stats (Table dims: 6 x 100)
        raw_party = sys_fields.get("party_members", [1])
        self.party_member_ids: List[int] = [int(aid) for aid in raw_party if int(aid) > 0] or [1]
        self.actor_states: Dict[int, Dict[str, Any]] = {
            aid: self._build_actor_state(aid)
            for aid in sorted(set(self.actors.keys()) | set(self.party_member_ids))
            if aid > 0
        }
        self._recompute_combat_stats()
        self.exp: int = 0
        self.game_over: bool = False
        self.last_battle_result: str = "none"

        self.pending: Optional[PendingDialogue] = None
        self.log: List[str] = list(self._battle_formula_warnings)
        self.ticks: int = 0
        self.encounter_steps: int = 0
        self.screen_tone: List[int] = [0, 0, 0, 0]
        self.pending_flash: Optional[Dict[str, Any]] = None
        self.pending_shake: Optional[Dict[str, Any]] = None
        self.pending_transition: Optional[Dict[str, Any]] = None
        self.pending_animations: List[Dict[str, Any]] = []
        self.pending_balloons: List[Dict[str, Any]] = []
        self.current_bgm: Optional[Dict[str, Any]] = None
        self.current_bgs: Optional[Dict[str, Any]] = None
        self.sys_fields: Dict[str, Any] = dict(sys_fields)
        self.title_bgm: Optional[Dict[str, Any]] = self._extract_audio_spec(sys_fields.get("title_bgm"))
        self.gameover_me: Optional[Dict[str, Any]] = self._extract_audio_spec(sys_fields.get("gameover_me"))
        self.battle_bgm: Optional[Dict[str, Any]] = self._extract_audio_spec(sys_fields.get("battle_bgm"))
        self.battle_end_me: Optional[Dict[str, Any]] = self._extract_audio_spec(sys_fields.get("battle_end_me"))
        self._pre_battle_bgm: Optional[Dict[str, Any]] = None
        self.played_se_queue: List[Dict[str, Any]] = []
        self.played_me_queue: List[Dict[str, Any]] = []
        self.audio_muted: bool = False
        self.memorized_bgm: Optional[Dict[str, Any]] = None
        self.memorized_bgs: Optional[Dict[str, Any]] = None
        self.pending_bgm_fade: Optional[Dict[str, Any]] = None
        self.pending_bgs_fade: Optional[Dict[str, Any]] = None
        self.text_options: Dict[str, Any] = {"position": 2, "show_frame": True}
        self.fog_state: Dict[str, Any] = {
            "name": "",
            "opacity": 64,
            "tone": [0, 0, 0, 0],
            "sx": 0,
            "sy": 0,
            "scroll_x": 0.0,
            "scroll_y": 0.0,
        }
        self.wait_frames_left: int = 0
        self.pending_wait: Optional[Dict[str, Any]] = None
        self.pending_menu_open: bool = False
        self.pending_battle_animations: List[Dict[str, Any]] = []
        self.save_checksum_valid: bool = True
        self.locale: str = ""
        self.last_slide_was_ice: bool = False
        self.screen_faded_out: bool = False
        self.weather: Dict[str, Any] = {"type": "none", "power": 0}
        self.timer_seconds: int = 0
        self.timer_active: bool = False
        self.timer_accum_ms: float = 0.0
        self.save_enabled: bool = True
        self.menu_enabled: bool = True
        self.encounter_enabled: bool = True
        self.player_transparent: bool = False
        self.player_through: bool = False
        self.player_opacity: int = 255
        self.player_blend_type: int = 0
        self.player_jump_frames: int = 0
        self.player_jump_total: int = 0
        self.npc_jump_state: Dict[Tuple[int, int], Dict[str, int]] = {}
        self.windowskin_name: str = str(sys_fields.get("windowskin_name", "") or "")
        self.erased_events: set[Tuple[int, int]] = set()
        self.camera_scroll_px: List[int] = [0, 0]
        self.camera_scroll_target_px: List[int] = [0, 0]
        self.camera_scroll_pos_px: List[float] = [0.0, 0.0]
        self.camera_scroll_speed: int = 4
        self.defeated_enemies: Dict[int, int] = {}
        self.play_time_ms: float = 0.0
        self.pictures: Dict[int, Dict[str, Any]] = {}
        self.quests: Dict[str, Dict[str, Any]] = {}
        self.vehicle: str = "walk"
        self.vehicle_positions: Dict[str, Dict[str, int]] = {}
        for vtype, mid_key, x_key, y_key in (
            ("boat", "boat_start_map_id", "boat_start_x", "boat_start_y"),
            ("ship", "ship_start_map_id", "ship_start_x", "ship_start_y"),
            ("airship", "airship_start_map_id", "airship_start_x", "airship_start_y"),
        ):
            vmid = int(sys_fields.get(mid_key, 0) or 0)
            if vmid > 0:
                self.vehicle_positions[vtype] = {
                    "map_id": vmid,
                    "x": int(sys_fields.get(x_key, 0) or 0),
                    "y": int(sys_fields.get(y_key, 0) or 0),
                }
        self._active_move_routes: Dict[Tuple[int, int], Dict[str, Any]] = {}
        self._npc_step_seq: int = 0
        self._map_revisions: Dict[int, int] = {}
        self._sync_map_autoplay_audio()
        self._process_auto_events()

    def toggle_audio_mute(self) -> bool:
        """Toggles audio mute state for BGM, BGS, SE, and ME and returns the new muted flag."""
        self.audio_muted = not self.audio_muted
        self.log.append(f"[Audio] 음소거 {'ON' if self.audio_muted else 'OFF'}")
        return self.audio_muted

    @staticmethod
    def _extract_audio_spec(raw: Any) -> Optional[Dict[str, Any]]:
        """Normalizes a Ruby-JSON AudioFile dict into {name, volume, pitch} or None when empty."""
        if not isinstance(raw, dict):
            return None
        fields = raw.get("fields", raw)
        if not isinstance(fields, dict):
            return None
        name = str(fields.get("name", "") or "")
        if not name:
            return None
        return {
            "name": name,
            "volume": int(fields.get("volume", 80)),
            "pitch": int(fields.get("pitch", 100)),
        }

    def _sync_map_autoplay_audio(self) -> None:
        m = self.maps.get(self.map_id, {})
        if m.get("autoplay_bgm"):
            spec = self._extract_audio_spec(m.get("bgm"))
            if spec is not None:
                self.current_bgm = spec
        if m.get("autoplay_bgs"):
            spec = self._extract_audio_spec(m.get("bgs"))
            if spec is not None:
                self.current_bgs = spec
        ts = self.tilesets_db.get(int(m.get("tileset_id", 1)), {})
        fog_name = str(ts.get("fog_name", "") or "")
        self.fog_state["name"] = fog_name
        if fog_name:
            self.fog_state["opacity"] = int(ts.get("fog_opacity", 64))
            self.fog_state["sx"] = int(ts.get("fog_sx", 0))
            self.fog_state["sy"] = int(ts.get("fog_sy", 0))

    def _build_actor_state(self, aid: int) -> Dict[str, Any]:
        """Builds an isolated runtime state dictionary for a single actor ID."""
        actor = self.actors.get(aid, {}).get("fields", {})
        params_tbl = actor.get("parameters", {})
        params = params_tbl.get("data", [])
        p_xsize = int(params_tbl.get("xsize", 6)) or 6
        lvl = int(actor.get("initial_level", 1))
        exp_basis = int(actor.get("exp_basis", 30)) or 30
        exp_inflation = int(actor.get("exp_inflation", 30))
        hp_idx = 0 + lvl * p_xsize
        sp_idx = 1 + lvl * p_xsize
        str_idx = 2 + lvl * p_xsize
        dex_idx = 3 + lvl * p_xsize
        agi_idx = 4 + lvl * p_xsize
        int_idx = 5 + lvl * p_xsize
        max_hp = int(params[hp_idx]) if len(params) > hp_idx and params[hp_idx] > 0 else 150
        max_sp = int(params[sp_idx]) if len(params) > sp_idx and params[sp_idx] > 0 else 40
        default_str = 14 if aid == self.lead_actor_id else 10
        base_str = int(params[str_idx]) if len(params) > str_idx and params[str_idx] > 0 else default_str
        base_dex = int(params[dex_idx]) if len(params) > dex_idx and params[dex_idx] > 0 else 10
        base_agi = int(params[agi_idx]) if len(params) > agi_idx and params[agi_idx] > 0 else 10
        base_int = int(params[int_idx]) if len(params) > int_idx and params[int_idx] > 0 else 10
        st = {
            "id": aid,
            "name": str(actor.get("name", f"Hero {aid}" if aid > 1 else "Hero")),
            "character_name": str(actor.get("character_name", "")),
            "class_id": int(actor.get("class_id", 1)),
            "level": lvl,
            "exp": _exp_for_level(lvl, exp_basis, exp_inflation),
            "exp_basis": exp_basis,
            "exp_inflation": exp_inflation,
            "max_hp": max_hp,
            "hp": max_hp,
            "max_sp": max_sp,
            "sp": max_sp,
            "base_str": base_str,
            "base_dex": base_dex,
            "base_agi": base_agi,
            "base_int": base_int,
            "base_pdef": 8,
            "class_mhp_bonus": 0,
            "class_msp_bonus": 0,
            "weapon_id": int(actor.get("weapon_id", 0)),
            "armor_ids": {
                1: int(actor.get("armor1_id", 0)),
                2: int(actor.get("armor2_id", 0)),
                3: int(actor.get("armor3_id", 0)),
                4: int(actor.get("armor4_id", 0)),
                5: int(actor.get("armor5_id", 0)),
            },
            "learned_skills": [],
            "states": [],
        }
        self._apply_class_vital_bonuses(st)
        self._sync_actor_learnings(st)
        return st

    def _class_stat_bonuses(self, class_id: int) -> Dict[str, int]:
        """Returns optional per-class stat modifiers defined in classes_db[class_id].fields.class_params."""
        cls_fields = self.classes_db.get(int(class_id), {}).get("fields", {})
        cp = cls_fields.get("class_params")
        if not isinstance(cp, dict):
            return {k: 0 for k in ("mhp", "msp", "atk", "str", "dex", "agi", "int", "pdef", "mdef", "eva")}
        return {
            k: int(cp.get(k, 0) or 0)
            for k in ("mhp", "msp", "atk", "str", "dex", "agi", "int", "pdef", "mdef", "eva")
        }

    def _apply_class_vital_bonuses(self, st: Dict[str, Any]) -> None:
        """Adjusts max_hp/max_sp reversibly according to the actor's current class_params mhp/msp."""
        cb = self._class_stat_bonuses(int(st.get("class_id", 1)))
        old_hb = int(st.get("class_mhp_bonus", 0))
        old_sb = int(st.get("class_msp_bonus", 0))
        new_hb, new_sb = cb["mhp"], cb["msp"]
        if new_hb != old_hb:
            st["max_hp"] = max(1, int(st["max_hp"]) + (new_hb - old_hb))
            st["hp"] = min(int(st["hp"]) + max(0, new_hb - old_hb), int(st["max_hp"]))
            st["class_mhp_bonus"] = new_hb
        if new_sb != old_sb:
            st["max_sp"] = max(0, int(st["max_sp"]) + (new_sb - old_sb))
            st["sp"] = min(int(st["sp"]) + max(0, new_sb - old_sb), int(st["max_sp"]))
            st["class_msp_bonus"] = new_sb

    def _sync_actor_learnings(self, st: Dict[str, Any]) -> List[int]:
        """Unlocks any class skills up to the actor's current level and returns newly added skill IDs."""
        added: List[int] = []
        lvl = int(st["level"])
        class_id = int(st.get("class_id", 1))
        skills = st["learned_skills"]
        for lrn in self.classes_db.get(class_id, {}).get("fields", {}).get("learnings", []):
            lf = lrn.get("fields", {}) if isinstance(lrn, dict) else {}
            if int(lf.get("level", 999)) <= lvl and int(lf.get("skill_id", 0)) > 0:
                sid = int(lf["skill_id"])
                if sid not in skills:
                    skills.append(sid)
                    added.append(sid)
        return added

    def _grant_actor_exp(self, aid: int, amount: int) -> None:
        if amount <= 0:
            return
        st = self._get_actor_state(aid)
        st["exp"] = int(st["exp"]) + amount
        actor = self.actors.get(aid, {}).get("fields", {})
        params_tbl = actor.get("parameters", {})
        params = params_tbl.get("data", [])
        p_xsize = int(params_tbl.get("xsize", 6)) or 6
        final_lv = int(actor.get("final_level", 99)) or 99
        leveled = False
        while int(st["level"]) < final_lv:
            next_lv = int(st["level"]) + 1
            needed = _exp_for_level(next_lv, int(st["exp_basis"]), int(st["exp_inflation"]))
            if int(st["exp"]) < needed:
                break
            old_lv = int(st["level"])
            st["level"] = next_lv
            old_mhp, old_msp = int(st["max_hp"]), int(st["max_sp"])
            hp_idx = 0 + next_lv * p_xsize
            sp_idx = 1 + next_lv * p_xsize
            str_idx = 2 + next_lv * p_xsize
            new_mhp = int(params[hp_idx]) if len(params) > hp_idx and params[hp_idx] > 0 else old_mhp + 18
            new_msp = int(params[sp_idx]) if len(params) > sp_idx and params[sp_idx] > 0 else old_msp + 6
            new_str = int(params[str_idx]) if len(params) > str_idx and params[str_idx] > 0 else int(st["base_str"]) + 2
            st["max_hp"] = max(old_mhp, new_mhp)
            st["max_sp"] = max(old_msp, new_msp)
            st["base_str"] = max(int(st["base_str"]), new_str)
            for idx_off, skey in ((3, "base_dex"), (4, "base_agi"), (5, "base_int")):
                p_idx = idx_off + next_lv * p_xsize
                cur_val = int(st.get(skey, 10))
                tbl_val = int(params[p_idx]) if len(params) > p_idx and params[p_idx] > 0 else cur_val + 1
                st[skey] = max(cur_val, tbl_val)
            st["hp"] = min(st["max_hp"], int(st["hp"]) + max(0, st["max_hp"] - old_mhp))
            st["sp"] = min(st["max_sp"], int(st["sp"]) + max(0, st["max_sp"] - old_msp))
            new_skills = self._sync_actor_learnings(st)
            skill_note = f" (스킬 {len(new_skills)}개 습득)" if new_skills else ""
            self.log.append(f"[LevelUp] {st['name']} Lv.{old_lv} -> Lv.{next_lv}{skill_note}")
            leveled = True
        if leveled:
            self._recompute_combat_stats()

    def _get_actor_state(self, aid: int) -> Dict[str, Any]:
        if aid not in self.actor_states:
            self.actor_states[aid] = self._build_actor_state(aid)
        return self.actor_states[aid]

    @property
    def lead_actor_id(self) -> int:
        return self.party_member_ids[0] if self.party_member_ids else 1

    @property
    def hero_name(self) -> str:
        return str(self._get_actor_state(self.lead_actor_id)["name"])

    @property
    def hero_graphic(self) -> str:
        return str(self._get_actor_state(self.lead_actor_id)["character_name"])

    def swap_party_order(self, idx_a: int, idx_b: int) -> bool:
        """Swaps two party positions and updates lead character presentation."""
        n = len(self.party_member_ids)
        ia, ib = int(idx_a), int(idx_b)
        if ia < 0 or ib < 0 or ia >= n or ib >= n or ia == ib:
            return False
        self.party_member_ids[ia], self.party_member_ids[ib] = self.party_member_ids[ib], self.party_member_ids[ia]
        self.log.append(f"[Party] 대열 교대: 선두 {self.hero_name}")
        return True

    @property
    def hp(self) -> int:
        return int(self._get_actor_state(self.lead_actor_id)["hp"])

    @hp.setter
    def hp(self, value: int) -> None:
        self._get_actor_state(self.lead_actor_id)["hp"] = int(value)

    @property
    def max_hp(self) -> int:
        return int(self._get_actor_state(self.lead_actor_id)["max_hp"])

    @max_hp.setter
    def max_hp(self, value: int) -> None:
        self._get_actor_state(self.lead_actor_id)["max_hp"] = int(value)

    @property
    def sp(self) -> int:
        return int(self._get_actor_state(self.lead_actor_id)["sp"])

    @sp.setter
    def sp(self, value: int) -> None:
        self._get_actor_state(self.lead_actor_id)["sp"] = int(value)

    @property
    def max_sp(self) -> int:
        return int(self._get_actor_state(self.lead_actor_id)["max_sp"])

    @max_sp.setter
    def max_sp(self, value: int) -> None:
        self._get_actor_state(self.lead_actor_id)["max_sp"] = int(value)

    @property
    def base_str(self) -> int:
        return int(self._get_actor_state(self.lead_actor_id)["base_str"])

    @base_str.setter
    def base_str(self, value: int) -> None:
        self._get_actor_state(self.lead_actor_id)["base_str"] = int(value)

    @property
    def base_pdef(self) -> int:
        return int(self._get_actor_state(self.lead_actor_id)["base_pdef"])

    @base_pdef.setter
    def base_pdef(self, value: int) -> None:
        self._get_actor_state(self.lead_actor_id)["base_pdef"] = int(value)

    @property
    def weapon_id(self) -> int:
        return int(self._get_actor_state(self.lead_actor_id)["weapon_id"])

    @weapon_id.setter
    def weapon_id(self, value: int) -> None:
        self._get_actor_state(self.lead_actor_id)["weapon_id"] = int(value)

    @property
    def armor_ids(self) -> Dict[int, int]:
        return self._get_actor_state(self.lead_actor_id)["armor_ids"]

    @armor_ids.setter
    def armor_ids(self, value: Dict[int, int]) -> None:
        self._get_actor_state(self.lead_actor_id)["armor_ids"] = dict(value)

    @property
    def learned_skills(self) -> List[int]:
        return self._get_actor_state(self.lead_actor_id)["learned_skills"]

    @learned_skills.setter
    def learned_skills(self, value: List[int]) -> None:
        self._get_actor_state(self.lead_actor_id)["learned_skills"] = list(value)

    def actor_combat_stats(self, actor_id: Optional[int] = None) -> Tuple[int, int]:
        """Returns individual (atk, pdef) stats for an actor based on their current class_params and gear."""
        aid = self.lead_actor_id if actor_id is None else int(actor_id)
        return self._actor_attack_power(aid), self._actor_defense(aid)

    def _recompute_combat_stats(self) -> None:
        lead_atk, lead_def = self.actor_combat_stats(self.lead_actor_id)
        ally_atk = 0
        ally_def = 0
        for ally_id in self.party_member_ids[1:]:
            a_atk, a_def = self.actor_combat_stats(ally_id)
            ally_atk += a_atk // 2
            ally_def += max(0, a_def - int(self._get_actor_state(ally_id)["base_pdef"])) // 2
        self.atk = lead_atk + ally_atk
        self.pdef = lead_def + ally_def

    def _load_db(self, filename: str) -> Dict[int, Dict[str, Any]]:
        path = self.ir_dir / "database" / filename
        if not path.exists():
            return {}
        raw = _load_json(path)
        return {
            int(item["fields"]["id"]): item
            for item in raw
            if isinstance(item, dict) and "fields" in item and "id" in item["fields"]
        }

    def current_map(self) -> Dict[str, Any]:
        return self.maps[self.map_id]

    def map_revision(self, map_id: Optional[int] = None) -> int:
        """Returns the current tile-mutation revision counter for a map."""
        mid = self.map_id if map_id is None else int(map_id)
        return int(self._map_revisions.get(mid, 0))

    def bump_map_revision(self, map_id: Optional[int] = None) -> int:
        """Increments a map's revision counter to invalidate cached render surfaces."""
        mid = self.map_id if map_id is None else int(map_id)
        nxt = self.map_revision(mid) + 1
        self._map_revisions[mid] = nxt
        return nxt

    def set_map_tile(self, map_id: int, layer: int, x: int, y: int, tile_id: int) -> bool:
        """Updates a map tile at runtime and bumps its revision counter."""
        m = self.maps.get(int(map_id))
        if m is None or layer not in (0, 1, 2):
            return False
        mw, mh = int(m.get("width", 0)), int(m.get("height", 0))
        if not (0 <= x < mw and 0 <= y < mh):
            return False
        grid = m.get("layers", {}).get(f"layer_{layer}")
        if not grid:
            return False
        grid[y][x] = int(tile_id)
        self.bump_map_revision(int(map_id))
        return True

    def active_events_on_map(self, map_id: Optional[int] = None) -> List[Tuple[Dict[str, Any], Dict[str, Any]]]:
        mid = self.map_id if map_id is None else map_id
        result: List[Tuple[Dict[str, Any], Dict[str, Any]]] = []
        for ev in self.maps[mid].get("events", []):
            if (mid, int(ev["id"])) in self.erased_events:
                continue
            active = select_active_page(ev, mid, self.switches, self.variables, self.self_switches)
            if active is not None:
                result.append((ev, active))
        return result

    def _blocking_coords(self) -> set[Tuple[int, int]]:
        blocked: set[Tuple[int, int]] = set()
        for ev, page in self.active_events_on_map():
            if not page.get("through", False) and page.get("trigger_code", 0) != 1:
                blocked.add((int(ev["x"]), int(ev["y"])))
        return blocked

    def _process_auto_events(self) -> None:
        """Runs active autorun map/common events first, then switch-eligible parallel events fairly."""
        if self.pending is not None:
            return
        active_map_pages = self.active_events_on_map()
        for ev, page in active_map_pages:
            if page.get("trigger_code", 0) == 3:
                self._run_commands(page.get("commands", []), 0, self.map_id, int(ev["id"]))
                if self.pending is not None:
                    return
        for _cid, ce in sorted(self.common_events.items()):
            if ce.get("trigger", 0) in (1, "autorun") and self.switches.get(int(ce.get("switch_id", 1)), False):
                self._run_commands(ce.get("commands", []), 0, self.map_id, 0)
                if self.pending is not None:
                    return
        parallel_map = [(ev, page) for ev, page in self.active_events_on_map() if page.get("trigger_code", 0) == 4]
        parallel_ces = [
            ce for _cid, ce in sorted(self.common_events.items())
            if ce.get("trigger", 0) in (2, "parallel") and self.switches.get(int(ce.get("switch_id", 1)), False)
        ]
        for ev, page in parallel_map:
            self._run_commands(page.get("commands", []), 0, self.map_id, int(ev["id"]))
            if self.pending is not None:
                return
        for ce in parallel_ces:
            self._run_commands(ce.get("commands", []), 0, self.map_id, 0)
            if self.pending is not None:
                return

    def _is_tile_walkable(self, x: int, y: int) -> bool:
        """Checks whether a single coordinate on the current map has walkable ground passages."""
        m = self.current_map()
        mw, mh = int(m.get("width", 1)), int(m.get("height", 1))
        if not (0 <= x < mw and 0 <= y < mh):
            return False
        if (x, y) in self._blocking_coords():
            return False
        passages = self.passages_by_ts.get(int(m.get("tileset_id", 1)), [])
        priorities = self.priorities_by_ts.get(int(m.get("tileset_id", 1)), [])
        layers = m.get("layers", {})
        for layer in (2, 1, 0):
            grid = layers.get(f"layer_{layer}", [])
            if not grid:
                continue
            tid = int(grid[y][x])
            if tid == 0:
                continue
            prio = priorities[tid] if tid < len(priorities) else 0
            pbit = passages[tid] if tid < len(passages) else 0
            if (pbit & 0x0F) == 0x0F and prio == 0:
                return False
            if (pbit & 0x0F) == 0x0F:
                continue
            return True
        return True

    def get_minimap_grid(self) -> Dict[str, Any]:
        """Builds compact terrain/entity coordinates for minimap rendering without GUI dependencies."""
        m = self.current_map()
        mw, mh = int(m.get("width", 1)), int(m.get("height", 1))
        ts_id = int(m.get("tileset_id", 1))
        tags = self.terrain_tags_by_ts.get(ts_id, [])
        has_water_tag = any(int(t) == 3 for t in tags)
        passages = self.passages_by_ts.get(ts_id, [])
        priorities = self.priorities_by_ts.get(ts_id, [])
        layers = m.get("layers", {})
        cells: List[List[int]] = []
        for y in range(mh):
            row: List[int] = []
            for x in range(mw):
                if has_water_tag and self._resolve_terrain_tag_at(x, y) == 3:
                    row.append(2)
                    continue
                walkable = True
                for layer in (2, 1, 0):
                    grid = layers.get(f"layer_{layer}", [])
                    if not grid:
                        continue
                    tid = int(grid[y][x])
                    if tid == 0:
                        continue
                    prio = priorities[tid] if tid < len(priorities) else 0
                    pbit = passages[tid] if tid < len(passages) else 0
                    if (pbit & 0x0F) == 0x0F and prio == 0:
                        walkable = False
                        break
                    if (pbit & 0x0F) != 0x0F:
                        walkable = True
                        break
                row.append(0 if walkable else 1)
            cells.append(row)
        return {
            "width": mw,
            "height": mh,
            "cells": cells,
            "player": (self.x, self.y),
            "events": [(int(ev["x"]), int(ev["y"])) for ev, _p in self.active_events_on_map()],
            "vehicles": [
                (vt, int(pos["x"]), int(pos["y"]))
                for vt, pos in self.vehicle_positions.items()
                if int(pos.get("map_id", 0)) == self.map_id
            ],
        }

    def _occupied_vehicle_coords(self, exclude_vtype: Optional[str] = None) -> set[Tuple[int, int]]:
        """Returns coordinates on the current map currently occupied by other parked vehicles."""
        occupied: set[Tuple[int, int]] = set()
        for vt, pos in self.vehicle_positions.items():
            if vt == exclude_vtype:
                continue
            if int(pos.get("map_id", 0)) == self.map_id:
                occupied.add((int(pos.get("x", -1)), int(pos.get("y", -1))))
        return occupied

    def _is_water_tile(self, x: int, y: int) -> bool:
        """Checks whether (x, y) is sailable water using terrain_tag==3 when present, else impassable non-blocking tiles."""
        m = self.current_map()
        ts_id = int(m.get("tileset_id", 1))
        tags = self.terrain_tags_by_ts.get(ts_id, [])
        if any(int(t) == 3 for t in tags):
            return self._resolve_terrain_tag_at(x, y) == 3
        return True

    def _can_vehicle_move(self, dx: int, dy: int) -> bool:
        m = self.current_map()
        mw, mh = int(m.get("width", 1)), int(m.get("height", 1))
        nx, ny = self.x + dx, self.y + dy
        if not (0 <= nx < mw and 0 <= ny < mh):
            return False
        if self.vehicle == "airship":
            return True
        if (nx, ny) in self._blocking_coords() or (nx, ny) in self._occupied_vehicle_coords(self.vehicle):
            return False
        return self._is_water_tile(nx, ny)

    def advance_picture_tweens(self, steps: int = 1) -> None:
        """Advances active move_picture frame interpolations deterministically."""
        if steps <= 0 or not self.pictures:
            return
        for pic in self.pictures.values():
            tw = pic.get("tween")
            if not isinstance(tw, dict):
                continue
            dur = max(1, int(tw.get("duration", 1)))
            elapsed = min(dur, int(tw.get("elapsed", 0)) + int(steps))
            tw["elapsed"] = elapsed
            t = elapsed / dur
            pic["render_x"] = round(int(tw["start_x"]) + (int(pic["x"]) - int(tw["start_x"])) * t)
            pic["render_y"] = round(int(tw["start_y"]) + (int(pic["y"]) - int(tw["start_y"])) * t)
            pic["render_zx"] = round(int(tw["start_zx"]) + (int(pic["zoom_x"]) - int(tw["start_zx"])) * t)
            pic["render_zy"] = round(int(tw["start_zy"]) + (int(pic["zoom_y"]) - int(tw["start_zy"])) * t)
            pic["render_op"] = round(int(tw["start_op"]) + (int(pic["opacity"]) - int(tw["start_op"])) * t)
            if elapsed >= dur:
                pic.pop("tween", None)
                for rk in ("render_x", "render_y", "render_zx", "render_zy", "render_op"):
                    pic.pop(rk, None)

    def _advance_camera_scroll_ms(self, dt_ms: float) -> None:
        if self.camera_scroll_pos_px[0] == self.camera_scroll_target_px[0] and self.camera_scroll_pos_px[1] == self.camera_scroll_target_px[1]:
            return
        px_per_sec = 32.0 * (2 ** max(0, int(self.camera_scroll_speed) - 2))
        max_step = px_per_sec * (float(dt_ms) / 1000.0)
        for axis in (0, 1):
            diff = float(self.camera_scroll_target_px[axis]) - self.camera_scroll_pos_px[axis]
            if abs(diff) <= max_step:
                self.camera_scroll_pos_px[axis] = float(self.camera_scroll_target_px[axis])
            else:
                self.camera_scroll_pos_px[axis] += max_step if diff > 0 else -max_step
            self.camera_scroll_px[axis] = int(round(self.camera_scroll_pos_px[axis]))

    def advance_timer_ms(self, dt_ms: float) -> None:
        """Advances play time, smooth camera scroll, and active RMXP countdown timer using elapsed frame ms."""
        if self.game_over or dt_ms <= 0:
            return
        self.play_time_ms += float(dt_ms)
        self._advance_camera_scroll_ms(dt_ms)
        if not self.timer_active:
            return
        self.timer_accum_ms += float(dt_ms)
        while self.timer_accum_ms >= 1000.0 and self.timer_seconds > 0:
            self.timer_accum_ms -= 1000.0
            self.timer_seconds -= 1
            if self.timer_seconds <= 0:
                self.timer_seconds = 0
                self.timer_active = False
                self.log.append("[Timer] 타이머 종료!")
                break

    def reset_erased_events(self, map_id: Optional[int] = None) -> int:
        """Restores temporarily erased events (all maps, or for a specific map_id)."""
        if map_id is None:
            cnt = len(self.erased_events)
            self.erased_events.clear()
            return cnt
        mid = int(map_id)
        targets = {pair for pair in self.erased_events if pair[0] == mid}
        self.erased_events -= targets
        return len(targets)

    def advance_screen_fx_frames(self, frames: int = 1) -> None:
        """Decays active flash/shake/fade/wait counters and advances picture tweens, rotation, fog scroll, and popups."""
        step = max(0, int(frames))
        if step <= 0:
            return
        if self.player_jump_frames > 0:
            self.player_jump_frames = max(0, self.player_jump_frames - step)
        if self.npc_jump_state:
            for k in list(self.npc_jump_state.keys()):
                rem_j = int(self.npc_jump_state[k].get("frames", 0)) - step
                if rem_j <= 0:
                    del self.npc_jump_state[k]
                else:
                    self.npc_jump_state[k]["frames"] = rem_j
        if self.wait_frames_left > 0:
            self.wait_frames_left = max(0, self.wait_frames_left - step)
            if self.wait_frames_left == 0 and self.pending_wait is not None and self.pending is None:
                pw = self.pending_wait
                self.pending_wait = None
                self._run_commands(
                    pw["commands"],
                    int(pw["resume_index"]),
                    int(pw["map_id"]),
                    int(pw["event_id"]),
                    stop_after_choice_branch=pw.get("stop_after_choice_branch"),
                    call_stack=pw.get("call_stack"),
                )
        for attr in ("pending_flash", "pending_shake", "pending_bgm_fade", "pending_bgs_fade"):
            fx = getattr(self, attr, None)
            if isinstance(fx, dict):
                rem = int(fx.get("frames", 0)) - step
                if rem <= 0:
                    setattr(self, attr, None)
                else:
                    fx["frames"] = rem
        if isinstance(self.pending_transition, dict) and not self.pending_transition.get("prepared", False):
            rem = int(self.pending_transition.get("frames", 0)) - step
            if rem <= 0:
                self.pending_transition = None
            else:
                self.pending_transition["frames"] = rem
        # Advance picture tweens & continuous rotation
        for pic in self.pictures.values():
            rot_sp = float(pic.get("rotate_speed", 0) or 0)
            if rot_sp != 0.0:
                pic["angle"] = (float(pic.get("angle", 0.0)) + rot_sp * step) % 360.0
            tw = pic.get("tween")
            if isinstance(tw, dict):
                dur = max(1, int(tw.get("duration", 1)))
                el = min(dur, int(tw.get("elapsed", 0)) + step)
                t = el / float(dur)
                pic["render_x"] = int(round(tw["start_x"] + (pic["x"] - tw["start_x"]) * t))
                pic["render_y"] = int(round(tw["start_y"] + (pic["y"] - tw["start_y"]) * t))
                pic["render_zx"] = int(round(tw["start_zx"] + (pic["zoom_x"] - tw["start_zx"]) * t))
                pic["render_zy"] = int(round(tw["start_zy"] + (pic["zoom_y"] - tw["start_zy"]) * t))
                pic["render_op"] = int(round(tw["start_op"] + (pic["opacity"] - tw["start_op"]) * t))
                if el >= dur:
                    pic.pop("tween", None)
                else:
                    tw["elapsed"] = el
        # Advance fog scroll coordinates
        if self.fog_state.get("name"):
            self.fog_state["scroll_x"] = float(self.fog_state.get("scroll_x", 0.0)) + float(self.fog_state.get("sx", 0)) * (step / 8.0)
            self.fog_state["scroll_y"] = float(self.fog_state.get("scroll_y", 0.0)) + float(self.fog_state.get("sy", 0)) * (step / 8.0)
        # Decay battle animation cues & floating damage popups
        if self.pending_battle_animations:
            kept_anims: List[Dict[str, Any]] = []
            for ba in self.pending_battle_animations:
                ba["frames"] = int(ba.get("frames", 20)) - step
                if ba["frames"] > 0:
                    kept_anims.append(ba)
            self.pending_battle_animations = kept_anims
        if self.pending and self.pending.battle_state and isinstance(self.pending.battle_state.get("floating_popups"), list):
            kept_pops: List[Dict[str, Any]] = []
            for pop in self.pending.battle_state["floating_popups"]:
                pop["frames"] = int(pop.get("frames", 30)) - step
                if pop["frames"] > 0:
                    kept_pops.append(pop)
            self.pending.battle_state["floating_popups"] = kept_pops

    def toggle_vehicle(self) -> bool:
        """Boards an adjacent/underfoot vehicle or disembarks onto valid terrain."""
        if self.vehicle == "walk":
            fdx, fdy = DIR_VECTORS[self.direction]
            candidates = [(self.x + fdx, self.y + fdy), (self.x, self.y)]
            for dname, (dx, dy) in DIR_VECTORS.items():
                if dname != self.direction:
                    candidates.append((self.x + dx, self.y + dy))
            for cx, cy in candidates:
                for vtype in ("airship", "ship", "boat"):
                    pos = self.vehicle_positions.get(vtype)
                    if pos and int(pos.get("map_id", 0)) == self.map_id and int(pos.get("x", -1)) == cx and int(pos.get("y", -1)) == cy:
                        self.vehicle = vtype
                        self.x, self.y = cx, cy
                        self.log.append(f"[Vehicle] {vtype} 탑승 ({cx}, {cy})")
                        return True
            return False

        # Disembark from current vehicle
        other_vehicles = self._occupied_vehicle_coords(self.vehicle)
        if self.vehicle == "airship":
            if self._is_tile_walkable(self.x, self.y) and (self.x, self.y) not in other_vehicles:
                self.vehicle_positions["airship"] = {"map_id": self.map_id, "x": self.x, "y": self.y}
                self.vehicle = "walk"
                self.log.append(f"[Vehicle] 비공정 하차 ({self.x}, {self.y})")
                return True
            return False

        if (self.x, self.y) in other_vehicles:
            return False
        fdx, fdy = DIR_VECTORS[self.direction]
        land_candidates = [(self.x + fdx, self.y + fdy)]
        for dname, (dx, dy) in DIR_VECTORS.items():
            if dname != self.direction:
                land_candidates.append((self.x + dx, self.y + dy))
        land_candidates.append((self.x, self.y))
        for lx, ly in land_candidates:
            if self._is_tile_walkable(lx, ly) and (lx, ly) not in other_vehicles:
                vname = self.vehicle
                self.vehicle_positions[vname] = {"map_id": self.map_id, "x": self.x, "y": self.y}
                self.x, self.y = lx, ly
                self.vehicle = "walk"
                self.log.append(f"[Vehicle] {vname} 하선 -> ({lx}, {ly})")
                return True
        return False

    def can_step_action(self, action: str) -> bool:
        """Checks whether a 4-way or 8-way diagonal move action is passable without corner-clipping."""
        m = self.current_map()
        blocked = self._blocking_coords()
        if action in DIR_VECTORS:
            dx, dy = DIR_VECTORS[action]
            if self.player_through:
                return 0 <= self.x + dx < int(m.get("width", 0)) and 0 <= self.y + dy < int(m.get("height", 0))
            if self.vehicle != "walk":
                return self._can_vehicle_move(dx, dy)
            return can_move_step(m, self.passages_by_ts, self.priorities_by_ts, blocked, self.x, self.y, dx, dy)
        if action in DIAG_VECTORS:
            dx, dy, _, _ = DIAG_VECTORS[action]
            if self.player_through:
                return 0 <= self.x + dx < int(m.get("width", 0)) and 0 <= self.y + dy < int(m.get("height", 0))
            if self.vehicle != "walk":
                return self._can_vehicle_move(dx, 0) and self._can_vehicle_move(dx, dy)
            path_h = can_move_step(m, self.passages_by_ts, self.priorities_by_ts, blocked, self.x, self.y, dx, 0) and can_move_step(
                m, self.passages_by_ts, self.priorities_by_ts, blocked, self.x + dx, self.y, 0, dy
            )
            path_v = can_move_step(m, self.passages_by_ts, self.priorities_by_ts, blocked, self.x, self.y, 0, dy) and can_move_step(
                m, self.passages_by_ts, self.priorities_by_ts, blocked, self.x, self.y + dy, dx, 0
            )
            return path_h or path_v
        return False

    def step(self, action: str) -> Dict[str, Any]:
        """Executes one deterministic player input action and returns step outcome."""
        self.ticks += 1
        events_before = len(self.log)
        if self.game_over:
            if action == "wait":
                return self._step_summary(events_before)
            if action == "restart":
                self.reload()
                return self._step_summary(events_before)
            if action == "load" or action.startswith("load:"):
                slot = int(action.split(":", 1)[1]) if ":" in action else 1
                self.load_from_file(slot=slot)
                return self._step_summary(events_before)
            if action == "gameover_retry":
                candidates: List[Tuple[float, Optional[int], Optional[Path]]] = [
                    (self.save_slot_path(s).stat().st_mtime, s, None)
                    for s in range(1, 10)
                    if self.slot_exists(s)
                ]
                if self.autosave_exists():
                    candidates.append((self.autosave_path().stat().st_mtime, None, self.autosave_path()))
                if candidates:
                    candidates.sort(key=lambda item: item[0], reverse=True)
                    _mt, slot_num, auto_p = candidates[0]
                    if auto_p is not None:
                        self.load_autosave()
                    elif slot_num is not None:
                        self.load_from_file(slot=slot_num)
                else:
                    self.reload()
                return self._step_summary(events_before)
            return self._step_summary(events_before)

        if self.pending is not None:
            if action.startswith("choose:") and self.pending.choices:
                idx = int(action.split(":", 1)[1])
                self._resolve_choice(idx)
            elif action in ("interact", "confirm") and not self.pending.choices:
                if self.pending.page_offset + 3 < len(self.pending.lines):
                    self.pending.page_offset += 3
                    return self._step_summary(events_before)
                pending = self.pending
                self.pending = None
                self._run_commands(
                    pending.commands,
                    pending.resume_index,
                    pending.map_id,
                    pending.event_id,
                    stop_after_choice_branch=pending.stop_after_choice_branch,
                    call_stack=pending.call_stack,
                )
            elif action == "wait":
                return self._step_summary(events_before)
            if self.pending is None:
                self._process_auto_events()
            return self._step_summary(events_before)

        if action.startswith("use_item:"):
            parts = action.split(":")
            iid = int(parts[1])
            tid = int(parts[2]) if len(parts) >= 3 else None
            self._use_healing_item(iid, actor_id=tid)
        elif action.startswith("use_skill:"):
            parts = action.split(":")
            if len(parts) < 3:
                raise ValueError(f"Invalid use_skill action format: {action!r}")
            caster_id = int(parts[1])
            skill_id = int(parts[2])
            tid = int(parts[3]) if len(parts) >= 4 else None
            self.use_skill_on_field(caster_id, skill_id, target_id=tid)
        elif action.startswith("face:"):
            d_face = action.split(":", 1)[1]
            if d_face not in DIR_VECTORS:
                raise ValueError(f"Unsupported facing direction: {d_face!r}")
            self.direction = d_face
        elif action.startswith("equip:"):
            parts = action.split(":")
            if len(parts) != 4:
                raise ValueError(f"Invalid equip action format: {action!r}")
            self.equip_from_bag(int(parts[1]), int(parts[2]), int(parts[3]))
        elif action == "save" or action.startswith("save:"):
            slot = int(action.split(":", 1)[1]) if ":" in action else 1
            self.save_to_file(slot=slot)
        elif action == "load" or action.startswith("load:"):
            slot = int(action.split(":", 1)[1]) if ":" in action else 1
            self.load_from_file(slot=slot)
        elif action == "toggle_vehicle":
            self.toggle_vehicle()
        elif action.startswith("swap_party:"):
            parts = action.split(":")
            if len(parts) == 3:
                self.swap_party_order(int(parts[1]), int(parts[2]))
        elif action in DIR_VECTORS:
            self.last_slide_was_ice = False
            self.direction = action
            dx, dy = DIR_VECTORS[action]
            if self.can_step_action(action):
                self.x += dx
                self.y += dy
                if self.vehicle != "walk":
                    self.vehicle_positions[self.vehicle] = {"map_id": self.map_id, "x": self.x, "y": self.y}
                else:
                    self._trigger_touch_at(self.x, self.y)
                    if not self.pending and not self.game_over:
                        self._apply_step_terrain_effects(action)
                    if not self.pending and not self.game_over:
                        self._advance_encounter_step()
            else:
                if self.vehicle == "walk":
                    self._trigger_touch_at(self.x + dx, self.y + dy, trigger_codes=(2,))
        elif action in DIAG_VECTORS:
            dx, dy, h_dir, v_dir = DIAG_VECTORS[action]
            if self.direction not in (h_dir, v_dir):
                self.direction = v_dir
            if self.can_step_action(action):
                self.x += dx
                self.y += dy
                if self.vehicle != "walk":
                    self.vehicle_positions[self.vehicle] = {"map_id": self.map_id, "x": self.x, "y": self.y}
                else:
                    self._trigger_touch_at(self.x, self.y)
                    if not self.pending and not self.game_over:
                        self._apply_step_terrain_effects(None)
                    if not self.pending and not self.game_over:
                        self._advance_encounter_step()
        elif action == "interact":
            dx, dy = DIR_VECTORS[self.direction]
            fx, fy = self.x + dx, self.y + dy
            if not self._trigger_action_at(fx, fy):
                triggered = False
                if self.tile_has_passage_flag(fx, fy, 0x80):
                    triggered = self._trigger_action_at(self.x + 2 * dx, self.y + 2 * dy)
                if not triggered:
                    self._trigger_action_at(self.x, self.y)
        elif action == "wait":
            pass
        else:
            raise ValueError(f"Unsupported simulation action: {action!r}")

        if self.pending is None:
            self._process_auto_events()
        return self._step_summary(events_before)

    def _step_summary(self, log_start: int) -> Dict[str, Any]:
        return {
            "tick": self.ticks,
            "map_id": self.map_id,
            "x": self.x,
            "y": self.y,
            "direction": self.direction,
            "new_logs": self.log[log_start:],
            "pending_choices": self.pending.choices if self.pending else [],
        }

    def _trigger_touch_at(self, tx: int, ty: int, trigger_codes: Tuple[int, ...] = (1, 2)) -> bool:
        for ev, page in self.active_events_on_map():
            if int(ev["x"]) == tx and int(ev["y"]) == ty and page.get("trigger_code", 0) in trigger_codes:
                self._run_commands(page.get("commands", []), 0, self.map_id, int(ev["id"]))
                return True
        return False

    def _trigger_action_at(self, tx: int, ty: int) -> bool:
        for ev, page in self.active_events_on_map():
            if int(ev["x"]) == tx and int(ev["y"]) == ty and page.get("trigger_code", 0) == 0:
                self._run_commands(page.get("commands", []), 0, self.map_id, int(ev["id"]))
                return True
        return False

    def tile_has_passage_flag(self, tx: int, ty: int, mask: int, map_id: Optional[int] = None) -> bool:
        """Checks whether any non-zero tile layer at (tx, ty) has the given RMXP passage bitmask (0x40 bush, 0x80 counter)."""
        mid = self.map_id if map_id is None else int(map_id)
        m = self.maps.get(mid)
        if not m:
            return False
        w, h = int(m.get("width", 0)), int(m.get("height", 0))
        if tx < 0 or ty < 0 or tx >= w or ty >= h:
            return False
        passages = self.passages_by_ts.get(int(m.get("tileset_id", 1)), [])
        if not passages:
            return False
        layers = m.get("layers", {})
        for z in ("layer_2", "layer_1", "layer_0"):
            tid = int(layers[z][ty][tx])
            if tid > 0 and tid < len(passages) and (int(passages[tid]) & int(mask)) != 0:
                return True
        return False

    def _resolve_terrain_tag_at(self, tx: int, ty: int) -> int:
        m = self.current_map()
        w, h = int(m["width"]), int(m["height"])
        if tx < 0 or ty < 0 or tx >= w or ty >= h:
            return 0
        tags = self.terrain_tags_by_ts.get(int(m["tileset_id"]), [])
        if not tags:
            return 0
        layers = m["layers"]
        for z in ("layer_2", "layer_1", "layer_0"):
            tid = int(layers[z][ty][tx])
            if tid == 0:
                continue
            tag = int(tags[tid]) if tid < len(tags) else 0
            if tag != 0:
                return tag
        return 0

    def _apply_step_terrain_effects(self, move_dir: Optional[str]) -> None:
        tag = self._resolve_terrain_tag_at(self.x, self.y)
        if tag == 2 and move_dir in DIR_VECTORS:
            dx, dy = DIR_VECTORS[move_dir]
            guard = 0
            while tag == 2 and guard < 20 and not self.pending and not self.game_over:
                if not self.can_step_action(move_dir):
                    break
                self.x += dx
                self.y += dy
                guard += 1
                self._trigger_touch_at(self.x, self.y)
                tag = self._resolve_terrain_tag_at(self.x, self.y)
            if guard > 0:
                self.last_slide_was_ice = True
                self.log.append(f"[Terrain] 빙판 미끄러짐 ({guard}칸)")
        if tag == 1:
            dmg_hit = 0
            for aid in self.party_member_ids:
                st = self._get_actor_state(aid)
                if int(st["hp"]) > 0:
                    dmg = max(1, int(st["max_hp"]) // 12)
                    st["hp"] = max(1, int(st["hp"]) - dmg)
                    dmg_hit = max(dmg_hit, dmg)
            if dmg_hit > 0:
                self.pending_flash = {"rgba": [255, 48, 48, 130], "frames": 8, "max_frames": 8}
                self.log.append(f"[Terrain] 바닥 함정 피해 (-{dmg_hit} HP)")
        slip_hit = 0
        for aid in self.party_member_ids:
            st = self._get_actor_state(aid)
            if int(st["hp"]) <= 1 or not st.get("states"):
                continue
            has_slip = any(
                bool(self.states_db.get(int(sid), {}).get("fields", {}).get("slip_damage", False))
                for sid in st.get("states", [])
            )
            if has_slip:
                sdmg = max(1, int(st["max_hp"]) // 20)
                st["hp"] = max(1, int(st["hp"]) - sdmg)
                slip_hit = max(slip_hit, sdmg)
        if slip_hit > 0:
            if self.pending_flash is None:
                self.pending_flash = {"rgba": [180, 48, 220, 95], "frames": 6, "max_frames": 6}
            self.log.append(f"[State] 상태이상 지속 피해 (-{slip_hit} HP)")

    def _format_dialogue_lines(self, lines: List[str]) -> List[str]:
        """Substitutes standard RMXP escape codes (\\N[n], \\V[n], \\G) in message text."""
        def _repl_actor(match: re.Match) -> str:
            aid = int(match.group(1))
            return str(self._get_actor_state(aid).get("name", f"Actor {aid}"))

        def _repl_var(match: re.Match) -> str:
            vid = int(match.group(1))
            return str(self.variables.get(vid, 0))

        out: List[str] = []
        for ln in lines:
            txt = re.sub(r"\\[Nn]\[(\d+)\]", _repl_actor, ln)
            txt = re.sub(r"\\[Vv]\[(\d+)\]", _repl_var, txt)
            txt = re.sub(r"\\[Gg]", f"{self.gold}G", txt)
            out.append(txt)
        return out

    def describe_shop_good(self, g: Dict[str, Any]) -> Dict[str, Any]:
        """Returns owned count, equippability, and lead-actor stat delta for a shop item."""
        lead_id = self.lead_actor_id
        lead_st = self._get_actor_state(lead_id)
        cur_wpn_atk = int(self.weapons.get(int(lead_st.get("weapon_id", 0)), {}).get("fields", {}).get("atk", 0))
        stores = {"item": self.inventory, "weapon": self.weapons_owned, "armor": self.armors_owned}
        g_type = str(g["type"])
        gid = int(g["id"])
        owned = int(stores.get(g_type, {}).get(gid, 0))
        tags: List[str] = [f"소지 {owned}"]
        can_equip = True
        stat_label = ""
        stat_diff = 0
        if g_type == "weapon":
            can_equip = self.can_actor_equip(lead_id, 0, gid)
            if can_equip:
                new_atk = int(self.weapons.get(gid, {}).get("fields", {}).get("atk", 0))
                stat_label = "ATK"
                stat_diff = new_atk - cur_wpn_atk
                tags.append(f"ATK {'+' if stat_diff > 0 else ''}{stat_diff}" if stat_diff != 0 else "ATK =")
            else:
                tags.append("장착불가")
        elif g_type == "armor":
            af = self.armors.get(gid, {}).get("fields", {})
            slot = max(1, min(4, int(af.get("kind", 0)) + 1))
            can_equip = self.can_actor_equip(lead_id, slot, gid)
            if can_equip:
                cur_aid = int(lead_st.get("armor_ids", {}).get(slot, 0))
                cur_def = int(self.armors.get(cur_aid, {}).get("fields", {}).get("pdef", 0))
                stat_label = "DEF"
                stat_diff = int(af.get("pdef", 0)) - cur_def
                tags.append(f"DEF {'+' if stat_diff > 0 else ''}{stat_diff}" if stat_diff != 0 else "DEF =")
            else:
                tags.append("장착불가")
        return {
            "type": g_type,
            "id": gid,
            "name": str(g.get("name", "")),
            "price": int(g.get("price", 0)),
            "owned": owned,
            "can_equip": can_equip,
            "stat_label": stat_label,
            "stat_diff": stat_diff,
            "summary_text": " | ".join(tags),
        }

    def get_shop_buy_details(self, pending: Optional[PendingDialogue] = None) -> List[Dict[str, Any]]:
        """Returns shop buy metadata (owned counts & gear stat diffs) for active shop goods."""
        target = pending or self.pending
        if target is None or not target.shop_goods:
            return []
        return [self.describe_shop_good(g) for g in target.shop_goods]

    def _format_shop_buy_choices(self, goods_meta: List[Dict[str, Any]], qty: int = 1) -> Tuple[List[str], int]:
        """Builds canonical shop buy choices and cancel index for dialogue menus."""
        q = 10 if int(qty) >= 10 else 1
        choices = [
            f"{g['name']} ({g['price']}G)" if q == 1 else f"{g['name']} x{q} ({int(g['price']) * q}G)"
            for g in goods_meta
        ]
        choices.extend(["판매하기", "나가기"])
        return choices, len(goods_meta) + 2

    def toggle_shop_qty(self) -> int:
        """Toggles active shop purchase/sale quantity between x1 and x10."""
        if self.pending is None or (not self.pending.shop_goods and self.pending.shop_mode != "sell"):
            return 1
        new_q = 10 if int(self.pending.shop_qty) == 1 else 1
        self.pending.shop_qty = new_q
        if self.pending.shop_mode == "buy":
            self.pending.choices, self.pending.cancel_type = self._format_shop_buy_choices(self.pending.shop_goods, qty=new_q)
            self.pending.lines = [f"[상점] 구매할 품목을 선택하세요 (x{new_q}, 소지금: {self.gold}G)"]
        return new_q

    def _collect_sellable_goods(self) -> List[Dict[str, Any]]:
        sell_list: List[Dict[str, Any]] = []
        for g_type, store, db in (
            ("item", self.inventory, self.items),
            ("weapon", self.weapons_owned, self.weapons),
            ("armor", self.armors_owned, self.armors),
        ):
            for oid in sorted(store):
                qty = int(store.get(oid, 0))
                if qty <= 0:
                    continue
                flds = db.get(oid, {}).get("fields", {})
                raw_price = int(flds.get("price", 0))
                if raw_price <= 0:
                    continue
                sell_price = max(1, raw_price // 2)
                sell_list.append(
                    {
                        "type": g_type,
                        "id": oid,
                        "name": str(flds.get("name", f"{g_type}#{oid}")),
                        "sell_price": sell_price,
                        "qty": qty,
                    }
                )
        return sell_list

    def _refresh_shop_sell_view(self, pending: PendingDialogue, header: Optional[str] = None) -> None:
        sell_items = self._collect_sellable_goods()
        pending.shop_mode = "sell"
        pending.shop_sell_items = sell_items
        pending.choices = [
            f"{it['name']} (판매 {it['sell_price']}G) x{it['qty']}"
            for it in sell_items
        ] + ["구매로 돌아가기"]
        pending.cancel_type = len(pending.choices)
        if header is not None:
            pending.lines = [header]
        else:
            pending.lines = [f"[상점-판매] 판매할 소지품을 선택하세요 (소지금: {self.gold}G)"]

    def _resolve_shop_choice(self, pending: PendingDialogue, choice_idx: int) -> None:
        qty = max(1, int(getattr(pending, "shop_qty", 1) or 1))
        if pending.shop_mode == "sell":
            sell_items = pending.shop_sell_items
            if choice_idx < 0 or choice_idx >= len(sell_items):
                pending.shop_mode = "buy"
                pending.shop_sell_items = []
                buy_choices, cancel_idx = self._format_shop_buy_choices(pending.shop_goods, qty=qty)
                pending.choices = buy_choices
                pending.cancel_type = cancel_idx
                pending.lines = [f"[상점] 구매할 품목을 선택하세요 (소지금: {self.gold}G)"]
                return
            entry = sell_items[choice_idx]
            sell_cnt = min(qty, int(entry.get("qty", 1)))
            s_price = int(entry["sell_price"]) * sell_cnt
            s_name = str(entry["name"])
            store = {"item": self.inventory, "weapon": self.weapons_owned, "armor": self.armors_owned}[entry["type"]]
            self._apply_inventory_delta(store, int(entry["id"]), "remove", sell_cnt)
            self.gold = min(9_999_999, self.gold + s_price)
            self.log.append(f"[Shop] {s_name} x{sell_cnt} 판매 (+{s_price}G)")
            self._refresh_shop_sell_view(pending, header=f"[상점] {s_name} x{sell_cnt} 판매 완료! (+{s_price}G / 소지금: {self.gold}G)")
            return

        goods = pending.shop_goods
        if choice_idx == len(goods):
            self._refresh_shop_sell_view(pending)
            return
        if choice_idx < 0 or choice_idx >= len(goods) + 1:
            self.pending = None
            self._run_commands(
                pending.commands,
                pending.resume_index,
                pending.map_id,
                pending.event_id,
                stop_after_choice_branch=pending.stop_after_choice_branch,
                call_stack=pending.call_stack,
            )
            return
        entry = goods[choice_idx]
        total_price = int(entry["price"]) * qty
        name = str(entry["name"])
        if self.gold >= total_price:
            self.gold -= total_price
            store = {"item": self.inventory, "weapon": self.weapons_owned, "armor": self.armors_owned}[entry["type"]]
            self._apply_inventory_delta(store, int(entry["id"]), "add", qty)
            suffix = f" x{qty}" if qty > 1 else ""
            self.log.append(f"[Shop] {name}{suffix} 구매 (-{total_price}G)")
            status_msg = f"[상점] {name}{suffix} 구매 완료! (소지금: {self.gold}G)"
        else:
            self.log.append(f"[Shop] 골드 부족 ({name}: {total_price}G 필요)")
            status_msg = f"[상점] 골드가 부족합니다 ({total_price}G 필요 / 소지금: {self.gold}G)"
        pending.choices, pending.cancel_type = self._format_shop_buy_choices(goods, qty=qty)
        pending.lines = [status_msg]

    def _resume_pending_script(self, pending: PendingDialogue) -> None:
        self.pending = None
        self._run_commands(
            pending.commands,
            pending.resume_index,
            pending.map_id,
            pending.event_id,
            stop_after_choice_branch=pending.stop_after_choice_branch,
            call_stack=pending.call_stack,
        )

    def submit_name_input(self, new_name: str) -> bool:
        """Applies an interactive actor rename for PendingDialogue.input_name and resumes event script."""
        pending = self.pending
        if pending is None or pending.input_name is None:
            return False
        aid = int(pending.input_name["actor_id"])
        max_char = max(1, int(pending.input_name.get("max_char", 12)))
        cleaned = str(new_name).strip()[:max_char]
        st = self._get_actor_state(aid)
        if cleaned:
            st["name"] = cleaned
            if aid in self.actors and isinstance(self.actors[aid].get("fields"), dict):
                self.actors[aid]["fields"]["name"] = cleaned
        self.log.append(f"[NameInput] Actor#{aid} -> {st['name']}")
        self._resume_pending_script(pending)
        return True

    def submit_button_input(self, key_code: int) -> bool:
        """Applies a captured RMXP button input code for PendingDialogue.button_input and resumes script."""
        pending = self.pending
        if pending is None or pending.button_input is None:
            return False
        vid = int(pending.button_input["variable_id"])
        code = int(key_code)
        self.variables[vid] = code
        self.log.append(f"[ButtonInput] V[{vid}] = {code}")
        self._resume_pending_script(pending)
        return True

    def _resolve_choice(self, choice_idx: int) -> None:
        pending = self.pending
        if pending is None:
            return
        if pending.shop_goods or pending.shop_mode == "sell":
            self._resolve_shop_choice(pending, choice_idx)
            return
        if pending.battle_state is not None:
            self._resolve_battle_choice(pending, choice_idx)
            return
        if pending.input_name is not None:
            self.submit_name_input(str(pending.input_name.get("text", "")))
            return
        if pending.button_input is not None:
            key_codes = [13, 27, 8, 2, 4, 6]
            code = key_codes[choice_idx] if 0 <= choice_idx < len(key_codes) else 13
            self.submit_button_input(code)
            return
        if pending.number_input is not None:
            ni = pending.number_input
            vid = int(ni["variable_id"])
            digits = int(ni["digits"])
            max_val = (10 ** digits) - 1
            val = int(ni["value"])
            if choice_idx == 0:
                val = min(max_val, val + 1)
            elif choice_idx == 1:
                val = min(max_val, val + 10)
            elif choice_idx == 2:
                val = max(0, val - 1)
            elif choice_idx == 3:
                val = 0
            else:
                self.variables[vid] = val
                self.log.append(f"[InputNumber] V[{vid}] = {val}")
                self._resume_pending_script(pending)
                return
            ni["value"] = val
            pending.lines = [f"[숫자 입력] 현재 값: {val:0{digits}d} ({digits}자리)"]
            return
        if pending.save_menu:
            if 0 <= choice_idx < 3:
                slot = choice_idx + 1
                self.save_to_file(slot=slot)
                pending.choices = [
                    f"슬롯 {s}: {self.get_save_slot_summary(s)['label']}"
                    for s in (1, 2, 3)
                ] + ["닫기"]
                pending.lines = [f"[저장] 슬롯 {slot}에 저장 완료!"]
                return
            self._resume_pending_script(pending)
            return
        self.pending = None
        cmds = pending.commands
        base_indent = int(cmds[pending.resume_index - 1].get("indent", 0)) if pending.resume_index > 0 else 0

        # Scan forward to matching when_choice branch at base_indent
        idx = pending.resume_index
        target_idx = len(cmds)
        while idx < len(cmds):
            c = cmds[idx]
            c_indent = int(c.get("indent", 0))
            if c_indent == base_indent and c.get("op") == "when_choice" and int(c.get("index", -1)) == choice_idx:
                target_idx = idx + 1
                break
            if c_indent == base_indent and c.get("op") == "end_choices":
                target_idx = idx + 1
                break
            idx += 1

        self.log.append(f"[Choice] {pending.choices[choice_idx] if 0 <= choice_idx < len(pending.choices) else choice_idx}")
        self._run_commands(
            cmds,
            target_idx,
            pending.map_id,
            pending.event_id,
            stop_after_choice_branch=base_indent,
            call_stack=pending.call_stack,
        )

    def _run_commands(
        self,
        cmds: List[Dict[str, Any]],
        start_idx: int,
        map_id: int,
        event_id: int,
        stop_after_choice_branch: Optional[int] = None,
        call_stack: Optional[List[Tuple[List[Dict[str, Any]], int, Optional[int]]]] = None,
    ) -> None:
        stack = list(call_stack) if call_stack else []
        i = start_idx
        loop_guard = 0

        while True:
            if i >= len(cmds):
                if stack:
                    cmds, i, stop_after_choice_branch = stack.pop()
                    continue
                return

            cmd = cmds[i]
            op = cmd.get("op")
            indent = int(cmd.get("indent", 0))

            if stop_after_choice_branch is not None and indent == stop_after_choice_branch and op in ("when_choice", "when_cancel", "end_choices"):
                while i < len(cmds) and not (int(cmds[i].get("indent", 0)) == stop_after_choice_branch and cmds[i].get("op") == "end_choices"):
                    i += 1
                i += 1
                stop_after_choice_branch = None
                continue

            if op in ("end", "exit_event", "erase_event"):
                if op == "erase_event":
                    if event_id > 0:
                        self.erased_events.add((map_id, event_id))
                        self._active_move_routes.pop((map_id, event_id), None)
                    return
                if op == "exit_event":
                    return
                if stack:
                    cmds, i, stop_after_choice_branch = stack.pop()
                    continue
                return

            if op == "say":
                raw_lines = self._format_dialogue_lines([str(x) for x in cmd.get("lines", [])])
                lines, tag_face, tag_fidx = _extract_face_tag(raw_lines)
                face_name = str(cmd.get("face_name", tag_face) or "")
                face_index = int(cmd.get("face_index", tag_fidx) or 0)
                tpos = int(self.text_options.get("position", 2))
                sframe = bool(self.text_options.get("show_frame", True))
                self.log.extend(lines)
                if i + 1 < len(cmds) and cmds[i + 1].get("op") == "show_choices" and int(cmds[i + 1].get("indent", 0)) == indent:
                    ch_cmd = cmds[i + 1]
                    self.pending = PendingDialogue(
                        lines=lines,
                        choices=list(ch_cmd.get("choices", [])),
                        cancel_type=int(ch_cmd.get("cancel_type", 0)),
                        commands=cmds,
                        resume_index=i + 2,
                        map_id=map_id,
                        event_id=event_id,
                        stop_after_choice_branch=stop_after_choice_branch,
                        call_stack=stack,
                        face_name=face_name,
                        face_index=face_index,
                        text_position=tpos,
                        show_frame=sframe,
                    )
                else:
                    self.pending = PendingDialogue(
                        lines=lines,
                        commands=cmds,
                        resume_index=i + 1,
                        map_id=map_id,
                        event_id=event_id,
                        stop_after_choice_branch=stop_after_choice_branch,
                        call_stack=stack,
                        face_name=face_name,
                        face_index=face_index,
                        text_position=tpos,
                        show_frame=sframe,
                    )
                return

            if op == "change_text_options":
                self.text_options = {
                    "position": max(0, min(2, int(cmd.get("position", 2)))),
                    "show_frame": bool(cmd.get("show_frame", True)),
                }
                self.log.append(f"[TextOpt] pos={self.text_options['position']} frame={self.text_options['show_frame']}")
            elif op == "button_input_processing":
                vid = int(cmd.get("variable_id", 1))
                self.pending = PendingDialogue(
                    lines=[f"[키 입력 대기] 키를 누르세요 (V[{vid}])"],
                    choices=["확인(Enter)", "취소(ESC)", "위", "아래", "왼쪽", "오른쪽"],
                    cancel_type=2,
                    commands=cmds,
                    resume_index=i + 1,
                    map_id=map_id,
                    event_id=event_id,
                    stop_after_choice_branch=stop_after_choice_branch,
                    call_stack=stack,
                    button_input={"variable_id": vid},
                )
                return
            elif op == "name_input":
                aid = int(cmd.get("actor_id", self.lead_actor_id))
                max_char = max(1, min(16, int(cmd.get("max_char", cmd.get("max_chars", 12)))))
                cur_name = str(self._get_actor_state(aid)["name"])[:max_char]
                self.pending = PendingDialogue(
                    lines=[f"[이름 입력] 현재 이름: {cur_name} (최대 {max_char}자)"],
                    choices=["확인"],
                    cancel_type=0,
                    commands=cmds,
                    resume_index=i + 1,
                    map_id=map_id,
                    event_id=event_id,
                    stop_after_choice_branch=stop_after_choice_branch,
                    call_stack=stack,
                    input_name={"actor_id": aid, "max_char": max_char, "text": cur_name},
                )
                return

            if op == "show_choices":
                self.pending = PendingDialogue(
                    lines=[],
                    choices=list(cmd.get("choices", [])),
                    cancel_type=int(cmd.get("cancel_type", 0)),
                    commands=cmds,
                    resume_index=i + 1,
                    map_id=map_id,
                    event_id=event_id,
                    stop_after_choice_branch=stop_after_choice_branch,
                    call_stack=stack,
                )
                return

            if op == "set_switch":
                val = str(cmd.get("value", "ON")).upper() == "ON"
                for sid in range(int(cmd["start_id"]), int(cmd["end_id"]) + 1):
                    self.switches[sid] = val
            elif op == "set_variable":
                operand = cmd.get("operand", [0])
                otype = int(cmd.get("operand_type", 0))
                if otype == 0:
                    rhs = int(operand[0]) if operand else 0
                elif otype == 1:
                    rhs = int(self.variables.get(int(operand[0]) if operand else 1, 0))
                elif otype == 2 and len(operand) >= 2:
                    lo, hi = int(operand[0]), int(operand[1])
                    if lo > hi:
                        lo, hi = hi, lo
                    rhs = lo + (self.ticks + self.x + self.y) % max(1, hi - lo + 1)
                elif otype == 4:
                    sub_other = int(operand[0]) if operand else 0
                    rhs = {
                        0: self.map_id,
                        1: len(self.party_member_ids),
                        2: self.gold,
                        3: self.ticks,
                        4: self.timer_seconds,
                    }.get(sub_other, 0)
                else:
                    rhs = 0
                for vid in range(int(cmd["start_id"]), int(cmd["end_id"]) + 1):
                    self.variables[vid] = _apply_variable_op(
                        self.variables.get(vid, 0), int(cmd.get("operation", 0)), rhs
                    )
            elif op == "set_self_switch":
                val = str(cmd.get("value", "ON")).upper() == "ON"
                self.self_switches[(map_id, event_id, str(cmd.get("channel", "A")))] = val
            elif op == "common_event":
                ce = self.common_events.get(int(cmd.get("common_event_id", 0)))
                if ce:
                    if len(stack) >= MAX_COMMON_EVENT_DEPTH:
                        raise RuntimeError(f"CommonEvent call depth exceeded limit ({MAX_COMMON_EVENT_DEPTH})")
                    stack.append((cmds, i + 1, stop_after_choice_branch))
                    cmds = ce.get("commands", [])
                    i = 0
                    stop_after_choice_branch = None
                    continue
            elif op == "change_gold":
                amt = int(cmd.get("amount", 0))
                self.gold = min(9_999_999, max(0, self.gold + amt if cmd.get("mode", "add") == "add" else self.gold - amt))
            elif op in ("change_item", "change_weapon", "change_armor"):
                store, id_key = (
                    (self.inventory, "item_id") if op == "change_item"
                    else (self.weapons_owned, "weapon_id") if op == "change_weapon"
                    else (self.armors_owned, "armor_id")
                )
                self._apply_inventory_delta(store, int(cmd[id_key]), str(cmd.get("mode", "add")), int(cmd.get("amount", 1)))
            elif op == "recover_all":
                for tid in self._target_actor_ids(int(cmd.get("actor_id", 0))):
                    st = self._get_actor_state(tid)
                    st["hp"] = st["max_hp"]
                    st["sp"] = st["max_sp"]
                    st["states"] = []
                    st.pop("state_turns", None)
                if int(cmd.get("actor_id", 0)) == 0 and len(self.party_member_ids) > 1:
                    self.log.append(f"[Heal] 전체 파티 전회복 (선두 HP {self.hp}/{self.max_hp}, SP {self.sp}/{self.max_sp})")
                else:
                    st = self._get_actor_state(self._target_actor_ids(int(cmd.get("actor_id", 0)))[0])
                    self.log.append(f"[Heal] {st['name']} 전회복 (HP {st['hp']}/{st['max_hp']}, SP {st['sp']}/{st['max_sp']})")
            elif op == "change_hp":
                amt = self._resolve_cmd_amount(cmd)
                is_add = cmd.get("mode", "add") == "add"
                allow_ko = bool(cmd.get("allow_knockout", False))
                for tid in self._target_actor_ids(int(cmd.get("actor_id", 0))):
                    st = self._get_actor_state(tid)
                    if is_add:
                        st["hp"] = min(int(st["max_hp"]), int(st["hp"]) + amt)
                    else:
                        floor_hp = 0 if allow_ko else 1
                        st["hp"] = max(floor_hp, int(st["hp"]) - amt)
                if all(int(self._get_actor_state(aid)["hp"]) <= 0 for aid in self.party_member_ids):
                    self.game_over = True
            elif op == "change_sp":
                amt = self._resolve_cmd_amount(cmd)
                is_add = cmd.get("mode", "add") == "add"
                for tid in self._target_actor_ids(int(cmd.get("actor_id", 0))):
                    st = self._get_actor_state(tid)
                    st["sp"] = min(int(st["max_sp"]), int(st["sp"]) + amt) if is_add else max(0, int(st["sp"]) - amt)
            elif op == "change_state":
                sid = int(cmd.get("state_id", 0))
                is_add = cmd.get("mode", "add") == "add"
                if sid > 0:
                    for tid in self._target_actor_ids(int(cmd.get("actor_id", 0))):
                        st_list = self._get_actor_state(tid).setdefault("states", [])
                        if is_add:
                            if sid not in st_list:
                                st_list.append(sid)
                        elif sid in st_list:
                            st_list.remove(sid)
            elif op == "change_exp":
                amt = self._resolve_cmd_amount(cmd)
                if cmd.get("mode", "add") == "add":
                    self.exp += amt
                    for tid in self._target_actor_ids(int(cmd.get("actor_id", 0))):
                        self._grant_actor_exp(tid, amt)
            elif op == "change_level":
                delta = self._resolve_cmd_amount(cmd)
                is_add = cmd.get("mode", "add") == "add"
                for tid in self._target_actor_ids(int(cmd.get("actor_id", 0))):
                    st = self._get_actor_state(tid)
                    target_lv = max(1, int(st["level"]) + (delta if is_add else -delta))
                    if target_lv > int(st["level"]):
                        needed = _exp_for_level(target_lv, int(st["exp_basis"]), int(st["exp_inflation"]))
                        self._grant_actor_exp(tid, max(0, needed - int(st["exp"])))
                    else:
                        st["level"] = target_lv
                        st["exp"] = _exp_for_level(target_lv, int(st["exp_basis"]), int(st["exp_inflation"]))
                self._recompute_combat_stats()
            elif op == "change_parameter":
                pid = int(cmd.get("param_id", 0))
                amt = self._resolve_cmd_amount(cmd)
                signed = amt if cmd.get("mode", "add") == "add" else -amt
                param_key_map = {
                    0: "max_hp", 1: "max_sp", 2: "base_str",
                    3: "base_dex", 4: "base_agi", 5: "base_int",
                }
                pkey = param_key_map.get(pid)
                if pkey:
                    for tid in self._target_actor_ids(int(cmd.get("actor_id", 0))):
                        st = self._get_actor_state(tid)
                        min_v = 1 if pkey in ("max_hp", "base_str") else 0
                        st[pkey] = max(min_v, int(st.get(pkey, 10)) + signed)
                        if pkey == "max_hp":
                            st["hp"] = min(int(st["hp"]), int(st["max_hp"]))
                        elif pkey == "max_sp":
                            st["sp"] = min(int(st["sp"]), int(st["max_sp"]))
                    self._recompute_combat_stats()
            elif op == "change_actor_name":
                tid = int(cmd.get("actor_id", 1))
                st = self._get_actor_state(tid)
                st["name"] = str(cmd.get("name", st["name"]))
            elif op == "change_actor_class":
                tid = int(cmd.get("actor_id", 1))
                new_cid = int(cmd.get("class_id", 1))
                st = self._get_actor_state(tid)
                st["class_id"] = new_cid
                self._apply_class_vital_bonuses(st)
                new_skills = self._sync_actor_learnings(st)
                if not self.is_slot_fixed(tid, 0) and int(st.get("weapon_id", 0)) > 0 and not self.can_actor_equip(tid, 0, int(st["weapon_id"])):
                    self.equip_from_bag(tid, 0, 0)
                for eq_slot in (1, 2, 3, 4):
                    aid_eq = int(st.get("armor_ids", {}).get(eq_slot, 0))
                    if aid_eq > 0 and not self.is_slot_fixed(tid, eq_slot) and not self.can_actor_equip(tid, eq_slot, aid_eq):
                        self.equip_from_bag(tid, eq_slot, 0)
                st["hp"] = min(int(st["hp"]), int(st["max_hp"]))
                st["sp"] = min(int(st["sp"]), int(st["max_sp"]))
                self._recompute_combat_stats()
                cls_name = str(self.classes_db.get(new_cid, {}).get("fields", {}).get("name", f"Class {new_cid}"))
                skill_note = f" (+스킬 {len(new_skills)}개)" if new_skills else ""
                self.log.append(f"[Actor] 클래스 변경: ID {tid} -> {cls_name}{skill_note}")
            elif op == "change_actor_graphic":
                tid = int(cmd.get("actor_id", 1))
                st = self._get_actor_state(tid)
                new_gfx = str(cmd.get("character_name", st["character_name"]))
                st["character_name"] = new_gfx
                if "character_hue" in cmd:
                    st["character_hue"] = int(cmd.get("character_hue", 0))
                self.log.append(f"[Actor] 그래픽 변경: ID {tid} -> {new_gfx or '(없음)'}")
            elif op == "game_over":
                self.hp = 0
                self.game_over = True
                return
            elif op == "return_to_title":
                self.reload()
                return
            elif op == "change_party":
                aid = int(cmd.get("actor_id", 1))
                mode = str(cmd.get("mode", "add"))
                if mode == "add":
                    if bool(cmd.get("initialize", False)) or aid not in self.actor_states:
                        self.actor_states[aid] = self._build_actor_state(aid)
                    if aid not in self.party_member_ids and len(self.party_member_ids) < 4:
                        self.party_member_ids.append(aid)
                        self.log.append(f"[Party] + {self._get_actor_state(aid)['name']} (ID {aid})")
                elif aid in self.party_member_ids and len(self.party_member_ids) > 1:
                    self.party_member_ids.remove(aid)
                    self.log.append(f"[Party] - {self._get_actor_state(aid)['name']} (ID {aid})")
                self._recompute_combat_stats()
            elif op == "change_skill":
                aid = int(cmd.get("actor_id", 0)) or self.lead_actor_id
                st = self._get_actor_state(aid)
                sid = int(cmd["skill_id"])
                skills = st["learned_skills"]
                if cmd.get("mode", "learn") == "learn":
                    if sid not in skills:
                        skills.append(sid)
                elif sid in skills:
                    skills.remove(sid)
            elif op == "change_equip":
                aid = int(cmd.get("actor_id", 0)) or self.lead_actor_id
                st = self._get_actor_state(aid)
                eq_type = int(cmd.get("equip_type", 0))
                eq_id = int(cmd.get("equip_id", 0))
                if eq_type == 0:
                    old_id = int(st["weapon_id"])
                    if old_id != eq_id:
                        if old_id > 0:
                            self._apply_inventory_delta(self.weapons_owned, old_id, "add", 1)
                        if eq_id > 0 and self.weapons_owned.get(eq_id, 0) > 0:
                            self._apply_inventory_delta(self.weapons_owned, eq_id, "remove", 1)
                        st["weapon_id"] = eq_id
                elif eq_type in (1, 2, 3, 4):
                    old_id = int(st["armor_ids"].get(eq_type, 0))
                    if old_id != eq_id:
                        if old_id > 0:
                            self._apply_inventory_delta(self.armors_owned, old_id, "add", 1)
                        if eq_id > 0 and self.armors_owned.get(eq_id, 0) > 0:
                            self._apply_inventory_delta(self.armors_owned, eq_id, "remove", 1)
                        st["armor_ids"][eq_type] = eq_id
                self._recompute_combat_stats()
            elif op == "transfer_player":
                prev_map = self.map_id
                if int(cmd.get("var_mode", 0)) == 1:
                    self.map_id = int(self.variables.get(int(cmd["map_id"]), self.map_id))
                    self.x = int(self.variables.get(int(cmd["x"]), 0))
                    self.y = int(self.variables.get(int(cmd["y"]), 0))
                else:
                    self.map_id = int(cmd["map_id"])
                    self.x = int(cmd["x"])
                    self.y = int(cmd["y"])
                d_str = str(cmd.get("direction", "retain"))
                if d_str in DIR_VECTORS:
                    self.direction = d_str
                if self.map_id != prev_map or bool(cmd.get("reset_erased", False)):
                    if self.map_id != prev_map:
                        self.last_map_banner = str(self.maps.get(self.map_id, {}).get("name", ""))
                    self.erased_events.clear()
                self.camera_scroll_px = [0, 0]
                self.camera_scroll_target_px = [0, 0]
                self.camera_scroll_pos_px = [0.0, 0.0]
                self._sync_map_autoplay_audio()
                self.log.append(f"[Warp] -> Map {self.map_id} ({self.x}, {self.y})")
            elif op == "set_vehicle_location":
                vtype = str(cmd.get("vehicle", "boat"))
                vmode = int(cmd.get("var_mode", cmd.get("loc_type", 0)))
                if vmode == 1:
                    vmid = int(self.variables.get(int(cmd.get("map_id", 1)), self.map_id))
                    vx = int(self.variables.get(int(cmd.get("x", 0)), 0))
                    vy = int(self.variables.get(int(cmd.get("y", 0)), 0))
                else:
                    vmid = int(cmd.get("map_id", self.map_id))
                    vx = int(cmd.get("x", 0))
                    vy = int(cmd.get("y", 0))
                self.vehicle_positions[vtype] = {"map_id": vmid, "x": vx, "y": vy}
                if self.vehicle == vtype:
                    self.map_id, self.x, self.y = vmid, vx, vy
                self.log.append(f"[Vehicle] {vtype} 위치 설정 -> Map {vmid} ({vx}, {vy})")
            elif op == "set_event_location":
                tid = int(cmd.get("event_id", cmd.get("target_id", 0)))
                ev_id = event_id if tid == 0 else tid
                loc_type = int(cmd.get("var_mode", cmd.get("loc_type", 0)))
                map_evs = self.maps.get(map_id, {}).get("events", [])
                target_ev = next((e for e in map_evs if int(e.get("id", -1)) == ev_id), None)
                if target_ev is not None:
                    mw = max(1, int(self.maps.get(map_id, {}).get("width", 1)))
                    mh = max(1, int(self.maps.get(map_id, {}).get("height", 1)))
                    if loc_type == 1:
                        nx = int(self.variables.get(int(cmd.get("x", 1)), target_ev.get("x", 0)))
                        ny = int(self.variables.get(int(cmd.get("y", 1)), target_ev.get("y", 0)))
                        target_ev["x"] = max(0, min(mw - 1, nx))
                        target_ev["y"] = max(0, min(mh - 1, ny))
                    elif loc_type == 2:
                        other_id = int(cmd.get("x", 0))
                        other_ev = next((e for e in map_evs if int(e.get("id", -1)) == other_id), None)
                        if other_ev is not None:
                            ox, oy = int(other_ev.get("x", 0)), int(other_ev.get("y", 0))
                            other_ev["x"], other_ev["y"] = int(target_ev.get("x", 0)), int(target_ev.get("y", 0))
                            target_ev["x"], target_ev["y"] = ox, oy
                    else:
                        nx = int(cmd.get("x", target_ev.get("x", 0)))
                        ny = int(cmd.get("y", target_ev.get("y", 0)))
                        target_ev["x"] = max(0, min(mw - 1, nx))
                        target_ev["y"] = max(0, min(mh - 1, ny))
                    self.log.append(f"[Event] #{ev_id} 이동 -> ({target_ev['x']}, {target_ev['y']})")
            elif op == "control_timer":
                if str(cmd.get("mode", "start")) == "start":
                    self.timer_seconds = max(0, int(cmd.get("seconds", 0)))
                    self.timer_active = self.timer_seconds > 0
                    self.timer_accum_ms = 0.0
                    self.log.append(f"[Timer] 시작 {self.timer_seconds}s")
                else:
                    self.timer_active = False
                    self.log.append("[Timer] 정지")
            elif op == "change_save_access":
                self.save_enabled = bool(cmd.get("enabled", True))
                self.log.append(f"[System] 저장 {'허용' if self.save_enabled else '금지'}")
            elif op == "change_menu_access":
                self.menu_enabled = bool(cmd.get("enabled", True))
                self.log.append(f"[System] 메뉴 {'허용' if self.menu_enabled else '금지'}")
            elif op == "change_encounter":
                self.encounter_enabled = bool(cmd.get("enabled", True))
                self.log.append(f"[System] 인카운터 {'활성' if self.encounter_enabled else '비활성'}")
            elif op == "change_transparent_flag":
                self.player_transparent = bool(cmd.get("transparent", False))
                self.log.append(f"[System] 플레이어 투명 상태: {'ON' if self.player_transparent else 'OFF'}")
            elif op == "change_windowskin":
                self.windowskin_name = str(cmd.get("windowskin_name", cmd.get("name", "")))
                self.log.append(f"[System] 윈도우스킨 변경: {self.windowskin_name or '(기본)'}")
            elif op == "change_battle_bgm":
                self.battle_bgm = {
                    "name": str(cmd.get("name", "")),
                    "volume": int(cmd.get("volume", 100)),
                    "pitch": int(cmd.get("pitch", 100)),
                }
                self.log.append(f"[Audio] 전투 BGM 변경: {self.battle_bgm['name'] or '(없음)'}")
            elif op == "change_battle_end_me":
                self.battle_end_me = {
                    "name": str(cmd.get("name", "")),
                    "volume": int(cmd.get("volume", 100)),
                    "pitch": int(cmd.get("pitch", 100)),
                }
                self.log.append(f"[Audio] 전투 종료 ME 변경: {self.battle_end_me['name'] or '(없음)'}")
            elif op in ("wait", "wait_frames"):
                frames = max(0, int(cmd.get("frames", 0)))
                if frames > 0:
                    self.log.append(f"[Wait] {frames}f")
                    if self.wait_pause_enabled:
                        self.wait_frames_left = frames
                        self.pending_wait = {
                            "commands": cmds,
                            "resume_index": i + 1,
                            "map_id": map_id,
                            "event_id": event_id,
                            "stop_after_choice_branch": stop_after_choice_branch,
                            "call_stack": list(stack),
                        }
                        return
                    self.advance_timer_ms(frames * (1000.0 / 60.0))
                    self.advance_screen_fx_frames(frames)
            elif op == "scroll_map":
                d_str = str(cmd.get("direction", "down"))
                dx, dy = DIR_VECTORS.get(d_str, (0, 0))
                dist = max(0, int(cmd.get("distance", 0)))
                self.camera_scroll_speed = max(1, min(6, int(cmd.get("speed", 4))))
                self.camera_scroll_target_px[0] += dx * dist * 32
                self.camera_scroll_target_px[1] += dy * dist * 32
                self.log.append(f"[Screen] 맵 스크롤 {d_str} {dist}타일")
            elif op == "show_animation":
                raw_cid = int(cmd.get("character_id", -1))
                cid = event_id if raw_cid == 0 else raw_cid
                aid = int(cmd.get("animation_id", 1))
                self.pending_animations.append({"character_id": cid, "animation_id": aid})
                self.log.append(f"[Anim] 캐릭터 {cid} 애니메이션 #{aid}")
            elif op == "show_balloon":
                raw_cid = int(cmd.get("character_id", -1))
                cid = event_id if raw_cid == 0 else raw_cid
                bid = max(1, int(cmd.get("balloon_id", 1)))
                self.pending_balloons.append({"character_id": cid, "balloon_id": bid})
                self.log.append(f"[Balloon] 캐릭터 {cid} 말풍선 #{bid}")
            elif op == "fadeout_screen":
                self.screen_faded_out = True
                self.log.append("[Screen] 페이드아웃")
            elif op == "fadein_screen":
                self.screen_faded_out = False
                self.log.append("[Screen] 페이드인")
            elif op == "prepare_transition":
                self.screen_faded_out = True
                self.pending_transition = {"prepared": True, "name": "", "frames": 0}
                self.log.append("[Screen] 트랜지션 준비")
            elif op == "execute_transition":
                tname = str(cmd.get("name", ""))
                tdur = max(1, int(cmd.get("duration", 20)))
                self.screen_faded_out = False
                self.pending_transition = {"prepared": False, "name": tname, "frames": tdur}
                self.log.append(f"[Screen] 트랜지션 실행: {tname or '기본'}")
            elif op == "set_weather":
                wtype = str(cmd.get("weather", "none"))
                if wtype not in ("none", "rain", "storm", "snow"):
                    wtype = "none"
                power = max(0, min(9, int(cmd.get("power", 0))))
                self.weather = {"type": wtype, "power": power}
                self.log.append(f"[Weather] {wtype} (강도 {power})")
            elif op == "show_picture":
                pnum = max(1, int(cmd.get("number", 1)))
                vmode = int(cmd.get("var_mode", 0))
                px = int(self.variables.get(int(cmd.get("x", 0)), 0)) if vmode == 1 else int(cmd.get("x", 0))
                py = int(self.variables.get(int(cmd.get("y", 0)), 0)) if vmode == 1 else int(cmd.get("y", 0))
                self.pictures[pnum] = {
                    "number": pnum,
                    "name": str(cmd.get("name", "")),
                    "origin": int(cmd.get("origin", 0)),
                    "x": px,
                    "y": py,
                    "zoom_x": int(cmd.get("zoom_x", 100)),
                    "zoom_y": int(cmd.get("zoom_y", 100)),
                    "opacity": max(0, min(255, int(cmd.get("opacity", 255)))),
                }
                self.log.append(f"[Picture] #{pnum} 표시 ({self.pictures[pnum]['name']})")
            elif op == "move_picture":
                pnum = max(1, int(cmd.get("number", 1)))
                vmode = int(cmd.get("var_mode", 0))
                px = int(self.variables.get(int(cmd.get("x", 0)), 0)) if vmode == 1 else int(cmd.get("x", 0))
                py = int(self.variables.get(int(cmd.get("y", 0)), 0)) if vmode == 1 else int(cmd.get("y", 0))
                if pnum in self.pictures:
                    cur_pic = self.pictures[pnum]
                    frames = max(0, int(cmd.get("frames", 0)))
                    new_zx = int(cmd.get("zoom_x", cur_pic.get("zoom_x", 100)))
                    new_zy = int(cmd.get("zoom_y", cur_pic.get("zoom_y", 100)))
                    new_op = max(0, min(255, int(cmd.get("opacity", cur_pic.get("opacity", 255)))))
                    if frames > 0:
                        sx = int(cur_pic.get("render_x", cur_pic["x"]))
                        sy = int(cur_pic.get("render_y", cur_pic["y"]))
                        szx = int(cur_pic.get("render_zx", cur_pic["zoom_x"]))
                        szy = int(cur_pic.get("render_zy", cur_pic["zoom_y"]))
                        sop = int(cur_pic.get("render_op", cur_pic["opacity"]))
                        cur_pic["tween"] = {
                            "start_x": sx, "start_y": sy, "start_zx": szx, "start_zy": szy, "start_op": sop,
                            "elapsed": 0, "duration": frames,
                        }
                        cur_pic.update({"render_x": sx, "render_y": sy, "render_zx": szx, "render_zy": szy, "render_op": sop})
                    cur_pic.update({
                        "origin": int(cmd.get("origin", cur_pic.get("origin", 0))),
                        "x": px,
                        "y": py,
                        "zoom_x": new_zx,
                        "zoom_y": new_zy,
                        "opacity": new_op,
                    })
                self.log.append(f"[Picture] #{pnum} 이동 -> ({px}, {py})")
            elif op == "erase_picture":
                pnum = max(1, int(cmd.get("number", 1)))
                self.pictures.pop(pnum, None)
                self.log.append(f"[Picture] #{pnum} 제거")
            elif op == "rotate_picture":
                pnum = max(1, int(cmd.get("number", 1)))
                spd = int(cmd.get("speed", 0))
                if pnum in self.pictures:
                    self.pictures[pnum]["rotate_speed"] = spd
                    if spd == 0:
                        self.pictures[pnum]["angle"] = 0.0
                self.log.append(f"[Picture] #{pnum} 회전 속도 {spd}")
            elif op == "change_picture_tone":
                pnum = max(1, int(cmd.get("number", 1)))
                tone = [max(-255, min(255, int(v))) for v in cmd.get("rgbg", [0, 0, 0, 0])]
                while len(tone) < 4:
                    tone.append(0)
                if pnum in self.pictures:
                    self.pictures[pnum]["tone"] = tone[:4]
                self.log.append(f"[Picture] #{pnum} 색조 {tone[:4]}")
            elif op == "change_fog_tone":
                tone = [max(-255, min(255, int(v))) for v in cmd.get("rgbg", [0, 0, 0, 0])]
                while len(tone) < 4:
                    tone.append(0)
                self.fog_state["tone"] = tone[:4]
                self.log.append(f"[Fog] 색조 변경 {tone[:4]}")
            elif op == "change_fog_opacity":
                fop = max(0, min(255, int(cmd.get("opacity", 64))))
                self.fog_state["opacity"] = fop
                self.log.append(f"[Fog] 불투명도 {fop}")
            elif op == "input_number":
                vid = int(cmd.get("variable_id", 1))
                digits = max(1, min(8, int(cmd.get("digits", 2))))
                cur_val = max(0, min((10 ** digits) - 1, int(self.variables.get(vid, 0))))
                self.pending = PendingDialogue(
                    lines=[f"[숫자 입력] 현재 값: {cur_val:0{digits}d} ({digits}자리)"],
                    choices=["+1", "+10", "-1", "초기화(0)", "확인"],
                    cancel_type=5,
                    commands=cmds,
                    resume_index=i + 1,
                    map_id=map_id,
                    event_id=event_id,
                    stop_after_choice_branch=stop_after_choice_branch,
                    call_stack=stack,
                    number_input={"variable_id": vid, "digits": digits, "value": cur_val},
                )
                return
            elif op == "open_save_menu":
                s_choices = [f"슬롯 {s}: {self.get_save_slot_summary(s)['label']}" for s in (1, 2, 3)] + ["닫기"]
                self.pending = PendingDialogue(
                    lines=["[저장] 저장할 슬롯을 선택하세요"],
                    choices=s_choices,
                    cancel_type=4,
                    commands=cmds,
                    resume_index=i + 1,
                    map_id=map_id,
                    event_id=event_id,
                    stop_after_choice_branch=stop_after_choice_branch,
                    call_stack=stack,
                    save_menu=True,
                )
                return
            elif op == "change_screen_tone":
                rgbg = [max(-255, min(255, int(v))) for v in cmd.get("rgbg", [0, 0, 0, 0])]
                while len(rgbg) < 4:
                    rgbg.append(0)
                self.screen_tone = rgbg[:4]
                self.log.append(f"[Screen] 색조 변경 {self.screen_tone}")
            elif op == "screen_flash":
                rgba = [max(0, min(255, int(v))) for v in cmd.get("rgba", [255, 255, 255, 255])]
                while len(rgba) < 4:
                    rgba.append(255)
                frames = max(1, int(cmd.get("frames", 20)))
                self.pending_flash = {"rgba": rgba[:4], "frames": frames, "max_frames": frames}
            elif op == "screen_shake":
                frames = max(1, int(cmd.get("frames", 20)))
                self.pending_shake = {
                    "power": max(1, int(cmd.get("power", 5))),
                    "speed": max(1, int(cmd.get("speed", 5))),
                    "frames": frames,
                    "max_frames": frames,
                }
            elif op == "play_bgm":
                b_name = str(cmd.get("name", ""))
                self.current_bgm = {
                    "name": b_name,
                    "volume": int(cmd.get("volume", 80)),
                    "pitch": int(cmd.get("pitch", 100)),
                } if b_name else None
                self.log.append(f"[Audio] BGM: {b_name or '정지'}")
            elif op == "fadeout_bgm":
                secs = max(0, int(cmd.get("seconds", 1)))
                self.pending_bgm_fade = {"seconds": secs, "frames": max(1, secs * 60), "max_frames": max(1, secs * 60)}
                self.current_bgm = None
                self.log.append(f"[Audio] BGM 페이드아웃 ({secs}s)")
            elif op == "play_bgs":
                s_name = str(cmd.get("name", ""))
                self.current_bgs = {
                    "name": s_name,
                    "volume": int(cmd.get("volume", 80)),
                    "pitch": int(cmd.get("pitch", 100)),
                } if s_name else None
                self.log.append(f"[Audio] BGS: {s_name or '정지'}")
            elif op == "fadeout_bgs":
                secs = max(0, int(cmd.get("seconds", 1)))
                self.pending_bgs_fade = {"seconds": secs, "frames": max(1, secs * 60), "max_frames": max(1, secs * 60)}
                self.current_bgs = None
                self.log.append(f"[Audio] BGS 페이드아웃 ({secs}s)")
            elif op == "memorize_bgm":
                self.memorized_bgm = dict(self.current_bgm) if self.current_bgm else None
                self.memorized_bgs = dict(self.current_bgs) if self.current_bgs else None
                self.log.append("[Audio] BGM/BGS 기억")
            elif op == "restore_bgm":
                self.current_bgm = dict(self.memorized_bgm) if self.memorized_bgm else None
                self.current_bgs = dict(self.memorized_bgs) if self.memorized_bgs else None
                self.log.append("[Audio] BGM/BGS 복원")
            elif op == "play_me":
                me_name = str(cmd.get("name", ""))
                if me_name:
                    self.played_me_queue.append(
                        {
                            "name": me_name,
                            "volume": int(cmd.get("volume", 80)),
                            "pitch": int(cmd.get("pitch", 100)),
                        }
                    )
                    self.log.append(f"[Audio] ME: {me_name}")
            elif op == "play_se":
                se_name = str(cmd.get("name", ""))
                if se_name:
                    self.played_se_queue.append(
                        {
                            "name": se_name,
                            "volume": int(cmd.get("volume", 80)),
                            "pitch": int(cmd.get("pitch", 100)),
                        }
                    )
                    self.log.append(f"[Audio] SE: {se_name}")
            elif op == "stop_se":
                self.played_se_queue.clear()
                self.log.append("[Audio] SE 정지")
            elif op == "call_menu":
                if self.menu_enabled:
                    self.pending_menu_open = True
                self.log.append("[System] 메뉴 호출")
            elif op == "script":
                self._execute_safe_script([str(x) for x in cmd.get("lines", [])])
            elif op in ("enemy_appear", "enemy_transform", "show_battle_animation", "deal_damage", "force_action"):
                bs_active = self.pending.battle_state if (self.pending and self.pending.battle_state) else None
                note = self._apply_battle_event_op(str(op), cmd, bs_active)
                if note:
                    self.log.append(note)
            elif op == "set_move_route":
                tid = int(cmd.get("target_id", 0))
                steps = [str(s) for s in cmd.get("steps", [])]
                repeat = bool(cmd.get("repeat", False))
                skippable = bool(cmd.get("skippable", True))
                if tid == -1:
                    for mv in steps:
                        if mv == "through_on":
                            self.player_through = True
                        elif mv == "through_off":
                            self.player_through = False
                        elif mv.startswith("opacity:"):
                            self.player_opacity = max(0, min(255, int(mv.split(":", 1)[1])))
                        elif mv.startswith("blend:"):
                            self.player_blend_type = max(0, min(2, int(mv.split(":", 1)[1])))
                        elif mv.startswith("jump:"):
                            parts = mv.split(":")
                            jx = int(parts[1]) if len(parts) > 1 else 0
                            jy = int(parts[2]) if len(parts) > 2 else 0
                            m = self.current_map()
                            nx = max(0, min(int(m["width"]) - 1, self.x + jx))
                            ny = max(0, min(int(m["height"]) - 1, self.y + jy))
                            if self.player_through or self._is_tile_walkable(nx, ny):
                                self.x, self.y = nx, ny
                                self.player_jump_frames = 14
                                self.player_jump_total = 14
                        elif mv.startswith("graphic:"):
                            parts = mv.split(":")
                            lead_st = self._get_actor_state(self.lead_actor_id)
                            if len(parts) > 1 and parts[1]:
                                lead_st["character_name"] = parts[1]
                        elif mv.startswith("turn_") and mv[5:] in DIR_VECTORS:
                            self.direction = mv[5:]
                        elif mv in DIR_VECTORS or mv in DIAG_VECTORS:
                            if self.can_step_action(mv) or not skippable:
                                dx, dy = DIR_VECTORS[mv] if mv in DIR_VECTORS else DIAG_VECTORS[mv][:2]
                                if mv in DIR_VECTORS:
                                    self.direction = mv
                                if self.can_step_action(mv):
                                    self.x += dx
                                    self.y += dy
                else:
                    ev_id = event_id if tid == 0 else tid
                    self._active_move_routes[(map_id, ev_id)] = {
                        "steps": steps,
                        "idx": 0,
                        "repeat": repeat,
                        "skippable": skippable,
                    }
                    self.advance_npc_routes(target_event_id=ev_id)
            elif op == "wait_for_move_completion":
                for k in list(self._active_move_routes.keys()):
                    k_mid, k_eid = k
                    if k_mid != map_id:
                        continue
                    rt = self._active_move_routes.get(k)
                    if not rt or rt.get("repeat"):
                        continue
                    guard = max(8, len(rt.get("steps", [])) + 8)
                    while k in self._active_move_routes and guard > 0:
                        guard -= 1
                        self.advance_npc_routes(target_event_id=k_eid)
                    self._active_move_routes.pop(k, None)
            elif op == "shop":
                goods_meta: List[Dict[str, Any]] = []
                type_dbs = {"item": self.items, "weapon": self.weapons, "armor": self.armors}
                for g in cmd.get("goods", []):
                    g_type = str(g.get("type", "item"))
                    gid = int(g.get("id", 0))
                    fields = type_dbs.get(g_type, {}).get(gid, {}).get("fields", {})
                    g_name = str(fields.get("name", f"{g_type}#{gid}"))
                    g_price = max(0, int(fields.get("price", 0)))
                    goods_meta.append({"type": g_type, "id": gid, "name": g_name, "price": g_price})
                choices, cancel_idx = self._format_shop_buy_choices(goods_meta)
                self.pending = PendingDialogue(
                    lines=[f"[상점] 구매할 품목을 선택하세요 (소지금: {self.gold}G)"],
                    choices=choices,
                    cancel_type=cancel_idx,
                    commands=cmds,
                    resume_index=i + 1,
                    map_id=map_id,
                    event_id=event_id,
                    stop_after_choice_branch=stop_after_choice_branch,
                    call_stack=stack,
                    shop_goods=goods_meta,
                )
                return
            elif op == "battle":
                troop_id = int(cmd.get("troop_id", 1))
                can_esc = bool(cmd.get("can_escape", False))
                can_los = bool(cmd.get("can_lose", False))
                if self.interactive_battle and troop_id in self.troops:
                    self._start_interactive_battle(
                        troop_id,
                        can_escape=can_esc,
                        can_lose=can_los,
                        commands=cmds,
                        battle_idx=i,
                        base_indent=indent,
                        map_id=map_id,
                        event_id=event_id,
                        stop_after_choice_branch=stop_after_choice_branch,
                        call_stack=stack,
                    )
                    return
                outcome = self._resolve_battle(troop_id, can_escape=can_esc, can_lose=can_los)
                if self.game_over:
                    return
                branch_idx = self._jump_battle_branch(cmds, i, indent, outcome)
                if branch_idx is not None:
                    i = branch_idx
                    continue
            elif op in ("when_win", "when_escape", "when_lose"):
                i = self._skip_to_end_battle(cmds, i, indent)
                continue
            elif op == "end_battle":
                pass
            elif op in (
                "if_switch", "if_variable", "if_item", "if_self_switch",
                "if_gold", "if_weapon", "if_armor",
                "if_timer", "if_vehicle",
                "if_actor_in_party", "if_actor_name", "if_actor_skill", "if_actor_weapon", "if_actor_armor", "if_actor_state",
            ):
                if not self._eval_branch_condition(str(op), cmd, map_id=map_id, event_id=event_id):
                    i = self._skip_if_branch(cmds, i, indent)
                    continue
            elif op == "else":
                i = self._skip_to_end_if(cmds, i, indent)
                continue
            elif op == "loop":
                pass
            elif op == "end_loop":
                loop_guard += 1
                if loop_guard < 500:
                    j = i - 1
                    while j >= 0:
                        if int(cmds[j].get("indent", 0)) == indent and cmds[j].get("op") == "loop":
                            i = j + 1
                            break
                        j -= 1
                    else:
                        i += 1
                    continue
            elif op == "break_loop":
                j = i + 1
                while j < len(cmds):
                    if int(cmds[j].get("indent", 0)) < indent and cmds[j].get("op") == "end_loop":
                        i = j + 1
                        break
                    j += 1
                else:
                    i = len(cmds)
                continue
            elif op == "label":
                pass
            elif op == "jump_to_label":
                loop_guard += 1
                target_lbl = str(cmd.get("name", ""))
                if loop_guard < 500:
                    lbl_idx = next(
                        (k for k, c in enumerate(cmds) if c.get("op") == "label" and str(c.get("name", "")) == target_lbl),
                        None,
                    )
                    if lbl_idx is not None:
                        i = lbl_idx + 1
                        continue
            elif op == "quest_update":
                qid = str(cmd.get("quest_id", "")).strip()
                if qid:
                    self.set_quest(
                        qid,
                        str(cmd.get("title", qid)),
                        status=str(cmd.get("status", "active")),
                        desc=str(cmd.get("desc", "")),
                    )
            elif op == "comment":
                c_lines = cmd.get("lines") or ([cmd.get("text", "")] if cmd.get("text") else [])
                for raw_ln in c_lines:
                    txt = str(raw_ln).strip()
                    if txt.startswith("@QUEST:"):
                        parts = txt[7:].split("|")
                        if len(parts) >= 3:
                            qid, qst, qtitle = parts[0].strip(), parts[1].strip(), parts[2].strip()
                            qdesc = parts[3].strip() if len(parts) > 3 else ""
                            if qid:
                                self.set_quest(qid, qtitle, status=qst or "active", desc=qdesc)

            i += 1

    def set_quest(self, quest_id: str, title: str, status: str = "active", desc: str = "") -> None:
        """Creates or updates a quest journal entry (active, completed, failed)."""
        st = str(status).lower().strip()
        if st not in ("active", "completed", "failed"):
            st = "active"
        prev = self.quests.get(str(quest_id), {})
        self.quests[str(quest_id)] = {
            "id": str(quest_id),
            "title": str(title or prev.get("title", quest_id)),
            "status": st,
            "desc": str(desc if desc != "" else prev.get("desc", "")),
        }
        tag = {"active": "수락", "completed": "완료", "failed": "실패"}[st]
        self.log.append(f"[Quest] {tag}: {self.quests[str(quest_id)]['title']}")

    def list_quest_lines(self) -> List[str]:
        """Formats current quest journal entries for GUI overlay rendering."""
        if not self.quests:
            return ["진행 중인 퀘스트가 없습니다."]
        tag_map = {"active": "[진행중]", "completed": "[완료]", "failed": "[실패]"}
        rank_map = {"active": 0, "completed": 1, "failed": 2}
        ordered = sorted(self.quests.values(), key=lambda q: (rank_map.get(q.get("status", "active"), 9), q.get("id", "")))
        out: List[str] = []
        for q in ordered:
            tag = tag_map.get(q.get("status", "active"), "[진행중]")
            desc = f" - {q['desc']}" if q.get("desc") else ""
            out.append(f"{tag} {q.get('title', q.get('id', ''))}{desc}")
        return out

    def _target_actor_ids(self, actor_id: int) -> List[int]:
        if actor_id <= 0:
            return list(self.party_member_ids)
        return [actor_id]

    def _resolve_cmd_amount(self, cmd: Dict[str, Any]) -> int:
        raw = int(cmd.get("amount", 0))
        if int(cmd.get("operand_type", 0)) == 1:
            return int(self.variables.get(raw, 0))
        return raw

    def _advance_encounter_step(self) -> None:
        if not self.enable_random_encounters or not self.encounter_enabled:
            return
        m = self.current_map()
        enc_list = [int(t) for t in m.get("encounter_list", []) if int(t) > 0]
        if not enc_list:
            return
        threshold = max(1, int(m.get("encounter_step", 30)))
        self.encounter_steps += 1
        if self.encounter_steps >= threshold:
            self.encounter_steps = 0
            troop_id = enc_list[(self.ticks + self.x + self.y) % len(enc_list)]
            if self.interactive_battle and troop_id in self.troops:
                self._start_interactive_battle(troop_id, can_escape=True, can_lose=False)
            else:
                self._resolve_battle(troop_id, can_escape=True, can_lose=False)

    @staticmethod
    def _apply_inventory_delta(store: Dict[int, int], target_id: int, mode: str, amount: int) -> None:
        cur = store.get(target_id, 0)
        store[target_id] = min(99, cur + amount) if mode == "add" else max(0, cur - amount)

    def _eval_branch_condition(self, op: str, cmd: Dict[str, Any], map_id: int = 0, event_id: int = 0) -> bool:
        if op == "if_switch":
            return self.switches.get(int(cmd["switch_id"]), False) == (str(cmd.get("expected", "ON")).upper() == "ON")
        if op == "if_variable":
            lhs = self.variables.get(int(cmd["variable_id"]), 0)
            rhs = self.variables.get(int(cmd["rhs_value"]), 0) if cmd.get("rhs_is_var") else int(cmd.get("rhs_value", 0))
            return eval_variable_cmp(lhs, rhs, int(cmd.get("cmp", 0)))
        if op == "if_self_switch":
            expected_on = str(cmd.get("expected", "ON")).upper() == "ON"
            return self.self_switches.get((map_id, event_id, str(cmd.get("channel", "A"))), False) == expected_on
        if op == "if_timer":
            sec = int(cmd.get("seconds", 0))
            return self.timer_active and ((self.timer_seconds >= sec) if int(cmd.get("cmp", 0)) == 0 else (self.timer_seconds <= sec))
        if op == "if_vehicle":
            return self.vehicle == str(cmd.get("vehicle", "boat"))
        if op == "if_gold":
            amt = int(cmd.get("amount", 0))
            return (self.gold >= amt) if int(cmd.get("cmp", 0)) == 0 else (self.gold <= amt)
        if op == "if_item":
            return self.inventory.get(int(cmd["item_id"]), 0) > 0
        if op == "if_weapon":
            wid = int(cmd["weapon_id"])
            return self.weapons_owned.get(wid, 0) > 0
        if op == "if_armor":
            aid = int(cmd["armor_id"])
            return self.armors_owned.get(aid, 0) > 0
        if op == "if_actor_in_party":
            return int(cmd["actor_id"]) in self.party_member_ids
        if op == "if_actor_name":
            return str(self._get_actor_state(int(cmd["actor_id"]))["name"]) == str(cmd.get("name", ""))
        if op == "if_actor_skill":
            return int(cmd["skill_id"]) in self._get_actor_state(int(cmd["actor_id"]))["learned_skills"]
        if op == "if_actor_weapon":
            return int(self._get_actor_state(int(cmd["actor_id"]))["weapon_id"]) == int(cmd["weapon_id"])
        if op == "if_actor_armor":
            aid_eq = int(cmd["armor_id"])
            return aid_eq in {int(v) for v in self._get_actor_state(int(cmd["actor_id"]))["armor_ids"].values()}
        if op == "if_actor_state":
            return int(cmd["state_id"]) in self._get_actor_state(int(cmd["actor_id"])).get("states", [])
        return True

    @staticmethod
    def _skip_if_branch(cmds: List[Dict[str, Any]], idx: int, base_indent: int) -> int:
        j = idx + 1
        while j < len(cmds):
            c = cmds[j]
            if int(c.get("indent", 0)) == base_indent and c.get("op") in ("else", "end_if"):
                return j + 1
            j += 1
        return len(cmds)

    @staticmethod
    def _skip_to_end_if(cmds: List[Dict[str, Any]], idx: int, base_indent: int) -> int:
        j = idx + 1
        while j < len(cmds):
            c = cmds[j]
            if int(c.get("indent", 0)) == base_indent and c.get("op") == "end_if":
                return j + 1
            j += 1
        return len(cmds)

    @staticmethod
    def _jump_battle_branch(
        cmds: List[Dict[str, Any]], idx: int, base_indent: int, outcome: str
    ) -> Optional[int]:
        if idx + 1 >= len(cmds):
            return None
        nxt = cmds[idx + 1]
        if int(nxt.get("indent", 0)) != base_indent or nxt.get("op") not in ("when_win", "when_escape", "when_lose"):
            return None
        target_op = f"when_{outcome}"
        j = idx + 1
        while j < len(cmds):
            c = cmds[j]
            if int(c.get("indent", 0)) == base_indent:
                cop = c.get("op")
                if cop == target_op:
                    return j + 1
                if cop == "end_battle":
                    return j + 1
            j += 1
        return len(cmds)

    @staticmethod
    def _skip_to_end_battle(cmds: List[Dict[str, Any]], idx: int, base_indent: int) -> int:
        j = idx + 1
        while j < len(cmds):
            c = cmds[j]
            if int(c.get("indent", 0)) == base_indent and c.get("op") == "end_battle":
                return j + 1
            j += 1
        return len(cmds)

    def _use_healing_item(self, item_id: int, actor_id: Optional[int] = None) -> bool:
        if self.inventory.get(item_id, 0) <= 0:
            return False
        item_fields = self.items.get(item_id, {}).get("fields", {})
        occ = int(item_fields.get("occasion", 0))
        in_battle = bool(self.pending and self.pending.battle_state is not None)
        if occ not in ((0, 1) if in_battle else (0, 2)):
            return False
        rec_hp = int(item_fields.get("recover_hp", 0))
        rec_hp_rate = int(item_fields.get("recover_hp_rate", 0))
        rec_sp = int(item_fields.get("recover_sp", 0))
        rec_sp_rate = int(item_fields.get("recover_sp_rate", 0))
        tid = self.lead_actor_id if actor_id is None else actor_id
        if actor_id is None and len(self.party_member_ids) > 1:
            living = [self._get_actor_state(aid) for aid in self.party_member_ids if self._get_actor_state(aid)["hp"] > 0]
            if living:
                most_hurt = min(living, key=lambda s: (s["hp"] / max(1, s["max_hp"]), 0 if s["id"] == self.lead_actor_id else 1))
                tid = int(most_hurt["id"])
        st = self._get_actor_state(tid)
        minus_states = [int(x) for x in item_fields.get("minus_state_set", [])]
        is_revive = int(item_fields.get("scope", 3)) in (5, 6) or 1 in minus_states
        if int(st["hp"]) <= 0 and not is_revive:
            return False
        heal_hp = rec_hp + (int(st["max_hp"]) * rec_hp_rate) // 100
        heal_sp = rec_sp + (int(st["max_sp"]) * rec_sp_rate) // 100
        cur_states = st.setdefault("states", [])
        can_cure = any(msid in cur_states for msid in minus_states)
        if heal_hp <= 0 and heal_sp <= 0 and not can_cure:
            return False
        if item_fields.get("consumable", True):
            self.inventory[item_id] -= 1
            if self.inventory[item_id] <= 0:
                self.inventory.pop(item_id, None)
        if int(st["hp"]) <= 0 and is_revive:
            st["hp"] = max(1, min(int(st["max_hp"]), heal_hp))
        else:
            st["hp"] = min(int(st["max_hp"]), int(st["hp"]) + heal_hp)
        st["sp"] = min(int(st["max_sp"]), int(st["sp"]) + heal_sp)
        self._apply_state_sets(cur_states, [], minus_states)
        anim2 = int(item_fields.get("animation2_id", 0) or 0)
        if anim2 > 0:
            self.pending_animations.append({"character_id": -1, "animation_id": anim2})
        mse = item_fields.get("menu_se")
        if isinstance(mse, dict):
            mf = mse.get("fields", mse)
            if mf.get("name"):
                self.played_se_queue.append({"name": str(mf["name"]), "volume": int(mf.get("volume", 80)), "pitch": int(mf.get("pitch", 100))})
        cure_note = ", 상태 회복" if can_cure else ""
        self.log.append(f"[Item] {st['name']} <- {item_fields.get('name', item_id)} (+{heal_hp} HP, +{heal_sp} SP{cure_note})")
        return True

    def use_skill_on_field(self, caster_id: int, skill_id: int, target_id: Optional[int] = None) -> bool:
        """Casts a learned recovery/cure skill on the field outside battle (occasion 0 or 2)."""
        caster = self._get_actor_state(caster_id)
        if int(caster["hp"]) <= 0 or skill_id not in caster.get("learned_skills", []):
            return False
        sk = self.skills_db.get(skill_id, {}).get("fields", {})
        if int(sk.get("occasion", 1)) not in (0, 2):
            return False
        sp_cost = max(0, int(sk.get("sp_cost", 0)))
        if int(caster["sp"]) < sp_cost:
            self.log.append(f"[Skill] {caster['name']}: SP 부족 ({sk.get('name', skill_id)})")
            return False
        power = int(sk.get("power", 0))
        scope = int(sk.get("scope", 3))
        if scope in (4, 6):
            targets = [aid for aid in self.party_member_ids if int(self._get_actor_state(aid)["hp"]) > 0 or scope == 6]
        elif scope == 5 and target_id is None:
            dead_ids = [aid for aid in self.party_member_ids if int(self._get_actor_state(aid)["hp"]) <= 0]
            targets = [dead_ids[0]] if dead_ids else [self.lead_actor_id]
        else:
            targets = [self.lead_actor_id if target_id is None else int(target_id)]
        minus_states = [int(x) for x in sk.get("minus_state_set", [])]
        did_apply = False
        for tid in targets:
            tst = self._get_actor_state(tid)
            if int(tst["hp"]) <= 0 and scope not in (5, 6):
                continue
            heal_amt = abs(power) if power < 0 else (power if int(sk.get("variance", 0)) == 0 and power > 0 and scope in (3, 4, 5, 6) else 0)
            if int(tst["hp"]) <= 0 and scope in (5, 6):
                tst["hp"] = max(1, min(int(tst["max_hp"]), heal_amt or int(tst["max_hp"]) // 4))
                self._apply_state_sets(tst.setdefault("states", []), [], [1])
                did_apply = True
            elif heal_amt > 0 and int(tst["hp"]) < int(tst["max_hp"]):
                tst["hp"] = min(int(tst["max_hp"]), int(tst["hp"]) + heal_amt)
                did_apply = True
            for msid in minus_states:
                if msid in tst.get("states", []):
                    tst["states"].remove(msid)
                    did_apply = True
        if not did_apply:
            return False
        caster["sp"] = int(caster["sp"]) - sp_cost
        anim2 = int(sk.get("animation2_id", 0) or 0)
        if anim2 > 0:
            self.pending_animations.append({"character_id": -1, "animation_id": anim2})
        mse = sk.get("menu_se")
        if isinstance(mse, dict):
            mf = mse.get("fields", mse)
            if mf.get("name"):
                self.played_se_queue.append({"name": str(mf["name"]), "volume": int(mf.get("volume", 80)), "pitch": int(mf.get("pitch", 100))})
        self.log.append(f"[Skill] {caster['name']} -> {sk.get('name', skill_id)} (SP -{sp_cost})")
        return True

    def _auto_use_potion_if_low(self, actor_id: Optional[int] = None) -> None:
        tid = self.lead_actor_id if actor_id is None else actor_id
        st = self._get_actor_state(tid)
        if int(st["hp"]) <= 0 or int(st["hp"]) * 100 > int(st["max_hp"]) * 40:
            return
        for iid, cnt in list(self.inventory.items()):
            if cnt <= 0:
                continue
            item_fields = self.items.get(iid, {}).get("fields", {})
            if int(item_fields.get("recover_hp", 0)) > 0 or int(item_fields.get("recover_hp_rate", 0)) > 0:
                if self._use_healing_item(iid, actor_id=tid):
                    return

    def _equipped_gear_fields(self, actor_id: int) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
        st = self._get_actor_state(actor_id)
        wf = self.weapons.get(int(st.get("weapon_id", 0)), {}).get("fields", {})
        arm_flds = [
            self.armors.get(int(aid), {}).get("fields", {})
            for aid in st.get("armor_ids", {}).values()
            if int(aid) > 0
        ]
        return wf, arm_flds

    def _actor_attack_power(self, actor_id: int) -> int:
        st = self._get_actor_state(actor_id)
        wf, _arm_flds = self._equipped_gear_fields(actor_id)
        cb = self._class_stat_bonuses(int(st.get("class_id", 1)))
        return int(st["base_str"]) + cb["str"] + cb["atk"] + int(wf.get("atk", 0))

    def _actor_defense(self, actor_id: int) -> int:
        st = self._get_actor_state(actor_id)
        wf, arm_flds = self._equipped_gear_fields(actor_id)
        cb = self._class_stat_bonuses(int(st.get("class_id", 1)))
        return int(st["base_pdef"]) + cb["pdef"] + int(wf.get("pdef", 0)) + sum(int(af.get("pdef", 0)) for af in arm_flds)

    def actor_full_stats(self, actor_id: int) -> Dict[str, int]:
        """Returns derived ATK, PDEF, MDEF, STR, DEX, AGI, INT, and EVA combining base stats, class_params, and gear."""
        st = self._get_actor_state(actor_id)
        wf, arm_flds = self._equipped_gear_fields(actor_id)
        cb = self._class_stat_bonuses(int(st.get("class_id", 1)))
        str_stat = int(st.get("base_str", 10)) + cb["str"] + int(wf.get("str_plus", 0)) + sum(int(af.get("str_plus", 0)) for af in arm_flds)
        mdef = cb["mdef"] + int(wf.get("mdef", 0)) + sum(int(af.get("mdef", 0)) for af in arm_flds)
        dex = int(st.get("base_dex", 10)) + cb["dex"] + int(wf.get("dex_plus", 0)) + sum(int(af.get("dex_plus", 0)) for af in arm_flds)
        agi = int(st.get("base_agi", 10)) + cb["agi"] + int(wf.get("agi_plus", 0)) + sum(int(af.get("agi_plus", 0)) for af in arm_flds)
        int_stat = int(st.get("base_int", 10)) + cb["int"] + int(wf.get("int_plus", 0)) + sum(int(af.get("int_plus", 0)) for af in arm_flds)
        eva = cb["eva"] + sum(int(af.get("eva", 0)) for af in arm_flds)
        return {
            "atk": self._actor_attack_power(actor_id),
            "pdef": self._actor_defense(actor_id),
            "mdef": mdef,
            "str": str_stat,
            "dex": dex,
            "agi": agi,
            "int": int_stat,
            "eva": eva,
        }

    @staticmethod
    def _evaluate_attack_roll(
        attacker_dex: int,
        skill_hit: int,
        defender_agi: int,
        defender_eva: int,
        roll_pct: int,
        is_physical: bool = True,
    ) -> Tuple[bool, bool]:
        """Deterministically computes (hit_success, is_critical) from RMXP hit/eva/dex parameters."""
        return _be_evaluate_attack_roll(attacker_dex, skill_hit, defender_agi, defender_eva, roll_pct, is_physical)

    _RMXP_ELEMENT_MULTS: Dict[int, float] = _RMXP_ELEMENT_MULTS

    def _element_multiplier(self, target_fields: Dict[str, Any], element_set: List[int]) -> float:
        """Computes strongest RMXP element_ranks multiplier (A=2.0..F=-1.0) for a skill's elements."""
        return _be_element_multiplier(target_fields, element_set)

    @staticmethod
    def _apply_state_sets(target_states: List[int], plus_ids: List[Any], minus_ids: List[Any]) -> None:
        _be_apply_state_sets(target_states, plus_ids, minus_ids)

    def _tick_states_on_combatant(self, combatant: Dict[str, Any]) -> Tuple[bool, int]:
        """Processes status slip_damage and action restrictions; returns (can_act, slip_damage_dealt)."""
        return _be_tick_states_on_combatant(combatant, self.states_db)

    def _clear_battle_only_states(self) -> None:
        for st in self.actor_states.values():
            cur = st.get("states", [])
            if not cur:
                continue
            st["states"] = [
                sid for sid in cur
                if not bool(self.states_db.get(int(sid), {}).get("fields", {}).get("battle_only", True))
            ]

    def _roll_battle_treasure_drops(self, enemies: Optional[List[Dict[str, Any]]]) -> List[str]:
        """Awards deterministic RMXP enemy item/weapon/armor drops weighted by treasure_prob."""
        if not enemies:
            return []
        dropped: List[str] = []
        for idx, foe in enumerate(enemies):
            eid = int(foe.get("enemy_id", 0) or 0)
            if eid <= 0:
                continue
            ef = self.enemies.get(eid, {}).get("fields", {})
            if not ef:
                continue
            prob = max(0, min(100, int(ef.get("treasure_prob", 100))))
            roll = (self.ticks * 17 + eid * 31 + idx * 13 + len(dropped) * 7) % 100
            if prob <= 0 or roll >= prob:
                continue
            iid = int(ef.get("item_id", 0) or 0)
            wid = int(ef.get("weapon_id", 0) or 0)
            aid = int(ef.get("armor_id", 0) or 0)
            if iid > 0 and iid in self.items:
                self._apply_inventory_delta(self.inventory, iid, "add", 1)
                dropped.append(str(self.items[iid].get("fields", {}).get("name", f"아이템#{iid}")))
            elif wid > 0 and wid in self.weapons:
                self._apply_inventory_delta(self.weapons_owned, wid, "add", 1)
                dropped.append(str(self.weapons[wid].get("fields", {}).get("name", f"무기#{wid}")))
            elif aid > 0 and aid in self.armors:
                self._apply_inventory_delta(self.armors_owned, aid, "add", 1)
                dropped.append(str(self.armors[aid].get("fields", {}).get("name", f"방어구#{aid}")))
        return dropped

    def _finish_battle_outcome(
        self,
        troop_name: str,
        total_exp: int,
        total_gold: int,
        outcome: str,
        can_escape: bool,
        can_lose: bool,
        enemies: Optional[List[Dict[str, Any]]] = None,
    ) -> str:
        self._clear_battle_only_states()
        if outcome == "win":
            self.exp += total_exp
            self.gold = min(9_999_999, self.gold + total_gold)
            for aid in self.party_member_ids:
                if int(self._get_actor_state(aid)["hp"]) > 0:
                    self._grant_actor_exp(aid, total_exp)
            drops = self._roll_battle_treasure_drops(enemies)
            drop_txt = f", 전리품: {', '.join(drops)}" if drops else ""
            self.last_battle_result = "win"
            self.log.append(f"[Battle] {troop_name} 승리! (+{total_exp} EXP, +{total_gold} G{drop_txt})")
            return "win"
        if outcome == "escape":
            if all(int(self._get_actor_state(aid)["hp"]) <= 0 for aid in self.party_member_ids):
                self.hp = max(1, self.max_hp // 10)
            self.last_battle_result = "escape"
            self.log.append(f"[Battle] {troop_name} 도주 성공!")
            return "escape"
        if can_lose:
            self.hp = max(1, self.hp)
            self.last_battle_result = "lose"
            self.log.append(f"[Battle] {troop_name} 패배 (이벤트 진행)")
            return "lose"
        if can_escape and not self.interactive_battle:
            self.hp = max(1, self.max_hp // 10)
            self.last_battle_result = "escape"
            self.log.append(f"[Battle] {troop_name} 위기 도주!")
            return "escape"
        self.hp = 0
        self.game_over = True
        self.last_battle_result = "lose"
        self.log.append(f"[Battle] {troop_name} 전멸 (Game Over)")
        return "lose"

    def _consume_best_offensive_skill(self, actor_id: Optional[int] = None) -> Tuple[int, Dict[str, Any]]:
        """Selects and pays SP for the actor's strongest affordable learned offensive skill."""
        tid = self.lead_actor_id if actor_id is None else actor_id
        st = self._get_actor_state(tid)
        best_cost = 0
        best_bonus = 0
        best_fields: Dict[str, Any] = {}
        for sid in st["learned_skills"]:
            sf = self.skills_db.get(sid, {}).get("fields", {})
            power = int(sf.get("power", 0))
            cost = max(0, int(sf.get("sp_cost", 0)))
            if power > 0 and int(st["sp"]) >= cost and power // 3 > best_bonus:
                best_bonus = power // 3
                best_cost = cost
                best_fields = sf
        if best_bonus > 0:
            st["sp"] = int(st["sp"]) - best_cost
        return best_bonus, best_fields

    def _build_battle_enemies(self, troop_id: int) -> Tuple[str, List[Dict[str, Any]]]:
        return _be_build_battle_enemies(self.troops.get(troop_id), troop_id, self.enemies)

    def _start_interactive_battle(
        self,
        troop_id: int,
        can_escape: bool = False,
        can_lose: bool = False,
        commands: Optional[List[Dict[str, Any]]] = None,
        battle_idx: int = 0,
        base_indent: int = 0,
        map_id: int = 1,
        event_id: int = 0,
        stop_after_choice_branch: Optional[int] = None,
        call_stack: Optional[List[Tuple[List[Dict[str, Any]], int, Optional[int]]]] = None,
    ) -> None:
        troop_name, enemies_inst = self._build_battle_enemies(troop_id)
        if not enemies_inst:
            self._finish_battle_outcome(troop_name, 0, 0, "win", can_escape, can_lose)
            return
        self._pre_battle_bgm = dict(self.current_bgm) if isinstance(self.current_bgm, dict) else None
        if isinstance(self.battle_bgm, dict) and self.battle_bgm.get("name"):
            self.current_bgm = dict(self.battle_bgm)
        pre_skip = False
        start_msg = f"{troop_name}이(가) 나타났다!"
        living_p = [aid for aid in self.party_member_ids if int(self._get_actor_state(aid)["hp"]) > 0]
        party_agi = sum(self.actor_full_stats(aid)["agi"] for aid in living_p) / max(1, len(living_p))
        enemy_agi = sum(int(e.get("agi", 10)) for e in enemies_inst) / max(1, len(enemies_inst))
        if party_agi >= enemy_agi * 1.5:
            pre_skip = True
            start_msg = f"{troop_name} 기선 제압! (선제 공격)"
        elif enemy_agi >= party_agi * 1.5:
            ambush_foe = max(enemies_inst, key=lambda e: int(e.get("agi", 10)))
            if living_p:
                ambush_aid = living_p[0]
                ast_t = self._get_actor_state(ambush_aid)
                amb_dmg = max(1, self.battle_formula.attack_damage(_be_enemy_formula_stats(ambush_foe), self._actor_formula_stats(ambush_aid)))
                ast_t["hp"] = max(1, int(ast_t["hp"]) - amb_dmg)
                start_msg = f"적의 기습! {ambush_foe['name']} -> {ast_t['name']} {amb_dmg} 피해"
        bstate: Dict[str, Any] = {
            "troop_id": troop_id,
            "troop_name": troop_name,
            "enemies": enemies_inst,
            "turn": 1,
            "actor_cursor": 0,
            "defending": [],
            "defending_enemies": [],
            "preemptive_skip": pre_skip,
            "fired_pages": [],
            "submenu": "main",
            "can_escape": can_escape,
            "can_lose": can_lose,
            "battle_idx": battle_idx,
            "base_indent": base_indent,
            "last_msg": start_msg,
        }
        self.pending = PendingDialogue(
            lines=[],
            choices=[],
            cancel_type=0,
            commands=commands or [],
            resume_index=battle_idx,
            map_id=map_id,
            event_id=event_id,
            stop_after_choice_branch=stop_after_choice_branch,
            call_stack=list(call_stack) if call_stack else [],
            battle_state=bstate,
        )
        self._refresh_interactive_battle_prompt(self.pending)

    def format_state_badges(self, state_ids: List[Any]) -> str:
        """Formats active RMXP state IDs as a compact bracketed badge string."""
        names = [
            str(self.states_db.get(int(sid), {}).get("fields", {}).get("name", sid))
            for sid in (state_ids or [])
        ]
        clean = [n for n in names if n]
        return f"[{','.join(clean)}]" if clean else ""

    def _battle_usable_skill_ids(self, actor_id: int) -> List[int]:
        """Returns learned skill IDs usable in battle (RMXP occasion 0=Always or 1=Only Battle)."""
        st = self._get_actor_state(actor_id)
        usable: List[int] = []
        for sid in st.get("learned_skills", []):
            sf = self.skills_db.get(int(sid), {}).get("fields", {})
            if int(sf.get("occasion", 0)) in (0, 1):
                usable.append(int(sid))
        return usable

    def get_battle_turn_order(self, bs: Dict[str, Any]) -> List[str]:
        """Returns current acting actor followed by remaining living actors and enemies sorted by AGI."""
        living_party = [aid for aid in self.party_member_ids if int(self._get_actor_state(aid)["hp"]) > 0]
        cur_idx = int(bs.get("actor_cursor", 0)) % max(1, len(living_party)) if living_party else 0
        order: List[str] = []
        if living_party:
            cur_aid = living_party[cur_idx]
            order.append(str(self._get_actor_state(cur_aid)["name"]))
            rem_actors = living_party[cur_idx + 1:]
        else:
            rem_actors = []
        pool: List[Tuple[int, int, int, str]] = []
        for idx, aid in enumerate(rem_actors):
            st = self._get_actor_state(aid)
            agi = int(self.actor_full_stats(aid)["agi"])
            pool.append((-agi, 0, idx, str(st["name"])))
        living_enemies = [e for e in bs.get("enemies", []) if int(e.get("hp", 0)) > 0 and not e.get("hidden", False)]
        for idx, foe in enumerate(living_enemies):
            agi = int(foe.get("agi", 10))
            pool.append((-agi, 1, idx, str(foe.get("name", "Enemy"))))
        pool.sort()
        order.extend(entry[3] for entry in pool)
        return order

    def _refresh_interactive_battle_prompt(self, pending: PendingDialogue) -> None:
        bs = pending.battle_state
        assert bs is not None
        living_party = [aid for aid in self.party_member_ids if int(self._get_actor_state(aid)["hp"]) > 0]
        cur_idx = int(bs["actor_cursor"]) % max(1, len(living_party))
        cur_aid = living_party[cur_idx] if living_party else self.lead_actor_id
        cur_st = self._get_actor_state(cur_aid)
        living_enemies = [e for e in bs["enemies"] if int(e["hp"]) > 0]
        foe = living_enemies[0] if living_enemies else bs["enemies"][0]
        sub = bs.get("submenu", "main")
        turn_order = self.get_battle_turn_order(bs)
        cur_badge = self.format_state_badges(cur_st.get("states", []))
        if cur_aid in bs.get("defending", []):
            cur_badge = f"{cur_badge}[방어]"
        foe_badge = self.format_state_badges(foe.get("states", []))
        order_str = " -> ".join(turn_order[:4])
        status_line = (
            f"[턴 {bs['turn']} | 순서: {order_str}] "
            f"{cur_st['name']}{cur_badge} (HP {cur_st['hp']}/{cur_st['max_hp']} SP {cur_st['sp']}/{cur_st['max_sp']}) "
            f"| 적: {foe['name']}{foe_badge} HP {foe['hp']}/{foe['max_hp']}"
        )
        pending.lines = [bs.get("last_msg", ""), status_line]
        if sub == "skill":
            skill_opts = []
            for sid in self._battle_usable_skill_ids(cur_aid):
                sf = self.skills_db.get(sid, {}).get("fields", {})
                cost = max(0, int(sf.get("sp_cost", 0)))
                lack = " [SP부족]" if int(cur_st["sp"]) < cost else ""
                skill_opts.append(f"{sf.get('name', f'스킬#{sid}')} (SP {cost}){lack}")
            skill_opts.append("뒤로")
            pending.choices = skill_opts
            pending.cancel_type = len(skill_opts)
        elif sub == "item":
            item_opts = []
            for iid in sorted(self.inventory):
                cnt = int(self.inventory.get(iid, 0))
                if cnt <= 0:
                    continue
                iname = str(self.items.get(iid, {}).get("fields", {}).get("name", f"아이템#{iid}"))
                item_opts.append(f"{iname} x{cnt}")
            item_opts.append("뒤로")
            pending.choices = item_opts
            pending.cancel_type = len(item_opts)
        elif sub == "target_enemy":
            t_opts = []
            for e in living_enemies:
                st_names = [
                    str(self.states_db.get(int(sid), {}).get("fields", {}).get("name", sid))
                    for sid in e.get("states", [])
                ]
                tag = f" [{','.join(st_names)}]" if st_names else ""
                t_opts.append(f"{e['name']} (HP {int(e['hp'])}/{int(e['max_hp'])}){tag}")
            t_opts.append("뒤로")
            pending.choices = t_opts
            pending.cancel_type = len(t_opts)
        elif sub == "target_ally":
            p_act = bs.get("pending_action") or {}
            ally_ids = list(p_act.get("eligible_allies") or self.party_member_ids)
            a_opts = [
                f"{self._get_actor_state(aid)['name']} (HP {int(self._get_actor_state(aid)['hp'])}/{int(self._get_actor_state(aid)['max_hp'])})"
                for aid in ally_ids
            ]
            a_opts.append("뒤로")
            pending.choices = a_opts
            pending.cancel_type = len(a_opts)
        else:
            pending.choices = ["공격", "스킬", "방어", "아이템", "도망"]
            pending.cancel_type = 0

    def _record_defeated_enemies(self, enemies: List[Dict[str, Any]]) -> None:
        for foe in enemies:
            eid = int(foe.get("enemy_id", 0) or 0)
            if eid > 0:
                self.defeated_enemies[eid] = self.defeated_enemies.get(eid, 0) + 1

    def get_bestiary_summary(self) -> List[str]:
        """Returns formatted defeated enemy counts for status/bestiary UI."""
        out: List[str] = []
        for eid, cnt in sorted(self.defeated_enemies.items()):
            if cnt <= 0:
                continue
            ename = str(self.enemies.get(eid, {}).get("fields", {}).get("name", f"Enemy#{eid}"))
            out.append(f"{ename} x{cnt}")
        return out

    def _conclude_interactive_battle(self, pending: PendingDialogue, outcome: str) -> None:
        bs = pending.battle_state
        assert bs is not None
        if outcome == "win":
            self._record_defeated_enemies(bs["enemies"])
            if isinstance(self.battle_end_me, dict) and self.battle_end_me.get("name"):
                self.played_me_queue.append(dict(self.battle_end_me))
        if self._pre_battle_bgm is not None or (isinstance(self.battle_bgm, dict) and self.current_bgm == self.battle_bgm):
            self.current_bgm = self._pre_battle_bgm
        self._pre_battle_bgm = None
        total_exp = sum(int(e["exp"]) for e in bs["enemies"]) if outcome == "win" else 0
        total_gold = sum(int(e["gold"]) for e in bs["enemies"]) if outcome == "win" else 0
        self._finish_battle_outcome(
            str(bs["troop_name"]),
            total_exp,
            total_gold,
            outcome,
            bool(bs["can_escape"]),
            bool(bs["can_lose"]),
            enemies=bs["enemies"],
        )
        self.pending = None
        if self.game_over or not pending.commands:
            return
        branch_idx = self._jump_battle_branch(pending.commands, int(bs["battle_idx"]), int(bs["base_indent"]), outcome)
        resume_from = branch_idx if branch_idx is not None else (int(bs["battle_idx"]) + 1)
        self._run_commands(
            pending.commands,
            resume_from,
            pending.map_id,
            pending.event_id,
            stop_after_choice_branch=pending.stop_after_choice_branch,
            call_stack=pending.call_stack,
        )

    def _advance_interactive_turn_or_enemy_phase(self, pending: PendingDialogue, msg: str) -> None:
        bs = pending.battle_state
        assert bs is not None
        bs["last_msg"] = msg
        bs["submenu"] = "main"
        living_enemies = [e for e in bs["enemies"] if int(e["hp"]) > 0 and not e.get("hidden", False)]
        if not living_enemies:
            self._conclude_interactive_battle(pending, "win")
            return
        living_party = [aid for aid in self.party_member_ids if int(self._get_actor_state(aid)["hp"]) > 0]
        bs["actor_cursor"] = int(bs["actor_cursor"]) + 1
        if bs["actor_cursor"] < len(living_party):
            self._refresh_interactive_battle_prompt(pending)
            return
        # Enemy phase (sorted by AGI descending, with Preemptive Skip & AI Skills)
        bs["actor_cursor"] = 0
        defending = set(bs.get("defending", []))
        bs["defending"] = []
        bs["defending_enemies"] = []
        enemy_logs: List[str] = []
        if bs.pop("preemptive_skip", False):
            enemy_logs.append("선제 공격으로 적들이 아직 태세를 갖추지 못했다!")
        else:
            turn_cursor = int(bs["turn"])
            ordered_enemies = sorted(
                enumerate(living_enemies),
                key=lambda pair: (-int(pair[1].get("agi", 10)), pair[0]),
            )
            for _idx, enemy in ordered_enemies:
                can_act, slip = self._tick_states_on_combatant(enemy)
                if slip > 0:
                    enemy_logs.append(f"{enemy['name']} 상태이상 피해 {slip}")
                if not can_act:
                    enemy_logs.append(f"{enemy['name']} 행동 불가!")
                    continue
                living_party = [aid for aid in self.party_member_ids if int(self._get_actor_state(aid)["hp"]) > 0]
                if not living_party:
                    break
                act_log = self._execute_enemy_turn_action(enemy, bs, defending, living_party, turn_cursor)
                turn_cursor += 1
                if act_log:
                    enemy_logs.append(act_log)
            for fa in bs.pop("forced_actions", []):
                fa_idx = int(fa.get("target_index", 0))
                if int(fa.get("target_type", 0)) == 0 and 0 <= fa_idx < len(bs["enemies"]):
                    fe = bs["enemies"][fa_idx]
                    if int(fe.get("hp", 0)) > 0 and living_party:
                        act_log = self._execute_enemy_turn_action(fe, bs, defending, living_party, turn_cursor)
                        if act_log:
                            enemy_logs.append(f"[강제] {act_log}")
        for aid in list(self.party_member_ids):
            ast = self._get_actor_state(aid)
            if int(ast["hp"]) > 0:
                _ok, p_slip = self._tick_states_on_combatant(ast)
                if p_slip > 0:
                    enemy_logs.append(f"{ast['name']} 상태이상 피해 {p_slip}")
        if all(int(self._get_actor_state(aid)["hp"]) <= 0 for aid in self.party_member_ids):
            self._conclude_interactive_battle(pending, "lose")
            return
        bs["turn"] = int(bs["turn"]) + 1
        troop_note = self._evaluate_battle_troop_events(bs)
        if bs.get("aborted"):
            self._conclude_interactive_battle(pending, "escape")
            return
        if all(int(e.get("hp", 0)) <= 0 for e in bs.get("enemies", [])):
            self._conclude_interactive_battle(pending, "win")
            return
        if troop_note:
            bs["last_msg"] = f"{msg} / {troop_note}"
        elif enemy_logs:
            bs["last_msg"] = f"{msg} / {enemy_logs[0]}"
        self._refresh_interactive_battle_prompt(pending)

    def _select_enemy_action(self, enemy: Dict[str, Any], turn: int) -> Optional[Dict[str, Any]]:
        """Selects highest-rating eligible RMXP Enemy::Action matching turn/HP conditions."""
        return _be_select_enemy_action(enemy, turn)

    def _actor_magic_defense(self, actor_id: int) -> int:
        return int(self.actor_full_stats(actor_id)["mdef"])

    def _actor_formula_stats(self, actor_id: int) -> Dict[str, int]:
        """Canonical RMXP-semantics stats of an actor for the project's BattleFormula."""
        st = self._get_actor_state(actor_id)
        full = self.actor_full_stats(actor_id)
        wf, arm_flds = self._equipped_gear_fields(actor_id)
        cb = self._class_stat_bonuses(int(st.get("class_id", 1)))
        gear_pdef = cb["pdef"] + int(wf.get("pdef", 0)) + sum(int(af.get("pdef", 0)) for af in arm_flds)
        return _bf_combatant_stats(
            is_actor=1,
            level=int(st.get("level", 1)),
            hp=int(st.get("hp", 1)),
            mhp=int(st.get("max_hp", 1)),
            sp=int(st.get("sp", 0)),
            msp=int(st.get("max_sp", 0)),
            atk=int(wf.get("atk", 0)) + cb["atk"],
            str=full["str"],
            dex=full["dex"],
            agi=full["agi"],
            int=full["int"],
            pdef=gear_pdef,
            mdef=full["mdef"],
            eva=full["eva"],
            legacy_atk=self._actor_attack_power(actor_id),
            legacy_def=self._actor_defense(actor_id),
        )

    def _actor_guard_elements(self, actor_id: int) -> List[int]:
        _wf, arm_flds = self._equipped_gear_fields(actor_id)
        elems: List[int] = []
        for af in arm_flds:
            for eid in af.get("guard_element_set", []):
                ie = int(eid)
                if ie > 0 and ie not in elems:
                    elems.append(ie)
        return elems

    def _actor_guard_states(self, actor_id: int) -> List[int]:
        _wf, arm_flds = self._equipped_gear_fields(actor_id)
        states: List[int] = []
        for af in arm_flds:
            for sid in af.get("guard_state_set", []):
                isid = int(sid)
                if isid > 0 and isid not in states:
                    states.append(isid)
        return states

    def _battle_anim_cue(self, target_type: int, target_index: int, animation_id: int) -> Dict[str, Any]:
        """Builds a battle animation cue whose lifetime covers every frame of the animation (RMXP 20fps)."""
        entry = self.animations_db.get(int(animation_id), {})
        frames_list = entry.get("fields", {}).get("frames", []) if isinstance(entry, dict) else []
        n_frames = len(frames_list) if isinstance(frames_list, list) else 0
        total = max(24, n_frames * ANIMATION_TICKS_PER_FRAME)
        return {
            "target_type": int(target_type),
            "target_index": int(target_index),
            "animation_id": int(animation_id),
            "frames": total,
            "total_frames": total,
        }

    def _queue_battle_media(self, fields: Dict[str, Any], target_type: int, target_index: int) -> None:
        anim2 = int(fields.get("animation2_id", 0) or 0)
        if anim2 > 0:
            self.pending_battle_animations.append(
                self._battle_anim_cue(target_type, target_index, anim2)
            )
        mse = fields.get("menu_se")
        if isinstance(mse, dict):
            mf = mse.get("fields", mse)
            if mf.get("name"):
                self.played_se_queue.append(
                    {"name": str(mf["name"]), "volume": int(mf.get("volume", 80)), "pitch": int(mf.get("pitch", 100))}
                )

    def _compute_skill_raw_damage(
        self, cur_aid: int, sf: Dict[str, Any], foe: Dict[str, Any], seed_val: Optional[int] = None
    ) -> int:
        raw = self.battle_formula.skill_damage(self._actor_formula_stats(cur_aid), _be_enemy_formula_stats(foe), sf)
        var = self.battle_formula.skill_variance_for(sf)
        if seed_val is not None and var > 0:
            raw = _be_apply_variance(raw, var, seed_val)
        return raw

    def _execute_enemy_turn_action(
        self,
        enemy: Dict[str, Any],
        bs: Dict[str, Any],
        defending: set,
        living_party: List[int],
        turn_cursor: int,
    ) -> str:
        return _be_execute_enemy_turn_action(
            enemy,
            bs,
            defending,
            living_party,
            turn_cursor,
            self.skills_db,
            self._get_actor_state,
            self._actor_defense,
            self._actor_magic_defense,
            self.switches,
            self.states_db,
            get_actor_guard_elements=self._actor_guard_elements,
            get_actor_guard_states=self._actor_guard_states,
            formula=self.battle_formula,
            get_actor_formula_stats=self._actor_formula_stats,
        )

    def _execute_safe_script(self, lines: List[str]) -> None:
        """Executes safe RGSS script assignments ($game_switches, $game_variables, gain_gold, self_switches)."""
        for raw in lines:
            stmt = str(raw).strip()
            if not stmt or stmt.startswith("#"):
                continue
            m_sw = re.match(r"^\$game_switches\[(\d+)\]\s*=\s*(true|false)\s*$", stmt, re.IGNORECASE)
            if m_sw:
                self.switches[int(m_sw.group(1))] = m_sw.group(2).lower() == "true"
                continue
            m_var = re.match(r"^\$game_variables\[(\d+)\]\s*=\s*(.+)$", stmt)
            if m_var:
                vid = int(m_var.group(1))
                try:
                    self.variables[vid] = _safe_eval_arithmetic(m_var.group(2), self.variables)
                except Exception as exc:
                    self.log.append(f"[Script] 무시된 표현식: {stmt} ({exc})")
                continue
            m_gold = re.match(r"^\$game_party\.gain_gold\((.+)\)\s*$", stmt)
            if m_gold:
                try:
                    delta = _safe_eval_arithmetic(m_gold.group(1), self.variables)
                    self.gold = min(9_999_999, max(0, self.gold + delta))
                except Exception as exc:
                    self.log.append(f"[Script] 무시된 골드 식: {stmt} ({exc})")
                continue
            m_ssw = re.match(r"^\$game_self_switches\[\[\s*(\d+)\s*,\s*(\d+)\s*,\s*['\"]([A-D])['\"]\s*\]\]\s*=\s*(true|false)\s*$", stmt, re.IGNORECASE)
            if m_ssw:
                key = (int(m_ssw.group(1)), int(m_ssw.group(2)), m_ssw.group(3).upper())
                self.self_switches[key] = m_ssw.group(4).lower() == "true"
                continue
            self.log.append(f"[Script] 실행 기록: {stmt}")

    @staticmethod
    def _record_battle_popup(bs: Optional[Dict[str, Any]], target_name: str, amount: int, kind: str = "damage") -> None:
        if not isinstance(bs, dict):
            return
        bs.setdefault("floating_popups", []).append({
            "target": target_name,
            "text": f"+{amount}" if kind == "heal" else str(amount),
            "kind": kind,
            "frames": 36,
        })

    def _apply_battle_event_op(
        self,
        op: str,
        cmd: Dict[str, Any],
        bs: Optional[Dict[str, Any]],
    ) -> str:
        """Executes RMXP battle event commands 335..339 cleanly inside or outside active battle."""
        enemies = bs.get("enemies", []) if isinstance(bs, dict) else []
        e_idx = int(cmd.get("enemy_index", 0))
        if op == "enemy_appear":
            if 0 <= e_idx < len(enemies):
                enemies[e_idx]["hidden"] = False
                return f"{enemies[e_idx]['name']} 출현!"
            return "[Battle] 적 출현"
        if op == "enemy_transform":
            new_eid = int(cmd.get("enemy_id", 1))
            if 0 <= e_idx < len(enemies):
                repl = _be_build_single_enemy(new_eid, self.enemies, hidden=bool(enemies[e_idx].get("hidden", False)))
                if repl is not None:
                    old_name = enemies[e_idx]["name"]
                    enemies[e_idx].update(repl)
                    return f"{old_name} -> {repl['name']} 변신!"
            return f"[Battle] 적 변신 -> Enemy#{new_eid}"
        if op == "show_battle_animation":
            ttype = int(cmd.get("target_type", 0))
            tidx = int(cmd.get("target_index", 0))
            aid = int(cmd.get("animation_id", 1))
            self.pending_battle_animations.append(self._battle_anim_cue(ttype, tidx, aid))
            return f"[BattleAnim] target=({ttype},{tidx}) anim=#{aid}"
        if op == "deal_damage":
            ttype = int(cmd.get("target_type", 0))
            tidx = int(cmd.get("target_index", 0))
            amt = max(0, int(cmd.get("value", 0)))
            if ttype == 0 and 0 <= tidx < len(enemies):
                enemies[tidx]["hp"] = max(0, int(enemies[tidx]["hp"]) - amt)
                self._record_battle_popup(bs, enemies[tidx]["name"], amt, "damage")
                return f"{enemies[tidx]['name']}에게 {amt} 추가 피해!"
            aid = self.party_member_ids[tidx] if 0 <= tidx < len(self.party_member_ids) else self.lead_actor_id
            ast = self._get_actor_state(aid)
            ast["hp"] = max(0, int(ast["hp"]) - amt)
            self._record_battle_popup(bs, ast["name"], amt, "damage")
            return f"{ast['name']}에게 {amt} 피해!"
        if op == "force_action":
            ttype = int(cmd.get("target_type", 0))
            tidx = int(cmd.get("target_index", 0))
            kind = int(cmd.get("kind", 0))
            sid = int(cmd.get("skill_id", 0))
            if isinstance(bs, dict):
                bs.setdefault("forced_actions", []).append({
                    "target_type": ttype,
                    "target_index": tidx,
                    "kind": kind,
                    "skill_id": sid,
                })
            return f"[ForceAction] kind={kind} skill={sid}"
        return ""

    def _evaluate_battle_troop_events(self, bs: Dict[str, Any]) -> str:
        """Evaluates RMXP Troop Battle Event pages matching current turn/HP/switch conditions."""
        troop = self.troops.get(int(bs.get("troop_id", 0)), {})
        pages = troop.get("fields", {}).get("pages", [])
        if not pages:
            return ""
        turn = int(bs.get("turn", 1))
        fired = set(bs.get("fired_pages", []))
        note = ""
        for idx, raw_pg in enumerate(pages):
            pf = raw_pg.get("fields", raw_pg) if isinstance(raw_pg, dict) else {}
            span = int(pf.get("span", 0))
            if span == 0 and idx in fired:
                continue
            cond = pf.get("condition", {})
            cf = cond.get("fields", cond) if isinstance(cond, dict) else {}
            if cf.get("turn_valid"):
                ta = int(cf.get("turn_a", 0))
                tb = int(cf.get("turn_b", 0))
                if tb == 0 and turn != ta:
                    continue
                if tb > 0 and (turn < ta or (turn - ta) % tb != 0):
                    continue
            if cf.get("enemy_valid"):
                e_idx = int(cf.get("enemy_index", 0))
                e_hp = int(cf.get("enemy_hp", 50))
                enemies = bs.get("enemies", [])
                if e_idx >= len(enemies):
                    continue
                e_cur = enemies[e_idx]
                pct = (int(e_cur["hp"]) * 100) // max(1, int(e_cur["max_hp"]))
                if pct > e_hp:
                    continue
            if cf.get("switch_valid") and not self.switches.get(int(cf.get("switch_id", 0)), False):
                continue
            raw_cmds = pf.get("list", pf.get("commands", []))
            if not raw_cmds:
                continue
            prev_pending = self.pending
            log_before = len(self.log)
            for c in raw_cmds:
                if not isinstance(c, dict):
                    continue
                cop = c.get("op")
                if cop in ("set_switch", "set_variable", "change_hp", "change_sp", "change_state"):
                    self._run_commands([c], 0, self.map_id, 0)
                elif cop == "say":
                    lines = self._format_dialogue_lines([str(x) for x in c.get("lines", [])])
                    note = lines[0] if lines else note
                elif cop in ("change_enemy_hp", "change_enemy_sp", "change_enemy_state", "enemy_recover_all"):
                    e_idx = int(c.get("enemy_index", -1))
                    enemies = bs.get("enemies", [])
                    targets = enemies if e_idx < 0 else ([enemies[e_idx]] if e_idx < len(enemies) else [])
                    for foe in targets:
                        if cop == "change_enemy_hp":
                            amt = self._resolve_cmd_amount(c)
                            signed = amt if c.get("mode", "add") == "add" else -amt
                            floor_hp = 0 if c.get("allow_knockout", False) else 1
                            foe["hp"] = max(floor_hp, min(int(foe["max_hp"]), int(foe["hp"]) + signed))
                        elif cop == "change_enemy_sp":
                            amt = self._resolve_cmd_amount(c)
                            signed = amt if c.get("mode", "add") == "add" else -amt
                            max_sp = int(foe.get("max_sp", foe.get("maxsp", 100)))
                            foe["sp"] = max(0, min(max_sp, int(foe.get("sp", max_sp)) + signed))
                        elif cop == "change_enemy_state":
                            sid = int(c.get("state_id", 0))
                            states = foe.setdefault("states", [])
                            if c.get("mode", "add") == "add":
                                self._apply_state_sets(states, [sid], [])
                            else:
                                self._apply_state_sets(states, [], [sid])
                        elif cop == "enemy_recover_all":
                            foe["hp"] = int(foe["max_hp"])
                            foe["sp"] = int(foe.get("max_sp", foe.get("maxsp", foe.get("sp", 100))))
                            foe["states"] = []
                            foe["state_turns"] = {}
                elif cop in ("enemy_appear", "enemy_transform", "show_battle_animation", "deal_damage", "force_action"):
                    f_note = self._apply_battle_event_op(str(cop), c, bs)
                    if f_note:
                        note = f_note
                elif cop == "abort_battle":
                    bs["aborted"] = True
                    note = "전투 중단!"
            self.pending = prev_pending
            if len(self.log) > log_before and not note:
                note = self.log[-1]
            fired.add(idx)
        bs["fired_pages"] = sorted(fired)
        return note

    def _battle_eligible_allies(self, allow_dead: bool = False) -> List[int]:
        if allow_dead:
            return [int(aid) for aid in self.party_member_ids]
        return [int(aid) for aid in self.party_member_ids if int(self._get_actor_state(aid)["hp"]) > 0]

    def _apply_ally_skill_recovery(self, caster_st: Dict[str, Any], sf: Dict[str, Any], target_ids: List[int]) -> str:
        power = int(sf.get("power", 0))
        sname = str(sf.get("name", "회복"))
        heal = max(20, abs(power) * 2)
        names: List[str] = []
        minus_states = sf.get("minus_state_set", [])
        allow_dead = int(sf.get("scope", 3)) in (5, 6) or 1 in [int(x) for x in minus_states]
        for tid in target_ids:
            tst = self._get_actor_state(tid)
            if int(tst["hp"]) <= 0 and not allow_dead:
                continue
            tst["hp"] = min(int(tst["max_hp"]), max(1 if allow_dead else 0, int(tst["hp"])) + heal)
            self._apply_state_sets(tst.setdefault("states", []), [], minus_states)
            names.append(str(tst["name"]))
        who = ", ".join(names) if names else caster_st["name"]
        return f"{caster_st['name']}의 {sname}! {who} HP +{heal}"

    def _execute_battle_skill_on_targets(
        self,
        pending: PendingDialogue,
        cur_aid: int,
        sid: int,
        target_enemy: Dict[str, Any],
        target_ally_id: Optional[int] = None,
    ) -> None:
        bs = pending.battle_state
        assert bs is not None
        cur_st = self._get_actor_state(cur_aid)
        sf = self.skills_db.get(sid, {}).get("fields", {})
        if int(sf.get("occasion", 0)) not in (0, 1):
            bs["last_msg"] = f"{sf.get('name', sid)}은(는) 전투 중 사용할 수 없습니다."
            bs["submenu"] = "skill"
            self._refresh_interactive_battle_prompt(pending)
            return
        cost = max(0, int(sf.get("sp_cost", 0)))
        if int(cur_st["sp"]) < cost:
            bs["last_msg"] = f"SP가 부족합니다! ({sf.get('name', sid)} SP {cost} 필요)"
            bs["submenu"] = "skill"
            self._refresh_interactive_battle_prompt(pending)
            return
        cur_st["sp"] = int(cur_st["sp"]) - cost
        power = int(sf.get("power", 0))
        scope = int(sf.get("scope", 1))
        sname = str(sf.get("name", f"스킬#{sid}"))
        living_enemies = [e for e in bs["enemies"] if int(e["hp"]) > 0]
        if scope in (3, 4, 5, 6) or power < 0:
            if scope in (4, 6):
                tids = self._battle_eligible_allies(allow_dead=(scope == 6))
            else:
                tids = [int(target_ally_id) if target_ally_id is not None else cur_aid]
            self._queue_battle_media(sf, 1, 0)
            msg = self._apply_ally_skill_recovery(cur_st, sf, tids)
            self._advance_interactive_turn_or_enemy_phase(pending, msg)
        elif scope == 2:
            self._queue_battle_media(sf, 0, 0)
            total_dmg = 0
            weak_hit = False
            for foe in living_enemies:
                mult = self._element_multiplier(foe, sf.get("element_set", []))
                if mult >= 1.5:
                    weak_hit = True
                raw_dmg = self._compute_skill_raw_damage(cur_aid, sf, foe)
                dmg = int(round(raw_dmg * mult))
                foe["hp"] = max(0, min(int(foe["max_hp"]), int(foe["hp"]) - dmg))
                self._apply_state_sets(foe["states"], sf.get("plus_state_set", []), sf.get("minus_state_set", []))
                total_dmg += dmg
            bs["last_eff_tag"] = "약점!" if weak_hit else ""
            aoe_note = " (약점 포함!)" if weak_hit else ""
            self._advance_interactive_turn_or_enemy_phase(pending, f"{cur_st['name']} {sname}! 적 전체 {total_dmg} 피해{aoe_note}")
        else:
            enemy_idx = next((idx for idx, e in enumerate(bs["enemies"]) if e is target_enemy), 0)
            self._queue_battle_media(sf, 0, enemy_idx)
            ast = self.actor_full_stats(cur_aid)
            roll = (int(bs.get("turn", 1)) * 37 + int(cur_aid) * 13 + len(living_enemies)) % 100
            hit_ok, is_crit = self._evaluate_attack_roll(
                ast["dex"],
                int(sf.get("hit", 100)),
                int(target_enemy.get("agi", 10)),
                int(target_enemy.get("eva", 0)),
                roll,
                is_physical=(int(sf.get("atk_f", 100)) > 0 and int(sf.get("int_f", 0)) == 0),
            )
            if not hit_ok:
                bs["last_eff_tag"] = "MISS"
                self._advance_interactive_turn_or_enemy_phase(pending, f"{cur_st['name']} {sname}! 빗나갔다!")
                return
            mult = self._element_multiplier(target_enemy, sf.get("element_set", []))
            raw_dmg = self._compute_skill_raw_damage(cur_aid, sf, target_enemy)
            dmg = int(round(raw_dmg * mult))
            if is_crit:
                dmg = self.battle_formula.apply_critical(dmg)
            target_enemy["hp"] = max(0, min(int(target_enemy["max_hp"]), int(target_enemy["hp"]) - dmg))
            self._apply_state_sets(target_enemy["states"], sf.get("plus_state_set", []), sf.get("minus_state_set", []))
            crit_note = " (치명타!)" if is_crit else ""
            eff_tag = (
                "약점!" if mult >= 1.5
                else ("무효" if mult == 0.0
                else ("반감" if 0.0 < mult < 1.0
                else ("흡수" if mult < 0 else "")))
            )
            bs["last_eff_tag"] = eff_tag or ("치명타!" if is_crit else "")
            self._record_battle_popup(bs, str(target_enemy["name"]), dmg, "heal" if dmg < 0 else "damage")
            note = f" ({eff_tag})" if eff_tag else ""
            self._advance_interactive_turn_or_enemy_phase(pending, f"{cur_st['name']} {sname}! {target_enemy['name']} {dmg} 피해{crit_note}{note}")

    def _apply_physical_attack(self, pending: PendingDialogue, cur_aid: int, target_enemy: Dict[str, Any]) -> None:
        bs = pending.battle_state
        assert bs is not None
        cur_st = self._get_actor_state(cur_aid)
        ast = self.actor_full_stats(cur_aid)
        wf, _arm = self._equipped_gear_fields(cur_aid)
        enemy_idx = next((idx for idx, e in enumerate(bs["enemies"]) if e is target_enemy), 0)
        self._queue_battle_media(wf, 0, enemy_idx)
        roll = (int(bs.get("turn", 1)) * 37 + int(cur_aid) * 13 + len(bs.get("enemies", []))) % 100
        hit_ok, is_crit = self._evaluate_attack_roll(
            ast["dex"],
            100,
            int(target_enemy.get("agi", 10)),
            int(target_enemy.get("eva", 0)),
            roll,
            is_physical=True,
        )
        if not hit_ok:
            bs["last_eff_tag"] = "MISS"
            self._record_battle_popup(bs, str(target_enemy["name"]), 0, "miss")
            self._advance_interactive_turn_or_enemy_phase(pending, f"{cur_st['name']} 공격! 빗나갔다! (Miss)")
            return
        w_elems = wf.get("element_set", [])
        mult = self._element_multiplier(target_enemy, w_elems) if w_elems else 1.0
        base_dmg = self.battle_formula.attack_damage(self._actor_formula_stats(cur_aid), _be_enemy_formula_stats(target_enemy))
        dmg = int(round(base_dmg * mult))
        if is_crit:
            dmg = self.battle_formula.apply_critical(dmg)
        dmg = self.battle_formula.vary(dmg, self.battle_formula.attack_variance, roll + int(bs.get("turn", 1)) * 101)
        target_enemy["hp"] = max(0, min(int(target_enemy["max_hp"]), int(target_enemy["hp"]) - dmg))
        self._record_battle_popup(bs, str(target_enemy["name"]), dmg, "crit" if is_crit else "damage")
        if dmg > 0:
            _be_shock_release_states(target_enemy, self.states_db, roll_pct=roll)
            w_plus = wf.get("plus_state_set", [])
            w_minus = wf.get("minus_state_set", [])
            if w_plus or w_minus:
                self._apply_state_sets(target_enemy.setdefault("states", []), w_plus, w_minus)
        crit_note = " (치명타!)" if is_crit else ""
        bs["last_eff_tag"] = "치명타!" if is_crit else ""
        self._advance_interactive_turn_or_enemy_phase(pending, f"{cur_st['name']} 공격! {target_enemy['name']}에게 {dmg} 피해{crit_note}")

    def _resolve_battle_choice(self, pending: PendingDialogue, choice_idx: int) -> None:
        bs = pending.battle_state
        assert bs is not None
        living_party = [aid for aid in self.party_member_ids if int(self._get_actor_state(aid)["hp"]) > 0]
        if not living_party:
            self._conclude_interactive_battle(pending, "lose")
            return
        cur_aid = living_party[int(bs["actor_cursor"]) % len(living_party)]
        cur_st = self._get_actor_state(cur_aid)
        living_enemies = [e for e in bs["enemies"] if int(e["hp"]) > 0 and not e.get("hidden", False)]
        if not living_enemies:
            self._conclude_interactive_battle(pending, "win")
            return
        target_enemy = living_enemies[0]
        sub = bs.get("submenu", "main")

        if sub == "target_enemy":
            if choice_idx < 0 or choice_idx >= len(living_enemies):
                bs["pending_action"] = None
                bs["submenu"] = "main"
                self._refresh_interactive_battle_prompt(pending)
                return
            chosen_enemy = living_enemies[choice_idx]
            p_act = bs.get("pending_action") or {}
            bs["pending_action"] = None
            if p_act.get("kind") == "skill":
                self._execute_battle_skill_on_targets(pending, cur_aid, int(p_act["id"]), chosen_enemy)
            else:
                self._apply_physical_attack(pending, cur_aid, chosen_enemy)
            return

        if sub == "target_ally":
            p_act = bs.get("pending_action") or {}
            ally_ids = list(p_act.get("eligible_allies") or self.party_member_ids)
            if choice_idx < 0 or choice_idx >= len(ally_ids):
                bs["pending_action"] = None
                bs["submenu"] = "main"
                self._refresh_interactive_battle_prompt(pending)
                return
            chosen_ally_id = int(ally_ids[choice_idx])
            bs["pending_action"] = None
            if p_act.get("kind") == "skill":
                self._execute_battle_skill_on_targets(pending, cur_aid, int(p_act["id"]), target_enemy, target_ally_id=chosen_ally_id)
            elif p_act.get("kind") == "item":
                iid = int(p_act["id"])
                it_flds = self.items.get(iid, {}).get("fields", {})
                iname = str(it_flds.get("name", f"아이템#{iid}"))
                if self._use_healing_item(iid, actor_id=chosen_ally_id):
                    anim2 = int(it_flds.get("animation2_id", 0) or 0)
                    if anim2 > 0:
                        self.pending_battle_animations.append(self._battle_anim_cue(1, 0, anim2))
                    tname = self._get_actor_state(chosen_ally_id)["name"]
                    self._advance_interactive_turn_or_enemy_phase(pending, f"{cur_st['name']} -> {tname}에게 {iname} 사용!")
                else:
                    bs["last_msg"] = f"{iname}은(는) 지금 사용할 수 없습니다."
                    bs["submenu"] = "item"
                    self._refresh_interactive_battle_prompt(pending)
            return

        if sub == "skill":
            skills = self._battle_usable_skill_ids(cur_aid)
            if choice_idx < 0 or choice_idx >= len(skills):
                bs["submenu"] = "main"
                self._refresh_interactive_battle_prompt(pending)
                return
            sid = int(skills[choice_idx])
            sf = self.skills_db.get(sid, {}).get("fields", {})
            cost = max(0, int(sf.get("sp_cost", 0)))
            if int(cur_st["sp"]) < cost:
                bs["last_msg"] = f"SP가 부족합니다! ({sf.get('name', sid)} SP {cost} 필요)"
                self._refresh_interactive_battle_prompt(pending)
                return
            power = int(sf.get("power", 0))
            scope = int(sf.get("scope", 1))
            if scope in (3, 5) or (power < 0 and scope not in (4, 6)):
                allow_dead = scope == 5 or 1 in [int(x) for x in sf.get("minus_state_set", [])]
                eligible = self._battle_eligible_allies(allow_dead=allow_dead)
                if len(eligible) > 1:
                    bs["pending_action"] = {"kind": "skill", "id": sid, "eligible_allies": eligible}
                    bs["submenu"] = "target_ally"
                    self._refresh_interactive_battle_prompt(pending)
                    return
                self._execute_battle_skill_on_targets(pending, cur_aid, sid, target_enemy, target_ally_id=(eligible[0] if eligible else cur_aid))
                return
            if scope == 1 and len(living_enemies) > 1:
                bs["pending_action"] = {"kind": "skill", "id": sid}
                bs["submenu"] = "target_enemy"
                self._refresh_interactive_battle_prompt(pending)
                return
            self._execute_battle_skill_on_targets(pending, cur_aid, sid, target_enemy)
            return

        if sub == "item":
            owned_ids = [iid for iid in sorted(self.inventory) if int(self.inventory.get(iid, 0)) > 0]
            if choice_idx < 0 or choice_idx >= len(owned_ids):
                bs["submenu"] = "main"
                self._refresh_interactive_battle_prompt(pending)
                return
            iid = owned_ids[choice_idx]
            it_flds = self.items.get(iid, {}).get("fields", {})
            iname = str(it_flds.get("name", f"아이템#{iid}"))
            if len(self.party_member_ids) > 1:
                allow_dead = int(it_flds.get("scope", 3)) in (5, 6) or 1 in [int(x) for x in it_flds.get("minus_state_set", [])]
                eligible = self._battle_eligible_allies(allow_dead=allow_dead)
                if len(eligible) > 1:
                    bs["pending_action"] = {"kind": "item", "id": iid, "eligible_allies": eligible}
                    bs["submenu"] = "target_ally"
                    self._refresh_interactive_battle_prompt(pending)
                    return
            if self._use_healing_item(iid, actor_id=cur_aid):
                anim2 = int(it_flds.get("animation2_id", 0) or 0)
                if anim2 > 0:
                    self.pending_battle_animations.append(self._battle_anim_cue(1, 0, anim2))
                self._advance_interactive_turn_or_enemy_phase(pending, f"{cur_st['name']} {iname} 사용!")
            else:
                bs["last_msg"] = f"{iname}은(는) 지금 사용할 수 없습니다."
                self._refresh_interactive_battle_prompt(pending)
            return

        restriction = _be_get_restriction(cur_st, self.states_db)
        can_act, _slip = self._tick_states_on_combatant(cur_st)
        if not can_act and choice_idx != 4:
            self._advance_interactive_turn_or_enemy_phase(pending, f"{cur_st['name']}은(는) 움직일 수 없다!")
            return
        if restriction == 2 and choice_idx != 4:
            foe_auto = living_enemies[int(bs.get("turn", 1)) % len(living_enemies)]
            self._apply_physical_attack(pending, cur_aid, foe_auto)
            return
        if restriction == 3 and choice_idx != 4:
            ally_aid = living_party[int(bs.get("turn", 1)) % len(living_party)]
            ally_st = self._get_actor_state(ally_aid)
            admg = max(1, self.battle_formula.attack_damage(self._actor_formula_stats(cur_aid), self._actor_formula_stats(ally_aid)))
            ally_st["hp"] = max(0, int(ally_st["hp"]) - admg)
            self._record_battle_popup(bs, ally_st["name"], admg, "damage")
            self._advance_interactive_turn_or_enemy_phase(pending, f"{cur_st['name']} 혼란! {ally_st['name']}에게 {admg} 피해")
            return
        if choice_idx == 0:  # 공격
            if len(living_enemies) > 1:
                bs["pending_action"] = {"kind": "attack", "id": 0}
                bs["submenu"] = "target_enemy"
                self._refresh_interactive_battle_prompt(pending)
                return
            self._apply_physical_attack(pending, cur_aid, target_enemy)
        elif choice_idx == 1:  # 스킬
            if restriction == 1:
                bs["last_msg"] = f"{cur_st['name']}은(는) 침묵 상태로 마법을 사용할 수 없다!"
                self._refresh_interactive_battle_prompt(pending)
                return
            bs["submenu"] = "skill"
            self._refresh_interactive_battle_prompt(pending)
        elif choice_idx == 2:  # 방어
            bs.setdefault("defending", []).append(cur_aid)
            cur_st["sp"] = min(int(cur_st["max_sp"]), int(cur_st["sp"]) + 2)
            self._advance_interactive_turn_or_enemy_phase(pending, f"{cur_st['name']} 방어 태세! (피해 반감, SP +2)")
        elif choice_idx == 3:  # 아이템
            bs["submenu"] = "item"
            self._refresh_interactive_battle_prompt(pending)
        elif choice_idx == 4:  # 도망
            if bool(bs.get("can_escape", False)):
                p_agi = sum(self.actor_full_stats(aid)["agi"] for aid in living_party) // max(1, len(living_party))
                e_agi = sum(int(e.get("agi", 10)) for e in living_enemies) // max(1, len(living_enemies))
                esc_chance = max(15, min(100, 60 + (p_agi - e_agi) * 2))
                esc_roll = (int(bs.get("turn", 1)) * 29 + cur_aid * 17) % 100
                if p_agi * 2 >= e_agi or esc_roll < esc_chance:
                    self._conclude_interactive_battle(pending, "escape")
                else:
                    self._advance_interactive_turn_or_enemy_phase(pending, "도망에 실패했다!")
            else:
                bs["last_msg"] = "이 전투에서는 도망칠 수 없다!"
                self._refresh_interactive_battle_prompt(pending)

    def _resolve_battle(self, troop_id: int, can_escape: bool = False, can_lose: bool = False) -> str:
        troop_name, enemies_inst = self._build_battle_enemies(troop_id)
        if not enemies_inst:
            return self._finish_battle_outcome(troop_name, 0, 0, "win", can_escape, can_lose)
        total_exp = 0
        total_gold = 0
        turn_cursor = 0
        bs_sim: Dict[str, Any] = {"troop_id": troop_id, "troop_name": troop_name, "enemies": enemies_inst, "turn": 1, "defending_enemies": []}
        for enemy in enemies_inst:
            while int(enemy["hp"]) > 0:
                living_ids = [aid for aid in self.party_member_ids if int(self._get_actor_state(aid)["hp"]) > 0]
                if not living_ids:
                    break
                for aid in living_ids:
                    ast = self._get_actor_state(aid)
                    can_act, _slip = self._tick_states_on_combatant(ast)
                    if not can_act:
                        continue
                    self._auto_use_potion_if_low(aid)
                    skill_bonus, sfields = self._consume_best_offensive_skill(aid)
                    stats = self.actor_full_stats(aid)
                    wf, _arm = self._equipped_gear_fields(aid)
                    roll = (int(bs_sim["turn"]) * 37 + int(aid) * 13 + len(enemies_inst)) % 100
                    is_phys = not (sfields and int(sfields.get("int_f", 0)) > 0 and int(sfields.get("atk_f", 0)) == 0)
                    hit_val = int(sfields.get("hit", 100)) if sfields else 100
                    hit_ok, is_crit = self._evaluate_attack_roll(
                        stats["dex"], hit_val, int(enemy.get("agi", 10)), int(enemy.get("eva", 0)), roll, is_physical=is_phys
                    )
                    if not hit_ok:
                        continue
                    elems = sfields.get("element_set", []) if sfields else wf.get("element_set", [])
                    mult = self._element_multiplier(enemy, elems) if elems else 1.0
                    if sfields:
                        raw_dmg = self._compute_skill_raw_damage(aid, sfields, enemy, seed_val=turn_cursor + aid)
                    else:
                        raw_dmg = self.battle_formula.attack_damage(self._actor_formula_stats(aid), _be_enemy_formula_stats(enemy))
                        raw_dmg = self.battle_formula.vary(raw_dmg, self.battle_formula.attack_variance, turn_cursor * 13 + aid)
                    hero_dmg = int(round(raw_dmg * mult))
                    if is_crit:
                        hero_dmg = self.battle_formula.apply_critical(hero_dmg)
                    enemy["hp"] = max(0, min(int(enemy["max_hp"]), int(enemy["hp"]) - hero_dmg))
                    if sfields and hero_dmg > 0:
                        self._apply_state_sets(enemy["states"], sfields.get("plus_state_set", []), sfields.get("minus_state_set", []))
                    elif not sfields and hero_dmg > 0:
                        w_plus = wf.get("plus_state_set", [])
                        if w_plus:
                            self._apply_state_sets(enemy["states"], w_plus, [])
                    if int(enemy["hp"]) <= 0:
                        break
                if int(enemy["hp"]) <= 0:
                    break
                e_can_act, _e_slip = self._tick_states_on_combatant(enemy)
                if not e_can_act:
                    continue
                living_ids = [aid for aid in self.party_member_ids if int(self._get_actor_state(aid)["hp"]) > 0]
                if not living_ids:
                    return self._finish_battle_outcome(troop_name, 0, 0, "lose", can_escape, can_lose)
                self._execute_enemy_turn_action(enemy, bs_sim, set(), living_ids, turn_cursor)
                turn_cursor += 1
                bs_sim["turn"] += 1
                if all(int(self._get_actor_state(aid)["hp"]) <= 0 for aid in self.party_member_ids):
                    return self._finish_battle_outcome(troop_name, 0, 0, "lose", can_escape, can_lose)
            total_exp += int(enemy["exp"])
            total_gold += int(enemy["gold"])
        self._record_defeated_enemies(enemies_inst)
        return self._finish_battle_outcome(
            troop_name, total_exp, total_gold, "win", can_escape, can_lose, enemies=enemies_inst
        )

    def is_slot_fixed(self, actor_id: int, equip_type: int) -> bool:
        """Returns True when an actor's equipment slot is locked via RMXP *_fix flags."""
        if equip_type not in (0, 1, 2, 3, 4, 5):
            return True
        af = self.actors.get(actor_id, {}).get("fields", {})
        if equip_type == 0:
            return bool(af.get("weapon_fix", False))
        fix_key = f"armor{equip_type}_fix"
        return bool(af.get(fix_key, False) or af.get("armor_fix", False))

    def can_actor_equip(self, actor_id: int, equip_type: int, equip_id: int) -> bool:
        """Checks slot fix flags, armor kind match, and actor class weapon_set/armor_set permissions."""
        if self.is_slot_fixed(actor_id, equip_type):
            return False
        if equip_id <= 0:
            return equip_id == 0
        st = self._get_actor_state(actor_id)
        cls_fields = self.classes_db.get(int(st.get("class_id", 1)), {}).get("fields", {})
        if equip_type == 0:
            wset = cls_fields.get("weapon_set")
            if isinstance(wset, list) and wset and equip_id not in {int(x) for x in wset}:
                return False
            return True
        afields = self.armors.get(equip_id, {}).get("fields", {})
        expected_kind = min(3, equip_type - 1)
        if afields and int(afields.get("kind", expected_kind)) != expected_kind:
            return False
        aset = cls_fields.get("armor_set")
        if isinstance(aset, list) and aset and equip_id not in {int(x) for x in aset}:
            return False
        return True

    def equip_from_bag(self, actor_id: int, equip_type: int, equip_id: int) -> bool:
        """Equips a weapon or armor owned in the bag onto actor_id (or 0 to unequip), swapping displaced gear back."""
        if not self.can_actor_equip(actor_id, equip_type, equip_id):
            return False
        st = self._get_actor_state(actor_id)
        if equip_type == 0:
            if equip_id > 0 and self.weapons_owned.get(equip_id, 0) <= 0:
                return False
            old_id = int(st["weapon_id"])
            if old_id == equip_id:
                return False
            if old_id > 0:
                self._apply_inventory_delta(self.weapons_owned, old_id, "add", 1)
            if equip_id > 0:
                self._apply_inventory_delta(self.weapons_owned, equip_id, "remove", 1)
            st["weapon_id"] = equip_id
            gname = self.weapons.get(equip_id, {}).get("fields", {}).get("name", "무기 해제") if equip_id > 0 else "무기 해제"
        else:
            if equip_id > 0 and self.armors_owned.get(equip_id, 0) <= 0:
                return False
            old_id = int(st["armor_ids"].get(equip_type, 0))
            if old_id == equip_id:
                return False
            if old_id > 0:
                self._apply_inventory_delta(self.armors_owned, old_id, "add", 1)
            if equip_id > 0:
                self._apply_inventory_delta(self.armors_owned, equip_id, "remove", 1)
            st["armor_ids"][equip_type] = equip_id
            gname = self.armors.get(equip_id, {}).get("fields", {}).get("name", "방어구 해제") if equip_id > 0 else "방어구 해제"
        self._recompute_combat_stats()
        self.log.append(f"[Equip] {st['name']} -> {gname}")
        return True

    @staticmethod
    def _decode_rmxp_move_command(mc: Dict[str, Any]) -> str:
        mf = mc.get("fields", mc) if isinstance(mc, dict) else {}
        code = int(mf.get("code", 0) or 0)
        params = list(mf.get("parameters", []))
        simple = {
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
        if code in simple:
            return simple[code]
        if code == 14 and len(params) >= 2:
            return f"jump:{int(params[0])}:{int(params[1])}"
        if code == 29 and params:
            return f"speed:{int(params[0])}"
        if code == 30 and params:
            return f"freq:{int(params[0])}"
        if code == 41 and params:
            hue = int(params[1]) if len(params) > 1 else 0
            d_val = int(params[2]) if len(params) > 2 else 2
            pat = int(params[3]) if len(params) > 3 else 0
            return f"graphic:{params[0]}:{hue}:{d_val}:{pat}"
        if code == 42 and params:
            return f"opacity:{int(params[0])}"
        if code == 43 and params:
            return f"blend:{int(params[0])}"
        return ""

    def advance_npc_routes(self, target_event_id: Optional[int] = None) -> None:
        """Advances autonomous NPC movement (`move_type` 1/2/3) and active `set_move_route` scripts."""
        m = self.current_map()
        blocked = self._blocking_coords() | {(self.x, self.y)}
        dir_to_int = {"down": 2, "left": 4, "right": 6, "up": 8}
        cardinal_cycle = ("down", "left", "up", "right")
        for ev, page in self.active_events_on_map():
            eid = int(ev["id"])
            if target_event_id is not None and eid != target_event_id:
                continue
            key = (self.map_id, eid)
            route = self._active_move_routes.get(key)
            step_name: Optional[str] = None
            skippable = True
            if route is not None:
                steps = route["steps"]
                idx = int(route["idx"])
                if idx < len(steps):
                    step_name = steps[idx]
                    skippable = bool(route.get("skippable", True))
                    next_idx = idx + 1
                    if next_idx >= len(steps):
                        if route.get("repeat"):
                            next_idx = 0
                        else:
                            self._active_move_routes.pop(key, None)
                    route["idx"] = next_idx
            else:
                mtype = int(page.get("move_type", 0))
                if mtype == 1:
                    self._npc_step_seq += 1
                    rng = (self._npc_step_seq * 1103515245 + eid * 12345 + int(ev["x"]) * 3 + int(ev["y"]) * 7) & 0x7FFFFFFF
                    step_name = ("down", "left", "right", "up")[rng % 4]
                elif mtype == 2:
                    dx_p = self.x - int(ev["x"])
                    dy_p = self.y - int(ev["y"])
                    if abs(dx_p) >= abs(dy_p) and dx_p != 0:
                        step_name = "right" if dx_p > 0 else "left"
                    elif dy_p != 0:
                        step_name = "down" if dy_p > 0 else "up"
                elif mtype == 3:
                    pr = page.get("move_route", {})
                    pf = pr.get("fields", {}) if isinstance(pr, dict) else {}
                    skippable = bool(pf.get("skippable", True))
                    cmds = [self._decode_rmxp_move_command(mc) for mc in pf.get("list", []) if isinstance(mc, dict)]
                    cmds = [c for c in cmds if c]
                    if cmds:
                        raw_idx = int(ev.get("_page_route_idx", 0))
                        repeat_route = bool(pf.get("repeat", True))
                        if repeat_route:
                            r_idx = raw_idx % len(cmds)
                            step_name = cmds[r_idx]
                            ev["_page_route_idx"] = (r_idx + 1) % len(cmds)
                        elif raw_idx < len(cmds):
                            step_name = cmds[raw_idx]
                            ev["_page_route_idx"] = raw_idx + 1
            if not step_name or step_name == "wait":
                continue
            gfx = page.get("graphic", {}).get("fields") if isinstance(page.get("graphic"), dict) else None
            if step_name == "through_on":
                page["through"] = True
                continue
            if step_name == "through_off":
                page["through"] = False
                continue
            if step_name in ("walk_anime_on", "walk_anime_off"):
                page["walk_anime"] = step_name.endswith("_on")
                continue
            if step_name in ("step_anime_on", "step_anime_off"):
                page["step_anime"] = step_name.endswith("_on")
                continue
            if step_name in ("dir_fix_on", "dir_fix_off"):
                page["direction_fix"] = step_name.endswith("_on")
                continue
            if step_name in ("always_on_top_on", "always_on_top_off"):
                page["always_on_top"] = step_name.endswith("_on")
                continue
            if step_name.startswith("speed:"):
                page["move_speed"] = max(1, min(6, int(step_name.split(":", 1)[1])))
                continue
            if step_name.startswith("freq:"):
                page["move_frequency"] = max(1, min(6, int(step_name.split(":", 1)[1])))
                continue
            if step_name.startswith("opacity:") and gfx is not None:
                gfx["opacity"] = max(0, min(255, int(step_name.split(":", 1)[1])))
                continue
            if step_name.startswith("blend:") and gfx is not None:
                gfx["blend_type"] = max(0, min(2, int(step_name.split(":", 1)[1])))
                continue
            if step_name.startswith("graphic:") and gfx is not None:
                gparts = step_name.split(":")
                gfx["character_name"] = gparts[1] if len(gparts) > 1 else ""
                if len(gparts) > 2:
                    gfx["character_hue"] = int(gparts[2])
                if len(gparts) > 3:
                    gfx["direction"] = int(gparts[3])
                if len(gparts) > 4:
                    gfx["pattern"] = int(gparts[4])
                continue
            if step_name.startswith("jump:"):
                jparts = step_name.split(":")
                jx = int(jparts[1]) if len(jparts) > 1 else 0
                jy = int(jparts[2]) if len(jparts) > 2 else 0
                nx = max(0, min(int(m["width"]) - 1, int(ev["x"]) + jx))
                ny = max(0, min(int(m["height"]) - 1, int(ev["y"]) + jy))
                ex, ey = int(ev["x"]), int(ev["y"])
                other_blocked = blocked - {(ex, ey)}
                if (bool(page.get("through", False)) or (nx, ny) not in other_blocked) and (nx, ny) != (self.x, self.y):
                    blocked.discard((ex, ey))
                    ev["x"], ev["y"] = nx, ny
                    blocked.add((nx, ny))
                    self.npc_jump_state[(self.map_id, eid)] = {"frames": 14, "total": 14}
                continue
            if step_name == "random":
                self._npc_step_seq += 1
                step_name = cardinal_cycle[(self._npc_step_seq + eid) % 4]
            if step_name.startswith("turn_") and step_name[5:] in DIR_VECTORS:
                gfx = page.get("graphic", {}).get("fields") if isinstance(page.get("graphic"), dict) else None
                if gfx is not None and not page.get("direction_fix", False):
                    gfx["direction"] = dir_to_int[step_name[5:]]
                continue
            if step_name not in DIR_VECTORS:
                continue
            dx, dy = DIR_VECTORS[step_name]
            ex, ey = int(ev["x"]), int(ev["y"])
            gfx = page.get("graphic", {}).get("fields") if isinstance(page.get("graphic"), dict) else None
            if gfx is not None and not page.get("direction_fix", False):
                gfx["direction"] = dir_to_int[step_name]
            other_blocked = blocked - {(ex, ey)}
            through = bool(page.get("through", False))
            can_step = through or can_move_step(m, self.passages_by_ts, self.priorities_by_ts, other_blocked, ex, ey, dx, dy)
            if can_step and (ex + dx, ey + dy) != (self.x, self.y):
                blocked.discard((ex, ey))
                ev["x"] = ex + dx
                ev["y"] = ey + dy
                blocked.add((ex + dx, ey + dy))
            elif route is not None and not skippable:
                route["idx"] = max(0, int(route["idx"]) - 1)

    def export_save_state(self) -> Dict[str, Any]:
        """Serializes full runtime progression state with schema version and SHA-256 checksum."""
        state = {
            "save_version": _SAVE_SCHEMA_VERSION,
            "map_id": self.map_id,
            "x": self.x,
            "y": self.y,
            "direction": self.direction,
            "gold": self.gold,
            "exp": self.exp,
            "ticks": self.ticks,
            "encounter_steps": self.encounter_steps,
            "screen_tone": list(self.screen_tone),
            "screen_faded_out": self.screen_faded_out,
            "weather": dict(self.weather),
            "fog_state": dict(self.fog_state),
            "text_options": dict(self.text_options),
            "timer_seconds": self.timer_seconds,
            "timer_active": self.timer_active,
            "save_enabled": self.save_enabled,
            "menu_enabled": self.menu_enabled,
            "encounter_enabled": self.encounter_enabled,
            "player_transparent": self.player_transparent,
            "player_through": self.player_through,
            "player_opacity": int(self.player_opacity),
            "player_blend_type": int(self.player_blend_type),
            "windowskin_name": self.windowskin_name,
            "camera_scroll_px": list(self.camera_scroll_px),
            "camera_scroll_target_px": list(self.camera_scroll_target_px),
            "camera_scroll_speed": int(self.camera_scroll_speed),
            "vehicle": self.vehicle,
            "vehicle_positions": {k: dict(v) for k, v in self.vehicle_positions.items()},
            "pictures": {str(k): dict(v) for k, v in self.pictures.items()},
            "quests": {str(k): dict(v) for k, v in self.quests.items()},
            "current_bgm": dict(self.current_bgm) if self.current_bgm else None,
            "current_bgs": dict(self.current_bgs) if self.current_bgs else None,
            "memorized_bgm": dict(self.memorized_bgm) if self.memorized_bgm else None,
            "memorized_bgs": dict(self.memorized_bgs) if self.memorized_bgs else None,
            "battle_bgm": dict(self.battle_bgm) if self.battle_bgm else None,
            "battle_end_me": dict(self.battle_end_me) if self.battle_end_me else None,
            "audio_muted": bool(self.audio_muted),
            "game_over": self.game_over,
            "last_battle_result": self.last_battle_result,
            "party_members": list(self.party_member_ids),
            "inventory": {str(k): v for k, v in self.inventory.items() if v > 0},
            "weapons_owned": {str(k): v for k, v in self.weapons_owned.items() if v > 0},
            "armors_owned": {str(k): v for k, v in self.armors_owned.items() if v > 0},
            "defeated_enemies": {str(k): v for k, v in self.defeated_enemies.items() if v > 0},
            "play_time_ms": float(self.play_time_ms),
            "switches": {str(k): v for k, v in self.switches.items() if v},
            "variables": {str(k): v for k, v in self.variables.items() if v != 0},
            "self_switches": {f"{m}:{e}:{c}": v for (m, e, c), v in self.self_switches.items() if v},
            "erased_events": [f"{m}:{e}" for (m, e) in sorted(self.erased_events)],
            "npc_step_seq": int(self._npc_step_seq),
            "active_move_routes": {
                f"{m}:{e}": dict(rt)
                for (m, e), rt in self._active_move_routes.items()
            },
            "event_positions": {
                f"{mid}:{int(ev['id'])}": {
                    "x": int(ev["x"]),
                    "y": int(ev["y"]),
                    "page_route_idx": int(ev.get("_page_route_idx", 0)),
                    "dirs": [
                        int(pg.get("graphic", {}).get("fields", {}).get("direction", 2))
                        if isinstance(pg.get("graphic"), dict)
                        else 2
                        for pg in ev.get("pages", [])
                    ],
                }
                for mid, mobj in self.maps.items()
                for ev in mobj.get("events", [])
            },
            "actor_states": {
                str(aid): {
                    **st,
                    "armor_ids": {str(slot): val for slot, val in st["armor_ids"].items()},
                    "learned_skills": list(st["learned_skills"]),
                }
                for aid, st in self.actor_states.items()
            },
        }
        state["checksum"] = _compute_save_checksum(state)
        return state

    def import_save_state(self, state: Dict[str, Any], strict_checksum: Optional[bool] = None) -> None:
        """Restores full runtime progression state from a saved dictionary with migration and checksum verification."""
        eff_strict = self.strict_checksum if strict_checksum is None else bool(strict_checksum)
        if "checksum" in state:
            expected = _compute_save_checksum(state)
            self.save_checksum_valid = (str(state.get("checksum", "")) == expected)
            if not self.save_checksum_valid:
                self.log.append("[Save] 체크섬 불일치 감지 (손상 또는 변조된 저장 데이터)")
                if eff_strict:
                    raise ValueError("Save file checksum verification failed")
        elif eff_strict:
            self.save_checksum_valid = False
            self.log.append("[Save] 체크섬 누락 감지 (손상 또는 변조된 저장 데이터)")
            raise ValueError("Save file checksum missing")
        else:
            self.save_checksum_valid = True
        state = migrate_save_state(state)
        self.map_id = int(state.get("map_id", self.map_id))
        self.x = int(state.get("x", self.x))
        self.y = int(state.get("y", self.y))
        self.direction = str(state.get("direction", "down"))
        self.gold = int(state.get("gold", 0))
        self.exp = int(state.get("exp", 0))
        self.ticks = int(state.get("ticks", 0))
        self.encounter_steps = int(state.get("encounter_steps", 0))
        tone = [int(v) for v in state.get("screen_tone", [0, 0, 0, 0])]
        while len(tone) < 4:
            tone.append(0)
        self.screen_tone = tone[:4]
        self.screen_faded_out = bool(state.get("screen_faded_out", False))
        w_raw = state.get("weather")
        self.weather = (
            {"type": str(w_raw.get("type", "none")), "power": int(w_raw.get("power", 0))}
            if isinstance(w_raw, dict)
            else {"type": "none", "power": 0}
        )
        fg_raw = state.get("fog_state")
        if isinstance(fg_raw, dict):
            self.fog_state = {
                "name": str(fg_raw.get("name", "")),
                "opacity": int(fg_raw.get("opacity", 64)),
                "tone": list(fg_raw.get("tone", [0, 0, 0, 0]))[:4],
                "sx": int(fg_raw.get("sx", 0)),
                "sy": int(fg_raw.get("sy", 0)),
                "scroll_x": float(fg_raw.get("scroll_x", 0.0)),
                "scroll_y": float(fg_raw.get("scroll_y", 0.0)),
            }
        to_raw = state.get("text_options")
        if isinstance(to_raw, dict):
            self.text_options = {
                "position": max(0, min(2, int(to_raw.get("position", 2)))),
                "show_frame": bool(to_raw.get("show_frame", True)),
            }
        self.timer_seconds = max(0, int(state.get("timer_seconds", 0)))
        self.timer_active = bool(state.get("timer_active", False)) and self.timer_seconds > 0
        self.timer_accum_ms = 0.0
        self.save_enabled = bool(state.get("save_enabled", True))
        self.menu_enabled = bool(state.get("menu_enabled", True))
        self.encounter_enabled = bool(state.get("encounter_enabled", True))
        self.player_transparent = bool(state.get("player_transparent", False))
        self.player_through = bool(state.get("player_through", False))
        self.player_opacity = max(0, min(255, int(state.get("player_opacity", 255))))
        self.player_blend_type = max(0, min(2, int(state.get("player_blend_type", 0))))
        self.windowskin_name = str(state.get("windowskin_name", self.windowskin_name))
        sc_px = state.get("camera_scroll_px")
        sc_tgt = state.get("camera_scroll_target_px")
        self.camera_scroll_px = [int(sc_px[0]), int(sc_px[1])] if isinstance(sc_px, list) and len(sc_px) >= 2 else [0, 0]
        self.camera_scroll_target_px = [int(sc_tgt[0]), int(sc_tgt[1])] if isinstance(sc_tgt, list) and len(sc_tgt) >= 2 else list(self.camera_scroll_px)
        self.camera_scroll_pos_px = [float(self.camera_scroll_px[0]), float(self.camera_scroll_px[1])]
        self.camera_scroll_speed = max(1, min(6, int(state.get("camera_scroll_speed", 4))))
        raw_defeated = state.get("defeated_enemies")
        self.defeated_enemies = (
            {int(k): int(v) for k, v in raw_defeated.items() if int(v) > 0}
            if isinstance(raw_defeated, dict)
            else {}
        )
        self.play_time_ms = max(0.0, float(state.get("play_time_ms", 0.0)))
        self.vehicle = str(state.get("vehicle", "walk"))
        vpos_raw = state.get("vehicle_positions")
        self.vehicle_positions = (
            {str(k): {"map_id": int(v["map_id"]), "x": int(v["x"]), "y": int(v["y"])} for k, v in vpos_raw.items() if isinstance(v, dict)}
            if isinstance(vpos_raw, dict)
            else {}
        )
        pics_raw = state.get("pictures")
        self.pictures = (
            {int(k): dict(v) for k, v in pics_raw.items() if isinstance(v, dict)}
            if isinstance(pics_raw, dict)
            else {}
        )
        self.current_bgm = dict(state["current_bgm"]) if isinstance(state.get("current_bgm"), dict) else None
        self.memorized_bgm = dict(state["memorized_bgm"]) if isinstance(state.get("memorized_bgm"), dict) else None
        self.memorized_bgs = dict(state["memorized_bgs"]) if isinstance(state.get("memorized_bgs"), dict) else None
        self.battle_bgm = dict(state["battle_bgm"]) if isinstance(state.get("battle_bgm"), dict) else None
        self.battle_end_me = dict(state["battle_end_me"]) if isinstance(state.get("battle_end_me"), dict) else None
        self.current_bgs = dict(state["current_bgs"]) if isinstance(state.get("current_bgs"), dict) else None
        self.audio_muted = bool(state.get("audio_muted", False))
        self.pending_flash = None
        self.pending_shake = None
        self.game_over = bool(state.get("game_over", False))
        self.last_battle_result = str(state.get("last_battle_result", "none"))
        self.party_member_ids = [int(aid) for aid in state.get("party_members", [1])] or [1]
        self.inventory = {int(k): int(v) for k, v in state.get("inventory", {}).items() if int(v) > 0}
        self.weapons_owned = {int(k): int(v) for k, v in state.get("weapons_owned", {}).items() if int(v) > 0}
        self.armors_owned = {int(k): int(v) for k, v in state.get("armors_owned", {}).items() if int(v) > 0}
        self.switches = {int(k): bool(v) for k, v in state.get("switches", {}).items() if v}
        self.variables = {int(k): int(v) for k, v in state.get("variables", {}).items()}
        self.self_switches = {}
        for k, v in state.get("self_switches", {}).items():
            if v:
                m_s, e_s, ch = str(k).split(":", 2)
                self.self_switches[(int(m_s), int(e_s), ch)] = True
        self.erased_events = set()
        self.quests = {
            str(k): dict(v)
            for k, v in state.get("quests", {}).items()
            if isinstance(v, dict)
        }
        for pair_s in state.get("erased_events", []):
            if isinstance(pair_s, str) and ":" in pair_s:
                ms, es = pair_s.split(":", 1)
                self.erased_events.add((int(ms), int(es)))
        restored_actors: Dict[int, Dict[str, Any]] = {}
        for aid_s, st in state.get("actor_states", {}).items():
            aid = int(aid_s)
            restored_actors[aid] = {
                **st,
                "id": aid,
                "armor_ids": {int(s): int(v) for s, v in st.get("armor_ids", {}).items()},
                "learned_skills": [int(sid) for sid in st.get("learned_skills", [])],
            }
        if restored_actors:
            self.actor_states = restored_actors
        self._npc_step_seq = int(state.get("npc_step_seq", 0))
        self._active_move_routes = {}
        for k, rt in state.get("active_move_routes", {}).items():
            m_s, e_s = str(k).split(":", 1)
            self._active_move_routes[(int(m_s), int(e_s))] = {
                "steps": [str(s) for s in rt.get("steps", [])],
                "idx": int(rt.get("idx", 0)),
                "repeat": bool(rt.get("repeat", False)),
                "skippable": bool(rt.get("skippable", True)),
            }
        ev_pos_map = state.get("event_positions", {})
        for mid, mobj in self.maps.items():
            for ev in mobj.get("events", []):
                epos = ev_pos_map.get(f"{mid}:{int(ev['id'])}")
                if not isinstance(epos, dict):
                    continue
                ev["x"] = int(epos.get("x", ev["x"]))
                ev["y"] = int(epos.get("y", ev["y"]))
                if "page_route_idx" in epos:
                    ev["_page_route_idx"] = int(epos["page_route_idx"])
                dirs = epos.get("dirs", [])
                for p_i, pg in enumerate(ev.get("pages", [])):
                    if p_i < len(dirs) and isinstance(pg.get("graphic"), dict) and isinstance(pg["graphic"].get("fields"), dict):
                        pg["graphic"]["fields"]["direction"] = int(dirs[p_i])
        self.pending = None
        self._recompute_combat_stats()
        self.log.append("[Save] 저장 데이터 불러오기 완료")

    def save_slot_path(self, slot: int = 1) -> Path:
        """Resolves the JSON save file path for numbered slot 1..9 (portable or OS save folder)."""
        clamped = max(1, min(9, int(slot)))
        if self.save_dir_override is not None:
            base_dir = self.save_dir_override
        elif self.use_os_save_dir:
            base_dir = resolve_user_save_dir(app_name=self.ir_dir.name or "rpgcreator_cli", fallback_dir=self.ir_dir)
        else:
            base_dir = self.ir_dir
        base_dir.mkdir(parents=True, exist_ok=True)
        return base_dir / f"save_slot{clamped}.json"

    def slot_exists(self, slot: int = 1) -> bool:
        """Checks whether a numbered save slot file exists."""
        return self.save_slot_path(slot).is_file()

    def get_save_slot_summary(self, slot: int = 1) -> Dict[str, Any]:
        """Reads metadata summary for save slot 1..9 without modifying runtime state."""
        clamped = max(1, min(9, int(slot)))
        path = self.save_slot_path(clamped)
        if not path.is_file():
            return {"slot": clamped, "exists": False, "map_name": "", "level": 0, "gold": 0, "ticks": 0, "play_time_ms": 0.0, "play_time": "00:00", "label": "비어 있음"}
        try:
            data = _load_json(path)
            mid = int(data.get("map_id", 1))
            mname = str(self.maps.get(mid, {}).get("name", f"Map {mid}"))
            pmembers = data.get("party_members") or [self.lead_actor_id]
            lead_id = str(pmembers[0])
            lv = int(data.get("actor_states", {}).get(lead_id, {}).get("level", 1))
            gold = int(data.get("gold", 0))
            ticks = int(data.get("ticks", 0))
            pt_ms = max(0.0, float(data.get("play_time_ms", 0.0)))
            pt_str = format_play_time(pt_ms)
            return {
                "slot": clamped,
                "exists": True,
                "map_name": mname,
                "level": lv,
                "gold": gold,
                "ticks": ticks,
                "play_time_ms": pt_ms,
                "play_time": pt_str,
                "label": f"{mname} Lv.{lv} {gold}G {pt_str}",
            }
        except Exception:
            return {"slot": clamped, "exists": True, "map_name": "?", "level": 1, "gold": 0, "ticks": 0, "play_time_ms": 0.0, "play_time": "00:00", "label": "저장 데이터"}

    def delete_slot(self, slot: int = 1) -> bool:
        """Deletes the save file for slot 1..9 if it exists."""
        path = self.save_slot_path(slot)
        if not path.is_file():
            return False
        path.unlink()
        self.log.append(f"[Save] 슬롯 {max(1, min(9, int(slot)))} 삭제")
        return True

    def get_autosave_summary(self) -> Dict[str, Any]:
        """Reads metadata summary for autosave file without modifying runtime state."""
        path = self.autosave_path()
        if not path.is_file():
            return {"slot": 0, "exists": False, "map_name": "", "level": 0, "gold": 0, "ticks": 0, "play_time_ms": 0.0, "play_time": "00:00", "label": "없음"}
        try:
            data = _load_json(path)
            mid = int(data.get("map_id", 1))
            mname = str(self.maps.get(mid, {}).get("name", f"Map {mid}"))
            pmembers = data.get("party_members") or [self.lead_actor_id]
            lead_id = str(pmembers[0])
            lv = int(data.get("actor_states", {}).get(lead_id, {}).get("level", 1))
            gold = int(data.get("gold", 0))
            pt_ms = max(0.0, float(data.get("play_time_ms", 0.0)))
            pt_str = format_play_time(pt_ms)
            return {
                "slot": 0,
                "exists": True,
                "map_name": mname,
                "level": lv,
                "gold": gold,
                "ticks": int(data.get("ticks", 0)),
                "play_time_ms": pt_ms,
                "play_time": pt_str,
                "label": f"[자동] {mname} Lv.{lv} {gold}G {pt_str}",
            }
        except Exception:
            return {"slot": 0, "exists": True, "map_name": "?", "level": 1, "gold": 0, "ticks": 0, "play_time_ms": 0.0, "play_time": "00:00", "label": "[자동] 저장 데이터"}

    def autosave_path(self) -> Path:
        return self.save_slot_path(1).with_name("save_autosave.json")

    def autosave_exists(self) -> bool:
        return self.autosave_path().is_file()

    def save_autosave(self) -> Path:
        target = self.save_to_file(save_path=self.autosave_path(), slot=1)
        self.log.append(f"[Save] 자동 저장 완료 ({target.name})")
        return target

    def load_autosave(self, strict_checksum: Optional[bool] = None) -> bool:
        return self.load_from_file(save_path=self.autosave_path(), slot=1, strict_checksum=strict_checksum)

    def save_to_file(self, save_path: Optional[Path] = None, slot: int = 1) -> Path:
        target = Path(save_path) if save_path else self.save_slot_path(slot)
        _save_json(target, self.export_save_state())
        self.log.append(f"[Save] 슬롯 {max(1, min(9, int(slot)))} 저장 완료 ({target.name})")
        return target

    def load_from_file(self, save_path: Optional[Path] = None, slot: int = 1, strict_checksum: Optional[bool] = None) -> bool:
        target = Path(save_path) if save_path else self.save_slot_path(slot)
        if not target.exists():
            self.log.append(f"[Save] 슬롯 {max(1, min(9, int(slot)))} 저장 파일이 없습니다")
            return False
        self.import_save_state(_load_json(target), strict_checksum=strict_checksum)
        return True

    def snapshot(self) -> Dict[str, Any]:
        lead_st = self._get_actor_state(self.lead_actor_id)
        return {
            "ticks": self.ticks,
            "map_id": self.map_id,
            "x": self.x,
            "y": self.y,
            "direction": self.direction,
            "level": int(lead_st["level"]),
            "hp": self.hp,
            "max_hp": self.max_hp,
            "sp": self.sp,
            "max_sp": self.max_sp,
            "atk": self.atk,
            "pdef": self.pdef,
            "party_size": len(self.party_member_ids),
            "party_members": list(self.party_member_ids),
            "weapon_id": self.weapon_id,
            "skills": list(self.learned_skills),
            "actor_states": {
                str(aid): {
                    "name": st["name"],
                    "level": int(st["level"]),
                    "exp": int(st["exp"]),
                    "hp": st["hp"],
                    "max_hp": st["max_hp"],
                    "sp": st["sp"],
                    "max_sp": st["max_sp"],
                    "weapon_id": st["weapon_id"],
                    "armor_ids": dict(st["armor_ids"]),
                    "skills": list(st["learned_skills"]),
                }
                for aid, st in sorted(self.actor_states.items())
            },
            "gold": self.gold,
            "exp": self.exp,
            "last_battle_result": self.last_battle_result,
            "screen_tone": list(self.screen_tone),
            "screen_faded_out": self.screen_faded_out,
            "weather": dict(self.weather),
            "timer_seconds": self.timer_seconds,
            "timer_active": self.timer_active,
            "save_enabled": self.save_enabled,
            "menu_enabled": self.menu_enabled,
            "encounter_enabled": self.encounter_enabled,
            "player_transparent": self.player_transparent,
            "player_through": self.player_through,
            "player_opacity": int(self.player_opacity),
            "player_blend_type": int(self.player_blend_type),
            "windowskin_name": self.windowskin_name,
            "camera_scroll_px": list(self.camera_scroll_px),
            "camera_scroll_target_px": list(self.camera_scroll_target_px),
            "camera_scroll_speed": int(self.camera_scroll_speed),
            "play_time_ms": float(self.play_time_ms),
            "defeated_enemies": dict(self.defeated_enemies),
            "vehicle": self.vehicle,
            "vehicle_positions": {k: dict(v) for k, v in self.vehicle_positions.items()},
            "pictures": {str(k): dict(v) for k, v in self.pictures.items()},
            "quests": {str(k): dict(v) for k, v in self.quests.items()},
            "current_bgm": dict(self.current_bgm) if self.current_bgm else None,
            "game_over": self.game_over,
            "inventory": self.inventory,
            "weapons_owned": dict(self.weapons_owned),
            "armors_owned": dict(self.armors_owned),
            "switches": {str(k): v for k, v in sorted(self.switches.items()) if v},
            "self_switches": {f"{m}:{e}:{c}": v for (m, e, c), v in sorted(self.self_switches.items()) if v},
            "pending_dialogue": self.pending.lines if self.pending else [],
            "pending_choices": self.pending.choices if self.pending else [],
            "log": self.log,
        }


def run_headless_simulation(ir_dir: Path, actions: List[str]) -> Dict[str, Any]:
    """Runs an input action script against HeadlessGameSession and returns the final snapshot."""
    session = HeadlessGameSession(ir_dir)
    for act in actions:
        session.step(act)
    return session.snapshot()


def run_fuzz_playtest(ir_dir: Path, steps: int = 200, seed: int = 1) -> Dict[str, Any]:
    """Runs a deterministic random-walk playtest agent for fuzz coverage and crash/hang detection."""
    import random

    rng = random.Random(int(seed))
    session = HeadlessGameSession(ir_dir)
    visited_tiles: set = {(session.map_id, session.x, session.y)}
    visited_maps: set = {session.map_id}
    dialogues = 0
    battles = 0
    errors: List[str] = []
    directions = ("up", "down", "left", "right")
    for step_idx in range(max(1, int(steps))):
        try:
            if session.game_over:
                session.step("gameover_retry")
                continue
            if session.pending is not None:
                dialogues += 1
                if session.pending.battle_state is not None:
                    battles += 1
                    session.step("choose:0")
                elif session.pending.choices:
                    idx = rng.randrange(len(session.pending.choices))
                    session.step(f"choose:{idx}")
                else:
                    session.step("confirm")
                continue
            legal_dirs = [d for d in directions if session.can_step_action(d)]
            roll = rng.random()
            if roll < 0.25 or not legal_dirs:
                session.step("interact")
            else:
                session.step(rng.choice(legal_dirs))
            visited_tiles.add((session.map_id, session.x, session.y))
            visited_maps.add(session.map_id)
        except Exception as exc:
            errors.append(f"step {step_idx}: {exc}")
            break
    snap = session.snapshot()
    snap["fuzz"] = {
        "steps_requested": max(1, int(steps)),
        "seed": int(seed),
        "unique_tiles": len(visited_tiles),
        "maps_visited": sorted(visited_maps),
        "prompt_interactions": dialogues,
        "battle_turns": battles,
        "errors": errors,
        "passed": len(errors) == 0,
    }
    return snap
