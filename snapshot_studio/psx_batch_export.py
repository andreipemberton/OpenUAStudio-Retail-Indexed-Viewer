"""Deterministic native-PlayStation mesh snapshot batch export.

This module is deliberately independent from :mod:`snapshot_studio.batch_export`.
The latter enumerates PC ``SET.BAS``/VISPROTO resources; this exporter accepts
only a frozen :class:`~psx_native_assets.PsxNativeBuild` and renders its native
PSW/PSV/PW3 mesh inventory through ``AssetViewport.load_psx_mesh``.

The public step API renders at most one image per call.  A Qt controller can
therefore drive ``step()`` from ``QTimer.singleShot`` and call
``request_cancel()`` between images without moving QWidget/QPainter work to a
worker thread.  ``run()`` is a synchronous convenience for tests and scripts.

Every committed PNG has a ``.png.json`` sidecar.  The JSON is the commit proof:
it records only sanitized logical source paths and hashes, the exact native
renderer profile, mesh/texture selection, deterministic camera preset, and the
PNG hash.  Skip-existing accepts a pair only when that complete identity and
the current PNG bytes match; partial or mismatched pairs fail closed.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import stat
import tempfile
from typing import Literal

from PySide6.QtCore import QSize
from PySide6.QtGui import QColor, QImage, QImageReader, QImageWriter

from assembly_viewer import (
    AssetViewport,
    PSX_NATIVE_PROFILE_ID,
    PSX_NATIVE_PROFILE_INFO,
    PSX_NATIVE_PROFILE_VERSION,
    PSX_NATIVE_SOURCE_PIPELINE,
    PSX_NATIVE_VIEW_MODE,
    VIEW_PRESET_ANGLES,
)
from psx_native_assets import (
    PSX_NATIVE_PARSER_ID,
    PSX_NATIVE_PARSER_VERSION,
    PsxNativeBuild,
    PsxNativeMesh,
    mesh_corner_shade_census,
    mesh_face_prefix_census,
    mesh_primitive_cull_census,
    mesh_raw_corner_shade_census,
    mesh_selector_census,
)
from psx_native_contract import (
    PsxNativeContractError,
    validate_psx_native_build,
)
from psx_native_textures import PsxNativeTexturePack


PSX_NATIVE_SNAPSHOT_SCHEMA = "openuastudio.psx_native_snapshot"
PSX_NATIVE_SNAPSHOT_SCHEMA_VERSION = 3
PSX_NATIVE_BATCH_PROFILE_ID = "psx_native_mesh_batch_v2"
PSX_NATIVE_MANUAL_PROFILE_ID = "psx_native_manual_snapshot_v2"
PSX_NATIVE_BATCH_MANIFEST_SCHEMA = "openuastudio.psx_native_batch_manifest"
PSX_NATIVE_BATCH_MANIFEST_SCHEMA_VERSION = 3
PSX_NATIVE_BATCH_MANIFEST_FILENAME = "psx_native_batch_manifest.json"
# Keep final and transient paths below the conservative Win32 directory/file
# boundary used by the Qt/Python runtime shipped with this Windows project.
# Atomic temporary basenames are intentionally short, so a valid 241-character
# final sidecar does not become a 262-character staging path.
_WINDOWS_SAFE_PATH_LIMIT = 248
_TEMPORARY_PREFIX = ".psxn-"
DEFAULT_PSX_NATIVE_BATCH_VIEWS = tuple(VIEW_PRESET_ANGLES)

_HEX_256 = re.compile(r"^[0-9a-fA-F]{64}$")
_UNSAFE_COMPONENT = re.compile(r"[^A-Za-z0-9_.-]+")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_STABLE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$")
_SIDECAR_LIMIT = 4 * 1024 * 1024
_MAX_DIMENSION = 8192
_MAX_PIXELS = 64 * 1024 * 1024


class PsxNativeBatchError(RuntimeError):
    """Base class for native mesh batch failures."""


class PsxNativeBatchCollisionError(PsxNativeBatchError):
    """An existing PNG/JSON pair cannot prove the requested identity."""


class PsxNativeBatchProvenanceError(PsxNativeBatchError):
    """The completed renderer pass lacks exact native-source proof."""


class PsxNativeAtomicWriteError(PsxNativeBatchError):
    """A staged PNG/JSON pair could not be committed or rolled back."""


def _require_frozen_build_contract(build: PsxNativeBuild) -> None:
    """Translate the shared native-object contract to the exporter API."""

    try:
        validate_psx_native_build(build)
    except PsxNativeContractError as exc:
        raise PsxNativeBatchProvenanceError(
            f"frozen native PSX build violates its provenance contract: "
            f"{exc}") from exc


@dataclass(frozen=True)
class NativeSnapshotPair:
    """One committed or independently verified PNG/JSON pair."""

    png_path: Path
    json_path: Path
    png_sha256: str
    json_sha256: str
    png_size_bytes: int


@dataclass(frozen=True)
class PsxNativeBatchConfig:
    """Frozen output-affecting options for one complete native batch."""

    output_root: Path
    width: int = 1024
    height: int = 1024
    zoom_percent: int = 100
    views: tuple[str, ...] = DEFAULT_PSX_NATIVE_BATCH_VIEWS
    texture_pack: PsxNativeTexturePack | None = None
    background_rgba: tuple[int, int, int, int] | None = None
    skip_existing: bool = True


@dataclass(frozen=True)
class PsxNativeBatchRecord:
    """Result for one mesh/view output target."""

    mesh_index: int
    model_slot: int | None
    archive_ordinal: int | None
    native_asset_path: str
    view_name: str
    relative_png: str
    relative_json: str
    status: Literal["WRITTEN", "SKIPPED_VERIFIED"]
    png_sha256: str
    json_sha256: str
    png_size_bytes: int


@dataclass(frozen=True)
class PsxNativeBatchProgress:
    """Small UI-facing snapshot of exporter state."""

    state: Literal["idle", "running", "complete", "cancelled", "failed"]
    total: int
    completed: int
    written: int
    skipped_verified: int
    cancelled: bool
    current_relative_png: str | None


@dataclass(frozen=True)
class PsxNativeBatchResult:
    """Terminal native batch result, ordered exactly like the frozen plan."""

    cancelled: bool
    total: int
    written: int
    skipped_verified: int
    records: tuple[PsxNativeBatchRecord, ...]
    manifest_path: Path
    manifest_sha256: str


@dataclass(frozen=True)
class _NativeBatchJob:
    key: tuple[int, int]
    mesh_index: int
    view_index: int
    mesh: PsxNativeMesh
    view_name: str
    view_angles: tuple[float, float]
    png_path: Path
    identity: dict[str, object]


def _sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def _json_clone(value, *, label: str):
    try:
        encoded = json.dumps(
            value, ensure_ascii=True, sort_keys=True,
            separators=(",", ":"), allow_nan=False)
        return json.loads(encoded)
    except (TypeError, ValueError) as exc:
        raise PsxNativeBatchProvenanceError(
            f"{label} is not finite JSON data: {exc}") from exc


def _hash256(value, *, label: str, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str) or _HEX_256.fullmatch(value) is None:
        raise PsxNativeBatchProvenanceError(
            f"{label} must be a 64-digit SHA-256 value")
    return value.lower()


def _logical_path(value, *, label: str, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str) or not value:
        raise PsxNativeBatchProvenanceError(
            f"{label} must be a non-empty logical path")
    normalized = value.replace("\\", "/")
    candidate = PurePosixPath(normalized)
    if candidate.is_absolute() or not candidate.parts:
        raise PsxNativeBatchProvenanceError(
            f"{label} must be relative to the selected PSX source")
    for part in candidate.parts:
        if part in {"", ".", ".."} or _CONTROL.search(part):
            raise PsxNativeBatchProvenanceError(
                f"{label} contains an unsafe path component")
        # A drive-like component is never a valid extracted-disc logical path.
        if ":" in part:
            raise PsxNativeBatchProvenanceError(
                f"{label} contains a drive or URI component")
    return "/".join(candidate.parts)


def _safe_component(value: str, fallback: str, *, limit: int = 72) -> str:
    text = _UNSAFE_COMPONENT.sub("_", str(value)).strip(" ._")
    text = text[:limit].rstrip(" ._")
    return text or fallback


def _plain_int(value, *, label: str, minimum: int | None = None,
               maximum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise PsxNativeBatchError(f"{label} must be an integer")
    if minimum is not None and value < minimum:
        raise PsxNativeBatchError(f"{label} must be at least {minimum}")
    if maximum is not None and value > maximum:
        raise PsxNativeBatchError(f"{label} must be at most {maximum}")
    return value


def _plain_text(value, *, label: str) -> str:
    if not isinstance(value, str) or not value or _CONTROL.search(value):
        raise PsxNativeBatchProvenanceError(
            f"{label} must be non-empty safe text")
    return value


def _stable_id(value, *, label: str) -> str:
    if not isinstance(value, str) or _STABLE_ID.fullmatch(value) is None:
        raise PsxNativeBatchProvenanceError(
            f"{label} must be a stable ASCII identifier")
    return value


def _finite_float(value, *, label: str, positive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PsxNativeBatchProvenanceError(
            f"{label} must be a finite number")
    result = float(value)
    if not math.isfinite(result) or (positive and result <= 0.0):
        qualifier = "positive finite" if positive else "finite"
        raise PsxNativeBatchProvenanceError(
            f"{label} must be {qualifier}")
    return result


def _finite_vector(value, *, label: str, length: int) -> list[float]:
    if not isinstance(value, (list, tuple)) or len(value) != length:
        raise PsxNativeBatchProvenanceError(
            f"{label} must contain exactly {length} finite numbers")
    return [
        _finite_float(item, label=f"{label}[{index}]")
        for index, item in enumerate(value)
    ]


def _normalize_camera_state(
        camera_state: Mapping[str, object] | None) -> dict[str, object] | None:
    if camera_state is None:
        return None
    if not isinstance(camera_state, Mapping):
        raise PsxNativeBatchProvenanceError(
            "camera_state must be a mapping")
    missing = {
        "yaw", "pitch", "zoom", "pan", "center", "scale"
    } - set(camera_state)
    if missing:
        raise PsxNativeBatchProvenanceError(
            "camera_state lacks " + ", ".join(sorted(missing)))
    return {
        "yaw": _finite_float(camera_state["yaw"], label="camera yaw"),
        "pitch": _finite_float(
            camera_state["pitch"], label="camera pitch"),
        "zoom": _finite_float(
            camera_state["zoom"], label="camera zoom", positive=True),
        "pan": _finite_vector(
            camera_state["pan"], label="camera pan", length=2),
        "center": _finite_vector(
            camera_state["center"], label="camera center", length=3),
        "scale": _finite_float(
            camera_state["scale"], label="camera scale", positive=True),
    }


def _normalize_background(
        value: tuple[int, int, int, int] | None) \
        -> tuple[int, int, int, int] | None:
    if value is None:
        return None
    if not isinstance(value, tuple) or len(value) != 4:
        raise PsxNativeBatchError(
            "background_rgba must be None or a four-integer tuple")
    return tuple(
        _plain_int(channel, label="background channel", minimum=0, maximum=255)
        for channel in value
    )


def _is_reparse_point(path: Path) -> bool:
    """Classify symlinks, junctions, and other Windows reparse points."""

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
        # An uninspectable path component is not a safe commit target.
        return True


def _reject_reparse_ancestry(path: Path, *, label: str) -> None:
    """Reject redirection in any existing component without following it."""

    absolute = Path(os.path.abspath(os.fspath(path)))
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current /= part
        if _is_reparse_point(current):
            raise PsxNativeBatchError(
                f"{label} contains a symlink, junction, or reparse point: "
                f"{current}")


def validate_native_output_commit_boundary(
        source_root: Path, output_path: Path) -> tuple[Path, Path]:
    """Revalidate one native output immediately before filesystem commit.

    Constructor/UI preflight is intentionally not treated as a durable proof:
    a previously absent output component can be replaced by a symlink or
    Windows junction while an image is rendering.  Inspect every component
    without following it, then resolve both paths afresh and fail closed if the
    destination now aliases the immutable extracted-disc source tree.
    """

    try:
        source = Path(os.path.abspath(os.fspath(source_root)))
        output = Path(os.path.abspath(os.fspath(output_path)))
        _reject_reparse_ancestry(
            source, label="native PlayStation source path")
        _reject_reparse_ancestry(
            output, label="native snapshot output path")
        resolved_source = source.resolve(strict=False)
        resolved_output = output.resolve(strict=False)
    except PsxNativeBatchError:
        raise
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise PsxNativeBatchError(
            "could not revalidate the native output/source boundary; no "
            "output was committed") from exc
    try:
        resolved_output.relative_to(resolved_source)
    except ValueError:
        return resolved_source, resolved_output
    raise PsxNativeBatchError(
        "native snapshot output must remain outside the extracted "
        "PlayStation source tree")


def _resolve_output_root_outside_source(
        build: PsxNativeBuild, output_root: Path) -> Path:
    """Resolve and enforce the native source-tree preservation boundary.

    The UI performs the same check for an immediate operator-facing error, but
    the exporter is also a public API.  Enforce the boundary again here before
    planning jobs or creating directories so direct callers cannot place PNG,
    JSON, manifest, staging, or rollback files in the extracted disc tree.
    Resolving both sides also closes lexical ``..`` aliases and existing
    symlink/junction aliases.  Resolution failures are unsafe and fail closed.
    """

    try:
        resolved_output = Path(output_root).expanduser().resolve(strict=False)
        resolved_source = Path(build.root).expanduser().resolve(strict=False)
    except (OSError, RuntimeError, ValueError) as exc:
        raise PsxNativeBatchError(
            "could not resolve the native batch output/source boundary; "
            "no output was created") from exc
    try:
        resolved_output.relative_to(resolved_source)
    except ValueError:
        return resolved_output
    raise PsxNativeBatchError(
        "native batch output must be outside the extracted PlayStation "
        "source tree")


def _texture_pack_identity(pack: PsxNativeTexturePack) -> dict[str, object]:
    populated = [
        _plain_int(
            value, label="populated native texture selector",
            minimum=0, maximum=127)
        for value in pack.populated_selectors
    ]
    if populated != sorted(set(populated)):
        raise PsxNativeBatchProvenanceError(
            "native texture populated selectors must be unique and ordered")
    material_count = _plain_int(
        pack.material_slot_count,
        label="native texture material slot count",
        minimum=0,
        maximum=128,
    )
    if material_count != len(populated):
        raise PsxNativeBatchProvenanceError(
            "native texture material count does not match its selector census")
    layout_id = _plain_text(
        pack.layout_id, label="native texture-pack layout ID")
    mapping = _plain_text(
        pack.selector_to_pixel_bank_mapping,
        label="native selector-to-pixel-bank mapping")
    return {
        "logical_path": _logical_path(
            pack.logical_path, label="native texture-pack path"),
        "sha256": _hash256(
            pack.source_sha256, label="native texture-pack hash"),
        # ``profile`` is retained because PsxNativeBuild.portable_identity
        # exposes that compatibility key.  It is never a hardcoded late-build
        # claim; the immutable decoded layout is authoritative.
        "profile": layout_id,
        "layout_id": layout_id,
        "selector_to_pixel_bank_mapping": mapping,
        "populated_selector_count": material_count,
        "populated_selectors": populated,
    }


def _model_slot_evidence_identity(
        build: PsxNativeBuild) -> dict[str, object] | None:
    """Sanitize allocation-table proof after the build contract was checked."""

    evidence = build.model_slot_evidence
    if evidence is None:
        for mesh in build.meshes:
            if mesh.model_slot is not None \
                    or mesh.model_slot_evidence_id is not None:
                raise PsxNativeBatchProvenanceError(
                    "native mesh claims a runtime model slot without build "
                    "allocation-table evidence")
        return None
    evidence_id = _stable_id(
        evidence.evidence_id, label="model-slot evidence ID")
    unit_hash = _hash256(
        evidence.unit_archive_sha256,
        label="model-slot evidence UNIT.BIN hash")
    executable_hash = _hash256(
        evidence.boot_executable_sha256,
        label="model-slot evidence executable hash")
    if unit_hash != _hash256(
            build.unit_archive_sha256, label="UNIT.BIN hash", optional=True):
        raise PsxNativeBatchProvenanceError(
            "model-slot evidence does not match the frozen UNIT.BIN hash")
    if executable_hash != _hash256(
            build.boot_executable_sha256, label="boot executable hash"):
        raise PsxNativeBatchProvenanceError(
            "model-slot evidence does not match the frozen executable hash")
    table_offset = _plain_int(
        evidence.executable_table_offset,
        label="model-slot table offset", minimum=0)
    table_count = _plain_int(
        evidence.executable_table_entry_count,
        label="model-slot table entry count", minimum=1)
    empty_slots = [
        _plain_int(slot, label="empty model slot", minimum=0)
        for slot in evidence.empty_model_slots
    ]
    if empty_slots != sorted(set(empty_slots)) \
            or any(slot >= table_count for slot in empty_slots):
        raise PsxNativeBatchProvenanceError(
            "empty model slots must be unique, ordered, and inside the table")
    sentinel_sector = evidence.trailing_sentinel_archive_sector
    if sentinel_sector is not None:
        sentinel_sector = _plain_int(
            sentinel_sector, label="trailing sentinel sector", minimum=0)
    for mesh in build.meshes:
        if mesh.archive_ordinal is None:
            if mesh.model_slot is not None \
                    or mesh.model_slot_evidence_id is not None:
                raise PsxNativeBatchProvenanceError(
                    "loose native mesh cannot inherit a packed model slot")
            continue
        if mesh.model_slot is None \
                or mesh.model_slot_evidence_id != evidence_id:
            raise PsxNativeBatchProvenanceError(
                "packed mesh is missing the build's model-slot evidence")
        slot = _plain_int(
            mesh.model_slot, label="runtime model slot", minimum=0)
        if slot >= table_count or slot in empty_slots:
            raise PsxNativeBatchProvenanceError(
                "packed mesh model slot is outside the proven allocation "
                "table or names an empty slot")
    return {
        "evidence_id": evidence_id,
        "unit_archive_sha256": unit_hash,
        "boot_executable_sha256": executable_hash,
        "executable_table_offset": table_offset,
        "executable_table_entry_count": table_count,
        "executable_table_sha256": _hash256(
            evidence.executable_table_sha256,
            label="model-slot table hash"),
        "empty_model_slots": empty_slots,
        "trailing_sentinel_archive_sector": sentinel_sector,
    }


def _mesh_runtime_identity(
        build: PsxNativeBuild, mesh: PsxNativeMesh) \
        -> tuple[int | None, str | None, str, str]:
    """Return one mesh's validated slot and explicit identity statuses."""

    evidence = _model_slot_evidence_identity(build)
    slot = mesh.model_slot
    evidence_id = mesh.model_slot_evidence_id
    if slot is not None:
        slot = _plain_int(slot, label="runtime model slot", minimum=0)
        if evidence is None or evidence_id != evidence["evidence_id"]:
            raise PsxNativeBatchProvenanceError(
                "mesh model slot is not covered by the frozen build evidence")
        return (
            slot,
            str(evidence_id),
            "executable_allocation_table_proven",
            "model_slot_only_friendly_name_unmapped",
        )
    if evidence_id is not None:
        raise PsxNativeBatchProvenanceError(
            "mesh has a model-slot evidence ID without a model slot")
    if mesh.archive_ordinal is not None:
        return (
            None,
            None,
            "unavailable_unproven",
            "dense_archive_ordinal_only_friendly_name_unmapped",
        )
    return (
        None,
        None,
        "unavailable_unproven",
        "native_filename_only_runtime_identity_unverified",
    )


