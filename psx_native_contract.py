"""Central fail-closed contracts for frozen native PlayStation objects.

The native parsers deliberately remain independent of this module.  Viewer
and exporter entry points can call :func:`validate_psx_native_build` before
serializing provenance, without making ``psx_native_assets`` import back into
this validation layer.

Only relationships represented by the frozen public objects are checked
here.  In particular, the compact model-slot evidence can prove the complete
set of non-empty table slots, but this module does not rediscover an
allocation table or infer identities for loose meshes.
"""

from __future__ import annotations

import math
import re
import struct

from psx_native_assets import (
    MESH_HEADER_SIZE,
    PSW_FACE_SIZE,
    PW3_FACE_SIZE,
    SECTOR_SIZE,
    VERTEX_SIZE,
    PsxNativeBuild,
    PsxNativeFace,
    PsxNativeMesh,
    PsxNativeModelSlotEvidence,
)
from psx_native_effects import (
    PV2_BINDING_STATUS,
    PV2_FORMAT_VERSION,
    PV2_PARSER_ID,
    PV2_PARSER_VERSION,
    PsxPv2Mesh,
)
from psx_native_identity import PSX_MODEL_SLOT_EVIDENCE_ID
from psx_psw_pipeline import (
    PsxPswPipelineError,
    psw_material_local_uv_quotient,
)
from psx_native_textures import (
    CLUT_ENTRY_COUNT,
    FULL_RECORD_SIZE,
    INDEXED_PIXEL_BYTES,
    LATE_SELECTOR_TO_PIXEL_BANK_MAPPING,
    LATE_SETGFX_LAYOUT_ID,
    LATE_SETGFX_SIZE,
    MATERIAL_SLOT_COUNT,
    PALETTE_RECORD_SIZE,
    PIXEL_BANK_COUNT,
    REPEAT_RECORD_HEADER,
    SECTOR_PADDED_EMPTY_ALLOCATION,
    SECTOR_PADDED_EMPTY_MARKER,
    SECTOR_PADDED_FULL_ALLOCATION,
    SECTOR_PADDED_OBSERVED_LAYOUTS,
    SECTOR_PADDED_SELECTOR_TO_PIXEL_BANK_MAPPING,
    SECTOR_PADDED_SETGFX_LAYOUT_ID,
    ZERO_RECORD_HEADER,
    PsxBgr555Color,
    PsxNativeTexturePack,
    PsxNativeTextureSlot,
)


_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_MESH_FORMAT_BY_VERSION = {
    1: "PSW/PSV",
    3: "PW3",
}
_SIGNED_I32_MIN = -(1 << 31)
_SIGNED_I32_MAX = (1 << 31) - 1


class PsxNativeContractError(ValueError):
    """Raised when a frozen native object violates its public contract."""


def _plain_int(
        value: object, *, label: str, minimum: int | None = None,
        maximum: int | None = None) -> int:
    if type(value) is not int:
        raise PsxNativeContractError(f"{label} must be a plain integer")
    if minimum is not None and value < minimum:
        raise PsxNativeContractError(
            f"{label} must be at least {minimum}")
    if maximum is not None and value > maximum:
        raise PsxNativeContractError(
            f"{label} must be at most {maximum}")
    return value


def _sha256(value: object, *, label: str) -> str:
    if type(value) is not str or _SHA256_RE.fullmatch(value) is None:
        raise PsxNativeContractError(
            f"{label} must be an exact lowercase SHA-256 value")
    return value


def _logical_path(value: object, *, label: str) -> str:
    if type(value) is not str or not value:
        raise PsxNativeContractError(
            f"{label} must be a non-empty logical path")
    if "\\" in value:
        raise PsxNativeContractError(
            f"{label} must use forward slashes")
    if value.startswith("/"):
        raise PsxNativeContractError(
            f"{label} must be relative to the selected PSX source")
    parts = value.split("/")
    if any(
            part in {"", ".", ".."}
            or ":" in part
            or _CONTROL_RE.search(part) is not None
            for part in parts):
        raise PsxNativeContractError(
            f"{label} contains an unsafe path component")
    return value


def _optional_path_hash_pair(
        path: object, digest: object, *, label: str) -> None:
    if (path is None) != (digest is None):
        raise PsxNativeContractError(
            f"{label} logical path and SHA-256 must be present together")
    if path is not None:
        _logical_path(path, label=f"{label} logical path")
        _sha256(digest, label=f"{label} SHA-256")


def _validate_build_provenance(build: PsxNativeBuild) -> None:
    _logical_path(
        build.system_cnf_logical_path,
        label="SYSTEM.CNF logical path",
    )
    _sha256(build.system_cnf_sha256, label="SYSTEM.CNF SHA-256")
    _logical_path(
        build.boot_executable_logical_path,
        label="boot executable logical path",
    )
    _sha256(
        build.boot_executable_sha256,
        label="boot executable SHA-256",
    )
    _optional_path_hash_pair(
        build.unit_archive_logical_path,
        build.unit_archive_sha256,
        label="UNIT.BIN",
    )
    _optional_path_hash_pair(
        build.vehicle_roster_logical_path,
        build.vehicle_roster_sha256,
        label="vehicle roster",
    )
    if type(build.vehicle_roster) is not tuple:
        raise PsxNativeContractError("vehicle roster must be a tuple")
    for entry_index, entry in enumerate(build.vehicle_roster):
        if type(entry) is not str:
            raise PsxNativeContractError(
                f"vehicle roster entry {entry_index} must be text")
    if build.vehicle_roster_logical_path is None and build.vehicle_roster:
        raise PsxNativeContractError(
            "vehicle roster entries require frozen roster provenance")


