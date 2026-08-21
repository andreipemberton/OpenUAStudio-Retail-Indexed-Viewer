"""Pure PlayStation GTE/GPU fidelity helpers with fail-closed UA profiles.

This module isolates the integer operations that can already be reproduced
from hardware tests and the recovered Urban Assault PlayStation executable.
It deliberately has no Qt dependency and is not wired into the viewer.

The hardware-level operations below reproduce the PS1 GTE division/screen
stage, integer SXY snapping, NCLIP, AVSZ3/AVSZ4, and the screen-anchored 4x4
GPU dither table.  The Urban Assault profile is intentionally explicit: a
caller must choose the recovered 512x240 or 512x256 presentation branch.

Important evidence boundaries are exported as constants.  In particular,
having a hardware-correct dither helper does not establish that Urban Assault
enabled GPU dithering, and recovering the overlay's ordering-table insertion
formula does not establish its full table traversal direction.  The mapping
from the viewer's floating camera/model transform to game-native GTE matrix
and translation registers is also unresolved, so no viewer default is
inferred here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, TypeVar


HARDWARE_HELPER_EVIDENCE = (
    "pcsx_redux_gte_core_and_scph_5501_hardware_truth_tests")
PSX_GTE_HELPER_PROFILE_ID = "psx_gte_integer_helpers_v1"
UA_GTE_CONFIG_EVIDENCE = "ua_main_executable_static_disassembly"
UA_GTE_CONFIG_SELECTION_STATE = "explicit_pal_or_ntsc_branch_required"
UA_VIEWER_TRANSFORM_MAPPING_STATE = "unresolved"
UA_DITHER_ENABLE_STATE = "unresolved"
UA_OT_BUCKET_FORMULA_EVIDENCE = "ua_over1_static_disassembly"
UA_OT_TRAVERSAL_STATE = "unresolved_beyond_same_bucket_lifo"
UA_LATE_UNIT_GT4_PATH_EVIDENCE = "ua_over1_static_disassembly"

UA_GTE_VIDEO_WIDTH = 512
UA_GTE_VIDEO_HEIGHT_NTSC = 240
UA_GTE_VIDEO_HEIGHT_PAL = 256
UA_GTE_H = 250
UA_GTE_ZSF3 = 0x0155
UA_GTE_ZSF4 = 0x0100
UA_GTE_DQA = -4194
UA_GTE_DQB = 0x01400000

# Portable anchors for the recovered 15 June 1999 executable.  Offsets are
# half-open file ranges.  Corpus-backed tests are opt-in through the same
# OPENUA_PSX_CORPUS_ROOT convention used by the other native PSX helpers.
UA_JUNE_MAIN_EXE_SHA256 = (
    "ea9def3942ba20077d4c06591dc3acdb85d7641e47d728eeb653267947bae767")
UA_GTE_CONTROL_INIT_FILE_RANGE = (0x65650, 0x656D0)
UA_GTE_CONTROL_INIT_RANGE_SHA256 = (
    "1a88998f41c547c585c690572050a34ea1498a26d1b5d24e0b880bc1b71c3b89")
UA_GTE_RENDER_INIT_FILE_RANGE = (0x53C80, 0x53DC0)
UA_GTE_RENDER_INIT_RANGE_SHA256 = (
    "612269c1fadcfc8b5731453366034959c4426d5cb8dafedf8ae0f764cfc357df")
UA_GTE_RENDER_CALL_FILE_RANGES = (
    (0x51030, 0x51060),
    (0x515B0, 0x515E0),
)
UA_GTE_RENDER_CALL_RANGE_SHA256 = (
    "2e58b4b1c0fe4d59a5d9e6ca0cf53b7f041a5f62b47893df25e196777a049ed4",
    "a89da69facdd11e27ef1d2771cd188562df05a8a376ddccbb87bf7d9a9274c94",
)

PSX_SCREEN_MIN = -0x400
PSX_SCREEN_MAX = 0x3FF
PSX_DIVIDE_MAX = 0x1FFFF

# Rows are screen Y modulo four; columns are screen X modulo four.
PSX_DITHER_MATRIX = (
    (-4, 0, -3, 1),
    (2, -2, 3, -1),
    (-3, 1, -4, 0),
    (3, -1, 2, -2),
)

PSX_POLY_GT4_PACKET_SIZE = 52
PSX_POLY_GT4_OPAQUE_COMMAND = 0x3C
PSX_POLY_GT4_SEMITRANSPARENT_COMMAND = 0x3E
PSX_POLY_GT4_FIELD_OFFSETS = (
    ("tag", 0),
    ("rgb0_code", 4),
    ("xy0", 8),
    ("uv0_clut", 12),
    ("rgb1", 16),
    ("xy1", 20),
    ("uv1_tpage", 24),
    ("rgb2", 28),
    ("xy2", 32),
    ("uv2", 36),
    ("rgb3", 40),
    ("xy3", 44),
    ("uv3", 48),
)
UA_LATE_UNIT_GT4_TPAGE_ABR_MASK = 0x0060

UA_OT_BUCKET_BIAS = 16
PSX_OT_ENTRY_BYTES = 4


class PsxGteError(ValueError):
    """Raised when a value or UA profile branch is not evidence-supported."""


def _require_plain_int(value: object, *, field: str) -> int:
    if type(value) is not int:
        raise PsxGteError(f"{field} must be an integer")
    return value


def _require_range(
        value: object, *, field: str, minimum: int, maximum: int) -> int:
    parsed = _require_plain_int(value, field=field)
    if not minimum <= parsed <= maximum:
        raise PsxGteError(
            f"{field} must be in [{minimum}, {maximum}], got {parsed}")
    return parsed


def _signed32(value: int) -> int:
    return ((value + 0x80000000) & 0xFFFFFFFF) - 0x80000000


@dataclass(frozen=True, slots=True)
class GteProjectionConfig:
    """Explicit GTE projection/depth constants for one presentation branch."""

    video_width: int
    video_height: int
    branch: str
    ofx: int
    ofy: int
    h: int
    zsf3: int
    zsf4: int
    dqa: int
    dqb: int

    def __post_init__(self) -> None:
        _require_range(
            self.video_width, field="video_width", minimum=1, maximum=4096)
        _require_range(
            self.video_height, field="video_height", minimum=1, maximum=4096)
        if type(self.branch) is not str or not self.branch:
            raise PsxGteError("branch must be a non-empty string")
        _require_range(
            self.ofx, field="ofx", minimum=-0x80000000,
            maximum=0x7FFFFFFF)
        _require_range(
            self.ofy, field="ofy", minimum=-0x80000000,
            maximum=0x7FFFFFFF)
        _require_range(self.h, field="h", minimum=0, maximum=0xFFFF)
        _require_range(
            self.zsf3, field="zsf3", minimum=-0x8000, maximum=0x7FFF)
        _require_range(
            self.zsf4, field="zsf4", minimum=-0x8000, maximum=0x7FFF)
        _require_range(
            self.dqa, field="dqa", minimum=-0x8000, maximum=0x7FFF)
        _require_range(
            self.dqb, field="dqb", minimum=-0x80000000,
            maximum=0x7FFFFFFF)


def urban_assault_gte_config(
        *, width: int, height: int) -> GteProjectionConfig:
    """Return only one of the two presentation branches proven in the EXE.

    There is intentionally no default branch.  Arbitrary viewer target sizes
    must not be mistaken for native game GTE constants.
    """

    parsed_width = _require_plain_int(width, field="width")
    parsed_height = _require_plain_int(height, field="height")
    if parsed_width != UA_GTE_VIDEO_WIDTH:
        raise PsxGteError(
            "Urban Assault GTE evidence only supports a 512-pixel width")
    if parsed_height == UA_GTE_VIDEO_HEIGHT_NTSC:
        branch = "ntsc_512x240"
    elif parsed_height == UA_GTE_VIDEO_HEIGHT_PAL:
        branch = "pal_512x256"
    else:
        raise PsxGteError(
            "Urban Assault GTE evidence only supports heights 240 and 256")
    return GteProjectionConfig(
        video_width=parsed_width,
        video_height=parsed_height,
        branch=branch,
        ofx=(parsed_width // 2) << 16,
        ofy=(parsed_height // 2) << 16,
        h=UA_GTE_H,
        zsf3=UA_GTE_ZSF3,
        zsf4=UA_GTE_ZSF4,
        dqa=UA_GTE_DQA,
        dqb=UA_GTE_DQB,
    )


@dataclass(frozen=True, slots=True)
class GteDivideResult:
    """The 17-bit GTE reciprocal result and division-overflow state."""

    quotient: int
    overflow: bool


def _unr_table_value(index: int) -> int:
    value = ((0x40000 // (index + 0x100)) + 1) // 2 - 0x101
    return max(0, value)


def gte_divide(numerator: int, denominator: int) -> GteDivideResult:
    """Reproduce the PS1 GTE's UNR-table unsigned 16-bit division."""

    parsed_numerator = _require_range(
        numerator, field="numerator", minimum=0, maximum=0xFFFF)
    parsed_denominator = _require_range(
        denominator, field="denominator", minimum=0, maximum=0xFFFF)
    if parsed_numerator >= parsed_denominator * 2:
        return GteDivideResult(PSX_DIVIDE_MAX, True)

    shift = 16 - parsed_denominator.bit_length()
    r1 = (parsed_denominator << shift) & 0x7FFF
    r2 = _unr_table_value((r1 + 0x40) >> 7) + 0x101
    r3 = ((0x80 - r2 * (r1 + 0x8000)) >> 8) & PSX_DIVIDE_MAX
    reciprocal = ((r2 * r3) + 0x80) >> 8
    result = (
        reciprocal * (parsed_numerator << shift) + 0x8000) >> 16
    return GteDivideResult(min(PSX_DIVIDE_MAX, result), False)


