from __future__ import annotations

from dataclasses import FrozenInstanceError
import hashlib
import os
from pathlib import Path
import struct
import unittest

from psx_native_textures import (
    FULL_RECORD_SIZE,
    LATE_SETGFX_LAYOUT_ID,
    LATE_SETGFX_SIZE,
    LATE_SELECTOR_TO_PIXEL_BANK_MAPPING,
    MATERIAL_SLOT_COUNT,
    PALETTE_RECORD_SIZE,
    PIXEL_BANK_COUNT,
    REPEAT_RECORD_HEADER,
    SECTOR_PAD_BYTE,
    SECTOR_PADDED_EMPTY_ALLOCATION,
    SECTOR_PADDED_EMPTY_MARKER,
    SECTOR_PADDED_FULL_ALLOCATION,
    SECTOR_PADDED_SELECTOR_TO_PIXEL_BANK_MAPPING,
    SECTOR_PADDED_SETGFX_LAYOUT_ID,
    TEXTURE_HEIGHT,
    TEXTURE_WIDTH,
    ZERO_RECORD_HEADER,
    PsxNativeTextureError,
    decode_bgr555,
    parse_late_setgfx_bytes,
    parse_late_setgfx_file,
    parse_sector_padded_setgfx_bytes,
    parse_sector_padded_setgfx_file,
)


def _synthetic_pack() -> bytes:
    records = []
    palettes: list[tuple[int, ...]] = []
    for selector in range(MATERIAL_SLOT_COUNT):
        palette = tuple(
            ((selector * 17 + entry * 0x421) & 0x7FFF)
            for entry in range(16))
        if selector == 1:
            palette = (0,) + palette[1:]
        if selector == 33:
            palette = (0x8000,) + palette[1:]
        header = ZERO_RECORD_HEADER
        if selector == 40:
            header = REPEAT_RECORD_HEADER
            palette = palettes[-1]
        palettes.append(palette)
        record = bytearray(header + struct.pack("<16H", *palette))
        if selector < PIXEL_BANK_COUNT:
            packed = bytearray([selector & 0xFF]) * 0x2000
            if selector == 1:
                packed[0] = 0xA3
            record.extend(packed)
            assert len(record) == FULL_RECORD_SIZE
        else:
            assert len(record) == PALETTE_RECORD_SIZE
        records.append(bytes(record))
    result = b"".join(records)
    assert len(result) == LATE_SETGFX_SIZE
    return result


_DECEMBER_POPULATED_SELECTORS = (
    0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15,
    16, 17, 18, 19, 20, 21, 22, 23, 24, 25, 26, 27, 28, 29,
    30, 31, 32, 33, 35, 36, 37, 38, 39, 41, 42, 44, 47, 48,
    49, 50, 51, 52, 53, 54, 56, 57, 58, 59, 60, 61, 62, 63,
    64, 69, 70, 71, 74, 80, 81, 83, 84, 86, 89, 92, 93, 94,
    95, 124, 125, 126, 127,
)

_MARCH_POPULATED_SELECTORS = (
    0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15,
    16, 17, 18, 19, 20, 21, 22, 23, 24, 25, 26, 27, 28, 29,
    30, 31, 32, 33, 35, 36, 37, 38, 39, 41, 42, 43, 44, 47,
    48, 49, 50, 51, 52, 53, 54, 56, 57, 58, 59, 60, 61, 62,
    63, 64, 69, 70, 71, 74, 80, 81, 83, 84, 86, 89, 92, 93,
    94, 95, 124, 125, 126, 127,
)

_MAY_POPULATED_SELECTORS = (
    0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15,
    16, 17, 18, 19, 20, 21, 22, 23, 24, 25, 26, 27, 28, 29,
    30, 31, 32, 33, 34, 35, 36, 37, 38, 39, 41, 42, 43, 44,
    47, 48, 49, 50, 51, 52, 53, 54, 56, 57, 58, 59, 60, 61,
    62, 63, 64, 66, 69, 70, 71, 74, 75, 80, 81, 83, 84, 86,
    89, 92, 93, 94, 95, 124, 125, 126, 127,
)


