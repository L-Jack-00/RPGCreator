# -*- coding: utf-8 -*-
"""Regression tests for animation authoring, export and playback (player + studio preview)."""
from __future__ import annotations

import copy
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PIL import Image

from src.core.animation_cells import extract_animation_frame_cells
from src.core.editor import set_animation, undo_last_edit
from src.core.ir_converter import pack_ir_to_rmxp, unpack_rmxp_to_ir
from src.core.sample_generator import generate_sample_project
from src.core.simulator import ANIMATION_TICKS_PER_FRAME, HeadlessGameSession
from src.core.utils import load_json

COLORS = [(255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 0), (255, 0, 255)]


def _make_probe_sheet(graphics_dir: Path) -> None:
    """Five 192x192 patterns, each a solid colour square, so rendered frames are identifiable."""
    (graphics_dir / "Animations").mkdir(parents=True, exist_ok=True)
    sheet = Image.new("RGBA", (960, 192), (0, 0, 0, 0))
    for i, col in enumerate(COLORS):
        sheet.paste(col + (255,), (i * 192 + 40, 40, i * 192 + 152, 152))
    sheet.save(graphics_dir / "Animations" / "Probe.png")


class TestAnimationData(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.tmp = Path(self._td.name)
        generate_sample_project(self.tmp / "ws")
        self.ir = self.tmp / "ws" / "ir"

    def tearDown(self) -> None:
        self._td.cleanup()

    def _anim(self, aid: int):
        return load_json(self.ir / "database" / "animations.json")[aid]

    def test_per_frame_multi_cell_layout_matches_rmxp_table(self):
        set_animation(self.ir, anim_id=3, name="multi", cells=[
            [{"pattern": 0, "x": -30, "y": 5}, {"pattern": 3, "x": 40, "y": -10, "zoom": 150, "opacity": 128}],
            [{"pattern": 1, "mirror": 1}],
        ])
        anim = self._anim(3)
        self.assertEqual(anim["fields"]["frame_max"], 2)
        cells = extract_animation_frame_cells(anim, 0)
        self.assertEqual(
            [(c["pattern"], c["x"], c["y"], c["zoom"], c["opacity"]) for c in cells],
            [(0, -30, 5, 100, 255), (3, 40, -10, 150, 128)],
        )
        # Table(cell_max, 8) is property-major: patterns of all cells come first
        self.assertEqual(anim["fields"]["frames"][0]["fields"]["cell_data"]["data"][:2], [0, 3])
        self.assertEqual(extract_animation_frame_cells(anim, 1)[0]["mirror"], 1)

    def test_studio_json_round_trip_keeps_frames_and_timings(self):
        set_animation(self.ir, anim_id=3, name="rt", cells=[[{"pattern": i, "x": i * 4}] for i in range(4)],
                      timings=[{"frame": 2, "se_name": "Slash1", "flash_color": [255, 0, 0, 128]}])
        before = copy.deepcopy(self._anim(3)["fields"])
        # The studio passes the JSON editor's {"frames", "timings"} straight back in.
        set_animation(self.ir, anim_id=3, name="rt", frame_max=9, cells=before["frames"], timings=before["timings"])
        after = self._anim(3)["fields"]
        self.assertEqual(after["frames"], before["frames"])
        self.assertEqual(after["timings"], before["timings"])

    def test_pack_to_rxdata_keeps_ruby_classes(self):
        set_animation(self.ir, anim_id=3, name="pack", cells=[[{"pattern": 0}, {"pattern": 2, "x": 50}]],
                      timings=[{"frame": 0, "se_name": "Slash1", "flash_color": [255, 0, 0, 128]}])
        rx, ir2 = self.tmp / "rx", self.tmp / "ir2"
        pack_ir_to_rmxp(self.ir, rx)
        unpack_rmxp_to_ir(rx, ir2)
        anim = load_json(ir2 / "database" / "animations.json")[3]
        self.assertEqual(anim["class"], "RPG::Animation")
        self.assertEqual(anim["fields"]["frames"][0]["class"], "RPG::Animation::Frame")
        self.assertEqual(anim["fields"]["frames"][0]["fields"]["cell_data"]["__type__"], "Table")
        self.assertEqual(anim["fields"]["timings"][0]["fields"]["flash_color"]["__type__"], "Color")
        self.assertEqual(extract_animation_frame_cells(anim, 0), extract_animation_frame_cells(self._anim(3), 0))

    def test_set_animation_is_one_undo_step(self):
        before = self._anim(1)
        set_animation(self.ir, anim_id=1, name="changed")
        undo_last_edit(self.ir)
        self.assertEqual(self._anim(1), before)

    def test_show_animation_this_event_targets_the_event(self):
        sess = HeadlessGameSession(self.ir)
        sess._run_commands([{"op": "show_animation", "character_id": 0, "animation_id": 1}], 0, 1, 7)
        self.assertEqual(sess.pending_animations, [{"character_id": 7, "animation_id": 1}])

    def test_battle_animation_cue_covers_all_frames(self):
        set_animation(self.ir, anim_id=4, name="long", cells=[[{"pattern": i % 5}] for i in range(20)])
        sess = HeadlessGameSession(self.ir, interactive_battle=True)
        cue = sess._battle_anim_cue(1, 0, 4)
        self.assertEqual(cue["frames"], 20 * ANIMATION_TICKS_PER_FRAME)
        self.assertEqual(cue["total_frames"], cue["frames"])


class TestPlayerAnimationPlayback(unittest.TestCase):
    def test_player_steps_through_every_frame(self):
        from src.harness.player_gui import launch_player

        with tempfile.TemporaryDirectory() as td:
            ws = Path(td) / "ws"
            generate_sample_project(ws)
            _make_probe_sheet(ws / "rmxp" / "Graphics")
            set_animation(ws / "ir", anim_id=2, name="Probe", animation_name="Probe",
                          cells=[[{"pattern": i, "blend_type": 0}] for i in range(5)])
            seen = []

            def _on_frame(tick, screen, sess):
                if tick == 2:
                    sess._run_commands([{"op": "show_animation", "character_id": -1, "animation_id": 2}], 0, sess.map_id, 0)
                w, h = screen.get_size()
                found = {COLORS.index(tuple(screen.get_at((x, y)))[:3])
                         for x in range(0, w, 4) for y in range(0, h, 4)
                         if tuple(screen.get_at((x, y)))[:3] in COLORS}
                if found:
                    seen.append(min(found))

            prev = {k: os.environ.get(k) for k in ("SDL_VIDEODRIVER", "SDL_AUDIODRIVER")}
            os.environ["SDL_VIDEODRIVER"] = "dummy"
            os.environ["SDL_AUDIODRIVER"] = "dummy"
            try:
                launch_player(ws / "ir", max_frames=24, on_frame=_on_frame)
            finally:
                for k, v in prev.items():
                    if v is None:
                        os.environ.pop(k, None)
                    else:
                        os.environ[k] = v
            expected = [i for i in range(5) for _ in range(ANIMATION_TICKS_PER_FRAME)]
            self.assertEqual(seen, expected)


class TestStudioAnimationPreview(unittest.TestCase):
    def test_play_composites_real_frames_and_save_keeps_data(self):
        import tkinter as tk
        from tkinter import messagebox

        try:
            root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(f"Tkinter display unavailable: {exc}")
            return
        root.withdraw()
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td) / "ws"
            generate_sample_project(ws)
            gfx = ws / "rmxp" / "Graphics"
            _make_probe_sheet(gfx)
            ir = ws / "ir"
            set_animation(ir, anim_id=1, name="Probe", animation_name="Probe", cells=[
                [{"pattern": 0, "x": -80, "zoom": 50, "blend_type": 0}, {"pattern": 2, "x": 80, "zoom": 50, "blend_type": 0}],
                [{"pattern": 1, "blend_type": 1}],
            ])
            errors = []
            orig_err = messagebox.showerror
            messagebox.showerror = lambda *a, **k: errors.append(a)
            try:
                from src.harness.studio_gui import StudioApp

                app = StudioApp(root, ir_dir=ir, graphics_dir=gfx)
                root.update_idletasks()
                tab = app.tab_ca
                tab._load_anim(1)
                shots = []
                orig = app.atlas.compose_animation_frame

                def _spy(name, cells, size_px=160, view_px=320):
                    img = orig(name, cells, size_px, view_px)
                    shots.append((len(cells), img))
                    return img

                app.atlas.compose_animation_frame = _spy
                tab._toggle_anim_play()
                tab._stop_anim_play()
                tab._toggle_anim_play()  # restart: tick 0 again
                tab._step_anim_play()
                tab._stop_anim_play()
                self.assertEqual([n for n, _ in shots[-2:]], [2, 1])
                frame0 = shots[-2][1]
                self.assertEqual(frame0.getpixel((40, 80))[:3], (255, 0, 0))
                self.assertEqual(frame0.getpixel((120, 80))[:3], (0, 0, 255))

                before = load_json(ir / "database" / "animations.json")[1]["fields"]["frames"]
                tab.save_anim()
                after = load_json(ir / "database" / "animations.json")[1]["fields"]["frames"]
                self.assertEqual(errors, [])
                self.assertEqual(after, before)
            finally:
                messagebox.showerror = orig_err
                root.destroy()


if __name__ == "__main__":
    unittest.main()