@dataclass(frozen=True, slots=True)
class GteScreenProjection:
    """One snapped SXY result from the RTPS/RTPT screen-output stage."""

    screen_x: int
    screen_y: int
    raw_x_fixed_16_16: int
    raw_y_fixed_16_16: int
    h_over_sz3: int
    division_overflow: bool
    x_saturated: bool
    y_saturated: bool


def gte_project_screen(
        ir1: int,
        ir2: int,
        sz3: int,
        config: GteProjectionConfig,
) -> GteScreenProjection:
    """Apply exact GTE perspective division, integer snap, and SXY clamp.

    ``ir1`` and ``ir2`` are the signed 16-bit transform results already
    produced by RTPS/RTPT.  This function intentionally does not invent the
    unresolved Urban Assault model/view matrix mapping.
    """

    parsed_ir1 = _require_range(
        ir1, field="ir1", minimum=-0x8000, maximum=0x7FFF)
    parsed_ir2 = _require_range(
        ir2, field="ir2", minimum=-0x8000, maximum=0x7FFF)
    parsed_sz3 = _require_range(
        sz3, field="sz3", minimum=0, maximum=0xFFFF)
    if not isinstance(config, GteProjectionConfig):
        raise PsxGteError("config must be a GteProjectionConfig")

    division = gte_divide(config.h, parsed_sz3)
    raw_x = config.ofx + parsed_ir1 * division.quotient
    raw_y = config.ofy + parsed_ir2 * division.quotient
    unsaturated_x = raw_x >> 16
    unsaturated_y = raw_y >> 16
    screen_x = min(PSX_SCREEN_MAX, max(PSX_SCREEN_MIN, unsaturated_x))
    screen_y = min(PSX_SCREEN_MAX, max(PSX_SCREEN_MIN, unsaturated_y))
    return GteScreenProjection(
        screen_x=screen_x,
        screen_y=screen_y,
        raw_x_fixed_16_16=raw_x,
        raw_y_fixed_16_16=raw_y,
        h_over_sz3=division.quotient,
        division_overflow=division.overflow,
        x_saturated=screen_x != unsaturated_x,
        y_saturated=screen_y != unsaturated_y,
    )


