# -*- coding: utf-8 -*-
"""
Headless & Real-Tkinter Studio GUI Test Suite.
Verifies undo-backed Studio editing helpers, Agent/CLI instant command bridge,
and live Tkinter StudioApp widget rendering, interaction, and refresh cycles.
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import tkinter as tk
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from src.core.battle_formulas import PRESETS
from src.core.editor import get_db_entry, list_maps, redo_last_edit, save_event, set_db_entry, undo_last_edit, update_map_properties
from src.core.sample_generator import generate_sample_project
from src.core.simulator import HeadlessGameSession
from src.core.utils import load_json
from src.core.validator import validate_ir_project
from src.harness.agent_bridge import build_agent_context_packet, execute_studio_command, format_agent_prompt_packet
from src.harness.studio_gui import StudioApp, _find_studio_icon_path


class TestStudioBackendAndAgentBridge(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="rpgc_studio_"))
        generate_sample_project(self.tmp)
        self.ir_dir = self.tmp / "ir"

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_update_map_properties_resize_and_undo_redo(self) -> None:
        res = update_map_properties(
            self.ir_dir,
            1,
            {"name": "Renamed Start", "width": 24, "height": 18, "encounter_step": 45},
        )
        self.assertEqual(res["width"], 24)
        self.assertEqual(res["height"], 18)
        m1 = load_json(self.ir_dir / "maps" / "map_001.json")
        self.assertEqual(m1["name"], "Renamed Start")
        self.assertEqual(len(m1["layers"]["layer_0"]), 18)
        self.assertEqual(len(m1["layers"]["layer_0"][0]), 24)

        undo_res = undo_last_edit(self.ir_dir)
        self.assertTrue(undo_res["restored"])
        m1_back = load_json(self.ir_dir / "maps" / "map_001.json")
        self.assertNotEqual(m1_back["name"], "Renamed Start")

        redo_res = redo_last_edit(self.ir_dir)
        self.assertTrue(redo_res["restored"])
        m1_fwd = load_json(self.ir_dir / "maps" / "map_001.json")
        self.assertEqual(m1_fwd["name"], "Renamed Start")

    def test_save_event_and_db_system_tileset_undo(self) -> None:
        ev_res = save_event(
            self.ir_dir,
            1,
            {
                "name": "StudioSage",
                "x": 3,
                "y": 4,
                "pages": [
                    {
                        "trigger": "action_button",
                        "through": False,
                        "commands": [{"op": "say", "lines": ["Welcome to Studio!"]}, {"op": "end", "indent": 0}],
                    }
                ],
            },
        )
        self.assertFalse(ev_res["replaced"])
        new_id = ev_res["event_id"]

        upd_res = save_event(
            self.ir_dir,
            1,
            {
                "id": new_id,
                "name": "StudioSageEdited",
                "x": 3,
                "y": 4,
                "pages": [
                    {
                        "trigger": "player_touch",
                        "through": True,
                        "commands": [{"op": "say", "lines": ["Updated!"]}, {"op": "end", "indent": 0}],
                    }
                ],
            },
        )
        self.assertTrue(upd_res["replaced"])

        set_db_entry(self.ir_dir, "system", 1, {"start_x": 6, "start_y": 7}, record_undo=True)
        sys_entry = get_db_entry(self.ir_dir, "system", 1)
        self.assertEqual(sys_entry["fields"]["start_x"], 6)
        self.assertEqual(sys_entry["fields"]["start_y"], 7)

        undo_last_edit(self.ir_dir)
        sys_back = get_db_entry(self.ir_dir, "system", 1)
        self.assertNotEqual(sys_back["fields"]["start_x"], 6)

    def test_agent_bridge_cli_workbench_blueprint_and_nl(self) -> None:
        ctx = {"current_map_id": 1, "selected_event_id": 1, "selected_table": "enemies"}
        pkt = build_agent_context_packet(self.ir_dir, **ctx)
        self.assertIn("active_map", pkt)
        prompt_md = format_agent_prompt_packet(self.ir_dir, user_goal="Tune boss", **ctx)
        self.assertIn("RPG Creator Studio Context", prompt_md)

        # CLI without ir_dir path
        r_cli = execute_studio_command(self.ir_dir, "map list --json", context=ctx)
        self.assertTrue(r_cli["ok"])
        self.assertFalse(r_cli["mutated"])
        self.assertIn("map_id", r_cli["summary"])

        r_db = execute_studio_command(self.ir_dir, "db set --table enemies --id 1 --field maxhp=555", context=ctx)
        self.assertTrue(r_db["ok"])
        self.assertTrue(r_db["mutated"])
        self.assertEqual(get_db_entry(self.ir_dir, "enemies", 1)["fields"]["maxhp"], 555)

        # Undo via Korean NL
        r_undo = execute_studio_command(self.ir_dir, "되돌리기", context=ctx)
        self.assertTrue(r_undo["ok"])
        self.assertNotEqual(get_db_entry(self.ir_dir, "enemies", 1)["fields"]["maxhp"], 555)

        # Workbench shorthand
        r_wb = execute_studio_command(self.ir_dir, "paint 1 385 2 2", context=ctx)
        self.assertTrue(r_wb["ok"])
        self.assertTrue(r_wb["mutated"])
        m1 = load_json(self.ir_dir / "maps" / "map_001.json")
        self.assertEqual(m1["layers"]["layer_0"][2][2], 385)

        # Korean NL: create map, add NPC, paint rect, fuzz, balance tune, check
        r_map = execute_studio_command(self.ir_dir, "새 맵 '신전' 12x10", context=ctx)
        self.assertTrue(r_map["ok"])
        self.assertEqual(r_map["result"]["width"], 12)
        self.assertEqual(r_map["result"]["height"], 10)

        r_npc = execute_studio_command(self.ir_dir, "(3,4)에 '수호자' NPC 추가 대사 '환영합니다'", context=ctx)
        self.assertTrue(r_npc["ok"])
        m1_evs = load_json(self.ir_dir / "maps" / "map_001.json")["events"]
        self.assertTrue(any(ev["name"] == "수호자" for ev in m1_evs))

        r_tune = execute_studio_command(self.ir_dir, "적 체력 2배", context=ctx)
        self.assertTrue(r_tune["ok"])
        self.assertTrue(r_tune["mutated"])

        r_fuzz = execute_studio_command(self.ir_dir, "퍼징 20회 seed 3", context=ctx)
        self.assertTrue(r_fuzz["ok"])

        r_chk = execute_studio_command(self.ir_dir, "검증해줘", context=ctx)
        self.assertTrue(r_chk["ok"])

        # Fenced JSON blueprint
        r_bp = execute_studio_command(
            self.ir_dir,
            '```json\n{"db": [{"table": "enemies", "id": 1, "fields": {"exp": 99}}]}\n```',
            context=ctx,
        )
        self.assertTrue(r_bp["ok"])
        self.assertEqual(get_db_entry(self.ir_dir, "enemies", 1)["fields"]["exp"], 99)

        # External CLI agent hook returning a blueprint
        ext_script = (
            f'"{sys.executable}" -c "import json; '
            'print(json.dumps({\'db\': [{\'table\': \'enemies\', \'id\': 1, \'fields\': {\'gold\': 333}}]}))"'
        )
        r_ext = execute_studio_command(
            self.ir_dir,
            "Make slime drop 333 gold via external agent",
            context=ctx,
            external_agent_cmd=ext_script,
            apply_external=True,
        )
        self.assertTrue(r_ext["ok"])
        self.assertTrue(r_ext["mutated"])
        self.assertEqual(get_db_entry(self.ir_dir, "enemies", 1)["fields"]["gold"], 333)

        # Major-2: workbench logs + undo via agent_bridge
        r_wb_paint = execute_studio_command(self.ir_dir, "paint 1 390 1 1", context=ctx)
        self.assertTrue(r_wb_paint["ok"])
        self.assertIn("painted=1", r_wb_paint["summary"])
        m1_painted = load_json(self.ir_dir / "maps" / "map_001.json")
        self.assertEqual(m1_painted["layers"]["layer_0"][1][1], 390)
        execute_studio_command(self.ir_dir, "되돌리기", context=ctx)
        m1_reverted = load_json(self.ir_dir / "maps" / "map_001.json")
        self.assertNotEqual(m1_reverted["layers"]["layer_0"][1][1], 390)

        # UX #5 & TOP 10 #7: "밸런스 체크", "고블린 체력 234", "도움말"
        r_bal = execute_studio_command(self.ir_dir, "밸런스 체크", context=ctx)
        self.assertTrue(r_bal["ok"])
        self.assertEqual(r_bal["kind"], "nl_balance_check")
        r_gob = execute_studio_command(self.ir_dir, "고블린 체력 234", context=ctx)
        self.assertTrue(r_gob["ok"])
        self.assertEqual(r_gob["kind"], "nl_set_db")
        r_help = execute_studio_command(self.ir_dir, "도움말", context=ctx)
        self.assertTrue(r_help["ok"])
        self.assertEqual(r_help["kind"], "nl_help")


class TestStudioTkinterWidgetSmoke(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="rpgc_tk_"))
        generate_sample_project(self.tmp)
        self.ir_dir = self.tmp / "ir"

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_studio_app_renders_and_mutates_via_tabs_and_agent_dock(self) -> None:
        try:
            root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(f"Tkinter display unavailable: {exc}")
            return

        root.withdraw()
        try:
            app = StudioApp(root, ir_dir=self.ir_dir, graphics_dir=self.tmp / "Graphics")
            root.update_idletasks()

            self.assertGreaterEqual(app.tab_map.map_list.size(), 1)
            self.assertGreaterEqual(app.tab_db.entry_list.size(), 1)

            # Mutate via bottom Agent Console and confirm live GUI Map List refresh
            before_maps = len(list_maps(self.ir_dir))
            res = app.agent_dock.run_command_text("새 맵 'GUI테스트맵' 14x11")
            root.update_idletasks()
            self.assertTrue(res["ok"])
            self.assertEqual(app.tab_map.map_list.size(), before_maps + 1)

            # Switch DB category to system, edit & save
            app.tab_db.current_table.set("system")
            app.tab_db.reload_entries()
            root.update_idletasks()
            self.assertEqual(app.tab_db.entry_list.size(), 1)

            # Run quick ops check button & fuzz inside OpsReleaseTab
            app.tab_ops._run_check()
            self.assertIn("무결성 검사", app.tab_ops.lbl_ops_summary.cget("text"))
            app.tab_ops.fuzz_steps_var.set(10)
            app.tab_ops._run_fuzz()
            root.update_idletasks()
            self.assertIn("steps", app.tab_ops.txt_ops_out.get("1.0", "end").lower())

            # UX #1 & #2: dirty tracking + text-input Ctrl+Z focus guard
            self.assertFalse(app.tab_map._is_event_dirty())
            app.tab_map.ev_name_var.set("ModifiedEventName")
            self.assertTrue(app.tab_map._is_event_dirty())
            self.assertTrue(app.tab_map.save_current_event())
            root.update_idletasks()
            self.assertFalse(app.tab_map._is_event_dirty())

            app.tab_map.txt_ev_cmds.focus_set()
            root.update_idletasks()
            if root.focus_get() is app.tab_map.txt_ev_cmds:
                self.assertTrue(app._is_text_widget_focused())
                self.assertIsNone(app._on_shortcut_undo())

            # Undo creation via Studio action_undo
            app.action_undo()
            app.action_undo()
            root.update_idletasks()
            self.assertEqual(len(list_maps(self.ir_dir)), before_maps)
        finally:
            root.destroy()

    def test_quick_choice_and_switch_generate_canonical_ir_and_simulate(self) -> None:
        try:
            root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(f"Tkinter display unavailable: {exc}")
            return

        root.withdraw()
        try:
            app = StudioApp(root, ir_dir=self.ir_dir, graphics_dir=self.tmp / "Graphics")
            root.update_idletasks()

            # Clear commands on the currently selected event and append via beginner quick buttons
            app.tab_map.txt_ev_cmds.delete("1.0", "end")
            with patch("src.harness.studio_gui.simpledialog.askstring", side_effect=["수락", "거절"]), \
                 patch("src.harness.studio_gui.simpledialog.askinteger", return_value=1), \
                 patch("src.harness.studio_gui.messagebox.askyesno", return_value=True):
                app.tab_map._quick_add_choice()
                app.tab_map._quick_add_switch()

            self.assertTrue(app.tab_map.save_current_event())
            root.update_idletasks()

            # Validate canonical IR structure
            report = validate_ir_project(self.ir_dir)
            self.assertTrue(report.passed, msg=f"Validation errors: {report.errors}")

            map_id = int(app.tab_map.current_map_id)
            ev_id = int(app.tab_map.selected_event_id)
            mdata = load_json(self.ir_dir / "maps" / f"map_{map_id:03d}.json")
            ev = next(e for e in mdata["events"] if int(e["id"]) == ev_id)
            cmds = ev["pages"][app.tab_map.selected_page_idx]["commands"]
            ops = [c["op"] for c in cmds]
            self.assertEqual(
                ops,
                ["show_choices", "when_choice", "say", "when_choice", "say", "end_choices", "set_switch", "end"],
            )
            sw_cmd = next(c for c in cmds if c["op"] == "set_switch")
            self.assertEqual(sw_cmd["start_id"], 1)
            self.assertEqual(sw_cmd["end_id"], 1)
            self.assertEqual(sw_cmd["value"], "ON")

            # Simulate triggering the event and choosing option 0 in HeadlessGameSession
            sess = HeadlessGameSession(self.ir_dir)
            sess.map_id = map_id
            ev_x, ev_y = int(ev["x"]), int(ev["y"])
            if ev_y > 0:
                sess.x, sess.y = ev_x, ev_y - 1
                sess.step("face:down")
            else:
                sess.x, sess.y = ev_x, ev_y + 1
                sess.step("face:up")

            sess.step("interact")
            self.assertIsNotNone(sess.pending)
            self.assertEqual(sess.pending.choices, ["수락", "거절"])

            sess.step("choose:0")
            self.assertIsNotNone(sess.pending)
            self.assertEqual(sess.pending.lines, ["수락 선택"])

            sess.step("interact")
            self.assertIsNone(sess.pending)
            self.assertTrue(sess.switches.get(1, False))
        finally:
            root.destroy()

    def test_nocode_builders_asset_browser_anim_playback_icon_and_async_dock(self) -> None:
        from PIL import Image

        ico = _find_studio_icon_path()
        self.assertIsNotNone(ico)
        with Image.open(ico) as im:
            self.assertEqual(im.format, "ICO")

        try:
            root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(f"Tkinter display unavailable: {exc}")
            return

        root.withdraw()
        try:
            app = StudioApp(root, ir_dir=self.ir_dir, graphics_dir=self.tmp / "Graphics")
            root.update_idletasks()

            # 1. No-code canonical event builders (transfer, shop, battle, conditional branch)
            app.tab_map.txt_ev_cmds.delete("1.0", "end")
            app.tab_map._append_ev_cmds(app.tab_map.build_branch_cmds("switch", target_id=1, expected="ON", include_else=True))
            app.tab_map._append_ev_cmds(app.tab_map.build_shop_cmds([{"type": "item", "id": 1}], purchase_only=True))
            app.tab_map._append_ev_cmds(app.tab_map.build_battle_cmds(1, can_escape=True, can_lose=False, with_branches=True))
            app.tab_map._append_ev_cmds(app.tab_map.build_transfer_cmds(2, 10, 5, direction="up", fade_type=0))
            self.assertTrue(app.tab_map.save_current_event())
            report = validate_ir_project(self.ir_dir)
            self.assertTrue(report.passed, msg=f"Validation errors: {report.errors}")

            # 2. Animation playback start/step/stop in Tab 3
            app.tab_ca._toggle_anim_play()
            self.assertTrue(app.tab_ca._anim_playing)
            app.tab_ca._stop_anim_play()
            self.assertFalse(app.tab_ca._anim_playing)

            # 3. Asset browser Tab 5: scan + import new PNG + preview
            self.assertGreaterEqual(len(app.tab_assets.list_category_files("Graphics/Tilesets")), 1)
            extra_png = self.tmp / "custom_hero.png"
            Image.new("RGBA", (32, 32), (220, 40, 60, 255)).save(extra_png)
            imported = app.tab_assets.import_asset_file(extra_png, category="Graphics/Characters")
            self.assertTrue(imported.is_file())
            app.tab_assets.cat_var.set("Graphics/Characters")
            app.tab_assets.reload_files()
            root.update_idletasks()
            self.assertTrue(any(p.name == "custom_hero.png" for p in app.tab_assets._files))

            # 4. Async External AI CLI dock execution + completion state reset
            ext_script = (
                f'"{sys.executable}" -c "import json; '
                'print(json.dumps({\'db\': [{\'table\': \'enemies\', \'id\': 1, \'fields\': {\'gold\': 444}}]}))"'
            )
            app.agent_dock.ext_cmd_var.set(ext_script)
            r_async = app.agent_dock.run_command_text("Make slime drop 444 gold async")
            self.assertEqual(r_async["kind"], "external_async")
            self.assertIsNotNone(app.agent_dock._last_async_thread)
            app.agent_dock._last_async_thread.join(timeout=10.0)
            app.agent_dock._drain_ext_queue()
            root.update_idletasks()
            self.assertFalse(app.agent_dock._ext_busy)
            self.assertEqual(get_db_entry(self.ir_dir, "enemies", 1)["fields"]["gold"], 444)

            # 5. Composite canvas rendering: single map PhotoImage & in-place palette selection rect
            app.tab_map.redraw_map()
            img_items = [i for i in app.tab_map.map_canvas.find_all() if app.tab_map.map_canvas.type(i) == "image"]
            self.assertEqual(len(img_items), 1)
            self.assertEqual(len(app.tab_map.palette_canvas.find_all()), 2)
            app.tab_map._select_tile_id(48)
            self.assertEqual(len(app.tab_map.palette_canvas.find_all()), 2)

            # 6. All PanedWindows use non-opaque gray preview sash mode & Win32 outline drag attaches cleanly
            from src.harness.window_opt import PREVIEW_GRAY_SASH_HEX, PreviewPanedWindow

            def _collect_paned(widget: tk.Misc) -> list:
                found = [widget] if isinstance(widget, PreviewPanedWindow) else []
                for ch in widget.winfo_children():
                    found.extend(_collect_paned(ch))
                return found

            paneds = _collect_paned(root)
            self.assertEqual(len(paneds), 5)
            for pw in paneds:
                self.assertFalse(bool(pw.cget("opaqueresize")))
                self.assertEqual(str(pw.cget("bg")).lower(), PREVIEW_GRAY_SASH_HEX.lower())
            if sys.platform == "win32":
                self.assertIsNotNone(app._outline_drag_ctrl)
        finally:
            root.destroy()


class TestStudioBattleFormulaPanel(unittest.TestCase):
    """Studio tab 2 battle formula panel: preset selection, overrides, validation, comparison, undo."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="rpgc_bfp_"))
        generate_sample_project(self.tmp)
        self.ir_dir = self.tmp / "ir"

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _project_formula(self):
        return load_json(self.ir_dir / "ir_project.json").get("battle_formula")

    def test_select_preset_override_apply_compare_and_undo(self) -> None:
        try:
            root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(f"Tkinter display unavailable: {exc}")
            return
        root.withdraw()
        try:
            app = StudioApp(root, ir_dir=self.ir_dir, graphics_dir=self.tmp / "Graphics")
            root.update_idletasks()
            panel = app.tab_db.formula_panel

            # Defaults: no stored setting -> RMXP preset, empty overrides
            self.assertEqual(panel._current_preset_id(), "rmxp")
            self.assertIn("기본값", panel.lbl_saved.cget("text"))
            self.assertEqual(panel.form_config(), {"preset": "rmxp"})

            # Comparison table: every preset row, saved preset starred, no unsaved row yet
            rows = panel.run_comparison()
            self.assertEqual(len(rows), len(PRESETS))
            self.assertTrue(rows[0]["preset"].startswith("★ rmxp"))
            self.assertEqual(len(panel.tree_cmp.get_children()), len(PRESETS))

            # Invalid custom formula: rejected, nothing written
            panel.attack_var.set("a.atk * 4 - b.def *")
            self.assertIsNone(panel.validate_form())
            self.assertIn("오류", panel.lbl_status.cget("text"))
            with patch("src.harness.studio_tabs.messagebox.showerror") as err:
                self.assertFalse(panel.apply_form())
                err.assert_called_once()
            self.assertIsNone(self._project_formula())
            panel.attack_var.set("")
            panel.crit_var.set("abc")
            self.assertIsNone(panel.validate_form())
            panel.crit_var.set("")

            # Select VX Ace + override critical multiplier and attack formula; unsaved row appears in comparison
            panel.preset_var.set(panel._preset_label("rmvxace"))
            panel._on_preset_changed()
            self.assertIn("a.atk * 4 - b.def * 2", panel.lbl_attack_hint.cget("text"))
            panel.crit_var.set("2.5")
            panel.attack_var.set("Math.max(1, a.atk * 3 - b.def)")
            rows = panel.run_comparison()
            self.assertEqual(len(rows), len(PRESETS) + 1)
            self.assertTrue(rows[0]["preset"].startswith("입력값"))
            self.assertIsNotNone(panel.validate_form())

            self.assertTrue(panel.apply_form())
            root.update_idletasks()
            stored = self._project_formula()
            self.assertEqual(
                stored, {"preset": "rmvxace", "attack": "Math.max(1, a.atk * 3 - b.def)", "critical_multiplier": 2.5}
            )
            self.assertIn("rmvxace", panel.lbl_saved.cget("text"))
            self.assertIn('"battle_formula": "rmvxace"', app.tab_db.txt_balance.get("1.0", "end"))
            self.assertEqual(HeadlessGameSession(self.ir_dir).battle_formula.critical_multiplier, 2.5)

            # Clear overrides -> preset only
            panel.clear_overrides()
            self.assertTrue(panel.apply_form())
            self.assertEqual(self._project_formula(), {"preset": "rmvxace"})

            # Undo twice restores the original (no setting) and the panel follows
            app.action_undo()
            self.assertEqual(self._project_formula()["critical_multiplier"], 2.5)
            self.assertEqual(panel.crit_var.get(), "2.5")
            app.action_undo()
            root.update_idletasks()
            self.assertIsNone(self._project_formula())
            self.assertEqual(panel._current_preset_id(), "rmxp")
            self.assertEqual(panel.attack_var.get(), "")

            # Changes made from the console are reflected in the panel
            res = app.agent_dock.run_command_text("battle-formula set --preset rm2k --attack-variance 10")
            self.assertTrue(res["ok"], res)
            self.assertEqual(panel._current_preset_id(), "rm2k")
            self.assertEqual(panel.attack_var_pct.get(), "10")
        finally:
            root.destroy()


