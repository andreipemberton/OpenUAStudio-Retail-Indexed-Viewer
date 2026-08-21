from __future__ import annotations

from dataclasses import FrozenInstanceError
import hashlib
import os
from pathlib import Path
import unittest

from psx_gpu_colors import (
    PSX_GT4_OPAQUE_COMMAND,
    PSX_GT4_SEMITRANSPARENT_COMMAND,
    PSX_GPU_COLOR_PROFILE_ID,
    PSX_VERTEX_COLOR_NEUTRAL,
    PW3_PACKET_RAW_CORNER_ORDER,
    UA_DIRECT_SHADE_FILE_RANGE,
    UA_DIRECT_SHADE_RANGE_SHA256,
    UA_JUNE_OVER1_SHA256,
    UA_PW3_SHADE_ROUTINE_DISPATCH_STATUS,
    UA_SELECTOR_TO_DESCRIPTOR_TPAGE_STATUS,
    UA_TINTED_SHADE_FILE_RANGE,
    UA_TINTED_SHADE_RANGE_SHA256,
    UA_TPAGE_DISPATCH_FILE_RANGE,
    UA_TPAGE_DISPATCH_RANGE_SHA256,
    PsxAbrMode,
    PsxGpuColorError,
    PsxPw3Gt4State,
    psx_abr_blend_channel_5,
    psx_abr_blend_channel_8,
    psx_texture_modulate_bgr555,
    psx_texture_modulate_channel,
    pw3_direct_packet_vertex_rgb,
    pw3_gt4_state_from_descriptor_tpage,
    pw3_packet_corner_shades,
    pw3_tinted_packet_vertex_rgb,
    shade_pw3_textured_fragment,
)
from psx_native_textures import (
    LATE_SETGFX_SIZE,
    parse_late_setgfx_file,
    parse_sector_padded_setgfx_file,
)


def _canonical_overlay(corpus: Path) -> Path | None:
    candidates = (
        corpus / "technical" / "analysis" / "1999-06-15"
        / "work" / "disc_files" / "OVER1.TXT",
        corpus / "analysis" / "1999-06-15"
        / "work" / "disc_files" / "OVER1.TXT",
        corpus / "1999-06-15" / "work" / "disc_files" / "OVER1.TXT",
        corpus / "work" / "disc_files" / "OVER1.TXT",
        corpus / "OVER1.TXT",
    )
    return next((path for path in candidates if path.is_file()), None)


