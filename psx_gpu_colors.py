"""Evidence-bounded PlayStation GPU color math for native PW3 materials.

This module implements color semantics that can be separated cleanly from
polygon rasterization.  It does not infer a material's texture-page word from
its PW3 selector, it does not select one of the recovered shade routines as
the normal UNIT renderer, and it does not approximate PSX dithering.

The Urban Assault-specific evidence is the recovered 15 June 1999
``OVER1.TXT`` overlay (SHA-256 :data:`UA_JUNE_OVER1_SHA256`):

* runtime ``0x800CFB70..0x800CFBAC`` writes disk shade bytes
  ``+23,+22,+24,+25`` as grayscale GT4 colors in raw-corner order
  ``1,0,2,3``;
* runtime ``0x800CF68C..0x800CF7A4`` writes the tinted variant as
  ``floor(shade * channel / 256)`` independently for R, G, and B; and
* runtime ``0x800CFBB0..0x800CFBD0`` embeds a resolved texture descriptor's
  TPage word and changes the GT4 command from ``0x3C`` to ``0x3E`` only when
  TPage ABR bits 5-6 are nonzero.

The fragment rules and four ABR equations match the archived PCSX-Redux
hardware fixtures under ``gpu-raster-phase8`` and ``gpu-raster-phase12``:
resolved word ``0x0000`` skips the write; a textured semi-transparent
primitive blends only texels whose resolved CLUT word has bit 15 set; and
that bit propagates to the written VRAM word.  This module deliberately
stops before drawing-area mask control, dithering, edge walking, or primitive
ordering.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum


PSX_GPU_COLOR_PROFILE_ID = "psx_gpu_color_math_v1"

PSX_VERTEX_COLOR_NEUTRAL = 0x80
PW3_PACKET_RAW_CORNER_ORDER = (1, 0, 2, 3)

PSX_TPAGE_ABR_MASK = 0x0060
PSX_GT4_OPAQUE_COMMAND = 0x3C
PSX_GT4_SEMITRANSPARENT_COMMAND = 0x3E

# Portable provenance anchors.  File offsets are half-open ranges and do not
# expose a workstation path.  Tests can verify them when the recovered corpus
# is explicitly enabled with OPENUA_PSX_CORPUS_ROOT.
UA_JUNE_OVER1_SHA256 = (
    "8bac11c449e06e4a2b8309d3b44e666bf9b10d6d34208af63227395205e6d5cf")
UA_JUNE_OVER1_LOAD_ADDRESS = 0x800BD5FC
UA_DIRECT_SHADE_FILE_RANGE = (0x12574, 0x125B0)
UA_DIRECT_SHADE_RANGE_SHA256 = (
    "80e208def25f28beb7a2adfa026ab6fda2eb1be433eb3412ccf91a1e827f38c4")
UA_TINTED_SHADE_FILE_RANGE = (0x12090, 0x121AC)
UA_TINTED_SHADE_RANGE_SHA256 = (
    "5a3f71cbd548237fd3620627069e1ea7373fcaf9b43d6962ee05fc58a320f48e")
UA_TPAGE_DISPATCH_FILE_RANGE = (0x125B0, 0x125D4)
UA_TPAGE_DISPATCH_RANGE_SHA256 = (
    "954653e2595e9ae3009769e7cd22ea8d6c8142516f5b0c6366f1c0d6f1736200")

# No recovered file field has yet been proven to supply the TPage ABR bits.
# A caller must provide the resolved runtime texture-descriptor TPage word;
# there is intentionally no selector-only state factory.
UA_SELECTOR_TO_DESCRIPTOR_TPAGE_STATUS = "unresolved_hard_gate"

# Both executable routines are real and their local arithmetic is exact, but
# neither recovered entry point has a proven normal UNIT-render call chain.
# Keep routine availability separate from an effective-profile assertion.
UA_PW3_SHADE_ROUTINE_DISPATCH_STATUS = "unresolved_hard_gate"


class PsxGpuColorError(ValueError):
    """Raised when PSX color state is missing, inconsistent, or out of range."""


class PsxAbrMode(IntEnum):
    """The four hardware ABR equations selected by TPage bits 5-6."""

    HALF_BACK_PLUS_HALF_FRONT = 0
    BACK_PLUS_FRONT = 1
    BACK_MINUS_FRONT = 2
    BACK_PLUS_QUARTER_FRONT = 3


def _plain_int(value: object, *, field: str) -> int:
    if type(value) is not int:
        raise PsxGpuColorError(f"{field} must be an integer")
    return value


def _bounded_int(
        value: object, *, field: str, minimum: int, maximum: int) -> int:
    number = _plain_int(value, field=field)
    if not minimum <= number <= maximum:
        raise PsxGpuColorError(
            f"{field} {number!r} is outside {minimum}..{maximum}")
    return number


def _u8(value: object, *, field: str) -> int:
    return _bounded_int(value, field=field, minimum=0, maximum=0xFF)


def _u16(value: object, *, field: str) -> int:
    return _bounded_int(value, field=field, minimum=0, maximum=0xFFFF)


def _rgb8(value: object, *, field: str) -> tuple[int, int, int]:
    if not isinstance(value, (tuple, list)) or len(value) != 3:
        raise PsxGpuColorError(f"{field} must contain exactly three channels")
    return tuple(
        _u8(channel, field=f"{field} channel {index}")
        for index, channel in enumerate(value)
    )  # type: ignore[return-value]


def _four_shades(value: object) -> tuple[int, int, int, int]:
    if not isinstance(value, (tuple, list)) or len(value) != 4:
        raise PsxGpuColorError(
            "PW3 raw corner shades must contain exactly four bytes")
    return tuple(
        _u8(shade, field=f"PW3 raw corner shade {index}")
        for index, shade in enumerate(value)
    )  # type: ignore[return-value]


def _abr_mode(value: PsxAbrMode | int) -> PsxAbrMode:
    if isinstance(value, PsxAbrMode):
        return value
    number = _bounded_int(
        value, field="PSX ABR mode", minimum=0, maximum=3)
    return PsxAbrMode(number)


@dataclass(frozen=True, slots=True)
class PsxPw3Gt4State:
    """UA's executable-proven GT4 state from a resolved descriptor TPage.

    This record cannot represent a selector guess.  In this particular UA
    path, ABR field zero emits opaque command ``0x3C``; nonzero ABR fields
    emit semi-transparent command ``0x3E``.
    """

    descriptor_tpage: int
    abr_mode: PsxAbrMode
    primitive_semitransparent: bool
    command_code: int

    def __post_init__(self) -> None:
        tpage = _u16(
            self.descriptor_tpage, field="resolved descriptor TPage")
        expected_abr = PsxAbrMode((tpage & PSX_TPAGE_ABR_MASK) >> 5)
        expected_semitransparent = bool(tpage & PSX_TPAGE_ABR_MASK)
        expected_code = (
            PSX_GT4_SEMITRANSPARENT_COMMAND
            if expected_semitransparent else PSX_GT4_OPAQUE_COMMAND)
        if self.abr_mode is not expected_abr:
            raise PsxGpuColorError(
                "PW3 GT4 ABR mode does not match descriptor TPage bits 5-6")
        if type(self.primitive_semitransparent) is not bool \
                or self.primitive_semitransparent \
                is not expected_semitransparent:
            raise PsxGpuColorError(
                "PW3 GT4 semi-transparency state does not match UA's "
                "TPage dispatch")
        if type(self.command_code) is not int \
                or self.command_code != expected_code:
            raise PsxGpuColorError(
                "PW3 GT4 command code does not match UA's TPage dispatch")


@dataclass(frozen=True, slots=True)
class PsxTexturedFragment:
    """One no-dither textured-fragment result in PSX BGR555/STP space."""

    source_word: int
    background_word: int
    vertex_rgb: tuple[int, int, int]
    modulated_rgb8: tuple[int, int, int]
    output_word: int
    wrote: bool
    zero_word_discarded: bool
    source_stp: bool
    blend_applied: bool
    primitive_semitransparent: bool
    abr_mode: PsxAbrMode


def pw3_packet_corner_shades(
        raw_corner_shades: object) -> tuple[int, int, int, int]:
    """Map four disk-order shade bytes to the executable's GT4 order."""

    raw = _four_shades(raw_corner_shades)
    return tuple(raw[index] for index in PW3_PACKET_RAW_CORNER_ORDER)


