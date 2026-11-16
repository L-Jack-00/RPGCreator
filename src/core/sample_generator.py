# -*- coding: utf-8 -*-
"""
Cleanroom RMXP Specification Sample Project Generator.
Produces a full RMXP project (Data/*.rxdata including Classes, Skills, Weapons,
Armors, Troops, States, Animations, CommonEvents, Game.rxproj, 32x32 PNGs)
from scratch and converts it into an AI-native IR workspace.
"""
from __future__ import annotations

import zlib
from pathlib import Path
from typing import Any, Dict, List, Optional

from PIL import Image, ImageDraw

from .ir_converter import pack_ir_to_rmxp, unpack_rmxp_to_ir
from .rmxp_marshal import RubyObject, marshal_dumps
from .utils import load_json, remove_map_info_entry, save_json, set_map_info_name
from .rmxp_types import (
    RgssTable,
    make_audio_file,
    make_event,
    make_event_command,
    make_event_page,
)


def _make_system() -> RubyObject:
    words = RubyObject("RPG::System::Words", {
        "@gold": "G",
        "@hp": "HP",
        "@sp": "SP",
        "@str": "힘",
        "@dex": "민첩",
        "@agi": "속도",
        "@int": "지능",
        "@atk": "공격력",
        "@pdef": "물리방어",
        "@mdef": "마법방어",
        "@weapon": "무기",
        "@armor1": "방패",
        "@armor2": "투구",
        "@armor3": "갑옷",
        "@armor4": "장신구",
        "@attack": "공격",
        "@skill": "스킬",
        "@guard": "방어",
        "@item": "아이템",
        "@equip": "장비",
    })
    return RubyObject("RPG::System", {
        "@magic_number": 1001,
        "@party_members": [1],
        "@elements": [None, "물리", "불", "물"],
        "@switches": [None, "퀘스트_수락", "보스_처치", "상자_개봉", "보물방_열쇠"],
        "@variables": [None, "처치_수", "골드_보상"],
        "@windowskin_name": "",
        "@title_name": "",
        "@gameover_name": "",
        "@battle_transition": "",
        "@title_bgm": make_audio_file(),
        "@battle_bgm": make_audio_file(),
        "@battle_end_me": make_audio_file(),
        "@gameover_me": make_audio_file(),
        "@cursor_se": make_audio_file(),
        "@decision_se": make_audio_file(),
        "@cancel_se": make_audio_file(),
        "@buzzer_se": make_audio_file(),
        "@equip_se": make_audio_file(),
        "@shop_se": make_audio_file(),
        "@save_se": make_audio_file(),
        "@load_se": make_audio_file(),
        "@battle_start_se": make_audio_file(),
        "@escape_se": make_audio_file(),
        "@actor_collapse_se": make_audio_file(),
        "@enemy_collapse_se": make_audio_file(),
        "@words": words,
        "@test_battlers": [],
        "@test_troop_id": 1,
        "@start_map_id": 1,
        "@start_x": 4,
        "@start_y": 10,
        "@battleback_name": "",
        "@battler_name": "",
        "@battler_hue": 0,
        "@edit_map_id": 1,
    })


def _make_tilesets() -> List[Optional[RubyObject]]:
    def build_tileset(ts_id: int, name: str, image_name: str) -> RubyObject:
        total_slots = 384 + 16
        passages = RgssTable.create(total_slots, dims=1, fill=0)
        priorities = RgssTable.create(total_slots, dims=1, fill=0)
        terrain_tags = RgssTable.create(total_slots, dims=1, fill=0)

        # Autotile slot 0 (IDs 48..95): water -> impassable (0x0F), terrain 1
        for tid in range(48, 96):
            passages.set(tid, value=0x0F)
            terrain_tags.set(tid, value=1)

        # Regular tiles:
        # 384: Grass / Stone floor (passable)
        # 385: Dirt path / Paved floor (passable)
        # 386: Tree / Cave wall (impassable 0x0F)
        # 387: Wooden bridge (passable)
        # 388: Cave / Exit portal (passable)
        # 389: Decorative flower / crystal (passable)
        passages.set(386, value=0x0F)
        priorities.set(386, value=1)

        return RubyObject("RPG::Tileset", {
            "@id": ts_id,
            "@name": name,
            "@tileset_name": image_name,
            "@autotile_names": ["water", "", "", "", "", "", ""],
            "@panorama_name": "",
            "@panorama_hue": 0,
            "@fog_name": "",
            "@fog_hue": 0,
            "@fog_opacity": 64,
            "@fog_blend_type": 0,
            "@fog_zoom": 200,
            "@fog_sx": 0,
            "@fog_sy": 0,
            "@battleback_name": "",
            "@passages": passages.to_user_def(),
            "@priorities": priorities.to_user_def(),
            "@terrain_tags": terrain_tags.to_user_def(),
        })

    return [
        None,
        build_tileset(1, "마을_외부", "sample_town"),
        build_tileset(2, "던전_내부", "sample_dungeon"),
    ]


