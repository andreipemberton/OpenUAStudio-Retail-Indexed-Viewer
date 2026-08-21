from __future__ import annotations

from dataclasses import FrozenInstanceError
import hashlib
import os
from pathlib import Path
import unittest

from psx_gte import (
    HARDWARE_HELPER_EVIDENCE,
    PSX_GTE_HELPER_PROFILE_ID,
    PSX_DITHER_MATRIX,
    PSX_DIVIDE_MAX,
    PSX_POLY_GT4_FIELD_OFFSETS,
    PSX_POLY_GT4_OPAQUE_COMMAND,
    PSX_POLY_GT4_PACKET_SIZE,
    PSX_POLY_GT4_SEMITRANSPARENT_COMMAND,
    UA_DITHER_ENABLE_STATE,
    UA_GTE_CONFIG_SELECTION_STATE,
    UA_GTE_CONTROL_INIT_FILE_RANGE,
    UA_GTE_CONTROL_INIT_RANGE_SHA256,
    UA_GTE_DQA,
    UA_GTE_DQB,
    UA_GTE_H,
    UA_GTE_RENDER_CALL_FILE_RANGES,
    UA_GTE_RENDER_CALL_RANGE_SHA256,
    UA_GTE_RENDER_INIT_FILE_RANGE,
    UA_GTE_RENDER_INIT_RANGE_SHA256,
    UA_GTE_ZSF3,
    UA_GTE_ZSF4,
    UA_JUNE_MAIN_EXE_SHA256,
    UA_OT_TRAVERSAL_STATE,
    UA_VIEWER_TRANSFORM_MAPPING_STATE,
    PsxGteError,
    gte_avsz3,
    gte_avsz4,
    gte_divide,
    gte_nclip,
    gte_project_screen,
    psx_add_prim_same_bucket_order,
    psx_dither_offset,
    psx_quantize_bgr555,
    psx_quantize_channel_5,
    ua_late_unit_gt4_command,
    ua_ot_bucket_relative_byte_offset,
    urban_assault_gte_config,
)


def _canonical_main_executable(corpus: Path) -> Path | None:
    candidates = (
        corpus / "technical" / "analysis" / "1999-06-15"
        / "work" / "disc_files" / "SCES_019.63",
        corpus / "analysis" / "1999-06-15"
        / "work" / "disc_files" / "SCES_019.63",
        corpus / "1999-06-15" / "work" / "disc_files" / "SCES_019.63",
        corpus / "work" / "disc_files" / "SCES_019.63",
        corpus / "SCES_019.63",
    )
    return next((path for path in candidates if path.is_file()), None)


class UrbanAssaultGteConfigTests(unittest.TestCase):
    def test_recovered_ntsc_branch_constants_are_exact(self) -> None:
        config = urban_assault_gte_config(width=512, height=240)

        self.assertEqual("ntsc_512x240", config.branch)
        self.assertEqual(256 << 16, config.ofx)
        self.assertEqual(120 << 16, config.ofy)
        self.assertEqual(UA_GTE_H, config.h)
        self.assertEqual(UA_GTE_ZSF3, config.zsf3)
        self.assertEqual(UA_GTE_ZSF4, config.zsf4)
        self.assertEqual(UA_GTE_DQA, config.dqa)
        self.assertEqual(UA_GTE_DQB, config.dqb)

    def test_recovered_pal_branch_must_be_selected_explicitly(self) -> None:
        config = urban_assault_gte_config(width=512, height=256)

        self.assertEqual("pal_512x256", config.branch)
        self.assertEqual(128 << 16, config.ofy)

    def test_unsupported_viewer_dimensions_fail_closed(self) -> None:
        for width, height in ((320, 240), (640, 240), (512, 224), (512, 480)):
            with self.subTest(width=width, height=height):
                with self.assertRaises(PsxGteError):
                    urban_assault_gte_config(width=width, height=height)
        with self.assertRaises(TypeError):
            urban_assault_gte_config()  # type: ignore[call-arg]

    def test_config_is_immutable(self) -> None:
        config = urban_assault_gte_config(width=512, height=240)
        with self.assertRaises(FrozenInstanceError):
            config.h = 1000  # type: ignore[misc]

    def test_evidence_boundaries_are_machine_readable(self) -> None:
        self.assertEqual(
            "psx_gte_integer_helpers_v1", PSX_GTE_HELPER_PROFILE_ID)
        self.assertIn("hardware_truth", HARDWARE_HELPER_EVIDENCE)
        self.assertEqual(
            "explicit_pal_or_ntsc_branch_required",
            UA_GTE_CONFIG_SELECTION_STATE,
        )
        self.assertEqual("unresolved", UA_VIEWER_TRANSFORM_MAPPING_STATE)
        self.assertEqual("unresolved", UA_DITHER_ENABLE_STATE)
        self.assertEqual(
            "unresolved_beyond_same_bucket_lifo", UA_OT_TRAVERSAL_STATE)

    @unittest.skipUnless(
        os.environ.get("OPENUA_PSX_CORPUS_ROOT"),
        "set OPENUA_PSX_CORPUS_ROOT for recovered executable checks")
    def test_recovered_executable_constant_anchors(self) -> None:
        corpus = Path(os.environ["OPENUA_PSX_CORPUS_ROOT"])
        executable = _canonical_main_executable(corpus)
        self.assertIsNotNone(
            executable,
            "OPENUA_PSX_CORPUS_ROOT has no 1999-06-15 SCES_019.63",
        )
        assert executable is not None
        data = executable.read_bytes()
        self.assertEqual(
            UA_JUNE_MAIN_EXE_SHA256, hashlib.sha256(data).hexdigest())

        anchors = (
            (
                UA_GTE_CONTROL_INIT_FILE_RANGE,
                UA_GTE_CONTROL_INIT_RANGE_SHA256,
            ),
            (
                UA_GTE_RENDER_INIT_FILE_RANGE,
                UA_GTE_RENDER_INIT_RANGE_SHA256,
            ),
            *zip(
                UA_GTE_RENDER_CALL_FILE_RANGES,
                UA_GTE_RENDER_CALL_RANGE_SHA256,
            ),
        )
        for offsets, expected_hash in anchors:
            with self.subTest(offsets=offsets):
                start, end = offsets
                self.assertEqual(
                    expected_hash,
                    hashlib.sha256(data[start:end]).hexdigest(),
                )


