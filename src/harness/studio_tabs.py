# -*- coding: utf-8 -*-
"""
Studio GUI Tabs: Database & Balance Fine-Tuner, Common Events & Animations,
and Validation / Simulator / i18n / Blueprint / Commercial Release Ops.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, ttk
from typing import Any, Callable, Dict, List, Optional, Tuple

from PIL import Image, ImageTk

from .window_opt import (
    bind_text_modified,
    create_preview_paned,
    replace_text_undoable,
    reset_text_with_content,
)

from ..core.animation_cells import extract_animation_frame_cells
from ..core.battle_formulas import PRESETS, BattleFormula, FormulaError, list_presets
from ..core.editor import (
    apply_blueprint,
    batch_tune_balance,
    calc_battle_damage,
    capture_undo_snapshot,
    delete_db_entry,
    export_release_bundle,
    get_battle_formula,
    get_db_entry,
    list_animations,
    list_common_events,
    push_undo_snapshot,
    set_animation,
    set_battle_formula,
    set_common_event,
    set_db_entry,
)
from ..core.i18n import apply_i18n_locale, extract_i18n_strings
from ..core.simulator import run_fuzz_playtest, run_headless_simulation
from ..core.utils import load_json
from ..core.validator import (
    export_flow_graph,
    render_map_preview,
    simulate_battle_balance,
    validate_ir_project,
    validate_project_assets,
)

DSL_TEMPLATES: Dict[str, Dict[str, Any]] = {
    "대사 (say)": {"op": "say", "lines": ["안녕하세요! 모험가님."], "indent": 0},
    "선택지 (show_choices)": {"op": "show_choices", "choices": ["예", "아니오"], "cancel_type": 2, "indent": 0},
    "스위치 ON (set_switch)": {"op": "set_switch", "start_id": 1, "end_id": 1, "value": "ON", "indent": 0},
    "셀프 스위치 A ON": {"op": "set_self_switch", "channel": "A", "value": "ON", "indent": 0},
    "변수 대입 (set_variable)": {"op": "set_variable", "start_id": 1, "end_id": 1, "operation": 0, "operand_type": 0, "operand": [1], "indent": 0},
    "골드 획득 (change_gold)": {"op": "change_gold", "mode": "add", "amount": 100, "indent": 0},
    "아이템 획득 (change_items)": {"op": "change_items", "item_id": 1, "mode": "add", "amount": 1, "indent": 0},
    "장소 이동 (transfer_player)": {"op": "transfer_player", "map_id": 1, "x": 5, "y": 5, "direction": "down", "indent": 0},
    "전투 개시 (battle)": {"op": "battle", "troop_id": 1, "can_escape": True, "can_lose": False, "indent": 0},
    "커먼 이벤트 (common_event)": {"op": "common_event", "common_event_id": 1, "indent": 0},
    "조건 분기 스위치 (if_switch)": {"op": "if_switch", "switch_id": 1, "expected": "ON", "indent": 0},
    "반복 / 종료 (end)": {"op": "end", "indent": 0},
}


# Structured fine-tuning fields exposed cleanly per database table
_TABLE_QUICK_FIELDS: Dict[str, List[Tuple[str, str, str]]] = {
    "actors": [
        ("name", "이름", "str"),
        ("class_id", "직업 ID", "int"),
        ("initial_level", "초기 레벨", "int"),
        ("final_level", "최대 레벨", "int"),
        ("character_name", "캐릭터 파일", "str"),
        ("battler_name", "배틀러 파일", "str"),
        ("weapon_id", "무기 ID", "int"),
        ("armor1_id", "방패 ID", "int"),
        ("armor2_id", "투구 ID", "int"),
        ("armor3_id", "갑옷 ID", "int"),
        ("armor4_id", "장신구 ID", "int"),
    ],
    "classes": [
        ("name", "직업명", "str"),
        ("position", "포지션(0전/1중/2후)", "int"),
    ],
    "skills": [
        ("name", "스킬명", "str"),
        ("description", "설명", "str"),
        ("icon_name", "아이콘", "str"),
        ("scope", "대상(0..7)", "int"),
        ("occasion", "사용시기(0..3)", "int"),
        ("sp_cost", "소모 SP", "int"),
        ("power", "위력", "int"),
        ("atk_f", "ATK 계수", "int"),
        ("eva_f", "회피 계수", "int"),
        ("str_f", "STR 계수", "int"),
        ("dex_f", "DEX 계수", "int"),
        ("agi_f", "AGI 계수", "int"),
        ("int_f", "INT 계수", "int"),
        ("hit", "명중률(%)", "int"),
        ("pdef_f", "PDEF 경감", "int"),
        ("mdef_f", "MDEF 경감", "int"),
        ("variance", "분산(%)", "int"),
    ],
    "items": [
        ("name", "아이템명", "str"),
        ("description", "설명", "str"),
        ("icon_name", "아이콘", "str"),
        ("scope", "대상(0..7)", "int"),
        ("occasion", "사용시기(0..3)", "int"),
        ("price", "가격", "int"),
        ("consumable", "소모품", "bool"),
        ("recover_hp_rate", "HP 회복%", "int"),
        ("recover_hp", "HP 고정회복", "int"),
        ("recover_sp_rate", "SP 회복%", "int"),
        ("recover_sp", "SP 고정회복", "int"),
        ("hit", "명중률(%)", "int"),
    ],
    "weapons": [
        ("name", "무기명", "str"),
        ("description", "설명", "str"),
        ("icon_name", "아이콘", "str"),
        ("price", "가격", "int"),
        ("atk", "공격력(ATK)", "int"),
        ("pdef", "물리방어", "int"),
        ("mdef", "마법방어", "int"),
        ("str_plus", "STR +", "int"),
        ("dex_plus", "DEX +", "int"),
        ("agi_plus", "AGI +", "int"),
        ("int_plus", "INT +", "int"),
    ],
    "armors": [
        ("name", "방어구명", "str"),
        ("description", "설명", "str"),
        ("icon_name", "아이콘", "str"),
        ("kind", "종류(0방/1투/2갑/3장)", "int"),
        ("price", "가격", "int"),
        ("pdef", "물리방어(PDEF)", "int"),
        ("mdef", "마법방어(MDEF)", "int"),
        ("eva", "회피(EVA)", "int"),
        ("str_plus", "STR +", "int"),
        ("dex_plus", "DEX +", "int"),
        ("agi_plus", "AGI +", "int"),
        ("int_plus", "INT +", "int"),
    ],
    "enemies": [
        ("name", "적 이름", "str"),
        ("battler_name", "배틀러 파일", "str"),
        ("battler_hue", "색조(Hue)", "int"),
        ("maxhp", "최대 HP", "int"),
        ("maxsp", "최대 SP", "int"),
        ("str", "STR", "int"),
        ("dex", "DEX", "int"),
        ("agi", "AGI", "int"),
        ("int", "INT", "int"),
        ("atk", "공격력(ATK)", "int"),
        ("pdef", "물리방어(PDEF)", "int"),
        ("mdef", "마법방어(MDEF)", "int"),
        ("eva", "회피(EVA)", "int"),
        ("exp", "경험치(EXP)", "int"),
        ("gold", "골드(Gold)", "int"),
        ("item_id", "드롭 아이템 ID", "int"),
        ("weapon_id", "드롭 무기 ID", "int"),
        ("armor_id", "드롭 방어구 ID", "int"),
        ("treasure_prob", "드롭 확률(%)", "int"),
    ],
    "troops": [
        ("name", "트룹명", "str"),
    ],
    "states": [
        ("name", "상태이상명", "str"),
        ("rating", "우선도(0..10)", "int"),
        ("restriction", "행동제한(0..4)", "int"),
        ("nonresistance", "무시 불가", "bool"),
        ("zero_hp", "HP 0 전투불능", "bool"),
        ("cant_get_exp", "경험치 획득불가", "bool"),
        ("cant_evade", "회피 불가", "bool"),
        ("slip_damage", "슬립 데미지", "bool"),
        ("atk_rate", "공격력%", "int"),
        ("pdef_rate", "물리방어%", "int"),
        ("mdef_rate", "마법방어%", "int"),
        ("agi_rate", "민첩%", "int"),
        ("hold_turn", "유지 턴 수", "int"),
        ("auto_release_prob", "자연해제 확률%", "int"),
    ],
    "tilesets": [
        ("name", "타일셋명", "str"),
        ("tileset_name", "타일셋 파일", "str"),
        ("panorama_name", "파노라마 파일", "str"),
        ("fog_name", "포그 파일", "str"),
        ("battleback_name", "전투배경 파일", "str"),
    ],
    "system": [
        ("start_map_id", "시작 맵 ID", "int"),
        ("start_x", "시작 X 좌표", "int"),
        ("start_y", "시작 Y 좌표", "int"),
        ("windowskin_name", "윈도우스킨", "str"),
        ("battler_name", "테스트 배틀러", "str"),
        ("magic_number", "매직 넘버", "int"),
    ],
}

DB_CATEGORIES: Tuple[str, ...] = (
    "actors",
    "classes",
    "skills",
    "items",
    "weapons",
    "armors",
    "enemies",
    "troops",
    "states",
    "tilesets",
    "switches",
    "variables",
    "system",
)


def create_scrollable_container(parent: tk.Misc) -> Tuple[ttk.Frame, ttk.Frame]:
    """Builds a vertical-scrollable container and returns (outer_frame, inner_frame)."""
    outer = ttk.Frame(parent)
    canvas = tk.Canvas(outer, highlightthickness=0)
    vbar = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
    inner = ttk.Frame(canvas)
    inner_id = canvas.create_window((0, 0), window=inner, anchor="nw")

    _last_scroll_bbox = [None]
    _last_inner_w = [None]

    def _on_inner_configure(_e: Any) -> None:
        bbox = canvas.bbox("all")
        if bbox != _last_scroll_bbox[0]:
            _last_scroll_bbox[0] = bbox
            canvas.configure(scrollregion=bbox)

    def _on_canvas_configure(e: Any) -> None:
        if e.width != _last_inner_w[0]:
            _last_inner_w[0] = e.width
            canvas.itemconfigure(inner_id, width=e.width)

    inner.bind("<Configure>", _on_inner_configure)
    canvas.bind("<Configure>", _on_canvas_configure)
    canvas.configure(yscrollcommand=vbar.set)
    canvas.pack(side="left", fill="both", expand=True)
    vbar.pack(side="right", fill="y")
    return outer, inner

# Animation preview: RMXP plays animations at 20 frames/sec.
ANIM_PREVIEW_PX = 160
ANIM_FRAME_MS = 50


class DatabaseTab(ttk.Frame):
    """Database & Balance Fine-Tuner covering every RMXP IR table and batch balance scaling."""

    def __init__(self, master: tk.Misc, app: Any):
        super().__init__(master)
        self.app = app
        self.current_table = tk.StringVar(value="enemies")
        self.current_entry_id: int = 1
        self._quick_vars: Dict[str, Tuple[tk.Variable, str]] = {}
        self._build_ui()

    def _build_ui(self) -> None:
        paned = create_preview_paned(self, orient="horizontal")
        paned.pack(fill="both", expand=True, padx=4, pady=4)

        # Left column: Category + Entry List
        left = ttk.Frame(paned, width=240)
        paned.add(left, weight=1)

        ttk.Label(left, text="테이블 선택", font=("", 9, "bold")).pack(anchor="w")
        tbl_cb = ttk.Combobox(left, textvariable=self.current_table, values=list(DB_CATEGORIES), state="readonly")
        tbl_cb.pack(fill="x", pady=(2, 6))
        tbl_cb.bind("<<ComboboxSelected>>", lambda _e: self.reload_entries())

        self.entry_list = tk.Listbox(left, exportselection=False)
        self.entry_list.pack(fill="both", expand=True)
        self.entry_list.bind("<<ListboxSelect>>", lambda _e: self._on_select_entry())

        btn_row = ttk.Frame(left)
        btn_row.pack(fill="x", pady=4)
        ttk.Button(btn_row, text="+ 새 항목", command=self.add_new_entry).pack(side="left", fill="x", expand=True, padx=1)
        ttk.Button(btn_row, text="복제", command=self.clone_entry).pack(side="left", fill="x", expand=True, padx=1)
        ttk.Button(btn_row, text="삭제", command=self.delete_selected_entry).pack(side="left", fill="x", expand=True, padx=1)

        # Center column: Quick Structured Form + Full JSON Inspector
        center = ttk.Frame(paned)
        paned.add(center, weight=3)

        hdr = ttk.Frame(center)
        hdr.pack(fill="x", pady=(0, 4))
        self.lbl_entry = ttk.Label(hdr, text="선택된 항목: -", font=("", 10, "bold"))
        self.lbl_entry.pack(side="left")
        ttk.Button(hdr, text="변경사항 저장 (Ctrl+Z 지원)", command=self.save_current_entry).pack(side="right", padx=2)
        ttk.Button(hdr, text="타일셋 통행 그리드 편집", command=self._open_tileset_passages_dialog).pack(side="right", padx=2)

        sub_nb = ttk.Notebook(center)
        sub_nb.pack(fill="both", expand=True)

        quick_outer, self.quick_form = create_scrollable_container(sub_nb)
        sub_nb.add(quick_outer, text="구조화 미세조정 (Quick Fields)")

        raw_frame = ttk.Frame(sub_nb)
        sub_nb.add(raw_frame, text="전체 필드 JSON (Raw Fields)")
        self.sub_nb = sub_nb
        self.txt_raw = scrolledtext.ScrolledText(raw_frame, wrap="none", font=("Consolas", 10), undo=True, maxundo=50)
        self.txt_raw.pack(fill="both", expand=True, padx=2, pady=2)
        bind_text_modified(self.txt_raw, lambda w: getattr(self.app, "mark_dirty", lambda *_: None)(w))
        ttk.Button(raw_frame, text="JSON 필드로 덮어쓰기 저장", command=self.save_raw_json_entry).pack(anchor="e", pady=3, padx=4)

        formula_outer, formula_inner = create_scrollable_container(sub_nb)
        sub_nb.add(formula_outer, text="⚔ 전투 공식")
        self.formula_panel = BattleFormulaPanel(formula_inner, self.app, on_applied=self._on_formula_applied)
        self.formula_panel.pack(fill="both", expand=True)

        # Right column: Batch Balance Fine-Tuner
        right = ttk.LabelFrame(paned, text="일괄 밸런스 튜너 (batch_tune_balance)", width=270)
        paned.add(right, weight=1)

        self.mult_vars = {
            "enemy_hp_mult": tk.DoubleVar(value=1.0),
            "enemy_atk_mult": tk.DoubleVar(value=1.0),
            "exp_mult": tk.DoubleVar(value=1.0),
            "gold_mult": tk.DoubleVar(value=1.0),
            "skill_power_mult": tk.DoubleVar(value=1.0),
        }
        labels = [
            ("enemy_hp_mult", "적 HP 배율"),
            ("enemy_atk_mult", "적 공격력(ATK) 배율"),
            ("exp_mult", "경험치(EXP) 배율"),
            ("gold_mult", "골드(Gold) 배율"),
            ("skill_power_mult", "스킬 위력 배율"),
        ]
        for key, title in labels:
            row = ttk.Frame(right)
            row.pack(fill="x", padx=6, pady=3)
            ttk.Label(row, text=title, width=18).pack(side="left")
            sp = ttk.Spinbox(row, from_=0.1, to=10.0, increment=0.1, textvariable=self.mult_vars[key], width=8)
            sp.pack(side="right")

        act_row = ttk.Frame(right)
        act_row.pack(fill="x", padx=6, pady=6)
        ttk.Button(act_row, text="미리보기 (Dry-Run)", command=lambda: self.run_batch_tune(dry_run=True)).pack(side="left", fill="x", expand=True, padx=2)
        ttk.Button(act_row, text="일괄 적용", command=lambda: self.run_batch_tune(dry_run=False)).pack(side="left", fill="x", expand=True, padx=2)
        ttk.Button(right, text="⚔ 즉시 전투 밸런스 검증", command=self._run_balance_check).pack(fill="x", padx=6, pady=(0, 6))

        self.txt_balance = scrolledtext.ScrolledText(right, height=16, font=("Consolas", 9))
        self.txt_balance.pack(fill="both", expand=True, padx=6, pady=4)

    def reload_entries(self, select_id: Optional[int] = None) -> None:
        if not self.app.ir_dir:
            return
        tbl = self.current_table.get()
        self.entry_list.delete(0, "end")
        self._entry_ids: List[int] = []

        if tbl == "system":
            self._entry_ids = [1]
            self.entry_list.insert("end", "1: System (전역 시스템)")
            self.entry_list.selection_set(0)
            self._load_entry(1)
            return

        if tbl in ("switches", "variables"):
            proj = load_json(self.app.ir_dir / "ir_project.json")
            arr = proj.get("system", {}).get("fields", {}).get(tbl, [])
            for idx in range(1, max(21, len(arr))):
                nm = str(arr[idx]) if idx < len(arr) else ""
                self._entry_ids.append(idx)
                self.entry_list.insert("end", f"{idx:03d}: {nm}")
        else:
            db_path = self.app.ir_dir / "database" / f"{tbl}.json"
            if db_path.is_file():
                rows = load_json(db_path)
                for idx in range(1, len(rows)):
                    row = rows[idx]
                    if isinstance(row, dict) and isinstance(row.get("fields"), dict):
                        nm = str(row["fields"].get("name", ""))
                        self._entry_ids.append(idx)
                        self.entry_list.insert("end", f"{idx:03d}: {nm}")

        if self._entry_ids:
            target = select_id if (select_id in self._entry_ids) else self._entry_ids[0]
            pos = self._entry_ids.index(target)
            self.entry_list.selection_set(pos)
            self.entry_list.see(pos)
            self._load_entry(target)
        else:
            self.lbl_entry.config(text=f"{tbl}: 항목 없음")
            self._clear_quick_form()
            self.txt_raw.delete("1.0", "end")

    def _on_select_entry(self) -> None:
        sel = self.entry_list.curselection()
        if sel and sel[0] < len(self._entry_ids):
            self._load_entry(self._entry_ids[sel[0]])

    def _clear_quick_form(self) -> None:
        for ch in self.quick_form.winfo_children():
            ch.destroy()
        self._quick_vars.clear()

    def _load_entry(self, entry_id: int) -> None:
        if not self.app.ir_dir:
            return
        tbl = self.current_table.get()
        self.current_entry_id = int(entry_id)
        data = get_db_entry(self.app.ir_dir, tbl, entry_id)
        self._clear_quick_form()

        if tbl in ("switches", "variables"):
            nm = "" if not isinstance(data, dict) else str(data.get("name") or "")
            self.lbl_entry.config(text=f"{tbl} #{entry_id}")
            var = tk.StringVar(value=nm)
            self._quick_vars["name"] = (var, "str")
            ttk.Label(self.quick_form, text="이름 (Label)", width=20).grid(row=0, column=0, sticky="w", padx=6, pady=4)
            ttk.Entry(self.quick_form, textvariable=var, width=36).grid(row=0, column=1, sticky="ew", padx=6, pady=4)
            reset_text_with_content(self.txt_raw, json.dumps({"name": nm}, ensure_ascii=False, indent=2))
            return

        fields = data.get("fields", {}) if isinstance(data, dict) else {}
        self.lbl_entry.config(text=f"{tbl} #{entry_id}: {fields.get('name', tbl)}")

        specs = _TABLE_QUICK_FIELDS.get(tbl, [("name", "이름", "str")])
        self.quick_form.columnconfigure(1, weight=1)
        for r_idx, (fk, label, kind) in enumerate(specs):
            val = fields.get(fk, "" if kind == "str" else (False if kind == "bool" else 0))
            ttk.Label(self.quick_form, text=f"{label} ({fk})", width=24).grid(row=r_idx, column=0, sticky="w", padx=6, pady=2)
            if kind == "bool":
                bvar = tk.BooleanVar(value=bool(val))
                self._quick_vars[fk] = (bvar, kind)
                ttk.Checkbutton(self.quick_form, variable=bvar).grid(row=r_idx, column=1, sticky="w", padx=6, pady=2)
            elif kind == "int":
                ivar = tk.StringVar(value=str(int(val) if isinstance(val, (int, float)) else 0))
                self._quick_vars[fk] = (ivar, kind)
                ttk.Entry(self.quick_form, textvariable=ivar, width=28).grid(row=r_idx, column=1, sticky="ew", padx=6, pady=2)
            else:
                svar = tk.StringVar(value=str(val if val is not None else ""))
                self._quick_vars[fk] = (svar, kind)
                ttk.Entry(self.quick_form, textvariable=svar, width=34).grid(row=r_idx, column=1, sticky="ew", padx=6, pady=2)

        if tbl == "troops":
            members = fields.get("members", [])
            eids = [str(m.get("fields", {}).get("enemy_id", 1)) for m in members if isinstance(m, dict)]
            mvar = tk.StringVar(value=",".join(eids))
            self._quick_vars["enemy_ids"] = (mvar, "int_list")
            r_idx = len(specs)
            ttk.Label(self.quick_form, text="적 ID 목록 (쉼표 구분)", width=24).grid(row=r_idx, column=0, sticky="w", padx=6, pady=4)
            ttk.Entry(self.quick_form, textvariable=mvar, width=34).grid(row=r_idx, column=1, sticky="ew", padx=6, pady=4)

        if tbl in ("skills", "items"):
            r_hint = len(specs)
            ttk.Label(
                self.quick_form,
                text="참고: 범위(scope)=0없음 1적1인 2적전체 3아군1인 4아군전체 7자신 | 시점(occasion)=0상시 1전투 2메뉴 3불가",
                foreground="#555",
            ).grid(row=r_hint, column=0, columnspan=2, sticky="w", padx=6, pady=4)

        reset_text_with_content(self.txt_raw, json.dumps(fields, ensure_ascii=False, indent=2))

    def save_current_entry(self) -> None:
        if not self.app.ir_dir:
            return
        tbl = self.current_table.get()
        updates: Dict[str, Any] = {}
        try:
            for fk, (var, kind) in self._quick_vars.items():
                if kind == "bool":
                    updates[fk] = bool(var.get())
                elif kind == "int":
                    updates[fk] = int(str(var.get()).strip() or "0")
                elif kind == "int_list":
                    raw = str(var.get()).strip()
                    updates[fk] = [int(x.strip()) for x in raw.split(",") if x.strip()]
                else:
                    updates[fk] = str(var.get())
            res = set_db_entry(self.app.ir_dir, tbl, self.current_entry_id, updates, record_undo=True)
            getattr(self.app, "clear_widget_dirty", lambda *_: None)(self.txt_raw)
            self.app.log_console(f"[DB 저장] {res}")
            self.app.reload_all(keep_db_id=self.current_entry_id)
        except Exception as exc:
            messagebox.showerror("DB 저장 오류", str(exc), parent=self)

    def save_raw_json_entry(self) -> bool:
        if not self.app.ir_dir:
            return False
        tbl = self.current_table.get()
        try:
            parsed = json.loads(self.txt_raw.get("1.0", "end"))
            if not isinstance(parsed, dict):
                raise ValueError("JSON object 형식이어야 합니다.")
            res = set_db_entry(self.app.ir_dir, tbl, self.current_entry_id, parsed, record_undo=True)
            getattr(self.app, "clear_widget_dirty", lambda *_: None)(self.txt_raw)
            self.app.log_console(f"[DB JSON 저장] {res}")
            self.app.reload_all(keep_db_id=self.current_entry_id)
            return True
        except Exception as exc:
            messagebox.showerror("JSON 저장 오류", str(exc), parent=self)
            return False

    def add_new_entry(self) -> None:
        if not self.app.ir_dir:
            return
        tbl = self.current_table.get()
        if tbl == "system":
            return
        new_id = (max(self._entry_ids) + 1) if self._entry_ids else 1
        set_db_entry(self.app.ir_dir, tbl, new_id, {"name": f"{tbl}_{new_id}"}, record_undo=True)
        self.app.reload_all(keep_db_id=new_id)

    def clone_entry(self) -> None:
        if not self.app.ir_dir:
            return
        tbl = self.current_table.get()
        if tbl in ("system", "switches", "variables") or not self._entry_ids:
            return
        cur = get_db_entry(self.app.ir_dir, tbl, self.current_entry_id)
        if not isinstance(cur, dict) or not isinstance(cur.get("fields"), dict):
            return
        new_id = max(self._entry_ids) + 1
        copied = dict(cur["fields"])
        copied["id"] = new_id
        copied["name"] = f"{copied.get('name', tbl)}_copy"
        set_db_entry(self.app.ir_dir, tbl, new_id, copied, record_undo=True)
        self.app.reload_all(keep_db_id=new_id)

    def delete_selected_entry(self) -> None:
        if not self.app.ir_dir:
            return
        tbl = self.current_table.get()
        if tbl == "system":
            return
        res = delete_db_entry(self.app.ir_dir, tbl, self.current_entry_id)
        self.app.log_console(f"[DB 삭제] {res}")
        self.app.reload_all()

    def _open_tileset_passages_dialog(self) -> None:
        if not self.app.ir_dir:
            return
        ts_id = self.current_entry_id if self.current_table.get() == "tilesets" else 1
        entry = get_db_entry(self.app.ir_dir, "tilesets", ts_id)
        if not isinstance(entry, dict):
            messagebox.showinfo("타일셋", "편집할 타일셋이 없습니다.", parent=self)
            return
        flds = entry.get("fields", {})
        passages = list(flds.get("passages", {}).get("data", []))
        priorities = list(flds.get("priorities", {}).get("data", []))

        win = tk.Toplevel(self)
        win.title(f"Tileset #{ts_id} 통행(passages) & 우선순위(priorities)")
        win.geometry("620x420")
        txt = scrolledtext.ScrolledText(win, font=("Consolas", 9))
        txt.pack(fill="both", expand=True, padx=6, pady=6)
        payload = {
            "note": "각 타일 ID별 passages 비트(0=통행가능, 15=불가)와 priorities(0..5)",
            "passages_from_384": passages[384:448],
            "priorities_from_384": priorities[384:448],
        }
        txt.insert("1.0", json.dumps(payload, ensure_ascii=False, indent=2))

        def _apply() -> None:
            try:
                upd = json.loads(txt.get("1.0", "end"))
                p_slice = [int(x) for x in upd.get("passages_from_384", [])]
                pr_slice = [int(x) for x in upd.get("priorities_from_384", [])]
                new_p = list(passages)
                new_pr = list(priorities)
                while len(new_p) < 384 + len(p_slice):
                    new_p.append(0)
                while len(new_pr) < 384 + len(pr_slice):
                    new_pr.append(0)
                for i, v in enumerate(p_slice):
                    new_p[384 + i] = v
                for i, v in enumerate(pr_slice):
                    new_pr[384 + i] = v
                p_tbl = dict(flds.get("passages", {"__type__": "Table", "dims": 1, "xsize": len(new_p), "ysize": 1, "zsize": 1}))
                pr_tbl = dict(flds.get("priorities", {"__type__": "Table", "dims": 1, "xsize": len(new_pr), "ysize": 1, "zsize": 1}))
                p_tbl["data"], p_tbl["xsize"] = new_p, len(new_p)
                pr_tbl["data"], pr_tbl["xsize"] = new_pr, len(new_pr)
                set_db_entry(self.app.ir_dir, "tilesets", ts_id, {"passages": p_tbl, "priorities": pr_tbl}, record_undo=True)
                self.app.log_console(f"[Tileset #{ts_id}] passages/priorities 저장 완료")
                self.app.reload_all()
                win.destroy()
            except Exception as exc:
                messagebox.showerror("저장 오류", str(exc), parent=win)

        ttk.Button(win, text="저장 및 적용", command=_apply).pack(pady=4)

    def run_batch_tune(self, dry_run: bool = False) -> None:
        if not self.app.ir_dir:
            return
        try:
            res = batch_tune_balance(
                self.app.ir_dir,
                enemy_hp_mult=float(self.mult_vars["enemy_hp_mult"].get()),
                enemy_atk_mult=float(self.mult_vars["enemy_atk_mult"].get()),
                exp_mult=float(self.mult_vars["exp_mult"].get()),
                gold_mult=float(self.mult_vars["gold_mult"].get()),
                skill_power_mult=float(self.mult_vars["skill_power_mult"].get()),
                dry_run=dry_run,
            )
            self.txt_balance.delete("1.0", "end")
            self.txt_balance.insert("1.0", json.dumps(res, ensure_ascii=False, indent=2))
            self.app.log_console(f"[일괄 밸런스] dry_run={dry_run} 완료")
            if not dry_run:
                self.app.reload_all(keep_db_id=self.current_entry_id)
        except Exception as exc:
            messagebox.showerror("밸런스 튜닝 오류", str(exc), parent=self)

    def _run_balance_check(self) -> None:
        if not self.app.ir_dir:
            return
        res = simulate_battle_balance(self.app.ir_dir)
        self.txt_balance.delete("1.0", "end")
        self.txt_balance.insert("1.0", json.dumps(res, ensure_ascii=False, indent=2))

    def reload_formula_panel(self) -> None:
        self.formula_panel.load_from_project()

    def _on_formula_applied(self) -> None:
        """Re-runs the balance check so the right-hand results reflect the newly applied formula."""
        self.app.reload_all(keep_db_id=self.current_entry_id)
        self._run_balance_check()


_PRESET_SHORT = {
    "rmxp": "XP",
    "simple": "단순(v0.1.0)",
    "rm2k": "2000",
    "rm2k3": "2003",
    "rmvx": "VX",
    "rmvxace": "VX Ace",
    "rmmv": "MV",
    "rmmz": "MZ",
}
_INT_DIV_CHOICES = ("프리셋 기본", "켜기 (Ruby 정수 나눗셈)", "끄기 (실수 나눗셈)")
_STAT_MODEL_CHOICES = ("프리셋 기본", "xp", "unified")


class BattleFormulaPanel(ttk.Frame):
    """Project-wide battle damage formula editor: preset picker, overrides, validation and preset comparison."""

    def __init__(self, master: tk.Misc, app: Any, on_applied: Optional[Callable[[], None]] = None):
        super().__init__(master)
        self.app = app
        self.on_applied = on_applied
        self._preset_rows = list_presets()
        self._label_to_id = {self._preset_label(r["id"]): r["id"] for r in self._preset_rows}
        self.preset_var = tk.StringVar(value=self._preset_label("rmxp"))
        self.attack_var = tk.StringVar()
        self.crit_var = tk.StringVar()
        self.attack_var_pct = tk.StringVar()
        self.skill_var_pct = tk.StringVar()
        self.guard_var = tk.StringVar()
        self.int_div_var = tk.StringVar(value=_INT_DIV_CHOICES[0])
        self.stat_model_var = tk.StringVar(value=_STAT_MODEL_CHOICES[0])
        self.cmp_enemy_var = tk.StringVar()
        self.cmp_skill_var = tk.StringVar()
        self._build_ui()
        self.load_from_project()

    @staticmethod
    def _preset_label(pid: str) -> str:
        return f"{pid} — {PRESETS[pid]['label']}"

    # -- UI -----------------------------------------------------------------
    def _build_ui(self) -> None:
        self.lbl_intro = ttk.Label(
            self,
            text="프로젝트 전체에 적용됩니다 (플레이어 · sim · balance-check 공통). 위의 '변경사항 저장'과는 별개로 아래 '적용' 버튼으로 저장합니다.",
            foreground="#666",
            justify="left",
        )
        self.lbl_intro.pack(anchor="w", padx=6, pady=(4, 0))
        self.lbl_saved = ttk.Label(self, text="저장된 설정: -", font=("", 9, "bold"))
        self.lbl_saved.pack(anchor="w", padx=6, pady=(2, 2))

        row = ttk.Frame(self)
        row.pack(fill="x", padx=6, pady=2)
        ttk.Label(row, text="프리셋", width=10).pack(side="left")
        self.cb_preset = ttk.Combobox(row, textvariable=self.preset_var, values=list(self._label_to_id), state="readonly", width=46)
        self.cb_preset.pack(side="left", fill="x", expand=True)
        self.cb_preset.bind("<<ComboboxSelected>>", lambda _e: self._on_preset_changed())

        self.lbl_desc = ttk.Label(self, text="", wraplength=560, justify="left", foreground="#444")
        self.lbl_desc.pack(fill="x", padx=6, pady=(2, 6))

        ov = ttk.LabelFrame(self, text="공식 덮어쓰기 (비워 두면 프리셋 기본값 사용)")
        ov.pack(fill="x", padx=6, pady=4)
        ttk.Label(ov, text="일반 공격 공식").grid(row=0, column=0, sticky="w", padx=4, pady=2)
        self.ent_attack = ttk.Entry(ov, textvariable=self.attack_var, font=("Consolas", 10))
        self.ent_attack.grid(row=0, column=1, columnspan=3, sticky="ew", padx=4, pady=2)
        self.lbl_attack_hint = ttk.Label(ov, text="", foreground="#666", font=("Consolas", 9))
        self.lbl_attack_hint.grid(row=1, column=1, columnspan=3, sticky="w", padx=4)

        ttk.Label(ov, text="스킬 공식").grid(row=2, column=0, sticky="nw", padx=4, pady=2)
        self.txt_skill = tk.Text(ov, height=4, font=("Consolas", 10), wrap="none", undo=True)
        self.txt_skill.grid(row=2, column=1, columnspan=3, sticky="ew", padx=4, pady=2)
        bind_text_modified(self.txt_skill, lambda w: getattr(self.app, "mark_dirty", lambda *_: None)(w))
        self.lbl_skill_hint = ttk.Label(ov, text="", foreground="#666", font=("Consolas", 9), justify="left")
        self.lbl_skill_hint.grid(row=3, column=1, columnspan=3, sticky="w", padx=4)

        opts = [
            ("치명타 배율", self.crit_var),
            ("일반 공격 분산 %", self.attack_var_pct),
            ("스킬 분산 (skill 또는 %)", self.skill_var_pct),
            ("방어 시 나누기", self.guard_var),
        ]
        for idx, (title, var) in enumerate(opts):
            r, c = 4 + idx // 2, (idx % 2) * 2
            ttk.Label(ov, text=title).grid(row=r, column=c, sticky="w", padx=4, pady=2)
            ttk.Entry(ov, textvariable=var, width=12).grid(row=r, column=c + 1, sticky="w", padx=4, pady=2)
        ttk.Label(ov, text="나눗셈").grid(row=6, column=0, sticky="w", padx=4, pady=2)
        ttk.Combobox(ov, textvariable=self.int_div_var, values=_INT_DIV_CHOICES, state="readonly", width=22).grid(row=6, column=1, sticky="w", padx=4)
        ttk.Label(ov, text="능력치 모델").grid(row=6, column=2, sticky="w", padx=4, pady=2)
        ttk.Combobox(ov, textvariable=self.stat_model_var, values=_STAT_MODEL_CHOICES, state="readonly", width=12).grid(row=6, column=3, sticky="w", padx=4)
        ov.columnconfigure(1, weight=1)
        ov.columnconfigure(3, weight=1)
        self.lbl_vars_help = ttk.Label(
            ov,
            text="변수: a=공격자, b=대상, s=스킬 · 능력치 atk str dex agi int pdef(def) mdef(mdf) mat hp mhp level · "
            "예: a.atk * 4 - b.def * 2, Math.max(1, a.atk - b.def / 2)",
            foreground="#666",
            wraplength=560,
            justify="left",
        )
        self.lbl_vars_help.grid(row=7, column=0, columnspan=4, sticky="w", padx=4, pady=(4, 2))

        btns = ttk.Frame(self)
        btns.pack(fill="x", padx=6, pady=4)
        ttk.Button(btns, text="검사 (저장 안 함)", command=self.validate_form).pack(side="left", padx=2)
        ttk.Button(btns, text="적용 (Ctrl+Z 지원)", command=self.apply_form).pack(side="left", padx=2)
        ttk.Button(btns, text="덮어쓰기 모두 지우기", command=self.clear_overrides).pack(side="left", padx=2)
        ttk.Button(btns, text="다시 불러오기", command=self.load_from_project).pack(side="left", padx=2)
        self.lbl_status = ttk.Label(self, text="", wraplength=560, justify="left")
        self.lbl_status.pack(fill="x", padx=6, pady=(0, 4))

        cmp_box = ttk.LabelFrame(self, text="프리셋별 예상 데미지 비교 (저장 안 함 · 원소/치명타/분산 적용 전 기본값)")
        cmp_box.pack(fill="both", expand=True, padx=6, pady=4)
        crow = ttk.Frame(cmp_box)
        crow.pack(fill="x", padx=4, pady=3)
        ttk.Label(crow, text="상대 적").pack(side="left")
        self.cb_cmp_enemy = ttk.Combobox(crow, textvariable=self.cmp_enemy_var, state="readonly", width=20)
        self.cb_cmp_enemy.pack(side="left", padx=4)
        ttk.Label(crow, text="주인공 스킬").pack(side="left")
        self.cb_cmp_skill = ttk.Combobox(crow, textvariable=self.cmp_skill_var, state="readonly", width=20)
        self.cb_cmp_skill.pack(side="left", padx=4)
        ttk.Button(crow, text="비교", command=self.run_comparison).pack(side="left", padx=4)
        cols = ("preset", "actor_attack", "actor_skill", "enemy_attack", "win_hits", "lose_hits")
        self.tree_cmp = ttk.Treeview(cmp_box, columns=cols, show="headings", height=10)
        heads = {
            "preset": ("공식", 150),
            "actor_attack": ("공격", 55),
            "actor_skill": ("스킬", 55),
            "enemy_attack": ("적 공격", 60),
            "win_hits": ("승리 타수", 70),
            "lose_hits": ("패배 타수", 70),
        }
        for col, (title, width) in heads.items():
            self.tree_cmp.heading(col, text=title)
            self.tree_cmp.column(col, width=width, anchor="w" if col == "preset" else "center")
        self.tree_cmp.pack(fill="both", expand=True, padx=4, pady=(0, 4))
        self.lbl_cmp_note = ttk.Label(cmp_box, text="", foreground="#666", wraplength=560, justify="left")
        self.lbl_cmp_note.pack(fill="x", padx=4, pady=(0, 4))
        self.bind("<Configure>", self._on_resize)

    def _on_resize(self, event: Any) -> None:
        """Keeps long descriptions readable in the narrow center column by wrapping to the panel width."""
        wrap = max(220, int(event.width) - 30)
        for lbl in (self.lbl_intro, self.lbl_desc, self.lbl_status, self.lbl_cmp_note, self.lbl_vars_help):
            lbl.configure(wraplength=wrap)

    # -- state <-> form ------------------------------------------------------
    def _set_status(self, text: str, ok: bool) -> None:
        self.lbl_status.configure(text=text, foreground="#1a7f37" if ok else "#c62828")

    def _current_preset_id(self) -> str:
        return self._label_to_id.get(self.preset_var.get(), "rmxp")

    def _on_preset_changed(self) -> None:
        pid = self._current_preset_id()
        cfg = PRESETS[pid]
        self.lbl_desc.configure(text=cfg["description"])
        self.lbl_attack_hint.configure(text=f"프리셋 기본: {cfg['attack']}")
        self.lbl_skill_hint.configure(text="프리셋 기본: " + str(cfg["skill"]).replace("\n", "\n              "))

    def load_from_project(self) -> None:
        """Fills the form from the project's stored battle_formula setting."""
        if not getattr(self.app, "ir_dir", None):
            return
        try:
            info = get_battle_formula(self.app.ir_dir)
        except Exception as exc:  # broken hand-edited setting
            self._set_status(f"저장된 설정을 읽을 수 없습니다: {exc}", ok=False)
            info = {"config": None, "effective": BattleFormula().describe()}
        cfg = info.get("config") or {}
        pid = info["effective"]["preset"] if not cfg else str(cfg.get("preset", "rmxp"))
        if pid not in PRESETS:
            pid = "rmxp"
        self.preset_var.set(self._preset_label(pid))
        self.attack_var.set(str(cfg.get("attack", "")))
        reset_text_with_content(self.txt_skill, str(cfg["skill"]) if cfg.get("skill") else "")
        self.crit_var.set("" if cfg.get("critical_multiplier") is None else str(cfg["critical_multiplier"]))
        self.attack_var_pct.set("" if cfg.get("attack_variance") is None else str(cfg["attack_variance"]))
        self.skill_var_pct.set("" if cfg.get("skill_variance") is None else str(cfg["skill_variance"]))
        self.guard_var.set("" if cfg.get("guard_divisor") is None else str(cfg["guard_divisor"]))
        if cfg.get("integer_division") is None:
            self.int_div_var.set(_INT_DIV_CHOICES[0])
        else:
            self.int_div_var.set(_INT_DIV_CHOICES[1] if cfg["integer_division"] else _INT_DIV_CHOICES[2])
        self.stat_model_var.set(str(cfg.get("stat_model") or _STAT_MODEL_CHOICES[0]))
        self._on_preset_changed()
        overrides = {k: v for k, v in cfg.items() if k != "preset"}
        saved = f"저장된 설정: {pid}" + (" (기본값 · ir_project.json에 설정 없음)" if not cfg else "")
        if overrides:
            saved += f" + 덮어쓰기 {len(overrides)}개 ({', '.join(overrides)})"
        self.lbl_saved.configure(text=saved)
        self._reload_comparison_choices()

    def clear_overrides(self) -> None:
        self.attack_var.set("")
        replace_text_undoable(self.txt_skill, "")
        for var in (self.crit_var, self.attack_var_pct, self.skill_var_pct, self.guard_var):
            var.set("")
        self.int_div_var.set(_INT_DIV_CHOICES[0])
        self.stat_model_var.set(_STAT_MODEL_CHOICES[0])
        getattr(self.app, "mark_dirty", lambda *_: None)(self.txt_skill)
        self._set_status("덮어쓰기를 지웠습니다. '적용'을 눌러야 저장됩니다.", ok=True)

    def form_config(self) -> Dict[str, Any]:
        """Builds a battle_formula config from the form; raises ValueError on malformed numbers."""
        cfg: Dict[str, Any] = {"preset": self._current_preset_id()}
        attack = self.attack_var.get().strip()
        if attack:
            cfg["attack"] = attack
        skill = self.txt_skill.get("1.0", "end").strip()
        if skill:
            cfg["skill"] = skill

        def _num(var: tk.StringVar, key: str, title: str, cast: Callable[[str], Any]) -> None:
            raw = var.get().strip()
            if not raw:
                return
            try:
                cfg[key] = cast(raw)
            except ValueError as exc:
                raise ValueError(f"{title} 값이 숫자가 아닙니다: {raw!r}") from exc

        _num(self.crit_var, "critical_multiplier", "치명타 배율", float)
        _num(self.attack_var_pct, "attack_variance", "일반 공격 분산", int)
        sv = self.skill_var_pct.get().strip()
        if sv:
            if sv.lower() == "skill":
                cfg["skill_variance"] = "skill"
            else:
                _num(self.skill_var_pct, "skill_variance", "스킬 분산", int)
        _num(self.guard_var, "guard_divisor", "방어 시 나누기", float)
        idv = self.int_div_var.get()
        if idv == _INT_DIV_CHOICES[1]:
            cfg["integer_division"] = True
        elif idv == _INT_DIV_CHOICES[2]:
            cfg["integer_division"] = False
        if self.stat_model_var.get() in ("xp", "unified"):
            cfg["stat_model"] = self.stat_model_var.get()
        return cfg

    def validate_form(self) -> Optional[Dict[str, Any]]:
        """Validates the form without saving. Returns the config when valid."""
        try:
            cfg = self.form_config()
            BattleFormula.from_config(cfg).validate()
        except (FormulaError, ValueError, TypeError) as exc:
            self._set_status(f"✗ 공식 오류: {exc}", ok=False)
            return None
        self._set_status("✓ 공식이 올바릅니다. (아직 저장되지 않음)", ok=True)
        return cfg

    def apply_form(self) -> bool:
        if not getattr(self.app, "ir_dir", None):
            return False
        cfg = self.validate_form()
        if cfg is None:
            messagebox.showerror("전투 공식 오류", self.lbl_status.cget("text"), parent=self)
            return False
        try:
            res = set_battle_formula(self.app.ir_dir, replace_config=cfg)
        except Exception as exc:
            self._set_status(f"✗ 저장 실패: {exc}", ok=False)
            messagebox.showerror("전투 공식 저장 오류", str(exc), parent=self)
            return False
        getattr(self.app, "clear_widget_dirty", lambda *_: None)(self.txt_skill)
        self.app.log_console(f"[전투 공식] {json.dumps(res['battle_formula'], ensure_ascii=False)} 적용 (Ctrl+Z로 되돌리기)")
        if self.on_applied is not None:
            self.on_applied()
        else:
            self.load_from_project()
        getattr(self.app, "sync_dirty_after_mutation", lambda: None)()
        self._set_status(f"✓ 적용했습니다: {res['battle_formula']['preset']} · 밸런스 검증 결과를 새 공식으로 다시 계산했습니다.", ok=True)
        return True

    # -- comparison ----------------------------------------------------------
    def _reload_comparison_choices(self) -> None:
        ir_dir = getattr(self.app, "ir_dir", None)
        if not ir_dir:
            return
        enemies: List[str] = []
        skills: List[str] = ["(없음)"]
        try:
            for e in load_json(Path(ir_dir) / "database" / "enemies.json"):
                if e:
                    enemies.append(f"{int(e['fields']['id'])}: {e['fields'].get('name', '')}")
            for sk in load_json(Path(ir_dir) / "database" / "skills.json"):
                if sk and int(sk["fields"].get("power", 0) or 0) > 0:
                    skills.append(f"{int(sk['fields']['id'])}: {sk['fields'].get('name', '')}")
        except (OSError, KeyError, ValueError):
            pass
        self.cb_cmp_enemy.configure(values=enemies)
        self.cb_cmp_skill.configure(values=skills)
        if enemies and self.cmp_enemy_var.get() not in enemies:
            self.cmp_enemy_var.set(enemies[-1])
        if self.cmp_skill_var.get() not in skills:
            self.cmp_skill_var.set(skills[1] if len(skills) > 1 else skills[0])

    @staticmethod
    def _choice_id(text: str) -> Optional[int]:
        head = text.split(":", 1)[0].strip()
        return int(head) if head.isdigit() else None

    def run_comparison(self) -> List[Dict[str, Any]]:
        """Fills the comparison table for every preset plus the unsaved form values."""
        for item in self.tree_cmp.get_children():
            self.tree_cmp.delete(item)
        ir_dir = getattr(self.app, "ir_dir", None)
        enemy_id = self._choice_id(self.cmp_enemy_var.get())
        if not ir_dir or enemy_id is None:
            self.lbl_cmp_note.configure(text="비교할 적을 선택하세요.")
            return []
        skill_id = self._choice_id(self.cmp_skill_var.get())
        try:
            res = calc_battle_damage(ir_dir, enemy_id=enemy_id, skill_id=skill_id, presets=list(PRESETS))
        except Exception as exc:
            self.lbl_cmp_note.configure(text=f"비교 실패: {exc}")
            return []
        saved_pid = BattleFormula.safe_from_project(load_json(Path(ir_dir) / "ir_project.json")).preset
        rows: List[Dict[str, Any]] = []
        form_cfg = None
        try:
            form_cfg = self.form_config()
            form_bf = BattleFormula.from_config(form_cfg)
            form_bf.validate()
        except (FormulaError, ValueError, TypeError):
            form_bf = None
        if form_bf is not None and form_cfg is not None and len(form_cfg) > 1:
            a_stats, e_stats = res["actor"]["stats"], res["enemy"]["stats"]
            skill_fields = None
            if skill_id:
                for sk in load_json(Path(ir_dir) / "database" / "skills.json"):
                    if sk and int(sk["fields"]["id"]) == skill_id:
                        skill_fields = sk["fields"]
            row = {
                "preset": f"입력값 (미저장 · {form_bf.preset})",
                "actor_attack": form_bf.attack_damage(a_stats, e_stats),
                "enemy_attack": form_bf.attack_damage(e_stats, a_stats),
            }
            if skill_fields is not None:
                row["actor_skill"] = form_bf.skill_damage(a_stats, e_stats, skill_fields)
            best_hero = max(row["actor_attack"], row.get("actor_skill", 0))
            row["actor_hits_to_defeat_enemy"] = -(-int(e_stats["mhp"]) // best_hero) if best_hero > 0 else None
            row["enemy_hits_to_defeat_actor"] = -(-int(a_stats["mhp"]) // row["enemy_attack"]) if row["enemy_attack"] > 0 else None
            rows.append(row)
        for r in res["results"]:
            r = dict(r)
            pid = r["preset"]
            r["preset_id"] = pid
            r["preset"] = ("★ " if pid == saved_pid else "") + f"{pid} · {_PRESET_SHORT.get(pid, pid)}"
            rows.append(r)
        for r in rows:
            self.tree_cmp.insert(
                "",
                "end",
                values=(
                    r["preset"],
                    r["actor_attack"],
                    r.get("actor_skill", "-"),
                    r["enemy_attack"],
                    r.get("actor_hits_to_defeat_enemy") or "-",
                    r.get("enemy_hits_to_defeat_actor") or "-",
                ),
            )
        self.lbl_cmp_note.configure(
            text=f"{res['actor']['name']} vs {res['enemy']['name']} · 공격/스킬 = 주인공이 주는 데미지 · ★ = 현재 저장된 프리셋 · "
            "타수는 가장 센 공격만 반복했을 때의 대략적인 값입니다."
        )
        return rows


class CommonAnimTab(ttk.Frame):
    """Common Events CRUD and Animation Editor with frame scrub preview."""

    def __init__(self, master: tk.Misc, app: Any):
        super().__init__(master)
        self.app = app
        self._anim_tk_ref: Any = None
        self._build_ui()

    def _build_ui(self) -> None:
        paned = create_preview_paned(self, orient="horizontal")
        paned.pack(fill="both", expand=True, padx=4, pady=4)

        # Left: Common Events
        ce_frame = ttk.LabelFrame(paned, text="커먼 이벤트 (Common Events)")
        paned.add(ce_frame, weight=1)

        top_ce = ttk.Frame(ce_frame)
        top_ce.pack(fill="x", padx=4, pady=4)
        self.ce_list = tk.Listbox(top_ce, height=7, exportselection=False)
        self.ce_list.pack(fill="x", expand=True)
        self.ce_list.bind("<<ListboxSelect>>", lambda _e: self._on_select_ce())

        ce_form = ttk.Frame(ce_frame)
        ce_form.pack(fill="x", padx=4, pady=2)
        self.ce_id_var = tk.IntVar(value=1)
        self.ce_name_var = tk.StringVar(value="New Common Event")
        self.ce_trig_var = tk.StringVar(value="none")
        self.ce_sw_var = tk.IntVar(value=1)

        ttk.Label(ce_form, text="ID").grid(row=0, column=0, sticky="w")
        ttk.Spinbox(ce_form, from_=1, to=999, textvariable=self.ce_id_var, width=6).grid(row=0, column=1, sticky="w", padx=3)
        ttk.Label(ce_form, text="이름").grid(row=0, column=2, sticky="w")
        ttk.Entry(ce_form, textvariable=self.ce_name_var, width=20).grid(row=0, column=3, sticky="ew", padx=3)
        ttk.Label(ce_form, text="트리거").grid(row=1, column=0, sticky="w", pady=2)
        ttk.Combobox(ce_form, textvariable=self.ce_trig_var, values=["none", "autorun", "parallel"], state="readonly", width=10).grid(row=1, column=1, sticky="w", padx=3)
        ttk.Label(ce_form, text="스위치 ID").grid(row=1, column=2, sticky="w")
        ttk.Spinbox(ce_form, from_=1, to=999, textvariable=self.ce_sw_var, width=8).grid(row=1, column=3, sticky="w", padx=3)
        ce_form.columnconfigure(3, weight=1)

        ce_ins_row = ttk.Frame(ce_frame)
        ce_ins_row.pack(fill="x", padx=4, pady=2)
        self.ce_preset_var = tk.StringVar(value="대사 (say)")
        ttk.Combobox(ce_ins_row, textvariable=self.ce_preset_var, values=list(DSL_TEMPLATES.keys()), state="readonly", width=22).pack(side="left")
        ttk.Button(ce_ins_row, text="+ DSL 삽입", command=self._insert_ce_dsl).pack(side="left", padx=4)
        ttk.Button(ce_ins_row, text="커먼 이벤트 저장", command=self.save_ce).pack(side="right")

        self.txt_ce_cmds = scrolledtext.ScrolledText(ce_frame, height=14, font=("Consolas", 9), undo=True, maxundo=50)
        self.txt_ce_cmds.pack(fill="both", expand=True, padx=4, pady=4)
        bind_text_modified(self.txt_ce_cmds, lambda w: getattr(self.app, "mark_dirty", lambda *_: None)(w))

        # Right: Animations
        anim_frame = ttk.LabelFrame(paned, text="애니메이션 에디터 (Animations)")
        paned.add(anim_frame, weight=1)

        self.anim_list = tk.Listbox(anim_frame, height=6, exportselection=False)
        self.anim_list.pack(fill="x", padx=4, pady=4)
        self.anim_list.bind("<<ListboxSelect>>", lambda _e: self._on_select_anim())

        a_top = ttk.Frame(anim_frame)
        a_top.pack(fill="x", padx=4)

        a_form = ttk.Frame(a_top)
        a_form.pack(side="left", fill="both", expand=True)
        self.anim_id_var = tk.IntVar(value=1)
        self.anim_name_var = tk.StringVar(value="Hit")
        self.anim_sheet_var = tk.StringVar(value="")
        self.anim_frames_var = tk.IntVar(value=6)
        self.anim_pos_var = tk.StringVar(value="center")
        self.anim_zoom_var = tk.IntVar(value=100)
        self.anim_se_var = tk.StringVar(value="")
        self.anim_flash_var = tk.StringVar(value="255,255,255,160")
        self.anim_pat_var = tk.IntVar(value=0)

        self.anim_sheet_cb = ttk.Combobox(a_form, textvariable=self.anim_sheet_var, width=16)
        rows = [
            ("ID", ttk.Spinbox(a_form, from_=1, to=999, textvariable=self.anim_id_var, width=8)),
            ("이름", ttk.Entry(a_form, textvariable=self.anim_name_var, width=18)),
            ("시트명 (Animations)", self.anim_sheet_cb),
            ("프레임 수", ttk.Spinbox(a_form, from_=1, to=120, textvariable=self.anim_frames_var, width=8)),
            ("위치", ttk.Combobox(a_form, textvariable=self.anim_pos_var, values=["top", "center", "bottom", "screen"], state="readonly", width=10)),
            ("배율(%)", ttk.Spinbox(a_form, from_=10, to=400, textvariable=self.anim_zoom_var, width=8)),
            ("SE", ttk.Entry(a_form, textvariable=self.anim_se_var, width=18)),
            ("플래시 RGBA", ttk.Entry(a_form, textvariable=self.anim_flash_var, width=18)),
        ]
        for idx, (lbl, widget) in enumerate(rows):
            ttk.Label(a_form, text=lbl, width=12).grid(row=idx // 2, column=(idx % 2) * 2, sticky="w", padx=2, pady=2)
            widget.grid(row=idx // 2, column=(idx % 2) * 2 + 1, sticky="ew", padx=2, pady=2)

        prev_box = ttk.Frame(a_top)
        prev_box.pack(side="right", padx=6)
        self.anim_canvas = tk.Canvas(prev_box, width=ANIM_PREVIEW_PX, height=ANIM_PREVIEW_PX, bg="#1c202c", highlightthickness=1)
        self.anim_canvas.pack()
        ctrl_a = ttk.Frame(prev_box)
        ctrl_a.pack(pady=2)
        sc = ttk.Spinbox(ctrl_a, from_=0, to=40, textvariable=self.anim_pat_var, width=5, command=self._refresh_anim_preview)
        sc.pack(side="left", padx=1)
        self.btn_anim_play = ttk.Button(ctrl_a, text="▶ 재생", width=6, command=self._toggle_anim_play)
        self.btn_anim_play.pack(side="left", padx=2)
        self.lbl_anim_frame = ttk.Label(prev_box, text="프레임 1", font=("", 8))
        self.lbl_anim_frame.pack()
        self._anim_playing: bool = False
        self._anim_after_id: Optional[str] = None
        self._anim_play_tick: int = 0

        btn_a = ttk.Frame(anim_frame)
        btn_a.pack(fill="x", padx=4, pady=2)
        ttk.Button(btn_a, text="미리보기 갱신", command=self._refresh_anim_preview).pack(side="left")
        ttk.Button(btn_a, text="⏹ 정지", command=self._stop_anim_play).pack(side="left", padx=4)
        ttk.Button(btn_a, text="애니메이션 저장", command=self.save_anim).pack(side="right")

        ttk.Label(anim_frame, text="Cells & Timings JSON (고급)").pack(anchor="w", padx=4)
        self.txt_anim_json = scrolledtext.ScrolledText(anim_frame, height=11, font=("Consolas", 9), undo=True, maxundo=50)
        self.txt_anim_json.pack(fill="both", expand=True, padx=4, pady=4)
        bind_text_modified(self.txt_anim_json, lambda w: getattr(self.app, "mark_dirty", lambda *_: None)(w))

    def reload_all(self) -> None:
        if not self.app.ir_dir:
            return
        self.ce_list.delete(0, "end")
        self._ce_ids: List[int] = []
        for item in list_common_events(self.app.ir_dir):
            self._ce_ids.append(int(item["id"]))
            self.ce_list.insert("end", f"{item['id']:03d}: {item['name']} ({item['trigger']})")
        if self._ce_ids:
            self.ce_list.selection_set(0)
            self._load_ce(self._ce_ids[0])

        self.anim_list.delete(0, "end")
        self._anim_ids: List[int] = []
        sheets: List[str] = [""]
        gdir = self.app.atlas.graphics_dir if self.app.atlas else None
        if gdir is not None and (Path(gdir) / "Animations").is_dir():
            sheets.extend(p.stem for p in sorted((Path(gdir) / "Animations").glob("*.png")))
        self.anim_sheet_cb.configure(values=sheets)
        for item in list_animations(self.app.ir_dir):
            self._anim_ids.append(int(item["id"]))
            self.anim_list.insert("end", f"{item['id']:03d}: {item['name']} ({item['frame_max']}f)")
        if self._anim_ids:
            self.anim_list.selection_set(0)
            self._load_anim(self._anim_ids[0])
        else:
            self._refresh_anim_preview()

    def _on_select_ce(self) -> None:
        sel = self.ce_list.curselection()
        if sel and sel[0] < len(self._ce_ids):
            self._load_ce(self._ce_ids[sel[0]])

    def _load_ce(self, ce_id: int) -> None:
        ce_path = self.app.ir_dir / "database" / "common_events.json"
        if not ce_path.is_file():
            return
        for row in load_json(ce_path):
            if isinstance(row, dict) and int(row.get("id", -1)) == int(ce_id):
                self.ce_id_var.set(int(row.get("id", 1)))
                self.ce_name_var.set(str(row.get("name", "")))
                self.ce_trig_var.set(str(row.get("trigger", "none")))
                self.ce_sw_var.set(int(row.get("switch_id", 1)))
                reset_text_with_content(self.txt_ce_cmds, json.dumps(row.get("commands", []), ensure_ascii=False, indent=2))
                return

    def _insert_ce_dsl(self) -> None:
        tpl = DSL_TEMPLATES.get(self.ce_preset_var.get())
        if not tpl:
            return
        try:
            cur = json.loads(self.txt_ce_cmds.get("1.0", "end").strip() or "[]")
            if not isinstance(cur, list):
                cur = []
        except json.JSONDecodeError:
            cur = []
        cur = [c for c in cur if c.get("op") != "end"]
        cur.append(dict(tpl))
        if cur[-1].get("op") != "end":
            cur.append({"op": "end", "indent": 0})
        replace_text_undoable(self.txt_ce_cmds, json.dumps(cur, ensure_ascii=False, indent=2))
        getattr(self.app, "mark_dirty", lambda *_: None)(self.txt_ce_cmds)

    def save_ce(self) -> bool:
        if not self.app.ir_dir:
            return False
        try:
            cmds = json.loads(self.txt_ce_cmds.get("1.0", "end").strip() or "[]")
            if not isinstance(cmds, list):
                raise ValueError("Commands JSON must be an array.")
            res = set_common_event(
                self.app.ir_dir,
                entry_id=int(self.ce_id_var.get()),
                name=self.ce_name_var.get(),
                trigger=self.ce_trig_var.get(),
                switch_id=int(self.ce_sw_var.get()),
                commands=cmds,
            )
            getattr(self.app, "clear_widget_dirty", lambda *_: None)(self.txt_ce_cmds)
            self.app.log_console(f"[커먼 이벤트 저장] {res}")
            self.app.reload_all()
            return True
        except Exception as exc:
            messagebox.showerror("커먼 이벤트 저장 오류", str(exc), parent=self)
            return False

    def _on_select_anim(self) -> None:
        sel = self.anim_list.curselection()
        if sel and sel[0] < len(self._anim_ids):
            self._load_anim(self._anim_ids[sel[0]])

    def _load_anim(self, anim_id: int) -> None:
        anim_path = self.app.ir_dir / "database" / "animations.json"
        if not anim_path.is_file():
            return
        rows = load_json(anim_path)
        entry = rows[anim_id] if 0 <= anim_id < len(rows) else None
        if not isinstance(entry, dict):
            return
        af = entry.get("fields", {})
        pos_names = {0: "top", 1: "center", 2: "bottom", 3: "screen"}
        self.anim_id_var.set(int(af.get("id", anim_id)))
        self.anim_name_var.set(str(af.get("name", "")))
        self.anim_sheet_var.set(str(af.get("animation_name", "")))
        self.anim_frames_var.set(int(af.get("frame_max", 6)))
        self.anim_pos_var.set(pos_names.get(int(af.get("position", 1)), "center"))
        timings = af.get("timings", [])
        se_name = ""
        flash_str = "255,255,255,160"
        if isinstance(timings, list) and timings:
            t0 = timings[0].get("fields", timings[0]) if isinstance(timings[0], dict) else {}
            se_f = t0.get("se", {})
            se_name = str(se_f.get("fields", se_f).get("name", "")) if isinstance(se_f, dict) else ""
            fc = t0.get("flash_color", {})
            rgba = fc.get("rgba", [255, 255, 255, 160]) if isinstance(fc, dict) else [255, 255, 255, 160]
            flash_str = ",".join(str(int(v)) for v in rgba)
        self.anim_se_var.set(se_name)
        self.anim_flash_var.set(flash_str)
        reset_text_with_content(self.txt_anim_json, json.dumps({"frames": af.get("frames", []), "timings": timings}, ensure_ascii=False, indent=2))
        self._refresh_anim_preview()

    def _refresh_anim_preview(self) -> None:
        if not self.app.atlas:
            return
        self.anim_canvas.delete("all")
        pat_idx = max(0, int(self.anim_pat_var.get()))
        tk_im = self.app.atlas.get_animation_frame_tk(self.anim_sheet_var.get(), pat_idx, size_px=ANIM_PREVIEW_PX - 32)
        self._anim_tk_ref = tk_im
        self.anim_canvas.create_image(ANIM_PREVIEW_PX // 2, ANIM_PREVIEW_PX // 2, image=tk_im)
        fmax = max(1, int(self.anim_frames_var.get()))
        self.lbl_anim_frame.config(text=f"패턴 #{pat_idx} · {min(pat_idx + 1, fmax)}/{fmax}f")

    def _toggle_anim_play(self) -> None:
        if self._anim_playing:
            self._stop_anim_play()
            return
        self._anim_playing = True
        self._anim_play_tick = 0
        self.btn_anim_play.config(text="⏸ 정지")
        self._step_anim_play()

    def _stop_anim_play(self) -> None:
        self._anim_playing = False
        if self._anim_after_id is not None:
            try:
                self.after_cancel(self._anim_after_id)
            except Exception:
                pass
            self._anim_after_id = None
        self.btn_anim_play.config(text="▶ 재생")

    def _current_anim_frames(self) -> List[Any]:
        """Frames from the JSON editor (so unsaved edits preview too); empty if the JSON is invalid."""
        try:
            parsed = json.loads(self.txt_anim_json.get("1.0", "end").strip() or "{}")
        except (ValueError, tk.TclError):
            return []
        frames = parsed.get("frames") if isinstance(parsed, dict) else None
        return frames if isinstance(frames, list) else []

    def render_anim_frame(self, frame_idx: int) -> int:
        """Draws one real animation frame (all cells composited). Returns the number of cells drawn."""
        if not self.app.atlas:
            return 0
        frames = self._current_anim_frames()
        cells = extract_animation_frame_cells({"fields": {"frames": frames}}, frame_idx)
        img = self.app.atlas.compose_animation_frame(self.anim_sheet_var.get(), cells, size_px=ANIM_PREVIEW_PX)
        tk_im = ImageTk.PhotoImage(img, master=self)
        self._anim_tk_ref = tk_im
        self.anim_canvas.delete("all")
        self.anim_canvas.create_image(ANIM_PREVIEW_PX // 2, ANIM_PREVIEW_PX // 2, image=tk_im)
        return len(cells)

    def _step_anim_play(self) -> None:
        if not self._anim_playing:
            return
        frames = self._current_anim_frames()
        if frames:
            fmax = len(frames)
            cur = self._anim_play_tick % fmax
            n_cells = self.render_anim_frame(cur)
            self.lbl_anim_frame.config(text=f"재생 {cur + 1}/{fmax}f · 셀 {n_cells}개")
        else:
            # No frame data (or invalid JSON): fall back to cycling sheet patterns.
            fmax = max(1, int(self.anim_frames_var.get()))
            cur = self._anim_play_tick % fmax
            self.anim_pat_var.set(cur)
            self._refresh_anim_preview()
            self.lbl_anim_frame.config(text=f"재생 {cur + 1}/{fmax}f (프레임 데이터 없음)")
        self._anim_play_tick += 1
        self._anim_after_id = self.after(ANIM_FRAME_MS, self._step_anim_play)

    def save_anim(self) -> bool:
        if not self.app.ir_dir:
            return False
        try:
            raw = self.txt_anim_json.get("1.0", "end").strip()
            parsed = json.loads(raw) if raw else {}
            flash = [int(x.strip()) for x in self.anim_flash_var.get().split(",") if x.strip()]
            pos_map = {"top": 0, "center": 1, "bottom": 2, "screen": 3}
            res = set_animation(
                self.app.ir_dir,
                anim_id=int(self.anim_id_var.get()),
                name=self.anim_name_var.get(),
                animation_name=self.anim_sheet_var.get(),
                frame_max=int(self.anim_frames_var.get()),
                position=pos_map.get(self.anim_pos_var.get(), 1),
                zoom=int(self.anim_zoom_var.get()),
                se_name=self.anim_se_var.get(),
                flash_color=flash if len(flash) == 4 else None,
                cells=parsed.get("cells") or parsed.get("frames"),
                timings=parsed.get("timings"),
            )
            getattr(self.app, "clear_widget_dirty", lambda *_: None)(self.txt_anim_json)
            self.app.log_console(f"[애니메이션 저장] {res}")
            self.app.reload_all()
            return True
        except Exception as exc:
            messagebox.showerror("애니메이션 저장 오류", str(exc), parent=self)
            return False


class OpsReleaseTab(ttk.Frame):
    """Project Validation, Headless Simulation/Fuzzing, i18n, Blueprint Diff & Release Bundler."""

    def __init__(self, master: tk.Misc, app: Any):
        super().__init__(master)
        self.app = app
        self._build_ui()

    def _build_ui(self) -> None:
        sub = ttk.Notebook(self)
        sub.pack(fill="both", expand=True, padx=4, pady=4)

        # 1) Validation, Graph & Map PNG Preview + Headless Simulator / Fuzz
        sim_tab = ttk.Frame(sub)
        sub.add(sim_tab, text="검증 · 그래프 · 시뮬레이션/퍼징")

        top_btns = ttk.Frame(sim_tab)
        top_btns.pack(fill="x", padx=6, pady=6)
        ttk.Button(top_btns, text="프로젝트 무결성 (check)", command=self._run_check).pack(side="left", padx=2)
        ttk.Button(top_btns, text="에셋 검사 (check-assets)", command=self._run_check_assets).pack(side="left", padx=2)
        ttk.Button(top_btns, text="전투 밸런스 (balance-check)", command=self._run_balance).pack(side="left", padx=2)
        ttk.Button(top_btns, text="흐름도 Mermaid", command=lambda: self._run_graph("mermaid")).pack(side="left", padx=2)
        ttk.Button(top_btns, text="흐름도 DOT", command=lambda: self._run_graph("dot")).pack(side="left", padx=2)
        ttk.Button(top_btns, text="현재 맵 PNG 프리뷰 저장", command=self._export_map_png).pack(side="left", padx=2)

        sim_bar = ttk.LabelFrame(sim_tab, text="헤드리스 시뮬레이션 & 퍼징 (sim)")
        sim_bar.pack(fill="x", padx=6, pady=4)
        self.fuzz_steps_var = tk.IntVar(value=120)
        self.fuzz_seed_var = tk.IntVar(value=1)
        self.sim_actions_var = tk.StringVar(value='[{"op": "move", "direction": "down"}, {"op": "interact"}]')
        ttk.Label(sim_bar, text="Fuzz Steps").pack(side="left", padx=4)
        ttk.Spinbox(sim_bar, from_=1, to=5000, textvariable=self.fuzz_steps_var, width=7).pack(side="left")
        ttk.Label(sim_bar, text="Seed").pack(side="left", padx=4)
        ttk.Spinbox(sim_bar, from_=0, to=99999, textvariable=self.fuzz_seed_var, width=7).pack(side="left")
        ttk.Button(sim_bar, text="▶ 퍼징 실행", command=self._run_fuzz).pack(side="left", padx=6)
        ttk.Label(sim_bar, text="Actions JSON").pack(side="left", padx=4)
        ttk.Entry(sim_bar, textvariable=self.sim_actions_var, width=36).pack(side="left", fill="x", expand=True, padx=4)
        ttk.Button(sim_bar, text="▶ 액션 시뮬", command=self._run_actions_sim).pack(side="left", padx=4)

        self.lbl_ops_summary = ttk.Label(
            sim_tab,
            text="준비됨 — 상단 버튼을 눌러 무결성·에셋·밸런스 검사 또는 퍼징을 실행하세요.",
            font=("", 9, "bold"),
            foreground="#1f6feb",
        )
        self.lbl_ops_summary.pack(fill="x", padx=8, pady=(4, 0))

        self.txt_ops_out = scrolledtext.ScrolledText(sim_tab, font=("Consolas", 9))
        self.txt_ops_out.pack(fill="both", expand=True, padx=6, pady=6)

        # 2) i18n Manager
        i18n_tab = ttk.Frame(sub)
        sub.add(i18n_tab, text="다국어 (i18n) 추출 & 적용")
        i_bar = ttk.Frame(i18n_tab)
        i_bar.pack(fill="x", padx=6, pady=6)
        self.i18n_locale_var = tk.StringVar(value="ko")
        ttk.Button(i_bar, text="문자열 추출 (extract)", command=self._i18n_extract).pack(side="left", padx=2)
        ttk.Label(i_bar, text="적용 로케일").pack(side="left", padx=(10, 4))
        ttk.Entry(i_bar, textvariable=self.i18n_locale_var, width=8).pack(side="left")
        ttk.Button(i_bar, text="편집본 저장 후 일괄 적용 (apply)", command=self._i18n_apply).pack(side="left", padx=6)
        self.txt_i18n = scrolledtext.ScrolledText(i18n_tab, font=("Consolas", 9), undo=True, maxundo=50)
        self.txt_i18n.pack(fill="both", expand=True, padx=6, pady=4)
        bind_text_modified(self.txt_i18n, lambda w: getattr(self.app, "mark_dirty", lambda *_: None)(w))

        # 3) Blueprint JSON Dry-Run / Apply
        bp_tab = ttk.Frame(sub)
        sub.add(bp_tab, text="블루프린트 JSON 적용")
        bp_bar = ttk.Frame(bp_tab)
        bp_bar.pack(fill="x", padx=6, pady=6)
        ttk.Button(bp_bar, text="샘플 템플릿 넣기", command=self._bp_sample).pack(side="left", padx=2)
        ttk.Button(bp_bar, text="파일 불러오기", command=self._bp_load_file).pack(side="left", padx=2)
        ttk.Button(bp_bar, text="Dry-Run Diff 미리보기", command=lambda: self._bp_run(dry_run=True)).pack(side="left", padx=4)
        ttk.Button(bp_bar, text="원자적 커밋 적용", command=lambda: self._bp_run(dry_run=False)).pack(side="right", padx=2)
        self.txt_bp = scrolledtext.ScrolledText(bp_tab, font=("Consolas", 9), undo=True, maxundo=50)
        self.txt_bp.pack(fill="both", expand=True, padx=6, pady=4)
        bind_text_modified(self.txt_bp, lambda w: getattr(self.app, "mark_dirty", lambda *_: None)(w))

        # 4) Commercial Release Bundler
        rel_tab = ttk.Frame(sub)
        sub.add(rel_tab, text="상용 릴리즈 번들 (export-release)")
        rel_form = ttk.Frame(rel_tab)
        rel_form.pack(fill="x", padx=10, pady=10)

        self.rel_out_var = tk.StringVar(value="")
        self.rel_lang_var = tk.StringVar(value="")
        self.rel_use_rgssad = tk.BooleanVar(value=True)
        self.rel_rgssad_ver = tk.IntVar(value=3)
        self.rel_rgssad_key = tk.StringVar(value="0xDEADCAFE")
        self.rel_launcher = tk.BooleanVar(value=True)
        self.rel_zip = tk.BooleanVar(value=False)
        self.rel_strict = tk.BooleanVar(value=False)
        self.rel_strict_save = tk.BooleanVar(value=False)
        self.rel_build_exe = tk.BooleanVar(value=False)
        self.rel_icon_var = tk.StringVar(value="")

        ttk.Label(rel_form, text="출력 폴더").grid(row=0, column=0, sticky="w", pady=3)
        ttk.Entry(rel_form, textvariable=self.rel_out_var, width=48).grid(row=0, column=1, sticky="ew", padx=4)
        ttk.Button(rel_form, text="찾기...", command=lambda: self._pick_dir(self.rel_out_var)).grid(row=0, column=2)

        ttk.Label(rel_form, text="로케일 (선택)").grid(row=1, column=0, sticky="w", pady=3)
        ttk.Entry(rel_form, textvariable=self.rel_lang_var, width=12).grid(row=1, column=1, sticky="w", padx=4)

        ttk.Label(rel_form, text="RGSSAD Key / 버전").grid(row=2, column=0, sticky="w", pady=3)
        r_row = ttk.Frame(rel_form)
        r_row.grid(row=2, column=1, sticky="w", padx=4)
        ttk.Checkbutton(r_row, text="RGSSAD 아카이빙", variable=self.rel_use_rgssad).pack(side="left")
        ttk.Combobox(r_row, textvariable=self.rel_rgssad_ver, values=[1, 2, 3], width=4, state="readonly").pack(side="left", padx=6)
        ttk.Entry(r_row, textvariable=self.rel_rgssad_key, width=14).pack(side="left")

        ttk.Label(rel_form, text="아이콘 (.ico)").grid(row=3, column=0, sticky="w", pady=3)
        ttk.Entry(rel_form, textvariable=self.rel_icon_var, width=48).grid(row=3, column=1, sticky="ew", padx=4)
        ttk.Button(rel_form, text="찾기...", command=self._pick_icon).grid(row=3, column=2)

        flags = ttk.Frame(rel_form)
        flags.grid(row=4, column=1, sticky="w", pady=6)
        ttk.Checkbutton(flags, text="런처 생성 (--launcher)", variable=self.rel_launcher).pack(side="left", padx=4)
        ttk.Checkbutton(flags, text="ZIP 압축 (--zip)", variable=self.rel_zip).pack(side="left", padx=4)
        ttk.Checkbutton(flags, text="엄격 에셋 (--strict-assets)", variable=self.rel_strict).pack(side="left", padx=4)
        ttk.Checkbutton(flags, text="세이브 변조 차단 (--strict-save)", variable=self.rel_strict_save).pack(side="left", padx=4)
        ttk.Checkbutton(flags, text="EXE 빌드 (--build-exe)", variable=self.rel_build_exe).pack(side="left", padx=4)

        rel_btns = ttk.Frame(rel_form)
        rel_btns.grid(row=5, column=1, sticky="w", pady=8)
        ttk.Button(rel_btns, text="🚀 상용 릴리즈 번들 빌드 실행", command=self._run_release).pack(side="left")
        self._last_release_dir: Optional[Path] = None
        self.btn_open_release = ttk.Button(rel_btns, text="📁 결과 폴더 열기", command=self._open_release_dir, state="disabled")
        self.btn_open_release.pack(side="left", padx=8)
        rel_form.columnconfigure(1, weight=1)

        self.txt_rel_out = scrolledtext.ScrolledText(rel_tab, height=12, font=("Consolas", 9))
        self.txt_rel_out.pack(fill="both", expand=True, padx=10, pady=4)

    def _emit(self, data: Any) -> None:
        if isinstance(data, dict):
            if "passed" in data and "errors" in data and isinstance(data.get("stats"), dict):
                ok = bool(data.get("passed"))
                status = "통과" if ok else f"실패 (오류 {len(data.get('errors', []))}건)"
                st = data.get("stats", {})
                self.lbl_ops_summary.config(
                    text=f"[무결성 검사 {status}] 맵 {st.get('maps', len(data.get('maps', [])))}개 · 이벤트 {st.get('events', 0)}개 · 오류 {len(data.get('errors', []))}건 · 경고 {len(data.get('warnings', []))}건",
                    foreground="#2ea043" if ok else "#d1242f",
                )
            elif "passed" in data and "missing_assets" in data:
                ok = bool(data.get("passed"))
                self.lbl_ops_summary.config(
                    text=f"[에셋 검사 {'통과' if ok else '누락 발견'}] 누락 {len(data.get('missing_assets', []))}건",
                    foreground="#2ea043" if ok else "#d1242f",
                )
            elif "steps" in data and "maps_visited" in data:
                self.lbl_ops_summary.config(
                    text=f"[퍼징 시뮬레이션 완료] {data.get('steps')}스텝 · 방문 맵 {len(data.get('maps_visited', []))}개 · 전투 {data.get('battles_fought', 0)}회 · 오류 {len(data.get('errors', []))}건",
                    foreground="#1f6feb",
                )
            elif "enemies" in data and "troops" in data:
                self.lbl_ops_summary.config(
                    text=f"[전투 밸런스 분석] 적 {len(data.get('enemies', []))}종 · 트룹 {len(data.get('troops', []))}개",
                    foreground="#1f6feb",
                )
            elif "steps_executed" in data:
                self.lbl_ops_summary.config(
                    text=f"[액션 시뮬레이션] {data.get('steps_executed')}스텝 · 완료={not data.get('errors')}",
                    foreground="#1f6feb",
                )
        elif isinstance(data, str):
            first_ln = data.splitlines()[0] if data.splitlines() else "결과 출력"
            self.lbl_ops_summary.config(text=f"[출력 완료] {first_ln[:80]}", foreground="#1f6feb")
        self.txt_ops_out.delete("1.0", "end")
        txt = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False, indent=2)
        self.txt_ops_out.insert("1.0", txt)

    def _run_check(self) -> None:
        if self.app.ir_dir:
            self._emit(validate_ir_project(self.app.ir_dir).to_dict())

    def _run_check_assets(self) -> None:
        if self.app.ir_dir:
            gdir = self.app.atlas.graphics_dir if self.app.atlas else None
            self._emit(validate_project_assets(self.app.ir_dir, graphics_dir=gdir))

    def _run_balance(self) -> None:
        if self.app.ir_dir:
            self._emit(simulate_battle_balance(self.app.ir_dir))

    def _run_graph(self, fmt: str) -> None:
        if self.app.ir_dir:
            self._emit(export_flow_graph(self.app.ir_dir, fmt=fmt))

    def _export_map_png(self) -> None:
        if not self.app.ir_dir:
            return
        mid = self.app.get_active_map_id()
        out = filedialog.asksaveasfilename(
            parent=self,
            defaultextension=".png",
            initialfile=f"map_{mid:03d}_preview.png",
            filetypes=[("PNG", "*.png")],
        )
        if not out:
            return
        gdir = self.app.atlas.graphics_dir if self.app.atlas else None
        ascii_map = render_map_preview(self.app.ir_dir, mid, png_out=Path(out), graphics_dir=gdir)
        self._emit(f"Saved PNG: {out}\n\n{ascii_map}")

    def _run_fuzz(self) -> None:
        if self.app.ir_dir:
            res = run_fuzz_playtest(self.app.ir_dir, steps=int(self.fuzz_steps_var.get()), seed=int(self.fuzz_seed_var.get()))
            self._emit(res)

    def _run_actions_sim(self) -> None:
        if not self.app.ir_dir:
            return
        try:
            actions = json.loads(self.sim_actions_var.get())
            self._emit(run_headless_simulation(self.app.ir_dir, actions))
        except Exception as exc:
            messagebox.showerror("시뮬레이션 오류", str(exc), parent=self)

    def _i18n_extract(self) -> None:
        if not self.app.ir_dir:
            return
        res = extract_i18n_strings(self.app.ir_dir, default_locale=self.i18n_locale_var.get() or "ko")
        msg_path = Path(res["catalog_path"])
        reset_text_with_content(self.txt_i18n, msg_path.read_text(encoding="utf-8"))
        self.app.log_console(f"[i18n 추출] {res}")

    def _i18n_apply(self) -> None:
        if not self.app.ir_dir:
            return
        try:
            raw = self.txt_i18n.get("1.0", "end").strip()
            msg_path = self.app.ir_dir / "locale" / "messages.json"
            if raw:
                parsed = json.loads(raw)
                msg_path.parent.mkdir(parents=True, exist_ok=True)
                msg_path.write_text(json.dumps(parsed, ensure_ascii=False, indent=2), encoding="utf-8")
            snap = capture_undo_snapshot(self.app.ir_dir)
            res = apply_i18n_locale(self.app.ir_dir, locale=self.i18n_locale_var.get() or "ko")
            push_undo_snapshot(self.app.ir_dir, "gui:i18n_apply", snap)
            getattr(self.app, "clear_widget_dirty", lambda *_: None)(self.txt_i18n)
            self.app.log_console(f"[i18n 적용] {res}")
            self.app.reload_all()
        except Exception as exc:
            messagebox.showerror("i18n 오류", str(exc), parent=self)

    def _bp_sample(self) -> None:
        sample = {
            "db": [{"table": "enemies", "id": 1, "fields": {"maxhp": 220, "exp": 25}}],
            "patch_maps": [{"map_id": 1, "paint": [{"layer": 0, "tile_id": 385, "rect": [2, 2, 3, 3]}]}],
        }
        replace_text_undoable(self.txt_bp, json.dumps(sample, ensure_ascii=False, indent=2))
        getattr(self.app, "mark_dirty", lambda *_: None)(self.txt_bp)

    def _bp_load_file(self) -> None:
        p = filedialog.askopenfilename(parent=self, filetypes=[("JSON", "*.json")])
        if p:
            reset_text_with_content(self.txt_bp, Path(p).read_text(encoding="utf-8"))

    def _bp_run(self, dry_run: bool) -> None:
        if not self.app.ir_dir:
            return
        try:
            bp = json.loads(self.txt_bp.get("1.0", "end"))
            res = apply_blueprint(self.app.ir_dir, bp, validate=True, dry_run=dry_run)
            self.app.log_console(f"[블루프린트 {'미리보기' if dry_run else '적용'}] {json.dumps(res, ensure_ascii=False)}")
            if not dry_run:
                getattr(self.app, "clear_widget_dirty", lambda *_: None)(self.txt_bp)
                self.app.reload_all()
        except Exception as exc:
            messagebox.showerror("블루프린트 오류", str(exc), parent=self)

    def _pick_dir(self, var: tk.StringVar) -> None:
        d = filedialog.askdirectory(parent=self)
        if d:
            var.set(d)

    def _pick_icon(self) -> None:
        f = filedialog.askopenfilename(parent=self, filetypes=[("Icon", "*.ico"), ("All", "*.*")])
        if f:
            self.rel_icon_var.set(f)

    def _open_release_dir(self) -> None:
        if not self._last_release_dir or not self._last_release_dir.exists():
            return
        try:
            if hasattr(os, "startfile"):
                os.startfile(str(self._last_release_dir))
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(self._last_release_dir)])
            else:
                subprocess.Popen(["xdg-open", str(self._last_release_dir)])
        except Exception as exc:
            messagebox.showerror("폴더 열기 오류", str(exc), parent=self)

    def _run_release(self) -> None:
        if not self.app.ir_dir:
            return
        out_str = self.rel_out_var.get().strip()
        if not out_str:
            out_str = str(self.app.ir_dir.parent / "release_bundle")
            self.rel_out_var.set(out_str)
        try:
            key_s = self.rel_rgssad_key.get().strip()
            key_int = int(key_s, 0) if key_s else None
            icon_s = self.rel_icon_var.get().strip()
            res = export_release_bundle(
                self.app.ir_dir,
                Path(out_str),
                use_rgssad=bool(self.rel_use_rgssad.get()),
                lang=self.rel_lang_var.get().strip() or None,
                launcher=bool(self.rel_launcher.get()),
                rgssad_key=key_int,
                build_exe=bool(self.rel_build_exe.get()),
                strict_assets=bool(self.rel_strict.get()),
                rgssad_version=int(self.rel_rgssad_ver.get()),
                icon_path=Path(icon_s) if icon_s else None,
                create_zip=bool(self.rel_zip.get()),
                strict_save=bool(self.rel_strict_save.get()),
            )
            self._last_release_dir = Path(res["output_dir"])
            self.btn_open_release.configure(state="normal")
            self.txt_rel_out.delete("1.0", "end")
            self.txt_rel_out.insert("1.0", json.dumps(res, ensure_ascii=False, indent=2))
            self.app.log_console(f"[상용 릴리즈 완료] {res['output_dir']}")
            messagebox.showinfo(
                "릴리즈 빌드 완료",
                f"상용 릴리즈 번들이 생성되었습니다!\n\n위치: {res['output_dir']}\n파일 수: {res.get('file_count', 0)}",
                parent=self,
            )
        except Exception as exc:
            messagebox.showerror("릴리즈 빌드 오류", str(exc), parent=self)


ASSET_CATEGORIES: Tuple[str, ...] = (
    "Graphics/Tilesets",
    "Graphics/Autotiles",
    "Graphics/Characters",
    "Graphics/Faces",
    "Graphics/Battlers",
    "Graphics/Battlebacks",
    "Graphics/Titles",
    "Graphics/Gameovers",
    "Graphics/Animations",
    "Graphics/Panoramas",
    "Graphics/Fogs",
    "Graphics/Windowskins",
    "Audio/BGM",
    "Audio/BGS",
    "Audio/SE",
    "Audio/ME",
    "Fonts",
)


class AssetBrowserTab(ttk.Frame):
    """Workspace Graphics, Audio, and Fonts browser with live image/audio preview and file import."""

    def __init__(self, master: tk.Misc, app: Any):
        super().__init__(master)
        self.app = app
        self.cat_var = tk.StringVar(value=ASSET_CATEGORIES[0])
        self._files: List[Path] = []
        self._preview_tk_ref: Optional[ImageTk.PhotoImage] = None
        self._mixer_ready: bool = False
        self._build_ui()

    def _build_ui(self) -> None:
        bar = ttk.Frame(self)
        bar.pack(fill="x", padx=8, pady=6)
        ttk.Label(bar, text="카테고리:").pack(side="left", padx=(0, 4))
        cb = ttk.Combobox(bar, textvariable=self.cat_var, values=list(ASSET_CATEGORIES), state="readonly", width=24)
        cb.pack(side="left", padx=4)
        cb.bind("<<ComboboxSelected>>", lambda _e: self.reload_files())
        ttk.Button(bar, text="🔄 목록 새로고침", command=self.reload_files).pack(side="left", padx=4)
        ttk.Button(bar, text="📥 에셋 파일 가져오기...", command=self._on_import_clicked).pack(side="left", padx=4)
        ttk.Button(bar, text="🗑 선택 파일 삭제", command=self._on_delete_clicked).pack(side="left", padx=4)

        body = create_preview_paned(self, orient="horizontal")
        body.pack(fill="both", expand=True, padx=8, pady=(0, 8))

        left = ttk.Frame(body)
        body.add(left, weight=1)
        ttk.Label(left, text="발견된 파일 목록", font=("", 9, "bold")).pack(anchor="w")
        self.file_list = tk.Listbox(left, font=("Consolas", 9), exportselection=False)
        self.file_list.pack(fill="both", expand=True, pady=(4, 0))
        self.file_list.bind("<<ListboxSelect>>", lambda _e: self._on_select_file())

        right = ttk.LabelFrame(body, text="에셋 미리보기 · 오디오 재생")
        body.add(right, weight=2)
        self.lbl_asset_info = ttk.Label(right, text="왼쪽 목록에서 파일을 선택하세요.", wraplength=540, justify="left")
        self.lbl_asset_info.pack(anchor="w", padx=8, pady=6)

        aud_bar = ttk.Frame(right)
        aud_bar.pack(fill="x", padx=8, pady=(0, 6))
        self.btn_play_audio = ttk.Button(aud_bar, text="▶ 오디오 미리듣기", command=self.play_selected_audio)
        self.btn_play_audio.pack(side="left", padx=(0, 6))
        ttk.Button(aud_bar, text="⏹ 재생 정지", command=self.stop_audio).pack(side="left")

        self.preview_canvas = tk.Canvas(right, bg="#181c26", highlightthickness=1, highlightbackground="#364152")
        self.preview_canvas.pack(fill="both", expand=True, padx=8, pady=(0, 8))

    def _search_roots(self) -> List[Path]:
        if not self.app.ir_dir:
            return []
        roots: List[Path] = [self.app.ir_dir / "assets", self.app.ir_dir]
        if self.app.atlas and self.app.atlas.graphics_dir:
            gdir = Path(self.app.atlas.graphics_dir)
            roots.append(gdir.parent if gdir.name.lower() == "graphics" else gdir)
        roots.extend([self.app.ir_dir.parent / "rmxp", self.app.ir_dir.parent])
        dedup: List[Path] = []
        seen: set[str] = set()
        for r in roots:
            k = str(r.resolve()) if r.exists() else str(r)
            if k not in seen:
                seen.add(k)
                dedup.append(r)
        return dedup

    def list_category_files(self, category: Optional[str] = None) -> List[Path]:
        cat = category or self.cat_var.get()
        found: List[Path] = []
        seen_names: set[str] = set()
        for root in self._search_roots():
            target = root / cat
            if not target.is_dir():
                continue
            for p in sorted(target.iterdir()):
                if p.is_file() and not p.name.startswith(".") and p.name not in seen_names:
                    seen_names.add(p.name)
                    found.append(p)
        return found

    def reload_all(self) -> None:
        self.reload_files()

    def reload_files(self) -> None:
        self._files = self.list_category_files()
        self.file_list.delete(0, "end")
        for p in self._files:
            try:
                sz_kb = max(1, (p.stat().st_size + 1023) // 1024)
                self.file_list.insert("end", f"{p.name}  ({sz_kb} KB)")
            except OSError:
                self.file_list.insert("end", p.name)
        if self._files:
            self.file_list.selection_set(0)
            self._on_select_file()
        else:
            self.preview_canvas.delete("all")
            self._preview_tk_ref = None
            self.lbl_asset_info.config(text=f"카테고리 '{self.cat_var.get()}'에 등록된 파일이 없습니다.")

    def _selected_path(self) -> Optional[Path]:
        sel = self.file_list.curselection()
        if not sel or sel[0] >= len(self._files):
            return None
        return self._files[sel[0]]

    def _on_select_file(self) -> None:
        p = self._selected_path()
        self.preview_canvas.delete("all")
        self._preview_tk_ref = None
        if p is None or not p.is_file():
            return
        ext = p.suffix.lower()
        size_kb = max(1, (p.stat().st_size + 1023) // 1024)
        if ext in (".png", ".jpg", ".jpeg", ".bmp", ".gif"):
            try:
                with Image.open(p) as im_raw:
                    im = im_raw.convert("RGBA")
                w, h = im.size
                cw = max(240, int(self.preview_canvas.winfo_width() or 460) - 16)
                ch = max(200, int(self.preview_canvas.winfo_height() or 320) - 16)
                scale = min(cw / max(1, w), ch / max(1, h), 4.0)
                nw, nh = max(1, int(round(w * scale))), max(1, int(round(h * scale)))
                resized = im.resize((nw, nh), Image.Resampling.NEAREST if scale >= 2.0 else Image.Resampling.LANCZOS)
                tk_im = ImageTk.PhotoImage(resized, master=self)
                self._preview_tk_ref = tk_im
                self.preview_canvas.create_image(cw // 2 + 8, ch // 2 + 8, image=tk_im, anchor="center")
                self.lbl_asset_info.config(text=f"이미지: {p.name} ({w}x{h}px, {size_kb} KB)\n경로: {p}")
            except Exception as exc:
                self.lbl_asset_info.config(text=f"이미지 열기 실패 ({p.name}): {exc}")
        elif ext in (".ogg", ".wav", ".mp3", ".mid", ".midi"):
            self.preview_canvas.create_text(
                220, 140,
                text=f"♪ 오디오 파일: {p.name}\n상단 '▶ 오디오 미리듣기' 버튼으로 재생할 수 있습니다.",
                fill="#cbd5e1", font=("", 10), justify="center",
            )
            self.lbl_asset_info.config(text=f"오디오: {p.name} ({size_kb} KB)\n경로: {p}")
        else:
            self.lbl_asset_info.config(text=f"파일: {p.name} ({size_kb} KB)\n경로: {p}")

    def import_asset_file(self, src_path: Path, category: Optional[str] = None) -> Path:
        if not self.app.ir_dir:
            raise ValueError("먼저 IR 프로젝트를 열거나 생성하세요.")
        src = Path(src_path)
        if not src.is_file():
            raise FileNotFoundError(f"가져올 파일이 존재하지 않습니다: {src}")
        cat = category or self.cat_var.get()
        dst_dir = self.app.ir_dir / "assets" / cat
        dst_dir.mkdir(parents=True, exist_ok=True)
        dst = dst_dir / src.name
        shutil.copy2(src, dst)
        self.app.log_console(f"[에셋 가져오기] {cat}/{src.name} -> {dst}")
        self.app.reload_all()
        return dst

    def _on_import_clicked(self) -> None:
        if not self.app.ir_dir:
            return
        cat = self.cat_var.get()
        if cat.startswith("Graphics/"):
            ftypes = [("Image Files", "*.png;*.jpg;*.jpeg;*.bmp"), ("All Files", "*.*")]
        elif cat.startswith("Audio/"):
            ftypes = [("Audio Files", "*.ogg;*.wav;*.mp3;*.mid"), ("All Files", "*.*")]
        else:
            ftypes = [("Font Files", "*.ttf;*.otf"), ("All Files", "*.*")]
        picked = filedialog.askopenfilename(parent=self, title=f"{cat} 에셋 선택", filetypes=ftypes)
        if picked:
            try:
                self.import_asset_file(Path(picked), category=cat)
            except Exception as exc:
                messagebox.showerror("에셋 가져오기 오류", str(exc), parent=self)

    def _on_delete_clicked(self) -> None:
        p = self._selected_path()
        if p is None or not p.is_file():
            return
        if not messagebox.askyesno("에셋 삭제 확인", f"선택한 파일을 삭제할까요?\n{p}", parent=self):
            return
        try:
            p.unlink()
            self.app.log_console(f"[에셋 삭제] {p}")
            self.reload_files()
        except Exception as exc:
            messagebox.showerror("삭제 오류", str(exc), parent=self)

    def _ensure_mixer(self) -> Any:
        import pygame

        if not self._mixer_ready:
            if not getattr(pygame, "mixer", None) or not pygame.mixer.get_init():
                pygame.mixer.init()
            self._mixer_ready = bool(pygame.mixer.get_init())
        return pygame

    def play_selected_audio(self) -> bool:
        p = self._selected_path()
        if p is None or not p.is_file():
            return False
        if p.suffix.lower() not in (".ogg", ".wav", ".mp3", ".mid", ".midi"):
            self.lbl_asset_info.config(text=f"선택한 파일 '{p.name}'은 오디오 형식이 아닙니다.")
            return False
        try:
            pygame = self._ensure_mixer()
            if self.cat_var.get() in ("Audio/SE", "Audio/ME"):
                snd = pygame.mixer.Sound(str(p))
                snd.play()
            else:
                pygame.mixer.music.load(str(p))
                pygame.mixer.music.play()
            self.lbl_asset_info.config(text=f"▶ 재생 중: {p.name}")
            return True
        except Exception as exc:
            self.lbl_asset_info.config(text=f"오디오 장치를 사용할 수 없어 재생을 건너뛰었습니다: {exc}")
            return False

    def stop_audio(self) -> None:
        try:
            import pygame

            if getattr(pygame, "mixer", None) and pygame.mixer.get_init():
                pygame.mixer.music.stop()
                pygame.mixer.stop()
        except Exception:
            pass