def _make_actors() -> List[Optional[RubyObject]]:
    params = RgssTable.create(6, 100, dims=2, fill=0)
    for lvl in range(1, 100):
        params.set(0, lvl, value=120 + lvl * 25)
        params.set(1, lvl, value=40 + lvl * 8)
        params.set(2, lvl, value=12 + lvl * 2)
        params.set(3, lvl, value=10 + lvl * 2)
        params.set(4, lvl, value=11 + lvl * 2)
        params.set(5, lvl, value=9 + lvl * 2)

    actor = RubyObject("RPG::Actor", {
        "@id": 1,
        "@name": "아린",
        "@class_id": 1,
        "@initial_level": 1,
        "@final_level": 99,
        "@exp_basis": 30,
        "@exp_inflation": 30,
        "@character_name": "hero",
        "@character_hue": 0,
        "@battler_name": "",
        "@battler_hue": 0,
        "@parameters": params.to_user_def(),
        "@weapon_id": 1,
        "@armor1_id": 0,
        "@armor2_id": 0,
        "@armor3_id": 1,
        "@armor4_id": 0,
        "@weapon_fix": False,
        "@armor1_fix": False,
        "@armor2_fix": False,
        "@armor3_fix": False,
        "@armor4_fix": False,
    })
    return [None, actor]


def _make_classes() -> List[Optional[RubyObject]]:
    elem_ranks = RgssTable.create(4, dims=1, fill=3)
    state_ranks = RgssTable.create(3, dims=1, fill=3)
    learning = RubyObject("RPG::Class::Learning", {"@level": 1, "@skill_id": 1})
    cls = RubyObject("RPG::Class", {
        "@id": 1,
        "@name": "모험가",
        "@position": 0,
        "@weapon_set": [1],
        "@armor_set": [1],
        "@element_ranks": elem_ranks.to_user_def(),
        "@state_ranks": state_ranks.to_user_def(),
        "@learnings": [learning],
    })
    return [None, cls]


def _make_skills() -> List[Optional[RubyObject]]:
    slash = RubyObject("RPG::Skill", {
        "@id": 1,
        "@name": "강타",
        "@icon_name": "",
        "@description": "적에게 강한 물리 피해를 입힌다.",
        "@scope": 1,
        "@occasion": 1,
        "@animation1_id": 0,
        "@animation2_id": 1,
        "@menu_se": make_audio_file(),
        "@common_event_id": 0,
        "@sp_cost": 5,
        "@power": 35,
        "@atk_f": 100,
        "@eva_f": 0,
        "@str_f": 20,
        "@dex_f": 0,
        "@agi_f": 0,
        "@int_f": 0,
        "@hit": 95,
        "@pdef_f": 100,
        "@mdef_f": 0,
        "@variance": 15,
        "@element_set": [1],
        "@plus_state_set": [],
        "@minus_state_set": [],
    })
    return [None, slash]


