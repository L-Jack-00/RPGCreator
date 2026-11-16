# -*- coding: utf-8 -*-
"""
Comprehensive tests for the clean-room native mruby 4.0 VM, RGSS1 & RGSS3 runtimes,
User RTP resource resolver, Script IDE CRUD & Undo/Redo, Studio Tab 6, Pygame runner, and CLI.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
import zlib
from pathlib import Path
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.core.editor import (
    create_script,
    delete_script,
    get_rgss_runtime_config,
    import_user_scripts,
    list_scripts,
    move_script,
    read_script,
    redo_last_edit,
    save_script,
    set_rgss_runtime_config,
    undo_last_edit,
)
from src.core.mruby_vm import MrubyExecutionError, MrubyRgssVM, resolve_mruby_dll_path, run_project_rgss_scripts
from src.core.rgss_runtime import (
    UserRgssResourceResolver,
    parse_ogg_loop_tags,
)
from src.core.rmxp_marshal import RubyObject, marshal_dumps
from src.core.sample_generator import generate_blank_project


class MrubyRgssVmTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.proj_dir = self.root / "proj"
        generate_blank_project(self.proj_dir, title="RGSS Verify")
        self.ir_dir = self.proj_dir / "ir"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_dll_present_and_language_core_features(self) -> None:
        dll_path = resolve_mruby_dll_path()
        self.assertTrue(dll_path.is_file())
        with MrubyRgssVM(self.ir_dir, version="rgss3", max_frames=10) as vm:
            self.assertIn("mruby", vm.mruby_version.lower())
            vm.eval(
                """
class CustomCounter
  def initialize
    @v = 10
  end
  attr_reader :v
end
class CustomCounter
  alias orig_init initialize
  def initialize
    orig_init
    @v += 5
  end
end
fb = Fiber.new do |x|
  y = Fiber.yield(x * 2)
  y + 1
end
r1 = fb.resume(7)
r2 = fb.resume(30)
c = CustomCounter.new
compressed = Zlib::Deflate.deflate("Hello RGSS World")
restored = Zlib::Inflate.inflate(compressed)
rx_out = "HP:120 SP:45".gsub(/([A-Z]+):([0-9]+)/i) { |m| m.downcase }
puts [c.v, r1, r2, restored, rx_out].join("|")
"""
            )
            self.assertEqual(vm.session.logs, ["15|14|31|Hello RGSS World|hp:120 sp:45"])

    def test_rgss1_and_rgss3_rendering_tilemap_sprite_window(self) -> None:
        # Prepare synthetic tileset PNG in user RTP dir (zero bundled copyrighted assets)
        rtp1 = self.root / "UserRTP1"
        tileset_dir = rtp1 / "Graphics" / "Tilesets"
        tileset_dir.mkdir(parents=True, exist_ok=True)
        ts_img = Image.new("RGBA", (256, 256), (0, 0, 0, 0))
        ts_img.paste(Image.new("RGBA", (32, 32), (220, 40, 60, 255)), (0, 0))
        ts_img.save(tileset_dir / "TestSet.png")

        set_rgss_runtime_config(self.ir_dir, version="rgss1", rtp_paths={"rgss1": [str(rtp1)], "rgss3": []})

        with MrubyRgssVM(self.ir_dir, version="rgss1", max_frames=5) as vm:
            vm.eval(
                """
tm = Tilemap.new
tm.tileset = Bitmap.new("Graphics/Tilesets/TestSet")
md = Table.new(20, 15, 3)
md[0, 0, 0] = 384
tm.map_data = md
tm.update
sp = Sprite.new
sp.bitmap = Bitmap.new(16, 16)
sp.bitmap.fill_rect(0, 0, 16, 16, Color.new(20, 200, 80, 255))
sp.x = 64
sp.y = 64
sp.z = 50
w = Window.new
w.x = 120
w.y = 120
w.width = 180
w.height = 90
w.contents = Bitmap.new(148, 58)
w.contents.fill_rect(0, 0, 40, 20, Color.new(255, 255, 0, 255))
Graphics.update
"""
            )
            self.assertEqual(vm.session.width, 640)
            self.assertEqual(vm.session.height, 480)
            self.assertEqual(vm.session.frame_rate, 40)
            frame = vm.session.last_frame
            self.assertEqual(frame.getpixel((10, 10))[:3], (220, 40, 60))
            self.assertEqual(frame.getpixel((70, 70))[:3], (20, 200, 80))

        # Now verify RGSS3 mode resolution + 4-arg Window + gradient_fill_rect + load_data
        with MrubyRgssVM(self.ir_dir, version="rgss3", max_frames=5) as vm3:
            vm3.eval(
                """