def _validate_psw_uv_tuple(value: object, *, label: str) -> None:
    if type(value) is not tuple or len(value) != 4:
        raise PsxNativeContractError(
            f"{label} must be a present four-corner tuple")
    for corner_index, uv in enumerate(value):
        corner_label = f"{label}[{corner_index}]"
        if type(uv) is not tuple or len(uv) != 2:
            raise PsxNativeContractError(
                f"{corner_label} must be an exact two-component tuple")
        for component_index, component in enumerate(uv):
            signed_component = _plain_int(
                component,
                label=f"{corner_label}[{component_index}]",
                minimum=_SIGNED_I32_MIN,
                maximum=_SIGNED_I32_MAX,
            )
            try:
                psw_material_local_uv_quotient(signed_component)
            except PsxPswPipelineError as exc:
                raise PsxNativeContractError(
                    f"{corner_label}[{component_index}] cannot enter the "
                    f"material-local preview: {exc}") from exc


def _validate_raw_face_contract(
        face: PsxNativeFace, *, face_index: int, vertex_count: int,
        version: int) -> None:
    label = f"native face {face_index}"
    expected_record_size = PSW_FACE_SIZE if version == 1 else PW3_FACE_SIZE
    expected_prefix_size = 8 if version == 1 else 4
    if type(face.raw_record) is not bytes \
            or len(face.raw_record) != expected_record_size:
        raise PsxNativeContractError(
            f"{label} raw record must be exactly {expected_record_size} bytes")
    if type(face.opaque_prefix) is not bytes \
            or len(face.opaque_prefix) != expected_prefix_size:
        raise PsxNativeContractError(
            f"{label} opaque prefix must be exactly "
            f"{expected_prefix_size} bytes")
    if face.opaque_prefix != face.raw_record[:expected_prefix_size]:
        raise PsxNativeContractError(
            f"{label} opaque prefix must match its raw record")

    if type(face.raw_vertex_indices) is not tuple \
            or len(face.raw_vertex_indices) != 4:
        raise PsxNativeContractError(
            f"{label} raw vertex indices must be an exact four-item tuple")
    for corner_index, vertex_index in enumerate(face.raw_vertex_indices):
        _plain_int(
            vertex_index,
            label=f"{label} raw vertex index {corner_index}",
            minimum=0,
            maximum=vertex_count - 1,
        )

    if type(face.raw_uv_bytes) is not tuple \
            or len(face.raw_uv_bytes) != 4:
        raise PsxNativeContractError(
            f"{label} raw UVs must be an exact four-item tuple")
    for corner_index, uv in enumerate(face.raw_uv_bytes):
        if type(uv) is not tuple or len(uv) != 2:
            raise PsxNativeContractError(
                f"{label} raw UV {corner_index} must be a two-item tuple")
        for component_index, component in enumerate(uv):
            _plain_int(
                component,
                label=(f"{label} raw UV {corner_index} component "
                       f"{component_index}"),
                minimum=0,
                maximum=0xFF,
            )

    if type(face.raw_corner_shades) is not tuple \
            or len(face.raw_corner_shades) != 4:
        raise PsxNativeContractError(
            f"{label} raw corner shades must be an exact four-item tuple")
    for corner_index, shade in enumerate(face.raw_corner_shades):
        _plain_int(
            shade,
            label=f"{label} raw corner shade {corner_index}",
            minimum=0,
            maximum=0xFF,
        )
    _plain_int(
        face.texture_selector,
        label=f"{label} texture selector",
        minimum=0,
        maximum=0xFFFF,
    )

    if version == 3:
        record_indices = struct.unpack_from("<4H", face.raw_record, 4)
        record_uvs = tuple(
            (face.raw_record[12 + corner * 2],
             face.raw_record[13 + corner * 2])
            for corner in range(4)
        )
        record_selector = struct.unpack_from("<H", face.raw_record, 20)[0]
        record_shades = tuple(face.raw_record[22:26])
        record_flags = struct.unpack_from("<H", face.raw_record, 0)[0]
        if face.pw3_primitive_flags != record_flags:
            raise PsxNativeContractError(
                f"{label} PW3 flags must match its raw record")
    else:
        record_indices = struct.unpack_from("<4I", face.raw_record, 8)
        fixed_components = struct.unpack_from("<8i", face.raw_record, 24)
        record_fixed_uvs = tuple(
            (fixed_components[corner * 2], fixed_components[corner * 2 + 1])
            for corner in range(4)
        )
        if face.psw_uv_fixed_16_16 != record_fixed_uvs:
            raise PsxNativeContractError(
                f"{label} signed PSW UVs must match its raw record")
        compatibility_components: list[int] = []
        for component in fixed_components:
            if component & 0xFFFF:
                raise PsxNativeContractError(
                    f"{label} raw PSW UVs must be integral 16.16")
            integer = component >> 16
            if not -1 <= integer <= 0xFF:
                raise PsxNativeContractError(
                    f"{label} raw PSW UVs are outside the authored domain")
            compatibility_components.append(
                0xFF if integer == -1 else integer)
        record_uvs = tuple(
            (compatibility_components[corner * 2],
             compatibility_components[corner * 2 + 1])
            for corner in range(4)
        )
        record_selector = struct.unpack_from("<I", face.raw_record, 56)[0]
        fixed_shades = struct.unpack_from("<4I", face.raw_record, 60)
        if any(value & 0xFFFF or value >> 16 > 0xFF
               for value in fixed_shades):
            raise PsxNativeContractError(
                f"{label} raw PSW shades must be integral authored bytes")
        record_shades = tuple(value >> 16 for value in fixed_shades)

    if face.raw_vertex_indices != record_indices:
        raise PsxNativeContractError(
            f"{label} raw vertex indices must match its raw record")
    if face.raw_uv_bytes != record_uvs:
        raise PsxNativeContractError(
            f"{label} raw UVs must match its raw record")
    if face.texture_selector != record_selector:
        raise PsxNativeContractError(
            f"{label} texture selector must match its raw record")
    if face.raw_corner_shades != record_shades:
        raise PsxNativeContractError(
            f"{label} raw corner shades must match its raw record")

    collapsed_indices: list[int] = []
    collapsed_uvs: list[tuple[int, int]] = []
    collapsed_shades: list[int] = []
    seen_indices: set[int] = set()
    for vertex_index, uv, shade in zip(
            record_indices, record_uvs, record_shades, strict=True):
        if vertex_index in seen_indices:
            continue
        seen_indices.add(vertex_index)
        collapsed_indices.append(vertex_index)
        collapsed_uvs.append(uv)
        collapsed_shades.append(shade)
    if len(collapsed_indices) not in (3, 4):
        raise PsxNativeContractError(
            f"{label} must collapse to exactly three or four unique corners")
    if type(face.vertex_indices) is not tuple \
            or face.vertex_indices != tuple(collapsed_indices):
        raise PsxNativeContractError(
            f"{label} collapsed vertex indices do not match raw corners")
    if type(face.uv_bytes) is not tuple \
            or face.uv_bytes != tuple(collapsed_uvs):
        raise PsxNativeContractError(
            f"{label} collapsed UVs do not match raw corners")
    if type(face.corner_shades) is not tuple \
            or face.corner_shades != tuple(collapsed_shades):
        raise PsxNativeContractError(
            f"{label} collapsed shades do not match raw corners")


