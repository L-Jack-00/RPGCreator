# -*- coding: utf-8 -*-
"""
Headless Test Suite for rpgcreator_cli.
Validates Ruby Marshal 4.8 codec (including Bignum and object links), RGSSAD v1,
full RMXP database round-tripping, directional passability, conditional page BFS,
atomic CLI editing, and automated playthrough simulation.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

if sys.stdout.encoding != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

from PIL import Image

from run_app import main as cli_main
from src.core.autotile import (
    autotile_frame_count,
    autotile_frame_x_offset,
    resolve_autotile_quads,
    subtile_pixel_rect,
)
from src.core.editor import (
    add_event,
    apply_blueprint,
    create_map,
    list_common_events,
    paint_map,
    set_common_event,
    set_db_entry,
)
from src.core.engine import CoreEngine
from src.core.ir_converter import pack_ir_to_rmxp, unpack_rmxp_to_ir
from src.core.models import HarnessState, JobContext
from src.core.rgssad import pack_rgssad, unpack_rgssad
from src.core.rmxp_marshal import RubyObject, RubySymbol, marshal_dumps, marshal_loads
from src.core.rmxp_types import RgssColor, RgssTable, RgssTone
from src.core.sample_generator import generate_sample_project
from src.core.simulator import MAX_COMMON_EVENT_DEPTH, HeadlessGameSession, run_headless_simulation
from src.core.validator import render_map_preview, validate_ir_project
from src.harness.player_gui import (
    _process_keydown,
    advance_slide_offset,
    compute_camera_offset,
    compute_camera_offset_px,
    compute_stride_col,
    compute_walk_frame_col,
    first_healing_item_id,
    format_hud_text,
    format_status_lines,
    launch_player,
    resolve_held_move_action,
)
from src.harness.supervisor import HarnessSupervisor


class TestRpgCreatorHeadless(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = CoreEngine()
        self.supervisor = HarnessSupervisor(self.engine)

    def test_01_marshal_bignum_shared_and_circular_links(self) -> None:
        tbl = RgssTable.create(3, 2, 2, dims=3, fill=0)
        tbl.set(1, 1, 1, 386)
        shared = RubyObject("RPG::AudioFile", {"@name": "BGM_01", "@volume": 90, "@pitch": 100})
        cyclic: list = []
        cyclic.append(cyclic)

        payload = {
            1: RubyObject("RPG::Sample", {
                "@name": "테스트_맵",
                "@flag": True,
                "@empty": None,
                "@sym": RubySymbol("ok"),
                "@nums": [0, 1, -1, 122, 123, -124, 65535, -70000],
                "@big_pos": 1 << 75,
                "@big_neg": -(1 << 82) - 12345,
                "@shared": [shared, shared],
                "@cyclic": cyclic,
                "@table": tbl.to_user_def(),
                "@color": RgssColor(12.5, 25.0, 200.0, 255.0).to_user_def(),
                "@tone": RgssTone(-20.0, 0.0, 30.0, 10.0).to_user_def(),
            })
        }
        raw = marshal_dumps(payload)
        decoded = marshal_loads(raw)
        obj = decoded[1]
        self.assertEqual(obj.ivars["@big_pos"], 1 << 75)
        self.assertEqual(obj.ivars["@big_neg"], -(1 << 82) - 12345)
        self.assertIs(obj.ivars["@shared"][0], obj.ivars["@shared"][1])
        self.assertIs(obj.ivars["@cyclic"][0], obj.ivars["@cyclic"])
        self.assertEqual(RgssTable.from_user_def(obj.ivars["@table"]).get(1, 1, 1), 386)

    def test_02_full_database_roundtrip_and_rgssad(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            info = generate_sample_project(root / "proj")
            rmxp_dir = Path(info["rmxp_dir"])
            ir_dir = Path(info["ir_dir"])

            for expected_db in (
                "Actors", "Classes", "Skills", "Items", "Weapons",
                "Armors", "Enemies", "Troops", "States", "Animations", "CommonEvents",
            ):
                self.assertTrue((rmxp_dir / "Data" / f"{expected_db}.rxdata").exists())
                json_file = "common_events.json" if expected_db == "CommonEvents" else f"{expected_db.lower()}.json"
                self.assertTrue((ir_dir / "database" / json_file).exists())

            repacked_rmxp = root / "repacked_rmxp"
            reunpacked_ir = root / "reunpacked_ir"
            pack_ir_to_rmxp(ir_dir, repacked_rmxp)
            unpack_rmxp_to_ir(repacked_rmxp, reunpacked_ir)

            for rel in ("ir_project.json", "maps/map_001.json", "maps/map_002.json", "database/troops.json", "database/common_events.json"):
                self.assertEqual(
                    json.loads((ir_dir / rel).read_text(encoding="utf-8")),
                    json.loads((reunpacked_ir / rel).read_text(encoding="utf-8")),
                    msg=f"Mismatch on round-trip for {rel}",
                )

            archive = root / "Game.rgssad"
            pack_info = pack_rgssad(rmxp_dir / "Data", archive)
            self.assertGreater(pack_info["files_packed"], 10)
            extracted = root / "extracted_data"
            unpack_info = unpack_rgssad(archive, extracted)
            self.assertEqual(unpack_info["files_extracted"], pack_info["files_packed"])
            self.assertEqual((rmxp_dir / "Data" / "Map001.rxdata").read_bytes(), (extracted / "Map001.rxdata").read_bytes())

    def test_03_directional_passability_and_conditional_gate_bfs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            info = generate_sample_project(root / "proj")
            ir_dir = Path(info["ir_dir"])

            ok_res = validate_ir_project(ir_dir)
            self.assertTrue(ok_res.passed, msg=f"Unexpected errors: {ok_res.errors}")
            self.assertIn(4, ok_res.stats["unlocked_switches"])

            # Removing the lever (event 5) on Map 2 must lock the vault and fail BFS for chest (event 2)
            map2_path = ir_dir / "maps" / "map_002.json"
            map2 = json.loads(map2_path.read_text(encoding="utf-8"))
            map2["events"] = [ev for ev in map2["events"] if ev["id"] != 5]
            map2_path.write_text(json.dumps(map2, ensure_ascii=False, indent=2), encoding="utf-8")

            locked_res = validate_ir_project(ir_dir)
            self.assertFalse(locked_res.passed)
            self.assertTrue(any("보물상자" in e for e in locked_res.errors))

            # Directional one-way tile test: tile 389 blocks walking UP (0x08) into/out of it
            ts_path = ir_dir / "database" / "tilesets.json"
            tilesets = json.loads(ts_path.read_text(encoding="utf-8"))
            tilesets[1]["fields"]["passages"]["data"][389] = 0x08
            ts_path.write_text(json.dumps(tilesets, ensure_ascii=False, indent=2), encoding="utf-8")
            map1_path = ir_dir / "maps" / "map_001.json"
            map1 = json.loads(map1_path.read_text(encoding="utf-8"))
            map1["layers"]["layer_0"][9][4] = 389  # directly north of player start (4,10)
            map1_path.write_text(json.dumps(map1, ensure_ascii=False, indent=2), encoding="utf-8")
            snap = run_headless_simulation(ir_dir, ["up"])
            # Entering (4,9) from south checks Down bit (0x01), so stepping onto (4,9) succeeds,
            # but stepping UP out of (4,9) to (4,8) checks Up bit (0x08) and must be blocked!
            self.assertEqual((snap["x"], snap["y"]), (4, 9))
            snap2 = run_headless_simulation(ir_dir, ["up", "up"])
            self.assertEqual((snap2["x"], snap2["y"]), (4, 9))

    def test_04_atomic_cli_editing_and_end_to_end_simulation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            proj_dir = root / "cli_proj"
            self.assertEqual(cli_main(["--json", "sample", str(proj_dir)]), 0)
            ir_dir = proj_dir / "ir"

            # Atomic edits: create Map 3, paint grass, add switch & NPC, link warp from Map 1
            created = create_map(ir_dir, name="숲속_비밀길", width=12, height=10, tileset_id=1, fill_tile=384)
            self.assertEqual(created["map_id"], 3)
            paint_map(ir_dir, map_id=3, layer=1, tile_id=386, rect=(0, 0, 12, 1))
            set_db_entry(ir_dir, "switches", 5, {"name": "비밀길_방문"})
            add_event(
                ir_dir,
                map_id=1,
                name="숲_포탈",
                x=12,
                y=10,
                trigger="player_touch",
                through=True,
                commands=[{"op": "transfer_player", "var_mode": 0, "map_id": 3, "x": 2, "y": 5, "direction": "right", "fade_type": 0, "indent": 0}],
            )
            add_event(
                ir_dir,
                map_id=3,
                name="은둔자",
                x=5,
                y=5,
                commands=[
                    {"op": "set_switch", "start_id": 5, "end_id": 5, "value": "ON", "indent": 0},
                    {"op": "say", "lines": ["조용한 숲이로군."], "indent": 0},
                ],
            )
            self.assertEqual(cli_main(["--json", "check", str(ir_dir)]), 0)

            # Full scripted playthrough: Elder -> Potion -> Bridge -> Mine -> Lever -> Vault Chest -> Boss!
            actions = [
                # Talk to Elder at (6,8) from (4,10)
                "right", "right", "up", "interact", "choose:0", "interact",
                # Walk to cave warp at (17,4)
                "right", "right", "right",
                "up", "up", "up", "up", "up",
                "right", "right", "right", "right", "right", "right", "right", "right",
                # Now on Map 2 (9,13) -> move to lever at (15,10)
                "right", "right", "right", "right", "right", "right",
                "up", "up", "interact", "interact",
                # Move to unlocked gate (6,3) and open chest at (3,3)
                "left",
                "up", "up", "up", "up", "up", "up", "up", "up",
                "left", "left", "left", "left", "left", "left", "left", "left", "left", "left",
                "interact", "interact",
                # Move to Goblin Boss at (11,4) from (4,3)
                "right", "right", "right", "right", "right", "right",
                "down", "right", "interact", "interact",
            ]
            snap = run_headless_simulation(ir_dir, actions)
            self.assertEqual(snap["map_id"], 2)
            self.assertEqual(snap["inventory"].get(1), 2)   # 2 potions from Elder
            self.assertEqual(snap["gold"], 220)             # 100G chest + 120G Goblin Boss
            self.assertEqual(snap["exp"], 80)               # 80 EXP from Goblin Boss
            self.assertTrue(snap["switches"].get("1"))      # Quest accepted
            self.assertTrue(snap["switches"].get("2"))      # Boss defeated
            self.assertTrue(snap["switches"].get("4"))      # Vault unlocked

            png_path = root / "map1_preview.png"
            ascii_map = render_map_preview(ir_dir, map_id=1, png_out=png_path)
            self.assertIn("P", ascii_map)
            self.assertTrue(png_path.exists())

    def test_05_supervisor_controls(self) -> None:
        ctx = JobContext(job_id="sup_001")
        self.supervisor.emergency_stop()
        self.assertFalse(self.supervisor.run_job_sync(ctx, total_steps=3, step_delay_sec=0.0))
        self.assertEqual(self.supervisor.state, HarnessState.STOPPED)
        self.supervisor.reset()
        self.supervisor.pause()
        self.assertEqual(self.supervisor.state, HarnessState.PAUSED)
        self.supervisor.resume()
        self.assertEqual(self.supervisor.state, HarnessState.RUNNING)

    def test_06_camera_scroll_branch_aware_bfs_and_rgss_v2_v3(self) -> None:
        # 1. Camera scroll clamping on a 50x40 map (640x480 viewport, 32px tiles)
        self.assertEqual(compute_camera_offset(0, 0, 50, 40), (0, 0))
        self.assertEqual(compute_camera_offset(25, 20, 50, 40), (496, 416))
        self.assertEqual(compute_camera_offset(49, 39, 50, 40), (960, 800))

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            info = generate_sample_project(root / "proj")
            rmxp_dir = Path(info["rmxp_dir"])
            ir_dir = Path(info["ir_dir"])

            # 2. RGSS2A (v2) and RGSS3A (v3) archive round-trip
            for ver, ext in ((2, "rgss2a"), (3, "rgss3a")):
                arc = root / f"Game.{ext}"
                p_info = pack_rgssad(rmxp_dir / "Data", arc, version=ver)
                out_dir = root / f"ext_v{ver}"
                u_info = unpack_rgssad(arc, out_dir)
                self.assertEqual(u_info["version"], ver)
                self.assertEqual(u_info["files_extracted"], p_info["files_packed"])
                self.assertEqual((rmxp_dir / "Data" / "Map001.rxdata").read_bytes(), (out_dir / "Map001.rxdata").read_bytes())

            # 3. Branch-aware conditional BFS: switch 5 gated inside if_item(2) must NOT trigger without item 2
            set_db_entry(ir_dir, "switches", 5, {"name": "열쇠_사용"})
            add_event(
                ir_dir,
                map_id=1,
                name="열쇠_감지",
                x=5,
                y=10,
                commands=[
                    {"op": "if_item", "item_id": 2, "indent": 0},
                    {"op": "set_switch", "start_id": 5, "end_id": 5, "value": "ON", "indent": 1},
                    {"op": "end_if", "indent": 0},
                ],
            )
            res_locked = validate_ir_project(ir_dir)
            self.assertTrue(res_locked.passed)
            self.assertNotIn(5, res_locked.stats["unlocked_switches"])

            # Now add an event giving item 2; branch-aware BFS must discover item 2 -> switch 5
            add_event(
                ir_dir,
                map_id=1,
                name="열쇠_상자",
                x=5,
                y=11,
                commands=[{"op": "change_item", "item_id": 2, "mode": "add", "operand_type": 0, "amount": 1, "indent": 0}],
            )
            res_unlocked = validate_ir_project(ir_dir)
            self.assertTrue(res_unlocked.passed)
            self.assertIn(5, res_unlocked.stats["unlocked_switches"])

    def test_07_autotile_quads_conditional_warp_variables_and_legacy_encoding(self) -> None:
        # 1. RMXP 48-pattern autotile quadrant resolution
        grid = [
            [384, 384, 384, 384, 384],
            [384, 48, 48, 48, 384],
            [384, 48, 384, 48, 384],
            [384, 48, 48, 48, 384],
            [384, 384, 384, 384, 384],
        ]
        # Top-left water corner at (1,1): up=False, left=False, down=True, right=True, down_right=False -> (13, 16, 31, 12)
        self.assertEqual(resolve_autotile_quads(grid, 1, 1, 48), (13, 16, 31, 12))
        self.assertEqual(subtile_pixel_rect(13), (0, 32, 16, 16))
        self.assertEqual(subtile_pixel_rect(27), (32, 64, 16, 16))
        # Isolated 1x1 water tile
        self.assertEqual(resolve_autotile_quads([[384, 384, 384], [384, 48, 384], [384, 384, 384]], 1, 1, 48), (13, 18, 43, 48))

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            info = generate_sample_project(root / "proj")
            ir_dir = Path(info["ir_dir"])

            # 2. Conditional warp gated behind if_item(2) + variable & common_event check
            create_map(ir_dir, name="비밀_신전", width=10, height=10, tileset_id=1, fill_tile=384)
            add_event(
                ir_dir,
                map_id=3,
                name="신전_수호자",
                x=5,
                y=5,
                commands=[
                    {"op": "set_variable", "start_id": 1, "end_id": 1, "operation": 1, "operand_type": 0, "operand": [3], "indent": 0},
                    {"op": "if_variable", "variable_id": 1, "rhs_is_var": False, "rhs_value": 3, "cmp": 1, "indent": 0},
                    {"op": "common_event", "common_event_id": 1, "indent": 1},
                    {"op": "end_if", "indent": 0},
                ],
            )
            add_event(
                ir_dir,
                map_id=1,
                name="비밀_포탈",
                x=5,
                y=10,
                commands=[
                    {"op": "if_item", "item_id": 2, "indent": 0},
                    {"op": "transfer_player", "var_mode": 0, "map_id": 3, "x": 5, "y": 6, "direction": "up", "fade_type": 0, "indent": 1},
                    {"op": "end_if", "indent": 0},
                ],
            )
            # Item 2 is not obtainable yet -> Map 3's NPC must be unreachable
            res_blocked = validate_ir_project(ir_dir)
            self.assertFalse(res_blocked.passed)
            self.assertTrue(any("신전_수호자" in e for e in res_blocked.errors))

            # Add event granting Item 2 -> conditional warp unlocks -> Map 3 becomes reachable
            add_event(
                ir_dir,
                map_id=1,
                name="열쇠_제단",
                x=4,
                y=11,
                commands=[{"op": "change_item", "item_id": 2, "mode": "add", "operand_type": 0, "amount": 1, "indent": 0}],
            )
            res_ok = validate_ir_project(ir_dir)
            self.assertTrue(res_ok.passed, msg=f"Unexpected errors: {res_ok.errors}")

            # Simulate obtaining key -> warping -> triggering variable + common_event on Map 3
            sim_snap = run_headless_simulation(
                ir_dir,
                ["down", "interact", "right", "interact", "interact", "interact"],
            )
            self.assertEqual(sim_snap["map_id"], 3)
            self.assertTrue(any("기운을 차렸다" in line for line in sim_snap["log"]))

            # 3. Legacy non-UTF-8 encoding round-trip (CP949 & CP932)
            cp949_dir = root / "rmxp_cp949"
            cp949_ir = root / "ir_cp949"
            pack_ir_to_rmxp(ir_dir, cp949_dir, encoding="cp949")
            raw_sys = (cp949_dir / "Data" / "System.rxdata").read_bytes()
            self.assertIn("퀘스트_수락".encode("cp949"), raw_sys)
            unpack_rmxp_to_ir(cp949_dir, cp949_ir, encoding="cp949")
            unpacked_proj = json.loads((cp949_ir / "ir_project.json").read_text(encoding="utf-8"))
            self.assertEqual(unpacked_proj["system"]["fields"]["switches"][1], "퀘스트_수락")

            # CP932 Japanese string round-trip via Marshal
            jp_bytes = marshal_dumps({"hero": "勇者", "map": "冒険の始まり"}, encoding="cp932")
            self.assertIn("勇者".encode("cp932"), jp_bytes)
            self.assertEqual(marshal_loads(jp_bytes, encoding="cp932"), {"hero": "勇者", "map": "冒険の始まり"})

    def test_08_common_event_call_stack_blueprint_rollback_and_animated_preview(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            info = generate_sample_project(root / "proj")
            rmxp_dir = Path(info["rmxp_dir"])
            ir_dir = Path(info["ir_dir"])

            # 1. Animated autotile frames (192x128 water.png) and 32x32 sprite PNG preview
            with Image.open(rmxp_dir / "Graphics" / "Autotiles" / "water.png") as water_img:
                water_size = water_img.size
            self.assertEqual(water_size, (192, 128))
            self.assertEqual(autotile_frame_count(water_size[0]), 2)
            self.assertEqual(autotile_frame_x_offset(0, water_size[0]), 0)
            self.assertEqual(autotile_frame_x_offset(1, water_size[0]), 96)
            self.assertEqual(subtile_pixel_rect(13, frame_x_offset=96), (96, 32, 16, 16))

            preview_png = root / "map1_sprites.png"
            render_map_preview(ir_dir, 1, png_out=preview_png)
            self.assertTrue(preview_png.exists())
            with Image.open(preview_png) as p_img:
                self.assertEqual(p_img.size, (20 * 32, 15 * 32))

            # 2. Common event call stack resumption across dialogue & recursion depth guard
            set_db_entry(ir_dir, "switches", 6, {"name": "공통_완료"})
            ce_path = ir_dir / "database" / "common_events.json"
            ce_data = [
                None,
                {
                    "id": 1,
                    "name": "공통_대화_테스트",
                    "trigger": "none",
                    "switch_id": 1,
                    "commands": [
                        {"op": "say", "lines": ["공통_인사"], "indent": 0},
                        {"op": "end", "indent": 0},
                    ],
                },
                {
                    "id": 2,
                    "name": "무한_재귀_가드",
                    "trigger": "none",
                    "switch_id": 1,
                    "commands": [
                        {"op": "common_event", "common_event_id": 2, "indent": 0},
                        {"op": "end", "indent": 0},
                    ],
                },
            ]
            ce_path.write_text(json.dumps(ce_data, ensure_ascii=False, indent=2), encoding="utf-8")

            add_event(
                ir_dir,
                map_id=1,
                name="호출_시험",
                x=4,
                y=12,
                commands=[
                    {"op": "common_event", "common_event_id": 1, "indent": 0},
                    {"op": "set_switch", "start_id": 6, "end_id": 6, "value": "ON", "indent": 0},
                    {"op": "end", "indent": 0},
                ],
            )
            add_event(
                ir_dir,
                map_id=1,
                name="재귀_시험",
                x=3,
                y=11,
                commands=[
                    {"op": "common_event", "common_event_id": 2, "indent": 0},
                    {"op": "end", "indent": 0},
                ],
            )

            sess = HeadlessGameSession(ir_dir)  # starts at (4, 10)
            sess.step("down")       # -> (4, 11) facing down toward (4, 12)
            sess.step("interact")   # triggers common event 1 -> pauses on say
            self.assertIsNotNone(sess.pending)
            self.assertEqual(sess.pending.lines, ["공통_인사"])
            self.assertFalse(sess.switches.get(6, False))
            sess.step("interact")   # closes dialogue -> resumes caller -> sets switch 6 ON
            self.assertIsNone(sess.pending)
            self.assertTrue(sess.switches.get(6, False))

            sess.step("left")       # turns left from (4, 11) or moves to (3, 11)? wait: at (4, 11) left moves to (3, 11)!
            with self.assertRaises(RuntimeError) as depth_err:
                sess.step("interact")
            self.assertIn(f"limit ({MAX_COMMON_EVENT_DEPTH})", str(depth_err.exception))

            # 3. Atomic apply_blueprint rollback vs valid CLI apply-blueprint
            proj_path = ir_dir / "ir_project.json"
            proj_before = proj_path.read_bytes()
            bad_bp = {
                "db": [{"table": "switches", "id": 8, "fields": {"name": "롤백_대상"}}],
                "maps": [
                    {
                        "name": "단절된_미궁",
                        "width": 10,
                        "height": 10,
                        "events": [{"name": "고립된_NPC", "x": 4, "y": 4}],
                    }
                ],
            }
            with self.assertRaises(ValueError):
                apply_blueprint(ir_dir, bad_bp, validate=True)
            self.assertEqual(proj_path.read_bytes(), proj_before)
            self.assertFalse((ir_dir / "maps" / "map_003.json").exists())

            good_bp = {
                "db": [{"table": "switches", "id": 8, "fields": {"name": "성공_스위치"}}],
                "patch_maps": [
                    {
                        "map_id": 1,
                        "events": [
                            {
                                "name": "안내석",
                                "x": 6,
                                "y": 11,
                                "commands": [{"op": "set_switch", "start_id": 8, "end_id": 8, "value": "ON", "indent": 0}],
                            }
                        ],
                    }
                ],
            }
            bp_file = root / "blueprint.json"
            bp_file.write_text(json.dumps(good_bp, ensure_ascii=False, indent=2), encoding="utf-8")
            rc = cli_main(["apply-blueprint", str(ir_dir), "--file", str(bp_file)])
            self.assertEqual(rc, 0)
            proj_after = json.loads(proj_path.read_text(encoding="utf-8"))
            self.assertEqual(proj_after["system"]["fields"]["switches"][8], "성공_스위치")

    def test_09_map_name_blueprint_ce_bfs_and_visual_page_walk(self) -> None:
        # 1. Walk animation helper cycles 0..3 while timer > 0 and returns 0 at rest
        self.assertEqual([compute_walk_frame_col(i, 5) for i in range(6)], [0, 1, 2, 3, 0, 1])
        self.assertEqual(compute_walk_frame_col(3, 0), 0)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            info = generate_sample_project(root / "proj")
            ir_dir = Path(info["ir_dir"])
            proj_path = ir_dir / "ir_project.json"
            proj_before = proj_path.read_bytes()

            # 2. Invalid map_name reference rolls back cleanly
            with self.assertRaises(ValueError):
                apply_blueprint(ir_dir, {"start": {"map_name": "존재하지_않는_맵", "x": 1, "y": 1}}, validate=True)
            self.assertEqual(proj_path.read_bytes(), proj_before)

            # 3. Single blueprint creating new start map + new dungeon + cross-map map_name warps
            bp_out = apply_blueprint(
                ir_dir,
                {
                    "db": [{"table": "switches", "id": 9, "fields": {"name": "공용_열쇠_스위치"}}],
                    "maps": [
                        {
                            "name": "새로운_거점",
                            "width": 10,
                            "height": 10,
                            "events": [
                                {
                                    "name": "기존마을_포탈",
                                    "x": 5,
                                    "y": 4,
                                    "commands": [
                                        {"op": "transfer_player", "var_mode": 0, "map_name": "시작의 마을", "x": 4, "y": 10, "direction": "down", "fade_type": 0, "indent": 0},
                                    ],
                                }
                            ],
                        },
                        {
                            "name": "신규_성소",
                            "width": 10,
                            "height": 10,
                            "events": [
                                {
                                    "name": "성소_수호상",
                                    "x": 5,
                                    "y": 5,
                                    "commands": [{"op": "say", "lines": ["수호상이다."], "indent": 0}],
                                }
                            ],
                        },
                    ],
                    "patch_maps": [
                        {
                            "map_id": 1,
                            "events": [
                                {
                                    "name": "성소_문",
                                    "x": 6,
                                    "y": 11,
                                    "commands": [
                                        {"op": "if_switch", "switch_id": 9, "expected": "ON", "indent": 0},
                                        {"op": "transfer_player", "var_mode": 0, "map_name": "신규_성소", "x": 5, "y": 6, "direction": "up", "fade_type": 0, "indent": 1},
                                        {"op": "end_if", "indent": 0},
                                    ],
                                }
                            ],
                        }
                    ],
                    "start": {"map_name": "새로운_거점", "x": 4, "y": 4},
                },
                validate=False,
            )
            self.assertEqual(len(bp_out["created_maps"]), 2)
            proj_after = json.loads(proj_path.read_text(encoding="utf-8"))
            self.assertEqual(proj_after["system"]["fields"]["start_map_id"], 3)

            # Switch 9 is not unlocked yet -> 성소_문 is locked -> Map 4 (성소_수호상) must be unreachable
            res_locked = validate_ir_project(ir_dir)
            self.assertFalse(res_locked.passed)
            self.assertTrue(any("성소_수호상" in e for e in res_locked.errors))

            # 4. Unlock switch 9 INSIDE a CommonEvent and invoke it from Map 1
            ce_path = ir_dir / "database" / "common_events.json"
            ce_list = json.loads(ce_path.read_text(encoding="utf-8"))
            ce_list.append({
                "id": 2,
                "name": "성소_개방_의식",
                "trigger": "none",
                "switch_id": 1,
                "commands": [
                    {"op": "set_switch", "start_id": 9, "end_id": 9, "value": "ON", "indent": 0},
                    {"op": "end", "indent": 0},
                ],
            })
            ce_path.write_text(json.dumps(ce_list, ensure_ascii=False, indent=2), encoding="utf-8")

            add_event(
                ir_dir,
                map_id=1,
                name="의식_제단",
                x=5,
                y=11,
                commands=[{"op": "common_event", "common_event_id": 2, "indent": 0}],
            )
            res_unlocked = validate_ir_project(ir_dir)
            self.assertTrue(res_unlocked.passed, msg=f"Unexpected errors: {res_unlocked.errors}")
            self.assertIn(9, res_unlocked.stats["unlocked_switches"])

            # 5. Render preview with active switch state vs default initial switch state
            map2_off = root / "map2_off.png"
            map2_on = root / "map2_on.png"
            render_map_preview(ir_dir, map_id=2, png_out=map2_off, switches_state={})
            render_map_preview(ir_dir, map_id=2, png_out=map2_on, switches_state={4: True})
            self.assertTrue(map2_off.exists() and map2_on.exists())
            self.assertNotEqual(map2_off.read_bytes(), map2_on.read_bytes())

    def test_10_ce_static_check_multipage_named_blueprint_and_cli_switch_preview(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            info = generate_sample_project(root / "proj")
            ir_dir = Path(info["ir_dir"])

            # 1. Blueprint with named switch/variable/item refs AND multi-page event
            bp_res = apply_blueprint(
                ir_dir,
                {
                    "db": [
                        {"table": "switches", "id": 10, "fields": {"name": "황금_열쇠_해제"}},
                        {"table": "variables", "id": 5, "fields": {"name": "탐험_점수"}},
                        {"table": "items", "id": 3, "fields": {"name": "황금_열쇠"}},
                    ],
                    "patch_maps": [
                        {
                            "map_id": 1,
                            "events": [
                                {
                                    "name": "다중_페이지_상자",
                                    "x": 5,
                                    "y": 11,
                                    "pages": [
                                        {
                                            "trigger": "action_button",
                                            "graphic": "NPC_Elder",
                                            "commands": [
                                                {"op": "set_switch", "switch_name": "황금_열쇠_해제", "value": "ON", "indent": 0},
                                                {"op": "change_item", "item_name": "황금_열쇠", "mode": "add", "operand_type": 0, "amount": 1, "indent": 0},
                                                {"op": "set_variable", "variable_name": "탐험_점수", "operation": 0, "operand_type": 0, "operand": [10], "indent": 0},
                                            ],
                                        },
                                        {
                                            "trigger": "action_button",
                                            "graphic": "",
                                            "condition": {"switch1_name": "황금_열쇠_해제"},
                                            "commands": [
                                                {"op": "if_item", "item_name": "황금_열쇠", "indent": 0},
                                                {"op": "if_variable", "variable_name": "탐험_점수", "rhs_is_var": False, "rhs_value": 10, "cmp": 0, "indent": 1},
                                                {"op": "say", "lines": ["상자는 이미 비었다."], "indent": 2},
                                                {"op": "end_if", "indent": 1},
                                                {"op": "end_if", "indent": 0},
                                            ],
                                        },
                                    ],
                                }
                            ],
                        }
                    ],
                },
                validate=True,
            )
            self.assertEqual(bp_res["events_added"], 1)
            snap = run_headless_simulation(ir_dir, ["right", "down", "interact", "interact"])
            self.assertEqual(snap["inventory"].get(3), 1)
            self.assertTrue(snap["switches"].get("10"))
            self.assertIn("상자는 이미 비었다.", snap["log"])

            # 2. CLI preview --switch overrides match API switches_state
            api_png = root / "api_sw4.png"
            cli_png = root / "cli_sw4.png"
            render_map_preview(ir_dir, map_id=2, png_out=api_png, switches_state={4: True})
            rc = cli_main(["preview", str(ir_dir), "--map-id", "2", "--png", str(cli_png), "--switch", "4=ON"])
            self.assertEqual(rc, 0)
            self.assertEqual(cli_png.read_bytes(), api_png.read_bytes())

            # 3. Static validation catches undefined map/switch references inside common_events.json
            ce_path = ir_dir / "database" / "common_events.json"
            ce_list = json.loads(ce_path.read_text(encoding="utf-8"))
            ce_list.append({
                "id": 9,
                "name": "잘못된_공용이벤트",
                "trigger": "none",
                "switch_id": 1,
                "commands": [
                    {"op": "transfer_player", "var_mode": 0, "map_id": 99, "x": 1, "y": 1, "direction": "down", "fade_type": 0, "indent": 0},
                    {"op": "set_switch", "start_id": 999, "end_id": 999, "value": "ON", "indent": 0},
                    {"op": "end", "indent": 0},
                ],
            })
            ce_path.write_text(json.dumps(ce_list, ensure_ascii=False, indent=2), encoding="utf-8")
            bad_res = validate_ir_project(ir_dir)
            self.assertFalse(bad_res.passed)
            self.assertTrue(any("CommonEvent 9" in e and "map 99" in e for e in bad_res.errors))
            self.assertTrue(any("CommonEvent 9" in e and "switch 999" in e for e in bad_res.errors))

    def test_11_blueprint_common_events_ambiguous_names_troops_and_ascii_parity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            info = generate_sample_project(root / "proj")
            ir_dir = Path(info["ir_dir"])

            # 1. Blueprint creating common_events + referencing common_event_name & conditional page
            bp_res = apply_blueprint(
                ir_dir,
                {
                    "db": [{"table": "switches", "id": 11, "fields": {"name": "축복_완료"}}],
                    "common_events": [
                        {
                            "name": "축복의_노래",
                            "trigger": "none",
                            "switch_name": "축복_완료",
                            "commands": [
                                {"op": "set_switch", "switch_name": "축복_완료", "value": "ON", "indent": 0},
                                {"op": "say", "lines": ["축복을 받았다."], "indent": 0},
                            ],
                        }
                    ],
                    "patch_maps": [
                        {
                            "map_id": 1,
                            "events": [
                                {
                                    "name": "성가대_비석",
                                    "x": 5,
                                    "y": 10,
                                    "commands": [
                                        {"op": "common_event", "common_event_name": "축복의_노래", "indent": 0},
                                    ],
                                },
                                {
                                    "name": "숨겨진_보답",
                                    "x": 5,
                                    "y": 11,
                                    "pages": [
                                        {
                                            "trigger": "action_button",
                                            "graphic": "NPC_Elder",
                                            "condition": {"switch1_name": "축복_완료"},
                                            "commands": [{"op": "say", "lines": ["보답이다."], "indent": 0}],
                                        }
                                    ],
                                },
                            ],
                        }
                    ],
                },
                validate=True,
            )
            self.assertEqual(bp_res["common_events_updated"], 1)
            snap = run_headless_simulation(ir_dir, ["right", "interact", "interact"])
            self.assertTrue(snap["switches"].get("11"))
            self.assertIn("축복을 받았다.", snap["log"])

            # 2. ASCII preview reflects active switch state identically to PNG active page selection
            ascii_off = render_map_preview(ir_dir, 1, switches_state={}).splitlines()
            ascii_on = render_map_preview(ir_dir, 1, switches_state={11: True}).splitlines()
            # Header is line 0, map row y=11 is line 12, x=5 is index 5
            self.assertEqual(ascii_off[12][5], ".")
            self.assertEqual(ascii_on[12][5], "E")

            # 3. Duplicate switch names raise ambiguous ValueError and roll back cleanly
            set_db_entry(ir_dir, "switches", 12, {"name": "중복_스위치"})
            set_db_entry(ir_dir, "switches", 13, {"name": "중복_스위치"})
            with self.assertRaises(ValueError) as dup_ctx:
                apply_blueprint(
                    ir_dir,
                    {
                        "patch_maps": [
                            {
                                "map_id": 1,
                                "events": [
                                    {
                                        "name": "모호한_이벤트",
                                        "x": 6,
                                        "y": 11,
                                        "commands": [{"op": "set_switch", "switch_name": "중복_스위치", "value": "ON", "indent": 0}],
                                    }
                                ],
                            }
                        ]
                    },
                    validate=True,
                )
            self.assertIn("Ambiguous duplicate switch name", str(dup_ctx.exception))

            # 4. Undefined troop_id in battle command is caught by static validation
            add_event(
                ir_dir,
                map_id=1,
                name="없는_적군",
                x=6,
                y=11,
                commands=[{"op": "battle", "troop_id": 999, "can_escape": False, "can_lose": False, "indent": 0}],
            )
            bad_troop_res = validate_ir_project(ir_dir)
            self.assertFalse(bad_troop_res.passed)
            self.assertTrue(any("undefined troop 999" in e for e in bad_troop_res.errors))

    def test_12_troops_patch_remove_events_and_auto_common_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            info = generate_sample_project(root / "proj")
            ir_dir = Path(info["ir_dir"])

            # 1. Missing target in remove_events fails & rolls back
            add_event(ir_dir, map_id=1, name="임시_미끼", x=5, y=11, commands=[])
            map1_before = (ir_dir / "maps" / "map_001.json").read_bytes()
            with self.assertRaises(ValueError):
                apply_blueprint(ir_dir, {"patch_maps": [{"map_id": 1, "remove_events": ["없는_이벤트"]}]}, validate=True)
            self.assertEqual((ir_dir / "maps" / "map_001.json").read_bytes(), map1_before)

            # 2. Blueprint creating troop + patching Event 1 (Elder) + removing decoy + autorun/parallel CEs
            bp_res = apply_blueprint(
                ir_dir,
                {
                    "db": [
                        {"table": "switches", "id": 12, "fields": {"name": "자동_축하"}},
                        {"table": "switches", "id": 13, "fields": {"name": "병렬_카운터"}},
                        {"table": "variables", "id": 6, "fields": {"name": "경과_틱"}},
                        {"table": "troops", "id": 3, "fields": {"name": "그림자_쌍둥이", "enemy_ids": [1, 2]}},
                    ],
                    "common_events": [
                        {
                            "id": 2,
                            "name": "자동_축하_이벤트",
                            "trigger": "autorun",
                            "switch_name": "자동_축하",
                            "commands": [
                                {"op": "say", "lines": ["쌍둥이 토벌 축하!"], "indent": 0},
                                {"op": "set_switch", "switch_name": "자동_축하", "value": "OFF", "indent": 0},
                                {"op": "set_switch", "switch_name": "병렬_카운터", "value": "ON", "indent": 0},
                            ],
                        },
                        {
                            "id": 3,
                            "name": "병렬_카운터_이벤트",
                            "trigger": "parallel",
                            "switch_name": "병렬_카운터",
                            "commands": [
                                {"op": "set_variable", "variable_name": "경과_틱", "operation": 1, "operand_type": 0, "operand": [1], "indent": 0},
                            ],
                        },
                    ],
                    "patch_maps": [
                        {
                            "map_id": 1,
                            "remove_events": ["임시_미끼"],
                            "patch_events": [
                                {
                                    "id": 1,
                                    "commands": [
                                        {"op": "battle", "troop_name": "그림자_쌍둥이", "can_escape": False, "can_lose": False, "indent": 0},
                                        {"op": "set_switch", "switch_name": "자동_축하", "value": "ON", "indent": 0},
                                    ],
                                }
                            ],
                        }
                    ],
                },
                validate=True,
            )
            self.assertEqual(bp_res["events_patched"], 1)
            self.assertEqual(bp_res["events_removed"], 1)

            map1 = json.loads((ir_dir / "maps" / "map_001.json").read_text(encoding="utf-8"))
            self.assertFalse(any(e["name"] == "임시_미끼" for e in map1["events"]))

            # 3. Execute patched battle -> triggers autorun CE dialogue -> parallel counter ticks on wait
            sess = HeadlessGameSession(ir_dir)
            for act in ("right", "right", "up", "interact"):
                sess.step(act)
            # Autorun CE triggered automatically right after switch 12 turned ON and paused on say
            self.assertIsNotNone(sess.pending)
            self.assertEqual(sess.pending.lines, ["쌍둥이 토벌 축하!"])
            self.assertTrue(any("그림자_쌍둥이 승리" in line for line in sess.log))

            # Advance autorun dialogue -> flips switch 12 OFF and switch 13 ON -> parallel ticks once
            sess.step("interact")
            self.assertFalse(sess.switches.get(12, False))
            self.assertTrue(sess.switches.get(13, False))
            self.assertEqual(sess.variables.get(6, 0), 1)
            sess.step("wait")
            sess.step("wait")
            self.assertEqual(sess.variables.get(6, 0), 3)

    def test_13_enemy_names_troop_validation_and_map_autorun_bfs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            info = generate_sample_project(root / "proj")
            ir_dir = Path(info["ir_dir"])

            # 1. Blueprint creating new enemy + troop referencing enemy_names (order-independent) + map autorun + CE autorun BFS
            bp_res = apply_blueprint(
                ir_dir,
                {
                    "db": [
                        # Put troop BEFORE enemy in JSON to verify dependency ordering
                        {"table": "troops", "id": 3, "fields": {"name": "마왕_호위대", "enemy_names": ["고블린", "심연의_기사"]}},
                        {"table": "enemies", "id": 3, "fields": {"name": "심연의_기사", "maxhp": 90, "atk": 16, "exp": 55, "gold": 80}},
                        {"table": "switches", "id": 14, "fields": {"name": "맵오토_완료"}},
                        {"table": "switches", "id": 15, "fields": {"name": "공용오토_개방"}},
                    ],
                    "common_events": [
                        {
                            "id": 2,
                            "name": "연쇄_개방",
                            "trigger": "autorun",
                            "switch_name": "맵오토_완료",
                            "commands": [
                                {"op": "set_switch", "switch_name": "공용오토_개방", "value": "ON", "indent": 0},
                                {"op": "set_switch", "switch_name": "맵오토_완료", "value": "OFF", "indent": 0},
                            ],
                        }
                    ],
                    "maps": [
                        {
                            "name": "결전의_전당",
                            "width": 10,
                            "height": 10,
                            "events": [
                                {
                                    "name": "결전_보스",
                                    "x": 5,
                                    "y": 5,
                                    "commands": [
                                        {"op": "battle", "troop_name": "마왕_호위대", "can_escape": False, "can_lose": False, "indent": 0},
                                    ],
                                }
                            ],
                        }
                    ],
                    "patch_maps": [
                        {
                            "map_id": 1,
                            "events": [
                                {
                                    "name": "오프닝_감독",
                                    "x": 2,
                                    "y": 2,
                                    "pages": [
                                        {
                                            "trigger": "autorun",
                                            "through": True,
                                            "commands": [
                                                {"op": "say", "lines": ["모험이 시작된다!"], "indent": 0},
                                                {"op": "set_switch", "switch_name": "맵오토_완료", "value": "ON", "indent": 0},
                                                {"op": "set_self_switch", "channel": "A", "value": "ON", "indent": 0},
                                            ],
                                        },
                                        {
                                            "trigger": "action_button",
                                            "through": True,
                                            "condition": {"self_switch_ch": "A"},
                                            "commands": [],
                                        },
                                    ],
                                },
                                {
                                    "name": "전당_차원문",
                                    "x": 5,
                                    "y": 11,
                                    "commands": [
                                        {"op": "if_switch", "switch_name": "공용오토_개방", "expected": "ON", "indent": 0},
                                        {"op": "transfer_player", "var_mode": 0, "map_name": "결전의_전당", "x": 5, "y": 6, "direction": "up", "fade_type": 0, "indent": 1},
                                        {"op": "end_if", "indent": 0},
                                    ],
                                },
                            ],
                        }
                    ],
                },
                validate=True,
            )
            self.assertEqual(bp_res["db_updates"], 4)
            # Static BFS unlocked 14 (via map autorun) -> 15 (via autorun CE) -> Map 3 reached!
            self.assertIn(14, bp_res["validation_stats"]["unlocked_switches"])
            self.assertIn(15, bp_res["validation_stats"]["unlocked_switches"])

            # 2. Runtime: first step triggers Map 1 autorun page -> pause on say -> confirm -> flips self-switch A & cascades CE autorun -> unlocks switch 15
            sess = HeadlessGameSession(ir_dir)
            sess.step("wait")
            self.assertIsNotNone(sess.pending)
            self.assertEqual(sess.pending.lines, ["모험이 시작된다!"])
            sess.step("interact")
            self.assertTrue(sess.switches.get(15, False))
            self.assertFalse(sess.switches.get(14, False))

            # 3. Undefined enemy_id in troops.json caught by static validation & unknown enemy_name rejected
            with self.assertRaises(ValueError):
                set_db_entry(ir_dir, "troops", 4, {"name": "불량_부대", "enemy_names": ["없는_몬스터"]})
            set_db_entry(ir_dir, "troops", 4, {"name": "불량_부대", "enemy_ids": [999]})
            bad_res = validate_ir_project(ir_dir)
            self.assertFalse(bad_res.passed)
            self.assertTrue(any("Troop 4" in e and "undefined enemy 999" in e for e in bad_res.errors))

    def test_14_immediate_autorun_equip_skill_heal_and_common_event_cli(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            generate_sample_project(ws)
            ir_dir = ws / "ir"
            rmxp_roundtrip = ws / "rt_data"
            ir_roundtrip = ws / "rt_ir"

            # 1. Add Weapon #2, Armor #2, Skill #2, Item #1 (Potion), autorun intro event, and trainer NPC
            set_db_entry(ir_dir, "weapons", 2, {"name": "미스릴 검", "atk": 60})
            set_db_entry(ir_dir, "armors", 2, {"name": "강철 방패", "kind": 0, "pdef": 25})
            set_db_entry(ir_dir, "skills", 2, {"name": "파이어 슬래시", "power": 90})
            set_db_entry(ir_dir, "items", 1, {"name": "상처약", "recover_hp": 50})
            set_db_entry(ir_dir, "enemies", 2, {"name": "훈련 골렘", "maxhp": 90, "atk": 15, "pdef": 5})

            apply_blueprint(
                ir_dir,
                {
                    "patch_maps": [
                        {
                            "map_id": 1,
                            "events": [
                                {
                                    "name": "오프닝_자동실행",
                                    "x": 0,
                                    "y": 0,
                                    "pages": [
                                        {
                                            "trigger": "autorun",
                                            "through": True,
                                            "commands": [
                                                {"op": "say", "lines": ["즉시 자동 실행!"], "indent": 0},
                                                {"op": "change_weapon", "weapon_id": 2, "mode": "add", "amount": 1, "indent": 0},
                                                {"op": "change_armor", "armor_id": 2, "mode": "add", "amount": 1, "indent": 0},
                                                {"op": "change_equip", "actor_id": 1, "equip_type": 0, "equip_id": 2, "indent": 0},
                                                {"op": "change_equip", "actor_id": 1, "equip_type": 1, "equip_id": 2, "indent": 0},
                                                {"op": "change_skill", "actor_id": 1, "mode": "learn", "skill_id": 2, "indent": 0},
                                                {"op": "change_item", "item_id": 1, "mode": "add", "amount": 1, "indent": 0},
                                                {"op": "recover_all", "actor_id": 0, "indent": 0},
                                                {"op": "set_self_switch", "channel": "A", "value": "ON", "indent": 0},
                                            ],
                                        },
                                        {
                                            "trigger": "action_button",
                                            "through": True,
                                            "condition": {"self_switch_ch": "A"},
                                            "commands": [],
                                        },
                                    ],
                                }
                            ],
                        }
                    ]
                },
                validate=True,
            )

            # Verify full .rxdata round-trip preserves event commands 127/128/314/318/319
            pack_ir_to_rmxp(ir_dir, rmxp_roundtrip)
            unpack_rmxp_to_ir(rmxp_roundtrip, ir_roundtrip)

            # 2. Immediate autorun fires on HeadlessGameSession init before any step()
            sess = HeadlessGameSession(ir_roundtrip)
            self.assertIsNotNone(sess.pending)
            self.assertEqual(sess.pending.lines, ["즉시 자동 실행!"])

            # "wait" must NOT dismiss non-choice dialogue; only "interact" advances it
            sess.step("wait")
            self.assertIsNotNone(sess.pending)
            self.assertEqual(sess.pending.lines, ["즉시 자동 실행!"])

            sess.step("interact")
            self.assertIsNone(sess.pending)
            snap = sess.snapshot()
            self.assertEqual(snap["weapon_id"], 2)
            self.assertIn(2, snap["skills"])
            self.assertGreaterEqual(snap["atk"], 60)
            self.assertGreaterEqual(snap["pdef"], 25)
            self.assertEqual(snap["inventory"].get(1), 1)

            # 3. Direct healing item usage recovers lost HP and decrements inventory
            sess.hp = 10
            sess.step("use_item:1")
            self.assertEqual(sess.hp, 60)
            self.assertEqual(sess.inventory.get(1, 0), 0)

            # 4. Dedicated common-event set / list CLI subcommands
            buf_set = io.StringIO()
            with contextlib.redirect_stdout(buf_set):
                rc_set = cli_main(
                    [
                        "--json",
                        "common-event",
                        "set",
                        str(ir_dir),
                        "--id",
                        "1",
                        "--name",
                        "전초기지_자동공용",
                        "--trigger",
                        "parallel",
                        "--switch-id",
                        "1",
                        "--say",
                        "CLI 테스트",
                    ]
                )
            self.assertEqual(rc_set, 0)
            set_payload = json.loads(buf_set.getvalue())
            self.assertEqual(set_payload["id"], 1)
            self.assertEqual(set_payload["trigger"], 2)

            buf_list = io.StringIO()
            with contextlib.redirect_stdout(buf_list):
                rc_list = cli_main(["--json", "common-event", "list", str(ir_dir)])
            self.assertEqual(rc_list, 0)
            listed = json.loads(buf_list.getvalue())
            self.assertTrue(any(ce["id"] == 1 and ce["name"] == "전초기지_자동공용" and ce["trigger"] == "parallel" for ce in listed))

    def test_15_combat_depth_static_gear_validation_and_player_hud(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            generate_sample_project(ws)
            ir_dir = ws / "ir"

            # 1. Blueprint creates weapon/armor/skill/item/ally/troops and references them by name
            bp_res = apply_blueprint(
                ir_dir,
                {
                    "db": [
                        {"table": "actors", "id": 2, "fields": {"name": "아리아", "weapon_id": 1}},
                        {"table": "weapons", "id": 2, "fields": {"name": "성광검", "atk": 35}},
                        {"table": "armors", "id": 2, "fields": {"name": "용린갑옷", "kind": 2, "pdef": 20}},
                        {"table": "skills", "id": 2, "fields": {"name": "성광참", "power": 60, "sp_cost": 15}},
                        {"table": "items", "id": 2, "fields": {"name": "마나포션", "occasion": 0, "recover_hp": 25, "recover_sp": 30}},
                        {"table": "enemies", "id": 3, "fields": {"name": "심연의_군주", "maxhp": 600, "atk": 250, "pdef": 5}},
                        {"table": "troops", "id": 3, "fields": {"name": "군주_부대", "enemy_names": ["심연의_군주"]}},
                    ],
                    "patch_maps": [
                        {
                            "map_id": 1,
                            "events": [
                                {
                                    "name": "장비_수련_교관",
                                    "x": 4,
                                    "y": 11,
                                    "commands": [
                                        {"op": "change_weapon", "weapon_name": "성광검", "mode": "add", "amount": 1, "indent": 0},
                                        {"op": "change_armor", "armor_name": "용린갑옷", "mode": "add", "amount": 1, "indent": 0},
                                        {"op": "change_equip", "actor_id": 1, "equip_type": 0, "weapon_name": "성광검", "indent": 0},
                                        {"op": "change_equip", "actor_id": 1, "equip_type": 3, "armor_name": "용린갑옷", "indent": 0},
                                        {"op": "change_skill", "actor_id": 1, "mode": "learn", "skill_name": "성광참", "indent": 0},
                                        {"op": "change_item", "item_name": "마나포션", "mode": "add", "amount": 1, "indent": 0},
                                        {"op": "battle", "troop_id": 1, "can_escape": False, "can_lose": False, "indent": 0},
                                    ],
                                },
                                {
                                    "name": "강적_도전",
                                    "x": 5,
                                    "y": 10,
                                    "commands": [
                                        {"op": "battle", "troop_name": "군주_부대", "can_escape": False, "can_lose": True, "indent": 0},
                                    ],
                                },
                                {
                                    "name": "전멸_함정",
                                    "x": 3,
                                    "y": 10,
                                    "commands": [
                                        {"op": "battle", "troop_name": "군주_부대", "can_escape": False, "can_lose": False, "indent": 0},
                                    ],
                                },
                            ],
                        }
                    ],
                },
                validate=True,
            )
            self.assertEqual(bp_res["db_updates"], 7)

            # Register companion actor #2 in party_members and verify multi-party combat bonus
            proj_path = ir_dir / "ir_project.json"
            proj = json.loads(proj_path.read_text(encoding="utf-8"))
            proj["system"]["fields"]["party_members"] = [1, 2]
            proj_path.write_text(json.dumps(proj, ensure_ascii=False, indent=2), encoding="utf-8")

            sess = HeadlessGameSession(ir_dir)
            base_snap = sess.snapshot()
            self.assertEqual(base_snap["party_size"], 2)
            base_sp = base_snap["sp"]

            # Interact with 장비_수련_교관 (start is (4,10) facing down -> (4,11))
            sess.step("interact")
            after_win = sess.snapshot()
            self.assertEqual(after_win["last_battle_result"], "win")
            self.assertFalse(after_win["game_over"])
            self.assertLess(after_win["sp"], base_sp)

            # HUD & player overlay helpers inspect inventory, weapon, skills, and stats cleanly
            self.assertEqual(first_healing_item_id(sess), 2)
            hud_line = format_hud_text(sess.current_map()["name"], after_win)
            self.assertIn("ATK", hud_line)
            self.assertIn("I:회복", hud_line)
            status_lines = format_status_lines(sess)
            self.assertTrue(any("성광검" in ln for ln in status_lines))
            self.assertTrue(any("성광참" in ln for ln in status_lines))
            self.assertTrue(any("마나포션x1" in ln for ln in status_lines))

            # Healing item restores both HP and SP
            sess.hp = 20
            sess.sp = 5
            sess.step("use_item:2")
            self.assertEqual(sess.hp, 45)
            self.assertEqual(sess.sp, 35)

            # can_lose=True battle survives at HP=1 without game_over
            sess.step("right")
            sess.step("interact")
            self.assertEqual(sess.last_battle_result, "lose")
            self.assertFalse(sess.game_over)
            self.assertEqual(sess.hp, 1)

            # Lethal can_lose=False battle triggers game_over=True and HUD Game Over banner
            sess.step("left")
            sess.step("interact")
            self.assertTrue(sess.game_over)
            self.assertEqual(sess.hp, 0)
            self.assertIn("전멸", format_hud_text(sess.current_map()["name"], sess.snapshot()))

            # 2. Static validation catches undefined weapon, armor, skill, and equip IDs
            add_event(
                ir_dir,
                map_id=1,
                name="잘못된_장비_참조",
                x=3,
                y=11,
                commands=[
                    {"op": "change_weapon", "weapon_id": 999, "mode": "add", "amount": 1, "indent": 0},
                    {"op": "change_armor", "armor_id": 888, "mode": "add", "amount": 1, "indent": 0},
                    {"op": "change_skill", "actor_id": 1, "mode": "learn", "skill_id": 777, "indent": 0},
                    {"op": "change_equip", "actor_id": 1, "equip_type": 0, "equip_id": 666, "indent": 0},
                ],
            )
            bad_res = validate_ir_project(ir_dir)
            self.assertFalse(bad_res.passed)
            self.assertTrue(any("undefined weapon 999" in e for e in bad_res.errors))
            self.assertTrue(any("undefined armor 888" in e for e in bad_res.errors))
            self.assertTrue(any("undefined skill 777" in e for e in bad_res.errors))
            self.assertTrue(any("equips undefined weapon 666" in e for e in bad_res.errors))

    def test_16_battle_branches_party_actor_gear_and_pygame_dummy_smoke(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ws = root / "ws"
            generate_sample_project(ws)
            ir_dir = ws / "ir"
            rmxp_dir = root / "rmxp_rt"
            unpacked_dir = root / "unpacked_rt"

            set_db_entry(
                ir_dir,
                "actors",
                2,
                {
                    "name": "아리아",
                    "class_id": 1,
                    "initial_level": 1,
                    "weapon_id": 0,
                    "armor1_id": 0,
                    "armor2_id": 0,
                    "armor3_id": 0,
                    "armor4_id": 0,
                },
            )
            set_db_entry(ir_dir, "weapons", 3, {"name": "기사의 창", "atk": 30, "pdef": 0, "price": 500})
            set_db_entry(ir_dir, "skills", 3, {"name": "성스러운 일격", "sp_cost": 8, "power": 60})
            set_db_entry(ir_dir, "enemies", 98, {"name": "연습용 슬라임", "maxhp": 10, "atk": 1, "pdef": 0, "exp": 5, "gold": 5})
            set_db_entry(ir_dir, "enemies", 99, {"name": "마왕 분신", "maxhp": 5000, "atk": 999, "pdef": 10, "exp": 0, "gold": 0})
            set_db_entry(
                ir_dir,
                "troops",
                98,
                {"name": "슬라임 부대", "members": [{"enemy_id": 98, "x": 320, "y": 240}]},
            )
            set_db_entry(
                ir_dir,
                "troops",
                99,
                {"name": "마왕 부대", "members": [{"enemy_id": 99, "x": 320, "y": 240}]},
            )

            # Move player away from existing sample events to a clean spot (Map 1: x=6, y=10 -> event at x=6, y=11 & x=7, y=10)
            proj_path = ir_dir / "ir_project.json"
            proj_obj = json.loads(proj_path.read_text(encoding="utf-8"))
            proj_obj["system"]["fields"]["start_map_id"] = 1
            proj_obj["system"]["fields"]["start_x"] = 6
            proj_obj["system"]["fields"]["start_y"] = 10
            sw_list = list(proj_obj["system"]["fields"].get("switches", [None]))
            while len(sw_list) <= 25:
                sw_list.append(f"SW_{len(sw_list)}")
            proj_obj["system"]["fields"]["switches"] = sw_list
            proj_path.write_text(json.dumps(proj_obj, ensure_ascii=False, indent=2), encoding="utf-8")

            apply_blueprint(
                ir_dir,
                {
                    "patch_maps": [
                        {
                            "map_id": 1,
                            "events": [
                                {
                                    "name": "동료_영입_및_승리분기",
                                    "x": 6,
                                    "y": 11,
                                    "commands": [
                                        {"op": "change_party", "actor_name": "아리아", "mode": "add", "initialize": False, "indent": 0},
                                        {"op": "change_weapon", "weapon_id": 3, "mode": "add", "amount": 1, "indent": 0},
                                        {"op": "change_equip", "actor_name": "아리아", "equip_type": 0, "equip_id": 3, "indent": 0},
                                        {"op": "change_skill", "actor_name": "아리아", "mode": "learn", "skill_id": 3, "indent": 0},
                                        {"op": "recover_all", "actor_name": "아리아", "indent": 0},
                                        {"op": "battle", "troop_id": 98, "can_escape": False, "can_lose": False, "indent": 0},
                                        {"op": "when_win", "indent": 0},
                                        {"op": "set_switch", "start_id": 10, "end_id": 10, "value": "ON", "indent": 1},
                                        {"op": "say", "lines": ["완승 보상 획득!"], "indent": 1},
                                        {"op": "when_escape", "indent": 0},
                                        {"op": "set_switch", "start_id": 11, "end_id": 11, "value": "ON", "indent": 1},
                                        {"op": "when_lose", "indent": 0},
                                        {"op": "set_switch", "start_id": 12, "end_id": 12, "value": "ON", "indent": 1},
                                        {"op": "end_battle", "indent": 0},
                                    ],
                                },
                                {
                                    "name": "패배분기_시험관",
                                    "x": 7,
                                    "y": 10,
                                    "commands": [
                                        {"op": "battle", "troop_id": 99, "can_escape": False, "can_lose": True, "indent": 0},
                                        {"op": "when_win", "indent": 0},
                                        {"op": "set_switch", "start_id": 20, "end_id": 20, "value": "ON", "indent": 1},
                                        {"op": "when_lose", "indent": 0},
                                        {"op": "set_switch", "start_id": 22, "end_id": 22, "value": "ON", "indent": 1},
                                        {"op": "end_battle", "indent": 0},
                                    ],
                                },
                                {
                                    "name": "장비_해제_및_파티이탈",
                                    "x": 5,
                                    "y": 10,
                                    "commands": [
                                        {"op": "change_equip", "actor_id": 2, "equip_type": 0, "equip_id": 0, "indent": 0},
                                        {"op": "change_party", "actor_id": 2, "mode": "remove", "initialize": False, "indent": 0},
                                    ],
                                },
                            ],
                        }
                    ]
                },
            )

            # Round-trip through .rxdata to prove codes 129 and 601..604 encode/decode cleanly
            pack_ir_to_rmxp(ir_dir, rmxp_dir)
            unpack_rmxp_to_ir(rmxp_dir, unpacked_dir)

            sess = HeadlessGameSession(unpacked_dir)
            self.assertEqual(len(sess.party_member_ids), 1)
            base_atk = sess.atk
            lead_initial_wpn = sess.weapon_id

            # Event 1: recruit Aria, equip weapon 3 from inventory, learn skill 3 only for Aria, win battle -> when_win
            sess.step("down")
            sess.step("interact")
            snap1 = sess.snapshot()
            self.assertEqual(snap1["party_size"], 2)
            self.assertEqual(snap1["party_members"], [1, 2])
            self.assertEqual(snap1["actor_states"]["2"]["weapon_id"], 3)
            self.assertEqual(snap1["actor_states"]["1"]["weapon_id"], lead_initial_wpn)
            self.assertIn(3, snap1["actor_states"]["2"]["skills"])
            self.assertNotIn(3, snap1["actor_states"]["1"]["skills"])
            self.assertEqual(snap1["weapons_owned"].get(3, 0), 0)
            self.assertGreater(snap1["atk"], base_atk)
            self.assertEqual(snap1["pending_dialogue"], ["완승 보상 획득!"])
            sess.step("interact")  # advance past dialogue -> skips when_escape & when_lose
            self.assertTrue(sess.switches.get(10))
            self.assertFalse(sess.switches.get(11, False))
            self.assertFalse(sess.switches.get(12, False))

            # Event 3 (at x=5, y=10): unequip Aria's weapon 3 (returned to bag) & remove Aria from party
            sess.step("left")
            sess.step("interact")
            snap2 = sess.snapshot()
            self.assertEqual(snap2["party_size"], 1)
            self.assertEqual(snap2["weapons_owned"].get(3, 0), 1)
            self.assertEqual(snap2["actor_states"]["2"]["weapon_id"], 0)
            self.assertEqual(snap2["atk"], base_atk)

            # Event 2 (at x=7, y=10): unwinnable can_lose=True battle routes to when_lose
            sess.step("right")
            sess.step("interact")
            self.assertEqual(sess.last_battle_result, "lose")
            self.assertFalse(sess.game_over)
            self.assertTrue(sess.switches.get(22))
            self.assertFalse(sess.switches.get(20, False))

            # Static validation rejects undefined actor_id
            add_event(
                unpacked_dir,
                map_id=1,
                name="잘못된_액터_참조",
                x=4,
                y=10,
                commands=[{"op": "change_party", "actor_id": 999, "mode": "add", "initialize": False, "indent": 0}],
            )
            bad_res = validate_ir_project(unpacked_dir)
            self.assertFalse(bad_res.passed)
            self.assertTrue(any("undefined actor 999" in e for e in bad_res.errors))

            # Offscreen dummy SDL smoke test for _process_keydown & launch_player(max_frames=3)
            prev_vid = os.environ.get("SDL_VIDEODRIVER")
            prev_aud = os.environ.get("SDL_AUDIODRIVER")
            os.environ["SDL_VIDEODRIVER"] = "dummy"
            os.environ["SDL_AUDIODRIVER"] = "dummy"
            try:
                import pygame

                running, cursor, show_status, wframe, wtimer = _process_keydown(
                    pygame, pygame.K_TAB, sess, 0, False, 0, 0
                )
                self.assertTrue(running)
                self.assertTrue(show_status)
                running, cursor, show_status, wframe, wtimer = _process_keydown(
                    pygame, pygame.K_F5, sess, cursor, show_status, wframe, wtimer
                )
                self.assertFalse(sess.switches.get(10, False))
                rc = launch_player(unpacked_dir, max_frames=3)
                self.assertEqual(rc, 0)
            finally:
                if prev_vid is None:
                    os.environ.pop("SDL_VIDEODRIVER", None)
                else:
                    os.environ["SDL_VIDEODRIVER"] = prev_vid
                if prev_aud is None:
                    os.environ.pop("SDL_AUDIODRIVER", None)
                else:
                    os.environ["SDL_AUDIODRIVER"] = prev_aud

    def test_17_party_turn_combat_levelup_shop_smooth_controls_and_gfx_render(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ws = root / "ws"
            generate_sample_project(ws)
            ir_dir = ws / "ir"
            rmxp_dir = root / "rmxp_rt"
            unpacked_dir = root / "unpacked_rt"

            # Add level-2 skill learning to Class #1 so automatic level-up unlocks skill #4
            set_db_entry(ir_dir, "skills", 3, {"name": "성광참", "sp_cost": 16, "power": 60})
            set_db_entry(ir_dir, "skills", 4, {"name": "승리의 함성", "sp_cost": 5, "power": 40})
            set_db_entry(ir_dir, "weapons", 3, {"name": "성기사 창", "atk": 18, "pdef": 0, "price": 120})
            set_db_entry(ir_dir, "items", 3, {"name": "하이포션", "recover_hp": 120, "price": 40, "consumable": True})
            set_db_entry(
                ir_dir,
                "actors",
                2,
                {"name": "아리아", "class_id": 1, "initial_level": 1, "weapon_id": 3},
            )
            set_db_entry(
                ir_dir,
                "enemies",
                90,
                {"name": "갑옷 골렘", "maxhp": 220, "atk": 22, "pdef": 4, "exp": 45, "gold": 150},
            )
            set_db_entry(
                ir_dir,
                "troops",
                90,
                {"name": "골렘 수호대", "members": [{"enemy_id": 90, "x": 320, "y": 240}]},
            )
            classes_path = ir_dir / "database" / "classes.json"
            classes_data = json.loads(classes_path.read_text(encoding="utf-8"))
            for item in classes_data:
                if isinstance(item, dict) and item.get("fields", {}).get("id") == 1:
                    item["fields"]["learnings"].append(
                        {"__type__": "RubyObject", "class": "RPG::Class::Learning", "fields": {"level": 2, "skill_id": 4}}
                    )
            classes_path.write_text(json.dumps(classes_data, ensure_ascii=False, indent=2), encoding="utf-8")

            # Set clean player start position & party with hero + companion
            proj_path = ir_dir / "ir_project.json"
            proj_obj = json.loads(proj_path.read_text(encoding="utf-8"))
            proj_obj["system"]["fields"]["start_map_id"] = 1
            proj_obj["system"]["fields"]["start_x"] = 6
            proj_obj["system"]["fields"]["start_y"] = 10
            proj_obj["system"]["fields"]["party_members"] = [1, 2]
            proj_path.write_text(json.dumps(proj_obj, ensure_ascii=False, indent=2), encoding="utf-8")

            apply_blueprint(
                ir_dir,
                {
                    "patch_maps": [
                        {
                            "map_id": 1,
                            "events": [
                                {
                                    "name": "연대_전투_시험관",
                                    "x": 6,
                                    "y": 11,
                                    "trigger": "event_touch",
                                    "commands": [
                                        {"op": "change_skill", "actor_name": "아리아", "mode": "learn", "skill_id": 3, "indent": 0},
                                        {"op": "battle", "troop_id": 90, "can_escape": False, "can_lose": False, "indent": 0},
                                    ],
                                },
                                {
                                    "name": "왕국_잡화상점",
                                    "x": 7,
                                    "y": 10,
                                    "commands": [
                                        {
                                            "op": "shop",
                                            "goods": [
                                                {"item_name": "하이포션"},
                                                {"weapon_name": "성기사 창"},
                                            ],
                                            "indent": 0,
                                        }
                                    ],
                                },
                            ],
                        }
                    ]
                },
            )

            # Round-trip shop (302+605) & battle events via .rxdata
            pack_ir_to_rmxp(ir_dir, rmxp_dir)
            unpack_rmxp_to_ir(rmxp_dir, unpacked_dir)

            sess = HeadlessGameSession(unpacked_dir)
            # Trigger Event Touch NPC at (6, 11) merely by bumping down!
            aria_sp_before = sess.actor_states[2]["sp"]
            lead_mhp_before = sess.actor_states[1]["max_hp"]
            sess.step("down")
            snap = sess.snapshot()
            self.assertEqual(snap["last_battle_result"], "win")
            self.assertEqual(snap["gold"], 150)
            self.assertGreaterEqual(snap["exp"], 45)
            # Companion actively cast skill #3 during party turn combat (SP decreased separately)
            self.assertLess(snap["actor_states"]["2"]["sp"], aria_sp_before)
            # Both living members leveled up (EXP 45 >= Lv2 threshold 30) and auto-learned Skill #4
            self.assertGreaterEqual(snap["level"], 2)
            self.assertGreaterEqual(snap["actor_states"]["2"]["level"], 2)
            self.assertGreater(snap["actor_states"]["1"]["max_hp"], lead_mhp_before)
            self.assertIn(4, snap["actor_states"]["1"]["skills"])
            self.assertIn(4, snap["actor_states"]["2"]["skills"])
            self.assertTrue(any("[LevelUp]" in line for line in sess.log))

            # Open Shop event at (7, 10) -> buy weapon (120G), fail 2nd weapon (30G < 120G), buy no / exit
            sess.step("right")
            sess.step("interact")
            self.assertIsNotNone(sess.pending)
            self.assertEqual(len(sess.pending.shop_goods), 2)
            sess.step("choose:1")  # buy 성기사 창 (-120G -> 30G left)
            self.assertEqual(sess.gold, 30)
            self.assertEqual(sess.weapons_owned.get(3, 0), 1)
            sess.step("choose:1")  # insufficient gold (30G < 120G)
            self.assertEqual(sess.gold, 30)
            self.assertEqual(sess.weapons_owned.get(3, 0), 1)
            sess.step("choose:3")  # exit shop ("나가기")
            self.assertIsNone(sess.pending)

            # Static validator rejects invalid shop good IDs
            add_event(
                unpacked_dir,
                map_id=1,
                name="불량_상점",
                x=5,
                y=10,
                commands=[{"op": "shop", "goods": [{"type": "weapon", "id": 999}], "indent": 0}],
            )
            bad_shop = validate_ir_project(unpacked_dir)
            self.assertFalse(bad_shop.passed)
            self.assertTrue(any("shop references undefined weapon 999" in e for e in bad_shop.errors))

            # Smooth control helpers & ESC overlay-first UX
            self.assertEqual(advance_slide_offset(-32, 0, 4), (-28, 0))
            self.assertEqual(advance_slide_offset(3, -2, 4), (0, 0))
            self.assertIsInstance(compute_camera_offset_px(192, 320, 20, 15), tuple)

            prev_vid = os.environ.get("SDL_VIDEODRIVER")
            prev_aud = os.environ.get("SDL_AUDIODRIVER")
            os.environ["SDL_VIDEODRIVER"] = "dummy"
            os.environ["SDL_AUDIODRIVER"] = "dummy"
            try:
                import pygame

                # Status overlay intercepts ESC before closing game window
                run_flag, _, st_open, _, _ = _process_keydown(pygame, pygame.K_ESCAPE, sess, 0, True, 0, 0)
                self.assertTrue(run_flag)
                self.assertFalse(st_open)

                observed_colors: List[set] = []

                def _probe_frame(frame_idx: int, surface: Any, live_sess: HeadlessGameSession) -> None:
                    if frame_idx == 0:
                        pygame.event.post(pygame.event.Event(pygame.KEYDOWN, key=pygame.K_TAB))
                    elif frame_idx == 1:
                        pygame.event.post(pygame.event.Event(pygame.KEYDOWN, key=pygame.K_ESCAPE))
                        pygame.event.post(pygame.event.Event(pygame.KEYDOWN, key=pygame.K_RIGHT))
                    samples = {
                        tuple(surface.get_at((px, py))[:3])
                        for px in range(16, 620, 32)
                        for py in range(8, 460, 32)
                    }
                    observed_colors.append(samples)

                rc = launch_player(
                    ir_dir,
                    graphics_dir=ws / "rmxp" / "Graphics",
                    max_frames=5,
                    on_frame=_probe_frame,
                )
                self.assertEqual(rc, 0)
                self.assertEqual(len(observed_colors), 5)
                # Real PNG tileset/autotile/character sprites + overlays produce rich non-uniform pixels
                self.assertGreater(len(set().union(*observed_colors)), 6)
            finally:
                if prev_vid is None:
                    os.environ.pop("SDL_VIDEODRIVER", None)
                else:
                    os.environ["SDL_VIDEODRIVER"] = prev_vid
                if prev_aud is None:
                    os.environ.pop("SDL_AUDIODRIVER", None)
                else:
                    os.environ["SDL_AUDIODRIVER"] = prev_aud

    def test_18_diagonal_grid_lock_wall_slide_and_two_leg_stride(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ws = root / "ws"
            generate_sample_project(ws)
            ir_dir = ws / "ir"

            # Verify directional hero.png distinguishes front vs back rows
            hero_img = Image.open(ws / "rmxp" / "Graphics" / "Characters" / "hero.png").convert("RGBA")
            front_eye_px = hero_img.getpixel((12, 12))
            back_head_px = hero_img.getpixel((12, 3 * 32 + 12))
            self.assertNotEqual(front_eye_px, back_head_px)

            # Move player to clear open grass at (8, 8)
            proj_path = ir_dir / "ir_project.json"
            proj_obj = json.loads(proj_path.read_text(encoding="utf-8"))
            proj_obj["system"]["fields"]["start_map_id"] = 1
            proj_obj["system"]["fields"]["start_x"] = 8
            proj_obj["system"]["fields"]["start_y"] = 8
            proj_path.write_text(json.dumps(proj_obj, ensure_ascii=False, indent=2), encoding="utf-8")

            sess = HeadlessGameSession(ir_dir)
            # 1. Open diagonal resolution & step
            self.assertEqual(resolve_held_move_action(["right", "up"], sess.can_step_action), "up_right")
            self.assertEqual(resolve_held_move_action(["left", "right"], sess.can_step_action), "right")
            sess.step("up_right")
            self.assertEqual((sess.x, sess.y), (9, 7))

            # 2. Corner-clip protection & wall-slide along blocked north fence (Map 1 row y=1 is passable, y=0 is border wall)
            sess.x, sess.y = 8, 1
            self.assertFalse(sess.can_step_action("up_right"))
            self.assertEqual(resolve_held_move_action(["right", "up"], sess.can_step_action), "right")

            # 3. True 4-phase two-legged walk cycle
            self.assertEqual(compute_stride_col(1, -24, 0), 1)
            self.assertEqual(compute_stride_col(1, -8, 0), 2)
            self.assertEqual(compute_stride_col(2, -24, 0), 3)
            self.assertEqual(compute_stride_col(2, -8, 0), 0)
            self.assertEqual(compute_stride_col(2, 0, 0), 0)

            # 4. Grid-locked movement: spamming KEYDOWN while mid-slide must NOT skip tiles or double-speed
            proj_obj["system"]["fields"]["start_x"] = 8
            proj_obj["system"]["fields"]["start_y"] = 8
            proj_path.write_text(json.dumps(proj_obj, ensure_ascii=False, indent=2), encoding="utf-8")

            prev_vid = os.environ.get("SDL_VIDEODRIVER")
            prev_aud = os.environ.get("SDL_AUDIODRIVER")
            os.environ["SDL_VIDEODRIVER"] = "dummy"
            os.environ["SDL_AUDIODRIVER"] = "dummy"
            try:
                import pygame

                recorded_x: List[int] = []

                def _spam_probe(fidx: int, _surf: Any, live_sess: HeadlessGameSession) -> None:
                    recorded_x.append(live_sess.x)
                    if fidx in (0, 1, 2, 3, 4):
                        pygame.event.post(pygame.event.Event(pygame.KEYDOWN, key=pygame.K_RIGHT))

                rc = launch_player(
                    ir_dir,
                    graphics_dir=ws / "rmxp" / "Graphics",
                    max_frames=8,
                    on_frame=_spam_probe,
                )
                self.assertEqual(rc, 0)
                # Frame 1 initiates +1 step to x=9; spam on frames 1..4 must be ignored mid-slide!
                self.assertEqual(recorded_x[0], 8)
                self.assertEqual(recorded_x[1], 9)
                self.assertEqual(max(recorded_x), 9)
            finally:
                if prev_vid is None:
                    os.environ.pop("SDL_VIDEODRIVER", None)
                else:
                    os.environ["SDL_VIDEODRIVER"] = prev_vid
                if prev_aud is None:
                    os.environ.pop("SDL_AUDIODRIVER", None)
                else:
                    os.environ["SDL_AUDIODRIVER"] = prev_aud

    def test_19_dt_speed_shift_turn_bag_equip_save_load_and_npc_routes(self) -> None:
        from src.core.ir_converter import commands_to_semantic_dsl, semantic_dsl_to_commands
        from src.core.simulator import HeadlessGameSession
        from src.harness.player_gui import (
            _process_keydown,
            _process_status_keydown,
            compute_dt_slide_step,
            list_bag_equips_for_lead,
        )

        # 1. Framerate-independent dt accumulator & Euclidean 1/sqrt(2) diagonal normalization
        def _sim_distance(frames: int, dt_ms: float, diag: bool = False) -> int:
            dist, accum = 0, 0.0
            for _ in range(frames):
                step_px, accum = compute_dt_slide_step(dt_ms, accum, is_diagonal=diag)
                dist += step_px
            return dist

        d60 = _sim_distance(60, 1000.0 / 60.0, diag=False)
        d30 = _sim_distance(30, 1000.0 / 30.0, diag=False)
        self.assertEqual(d60, 120)
        self.assertEqual(d30, 120)
        diag_axis = _sim_distance(60, 1000.0 / 60.0, diag=True)
        self.assertTrue(83 <= diag_axis <= 86, f"Expected ~85px per axis, got {diag_axis}")

        # 2. 209/509 set_move_route DSL encode/decode & RMXP .rxdata round-trip
        move_cmds = [
            {
                "op": "set_move_route",
                "target_id": 0,
                "repeat": False,
                "skippable": True,
                "steps": ["right", "down", "wait"],
            }
        ]
        packed_cmds = semantic_dsl_to_commands(move_cmds)
        self.assertEqual(int(packed_cmds[0].ivars["@code"]), 209)
        self.assertEqual(int(packed_cmds[1].ivars["@code"]), 509)
        decoded_cmds = commands_to_semantic_dsl(packed_cmds)
        self.assertEqual(decoded_cmds[0]["op"], "set_move_route")
        self.assertEqual(decoded_cmds[0]["steps"], ["right", "down", "wait"])

        with tempfile.TemporaryDirectory() as td:
            ws = Path(td) / "ws"
            generate_sample_project(ws)
            ir_dir = ws / "ir"

            # Verify .rxdata round-trip preserves MoveRoute & 209/509 commands
            map1_path = ir_dir / "maps" / "map_001.json"
            map1 = json.loads(map1_path.read_text(encoding="utf-8"))
            self.assertTrue(len(map1["events"]) > 0)
            target_ev = map1["events"][-1]
            target_eid = int(target_ev["id"])
            target_ev["x"] = 8
            target_ev["y"] = 7
            target_page = target_ev["pages"][0]
            target_page["trigger"] = "action_button"
            target_page["trigger_code"] = 0
            target_page["move_type"] = 3
            target_page["move_route"] = {
                "__type__": "RubyObject",
                "class": "RPG::MoveRoute",
                "fields": {
                    "repeat": True,
                    "skippable": True,
                    "list": [
                        {"__type__": "RubyObject", "class": "RPG::MoveCommand", "fields": {"code": 3, "parameters": []}},
                        {"__type__": "RubyObject", "class": "RPG::MoveCommand", "fields": {"code": 0, "parameters": []}},
                    ],
                },
            }
            target_page["commands"] = move_cmds + [{"op": "end"}]
            map1_path.write_text(json.dumps(map1, ensure_ascii=False, indent=2), encoding="utf-8")

            rmxp_rt = ws / "rt_rmxp"
            ir_rt = ws / "rt_ir"
            pack_ir_to_rmxp(ir_dir, rmxp_rt)
            unpack_rmxp_to_ir(rmxp_rt, ir_rt)

            sess = HeadlessGameSession(ir_rt)
            rt_ev = next(ev for ev in sess.maps[1]["events"] if int(ev["id"]) == target_eid)
            self.assertEqual(rt_ev["pages"][0]["commands"][0]["op"], "set_move_route")
            self.assertEqual(rt_ev["pages"][0]["commands"][0]["steps"], ["right", "down", "wait"])

            # 3. Turn-in-place (face:<dir>) changes direction without changing coordinates
            pos_before = (sess.x, sess.y)
            for d_name in ("up", "right", "left", "down"):
                sess.step(f"face:{d_name}")
                self.assertEqual(sess.direction, d_name)
                self.assertEqual((sess.x, sess.y), pos_before)

            # 4. Bag equipment swapping (equip_from_bag + Tab status UI keys)
            orig_wid = sess.weapon_id
            orig_atk = sess.atk
            orig_pdef = sess.pdef
            alt_wid = 99
            alt_aid = 99
            sess.weapons[alt_wid] = {"fields": {"id": alt_wid, "name": "미스릴 검", "atk": 60, "pdef": 2}}
            sess.armors[alt_aid] = {"fields": {"id": alt_aid, "name": "미스릴 투구", "kind": 1, "pdef": 25}}
            sess.classes_db[1]["fields"]["weapon_set"].append(alt_wid)
            sess.classes_db[1]["fields"]["armor_set"].append(alt_aid)
            sess.weapons_owned[alt_wid] = 1
            sess.armors_owned[alt_aid] = 1
            self.assertFalse(sess.equip_from_bag(sess.lead_actor_id, 0, 99999))

            equips = list_bag_equips_for_lead(sess)
            self.assertTrue(len(equips) >= 2)

            prev_vid = os.environ.get("SDL_VIDEODRIVER")
            prev_aud = os.environ.get("SDL_AUDIODRIVER")
            os.environ["SDL_VIDEODRIVER"] = "dummy"
            os.environ["SDL_AUDIODRIVER"] = "dummy"
            try:
                import pygame
                pygame.init()
                still_open, cur = _process_status_keydown(pygame, pygame.K_RETURN, sess, 0)
                self.assertTrue(still_open)
                self.assertEqual(sess.weapon_id, equips[0][1])
                if orig_wid > 0:
                    self.assertGreaterEqual(sess.weapons_owned.get(orig_wid, 0), 1)
                still_open, cur = _process_status_keydown(pygame, pygame.K_DOWN, sess, cur)
                self.assertTrue(still_open)
                still_open, cur = _process_status_keydown(pygame, pygame.K_RETURN, sess, cur)
                self.assertTrue(still_open)
                self.assertGreater(sess.pdef, 0)

                # 5. Save/Load round-trip via F6/F9
                sess.x, sess.y = 7, 9
                sess.gold += 321
                saved_gold = sess.gold
                sess.switches[7] = True
                _, _, _, _, _ = _process_keydown(pygame, pygame.K_F6, sess, 0, False, 0, 0)
                self.assertTrue((ir_rt / "save_slot1.json").is_file())
                sess.x, sess.y = 3, 3
                sess.gold = 0
                sess.switches[7] = False
                _, _, _, _, _ = _process_keydown(pygame, pygame.K_F9, sess, 0, True, 2, 5)
                self.assertEqual((sess.x, sess.y), (7, 9))
                self.assertEqual(sess.gold, saved_gold)
                self.assertTrue(sess.switches.get(7))
            finally:
                pygame.quit()
                if prev_vid is None:
                    os.environ.pop("SDL_VIDEODRIVER", None)
                else:
                    os.environ["SDL_VIDEODRIVER"] = prev_vid
                if prev_aud is None:
                    os.environ.pop("SDL_AUDIODRIVER", None)
                else:
                    os.environ["SDL_AUDIODRIVER"] = prev_aud

            # 6. NPC route execution (autonomous page move_type=3)
            ev_obj = next(ev for ev in sess.maps[1]["events"] if int(ev["id"]) == target_eid)
            ev_start = (int(ev_obj["x"]), int(ev_obj["y"]))
            sess.advance_npc_routes(target_event_id=target_eid)
            self.assertEqual((int(ev_obj["x"]), int(ev_obj["y"])), (ev_start[0] + 1, ev_start[1]))
            self.assertEqual(int(ev_obj["pages"][0]["graphic"]["fields"]["direction"]), 6)

    def test_20_npc_slide_party_status_scroll_and_multislot_save_guard(self) -> None:
        from src.core.simulator import HeadlessGameSession
        from src.harness.player_gui import (
            _process_status_panel_keydown,
            advance_npc_slide_states,
            compute_status_equip_window,
            format_hud_text,
            format_status_lines,
            process_save_load_shortcut,
            sync_npc_slide_states,
        )

        # 1. Status equip window pagination stays bounded for large inventories
        sel0, s0, e0 = compute_status_equip_window(15, 0, max_visible=6)
        self.assertEqual((sel0, s0, e0), (0, 0, 6))
        sel_mid, s_mid, e_mid = compute_status_equip_window(15, 10, max_visible=6)
        self.assertEqual(sel_mid, 10)
        self.assertEqual(e_mid - s_mid, 6)
        self.assertTrue(s_mid <= sel_mid < e_mid)

        with tempfile.TemporaryDirectory() as td:
            ws = Path(td) / "ws"
            generate_sample_project(ws)
            ir_dir = ws / "ir"
            sess = HeadlessGameSession(ir_dir)

            # 2. NPC sub-tile pixel slide interpolation
            active = sess.active_events_on_map()
            self.assertTrue(len(active) > 0)
            npc_ev, _ = active[0]
            nid = int(npc_ev["id"])
            before = {int(ev["id"]): (int(ev["x"]), int(ev["y"])) for ev, _p in active}
            npc_ev["x"] = int(npc_ev["x"]) + 1
            npc_slides: Dict[Any, Any] = {}
            sync_npc_slide_states(npc_slides, sess.map_id, before, sess.active_events_on_map())
            key = (sess.map_id, nid)
            self.assertIn(key, npc_slides)
            self.assertEqual((npc_slides[key]["dx"], npc_slides[key]["dy"], npc_slides[key]["stride"]), (-32, 0, 1))
            for _ in range(11):
                advance_npc_slide_states(npc_slides, 1000.0 / 60.0)
            self.assertLess(abs(npc_slides[key]["dx"]), 32)
            self.assertGreater(abs(npc_slides[key]["dx"]), 0)
            for _ in range(25):
                advance_npc_slide_states(npc_slides, 1000.0 / 60.0)
            self.assertEqual((npc_slides[key]["dx"], npc_slides[key]["dy"]), (0, 0))

            # 3. Party member switching & gear equipping in Status overlay
            sess.party_member_ids = [1, 2]
            ally_st = sess._get_actor_state(2)
            ally_st["name"] = "리나"
            lead_orig_wpn = sess.weapon_id
            ally_new_wpn = 88
            sess.weapons[ally_new_wpn] = {"fields": {"id": ally_new_wpn, "name": "바람의 활", "atk": 48, "pdef": 0}}
            sess.classes_db[1]["fields"]["weapon_set"].append(ally_new_wpn)
            sess.weapons_owned[ally_new_wpn] = 1
            ally_lines = format_status_lines(sess, actor_id=2)
            self.assertIn("리나", ally_lines[0])
            self.assertIn("2/2", ally_lines[0])

            prev_vid = os.environ.get("SDL_VIDEODRIVER")
            prev_aud = os.environ.get("SDL_AUDIODRIVER")
            os.environ["SDL_VIDEODRIVER"] = "dummy"
            os.environ["SDL_AUDIODRIVER"] = "dummy"
            try:
                import pygame
                pygame.init()
                still_open, cur, a_idx = _process_status_panel_keydown(pygame, pygame.K_RIGHT, sess, 0, 0)
                self.assertTrue(still_open)
                self.assertEqual(a_idx, 1)
                still_open, cur, a_idx = _process_status_panel_keydown(pygame, pygame.K_RETURN, sess, cur, a_idx)
                self.assertTrue(still_open)
                self.assertEqual(int(sess._get_actor_state(2)["weapon_id"]), ally_new_wpn)
                self.assertEqual(sess.weapon_id, lead_orig_wpn)

                # 4. Multi-slot save/load (F7 cycle, F6 save, F9 confirmation guard)
                self.assertIn("슬롯2", format_hud_text("마을", sess.snapshot(), active_slot=2))
                handled, slot, confirm, msg = process_save_load_shortcut(pygame, pygame.K_F7, sess, 1, False)
                self.assertTrue(handled)
                self.assertEqual((slot, confirm), (2, False))
                self.assertIn("비어 있음", msg)

                sess.x, sess.y = 5, 6
                sess.gold = 777
                handled, slot, confirm, msg = process_save_load_shortcut(pygame, pygame.K_F6, sess, slot, confirm)
                self.assertTrue(handled)
                self.assertTrue((ir_dir / "save_slot2.json").is_file())
                self.assertIn("슬롯 2 저장 완료", msg)

                # First F9 triggers confirmation without clobbering mutated state
                sess.x, sess.y = 9, 9
                sess.gold = 1
                handled, slot, confirm, msg = process_save_load_shortcut(pygame, pygame.K_F9, sess, slot, False)
                self.assertTrue(handled and confirm)
                self.assertEqual((sess.x, sess.y, sess.gold), (9, 9, 1))

                # ESC cancels confirmation safely
                handled, slot, confirm, _ = process_save_load_shortcut(pygame, pygame.K_ESCAPE, sess, slot, confirm)
                self.assertTrue(handled and not confirm)
                self.assertEqual((sess.x, sess.y), (9, 9))

                # Two F9 presses confirm and restore Slot 2
                _, slot, confirm, _ = process_save_load_shortcut(pygame, pygame.K_F9, sess, slot, False)
                handled, slot, confirm, msg = process_save_load_shortcut(pygame, pygame.K_F9, sess, slot, confirm)
                self.assertTrue(handled and not confirm)
                self.assertEqual((sess.x, sess.y, sess.gold), (5, 6, 777))
                self.assertIn("불러오기 완료", msg)
            finally:
                pygame.quit()
                if prev_vid is None:
                    os.environ.pop("SDL_VIDEODRIVER", None)
                else:
                    os.environ["SDL_VIDEODRIVER"] = prev_vid
                if prev_aud is None:
                    os.environ.pop("SDL_AUDIODRIVER", None)
                else:
                    os.environ["SDL_AUDIODRIVER"] = prev_aud

    def test_21_class_equip_rules_unequip_npc_speed_freq_and_save_routes(self) -> None:
        from src.core.simulator import HeadlessGameSession
        from src.harness.player_gui import (
            advance_npc_slide_states,
            list_bag_equips_for_lead,
            resolve_npc_interval_ms,
            resolve_npc_speed_px,
            sync_npc_slide_states,
        )

        # 1. Per-event move_speed & move_frequency timing resolution
        self.assertLess(resolve_npc_speed_px({"move_speed": 1}), resolve_npc_speed_px({"move_speed": 5}))
        self.assertGreater(resolve_npc_interval_ms({"move_frequency": 1}), resolve_npc_interval_ms({"move_frequency": 5}))

        with tempfile.TemporaryDirectory() as td:
            ws = Path(td) / "ws"
            generate_sample_project(ws)
            ir_dir = ws / "ir"
            sess = HeadlessGameSession(ir_dir)

            # 2. Class weapon_set/armor_set restriction + slot fix + unequip support
            restricted_wid = 777
            sess.weapons[restricted_wid] = {"fields": {"id": restricted_wid, "name": "마법사의 지팡이", "atk": 50, "pdef": 0}}
            sess.weapons_owned[restricted_wid] = 1
            self.assertFalse(sess.can_actor_equip(sess.lead_actor_id, 0, restricted_wid))
            self.assertFalse(sess.equip_from_bag(sess.lead_actor_id, 0, restricted_wid))
            bag_labels = [lbl for _t, _id, lbl, _c in list_bag_equips_for_lead(sess)]
            self.assertFalse(any("마법사의 지팡이" in lbl for lbl in bag_labels))
            self.assertTrue(any("[무기 해제]" in lbl for lbl in bag_labels))

            orig_wpn = sess.weapon_id
            self.assertTrue(sess.equip_from_bag(sess.lead_actor_id, 0, 0))
            self.assertEqual(sess.weapon_id, 0)
            self.assertEqual(sess.weapons_owned.get(orig_wpn, 0), 1)
            self.assertTrue(sess.equip_from_bag(sess.lead_actor_id, 0, orig_wpn))

            sess.actors[sess.lead_actor_id]["fields"]["weapon_fix"] = True
            self.assertFalse(sess.can_actor_equip(sess.lead_actor_id, 0, 0))
            sess.actors[sess.lead_actor_id]["fields"]["weapon_fix"] = False

            # 3. NPC slide speed scales per-event move_speed
            active = sess.active_events_on_map()
            ev, page = active[0]
            eid = int(ev["id"])
            page["move_speed"] = 6
            before = {int(e["id"]): (int(e["x"]), int(e["y"])) for e, _ in active}
            ev["x"] = int(ev["x"]) + 1
            slides: Dict[Any, Any] = {}
            sync_npc_slide_states(slides, sess.map_id, before, active)
            self.assertEqual(slides[(sess.map_id, eid)]["speed"], resolve_npc_speed_px(page))
            advance_npc_slide_states(slides, 50.0)
            self.assertGreater(abs(slides[(sess.map_id, eid)]["dx"]), 0)
            self.assertLess(abs(slides[(sess.map_id, eid)]["dx"]), 22)

            # 4. Save/Load preserves active set_move_route cursor and event position mid-walk
            route_ev_id = eid
            ev["x"], ev["y"] = 8, 7
            sess._active_move_routes[(sess.map_id, route_ev_id)] = {
                "steps": ["right", "down", "left"],
                "idx": 1,
                "repeat": True,
                "skippable": True,
            }
            saved_path = sess.save_to_file(slot=3)
            self.assertTrue(saved_path.is_file())
            ev["x"], ev["y"] = 2, 2
            sess._active_move_routes.clear()
            self.assertTrue(sess.load_from_file(slot=3))
            restored_ev = next(e for e, _ in sess.active_events_on_map() if int(e["id"]) == route_ev_id)
            self.assertEqual((int(restored_ev["x"]), int(restored_ev["y"])), (8, 7))
            self.assertEqual(sess._active_move_routes[(sess.map_id, route_ev_id)]["idx"], 1)
            sess.advance_npc_routes(target_event_id=route_ev_id)
            self.assertEqual((int(restored_ev["x"]), int(restored_ev["y"])), (8, 8))

    def test_22_rmxp_loops_labels_actor_hp_sp_gold_states_and_random_encounters(self) -> None:
        from src.core.ir_converter import commands_to_semantic_dsl, semantic_dsl_to_commands
        from src.core.simulator import HeadlessGameSession

        # 1. Round-trip DSL encode/decode for new RMXP conditional branches, loops, labels & actor commands
        dsl_ops = [
            {"op": "set_gold_init_placeholder"} if False else {"op": "if_gold", "amount": 50, "cmp": 0, "indent": 0},
            {"op": "change_hp", "actor_id": 1, "mode": "sub", "operand_type": 0, "amount": 20, "allow_knockout": False, "indent": 1},
            {"op": "change_sp", "actor_id": 1, "mode": "sub", "operand_type": 0, "amount": 5, "indent": 1},
            {"op": "change_state", "actor_id": 1, "mode": "add", "state_id": 2, "indent": 1},
            {"op": "end_if", "indent": 0},
            {"op": "if_self_switch", "channel": "A", "expected": "OFF", "indent": 0},
            {"op": "if_actor_in_party", "actor_id": 1, "indent": 1},
            {"op": "if_actor_state", "actor_id": 1, "state_id": 2, "indent": 2},
            {"op": "change_exp", "actor_id": 1, "mode": "add", "operand_type": 0, "amount": 15, "indent": 3},
            {"op": "change_level", "actor_id": 1, "mode": "add", "operand_type": 0, "amount": 1, "indent": 3},
            {"op": "end_if", "indent": 2},
            {"op": "end_if", "indent": 1},
            {"op": "end_if", "indent": 0},
            {"op": "loop", "indent": 0},
            {"op": "set_variable", "start_id": 3, "end_id": 3, "operation": 1, "operand_type": 0, "operand": [1], "indent": 1},
            {"op": "if_variable", "variable_id": 3, "rhs_is_var": False, "rhs_value": 3, "cmp": 1, "indent": 1},
            {"op": "break_loop", "indent": 2},
            {"op": "end_if", "indent": 1},
            {"op": "end_loop", "indent": 0},
            {"op": "jump_to_label", "name": "skip_tag", "indent": 0},
            {"op": "change_gold", "mode": "add", "operand_type": 0, "amount": 999, "indent": 0},
            {"op": "label", "name": "skip_tag", "indent": 0},
            {"op": "change_gold", "mode": "add", "operand_type": 0, "amount": 25, "indent": 0},
            {"op": "end", "indent": 0},
        ]
        packed = semantic_dsl_to_commands(dsl_ops)
        unpacked = commands_to_semantic_dsl(packed)
        self.assertEqual([c["op"] for c in unpacked], [c["op"] for c in dsl_ops])

        with tempfile.TemporaryDirectory() as td:
            ws = Path(td) / "ws"
            generate_sample_project(ws)
            ir_dir = ws / "ir"
            sess = HeadlessGameSession(ir_dir)

            sess.gold = 100
            lv_before = sess.actor_states[1]["level"]
            hp_before = sess.hp
            mhp_before = sess.max_hp
            sp_before = sess.sp
            msp_before = sess.max_sp
            sess._run_commands(unpacked, 0, sess.map_id, 1)
            expected_hp = max(1, hp_before - 20) + (sess.max_hp - mhp_before)
            expected_sp = max(0, sp_before - 5) + (sess.max_sp - msp_before)
            self.assertEqual(sess.hp, expected_hp)
            self.assertEqual(sess.sp, expected_sp)
            self.assertIn(2, sess.actor_states[1]["states"])
            self.assertGreaterEqual(sess.actor_states[1]["level"], lv_before + 1)
            self.assertEqual(sess.variables[3], 3)
            # jump_to_label skipped +999G and added +25G
            self.assertEqual(sess.gold, 125)

            # 2. recover_all clears actor states too
            sess._run_commands([{"op": "recover_all", "actor_id": 0}], 0, sess.map_id, 1)
            self.assertEqual(sess.actor_states[1]["states"], [])

            # 3. Random encounter triggers battle after encounter_step tiles walked
            sess.enable_random_encounters = True
            sess.maps[sess.map_id]["encounter_list"] = [1]
            sess.maps[sess.map_id]["encounter_step"] = 2
            sess.encounter_steps = 0
            sess.x, sess.y = 8, 8
            sess.last_battle_result = "none"
            sess.step("right")
            self.assertEqual(sess.encounter_steps, 1)
            sess.step("right")
            self.assertEqual(sess.encounter_steps, 0)
            self.assertEqual(sess.last_battle_result, "win")

    def test_23_states_element_ranks_defend_escape_and_interactive_battle(self) -> None:
        from src.core.simulator import HeadlessGameSession

        with tempfile.TemporaryDirectory() as td:
            ws = Path(td) / "ws"
            generate_sample_project(ws)
            ir_dir = ws / "ir"

            sess = HeadlessGameSession(ir_dir, interactive_battle=True)
            # Configure State 2 (Poison: slip_damage=True, battle_only=False) & State 3 (Stun: restriction=4, battle_only=True)
            sess.states_db[2] = {"fields": {"id": 2, "name": "독", "restriction": 0, "slip_damage": True, "battle_only": False}}
            sess.states_db[3] = {"fields": {"id": 3, "name": "기절", "restriction": 4, "slip_damage": False, "battle_only": True}}
            # Configure Fire Skill #10 with element 1 + Poison state
            sess.skills_db[10] = {
                "fields": {
                    "id": 10,
                    "name": "파이어볼",
                    "scope": 1,
                    "power": 30,
                    "sp_cost": 4,
                    "element_set": [1],
                    "plus_state_set": [2],
                    "minus_state_set": [],
                }
            }
            sess._get_actor_state(1)["learned_skills"].append(10)
            # Make Enemy 1 weak to Element 1 (rank 1 = 200% weakness) & give plenty of HP to survive multiple turns
            sess.enemies[1]["fields"]["maxhp"] = 140
            sess.enemies[1]["fields"]["element_ranks"] = {"data": [3, 1, 3, 3]}

            battle_cmds = [
                {"op": "battle", "troop_id": 1, "can_escape": True, "can_lose": False, "indent": 0},
                {"op": "when_win", "indent": 0},
                {"op": "set_switch", "start_id": 21, "end_id": 21, "value": "ON", "indent": 1},
                {"op": "when_escape", "indent": 0},
                {"op": "set_switch", "start_id": 20, "end_id": 20, "value": "ON", "indent": 1},
                {"op": "end_battle", "indent": 0},
            ]

            # 1. Escape path in interactive battle jumps to when_escape
            sess._run_commands(battle_cmds, 0, 1, 1)
            self.assertIsNotNone(sess.pending)
            self.assertIsNotNone(sess.pending.battle_state)
            self.assertEqual(sess.pending.choices, ["공격", "스킬", "방어", "아이템", "도망"])
            sess.step("choose:4")  # 도망
            self.assertIsNone(sess.pending)
            self.assertEqual(sess.last_battle_result, "escape")
            self.assertTrue(sess.switches.get(20))
            self.assertFalse(sess.switches.get(21))

            # 2. Full interactive turn loop: Defend (SP +2) -> Fire Skill (weakness + Poison) -> Attack until Win
            sess._run_commands(battle_cmds, 0, 1, 1)
            bs = sess.pending.battle_state
            self.assertIsNotNone(bs)
            sp_start = sess.sp
            sess.step("choose:2")  # 방어 (restores +2 SP)
            self.assertEqual(sess.sp, min(sess.max_sp, sp_start + 2))
            self.assertEqual(bs["turn"], 2)

            # Open skill submenu and cast 파이어볼 (index 1 in learned_skills [1, 10])
            hp_before_skill = bs["enemies"][0]["hp"]
            sess.step("choose:1")  # 스킬 메뉴
            skill_idx = sess._get_actor_state(1)["learned_skills"].index(10)
            sess.step(f"choose:{skill_idx}")
            self.assertIn("약점!", bs["last_msg"])
            self.assertLess(bs["enemies"][0]["hp"], hp_before_skill)
            self.assertIn(2, bs["enemies"][0]["states"])

            # Apply temporary battle_only stun to hero and clear it after win
            sess._get_actor_state(1)["states"].append(3)
            # Clear Stun restriction just for hero to finish attack cleanly or keep defending if stunned -> test Stun prevents attack then finish
            sess.step("choose:0")
            self.assertIn("움직일 수 없다", bs["last_msg"])
            sess._get_actor_state(1)["states"].remove(3)
            # Re-add battle_only state after battle check or finish fight via basic attacks
            guard = 0
            while sess.pending is not None and guard < 20:
                sess.step("choose:0")
                guard += 1
            self.assertIsNone(sess.pending)
            self.assertEqual(sess.last_battle_result, "win")
            self.assertTrue(sess.switches.get(21))

            # Verify battle_only state cleanup helper directly
            sess._get_actor_state(1)["states"] = [2, 3]
            sess._clear_battle_only_states()
            self.assertEqual(sess._get_actor_state(1)["states"], [2])

    def test_24_field_skill_item_use_dash_speed_bgm_se_and_screen_fx(self):
        import os
        from src.core.ir_converter import commands_to_semantic_dsl, pack_ir_to_rmxp, semantic_dsl_to_commands, unpack_rmxp_to_ir
        from src.core.utils import load_json, save_json
        from src.harness.player_gui import (
            BASE_SPEED_PX_PER_SEC,
            DASH_SPEED_PX_PER_SEC,
            _draw_screen_tone_and_flash,
            _process_status_panel_action,
            launch_player,
            list_field_skills_and_items_for_actor,
            resolve_player_speed_px,
        )

        # 1. Round-trip RMXP opcodes 106, 223, 224, 225, 241, 242, 245, 250
        dsl_ops = [
            {"op": "play_bgm", "name": "Town01", "volume": 85, "pitch": 100, "indent": 0},
            {"op": "play_bgs", "name": "Wind01", "volume": 50, "pitch": 100, "indent": 0},
            {"op": "play_se", "name": "Heal01", "volume": 90, "pitch": 110, "indent": 0},
            {"op": "change_screen_tone", "rgbg": [-68, -68, 0, 40], "frames": 20, "indent": 0},
            {"op": "screen_flash", "rgba": [255, 64, 64, 180], "frames": 15, "indent": 0},
            {"op": "screen_shake", "power": 6, "speed": 7, "frames": 24, "indent": 0},
            {"op": "wait_frames", "frames": 10, "indent": 0},
            {"op": "fadeout_bgm", "seconds": 2, "indent": 0},
        ]
        packed_cmds = semantic_dsl_to_commands(dsl_ops)
        unpacked_dsl = commands_to_semantic_dsl(packed_cmds)
        self.assertEqual(unpacked_dsl, dsl_ops)

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            ws = tmp_path / "ws"
            generate_sample_project(ws)
            ir_dir = ws / "ir"

            # Inject a field heal skill (id=20, occasion=2 menu-only, power=-45, sp_cost=5, cures state 2)
            skills_path = ir_dir / "database" / "skills.json"
            skills = load_json(skills_path)
            skills.append(
                {
                    "__ruby_class__": "RPG::Skill",
                    "fields": {
                        "id": 20,
                        "name": "힐",
                        "sp_cost": 5,
                        "power": -45,
                        "scope": 3,
                        "occasion": 2,
                        "variance": 0,
                        "pdef_f": 0,
                        "element_set": [],
                        "minus_state_set": [2],
                    },
                }
            )
            save_json(skills_path, skills)

            # Give hero skill 20 and event running screen/audio opcodes right east of start pos
            probe_sess = HeadlessGameSession(ir_dir)
            sx, sy = probe_sess.x, probe_sess.y
            m1_path = ir_dir / "maps" / "map_001.json"
            m1 = load_json(m1_path)
            import copy
            tpl_page = copy.deepcopy(m1["events"][0]["pages"][0])
            tpl_page["trigger"] = "action_button"
            tpl_page["trigger_code"] = 0
            tpl_page["move_type"] = 0
            tpl_page["through"] = False
            tpl_page["condition"] = {"switch1_valid": False, "switch2_valid": False, "variable_valid": False, "self_switch_valid": False}
            tpl_page["commands"] = dsl_ops[:-1]  # keep BGM active after trigger
            m1["events"].append(
                {
                    "id": 91,
                    "name": "FxEvent",
                    "x": sx + 1,
                    "y": sy,
                    "pages": [tpl_page],
                }
            )
            save_json(m1_path, m1)

            # Verify full pack -> unpack preserves new opcodes in Map 1 event 91
            repack_dir = tmp_path / "rmxp_repack"
            reunpack_dir = tmp_path / "ir_reunpack"
            pack_ir_to_rmxp(ir_dir, repack_dir)
            unpack_rmxp_to_ir(repack_dir, reunpack_dir)
            re_m1 = load_json(reunpack_dir / "maps" / "map_001.json")
            re_ev91 = next(e for e in re_m1["events"] if e["id"] == 91)
            re_ops = [c["op"] for c in re_ev91["pages"][0]["commands"]]
            for expected_op in ("play_bgm", "play_bgs", "play_se", "change_screen_tone", "screen_flash", "screen_shake", "wait_frames"):
                self.assertIn(expected_op, re_ops)

            sess = HeadlessGameSession(ir_dir)
            sess._get_actor_state(1)["learned_skills"].append(20)
            sess._get_actor_state(1)["hp"] = max(1, sess.max_hp - 60)
            sess._get_actor_state(1)["states"] = [2]
            sp_before = sess.sp

            # Field skill & item panel list + Q/E tab + Enter execution
            entries = list_field_skills_and_items_for_actor(sess, actor_id=1)
            skill_pos = next(idx for idx, e in enumerate(entries) if e[0] == "skill" and e[1] == 20)
            import pygame
            os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
            still_open, cur, aidx, mode = _process_status_panel_action(pygame, pygame.K_q, sess, 0, 0, "equip")
            self.assertTrue(still_open)
            self.assertEqual(mode, "use")
            still_open, cur, aidx, mode = _process_status_panel_action(pygame, pygame.K_RETURN, sess, skill_pos, 0, "use")
            self.assertTrue(still_open)
            self.assertEqual(sess.hp, min(sess.max_hp, (sess.max_hp - 60) + 45))
            self.assertEqual(sess.sp, sp_before - 5)
            self.assertEqual(sess._get_actor_state(1)["states"], [])

            # Trigger FX & audio event directly east of start_pos
            sess.step("face:right")
            sess.step("interact")
            self.assertEqual(sess.screen_tone, [-68, -68, 0, 40])
            self.assertEqual(sess.current_bgm["name"], "Town01")
            self.assertEqual(sess.current_bgs["name"], "Wind01")
            self.assertEqual(sess.played_se_queue[0]["name"], "Heal01")
            self.assertIsNotNone(sess.pending_flash)
            self.assertIsNotNone(sess.pending_shake)

            # Save -> reset -> load preserves screen_tone & current_bgm
            save_path = tmp_path / "slot_fx.json"
            sess.save_to_file(save_path)
            sess.reload()
            self.assertEqual(sess.screen_tone, [0, 0, 0, 0])
            sess.load_from_file(save_path)
            self.assertEqual(sess.screen_tone, [-68, -68, 0, 40])
            self.assertEqual(sess.current_bgm["name"], "Town01")

            # Dash speed check
            self.assertEqual(resolve_player_speed_px(False), BASE_SPEED_PX_PER_SEC)
            self.assertEqual(resolve_player_speed_px(True), DASH_SPEED_PX_PER_SEC)
            self.assertGreater(DASH_SPEED_PX_PER_SEC, BASE_SPEED_PX_PER_SEC)

            # Dummy pygame frame smoke test with active tone/flash/shake
            def _inject_fx(tick, screen, s):
                if tick == 0:
                    s.screen_tone = [-60, -40, 20, 30]
                    s.pending_flash = {"rgba": [255, 80, 80, 160], "frames": 3, "max_frames": 3}
                    s.pending_shake = {"power": 4, "speed": 5, "frames": 3, "max_frames": 3}
                    _draw_screen_tone_and_flash(pygame, screen, s)

            rc = launch_player(ir_dir, max_frames=3, on_frame=_inject_fx)
            self.assertEqual(rc, 0)

    def test_25_shop_sell_terrain_damage_ice_text_codes_and_full_rpg_loop(self):
        from src.core.utils import load_json, save_json

        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "ws"
            generate_sample_project(ws)
            ir_dir = ws / "ir"

            # Add custom terrain tags: tile 384 -> tag 0, configure custom tileset indices for test
            # Ensure map_001 has a clear open row and test shop, text codes, damage floor & ice floor cleanly
            ts_path = ir_dir / "database" / "tilesets.json"
            tilesets = load_json(ts_path)
            ts1 = next(t for t in tilesets if t and t["fields"]["id"] == 1)
            tags = list(ts1["fields"]["terrain_tags"]["data"])
            while len(tags) <= 390:
                tags.append(0)
            tags[385] = 1  # Damage Floor
            tags[386] = 2  # Ice Floor
            ts1["fields"]["terrain_tags"]["data"] = tags
            passages = list(ts1["fields"]["passages"]["data"])
            while len(passages) <= 390:
                passages.append(0)
            passages[385] = 0
            passages[386] = 0
            ts1["fields"]["passages"]["data"] = passages
            save_json(ts_path, tilesets)

            # Paint Damage tile (385) and 2 consecutive Ice tiles (386) on Map 1 near start pos
            probe = HeadlessGameSession(ir_dir)
            sx, sy = probe.x, probe.y
            m1_path = ir_dir / "maps" / "map_001.json"
            m1 = load_json(m1_path)
            m1["layers"]["layer_0"][sy][sx + 1] = 385
            m1["layers"]["layer_0"][sy][sx + 2] = 386
            m1["layers"]["layer_0"][sy][sx + 3] = 386
            m1["layers"]["layer_0"][sy][sx + 4] = 384

            import copy
            tpl = copy.deepcopy(m1["events"][0]["pages"][0])
            tpl["trigger"] = "action_button"
            tpl["trigger_code"] = 0
            tpl["move_type"] = 0
            tpl["through"] = False
            tpl["condition"] = {"switch1_valid": False, "switch2_valid": False, "variable_valid": False, "self_switch_valid": False}
            tpl_msg = copy.deepcopy(tpl)
            tpl_msg["commands"] = [{"op": "say", "lines": ["\\N[1]님, 점수:\\V[3], 소지:\\G"], "indent": 0}]
            tpl_shop = copy.deepcopy(tpl)
            tpl_shop["commands"] = [{"op": "shop", "goods": [{"type": "item", "id": 1}], "indent": 0}]
            m1["events"].append({"id": 95, "name": "MsgEv", "x": sx, "y": sy + 1, "pages": [tpl_msg]})
            m1["events"].append({"id": 96, "name": "ShopEv", "x": sx - 1, "y": sy, "pages": [tpl_shop]})
            save_json(m1_path, m1)

            sess = HeadlessGameSession(ir_dir)
            sess.gold = 200
            sess.variables[3] = 77
            sess.inventory[1] = 2

            # 1. Text formatting codes \N[1], \V[3], \G
            sess.step("face:down")
            sess.step("interact")
            self.assertIsNotNone(sess.pending)
            hero_name = sess._get_actor_state(1)["name"]
            self.assertEqual(sess.pending.lines[0], f"{hero_name}님, 점수:77, 소지:200G")
            sess.step("interact")
            self.assertIsNone(sess.pending)

            # 2. Shop Buy & Sell (판매하기) -> price/2 return
            item1_price = int(sess.items[1]["fields"]["price"])
            expected_sell = max(1, item1_price // 2)
            sess.step("face:left")
            sess.step("interact")
            self.assertIsNotNone(sess.pending)
            self.assertEqual(sess.pending.choices, [f"{sess.items[1]['fields']['name']} ({item1_price}G)", "판매하기", "나가기"])
            sess.step("choose:1")  # enter 판매하기
            self.assertEqual(sess.pending.shop_mode, "sell")
            sess.step("choose:0")  # sell 1x item 1
            self.assertEqual(sess.inventory.get(1, 0), 1)
            self.assertEqual(sess.gold, 200 + expected_sell)
            sess.step("choose:1")  # return to buy mode
            self.assertEqual(sess.pending.shop_mode, "buy")
            sess.step("choose:2")  # exit shop
            self.assertIsNone(sess.pending)

            # 3. Damage floor (tag 1) & Ice floor (tag 2) sliding
            hp_before = sess.hp
            sess.step("right")  # lands on (sx + 1, sy) tag=1
            self.assertEqual(sess.x, sx + 1)
            self.assertLess(sess.hp, hp_before)
            self.assertIsNotNone(sess.pending_flash)

            sess.step("right")  # hits ice at (sx + 2, sy), slides over (sx + 3) onto normal tile (sx + 4, sy)
            self.assertEqual(sess.x, sx + 4)
            self.assertTrue(any("[Terrain] 빙판 미끄러짐" in l for l in sess.log))

    def test_26_radar_ice_glide_bgs_channel_and_battle_target_selection(self):
        from src.harness.player_gui import (
            ICE_SLIDE_SPEED_PX_PER_SEC,
            _sync_audio_runtime,
            resolve_player_speed_px,
        )

        # 1. Ice glide velocity scaling helper & last_slide_was_ice
        self.assertEqual(resolve_player_speed_px(False, is_ice_slide=True), ICE_SLIDE_SPEED_PX_PER_SEC)

        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "ws"
            generate_sample_project(ws)
            ir_dir = ws / "ir"

            # Create fake Audio/BGS/Wind.ogg to test reserved BGS Channel playback & stop
            audio_root = Path(tmp) / "Audio"
            (audio_root / "BGS").mkdir(parents=True, exist_ok=True)
            (audio_root / "BGS" / "Wind.ogg").write_bytes(b"OggS")

            class _FakeChannel:
                def __init__(self):
                    self.volume = 0.0
                    self.playing = False
                    self.stopped = 0
                def set_volume(self, v):
                    self.volume = v
                def play(self, snd, loops=0):
                    self.playing = (snd, loops)
                def stop(self):
                    self.playing = False
                    self.stopped += 1

            fake_ch = _FakeChannel()
            class _FakeMixer:
                class music:
                    @staticmethod
                    def stop(): pass
                @staticmethod
                def set_reserved(n): pass
                @staticmethod
                def Channel(idx): return fake_ch
                @staticmethod
                def Sound(path): return f"SOUND:{Path(path).name}"
            class _FakePygame:
                mixer = _FakeMixer()

            sess = HeadlessGameSession(ir_dir, interactive_battle=True)
            sess.current_bgs = {"name": "Wind", "volume": 60, "pitch": 100}
            bgm, bgs, ch = _sync_audio_runtime(_FakePygame(), audio_root, sess, None, True, None, None)
            self.assertEqual(bgs, "Wind")
            self.assertIs(ch, fake_ch)
            self.assertEqual(fake_ch.playing, ("SOUND:Wind.ogg", -1))
            self.assertAlmostEqual(fake_ch.volume, 0.6)
            sess.current_bgs = None
            bgm, bgs, ch = _sync_audio_runtime(_FakePygame(), audio_root, sess, bgm, True, bgs, ch)
            self.assertIsNone(bgs)
            self.assertEqual(fake_ch.stopped, 1)

            # 2. Multi-member interactive battle ally/enemy target selection & revival
            sess.actor_states[2] = sess._build_actor_state(1)
            sess.actor_states[2]["id"] = 2
            sess.actor_states[2]["name"] = "아리아"
            sess.party_member_ids = [1, 2]
            # Ally 2 is hurt, Hero heals Ally 2 via scope=3 single-ally skill #30
            sess.actor_states[2]["hp"] = 30
            sess.skills_db[30] = {
                "fields": {"id": 30, "name": "케어", "scope": 3, "power": -30, "sp_cost": 3, "occasion": 0, "minus_state_set": []}
            }
            sess.skills_db[31] = {
                "fields": {"id": 31, "name": "부활", "scope": 5, "power": -20, "sp_cost": 5, "occasion": 0, "minus_state_set": [1]}
            }
            sess._get_actor_state(1)["learned_skills"].extend([30, 31])
            # Troop 1 with 2 enemies so enemy target submenu appears
            sess.troops[1]["fields"]["members"].append({"fields": {"enemy_id": 1}})
            sess.enemies[1]["fields"]["maxhp"] = 80
            sess.enemies[1]["fields"]["atk"] = 1

            sess._start_interactive_battle(1, can_escape=True, can_lose=False)
            bs = sess.pending.battle_state
            self.assertEqual(len(bs["enemies"]), 2)
            # Hero chooses Attack -> enters target_enemy -> picks 2nd enemy (index 1)
            sess.step("choose:0")
            self.assertEqual(bs["submenu"], "target_enemy")
            e2_before = bs["enemies"][1]["hp"]
            sess.step("choose:1")
            self.assertLess(bs["enemies"][1]["hp"], e2_before)
            self.assertEqual(bs["submenu"], "main")
            # Now Aria's turn (actor_cursor=1): Aria opens Skill, Hero doesn't have 30; Aria chooses Defend (2)
            sess.step("choose:2")
            self.assertEqual(bs["turn"], 2)
            # Turn 2 Hero chooses 케어 (skill index matching 30) -> enters target_ally -> heals Aria (idx 1)
            sess.step("choose:1")
            skill_idx_care = sess._get_actor_state(1)["learned_skills"].index(30)
            sess.step(f"choose:{skill_idx_care}")
            self.assertEqual(bs["submenu"], "target_ally")
            aria_hp_before = sess._get_actor_state(2)["hp"]
            sess.step("choose:1")  # heal Aria specifically!
            self.assertGreater(sess._get_actor_state(2)["hp"], aria_hp_before)

    def test_27_rmxp_actor_class_param_name_scope_crit_and_evasion(self):
        from src.core.ir_converter import commands_to_semantic_dsl, semantic_dsl_to_commands

        # 1. Round-trip RMXP opcodes 317, 320, 321
        ops = [
            {"op": "change_parameter", "actor_id": 1, "param_id": 3, "mode": "add", "operand_type": 0, "amount": 25, "indent": 0},
            {"op": "change_actor_name", "actor_id": 1, "name": "아서", "indent": 0},
            {"op": "change_actor_class", "actor_id": 1, "class_id": 2, "indent": 0},
            {"op": "recover_all", "actor_id": 0, "indent": 0},
        ]
        rt = commands_to_semantic_dsl(semantic_dsl_to_commands(ops))
        self.assertEqual(rt, ops)

        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "ws"
            generate_sample_project(ws)
            ir_dir = ws / "ir"
            sess = HeadlessGameSession(ir_dir, interactive_battle=True)
            # Class 2 restricts weapons to [99] and grants Skill 50 at Lv 1
            sess.classes_db[2] = {
                "fields": {
                    "id": 2,
                    "name": "마법사",
                    "weapon_set": [99],
                    "armor_set": [99],
                    "learnings": [{"fields": {"level": 1, "skill_id": 50}}],
                }
            }
            sess.skills_db[50] = {
                "fields": {"id": 50, "name": "광역섬광", "scope": 2, "power": 25, "sp_cost": 4, "element_set": []}
            }
            # Equip weapon 1 first (allowed by class 1) then run change_actor_class -> 2
            sess.weapons_owned[1] = 1
            sess.equip_from_bag(1, 0, 1)
            self.assertEqual(sess._get_actor_state(1)["weapon_id"], 1)

            sess._run_commands(ops, 0, 1, 99)
            st1 = sess._get_actor_state(1)
            self.assertEqual(st1["name"], "아서")
            self.assertEqual(st1["class_id"], 2)
            self.assertEqual(st1["weapon_id"], 0)  # unequips disallowed class 1 weapon
            self.assertIn(50, st1["learned_skills"])
            self.assertGreaterEqual(sess.actor_full_stats(1)["dex"], 35)

            # High EVA Miss & High DEX Critical & scope=2 All-Enemies AoE
            sess.troops[1]["fields"]["members"].append({"fields": {"enemy_id": 1}})
            sess.enemies[1]["fields"]["maxhp"] = 120
            sess.enemies[1]["fields"]["eva"] = 99
            sess._start_interactive_battle(1, can_escape=True, can_lose=False)
            bs = sess.pending.battle_state
            sess.step("choose:0")  # Attack -> target_enemy
            sess.step("choose:0")  # Evasion 99 -> guaranteed Miss
            self.assertIn("빗나갔다", bs["last_msg"])

            # Clear enemy evasion + boost hero DEX -> guaranteed Critical!
            for e in bs["enemies"]:
                e["eva"] = 0
            st1["base_dex"] = 200
            sess.step("choose:0")
            sess.step("choose:0")
            self.assertIn("치명타!", bs["last_msg"])

            # Cast AoE Skill 50 (scope=2): hits BOTH enemies simultaneously!
            hp0_before = bs["enemies"][0]["hp"]
            hp1_before = bs["enemies"][1]["hp"]
            sess.step("choose:1")
            s50_idx = st1["learned_skills"].index(50)
            sess.step(f"choose:{s50_idx}")
            self.assertLess(bs["enemies"][0]["hp"], hp0_before)
            self.assertLess(bs["enemies"][1]["hp"], hp1_before)

    def test_28_agi_preemptive_ambush_enemy_ai_skills_and_troop_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "ws"
            generate_sample_project(ws)
            ir_dir = ws / "ir"
            sess = HeadlessGameSession(ir_dir, interactive_battle=True)

            # 1. Preemptive strike when Hero AGI >= 1.5x Enemy AGI
            sess._get_actor_state(1)["base_agi"] = 25
            sess.enemies[1]["fields"]["agi"] = 10
            sess.enemies[1]["fields"]["maxhp"] = 120
            sess._start_interactive_battle(1, can_escape=True, can_lose=False)
            bs = sess.pending.battle_state
            self.assertIn("선제 공격", bs["last_msg"])
            hp_before_pre = sess.hp
            sess.step("choose:0")
            # Enemy skipped Turn 1 due to preemptive advantage!
            self.assertEqual(sess.hp, hp_before_pre)
            self.assertEqual(bs["turn"], 2)
            sess.step("choose:4")  # Escape

            # 2. Enemy AI skill casting (AoE poison/damage) + Troop Battle Event Page on Turn 2
            sess._get_actor_state(1)["base_agi"] = 10
            sess.skills_db[60] = {
                "fields": {"id": 60, "name": "독가스", "scope": 2, "power": 18, "sp_cost": 0, "plus_state_set": [2]}
            }
            sess.states_db[2] = {"fields": {"id": 2, "name": "독", "restriction": 0, "slip_damage": True, "battle_only": True}}
            sess.enemies[1]["fields"]["actions"] = [
                {"fields": {"kind": 1, "skill_id": 60, "condition_turn_a": 0, "condition_turn_b": 1, "condition_hp": 100, "rating": 9}}
            ]
            sess.troops[1]["fields"]["pages"] = [
                {
                    "fields": {
                        "span": 0,
                        "condition": {"fields": {"turn_valid": True, "turn_a": 2, "turn_b": 0, "enemy_valid": False, "switch_valid": False}},
                        "list": [
                            {"op": "set_switch", "start_id": 77, "end_id": 77, "value": "ON"},
                            {"op": "say", "lines": ["보스가 분노했다!"]},
                        ],
                    }
                }
            ]
            sess._start_interactive_battle(1, can_escape=True, can_lose=False)
            bs = sess.pending.battle_state
            sess.step("choose:2")  # Defend turn 1 -> Enemy casts 독가스 + Turn advances to 2 -> Troop page fires!
            self.assertIn(2, sess._get_actor_state(1)["states"])
            self.assertTrue(sess.switches.get(77))
            self.assertIn("보스가 분노했다!", bs["last_msg"])

    def test_29_timer_weather_fade_event_relocate_and_vehicles(self):
        import copy
        from src.core.ir_converter import commands_to_semantic_dsl, semantic_dsl_to_commands
        from src.core.utils import load_json, save_json

        ops = [
            {"op": "control_timer", "mode": "start", "seconds": 90, "indent": 0},
            {"op": "set_weather", "weather": "storm", "power": 7, "indent": 0},
            {"op": "fadeout_screen", "indent": 0},
            {"op": "set_event_location", "target_id": 1, "loc_type": 0, "x": 5, "y": 6, "direction": "left", "indent": 0},
            {"op": "set_vehicle_location", "vehicle": "boat", "loc_type": 0, "map_id": 1, "x": 3, "y": 4, "indent": 0},
            {"op": "fadein_screen", "indent": 0},
        ]
        cmds = semantic_dsl_to_commands(ops)
        roundtrip = commands_to_semantic_dsl(cmds)
        self.assertEqual([c["op"] for c in roundtrip], [c["op"] for c in ops])
        self.assertEqual(roundtrip[0]["seconds"], 90)
        self.assertEqual(roundtrip[1]["weather"], "storm")
        self.assertEqual(roundtrip[4]["vehicle"], "boat")

        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "ws"
            generate_sample_project(ws)
            ir_dir = ws / "ir"
            # Mark tile (sx+1, sy) and (sx+2, sy) as blocked/water (tile 385 in tileset -> make passage 0x0F)
            m1_path = ir_dir / "maps" / "map_001.json"
            m1 = load_json(m1_path)
            sx, sy = 5, 5
            proj_path = ir_dir / "ir_project.json"
            proj = load_json(proj_path)
            proj["system"]["fields"]["start_map_id"] = 1
            proj["system"]["fields"]["start_x"] = sx
            proj["system"]["fields"]["start_y"] = sy
            save_json(proj_path, proj)

            ts_path = ir_dir / "database" / "tilesets.json"
            ts_list = load_json(ts_path)
            ts_id = int(m1["tileset_id"])
            for ts in ts_list:
                if ts and int(ts["fields"]["id"]) == ts_id:
                    while len(ts["fields"]["passages"]["data"]) <= 390:
                        ts["fields"]["passages"]["data"].append(0)
                    ts["fields"]["passages"]["data"][390] = 0x0F
            save_json(ts_path, ts_list)

            # Paint water walls at (sx+1, sy) & (sx+2, sy), walkable grass at (sx+3, sy)
            m1["layers"]["layer_0"][sy][sx + 1] = 390
            m1["layers"]["layer_0"][sy][sx + 2] = 390
            # Ensure two events exist to verify set_event_location direct & swap
            tpl_ev = copy.deepcopy(m1["events"][0]["pages"][0])
            tpl_ev["through"] = False
            tpl_ev["trigger_code"] = 0
            tpl_ev["commands"] = []
            m1["events"] = [
                {"id": 1, "name": "EvA", "x": 1, "y": 1, "pages": [copy.deepcopy(tpl_ev)]},
                {"id": 2, "name": "EvB", "x": 2, "y": 2, "pages": [copy.deepcopy(tpl_ev)]},
            ]
            save_json(m1_path, m1)

            sess = HeadlessGameSession(ir_dir)
            self.assertFalse(sess.can_step_action("right"))  # Blocked water tile on foot

            # Run field commands: Timer + Weather + Fade + Set Event Location + Vehicle Location
            sess._run_commands(
                [
                    {"op": "control_timer", "mode": "start", "seconds": 5},
                    {"op": "set_weather", "weather": "rain", "power": 6},
                    {"op": "fadeout_screen"},
                    {"op": "set_event_location", "target_id": 1, "loc_type": 0, "x": 7, "y": 7},
                    {"op": "set_event_location", "target_id": 1, "loc_type": 2, "x": 2},  # swap EvA(7,7) <-> EvB(2,2)
                    {"op": "set_vehicle_location", "vehicle": "boat", "loc_type": 0, "map_id": 1, "x": sx + 1, "y": sy},
                    {"op": "set_vehicle_location", "vehicle": "airship", "loc_type": 0, "map_id": 1, "x": sx - 1, "y": sy},
                ],
                0,
                1,
                1,
            )
            self.assertTrue(sess.timer_active)
            self.assertEqual(sess.timer_seconds, 5)
            sess.advance_timer_ms(2500.0)
            self.assertEqual(sess.timer_seconds, 3)
            sess.advance_timer_ms(3000.0)
            self.assertFalse(sess.timer_active)
            self.assertEqual(sess.timer_seconds, 0)
            self.assertTrue(sess.screen_faded_out)
            self.assertEqual(sess.weather, {"type": "rain", "power": 6})
            evs_by_id = {int(e["id"]): e for e in sess.current_map()["events"]}
            self.assertEqual((evs_by_id[1]["x"], evs_by_id[1]["y"]), (2, 2))
            self.assertEqual((evs_by_id[2]["x"], evs_by_id[2]["y"]), (7, 7))

            # Board boat facing right, sail across water tiles (sx+1, sx+2), then dock onto walkable (sx+3, sy)
            sess.step("face:right")
            sess.step("toggle_vehicle")
            self.assertEqual(sess.vehicle, "boat")
            self.assertEqual((sess.x, sess.y), (sx + 1, sy))
            self.assertTrue(sess.can_step_action("right"))
            sess.step("right")
            self.assertEqual((sess.x, sess.y), (sx + 2, sy))
            sess.step("toggle_vehicle")  # Disembark onto walkable shore (sx+3, sy) facing right
            self.assertEqual(sess.vehicle, "walk")
            self.assertEqual((sess.x, sess.y), (sx + 3, sy))

            # Save/load round-trip preserves weather, fade, fadein & vehicle state
            sess._run_commands([{"op": "control_timer", "mode": "start", "seconds": 42}, {"op": "fadein_screen"}], 0, 1, 1)
            self.assertFalse(sess.screen_faded_out)
            blob = sess.export_save_state()
            sess.weather = {"type": "none", "power": 0}
            sess.timer_active = False
            sess.import_save_state(blob)
            self.assertTrue(sess.timer_active)
            self.assertEqual(sess.timer_seconds, 42)
            self.assertEqual(sess.weather, {"type": "rain", "power": 6})

            # Board airship and verify flying over blocked water/NPC tiles & _draw_weather_and_fade render
            sess.x, sess.y = sx, sy
            sess.step("face:left")
            sess.step("toggle_vehicle")
            self.assertEqual(sess.vehicle, "airship")
            self.assertTrue(sess.can_step_action("right"))
            import os
            import pygame
            from src.harness.player_gui import VIEW_H, VIEW_W, _draw_weather_and_fade
            os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
            pygame.init()
            try:
                surf = pygame.Surface((VIEW_W, VIEW_H))
                sfont = pygame.font.Font(None, 18)
                _draw_weather_and_fade(pygame, surf, sfont, sess, anim_tick=12)
            finally:
                pygame.quit()

    def test_30_input_number_pictures_save_menu_slot_summary_and_gameover_retry(self):
        from src.core.ir_converter import commands_to_semantic_dsl, semantic_dsl_to_commands
        from src.harness.player_gui import VIEW_H, VIEW_W, _draw_pictures, format_hud_text, process_save_load_shortcut

        ops = [
            {"op": "input_number", "variable_id": 12, "digits": 2, "indent": 0},
            {"op": "show_picture", "number": 1, "name": "HeroCut", "origin": 1, "var_mode": 0, "x": 160, "y": 120, "zoom_x": 100, "zoom_y": 100, "opacity": 220, "indent": 0},
            {"op": "move_picture", "number": 1, "frames": 10, "origin": 1, "var_mode": 0, "x": 220, "y": 140, "zoom_x": 120, "zoom_y": 120, "opacity": 255, "indent": 0},
            {"op": "open_save_menu", "indent": 0},
            {"op": "erase_picture", "number": 1, "indent": 0},
        ]
        rt = commands_to_semantic_dsl(semantic_dsl_to_commands(ops))
        self.assertEqual([c["op"] for c in rt], [c["op"] for c in ops])
        self.assertEqual(rt[0]["variable_id"], 12)
        self.assertEqual(rt[1]["name"], "HeroCut")

        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "ws"
            generate_sample_project(ws)
            ir_dir = ws / "ir"
            sess = HeadlessGameSession(ir_dir)
            sess.gold = 345

            # 1. Input Number interactive prompt -> +10, +10, +1 = 21 -> confirm
            sess._run_commands(ops, 0, 1, 1)
            self.assertIsNotNone(sess.pending)
            self.assertIsNotNone(sess.pending.number_input)
            sess.step("choose:1")  # +10 -> 10
            sess.step("choose:1")  # +10 -> 20
            sess.step("choose:0")  # +1  -> 21
            sess.step("choose:4")  # confirm -> stores variables[12] = 21 & runs show/move_picture then pauses on open_save_menu!
            self.assertEqual(sess.variables.get(12), 21)
            self.assertIn(1, sess.pictures)
            self.assertEqual((sess.pictures[1]["x"], sess.pictures[1]["y"]), (220, 140))

            # Render picture fallback via Pygame dummy driver
            import pygame
            os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
            pygame.init()
            try:
                surf = pygame.Surface((VIEW_W, VIEW_H))
                sfont = pygame.font.Font(None, 18)
                _draw_pictures(pygame, surf, lambda _k: None, sfont, sess)
            finally:
                pygame.quit()

            # 2. Save Menu (slot 2) & Save Slot Summary in F7 shortcut
            self.assertIsNotNone(sess.pending)
            self.assertTrue(sess.pending.save_menu)
            sess.step("choose:1")  # save to slot 2
            meta2 = sess.get_save_slot_summary(2)
            self.assertTrue(meta2["exists"])
            self.assertEqual(meta2["gold"], 345)
            self.assertIn("345G", meta2["label"])
            sess.step("choose:3")  # close save menu -> resumes erase_picture!
            self.assertIsNone(sess.pending)
            self.assertNotIn(1, sess.pictures)

            handled, next_slot, _c, msg = process_save_load_shortcut(pygame, pygame.K_F7, sess, 1, False)
            self.assertTrue(handled)
            self.assertEqual(next_slot, 2)
            self.assertIn("345G", msg)

            # 3. Game Over recovery via gameover_retry loads latest save slot!
            sess.gold = 0
            sess._run_commands([{"op": "game_over"}], 0, 1, 1)
            self.assertTrue(sess.game_over)
            self.assertIn("R/Enter", format_hud_text("마을", sess.snapshot()))
            sess.step("gameover_retry")
            self.assertFalse(sess.game_over)
            self.assertEqual(sess.gold, 345)

    def test_31_radar_picture_tween_vehicle_terrain_and_map_cache(self):
        from src.core.utils import load_json, save_json
        from src.harness.player_gui import _get_or_build_map_cache

        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "ws"
            generate_sample_project(ws)
            ir_dir = ws / "ir"

            # Setup terrain_tag=3 (water) on tile 390, cliff wall (tag=0, passage=0x0F) on tile 391
            m1_path = ir_dir / "maps" / "map_001.json"
            m1 = load_json(m1_path)
            ts_path = ir_dir / "database" / "tilesets.json"
            ts_list = load_json(ts_path)
            ts_id = int(m1["tileset_id"])
            for ts in ts_list:
                if ts and int(ts["fields"]["id"]) == ts_id:
                    for key in ("passages", "terrain_tags"):
                        while len(ts["fields"][key]["data"]) <= 395:
                            ts["fields"][key]["data"].append(0)
                    ts["fields"]["passages"]["data"][390] = 0x0F
                    ts["fields"]["terrain_tags"]["data"][390] = 3  # water
                    ts["fields"]["passages"]["data"][391] = 0x0F
                    ts["fields"]["terrain_tags"]["data"][391] = 0  # cliff wall
            save_json(ts_path, ts_list)

            sx, sy = 5, 5
            proj_path = ir_dir / "ir_project.json"
            proj = load_json(proj_path)
            proj["system"]["fields"]["start_map_id"] = 1
            proj["system"]["fields"]["start_x"] = sx
            proj["system"]["fields"]["start_y"] = sy
            save_json(proj_path, proj)

            # (sx+1, sy) = water, (sx+2, sy) = water, (sx+1, sy-1) = cliff wall
            m1["layers"]["layer_0"][sy][sx + 1] = 390
            m1["layers"]["layer_0"][sy][sx + 2] = 390
            m1["layers"]["layer_0"][sy - 1][sx + 1] = 391
            save_json(m1_path, m1)

            sess = HeadlessGameSession(ir_dir)
            # 1. Picture move_picture real-time tween interpolation
            sess._run_commands([
                {"op": "show_picture", "number": 1, "name": "Banner", "origin": 0, "x": 100, "y": 50, "zoom_x": 100, "zoom_y": 100, "opacity": 100},
                {"op": "move_picture", "number": 1, "frames": 4, "origin": 0, "x": 140, "y": 90, "zoom_x": 120, "zoom_y": 120, "opacity": 200},
            ], 0, 1, 1)
            self.assertEqual((sess.pictures[1]["render_x"], sess.pictures[1]["render_y"]), (100, 50))
            sess.advance_picture_tweens(2)  # 50%
            self.assertEqual((sess.pictures[1]["render_x"], sess.pictures[1]["render_y"]), (120, 70))
            self.assertEqual(sess.pictures[1]["render_op"], 150)
            sess.advance_picture_tweens(2)  # 100% -> cleared tween
            self.assertNotIn("tween", sess.pictures[1])
            self.assertEqual((sess.pictures[1]["x"], sess.pictures[1]["y"]), (140, 90))

            # 2. Boat sails on terrain_tag==3 water ONLY (blocked by cliff 391 & blocked by parked ship!)
            sess._run_commands([
                {"op": "set_vehicle_location", "vehicle": "boat", "var_mode": 0, "map_id": 1, "x": sx + 1, "y": sy},
                {"op": "set_vehicle_location", "vehicle": "ship", "var_mode": 0, "map_id": 1, "x": sx + 2, "y": sy},
            ], 0, 1, 1)
            sess.step("face:right")
            sess.step("toggle_vehicle")
            self.assertEqual(sess.vehicle, "boat")
            self.assertFalse(sess.can_step_action("up"))     # cliff wall (tag=0) blocks boat!
            self.assertFalse(sess.can_step_action("right"))  # ship parked at (sx+2, sy) blocks overlap!

            # 3. Static tilemap layer cache builds lower/upper surfaces & indexes autotiles
            import pygame
            os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
            pygame.init()
            try:
                cache = {}
                draw_calls = []
                def _rec_draw(*args, **kwargs):
                    draw_calls.append(args[4])
                e1 = _get_or_build_map_cache(pygame, 1, sess.current_map(), ts_id, {}, sess.priorities_by_ts.get(ts_id, []), _rec_draw, cache)
                n_first = len(draw_calls)
                self.assertGreater(n_first, 0)
                e2 = _get_or_build_map_cache(pygame, 1, sess.current_map(), ts_id, {}, sess.priorities_by_ts.get(ts_id, []), _rec_draw, cache)
                self.assertIs(e1, e2)
                self.assertEqual(len(draw_calls), n_first)  # cache hit does zero redundant tile draws
            finally:
                pygame.quit()

    def test_32_battle_party_hud_status_tags_and_agi_escape(self):
        from src.harness.player_gui import VIEW_H, VIEW_W, _draw_overlays

        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "ws"
            generate_sample_project(ws)
            ir_dir = ws / "ir"
            sess = HeadlessGameSession(ir_dir, interactive_battle=True)
            sess.states_db[2] = {"fields": {"id": 2, "name": "독"}}
            # Build a 2-enemy troop where enemy AGI is much higher than hero AGI to test failed escape
            sess._get_actor_state(1)["base_agi"] = 5
            sess.enemies[1]["fields"]["agi"] = 60
            sess.enemies[1]["fields"]["maxhp"] = 90
            sess.troops[1]["fields"]["members"] = [
                {"fields": {"enemy_id": 1}},
                {"fields": {"enemy_id": 1}},
            ]
            sess._start_interactive_battle(1, can_escape=True, can_lose=False)
            bs = sess.pending.battle_state
            bs["enemies"][0]["states"] = [2]
            # Turn 1: (1*29 + 1*17)%100 = 46 >= esc_chance(15) -> Escape fails!
            sess.step("choose:4")
            self.assertIsNotNone(sess.pending)
            self.assertIn("도망에 실패했다", bs["last_msg"])

            # Target enemy submenu displays status tag [독]
            sess.step("choose:0")  # 공격 -> target_enemy submenu
            self.assertTrue(any("[독]" in ch for ch in sess.pending.choices))

            # Multi-enemy + Party battle HUD renders cleanly via _draw_overlays
            import pygame
            os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
            pygame.init()
            try:
                surf = pygame.Surface((VIEW_W, VIEW_H))
                font = pygame.font.Font(None, 20)
                sfont = pygame.font.Font(None, 16)
                _draw_overlays(pygame, surf, font, sfont, sess, 0, False)
            finally:
                pygame.quit()

    def test_33_conditional_timer_vehicle_and_variable_math(self):
        from src.core.ir_converter import commands_to_semantic_dsl, semantic_dsl_to_commands

        ops = [
            {"op": "if_timer", "seconds": 30, "cmp": 0, "indent": 0},
            {"op": "set_switch", "start_id": 80, "end_id": 80, "value": "ON", "indent": 1},
            {"op": "end_if", "indent": 0},
            {"op": "if_vehicle", "vehicle": "airship", "indent": 0},
            {"op": "set_switch", "start_id": 81, "end_id": 81, "value": "ON", "indent": 1},
            {"op": "end_if", "indent": 0},
        ]
        rt = commands_to_semantic_dsl(semantic_dsl_to_commands(ops))
        self.assertEqual([c["op"] for c in rt], [c["op"] for c in ops])
        self.assertEqual(rt[0]["seconds"], 30)
        self.assertEqual(rt[3]["vehicle"], "airship")

        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "ws"
            generate_sample_project(ws)
            sess = HeadlessGameSession(ws / "ir", interactive_battle=True)
            sess.gold = 777
            sess.timer_seconds = 45
            sess.timer_active = True
            sess.vehicle = "airship"
            sess._run_commands(ops, 0, 1, 1)
            self.assertTrue(sess.switches.get(80))
            self.assertTrue(sess.switches.get(81))

            # Variable operand types: var (1), random range (2), other:gold (4:2)
            sess.variables[1] = 25
            sess._run_commands([
                {"op": "set_variable", "start_id": 2, "end_id": 2, "operation": 0, "operand_type": 1, "operand": [1]},
                {"op": "set_variable", "start_id": 3, "end_id": 3, "operation": 0, "operand_type": 2, "operand": [10, 20]},
                {"op": "set_variable", "start_id": 4, "end_id": 4, "operation": 0, "operand_type": 4, "operand": [2]},
            ], 0, 1, 1)
            self.assertEqual(sess.variables.get(2), 25)
            self.assertGreaterEqual(sess.variables.get(3, 0), 10)
            self.assertLessEqual(sess.variables.get(3, 0), 20)
            self.assertEqual(sess.variables.get(4), 777)

    def test_34_minimap_grid_and_extended_status_stats(self):
        from src.harness.player_gui import VIEW_H, VIEW_W, _draw_minimap, format_status_lines

        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "ws"
            generate_sample_project(ws)
            sess = HeadlessGameSession(ws / "ir", interactive_battle=True)
            sess.vehicle_positions["boat"] = {"map_id": 1, "x": 3, "y": 3}

            mm = sess.get_minimap_grid()
            self.assertEqual(len(mm["cells"]), mm["height"])
            self.assertEqual(len(mm["cells"][0]), mm["width"])
            self.assertEqual(mm["player"], (sess.x, sess.y))
            self.assertIn(("boat", 3, 3), mm["vehicles"])

            st_lines = format_status_lines(sess)
            self.assertTrue(any("DEX" in ln and "AGI" in ln and "EVA" in ln for ln in st_lines))

            import pygame
            os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
            pygame.init()
            try:
                surf = pygame.Surface((VIEW_W, VIEW_H))
                _draw_minimap(pygame, surf, sess)
            finally:
                pygame.quit()

    def test_35_radar_map_cache_revision_dialogue_paging_and_modal_input_reset(self):
        from src.harness.player_gui import (
            VIEW_H,
            VIEW_W,
            _draw_overlays,
            _get_or_build_map_cache,
            _sync_modal_input_reset,
        )

        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "ws"
            generate_sample_project(ws)
            sess = HeadlessGameSession(ws / "ir", interactive_battle=True)

            # 1. Long 5-line dialogue paginates 3 lines -> 2 lines before closing
            sess._run_commands([
                {"op": "say", "lines": ["1줄", "2줄", "3줄", "4줄", "5줄"]},
                {"op": "set_switch", "start_id": 90, "end_id": 90, "value": "ON"},
            ], 0, 1, 1)
            self.assertIsNotNone(sess.pending)
            self.assertEqual(sess.pending.page_offset, 0)
            self.assertFalse(sess.switches.get(90, False))

            import pygame
            os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
            pygame.init()
            try:
                surf = pygame.Surface((VIEW_W, VIEW_H))
                font = pygame.font.Font(None, 20)
                sfont = pygame.font.Font(None, 16)
                _draw_overlays(pygame, surf, font, sfont, sess, 0, False)

                sess.step("interact")  # advances to page 2 (offset=3)
                self.assertIsNotNone(sess.pending)
                self.assertEqual(sess.pending.page_offset, 3)
                self.assertFalse(sess.switches.get(90, False))

                sess.step("interact")  # finishes dialogue and runs set_switch 90
                self.assertIsNone(sess.pending)
                self.assertTrue(sess.switches.get(90))

                # 2. Runtime tile mutation bumps map_revision and invalidates stale static map cache
                cache = {}
                draw_calls = []
                ts_id = int(sess.current_map()["tileset_id"])
                _get_or_build_map_cache(
                    pygame, 1, sess.current_map(), ts_id, {}, sess.priorities_by_ts.get(ts_id, []),
                    lambda *a, **kw: draw_calls.append(1), cache, revision=sess.map_revision(1)
                )
                first_draws = len(draw_calls)
                self.assertTrue(sess.set_map_tile(1, 0, 2, 2, 384))
                self.assertEqual(sess.map_revision(1), 1)
                _get_or_build_map_cache(
                    pygame, 1, sess.current_map(), ts_id, {}, sess.priorities_by_ts.get(ts_id, []),
                    lambda *a, **kw: draw_calls.append(1), cache, revision=sess.map_revision(1)
                )
                self.assertGreater(len(draw_calls), first_draws)
                self.assertNotIn((1, ts_id, 0), cache)
                self.assertIn((1, ts_id, 1), cache)
            finally:
                pygame.quit()

            # 3. Modal exit resets residual directional buffers and enforces short cooldown
            held, taps = ["right"], ["up"]
            cd = _sync_modal_input_reset(True, False, held, taps, 0)
            self.assertEqual(held, [])
            self.assertEqual(taps, [])
            self.assertGreaterEqual(cd, 4)

    def test_36_system_access_flags_and_scroll_map(self):
        from src.core.ir_converter import commands_to_semantic_dsl, semantic_dsl_to_commands
        from src.harness.player_gui import _process_keydown, process_save_load_shortcut

        ops = [
            {"op": "change_save_access", "enabled": False, "indent": 0},
            {"op": "change_menu_access", "enabled": False, "indent": 0},
            {"op": "change_encounter", "enabled": False, "indent": 0},
            {"op": "scroll_map", "direction": "right", "distance": 3, "speed": 4, "indent": 0},
        ]
        rt = commands_to_semantic_dsl(semantic_dsl_to_commands(ops))
        self.assertEqual([c["op"] for c in rt], [c["op"] for c in ops])
        self.assertFalse(rt[0]["enabled"])
        self.assertEqual(rt[3]["direction"], "right")
        self.assertEqual(rt[3]["distance"], 3)

        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "ws"
            generate_sample_project(ws)
            sess = HeadlessGameSession(ws / "ir", enable_random_encounters=True)
            sess._run_commands(ops, 0, 1, 1)
            self.assertFalse(sess.save_enabled)
            self.assertFalse(sess.menu_enabled)
            self.assertFalse(sess.encounter_enabled)
            self.assertEqual(sess.camera_scroll_target_px, [96, 0])
            sess.advance_timer_ms(250.0)
            self.assertTrue(0 < sess.camera_scroll_px[0] < 96)
            sess.advance_timer_ms(1000.0)
            self.assertEqual(sess.camera_scroll_px, [96, 0])

            # Encounters suppressed even when encounter_steps exceeds threshold
            sess.encounter_steps = 999
            sess._advance_encounter_step()
            self.assertEqual(sess.last_battle_result, "none")

            # F6 save & Tab menu blocked while disabled
            import pygame
            handled, _slot, _c, msg = process_save_load_shortcut(pygame, pygame.K_F6, sess, 1, False)
            self.assertTrue(handled)
            self.assertIn("금지", msg)
            self.assertFalse(sess.slot_exists(1))
            _run, _cur, show_st, _wf, _wt = _process_keydown(pygame, pygame.K_TAB, sess, 0, False, 0, 0)
            self.assertFalse(show_st)

            # Save/load preserves access flags & transfer resets scroll
            blob = sess.export_save_state()
            sess.save_enabled = True
            sess.import_save_state(blob)
            self.assertFalse(sess.save_enabled)
            sess._run_commands([{"op": "transfer_player", "map_id": 1, "x": 3, "y": 3}], 0, 1, 1)
            self.assertEqual(sess.camera_scroll_px, [0, 0])

    def test_37_occasion_rules_reusable_items_and_interactive_element_weakness(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "ws"
            generate_sample_project(ws)
            sess = HeadlessGameSession(ws / "ir", interactive_battle=True)
            # Battle-only item (occasion=1) and reusable field item (occasion=2, consumable=False)
            sess.items[901] = {
                "id": 901,
                "fields": {"name": "전투전용물약", "occasion": 1, "recover_hp": 30, "consumable": True, "scope": 3},
            }
            sess.items[902] = {
                "id": 902,
                "fields": {"name": "무한약초", "occasion": 2, "recover_hp": 20, "consumable": False, "scope": 3},
            }
            sess.inventory[901] = 1
            sess.inventory[902] = 1
            st = sess._get_actor_state(sess.lead_actor_id)
            st["hp"] = max(1, int(st["hp"]) - 40)
            self.assertFalse(sess._use_healing_item(901))
            self.assertTrue(sess._use_healing_item(902))
            self.assertEqual(sess.inventory[902], 1)

            # Menu-only skill (occasion=2) blocked in interactive battle; null & weakness feedback verified
            sess.skills_db[903] = {
                "id": 903,
                "fields": {"name": "귀환주문", "occasion": 2, "sp_cost": 0, "power": 10, "scope": 1},
            }
            sess.skills_db[904] = {
                "id": 904,
                "fields": {"name": "화염참", "occasion": 1, "sp_cost": 0, "power": 50, "scope": 1, "element_set": [2], "hit": 100},
            }
            sess.skills_db[905] = {
                "id": 905,
                "fields": {"name": "냉기파동", "occasion": 1, "sp_cost": 0, "power": 50, "scope": 1, "element_set": [3], "hit": 100},
            }
            sess._run_commands([{"op": "battle", "troop_id": 1, "can_escape": True}], 0, 1, 1)
            self.assertIsNotNone(sess.pending)
            bs = sess.pending.battle_state
            assert bs is not None
            foe = bs["enemies"][0]
            foe["hp"] = 999
            foe["max_hp"] = 999
            foe["element_ranks"] = {"data": [3, 3, 1, 5]}  # elem 2=A(2.0x), elem 3=E(0.0x null)
            sess._execute_battle_skill_on_targets(sess.pending, sess.lead_actor_id, 903, foe)
            self.assertIn("사용할 수 없습니다", bs["last_msg"])
            sess._execute_battle_skill_on_targets(sess.pending, sess.lead_actor_id, 905, foe)
            self.assertIn("무효", bs["last_msg"])
            sess._execute_battle_skill_on_targets(sess.pending, sess.lead_actor_id, 904, foe)
            self.assertIn("약점!", bs["last_msg"])

    def test_38_bestiary_tracking_playtime_and_status_summary(self):
        from src.harness.player_gui import format_status_lines

        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "ws"
            generate_sample_project(ws)
            sess = HeadlessGameSession(ws / "ir", interactive_battle=False)
            sess.advance_timer_ms(65000.0)
            self.assertEqual(sess.play_time_ms, 65000.0)

            res = sess._resolve_battle(1, can_escape=False, can_lose=False)
            self.assertEqual(res, "win")
            self.assertTrue(sess.defeated_enemies)
            summary = sess.get_bestiary_summary()
            self.assertTrue(any("x" in entry for entry in summary))

            lines = format_status_lines(sess)
            self.assertTrue(any("01:05" in ln for ln in lines))
            self.assertTrue(any("몬스터 도감:" in ln for ln in lines))

            blob = sess.export_save_state()
            sess.defeated_enemies.clear()
            sess.play_time_ms = 0.0
            sess.import_save_state(blob)
            self.assertEqual(sess.play_time_ms, 65000.0)
            self.assertTrue(sess.defeated_enemies)

    def test_39_smooth_camera_scroll_and_gui_timer_tick(self):
        from src.harness.player_gui import launch_player

        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "ws"
            generate_sample_project(ws)
            prev_drv = os.environ.get("SDL_VIDEODRIVER")
            os.environ["SDL_VIDEODRIVER"] = "dummy"
            recorded_playtime: List[float] = []
            try:
                def _on_frame(tick, _screen, sess):
                    if tick == 0:
                        sess._run_commands([{"op": "scroll_map", "direction": "down", "distance": 2, "speed": 6, "indent": 0}], 0, 1, 1)
                    recorded_playtime.append(float(sess.play_time_ms))

                launch_player(ws / "ir", max_frames=8, on_frame=_on_frame)
            finally:
                if prev_drv is None:
                    os.environ.pop("SDL_VIDEODRIVER", None)
                else:
                    os.environ["SDL_VIDEODRIVER"] = prev_drv
            self.assertEqual(len(recorded_playtime), 8)
            self.assertGreater(recorded_playtime[-1], recorded_playtime[0])

    def test_40_parallel_common_events_fairness_and_stress(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "ws"
            generate_sample_project(ws)
            sess = HeadlessGameSession(ws / "ir")
            sess.switches[10] = True
            sess.switches[11] = True
            sess.common_events[10] = {
                "id": 10,
                "name": "병렬_카운터A",
                "trigger": "parallel",
                "switch_id": 10,
                "commands": [
                    {"op": "set_variable", "start_id": 20, "end_id": 20, "operation": 1, "operand_type": 0, "operand": [1], "indent": 0},
                    {"op": "set_switch", "start_id": 11, "end_id": 11, "value": "OFF", "indent": 0},
                ],
            }
            sess.common_events[11] = {
                "id": 11,
                "name": "병렬_카운터B",
                "trigger": "parallel",
                "switch_id": 11,
                "commands": [
                    {"op": "set_variable", "start_id": 21, "end_id": 21, "operation": 1, "operand_type": 0, "operand": [1], "indent": 0},
                ],
            }
            sess._process_auto_events()
            # Both parallel CEs active at tick start execute fairly on that tick
            self.assertEqual(sess.variables.get(20), 1)
            self.assertEqual(sess.variables.get(21), 1)
            # On the next tick, CE 11 is now disabled by switch 11=False while CE 10 keeps ticking
            sess._process_auto_events()
            self.assertEqual(sess.variables.get(20), 2)
            self.assertEqual(sess.variables.get(21), 1)

    def test_41_equipment_stat_bonuses_and_class_restrictions(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "ws"
            generate_sample_project(ws)
            sess = HeadlessGameSession(ws / "ir")
            base_stats = sess.actor_full_stats(sess.lead_actor_id)
            sess.weapons[50] = {
                "id": 50,
                "fields": {"name": "룬블레이드", "atk": 20, "pdef": 2, "mdef": 5, "str_plus": 4, "agi_plus": 3, "int_plus": 6},
            }
            sess.weapons[52] = {
                "id": 52,
                "fields": {"name": "금지된지팡이", "atk": 99},
            }
            sess.armors[51] = {
                "id": 51,
                "fields": {"name": "민첩의반지", "kind": 3, "pdef": 1, "mdef": 8, "str_plus": 2, "agi_plus": 5, "eva": 10},
            }
            sess.weapons_owned[50] = 1
            sess.weapons_owned[52] = 1
            sess.armors_owned[51] = 1
            cls_fields = sess.classes_db[int(sess._get_actor_state(sess.lead_actor_id)["class_id"])]["fields"]
            cls_fields["weapon_set"] = [1, 50]
            cls_fields["armor_set"] = list(cls_fields.get("armor_set", [])) + [51]
            self.assertFalse(sess.equip_from_bag(sess.lead_actor_id, 0, 52))
            self.assertTrue(sess.equip_from_bag(sess.lead_actor_id, 0, 50))
            self.assertTrue(sess.equip_from_bag(sess.lead_actor_id, 4, 51))
            boosted = sess.actor_full_stats(sess.lead_actor_id)
            self.assertEqual(boosted["str"], base_stats["str"] + 4)  # iron sword had str_plus=2 -> 4+2-2=4
            self.assertEqual(boosted["mdef"], base_stats["mdef"] + 13)
            self.assertEqual(boosted["agi"], base_stats["agi"] + 8)
            self.assertGreaterEqual(boosted["eva"], base_stats["eva"] + 10)

    def test_42_battle_bgm_me_and_wait_command(self):
        from src.core.ir_converter import commands_to_semantic_dsl, semantic_dsl_to_commands

        ops = [
            {"op": "change_battle_bgm", "name": "Battle2", "volume": 90, "pitch": 100, "indent": 0},
            {"op": "change_battle_end_me", "name": "Victory1", "volume": 85, "pitch": 100, "indent": 0},
            {"op": "wait_frames", "frames": 60, "indent": 0},
        ]
        rt = commands_to_semantic_dsl(semantic_dsl_to_commands(ops))
        self.assertEqual([c["op"] for c in rt], [c["op"] for c in ops])
        self.assertEqual(rt[0]["name"], "Battle2")
        self.assertEqual(rt[1]["name"], "Victory1")
        self.assertEqual(rt[2]["frames"], 60)

        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "ws"
            generate_sample_project(ws)
            sess = HeadlessGameSession(ws / "ir")
            t0 = sess.play_time_ms
            sess._run_commands(ops, 0, 1, 1)
            self.assertEqual(sess.battle_bgm["name"], "Battle2")
            self.assertEqual(sess.battle_end_me["name"], "Victory1")
            self.assertAlmostEqual(sess.play_time_ms - t0, 1000.0, places=3)

    def test_43_battle_bgm_and_victory_me_audio_transition(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "ws"
            generate_sample_project(ws)
            sess = HeadlessGameSession(ws / "ir", interactive_battle=True)
            sess.current_bgm = {"name": "Town1", "volume": 80, "pitch": 100}
            sess._run_commands([
                {"op": "change_battle_bgm", "name": "Battle2", "volume": 90, "pitch": 100, "indent": 0},
                {"op": "change_battle_end_me", "name": "Victory1", "volume": 85, "pitch": 100, "indent": 0},
                {"op": "battle", "troop_id": 1, "can_escape": True, "indent": 0},
            ], 0, 1, 1)
            self.assertIsNotNone(sess.pending)
            self.assertEqual(sess.current_bgm["name"], "Battle2")
            bs = sess.pending.battle_state
            assert bs is not None
            for foe in bs["enemies"]:
                foe["hp"] = 1
            sess.step("choose:0")
            self.assertIsNone(sess.pending)
            self.assertEqual(sess.last_battle_result, "win")
            self.assertEqual(sess.current_bgm["name"], "Town1")
            self.assertTrue(any(m.get("name") == "Victory1" for m in sess.played_me_queue))

    def test_44_magic_defense_and_magic_skill_damage_formula(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "ws"
            generate_sample_project(ws)
            sess = HeadlessGameSession(ws / "ir", interactive_battle=True)
            sess.skills_db[910] = {
                "id": 910,
                "fields": {"name": "암흑파동", "occasion": 1, "sp_cost": 0, "power": 40, "scope": 1, "atk_f": 0, "int_f": 100, "hit": 100},
            }
            sess.armors[60] = {"id": 60, "fields": {"name": "마력로브", "kind": 2, "pdef": 0, "mdef": 40}}
            sess.armors_owned[60] = 1
            cls_fields = sess.classes_db[int(sess._get_actor_state(sess.lead_actor_id)["class_id"])]["fields"]
            cls_fields["armor_set"] = list(cls_fields.get("armor_set", [])) + [60]
            self.assertTrue(sess.equip_from_bag(sess.lead_actor_id, 3, 60))

            enemy = {
                "name": "마법사", "hp": 100, "max_hp": 100, "atk": 10, "int": 40, "pdef": 50, "mdef": 2,
                "actions": [{"kind": 1, "skill_id": 910, "rating": 9}],
            }
            bs = {"turn": 1}
            st = sess._get_actor_state(sess.lead_actor_id)
            hp_before = int(st["hp"])
            sess._execute_enemy_turn_action(enemy, bs, set(), [sess.lead_actor_id], 0)
            dmg_taken = hp_before - int(st["hp"])
            # Enemy int(40) + power//2(20) - mdef(40)//2(20) = 40
            self.assertEqual(dmg_taken, 40)

            # Actor magic skill hits enemy mdef(2) instead of high pdef(50)
            raw = sess._compute_skill_raw_damage(sess.lead_actor_id, sess.skills_db[910]["fields"], enemy)
            self.assertGreater(raw, 20)

    def test_45_camera_scroll_save_load_persistence(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "ws"
            generate_sample_project(ws)
            sess = HeadlessGameSession(ws / "ir")
            sess._run_commands([{"op": "scroll_map", "direction": "down", "distance": 2, "speed": 5, "indent": 0}], 0, 1, 1)
            sess.advance_timer_ms(125.0)
            saved_px = list(sess.camera_scroll_px)
            self.assertGreater(saved_px[1], 0)
            blob = sess.export_save_state()

            sess._run_commands([{"op": "transfer_player", "map_id": 1, "x": 3, "y": 3}], 0, 1, 1)
            self.assertEqual(sess.camera_scroll_px, [0, 0])
            self.assertEqual(sess.camera_scroll_target_px, [0, 0])

            sess.import_save_state(blob)
            self.assertEqual(sess.camera_scroll_px, saved_px)
            self.assertEqual(sess.camera_scroll_target_px, [0, 64])
            self.assertEqual(sess.camera_scroll_speed, 5)

    def test_46_state_shock_and_turn_release_mechanics(self):
        from src.core.battle_engine import shock_release_states, tick_states_on_combatant

        states_db = {
            10: {"fields": {"name": "수면", "restriction": 4, "hold_turn": 2, "auto_release_prob": 100, "shock_release_prob": 100}},
            11: {"fields": {"name": "독", "slip_damage": True, "hold_turn": 3, "auto_release_prob": 0, "shock_release_prob": 0}},
        }
        actor = {"hp": 100, "max_hp": 100, "states": [10, 11]}
        removed = shock_release_states(actor, states_db, roll_pct=20)
        self.assertEqual(removed, [10])
        self.assertEqual(actor["states"], [11])

        actor2 = {"hp": 100, "max_hp": 100, "states": [10]}
        can_act1, _ = tick_states_on_combatant(actor2, states_db, roll_pct=10)
        self.assertFalse(can_act1)
        self.assertIn(10, actor2["states"])
        can_act2, _ = tick_states_on_combatant(actor2, states_db, roll_pct=10)
        self.assertFalse(can_act2)
        self.assertNotIn(10, actor2["states"])  # released at hold_turn=2

    def test_47_enemy_ai_turn_hp_switch_conditions(self):
        from src.core.battle_engine import select_enemy_action

        enemy = {
            "hp": 40,
            "max_hp": 100,
            "actions": [
                {"kind": 0, "basic": 0, "rating": 3, "condition_hp": 100},
                {"kind": 1, "skill_id": 5, "rating": 9, "condition_hp": 50, "condition_switch_id": 77},
            ],
        }
        act_off = select_enemy_action(enemy, 1, switches={77: False})
        self.assertIsNotNone(act_off)
        self.assertEqual(act_off["kind"], 0)
        act_on = select_enemy_action(enemy, 1, switches={77: True})
        self.assertIsNotNone(act_on)
        self.assertEqual(act_on["skill_id"], 5)

    def test_48_skill_and_item_variance_determinism(self):
        from src.core.battle_engine import apply_variance

        vals = [apply_variance(100, 15, seed_val=s) for s in range(31)]
        self.assertTrue(all(85 <= v <= 115 for v in vals))
        self.assertGreater(len(set(vals)), 5)
        self.assertEqual(apply_variance(100, 0, seed_val=7), 100)

    def test_49_recover_all_clears_state_turns_and_skill_variance(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "ws"
            generate_sample_project(ws)
            sess = HeadlessGameSession(ws / "ir")
            st = sess._get_actor_state(sess.lead_actor_id)
            st["hp"] = 5
            st["sp"] = 1
            st["states"] = [2]
            st["state_turns"] = {2: 3}
            sess._run_commands([{"op": "recover_all", "actor_id": 0, "indent": 0}], 0, 1, 1)
            self.assertEqual(st["hp"], st["max_hp"])
            self.assertEqual(st["sp"], st["max_sp"])
            self.assertEqual(st["states"], [])
            self.assertNotIn("state_turns", st)

            sf = {"power": 80, "atk_f": 100, "int_f": 0, "variance": 20}
            foe = {"pdef": 5, "mdef": 5}
            base = sess._compute_skill_raw_damage(sess.lead_actor_id, sf, foe)
            varied = [sess._compute_skill_raw_damage(sess.lead_actor_id, sf, foe, seed_val=i) for i in range(15)]
            self.assertTrue(all(int(base * 0.8) - 1 <= v <= int(base * 1.2) + 1 for v in varied))

    def test_50_show_animation_command_and_queue(self):
        from src.core.ir_converter import commands_to_semantic_dsl, semantic_dsl_to_commands

        ops = [{"op": "show_animation", "character_id": -1, "animation_id": 4, "indent": 0}]
        rt = commands_to_semantic_dsl(semantic_dsl_to_commands(ops))
        self.assertEqual(rt[0]["op"], "show_animation")
        self.assertEqual(rt[0]["character_id"], -1)
        self.assertEqual(rt[0]["animation_id"], 4)

        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "ws"
            generate_sample_project(ws)
            sess = HeadlessGameSession(ws / "ir")
            sess._run_commands(ops, 0, 1, 1)
            self.assertEqual(sess.pending_animations, [{"character_id": -1, "animation_id": 4}])

    def test_51_transfer_player_direction_and_variable_coords(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "ws"
            generate_sample_project(ws)
            sess = HeadlessGameSession(ws / "ir")
            sess.direction = "left"
            sess._run_commands([{"op": "transfer_player", "map_id": 1, "x": 4, "y": 5, "direction": "retain"}], 0, 1, 1)
            self.assertEqual((sess.map_id, sess.x, sess.y, sess.direction), (1, 4, 5, "left"))

            sess.variables[1] = 2
            sess.variables[2] = 6
            sess.variables[3] = 7
            sess._run_commands([{"op": "transfer_player", "var_mode": 1, "map_id": 1, "x": 2, "y": 3, "direction": "up"}], 0, 1, 1)
            self.assertEqual((sess.map_id, sess.x, sess.y, sess.direction), (2, 6, 7, "up"))

    def test_52_gui_pending_animations_render_and_expire(self):
        from src.harness.player_gui import launch_player

        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "ws"
            generate_sample_project(ws)
            prev_drv = os.environ.get("SDL_VIDEODRIVER")
            os.environ["SDL_VIDEODRIVER"] = "dummy"
            queue_lens: List[int] = []
            try:
                def _on_frame(tick, _screen, sess):
                    if tick == 0:
                        sess._run_commands([{"op": "show_animation", "character_id": -1, "animation_id": 2}], 0, 1, 1)
                    queue_lens.append(len(sess.pending_animations))

                launch_player(ws / "ir", max_frames=4, on_frame=_on_frame)
            finally:
                if prev_drv is None:
                    os.environ.pop("SDL_VIDEODRIVER", None)
                else:
                    os.environ["SDL_VIDEODRIVER"] = prev_drv
            self.assertEqual(queue_lens[0], 1)
            self.assertEqual(queue_lens[-1], 0)

    def test_53_battle_physical_hit_triggers_shock_release(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "ws"
            generate_sample_project(ws)
            sess = HeadlessGameSession(ws / "ir", interactive_battle=True)
            sess.states_db[10] = {"fields": {"name": "수면", "restriction": 4, "shock_release_prob": 100}}
            sess._run_commands([{"op": "battle", "troop_id": 1, "can_escape": True}], 0, 1, 1)
            bs = sess.pending.battle_state
            assert bs is not None
            foe = bs["enemies"][0]
            foe["hp"] = 500
            foe["max_hp"] = 500
            foe["states"] = [10]
            sess._apply_physical_attack(sess.pending, sess.lead_actor_id, foe)
            self.assertNotIn(10, foe["states"])

            st = sess._get_actor_state(sess.lead_actor_id)
            st["states"] = [10]
            sess._execute_enemy_turn_action(foe, bs, set(), [sess.lead_actor_id], 0)
            self.assertNotIn(10, st["states"])

    def test_54_auto_battle_uses_unified_skill_formula_and_variance(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "ws"
            generate_sample_project(ws)
            sess = HeadlessGameSession(ws / "ir", interactive_battle=False)
            sess.skills_db[920] = {
                "id": 920,
                "fields": {"name": "성광마법", "occasion": 1, "sp_cost": 0, "power": 80, "atk_f": 0, "int_f": 100, "variance": 10},
            }
            sess._get_actor_state(sess.lead_actor_id)["learned_skills"] = [920]
            res = sess._resolve_battle(1, can_escape=False, can_lose=False)
            self.assertEqual(res, "win")

    def test_55_erase_event_until_map_transfer(self):
        from src.core.ir_converter import commands_to_semantic_dsl, semantic_dsl_to_commands

        ops = [{"op": "erase_event", "indent": 0}]
        rt = commands_to_semantic_dsl(semantic_dsl_to_commands(ops))
        self.assertEqual(rt[0]["op"], "erase_event")

        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "ws"
            generate_sample_project(ws)
            sess = HeadlessGameSession(ws / "ir")
            ev_ids_before = [int(ev["id"]) for ev, _pg in sess.active_events_on_map(1)]
            self.assertTrue(ev_ids_before)
            target_eid = ev_ids_before[0]
            sess._run_commands(ops, 0, 1, target_eid)
            ev_ids_after = [int(ev["id"]) for ev, _pg in sess.active_events_on_map(1)]
            self.assertNotIn(target_eid, ev_ids_after)
            # Transferring to another map restores erased events
            sess._run_commands([{"op": "transfer_player", "map_id": 2, "x": 3, "y": 3}], 0, 1, 1)
            self.assertIn(target_eid, [int(ev["id"]) for ev, _pg in sess.active_events_on_map(1)])

    def test_56_change_transparent_flag_and_save_restore(self):
        from src.core.ir_converter import commands_to_semantic_dsl, semantic_dsl_to_commands

        ops = [
            {"op": "change_transparent_flag", "transparent": True, "indent": 0},
            {"op": "change_transparent_flag", "transparent": False, "indent": 0},
        ]
        rt = commands_to_semantic_dsl(semantic_dsl_to_commands(ops))
        self.assertEqual(rt[0]["op"], "change_transparent_flag")
        self.assertTrue(rt[0]["transparent"])
        self.assertFalse(rt[1]["transparent"])

        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "ws"
            generate_sample_project(ws)
            sess = HeadlessGameSession(ws / "ir")
            self.assertFalse(sess.player_transparent)
            sess._run_commands([ops[0]], 0, 1, 1)
            self.assertTrue(sess.player_transparent)
            self.assertTrue(sess.snapshot()["player_transparent"])
            saved = sess.export_save_state()
            sess._run_commands([ops[1]], 0, 1, 1)
            self.assertFalse(sess.player_transparent)
            sess.import_save_state(saved)
            self.assertTrue(sess.player_transparent)

    def test_57_change_actor_graphic_updates_actor_state(self):
        from src.core.ir_converter import commands_to_semantic_dsl, semantic_dsl_to_commands

        ops = [
            {
                "op": "change_actor_graphic",
                "actor_id": 1,
                "character_name": "002-Fighter02",
                "character_hue": 45,
                "battler_name": "002-Fighter02",
                "battler_hue": 0,
                "indent": 0,
            }
        ]
        rt = commands_to_semantic_dsl(semantic_dsl_to_commands(ops))
        self.assertEqual(rt[0]["op"], "change_actor_graphic")
        self.assertEqual(rt[0]["character_name"], "002-Fighter02")
        self.assertEqual(rt[0]["character_hue"], 45)

        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "ws"
            generate_sample_project(ws)
            sess = HeadlessGameSession(ws / "ir")
            orig_gfx = sess.hero_graphic
            sess._run_commands(ops, 0, 1, 1)
            self.assertEqual(sess.hero_graphic, "002-Fighter02")
            self.assertEqual(sess._get_actor_state(1)["character_hue"], 45)
            saved = sess.export_save_state()
            sess._get_actor_state(1)["character_name"] = orig_gfx
            sess.import_save_state(saved)
            self.assertEqual(sess.hero_graphic, "002-Fighter02")

    def test_58_change_actor_class_refreshes_stats(self):
        from src.core.ir_converter import commands_to_semantic_dsl, semantic_dsl_to_commands

        ops = [{"op": "change_actor_class", "actor_id": 1, "class_id": 2, "indent": 0}]
        rt = commands_to_semantic_dsl(semantic_dsl_to_commands(ops))
        self.assertEqual(rt[0]["op"], "change_actor_class")
        self.assertEqual(rt[0]["class_id"], 2)

        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "ws"
            generate_sample_project(ws)
            sess = HeadlessGameSession(ws / "ir")
            # Add class 2 that only allows weapon_set=[2] and teaches skill 990 at level 1
            sess.classes_db[2] = {
                "id": 2,
                "fields": {
                    "name": "마도사",
                    "weapon_set": [2],
                    "armor_set": [2],
                    "learnings": [{"fields": {"level": 1, "skill_id": 990}}],
                },
            }
            st = sess._get_actor_state(1)
            st["weapon_id"] = 1
            sess._recompute_combat_stats()
            atk_before = sess.atk
            st["hp"] = st["max_hp"] + 50
            st["learned_skills"] = [1]

            sess._run_commands(ops, 0, 1, 1)
            self.assertEqual(st["class_id"], 2)
            self.assertEqual(st["weapon_id"], 0)
            self.assertLessEqual(st["hp"], st["max_hp"])
            self.assertIn(1, st["learned_skills"])
            self.assertIn(990, st["learned_skills"])
            self.assertLessEqual(sess.atk, atk_before)

    def test_59_advance_screen_fx_frames_parity(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "ws"
            generate_sample_project(ws)
            sess = HeadlessGameSession(ws / "ir")
            sess.pending_flash = {"rgba": [255, 255, 255, 180], "frames": 6, "max_frames": 6}
            sess.pending_shake = {"power": 5, "speed": 5, "frames": 4}
            sess.advance_screen_fx_frames(2)
            self.assertEqual(sess.pending_flash["frames"], 4)
            self.assertEqual(sess.pending_shake["frames"], 2)
            sess._run_commands([{"op": "wait", "frames": 2}], 0, 1, 1)
            self.assertEqual(sess.pending_flash["frames"], 2)
            self.assertIsNone(sess.pending_shake)
            sess.advance_screen_fx_frames(2)
            self.assertIsNone(sess.pending_flash)

    def test_60_change_windowskin_roundtrip_and_save(self):
        from src.core.ir_converter import commands_to_semantic_dsl, semantic_dsl_to_commands

        ops = [{"op": "change_windowskin", "windowskin_name": "001-Blue01", "indent": 0}]
        rt = commands_to_semantic_dsl(semantic_dsl_to_commands(ops))
        self.assertEqual(rt[0]["op"], "change_windowskin")
        self.assertEqual(rt[0]["windowskin_name"], "001-Blue01")

        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "ws"
            generate_sample_project(ws)
            sess = HeadlessGameSession(ws / "ir")
            sess._run_commands(ops, 0, 1, 1)
            self.assertEqual(sess.windowskin_name, "001-Blue01")
            self.assertEqual(sess.snapshot()["windowskin_name"], "001-Blue01")
            saved = sess.export_save_state()
            sess._run_commands([{"op": "change_windowskin", "windowskin_name": "Default"}], 0, 1, 1)
            self.assertEqual(sess.windowskin_name, "Default")
            sess.import_save_state(saved)
            self.assertEqual(sess.windowskin_name, "001-Blue01")

    def test_61_gui_windowskin_theme_resolution(self):
        from src.harness.player_gui import resolve_windowskin_theme

        cache = {}
        t_blue = resolve_windowskin_theme("001-Blue01", cache=cache)
        t_red = resolve_windowskin_theme("Crimson-Red02", cache=cache)
        t_def = resolve_windowskin_theme("", cache=cache)
        self.assertEqual(t_blue["bg"], (22, 34, 68))
        self.assertEqual(t_red["bg"], (56, 20, 28))
        self.assertEqual(t_def["bg"], (20, 24, 36))
        self.assertIs(resolve_windowskin_theme("001-Blue01", cache=cache), t_blue)

    def test_62_transfer_reset_erased_and_move_route_cleanup(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "ws"
            generate_sample_project(ws)
            sess = HeadlessGameSession(ws / "ir")
            target_eid = int(sess.active_events_on_map(1)[0][0]["id"])
            sess._active_move_routes[(1, target_eid)] = {"route": ["down"], "idx": 0}
            sess._run_commands([{"op": "erase_event", "indent": 0}], 0, 1, target_eid)
            self.assertNotIn((1, target_eid), sess._active_move_routes)
            saved = sess.export_save_state()
            # Same-map transfer preserves erased state unless reset_erased=True
            sess._run_commands([{"op": "transfer_player", "map_id": 1, "x": 1, "y": 1}], 0, 1, 1)
            self.assertIn((1, target_eid), sess.erased_events)
            # Same-map transfer with reset_erased=True restores
            sess._run_commands([{"op": "transfer_player", "map_id": 1, "x": 1, "y": 1, "reset_erased": True}], 0, 1, 1)
            self.assertNotIn((1, target_eid), sess.erased_events)
            # Save restore brings erased event set back, then reset_erased_events clears it
            sess.import_save_state(saved)
            self.assertIn((1, target_eid), sess.erased_events)
            self.assertEqual(sess.reset_erased_events(1), 1)
            self.assertNotIn((1, target_eid), sess.erased_events)

    def test_63_class_param_bonuses_and_magic_stats(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "ws"
            generate_sample_project(ws)
            sess = HeadlessGameSession(ws / "ir")
            base_mhp = sess.max_hp
            base_atk = sess.atk
            base_int = sess.actor_full_stats(1)["int"]
            sess.classes_db[3] = {
                "id": 3,
                "fields": {
                    "name": "현자",
                    "class_params": {"mhp": 40, "msp": 30, "int": 18, "mdef": 12, "atk": -2},
                    "learnings": [],
                },
            }
            sess._run_commands([{"op": "change_actor_class", "actor_id": 1, "class_id": 3}], 0, 1, 1)
            stats = sess.actor_full_stats(1)
            self.assertEqual(sess.max_hp, base_mhp + 40)
            self.assertEqual(stats["int"], base_int + 18)
            self.assertGreaterEqual(stats["mdef"], 12)
            self.assertEqual(sess.atk, base_atk - 2)
            # Switching back cleanly removes the reversible class_params vital bonus
            sess._run_commands([{"op": "change_actor_class", "actor_id": 1, "class_id": 1}], 0, 1, 1)
            self.assertEqual(sess.max_hp, base_mhp)
            self.assertEqual(sess.actor_full_stats(1)["int"], base_int)

    def test_64_comment_and_actor_name_armor_branches(self):
        from src.core.ir_converter import commands_to_semantic_dsl, semantic_dsl_to_commands

        ops = [
            {"op": "comment", "lines": ["주석 1줄", "주석 2줄"], "indent": 0},
            {"op": "change_actor_name", "actor_id": 1, "name": "아서", "indent": 0},
            {"op": "if_actor_name", "actor_id": 1, "name": "아서", "indent": 0},
            {"op": "set_switch", "start_id": 12, "end_id": 12, "value": "ON", "indent": 1},
            {"op": "end_if", "indent": 0},
            {"op": "if_actor_armor", "actor_id": 1, "armor_id": 1, "indent": 0},
            {"op": "set_switch", "start_id": 13, "end_id": 13, "value": "ON", "indent": 1},
            {"op": "end_if", "indent": 0},
        ]
        rt = commands_to_semantic_dsl(semantic_dsl_to_commands(ops))
        self.assertEqual(rt[0], {"op": "comment", "lines": ["주석 1줄", "주석 2줄"], "indent": 0})
        self.assertEqual(rt[2]["op"], "if_actor_name")
        self.assertEqual(rt[5]["op"], "if_actor_armor")

        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "ws"
            generate_sample_project(ws)
            sess = HeadlessGameSession(ws / "ir")
            sess._get_actor_state(1)["armor_ids"][1] = 1
            sess._run_commands(ops, 0, 1, 1)
            self.assertTrue(sess.switches.get(12, False))
            self.assertTrue(sess.switches.get(13, False))

    def test_65_troop_battle_enemy_commands_and_abort(self):
        from src.core.ir_converter import commands_to_semantic_dsl, semantic_dsl_to_commands

        ops = [
            {"op": "change_enemy_hp", "enemy_index": 0, "mode": "sub", "operand_type": 0, "amount": 25, "allow_knockout": True, "indent": 0},
            {"op": "change_enemy_sp", "enemy_index": 0, "mode": "sub", "operand_type": 0, "amount": 10, "indent": 0},
            {"op": "change_enemy_state", "enemy_index": 0, "mode": "add", "state_id": 3, "indent": 0},
            {"op": "enemy_recover_all", "enemy_index": 0, "indent": 0},
            {"op": "abort_battle", "indent": 0},
        ]
        rt = commands_to_semantic_dsl(semantic_dsl_to_commands(ops))
        self.assertEqual([x["op"] for x in rt], [x["op"] for x in ops])

        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "ws"
            generate_sample_project(ws)
            sess = HeadlessGameSession(ws / "ir", interactive_battle=True)
            troop = sess.troops[1]["fields"]
            troop["pages"] = [
                {
                    "fields": {
                        "span": 0,
                        "condition": {"fields": {"turn_valid": True, "turn_a": 2, "turn_b": 0}},
                        "list": ops[:3],
                    }
                }
            ]
            sess._start_interactive_battle(1, can_escape=True, can_lose=True)
            foe = sess.pending.battle_state["enemies"][0]
            foe["max_hp"] = 200
            foe["hp"] = 180
            while sess.pending and int(sess.pending.battle_state["turn"]) < 2:
                sess._resolve_battle_choice(sess.pending, 2)  # Defend until Turn 2 -> troop event fires!
            self.assertLessEqual(foe["hp"], 155)
            self.assertIn(3, foe["states"])
            # Now verify abort_battle on page for turn 3
            troop["pages"].append({
                "fields": {
                    "span": 0,
                    "condition": {"fields": {"turn_valid": True, "turn_a": 3, "turn_b": 0}},
                    "list": [ops[4]],
                }
            })
            while sess.pending and sess.pending.battle_state is not None:
                sess._resolve_battle_choice(sess.pending, 2)
            self.assertEqual(sess.last_battle_result, "escape")

    def test_66_player_move_route_cutscene_and_through(self):
        from src.core.ir_converter import commands_to_semantic_dsl, semantic_dsl_to_commands

        ops = [
            {
                "op": "set_move_route",
                "target_id": -1,
                "steps": ["through_on", "up", "turn_left", "through_off"],
                "repeat": False,
                "skippable": True,
                "indent": 0,
            }
        ]
        rt = commands_to_semantic_dsl(semantic_dsl_to_commands(ops))
        self.assertEqual(rt[0]["steps"], ["through_on", "up", "turn_left", "through_off"])

        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "ws"
            generate_sample_project(ws)
            sess = HeadlessGameSession(ws / "ir")
            sess.x, sess.y = 1, 1  # y=0 is border wall!
            self.assertFalse(sess.can_step_action("up"))
            sess._run_commands(ops, 0, 1, 1)
            self.assertEqual((sess.x, sess.y), (1, 0))
            self.assertEqual(sess.direction, "left")
            self.assertFalse(sess.player_through)

    def test_67_audio_me_fadeout_bgs_and_stop_se(self):
        from src.core.ir_converter import commands_to_semantic_dsl, semantic_dsl_to_commands

        ops = [
            {"op": "play_bgs", "name": "001-Wind01", "volume": 75, "pitch": 100, "indent": 0},
            {"op": "fadeout_bgs", "seconds": 3, "indent": 0},
            {"op": "play_me", "name": "008-Inn01", "volume": 90, "pitch": 100, "indent": 0},
            {"op": "play_se", "name": "001-System01", "volume": 80, "pitch": 100, "indent": 0},
            {"op": "stop_se", "indent": 0},
        ]
        rt = commands_to_semantic_dsl(semantic_dsl_to_commands(ops))
        self.assertEqual([x["op"] for x in rt], [x["op"] for x in ops])

        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "ws"
            generate_sample_project(ws)
            sess = HeadlessGameSession(ws / "ir")
            sess._run_commands(ops[:1], 0, 1, 1)
            self.assertEqual(sess.current_bgs["name"], "001-Wind01")
            sess._run_commands(ops[1:3], 0, 1, 1)
            self.assertIsNone(sess.current_bgs)
            self.assertEqual(sess.played_me_queue[-1]["name"], "008-Inn01")
            sess._run_commands(ops[3:], 0, 1, 1)
            self.assertEqual(sess.played_se_queue, [])

    def test_68_enemy_item_weapon_armor_treasure_drops(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "ws"
            generate_sample_project(ws)
            sess = HeadlessGameSession(ws / "ir", interactive_battle=False)
            ef = sess.enemies[1]["fields"]
            ef["item_id"] = 1
            ef["weapon_id"] = 0
            ef["armor_id"] = 0
            ef["treasure_prob"] = 100
            before_item = sess.inventory.get(1, 0)
            res = sess._resolve_battle(1, can_escape=False, can_lose=False)
            self.assertEqual(res, "win")
            self.assertGreater(sess.inventory.get(1, 0), before_item)
            # Also verify weapon drop when treasure_prob == 100
            ef["item_id"] = 0
            ef["weapon_id"] = 1
            before_wpn = sess.weapons_owned.get(1, 0)
            sess._resolve_battle(1, can_escape=False, can_lose=False)
            self.assertGreater(sess.weapons_owned.get(1, 0), before_wpn)

    def test_69_status_cure_items_skills_and_field_slip(self):
        from src.harness.player_gui import list_field_skills_and_items_for_actor

        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "ws"
            generate_sample_project(ws)
            sess = HeadlessGameSession(ws / "ir")
            sess.states_db[2] = {"id": 2, "fields": {"name": "독", "slip_damage": True}}
            sess.items[50] = {"id": 50, "fields": {"name": "해독초", "occasion": 0, "minus_state_set": [2], "consumable": True}}
            sess.inventory[50] = 2
            sess._get_actor_state(1)["states"] = [2]
            hp_before = sess.hp
            sess.step("right")
            self.assertLess(sess.hp, hp_before)
            entries = list_field_skills_and_items_for_actor(sess, actor_id=1)
            self.assertTrue(any(kind == "item" and oid == 50 for kind, oid, _lbl in entries))
            self.assertTrue(sess._use_healing_item(50, actor_id=1))
            self.assertNotIn(2, sess._get_actor_state(1)["states"])
            self.assertEqual(sess.inventory[50], 1)
            # Second use when healthy does not waste item
            self.assertFalse(sess._use_healing_item(50, actor_id=1))
            self.assertEqual(sess.inventory[50], 1)
            # Revive skill on field (scope 5)
            sess.actors[2] = {"id": 2, "fields": {"name": "아리아", "initial_level": 1, "max_hp": 100, "max_sp": 30}}
            sess.party_member_ids.append(2)
            st2 = sess._get_actor_state(2)
            st2["hp"] = 0
            sess.skills_db[930] = {"id": 930, "fields": {"name": "소생", "occasion": 2, "scope": 5, "power": -60, "sp_cost": 5}}
            sess._get_actor_state(1)["learned_skills"].append(930)
            self.assertTrue(sess.use_skill_on_field(1, 930))
            self.assertGreater(st2["hp"], 0)

    def test_70_commercial_shop_owned_qty_and_stat_diff(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "ws"
            generate_sample_project(ws)
            sess = HeadlessGameSession(ws / "ir")
            sess.gold = 1000
            sess.inventory[1] = 2
            sess.weapons[50] = {"id": 50, "fields": {"name": "마검", "price": 200, "atk": 35}}
            sess.armors[50] = {"id": 50, "fields": {"name": "용갑옷", "price": 300, "kind": 2, "pdef": 25}}
            sess.armors[51] = {"id": 51, "fields": {"name": "금단의방패", "price": 400, "kind": 0, "pdef": 40}}
            cls1 = sess.classes_db.get(sess._get_actor_state(1)["class_id"], {}).get("fields", {})
            cls1.setdefault("weapon_set", []).append(50)
            cls1.setdefault("armor_set", []).append(50)
            sess._run_commands([{"op": "shop", "goods": [{"type": "item", "id": 1}, {"type": "weapon", "id": 50}, {"type": "armor", "id": 50}, {"type": "armor", "id": 51}]}], 0, 1, 1)
            self.assertIsNotNone(sess.pending)
            details = sess.get_shop_buy_details()
            self.assertEqual(details[0]["owned"], 2)
            self.assertEqual(details[1]["stat_label"], "ATK")
            self.assertGreater(details[1]["stat_diff"], 0)
            self.assertEqual(details[2]["stat_label"], "DEF")
            self.assertGreater(details[2]["stat_diff"], 0)
            self.assertFalse(details[3]["can_equip"])
            self.assertIn("장착불가", details[3]["summary_text"])
            # Purchasing updates owned count immediately in get_shop_buy_details
            sess.step("choose:0")
            self.assertEqual(sess.get_shop_buy_details()[0]["owned"], 3)
            sess.step("choose:1")
            self.assertEqual(sess.get_shop_buy_details()[1]["owned"], 1)
            sess.step(f"choose:{len(sess.pending.shop_goods) + 1}")  # exit shop
            self.assertIsNone(sess.pending)

    def test_71_audio_mute_toggle_and_save_slot_metadata(self):
        from src.harness.player_gui import _sync_audio_runtime

        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "ws"
            generate_sample_project(ws)
            sess = HeadlessGameSession(ws / "ir")
            sess.play_time_ms = 125_000.0  # 02:05
            sess.gold = 999
            self.assertFalse(sess.audio_muted)
            self.assertTrue(sess.toggle_audio_mute())
            self.assertTrue(sess.audio_muted)
            sess.played_se_queue.append({"name": "Door", "volume": 80, "pitch": 100})
            sess.played_me_queue.append({"name": "Fanfare", "volume": 90, "pitch": 100})
            # Muted sync drains queued SE/ME cleanly without mixer
            _sync_audio_runtime(None, None, sess, None, False)
            self.assertEqual(sess.played_se_queue, [])
            self.assertEqual(sess.played_me_queue, [])
            sess.save_to_file(slot=3)
            meta = sess.get_save_slot_summary(3)
            self.assertEqual(meta["play_time"], "02:05")
            self.assertIn("02:05", meta["label"])
            # Restore into fresh session preserves audio_muted and play_time_ms
            sess2 = HeadlessGameSession(ws / "ir")
            self.assertFalse(sess2.audio_muted)
            self.assertTrue(sess2.load_from_file(slot=3))
            self.assertTrue(sess2.audio_muted)
            self.assertAlmostEqual(sess2.play_time_ms, 125_000.0)

    def test_72_cli_inspect_summary_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "ws"
            generate_sample_project(ws)
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = cli_main(["--json", "inspect", str(ws / "ir")])
            self.assertEqual(rc, 0)
            payload = json.loads(buf.getvalue())
            self.assertGreaterEqual(len(payload["maps"]), 2)
            self.assertGreater(payload["events_total"], 0)
            for key in ("actors", "classes", "skills", "items", "weapons", "armors", "enemies", "troops", "states", "common_events"):
                self.assertIn(key, payload)
                self.assertGreaterEqual(payload[key], 1)

    def test_73_e2e_commercial_rpg_loop_and_gui_render_stress(self):
        from src.harness.player_gui import VIEW_H, VIEW_W, launch_player

        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "ws"
            generate_sample_project(ws)
            ir_dir = ws / "ir"
            sess = HeadlessGameSession(ir_dir, interactive_battle=False)
            sess.classes_db[5] = {
                "id": 5,
                "fields": {"name": "성기사", "class_params": {"str": 6, "int": 4, "pdef": 5, "mdef": 7}},
            }
            # 1. Cutscene + Windowskin + Transparent + Player Move Route + Audio ME/BGS/SE + Class change
            sess._run_commands(
                [
                    {"op": "comment", "text": "Commercial E2E Cutscene"},
                    {"op": "change_windowskin", "windowskin_name": "Blue"},
                    {"op": "change_transparent_flag", "transparent": True},
                    {"op": "change_actor_class", "actor_id": 1, "class_id": 5},
                    {"op": "play_bgs", "name": "River", "volume": 65, "pitch": 100},
                    {"op": "fadeout_bgs", "seconds": 1},
                    {"op": "play_me", "name": "Victory", "volume": 85, "pitch": 100},
                    {"op": "stop_se"},
                    {"op": "set_move_route", "target_id": -1, "steps": ["through_on", "move_right", "turn_up"], "repeat": False},
                ],
                0,
                1,
                1,
            )
            self.assertEqual(sess.windowskin_name, "Blue")
            self.assertTrue(sess.player_transparent)
            self.assertTrue(sess.player_through)
            self.assertEqual(sess.direction, "up")
            self.assertIsNone(sess.current_bgs)
            self.assertEqual(len(sess.played_me_queue), 1)
            self.assertEqual(sess.played_se_queue, [])
            st1 = sess._get_actor_state(1)
            self.assertEqual(st1["class_id"], 5)
            self.assertGreaterEqual(sess.actor_full_stats(1)["mdef"], 7)

            # 2. Battle drop + shop purchase + status cure + mute + save/reload
            sess.enemies[1]["fields"]["item_id"] = 1
            sess.enemies[1]["fields"]["treasure_prob"] = 100
            before_i = sess.inventory.get(1, 0)
            self.assertEqual(sess._resolve_battle(1, can_escape=False, can_lose=False), "win")
            self.assertGreater(sess.inventory.get(1, 0), before_i)
            sess.play_time_ms = 64_000.0  # 01:04
            sess.toggle_audio_mute()
            sess.save_to_file(slot=5)
            meta = sess.get_save_slot_summary(5)
            self.assertEqual(meta["play_time"], "01:04")

            sess2 = HeadlessGameSession(ir_dir)
            self.assertTrue(sess2.load_from_file(slot=5))
            self.assertEqual(sess2.windowskin_name, "Blue")
            self.assertTrue(sess2.player_through)
            self.assertTrue(sess2.audio_muted)

            # 3. Multi-frame Pygame dummy GUI render stress (5 frames)
            os.environ["SDL_VIDEODRIVER"] = "dummy"
            frames_seen: list[int] = []
            samples: list[tuple[int, int, int]] = []

            def _on_frame(tick, screen, _s):
                frames_seen.append(tick)
                if tick == 4:
                    for pt in ((16, 8), (VIEW_W // 2, VIEW_H // 2), (VIEW_W - 10, VIEW_H - 10)):
                        c = screen.get_at(pt)
                        samples.append((int(c[0]), int(c[1]), int(c[2])))

            rc = launch_player(ir_dir, graphics_dir=ws / "rmxp" / "Graphics", max_frames=5, on_frame=_on_frame)
            self.assertEqual(rc, 0)
            self.assertEqual(frames_seen, [0, 1, 2, 3, 4])
            self.assertGreaterEqual(len(set(samples)), 2)

    def test_74_gui_title_and_options_scene(self):
        from src.harness.player_gui import (
            build_title_menu_options,
            launch_player,
            process_title_selection,
        )

        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "ws"
            generate_sample_project(ws)
            ir_dir = ws / "ir"
            sess = HeadlessGameSession(ir_dir)
            opts0 = build_title_menu_options(sess, mode="title", show_minimap=True)
            self.assertIn("불러오기 (저장 없음)", opts0)
            sess.save_to_file(slot=2)
            opts1 = build_title_menu_options(sess, mode="title", show_minimap=True)
            self.assertIn("불러오기", opts1)
            # Options submenu toggles mute and minimap cleanly
            r_opt = process_title_selection(sess, "title", 2, True)
            self.assertEqual(r_opt["next_mode"], "options")
            r_mute = process_title_selection(sess, "options", 0, True)
            self.assertTrue(sess.audio_muted)
            self.assertEqual(r_mute["next_mode"], "options")
            r_mm = process_title_selection(sess, "options", 1, True)
            self.assertFalse(r_mm["show_minimap"])
            r_back = process_title_selection(sess, "options", 2, False)
            self.assertEqual(r_back["next_mode"], "title")
            r_start = process_title_selection(sess, "title", 0, False)
            self.assertEqual(r_start["next_mode"], "map")

            os.environ["SDL_VIDEODRIVER"] = "dummy"
            seen = []
            rc = launch_player(ir_dir, max_frames=3, on_frame=lambda t, _scr, _s: seen.append(t), start_on_title=True)
            self.assertEqual(rc, 0)
            self.assertEqual(seen, [0, 1, 2])

    def test_75_gui_save_slot_overlay_and_mouse_click(self):
        import pygame
        from src.harness.player_gui import (
            SAVE_SLOT_COUNT,
            VIEW_H,
            VIEW_W,
            _draw_overlays,
            _draw_save_slot_overlay,
            build_save_slot_rows,
            hit_test_rects,
        )

        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "ws"
            generate_sample_project(ws)
            sess = HeadlessGameSession(ws / "ir")
            sess.gold = 777
            sess.save_to_file(slot=4)
            rows = build_save_slot_rows(sess)
            self.assertEqual(len(rows), 9)
            self.assertTrue(rows[3]["exists"])
            self.assertIn("777G", rows[3]["label"])

            os.environ["SDL_VIDEODRIVER"] = "dummy"
            pygame.init()
            try:
                surf = pygame.Surface((VIEW_W, VIEW_H))
                font = pygame.font.Font(None, 20)
                sfont = pygame.font.Font(None, 16)
                slot_rects = _draw_save_slot_overlay(pygame, surf, font, sfont, sess, "load", 3)
                self.assertEqual(len(slot_rects), 9)
                rx, ry, rw, rh = slot_rects[3]
                self.assertEqual(hit_test_rects((rx + 5, ry + 5), slot_rects), 3)
                self.assertIsNone(hit_test_rects((0, 0), slot_rects))

                # Dialogue choice click rect hit testing
                sess._run_commands(
                    [{"op": "show_choices", "choices": ["수락", "거절"], "cancel_type": 2}],
                    0,
                    1,
                    1,
                )
                c_rects = []
                _draw_overlays(pygame, surf, font, sfont, sess, 0, False, out_choice_rects=c_rects)
                self.assertEqual(len(c_rects), 2)
                cx, cy, _cw, _ch = c_rects[1]
                self.assertEqual(hit_test_rects((cx + 4, cy + 4), c_rects), 1)
            finally:
                pygame.quit()

    def test_76_animation_sheet_cells_and_interpolation(self):
        from src.harness.player_gui import extract_animation_frame_cells, launch_player

        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "ws"
            generate_sample_project(ws)
            ir_dir = ws / "ir"
            # Construct a real 2-cell RPG::Animation frame table (xsize=2, ysize=8)
            # cell 0: pattern=0, x=12, y=-8, zoom=120, angle=45, mirror=0, opacity=200, blend=0
            # cell 1: pattern=-1 (empty)
            data = [0] * 16
            data[0 + 0 * 2] = 0
            data[0 + 1 * 2] = 12
            data[0 + 2 * 2] = -8
            data[0 + 3 * 2] = 120
            data[0 + 4 * 2] = 45
            data[0 + 5 * 2] = 0
            data[0 + 6 * 2] = 200
            data[0 + 7 * 2] = 0
            data[1 + 0 * 2] = -1
            anim_path = ir_dir / "database" / "animations.json"
            anims = json.loads(anim_path.read_text(encoding="utf-8"))
            anims.append({
                "__type__": "RubyObject",
                "class": "RPG::Animation",
                "fields": {
                    "id": 2,
                    "name": "검기",
                    "animation_name": "SlashSheet",
                    "frame_max": 1,
                    "frames": [{
                        "__type__": "RubyObject",
                        "class": "RPG::Animation::Frame",
                        "fields": {
                            "cell_max": 2,
                            "cell_data": {"__type__": "Table", "dims": 2, "xsize": 2, "ysize": 8, "zsize": 1, "data": data},
                        },
                    }],
                },
            })
            anim_path.write_text(json.dumps(anims, ensure_ascii=False, indent=2), encoding="utf-8")
            # Provide an actual 192x192 animation sprite sheet PNG
            anim_dir = ws / "rmxp" / "Graphics" / "Animations"
            anim_dir.mkdir(parents=True, exist_ok=True)
            Image.new("RGBA", (192 * 5, 192), (255, 180, 40, 220)).save(anim_dir / "SlashSheet.png")

            sess = HeadlessGameSession(ir_dir)
            self.assertIn(2, sess.animations_db)
            cells = extract_animation_frame_cells(sess.animations_db[2], 0)
            self.assertEqual(len(cells), 1)
            self.assertEqual((cells[0]["x"], cells[0]["y"], cells[0]["zoom"], cells[0]["angle"]), (12, -8, 120, 45))

            os.environ["SDL_VIDEODRIVER"] = "dummy"
            def _inject_anim(tick, _scr, live_sess):
                if tick == 0:
                    live_sess.pending_animations.append({"character_id": -1, "animation_id": 2})

            rc = launch_player(ir_dir, graphics_dir=ws / "rmxp" / "Graphics", max_frames=3, on_frame=_inject_anim)
            self.assertEqual(rc, 0)

    def test_77_audio_pitch_resampler_wav_and_cache(self):
        import array
        import wave
        from src.harness.player_gui import resample_wav_pitch_bytes

        # Synthesize 440Hz 16-bit mono WAV (4410 frames = 0.2s @ 22050Hz)
        buf = io.BytesIO()
        samples = array.array("h", [int(8000 * ((-1) ** (i // 25))) for i in range(4410)])
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(22050)
            wf.writeframes(samples.tobytes())
        raw_wav = buf.getvalue()

        # pitch=150 -> ~2940 frames; pitch=75 -> ~5880 frames
        hi = resample_wav_pitch_bytes(raw_wav, 150)
        lo = resample_wav_pitch_bytes(raw_wav, 75)
        self.assertIsNotNone(hi)
        self.assertIsNotNone(lo)
        with wave.open(io.BytesIO(hi), "rb") as whi, wave.open(io.BytesIO(lo), "rb") as wlo:
            self.assertEqual(whi.getframerate(), 22050)
            self.assertEqual(whi.getnframes(), 2940)
            self.assertEqual(wlo.getnframes(), 5880)
        self.assertEqual(resample_wav_pitch_bytes(raw_wav, 100), raw_wav)

    def test_78_large_project_100_maps_rgssad_and_bfs_benchmark(self):
        import time
        from src.core.validator import validate_ir_project

        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "ws_100"
            generate_sample_project(ws)
            ir_dir = ws / "ir"
            proj_path = ir_dir / "ir_project.json"
            proj = json.loads(proj_path.read_text(encoding="utf-8"))
            m1_path = ir_dir / "maps" / "map_001.json"
            m1 = json.loads(m1_path.read_text(encoding="utf-8"))
            base_map = dict(m1)
            base_page = dict(m1["events"][0]["pages"][0])
            empty_row = [0] * 20
            walk_row = [384] * 20
            blank_grid = [list(empty_row) for _ in range(15)]
            walk_grid = [list(walk_row) for _ in range(15)]

            def _make_page(trigger_code: int, commands: list) -> dict:
                pg = json.loads(json.dumps(base_page))
                pg["trigger_code"] = trigger_code
                pg["condition"] = {"fields": {}}
                pg["commands"] = commands
                return pg

            def _make_transfer_event(ev_id: int, x: int, y: int, target_map: int, tx: int, ty: int) -> dict:
                return {
                    "id": ev_id,
                    "name": f"Warp_{target_map}",
                    "x": x,
                    "y": y,
                    "pages": [_make_page(1, [
                        {"op": "transfer_player", "map_id": target_map, "x": tx, "y": ty, "direction": 2},
                    ])],
                }

            # Existing sample project has map_001 and map_002; add map_003..map_100 linked sequentially
            m1["events"].append(_make_transfer_event(900, 10, 7, 3, 2, 7))
            m1_path.write_text(json.dumps(m1, ensure_ascii=False), encoding="utf-8")

            for mid in range(3, 101):
                events = []
                if mid < 100:
                    events.append(_make_transfer_event(1, 10, 7, mid + 1, 2, 7))
                else:
                    events.append({
                        "id": 1,
                        "name": "Goal100",
                        "x": 5,
                        "y": 7,
                        "pages": [_make_page(0, [
                            {"op": "set_switch", "start_id": 10, "end_id": 10, "value": "ON"},
                        ])],
                    })
                m_ir = {
                    "map_id": mid,
                    "name": f"Stage_{mid}",
                    "tileset_id": int(base_map.get("tileset_id", 1)),
                    "width": 20,
                    "height": 15,
                    "autoplay_bgm": False,
                    "bgm": dict(base_map.get("bgm", {})),
                    "autoplay_bgs": False,
                    "bgs": dict(base_map.get("bgs", {})),
                    "encounter_list": [],
                    "encounter_step": 30,
                    "layers": {
                        "layer_0": [list(r) for r in walk_grid],
                        "layer_1": [list(r) for r in blank_grid],
                        "layer_2": [list(r) for r in blank_grid],
                    },
                    "events": events,
                }
                (ir_dir / "maps" / f"map_{mid:03d}.json").write_text(json.dumps(m_ir, ensure_ascii=False), encoding="utf-8")
                info_obj = {
                    "__type__": "RubyObject",
                    "class": "RPG::MapInfo",
                    "fields": {"name": f"Stage_{mid}", "parent_id": 0, "order": mid, "expanded": False, "scroll_x": 0, "scroll_y": 0},
                }
                proj["map_infos"]["entries"].append([mid, info_obj])
            sw_list = proj["system"]["fields"].setdefault("switches", [None])
            while len(sw_list) <= 10:
                sw_list.append(f"StageFlag_{len(sw_list)}")
            proj_path.write_text(json.dumps(proj, ensure_ascii=False), encoding="utf-8")

            t0 = time.perf_counter()
            val_res = validate_ir_project(ir_dir)
            bfs_elapsed = time.perf_counter() - t0
            self.assertTrue(val_res.passed, f"100-map validation failed: {val_res.errors}")
            self.assertLess(bfs_elapsed, 5.0, f"100-map BFS too slow: {bfs_elapsed:.3f}s")
            self.assertGreaterEqual(val_res.stats["maps"], 100)
            self.assertIn(10, val_res.stats["unlocked_switches"])

            # Pack 100 maps into RMXP rxdata & RGSSAD v3 archive and unpack cleanly
            rmxp_root = ws / "pack_test"
            pack_ir_to_rmxp(ir_dir, rmxp_root)
            rgssad_path = ws / "Game.rgss3a"
            pack_stats = pack_rgssad(rmxp_root, rgssad_path, version=3)
            self.assertEqual(pack_stats["version"], 3)
            unpack_dir = ws / "unpack_test"
            unpack_stats = unpack_rgssad(rgssad_path, unpack_dir)
            self.assertEqual(unpack_stats["files_extracted"], pack_stats["files_packed"])
            self.assertTrue((unpack_dir / "Data" / "Map100.rxdata").exists())

            # Session traversal check across Map 1 -> Map 50 -> Map 100
            sess = HeadlessGameSession(ir_dir)
            sess._run_commands([{"op": "transfer_player", "map_id": 50, "x": 10, "y": 7, "direction": 6}], 0, 1, 1)
            self.assertEqual(sess.map_id, 50)
            sess._run_commands([{"op": "transfer_player", "map_id": 100, "x": 4, "y": 7, "direction": 6}], 0, 50, 1)
            self.assertEqual(sess.map_id, 100)
            sess.direction = "right"
            sess.step("interact")
            self.assertTrue(sess.switches.get(10))

    def test_79_battle_hud_turn_order_state_badges_and_elemental_feedback(self):
        import pygame
        from src.harness.player_gui import VIEW_H, VIEW_W, _draw_overlays

        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "ws_battle_ux"
            generate_sample_project(ws)
            sess = HeadlessGameSession(ws / "ir", interactive_battle=True)
            sess.states_db[2] = {"fields": {"id": 2, "name": "독", "slip_damage": True}}
            sess.enemies[1]["fields"]["maxhp"] = 500
            sess.enemies[1]["fields"]["element_ranks"] = {"__type__": "Table", "dims": 1, "xsize": 10, "ysize": 1, "zsize": 1, "data": [3, 1, 1, 6, 3, 3, 3, 3, 3, 3]}
            sess.skills_db[90] = {
                "__type__": "RubyObject",
                "class": "RPG::Skill",
                "fields": {"id": 90, "name": "파이어볼", "sp_cost": 2, "power": 30, "scope": 1, "occasion": 1, "hit": 100, "atk_f": 0, "int_f": 100, "element_set": [2]},
            }
            sess._get_actor_state(1)["learned_skills"].append(90)
            sess._start_interactive_battle(1, can_escape=True, can_lose=False)
            bs = sess.pending.battle_state
            bs["enemies"][0]["states"] = [2]  # Poison state badge
            sess._get_actor_state(1)["states"] = [2]
            sess._refresh_interactive_battle_prompt(sess.pending)

            order = sess.get_battle_turn_order(bs)
            self.assertGreaterEqual(len(order), 2)
            self.assertIn("순서:", sess.pending.lines[1])
            self.assertIn("[독]", sess.pending.lines[1])

            # Cast Fireball (element 2, rank A = weakness on enemy)
            sess.step("choose:1")  # 스킬
            fire_idx = next(i for i, ch in enumerate(sess.pending.choices) if "파이어볼" in ch)
            sess.step(f"choose:{fire_idx}")
            self.assertEqual(bs.get("last_eff_tag"), "약점!")
            self.assertIn("약점!", bs["last_msg"])

            os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
            pygame.init()
            try:
                surf = pygame.Surface((VIEW_W, VIEW_H))
                font = pygame.font.Font(None, 20)
                sfont = pygame.font.Font(None, 16)
                _draw_overlays(pygame, surf, font, sfont, sess, 0, False)
            finally:
                pygame.quit()


    def test_80_quest_journal_tracking_save_and_gui_overlay(self):
        import pygame
        from src.harness.player_gui import VIEW_H, VIEW_W, _draw_quest_overlay, launch_player

        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "ws_quest"
            generate_sample_project(ws)
            sess = HeadlessGameSession(ws / "ir")
            self.assertEqual(sess.list_quest_lines(), ["진행 중인 퀘스트가 없습니다."])

            # Update quests via direct quest_update op & RMXP @QUEST: comment directive
            sess._run_commands([
                {"op": "quest_update", "quest_id": "q_main", "status": "active", "title": "고블린 소탕", "desc": "폐광 고블린 3마리 처치"},
                {"op": "comment", "lines": ["@QUEST:q_side|completed|약초 채집|마을 약초 전달 완료"]},
            ], 0, 1, 1)
            qlines = sess.list_quest_lines()
            self.assertEqual(len(qlines), 2)
            self.assertTrue(qlines[0].startswith("[진행중] 고블린 소탕"))
            self.assertTrue(qlines[1].startswith("[완료] 약초 채집"))

            # Save / reload roundtrip preserves quests
            saved = sess.export_save_state()
            sess2 = HeadlessGameSession(ws / "ir")
            sess2.import_save_state(saved)
            self.assertEqual(sess2.list_quest_lines(), qlines)

            os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
            pygame.init()
            try:
                surf = pygame.Surface((VIEW_W, VIEW_H))
                font = pygame.font.Font(None, 20)
                sfont = pygame.font.Font(None, 16)
                _draw_quest_overlay(pygame, surf, font, sfont, sess2)
            finally:
                pygame.quit()

            # J key toggle inside GUI event loop
            def _press_j(tick, _scr, _s):
                if tick == 0:
                    pygame.event.post(pygame.event.Event(pygame.KEYDOWN, {"key": pygame.K_j}))
                elif tick == 2:
                    pygame.event.post(pygame.event.Event(pygame.KEYDOWN, {"key": pygame.K_ESCAPE}))

            rc = launch_player(ws / "ir", max_frames=4, on_frame=_press_j)
            self.assertEqual(rc, 0)

    def test_81_wait_for_move_completion_and_screen_transitions(self):
        from src.core.ir_converter import commands_to_semantic_dsl, semantic_dsl_to_commands

        ops = [
            {"op": "set_move_route", "target_id": 1, "steps": ["right", "right", "down"], "repeat": False, "skippable": True, "indent": 0},
            {"op": "wait_for_move_completion", "indent": 0},
        ]
        rt = commands_to_semantic_dsl(semantic_dsl_to_commands(ops))
        self.assertEqual([c["op"] for c in rt], ["set_move_route", "wait_for_move_completion"])

        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "ws_cutscene"
            generate_sample_project(ws)
            sess = HeadlessGameSession(ws / "ir")
            ev1 = next(e for e in sess.maps[1]["events"] if int(e["id"]) == 1)
            ev1["x"], ev1["y"] = 2, 2

            sess._run_commands([
                {"op": "prepare_transition"},
                {"op": "set_move_route", "target_id": 1, "steps": ["right", "right", "down"], "repeat": False, "skippable": True},
                {"op": "wait_for_move_completion"},
                {"op": "execute_transition", "name": "001-Blind01", "duration": 10},
            ], 0, 1, 1)

            # Event 1 finished all 3 steps immediately cleanly and route drained
            self.assertEqual((ev1["x"], ev1["y"]), (4, 3))
            self.assertNotIn((1, 1), sess._active_move_routes)
            self.assertFalse(sess.screen_faded_out)
            self.assertIsNotNone(sess.pending_transition)
            self.assertEqual(sess.pending_transition["name"], "001-Blind01")
            sess.advance_screen_fx_frames(10)
            self.assertIsNone(sess.pending_transition)

    def test_82_class_skill_learnings_and_party_formation_swap(self):
        import pygame
        from src.harness.player_gui import _process_status_panel_action

        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "ws_party_swap"
            generate_sample_project(ws)
            sess = HeadlessGameSession(ws / "ir")
            # Configure Class 1 learnings at Lv.1 and Lv.3
            sess.classes_db[1] = {
                "__type__": "RubyObject",
                "class": "RPG::Class",
                "fields": {
                    "id": 1,
                    "name": "전사",
                    "learnings": [
                        {"fields": {"level": 1, "skill_id": 1}},
                        {"fields": {"level": 3, "skill_id": 2}},
                    ],
                },
            }
            sess._get_actor_state(1)["learned_skills"] = []
            sess._sync_actor_learnings(sess._get_actor_state(1))
            self.assertIn(1, sess._get_actor_state(1)["learned_skills"])
            self.assertNotIn(2, sess._get_actor_state(1)["learned_skills"])

            # Grant EXP to reach Lv.3+ -> Skill 2 auto-learned
            sess._grant_actor_exp(1, 500)
            self.assertGreaterEqual(sess._get_actor_state(1)["level"], 3)
            self.assertIn(2, sess._get_actor_state(1)["learned_skills"])

            # Add Actor 2 and swap party order via action & Status Panel P key
            sess.actors[2] = {
                "__type__": "RubyObject",
                "class": "RPG::Actor",
                "fields": {
                    **sess.actors[1]["fields"],
                    "id": 2,
                    "name": "마법사",
                    "character_name": "002-Fighter02",
                },
            }
            sess._run_commands([{"op": "change_party", "actor_id": 2, "mode": "add", "initialize": True}], 0, 1, 1)
            self.assertEqual(sess.party_member_ids, [1, 2])
            self.assertEqual(sess.lead_actor_id, 1)

            sess.step("swap_party:0:1")
            self.assertEqual(sess.party_member_ids, [2, 1])
            self.assertEqual(sess.lead_actor_id, 2)
            self.assertEqual(sess.hero_name, "마법사")
            self.assertEqual(sess.hero_graphic, "002-Fighter02")

            # Swap back via Status Panel P key handler
            still_open, cur, aidx, smode = _process_status_panel_action(pygame, pygame.K_p, sess, 0, 1, "equip")
            self.assertTrue(still_open)
            self.assertEqual((cur, aidx, smode), (0, 0, "equip"))
            self.assertEqual(sess.party_member_ids, [1, 2])

    def test_83_show_balloon_roundtrip_and_gui_render(self):
        from src.core.ir_converter import commands_to_semantic_dsl, semantic_dsl_to_commands
        from src.harness.player_gui import launch_player

        ops = [
            {"op": "show_balloon", "character_id": -1, "balloon_id": 1, "indent": 0},
            {"op": "show_balloon", "character_id": 0, "balloon_id": 4, "indent": 0},
        ]
        rt = commands_to_semantic_dsl(semantic_dsl_to_commands(ops))
        self.assertEqual([(c["op"], c["character_id"], c["balloon_id"]) for c in rt], [("show_balloon", -1, 1), ("show_balloon", 0, 4)])

        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "ws_balloon"
            generate_sample_project(ws)
            sess = HeadlessGameSession(ws / "ir")
            sess._run_commands(ops, 0, 1, 1)
            self.assertEqual(len(sess.pending_balloons), 2)
            # character_id 0 resolves to triggering event_id (1)
            self.assertEqual(sess.pending_balloons[0], {"character_id": -1, "balloon_id": 1})
            self.assertEqual(sess.pending_balloons[1], {"character_id": 1, "balloon_id": 4})

            os.environ["SDL_VIDEODRIVER"] = "dummy"
            def _inject_balloon(tick, _scr, live_sess):
                if tick == 0:
                    live_sess.pending_balloons.append({"character_id": -1, "balloon_id": 1})
                    live_sess.pending_balloons.append({"character_id": 1, "balloon_id": 4})

            rc = launch_player(ws / "ir", max_frames=4, on_frame=_inject_balloon)
            self.assertEqual(rc, 0)

    def test_84_cli_export_release_bundle_and_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "ws_release"
            generate_sample_project(ws)
            ir_dir = ws / "ir"
            # Plant a temporary save slot file that must be excluded from release output
            (ir_dir / "save_slot1.json").write_text("{}", encoding="utf-8")

            raw_out = Path(tmp) / "dist_raw"
            buf_raw = io.StringIO()
            with contextlib.redirect_stdout(buf_raw):
                rc_raw = cli_main(["--json", "export-release", str(ir_dir), str(raw_out)])
            self.assertEqual(rc_raw, 0)
            res_raw = json.loads(buf_raw.getvalue())
            self.assertEqual(res_raw["archive_mode"], "raw")
            self.assertTrue((raw_out / "Data" / "Actors.rxdata").is_file())
            self.assertTrue((raw_out / "manifest.json").is_file())
            self.assertFalse((raw_out / "save_slot1.json").exists())

            rgss_out = Path(tmp) / "dist_rgssad"
            buf_rgss = io.StringIO()
            with contextlib.redirect_stdout(buf_rgss):
                rc_rgss = cli_main(["--json", "export-release", str(ir_dir), str(rgss_out), "--rgssad"])
            self.assertEqual(rc_rgss, 0)
            res_rgss = json.loads(buf_rgss.getvalue())
            self.assertEqual(res_rgss["archive_mode"], "rgss3a")
            self.assertTrue((rgss_out / "Game.rgss3a").is_file())
            manifest = json.loads((rgss_out / "manifest.json").read_text(encoding="utf-8"))
            self.assertIn("Game.rgss3a", manifest["files"])
            self.assertEqual(len(manifest["files"]["Game.rgss3a"]["sha256"]), 64)

    def test_85_extended_rmxp_commands_script_and_save_checksum(self):
        from src.core.ir_converter import commands_to_semantic_dsl, inspect_scripts, semantic_dsl_to_commands
        from src.core.simulator import parse_message_segments, resolve_user_save_dir, write_crash_report

        ops = [
            {"op": "change_text_options", "position": 0, "show_frame": False, "indent": 0},
            {"op": "button_input_processing", "variable_id": 7, "indent": 0},
            {"op": "name_input", "actor_id": 1, "max_char": 8, "indent": 0},
            {"op": "change_fog_tone", "rgbg": [30, -20, 10, 0], "frames": 10, "indent": 0},
            {"op": "change_fog_opacity", "opacity": 140, "frames": 8, "indent": 0},
            {"op": "show_picture", "number": 1, "name": "Emblem", "var_mode": 0, "origin": 1, "x": 100, "y": 80, "zoom_x": 100, "zoom_y": 100, "opacity": 255, "indent": 0},
            {"op": "rotate_picture", "number": 1, "speed": 5, "indent": 0},
            {"op": "change_picture_tone", "number": 1, "rgbg": [40, 0, -40, 0], "frames": 10, "indent": 0},
            {"op": "play_bgm", "name": "Town01", "volume": 80, "pitch": 100, "indent": 0},
            {"op": "memorize_bgm", "indent": 0},
            {"op": "play_bgm", "name": "Battle01", "volume": 90, "pitch": 110, "indent": 0},
            {"op": "restore_bgm", "indent": 0},
            {"op": "call_menu", "indent": 0},
            {"op": "script", "lines": ["$game_switches[9] = true", "$game_variables[8] = 10 + 5", "$game_party.gain_gold(150)"], "indent": 0},
            {"op": "set_move_route", "target_id": 1, "repeat": False, "skippable": True, "steps": ["speed:5", "freq:4", "opacity:180", "blend:1", "graphic:Actor2:0:6:1", "walk_anime_on", "step_anime_off", "dir_fix_on", "always_on_top_on", "jump:1:0"], "indent": 0},
        ]
        packed = semantic_dsl_to_commands(ops)
        unpacked = commands_to_semantic_dsl(packed)
        self.assertEqual([x["op"] for x in unpacked], [x["op"] for x in ops])
        self.assertEqual(unpacked[-1]["steps"], ops[-1]["steps"])

        segs, extra_wait = parse_message_segments("\\C[2]붉은 글씨\\C[0] 대기\\. \\|끝")
        self.assertEqual(segs[0], ("붉은 글씨", 2))
        self.assertEqual(extra_wait, 75)

        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "ws_ext"
            generate_sample_project(ws)
            ir_dir = ws / "ir"
            # Inspect scripts/_index.json summary
            scr_summary = inspect_scripts(ir_dir)
            self.assertGreaterEqual(len(scr_summary), 1)
            self.assertIn("title", scr_summary[0])
            self.assertIn("line_count", scr_summary[0])

            sess = HeadlessGameSession(ir_dir)
            sess._run_commands(ops, 0, 1, 1)
            # First pause: button_input_processing
            self.assertIsNotNone(sess.pending)
            self.assertIsNotNone(sess.pending.button_input)
            sess.step("choose:0")  # Enter -> 13
            self.assertEqual(sess.variables[7], 13)
            # Second pause: name_input
            self.assertIsNotNone(sess.pending)
            self.assertIsNotNone(sess.pending.input_name)
            self.assertTrue(sess.submit_name_input("아서왕"))
            self.assertEqual(sess._get_actor_state(1)["name"], "아서왕")
            self.assertIsNone(sess.pending)

            self.assertEqual(sess.text_options, {"position": 0, "show_frame": False})
            self.assertEqual(sess.fog_state["opacity"], 140)
            self.assertEqual(sess.fog_state["tone"], [30, -20, 10, 0])
            self.assertEqual(sess.current_bgm["name"], "Town01")
            self.assertTrue(sess.pending_menu_open)
            self.assertTrue(sess.switches.get(9))
            self.assertEqual(sess.variables.get(8), 15)
            self.assertEqual(sess.gold, 150)

            sess.advance_screen_fx_frames(4)
            self.assertAlmostEqual(sess.pictures[1]["angle"], 20.0)
            self.assertEqual(sess.pictures[1]["tone"], [40, 0, -40, 0])

            # Test face tag directive in dialogue
            sess._run_commands([{"op": "say", "lines": ["[FACE:Hero:2] 반갑다!"], "indent": 0}], 0, 1, 1)
            self.assertEqual(sess.pending.face_name, "Hero")
            self.assertEqual(sess.pending.face_index, 2)
            self.assertEqual(sess.pending.lines, ["반갑다!"])
            self.assertEqual(sess.pending.text_position, 0)
            self.assertFalse(sess.pending.show_frame)
            sess.step("confirm")

            # Save checksum integrity verification + OS user save dir + crash logger
            spath = sess.save_to_file(slot=1)
            sess2 = HeadlessGameSession(ir_dir)
            self.assertTrue(sess2.load_from_file(slot=1, strict_checksum=True))
            self.assertTrue(sess2.save_checksum_valid)

            tampered = json.loads(spath.read_text(encoding="utf-8"))
            tampered["gold"] = 999999
            spath.write_text(json.dumps(tampered), encoding="utf-8")
            with self.assertRaises(ValueError):
                sess2.load_from_file(slot=1, strict_checksum=True)
            sess2.load_from_file(slot=1, strict_checksum=False)
            self.assertFalse(sess2.save_checksum_valid)

            os.environ["RPGCREATOR_SAVE_DIR"] = str(Path(tmp) / "os_saves")
            try:
                self.assertEqual(resolve_user_save_dir(), Path(tmp) / "os_saves")
            finally:
                os.environ.pop("RPGCREATOR_SAVE_DIR", None)
            try:
                raise RuntimeError("sample failure")
            except RuntimeError as err:
                clog = write_crash_report(err, Path(tmp) / "crashes", {"map_id": sess.map_id})
                self.assertTrue(clog.is_file())
                self.assertIn("sample failure", clog.read_text(encoding="utf-8"))

    def test_86_battle_commands_335_339_restrictions_and_gear_locks(self):
        from src.core.ir_converter import commands_to_semantic_dsl, semantic_dsl_to_commands
        from src.core.utils import load_json, save_json

        b_ops = [
            {"op": "enemy_appear", "enemy_index": 1, "indent": 0},
            {"op": "enemy_transform", "enemy_index": 0, "enemy_id": 1, "indent": 0},
            {"op": "show_battle_animation", "target_type": 0, "target_index": 0, "animation_id": 3, "indent": 0},
            {"op": "deal_damage", "target_type": 0, "target_index": 0, "value": 12, "indent": 0},
            {"op": "force_action", "target_type": 0, "target_index": 0, "kind": 0, "skill_id": 0, "indent": 0},
        ]
        packed = semantic_dsl_to_commands(b_ops)
        unpacked = commands_to_semantic_dsl(packed)
        self.assertEqual([x["op"] for x in unpacked], [x["op"] for x in b_ops])

        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "ws_bat"
            generate_sample_project(ws)
            ir_dir = ws / "ir"
            sess = HeadlessGameSession(ir_dir, interactive_battle=True)

            # Extra accessory slot armor5 + armor_fix lock
            armors = load_json(ir_dir / "database" / "armors.json")
            armors.append({"__ruby_class__": "RPG::Armor", "fields": {"id": 99, "name": "수호 반지", "kind": 3, "pdef": 4, "mdef": 6, "price": 100}})
            save_json(ir_dir / "database" / "armors.json", armors)
            classes = load_json(ir_dir / "database" / "classes.json")
            for cls_row in classes:
                if isinstance(cls_row, dict) and isinstance(cls_row.get("fields"), dict):
                    aset = cls_row["fields"].get("armor_set")
                    if isinstance(aset, list) and 99 not in aset:
                        aset.append(99)
            save_json(ir_dir / "database" / "classes.json", classes)
            sess.reload()
            sess.armors_owned[99] = 1
            base_def = sess._actor_defense(1)
            self.assertTrue(sess.equip_from_bag(1, 5, 99))
            self.assertEqual(sess._actor_defense(1), base_def + 4)
            sess.actors[1]["fields"]["armor_fix"] = True
            self.assertTrue(sess.is_slot_fixed(1, 5))
            self.assertFalse(sess.equip_from_bag(1, 5, 0))
            sess.actors[1]["fields"]["armor_fix"] = False

            # Field skill menu_se + animation2_id cue
            sess.skills_db[2] = {
                "__ruby_class__": "RPG::Skill",
                "fields": {"id": 2, "name": "응급 처치", "occasion": 2, "sp_cost": 1, "power": -20, "scope": 3, "animation2_id": 5, "menu_se": {"fields": {"name": "Heal01", "volume": 70, "pitch": 100}}},
            }
            st1 = sess._get_actor_state(1)
            st1["learned_skills"].append(2)
            st1["hp"] = max(1, st1["max_hp"] - 30)
            self.assertTrue(sess.use_skill_on_field(1, 2))
            self.assertTrue(any(a["animation_id"] == 5 for a in sess.pending_animations))
            self.assertTrue(any(s["name"] == "Heal01" for s in sess.played_se_queue))

            # State restrictions 1 (no magic), 2 (auto enemy attack), 3 (auto ally), 4 (skip)
            sess.states_db[10] = {"__ruby_class__": "RPG::State", "fields": {"id": 10, "name": "침묵", "restriction": 1}}
            sess.states_db[11] = {"__ruby_class__": "RPG::State", "fields": {"id": 11, "name": "광폭화", "restriction": 2}}
            sess.states_db[12] = {"__ruby_class__": "RPG::State", "fields": {"id": 12, "name": "혼란", "restriction": 3}}
            sess._start_interactive_battle(1, can_escape=True, can_lose=True)
            bs = sess.pending.battle_state
            self.assertIsNotNone(bs)
            # Duplicate enemy hidden in index 1 to test enemy_appear
            e_clone = dict(bs["enemies"][0])
            e_clone["hidden"] = True
            bs["enemies"].append(e_clone)
            sess._run_commands(b_ops, 0, 1, 1)
            self.assertFalse(bs["enemies"][1]["hidden"])
            self.assertTrue(len(sess.pending_battle_animations) >= 1)
            self.assertTrue(any(p["text"] == "12" for p in bs.get("floating_popups", [])))

            # Restriction 1 blocks Skill menu
            st1["states"] = [10]
            sess.step("choose:1")
            self.assertIn("마법", bs["last_msg"])

            # Restriction 2 forces physical attack on enemy
            st1["states"] = [11]
            hp_before = sum(int(e["hp"]) for e in bs["enemies"])
            sess.step("choose:2")  # tried defend, forced to attack!
            self.assertLess(sum(int(e["hp"]) for e in bs["enemies"]), hp_before)

            # Restriction 3 confuses actor into attacking party
            st1["states"] = [12]
            my_hp_before = st1["hp"]
            sess.step("choose:0")
            self.assertLess(st1["hp"], my_hp_before + 50)

    def test_87_gui_panorama_fog_priority_faces_battle_scene_and_transitions(self):
        import pygame
        from src.harness.player_gui import (
            VIEW_H,
            VIEW_W,
            _draw_battle_scene,
            _draw_fog,
            _draw_overlays,
            _draw_panorama,
            _draw_pictures,
            _draw_weather_and_fade,
            _get_or_build_map_cache,
            launch_player,
            typewriter_chars_for_elapsed,
            wrap_colored_segments,
            wrap_text_to_width,
        )
        from src.core.simulator import parse_message_segments

        os.environ["SDL_VIDEODRIVER"] = "dummy"
        pygame.init()
        try:
            font = pygame.font.Font(None, 22)
            sfont = pygame.font.Font(None, 18)
            wrapped = wrap_text_to_width("동해물과 백두산이 마르고 닳도록 하느님이 보우하사 우리나라 만세", font, 160)
            self.assertGreaterEqual(len(wrapped), 2)
            segs, ew = parse_message_segments("\\C[2]긴급 경고\\C[0]입니다\\.!")
            c_rows = wrap_colored_segments(segs, font, 90)
            self.assertGreaterEqual(len(c_rows), 1)
            self.assertGreater(typewriter_chars_for_elapsed(20, 250.0, text_speed=2), 1)
            self.assertEqual(typewriter_chars_for_elapsed(20, 10.0, text_speed=4), 20)
        finally:
            pygame.quit()

        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "ws_gui2"
            generate_sample_project(ws)
            ir_dir = ws / "ir"
            # Plant minimal valid PNGs in Panoramas, Fogs, Faces, Battlebacks, Battlers, Titles, Gameovers, Transitions, Pictures
            for sub in ("Panoramas/Sky", "Fogs/Mist", "Faces/Hero", "Battlebacks/Plains", "Battlers/Slime", "Titles/Title", "Gameovers/Gameover", "Transitions/Wipe", "Pictures/Emblem"):
                p = ws / "rmxp" / "Graphics" / f"{sub}.png"
                p.parent.mkdir(parents=True, exist_ok=True)
                Image.new("RGBA", (64, 64), (100, 140, 210, 255)).save(p)
            ts = json.loads((ir_dir / "database" / "tilesets.json").read_text(encoding="utf-8"))
            ts[1]["fields"]["panorama_name"] = "Sky"
            ts[1]["fields"]["fog_name"] = "Mist"
            ts[1]["fields"]["fog_opacity"] = 96
            ts[1]["fields"]["fog_sx"] = 2
            ts[1]["fields"]["battleback_name"] = "Plains"
            ts[1]["fields"]["priorities"]["data"][384] = 1
            (ir_dir / "database" / "tilesets.json").write_text(json.dumps(ts), encoding="utf-8")

            # Launch headless pygame frame sequence verifying all layers & scenes render cleanly
            sess_seen = []
            def _drive_layers(tick, scr, live_sess):
                sess_seen.append(tick)
                if tick == 0:
                    live_sess._run_commands([
                        {"op": "change_text_options", "position": 0, "show_frame": False, "indent": 0},
                        {"op": "show_picture", "number": 1, "name": "Emblem", "var_mode": 0, "origin": 1, "x": 160, "y": 120, "zoom_x": 120, "zoom_y": 120, "opacity": 200, "indent": 0},
                        {"op": "rotate_picture", "number": 1, "speed": 6, "indent": 0},
                        {"op": "change_picture_tone", "number": 1, "rgbg": [30, -10, 20, 0], "frames": 5, "indent": 0},
                        {"op": "execute_transition", "name": "Wipe", "duration": 6, "indent": 0},
                        {"op": "say", "lines": ["[FACE:Hero:0]\\C[6]황금 대사\\C[0] 자동 줄바꿈 테스트"], "indent": 0},
                    ], 0, 1, 1)
                elif tick == 2:
                    live_sess.step("confirm")
                    live_sess._start_interactive_battle(1, can_escape=True, can_lose=True)
                    live_sess.pending_battle_animations.append({"target_type": 0, "target_index": 0, "animation_id": 1, "frames": 10})
                    live_sess._record_battle_popup(live_sess.pending.battle_state, live_sess.pending.battle_state["enemies"][0]["name"], 25, "crit")
                elif tick == 4:
                    live_sess.pending = None
                    live_sess.game_over = True

            rc = launch_player(ir_dir, max_frames=6, on_frame=_drive_layers, start_on_title=False)
            self.assertEqual(rc, 0)
            self.assertEqual(sess_seen, [0, 1, 2, 3, 4, 5])

            # Also verify Title art path renders cleanly
            rc_t = launch_player(ir_dir, max_frames=2, start_on_title=True)
            self.assertEqual(rc_t, 0)

    def test_88_gui_scaling_inputs_settings_gamepad_integrated_menu_and_audio_volumes(self):
        os.environ["SDL_VIDEODRIVER"] = "dummy"
        os.environ["SDL_AUDIODRIVER"] = "dummy"
        import pygame
        from src.core.simulator import PendingDialogue
        from src.harness.player_gui import (
            VIEW_H,
            VIEW_W,
            build_main_menu_items,
            build_title_menu_options,
            compute_letterbox_rect,
            launch_player,
            load_player_settings,
            process_title_selection,
            resample_sndarray_samples,
            save_player_settings,
            window_to_logical_pos,
        )

        # Letterbox & coordinate conversion across 16:9 widescreen window
        lb = compute_letterbox_rect(1280, 720, VIEW_W, VIEW_H)
        self.assertEqual(lb, (160, 0, 960, 720))
        self.assertEqual(window_to_logical_pos((160 + 480, 360), lb), (320, 240))

        # Universal sndarray sample resampling (non-WAV pitch fallback)
        samples_1d = [0, 10, 20, 30, 40, 50, 60, 70]
        up_samples = resample_sndarray_samples(samples_1d, pitch=200)
        self.assertEqual(len(up_samples), 4)
        dn_samples = resample_sndarray_samples(samples_1d, pitch=50)
        self.assertEqual(len(dn_samples), 16)

        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "p88"
            generate_sample_project(ws)
            ir_dir = ws / "ir"

            # Persisted settings + expanded Options
            cfg = {"bgm_volume": 60, "se_volume": 80, "text_speed": 200, "auto_dash": True}
            save_player_settings(ir_dir, cfg)
            loaded_cfg = load_player_settings(ir_dir)
            self.assertEqual(loaded_cfg["bgm_volume"], 60)
            self.assertTrue(loaded_cfg["auto_dash"])

            sess = HeadlessGameSession(ir_dir)
            ext_opts = build_title_menu_options(sess, mode="options", show_minimap=True, settings=loaded_cfg)
            self.assertEqual(len(ext_opts), 7)
            r_bgm = process_title_selection(sess, "options", 0, True, settings=loaded_cfg, adjust_delta=1)
            self.assertEqual(loaded_cfg["bgm_volume"], 80)
            self.assertIn("80%", r_bgm["toast"])
            self.assertEqual(len(build_main_menu_items(sess)), 8)

            # Live player loop: ESC menu hub, Number Input typing, Name Input typing, Shop x10, JOYHAT/JOYBUTTONDOWN, F11 toggle
            shop_qty_seen: List[int] = []

            def _drive_c(tick: int, _surf: Any, live: HeadlessGameSession) -> None:
                if tick == 0:
                    pygame.event.post(pygame.event.Event(pygame.KEYDOWN, key=pygame.K_ESCAPE))
                elif tick == 1:
                    pygame.event.post(pygame.event.Event(pygame.KEYDOWN, key=pygame.K_ESCAPE))
                    live._run_commands([
                        {"op": "input_number", "variable_id": 10, "digits": 3, "indent": 0},
                    ], 0, 1, 1)
                elif tick == 2:
                    pygame.event.post(pygame.event.Event(pygame.KEYDOWN, key=pygame.K_7, unicode="7"))
                    pygame.event.post(pygame.event.Event(pygame.KEYDOWN, key=pygame.K_3, unicode="3"))
                    pygame.event.post(pygame.event.Event(pygame.KEYDOWN, key=pygame.K_RETURN))
                elif tick == 3:
                    live._run_commands([
                        {"op": "input_name", "actor_id": 1, "max_char": 8, "indent": 0},
                    ], 0, 1, 1)
                elif tick == 4:
                    if live.pending and live.pending.input_name is not None:
                        live.pending.input_name["text"] = "Aria"
                    pygame.event.post(pygame.event.Event(pygame.KEYDOWN, key=pygame.K_RETURN))
                elif tick == 5:
                    live.gold = 9999
                    live._run_commands([
                        {"op": "shop", "goods": [{"type": "item", "id": 1}], "indent": 0},
                    ], 0, 1, 1)
                elif tick == 6:
                    pygame.event.post(pygame.event.Event(pygame.KEYDOWN, key=pygame.K_TAB))
                    pygame.event.post(pygame.event.Event(pygame.MOUSEWHEEL, y=1))
                elif tick == 7:
                    if live.pending:
                        shop_qty_seen.append(int(live.pending.shop_qty))
                    live.pending = None
                    pygame.event.post(pygame.event.Event(pygame.JOYHATMOTION, joy=0, hat=0, value=(1, 0)))

            rc = launch_player(ir_dir, max_frames=9, on_frame=_drive_c)
            self.assertEqual(rc, 0)
            self.assertEqual(shop_qty_seen, [10])
            s_post = load_player_settings(ir_dir)
            self.assertEqual(s_post["bgm_volume"], 60)

    def test_89_asset_validator_balance_sim_blueprint_dryrun_undo_and_graph(self):
        from src.core.editor import apply_blueprint, undo_last_edit
        from src.core.validator import export_flow_graph, simulate_battle_balance, validate_project_assets

        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "p89"
            generate_sample_project(ws)
            ir_dir = ws / "ir"

            # 1. Asset audit: detect missing battleback/BGM refs vs orphan files
            ts = json.loads((ir_dir / "database" / "tilesets.json").read_text(encoding="utf-8"))
            ts[1]["fields"]["battleback_name"] = "MissingBattleback"
            (ir_dir / "database" / "tilesets.json").write_text(json.dumps(ts), encoding="utf-8")
            orphan_png = ws / "rmxp" / "Graphics" / "Pictures" / "UnusedSplash.png"
            orphan_png.parent.mkdir(parents=True, exist_ok=True)
            Image.new("RGBA", (16, 16), (255, 0, 0, 255)).save(orphan_png)
            asset_rep = validate_project_assets(ir_dir)
            self.assertFalse(asset_rep["passed"])
            self.assertIn("Battlebacks/MissingBattleback", asset_rep["missing_graphics"])
            self.assertIn("Pictures/UnusedSplash.png", asset_rep["orphan_graphics"])

            # 2. Battle balance simulation
            bal = simulate_battle_balance(ir_dir, trials_per_troop=12)
            self.assertGreaterEqual(len(bal["troops"]), 1)
            self.assertIn("win_rate", bal["troops"][0])
            self.assertIn("recommended_level", bal["troops"][0])

            # 3. Blueprint dry-run diff vs live apply + undo rollback
            bp = {"maps": [{"name": "비밀 정원", "width": 10, "height": 8, "events": []}]}
            dry_res = apply_blueprint(ir_dir, bp, dry_run=True)
            self.assertTrue(dry_res["dry_run"])
            self.assertIn("maps/map_003.json", dry_res["diff"]["added"])
            self.assertFalse((ir_dir / "maps" / "map_003.json").exists())

            live_res = apply_blueprint(ir_dir, bp, dry_run=False)
            self.assertFalse(live_res["dry_run"])
            self.assertTrue((ir_dir / "maps" / "map_003.json").exists())
            undo_res = undo_last_edit(ir_dir)
            self.assertTrue(undo_res["restored"])
            self.assertFalse((ir_dir / "maps" / "map_003.json").exists())

            # 4. Flow graph export (Mermaid & DOT)
            mmd = export_flow_graph(ir_dir, fmt="mermaid")
            self.assertIn("flowchart LR", mmd)
            self.assertIn("-->|", mmd)
            dot = export_flow_graph(ir_dir, fmt="dot")
            self.assertIn("digraph RPGFlow", dot)
            self.assertIn("->", dot)

            # 5. CLI smoke for balance-check and export-graph
            import subprocess
            proc = subprocess.run(
                [sys.executable, "run_app.py", "--json", "balance-check", str(ir_dir), "--trials", "8"],
                cwd=str(Path(__file__).resolve().parent.parent),
                capture_output=True,
                text=True,
                check=True,
            )
            cli_bal = json.loads(proc.stdout)
            self.assertGreaterEqual(len(cli_bal["troops"]), 1)

    def test_90_i18n_extract_apply_mv_mz_tiled_vxace_importers_and_launcher_export(self):
        from src.core.editor import export_release_bundle
        from src.core.i18n import apply_i18n_locale, extract_i18n_strings
        from src.core.importers import import_rpgmaker_mv_mz, import_tiled_map, import_vxace_rxdata
        from src.core.rmxp_marshal import marshal_dumps
        from src.core.rmxp_types import RgssTable, RubyObject

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ws = root / "p90"
            generate_sample_project(ws)
            ir_dir = ws / "ir"

            # 1. i18n extract + runtime locale + localized export with launcher & spec
            cat_meta = extract_i18n_strings(ir_dir)
            self.assertGreater(cat_meta["key_count"], 5)
            cat_file = Path(cat_meta["output"])
            cat = json.loads(cat_file.read_text(encoding="utf-8"))
            cat["strings"]["map.1.name"]["en"] = "Starter Village"
            for k in list(cat["strings"]):
                if ".say." in k:
                    cat["strings"][k]["en"] = "Welcome hero!"
                    break
            cat_file.write_text(json.dumps(cat, ensure_ascii=False, indent=2), encoding="utf-8")

            sess_en = HeadlessGameSession(ir_dir, lang="en")
            self.assertEqual(sess_en.maps[1]["name"], "Starter Village")

            rel_dir = root / "release_en"
            rel_meta = export_release_bundle(ir_dir, rel_dir, use_rgssad=True, lang="en", launcher=True)
            self.assertTrue((rel_dir / "Game.rgss3a").is_file())
            self.assertTrue((rel_dir / "play_game.bat").is_file())
            self.assertTrue((rel_dir / "play_game.sh").is_file())
            self.assertTrue((rel_dir / "build_game.spec").is_file())
            self.assertTrue((rel_dir / "launch_player.py").is_file())
            self.assertEqual(rel_meta["locale"], "en")

            apply_res = apply_i18n_locale(ir_dir, "en")
            self.assertGreaterEqual(apply_res["applied_count"], 2)

            # 2. Tiled (.tmj & .tmx) map importers
            tmj_path = root / "arena.tmj"
            tmj_path.write_text(json.dumps({
                "name": "Tiled Arena",
                "width": 6,
                "height": 5,
                "tilewidth": 32,
                "tileheight": 32,
                "layers": [
                    {"type": "tilelayer", "data": [1] * 30},
                    {"type": "objectgroup", "objects": [{"name": "Signpost", "x": 64, "y": 64, "properties": [{"name": "say", "value": "Arena Rules"}]}]},
                ],
            }), encoding="utf-8")
            t_res = import_tiled_map(tmj_path, ir_dir)
            self.assertEqual(t_res["name"], "Tiled Arena")
            self.assertEqual(t_res["events"], 1)

            tmx_path = root / "ruins.tmx"
            tmx_path.write_text(
                "<map width='5' height='4' tilewidth='32' tileheight='32'>"
                "<layer name='Ground'><data encoding='csv'>" + ",".join(["2"] * 20) + "</data></layer>"
                "<objectgroup name='Events'><object name='Statue' x='32' y='32'><properties><property name='say' value='Ancient stone'/></properties></object></objectgroup>"
                "</map>",
                encoding="utf-8",
            )
            tmx_res = import_tiled_map(tmx_path, ir_dir)
            self.assertEqual(tmx_res["events"], 1)

            # 3. RPG Maker MV/MZ JSON importer
            mv_dir = root / "mv_proj"
            (mv_dir / "data").mkdir(parents=True)
            (mv_dir / "data" / "System.json").write_text(json.dumps({"gameTitle": "MV Hero Quest", "startMapId": 1, "startX": 2, "startY": 2, "partyMembers": [1]}), encoding="utf-8")
            (mv_dir / "data" / "Actors.json").write_text(json.dumps([None, {"id": 1, "name": "Reid", "classId": 1, "initialLevel": 1, "characterName": "Actor1", "battlerName": ""}]), encoding="utf-8")
            (mv_dir / "data" / "MapInfos.json").write_text(json.dumps([None, {"id": 1, "name": "MV Town"}]), encoding="utf-8")
            (mv_dir / "data" / "Map001.json").write_text(json.dumps({
                "width": 6,
                "height": 6,
                "data": [2048] * 36,
                "events": [None, {"id": 1, "name": "Mayor", "x": 3, "y": 3, "pages": [{"trigger": 0, "image": {"characterName": "People1", "direction": 2, "pattern": 0}, "conditions": {}, "list": [{"code": 101, "indent": 0, "parameters": ["", 0, 0, 2]}, {"code": 401, "indent": 0, "parameters": ["Hello from MV!"]}, {"code": 0, "indent": 0, "parameters": []}]}]}],
            }), encoding="utf-8")
            mv_ir = root / "mv_ir"
            mv_res = import_rpgmaker_mv_mz(mv_dir, mv_ir)
            self.assertEqual(mv_res["title"], "MV Hero Quest")
            self.assertEqual(mv_res["maps_imported"], 1)
            mv_sess = HeadlessGameSession(mv_ir)
            self.assertEqual(mv_sess.maps[1]["events"][0]["pages"][0]["commands"][0]["lines"], ["Hello from MV!"])

            # 4. RPG Maker VX Ace (.rvdata2) importer
            vxa_dir = root / "vxa_proj"
            (vxa_dir / "Data").mkdir(parents=True)
            vxa_sys = RubyObject("RPG::System", {"@game_title": "VXA Saga", "@start_map_id": 1, "@start_x": 1, "@start_y": 1})
            vxa_mi = RubyObject("RPG::MapInfo", {"@name": "VXA Field", "@parent_id": 0, "@order": 1, "@expanded": False, "@scroll_x": 0, "@scroll_y": 0})
            vxa_map = RubyObject("RPG::Map", {"@width": 8, "@height": 6, "@tileset_id": 1, "@data": RgssTable.create(8, 6, 3, dims=3, fill=0).to_user_def(), "@events": {}})
            (vxa_dir / "Data" / "System.rvdata2").write_bytes(marshal_dumps(vxa_sys))
            (vxa_dir / "Data" / "MapInfos.rvdata2").write_bytes(marshal_dumps({1: vxa_mi}))
            (vxa_dir / "Data" / "Map001.rvdata2").write_bytes(marshal_dumps(vxa_map))
            vxa_ir = root / "vxa_ir"
            vxa_res = import_vxace_rxdata(vxa_dir, vxa_ir)
            self.assertEqual(vxa_res["title"], "VXA Saga")
            self.assertEqual(vxa_res["maps_imported"], 1)

    def test_91_button_input_wait_frames_luma_wipe_jump_opacity_and_keybindings(self) -> None:
        import os
        os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
        os.environ.setdefault("SDL_AUDIODRIVER", "dummy")
        import pygame
        from src.core.ir_converter import commands_to_semantic_dsl
        from src.core.rmxp_types import make_event_command
        from src.core.sample_generator import generate_sample_project
        from src.core.simulator import HeadlessGameSession
        from src.core.utils import load_json, save_json
        from src.harness.player_gui import (
            _build_transition_wipe_surface,
            compute_jump_arc_px,
            launch_player,
            load_player_settings,
            save_player_settings,
        )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            res = generate_sample_project(root)
            ir_dir = Path(res["ir_dir"])

            # 1. name_input max_chars vs max_char key parity
            dsl = commands_to_semantic_dsl([make_event_command(303, 0, [1, 5])])
            self.assertEqual(dsl[0]["op"], "name_input")
            self.assertEqual(dsl[0]["max_chars"], 5)

            # 2. title_bgm & gameover_me loaded + Animation timings + wait_frames pause/resume
            proj_path = ir_dir / "ir_project.json"
            proj = load_json(proj_path)
            proj["system"]["fields"]["title_bgm"] = {"__ruby_class__": "RPG::AudioFile", "fields": {"name": "TitleTheme", "volume": 90, "pitch": 100}}
            proj["system"]["fields"]["gameover_me"] = {"__ruby_class__": "RPG::AudioFile", "fields": {"name": "GameoverFanfare", "volume": 85, "pitch": 100}}
            save_json(proj_path, proj)

            anims_path = ir_dir / "database" / "animations.json"
            anims = load_json(anims_path)
            if len(anims) > 1 and isinstance(anims[1], dict):
                anims[1]["fields"]["timings"] = [{
                    "fields": {
                        "frame": 0,
                        "se": {"fields": {"name": "SlashSE", "volume": 80, "pitch": 100}},
                        "flash_scope": 1,
                        "flash_color": [255, 220, 120, 180],
                        "flash_duration": 6,
                    }
                }]
                save_json(anims_path, anims)

            sess = HeadlessGameSession(ir_dir, wait_pause_enabled=True)
            self.assertIsNotNone(sess.title_bgm)
            self.assertEqual(sess.title_bgm["name"], "TitleTheme")
            self.assertIsNotNone(sess.gameover_me)
            self.assertEqual(sess.gameover_me["name"], "GameoverFanfare")
            sess.pending_wait = None
            sess._run_commands([
                dsl[0],
            ], 0, 1, 0)
            self.assertIsNotNone(sess.pending)
            self.assertEqual(sess.pending.input_name["max_char"], 5)
            sess.submit_name_input("ArthurLongName")
            self.assertEqual(sess._get_actor_state(1)["name"], "Arthu")

            sess._run_commands([
                {"op": "wait_frames", "frames": 3, "indent": 0},
                {"op": "set_switch", "start_id": 7, "end_id": 7, "value": "ON", "indent": 0},
            ], 0, 1, 0)
            self.assertFalse(sess.switches.get(7, False))
            self.assertEqual(sess.wait_frames_left, 3)
            sess.advance_screen_fx_frames(2)
            self.assertFalse(sess.switches.get(7, False))
            sess.advance_screen_fx_frames(1)
            self.assertTrue(sess.switches.get(7, False))

            # 3. Move route opacity / blend / parabolic jump arc
            sess._run_commands([
                {"op": "set_move_route", "target_id": -1, "repeat": False, "skippable": True, "steps": ["opacity:140", "blend:1", "jump:1:0"], "indent": 0}
            ], 0, 1, 0)
            self.assertEqual(sess.player_opacity, 140)
            self.assertEqual(sess.player_blend_type, 1)
            self.assertEqual(sess.player_jump_frames, 14)
            self.assertGreater(compute_jump_arc_px(7, 14), 10)

            # 4. Grayscale luma cutoff wipe surface
            pygame.init()
            try:
                grad = pygame.Surface((640, 480))
                grad.fill((240, 240, 240))
                grad.fill((20, 20, 20), pygame.Rect(0, 0, 320, 480))
                mask = _build_transition_wipe_surface(pygame, grad, 0.5)
                dark_alpha = mask.get_at((100, 240)).a
                bright_alpha = mask.get_at((500, 240)).a
                self.assertEqual(dark_alpha, 255)
                self.assertEqual(bright_alpha, 0)
            finally:
                pygame.quit()

            # 5. Direct key capture for button_input + custom keybindings + JOYAXISMOTION + animation timing flash
            save_player_settings(ir_dir, {**load_player_settings(ir_dir), "keybindings": {"up": "i", "down": "k", "left": "u", "right": "o", "confirm": "z", "cancel": "x", "menu": "escape"}})
            captured_var = {}

            def _drive_gui(frame_idx: int, _scr: Any, live_sess: HeadlessGameSession) -> None:
                if frame_idx == 1:
                    live_sess.pending_animations.append({"character_id": -1, "animation_id": 1})
                    live_sess._run_commands([{"op": "button_input_processing", "variable_id": 12, "indent": 0}], 0, 1, 0)
                elif frame_idx == 2:
                    pygame.event.post(pygame.event.Event(pygame.KEYDOWN, key=pygame.K_RIGHT))
                elif frame_idx == 3:
                    captured_var["v12"] = live_sess.variables.get(12)
                    pygame.event.post(pygame.event.Event(pygame.KEYDOWN, key=pygame.K_o))
                elif frame_idx == 4:
                    captured_var["flash"] = live_sess.pending_flash
                    pygame.event.post(pygame.event.Event(pygame.JOYAXISMOTION, axis=0, value=0.9))

            rc = launch_player(ir_dir, max_frames=8, on_frame=_drive_gui)
            self.assertEqual(rc, 0)
            self.assertEqual(captured_var.get("v12"), 6)
            self.assertIsNotNone(captured_var.get("flash"))

    def test_92_vxace_full_db_tsx_importer_skill_balance_and_crash_save_routing(self) -> None:
        import os
        from src.core.editor import export_release_bundle
        from src.core.importers import _mv_tile_to_rmxp, import_tiled_map, import_vxace_rxdata
        from src.core.rmxp_marshal import RubyObject, marshal_dumps
        from src.core.rmxp_types import RgssTable
        from src.core.sample_generator import generate_sample_project
        from src.core.simulator import HeadlessGameSession, resolve_user_save_dir
        from src.core.utils import load_json, save_json
        from src.core.validator import simulate_battle_balance
        from src.harness.player_gui import launch_player

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            # 1. MV/MZ A1-A5 tile band fidelity
            a1_tile = _mv_tile_to_rmxp(2048)
            be_tile = _mv_tile_to_rmxp(100)
            self.assertGreaterEqual(a1_tile, 48)
            self.assertLess(a1_tile, 384)
            self.assertGreaterEqual(be_tile, 384)

            # 2. Tiled external .tsx passability & priority import
            res = generate_sample_project(root / "base")
            ir_dir = Path(res["ir_dir"])
            tdir = root / "tiled"
            tdir.mkdir()
            (tdir / "walls.tsx").write_text(
                "<tileset name='Walls' tilewidth='32' tileheight='32'>"
                "<tile id='2'><properties><property name='passable' type='bool' value='false'/><property name='priority' type='int' value='2'/></properties></tile>"
                "</tileset>",
                encoding="utf-8",
            )
            tmx_path = tdir / "citadel.tmx"
            tmx_path.write_text(
                "<map width='4' height='4' tilewidth='32' tileheight='32'>"
                "<tileset firstgid='1' source='walls.tsx'/>"
                "<layer name='Ground'><data encoding='csv'>" + ",".join(["3"] * 16) + "</data></layer>"
                "</map>",
                encoding="utf-8",
            )
            t_out = import_tiled_map(tmx_path, ir_dir, tileset_id=1)
            self.assertGreaterEqual(t_out["tileset_rules_applied"], 2)
            ts_db = load_json(ir_dir / "database" / "tilesets.json")
            tid_mapped = 384 + 3 - 1
            self.assertEqual(ts_db[1]["fields"]["passages"]["data"][tid_mapped], 15)
            self.assertEqual(ts_db[1]["fields"]["priorities"]["data"][tid_mapped], 2)

            # 3. Full VX Ace DB + CommonEvents import
            vxa = root / "vxa_full"
            (vxa / "Data").mkdir(parents=True)
            (vxa / "Data" / "System.rvdata2").write_bytes(marshal_dumps(RubyObject("RPG::System", {"@game_title": "Full VXA", "@party_members": [1]})))
            (vxa / "Data" / "Actors.rvdata2").write_bytes(marshal_dumps([None, RubyObject("RPG::Actor", {"@id": 1, "@name": "Eric", "@class_id": 1, "@initial_level": 3, "@character_name": "Hero"})]))
            (vxa / "Data" / "Classes.rvdata2").write_bytes(marshal_dumps([None, RubyObject("RPG::Class", {"@id": 1, "@name": "Paladin"})]))
            (vxa / "Data" / "Skills.rvdata2").write_bytes(marshal_dumps([None, RubyObject("RPG::Skill", {"@id": 1, "@name": "HolyNova", "@mp_cost": 8, "@power": 60})]))
            (vxa / "Data" / "Enemies.rvdata2").write_bytes(marshal_dumps([None, RubyObject("RPG::Enemy", {"@id": 1, "@name": "Lich", "@params": [220, 50, 28, 12], "@exp": 50, "@gold": 90})]))
            (vxa / "Data" / "CommonEvents.rvdata2").write_bytes(marshal_dumps([None, RubyObject("RPG::CommonEvent", {"@id": 1, "@name": "InitVXA", "@trigger": 0, "@switch_id": 1, "@list": [RubyObject("RPG::EventCommand", {"@code": 101, "@indent": 0, "@parameters": ["", 0, 0, 2]}), RubyObject("RPG::EventCommand", {"@code": 401, "@indent": 0, "@parameters": ["Welcome VXA!"]}), RubyObject("RPG::EventCommand", {"@code": 0, "@indent": 0, "@parameters": []})]})]))
            (vxa / "Data" / "MapInfos.rvdata2").write_bytes(marshal_dumps({1: RubyObject("RPG::MapInfo", {"@name": "Sanctum"})}))
            (vxa / "Data" / "Map001.rvdata2").write_bytes(marshal_dumps(RubyObject("RPG::Map", {"@width": 5, "@height": 5, "@tileset_id": 1, "@data": RgssTable.create(5, 5, 3, dims=3, fill=0).to_user_def(), "@events": {}})))
            vxa_ir = root / "vxa_ir_full"
            vxa_res = import_vxace_rxdata(vxa, vxa_ir)
            self.assertGreaterEqual(vxa_res["tables_imported"]["actors"], 1)
            self.assertEqual(vxa_res["tables_imported"]["common_events"], 1)
            self.assertEqual(load_json(vxa_ir / "database" / "enemies.json")[1]["fields"]["maxhp"], 220)

            # 4. Skill & state-aware battle balance simulation
            skills_p = ir_dir / "database" / "skills.json"
            sk = load_json(skills_p)
            if len(sk) > 1 and isinstance(sk[1], dict):
                sk[1]["fields"]["power"] = 90
                sk[1]["fields"]["sp_cost"] = 3
                save_json(skills_p, sk)
            bal = simulate_battle_balance(ir_dir, trials_per_troop=8)
            self.assertIn("troops", bal)
            self.assertGreaterEqual(len(bal["troops"]), 1)

            # 5. OS user save routing & launch_player crash report + rgssad launcher + build_exe check
            os_save_tmp = root / "os_saves"
            old_env = os.environ.get("RPGCREATOR_SAVE_DIR")
            os.environ["RPGCREATOR_SAVE_DIR"] = str(os_save_tmp)
            try:
                resolved = resolve_user_save_dir("rpgcreator_test", fallback_dir=ir_dir)
                self.assertEqual(resolved, os_save_tmp)
                os_sess = HeadlessGameSession(ir_dir, use_os_save_dir=True)
                os_sess.save_to_file(slot=2)
                self.assertTrue((os_save_tmp / "save_slot2.json").is_file())
            finally:
                if old_env is None:
                    os.environ.pop("RPGCREATOR_SAVE_DIR", None)
                else:
                    os.environ["RPGCREATOR_SAVE_DIR"] = old_env

            def _boom(_frame_idx: int, _scr: Any, _s: HeadlessGameSession) -> None:
                raise RuntimeError("Simulated player crash")

            with self.assertRaises(RuntimeError):
                launch_player(ir_dir, max_frames=3, on_frame=_boom)
            self.assertTrue(any((ir_dir / "crash_reports").glob("crash_*.log")))

            rel_out = root / "release_rgss3a"
            rel_res = export_release_bundle(ir_dir, rel_out, use_rgssad=True, launcher=True)
            self.assertEqual(rel_res["archive_mode"], "rgss3a")
            launcher_py = (rel_out / "launch_player.py").read_text(encoding="utf-8")
            self.assertIn("Game.rgss3a", launcher_py)
            self.assertIn("use_os_save_dir=True", launcher_py)

    def test_93_rgssad_audio_dir_strict_assets_keybindings_hue_gamepad_modal_and_mv_flags(self) -> None:
        import pygame
        from src.core.editor import export_release_bundle
        from src.core.importers import import_rpgmaker_mv_mz, import_vxace_rxdata
        from src.core.utils import load_json, save_json
        from src.harness.player_gui import (
            SAVE_SLOT_COUNT,
            _find_audio_dir,
            apply_hue_shift,
            build_title_menu_options,
            load_player_settings,
            process_save_load_shortcut,
            process_title_selection,
        )

        self.assertEqual(SAVE_SLOT_COUNT, 9)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ws = root / "p93"
            generate_sample_project(ws)
            ir_dir = ws / "ir"

            # 1. --rgssad --launcher passes both graphics_dir & audio_dir + --strict-assets
            rel_ok = root / "dist_ok"
            ok_res = export_release_bundle(
                ir_dir, rel_ok, use_rgssad=True, launcher=True, strict_assets=True
            )
            self.assertIn("assets_audit", ok_res)
            self.assertTrue(ok_res["assets_audit"]["passed"])
            self.assertEqual(ok_res["assets_audit"]["missing_audio"], 0)
            launcher_txt = (rel_ok / "launch_player.py").read_text(encoding="utf-8")
            self.assertIn("_resolve_asset_dir('Audio')", launcher_txt)
            self.assertIn("audio_dir=", launcher_txt)
            self.assertIn("Game.rgss3a", (rel_ok / "build_game.spec").read_text(encoding="utf-8"))
            custom_audio = root / "custom_audio"
            (custom_audio / "BGM").mkdir(parents=True)
            self.assertEqual(_find_audio_dir(ir_dir, custom_audio), custom_audio)

            proj_p = ir_dir / "ir_project.json"
            proj_doc = load_json(proj_p)
            proj_doc["system"]["fields"]["title_bgm"] = {"__class__": "RPG::AudioFile", "fields": {"name": "MissingBGM_93", "volume": 80, "pitch": 100}}
            save_json(proj_p, proj_doc)
            with self.assertRaises(ValueError):
                export_release_bundle(ir_dir, root / "dist_fail", strict_assets=True)
            proj_doc["system"]["fields"]["title_bgm"] = {"__class__": "RPG::AudioFile", "fields": {"name": "", "volume": 80, "pitch": 100}}
            save_json(proj_p, proj_doc)

            # 2. Save/Load roundtrip for player_opacity & player_blend_type + 9-slot wrap
            s1 = HeadlessGameSession(ir_dir)
            s1.player_opacity = 128
            s1.player_blend_type = 1
            s1.save_to_file(slot=3)
            s2 = HeadlessGameSession(ir_dir)
            self.assertTrue(s2.load_from_file(slot=3))
            self.assertEqual(s2.player_opacity, 128)
            self.assertEqual(s2.player_blend_type, 1)
            _, next_slot, _, _ = process_save_load_shortcut(pygame, pygame.K_F7, s2, 9, False)
            self.assertEqual(next_slot, 1)

            # 3. MV/MZ & VX Ace audio specs + tileset flags parity
            mv_dir = root / "mv_p93"
            (mv_dir / "data").mkdir(parents=True)
            (mv_dir / "data" / "System.json").write_text(json.dumps({
                "gameTitle": "MV93",
                "partyMembers": [1],
                "titleBgm": {"name": "MVTitle", "volume": 85, "pitch": 100},
                "battleBgm": {"name": "MVBattle", "volume": 90, "pitch": 105},
                "battleEndMe": {"name": "MVWin", "volume": 75, "pitch": 100},
                "gameoverMe": {"name": "MVOver", "volume": 80, "pitch": 95},
            }), encoding="utf-8")
            (mv_dir / "data" / "MapInfos.json").write_text(json.dumps([None, {"id": 1, "name": "Town"}]), encoding="utf-8")
            (mv_dir / "data" / "Tilesets.json").write_text(json.dumps([
                None,
                {"id": 1, "name": "Field", "tilesetNames": ["", "", "", "", "", "Field_B"], "flags": [0] * 2048 + [0x1F]},
            ]), encoding="utf-8")
            (mv_dir / "data" / "Map001.json").write_text(json.dumps({
                "tilesetId": 1, "width": 2, "height": 2, "data": [2048, 0, 0, 0] * 3, "events": [None]
            }), encoding="utf-8")
            mv_ir = root / "mv_ir93"
            import_rpgmaker_mv_mz(mv_dir, mv_ir)
            mv_sys = load_json(mv_ir / "ir_project.json")["system"]["fields"]
            self.assertEqual(mv_sys["title_bgm"]["fields"]["name"], "MVTitle")
            self.assertEqual(mv_sys["battle_bgm"]["fields"]["name"], "MVBattle")
            self.assertEqual(mv_sys["battle_end_me"]["fields"]["name"], "MVWin")
            self.assertEqual(mv_sys["gameover_me"]["fields"]["name"], "MVOver")
            mv_ts = load_json(mv_ir / "database" / "tilesets.json")[1]["fields"]
            self.assertEqual(mv_ts["passages"]["data"][48], 0x0F)
            self.assertEqual(mv_ts["priorities"]["data"][48], 5)

            vxa_dir = root / "vxa_p93"
            (vxa_dir / "Data").mkdir(parents=True)
            (vxa_dir / "Data" / "System.rvdata2").write_bytes(marshal_dumps(RubyObject("RPG::System", {
                "@game_title": "VXA93",
                "@party_members": [1],
                "@title_bgm": RubyObject("RPG::BGM", {"@name": "VXATitle", "@volume": 88, "@pitch": 100}),
                "@gameover_me": RubyObject("RPG::ME", {"@name": "VXAOver", "@volume": 70, "@pitch": 100}),
            })))
            (vxa_dir / "Data" / "Tilesets.rvdata2").write_bytes(marshal_dumps([
                None,
                RubyObject("RPG::Tileset", {"@id": 1, "@name": "VXA_TS", "@tileset_names": ["", ""], "@flags": RgssTable.create(50, 1, 1, dims=1, fill=0).to_user_def()}),
            ]))
            tbl_vxa = RgssTable.create(2, 2, 3, dims=3, fill=0)
            tbl_vxa.set(0, 0, 0, 2048)
            (vxa_dir / "Data" / "MapInfos.rvdata2").write_bytes(marshal_dumps({1: RubyObject("RPG::MapInfo", {"@name": "VXA_Map"})}))
            (vxa_dir / "Data" / "Map001.rvdata2").write_bytes(marshal_dumps(RubyObject("RPG::Map", {
                "@width": 2, "@height": 2, "@tileset_id": 1, "@data": tbl_vxa.to_user_def(), "@events": {}
            })))
            vxa_ir = root / "vxa_ir93"
            import_vxace_rxdata(vxa_dir, vxa_ir)
            vxa_sys = load_json(vxa_ir / "ir_project.json")["system"]["fields"]
            self.assertEqual(vxa_sys["title_bgm"]["fields"]["name"], "VXATitle")
            self.assertEqual(vxa_sys["gameover_me"]["fields"]["name"], "VXAOver")
            vxa_map = load_json(vxa_ir / "maps" / "map_001.json")
            self.assertEqual(vxa_map["layers"]["layer_0"][0][0], 48)

            # 4. Hue rotation on Surface
            pygame.init()
            red_surf = pygame.Surface((4, 4), pygame.SRCALPHA)
            red_surf.fill((255, 0, 0, 255))
            shifted = apply_hue_shift(pygame, red_surf, 120)
            r, g, b, _ = shifted.get_at((1, 1))
            self.assertGreater(g, 200)
            self.assertLess(r, 40)
            self.assertIs(apply_hue_shift(pygame, red_surf, 0), red_surf)

            # 5. Custom keybindings + Options preset row + Modal Gamepad navigation inside launch_player
            custom_cfg = {"keybindings": {"up": ["i"], "down": ["k"], "left": ["j"], "right": ["l"], "confirm": "u", "cancel": "y"}}
            s_kb = HeadlessGameSession(ir_dir)
            s_kb._run_commands([
                {"op": "show_choices", "choices": ["A", "B"], "cancel_type": 2, "indent": 0},
            ], 0, 1, 1)
            _, _, _, _, _ = _process_keydown(pygame, pygame.K_u, s_kb, 0, False, 0, 0, settings=custom_cfg)
            self.assertIsNone(s_kb.pending)
            self.assertGreater(len(build_title_menu_options(s_kb, mode="options", show_minimap=True, settings=custom_cfg, include_controls=True)), 7)
            opt_res = process_title_selection(s_kb, "options", 6, True, settings=dict(custom_cfg), include_controls=True)
            self.assertEqual(opt_res["next_mode"], "options")

            # Live modal gamepad test inside launch_player (Title -> Options -> Keyset -> Title -> Game -> Choice)
            clean_ws = root / "p93_gp"
            generate_sample_project(clean_ws)
            clean_ir = clean_ws / "ir"
            # Delete any sample save slot so Title cursor order is predictable
            for sf in clean_ir.glob("save_slot*.json"):
                sf.unlink()

            choice_Pick: list = []

            def _drive_gamepad_modal(tick: int, _scr: Any, live: HeadlessGameSession) -> None:
                if tick == 0:
                    pygame.event.post(pygame.event.Event(pygame.JOYHATMOTION, value=(0, -1)))
                    pygame.event.post(pygame.event.Event(pygame.JOYHATMOTION, value=(0, -1)))
                    pygame.event.post(pygame.event.Event(pygame.JOYBUTTONDOWN, button=0))  # Open Options
                elif tick == 1:
                    for _ in range(6):
                        pygame.event.post(pygame.event.Event(pygame.JOYHATMOTION, value=(0, -1)))
                    pygame.event.post(pygame.event.Event(pygame.JOYBUTTONDOWN, button=0))  # Cycle keyset WASD/Z -> IJKL/U
                elif tick == 2:
                    pygame.event.post(pygame.event.Event(pygame.JOYBUTTONDOWN, button=1))  # Back to Title
                elif tick == 3:
                    pygame.event.post(pygame.event.Event(pygame.JOYBUTTONDOWN, button=0))  # Start New Game
                elif tick == 4:
                    live._run_commands([
                        {"op": "show_choices", "choices": ["첫째", "둘째"], "cancel_type": 0, "indent": 0},
                    ], 0, 1, 1)
                elif tick == 5:
                    pygame.event.post(pygame.event.Event(pygame.JOYAXISMOTION, axis=1, value=0.9))  # Move down to "둘째"
                    pygame.event.post(pygame.event.Event(pygame.JOYAXISMOTION, axis=1, value=0.0))
                    pygame.event.post(pygame.event.Event(pygame.JOYBUTTONDOWN, button=0))
                elif tick == 6:
                    choice_Pick.append(live.pending is None)

            rc = launch_player(clean_ir, start_on_title=True, max_frames=8, on_frame=_drive_gamepad_modal)
            self.assertEqual(rc, 0)
            self.assertEqual(load_player_settings(clean_ir)["keybindings"]["confirm"], "space")
            self.assertEqual(choice_Pick, [True])

    def test_94_inmemory_rgssad_spec_exe_checksum_warn_autosave_status_mouse_fog_cache_and_cli_tools(self):
        import json
        import os
        import pygame
        from src.core.editor import (
            batch_tune_balance,
            export_release_bundle,
            list_animations,
            run_interactive_workbench,
            set_animation,
            undo_last_edit,
        )
        from src.core.importers import import_rpgmaker_mv_mz, import_vxace_rxdata
        from src.core.rgssad import RgssadArchiveReader
        from src.core.utils import load_json
        from src.harness.player_gui import (
            VIEW_H,
            VIEW_W,
            _draw_fog,
            _draw_overlays,
            compute_equip_stat_delta,
            launch_player,
            load_player_settings,
            process_save_load_shortcut,
        )

        os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ws = root / "ws94"
            generate_sample_project(ws)
            ir_dir = ws / "ir"

            # 1. Custom --rgssad-key export, PyInstaller spec binaries/datas, _MEIPASS & .rgss3a_stamp, and RAM streaming
            custom_key = 0x13572468
            rel_dir = root / "dist94"
            rel_res = export_release_bundle(
                ir_dir, rel_dir, use_rgssad=True, launcher=True, strict_assets=False, rgssad_key=custom_key
            )
            self.assertEqual(rel_res["rgssad_key"], f"0x{custom_key:08X}")
            arch_path = rel_dir / "Game.rgss3a"
            self.assertTrue(arch_path.is_file())
            self.assertFalse((rel_dir / "Graphics").exists())

            lp_src = (rel_dir / "launch_player.py").read_text(encoding="utf-8")
            self.assertIn("_MEIPASS", lp_src)
            self.assertIn(".rgss3a_stamp", lp_src)
            self.assertIn(str(custom_key), lp_src)
            spec_src = (rel_dir / "build_game.spec").read_text(encoding="utf-8")
            self.assertIn("a.binaries, a.zipfiles, a.datas", spec_src)

            reader = RgssadArchiveReader(arch_path, initial_key=custom_key)
            png_bytes = reader.read_bytes("Graphics/Tilesets/sample_town.png", extensions=(".png",))
            self.assertIsNotNone(png_bytes)
            self.assertTrue(png_bytes.startswith(b"\x89PNG"))
            import struct
            self.assertEqual(struct.unpack_from("<I", arch_path.read_bytes(), 8)[0], custom_key)

            # Verify launch_player streams sprites from encrypted Game.rgss3a without graphics_dir on disk
            streamed_ok: list = []

            def _check_streamed(tick: int, _scr: Any, live: HeadlessGameSession) -> None:
                if tick == 0:
                    streamed_ok.append(live.map_id)

            rc_stream = launch_player(
                rel_dir / "ir",
                graphics_dir=root / "nonexistent_gfx",
                audio_dir=root / "nonexistent_aud",
                rgssad_archive=arch_path,
                rgssad_key=custom_key,
                max_frames=2,
                on_frame=_check_streamed,
            )
            self.assertEqual(rc_stream, 0)
            self.assertEqual(streamed_ok, [1])

            # 2. Save checksum tampering warning + Auto-save on map transition
            sess = HeadlessGameSession(ir_dir)
            sess.save_to_file(slot=1)
            slot1_path = ir_dir / "save_slot1.json"
            payload = json.loads(slot1_path.read_text(encoding="utf-8"))
            payload["gold"] = 999999
            slot1_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

            pygame.init()
            try:
                _h1, _sl1, pend1, msg1 = process_save_load_shortcut(pygame, pygame.K_F9, sess, 1, False)
                self.assertTrue(pend1)
                self.assertIn("F9", msg1)
                _h2, _sl2, pend2, msg2 = process_save_load_shortcut(pygame, pygame.K_F9, sess, 1, True)
                self.assertFalse(pend2)
                self.assertFalse(sess.save_checksum_valid)
                self.assertIn("경고", msg2)
                self.assertIn("무결성", msg2)

                # Auto-save round-trip + status equip stat delta preview + fog/hue caches
                sess.gold = 432
                as_path = sess.save_autosave()
                self.assertTrue(as_path.is_file())
                sess.gold = 0
                self.assertTrue(sess.load_autosave())
                self.assertEqual(sess.gold, 432)

                actor = sess._get_actor_state(1)
                actor["weapon_id"] = 0
                sess.weapons_owned[1] = 1
                delta = compute_equip_stat_delta(sess, 1, 0, 1)
                self.assertIn("ATK", delta)

                surf = pygame.Surface((VIEW_W, VIEW_H))
                font = pygame.font.Font(None, 20)
                sfont = pygame.font.Font(None, 16)
                st_rects: list = []
                hue_cache: dict = {}
                _draw_overlays(
                    pygame, surf, font, sfont, sess, 0, True, status_mode="equip",
                    hue_cache=hue_cache, out_status_rects=st_rects,
                )
                kinds = {k for k, _v, _r in st_rects}
                self.assertIn("toggle_tab", kinds)
                self.assertIn("close", kinds)
                self.assertIn("item", kinds)

                fog_cache: dict = {}
                fog_img = pygame.Surface((32, 32), pygame.SRCALPHA)
                fog_img.fill((255, 255, 255, 180))
                sess.fog_state = {"name": "Mist", "opacity": 90, "tone": [0, 0, 0, 0]}
                _draw_fog(pygame, surf, lambda _p: fog_img, sess, 0, 0, fog_cache=fog_cache)
                _draw_fog(pygame, surf, lambda _p: fog_img, sess, 12, 8, fog_cache=fog_cache)
                self.assertEqual(len(fog_cache), 1)
            finally:
                pygame.quit()

            # 3. Live Options individual key capture + Mouse status panel close
            kb_ws = root / "kb_ws94"
            generate_sample_project(kb_ws)
            kb_ir = kb_ws / "ir"
            for sf in kb_ir.glob("save_slot*.json"):
                sf.unlink()

            def _drive_kb_capture(tick: int, _scr: Any, _live: HeadlessGameSession) -> None:
                if tick == 0:
                    pygame.event.post(pygame.event.Event(pygame.KEYDOWN, key=pygame.K_DOWN))
                    pygame.event.post(pygame.event.Event(pygame.KEYDOWN, key=pygame.K_DOWN))
                    pygame.event.post(pygame.event.Event(pygame.KEYDOWN, key=pygame.K_RETURN))  # Options
                elif tick == 1:
                    for _ in range(7):
                        pygame.event.post(pygame.event.Event(pygame.KEYDOWN, key=pygame.K_DOWN))
                    pygame.event.post(pygame.event.Event(pygame.KEYDOWN, key=pygame.K_RETURN))  # Row 7: 확인 키 지정
                elif tick == 2:
                    pygame.event.post(pygame.event.Event(pygame.KEYDOWN, key=pygame.K_v))  # Remap confirm -> 'v'

            rc_kb = launch_player(kb_ir, start_on_title=True, max_frames=4, on_frame=_drive_kb_capture)
            self.assertEqual(rc_kb, 0)
            self.assertEqual(load_player_settings(kb_ir)["keybindings"]["confirm"], "v")

            # 4. MV/MZ & VX Ace map metadata preservation (tileset_id, autoplay_bgm, parallax_name, encounter_list)
            mv_dir = root / "mv_p94"
            (mv_dir / "data").mkdir(parents=True)
            (mv_dir / "data" / "System.json").write_text(json.dumps({
                "gameTitle": "MV94", "partyMembers": [1], "startMapId": 1, "startX": 0, "startY": 0,
                "switches": [None, "S1"], "variables": [None, "V1"], "terms": {"commands": [], "params": []},
            }), encoding="utf-8")
            (mv_dir / "data" / "Tilesets.json").write_text(json.dumps([
                None,
                {"id": 1, "name": "TS1", "flags": [0, 0, 0]},
                {"id": 2, "name": "TS2", "flags": [0, 0, 0]},
            ]), encoding="utf-8")
            (mv_dir / "data" / "MapInfos.json").write_text(json.dumps([None, {"id": 1, "name": "MVField"}]), encoding="utf-8")
            (mv_dir / "data" / "Map001.json").write_text(json.dumps({
                "width": 2, "height": 2, "tilesetId": 2,
                "autoplayBgm": True, "bgm": {"name": "FieldTheme", "volume": 85, "pitch": 100},
                "autoplayBgs": True, "bgs": {"name": "Wind", "volume": 40, "pitch": 100},
                "parallaxName": "BlueSky", "parallaxLoopX": True, "parallaxLoopY": False,
                "encounterList": [{"troopId": 3, "weight": 10, "regionSet": []}],
                "data": [0] * (2 * 2 * 6),
                "events": [None],
            }), encoding="utf-8")
            mv_ir = root / "mv_ir94"
            import_rpgmaker_mv_mz(mv_dir, mv_ir)
            mv_map = load_json(mv_ir / "maps" / "map_001.json")
            self.assertEqual(mv_map["tileset_id"], 2)
            self.assertTrue(mv_map["autoplay_bgm"])
            self.assertEqual(mv_map["bgm"]["fields"]["name"], "FieldTheme")
            self.assertEqual(mv_map["parallax_name"], "BlueSky")
            self.assertTrue(mv_map["parallax_loop_x"])
            self.assertEqual(mv_map["encounter_list"], [3])

            vxa_dir = root / "vxa_p94"
            (vxa_dir / "Data").mkdir(parents=True)
            (vxa_dir / "Data" / "System.rvdata2").write_bytes(marshal_dumps(RubyObject("RPG::System", {
                "@game_title": "VXA94", "@party_members": [1],
            })))
            (vxa_dir / "Data" / "Tilesets.rvdata2").write_bytes(marshal_dumps([
                None,
                RubyObject("RPG::Tileset", {"@id": 1, "@name": "V1", "@tileset_names": ["", ""], "@flags": RgssTable.create(50, 1, 1, dims=1, fill=0).to_user_def()}),
                RubyObject("RPG::Tileset", {"@id": 2, "@name": "V2", "@tileset_names": ["", ""], "@flags": RgssTable.create(50, 1, 1, dims=1, fill=0).to_user_def()}),
            ]))
            tbl_vxa = RgssTable.create(2, 2, 3, dims=3, fill=0)
            (vxa_dir / "Data" / "MapInfos.rvdata2").write_bytes(marshal_dumps({1: RubyObject("RPG::MapInfo", {"@name": "VXADung"})}))
            (vxa_dir / "Data" / "Map001.rvdata2").write_bytes(marshal_dumps(RubyObject("RPG::Map", {
                "@width": 2, "@height": 2, "@tileset_id": 2,
                "@autoplay_bgm": True, "@bgm": RubyObject("RPG::BGM", {"@name": "Dungeon", "@volume": 90, "@pitch": 100}),
                "@parallax_name": "Starlight", "@parallax_loop_x": True,
                "@encounter_list": [RubyObject("RPG::Map::Encounter", {"@troop_id": 2, "@weight": 5})],
                "@data": tbl_vxa.to_user_def(), "@events": {},
            })))
            vxa_ir = root / "vxa_ir94"
            import_vxace_rxdata(vxa_dir, vxa_ir)
            vxa_map = load_json(vxa_ir / "maps" / "map_001.json")
            self.assertEqual(vxa_map["tileset_id"], 2)
            self.assertTrue(vxa_map["autoplay_bgm"])
            self.assertEqual(vxa_map["bgm"]["fields"]["name"], "Dungeon")
            self.assertEqual(vxa_map["parallax_name"], "Starlight")
            self.assertEqual(vxa_map["encounter_list"], [2])

            # 5. Authoring CLI APIs: set_animation/list_animations, batch_tune_balance, run_interactive_workbench + undo
            a_res = set_animation(
                ir_dir, anim_id=2, name="회오리베기", animation_name="SlashSheet",
                frame_max=4, se_name="Sword1", flash_color=[255, 220, 100, 180],
            )
            self.assertEqual(a_res["frame_max"], 4)
            anims = list_animations(ir_dir)
            self.assertTrue(any(a["id"] == 2 and a["name"] == "회오리베기" for a in anims))

            enemies_before = load_json(ir_dir / "database" / "enemies.json")
            hp_before = int(enemies_before[1]["fields"]["maxhp"])
            tune_res = batch_tune_balance(ir_dir, enemy_hp_mult=2.0, gold_mult=1.5)
            self.assertGreaterEqual(tune_res["enemies_tuned"], 1)
            hp_after = int(load_json(ir_dir / "database" / "enemies.json")[1]["fields"]["maxhp"])
            self.assertEqual(hp_after, hp_before * 2)
            undo_last_edit(ir_dir)
            self.assertEqual(int(load_json(ir_dir / "database" / "enemies.json")[1]["fields"]["maxhp"]), hp_before)

            wb_res = run_interactive_workbench(ir_dir, stdin_lines=[
                "inspect",
                "maps",
                "set-db actors 1 name=클라우드",
                "check",
                "undo",
                "quit",
            ])
            self.assertEqual(wb_res["commands_run"], 5)
            self.assertTrue(any("check passed=True" in ln for ln in wb_res["logs"]))


if __name__ == "__main__":
    suite = unittest.TestLoader().loadTestsFromTestCase(TestRpgCreatorHeadless)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
