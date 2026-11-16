# -*- coding: utf-8 -*-
"""
Clean-Room RGSS1 (RMXP) & RGSS3 (VX Ace) Runtime Core and User RTP Resolver.
Provides Graphics, Input, Audio (including OGG loop metadata), Viewport, Bitmap,
Sprite, Plane, Window, Tilemap, Color, Tone, Rect, Table, and Font primitives
with zero bundled copyrighted RPG Maker scripts or RTP assets.
"""
from __future__ import annotations

import base64
import io
import json
import os
import re
import struct
import sys
import zlib
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

from .autotile import resolve_autotile_quads, subtile_pixel_rect
from .rgssad import RgssadArchiveReader
from .rmxp_marshal import marshal_dumps, marshal_loads
from .utils import load_json

_GRAPHICS_EXTS: Tuple[str, ...] = (".png", ".jpg", ".jpeg", ".bmp")
_AUDIO_EXTS: Tuple[str, ...] = (".ogg", ".wav", ".mp3", ".mid", ".midi")

_KEY_NAME_TO_CODE: Dict[str, int] = {
    "DOWN": 2, "LEFT": 4, "RIGHT": 6, "UP": 8,
    "A": 11, "B": 12, "C": 13, "X": 14, "Y": 15, "Z": 16, "L": 17, "R": 18,
    "SHIFT": 21, "CTRL": 22, "ALT": 23,
    "F5": 25, "F6": 26, "F7": 27, "F8": 28, "F9": 29,
}
_CODE_TO_KEY_NAME: Dict[int, str] = {v: k for k, v in _KEY_NAME_TO_CODE.items()}


def default_rgss_config() -> Dict[str, Any]:
    """Returns default clean-room RGSS runtime & user RTP configuration."""
    return {
        "version": "auto",  # auto | rgss1 | rgss3
        "rtp_paths": {"rgss1": [], "rgss3": []},
        "external_scripts_path": "",
        "sandbox_strict": True,
    }


def get_global_rtp_config_path() -> Path:
    """Returns per-user RTP configuration path outside the project repository."""
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA") or (Path.home() / "AppData" / "Roaming"))
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config"))
    return base / "RPGCreator" / "rtp_config.json"


def load_global_rtp_config() -> Dict[str, Any]:
    """Reads per-user global RTP configuration if available."""
    cfg = default_rgss_config()
    p = get_global_rtp_config_path()
    if p.is_file():
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                rtp = raw.get("rtp_paths", {})
                if isinstance(rtp, dict):
                    cfg["rtp_paths"]["rgss1"] = [str(x) for x in rtp.get("rgss1", []) if str(x).strip()]
                    cfg["rtp_paths"]["rgss3"] = [str(x) for x in rtp.get("rgss3", []) if str(x).strip()]
                if str(raw.get("version", "")) in ("auto", "rgss1", "rgss3"):
                    cfg["version"] = str(raw["version"])
        except Exception:
            pass
    return cfg