def _validate_mesh_vertices(mesh: PsxNativeMesh) -> None:
    if type(mesh.raw_vertices) is not tuple \
            or type(mesh.vertices) is not tuple \
            or not mesh.vertices \
            or len(mesh.raw_vertices) != len(mesh.vertices):
        raise PsxNativeContractError(
            "native mesh raw/decoded vertex tuples must be nonempty and "
            "have equal cardinality")
    for vertex_index, (raw_vertex, decoded_vertex) in enumerate(zip(
            mesh.raw_vertices, mesh.vertices, strict=True)):
        label = f"native vertex {vertex_index}"
        if type(raw_vertex) is not tuple or len(raw_vertex) != 3:
            raise PsxNativeContractError(
                f"{label} raw value must be an exact three-item tuple")
        if type(decoded_vertex) is not tuple or len(decoded_vertex) != 3:
            raise PsxNativeContractError(
                f"{label} decoded value must be an exact three-item tuple")
        for component_index, (raw_component, decoded_component) in enumerate(
                zip(raw_vertex, decoded_vertex, strict=True)):
            component_label = f"{label} component {component_index}"
            raw_value = _plain_int(
                raw_component,
                label=f"{component_label} raw value",
                minimum=_SIGNED_I32_MIN,
                maximum=_SIGNED_I32_MAX,
            )
            if isinstance(decoded_component, bool) or not isinstance(
                    decoded_component, (int, float)):
                raise PsxNativeContractError(
                    f"{component_label} decoded value must be finite")
            try:
                finite = math.isfinite(decoded_component)
            except (OverflowError, TypeError):
                finite = False
            if not finite:
                raise PsxNativeContractError(
                    f"{component_label} decoded value must be finite")
            if decoded_component != raw_value / 65536.0:
                raise PsxNativeContractError(
                    f"{component_label} decoded value must exactly equal "
                    "raw / 65536.0")


