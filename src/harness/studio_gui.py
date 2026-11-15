# -*- coding: utf-8 -*-
"""
RPG Creator Studio GUI (Tkinter + Pillow).
Provides visual layered map & tile painting, multi-page event authoring,
full RMXP database & balance fine-tuning, common event & animation editing,
headless verification/fuzzing/i18n/release bundling, Pygame playtest launch,
and an integrated bottom-docked Agent / CLI instant command console.
"""
from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, simpledialog, ttk
from typing import Any, Dict, List, Optional, Tuple

from ..core.editor import (
    _build_ir_page,
    add_event,
    create_map,
    delete_event,
    delete_map,
    list_maps,
    paint_map,
    redo_last_edit,
    save_event,
    set_db_entry,
    undo_last_edit,
    update_map_properties,
)
from ..core.importers import import_rpgmaker_mv_mz, import_tiled_map, import_vxace_rxdata
from ..core.ir_converter import TRIGGER_NAME_TO_INT, pack_ir_to_rmxp, unpack_rmxp_to_ir
from ..core.rgssad import DEFAULT_RGSSAD_KEY, pack_rgssad, unpack_rgssad
from ..core.sample_generator import generate_sample_project
from ..core.utils import load_json
from .agent_bridge import execute_studio_command, format_agent_prompt_packet, parse_agent_instruction
from .studio_assets import TileAtlasCache
from .studio_tabs import AssetBrowserTab, CommonAnimTab, DSL_TEMPLATES, DatabaseTab, OpsReleaseTab
from .window_opt import (
    PREVIEW_GRAY_DARK_HEX,
    PREVIEW_GRAY_LIGHT_HEX,
    attach_outline_drag_preview,
    create_preview_paned,
)

_ROOT_DIR = Path(__file__).resolve().parents[2]


def _find_studio_icon_path() -> Optional[Path]:
    """Locates RPGCreatorStudio.ico across frozen MEIPASS bundle, executable folder, and project root."""
    candidates: List[Path] = []
    if hasattr(sys, "_MEIPASS"):
        candidates.append(Path(getattr(sys, "_MEIPASS")) / "RPGCreatorStudio.ico")
    candidates.extend([
        Path(sys.executable).resolve().parent / "RPGCreatorStudio.ico",
        _ROOT_DIR / "RPGCreatorStudio.ico",
    ])
    for cand in candidates:
        if cand.is_file():
            return cand
    return None
_MOVE_TYPE_LABELS: Tuple[str, ...] = ("0: 고정", "1: 랜덤", "2: 접근", "3: 커스텀")
_MOVE_SPEED_LABELS: Tuple[str, ...] = ("1: 아주 느림", "2: 느림", "3: 보통", "4: 빠름", "5: 매우 빠름", "6: 최속")
_MOVE_FREQ_LABELS: Tuple[str, ...] = ("1: 최하", "2: 낮음", "3: 보통", "4: 높음", "5: 매우 높음", "6: 최고")