class GteHardwareTruthTests(unittest.TestCase):
    def test_unr_division_rounding_matches_rtps_oracle(self) -> None:
        result = gte_divide(200, 500)

        self.assertEqual(0x6666, result.quotient)
        self.assertFalse(result.overflow)

    def test_division_overflow_matches_gte_flag_17_condition(self) -> None:
        overflow = gte_divide(200, 100)
        self.assertEqual(PSX_DIVIDE_MAX, overflow.quotient)
        self.assertTrue(overflow.overflow)
        zero = gte_divide(0, 0)
        self.assertEqual(PSX_DIVIDE_MAX, zero.quotient)
        self.assertTrue(zero.overflow)

    def test_rtps_identity_center_hardware_oracle(self) -> None:
        config = urban_assault_gte_config(width=512, height=240)
        # Replace only the explicit screen controls used by the PCSX test.
        test_config = type(config)(
            video_width=320,
            video_height=240,
            branch="pcsx_hw_rtps_identity",
            ofx=160 << 16,
            ofy=120 << 16,
            h=200,
            zsf3=config.zsf3,
            zsf4=config.zsf4,
            dqa=config.dqa,
            dqb=config.dqb,
        )

        projected = gte_project_screen(0, 0, 1000, test_config)

        self.assertEqual((160, 120), (projected.screen_x, projected.screen_y))
        self.assertFalse(projected.division_overflow)

    def test_rtps_offset_vertex_hardware_rounding_oracle(self) -> None:
        config = urban_assault_gte_config(width=512, height=240)
        test_config = type(config)(
            video_width=320,
            video_height=240,
            branch="pcsx_hw_rtps_offset",
            ofx=160 << 16,
            ofy=120 << 16,
            h=200,
            zsf3=config.zsf3,
            zsf4=config.zsf4,
            dqa=config.dqa,
            dqb=config.dqb,
        )

        projected = gte_project_screen(100, 50, 500, test_config)

        self.assertEqual((199, 139), (projected.screen_x, projected.screen_y))
        self.assertEqual(0x6666, projected.h_over_sz3)

    def test_rtps_screen_coordinate_saturates_at_hardware_bounds(self) -> None:
        config = urban_assault_gte_config(width=512, height=240)
        test_config = type(config)(
            video_width=320,
            video_height=240,
            branch="pcsx_hw_rtps_saturation",
            ofx=0,
            ofy=0,
            h=200,
            zsf3=config.zsf3,
            zsf4=config.zsf4,
            dqa=config.dqa,
            dqb=config.dqb,
        )

        projected = gte_project_screen(0x7FFF, 0, 100, test_config)

        self.assertEqual(0x3FF, projected.screen_x)
        self.assertTrue(projected.x_saturated)
        self.assertTrue(projected.division_overflow)

    def test_screen_projection_requires_an_explicit_config(self) -> None:
        with self.assertRaises(TypeError):
            gte_project_screen(0, 0, 1000)  # type: ignore[call-arg]

    def test_nclip_hardware_winding_and_collinear_oracles(self) -> None:
        self.assertEqual(
            10000, gte_nclip((0, 0), (100, 0), (0, 100)).mac0)
        self.assertEqual(
            -10000, gte_nclip((0, 0), (0, 100), (100, 0)).mac0)
        self.assertEqual(
            0, gte_nclip((0, 0), (50, 50), (100, 100)).mac0)

    def test_nclip_hardware_mac0_wrap_oracle(self) -> None:
        result = gte_nclip(
            (0x7FFF, 0x7FFF),
            (-0x8000, -0x8000),
            (-0x8000, 0x7FFF),
        )

        self.assertEqual(131071, result.mac0)
        self.assertTrue(result.negative_overflow)
        self.assertFalse(result.positive_overflow)

    def test_avsz_hardware_oracles(self) -> None:
        avsz3 = gte_avsz3(100, 200, 300, 0x555)
        self.assertEqual(819000, avsz3.mac0)
        self.assertEqual(199, avsz3.otz)

        avsz4 = gte_avsz4(100, 200, 300, 400, 0x400)
        self.assertEqual(1024000, avsz4.mac0)
        self.assertEqual(250, avsz4.otz)

    def test_avsz_saturates_negative_and_overlarge_otz(self) -> None:
        negative = gte_avsz3(100, 200, 300, -0x1000)
        self.assertEqual(0, negative.otz)
        self.assertTrue(negative.otz_saturated)

        overlarge = gte_avsz3(0xFFFF, 0xFFFF, 0xFFFF, 0x1000)
        self.assertEqual(0xFFFF, overlarge.otz)
        self.assertTrue(overlarge.otz_saturated)