@dataclass(frozen=True, slots=True)
class GteNclipResult:
    """NCLIP MAC0 plus the two MAC0 overflow conditions."""

    mac0: int
    positive_overflow: bool
    negative_overflow: bool


def _screen_point(point: tuple[int, int], *, field: str) -> tuple[int, int]:
    if type(point) is not tuple or len(point) != 2:
        raise PsxGteError(f"{field} must be a two-integer tuple")
    return (
        _require_range(
            point[0], field=f"{field}.x", minimum=-0x8000,
            maximum=0x7FFF),
        _require_range(
            point[1], field=f"{field}.y", minimum=-0x8000,
            maximum=0x7FFF),
    )


def gte_nclip(
        sxy0: tuple[int, int],
        sxy1: tuple[int, int],
        sxy2: tuple[int, int],
) -> GteNclipResult:
    """Reproduce NCLIP on the snapped signed-16-bit SXY registers."""

    x0, y0 = _screen_point(sxy0, field="sxy0")
    x1, y1 = _screen_point(sxy1, field="sxy1")
    x2, y2 = _screen_point(sxy2, field="sxy2")
    raw = (
        x0 * y1 + x1 * y2 + x2 * y0
        - x0 * y2 - x1 * y0 - x2 * y1)
    return GteNclipResult(
        mac0=_signed32(raw),
        positive_overflow=raw > 0x7FFFFFFF,
        negative_overflow=raw < -0x80000000,
    )


