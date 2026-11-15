# -*- coding: utf-8 -*-
"""
Cleanroom RGSSAD v1 / v2 / v3 Archive Reader & Writer.
Supports RMXP (.rgssad, v1), RMVX (.rgss2a, v2), and RMVXA (.rgss3a, v3)
obfuscated archives with path-traversal protection.
"""
from __future__ import annotations

import hashlib
import struct
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

RGSSAD_MAGIC_PREFIX = b"RGSSAD\x00"
DEFAULT_RGSSAD_KEY = 0xDEADCAFE


def _advance_key(key: int) -> int:
    return (key * 7 + 3) & 0xFFFFFFFF


def _xor_payload(data: bytes, start_key: int) -> bytes:
    """XORs file payload in 4-byte blocks with a local copy of the rolling key."""
    out = bytearray(len(data))
    key = start_key & 0xFFFFFFFF
    for idx, byte in enumerate(data):
        shift = (idx % 4) * 8
        out[idx] = byte ^ ((key >> shift) & 0xFF)
        if (idx % 4) == 3:
            key = _advance_key(key)
    return bytes(out)


def _xor_v3_filename(data: bytes, key: int) -> bytes:
    """XORs RGSS3A filenames against repeating 4-byte little-endian key chunks."""
    out = bytearray(len(data))
    for idx, byte in enumerate(data):
        shift = (idx % 4) * 8
        out[idx] = byte ^ ((key >> shift) & 0xFF)
    return bytes(out)


def _safe_dest(output_dir: Path, rel_name: str) -> Path:
    clean = rel_name.replace("\\", "/").lstrip("/")
    dest = (output_dir / clean).resolve()
    if output_dir not in dest.parents and dest != output_dir:
        raise ValueError(f"Unsafe archive entry path: {rel_name}")
    return dest


def _unpack_v1_v2(raw: bytes, output_dir: Path, initial_key: int) -> List[str]:
    pos = 8
    key = initial_key & 0xFFFFFFFF
    extracted: List[str] = []

    while pos < len(raw):
        if pos + 4 > len(raw):
            raise ValueError("Truncated filename length in RGSSAD v1/v2 archive")
        name_len = struct.unpack_from("<I", raw, pos)[0] ^ key
        pos += 4
        key = _advance_key(key)

        if name_len <= 0 or pos + name_len > len(raw):
            raise ValueError(f"Invalid entry filename length: {name_len}")

        name_bytes = bytearray(name_len)
        for i in range(name_len):
            name_bytes[i] = raw[pos + i] ^ (key & 0xFF)
            key = _advance_key(key)
        pos += name_len

        rel_name = name_bytes.decode("utf-8", errors="replace").replace("\\", "/").lstrip("/")
        if pos + 4 > len(raw):
            raise ValueError(f"Truncated file size for {rel_name}")

        file_size = struct.unpack_from("<I", raw, pos)[0] ^ key
        pos += 4
        key = _advance_key(key)

        if pos + file_size > len(raw):
            raise ValueError(f"Truncated payload for {rel_name}")

        payload = _xor_payload(raw[pos:pos + file_size], key)
        pos += file_size

        dest = _safe_dest(output_dir, rel_name)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(payload)
        extracted.append(rel_name)

    return extracted


def _unpack_v3(raw: bytes, output_dir: Path) -> List[str]:
    if len(raw) < 12:
        raise ValueError("Truncated RGSS3A header")
    seed = struct.unpack_from("<I", raw, 8)[0]
    key = (seed * 9 + 3) & 0xFFFFFFFF
    pos = 12
    entries: List[Tuple[int, int, int, str]] = []

    while pos + 4 <= len(raw):
        offset = struct.unpack_from("<I", raw, pos)[0] ^ key
        pos += 4
        if offset == 0:
            break
        if pos + 12 > len(raw):
            raise ValueError("Truncated RGSS3A index entry")
        size = struct.unpack_from("<I", raw, pos)[0] ^ key
        magic_key = struct.unpack_from("<I", raw, pos + 4)[0] ^ key
        name_len = struct.unpack_from("<I", raw, pos + 8)[0] ^ key
        pos += 12
        if name_len <= 0 or pos + name_len > len(raw):
            raise ValueError(f"Invalid RGSS3A filename length: {name_len}")
        rel_name = _xor_v3_filename(raw[pos:pos + name_len], key).decode("utf-8", errors="replace").replace("\\", "/").lstrip("/")
        pos += name_len
        entries.append((offset, size, magic_key, rel_name))

    extracted: List[str] = []
    for offset, size, magic_key, rel_name in entries:
        if offset + size > len(raw):
            raise ValueError(f"Truncated RGSS3A payload for {rel_name}")
        payload = _xor_payload(raw[offset:offset + size], magic_key)
        dest = _safe_dest(output_dir, rel_name)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(payload)
        extracted.append(rel_name)
    return extracted


