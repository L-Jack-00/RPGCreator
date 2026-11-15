# -*- coding: utf-8 -*-
"""
External AI CLI bridge tests.
Verifies that the Studio context prompt reaches an external CLI intact, both via stdin
and via `{prompt}` substitution, without being interpreted by a shell.
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from src.core.editor import get_db_entry
from src.core.sample_generator import generate_sample_project
from src.harness.agent_bridge import _run_external_agent, execute_studio_command

TRICKY_PROMPT = 'Line1 "quoted" `whoami` $(echo pwned) $HOME\nLine2 \'single\' & | ; > out.txt'


def _py_cmd(script_path: Path, *extra: str) -> str:
    parts = [f'"{sys.executable}"', f'"{script_path}"', *extra]
    return " ".join(parts)


class TestExternalAgentCli(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="rpgc_ext_"))
        generate_sample_project(self.tmp)
        self.ir_dir = self.tmp / "ir"
        self.scripts = self.tmp / "scripts"
        self.scripts.mkdir()
        self.echo_arg = self.scripts / "echo_arg.py"
        self.echo_arg.write_text(
            "import sys, json\nsys.stdout.write(json.dumps({'echo': sys.argv[1]}))\n", encoding="utf-8"
        )
        self.echo_stdin = self.scripts / "echo_stdin.py"
        self.echo_stdin.write_text(
            "import sys, json\nsys.stdout.write(json.dumps({'echo': sys.stdin.read()}))\n", encoding="utf-8"
        )

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _echo(self, res: dict) -> str:
        self.assertTrue(res["ok"], res.get("summary"))
        return json.loads(res["result"]["stdout"])["echo"]

    def test_prompt_placeholder_is_passed_verbatim_without_shell(self) -> None:
        res = _run_external_agent(_py_cmd(self.echo_arg, '"{prompt}"'), TRICKY_PROMPT, self.ir_dir)
        self.assertEqual(self._echo(res), TRICKY_PROMPT)
        self.assertFalse((self.ir_dir / "out.txt").exists())

    def test_unquoted_placeholder_also_safe(self) -> None:
        res = _run_external_agent(_py_cmd(self.echo_arg, "{prompt}"), TRICKY_PROMPT, self.ir_dir)
        self.assertEqual(self._echo(res), TRICKY_PROMPT)

    def test_stdin_delivery_without_placeholder(self) -> None:
        res = _run_external_agent(_py_cmd(self.echo_stdin), TRICKY_PROMPT, self.ir_dir)
        self.assertEqual(self._echo(res).replace("\r\n", "\n"), TRICKY_PROMPT)

    def test_missing_executable_reports_error(self) -> None:
        res = _run_external_agent("rpgc-definitely-missing-cli -p {prompt}", "hi", self.ir_dir)
        self.assertFalse(res["ok"])
        self.assertFalse(res["mutated"])

    def test_real_context_packet_via_placeholder_applies_blueprint(self) -> None:
        # The real packet contains JSON quotes and Markdown backticks; it must survive argv delivery.
        script = self.scripts / "agent.py"
        script.write_text(
            "import sys, json\n"
            "packet = sys.argv[1]\n"
            "assert '# RPG Creator Studio Context' in packet and '`' in packet and '\"' in packet\n"
            "print('```json')\n"
            "print(json.dumps({'db': [{'table': 'enemies', 'id': 1, 'fields': {'gold': 777}}]}))\n"
            "print('```')\n",
            encoding="utf-8",
        )
        res = execute_studio_command(
            self.ir_dir,
            "슬라임이 골드 777을 떨어뜨리게 해줘 (external)",
            external_agent_cmd=_py_cmd(script, '"{prompt}"'),
            apply_external=True,
        )
        self.assertTrue(res["ok"], res.get("summary"))
        self.assertEqual(res["kind"], "external_blueprint")
        self.assertEqual(get_db_entry(self.ir_dir, "enemies", 1)["fields"]["gold"], 777)


if __name__ == "__main__":
    unittest.main()