def _effect_inventory_identity(build: PsxNativeBuild) -> list[dict[str, object]]:
    """Return static PV2 evidence after the build contract was checked."""

    records: list[dict[str, object]] = []
    previous_key: tuple[str, str] | None = None
    for effect in build.effects:
        logical_path = _logical_path(
            effect.logical_path, label="native PV2 logical path")
        key = (logical_path.casefold(), logical_path)
        if previous_key is not None and key <= previous_key:
            raise PsxNativeBatchProvenanceError(
                "native PV2 inventory must be unique and deterministically "
                "ordered")
        previous_key = key
        records.append({
            "logical_path": logical_path,
            "source_sha256": _hash256(
                effect.source_sha256, label="native PV2 source hash"),
            "vertex_stream_sha256": _hash256(
                effect.vertex_stream_sha256,
                label="native PV2 vertex-stream hash"),
            "face_stream_sha256": _hash256(
                effect.face_stream_sha256,
                label="native PV2 face-stream hash"),
            "format_id": _plain_text(
                effect.format_id, label="native PV2 format ID"),
            "format_version": _plain_int(
                effect.format_version, label="native PV2 format version",
                minimum=1),
            "parser_id": _stable_id(
                effect.parser_id, label="native PV2 parser ID"),
            "parser_version": _plain_int(
                effect.parser_version, label="native PV2 parser version",
                minimum=1),
            "binding_status": _plain_text(
                effect.binding_status, label="native PV2 binding status"),
        })
    return records


def _portable_build_renderer_identity(build: PsxNativeBuild) \
        -> dict[str, object]:
    """Reconstruct the viewport's portable build block without ``root``."""

    _require_frozen_build_contract(build)

    model_slot_evidence = _model_slot_evidence_identity(build)
    return {
        "source_container_kind": "extracted_psx_disc_tree",
        "system_cnf_path": _logical_path(
            build.system_cnf_logical_path, label="SYSTEM.CNF path"),
        "system_cnf_sha256": _hash256(
            build.system_cnf_sha256, label="SYSTEM.CNF hash"),
        "boot_executable_path": _logical_path(
            build.boot_executable_logical_path, label="boot executable path"),
        "boot_executable_sha256": _hash256(
            build.boot_executable_sha256, label="boot executable hash"),
        "unit_archive_path": _logical_path(
            build.unit_archive_logical_path, label="UNIT.BIN path",
            optional=True),
        "unit_archive_sha256": _hash256(
            build.unit_archive_sha256, label="UNIT.BIN hash", optional=True),
        "vehicle_roster_path": _logical_path(
            build.vehicle_roster_logical_path, label="VEHICLE.TXT path",
            optional=True),
        "vehicle_roster_sha256": _hash256(
            build.vehicle_roster_sha256, label="VEHICLE.TXT hash",
            optional=True),
        "native_mesh_count": len(build.meshes),
        "name_binding_status": "friendly_name_unmapped_roster",
        "model_slot_binding_status": (
            "executable_allocation_table_proven"
            if model_slot_evidence is not None else "unavailable_unproven"),
        "model_slot_evidence": model_slot_evidence,
        "native_effect_count": len(build.effects),
        "native_effects": _effect_inventory_identity(build),
        "native_texture_packs": [
            _texture_pack_identity(pack)
            for pack in build.texture_packs
        ],
    }


def _portable_source_identity(build: PsxNativeBuild) -> dict[str, object]:
    portable = _portable_build_renderer_identity(build)
    return {
        "container_kind": portable["source_container_kind"],
        "system_cnf": {
            "logical_path": portable["system_cnf_path"],
            "sha256": portable["system_cnf_sha256"],
        },
        "boot_executable": {
            "logical_path": portable["boot_executable_path"],
            "sha256": portable["boot_executable_sha256"],
        },
        "unit_archive": (
            {
                "logical_path": portable["unit_archive_path"],
                "sha256": portable["unit_archive_sha256"],
            }
            if portable["unit_archive_path"] is not None else None
        ),
        "vehicle_roster": (
            {
                "logical_path": portable["vehicle_roster_path"],
                "sha256": portable["vehicle_roster_sha256"],
                "binding_status": "friendly_name_unmapped_roster",
            }
            if portable["vehicle_roster_path"] is not None else None
        ),
        "native_mesh_count": portable["native_mesh_count"],
        "name_binding_status": portable["name_binding_status"],
        "model_slot_binding_status": portable["model_slot_binding_status"],
        "model_slot_evidence": _json_clone(
            portable["model_slot_evidence"],
            label="native model-slot evidence"),
        "native_effect_count": portable["native_effect_count"],
        "native_effects": _json_clone(
            portable["native_effects"],
            label="native static-effect inventory"),
        "native_texture_packs": _json_clone(
            portable["native_texture_packs"],
            label="native texture-pack inventory"),
    }