class PsxGpuColorTests(unittest.TestCase):
    def test_profile_and_packet_shade_order_are_explicit(self):
        self.assertEqual(PSX_GPU_COLOR_PROFILE_ID, "psx_gpu_color_math_v1")
        self.assertEqual(PW3_PACKET_RAW_CORNER_ORDER, (1, 0, 2, 3))
        self.assertEqual(
            pw3_packet_corner_shades((10, 20, 30, 40)),
            (20, 10, 30, 40))
        self.assertEqual(
            pw3_direct_packet_vertex_rgb((10, 20, 30, 40)),
            ((20, 20, 20), (10, 10, 10),
             (30, 30, 30), (40, 40, 40)))

    def test_tinted_vertex_colors_use_truncating_divide_by_256(self):
        self.assertEqual(
            pw3_tinted_packet_vertex_rgb(
                (10, 20, 30, 40), (255, 128, 1)),
            ((19, 10, 0), (9, 5, 0),
             (29, 15, 0), (39, 20, 0)))
        # The recovered routine never maps 255*255 back to 255.
        self.assertEqual(
            pw3_tinted_packet_vertex_rgb(
                (255, 255, 255, 255), (255, 255, 255))[0],
            (254, 254, 254))

    def test_texture_modulation_uses_neutral_128_and_stays_wide(self):
        self.assertEqual(PSX_VERTEX_COLOR_NEUTRAL, 128)
        for texel in range(32):
            with self.subTest(texel=texel):
                self.assertEqual(
                    psx_texture_modulate_channel(texel, 128), texel << 3)
        self.assertEqual(psx_texture_modulate_channel(17, 73), 77)
        # Over-bright modulation is not clamped before ABR.
        self.assertEqual(psx_texture_modulate_channel(31, 255), 494)

        word = 17 | (3 << 5) | (31 << 10) | 0x8000
        self.assertEqual(
            psx_texture_modulate_bgr555(word, (73, 128, 255)),
            (77, 24, 494))

    def test_ua_tpage_dispatch_keeps_abr_zero_opaque(self):
        expected = (
            (0x0000, PsxAbrMode.HALF_BACK_PLUS_HALF_FRONT,
             False, PSX_GT4_OPAQUE_COMMAND),
            (0x0020, PsxAbrMode.BACK_PLUS_FRONT,
             True, PSX_GT4_SEMITRANSPARENT_COMMAND),
            (0x0040, PsxAbrMode.BACK_MINUS_FRONT,
             True, PSX_GT4_SEMITRANSPARENT_COMMAND),
            (0x0060, PsxAbrMode.BACK_PLUS_QUARTER_FRONT,
             True, PSX_GT4_SEMITRANSPARENT_COMMAND),
        )
        for tpage, abr, semitransparent, command in expected:
            with self.subTest(tpage=tpage):
                state = pw3_gt4_state_from_descriptor_tpage(tpage)
                self.assertEqual(state.descriptor_tpage, tpage)
                self.assertIs(state.abr_mode, abr)
                self.assertIs(
                    state.primitive_semitransparent, semitransparent)
                self.assertEqual(state.command_code, command)

        # Unrelated TPage bits do not turn UA's ABR-zero path translucent.
        state = pw3_gt4_state_from_descriptor_tpage(0x019F)
        self.assertIs(
            state.abr_mode, PsxAbrMode.HALF_BACK_PLUS_HALF_FRONT)
        self.assertFalse(state.primitive_semitransparent)
        self.assertEqual(state.command_code, PSX_GT4_OPAQUE_COMMAND)

    def test_selector_to_runtime_tpage_is_a_hard_gate(self):
        self.assertEqual(
            UA_SELECTOR_TO_DESCRIPTOR_TPAGE_STATUS,
            "unresolved_hard_gate")
        with self.assertRaisesRegex(
                PsxGpuColorError,
                "selector-to-descriptor TPage/ABR binding is unresolved"):
            pw3_gt4_state_from_descriptor_tpage(None)

    def test_effective_pw3_shade_routine_is_also_a_hard_gate(self):
        self.assertEqual(
            UA_PW3_SHADE_ROUTINE_DISPATCH_STATUS,
            "unresolved_hard_gate")

    def test_all_hardware_abr_channel_equations_and_saturation(self):
        expected_5bit = {
            PsxAbrMode.HALF_BACK_PLUS_HALF_FRONT: (
                (0, 8, 15), (8, 16, 23), (15, 23, 31)),
            PsxAbrMode.BACK_PLUS_FRONT: (
                (0, 16, 31), (16, 31, 31), (31, 31, 31)),
            PsxAbrMode.BACK_MINUS_FRONT: (
                (0, 0, 0), (16, 0, 0), (31, 15, 0)),
            PsxAbrMode.BACK_PLUS_QUARTER_FRONT: (
                (0, 4, 7), (16, 20, 23), (31, 31, 31)),
        }
        values = (0, 16, 31)
        for mode, rows in expected_5bit.items():
            for background_index, background5 in enumerate(values):
                for foreground_index, foreground5 in enumerate(values):
                    with self.subTest(
                            mode=mode, background=background5,
                            foreground=foreground5):
                        foreground8 = foreground5 << 3
                        self.assertEqual(
                            psx_abr_blend_channel_5(
                                background5, foreground8, mode),
                            rows[background_index][foreground_index])

        # Exercise the exact pre-quantization equations with non-boundary
        # foreground values, including subtractive floor and saturation.
        self.assertEqual(psx_abr_blend_channel_8(10, 77, 0), 78)
        self.assertEqual(psx_abr_blend_channel_8(31, 255, 1), 255)
        self.assertEqual(psx_abr_blend_channel_8(4, 99, 2), 0)
        self.assertEqual(psx_abr_blend_channel_8(31, 31, 3), 255)

        # Preserve the wide modulation intermediate until after ABR.  These
        # results distinguish the exact order from a premature byte clamp.
        overbright = psx_texture_modulate_channel(31, 255)
        self.assertEqual(overbright, 494)
        self.assertEqual(psx_abr_blend_channel_8(0, overbright, 0), 247)
        self.assertEqual(psx_abr_blend_channel_8(0, overbright, 1), 255)
        self.assertEqual(psx_abr_blend_channel_8(31, overbright, 2), 0)
        self.assertEqual(psx_abr_blend_channel_8(0, overbright, 3), 123)

    def test_zero_word_skips_write_and_preserves_background_exactly(self):
        state = pw3_gt4_state_from_descriptor_tpage(0x20)
        fragment = shade_pw3_textured_fragment(
            0x0000, (128, 128, 128), 0x9234, state)

        self.assertFalse(fragment.wrote)
        self.assertTrue(fragment.zero_word_discarded)
        self.assertFalse(fragment.source_stp)
        self.assertFalse(fragment.blend_applied)
        self.assertEqual(fragment.modulated_rgb8, (0, 0, 0))
        self.assertEqual(fragment.output_word, 0x9234)

    def test_textured_semitransparency_is_conditioned_on_source_stp(self):
        state = pw3_gt4_state_from_descriptor_tpage(0x20)
        source_clear = 0x0364  # R5=4, G5=27, B5=0, STP clear.
        source_set = source_clear | 0x8000
        background = 0x0010  # R5=16.

        opaque_texel = shade_pw3_textured_fragment(
            source_clear, (128, 128, 128), background, state)
        self.assertTrue(opaque_texel.wrote)
        self.assertFalse(opaque_texel.source_stp)
        self.assertFalse(opaque_texel.blend_applied)
        self.assertEqual(opaque_texel.output_word, source_clear)

        blended_texel = shade_pw3_textured_fragment(
            source_set, (128, 128, 128), background, state)
        self.assertTrue(blended_texel.wrote)
        self.assertTrue(blended_texel.source_stp)
        self.assertTrue(blended_texel.blend_applied)
        # Archived phase-12 hardware fixture ABR1_TEX_MASKED.
        self.assertEqual(blended_texel.output_word, 0x8374)

    def test_abr2_abr3_and_stp_propagation_match_hardware_fixtures(self):
        source = 0x8364  # R5=4, G5=27, B5=0, STP set.
        background = 0x0010
        expected = {
            0x40: 0x800C,  # ABR2_TEX_MASKED
            0x60: 0x80D1,  # ABR3_TEX_MASKED
        }
        for tpage, output in expected.items():
            with self.subTest(tpage=tpage):
                fragment = shade_pw3_textured_fragment(
                    source, (128, 128, 128), background,
                    pw3_gt4_state_from_descriptor_tpage(tpage))
                self.assertTrue(fragment.blend_applied)
                self.assertEqual(fragment.output_word, output)
                self.assertTrue(fragment.output_word & 0x8000)

        # Hardware ABR0_TEX_MASKED is still independently represented by
        # the generic equation even though UA's observed GT4 dispatch does
        # not emit command 0x3E for ABR field zero.
        red = psx_abr_blend_channel_5(16, 4 << 3, 0)
        green = psx_abr_blend_channel_5(0, 27 << 3, 0)
        self.assertEqual(0x8000 | red | (green << 5), 0x81AA)

    def test_opaque_gt4_does_not_blend_stp_texel_but_retains_mask(self):
        state = pw3_gt4_state_from_descriptor_tpage(0x0000)
        fragment = shade_pw3_textured_fragment(
            0x8364, (128, 128, 128), 0x0010, state)

        self.assertTrue(fragment.wrote)
        self.assertTrue(fragment.source_stp)
        self.assertFalse(fragment.blend_applied)
        self.assertEqual(fragment.output_word, 0x8364)

        # 0x8000 is nonzero black, not the transparent zero word.  In UA's
        # additive mode it performs a blend with black and propagates STP.
        black = shade_pw3_textured_fragment(
            0x8000, (128, 128, 128), 0x0010,
            pw3_gt4_state_from_descriptor_tpage(0x20))
        self.assertTrue(black.wrote)
        self.assertTrue(black.blend_applied)
        self.assertEqual(black.output_word, 0x8010)

        # Opaque output saturates only after wide texture modulation.
        overbright = shade_pw3_textured_fragment(
            0x7FFF, (255, 255, 255), 0,
            pw3_gt4_state_from_descriptor_tpage(0x0000))
        self.assertEqual(overbright.modulated_rgb8, (494, 494, 494))
        self.assertEqual(overbright.output_word, 0x7FFF)

    def test_records_are_immutable_and_inconsistent_state_fails_closed(self):
        state = pw3_gt4_state_from_descriptor_tpage(0x20)
        fragment = shade_pw3_textured_fragment(
            0x8001, (128, 128, 128), 0, state)
        with self.assertRaises(FrozenInstanceError):
            state.descriptor_tpage = 0  # type: ignore[misc]
        with self.assertRaises(FrozenInstanceError):
            fragment.output_word = 0  # type: ignore[misc]

        with self.assertRaisesRegex(PsxGpuColorError, "ABR mode"):
            PsxPw3Gt4State(
                descriptor_tpage=0x20,
                abr_mode=PsxAbrMode.BACK_MINUS_FRONT,
                primitive_semitransparent=True,
                command_code=PSX_GT4_SEMITRANSPARENT_COMMAND)
        with self.assertRaisesRegex(PsxGpuColorError, "command code"):
            PsxPw3Gt4State(
                descriptor_tpage=0x20,
                abr_mode=PsxAbrMode.BACK_PLUS_FRONT,
                primitive_semitransparent=True,
                command_code=PSX_GT4_OPAQUE_COMMAND)

    def test_invalid_color_inputs_fail_closed(self):
        invalid_calls = (
            lambda: pw3_packet_corner_shades((1, 2, 3)),
            lambda: pw3_packet_corner_shades((1, 2, 3, True)),
            lambda: pw3_tinted_packet_vertex_rgb(
                (1, 2, 3, 4), (1, 2)),
            lambda: psx_texture_modulate_channel(32, 128),
            lambda: psx_texture_modulate_channel(1, 256),
            lambda: psx_texture_modulate_bgr555(
                0x10000, (128, 128, 128)),
            lambda: psx_abr_blend_channel_8(0, 0, 4),
            lambda: pw3_gt4_state_from_descriptor_tpage(True),
            lambda: shade_pw3_textured_fragment(
                1, (128, 128, 128), 0, object()),
        )
        for call in invalid_calls:
            with self.subTest(call=call):
                with self.assertRaises(PsxGpuColorError):
                    call()

    @unittest.skipUnless(
        os.environ.get("OPENUA_PSX_CORPUS_ROOT"),
        "set OPENUA_PSX_CORPUS_ROOT for recovered overlay checks")
    def test_canonical_june_overlay_color_routine_anchors(self):
        corpus = Path(os.environ["OPENUA_PSX_CORPUS_ROOT"])
        overlay = _canonical_overlay(corpus)
        self.assertIsNotNone(
            overlay,
            "OPENUA_PSX_CORPUS_ROOT has no 1999-06-15 OVER1.TXT")
        assert overlay is not None
        data = overlay.read_bytes()
        self.assertEqual(hashlib.sha256(data).hexdigest(),
                         UA_JUNE_OVER1_SHA256)

        for offsets, expected_hash in (
                (UA_DIRECT_SHADE_FILE_RANGE,
                 UA_DIRECT_SHADE_RANGE_SHA256),
                (UA_TINTED_SHADE_FILE_RANGE,
                 UA_TINTED_SHADE_RANGE_SHA256),
                (UA_TPAGE_DISPATCH_FILE_RANGE,
                 UA_TPAGE_DISPATCH_RANGE_SHA256)):
            with self.subTest(offsets=offsets):
                start, end = offsets
                self.assertEqual(
                    hashlib.sha256(data[start:end]).hexdigest(),
                    expected_hash)

    @unittest.skipUnless(
        os.environ.get("OPENUA_PSX_CORPUS_ROOT"),
        "set OPENUA_PSX_CORPUS_ROOT for recovered STP checks")
    def test_canonical_setgfx_stp_words_are_nonzero_black(self):
        """Lock the corpus fact without inferring a material's TPage.

        Every STP-set CLUT entry in the 19 canonical SET packs is exactly
        0x8000.  That word is not the transparent zero word.  It becomes a
        no-op foreground under UA's evidenced ABR1/2/3 paths, but would draw
        opaque black under command 0x3C, which is why STP presence alone is
        not accepted as a selector-to-semitransparency binding.
        """

        corpus = Path(os.environ["OPENUA_PSX_CORPUS_ROOT"])
        expected = {
            "1998-12-18": (1, 63),
            "1999-03-12": (6, 337),
            "1999-05-14": (6, 355),
            "1999-06-15": (6, 921),
        }
        for build, (pack_count, stp_count) in expected.items():
            candidates = (
                corpus / "technical" / "analysis" / build
                / "work" / "disc_files" / "GFX",
                corpus / "analysis" / build
                / "work" / "disc_files" / "GFX",
                corpus / build / "work" / "disc_files" / "GFX",
            )
            gfx = next((path for path in candidates if path.is_dir()), None)
            self.assertIsNotNone(gfx, f"no canonical {build} GFX tree")
            assert gfx is not None
            sources = tuple(sorted(
                gfx.glob("SET?GFX.BIN"), key=lambda path: path.name))
            self.assertEqual(len(sources), pack_count)
            stp_words = []
            for source in sources:
                pack = (
                    parse_late_setgfx_file(source)
                    if source.stat().st_size == LATE_SETGFX_SIZE
                    else parse_sector_padded_setgfx_file(source))
                stp_words.extend(
                    word
                    for slot in pack.slots if slot.available
                    for word in slot.palette_words
                    if word & 0x8000)
            with self.subTest(build=build):
                self.assertEqual(len(stp_words), stp_count)
                self.assertEqual(set(stp_words), {0x8000})


if __name__ == "__main__":
    unittest.main()