def _synthetic_sector_padded_pack() -> bytes:
    populated = set(_DECEMBER_POPULATED_SELECTORS)
    records = []
    for selector in range(MATERIAL_SLOT_COUNT):
        if selector not in populated:
            records.append(
                SECTOR_PADDED_EMPTY_MARKER
                + bytes((SECTOR_PAD_BYTE,))
                * (SECTOR_PADDED_EMPTY_ALLOCATION
                   - len(SECTOR_PADDED_EMPTY_MARKER)))
            continue
        palette = tuple(
            ((selector * 31 + entry * 0x421) & 0x7FFF)
            for entry in range(16))
        packed = bytearray((selector & 0xFF,)) * 0x2000
        if selector == 33:
            packed[0] = 0xA3
        payload = (
            ZERO_RECORD_HEADER + struct.pack("<16H", *palette) + packed)
        records.append(
            payload
            + bytes((SECTOR_PAD_BYTE,))
            * (SECTOR_PADDED_FULL_ALLOCATION - len(payload)))
    result = b"".join(records)
    assert len(result) == 0xDA000
    return result


def _canonical_gfx_root(corpus: Path, build: str) -> Path | None:
    candidates = (
        corpus / "technical" / "analysis" / build
        / "work" / "disc_files" / "GFX",
        corpus / "analysis" / build / "work" / "disc_files" / "GFX",
        corpus / build / "work" / "disc_files" / "GFX",
        corpus / "work" / "disc_files" / "GFX",
        corpus / "GFX",
    )
    return next((path for path in candidates if path.is_dir()), None)


