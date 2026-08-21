"""Recovered legacy PSW/PSV unit-model packet semantics.

This module records the part of Urban Assault's older PlayStation mesh path
that is proven by the recovered December 1998 and March 1999 overlays.  It is
pure and Qt-free.  The bounded descriptor-relative material-local UV quotient
is connected to the preview; runtime packet/descriptor binding remains
unconnected.

The December unit path narrows the wide version-1 face records in place and
submits one 52-byte ``POLY_GT4`` packet per accepted face.  Authored corners
are submitted in slot order ``1, 0, 2, 3``.  NCLIP is unconditional and only
a strictly positive MAC0 survives.  The first prefix word is a primitive
flags field whose bit 13 survives the later PW3 conversion, but the recovered
unit loader and draw loop do not apply it.  The second prefix word is not read
by either routine and remains unnamed.

All helpers below therefore require explicit inputs and expose unresolved
states as data.  In particular, no helper turns either prefix field into a
culling, blending, or material decision.
"""

from __future__ import annotations

from dataclasses import dataclass
import struct
from typing import Sequence, TypeVar


PSX_PSW_PIPELINE_PROFILE_ID = "ua_psw_unit_gt4_static_v1"
PSX_PSW_PIPELINE_EVIDENCE = "ua_over1_cross_build_static_disassembly"
PSX_PSW_VIEWER_INTEGRATION_STATE = (
    "material_local_uv_quotient_preview_integrated_"
    "descriptor_runtime_binding_unresolved")

PSW_FACE_SIZE = 0x4C
PSX_POLY_GT4_PACKET_SIZE = 0x34
PSX_POLY_GT4_OPAQUE_COMMAND = 0x3C
PSX_POLY_GT4_SEMITRANSPARENT_COMMAND = 0x3E
PSX_TPAGE_ABR_MASK = 0x0060
PSW_TEXTURE_DESCRIPTOR_SIZE = 0x10
PSW_TEXTURE_DESCRIPTOR_U_ORIGIN_OFFSET = 0x04
PSW_TEXTURE_DESCRIPTOR_V_ORIGIN_OFFSET = 0x06
PSW_TEXTURE_DESCRIPTOR_TPAGE_OFFSET = 0x0C
PSW_TEXTURE_DESCRIPTOR_CLUT_OFFSET = 0x0E

PSW_AUTHORED_TO_GT4_ORDER = (1, 0, 2, 3)
PSW_NCLIP_AUTHORED_ORDER = PSW_AUTHORED_TO_GT4_ORDER[:3]
PSW_NCLIP_POLICY = "unconditional_strict_positive_mac0"
PSW_PRIMITIVE_FLAGS_BIT13 = 0x00002000
PSW_PRIMITIVE_FLAGS_STATE = (
    "bit13_preserved_to_pw3_but_unapplied_by_recovered_unit_path")
PSW_PREFIX_WORD4_STATE = (
    "unresolved_not_read_by_recovered_unit_loader_or_renderer")

# Portable corpus anchors.  All ranges are half-open file ranges.
UA_DECEMBER_MAIN_EXE_SHA256 = (
    "d03137a60a9fcafdfb5627ec267ae335f39a5f054e2723282a81e4ffb1efc518")
UA_DECEMBER_OVER1_SHA256 = (
    "fbb8bd12146b161dac5363c3881a3c97d7daceca67a8cfa4bcd7cfd0f47741ca")
UA_DECEMBER_OVER1_LOAD_ADDRESS = 0x8010F3A0
UA_DECEMBER_UNIT_RENDER_CALL_FILE_OFFSET = 0x8928
UA_DECEMBER_UNIT_RENDER_CALL_ADDRESS = 0x80117CC8
UA_DECEMBER_PSW_RENDER_FUNCTION_FILE_RANGE = (0x9ABC, 0xA02C)
UA_DECEMBER_PSW_RENDER_FUNCTION_ADDRESS_RANGE = (0x80118E5C, 0x801193CC)
UA_DECEMBER_PSW_RENDER_FUNCTION_RANGE_SHA256 = (
    "c527ff121142740660e03968c7d02967ddfaf0f5bd9c33b3a412942bbb01621f")
