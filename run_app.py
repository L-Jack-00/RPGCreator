#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
rpgcreator_cli entrypoint with UTF-8 console guard.
Supports RMXP sample generation, .rxdata/.rgssad conversion, atomic map/event/DB
editing, integrity & BFS reachability checks, automated playthroughs, and live play.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

PROJECT_ROOT = os.path.abspath(os.path.dirname(__file__))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from src.core.engine import CoreEngine
from src.core.models import JobContext
from src.core.rgssad import DEFAULT_RGSSAD_KEY
from src.core.simulator import write_crash_report
from src.harness.player_gui import launch_player
from src.harness.supervisor import HarnessSupervisor

# UTF-8 Console encoding safety guard (safe in headless / --noconsole frozen builds)
_WINDOWED_STDIO = sys.stdout is None or sys.stderr is None
if sys.stdout is None:
    try:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    except Exception:
        pass
if sys.stderr is None:
    try:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")
    except Exception:
        pass
if sys.stdout is not None and getattr(sys.stdout, "encoding", None) != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass


def _parse_int_tuple(raw: str, expected_len: int) -> Tuple[int, ...]:
    parts = [int(x.strip()) for x in raw.split(",")]
    if len(parts) != expected_len:
        raise ValueError(f"Expected {expected_len} comma-separated integers, got {raw!r}")
    return tuple(parts)