class PsxNativeTextureTests(unittest.TestCase):
    def test_exact_layout_and_low_nibble_first_indexed_pixels(self):
        pack = parse_late_setgfx_bytes(
            _synthetic_pack(), logical_path="GFX/SET1GFX.BIN")

        self.assertEqual(pack.layout_id, LATE_SETGFX_LAYOUT_ID)
        self.assertEqual(
            pack.selector_to_pixel_bank_mapping,
            LATE_SELECTOR_TO_PIXEL_BANK_MAPPING)
        self.assertEqual(pack.selector_pixel_banks[33], 1)
        self.assertEqual(len(pack.slots), 128)
        self.assertEqual(len(pack.pixel_banks), 32)
        self.assertEqual(len(pack.pixel_banks[0]), 128 * 128)
        self.assertEqual(pack.pixel_banks[1][:4], bytes((3, 10, 1, 0)))
        self.assertEqual(pack.slots[40].header, REPEAT_RECORD_HEADER)
        self.assertEqual(
            pack.slots[40].palette_words,
            pack.slots[39].palette_words)

    def test_selector_uses_its_clut_and_low_five_bit_pixel_bank(self):
        pack = parse_late_setgfx_bytes(_synthetic_pack())
        material = pack.material(33)

        self.assertEqual(material.selector, 33)
        self.assertEqual(material.pixel_bank, 1)
        self.assertEqual((material.width, material.height), (128, 128))
        self.assertIs(material.indices, pack.pixel_banks[1])
        self.assertEqual(material.indices[:2], bytes((3, 10)))
        self.assertEqual(material.palette_words, pack.slots[33].palette_words)
        self.assertEqual(material.palette_words[0], 0x8000)
        self.assertTrue(material.palette[0].stp)

    def test_rgba_keys_resolved_zero_word_not_palette_index_zero(self):
        pack = parse_late_setgfx_bytes(_synthetic_pack())
        transparent = pack.material(1)
        nonzero_black_stp = pack.material(33)

        # Bank 1's third pixel uses palette index 1, while a fresh all-zero
        # location uses palette index 0.  Slot 1 resolves index zero to word
        # 0x0000 and therefore transparent.
        self.assertEqual(transparent.indices[3], 0)
        self.assertEqual(transparent.rgba[3 * 4 + 3], 0)
        # Selector 33 uses the same indexed bank, but CLUT[0] is 0x8000.
        # It is not keyed out merely because its palette index is zero.
        self.assertEqual(nonzero_black_stp.indices[3], 0)
        self.assertEqual(nonzero_black_stp.rgba[3 * 4:3 * 4 + 4],
                         bytes((0, 0, 0, 255)))

    def test_bgr555_expands_channels_and_retains_stp(self):
        color = decode_bgr555(0xFC1F)

        self.assertEqual(color.word, 0xFC1F)
        self.assertEqual((color.red, color.green, color.blue),
                         (255, 0, 255))
        self.assertTrue(color.stp)
        self.assertEqual(color.rgba, (255, 0, 255, 255))
        self.assertEqual(decode_bgr555(0).rgba, (0, 0, 0, 0))

    def test_public_records_are_immutable(self):
        pack = parse_late_setgfx_bytes(_synthetic_pack())
        material = pack.material(1)

        with self.assertRaises(FrozenInstanceError):
            pack.logical_path = "changed"  # type: ignore[misc]
        with self.assertRaises(FrozenInstanceError):
            material.selector = 2  # type: ignore[misc]
        with self.assertRaises(FrozenInstanceError):
            pack.layout_id = "changed"  # type: ignore[misc]
        self.assertIsInstance(material.indices, bytes)
        self.assertIsInstance(material.rgba, bytes)
        self.assertIsInstance(material.palette, tuple)
        self.assertIsInstance(pack.selector_pixel_banks, tuple)

    def test_wrong_size_header_repeat_clut_and_selector_fail_closed(self):
        source = _synthetic_pack()
        with self.assertRaisesRegex(PsxNativeTextureError, "expected 267264"):
            parse_late_setgfx_bytes(source[:-1])

        bad_header = bytearray(source)
        bad_header[0] = 1
        with self.assertRaisesRegex(PsxNativeTextureError, "invalid header"):
            parse_late_setgfx_bytes(bytes(bad_header))

        # Slot 40 begins after 32 full records and eight palette records.
        slot_40 = PIXEL_BANK_COUNT * FULL_RECORD_SIZE \
            + (40 - PIXEL_BANK_COUNT) * PALETTE_RECORD_SIZE
        bad_repeat = bytearray(source)
        bad_repeat[slot_40 + 8] ^= 1
        with self.assertRaisesRegex(
                PsxNativeTextureError, "does not repeat"):
            parse_late_setgfx_bytes(bytes(bad_repeat))

        pack = parse_late_setgfx_bytes(source)
        for selector in (-1, 128, True, 1.0):
            with self.subTest(selector=selector):
                with self.assertRaises(PsxNativeTextureError):
                    pack.material(selector)  # type: ignore[arg-type]

    def test_sector_padded_layout_uses_direct_selector_slots(self):
        source = _synthetic_sector_padded_pack()
        pack = parse_sector_padded_setgfx_bytes(
            source, logical_path="GFX/SET1GFX.BIN")

        self.assertEqual(pack.layout_id, SECTOR_PADDED_SETGFX_LAYOUT_ID)
        self.assertEqual(
            pack.selector_to_pixel_bank_mapping,
            SECTOR_PADDED_SELECTOR_TO_PIXEL_BANK_MAPPING)
        self.assertEqual(pack.material_slot_count, 77)
        self.assertEqual(pack.populated_selectors,
                         _DECEMBER_POPULATED_SELECTORS)
        self.assertEqual(len(pack.slots), MATERIAL_SLOT_COUNT)
        self.assertEqual(len(pack.pixel_banks), MATERIAL_SLOT_COUNT)
        self.assertEqual(pack.selector_pixel_banks[33], 33)
        self.assertIsNone(pack.selector_pixel_banks[34])

        material = pack.material(33)
        self.assertEqual(material.pixel_bank, 33)
        self.assertEqual(material.indices[:2], bytes((3, 10)))
        self.assertNotEqual(pack.pixel_banks[33], pack.pixel_banks[1])
        self.assertEqual((material.width, material.height), (128, 128))
        self.assertTrue(pack.has_material(33))
        self.assertFalse(pack.has_material(34))
        empty = pack.slot(34)
        self.assertFalse(empty.available)
        self.assertEqual(empty.header, SECTOR_PADDED_EMPTY_MARKER)
        self.assertEqual(empty.palette_words, ())
        self.assertEqual(empty.allocation_size,
                         SECTOR_PADDED_EMPTY_ALLOCATION)
        with self.assertRaisesRegex(PsxNativeTextureError, "empty"):
            pack.material(34)

    def test_sector_padded_size_header_and_padding_fail_closed(self):
        source = _synthetic_sector_padded_pack()

        with self.assertRaisesRegex(PsxNativeTextureError, "expected one of"):
            parse_sector_padded_setgfx_bytes(source[:-1])
        with self.assertRaisesRegex(PsxNativeTextureError, "must be bytes"):
            parse_sector_padded_setgfx_bytes(  # type: ignore[arg-type]
                bytearray(source))

        bad_header = bytearray(source)
        bad_header[0] = 1
        with self.assertRaisesRegex(PsxNativeTextureError, "invalid header"):
            parse_sector_padded_setgfx_bytes(bytes(bad_header))

        bad_full_padding = bytearray(source)
        bad_full_padding[FULL_RECORD_SIZE] = 0
        with self.assertRaisesRegex(PsxNativeTextureError, "non-0xBA"):
            parse_sector_padded_setgfx_bytes(bytes(bad_full_padding))

        # Selectors 0..33 are populated in the December profile, so selector
        # 34's empty sector begins after 34 five-sector allocations.
        empty_offset = 34 * SECTOR_PADDED_FULL_ALLOCATION
        self.assertEqual(
            source[empty_offset:empty_offset + 4],
            SECTOR_PADDED_EMPTY_MARKER)
        bad_empty_marker = bytearray(source)
        bad_empty_marker[empty_offset] ^= 1
        with self.assertRaisesRegex(PsxNativeTextureError, "invalid header"):
            parse_sector_padded_setgfx_bytes(bytes(bad_empty_marker))

        bad_empty_padding = bytearray(source)
        bad_empty_padding[empty_offset + 4] = 0
        with self.assertRaisesRegex(PsxNativeTextureError, "non-0xBA"):
            parse_sector_padded_setgfx_bytes(bytes(bad_empty_padding))

    def test_sector_padded_file_api_preserves_portable_logical_path(self):
        import tempfile

        payload = _synthetic_sector_padded_pack()
        expected_hash = hashlib.sha256(payload).hexdigest()
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "SET1GFX.BIN"
            source.write_bytes(payload)
            pack = parse_sector_padded_setgfx_file(
                source, logical_path=r"GFX\SET1GFX.BIN")

        self.assertEqual(pack.logical_path, "GFX/SET1GFX.BIN")
        self.assertEqual(pack.source_sha256, expected_hash)

    @unittest.skipUnless(
        os.environ.get("OPENUA_PSX_CORPUS_ROOT"),
        "set OPENUA_PSX_CORPUS_ROOT for recovered prototype checks")
    def test_canonical_late_set_packs(self):
        corpus = Path(os.environ["OPENUA_PSX_CORPUS_ROOT"])
        candidates = (
            corpus / "technical" / "analysis" / "1999-06-15"
            / "work" / "disc_files" / "GFX",
            corpus / "1999-06-15" / "work" / "disc_files" / "GFX",
            corpus / "work" / "disc_files" / "GFX",
            corpus / "GFX",
        )
        gfx_root = next((path for path in candidates if path.is_dir()), None)
        self.assertIsNotNone(
            gfx_root, "OPENUA_PSX_CORPUS_ROOT has no late-build GFX tree")
        expected = {
            "SET1GFX.BIN":
                "d2eb2f43f8db085c89b4a29f39d3ade840dfb7974333591490fc59fcf6ef60a9",
            "SET2GFX.BIN":
                "430031edeff623cdaeca9c8b4e5c88c67440cefc7c0d87d58174e4701e5dd23b",
            "SET3GFX.BIN":
                "a460a71b5a7b7cdf6ab6e404b6ac3f9cd324abe79d8e8a12bdaacd864eff98a8",
            "SET4GFX.BIN":
                "c150bcbe1f51dad0fad2a56447b0d22288474c08b5c10f2530ac802fd5267268",
            "SET5GFX.BIN":
                "ab704d8fc7598cbc4b4d0bfb0684b29b7eb2bf7d199679fa86b0343ef44a0dd3",
            "SET6GFX.BIN":
                "781053bf221ce7e793d64ce95c99eb12413c8dcf282066b31258bf27a6be425d",
        }
        assert gfx_root is not None
        for filename, sha256 in expected.items():
            with self.subTest(filename=filename):
                source = gfx_root / filename
                self.assertEqual(
                    hashlib.sha256(source.read_bytes()).hexdigest(), sha256)
                pack = parse_late_setgfx_file(
                    source, logical_path=f"GFX/{filename}")
                self.assertEqual(len(pack.slots), MATERIAL_SLOT_COUNT)
                self.assertEqual(len(pack.pixel_banks), PIXEL_BANK_COUNT)
                self.assertEqual(
                    (pack.material(124).width, pack.material(124).height),
                    (TEXTURE_WIDTH, TEXTURE_HEIGHT))

    @unittest.skipUnless(
        os.environ.get("OPENUA_PSX_CORPUS_ROOT"),
        "set OPENUA_PSX_CORPUS_ROOT for recovered prototype checks")
    def test_canonical_sector_padded_set_packs(self):
        corpus = Path(os.environ["OPENUA_PSX_CORPUS_ROOT"])
        expected = {
            "1998-12-18": {
                "size": 0xDA000,
                "selectors": _DECEMBER_POPULATED_SELECTORS,
                "files": {
                    "SET1GFX.BIN": (
                        "a3c4fa5ae22ac08e94829c2e3b8629b23b078ba7d9d595"
                        "c4b5af64b1e68ded07"),
                },
            },
            "1999-03-12": {
                "size": 0xDC000,
                "selectors": _MARCH_POPULATED_SELECTORS,
                "files": {
                    "SET1GFX.BIN": (
                        "626f1905508b2dd19b2fe101bd654733f641c5177bfb899"
                        "4a06d365ac4f70284"),
                    "SET2GFX.BIN": (
                        "f1ee8dfd262ce2a4bc0b812194f5102432fcaff296fddb1"
                        "33122c83b1ebd554d"),
                    "SET3GFX.BIN": (
                        "4454c33f6d09e9cef052b55161ea099247cb9eb176a86b2"
                        "e8c71e817791f6245"),
                    "SET4GFX.BIN": (
                        "8383468a30ad28e8138b08a5c2cdbf0a46c9a1bc28ab84"
                        "6c7ff02e8fc75f56be"),
                    "SET5GFX.BIN": (
                        "251654dcf095a1f0bebcd5d87a5f9e6a3bc174b0c1eff9"
                        "3321ef0e5a3a975326"),
                    "SET6GFX.BIN": (
                        "d67f59bca06e77808f9cd0e75b503e0ad11f7a5995e2eb"
                        "02da5342790b6b7fbc"),
                },
            },
            "1999-05-14": {
                "size": 0xE2000,
                "selectors": _MAY_POPULATED_SELECTORS,
                "files": {
                    "SET1GFX.BIN": (
                        "e096c6db1b810894289fa7292ae8b403e140f41e7314bdc"
                        "67126b7037256dc35"),
                    "SET2GFX.BIN": (
                        "74ec97e220db4f28e820fc0963a66555db8de7169c84b32"
                        "11652b8253094f133"),
                    "SET3GFX.BIN": (
                        "7162e9bde4b2b8835a7a968ea279d166179d922ffb8e1b6"
                        "17061e30b64ed9506"),
                    "SET4GFX.BIN": (
                        "def61dbab73e666fddc233129eccce8699a301ed68bac5e8"
                        "2e0258eb666f31d6"),
                    "SET5GFX.BIN": (
                        "afb756878ede6018e883e0efb35ed8d188980d345c7838a"
                        "5ab7e05c15add935b"),
                    "SET6GFX.BIN": (
                        "e5bdbd82cdcef0bce2b128ed5814f5605c9e8f2c92ed90"
                        "d8ca8038e9797b0ee6"),
                },
            },
        }
        for build, profile in expected.items():
            gfx_root = _canonical_gfx_root(corpus, build)
            self.assertIsNotNone(
                gfx_root,
                f"OPENUA_PSX_CORPUS_ROOT has no {build} GFX tree")
            assert gfx_root is not None
            for filename, expected_hash in profile["files"].items():
                with self.subTest(build=build, filename=filename):
                    source = gfx_root / filename
                    data = source.read_bytes()
                    self.assertEqual(len(data), profile["size"])
                    self.assertEqual(
                        hashlib.sha256(data).hexdigest(), expected_hash)
                    pack = parse_sector_padded_setgfx_file(
                        source, logical_path=f"GFX/{filename}")
                    self.assertEqual(
                        pack.layout_id,
                        SECTOR_PADDED_SETGFX_LAYOUT_ID)
                    self.assertEqual(
                        pack.selector_to_pixel_bank_mapping,
                        SECTOR_PADDED_SELECTOR_TO_PIXEL_BANK_MAPPING)
                    self.assertEqual(
                        pack.populated_selectors, profile["selectors"])
                    self.assertEqual(
                        pack.material_slot_count,
                        len(profile["selectors"]))
                    self.assertEqual(
                        pack.selector_pixel_banks[124], 124)
                    self.assertNotEqual(
                        pack.pixel_banks[33], pack.pixel_banks[1])
                    material = pack.material(124)
                    self.assertEqual(material.pixel_bank, 124)
                    self.assertEqual(
                        (material.width, material.height),
                        (TEXTURE_WIDTH, TEXTURE_HEIGHT))


if __name__ == "__main__":
    unittest.main()