@dataclass(frozen=True, slots=True)
class GteAverageDepth:
    """AVSZ MAC0/OTZ output and its independently useful saturation flags."""

    mac0: int
    otz: int
    otz_saturated: bool
    mac0_positive_overflow: bool
    mac0_negative_overflow: bool


def _gte_average_depth(depths: tuple[int, ...], z_scale: int) -> GteAverageDepth:
    parsed_depths = tuple(
        _require_range(
            value, field=f"depth[{index}]", minimum=0, maximum=0xFFFF)
        for index, value in enumerate(depths))
    parsed_z_scale = _require_range(
        z_scale, field="z_scale", minimum=-0x8000, maximum=0x7FFF)
    raw = parsed_z_scale * sum(parsed_depths)
    shifted = raw >> 12
    otz = min(0xFFFF, max(0, shifted))
    return GteAverageDepth(
        mac0=_signed32(raw),
        otz=otz,
        otz_saturated=otz != shifted,
        mac0_positive_overflow=raw > 0x7FFFFFFF,
        mac0_negative_overflow=raw < -0x80000000,
    )


def gte_avsz3(sz1: int, sz2: int, sz3: int, zsf3: int) -> GteAverageDepth:
    """Reproduce AVSZ3, which consumes SZ1+SZ2+SZ3 (not SZ0)."""

    return _gte_average_depth((sz1, sz2, sz3), zsf3)


def gte_avsz4(
        sz0: int, sz1: int, sz2: int, sz3: int,
        zsf4: int) -> GteAverageDepth:
    """Reproduce AVSZ4 over SZ0+SZ1+SZ2+SZ3."""

    return _gte_average_depth((sz0, sz1, sz2, sz3), zsf4)


def psx_dither_offset(screen_x: int, screen_y: int) -> int:
    """Return the hardware-verified screen-space 4x4 dither offset."""

    parsed_x = _require_plain_int(screen_x, field="screen_x")
    parsed_y = _require_plain_int(screen_y, field="screen_y")
    return PSX_DITHER_MATRIX[parsed_y & 3][parsed_x & 3]


def psx_quantize_channel_5(
        channel: int,
        screen_x: int,
        screen_y: int,
        *,
        dither_enabled: bool,
) -> int:
    """Quantize one 8-bit channel to five bits with explicit dither state."""

    parsed_channel = _require_range(
        channel, field="channel", minimum=0, maximum=0xFF)
    if type(dither_enabled) is not bool:
        raise PsxGteError("dither_enabled must be a boolean")
    if dither_enabled:
        parsed_channel += psx_dither_offset(screen_x, screen_y)
        parsed_channel = min(0xFF, max(0, parsed_channel))
    return parsed_channel >> 3


def psx_quantize_bgr555(
        red: int,
        green: int,
        blue: int,
        screen_x: int,
        screen_y: int,
        *,
        dither_enabled: bool,
) -> int:
    """Pack RGB888 as PSX BGR555 without inferring dither enablement."""

    red5 = psx_quantize_channel_5(
        red, screen_x, screen_y, dither_enabled=dither_enabled)
    green5 = psx_quantize_channel_5(
        green, screen_x, screen_y, dither_enabled=dither_enabled)
    blue5 = psx_quantize_channel_5(
        blue, screen_x, screen_y, dither_enabled=dither_enabled)
    return red5 | (green5 << 5) | (blue5 << 10)


