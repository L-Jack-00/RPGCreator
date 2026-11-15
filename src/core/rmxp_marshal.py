# -*- coding: utf-8 -*-
"""
Pure-Python Ruby Marshal 4.8 Reader & Writer.
Supports standard RMXP serialization primitives, Bignum ('l'), symbol tables,
shared/circular object links ('@'), and RGSS user-defined binary blobs (Table, Color, Tone).
"""
from __future__ import annotations

import math
import struct
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

MARSHAL_MAJOR = 4
MARSHAL_MINOR = 8
FIXNUM_MIN = -(1 << 30)
FIXNUM_MAX = (1 << 30) - 1


@dataclass(frozen=True)
class RubySymbol:
    """Represents a Ruby Symbol distinct from plain strings."""
    name: str

    def __repr__(self) -> str:
        return f":{self.name}"


@dataclass
class RubyUserDef:
    """Represents a Ruby _dump user-defined object (Marshal tag 'u')."""
    class_name: str
    data: bytes


@dataclass
class RubyObject:
    """Represents a Ruby object instance (Marshal tag 'o')."""
    class_name: str
    ivars: Dict[str, Any] = field(default_factory=dict)


@dataclass
class RubyRegexp:
    """Represents a Ruby Regexp literal (Marshal tag '/')."""
    pattern: str
    options: int


@dataclass
class RubyClassRef:
    """Represents a Ruby Class or Module reference."""
    kind: str  # 'c' or 'm'
    name: str


class MarshalError(ValueError):
    """Raised when malformed or unsupported Marshal 4.8 data is encountered."""