def _make_items() -> List[Optional[RubyObject]]:
    potion = RubyObject("RPG::Item", {
        "@id": 1,
        "@name": "회복약",
        "@icon_name": "",
        "@description": "HP를 100 회복하는 기본 물약.",
        "@scope": 3,
        "@occasion": 0,
        "@animation1_id": 0,
        "@animation2_id": 0,
        "@menu_se": make_audio_file(),
        "@common_event_id": 0,
        "@price": 50,
        "@consumable": True,
        "@parameter_type": 0,
        "@parameter_points": 0,
        "@recover_hp_rate": 0,
        "@recover_hp": 100,
        "@recover_sp_rate": 0,
        "@recover_sp": 0,
        "@hit": 100,
        "@pdef_f": 0,
        "@mdef_f": 0,
        "@variance": 10,
        "@element_set": [],
        "@minus_state_set": [],
        "@plus_state_set": [],
    })
    key = RubyObject("RPG::Item", {
        "@id": 2,
        "@name": "폐광 열쇠",
        "@icon_name": "",
        "@description": "폐광 안쪽 문을 여는 낡은 열쇠.",
        "@scope": 0,
        "@occasion": 3,
        "@animation1_id": 0,
        "@animation2_id": 0,
        "@menu_se": make_audio_file(),
        "@common_event_id": 0,
        "@price": 0,
        "@consumable": False,
        "@parameter_type": 0,
        "@parameter_points": 0,
        "@recover_hp_rate": 0,
        "@recover_hp": 0,
        "@recover_sp_rate": 0,
        "@recover_sp": 0,
        "@hit": 100,
        "@pdef_f": 0,
        "@mdef_f": 0,
        "@variance": 0,
        "@element_set": [],
        "@minus_state_set": [],
        "@plus_state_set": [],
    })
    return [None, potion, key]


def _make_weapons() -> List[Optional[RubyObject]]:
    sword = RubyObject("RPG::Weapon", {
        "@id": 1,
        "@name": "철검",
        "@icon_name": "",
        "@description": "초보 모험가가 사용하는 튼튼한 검.",
        "@animation1_id": 0,
        "@animation2_id": 1,
        "@price": 120,
        "@atk": 18,
        "@pdef": 0,
        "@mdef": 0,
        "@str_plus": 2,
        "@dex_plus": 0,
        "@agi_plus": 0,
        "@int_plus": 0,
        "@element_set": [1],
        "@plus_state_set": [],
        "@minus_state_set": [],
    })
    return [None, sword]


def _make_armors() -> List[Optional[RubyObject]]:
    mail = RubyObject("RPG::Armor", {
        "@id": 1,
        "@name": "가죽 갑옷",
        "@icon_name": "",
        "@description": "가볍고 튼튼한 여행자용 갑옷.",
        "@kind": 2,
        "@auto_state_id": 0,
        "@price": 100,
        "@pdef": 12,
        "@mdef": 4,
        "@eva": 2,
        "@str_plus": 0,
        "@dex_plus": 0,
        "@agi_plus": 0,
        "@int_plus": 0,
        "@guard_element_set": [],
        "@guard_state_set": [],
    })
    return [None, mail]


def _make_states() -> List[Optional[RubyObject]]:
    poison = RubyObject("RPG::State", {
        "@id": 1,
        "@name": "전투불능",
        "@animation_id": 0,
        "@restriction": 4,
        "@nonresistance": True,
        "@zero_hp": True,
        "@cant_get_exp": True,
        "@cant_evade": True,
        "@slip_damage": False,
        "@rating": 10,
        "@hit_rate": 100,
        "@maxhp_rate": 100,
        "@maxsp_rate": 100,
        "@str_rate": 100,
        "@dex_rate": 100,
        "@agi_rate": 100,
        "@int_rate": 100,
        "@atk_rate": 100,
        "@pdef_rate": 100,
        "@mdef_rate": 100,
        "@eva": 0,
        "@battle_only": True,
        "@hold_turn": 0,
        "@auto_release_prob": 0,
        "@shock_release_prob": 0,
        "@guard_element_set": [],
        "@plus_state_set": [],
        "@minus_state_set": [],
    })
    return [None, poison]


def _make_animations() -> List[Optional[RubyObject]]:
    frame = RubyObject("RPG::Animation::Frame", {
        "@cell_max": 0,
        "@cell_data": RgssTable.create(1, 8, dims=2, fill=0).to_user_def(),
    })
    hit = RubyObject("RPG::Animation", {
        "@id": 1,
        "@name": "기본 타격",
        "@animation_name": "",
        "@animation_hue": 0,
        "@position": 1,
        "@frame_max": 1,
        "@frames": [frame],
        "@timings": [],
    })
    return [None, hit]


