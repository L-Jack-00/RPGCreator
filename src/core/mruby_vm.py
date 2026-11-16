# -*- coding: utf-8 -*-
"""
Native mruby 4.0 VM Bridge for RGSS1 (RMXP) & RGSS3 (VX Ace) Scripts.

Loads native/rpgc_mruby.dll via ctypes, boots the clean-room RGSS prelude
(rgss_prelude.rb), and routes Ruby Graphics/Input/Audio/Bitmap/Sprite/Window/
Tilemap/Marshal/Zlib operations to RgssRuntimeSession without bundling any
copyrighted RPG Maker RTP assets or official base scripts.
"""
from __future__ import annotations

import ctypes
import json
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .rgss_runtime import RgssRuntimeSession, read_user_scripts_package


_CB_TYPE = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_char_p, ctypes.c_char_p, ctypes.c_int)


class MrubyExecutionError(RuntimeError):
    """Raised when compiled native mruby reports a syntax or runtime exception."""


def resolve_mruby_dll_path() -> Path:
    """Finds rpgc_mruby.dll across frozen PyInstaller and source repository layouts."""
    candidates: List[Path] = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.append(Path(meipass) / "native" / "rpgc_mruby.dll")
        candidates.append(Path(meipass) / "rpgc_mruby.dll")
    repo_root = Path(__file__).resolve().parents[2]
    candidates.append(repo_root / "native" / "rpgc_mruby.dll")
    candidates.append(Path.cwd() / "native" / "rpgc_mruby.dll")
    for c in candidates:
        if c.is_file():
            return c
    raise FileNotFoundError(
        "native/rpgc_mruby.dll not found. Run native/build_mruby.ps1 on Windows x64 first."
    )


def _load_prelude_source() -> str:
    prelude_path = Path(__file__).resolve().with_name("rgss_prelude.rb")
    return prelude_path.read_text(encoding="utf-8")


