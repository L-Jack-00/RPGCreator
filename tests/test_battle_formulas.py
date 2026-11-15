# -*- coding: utf-8 -*-
"""
Per-project battle formula tests: presets, sandboxed evaluator, project settings (CLI, blueprint,
validation, undo), and agreement between the game engine and balance-check for every preset.
"""
from __future__ import annotations

import io
import json
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from src.core.battle_formulas import (
    PRESETS,
    BattleFormula,
    FormulaError,
    combatant_stats,
    compile_formula,
    evaluate_formula,
)
from src.core.editor import apply_blueprint, calc_battle_damage, get_battle_formula, set_battle_formula, undo_last_edit
from src.core.sample_generator import generate_sample_project
from src.core.simulator import HeadlessGameSession
from src.core.validator import simulate_battle_balance, validate_ir_project

HERO = combatant_stats(is_actor=1, atk=18, str=16, dex=13, agi=13, int=11, pdef=12, mdef=4, hp=145, mhp=145, legacy_atk=32)
GOBLIN = combatant_stats(atk=18, str=18, dex=10, agi=10, int=8, pdef=10, mdef=8, eva=5, hp=90, mhp=90)
SMASH = {"power": 35, "atk_f": 100, "str_f": 20, "pdef_f": 100, "variance": 15}
DARK = {"power": 60, "atk_f": 0, "int_f": 100, "mdef_f": 100}