class _MarshalReader:
    """Stateful single-pass Ruby Marshal 4.8 binary stream decoder."""

    def __init__(self, data: bytes, encoding: str = "utf-8") -> None:
        if len(data) < 2 or data[0] != MARSHAL_MAJOR or data[1] != MARSHAL_MINOR:
            ver = tuple(data[:2]) if len(data) >= 2 else ()
            raise MarshalError(f"Unsupported Marshal header: {ver}, expected (4, 8)")
        self._buf = data
        self._pos = 2
        self._encoding = encoding
        self._symbols: List[str] = []
        self._objects: List[Any] = []

    def _read_bytes(self, count: int) -> bytes:
        end = self._pos + count
        if end > len(self._buf):
            raise MarshalError(f"Unexpected EOF at offset {self._pos} (need {count} bytes)")
        chunk = self._buf[self._pos:end]
        self._pos = end
        return chunk

    def _read_byte(self) -> int:
        return self._read_bytes(1)[0]

    def _read_fixnum(self) -> int:
        c = struct.unpack("<b", self._read_bytes(1))[0]
        if c == 0:
            return 0
        if 6 <= c <= 127:
            return c - 5
        if -128 <= c <= -6:
            return c + 5
        if 1 <= c <= 4:
            raw = self._read_bytes(c)
            return int.from_bytes(raw, "little", signed=False)
        if -4 <= c <= -1:
            size = -c
            raw = self._read_bytes(size)
            pad = b"\xff" * (4 - size)
            return int.from_bytes(raw + pad, "little", signed=True)
        raise MarshalError(f"Invalid Fixnum prefix byte: {c}")

    def _read_raw_bytes_prefixed(self) -> bytes:
        length = self._read_fixnum()
        if length < 0:
            raise MarshalError(f"Negative byte length: {length}")
        return self._read_bytes(length)

    def _read_symbol_name(self) -> str:
        tag = chr(self._read_byte())
        if tag == ":":
            name = self._read_raw_bytes_prefixed().decode(self._encoding, errors="replace")
            self._symbols.append(name)
            return name
        if tag == ";":
            idx = self._read_fixnum()
            if idx < 0 or idx >= len(self._symbols):
                raise MarshalError(f"Invalid symlink index: {idx}")
            return self._symbols[idx]
        raise MarshalError(f"Expected symbol or symlink, got tag {tag!r}")

    def _reserve_object_slot(self) -> int:
        slot = len(self._objects)
        self._objects.append(None)
        return slot

    def read_value(self) -> Any:
        tag = chr(self._read_byte())

        if tag == "0":
            return None
        if tag == "T":
            return True
        if tag == "F":
            return False
        if tag == "i":
            return self._read_fixnum()
        if tag == "l":
            slot = self._reserve_object_slot()
            sign_ch = chr(self._read_byte())
            if sign_ch not in ("+", "-"):
                raise MarshalError(f"Invalid Bignum sign byte: {sign_ch!r}")
            word_count = self._read_fixnum()
            raw = self._read_bytes(word_count * 2)
            magnitude = int.from_bytes(raw, "little", signed=False)
            val = -magnitude if sign_ch == "-" else magnitude
            self._objects[slot] = val
            return val
        if tag == ":":
            name = self._read_raw_bytes_prefixed().decode(self._encoding, errors="replace")
            self._symbols.append(name)
            return RubySymbol(name)
        if tag == ";":
            idx = self._read_fixnum()
            if idx < 0 or idx >= len(self._symbols):
                raise MarshalError(f"Invalid symlink index: {idx}")
            return RubySymbol(self._symbols[idx])
        if tag == "@":
            idx = self._read_fixnum()
            if idx < 0 or idx >= len(self._objects):
                raise MarshalError(f"Invalid object link index: {idx}")
            return self._objects[idx]

        if tag == "I":
            obj = self.read_value()
            ivar_count = self._read_fixnum()
            encoding = self._encoding
            extra_ivars: Dict[str, Any] = {}
            for _ in range(ivar_count):
                k = self._read_symbol_name()
                v = self.read_value()
                if k == "E":
                    encoding = "utf-8" if v else self._encoding
                elif k == "encoding" and isinstance(v, (bytes, str)):
                    encoding = v.decode("ascii", errors="replace") if isinstance(v, bytes) else v
                else:
                    extra_ivars[k] = v
            if isinstance(obj, bytes):
                decoded = obj.decode(encoding, errors="replace")
                for idx in range(len(self._objects) - 1, -1, -1):
                    if self._objects[idx] is obj:
                        self._objects[idx] = decoded
                        break
                return decoded
            if isinstance(obj, RubyObject):
                obj.ivars.update(extra_ivars)
            return obj

        if tag == '"':
            slot = self._reserve_object_slot()
            raw = self._read_raw_bytes_prefixed()
            self._objects[slot] = raw
            return raw

        if tag == "f":
            slot = self._reserve_object_slot()
            text = self._read_raw_bytes_prefixed().decode("ascii", errors="replace")
            if text == "inf":
                val = float("inf")
            elif text == "-inf":
                val = float("-inf")
            elif text == "nan":
                val = float("nan")
            else:
                val = float(text.split("\x00", 1)[0])
            self._objects[slot] = val
            return val

        if tag == "[":
            slot = self._reserve_object_slot()
            count = self._read_fixnum()
            arr: List[Any] = []
            self._objects[slot] = arr
            for _ in range(count):
                arr.append(self.read_value())
            return arr

        if tag == "{":
            slot = self._reserve_object_slot()
            count = self._read_fixnum()
            mapping: Dict[Any, Any] = {}
            self._objects[slot] = mapping
            for _ in range(count):
                k = self._normalize_key(self.read_value(), self._encoding)
                v = self.read_value()
                mapping[k] = v
            return mapping

        if tag == "o":
            slot = self._reserve_object_slot()
            class_name = self._read_symbol_name()
            obj = RubyObject(class_name=class_name)
            self._objects[slot] = obj
            ivar_count = self._read_fixnum()
            for _ in range(ivar_count):
                key = self._read_symbol_name()
                val = self.read_value()
                if isinstance(val, bytes) and class_name != "RPG::Script":
                    val = val.decode(self._encoding, errors="replace")
                obj.ivars[key] = val
            return obj

        if tag == "u":
            slot = self._reserve_object_slot()
            class_name = self._read_symbol_name()
            raw = self._read_raw_bytes_prefixed()
            udef = RubyUserDef(class_name=class_name, data=raw)
            self._objects[slot] = udef
            return udef

        if tag == "/":
            slot = self._reserve_object_slot()
            pat = self._read_raw_bytes_prefixed().decode("utf-8", errors="replace")
            opts = self._read_byte()
            rx = RubyRegexp(pattern=pat, options=opts)
            self._objects[slot] = rx
            return rx

        if tag in ("c", "m"):
            slot = self._reserve_object_slot()
            name = self._read_raw_bytes_prefixed().decode("utf-8", errors="replace")
            ref = RubyClassRef(kind=tag, name=name)
            self._objects[slot] = ref
            return ref

        raise MarshalError(f"Unsupported Marshal tag {tag!r} at offset {self._pos - 1}")

    @staticmethod
    def _normalize_key(key: Any, encoding: str = "utf-8") -> Any:
        if isinstance(key, bytes):
            return key.decode(encoding, errors="replace")
        return key