def pw3_direct_packet_vertex_rgb(
        raw_corner_shades: object) -> tuple[tuple[int, int, int], ...]:
    """Return the direct routine's grayscale RGB command colors."""

    return tuple(
        (shade, shade, shade)
        for shade in pw3_packet_corner_shades(raw_corner_shades)
    )


def pw3_tinted_packet_vertex_rgb(
        raw_corner_shades: object, tint_rgb: object) \
        -> tuple[tuple[int, int, int], ...]:
    """Return the alternate routine's exact per-channel tinted colors.

    Each output channel is ``(shade * tint_channel) >> 8``.  The shift is a
    truncating divide by 256; it is not division by 255 and is not rounded.
    """

    tint = _rgb8(tint_rgb, field="PW3 tint RGB")
    return tuple(
        tuple((shade * channel) >> 8 for channel in tint)
        for shade in pw3_packet_corner_shades(raw_corner_shades)
    )


def psx_texture_modulate_channel(
        texel_channel_5bit: object, vertex_channel_8bit: object) -> int:
    """Return one pre-dither PSX textured channel on the 8-bit scale.

    The exact intermediate is ``(texel5 * vertex8) >> 4``.  Vertex value 128
    is therefore neutral: it maps ``texel5`` to ``texel5 << 3`` exactly.
    This result deliberately remains *wide* (0..494): the archived renderer
    applies ABR first and saturates the blended result afterwards.  Clamping
    an over-bright modulated foreground here would change ABR 0 and ABR 3.
    """

    texel = _bounded_int(
        texel_channel_5bit, field="PSX texel channel",
        minimum=0, maximum=0x1F)
    vertex = _u8(vertex_channel_8bit, field="PSX vertex color channel")
    return (texel * vertex) >> 4


