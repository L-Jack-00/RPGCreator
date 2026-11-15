# -*- coding: utf-8 -*-
"""
Cleanroom RMXP Data Types & RGSS Binary Primitive Codecs.
Models RGSS Table, Color, Tone, and standard RPG::* structures without
external Ruby runtime dependencies.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple
from .rmxp_marshal import RubyObject, RubyUserDef


@dataclass
class RgssTable:
    """1D, 2D, or 3D signed 16-bit integer grid serialized as RGSS Table."""
    dims: int
    xsize: int
    ysize: int
    zsize: int
    data: List[int]

    @classmethod
    def create(cls, xsize: int, ysize: int = 1, zsize: int = 1, dims: Optional[int] = None, fill: int = 0) -> "RgssTable":
        if dims is None:
            if zsize > 1:
                dims = 3
            elif ysize > 1:
                dims = 2
            else:
                dims = 1
        total = max(0, xsize * ysize * zsize)
        return cls(dims=dims, xsize=xsize, ysize=ysize, zsize=zsize, data=[fill] * total)

    def get(self, x: int, y: int = 0, z: int = 0) -> int:
        idx = x + (y * self.xsize) + (z * self.xsize * self.ysize)
        return self.data[idx]

    def set(self, x: int, y: int = 0, z: int = 0, value: int = 0) -> None:
        idx = x + (y * self.xsize) + (z * self.xsize * self.ysize)
        self.data[idx] = int(value)

    def to_user_def(self) -> RubyUserDef:
        count = len(self.data)
        header = struct.pack("<5i", self.dims, self.xsize, self.ysize, self.zsize, count)
        body = struct.pack(f"<{count}h", *self.data) if count > 0 else b""
        return RubyUserDef(class_name="Table", data=header + body)

    @classmethod
    def from_user_def(cls, udef: RubyUserDef) -> "RgssTable":
        if udef.class_name != "Table" or len(udef.data) < 20:
            raise ValueError(f"Invalid Table blob: {udef.class_name}")
        dims, xsize, ysize, zsize, count = struct.unpack("<5i", udef.data[:20])
        expected_bytes = 20 + count * 2
        if len(udef.data) < expected_bytes:
            raise ValueError(f"Truncated Table blob: expected {expected_bytes}, got {len(udef.data)}")
        values = list(struct.unpack(f"<{count}h", udef.data[20:expected_bytes])) if count > 0 else []
        return cls(dims=dims, xsize=xsize, ysize=ysize, zsize=zsize, data=values)


@dataclass
class RgssColor:
    """RGBA color serialized as RGSS Color (four IEEE-754 doubles)."""
    red: float = 0.0
    green: float = 0.0
    blue: float = 0.0
    alpha: float = 255.0

    def to_user_def(self) -> RubyUserDef:
        return RubyUserDef("Color", struct.pack("<4d", self.red, self.green, self.blue, self.alpha))

    @classmethod
    def from_user_def(cls, udef: RubyUserDef) -> "RgssColor":
        r, g, b, a = struct.unpack("<4d", udef.data[:32])
        return cls(red=r, green=g, blue=b, alpha=a)


@dataclass
class RgssTone:
    """RGB + Gray tone adjustment serialized as RGSS Tone (four IEEE-754 doubles)."""
    red: float = 0.0
    green: float = 0.0
    blue: float = 0.0
    gray: float = 0.0

    def to_user_def(self) -> RubyUserDef:
        return RubyUserDef("Tone", struct.pack("<4d", self.red, self.green, self.blue, self.gray))

    @classmethod
    def from_user_def(cls, udef: RubyUserDef) -> "RgssTone":
        r, g, b, gr = struct.unpack("<4d", udef.data[:32])
        return cls(red=r, green=g, blue=b, gray=gr)


def make_audio_file(name: str = "", volume: int = 100, pitch: int = 100) -> RubyObject:
    return RubyObject("RPG::AudioFile", {
        "@name": name,
        "@volume": volume,
        "@pitch": pitch,
    })


def make_move_route(repeat: bool = True, skippable: bool = False, list_cmds: Optional[List[RubyObject]] = None) -> RubyObject:
    cmds = list_cmds if list_cmds is not None else [RubyObject("RPG::MoveCommand", {"@code": 0, "@parameters": []})]
    return RubyObject("RPG::MoveRoute", {
        "@repeat": repeat,
        "@skippable": skippable,
        "@list": cmds,
    })


def make_event_command(code: int = 0, indent: int = 0, parameters: Optional[List[Any]] = None) -> RubyObject:
    return RubyObject("RPG::EventCommand", {
        "@code": code,
        "@indent": indent,
        "@parameters": parameters if parameters is not None else [],
    })


def make_event_page(
    graphic_name: str = "",
    direction: int = 2,
    pattern: int = 0,
    trigger: int = 0,
    commands: Optional[List[RubyObject]] = None,
    switch1_id: Optional[int] = None,
    switch2_id: Optional[int] = None,
    variable_id: Optional[int] = None,
    variable_value: int = 0,
    self_switch_ch: Optional[str] = None,
    through: bool = False,
    always_on_top: bool = False,
) -> RubyObject:
    condition = RubyObject("RPG::Event::Page::Condition", {
        "@switch1_valid": switch1_id is not None,
        "@switch2_valid": switch2_id is not None,
        "@variable_valid": variable_id is not None,
        "@self_switch_valid": self_switch_ch is not None,
        "@switch1_id": switch1_id if switch1_id is not None else 1,
        "@switch2_id": switch2_id if switch2_id is not None else 1,
        "@variable_id": variable_id if variable_id is not None else 1,
        "@variable_value": variable_value,
        "@self_switch_ch": self_switch_ch if self_switch_ch is not None else "A",
    })
    graphic = RubyObject("RPG::Event::Page::Graphic", {
        "@tile_id": 0,
        "@character_name": graphic_name,
        "@character_hue": 0,
        "@direction": direction,
        "@pattern": pattern,
        "@opacity": 255,
        "@blend_type": 0,
    })
    cmd_list = commands if commands is not None else [make_event_command(0, 0, [])]
    if not cmd_list or cmd_list[-1].ivars.get("@code") != 0:
        cmd_list = list(cmd_list) + [make_event_command(0, 0, [])]
    return RubyObject("RPG::Event::Page", {
        "@condition": condition,
        "@graphic": graphic,
        "@move_type": 0,
        "@move_speed": 3,
        "@move_frequency": 3,
        "@move_route": make_move_route(),
        "@walk_anime": True,
        "@step_anime": False,
        "@direction_fix": False,
        "@through": through,
        "@always_on_top": always_on_top,
        "@trigger": trigger,
        "@list": cmd_list,
    })


def make_event(event_id: int, name: str, x: int, y: int, pages: List[RubyObject]) -> RubyObject:
    return RubyObject("RPG::Event", {
        "@id": event_id,
        "@name": name,
        "@x": x,
        "@y": y,
        "@pages": pages,
    })
