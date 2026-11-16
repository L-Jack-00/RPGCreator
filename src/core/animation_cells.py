# -*- coding: utf-8 -*-
"""Shared RPG::Animation frame cell decoding used by the player and the studio preview."""
from __future__ import annotations

from typing import Any, Dict, List


def extract_animation_frame_cells(
    anim_entry: Dict[str, Any],
    frame_idx: int,
) -> List[Dict[str, int]]:
    """Extracts active cell sprites (pattern, x, y, zoom, angle, mirror, opacity, blend_type) from an RPG::Animation frame."""
    af = anim_entry.get("fields", {}) if isinstance(anim_entry, dict) else {}
    frames = af.get("frames", [])
    if not isinstance(frames, list) or not (0 <= frame_idx < len(frames)):
        return []
    ff = frames[frame_idx].get("fields", {}) if isinstance(frames[frame_idx], dict) else {}
    cell_max = int(ff.get("cell_max", 0))
    if cell_max <= 0:
        return []
    tbl = ff.get("cell_data", {})
    xsize = max(1, int(tbl.get("xsize", cell_max) or cell_max))
    ysize = int(tbl.get("ysize", 8) or 8)
    data = tbl.get("data", [])
    if not isinstance(data, list) or not data:
        return []
    cells: List[Dict[str, int]] = []
    limit = min(cell_max, xsize)
    for cx in range(limit):
        if cx >= len(data):
            break
        pat = int(data[cx])
        if pat < 0:
            continue
        def _prop(py: int, default: int = 0) -> int:
            idx = cx + py * xsize
            return int(data[idx]) if py < ysize and idx < len(data) else default

        cells.append({
            "pattern": pat,
            "x": _prop(1, 0),
            "y": _prop(2, 0),
            "zoom": max(10, _prop(3, 100)),
            "angle": _prop(4, 0),
            "mirror": 1 if _prop(5, 0) != 0 else 0,
            "opacity": max(0, min(255, _prop(6, 255))),
            "blend_type": _prop(7, 0),
        })
    return cells
