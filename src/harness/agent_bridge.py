# -*- coding: utf-8 -*-
"""
Studio Agent & CLI Command Bridge.
Provides headless command routing, Korean/English natural-language intent execution,
JSON Blueprint application, context packet generation for external AI coding agents,
and optional external AI CLI delegation with strict undo safety.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import re
import shlex
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..core.editor import (
    add_event,
    apply_blueprint,
    batch_tune_balance,
    capture_undo_snapshot,
    create_map,
    inspect_ir_project,
    list_animations,
    list_common_events,
    list_events,
    list_maps,
    paint_map,
    push_undo_snapshot,
    redo_last_edit,
    run_interactive_workbench,
    set_db_entry,
    undo_last_edit,
)
from ..core.simulator import run_fuzz_playtest
from ..core.utils import load_json
from ..core.validator import (
    export_flow_graph,
    simulate_battle_balance,
    validate_ir_project,
    validate_project_assets,
)

# CLI subcommands whose syntax is `<cmd> <ir_dir> ...` vs `<cmd> <sub_action> <ir_dir> ...`
_ONE_WORD_IR_CMDS = {
    "check",
    "check-assets",
    "balance-check",
    "export-graph",
    "inspect",
    "preview",
    "sim",
    "play",
    "undo",
    "redo",
    "apply-blueprint",
    "workbench",
    "export-release",
    "balance-tune",
}

_TWO_WORD_IR_CMDS = {
    "map": {"create", "list", "delete", "paint"},
    "event": {"add", "list", "delete"},
    "db": {"set", "get", "delete"},
    "common-event": {"set", "list"},
    "anim": {"set", "list"},
    "i18n": {"extract", "apply"},
}

_READ_ONLY_CLI_CMDS = {
    ("inspect", None),
    ("check", None),
    ("check-assets", None),
    ("balance-check", None),
    ("export-graph", None),
    ("sim", None),
    ("map", "list"),
    ("event", "list"),
    ("db", "get"),
    ("common-event", "list"),
    ("anim", "list"),
}

_WORKBENCH_VERBS = {
    "help",
    "status",
    "summary",
    "maps",
    "events",
    "paint",
    "add-event",
    "delete-event",
    "set-db",
    "db",
    "tune",
    "check",
    "assets",
    "balance",
    "fuzz",
    "undo",
    "redo",
}


def _looks_like_path(tok: str, ir_dir: Path) -> bool:
    if not tok or tok.startswith("-"):
        return False
    if tok == str(ir_dir) or "/" in tok or "\\" in tok:
        return True
    cand = Path(tok)
    return cand.exists()


def _inject_ir_dir(tokens: List[str], ir_dir: Path) -> List[str]:
    """Inserts str(ir_dir) into CLI tokens when the user omitted the workspace path."""
    if not tokens:
        return tokens
    cmd = tokens[0]
    ir_str = str(ir_dir)
    if cmd in _TWO_WORD_IR_CMDS and len(tokens) >= 2 and tokens[1] in _TWO_WORD_IR_CMDS[cmd]:
        # Flags could sit after sub_action; check first non-flag token from index 2 onward.
        rest = tokens[2:]
        first_pos = next((t for t in rest if not t.startswith("-")), None)
        if first_pos is not None and _looks_like_path(first_pos, ir_dir):
            return tokens
        return [cmd, tokens[1], ir_str, *rest]

    if cmd in _ONE_WORD_IR_CMDS:
        rest = tokens[1:]
        first_pos = next((t for t in rest if not t.startswith("-")), None)
        if first_pos is not None and _looks_like_path(first_pos, ir_dir):
            return tokens
        return [cmd, ir_str, *rest]

    return tokens


def build_agent_context_packet(
    ir_dir: Path,
    current_map_id: Optional[int] = None,
    selected_event_id: Optional[int] = None,
    selected_table: Optional[str] = None,
) -> Dict[str, Any]:
    """Builds a structured workspace summary for external AI agents or GUI inspection."""
    ir_path = Path(ir_dir)
    summary = inspect_ir_project(ir_path)
    maps = list_maps(ir_path)
    packet: Dict[str, Any] = {
        "ir_dir": str(ir_path),
        "project": summary,
        "maps": maps,
        "common_events_count": len(list_common_events(ir_path)),
        "animations_count": len(list_animations(ir_path)),
        "selection": {
            "current_map_id": current_map_id,
            "selected_event_id": selected_event_id,
            "selected_table": selected_table,
        },
        "supported_cli_examples": [
            "check",
            "check-assets",
            "balance-check",
            "map create Town2 24 18 --fill-tile 384",
            "paint 1 0 385 --rect 2 2 6 6",
            "event add 1 Merchant 5 6 --trigger action_button --say 'Welcome!'",
            "db set enemies 1 maxhp=500 exp=40",
            "balance-tune --enemy-hp-mult 1.2 --exp-mult 1.1",
            "sim --fuzz-steps 100 --seed 1",
        ],
    }
    if current_map_id is not None:
        mpath = ir_path / "maps" / f"map_{int(current_map_id):03d}.json"
        if mpath.is_file():
            m_data = load_json(mpath)
            evs = list(m_data.get("events", []))
            packet["active_map"] = {
                "map_id": int(m_data.get("map_id", current_map_id)),
                "name": str(m_data.get("name", "")),
                "width": int(m_data.get("width", 0)),
                "height": int(m_data.get("height", 0)),
                "tileset_id": int(m_data.get("tileset_id", 1)),
                "events": [
                    {"id": int(e.get("id", 0)), "name": str(e.get("name", "")), "x": int(e.get("x", 0)), "y": int(e.get("y", 0))}
                    for e in evs[:50]
                ],
            }
            if selected_event_id is not None:
                found = next((e for e in evs if int(e.get("id", -1)) == int(selected_event_id)), None)
                if found is not None:
                    packet["active_event"] = found
    return packet


def format_agent_prompt_packet(
    ir_dir: Path,
    user_goal: str = "",
    current_map_id: Optional[int] = None,
    selected_event_id: Optional[int] = None,
    selected_table: Optional[str] = None,
) -> str:
    """Formats a copy-ready Markdown prompt containing current Studio IR context and schema rules."""
    pkt = build_agent_context_packet(
        ir_dir,
        current_map_id=current_map_id,
        selected_event_id=selected_event_id,
        selected_table=selected_table,
    )
    return (
        "# RPG Creator Studio Context\n"
        f"- Workspace IR: `{pkt['ir_dir']}`\n"
        f"- Goal: {user_goal or '(describe your change or request a Blueprint JSON)'}\n\n"
        "Respond with a valid RPG Creator Blueprint JSON (`{\"db\": [...], \"maps\": [...], \"paint\": [...], \"events\": [...]}`) "
        "or CLI commands (`check`, `map ...`, `event ...`, `db ...`, `balance-tune ...`).\n\n"
        "```json\n"
        f"{json.dumps(pkt, ensure_ascii=False, indent=2)}\n"
        "```\n"
    )


def _find_entry_id_by_name(ir_dir: Path, table: str, name_query: str) -> Optional[int]:
    db_path = Path(ir_dir) / "database" / f"{table}.json"
    if not db_path.is_file():
        return None
    q = name_query.strip().lower()
    if not q:
        return None
    entries = load_json(db_path)
    for exact_only in (True, False):
        for entry in entries:
            if isinstance(entry, dict) and isinstance(entry.get("fields"), dict):
                flds = entry["fields"]
                nm = str(flds.get("name", "")).strip().lower()
                if not nm:
                    continue
                if (exact_only and nm == q) or (not exact_only and q in nm):
                    return int(flds.get("id", 0))
    return None


def _split_shell_tokens(text: str) -> List[str]:
    tokens = shlex.split(text, posix=(os.name != "nt"))
    return [t[1:-1] if len(t) >= 2 and t[0] == t[-1] and t[0] in ("'", '"') else t for t in tokens]


def _extract_quoted_text(text: str) -> Optional[str]:
    m = re.search(r"['\"]([^'\"]+)['\"]", text)
    return m.group(1) if m else None


def parse_agent_instruction(
    prompt_text: str,
    ir_dir: Path,
    context: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Deterministically parses a Studio console prompt into a structured execution plan."""
    raw = str(prompt_text or "").strip()
    if not raw:
        return {"kind": "empty", "raw": raw}

    ctx = dict(context or {})
    cur_map = int(ctx.get("current_map_id") or 1)
    ir_path = Path(ir_dir)

    # 1) Raw JSON Blueprint or fenced JSON block
    cleaned = raw
    if raw.startswith("```"):
        cleaned = re.sub(r"^```[a-zA-Z0-9_-]*\s*|\s*```$", "", raw, flags=re.DOTALL).strip()
    if cleaned.startswith("{"):
        try:
            bp = json.loads(cleaned)
            if isinstance(bp, dict):
                return {"kind": "blueprint", "blueprint": bp}
        except json.JSONDecodeError:
            pass

    # 2) Exact CLI command routing (with optional leading slash stripped)
    cli_cand = raw[1:].strip() if raw.startswith("/") else raw
    try:
        tokens = _split_shell_tokens(cli_cand)
    except ValueError:
        tokens = cli_cand.split()

    if tokens:
        cmd0 = tokens[0]
        if cmd0 in _ONE_WORD_IR_CMDS or (cmd0 in _TWO_WORD_IR_CMDS and len(tokens) >= 2 and tokens[1] in _TWO_WORD_IR_CMDS[cmd0]):
            sub_tok = tokens[1] if cmd0 in _TWO_WORD_IR_CMDS and len(tokens) >= 2 else None
            # Prefer workbench for plain `check`/`undo`/`redo` only? CLI path handles all flags (`--json`, `--rect`, `--say`, etc.).
            return {
                "kind": "cli",
                "argv": _inject_ir_dir(tokens, ir_path),
                "mutated": (cmd0, sub_tok) not in _READ_ONLY_CLI_CMDS,
            }
        # Shorthand workbench REPL commands (`maps`, `events`, `summary`, `status`, `assets`, `balance`, `fuzz ...`, `tune ...`)
        if cmd0 in _WORKBENCH_VERBS and cmd0 not in ("check", "undo", "redo"):
            return {
                "kind": "workbench",
                "line": cli_cand,
                "mutated": cmd0 in ("tune", "paint", "add-event", "delete-event", "set-db"),
            }

    lower = raw.lower()

    # 3) Natural-language Korean & English intents
    if lower.strip() in ("도움말", "명령어", "help", "?") or "도움말" in lower or "명령어" in lower:
        return {"kind": "nl_help", "mutated": False}

    if any(k in lower for k in ("되돌리기", "실행 취소", "undo")) and "redo" not in lower:
        return {"kind": "nl_undo", "mutated": True}
    if any(k in lower for k in ("다시 실행", "다시실행", "재실행", "redo")):
        return {"kind": "nl_redo", "mutated": True}

    if any(k in lower for k in ("에셋 검사", "에셋 검증", "리소스 검사", "asset check", "check asset")):
        return {"kind": "nl_check_assets", "mutated": False}

    if any(k in lower for k in ("밸런스 체크", "밸런스 검사", "밸런스 시뮬", "전투 밸런스", "balance check")):
        return {"kind": "nl_balance_check", "mutated": False}

    if any(k in lower for k in ("흐름도", "플로우 그래프", "flow graph", "mermaid")):
        fmt = "dot" if "dot" in lower or "graphviz" in lower else "mermaid"
        return {"kind": "nl_export_graph", "fmt": fmt, "mutated": False}

    if any(k in lower for k in ("무결성", "프로젝트 검증", "전체 검증", "검증해", "검사해", "validate")):
        return {"kind": "nl_check", "mutated": False}

    # Fuzz playtesting: e.g. "퍼징 100회", "fuzz 150 steps seed 3"
    if any(k in lower for k in ("퍼징", "랜덤 플레이", "무작위 탐험", "fuzz")):
        nums = [int(n) for n in re.findall(r"\d+", raw)]
        seed_m = re.search(r"(?:seed|시드)\s*[=:]?\s*(\d+)", lower)
        seed = int(seed_m.group(1)) if seed_m else 1
        steps = next((n for n in nums if n != seed), nums[0]) if nums else 100
        return {"kind": "nl_fuzz", "steps": max(1, steps), "seed": seed, "mutated": False}

    # Bulk balance tuning: e.g. "몬스터 체력 1.5배", "적 공격력 1.2배 경험치 2배", "enemy hp 1.5x"
    mult_matches = re.findall(r"([0-9]+(?:\.[0-9]+)?)\s*(?:배|x|×)", lower)
    if mult_matches or any(k in lower for k in ("배율", "난이도", "tune", "mult")):
        params: Dict[str, float] = {}
        patterns = [
            ("enemy_hp_mult", r"(?:적|몬스터|enemy)\s*(?:체력|hp|maxhp)[^0-9]*([0-9]+(?:\.[0-9]+)?)"),
            ("enemy_atk_mult", r"(?:적|몬스터|enemy)\s*(?:공격력|atk|attack)[^0-9]*([0-9]+(?:\.[0-9]+)?)"),
            ("exp_mult", r"(?:경험치|exp)[^0-9]*([0-9]+(?:\.[0-9]+)?)"),
            ("gold_mult", r"(?:골드|돈|gold)[^0-9]*([0-9]+(?:\.[0-9]+)?)"),
            ("skill_power_mult", r"(?:스킬|마법|skill)\s*(?:위력|power)?[^0-9]*([0-9]+(?:\.[0-9]+)?)"),
        ]
        for key, pat in patterns:
            m = re.search(pat, lower)
            if m:
                params[key] = float(m.group(1))
        # Shorthand like "체력 1.5배" or "공격력 2배" without "적"
        if not params:
            m_hp = re.search(r"(?:체력|hp)[^0-9]*([0-9]+(?:\.[0-9]+)?)\s*(?:배|x|×)", lower)
            m_atk = re.search(r"(?:공격력|atk)[^0-9]*([0-9]+(?:\.[0-9]+)?)\s*(?:배|x|×)", lower)
            if m_hp:
                params["enemy_hp_mult"] = float(m_hp.group(1))
            if m_atk:
                params["enemy_atk_mult"] = float(m_atk.group(1))
        if params:
            return {"kind": "nl_balance_tune", "params": params, "dry_run": ("미리보기" in lower or "dry" in lower), "mutated": not ("미리보기" in lower or "dry" in lower)}

    # Create map: "새 맵 '던전' 24x18", "create map Forest 25x20", "맵 생성 마을 20x15"
    if ("맵" in lower or "map" in lower) and any(k in lower for k in ("새", "생성", "만들", "추가", "create", "new")):
        size_m = re.search(r"(\d+)\s*[xX*×]\s*(\d+)", raw)
        w = int(size_m.group(1)) if size_m else 20
        h = int(size_m.group(2)) if size_m else 15
        quoted = _extract_quoted_text(raw)
        if not quoted:
            # Grab word after "맵" or "map" if present
            name_m = re.search(r"(?:map|맵)\s+(?:생성\s+|만들기\s+|추가\s+)?([^\d\s]+)", raw, re.IGNORECASE)
            quoted = name_m.group(1) if name_m else "New Map"
        return {"kind": "nl_create_map", "name": quoted, "width": w, "height": h, "mutated": True}

    # Paint tile rect or single tile: "(2,3)~(5,6) 타일 385 칠하기" or "타일 385로 채우기"
    if any(k in lower for k in ("타일", "칠", "채우", "paint", "fill")):
        tile_m = re.search(r"(?:타일|tile)\s*(?:id)?\s*[=:#]?\s*(\d+)", lower)
        layer_m = re.search(r"(?:레이어|layer)\s*[=:#]?\s*([012])", lower)
        coords = [(int(a), int(b)) for a, b in re.findall(r"\(?\s*(\d+)\s*,\s*(\d+)\s*\)?", raw)]
        if tile_m and coords:
            tile_id = int(tile_m.group(1))
            layer = int(layer_m.group(1)) if layer_m else 0
            if len(coords) >= 2:
                x0, y0 = min(coords[0][0], coords[1][0]), min(coords[0][1], coords[1][1])
                x1, y1 = max(coords[0][0], coords[1][0]), max(coords[0][1], coords[1][1])
                rect = (x0, y0, x1 - x0 + 1, y1 - y0 + 1)
                return {"kind": "nl_paint", "map_id": cur_map, "layer": layer, "tile_id": tile_id, "rect": rect, "mutated": True}
            return {"kind": "nl_paint", "map_id": cur_map, "layer": layer, "tile_id": tile_id, "points": [coords[0]], "mutated": True}

    # Add NPC / Event: "(5,6)에 '상인' NPC 추가 대사 '어서오세요'"
    if any(k in lower for k in ("npc", "이벤트", "event", "상인", "보물상자", "엔피씨")) and any(
        k in lower for k in ("추가", "배치", "만들", "생성", "add", "place", "create")
    ):
        pos_m = re.search(r"\(?\s*(\d+)\s*,\s*(\d+)\s*\)?", raw)
        ex = int(pos_m.group(1)) if pos_m else 1
        ey = int(pos_m.group(2)) if pos_m else 1
        quotes = re.findall(r"['\"]([^'\"]+)['\"]", raw)
        ev_name = quotes[0] if quotes else "NPC"
        say_text = quotes[1] if len(quotes) >= 2 else None
        map_m = re.search(r"맵\s*(\d+)|map\s*(\d+)", lower)
        mid = int(map_m.group(1) or map_m.group(2)) if map_m else cur_map
        cmds = [{"op": "say", "lines": [say_text]}] if say_text else [{"op": "say", "lines": [f"{ev_name}: 안녕하세요."]}]
        return {
            "kind": "nl_add_event",
            "map_id": mid,
            "name": ev_name,
            "x": ex,
            "y": ey,
            "commands": cmds,
            "mutated": True,
        }

    # Start map/pos: "시작 위치 맵 2 (10,8)"
    if any(k in lower for k in ("시작 위치", "시작위치", "시작 맵", "start pos", "start map")):
        map_m = re.search(r"(?:맵|map)\s*(\d+)", lower)
        pos_m = re.search(r"\(?\s*(\d+)\s*,\s*(\d+)\s*\)?", raw)
        fields: Dict[str, Any] = {}
        if map_m:
            fields["start_map_id"] = int(map_m.group(1))
        if pos_m:
            fields["start_x"] = int(pos_m.group(1))
            fields["start_y"] = int(pos_m.group(2))
        if fields:
            return {"kind": "nl_set_db", "table": "system", "entry_id": 1, "fields": fields, "mutated": True}

    # Direct DB stat change: "적 1번 hp 300", "슬라임 체력 250", "아이템 1번 가격 100", "액터 1 이름 '아르스'"
    table_aliases = [
        ("enemies", ("적", "몬스터", "enemy", "enemies")),
        ("actors", ("액터", "주인공", "영웅", "actor", "actors")),
        ("skills", ("스킬", "마법", "skill", "skills")),
        ("items", ("아이템", "물약", "포션", "item", "items")),
        ("weapons", ("무기", "weapon", "weapons")),
        ("armors", ("방어구", "갑옷", "armor", "armors")),
        ("classes", ("직업", "클래스", "class", "classes")),
        ("states", ("상태이상", "상태", "state", "states")),
        ("switches", ("스위치", "switch", "switches")),
        ("variables", ("변수", "variable", "variables")),
    ]
    matched_table = None
    for tbl, keys in table_aliases:
        if any(k in lower for k in keys):
            matched_table = tbl
            break
    entry_id: Optional[int] = None
    if matched_table is not None:
        id_m = re.search(r"(?:#|id\s*[=:]?\s*|(\d+)\s*번|table\s+)(\d+)|(?:적|액터|스킬|아이템|무기|방어구|스위치|변수|enemy|actor|skill|item|weapon|armor|switch|variable)s?\s+(\d+)", lower)
        if id_m:
            entry_id = int(next(g for g in id_m.groups() if g is not None))
        else:
            for tok in re.split(r"[\s,]+", raw):
                if len(tok) >= 2 and not tok.isdigit():
                    cand_id = _find_entry_id_by_name(ir_path, matched_table, tok)
                    if cand_id is not None:
                        entry_id = cand_id
                        break
    else:
        # Entity-name fallback without explicit category prefix (e.g. "고블린 체력 200")
        for tok in re.split(r"[\s,]+", raw):
            if len(tok) < 2 or tok.isdigit():
                continue
            for cand_tbl in ("enemies", "actors", "items", "weapons", "armors", "skills", "classes", "states"):
                cand_id = _find_entry_id_by_name(ir_path, cand_tbl, tok)
                if cand_id is not None:
                    matched_table = cand_tbl
                    entry_id = cand_id
                    break
            if matched_table is not None:
                break

    if matched_table is not None and entry_id is not None:
        fields: Dict[str, Any] = {}
        if matched_table in ("switches", "variables"):
            quoted = _extract_quoted_text(raw)
            if quoted:
                fields["name"] = quoted
        else:
            stat_aliases = [
                ("maxhp", r"(?:maxhp|체력|hp)\s*[=:]?\s*(\d+)"),
                ("maxsp", r"(?:maxsp|마나|mp|sp)\s*[=:]?\s*(\d+)"),
                ("atk", r"(?:atk|공격력)\s*[=:]?\s*(\d+)"),
                ("pdef", r"(?:pdef|물리방어|방어력)\s*[=:]?\s*(\d+)"),
                ("mdef", r"(?:mdef|마법방어)\s*[=:]?\s*(\d+)"),
                ("str", r"(?:\bstr\b|힘)\s*[=:]?\s*(\d+)"),
                ("dex", r"(?:\bdex\b|민첩)\s*[=:]?\s*(\d+)"),
                ("agi", r"(?:\bagi\b|속도)\s*[=:]?\s*(\d+)"),
                ("int", r"(?:\bint\b|지능|마력)\s*[=:]?\s*(\d+)"),
                ("exp", r"(?:exp|경험치)\s*[=:]?\s*(\d+)"),
                ("gold", r"(?:gold|골드)\s*[=:]?\s*(\d+)"),
                ("price", r"(?:price|가격)\s*[=:]?\s*(\d+)"),
                ("power", r"(?:power|위력)\s*[=:]?\s*(\d+)"),
                ("sp_cost", r"(?:sp_cost|소모\s*sp|마나\s*소모)\s*[=:]?\s*(\d+)"),
                ("initial_level", r"(?:initial_level|초기\s*레벨|레벨)\s*[=:]?\s*(\d+)"),
            ]
            for fk, pat in stat_aliases:
                sm = re.search(pat, lower)
                if sm:
                    fields[fk] = int(sm.group(1))
            quoted = _extract_quoted_text(raw)
            if quoted and ("이름" in lower or "name" in lower or not fields):
                fields["name"] = quoted
        if fields:
            return {"kind": "nl_set_db", "table": matched_table, "entry_id": entry_id, "fields": fields, "mutated": True}

    return {"kind": "unmatched", "raw": raw}