class MrubyRgssVM:
    """In-process mruby VM wired to an RgssRuntimeSession host."""

    def __init__(
        self,
        ir_dir: Path,
        explicit_graphics_dir: Optional[Path] = None,
        version: str = "auto",
        on_frame: Optional[Callable[[Any, int], Optional[bool]]] = None,
        max_frames: Optional[int] = None,
    ) -> None:
        self.ir_dir = Path(ir_dir)
        self.max_frames = max_frames
        self._user_on_frame = on_frame
        self._dll_path = resolve_mruby_dll_path()
        self._dll = self._bind_dll(self._dll_path)

        def _frame_gate(img: Any, frame_idx: int) -> Optional[bool]:
            if self._user_on_frame is not None:
                res = self._user_on_frame(img, frame_idx)
                if res is False:
                    return False
            if self.max_frames is not None and frame_idx >= self.max_frames:
                return False
            return True

        self.session = RgssRuntimeSession(
            self.ir_dir,
            explicit_graphics_dir=explicit_graphics_dir,
            version=version,
            on_frame=_frame_gate,
        )
        self._ctx: Optional[int] = None
        self._cb_ref = _CB_TYPE(self._on_host_call)
        self._ctx = int(self._dll.rpgc_mrb_create(self._cb_ref, None))
        if not self._ctx:
            raise RuntimeError("Failed to initialize mruby VM context.")
        self.eval(_load_prelude_source(), filename="rgss_prelude.rb")

    @staticmethod
    def _bind_dll(dll_path: Path) -> ctypes.CDLL:
        dll = ctypes.CDLL(str(dll_path))
        dll.rpgc_mrb_version.restype = ctypes.c_char_p
        dll.rpgc_mrb_create.argtypes = [_CB_TYPE, ctypes.c_void_p]
        dll.rpgc_mrb_create.restype = ctypes.c_void_p
        dll.rpgc_mrb_destroy.argtypes = [ctypes.c_void_p]
        dll.rpgc_mrb_destroy.restype = None
        dll.rpgc_mrb_set_reply.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int]
        dll.rpgc_mrb_set_reply.restype = None
        dll.rpgc_mrb_set_error.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int]
        dll.rpgc_mrb_set_error.restype = None
        dll.rpgc_mrb_eval.argtypes = [
            ctypes.c_void_p,
            ctypes.c_char_p,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
        ]
        dll.rpgc_mrb_eval.restype = ctypes.c_int
        return dll

    @property
    def mruby_version(self) -> str:
        raw = self._dll.rpgc_mrb_version()
        return raw.decode("utf-8", errors="replace") if raw else "mruby"

    def _on_host_call(self, _user_data: Any, op_ptr: bytes, payload_ptr: bytes, payload_len: int) -> None:
        if not self._ctx:
            return
        try:
            op = op_ptr.decode("utf-8", errors="replace") if op_ptr else ""
            raw_json = (
                ctypes.string_at(payload_ptr, max(0, int(payload_len))).decode("utf-8", errors="replace")
                if payload_ptr and payload_len > 0
                else "{}"
            )
            payload = json.loads(raw_json) if raw_json.strip() else {}
            if not isinstance(payload, dict):
                payload = {"value": payload}
            result = self.session.dispatch(op, payload)
            encoded = json.dumps(result, ensure_ascii=False).encode("utf-8")
            self._dll.rpgc_mrb_set_reply(self._ctx, encoded, len(encoded))
        except Exception as exc:
            err = f"{type(exc).__name__}: {exc}".encode("utf-8", errors="replace")
            self._dll.rpgc_mrb_set_error(self._ctx, err, len(err))

    def eval(self, code: str, filename: str = "(script)") -> str:
        """Evaluates Ruby code in the active mruby state, returning inspect text."""
        if not self._ctx:
            raise RuntimeError("mruby VM has already been closed.")
        code_bytes = str(code).encode("utf-8")
        fname_bytes = str(filename).encode("utf-8", errors="replace")
        out_cap = 32768
        out_buf = ctypes.create_string_buffer(out_cap)
        rc = self._dll.rpgc_mrb_eval(
            self._ctx,
            fname_bytes,
            code_bytes,
            len(code_bytes),
            out_buf,
            out_cap,
        )
        text = out_buf.value.decode("utf-8", errors="replace")
        if rc != 0:
            raise MrubyExecutionError(text or f"mruby eval failed with exit code {rc}")
        return text

    def load_project_script_slots(self, external_only: Optional[bool] = None) -> List[Dict[str, Any]]:
        """Collects ordered Ruby scripts either from user's external .rxdata/.rvdata2 or ir/scripts."""
        from .editor import get_rgss_runtime_config

        cfg = get_rgss_runtime_config(self.ir_dir)
        ext_path_str = str(cfg.get("external_scripts_path", "")).strip()
        use_ext = bool(cfg.get("run_external_only", False)) if external_only is None else bool(external_only)
        if use_ext and ext_path_str:
            ext_path = Path(ext_path_str)
            if not ext_path.is_file():
                raise FileNotFoundError(f"Configured external scripts file not found: {ext_path}")
            return [
                {"title": row["title"], "file": f"{ext_path.name}:{row['title']}", "code": row["code"]}
                for row in read_user_scripts_package(ext_path)
            ]

        scripts_dir = self.ir_dir / "scripts"
        idx_file = scripts_dir / "_index.json"
        if not idx_file.is_file():
            return []
        entries = json.loads(idx_file.read_text(encoding="utf-8"))
        slots: List[Dict[str, Any]] = []
        for entry in entries:
            fname = str(entry.get("file", ""))
            fpath = scripts_dir / fname
            code = fpath.read_text(encoding="utf-8", errors="replace") if fpath.is_file() else ""
            slots.append({
                "title": str(entry.get("title", fname)),
                "file": fname,
                "code": code,
            })
        return slots

    def run_project_scripts(self, external_only: Optional[bool] = None) -> Dict[str, Any]:
        """Executes ordered project scripts inside mruby and stops cleanly when frame cap or exit arrives."""
        slots = self.load_project_script_slots(external_only=external_only)
        executed: List[str] = []
        stopped_by_frame_limit = False
        error_text: Optional[str] = None
        for slot in slots:
            code = slot["code"]
            if not code.strip():
                continue
            executed.append(slot["title"])
            try:
                self.eval(code, filename=slot["file"])
            except MrubyExecutionError as exc:
                msg = str(exc)
                if "StopIteration" in msg or "Graphics host stopped" in msg:
                    stopped_by_frame_limit = True
                    break
                error_text = msg
                break
        return {
            "ok": error_text is None,
            "mruby_version": self.mruby_version,
            "rgss_version": self.session.version,
            "resolution": [self.session.width, self.session.height],
            "scripts_total": len(slots),
            "scripts_executed": executed,
            "frame_count": self.session.frame_count,
            "stopped_by_frame_limit": stopped_by_frame_limit,
            "logs": list(self.session.logs),
            "bgm": self.session.audio.bgm,
            "se_count": len(self.session.audio.se_events),
            "error": error_text,
            "rtp_status": self.session.resolver.inspect_status(),
        }

    def close(self) -> None:
        if self._ctx:
            self._dll.rpgc_mrb_destroy(self._ctx)
            self._ctx = None

    def __enter__(self) -> "MrubyRgssVM":
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass


def run_project_rgss_scripts(
    ir_dir: Path,
    explicit_graphics_dir: Optional[Path] = None,
    version: str = "auto",
    max_frames: int = 30,
    external_only: Optional[bool] = None,
) -> Dict[str, Any]:
    """Convenience runner executing project Ruby scripts headlessly under native mruby VM."""
    with MrubyRgssVM(
        Path(ir_dir),
        explicit_graphics_dir=explicit_graphics_dir,
        version=version,
        max_frames=max_frames,
    ) as vm:
        return vm.run_project_scripts(external_only=external_only)