def _require_build_membership(
        build: PsxNativeBuild, mesh: PsxNativeMesh,
        texture_pack: PsxNativeTexturePack | None) -> None:
    if not isinstance(build, PsxNativeBuild):
        raise TypeError("native batch requires a PsxNativeBuild")
    if not isinstance(mesh, PsxNativeMesh):
        raise TypeError("native batch requires a PsxNativeMesh")
    _require_frozen_build_contract(build)
    if not any(candidate is mesh for candidate in build.meshes):
        raise PsxNativeBatchProvenanceError(
            "native mesh does not belong to the frozen PSX build")
    if texture_pack is not None:
        if not isinstance(texture_pack, PsxNativeTexturePack):
            raise TypeError(
                "native texture selection must be a PsxNativeTexturePack")
        if not any(candidate is texture_pack
                   for candidate in build.texture_packs):
            raise PsxNativeBatchProvenanceError(
                "native texture pack does not belong to the frozen PSX build")


def _cardinal_sin_cos(angle_degrees: float) -> tuple[float, float]:
    radians = math.radians(angle_degrees)
    sine = math.sin(radians)
    cosine = math.cos(radians)
    if abs(sine) < 1e-12:
        sine = 0.0
    elif abs(abs(sine) - 1.0) < 1e-12:
        sine = math.copysign(1.0, sine)
    if abs(cosine) < 1e-12:
        cosine = 0.0
    elif abs(abs(cosine) - 1.0) < 1e-12:
        cosine = math.copysign(1.0, cosine)
    return sine, cosine


def _frozen_batch_camera_state(
        mesh: PsxNativeMesh,
        view_angles: tuple[float, float],
        *,
        width: int,
        height: int,
        zoom_percent: int,
) -> dict[str, object]:
    """Mirror the native viewport's deterministic preset framing math."""

    points = [
        mesh.vertices[index]
        for face in mesh.faces
        for index in face.vertex_indices
    ]
    if not points:
        raise PsxNativeBatchProvenanceError(
            "native batch mesh has no framed face vertices")
    xs = [point[0] for point in points]
    ys = [point[1] for point in points]
    zs = [point[2] for point in points]
    center = [
        (min(xs) + max(xs)) / 2,
        (min(ys) + max(ys)) / 2,
        (min(zs) + max(zs)) / 2,
    ]
    extent = max(
        max(xs) - min(xs), max(ys) - min(ys),
        max(zs) - min(zs), 1e-6)
    scale = 2.0 / extent
    yaw, pitch = view_angles
    yaw_sin, yaw_cos = _cardinal_sin_cos(yaw)
    pitch_sin, pitch_cos = _cardinal_sin_cos(pitch)
    camera_points = []
    for point in points:
        x = (point[0] - center[0]) * scale
        y = -(point[1] - center[1]) * scale
        z = (point[2] - center[2]) * scale
        xz_x = x * yaw_cos + z * yaw_sin
        xz_z = -x * yaw_sin + z * yaw_cos
        yz_y = y * pitch_cos - xz_z * pitch_sin
        yz_z = y * pitch_sin + xz_z * pitch_cos
        camera_points.append((xz_x, yz_y, yz_z))
    base_focal = min(max(1, width), max(1, height)) * 1.5
    usable = 0.92
    half_width = max(1, width) * 0.5 * usable
    half_height = max(1, height) * 0.5 * usable
    limits = [30.0]
    for x, y, z in camera_points:
        denominator = max(0.2, 4.0 - z)
        if abs(x) > 1e-9:
            limits.append(half_width * denominator / (abs(x) * base_focal))
        if abs(y) > 1e-9:
            limits.append(half_height * denominator / (abs(y) * base_focal))
    zoom_factor = max(25, min(300, zoom_percent)) / 100.0
    zoom = max(0.08, min(30.0, min(limits) * zoom_factor))
    return {
        "yaw": float(yaw),
        "pitch": float(pitch),
        "zoom": zoom,
        "pan": [0.0, 0.0],
        "center": [float(value) for value in center],
        "scale": scale,
    }


def build_native_snapshot_identity(
        build: PsxNativeBuild,
        mesh: PsxNativeMesh,
        texture_pack: PsxNativeTexturePack | None,
        *,
        view_name: str,
        view_angles: tuple[float, float],
        width: int,
        height: int,
        zoom_percent: int,
        background_rgba: tuple[int, int, int, int] | None = None,
        camera_state: Mapping[str, object] | None = None,
        guides: bool = False,
        capture_profile_id: str = PSX_NATIVE_BATCH_PROFILE_ID,
) -> dict[str, object]:
    """Build the portable, output-affecting identity for one native image.

    The function intentionally accepts no filesystem output path and never
    serializes ``PsxNativeBuild.root``.  Manual Snapshot export can call this
    with ``viewport.camera_orientation`` for a current/custom view; the batch
    exporter passes one of the fixed ``VIEW_PRESET_ANGLES`` entries.
    """

    _require_build_membership(build, mesh, texture_pack)
    width = _plain_int(
        width, label="snapshot width", minimum=1, maximum=_MAX_DIMENSION)
    height = _plain_int(
        height, label="snapshot height", minimum=1, maximum=_MAX_DIMENSION)
    if width * height > _MAX_PIXELS:
        raise PsxNativeBatchError(
            f"snapshot size {width} x {height} exceeds the native batch "
            f"safety budget of {_MAX_PIXELS:,} pixels")
    zoom = _plain_int(
        zoom_percent, label="zoom_percent", minimum=25, maximum=300)
    if not isinstance(view_name, str) or not view_name \
            or _CONTROL.search(view_name):
        raise PsxNativeBatchError("view_name must be non-empty display text")
    if not isinstance(view_angles, tuple) or len(view_angles) != 2:
        raise PsxNativeBatchError("view_angles must be a (yaw, pitch) tuple")
    angles = []
    for value in view_angles:
        if isinstance(value, bool) or not isinstance(value, (int, float)) \
                or not math.isfinite(float(value)):
            raise PsxNativeBatchError("view angles must be finite numbers")
        angles.append(float(value))
    background = _normalize_background(background_rgba)
    camera = _normalize_camera_state(camera_state)
    if not isinstance(guides, bool):
        raise PsxNativeBatchError("guides must be a bool")
    capture_profile = _stable_id(
        capture_profile_id, label="capture profile ID")
    if camera is not None and (
            not math.isclose(camera["yaw"], angles[0], abs_tol=1e-9)
            or not math.isclose(
                camera["pitch"], angles[1], abs_tol=1e-9)):
        raise PsxNativeBatchProvenanceError(
            "camera_state yaw/pitch do not match the declared view angles")

    ordinal = mesh.archive_ordinal
    if ordinal is not None:
        ordinal = _plain_int(ordinal, label="archive ordinal", minimum=0)
    archive_offset = mesh.archive_offset
    if archive_offset is not None:
        archive_offset = _plain_int(
            archive_offset, label="archive offset", minimum=0)
    archive_sector = mesh.archive_sector
    if archive_sector is not None:
        archive_sector = _plain_int(
            archive_sector, label="archive sector", minimum=0)
    model_slot, model_slot_evidence_id, runtime_identity_status, \
        name_binding_status = _mesh_runtime_identity(build, mesh)

    selected_pack = (
        _texture_pack_identity(texture_pack)
        if texture_pack is not None else None)
    texture_identity = {
        "mode": "topology_only" if texture_pack is None else "explicit_native_pack",
        "logical_path": (
            selected_pack["logical_path"] if selected_pack else None),
        "sha256": selected_pack["sha256"] if selected_pack else None,
        "selector_table_profile": (
            selected_pack["layout_id"] if selected_pack else None),
        "layout_id": (
            selected_pack["layout_id"] if selected_pack else None),
        "selector_to_pixel_bank_mapping": (
            selected_pack["selector_to_pixel_bank_mapping"]
            if selected_pack else None),
        "material_slot_count": (
            selected_pack["populated_selector_count"]
            if selected_pack else None),
        "populated_selectors": (
            selected_pack["populated_selectors"]
            if selected_pack else None),
        "mesh_environment_affinity": (
            "operator_selected_environment_no_mesh_inherent_affinity"
            if texture_pack is not None else "none"),
        "pc_texture_substitution": False,
    }
    profile = _json_clone(PSX_NATIVE_PROFILE_INFO, label="native profile")
    return {
        "capture_profile_id": capture_profile,
        "source": _portable_source_identity(build),
        "mesh": {
            "logical_path": _logical_path(
                mesh.logical_path, label="native mesh path"),
            "format": str(mesh.format_id),
            "format_version": int(mesh.format_version),
            "archive_ordinal": ordinal,
            "model_slot": model_slot,
            "model_slot_evidence_id": model_slot_evidence_id,
            "archive_offset": archive_offset,
            "archive_sector": archive_sector,
            "body_size": int(mesh.body_size),
            "body_sha256": _hash256(
                mesh.body_sha256, label="native mesh body hash"),
            "vertex_stream_sha256": _hash256(
                mesh.vertex_stream_sha256, label="native vertex-stream hash"),
            "face_stream_sha256": _hash256(
                mesh.face_stream_sha256, label="native face-stream hash"),
            "vertex_count": len(mesh.vertices),
            "face_count": len(mesh.faces),
            "texture_selector_census": [
                [selector, count]
                for selector, count in mesh_selector_census(mesh)
            ],
            "native_face_prefix_census": [
                [prefix, count]
                for prefix, count in mesh_face_prefix_census(mesh)
            ],
            "native_corner_shade_census": [
                [value, count]
                for value, count in mesh_corner_shade_census(mesh)
            ],
            "native_primitive_cull_census": [
                [policy, count]
                for policy, count in mesh_primitive_cull_census(mesh)
            ],
            "native_raw_corner_shade_census": [
                [value, count]
                for value, count in mesh_raw_corner_shade_census(mesh)
            ],
            "name_binding_status": name_binding_status,
            "runtime_identity_status": runtime_identity_status,
        },
        "texture": texture_identity,
        "view": {
            "name": view_name,
            "yaw_degrees": angles[0],
            "pitch_degrees": angles[1],
            "zoom_percent": zoom,
            "camera_state": camera,
            "preset_source": (
                "deterministic_named_preset_frozen_camera"
                if capture_profile == PSX_NATIVE_BATCH_PROFILE_ID
                and camera is not None else
                "explicit_full_camera_state"
                if camera is not None else
                "explicit_frozen_angles"),
        },
        "output": {
            "width": width,
            "height": height,
            "format": "PNG",
            "guides": guides,
            "animation": "disabled_initial_frame",
            "background": (
                {"mode": "transparent"}
                if background is None else
                {"mode": "rgba", "rgba": list(background)}),
        },
        "renderer_contract": {
            "requested_mode": PSX_NATIVE_VIEW_MODE,
            "effective_mode": PSX_NATIVE_PROFILE_ID,
            "profile_id": PSX_NATIVE_PROFILE_ID,
            "profile_version": PSX_NATIVE_PROFILE_VERSION,
            "parser_id": PSX_NATIVE_PARSER_ID,
            "parser_version": PSX_NATIVE_PARSER_VERSION,
            "source_asset_pipeline": PSX_NATIVE_SOURCE_PIPELINE,
            "native_psx_asset_decode": True,
            "pc_openua_source_used": False,
            "fallback_used": False,
            "profile": profile,
        },
    }


def _require_exact(info: Mapping[str, object], key: str, expected) -> None:
    actual = info.get(key)
    if isinstance(expected, bool):
        matches = isinstance(actual, bool) and actual is expected
    elif isinstance(expected, int):
        matches = isinstance(actual, int) and not isinstance(actual, bool) \
            and actual == expected
    else:
        matches = actual == expected
    if not matches:
        raise PsxNativeBatchProvenanceError(
            f"native renderer proof requires {key}={expected!r}; "
            f"received {actual!r}")


def _require_exact_keys(
        info: Mapping[str, object], expected: set[str], *, label: str) -> None:
    actual = set(info)
    if actual != expected:
        raise PsxNativeBatchProvenanceError(
            f"{label} has an inexact field set; "
            f"unexpected={sorted(actual - expected)!r}, "
            f"missing={sorted(expected - actual)!r}")