def _make_enemies() -> List[Optional[RubyObject]]:
    def enemy(eid: int, name: str, hp: int, atk: int, exp: int, gold: int, str_stat: int = 10) -> RubyObject:
        basic_attack = RubyObject("RPG::Enemy::Action", {
            "@kind": 0,
            "@basic": 0,
            "@skill_id": 1,
            "@condition_turn_a": 0,
            "@condition_turn_b": 1,
            "@condition_hp": 100,
            "@condition_level": 1,
            "@condition_switch_id": 0,
            "@rating": 5,
        })
        return RubyObject("RPG::Enemy", {
            "@id": eid,
            "@name": name,
            "@battler_name": "",
            "@battler_hue": 0,
            "@maxhp": hp,
            "@maxsp": 20,
            "@str": str_stat,
            "@dex": 10,
            "@agi": 10,
            "@int": 8,
            "@atk": atk,
            "@pdef": 10,
            "@mdef": 8,
            "@eva": 5,
            "@animation1_id": 0,
            "@animation2_id": 1,
            "@actions": [basic_attack],
            "@exp": exp,
            "@gold": gold,
            "@item_id": 1,
            "@weapon_id": 0,
            "@armor_id": 0,
            "@treasure_prob": 25,
        })

    return [
        None,
        # Tuned for the default RPG Maker XP damage formula: ATK - PDEF/2, scaled by (20 + STR) / 20.
        enemy(1, "고블린", 90, 18, 15, 20, str_stat=10),
        enemy(2, "고블린 대장", 180, 22, 80, 120, str_stat=10),
    ]


def _make_troops() -> List[Optional[RubyObject]]:
    def troop(tid: int, name: str, enemy_id: int) -> RubyObject:
        member = RubyObject("RPG::Troop::Member", {
            "@enemy_id": enemy_id,
            "@x": 320,
            "@y": 240,
            "@hidden": False,
            "@immortal": False,
        })
        return RubyObject("RPG::Troop", {
            "@id": tid,
            "@name": name,
            "@members": [member],
            "@pages": [],
        })

    return [
        None,
        troop(1, "고블린 무리", 1),
        troop(2, "고블린 대장전", 2),
    ]


def _make_common_events() -> List[Optional[RubyObject]]:
    ce = RubyObject("RPG::CommonEvent", {
        "@id": 1,
        "@name": "간단 회복",
        "@trigger": 0,
        "@switch_id": 1,
        "@list": [
            make_event_command(101, 0, ["휴식을 취해 기운을 차렸다."]),
            make_event_command(0, 0, []),
        ],
    })
    return [None, ce]