sys = load_data("Data/System.rvdata2")
w = Window.new(20, 20, 200, 100)
w.contents.gradient_fill_rect(0, 0, 100, 40, Color.new(10, 20, 200), Color.new(200, 40, 20), false)
Graphics.update
puts sys.game_title
"""
            )
            self.assertEqual(vm3.session.width, 544)
            self.assertEqual(vm3.session.height, 416)
            self.assertEqual(vm3.session.frame_rate, 60)
            self.assertEqual(vm3.session.logs, ["RGSS Verify"])

    def test_marshal_load_and_error_reporting(self) -> None:
        raw_blob = marshal_dumps(
            RubyObject("RPG::Actor", {"@id": 7, "@name": "TestHero", "@initial_level": 12}),
            encoding="utf-8",
        )
        blob_path = self.proj_dir / "hero.bin"
        blob_path.write_bytes(raw_blob)
        with MrubyRgssVM(self.ir_dir, version="rgss3", max_frames=5) as vm:
            vm.eval(
                f"""
raw = File.binread({json.dumps(str(blob_path))})
actor = Marshal.load(raw)
puts actor.name + ":" + actor.initial_level.to_s
"""
            )
            self.assertEqual(vm.session.logs, ["TestHero:12"])
            with self.assertRaises(MrubyExecutionError) as ctx:
                vm.eval("undefined_method_call_xyz")
            self.assertIn("NoMethodError", str(ctx.exception))

    def test_user_rtp_isolation_ogg_loop_and_no_bundled_copyright_scripts(self) -> None:
        # Build OGG VorbisComment sample packet cleanly
        fake_ogg = b"OggS\x00\x02dummyVORBIS\x00LOOPSTART=12345\x00LOOPLENGTH=67890\x00"
        tags = parse_ogg_loop_tags(fake_ogg)
        self.assertEqual(tags, {"LOOPSTART": 12345, "LOOPLENGTH": 67890})

        rtp3 = self.root / "ExternalVxaRTP"
        (rtp3 / "Graphics" / "Characters").mkdir(parents=True, exist_ok=True)
        Image.new("RGBA", (32, 32), (90, 120, 240, 255)).save(rtp3 / "Graphics" / "Characters" / "RtpHero.png")

        # Without RTP connected -> cannot resolve RtpHero
        resolver_missing = UserRgssResourceResolver(self.ir_dir, version="rgss3")
        self.assertFalse(resolver_missing.exists("Graphics/Characters/RtpHero"))

        # With user RTP configured -> resolves cleanly
        set_rgss_runtime_config(self.ir_dir, version="rgss3", rtp_paths={"rgss1": [], "rgss3": [str(rtp3)]})
        resolver_ok = UserRgssResourceResolver(self.ir_dir, version="rgss3")
        self.assertTrue(resolver_ok.exists("Graphics/Characters/RtpHero"))
        self.assertTrue(resolver_ok.inspect_status()["has_user_rtp"])

        # Strict clean-room verification: official base script templates must NOT be shipped in src/ or sample_workspace/scripts
        src_rb_files = list((ROOT / "src").rglob("*.rb"))
        self.assertEqual([p.name for p in src_rb_files], ["rgss_prelude.rb"])
        sample_scripts = list((ROOT / "sample_workspace" / "ir" / "scripts").glob("*.rb"))
        for sp in sample_scripts:
            self.assertNotIn("class Game_Actor", sp.read_text(encoding="utf-8", errors="ignore"))
            self.assertNotIn("class Scene_Title", sp.read_text(encoding="utf-8", errors="ignore"))

    def test_script_crud_import_and_undo_redo(self) -> None:
        s0 = list_scripts(self.ir_dir)
        self.assertEqual(len(s0), 1)
        added = create_script(self.ir_dir, "MyPlugin", code="puts 'plugin_v1'\n")
        self.assertEqual(added["index"], 1)
        save_script(self.ir_dir, 1, code="puts 'plugin_v2'\n", title="MyPluginV2")
        self.assertEqual(read_script(self.ir_dir, 1)["title"], "MyPluginV2")
        move_script(self.ir_dir, 1, 0)
        self.assertEqual(list_scripts(self.ir_dir)[0]["title"], "MyPluginV2")

        # Undo should move slot back cleanly
        undo_last_edit(self.ir_dir)
        self.assertEqual(list_scripts(self.ir_dir)[1]["title"], "MyPluginV2")
        redo_last_edit(self.ir_dir)
        self.assertEqual(list_scripts(self.ir_dir)[0]["title"], "MyPluginV2")

        # Import user-supplied Scripts.rvdata2 binary cleanly
        ext_scripts = self.root / "Scripts.rvdata2"
        payload = [
            [101, b"UserVocab", zlib.compress(b"$user_vocab = 'CustomOK'\n")],
            [102, b"UserMain", zlib.compress(b"puts $user_vocab\nGraphics.update\n")],
        ]
        ext_scripts.write_bytes(marshal_dumps(payload, encoding="utf-8"))
        imp = import_user_scripts(self.ir_dir, ext_scripts, replace=True)
        self.assertEqual(imp["imported"], 2)
        run_res = run_project_rgss_scripts(self.ir_dir, max_frames=5)
        self.assertTrue(run_res["ok"], run_res)
        self.assertEqual(run_res["logs"], ["CustomOK"])
        delete_script(self.ir_dir, 0)
        self.assertEqual(len(list_scripts(self.ir_dir)), 1)

    def test_studio_tab6_and_script_undo_and_dirty_star(self) -> None:
        try:
            import tkinter as tk
            from src.harness.studio_gui import StudioApp

            root = tk.Tk()
            root.withdraw()
        except Exception as exc:
            self.skipTest(f"Tkinter display unavailable in environment: {exc}")

        try:
            app = StudioApp(root, ir_dir=self.ir_dir)
            app.notebook.select(app.tab_scripts)
            root.update_idletasks()
            self.assertFalse(root.title().startswith("*"))

            # Switching scripts uses reset_text_with_content so Ctrl+Z in Text does not erase preloaded script
            before_text = app.tab_scripts.txt_code.get("1.0", "end-1c")
            self.assertTrue(len(before_text) > 0)
            try:
                app.tab_scripts.txt_code.edit_undo()
            except tk.TclError:
                pass
            self.assertEqual(app.tab_scripts.txt_code.get("1.0", "end-1c"), before_text)

            # Edit script text -> triggers dirty '*' prefix and saves cleanly
            app.tab_scripts.txt_code.insert("end", "\nputs 'studio_tab_edited'\n")
            app.tab_scripts.txt_code.edit_modified(True)
            root.update()
            self.assertTrue(root.title().startswith("*"))
            self.assertTrue(app.save_project())
            self.assertFalse(root.title().startswith("*"))
            saved = read_script(self.ir_dir, 0)
            self.assertIn("studio_tab_edited", saved["code"])

            # Clear RTP logs cleanly and headless VM test logs rtp=... status
            app.tab_scripts.clear_rtp_folders()
            app.tab_scripts.run_headless_vm_test()
            console_dump = app.agent_dock.log_box.get("1.0", "end-1c")
            self.assertIn("[RTP 목록 비움]", console_dump)
            self.assertIn("rtp=[]", console_dump)
        finally:
            try:
                root.destroy()
            except Exception:
                pass

    def test_cli_scripts_rgss_and_pygame_runner(self) -> None:
        # CLI script add + rgss run --json
        add_p = subprocess.run(
            [
                sys.executable,
                str(ROOT / "run_app.py"),
                "scripts",
                "add",
                str(self.ir_dir),
                "--title",
                "CliMain",
                "--code",
                "puts 'cli_vm_ok'\n2.times { Graphics.update }\n",
                "--json",
            ],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertEqual(json.loads(add_p.stdout)["title"], "CliMain")

        run_p = subprocess.run(
            [
                sys.executable,
                str(ROOT / "run_app.py"),
                "rgss",
                "run",
                str(self.ir_dir),
                "--version",
                "rgss3",
                "--max-frames",
                "5",
                "--json",
            ],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            check=True,
        )
        run_data = json.loads(run_p.stdout)
        self.assertTrue(run_data["ok"])
        self.assertIn("cli_vm_ok", run_data["logs"])
        self.assertEqual(run_data["frame_count"], 2)

        # Headless SDL Pygame RGSS script runner smoke check
        env = dict(os.environ)
        env["SDL_VIDEODRIVER"] = "dummy"
        env["SDL_AUDIODRIVER"] = "dummy"
        play_p = subprocess.run(
            [
                sys.executable,
                str(ROOT / "run_app.py"),
                "rgss",
                "play",
                str(self.ir_dir),
                "--version",
                "rgss3",
                "--max-frames",
                "2",
            ],
            cwd=str(ROOT),
            env=env,
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertEqual(play_p.returncode, 0)


if __name__ == "__main__":
    unittest.main()
