# -*- coding: utf-8 -*-
"""
RMXP 48-Pattern Autotile Quadrant Resolver.
Maps RMXP autotile pattern indices (0..47) and 8-neighbor terrain masks
to four 16x16 subtile coordinates within a standard 96x128 autotile sheet.
"""
from __future__ import annotations

from typing import List, Tuple

# Standard RPG Maker XP 48-pattern autotile table (1-indexed 16x16 subtiles on a 6x8 grid).
RMXP_AUTOTILE_PATTERNS: Tuple[Tuple[int, int, int, int], ...] = (
    (27, 28, 33, 34), (5, 28, 33, 34), (27, 6, 33, 34), (5, 6, 33, 34),
    (27, 28, 33, 12), (5, 28, 33, 12), (27, 6, 33, 12), (5, 6, 33, 12),
    (27, 28, 11, 34), (5, 28, 11, 34), (27, 6, 11, 34), (5, 6, 11, 34),
    (27, 28, 11, 12), (5, 28, 11, 12), (27, 6, 11, 12), (5, 6, 11, 12),
    (25, 26, 31, 32), (25, 6, 31, 32), (25, 26, 31, 12), (25, 6, 31, 12),
    (15, 16, 21, 22), (15, 16, 21, 12), (15, 16, 11, 22), (15, 16, 11, 12),
    (29, 30, 35, 36), (29, 30, 11, 36), (5, 30, 35, 36), (5, 30, 11, 36),
    (39, 40, 45, 46), (5, 40, 45, 46), (39, 6, 45, 46), (5, 6, 45, 46),
    (25, 30, 31, 36), (15, 16, 45, 46), (13, 14, 19, 20), (13, 14, 19, 12),
    (17, 18, 23, 24), (17, 18, 11, 24), (41, 42, 47, 48), (5, 42, 47, 48),
    (37, 38, 43, 44), (37, 6, 43, 44), (13, 18, 19, 24), (13, 14, 43, 44),
    (37, 42, 43, 48), (17, 18, 47, 48), (13, 18, 43, 48), (1, 2, 7, 8),
)


def autotile_frame_count(sheet_width: int) -> int:
    """Returns number of 96px-wide animation frames in an RMXP autotile sheet."""
    return max(1, sheet_width // 96)


def autotile_frame_x_offset(frame_index: int, sheet_width: int) -> int:
    """Returns horizontal pixel offset for the active autotile animation frame."""
    frames = autotile_frame_count(sheet_width)
    return (frame_index % frames) * 96


def subtile_pixel_rect(subtile_id: int, frame_x_offset: int = 0) -> Tuple[int, int, int, int]:
    """Converts a 1-indexed subtile ID (1..48) to (sx, sy, 16, 16) inside 96*N x 128."""
    idx = max(0, min(47, subtile_id - 1))
    col = idx % 6
    row = idx // 6
    return frame_x_offset + col * 16, row * 16, 16, 16


def quads_from_neighbors(
    up: bool,
    down: bool,
    left: bool,
    right: bool,
    up_left: bool,
    up_right: bool,
    down_left: bool,
    down_right: bool,
) -> Tuple[int, int, int, int]:
    """Computes the four 1-indexed 16x16 subtile IDs from 8-neighbor connectivity."""
    if not up and not left:
        tl = 13
    elif not up:
        tl = 15
    elif not left:
        tl = 25
    elif not up_left:
        tl = 5
    else:
        tl = 27

    if not up and not right:
        tr = 18
    elif not up:
        tr = 16
    elif not right:
        tr = 30
    elif not up_right:
        tr = 6
    else:
        tr = 28

    if not down and not left:
        bl = 43
    elif not down:
        bl = 45
    elif not left:
        bl = 31
    elif not down_left:
        bl = 11
    else:
        bl = 33

    if not down and not right:
        br = 48
    elif not down:
        br = 46
    elif not right:
        br = 36
    elif not down_right:
        br = 12
    else:
        br = 34

    return tl, tr, bl, br


def resolve_autotile_quads(grid: List[List[int]], x: int, y: int, tile_id: int) -> Tuple[int, int, int, int]:
    """
    Returns the four 16x16 subtile IDs for an autotile ID (48..383).
    If a specific non-zero pattern index (1..47) is stored directly, uses the
    RMXP pattern table; if pattern 0 is placed at a boundary, resolves neighbors.
    """
    slot = (tile_id - 48) // 48
    pattern_idx = (tile_id - 48) % 48
    if pattern_idx != 0:
        return RMXP_AUTOTILE_PATTERNS[pattern_idx]

    h = len(grid)
    w = len(grid[0]) if h > 0 else 0

    def same_slot(nx: int, ny: int) -> bool:
        if nx < 0 or ny < 0 or nx >= w or ny >= h:
            return True  # Map edges continue seamlessly in RMXP
        ntid = int(grid[ny][nx])
        return 48 <= ntid < 384 and ((ntid - 48) // 48) == slot

    return quads_from_neighbors(
        up=same_slot(x, y - 1),
        down=same_slot(x, y + 1),
        left=same_slot(x - 1, y),
        right=same_slot(x + 1, y),
        up_left=same_slot(x - 1, y - 1),
        up_right=same_slot(x + 1, y - 1),
        down_left=same_slot(x - 1, y + 1),
        down_right=same_slot(x + 1, y + 1),
    )
