"""Strict, Qt-free decoders for Urban Assault PSX ``SETnGFX.BIN``.

The 15/16 June 1999 PlayStation builds store each environmental texture set
in a compact 0x41400-byte table.  The table contains 128 selector-indexed
CLUT slots and 32 packed four-bit pixel banks.  A PW3 face selector ``S``
uses CLUT slot ``S`` and pixel bank ``S & 0x1F``.

The December 1998 through May 1999 builds use a separate, sector-padded
layout.  It has 128 direct selector slots.  A populated slot stores the same
eight-byte header, CLUT, and 0x2000-byte pixel payload, then pads the record to
five CD sectors with 0xBA.  An absent slot is one sector containing the
four-byte ``4e0d0a1a`` marker and 0xBA fill.  Unlike the late layout, selector
``S`` uses the pixel payload in direct slot ``S``; selectors above 31 are not
aliases for the low five bits.  Only the three exact corpus sizes and this
independently cross-validated grammar are accepted.

The decoder deliberately has no Qt dependency.  Returned palettes, indexed
pixels, and RGBA pixels are tuples or ``bytes`` and all public records are
frozen.  The PSX STP bit is retained on every palette entry.  RGBA output
uses the conservative textured-color rule: a resolved CLUT word of 0x0000
is transparent and every nonzero word is opaque.  In particular, palette
index zero is *not* universally transparent--several canonical used slots
store a nonzero word at CLUT index zero.  Exact STP/ABR blending remains a
renderer concern because the mesh face's opaque field has not been decoded.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path
import struct


LATE_SETGFX_SIZE = 0x41400
MATERIAL_SLOT_COUNT = 128
PIXEL_BANK_COUNT = 32
TEXTURE_WIDTH = 128
TEXTURE_HEIGHT = 128
PACKED_PIXEL_BYTES = 0x2000
INDEXED_PIXEL_BYTES = TEXTURE_WIDTH * TEXTURE_HEIGHT
RECORD_HEADER_SIZE = 8
CLUT_ENTRY_COUNT = 16
CLUT_BYTES = CLUT_ENTRY_COUNT * 2
FULL_RECORD_SIZE = RECORD_HEADER_SIZE + CLUT_BYTES + PACKED_PIXEL_BYTES
PALETTE_RECORD_SIZE = RECORD_HEADER_SIZE + CLUT_BYTES

ZERO_RECORD_HEADER = b"\x00" * RECORD_HEADER_SIZE
REPEAT_RECORD_HEADER = bytes.fromhex("4e0d0a1a00000000")
_ALLOWED_PALETTE_HEADERS = frozenset(
    (ZERO_RECORD_HEADER, REPEAT_RECORD_HEADER))

LATE_SETGFX_LAYOUT_ID = "late_compact_setgfx_v1"
LATE_SELECTOR_TO_PIXEL_BANK_MAPPING = (
    "selector_S_clut_S_pixel_bank_S_and_31")

SECTOR_SIZE = 0x800
SECTOR_PAD_BYTE = 0xBA
SECTOR_PADDED_FULL_ALLOCATION = 0x2800
SECTOR_PADDED_EMPTY_ALLOCATION = SECTOR_SIZE
SECTOR_PADDED_EMPTY_MARKER = bytes.fromhex("4e0d0a1a")
SECTOR_PADDED_SETGFX_LAYOUT_ID = "sector_padded_setgfx_direct_v1"
SECTOR_PADDED_SELECTOR_TO_PIXEL_BANK_MAPPING = (
    "selector_S_clut_S_pixel_bank_direct_slot_S")
# These are the only complete sector-padded table sizes in the local corpus.
# Each count follows independently from parsing exactly 128 slots.
SECTOR_PADDED_OBSERVED_LAYOUTS = (
    (0xDA000, 77),  # 18 December 1998
    (0xDC000, 78),  # 12 March 1999
    (0xE2000, 81),  # 14 May 1999
)
SECTOR_PADDED_OBSERVED_SIZES = frozenset(
    size for size, _material_count in SECTOR_PADDED_OBSERVED_LAYOUTS)


class PsxNativeTextureError(ValueError):
    """Raised when a candidate native PSX texture pack is not exact."""


def _require_plain_int(value: object, *, field: str) -> int:
    if type(value) is not int:
        raise PsxNativeTextureError(f"{field} must be an integer")
    return value


def _expand_5bit(value: int) -> int:
    return (value << 3) | (value >> 2)


@dataclass(frozen=True, slots=True)
class PsxBgr555Color:
    """One immutable PSX BGR555/STP word and its expanded RGB value."""

    word: int
    red: int
    green: int
    blue: int
    stp: bool

    @property
    def is_transparent_zero_word(self) -> bool:
        """Whether this resolved PSX textured color is exactly 0x0000."""

        return self.word == 0

    @property
    def rgba(self) -> tuple[int, int, int, int]:
        """Return conservative RGBA while leaving STP blending unresolved.

        A zero resolved CLUT word is transparent.  Nonzero entries are kept
        opaque, including 0x8000, because STP alone is not a portable alpha
        value without the primitive semi-transparency state and ABR mode.
        """

        alpha = 0 if self.is_transparent_zero_word else 255
        return self.red, self.green, self.blue, alpha


def decode_bgr555(word: int) -> PsxBgr555Color:
    """Decode one little-endian PSX palette word without discarding STP."""

    value = _require_plain_int(word, field="BGR555 word")
    if not 0 <= value <= 0xFFFF:
        raise PsxNativeTextureError(
            f"BGR555 word {value!r} is outside 0..65535")
    return PsxBgr555Color(
        word=value,
        red=_expand_5bit(value & 0x1F),
        green=_expand_5bit((value >> 5) & 0x1F),
        blue=_expand_5bit((value >> 10) & 0x1F),
        stp=bool(value & 0x8000),
    )


@dataclass(frozen=True, slots=True)
class PsxNativeTextureSlot:
    """One selector-indexed texture slot.

    Compact slots and populated sector-padded slots have a complete CLUT.
    An unavailable sector-padded slot retains its four-byte marker and source
    offset but has empty palette tuples and ``available=False``.
    """

    selector: int
    source_offset: int
    header: bytes
    palette_words: tuple[int, ...]
    palette: tuple[PsxBgr555Color, ...]
    available: bool = True
    allocation_size: int | None = None

    @property
    def has_repeat_marker(self) -> bool:
        return self.header == REPEAT_RECORD_HEADER


@dataclass(frozen=True, slots=True)
class PsxNativeMaterial:
    """One resolved material: indexed pixels plus selector-specific CLUT."""

    selector: int
    pixel_bank: int
    width: int
    height: int
    indices: bytes
    palette_words: tuple[int, ...]
    palette: tuple[PsxBgr555Color, ...]
    rgba: bytes


@dataclass(frozen=True, slots=True)
class PsxNativeTexturePack:
    """Immutable, layout-identified ``SETnGFX.BIN`` material table."""

    logical_path: str
    source_sha256: str
    slots: tuple[PsxNativeTextureSlot, ...]
    pixel_banks: tuple[bytes, ...]
    layout_id: str = LATE_SETGFX_LAYOUT_ID
    selector_to_pixel_bank_mapping: str = (
        LATE_SELECTOR_TO_PIXEL_BANK_MAPPING)
    selector_pixel_banks: tuple[int | None, ...] = ()

    def slot(self, selector: int) -> PsxNativeTextureSlot:
        value = _validate_selector(selector)
        return self.slots[value]

    def has_material(self, selector: int) -> bool:
        """Return whether ``selector`` has a CLUT and pixel payload."""

        value = _validate_selector(selector)
        slot = self.slots[value]
        return bool(
            slot.available and self._pixel_bank_number(value) is not None)

    @property
    def material_slot_count(self) -> int:
        """Return the number of selectors with complete material records."""

        return sum(self.has_material(selector)
                   for selector in range(MATERIAL_SLOT_COUNT))

    @property
    def populated_selectors(self) -> tuple[int, ...]:
        """Return selectors whose material records are present."""

        return tuple(selector for selector in range(MATERIAL_SLOT_COUNT)
                     if self.has_material(selector))

    def _pixel_bank_number(self, selector: int) -> int | None:
        if self.selector_pixel_banks:
            if len(self.selector_pixel_banks) != MATERIAL_SLOT_COUNT:
                raise PsxNativeTextureError(
                    "texture pack selector-to-bank table is not 128 entries")
            bank_number = self.selector_pixel_banks[selector]
        else:
            # Backward compatibility for callers that constructed the original
            # late-pack record before explicit mapping metadata was added.
            bank_number = selector & (PIXEL_BANK_COUNT - 1)
        if bank_number is None:
            return None
        if type(bank_number) is not int \
                or not 0 <= bank_number < len(self.pixel_banks):
            raise PsxNativeTextureError(
                f"texture selector {selector} maps to invalid pixel bank "
                f"{bank_number!r}")
        return bank_number

    def material(self, selector: int) -> PsxNativeMaterial:
        """Resolve ``selector`` through the pack's immutable bank mapping."""

        value = _validate_selector(selector)
        slot = self.slots[value]
        if not slot.available:
            raise PsxNativeTextureError(
                f"texture selector {value} is an empty sector-padded slot")
        bank_number = self._pixel_bank_number(value)
        if bank_number is None:
            raise PsxNativeTextureError(
                f"texture selector {value} has no pixel bank")
        indices = self.pixel_banks[bank_number]
        if len(indices) != INDEXED_PIXEL_BYTES:
            raise PsxNativeTextureError(
                f"texture selector {value} pixel bank contains "
                f"{len(indices)} indices; expected {INDEXED_PIXEL_BYTES}")
        if len(slot.palette) != CLUT_ENTRY_COUNT:
            raise PsxNativeTextureError(
                f"texture selector {value} has {len(slot.palette)} CLUT "
                f"entries; expected {CLUT_ENTRY_COUNT}")
        rgba = bytearray(INDEXED_PIXEL_BYTES * 4)
        for pixel, palette_index in enumerate(indices):
            color = slot.palette[palette_index]
            offset = pixel * 4
            rgba[offset:offset + 4] = bytes(color.rgba)
        return PsxNativeMaterial(
            selector=value,
            pixel_bank=bank_number,
            width=TEXTURE_WIDTH,
            height=TEXTURE_HEIGHT,
            indices=indices,
            palette_words=slot.palette_words,
            palette=slot.palette,
            rgba=bytes(rgba),
        )