UA_DECEMBER_PSW_FACE_LOOP_FILE_RANGE = (0x9CE4, 0xA000)
UA_DECEMBER_PSW_FACE_LOOP_ADDRESS_RANGE = (0x80119084, 0x801193A0)
UA_DECEMBER_PSW_FACE_LOOP_RANGE_SHA256 = (
    "73902b216e42fac012417282f9af09da365a621c2987f6651286abc07ca8bbe5")
UA_DECEMBER_PSW_RTPT_FILE_OFFSET = 0x9DF4
UA_DECEMBER_PSW_RTPT_ADDRESS = 0x80119194
UA_DECEMBER_PSW_NCLIP_FILE_OFFSET = 0x9E08
UA_DECEMBER_PSW_NCLIP_ADDRESS = 0x801191A8
UA_DECEMBER_PSW_NCLIP_REJECT_FILE_OFFSET = 0x9E1C
UA_DECEMBER_PSW_NCLIP_REJECT_ADDRESS = 0x801191BC
UA_DECEMBER_PSW_FOURTH_RTPS_FILE_OFFSET = 0x9E78
UA_DECEMBER_PSW_FOURTH_RTPS_ADDRESS = 0x80119218
UA_DECEMBER_PSW_LOADER_FILE_RANGE = (0x11C74, 0x12128)
UA_DECEMBER_PSW_LOADER_ADDRESS_RANGE = (0x80121014, 0x801214C8)
UA_DECEMBER_PSW_LOADER_RANGE_SHA256 = (
    "2ba67a32e656e83cc2e8b656a42c57122ce8fe0cb7484ebb392f28b85fb7bfc8")

UA_MARCH_MAIN_EXE_SHA256 = (
    "53c40bb8ad160df689d89df69af102e986719e7138df90779e9197ae14856e70")
UA_MARCH_OVER1_SHA256 = (
    "85f0ec6874928c9fa69fac674d6b9a8093ec4ef913aed894c7d4b8162f748aac")
UA_MARCH_OVER1_LOAD_ADDRESS = 0x800A5730
UA_MARCH_PSW_RENDER_FUNCTION_FILE_RANGE = (0xE7F0, 0xED60)
UA_MARCH_PSW_RENDER_FUNCTION_ADDRESS_RANGE = (0x800B3F20, 0x800B4490)
UA_MARCH_PSW_RENDER_FUNCTION_RANGE_SHA256 = (
    "ecdfe4cf515c763019da818fee9fff439f0acd91daf8bbb92316d1cefa2ec86b")
UA_MARCH_PSW_FACE_LOOP_FILE_RANGE = (0xEA18, 0xED34)
UA_MARCH_PSW_FACE_LOOP_ADDRESS_RANGE = (0x800B4148, 0x800B4464)
UA_MARCH_PSW_FACE_LOOP_RANGE_SHA256 = (
    "8c6455343e872d089a1bd8cad37b2473583839611ce5495dd88dc48a6cac6851")
UA_MARCH_PSW_RTPT_FILE_OFFSET = 0xEB28
UA_MARCH_PSW_RTPT_ADDRESS = 0x800B4258
UA_MARCH_PSW_NCLIP_FILE_OFFSET = 0xEB3C
UA_MARCH_PSW_NCLIP_ADDRESS = 0x800B426C
UA_MARCH_PSW_NCLIP_REJECT_FILE_OFFSET = 0xEB50
UA_MARCH_PSW_NCLIP_REJECT_ADDRESS = 0x800B4280

UA_DECEMBER_V1_PSW_SHA256 = (
    "596feb09afc440d27e9fbb387114e365744d40f644b6517a79dc9b231d7e6eaf")
UA_MARCH_UNIT_BIN_SHA256 = (
    "d5df80950cc0e639ef94eaed0ba85cbfc663fc9c5973147c1e67b826192b74db")
UA_PSW_PW3_PREFIX_MATCH_MESH_COUNT = 18
UA_PSW_PW3_PREFIX_MATCH_FACE_COUNT = 747