def validate_native_renderer_info(
        renderer_info: Mapping[str, object],
        *,
        build: PsxNativeBuild,
        mesh: PsxNativeMesh,
        texture_pack: PsxNativeTexturePack | None,
) -> dict[str, object]:
    """Validate and sanitize one completed native renderer transaction.

    The returned dictionary is reconstructed from the frozen source objects and
    exact validated fields.  It cannot contain ``build.root`` or caller-added
    absolute paths.  Both the native mesh batch and future manual Snapshot
    export should use this helper before committing a file.
    """

    _require_build_membership(build, mesh, texture_pack)
    if not isinstance(renderer_info, Mapping):
        raise PsxNativeBatchProvenanceError(
            "native snapshot lacks renderer_info")
    texture_packs_available = bool(build.texture_packs)
    selected_pack = (
        _texture_pack_identity(texture_pack)
        if texture_pack is not None else None)
    model_slot, model_slot_evidence_id, runtime_identity_status, \
        name_binding_status = _mesh_runtime_identity(build, mesh)
    if texture_pack is not None:
        color_semantics = (
            (
                "bgr555_and_zero_word_transparency_applied; "
                "legacy_psw_material_local_uv_quotient_and_direct_"
                "grayscale_affine_modulation_applied; descriptor_origin_"
                "tpage_clut_offset_stp_abr_runtime_binding_unresolved"
            )
            if mesh.format_id == "PSW/PSV" else (
                "bgr555_and_zero_word_transparency_applied; "
                "pw3_packet_shade_formulas_recovered_but_effective_"
                "dispatch_unresolved_not_applied; "
                "descriptor_origin_tpage_clut_offset_stp_abr_runtime_"
                "binding_unresolved"
            ))
        binding_status = (
            "operator_selected_pack_with_validated_selector_table")
        selector_table_status = "validated_native_setgfx_selector_table"
        mesh_binding = (
            "operator_selected_environment_variant_no_mesh_inherent_affinity")
        texture_stp = "preserved_not_blended"
    elif texture_packs_available:
        color_semantics = (
            "not_applied_topology_only; "
            "validated_native_packs_available_not_selected; "
            "diagnostic_selector_colors")
        binding_status = "topology_only_operator_default"
        selector_table_status = "validated_native_packs_available_not_selected"
        mesh_binding = "none_selected"
        texture_stp = "not_applied_no_pack_selected"
    else:
        color_semantics = (
            "not_applied_topology_only; no_validated_native_pack; "
            "diagnostic_selector_colors")
        binding_status = "unavailable_not_substituted"
        selector_table_status = "unavailable_not_substituted"
        mesh_binding = "none_available"
        texture_stp = "unavailable"

    required = {
        "available": True,
        "resources_available": True,
        "mode": PSX_NATIVE_PROFILE_ID,
        "requested_mode": PSX_NATIVE_VIEW_MODE,
        "effective_mode": PSX_NATIVE_PROFILE_ID,
        "fallback_used": False,
        "fallback_reason": "",
        "profile_id": PSX_NATIVE_PROFILE_ID,
        "profile_version": PSX_NATIVE_PROFILE_VERSION,
        "source_asset_pipeline": PSX_NATIVE_SOURCE_PIPELINE,
        "native_psx_asset_decode": True,
        "pc_openua_source_used": False,
        "parser_id": PSX_NATIVE_PARSER_ID,
        "parser_version": PSX_NATIVE_PARSER_VERSION,
        "native_asset_path": _logical_path(
            mesh.logical_path, label="native mesh path"),
        "native_mesh_format": mesh.format_id,
        "native_mesh_format_version": mesh.format_version,
        "native_mesh_ordinal": mesh.archive_ordinal,
        "native_model_slot": model_slot,
        "native_model_slot_evidence_id": model_slot_evidence_id,
        "native_mesh_offset": mesh.archive_offset,
        "native_mesh_sector": mesh.archive_sector,
        "native_mesh_sha256": _hash256(
            mesh.body_sha256, label="native mesh body hash"),
        "native_vertex_stream_sha256": _hash256(
            mesh.vertex_stream_sha256, label="native vertex-stream hash"),
        "native_face_stream_sha256": _hash256(
            mesh.face_stream_sha256, label="native face-stream hash"),
        "vertex_count": len(mesh.vertices),
        "face_count": len(mesh.faces),
        "texture_selector_census": [
            [selector, count]
            for selector, count in mesh_selector_census(mesh)
        ],
        "native_face_prefix_census": [
            [prefix, count]
            for prefix, count in mesh_face_prefix_census(mesh)
        ],
        "native_corner_shade_census": [
            [value, count]
            for value, count in mesh_corner_shade_census(mesh)
        ],
        "native_primitive_cull_census": [
            [policy, count]
            for policy, count in mesh_primitive_cull_census(mesh)
        ],
        "native_raw_corner_shade_census": [
            [value, count]
            for value, count in mesh_raw_corner_shade_census(mesh)
        ],
        "name_binding_status": name_binding_status,
        "runtime_identity_status": runtime_identity_status,
        "native_texture_decode": texture_pack is not None,
        "native_texture_pack_path": (
            _logical_path(texture_pack.logical_path,
                          label="selected texture-pack path")
            if texture_pack is not None else None),
        "native_texture_pack_sha256": (
            selected_pack["sha256"] if selected_pack else None),
        "native_texture_pack_layout_id": (
            selected_pack["layout_id"] if selected_pack else None),
        "native_texture_selector_to_pixel_bank_mapping": (
            selected_pack["selector_to_pixel_bank_mapping"]
            if selected_pack else None),
        "native_texture_material_slot_count": (
            selected_pack["populated_selector_count"]
            if selected_pack else None),
        "native_texture_populated_selectors": (
            selected_pack["populated_selectors"]
            if selected_pack else None),
        "native_texture_dimensions": (
            [128, 128] if texture_pack is not None else None),
        "native_texture_index_depth": (
            4 if texture_pack is not None else None),
        "native_texture_mapping": (
            selected_pack["selector_to_pixel_bank_mapping"]
            if selected_pack else None),
        "native_texture_uv_profile": (
            (
                PSX_NATIVE_PROFILE_INFO["psw_psv_uv_profile"]
                if mesh.format_id == "PSW/PSV"
                else PSX_NATIVE_PROFILE_INFO["pw3_uv_profile"]
            )
            if selected_pack else None),
        "native_texture_descriptor_origin": (
            (
                PSX_NATIVE_PROFILE_INFO["psw_psv_descriptor_origin_state"]
                if mesh.format_id == "PSW/PSV"
                else "unresolved_not_applied"
            )
            if selected_pack else None),
        "native_texture_absolute_vram_binding": (
            "unresolved_not_applied" if selected_pack else None),
        "native_texture_filtering": (
            "nearest" if texture_pack is not None else None),
        "native_texture_zero_rule": (
            "resolved_CLUT_word_0x0000_transparent"
            if texture_pack is not None else None),
        "native_texture_stp": texture_stp,
        "psx_color_semantics": color_semantics,
        "texture_binding_status": binding_status,
        "texture_selector_table_status": selector_table_status,
        "mesh_to_texture_pack_binding": mesh_binding,
    }
    for key, expected in PSX_NATIVE_PROFILE_INFO.items():
        # The renderer specializes this contract-level statement to the exact
        # selected-pack/topology transaction above.
        if key != "psx_color_semantics":
            required[key] = expected
    for key, expected in required.items():
        _require_exact(renderer_info, key, expected)

    # Accept exactly the renderer_info contract emitted by AssetViewport.
    # ``reason`` is a known redundant live-view field and is validated but
    # deliberately omitted from the portable sidecar proof.  Any other caller
    # addition is rejected instead of being silently sanitized away.
    _require_exact_keys(renderer_info, {
        *required.keys(), "background_mode",
        "presentation_background_mode", "sources", "reason",
    }, label="native renderer_info")
    _require_exact(renderer_info, "reason", "")

    sources = renderer_info.get("sources")
    if not isinstance(sources, Mapping):
        raise PsxNativeBatchProvenanceError(
            "native renderer proof lacks its PSX source block")
    build_info = sources.get("psx_build")
    expected_build = _portable_build_renderer_identity(build)
    if not isinstance(build_info, Mapping):
        raise PsxNativeBatchProvenanceError(
            "native renderer proof lacks its frozen build identity")
    _require_exact_keys(
        sources, {"psx_build"}, label="native renderer sources")
    _require_exact_keys(
        build_info, set(expected_build),
        label="native renderer build identity")
    for key, expected in expected_build.items():
        _require_exact(build_info, key, expected)

    background_mode = renderer_info.get("background_mode")
    if background_mode not in {
            "transparent_or_viewer_default",
            "rgb_presentation_post_composite"}:
        raise PsxNativeBatchProvenanceError(
            "native renderer proof lacks a recognized background mode")
    presentation_background_mode = renderer_info.get(
        "presentation_background_mode")
    if presentation_background_mode != background_mode:
        raise PsxNativeBatchProvenanceError(
            "native renderer background and presentation proof disagree")

    # Select only validated, JSON-safe fields.  In particular, never copy an
    # arbitrary caller mapping wholesale into an exported sidecar.
    result = {key: _json_clone(value, label=f"renderer field {key}")
              for key, value in required.items()}
    result.update({
        "background_mode": background_mode,
        "presentation_background_mode": presentation_background_mode,
        "sources": {"psx_build": expected_build},
    })
    return result


