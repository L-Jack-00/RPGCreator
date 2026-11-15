# -*- coding: utf-8 -*-
"""
Zero-CLI GUI Executable Entrypoint for RPG Creator Studio.
Supports direct double-click launch, dragging an IR project folder onto the icon,
or re-dispatching `play` / CLI arguments when packaged as a standalone executable.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# Windowed PyInstaller/pyw double-click leaves stdout/stderr as None; bind devnull safely.
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w", encoding="utf-8")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w", encoding="utf-8")

_APP_DIR = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
if str(_APP_DIR) not in sys.path:
    sys.path.insert(0, str(_APP_DIR))

import run_app  # noqa: E402,F401  (ensures PyInstaller bundles all CLI & engine modules)
from src.harness.studio_gui import launch_studio  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args:
        cand = Path(args[0])
        if cand.is_dir():
            return launch_studio(ir_dir=cand)
        return run_app.main(args)
    return launch_studio()


if __name__ == "__main__":
    raise SystemExit(main())