class PsxPswPipelineError(ValueError):
    """Raised when an input is outside the statically supported contract."""


def _require_plain_int(value: object, *, field: str) -> int:
    if type(value) is not int:
        raise PsxPswPipelineError(f"{field} must be an integer")
    return value


def _require_range(
        value: object, *, field: str, minimum: int, maximum: int) -> int:
    parsed = _require_plain_int(value, field=field)
    if not minimum <= parsed <= maximum:
        raise PsxPswPipelineError(
            f"{field} must be in [{minimum}, {maximum}], got {parsed}")
    return parsed


def _signed32(value: int) -> int:
    return ((value + 0x80000000) & 0xFFFFFFFF) - 0x80000000


def _truncate_fixed_16_16(value: int) -> int:
    """Match the overlay's signed 16.16 truncation toward zero."""

    signed = _signed32(value)
    if signed < 0:
        signed += 0xFFFF
    return signed >> 16


@dataclass(frozen=True, slots=True)
class PswFacePrefix:
    """The two serialized PSW prefix words with calibrated semantics."""

    primitive_flags: int
    unresolved_word4: int

    def __post_init__(self) -> None:
        _require_range(
            self.primitive_flags, field="primitive_flags",
            minimum=0, maximum=0xFFFFFFFF)
        _require_range(
            self.unresolved_word4, field="unresolved_word4",
            minimum=0, maximum=0xFFFFFFFF)

    @property
    def bit13_is_set(self) -> bool:
        """Report the preserved bit only; do not infer a render behavior."""

        return bool(self.primitive_flags & PSW_PRIMITIVE_FLAGS_BIT13)


@dataclass(frozen=True, slots=True)
class PswAuthoredFace:
    """One exact 76-byte on-disc PSW/PSV face record."""

    prefix: PswFacePrefix
    vertex_indices: tuple[int, int, int, int]
    uv_fixed_16_16: tuple[
        tuple[int, int], tuple[int, int], tuple[int, int], tuple[int, int]]
    texture_selector: int
    shade_fixed_16_16: tuple[int, int, int, int]

    def __post_init__(self) -> None:
        if not isinstance(self.prefix, PswFacePrefix):
            raise PsxPswPipelineError("prefix must be a PswFacePrefix")
        if type(self.vertex_indices) is not tuple or len(
                self.vertex_indices) != 4:
            raise PsxPswPipelineError(
                "vertex_indices must be an exact four-item tuple")
        for index in self.vertex_indices:
            _require_range(
                index, field="vertex index", minimum=0, maximum=0xFFFF)
        if type(self.uv_fixed_16_16) is not tuple or len(
                self.uv_fixed_16_16) != 4:
            raise PsxPswPipelineError(
                "uv_fixed_16_16 must be an exact four-item tuple")
        for uv in self.uv_fixed_16_16:
            if type(uv) is not tuple or len(uv) != 2:
                raise PsxPswPipelineError(
                    "each uv_fixed_16_16 value must be a two-item tuple")
            for component in uv:
                _require_range(
                    component, field="UV fixed component",
                    minimum=-0x80000000, maximum=0x7FFFFFFF)
        _require_range(
            self.texture_selector, field="texture_selector",
            minimum=0, maximum=0xFFFF)
        if type(self.shade_fixed_16_16) is not tuple or len(
                self.shade_fixed_16_16) != 4:
            raise PsxPswPipelineError(
                "shade_fixed_16_16 must be an exact four-item tuple")
        for shade in self.shade_fixed_16_16:
            _require_range(
                shade, field="shade fixed component",
                minimum=-0x80000000, maximum=0x7FFFFFFF)


@dataclass(frozen=True, slots=True)
class PswTextureDescriptor:
    """Only the four 16-bit descriptor fields proven on the unit path."""

    u_origin: int
    v_origin: int
    tpage: int
    clut_offset: int

    def __post_init__(self) -> None:
        for field in ("u_origin", "v_origin", "tpage", "clut_offset"):
            _require_range(
                getattr(self, field), field=field, minimum=0, maximum=0xFFFF)