def _validate_identity_renderer_alignment(
        identity: Mapping[str, object],
        renderer: Mapping[str, object]) -> None:
    """Fail closed if trusted renderer proof and capture identity diverge."""

    if not isinstance(identity, Mapping) or not isinstance(renderer, Mapping):
        raise PsxNativeBatchProvenanceError(
            "native sidecar requires identity and renderer mappings")
    _stable_id(
        identity.get("capture_profile_id"), label="capture profile ID")
    source = identity.get("source")
    mesh = identity.get("mesh")
    texture = identity.get("texture")
    view = identity.get("view")
    output = identity.get("output")
    contract = identity.get("renderer_contract")
    if not all(isinstance(value, Mapping) for value in (
            source, mesh, texture, view, output, contract)):
        raise PsxNativeBatchProvenanceError(
            "native identity lacks source/mesh/texture/view/output/contract")
    assert isinstance(source, Mapping)
    assert isinstance(mesh, Mapping)
    assert isinstance(texture, Mapping)
    assert isinstance(view, Mapping)
    assert isinstance(output, Mapping)
    assert isinstance(contract, Mapping)
    _require_exact_keys(identity, {
        "capture_profile_id", "source", "mesh", "texture", "view",
        "output", "renderer_contract",
    }, label="native capture identity")
    _require_exact_keys(source, {
        "container_kind", "system_cnf", "boot_executable",
        "unit_archive", "vehicle_roster", "native_mesh_count",
        "name_binding_status", "model_slot_binding_status",
        "model_slot_evidence", "native_effect_count", "native_effects",
        "native_texture_packs",
    }, label="native source identity")
    _require_exact_keys(mesh, {
        "logical_path", "format", "format_version", "archive_ordinal",
        "model_slot", "model_slot_evidence_id", "archive_offset",
        "archive_sector", "body_size", "body_sha256",
        "vertex_stream_sha256", "face_stream_sha256", "vertex_count",
        "face_count", "texture_selector_census",
        "native_face_prefix_census", "native_corner_shade_census",
        "native_primitive_cull_census",
        "native_raw_corner_shade_census",
        "name_binding_status", "runtime_identity_status",
    }, label="native mesh identity")
    _require_exact_keys(texture, {
        "mode", "logical_path", "sha256", "selector_table_profile",
        "layout_id", "selector_to_pixel_bank_mapping",
        "material_slot_count", "populated_selectors",
        "mesh_environment_affinity", "pc_texture_substitution",
    }, label="native texture identity")
    _require_exact_keys(view, {
        "name", "yaw_degrees", "pitch_degrees", "zoom_percent",
        "camera_state", "preset_source",
    }, label="native view identity")
    _require_exact_keys(output, {
        "width", "height", "format", "guides", "animation",
        "background",
    }, label="native output identity")
    _require_exact_keys(contract, {
        "requested_mode", "effective_mode", "profile_id",
        "profile_version", "parser_id", "parser_version",
        "source_asset_pipeline", "native_psx_asset_decode",
        "pc_openua_source_used", "fallback_used", "profile",
    }, label="native renderer contract")

    for key in (
            "requested_mode", "effective_mode", "profile_id",
            "profile_version", "parser_id", "parser_version",
            "source_asset_pipeline", "native_psx_asset_decode",
            "pc_openua_source_used", "fallback_used"):
        _require_exact(renderer, key, contract.get(key))
    _require_exact(renderer, "available", True)
    _require_exact(renderer, "resources_available", True)

    sources = renderer.get("sources")
    build_info = sources.get("psx_build") \
        if isinstance(sources, Mapping) else None
    if not isinstance(build_info, Mapping):
        raise PsxNativeBatchProvenanceError(
            "native renderer proof lacks its build source")
    for key in ("system_cnf", "boot_executable"):
        nested = source.get(key)
        if not isinstance(nested, Mapping):
            raise PsxNativeBatchProvenanceError(
                f"native source identity lacks {key}")
        _require_exact_keys(
            nested, {"logical_path", "sha256"},
            label=f"native {key} identity")
    unit_archive = source.get("unit_archive")
    if unit_archive is not None:
        if not isinstance(unit_archive, Mapping):
            raise PsxNativeBatchProvenanceError(
                "native unit archive identity is malformed")
        _require_exact_keys(
            unit_archive, {"logical_path", "sha256"},
            label="native unit archive identity")
    vehicle_roster = source.get("vehicle_roster")
    if vehicle_roster is not None:
        if not isinstance(vehicle_roster, Mapping):
            raise PsxNativeBatchProvenanceError(
                "native vehicle roster identity is malformed")
        _require_exact_keys(
            vehicle_roster, {"logical_path", "sha256", "binding_status"},
            label="native vehicle roster identity")
    model_slot_evidence = source.get("model_slot_evidence")
    if model_slot_evidence is not None:
        if not isinstance(model_slot_evidence, Mapping):
            raise PsxNativeBatchProvenanceError(
                "native model-slot evidence is malformed")
        _require_exact_keys(model_slot_evidence, {
            "evidence_id", "unit_archive_sha256",
            "boot_executable_sha256", "executable_table_offset",
            "executable_table_entry_count", "executable_table_sha256",
            "empty_model_slots", "trailing_sentinel_archive_sector",
        }, label="native model-slot evidence")
    expected_slot_status = (
        "executable_allocation_table_proven"
        if model_slot_evidence is not None else "unavailable_unproven")
    _require_exact(
        source, "model_slot_binding_status", expected_slot_status)
    effects = source.get("native_effects")
    if not isinstance(effects, list) \
            or source.get("native_effect_count") != len(effects):
        raise PsxNativeBatchProvenanceError(
            "native static-effect inventory count is malformed")
    for effect in effects:
        if not isinstance(effect, Mapping):
            raise PsxNativeBatchProvenanceError(
                "native static-effect inventory entry is malformed")
        _require_exact_keys(effect, {
            "logical_path", "source_sha256", "vertex_stream_sha256",
            "face_stream_sha256", "format_id", "format_version",
            "parser_id", "parser_version", "binding_status",
        }, label="native static-effect inventory entry")
    packs = source.get("native_texture_packs")
    if not isinstance(packs, list):
        raise PsxNativeBatchProvenanceError(
            "native texture-pack inventory is malformed")
    for pack in packs:
        if not isinstance(pack, Mapping):
            raise PsxNativeBatchProvenanceError(
                "native texture-pack inventory entry is malformed")
        _require_exact_keys(pack, {
            "logical_path", "sha256", "profile", "layout_id",
            "selector_to_pixel_bank_mapping", "populated_selector_count",
            "populated_selectors",
        }, label="native texture-pack inventory entry")
    expected_build = {
        "source_container_kind": source.get("container_kind"),
        "system_cnf_path": (
            source.get("system_cnf", {}).get("logical_path")
            if isinstance(source.get("system_cnf"), Mapping) else None),
        "system_cnf_sha256": (
            source.get("system_cnf", {}).get("sha256")
            if isinstance(source.get("system_cnf"), Mapping) else None),
        "boot_executable_path": (
            source.get("boot_executable", {}).get("logical_path")
            if isinstance(source.get("boot_executable"), Mapping) else None),
        "boot_executable_sha256": (
            source.get("boot_executable", {}).get("sha256")
            if isinstance(source.get("boot_executable"), Mapping) else None),
        "unit_archive_path": (
            source.get("unit_archive", {}).get("logical_path")
            if isinstance(source.get("unit_archive"), Mapping) else None),
        "unit_archive_sha256": (
            source.get("unit_archive", {}).get("sha256")
            if isinstance(source.get("unit_archive"), Mapping) else None),
        "vehicle_roster_path": (
            source.get("vehicle_roster", {}).get("logical_path")
            if isinstance(source.get("vehicle_roster"), Mapping) else None),
        "vehicle_roster_sha256": (
            source.get("vehicle_roster", {}).get("sha256")
            if isinstance(source.get("vehicle_roster"), Mapping) else None),
        "native_mesh_count": source.get("native_mesh_count"),
        "name_binding_status": source.get("name_binding_status"),
        "model_slot_binding_status": source.get(
            "model_slot_binding_status"),
        "model_slot_evidence": source.get("model_slot_evidence"),
        "native_effect_count": source.get("native_effect_count"),
        "native_effects": source.get("native_effects"),
        "native_texture_packs": source.get("native_texture_packs"),
    }
    for key, expected in expected_build.items():
        _require_exact(build_info, key, expected)
    if set(sources) != {"psx_build"} \
            or set(build_info) != set(expected_build):
        raise PsxNativeBatchProvenanceError(
            "native renderer source proof has unexpected fields")

    mesh_fields = {
        "native_asset_path": mesh.get("logical_path"),
        "native_mesh_format": mesh.get("format"),
        "native_mesh_format_version": mesh.get("format_version"),
        "native_mesh_ordinal": mesh.get("archive_ordinal"),
        "native_model_slot": mesh.get("model_slot"),
        "native_model_slot_evidence_id": mesh.get(
            "model_slot_evidence_id"),
        "native_mesh_offset": mesh.get("archive_offset"),
        "native_mesh_sector": mesh.get("archive_sector"),
        "native_mesh_sha256": mesh.get("body_sha256"),
        "native_vertex_stream_sha256": mesh.get("vertex_stream_sha256"),
        "native_face_stream_sha256": mesh.get("face_stream_sha256"),
        "vertex_count": mesh.get("vertex_count"),
        "face_count": mesh.get("face_count"),
        "texture_selector_census": mesh.get("texture_selector_census"),
        "native_face_prefix_census": mesh.get(
            "native_face_prefix_census"),
        "native_corner_shade_census": mesh.get(
            "native_corner_shade_census"),
        "native_primitive_cull_census": mesh.get(
            "native_primitive_cull_census"),
        "native_raw_corner_shade_census": mesh.get(
            "native_raw_corner_shade_census"),
        "name_binding_status": mesh.get("name_binding_status"),
        "runtime_identity_status": mesh.get("runtime_identity_status"),
    }
    for key, expected in mesh_fields.items():
        _require_exact(renderer, key, expected)

    explicit_pack = texture.get("mode") == "explicit_native_pack"
    if texture.get("mode") not in {"topology_only", "explicit_native_pack"}:
        raise PsxNativeBatchProvenanceError(
            "native identity has an unknown texture mode")
    packs_available = bool(source.get("native_texture_packs"))
    texture_fields = {
        "native_texture_decode": explicit_pack,
        "native_texture_pack_path": texture.get("logical_path"),
        "native_texture_pack_sha256": texture.get("sha256"),
        "native_texture_pack_layout_id": texture.get("layout_id"),
        "native_texture_selector_to_pixel_bank_mapping": texture.get(
            "selector_to_pixel_bank_mapping"),
        "native_texture_material_slot_count": texture.get(
            "material_slot_count"),
        "native_texture_populated_selectors": texture.get(
            "populated_selectors"),
        "native_texture_dimensions": [128, 128] if explicit_pack else None,
        "native_texture_index_depth": 4 if explicit_pack else None,
        "native_texture_mapping": (
            str(texture.get("selector_to_pixel_bank_mapping"))
            if explicit_pack else None),
        "native_texture_uv_profile": (
            (
                PSX_NATIVE_PROFILE_INFO["psw_psv_uv_profile"]
                if mesh.get("format") == "PSW/PSV"
                else PSX_NATIVE_PROFILE_INFO["pw3_uv_profile"]
            )
            if explicit_pack else None),
        "native_texture_descriptor_origin": (
            (
                PSX_NATIVE_PROFILE_INFO["psw_psv_descriptor_origin_state"]
                if mesh.get("format") == "PSW/PSV"
                else "unresolved_not_applied"
            )
            if explicit_pack else None),
        "native_texture_absolute_vram_binding": (
            "unresolved_not_applied" if explicit_pack else None),
        "native_texture_filtering": "nearest" if explicit_pack else None,
        "native_texture_zero_rule": (
            "resolved_CLUT_word_0x0000_transparent"
            if explicit_pack else None),
        "native_texture_stp": (
            "preserved_not_blended" if explicit_pack else
            "not_applied_no_pack_selected"
            if packs_available else "unavailable"),
        "psx_color_semantics": (
            (
                (
                    "bgr555_and_zero_word_transparency_applied; "
                    "legacy_psw_material_local_uv_quotient_and_direct_"
                    "grayscale_affine_modulation_applied; descriptor_origin_"
                    "tpage_clut_offset_stp_abr_runtime_binding_unresolved"
                )
                if mesh.get("format") == "PSW/PSV" else (
                    "bgr555_and_zero_word_transparency_applied; "
                    "pw3_packet_shade_formulas_recovered_but_effective_"
                    "dispatch_unresolved_not_applied; "
                    "descriptor_origin_tpage_clut_offset_stp_abr_runtime_"
                    "binding_unresolved"
                )
            )
            if explicit_pack else
            "not_applied_topology_only; "
            "validated_native_packs_available_not_selected; "
            "diagnostic_selector_colors"
            if packs_available else
            "not_applied_topology_only; no_validated_native_pack; "
            "diagnostic_selector_colors"),
        "texture_binding_status": (
            "operator_selected_pack_with_validated_selector_table"
            if explicit_pack else
            "topology_only_operator_default"
            if packs_available else "unavailable_not_substituted"),
        "texture_selector_table_status": (
            "validated_native_setgfx_selector_table"
            if explicit_pack else
            "validated_native_packs_available_not_selected"
            if packs_available else "unavailable_not_substituted"),
        "mesh_to_texture_pack_binding": (
            "operator_selected_environment_variant_no_mesh_inherent_affinity"
            if explicit_pack else
            "none_selected" if packs_available else "none_available"),
    }
    for key, expected in texture_fields.items():
        _require_exact(renderer, key, expected)

    profile = contract.get("profile")
    if not isinstance(profile, Mapping):
        raise PsxNativeBatchProvenanceError(
            "native identity lacks its renderer profile contract")
    for key, expected in profile.items():
        if key != "psx_color_semantics":
            _require_exact(renderer, key, expected)

    allowed_renderer_keys = {
        "available", "resources_available", "mode", "requested_mode",
        "effective_mode", "fallback_used", "fallback_reason",
        "background_mode", "presentation_background_mode", "sources",
        *contract.keys(), *mesh_fields.keys(), *texture_fields.keys(),
        *profile.keys(),
    }
    # ``profile`` is a nested contract block rather than a renderer_info key.
    allowed_renderer_keys.discard("profile")
    if set(renderer) != allowed_renderer_keys:
        unexpected = sorted(set(renderer) - allowed_renderer_keys)
        missing_renderer = sorted(allowed_renderer_keys - set(renderer))
        raise PsxNativeBatchProvenanceError(
            "native renderer proof has an inexact field set; "
            f"unexpected={unexpected!r}, missing={missing_renderer!r}")

    camera = _normalize_camera_state(view.get("camera_state"))
    if camera != view.get("camera_state"):
        raise PsxNativeBatchProvenanceError(
            "native identity camera is not in canonical finite form")
    if camera is not None and (
            camera["yaw"] != view.get("yaw_degrees")
            or camera["pitch"] != view.get("pitch_degrees")):
        raise PsxNativeBatchProvenanceError(
            "native identity camera and view angles disagree")
    if not isinstance(output.get("guides"), bool):
        raise PsxNativeBatchProvenanceError(
            "native identity guides flag is not a bool")
    background = output.get("background")
    if not isinstance(background, Mapping) \
            or background.get("mode") not in {"transparent", "rgba"}:
        raise PsxNativeBatchProvenanceError(
            "native identity lacks a recognized output background")
    expected_background = (
        "transparent_or_viewer_default"
        if background.get("mode") == "transparent" else
        "rgb_presentation_post_composite")
    _require_exact(renderer, "background_mode", expected_background)
    _require_exact(
        renderer, "presentation_background_mode", expected_background)