def validate_psx_native_mesh(mesh: PsxNativeMesh) -> PsxNativeMesh:
    """Validate the exact version/format and per-face public-field pairing."""

    if type(mesh) is not PsxNativeMesh:
        raise PsxNativeContractError(
            "native mesh must be an exact PsxNativeMesh object")
    _logical_path(mesh.logical_path, label="native mesh logical path")
    _sha256(mesh.body_sha256, label="native mesh body SHA-256")
    _sha256(
        mesh.vertex_stream_sha256,
        label="native mesh vertex-stream SHA-256",
    )
    _sha256(
        mesh.face_stream_sha256,
        label="native mesh face-stream SHA-256",
    )
    _validate_mesh_vertices(mesh)
    version = _plain_int(mesh.format_version, label="native mesh version")
    expected_format = _MESH_FORMAT_BY_VERSION.get(version)
    if expected_format is None or mesh.format_id != expected_format:
        raise PsxNativeContractError(
            "native mesh format/version must be exactly "
            "v1 PSW/PSV or v3 PW3")
    if type(mesh.faces) is not tuple:
        raise PsxNativeContractError("native mesh faces must be a tuple")
    if not mesh.faces:
        raise PsxNativeContractError("native mesh faces must be nonempty")

    face_size = PSW_FACE_SIZE if version == 1 else PW3_FACE_SIZE
    face_stream_offset = MESH_HEADER_SIZE + len(mesh.vertices) * VERTEX_SIZE

    for face_index, face in enumerate(mesh.faces):
        if type(face) is not PsxNativeFace:
            raise PsxNativeContractError(
                f"native face {face_index} must be an exact "
                "PsxNativeFace object")
        expected_source_offset = face_stream_offset + face_index * face_size
        if _plain_int(
                face.source_offset,
                label=f"native face {face_index} source offset",
                minimum=0) != expected_source_offset:
            raise PsxNativeContractError(
                f"native face {face_index} source offset does not match its "
                "stream position")
        if version == 1:
            if face.pw3_primitive_flags is not None:
                raise PsxNativeContractError(
                    f"PSW/PSV face {face_index} must not carry PW3 flags")
            _validate_psw_uv_tuple(
                face.psw_uv_fixed_16_16,
                label=f"PSW/PSV face {face_index} signed UVs",
            )
        else:
            _plain_int(
                face.pw3_primitive_flags,
                label=f"PW3 face {face_index} primitive flags",
                minimum=0,
                maximum=0xFFFF,
            )
            if type(face.psw_uv_fixed_16_16) is not tuple \
                    or face.psw_uv_fixed_16_16:
                raise PsxNativeContractError(
                    f"PW3 face {face_index} must have an empty PSW UV tuple")
        _validate_raw_face_contract(
            face,
            face_index=face_index,
            vertex_count=len(mesh.vertices),
            version=version,
        )

    expected_body_size = (
        face_stream_offset + len(mesh.faces) * face_size + 3) & ~3
    if _plain_int(
            mesh.body_size, label="native mesh body size", minimum=1) != \
            expected_body_size:
        raise PsxNativeContractError(
            "native mesh body size does not match its vertex/face streams")

    return mesh


def validate_psx_native_effect_inventory(
        effects: tuple[PsxPv2Mesh, ...]) -> tuple[PsxPv2Mesh, ...]:
    """Validate the static PV2 contract and canonical inventory ordering."""

    if type(effects) is not tuple:
        raise PsxNativeContractError("native PV2 effects must be a tuple")

    paths: list[str] = []
    folded_paths: set[str] = set()
    for effect_index, effect in enumerate(effects):
        if type(effect) is not PsxPv2Mesh:
            raise PsxNativeContractError(
                f"native effect {effect_index} must be an exact PsxPv2Mesh")
        _logical_path(
            effect.logical_path,
            label=f"native effect {effect_index} logical path",
        )
        _sha256(
            effect.source_sha256,
            label=f"native effect {effect_index} source SHA-256",
        )
        _sha256(
            effect.vertex_stream_sha256,
            label=f"native effect {effect_index} vertex-stream SHA-256",
        )
        _sha256(
            effect.face_stream_sha256,
            label=f"native effect {effect_index} face-stream SHA-256",
        )
        if effect.format_id != "PV2" \
                or type(effect.format_version) is not int \
                or effect.format_version != PV2_FORMAT_VERSION:
            raise PsxNativeContractError(
                f"native effect {effect_index} must be exactly PV2 v1")
        if effect.parser_id != PV2_PARSER_ID \
                or type(effect.parser_version) is not int \
                or effect.parser_version != PV2_PARSER_VERSION:
            raise PsxNativeContractError(
                f"native effect {effect_index} does not carry the canonical "
                "PV2 parser identity")
        if effect.binding_status != PV2_BINDING_STATUS:
            raise PsxNativeContractError(
                f"native effect {effect_index} must remain "
                f"{PV2_BINDING_STATUS}")
        folded = effect.logical_path.casefold()
        if folded in folded_paths:
            raise PsxNativeContractError(
                "native PV2 logical paths must be case-insensitively unique")
        folded_paths.add(folded)
        paths.append(effect.logical_path)

    expected_paths = sorted(paths, key=lambda path: (path.casefold(), path))
    if paths != expected_paths:
        raise PsxNativeContractError(
            "native PV2 logical paths must be in canonical "
            "case-insensitive order")
    return effects


