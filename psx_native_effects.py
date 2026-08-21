"""Strict read-only parser for recovered Urban Assault PSX ``PV2`` effects.

Every one of the 264 recovered ``PV2`` files uses the same 352-byte static
mesh grammar: a 32-byte version-1 header, ten signed 16.16 vertices, and five
40-byte faces.  Horizontal/vertical families change authored geometry axis,
UVs, selector, and shade values, but the files contain no frame timing,
packed-model ordinal, or attachment record.  This module therefore exposes
them only as immutable, unbound static effect meshes.

The two final header dwords are identical authoring-address residues in the
entire corpus.  They are validated and preserved but never dereferenced.
Opaque face prefixes are likewise preserved and not assigned render flags.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import re
import struct


PV2_PARSER_ID = "openuastudio_psx_pv2_static_effect"
PV2_PARSER_VERSION = 1
PV2_FORMAT_VERSION = 1
PV2_HEADER_SIZE = 32
PV2_VERTEX_COUNT = 10
PV2_VERTEX_SIZE = 12
PV2_FACE_COUNT = 5
PV2_FACE_SIZE = 40
PV2_BODY_SIZE = (
    PV2_HEADER_SIZE
    + PV2_VERTEX_COUNT * PV2_VERTEX_SIZE
    + PV2_FACE_COUNT * PV2_FACE_SIZE
)
PV2_VERTEX_POINTER_RESIDUE = 0x00427C20
PV2_FACE_POINTER_RESIDUE = 0x00427C98
PV2_FIXED_ONE = 65536.0
PV2_BINDING_STATUS = "static_effect_mesh_unbound"
PV2_OPAQUE_PREFIXES = frozenset((b"\x00\x00\x00\x00", b"\x00\x20\x00\x00"))
_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:")


class PsxNativeEffectError(ValueError):
    """Raised when a candidate PV2 is outside the recovered strict grammar."""


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _is_reparse_point(path: Path) -> bool:
    try:
        if path.is_symlink():
            return True
        is_junction = getattr(path, "is_junction", None)
        return bool(is_junction is not None and is_junction())
    except OSError:
        return True


def _reject_reparse_path(path: Path) -> None:
    absolute = Path(os.path.abspath(os.fspath(path)))
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current /= part
        if _is_reparse_point(current):
            raise PsxNativeEffectError(
                f"PV2 source path contains a symlink or junction: {current}")


def _normalize_logical_path(logical_path: str) -> str:
    """Preserve an authored relative path while rejecting local-path leaks."""

    if not isinstance(logical_path, str) or not logical_path or "\0" in logical_path:
        raise PsxNativeEffectError("PV2 logical path must be a nonempty string")
    normalized = logical_path.replace("\\", "/")
    parts = normalized.split("/")
    if (
            normalized.startswith("/")
            or _WINDOWS_DRIVE_RE.match(normalized)
            or any(part in ("", ".", "..") for part in parts)):
        raise PsxNativeEffectError(
            f"PV2 logical path must be normalized and relative: "
            f"{logical_path!r}")
    return normalized


def _collapse_repeated_corners(
        indices: tuple[int, int, int, int],
        uvs: tuple[tuple[int, int], ...],
        shades: tuple[int, int, int, int], *,
        logical_path: str, face_index: int,
        ) -> tuple[tuple[int, ...], tuple[tuple[int, int], ...], tuple[int, ...]]:
    seen: set[int] = set()
    kept_indices: list[int] = []
    kept_uvs: list[tuple[int, int]] = []
    kept_shades: list[int] = []
    for index, uv, shade in zip(indices, uvs, shades):
        if index in seen:
            continue
        seen.add(index)
        kept_indices.append(index)
        kept_uvs.append(uv)
        kept_shades.append(shade)
    if len(kept_indices) not in (3, 4):
        raise PsxNativeEffectError(
            f"{logical_path}: face {face_index} has {len(kept_indices)} "
            "unique corners; expected 3 or 4")
    return tuple(kept_indices), tuple(kept_uvs), tuple(kept_shades)


@dataclass(frozen=True, slots=True)
class PsxPv2Face:
    """One validated PV2 face with both raw and collapsed corner views."""

    source_offset: int
    raw_record: bytes
    opaque_prefix: bytes
    raw_vertex_indices: tuple[int, int, int, int]
    raw_uv_values: tuple[
        tuple[int, int], tuple[int, int], tuple[int, int], tuple[int, int]]
    texture_selector: int
    raw_corner_shades: tuple[int, int, int, int]
    vertex_indices: tuple[int, ...]
    uv_values: tuple[tuple[int, int], ...]
    corner_shades: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class PsxPv2Mesh:
    """Immutable static PV2 effect plus source-preserving provenance."""

    logical_path: str
    format_id: str
    format_version: int
    parser_id: str
    parser_version: int
    binding_status: str
    body_size: int
    source_sha256: str
    vertex_stream_sha256: str
    face_stream_sha256: str
    raw_header: bytes
    raw_translation: tuple[int, int, int]
    translation: tuple[float, float, float]
    vertex_pointer_residue: int
    face_pointer_residue: int
    raw_vertices: tuple[tuple[int, int, int], ...]
    vertices: tuple[tuple[float, float, float], ...]
    faces: tuple[PsxPv2Face, ...]

    @property
    def label(self) -> str:
        return (
            f"{self.logical_path} | PV2 static effect | "
            f"{len(self.vertices)} vertices | {len(self.faces)} faces | unbound"
        )

    @property
    def portable_identity(self) -> dict:
        return {
            "logical_path": self.logical_path,
            "source_sha256": self.source_sha256,
            "format_id": self.format_id,
            "format_version": self.format_version,
            "parser_id": self.parser_id,
            "parser_version": self.parser_version,
            "binding_status": self.binding_status,
            "body_size": self.body_size,
            "vertex_count": len(self.vertices),
            "face_count": len(self.faces),
            "vertex_pointer_residue": self.vertex_pointer_residue,
            "face_pointer_residue": self.face_pointer_residue,
        }


def parse_pv2_bytes(data: bytes, *, logical_path: str) -> PsxPv2Mesh:
    """Parse only the exact static PV2 grammar recovered from the corpus."""

    normalized_path = _normalize_logical_path(logical_path)
    if len(data) != PV2_BODY_SIZE:
        raise PsxNativeEffectError(
            f"{normalized_path}: PV2 source contains {len(data)} bytes; "
            f"expected exactly {PV2_BODY_SIZE}")

    header = data[:PV2_HEADER_SIZE]
    version = struct.unpack_from("<I", header, 0)[0]
    if version != PV2_FORMAT_VERSION:
        raise PsxNativeEffectError(
            f"{normalized_path}: unsupported PV2 version {version}")
    raw_translation = struct.unpack_from("<iii", header, 4)
    vertex_count, face_count, vertex_pointer, face_pointer = (
        struct.unpack_from("<4I", header, 0x10))
    if vertex_count != PV2_VERTEX_COUNT or face_count != PV2_FACE_COUNT:
        raise PsxNativeEffectError(
            f"{normalized_path}: PV2 counts are {vertex_count}/{face_count}; "
            f"expected {PV2_VERTEX_COUNT}/{PV2_FACE_COUNT}")
    if vertex_pointer != PV2_VERTEX_POINTER_RESIDUE:
        raise PsxNativeEffectError(
            f"{normalized_path}: unrecognized PV2 vertex-pointer residue "
            f"0x{vertex_pointer:08X}")
    if face_pointer != PV2_FACE_POINTER_RESIDUE:
        raise PsxNativeEffectError(
            f"{normalized_path}: unrecognized PV2 face-pointer residue "
            f"0x{face_pointer:08X}")

    vertex_offset = PV2_HEADER_SIZE
    face_offset = vertex_offset + PV2_VERTEX_COUNT * PV2_VERTEX_SIZE
    vertex_stream = data[vertex_offset:face_offset]
    face_stream = data[face_offset:]
    raw_vertices = tuple(
        struct.unpack_from("<iii", vertex_stream, index * PV2_VERTEX_SIZE)
        for index in range(PV2_VERTEX_COUNT)
    )
    vertices = tuple(
        tuple(component / PV2_FIXED_ONE for component in vertex)
        for vertex in raw_vertices
    )

    faces: list[PsxPv2Face] = []
    for face_index in range(PV2_FACE_COUNT):
        source_offset = face_offset + face_index * PV2_FACE_SIZE
        record = data[source_offset:source_offset + PV2_FACE_SIZE]
        opaque_prefix = record[:4]
        if opaque_prefix not in PV2_OPAQUE_PREFIXES:
            raise PsxNativeEffectError(
                f"{normalized_path}: face {face_index} has unrecognized opaque "
                f"prefix {opaque_prefix.hex()}")
        raw_indices = struct.unpack_from("<4H", record, 4)
        if any(index >= PV2_VERTEX_COUNT for index in raw_indices):
            bad = next(index for index in raw_indices if index >= PV2_VERTEX_COUNT)
            raise PsxNativeEffectError(
                f"{normalized_path}: face {face_index} vertex index {bad} is "
                f"outside 0..{PV2_VERTEX_COUNT - 1}")
        raw_uv_scalars = struct.unpack_from("<8H", record, 12)
        if any(value > 0xFF for value in raw_uv_scalars):
            raise PsxNativeEffectError(
                f"{normalized_path}: face {face_index} has a non-narrowable "
                "PV2 UV value")
        raw_uvs = tuple(
            (raw_uv_scalars[corner * 2], raw_uv_scalars[corner * 2 + 1])
            for corner in range(4)
        )
        selector = struct.unpack_from("<I", record, 28)[0]
        if selector > 127:
            raise PsxNativeEffectError(
                f"{normalized_path}: face {face_index} selector {selector} is "
                "outside the validated PSX material domain 0..127")
        raw_shades = struct.unpack_from("<4H", record, 32)
        if any(value > 0xFF for value in raw_shades):
            raise PsxNativeEffectError(
                f"{normalized_path}: face {face_index} has a non-narrowable "
                "PV2 corner shade")
        indices, uvs, shades = _collapse_repeated_corners(
            raw_indices,
            raw_uvs,
            raw_shades,
            logical_path=normalized_path,
            face_index=face_index,
        )
        faces.append(PsxPv2Face(
            source_offset=source_offset,
            raw_record=record,
            opaque_prefix=opaque_prefix,
            raw_vertex_indices=raw_indices,
            raw_uv_values=raw_uvs,
            texture_selector=selector,
            raw_corner_shades=raw_shades,
            vertex_indices=indices,
            uv_values=uvs,
            corner_shades=shades,
        ))

    return PsxPv2Mesh(
        logical_path=normalized_path,
        format_id="PV2",
        format_version=version,
        parser_id=PV2_PARSER_ID,
        parser_version=PV2_PARSER_VERSION,
        binding_status=PV2_BINDING_STATUS,
        body_size=len(data),
        source_sha256=_sha256(data),
        vertex_stream_sha256=_sha256(vertex_stream),
        face_stream_sha256=_sha256(face_stream),
        raw_header=header,
        raw_translation=raw_translation,
        translation=tuple(
            component / PV2_FIXED_ONE for component in raw_translation),
        vertex_pointer_residue=vertex_pointer,
        face_pointer_residue=face_pointer,
        raw_vertices=raw_vertices,
        vertices=vertices,
        faces=tuple(faces),
    )


def parse_recovered_pv2_candidate_bytes(
        data: bytes, *, logical_path: str) -> PsxPv2Mesh | None:
    """Parse a recovered-size candidate and ignore unknown PV2 layouts.

    The recovered corpus proves only the exact 352-byte grammar.  Other sizes
    remain unavailable rather than being guessed; a 352-byte candidate must
    satisfy the full grammar or raises ``PsxNativeEffectError``.
    """

    if len(data) != PV2_BODY_SIZE:
        return None
    return parse_pv2_bytes(data, logical_path=logical_path)


def parse_pv2_file(
        path: Path, *, logical_path: str | None = None) -> PsxPv2Mesh:
    """Read one PV2 without following a symlink or directory junction."""

    source = Path(path)
    _reject_reparse_path(source)
    return parse_pv2_bytes(
        source.read_bytes(), logical_path=logical_path or source.name)