def _make_town_map() -> RubyObject:
    w, h = 20, 15
    table = RgssTable.create(w, h, 3, dims=3, fill=0)

    for y in range(h):
        for x in range(w):
            table.set(x, y, 0, 384)

    for x in range(w):
        table.set(x, 0, 1, 386)
        table.set(x, h - 1, 1, 386)
    for y in range(h):
        table.set(0, y, 1, 386)
        table.set(w - 1, y, 1, 386)

    for x in range(1, w - 1):
        if x in (9, 10):
            table.set(x, 6, 0, 387)
        else:
            table.set(x, 6, 0, 48)

    for x in range(4, 10):
        table.set(x, 10, 0, 385)
    for y in range(4, 11):
        table.set(9, y, 0, 385)
    for x in range(9, 18):
        table.set(x, 4, 0, 385)

    table.set(17, 4, 1, 388)

    elder_page1 = make_event_page(
        graphic_name="elder",
        direction=2,
        trigger=0,
        commands=[
            make_event_command(101, 0, ["촌장: 어서 오게, 모험가여!"]),
            make_event_command(401, 0, ["북동쪽 폐광에 고블린 무리가 나타났다네."]),
            make_event_command(102, 0, [["도와드리겠습니다", "조금 더 준비하겠습니다"], 1]),
            make_event_command(402, 0, [0, "도와드리겠습니다"]),
            make_event_command(126, 0, [1, 0, 0, 2]),
            make_event_command(121, 0, [1, 1, 0]),
            make_event_command(108, 0, ["@QUEST:goblin_mine|active|폐광의 고블린 토벌|북동쪽 폐광으로 가 고블린 대장을 처치하자"]),
            make_event_command(101, 0, ["촌장: 고맙네! 회복약 2개를 받아가게."]),
            make_event_command(402, 0, [1, "조금 더 준비하겠습니다"]),
            make_event_command(101, 0, ["촌장: 준비가 되면 언제든 다시 말을 걸게."]),
            make_event_command(404, 0, []),
        ],
    )
    elder_page2 = make_event_page(
        graphic_name="elder",
        direction=2,
        trigger=0,
        switch1_id=1,
        commands=[
            make_event_command(101, 0, ["촌장: 다리를 건너 북동쪽 폐광으로 가면 된다네."]),
        ],
    )
    elder_ev = make_event(1, "촌장", 6, 8, [elder_page1, elder_page2])

    warp_page = make_event_page(
        graphic_name="",
        trigger=1,
        through=True,
        commands=[
            make_event_command(201, 0, [0, 2, 9, 13, 8, 0]),
        ],
    )
    warp_ev = make_event(2, "폐광_입구", 17, 4, [warp_page])

    return RubyObject("RPG::Map", {
        "@tileset_id": 1,
        "@width": w,
        "@height": h,
        "@autoplay_bgm": False,
        "@bgm": make_audio_file(),
        "@autoplay_bgs": False,
        "@bgs": make_audio_file(),
        "@encounter_list": [],
        "@encounter_step": 30,
        "@data": table.to_user_def(),
        "@events": {1: elder_ev, 2: warp_ev},
    })


def _make_dungeon_map() -> RubyObject:
    w, h = 20, 15
    table = RgssTable.create(w, h, 3, dims=3, fill=0)

    for y in range(h):
        for x in range(w):
            table.set(x, y, 0, 384)

    for x in range(w):
        table.set(x, 0, 1, 386)
        table.set(x, h - 1, 1, 386)
    for y in range(h):
        table.set(0, y, 1, 386)
        table.set(w - 1, y, 1, 386)

    # Inner pillar obstacle leaving corridors
    for x in range(8, 14):
        table.set(x, 8, 1, 386)

    # Locked vault walls at top-left (x=1..6, y=1..5) with gate opening at (6, 3)
    for y in range(1, 6):
        if y != 3:
            table.set(6, y, 1, 386)
    for x in range(1, 7):
        table.set(x, 5, 1, 386)

    # Exit marker at (9, 13)
    table.set(9, 13, 1, 388)

    exit_ev = make_event(1, "마을_출구", 9, 13, [
        make_event_page(
            graphic_name="",
            trigger=1,
            through=True,
            commands=[
                make_event_command(201, 0, [0, 1, 16, 4, 4, 0]),
            ],
        )
    ])

    chest_closed = make_event_page(
        graphic_name="chest",
        trigger=0,
        commands=[
            make_event_command(125, 0, [0, 0, 100]),
            make_event_command(123, 0, ["A", 0]),
            make_event_command(101, 0, ["보물상자에서 100 G를 발견했다!"]),
        ],
    )
    chest_opened = make_event_page(
        graphic_name="chest",
        trigger=0,
        self_switch_ch="A",
        commands=[
            make_event_command(101, 0, ["상자는 비어 있다."]),
        ],
    )
    chest_ev = make_event(2, "보물상자", 3, 3, [chest_closed, chest_opened])

    boss_active = make_event_page(
        graphic_name="goblin",
        trigger=0,
        commands=[
            make_event_command(101, 0, ["고블린 대장: 크르륵! 인간 녀석이 여기까지 오다니!"]),
            make_event_command(301, 0, [2, False, False]),
            make_event_command(121, 0, [2, 2, 0]),
            make_event_command(108, 0, ["@QUEST:goblin_mine|completed|폐광의 고블린 토벌|고블린 대장을 물리치고 폐광에 평화를 되찾았다!"]),
            make_event_command(101, 0, ["고블린 대장을 물리쳤다! 폐광에 평화가 돌아왔다."]),
        ],
    )
    boss_defeated = make_event_page(
        graphic_name="",
        trigger=0,
        switch1_id=2,
        through=True,
        commands=[],
    )
    boss_ev = make_event(3, "고블린_대장", 11, 4, [boss_active, boss_defeated])

    gate_locked = make_event_page(
        graphic_name="chest",
        trigger=0,
        through=False,
        commands=[
            make_event_command(101, 0, ["보물방 철문이 굳게 잠겨 있다. 레버를 찾아보자."]),
        ],
    )
    gate_open = make_event_page(
        graphic_name="",
        trigger=0,
        switch1_id=4,
        through=True,
        commands=[],
    )
    gate_ev = make_event(4, "보물방_철문", 6, 3, [gate_locked, gate_open])

    lever_page = make_event_page(
        graphic_name="elder",
        trigger=0,
        commands=[
            make_event_command(121, 0, [4, 4, 0]),
            make_event_command(101, 0, ["철컥! 북서쪽 보물방 철문이 열렸다."]),
        ],
    )
    lever_ev = make_event(5, "금고_레버", 15, 10, [lever_page])

    return RubyObject("RPG::Map", {
        "@tileset_id": 2,
        "@width": w,
        "@height": h,
        "@autoplay_bgm": False,
        "@bgm": make_audio_file(),
        "@autoplay_bgs": False,
        "@bgs": make_audio_file(),
        "@encounter_list": [1],
        "@encounter_step": 25,
        "@data": table.to_user_def(),
        "@events": {1: exit_ev, 2: chest_ev, 3: boss_ev, 4: gate_ev, 5: lever_ev},
    })