def _validate_texture_palette(
        slot: PsxNativeTextureSlot, *, label: str,
        required: bool) -> None:
    if type(slot.palette_words) is not tuple \
            or type(slot.palette) is not tuple:
        raise PsxNativeContractError(
            f"{label} palette fields must be tuples")
    if not required:
        if slot.palette_words or slot.palette:
            raise PsxNativeContractError(
                f"{label} unavailable slot palettes must be empty")
        return
    if len(slot.palette_words) != CLUT_ENTRY_COUNT \
            or len(slot.palette) != CLUT_ENTRY_COUNT:
        raise PsxNativeContractError(
            f"{label} must carry exactly {CLUT_ENTRY_COUNT} palette entries")

    for entry_index, (word, color) in enumerate(zip(
            slot.palette_words, slot.palette, strict=True)):
        entry_label = f"{label} palette entry {entry_index}"
        value = _plain_int(
            word,
            label=f"{entry_label} word",
            minimum=0,
            maximum=0xFFFF,
        )
        if type(color) is not PsxBgr555Color:
            raise PsxNativeContractError(
                f"{entry_label} must be an exact PsxBgr555Color")
        color_word = _plain_int(
            color.word,
            label=f"{entry_label} decoded word",
            minimum=0,
            maximum=0xFFFF,
        )
        red = _plain_int(
            color.red, label=f"{entry_label} red", minimum=0, maximum=255)
        green = _plain_int(
            color.green,
            label=f"{entry_label} green",
            minimum=0,
            maximum=255,
        )
        blue = _plain_int(
            color.blue,
            label=f"{entry_label} blue",
            minimum=0,
            maximum=255,
        )
        if type(color.stp) is not bool:
            raise PsxNativeContractError(
                f"{entry_label} STP flag must be a boolean")
        expected_red_5 = value & 0x1F
        expected_green_5 = (value >> 5) & 0x1F
        expected_blue_5 = (value >> 10) & 0x1F
        expected = (
            value,
            (expected_red_5 << 3) | (expected_red_5 >> 2),
            (expected_green_5 << 3) | (expected_green_5 >> 2),
            (expected_blue_5 << 3) | (expected_blue_5 >> 2),
            bool(value & 0x8000),
        )
        if (color_word, red, green, blue, color.stp) != expected:
            raise PsxNativeContractError(
                f"{entry_label} does not match its BGR555/STP word")


def _validate_texture_slot_identity(
        slot: object, *, selector: int, source_offset: int,
        label: str) -> PsxNativeTextureSlot:
    if type(slot) is not PsxNativeTextureSlot:
        raise PsxNativeContractError(
            f"{label} must be an exact PsxNativeTextureSlot")
    if _plain_int(
            slot.selector, label=f"{label} selector",
            minimum=0, maximum=MATERIAL_SLOT_COUNT - 1) != selector:
        raise PsxNativeContractError(
            f"{label} selector must match its table position")
    if _plain_int(
            slot.source_offset, label=f"{label} source offset",
            minimum=0) != source_offset:
        raise PsxNativeContractError(
            f"{label} source offset does not match the decoded layout")
    if type(slot.header) is not bytes:
        raise PsxNativeContractError(f"{label} header must be bytes")
    if type(slot.available) is not bool:
        raise PsxNativeContractError(
            f"{label} availability must be a boolean")
    return slot


def _validate_pixel_bank(
        value: object, *, label: str, available: bool) -> None:
    if type(value) is not bytes:
        raise PsxNativeContractError(f"{label} must be bytes")
    expected_size = INDEXED_PIXEL_BYTES if available else 0
    if len(value) != expected_size:
        raise PsxNativeContractError(
            f"{label} must contain exactly {expected_size} indices")


def _validate_compact_texture_pack(pack: PsxNativeTexturePack) -> None:
    if pack.selector_to_pixel_bank_mapping != \
            LATE_SELECTOR_TO_PIXEL_BANK_MAPPING:
        raise PsxNativeContractError(
            "compact texture pack has the wrong selector mapping ID")
    if len(pack.pixel_banks) != PIXEL_BANK_COUNT:
        raise PsxNativeContractError(
            f"compact texture pack must carry {PIXEL_BANK_COUNT} pixel banks")
    for bank_index, bank in enumerate(pack.pixel_banks):
        _validate_pixel_bank(
            bank, label=f"compact pixel bank {bank_index}", available=True)

    source_offset = 0
    for selector, slot_object in enumerate(pack.slots):
        slot = _validate_texture_slot_identity(
            slot_object,
            selector=selector,
            source_offset=source_offset,
            label=f"compact texture slot {selector}",
        )
        if not slot.available:
            raise PsxNativeContractError(
                "compact texture slots must all be available")
        if slot.allocation_size is not None:
            raise PsxNativeContractError(
                "compact texture slots must not claim sector allocations")
        if selector < PIXEL_BANK_COUNT:
            if slot.header != ZERO_RECORD_HEADER:
                raise PsxNativeContractError(
                    f"compact pixel slot {selector} has an invalid header")
            record_size = FULL_RECORD_SIZE
        else:
            if slot.header not in (ZERO_RECORD_HEADER, REPEAT_RECORD_HEADER):
                raise PsxNativeContractError(
                    f"compact palette slot {selector} has an invalid header")
            if slot.header == REPEAT_RECORD_HEADER \
                    and slot.palette_words != \
                    pack.slots[selector - 1].palette_words:
                raise PsxNativeContractError(
                    f"compact palette slot {selector} repeat marker does not "
                    "match the preceding palette")
            record_size = PALETTE_RECORD_SIZE
        _validate_texture_palette(
            slot, label=f"compact texture slot {selector}", required=True)

        mapping = pack.selector_pixel_banks[selector]
        bank = _plain_int(
            mapping,
            label=f"compact selector {selector} pixel-bank mapping",
            minimum=0,
            maximum=PIXEL_BANK_COUNT - 1,
        )
        if bank != selector & (PIXEL_BANK_COUNT - 1):
            raise PsxNativeContractError(
                f"compact selector {selector} must map to S & 31")
        source_offset += record_size

    if source_offset != LATE_SETGFX_SIZE:
        raise PsxNativeContractError(
            "compact texture slot layout does not span the exact source size")


