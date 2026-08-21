"""Evidence-gated identity helpers for native Urban Assault PSX models.

``UNIT.BIN`` is not merely a dense sequence of meshes.  The recovered March,
May, and June executables contain a table of ``(start_sector, body_size)``
pairs.  The game advances a model-pointer slot for every table entry,
including four-byte/one-sector empty placeholders, and later indexes that
pointer table with the model id.  A viewer may therefore retain its historic
dense archive ordinal while also exposing the independently proven model
slot.

The last sector in the May and June archives is a marker which is *not* in
the executable table.  It is a trailing sentinel, not another model slot.
That distinction cannot be made from the archive bytes alone, so this module
requires exact executable-table evidence and fails closed when it is absent
or ambiguous.

Loose names such as ``V56B.PW3`` carry a filename-derived asset number and
variant.  They are not assumed to equal a packed model slot: the December
``V90.PSW`` body matches four packed slots and ``V91.PSW`` is carried at
packed slot 94 in the March build.  Friendly ``VEHICLE.TXT`` names are
deliberately outside this contract.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import re
import stat
import struct

from psx_native_assets import (
    MESH_HEADER_SIZE,
    PW3_FACE_SIZE,
    SECTOR_SIZE,
    UNIT_ARCHIVE_MAGIC,
    VERTEX_SIZE,
    PsxNativeMesh,
    parse_psx_mesh_bytes,
)


PSX_MODEL_SLOT_EVIDENCE_ID = "psx_unit_bin_executable_allocation_table_v1"
PSX_MESH_SEMANTIC_FINGERPRINT_ID = "psx_mesh_normalized_semantics_v1"
PSX_MESH_GEOMETRY_FINGERPRINT_ID = "psx_mesh_normalized_geometry_v1"
PSX_LOOSE_OVERRIDE_EVIDENCE_ID = "psx_executable_loose_model_override_v1"

# The exact June evidence triplet.  The executable loads the 0x1448-byte
# loose body, and the linked OVER1 routine substitutes its pointer only for
# model slot 56 while a coordinate-derived near-view metric is <= 0x1869F.
# The routine appears to compute shifted X delta squared plus twice shifted Z
# delta squared; this is deliberately not described as Euclidean distance.
JUNE_V56B_EXECUTABLE_SHA256 = (
    "ea9def3942ba20077d4c06591dc3acdb85d7641e47d728eeb653267947bae767")
JUNE_V56B_OVERLAY_SHA256 = (
    "8bac11c449e06e4a2b8309d3b44e666bf9b10d6d34208af63227395205e6d5cf")
JUNE_V56B_ASSET_SHA256 = (
    "3bd8e691042ce9670e8104fc4f397c260d1a89ffd96dc9e15697fa2879b0734f")
JUNE_V56B_MAIN_LOAD_FILE_OFFSET = 0x54614
JUNE_V56B_OVERLAY_SELECT_FILE_OFFSET = 0xF738
JUNE_V56B_MODEL_SLOT = 56
JUNE_V56B_NEAR_VIEW_METRIC_MAX = 0x1869F

_SECTOR_PAD_BYTE = 0xBA
_LOOSE_MODEL_RE = re.compile(
    r"^V(?P<number>[1-9][0-9]*)(?P<variant>[A-Z][A-Z0-9_]*)?$",
    re.IGNORECASE,
)
_LOOSE_MESH_EXTENSIONS = frozenset((".psw", ".psv", ".pw3"))


class PsxNativeIdentityError(ValueError):
    """Raised when identity evidence is missing, malformed, or ambiguous."""


def _is_reparse_point(path: Path) -> bool:
    """Classify symlinks, junctions, and other Windows reparse points.

    Missing components are safe for this predicate.  Other filesystem-query
    failures are unsafe because the direct file helper must not guess where
    executable evidence will be read from.
    """

    try:
        if path.is_symlink():
            return True
        is_junction = getattr(path, "is_junction", None)
        if is_junction is not None and is_junction():
            return True
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            return False
        reparse_flag = getattr(
            stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x0400)
        return bool(
            getattr(metadata, "st_file_attributes", 0) & reparse_flag)
    except OSError:
        return True


def _reject_reparse_path(path: Path) -> None:
    """Reject a redirected leaf or ancestor without resolving through it."""

    absolute = Path(os.path.abspath(os.fspath(path)))
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current /= part
        if _is_reparse_point(current):
            raise PsxNativeIdentityError(
                "model-slot evidence path contains a symlink, junction, or "
                f"reparse point: {current}")


@dataclass(frozen=True, slots=True)
class PsxLooseModelOverrideEvidence:
    """One exact executable/overlay-gated loose model override."""

    evidence_id: str
    logical_path: str
    source_sha256: str
    model_slot: int
    selection_kind: str
    near_view_metric: str
    near_view_metric_max_inclusive: int
    executable_sha256: str
    overlay_sha256: str
    main_load_file_offset: int
    overlay_select_file_offset: int


def identify_june_v56b_override(
        *, executable_bytes: bytes, overlay_bytes: bytes,
        asset_bytes: bytes, logical_path: str,
        ) -> PsxLooseModelOverrideEvidence | None:
    """Return the V56B override only for the exact recovered evidence triplet.

    A mismatch returns ``None`` rather than generalizing the binding to an
    unverified executable, overlay, or loose asset revision.
    """

    normalized_path = logical_path.replace("\\", "/")
    if normalized_path.casefold() != "unitmodl/v56b.pw3":
        return None
    if _sha256(executable_bytes) != JUNE_V56B_EXECUTABLE_SHA256:
        return None
    if _sha256(overlay_bytes) != JUNE_V56B_OVERLAY_SHA256:
        return None
    if _sha256(asset_bytes) != JUNE_V56B_ASSET_SHA256:
        return None
    return PsxLooseModelOverrideEvidence(
        evidence_id=PSX_LOOSE_OVERRIDE_EVIDENCE_ID,
        logical_path=normalized_path,
        source_sha256=JUNE_V56B_ASSET_SHA256,
        model_slot=JUNE_V56B_MODEL_SLOT,
        selection_kind="near_view_metric_conditional_model_override",
        near_view_metric=(
            "shifted_x_delta_squared_plus_twice_shifted_z_delta_squared"),
        near_view_metric_max_inclusive=JUNE_V56B_NEAR_VIEW_METRIC_MAX,
        executable_sha256=JUNE_V56B_EXECUTABLE_SHA256,
        overlay_sha256=JUNE_V56B_OVERLAY_SHA256,
        main_load_file_offset=JUNE_V56B_MAIN_LOAD_FILE_OFFSET,
        overlay_select_file_offset=JUNE_V56B_OVERLAY_SELECT_FILE_OFFSET,
    )


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _u32(data: bytes, offset: int) -> int:
    return struct.unpack_from("<I", data, offset)[0]


def _all_sector_pad(data: bytes) -> bool:
    # Empty post-body padding is valid when a model ends exactly at a sector
    # boundary.  Callers which require a nonempty sector already pass one
    # complete 0x800-byte sector.
    return all(byte == _SECTOR_PAD_BYTE for byte in data)


def _is_empty_allocation_sector(sector: bytes) -> bool:
    return (
        _all_sector_pad(sector)
        or (
            sector.startswith(UNIT_ARCHIVE_MAGIC)
            and _all_sector_pad(sector[len(UNIT_ARCHIVE_MAGIC):])
        )
    )


def _find_all(data: bytes, needle: bytes) -> tuple[int, ...]:
    positions: list[int] = []
    start = 0
    while True:
        position = data.find(needle, start)
        if position < 0:
            break
        positions.append(position)
        start = position + 1
    return tuple(positions)


@dataclass(frozen=True, slots=True)
class PsxLooseModelIdentity:
    """Identity carried by a strict loose ``V<number><variant>`` filename."""

    logical_path: str
    asset_number: int
    variant: str | None
    extension: str
    evidence_basis: str = "loose_filename_v_numeric"

    @property
    def asset_key(self) -> str:
        suffix = self.variant or ""
        return f"V{self.asset_number}{suffix}"


def loose_model_identity(logical_path: str) -> PsxLooseModelIdentity | None:
    """Return strict filename evidence, or ``None`` for a non-``V`` asset.

    The returned number is an *asset number*.  It is intentionally not named
    ``model_slot`` because recovered counterexamples prove the two namespaces
    are not universally interchangeable.
    """

    if not isinstance(logical_path, str):
        raise PsxNativeIdentityError("logical path must be a string")
    normalized = logical_path.replace("\\", "/")
    if not normalized or "\0" in normalized:
        raise PsxNativeIdentityError("logical path is empty or contains NUL")
    parts = normalized.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise PsxNativeIdentityError(
            f"logical path is not a normalized relative path: {logical_path!r}")
    filename = parts[-1]
    suffix = Path(filename).suffix.casefold()
    if suffix not in _LOOSE_MESH_EXTENSIONS:
        return None
    stem = filename[:-len(suffix)]
    match = _LOOSE_MODEL_RE.fullmatch(stem)
    if match is None:
        return None
    number = int(match.group("number"), 10)
    if number > 0x7FFFFFFF:
        raise PsxNativeIdentityError(
            f"loose asset number {number} exceeds the supported integer range")
    variant_text = match.group("variant")
    return PsxLooseModelIdentity(
        logical_path=normalized,
        asset_number=number,
        variant=(variant_text.upper() if variant_text else None),
        extension=suffix.upper(),
    )


def require_loose_model_identity(logical_path: str) -> PsxLooseModelIdentity:
    """Return filename evidence or reject a path without that evidence."""

    identity = loose_model_identity(logical_path)
    if identity is None:
        raise PsxNativeIdentityError(
            f"{logical_path!r} is not a strict V<number><variant> PSX mesh")
    return identity


@dataclass(frozen=True, slots=True)
class PsxUnitAllocationIdentity:
    """One executable-validated ``UNIT.BIN`` allocation-table entry."""

    allocation_index: int
    model_slot: int | None
    archive_sector: int
    archive_offset: int
    body_size: int
    allocation_size: int
    is_empty_placeholder: bool
    is_proven_trailing_sentinel: bool
    archive_ordinal: int | None
    mesh: PsxNativeMesh | None

    @property
    def kind(self) -> str:
        if self.is_proven_trailing_sentinel:
            return "proven_trailing_sentinel"
        if self.is_empty_placeholder:
            return "empty_model_slot"
        return "mesh_model_slot"


@dataclass(frozen=True, slots=True)
class PsxUnitArchiveIdentity:
    """A ``UNIT.BIN`` whose model slots are proven by an executable table."""

    logical_path: str
    source_sha256: str
    executable_sha256: str
    evidence_id: str
    executable_table_offset: int
    executable_table_entry_count: int
    executable_table_sha256: str
    allocations: tuple[PsxUnitAllocationIdentity, ...]

    @property
    def meshes(self) -> tuple[PsxNativeMesh, ...]:
        return tuple(
            allocation.mesh
            for allocation in self.allocations
            if allocation.mesh is not None
        )

    @property
    def model_slots(self) -> tuple[int, ...]:
        return tuple(
            allocation.model_slot
            for allocation in self.allocations
            if allocation.mesh is not None
            and allocation.model_slot is not None
        )

    @property
    def trailing_sentinel(self) -> PsxUnitAllocationIdentity | None:
        sentinels = tuple(
            allocation for allocation in self.allocations
            if allocation.is_proven_trailing_sentinel
        )
        if len(sentinels) > 1:
            raise AssertionError("more than one trailing sentinel was classified")
        return sentinels[0] if sentinels else None

    def allocation_for_model_slot(
            self, model_slot: int) -> PsxUnitAllocationIdentity:
        if type(model_slot) is not int or model_slot < 0:
            raise PsxNativeIdentityError(
                "model slot must be a non-negative integer")
        matches = tuple(
            allocation for allocation in self.allocations
            if allocation.model_slot == model_slot
        )
        if len(matches) != 1:
            raise PsxNativeIdentityError(
                f"model slot {model_slot} has {len(matches)} allocation entries")
        return matches[0]

    def mesh_for_model_slot(self, model_slot: int) -> PsxNativeMesh:
        allocation = self.allocation_for_model_slot(model_slot)
        if allocation.mesh is None:
            raise PsxNativeIdentityError(
                f"model slot {model_slot} is an empty placeholder")
        return allocation.mesh


@dataclass(frozen=True, slots=True)
class _RawAllocation:
    allocation_index: int
    archive_sector: int
    archive_offset: int
    body_size: int
    allocation_size: int
    is_empty: bool
    archive_ordinal: int | None
    mesh: PsxNativeMesh | None


def _scan_raw_allocations(
        data: bytes, *, logical_path: str) -> tuple[_RawAllocation, ...]:
    """Parse allocations without assigning model slots or sentinels."""

    if not data.startswith(UNIT_ARCHIVE_MAGIC):
        raise PsxNativeIdentityError(
            f"{logical_path}: missing UNIT.BIN archive magic 4e0d0a1a")
    if len(data) < SECTOR_SIZE or len(data) % SECTOR_SIZE:
        raise PsxNativeIdentityError(
            f"{logical_path}: archive must contain whole 0x{SECTOR_SIZE:X}-byte "
            "sectors")
    if not _all_sector_pad(data[len(UNIT_ARCHIVE_MAGIC):SECTOR_SIZE]):
        raise PsxNativeIdentityError(
            f"{logical_path}: first-sector padding is not uniformly 0xBA")

    allocations: list[_RawAllocation] = []
    offset = 0
    dense_ordinal = 0
    while offset < len(data):
        allocation_index = len(allocations)
        sector = data[offset:offset + SECTOR_SIZE]
        if _is_empty_allocation_sector(sector):
            allocations.append(_RawAllocation(
                allocation_index=allocation_index,
                archive_sector=offset // SECTOR_SIZE,
                archive_offset=offset,
                body_size=len(UNIT_ARCHIVE_MAGIC),
                allocation_size=SECTOR_SIZE,
                is_empty=True,
                archive_ordinal=None,
                mesh=None,
            ))
            offset += SECTOR_SIZE
            continue

        if len(data) - offset < MESH_HEADER_SIZE:
            raise PsxNativeIdentityError(
                f"{logical_path}: truncated model header at 0x{offset:08X}")
        version = _u32(data, offset)
        if version != 3:
            raise PsxNativeIdentityError(
                f"{logical_path}: allocation {allocation_index} starts with "
                f"unsupported model version {version}")
        vertex_count = _u32(data, offset + 0x38)
        face_count = _u32(data, offset + 0x3C)
        face_offset = _u32(data, offset + 0x44)
        if not 1 <= vertex_count <= 10000 or not 1 <= face_count <= 10000:
            raise PsxNativeIdentityError(
                f"{logical_path}: invalid model counts at 0x{offset:08X}")
        expected_face_offset = MESH_HEADER_SIZE + vertex_count * VERTEX_SIZE
        if face_offset != expected_face_offset:
            raise PsxNativeIdentityError(
                f"{logical_path}: malformed model offsets at 0x{offset:08X}")
        unaligned_size = face_offset + face_count * PW3_FACE_SIZE
        body_size = (unaligned_size + 3) & ~3
        allocation_size = (
            (body_size + SECTOR_SIZE - 1) // SECTOR_SIZE * SECTOR_SIZE)
        end = offset + allocation_size
        if end > len(data):
            raise PsxNativeIdentityError(
                f"{logical_path}: allocation {allocation_index} exceeds archive")
        try:
            mesh = parse_psx_mesh_bytes(
                data[offset:offset + body_size],
                logical_path=logical_path,
                archive_ordinal=dense_ordinal,
                archive_offset=offset,
            )
        except ValueError as exc:
            raise PsxNativeIdentityError(str(exc)) from exc
        padding = data[offset + body_size:end]
        if not _all_sector_pad(padding):
            raise PsxNativeIdentityError(
                f"{logical_path}: allocation {allocation_index} padding is "
                "not uniformly 0xBA")
        allocations.append(_RawAllocation(
            allocation_index=allocation_index,
            archive_sector=offset // SECTOR_SIZE,
            archive_offset=offset,
            body_size=body_size,
            allocation_size=allocation_size,
            is_empty=False,
            archive_ordinal=dense_ordinal,
            mesh=mesh,
        ))
        dense_ordinal += 1
        offset = end

    if not any(allocation.mesh is not None for allocation in allocations):
        raise PsxNativeIdentityError(f"{logical_path}: no PW3 model bodies found")
    return tuple(allocations)


def _allocation_table_bytes(
        allocations: Iterable[_RawAllocation]) -> bytes:
    return b"".join(
        struct.pack("<II", allocation.archive_sector, allocation.body_size)
        for allocation in allocations
    )


def identify_unit_archive_slots_bytes(
        data: bytes, *, executable_bytes: bytes,
        logical_path: str = "UNITMODL/UNIT.BIN") -> PsxUnitArchiveIdentity:
    """Assign model slots only after exact executable-table validation.

    The longest admissible table is selected: either every archive
    allocation, or every allocation except one final empty marker.  The table
    must occur exactly once at a four-byte-aligned executable offset.  No
    archive-only heuristic is used to call the last marker a sentinel.
    """

    if not executable_bytes.startswith(b"PS-X EXE"):
        raise PsxNativeIdentityError(
            "model-slot evidence must be supplied by a PS-X EXE image")
    allocations = _scan_raw_allocations(data, logical_path=logical_path)

    candidate_counts = [len(allocations)]
    if len(allocations) > 1 and allocations[-1].is_empty:
        candidate_counts.append(len(allocations) - 1)

    candidates: list[tuple[int, int, bytes]] = []
    ambiguous_counts: list[int] = []
    for entry_count in candidate_counts:
        table = _allocation_table_bytes(allocations[:entry_count])
        positions = tuple(
            position for position in _find_all(executable_bytes, table)
            if position % 4 == 0
        )
        if len(positions) == 1:
            candidates.append((entry_count, positions[0], table))
        elif len(positions) > 1:
            ambiguous_counts.append(entry_count)

    if ambiguous_counts:
        counts = ", ".join(str(count) for count in ambiguous_counts)
        raise PsxNativeIdentityError(
            f"executable allocation-table evidence is ambiguous for "
            f"entry count(s) {counts}")
    if not candidates:
        raise PsxNativeIdentityError(
            "executable does not contain an exact allocation-table sequence; "
            "model slots and trailing-sentinel status remain unproven")

    entry_count, table_offset, table = max(
        candidates, key=lambda candidate: candidate[0])
    if len(allocations) - entry_count not in (0, 1):
        raise AssertionError("unsupported executable/archive allocation delta")
    trailing_sentinel_index = (
        entry_count if entry_count < len(allocations) else None)
    if trailing_sentinel_index is not None:
        sentinel = allocations[trailing_sentinel_index]
        if not sentinel.is_empty or sentinel.allocation_index != len(allocations) - 1:
            raise PsxNativeIdentityError(
                "executable table omits an allocation that is not one final "
                "empty marker")

    identified: list[PsxUnitAllocationIdentity] = []
    for allocation in allocations:
        is_sentinel = allocation.allocation_index == trailing_sentinel_index
        identified.append(PsxUnitAllocationIdentity(
            allocation_index=allocation.allocation_index,
            model_slot=(None if is_sentinel else allocation.allocation_index),
            archive_sector=allocation.archive_sector,
            archive_offset=allocation.archive_offset,
            body_size=allocation.body_size,
            allocation_size=allocation.allocation_size,
            is_empty_placeholder=allocation.is_empty and not is_sentinel,
            is_proven_trailing_sentinel=is_sentinel,
            archive_ordinal=allocation.archive_ordinal,
            mesh=allocation.mesh,
        ))

    return PsxUnitArchiveIdentity(
        logical_path=logical_path.replace("\\", "/"),
        source_sha256=_sha256(data),
        executable_sha256=_sha256(executable_bytes),
        evidence_id=PSX_MODEL_SLOT_EVIDENCE_ID,
        executable_table_offset=table_offset,
        executable_table_entry_count=entry_count,
        executable_table_sha256=_sha256(table),
        allocations=tuple(identified),
    )


def identify_unit_archive_slots_file(
        archive_path: Path, executable_path: Path, *,
        logical_path: str = "UNITMODL/UNIT.BIN") -> PsxUnitArchiveIdentity:
    """Read and validate one archive/executable evidence pair."""

    archive = Path(archive_path)
    executable = Path(executable_path)
    # Check every path component immediately before reading.  A leaf-only
    # ``is_symlink`` check misses parent-directory symlinks and, on Windows,
    # directory junctions and other reparse-point aliases.
    _reject_reparse_path(archive)
    _reject_reparse_path(executable)
    return identify_unit_archive_slots_bytes(
        archive.read_bytes(),
        executable_bytes=executable.read_bytes(),
        logical_path=logical_path,
    )


def _require_raw_face_tuples(mesh: PsxNativeMesh) -> None:
    for face_index, face in enumerate(mesh.faces):
        if len(face.raw_vertex_indices) != 4:
            raise PsxNativeIdentityError(
                f"{mesh.logical_path}: face {face_index} lacks four raw indices")
        if len(face.raw_uv_bytes) != 4:
            raise PsxNativeIdentityError(
                f"{mesh.logical_path}: face {face_index} lacks four raw UVs")
        if len(face.raw_corner_shades) != 4:
            raise PsxNativeIdentityError(
                f"{mesh.logical_path}: face {face_index} lacks four raw shades")


def mesh_semantic_sha256(mesh: PsxNativeMesh) -> str:
    """Hash normalized geometry, UVs, selectors, and corner shades.

    Container version, serialized pointer/header residue, and opaque primitive
    prefixes are excluded.  This is the exact cross-version comparison which
    lets a widened December PSW body corroborate a narrowed packed PW3 body.
    """

    _require_raw_face_tuples(mesh)
    digest = hashlib.sha256()
    digest.update(PSX_MESH_SEMANTIC_FINGERPRINT_ID.encode("ascii") + b"\0")
    digest.update(struct.pack("<II", len(mesh.raw_vertices), len(mesh.faces)))
    for vertex in mesh.raw_vertices:
        digest.update(struct.pack("<iii", *vertex))
    for face in mesh.faces:
        if not 0 <= face.texture_selector <= 0xFFFF:
            raise PsxNativeIdentityError(
                f"{mesh.logical_path}: selector is outside normalized u16 range")
        digest.update(struct.pack("<4H", *face.raw_vertex_indices))
        digest.update(bytes(component for uv in face.raw_uv_bytes for component in uv))
        digest.update(struct.pack("<H", face.texture_selector))
        digest.update(bytes(face.raw_corner_shades))
    return digest.hexdigest()


def mesh_geometry_sha256(mesh: PsxNativeMesh) -> str:
    """Hash only normalized vertices and authored four-corner topology."""

    _require_raw_face_tuples(mesh)
    digest = hashlib.sha256()
    digest.update(PSX_MESH_GEOMETRY_FINGERPRINT_ID.encode("ascii") + b"\0")
    digest.update(struct.pack("<II", len(mesh.raw_vertices), len(mesh.faces)))
    for vertex in mesh.raw_vertices:
        digest.update(struct.pack("<iii", *vertex))
    for face in mesh.faces:
        digest.update(struct.pack("<4H", *face.raw_vertex_indices))
    return digest.hexdigest()


@dataclass(frozen=True, slots=True)
class PsxMeshLineageMatch:
    """Evidence-ranked candidates for one loose/older model body."""

    status: str
    semantic_sha256: str
    geometry_sha256: str
    candidate_model_slots: tuple[int, ...]

    @property
    def proven_model_slot(self) -> int | None:
        if self.status != "exact_semantic_unique":
            return None
        return self.candidate_model_slots[0]


def match_mesh_lineage(
        anchor: PsxNativeMesh,
        candidates_by_model_slot: Mapping[int, PsxNativeMesh],
        ) -> PsxMeshLineageMatch:
    """Rank exact semantic matches above geometry-only similarities.

    Only one exact semantic candidate is exposed as ``proven_model_slot``.
    Geometry-only and collision results remain non-binding negative controls.
    """

    normalized: list[tuple[int, PsxNativeMesh]] = []
    for model_slot, mesh in candidates_by_model_slot.items():
        if type(model_slot) is not int or model_slot < 0:
            raise PsxNativeIdentityError(
                "candidate model slots must be non-negative integers")
        if not isinstance(mesh, PsxNativeMesh):
            raise PsxNativeIdentityError(
                f"candidate for model slot {model_slot} is not a PSX mesh")
        normalized.append((model_slot, mesh))
    normalized.sort(key=lambda item: item[0])

    semantic = mesh_semantic_sha256(anchor)
    geometry = mesh_geometry_sha256(anchor)
    semantic_matches = tuple(
        model_slot for model_slot, mesh in normalized
        if mesh_semantic_sha256(mesh) == semantic
    )
    if semantic_matches:
        status = (
            "exact_semantic_unique"
            if len(semantic_matches) == 1 else "exact_semantic_ambiguous")
        return PsxMeshLineageMatch(
            status=status,
            semantic_sha256=semantic,
            geometry_sha256=geometry,
            candidate_model_slots=semantic_matches,
        )

    geometry_matches = tuple(
        model_slot for model_slot, mesh in normalized
        if mesh_geometry_sha256(mesh) == geometry
    )
    if geometry_matches:
        status = (
            "geometry_only_unique"
            if len(geometry_matches) == 1 else "geometry_only_ambiguous")
    else:
        status = "none"
    return PsxMeshLineageMatch(
        status=status,
        semantic_sha256=semantic,
        geometry_sha256=geometry,
        candidate_model_slots=geometry_matches,
    )