def _make_map_infos() -> Dict[int, RubyObject]:
    return {
        1: RubyObject("RPG::MapInfo", {
            "@name": "시작의 마을",
            "@parent_id": 0,
            "@order": 1,
            "@expanded": False,
            "@scroll_x": 0,
            "@scroll_y": 0,
        }),
        2: RubyObject("RPG::MapInfo", {
            "@name": "고블린 폐광",
            "@parent_id": 1,
            "@order": 2,
            "@expanded": False,
            "@scroll_x": 0,
            "@scroll_y": 0,
        }),
    }


def _generate_cleanroom_png_assets(rmxp_dir: Path) -> None:
    """Generates self-contained 32x32 placeholder tilesets and character sprites."""
    tilesets_dir = rmxp_dir / "Graphics" / "Tilesets"
    autotiles_dir = rmxp_dir / "Graphics" / "Autotiles"
    chars_dir = rmxp_dir / "Graphics" / "Characters"
    for d in (tilesets_dir, autotiles_dir, chars_dir):
        d.mkdir(parents=True, exist_ok=True)

    def make_tileset_sheet(path: Path, palette: List[tuple]) -> None:
        img = Image.new("RGBA", (256, 64), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        for idx, color in enumerate(palette):
            col = idx % 8
            row = idx // 8
            x0, y0 = col * 32, row * 32
            draw.rectangle([x0, y0, x0 + 31, y0 + 31], fill=color)
            draw.rectangle([x0 + 2, y0 + 2, x0 + 29, y0 + 29], outline=(255, 255, 255, 40))
        img.save(path)

    make_tileset_sheet(tilesets_dir / "sample_town.png", [
        (74, 148, 74, 255),
        (166, 134, 92, 255),
        (38, 92, 46, 255),
        (135, 92, 54, 255),
        (48, 42, 60, 255),
        (220, 195, 70, 255),
    ])
    make_tileset_sheet(tilesets_dir / "sample_dungeon.png", [
        (82, 82, 94, 255),
        (106, 102, 114, 255),
        (36, 34, 42, 255),
        (120, 90, 60, 255),
        (60, 140, 180, 255),
        (95, 190, 220, 255),
    ])

    # Water autotile (2 animation frames: 192x128) with shoreline borders & inner corners
    water = Image.new("RGBA", (192, 128), (48, 122, 196, 255))
    wdraw = ImageDraw.Draw(water)
    shore = (210, 195, 145, 255)
    for frame in range(2):
        fx = frame * 96
        shift = frame * 6
        for y in range(0, 128, 16):
            wdraw.line([(fx, y + shift % 16), (fx + 95, y + shift % 16)], fill=(86, 160, 224, 255), width=1)
        wdraw.rectangle([fx, 32, fx + 95, 127], outline=shore, width=3)
        wdraw.rectangle([fx, 0, fx + 31, 31], outline=shore, width=3)
        for cx, cy in ((fx + 64, 0), (fx + 92, 0), (fx + 64, 28), (fx + 92, 28)):
            wdraw.rectangle([cx, cy, cx + 3, cy + 3], fill=shore)
    water.save(autotiles_dir / "water.png")

    char_colors = {
        "hero.png": (70, 130, 230, 255),
        "elder.png": (210, 190, 140, 255),
        "chest.png": (195, 140, 50, 255),
        "goblin.png": (90, 180, 80, 255),
    }
    for fname, body_col in char_colors.items():
        sheet = Image.new("RGBA", (128, 128), (0, 0, 0, 0))
        cdraw = ImageDraw.Draw(sheet)
        for row in range(4):  # 0=down, 1=left, 2=right, 3=up
            for col in range(4):
                cx, cy = col * 32, row * 32
                if fname == "chest.png":
                    cdraw.rectangle([cx + 5, cy + 8, cx + 26, cy + 27], fill=body_col, outline=(35, 25, 15, 255))
                    cdraw.rectangle([cx + 5, cy + 14, cx + 26, cy + 16], fill=(90, 60, 25, 255))
                    cdraw.rectangle([cx + 14, cy + 13, cx + 17, cy + 18], fill=(245, 225, 110, 255))
                    continue
                bob = -1 if col in (1, 3) else 0
                left_leg_y = 26 if col == 1 else 28
                right_leg_y = 26 if col == 3 else 28
                cdraw.rectangle([cx + 10, cy + 21, cx + 14, cy + left_leg_y], fill=(45, 50, 70, 255))
                cdraw.rectangle([cx + 17, cy + 21, cx + 21, cy + right_leg_y], fill=(45, 50, 70, 255))
                cdraw.ellipse([cx + 7, cy + 5 + bob, cx + 24, cy + 23 + bob], fill=body_col, outline=(25, 25, 30, 255))
                eye_col = (248, 250, 255, 255)
                if row == 0:
                    cdraw.rectangle([cx + 11, cy + 11 + bob, cx + 13, cy + 14 + bob], fill=eye_col)
                    cdraw.rectangle([cx + 18, cy + 11 + bob, cx + 20, cy + 14 + bob], fill=eye_col)
                elif row == 1:
                    cdraw.rectangle([cx + 9, cy + 11 + bob, cx + 12, cy + 14 + bob], fill=eye_col)
                elif row == 2:
                    cdraw.rectangle([cx + 19, cy + 11 + bob, cx + 22, cy + 14 + bob], fill=eye_col)
        sheet.save(chars_dir / fname)


def generate_sample_project(output_dir: Path) -> Dict[str, Any]:
    """
    Creates a complete standard RMXP sample project at <output_dir>/rmxp
    and unpacks it into an AI-native IR workspace at <output_dir>/ir.
    """
    output_dir = Path(output_dir)
    rmxp_dir = output_dir / "rmxp"
    ir_dir = output_dir / "ir"
    data_dir = rmxp_dir / "Data"
    data_dir.mkdir(parents=True, exist_ok=True)

    (rmxp_dir / "Game.rxproj").write_text("RPGXP 1.02\n", encoding="utf-8")

    (data_dir / "System.rxdata").write_bytes(marshal_dumps(_make_system()))
    (data_dir / "MapInfos.rxdata").write_bytes(marshal_dumps(_make_map_infos()))
    (data_dir / "Tilesets.rxdata").write_bytes(marshal_dumps(_make_tilesets()))
    (data_dir / "Actors.rxdata").write_bytes(marshal_dumps(_make_actors()))
    (data_dir / "Classes.rxdata").write_bytes(marshal_dumps(_make_classes()))
    (data_dir / "Skills.rxdata").write_bytes(marshal_dumps(_make_skills()))
    (data_dir / "Items.rxdata").write_bytes(marshal_dumps(_make_items()))
    (data_dir / "Weapons.rxdata").write_bytes(marshal_dumps(_make_weapons()))
    (data_dir / "Armors.rxdata").write_bytes(marshal_dumps(_make_armors()))
    (data_dir / "Enemies.rxdata").write_bytes(marshal_dumps(_make_enemies()))
    (data_dir / "Troops.rxdata").write_bytes(marshal_dumps(_make_troops()))
    (data_dir / "States.rxdata").write_bytes(marshal_dumps(_make_states()))
    (data_dir / "Animations.rxdata").write_bytes(marshal_dumps(_make_animations()))
    (data_dir / "CommonEvents.rxdata").write_bytes(marshal_dumps(_make_common_events()))
    (data_dir / "Map001.rxdata").write_bytes(marshal_dumps(_make_town_map()))
    (data_dir / "Map002.rxdata").write_bytes(marshal_dumps(_make_dungeon_map()))

    starter_ruby = (
        "# Cleanroom RGSS entry script\n"
        "begin\n"
        "  Graphics.freeze\n"
        "  $scene = Scene_Title.new\n"
        "  $scene.main while $scene != nil\n"
        "  Graphics.transition(20)\n"
        "rescue Errno::ENOENT\n"
        "  filename = $!.message.sub('No such file or directory - ', '')\n"
        "  print(\"Unable to find file #{filename}.\")\n"
        "end\n"
    ).encode("utf-8")
    scripts = [[1, "Main", zlib.compress(starter_ruby)]]
    (data_dir / "Scripts.rxdata").write_bytes(marshal_dumps(scripts))

    _generate_cleanroom_png_assets(rmxp_dir)
    unpack_info = unpack_rmxp_to_ir(rmxp_dir, ir_dir)

    return {
        "output_dir": str(output_dir),
        "rmxp_dir": str(rmxp_dir),
        "ir_dir": str(ir_dir),
        "maps": unpack_info["maps"],
        "scripts": unpack_info["scripts"],
    }


def generate_blank_project(output_dir: Path, title: str = "새 프로젝트") -> Dict[str, Any]:
    """Creates a clean empty RMXP/IR starter workspace with one blank 20x15 map and zero events."""
    output_dir = Path(output_dir)
    base = generate_sample_project(output_dir)
    ir_dir = Path(base["ir_dir"])
    rmxp_dir = Path(base["rmxp_dir"])

    map2_json = ir_dir / "maps" / "map_002.json"
    if map2_json.exists():
        map2_json.unlink()
    map2_rx = rmxp_dir / "Data" / "Map002.rxdata"
    if map2_rx.exists():
        map2_rx.unlink()

    map1_path = ir_dir / "maps" / "map_001.json"
    map1 = load_json(map1_path)
    w, h = 20, 15
    map1["name"] = "맵 1"
    map1["width"] = w
    map1["height"] = h
    map1["layers"] = {
        "layer_0": [[384 for _ in range(w)] for _ in range(h)],
        "layer_1": [[0 for _ in range(w)] for _ in range(h)],
        "layer_2": [[0 for _ in range(w)] for _ in range(h)],
    }
    map1["events"] = []
    map1["encounter_list"] = []
    save_json(map1_path, map1)

    proj_path = ir_dir / "ir_project.json"
    proj = load_json(proj_path)
    sf = proj.setdefault("system", {}).setdefault("fields", {})
    sf["game_title"] = str(title)
    sf["start_map_id"] = 1
    sf["start_x"] = 10
    sf["start_y"] = 7
    sf["switches"] = [None, ""]
    sf["variables"] = [None, ""]
    remove_map_info_entry(proj, 2)
    set_map_info_name(proj, 1, "맵 1")
    save_json(proj_path, proj)

    pack_ir_to_rmxp(ir_dir, rmxp_dir)
    return {
        "output_dir": str(output_dir),
        "rmxp_dir": str(rmxp_dir),
        "ir_dir": str(ir_dir),
        "maps": 1,
        "scripts": int(base.get("scripts", 1)),
    }