def _validate_selector(selector: int) -> int:
    value = _require_plain_int(selector, field="texture selector")
    if not 0 <= value < MATERIAL_SLOT_COUNT:
        raise PsxNativeTextureError(
            f"texture selector {value} is outside 0..127")
    return value


def _unpack_low_nibble_first(packed: bytes) -> bytes:
    if len(packed) != PACKED_PIXEL_BYTES:
        raise PsxNativeTextureError(
            f"pixel bank is {len(packed)} bytes; expected "
            f"{PACKED_PIXEL_BYTES}")
    indices = bytearray(INDEXED_PIXEL_BYTES)
    for offset, value in enumerate(packed):
        indices[offset * 2] = value & 0x0F
        indices[offset * 2 + 1] = value >> 4
    return bytes(indices)


def parse_late_setgfx_bytes(
        data: bytes, *, logical_path: str = "GFX/SETnGFX.BIN") \
        -> PsxNativeTexturePack:
    """Parse one strict 15/16 June 1999 compact texture pack.

    The parser fails closed on size, record-header, repeat-marker, or table
    cardinality deviations.  Repeat-marker slots still carry their complete
    palette bytes in the late file; no palette inheritance is performed.
    """

    if not isinstance(data, bytes):
        raise PsxNativeTextureError("late SETnGFX source must be bytes")
    if len(data) != LATE_SETGFX_SIZE:
        raise PsxNativeTextureError(
            f"{logical_path}: late SETnGFX is {len(data)} bytes; expected "
            f"{LATE_SETGFX_SIZE} (0x{LATE_SETGFX_SIZE:X})")

    slots: list[PsxNativeTextureSlot] = []
    pixel_banks: list[bytes] = []
    source_offset = 0
    for selector in range(MATERIAL_SLOT_COUNT):
        record_size = (
            FULL_RECORD_SIZE
            if selector < PIXEL_BANK_COUNT else PALETTE_RECORD_SIZE)
        record = data[source_offset:source_offset + record_size]
        if len(record) != record_size:
            raise PsxNativeTextureError(
                f"{logical_path}: selector {selector} record is truncated")
        header = record[:RECORD_HEADER_SIZE]
        if selector < PIXEL_BANK_COUNT:
            if header != ZERO_RECORD_HEADER:
                raise PsxNativeTextureError(
                    f"{logical_path}: pixel-bank selector {selector} has "
                    f"invalid header {header.hex()}")
        elif header not in _ALLOWED_PALETTE_HEADERS:
            raise PsxNativeTextureError(
                f"{logical_path}: palette selector {selector} has invalid "
                f"header {header.hex()}")

        palette_bytes = record[
            RECORD_HEADER_SIZE:RECORD_HEADER_SIZE + CLUT_BYTES]
        palette_words = struct.unpack("<16H", palette_bytes)
        if header == REPEAT_RECORD_HEADER:
            # Canonical late files materialize the repeated palette bytes.
            # Validate that evidence, but retain this slot's own bytes rather
            # than applying an implicit inheritance rule.
            if not slots or palette_words != slots[-1].palette_words:
                raise PsxNativeTextureError(
                    f"{logical_path}: repeat-marker selector {selector} "
                    "does not repeat the preceding explicit CLUT")
        palette = tuple(decode_bgr555(word) for word in palette_words)
        slots.append(PsxNativeTextureSlot(
            selector=selector,
            source_offset=source_offset,
            header=header,
            palette_words=tuple(palette_words),
            palette=palette,
        ))
        if selector < PIXEL_BANK_COUNT:
            packed = record[RECORD_HEADER_SIZE + CLUT_BYTES:]
            pixel_banks.append(_unpack_low_nibble_first(packed))
        source_offset += record_size

    if source_offset != len(data):
        raise PsxNativeTextureError(
            f"{logical_path}: parser stopped at 0x{source_offset:X}, "
            f"source ends at 0x{len(data):X}")
    if len(slots) != MATERIAL_SLOT_COUNT:
        raise AssertionError("late SETnGFX slot cardinality changed")
    if len(pixel_banks) != PIXEL_BANK_COUNT:
        raise AssertionError("late SETnGFX pixel-bank cardinality changed")
    return PsxNativeTexturePack(
        logical_path=logical_path.replace("\\", "/"),
        source_sha256=hashlib.sha256(data).hexdigest(),
        slots=tuple(slots),
        pixel_banks=tuple(pixel_banks),
        layout_id=LATE_SETGFX_LAYOUT_ID,
        selector_to_pixel_bank_mapping=(
            LATE_SELECTOR_TO_PIXEL_BANK_MAPPING),
        selector_pixel_banks=tuple(
            selector & (PIXEL_BANK_COUNT - 1)
            for selector in range(MATERIAL_SLOT_COUNT)),
    )