_PROMPT_PLACEHOLDER = "{prompt}"
_EXTERNAL_AGENT_TIMEOUT = 120
_STDIN_POINTER_TEXT = (
    "Read the RPG Creator Studio context and request from standard input, "
    "then respond with a Blueprint JSON or CLI commands."
)


def _split_agent_command(cmd_template: str) -> List[str]:
    """Splits a user command template into argv tokens without invoking a shell."""
    if os.name == "nt":
        # posix=False keeps Windows backslash paths intact; strip wrapping quotes manually.
        tokens = shlex.split(cmd_template, posix=False)
        out: List[str] = []
        for tok in tokens:
            if len(tok) >= 2 and tok[0] == tok[-1] and tok[0] in ("'", '"'):
                tok = tok[1:-1]
            out.append(tok)
        return out
    return shlex.split(cmd_template, posix=True)


def _build_external_agent_invocation(cmd_template: str, prompt_packet: str) -> Dict[str, Any]:
    """Builds subprocess arguments for an external AI CLI.

    - Without ``{prompt}``: the template runs through the shell (pipes etc. allowed) and the
      prompt is delivered via stdin, so no prompt text is ever interpreted by the shell.
    - With ``{prompt}``: the template is tokenized and executed *without* a shell; the
      placeholder is replaced inside the argv token, so quotes, backticks, ``$`` and newlines
      in the prompt cannot break the command or trigger command substitution.
      On Windows, ``.cmd``/``.bat`` shims (e.g. npm-installed CLIs) would be re-parsed by
      cmd.exe, so the placeholder is replaced by a short pointer and the full prompt goes to stdin.
    """
    if _PROMPT_PLACEHOLDER not in cmd_template:
        return {"args": cmd_template, "shell": True, "input": prompt_packet}

    argv = _split_agent_command(cmd_template)
    if not argv:
        raise ValueError("외부 AI CLI 명령이 비어 있습니다.")
    resolved = shutil.which(argv[0])
    if resolved:
        argv[0] = resolved
    is_batch_shim = os.name == "nt" and argv[0].lower().endswith((".cmd", ".bat"))
    replacement = _STDIN_POINTER_TEXT if is_batch_shim else prompt_packet
    argv = [tok.replace(_PROMPT_PLACEHOLDER, replacement) for tok in argv]
    return {"args": argv, "shell": False, "input": prompt_packet if is_batch_shim else None}