def _validate_sector_texture_pack(pack: PsxNativeTexturePack) -> None:
    if pack.selector_to_pixel_bank_mapping != \
            SECTOR_PADDED_SELECTOR_TO_PIXEL_BANK_MAPPING:
        raise PsxNativeContractError(
            "sector-padded texture pack has the wrong selector mapping ID")
    if len(pack.pixel_banks) != MATERIAL_SLOT_COUNT:
        raise PsxNativeContractError(
            "sector-padded texture pack must carry 128 direct bank positions")

    source_offset = 0
    populated_count = 0
    for selector, slot_object in enumerate(pack.slots):
        slot = _validate_texture_slot_identity(
            slot_object,
            selector=selector,
            source_offset=source_offset,
            label=f"sector-padded texture slot {selector}",
        )
        mapping = pack.selector_pixel_banks[selector]
        bank_payload = pack.pixel_banks[selector]
        if slot.available:
            if slot.header != ZERO_RECORD_HEADER:
                raise PsxNativeContractError(
                    f"populated sector slot {selector} has an invalid header")
            if slot.allocation_size != SECTOR_PADDED_FULL_ALLOCATION \
                    or type(slot.allocation_size) is not int:
                raise PsxNativeContractError(
                    f"populated sector slot {selector} has an invalid "
                    "allocation size")
            _validate_texture_palette(
                slot,
                label=f"sector-padded texture slot {selector}",
                required=True,
            )
            bank = _plain_int(
                mapping,
                label=f"sector selector {selector} pixel-bank mapping",
                minimum=0,
                maximum=MATERIAL_SLOT_COUNT - 1,
            )
            if bank != selector:
                raise PsxNativeContractError(
                    f"sector selector {selector} must map directly to itself")
            _validate_pixel_bank(
                bank_payload,
                label=f"sector pixel bank {selector}",
                available=True,
            )
            allocation_size = SECTOR_PADDED_FULL_ALLOCATION
            populated_count += 1
        else:
            if slot.header != SECTOR_PADDED_EMPTY_MARKER:
                raise PsxNativeContractError(
                    f"empty sector slot {selector} has an invalid marker")
            if slot.allocation_size != SECTOR_PADDED_EMPTY_ALLOCATION \
                    or type(slot.allocation_size) is not int:
                raise PsxNativeContractError(
                    f"empty sector slot {selector} has an invalid allocation "
                    "size")
            _validate_texture_palette(
                slot,
                label=f"sector-padded texture slot {selector}",
                required=False,
            )
            if mapping is not None:
                raise PsxNativeContractError(
                    f"empty sector selector {selector} must map to None")
            _validate_pixel_bank(
                bank_payload,
                label=f"empty sector pixel bank {selector}",
                available=False,
            )
            allocation_size = SECTOR_PADDED_EMPTY_ALLOCATION
        source_offset += allocation_size

    if (source_offset, populated_count) not in SECTOR_PADDED_OBSERVED_LAYOUTS:
        raise PsxNativeContractError(
            "sector-padded texture pack size/material cardinality is not one "
            "of the exact observed layouts")


def validate_psx_native_texture_pack(
        pack: PsxNativeTexturePack) -> PsxNativeTexturePack:
    """Validate one exact parsed texture-pack object without resolving RGBA."""

    if type(pack) is not PsxNativeTexturePack:
        raise PsxNativeContractError(
            "native texture pack must be an exact PsxNativeTexturePack")
    _logical_path(pack.logical_path, label="native texture-pack logical path")
    _sha256(pack.source_sha256, label="native texture-pack source SHA-256")
    if type(pack.slots) is not tuple \
            or len(pack.slots) != MATERIAL_SLOT_COUNT:
        raise PsxNativeContractError(
            "native texture pack must carry exactly 128 selector slots")
    if type(pack.pixel_banks) is not tuple:
        raise PsxNativeContractError(
            "native texture-pack pixel banks must be a tuple")
    if type(pack.selector_pixel_banks) is not tuple \
            or len(pack.selector_pixel_banks) != MATERIAL_SLOT_COUNT:
        raise PsxNativeContractError(
            "native texture pack must carry an exact 128-entry selector "
            "mapping")

    if pack.layout_id == LATE_SETGFX_LAYOUT_ID:
        _validate_compact_texture_pack(pack)
    elif pack.layout_id == SECTOR_PADDED_SETGFX_LAYOUT_ID:
        _validate_sector_texture_pack(pack)
    else:
        raise PsxNativeContractError(
            "native texture pack has an unsupported layout ID")
    return pack