def unpack_rgssad(archive_path: Path, output_dir: Path, initial_key: int = DEFAULT_RGSSAD_KEY) -> Dict[str, Any]:
    """Extracts an RGSSAD v1, v2 (.rgss2a), or v3 (.rgss3a) archive safely into output_dir."""
    archive_path = Path(archive_path)
    output_dir = Path(output_dir).resolve()
    raw = archive_path.read_bytes()

    if len(raw) < 8 or raw[:7] != RGSSAD_MAGIC_PREFIX:
        raise ValueError(f"Unsupported archive header in {archive_path}: expected RGSSAD magic")

    version = raw[7]
    output_dir.mkdir(parents=True, exist_ok=True)

    if version in (1, 2):
        extracted = _unpack_v1_v2(raw, output_dir, initial_key)
    elif version == 3:
        extracted = _unpack_v3(raw, output_dir)
    else:
        raise ValueError(f"Unsupported RGSSAD version: {version}")

    return {
        "archive": str(archive_path),
        "version": version,
        "output_dir": str(output_dir),
        "files_extracted": len(extracted),
        "files": extracted,
    }


def pack_rgssad(
    source_dir: Path,
    archive_path: Path,
    initial_key: int = DEFAULT_RGSSAD_KEY,
    version: int = 1,
) -> Dict[str, Any]:
    """Packs files under source_dir into an RGSSAD v1, v2, or v3 archive."""
    if version not in (1, 2, 3):
        raise ValueError(f"Unsupported RGSSAD target version: {version}")
    source_dir = Path(source_dir).resolve()
    archive_path = Path(archive_path)
    archive_path.parent.mkdir(parents=True, exist_ok=True)

    file_list = sorted(p for p in source_dir.rglob("*") if p.is_file())
    files_packed: List[str] = []

    if version in (1, 2):
        out = bytearray(RGSSAD_MAGIC_PREFIX + bytes([version]))
        key = initial_key & 0xFFFFFFFF
        for fpath in file_list:
            rel = fpath.relative_to(source_dir).as_posix().replace("/", "\\")
            rel_bytes = rel.encode("utf-8")
            data = fpath.read_bytes()

            out.extend(struct.pack("<I", len(rel_bytes) ^ key))
            key = _advance_key(key)
            for b in rel_bytes:
                out.append(b ^ (key & 0xFF))
                key = _advance_key(key)
            out.extend(struct.pack("<I", len(data) ^ key))
            key = _advance_key(key)
            out.extend(_xor_payload(data, key))
            files_packed.append(rel.replace("\\", "/"))
        archive_path.write_bytes(bytes(out))
    else:
        seed = initial_key & 0xFFFFFFFF
        key = (seed * 9 + 3) & 0xFFFFFFFF
        header_entries = []
        index_size = 12  # 8 magic + 4 seed
        for fpath in file_list:
            rel = fpath.relative_to(source_dir).as_posix().replace("/", "\\")
            rel_bytes = rel.encode("utf-8")
            data = fpath.read_bytes()
            header_entries.append((rel, rel_bytes, data))
            index_size += 16 + len(rel_bytes)
        index_size += 4  # terminating 0 offset

        out = bytearray(RGSSAD_MAGIC_PREFIX + bytes([3]) + struct.pack("<I", seed))
        payload_buf = bytearray()
        cur_offset = index_size
        for idx, (rel, rel_bytes, data) in enumerate(header_entries):
            magic_key = ((idx + 1) * 0x01010101) & 0xFFFFFFFF
            out.extend(struct.pack("<IIII", cur_offset ^ key, len(data) ^ key, magic_key ^ key, len(rel_bytes) ^ key))
            out.extend(_xor_v3_filename(rel_bytes, key))
            payload_buf.extend(_xor_payload(data, magic_key))
            cur_offset += len(data)
            files_packed.append(rel.replace("\\", "/"))

        out.extend(struct.pack("<I", 0 ^ key))
        out.extend(payload_buf)
        archive_path.write_bytes(bytes(out))

    return {
        "source_dir": str(source_dir),
        "archive": str(archive_path),
        "version": version,
        "files_packed": len(files_packed),
    }