def _parse_kv_fields(items: List[str]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for item in items:
        if "=" not in item:
            raise ValueError(f"Expected key=value format, got {item!r}")
        k, v = item.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def _commands_from_cli_args(
    commands_json: Optional[str],
    say: Optional[str] = None,
    warp: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Builds a semantic event command list from CLI flags."""
    if commands_json:
        return json.loads(commands_json)
    cmds: List[Dict[str, Any]] = []
    if say:
        cmds.append({"op": "say", "lines": say.split("|"), "indent": 0})
    if warp:
        m_id, wx, wy = _parse_int_tuple(warp, 3)
        cmds.append({
            "op": "transfer_player",
            "var_mode": 0,
            "map_id": m_id,
            "x": wx,
            "y": wy,
            "direction": "down",
            "fade_type": 0,
            "indent": 0,
        })
    return cmds


def _build_parser() -> argparse.ArgumentParser:
    json_parent = argparse.ArgumentParser(add_help=False)
    json_parent.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="Print machine-readable JSON output.")

    parser = argparse.ArgumentParser(
        prog="rpgcreator_cli",
        description="RMXP-compatible project converter, editor, validator, and player.",
    )
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON output.")
    sub = parser.add_subparsers(dest="command")

    p_sample = sub.add_parser("sample", parents=[json_parent], help="Generate a standard RMXP sample project and IR workspace.")
    p_sample.add_argument("output_dir", type=Path, help="Destination directory for sample output.")

    p_unpack = sub.add_parser("unpack", parents=[json_parent], help="Convert RMXP Data/*.rxdata into an IR workspace.")
    p_unpack.add_argument("rmxp_dir", type=Path, help="Source RMXP project directory containing Data/.")
    p_unpack.add_argument("ir_dir", type=Path, help="Target IR workspace directory.")
    p_unpack.add_argument("--encoding", type=str, default="utf-8", help="Source string encoding (e.g. utf-8, cp949, cp932).")

    p_pack = sub.add_parser("pack", parents=[json_parent], help="Compile an IR workspace back into RMXP Data/*.rxdata.")
    p_pack.add_argument("ir_dir", type=Path, help="Source IR workspace directory.")
    p_pack.add_argument("rmxp_dir", type=Path, help="Target RMXP output directory.")
    p_pack.add_argument("--encoding", type=str, default="utf-8", help="Target string encoding (e.g. utf-8, cp949, cp932).")

    p_urg = sub.add_parser("unpack-rgssad", parents=[json_parent], help="Extract an RGSSAD v1 (.rgssad) archive.")
    p_urg.add_argument("archive", type=Path, help="Path to .rgssad archive file.")
    p_urg.add_argument("output_dir", type=Path, help="Destination directory.")
    p_urg.add_argument("--key", type=lambda s: int(s, 0), default=DEFAULT_RGSSAD_KEY, help="Initial 32-bit key.")

    p_prg = sub.add_parser("pack-rgssad", parents=[json_parent], help="Pack a directory into an RGSSAD v1/v2/v3 archive.")
    p_prg.add_argument("source_dir", type=Path, help="Directory to pack.")
    p_prg.add_argument("archive", type=Path, help="Destination archive file (.rgssad/.rgss2a/.rgss3a).")
    p_prg.add_argument("--key", type=lambda s: int(s, 0), default=DEFAULT_RGSSAD_KEY, help="Initial 32-bit key/seed.")
    p_prg.add_argument("--version", type=int, choices=[1, 2, 3], default=1, help="Archive format version (1, 2, or 3).")

    p_map = sub.add_parser("map", parents=[json_parent], help="Atomic map creation, listing, deletion, and tile painting commands.")
    map_sub = p_map.add_subparsers(dest="map_command")

    p_mc = map_sub.add_parser("create", parents=[json_parent], help="Create a new map in the IR workspace.")
    p_mc.add_argument("ir_dir", type=Path)
    p_mc.add_argument("--name", required=True)
    p_mc.add_argument("--width", type=int, default=20)
    p_mc.add_argument("--height", type=int, default=15)
    p_mc.add_argument("--tileset-id", type=int, default=1)
    p_mc.add_argument("--fill-tile", type=int, default=384)

    p_mp = map_sub.add_parser("paint", parents=[json_parent], help="Paint a tile ID onto a map layer.")
    p_mp.add_argument("ir_dir", type=Path)
    p_mp.add_argument("--map-id", type=int, required=True)
    p_mp.add_argument("--layer", type=int, default=0)
    p_mp.add_argument("--tile", type=int, required=True)
    p_mp.add_argument("--rect", type=str, default=None, help="x,y,w,h")
    p_mp.add_argument("--point", action="append", default=[], help="x,y (repeatable)")

    p_ml = map_sub.add_parser("list", parents=[json_parent], help="List maps in the IR workspace.")
    p_ml.add_argument("ir_dir", type=Path)

    p_md = map_sub.add_parser("delete", parents=[json_parent], help="Delete a map by ID.")
    p_md.add_argument("ir_dir", type=Path)
    p_md.add_argument("--map-id", type=int, required=True)

    p_ev = sub.add_parser("event", parents=[json_parent], help="Atomic semantic event commands.")
    ev_sub = p_ev.add_subparsers(dest="event_command")
    p_ea = ev_sub.add_parser("add", parents=[json_parent], help="Add an event to a map.")
    p_ea.add_argument("ir_dir", type=Path)
    p_ea.add_argument("--map-id", type=int, required=True)
    p_ea.add_argument("--name", required=True)
    p_ea.add_argument("--x", type=int, required=True)
    p_ea.add_argument("--y", type=int, required=True)
    p_ea.add_argument("--trigger", type=str, default="action_button")
    p_ea.add_argument("--graphic", type=str, default="")
    p_ea.add_argument("--through", action="store_true")
    p_ea.add_argument("--say", type=str, default=None, help="Pipe-separated dialogue lines.")
    p_ea.add_argument("--warp", type=str, default=None, help="map_id,x,y")
    p_ea.add_argument("--commands-json", type=str, default=None, help="JSON array of semantic DSL commands.")

    p_el = ev_sub.add_parser("list", parents=[json_parent], help="List events on a map.")
    p_el.add_argument("ir_dir", type=Path)
    p_el.add_argument("--map-id", type=int, required=True)

    p_ed = ev_sub.add_parser("delete", parents=[json_parent], help="Delete an event from a map.")
    p_ed.add_argument("ir_dir", type=Path)
    p_ed.add_argument("--map-id", type=int, required=True)
    p_ed.add_argument("--event-id", type=int, required=True)

    p_db = sub.add_parser("db", parents=[json_parent], help="Atomic database editing commands.")
    db_sub = p_db.add_subparsers(dest="db_command")
    p_ds = db_sub.add_parser("set", parents=[json_parent], help="Create or update a database entry.")
    p_ds.add_argument("ir_dir", type=Path)
    p_ds.add_argument("--table", required=True)
    p_ds.add_argument("--id", type=int, required=True)
    p_ds.add_argument("--field", action="append", required=True, help="key=value (repeatable)")

    p_dg = db_sub.add_parser("get", parents=[json_parent], help="Inspect a database entry by table and ID.")
    p_dg.add_argument("ir_dir", type=Path)
    p_dg.add_argument("--table", required=True)
    p_dg.add_argument("--id", type=int, required=True)

    p_dd = db_sub.add_parser("delete", parents=[json_parent], help="Delete a database entry by table and ID.")
    p_dd.add_argument("ir_dir", type=Path)
    p_dd.add_argument("--table", required=True)
    p_dd.add_argument("--id", type=int, required=True)

    p_ce = sub.add_parser("common-event", parents=[json_parent], help="Common event editing and inspection commands.")
    ce_sub = p_ce.add_subparsers(dest="common_event_command")
    p_ces = ce_sub.add_parser("set", parents=[json_parent], help="Create or update a common event.")
    p_ces.add_argument("ir_dir", type=Path)
    p_ces.add_argument("--id", type=int, required=True)
    p_ces.add_argument("--name", required=True)
    p_ces.add_argument("--trigger", default="none", help="none, autorun, or parallel (or 0/1/2)")
    p_ces.add_argument("--switch-id", type=int, default=1)
    p_ces.add_argument("--say", default=None, help="Quick dialogue lines joined by '|'.")
    p_ces.add_argument("--commands-json", default=None, help="Semantic DSL command list as JSON string.")

    p_cel = ce_sub.add_parser("list", parents=[json_parent], help="List defined common events.")
    p_cel.add_argument("ir_dir", type=Path)

    p_bp = sub.add_parser("apply-blueprint", parents=[json_parent], help="Apply a declarative JSON blueprint atomically.")
    p_bp.add_argument("ir_dir", type=Path, help="IR workspace directory.")
    p_bp_src = p_bp.add_mutually_exclusive_group(required=True)
    p_bp_src.add_argument("--file", type=Path, help="Path to a JSON blueprint file.")
    p_bp_src.add_argument("--json-str", type=str, help="Inline JSON blueprint string.")
    p_bp.add_argument("--no-check", action="store_true", help="Skip automatic validation rollback.")
    p_bp.add_argument("--dry-run", action="store_true", help="Compute structural diff preview and roll back without saving.")

    p_undo = sub.add_parser("undo", parents=[json_parent], help="Revert the last edit from .ir_history.")
    p_undo.add_argument("ir_dir", type=Path, help="IR workspace directory.")

    p_redo = sub.add_parser("redo", parents=[json_parent], help="Re-apply the last undone edit from .ir_redo_history.")
    p_redo.add_argument("ir_dir", type=Path, help="IR workspace directory.")

    p_check = sub.add_parser("check", parents=[json_parent], help="Run integrity and BFS reachability checks on an IR workspace.")
    p_check.add_argument("ir_dir", type=Path, help="IR workspace directory to inspect.")

    p_ca = sub.add_parser("check-assets", parents=[json_parent], help="Verify referenced Graphics/ and Audio/ files and detect orphans.")
    p_ca.add_argument("ir_dir", type=Path, help="IR workspace directory.")
    p_ca.add_argument("--graphics-dir", type=Path, default=None)
    p_ca.add_argument("--audio-dir", type=Path, default=None)

    p_bal = sub.add_parser("balance-check", parents=[json_parent], help="Run combat balance estimation against defined RMXP troops.")
    p_bal.add_argument("ir_dir", type=Path, help="IR workspace directory.")
    p_bal.add_argument("--troop-id", type=int, default=None)
    p_bal.add_argument("--trials", type=int, default=20)

    p_grp = sub.add_parser("export-graph", parents=[json_parent], help="Export cross-map warp and switch dependency graph.")
    p_grp.add_argument("ir_dir", type=Path, help="IR workspace directory.")
    p_grp.add_argument("--format", choices=["mermaid", "dot"], default="mermaid")
    p_grp.add_argument("--out", type=Path, default=None, help="Optional output file path.")

    p_insp = sub.add_parser("inspect", parents=[json_parent], help="Print a summary of maps, events, and database entries in an IR workspace.")
    p_insp.add_argument("ir_dir", type=Path, help="IR workspace directory to summarize.")

    p_prev = sub.add_parser("preview", parents=[json_parent], help="Render an ASCII/PNG passability and event preview of a map.")
    p_prev.add_argument("ir_dir", type=Path, help="IR workspace directory.")
    p_prev.add_argument("--map-id", type=int, default=1, help="Numeric map ID to render (default: 1).")
    p_prev.add_argument("--png", type=Path, default=None, help="Optional PNG output file path.")
    p_prev.add_argument("--graphics-dir", type=Path, default=None, help="Optional Graphics directory for sprite rendering.")
    p_prev.add_argument("--switch", action="append", default=[], help="Switch state override id=ON|OFF (repeatable).")

    p_sim = sub.add_parser("sim", parents=[json_parent], help="Run an automated input sequence or fuzz playtest in memory.")
    p_sim.add_argument("ir_dir", type=Path)
    p_sim.add_argument("--actions", default=None, help="Comma-separated actions (e.g. right,up,interact,choose:0)")
    p_sim.add_argument("--script", type=Path, default=None, help="Path to action script file (newline/comma separated).")
    p_sim.add_argument("--fuzz-steps", type=int, default=0, help="Run deterministic fuzz playtest for N steps.")
    p_sim.add_argument("--seed", type=int, default=1, help="Seed for --fuzz-steps.")

    p_play = sub.add_parser("play", parents=[json_parent], help="Launch the lightweight Pygame player for an IR workspace.")
    p_play.add_argument("ir_dir", type=Path)
    p_play.add_argument("--graphics-dir", type=Path, default=None)
    p_play.add_argument("--title", action="store_true", help="Start on the Title / Options screen.")
    p_play.add_argument("--lang", type=str, default=None, help="Runtime locale code from locale/messages.json.")
    p_play.add_argument("--user-save", action="store_true", help="Store saves in the OS user data folder instead of ir_dir.")
    p_play.add_argument("--strict-save", action="store_true", help="Reject save files that fail checksum verification instead of warning.")

    p_i18n = sub.add_parser("i18n", parents=[json_parent], help="Internationalization catalog extraction and locale application.")
    i18n_sub = p_i18n.add_subparsers(dest="i18n_command")
    p_iex = i18n_sub.add_parser("extract", parents=[json_parent], help="Extract project dialogue, names, and DB text into locale/messages.json.")
    p_iex.add_argument("ir_dir", type=Path)
    p_iex.add_argument("--out", type=Path, default=None)
    p_iex.add_argument("--default-locale", type=str, default="ko")
    p_iap = i18n_sub.add_parser("apply", parents=[json_parent], help="Apply a locale translation onto an IR workspace.")
    p_iap.add_argument("ir_dir", type=Path)
    p_iap.add_argument("--lang", required=True)
    p_iap.add_argument("--file", type=Path, default=None)

    p_imv = sub.add_parser("import-mv", parents=[json_parent], help="Import an RPG Maker MV or MZ project directory into IR.")
    p_imv.add_argument("src_dir", type=Path)
    p_imv.add_argument("out_ir_dir", type=Path)

    p_itiled = sub.add_parser("import-tiled", parents=[json_parent], help="Import a Tiled .tmj/.tmx map into an IR workspace.")
    p_itiled.add_argument("tiled_file", type=Path)
    p_itiled.add_argument("ir_dir", type=Path)
    p_itiled.add_argument("--map-id", type=int, default=None)
    p_itiled.add_argument("--tileset-id", type=int, default=1)

    p_ivxa = sub.add_parser("import-vxace", parents=[json_parent], help="Import an RPG Maker VX Ace project (.rvdata2) into IR.")
    p_ivxa.add_argument("vxace_dir", type=Path)
    p_ivxa.add_argument("out_ir_dir", type=Path)

    p_rel = sub.add_parser("export-release", parents=[json_parent], help="Validate and bundle a clean RMXP release build with SHA-256 manifest.")
    p_rel.add_argument("ir_dir", type=Path, help="IR workspace directory to package.")
    p_rel.add_argument("out_dir", type=Path, help="Destination directory for the release bundle.")
    p_rel.add_argument("--rgssad", action="store_true", help="Pack Data/Graphics/Audio into Game.rgss3a/rgss2a/rgssad.")
    p_rel.add_argument("--rgssad-key", type=str, default=None, help="Optional custom integer/hex seed key (e.g. 0x1234ABCD).")
    p_rel.add_argument("--rgssad-version", type=int, choices=[1, 2, 3], default=3, help="RGSSAD archive format version (1, 2, or 3).")
    p_rel.add_argument("--lang", type=str, default=None, help="Apply a localized language table cleanly during export.")
    p_rel.add_argument("--launcher", action="store_true", help="Emit standalone OS launcher scripts and PyInstaller spec.")
    p_rel.add_argument("--icon", type=Path, default=None, help="Optional .ico/.png icon file copied alongside launcher/spec.")
    p_rel.add_argument("--zip", action="store_true", help="Also package the release directory as a compressed .zip archive.")
    p_rel.add_argument("--build-exe", action="store_true", help="Invoke PyInstaller to build a standalone executable after bundling.")
    p_rel.add_argument("--strict-assets", action="store_true", help="Abort release export if referenced Graphics/Audio files are missing.")
    p_rel.add_argument("--strict-save", action="store_true", help="Configure generated release launcher to reject tampered save files.")

    p_anim = sub.add_parser("anim", parents=[json_parent], help="List or configure Animations database entries.")
    anim_sub = p_anim.add_subparsers(dest="anim_command")
    p_al = anim_sub.add_parser("list", parents=[json_parent], help="List animations in database/animations.json.")
    p_al.add_argument("ir_dir", type=Path)
    p_as = anim_sub.add_parser("set", parents=[json_parent], help="Create or update an animation entry.")
    p_as.add_argument("ir_dir", type=Path)
    p_as.add_argument("--id", type=int, required=True, dest="anim_id")
    p_as.add_argument("--name", required=True)
    p_as.add_argument("--sheet", default="", help="Animations/ sheet image name.")
    p_as.add_argument("--frames", type=int, default=6)
    p_as.add_argument("--se", default="", help="Optional SE name triggered on frame 0.")
    p_as.add_argument("--cells-json", default=None, help="JSON array of multi-cell animation frames/entries.")
    p_as.add_argument("--timings-json", default=None, help="JSON array of animation timing cues.")

    p_tune = sub.add_parser("balance-tune", parents=[json_parent], help="Scale enemy stats, rewards, and skill power in bulk.")
    p_tune.add_argument("ir_dir", type=Path)
    p_tune.add_argument("--enemy-hp-mult", type=float, default=1.0)
    p_tune.add_argument("--enemy-atk-mult", type=float, default=1.0)
    p_tune.add_argument("--exp-mult", type=float, default=1.0)
    p_tune.add_argument("--gold-mult", type=float, default=1.0)
    p_tune.add_argument("--skill-power-mult", type=float, default=1.0)
    p_tune.add_argument("--dry-run", action="store_true")

    p_bf = sub.add_parser("battle-formula", parents=[json_parent], help="Show, change, or compare the project's battle damage formula preset.")
    bf_sub = p_bf.add_subparsers(dest="bf_command")
    bf_sub.add_parser("list", parents=[json_parent], help="List built-in formula presets (RMXP, 2000/2003, VX, VX Ace, MV, MZ, simple).")
    p_bfs = bf_sub.add_parser("show", parents=[json_parent], help="Show the project's stored setting and effective formulas.")
    p_bfs.add_argument("ir_dir", type=Path)
    p_bft = bf_sub.add_parser("set", parents=[json_parent], help="Choose a preset and/or override individual formula options.")
    p_bft.add_argument("ir_dir", type=Path)
    p_bft.add_argument("--preset", default=None, help="rmxp (default), simple, rm2k, rm2k3, rmvx, rmvxace, rmmv, rmmz")
    p_bft.add_argument("--attack", default=None, help="Custom normal-attack formula, e.g. 'a.atk * 4 - b.def * 2'.")
    p_bft.add_argument("--skill", default=None, help="Custom offensive skill formula (variables a, b, s).")
    p_bft.add_argument("--critical-multiplier", type=float, default=None)
    p_bft.add_argument("--attack-variance", type=int, default=None, help="Normal-attack variance percent.")
    p_bft.add_argument("--skill-variance", default=None, help="'skill' (use each skill's variance) or a percent.")
    p_bft.add_argument("--guard-divisor", type=float, default=None)
    p_bft.add_argument("--integer-division", choices=["on", "off"], default=None, help="Ruby-style integer division for '/'.")
    p_bft.add_argument("--stat-model", choices=["xp", "unified"], default=None)
    p_bft.add_argument("--reset-option", action="append", default=[], help="Remove one override (repeatable).")
    p_bft.add_argument("--clear-overrides", action="store_true", help="Drop all overrides and use the preset as-is.")
    p_bft.add_argument("--dry-run", action="store_true")
    p_bfc = bf_sub.add_parser("calc", parents=[json_parent], help="Calculate base damage between an actor and an enemy.")
    p_bfc.add_argument("ir_dir", type=Path)
    p_bfc.add_argument("--enemy", type=int, required=True, dest="enemy_id")
    p_bfc.add_argument("--actor", type=int, default=None, dest="actor_id", help="Defaults to the lead party actor.")
    p_bfc.add_argument("--skill", type=int, default=None, dest="skill_id", help="Actor skill ID to evaluate.")
    p_bfc.add_argument("--enemy-skill", type=int, default=None, dest="enemy_skill_id")
    p_bfc.add_argument("--preset", action="append", default=None, dest="presets", help="Preview another preset (repeatable) without saving.")
    p_bfc.add_argument("--all-presets", action="store_true", help="Compare every preset.")

    p_wb = sub.add_parser("workbench", parents=[json_parent], help="Start interactive terminal inspector and quick editor.")
    p_wb.add_argument("ir_dir", type=Path)

    p_studio = sub.add_parser(
        "studio",
        aliases=["gui", "editor"],
        help="Launch the visual RPG Creator Studio GUI with built-in Agent/CLI command dock.",
    )
    p_studio.add_argument("ir_dir", type=Path, nargs="?", default=None, help="Optional IR workspace directory.")
    p_studio.add_argument("--graphics-dir", type=Path, default=None, help="Optional RMXP Graphics/ asset folder.")

    return parser


def _parse_switch_overrides(items: List[str]) -> Dict[int, bool]:
    out: Dict[int, bool] = {}
    for raw in items:
        if "=" in raw:
            sid_s, val_s = raw.split("=", 1)
            out[int(sid_s.strip())] = val_s.strip().upper() in ("ON", "TRUE", "1", "YES")
        else:
            out[int(raw.strip())] = True
    return out


def _run_supervised(engine: CoreEngine, job_id: str, tasks: List[Any]) -> Any:
    supervisor = HarnessSupervisor(engine)
    ctx = JobContext(job_id=job_id, payload={"tasks": tasks})
    ok = supervisor.run_job_sync(ctx, total_steps=len(tasks), step_delay_sec=0.0)
    if not ok:
        raise RuntimeError(ctx.error_message or f"Job terminated with status {supervisor.state.name}")
    results = ctx.payload.get("results", [])
    return results[-1] if results else {}


def _emit_output(payload: Any, json_mode: bool, text_printer: Optional[Callable[[Any], None]] = None) -> None:
    """Formats a command output either as JSON or using an optional human-readable printer."""
    if json_mode:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    if text_printer is not None:
        text_printer(payload)
    else:
        print(f"[OK] {payload}")


def _handle_map(parser: argparse.ArgumentParser, args: argparse.Namespace, engine: CoreEngine) -> int:
    if args.map_command == "create":
        out = _run_supervised(
            engine,
            "map_create",
            [lambda: engine.map_create(args.ir_dir, args.name, args.width, args.height, args.tileset_id, args.fill_tile)],
        )
    elif args.map_command == "paint":
        rect = _parse_int_tuple(args.rect, 4) if args.rect else None
        pts = [_parse_int_tuple(p, 2) for p in args.point] if args.point else None
        out = _run_supervised(
            engine,
            "map_paint",
            [lambda: engine.map_paint(args.ir_dir, args.map_id, args.layer, args.tile, rect=rect, points=pts)],
        )
    elif args.map_command == "list":
        out = _run_supervised(engine, "map_list", [lambda: engine.map_list(args.ir_dir)])
    elif args.map_command == "delete":
        out = _run_supervised(engine, "map_delete", [lambda: engine.map_delete(args.ir_dir, args.map_id)])
    else:
        parser.parse_args(["map", "--help"])
        return 0
    _emit_output(out, args.json)
    return 0


def _handle_common_event(parser: argparse.ArgumentParser, args: argparse.Namespace, engine: CoreEngine) -> int:
    if args.common_event_command == "set":
        cmds = _commands_from_cli_args(args.commands_json, args.say)
        out = _run_supervised(
            engine,
            "common_event_set",
            [lambda: engine.common_event_set(args.ir_dir, args.id, args.name, args.trigger, args.switch_id, cmds)],
        )
        _emit_output(out, args.json)
        return 0
    if args.common_event_command == "list":
        out = _run_supervised(engine, "common_event_list", [lambda: engine.common_event_list(args.ir_dir)])

        def _print_ces(items: List[Dict[str, Any]]) -> None:
            print(f"[COMMON EVENTS] {len(items)} defined")
            for ce in items:
                print(
                    f"  #{ce['id']}: {ce['name']} "
                    f"(trigger={ce['trigger']}, switch={ce['switch_id']}, commands={ce['command_count']})"
                )

        _emit_output(out, args.json, _print_ces)
        return 0
    parser.parse_args(["common-event", "--help"])
    return 0


def _handle_battle_formula(parser: argparse.ArgumentParser, args: argparse.Namespace) -> int:
    from src.core.battle_formulas import PRESETS, list_presets
    from src.core.editor import calc_battle_damage, get_battle_formula, set_battle_formula

    sub_cmd = getattr(args, "bf_command", None)
    if sub_cmd == "list":
        def _print_list(rows: List[Dict[str, Any]]) -> None:
            for row in rows:
                mark = " (default)" if row["default"] else ""
                print(f"- {row['id']}{mark}: {row['label']}")
                print(f"    {row['description']}")

        _emit_output(list_presets(), args.json, _print_list)
        return 0
    if sub_cmd == "show":
        def _print_show(d: Dict[str, Any]) -> None:
            eff = d["effective"]
            print(f"[BATTLE FORMULA] {eff['preset']} - {eff['label']}" + ("" if d["config"] else " (not set: default)"))
            if eff["overrides"]:
                print(f"  overrides: {json.dumps(eff['overrides'], ensure_ascii=False)}")
            print(f"  attack: {eff['attack']}")
            print("  skill : " + eff["skill"].replace("\n", "\n          "))
            print(
                f"  critical x{eff['critical_multiplier']}, attack variance {eff['attack_variance']}%, "
                f"skill variance {eff['skill_variance']}, guard /{eff['guard_divisor']}, "
                f"integer division {'on' if eff['integer_division'] else 'off'}, stat model {eff['stat_model']}"
            )

        _emit_output(get_battle_formula(args.ir_dir), args.json, _print_show)
        return 0
    if sub_cmd == "set":
        overrides: Dict[str, Any] = {
            "attack": args.attack,
            "skill": args.skill,
            "critical_multiplier": args.critical_multiplier,
            "attack_variance": args.attack_variance,
            "guard_divisor": args.guard_divisor,
            "stat_model": args.stat_model,
        }
        if args.skill_variance is not None:
            overrides["skill_variance"] = "skill" if args.skill_variance == "skill" else int(args.skill_variance)
        if args.integer_division is not None:
            overrides["integer_division"] = args.integer_division == "on"
        for key in args.reset_option:
            overrides[key.replace("-", "_")] = ""
        res = set_battle_formula(
            args.ir_dir,
            preset=args.preset,
            overrides=overrides,
            clear_overrides=bool(args.clear_overrides),
            dry_run=bool(args.dry_run),
        )
        _emit_output(res, args.json, lambda d: print(f"[OK] battle_formula = {json.dumps(d['battle_formula'], ensure_ascii=False)}" + (" (dry-run)" if d["dry_run"] else "")))
        return 0
    if sub_cmd == "calc":
        presets = list(PRESETS) if args.all_presets else args.presets
        res = calc_battle_damage(
            args.ir_dir,
            enemy_id=args.enemy_id,
            actor_id=args.actor_id,
            skill_id=args.skill_id,
            enemy_skill_id=args.enemy_skill_id,
            presets=presets,
        )

        def _print_calc(d: Dict[str, Any]) -> None:
            print(f"[DAMAGE] {d['actor']['name']} (actor {d['actor']['id']}) vs {d['enemy']['name']} (enemy {d['enemy']['id']})  *before element/critical/variance")
            for row in d["results"]:
                parts = [f"actor attack {row['actor_attack']}"]
                if "actor_skill" in row:
                    parts.append(f"actor skill {row['actor_skill']}")
                parts.append(f"enemy attack {row['enemy_attack']}")
                if "enemy_skill" in row:
                    parts.append(f"enemy skill {row['enemy_skill']}")
                parts.append(f"hits to win {row['actor_hits_to_defeat_enemy']} / to lose {row['enemy_hits_to_defeat_actor']}")
                print(f"  {row['preset']:8} " + ", ".join(parts))

        _emit_output(res, args.json, _print_calc)
        return 0
    parser.parse_args(["battle-formula", "--help"])
    return 0


def _dispatch_command(parser: argparse.ArgumentParser, args: argparse.Namespace, engine: CoreEngine) -> int:
    """Executes a parsed CLI subcommand and returns an exit code."""
    cmd = args.command
    if cmd == "sample":
        out = _run_supervised(engine, "sample_init", [lambda: engine.create_sample(args.output_dir)])

        def _print_sample(d: Dict[str, Any]) -> None:
            print(f"[OK] Sample project created at: {d['output_dir']}")
            print(f"     - RMXP Binary: {d['rmxp_dir']}")
            print(f"     - IR Workspace: {d['ir_dir']}")
            print(f"     - Maps: {d['maps']}, Scripts: {d['scripts']}")

        _emit_output(out, args.json, _print_sample)
        return 0

    simple_jobs = {
        "unpack": (
            "unpack_rmxp",
            lambda: engine.unpack(args.rmxp_dir, args.ir_dir, encoding=args.encoding),
            lambda d: print(f"[OK] Unpacked {d['maps']} maps and {d['scripts']} scripts to: {d['ir_dir']}"),
        ),
        "pack": (
            "pack_rmxp",
            lambda: engine.pack(args.ir_dir, args.rmxp_dir, encoding=args.encoding),
            lambda d: print(f"[OK] Packed {d['maps']} maps and {d['scripts']} scripts to: {d['rmxp_dir']}"),
        ),
        "unpack-rgssad": (
            "unpack_rgssad",
            lambda: engine.unpack_archive(args.archive, args.output_dir, args.key),
            lambda d: print(f"[OK] Extracted {d['files_extracted']} files to: {d['output_dir']}"),
        ),
        "pack-rgssad": (
            "pack_rgssad",
            lambda: engine.pack_archive(args.source_dir, args.archive, args.key, version=args.version),
            lambda d: print(f"[OK] Packed {d['files_packed']} files into: {d['archive']}"),
        ),
    }
    if cmd in simple_jobs:
        job_name, step_fn, printer = simple_jobs[cmd]
        out = _run_supervised(engine, job_name, [step_fn])
        _emit_output(out, args.json, printer)
        return 0

    if cmd == "map":
        return _handle_map(parser, args, engine)
    if cmd == "event":
        if args.event_command == "add":
            cmds = _commands_from_cli_args(args.commands_json, args.say, args.warp)
            out = _run_supervised(
                engine,
                "event_add",
                [lambda: engine.event_add(args.ir_dir, args.map_id, args.name, args.x, args.y, args.trigger, args.graphic, args.through, cmds)],
            )
            _emit_output(out, args.json)
            return 0
        if args.event_command == "list":
            out = _run_supervised(engine, "event_list", [lambda: engine.event_list(args.ir_dir, args.map_id)])
            _emit_output(out, args.json)
            return 0
        if args.event_command == "delete":
            out = _run_supervised(engine, "event_delete", [lambda: engine.event_delete(args.ir_dir, args.map_id, args.event_id)])
            _emit_output(out, args.json)
            return 0
        parser.parse_args(["event", "--help"])
        return 0
    if cmd == "db":
        if args.db_command == "set":
            kv = _parse_kv_fields(args.field)
            out = _run_supervised(engine, "db_set", [lambda: engine.db_set(args.ir_dir, args.table, args.id, kv)])
            _emit_output(out, args.json)
            return 0
        if args.db_command == "get":
            out = _run_supervised(engine, "db_get", [lambda: engine.db_get(args.ir_dir, args.table, args.id)])
            _emit_output(out, args.json)
            return 0
        if args.db_command == "delete":
            out = _run_supervised(engine, "db_delete", [lambda: engine.db_delete(args.ir_dir, args.table, args.id)])
            _emit_output(out, args.json)
            return 0
        parser.parse_args(["db", "--help"])
        return 0
    if cmd == "common-event":
        return _handle_common_event(parser, args, engine)
    if cmd == "apply-blueprint":
        raw_text = args.file.read_text(encoding="utf-8") if args.file else args.json_str
        blueprint_data = json.loads(raw_text)
        out = _run_supervised(
            engine,
            "apply_blueprint",
            [lambda: engine.blueprint_apply(args.ir_dir, blueprint_data, validate=not args.no_check, dry_run=bool(args.dry_run))],
        )
        _emit_output(out, args.json)
        return 0
    if cmd == "undo":
        out = _run_supervised(engine, "undo_edit", [lambda: engine.undo(args.ir_dir)])
        _emit_output(out, args.json)
        return 0
    if cmd == "redo":
        out = _run_supervised(engine, "redo_edit", [lambda: engine.redo(args.ir_dir)])
        _emit_output(out, args.json)
        return 0
    if cmd == "check-assets":
        res = _run_supervised(
            engine,
            "check_assets",
            [lambda: engine.check_assets(args.ir_dir, graphics_dir=args.graphics_dir, audio_dir=args.audio_dir)],
        )

        def _print_assets(d: Dict[str, Any]) -> None:
            status = "PASS" if d["passed"] else "MISSING"
            print(
                f"[ASSETS:{status}] Graphics={d['referenced_graphics']} (missing={len(d['missing_graphics'])}, orphan={len(d['orphan_graphics'])}) | "
                f"Audio={d['referenced_audio']} (missing={len(d['missing_audio'])}, orphan={len(d['orphan_audio'])})"
            )
            for m in d["missing_graphics"][:10]:
                print(f"  [MISS GFX] {m}")
            for m in d["missing_audio"][:10]:
                print(f"  [MISS AUD] {m}")

        _emit_output(res, args.json, _print_assets)
        return 0 if res["passed"] else 1
    if cmd == "balance-check":
        res = _run_supervised(
            engine,
            "balance_check",
            [lambda: engine.balance_check(args.ir_dir, troop_id=args.troop_id, trials=args.trials)],
        )

        def _print_bal(d: Dict[str, Any]) -> None:
            print(f"[BALANCE] Balanced={d['balanced']} (trials={d['trials_per_troop']}, formula={d.get('battle_formula', 'rmxp')})")
            for tr in d["troops"]:
                print(
                    f"  Troop #{tr['troop_id']} {tr['name']}: win_rate={tr['win_rate']:.0%}, "
                    f"avg_turns={tr['avg_turns']}, rec_lv={tr['recommended_level']} [{tr['status']}]"
                )

        _emit_output(res, args.json, _print_bal)
        return 0
    if cmd == "export-graph":
        graph_txt = _run_supervised(
            engine,
            "export_graph",
            [lambda: engine.export_graph(args.ir_dir, fmt=args.format)],
        )
        if args.out:
            args.out.parent.mkdir(parents=True, exist_ok=True)
            args.out.write_text(graph_txt, encoding="utf-8")
        if args.json:
            print(json.dumps({"format": args.format, "graph": graph_txt, "out": str(args.out) if args.out else None}, ensure_ascii=False, indent=2))
        else:
            print(graph_txt)
        return 0
    if cmd == "check":
        res = _run_supervised(engine, "check_ir", [lambda: engine.check(args.ir_dir)])
        if args.json:
            print(json.dumps(res.to_dict(), ensure_ascii=False, indent=2))
        else:
            status = "PASS" if res.passed else "FAIL"
            print(f"[{status}] Maps: {res.stats.get('maps', 0)}, Events: {res.stats.get('events', 0)}, Reachable tiles: {res.stats.get('reachable_tiles', 0)}")
            for err in res.errors:
                print(f"  [ERROR] {err}")
            for warn in res.warnings:
                print(f"  [WARN]  {warn}")
        return 0 if res.passed else 1
    if cmd == "inspect":
        info = _run_supervised(engine, "inspect_ir", [lambda: engine.inspect(args.ir_dir)])

        def _print_inspect(d: Dict[str, Any]) -> None:
            st = d["start"]
            print(
                f"[INSPECT] {d['title']} | Start Map {st['map_id']} ({st['x']},{st['y']}) | "
                f"Maps: {len(d['maps'])}, Events: {d['events_total']}, CommonEvents: {d['common_events']}"
            )
            print(
                f"  DB: actors={d['actors']}, classes={d['classes']}, skills={d['skills']}, "
                f"items={d['items']}, weapons={d['weapons']}, armors={d['armors']}, "
                f"enemies={d['enemies']}, troops={d['troops']}, states={d['states']}"
            )
            for m in d["maps"]:
                print(f"  - Map #{m['id']}: {m['name']} ({m['width']}x{m['height']}, events={m['events']})")

        _emit_output(info, args.json, _print_inspect)
        return 0
    if cmd == "preview":
        sw_overrides = _parse_switch_overrides(args.switch)
        text = _run_supervised(
            engine,
            "preview_map",
            [lambda: engine.preview(args.ir_dir, map_id=args.map_id, png_out=args.png, graphics_dir=args.graphics_dir, switches_state=sw_overrides)],
        )
        if args.json:
            print(json.dumps({"map_id": args.map_id, "ascii": text, "png": str(args.png) if args.png else None}, ensure_ascii=False, indent=2))
        else:
            print(text)
            if args.png:
                print(f"[OK] Saved PNG preview: {args.png}")
        return 0
    if cmd == "sim":
        if int(getattr(args, "fuzz_steps", 0) or 0) > 0:
            snap = _run_supervised(
                engine,
                "simulate_fuzz",
                [lambda: engine.simulate_fuzz(args.ir_dir, steps=args.fuzz_steps, seed=args.seed)],
            )

            def _print_fuzz(s: Dict[str, Any]) -> None:
                fz = s.get("fuzz", {})
                print(
                    f"[FUZZ] steps={fz.get('steps_requested')} seed={fz.get('seed')} "
                    f"tiles={fz.get('unique_tiles')} maps={fz.get('maps_visited')} "
                    f"prompts={fz.get('prompt_interactions')} battles={fz.get('battle_turns')} "
                    f"errors={len(fz.get('errors', []))}"
                )

            _emit_output(snap, args.json, _print_fuzz)
            return 0
        raw_actions = args.actions or ""
        if args.script:
            raw_actions = args.script.read_text(encoding="utf-8")
        actions = [a.strip() for a in raw_actions.replace("\n", ",").split(",") if a.strip() and not a.strip().startswith("#")]
        if not actions:
            raise ValueError("Specify --actions, --script, or --fuzz-steps > 0.")
        snap = _run_supervised(engine, "simulate", [lambda: engine.simulate(args.ir_dir, actions)])

        def _print_sim(s: Dict[str, Any]) -> None:
            print(f"[SIM] Map {s['map_id']} ({s['x']},{s['y']}) | HP {s['hp']}/{s['max_hp']} SP {s.get('sp', 0)}/{s.get('max_sp', 0)} | Gold {s['gold']}G | EXP {s['exp']}")
            for line in s["log"]:
                print(f"  > {line}")

        _emit_output(snap, args.json, _print_sim)
        return 0
    if cmd == "play":
        return launch_player(
            args.ir_dir,
            graphics_dir=args.graphics_dir,
            start_on_title=bool(args.title),
            lang=args.lang,
            use_os_save_dir=bool(args.user_save),
            strict_save=bool(args.strict_save),
        )
    if cmd == "i18n":
        if args.i18n_command == "extract":
            out = _run_supervised(
                engine,
                "i18n_extract",
                [lambda: engine.i18n_extract(args.ir_dir, out_path=args.out, default_locale=args.default_locale)],
            )
            _emit_output(out, args.json)
            return 0
        if args.i18n_command == "apply":
            out = _run_supervised(
                engine,
                "i18n_apply",
                [lambda: engine.i18n_apply(args.ir_dir, locale=args.lang, strings_path=args.file)],
            )
            _emit_output(out, args.json)
            return 0
        parser.parse_args(["i18n", "--help"])
        return 0
    if cmd == "import-mv":
        out = _run_supervised(engine, "import_mv", [lambda: engine.import_mv(args.src_dir, args.out_ir_dir)])
        _emit_output(out, args.json)
        return 0
    if cmd == "import-tiled":
        out = _run_supervised(
            engine,
            "import_tiled",
            [lambda: engine.import_tiled(args.tiled_file, args.ir_dir, map_id=args.map_id, tileset_id=args.tileset_id)],
        )
        _emit_output(out, args.json)
        return 0
    if cmd == "import-vxace":
        out = _run_supervised(engine, "import_vxace", [lambda: engine.import_vxace(args.vxace_dir, args.out_ir_dir)])
        _emit_output(out, args.json)
        return 0
    if cmd == "anim":
        if args.anim_command == "list":
            rows = _run_supervised(engine, "anim_list", [lambda: engine.list_animations(args.ir_dir)])
            _emit_output(rows, args.json)
            return 0
        if args.anim_command == "set":
            cells_data = json.loads(args.cells_json) if getattr(args, "cells_json", None) else None
            timings_data = json.loads(args.timings_json) if getattr(args, "timings_json", None) else None
            res = _run_supervised(
                engine,
                "anim_set",
                [
                    lambda: engine.set_animation(
                        args.ir_dir,
                        anim_id=args.anim_id,
                        name=args.name,
                        animation_name=args.sheet,
                        frame_max=args.frames,
                        se_name=args.se,
                        cells=cells_data,
                        timings=timings_data,
                    )
                ],
            )
            _emit_output(res, args.json)
            return 0
        parser.parse_args(["anim", "--help"])
        return 0
    if cmd == "balance-tune":
        res = _run_supervised(
            engine,
            "balance_tune",
            [
                lambda: engine.batch_tune_balance(
                    args.ir_dir,
                    enemy_hp_mult=args.enemy_hp_mult,
                    enemy_atk_mult=args.enemy_atk_mult,
                    exp_mult=args.exp_mult,
                    gold_mult=args.gold_mult,
                    skill_power_mult=args.skill_power_mult,
                    dry_run=bool(args.dry_run),
                )
            ],
        )
        _emit_output(res, args.json)
        return 0
    if cmd == "battle-formula":
        return _handle_battle_formula(parser, args)
    if cmd == "workbench":
        lines_in = [ln for ln in sys.stdin.read().splitlines()] if not sys.stdin.isatty() else None
        res = engine.workbench(args.ir_dir, stdin_lines=lines_in)
        _emit_output(res, args.json)
        return 0
    if cmd in ("studio", "gui", "editor"):
        from src.harness.studio_gui import launch_studio

        return launch_studio(ir_dir=args.ir_dir, graphics_dir=args.graphics_dir)
    if cmd == "export-release":
        parsed_key = int(args.rgssad_key, 0) if getattr(args, "rgssad_key", None) else None
        res = _run_supervised(
            engine,
            "export_release",
            [
                lambda: engine.export_release(
                    args.ir_dir,
                    args.out_dir,
                    use_rgssad=bool(args.rgssad),
                    lang=args.lang,
                    launcher=bool(args.launcher),
                    build_exe=bool(args.build_exe),
                    strict_assets=bool(args.strict_assets),
                    rgssad_key=parsed_key,
                    rgssad_version=int(args.rgssad_version),
                    icon_path=args.icon,
                    create_zip=bool(args.zip),
                    strict_save=bool(args.strict_save),
                )
            ],
        )

        def _print_rel(d: Dict[str, Any]) -> None:
            print(f"[RELEASE] Exported {d['file_count']} files ({d['archive_mode']}) -> {d['output_dir']}")
            print(f"  Manifest: {d['manifest']}")
            if d.get("zip_path"):
                print(f"  Zip: {d['zip_path']}")

        _emit_output(res, args.json, _print_rel)
        return 0
    return 0


_KNOWN_SUBCOMMANDS = frozenset({
    "sample", "unpack", "pack", "unpack-rgssad", "pack-rgssad",
    "map", "event", "db", "common-event", "apply-blueprint", "undo", "redo",
    "check", "check-assets", "balance-check", "export-graph", "inspect", "preview",
    "sim", "play", "i18n", "import-mv", "import-tiled", "import-vxace",
    "export-release", "anim", "balance-tune", "workbench", "studio", "gui", "editor",
    "battle-formula",
})


def _resolve_frozen_default_launch(raw_argv: List[str], frozen: Optional[bool] = None) -> Optional[Tuple[str, Optional[Path]]]:
    """Resolves bare or folder-drop invocations in frozen/windowed GUI builds into a Studio launch."""
    is_frozen = (bool(getattr(sys, "frozen", False)) or _WINDOWED_STDIO) if frozen is None else bool(frozen)
    if not is_frozen:
        return None
    if not raw_argv:
        return ("studio", None)
    if len(raw_argv) == 1:
        arg0 = raw_argv[0]
        if not arg0.startswith("-") and arg0 not in _KNOWN_SUBCOMMANDS:
            cand = Path(arg0)
            if cand.exists() and cand.is_dir():
                ir_cand = (cand / "ir") if (cand / "ir" / "ir_project.json").is_file() else cand
                return ("studio", ir_cand)
    return None


def main(argv: Optional[List[str]] = None) -> int:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    default_gui = _resolve_frozen_default_launch(raw_argv)
    if default_gui is not None:
        _, target_ir = default_gui
        try:
            from src.harness.studio_gui import launch_studio

            return launch_studio(ir_dir=target_ir)
        except Exception as exc:
            write_crash_report(exc, Path.cwd() / "crash_reports", {"mode": "frozen_studio_launch"})
            try:
                import tkinter as tk
                from tkinter import messagebox

                r = tk.Tk()
                r.withdraw()
                messagebox.showerror("RPGCreatorStudio 실행 오류", f"Studio 시작 중 오류가 발생했습니다:\n{exc}")
                r.destroy()
            except Exception:
                pass
            return 1

    parser = _build_parser()
    args = parser.parse_args(argv)

    if not args.command:
        parser.print_help()
        return 0

    engine = CoreEngine()
    try:
        return _dispatch_command(parser, args, engine)
    except Exception as exc:
        if not isinstance(exc, (ValueError, FileNotFoundError, KeyError, json.JSONDecodeError)):
            ir_cand = getattr(args, "ir_dir", None)
            if isinstance(ir_cand, Path) and ir_cand.is_dir():
                write_crash_report(exc, ir_cand / "crash_reports", {"command": getattr(args, "command", None)})
        if getattr(args, "json", False):
            print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        elif sys.stderr is not None or sys.stdout is not None:
            print(f"[ERROR] {exc}", file=sys.stderr or sys.stdout)
        return 1


if __name__ == "__main__":
    sys.exit(main())