def validate_psx_native_texture_inventory(
        packs: tuple[PsxNativeTexturePack, ...],
        ) -> tuple[PsxNativeTexturePack, ...]:
    """Validate canonical case-insensitive ordering of exact texture packs."""

    if type(packs) is not tuple:
        raise PsxNativeContractError("native texture packs must be a tuple")
    paths: list[str] = []
    folded_paths: set[str] = set()
    for pack in packs:
        validate_psx_native_texture_pack(pack)
        folded = pack.logical_path.casefold()
        if folded in folded_paths:
            raise PsxNativeContractError(
                "native texture-pack paths must be case-insensitively unique")
        folded_paths.add(folded)
        paths.append(pack.logical_path)
    expected_paths = sorted(paths, key=lambda path: (path.casefold(), path))
    if paths != expected_paths:
        raise PsxNativeContractError(
            "native texture-pack paths must be in canonical "
            "case-insensitive order")
    return packs


def _partition_meshes(
        build: PsxNativeBuild,
        ) -> tuple[tuple[PsxNativeMesh, ...], tuple[PsxNativeMesh, ...]]:
    meshes = build.meshes
    packed: list[PsxNativeMesh] = []
    loose: list[PsxNativeMesh] = []
    ordinals: list[int] = []
    packed_after_loose = False
    saw_loose = False
    for mesh_index, mesh in enumerate(meshes):
        ordinal = mesh.archive_ordinal
        if ordinal is None:
            saw_loose = True
            if mesh.archive_offset is not None \
                    or mesh.archive_sector is not None:
                raise PsxNativeContractError(
                    f"loose native mesh {mesh_index} must not carry archive "
                    "offset or sector provenance")
            loose.append(mesh)
            continue
        if saw_loose:
            packed_after_loose = True
        ordinals.append(_plain_int(
            ordinal,
            label=f"native mesh {mesh_index} dense archive ordinal",
            minimum=0,
        ))
        archive_offset = _plain_int(
            mesh.archive_offset,
            label=f"packed native mesh {mesh_index} archive offset",
            minimum=0,
        )
        if archive_offset % SECTOR_SIZE:
            raise PsxNativeContractError(
                f"packed native mesh {mesh_index} archive offset must be "
                f"0x{SECTOR_SIZE:X}-byte sector aligned")
        archive_sector = _plain_int(
            mesh.archive_sector,
            label=f"packed native mesh {mesh_index} archive sector",
            minimum=0,
        )
        if archive_sector != archive_offset // SECTOR_SIZE:
            raise PsxNativeContractError(
                f"packed native mesh {mesh_index} archive sector must match "
                "its archive offset")
        packed.append(mesh)

    if packed_after_loose:
        raise PsxNativeContractError(
            "packed native meshes must appear before every loose mesh")
    if len(ordinals) != len(set(ordinals)):
        raise PsxNativeContractError(
            "packed native mesh dense archive ordinals must be unique")
    if ordinals != list(range(len(packed))):
        raise PsxNativeContractError(
            "packed native mesh dense archive ordinals must be contiguous "
            "and appear in exact order from zero")

    has_unit_archive = build.unit_archive_logical_path is not None
    if bool(packed) != has_unit_archive:
        raise PsxNativeContractError(
            "UNIT.BIN path/hash provenance must be present exactly when the "
            "build has a packed mesh inventory")
    if packed:
        unit_path = build.unit_archive_logical_path
        for mesh_index, mesh in enumerate(packed):
            if mesh.logical_path != unit_path:
                raise PsxNativeContractError(
                    f"packed native mesh {mesh_index} logical path must "
                    "exactly match the frozen UNIT.BIN path")

    loose_paths = [mesh.logical_path for mesh in loose]
    folded_loose_paths = [path.casefold() for path in loose_paths]
    if len(folded_loose_paths) != len(set(folded_loose_paths)):
        raise PsxNativeContractError(
            "loose native mesh paths must be case-insensitively unique")
    if loose_paths != sorted(
            loose_paths, key=lambda path: (path.casefold(), path)):
        raise PsxNativeContractError(
            "loose native mesh paths must be in canonical "
            "case-insensitive order")
    return tuple(packed), tuple(loose)


def _validate_no_model_slot_proof(meshes: tuple[PsxNativeMesh, ...]) -> None:
    for mesh_index, mesh in enumerate(meshes):
        if mesh.model_slot is not None \
                or mesh.model_slot_evidence_id is not None:
            raise PsxNativeContractError(
                f"native mesh {mesh_index} claims a model slot without "
                "frozen executable-table evidence")