def parse_late_setgfx_file(
        path: Path, *, logical_path: str | None = None) \
        -> PsxNativeTexturePack:
    """Read and parse one late compact texture pack from disk."""

    source = Path(path)
    return parse_late_setgfx_bytes(
        source.read_bytes(), logical_path=logical_path or source.name)


def parse_sector_padded_setgfx_bytes(
        data: bytes, *, logical_path: str = "GFX/SETnGFX.BIN") \
        -> PsxNativeTexturePack:
    """Parse one strict December 1998 through May 1999 texture pack.

    Exactly 128 direct selector slots are required.  Populated records occupy
    0x2800 bytes and absent records occupy 0x800 bytes.  Every unused byte must
    be the observed 0xBA sector fill.  Only the three complete corpus sizes are
    accepted, preventing an unrelated sector file from being interpreted as
    this layout merely because it begins with a familiar marker.
    """

    if not isinstance(data, bytes):
        raise PsxNativeTextureError(
            "sector-padded SETnGFX source must be bytes")
    if len(data) not in SECTOR_PADDED_OBSERVED_SIZES:
        expected = ", ".join(
            f"{size} (0x{size:X})"
            for size in sorted(SECTOR_PADDED_OBSERVED_SIZES))
        raise PsxNativeTextureError(
            f"{logical_path}: sector-padded SETnGFX is {len(data)} bytes; "
            f"expected one of {expected}")

    slots: list[PsxNativeTextureSlot] = []
    pixel_banks: list[bytes] = [b""] * MATERIAL_SLOT_COUNT
    selector_pixel_banks: list[int | None] = []
    source_offset = 0
    full_padding_size = (
        SECTOR_PADDED_FULL_ALLOCATION - FULL_RECORD_SIZE)
    empty_padding_size = (
        SECTOR_PADDED_EMPTY_ALLOCATION
        - len(SECTOR_PADDED_EMPTY_MARKER))
    full_padding = bytes((SECTOR_PAD_BYTE,)) * full_padding_size
    empty_padding = bytes((SECTOR_PAD_BYTE,)) * empty_padding_size

    for selector in range(MATERIAL_SLOT_COUNT):
        if source_offset + len(SECTOR_PADDED_EMPTY_MARKER) > len(data):
            raise PsxNativeTextureError(
                f"{logical_path}: selector {selector} is truncated at "
                f"0x{source_offset:X}")
        prefix = data[
            source_offset:source_offset + RECORD_HEADER_SIZE]
        if prefix == ZERO_RECORD_HEADER:
            end = source_offset + SECTOR_PADDED_FULL_ALLOCATION
            if end > len(data):
                raise PsxNativeTextureError(
                    f"{logical_path}: populated selector {selector} is "
                    "truncated")
            payload = data[source_offset:source_offset + FULL_RECORD_SIZE]
            padding = data[source_offset + FULL_RECORD_SIZE:end]
            if padding != full_padding:
                raise PsxNativeTextureError(
                    f"{logical_path}: populated selector {selector} has "
                    "non-0xBA sector padding")
            palette_bytes = payload[
                RECORD_HEADER_SIZE:RECORD_HEADER_SIZE + CLUT_BYTES]
            palette_words = struct.unpack("<16H", palette_bytes)
            palette = tuple(decode_bgr555(word) for word in palette_words)
            packed = payload[RECORD_HEADER_SIZE + CLUT_BYTES:]
            # Direct-slot packs deliberately retain 128 bank positions so the
            # immutable mapping table can state S -> S without renumbering.
            pixel_banks[selector] = _unpack_low_nibble_first(packed)
            selector_pixel_banks.append(selector)
            slots.append(PsxNativeTextureSlot(
                selector=selector,
                source_offset=source_offset,
                header=prefix,
                palette_words=tuple(palette_words),
                palette=palette,
                available=True,
                allocation_size=SECTOR_PADDED_FULL_ALLOCATION,
            ))
            source_offset = end
            continue

        marker = data[
            source_offset:
            source_offset + len(SECTOR_PADDED_EMPTY_MARKER)]
        if marker != SECTOR_PADDED_EMPTY_MARKER:
            raise PsxNativeTextureError(
                f"{logical_path}: selector {selector} at "
                f"0x{source_offset:X} has invalid header {prefix.hex()}")
        end = source_offset + SECTOR_PADDED_EMPTY_ALLOCATION
        if end > len(data):
            raise PsxNativeTextureError(
                f"{logical_path}: empty selector {selector} is truncated")
        padding = data[
            source_offset + len(SECTOR_PADDED_EMPTY_MARKER):end]
        if padding != empty_padding:
            raise PsxNativeTextureError(
                f"{logical_path}: empty selector {selector} has non-0xBA "
                "sector padding")
        selector_pixel_banks.append(None)
        slots.append(PsxNativeTextureSlot(
            selector=selector,
            source_offset=source_offset,
            header=marker,
            palette_words=(),
            palette=(),
            available=False,
            allocation_size=SECTOR_PADDED_EMPTY_ALLOCATION,
        ))
        source_offset = end

    if source_offset != len(data):
        raise PsxNativeTextureError(
            f"{logical_path}: parser stopped at 0x{source_offset:X}, "
            f"source ends at 0x{len(data):X}")
    if len(slots) != MATERIAL_SLOT_COUNT:
        raise AssertionError("sector-padded SETnGFX slot count changed")
    if len(pixel_banks) != MATERIAL_SLOT_COUNT:
        raise AssertionError("sector-padded pixel-bank table changed")
    material_count = sum(slot.available for slot in slots)
    expected_count = dict(SECTOR_PADDED_OBSERVED_LAYOUTS)[len(data)]
    if material_count != expected_count:
        raise PsxNativeTextureError(
            f"{logical_path}: {len(data)}-byte sector-padded pack contains "
            f"{material_count} populated slots; expected {expected_count}")

    return PsxNativeTexturePack(
        logical_path=logical_path.replace("\\", "/"),
        source_sha256=hashlib.sha256(data).hexdigest(),
        slots=tuple(slots),
        pixel_banks=tuple(pixel_banks),
        layout_id=SECTOR_PADDED_SETGFX_LAYOUT_ID,
        selector_to_pixel_bank_mapping=(
            SECTOR_PADDED_SELECTOR_TO_PIXEL_BANK_MAPPING),
        selector_pixel_banks=tuple(selector_pixel_banks),
    )


def parse_sector_padded_setgfx_file(
        path: Path, *, logical_path: str | None = None) \
        -> PsxNativeTexturePack:
    """Read and parse one observed sector-padded pack from disk."""

    source = Path(path)
    return parse_sector_padded_setgfx_bytes(
        source.read_bytes(), logical_path=logical_path or source.name)