def ua_ot_bucket_relative_byte_offset(otz: int) -> int:
    """Return the literal late-overlay OT address delta: ``OTZ*4 - 64``.

    This is deliberately a relative byte offset, not a claim about the
    unresolved allocation base or table traversal direction.
    """

    parsed_otz = _require_range(
        otz, field="otz", minimum=0, maximum=0xFFFF)
    return parsed_otz * PSX_OT_ENTRY_BYTES - (
        UA_OT_BUCKET_BIAS * PSX_OT_ENTRY_BYTES)


_Packet = TypeVar("_Packet")


def psx_add_prim_same_bucket_order(
        submission_order: Iterable[_Packet]) -> tuple[_Packet, ...]:
    """Return packet encounter order after head insertion into one bucket.

    PsyQ-style ``addPrim`` links every new packet ahead of the prior head, so
    packets sharing a bucket are encountered last-submitted first regardless
    of how buckets themselves are traversed.
    """

    return tuple(reversed(tuple(submission_order)))


def ua_late_unit_gt4_command(descriptor_tpage: int) -> int:
    """Select GT4 code from the recovered path's resolved TPage word.

    The binding from a PW3 on-disk selector to this runtime descriptor remains
    unresolved.  Callers must therefore supply a descriptor TPage; treating a
    selector or other face field as the TPage would cross the evidence gate.
    """

    parsed_tpage = _require_range(
        descriptor_tpage, field="descriptor_tpage", minimum=0,
        maximum=0xFFFF)
    if parsed_tpage & UA_LATE_UNIT_GT4_TPAGE_ABR_MASK:
        return PSX_POLY_GT4_SEMITRANSPARENT_COMMAND
    return PSX_POLY_GT4_OPAQUE_COMMAND


__all__ = [
    "GteAverageDepth",
    "GteDivideResult",
    "GteNclipResult",
    "GteProjectionConfig",
    "GteScreenProjection",
    "HARDWARE_HELPER_EVIDENCE",
    "PSX_GTE_HELPER_PROFILE_ID",
    "PSX_DITHER_MATRIX",
    "PSX_DIVIDE_MAX",
    "PSX_OT_ENTRY_BYTES",
    "PSX_POLY_GT4_FIELD_OFFSETS",
    "PSX_POLY_GT4_OPAQUE_COMMAND",
    "PSX_POLY_GT4_PACKET_SIZE",
    "PSX_POLY_GT4_SEMITRANSPARENT_COMMAND",
    "PSX_SCREEN_MAX",
    "PSX_SCREEN_MIN",
    "PsxGteError",
    "UA_DITHER_ENABLE_STATE",
    "UA_GTE_CONFIG_EVIDENCE",
    "UA_GTE_CONFIG_SELECTION_STATE",
    "UA_GTE_CONTROL_INIT_FILE_RANGE",
    "UA_GTE_CONTROL_INIT_RANGE_SHA256",
    "UA_GTE_DQA",
    "UA_GTE_DQB",
    "UA_GTE_H",
    "UA_GTE_RENDER_CALL_FILE_RANGES",
    "UA_GTE_RENDER_CALL_RANGE_SHA256",
    "UA_GTE_RENDER_INIT_FILE_RANGE",
    "UA_GTE_RENDER_INIT_RANGE_SHA256",
    "UA_GTE_VIDEO_HEIGHT_NTSC",
    "UA_GTE_VIDEO_HEIGHT_PAL",
    "UA_GTE_VIDEO_WIDTH",
    "UA_GTE_ZSF3",
    "UA_GTE_ZSF4",
    "UA_JUNE_MAIN_EXE_SHA256",
    "UA_LATE_UNIT_GT4_PATH_EVIDENCE",
    "UA_LATE_UNIT_GT4_TPAGE_ABR_MASK",
    "UA_OT_BUCKET_BIAS",
    "UA_OT_BUCKET_FORMULA_EVIDENCE",
    "UA_OT_TRAVERSAL_STATE",
    "UA_VIEWER_TRANSFORM_MAPPING_STATE",
    "gte_avsz3",
    "gte_avsz4",
    "gte_divide",
    "gte_nclip",
    "gte_project_screen",
    "psx_add_prim_same_bucket_order",
    "psx_dither_offset",
    "psx_quantize_bgr555",
    "psx_quantize_channel_5",
    "ua_late_unit_gt4_command",
    "ua_ot_bucket_relative_byte_offset",
    "urban_assault_gte_config",
]
