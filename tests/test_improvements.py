# -*- coding: utf-8 -*-
"""
Modular Regression Suite for All 35 Architectural, Engine, Simulator, and GUI Improvements.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from PIL import Image

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from run_app import _build_parser, main as cli_main
from src.core.battle_engine import (
    apply_state_sets,
    compute_guard_element_multiplier,
    execute_enemy_turn_action,
)
from src.core.editor import (
    delete_db_entry,
    delete_event,
    delete_map,
    export_release_bundle,
    get_db_entry,
    list_events,
    list_maps,
    redo_last_edit,
    run_interactive_workbench,
    set_animation,
    set_db_entry,
    undo_last_edit,
)
from src.core.importers import import_rpgmaker_mv_mz, import_vxace_rxdata
from src.core.ir_converter import pack_ir_to_rmxp
from src.core.rgssad import unpack_rgssad
from src.core.rmxp_marshal import RubyObject, marshal_dumps
from src.core.rmxp_types import RgssTable
from src.core.sample_generator import generate_sample_project
from src.core.simulator import HeadlessGameSession, run_fuzz_playtest
from src.core.utils import load_json, save_json
from src.core.validator import (
    render_map_preview,
    validate_ir_project,
    validate_project_assets,
)
from src.harness import player_gui


class _FakeFont:
    def render(self, text: str, _aa: bool, _color: tuple) -> "FakeSurface":
        return FakeSurface((max(8, len(str(text)) * 6), 16))


class FakeSurface:
    """Minimal pygame.Surface stand-in for deterministic headless rendering checks."""

    def __init__(self, size: tuple[int, int] = (640, 480), flags: int = 0) -> None:
        self._size = (int(size[0]), int(size[1]))
        self.flags = int(flags)
        self.blits: list[tuple] = []
        self.fills: list[tuple] = []
        self.alpha = 255

    def get_size(self) -> tuple[int, int]:
        return self._size

    def get_width(self) -> int:
        return self._size[0]

    def get_height(self) -> int:
        return self._size[1]

    def convert_alpha(self) -> "FakeSurface":
        return self

    def copy(self) -> "FakeSurface":
        return FakeSurface(self._size)

    def subsurface(self, rect) -> "FakeSurface":
        if hasattr(rect, "width"):
            return FakeSurface((int(rect.width), int(rect.height)))
        return FakeSurface((int(rect[2]), int(rect[3])))

    def set_alpha(self, val: int) -> None:
        self.alpha = int(val)

    def fill(self, color, rect=None, special_flags: int = 0) -> None:
        self.fills.append((color, rect, special_flags))

    def blit(self, surf, dest, area=None, special_flags: int = 0) -> None:
        self.blits.append((surf, dest, area, special_flags))


class _FakeRect:
    def __init__(self, x: int, y: int, w: int, h: int) -> None:
        self.x, self.y, self.w, self.h = int(x), int(y), int(w), int(h)
        self.width, self.height = self.w, self.h

    def colliderect(self, other) -> bool:
        return True


class _FakeDraw:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def rect(self, _surf, color, rect, width: int = 0, border_radius: int = 0) -> None:
        self.calls.append(("rect", color, rect, width))

    def circle(self, _surf, color, center, radius: int, width: int = 0) -> None:
        self.calls.append(("circle", color, center, radius))

    def line(self, _surf, color, p1, p2, width: int = 1) -> None:
        self.calls.append(("line", color, p1, p2))


class _FakeTransform:
    def smoothscale(self, surf, size):
        return FakeSurface(size)

    def scale(self, surf, size):
        return FakeSurface(size)

    def rotate(self, surf, angle):
        return surf.copy()


class _FakePygame:
    SRCALPHA = 1
    BLEND_ADD = 1
    BLEND_SUB = 2
    BLEND_RGBA_MULT = 4
    K_UP = 273
    K_DOWN = 274
    K_LEFT = 276
    K_RIGHT = 275
    K_RETURN = 13
    K_SPACE = 32
    K_ESCAPE = 27
    K_TAB = 9
    K_F1 = 282
    K_F5 = 286
    K_F9 = 290
    K_F11 = 292
    K_w = 119
    K_a = 97
    K_s = 115
    K_d = 100
    K_z = 122
    K_x = 120
    K_m = 109
    K_j = 106
    K_q = 113
    K_e = 101
    K_p = 112

    def __init__(self) -> None:
        self.draw = _FakeDraw()
        self.transform = _FakeTransform()
        self.font_obj = _FakeFont()
        self.Surface = FakeSurface
        self.Rect = _FakeRect
        self.font = type(
            "_FontMod",
            (),
            {
                "Font": staticmethod(lambda path, size: self.font_obj),
                "SysFont": staticmethod(lambda name, size: self.font_obj),
            },
        )()

    def key(self):
        return None


def _run_cli(argv: list[str]) -> tuple[int, str]:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = cli_main(argv)
    return rc, buf.getvalue()


class TestCoreAndCliImprovements(unittest.TestCase):
    def test_packaging_files_and_atomic_save_json(self) -> None:
        root = Path(PROJECT_ROOT)
        self.assertTrue((root / "requirements.txt").is_file())
        self.assertTrue((root / "pyproject.toml").is_file())
        req_text = (root / "requirements.txt").read_text(encoding="utf-8")
        self.assertIn("pygame>=", req_text)
        self.assertIn("Pillow>=", req_text)
        pyproj_text = (root / "pyproject.toml").read_text(encoding="utf-8")
        self.assertIn('rpgcreator = "run_app:main"', pyproj_text)

        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "atomic.json"
            save_json(target, {"ok": True, "n": 42})
            self.assertEqual(load_json(target), {"ok": True, "n": 42})
            with mock.patch("src.core.utils.json.dumps", side_effect=RuntimeError("boom")):
                with self.assertRaises(RuntimeError):
                    save_json(target, {"ok": False})
            self.assertEqual(load_json(target), {"ok": True, "n": 42})
            leftovers = [p.name for p in Path(td).iterdir() if p.name.startswith(".tmp_")]
            self.assertEqual(leftovers, [])

    def test_crud_redo_global_json_anim_workbench_and_release_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            proj_root = Path(td) / "proj"
            generate_sample_project(proj_root)
            ir_dir = proj_root / "ir"

            # Global --json usable AFTER subcommands as well as before
            rc1, out1 = _run_cli(["map", "list", str(ir_dir), "--json"])
            self.assertEqual(rc1, 0)
            maps_data = json.loads(out1)
            self.assertTrue(isinstance(maps_data, list) and len(maps_data) >= 1)

            rc_get, out_get = _run_cli(["--json", "db", "get", str(ir_dir), "--table", "actors", "--id", "1"])
            self.assertEqual(rc_get, 0)
            actor1 = json.loads(out_get)
            self.assertEqual(actor1["fields"]["id"], 1)

            # Edit -> Undo -> Redo roundtrip using delete_event and undo/redo CLI
            evs_m1 = list_events(ir_dir, 1)
            self.assertGreaterEqual(len(evs_m1), 1)
            target_eid = int(evs_m1[0]["id"])
            del_ev = delete_event(ir_dir, 1, target_eid)
            self.assertTrue(del_ev["deleted"])
            self.assertEqual(del_ev["event_id"], target_eid)
            self.assertFalse(any(int(e["id"]) == target_eid for e in list_events(ir_dir, 1)))
            undo_res1 = undo_last_edit(ir_dir)
            self.assertTrue(undo_res1["restored"])
            self.assertTrue(any(int(e["id"]) == target_eid for e in list_events(ir_dir, 1)))
            rc_redo, out_redo = _run_cli(["redo", str(ir_dir), "--json"])
            self.assertEqual(rc_redo, 0)
            self.assertTrue(json.loads(out_redo)["restored"])
            self.assertFalse(any(int(e["id"]) == target_eid for e in list_events(ir_dir, 1)))
            undo_last_edit(ir_dir)
            self.assertTrue(any(int(e["id"]) == target_eid for e in list_events(ir_dir, 1)))

            # Start map (map 1) cannot be deleted, secondary map can (and restore via undo for later export)
            with self.assertRaises(ValueError):
                delete_map(ir_dir, 1)
            non_start = [m["map_id"] for m in list_maps(ir_dir) if int(m["map_id"]) != 1]
            if non_start:
                del_m = delete_map(ir_dir, non_start[0])
                self.assertEqual(del_m["map_id"], non_start[0])
                undo_last_edit(ir_dir)

            # Delete non-slot-1 db entry
            set_db_entry(ir_dir, "items", 2, {"name": "임시아이템", "price": 50})
            self.assertEqual(get_db_entry(ir_dir, "items", 2)["fields"]["name"], "임시아이템")
            delete_db_entry(ir_dir, "items", 2)
            self.assertIsNone(get_db_entry(ir_dir, "items", 2))
            u2 = undo_last_edit(ir_dir)
            self.assertTrue(u2["restored"])
            redo_last_edit(ir_dir)
            self.assertIsNone(get_db_entry(ir_dir, "items", 2))

            # anim set with --cells-json / --timings-json
            cells = [[{"pattern": 0, "x": 10, "y": -20, "zoom": 100, "angle": 0, "mirror": 0, "opacity": 255, "blend_type": 0}]]
            timings = [{"frame": 0, "se_name": "Slash1", "flash_scope": 1, "flash_color": [255, 0, 0, 128]}]
            rc_anim, out_anim = _run_cli([
                "anim", "set", str(ir_dir), "--id", "3",
                "--name", "파워슬래시",
                "--cells-json", json.dumps(cells),
                "--timings-json", json.dumps(timings),
                "--json",
            ])
            self.assertEqual(rc_anim, 0)
            anim_entry = get_db_entry(ir_dir, "animations", 3)
            self.assertEqual(anim_entry["fields"]["frames"][0]["fields"]["cell_max"], 1)
            self.assertEqual(len(anim_entry["fields"]["frames"][0]["fields"]["cell_data"]["data"]), 8)
            self.assertEqual(len(anim_entry["fields"]["timings"]), 1)

            # Workbench extended commands (redo / anim / balance-tune / export-release)
            rel_wb = Path(td) / "wb_rel"
            wb = run_interactive_workbench(ir_dir, stdin_lines=[
                "set-db actors 1 name=워크벤치",
                "undo",
                "redo",
                "anim set 4 검격",
                "balance-tune 1.5 1.2",
                f"export-release {rel_wb}",
                "quit",
            ])
            self.assertGreaterEqual(wb["commands_run"], 6)
            self.assertTrue(any(ln.startswith("released=") for ln in wb["logs"]))

            # export_release_bundle v2 + Fonts + Icon + Zip
            fonts_dir = ir_dir / "Fonts"
            fonts_dir.mkdir(exist_ok=True)
            (fonts_dir / "gamefont.ttf").write_bytes(b"FAKEFONT")
            icon_src = Path(td) / "icon.ico"
            icon_src.write_bytes(b"FAKEICON")
            rel_out = Path(td) / "release_v2"
            rel_info = export_release_bundle(
                ir_dir,
                rel_out,
                use_rgssad=True,
                rgssad_version=2,
                launcher=True,
                icon_path=icon_src,
                create_zip=True,
            )
            self.assertEqual(rel_info["rgssad_version"], 2)
            self.assertEqual(rel_info["archive_mode"], "rgss2a")
            self.assertTrue((rel_out / "Game.rgss2a").is_file())
            unpacked_v2 = Path(td) / "unpacked_v2"
            unpack_rgssad(rel_out / "Game.rgss2a", unpacked_v2)
            self.assertTrue((unpacked_v2 / "Fonts" / "gamefont.ttf").is_file())
            self.assertTrue((rel_out / "icon.ico").is_file())
            spec_text = (rel_out / "build_game.spec").read_text(encoding="utf-8")
            self.assertIn("icon='icon.ico'", spec_text)
            self.assertIn("src.core.simulator", spec_text)
            zip_path = Path(rel_info["zip_archive"])
            self.assertTrue(zip_path.is_file())
            with zipfile.ZipFile(zip_path, "r") as zf:
                names = zf.namelist()
                self.assertIn("Game.rgss2a", names)
                self.assertIn("build_game.spec", names)


class TestValidatorAndImporterImprovements(unittest.TestCase):
    def test_validator_warnings_assets_parallax_and_ascii_preview(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            proj_root = Path(td) / "val"
            generate_sample_project(proj_root)
            ir_dir = proj_root / "ir"

            # Set up map 1 interior (3, 3) water autotile (48) + (4, 3) bush flag (0x40)
            m1_path = ir_dir / "maps" / "map_001.json"
            m1 = load_json(m1_path)
            m1["layers"]["layer_0"][3][3] = 48
            m1["layers"]["layer_1"][3][3] = 0
            m1["layers"]["layer_2"][3][3] = 0
            m1["layers"]["layer_0"][3][4] = 389
            m1["layers"]["layer_1"][3][4] = 0
            m1["layers"]["layer_2"][3][4] = 0
            m1["parallax_name"] = "MissingSky"
            save_json(m1_path, m1)

            ts_path = ir_dir / "database" / "tilesets.json"
            ts_list = load_json(ts_path)
            ts_list[1]["fields"]["passages"]["data"][389] = 0x40  # bush flag
            ts_list[1]["fields"]["passages"]["data"][48] = 0x0F   # impassable water so ~ renders cleanly
            save_json(ts_path, ts_list)

            # Inject invalid enemy drop + AI skill
            en_path = ir_dir / "database" / "enemies.json"
            ens = load_json(en_path)
            ens[1]["fields"]["item_id"] = 999
            ens[1]["fields"]["actions"] = [
                {"class": "RPG::Enemy::Action", "fields": {"kind": 1, "skill_id": 888, "rating": 5}}
            ]
            save_json(en_path, ens)

            val = validate_ir_project(ir_dir)
            self.assertTrue(any("undefined drop item 999" in w for w in val.warnings))
            self.assertTrue(any("undefined skill 888" in w for w in val.warnings))

            asset_res = validate_project_assets(ir_dir)
            self.assertTrue(any("Panoramas/MissingSky" in m for m in asset_res["missing_graphics"]))

            prev = render_map_preview(ir_dir, 1)
            ascii_lines = prev.splitlines()[1:]
            self.assertEqual(ascii_lines[3][3], "~")
            self.assertEqual(ascii_lines[3][4], ",")

    def test_mv_and_vxa_canonical_ir_roundtrip_to_rmxp(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            mv_dir = root / "mv"
            (mv_dir / "data").mkdir(parents=True)
            (mv_dir / "data" / "System.json").write_text(json.dumps({"gameTitle": "MVCan", "partyMembers": [1]}), encoding="utf-8")
            (mv_dir / "data" / "Tilesets.json").write_text(json.dumps([None, {"id": 1, "name": "T1", "flags": [0] * 10}]), encoding="utf-8")
            (mv_dir / "data" / "MapInfos.json").write_text(json.dumps([None, {"id": 1, "name": "M1"}]), encoding="utf-8")
            (mv_dir / "data" / "Map001.json").write_text(json.dumps({
                "width": 2, "height": 2, "tilesetId": 1,
                "autoplayBgm": True, "bgm": {"name": "Town1", "volume": 85, "pitch": 100},
                "autoplayBgs": True, "bgs": {"name": "River", "volume": 60, "pitch": 100},
                "encounterList": [{"troopId": 4, "weight": 10}],
                "data": [0] * (2 * 2 * 6), "events": [None],
            }), encoding="utf-8")
            mv_ir = root / "mv_ir"
            import_rpgmaker_mv_mz(mv_dir, mv_ir)
            m_ir = load_json(mv_ir / "maps" / "map_001.json")
            self.assertEqual(m_ir["bgm"]["class"], "RPG::AudioFile")
            self.assertEqual(m_ir["bgm"]["fields"]["name"], "Town1")
            self.assertEqual(m_ir["bgs"]["fields"]["name"], "River")
            self.assertEqual(m_ir["encounter_list"], [4])

            vxa_dir = root / "vxa"
            (vxa_dir / "Data").mkdir(parents=True)
            (vxa_dir / "Data" / "System.rvdata2").write_bytes(marshal_dumps(RubyObject("RPG::System", {"@game_title": "VXCan", "@party_members": [1]})))
            (vxa_dir / "Data" / "Tilesets.rvdata2").write_bytes(marshal_dumps([
                None,
                RubyObject("RPG::Tileset", {"@id": 1, "@name": "V1", "@tileset_names": ["", ""], "@flags": RgssTable.create(50, 1, 1, dims=1, fill=0).to_user_def()}),
            ]))
            (vxa_dir / "Data" / "MapInfos.rvdata2").write_bytes(marshal_dumps({1: RubyObject("RPG::MapInfo", {"@name": "VMap"})}))
            (vxa_dir / "Data" / "Map001.rvdata2").write_bytes(marshal_dumps(RubyObject("RPG::Map", {
                "@width": 2, "@height": 2, "@tileset_id": 1,
                "@autoplay_bgm": True, "@bgm": RubyObject("RPG::BGM", {"@name": "Field", "@volume": 90, "@pitch": 100}),
                "@encounter_list": [RubyObject("RPG::Map::Encounter", {"@troop_id": 2})],
                "@data": RgssTable.create(2, 2, 3, dims=3, fill=0).to_user_def(), "@events": {},
            })))
            vxa_ir = root / "vxa_ir"
            import_vxace_rxdata(vxa_dir, vxa_ir)
            v_ir = load_json(vxa_ir / "maps" / "map_001.json")
            self.assertEqual(v_ir["bgm"]["class"], "RPG::AudioFile")
            self.assertEqual(v_ir["encounter_list"], [2])

            rmxp_out = root / "rmxp_packed"
            pack_ir_to_rmxp(mv_ir, rmxp_out)
            self.assertTrue((rmxp_out / "Data" / "Map001.rxdata").is_file())


class TestBattleAndSimulatorImprovements(unittest.TestCase):
    def test_battle_engine_guards_simulator_battle_caps_counter_routes_and_fuzz(self) -> None:
        # 1. battle_engine state/element armor guards
        applied: list[int] = []
        apply_state_sets(applied, [2, 3], [], guard_states=[3])
        self.assertEqual(applied, [2])
        self.assertEqual(compute_guard_element_multiplier([2], [2, 4]), 0.5)
        self.assertEqual(compute_guard_element_multiplier([5], [2, 4]), 1.0)

        with tempfile.TemporaryDirectory() as td:
            proj_root = Path(td) / "sim"
            generate_sample_project(proj_root)
            ir_dir = proj_root / "ir"

            # Set up weapon plus_state_set, armor guard_element_set / guard_state_set, and skill occasion=2 (menu-only)
            weapons = load_json(ir_dir / "database" / "weapons.json")
            weapons[1]["fields"]["plus_state_set"] = [2]
            weapons[1]["fields"]["element_set"] = [1]
            save_json(ir_dir / "database" / "weapons.json", weapons)

            armors = load_json(ir_dir / "database" / "armors.json")
            armors[1]["fields"]["guard_element_set"] = [2]
            armors[1]["fields"]["guard_state_set"] = [2]
            save_json(ir_dir / "database" / "armors.json", armors)

            skills = load_json(ir_dir / "database" / "skills.json")
            while len(skills) <= 3:
                skills.append(None)
            skills[2] = {
                "class": "RPG::Skill",
                "fields": {
                    "id": 2, "name": "메뉴전용", "occasion": 2, "sp_cost": 1,
                    "power": 20, "atk_f": 0, "int_f": 100, "hit": 100, "pdef_f": 0, "mdef_f": 0,
                    "element_set": [], "plus_state_set": [], "minus_state_set": [],
                    "animation2_id": 4,
                },
            }
            skills[3] = {
                "class": "RPG::Skill",
                "fields": {
                    "id": 3, "name": "전투마법", "occasion": 1, "sp_cost": 9999,
                    "power": 40, "atk_f": 0, "int_f": 100, "hit": 100, "pdef_f": 0, "mdef_f": 0,
                    "element_set": [2], "plus_state_set": [], "minus_state_set": [],
                    "animation2_id": 7, "menu_se": {"class": "RPG::AudioFile", "fields": {"name": "Fire1", "volume": 80, "pitch": 100}},
                },
            }
            save_json(ir_dir / "database" / "skills.json", skills)

            # Configure map counter tile (0x80) and NPC routes
            m1_path = ir_dir / "maps" / "map_001.json"
            m1 = load_json(m1_path)
            m1["layers"]["layer_0"][5][6] = 390
            ts_path = ir_dir / "database" / "tilesets.json"
            ts_list = load_json(ts_path)
            ts_list[1]["fields"]["passages"]["data"][390] = 0x80  # counter flag
            save_json(ts_path, ts_list)
            save_json(m1_path, m1)

            session = HeadlessGameSession(ir_dir)
            # Caps: gold 9_999_999 and inventory 99
            session._run_commands([{"op": "change_gold", "mode": "add", "amount": 99_999_999}], 0, session.map_id, 0)
            self.assertEqual(session.gold, 9_999_999)
            session._apply_inventory_delta(session.inventory, 1, "add", 500)
            self.assertEqual(session.inventory[1], 99)

            # Counter flag helper check
            self.assertTrue(session.tile_has_passage_flag(6, 5, 0x80))

            # Save slot deletion & autosave retry
            p1 = session.save_to_file(slot=1)
            self.assertTrue(p1.is_file())
            self.assertTrue(session.delete_slot(1))
            self.assertFalse(p1.is_file())
            self.assertFalse(session.delete_slot(1))

            session.x, session.y = 5, 5
            session.save_autosave()
            session.game_over = True
            snap = session.step("gameover_retry")
            self.assertFalse(snap.get("game_over"))
            self.assertFalse(session.game_over)
            self.assertEqual((session.x, session.y), (5, 5))

            # Skill filtering in battle & [SP부족] tag + AGI turn order + animation2_id queueing
            session.actor_states[session.lead_actor_id]["learned_skills"] = [1, 2, 3]
            session._start_interactive_battle(troop_id=1)
            bs = session.pending.battle_state
            usable_ids = session._battle_usable_skill_ids(session.lead_actor_id)
            self.assertIn(1, usable_ids)
            self.assertNotIn(2, usable_ids)  # occasion == 2 excluded in battle
            self.assertIn(3, usable_ids)

            turn_order = session.get_battle_turn_order(bs)
            self.assertGreaterEqual(len(turn_order), 2)

            session.step("choose:1")  # open skill submenu
            self.assertTrue(any("[SP부족]" in c for c in session.pending.choices))
            session.step("choose:" + str(len(session.pending.choices) - 1))  # back to main battle menu
            session._queue_battle_media(skills[3]["fields"], 0, 0)
            self.assertTrue(any(ev.get("animation_id") == 7 for ev in session.pending_battle_animations))
            self.assertTrue(any(se.get("name") == "Fire1" for se in session.played_se_queue))

            # Check guard element reduction + guard state immunity on enemy turn
            bs["actor_cursor"] = 0
            lead_id = session.lead_actor_id
            session.actor_states[lead_id]["equip_armor_ids"] = [1, 0, 0, 0]
            self.assertEqual(session._actor_guard_elements(lead_id), [2])
            self.assertEqual(session._actor_guard_states(lead_id), [2])
            enemy_msg = execute_enemy_turn_action(
                {"id": 1, "name": "Slime", "atk": 100, "agi": 10, "int": 10, "hp": 50, "max_hp": 50, "actions": []},
                bs,
                set(),
                [lead_id],
                0,
                session.skills_db,
                session._get_actor_state,
                lambda aid: 10,
                get_actor_guard_elements=session._actor_guard_elements,
                get_actor_guard_states=session._actor_guard_states,
            )
            self.assertTrue(bool(enemy_msg))

            # End battle cleanly and test non-cyclic move_type=3
            session.pending = None
            m_cur = session.current_map()
            mover_ev = {
                "id": 90,
                "name": "Mover",
                "x": 3,
                "y": 3,
                "pages": [{
                    "condition": {},
                    "move_type": 3,
                    "move_route": {
                        "fields": {
                            "repeat": False,
                            "skippable": True,
                            "list": [
                                {"fields": {"code": 2, "parameters": []}},
                                {"fields": {"code": 0, "parameters": []}},
                            ],
                        }
                    },
                    "list": [{"code": 0, "parameters": []}],
                }],
            }
            m_cur["events"].append(mover_ev)
            session._active_move_routes.pop((session.map_id, 90), None)
            session.advance_npc_routes(target_event_id=90)
            self.assertEqual(int(mover_ev.get("_page_route_idx", 0)), 1)
            pos_after_step1 = (int(mover_ev["x"]), int(mover_ev["y"]))
            session.advance_npc_routes(target_event_id=90)
            self.assertEqual((int(mover_ev["x"]), int(mover_ev["y"])), pos_after_step1)

            # Fuzz runner + CLI --fuzz-steps
            fuzz = run_fuzz_playtest(ir_dir, steps=60, seed=7)
            self.assertEqual(fuzz["fuzz"]["steps_requested"], 60)
            self.assertEqual(fuzz["fuzz"]["errors"], [])

            rc_fuzz, out_fuzz = _run_cli(["sim", str(ir_dir), "--fuzz-steps", "30", "--seed", "5", "--json"])
            self.assertEqual(rc_fuzz, 0)
            self.assertEqual(json.loads(out_fuzz)["fuzz"]["steps_requested"], 30)


class TestGuiAndRenderingImprovements(unittest.TestCase):
    def test_i18n_options_slots_playtime_font_windowskin_caches_and_bush(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            proj_root = Path(td) / "gui"
            generate_sample_project(proj_root)
            ir_dir = proj_root / "ir"
            session = HeadlessGameSession(ir_dir)
            session.play_time_ms = 65_000.0  # 01:05

            # i18n lookup
            self.assertEqual(player_gui._resolve_ui_lang(session, {"lang": "en"}), "en")
            self.assertEqual(player_gui._ui_tr("en", "menu_quests"), "Quest Journal")
            self.assertEqual(player_gui._ui_tr("ko", "menu_quests"), "퀘스트 일지")

            # Custom Fonts/*.ttf loader fallback check
            fonts_dir = ir_dir / "Fonts"
            fonts_dir.mkdir(exist_ok=True)
            font_path = fonts_dir / "custom.ttf"
            font_path.write_bytes(b"TTF")
            loaded_paths: list[str] = []
            fpg = _FakePygame()
            fpg.font = type(
                "_CustomFontMod",
                (),
                {
                    "Font": staticmethod(lambda path, size: loaded_paths.append(str(path)) or _FakeFont()),
                    "SysFont": staticmethod(lambda name, size: _FakeFont()),
                },
            )()
            player_gui._load_ui_font(fpg, ir_dir, 16)
            self.assertEqual(loaded_paths, [str(font_path)])

            # System SE queueing
            session.sys_fields["decision_se"] = {"class": "RPG::AudioFile", "fields": {"name": "Decide1", "volume": 85, "pitch": 100}}
            player_gui._queue_system_se(session, "decision")
            self.assertEqual(session.played_se_queue[-1]["name"], "Decide1")

            # Options split volumes & keybinds (cancel/menu)
            settings = player_gui.load_player_settings(ir_dir)
            opts = player_gui.build_title_menu_options(session, mode="options", settings=settings, include_controls=True)
            labels = list(opts)
            self.assertTrue(any("BGS 볼륨" in lb for lb in labels))
            self.assertTrue(any("ME 볼륨" in lb for lb in labels))
            self.assertTrue(any("취소 키 지정" in lb for lb in labels))
            self.assertTrue(any("메뉴 키 지정" in lb for lb in labels))

            cancel_idx = next(i for i, o in enumerate(opts) if "취소 키 지정" in o)
            cap_res = player_gui.process_title_selection(session, "options", cancel_idx, True, settings, include_controls=True)
            self.assertEqual(cap_res.get("capture_target"), "cancel")

            bgs_idx = next(i for i, o in enumerate(opts) if "BGS 볼륨" in o)
            before_bgs = int(settings["bgs_volume"])
            player_gui.process_title_selection(session, "options", bgs_idx, True, settings, adjust_delta=-1, include_controls=True)
            self.assertNotEqual(int(settings["bgs_volume"]), before_bgs)

            # Save slot rows with autosave
            session.save_autosave()
            rows_load = player_gui.build_save_slot_rows(session, include_autosave=True)
            self.assertEqual(len(rows_load), 10)
            self.assertEqual(rows_load[-1]["slot"], 0)
            rows_save = player_gui.build_save_slot_rows(session, include_autosave=False)
            self.assertEqual(len(rows_save), 9)

            # Main menu overlay play_time_ms formatting (01:05)
            rendered_texts: list[str] = []

            class _CapFont(_FakeFont):
                def render(self, text: str, _aa: bool, _color: tuple) -> FakeSurface:
                    rendered_texts.append(str(text))
                    return FakeSurface((60, 16))

            screen = FakeSurface((640, 480))
            player_gui._draw_main_menu_overlay(fpg, screen, _CapFont(), _CapFont(), session, 0)
            self.assertTrue(any("01:05" in t for t in rendered_texts))

            # 9-slice _draw_window_box with valid >=96x96 Windowskin surface
            wskin_surf = FakeSurface((192, 128))
            screen.blits.clear()
            player_gui._draw_window_box(
                fpg,
                screen,
                (10, 10, 200, 100),
                player_gui._WINDOWSKIN_PRESETS["default"],
                load_img=lambda name: wskin_surf if name.startswith("Windowskins/") else None,
                windowskin_name="BlueSkin",
            )
            self.assertEqual(len(screen.blits), 1)
            cached_box = player_gui._WINDOW_BOX_CACHE[(id(wskin_surf), 200, 100)][0]
            self.assertGreaterEqual(len(cached_box.blits), 9)

            # _draw_pictures cache & blend_type
            pic_surf = FakeSurface((64, 64))
            pic_cache: dict = {}
            session.pictures[1] = {
                "name": "Portrait",
                "origin": 0,
                "x": 100,
                "y": 80,
                "zoom_x": 100,
                "zoom_y": 100,
                "opacity": 200,
                "blend_type": 1,
                "angle": 15.0,
                "tone": (0, 0, 0, 0),
            }
            screen.blits.clear()
            player_gui._draw_pictures(
                fpg,
                screen,
                lambda name: pic_surf if name == "Pictures/Portrait" else None,
                _FakeFont(),
                session,
                pic_cache=pic_cache,
            )
            self.assertEqual(len(pic_cache), 1)
            self.assertEqual(screen.blits[-1][3], fpg.BLEND_ADD)

            # _draw_minimap terrain cache reuse
            mm_cache: dict = {}
            player_gui._draw_minimap(fpg, screen, session, minimap_cache=mm_cache)
            self.assertEqual(len(mm_cache), 1)
            cached_surf = next(iter(mm_cache.values()))
            player_gui._draw_minimap(fpg, screen, session, minimap_cache=mm_cache)
            self.assertIs(next(iter(mm_cache.values())), cached_surf)

            # _draw_panorama prefers map_ir["parallax_name"] & anim_tick auto-scroll
            pano_used: list[str] = []
            screen.blits.clear()
            player_gui._draw_panorama(
                fpg,
                screen,
                lambda name: pano_used.append(name) or FakeSurface((128, 128)),
                {"panorama_name": "FallbackSky"},
                0,
                0,
                map_ir={"parallax_name": "MapSky", "parallax_loop_x": True, "parallax_sx": 4},
                anim_tick=16,
            )
            self.assertEqual(pano_used, ["Panoramas/MapSky"])
            self.assertGreaterEqual(len(screen.blits), 1)

    def test_launch_player_offscreen_smoke_and_playtime_tick(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            proj_root = Path(td) / "gui_live"
            generate_sample_project(proj_root)
            ir_dir = proj_root / "ir"
            seen: dict = {}

            def _on_frame(tick: int, _surf, sess: HeadlessGameSession) -> None:
                seen["tick"] = tick
                seen["play_time_ms"] = float(getattr(sess, "play_time_ms", 0.0))

            old_vd = os.environ.get("SDL_VIDEODRIVER")
            old_ad = os.environ.get("SDL_AUDIODRIVER")
            os.environ["SDL_VIDEODRIVER"] = "dummy"
            os.environ["SDL_AUDIODRIVER"] = "dummy"
            try:
                rc = player_gui.launch_player(ir_dir, max_frames=3, start_on_title=False, on_frame=_on_frame)
            finally:
                if old_vd is None:
                    os.environ.pop("SDL_VIDEODRIVER", None)
                else:
                    os.environ["SDL_VIDEODRIVER"] = old_vd
                if old_ad is None:
                    os.environ.pop("SDL_AUDIODRIVER", None)
                else:
                    os.environ["SDL_AUDIODRIVER"] = old_ad

            self.assertEqual(rc, 0)
            self.assertEqual(seen.get("tick"), 2)
            self.assertGreater(seen.get("play_time_ms", 0.0), 0.0)

    def test_qa_workbench_quoting_add_event_repl_survival_and_cli_undo_chain(self) -> None:
        from src.core.editor import create_map, list_common_events, list_maps, set_common_event

        with tempfile.TemporaryDirectory() as td:
            proj_root = Path(td) / "qa_wb"
            generate_sample_project(proj_root)
            ir_dir = proj_root / "ir"

            # Major-1, Minor-7: add-event + mid-token quotes + unclosed quote REPL survival
            wb = run_interactive_workbench(
                ir_dir,
                stdin_lines=[
                    'add-event 1 "NPC With Spaces" 3 3 "Hello from workbench"',
                    'set-db actors 1 name="Hero Spaced"',
                    'add-event 1 "Unclosed 3 3 oops',
                    "maps",
                    "quit",
                ],
            )
            self.assertTrue(any(ln.startswith("added_event=#") for ln in wb["logs"]))
            self.assertTrue(any("[ERROR]" in ln for ln in wb["logs"]))
            self.assertTrue(any("Town" in ln or "#1:" in ln for ln in wb["logs"]))
            self.assertEqual(get_db_entry(ir_dir, "actors", 1)["fields"]["name"], "Hero Spaced")
            evs = list_events(ir_dir, 1)
            self.assertTrue(any(e["name"] == "NPC With Spaces" for e in evs))

            # Major-3 & Minor-6: 7-step CLI undo chain + map_infos.entries cleanup on delete_map
            m_before = len(list_maps(ir_dir))
            rc, out = _run_cli(["map", "create", str(ir_dir), "--name", "UndoChainMap", "--json"])
            self.assertEqual(rc, 0)
            new_mid = json.loads(out)["map_id"]
            self.assertEqual(len(list_maps(ir_dir)), m_before + 1)

            rc, _ = _run_cli(["map", "paint", str(ir_dir), "--map-id", str(new_mid), "--tile", "390", "--point", "1,1", "--json"])
            self.assertEqual(rc, 0)
            rc, _ = _run_cli(["event", "add", str(ir_dir), "--map-id", str(new_mid), "--name", "ChainEV", "--x", "2", "--y", "2", "--say", "hi", "--json"])
            self.assertEqual(rc, 0)
            rc, _ = _run_cli(["db", "set", str(ir_dir), "--table", "enemies", "--id", "1", "--field", "maxhp=999", "--json"])
            self.assertEqual(rc, 0)
            rc, _ = _run_cli(["common-event", "set", str(ir_dir), "--id", "5", "--name", "ChainCE", "--say", "ce", "--json"])
            self.assertEqual(rc, 0)
            rc, _ = _run_cli(["anim", "set", str(ir_dir), "--id", "5", "--name", "ChainAnim", "--json"])
            self.assertEqual(rc, 0)
            rc, _ = _run_cli(["balance-tune", str(ir_dir), "--enemy-hp-mult", "2.0", "--json"])
            self.assertEqual(rc, 0)

            for _ in range(7):
                u = undo_last_edit(ir_dir)
                self.assertTrue(u["restored"])
            self.assertEqual(len(list_maps(ir_dir)), m_before)
            self.assertNotEqual(get_db_entry(ir_dir, "enemies", 1)["fields"]["maxhp"], 999)
            self.assertFalse(any(int(ce["id"]) == 5 for ce in list_common_events(ir_dir)))

            # Minor-6: delete_map cleans ir_project.json map_infos.entries
            c2 = create_map(ir_dir, name="TempDeleteMap")
            del_id = int(c2["map_id"])
            delete_map(ir_dir, del_id)
            proj_after = json.loads((ir_dir / "ir_project.json").read_text(encoding="utf-8"))
            entry_ids = [int(p[0]) for p in proj_after["map_infos"]["entries"]]
            self.assertNotIn(del_id, entry_ids)

    def test_qa_importers_validation_empty_mv_and_rgssad_truncated_index(self) -> None:
        from src.core.importers import import_rpgmaker_mv_mz, import_vxace_rxdata
        from src.core.rgssad import RgssadArchiveReader, pack_rgssad

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            out_bad = root / "bad_vxa_out"
            with self.assertRaises(FileNotFoundError):
                import_vxace_rxdata(root / "does_not_exist", out_bad)
            self.assertFalse(out_bad.exists())

            empty_vxa = root / "empty_vxa"
            empty_vxa.mkdir()
            with self.assertRaises(ValueError):
                import_vxace_rxdata(empty_vxa, out_bad)
            self.assertFalse(out_bad.exists())

            # Minor-5: empty MV map data: [] with 0x0 size
            mv_src = root / "mv_src"
            (mv_src / "data").mkdir(parents=True)
            (mv_src / "data" / "System.json").write_text(json.dumps({"gameTitle": "EmptyMV"}), encoding="utf-8")
            (mv_src / "data" / "Map001.json").write_text(
                json.dumps({"width": 0, "height": 0, "data": [], "events": []}),
                encoding="utf-8",
            )
            mv_out = root / "mv_ir"
            mv_res = import_rpgmaker_mv_mz(mv_src, mv_out)
            self.assertEqual(mv_res["maps_imported"], 1)

            # Edge-2: truncated RGSSAD v1 and v3 archive raises ValueError in RgssadArchiveReader
            src_files = root / "rgss_src"
            (src_files / "Data").mkdir(parents=True)
            (src_files / "Data" / "test.bin").write_bytes(b"1234567890")
            for ver, ext in ((1, ".rgssad"), (3, ".rgss3a")):
                arc = root / f"test_v{ver}{ext}"
                pack_rgssad(src_files, arc, version=ver)
                raw = arc.read_bytes()
                trunc_arc = root / f"trunc_v{ver}{ext}"
                trunc_arc.write_bytes(raw[: len(raw) - 5])
                with self.assertRaises(ValueError):
                    RgssadArchiveReader(trunc_arc)

    def test_ux_player_key_aliases_and_sample_quest_lifecycle(self) -> None:
        import pygame

        self.assertEqual(player_gui._resolve_custom_key_code(pygame, "space"), pygame.K_SPACE)
        self.assertEqual(player_gui._resolve_custom_key_code(pygame, "return"), pygame.K_RETURN)
        self.assertEqual(player_gui._resolve_custom_key_code(pygame, "enter"), pygame.K_RETURN)
        self.assertEqual(player_gui._resolve_custom_key_code(pygame, "left shift"), pygame.K_LSHIFT)
        self.assertEqual(player_gui._resolve_custom_key_code(pygame, "rshift"), pygame.K_RSHIFT)
        self.assertEqual(player_gui._resolve_custom_key_code(pygame, "backspace"), pygame.K_BACKSPACE)

        with tempfile.TemporaryDirectory() as td:
            proj_root = Path(td) / "quest_sample"
            generate_sample_project(proj_root)
            ir_dir = proj_root / "ir"
            m1 = json.loads((ir_dir / "maps" / "map_001.json").read_text(encoding="utf-8"))
            m2 = json.loads((ir_dir / "maps" / "map_002.json").read_text(encoding="utf-8"))
            elder_cmds = json.dumps(m1["events"][0], ensure_ascii=False)
            boss_cmds = json.dumps(m2["events"], ensure_ascii=False)
            self.assertIn("@QUEST:goblin_mine|active|", elder_cmds)
            self.assertIn("@QUEST:goblin_mine|completed|", boss_cmds)

    def test_strict_save_checksum_mode_player_cli_and_release_bundle(self) -> None:
        import pygame

        with tempfile.TemporaryDirectory() as td:
            proj_root = Path(td) / "strict_save_ws"
            generate_sample_project(proj_root)
            ir_dir = proj_root / "ir"

            # Create valid slot 1 and autosave
            sess = HeadlessGameSession(ir_dir)
            sess.gold = 250
            slot1_path = sess.save_to_file(slot=1)
            auto_path = sess.save_autosave()

            # Tamper slot 1 and autosave
            slot1_data = json.loads(slot1_path.read_text(encoding="utf-8"))
            slot1_data["gold"] = 88888
            slot1_path.write_text(json.dumps(slot1_data, ensure_ascii=False), encoding="utf-8")

            auto_data = json.loads(auto_path.read_text(encoding="utf-8"))
            auto_data["gold"] = 77777
            auto_path.write_text(json.dumps(auto_data, ensure_ascii=False), encoding="utf-8")

            # Default mode: warns and loads
            sess_warn = HeadlessGameSession(ir_dir, strict_checksum=False)
            sess_warn.gold = 10
            self.assertTrue(sess_warn.load_from_file(slot=1))
            self.assertFalse(sess_warn.save_checksum_valid)
            self.assertEqual(sess_warn.gold, 88888)

            # Strict mode: raises ValueError and preserves current state
            sess_strict = HeadlessGameSession(ir_dir, strict_checksum=True)
            sess_strict.gold = 10
            with self.assertRaises(ValueError):
                sess_strict.load_from_file(slot=1)
            self.assertEqual(sess_strict.gold, 10)
            with self.assertRaises(ValueError):
                sess_strict.step("load:1")
            self.assertEqual(sess_strict.gold, 10)

            # Stripping checksum field completely is also rejected in strict mode
            no_ck_path = ir_dir / "save_slot2.json"
            slot1_data.pop("checksum", None)
            no_ck_path.write_text(json.dumps(slot1_data, ensure_ascii=False), encoding="utf-8")
            with self.assertRaises(ValueError):
                sess_strict.load_from_file(slot=2)
            self.assertFalse(sess_strict.save_checksum_valid)

            # Player GUI F9 quickload + Title Continue reject cleanly without crashing
            pygame.init()
            try:
                _h, _sl, pend, msg = player_gui.process_save_load_shortcut(
                    pygame, pygame.K_F9, sess_strict, 1, True
                )
                self.assertFalse(pend)
                self.assertIn("거부됨", msg)
                self.assertEqual(sess_strict.gold, 10)

                res_title = player_gui.process_title_selection(sess_strict, "title", 4, True)
                self.assertEqual(res_title["action"], "load_rejected")
                self.assertEqual(res_title["next_mode"], "title")
                self.assertIn("거부됨", res_title["toast"])
                self.assertEqual(sess_strict.gold, 10)
            finally:
                pygame.quit()

            # Offscreen player launch with strict_save=True propagates to session
            prev_vid = os.environ.get("SDL_VIDEODRIVER")
            prev_aud = os.environ.get("SDL_AUDIODRIVER")
            os.environ["SDL_VIDEODRIVER"] = "dummy"
            os.environ["SDL_AUDIODRIVER"] = "dummy"
            seen_strict: list[bool] = []
            try:
                rc_p = player_gui.launch_player(
                    ir_dir,
                    max_frames=2,
                    strict_save=True,
                    on_frame=lambda _f, _scr, s: seen_strict.append(bool(s.strict_checksum)),
                )
                self.assertEqual(rc_p, 0)
                self.assertTrue(all(seen_strict) and len(seen_strict) >= 1)
            finally:
                if prev_vid is None:
                    os.environ.pop("SDL_VIDEODRIVER", None)
                else:
                    os.environ["SDL_VIDEODRIVER"] = prev_vid
                if prev_aud is None:
                    os.environ.pop("SDL_AUDIODRIVER", None)
                else:
                    os.environ["SDL_AUDIODRIVER"] = prev_aud

            # CLI parser flags + export-release launcher strict_save=True emission
            parser = _build_parser()
            args_play = parser.parse_args(["play", str(ir_dir), "--strict-save"])
            self.assertTrue(args_play.strict_save)
            args_rel = parser.parse_args(["export-release", str(ir_dir), str(proj_root / "rel"), "--strict-save"])
            self.assertTrue(args_rel.strict_save)

            rel_dir = proj_root / "rel_strict"
            export_release_bundle(ir_dir, rel_dir, launcher=True, strict_save=True)
            launcher_src = (rel_dir / "launch_player.py").read_text(encoding="utf-8")
            self.assertIn("strict_save=True", launcher_src)

    def test_window_drag_optimization_clamp_dt_and_surface_cache(self) -> None:
        import pygame

        self.assertEqual(player_gui._clamp_frame_dt(2500.0), 50.0)
        self.assertEqual(player_gui._clamp_frame_dt(16.0), 16.0)
        self.assertAlmostEqual(player_gui._clamp_frame_dt(0.0), 1000.0 / player_gui.TARGET_FPS, places=3)

        pygame.init()
        try:
            src = pygame.Surface((64, 64), pygame.SRCALPHA)
            src.fill((80, 120, 200, 255))
            s1 = player_gui._get_scaled_surface(pygame, src, (128, 96))
            s2 = player_gui._get_scaled_surface(pygame, src, (128, 96))
            self.assertIs(s1, s2)

            ws = pygame.Surface((192, 128), pygame.SRCALPHA)
            ws.fill((40, 60, 120, 255))
            screen = pygame.Surface((player_gui.VIEW_W, player_gui.VIEW_H))
            theme = {"bg": (20, 20, 40), "border": (200, 200, 220), "accent": (255, 220, 90)}
            player_gui._draw_window_box(
                pygame,
                screen,
                (10, 10, 140, 60),
                theme,
                load_img=lambda _p: ws,
                windowskin_name="001-Blue01",
            )
            self.assertIn((id(ws), 140, 60), player_gui._WINDOW_BOX_CACHE)
        finally:
            pygame.quit()

    def test_frozen_windowed_default_launch_resolution(self) -> None:
        from run_app import _resolve_frozen_default_launch

        self.assertIsNone(_resolve_frozen_default_launch([], frozen=False))
        self.assertEqual(_resolve_frozen_default_launch([], frozen=True), ("studio", None))
        self.assertIsNone(_resolve_frozen_default_launch(["check"], frozen=True))
        self.assertIsNone(_resolve_frozen_default_launch(["--help"], frozen=True))
        with tempfile.TemporaryDirectory() as td:
            proj = Path(td) / "drop_proj"
            generate_sample_project(proj)
            self.assertEqual(_resolve_frozen_default_launch([str(proj)], frozen=True), ("studio", proj / "ir"))
            self.assertEqual(_resolve_frozen_default_launch([str(proj / "ir")], frozen=True), ("studio", proj / "ir"))

    def test_window_outline_drag_preview_math_and_win32_commit(self) -> None:
        from src.harness.window_opt import (
            WM_ENTERSIZEMOVE,
            WM_EXITSIZEMOVE,
            WM_MOVING,
            WM_SIZING,
            attach_outline_drag_preview,
            compute_moving_rect,
            compute_sizing_rect,
        )

        self.assertEqual(compute_moving_rect((100, 100, 500, 400), 45, -20), (145, 80, 545, 380))
        self.assertEqual(compute_sizing_rect((100, 100, 500, 400), 8, 80, 60, min_w=320, min_h=240), (100, 100, 580, 460))
        # Clamp left-top drag against right-bottom anchor
        self.assertEqual(compute_sizing_rect((100, 100, 500, 400), 4, 300, 300, min_w=320, min_h=240), (180, 160, 500, 400))

        if sys.platform == "win32":
            import ctypes
            from ctypes import wintypes
            import tkinter as tk

            r = tk.Tk()
            try:
                r.geometry("400x300+140+140")
                r.update()
                commits: list = []
                ctrl = attach_outline_drag_preview(r.winfo_id(), min_size=(200, 150), on_commit=lambda w, h: commits.append((w, h)))
                self.assertIsNotNone(ctrl)
                u32 = ctypes.windll.user32
                u32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
                u32.SendMessageW.restype = ctypes.c_ssize_t

                rc_before = wintypes.RECT()
                u32.GetWindowRect(ctrl.hwnd, ctypes.byref(rc_before))
                before_sz = (rc_before.right - rc_before.left, rc_before.bottom - rc_before.top)

                u32.SendMessageW(ctrl.hwnd, WM_ENTERSIZEMOVE, 0, 0)
                move_rc = wintypes.RECT(220, 200, 220 + before_sz[0], 200 + before_sz[1])
                u32.SendMessageW(ctrl.hwnd, WM_MOVING, 0, ctypes.addressof(move_rc))
                self.assertTrue(bool(u32.IsWindowVisible(ctrl.preview_hwnd)))

                rc_mid = wintypes.RECT()
                u32.GetWindowRect(ctrl.hwnd, ctypes.byref(rc_mid))
                self.assertEqual((rc_mid.left, rc_mid.top), (rc_before.left, rc_before.top))

                size_rc = wintypes.RECT(140, 140, 640, 500)
                u32.SendMessageW(ctrl.hwnd, WM_SIZING, 8, ctypes.addressof(size_rc))
                u32.SendMessageW(ctrl.hwnd, WM_EXITSIZEMOVE, 0, 0)
                self.assertFalse(bool(u32.IsWindowVisible(ctrl.preview_hwnd)))

                rc_after = wintypes.RECT()
                u32.GetWindowRect(ctrl.hwnd, ctypes.byref(rc_after))
                self.assertEqual((rc_after.right - rc_after.left, rc_after.bottom - rc_after.top), (500, 360))
                self.assertEqual(commits, [(500, 360)])

                from src.harness import window_opt

                self.assertIsNotNone(window_opt._PREVIEW_PATTERN_BRUSH)
                self.assertEqual(window_opt.PREVIEW_GRAY_LIGHT_RGB, (184, 188, 196))
                self.assertEqual(window_opt.PREVIEW_GRAY_DARK_RGB, (88, 92, 100))
            finally:
                r.destroy()

        import pygame

        pygame.init()
        try:
            psurf = pygame.Surface((120, 80))
            psurf.fill((10, 10, 10))
            player_gui._draw_gray_pattern_rect(pygame, psurf, (4, 4, 112, 72))
            self.assertEqual(psurf.get_at((4, 4))[:3], (184, 188, 196))
            self.assertEqual(psurf.get_at((11, 4))[:3], (88, 92, 100))
        finally:
            pygame.quit()

    def test_shared_map_info_helpers_and_i18n_int_map(self) -> None:
        from src.core.i18n import apply_i18n_locale, extract_i18n_strings
        from src.core.utils import remove_map_info_entry, set_map_info_name, upsert_map_info_entry

        proj_int = {"map_infos": {"__type__": "int_map", "entries": []}}
        upsert_map_info_entry(proj_int, 2, {"fields": {"name": "Town"}})
        self.assertTrue(set_map_info_name(proj_int, 2, "Port Town"))
        self.assertEqual(proj_int["map_infos"]["entries"][0][1]["fields"]["name"], "Port Town")
        self.assertTrue(remove_map_info_entry(proj_int, 2))
        self.assertEqual(proj_int["map_infos"]["entries"], [])

        proj_flat = {"map_infos": {}}
        upsert_map_info_entry(proj_flat, 3, {"fields": {"name": "Cave"}})
        self.assertTrue(set_map_info_name(proj_flat, 3, "Ice Cave"))
        self.assertEqual(proj_flat["map_infos"]["3"]["fields"]["name"], "Ice Cave")
        self.assertTrue(remove_map_info_entry(proj_flat, 3))
        self.assertNotIn("3", proj_flat["map_infos"])

        with tempfile.TemporaryDirectory() as td:
            ws = Path(td) / "i18n_ws"
            generate_sample_project(ws)
            ir_dir = ws / "ir"
            extract_i18n_strings(ir_dir)
            msg_path = ir_dir / "locale" / "messages.json"
            catalog = json.loads(msg_path.read_text(encoding="utf-8"))
            catalog["strings"].setdefault("map.1.name", {})["en"] = "Localized Meadow"
            msg_path.write_text(json.dumps(catalog, ensure_ascii=False, indent=2), encoding="utf-8")
            res = apply_i18n_locale(ir_dir, "en")
            self.assertGreaterEqual(res["applied_count"], 1)
            proj_after = json.loads((ir_dir / "ir_project.json").read_text(encoding="utf-8"))
            entries = proj_after["map_infos"]["entries"]
            map1_entry = [pair[1] for pair in entries if int(pair[0]) == 1][0]
            self.assertEqual(map1_entry["fields"]["name"], "Localized Meadow")

    def test_player_gui_text_overlay_and_face_caches(self) -> None:
        import pygame

        pygame.init()
        try:
            fnt = pygame.font.SysFont(None, 18)
            s1 = player_gui._render_text(fnt, "HP 100/100", True, (240, 240, 240))
            s2 = player_gui._render_text(fnt, "HP 100/100", True, (240, 240, 240))
            s3 = player_gui._render_text(fnt, "HP 100/100", True, (255, 220, 95))
            self.assertIs(s1, s2)
            self.assertIsNot(s1, s3)

            ov1 = player_gui._get_alpha_overlay(pygame, 320, 120, (6, 10, 18, 175))
            ov2 = player_gui._get_alpha_overlay(pygame, 320, 120, (6, 10, 18, 175))
            ov3 = player_gui._get_alpha_overlay(pygame, 320, 120, (6, 10, 18, 90))
            self.assertIs(ov1, ov2)
            self.assertIsNot(ov1, ov3)
            self.assertEqual(ov1.get_size(), (320, 120))

            sheet = pygame.Surface((384, 192), pygame.SRCALPHA)
            sheet.fill((40, 80, 160, 255))
            p1 = player_gui._get_face_portrait(pygame, sheet, 0)
            p2 = player_gui._get_face_portrait(pygame, sheet, 0)
            p3 = player_gui._get_face_portrait(pygame, sheet, 1)
            self.assertIs(p1, p2)
            self.assertIsNot(p1, p3)
            self.assertEqual(p1.get_size(), (92, 92))
        finally:
            pygame.quit()


if __name__ == "__main__":
    unittest.main(verbosity=2)
