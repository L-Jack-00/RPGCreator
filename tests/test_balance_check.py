# -*- coding: utf-8 -*-
"""
balance-check regression tests.
The Monte Carlo estimator must read RMXP actor parameters (Table(6, 100): [kind, level])
the same way the in-game simulator does, and agree with the real battle outcome.
"""
from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from src.core.editor import set_db_entry
from src.core.sample_generator import generate_sample_project
from src.core.simulator import HeadlessGameSession
from src.core.validator import simulate_battle_balance


class TestBalanceCheck(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="rpgc_bal_"))
        generate_sample_project(self.tmp)
        self.ir_dir = self.tmp / "ir"

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_sample_troops_are_winnable_like_in_game(self) -> None:
        res = simulate_battle_balance(self.ir_dir)
        by_id = {r["troop_id"]: r for r in res["troops"]}
        # Previously the hero was simulated with 1 HP, so every fight ended on turn 1
        # and the sample boss (troop 2) reported a 0% win rate.
        for tid in (1, 2):
            self.assertGreaterEqual(by_id[tid]["win_rate"], 0.9, by_id[tid])
            self.assertGreater(by_id[tid]["avg_turns"], 1.0, by_id[tid])
        for tid in (1, 2):
            session = HeadlessGameSession(self.ir_dir)
            self.assertEqual(session._resolve_battle(tid), "win")

    def test_overtuned_enemy_is_reported_hard(self) -> None:
        set_db_entry(self.ir_dir, "enemies", 2, {"maxhp": 5000, "atk": 120}, record_undo=False)
        res = simulate_battle_balance(self.ir_dir, troop_id=2)
        report = res["troops"][0]
        self.assertLess(report["win_rate"], 0.25, report)
        self.assertEqual(report["status"], "overpowered")
        self.assertFalse(res["balanced"])


if __name__ == "__main__":
    unittest.main()
