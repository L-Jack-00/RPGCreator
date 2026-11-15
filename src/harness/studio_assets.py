# -*- coding: utf-8 -*-
"""
Studio Graphics & Tile Atlas Cache.
Resolves RMXP Graphics/ directories and builds zoom-cached PIL/Tkinter tile
and animation frame images with crisp procedural fallbacks when raw PNGs are absent.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from PIL import Image, ImageDraw, ImageTk

from ..core.autotile import RMXP_AUTOTILE_PATTERNS, subtile_pixel_rect
from ..core.utils import load_json


def find_graphics_dir(ir_dir: Path, explicit_dir: Optional[Path] = None) -> Optional[Path]:
    """Locates the active Graphics directory relative to the IR workspace."""
    if explicit_dir and Path(explicit_dir).is_dir():
        return Path(explicit_dir)
    ir_path = Path(ir_dir)
    for cand in (
        ir_path / "Graphics",
        ir_path.parent / "Graphics",
        ir_path.parent / "rmxp" / "Graphics",
        ir_path.parent.parent / "Graphics",
    ):
        if cand.is_dir():
            return cand
    return None


def _procedural_tile_color(tile_id: int) -> Tuple[int, int, int]:
    """Deterministic pleasing color for any RMXP tile ID when sheet PNGs are missing."""
    if tile_id == 384:
        return (76, 138, 72)
    if tile_id == 385:
        return (176, 152, 108)
    if tile_id == 386:
        return (74, 118, 178)
    if tile_id == 387:
        return (110, 106, 114)
    if 48 <= tile_id < 384:
        at_idx = (tile_id - 48) // 48
        palette = [
            (62, 132, 194),
            (66, 148, 86),
            (168, 132, 84),
            (126, 98, 148),
            (184, 90, 74),
            (94, 154, 148),
            (138, 138, 104),
        ]
        return palette[at_idx % len(palette)]
    digest = hashlib.md5(str(tile_id).encode("ascii")).digest()
    return (60 + digest[0] % 140, 60 + digest[1] % 140, 60 + digest[2] % 140)


class TileAtlasCache:
    """Loads tileset/autotile PNG sheets and caches Tkinter PhotoImages by zoom size."""

    def __init__(self, master: Any, ir_dir: Path, graphics_dir: Optional[Path] = None):
        self.master = master
        self.ir_dir = Path(ir_dir)
        self.graphics_dir = find_graphics_dir(self.ir_dir, graphics_dir)
        self._tilesets_meta: Dict[int, Dict[str, Any]] = {}
        self._sheet_cache: Dict[str, Optional[Image.Image]] = {}
        self._tile_rgba_cache: Dict[Tuple[int, int], Image.Image] = {}
        self._tk_cache: Dict[Tuple[int, int, int], ImageTk.PhotoImage] = {}
        self._anim_tk_cache: Dict[Tuple[str, int, int], ImageTk.PhotoImage] = {}
        self.reload_metadata()

    def reload_metadata(self, graphics_dir: Optional[Path] = None) -> None:
        if graphics_dir is not None:
            self.graphics_dir = find_graphics_dir(self.ir_dir, graphics_dir)
        else:
            self.graphics_dir = find_graphics_dir(self.ir_dir, self.graphics_dir)
        self._tilesets_meta.clear()
        self._sheet_cache.clear()
        self._tile_rgba_cache.clear()
        self._tk_cache.clear()
        self._anim_tk_cache.clear()
        ts_path = self.ir_dir / "database" / "tilesets.json"
        if ts_path.is_file():
            for idx, entry in enumerate(load_json(ts_path)):
                if isinstance(entry, dict) and isinstance(entry.get("fields"), dict):
                    flds = entry["fields"]
                    tid = int(flds.get("id", idx))
                    self._tilesets_meta[tid] = flds

    def _load_png(self, sub_folder: str, base_name: str) -> Optional[Image.Image]:
        key = f"{sub_folder}/{base_name}"
        if key in self._sheet_cache:
            return self._sheet_cache[key]
        img_out: Optional[Image.Image] = None
        if self.graphics_dir and base_name:
            folder = self.graphics_dir / sub_folder
            for ext in ("", ".png", ".jpg", ".bmp"):
                cand = folder / f"{base_name}{ext}"
                if cand.is_file():
                    with Image.open(cand) as im:
                        img_out = im.convert("RGBA")
                    break
        self._sheet_cache[key] = img_out
        return img_out

    def tileset_max_tile_id(self, tileset_id: int) -> int:
        meta = self._tilesets_meta.get(int(tileset_id), {})
        tname = str(meta.get("tileset_name", ""))
        sheet = self._load_png("Tilesets", tname)
        if sheet is not None:
            rows = max(1, sheet.height // 32)
            return 384 + rows * 8
        passages = meta.get("passages", {}).get("data", []) if isinstance(meta.get("passages"), dict) else []
        return max(448, len(passages))

    def get_tile_rgba(self, tileset_id: int, tile_id: int) -> Image.Image:
        key = (int(tileset_id), int(tile_id))
        cached = self._tile_rgba_cache.get(key)
        if cached is not None:
            return cached

        tid = int(tile_id)
        if tid <= 0:
            im = Image.new("RGBA", (32, 32), (0, 0, 0, 0))
            self._tile_rgba_cache[key] = im
            return im

        meta = self._tilesets_meta.get(int(tileset_id), {})
        if 48 <= tid < 384:
            at_idx = (tid - 48) // 48
            pat_idx = (tid - 48) % 48
            at_names = list(meta.get("autotile_names", []))
            at_name = str(at_names[at_idx]) if at_idx < len(at_names) else ""
            sheet = self._load_png("Autotiles", at_name)
            if sheet is not None and sheet.width >= 96 and sheet.height >= 128:
                quads = RMXP_AUTOTILE_PATTERNS[pat_idx]
                out = Image.new("RGBA", (32, 32), (0, 0, 0, 0))
                offsets = ((0, 0), (16, 0), (0, 16), (16, 16))
                for q_id, (ox, oy) in zip(quads, offsets):
                    sx, sy, sw, sh = subtile_pixel_rect(q_id, 0)
                    out.paste(sheet.crop((sx, sy, sx + sw, sy + sh)), (ox, oy))
                self._tile_rgba_cache[key] = out
                return out
        elif tid >= 384:
            tname = str(meta.get("tileset_name", ""))
            sheet = self._load_png("Tilesets", tname)
            if sheet is not None:
                idx = tid - 384
                col = idx % 8
                row = idx // 8
                if (row + 1) * 32 <= sheet.height and (col + 1) * 32 <= sheet.width:
                    crop = sheet.crop((col * 32, row * 32, (col + 1) * 32, (row + 1) * 32))
                    self._tile_rgba_cache[key] = crop
                    return crop

        # Crisp procedural fallback tile with subtle bevel & tile ID tag
        r, g, b = _procedural_tile_color(tid)
        im = Image.new("RGBA", (32, 32), (r, g, b, 255))
        draw = ImageDraw.Draw(im)
        hi = (min(255, r + 24), min(255, g + 24), min(255, b + 24), 255)
        lo = (max(0, r - 28), max(0, g - 28), max(0, b - 28), 255)
        draw.line([(0, 0), (31, 0), (0, 0), (0, 31)], fill=hi)
        draw.line([(31, 1), (31, 31), (1, 31)], fill=lo)
        draw.text((3, 18), str(tid)[-4:], fill=(245, 245, 245, 220))
        self._tile_rgba_cache[key] = im
        return im

    def get_tk_tile(self, tileset_id: int, tile_id: int, tile_px: int = 32) -> Optional[ImageTk.PhotoImage]:
        tid = int(tile_id)
        if tid <= 0:
            return None
        ck = (int(tileset_id), tid, int(tile_px))
        if ck in self._tk_cache:
            return self._tk_cache[ck]
        rgba = self.get_tile_rgba(tileset_id, tid)
        if int(tile_px) != 32:
            rgba = rgba.resize((int(tile_px), int(tile_px)), Image.Resampling.NEAREST)
        tk_img = ImageTk.PhotoImage(rgba, master=self.master)
        self._tk_cache[ck] = tk_img
        return tk_img

    def render_map_tk(
        self, map_data: Dict[str, Any], tile_px: int = 32, show_grid: bool = True
    ) -> ImageTk.PhotoImage:
        """Composites all map layers and optional grid into a single PhotoImage."""
        w = max(1, int(map_data.get("width", 20)))
        h = max(1, int(map_data.get("height", 15)))
        px = max(8, int(tile_px))
        ts_id = int(map_data.get("tileset_id", 1))
        layers = map_data.get("layers", {})

        canvas_im = Image.new("RGBA", (w * 32, h * 32), (21, 24, 32, 255))
        for l_idx in (0, 1, 2):
            grid = layers.get(f"layer_{l_idx}", [])
            for y in range(min(h, len(grid))):
                row = grid[y]
                for x in range(min(w, len(row))):
                    tid = int(row[x])
                    if tid <= 0:
                        continue
                    t_im = self.get_tile_rgba(ts_id, tid)
                    canvas_im.paste(t_im, (x * 32, y * 32), t_im)

        if px != 32:
            canvas_im = canvas_im.resize((w * px, h * px), Image.Resampling.NEAREST)

        if show_grid:
            draw = ImageDraw.Draw(canvas_im)
            grid_rgba = (48, 54, 66, 255)
            max_x, max_y = w * px, h * px
            for x in range(w + 1):
                gx = min(max_x - 1, x * px) if x == w else x * px
                draw.line([(gx, 0), (gx, max_y)], fill=grid_rgba, width=1)
            for y in range(h + 1):
                gy = min(max_y - 1, y * px) if y == h else y * px
                draw.line([(0, gy), (max_x, gy)], fill=grid_rgba, width=1)

        tk_img = ImageTk.PhotoImage(canvas_im, master=self.master)
        self._map_tk_ref = tk_img
        return tk_img

    def render_palette_tk(
        self, tile_ids: List[int], tileset_id: int, cols: int = 5, cell: int = 34
    ) -> ImageTk.PhotoImage:
        """Composites all palette tiles into one PhotoImage to minimize Canvas items."""
        rows = max(1, (len(tile_ids) + cols - 1) // cols)
        pw = cols * cell + 8
        ph = rows * cell + 8
        pal_im = Image.new("RGBA", (pw, ph), (26, 29, 36, 255))
        draw = ImageDraw.Draw(pal_im)
        for idx, tid in enumerate(tile_ids):
            c = idx % cols
            r = idx // cols
            x0, y0 = c * cell + 4, r * cell + 4
            if tid == 0:
                draw.rectangle([x0, y0, x0 + 31, y0 + 31], fill=(17, 17, 17, 255), outline=(102, 102, 102, 255))
                draw.text((x0 + 6, y0 + 11), "DEL", fill=(238, 102, 102, 255))
            else:
                t_im = self.get_tile_rgba(tileset_id, tid)
                pal_im.paste(t_im, (x0, y0), t_im)
        tk_img = ImageTk.PhotoImage(pal_im, master=self.master)
        self._palette_tk_ref = tk_img
        return tk_img

    def get_palette_tiles(self, tileset_id: int, max_tileset_tiles: int = 96) -> List[int]:
        """Returns compact selectable tile IDs: [0 (eraser), autotiles 48..336, tileset 384..]."""
        ids: List[int] = [0]
        meta = self._tilesets_meta.get(int(tileset_id), {})
        at_names = list(meta.get("autotile_names", ["", "", "", "", "", "", ""]))
        for idx in range(min(7, max(len(at_names), 1))):
            ids.append(48 + idx * 48)
        max_id = min(self.tileset_max_tile_id(tileset_id), 384 + max_tileset_tiles)
        for tid in range(384, max_id):
            ids.append(tid)
        return ids

    def get_animation_frame_tk(
        self,
        animation_name: str,
        pattern: int = 0,
        size_px: int = 96,
    ) -> ImageTk.PhotoImage:
        """Returns a single 192x192 RMXP animation cell preview (or procedural fallback)."""
        ck = (str(animation_name), int(pattern), int(size_px))
        if ck in self._anim_tk_cache:
            return self._anim_tk_cache[ck]
        sheet = self._load_png("Animations", str(animation_name)) if animation_name else None
        if sheet is not None and sheet.width >= 192 and sheet.height >= 192:
            cols = max(1, sheet.width // 192)
            col = int(pattern) % cols
            row = int(pattern) // cols
            if (row + 1) * 192 <= sheet.height:
                cell = sheet.crop((col * 192, row * 192, (col + 1) * 192, (row + 1) * 192))
                cell = cell.resize((size_px, size_px), Image.Resampling.BILINEAR)
                tk_im = ImageTk.PhotoImage(cell, master=self.master)
                self._anim_tk_cache[ck] = tk_im
                return tk_im

        im = Image.new("RGBA", (size_px, size_px), (28, 32, 44, 255))
        draw = ImageDraw.Draw(im)
        cx, cy = size_px // 2, size_px // 2
        radius = max(8, (size_px // 3) - (int(pattern) % 5) * 3)
        draw.ellipse((cx - radius, cy - radius, cx + radius, cy + radius), outline=(255, 200, 72, 255), width=3)
        draw.text((6, 6), f"P{int(pattern)}", fill=(220, 230, 245, 255))
        tk_im = ImageTk.PhotoImage(im, master=self.master)
        self._anim_tk_cache[ck] = tk_im
        return tk_im