def psx_texture_modulate_bgr555(
        source_word: object, vertex_rgb: object) -> tuple[int, int, int]:
    """Modulate a resolved BGR555 texel with an interpolated vertex color.

    The returned tuple is RGB on the GPU's pre-dither 8-bit channel scale;
    over-bright intermediates may exceed 255 and are saturated only after
    opaque output or ABR evaluation.
    Word-zero discard and STP blending are handled by
    :func:`shade_pw3_textured_fragment`.
    """

    word = _u16(source_word, field="resolved PSX texture word")
    red, green, blue = _rgb8(vertex_rgb, field="PSX vertex RGB")
    return (
        psx_texture_modulate_channel(word & 0x1F, red),
        psx_texture_modulate_channel((word >> 5) & 0x1F, green),
        psx_texture_modulate_channel((word >> 10) & 0x1F, blue),
    )


def psx_abr_blend_channel_8(
        background_channel_5bit: object,
        foreground_channel_8bit: object,
        abr_mode: PsxAbrMode | int) -> int:
    """Apply one ABR equation and return its saturated 8-bit-scale value.

    ``foreground_channel_8bit`` is the wide post-modulation intermediate
    (0..494), not an already saturated byte.  The equations are evaluated
    with integer shifts/floors and the result is then clamped to 0..255.
    """

    background = _bounded_int(
        background_channel_5bit, field="PSX background channel",
        minimum=0, maximum=0x1F) << 3
    foreground = _bounded_int(
        foreground_channel_8bit, field="PSX foreground channel",
        minimum=0, maximum=(0x1F * 0xFF) >> 4)
    mode = _abr_mode(abr_mode)
    if mode is PsxAbrMode.HALF_BACK_PLUS_HALF_FRONT:
        return min(0xFF, (background + foreground) >> 1)
    if mode is PsxAbrMode.BACK_PLUS_FRONT:
        return min(0xFF, background + foreground)
    if mode is PsxAbrMode.BACK_MINUS_FRONT:
        return max(0, background - foreground)
    return min(0xFF, background + (foreground >> 2))


def psx_abr_blend_channel_5(
        background_channel_5bit: object,
        foreground_channel_8bit: object,
        abr_mode: PsxAbrMode | int) -> int:
    """Apply one ABR equation and truncate the result back to five bits."""

    return psx_abr_blend_channel_8(
        background_channel_5bit,
        foreground_channel_8bit,
        abr_mode,
    ) >> 3