def _decode_strings_recursive(value: Any, encoding: str = "utf-8", seen: Optional[set] = None) -> Any:
    """Converts raw Marshal byte strings to decoded strings safely across cyclic graphs."""
    if seen is None:
        seen = set()
    if isinstance(value, bytes):
        return value.decode(encoding, errors="replace")
    if isinstance(value, list):
        oid = id(value)
        if oid in seen:
            return value
        seen.add(oid)
        for i, item in enumerate(value):
            value[i] = _decode_strings_recursive(item, encoding, seen)
        return value
    if isinstance(value, dict):
        oid = id(value)
        if oid in seen:
            return value
        seen.add(oid)
        for k in list(value.keys()):
            value[k] = _decode_strings_recursive(value[k], encoding, seen)
        return value
    if isinstance(value, RubyObject):
        oid = id(value)
        if oid in seen:
            return value
        seen.add(oid)
        for k, v in value.ivars.items():
            value.ivars[k] = _decode_strings_recursive(v, encoding, seen)
        return value
    return value


class _MarshalWriter:
    """
    Stateful Ruby Marshal 4.8 encoder compatible with Ruby 1.8 (RMXP) and Ruby 3.x (mkxp-z).
    Maintains an object-link table ('@') for shared and circular object graphs plus Bignum ('l').
    """

    def __init__(self, encoding: str = "utf-8") -> None:
        self._out = bytearray([MARSHAL_MAJOR, MARSHAL_MINOR])
        self._encoding = encoding
        self._symbols: Dict[str, int] = {}
        self._obj_count: int = 0
        self._obj_links: Dict[int, int] = {}
        self._pinned_refs: List[Any] = []

    def _alloc_slot(self, obj: Any, track_link: bool = False) -> int:
        slot = self._obj_count
        self._obj_count += 1
        if track_link:
            self._obj_links[id(obj)] = slot
            self._pinned_refs.append(obj)
        return slot

    def _write_fixnum(self, val: int) -> None:
        if val == 0:
            self._out.append(0)
            return
        if 0 < val < 123:
            self._out.append(val + 5)
            return
        if -124 < val < 0:
            self._out.extend(struct.pack("<b", val - 5))
            return
        if val > 0:
            for size in range(1, 5):
                if val < (1 << (8 * size)):
                    self._out.append(size)
                    self._out.extend(val.to_bytes(size, "little", signed=False))
                    return
        else:
            for size in range(1, 5):
                if val >= -(1 << (8 * size - 1)):
                    self._out.extend(struct.pack("<b", -size))
                    self._out.extend(val.to_bytes(size, "little", signed=True))
                    return
        raise MarshalError(f"Integer out of 32-bit Fixnum range: {val}")

    def _write_raw_prefixed(self, raw: bytes) -> None:
        self._write_fixnum(len(raw))
        self._out.extend(raw)

    def _write_symbol(self, name: str) -> None:
        if name in self._symbols:
            self._out.append(ord(";"))
            self._write_fixnum(self._symbols[name])
            return
        idx = len(self._symbols)
        self._symbols[name] = idx
        self._out.append(ord(":"))
        self._write_raw_prefixed(name.encode(self._encoding, errors="replace"))

    def write_value(self, val: Any) -> None:
        if val is None:
            self._out.append(ord("0"))
            return
        if val is True:
            self._out.append(ord("T"))
            return
        if val is False:
            self._out.append(ord("F"))
            return
        if isinstance(val, int):
            if FIXNUM_MIN <= val <= FIXNUM_MAX:
                self._out.append(ord("i"))
                self._write_fixnum(val)
                return
            self._alloc_slot(val, track_link=False)
            self._out.append(ord("l"))
            self._out.append(ord("-") if val < 0 else ord("+"))
            mag = abs(val)
            byte_len = max(2, (mag.bit_length() + 7) // 8)
            if byte_len % 2 != 0:
                byte_len += 1
            word_count = byte_len // 2
            self._write_fixnum(word_count)
            self._out.extend(mag.to_bytes(byte_len, "little", signed=False))
            return
        if isinstance(val, float):
            self._alloc_slot(val, track_link=False)
            self._out.append(ord("f"))
            if math.isnan(val):
                rep = b"nan"
            elif math.isinf(val):
                rep = b"inf" if val > 0 else b"-inf"
            else:
                rep = repr(val).encode("ascii")
            self._write_raw_prefixed(rep)
            return
        if isinstance(val, RubySymbol):
            self._write_symbol(val.name)
            return
        if isinstance(val, bytes):
            self._alloc_slot(val, track_link=False)
            self._out.append(ord('"'))
            self._write_raw_prefixed(val)
            return
        if isinstance(val, str):
            self._alloc_slot(val, track_link=False)
            self._out.append(ord('"'))
            self._write_raw_prefixed(val.encode(self._encoding, errors="replace"))
            return

        # Shared / circular reference lookup for heap container and custom types
        oid = id(val)
        if oid in self._obj_links:
            self._out.append(ord("@"))
            self._write_fixnum(self._obj_links[oid])
            return

        if isinstance(val, list):
            self._alloc_slot(val, track_link=True)
            self._out.append(ord("["))
            self._write_fixnum(len(val))
            for item in val:
                self.write_value(item)
            return
        if isinstance(val, dict):
            self._alloc_slot(val, track_link=True)
            self._out.append(ord("{"))
            self._write_fixnum(len(val))
            for k, v in val.items():
                self.write_value(k)
                self.write_value(v)
            return
        if isinstance(val, RubyObject):
            self._alloc_slot(val, track_link=True)
            self._out.append(ord("o"))
            self._write_symbol(val.class_name)
            self._write_fixnum(len(val.ivars))
            for k, v in val.ivars.items():
                self._write_symbol(k)
                self.write_value(v)
            return
        if isinstance(val, RubyUserDef):
            self._alloc_slot(val, track_link=True)
            self._out.append(ord("u"))
            self._write_symbol(val.class_name)
            self._write_raw_prefixed(val.data)
            return
        if isinstance(val, RubyRegexp):
            self._alloc_slot(val, track_link=True)
            self._out.append(ord("/"))
            self._write_raw_prefixed(val.pattern.encode("utf-8"))
            self._out.append(val.options & 0xFF)
            return
        if isinstance(val, RubyClassRef):
            self._alloc_slot(val, track_link=True)
            self._out.append(ord(val.kind))
            self._write_raw_prefixed(val.name.encode("utf-8"))
            return
        raise MarshalError(f"Cannot marshal unsupported type: {type(val).__name__}")

    def to_bytes(self) -> bytes:
        return bytes(self._out)


def marshal_loads(data: bytes, decode_strings: bool = True, encoding: str = "utf-8") -> Any:
    """Deserializes a Ruby Marshal 4.8 byte payload into Python objects."""
    reader = _MarshalReader(data, encoding=encoding)
    val = reader.read_value()
    if decode_strings:
        val = _decode_strings_recursive(val, encoding=encoding)
    return val


def marshal_dumps(value: Any, encoding: str = "utf-8") -> bytes:
    """Serializes Python/Ruby wrapper objects into Ruby Marshal 4.8 bytes."""
    writer = _MarshalWriter(encoding=encoding)
    writer.write_value(value)
    return writer.to_bytes()
