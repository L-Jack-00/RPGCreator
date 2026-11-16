# -*- coding: utf-8 -*-
"""
Lightweight Pygame Player for IR Workspaces.
Consumes HeadlessGameSession without duplicating movement, event, or battle logic.
Supports 32x32 RMXP sprite sheets, viewport camera scrolling for large maps,
procedural fallback rendering, and F5 hot-reload.
"""
from __future__ import annotations

import array
from datetime import datetime
import io
import json
import math
import time
import wave
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..core.animation_cells import extract_animation_frame_cells
from ..core.autotile import autotile_frame_x_offset, resolve_autotile_quads, subtile_pixel_rect
from ..core.simulator import ANIMATION_TICKS_PER_FRAME, HeadlessGameSession, parse_message_segments, write_crash_report
from ..core.utils import load_json
from .window_opt import (
    PREVIEW_GRAY_DARK_RGB,
    PREVIEW_GRAY_LIGHT_RGB,
    attach_outline_drag_preview,
)

TILE_PX = 32
VIEW_W = 640
VIEW_H = 480
TARGET_FPS = 60
WALK_STEP_PX = 2
BASE_SPEED_PX_PER_SEC = 120.0
DASH_SPEED_PX_PER_SEC = 216.0
ICE_SLIDE_SPEED_PX_PER_SEC = 216.0
NPC_STEP_INTERVAL_MS = 350.0
NPC_SLIDE_SPEED_PX_PER_SEC = float(TILE_PX) / (NPC_STEP_INTERVAL_MS / 1000.0)
NPC_SPEED_TABLE_PX_PER_SEC: Dict[int, float] = {1: 42.0, 2: 64.0, 3: 92.0, 4: 120.0, 5: 170.0, 6: 240.0}
NPC_FREQ_INTERVAL_MS: Dict[int, float] = {1: 1100.0, 2: 750.0, 3: 480.0, 4: 320.0, 5: 180.0}
STATUS_MAX_VISIBLE_EQUIPS = 6
SAVE_SLOT_COUNT = 9

_UI_STRINGS: Dict[str, Dict[str, str]] = {
    "ko": {
        "new_game": "새 게임",
        "continue_autosave": "자동 저장 이어하기",
        "load": "불러오기",
        "load_empty": "불러오기 (저장 없음)",
        "options": "옵션",
        "quit": "종료",
        "back": "돌아가기",
        "back_title": "타이틀로 돌아가기",
        "bgm_vol": "BGM 볼륨",
        "se_vol": "효과음 볼륨",
        "bgs_vol": "BGS 볼륨",
        "me_vol": "ME 볼륨",
        "text_speed": "텍스트 속도",
        "auto_dash": "자동 대시",
        "audio_mute": "오디오 음소거",
        "minimap": "미니맵 표시",
        "shown": "표시",
        "hidden": "숨김",
        "keyset": "조작 키셋",
        "key_confirm": "확인 키 지정",
        "key_cancel": "취소 키 지정",
        "key_menu": "메뉴 키 지정",
        "auto_save": "자동 저장",
        "ui_lang": "UI 언어",
        "waiting_key": "입력 대기...",
        "menu_use": "아이템 / 스킬",
        "menu_equip": "장비 / 스테이터스",
        "menu_quests": "퀘스트 일지",
        "menu_save": "저장하기",
        "menu_load": "불러오기",
        "menu_options": "옵션",
        "menu_title": "타이틀로",
        "menu_close": "닫기",
        "gold": "소지금",
        "playtime": "플레이타임",
        "location": "위치",
        "subtitle_title": "모험을 시작하세요",
        "subtitle_options": "옵션 설정",
        "slot_save_hdr": "세이브 슬롯 선택 (Enter 저장 | Del 삭제 | ESC 닫기)",
        "slot_load_hdr": "불러오기 슬롯 선택 (Enter 로드 | F10 자동저장 | Del 삭제 | ESC 닫기)",
    },
    "en": {
        "new_game": "New Game",
        "continue_autosave": "Continue Autosave",
        "load": "Load Game",
        "load_empty": "Load Game (Empty)",
        "options": "Options",
        "quit": "Quit",
        "back": "Back",
        "back_title": "Return to Title",
        "bgm_vol": "BGM Volume",
        "se_vol": "SE Volume",
        "bgs_vol": "BGS Volume",
        "me_vol": "ME Volume",
        "text_speed": "Text Speed",
        "auto_dash": "Auto Dash",
        "audio_mute": "Audio Mute",
        "minimap": "Minimap",
        "shown": "Shown",
        "hidden": "Hidden",
        "keyset": "Key Preset",
        "key_confirm": "Confirm Key",
        "key_cancel": "Cancel Key",
        "key_menu": "Menu Key",
        "auto_save": "Auto Save",
        "ui_lang": "Language",
        "waiting_key": "Press Key...",
        "menu_use": "Items / Skills",
        "menu_equip": "Equip / Status",
        "menu_quests": "Quest Journal",
        "menu_save": "Save",
        "menu_load": "Load",
        "menu_options": "Options",
        "menu_title": "Title Screen",
        "menu_close": "Close",
        "gold": "Gold",
        "playtime": "Play Time",
        "location": "Location",
        "subtitle_title": "Begin Your Adventure",
        "subtitle_options": "Settings & Controls",
        "slot_save_hdr": "Save Slot (Enter Save | Del Delete | ESC Close)",
        "slot_load_hdr": "Load Slot (Enter Load | F10 Autosave | Del Delete | ESC Close)",
    },
}


def _resolve_ui_lang(session: Optional[HeadlessGameSession] = None, settings: Optional[Dict[str, Any]] = None) -> str:
    raw = ""
    if isinstance(settings, dict) and settings.get("lang"):
        raw = str(settings["lang"])
    elif session is not None and getattr(session, "locale", ""):
        raw = str(session.locale)
    return "en" if raw.lower().startswith("en") else "ko"


def _ui_tr(lang: str, key: str) -> str:
    tbl = _UI_STRINGS.get(lang, _UI_STRINGS["ko"])
    return tbl.get(key, _UI_STRINGS["ko"].get(key, key))


def _load_ui_font(pygame: Any, ir_dir: Path, size: int, gfx_root: Optional[Path] = None) -> Any:
    """Loads custom Fonts/*.ttf/.otf if present, else multi-OS CJK SysFont fallback."""
    font_dirs = [ir_dir / "Fonts", ir_dir.parent / "Fonts", ir_dir / "assets" / "Fonts"]
    if gfx_root is not None:
        font_dirs.append(gfx_root.parent / "Fonts")
    for fdir in font_dirs:
        if fdir.is_dir():
            for fp in sorted(fdir.iterdir()):
                if fp.is_file() and fp.suffix.lower() in (".ttf", ".otf"):
                    try:
                        return pygame.font.Font(str(fp), size)
                    except Exception:
                        pass
    for fam in ("malgungothic", "applesdgothicneo", "nanumgothic", "notosanscjkkr", "noto sans cjk kr"):
        try:
            f = pygame.font.SysFont(fam, size)
            if f is not None:
                return f
        except Exception:
            pass
    return pygame.font.Font(None, size + 5)


def _queue_system_se(session: HeadlessGameSession, kind: str) -> None:
    """Pushes a configured System DB SE (cursor, decision, cancel, buzzer) onto session.played_se_queue."""
    sys_f = getattr(session, "sys_fields", {})
    if not isinstance(sys_f, dict):
        return
    raw = sys_f.get(f"{kind}_se")
    if not isinstance(raw, dict):
        return
    sf = raw.get("fields", raw)
    name = str(sf.get("name", "") or "")
    if name:
        session.played_se_queue.append({
            "name": name,
            "volume": int(sf.get("volume", 80)),
            "pitch": int(sf.get("pitch", 100)),
        })


def compute_walk_frame_col(step_count: int, step_timer: int) -> int:
    """Returns the 4-column character sprite walk frame index while moving, or 0 at rest."""
    return (step_count % 4) if step_timer > 0 else 0


def compute_stride_col(stride_count: int, slide_dx: int, slide_dy: int) -> int:
    """Computes alternating two-legged walk columns (1->2->3->0) during a sub-tile slide."""
    progress = max(abs(slide_dx), abs(slide_dy))
    if progress <= 0:
        return 0
    is_odd = (stride_count % 2) == 1
    if progress > TILE_PX // 2:
        return 1 if is_odd else 3
    return 2 if is_odd else 0


def advance_slide_offset(slide_dx: int, slide_dy: int, step_px: int = WALK_STEP_PX) -> Tuple[int, int]:
    """Steps sub-tile pixel slide offsets smoothly toward zero by at most step_px per axis."""
    def _approach_zero(val: int) -> int:
        if val > 0:
            return max(0, val - step_px)
        if val < 0:
            return min(0, val + step_px)
        return 0

    return _approach_zero(slide_dx), _approach_zero(slide_dy)


def compute_dt_slide_step(
    dt_ms: float,
    accum: float,
    speed_px_per_sec: float = BASE_SPEED_PX_PER_SEC,
    is_diagonal: bool = False,
) -> Tuple[int, float]:
    """Computes integer pixel advance from elapsed frame time with diagonal 1/sqrt(2) normalization."""
    dt_s = min(max(float(dt_ms), 0.0), 50.0) / 1000.0
    eff_speed = speed_px_per_sec / math.sqrt(2.0) if is_diagonal else speed_px_per_sec
    total = max(0.0, float(accum)) + eff_speed * dt_s
    step_px = int(total)
    return step_px, total - step_px


def resolve_player_speed_px(dash_held: bool, is_ice_slide: bool = False) -> float:
    """Returns active movement velocity accounting for Ctrl dash and smooth ice floor glide."""
    if is_ice_slide:
        return max(ICE_SLIDE_SPEED_PX_PER_SEC, DASH_SPEED_PX_PER_SEC if dash_held else ICE_SLIDE_SPEED_PX_PER_SEC)
    return DASH_SPEED_PX_PER_SEC if dash_held else BASE_SPEED_PX_PER_SEC


def resolve_npc_speed_px(page: Dict[str, Any]) -> float:
    """Maps RMXP event move_speed (1..6) to pixel slide velocity."""
    spd = int(page.get("move_speed", 3))
    return NPC_SPEED_TABLE_PX_PER_SEC.get(spd, NPC_SLIDE_SPEED_PX_PER_SEC)


def resolve_npc_interval_ms(page: Dict[str, Any]) -> float:
    """Maps RMXP event move_frequency (1..5) to route step interval in milliseconds."""
    freq = int(page.get("move_frequency", 3))
    return NPC_FREQ_INTERVAL_MS.get(freq, NPC_STEP_INTERVAL_MS)


def sync_npc_slide_states(
    npc_slides: Dict[Tuple[int, int], Dict[str, Any]],
    map_id: int,
    before_coords: Dict[int, Tuple[int, int]],
    active_events: List[Tuple[Dict[str, Any], Dict[str, Any]]],
    tile_px: int = TILE_PX,
) -> None:
    """Initializes pixel slide offsets for any NPC whose tile coordinate changed on step."""
    active_keys = set()
    for ev, page in active_events:
        eid = int(ev["id"])
        key = (map_id, eid)
        active_keys.add(key)
        nx, ny = int(ev["x"]), int(ev["y"])
        if eid in before_coords and (nx, ny) != before_coords[eid]:
            ox, oy = before_coords[eid]
            prev_stride = int(npc_slides.get(key, {}).get("stride", 0))
            npc_slides[key] = {
                "dx": (ox - nx) * tile_px,
                "dy": (oy - ny) * tile_px,
                "stride": prev_stride + 1,
                "accum": 0.0,
                "speed": resolve_npc_speed_px(page),
            }
    for stale_key in [k for k in npc_slides if k[0] != map_id or k not in active_keys]:
        npc_slides.pop(stale_key, None)


def advance_npc_slide_states(
    npc_slides: Dict[Tuple[int, int], Dict[str, Any]],
    dt_ms: float,
    speed_px_per_sec: float = NPC_SLIDE_SPEED_PX_PER_SEC,
) -> None:
    """Advances active NPC pixel slide offsets toward zero based on elapsed frame time."""
    for entry in npc_slides.values():
        ndx = int(entry.get("dx", 0))
        ndy = int(entry.get("dy", 0))
        if ndx == 0 and ndy == 0:
            entry["accum"] = 0.0
            continue
        eff_speed = float(entry.get("speed", speed_px_per_sec))
        step_px, next_acc = compute_dt_slide_step(
            dt_ms,
            float(entry.get("accum", 0.0)),
            speed_px_per_sec=eff_speed,
            is_diagonal=(ndx != 0 and ndy != 0),
        )
        entry["dx"], entry["dy"] = advance_slide_offset(ndx, ndy, step_px)
        entry["accum"] = next_acc