class GpuHardwareTruthTests(unittest.TestCase):
    def test_dither_matrix_matches_all_sixteen_hardware_cells(self) -> None:
        observed = tuple(
            tuple(psx_dither_offset(x, y) for x in range(4))
            for y in range(4))
        self.assertEqual(PSX_DITHER_MATRIX, observed)
        self.assertEqual(psx_dither_offset(0, 0), psx_dither_offset(16, 16))

    def test_mid_gray_bgr555_matches_hardware_phase_11(self) -> None:
        self.assertEqual(
            0x3DEF,
            psx_quantize_bgr555(
                128, 128, 128, 8, 8, dither_enabled=True),
        )
        self.assertEqual(
            0x4210,
            psx_quantize_bgr555(
                128, 128, 128, 9, 8, dither_enabled=True),
        )

    def test_dither_is_channel_independent_and_clamped(self) -> None:
        self.assertEqual(
            0x000F,
            psx_quantize_bgr555(128, 0, 0, 8, 8, dither_enabled=True),
        )
        self.assertEqual(
            0x01E0,
            psx_quantize_bgr555(0, 128, 0, 8, 8, dither_enabled=True),
        )
        self.assertEqual(
            0x3C00,
            psx_quantize_bgr555(0, 0, 128, 8, 8, dither_enabled=True),
        )
        self.assertEqual(
            0,
            psx_quantize_channel_5(3, 8, 8, dither_enabled=True),
        )
        self.assertEqual(
            31,
            psx_quantize_channel_5(255, 10, 9, dither_enabled=True),
        )

    def test_dither_enablement_cannot_be_implicit(self) -> None:
        with self.assertRaises(TypeError):
            psx_quantize_channel_5(128, 8, 8)  # type: ignore[call-arg]


class UrbanAssaultPacketPathTests(unittest.TestCase):
    def test_late_unit_packet_is_exact_poly_gt4_layout(self) -> None:
        offsets = dict(PSX_POLY_GT4_FIELD_OFFSETS)

        self.assertEqual(52, PSX_POLY_GT4_PACKET_SIZE)
        self.assertEqual((8, 20, 32, 44), tuple(
            offsets[f"xy{index}"] for index in range(4)))
        self.assertEqual((4, 16, 28, 40), (
            offsets["rgb0_code"], offsets["rgb1"],
            offsets["rgb2"], offsets["rgb3"],
        ))
        self.assertEqual((12, 24, 36, 48), (
            offsets["uv0_clut"], offsets["uv1_tpage"],
            offsets["uv2"], offsets["uv3"],
        ))

    def test_late_unit_descriptor_tpage_selects_gt4_semitransparency(self) -> None:
        self.assertEqual(
            PSX_POLY_GT4_OPAQUE_COMMAND, ua_late_unit_gt4_command(0))
        self.assertEqual(
            PSX_POLY_GT4_SEMITRANSPARENT_COMMAND,
            ua_late_unit_gt4_command(0x20),
        )
        self.assertEqual(
            PSX_POLY_GT4_SEMITRANSPARENT_COMMAND,
            ua_late_unit_gt4_command(0x40),
        )
        self.assertEqual(
            PSX_POLY_GT4_SEMITRANSPARENT_COMMAND,
            ua_late_unit_gt4_command(0x60),
        )

    def test_recovered_ot_formula_and_same_bucket_lifo(self) -> None:
        self.assertEqual(-64, ua_ot_bucket_relative_byte_offset(0))
        self.assertEqual(0, ua_ot_bucket_relative_byte_offset(16))
        self.assertEqual(732, ua_ot_bucket_relative_byte_offset(199))
        self.assertEqual(
            ("packet-c", "packet-b", "packet-a"),
            psx_add_prim_same_bucket_order(
                ("packet-a", "packet-b", "packet-c")),
        )


if __name__ == "__main__":
    unittest.main()