@dataclass(frozen=True, slots=True)
class PswGt4Binding:
    """Packet-facing fields after the recovered unit-loader narrowing."""

    descriptor_index: int
    command: int
    tpage: int
    clut_offset: int
    vertex_indices: tuple[int, int, int, int]
    uv_bytes: tuple[
        tuple[int, int], tuple[int, int], tuple[int, int], tuple[int, int]]
    shade_bytes: tuple[int, int, int, int]


def decode_psw_face_record(record: bytes) -> PswAuthoredFace:
    """Decode the exact wide face layout without interpreting prefix word 4."""

    if type(record) is not bytes:
        raise PsxPswPipelineError("record must be bytes")
    if len(record) != PSW_FACE_SIZE:
        raise PsxPswPipelineError(
            f"record must contain exactly {PSW_FACE_SIZE} bytes")
    flags, unresolved = struct.unpack_from("<II", record, 0)
    indices = struct.unpack_from("<4I", record, 8)
    uv_values = struct.unpack_from("<8i", record, 24)
    selector = struct.unpack_from("<I", record, 56)[0]
    shades = struct.unpack_from("<4i", record, 60)
    if any(index > 0xFFFF for index in indices):
        raise PsxPswPipelineError(
            "recovered PSW unit indices must fit unsigned 16 bits")
    if selector > 0xFFFF:
        raise PsxPswPipelineError(
            "recovered PSW unit texture selectors must fit unsigned 16 bits")
    uvs = tuple(
        (uv_values[corner * 2], uv_values[corner * 2 + 1])
        for corner in range(4)
    )
    return PswAuthoredFace(
        prefix=PswFacePrefix(flags, unresolved),
        vertex_indices=indices,
        uv_fixed_16_16=uvs,  # type: ignore[arg-type]
        texture_selector=selector,
        shade_fixed_16_16=shades,
    )


def decode_psw_texture_descriptor(
        descriptor: bytes) -> PswTextureDescriptor:
    """Decode only fields read by the recovered loader and draw routine."""

    if type(descriptor) is not bytes:
        raise PsxPswPipelineError("descriptor must be bytes")
    if len(descriptor) != PSW_TEXTURE_DESCRIPTOR_SIZE:
        raise PsxPswPipelineError(
            "descriptor must contain exactly 16 bytes")
    return PswTextureDescriptor(
        u_origin=struct.unpack_from(
            "<H", descriptor, PSW_TEXTURE_DESCRIPTOR_U_ORIGIN_OFFSET)[0],
        v_origin=struct.unpack_from(
            "<H", descriptor, PSW_TEXTURE_DESCRIPTOR_V_ORIGIN_OFFSET)[0],
        tpage=struct.unpack_from(
            "<H", descriptor, PSW_TEXTURE_DESCRIPTOR_TPAGE_OFFSET)[0],
        clut_offset=struct.unpack_from(
            "<H", descriptor, PSW_TEXTURE_DESCRIPTOR_CLUT_OFFSET)[0],
    )


T = TypeVar("T")


def authored_to_gt4(values: Sequence[T]) -> tuple[T, T, T, T]:
    """Reorder four authored corner slots exactly as the PSW GT4 path."""

    if isinstance(values, (str, bytes, bytearray, memoryview)):
        raise PsxPswPipelineError("values must be a four-item sequence")
    if len(values) != 4:
        raise PsxPswPipelineError("values must contain exactly four items")
    return tuple(values[index] for index in PSW_AUTHORED_TO_GT4_ORDER)  # type: ignore[return-value]


def psw_unit_nclip_submits(mac0: int) -> bool:
    """Apply the recovered unconditional ``blez`` test after NCLIP."""

    parsed = _require_range(
        mac0, field="mac0", minimum=-0x80000000, maximum=0x7FFFFFFF)
    return parsed > 0


def psw_unit_descriptor_index(texture_selector: int) -> int:
    """Return the unit path's explicit ``selector + 1`` descriptor index."""

    selector = _require_range(
        texture_selector, field="texture_selector",
        minimum=0, maximum=0xFFFF)
    return selector + 1