def save_global_rtp_config(cfg: Dict[str, Any]) -> Path:
    """Persists per-user global RTP configuration JSON cleanly."""
    p = get_global_rtp_config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    clean = default_rgss_config()
    if isinstance(cfg, dict):
        if str(cfg.get("version", "")) in ("auto", "rgss1", "rgss3"):
            clean["version"] = str(cfg["version"])
        rtp = cfg.get("rtp_paths", {})
        if isinstance(rtp, dict):
            clean["rtp_paths"]["rgss1"] = [str(x) for x in rtp.get("rgss1", []) if str(x).strip()]
            clean["rtp_paths"]["rgss3"] = [str(x) for x in rtp.get("rgss3", []) if str(x).strip()]
    p.write_text(json.dumps(clean, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


def discover_local_rtp_candidates() -> Dict[str, List[str]]:
    """Probes user machine (Registry / standard install dirs) for already-installed RGSS1/RGSS3 RTP folders."""
    found: Dict[str, List[str]] = {"rgss1": [], "rgss3": []}
    seen: Dict[str, set] = {"rgss1": set(), "rgss3": set()}

    def _add(ver: str, cand: str) -> None:
        if not cand:
            return
        norm = str(Path(cand))
        if norm not in seen[ver] and Path(norm).is_dir():
            seen[ver].add(norm)
            found[ver].append(norm)

    if sys.platform == "win32":
        try:
            import winreg  # type: ignore

            reg_targets = (
                ("rgss1", r"SOFTWARE\Enterbrain\RGSS\RTP"),
                ("rgss1", r"SOFTWARE\WOW6432Node\Enterbrain\RGSS\RTP"),
                ("rgss3", r"SOFTWARE\Enterbrain\RGSS3\RTP"),
                ("rgss3", r"SOFTWARE\WOW6432Node\Enterbrain\RGSS3\RTP"),
            )
            for ver, subkey in reg_targets:
                for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
                    try:
                        with winreg.OpenKey(hive, subkey) as key:
                            idx = 0
                            while True:
                                try:
                                    _name, val, _kind = winreg.EnumValue(key, idx)
                                    if isinstance(val, str):
                                        _add(ver, val)
                                    idx += 1
                                except OSError:
                                    break
                    except OSError:
                        pass
        except Exception:
            pass

    common_candidates = (
        ("rgss1", r"C:\Program Files (x86)\Common Files\Enterbrain\RGSS\Standard"),
        ("rgss1", r"C:\Program Files (x86)\Steam\steamapps\common\RPGXP\rtp"),
        ("rgss3", r"C:\Program Files (x86)\Common Files\Enterbrain\RGSS3\RPGVXAce"),
        ("rgss3", r"C:\Program Files (x86)\Steam\steamapps\common\RPGVXAce\rtp"),
    )
    for ver, path_str in common_candidates:
        _add(ver, path_str)
    return found


def detect_project_rgss_version(ir_dir: Path) -> str:
    """Detects whether an IR workspace targets rgss1 (RMXP) or rgss3 (VX Ace)."""
    ir_dir = Path(ir_dir)
    proj_file = ir_dir / "ir_project.json"
    if proj_file.is_file():
        try:
            proj = load_json(proj_file)
            cfg = proj.get("rgss_runtime", {})
            if isinstance(cfg, dict) and str(cfg.get("version", "")) in ("rgss1", "rgss3"):
                return str(cfg["version"])
        except Exception:
            pass

    parent = ir_dir.parent
    if (parent / "vxa" / "Data" / "Scripts.rvdata2").is_file() or (parent / "Game.rvproj2").is_file():
        return "rgss3"

    idx_path = ir_dir / "scripts" / "_index.json"
    if idx_path.is_file():
        try:
            entries = load_json(idx_path)
            for item in entries:
                fp = ir_dir / "scripts" / str(item.get("file", ""))
                if fp.is_file():
                    txt = fp.read_text(encoding="utf-8", errors="replace")
                    if "rgss_main" in txt or "SceneManager" in txt or "Fiber" in txt:
                        return "rgss3"
        except Exception:
            pass
    return "rgss1"


def read_user_scripts_package(script_archive_path: Path, encoding: str = "utf-8") -> List[Dict[str, Any]]:
    """Decodes user-provided Scripts.rxdata (RGSS1) or Scripts.rvdata2 (RGSS3) into ordered script records."""
    p = Path(script_archive_path)
    if not p.is_file():
        raise FileNotFoundError(f"Script archive not found: {p}")
    rows = marshal_loads(p.read_bytes(), decode_strings=False, encoding=encoding)
    if not isinstance(rows, list):
        raise ValueError(f"Invalid RM script archive format in {p}")
    out: List[Dict[str, Any]] = []
    for idx, entry in enumerate(rows):
        if not isinstance(entry, (list, tuple)) or len(entry) < 3:
            continue
        sid, title_raw, compressed = entry[0], entry[1], entry[2]
        title = title_raw.decode(encoding, errors="replace") if isinstance(title_raw, bytes) else str(title_raw)
        raw_bytes = b""
        if isinstance(compressed, (bytes, bytearray)) and compressed:
            raw_bytes = zlib.decompress(bytes(compressed))
        code = raw_bytes.decode(encoding, errors="replace")
        out.append({"id": int(sid) if isinstance(sid, int) else (idx + 1), "title": title, "code": code})
    return out


def parse_ogg_loop_tags(raw: bytes) -> Dict[str, int]:
    """Parses LOOPSTART and LOOPLENGTH sample indices from OGG Vorbis comment headers."""
    out: Dict[str, int] = {}
    if not raw or not raw.startswith(b"OggS"):
        return out
    idx = raw.find(b"\x03vorbis")
    if idx >= 0:
        pos = idx + 7
        try:
            if pos + 4 <= len(raw):
                vendor_len = struct.unpack_from("<I", raw, pos)[0]
                pos += 4 + max(0, vendor_len)
            if pos + 4 <= len(raw):
                comment_cnt = struct.unpack_from("<I", raw, pos)[0]
                pos += 4
                for _ in range(min(comment_cnt, 128)):
                    if pos + 4 > len(raw):
                        break
                    c_len = struct.unpack_from("<I", raw, pos)[0]
                    pos += 4
                    if pos + c_len > len(raw):
                        break
                    entry = raw[pos : pos + c_len].decode("utf-8", errors="replace")
                    pos += c_len
                    if "=" in entry:
                        k, v = entry.split("=", 1)
                        ku = k.strip().upper()
                        if ku in ("LOOPSTART", "LOOPLENGTH"):
                            try:
                                out[ku] = max(0, int(v.strip()))
                            except ValueError:
                                pass
        except Exception:
            pass
    if not out:
        head = raw[:65536].decode("latin-1", errors="ignore")
        for key in ("LOOPSTART", "LOOPLENGTH"):
            m = re.search(rf"{key}=(\d+)", head, flags=re.IGNORECASE)
            if m:
                out[key] = max(0, int(m.group(1)))
    return out


class UserRgssResourceResolver:
    """Resolves project data, Graphics/, Audio/, and user-configured RTP assets cleanly."""

    def __init__(
        self,
        ir_dir: Path,
        explicit_graphics_dir: Optional[Path] = None,
        version: str = "auto",
        rtp_paths: Optional[Dict[str, List[str]]] = None,
    ) -> None:
        self.ir_dir = Path(ir_dir)
        self.version = detect_project_rgss_version(self.ir_dir) if version == "auto" else str(version)
        self.explicit_graphics_dir = Path(explicit_graphics_dir) if explicit_graphics_dir else None
        self.rtp_paths = self._collect_rtp_paths(rtp_paths)
        self._archives: List[RgssadArchiveReader] = self._open_archives()
        self.search_roots: List[Path] = self._build_search_roots()

    def _collect_rtp_paths(self, override: Optional[Dict[str, List[str]]]) -> Dict[str, List[str]]:
        merged = load_global_rtp_config().get("rtp_paths", {"rgss1": [], "rgss3": []})
        proj_file = self.ir_dir / "ir_project.json"
        if proj_file.is_file():
            try:
                proj = load_json(proj_file)
                prtp = proj.get("rgss_runtime", {}).get("rtp_paths", {})
                if isinstance(prtp, dict):
                    for ver in ("rgss1", "rgss3"):
                        merged[ver] = list(dict.fromkeys([*prtp.get(ver, []), *merged.get(ver, [])]))
            except Exception:
                pass
        if isinstance(override, dict):
            for ver in ("rgss1", "rgss3"):
                if ver in override:
                    merged[ver] = list(dict.fromkeys([*override.get(ver, []), *merged.get(ver, [])]))
        return merged

    def _open_archives(self) -> List[RgssadArchiveReader]:
        readers: List[RgssadArchiveReader] = []
        parent = self.ir_dir.parent
        for base in (self.ir_dir, parent, parent / "rmxp", parent / "vxa"):
            for fname in ("Game.rgss3a", "Game.rgss2a", "Game.rgssad"):
                cand = base / fname
                if cand.is_file():
                    try:
                        readers.append(RgssadArchiveReader(cand))
                    except Exception:
                        pass
        return readers

    def _build_search_roots(self) -> List[Path]:
        parent = self.ir_dir.parent
        candidates: List[Path] = []
        if self.explicit_graphics_dir is not None:
            candidates.append(self.explicit_graphics_dir.parent if self.explicit_graphics_dir.name.lower() == "graphics" else self.explicit_graphics_dir)
        candidates.extend([
            self.ir_dir / "assets",
            parent / "rmxp",
            parent / "vxa",
            parent,
        ])
        for rtp_dir in self.rtp_paths.get(self.version, []):
            candidates.append(Path(rtp_dir))
        out: List[Path] = []
        seen = set()
        for c in candidates:
            if c.is_dir():
                rc = str(c.resolve())
                if rc not in seen:
                    seen.add(rc)
                    out.append(c)
        return out

    def _normalize_rel(self, rel_path: str) -> str:
        clean = str(rel_path).replace("\\", "/").lstrip("/")
        return clean

    def resolve_data_path(self, rel_path: str) -> Optional[Path]:
        clean = self._normalize_rel(rel_path)
        parent = self.ir_dir.parent
        for base in (parent / "rmxp", parent / "vxa", parent, self.ir_dir):
            cand = base / clean
            if cand.is_file():
                return cand
        return None

    def read_bytes(self, rel_path: str, extensions: Optional[Tuple[str, ...]] = None) -> Optional[bytes]:
        clean = self._normalize_rel(rel_path)
        exts = extensions
        if exts is None:
            lower = clean.lower()
            if lower.startswith("graphics/"):
                exts = _GRAPHICS_EXTS
            elif lower.startswith("audio/"):
                exts = _AUDIO_EXTS
            else:
                exts = ("",)
        for rd in self._archives:
            raw = rd.read_bytes(clean, extensions=exts)
            if raw is not None:
                return raw
        for root in self.search_roots:
            base_cand = root / clean
            if base_cand.is_file():
                return base_cand.read_bytes()
            if not Path(clean).suffix:
                for ext in exts:
                    if not ext:
                        continue
                    ec = root / f"{clean}{ext}"
                    if ec.is_file():
                        return ec.read_bytes()
        parent = self.ir_dir.parent
        for root in (parent / "rmxp", parent / "vxa", parent):
            cand = root / clean
            if cand.is_file():
                return cand.read_bytes()
        return None

    def exists(self, rel_path: str) -> bool:
        clean = self._normalize_rel(rel_path)
        if self.read_bytes(clean, extensions=("",)) is not None:
            return True
        if not Path(clean).suffix:
            return self.read_bytes(clean) is not None
        return False

    def resolve_data_object(self, rel_path: str) -> Any:
        """Loads RMXP/VXA Marshal data from archive or folder, falling back to IR conversion when needed."""
        raw = self.read_bytes(rel_path, extensions=("",))
        if raw is not None:
            return marshal_loads(raw)
        clean = self._normalize_rel(rel_path)
        lower = clean.lower()
        if lower.startswith("data/") and (lower.endswith(".rxdata") or lower.endswith(".rvdata2")):
            stem = Path(clean).stem.lower()
            ir_map = {
                "actors": self.ir_dir / "database" / "actors.json",
                "classes": self.ir_dir / "database" / "classes.json",
                "skills": self.ir_dir / "database" / "skills.json",
                "items": self.ir_dir / "database" / "items.json",
                "weapons": self.ir_dir / "database" / "weapons.json",
                "armors": self.ir_dir / "database" / "armors.json",
                "enemies": self.ir_dir / "database" / "enemies.json",
                "troops": self.ir_dir / "database" / "troops.json",
                "states": self.ir_dir / "database" / "states.json",
                "animations": self.ir_dir / "database" / "animations.json",
                "tilesets": self.ir_dir / "database" / "tilesets.json",
                "commonevents": self.ir_dir / "database" / "common_events.json",
            }
            if stem == "system" and (self.ir_dir / "ir_project.json").is_file():
                from .ir_converter import ir_to_ruby

                return ir_to_ruby(load_json(self.ir_dir / "ir_project.json")["system"])
            if stem == "mapinfos" and (self.ir_dir / "ir_project.json").is_file():
                from .ir_converter import ir_to_ruby

                return ir_to_ruby(load_json(self.ir_dir / "ir_project.json")["map_infos"])
            if stem in ir_map and ir_map[stem].is_file():
                from .ir_converter import ir_to_ruby

                return ir_to_ruby(load_json(ir_map[stem]))
            if stem.startswith("map") and stem[3:].isdigit():
                mid = int(stem[3:])
                mp = self.ir_dir / "maps" / f"map_{mid:03d}.json"
                if mp.is_file():
                    from .ir_converter import ir_map_to_ruby

                    return ir_map_to_ruby(load_json(mp))
        raise FileNotFoundError(f"Data resource not found: {rel_path}")

    def inspect_status(self) -> Dict[str, Any]:
        configured = self.rtp_paths.get(self.version, [])
        valid = [p for p in configured if Path(p).is_dir()]
        invalid = [p for p in configured if not Path(p).is_dir()]
        return {
            "version": self.version,
            "search_roots": [str(p) for p in self.search_roots],
            "archives": [str(r.archive_path) for r in self._archives],
            "configured_rtp": configured,
            "valid_rtp": valid,
            "missing_rtp": invalid,
            "has_user_rtp": bool(valid),
        }


# ---------------------------------------------------------------------------
# Clean-Room RGSS1 / RGSS3 Native Classes
# ---------------------------------------------------------------------------

def _clamp_u8(val: float) -> int:
    return max(0, min(255, int(round(val))))


def _clamp_tone(val: float) -> int:
    return max(-255, min(255, int(round(val))))


class RgssColor:
    """Mutable RGBA color primitive matching RGSS Color semantics."""

    def __init__(self, r: float = 0.0, g: float = 0.0, b: float = 0.0, a: float = 255.0) -> None:
        self.set(r, g, b, a)

    def set(self, r: float, g: float, b: float, a: float = 255.0) -> "RgssColor":
        self.red = float(max(0.0, min(255.0, float(r))))
        self.green = float(max(0.0, min(255.0, float(g))))
        self.blue = float(max(0.0, min(255.0, float(b))))
        self.alpha = float(max(0.0, min(255.0, float(a))))
        return self

    def to_rgba(self) -> Tuple[int, int, int, int]:
        return (_clamp_u8(self.red), _clamp_u8(self.green), _clamp_u8(self.blue), _clamp_u8(self.alpha))


class RgssTone:
    """Color balance tone modifier matching RGSS Tone(r, g, b, gray)."""

    def __init__(self, r: float = 0.0, g: float = 0.0, b: float = 0.0, gray: float = 0.0) -> None:
        self.set(r, g, b, gray)

    def set(self, r: float, g: float, b: float, gray: float = 0.0) -> "RgssTone":
        self.red = float(_clamp_tone(float(r)))
        self.green = float(_clamp_tone(float(g)))
        self.blue = float(_clamp_tone(float(b)))
        self.gray = float(max(0.0, min(255.0, float(gray))))
        return self


class RgssRect:
    """Mutable 2D integer rectangle primitive matching RGSS Rect."""

    def __init__(self, x: int = 0, y: int = 0, width: int = 0, height: int = 0) -> None:
        self.set(x, y, width, height)

    def set(self, x: int, y: int, width: int, height: int) -> "RgssRect":
        self.x = int(x)
        self.y = int(y)
        self.width = int(width)
        self.height = int(height)
        return self

    def empty(self) -> "RgssRect":
        return self.set(0, 0, 0, 0)


class RgssFont:
    """Text styling object supporting both RGSS1 and RGSS3 outline/shadow attributes."""

    default_name: Any = ["Malgun Gothic", "NanumGothic", "Arial"]
    default_size: int = 20
    default_bold: bool = False
    default_italic: bool = False
    default_shadow: bool = True
    default_outline: bool = True
    default_color: Tuple[float, float, float, float] = (255.0, 255.0, 255.0, 255.0)
    default_out_color: Tuple[float, float, float, float] = (0.0, 0.0, 0.0, 128.0)

    def __init__(self, name: Any = None, size: Optional[int] = None) -> None:
        self.name = name if name is not None else self.default_name
        self.size = int(size if size is not None else self.default_size)
        self.bold = bool(self.default_bold)
        self.italic = bool(self.default_italic)
        self.shadow = bool(self.default_shadow)
        self.outline = bool(self.default_outline)
        self.color = RgssColor(*self.default_color)
        self.out_color = RgssColor(*self.default_out_color)


class RgssTableRuntime:
    """Mutable 1D/2D/3D signed 16-bit integer grid matching RGSS Table semantics."""

    def __init__(
        self,
        xsize: int = 1,
        ysize: Optional[int] = None,
        zsize: Optional[int] = None,
        data: Optional[List[int]] = None,
    ) -> None:
        self.dims = 1 if ysize is None else (2 if zsize is None else 3)
        self.xsize = max(0, int(xsize))
        self.ysize = 1 if ysize is None else max(0, int(ysize))
        self.zsize = 1 if zsize is None else max(0, int(zsize))
        total = self.xsize * self.ysize * self.zsize
        if data is not None:
            padded = [int(v) for v in data[:total]]
            if len(padded) < total:
                padded.extend([0] * (total - len(padded)))
            self.data = padded
        else:
            self.data = [0] * total

    def resize(self, xsize: int, ysize: Optional[int] = None, zsize: Optional[int] = None) -> None:
        nx = max(0, int(xsize))
        ny = self.ysize if ysize is None else max(0, int(ysize))
        nz = self.zsize if zsize is None else max(0, int(zsize))
        if ysize is not None:
            self.dims = 2 if zsize is None else 3
        new_data = [0] * (nx * ny * nz)
        for z in range(min(self.zsize, nz)):
            for y in range(min(self.ysize, ny)):
                for x in range(min(self.xsize, nx)):
                    new_data[(z * ny + y) * nx + x] = self.get(x, y, z)
        self.xsize, self.ysize, self.zsize = nx, ny, nz
        self.data = new_data

    def _index(self, x: int, y: int = 0, z: int = 0) -> Optional[int]:
        ix, iy, iz = int(x), int(y), int(z)
        if not (0 <= ix < self.xsize and 0 <= iy < self.ysize and 0 <= iz < self.zsize):
            return None
        return (iz * self.ysize + iy) * self.xsize + ix

    def get(self, x: int, y: int = 0, z: int = 0) -> int:
        idx = self._index(x, y, z)
        return self.data[idx] if idx is not None else 0

    def set(self, value: int, x: int, y: int = 0, z: int = 0) -> int:
        idx = self._index(x, y, z)
        if idx is not None:
            v = int(value) & 0xFFFF
            if v >= 0x8000:
                v -= 0x10000
            self.data[idx] = v
            return v
        return 0


class RgssBitmap:
    """RGBA software surface matching RGSS Bitmap operations via Pillow."""

    def __init__(
        self,
        width_or_path: Any,
        height: Optional[int] = None,
        resolver: Optional[UserRgssResourceResolver] = None,
    ) -> None:
        self.disposed = False
        self.font = RgssFont()
        if isinstance(width_or_path, str):
            raw = resolver.read_bytes(width_or_path) if resolver else None
            if raw is None and resolver and not width_or_path.lower().startswith("graphics/"):
                raw = resolver.read_bytes(f"Graphics/{width_or_path}")
            if raw is not None:
                self.image = Image.open(io.BytesIO(raw)).convert("RGBA")
            else:
                # Try direct path as last resort inside workspace
                cand = Path(width_or_path)
                if cand.is_file():
                    self.image = Image.open(cand).convert("RGBA")
                else:
                    raise FileNotFoundError(f"Bitmap asset not found: {width_or_path}")
        else:
            w = max(1, int(width_or_path))
            h = max(1, int(height or 1))
            self.image = Image.new("RGBA", (w, h), (0, 0, 0, 0))

    @property
    def width(self) -> int:
        return 0 if self.disposed else self.image.width

    @property
    def height(self) -> int:
        return 0 if self.disposed else self.image.height

    @property
    def rect(self) -> RgssRect:
        return RgssRect(0, 0, self.width, self.height)

    def dispose(self) -> None:
        self.disposed = True

    def clear(self) -> None:
        if not self.disposed:
            self.image = Image.new("RGBA", self.image.size, (0, 0, 0, 0))

    def clear_rect(self, x: int, y: int, w: int, h: int) -> None:
        if self.disposed or w <= 0 or h <= 0:
            return
        draw = ImageDraw.Draw(self.image)
        draw.rectangle([x, y, x + w - 1, y + h - 1], fill=(0, 0, 0, 0))

    def fill_rect(self, x: int, y: int, w: int, h: int, color: RgssColor) -> None:
        if self.disposed or w <= 0 or h <= 0:
            return
        rgba = color.to_rgba()
        patch = Image.new("RGBA", (int(w), int(h)), rgba)
        self.image.paste(patch, (int(x), int(y)), patch if rgba[3] < 255 else None)

    def gradient_fill_rect(
        self,
        x: int,
        y: int,
        w: int,
        h: int,
        color1: RgssColor,
        color2: RgssColor,
        vertical: bool = False,
    ) -> None:
        if self.disposed or w <= 0 or h <= 0:
            return
        c1 = color1.to_rgba()
        c2 = color2.to_rgba()
        patch = Image.new("RGBA", (int(w), int(h)), (0, 0, 0, 0))
        draw = ImageDraw.Draw(patch)
        steps = int(h) if vertical else int(w)
        denom = max(1, steps - 1)
        for i in range(steps):
            t = i / float(denom)
            col = tuple(_clamp_u8(c1[k] + (c2[k] - c1[k]) * t) for k in range(4))
            if vertical:
                draw.line([(0, i), (int(w) - 1, i)], fill=col)
            else:
                draw.line([(i, 0), (i, int(h) - 1)], fill=col)
        self.image.paste(patch, (int(x), int(y)), patch)

    def get_pixel(self, x: int, y: int) -> RgssColor:
        if self.disposed or not (0 <= x < self.image.width and 0 <= y < self.image.height):
            return RgssColor(0, 0, 0, 0)
        return RgssColor(*self.image.getpixel((int(x), int(y))))

    def set_pixel(self, x: int, y: int, color: RgssColor) -> None:
        if not self.disposed and 0 <= x < self.image.width and 0 <= y < self.image.height:
            self.image.putpixel((int(x), int(y)), color.to_rgba())

    def blt(
        self,
        dx: int,
        dy: int,
        src: "RgssBitmap",
        src_rect: RgssRect,
        opacity: int = 255,
    ) -> None:
        if self.disposed or src.disposed or src_rect.width <= 0 or src_rect.height <= 0:
            return
        sx = max(0, src_rect.x)
        sy = max(0, src_rect.y)
        ex = min(src.width, src_rect.x + src_rect.width)
        ey = min(src.height, src_rect.y + src_rect.height)
        if ex <= sx or ey <= sy:
            return
        crop = src.image.crop((sx, sy, ex, ey))
        op = _clamp_u8(opacity)
        if op < 255:
            alpha = crop.getchannel("A").point(lambda v: (v * op) // 255)
            crop.putalpha(alpha)
        self.image.alpha_composite(crop, dest=(int(dx), int(dy)))

    def stretch_blt(
        self,
        dest_rect: RgssRect,
        src: "RgssBitmap",
        src_rect: RgssRect,
        opacity: int = 255,
    ) -> None:
        if self.disposed or src.disposed or dest_rect.width <= 0 or dest_rect.height <= 0:
            return
        if src_rect.width <= 0 or src_rect.height <= 0:
            return
        sx = max(0, src_rect.x)
        sy = max(0, src_rect.y)
        ex = min(src.width, src_rect.x + src_rect.width)
        ey = min(src.height, src_rect.y + src_rect.height)
        if ex <= sx or ey <= sy:
            return
        crop = src.image.crop((sx, sy, ex, ey)).resize((int(dest_rect.width), int(dest_rect.height)), Image.Resampling.BILINEAR)
        op = _clamp_u8(opacity)
        if op < 255:
            alpha = crop.getchannel("A").point(lambda v: (v * op) // 255)
            crop.putalpha(alpha)
        self.image.alpha_composite(crop, dest=(int(dest_rect.x), int(dest_rect.y)))

    def hue_change(self, hue: float) -> None:
        if self.disposed:
            return
        shift = int(round(hue)) % 360
        if shift == 0:
            return
        delta = int(round(shift * 255.0 / 360.0))
        hsv = self.image.convert("HSV")
        h_ch, s_ch, v_ch = hsv.split()
        h_ch = h_ch.point(lambda p: (p + delta) % 256)
        rgb = Image.merge("HSV", (h_ch, s_ch, v_ch)).convert("RGBA")
        rgb.putalpha(self.image.getchannel("A"))
        self.image = rgb

    def blur(self) -> None:
        if not self.disposed:
            self.image = self.image.filter(ImageFilter.BoxBlur(1))

    def _pil_font(self) -> ImageFont.ImageFont:
        fsize = max(8, int(self.font.size))
        candidates = self.font.name if isinstance(self.font.name, list) else [self.font.name]
        for nm in candidates:
            s = str(nm or "").strip()
            if not s:
                continue
            for cand in (
                s,
                f"C:/Windows/Fonts/{s}.ttf",
                "C:/Windows/Fonts/malgun.ttf",
                "C:/Windows/Fonts/arial.ttf",
            ):
                try:
                    return ImageFont.truetype(cand, fsize)
                except Exception:
                    pass
        return ImageFont.load_default()

    def text_size(self, text: str) -> RgssRect:
        if self.disposed:
            return RgssRect(0, 0, 0, 0)
        draw = ImageDraw.Draw(self.image)
        fnt = self._pil_font()
        box = draw.textbbox((0, 0), str(text), font=fnt)
        return RgssRect(0, 0, max(0, box[2] - box[0]), max(0, box[3] - box[1]))

    def draw_text(self, x: int, y: int, w: int, h: int, text: str, align: int = 0) -> None:
        if self.disposed:
            return
        txt = str(text)
        draw = ImageDraw.Draw(self.image)
        fnt = self._pil_font()
        box = draw.textbbox((0, 0), txt, font=fnt)
        tw = max(0, box[2] - box[0])
        th = max(0, box[3] - box[1])
        if int(align) == 1:
            tx = int(x) + max(0, (int(w) - tw) // 2)
        elif int(align) == 2:
            tx = int(x) + max(0, int(w) - tw)
        else:
            tx = int(x)
        ty = int(y) + max(0, (int(h) - th) // 2)
        if self.font.outline:
            ocol = self.font.out_color.to_rgba()
            for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1)):
                draw.text((tx + dx, ty + dy), txt, font=fnt, fill=ocol)
        elif self.font.shadow:
            draw.text((tx + 1, ty + 1), txt, font=fnt, fill=(0, 0, 0, 180))
        draw.text((tx, ty), txt, font=fnt, fill=self.font.color.to_rgba())


class RgssViewport:
    """Clipping & offset viewport matching RGSS Viewport."""

    def __init__(self, x: int = 0, y: int = 0, width: int = 640, height: int = 480) -> None:
        self.rect = RgssRect(x, y, width, height)
        self.visible = True
        self.z = 0
        self.ox = 0
        self.oy = 0
        self.color = RgssColor(0, 0, 0, 0)
        self.tone = RgssTone(0, 0, 0, 0)
        self.disposed = False

    def dispose(self) -> None:
        self.disposed = True

    def update(self) -> None:
        pass


class RgssSprite:
    """2D sprite object matching RGSS1/RGSS3 Sprite."""

    def __init__(self, viewport: Optional[RgssViewport] = None) -> None:
        self.viewport = viewport
        self.bitmap: Optional[RgssBitmap] = None
        self.src_rect = RgssRect(0, 0, 0, 0)
        self.visible = True
        self.x = 0
        self.y = 0
        self.z = 0
        self.ox = 0
        self.oy = 0
        self.zoom_x = 1.0
        self.zoom_y = 1.0
        self.angle = 0.0
        self.mirror = False
        self.bush_depth = 0
        self.bush_opacity = 128
        self.opacity = 255
        self.blend_type = 0
        self.color = RgssColor(0, 0, 0, 0)
        self.tone = RgssTone(0, 0, 0, 0)
        self.disposed = False
        self._flash_frames = 0

    def flash(self, color: Optional[RgssColor], duration: int) -> None:
        self._flash_frames = max(0, int(duration))

    def update(self) -> None:
        if self._flash_frames > 0:
            self._flash_frames -= 1

    def dispose(self) -> None:
        self.disposed = True


class RgssPlane:
    """Infinite scrolling tiled layer matching RGSS Plane."""

    def __init__(self, viewport: Optional[RgssViewport] = None) -> None:
        self.viewport = viewport
        self.bitmap: Optional[RgssBitmap] = None
        self.visible = True
        self.z = 0
        self.ox = 0
        self.oy = 0
        self.zoom_x = 1.0
        self.zoom_y = 1.0
        self.opacity = 255
        self.blend_type = 0
        self.color = RgssColor(0, 0, 0, 0)
        self.tone = RgssTone(0, 0, 0, 0)
        self.disposed = False

    def dispose(self) -> None:
        self.disposed = True


class RgssWindow:
    """UI Window supporting both RGSS1 1-arg and RGSS3 4-arg construction."""

    def __init__(self, x_or_viewport: Any = None, y: Optional[int] = None, w: Optional[int] = None, h: Optional[int] = None) -> None:
        if isinstance(x_or_viewport, RgssViewport):
            self.viewport: Optional[RgssViewport] = x_or_viewport
            self.x, self.y, self.width, self.height = 0, 0, 0, 0
        elif y is not None and w is not None and h is not None:
            self.viewport = None
            self.x, self.y, self.width, self.height = int(x_or_viewport), int(y), int(w), int(h)
        else:
            self.viewport = None
            self.x, self.y, self.width, self.height = 0, 0, 0, 0
        self.windowskin: Optional[RgssBitmap] = None
        self.contents: RgssBitmap = RgssBitmap(max(1, self.width - 32), max(1, self.height - 32))
        self.cursor_rect = RgssRect(0, 0, 0, 0)
        self.active = True
        self.visible = True
        self.pause = False
        self.z = 100
        self.ox = 0
        self.oy = 0
        self.padding = 12
        self.padding_bottom = 12
        self.openness = 255
        self.opacity = 255
        self.back_opacity = 200
        self.contents_opacity = 255
        self.tone = RgssTone(0, 0, 0, 0)
        self.arrows_visible = True
        self.disposed = False

    def update(self) -> None:
        pass

    def dispose(self) -> None:
        self.disposed = True


class RgssTilemap:
    """Unified tilemap object supporting RGSS1 (3-layer RMXP) and RGSS3 (A1-E VX Ace) layouts."""

    def __init__(self, viewport: Optional[RgssViewport] = None, mode: str = "rgss1") -> None:
        self.viewport = viewport
        self.mode = mode
        self.tileset: Optional[RgssBitmap] = None
        self.autotiles: List[Optional[RgssBitmap]] = [None] * 7
        self.bitmaps: List[Optional[RgssBitmap]] = [None] * 9  # RGSS3 A1-A5, B-E
        self.map_data: Optional[RgssTableRuntime] = None
        self.priorities: Optional[RgssTableRuntime] = None
        self.flags: Optional[RgssTableRuntime] = None
        self.flash_data: Optional[RgssTableRuntime] = None
        self.visible = True
        self.ox = 0
        self.oy = 0
        self.tick = 0
        self.disposed = False

    def update(self) -> None:
        self.tick += 1

    def dispose(self) -> None:
        self.disposed = True


class RgssInput:
    """Keyboard/gamepad button state tracker implementing RGSS Input.trigger?/press?/repeat?/dir4/dir8."""

    def __init__(self) -> None:
        self._held: Dict[int, int] = {}
        self._next_held: set[int] = set()

    def set_key_state(self, key_code: int, is_down: bool) -> None:
        code = _KEY_NAME_TO_CODE.get(str(key_code).upper(), 0) if isinstance(key_code, str) else int(key_code)
        if code <= 0:
            return
        if is_down:
            self._next_held.add(code)
        else:
            self._next_held.discard(code)

    def inject_tap(self, key_code: Any) -> None:
        self.set_key_state(key_code, True)

    def update(self) -> None:
        new_held: Dict[int, int] = {}
        for code in self._next_held:
            new_held[code] = self._held.get(code, 0) + 1
        self._held = new_held

    def _resolve_code(self, sym: Any) -> int:
        if isinstance(sym, int):
            return sym
        clean = str(sym).lstrip(":").upper()
        return _KEY_NAME_TO_CODE.get(clean, 0)

    def press(self, sym: Any) -> bool:
        return self._held.get(self._resolve_code(sym), 0) > 0

    def trigger(self, sym: Any) -> bool:
        return self._held.get(self._resolve_code(sym), 0) == 1

    def repeat(self, sym: Any) -> bool:
        cnt = self._held.get(self._resolve_code(sym), 0)
        return cnt == 1 or (cnt >= 18 and cnt % 6 == 0)

    def dir4(self) -> int:
        for code in (2, 4, 6, 8):
            if self.press(code):
                return code
        return 0

    def dir8(self) -> int:
        d = self.press(2)
        l = self.press(4)
        r = self.press(6)
        u = self.press(8)
        if d and l:
            return 1
        if d and r:
            return 3
        if u and l:
            return 7
        if u and r:
            return 9
        return self.dir4()


class RgssAudio:
    """Tracks Audio.bgm_*/bgs_*/me_*/se_* requests and parses OGG loop metadata."""

    def __init__(self, resolver: Optional[UserRgssResourceResolver] = None) -> None:
        self.resolver = resolver
        self.bgm: Optional[Dict[str, Any]] = None
        self.bgs: Optional[Dict[str, Any]] = None
        self.last_me: Optional[Dict[str, Any]] = None
        self.se_events: List[Dict[str, Any]] = []
        self.bgm_pos: int = 0
        self.bgs_pos: int = 0

    def _make_record(self, filename: str, volume: int, pitch: int, pos: int = 0) -> Dict[str, Any]:
        loop_tags: Dict[str, int] = {}
        if self.resolver and filename:
            raw = self.resolver.read_bytes(filename, extensions=_AUDIO_EXTS)
            if raw:
                loop_tags = parse_ogg_loop_tags(raw)
        return {
            "filename": str(filename),
            "volume": int(volume),
            "pitch": int(pitch),
            "pos": int(pos),
            "loop_tags": loop_tags,
        }

    def bgm_play(self, filename: str, volume: int = 100, pitch: int = 100, pos: int = 0) -> None:
        self.bgm = self._make_record(filename, volume, pitch, pos)
        self.bgm_pos = int(pos)

    def bgm_stop(self) -> None:
        self.bgm = None
        self.bgm_pos = 0

    def bgm_fade(self, time_ms: int) -> None:
        self.bgm = None

    def bgs_play(self, filename: str, volume: int = 100, pitch: int = 100, pos: int = 0) -> None:
        self.bgs = self._make_record(filename, volume, pitch, pos)
        self.bgs_pos = int(pos)

    def bgs_stop(self) -> None:
        self.bgs = None
        self.bgs_pos = 0

    def bgs_fade(self, time_ms: int) -> None:
        self.bgs = None

    def me_play(self, filename: str, volume: int = 100, pitch: int = 100) -> None:
        self.last_me = self._make_record(filename, volume, pitch)

    def me_stop(self) -> None:
        self.last_me = None

    def se_play(self, filename: str, volume: int = 100, pitch: int = 100) -> None:
        self.se_events.append(self._make_record(filename, volume, pitch))

    def se_stop(self) -> None:
        self.se_events.clear()


class RgssRuntimeSession:
    """Host registry & compositor linking mruby callbacks to RGSS1/RGSS3 objects."""

    def __init__(
        self,
        ir_dir: Path,
        explicit_graphics_dir: Optional[Path] = None,
        version: str = "auto",
        on_frame: Optional[Callable[[Image.Image, int], Optional[bool]]] = None,
    ) -> None:
        self.ir_dir = Path(ir_dir)
        self.version = detect_project_rgss_version(self.ir_dir) if version == "auto" else str(version)
        self.resolver = UserRgssResourceResolver(self.ir_dir, explicit_graphics_dir=explicit_graphics_dir, version=self.version)
        self.width = 544 if self.version == "rgss3" else 640
        self.height = 416 if self.version == "rgss3" else 480
        self.frame_rate = 60 if self.version == "rgss3" else 40
        self.frame_count = 0
        self.brightness = 255
        self.frozen_image: Optional[Image.Image] = None
        self.input = RgssInput()
        self.audio = RgssAudio(self.resolver)
        self.on_frame = on_frame
        self.logs: List[str] = []
        self._next_handle = 1
        self._handles: Dict[int, Any] = {}
        self.last_frame: Image.Image = Image.new("RGBA", (self.width, self.height), (0, 0, 0, 255))

    def _alloc(self, obj: Any) -> int:
        hid = self._next_handle
        self._next_handle += 1
        self._handles[hid] = obj
        return hid

    def get_handle(self, hid: int) -> Any:
        return self._handles.get(int(hid))

    def composite_frame(self) -> Image.Image:
        """Composites active viewports, tilemaps, planes, sprites, and windows in Z-order."""
        canvas = Image.new("RGBA", (self.width, self.height), (0, 0, 0, 255))
        drawables: List[Tuple[int, int, Any]] = []
        for hid, obj in list(self._handles.items()):
            if getattr(obj, "disposed", False) or not getattr(obj, "visible", True):
                continue
            if isinstance(obj, (RgssSprite, RgssPlane, RgssWindow, RgssTilemap)):
                vp = getattr(obj, "viewport", None)
                if vp is not None and (vp.disposed or not vp.visible):
                    continue
                vz = vp.z if vp is not None else 0
                oz = int(getattr(obj, "z", 0))
                drawables.append((vz * 10000 + oz, hid, obj))
        drawables.sort(key=lambda item: (item[0], item[1]))

        for _zkey, _hid, obj in drawables:
            vp = getattr(obj, "viewport", None)
            vx = (vp.rect.x - vp.ox) if vp is not None else 0
            vy = (vp.rect.y - vp.oy) if vp is not None else 0
            if isinstance(obj, RgssSprite) and obj.bitmap and not obj.bitmap.disposed:
                sr = obj.src_rect
                bx = max(0, sr.x) if sr.width > 0 else 0
                by = max(0, sr.y) if sr.height > 0 else 0
                bw = sr.width if sr.width > 0 else obj.bitmap.width
                bh = sr.height if sr.height > 0 else obj.bitmap.height
                patch = obj.bitmap.image.crop((bx, by, min(obj.bitmap.width, bx + bw), min(obj.bitmap.height, by + bh)))
                if obj.mirror:
                    patch = patch.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
                if abs(obj.zoom_x - 1.0) > 0.01 or abs(obj.zoom_y - 1.0) > 0.01:
                    nw = max(1, int(round(patch.width * abs(obj.zoom_x))))
                    nh = max(1, int(round(patch.height * abs(obj.zoom_y))))
                    patch = patch.resize((nw, nh), Image.Resampling.BILINEAR)
                if abs(obj.angle) > 0.1:
                    patch = patch.rotate(obj.angle, expand=True)
                op = _clamp_u8(obj.opacity)
                if op < 255:
                    patch = patch.copy()
                    patch.putalpha(patch.getchannel("A").point(lambda v: (v * op) // 255))
                dx = int(round(obj.x - obj.ox + vx))
                dy = int(round(obj.y - obj.oy + vy))
                if dx < canvas.width and dy < canvas.height and dx + patch.width > 0 and dy + patch.height > 0:
                    tmp = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
                    tmp.paste(patch, (dx, dy))
                    if int(obj.blend_type) == 1:
                        canvas = ImageChops.add(canvas, tmp)
                    else:
                        canvas = Image.alpha_composite(canvas, tmp)
            elif isinstance(obj, RgssPlane) and obj.bitmap and not obj.bitmap.disposed:
                bw, bh = max(1, obj.bitmap.width), max(1, obj.bitmap.height)
                ox = obj.ox % bw
                oy = obj.oy % bh
                for ty in range(-oy, canvas.height, bh):
                    for tx in range(-ox, canvas.width, bw):
                        tmp = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
                        tmp.paste(obj.bitmap.image, (tx + vx, ty + vy))
                        canvas = Image.alpha_composite(canvas, tmp)
            elif isinstance(obj, RgssTilemap):
                self._render_tilemap(canvas, obj, vx, vy)
            elif isinstance(obj, RgssWindow) and obj.width > 0 and obj.height > 0 and obj.openness > 0:
                wx, wy = int(obj.x + vx), int(obj.y + vy)
                ww, wh = int(obj.width), max(2, int(obj.height * obj.openness // 255))
                wbox = Image.new("RGBA", (ww, wh), (22, 28, 52, _clamp_u8(obj.back_opacity * obj.opacity / 255.0)))
                wdraw = ImageDraw.Draw(wbox)
                wdraw.rectangle([0, 0, ww - 1, wh - 1], outline=(210, 220, 245, _clamp_u8(obj.opacity)), width=2)
                if obj.contents and not obj.contents.disposed and obj.openness >= 255:
                    wbox.alpha_composite(obj.contents.image.crop((obj.ox, obj.oy, min(obj.contents.width, obj.ox + max(1, ww - obj.padding * 2)), min(obj.contents.height, obj.oy + max(1, wh - obj.padding - obj.padding_bottom)))), dest=(obj.padding, obj.padding))
                tmp = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
                tmp.paste(wbox, (wx, wy))
                canvas = Image.alpha_composite(canvas, tmp)

        if self.brightness < 255:
            scale = max(0.0, min(1.0, self.brightness / 255.0))
            dark = Image.new("RGBA", canvas.size, (0, 0, 0, _clamp_u8((1.0 - scale) * 255.0)))
            canvas = Image.alpha_composite(canvas, dark)
        self.last_frame = canvas
        return canvas

    def _render_tilemap(self, canvas: Image.Image, tm: RgssTilemap, vx: int, vy: int) -> None:
        """Composites visible 32x32 tiles from an RGSS1 or RGSS3 Tilemap onto canvas."""
        if tm.map_data is None or tm.map_data.xsize <= 0 or tm.map_data.ysize <= 0:
            return
        ts = 32
        start_x = max(0, (int(tm.ox) - vx) // ts - 1)
        start_y = max(0, (int(tm.oy) - vy) // ts - 1)
        end_x = min(tm.map_data.xsize, start_x + canvas.width // ts + 3)
        end_y = min(tm.map_data.ysize, start_y + canvas.height // ts + 3)
        for z in range(tm.map_data.zsize):
            for ty in range(start_y, end_y):
                py = ty * ts - int(tm.oy) + vy
                for tx in range(start_x, end_x):
                    tid = tm.map_data.get(tx, ty, z)
                    if tid <= 0:
                        continue
                    px = tx * ts - int(tm.ox) + vx
                    src_tile = self._extract_rgss_tile(tm, tid)
                    if src_tile is not None:
                        canvas.alpha_composite(src_tile, dest=(px, py))

    def _extract_rgss_tile(self, tm: RgssTilemap, tid: int) -> Optional[Image.Image]:
        """Extracts a 32x32 tile patch from RMXP or VX Ace tilemap sheets."""
        if tm.mode == "rgss1":
            if tid >= 384 and tm.tileset and not tm.tileset.disposed:
                idx = tid - 384
                sx, sy = (idx % 8) * 32, (idx // 8) * 32
                if sx + 32 <= tm.tileset.width and sy + 32 <= tm.tileset.height:
                    return tm.tileset.image.crop((sx, sy, sx + 32, sy + 32))
            elif 48 <= tid < 384:
                slot = (tid - 48) // 48
                at = tm.autotiles[slot] if 0 <= slot < len(tm.autotiles) else None
                if at and not at.disposed and at.width >= 32 and at.height >= 32:
                    return at.image.crop((0, 0, 32, 32))
            return None
        # RGSS3 A5 (1536..2047) & B-E (0..1023)
        sheet = None
        local = tid
        if 0 <= tid < 256:
            sheet, local = tm.bitmaps[5], tid
        elif 256 <= tid < 512:
            sheet, local = tm.bitmaps[6], tid - 256
        elif 512 <= tid < 768:
            sheet, local = tm.bitmaps[7], tid - 512
        elif 768 <= tid < 1024:
            sheet, local = tm.bitmaps[8], tid - 768
        elif 1536 <= tid < 2048:
            sheet, local = tm.bitmaps[4], tid - 1536
        elif tid >= 2048:
            for b_idx in (0, 1, 2, 3):
                if tm.bitmaps[b_idx] and not tm.bitmaps[b_idx].disposed:
                    sheet = tm.bitmaps[b_idx]
                    return sheet.image.crop((0, 0, min(32, sheet.width), min(32, sheet.height)))
        if sheet and not sheet.disposed:
            if tid < 1024 and sheet.width >= 512:
                # VX Ace 16x16 tilesheet arranged as two 8-col halves
                half = local // 128
                sub = local % 128
                col = half * 8 + (sub % 8)
                row = sub // 8
            else:
                cols = max(1, sheet.width // 32)
                col = local % cols
                row = local // cols
            sx, sy = col * 32, row * 32
            if sx + 32 <= sheet.width and sy + 32 <= sheet.height:
                return sheet.image.crop((sx, sy, sx + 32, sy + 32))
        return None

    def dispatch(self, op: str, payload: Dict[str, Any]) -> Any:
        """Executes a host call from Ruby mruby VM and returns JSON-serializable result."""
        if op == "log":
            msg = str(payload.get("message", ""))
            self.logs.append(msg)
            return {"ok": True}

        if op == "graphics_info":
            return {
                "width": self.width,
                "height": self.height,
                "frame_rate": self.frame_rate,
                "frame_count": self.frame_count,
                "brightness": self.brightness,
                "version": self.version,
            }

        if op == "graphics_set":
            if "frame_rate" in payload:
                self.frame_rate = max(10, min(120, int(payload["frame_rate"])))
            if "frame_count" in payload:
                self.frame_count = max(0, int(payload["frame_count"]))
            if "brightness" in payload:
                self.brightness = _clamp_u8(payload["brightness"])
            return {"ok": True}

        if op == "graphics_resize":
            self.width = max(160, int(payload.get("width", self.width)))
            self.height = max(120, int(payload.get("height", self.height)))
            return {"width": self.width, "height": self.height}

        if op == "graphics_freeze":
            self.frozen_image = self.composite_frame()
            return {"ok": True}

        if op == "graphics_transition":
            self.frozen_image = None
            frame = self.composite_frame()
            if self.on_frame:
                self.on_frame(frame, self.frame_count)
            return {"ok": True}

        if op == "graphics_update":
            self.frame_count += 1
            self.input.update()
            frame = self.composite_frame()
            keep_going = True
            if self.on_frame:
                res = self.on_frame(frame, self.frame_count)
                if res is False:
                    keep_going = False
            return {"frame_count": self.frame_count, "continue": keep_going}

        if op == "input_state":
            kind = str(payload.get("kind", ""))
            if kind == "dir4":
                return self.input.dir4()
            if kind == "dir8":
                return self.input.dir8()
            sym = payload.get("sym")
            if kind == "trigger":
                return self.input.trigger(sym)
            if kind == "repeat":
                return self.input.repeat(sym)
            return self.input.press(sym)

        if op == "audio_cmd":
            fn = str(payload.get("fn", ""))
            fname = str(payload.get("filename", ""))
            vol = int(payload.get("volume", 100))
            pitch = int(payload.get("pitch", 100))
            pos = int(payload.get("pos", 0))
            if fn == "bgm_play":
                self.audio.bgm_play(fname, vol, pitch, pos)
            elif fn == "bgm_stop":
                self.audio.bgm_stop()
            elif fn == "bgm_fade":
                self.audio.bgm_fade(int(payload.get("time", 0)))
            elif fn == "bgs_play":
                self.audio.bgs_play(fname, vol, pitch, pos)
            elif fn == "bgs_stop":
                self.audio.bgs_stop()
            elif fn == "bgs_fade":
                self.audio.bgs_fade(int(payload.get("time", 0)))
            elif fn == "me_play":
                self.audio.me_play(fname, vol, pitch)
            elif fn == "me_stop":
                self.audio.me_stop()
            elif fn == "se_play":
                self.audio.se_play(fname, vol, pitch)
            elif fn == "se_stop":
                self.audio.se_stop()
            elif fn == "bgm_pos":
                return self.audio.bgm_pos
            elif fn == "bgs_pos":
                return self.audio.bgs_pos
            return {"ok": True}

        if op == "file_exist":
            return {"exists": self.resolver.exists(str(payload.get("path", "")))}

        if op == "load_data_json":
            rel = str(payload.get("path", ""))
            obj = self.resolver.resolve_data_object(rel)
            return self._serialize_ruby_like(obj)

        if op == "bitmap_new":
            if "path" in payload and payload["path"]:
                bm = RgssBitmap(str(payload["path"]), resolver=self.resolver)
            else:
                bm = RgssBitmap(int(payload.get("width", 1)), int(payload.get("height", 1)), resolver=self.resolver)
            return {"id": self._alloc(bm), "width": bm.width, "height": bm.height}

        if op == "bitmap_op":
            bm = self.get_handle(int(payload["id"]))
            if not isinstance(bm, RgssBitmap):
                return {"ok": False}
            fn = str(payload.get("fn", ""))
            if fn == "dispose":
                bm.dispose()
            elif fn == "clear":
                bm.clear()
            elif fn == "fill_rect":
                c = RgssColor(*payload.get("color", [0, 0, 0, 255]))
                bm.fill_rect(int(payload["x"]), int(payload["y"]), int(payload["w"]), int(payload["h"]), c)
            elif fn == "gradient_fill_rect":
                c1 = RgssColor(*payload.get("color1", [0, 0, 0, 255]))
                c2 = RgssColor(*payload.get("color2", [255, 255, 255, 255]))
                bm.gradient_fill_rect(int(payload["x"]), int(payload["y"]), int(payload["w"]), int(payload["h"]), c1, c2, bool(payload.get("vertical", False)))
            elif fn == "draw_text":
                f = payload.get("font", {})
                if isinstance(f, dict):
                    bm.font.size = int(f.get("size", bm.font.size))
                    bm.font.bold = bool(f.get("bold", bm.font.bold))
                    bm.font.color = RgssColor(*f.get("color", bm.font.color.to_rgba()))
                bm.draw_text(int(payload["x"]), int(payload["y"]), int(payload["w"]), int(payload["h"]), str(payload.get("text", "")), int(payload.get("align", 0)))
            elif fn == "text_size":
                r = bm.text_size(str(payload.get("text", "")))
                return {"x": r.x, "y": r.y, "width": r.width, "height": r.height}
            elif fn == "blt":
                src = self.get_handle(int(payload["src_id"]))
                sr = RgssRect(*payload.get("src_rect", [0, 0, 0, 0]))
                if isinstance(src, RgssBitmap):
                    bm.blt(int(payload["x"]), int(payload["y"]), src, sr, int(payload.get("opacity", 255)))
            elif fn == "hue_change":
                bm.hue_change(float(payload.get("hue", 0)))
            elif fn == "get_pixel":
                return list(bm.get_pixel(int(payload["x"]), int(payload["y"])).to_rgba())
            elif fn == "set_pixel":
                bm.set_pixel(int(payload["x"]), int(payload["y"]), RgssColor(*payload.get("color", [0, 0, 0, 255])))
            return {"width": bm.width, "height": bm.height, "disposed": bm.disposed}

        if op == "viewport_new":
            vp = RgssViewport(int(payload.get("x", 0)), int(payload.get("y", 0)), int(payload.get("width", self.width)), int(payload.get("height", self.height)))
            return {"id": self._alloc(vp)}

        if op == "sprite_new":
            vp = self.get_handle(int(payload["viewport_id"])) if payload.get("viewport_id") else None
            sp = RgssSprite(vp if isinstance(vp, RgssViewport) else None)
            return {"id": self._alloc(sp)}

        if op == "plane_new":
            vp = self.get_handle(int(payload["viewport_id"])) if payload.get("viewport_id") else None
            pl = RgssPlane(vp if isinstance(vp, RgssViewport) else None)
            return {"id": self._alloc(pl)}

        if op == "window_new":
            if all(k in payload for k in ("x", "y", "width", "height")):
                win = RgssWindow(int(payload["x"]), int(payload["y"]), int(payload["width"]), int(payload["height"]))
            else:
                vp = self.get_handle(int(payload["viewport_id"])) if payload.get("viewport_id") else None
                win = RgssWindow(vp if isinstance(vp, RgssViewport) else None)
            cid = self._alloc(win.contents)
            return {"id": self._alloc(win), "contents_id": cid}

        if op == "tilemap_new":
            vp = self.get_handle(int(payload["viewport_id"])) if payload.get("viewport_id") else None
            tm = RgssTilemap(vp if isinstance(vp, RgssViewport) else None, mode=self.version)
            return {"id": self._alloc(tm)}

        if op == "obj_sync":
            obj = self.get_handle(int(payload["id"]))
            if obj is None:
                return {"ok": False}
            if payload.get("dispose"):
                if hasattr(obj, "dispose"):
                    obj.dispose()
                return {"ok": True}
            for k, v in payload.get("props", {}).items():
                if k == "bitmap_id":
                    setattr(obj, "bitmap", self.get_handle(int(v)) if v else None)
                elif k == "windowskin_id":
                    setattr(obj, "windowskin", self.get_handle(int(v)) if v else None)
                elif k == "contents_id":
                    bm = self.get_handle(int(v)) if v else None
                    if isinstance(bm, RgssBitmap):
                        obj.contents = bm
                elif k == "tileset_id" and isinstance(obj, RgssTilemap):
                    bm = self.get_handle(int(v)) if v else None
                    obj.tileset = bm if isinstance(bm, RgssBitmap) else None
                elif k == "autotile_ids" and isinstance(obj, RgssTilemap) and isinstance(v, list):
                    for idx, bid in enumerate(v[:7]):
                        bm = self.get_handle(int(bid)) if bid else None
                        obj.autotiles[idx] = bm if isinstance(bm, RgssBitmap) else None
                elif k == "bitmap_ids" and isinstance(obj, RgssTilemap) and isinstance(v, list):
                    for idx, bid in enumerate(v[:9]):
                        bm = self.get_handle(int(bid)) if bid else None
                        obj.bitmaps[idx] = bm if isinstance(bm, RgssBitmap) else None
                elif k in ("map_data", "priorities", "flags") and isinstance(obj, RgssTilemap) and isinstance(v, dict):
                    tbl = RgssTableRuntime(int(v.get("xsize", 1)), int(v.get("ysize", 1)), int(v.get("zsize", 1)), list(v.get("data", [])))
                    setattr(obj, k, tbl)
                elif k == "src_rect" and isinstance(v, list) and len(v) == 4:
                    obj.src_rect = RgssRect(*v)
                elif k == "rect" and isinstance(v, list) and len(v) == 4:
                    obj.rect = RgssRect(*v)
                elif hasattr(obj, k):
                    setattr(obj, k, v)
            return {"ok": True}

        if op == "zlib_inflate":
            raw = base64.b64decode(str(payload.get("b64", "")))
            out = zlib.decompress(raw)
            return {"b64": base64.b64encode(out).decode("ascii")}

        if op == "zlib_deflate":
            raw = base64.b64decode(str(payload.get("b64", "")))
            out = zlib.compress(raw)
            return {"b64": base64.b64encode(out).decode("ascii")}

        if op == "marshal_loads_b64":
            raw = base64.b64decode(str(payload.get("b64", "")))
            return self._serialize_ruby_like(marshal_loads(raw))

        if op == "regex_match":
            pat = str(payload.get("pattern", ""))
            opt = str(payload.get("flags", ""))
            text = str(payload.get("text", ""))
            pos = int(payload.get("pos", 0))
            re_flags = 0
            if "i" in opt:
                re_flags |= re.IGNORECASE
            if "m" in opt:
                re_flags |= re.DOTALL | re.MULTILINE
            if "x" in opt:
                re_flags |= re.VERBOSE
            rx = re.compile(pat, re_flags)
            matches: List[Dict[str, Any]] = []
            if bool(payload.get("all", False)):
                for m in rx.finditer(text, pos):
                    matches.append({"start": m.start(), "end": m.end(), "groups": [m.group(0), *m.groups()]})
            else:
                m = rx.search(text, pos)
                if m is not None:
                    matches.append({"start": m.start(), "end": m.end(), "groups": [m.group(0), *m.groups()]})
            return {"matches": matches}

        return {"error": f"Unknown host op: {op}"}

    def _serialize_ruby_like(self, val: Any) -> Any:
        """Converts RubyObject / RgssTable / primitives into JSON-safe structures for Ruby hydration."""
        from .ir_converter import ruby_to_ir

        return ruby_to_ir(val)
