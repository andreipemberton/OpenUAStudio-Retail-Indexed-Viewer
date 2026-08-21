from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
import math
import unittest
from unittest import mock

import psx_native_raster
from psx_gpu_colors import psx_texture_modulate_bgr555
from psx_native_raster import (
    MAX_PATCH_PIXELS,
    MAX_SCREEN_COORDINATE_ABS,
    MAX_TARGET_DIMENSION,
    PSX_LEGACY_PSW_RASTER_PROFILE_ID,
    PSX_NATIVE_RASTER_ACCELERATION_BACKEND,
    PsxNativeRasterError,
    PsxNativeRasterPatch,
    rasterize_legacy_psw_triangle,
)
from psx_native_textures import (
    TEXTURE_HEIGHT,
    TEXTURE_WIDTH,
    PsxNativeMaterial,
    decode_bgr555,
)


def _material(
        palette_words: tuple[int, ...], *, indices: bytes | None = None) \
        -> PsxNativeMaterial:
    words = palette_words + (0,) * (16 - len(palette_words))
    pixels = indices if indices is not None else bytes(
        (1,)) * (TEXTURE_WIDTH * TEXTURE_HEIGHT)
    rgba = bytearray(TEXTURE_WIDTH * TEXTURE_HEIGHT * 4)
    palette = tuple(decode_bgr555(word) for word in words)
    for pixel, palette_index in enumerate(pixels):
        if palette_index < len(palette):
            rgba[pixel * 4:pixel * 4 + 4] = bytes(
                palette[palette_index].rgba)
    return PsxNativeMaterial(
        selector=0,
        pixel_bank=0,
        width=TEXTURE_WIDTH,
        height=TEXTURE_HEIGHT,
        indices=pixels,
        palette_words=words,
        palette=palette,
        rgba=bytes(rgba),
    )


def _covered_target_pixels(patch: PsxNativeRasterPatch) -> set[tuple[int, int]]:
    result = set()
    for y in range(patch.height):
        for x in range(patch.width):
            offset = (y * patch.width + x) * 4
            if patch.rgba[offset + 3]:
                result.add((patch.origin_x + x, patch.origin_y + y))
    return result


class PsxNativeRasterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.triangle = ((0.0, 0.0), (4.0, 0.0), (0.0, 4.0))

    def test_profile_patch_contract_and_immutability_are_explicit(self):
        self.assertEqual(
            PSX_LEGACY_PSW_RASTER_PROFILE_ID,
            "psx_legacy_psw_material_local_affine_modulation_preview_v2")
        self.assertEqual(
            PSX_NATIVE_RASTER_ACCELERATION_BACKEND,
            "numpy" if psx_native_raster._np is not None else "python")
        patch = rasterize_legacy_psw_triangle(
            8, 8, self.triangle,
            ((0, 0), (0, 0), (0, 0)),
            (128, 128, 128), _material((0, 0x001F)))

        self.assertEqual(patch.target_origin, (0, 0))
        self.assertFalse(patch.is_empty)
        self.assertEqual(len(patch.rgba), patch.width * patch.height * 4)
        with self.assertRaises(FrozenInstanceError):
            patch.origin_x = 7  # type: ignore[misc]

    def test_exact_gpu_modulation_is_used_after_affine_shade(self):
        source_word = 17 | (3 << 5) | (31 << 10)
        patch = rasterize_legacy_psw_triangle(
            8, 8, self.triangle,
            ((0, 0), (0, 0), (0, 0)),
            (73, 73, 73), _material((0, source_word)))

        expected = psx_texture_modulate_bgr555(
            source_word, (73, 73, 73)) + (255,)
        pixels = tuple(
            tuple(patch.rgba[offset:offset + 4])
            for offset in range(0, len(patch.rgba), 4)
            if patch.rgba[offset + 3])
        self.assertTrue(pixels)
        self.assertEqual(set(pixels), {expected})

        # The shared modulation helper preserves the exact wide GPU
        # intermediate.  The no-ABR RGBA preview saturates it only when the
        # opaque byte output is formed.
        overbright = rasterize_legacy_psw_triangle(
            8, 8, self.triangle,
            ((0, 0), (0, 0), (0, 0)),
            (255, 255, 255), _material((0, 0x7FFF)))
        self.assertEqual(
            {tuple(overbright.rgba[offset:offset + 4])
             for offset in range(0, len(overbright.rgba), 4)
             if overbright.rgba[offset + 3]},
            {(255, 255, 255, 255)})

    def test_fractional_clipped_shades_are_bounded_and_deterministic(self):
        material = _material((0, 0x7FFF))
        first = rasterize_legacy_psw_triangle(
            8, 8, self.triangle,
            ((0, 0), (0, 0), (0, 0)),
            (63.5, 127.25, 191.75), material)
        second = rasterize_legacy_psw_triangle(
            8, 8, self.triangle,
            ((0, 0), (0, 0), (0, 0)),
            (63.5, 127.25, 191.75), material)

        self.assertEqual(first, second)
        self.assertGreater(first.coverage_count, 0)

    def test_fractional_clipped_uvs_are_bounded_and_deterministic(self):
        material = _material((0, 0x7FFF))
        first = rasterize_legacy_psw_triangle(
            8, 8, self.triangle,
            ((0.25, 0.5), (64.75, 0.5), (0.25, 64.5)),
            (128, 128, 128), material)
        second = rasterize_legacy_psw_triangle(
            8, 8, self.triangle,
            ((0.25, 0.5), (64.75, 0.5), (0.25, 64.5)),
            (128, 128, 128), material)

        self.assertEqual(first, second)
        self.assertGreater(first.coverage_count, 0)

    @unittest.skipUnless(
        psx_native_raster._np is not None,
        "NumPy is unavailable for forced backend parity")
    def test_forced_numpy_and_python_backends_are_byte_exact(self):
        words = (
            0x0000, 0x001F, 0x03E0, 0x7C00,
            0x7FFF, 0x8000, 0x4210, 0x1234,
            0x801F, 0x83E0, 0xFC00, 0x294A,
            0x56B5, 0x6F7B, 0x0444, 0x8888,
        )
        indices = bytes(
            (x * 11 + y * 7 + (x * y) % 13) % 16
            for y in range(TEXTURE_HEIGHT)
            for x in range(TEXTURE_WIDTH)
        )
        material = _material(words, indices=indices)
        cases = (
            (
                "integer_top_left",
                16, 14,
                ((0, 0), (14, 0), (0, 12)),
                ((0, 0), (127, 0), (0, 127)),
                (0, 128, 255),
            ),
            (
                "fractional_and_clipped",
                13, 11,
                ((-3.75, -1.125), (15.875, 1.375), (4.625, 14.25)),
                ((0.25, 126.75), (96.5, 7.125), (63.875, 64.5)),
                (31.25, 173.75, 254.5),
            ),
            (
                "reverse_winding_fractional",
                19, 17,
                ((2.125, 1.875), (8.625, 18.25), (20.75, -2.5)),
                ((127.0, 1.0), (91.5, 126.25), (0.5, 73.75)),
                (255.0, 80.5, 0.25),
            ),
            (
                "thin_subpixel_edges",
                17, 15,
                ((1.5, 1.5), (15.5, 2.5), (7.25, 4.5000000001)),
                ((1.0, 1.0), (126.0, 2.0), (63.5, 126.5)),
                (63.5, 127.25, 191.75),
            ),
            (
                "constant_half_up_boundaries",
                7, 7,
                ((0.0, 0.0), (7.0, 0.0), (0.0, 7.0)),
                ((1.0, 3.0), (1.0, 3.0), (1.0, 3.0)),
                (127.5, 127.5, 127.5),
            ),
            (
                "fully_offscreen",
                9, 9,
                ((20.25, 20.5), (24.75, 20.5), (20.25, 24.75)),
                ((0.5, 0.5), (64.5, 0.5), (0.5, 64.5)),
                (32.5, 128.5, 224.5),
            ),
        )

        for (name, width, height, triangle, uvs, shades) in cases:
            with self.subTest(case=name):
                # Force several small vector strips as well as forcing the
                # scalar fallback.  Patch bytes and geometric coverage must
                # remain identical across both implementation boundaries.
                with mock.patch.object(
                        psx_native_raster, "NUMPY_STRIP_PIXELS", 23):
                    accelerated = rasterize_legacy_psw_triangle(
                        width, height, triangle, uvs, shades, material)
                with mock.patch.object(psx_native_raster, "_np", None):
                    fallback = rasterize_legacy_psw_triangle(
                        width, height, triangle, uvs, shades, material)
                self.assertEqual(accelerated, fallback)

    @unittest.skipUnless(
        psx_native_raster._np is not None,
        "NumPy is unavailable for forced backend parity")
    def test_forced_backends_reject_the_same_sampled_invalid_clut(self):
        invalid_indices = bytes((255,)) * (
            TEXTURE_WIDTH * TEXTURE_HEIGHT)
        malformed = _material((0, 0x7FFF), indices=invalid_indices)
        arguments = (
            8, 8, self.triangle,
            ((0, 0), (0, 0), (0, 0)),
            (128, 128, 128), malformed,
        )
        with self.assertRaisesRegex(
                PsxNativeRasterError,
                r"material texel \(0, 0\).*CLUT index 255"):
            rasterize_legacy_psw_triangle(*arguments)
        with mock.patch.object(psx_native_raster, "_np", None):
            with self.assertRaisesRegex(
                    PsxNativeRasterError,
                    r"material texel \(0, 0\).*CLUT index 255"):
                rasterize_legacy_psw_triangle(*arguments)

    def test_affine_uv_samples_direct_material_local_texel(self):
        indices = bytearray(TEXTURE_WIDTH * TEXTURE_HEIGHT)
        indices[5 * TEXTURE_WIDTH + 3] = 1
        patch = rasterize_legacy_psw_triangle(
            8, 8, self.triangle,
            ((3, 5), (3, 5), (3, 5)),
            (128, 128, 128),
            _material((0, 0x001F), indices=bytes(indices)))

        self.assertTrue(_covered_target_pixels(patch))
        self.assertEqual(
            {tuple(patch.rgba[offset:offset + 4])
             for offset in range(0, len(patch.rgba), 4)
             if patch.rgba[offset + 3]},
            {(248, 0, 0, 255)})

    def test_zero_word_is_transparent_but_still_counts_coverage(self):
        patch = rasterize_legacy_psw_triangle(
            8, 8, self.triangle,
            ((0, 0), (0, 0), (0, 0)),
            (128, 128, 128), _material((0,)))

        self.assertGreater(patch.coverage_count, 0)
        self.assertEqual(patch.rgba, bytes(len(patch.rgba)))
        self.assertEqual(_covered_target_pixels(patch), set())

    def test_every_nonzero_stp_word_remains_conservatively_opaque(self):
        patch = rasterize_legacy_psw_triangle(
            8, 8, self.triangle,
            ((0, 0), (0, 0), (0, 0)),
            (128, 128, 128), _material((0, 0x8000)))

        visible = [
            tuple(patch.rgba[offset:offset + 4])
            for offset in range(0, len(patch.rgba), 4)
            if patch.rgba[offset + 3]
        ]
        self.assertTrue(visible)
        self.assertEqual(set(visible), {(0, 0, 0, 255)})

    def test_patch_is_tight_and_clipped_to_the_target(self):
        patch = rasterize_legacy_psw_triangle(
            3, 3,
            ((-20.0, -20.0), (2.0, -20.0), (2.0, 2.0)),
            ((0, 0), (0, 0), (0, 0)),
            (128, 128, 128), _material((0, 0x03E0)))

        self.assertEqual(patch.target_origin, (0, 0))
        self.assertLessEqual(patch.origin_x + patch.width, 3)
        self.assertLessEqual(patch.origin_y + patch.height, 3)
        self.assertEqual(patch.width, 2)
        self.assertEqual(patch.height, 2)
        self.assertEqual(patch.coverage_count, 3)

        offscreen = rasterize_legacy_psw_triangle(
            16, 16,
            ((1.0e8, 1.0e8), (1.0e8 + 4, 1.0e8),
             (1.0e8, 1.0e8 + 4)),
            ((0, 0), (0, 0), (0, 0)),
            (128, 128, 128), _material((0, 0x03E0)))
        self.assertTrue(offscreen.is_empty)
        self.assertEqual(offscreen.rgba, b"")

    def test_shared_edge_top_left_ownership_is_disjoint_and_complete(self):
        material = _material((0, 0x7FFF))
        common = (
            ((0, 0), (0, 0), (0, 0)),
            (128, 128, 128),
            material,
        )
        first = rasterize_legacy_psw_triangle(
            2, 2, ((0, 0), (2, 0), (2, 2)), *common)
        second = rasterize_legacy_psw_triangle(
            2, 2, ((0, 0), (2, 2), (0, 2)), *common)
        first_pixels = _covered_target_pixels(first)
        second_pixels = _covered_target_pixels(second)

        self.assertFalse(first_pixels & second_pixels)
        self.assertEqual(
            first_pixels | second_pixels,
            {(0, 0), (1, 0), (0, 1), (1, 1)})
        self.assertEqual(
            first.coverage_count + second.coverage_count, 4)

    def test_reverse_winding_and_repeated_calls_are_byte_deterministic(self):
        material = _material((0, 0x4210))
        forward = rasterize_legacy_psw_triangle(
            8, 8, self.triangle,
            ((0, 0), (80, 0), (0, 80)),
            (20, 120, 220), material)
        reverse = rasterize_legacy_psw_triangle(
            8, 8, (self.triangle[0], self.triangle[2], self.triangle[1]),
            ((0, 0), (0, 80), (80, 0)),
            (20, 220, 120), material)
        repeated = rasterize_legacy_psw_triangle(
            8, 8, self.triangle,
            ((0, 0), (80, 0), (0, 80)),
            (20, 120, 220), material)

        self.assertEqual(forward, reverse)
        self.assertEqual(forward, repeated)

    def test_invalid_requests_fail_closed_before_unbounded_work(self):
        material = _material((0, 0x7FFF))
        valid_uv = ((0, 0), (0, 0), (0, 0))
        valid_shade = (128, 128, 128)
        calls = (
            lambda: rasterize_legacy_psw_triangle(
                True, 8, self.triangle, valid_uv, valid_shade, material),
            lambda: rasterize_legacy_psw_triangle(
                0, 8, self.triangle, valid_uv, valid_shade, material),
            lambda: rasterize_legacy_psw_triangle(
                MAX_TARGET_DIMENSION + 1, 8, self.triangle,
                valid_uv, valid_shade, material),
            lambda: rasterize_legacy_psw_triangle(
                8, 8, ((0, 0), (1, 1), (2, 2)),
                valid_uv, valid_shade, material),
            lambda: rasterize_legacy_psw_triangle(
                8, 8, ((math.nan, 0), (1, 0), (0, 1)),
                valid_uv, valid_shade, material),
            lambda: rasterize_legacy_psw_triangle(
                8, 8,
                ((MAX_SCREEN_COORDINATE_ABS + 1, 0), (1, 0), (0, 1)),
                valid_uv, valid_shade, material),
            lambda: rasterize_legacy_psw_triangle(
                8, 8, self.triangle,
                ((-1, 0), (0, 0), (0, 0)), valid_shade, material),
            lambda: rasterize_legacy_psw_triangle(
                8, 8, self.triangle,
                ((128, 0), (0, 0), (0, 0)), valid_shade, material),
            lambda: rasterize_legacy_psw_triangle(
                8, 8, self.triangle,
                ((math.nan, 0), (0, 0), (0, 0)), valid_shade, material),
            lambda: rasterize_legacy_psw_triangle(
                8, 8, self.triangle, valid_uv,
                (128, 256, 128), material),
            lambda: rasterize_legacy_psw_triangle(
                8, 8, self.triangle, valid_uv,
                (128, math.nan, 128), material),
            lambda: rasterize_legacy_psw_triangle(
                8, 8, self.triangle, valid_uv,
                (128, True, 128), material),
            lambda: rasterize_legacy_psw_triangle(
                8, 8, self.triangle, valid_uv, valid_shade, object()),
            lambda: rasterize_legacy_psw_triangle(
                8, 8, self.triangle, valid_uv, valid_shade,
                replace(material, width=64)),
            lambda: rasterize_legacy_psw_triangle(
                8, 8, self.triangle, valid_uv, valid_shade,
                replace(material, indices=b"")),
            lambda: PsxNativeRasterPatch(0, 0, 2, 2, b"", 0),
        )
        for call in calls:
            with self.subTest(call=call):
                with self.assertRaises(PsxNativeRasterError):
                    call()

        # A candidate larger than the explicit allocation/work boundary is
        # rejected instead of creating memory proportional to the target.
        side = int(math.sqrt(MAX_PATCH_PIXELS)) + 1
        with self.assertRaisesRegex(PsxNativeRasterError, "safe limit"):
            rasterize_legacy_psw_triangle(
                side, side,
                ((0, 0), (side, 0), (0, side)),
                valid_uv, valid_shade, material)

    def test_sampled_out_of_range_palette_index_fails_closed(self):
        invalid_indices = bytes((255,)) * (TEXTURE_WIDTH * TEXTURE_HEIGHT)
        malformed = _material((0, 0x7FFF), indices=invalid_indices)
        with self.assertRaisesRegex(
                PsxNativeRasterError, "out-of-range CLUT index"):
            rasterize_legacy_psw_triangle(
                8, 8, self.triangle,
                ((0, 0), (0, 0), (0, 0)),
                (128, 128, 128), malformed)


if __name__ == "__main__":
    unittest.main()