def compute_status_equip_window(
    total: int,
    cursor: int,
    max_visible: int = STATUS_MAX_VISIBLE_EQUIPS,
) -> Tuple[int, int, int]:
    """Computes (selected_idx, start_idx, end_idx) slice for scrollable status gear lists."""
    if total <= 0:
        return 0, 0, 0
    sel_idx = cursor % total
    if total <= max_visible:
        return sel_idx, 0, total
    start_idx = max(0, min(sel_idx - max_visible // 2, total - max_visible))
    return sel_idx, start_idx, min(total, start_idx + max_visible)


def process_save_load_shortcut(
    pygame: Any,
    key: int,
    session: HeadlessGameSession,
    active_slot: int,
    pending_load_confirm: bool,
) -> Tuple[bool, int, bool, str]:
    """Handles F6/F7/F9 multi-slot save/load shortcuts with confirmation guard and toast text."""
    slot = max(1, min(SAVE_SLOT_COUNT, int(active_slot)))
    if key == pygame.K_F7:
        next_slot = (slot % SAVE_SLOT_COUNT) + 1
        meta = session.get_save_slot_summary(next_slot)
        return True, next_slot, False, f"슬롯 {next_slot} 선택 ({meta['label']})"
    if key == pygame.K_F6 and not session.pending and not session.game_over:
        if not session.save_enabled:
            return True, slot, False, "현재 구역에서는 저장이 금지되어 있습니다"
        session.save_to_file(slot=slot)
        meta = session.get_save_slot_summary(slot)
        return True, slot, False, f"슬롯 {slot} 저장 완료 ({meta['label']})"
    if key == pygame.K_F9 and not session.pending:
        if not session.slot_exists(slot):
            return True, slot, False, f"슬롯 {slot}에 저장된 파일이 없습니다"
        meta = session.get_save_slot_summary(slot)
        if not pending_load_confirm:
            return True, slot, True, f"슬롯 {slot} [{meta['label']}] 로드? F9 재입력 확인 (ESC 취소)"
        try:
            session.load_from_file(slot=slot)
        except ValueError:
            return True, slot, False, f"슬롯 {slot} 로드 거부됨: 저장 데이터 무결성 검증 실패"
        if not bool(getattr(session, "save_checksum_valid", True)):
            return True, slot, False, f"슬롯 {slot} 로드 완료 (경고: 저장 데이터 무결성 확인 실패)"
        return True, slot, False, f"슬롯 {slot} 불러오기 완료"
    if key == pygame.K_ESCAPE and pending_load_confirm:
        return True, slot, False, "불러오기 취소됨"
    return False, slot, pending_load_confirm, ""


def compute_camera_offset_px(
    pixel_x: int,
    pixel_y: int,
    map_width: int,
    map_height: int,
    view_w: int = VIEW_W,
    view_h: int = VIEW_H,
    tile_px: int = TILE_PX,
) -> Tuple[int, int]:
    """Computes clamped pixel camera offsets centering an interpolated pixel coordinate."""
    max_x = max(0, map_width * tile_px - view_w)
    max_y = max(0, map_height * tile_px - view_h)
    target_x = pixel_x - (view_w - tile_px) // 2
    target_y = pixel_y - (view_h - tile_px) // 2
    return max(0, min(target_x, max_x)), max(0, min(target_y, max_y))


def first_healing_item_id(session: HeadlessGameSession) -> Optional[int]:
    """Returns the first owned item ID that restores HP or SP, if available."""
    for iid in sorted(session.inventory):
        if session.inventory.get(iid, 0) <= 0:
            continue
        flds = session.items.get(iid, {}).get("fields", {})
        if any(int(flds.get(k, 0)) > 0 for k in ("recover_hp", "recover_hp_rate", "recover_sp", "recover_sp_rate")):
            return iid
    return None


def format_hud_text(map_name: str, snap: Dict[str, Any], active_slot: int = 1) -> str:
    """Builds the single-line top HUD banner from session snapshot values."""
    v_label = {"boat": "[배] ", "ship": "[함선] ", "airship": "[비공정] "}.get(str(snap.get("vehicle", "walk")), "")
    base = (
        f"{v_label}{map_name} ({snap['x']},{snap['y']}) | "
        f"HP {snap['hp']}/{snap['max_hp']} SP {snap['sp']}/{snap['max_sp']} | "
        f"ATK {snap['atk']} DEF {snap['pdef']} | {snap['gold']}G"
    )
    if snap.get("game_over"):
        return f"전멸 - R/Enter: 최근 저장 복귀 | F5: 재시작 | {base}"
    return f"{base} | I:회복 Tab:상태 F5:리로드 F6:저장 F7:슬롯{active_slot} F9:로드"


def list_bag_equips_for_lead(
    session: HeadlessGameSession,
    actor_id: Optional[int] = None,
) -> List[Tuple[int, int, str, int]]:
    """Lists class-compatible owned gear and unequip actions for the target actor."""
    target_aid = session.lead_actor_id if actor_id is None else int(actor_id)
    entries: List[Tuple[int, int, str, int]] = []
    for wid in sorted(session.weapons_owned):
        cnt = int(session.weapons_owned.get(wid, 0))
        if cnt <= 0 or wid not in session.weapons or not session.can_actor_equip(target_aid, 0, wid):
            continue
        wf = session.weapons[wid].get("fields", {})
        wname = str(wf.get("name", f"무기#{wid}"))
        watk = int(wf.get("atk", 0))
        entries.append((0, wid, f"[무기] {wname} (ATK +{watk}) x{cnt}", cnt))
    for aid in sorted(session.armors_owned):
        cnt = int(session.armors_owned.get(aid, 0))
        if cnt <= 0 or aid not in session.armors:
            continue
        af = session.armors[aid].get("fields", {})
        aname = str(af.get("name", f"방어구#{aid}"))
        apdef = int(af.get("pdef", 0))
        eq_type = int(af.get("kind", 0)) + 1
        if not session.can_actor_equip(target_aid, eq_type, aid):
            continue
        entries.append((eq_type, aid, f"[방어구] {aname} (PDEF +{apdef}) x{cnt}", cnt))
    st = session._get_actor_state(target_aid)
    if int(st.get("weapon_id", 0)) > 0 and session.can_actor_equip(target_aid, 0, 0):
        entries.append((0, 0, "[무기 해제] 빈 손", 1))
    for slot_id in (1, 2, 3, 4):
        if int(st.get("armor_ids", {}).get(slot_id, 0)) > 0 and session.can_actor_equip(target_aid, slot_id, 0):
            entries.append((slot_id, 0, f"[방어구 슬롯{slot_id} 해제]", 1))
    return entries


def compute_equip_stat_delta(
    session: HeadlessGameSession,
    actor_id: int,
    eq_type: int,
    eq_id: int,
) -> str:
    """Computes human-readable stat delta preview string before equipping or unequipping gear."""
    st = session._get_actor_state(actor_id)
    if eq_type == 0:
        cur_id = int(st.get("weapon_id", 0))
        cur_f = session.weapons.get(cur_id, {}).get("fields", {}) if cur_id else {}
        new_f = session.weapons.get(eq_id, {}).get("fields", {}) if eq_id else {}
    else:
        cur_id = int(st.get("armor_ids", {}).get(eq_type, 0))
        cur_f = session.armors.get(cur_id, {}).get("fields", {}) if cur_id else {}
        new_f = session.armors.get(eq_id, {}).get("fields", {}) if eq_id else {}
    parts: List[str] = []
    for stat_k, label in (("atk", "ATK"), ("pdef", "DEF"), ("mdef", "MDEF"), ("agi_plus", "AGI"), ("dex_plus", "DEX"), ("int_plus", "INT")):
        cur_v = int(cur_f.get(stat_k, 0) or 0)
        new_v = int(new_f.get(stat_k, 0) or 0)
        diff = new_v - cur_v
        if diff != 0:
            sign = "+" if diff > 0 else ""
            parts.append(f"{label} {sign}{diff}")
    return " | ".join(parts) if parts else "능력치 변화 없음"


def format_status_lines(
    session: HeadlessGameSession,
    actor_id: Optional[int] = None,
) -> List[str]:
    """Formats party member combat stats, equipped gear, skills, and items for overlay display."""
    party = session.party_member_ids or [session.lead_actor_id]
    target_aid = session.lead_actor_id if actor_id is None else int(actor_id)
    st = session._get_actor_state(target_aid)
    wpn_id = int(st.get("weapon_id", 0))
    wpn = session.weapons.get(wpn_id, {}).get("fields", {}).get("name", "없음") if wpn_id else "없음"
    arm_names = [
        str(session.armors.get(aid, {}).get("fields", {}).get("name", aid))
        for aid in st.get("armor_ids", {}).values()
        if int(aid) > 0 and int(aid) in session.armors
    ]
    skill_names = [
        str(session.skills_db.get(sid, {}).get("fields", {}).get("name", sid))
        for sid in st.get("learned_skills", [])
    ]
    item_summary = [
        f"{session.items.get(iid, {}).get('fields', {}).get('name', iid)}x{cnt}"
        for iid, cnt in sorted(session.inventory.items())
        if cnt > 0
    ]
    fstats = session.actor_full_stats(target_aid)
    party_idx = (party.index(target_aid) + 1) if target_aid in party else 1
    party_tag = f" ({party_idx}/{len(party)} ←/→ 전환)" if len(party) > 1 else ""
    total_sec = int(float(getattr(session, "play_time_ms", 0.0)) // 1000)
    mm, ss = divmod(total_sec, 60)
    lines = [
        f"[{st['name']} Lv.{int(st['level'])}]{party_tag} HP {int(st['hp'])}/{int(st['max_hp'])} SP {int(st['sp'])}/{int(st['max_sp'])}",
        f"ATK {fstats['atk']} DEF {fstats['pdef']} DEX {fstats['dex']} AGI {fstats['agi']} INT {fstats['int']} EVA {fstats['eva']}% | 플레이 타임 {mm:02d}:{ss:02d}",
        f"무기: {wpn} | 방어구: {', '.join(arm_names) if arm_names else '없음'}",
        f"스킬: {', '.join(skill_names) if skill_names else '없음'}",
        f"소지품: {', '.join(item_summary) if item_summary else '없음'}",
    ]
    bestiary = session.get_bestiary_summary() if hasattr(session, "get_bestiary_summary") else []
    if bestiary:
        lines.append(f"몬스터 도감: {', '.join(bestiary)}")
    if session.log:
        lines.append(f"최근: {session.log[-1]}")
    return lines


def compute_camera_offset(
    player_x: int,
    player_y: int,
    map_width: int,
    map_height: int,
    view_w: int = VIEW_W,
    view_h: int = VIEW_H,
    tile_px: int = TILE_PX,
) -> Tuple[int, int]:
    """Computes clamped pixel camera offsets centering the player on large maps."""
    return compute_camera_offset_px(
        player_x * tile_px,
        player_y * tile_px,
        map_width,
        map_height,
        view_w=view_w,
        view_h=view_h,
        tile_px=tile_px,
    )


def _find_graphics_dir(ir_dir: Path, graphics_dir: Optional[Path]) -> Optional[Path]:
    if graphics_dir and Path(graphics_dir).is_dir():
        return Path(graphics_dir)
    for candidate in (ir_dir / "Graphics", ir_dir.parent / "rmxp" / "Graphics", ir_dir.parent / "Graphics"):
        if candidate.is_dir():
            return candidate
    return None


def _find_audio_dir(ir_dir: Path, audio_dir: Optional[Path] = None) -> Optional[Path]:
    if audio_dir and Path(audio_dir).is_dir():
        return Path(audio_dir)
    for candidate in (ir_dir / "Audio", ir_dir.parent / "rmxp" / "Audio", ir_dir.parent / "Audio"):
        if candidate.is_dir():
            return candidate
    return None


def apply_hue_shift(
    pygame: Any,
    surf: Any,
    hue: int,
    cache: Optional[Dict[Tuple[int, int], Any]] = None,
) -> Any:
    """Applies RMXP 0..359 degree hue rotation to a Pygame surface using cached HSV conversion."""
    norm_hue = int(hue) % 360
    if norm_hue == 0 or surf is None:
        return surf
    ckey = (id(surf), norm_hue)
    if cache is not None and ckey in cache:
        return cache[ckey]
    try:
        from PIL import Image

        w, h = surf.get_size()
        raw = pygame.image.tostring(surf, "RGBA")
        rgba_img = Image.frombytes("RGBA", (w, h), raw)
        alpha = rgba_img.getchannel("A")
        hsv = rgba_img.convert("RGB").convert("HSV")
        h_ch, s_ch, v_ch = hsv.split()
        shift_byte = int(round(norm_hue * 255.0 / 360.0)) % 256
        h_shifted = h_ch.point(lambda px: (px + shift_byte) % 256)
        rebuilt = Image.merge("HSV", (h_shifted, s_ch, v_ch)).convert("RGBA")
        rebuilt.putalpha(alpha)
        out = pygame.Surface((w, h), pygame.SRCALPHA)
        out.blit(pygame.image.fromstring(rebuilt.tobytes(), (w, h), "RGBA"), (0, 0))
        if cache is not None:
            cache[ckey] = out
        return out
    except Exception:
        return surf


def _resolve_audio_file(audio_root: Optional[Path], kind: str, name: str) -> Optional[Path]:
    if not audio_root or not name:
        return None
    base = audio_root / kind
    if not base.is_dir():
        return None
    for ext in (".ogg", ".mp3", ".wav", ".mid"):
        cand = base / f"{name}{ext}"
        if cand.is_file():
            return cand
    return None


def _resolve_audio_source(
    audio_root: Optional[Path],
    kind: str,
    name: str,
    rgssad_reader: Optional[Any] = None,
) -> Optional[Tuple[str, Any, str]]:
    """Resolves audio either from disk or straight from an encrypted RGSSAD archive stream."""
    if not name:
        return None
    if rgssad_reader is not None:
        exts = (".ogg", ".mp3", ".wav", ".mid")
        entry = rgssad_reader.find_entry(f"Audio/{kind}/{name}", extensions=exts)
        if entry is not None:
            raw = rgssad_reader.read_bytes(entry)
            if raw is not None:
                return ("bytes", raw, Path(entry).suffix.lower())
    fpath = _resolve_audio_file(audio_root, kind, name)
    if fpath is not None:
        return ("path", fpath, fpath.suffix.lower())
    return None


def resample_wav_pitch_bytes(raw_wav: bytes, pitch: int = 100) -> Optional[bytes]:
    """Resamples uncompressed PCM WAV bytes linearly to match an RMXP pitch percentage (50..150)."""
    clamped = max(50, min(200, int(pitch)))
    if clamped == 100 or not raw_wav:
        return raw_wav
    try:
        with wave.open(io.BytesIO(raw_wav), "rb") as wf:
            nch = wf.getnchannels()
            sw = wf.getsampwidth()
            fr = wf.getframerate()
            nf = wf.getnframes()
            comp = wf.getcomptype()
            if comp != "NONE" or sw not in (1, 2) or nf <= 1 or nch < 1:
                return raw_wav
            raw_pcm = wf.readframes(nf)
    except Exception:
        return None
    rate_mult = clamped / 100.0
    out_frames = max(1, int(round(nf / rate_mult)))
    if sw == 2:
        src = array.array("h")
        src.frombytes(raw_pcm)
        dst = array.array("h", [0]) * (out_frames * nch)
        max_idx = nf - 1
        for i in range(out_frames):
            pos = i * rate_mult
            i0 = min(max_idx, int(pos))
            i1 = min(max_idx, i0 + 1)
            frac = pos - i0
            for ch in range(nch):
                v0 = src[i0 * nch + ch]
                v1 = src[i1 * nch + ch]
                dst[i * nch + ch] = max(-32768, min(32767, int(round(v0 + (v1 - v0) * frac))))
        out_pcm = dst.tobytes()
    else:
        src_b = raw_pcm
        dst_b = bytearray(out_frames * nch)
        max_idx = nf - 1
        for i in range(out_frames):
            pos = i * rate_mult
            i0 = min(max_idx, int(pos))
            i1 = min(max_idx, i0 + 1)
            frac = pos - i0
            for ch in range(nch):
                v0 = src_b[i0 * nch + ch]
                v1 = src_b[i1 * nch + ch]
                dst_b[i * nch + ch] = max(0, min(255, int(round(v0 + (v1 - v0) * frac))))
        out_pcm = bytes(dst_b)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as out_wf:
        out_wf.setnchannels(nch)
        out_wf.setsampwidth(sw)
        out_wf.setframerate(fr)
        out_wf.writeframes(out_pcm)
    return buf.getvalue()


def resample_sndarray_samples(samples: Any, pitch: int = 100) -> Any:
    """Resamples 1D or 2D PCM sample arrays (numpy or sequence) for universal .ogg/.mp3/.wav pitch playback."""
    clamped = max(50, min(200, int(pitch)))
    if clamped == 100:
        return samples
    rate_mult = clamped / 100.0
    n_len = len(samples)
    if n_len <= 1:
        return samples
    out_len = max(1, int(round(n_len / rate_mult)))
    shape = getattr(samples, "shape", None)
    if shape is not None:
        try:
            import numpy as np  # type: ignore[import-not-found]
            idx = np.clip(np.round(np.arange(out_len, dtype=np.float64) * rate_mult).astype(np.int64), 0, n_len - 1)
            return np.ascontiguousarray(samples[idx])
        except Exception:
            pass
    indices = [min(n_len - 1, int(round(i * rate_mult))) for i in range(out_len)]
    return [samples[i] for i in indices]


def _load_pitched_sound(
    pygame: Any,
    fpath: Any,
    pitch: int = 100,
    sound_cache: Optional[Dict[Tuple[str, int], Any]] = None,
    raw_bytes: Optional[bytes] = None,
    suffix: str = "",
) -> Any:
    """Loads a pygame.mixer.Sound from disk or raw archive bytes with pitch resampling and cache lookup."""
    clamped = max(50, min(200, int(pitch)))
    cache_tag = f"mem:{hash(raw_bytes) if raw_bytes is not None else str(fpath)}"
    key = (cache_tag if raw_bytes is not None else str(fpath), clamped)
    if sound_cache is not None and key in sound_cache:
        return sound_cache[key]
    snd = None
    ext = suffix.lower() if suffix else (fpath.suffix.lower() if hasattr(fpath, "suffix") else "")
    if clamped != 100 and ext == ".wav":
        wav_src = raw_bytes if raw_bytes is not None else Path(fpath).read_bytes()
        resampled = resample_wav_pitch_bytes(wav_src, clamped)
        if resampled:
            snd = pygame.mixer.Sound(file=io.BytesIO(resampled))
    if snd is None:
        base_snd = pygame.mixer.Sound(file=io.BytesIO(raw_bytes)) if raw_bytes is not None else pygame.mixer.Sound(str(fpath))
        if clamped != 100 and getattr(pygame, "sndarray", None) is not None:
            try:
                raw_arr = pygame.sndarray.array(base_snd)
                res_arr = resample_sndarray_samples(raw_arr, clamped)
                snd = pygame.sndarray.make_sound(res_arr)
            except Exception:
                snd = base_snd
        else:
            snd = base_snd
    if sound_cache is not None:
        sound_cache[key] = snd
    return snd


def _music_load_source(pygame: Any, source: Tuple[str, Any, str]) -> None:
    kind, val, _ext = source
    if kind == "bytes":
        pygame.mixer.music.load(io.BytesIO(val))
    else:
        pygame.mixer.music.load(str(val))


def _sync_audio_runtime(
    pygame: Any,
    audio_root: Optional[Path],
    session: HeadlessGameSession,
    playing_bgm: Optional[str],
    mixer_ok: bool,
    playing_bgs: Optional[str] = None,
    bgs_channel: Optional[Any] = None,
    sound_cache: Optional[Dict[Tuple[str, int], Any]] = None,
    settings: Optional[Dict[str, Any]] = None,
    rgssad_reader: Optional[Any] = None,
) -> Tuple[Optional[str], Optional[str], Optional[Any]]:
    """Plays BGM, BGS (reserved channel), and queued SE/ME with per-bus volume settings and archive streaming."""
    target_bgm = session.current_bgm.get("name") if isinstance(session.current_bgm, dict) else None
    target_bgs = session.current_bgs.get("name") if isinstance(session.current_bgs, dict) else None
    muted = bool(getattr(session, "audio_muted", False))
    cfg = settings or {}
    master_bgm = max(0, min(100, int(cfg.get("bgm_volume", 100)))) / 100.0
    master_bgs = max(0, min(100, int(cfg.get("bgs_volume", cfg.get("se_volume", 100))))) / 100.0
    master_se = max(0, min(100, int(cfg.get("se_volume", 100)))) / 100.0
    master_me = max(0, min(100, int(cfg.get("me_volume", cfg.get("se_volume", 100))))) / 100.0
    if not mixer_ok:
        session.played_se_queue.clear()
        session.played_me_queue.clear()
        session.pending_bgm_fade = None
        session.pending_bgs_fade = None
        return target_bgm, target_bgs, bgs_channel
    try:
        bgm_fade = getattr(session, "pending_bgm_fade", None)
        if isinstance(bgm_fade, dict):
            session.pending_bgm_fade = None
            fade_ms = max(50, int(round(float(bgm_fade.get("seconds", 1)) * 1000)))
            pygame.mixer.music.fadeout(fade_ms)
            playing_bgm = None
        else:
            bgm_vol = 0.0 if muted else max(0.0, min(1.0, (int((session.current_bgm or {}).get("volume", 80)) / 100.0) * master_bgm))
            if target_bgm != playing_bgm:
                if playing_bgm and hasattr(pygame.mixer.music, "fadeout"):
                    pygame.mixer.music.fadeout(180)
                if target_bgm:
                    src = _resolve_audio_source(audio_root, "BGM", str(target_bgm), rgssad_reader=rgssad_reader)
                    if src is not None:
                        _music_load_source(pygame, src)
                        pygame.mixer.music.set_volume(bgm_vol)
                        pygame.mixer.music.play(-1)
                else:
                    pygame.mixer.music.stop()
                playing_bgm = target_bgm
            elif target_bgm:
                pygame.mixer.music.set_volume(bgm_vol)
        bgs_fade = getattr(session, "pending_bgs_fade", None)
        if isinstance(bgs_fade, dict):
            session.pending_bgs_fade = None
            fade_ms = max(50, int(round(float(bgs_fade.get("seconds", 1)) * 1000)))
            if bgs_channel is not None:
                bgs_channel.fadeout(fade_ms)
            playing_bgs = None
        else:
            bgs_vol = 0.0 if muted else max(0.0, min(1.0, (int((session.current_bgs or {}).get("volume", 80)) / 100.0) * master_bgs))
            if target_bgs != playing_bgs:
                if target_bgs:
                    src = _resolve_audio_source(audio_root, "BGS", str(target_bgs), rgssad_reader=rgssad_reader)
                    if src is not None:
                        if bgs_channel is None:
                            if hasattr(pygame.mixer, "set_reserved"):
                                pygame.mixer.set_reserved(1)
                            bgs_channel = pygame.mixer.Channel(0)
                        bgs_pitch = int((session.current_bgs or {}).get("pitch", 100))
                        skind, sval, sext = src
                        bgs_snd = _load_pitched_sound(
                            pygame, sval if skind == "path" else Path(f"{target_bgs}{sext}"),
                            pitch=bgs_pitch, sound_cache=sound_cache,
                            raw_bytes=(sval if skind == "bytes" else None), suffix=sext,
                        )
                        bgs_channel.set_volume(bgs_vol)
                        bgs_channel.play(bgs_snd, loops=-1)
                elif bgs_channel is not None:
                    bgs_channel.stop()
                playing_bgs = target_bgs
            elif bgs_channel is not None and target_bgs:
                bgs_channel.set_volume(bgs_vol)
        for folder, queue, bus_vol in (("SE", session.played_se_queue, master_se), ("ME", session.played_me_queue, master_me)):
            while queue:
                ev = queue.pop(0)
                if muted:
                    continue
                src = _resolve_audio_source(audio_root, folder, str(ev.get("name", "")), rgssad_reader=rgssad_reader)
                if src is not None:
                    skind, sval, sext = src
                    snd = _load_pitched_sound(
                        pygame, sval if skind == "path" else Path(f"{ev.get('name')}{sext}"),
                        pitch=int(ev.get("pitch", 100)), sound_cache=sound_cache,
                        raw_bytes=(sval if skind == "bytes" else None), suffix=sext,
                    )
                    snd.set_volume(max(0.0, min(1.0, (int(ev.get("volume", 80)) / 100.0) * bus_vol)))
                    snd.play()
    except Exception:
        session.played_se_queue.clear()
        session.played_me_queue.clear()
    return playing_bgm, playing_bgs, bgs_channel


def list_field_skills_and_items_for_actor(
    session: HeadlessGameSession,
    actor_id: Optional[int] = None,
) -> List[Tuple[str, int, str]]:
    """Lists owned recovery items and field-usable learned skills for the target actor."""
    target_aid = session.lead_actor_id if actor_id is None else int(actor_id)
    st = session._get_actor_state(target_aid)
    entries: List[Tuple[str, int, str]] = []
    for iid in sorted(session.inventory):
        cnt = int(session.inventory.get(iid, 0))
        if cnt <= 0 or iid not in session.items:
            continue
        itf = session.items[iid].get("fields", {})
        if int(itf.get("occasion", 0)) not in (0, 2):
            continue
        if any(int(itf.get(k, 0)) > 0 for k in ("recover_hp", "recover_hp_rate", "recover_sp", "recover_sp_rate")) or bool(
            itf.get("minus_state_set")
        ):
            entries.append(("item", iid, f"[아이템] {itf.get('name', iid)} x{cnt}"))
    for sid in st.get("learned_skills", []):
        skf = session.skills_db.get(int(sid), {}).get("fields", {})
        if int(skf.get("occasion", 1)) in (0, 2):
            sp_c = int(skf.get("sp_cost", 0))
            entries.append(("skill", int(sid), f"[스킬] {skf.get('name', sid)} (SP {sp_c})"))
    return entries


def _draw_screen_tone_and_flash(
    pygame: Any,
    screen: Any,
    session: HeadlessGameSession,
    overlay_cache: Optional[Dict[str, Any]] = None,
) -> None:
    """Applies map screen tone darkening/tinting and active screen flash overlays without per-frame surface churn."""
    def _get_overlay() -> Any:
        if overlay_cache is not None:
            cached = overlay_cache.get("screen_ov")
            if cached is not None:
                return cached
            surf = pygame.Surface((VIEW_W, VIEW_H), pygame.SRCALPHA)
            overlay_cache["screen_ov"] = surf
            return surf
        return pygame.Surface((VIEW_W, VIEW_H), pygame.SRCALPHA)

    r, g, b, gray = [int(v) for v in (session.screen_tone or [0, 0, 0, 0])[:4]]
    dark_alpha = min(210, max(0, (-r - g - b) // 3 + max(0, gray) // 2))
    if dark_alpha > 0 or r > 20 or g > 20 or b > 20:
        tint_r = max(0, min(255, r)) if r > 0 else 0
        tint_g = max(0, min(255, g)) if g > 0 else 0
        tint_b = max(0, min(255, b)) if b > 0 else 0
        alpha = max(dark_alpha, min(140, max(tint_r, tint_g, tint_b) // 2))
        if alpha > 0:
            ov = _get_overlay()
            ov.fill((tint_r, tint_g, tint_b, alpha))
            screen.blit(ov, (0, 0))
    fl = session.pending_flash
    if isinstance(fl, dict) and int(fl.get("frames", 0)) > 0:
        rgba = fl.get("rgba", [255, 255, 255, 200])
        max_f = max(1, int(fl.get("max_frames", 1)))
        alpha = int(max(0, min(255, int(rgba[3]))) * (int(fl["frames"]) / max_f))
        if alpha > 0:
            ov = _get_overlay()
            ov.fill((int(rgba[0]), int(rgba[1]), int(rgba[2]), alpha))
            screen.blit(ov, (0, 0))


def _tint_surface(pygame: Any, surf: Any, tone: List[int]) -> Any:
    """Applies RMXP RGBG tone (-255..255) onto a copied pygame Surface."""
    r, g, b = int(tone[0]) if len(tone) > 0 else 0, int(tone[1]) if len(tone) > 1 else 0, int(tone[2]) if len(tone) > 2 else 0
    if r == 0 and g == 0 and b == 0:
        return surf
    out = surf.copy()
    if r > 0 or g > 0 or b > 0:
        out.fill((max(0, min(255, r)), max(0, min(255, g)), max(0, min(255, b)), 0), special_flags=pygame.BLEND_RGBA_ADD)
    if r < 0 or g < 0 or b < 0:
        out.fill((max(0, min(255, -r)), max(0, min(255, -g)), max(0, min(255, -b)), 0), special_flags=pygame.BLEND_RGBA_SUB)
    return out


def _build_transition_wipe_surface(pygame: Any, t_img: Any, ratio: float) -> Any:
    """Builds a grayscale luminance cutoff wipe mask surface for Transitions/ textures."""
    clamped = max(0.0, min(1.0, float(ratio)))
    scaled = pygame.Surface((VIEW_W, VIEW_H), pygame.SRCALPHA)
    scaled.blit(pygame.transform.smoothscale(t_img, (VIEW_W, VIEW_H)), (0, 0))
    cutoff = int(round(clamped * 255))
    try:
        import numpy as np  # type: ignore

        rgb = pygame.surfarray.pixels3d(scaled)
        alpha = pygame.surfarray.pixels_alpha(scaled)
        luma = (0.299 * rgb[:, :, 0] + 0.587 * rgb[:, :, 1] + 0.114 * rgb[:, :, 2]).astype(np.uint8)
        rgb[:, :, :] = 0
        alpha[:, :] = np.where(luma <= cutoff, 255, 0).astype(np.uint8)
        del rgb, alpha
        return scaled
    except Exception:
        pass
    try:
        from PIL import Image

        raw = pygame.image.tostring(scaled, "RGBA")
        luma_img = Image.frombytes("RGBA", (VIEW_W, VIEW_H), raw).convert("L")
        mask_alpha = luma_img.point(lambda px: 255 if px <= cutoff else 0)
        black = Image.new("RGBA", (VIEW_W, VIEW_H), (0, 0, 0, 0))
        black.putalpha(mask_alpha)
        surf_out = pygame.image.fromstring(black.tobytes(), (VIEW_W, VIEW_H), "RGBA")
        out_alpha = pygame.Surface((VIEW_W, VIEW_H), pygame.SRCALPHA)
        out_alpha.blit(surf_out, (0, 0))
        return out_alpha
    except Exception:
        scaled.set_alpha(int(round(255 * clamped)))
        return scaled


def _draw_weather_and_fade(
    pygame: Any,
    screen: Any,
    small_font: Any,
    session: HeadlessGameSession,
    anim_tick: int,
    load_img: Optional[Any] = None,
) -> None:
    """Draws deterministic RMXP weather particles, screen fade/transition wipe, and active timer badge."""
    weather = session.weather if isinstance(session.weather, dict) else {}
    wtype = str(weather.get("type", "none"))
    power = max(0, min(9, int(weather.get("power", 0))))
    if wtype in ("rain", "storm", "snow") and power > 0:
        count = 12 + power * 6
        if wtype == "snow":
            for idx in range(count):
                sx = (idx * 67 + int(anim_tick * 1.1) + (idx % 5) * 19) % VIEW_W
                sy = (idx * 43 + int(anim_tick * 2.2) + (idx % 3) * 17) % VIEW_H
                pygame.draw.circle(screen, (235, 245, 255), (sx, sy), 2)
        else:
            dx_off = -10 if wtype == "storm" else -4
            dy_len = 16 if wtype == "storm" else 11
            spd = 9 if wtype == "storm" else 6
            col = (140, 180, 240) if wtype == "storm" else (170, 205, 255)
            for idx in range(count):
                sx = (idx * 53 + anim_tick * (3 if wtype == "storm" else 1)) % VIEW_W
                sy = (idx * 37 + anim_tick * spd) % VIEW_H
                pygame.draw.line(screen, col, (sx, sy), (sx + dx_off, sy + dy_len), 1)
    pt = getattr(session, "pending_transition", None)
    if isinstance(pt, dict) and not pt.get("prepared", False) and int(pt.get("frames", 0)) > 0:
        total_f = max(1, int(pt.get("max_frames", 20) or 20))
        ratio = max(0.0, min(1.0, int(pt.get("frames", 0)) / float(total_f)))
        tname = str(pt.get("name", "") or "")
        t_img = load_img(f"Transitions/{tname}") if (load_img and tname) else None
        if t_img is not None:
            wipe_mask = _build_transition_wipe_surface(pygame, t_img, ratio)
            screen.blit(wipe_mask, (0, 0))
        else:
            wipe_w = int(round(VIEW_W * ratio))
            wipe_surf = _get_alpha_overlay(pygame, VIEW_W, VIEW_H, (0, 0, 0, int(round(230 * ratio))))
            screen.blit(wipe_surf, (0, 0), pygame.Rect(0, 0, max(1, wipe_w), VIEW_H))
    if session.screen_faded_out:
        screen.fill((0, 0, 0))
    if session.timer_active:
        mins, secs = divmod(max(0, int(session.timer_seconds)), 60)
        badge = f"⏱ {mins:02d}:{secs:02d}"
        pygame.draw.rect(screen, (24, 24, 38), (VIEW_W - 92, 28, 82, 22))
        pygame.draw.rect(screen, (255, 205, 95), (VIEW_W - 92, 28, 82, 22), 1)
        screen.blit(_render_text(small_font, badge, True, (255, 225, 130)), (VIEW_W - 84, 31))


def _draw_pictures(
    pygame: Any,
    screen: Any,
    load_img: Any,
    small_font: Any,
    session: HeadlessGameSession,
    pic_cache: Optional[Dict[Tuple[Any, ...], Any]] = None,
) -> None:
    """Renders active RMXP picture overlays with zoom, rotation, blend_type, tone tinting, and surface cache."""
    if not session.pictures:
        return
    for num in sorted(session.pictures):
        pic = session.pictures[num]
        opacity = max(0, min(255, int(pic.get("render_op", pic.get("opacity", 255)))))
        if opacity <= 0:
            continue
        pname = str(pic.get("name", ""))
        px = int(pic.get("render_x", pic.get("x", 0)))
        py = int(pic.get("render_y", pic.get("y", 0)))
        origin = int(pic.get("origin", 0))
        zx = max(10, int(pic.get("render_zx", pic.get("zoom_x", 100))))
        zy = max(10, int(pic.get("render_zy", pic.get("zoom_y", 100))))
        angle = float(pic.get("angle", 0.0) or 0.0)
        blend_type = int(pic.get("blend_type", 0) or 0)
        b_flags = getattr(pygame, "BLEND_ADD", 0) if blend_type == 1 else (getattr(pygame, "BLEND_SUB", 0) if blend_type == 2 else 0)
        tone = pic.get("tone")
        tone_key = tuple(int(v) for v in tone[:4]) if isinstance(tone, list) else ()
        img = load_img(f"Pictures/{pname}") if pname else None
        if img is not None:
            ckey = (pname, id(img), tone_key, zx, zy, round(angle, 1), opacity)
            surf = pic_cache.get(ckey) if pic_cache is not None else None
            if surf is None:
                surf = img
                if tone_key and any(v != 0 for v in tone_key[:3]):
                    surf = _tint_surface(pygame, surf, list(tone_key))
                if zx != 100 or zy != 100:
                    surf = pygame.transform.smoothscale(
                        surf,
                        (max(1, surf.get_width() * zx // 100), max(1, surf.get_height() * zy // 100)),
                    )
                if abs(angle) > 0.01:
                    surf = pygame.transform.rotate(surf, -angle)
                if opacity < 255:
                    surf = surf.copy()
                    surf.set_alpha(opacity)
                if pic_cache is not None:
                    if len(pic_cache) > 64:
                        pic_cache.clear()
                    pic_cache[ckey] = surf
            dx = px - surf.get_width() // 2 if origin == 1 else px
            dy = py - surf.get_height() // 2 if origin == 1 else py
            screen.blit(surf, (dx, dy), special_flags=b_flags)
        else:
            box_w = max(40, (180 * zx) // 100)
            box_h = max(20, (32 * zy) // 100)
            ov = pygame.Surface((box_w, box_h), pygame.SRCALPHA)
            ov.fill((28, 36, 54, min(220, opacity)))
            pygame.draw.rect(ov, (190, 210, 245, opacity), (0, 0, box_w, box_h), 1)
            ov.blit(_render_text(small_font, f"[{pname or f'Pic#{num}'}]", True, (240, 245, 255)), (10, 7))
            if abs(angle) > 0.01:
                ov = pygame.transform.rotate(ov, -angle)
            dx = px - ov.get_width() // 2 if origin == 1 else px
            dy = py - ov.get_height() // 2 if origin == 1 else py
            screen.blit(ov, (dx, dy), special_flags=b_flags)


_WINDOWSKIN_PRESETS: Dict[str, Dict[str, Tuple[int, int, int]]] = {
    "blue": {"bg": (22, 34, 68), "border": (165, 200, 245), "panel_bg": (18, 28, 56), "accent": (255, 220, 105)},
    "red": {"bg": (56, 20, 28), "border": (235, 145, 145), "panel_bg": (44, 16, 24), "accent": (255, 215, 120)},
    "green": {"bg": (18, 46, 32), "border": (145, 225, 170), "panel_bg": (14, 38, 26), "accent": (245, 230, 120)},
    "gold": {"bg": (48, 38, 18), "border": (240, 205, 115), "panel_bg": (38, 30, 14), "accent": (255, 240, 165)},
    "dark": {"bg": (12, 12, 18), "border": (135, 140, 160), "panel_bg": (10, 10, 15), "accent": (220, 225, 245)},
    "default": {"bg": (20, 24, 36), "border": (200, 210, 235), "panel_bg": (18, 22, 34), "accent": (255, 220, 95)},
}


def resolve_windowskin_theme(
    windowskin_name: str,
    load_img: Any = None,
    cache: Optional[Dict[str, Dict[str, Tuple[int, int, int]]]] = None,
) -> Dict[str, Tuple[int, int, int]]:
    """Resolves UI box colors from an RMXP Windowskin PNG or named skin preset."""
    key = str(windowskin_name or "").strip()
    if cache is not None and key in cache:
        return cache[key]
    theme: Optional[Dict[str, Tuple[int, int, int]]] = None
    if key and load_img is not None:
        img = load_img(f"Windowskins/{key}")
        if img is not None and img.get_width() >= 132 and img.get_height() >= 32:
            c_bg = img.get_at((16, 16))
            c_bd = img.get_at((128, 2))
            bg = (max(8, min(90, int(c_bg[0]))), max(8, min(90, int(c_bg[1]))), max(12, min(110, int(c_bg[2]))))
            bd = (max(100, int(c_bd[0])), max(100, int(c_bd[1])), max(100, int(c_bd[2])))
            theme = {"bg": bg, "border": bd, "panel_bg": bg, "accent": (255, 220, 95)}
    if theme is None:
        low = key.lower()
        chosen = _WINDOWSKIN_PRESETS["default"]
        for tag in ("blue", "red", "green", "gold", "dark"):
            if tag in low:
                chosen = _WINDOWSKIN_PRESETS[tag]
                break
        theme = dict(chosen)
    if cache is not None:
        cache[key] = theme
    return theme


_SCALED_SURFACE_CACHE: Dict[Tuple[int, int, int], Tuple[Any, Any]] = {}
_WINDOW_BOX_CACHE: Dict[Tuple[int, int, int], Tuple[Any, Any]] = {}
_TEXT_SURFACE_CACHE: Dict[Tuple[int, str, bool, Tuple[int, ...]], Tuple[Any, Any]] = {}
_ALPHA_OVERLAY_CACHE: Dict[Tuple[int, int, int, Tuple[int, int, int, int]], Tuple[Any, Any]] = {}
_FACE_PORTRAIT_CACHE: Dict[Tuple[int, int, int], Tuple[Any, Any, Any]] = {}


def _render_text(font: Any, text: str, antialias: bool, color: Tuple[int, ...]) -> Any:
    """Returns a cached rendered text surface keyed by font identity, string, AA, and color."""
    txt = str(text)
    aa = bool(antialias)
    col = tuple(int(c) for c in color)
    ck = (id(font), txt, aa, col)
    entry = _TEXT_SURFACE_CACHE.get(ck)
    if entry is not None and entry[1] is font:
        return entry[0]
    if len(_TEXT_SURFACE_CACHE) >= 512:
        _TEXT_SURFACE_CACHE.clear()
    surf = font.render(txt, aa, col)
    _TEXT_SURFACE_CACHE[ck] = (surf, font)
    return surf


def _get_alpha_overlay(pygame: Any, width: int, height: int, rgba: Tuple[int, int, int, int]) -> Any:
    """Returns a cached read-only SRCALPHA curtain surface filled with rgba."""
    tw = max(1, int(width))
    th = max(1, int(height))
    col = (int(rgba[0]), int(rgba[1]), int(rgba[2]), int(rgba[3]))
    ck = (id(pygame), tw, th, col)
    entry = _ALPHA_OVERLAY_CACHE.get(ck)
    if entry is not None and entry[1] is pygame:
        return entry[0]
    if len(_ALPHA_OVERLAY_CACHE) >= 64:
        _ALPHA_OVERLAY_CACHE.clear()
    surf = pygame.Surface((tw, th), pygame.SRCALPHA)
    surf.fill(col)
    _ALPHA_OVERLAY_CACHE[ck] = (surf, pygame)
    return surf


def _get_face_portrait(pygame: Any, f_img: Any, face_index: int) -> Any:
    """Returns a cached 92x92 smoothscaled dialogue face portrait from an RMXP Faces sheet."""
    fidx = int(face_index)
    ck = (id(pygame), id(f_img), fidx)
    entry = _FACE_PORTRAIT_CACHE.get(ck)
    if entry is not None and entry[1] is f_img and entry[2] is pygame:
        return entry[0]
    fw, fh = f_img.get_width(), f_img.get_height()
    if fw >= 384 and fh >= 192:
        cols = max(1, fw // 96)
        sx = (fidx % cols) * 96
        sy = (fidx // cols) * 96
        src = f_img.subsurface(pygame.Rect(sx, sy, min(96, fw - sx), min(96, fh - sy)))
    else:
        src = f_img
    scaled = pygame.transform.smoothscale(src, (92, 92))
    if len(_FACE_PORTRAIT_CACHE) >= 64:
        _FACE_PORTRAIT_CACHE.clear()
    _FACE_PORTRAIT_CACHE[ck] = (scaled, f_img, pygame)
    return scaled


def _clamp_frame_dt(raw_dt: float, target_fps: float = TARGET_FPS) -> float:
    """Clamps per-frame delta time (ms) to avoid jump/stutter after OS window drags."""
    if raw_dt <= 0.0:
        return 1000.0 / max(1.0, float(target_fps))
    return min(float(raw_dt), 50.0)


def _draw_gray_pattern_rect(
    pygame: Any,
    surf: Any,
    rect: Tuple[int, int, int, int],
    seg: int = 6,
    width: int = 2,
) -> None:
    """Draws an alternating light/dark gray dashed border matching the unified preview style."""
    rx, ry, rw, rh = int(rect[0]), int(rect[1]), max(2, int(rect[2])), max(2, int(rect[3]))
    step = max(2, int(seg))
    for i, x0 in enumerate(range(rx, rx + rw, step)):
        x1 = min(rx + rw - 1, x0 + step)
        col = PREVIEW_GRAY_LIGHT_RGB if (i % 2 == 0) else PREVIEW_GRAY_DARK_RGB
        pygame.draw.line(surf, col, (x0, ry), (x1, ry), width)
        pygame.draw.line(surf, col, (x0, ry + rh - 1), (x1, ry + rh - 1), width)
    for i, y0 in enumerate(range(ry, ry + rh, step)):
        y1 = min(ry + rh - 1, y0 + step)
        col = PREVIEW_GRAY_LIGHT_RGB if (i % 2 == 0) else PREVIEW_GRAY_DARK_RGB
        pygame.draw.line(surf, col, (rx, y0), (rx, y1), width)
        pygame.draw.line(surf, col, (rx + rw - 1, y0), (rx + rw - 1, y1), width)


def _get_scaled_surface(pygame: Any, src_surf: Any, target_size: Tuple[int, int]) -> Any:
    """Returns a cached smoothscaled surface keyed by source identity and target size."""
    tw = max(1, int(target_size[0]))
    th = max(1, int(target_size[1]))
    if src_surf.get_size() == (tw, th):
        return src_surf
    ck = (id(src_surf), tw, th)
    entry = _SCALED_SURFACE_CACHE.get(ck)
    if entry is not None and entry[1] is src_surf:
        return entry[0]
    if len(_SCALED_SURFACE_CACHE) >= 64:
        _SCALED_SURFACE_CACHE.clear()
    scaled = pygame.transform.smoothscale(src_surf, (tw, th))
    _SCALED_SURFACE_CACHE[ck] = (scaled, src_surf)
    return scaled


def _draw_window_box(
    pygame: Any,
    screen: Any,
    rect: Tuple[int, int, int, int],
    ui_theme: Dict[str, Tuple[int, int, int]],
    load_img: Optional[Any] = None,
    windowskin_name: str = "",
) -> None:
    """Draws an RMXP Windowskin 9-slice frame when available, falling back to themed vector rect."""
    rx, ry, rw, rh = int(rect[0]), int(rect[1]), int(rect[2]), int(rect[3])
    ws_img = load_img(f"Windowskins/{windowskin_name}") if (load_img and windowskin_name) else None
    if ws_img is not None and ws_img.get_width() >= 192 and ws_img.get_height() >= 128 and rw >= 32 and rh >= 32:
        wkey = (id(ws_img), rw, rh)
        cached = _WINDOW_BOX_CACHE.get(wkey)
        if cached is not None and cached[1] is ws_img:
            screen.blit(cached[0], (rx, ry))
            return
        box_surf = pygame.Surface((rw, rh), pygame.SRCALPHA)
        bg_patch = ws_img.subsurface(pygame.Rect(0, 0, 128, 128))
        bg_scaled = pygame.transform.smoothscale(bg_patch, (max(1, rw - 4), max(1, rh - 4)))
        bg_scaled.set_alpha(230)
        box_surf.blit(bg_scaled, (2, 2))
        corners = (
            ((128, 0, 16, 16), (0, 0)),
            ((176, 0, 16, 16), (rw - 16, 0)),
            ((128, 48, 16, 16), (0, rh - 16)),
            ((176, 48, 16, 16), (rw - 16, rh - 16)),
        )
        top_e = pygame.transform.scale(ws_img.subsurface(pygame.Rect(144, 0, 32, 16)), (max(1, rw - 32), 16))
        bot_e = pygame.transform.scale(ws_img.subsurface(pygame.Rect(144, 48, 32, 16)), (max(1, rw - 32), 16))
        left_e = pygame.transform.scale(ws_img.subsurface(pygame.Rect(128, 16, 16, 32)), (16, max(1, rh - 32)))
        right_e = pygame.transform.scale(ws_img.subsurface(pygame.Rect(176, 16, 16, 32)), (16, max(1, rh - 32)))
        if rw > 32:
            box_surf.blit(top_e, (16, 0))
            box_surf.blit(bot_e, (16, rh - 16))
        if rh > 32:
            box_surf.blit(left_e, (0, 16))
            box_surf.blit(right_e, (rw - 16, 16))
        for src_r, dest_xy in corners:
            box_surf.blit(ws_img, dest_xy, pygame.Rect(*src_r))
        if len(_WINDOW_BOX_CACHE) >= 64:
            _WINDOW_BOX_CACHE.clear()
        _WINDOW_BOX_CACHE[wkey] = (box_surf, ws_img)
        screen.blit(box_surf, (rx, ry))
        return
    pygame.draw.rect(screen, ui_theme["bg"], (rx, ry, rw, rh))
    pygame.draw.rect(screen, ui_theme["border"], (rx, ry, rw, rh), 2)


def _draw_minimap(
    pygame: Any,
    screen: Any,
    session: HeadlessGameSession,
    theme: Optional[Dict[str, Tuple[int, int, int]]] = None,
    minimap_cache: Optional[Dict[Tuple[int, ...], Any]] = None,
) -> None:
    """Draws a compact top-right minimap with cached static terrain surface and live dynamic pins."""
    mm = session.get_minimap_grid()
    mw, mh = max(1, int(mm["width"])), max(1, int(mm["height"]))
    cell_px = max(2, min(6, min(140 // mw, 100 // mh)))
    box_w, box_h = mw * cell_px, mh * cell_px
    ox = VIEW_W - box_w - 12
    oy = 56 if session.timer_active else 32
    bg_col = theme["panel_bg"] if theme else (14, 18, 28)
    bd_col = theme["border"] if theme else (140, 165, 210)
    pygame.draw.rect(screen, bg_col, (ox - 2, oy - 2, box_w + 4, box_h + 4))
    pygame.draw.rect(screen, bd_col, (ox - 2, oy - 2, box_w + 4, box_h + 4), 1)
    rev = session.map_revision(session.map_id) if hasattr(session, "map_revision") else 0
    ckey = (int(session.map_id), int(rev), int(cell_px))
    base_surf = minimap_cache.get(ckey) if minimap_cache is not None else None
    if base_surf is None:
        base_surf = pygame.Surface((box_w, box_h))
        palette = {0: (58, 86, 62), 1: (32, 34, 44), 2: (46, 98, 165)}
        for y, row in enumerate(mm["cells"]):
            for x, code in enumerate(row):
                pygame.draw.rect(base_surf, palette.get(code, (58, 86, 62)), (x * cell_px, y * cell_px, cell_px, cell_px))
        if minimap_cache is not None:
            minimap_cache.clear()
            minimap_cache[ckey] = base_surf
    screen.blit(base_surf, (ox, oy))
    for ex, ey in mm["events"]:
        pygame.draw.rect(screen, (245, 205, 70), (ox + ex * cell_px, oy + ey * cell_px, cell_px, cell_px))
    for _vt, vx, vy in mm["vehicles"]:
        pygame.draw.rect(screen, (110, 225, 245), (ox + vx * cell_px, oy + vy * cell_px, cell_px, cell_px))
    px, py = mm["player"]
    pygame.draw.rect(screen, (255, 255, 255), (ox + px * cell_px, oy + py * cell_px, cell_px, cell_px))


RMXP_TEXT_COLORS: Tuple[Tuple[int, int, int], ...] = (
    (255, 255, 255),  # 0: white
    (120, 185, 255),  # 1: blue
    (255, 115, 115),  # 2: red
    (130, 235, 145),  # 3: green
    (115, 235, 240),  # 4: cyan
    (235, 145, 245),  # 5: magenta
    (255, 225, 105),  # 6: yellow
    (175, 180, 195),  # 7: gray
)


def wrap_text_to_width(text: str, font: Any, max_width: int) -> List[str]:
    """Wraps Korean/Latin text lines to fit within max_width pixels using font metrics."""
    if not text:
        return [""]
    limit = max(32, int(max_width))
    out: List[str] = []
    for para in str(text).split("\n"):
        if not para:
            out.append("")
            continue
        cur = ""
        for ch in para:
            cand = cur + ch
            w = font.size(cand)[0] if hasattr(font, "size") else len(cand) * 10
            if cur and w > limit:
                out.append(cur.rstrip())
                cur = ch.lstrip() if ch == " " else ch
            else:
                cur = cand
        if cur:
            out.append(cur)
    return out or [""]


def wrap_colored_segments(
    segments: List[Tuple[str, int]],
    font: Any,
    max_width: int,
) -> List[List[Tuple[str, int]]]:
    """Wraps colored message segments across lines without breaking color spans."""
    limit = max(32, int(max_width))
    rows: List[List[Tuple[str, int]]] = [[]]
    row_w = 0
    for seg_txt, col_idx in segments:
        buf = ""
        for ch in seg_txt:
            if ch == "\n":
                if buf:
                    rows[-1].append((buf, col_idx))
                    buf = ""
                rows.append([])
                row_w = 0
                continue
            cw = font.size(buf + ch)[0] if hasattr(font, "size") else len(buf + ch) * 10
            if (rows[-1] or buf) and row_w + cw > limit:
                if buf:
                    rows[-1].append((buf, col_idx))
                rows.append([])
                row_w = 0
                buf = "" if ch == " " else ch
            else:
                buf += ch
        if buf:
            bw = font.size(buf)[0] if hasattr(font, "size") else len(buf) * 10
            rows[-1].append((buf, col_idx))
            row_w += bw
    return [r for r in rows if r] or [[("", 0)]]


def typewriter_chars_for_elapsed(
    total_chars: int,
    elapsed_ms: float,
    text_speed: int = 2,
) -> int:
    """Computes revealed glyph count for typewriter effect given elapsed ms and speed (1=slow, 2=med, 3=fast, 4=instant)."""
    spd = max(1, min(4, int(text_speed)))
    if spd >= 4 or elapsed_ms >= 1e8:
        return max(0, int(total_chars))
    cps = {1: 24.0, 2: 48.0, 3: 90.0}.get(spd, 48.0)
    shown = int(max(0.0, float(elapsed_ms)) * cps / 1000.0) + 1
    return max(0, min(int(total_chars), shown))


def _draw_panorama(
    pygame: Any,
    screen: Any,
    load_img: Any,
    ts_fields: Dict[str, Any],
    cam_x: int,
    cam_y: int,
    map_ir: Optional[Dict[str, Any]] = None,
    anim_tick: int = 0,
) -> None:
    """Draws tiled parallax panorama backdrop from map parallax_name or tileset panorama_name."""
    m_obj = map_ir or {}
    pname = str(m_obj.get("parallax_name") or ts_fields.get("panorama_name") or "").strip()
    if not pname:
        return
    p_img = load_img(f"Panoramas/{pname}")
    if p_img is None:
        return
    pw, ph = max(1, p_img.get_width()), max(1, p_img.get_height())
    sx_drift = (anim_tick * int(m_obj.get("parallax_sx", 0) or 0)) // 4 if m_obj.get("parallax_loop_x", True) else 0
    sy_drift = (anim_tick * int(m_obj.get("parallax_sy", 0) or 0)) // 4 if m_obj.get("parallax_loop_y", True) else 0
    ox = ((-cam_x // 2) + sx_drift) % pw - pw
    oy = ((-cam_y // 2) + sy_drift) % ph - ph
    for ty in range(oy, VIEW_H, ph):
        for tx in range(ox, VIEW_W, pw):
            screen.blit(p_img, (tx, ty))


def _draw_fog(
    pygame: Any,
    screen: Any,
    load_img: Any,
    session: HeadlessGameSession,
    cam_x: int,
    cam_y: int,
    fog_cache: Optional[Dict[Tuple[Any, ...], Any]] = None,
) -> None:
    """Draws scrolling semi-transparent fog layer above map and actors using cached fog surfaces."""
    fg = getattr(session, "fog_state", None)
    if not isinstance(fg, dict) or not fg.get("name"):
        return
    op = max(0, min(255, int(fg.get("opacity", 64))))
    if op <= 0:
        return
    fname = str(fg.get("name", ""))
    f_img = load_img(f"Fogs/{fname}")
    sx_off = int(round(float(fg.get("scroll_x", 0.0))))
    sy_off = int(round(float(fg.get("scroll_y", 0.0))))
    if f_img is not None:
        tone = fg.get("tone")
        tone_key = tuple(int(v) for v in tone[:4]) if isinstance(tone, list) else ()
        ckey = (fname, id(f_img), tone_key, op)
        surf = fog_cache.get(ckey) if fog_cache is not None else None
        if surf is None:
            base = _tint_surface(pygame, f_img, list(tone_key)) if (tone_key and any(v != 0 for v in tone_key[:3])) else f_img
            surf = base.copy()
            surf.set_alpha(op)
            if fog_cache is not None:
                fog_cache[ckey] = surf
        fw, fh = max(1, surf.get_width()), max(1, surf.get_height())
        ox = (-(cam_x - sx_off)) % fw - fw
        oy = (-(cam_y - sy_off)) % fh - fh
        for ty in range(oy, VIEW_H, fh):
            for tx in range(ox, VIEW_W, fw):
                screen.blit(surf, (tx, ty))
    else:
        tone = fg.get("tone", [0, 0, 0, 0])
        r = max(0, min(255, 200 + int(tone[0]) // 3)) if len(tone) > 0 else 200
        g = max(0, min(255, 210 + int(tone[1]) // 3)) if len(tone) > 1 else 210
        b = max(0, min(255, 225 + int(tone[2]) // 3)) if len(tone) > 2 else 225
        ov_key = ("solid", r, g, b, op)
        ov = fog_cache.get(ov_key) if fog_cache is not None else None
        if ov is None:
            ov = pygame.Surface((VIEW_W, VIEW_H), pygame.SRCALPHA)
            ov.fill((r, g, b, min(140, op // 2)))
            if fog_cache is not None:
                fog_cache[ov_key] = ov
        screen.blit(ov, (0, 0))


def _render_animation_frame_cells(
    pygame: Any,
    screen: Any,
    load_img: Optional[Any],
    anim_entry: Any,
    frame_idx: int,
    sx: int,
    sy: int,
    fallback_col: Tuple[int, int, int] = (255, 230, 110),
    elapsed_frames: int = 1,
) -> None:
    """Shared RMXP Animations/ sprite-sheet cell renderer for both map and battle scenes."""
    cells = extract_animation_frame_cells(anim_entry, frame_idx)
    sheet_name = str(anim_entry.get("fields", {}).get("animation_name", "")) if isinstance(anim_entry, dict) else ""
    sheet_img = load_img(f"Animations/{sheet_name}") if (load_img and sheet_name) else None
    if cells:
        for cell in cells:
            cx = sx + cell["x"]
            cy = sy + cell["y"]
            if sheet_img is not None:
                pat = cell["pattern"]
                rx = (pat % 5) * 192
                ry = (pat // 5) * 192
                if rx + 192 <= sheet_img.get_width() and ry + 192 <= sheet_img.get_height():
                    sub = sheet_img.subsurface(pygame.Rect(rx, ry, 192, 192))
                    if cell["mirror"]:
                        sub = pygame.transform.flip(sub, True, False)
                    if cell["zoom"] != 100 or cell["angle"] != 0:
                        sub = pygame.transform.rotozoom(sub, cell["angle"], cell["zoom"] / 100.0)
                    dest = (cx - sub.get_width() // 2, cy - sub.get_height() // 2)
                    blend = int(cell.get("blend_type", 0))
                    if blend in (1, 2) and hasattr(sub, "premul_alpha"):
                        # RMXP blend_type 1 = additive, 2 = subtractive (alpha premultiplied so clear pixels add nothing)
                        lit = sub.premul_alpha()
                        if cell["opacity"] < 255:
                            op = cell["opacity"]
                            lit.fill((op, op, op, 255), special_flags=pygame.BLEND_RGBA_MULT)
                        flag = pygame.BLEND_RGB_ADD if blend == 1 else pygame.BLEND_RGB_SUB
                        screen.blit(lit, dest, special_flags=flag)
                        continue
                    if cell["opacity"] < 255:
                        sub = sub.copy()
                        sub.set_alpha(cell["opacity"])
                    screen.blit(sub, dest)
                    continue
            c_rad = max(4, (14 * cell["zoom"]) // 100)
            pygame.draw.circle(screen, fallback_col, (cx, cy), c_rad, 2)
    else:
        radius = max(6, elapsed_frames * 3)
        pygame.draw.circle(screen, fallback_col, (sx, sy), radius, 2)


def _draw_battle_scene(
    pygame: Any,
    screen: Any,
    small_font: Any,
    load_img: Optional[Any],
    session: HeadlessGameSession,
    bs: Dict[str, Any],
    hue_cache: Optional[Dict[Tuple[int, int], Any]] = None,
) -> None:
    """Renders Battlebacks/ backdrop, Battlers/ enemy sprites (cached hue), battle animations, and damage popups."""
    m = session.current_map()
    ts_fields = getattr(session, "tilesets_db", {}).get(int(m.get("tileset_id", 1)), {})
    bback_name = str(bs.get("battleback_name") or ts_fields.get("battleback_name") or "")
    bb_img = load_img(f"Battlebacks/{bback_name}") if (load_img and bback_name) else None
    if bb_img is not None:
        screen.blit(_get_scaled_surface(pygame, bb_img, (VIEW_W, VIEW_H)), (0, 0))
    else:
        pygame.draw.rect(screen, (20, 24, 38), (0, 24, VIEW_W, VIEW_H - 24))
        pygame.draw.ellipse(screen, (34, 44, 64), (70, 175, VIEW_W - 140, 110))
    living = [e for e in bs.get("enemies", []) if int(e.get("hp", 0)) > 0 and not e.get("hidden", False)]
    enemy_centers: Dict[str, Tuple[int, int]] = {}
    shown = living[:4]
    for idx_e, foe in enumerate(shown):
        cx = int(VIEW_W * (idx_e + 1) / (len(shown) + 1))
        cy = 185 + (idx_e % 2) * 18
        enemy_centers[str(foe.get("name", ""))] = (cx, cy)
        bname = str(foe.get("battler_name", "") or "")
        bhue = int(foe.get("battler_hue", 0) or 0)
        b_img = load_img(f"Battlers/{bname}") if (load_img and bname) else None
        if b_img is not None:
            if bhue % 360 != 0:
                b_img = apply_hue_shift(pygame, b_img, bhue, cache=hue_cache)
            screen.blit(b_img, (cx - b_img.get_width() // 2, cy - b_img.get_height() // 2))
        else:
            pygame.draw.circle(screen, (185, 72, 86), (cx, cy), 28)
            pygame.draw.circle(screen, (245, 210, 215), (cx, cy), 28, 2)
            lbl = _render_text(small_font, str(foe.get("name", "Enemy")), True, (255, 245, 245))
            screen.blit(lbl, (cx - lbl.get_width() // 2, cy + 32))
    for pop in bs.get("floating_popups", []):
        frames_left = max(1, int(pop.get("frames", 30)))
        drift = (36 - min(36, frames_left)) // 2
        px, py = enemy_centers.get(str(pop.get("target", "")), (VIEW_W // 2, 180))
        kind = str(pop.get("kind", "damage"))
        pcol = (120, 245, 145) if kind == "heal" else ((255, 225, 95) if kind == "crit" else ((200, 205, 220) if kind == "miss" else (255, 120, 120)))
        txt = _render_text(small_font, str(pop.get("text", "")), True, pcol)
        screen.blit(txt, (px - txt.get_width() // 2, py - 42 - drift))
    for ba in getattr(session, "pending_battle_animations", []):
        idx_t = int(ba.get("target_index", 0))
        cx, cy = (enemy_centers.get(str(shown[idx_t].get("name", "")), (VIEW_W // 2, 180)) if 0 <= idx_t < len(shown) else (VIEW_W // 2, 180))
        aid = max(1, int(ba.get("animation_id", 1)))
        anim_entry = getattr(session, "animations_db", {}).get(aid, {})
        frames_left = max(1, int(ba.get("frames", 12)))
        total_cue = max(frames_left, int(ba.get("total_frames", 24)))
        elapsed = max(0, total_cue - frames_left)
        frames_list = anim_entry.get("fields", {}).get("frames", []) if isinstance(anim_entry, dict) else []
        f_idx = min(len(frames_list) - 1, elapsed // ANIMATION_TICKS_PER_FRAME) if frames_list else 0
        _render_animation_frame_cells(
            pygame, screen, load_img, anim_entry, f_idx, cx, cy,
            fallback_col=(255, 230, 110), elapsed_frames=max(2, elapsed),
        )


def _draw_gameover_overlay(
    pygame: Any,
    screen: Any,
    font: Any,
    small_font: Any,
    load_img: Optional[Any],
    session: HeadlessGameSession,
) -> None:
    """Draws Gameovers/ backdrop or clean dark curtain with retry guidance when party is defeated."""
    go_img = load_img("Gameovers/Gameover") if load_img else None
    if go_img is not None:
        screen.blit(_get_scaled_surface(pygame, go_img, (VIEW_W, VIEW_H)), (0, 0))
    else:
        screen.blit(_get_alpha_overlay(pygame, VIEW_W, VIEW_H, (12, 8, 12, 210)), (0, 0))
    title = _render_text(font, "GAME OVER", True, (245, 95, 95))
    sub = _render_text(small_font, "Enter/R: 최근 저장 복귀 | F5: 처음부터 재시작", True, (235, 235, 245))
    screen.blit(title, ((VIEW_W - title.get_width()) // 2, VIEW_H // 2 - 28))
    screen.blit(sub, ((VIEW_W - sub.get_width()) // 2, VIEW_H // 2 + 12))


def _draw_overlays(
    pygame: Any,
    screen: Any,
    font: Any,
    small_font: Any,
    session: HeadlessGameSession,
    choice_cursor: int,
    show_status: bool,
    status_cursor: int = 0,
    status_actor_idx: int = 0,
    active_slot: int = 1,
    toast_text: str = "",
    status_mode: str = "equip",
    hud_cache: Optional[Dict[str, Any]] = None,
    theme: Optional[Dict[str, Tuple[int, int, int]]] = None,
    out_choice_rects: Optional[List[Tuple[int, int, int, int]]] = None,
    load_img: Optional[Any] = None,
    dialogue_elapsed_ms: float = 1e9,
    text_speed: int = 2,
    hue_cache: Optional[Dict[Tuple[int, int], Any]] = None,
    out_status_rects: Optional[List[Tuple[str, Any, Tuple[int, int, int, int]]]] = None,
) -> None:
    """Draws the top HUD banner, optional status panel (with mouse hits & stat deltas), toast, and dialogue window."""
    ui_theme = theme or _WINDOWSKIN_PRESETS["default"]
    m = session.current_map()
    hud = format_hud_text(str(m["name"]), session.snapshot(), active_slot=active_slot)
    pygame.draw.rect(screen, (15, 17, 24, 210), (0, 0, VIEW_W, 24))
    if hud_cache is not None and hud_cache.get("text") == hud and hud_cache.get("surf") is not None:
        hud_surf = hud_cache["surf"]
    else:
        hud_surf = _render_text(small_font, hud, True, (230, 235, 245))
        if hud_cache is not None:
            hud_cache["text"] = hud
            hud_cache["surf"] = hud_surf
    screen.blit(hud_surf, (8, 4))

    if show_status and not session.pending:
        party = session.party_member_ids or [session.lead_actor_id]
        target_aid = party[status_actor_idx % len(party)]
        status_lines = format_status_lines(session, actor_id=target_aid)
        if status_mode == "use":
            usable_entries = list_field_skills_and_items_for_actor(session, actor_id=target_aid)
            panel_items = [(0, oid, label, 1) for _kind, oid, label in usable_entries]
        else:
            panel_items = list_bag_equips_for_lead(session, actor_id=target_aid)
        sel_idx, win_start, win_end = compute_status_equip_window(len(panel_items), status_cursor)
        visible_equips = panel_items[win_start:win_end]
        has_delta_row = (status_mode == "equip" and bool(panel_items))
        extra_rows = (1 + len(visible_equips) + (1 if has_delta_row else 0)) if panel_items else 1
        box_h = 20 + (len(status_lines) + extra_rows) * 20
        pygame.draw.rect(screen, ui_theme["panel_bg"], (16, 32, VIEW_W - 32, box_h))
        pygame.draw.rect(screen, ui_theme["border"], (16, 32, VIEW_W - 32, box_h), 1)
        row_idx = 0
        for sline in status_lines:
            screen.blit(_render_text(small_font, sline, True, (235, 240, 248)), (26, 40 + row_idx * 20))
            row_idx += 1
        mode_tab = "[필드 스킬/아이템 사용 | Q/E·클릭: 장비]" if status_mode == "use" else "[소지 장비 장착 | Q/E·클릭: 사용]"
        party_hint = " / P: 선두교대" if len(party) > 1 else ""
        scroll_hint = ""
        if len(panel_items) > len(visible_equips):
            up_m = "▲" if win_start > 0 else " "
            dn_m = "▼" if win_end < len(panel_items) else " "
            scroll_hint = f" [{sel_idx + 1}/{len(panel_items)} {up_m}{dn_m}]"
        header = f"{mode_tab}: ↑↓ 선택 / Enter·클릭 실행{party_hint}{scroll_hint}"
        tab_y = 40 + row_idx * 20
        if out_status_rects is not None:
            out_status_rects.append(("toggle_tab", None, (24, tab_y, VIEW_W - 110, 20)))
            out_status_rects.append(("close", None, (VIEW_W - 82, 36, 58, 20)))
        pygame.draw.rect(screen, ui_theme["bg"], (VIEW_W - 82, 36, 58, 20))
        pygame.draw.rect(screen, ui_theme["border"], (VIEW_W - 82, 36, 58, 20), 1)
        screen.blit(_render_text(small_font, "닫기", True, ui_theme["accent"]), (VIEW_W - 68, 38))
        screen.blit(_render_text(small_font, header, True, (170, 200, 245)), (26, tab_y))
        row_idx += 1
        for offset_idx, (_eq_type, _eq_id, label, _cnt) in enumerate(visible_equips):
            b_idx = win_start + offset_idx
            is_sel = b_idx == sel_idx
            prefix = ">" if is_sel else " "
            col = (255, 220, 95) if is_sel else (210, 218, 232)
            ry = 40 + row_idx * 20
            if out_status_rects is not None:
                out_status_rects.append(("item", b_idx, (28, ry, VIEW_W - 56, 20)))
            screen.blit(_render_text(small_font, f"{prefix} {label}", True, col), (34, ry))
            row_idx += 1
        if has_delta_row and 0 <= sel_idx < len(panel_items):
            eq_type, eq_id, _lbl, _cnt = panel_items[sel_idx]
            delta_str = compute_equip_stat_delta(session, target_aid, eq_type, eq_id)
            screen.blit(_render_text(small_font, f"  ↳ 능력치 비교: {delta_str}", True, (135, 235, 175)), (34, 40 + row_idx * 20))

    if toast_text:
        toast_y = VIEW_H - 36 if not session.pending else 28
        pygame.draw.rect(screen, (26, 42, 68), (24, toast_y, VIEW_W - 48, 24))
        pygame.draw.rect(screen, (255, 215, 110), (24, toast_y, VIEW_W - 48, 24), 1)
        screen.blit(_render_text(small_font, toast_text, True, (255, 240, 185)), (34, toast_y + 4))

    if session.game_over and not session.pending:
        _draw_gameover_overlay(pygame, screen, font, small_font, load_img, session)
        return
    if not session.pending:
        return
    if session.pending.battle_state is not None:
        bs = session.pending.battle_state
        _draw_battle_scene(pygame, screen, small_font, load_img, session, bs, hue_cache=hue_cache)
        turn_order = session.get_battle_turn_order(bs)
        eff_tag = str(bs.get("last_eff_tag", "") or "")
        order_txt = f"턴 {bs.get('turn', 1)} | 순서: {' > '.join(turn_order[:4])}"
        pygame.draw.rect(screen, ui_theme["panel_bg"], (16, 26, VIEW_W - 32, 18))
        pygame.draw.rect(screen, ui_theme["border"], (16, 26, VIEW_W - 32, 18), 1)
        screen.blit(_render_text(small_font, order_txt, True, ui_theme["accent"]), (24, 27))
        if eff_tag:
            eff_colors = {
                "약점!": (255, 195, 65),
                "치명타!": (255, 130, 85),
                "반감": (150, 185, 235),
                "무효": (170, 175, 190),
                "흡수": (125, 240, 165),
                "MISS": (190, 195, 210),
            }
            ecol = eff_colors.get(eff_tag, (240, 240, 240))
            pill_surf = _render_text(small_font, f"[{eff_tag}]", True, ecol)
            screen.blit(pill_surf, (VIEW_W - pill_surf.get_width() - 26, 27))
        living_enemies = [e for e in bs.get("enemies", []) if int(e.get("hp", 0)) > 0 and not e.get("hidden", False)]
        if living_enemies:
            shown = living_enemies[:3]
            gap = 8
            card_w = (VIEW_W - 64 - gap * (len(shown) - 1)) // len(shown)
            eh = 52
            for idx_e, foe in enumerate(shown):
                ex = 32 + idx_e * (card_w + gap)
                pygame.draw.rect(screen, (28, 18, 24), (ex, 46, card_w, eh))
                pygame.draw.rect(screen, (220, 120, 120), (ex, 46, card_w, eh), 1)
                e_badge = session.format_state_badges(foe.get("states", []))
                elabel = f"{foe['name']}{e_badge} {int(foe['hp'])}/{int(foe['max_hp'])}"
                screen.blit(_render_text(small_font, elabel, True, (255, 225, 225)), (ex + 8, 52))
                ratio = max(0.0, min(1.0, int(foe["hp"]) / max(1, int(foe["max_hp"]))))
                pygame.draw.rect(screen, (60, 28, 34), (ex + 8, 78, card_w - 16, 10))
                pygame.draw.rect(screen, (220, 68, 68), (ex + 8, 78, int((card_w - 16) * ratio), 10))
        party_ids = session.party_member_ids or [session.lead_actor_id]
        defending_set = set(bs.get("defending", []))
        p_texts = []
        for aid in party_ids[:4]:
            ast = session._get_actor_state(aid)
            p_badge = session.format_state_badges(ast.get("states", []))
            if aid in defending_set:
                p_badge = f"{p_badge}[방어]"
            p_texts.append(f"{ast['name']}{p_badge} {int(ast['hp'])}/{int(ast['max_hp'])} ({int(ast['sp'])}SP)")
        if p_texts:
            pygame.draw.rect(screen, ui_theme["panel_bg"], (16, 102, VIEW_W - 32, 24))
            pygame.draw.rect(screen, ui_theme["border"], (16, 102, VIEW_W - 32, 24), 1)
            screen.blit(_render_text(small_font, " | ".join(p_texts), True, (220, 235, 255)), (24, 106))
    p_off = max(0, int(getattr(session.pending, "page_offset", 0)))
    visible_lines = session.pending.lines[p_off : p_off + 3]
    has_more_pages = (not session.pending.choices) and (p_off + 3 < len(session.pending.lines))
    choices = session.pending.choices
    safe_cursor = choice_cursor % len(choices) if choices else 0
    face_name = str(getattr(session.pending, "face_name", "") or "")
    face_index = int(getattr(session.pending, "face_index", 0) or 0)
    face_pad = 104 if face_name else 0
    wrapped_rows: List[List[Tuple[str, int]]] = []
    extra_wait = 0
    max_text_w = VIEW_W - 60 - face_pad
    for line in visible_lines:
        segs, ew = parse_message_segments(str(line))
        extra_wait += ew
        wrapped_rows.extend(wrap_colored_segments(segs, font, max_text_w))
    total_chars = sum(len(s) for r in wrapped_rows for s, _c in r)
    chars_budget = typewriter_chars_for_elapsed(total_chars, dialogue_elapsed_ms, text_speed=text_speed)
    text_rows = max(len(visible_lines), len(wrapped_rows))
    box_h = max(114 if not face_name else 120, 24 + (text_rows + len(choices)) * 22)
    tpos = int(getattr(session.pending, "text_position", 2))
    if tpos == 0:
        box_y = 32
    elif tpos == 1:
        box_y = max(32, (VIEW_H - box_h) // 2)
    else:
        box_y = max(32, VIEW_H - box_h - 16)
    show_frame = bool(getattr(session.pending, "show_frame", True))
    if show_frame:
        _draw_window_box(
            pygame,
            screen,
            (16, box_y, VIEW_W - 32, box_h),
            ui_theme,
            load_img=load_img,
            windowskin_name=str(getattr(session, "windowskin_name", "") or ""),
        )
    else:
        screen.blit(_get_alpha_overlay(pygame, VIEW_W - 32, box_h, (8, 10, 18, 150)), (16, box_y))
    if face_name:
        fx, fy = 26, box_y + 12
        f_img = load_img(f"Faces/{face_name}") if load_img else None
        if f_img is not None:
            screen.blit(_get_face_portrait(pygame, f_img, face_index), (fx, fy))
        else:
            pygame.draw.rect(screen, ui_theme["panel_bg"], (fx, fy, 92, 92))
            pygame.draw.rect(screen, ui_theme["border"], (fx, fy, 92, 92), 1)
            flbl = _render_text(small_font, face_name[:8], True, ui_theme["accent"])
            screen.blit(flbl, (fx + (92 - flbl.get_width()) // 2, fy + 38))
    y_off = box_y + 10
    rem_chars = chars_budget
    for seg_row in wrapped_rows:
        x_off = 28 + face_pad
        for seg_txt, col_idx in seg_row:
            if rem_chars <= 0:
                break
            vis_txt = seg_txt[:rem_chars]
            rem_chars -= len(vis_txt)
            tcol = RMXP_TEXT_COLORS[col_idx % len(RMXP_TEXT_COLORS)]
            rendered = _render_text(font, vis_txt, True, tcol)
            screen.blit(rendered, (x_off, y_off))
            x_off += rendered.get_width()
        y_off += 22
    if has_more_pages:
        screen.blit(_render_text(small_font, "▼ 다음", True, ui_theme["accent"]), (VIEW_W - 76, box_y + box_h - 22))
    for idx, ch in enumerate(choices):
        prefix = ">" if idx == safe_cursor else " "
        col = ui_theme["accent"] if idx == safe_cursor else (210, 215, 225)
        if out_choice_rects is not None:
            out_choice_rects.append((28 + face_pad, y_off, VIEW_W - 56 - face_pad, 22))
        screen.blit(_render_text(font, f"{prefix} {ch}", True, col), (36 + face_pad, y_off))
        y_off += 22
    if out_choice_rects is not None and not choices:
        out_choice_rects.append((16, box_y, VIEW_W - 32, box_h))
    if session.pending.number_input is not None:
        ni = session.pending.number_input
        digits = max(1, min(8, int(ni.get("digits", 4))))
        val_str = f"{int(ni.get('value', 0)):0{digits}d}"
        dial_y = max(28, box_y - 38)
        dial_w = digits * 26 + 170
        pygame.draw.rect(screen, ui_theme["panel_bg"], (16, dial_y, dial_w, 32))
        pygame.draw.rect(screen, ui_theme["border"], (16, dial_y, dial_w, 32), 1)
        for d_idx, d_ch in enumerate(val_str):
            dx = 24 + d_idx * 24
            pygame.draw.rect(screen, (18, 22, 34), (dx, dial_y + 4, 20, 24))
            pygame.draw.rect(screen, ui_theme["accent"], (dx, dial_y + 4, 20, 24), 1)
            screen.blit(_render_text(font, d_ch, True, ui_theme["accent"]), (dx + 5, dial_y + 6))
        screen.blit(_render_text(small_font, "0-9 입력 | Enter 확정", True, (200, 215, 240)), (28 + digits * 24, dial_y + 8))
    if getattr(session.pending, "input_name", None) is not None:
        nm_info = session.pending.input_name
        cur_txt = str(nm_info.get("text", ""))
        max_c = int(nm_info.get("max_char", 12))
        nb_y = max(28, box_y - 36)
        pygame.draw.rect(screen, ui_theme["panel_bg"], (16, nb_y, 380, 30))
        pygame.draw.rect(screen, ui_theme["accent"], (16, nb_y, 380, 30), 1)
        screen.blit(_render_text(font, f"이름 ({len(cur_txt)}/{max_c}): {cur_txt}_", True, ui_theme["accent"]), (26, nb_y + 5))
    if session.pending.shop_goods and session.pending.shop_mode == "buy":
        sqty = max(1, int(getattr(session.pending, "shop_qty", 1) or 1))
        qty_badge = f"수량 x{sqty} [TAB/Q 전환]"
        info_y = max(28, box_y - 26)
        info_txt = qty_badge
        if safe_cursor < len(session.pending.shop_goods):
            details = session.get_shop_buy_details(session.pending)
            if safe_cursor < len(details):
                info_txt = f"{details[safe_cursor]['summary_text']} | {qty_badge}"
        pygame.draw.rect(screen, ui_theme["panel_bg"], (16, info_y, VIEW_W - 32, 22))
        pygame.draw.rect(screen, ui_theme["border"], (16, info_y, VIEW_W - 32, 22), 1)
        screen.blit(_render_text(small_font, info_txt, True, ui_theme["accent"]), (26, info_y + 3))


_CUSTOM_KEY_ALIASES: Dict[str, str] = {
    "space": "K_SPACE",
    "spacebar": "K_SPACE",
    "enter": "K_RETURN",
    "return": "K_RETURN",
    "kp_enter": "K_KP_ENTER",
    "esc": "K_ESCAPE",
    "escape": "K_ESCAPE",
    "backspace": "K_BACKSPACE",
    "tab": "K_TAB",
    "lshift": "K_LSHIFT",
    "left shift": "K_LSHIFT",
    "left_shift": "K_LSHIFT",
    "shift": "K_LSHIFT",
    "rshift": "K_RSHIFT",
    "right shift": "K_RSHIFT",
    "right_shift": "K_RSHIFT",
    "lctrl": "K_LCTRL",
    "left ctrl": "K_LCTRL",
    "left_ctrl": "K_LCTRL",
    "ctrl": "K_LCTRL",
    "rctrl": "K_RCTRL",
    "right ctrl": "K_RCTRL",
    "right_ctrl": "K_RCTRL",
    "lalt": "K_LALT",
    "left alt": "K_LALT",
    "left_alt": "K_LALT",
    "alt": "K_LALT",
    "ralt": "K_RALT",
    "right alt": "K_RALT",
    "right_alt": "K_RALT",
    "up": "K_UP",
    "down": "K_DOWN",
    "left": "K_LEFT",
    "right": "K_RIGHT",
}


def _resolve_custom_key_code(pygame: Any, key_name: Any) -> Optional[int]:
    """Resolves a key name string (e.g. 'w', 'space', 'left shift', 'return') to a Pygame key constant."""
    raw = str(key_name or "").strip().lower()
    if not raw:
        return None
    alias_attr = _CUSTOM_KEY_ALIASES.get(raw) or _CUSTOM_KEY_ALIASES.get(raw.replace("-", "_").replace(" ", "_"))
    if alias_attr:
        val = getattr(pygame, alias_attr, None)
        if isinstance(val, int):
            return val
    attr = getattr(pygame, f"K_{raw}", None)
    if isinstance(attr, int):
        return attr
    attr_u = getattr(pygame, f"K_{raw.upper()}", None)
    if isinstance(attr_u, int):
        return attr_u
    try:
        kc = pygame.key.key_code(raw)
        if isinstance(kc, int):
            return kc
    except Exception:
        pass
    return None


def _direction_key_table(
    pygame: Any, settings: Optional[Dict[str, Any]] = None
) -> Tuple[Tuple[int, str], ...]:
    pairs: List[Tuple[int, str]] = [
        (pygame.K_UP, "up"),
        (pygame.K_DOWN, "down"),
        (pygame.K_LEFT, "left"),
        (pygame.K_RIGHT, "right"),
    ]
    kb = settings.get("keybindings", {}) if isinstance(settings, dict) else {}
    defaults = {"up": "w", "down": "s", "left": "a", "right": "d"}
    for dname, dkey in defaults.items():
        kcode = _resolve_custom_key_code(pygame, kb.get(dname, dkey) if isinstance(kb, dict) else dkey)
        if kcode is not None and (kcode, dname) not in pairs:
            pairs.append((kcode, dname))
    return tuple(pairs)


def _held_move_direction(
    pygame: Any, pressed: Any, key_table: Optional[Tuple[Tuple[int, str], ...]] = None
) -> Optional[str]:
    table = key_table if key_table is not None else _direction_key_table(pygame)
    for key_code, dir_name in table:
        if pressed[key_code]:
            return dir_name
    return None


def _sync_held_dirs(
    pygame: Any,
    pressed: Any,
    held_order: List[str],
    key_table: Optional[Tuple[Tuple[int, str], ...]] = None,
) -> List[str]:
    """Reconciles chronological held-direction order against live pressed key states."""
    table = key_table if key_table is not None else _direction_key_table(pygame)
    currently_down = {dir_name for key_code, dir_name in table if pressed[key_code]}
    synced = [d for d in held_order if d in currently_down]
    for d in ("up", "down", "left", "right"):
        if d in currently_down and d not in synced:
            synced.append(d)
    return synced


def resolve_held_move_action(held_dirs: List[str], can_step: Any) -> Optional[str]:
    """Resolves active directional inputs into a smooth 4-way, diagonal, or wall-slide step."""
    latest_h = next((d for d in reversed(held_dirs) if d in ("left", "right")), None)
    latest_v = next((d for d in reversed(held_dirs) if d in ("up", "down")), None)
    axes = [d for d in held_dirs if d in (latest_h, latest_v)]
    if not axes:
        return None
    if len(axes) == 1:
        return axes[0]
    older, newer = axes[0], axes[1]
    diag_action = f"{latest_v}_{latest_h}"
    if can_step(diag_action):
        return diag_action
    if can_step(newer):
        return newer
    if can_step(older):
        return older
    return newer


def _process_status_keydown(
    pygame: Any,
    key: int,
    session: HeadlessGameSession,
    status_cursor: int,
    actor_id: Optional[int] = None,
) -> Tuple[bool, int]:
    """Handles navigation and quick equipment swapping inside the Tab status overlay."""
    if key in (pygame.K_ESCAPE, pygame.K_TAB):
        return False, 0
    target_aid = session.lead_actor_id if actor_id is None else int(actor_id)
    bag_equips = list_bag_equips_for_lead(session, actor_id=target_aid)
    if not bag_equips:
        return True, 0
    cur = status_cursor % len(bag_equips)
    if key in (pygame.K_UP, pygame.K_w):
        return True, (cur - 1) % len(bag_equips)
    if key in (pygame.K_DOWN, pygame.K_s):
        return True, (cur + 1) % len(bag_equips)
    if key in (pygame.K_RETURN, pygame.K_SPACE, pygame.K_z):
        eq_type, eq_id, _label, _cnt = bag_equips[cur]
        session.step(f"equip:{target_aid}:{eq_type}:{eq_id}")
        next_equips = list_bag_equips_for_lead(session, actor_id=target_aid)
        if not next_equips:
            return True, 0
        return True, min(cur, len(next_equips) - 1)
    return True, cur


def _process_status_panel_keydown(
    pygame: Any,
    key: int,
    session: HeadlessGameSession,
    status_cursor: int,
    status_actor_idx: int,
) -> Tuple[bool, int, int]:
    """Handles party-member switching (Left/Right) plus gear swapping inside Status UI."""
    party = session.party_member_ids or [session.lead_actor_id]
    n_party = len(party)
    cur_actor_idx = status_actor_idx % n_party
    if key in (pygame.K_LEFT, pygame.K_a) and n_party > 1:
        return True, 0, (cur_actor_idx - 1) % n_party
    if key in (pygame.K_RIGHT, pygame.K_d) and n_party > 1:
        return True, 0, (cur_actor_idx + 1) % n_party
    still_open, next_cur = _process_status_keydown(
        pygame, key, session, status_cursor, actor_id=party[cur_actor_idx]
    )
    return still_open, next_cur, (cur_actor_idx if still_open else 0)


def _process_status_panel_action(
    pygame: Any,
    key: int,
    session: HeadlessGameSession,
    status_cursor: int,
    status_actor_idx: int,
    status_mode: str = "equip",
) -> Tuple[bool, int, int, str]:
    """Handles equip/use tab switching (Q/E), party cycling, and field skill/item execution."""
    if key in (pygame.K_q, pygame.K_e):
        next_mode = "use" if status_mode == "equip" else "equip"
        return True, 0, status_actor_idx, next_mode
    party = session.party_member_ids or [session.lead_actor_id]
    n_party = len(party)
    cur_actor_idx = status_actor_idx % n_party
    if key == pygame.K_p and n_party > 1:
        session.swap_party_order(0, cur_actor_idx)
        return True, 0, 0, status_mode
    if status_mode == "equip":
        still_open, next_cur, next_actor = _process_status_panel_keydown(
            pygame, key, session, status_cursor, status_actor_idx
        )
        return still_open, next_cur, next_actor, ("equip" if still_open else "equip")
    target_aid = party[cur_actor_idx]
    if key in (pygame.K_ESCAPE, pygame.K_TAB, pygame.K_m):
        return False, 0, 0, "equip"
    if key in (pygame.K_LEFT, pygame.K_a) and n_party > 1:
        return True, 0, (cur_actor_idx - 1) % n_party, status_mode
    if key in (pygame.K_RIGHT, pygame.K_d) and n_party > 1:
        return True, 0, (cur_actor_idx + 1) % n_party, status_mode
    entries = list_field_skills_and_items_for_actor(session, actor_id=target_aid)
    if not entries:
        return True, 0, cur_actor_idx, status_mode
    cur = status_cursor % len(entries)
    if key in (pygame.K_UP, pygame.K_w):
        return True, (cur - 1) % len(entries), cur_actor_idx, status_mode
    if key in (pygame.K_DOWN, pygame.K_s):
        return True, (cur + 1) % len(entries), cur_actor_idx, status_mode
    if key in (pygame.K_RETURN, pygame.K_SPACE, pygame.K_z):
        kind, obj_id, _lbl = entries[cur]
        if kind == "item":
            session.step(f"use_item:{obj_id}:{target_aid}")
        else:
            session.step(f"use_skill:{target_aid}:{obj_id}:{target_aid}")
        next_entries = list_field_skills_and_items_for_actor(session, actor_id=target_aid)
        return True, (min(cur, len(next_entries) - 1) if next_entries else 0), cur_actor_idx, status_mode
    return True, cur, cur_actor_idx, status_mode


def _action_keys_for_settings(pygame: Any, settings: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Builds confirm, cancel, and navigation key sets honoring settings['keybindings']."""
    kb = settings.get("keybindings", {}) if isinstance(settings, dict) else {}
    dir_map = dict(_direction_key_table(pygame, settings))
    confirm_keys = {pygame.K_RETURN, pygame.K_SPACE, pygame.K_z}
    cancel_keys = {pygame.K_BACKSPACE, pygame.K_x}
    up_keys = {pygame.K_UP, pygame.K_w}
    down_keys = {pygame.K_DOWN, pygame.K_s}
    left_keys = {pygame.K_LEFT, pygame.K_a}
    right_keys = {pygame.K_RIGHT, pygame.K_d}
    for kc, dname in dir_map.items():
        if dname == "up":
            up_keys.add(kc)
        elif dname == "down":
            down_keys.add(kc)
        elif dname == "left":
            left_keys.add(kc)
        elif dname == "right":
            right_keys.add(kc)
    if isinstance(kb, dict):
        c_code = _resolve_custom_key_code(pygame, kb.get("confirm", "z"))
        if c_code is not None:
            confirm_keys.add(c_code)
        x_code = _resolve_custom_key_code(pygame, kb.get("cancel", "x"))
        if x_code is not None:
            cancel_keys.add(x_code)
    return {
        "dir_map": dir_map,
        "confirm": frozenset(confirm_keys),
        "cancel": frozenset(cancel_keys),
        "up": frozenset(up_keys),
        "down": frozenset(down_keys),
        "left": frozenset(left_keys),
        "right": frozenset(right_keys),
    }


def _translate_gamepad_event(
    pygame: Any,
    event: Any,
    joy_axis_held: Dict[int, Optional[str]],
) -> Optional[str]:
    """Maps JOYHATMOTION, JOYAXISMOTION (deadzone 0.45), and JOYBUTTONDOWN into canonical UI/move actions."""
    etype = getattr(event, "type", None)
    if etype == getattr(pygame, "JOYHATMOTION", None):
        hx, hy = getattr(event, "value", (0, 0))
        if hx > 0:
            return "right"
        if hx < 0:
            return "left"
        if hy > 0:
            return "up"
        if hy < 0:
            return "down"
        return None
    if etype == getattr(pygame, "JOYAXISMOTION", None):
        ax = int(getattr(event, "axis", -1))
        val = float(getattr(event, "value", 0.0))
        ax_dir: Optional[str] = None
        if ax == 0:
            ax_dir = "right" if val > 0.45 else ("left" if val < -0.45 else None)
        elif ax == 1:
            ax_dir = "down" if val > 0.45 else ("up" if val < -0.45 else None)
        if ax in (0, 1) and ax_dir != joy_axis_held.get(ax):
            joy_axis_held[ax] = ax_dir
            return ax_dir
        return None
    if etype == getattr(pygame, "JOYBUTTONDOWN", None):
        jb = int(getattr(event, "button", -1))
        if jb == 0:
            return "confirm"
        if jb == 1:
            return "cancel"
        if jb in (3, 7):
            return "menu"
    return None


def _process_keydown(
    pygame: Any,
    key: int,
    session: HeadlessGameSession,
    choice_cursor: int,
    show_status: bool,
    walk_frame: int,
    walk_timer: int,
    settings: Optional[Dict[str, Any]] = None,
) -> Tuple[bool, int, bool, int, int]:
    """Handles a single KEYDOWN event and returns updated player UI state."""
    ksets = _action_keys_for_settings(pygame, settings)
    confirm_keys = ksets["confirm"]
    cancel_keys = ksets["cancel"]
    up_keys = ksets["up"]
    down_keys = ksets["down"]
    if key == pygame.K_ESCAPE:
        if show_status:
            return True, choice_cursor, False, walk_frame, walk_timer
        if session.pending and session.pending.choices and session.pending.cancel_type > 0:
            cancel_idx = min(len(session.pending.choices) - 1, max(0, session.pending.cancel_type - 1))
            session.step(f"choose:{cancel_idx}")
            return True, 0, show_status, walk_frame, walk_timer
        return False, choice_cursor, show_status, walk_frame, walk_timer
    if key == pygame.K_F5:
        session.reload()
        return True, 0, False, 0, 0
    if key == pygame.K_F6 and not session.pending:
        session.save_to_file()
        return True, choice_cursor, show_status, walk_frame, walk_timer
    if key == pygame.K_F9 and not session.pending:
        try:
            session.load_from_file()
        except ValueError:
            pass
        return True, 0, False, 0, 0
    if session.game_over:
        if key in confirm_keys or key == pygame.K_r:
            try:
                session.step("gameover_retry")
            except ValueError:
                session.reload()
        return True, 0, False, 0, 0
    if key == pygame.K_TAB:
        if not show_status and not session.menu_enabled:
            return True, choice_cursor, False, walk_frame, walk_timer
        return True, choice_cursor, not show_status, walk_frame, walk_timer
    if show_status:
        return True, choice_cursor, show_status, walk_frame, walk_timer
    if key in (pygame.K_i, pygame.K_h) and not session.pending:
        heal_iid = first_healing_item_id(session)
        if heal_iid is not None:
            session.step(f"use_item:{heal_iid}")
        return True, choice_cursor, show_status, walk_frame, walk_timer
    if session.pending and session.pending.choices:
        n_choices = len(session.pending.choices)
        if key in up_keys:
            return True, (choice_cursor - 1) % n_choices, show_status, walk_frame, walk_timer
        if key in down_keys:
            return True, (choice_cursor + 1) % n_choices, show_status, walk_frame, walk_timer
        if key in confirm_keys:
            safe_idx = choice_cursor % n_choices
            session.step(f"choose:{safe_idx}")
            keep_cur = bool(session.pending and (session.pending.shop_goods or session.pending.battle_state is not None))
            next_cursor = min(safe_idx, max(0, len(session.pending.choices) - 1)) if keep_cur else 0
            return True, next_cursor, show_status, walk_frame, walk_timer
        if key in cancel_keys and session.pending.cancel_type > 0:
            cancel_idx = min(n_choices - 1, max(0, session.pending.cancel_type - 1))
            session.step(f"choose:{cancel_idx}")
            return True, 0, show_status, walk_frame, walk_timer
        return True, choice_cursor, show_status, walk_frame, walk_timer

    dir_map = ksets["dir_map"]
    if key in dir_map:
        prev_pos = (session.map_id, session.x, session.y)
        prev_dir = session.direction
        session.step(dir_map[key])
        if (session.map_id, session.x, session.y) != prev_pos:
            walk_frame = (walk_frame + 1) % 4
            walk_timer = TILE_PX // WALK_STEP_PX
        elif prev_dir != session.direction:
            walk_timer = 4
        return True, choice_cursor, show_status, walk_frame, walk_timer
    if key in confirm_keys:
        session.step("interact")
    elif key == pygame.K_v and not session.pending:
        session.step("toggle_vehicle")
    return True, choice_cursor, show_status, walk_frame, walk_timer


def _sync_modal_input_reset(
    was_modal: bool,
    is_modal: bool,
    held_dir_order: List[str],
    tap_dirs: List[str],
    move_cooldown: int,
) -> int:
    """Clears residual directional buffers during and right after modal/battle/dialogue states."""
    if is_modal:
        held_dir_order.clear()
        tap_dirs.clear()
        return move_cooldown
    if was_modal:
        held_dir_order.clear()
        tap_dirs.clear()
        return max(move_cooldown, 4)
    return move_cooldown


def _get_or_build_map_cache(
    pygame: Any,
    map_id: int,
    m: Dict[str, Any],
    ts_id: int,
    ts_fields: Dict[str, Any],
    ts_priorities: List[int],
    draw_tile: Any,
    cache_dict: Dict[Tuple[int, int, int], Dict[str, Any]],
    revision: int = 0,
) -> Dict[str, Any]:
    """Caches static non-autotile lower/upper map surfaces keyed by (map_id, ts_id, revision)."""
    key = (int(map_id), int(ts_id), int(revision))
    cached = cache_dict.get(key)
    if cached is not None:
        return cached
    for stale_k in [k for k in cache_dict if k[:2] == (int(map_id), int(ts_id))]:
        cache_dict.pop(stale_k, None)
    mw, mh = int(m["width"]), int(m["height"])
    lower_surf = pygame.Surface((mw * TILE_PX, mh * TILE_PX), pygame.SRCALPHA)
    upper_surf = pygame.Surface((mw * TILE_PX, mh * TILE_PX), pygame.SRCALPHA)
    pri1_rows: Dict[int, Any] = {}
    lower_autos: Dict[Tuple[int, int], List[Tuple[list, int]]] = {}
    pri1_autos: Dict[int, Dict[Tuple[int, int], List[Tuple[list, int]]]] = {}
    upper_autos: Dict[Tuple[int, int], List[Tuple[list, int]]] = {}
    layers = m.get("layers", {})
    for y in range(mh):
        for x in range(mw):
            px, py = x * TILE_PX, y * TILE_PX
            for z in (0, 1, 2):
                layer_grid = layers[f"layer_{z}"]
                tid = int(layer_grid[y][x])
                if tid == 0:
                    continue
                pri = ts_priorities[tid] if 0 <= tid < len(ts_priorities) else 0
                if 48 <= tid < 384:
                    if pri == 1:
                        pri1_autos.setdefault(y, {}).setdefault((x, y), []).append((layer_grid, tid))
                    elif pri >= 2:
                        upper_autos.setdefault((x, y), []).append((layer_grid, tid))
                    else:
                        lower_autos.setdefault((x, y), []).append((layer_grid, tid))
                else:
                    if pri == 1:
                        row_s = pri1_rows.get(y)
                        if row_s is None:
                            row_s = pygame.Surface((mw * TILE_PX, TILE_PX), pygame.SRCALPHA)
                            pri1_rows[y] = row_s
                        draw_tile(row_s, ts_fields, layer_grid, x, y, tid, px, 0, anim_frame=0)
                    else:
                        target_surf = upper_surf if pri >= 2 else lower_surf
                        draw_tile(target_surf, ts_fields, layer_grid, x, y, tid, px, py, anim_frame=0)
    entry = {
        "lower_surf": lower_surf,
        "upper_surf": upper_surf,
        "pri1_rows": pri1_rows,
        "lower_autos": lower_autos,
        "pri1_autos": pri1_autos,
        "upper_autos": upper_autos,
    }
    cache_dict[key] = entry
    return entry


TITLE_COMMANDS: Tuple[str, ...] = ("새 게임", "불러오기", "옵션", "종료")

DEFAULT_PLAYER_SETTINGS: Dict[str, Any] = {
    "bgm_volume": 100,
    "bgs_volume": 100,
    "se_volume": 100,
    "me_volume": 100,
    "text_speed": 100,
    "auto_dash": False,
    "auto_save": True,
    "fullscreen": False,
    "show_minimap": True,
    "lang": "ko",
    "keybindings": {
        "up": "w",
        "down": "s",
        "left": "a",
        "right": "d",
        "confirm": "z",
        "cancel": "x",
        "menu": "escape",
    },
}


def compute_jump_arc_px(frames_left: int, total_frames: int, peak_px: int = 18) -> int:
    """Computes parabolic pixel elevation for active character jump animations."""
    if frames_left <= 0 or total_frames <= 0:
        return 0
    u = max(0.0, min(1.0, float(frames_left) / float(total_frames)))
    return int(round(4.0 * peak_px * u * (1.0 - u)))


def compute_letterbox_rect(
    win_w: int,
    win_h: int,
    logical_w: int = VIEW_W,
    logical_h: int = VIEW_H,
) -> Tuple[int, int, int, int]:
    """Computes centered aspect-preserving viewport (x, y, w, h) inside an arbitrary window size."""
    w = max(1, int(win_w))
    h = max(1, int(win_h))
    lw = max(1, int(logical_w))
    lh = max(1, int(logical_h))
    scale = min(w / float(lw), h / float(lh))
    out_w = max(1, int(round(lw * scale)))
    out_h = max(1, int(round(lh * scale)))
    return ((w - out_w) // 2, (h - out_h) // 2, out_w, out_h)


def window_to_logical_pos(
    pos: Tuple[int, int],
    letterbox: Tuple[int, int, int, int],
    logical_w: int = VIEW_W,
    logical_h: int = VIEW_H,
) -> Tuple[int, int]:
    """Transforms mouse coordinates from scaled window space into logical VIEW_W x VIEW_H coordinates."""
    lx, ly, lw_px, lh_px = letterbox
    rel_x = (int(pos[0]) - lx) / max(1.0, float(lw_px))
    rel_y = (int(pos[1]) - ly) / max(1.0, float(lh_px))
    return (
        max(0, min(logical_w - 1, int(rel_x * logical_w))),
        max(0, min(logical_h - 1, int(rel_y * logical_h))),
    )


def load_player_settings(ir_dir: Path) -> Dict[str, Any]:
    """Loads persisted settings.json from the IR project directory with safe defaults."""
    out = dict(DEFAULT_PLAYER_SETTINGS)
    spath = Path(ir_dir) / "settings.json"
    if spath.exists():
        try:
            loaded = json.loads(spath.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                out.update(loaded)
        except Exception:
            pass
    return out


def save_player_settings(ir_dir: Path, settings: Dict[str, Any]) -> Path:
    """Writes player settings atomically to <ir_dir>/settings.json."""
    spath = Path(ir_dir) / "settings.json"
    merged = dict(DEFAULT_PLAYER_SETTINGS)
    merged.update(settings)
    tmp_path = spath.with_suffix(".tmp")
    tmp_path.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp_path.replace(spath)
    return spath


KEYBINDING_PRESETS: Tuple[Tuple[str, Dict[str, str]], ...] = (
    ("표준 (WASD + Z/X)", {"up": "w", "down": "s", "left": "a", "right": "d", "confirm": "z", "cancel": "x", "menu": "escape"}),
    ("왼손 확장 (WASD + Space/Back)", {"up": "w", "down": "s", "left": "a", "right": "d", "confirm": "space", "cancel": "backspace", "menu": "escape"}),
    ("오른손 IJKL (IJKL + Z/X)", {"up": "i", "down": "k", "left": "j", "right": "l", "confirm": "z", "cancel": "x", "menu": "escape"}),
)


def _current_preset_label(settings: Dict[str, Any]) -> str:
    cur = settings.get("keybindings", {}) if isinstance(settings, dict) else {}
    for label, preset in KEYBINDING_PRESETS:
        if all(str(cur.get(k, "")).lower() == str(v).lower() for k, v in preset.items()):
            return label
    return "사용자 지정"


def build_title_menu_options(
    session: HeadlessGameSession,
    mode: str = "title",
    show_minimap: bool = True,
    settings: Optional[Dict[str, Any]] = None,
    include_controls: bool = False,
    capturing_action: Optional[str] = None,
) -> List[str]:
    """Returns selectable labels for the start Title screen or Options submenu."""
    lang = _resolve_ui_lang(session, settings)
    if mode == "options":
        if settings is not None:
            opts = [
                f"{_ui_tr(lang, 'bgm_vol')}: {int(settings.get('bgm_volume', 100))}%",
                f"{_ui_tr(lang, 'se_vol')}: {int(settings.get('se_volume', 100))}%",
                f"{_ui_tr(lang, 'text_speed')}: {int(settings.get('text_speed', 100))}%",
                f"{_ui_tr(lang, 'auto_dash')}: {'ON' if settings.get('auto_dash') else 'OFF'}",
                f"{_ui_tr(lang, 'audio_mute')}: {'ON' if bool(getattr(session, 'audio_muted', False)) else 'OFF'}",
                f"{_ui_tr(lang, 'minimap')}: {_ui_tr(lang, 'shown') if show_minimap else _ui_tr(lang, 'hidden')}",
            ]
            if include_controls:
                kb_map = settings.get("keybindings", {}) if isinstance(settings.get("keybindings"), dict) else {}

                def _kbadge(act: str, default_k: str) -> str:
                    if capturing_action == act:
                        return f"[{_ui_tr(lang, 'waiting_key')}]"
                    return f"[{str(kb_map.get(act, default_k)).upper()}]"

                opts.append(f"{_ui_tr(lang, 'keyset')}: {_current_preset_label(settings)}")
                opts.append(f"{_ui_tr(lang, 'key_confirm')}: {_kbadge('confirm', 'z')}")
                opts.append(f"{_ui_tr(lang, 'key_cancel')}: {_kbadge('cancel', 'x')}")
                opts.append(f"{_ui_tr(lang, 'key_menu')}: {_kbadge('menu', 'escape')}")
                opts.append(f"{_ui_tr(lang, 'bgs_vol')}: {int(settings.get('bgs_volume', 100))}%")
                opts.append(f"{_ui_tr(lang, 'me_vol')}: {int(settings.get('me_volume', 100))}%")
                opts.append(f"{_ui_tr(lang, 'auto_save')}: {'ON' if settings.get('auto_save', True) else 'OFF'}")
                opts.append(f"{_ui_tr(lang, 'ui_lang')}: {'English' if lang == 'en' else '한국어'}")
            opts.append(_ui_tr(lang, "back"))
            return opts
        return [
            f"{_ui_tr(lang, 'audio_mute')}: {'ON' if bool(getattr(session, 'audio_muted', False)) else 'OFF'}",
            f"{_ui_tr(lang, 'minimap')}: {_ui_tr(lang, 'shown') if show_minimap else _ui_tr(lang, 'hidden')}",
            _ui_tr(lang, "back_title"),
        ]
    has_any_save = any(session.slot_exists(s) for s in range(1, 10))
    load_label = _ui_tr(lang, "load") if has_any_save else _ui_tr(lang, "load_empty")
    items = [_ui_tr(lang, "new_game"), load_label, _ui_tr(lang, "options"), _ui_tr(lang, "quit")]
    if session.autosave_exists():
        items.append(_ui_tr(lang, "continue_autosave"))
    return items


def process_title_selection(
    session: HeadlessGameSession,
    mode: str,
    cursor_idx: int,
    show_minimap: bool,
    settings: Optional[Dict[str, Any]] = None,
    adjust_delta: int = 0,
    include_controls: bool = False,
) -> Dict[str, Any]:
    """Evaluates a Title or Options menu choice and returns next scene transition instructions."""
    lang = _resolve_ui_lang(session, settings)
    if mode == "options":
        if settings is not None:
            total_rows = 15 if include_controls else 7
            safe = cursor_idx % total_rows
            step_pct = (20 * adjust_delta) if adjust_delta != 0 else 20
            if safe in (0, 1):
                key = "bgm_volume" if safe == 0 else "se_volume"
                cur = int(settings.get(key, 100))
                nxt = ((cur + step_pct) % 120) if adjust_delta == 0 else max(0, min(100, cur + step_pct))
                settings[key] = nxt
                lbl = _ui_tr(lang, "bgm_vol") if safe == 0 else _ui_tr(lang, "se_vol")
                return {"action": f"set_{key}", "next_mode": "options", "show_minimap": show_minimap, "toast": f"{lbl}: {nxt}%"}
            if safe == 2:
                speeds = [50, 100, 200, 400]
                cur = int(settings.get("text_speed", 100))
                idx_s = speeds.index(cur) if cur in speeds else 1
                nxt = speeds[(idx_s + (adjust_delta if adjust_delta else 1)) % len(speeds)]
                settings["text_speed"] = nxt
                return {"action": "set_text_speed", "next_mode": "options", "show_minimap": show_minimap, "toast": f"{_ui_tr(lang, 'text_speed')}: {nxt}%"}
            if safe == 3:
                settings["auto_dash"] = not bool(settings.get("auto_dash", False))
                return {"action": "toggle_auto_dash", "next_mode": "options", "show_minimap": show_minimap, "toast": f"{_ui_tr(lang, 'auto_dash')} {'ON' if settings['auto_dash'] else 'OFF'}"}
            if safe == 4:
                muted = session.toggle_audio_mute()
                return {"action": "toggle_mute", "next_mode": "options", "show_minimap": show_minimap, "toast": f"{_ui_tr(lang, 'audio_mute')} {'ON' if muted else 'OFF'}"}
            if safe == 5:
                nm = not show_minimap
                settings["show_minimap"] = nm
                return {"action": "toggle_minimap", "next_mode": "options", "show_minimap": nm, "toast": f"{_ui_tr(lang, 'minimap')}: {_ui_tr(lang, 'shown') if nm else _ui_tr(lang, 'hidden')}"}
            if include_controls and safe == 6:
                cur_lbl = _current_preset_label(settings)
                labels = [p[0] for p in KEYBINDING_PRESETS]
                idx_p = labels.index(cur_lbl) if cur_lbl in labels else -1
                nxt_p = (idx_p + (adjust_delta if adjust_delta else 1)) % len(KEYBINDING_PRESETS)
                new_lbl, new_kb = KEYBINDING_PRESETS[nxt_p]
                settings["keybindings"] = dict(new_kb)
                return {"action": "set_keybindings", "next_mode": "options", "show_minimap": show_minimap, "toast": f"{_ui_tr(lang, 'keyset')}: {new_lbl}"}
            if include_controls and safe in (7, 8, 9):
                act_name = ("confirm", "cancel", "menu")[safe - 7]
                return {"action": f"capture_{act_name}_key", "capture_target": act_name, "next_mode": "options", "show_minimap": show_minimap, "toast": f"{act_name.upper()} 키로 사용할 새 키를 누르세요 (ESC 취소)"}
            if include_controls and safe in (10, 11):
                vkey = "bgs_volume" if safe == 10 else "me_volume"
                cur_v = int(settings.get(vkey, 100))
                nxt_v = ((cur_v + step_pct) % 120) if adjust_delta == 0 else max(0, min(100, cur_v + step_pct))
                settings[vkey] = nxt_v
                lbl = _ui_tr(lang, "bgs_vol") if safe == 10 else _ui_tr(lang, "me_vol")
                return {"action": f"set_{vkey}", "next_mode": "options", "show_minimap": show_minimap, "toast": f"{lbl}: {nxt_v}%"}
            if include_controls and safe == 12:
                settings["auto_save"] = not bool(settings.get("auto_save", True))
                return {"action": "toggle_auto_save", "next_mode": "options", "show_minimap": show_minimap, "toast": f"{_ui_tr(lang, 'auto_save')} {'ON' if settings['auto_save'] else 'OFF'}"}
            if include_controls and safe == 13:
                nxt_lang = "en" if lang == "ko" else "ko"
                settings["lang"] = nxt_lang
                session.set_locale(nxt_lang)
                return {"action": "toggle_lang", "next_mode": "options", "show_minimap": show_minimap, "toast": "Language: English" if nxt_lang == "en" else "언어: 한국어"}
            return {"action": "back", "next_mode": "title", "show_minimap": show_minimap, "toast": ""}
        safe = cursor_idx % 3
        if safe == 0:
            muted = session.toggle_audio_mute()
            return {"action": "toggle_mute", "next_mode": "options", "show_minimap": show_minimap, "toast": f"{_ui_tr(lang, 'audio_mute')} {'ON' if muted else 'OFF'}"}
        if safe == 1:
            nm = not show_minimap
            return {"action": "toggle_minimap", "next_mode": "options", "show_minimap": nm, "toast": f"{_ui_tr(lang, 'minimap')}: {_ui_tr(lang, 'shown') if nm else _ui_tr(lang, 'hidden')}"}
        return {"action": "back", "next_mode": "title", "show_minimap": show_minimap, "toast": ""}
    has_auto = session.autosave_exists()
    safe = cursor_idx % (5 if has_auto else 4)
    if safe == 0:
        return {"action": "start", "next_mode": "map", "show_minimap": show_minimap, "toast": f"📍 {session.current_map().get('name', '')}"}
    if safe == 1:
        has_any_save = any(session.slot_exists(s) for s in range(1, 10)) or has_auto
        if not has_any_save:
            return {"action": "noop", "next_mode": "title", "show_minimap": show_minimap, "toast": "저장된 슬롯이 없습니다"}
        return {"action": "open_load", "next_mode": "title", "show_minimap": show_minimap, "toast": "불러올 슬롯을 선택하세요"}
    if safe == 2:
        return {"action": "options", "next_mode": "options", "show_minimap": show_minimap, "toast": ""}
    if safe == 4 and has_auto:
        try:
            session.load_autosave()
        except ValueError:
            return {"action": "load_rejected", "next_mode": "title", "show_minimap": show_minimap, "toast": "자동 저장 로드 거부됨: 저장 데이터 무결성 검증 실패"}
        if not bool(getattr(session, "save_checksum_valid", True)):
            return {"action": "continue_autosave", "next_mode": "map", "show_minimap": show_minimap, "toast": "자동 저장 로드 완료 (경고: 저장 데이터 무결성 확인 실패)"}
        return {"action": "continue_autosave", "next_mode": "map", "show_minimap": show_minimap, "toast": "자동 저장 불러오기 완료"}
    return {"action": "quit", "next_mode": "title", "show_minimap": show_minimap, "toast": ""}


def build_main_menu_items(session: HeadlessGameSession, settings: Optional[Dict[str, Any]] = None) -> List[Tuple[str, str, bool]]:
    """Returns ordered Integrated RPG Menu commands as (action_id, label, enabled) tuples."""
    lang = _resolve_ui_lang(session, settings)
    has_loadable = any(session.slot_exists(s) for s in range(1, 10)) or session.autosave_exists()
    return [
        ("use", _ui_tr(lang, "menu_use"), True),
        ("equip", _ui_tr(lang, "menu_equip"), True),
        ("quests", _ui_tr(lang, "menu_quests"), True),
        ("save", _ui_tr(lang, "menu_save"), bool(getattr(session, "save_enabled", True))),
        ("load", _ui_tr(lang, "menu_load"), has_loadable),
        ("options", _ui_tr(lang, "menu_options"), True),
        ("title", _ui_tr(lang, "menu_title"), True),
        ("close", _ui_tr(lang, "menu_close"), True),
    ]


def _draw_main_menu_overlay(
    pygame: Any,
    screen: Any,
    font: Any,
    small_font: Any,
    session: HeadlessGameSession,
    menu_cursor: int,
    theme: Optional[Dict[str, Tuple[int, int, int]]] = None,
    load_img: Optional[Any] = None,
    settings: Optional[Dict[str, Any]] = None,
) -> List[Tuple[int, int, int, int]]:
    """Draws the ESC integrated RPG Menu Hub with party summary cards and returns command Rects."""
    ui_theme = theme or _WINDOWSKIN_PRESETS["default"]
    lang = _resolve_ui_lang(session, settings)
    wskin = str(getattr(session, "windowskin_name", "") or "")
    screen.blit(_get_alpha_overlay(pygame, VIEW_W, VIEW_H, (6, 10, 18, 175)), (0, 0))
    items = build_main_menu_items(session, settings=settings)
    safe = menu_cursor % len(items)
    cmd_x, cmd_y, cmd_w, cmd_h = 24, 32, 186, 20 + len(items) * 32
    _draw_window_box(pygame, screen, (cmd_x, cmd_y, cmd_w, cmd_h), ui_theme, load_img=load_img, windowskin_name=wskin)
    rects: List[Tuple[int, int, int, int]] = []
    for idx, (_act, lbl, enabled) in enumerate(items):
        ry = cmd_y + 10 + idx * 32
        rt = (cmd_x + 10, ry, cmd_w - 20, 28)
        rects.append(rt)
        if idx == safe:
            pygame.draw.rect(screen, ui_theme["panel_bg"], rt)
            pygame.draw.rect(screen, ui_theme["accent"], rt, 1)
        col = ui_theme["accent"] if idx == safe else ((225, 232, 244) if enabled else (115, 125, 145))
        screen.blit(_render_text(small_font, f"{'▶ ' if idx == safe else '  '}{lbl}", True, col), (rt[0] + 6, rt[1] + 5))
    info_y = cmd_y + cmd_h + 10
    _draw_window_box(pygame, screen, (cmd_x, info_y, cmd_w, 84), ui_theme, load_img=load_img, windowskin_name=wskin)
    screen.blit(_render_text(small_font, f"{_ui_tr(lang, 'gold')}: {session.gold} G", True, (255, 220, 115)), (cmd_x + 12, info_y + 10))
    total_secs = int(float(getattr(session, "play_time_ms", 0.0)) // 1000.0)
    mins, secs = divmod(total_secs, 60)
    screen.blit(_render_text(small_font, f"{_ui_tr(lang, 'playtime')}: {mins:02d}:{secs:02d}", True, (200, 220, 245)), (cmd_x + 12, info_y + 34))
    screen.blit(_render_text(small_font, f"{_ui_tr(lang, 'location')}: {session.current_map().get('name', '')}", True, (180, 200, 225)), (cmd_x + 12, info_y + 56))
    px_box, py_box, pw_box = 224, 32, VIEW_W - 248
    party_ids = session.party_member_ids or [session.lead_actor_id]
    _draw_window_box(pygame, screen, (px_box, py_box, pw_box, max(120, len(party_ids) * 84 + 20)), ui_theme, load_img=load_img, windowskin_name=wskin)
    for p_idx, aid in enumerate(party_ids[:4]):
        st = session._get_actor_state(aid)
        cy = py_box + 14 + p_idx * 82
        pygame.draw.rect(screen, ui_theme["panel_bg"], (px_box + 12, cy, pw_box - 24, 72))
        pygame.draw.rect(screen, ui_theme["border"], (px_box + 12, cy, pw_box - 24, 72), 1)
        badge = session.format_state_badges(st.get("states", []))
        screen.blit(_render_text(font, f"{st['name']} Lv.{int(st['level'])} {badge}", True, ui_theme["accent"]), (px_box + 22, cy + 8))
        hp_r = max(0.0, min(1.0, int(st["hp"]) / max(1, int(st["max_hp"]))))
        sp_r = max(0.0, min(1.0, int(st["sp"]) / max(1, int(st["max_sp"]))))
        screen.blit(_render_text(small_font, f"HP {int(st['hp'])}/{int(st['max_hp'])}", True, (220, 245, 225)), (px_box + 22, cy + 36))
        pygame.draw.rect(screen, (50, 75, 55), (px_box + 150, cy + 40, 120, 8))
        pygame.draw.rect(screen, (95, 215, 130), (px_box + 150, cy + 40, int(120 * hp_r), 8))
        screen.blit(_render_text(small_font, f"SP {int(st['sp'])}/{int(st['max_sp'])}", True, (185, 220, 255)), (px_box + 22, cy + 54))
        pygame.draw.rect(screen, (40, 55, 85), (px_box + 150, cy + 58, 120, 8))
        pygame.draw.rect(screen, (95, 165, 245), (px_box + 150, cy + 58, int(120 * sp_r), 8))
    return rects


def _draw_title_screen(
    pygame: Any,
    screen: Any,
    font: Any,
    small_font: Any,
    session: HeadlessGameSession,
    mode: str,
    cursor_idx: int,
    show_minimap: bool,
    toast_text: str = "",
    theme: Optional[Dict[str, Tuple[int, int, int]]] = None,
    load_img: Optional[Any] = None,
    settings: Optional[Dict[str, Any]] = None,
    include_controls: bool = False,
    capturing_action: Optional[str] = None,
) -> List[Any]:
    """Draws the commercial start Title / Options screen with optional Titles/ art and returns clickable menu Rects."""
    ui_theme = theme or _WINDOWSKIN_PRESETS["default"]
    lang = _resolve_ui_lang(session, settings)
    wskin = str(getattr(session, "windowskin_name", "") or "")
    screen.fill((14, 18, 30))
    title_art = (
        load_img("Titles/Title")
        or load_img(f"Titles/{getattr(session, 'title', '')}")
    ) if load_img else None
    if title_art is not None:
        screen.blit(_get_scaled_surface(pygame, title_art, (VIEW_W, VIEW_H)), (0, 0))
    _draw_window_box(pygame, screen, (32, 24, VIEW_W - 64, 96), ui_theme, load_img=load_img, windowskin_name=wskin)
    title_str = str(getattr(session, "title", "RPG CREATOR")) or "RPG CREATOR"
    sub_str = _ui_tr(lang, "subtitle_options") if mode == "options" else _ui_tr(lang, "subtitle_title")
    screen.blit(_render_text(font, title_str, True, ui_theme["accent"]), (64, 46))
    screen.blit(_render_text(small_font, sub_str, True, (210, 222, 245)), (66, 80))
    opts = build_title_menu_options(
        session, mode=mode, show_minimap=show_minimap, settings=settings, include_controls=include_controls, capturing_action=capturing_action
    )
    safe = cursor_idx % len(opts) if opts else 0
    row_step = 30 if len(opts) <= 7 else max(21, (VIEW_H - 184) // max(1, len(opts)))
    row_h = row_step - 2
    box_w = 400
    box_h = 20 + len(opts) * row_step
    bx = (VIEW_W - box_w) // 2
    by = 132 if len(opts) >= 8 else (150 if len(opts) > 4 else 172)
    _draw_window_box(pygame, screen, (bx, by, box_w, box_h), ui_theme, load_img=load_img, windowskin_name=wskin)
    rects: List[Any] = []
    use_font = small_font if row_step < 26 else font
    for idx, label in enumerate(opts):
        ry = by + 10 + idx * row_step
        r = pygame.Rect(bx + 12, ry, box_w - 24, row_h)
        if idx == safe:
            pygame.draw.rect(screen, ui_theme["panel_bg"], r)
            pygame.draw.rect(screen, ui_theme["accent"], r, 1)
        col = ui_theme["accent"] if idx == safe else (225, 232, 244)
        prefix = "▶ " if idx == safe else "  "
        screen.blit(_render_text(use_font, f"{prefix}{label}", True, col), (r.x + 8, r.y + max(1, (row_h - 16) // 2)))
        rects.append(r)
    if toast_text:
        screen.blit(_render_text(small_font, toast_text, True, ui_theme["accent"]), (bx, min(VIEW_H - 22, by + box_h + 8)))
    return rects


def build_save_slot_rows(session: HeadlessGameSession, count: int = 9, include_autosave: bool = False) -> List[Dict[str, Any]]:
    """Returns summary dictionaries for numbered save slots 1..count (and optional autosave row)."""
    rows = [session.get_save_slot_summary(slot) for slot in range(1, max(1, min(9, int(count))) + 1)]
    if include_autosave and session.autosave_exists():
        rows.append(session.get_autosave_summary())
    return rows


def hit_test_rects(pos: Tuple[int, int], rects: List[Any]) -> Optional[int]:
    """Returns the index of the first Rect or (x, y, w, h) tuple containing pos, else None."""
    px, py = int(pos[0]), int(pos[1])
    for idx, r in enumerate(rects):
        if hasattr(r, "collidepoint"):
            if r.collidepoint(px, py):
                return idx
        elif isinstance(r, (tuple, list)) and len(r) >= 4:
            rx, ry, rw, rh = int(r[0]), int(r[1]), int(r[2]), int(r[3])
            if rx <= px < rx + rw and ry <= py < ry + rh:
                return idx
    return None


def _draw_save_slot_overlay(
    pygame: Any,
    screen: Any,
    font: Any,
    small_font: Any,
    session: HeadlessGameSession,
    slot_mode: str,
    slot_cursor: int,
    toast_text: str = "",
    theme: Optional[Dict[str, Tuple[int, int, int]]] = None,
    load_img: Optional[Any] = None,
    settings: Optional[Dict[str, Any]] = None,
) -> List[Tuple[int, int, int, int]]:
    """Draws the 9-slot (+autosave in load mode) Save/Load overlay panel and returns clickable row bounds."""
    ui_theme = theme or _WINDOWSKIN_PRESETS["default"]
    lang = _resolve_ui_lang(session, settings)
    wskin = str(getattr(session, "windowskin_name", "") or "")
    rows = build_save_slot_rows(session, include_autosave=(slot_mode == "load"))
    safe = slot_cursor % len(rows) if rows else 0
    box_x, box_y, box_w, box_h = 44, 40, VIEW_W - 88, 46 + len(rows) * 32
    _draw_window_box(pygame, screen, (box_x, box_y, box_w, box_h), ui_theme, load_img=load_img, windowskin_name=wskin)
    header = _ui_tr(lang, "slot_save_hdr") if slot_mode == "save" else _ui_tr(lang, "slot_load_hdr")
    screen.blit(_render_text(small_font, header, True, ui_theme["accent"]), (box_x + 14, box_y + 12))
    rects: List[Tuple[int, int, int, int]] = []
    for idx, info in enumerate(rows):
        ry = box_y + 42 + idx * 32
        rect_tuple = (box_x + 12, ry, box_w - 24, 28)
        rects.append(rect_tuple)
        if idx == safe:
            pygame.draw.rect(screen, ui_theme["panel_bg"], rect_tuple)
            pygame.draw.rect(screen, ui_theme["accent"], rect_tuple, 1)
        col = ui_theme["accent"] if idx == safe else ((225, 232, 245) if info["exists"] else (130, 140, 160))
        prefix = "▶ " if idx == safe else "  "
        slot_lbl = info["label"] if int(info["slot"]) == 0 else f"슬롯 {info['slot']}: {info['label']}"
        screen.blit(_render_text(small_font, f"{prefix}{slot_lbl}", True, col), (box_x + 20, ry + 5))
    if toast_text:
        screen.blit(_render_text(small_font, toast_text, True, ui_theme["accent"]), (box_x + 14, min(VIEW_H - 22, box_y + box_h + 10)))
    return rects


def _draw_quest_overlay(
    pygame: Any,
    screen: Any,
    font: Any,
    small_font: Any,
    session: HeadlessGameSession,
    theme: Optional[Dict[str, Tuple[int, int, int]]] = None,
    scroll_offset: int = 0,
    out_close_rects: Optional[List[Tuple[int, int, int, int]]] = None,
) -> None:
    """Draws the Quest Journal modal panel (J hotkey)."""
    if out_close_rects is not None:
        out_close_rects.clear()
    ui_theme = theme or _WINDOWSKIN_PRESETS["default"]
    all_lines = session.list_quest_lines()
    max_off = max(0, len(all_lines) - 12)
    off = max(0, min(int(scroll_offset), max_off))
    lines = all_lines[off : off + 12]
    box_x, box_y, box_w = 48, 56, VIEW_W - 96
    box_h = max(120, 56 + max(1, len(lines)) * 24)
    pygame.draw.rect(screen, ui_theme["bg"], (box_x, box_y, box_w, box_h))
    pygame.draw.rect(screen, ui_theme["border"], (box_x, box_y, box_w, box_h), 2)
    screen.blit(_render_text(font, "퀘스트 일지 (J / ESC 닫기)", True, ui_theme["accent"]), (box_x + 16, box_y + 12))
    close_rect = (box_x + box_w - 72, box_y + 8, 60, 24)
    pygame.draw.rect(screen, ui_theme["panel_bg"], close_rect)
    pygame.draw.rect(screen, ui_theme["border"], close_rect, 1)
    screen.blit(_render_text(small_font, "닫기", True, (235, 240, 250)), (close_rect[0] + 16, close_rect[1] + 4))
    if out_close_rects is not None:
        out_close_rects.append(close_rect)
    if not lines:
        screen.blit(_render_text(small_font, "등록된 퀘스트가 없습니다.", True, (190, 200, 215)), (box_x + 18, box_y + 44))
        return
    for idx, qline in enumerate(lines):
        col = (
            (130, 235, 165) if qline.startswith("[완료]")
            else ((180, 130, 140) if qline.startswith("[실패]") else (235, 240, 250))
        )
        screen.blit(_render_text(small_font, qline, True, col), (box_x + 18, box_y + 44 + idx * 24))


def _sync_override_bgm(
    pygame: Any,
    audio_root: Optional[Path],
    bgm_spec: Optional[Dict[str, Any]],
    playing_bgm: Optional[str],
    mixer_ok: bool,
    settings: Optional[Dict[str, Any]] = None,
    muted: bool = False,
    rgssad_reader: Optional[Any] = None,
) -> Optional[str]:
    """Syncs pygame music playback to a non-map BGM spec such as title_bgm."""
    target = bgm_spec.get("name") if isinstance(bgm_spec, dict) else None
    if not mixer_ok or not target:
        return playing_bgm
    master_bgm = max(0, min(100, int((settings or {}).get("bgm_volume", 100)))) / 100.0
    vol = 0.0 if muted else max(0.0, min(1.0, (int((bgm_spec or {}).get("volume", 80)) / 100.0) * master_bgm))
    try:
        if target != playing_bgm:
            src_kind, src_val = _resolve_audio_source(audio_root, "BGM", str(target), rgssad_reader=rgssad_reader)
            if src_kind is not None:
                if playing_bgm and hasattr(pygame.mixer.music, "fadeout"):
                    pygame.mixer.music.fadeout(180)
                if _music_load_source(pygame, src_kind, src_val):
                    pygame.mixer.music.set_volume(vol)
                    pygame.mixer.music.play(-1)
            playing_bgm = target
        else:
            pygame.mixer.music.set_volume(vol)
    except Exception:
        pass
    return playing_bgm


def launch_player(
    ir_dir: Path,
    graphics_dir: Optional[Path] = None,
    max_frames: Optional[int] = None,
    on_frame: Optional[Any] = None,
    start_on_title: bool = False,
    lang: Optional[str] = None,
    use_os_save_dir: bool = False,
    audio_dir: Optional[Path] = None,
    rgssad_archive: Optional[Path] = None,
    rgssad_key: Optional[int] = None,
    strict_save: bool = False,
) -> int:
    """Opens a live Pygame window running the IR project."""
    import pygame
    from ..core.rgssad import DEFAULT_RGSSAD_KEY, RgssadArchiveReader

    ir_dir = Path(ir_dir)
    rgssad_reader: Optional[RgssadArchiveReader] = None
    if rgssad_archive is not None:
        arch_path = Path(rgssad_archive)
        if arch_path.is_file():
            try:
                key_val = int(rgssad_key) if rgssad_key is not None else DEFAULT_RGSSAD_KEY
                rgssad_reader = RgssadArchiveReader(arch_path, initial_key=key_val)
            except Exception:
                rgssad_reader = None
    settings = load_player_settings(ir_dir)
    active_lang = lang if lang is not None else settings.get("lang")
    session = HeadlessGameSession(
        ir_dir,
        enable_random_encounters=True,
        interactive_battle=True,
        lang=str(active_lang) if active_lang else None,
        use_os_save_dir=use_os_save_dir,
        wait_pause_enabled=True,
        strict_checksum=bool(strict_save),
    )
    gfx_root = _find_graphics_dir(ir_dir, graphics_dir)
    audio_root = _find_audio_dir(ir_dir, audio_dir)

    tilesets_meta = {
        int(ts["fields"]["id"]): ts["fields"]
        for ts in load_json(ir_dir / "database" / "tilesets.json")
        if ts
    }

    pygame.init()
    mixer_ok = bool(getattr(pygame, "mixer", None) and pygame.mixer.get_init())
    settings = load_player_settings(ir_dir)
    if getattr(pygame, "joystick", None) is not None:
        try:
            pygame.joystick.init()
        except Exception:
            pass
    try:
        is_fullscreen = bool(settings.get("fullscreen", False))
        disp_flags = pygame.FULLSCREEN if is_fullscreen else pygame.RESIZABLE
        display_surf = pygame.display.set_mode((VIEW_W, VIEW_H), disp_flags)
        screen = pygame.Surface((VIEW_W, VIEW_H))
        pygame.display.set_caption(f"RPG Creator Player - {session.title}")
        clock = pygame.time.Clock()
        font = _load_ui_font(pygame, ir_dir, 16)
        small_font = _load_ui_font(pygame, ir_dir, 13)

        joysticks: List[Any] = []
        if getattr(pygame, "joystick", None) is not None and pygame.joystick.get_init():
            for j_idx in range(pygame.joystick.get_count()):
                try:
                    joy = pygame.joystick.Joystick(j_idx)
                    joy.init()
                    joysticks.append(joy)
                except Exception:
                    pass

        def _current_letterbox() -> Tuple[int, int, int, int]:
            dw, dh = display_surf.get_size()
            return compute_letterbox_rect(dw, dh, VIEW_W, VIEW_H)

        pending_resize: Optional[Tuple[int, int]] = None
        pending_resize_deadline = 0.0

        def _flush_pending_resize(force: bool = False) -> None:
            nonlocal display_surf, pending_resize
            if pending_resize is None or is_fullscreen:
                return
            if not force and time.monotonic() < pending_resize_deadline:
                return
            rw, rh = pending_resize
            pending_resize = None
            if display_surf.get_size() != (rw, rh):
                display_surf = pygame.display.set_mode((rw, rh), pygame.RESIZABLE)

        def _queue_video_resize(ev: Any) -> None:
            nonlocal pending_resize, pending_resize_deadline
            if is_fullscreen:
                return
            rw = max(320, int(getattr(ev, "w", VIEW_W)))
            rh = max(240, int(getattr(ev, "h", VIEW_H)))
            if display_surf.get_size() != (rw, rh):
                pending_resize = (rw, rh)
                pending_resize_deadline = time.monotonic() + 0.12

        outline_ctrl: Any = None
        try:
            import os as _os

            if _os.environ.get("SDL_VIDEODRIVER", "").lower() != "dummy":
                wm_info = pygame.display.get_wm_info()
                wm_hwnd = wm_info.get("window") if isinstance(wm_info, dict) else 0
                if wm_hwnd:
                    outline_ctrl = attach_outline_drag_preview(
                        wm_hwnd,
                        min_size=(320, 240),
                        on_commit=lambda _w, _h: _flush_pending_resize(force=True),
                    )
        except Exception:
            outline_ctrl = None

        def _present_frame() -> None:
            _flush_pending_resize(force=False)
            dw, dh = display_surf.get_size()
            if (dw, dh) == (VIEW_W, VIEW_H):
                display_surf.blit(screen, (0, 0))
            else:
                lx, ly, lw_px, lh_px = compute_letterbox_rect(dw, dh, VIEW_W, VIEW_H)
                display_surf.fill((0, 0, 0))
                display_surf.blit(pygame.transform.scale(screen, (lw_px, lh_px)), (lx, ly))
            if pending_resize is not None:
                pw, ph = pending_resize
                _draw_gray_pattern_rect(pygame, display_surf, (2, 2, max(4, dw - 4), max(4, dh - 4)))
                badge = _render_text(small_font, f"크기 조절 미리보기: {pw}x{ph}", True, (224, 226, 230))
                bx = max(8, (dw - badge.get_width()) // 2)
                pygame.draw.rect(display_surf, (48, 52, 58), (bx - 8, 8, badge.get_width() + 16, badge.get_height() + 8))
                pygame.draw.rect(display_surf, PREVIEW_GRAY_LIGHT_RGB, (bx - 8, 8, badge.get_width() + 16, badge.get_height() + 8), 1)
                display_surf.blit(badge, (bx, 12))
            pygame.display.flip()

        def _toggle_fullscreen() -> None:
            nonlocal display_surf, is_fullscreen, pending_resize
            pending_resize = None
            is_fullscreen = not is_fullscreen
            settings["fullscreen"] = is_fullscreen
            save_player_settings(ir_dir, settings)
            flags = pygame.FULLSCREEN if is_fullscreen else pygame.RESIZABLE
            target_sz = (0, 0) if is_fullscreen else (VIEW_W, VIEW_H)
            display_surf = pygame.display.set_mode(target_sz, flags)

        image_cache: Dict[str, Optional[pygame.Surface]] = {}
        render_cache: Dict[str, Any] = {}
        hud_cache: Dict[str, Any] = {}
        map_layer_cache: Dict[Tuple[int, int], Dict[str, Any]] = {}
        pic_cache: Dict[Tuple[Any, ...], Any] = {}
        minimap_cache: Dict[str, Any] = {}
        choice_cursor = 0
        title_mode = "title" if start_on_title else "map"
        title_cursor = 0
        options_return_to_menu = False
        menu_hub_open = False
        menu_cursor = 0
        slot_overlay_mode: Optional[str] = None
        slot_overlay_return_title = False
        slot_overlay_return_menu = False
        slot_cursor = 0
        overlay_confirm_slot: Optional[int] = None
        fx_accum_ms = 0.0
        last_title_rects: List[Any] = []
        last_menu_rects: List[Tuple[int, int, int, int]] = []
        last_slot_rects: List[Tuple[int, int, int, int]] = []
        last_choice_rects: List[Tuple[int, int, int, int]] = []
        status_cursor = 0
        status_actor_idx = 0
        status_mode = "equip"
        playing_bgm: Optional[str] = None
        playing_bgs: Optional[str] = None
        bgs_channel: Optional[Any] = None
        ice_sliding = False
        active_slot = 1
        pending_load_confirm = False
        toast_text = f"📍 {session.current_map().get('name', '')}"
        toast_timer_ms = 2200.0
        last_seen_map_id = session.map_id
        load_confirm_timer_ms = 0.0
        anim_tick = 0
        walk_frame = 0
        walk_timer = 0
        stride_count = 0
        slide_dx = 0
        slide_dy = 0
        move_px_accum = 0.0
        last_dt_ms = 1000.0 / TARGET_FPS
        dialogue_elapsed_ms = 0.0
        last_dialogue_sig: Optional[Tuple[int, Tuple[str, ...]]] = None
        npc_step_timers: Dict[Tuple[int, int], float] = {}
        npc_slides: Dict[Tuple[int, int], Dict[str, Any]] = {}
        move_cooldown = 0
        show_status = False
        show_quests = False
        show_minimap = bool(settings.get("show_minimap", True))
        was_modal_open = False
        running = True
        held_dir_order: List[str] = []
        tap_dirs: List[str] = []
        joy_axis_held: Dict[int, Optional[str]] = {}
        played_gameover_me = False
        hue_cache: Dict[Tuple[int, int], Any] = {}
        fog_cache: Dict[Tuple[Any, ...], Any] = {}
        last_status_rects: List[Tuple[str, Any, Tuple[int, int, int, int]]] = []
        last_quest_close_rects: List[Tuple[int, int, int, int]] = []
        quest_scroll = 0
        capturing_key_action: Optional[str] = None
        dir_key_table: Dict[int, str] = {}
        dir_key_map: Dict[int, str] = {}
        custom_confirm_key = pygame.K_z
        custom_cancel_key = pygame.K_x
        custom_menu_key = pygame.K_ESCAPE
        confirm_keys = frozenset({pygame.K_RETURN, pygame.K_SPACE, pygame.K_z})
        cancel_keys = frozenset({pygame.K_BACKSPACE, pygame.K_x})
        up_keys = frozenset({pygame.K_UP, pygame.K_w})
        down_keys = frozenset({pygame.K_DOWN, pygame.K_s})
        left_keys = frozenset({pygame.K_LEFT, pygame.K_a})
        right_keys = frozenset({pygame.K_RIGHT, pygame.K_d})

        def _refresh_keybindings() -> None:
            nonlocal dir_key_table, dir_key_map, custom_confirm_key, custom_cancel_key, custom_menu_key
            nonlocal confirm_keys, cancel_keys, up_keys, down_keys, left_keys, right_keys
            dir_key_table = _direction_key_table(pygame, settings)
            ksets = _action_keys_for_settings(pygame, settings)
            dir_key_map = dict(ksets["dir_map"])
            confirm_keys = ksets["confirm"]
            cancel_keys = ksets["cancel"]
            up_keys = ksets["up"]
            down_keys = ksets["down"]
            left_keys = ksets["left"]
            right_keys = ksets["right"]
            kb_cfg = settings.get("keybindings", {}) if isinstance(settings.get("keybindings"), dict) else {}
            custom_confirm_key = _resolve_custom_key_code(pygame, kb_cfg.get("confirm", "z")) or pygame.K_z
            custom_cancel_key = _resolve_custom_key_code(pygame, kb_cfg.get("cancel", "x")) or pygame.K_x
            custom_menu_key = _resolve_custom_key_code(pygame, kb_cfg.get("menu", "escape")) or pygame.K_ESCAPE

        _refresh_keybindings()

        def _sync_move_visual(prev_map: int, prev_x: int, prev_y: int) -> None:
            nonlocal slide_dx, slide_dy, walk_frame, walk_timer, stride_count, move_cooldown, move_px_accum, toast_text, toast_timer_ms, last_seen_map_id, ice_sliding
            if session.map_id != prev_map:
                slide_dx, slide_dy = 0, 0
                move_px_accum = 0.0
                walk_timer = 0
                ice_sliding = False
                move_cooldown = 8
                npc_slides.clear()
                npc_step_timers.clear()
                last_seen_map_id = session.map_id
                if settings.get("auto_save", True):
                    try:
                        session.save_autosave()
                    except Exception:
                        pass
                toast_text = f"📍 {session.current_map().get('name', '')}"
                toast_timer_ms = 2400.0
            elif (session.x, session.y) != (prev_x, prev_y):
                slide_dx = (prev_x - session.x) * TILE_PX
                slide_dy = (prev_y - session.y) * TILE_PX
                move_px_accum = 0.0
                ice_sliding = bool(session.last_slide_was_ice)
                if not ice_sliding:
                    stride_count += 1
                    walk_frame = (walk_frame + 1) % 4
                    walk_timer = TILE_PX // WALK_STEP_PX
                else:
                    walk_timer = 0
                move_cooldown = 0
            else:
                ice_sliding = False
                move_cooldown = 8

        def load_img(subpath: str) -> Optional[pygame.Surface]:
            if subpath in image_cache:
                return image_cache[subpath]
            if rgssad_reader is not None:
                raw = rgssad_reader.read_bytes(f"Graphics/{subpath}.png", extensions=(".png",))
                if raw is not None:
                    try:
                        surf = pygame.image.load(io.BytesIO(raw)).convert_alpha()
                        image_cache[subpath] = surf
                        return surf
                    except Exception:
                        pass
            if not gfx_root:
                image_cache[subpath] = None
                return None
            p = gfx_root / f"{subpath}.png"
            if p.exists():
                surf = pygame.image.load(str(p)).convert_alpha()
                image_cache[subpath] = surf
                return surf
            image_cache[subpath] = None
            return None

        def draw_tile(
            surface: pygame.Surface,
            ts_fields: Dict,
            layer_grid: list,
            gx: int,
            gy: int,
            tid: int,
            px: int,
            py: int,
            anim_frame: int = 0,
        ) -> None:
            if tid == 0:
                return
            if 48 <= tid < 384:
                slot = (tid - 48) // 48
                auto_names = ts_fields.get("autotile_names", [])
                aname = auto_names[slot] if slot < len(auto_names) else ""
                img = load_img(f"Autotiles/{aname}") if aname else None
                if img is not None:
                    q_ids = resolve_autotile_quads(layer_grid, gx, gy, tid)
                    fx_off = autotile_frame_x_offset(anim_frame, img.get_width())
                    quad_offsets = ((0, 0), (16, 0), (0, 16), (16, 16))
                    for sub_id, (ox, oy) in zip(q_ids, quad_offsets):
                        sx, sy, sw, sh = subtile_pixel_rect(sub_id, frame_x_offset=fx_off)
                        surface.blit(img, (px + ox, py + oy), pygame.Rect(sx, sy, sw, sh))
                else:
                    pygame.draw.rect(surface, (48, 122, 196), (px, py, TILE_PX, TILE_PX))
                return
            if tid >= 384:
                ts_img_name = ts_fields.get("tileset_name", "")
                img = load_img(f"Tilesets/{ts_img_name}") if ts_img_name else None
                rel = tid - 384
                sx = (rel % 8) * TILE_PX
                sy = (rel // 8) * TILE_PX
                if img is not None and sx + TILE_PX <= img.get_width() and sy + TILE_PX <= img.get_height():
                    surface.blit(img, (px, py), pygame.Rect(sx, sy, TILE_PX, TILE_PX))
                else:
                    fallback = (80, 145, 80) if tid == 384 else (45, 50, 62)
                    pygame.draw.rect(surface, fallback, (px, py, TILE_PX, TILE_PX))

        def draw_character(
            surface: pygame.Surface,
            char_name: str,
            direction: str,
            px: int,
            py: int,
            fallback_col: Tuple[int, int, int],
            step_col: int = 0,
            opacity: int = 255,
            blend_type: int = 0,
            jump_px: int = 0,
            hue: int = 0,
            on_bush: bool = False,
        ) -> None:
            op = max(0, min(255, int(opacity)))
            if op <= 0:
                return
            py_draw = int(py) - max(0, int(jump_px))
            dir_row = {"down": 0, "left": 1, "right": 2, "up": 3}.get(direction, 0)
            img = load_img(f"Characters/{char_name}") if char_name else None
            flags = 0
            if int(blend_type) == 1:
                flags = getattr(pygame, "BLEND_ADD", 0)
            elif int(blend_type) == 2:
                flags = getattr(pygame, "BLEND_SUB", 0)
            if img is not None:
                if int(hue) % 360 != 0:
                    img = apply_hue_shift(pygame, img, int(hue), cache=hue_cache)
                cw = img.get_width() // 4
                ch = img.get_height() // 4
                col_idx = step_col % 4
                rect = pygame.Rect(col_idx * cw, dir_row * ch, cw, ch)
                dest = (px + (TILE_PX - cw) // 2, py_draw + TILE_PX - ch)
                if on_bush and ch > 8:
                    bush_h = max(6, ch // 4)
                    top_h = ch - bush_h
                    top_rect = pygame.Rect(rect.x, rect.y, cw, top_h)
                    bot_rect = pygame.Rect(rect.x, rect.y + top_h, cw, bush_h)
                    top_sub = img.subsurface(top_rect).copy()
                    if op < 255:
                        top_sub.set_alpha(op)
                    surface.blit(top_sub, dest, special_flags=flags)
                    bot_sub = img.subsurface(bot_rect).copy()
                    bot_sub.set_alpha(max(1, op // 2))
                    surface.blit(bot_sub, (dest[0], dest[1] + top_h), special_flags=flags)
                elif op < 255:
                    sub = img.subsurface(rect).copy()
                    sub.set_alpha(op)
                    surface.blit(sub, dest, special_flags=flags)
                else:
                    surface.blit(img, dest, rect, special_flags=flags)
            else:
                if op < 255 or flags or on_bush:
                    tmp = pygame.Surface((TILE_PX, TILE_PX), pygame.SRCALPHA)
                    pygame.draw.circle(tmp, (*fallback_col[:3], op), (TILE_PX // 2, TILE_PX // 2), 11)
                    if on_bush:
                        tmp.fill((255, 255, 255, 128), pygame.Rect(0, TILE_PX - 8, TILE_PX, 8), special_flags=getattr(pygame, "BLEND_RGBA_MULT", 0))
                    surface.blit(tmp, (px, py_draw), special_flags=flags)
                else:
                    pygame.draw.circle(surface, fallback_col, (px + TILE_PX // 2, py_draw + TILE_PX // 2), 11)

        BALLOON_SYMBOLS = {1: "!", 2: "?", 3: "♪", 4: "♥", 5: "!", 6: ";;", 7: "...", 8: "*", 9: "Z", 10: "X"}

        def _char_world_center(cid: int, m_events: Dict[int, Dict[str, Any]]) -> Optional[Tuple[int, int]]:
            if cid <= 0:
                return (session.x * TILE_PX + TILE_PX // 2, session.y * TILE_PX + TILE_PX // 2)
            if cid in m_events:
                ev = m_events[cid]
                return (int(ev["x"]) * TILE_PX + TILE_PX // 2, int(ev["y"]) * TILE_PX + TILE_PX // 2)
            return None

        def _draw_pending_animations(cam_x: int, cam_y: int) -> None:
            while session.pending_animations:
                ev_anim = session.pending_animations.pop(0)
                aid = int(ev_anim.get("animation_id", 1))
                anim_entry = getattr(session, "animations_db", {}).get(aid, {})
                frames_list = anim_entry.get("fields", {}).get("frames", []) if isinstance(anim_entry, dict) else []
                total_f = max(1, len(frames_list)) * ANIMATION_TICKS_PER_FRAME if frames_list else 12
                active_anims.append({
                    "character_id": int(ev_anim.get("character_id", -1)),
                    "animation_id": aid,
                    "frames_left": total_f,
                    "total_frames": total_f,
                })
            if not active_anims:
                return
            palette = [(255, 220, 80), (120, 220, 255), (255, 130, 180)]
            m_events = {int(ev.get("id", 0)): ev for ev in session.current_map().get("events", [])}
            kept: List[Dict[str, Any]] = []
            for anim in active_anims:
                pos = _char_world_center(int(anim["character_id"]), m_events)
                if pos is None:
                    continue
                wx, wy = pos
                sx, sy = wx - cam_x, wy - cam_y
                aid = int(anim["animation_id"])
                anim_entry = getattr(session, "animations_db", {}).get(aid, {})
                a_fields = anim_entry.get("fields", {}) if isinstance(anim_entry, dict) else {}
                a_pos = int(a_fields.get("position", 1) or 0)
                if a_pos == 0:
                    sy -= TILE_PX  # top of a 32x48 character sprite
                elif a_pos == 2:
                    sy += TILE_PX // 2  # feet
                elif a_pos == 3:
                    sx, sy = VIEW_W // 2, VIEW_H // 2  # whole screen
                fl = int(anim["frames_left"])
                tot = max(1, int(anim.get("total_frames", 12)))
                elapsed = tot - fl
                frames_list = anim_entry.get("fields", {}).get("frames", []) if isinstance(anim_entry, dict) else []
                f_idx = min(len(frames_list) - 1, elapsed // ANIMATION_TICKS_PER_FRAME) if frames_list else 0
                triggered = anim.setdefault("triggered_frames", set())
                if f_idx not in triggered and isinstance(anim_entry, dict):
                    triggered.add(f_idx)
                    for timing in anim_entry.get("fields", {}).get("timings", []):
                        tf = timing.get("fields", timing) if isinstance(timing, dict) else {}
                        if int(tf.get("frame", -1)) != f_idx:
                            continue
                        se_f = tf.get("se", {}).get("fields", tf.get("se", {})) if isinstance(tf.get("se"), dict) else {}
                        se_name = str(se_f.get("name", "") or "")
                        if se_name:
                            session.played_se_queue.append({
                                "name": se_name,
                                "volume": int(se_f.get("volume", 80)),
                                "pitch": int(se_f.get("pitch", 100)),
                            })
                        f_scope = int(tf.get("flash_scope", 0))
                        f_dur = int(tf.get("flash_duration", 0))
                        fc = tf.get("flash_color")
                        if isinstance(fc, dict):
                            fc = fc.get("rgba")
                        if f_scope in (1, 2) and f_dur > 0 and isinstance(fc, list) and len(fc) >= 3:
                            session.pending_flash = {
                                "rgba": [max(0, min(255, int(c))) for c in (fc + [180])[:4]],
                                "frames": f_dur,
                                "max_frames": f_dur,
                            }
                col = palette[aid % len(palette)]
                _render_animation_frame_cells(
                    pygame,
                    screen,
                    load_img,
                    anim_entry,
                    f_idx,
                    sx,
                    sy,
                    fallback_col=col,
                    elapsed_frames=max(1, tot + 1 - fl),
                )
                if fl > 1:
                    anim["frames_left"] = fl - 1
                    kept.append(anim)
            active_anims[:] = kept

        def _draw_pending_balloons(cam_x: int, cam_y: int) -> None:
            while getattr(session, "pending_balloons", None):
                ev_bal = session.pending_balloons.pop(0)
                active_balloons.append({
                    "character_id": int(ev_bal.get("character_id", -1)),
                    "balloon_id": max(1, int(ev_bal.get("balloon_id", 1))),
                    "frames_left": 24,
                })
            if not active_balloons:
                return
            m_events = {int(ev.get("id", 0)): ev for ev in session.current_map().get("events", [])}
            kept_b: List[Dict[str, Any]] = []
            for bal in active_balloons:
                pos = _char_world_center(int(bal["character_id"]), m_events)
                if pos is None:
                    continue
                wx, wy = pos
                fl = int(bal["frames_left"])
                bob = min(8, (24 - fl) // 2)
                sx, sy = wx - cam_x, wy - cam_y - TILE_PX - bob
                bid = int(bal["balloon_id"])
                sym = BALLOON_SYMBOLS.get(bid, "!")
                b_col = (245, 95, 120) if bid in (4, 5) else ((90, 175, 250) if bid == 2 else (255, 220, 85))
                pygame.draw.rect(screen, (250, 252, 255), (sx - 12, sy - 12, 24, 20), border_radius=6)
                pygame.draw.rect(screen, (40, 48, 68), (sx - 12, sy - 12, 24, 20), 1, border_radius=6)
                txt = _render_text(small_font, sym, True, b_col)
                screen.blit(txt, (sx - txt.get_width() // 2, sy - 10))
                if fl > 1:
                    bal["frames_left"] = fl - 1
                    kept_b.append(bal)
            active_balloons[:] = kept_b

        active_anims: List[Dict[str, Any]] = []
        active_balloons: List[Dict[str, Any]] = []
        windowskin_cache: Dict[str, Dict[str, Tuple[int, int, int]]] = {}
        sound_cache: Dict[Tuple[str, int], Any] = {}
        while running:
            ui_theme = resolve_windowskin_theme(session.windowskin_name, load_img=load_img, cache=windowskin_cache)
            if getattr(session, "pending_menu_open", False) and not session.pending and not session.game_over:
                session.pending_menu_open = False
                menu_hub_open = True
                menu_cursor = 0
                show_status = False
                show_quests = False

            def _apply_title_choice(idx_pick: int, adjust_delta: int = 0) -> None:
                nonlocal running, title_mode, title_cursor, show_minimap, toast_text, toast_timer_ms, slot_overlay_mode, slot_overlay_return_title, slot_cursor, options_return_to_menu, menu_hub_open, capturing_key_action
                res = process_title_selection(
                    session, title_mode, idx_pick, show_minimap, settings=settings, adjust_delta=adjust_delta, include_controls=True
                )
                show_minimap = bool(res["show_minimap"])
                save_player_settings(ir_dir, settings)
                _refresh_keybindings()
                if res["toast"]:
                    toast_text = str(res["toast"])
                    toast_timer_ms = 2000.0
                if res["action"] == "quit":
                    running = False
                elif res["action"] == "load_rejected":
                    _queue_system_se(session, "buzzer")
                elif res.get("capture_target"):
                    capturing_key_action = str(res["capture_target"])
                    toast_timer_ms = 4000.0
                elif res["action"] == "capture_confirm_key":
                    capturing_key_action = "confirm"
                    toast_timer_ms = 4000.0
                elif res["action"] == "open_load":
                    slot_overlay_mode = "load"
                    slot_overlay_return_title = True
                    slot_cursor = max(0, active_slot - 1)
                    title_mode = "map"
                elif res["action"] == "back" and options_return_to_menu:
                    options_return_to_menu = False
                    title_mode = "map"
                    menu_hub_open = True
                    title_cursor = 0
                elif res["next_mode"] != title_mode:
                    title_mode = str(res["next_mode"])
                    title_cursor = 0

            def _apply_slot_overlay(idx_pick: int) -> None:
                nonlocal slot_overlay_mode, active_slot, toast_text, toast_timer_ms, title_mode, slide_dx, slide_dy, move_cooldown, move_px_accum, show_status, slot_overlay_return_menu, menu_hub_open, overlay_confirm_slot
                rows = build_save_slot_rows(session, include_autosave=(slot_overlay_mode == "load"))
                if not rows:
                    return
                picked = rows[idx_pick % len(rows)]
                chosen_slot = int(picked.get("slot", idx_pick + 1))
                if slot_overlay_mode == "save":
                    if not session.save_enabled:
                        _queue_system_se(session, "buzzer")
                        toast_text = "현재 구역에서는 저장이 금지되어 있습니다"
                    elif session.slot_exists(chosen_slot) and overlay_confirm_slot != chosen_slot:
                        overlay_confirm_slot = chosen_slot
                        _queue_system_se(session, "cursor")
                        toast_text = f"슬롯 {chosen_slot}을 덮어쓰시겠습니까? 한 번 더 확인"
                    else:
                        overlay_confirm_slot = None
                        active_slot = chosen_slot
                        session.save_to_file(slot=chosen_slot)
                        _queue_system_se(session, "decision")
                        toast_text = f"슬롯 {chosen_slot} 저장 완료"
                        slot_overlay_mode = None
                        slot_overlay_return_menu = False
                elif slot_overlay_mode == "load":
                    overlay_confirm_slot = None
                    loaded_ok = False
                    load_label = "자동 저장" if chosen_slot == 0 else f"슬롯 {chosen_slot}"
                    try:
                        if chosen_slot == 0:
                            if not session.autosave_exists():
                                _queue_system_se(session, "buzzer")
                                toast_text = "자동 저장 파일이 없습니다"
                            else:
                                loaded_ok = session.load_autosave()
                        elif not session.slot_exists(chosen_slot):
                            _queue_system_se(session, "buzzer")
                            toast_text = f"슬롯 {chosen_slot}에 저장된 파일이 없습니다"
                        else:
                            active_slot = chosen_slot
                            loaded_ok = session.load_from_file(slot=chosen_slot)
                    except ValueError:
                        _queue_system_se(session, "buzzer")
                        toast_text = f"{load_label} 로드 거부됨: 저장 데이터 무결성 검증 실패"
                        loaded_ok = False
                    if loaded_ok:
                        slide_dx, slide_dy, move_cooldown = 0, 0, 0
                        move_px_accum = 0.0
                        show_status = False
                        menu_hub_open = False
                        slot_overlay_return_menu = False
                        npc_slides.clear()
                        npc_step_timers.clear()
                        _queue_system_se(session, "decision")
                        if not bool(getattr(session, "save_checksum_valid", True)):
                            toast_text = f"{load_label} 로드 완료 (경고: 저장 데이터 무결성 확인 실패)"
                        else:
                            toast_text = f"{load_label} 불러오기 완료"
                        slot_overlay_mode = None
                toast_timer_ms = 2200.0

            def _apply_menu_hub_choice(idx_pick: int) -> None:
                nonlocal menu_hub_open, show_status, status_mode, status_cursor, show_quests, slot_overlay_mode, slot_overlay_return_menu, slot_cursor, title_mode, title_cursor, options_return_to_menu, toast_text, toast_timer_ms, overlay_confirm_slot
                items = build_main_menu_items(session)
                if not items:
                    return
                act_id, _lbl, enabled = items[idx_pick % len(items)]
                if not enabled:
                    _queue_system_se(session, "buzzer")
                    toast_text = "현재 사용할 수 없는 메뉴입니다"
                    toast_timer_ms = 1600.0
                    return
                _queue_system_se(session, "decision")
                if act_id == "use":
                    menu_hub_open = False
                    show_status = True
                    status_mode = "use"
                    status_cursor = 0
                elif act_id == "equip":
                    menu_hub_open = False
                    show_status = True
                    status_mode = "equip"
                    status_cursor = 0
                elif act_id == "quests":
                    menu_hub_open = False
                    show_quests = True
                elif act_id == "save":
                    menu_hub_open = False
                    overlay_confirm_slot = None
                    slot_overlay_mode = "save"
                    slot_overlay_return_menu = True
                    slot_cursor = max(0, active_slot - 1)
                elif act_id == "load":
                    menu_hub_open = False
                    overlay_confirm_slot = None
                    slot_overlay_mode = "load"
                    slot_overlay_return_menu = True
                    slot_cursor = max(0, active_slot - 1)
                elif act_id == "options":
                    menu_hub_open = False
                    options_return_to_menu = True
                    title_mode = "options"
                    title_cursor = 0
                elif act_id == "title":
                    menu_hub_open = False
                    title_mode = "title"
                    title_cursor = 0
                else:
                    menu_hub_open = False

            def _handle_fullscreen_hotkey(ev: Any) -> bool:
                if ev.key == pygame.K_F11 or (ev.key == pygame.K_RETURN and (pygame.key.get_mods() & pygame.KMOD_ALT)):
                    _toggle_fullscreen()
                    return True
                return False

            if slot_overlay_mode in ("save", "load"):
                def _close_slot_overlay() -> None:
                    nonlocal slot_overlay_mode, title_mode, slot_overlay_return_title, menu_hub_open, slot_overlay_return_menu, overlay_confirm_slot
                    overlay_confirm_slot = None
                    slot_overlay_mode = None
                    if slot_overlay_return_title:
                        title_mode = "title"
                        slot_overlay_return_title = False
                    elif slot_overlay_return_menu:
                        menu_hub_open = True
                        slot_overlay_return_menu = False

                slot_rows = build_save_slot_rows(session, include_autosave=(slot_overlay_mode == "load"))
                total_rows = max(1, len(slot_rows))
                slot_cursor = slot_cursor % total_rows
                for event in pygame.event.get():
                    if event.type == pygame.QUIT:
                        running = False
                    elif getattr(pygame, "VIDEORESIZE", None) and event.type == pygame.VIDEORESIZE:
                        _queue_video_resize(event)
                    elif event.type == pygame.KEYDOWN:
                        if _handle_fullscreen_hotkey(event):
                            continue
                        if event.key in up_keys:
                            slot_cursor = (slot_cursor - 1) % total_rows
                            overlay_confirm_slot = None
                            _queue_system_se(session, "cursor")
                        elif event.key in down_keys:
                            slot_cursor = (slot_cursor + 1) % total_rows
                            overlay_confirm_slot = None
                            _queue_system_se(session, "cursor")
                        elif event.key == pygame.K_DELETE:
                            cur_row = slot_rows[slot_cursor % total_rows]
                            del_slot = int(cur_row.get("slot", 0))
                            if del_slot >= 1 and session.delete_slot(del_slot):
                                _queue_system_se(session, "decision")
                                toast_text = f"슬롯 {del_slot} 삭제 완료"
                            else:
                                _queue_system_se(session, "buzzer")
                                toast_text = "삭제할 수 있는 슬롯이 없습니다"
                            overlay_confirm_slot = None
                            toast_timer_ms = 2000.0
                        elif event.key == pygame.K_F10 and slot_overlay_mode == "load":
                            if session.autosave_exists():
                                auto_idx = next((i for i, r in enumerate(slot_rows) if int(r.get("slot", -1)) == 0), total_rows - 1)
                                _apply_slot_overlay(auto_idx)
                            else:
                                _queue_system_se(session, "buzzer")
                                toast_text = "자동 저장 파일이 없습니다"
                                toast_timer_ms = 1800.0
                        elif event.key in (cancel_keys | {pygame.K_ESCAPE, custom_menu_key}):
                            _queue_system_se(session, "cancel")
                            _close_slot_overlay()
                        elif event.key in confirm_keys:
                            _apply_slot_overlay(slot_cursor)
                    elif event.type in (getattr(pygame, "JOYHATMOTION", -1), getattr(pygame, "JOYAXISMOTION", -1), getattr(pygame, "JOYBUTTONDOWN", -1)):
                        gp_act = _translate_gamepad_event(pygame, event, joy_axis_held)
                        if gp_act == "up":
                            slot_cursor = (slot_cursor - 1) % total_rows
                            overlay_confirm_slot = None
                            _queue_system_se(session, "cursor")
                        elif gp_act == "down":
                            slot_cursor = (slot_cursor + 1) % total_rows
                            overlay_confirm_slot = None
                            _queue_system_se(session, "cursor")
                        elif gp_act in ("cancel", "menu"):
                            _queue_system_se(session, "cancel")
                            _close_slot_overlay()
                        elif gp_act == "confirm":
                            _apply_slot_overlay(slot_cursor)
                    elif event.type == pygame.MOUSEWHEEL:
                        slot_cursor = (slot_cursor - int(getattr(event, "y", 0))) % total_rows
                        overlay_confirm_slot = None
                    elif event.type == pygame.MOUSEBUTTONDOWN and getattr(event, "button", 1) == 3:
                        _queue_system_se(session, "cancel")
                        _close_slot_overlay()
                    elif event.type == pygame.MOUSEBUTTONDOWN and getattr(event, "button", 1) == 1:
                        lpos = window_to_logical_pos(event.pos, _current_letterbox())
                        hit = hit_test_rects(lpos, last_slot_rects)
                        if hit is not None:
                            slot_cursor = hit
                            _apply_slot_overlay(hit)
                screen.fill((14, 18, 30))
                last_slot_rects = _draw_save_slot_overlay(
                    pygame, screen, font, small_font, session, slot_overlay_mode or "load", slot_cursor,
                    toast_text=toast_text, theme=ui_theme, load_img=load_img, settings=settings,
                )
                _present_frame()
                if toast_timer_ms > 0.0:
                    toast_timer_ms = max(0.0, toast_timer_ms - last_dt_ms)
                    if toast_timer_ms == 0.0:
                        toast_text = ""
                if on_frame is not None:
                    on_frame(anim_tick, screen, session)
                anim_tick += 1
                if max_frames is not None and anim_tick >= max_frames:
                    running = False
                last_dt_ms = _clamp_frame_dt(float(clock.tick(TARGET_FPS)))
                continue

            if menu_hub_open:
                m_items = build_main_menu_items(session)
                for event in pygame.event.get():
                    if event.type == pygame.QUIT:
                        running = False
                    elif getattr(pygame, "VIDEORESIZE", None) and event.type == pygame.VIDEORESIZE:
                        _queue_video_resize(event)
                    elif event.type == pygame.KEYDOWN:
                        if _handle_fullscreen_hotkey(event):
                            continue
                        if event.key in up_keys:
                            menu_cursor = (menu_cursor - 1) % len(m_items)
                        elif event.key in down_keys:
                            menu_cursor = (menu_cursor + 1) % len(m_items)
                        elif event.key in (cancel_keys | {pygame.K_ESCAPE, custom_menu_key}):
                            menu_hub_open = False
                        elif event.key in confirm_keys:
                            _apply_menu_hub_choice(menu_cursor)
                    elif event.type in (getattr(pygame, "JOYHATMOTION", -1), getattr(pygame, "JOYAXISMOTION", -1), getattr(pygame, "JOYBUTTONDOWN", -1)):
                        gp_act = _translate_gamepad_event(pygame, event, joy_axis_held)
                        if gp_act == "up":
                            menu_cursor = (menu_cursor - 1) % len(m_items)
                        elif gp_act == "down":
                            menu_cursor = (menu_cursor + 1) % len(m_items)
                        elif gp_act in ("cancel", "menu"):
                            menu_hub_open = False
                        elif gp_act == "confirm":
                            _apply_menu_hub_choice(menu_cursor)
                    elif event.type == pygame.MOUSEWHEEL:
                        menu_cursor = (menu_cursor - int(getattr(event, "y", 0))) % len(m_items)
                    elif event.type == pygame.MOUSEBUTTONDOWN and getattr(event, "button", 1) == 3:
                        _queue_system_se(session, "cancel")
                        menu_hub_open = False
                    elif event.type == pygame.MOUSEBUTTONDOWN and getattr(event, "button", 1) == 1:
                        lpos = window_to_logical_pos(event.pos, _current_letterbox())
                        hit = hit_test_rects(lpos, last_menu_rects)
                        if hit is not None:
                            menu_cursor = hit
                            _apply_menu_hub_choice(hit)
                screen.fill((12, 16, 26))
                last_menu_rects = _draw_main_menu_overlay(
                    pygame, screen, font, small_font, session, menu_cursor, theme=ui_theme, load_img=load_img, settings=settings
                )
                if toast_text:
                    screen.blit(_render_text(small_font, toast_text, True, ui_theme["accent"]), (24, VIEW_H - 28))
                _present_frame()
                if toast_timer_ms > 0.0:
                    toast_timer_ms = max(0.0, toast_timer_ms - last_dt_ms)
                    if toast_timer_ms == 0.0:
                        toast_text = ""
                if on_frame is not None:
                    on_frame(anim_tick, screen, session)
                anim_tick += 1
                if max_frames is not None and anim_tick >= max_frames:
                    running = False
                last_dt_ms = _clamp_frame_dt(float(clock.tick(TARGET_FPS)))
                continue

            if title_mode in ("title", "options"):
                t_opts = build_title_menu_options(
                    session, mode=title_mode, show_minimap=show_minimap, settings=settings, include_controls=True
                )

                def _leave_options() -> None:
                    nonlocal options_return_to_menu, title_mode, menu_hub_open, title_cursor
                    if options_return_to_menu:
                        options_return_to_menu = False
                        title_mode = "map"
                        menu_hub_open = True
                    else:
                        title_mode = "title"
                    title_cursor = 0

                for event in pygame.event.get():
                    if event.type == pygame.QUIT:
                        running = False
                    elif getattr(pygame, "VIDEORESIZE", None) and event.type == pygame.VIDEORESIZE:
                        _queue_video_resize(event)
                    elif event.type == pygame.KEYDOWN:
                        if _handle_fullscreen_hotkey(event):
                            continue
                        if capturing_key_action is not None:
                            if event.key == pygame.K_ESCAPE:
                                capturing_key_action = None
                                toast_text = "키 변경이 취소되었습니다"
                            else:
                                kname = str(pygame.key.name(event.key)).lower()
                                kb_cfg = settings.setdefault("keybindings", {})
                                if isinstance(kb_cfg, dict):
                                    kb_cfg[capturing_key_action] = kname
                                save_player_settings(ir_dir, settings)
                                _refresh_keybindings()
                                toast_text = f"{capturing_key_action.upper()} 키가 [{kname.upper()}]로 변경되었습니다"
                                capturing_key_action = None
                            toast_timer_ms = 2200.0
                            continue
                        if event.key in up_keys:
                            title_cursor = (title_cursor - 1) % len(t_opts)
                            _queue_system_se(session, "cursor")
                        elif event.key in down_keys:
                            title_cursor = (title_cursor + 1) % len(t_opts)
                            _queue_system_se(session, "cursor")
                        elif event.key in left_keys and title_mode == "options":
                            _queue_system_se(session, "cursor")
                            _apply_title_choice(title_cursor, adjust_delta=-1)
                        elif event.key in right_keys and title_mode == "options":
                            _queue_system_se(session, "cursor")
                            _apply_title_choice(title_cursor, adjust_delta=1)
                        elif event.key in (cancel_keys | {pygame.K_ESCAPE, custom_menu_key}) and title_mode == "options":
                            _queue_system_se(session, "cancel")
                            _leave_options()
                        elif event.key in confirm_keys:
                            _queue_system_se(session, "decision")
                            _apply_title_choice(title_cursor)
                    elif event.type in (getattr(pygame, "JOYHATMOTION", -1), getattr(pygame, "JOYAXISMOTION", -1), getattr(pygame, "JOYBUTTONDOWN", -1)):
                        gp_act = _translate_gamepad_event(pygame, event, joy_axis_held)
                        if gp_act == "up":
                            title_cursor = (title_cursor - 1) % len(t_opts)
                        elif gp_act == "down":
                            title_cursor = (title_cursor + 1) % len(t_opts)
                        elif gp_act == "left" and title_mode == "options":
                            _apply_title_choice(title_cursor, adjust_delta=-1)
                        elif gp_act == "right" and title_mode == "options":
                            _apply_title_choice(title_cursor, adjust_delta=1)
                        elif gp_act in ("cancel", "menu") and title_mode == "options":
                            _leave_options()
                        elif gp_act == "confirm":
                            _apply_title_choice(title_cursor)
                    elif event.type == pygame.MOUSEWHEEL:
                        title_cursor = (title_cursor - int(getattr(event, "y", 0))) % len(t_opts)
                    elif event.type == pygame.MOUSEBUTTONDOWN and getattr(event, "button", 1) == 3 and title_mode == "options":
                        _queue_system_se(session, "cancel")
                        _leave_options()
                    elif event.type == pygame.MOUSEBUTTONDOWN and getattr(event, "button", 1) == 1:
                        lpos = window_to_logical_pos(event.pos, _current_letterbox())
                        hit = hit_test_rects(lpos, last_title_rects)
                        if hit is not None:
                            title_cursor = hit
                            _queue_system_se(session, "decision")
                            _apply_title_choice(hit)
                last_title_rects = _draw_title_screen(
                    pygame, screen, font, small_font, session, title_mode, title_cursor, show_minimap,
                    toast_text=toast_text, theme=ui_theme, load_img=load_img, settings=settings, include_controls=True,
                    capturing_action=capturing_key_action,
                )
                if getattr(session, "title_bgm", None):
                    playing_bgm = _sync_override_bgm(
                        pygame,
                        audio_root,
                        session.title_bgm,
                        playing_bgm,
                        mixer_ok,
                        settings=settings,
                        muted=bool(getattr(session, "audio_muted", False)),
                        rgssad_reader=rgssad_reader,
                    )
                _present_frame()
                if toast_timer_ms > 0.0:
                    toast_timer_ms = max(0.0, toast_timer_ms - last_dt_ms)
                    if toast_timer_ms == 0.0:
                        toast_text = ""
                if on_frame is not None:
                    on_frame(anim_tick, screen, session)
                anim_tick += 1
                if max_frames is not None and anim_tick >= max_frames:
                    running = False
                last_dt_ms = _clamp_frame_dt(float(clock.tick(TARGET_FPS)))
                continue
            is_sliding = slide_dx != 0 or slide_dy != 0
            for event in pygame.event.get():
                if event.type == pygame.QUIT:
                    running = False
                elif getattr(pygame, "VIDEORESIZE", None) and event.type == pygame.VIDEORESIZE:
                    _queue_video_resize(event)
                elif getattr(pygame, "JOYDEVICEADDED", None) and event.type == pygame.JOYDEVICEADDED:
                    try:
                        joy = pygame.joystick.Joystick(int(getattr(event, "device_index", 0)))
                        joy.init()
                        joysticks.append(joy)
                    except Exception:
                        pass
                elif event.type in (
                    getattr(pygame, "JOYHATMOTION", -1),
                    getattr(pygame, "JOYAXISMOTION", -1),
                    getattr(pygame, "JOYBUTTONDOWN", -1),
                ):
                    ax_idx = int(getattr(event, "axis", -1))
                    is_axis_ev = event.type == getattr(pygame, "JOYAXISMOTION", None)
                    prev_ax = joy_axis_held.get(ax_idx) if is_axis_ev else None
                    gp_act = _translate_gamepad_event(pygame, event, joy_axis_held)
                    if is_axis_ev and ax_idx in (0, 1) and prev_ax and prev_ax != gp_act and prev_ax in held_dir_order:
                        held_dir_order.remove(prev_ax)
                    if gp_act is None:
                        continue
                    if show_quests:
                        if gp_act in ("confirm", "cancel", "menu"):
                            show_quests = False
                    elif show_status and not session.pending:
                        gp_key_map = {
                            "up": pygame.K_UP,
                            "down": pygame.K_DOWN,
                            "left": pygame.K_LEFT,
                            "right": pygame.K_RIGHT,
                            "confirm": pygame.K_RETURN,
                            "cancel": pygame.K_ESCAPE,
                            "menu": pygame.K_ESCAPE,
                        }
                        if gp_act in gp_key_map:
                            show_status, status_cursor, status_actor_idx, status_mode = _process_status_panel_action(
                                pygame, gp_key_map[gp_act], session, status_cursor, status_actor_idx, status_mode
                            )
                            held_dir_order.clear()
                            tap_dirs.clear()
                    elif session.pending:
                        if gp_act in ("up", "left") and session.pending.choices:
                            choice_cursor = (choice_cursor - 1) % len(session.pending.choices)
                        elif gp_act in ("down", "right") and session.pending.choices:
                            choice_cursor = (choice_cursor + 1) % len(session.pending.choices)
                        elif gp_act == "confirm":
                            running, choice_cursor, show_status, walk_frame, walk_timer = _process_keydown(
                                pygame, pygame.K_RETURN, session, choice_cursor, show_status, walk_frame, walk_timer, settings=settings
                            )
                        elif gp_act == "cancel":
                            running, choice_cursor, show_status, walk_frame, walk_timer = _process_keydown(
                                pygame, pygame.K_ESCAPE, session, choice_cursor, show_status, walk_frame, walk_timer, settings=settings
                            )
                    elif gp_act in ("up", "down", "left", "right"):
                        if is_axis_ev:
                            if gp_act not in held_dir_order:
                                held_dir_order.append(gp_act)
                        if not is_sliding and gp_act not in tap_dirs:
                            tap_dirs.append(gp_act)
                    elif gp_act == "confirm":
                        if session.game_over:
                            session.step("gameover_retry")
                        else:
                            session.step("interact")
                    elif gp_act in ("menu", "cancel") and not session.game_over and session.menu_enabled:
                        menu_hub_open = True
                        menu_cursor = 0
                        held_dir_order.clear()
                        tap_dirs.clear()
                elif event.type == pygame.MOUSEWHEEL:
                    dy = int(getattr(event, "y", 0))
                    if show_quests:
                        max_q = max(0, len(session.list_quest_lines()) - 12)
                        quest_scroll = max(0, min(max_q, quest_scroll - dy))
                    elif show_status and not session.pending:
                        step_k = pygame.K_UP if dy > 0 else pygame.K_DOWN
                        show_status, status_cursor, status_actor_idx, status_mode = _process_status_panel_action(
                            pygame, step_k, session, status_cursor, status_actor_idx, status_mode
                        )
                    elif session.pending and session.pending.choices:
                        n_c = len(session.pending.choices)
                        choice_cursor = (choice_cursor - dy) % n_c
                elif event.type == pygame.MOUSEBUTTONDOWN and getattr(event, "button", 1) == 1:
                    lpos = window_to_logical_pos(event.pos, _current_letterbox())
                    if show_quests and last_quest_close_rects:
                        if hit_test_rects(lpos, last_quest_close_rects) is not None:
                            show_quests = False
                    elif show_status and not session.pending and last_status_rects:
                        hit_st = hit_test_rects(lpos, [r[2] for r in last_status_rects])
                        if hit_st is not None:
                            kind_st, val_st, _rect_st = last_status_rects[hit_st]
                            if kind_st == "close":
                                show_status = False
                            elif kind_st == "toggle_tab":
                                show_status, status_cursor, status_actor_idx, status_mode = _process_status_panel_action(
                                    pygame, pygame.K_TAB, session, status_cursor, status_actor_idx, status_mode
                                )
                            elif kind_st == "item":
                                status_cursor = int(val_st)
                                show_status, status_cursor, status_actor_idx, status_mode = _process_status_panel_action(
                                    pygame, pygame.K_RETURN, session, status_cursor, status_actor_idx, status_mode
                                )
                    elif session.pending and last_choice_rects:
                        hit = hit_test_rects(lpos, last_choice_rects)
                        if hit is not None:
                            if session.pending.choices:
                                choice_cursor = hit
                                session.step(f"choose:{hit}")
                            else:
                                session.step("interact")
                elif event.type == pygame.MOUSEBUTTONDOWN and getattr(event, "button", 1) == 3:
                    _queue_system_se(session, "cancel")
                    if show_quests:
                        show_quests = False
                    elif show_status and not session.pending:
                        show_status, status_cursor, status_actor_idx, status_mode = _process_status_panel_action(
                            pygame, pygame.K_ESCAPE, session, status_cursor, status_actor_idx, status_mode
                        )
                    elif session.pending:
                        running, choice_cursor, show_status, walk_frame, walk_timer = _process_keydown(
                            pygame, pygame.K_ESCAPE, session, choice_cursor, show_status, walk_frame, walk_timer, settings=settings
                        )
                    elif not session.game_over and session.menu_enabled:
                        menu_hub_open = True
                        menu_cursor = 0
                        held_dir_order.clear()
                        tap_dirs.clear()
                elif event.type == pygame.KEYUP and event.key in dir_key_map:
                    d_up = dir_key_map[event.key]
                    if d_up in held_dir_order:
                        held_dir_order.remove(d_up)
                elif event.type == pygame.KEYDOWN:
                    if _handle_fullscreen_hotkey(event):
                        continue
                    if session.pending and session.pending.number_input is not None:
                        ni = session.pending.number_input
                        digits = int(ni.get("digits", 4))
                        max_val = (10 ** digits) - 1
                        cur_v = int(ni.get("value", 0))
                        ch_u = getattr(event, "unicode", "")
                        if ch_u and ch_u.isdigit():
                            ni["value"] = min(max_val, (cur_v * 10 + int(ch_u)) % (10 ** digits))
                            session.pending.lines = [f"[숫자 입력] 현재 값: {ni['value']:0{digits}d} ({digits}자리)"]
                            continue
                        if event.key == pygame.K_BACKSPACE:
                            ni["value"] = cur_v // 10
                            session.pending.lines = [f"[숫자 입력] 현재 값: {ni['value']:0{digits}d} ({digits}자리)"]
                            continue
                        if event.key in confirm_keys:
                            choice_cursor = len(session.pending.choices) - 1
                            session.step(f"choose:{choice_cursor}")
                            choice_cursor = 0
                            continue
                    if session.pending and getattr(session.pending, "input_name", None) is not None:
                        nm = session.pending.input_name
                        cur_t = str(nm.get("text", ""))
                        max_c = int(nm.get("max_char", 12))
                        if event.key == pygame.K_BACKSPACE:
                            nm["text"] = cur_t[:-1]
                            continue
                        if event.key == pygame.K_RETURN:
                            session.submit_name_input(str(nm.get("text", "")))
                            choice_cursor = 0
                            continue
                        ch_u = getattr(event, "unicode", "")
                        if ch_u and ch_u.isprintable() and len(cur_t) < max_c:
                            nm["text"] = cur_t + ch_u
                            continue
                    if session.pending and getattr(session.pending, "button_input", None) is not None:
                        bkey_dir = dir_key_map.get(event.key)
                        dir_to_rmxp = {"down": 2, "left": 4, "right": 6, "up": 8}
                        if bkey_dir in dir_to_rmxp:
                            session.submit_button_input(dir_to_rmxp[bkey_dir])
                            choice_cursor = 0
                            continue
                        rmxp_code_map = {
                            pygame.K_LSHIFT: 11,
                            pygame.K_RSHIFT: 11,
                            pygame.K_ESCAPE: 12,
                            pygame.K_x: 12,
                            custom_cancel_key: 12,
                            pygame.K_RETURN: 13,
                            pygame.K_SPACE: 13,
                            pygame.K_z: 13,
                            custom_confirm_key: 13,
                        }
                        if event.key in rmxp_code_map:
                            session.submit_button_input(rmxp_code_map[event.key])
                            choice_cursor = 0
                            continue
                    if session.pending and session.pending.shop_goods and event.key in (pygame.K_TAB, pygame.K_q):
                        new_q = session.toggle_shop_qty()
                        toast_text = f"상점 수량 x{new_q}"
                        toast_timer_ms = 1200.0
                        continue
                    if event.key in (pygame.K_ESCAPE, custom_menu_key) and not show_status and not show_quests and not session.pending and not session.game_over:
                        if session.menu_enabled:
                            menu_hub_open = True
                            menu_cursor = 0
                            held_dir_order.clear()
                            tap_dirs.clear()
                        continue
                    was_confirming = pending_load_confirm
                    handled_sl, active_slot, pending_load_confirm, sl_msg = process_save_load_shortcut(
                        pygame, event.key, session, active_slot, pending_load_confirm
                    )
                    if handled_sl:
                        toast_text = sl_msg
                        toast_timer_ms = 3000.0 if pending_load_confirm else 2200.0
                        load_confirm_timer_ms = 3000.0 if pending_load_confirm else 0.0
                        if "거부됨" in sl_msg:
                            _queue_system_se(session, "buzzer")
                        elif was_confirming and not pending_load_confirm and event.key == pygame.K_F9:
                            slide_dx, slide_dy, move_cooldown = 0, 0, 0
                            move_px_accum = 0.0
                            status_cursor = 0
                            status_actor_idx = 0
                            show_status = False
                            npc_slides.clear()
                            npc_step_timers.clear()
                            held_dir_order.clear()
                            tap_dirs.clear()
                        continue
                    bound_nav = up_keys | down_keys | left_keys | right_keys | confirm_keys | cancel_keys
                    if (event.key == pygame.K_F8 or (event.key == pygame.K_n and event.key not in bound_nav)) and not session.pending:
                        show_minimap = not show_minimap
                        toast_text = "미니맵 표시" if show_minimap else "미니맵 숨김"
                        toast_timer_ms = 1400.0
                        continue
                    if event.key == pygame.K_F10:
                        is_muted = session.toggle_audio_mute()
                        toast_text = "오디오 음소거 ON" if is_muted else "오디오 음소거 OFF"
                        toast_timer_ms = 1400.0
                        continue
                    if event.key == pygame.K_o and event.key not in bound_nav and not session.pending and not session.game_over:
                        slot_overlay_mode = "save"
                        slot_overlay_return_title = False
                        slot_cursor = max(0, active_slot - 1)
                        continue
                    if event.key == pygame.K_l and event.key not in bound_nav and not session.pending:
                        slot_overlay_mode = "load"
                        slot_overlay_return_title = False
                        slot_cursor = max(0, active_slot - 1)
                        continue
                    if event.key == pygame.K_j and event.key not in bound_nav and not session.pending and not session.game_over:
                        show_quests = not show_quests
                        if show_quests:
                            show_status = False
                        held_dir_order.clear()
                        tap_dirs.clear()
                        continue
                    if show_quests:
                        if event.key in (confirm_keys | cancel_keys | {pygame.K_ESCAPE}):
                            show_quests = False
                        continue
                    if show_status and not session.pending:
                        status_keys = (
                            up_keys | down_keys | left_keys | right_keys | confirm_keys | cancel_keys
                            | {pygame.K_q, pygame.K_e, pygame.K_p, pygame.K_m, pygame.K_ESCAPE, pygame.K_TAB}
                        )
                        if event.key in status_keys:
                            norm_key = event.key
                            if event.key in up_keys:
                                norm_key = pygame.K_UP
                            elif event.key in down_keys:
                                norm_key = pygame.K_DOWN
                            elif event.key in left_keys:
                                norm_key = pygame.K_LEFT
                            elif event.key in right_keys:
                                norm_key = pygame.K_RIGHT
                            elif event.key in confirm_keys:
                                norm_key = pygame.K_RETURN
                            elif event.key in cancel_keys:
                                norm_key = pygame.K_ESCAPE
                            show_status, status_cursor, status_actor_idx, status_mode = _process_status_panel_action(
                                pygame, norm_key, session, status_cursor, status_actor_idx, status_mode
                            )
                            held_dir_order.clear()
                            tap_dirs.clear()
                            continue
                    if event.key == pygame.K_m and not session.pending:
                        show_status = True
                        status_mode = "use"
                        status_cursor = 0
                        held_dir_order.clear()
                        tap_dirs.clear()
                        continue
                    if event.key in dir_key_map and not session.pending and not show_status:
                        d_down = dir_key_map[event.key]
                        if pygame.key.get_mods() & pygame.KMOD_SHIFT:
                            if not is_sliding:
                                session.step(f"face:{d_down}")
                                walk_timer = 4
                            continue
                        if d_down in held_dir_order:
                            held_dir_order.remove(d_down)
                        held_dir_order.append(d_down)
                        if not is_sliding and d_down not in tap_dirs:
                            tap_dirs.append(d_down)
                        continue
                    if event.key == pygame.K_F12:
                        try:
                            shot_dir = ir_dir / "screenshots"
                            shot_dir.mkdir(parents=True, exist_ok=True)
                            shot_path = shot_dir / f"shot_{int(time.time())}_{anim_tick}.png"
                            pygame.image.save(screen, str(shot_path))
                            toast_text = f"스크린샷 저장: {shot_path.name}"
                        except Exception as exc:
                            toast_text = f"스크린샷 실패: {exc}"
                        toast_timer_ms = 2200.0
                        continue
                    if event.key == pygame.K_F5:
                        slide_dx, slide_dy, move_cooldown = 0, 0, 0
                        move_px_accum = 0.0
                        status_cursor = 0
                        status_actor_idx = 0
                        pending_load_confirm = False
                        npc_slides.clear()
                        npc_step_timers.clear()
                        map_layer_cache.clear()
                        held_dir_order.clear()
                        tap_dirs.clear()
                        toast_text = "프로젝트 리로드 완료"
                        toast_timer_ms = 2000.0
                    running, choice_cursor, show_status, walk_frame, walk_timer = _process_keydown(
                        pygame, event.key, session, choice_cursor, show_status, walk_frame, walk_timer, settings=settings
                    )
                    if session.map_id != last_seen_map_id:
                        last_seen_map_id = session.map_id
                        mname_cap = str(session.current_map().get("name", ""))
                        pygame.display.set_caption(f"RPG Creator Player - {session.title} - {mname_cap}")
                        toast_text = f"📍 {mname_cap}"
                        toast_timer_ms = 2400.0
                    if not show_status:
                        status_cursor = 0
                        status_actor_idx = 0
                    if show_status or session.pending:
                        held_dir_order.clear()
                        tap_dirs.clear()

            held_dir_order = _sync_held_dirs(
                pygame, pygame.key.get_pressed(), held_dir_order, key_table=dir_key_table
            )
            is_modal_now = bool(show_status or session.pending or session.game_over)
            move_cooldown = _sync_modal_input_reset(
                was_modal_open, is_modal_now, held_dir_order, tap_dirs, move_cooldown
            )
            was_modal_open = is_modal_now
            shift_held = bool(pygame.key.get_mods() & pygame.KMOD_SHIFT)

            if (
                running
                and slide_dx == 0
                and slide_dy == 0
                and move_cooldown == 0
                and not show_status
                and not session.pending
                and not session.game_over
            ):
                active_dirs = held_dir_order if held_dir_order else tap_dirs
                if shift_held and active_dirs:
                    session.step(f"face:{active_dirs[-1]}")
                    walk_timer = 4
                    move_cooldown = 6
                    tap_dirs.clear()
                else:
                    move_action = resolve_held_move_action(active_dirs, session.can_step_action)
                    tap_dirs.clear()
                    if move_action is not None:
                        prev_m, prev_px, prev_py, prev_dir = session.map_id, session.x, session.y, session.direction
                        session.step(move_action)
                        if (session.map_id, session.x, session.y) == (prev_m, prev_px, prev_py) and prev_dir != session.direction:
                            walk_timer = 4
                        _sync_move_visual(prev_m, prev_px, prev_py)
            elif is_sliding:
                tap_dirs.clear()

            if session.map_id != last_seen_map_id:
                last_seen_map_id = session.map_id
                mname_cap = str(session.current_map().get("name", ""))
                pygame.display.set_caption(f"RPG Creator Player - {session.title} - {mname_cap}")
                toast_text = f"📍 {mname_cap}"
                toast_timer_ms = 2400.0
            screen.fill((18, 20, 26))
            m = session.current_map()
            ts_id = int(m["tileset_id"])
            ts_fields = tilesets_meta.get(ts_id, {})
            ts_priorities = session.priorities_by_ts.get(ts_id, [])
            mw, mh = int(m["width"]), int(m["height"])
            hero_world_x = session.x * TILE_PX + slide_dx
            hero_world_y = session.y * TILE_PX + slide_dy
            sc_x, sc_y = session.camera_scroll_px[0], session.camera_scroll_px[1]
            cam_x, cam_y = compute_camera_offset_px(hero_world_x + sc_x, hero_world_y + sc_y, mw, mh, VIEW_W, VIEW_H, TILE_PX)
            if isinstance(session.pending_shake, dict) and int(session.pending_shake.get("frames", 0)) > 0:
                pwr = int(session.pending_shake.get("power", 4))
                spd = int(session.pending_shake.get("speed", 5))
                shake_off = int(round(math.sin(anim_tick * max(1, spd) * 0.45) * pwr * 2))
                shake_off_y = int(round(math.cos(anim_tick * max(1, spd) * 0.45) * pwr * 2))
                cam_x += shake_off
                cam_y += shake_off_y

            start_tx = max(0, cam_x // TILE_PX)
            end_tx = min(mw, (cam_x + VIEW_W + TILE_PX - 1) // TILE_PX)
            start_ty = max(0, cam_y // TILE_PX)
            end_ty = min(mh, (cam_y + VIEW_H + TILE_PX - 1) // TILE_PX)

            mcache = _get_or_build_map_cache(
                pygame,
                session.map_id,
                m,
                ts_id,
                ts_fields,
                ts_priorities,
                draw_tile,
                map_layer_cache,
                revision=session.map_revision(session.map_id),
            )
            cam_rect = pygame.Rect(cam_x, cam_y, VIEW_W, VIEW_H)
            _draw_panorama(pygame, screen, load_img, ts_fields, cam_x, cam_y, map_ir=m, anim_tick=anim_tick)
            screen.blit(mcache["lower_surf"], (0, 0), cam_rect)
            anim_f = anim_tick // 30
            lower_autos = mcache["lower_autos"]
            pri1_rows = mcache.get("pri1_rows", {})
            pri1_autos = mcache.get("pri1_autos", {})
            upper_autos = mcache["upper_autos"]
            for y in range(start_ty, end_ty):
                for x in range(start_tx, end_tx):
                    if (x, y) in lower_autos:
                        px, py = x * TILE_PX - cam_x, y * TILE_PX - cam_y
                        for layer_grid, tid in lower_autos[(x, y)]:
                            draw_tile(screen, ts_fields, layer_grid, x, y, tid, px, py, anim_frame=anim_f)

            ev_dir_map = {2: "down", 4: "left", 6: "right", 8: "up"}
            depth_queue: List[Tuple[int, int, str, Any]] = []
            for ty in range(start_ty, end_ty):
                if ty in pri1_rows or ty in pri1_autos:
                    depth_queue.append((ty * TILE_PX, 0, "row", ty))
            for ev, page in session.active_events_on_map():
                gfx_fields = page.get("graphic", {}).get("fields", {})
                gfx_name = str(gfx_fields.get("character_name", ""))
                if gfx_name:
                    sl = npc_slides.get((session.map_id, int(ev["id"])), {})
                    ndx, ndy = int(sl.get("dx", 0)), int(sl.get("dy", 0))
                    ex_world = int(ev["x"]) * TILE_PX + ndx
                    ey_world = int(ev["y"]) * TILE_PX + ndy
                    ex_px = ex_world - cam_x
                    ey_px = ey_world - cam_y
                    if -TILE_PX <= ex_px < VIEW_W and -TILE_PX <= ey_px < VIEW_H:
                        if ndx != 0 or ndy != 0:
                            ev_col = compute_stride_col(int(sl.get("stride", 0)), ndx, ndy)
                        else:
                            ev_col = (anim_tick // 16) % 4 if page.get("step_anime") else 0
                        ev_dir = ev_dir_map.get(int(gfx_fields.get("direction", 2)), "down")
                        ev_op = int(gfx_fields.get("opacity", 255))
                        ev_bl = int(gfx_fields.get("blend_type", 0))
                        ev_hue = int(gfx_fields.get("character_hue", 0) or 0)
                        nst = getattr(session, "npc_jump_state", {}).get((session.map_id, int(ev["id"])), {})
                        ev_jump = compute_jump_arc_px(int(nst.get("frames", 0)), int(nst.get("total", 0)))
                        ev_bush = session.tile_has_passage_flag(int(ev["x"]), int(ev["y"]), 0x40)
                        layer_ord = 3 if page.get("always_on_top") else 1
                        depth_queue.append(
                            (ey_world, layer_ord, "char", (gfx_name, ev_dir, ex_px, ey_px, (240, 200, 75), ev_col, ev_op, ev_bl, ev_jump, ev_hue, ev_bush))
                        )

            hero_col = (
                0
                if ice_sliding
                else (
                    compute_stride_col(stride_count, slide_dx, slide_dy)
                    if (slide_dx != 0 or slide_dy != 0)
                    else compute_walk_frame_col(walk_frame, walk_timer)
                )
            )
            if not session.player_transparent:
                p_jump = compute_jump_arc_px(
                    int(getattr(session, "player_jump_frames", 0)),
                    int(getattr(session, "player_jump_total", 0)),
                )
                p_bush = session.tile_has_passage_flag(session.x, session.y, 0x40)
                depth_queue.append(
                    (
                        hero_world_y,
                        2,
                        "char",
                        (
                            session.hero_graphic,
                            session.direction,
                            hero_world_x - cam_x,
                            hero_world_y - cam_y,
                            (75, 160, 245),
                            hero_col,
                            int(getattr(session, "player_opacity", 255)),
                            int(getattr(session, "player_blend_type", 0)),
                            p_jump,
                            0,
                            p_bush,
                        ),
                    )
                )
            for vtype, vpos in session.vehicle_positions.items():
                if int(vpos.get("map_id", 0)) == session.map_id:
                    is_mounted = session.vehicle == vtype
                    vy_w = hero_world_y if is_mounted else int(vpos["y"]) * TILE_PX
                    vx_px = (hero_world_x if is_mounted else int(vpos["x"]) * TILE_PX) - cam_x
                    vy_px = vy_w - cam_y
                    if -TILE_PX <= vx_px < VIEW_W and -TILE_PX <= vy_px < VIEW_H:
                        depth_queue.append((vy_w, 1, "vehicle", (vtype, vx_px, vy_px)))

            depth_queue.sort(key=lambda item: (item[0], item[1]))
            row_crop = pygame.Rect(cam_x, 0, VIEW_W, TILE_PX)
            for _wy, _ord, kind, payload in depth_queue:
                if kind == "row":
                    ty = int(payload)
                    ry_px = ty * TILE_PX - cam_y
                    if ty in pri1_rows:
                        screen.blit(pri1_rows[ty], (0, ry_px), row_crop)
                    if ty in pri1_autos:
                        for (ax, ay), alist in pri1_autos[ty].items():
                            if start_tx <= ax < end_tx:
                                for layer_grid, tid in alist:
                                    draw_tile(screen, ts_fields, layer_grid, ax, ay, tid, ax * TILE_PX - cam_x, ry_px, anim_frame=anim_f)
                elif kind == "char":
                    gname, dname, dx_px, dy_px, fcol, scol, char_op, char_bl, char_jump, char_hue, char_bush = payload
                    draw_character(
                        screen,
                        gname,
                        dname,
                        dx_px,
                        dy_px,
                        fcol,
                        step_col=scol,
                        opacity=char_op,
                        blend_type=char_bl,
                        jump_px=char_jump,
                        hue=char_hue,
                        on_bush=char_bush,
                    )
                elif kind == "vehicle":
                    vtype, vx_px, vy_px = payload
                    vcol = {"boat": (165, 115, 65), "ship": (195, 140, 75), "airship": (165, 220, 245)}.get(vtype, (180, 180, 180))
                    pygame.draw.rect(screen, vcol, (vx_px + 4, vy_px + 16, TILE_PX - 8, 12), border_radius=4)
                    pygame.draw.rect(screen, (25, 25, 32), (vx_px + 4, vy_px + 16, TILE_PX - 8, 12), 1, border_radius=4)

            screen.blit(mcache["upper_surf"], (0, 0), cam_rect)
            for y in range(start_ty, end_ty):
                for x in range(start_tx, end_tx):
                    if (x, y) in upper_autos:
                        px, py = x * TILE_PX - cam_x, y * TILE_PX - cam_y
                        for layer_grid, tid in upper_autos[(x, y)]:
                            draw_tile(screen, ts_fields, layer_grid, x, y, tid, px, py, anim_frame=anim_f)

            _draw_fog(pygame, screen, load_img, session, cam_x, cam_y, fog_cache=fog_cache)
            _draw_pending_animations(cam_x, cam_y)
            _draw_pending_balloons(cam_x, cam_y)
            _draw_screen_tone_and_flash(pygame, screen, session, overlay_cache=render_cache)
            _draw_pictures(pygame, screen, load_img, small_font, session, pic_cache=pic_cache)
            _draw_weather_and_fade(pygame, screen, small_font, session, anim_tick, load_img=load_img)
            cur_dsig = (id(session.pending), tuple(session.pending.lines), int(getattr(session.pending, "page_offset", 0))) if session.pending else None
            if cur_dsig != last_dialogue_sig:
                last_dialogue_sig = cur_dsig
                dialogue_elapsed_ms = 0.0
            elif session.pending:
                dialogue_elapsed_ms += last_dt_ms
            last_choice_rects = []
            _draw_overlays(
                pygame,
                screen,
                font,
                small_font,
                session,
                choice_cursor,
                show_status,
                status_cursor=status_cursor,
                status_actor_idx=status_actor_idx,
                active_slot=active_slot,
                toast_text=toast_text,
                status_mode=status_mode,
                hud_cache=hud_cache,
                theme=ui_theme,
                out_choice_rects=last_choice_rects,
                load_img=load_img,
                dialogue_elapsed_ms=dialogue_elapsed_ms,
                text_speed=int(settings.get("text_speed", 100)),
                hue_cache=hue_cache,
                out_status_rects=last_status_rects,
            )
            if show_quests and not session.pending and not session.game_over:
                _draw_quest_overlay(
                    pygame,
                    screen,
                    font,
                    small_font,
                    session,
                    theme=ui_theme,
                    scroll_offset=quest_scroll,
                    out_close_rects=last_quest_close_rects,
                )
            elif show_minimap and not show_status and not session.pending and not session.game_over:
                _draw_minimap(pygame, screen, session, theme=ui_theme, minimap_cache=minimap_cache)

            _present_frame()
            if session.game_over:
                if not played_gameover_me and isinstance(getattr(session, "gameover_me", None), dict):
                    session.played_me_queue.append(dict(session.gameover_me))
                    played_gameover_me = True
            else:
                played_gameover_me = False
            playing_bgm, playing_bgs, bgs_channel = _sync_audio_runtime(
                pygame,
                audio_root,
                session,
                playing_bgm,
                mixer_ok,
                playing_bgs=playing_bgs,
                bgs_channel=bgs_channel,
                sound_cache=sound_cache,
                settings=settings,
                rgssad_reader=rgssad_reader,
            )
            fx_step_ms = 1000.0 / TARGET_FPS
            fx_accum_ms += last_dt_ms
            fx_steps = max(0, int(fx_accum_ms // fx_step_ms))
            if fx_steps > 0:
                fx_accum_ms -= fx_steps * fx_step_ms
                session.advance_screen_fx_frames(fx_steps)
            session.advance_timer_ms(last_dt_ms)
            if not session.game_over:
                session.play_time_ms = float(getattr(session, "play_time_ms", 0.0)) + float(last_dt_ms)
            if on_frame is not None:
                on_frame(anim_tick, screen, session)
            anim_tick += 1
            if max_frames is not None and anim_tick >= max_frames:
                running = False
            if slide_dx != 0 or slide_dy != 0:
                ctrl_held = bool(pygame.key.get_mods() & pygame.KMOD_CTRL) or bool(settings.get("auto_dash", False))
                step_px, move_px_accum = compute_dt_slide_step(
                    last_dt_ms,
                    move_px_accum,
                    speed_px_per_sec=resolve_player_speed_px(ctrl_held, is_ice_slide=ice_sliding),
                    is_diagonal=(slide_dx != 0 and slide_dy != 0),
                )
                slide_dx, slide_dy = advance_slide_offset(slide_dx, slide_dy, step_px)
                if slide_dx == 0 and slide_dy == 0:
                    ice_sliding = False
            else:
                move_px_accum = 0.0
                ice_sliding = False
            advance_npc_slide_states(npc_slides, last_dt_ms)
            if not show_status and not show_quests and not session.pending and not session.game_over:
                cur_mid = session.map_id
                active_list = session.active_events_on_map()
                active_keys = {(cur_mid, int(ev["id"])) for ev, _p in active_list}
                for stale_k in [k for k in npc_step_timers if k not in active_keys]:
                    npc_step_timers.pop(stale_k, None)
                before_ev_pos = {
                    int(ev["id"]): (int(ev["x"]), int(ev["y"]))
                    for ev, _p in active_list
                }
                stepped_any = False
                for ev, page in active_list:
                    eid = int(ev["id"])
                    if (cur_mid, eid) not in session._active_move_routes and int(page.get("move_type", 0)) == 0:
                        continue
                    tkey = (cur_mid, eid)
                    interval_ms = resolve_npc_interval_ms(page)
                    acc_ms = npc_step_timers.get(tkey, 0.0) + last_dt_ms
                    while acc_ms >= interval_ms:
                        acc_ms -= interval_ms
                        session.advance_npc_routes(target_event_id=eid)
                        stepped_any = True
                    npc_step_timers[tkey] = acc_ms
                if stepped_any and session.map_id == cur_mid:
                    sync_npc_slide_states(
                        npc_slides,
                        cur_mid,
                        before_ev_pos,
                        session.active_events_on_map(),
                    )
            if toast_timer_ms > 0.0:
                toast_timer_ms = max(0.0, toast_timer_ms - last_dt_ms)
                if toast_timer_ms == 0.0:
                    toast_text = ""
            if load_confirm_timer_ms > 0.0:
                load_confirm_timer_ms = max(0.0, load_confirm_timer_ms - last_dt_ms)
                if load_confirm_timer_ms == 0.0:
                    pending_load_confirm = False
            if move_cooldown > 0:
                move_cooldown -= 1
            if walk_timer > 0:
                walk_timer -= 1
                if walk_timer == 0 and slide_dx == 0 and slide_dy == 0:
                    walk_frame = 0
            last_dt_ms = _clamp_frame_dt(float(clock.tick(TARGET_FPS)))
    except Exception as exc:
        write_crash_report(
            exc,
            ir_dir / "crash_reports",
            {"map_id": session.map_id, "x": session.x, "y": session.y, "title": session.title},
        )
        raise
    finally:
        if "outline_ctrl" in locals() and outline_ctrl is not None:
            try:
                outline_ctrl.detach()
            except Exception:
                pass
        pygame.quit()
    return 0