def compute_rgssad_stamp(archive_path: Path) -> str:
    """Computes a fast SHA-256 stamp string for cache invalidation checks."""
    h = hashlib.sha256()
    with Path(archive_path).open("rb") as fp:
        for chunk in iter(lambda: fp.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


class RgssadArchiveReader:
    """Indexes RGSSAD v1/v2/v3 archives once and streams decrypted entries straight into memory."""

    def __init__(self, archive_path: Path, initial_key: int = DEFAULT_RGSSAD_KEY) -> None:
        self.archive_path = Path(archive_path)
        self.initial_key = int(initial_key) & 0xFFFFFFFF
        self._index: Dict[str, Tuple[int, int, int]] = {}
        self.version: int = 0
        self._build_index()

    def _build_index(self) -> None:
        raw = self.archive_path.read_bytes()
        if len(raw) < 8 or raw[:7] != RGSSAD_MAGIC_PREFIX:
            raise ValueError(f"Unsupported archive header in {self.archive_path}")
        self.version = raw[7]
        if self.version in (1, 2):
            pos = 8
            key = self.initial_key
            while pos < len(raw):
                if pos + 4 > len(raw):
                    raise ValueError("Truncated RGSSAD index header")
                name_len = struct.unpack_from("<I", raw, pos)[0] ^ key
                pos += 4
                key = _advance_key(key)
                if name_len <= 0 or pos + name_len > len(raw):
                    raise ValueError(f"Corrupt or truncated RGSSAD entry name (len={name_len})")
                name_bytes = bytearray(name_len)
                for i in range(name_len):
                    name_bytes[i] = raw[pos + i] ^ (key & 0xFF)
                    key = _advance_key(key)
                pos += name_len
                rel_name = name_bytes.decode("utf-8", errors="replace").replace("\\", "/").lstrip("/")
                if pos + 4 > len(raw):
                    raise ValueError("Truncated RGSSAD entry size")
                file_size = struct.unpack_from("<I", raw, pos)[0] ^ key
                pos += 4
                key = _advance_key(key)
                if pos + file_size > len(raw):
                    raise ValueError(f"Truncated RGSSAD payload for {rel_name}")
                self._index[rel_name.lower()] = (pos, file_size, key)
                pos += file_size
        elif self.version == 3:
            if len(raw) < 12:
                raise ValueError("Truncated RGSS3A header")
            seed = struct.unpack_from("<I", raw, 8)[0]
            key = (seed * 9 + 3) & 0xFFFFFFFF
            pos = 12
            terminated = False
            while pos + 4 <= len(raw):
                offset = struct.unpack_from("<I", raw, pos)[0] ^ key
                pos += 4
                if offset == 0:
                    terminated = True
                    break
                if pos + 12 > len(raw):
                    raise ValueError("Truncated RGSS3A index entry")
                size = struct.unpack_from("<I", raw, pos)[0] ^ key
                magic_key = struct.unpack_from("<I", raw, pos + 4)[0] ^ key
                name_len = struct.unpack_from("<I", raw, pos + 8)[0] ^ key
                pos += 12
                if name_len <= 0 or pos + name_len > len(raw):
                    raise ValueError(f"Corrupt or truncated RGSS3A entry name (len={name_len})")
                rel_name = (
                    _xor_v3_filename(raw[pos:pos + name_len], key)
                    .decode("utf-8", errors="replace")
                    .replace("\\", "/")
                    .lstrip("/")
                )
                pos += name_len
                if offset + size > len(raw):
                    raise ValueError(f"Truncated RGSS3A payload for {rel_name}")
                self._index[rel_name.lower()] = (offset, size, magic_key)
            if not terminated:
                raise ValueError("Truncated RGSS3A index table (missing terminator)")
        else:
            raise ValueError(f"Unsupported RGSSAD version: {self.version}")

    def find_entry(self, rel_path: str, extensions: Tuple[str, ...] = ()) -> Optional[str]:
        norm = str(rel_path).replace("\\", "/").lstrip("/").lower()
        if norm in self._index:
            return norm
        for ext in extensions:
            cand = f"{norm}{ext.lower()}"
            if cand in self._index:
                return cand
        return None

    def read_bytes(self, rel_path: str, extensions: Tuple[str, ...] = ()) -> Optional[bytes]:
        matched = self.find_entry(rel_path, extensions=extensions)
        if matched is None:
            return None
        offset, size, xkey = self._index[matched]
        with self.archive_path.open("rb") as fp:
            fp.seek(offset)
            enc = fp.read(size)
        return _xor_payload(enc, xkey)

    @property
    def entries(self) -> List[str]:
        return sorted(self._index.keys())