def _run_external_agent(
    cmd_template: str,
    prompt_packet: str,
    ir_dir: Path,
    apply_external: bool = False,
) -> Dict[str, Any]:
    """Invokes a user-configured external AI CLI command and extracts optional JSON blueprint output."""
    try:
        inv = _build_external_agent_invocation(cmd_template, prompt_packet)
        proc = subprocess.run(
            inv["args"],
            shell=inv["shell"],
            cwd=str(ir_dir),
            input=inv["input"],
            stdin=None if inv["input"] is not None else subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_EXTERNAL_AGENT_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "kind": "external_agent",
            "summary": f"External agent timed out after {_EXTERNAL_AGENT_TIMEOUT}s: {cmd_template}",
            "mutated": False,
        }
    except (OSError, ValueError) as exc:
        return {
            "ok": False,
            "kind": "external_agent",
            "summary": f"External agent could not be started ({exc}). 명령어 설치/로그인 상태와 PATH를 확인하세요: {cmd_template}",
            "mutated": False,
        }
    stdout = (proc.stdout or "").strip()
    stderr = (proc.stderr or "").strip()
    if proc.returncode != 0:
        return {
            "ok": False,
            "kind": "external_agent",
            "summary": f"External agent exited with code {proc.returncode}: {stderr or stdout}",
            "result": {"returncode": proc.returncode, "stdout": stdout, "stderr": stderr},
            "mutated": False,
        }

    # Check if external agent emitted a JSON blueprint
    bp_match = re.search(r"```json\s*(\{.*?\})\s*```", stdout, re.DOTALL)
    cand_json = bp_match.group(1) if bp_match else (stdout if stdout.startswith("{") else None)
    if cand_json:
        try:
            bp = json.loads(cand_json)
            if isinstance(bp, dict) and any(k in bp for k in ("db", "maps", "paint", "events", "patch_maps", "common_events")):
                res = apply_blueprint(ir_dir, bp, validate=True, dry_run=not apply_external)
                return {
                    "ok": True,
                    "kind": "external_blueprint",
                    "summary": f"External agent blueprint ({'applied' if apply_external else 'dry-run'}): {json.dumps(res, ensure_ascii=False)}",
                    "result": res,
                    "raw_output": stdout,
                    "mutated": bool(apply_external),
                }
        except json.JSONDecodeError:
            pass

    return {
        "ok": True,
        "kind": "external_agent",
        "summary": stdout or "(External agent returned empty output)",
        "result": {"stdout": stdout, "stderr": stderr},
        "mutated": False,
    }