def _validate_model_slot_proof(
        build: PsxNativeBuild, packed: tuple[PsxNativeMesh, ...],
        loose: tuple[PsxNativeMesh, ...]) -> None:
    evidence = build.model_slot_evidence
    if evidence is None:
        _validate_no_model_slot_proof(build.meshes)
        return
    if type(evidence) is not PsxNativeModelSlotEvidence:
        raise PsxNativeContractError(
            "model-slot evidence must be an exact "
            "PsxNativeModelSlotEvidence object")
    if evidence.evidence_id != PSX_MODEL_SLOT_EVIDENCE_ID:
        raise PsxNativeContractError(
            "model-slot evidence ID must match the canonical executable-table "
            "proof ID")

    build_unit_hash = _sha256(
        build.unit_archive_sha256, label="frozen UNIT.BIN hash")
    evidence_unit_hash = _sha256(
        evidence.unit_archive_sha256,
        label="model-slot evidence UNIT.BIN hash",
    )
    if evidence_unit_hash != build_unit_hash:
        raise PsxNativeContractError(
            "model-slot evidence UNIT.BIN hash must exactly match the build")

    build_executable_hash = _sha256(
        build.boot_executable_sha256,
        label="frozen boot executable hash",
    )
    evidence_executable_hash = _sha256(
        evidence.boot_executable_sha256,
        label="model-slot evidence boot executable hash",
    )
    if evidence_executable_hash != build_executable_hash:
        raise PsxNativeContractError(
            "model-slot evidence executable hash must exactly match the build")

    _plain_int(
        evidence.executable_table_offset,
        label="executable allocation-table offset",
        minimum=0,
    )
    table_count = _plain_int(
        evidence.executable_table_entry_count,
        label="executable allocation-table entry count",
        minimum=1,
    )
    _sha256(
        evidence.executable_table_sha256,
        label="executable allocation-table hash",
    )

    if type(evidence.empty_model_slots) is not tuple:
        raise PsxNativeContractError("empty model slots must be a tuple")
    empty_slots = tuple(
        _plain_int(
            slot,
            label=f"empty model slot {slot_index}",
            minimum=0,
        )
        for slot_index, slot in enumerate(evidence.empty_model_slots)
    )
    if empty_slots != tuple(sorted(set(empty_slots))):
        raise PsxNativeContractError(
            "empty model slots must be unique and ordered")
    if any(slot >= table_count for slot in empty_slots):
        raise PsxNativeContractError(
            "empty model slots must be inside the executable table")

    sentinel_sector = evidence.trailing_sentinel_archive_sector
    if sentinel_sector is not None:
        _plain_int(
            sentinel_sector,
            label="trailing sentinel archive sector",
            minimum=0,
        )

    for mesh in loose:
        if mesh.model_slot is not None \
                or mesh.model_slot_evidence_id is not None:
            raise PsxNativeContractError(
                "loose native meshes must never inherit packed model slots")

    expected_packed_count = table_count - len(empty_slots)
    if len(packed) != expected_packed_count:
        raise PsxNativeContractError(
            "packed native mesh count must equal executable table count "
            "minus empty-slot count")

    slots: list[int] = []
    for mesh_index, mesh in enumerate(packed):
        if mesh.model_slot_evidence_id != evidence.evidence_id:
            raise PsxNativeContractError(
                f"packed native mesh {mesh_index} must carry the exact "
                "build model-slot evidence ID")
        slots.append(_plain_int(
            mesh.model_slot,
            label=f"packed native mesh {mesh_index} model slot",
            minimum=0,
        ))

    if len(slots) != len(set(slots)):
        raise PsxNativeContractError(
            "packed native mesh model slots must be unique")
    expected_slots = set(range(table_count)) - set(empty_slots)
    if set(slots) != expected_slots:
        raise PsxNativeContractError(
            "packed native mesh slots must exactly equal the non-empty "
            "executable-table entries")


def validate_psx_native_build(build: PsxNativeBuild) -> PsxNativeBuild:
    """Validate one frozen native build before viewer/export provenance use."""

    if type(build) is not PsxNativeBuild:
        raise PsxNativeContractError(
            "native build must be an exact PsxNativeBuild object")
    _validate_build_provenance(build)
    if type(build.meshes) is not tuple:
        raise PsxNativeContractError("native build meshes must be a tuple")
    if not build.meshes:
        raise PsxNativeContractError(
            "native build must contain at least one mesh")
    for mesh in build.meshes:
        validate_psx_native_mesh(mesh)

    packed, loose = _partition_meshes(build)
    validate_psx_native_effect_inventory(build.effects)
    validate_psx_native_texture_inventory(build.texture_packs)
    _validate_model_slot_proof(build, packed, loose)
    return build


__all__ = (
    "PsxNativeContractError",
    "validate_psx_native_build",
    "validate_psx_native_effect_inventory",
    "validate_psx_native_mesh",
    "validate_psx_native_texture_inventory",
    "validate_psx_native_texture_pack",
)