class TestStudioWorkspaceLifecycleAndScriptUndo(unittest.TestCase):
    """Tests v0.1.4 features: default blank project, script Undo safety, '*' dirty title, and close save prompt."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="rpgc_v014_"))
        generate_sample_project(self.tmp)
        self.ir_dir = self.tmp / "ir"

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_default_blank_project_on_startup_and_save_as(self) -> None:
        try:
            root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(f"Tkinter display unavailable: {exc}")
            return
        root.withdraw()
        try:
            app = StudioApp(root)
            root.update_idletasks()
            self.assertTrue(app._is_ephemeral_blank)
            self.assertFalse(app._dirty)
            self.assertFalse(root.title().startswith("*"))
            self.assertIn("새 빈 프로젝트", root.title())
            maps = list_maps(app.ir_dir)
            self.assertEqual(len(maps), 1)
            self.assertEqual(maps[0]["name"], "맵 1")
            ev_data = load_json(app.ir_dir / "maps" / "map_001.json").get("events", [])
            self.assertEqual(ev_data, [])

            # Mutate blank project -> '*' appears in window title
            app.tab_map._append_ev_cmds([{"op": "say", "lines": ["Hi"], "indent": 0}])
            root.update_idletasks()
            self.assertTrue(app._dirty)
            self.assertTrue(root.title().startswith("*"))

            # Save blank project into explicit directory -> clears ephemeral & '*'
            dest_dir = self.tmp / "saved_blank"
            self.assertTrue(app.save_project(target_dir=dest_dir))
            self.assertFalse(app._is_ephemeral_blank)
            self.assertFalse(app._dirty)
            self.assertFalse(root.title().startswith("*"))
            self.assertTrue((dest_dir / "ir" / "ir_project.json").is_file())
            self.assertTrue((dest_dir / "rmxp" / "Data" / "Map001.rxdata").is_file())
        finally:
            try:
                app._cleanup_ephemeral_dir()
            except Exception:
                pass
            if root.winfo_exists():
                root.destroy()

    def test_script_text_undo_preserves_content_across_editors(self) -> None:
        try:
            root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(f"Tkinter display unavailable: {exc}")
            return
        root.withdraw()
        try:
            app = StudioApp(root, ir_dir=self.ir_dir, graphics_dir=self.tmp / "rmxp" / "Graphics")
            root.update_idletasks()

            # 1. Event cmds: snippet append followed by Undo must keep original commands intact
            orig_ev = app.tab_map.txt_ev_cmds.get("1.0", "end-1c")
            self.assertTrue(len(orig_ev.strip()) > 0)
            app.tab_map._append_ev_cmds([{"op": "say", "lines": ["Extra"], "indent": 0}])
            app.tab_map.txt_ev_cmds.event_generate("<<Undo>>")
            self.assertEqual(app.tab_map.txt_ev_cmds.get("1.0", "end-1c"), orig_ev)

            # 2. Common Event cmds: direct typing followed by Undo keeps loaded commands
            orig_ce = app.tab_ca.txt_ce_cmds.get("1.0", "end-1c")
            self.assertTrue(len(orig_ce.strip()) > 0)
            app.tab_ca.txt_ce_cmds.insert("end", "\n// edit")
            app.tab_ca.txt_ce_cmds.event_generate("<<Undo>>")
            self.assertEqual(app.tab_ca.txt_ce_cmds.get("1.0", "end-1c"), orig_ce)

            # 3. Animation JSON & Battle Formula skill script: Undo keeps loaded text
            orig_anim = app.tab_ca.txt_anim_json.get("1.0", "end-1c")
            self.assertTrue(len(orig_anim.strip()) > 0)
            app.tab_ca.txt_anim_json.insert("end", " ")
            app.tab_ca.txt_anim_json.event_generate("<<Undo>>")
            self.assertEqual(app.tab_ca.txt_anim_json.get("1.0", "end-1c"), orig_anim)

            app.tab_db.formula_panel.clear_overrides()
            app.tab_db.formula_panel.txt_skill.insert("1.0", "a.atk * 5")
            app.tab_db.formula_panel.txt_skill.edit_reset()
            app.tab_db.formula_panel.txt_skill.insert("end", " - b.def")
            app.tab_db.formula_panel.txt_skill.event_generate("<<Undo>>")
            self.assertEqual(app.tab_db.formula_panel.txt_skill.get("1.0", "end-1c"), "a.atk * 5")
        finally:
            if root.winfo_exists():
                root.destroy()

    def test_dirty_title_and_close_save_prompt(self) -> None:
        try:
            root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(f"Tkinter display unavailable: {exc}")
            return
        root.withdraw()
        try:
            app = StudioApp(root, ir_dir=self.ir_dir, graphics_dir=self.tmp / "rmxp" / "Graphics")
            root.update_idletasks()
            self.assertFalse(app._dirty)
            self.assertFalse(root.title().startswith("*"))

            # Modify map name via direct IR mutation + reload_all -> '*' appears
            orig_enemy = get_db_entry(self.ir_dir, "enemies", 1)["fields"]["maxhp"]
            set_db_entry(self.ir_dir, "enemies", 1, {"maxhp": orig_enemy + 50}, record_undo=True)
            app.reload_all()
            self.assertTrue(app._dirty)
            self.assertTrue(root.title().startswith("*"))

            # Cancel on exit -> keeps window open & stays dirty
            with patch("src.harness.studio_gui.messagebox.askyesnocancel", return_value=None) as ask:
                app.action_exit()
                ask.assert_called_once()
                self.assertTrue(bool(root.winfo_exists()))
                self.assertTrue(app._dirty)

            # No (discard) -> restores original disk IR snapshot & clears dirty
            with patch("src.harness.studio_gui.messagebox.askyesnocancel", return_value=False) as ask:
                self.assertTrue(app.confirm_discard_dirty())
                ask.assert_called_once()
                self.assertFalse(app._dirty)
                self.assertEqual(get_db_entry(self.ir_dir, "enemies", 1)["fields"]["maxhp"], orig_enemy)

            # Re-dirty and Yes (save) -> closes cleanly & persists edit
            set_db_entry(self.ir_dir, "enemies", 1, {"maxhp": orig_enemy + 77}, record_undo=True)
            app.reload_all()
            self.assertTrue(app._dirty)
            with patch("src.harness.studio_gui.messagebox.askyesnocancel", return_value=True) as ask:
                app.action_exit()
                ask.assert_called_once()
            self.assertEqual(get_db_entry(self.ir_dir, "enemies", 1)["fields"]["maxhp"], orig_enemy + 77)
        finally:
            try:
                if root.winfo_exists():
                    root.destroy()
            except Exception:
                pass


if __name__ == "__main__":
    unittest.main()