def build_native_snapshot_sidecar(
        *, identity: Mapping[str, object],
        renderer: Mapping[str, object]) -> dict[str, object]:
    """Return pre-artifact sidecar data accepted by the atomic pair writer."""

    _validate_identity_renderer_alignment(identity, renderer)
    return {
        "schema_id": PSX_NATIVE_SNAPSHOT_SCHEMA,
        "schema_version": PSX_NATIVE_SNAPSHOT_SCHEMA_VERSION,
        "identity": _json_clone(identity, label="snapshot identity"),
        "renderer": _json_clone(renderer, label="renderer provenance"),
    }


def _validate_windows_path_budget(
        path: Path, *, label: str,
        error_type=PsxNativeBatchError) -> None:
    """Fail before I/O when Qt/Python cannot safely address a Windows path."""

    if os.name != "nt":
        return
    absolute = Path(path).expanduser().absolute()
    length = len(str(absolute))
    if length > _WINDOWS_SAFE_PATH_LIMIT:
        raise error_type(
            f"{label} is {length} characters; native snapshot paths must be "
            f"at most {_WINDOWS_SAFE_PATH_LIMIT} characters on Windows. "
            "Choose a shorter output folder.")


def _temporary_path(parent: Path, name: str, suffix: str) -> Path:
    # Do not repeat the potentially long final filename in a staging path.
    # The digest preserves useful per-target separation while tempfile's
    # random suffix retains collision safety in one directory.
    digest = hashlib.sha256(str(name).encode("utf-8")).hexdigest()[:12]
    prefix = f"{_TEMPORARY_PREFIX}{digest}."
    representative = parent / f"{prefix}XXXXXXXX{suffix}"
    _validate_windows_path_budget(
        representative,
        label="native atomic temporary path",
        error_type=PsxNativeAtomicWriteError,
    )
    fd: int | None = None
    value: str | None = None
    try:
        fd, value = tempfile.mkstemp(
            prefix=prefix, suffix=suffix, dir=str(parent))
        os.close(fd)
        fd = None
        return Path(value)
    except OSError as exc:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        if value is not None:
            try:
                Path(value).unlink(missing_ok=True)
            except OSError:
                pass
        raise PsxNativeAtomicWriteError(
            f"could not allocate a native atomic staging file: {exc}") \
            from exc


def _cleanup_paths(paths: Sequence[Path]) -> None:
    for path in paths:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