class TestFormulaPresets(unittest.TestCase):
    def test_default_is_rmxp(self) -> None:
        self.assertEqual(BattleFormula.from_project({}).preset, "rmxp")
        self.assertEqual(BattleFormula.from_config(None).preset, "rmxp")

    def test_rmxp_matches_hand_computed_game_battler_values(self) -> None:
        bf = BattleFormula("rmxp")
        # max(18 - 10/2, 0) * (20 + 16) / 20 = 13 * 36 / 20 = 23
        self.assertEqual(bf.attack_damage(HERO, GOBLIN), 23)
        # (18 - 12/2) * (20 + 18) / 20 = 12 * 38 / 20 = 22
        self.assertEqual(bf.attack_damage(GOBLIN, HERO), 22)
        # power 35 + 18 = 53; - 10*100/200 = 48; rate 20 + 16*20/100 = 23 -> 48 * 23 / 20 = 55
        self.assertEqual(bf.skill_damage(HERO, GOBLIN, SMASH), 55)
        # power 60 - 8*100/200 = 56; rate 20 + 11 = 31 -> 56 * 31 / 20 = 86
        self.assertEqual(bf.skill_damage(HERO, GOBLIN, DARK), 86)
        self.assertEqual(bf.critical_multiplier, 2.0)
        self.assertEqual(bf.attack_variance, 15)

    def test_simple_preset_reproduces_v010_formulas(self) -> None:
        bf = BattleFormula("simple")
        self.assertEqual(bf.attack_damage(HERO, GOBLIN), 54)  # max(5, 32*2 - 10)
        self.assertEqual(bf.attack_damage(GOBLIN, HERO), 8)  # max(1, 18 - (12+8)//2)
        self.assertEqual(bf.skill_damage(HERO, GOBLIN, SMASH), 71)  # max(5, 32*2 + 35//2 - 10)
        self.assertEqual(bf.critical_multiplier, 1.5)

    def test_later_engine_presets(self) -> None:
        # Unified attack stat = STR(16) + weapon ATK(18) = 34
        self.assertEqual(BattleFormula("rmvx").attack_damage(HERO, GOBLIN), 34 * 4 - 10 * 2)
        self.assertEqual(BattleFormula("rmvxace").attack_damage(HERO, GOBLIN), 116)
        self.assertEqual(BattleFormula("rmmv").attack_damage(HERO, GOBLIN), 116)
        self.assertEqual(BattleFormula("rmmz").attack_damage(HERO, GOBLIN), 116)
        self.assertEqual(BattleFormula("rm2k").attack_damage(HERO, GOBLIN), 34 // 2 - 10 // 4)
        self.assertEqual(BattleFormula("rm2k3").attack_damage(HERO, GOBLIN), 15)
        for key in ("rm2k", "rm2k3", "rmvx", "rmvxace", "rmmv", "rmmz"):
            self.assertEqual(BattleFormula(key).critical_multiplier, 3.0, key)

    def test_aliases_and_unknown_preset(self) -> None:
        self.assertEqual(BattleFormula("vxace").preset, "rmvxace")
        self.assertEqual(BattleFormula("MV").preset, "rmmv")
        self.assertEqual(BattleFormula("2003").preset, "rm2k3")
        with self.assertRaises(FormulaError):
            BattleFormula("rpg_maker_9000")

    def test_every_preset_validates(self) -> None:
        for key in PRESETS:
            BattleFormula(key).validate()

    def test_overrides_and_per_skill_formula(self) -> None:
        bf = BattleFormula("rmxp", {"attack": "a.atk * 3 - b.def", "critical_multiplier": 1.75})
        self.assertEqual(bf.attack_damage(HERO, GOBLIN), 18 * 3 - 10)
        self.assertEqual(bf.critical_multiplier, 1.75)
        skill = dict(SMASH, damage_formula="s.power * 2")
        self.assertEqual(bf.skill_damage(HERO, GOBLIN, skill), 70)
        broken = dict(SMASH, damage_formula="s.power +")
        self.assertEqual(bf.skill_damage(HERO, GOBLIN, broken), bf.skill_damage(HERO, GOBLIN, SMASH))
        with self.assertRaises(FormulaError):
            bf.skill_damage(HERO, GOBLIN, broken, strict=True)

    def test_damage_never_negative(self) -> None:
        tank = combatant_stats(atk=1, str=1, pdef=999, mdef=999)
        for key in PRESETS:
            self.assertGreaterEqual(BattleFormula(key).attack_damage(HERO, tank), 0, key)


class TestFormulaEvaluator(unittest.TestCase):
    def test_rpg_maker_style_syntax(self) -> None:
        a = {"atk": 10}
        b = {"def": 3, "def_": 3}
        self.assertEqual(evaluate_formula("a.atk * 4 - b.def * 2", a, b), 34)
        self.assertAlmostEqual(
            evaluate_formula("var x = Math.max(0, a.atk * 4 - b.def * 2); x / 3", a, b, integer_division=False), 34 / 3
        )
        self.assertEqual(evaluate_formula("a.atk / 3", a, b, integer_division=True), 3)
        self.assertEqual(evaluate_formula("a.atk > 5 && b.def < 5", a, b), True)
        self.assertEqual(evaluate_formula("1 if a.atk != 10 else 2", a, b), 2)
        self.assertEqual(evaluate_formula("a.atk / 0", a, b), 0)

    def test_rejects_unsafe_or_invalid_input(self) -> None:
        for bad in ("__import__('os')", "a.__class__", "open('x')", "(1).real", "[1, 2]", "lambda: 1", "x = 1", "a.atk if", ""):
            with self.assertRaises(FormulaError, msg=bad):
                compile_formula(bad)
        with self.assertRaises(FormulaError):
            evaluate_formula("2 ** 100", {}, {})
        with self.assertRaises(FormulaError):
            evaluate_formula("a.nope", {"atk": 1}, {})


class TestProjectBattleFormula(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="rpgc_bf_"))
        generate_sample_project(self.tmp)
        self.ir_dir = self.tmp / "ir"

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _project(self) -> dict:
        return json.loads((self.ir_dir / "ir_project.json").read_text(encoding="utf-8"))

    def test_set_show_undo(self) -> None:
        self.assertIsNone(get_battle_formula(self.ir_dir)["config"])
        res = set_battle_formula(self.ir_dir, preset="vxace", overrides={"critical_multiplier": 2.5})
        self.assertEqual(res["battle_formula"], {"preset": "rmvxace", "critical_multiplier": 2.5})
        self.assertEqual(self._project()["battle_formula"]["preset"], "rmvxace")
        self.assertEqual(HeadlessGameSession(self.ir_dir).battle_formula.critical_multiplier, 2.5)
        set_battle_formula(self.ir_dir, overrides={"critical_multiplier": ""})
        self.assertNotIn("critical_multiplier", self._project()["battle_formula"])
        undo_last_edit(self.ir_dir)
        self.assertEqual(self._project()["battle_formula"]["critical_multiplier"], 2.5)

    def test_invalid_settings_are_rejected_and_reported(self) -> None:
        with self.assertRaises(FormulaError):
            set_battle_formula(self.ir_dir, overrides={"attack": "a.atk *"})
        with self.assertRaises(ValueError):
            set_battle_formula(self.ir_dir, overrides={"bogus": 1})
        self.assertNotIn("battle_formula", self._project())
        # A hand-edited invalid setting is reported by `check` and ignored by the player.
        proj = self._project()
        proj["battle_formula"] = {"preset": "rmxp", "attack": "open('x')"}
        (self.ir_dir / "ir_project.json").write_text(json.dumps(proj, ensure_ascii=False), encoding="utf-8")
        res = validate_ir_project(self.ir_dir)
        self.assertFalse(res.passed)
        self.assertTrue(any("battle_formula" in e for e in res.errors))
        sess = HeadlessGameSession(self.ir_dir)
        self.assertEqual(sess.battle_formula.preset, "rmxp")
        self.assertTrue(any("BattleFormula" in line for line in sess.log))

    def test_blueprint_sets_formula(self) -> None:
        res = apply_blueprint(self.ir_dir, {"battle_formula": {"preset": "rmmv", "attack": "a.atk * 3 - b.def * 2"}})
        self.assertTrue(res["validated"])
        self.assertEqual(self._project()["battle_formula"]["attack"], "a.atk * 3 - b.def * 2")
        with self.assertRaises(Exception):
            apply_blueprint(self.ir_dir, {"battle_formula": {"preset": "rmxp", "skill": "s.power +"}})
        self.assertEqual(self._project()["battle_formula"]["preset"], "rmmv")

    def test_calc_compares_presets(self) -> None:
        res = calc_battle_damage(self.ir_dir, enemy_id=1, skill_id=1, presets=list(PRESETS))
        rows = {r["preset"]: r for r in res["results"]}
        self.assertEqual(set(rows), set(PRESETS))
        self.assertEqual(rows["rmxp"]["actor_attack"], BattleFormula("rmxp").attack_damage(res["actor"]["stats"], res["enemy"]["stats"]))
        self.assertIn("actor_skill", rows["rmvx"])

    def test_cli_battle_formula_commands(self) -> None:
        import run_app

        def run(*argv: str) -> dict:
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = run_app.main(["battle-formula", *argv, "--json"])
            self.assertEqual(rc, 0, buf.getvalue())
            return json.loads(buf.getvalue())

        self.assertEqual(len(run("list")), len(PRESETS))
        out = run("set", str(self.ir_dir), "--preset", "rm2k", "--attack-variance", "10")
        self.assertEqual(out["battle_formula"], {"preset": "rm2k", "attack_variance": 10})
        self.assertEqual(run("show", str(self.ir_dir))["effective"]["preset"], "rm2k")
        calc = run("calc", str(self.ir_dir), "--enemy", "2", "--all-presets")
        self.assertEqual(len(calc["results"]), len(PRESETS))
        run("set", str(self.ir_dir), "--preset", "rmxp", "--clear-overrides")
        self.assertEqual(self._project()["battle_formula"], {"preset": "rmxp"})

    def test_engine_and_balance_check_agree_for_every_preset(self) -> None:
        for key in PRESETS:
            set_battle_formula(self.ir_dir, preset=key, clear_overrides=True, record_undo=False)
            bal = {t["troop_id"]: t for t in simulate_battle_balance(self.ir_dir)["troops"]}
            self.assertEqual(simulate_battle_balance(self.ir_dir, troop_id=2)["battle_formula"], key)
            for tid in (1, 2):
                outcome = HeadlessGameSession(self.ir_dir)._resolve_battle(tid)
                self.assertEqual(outcome, "win", (key, tid))
                self.assertGreaterEqual(bal[tid]["win_rate"], 0.6, (key, bal[tid]))


if __name__ == "__main__":
    unittest.main()