class MapEditorTab(ttk.Frame):
    """Visual Map, Tileset Palette, Layer Brush/Rect/Eraser/Eyedropper, and Multi-Page Event Editor."""

    def __init__(self, master: tk.Misc, app: Any):
        super().__init__(master)
        self.app = app
        self.current_map_id: int = 1
        self.current_map_data: Dict[str, Any] = {}
        self.active_tile_id: int = 385
        self.active_layer = tk.IntVar(value=0)
        self.active_tool = tk.StringVar(value="brush")  # brush | rect | eraser | eyedropper | event | start
        self.tile_px = tk.IntVar(value=32)
        self.show_grid = tk.BooleanVar(value=True)
        self.selected_event_id: Optional[int] = None
        self.selected_page_idx: int = 0
        self._working_event: Dict[str, Any] = {}
        self._event_clean_fp: str = ""
        self._other_pages_dirty: bool = False
        self._stroke_points: List[Tuple[int, int]] = []
        self._rect_start: Optional[Tuple[int, int]] = None
        self._rect_preview_id: Optional[int] = None
        self._canvas_img_refs: List[Any] = []
        self._palette_img_refs: List[Any] = []
        self._palette_rects: Dict[int, Tuple[int, int]] = {}
        self._build_ui()

    def _build_ui(self) -> None:
        paned = create_preview_paned(self, orient="horizontal")
        paned.pack(fill="both", expand=True, padx=4, pady=4)

        # 1) Far Left: Maps & Map Metadata Properties
        col_map = ttk.Frame(paned, width=230)
        paned.add(col_map, weight=1)

        ttk.Label(col_map, text="맵 목록 (Maps)", font=("", 9, "bold")).pack(anchor="w")
        self.map_list = tk.Listbox(col_map, height=7, exportselection=False)
        self.map_list.pack(fill="x", pady=2)
        self.map_list.bind("<<ListboxSelect>>", lambda _e: self._on_select_map())

        mb_row = ttk.Frame(col_map)
        mb_row.pack(fill="x", pady=(0, 6))
        ttk.Button(mb_row, text="+ 맵 생성", command=self.create_new_map).pack(side="left", fill="x", expand=True, padx=1)
        ttk.Button(mb_row, text="맵 삭제", command=self.delete_current_map).pack(side="left", fill="x", expand=True, padx=1)

        props_box = ttk.LabelFrame(col_map, text="맵 상세 속성 미세조정")
        props_box.pack(fill="both", expand=True)

        self.mp_name = tk.StringVar()
        self.mp_w = tk.IntVar(value=20)
        self.mp_h = tk.IntVar(value=15)
        self.mp_ts = tk.IntVar(value=1)
        self.mp_bgm = tk.StringVar()
        self.mp_auto_bgm = tk.BooleanVar(value=False)
        self.mp_bgs = tk.StringVar()
        self.mp_auto_bgs = tk.BooleanVar(value=False)
        self.mp_parallax = tk.StringVar()
        self.mp_enc_step = tk.IntVar(value=30)
        self.mp_enc_list = tk.StringVar(value="")

        form = ttk.Frame(props_box)
        form.pack(fill="x", padx=4, pady=4)
        fields = [
            ("이름", ttk.Entry(form, textvariable=self.mp_name, width=16)),
            ("너비(W)", ttk.Spinbox(form, from_=5, to=200, textvariable=self.mp_w, width=7)),
            ("높이(H)", ttk.Spinbox(form, from_=5, to=200, textvariable=self.mp_h, width=7)),
            ("타일셋ID", ttk.Spinbox(form, from_=1, to=99, textvariable=self.mp_ts, width=7)),
            ("BGM", ttk.Entry(form, textvariable=self.mp_bgm, width=16)),
            ("BGS", ttk.Entry(form, textvariable=self.mp_bgs, width=16)),
            ("패럴랙스", ttk.Entry(form, textvariable=self.mp_parallax, width=16)),
            ("인카운트걸음", ttk.Spinbox(form, from_=1, to=999, textvariable=self.mp_enc_step, width=7)),
            ("트룹 IDs", ttk.Entry(form, textvariable=self.mp_enc_list, width=16)),
        ]
        for r_idx, (lbl, wdg) in enumerate(fields):
            ttk.Label(form, text=lbl, width=10).grid(row=r_idx, column=0, sticky="w", pady=1)
            wdg.grid(row=r_idx, column=1, sticky="ew", pady=1)
        form.columnconfigure(1, weight=1)

        chk_row = ttk.Frame(props_box)
        chk_row.pack(fill="x", padx=4)
        ttk.Checkbutton(chk_row, text="자동BGM", variable=self.mp_auto_bgm).pack(side="left")
        ttk.Checkbutton(chk_row, text="자동BGS", variable=self.mp_auto_bgs).pack(side="left", padx=4)
        ttk.Button(props_box, text="맵 속성 저장 (Undo 지원)", command=self.save_map_props).pack(fill="x", padx=4, pady=6)

        # 2) Tile Palette Column
        col_pal = ttk.Frame(paned, width=210)
        paned.add(col_pal, weight=1)

        self.lbl_active_tile = ttk.Label(col_pal, text=f"선택 타일 ID: {self.active_tile_id}", font=("", 9, "bold"))
        self.lbl_active_tile.pack(anchor="w")
        pal_ctrl = ttk.Frame(col_pal)
        pal_ctrl.pack(fill="x", pady=2)
        self.custom_tile_var = tk.IntVar(value=385)
        ttk.Spinbox(pal_ctrl, from_=0, to=9999, textvariable=self.custom_tile_var, width=6).pack(side="left")
        ttk.Button(pal_ctrl, text="ID 선택", command=self._apply_custom_tile_id).pack(side="left", padx=3)

        pal_wrap = ttk.Frame(col_pal)
        pal_wrap.pack(fill="both", expand=True)
        self.palette_canvas = tk.Canvas(pal_wrap, width=184, bg="#20242c", highlightthickness=1)
        pal_sb = ttk.Scrollbar(pal_wrap, orient="vertical", command=self.palette_canvas.yview)
        self.palette_canvas.configure(yscrollcommand=pal_sb.set)
        self.palette_canvas.pack(side="left", fill="both", expand=True)
        pal_sb.pack(side="right", fill="y")
        self.palette_canvas.bind("<Button-1>", self._on_palette_click)

        # 3) Center: Tool Bar + Layered 32x32 Map Canvas
        col_canvas = ttk.Frame(paned)
        paned.add(col_canvas, weight=3)

        tbar = ttk.Frame(col_canvas)
        tbar.pack(fill="x", pady=(0, 3))
        ttk.Label(tbar, text="레이어:").pack(side="left")
        for l_idx in (0, 1, 2):
            ttk.Radiobutton(tbar, text=f"L{l_idx}", value=l_idx, variable=self.active_layer).pack(side="left", padx=2)
        ttk.Separator(tbar, orient="vertical").pack(side="left", fill="y", padx=5)
        tools = [
            ("붓", "brush"),
            ("사각형", "rect"),
            ("지우개", "eraser"),
            ("스포이드", "eyedropper"),
            ("이벤트", "event"),
            ("시작위치", "start"),
        ]
        for lbl, tval in tools:
            ttk.Radiobutton(tbar, text=lbl, value=tval, variable=self.active_tool).pack(side="left", padx=2)
        ttk.Separator(tbar, orient="vertical").pack(side="left", fill="y", padx=5)
        ttk.Checkbutton(tbar, text="격자", variable=self.show_grid, command=self.redraw_map).pack(side="left")
        zcb = ttk.Combobox(tbar, textvariable=self.tile_px, values=[24, 32, 40], width=4, state="readonly")
        zcb.pack(side="left", padx=4)
        zcb.bind("<<ComboboxSelected>>", lambda _e: self.redraw_map())

        self.lbl_hover = ttk.Label(col_canvas, text="좌표: (-, -) | 우클릭: 스포이드")
        self.lbl_hover.pack(anchor="w")

        c_wrap = ttk.Frame(col_canvas)
        c_wrap.pack(fill="both", expand=True)
        self.map_canvas = tk.Canvas(c_wrap, bg="#151820", highlightthickness=0)
        xsb = ttk.Scrollbar(c_wrap, orient="horizontal", command=self.map_canvas.xview)
        ysb = ttk.Scrollbar(c_wrap, orient="vertical", command=self.map_canvas.yview)
        self.map_canvas.configure(xscrollcommand=xsb.set, yscrollcommand=ysb.set)
        self.map_canvas.grid(row=0, column=0, sticky="nsew")
        ysb.grid(row=0, column=1, sticky="ns")
        xsb.grid(row=1, column=0, sticky="ew")
        c_wrap.rowconfigure(0, weight=1)
        c_wrap.columnconfigure(0, weight=1)

        self.map_canvas.bind("<ButtonPress-1>", self._on_canvas_press)
        self.map_canvas.bind("<B1-Motion>", self._on_canvas_drag)
        self.map_canvas.bind("<ButtonRelease-1>", self._on_canvas_release)
        self.map_canvas.bind("<Button-3>", self._on_canvas_right_click)
        self.map_canvas.bind("<Motion>", self._on_canvas_hover)

        # 4) Right: Event List + Multi-Page Fine-Tuning Inspector
        col_ev = ttk.Frame(paned, width=320)
        paned.add(col_ev, weight=2)

        ttk.Label(col_ev, text="맵 이벤트 & 페이지 인스펙터", font=("", 9, "bold")).pack(anchor="w")
        self.ev_list = tk.Listbox(col_ev, height=5, exportselection=False)
        self.ev_list.pack(fill="x", pady=2)
        self.ev_list.bind("<<ListboxSelect>>", lambda _e: self._on_select_event())

        ev_btns = ttk.Frame(col_ev)
        ev_btns.pack(fill="x", pady=2)
        ttk.Button(ev_btns, text="+ 신규 이벤트", command=self._on_new_event_clicked).pack(side="left", fill="x", expand=True, padx=1)
        ttk.Button(ev_btns, text="이벤트 저장", command=self.save_current_event).pack(side="left", fill="x", expand=True, padx=1)
        ttk.Button(ev_btns, text="삭제", command=self.delete_selected_event).pack(side="left", fill="x", expand=True, padx=1)

        ev_meta = ttk.Frame(col_ev)
        ev_meta.pack(fill="x", pady=3)
        self.ev_id_var = tk.IntVar(value=1)
        self.ev_name_var = tk.StringVar(value="EV001")
        self.ev_x_var = tk.IntVar(value=0)
        self.ev_y_var = tk.IntVar(value=0)
        ttk.Label(ev_meta, text="ID").grid(row=0, column=0, sticky="w")
        ttk.Entry(ev_meta, textvariable=self.ev_id_var, width=5, state="readonly").grid(row=0, column=1, padx=2)
        ttk.Label(ev_meta, text="이름").grid(row=0, column=2, sticky="w")
        ttk.Entry(ev_meta, textvariable=self.ev_name_var, width=14).grid(row=0, column=3, sticky="ew", padx=2)
        ttk.Label(ev_meta, text="X").grid(row=1, column=0, sticky="w")
        ttk.Spinbox(ev_meta, from_=0, to=200, textvariable=self.ev_x_var, width=5).grid(row=1, column=1, padx=2)
        ttk.Label(ev_meta, text="Y").grid(row=1, column=2, sticky="w")
        ttk.Spinbox(ev_meta, from_=0, to=200, textvariable=self.ev_y_var, width=5).grid(row=1, column=3, sticky="w", padx=2)
        ev_meta.columnconfigure(3, weight=1)

        page_bar = ttk.Frame(col_ev)
        page_bar.pack(fill="x", pady=2)
        ttk.Label(page_bar, text="페이지:").pack(side="left")
        self.page_var = tk.StringVar(value="Page 1")
        self.page_cb = ttk.Combobox(page_bar, textvariable=self.page_var, width=10, state="readonly")
        self.page_cb.pack(side="left", padx=3)
        self.page_cb.bind("<<ComboboxSelected>>", lambda _e: self._on_switch_page())
        ttk.Button(page_bar, text="+페이지", command=self._add_event_page).pack(side="left", padx=2)
        ttk.Button(page_bar, text="-페이지", command=self._remove_event_page).pack(side="left", padx=2)

        pg_box = ttk.LabelFrame(col_ev, text="페이지 조건 · 그래픽 · 이동")
        pg_box.pack(fill="x", pady=2)

        self.pg_trig_var = tk.StringVar(value="action_button")
        self.pg_graphic_var = tk.StringVar(value="")
        self.pg_sw1_var = tk.IntVar(value=0)
        self.pg_sw2_var = tk.IntVar(value=0)
        self.pg_var_id = tk.IntVar(value=0)
        self.pg_var_val = tk.IntVar(value=0)
        self.pg_self_sw = tk.StringVar(value="")
        self.pg_move_type = tk.IntVar(value=0)
        self.pg_move_speed = tk.IntVar(value=3)
        self.pg_move_freq = tk.IntVar(value=3)
        self.pg_move_type_label = tk.StringVar(value=_MOVE_TYPE_LABELS[0])
        self.pg_move_speed_label = tk.StringVar(value=_MOVE_SPEED_LABELS[2])
        self.pg_move_freq_label = tk.StringVar(value=_MOVE_FREQ_LABELS[2])
        self.pg_sw1_label = tk.StringVar(value="0: (없음)")
        self.pg_sw2_label = tk.StringVar(value="0: (없음)")
        self.pg_var_label = tk.StringVar(value="0: (없음)")
        self.pg_through = tk.BooleanVar(value=False)
        self.pg_walk_anime = tk.BooleanVar(value=True)
        self.pg_step_anime = tk.BooleanVar(value=False)
        self.pg_dir_fix = tk.BooleanVar(value=False)
        self.pg_top = tk.BooleanVar(value=False)

        pg_f = ttk.Frame(pg_box)
        pg_f.pack(fill="x", padx=4, pady=3)
        ttk.Label(pg_f, text="트리거").grid(row=0, column=0, sticky="w")
        ttk.Combobox(pg_f, textvariable=self.pg_trig_var, values=list(TRIGGER_NAME_TO_INT.keys()), state="readonly", width=13).grid(row=0, column=1, sticky="ew", padx=2)
        ttk.Label(pg_f, text="그래픽").grid(row=0, column=2, sticky="w")
        self.pg_graphic_cb = ttk.Combobox(pg_f, textvariable=self.pg_graphic_var, width=12)
        self.pg_graphic_cb.grid(row=0, column=3, sticky="ew", padx=2)

        ttk.Label(pg_f, text="스위치1/2").grid(row=1, column=0, sticky="w")
        sw_f = ttk.Frame(pg_f)
        sw_f.grid(row=1, column=1, sticky="ew")
        self.pg_sw1_cb = ttk.Combobox(sw_f, textvariable=self.pg_sw1_label, width=9)
        self.pg_sw1_cb.pack(side="left", fill="x", expand=True)
        self.pg_sw2_cb = ttk.Combobox(sw_f, textvariable=self.pg_sw2_label, width=9)
        self.pg_sw2_cb.pack(side="left", fill="x", expand=True, padx=2)
        ttk.Label(pg_f, text="변수>=").grid(row=1, column=2, sticky="w")
        vr_f = ttk.Frame(pg_f)
        vr_f.grid(row=1, column=3, sticky="ew")
        self.pg_var_cb = ttk.Combobox(vr_f, textvariable=self.pg_var_label, width=10)
        self.pg_var_cb.pack(side="left", fill="x", expand=True)
        ttk.Spinbox(vr_f, from_=-9999, to=9999, textvariable=self.pg_var_val, width=4).pack(side="left", padx=2)

        ttk.Label(pg_f, text="셀프SW").grid(row=2, column=0, sticky="w")
        ttk.Combobox(pg_f, textvariable=self.pg_self_sw, values=["", "A", "B", "C", "D"], state="readonly", width=5).grid(row=2, column=1, sticky="w", padx=2)
        ttk.Label(pg_f, text="이동/속/빈").grid(row=2, column=2, sticky="w")
        mv_f = ttk.Frame(pg_f)
        mv_f.grid(row=2, column=3, sticky="ew")
        ttk.Combobox(mv_f, textvariable=self.pg_move_type_label, values=list(_MOVE_TYPE_LABELS), state="readonly", width=8).pack(side="left")
        ttk.Combobox(mv_f, textvariable=self.pg_move_speed_label, values=list(_MOVE_SPEED_LABELS), state="readonly", width=8).pack(side="left", padx=1)
        ttk.Combobox(mv_f, textvariable=self.pg_move_freq_label, values=list(_MOVE_FREQ_LABELS), state="readonly", width=7).pack(side="left")
        pg_f.columnconfigure(1, weight=1)
        pg_f.columnconfigure(3, weight=1)

        pg_c = ttk.Frame(pg_box)
        pg_c.pack(fill="x", padx=4, pady=2)
        ttk.Checkbutton(pg_c, text="관통", variable=self.pg_through).pack(side="left")
        ttk.Checkbutton(pg_c, text="보행", variable=self.pg_walk_anime).pack(side="left")
        ttk.Checkbutton(pg_c, text="제자리", variable=self.pg_step_anime).pack(side="left")
        ttk.Checkbutton(pg_c, text="방향고정", variable=self.pg_dir_fix).pack(side="left")
        ttk.Checkbutton(pg_c, text="항상위", variable=self.pg_top).pack(side="left")

        easy_bar = ttk.Frame(col_ev)
        easy_bar.pack(fill="x", pady=(2, 1))
        ttk.Button(easy_bar, text="💬 대사 추가", command=self._quick_add_dialogue).pack(side="left", padx=1)
        ttk.Button(easy_bar, text="🔀 선택지", command=self._quick_add_choice).pack(side="left", padx=1)
        ttk.Button(easy_bar, text="🎚 스위치", command=self._quick_add_switch).pack(side="left", padx=1)
        ttk.Button(easy_bar, text="🚪 장소 이동", command=self._quick_add_transfer).pack(side="left", padx=1)

        easy_bar2 = ttk.Frame(col_ev)
        easy_bar2.pack(fill="x", pady=(0, 2))
        ttk.Button(easy_bar2, text="🛒 상점", command=self._quick_add_shop).pack(side="left", padx=1)
        ttk.Button(easy_bar2, text="⚔ 전투", command=self._quick_add_battle).pack(side="left", padx=1)
        ttk.Button(easy_bar2, text="❓ 조건분기", command=self._quick_add_branch).pack(side="left", padx=1)

        dsl_bar = ttk.Frame(col_ev)
        dsl_bar.pack(fill="x", pady=2)
        self.ev_dsl_preset = tk.StringVar(value="대사 (say)")
        ttk.Combobox(dsl_bar, textvariable=self.ev_dsl_preset, values=list(DSL_TEMPLATES.keys()), state="readonly", width=20).pack(side="left", fill="x", expand=True)
        ttk.Button(dsl_bar, text="+ 명령 삽입", command=self._insert_ev_dsl).pack(side="left", padx=3)

        ttk.Label(col_ev, text="이벤트 명령 JSON (고급 / 직접 편집)", font=("", 8)).pack(anchor="w")
        self.txt_ev_cmds = scrolledtext.ScrolledText(col_ev, height=10, font=("Consolas", 9), undo=True, maxundo=50)
        self.txt_ev_cmds.pack(fill="both", expand=True)

    def reload_maps(self, select_map_id: Optional[int] = None, select_event_id: Optional[int] = None) -> None:
        if not self.app.ir_dir:
            return
        maps = list_maps(self.app.ir_dir)
        self.map_list.delete(0, "end")
        self._map_ids: List[int] = []
        for m in maps:
            mid = int(m["map_id"])
            self._map_ids.append(mid)
            self.map_list.insert("end", f"{mid:03d}: {m['name']} ({m['width']}x{m['height']})")
        if not self._map_ids:
            return
        target = select_map_id if select_map_id in self._map_ids else (
            self.current_map_id if self.current_map_id in self._map_ids else self._map_ids[0]
        )
        pos = self._map_ids.index(target)
        self.map_list.selection_set(pos)
        self._load_map(target, select_event_id=select_event_id)

    def _on_select_map(self) -> None:
        sel = self.map_list.curselection()
        if not sel or sel[0] >= len(self._map_ids):
            return
        target_mid = self._map_ids[sel[0]]
        if target_mid == self.current_map_id:
            return
        if not self._confirm_discard_dirty_event():
            if self.current_map_id in self._map_ids:
                self.map_list.selection_clear(0, "end")
                self.map_list.selection_set(self._map_ids.index(self.current_map_id))
            return
        self._load_map(target_mid)

    def _refresh_event_lookup_combos(self) -> None:
        if not self.app.ir_dir:
            return
        try:
            proj = load_json(self.app.ir_dir / "ir_project.json")
            sf = proj.get("system", {}).get("fields", {})
        except Exception:
            sf = {}
        sw_names = sf.get("switches", []) if isinstance(sf.get("switches"), list) else []
        vr_names = sf.get("variables", []) if isinstance(sf.get("variables"), list) else []
        sw_opts = ["0: (없음)"] + [
            f"{i}: {sw_names[i]}" if i < len(sw_names) and sw_names[i] else f"{i}: SW#{i}"
            for i in range(1, max(21, len(sw_names)))
        ]
        vr_opts = ["0: (없음)"] + [
            f"{i}: {vr_names[i]}" if i < len(vr_names) and vr_names[i] else f"{i}: VAR#{i}"
            for i in range(1, max(21, len(vr_names)))
        ]
        self.pg_sw1_cb.configure(values=sw_opts)
        self.pg_sw2_cb.configure(values=sw_opts)
        self.pg_var_cb.configure(values=vr_opts)

        char_names = [""]
        gdir = self.app.atlas.graphics_dir if self.app.atlas else None
        if gdir is not None:
            cdir = Path(gdir) / "Characters"
            if cdir.is_dir():
                for p in sorted(cdir.glob("*.png")):
                    char_names.append(p.stem)
        self.pg_graphic_cb.configure(values=char_names)

    def _load_map(self, map_id: int, select_event_id: Optional[int] = None) -> None:
        mpath = self.app.ir_dir / "maps" / f"map_{int(map_id):03d}.json"
        if not mpath.is_file():
            return
        self.current_map_id = int(map_id)
        self.current_map_data = load_json(mpath)
        md = self.current_map_data

        self.mp_name.set(str(md.get("name", "")))
        self.mp_w.set(int(md.get("width", 20)))
        self.mp_h.set(int(md.get("height", 15)))
        self.mp_ts.set(int(md.get("tileset_id", 1)))
        bgm_f = md.get("bgm", {}).get("fields", {}) if isinstance(md.get("bgm"), dict) else {}
        bgs_f = md.get("bgs", {}).get("fields", {}) if isinstance(md.get("bgs"), dict) else {}
        self.mp_bgm.set(str(bgm_f.get("name", "")))
        self.mp_bgs.set(str(bgs_f.get("name", "")))
        self.mp_auto_bgm.set(bool(md.get("autoplay_bgm", False)))
        self.mp_auto_bgs.set(bool(md.get("autoplay_bgs", False)))
        self.mp_parallax.set(str(md.get("parallax_name", "")))
        self.mp_enc_step.set(int(md.get("encounter_step", 30)))
        self.mp_enc_list.set(",".join(str(x) for x in md.get("encounter_list", [])))

        self._refresh_event_lookup_combos()
        self._rebuild_palette()
        self.redraw_map()
        self._reload_events_list(select_event_id=select_event_id)

    def _rebuild_palette(self) -> None:
        if not self.app.atlas:
            return
        self.palette_canvas.delete("all")
        self._palette_img_refs.clear()
        self._palette_rects.clear()
        ts_id = int(self.current_map_data.get("tileset_id", 1))
        tile_ids = self.app.atlas.get_palette_tiles(ts_id)
        cols = 5
        cell = 34
        pal_tk = self.app.atlas.render_palette_tk(tile_ids, ts_id, cols=cols, cell=cell)
        self._palette_img_refs.append(pal_tk)
        self.palette_canvas.create_image(0, 0, anchor="nw", image=pal_tk)
        for idx, tid in enumerate(tile_ids):
            c = idx % cols
            r = idx // cols
            self._palette_rects[tid] = (c * cell + 4, r * cell + 4)
        sel_xy = self._palette_rects.get(self.active_tile_id, (4, 4))
        self.palette_canvas.create_rectangle(
            sel_xy[0], sel_xy[1], sel_xy[0] + 32, sel_xy[1] + 32,
            outline="#ffd54f", width=2, tags=("pal_sel",),
        )
        rows = (len(tile_ids) + cols - 1) // cols
        self.palette_canvas.configure(scrollregion=(0, 0, cols * cell + 8, rows * cell + 8))

    def _on_palette_click(self, event: tk.Event) -> None:
        cx = self.palette_canvas.canvasx(event.x)
        cy = self.palette_canvas.canvasy(event.y)
        for tid, (x0, y0) in self._palette_rects.items():
            if x0 <= cx <= x0 + 32 and y0 <= cy <= y0 + 32:
                self._select_tile_id(tid)
                return

    def _apply_custom_tile_id(self) -> None:
        self._select_tile_id(int(self.custom_tile_var.get()))

    def _select_tile_id(self, tid: int) -> None:
        self.active_tile_id = int(tid)
        self.custom_tile_var.set(self.active_tile_id)
        self.lbl_active_tile.config(text=f"선택 타일 ID: {self.active_tile_id}")
        sel_xy = self._palette_rects.get(self.active_tile_id)
        if sel_xy is not None and self.palette_canvas.find_withtag("pal_sel"):
            self.palette_canvas.coords("pal_sel", sel_xy[0], sel_xy[1], sel_xy[0] + 32, sel_xy[1] + 32)
        else:
            self._rebuild_palette()

    def redraw_map(self) -> None:
        if not self.current_map_data or not self.app.atlas:
            return
        md = self.current_map_data
        w = int(md.get("width", 20))
        h = int(md.get("height", 15))
        px = int(self.tile_px.get())

        self.map_canvas.delete("all")
        self._canvas_img_refs.clear()
        self.map_canvas.configure(scrollregion=(0, 0, w * px, h * px))

        comp_tk = self.app.atlas.render_map_tk(md, tile_px=px, show_grid=bool(self.show_grid.get()))
        self._canvas_img_refs.append(comp_tk)
        self.map_canvas.create_image(0, 0, anchor="nw", image=comp_tk)

        # Player start marker
        proj_path = self.app.ir_dir / "ir_project.json"
        if proj_path.is_file():
            sys_f = load_json(proj_path).get("system", {}).get("fields", {})
            if int(sys_f.get("start_map_id", 1)) == self.current_map_id:
                sx, sy = int(sys_f.get("start_x", 0)), int(sys_f.get("start_y", 0))
                self.map_canvas.create_rectangle(
                    sx * px + 2, sy * px + 2, (sx + 1) * px - 2, (sy + 1) * px - 2,
                    outline="#4fc3f7", width=2,
                )
                self.map_canvas.create_text(
                    sx * px + px // 2, sy * px + px // 2,
                    text="START", fill="#81d4fa", font=("", max(6, px // 4), "bold"),
                )

        # Event markers
        for ev in md.get("events", []):
            ex, ey = int(ev.get("x", 0)), int(ev.get("y", 0))
            eid = int(ev.get("id", 0))
            is_sel = eid == self.selected_event_id
            outline = "#ffd54f" if is_sel else "#ff7043"
            self.map_canvas.create_rectangle(
                ex * px + 2, ey * px + 2, (ex + 1) * px - 2, (ey + 1) * px - 2,
                outline=outline, width=3 if is_sel else 2,
            )
            self.map_canvas.create_text(
                ex * px + px // 2, ey * px + px // 2,
                text=f"E{eid}", fill="#ffffff", font=("", max(7, px // 3), "bold"),
            )

    def _canvas_tile_xy(self, event: tk.Event) -> Optional[Tuple[int, int]]:
        if not self.current_map_data:
            return None
        px = int(self.tile_px.get())
        cx = int(self.map_canvas.canvasx(event.x))
        cy = int(self.map_canvas.canvasy(event.y))
        tx, ty = cx // px, cy // px
        w, h = int(self.current_map_data.get("width", 0)), int(self.current_map_data.get("height", 0))
        if 0 <= tx < w and 0 <= ty < h:
            return tx, ty
        return None

    def _on_canvas_hover(self, event: tk.Event) -> None:
        pos = self._canvas_tile_xy(event)
        if not pos:
            return
        tx, ty = pos
        layer = int(self.active_layer.get())
        grid = self.current_map_data.get("layers", {}).get(f"layer_{layer}", [])
        tid = grid[ty][tx] if ty < len(grid) and tx < len(grid[ty]) else 0
        self.lbl_hover.config(text=f"좌표: ({tx}, {ty}) | L{layer} 타일={tid} | 우클릭: 스포이드")

    def _on_canvas_right_click(self, event: tk.Event) -> None:
        pos = self._canvas_tile_xy(event)
        if not pos:
            return
        tx, ty = pos
        layer = int(self.active_layer.get())
        grid = self.current_map_data.get("layers", {}).get(f"layer_{layer}", [])
        if ty < len(grid) and tx < len(grid[ty]):
            self._select_tile_id(int(grid[ty][tx]))

    def _on_canvas_press(self, event: tk.Event) -> None:
        pos = self._canvas_tile_xy(event)
        if not pos:
            return
        tx, ty = pos
        tool = self.active_tool.get()
        if tool == "eyedropper":
            self._on_canvas_right_click(event)
            return
        if tool == "start":
            set_db_entry(
                self.app.ir_dir,
                "system",
                1,
                {"start_map_id": self.current_map_id, "start_x": tx, "start_y": ty},
                record_undo=True,
            )
            self.app.log_console(f"[시작 위치 설정] Map {self.current_map_id} ({tx}, {ty})")
            self.app.reload_all(keep_map_id=self.current_map_id)
            return
        if tool == "event":
            found = next((ev for ev in self.current_map_data.get("events", []) if int(ev.get("x", -1)) == tx and int(ev.get("y", -1)) == ty), None)
            if found is not None:
                self._load_event(int(found["id"]))
                self.redraw_map()
            else:
                res = add_event(self.app.ir_dir, self.current_map_id, f"Event_{tx}_{ty}", tx, ty, commands=[{"op": "say", "lines": ["Hello!"]}])
                self.app.log_console(f"[이벤트 배치] {res}")
                self.app.reload_all(keep_map_id=self.current_map_id, keep_event_id=int(res["event_id"]))
            return
        if tool == "rect":
            self._rect_start = (tx, ty)
            return
        # brush or eraser
        self._stroke_points = [(tx, ty)]
        self._preview_local_tile(tx, ty, 0 if tool == "eraser" else self.active_tile_id)

    def _on_canvas_drag(self, event: tk.Event) -> None:
        pos = self._canvas_tile_xy(event)
        if not pos:
            return
        tx, ty = pos
        tool = self.active_tool.get()
        if tool == "rect" and self._rect_start is not None:
            px = int(self.tile_px.get())
            x0, y0 = min(self._rect_start[0], tx), min(self._rect_start[1], ty)
            x1, y1 = max(self._rect_start[0], tx), max(self._rect_start[1], ty)
            if self._rect_preview_id is not None:
                self.map_canvas.delete(self._rect_preview_id)
            self._rect_preview_id = self.map_canvas.create_rectangle(
                x0 * px,
                y0 * px,
                (x1 + 1) * px,
                (y1 + 1) * px,
                fill=PREVIEW_GRAY_DARK_HEX,
                stipple="gray25",
                outline=PREVIEW_GRAY_LIGHT_HEX,
                width=2,
                dash=(4, 4),
            )
            return
        if tool in ("brush", "eraser") and (tx, ty) not in self._stroke_points:
            self._stroke_points.append((tx, ty))
            self._preview_local_tile(tx, ty, 0 if tool == "eraser" else self.active_tile_id)

    def _on_canvas_release(self, event: tk.Event) -> None:
        tool = self.active_tool.get()
        layer = int(self.active_layer.get())
        if tool == "rect" and self._rect_start is not None:
            pos = self._canvas_tile_xy(event) or self._rect_start
            x0, y0 = min(self._rect_start[0], pos[0]), min(self._rect_start[1], pos[1])
            x1, y1 = max(self._rect_start[0], pos[0]), max(self._rect_start[1], pos[1])
            self._rect_start = None
            if self._rect_preview_id is not None:
                self.map_canvas.delete(self._rect_preview_id)
                self._rect_preview_id = None
            paint_map(self.app.ir_dir, self.current_map_id, layer, self.active_tile_id, rect=(x0, y0, x1 - x0 + 1, y1 - y0 + 1))
            self._load_map(self.current_map_id, select_event_id=self.selected_event_id)
            return

        if tool in ("brush", "eraser") and self._stroke_points:
            pts = list(self._stroke_points)
            self._stroke_points.clear()
            tid = 0 if tool == "eraser" else self.active_tile_id
            paint_map(self.app.ir_dir, self.current_map_id, layer, tid, points=pts)
            self._load_map(self.current_map_id, select_event_id=self.selected_event_id)

    def _preview_local_tile(self, tx: int, ty: int, tid: int) -> None:
        if not self.app.atlas or not self.current_map_data:
            return
        px = int(self.tile_px.get())
        ts_id = int(self.current_map_data.get("tileset_id", 1))
        if tid > 0:
            tk_im = self.app.atlas.get_tk_tile(ts_id, tid, px)
            if tk_im is not None:
                self._canvas_img_refs.append(tk_im)
                self.map_canvas.create_image(tx * px, ty * px, anchor="nw", image=tk_im)
        else:
            self.map_canvas.create_rectangle(tx * px, ty * px, (tx + 1) * px, (ty + 1) * px, fill="#151820", outline="#303642")

    def create_new_map(self) -> None:
        if not self.app.ir_dir:
            return
        name = simpledialog.askstring("새 맵 생성", "맵 이름:", initialvalue="New Area", parent=self)
        if not name:
            return
        w = simpledialog.askinteger("너비", "맵 너비(타일 수):", initialvalue=20, minvalue=5, maxvalue=200, parent=self) or 20
        h = simpledialog.askinteger("높이", "맵 높이(타일 수):", initialvalue=15, minvalue=5, maxvalue=200, parent=self) or 15
        res = create_map(self.app.ir_dir, name=name, width=w, height=h)
        self.app.log_console(f"[맵 생성] {res}")
        self.app.reload_all(keep_map_id=int(res["map_id"]))

    def delete_current_map(self) -> None:
        if not self.app.ir_dir:
            return
        try:
            res = delete_map(self.app.ir_dir, self.current_map_id)
            self.app.log_console(f"[맵 삭제] {res}")
            self.app.reload_all()
        except Exception as exc:
            messagebox.showerror("맵 삭제 불가", str(exc), parent=self)

    def save_map_props(self) -> None:
        if not self.app.ir_dir:
            return
        try:
            enc = [int(x.strip()) for x in self.mp_enc_list.get().split(",") if x.strip()]
            bgm_cur = dict(self.current_map_data.get("bgm", {"__type__": "RubyObject", "class": "RPG::AudioFile", "fields": {"name": "", "volume": 100, "pitch": 100}}))
            bgs_cur = dict(self.current_map_data.get("bgs", {"__type__": "RubyObject", "class": "RPG::AudioFile", "fields": {"name": "", "volume": 80, "pitch": 100}}))
            bgm_cur.setdefault("fields", {})["name"] = self.mp_bgm.get().strip()
            bgs_cur.setdefault("fields", {})["name"] = self.mp_bgs.get().strip()

            res = update_map_properties(
                self.app.ir_dir,
                self.current_map_id,
                {
                    "name": self.mp_name.get(),
                    "width": int(self.mp_w.get()),
                    "height": int(self.mp_h.get()),
                    "tileset_id": int(self.mp_ts.get()),
                    "autoplay_bgm": bool(self.mp_auto_bgm.get()),
                    "bgm": bgm_cur,
                    "autoplay_bgs": bool(self.mp_auto_bgs.get()),
                    "bgs": bgs_cur,
                    "parallax_name": self.mp_parallax.get().strip(),
                    "encounter_step": int(self.mp_enc_step.get()),
                    "encounter_list": enc,
                },
            )
            self.app.log_console(f"[맵 속성 저장] {res}")
            self.app.reload_all(keep_map_id=self.current_map_id)
        except Exception as exc:
            messagebox.showerror("맵 속성 저장 오류", str(exc), parent=self)

    def _reload_events_list(self, select_event_id: Optional[int] = None) -> None:
        self.ev_list.delete(0, "end")
        evs = list(self.current_map_data.get("events", []))
        self._event_ids = [int(e["id"]) for e in evs]
        for ev in evs:
            self.ev_list.insert("end", f"E{ev['id']:03d}: {ev.get('name', '')} ({ev.get('x')},{ev.get('y')})")
        if not self._event_ids:
            self.selected_event_id = None
            self._new_event_draft()
            return
        target = select_event_id if select_event_id in self._event_ids else (
            self.selected_event_id if self.selected_event_id in self._event_ids else self._event_ids[0]
        )
        pos = self._event_ids.index(target)
        self.ev_list.selection_set(pos)
        self._load_event(target)

    @staticmethod
    def _parse_leading_int(text: Any, fallback: int = 0) -> int:
        raw = str(text or "").strip()
        if not raw:
            return fallback
        head = raw.split(":", 1)[0].strip()
        try:
            return int(head)
        except ValueError:
            return fallback

    def _current_event_fingerprint(self) -> str:
        payload = {
            "id": str(self.ev_id_var.get()),
            "name": self.ev_name_var.get(),
            "x": str(self.ev_x_var.get()),
            "y": str(self.ev_y_var.get()),
            "page": self.selected_page_idx,
            "trigger": self.pg_trig_var.get(),
            "graphic": self.pg_graphic_var.get(),
            "sw1": self.pg_sw1_label.get(),
            "sw2": self.pg_sw2_label.get(),
            "var": self.pg_var_label.get(),
            "var_val": str(self.pg_var_val.get()),
            "self_sw": self.pg_self_sw.get(),
            "move_type": self.pg_move_type_label.get(),
            "move_speed": self.pg_move_speed_label.get(),
            "move_freq": self.pg_move_freq_label.get(),
            "through": bool(self.pg_through.get()),
            "walk": bool(self.pg_walk_anime.get()),
            "step": bool(self.pg_step_anime.get()),
            "dir_fix": bool(self.pg_dir_fix.get()),
            "top": bool(self.pg_top.get()),
            "cmds": self.txt_ev_cmds.get("1.0", "end").strip(),
        }
        return json.dumps(payload, ensure_ascii=False, sort_keys=True)

    def _is_event_dirty(self) -> bool:
        if self._other_pages_dirty:
            return True
        return bool(self._event_clean_fp) and self._current_event_fingerprint() != self._event_clean_fp

    def _confirm_discard_dirty_event(self) -> bool:
        if not self._is_event_dirty():
            return True
        ans = messagebox.askyesnocancel(
            "저장되지 않은 이벤트 변경",
            "현재 이벤트에 저장되지 않은 수정사항이 있습니다.\n저장하시겠습니까? (예: 저장 후 이동 / 아니오: 변경 버림 / 취소: 머무르기)",
            parent=self,
        )
        if ans is None:
            return False
        if ans is True:
            return bool(self.save_current_event())
        return True

    def _on_new_event_clicked(self) -> None:
        if not self._confirm_discard_dirty_event():
            return
        self._new_event_draft()

    def _on_select_event(self) -> None:
        sel = self.ev_list.curselection()
        if not sel or sel[0] >= len(self._event_ids):
            return
        target_ev_id = self._event_ids[sel[0]]
        if target_ev_id == self.selected_event_id:
            return
        if not self._confirm_discard_dirty_event():
            if self.selected_event_id in self._event_ids:
                self.ev_list.selection_clear(0, "end")
                self.ev_list.selection_set(self._event_ids.index(self.selected_event_id))
            return
        self._load_event(target_ev_id)
        self.redraw_map()

    def _load_event(self, event_id: int) -> None:
        found = next((ev for ev in self.current_map_data.get("events", []) if int(ev.get("id", -1)) == int(event_id)), None)
        if not found:
            return
        self.selected_event_id = int(event_id)
        self._other_pages_dirty = False
        self._working_event = json.loads(json.dumps(found))
        self.ev_id_var.set(int(found.get("id", 1)))
        self.ev_name_var.set(str(found.get("name", "")))
        self.ev_x_var.set(int(found.get("x", 0)))
        self.ev_y_var.set(int(found.get("y", 0)))
        self._refresh_page_selector(0)

    def _new_event_draft(self) -> None:
        evs = self.current_map_data.get("events", [])
        next_id = (max(int(e["id"]) for e in evs) + 1) if evs else 1
        self.selected_event_id = None
        self._other_pages_dirty = False
        self._working_event = {
            "id": next_id,
            "name": f"EV{next_id:03d}",
            "x": 0,
            "y": 0,
            "pages": [_build_ir_page({"commands": [{"op": "say", "lines": ["New Event"]}]})],
        }
        self.ev_id_var.set(next_id)
        self.ev_name_var.set(self._working_event["name"])
        self.ev_x_var.set(0)
        self.ev_y_var.set(0)
        self._refresh_page_selector(0)

    def _refresh_page_selector(self, page_idx: int = 0) -> None:
        pages = self._working_event.get("pages", [])
        if not pages:
            pages = [_build_ir_page({})]
            self._working_event["pages"] = pages
        self.selected_page_idx = max(0, min(page_idx, len(pages) - 1))
        labels = [f"Page {i + 1}" for i in range(len(pages))]
        self.page_cb.configure(values=labels)
        self.page_var.set(labels[self.selected_page_idx])
        self._load_page_fields(pages[self.selected_page_idx])

    def _load_page_fields(self, page: Dict[str, Any]) -> None:
        self.pg_trig_var.set(str(page.get("trigger", "action_button")))
        gfx_f = page.get("graphic", {}).get("fields", {}) if isinstance(page.get("graphic"), dict) else {}
        self.pg_graphic_var.set(str(gfx_f.get("character_name", "")))
        cond_f = page.get("condition", {}).get("fields", {}) if isinstance(page.get("condition"), dict) else {}
        self.pg_sw1_var.set(int(cond_f.get("switch1_id", 1)) if cond_f.get("switch1_valid") else 0)
        self.pg_sw2_var.set(int(cond_f.get("switch2_id", 1)) if cond_f.get("switch2_valid") else 0)
        self.pg_var_id.set(int(cond_f.get("variable_id", 1)) if cond_f.get("variable_valid") else 0)
        self.pg_var_val.set(int(cond_f.get("variable_value", 0)))
        self.pg_self_sw.set(str(cond_f.get("self_switch_ch", "A")) if cond_f.get("self_switch_valid") else "")

        def _match_opt(combo: ttk.Combobox, num_id: int) -> str:
            for item in combo.cget("values") or ():
                if str(item).startswith(f"{num_id}:"):
                    return str(item)
            return f"{num_id}: (없음)" if num_id == 0 else f"{num_id}: #{num_id}"

        self.pg_sw1_label.set(_match_opt(self.pg_sw1_cb, int(self.pg_sw1_var.get())))
        self.pg_sw2_label.set(_match_opt(self.pg_sw2_cb, int(self.pg_sw2_var.get())))
        self.pg_var_label.set(_match_opt(self.pg_var_cb, int(self.pg_var_id.get())))

        mt = max(0, min(3, int(page.get("move_type", 0))))
        ms = max(1, min(6, int(page.get("move_speed", 3))))
        mf = max(1, min(6, int(page.get("move_frequency", 3))))
        self.pg_move_type.set(mt)
        self.pg_move_speed.set(ms)
        self.pg_move_freq.set(mf)
        self.pg_move_type_label.set(_MOVE_TYPE_LABELS[mt])
        self.pg_move_speed_label.set(_MOVE_SPEED_LABELS[ms - 1])
        self.pg_move_freq_label.set(_MOVE_FREQ_LABELS[mf - 1])

        self.pg_through.set(bool(page.get("through", False)))
        self.pg_walk_anime.set(bool(page.get("walk_anime", True)))
        self.pg_step_anime.set(bool(page.get("step_anime", False)))
        self.pg_dir_fix.set(bool(page.get("direction_fix", False)))
        self.pg_top.set(bool(page.get("always_on_top", False)))

        self.txt_ev_cmds.delete("1.0", "end")
        self.txt_ev_cmds.insert("1.0", json.dumps(page.get("commands", []), ensure_ascii=False, indent=2))
        self.txt_ev_cmds.edit_reset()
        self._event_clean_fp = self._current_event_fingerprint()

    def _flush_page_fields_into_working(self) -> None:
        pages = self._working_event.setdefault("pages", [])
        if not pages:
            return
        idx = self.selected_page_idx
        cmds = json.loads(self.txt_ev_cmds.get("1.0", "end").strip() or "[]")
        if not isinstance(cmds, list):
            raise ValueError("이벤트 명령은 JSON 배열이어야 합니다.")
        if not cmds or cmds[-1].get("op") != "end":
            cmds.append({"op": "end", "indent": 0})
        sw1 = self._parse_leading_int(self.pg_sw1_label.get(), int(self.pg_sw1_var.get()))
        sw2 = self._parse_leading_int(self.pg_sw2_label.get(), int(self.pg_sw2_var.get()))
        vid = self._parse_leading_int(self.pg_var_label.get(), int(self.pg_var_id.get()))
        self.pg_sw1_var.set(sw1)
        self.pg_sw2_var.set(sw2)
        self.pg_var_id.set(vid)
        mt = max(0, min(3, self._parse_leading_int(self.pg_move_type_label.get(), int(self.pg_move_type.get()))))
        ms = max(1, min(6, self._parse_leading_int(self.pg_move_speed_label.get(), int(self.pg_move_speed.get()))))
        mf = max(1, min(6, self._parse_leading_int(self.pg_move_freq_label.get(), int(self.pg_move_freq.get()))))
        self.pg_move_type.set(mt)
        self.pg_move_speed.set(ms)
        self.pg_move_freq.set(mf)
        ssw = self.pg_self_sw.get().strip()
        rebuilt = _build_ir_page({
            "trigger": self.pg_trig_var.get(),
            "graphic": self.pg_graphic_var.get().strip(),
            "through": bool(self.pg_through.get()),
            "always_on_top": bool(self.pg_top.get()),
            "walk_anime": bool(self.pg_walk_anime.get()),
            "step_anime": bool(self.pg_step_anime.get()),
            "direction_fix": bool(self.pg_dir_fix.get()),
            "move_type": mt,
            "move_speed": ms,
            "move_frequency": mf,
            "condition": {
                "switch1_id": sw1 if sw1 > 0 else None,
                "switch2_id": sw2 if sw2 > 0 else None,
                "variable_id": vid if vid > 0 else None,
                "variable_value": int(self.pg_var_val.get()),
                "self_switch_ch": ssw if ssw else None,
            },
            "commands": cmds,
        })
        # Preserve extra move_route / graphic fields if previously set
        old = pages[idx]
        if isinstance(old.get("move_route"), dict):
            rebuilt["move_route"] = old["move_route"]
        pages[idx] = rebuilt

    def _on_switch_page(self) -> None:
        if self._event_clean_fp and self._current_event_fingerprint() != self._event_clean_fp:
            self._other_pages_dirty = True
        try:
            self._flush_page_fields_into_working()
        except Exception:
            pass
        val = self.page_var.get()
        idx = int(val.split()[-1]) - 1 if val.startswith("Page ") else 0
        self._refresh_page_selector(idx)

    def _add_event_page(self) -> None:
        self._other_pages_dirty = True
        try:
            self._flush_page_fields_into_working()
        except Exception:
            pass
        self._working_event.setdefault("pages", []).append(_build_ir_page({}))
        self._refresh_page_selector(len(self._working_event["pages"]) - 1)

    def _remove_event_page(self) -> None:
        pages = self._working_event.get("pages", [])
        if len(pages) <= 1:
            return
        self._other_pages_dirty = True
        pages.pop(self.selected_page_idx)
        self._refresh_page_selector(max(0, self.selected_page_idx - 1))

    def _append_ev_cmds(self, new_cmds: List[Dict[str, Any]]) -> None:
        try:
            cur = json.loads(self.txt_ev_cmds.get("1.0", "end").strip() or "[]")
            if not isinstance(cur, list):
                cur = []
        except json.JSONDecodeError:
            cur = []
        cur = [c for c in cur if c.get("op") != "end"]
        cur.extend(new_cmds)
        cur.append({"op": "end", "indent": 0})
        self.txt_ev_cmds.delete("1.0", "end")
        self.txt_ev_cmds.insert("1.0", json.dumps(cur, ensure_ascii=False, indent=2))

    def _quick_add_dialogue(self) -> None:
        msg = simpledialog.askstring("간편 대사 추가", "NPC가 말할 대사를 입력하세요:", parent=self)
        if msg:
            self._append_ev_cmds([{"op": "say", "lines": msg.splitlines(), "indent": 0}])

    def _quick_add_choice(self) -> None:
        c1 = simpledialog.askstring("선택지 1", "첫 번째 선택지 문구:", initialvalue="네", parent=self)
        if not c1:
            return
        c2 = simpledialog.askstring("선택지 2", "두 번째 선택지 문구:", initialvalue="아니오", parent=self)
        if not c2:
            return
        self._append_ev_cmds([
            {"op": "show_choices", "choices": [c1, c2], "cancel_type": 2, "indent": 0},
            {"op": "when_choice", "index": 0, "label": c1, "indent": 0},
            {"op": "say", "lines": [f"{c1} 선택"], "indent": 1},
            {"op": "when_choice", "index": 1, "label": c2, "indent": 0},
            {"op": "say", "lines": [f"{c2} 선택"], "indent": 1},
            {"op": "end_choices", "indent": 0},
        ])

    def _quick_add_switch(self) -> None:
        sid = simpledialog.askinteger("스위치 조작", "스위치 ID (1 이상):", initialvalue=1, minvalue=1, maxvalue=999, parent=self)
        if sid is None:
            return
        turn_on = messagebox.askyesno("스위치 상태", f"스위치 #{sid} 을(를) ON으로 설정할까요?\n(아니오 = OFF)", parent=self)
        self._append_ev_cmds([
            {"op": "set_switch", "start_id": int(sid), "end_id": int(sid), "value": "ON" if turn_on else "OFF", "indent": 0},
        ])

    @staticmethod
    def build_transfer_cmds(map_id: int, x: int, y: int, direction: str = "down", fade_type: int = 0) -> List[Dict[str, Any]]:
        return [{
            "op": "transfer_player",
            "var_mode": 0,
            "map_id": int(map_id),
            "x": int(x),
            "y": int(y),
            "direction": str(direction or "down"),
            "fade_type": int(fade_type),
            "indent": 0,
        }]

    @staticmethod
    def build_shop_cmds(goods: List[Dict[str, Any]], purchase_only: bool = False) -> List[Dict[str, Any]]:
        cleaned = [{"type": str(g.get("type", "item")), "id": int(g.get("id", 1))} for g in goods]
        return [{"op": "shop", "goods": cleaned, "purchase_only": bool(purchase_only), "indent": 0}]

    @staticmethod
    def build_battle_cmds(
        troop_id: int,
        can_escape: bool = True,
        can_lose: bool = False,
        with_branches: bool = False,
    ) -> List[Dict[str, Any]]:
        cmds: List[Dict[str, Any]] = [{
            "op": "battle",
            "troop_id": int(troop_id),
            "can_escape": bool(can_escape),
            "can_lose": bool(can_lose),
            "indent": 0,
        }]
        if with_branches:
            cmds.extend([
                {"op": "when_win", "indent": 0},
                {"op": "say", "lines": ["전투에서 승리했다!"], "indent": 1},
            ])
            if can_escape:
                cmds.extend([
                    {"op": "when_escape", "indent": 0},
                    {"op": "say", "lines": ["무사히 도망쳤다."], "indent": 1},
                ])
            if can_lose:
                cmds.extend([
                    {"op": "when_lose", "indent": 0},
                    {"op": "say", "lines": ["쓰러지고 말았다..."], "indent": 1},
                ])
            cmds.append({"op": "end_battle", "indent": 0})
        return cmds

    @staticmethod
    def build_branch_cmds(
        kind: str,
        target_id: int = 1,
        value: int = 0,
        expected: str = "ON",
        include_else: bool = True,
    ) -> List[Dict[str, Any]]:
        if kind == "switch":
            head: Dict[str, Any] = {"op": "if_switch", "switch_id": int(target_id), "expected": str(expected).upper(), "indent": 0}
        elif kind == "variable":
            head = {"op": "if_variable", "variable_id": int(target_id), "rhs_is_var": False, "rhs_value": int(value), "cmp": 1, "indent": 0}
        elif kind == "gold":
            head = {"op": "if_gold", "amount": int(value), "cmp": 0, "indent": 0}
        else:
            head = {"op": "if_item", "item_id": int(target_id), "indent": 0}
        cmds: List[Dict[str, Any]] = [
            head,
            {"op": "say", "lines": ["조건을 충족했습니다."], "indent": 1},
        ]
        if include_else:
            cmds.extend([
                {"op": "else", "indent": 0},
                {"op": "say", "lines": ["조건이 맞지 않습니다."], "indent": 1},
            ])
        cmds.append({"op": "end_if", "indent": 0})
        return cmds

    def _quick_add_transfer(self) -> None:
        if not self.app.ir_dir:
            return
        maps = list_maps(self.app.ir_dir)
        map_labels = [f"{int(m['map_id'])}: {m['name']}" for m in maps] or [f"{self.current_map_id}: 현재 맵"]
        dlg = tk.Toplevel(self)
        dlg.title("장소 이동 (Transfer Player)")
        dlg.transient(self.winfo_toplevel())
        dlg.grab_set()
        m_var = tk.StringVar(value=map_labels[0])
        x_var = tk.IntVar(value=1)
        y_var = tk.IntVar(value=1)
        dir_var = tk.StringVar(value="down")
        fade_var = tk.StringVar(value="0: 검정 페이드")
        ttk.Label(dlg, text="이동할 맵").grid(row=0, column=0, sticky="w", padx=8, pady=4)
        ttk.Combobox(dlg, textvariable=m_var, values=map_labels, state="readonly", width=24).grid(row=0, column=1, padx=8, pady=4)
        ttk.Label(dlg, text="목표 X").grid(row=1, column=0, sticky="w", padx=8, pady=4)
        ttk.Spinbox(dlg, from_=0, to=200, textvariable=x_var, width=10).grid(row=1, column=1, sticky="w", padx=8, pady=4)
        ttk.Label(dlg, text="목표 Y").grid(row=2, column=0, sticky="w", padx=8, pady=4)
        ttk.Spinbox(dlg, from_=0, to=200, textvariable=y_var, width=10).grid(row=2, column=1, sticky="w", padx=8, pady=4)
        ttk.Label(dlg, text="도착 방향").grid(row=3, column=0, sticky="w", padx=8, pady=4)
        ttk.Combobox(dlg, textvariable=dir_var, values=["down", "up", "left", "right", "retain"], state="readonly", width=12).grid(row=3, column=1, sticky="w", padx=8, pady=4)
        ttk.Label(dlg, text="페이드 효과").grid(row=4, column=0, sticky="w", padx=8, pady=4)
        ttk.Combobox(dlg, textvariable=fade_var, values=["0: 검정 페이드", "1: 흰색 페이드", "2: 없음"], state="readonly", width=16).grid(row=4, column=1, sticky="w", padx=8, pady=4)

        def _ok() -> None:
            mid = int(m_var.get().split(":", 1)[0])
            fade = int(fade_var.get().split(":", 1)[0])
            self._append_ev_cmds(self.build_transfer_cmds(mid, int(x_var.get()), int(y_var.get()), dir_var.get(), fade))
            dlg.destroy()

        btns = ttk.Frame(dlg)
        btns.grid(row=5, column=0, columnspan=2, pady=8)
        ttk.Button(btns, text="추가", command=_ok).pack(side="left", padx=6)
        ttk.Button(btns, text="취소", command=dlg.destroy).pack(side="left", padx=6)

    def _quick_add_shop(self) -> None:
        if not self.app.ir_dir:
            return
        db_choices: Dict[str, List[str]] = {}
        for tbl in ("items", "weapons", "armors"):
            p = self.app.ir_dir / "database" / f"{tbl}.json"
            opts: List[str] = []
            if p.is_file():
                for row in load_json(p):
                    if isinstance(row, dict) and isinstance(row.get("fields"), dict):
                        f = row["fields"]
                        opts.append(f"{int(f.get('id', 0))}: {f.get('name', '')} ({int(f.get('price', 0))}G)")
            db_choices[tbl] = opts or ["1: 기본 항목"]
        dlg = tk.Toplevel(self)
        dlg.title("상점 구성 (Shop Processing)")
        dlg.transient(self.winfo_toplevel())
        dlg.grab_set()
        kind_map = {"아이템": "items", "무기": "weapons", "방어구": "armors"}
        kind_to_g = {"아이템": "item", "무기": "weapon", "방어구": "armor"}
        k_var = tk.StringVar(value="아이템")
        g_var = tk.StringVar(value=db_choices["items"][0])
        ponly_var = tk.BooleanVar(value=False)
        goods_buf: List[Dict[str, Any]] = []

        ttk.Label(dlg, text="상품 종류").grid(row=0, column=0, sticky="w", padx=8, pady=4)
        k_cb = ttk.Combobox(dlg, textvariable=k_var, values=list(kind_map.keys()), state="readonly", width=12)
        k_cb.grid(row=0, column=1, sticky="w", padx=8, pady=4)
        ttk.Label(dlg, text="품목 선택").grid(row=1, column=0, sticky="w", padx=8, pady=4)
        g_cb = ttk.Combobox(dlg, textvariable=g_var, values=db_choices["items"], state="readonly", width=26)
        g_cb.grid(row=1, column=1, padx=8, pady=4)

        def _on_kind_chg(_e: Any = None) -> None:
            lst = db_choices[kind_map[k_var.get()]]
            g_cb.configure(values=lst)
            g_var.set(lst[0])

        k_cb.bind("<<ComboboxSelected>>", _on_kind_chg)
        lb = tk.Listbox(dlg, height=5, width=36)
        lb.grid(row=2, column=0, columnspan=2, padx=8, pady=4)

        def _add_good() -> None:
            gid = int(g_var.get().split(":", 1)[0])
            gt = kind_to_g[k_var.get()]
            goods_buf.append({"type": gt, "id": gid})
            lb.insert("end", f"[{k_var.get()}] {g_var.get()}")

        ttk.Button(dlg, text="+ 진열 품목 추가", command=_add_good).grid(row=3, column=1, sticky="e", padx=8)
        ttk.Checkbutton(dlg, text="구매 전용 (판매 불가)", variable=ponly_var).grid(row=3, column=0, sticky="w", padx=8)

        def _ok() -> None:
            if not goods_buf:
                _add_good()
            self._append_ev_cmds(self.build_shop_cmds(goods_buf, purchase_only=bool(ponly_var.get())))
            dlg.destroy()

        btns = ttk.Frame(dlg)
        btns.grid(row=4, column=0, columnspan=2, pady=8)
        ttk.Button(btns, text="추가", command=_ok).pack(side="left", padx=6)
        ttk.Button(btns, text="취소", command=dlg.destroy).pack(side="left", padx=6)

    def _quick_add_battle(self) -> None:
        if not self.app.ir_dir:
            return
        p = self.app.ir_dir / "database" / "troops.json"
        troops: List[str] = []
        if p.is_file():
            for row in load_json(p):
                if isinstance(row, dict) and isinstance(row.get("fields"), dict):
                    f = row["fields"]
                    troops.append(f"{int(f.get('id', 0))}: {f.get('name', '')}")
        troops = troops or ["1: 기본 적 그룹"]
        dlg = tk.Toplevel(self)
        dlg.title("전투 개시 (Battle Processing)")
        dlg.transient(self.winfo_toplevel())
        dlg.grab_set()
        t_var = tk.StringVar(value=troops[0])
        esc_var = tk.BooleanVar(value=True)
        los_var = tk.BooleanVar(value=False)
        br_var = tk.BooleanVar(value=True)
        ttk.Label(dlg, text="적 그룹 (Troop)").grid(row=0, column=0, sticky="w", padx=8, pady=4)
        ttk.Combobox(dlg, textvariable=t_var, values=troops, state="readonly", width=26).grid(row=0, column=1, padx=8, pady=4)
        ttk.Checkbutton(dlg, text="도망 가능", variable=esc_var).grid(row=1, column=0, sticky="w", padx=8, pady=2)
        ttk.Checkbutton(dlg, text="패배 허용 (게임오버 안 됨)", variable=los_var).grid(row=1, column=1, sticky="w", padx=8, pady=2)
        ttk.Checkbutton(dlg, text="승리/도망/패배 대사 분기 포함", variable=br_var).grid(row=2, column=0, columnspan=2, sticky="w", padx=8, pady=4)

        def _ok() -> None:
            tid = int(t_var.get().split(":", 1)[0])
            self._append_ev_cmds(self.build_battle_cmds(tid, bool(esc_var.get()), bool(los_var.get()), bool(br_var.get())))
            dlg.destroy()

        btns = ttk.Frame(dlg)
        btns.grid(row=3, column=0, columnspan=2, pady=8)
        ttk.Button(btns, text="추가", command=_ok).pack(side="left", padx=6)
        ttk.Button(btns, text="취소", command=dlg.destroy).pack(side="left", padx=6)

    def _quick_add_branch(self) -> None:
        dlg = tk.Toplevel(self)
        dlg.title("조건 분기 (Conditional Branch)")
        dlg.transient(self.winfo_toplevel())
        dlg.grab_set()
        kind_map = {"스위치 (ON/OFF)": "switch", "변수 (>= 값)": "variable", "소지금 (>= 골드)": "gold", "아이템 보유": "item"}
        k_var = tk.StringVar(value="스위치 (ON/OFF)")
        id_var = tk.IntVar(value=1)
        val_var = tk.IntVar(value=100)
        exp_var = tk.StringVar(value="ON")
        else_var = tk.BooleanVar(value=True)
        ttk.Label(dlg, text="조건 종류").grid(row=0, column=0, sticky="w", padx=8, pady=4)
        ttk.Combobox(dlg, textvariable=k_var, values=list(kind_map.keys()), state="readonly", width=20).grid(row=0, column=1, padx=8, pady=4)
        ttk.Label(dlg, text="대상 ID (스위치/변수/아이템)").grid(row=1, column=0, sticky="w", padx=8, pady=4)
        ttk.Spinbox(dlg, from_=1, to=999, textvariable=id_var, width=10).grid(row=1, column=1, sticky="w", padx=8, pady=4)
        ttk.Label(dlg, text="비교 값 (변수/소지금)").grid(row=2, column=0, sticky="w", padx=8, pady=4)
        ttk.Spinbox(dlg, from_=0, to=999999, textvariable=val_var, width=10).grid(row=2, column=1, sticky="w", padx=8, pady=4)
        ttk.Label(dlg, text="스위치 기대 상태").grid(row=3, column=0, sticky="w", padx=8, pady=4)
        ttk.Combobox(dlg, textvariable=exp_var, values=["ON", "OFF"], state="readonly", width=8).grid(row=3, column=1, sticky="w", padx=8, pady=4)
        ttk.Checkbutton(dlg, text="거짓(else) 분기 포함", variable=else_var).grid(row=4, column=0, columnspan=2, sticky="w", padx=8, pady=4)

        def _ok() -> None:
            self._append_ev_cmds(
                self.build_branch_cmds(
                    kind_map[k_var.get()],
                    target_id=int(id_var.get()),
                    value=int(val_var.get()),
                    expected=exp_var.get(),
                    include_else=bool(else_var.get()),
                )
            )
            dlg.destroy()

        btns = ttk.Frame(dlg)
        btns.grid(row=5, column=0, columnspan=2, pady=8)
        ttk.Button(btns, text="추가", command=_ok).pack(side="left", padx=6)
        ttk.Button(btns, text="취소", command=dlg.destroy).pack(side="left", padx=6)

    def _insert_ev_dsl(self) -> None:
        tpl = DSL_TEMPLATES.get(self.ev_dsl_preset.get())
        if not tpl:
            return
        self._append_ev_cmds([dict(tpl)])

    def save_current_event(self) -> bool:
        if not self.app.ir_dir:
            return False
        try:
            self._flush_page_fields_into_working()
            self._working_event["id"] = int(self.ev_id_var.get())
            self._working_event["name"] = self.ev_name_var.get().strip() or "Event"
            self._working_event["x"] = int(self.ev_x_var.get())
            self._working_event["y"] = int(self.ev_y_var.get())
            res = save_event(self.app.ir_dir, self.current_map_id, self._working_event)
            self._event_clean_fp = ""
            self.app.log_console(f"[이벤트 저장] {res}")
            self.app.reload_all(keep_map_id=self.current_map_id, keep_event_id=int(res["event_id"]))
            return True
        except Exception as exc:
            messagebox.showerror("이벤트 저장 오류", str(exc), parent=self)
            return False

    def delete_selected_event(self) -> None:
        if not self.app.ir_dir or self.selected_event_id is None:
            return
        res = delete_event(self.app.ir_dir, self.current_map_id, self.selected_event_id)
        self.app.log_console(f"[이벤트 삭제] {res}")
        self.app.reload_all(keep_map_id=self.current_map_id)


class AgentConsoleDock(ttk.LabelFrame):
    """Bottom-docked Agent & CLI Instant Command Console integrated with Studio selection state."""

    def __init__(self, master: tk.Misc, app: Any):
        super().__init__(master, text="에이전트 / CLI 즉시 명령 콘솔 (CLI · Workbench · JSON Blueprint · 한국어/영문 자연어 · 외부 AI CLI)")
        self.app = app
        self._history: List[str] = []
        self._hist_idx: int = -1
        self._ext_busy: bool = False
        self._ext_queue: queue.Queue[Dict[str, Any]] = queue.Queue()
        self._last_async_thread: Optional[threading.Thread] = None
        self.ext_cmd_var = tk.StringVar(value=os.environ.get("RPGC_AGENT_CMD", ""))
        self._build_ui()

    def _build_ui(self) -> None:
        chips = ttk.Frame(self)
        chips.pack(fill="x", padx=6, pady=(4, 2))
        quick = [
            ("무결성 검증", "check"),
            ("에셋 검사", "check-assets"),
            ("밸런스 체크", "balance-check"),
            ("퍼징 100회", "퍼징 100회"),
            ("적 체력 1.2배", "적 체력 1.2배"),
            ("되돌리기", "undo"),
        ]
        for label, cmd in quick:
            ttk.Button(chips, text=label, command=lambda c=cmd: self.run_command_text(c)).pack(side="left", padx=2)

        ttk.Button(chips, text="AI 컨텍스트 복사", command=self._copy_ai_context).pack(side="left", padx=(10, 2))
        ttk.Label(chips, text="외부 AI CLI (선택):").pack(side="left", padx=(10, 2))
        ttk.Entry(chips, textvariable=self.ext_cmd_var, width=24).pack(side="left", padx=2)
        ttk.Button(chips, text="?", width=2, command=self._show_external_cli_help).pack(side="left", padx=1)

        row = ttk.Frame(self)
        row.pack(fill="x", padx=6, pady=3)
        self.cmd_var = tk.StringVar()
        self.cmd_entry = ttk.Entry(row, textvariable=self.cmd_var, font=("Consolas", 10))
        self.cmd_entry.pack(side="left", fill="x", expand=True)
        self.cmd_entry.bind("<Return>", lambda _e: self.on_submit())
        self.cmd_entry.bind("<Up>", lambda _e: self._step_history(-1))
        self.cmd_entry.bind("<Down>", lambda _e: self._step_history(1))
        self.btn_send = ttk.Button(row, text="▶ 실행", command=self.on_submit)
        self.btn_send.pack(side="left", padx=(6, 0))

        self.log_box = scrolledtext.ScrolledText(self, height=7, font=("Consolas", 9))
        self.log_box.pack(fill="both", expand=True, padx=6, pady=(0, 4))

    def _show_external_cli_help(self) -> None:
        messagebox.showinfo(
            "외부 AI CLI 연동 안내",
            "외부 AI CLI (gemini / claude 등)를 연결하지 않아도 위 버튼 및 자연어(예: '고블린 체력 200', '밸런스 체크', '도움말')는 즉시 동작합니다.\n\n"
            "외부 AI 연동 예시 (권장 — 프로젝트 컨텍스트 프롬프트가 stdin으로 전달됩니다):\n"
            "• claude -p\n"
            "• gemini -p \"아래 요청에 맞는 Blueprint JSON만 출력해\"\n\n"
            "명령에 {prompt}를 넣으면 그 자리에 프롬프트가 인자로 안전하게 치환됩니다 (셸을 거치지 않음).\n"
            "환경변수 RPGC_AGENT_CMD로 기본값을 지정할 수도 있습니다.\n"
            "(실행 중에도 화면이 멈추지 않도록 백그라운드에서 실행되며, 제한시간은 120초입니다.)",
            parent=self,
        )

    def append_log(self, msg: str) -> None:
        self.log_box.insert("end", msg.rstrip() + "\n")
        self.log_box.see("end")

    def _step_history(self, delta: int) -> None:
        if not self._history:
            return
        self._hist_idx = max(0, min(len(self._history) - 1, self._hist_idx + delta))
        self.cmd_var.set(self._history[self._hist_idx])
        self.cmd_entry.icursor("end")

    def on_submit(self) -> None:
        text = self.cmd_var.get().strip()
        if not text:
            return
        self.cmd_var.set("")
        self.run_command_text(text)

    def run_command_text(self, text: str) -> Dict[str, Any]:
        if not self.app.ir_dir:
            self.append_log("[INFO] 먼저 IR 프로젝트를 열거나 새 샘플을 생성하세요.")
            return {"ok": False, "mutated": False}
        self._history.append(text)
        self._hist_idx = len(self._history)
        self.append_log(f">>> {text}")

        ctx = self.app.get_selection_context()
        ext_cmd = self.ext_cmd_var.get().strip() or os.environ.get("RPGC_AGENT_CMD", "").strip()
        if ext_cmd:
            plan = parse_agent_instruction(text, self.app.ir_dir, context=ctx)
            if plan.get("kind") == "unmatched":
                if self._ext_busy:
                    self.append_log("[대기] 이전 외부 AI CLI 작업이 완료될 때까지 잠시 기다려 주세요.")
                    return {"ok": False, "kind": "external_busy", "summary": "외부 AI CLI 실행 중", "mutated": False}
                self._ext_busy = True
                self.btn_send.configure(text="⏳ 대기...", state="disabled")
                self.append_log(f"[외부 AI CLI 백그라운드 실행 중...] {ext_cmd}")
                ir_target = self.app.ir_dir

                def _worker() -> None:
                    try:
                        res = execute_studio_command(
                            ir_target,
                            text,
                            context=ctx,
                            external_agent_cmd=ext_cmd,
                            apply_external=True,
                        )
                    except Exception as exc:
                        res = {"ok": False, "mutated": False, "summary": f"[외부 AI CLI 오류] {exc}"}
                    self._ext_queue.put(res)

                t = threading.Thread(target=_worker, daemon=True)
                self._last_async_thread = t
                t.start()
                self.after(50, self._poll_ext_queue)
                return {"ok": True, "kind": "external_async", "summary": "외부 AI CLI 실행 시작", "mutated": False}

        res = execute_studio_command(
            self.app.ir_dir,
            text,
            context=ctx,
            external_agent_cmd=ext_cmd or None,
            apply_external=True,
        )
        self.append_log(str(res.get("summary", "")))
        if res.get("mutated"):
            self.app.reload_all()
        return res

    def _drain_ext_queue(self) -> bool:
        handled = False
        while True:
            try:
                res = self._ext_queue.get_nowait()
            except queue.Empty:
                break
            handled = True
            self._ext_busy = False
            try:
                self.btn_send.configure(text="▶ 실행", state="normal")
                self.append_log(str(res.get("summary", "")))
                if res.get("mutated"):
                    self.app.reload_all()
            except tk.TclError:
                pass
        return handled

    def _poll_ext_queue(self) -> None:
        self._drain_ext_queue()
        if self._ext_busy:
            try:
                self.after(50, self._poll_ext_queue)
            except tk.TclError:
                self._ext_busy = False

    def _copy_ai_context(self) -> None:
        if not self.app.ir_dir:
            return
        ctx = self.app.get_selection_context()
        prompt = format_agent_prompt_packet(
            self.app.ir_dir,
            user_goal="",
            current_map_id=ctx.get("current_map_id"),
            selected_event_id=ctx.get("selected_event_id"),
            selected_table=ctx.get("selected_table"),
        )
        self.clipboard_clear()
        self.clipboard_append(prompt)
        self.append_log("[AI 컨텍스트 복사 완료] 현재 맵/이벤트/DB 상태 프롬프트가 클립보드에 복사되었습니다.")


class StudioApp:
    """Main Tkinter Studio controller orchestrating workspace lifecycle, tabs, playtest, and agent dock."""

    def __init__(self, root: tk.Tk, ir_dir: Optional[Path] = None, graphics_dir: Optional[Path] = None):
        self.root = root
        self.root.title("RPG Creator Studio (RMXP Visual + Agent IDE)")
        self.root.geometry("1380x880")
        ico_path = _find_studio_icon_path()
        if ico_path is not None:
            try:
                self.root.iconbitmap(default=str(ico_path))
            except Exception:
                pass
        self.ir_dir: Optional[Path] = Path(ir_dir) if ir_dir else None
        self.explicit_graphics_dir: Optional[Path] = Path(graphics_dir) if graphics_dir else None
        self.atlas: Optional[TileAtlasCache] = None
        self._player_proc: Optional[subprocess.Popen[Any]] = None

        self._build_menu()
        self._build_layout()
        self._bind_shortcuts()
        self.root.update_idletasks()
        self._outline_drag_ctrl = attach_outline_drag_preview(
            self.root.winfo_id(),
            min_size=(720, 480),
            on_commit=lambda _w, _h: self.root.update_idletasks(),
        )

        if self.ir_dir and self.ir_dir.is_dir() and (self.ir_dir / "ir_project.json").is_file():
            self.open_workspace(self.ir_dir, self.explicit_graphics_dir)
        else:
            self.log_console("[Studio] 상단 '새 샘플' 또는 '열기 (IR)' 버튼으로 작업할 프로젝트를 선택하세요.")

    def _build_menu(self) -> None:
        menubar = tk.Menu(self.root)
        m_file = tk.Menu(menubar, tearoff=0)
        m_file.add_command(label="새 샘플 프로젝트 생성...", command=self.action_new_sample)
        m_file.add_command(label="IR 프로젝트 열기...", command=self.action_open_ir)
        m_file.add_separator()
        m_file.add_command(label="RMXP 원본 언팩 (Data -> IR)...", command=self.action_unpack_rmxp)
        m_file.add_command(label="RMXP 바이너리 팩 (IR -> Data)...", command=self.action_pack_rmxp)
        m_file.add_command(label="RGSSAD 언팩 (.rgss3a/.rgssad)...", command=self.action_unpack_rgssad)
        m_file.add_command(label="RGSSAD 팩...", command=self.action_pack_rgssad)
        m_file.add_separator()
        m_file.add_command(label="RPG Maker MV/MZ 가져오기...", command=self.action_import_mv)
        m_file.add_command(label="RPG Maker VX Ace 가져오기...", command=self.action_import_vxace)
        m_file.add_command(label="Tiled (.tmj/.json) 가져오기...", command=self.action_import_tiled)
        m_file.add_separator()
        m_file.add_command(label="종료", command=self.root.destroy)
        menubar.add_cascade(label="파일", menu=m_file)

        m_edit = tk.Menu(menubar, tearoff=0)
        m_edit.add_command(label="되돌리기 (Ctrl+Z)", command=self.action_undo)
        m_edit.add_command(label="다시 실행 (Ctrl+Y)", command=self.action_redo)
        m_edit.add_command(label="새로고침 (F5)", command=self.reload_all)
        menubar.add_cascade(label="편집", menu=m_edit)

        m_play = tk.Menu(menubar, tearoff=0)
        m_play.add_command(label="▶ Pygame 플레이테스트 (F6)", command=self.action_playtest)
        menubar.add_cascade(label="실행", menu=m_play)
        self.root.config(menu=menubar)

    def _build_layout(self) -> None:
        tbar = ttk.Frame(self.root)
        tbar.pack(fill="x", padx=6, pady=4)

        ttk.Button(tbar, text="새 샘플", command=self.action_new_sample).pack(side="left", padx=2)
        ttk.Button(tbar, text="열기 (IR)", command=self.action_open_ir).pack(side="left", padx=2)
        ttk.Button(tbar, text="RMXP 팩", command=self.action_pack_rmxp).pack(side="left", padx=2)
        ttk.Separator(tbar, orient="vertical").pack(side="left", fill="y", padx=6)
        ttk.Button(tbar, text="↶ 되돌리기", command=self.action_undo).pack(side="left", padx=2)
        ttk.Button(tbar, text="↷ 다시실행", command=self.action_redo).pack(side="left", padx=2)
        ttk.Button(tbar, text="↻ 새로고침", command=self.reload_all).pack(side="left", padx=2)
        ttk.Separator(tbar, orient="vertical").pack(side="left", fill="y", padx=6)
        ttk.Button(tbar, text="▶ 플레이테스트 (Pygame)", command=self.action_playtest).pack(side="left", padx=4)

        self.lbl_workspace = ttk.Label(tbar, text="프로젝트: (없음)", font=("", 9, "bold"))
        self.lbl_workspace.pack(side="right", padx=6)

        v_split = create_preview_paned(self.root, orient="vertical")
        v_split.pack(fill="both", expand=True, padx=4, pady=(0, 4))

        self.notebook = ttk.Notebook(v_split)
        v_split.add(self.notebook, weight=4)

        self.tab_map = MapEditorTab(self.notebook, self)
        self.tab_db = DatabaseTab(self.notebook, self)
        self.tab_ca = CommonAnimTab(self.notebook, self)
        self.tab_ops = OpsReleaseTab(self.notebook, self)
        self.tab_assets = AssetBrowserTab(self.notebook, self)

        self.notebook.add(self.tab_map, text="1. 맵 · 타일 · 이벤트")
        self.notebook.add(self.tab_db, text="2. 데이터베이스 · 밸런스")
        self.notebook.add(self.tab_ca, text="3. 커먼 이벤트 · 애니메이션")
        self.notebook.add(self.tab_ops, text="4. 검증 · 시뮬 · 다국어 · 출시")
        self.notebook.add(self.tab_assets, text="5. 에셋 브라우저 (Graphics · Audio)")

        self.agent_dock = AgentConsoleDock(v_split, self)
        v_split.add(self.agent_dock, weight=1)

    def _is_text_widget_focused(self) -> bool:
        try:
            w = self.root.focus_get()
        except Exception:
            return False
        if w is None:
            return False
        return isinstance(w, (tk.Text, tk.Entry, ttk.Entry, ttk.Spinbox, ttk.Combobox))

    def _on_shortcut_undo(self) -> Optional[str]:
        if self._is_text_widget_focused():
            return None
        self.action_undo()
        return "break"

    def _on_shortcut_redo(self) -> Optional[str]:
        if self._is_text_widget_focused():
            return None
        self.action_redo()
        return "break"

    def _bind_shortcuts(self) -> None:
        self.root.bind("<Control-z>", lambda _e: self._on_shortcut_undo())
        self.root.bind("<Control-y>", lambda _e: self._on_shortcut_redo())
        self.root.bind("<F5>", lambda _e: self.reload_all())
        self.root.bind("<F6>", lambda _e: self.action_playtest())

    def log_console(self, msg: str) -> None:
        self.agent_dock.append_log(msg)

    def get_active_map_id(self) -> int:
        return int(self.tab_map.current_map_id or 1)

    def get_selection_context(self) -> Dict[str, Any]:
        return {
            "current_map_id": self.tab_map.current_map_id,
            "selected_event_id": self.tab_map.selected_event_id,
            "selected_table": self.tab_db.current_table.get(),
        }

    def open_workspace(self, ir_dir: Path, graphics_dir: Optional[Path] = None) -> None:
        ir_path = Path(ir_dir)
        if not (ir_path / "ir_project.json").is_file():
            raise FileNotFoundError(f"유효한 IR 워크스페이스가 아닙니다: {ir_path}")
        self.ir_dir = ir_path
        if graphics_dir is not None:
            self.explicit_graphics_dir = Path(graphics_dir)
        self.atlas = TileAtlasCache(self.root, self.ir_dir, self.explicit_graphics_dir)
        self.lbl_workspace.config(text=f"프로젝트: {self.ir_dir}")
        self.reload_all()
        self.log_console(f"[열기 완료] {self.ir_dir}")

    def reload_all(
        self,
        keep_map_id: Optional[int] = None,
        keep_event_id: Optional[int] = None,
        keep_db_id: Optional[int] = None,
    ) -> None:
        if not self.ir_dir or not self.atlas:
            return
        self.atlas.reload_metadata(self.explicit_graphics_dir)
        self.tab_map.reload_maps(select_map_id=keep_map_id, select_event_id=keep_event_id)
        self.tab_db.reload_entries(select_id=keep_db_id)
        self.tab_ca.reload_all()
        self.tab_assets.reload_all()

    def action_new_sample(self) -> None:
        target = filedialog.askdirectory(parent=self.root, title="새 샘플 프로젝트를 생성할 폴더 선택")
        if not target:
            return
        res = generate_sample_project(Path(target))
        self.open_workspace(Path(res["ir_dir"]), Path(target) / "rmxp" / "Graphics")
        self.log_console(f"[새 샘플 생성] {res}")

    def action_open_ir(self) -> None:
        target = filedialog.askdirectory(parent=self.root, title="IR 프로젝트 폴더 (ir_project.json 포함) 선택")
        if target:
            try:
                self.open_workspace(Path(target))
            except Exception as exc:
                messagebox.showerror("열기 오류", str(exc), parent=self.root)

    def action_unpack_rmxp(self) -> None:
        rmxp = filedialog.askdirectory(parent=self.root, title="RMXP 원본 프로젝트 폴더 선택")
        if not rmxp:
            return
        out = filedialog.askdirectory(parent=self.root, title="출력 IR 폴더 선택")
        if not out:
            return
        res = unpack_rmxp_to_ir(Path(rmxp), Path(out))
        self.open_workspace(Path(out), Path(rmxp) / "Graphics")
        self.log_console(f"[RMXP 언팩] {res}")

    def action_pack_rmxp(self) -> None:
        if not self.ir_dir:
            return
        out = filedialog.askdirectory(parent=self.root, title="RMXP Data 출력 폴더 선택")
        if not out:
            return
        res = pack_ir_to_rmxp(self.ir_dir, Path(out))
        self.log_console(f"[RMXP 팩 완료] {res}")

    def action_unpack_rgssad(self) -> None:
        arch = filedialog.askopenfilename(parent=self.root, filetypes=[("RGSS Archive", "*.rgssad;*.rgss2a;*.rgss3a"), ("All", "*.*")])
        if not arch:
            return
        out = filedialog.askdirectory(parent=self.root, title="RGSSAD 압축 해제 대상 폴더")
        if out:
            res = unpack_rgssad(Path(arch), Path(out), key=DEFAULT_RGSSAD_KEY)
            self.log_console(f"[RGSSAD 언팩] {res}")

    def action_pack_rgssad(self) -> None:
        src = filedialog.askdirectory(parent=self.root, title="아카이빙할 프로젝트 루트 폴더")
        if not src:
            return
        out = filedialog.asksaveasfilename(parent=self.root, defaultextension=".rgss3a", filetypes=[("RGSS3A", "*.rgss3a"), ("RGSSAD", "*.rgssad")])
        if out:
            res = pack_rgssad(Path(src), Path(out), key=DEFAULT_RGSSAD_KEY)
            self.log_console(f"[RGSSAD 팩] {res}")

    def action_import_mv(self) -> None:
        mv_dir = filedialog.askdirectory(parent=self.root, title="RPG Maker MV/MZ 폴더 선택")
        if not mv_dir:
            return
        out = filedialog.askdirectory(parent=self.root, title="변환 IR 저장 폴더 선택")
        if out:
            res = import_rpgmaker_mv_mz(Path(mv_dir), Path(out))
            self.open_workspace(Path(out))
            self.log_console(f"[MV/MZ 임포트] {res}")

    def action_import_vxace(self) -> None:
        vx_dir = filedialog.askdirectory(parent=self.root, title="RPG Maker VX Ace Data 폴더 선택")
        if not vx_dir:
            return
        out = filedialog.askdirectory(parent=self.root, title="변환 IR 저장 폴더 선택")
        if out:
            res = import_vxace_rxdata(Path(vx_dir), Path(out))
            self.open_workspace(Path(out))
            self.log_console(f"[VX Ace 임포트] {res}")

    def action_import_tiled(self) -> None:
        if not self.ir_dir:
            return
        tmj = filedialog.askopenfilename(parent=self.root, filetypes=[("Tiled JSON", "*.tmj;*.json")])
        if tmj:
            res = import_tiled_map(Path(tmj), self.ir_dir)
            self.reload_all(keep_map_id=int(res["map_id"]))
            self.log_console(f"[Tiled 임포트] {res}")

    def action_undo(self) -> None:
        if not self.ir_dir:
            return
        res = undo_last_edit(self.ir_dir)
        self.log_console(f"[되돌리기] {res}")
        if res.get("restored"):
            self.reload_all()

    def action_redo(self) -> None:
        if not self.ir_dir:
            return
        res = redo_last_edit(self.ir_dir)
        self.log_console(f"[다시실행] {res}")
        if res.get("restored"):
            self.reload_all()

    def action_playtest(self) -> None:
        if not self.ir_dir:
            messagebox.showinfo("플레이테스트", "먼저 프로젝트를 열거나 새 샘플을 생성하세요.", parent=self.root)
            return
        if self._player_proc is not None and self._player_proc.poll() is None:
            messagebox.showinfo("플레이테스트", "이미 플레이테스트 창이 실행 중입니다.", parent=self.root)
            return
        if getattr(sys, "frozen", False):
            cmd = [sys.executable, "play", str(self.ir_dir), "--title"]
            run_cwd = str(Path(sys.executable).resolve().parent)
        else:
            cmd = [sys.executable, str(_ROOT_DIR / "run_app.py"), "play", str(self.ir_dir), "--title"]
            run_cwd = str(_ROOT_DIR)
        if self.atlas and self.atlas.graphics_dir:
            cmd.extend(["--graphics-dir", str(self.atlas.graphics_dir)])
        self._player_proc = subprocess.Popen(cmd, cwd=run_cwd)
        self.log_console(f"[플레이테스트 실행] PID={self._player_proc.pid} -> {' '.join(cmd)}")


def _find_default_ir_dir() -> Optional[Path]:
    """Searches likely workspace directories for an existing sample IR project."""
    candidates = [
        Path.cwd() / "sample_workspace" / "ir",
        Path(sys.executable).resolve().parent / "sample_workspace" / "ir",
        _ROOT_DIR / "sample_workspace" / "ir",
    ]
    for cand in candidates:
        if (cand / "ir_project.json").is_file():
            return cand
    return None


def launch_studio(
    ir_dir: Optional[Path] = None,
    graphics_dir: Optional[Path] = None,
) -> int:
    """Starts the Tkinter + Pillow RPG Creator Studio GUI main loop."""
    root = tk.Tk()
    default_ir = ir_dir if ir_dir is not None else _find_default_ir_dir()
    StudioApp(root, ir_dir=default_ir, graphics_dir=graphics_dir)
    root.mainloop()
    return 0
