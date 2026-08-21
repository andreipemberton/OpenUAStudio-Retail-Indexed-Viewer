"""Strict, read-only loaders for native Urban Assault PlayStation meshes.

The PlayStation prototypes do not use the PC ``BASE/SKLT/ILBM`` asset
pipeline.  This module deliberately keeps their source identity separate and
implements only fields that have been cross-validated across recovered PSW
and PW3 files.  Unknown bytes are preserved; they are never reinterpreted as
PC materials or animation data.

Supported inputs are an extracted PlayStation disc tree containing
``SYSTEM.CNF`` and ``UNITMODL``, a sector-aligned ``UNIT.BIN`` archive, or a
loose version-1 PSW/PSV or version-3 PW3 mesh.  Packed meshes retain their
legacy dense ordinal, while a separate runtime model slot is exposed only
when the exact boot executable proves the archive allocation table.  Friendly
names remain unbound.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import os
from pathlib import Path
import re
import struct
from typing import Iterable

from psx_native_effects import (
    PV2_BODY_SIZE,
    PsxNativeEffectError,
    PsxPv2Mesh,
    parse_recovered_pv2_candidate_bytes,
)
from psx_psw_pipeline import (
    PsxPswPipelineError,
    psw_material_local_uv_quotient,
)
from psx_native_textures import (
    LATE_SETGFX_SIZE,
    SECTOR_PADDED_OBSERVED_SIZES,
    PsxNativeTexturePack,
    PsxNativeTextureError,
    parse_late_setgfx_file,
    parse_sector_padded_setgfx_file,
)


PSX_NATIVE_PARSER_ID = "openuastudio_psx_native_mesh"
PSX_NATIVE_PARSER_VERSION = 3
UNIT_ARCHIVE_MAGIC = bytes.fromhex("4e0d0a1a")
SECTOR_SIZE = 0x800
MESH_HEADER_SIZE = 80
VERTEX_SIZE = 12
PW3_FACE_SIZE = 26
PSW_FACE_SIZE = 76
PW3_TWO_SIDED_FLAG = 0x4000
# The June executable writes GPU packet corners in raw-file order 1,0,2,3.
# Its NCLIP test consumes the first packet triangle before any clip or quad
# decomposition.  The viewer keeps raw slots in file order, so the equivalent
# source indices are explicit here rather than hidden in a generic winding
# helper.
PW3_NCLIP_RAW_CORNER_ORDER = (1, 0, 2)
PW3_RAW_REVERSE_FAN_TRIANGLES = ((0, 2, 1), (0, 3, 2))
FIXED_ONE = 65536.0
_SUPPORTED_MESH_VERSIONS = {1, 3}
_BOOT_RE = re.compile(
    r"(?im)^\s*BOOT\s*=\s*cdrom:\\+([^;\r\n]+)(?:;\d+)?\s*$")


class PsxNativeAssetError(ValueError):
    """Raised when a candidate PSX source cannot be decoded exactly."""


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def _u32(data: bytes, offset: int) -> int:
    return struct.unpack_from("<I", data, offset)[0]


def _is_reparse_point(path: Path) -> bool:
    """Return whether ``path`` can redirect reads outside its parent.

    ``Path.is_symlink`` does not classify Windows directory junctions as
    symlinks.  The native-source boundary rejects both forms because a file
    selected beneath an extracted tree must not silently resolve to bytes
    elsewhere on the machine.
    """

    candidate = Path(path)
    try:
        if candidate.is_symlink():
            return True
        is_junction = getattr(candidate, "is_junction", None)
        return bool(is_junction is not None and is_junction())
    except OSError:
        # An unreadable source component is not safe to traverse.  Callers
        # will reject it when they try to identify the component.
        return True


def _reject_reparse_path(path: Path) -> None:
    """Reject a selected path containing a symlink or directory junction."""

    absolute = Path(os.path.abspath(os.fspath(path)))
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current /= part
        if _is_reparse_point(current):
            raise PsxNativeAssetError(
                f"native PSX source path contains a symlink or junction: "
                f"{current}")


def _reject_source_component(path: Path) -> None:
    if _is_reparse_point(path):
        raise PsxNativeAssetError(
            "native PSX source components may not be symlinks or junctions: "
            f"{path}")


@dataclass(frozen=True)
class PsxNativeFace:
    """One validated triangle or quad from a PSW/PSV/PW3 mesh."""

    source_offset: int
    raw_record: bytes
    opaque_prefix: bytes
    vertex_indices: tuple[int, ...]
    uv_bytes: tuple[tuple[int, int], ...]
    texture_selector: int
    corner_shades: tuple[int, ...]
    # Keep the original four authored slots separately from the established
    # unique-corner diagnostic view above.  PW3 rendering consumes these raw
    # tuples; compatibility callers can continue using the collapsed fields.
    raw_vertex_indices: tuple[int, int, int, int] = ()
    raw_uv_bytes: tuple[
        tuple[int, int], tuple[int, int], tuple[int, int], tuple[int, int]] = ()
    raw_corner_shades: tuple[int, int, int, int] = ()
    # Version-1 PSW/PSV retains the exact authored signed 16.16 components.
    # ``raw_uv_bytes`` remains the compatibility narrowing used by existing
    # inspectors; rendering derives its separate material-local quotient from
    # this lossless field.  PW3 leaves it empty and keeps byte UV behavior.
    psw_uv_fixed_16_16: tuple[
        tuple[int, int], tuple[int, int], tuple[int, int], tuple[int, int]] = ()
    # Only PW3 v3 uses the little-endian u16 at prefix +0 as the proven bit-14
    # NCLIP-bypass flag.  PSW/PSV leaves this PW3-specific field as None: its
    # eight prefix bytes remain preserved, while the recovered legacy packet
    # path independently applies unconditional strict-positive NCLIP.
    pw3_primitive_flags: int | None = None

    @property
    def pw3_two_sided(self) -> bool | None:
        """Return the proven PW3 bit-14 state, or None when it does not apply.

        ``None`` on PSW/PSV is not an unresolved cull policy.  The recovered
        legacy executable path unconditionally accepts only positive NCLIP
        MAC0 results; it has no per-face two-sided bypass.
        """

        if self.pw3_primitive_flags is None:
            return None
        return bool(self.pw3_primitive_flags & PW3_TWO_SIDED_FLAG)


@dataclass(frozen=True)
class PsxNativeMesh:
    """Immutable native mesh plus portable source provenance."""

    logical_path: str
    format_id: str
    format_version: int
    archive_ordinal: int | None
    archive_offset: int | None
    archive_sector: int | None
    body_size: int
    body_sha256: str
    vertex_stream_sha256: str
    face_stream_sha256: str
    raw_vertices: tuple[tuple[int, int, int], ...]
    vertices: tuple[tuple[float, float, float], ...]
    faces: tuple[PsxNativeFace, ...]
    # ``archive_ordinal`` remains the historic dense mesh enumeration.  The
    # runtime model id instead indexes every executable allocation-table slot,
    # including empty placeholders, so it is exposed separately and only
    # when exact boot-executable evidence proves the mapping.
    model_slot: int | None = None
    model_slot_evidence_id: str | None = None

    @property
    def label(self) -> str:
        if self.archive_ordinal is not None:
            identity = (
                f"model slot {self.model_slot:03d} | "
                f"dense ordinal {self.archive_ordinal:03d}"
                if self.model_slot is not None else
                f"dense ordinal {self.archive_ordinal:03d}"
            )
            return (
                f"{identity} | PW3 | sector "
                f"{self.archive_sector} | 0x{self.archive_offset:08X} | "
                f"{len(self.vertices)} vertices | {len(self.faces)} faces"
            )
        return (
            f"{self.logical_path} | {self.format_id} | "
            f"{len(self.vertices)} vertices | {len(self.faces)} faces"
        )


@dataclass(frozen=True)
class PsxNativeModelSlotEvidence:
    """Portable proof that one boot executable defines ``UNIT.BIN`` slots.

    Absence of this object is meaningful: model slots and trailing-sentinel
    status were not proven and must not be inferred from archive bytes alone.
    """

    evidence_id: str
    unit_archive_sha256: str
    boot_executable_sha256: str
    executable_table_offset: int
    executable_table_entry_count: int
    executable_table_sha256: str
    empty_model_slots: tuple[int, ...]
    trailing_sentinel_archive_sector: int | None


@dataclass(frozen=True)
class PsxNativeBuild:
    """One verified extracted-disc source and its native mesh inventory."""

    root: Path
    system_cnf_logical_path: str
    system_cnf_sha256: str
    boot_executable_logical_path: str
    boot_executable_sha256: str
    unit_archive_logical_path: str | None
    unit_archive_sha256: str | None
    vehicle_roster_logical_path: str | None
    vehicle_roster_sha256: str | None
    vehicle_roster: tuple[str, ...]
    meshes: tuple[PsxNativeMesh, ...]
    texture_packs: tuple[PsxNativeTexturePack, ...] = ()
    model_slot_evidence: PsxNativeModelSlotEvidence | None = None
    effects: tuple[PsxPv2Mesh, ...] = ()

    @property
    def portable_identity(self) -> dict:
        """Return provenance without leaking the local extracted-tree path."""

        evidence = self.model_slot_evidence
        portable_evidence = (
            None
            if evidence is None else
            {
                "evidence_id": evidence.evidence_id,
                "unit_archive_sha256": evidence.unit_archive_sha256,
                "boot_executable_sha256": evidence.boot_executable_sha256,
                "executable_table_offset": evidence.executable_table_offset,
                "executable_table_entry_count": (
                    evidence.executable_table_entry_count),
                "executable_table_sha256": (
                    evidence.executable_table_sha256),
                "empty_model_slots": list(evidence.empty_model_slots),
                "trailing_sentinel_archive_sector": (
                    evidence.trailing_sentinel_archive_sector),
            }
        )
        return {
            "source_container_kind": "extracted_psx_disc_tree",
            "system_cnf_path": self.system_cnf_logical_path,
            "system_cnf_sha256": self.system_cnf_sha256,
            "boot_executable_path": self.boot_executable_logical_path,
            "boot_executable_sha256": self.boot_executable_sha256,
            "unit_archive_path": self.unit_archive_logical_path,
            "unit_archive_sha256": self.unit_archive_sha256,
            "vehicle_roster_path": self.vehicle_roster_logical_path,
            "vehicle_roster_sha256": self.vehicle_roster_sha256,
            "native_mesh_count": len(self.meshes),
            "name_binding_status": "friendly_name_unmapped_roster",
            "model_slot_binding_status": (
                "executable_allocation_table_proven"
                if evidence is not None else "unavailable_unproven"),
            "model_slot_evidence": portable_evidence,
            "native_effect_count": len(self.effects),
            "native_effects": [
                {
                    "logical_path": effect.logical_path,
                    "source_sha256": effect.source_sha256,
                    "vertex_stream_sha256": effect.vertex_stream_sha256,
                    "face_stream_sha256": effect.face_stream_sha256,
                    "format_id": effect.format_id,
                    "format_version": effect.format_version,
                    "parser_id": effect.parser_id,
                    "parser_version": effect.parser_version,
                    "binding_status": effect.binding_status,
                }
                for effect in self.effects
            ],
            "native_texture_packs": [
                {
                    "logical_path": pack.logical_path,
                    "sha256": pack.source_sha256,
                    # Keep the historical key for consumers that already read
                    # it, but derive it from the immutable decoded layout.
                    "profile": pack.layout_id,
                    "layout_id": pack.layout_id,
                    "selector_to_pixel_bank_mapping": (
                        pack.selector_to_pixel_bank_mapping),
                    "populated_selector_count": (
                        pack.material_slot_count),
                    "populated_selectors": list(
                        pack.populated_selectors),
                }
                for pack in self.texture_packs
            ],
        }


def _collapse_repeated_corners(
        indices: tuple[int, int, int, int],
        uvs: tuple[tuple[int, int], ...],
        shades: tuple[int, int, int, int],
        *, source: str, face_index: int) -> tuple[
            tuple[int, ...], tuple[tuple[int, int], ...], tuple[int, ...]]:
    """Collapse the repeated-corner triangle convention without guessing.

    Prototype files repeat different corner slots, so checking only the last
    two indices is incorrect.  The first authored occurrence is retained with
    its corresponding UV and shade value.
    """

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
        raise PsxNativeAssetError(
            f"{source}: face {face_index} has "
            f"{len(kept_indices)} unique corners; expected 3 or 4")
    return tuple(kept_indices), tuple(kept_uvs), tuple(kept_shades)


def _decode_pw3_face(record: bytes) -> tuple[
        bytes, tuple[int, int, int, int],
        tuple[tuple[int, int], ...], int, tuple[int, int, int, int]]:
    indices = struct.unpack_from("<4H", record, 4)
    uvs = tuple((record[12 + corner * 2], record[13 + corner * 2])
                for corner in range(4))
    selector = struct.unpack_from("<H", record, 20)[0]
    shades = tuple(record[22:26])
    return record[:4], indices, uvs, selector, shades


def _narrow_psw_fixed_byte(
        value: int, *, source: str, face_index: int, field: str,
        allow_wrapped_minus_one: bool = False) -> int:
    """Narrow one cross-validated 16.16 PSW field without truncation.

    Recovered PSW UVs are integral signed 16.16 values in the byte domain,
    with independently observed ``-1.0`` values retained as compatibility
    byte 255.  Fractional values and every other out-of-domain integer are not
    proven encodings and therefore fail closed.  Corner shades do not use the
    wrapped UV sentinel.
    """

    if value & 0xFFFF:
        raise PsxNativeAssetError(
            f"{source}: face {face_index} has a non-integral PSW {field}")
    integer = value >> 16
    if allow_wrapped_minus_one and integer == -1:
        return 0xFF
    if not 0 <= integer <= 0xFF:
        raise PsxNativeAssetError(
            f"{source}: face {face_index} has a non-narrowable PSW {field}")
    return integer


def _decode_psw_face(record: bytes, *, source: str, face_index: int) -> tuple[
        bytes, tuple[int, int, int, int],
        tuple[tuple[int, int], ...], int, tuple[int, int, int, int],
        tuple[tuple[int, int], tuple[int, int],
              tuple[int, int], tuple[int, int]]]:
    # Cross-build validation establishes that PW3 is a narrowed encoding of
    # these PSW v1 fields: four u32 indices, eight 16.16 UV coordinates, one
    # u32 texture selector, and four 16.16 corner shades.  The first eight
    # bytes remain opaque and are intentionally preserved.
    index_values = struct.unpack_from("<4I", record, 8)
    selector_value = struct.unpack_from("<I", record, 56)[0]
    if any(value > 0xFFFF for value in index_values):
        raise PsxNativeAssetError(
            f"{source}: face {face_index} has a non-narrowable PSW index")
    if selector_value > 0xFFFF:
        raise PsxNativeAssetError(
            f"{source}: face {face_index} has a non-narrowable selector")
    fixed_uvs = struct.unpack_from("<8i", record, 24)
    fixed_shades = struct.unpack_from("<4I", record, 60)
    compatibility_components = []
    for component in fixed_uvs:
        compatibility_components.append(_narrow_psw_fixed_byte(
            component, source=source, face_index=face_index,
            field="UV coordinate", allow_wrapped_minus_one=True))
        try:
            psw_material_local_uv_quotient(component)
        except PsxPswPipelineError as exc:
            raise PsxNativeAssetError(
                f"{source}: face {face_index} has an invalid PSW "
                f"material-local UV: {exc}") from exc
    uvs = tuple(
        (compatibility_components[corner * 2],
         compatibility_components[corner * 2 + 1])
        for corner in range(4))
    signed_uvs = tuple(
        (fixed_uvs[corner * 2], fixed_uvs[corner * 2 + 1])
        for corner in range(4))
    shades = tuple(
        _narrow_psw_fixed_byte(
            value, source=source, face_index=face_index,
            field="corner shade")
        for value in fixed_shades)
    return (record[:8], tuple(index_values), uvs,
            int(selector_value), shades, signed_uvs)  # type: ignore[return-value]


def parse_psx_mesh_bytes(
        data: bytes, *, logical_path: str,
        archive_ordinal: int | None = None,
        archive_offset: int | None = None) -> PsxNativeMesh:
    """Parse one strict version-1 PSW/PSV or version-3 PW3 body."""

    if archive_offset is not None and (
            archive_offset < 0 or archive_offset % SECTOR_SIZE):
        raise PsxNativeAssetError(
            f"{logical_path}: archive offset {archive_offset} is not "
            f"0x{SECTOR_SIZE:X}-byte sector aligned")
    if archive_ordinal is not None and archive_ordinal < 0:
        raise PsxNativeAssetError(
            f"{logical_path}: archive ordinal may not be negative")

    if len(data) < MESH_HEADER_SIZE:
        raise PsxNativeAssetError(
            f"{logical_path}: truncated {len(data)}-byte mesh header")
    version = _u32(data, 0)
    if version not in _SUPPORTED_MESH_VERSIONS:
        raise PsxNativeAssetError(
            f"{logical_path}: unsupported native mesh version {version}")
    vertex_count = _u32(data, 0x38)
    face_count = _u32(data, 0x3C)
    vertex_offset = _u32(data, 0x40)
    face_offset = _u32(data, 0x44)
    if not 1 <= vertex_count <= 10000:
        raise PsxNativeAssetError(
            f"{logical_path}: invalid vertex count {vertex_count}")
    if not 1 <= face_count <= 10000:
        raise PsxNativeAssetError(
            f"{logical_path}: invalid face count {face_count}")
    expected_face_offset = MESH_HEADER_SIZE + vertex_count * VERTEX_SIZE
    if vertex_offset != MESH_HEADER_SIZE:
        raise PsxNativeAssetError(
            f"{logical_path}: vertex stream starts at {vertex_offset}, "
            f"expected {MESH_HEADER_SIZE}")
    if face_offset != expected_face_offset:
        raise PsxNativeAssetError(
            f"{logical_path}: face stream starts at {face_offset}, "
            f"expected {expected_face_offset}")
    face_size = PW3_FACE_SIZE if version == 3 else PSW_FACE_SIZE
    unaligned_size = face_offset + face_count * face_size
    body_size = (unaligned_size + 3) & ~3
    if body_size > len(data):
        raise PsxNativeAssetError(
            f"{logical_path}: mesh requires {body_size} bytes but source "
            f"contains {len(data)}")
    if body_size != len(data):
        raise PsxNativeAssetError(
            f"{logical_path}: mesh source contains {len(data)} bytes; "
            f"expected exact body size {body_size} (trailing bytes are not "
            "part of the validated mesh format)")
    alignment = data[unaligned_size:body_size]
    if len(alignment) > 3:
        raise AssertionError("mesh alignment exceeded three bytes")

    vertex_stream = data[vertex_offset:face_offset]
    raw_vertices = tuple(
        struct.unpack_from("<iii", vertex_stream, index * VERTEX_SIZE)
        for index in range(vertex_count)
    )
    vertices = tuple(
        tuple(component / FIXED_ONE for component in vertex)
        for vertex in raw_vertices
    )

    faces: list[PsxNativeFace] = []
    for face_index in range(face_count):
        source_offset = face_offset + face_index * face_size
        record = data[source_offset:source_offset + face_size]
        if version == 3:
            opaque, indices4, uvs4, selector, shades4 = (
                _decode_pw3_face(record))
            psw_uv_fixed_16_16 = ()
        else:
            opaque, indices4, uvs4, selector, shades4, \
                psw_uv_fixed_16_16 = _decode_psw_face(
                    record, source=logical_path, face_index=face_index)
        if any(index >= vertex_count for index in indices4):
            bad = next(index for index in indices4 if index >= vertex_count)
            raise PsxNativeAssetError(
                f"{logical_path}: face {face_index} vertex index {bad} is "
                f"outside 0..{vertex_count - 1}")
        collapsed_indices, collapsed_uvs, collapsed_shades = (
            _collapse_repeated_corners(
                indices4, uvs4, shades4,
                source=logical_path, face_index=face_index))
        if version == 3:
            # PW3 rendering consumes the four raw slots below, while the
            # compatibility fields retain their established unique-corner
            # topology view.
            pw3_primitive_flags = struct.unpack_from("<H", opaque, 0)[0]
        else:
            # December and March executable evidence proves that the legacy
            # packet path submits authored slots 1,0,2,3 and unconditionally
            # accepts only a strict-positive NCLIP result.  That policy is not
            # encoded by PW3 bit 14, so keep this PW3-specific field empty and
            # continue preserving both PSW/PSV prefix words without assigning
            # either one a culling meaning.
            pw3_primitive_flags = None
        faces.append(PsxNativeFace(
            source_offset=source_offset,
            raw_record=record,
            opaque_prefix=opaque,
            vertex_indices=collapsed_indices,
            uv_bytes=collapsed_uvs,
            texture_selector=selector,
            corner_shades=collapsed_shades,
            raw_vertex_indices=indices4,
            raw_uv_bytes=uvs4,
            raw_corner_shades=shades4,
            psw_uv_fixed_16_16=psw_uv_fixed_16_16,
            pw3_primitive_flags=pw3_primitive_flags,
        ))

    body = data[:body_size]
    format_id = "PW3" if version == 3 else "PSW/PSV"
    return PsxNativeMesh(
        logical_path=logical_path.replace("\\", "/"),
        format_id=format_id,
        format_version=version,
        archive_ordinal=archive_ordinal,
        archive_offset=archive_offset,
        archive_sector=(
            archive_offset // SECTOR_SIZE
            if archive_offset is not None else None),
        body_size=body_size,
        body_sha256=_sha256(body),
        vertex_stream_sha256=_sha256(vertex_stream),
        face_stream_sha256=_sha256(data[face_offset:unaligned_size]),
        raw_vertices=raw_vertices,
        vertices=vertices,
        faces=tuple(faces),
    )


def parse_psx_mesh_file(path: Path, *, logical_path: str | None = None) \
        -> PsxNativeMesh:
    source = Path(path)
    _reject_reparse_path(source)
    return parse_psx_mesh_bytes(
        source.read_bytes(), logical_path=logical_path or source.name)


def scan_unit_archive_bytes(
        data: bytes, *, logical_path: str = "UNITMODL/UNIT.BIN") \
        -> tuple[PsxNativeMesh, ...]:
    """Validate and enumerate every sector-aligned PW3 in ``UNIT.BIN``."""

    if not data.startswith(UNIT_ARCHIVE_MAGIC):
        raise PsxNativeAssetError(
            f"{logical_path}: missing UNIT.BIN archive magic 4e0d0a1a")
    if len(data) < SECTOR_SIZE:
        raise PsxNativeAssetError(
            f"{logical_path}: archive is shorter than one sector")
    if len(data) % SECTOR_SIZE:
        raise PsxNativeAssetError(
            f"{logical_path}: archive length {len(data)} is not a whole "
            f"number of 0x{SECTOR_SIZE:X}-byte sectors")
    first_padding = data[4:SECTOR_SIZE]
    if any(byte != 0xBA for byte in first_padding):
        raise PsxNativeAssetError(
            f"{logical_path}: first-sector padding is not uniformly 0xBA")

    meshes: list[PsxNativeMesh] = []
    offset = SECTOR_SIZE
    while offset + MESH_HEADER_SIZE <= len(data):
        sector = data[offset:offset + SECTOR_SIZE]
        # Empty allocations occur in both observed forms: a full 0xBA sector,
        # or the four-byte archive marker followed by 0xBA fill.  They are not
        # model bodies and must not consume an ordinal.
        if sector and (
                all(byte == 0xBA for byte in sector)
                or (sector.startswith(UNIT_ARCHIVE_MAGIC)
                    and all(byte == 0xBA for byte in sector[4:]))):
            offset += SECTOR_SIZE
            continue
        version = _u32(data, offset)
        if version != 3:
            raise PsxNativeAssetError(
                f"{logical_path}: non-padding sector {offset // SECTOR_SIZE} "
                f"starts with unsupported version {version}")
        vertex_count = _u32(data, offset + 0x38)
        face_count = _u32(data, offset + 0x3C)
        face_offset = _u32(data, offset + 0x44)
        if face_offset != MESH_HEADER_SIZE + vertex_count * VERTEX_SIZE:
            raise PsxNativeAssetError(
                f"{logical_path}: malformed PW3 header at 0x{offset:08X}")
        unaligned_size = face_offset + face_count * PW3_FACE_SIZE
        body_size = (unaligned_size + 3) & ~3
        allocation_size = (
            (body_size + SECTOR_SIZE - 1) // SECTOR_SIZE * SECTOR_SIZE)
        end = offset + allocation_size
        if end > len(data):
            raise PsxNativeAssetError(
                f"{logical_path}: PW3 at 0x{offset:08X} exceeds archive")
        mesh = parse_psx_mesh_bytes(
            data[offset:offset + body_size],
            logical_path=logical_path,
            archive_ordinal=len(meshes),
            archive_offset=offset,
        )
        padding = data[offset + body_size:end]
        if any(byte != 0xBA for byte in padding):
            raise PsxNativeAssetError(
                f"{logical_path}: PW3 {len(meshes)} padding at "
                f"0x{offset + body_size:08X} is not uniformly 0xBA")
        meshes.append(mesh)
        offset = end

    if offset < len(data) and any(byte != 0xBA for byte in data[offset:]):
        raise PsxNativeAssetError(
            f"{logical_path}: non-padding trailing archive bytes")
    if not meshes:
        raise PsxNativeAssetError(f"{logical_path}: no PW3 meshes found")
    return tuple(meshes)


def scan_unit_archive_file(path: Path, *, logical_path: str | None = None) \
        -> tuple[PsxNativeMesh, ...]:
    source = Path(path)
    _reject_reparse_path(source)
    return scan_unit_archive_bytes(
        source.read_bytes(), logical_path=logical_path or source.name)


def _casefold_child(directory: Path, name: str) -> Path | None:
    _reject_source_component(directory)
    wanted = name.casefold()
    try:
        children = tuple(directory.iterdir())
    except OSError:
        return None
    matches = tuple(
        child for child in children if child.name.casefold() == wanted)
    if len(matches) > 1:
        names = ", ".join(sorted(
            (child.name for child in matches), key=str.casefold))
        raise PsxNativeAssetError(
            f"{directory}: ambiguous case-insensitive source entry for "
            f"{name!r}: {names}")
    if not matches:
        return None
    _reject_source_component(matches[0])
    return matches[0]


def _logical_path(root: Path, path: Path) -> str:
    root_absolute = Path(os.path.abspath(os.fspath(root)))
    path_absolute = Path(os.path.abspath(os.fspath(path)))
    try:
        relative = path_absolute.relative_to(root_absolute)
    except ValueError as exc:
        raise PsxNativeAssetError(
            f"native PSX source is outside the extracted tree: {path}") \
            from exc

    current = root_absolute
    _reject_source_component(current)
    for part in relative.parts:
        current /= part
        _reject_source_component(current)

    try:
        resolved_root = root_absolute.resolve(strict=True)
        resolved_path = path_absolute.resolve(strict=True)
        resolved_path.relative_to(resolved_root)
    except (OSError, ValueError) as exc:
        raise PsxNativeAssetError(
            f"native PSX source resolves outside the extracted tree: "
            f"{path}") from exc
    return relative.as_posix()


def _parse_vehicle_roster(data: bytes) -> tuple[str, ...]:
    values = []
    for raw in data.split(b"\x00"):
        raw = raw.strip()
        if not raw:
            continue
        try:
            value = raw.decode("ascii")
        except UnicodeDecodeError as exc:
            raise PsxNativeAssetError(
                "LISTS/VEHICLE.TXT contains non-ASCII roster data") from exc
        values.append(value)
    return tuple(values)


def _resolve_boot_executable(root: Path, system_cnf: Path) -> Path:
    text = system_cnf.read_text(encoding="ascii", errors="replace")
    match = _BOOT_RE.search(text)
    if match is None:
        raise PsxNativeAssetError(
            "SYSTEM.CNF does not contain a supported BOOT=cdrom entry")
    logical = match.group(1).replace("\\", "/").strip("/")
    current = root
    for part in logical.split("/"):
        if not part or part in {".", ".."}:
            raise PsxNativeAssetError(
                f"SYSTEM.CNF contains an unsafe executable path: "
                f"{logical!r}")
        child = _casefold_child(current, part)
        if child is None:
            raise PsxNativeAssetError(
                f"SYSTEM.CNF references missing executable {logical!r}")
        current = child
    if not current.is_file():
        raise PsxNativeAssetError(
            f"SYSTEM.CNF boot target is not a file: {logical!r}")
    with current.open("rb") as handle:
        if handle.read(8) != b"PS-X EXE":
            raise PsxNativeAssetError(
                f"SYSTEM.CNF boot target {logical!r} is not a PS-X EXE")
    return current


def is_extracted_psx_build_root(path: Path) -> bool:
    source = Path(path)
    if _is_reparse_point(source):
        return False
    system_cnf = _casefold_child(source, "SYSTEM.CNF")
    unit_dir = _casefold_child(source, "UNITMODL")
    return bool(
        source.is_dir()
        and system_cnf is not None
        and system_cnf.is_file()
        and unit_dir is not None
        and unit_dir.is_dir()
    )


def find_extracted_psx_build_root(path: Path) -> Path:
    """Resolve one explicit extracted tree without scanning arbitrary drives."""

    _reject_reparse_path(Path(path))
    selected = Path(path).resolve()
    current = selected if selected.is_dir() else selected.parent
    for candidate in (current, *current.parents):
        if is_extracted_psx_build_root(candidate):
            return candidate
    if not selected.is_dir():
        raise PsxNativeAssetError(
            "the selected path is not inside an extracted PSX disc tree")
    found = discover_extracted_psx_builds(selected, max_depth=4)
    if not found:
        raise PsxNativeAssetError(
            "no extracted Urban Assault PSX disc tree was found under the "
            "selected folder")
    if len(found) > 1:
        raise PsxNativeAssetError(
            f"the selected folder contains {len(found)} PSX disc trees; "
            "choose one extracted build root")
    return found[0]


def discover_extracted_psx_builds(
        root: Path, *, max_depth: int = 4) -> tuple[Path, ...]:
    """Find build roots only beneath a user-approved directory.

    Reparse points/symlinks are not followed, which keeps discovery bounded to
    the user's explicit source library and prevents directory cycles.
    """

    _reject_reparse_path(Path(root))
    approved = Path(root).resolve()
    if not approved.is_dir():
        return ()
    found: list[Path] = []
    for current_text, dirnames, _filenames in os.walk(
            approved, topdown=True, followlinks=False):
        current = Path(current_text)
        try:
            depth = len(current.relative_to(approved).parts)
        except ValueError:
            dirnames[:] = []
            continue
        dirnames[:] = [
            name for name in dirnames
            if not _is_reparse_point(current / name)
        ]
        if depth >= max_depth:
            dirnames[:] = []
        try:
            if is_extracted_psx_build_root(current):
                found.append(current)
                dirnames[:] = []
        except PsxNativeAssetError:
            # An ambiguous case-insensitive source tree is not a build root,
            # and discovery must not choose one of its entries arbitrarily.
            dirnames[:] = []
    return tuple(sorted(set(found), key=lambda item: str(item).casefold()))


def _load_immediate_pv2_effects(
        root: Path, directories: Iterable[Path]) -> tuple[PsxPv2Mesh, ...]:
    """Load only proven-size PV2 files immediately inside approved folders."""

    effects: list[PsxPv2Mesh] = []
    for directory in directories:
        if not directory.is_dir():
            continue
        names_by_casefold: dict[str, list[Path]] = {}
        for candidate in directory.iterdir():
            if candidate.suffix.casefold() != ".pv2":
                continue
            _reject_source_component(candidate)
            if candidate.is_file():
                names_by_casefold.setdefault(
                    candidate.name.casefold(), []).append(candidate)
        ambiguous = next((
            values for values in names_by_casefold.values()
            if len(values) > 1), None)
        if ambiguous is not None:
            names = ", ".join(sorted(
                (candidate.name for candidate in ambiguous),
                key=str.casefold))
            raise PsxNativeAssetError(
                f"{_logical_path(root, directory)} contains ambiguous "
                f"case-insensitive PV2 names: {names}")

        for candidate in (
                values[0] for values in names_by_casefold.values()):
            if candidate.stat().st_size != PV2_BODY_SIZE:
                continue
            logical_path = _logical_path(root, candidate)
            try:
                effect = parse_recovered_pv2_candidate_bytes(
                    candidate.read_bytes(), logical_path=logical_path)
            except PsxNativeEffectError as exc:
                raise PsxNativeAssetError(
                    f"native PV2 validation failed: {exc}") from exc
            if effect is None:
                raise PsxNativeAssetError(
                    f"{logical_path}: recovered-size PV2 candidate changed "
                    "size while it was being read")
            effects.append(effect)

    return tuple(sorted(
        effects,
        key=lambda effect: (
            effect.logical_path.casefold(), effect.logical_path),
    ))


def load_extracted_psx_build(path: Path) -> PsxNativeBuild:
    """Open an extracted Urban Assault PSX tree without borrowing files."""

    root = find_extracted_psx_build_root(path)
    system_cnf = _casefold_child(root, "SYSTEM.CNF")
    unit_dir = _casefold_child(root, "UNITMODL")
    assert system_cnf is not None and unit_dir is not None
    boot_executable = _resolve_boot_executable(root, system_cnf)
    boot_executable_data = boot_executable.read_bytes()
    boot_executable_sha = _sha256(boot_executable_data)

    archive = _casefold_child(unit_dir, "UNIT.BIN")
    meshes: list[PsxNativeMesh] = []
    archive_logical = None
    archive_sha = None
    model_slot_evidence = None
    if archive is not None and archive.is_file():
        archive_logical = _logical_path(root, archive)
        archive_data = archive.read_bytes()
        archive_sha = _sha256(archive_data)
        packed_meshes = scan_unit_archive_bytes(
            archive_data, logical_path=archive_logical)

        # ``psx_native_identity`` imports this module's strict mesh parser.
        # Importing it only after this module is initialized avoids a circular
        # module dependency while still requiring the exact bytes selected by
        # SYSTEM.CNF as the authority for model-slot binding.
        from psx_native_identity import (
            PsxNativeIdentityError,
            identify_unit_archive_slots_bytes,
        )

        try:
            archive_identity = identify_unit_archive_slots_bytes(
                archive_data,
                executable_bytes=boot_executable_data,
                logical_path=archive_logical,
            )
        except PsxNativeIdentityError:
            # Archive bytes alone cannot distinguish runtime slots, empty
            # placeholders, or a trailing sentinel.  Preserve dense ordinals
            # and leave the new identity fields explicitly unavailable.
            pass
        else:
            allocations_by_ordinal = {
                allocation.archive_ordinal: allocation
                for allocation in archive_identity.allocations
                if allocation.mesh is not None
            }
            expected_ordinals = set(range(len(packed_meshes)))
            if set(allocations_by_ordinal) != expected_ordinals:
                raise PsxNativeAssetError(
                    f"{archive_logical}: executable-backed identity does not "
                    "cover every dense archive ordinal")
            packed_meshes = tuple(
                replace(
                    mesh,
                    model_slot=(
                        allocations_by_ordinal[
                            mesh.archive_ordinal].model_slot),
                    model_slot_evidence_id=archive_identity.evidence_id,
                )
                for mesh in packed_meshes
            )
            if any(mesh.model_slot is None for mesh in packed_meshes):
                raise PsxNativeAssetError(
                    f"{archive_logical}: a packed mesh lacks a proven model "
                    "slot after executable-table validation")
            sentinel = archive_identity.trailing_sentinel
            model_slot_evidence = PsxNativeModelSlotEvidence(
                evidence_id=archive_identity.evidence_id,
                unit_archive_sha256=archive_identity.source_sha256,
                boot_executable_sha256=archive_identity.executable_sha256,
                executable_table_offset=(
                    archive_identity.executable_table_offset),
                executable_table_entry_count=(
                    archive_identity.executable_table_entry_count),
                executable_table_sha256=(
                    archive_identity.executable_table_sha256),
                empty_model_slots=tuple(
                    allocation.model_slot
                    for allocation in archive_identity.allocations
                    if allocation.is_empty_placeholder
                    and allocation.model_slot is not None
                ),
                trailing_sentinel_archive_sector=(
                    sentinel.archive_sector if sentinel is not None else None),
            )
        meshes.extend(packed_meshes)

    loose_candidates = [
        item for item in unit_dir.iterdir()
        if item.suffix.casefold() in {".psw", ".psv", ".pw3"}
    ]
    names_by_casefold: dict[str, list[Path]] = {}
    for candidate in loose_candidates:
        _reject_source_component(candidate)
        if candidate.is_file():
            names_by_casefold.setdefault(
                candidate.name.casefold(), []).append(candidate)
    ambiguous = next((
        values for values in names_by_casefold.values()
        if len(values) > 1), None)
    if ambiguous is not None:
        names = ", ".join(sorted(
            (candidate.name for candidate in ambiguous), key=str.casefold))
        raise PsxNativeAssetError(
            f"{unit_dir}: ambiguous case-insensitive loose mesh names: "
            f"{names}")

    for candidate in sorted(
            (values[0] for values in names_by_casefold.values()),
            key=lambda item: item.name.casefold()):
        logical = _logical_path(root, candidate)
        meshes.append(parse_psx_mesh_bytes(
            candidate.read_bytes(), logical_path=logical))
    if not meshes:
        raise PsxNativeAssetError(
            "UNITMODL contains neither a supported UNIT.BIN archive nor "
            "loose PSW/PSV/PW3 meshes")

    test_art_dir = _casefold_child(root, "TEST_ART")
    effect_directories = [unit_dir]
    if test_art_dir is not None and test_art_dir.is_dir():
        effect_directories.append(test_art_dir)
    effects = _load_immediate_pv2_effects(root, effect_directories)

    lists_dir = _casefold_child(root, "LISTS")
    roster_path = (
        _casefold_child(lists_dir, "VEHICLE.TXT")
        if lists_dir is not None and lists_dir.is_dir() else None)
    roster = ()
    roster_logical = None
    roster_sha = None
    if roster_path is not None and roster_path.is_file():
        roster_data = roster_path.read_bytes()
        roster = _parse_vehicle_roster(roster_data)
        roster_logical = _logical_path(root, roster_path)
        roster_sha = _sha256(roster_data)

    # Compact June and the three recovered sector-padded sizes are separate,
    # independently validated layouts.  Unknown sizes remain unavailable; a
    # recognized size must parse exactly or the source fails closed.
    texture_packs: list[PsxNativeTexturePack] = []
    gfx_dir = _casefold_child(root, "GFX")
    if gfx_dir is not None and gfx_dir.is_dir():
        for set_number in range(1, 7):
            candidate = _casefold_child(gfx_dir, f"SET{set_number}GFX.BIN")
            if candidate is None or not candidate.is_file():
                continue
            candidate_size = candidate.stat().st_size
            if candidate_size == LATE_SETGFX_SIZE:
                parser = parse_late_setgfx_file
            elif candidate_size in SECTOR_PADDED_OBSERVED_SIZES:
                parser = parse_sector_padded_setgfx_file
            else:
                continue
            try:
                texture_packs.append(parser(
                    candidate, logical_path=_logical_path(root, candidate)))
            except PsxNativeTextureError as exc:
                raise PsxNativeAssetError(
                    f"native texture validation failed: {exc}") from exc

    return PsxNativeBuild(
        root=root,
        system_cnf_logical_path=_logical_path(root, system_cnf),
        system_cnf_sha256=sha256_file(system_cnf),
        boot_executable_logical_path=_logical_path(root, boot_executable),
        boot_executable_sha256=boot_executable_sha,
        unit_archive_logical_path=archive_logical,
        unit_archive_sha256=archive_sha,
        vehicle_roster_logical_path=roster_logical,
        vehicle_roster_sha256=roster_sha,
        vehicle_roster=roster,
        meshes=tuple(meshes),
        texture_packs=tuple(texture_packs),
        model_slot_evidence=model_slot_evidence,
        effects=effects,
    )


def mesh_selector_census(mesh: PsxNativeMesh) -> tuple[tuple[int, int], ...]:
    counts: dict[int, int] = {}
    for face in mesh.faces:
        counts[face.texture_selector] = counts.get(
            face.texture_selector, 0) + 1
    return tuple(sorted(counts.items()))


def mesh_face_prefix_census(
        mesh: PsxNativeMesh) -> tuple[tuple[str, int], ...]:
    """Inventory raw prefixes independently of the narrow PW3 flag decode."""

    counts: dict[str, int] = {}
    for face in mesh.faces:
        value = face.opaque_prefix.hex()
        counts[value] = counts.get(value, 0) + 1
    return tuple(sorted(counts.items()))


def mesh_primitive_cull_census(
        mesh: PsxNativeMesh) -> tuple[tuple[str, int], ...]:
    """Inventory the exact primitive cull policy applied by the viewer.

    PW3 bit 14 is backed by the recovered June executable: clear primitives
    pass only a strict-positive NCLIP result, while set primitives bypass that
    test.  The recovered December and March PSW/PSV packet routines instead
    apply NCLIP unconditionally and reject every non-positive MAC0 result;
    neither preserved legacy prefix word provides a two-sided bypass.
    """

    counts: dict[str, int] = {}
    for face in mesh.faces:
        if face.pw3_two_sided is None:
            policy = "psw_psv_unconditional_nclip_strict_positive"
        elif face.pw3_two_sided:
            policy = "pw3_bit14_set_two_sided"
        else:
            policy = "pw3_bit14_clear_nclip_strict_positive"
        counts[policy] = counts.get(policy, 0) + 1
    return tuple(sorted(counts.items()))


def mesh_corner_shade_census(
        mesh: PsxNativeMesh) -> tuple[tuple[int, int], ...]:
    """Inventory unique-corner shade diagnostics without applying a formula."""

    counts: dict[int, int] = {}
    for face in mesh.faces:
        for value in face.corner_shades:
            counts[value] = counts.get(value, 0) + 1
    return tuple(sorted(counts.items()))


def mesh_raw_corner_shade_census(
        mesh: PsxNativeMesh) -> tuple[tuple[int, int], ...]:
    """Inventory all four immutable authored shade slots per primitive."""

    counts: dict[int, int] = {}
    for face in mesh.faces:
        for value in face.raw_corner_shades:
            counts[value] = counts.get(value, 0) + 1
    return tuple(sorted(counts.items()))


def iter_mesh_points(
        mesh: PsxNativeMesh) -> Iterable[tuple[float, float, float]]:
    return iter(mesh.vertices)