def pw3_gt4_state_from_descriptor_tpage(
        descriptor_tpage: object | None) -> PsxPw3Gt4State:
    """Decode UA GT4 state from an already resolved runtime TPage word.

    ``None`` fails closed.  The PW3 texture selector is not a TPage and no
    selector-to-ABR rule has been proven from the recovered source files.
    """

    if descriptor_tpage is None:
        raise PsxGpuColorError(
            "PW3 selector-to-descriptor TPage/ABR binding is unresolved; "
            "a resolved runtime texture-descriptor TPage word is required")
    tpage = _u16(
        descriptor_tpage, field="resolved descriptor TPage")
    abr_mode = PsxAbrMode((tpage & PSX_TPAGE_ABR_MASK) >> 5)
    primitive_semitransparent = bool(tpage & PSX_TPAGE_ABR_MASK)
    return PsxPw3Gt4State(
        descriptor_tpage=tpage,
        abr_mode=abr_mode,
        primitive_semitransparent=primitive_semitransparent,
        command_code=(
            PSX_GT4_SEMITRANSPARENT_COMMAND
            if primitive_semitransparent else PSX_GT4_OPAQUE_COMMAND),
    )


def _bgr555_word(
        red5: int, green5: int, blue5: int, *, stp: bool) -> int:
    return (
        red5 | (green5 << 5) | (blue5 << 10)
        | (0x8000 if stp else 0))


def shade_pw3_textured_fragment(
        source_word: object,
        vertex_rgb: object,
        background_word: object,
        state: PsxPw3Gt4State) -> PsxTexturedFragment:
    """Resolve one UA PW3 textured fragment without dithering.

    A resolved source word of ``0x0000`` leaves the background untouched.
    For a semi-transparent GT4, ABR is applied only when the source texel's
    STP bit is set; an STP-clear texel overwrites opaquely.  For an opaque
    GT4, STP does not cause blending.  A nonzero source STP bit propagates to
    the output word in either case, matching the archived hardware fixtures.

    ``state`` must come from a resolved descriptor TPage.  The routine does
    not accept a PW3 selector in its place.
    """

    word = _u16(source_word, field="resolved PSX texture word")
    background = _u16(background_word, field="PSX background word")
    rgb = _rgb8(vertex_rgb, field="PSX vertex RGB")
    if not isinstance(state, PsxPw3Gt4State):
        raise PsxGpuColorError(
            "PW3 textured shading requires resolved PsxPw3Gt4State")

    modulated = psx_texture_modulate_bgr555(word, rgb)
    source_stp = bool(word & 0x8000)
    if word == 0:
        return PsxTexturedFragment(
            source_word=word,
            background_word=background,
            vertex_rgb=rgb,
            modulated_rgb8=modulated,
            output_word=background,
            wrote=False,
            zero_word_discarded=True,
            source_stp=False,
            blend_applied=False,
            primitive_semitransparent=(
                state.primitive_semitransparent),
            abr_mode=state.abr_mode,
        )

    blend = state.primitive_semitransparent and source_stp
    if blend:
        output_channels = (
            psx_abr_blend_channel_5(
                background & 0x1F, modulated[0], state.abr_mode),
            psx_abr_blend_channel_5(
                (background >> 5) & 0x1F,
                modulated[1], state.abr_mode),
            psx_abr_blend_channel_5(
                (background >> 10) & 0x1F,
                modulated[2], state.abr_mode),
        )
    else:
        output_channels = tuple(min(0xFF, channel) >> 3
                                for channel in modulated)

    return PsxTexturedFragment(
        source_word=word,
        background_word=background,
        vertex_rgb=rgb,
        modulated_rgb8=modulated,
        output_word=_bgr555_word(
            output_channels[0], output_channels[1], output_channels[2],
            stp=source_stp),
        wrote=True,
        zero_word_discarded=False,
        source_stp=source_stp,
        blend_applied=blend,
        primitive_semitransparent=state.primitive_semitransparent,
        abr_mode=state.abr_mode,
    )