def execute_studio_command(
    ir_dir: Path,
    command_text: str,
    context: Optional[Dict[str, Any]] = None,
    external_agent_cmd: Optional[str] = None,
    apply_external: bool = True,
) -> Dict[str, Any]:
    """Executes a CLI command, workbench line, Blueprint JSON, or natural-language agent prompt."""
    ir_path = Path(ir_dir)
    plan = parse_agent_instruction(command_text, ir_path, context=context)
    kind = plan["kind"]

    if kind == "empty":
        return {"ok": False, "kind": "empty", "summary": "명령이나 자연어 프롬프트를 입력하세요.", "mutated": False}

    try:
        if kind == "cli":
            import run_app as cli_app

            argv = list(plan["argv"])
            mutated = bool(plan.get("mutated", False))
            native_undo_cmds = ("undo", "redo", "apply-blueprint", "map", "event", "db", "common-event", "anim", "balance-tune")
            snap = capture_undo_snapshot(ir_path) if (mutated and argv[0] not in native_undo_cmds) else None
            out_buf = io.StringIO()
            err_buf = io.StringIO()
            with contextlib.redirect_stdout(out_buf), contextlib.redirect_stderr(err_buf):
                rc = cli_app.main(argv)
            out_txt = out_buf.getvalue().strip()
            err_txt = err_buf.getvalue().strip()
            if rc != 0:
                return {
                    "ok": False,
                    "kind": "cli",
                    "argv": argv,
                    "summary": err_txt or out_txt or f"CLI command failed with exit code {rc}",
                    "mutated": False,
                }
            if snap is not None:
                push_undo_snapshot(ir_path, f"cli:{' '.join(argv[:2])}", snap)
            return {
                "ok": True,
                "kind": "cli",
                "argv": argv,
                "summary": out_txt or f"Executed: {' '.join(argv)}",
                "result": out_txt,
                "mutated": mutated,
            }

        if kind == "workbench":
            res = run_interactive_workbench(ir_path, stdin_lines=[str(plan["line"])])
            logs = [str(item) for item in res.get("logs", [])]
            summary = "\n".join(logs)
            ok = not any(ln.startswith("[ERROR]") for ln in logs)
            return {
                "ok": ok,
                "kind": "workbench",
                "summary": summary or f"Executed workbench command (run={res.get('commands_run', 0)}).",
                "result": res,
                "mutated": bool(plan.get("mutated", False)) and ok,
            }

        if kind == "nl_help":
            help_txt = (
                "[사용 가능한 에이전트 / CLI 명령]\n"
                "- 검사: 검증해 / 에셋 검사 / 밸런스 체크 / 퍼징 200회 / 흐름도\n"
                "- 편집: 고블린 체력 200 / 적 체력 1.5배 / 새 맵 \"던전2\" 20x15\n"
                "- 배치: (3,4)에 \"안내판\" 이벤트 추가 대사 \"환영합니다\"\n"
                "- 워크벤치: paint 1 390 2 2 | add-event 1 \"NPC\" 3 3 \"안녕\" | set-db enemies 1 maxhp=200\n"
                "- 실행취소/재실행: 되돌리기 (undo) / 다시 실행 (redo)\n"
                "- 외부 AI CLI 사용 시 위 '외부 AI CLI' 칸에 실행 커맨드를 입력하세요 (권장: claude -p  /  gemini -p \"Blueprint JSON만 출력\" — 프롬프트는 stdin으로 전달)"
            )
            return {"ok": True, "kind": kind, "summary": help_txt, "mutated": False}

        if kind == "blueprint":
            res = apply_blueprint(ir_path, plan["blueprint"], validate=True, dry_run=False)
            return {
                "ok": True,
                "kind": "blueprint",
                "summary": json.dumps(res, ensure_ascii=False, indent=2),
                "result": res,
                "mutated": True,
            }

        if kind == "nl_undo":
            res = undo_last_edit(ir_path)
            return {"ok": True, "kind": kind, "summary": json.dumps(res, ensure_ascii=False), "result": res, "mutated": bool(res.get("restored"))}

        if kind == "nl_redo":
            res = redo_last_edit(ir_path)
            return {"ok": True, "kind": kind, "summary": json.dumps(res, ensure_ascii=False), "result": res, "mutated": bool(res.get("restored"))}

        if kind == "nl_check":
            vr = validate_ir_project(ir_path)
            res = vr.to_dict()
            return {"ok": vr.passed, "kind": kind, "summary": json.dumps(res, ensure_ascii=False, indent=2), "result": res, "mutated": False}

        if kind == "nl_check_assets":
            res = validate_project_assets(ir_path)
            return {"ok": bool(res.get("passed")), "kind": kind, "summary": json.dumps(res, ensure_ascii=False, indent=2), "result": res, "mutated": False}

        if kind == "nl_balance_check":
            res = simulate_battle_balance(ir_path)
            return {"ok": True, "kind": kind, "summary": json.dumps(res, ensure_ascii=False, indent=2), "result": res, "mutated": False}

        if kind == "nl_export_graph":
            txt = export_flow_graph(ir_path, fmt=str(plan.get("fmt", "mermaid")))
            return {"ok": True, "kind": kind, "summary": txt, "result": txt, "mutated": False}

        if kind == "nl_fuzz":
            res = run_fuzz_playtest(ir_path, steps=int(plan["steps"]), seed=int(plan["seed"]))
            return {"ok": True, "kind": kind, "summary": json.dumps(res, ensure_ascii=False, indent=2), "result": res, "mutated": False}

        if kind == "nl_balance_tune":
            dry = bool(plan.get("dry_run", False))
            res = batch_tune_balance(ir_path, dry_run=dry, **plan["params"])
            return {"ok": True, "kind": kind, "summary": json.dumps(res, ensure_ascii=False, indent=2), "result": res, "mutated": not dry}

        if kind == "nl_create_map":
            res = create_map(ir_path, name=plan["name"], width=int(plan["width"]), height=int(plan["height"]))
            return {"ok": True, "kind": kind, "summary": json.dumps(res, ensure_ascii=False), "result": res, "mutated": True}

        if kind == "nl_paint":
            res = paint_map(
                ir_path,
                map_id=int(plan["map_id"]),
                layer=int(plan["layer"]),
                tile_id=int(plan["tile_id"]),
                rect=plan.get("rect"),
                points=plan.get("points"),
            )
            return {"ok": True, "kind": kind, "summary": json.dumps(res, ensure_ascii=False), "result": res, "mutated": True}

        if kind == "nl_add_event":
            res = add_event(
                ir_path,
                map_id=int(plan["map_id"]),
                name=str(plan["name"]),
                x=int(plan["x"]),
                y=int(plan["y"]),
                commands=list(plan.get("commands", [])),
            )
            return {"ok": True, "kind": kind, "summary": json.dumps(res, ensure_ascii=False), "result": res, "mutated": True}

        if kind == "nl_set_db":
            res = set_db_entry(
                ir_path,
                table=str(plan["table"]),
                entry_id=int(plan["entry_id"]),
                fields=dict(plan["fields"]),
                record_undo=True,
            )
            return {"ok": True, "kind": kind, "summary": json.dumps(res, ensure_ascii=False), "result": res, "mutated": True}

        ext_cmd = (external_agent_cmd or os.environ.get("RPGC_AGENT_CMD", "")).strip()
        if ext_cmd:
            ctx = dict(context or {})
            pkt_txt = format_agent_prompt_packet(
                ir_path,
                user_goal=command_text,
                current_map_id=ctx.get("current_map_id"),
                selected_event_id=ctx.get("selected_event_id"),
                selected_table=ctx.get("selected_table"),
            )
            return _run_external_agent(ext_cmd, pkt_txt, ir_path, apply_external=apply_external)

        return {
            "ok": False,
            "kind": "unmatched",
            "summary": (
                "로컬 에이전트가 해석하지 못한 명령입니다. 다음 형식을 사용할 수 있습니다:\n"
                "• CLI 명령: `check`, `balance-check`, `map create 숲 24 18`, `db set enemies 1 maxhp=400`, `sim --fuzz-steps 100`\n"
                "• 자연어: `검증해줘`, `적 체력 1.5배`, `새 맵 '동굴' 25x20`, `(4,5)에 '상인' NPC 추가 대사 '어서오세요'`, `퍼징 100회`\n"
                "• JSON Blueprint: `{\"db\": [...], \"events\": [...]}`\n"
                "• 외부 AI CLI 연결: 환경변수 `RPGC_AGENT_CMD` 또는 우측 외부 에이전트 명령칸 설정"
            ),
            "mutated": False,
        }
    except Exception as exc:
        return {
            "ok": False,
            "kind": kind,
            "summary": f"[ERROR] {exc}",
            "mutated": False,
        }