def _write_atomic_json_document(
        path: Path, document: Mapping[str, object], *,
        source_root: Path | None = None) -> str:
    """Atomically replace one deterministic JSON document."""

    if source_root is not None:
        validate_native_output_commit_boundary(source_root, path)
    _validate_windows_path_budget(
        path,
        label="native batch manifest path",
        error_type=PsxNativeAtomicWriteError,
    )
    if path.exists() and path.is_dir():
        raise PsxNativeAtomicWriteError(
            f"JSON target is a directory: {path}")
    if path.is_symlink():
        raise PsxNativeAtomicWriteError(
            f"JSON target is a symlink: {path}")
    encoded = (
        json.dumps(
            _json_clone(document, label="JSON document"),
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n").encode("utf-8")
    staged = _temporary_path(path.parent, path.name, ".json.stage")
    try:
        with staged.open("wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        if source_root is not None:
            # The target may have been redirected after staging.  Re-resolve
            # immediately before installing the final manifest bytes.
            validate_native_output_commit_boundary(source_root, path)
        os.replace(staged, path)
    except Exception as exc:
        _cleanup_paths((staged,))
        raise PsxNativeAtomicWriteError(
            f"atomic JSON commit failed for {path.name}: {exc}") from exc
    return hashlib.sha256(encoded).hexdigest()


def _commit_staged_pair(
        staged_png: Path, staged_json: Path,
        final_png: Path, final_json: Path, *, overwrite: bool) -> None:
    finals = (final_png, final_json)
    stages = (staged_png, staged_json)
    for final in finals:
        if final.exists() and final.is_dir():
            raise PsxNativeAtomicWriteError(
                f"snapshot target is a directory: {final}")
    if not overwrite and any(final.exists() for final in finals):
        raise PsxNativeBatchCollisionError(
            "snapshot pair already exists and overwrite was not authorized")

    backups: dict[Path, Path] = {}
    reserved_backups: list[Path] = []
    committed: list[Path] = []
    try:
        if overwrite:
            for final in finals:
                if not final.exists():
                    continue
                backup = _temporary_path(
                    final.parent, final.name, ".rollback")
                # Track the reservation before the first operation that can
                # fail.  If unlinking the empty mkstemp file is interrupted,
                # failure cleanup must still remove that reservation.
                reserved_backups.append(backup)
                # mkstemp reserves a unique name; os.replace requires that name
                # to be absent when moving the old final aside on all platforms.
                backup.unlink()
                os.replace(final, backup)
                backups[final] = backup
        for staged, final in zip(stages, finals):
            os.replace(staged, final)
            committed.append(final)
    except Exception as exc:
        rollback_errors: list[str] = []
        retained_backups: set[Path] = set()
        for final in reversed(committed):
            try:
                final.unlink(missing_ok=True)
            except OSError as rollback_exc:
                rollback_errors.append(f"remove {final}: {rollback_exc}")
        for final, backup in backups.items():
            if not backup.exists():
                continue
            try:
                os.replace(backup, final)
            except OSError as rollback_exc:
                # This backup now contains the user's previous final. Preserve
                # it for manual recovery rather than treating it as an empty
                # reservation during the general cleanup below.
                retained_backups.add(backup)
                rollback_errors.append(
                    f"restore {final}: {rollback_exc}")
        _cleanup_paths((
            *stages,
            *(backup for backup in reserved_backups
              if backup not in retained_backups),
        ))
        # A failed restore is material: retain its unique rollback file rather
        # than deleting the user's previous bytes.  Successful restores remove
        # the backup through os.replace.
        detail = (
            "; rollback issues: " + "; ".join(rollback_errors)
            if rollback_errors else "")
        raise PsxNativeAtomicWriteError(
            f"native PNG/JSON transaction failed: {exc}{detail}") from exc
    else:
        _cleanup_paths(tuple(reserved_backups))


def write_atomic_png_json_pair(
        image: QImage,
        png_path: str | Path,
        metadata: Mapping[str, object],
        *,
        overwrite: bool = True,
        source_root: Path | None = None,
) -> NativeSnapshotPair:
    """Stage and transactionally commit a PNG plus ``.png.json`` sidecar.

    ``metadata`` must be the result of :func:`build_native_snapshot_sidecar`.
    The helper adds the byte-derived ``artifact`` block.  Existing finals are
    moved to unique same-directory rollback files before either staged final is
    installed; a failed second replace therefore restores the old pair.
    """

    if not isinstance(image, QImage) or image.isNull():
        raise PsxNativeAtomicWriteError(
            "native snapshot image is null or not a QImage")
    if not isinstance(overwrite, bool):
        raise PsxNativeAtomicWriteError("overwrite must be a bool")
    final_png = Path(png_path)
    if final_png.suffix.casefold() != ".png":
        raise PsxNativeAtomicWriteError(
            "native snapshot target must have a .png suffix")
    if source_root is not None:
        validate_native_output_commit_boundary(source_root, final_png)
    parent = final_png.parent
    if not parent.is_dir():
        raise PsxNativeAtomicWriteError(
            f"snapshot output directory does not exist: {parent}")
    final_json = final_png.with_suffix(final_png.suffix + ".json")
    _validate_windows_path_budget(
        final_png,
        label="native snapshot PNG path",
        error_type=PsxNativeAtomicWriteError,
    )
    _validate_windows_path_budget(
        final_json,
        label="native snapshot sidecar path",
        error_type=PsxNativeAtomicWriteError,
    )
    if final_png.is_symlink() or final_json.is_symlink():
        raise PsxNativeAtomicWriteError(
            "native snapshot target pair must not contain symlinks")
    base = _json_clone(metadata, label="snapshot sidecar")
    if not isinstance(base, dict) \
            or base.get("schema_id") != PSX_NATIVE_SNAPSHOT_SCHEMA \
            or base.get("schema_version") != PSX_NATIVE_SNAPSHOT_SCHEMA_VERSION:
        raise PsxNativeAtomicWriteError(
            "snapshot metadata lacks the native schema ID/version")
    if "artifact" in base:
        raise PsxNativeAtomicWriteError(
            "artifact is reserved for byte-derived commit metadata")
    if set(base) != {"schema_id", "schema_version", "identity", "renderer"}:
        raise PsxNativeAtomicWriteError(
            "snapshot metadata has an inexact native schema field set")

    staged_png: Path | None = None
    staged_json: Path | None = None
    try:
        # Allocate both names inside the cleanup guard. If the second mkstemp
        # fails, the first reserved file must not survive as zero-byte debris.
        staged_png = _temporary_path(parent, final_png.name, ".png.stage")
        staged_json = _temporary_path(parent, final_json.name, ".json.stage")
        writer = QImageWriter(str(staged_png), b"png")
        written = writer.write(image)
        writer_error = writer.errorString()
        # On Windows the writer's underlying QFile remains open until the Qt
        # wrapper is destroyed, which would make the same-directory replace
        # fail with a sharing violation.
        del writer
        if not written:
            raise PsxNativeAtomicWriteError(
                writer_error or "Qt could not encode the native PNG")
        # QImageWriter has closed the device before returning.  Flush the
        # staged bytes to the filesystem before making either final visible.
        with staged_png.open("r+b") as handle:
            os.fsync(handle.fileno())
        png_size = staged_png.stat().st_size
        png_sha = _sha256_file(staged_png)
        base["artifact"] = {
            "png_file": final_png.name,
            "png_sha256": png_sha,
            "png_size_bytes": png_size,
            "width": image.width(),
            "height": image.height(),
        }
        encoded = (
            json.dumps(base, ensure_ascii=True, sort_keys=True,
                       separators=(",", ":"), allow_nan=False)
            + "\n").encode("utf-8")
        with staged_json.open("wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        json_sha = hashlib.sha256(encoded).hexdigest()
        if source_root is not None:
            # Rendering/encoding is intentionally outside the source tree.
            # Recheck after staging so a swapped parent cannot redirect the
            # final pair into the immutable prototype corpus.
            validate_native_output_commit_boundary(source_root, final_png)
        _commit_staged_pair(
            staged_png, staged_json, final_png, final_json,
            overwrite=overwrite)
    except Exception:
        _cleanup_paths(tuple(
            path for path in (staged_png, staged_json)
            if path is not None))
        raise
    return NativeSnapshotPair(
        png_path=final_png,
        json_path=final_json,
        png_sha256=png_sha,
        json_sha256=json_sha,
        png_size_bytes=png_size,
    )


def _validate_sidecar_renderer_contract(
        sidecar: Mapping[str, object], identity: Mapping[str, object]) -> None:
    renderer = sidecar.get("renderer")
    if not isinstance(renderer, Mapping):
        raise PsxNativeBatchCollisionError(
            "native sidecar lacks renderer provenance")
    try:
        _validate_identity_renderer_alignment(identity, renderer)
    except PsxNativeBatchProvenanceError as exc:
        raise PsxNativeBatchCollisionError(str(exc)) from exc
    contract = identity.get("renderer_contract")
    mesh = identity.get("mesh")
    texture = identity.get("texture")
    if not all(isinstance(value, Mapping)
               for value in (renderer, contract, mesh, texture)):
        raise PsxNativeBatchCollisionError(
            "native sidecar lacks renderer/source identity blocks")
    assert isinstance(renderer, Mapping)
    assert isinstance(contract, Mapping)
    assert isinstance(mesh, Mapping)
    assert isinstance(texture, Mapping)
    required = {
        "requested_mode": contract.get("requested_mode"),
        "effective_mode": contract.get("effective_mode"),
        "profile_id": contract.get("profile_id"),
        "profile_version": contract.get("profile_version"),
        "source_asset_pipeline": contract.get("source_asset_pipeline"),
        "native_psx_asset_decode": True,
        "pc_openua_source_used": False,
        "fallback_used": False,
        "native_mesh_sha256": mesh.get("body_sha256"),
        "native_model_slot": mesh.get("model_slot"),
        "native_model_slot_evidence_id": mesh.get(
            "model_slot_evidence_id"),
        "runtime_identity_status": mesh.get("runtime_identity_status"),
        "native_primitive_cull_census": mesh.get(
            "native_primitive_cull_census"),
        "native_raw_corner_shade_census": mesh.get(
            "native_raw_corner_shade_census"),
        "native_texture_pack_path": texture.get("logical_path"),
        "native_texture_pack_sha256": texture.get("sha256"),
        "native_texture_pack_layout_id": texture.get("layout_id"),
        "native_texture_selector_to_pixel_bank_mapping": texture.get(
            "selector_to_pixel_bank_mapping"),
        "native_texture_material_slot_count": texture.get(
            "material_slot_count"),
        "native_texture_populated_selectors": texture.get(
            "populated_selectors"),
    }
    for key, expected in required.items():
        if renderer.get(key) != expected:
            raise PsxNativeBatchCollisionError(
                f"native sidecar renderer proof mismatches {key}")


def prove_existing_native_snapshot_pair(
        png_path: str | Path,
        *,
        expected_identity: Mapping[str, object],
) -> NativeSnapshotPair | None:
    """Return proof for an exact existing pair, ``None`` if neither exists.

    A lone PNG/JSON file, malformed sidecar, identity mismatch, unreadable PNG,
    or byte-hash mismatch raises :class:`PsxNativeBatchCollisionError`.
    """

    final_png = Path(png_path)
    final_json = final_png.with_suffix(final_png.suffix + ".json")
    if final_png.is_symlink() or final_json.is_symlink():
        raise PsxNativeBatchCollisionError(
            f"native snapshot pair contains a symlink at {final_png}")
    png_exists = final_png.is_file()
    json_exists = final_json.is_file()
    if not png_exists and not json_exists:
        return None
    if png_exists != json_exists:
        raise PsxNativeBatchCollisionError(
            f"partial native snapshot pair at {final_png}")
    if final_json.stat().st_size > _SIDECAR_LIMIT:
        raise PsxNativeBatchCollisionError(
            f"native snapshot sidecar is unexpectedly large: {final_json}")
    try:
        sidecar = json.loads(final_json.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PsxNativeBatchCollisionError(
            f"native snapshot sidecar is unreadable: {exc}") from exc
    if not isinstance(sidecar, dict) \
            or sidecar.get("schema_id") != PSX_NATIVE_SNAPSHOT_SCHEMA \
            or sidecar.get("schema_version") \
            != PSX_NATIVE_SNAPSHOT_SCHEMA_VERSION:
        raise PsxNativeBatchCollisionError(
            "existing sidecar is not the native snapshot schema")
    if set(sidecar) != {
            "schema_id", "schema_version", "identity", "renderer",
            "artifact"}:
        raise PsxNativeBatchCollisionError(
            "existing native sidecar has an inexact field set")
    expected = _json_clone(expected_identity, label="expected identity")
    if sidecar.get("identity") != expected:
        raise PsxNativeBatchCollisionError(
            "existing native snapshot identity does not match this batch")
    _validate_sidecar_renderer_contract(sidecar, expected)
    artifact = sidecar.get("artifact")
    if not isinstance(artifact, Mapping):
        raise PsxNativeBatchCollisionError(
            "existing native sidecar lacks byte-derived artifact proof")
    if set(artifact) != {
            "png_file", "png_sha256", "png_size_bytes", "width", "height"}:
        raise PsxNativeBatchCollisionError(
            "existing native artifact proof has an inexact field set")
    png_sha = _sha256_file(final_png)
    png_size = final_png.stat().st_size
    if artifact.get("png_file") != final_png.name \
            or artifact.get("png_sha256") != png_sha \
            or artifact.get("png_size_bytes") != png_size:
        raise PsxNativeBatchCollisionError(
            "existing native PNG bytes do not match their sidecar")
    output = expected.get("output")
    if not isinstance(output, Mapping):
        raise PsxNativeBatchCollisionError(
            "expected identity lacks output dimensions")
    reader = QImageReader(str(final_png), b"png")
    size = reader.size()
    if not size.isValid() \
            or size.width() != output.get("width") \
            or size.height() != output.get("height") \
            or artifact.get("width") != size.width() \
            or artifact.get("height") != size.height():
        raise PsxNativeBatchCollisionError(
            "existing native PNG dimensions do not match their sidecar")
    return NativeSnapshotPair(
        png_path=final_png,
        json_path=final_json,
        png_sha256=png_sha,
        json_sha256=_sha256_file(final_json),
        png_size_bytes=png_size,
    )


def _mesh_folder(mesh: PsxNativeMesh, mesh_index: int) -> str:
    digest = _hash256(mesh.body_sha256, label="native mesh body hash")[:12]
    if mesh.model_slot is not None:
        if mesh.archive_ordinal is None:
            raise PsxNativeBatchProvenanceError(
                "runtime model slot lacks its packed archive ordinal")
        return (
            f"UNIT_SLOT_{mesh.model_slot:03d}_"
            f"ORD_{mesh.archive_ordinal:03d}_{digest}")
    if mesh.archive_ordinal is not None:
        return f"UNIT_ORD_{mesh.archive_ordinal:03d}_SLOT_UNPROVEN_{digest}"
    stem = PurePosixPath(
        _logical_path(mesh.logical_path, label="native mesh path")).stem
    return f"LOOSE_{mesh_index:03d}_{_safe_component(stem, 'MESH')}_{digest}"


def _view_filename(view_index: int, view_name: str) -> str:
    slug = _safe_component(view_name.lower().replace(" ", "_"), "view")
    return f"{view_index + 1:02d}_{slug}.png"


class PsxNativeBatchExporter:
    """Step-driven complete native mesh/view exporter.

    ``start()`` freezes and collision-checks the entire plan without rendering.
    ``step()`` commits at most one new PNG/JSON pair.  Once ``done`` is true,
    ``result`` returns records in deterministic mesh-major/view-minor order.
    """

    def __init__(
            self,
            build: PsxNativeBuild,
            config: PsxNativeBatchConfig,
            *,
            viewport_factory: Callable[[], AssetViewport] = AssetViewport,
    ) -> None:
        if not isinstance(build, PsxNativeBuild):
            raise TypeError("native batch requires a PsxNativeBuild")
        if not isinstance(config, PsxNativeBatchConfig):
            raise TypeError("config must be PsxNativeBatchConfig")
        if not build.meshes:
            raise PsxNativeBatchError("native PSX build contains no meshes")
        if not callable(viewport_factory):
            raise TypeError("viewport_factory must be callable")
        if not isinstance(config.skip_existing, bool):
            raise PsxNativeBatchError("skip_existing must be a bool")
        if config.texture_pack is not None and not any(
                candidate is config.texture_pack
                for candidate in build.texture_packs):
            raise PsxNativeBatchProvenanceError(
                "selected texture pack does not belong to the active build")
        width = _plain_int(
            config.width, label="batch width", minimum=1,
            maximum=_MAX_DIMENSION)
        height = _plain_int(
            config.height, label="batch height", minimum=1,
            maximum=_MAX_DIMENSION)
        if width * height > _MAX_PIXELS:
            raise PsxNativeBatchError(
                "native batch output exceeds the pixel safety budget")
        zoom = _plain_int(
            config.zoom_percent, label="zoom_percent", minimum=25,
            maximum=300)
        if not isinstance(config.views, tuple) or not config.views:
            raise PsxNativeBatchError(
                "native batch requires one or more fixed view presets")
        if len(set(config.views)) != len(config.views):
            raise PsxNativeBatchError("native batch view presets must be unique")
        for view in config.views:
            if view not in VIEW_PRESET_ANGLES:
                raise PsxNativeBatchError(
                    f"native batch view is not deterministic: {view!r}")

        self.build = build
        self.config = PsxNativeBatchConfig(
            output_root=Path(config.output_root),
            width=width,
            height=height,
            zoom_percent=zoom,
            views=tuple(config.views),
            texture_pack=config.texture_pack,
            background_rgba=_normalize_background(config.background_rgba),
            skip_existing=config.skip_existing,
        )
        self.output_root = _resolve_output_root_outside_source(
            self.build, self.config.output_root)
        self.manifest_path = (
            self.output_root / PSX_NATIVE_BATCH_MANIFEST_FILENAME)
        _validate_windows_path_budget(
            self.manifest_path, label="native batch manifest path")
        self._viewport_factory = viewport_factory
        self._viewport: AssetViewport | None = None
        self._jobs = self._build_plan()
        self._pending: list[_NativeBatchJob] = []
        self._records: dict[tuple[int, int], PsxNativeBatchRecord] = {}
        self._pending_index = 0
        self._state: Literal[
            "idle", "running", "complete", "cancelled", "failed"] = "idle"
        self._cancel_requested = False
        self._current_relative_png: str | None = None
        self._manifest_sha256: str | None = None

    def _build_plan(self) -> tuple[_NativeBatchJob, ...]:
        jobs: list[_NativeBatchJob] = []
        target_keys: set[str] = set()
        for mesh_index, mesh in enumerate(self.build.meshes):
            folder = self.output_root / _mesh_folder(mesh, mesh_index)
            for view_index, view_name in enumerate(self.config.views):
                angles = tuple(float(value)
                               for value in VIEW_PRESET_ANGLES[view_name])
                png_path = folder / _view_filename(view_index, view_name)
                _validate_windows_path_budget(
                    png_path, label="native batch PNG path")
                _validate_windows_path_budget(
                    png_path.with_suffix(".png.json"),
                    label="native batch sidecar path")
                # Existing symlinked folders cannot redirect a bounded batch
                # target outside the approved output root.
                resolved_target = png_path.resolve(strict=False)
                try:
                    resolved_target.relative_to(self.output_root)
                except ValueError as exc:
                    raise PsxNativeBatchError(
                        "native batch target escapes its output root") from exc
                key_text = str(resolved_target).casefold()
                if key_text in target_keys:
                    raise PsxNativeBatchError(
                        f"native batch target collision: {png_path}")
                target_keys.add(key_text)
                identity = build_native_snapshot_identity(
                    self.build, mesh, self.config.texture_pack,
                    view_name=view_name,
                    view_angles=angles,
                    width=self.config.width,
                    height=self.config.height,
                    zoom_percent=self.config.zoom_percent,
                    background_rgba=self.config.background_rgba,
                    camera_state=_frozen_batch_camera_state(
                        mesh,
                        angles,
                        width=self.config.width,
                        height=self.config.height,
                        zoom_percent=self.config.zoom_percent,
                    ),
                    guides=False,
                    capture_profile_id=PSX_NATIVE_BATCH_PROFILE_ID,
                )
                jobs.append(_NativeBatchJob(
                    key=(mesh_index, view_index),
                    mesh_index=mesh_index,
                    view_index=view_index,
                    mesh=mesh,
                    view_name=view_name,
                    view_angles=angles,
                    png_path=png_path,
                    identity=identity,
                ))
        return tuple(jobs)

    @property
    def state(self) -> str:
        return self._state

    @property
    def done(self) -> bool:
        return self._state in {"complete", "cancelled", "failed"}

    @property
    def progress(self) -> PsxNativeBatchProgress:
        written = sum(record.status == "WRITTEN"
                      for record in self._records.values())
        skipped = sum(record.status == "SKIPPED_VERIFIED"
                      for record in self._records.values())
        return PsxNativeBatchProgress(
            state=self._state,
            total=len(self._jobs),
            completed=len(self._records),
            written=written,
            skipped_verified=skipped,
            cancelled=self._state == "cancelled",
            current_relative_png=self._current_relative_png,
        )

    @property
    def result(self) -> PsxNativeBatchResult:
        if self._state not in {"complete", "cancelled"}:
            raise PsxNativeBatchError(
                "native batch result is available only after completion or "
                "cancellation")
        ordered = tuple(
            self._records[job.key]
            for job in self._jobs if job.key in self._records)
        if self._manifest_sha256 is None or not self.manifest_path.is_file():
            raise PsxNativeBatchError(
                "terminal native batch lacks its atomic run manifest")
        return PsxNativeBatchResult(
            cancelled=self._state == "cancelled",
            total=len(self._jobs),
            written=sum(record.status == "WRITTEN" for record in ordered),
            skipped_verified=sum(
                record.status == "SKIPPED_VERIFIED" for record in ordered),
            records=ordered,
            manifest_path=self.manifest_path,
            manifest_sha256=self._manifest_sha256,
        )

    def request_cancel(self) -> None:
        """Request cancellation before the next image transaction starts."""

        self._cancel_requested = True

    def _record_from_pair(
            self, job: _NativeBatchJob, pair: NativeSnapshotPair,
            status: Literal["WRITTEN", "SKIPPED_VERIFIED"]) \
            -> PsxNativeBatchRecord:
        return PsxNativeBatchRecord(
            mesh_index=job.mesh_index,
            model_slot=job.mesh.model_slot,
            archive_ordinal=job.mesh.archive_ordinal,
            native_asset_path=_logical_path(
                job.mesh.logical_path, label="native mesh path"),
            view_name=job.view_name,
            relative_png=job.png_path.relative_to(
                self.output_root).as_posix(),
            relative_json=pair.json_path.relative_to(
                self.output_root).as_posix(),
            status=status,
            png_sha256=pair.png_sha256,
            json_sha256=pair.json_sha256,
            png_size_bytes=pair.png_size_bytes,
        )

    def _preflight_manifest_target(self) -> None:
        if self.output_root.exists() and not self.output_root.is_dir():
            raise PsxNativeBatchError(
                f"native batch output root is not a directory: "
                f"{self.output_root}")
        if self.manifest_path.exists() and self.manifest_path.is_dir():
            raise PsxNativeBatchCollisionError(
                "native batch manifest target is a directory")
        if self.manifest_path.is_symlink():
            raise PsxNativeBatchCollisionError(
                "native batch manifest target is a symlink")

    def _terminal_manifest(
            self, terminal_state: Literal["complete", "cancelled"]
    ) -> dict[str, object]:
        ordered_jobs = [
            job for job in self._jobs if job.key in self._records]
        records = []
        for job in ordered_jobs:
            record = self._records[job.key]
            mesh_identity = job.identity["mesh"]
            assert isinstance(mesh_identity, Mapping)
            records.append({
                "mesh_index": record.mesh_index,
                "mesh": {
                    "logical_path": record.native_asset_path,
                    "model_slot": record.model_slot,
                    "archive_ordinal": record.archive_ordinal,
                    "model_slot_evidence_id": mesh_identity.get(
                        "model_slot_evidence_id"),
                    "archive_offset": mesh_identity.get("archive_offset"),
                    "archive_sector": mesh_identity.get("archive_sector"),
                    "format": mesh_identity.get("format"),
                    "format_version": mesh_identity.get("format_version"),
                    "body_sha256": mesh_identity.get("body_sha256"),
                    "vertex_stream_sha256": mesh_identity.get(
                        "vertex_stream_sha256"),
                    "face_stream_sha256": mesh_identity.get(
                        "face_stream_sha256"),
                    "native_primitive_cull_census": mesh_identity.get(
                        "native_primitive_cull_census"),
                    "native_raw_corner_shade_census": mesh_identity.get(
                        "native_raw_corner_shade_census"),
                },
                "view": {
                    "name": record.view_name,
                    "yaw_degrees": job.view_angles[0],
                    "pitch_degrees": job.view_angles[1],
                },
                "relative_png": record.relative_png,
                "relative_json": record.relative_json,
                "status": record.status,
                "artifact": {
                    "png_sha256": record.png_sha256,
                    "json_sha256": record.json_sha256,
                    "png_size_bytes": record.png_size_bytes,
                },
            })
        first_identity = self._jobs[0].identity
        return {
            "schema_id": PSX_NATIVE_BATCH_MANIFEST_SCHEMA,
            "schema_version": PSX_NATIVE_BATCH_MANIFEST_SCHEMA_VERSION,
            "batch_profile_id": PSX_NATIVE_BATCH_PROFILE_ID,
            "terminal_state": terminal_state,
            "cancelled": terminal_state == "cancelled",
            "source": _json_clone(
                first_identity["source"], label="manifest source"),
            "renderer_contract": _json_clone(
                first_identity["renderer_contract"],
                label="manifest renderer contract"),
            "config": {
                "width": self.config.width,
                "height": self.config.height,
                "zoom_percent": self.config.zoom_percent,
                "views": [
                    {
                        "name": name,
                        "yaw_degrees": float(VIEW_PRESET_ANGLES[name][0]),
                        "pitch_degrees": float(VIEW_PRESET_ANGLES[name][1]),
                    }
                    for name in self.config.views
                ],
                "texture": _json_clone(
                    first_identity["texture"],
                    label="manifest texture selection"),
                "background": _json_clone(
                    first_identity["output"]["background"],
                    label="manifest background"),
                "format": "PNG",
                "guides": False,
                "animation": "disabled_initial_frame",
                "skip_existing": self.config.skip_existing,
            },
            "plan": {
                "mesh_count": len(self.build.meshes),
                "view_count": len(self.config.views),
                "total": len(self._jobs),
            },
            "execution": {
                "completed": len(records),
                "remaining": len(self._jobs) - len(records),
                "written": sum(
                    record["status"] == "WRITTEN" for record in records),
                "skipped_verified": sum(
                    record["status"] == "SKIPPED_VERIFIED"
                    for record in records),
            },
            "records": records,
        }

    def _finish(
            self, terminal_state: Literal["complete", "cancelled"]
    ) -> PsxNativeBatchProgress:
        self._current_relative_png = None
        try:
            # Cancellation and skip-only batches never visit the per-image
            # parent guard.  Recheck before even creating the output root so
            # an absent path swapped for a junction cannot create directories
            # inside the immutable source tree.
            validate_native_output_commit_boundary(
                self.build.root, self.manifest_path)
            self.output_root.mkdir(parents=True, exist_ok=True)
            self._preflight_manifest_target()
            document = self._terminal_manifest(terminal_state)
            self._manifest_sha256 = _write_atomic_json_document(
                self.manifest_path, document,
                source_root=self.build.root)
            self._state = terminal_state
        except Exception:
            self._state = "failed"
            raise
        finally:
            self._dispose_viewport()
        return self.progress

    def start(self) -> PsxNativeBatchProgress:
        """Freeze/collision-check jobs without rendering any image.

        A pre-requested cancellation skips pair hashing and writes an empty
        deterministic cancelled manifest.  A fully verified skip-only batch
        writes its complete terminal manifest here.
        """

        if self._state != "idle":
            raise PsxNativeBatchError("native batch has already been started")
        try:
            self._preflight_manifest_target()
            if self._cancel_requested:
                return self._finish("cancelled")
            for job in self._jobs:
                pair = (
                    prove_existing_native_snapshot_pair(
                        job.png_path, expected_identity=job.identity)
                    if self.config.skip_existing else None)
                if pair is None:
                    self._pending.append(job)
                else:
                    self._records[job.key] = self._record_from_pair(
                        job, pair, "SKIPPED_VERIFIED")
        except Exception:
            self._state = "failed"
            raise
        if self._cancel_requested:
            return self._finish("cancelled")
        if not self._pending:
            return self._finish("complete")
        self._state = "running"
        return self.progress

    def _ensure_job_parent(self, job: _NativeBatchJob) -> None:
        parent = job.png_path.parent
        resolved = parent.resolve(strict=False)
        try:
            resolved.relative_to(self.output_root)
        except ValueError as exc:
            raise PsxNativeBatchError(
                "native batch output folder escapes the output root") from exc
        if parent.exists() and (
                parent.is_symlink()
                or bool(getattr(parent, "is_junction", lambda: False)())):
            raise PsxNativeBatchError(
                f"native batch output folder is a symlink or junction: {parent}")
        parent.mkdir(parents=True, exist_ok=True)
        # Re-resolve after creation to close a redirection race detectable at
        # the Python filesystem boundary.
        try:
            parent.resolve().relative_to(self.output_root)
        except ValueError as exc:
            raise PsxNativeBatchError(
                "native batch output folder redirected outside its root") from exc

    def _get_viewport(self) -> AssetViewport:
        if self._viewport is None:
            viewport = self._viewport_factory()
            if not isinstance(viewport, AssetViewport):
                raise TypeError(
                    "viewport_factory must return an AssetViewport")
            self._viewport = viewport
        return self._viewport

    def _dispose_viewport(self) -> None:
        viewport = self._viewport
        self._viewport = None
        if viewport is None:
            return
        try:
            viewport.play_animation(False)
            if getattr(viewport, "_snapshot_active", False):
                viewport.end_snapshot_mode()
        finally:
            viewport.deleteLater()

    def _render_job(self, job: _NativeBatchJob) -> NativeSnapshotPair:
        self._ensure_job_parent(job)
        viewport = self._get_viewport()
        viewport.load_psx_mesh(
            self.build, job.mesh,
            texture_pack=self.config.texture_pack)
        viewport.begin_snapshot_mode(None)
        try:
            viewport.play_animation(False)
            viewport.set_snapshot_guides_visible(False)
            viewport.apply_snapshot_preset(
                job.view_name,
                QSize(self.config.width, self.config.height),
                self.config.zoom_percent,
            )
            view_identity = job.identity.get("view")
            expected_camera = (
                view_identity.get("camera_state")
                if isinstance(view_identity, Mapping) else None)
            actual_camera = _normalize_camera_state(
                viewport.snapshot_camera_info)
            if actual_camera != expected_camera:
                raise PsxNativeBatchProvenanceError(
                    "native batch camera does not match its frozen preset "
                    "identity")
            background = (
                QColor(*self.config.background_rgba)
                if self.config.background_rgba is not None else None)
            image = viewport.render_snapshot(
                QSize(self.config.width, self.config.height),
                background,
                include_guides=False,
            )
            if image.isNull():
                raise PsxNativeBatchError(
                    "native renderer returned a null snapshot")
            renderer = validate_native_renderer_info(
                viewport.renderer_info,
                build=self.build,
                mesh=job.mesh,
                texture_pack=self.config.texture_pack,
            )
            metadata = build_native_snapshot_sidecar(
                identity=job.identity, renderer=renderer)
            return write_atomic_png_json_pair(
                image, job.png_path, metadata,
                overwrite=not self.config.skip_existing,
                source_root=self.build.root)
        finally:
            viewport.end_snapshot_mode()

    def step(self) -> PsxNativeBatchProgress:
        """Render/commit at most one pending pair and return current progress."""

        if self._state == "idle":
            self.start()
        if self._state in {"complete", "cancelled"}:
            return self.progress
        if self._state == "failed":
            raise PsxNativeBatchError("native batch is in a failed state")
        if self._cancel_requested:
            return self._finish("cancelled")
        if self._pending_index >= len(self._pending):
            return self._finish("complete")

        job = self._pending[self._pending_index]
        self._current_relative_png = job.png_path.relative_to(
            self.output_root).as_posix()
        try:
            pair = self._render_job(job)
            self._records[job.key] = self._record_from_pair(
                job, pair, "WRITTEN")
            self._pending_index += 1
        except Exception:
            self._state = "failed"
            self._current_relative_png = None
            self._dispose_viewport()
            raise
        if self._cancel_requested:
            return self._finish("cancelled")
        elif self._pending_index >= len(self._pending):
            return self._finish("complete")
        return self.progress

    def run(
            self,
            on_progress: Callable[[PsxNativeBatchProgress], None] | None = None,
    ) -> PsxNativeBatchResult:
        """Synchronous convenience wrapper around ``start``/``step``."""

        progress = self.start()
        if on_progress is not None:
            on_progress(progress)
        while not self.done:
            progress = self.step()
            if on_progress is not None:
                on_progress(progress)
        return self.result


__all__ = [
    "DEFAULT_PSX_NATIVE_BATCH_VIEWS",
    "NativeSnapshotPair",
    "PSX_NATIVE_BATCH_MANIFEST_FILENAME",
    "PSX_NATIVE_BATCH_MANIFEST_SCHEMA",
    "PSX_NATIVE_BATCH_MANIFEST_SCHEMA_VERSION",
    "PSX_NATIVE_BATCH_PROFILE_ID",
    "PSX_NATIVE_MANUAL_PROFILE_ID",
    "PSX_NATIVE_SNAPSHOT_SCHEMA",
    "PSX_NATIVE_SNAPSHOT_SCHEMA_VERSION",
    "PsxNativeAtomicWriteError",
    "PsxNativeBatchCollisionError",
    "PsxNativeBatchConfig",
    "PsxNativeBatchError",
    "PsxNativeBatchExporter",
    "PsxNativeBatchProgress",
    "PsxNativeBatchProvenanceError",
    "PsxNativeBatchRecord",
    "PsxNativeBatchResult",
    "build_native_snapshot_identity",
    "build_native_snapshot_sidecar",
    "prove_existing_native_snapshot_pair",
    "validate_native_output_commit_boundary",
    "validate_native_renderer_info",
    "write_atomic_png_json_pair",
]