def psw_material_local_uv_quotient(signed_fixed_16_16: int) -> int:
    """Return the exact descriptor-relative PSW material-local coordinate.

    The recovered unit loader computes
    ``q = ((signed >> 16) + (1 if signed < 0 else 0)) >> 1`` before adding a
    texture-descriptor origin.  This helper exposes only that proven quotient.
    Viewer use is intentionally limited to integral corpus inputs whose local
    result fits the decoded 128 x 128 material; descriptor origin, TPage, CLUT
    offset, and runtime descriptor selection are separate unresolved bindings.
    """

    signed = _require_range(
        signed_fixed_16_16, field="signed PSW UV fixed component",
        minimum=-0x80000000, maximum=0x7FFFFFFF)
    if signed & 0xFFFF:
        raise PsxPswPipelineError(
            "signed PSW UV fixed component must be integral 16.16")
    quotient = _psw_unit_uv_quotient(signed)
    if not 0 <= quotient <= 127:
        raise PsxPswPipelineError(
            "PSW material-local UV quotient must be in [0, 127], "
            f"got {quotient}")
    return quotient


def _psw_unit_uv_quotient(signed_fixed_16_16: int) -> int:
    """Return the loader's unbounded signed quotient before byte wrapping.

    Unlike the public material-local preview helper, this runtime primitive
    deliberately accepts fractional 16.16 values and results outside one
    decoded 128 x 128 material.  The recovered packet path adds a descriptor
    origin and retains the low byte; narrowing it to the preview domain would
    change executable-backed behavior.
    """

    signed = _require_range(
        signed_fixed_16_16, field="signed PSW UV fixed component",
        minimum=-0x80000000, maximum=0x7FFFFFFF)
    return ((signed >> 16) + (1 if signed < 0 else 0)) >> 1


def psw_unit_gt4_command(tpage: int) -> int:
    """Select opaque or semitransparent GT4 from TPage ABR bits 5..6."""

    parsed = _require_range(tpage, field="tpage", minimum=0, maximum=0xFFFF)
    if parsed & PSX_TPAGE_ABR_MASK:
        return PSX_POLY_GT4_SEMITRANSPARENT_COMMAND
    return PSX_POLY_GT4_OPAQUE_COMMAND


def _narrow_unit_uv(raw_fixed: int, origin: int) -> int:
    # The unit loader first truncates the signed integral component divided by
    # two, then adds the descriptor origin.  Packet byte stores retain low 8.
    return (_psw_unit_uv_quotient(raw_fixed) + origin) & 0xFF


def _narrow_unit_shade(raw_fixed: int) -> int:
    # The loader stores the signed integer; the draw routine later writes the
    # low byte to all three RGB channels.
    return _truncate_fixed_16_16(raw_fixed) & 0xFF


def bind_psw_unit_face(
        face: PswAuthoredFace,
        descriptor: PswTextureDescriptor,
) -> PswGt4Binding:
    """Apply the proven loader narrowing and packet-facing corner mapping."""

    if not isinstance(face, PswAuthoredFace):
        raise PsxPswPipelineError("face must be a PswAuthoredFace")
    if not isinstance(descriptor, PswTextureDescriptor):
        raise PsxPswPipelineError(
            "descriptor must be a PswTextureDescriptor")
    authored_uvs = tuple(
        (
            _narrow_unit_uv(u, descriptor.u_origin),
            _narrow_unit_uv(v, descriptor.v_origin),
        )
        for u, v in face.uv_fixed_16_16
    )
    authored_shades = tuple(
        _narrow_unit_shade(value) for value in face.shade_fixed_16_16)
    return PswGt4Binding(
        descriptor_index=psw_unit_descriptor_index(face.texture_selector),
        command=psw_unit_gt4_command(descriptor.tpage),
        tpage=descriptor.tpage,
        clut_offset=descriptor.clut_offset,
        vertex_indices=authored_to_gt4(face.vertex_indices),
        uv_bytes=authored_to_gt4(authored_uvs),
        shade_bytes=authored_to_gt4(authored_shades),
    )
