from __future__ import annotations

from dataclasses import replace
import json
import os
from pathlib import Path
import struct
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPointF, QRectF, QSize
from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtWidgets import QApplication

from assembly_viewer import (
    PSX_NATIVE_PROFILE_ID,
    PSX_NATIVE_VIEW_MODE,
    PSX_PROTOTYPE_VIEW_MODE,
    TEXTURED_VIEW_MODES,
    VIEW_MODES,
    AssetViewport,
    _pw3_packet_nclip_front_facing,
)
from psx_native_assets import (
    PsxNativeBuild,
    load_extracted_psx_build,
    parse_psx_mesh_bytes,
)
from psx_native_contract import PsxNativeContractError
from psx_native_textures import (
    MATERIAL_SLOT_COUNT,
    PIXEL_BANK_COUNT,
    ZERO_RECORD_HEADER,
    parse_late_setgfx_bytes,
)
from viewer_main import _open_startup_path


def _native_mesh(
        selector: int = 7, *, primitive_flags: int = 0x6010):
    vertices = [
        (-65536, 65536, 0),
        (0, -65536, 32768),
        (65536, 65536, 0),
    ]
    header = bytearray(80)
    struct.pack_into("<I", header, 0, 3)
    struct.pack_into("<II", header, 0x38, 3, 1)
    struct.pack_into("<II", header, 0x40, 80, 116)
    face = bytearray(26)
    struct.pack_into("<H", face, 0, primitive_flags)
    face[2:4] = b"\x30\x40"
    struct.pack_into("<4H", face, 4, 0, 1, 2, 2)
    face[12:20] = bytes((0, 0, 128, 255, 255, 0, 255, 0))
    struct.pack_into("<H", face, 20, selector)
    face[22:26] = bytes((10, 20, 30, 30))
    data = bytes(header) + b"".join(
        struct.pack("<iii", *vertex) for vertex in vertices) + bytes(face)
    data += b"\0" * ((-len(data)) % 4)
    return parse_psx_mesh_bytes(
        data, logical_path="UNITMODL/UNIT.BIN",
        archive_ordinal=0, archive_offset=0x800)


def _native_psw_mesh(selector: int = 7):
    vertices = [
        (-65536, 65536, 0),
        (0, -65536, 32768),
        (65536, 65536, 0),
    ]
    header = bytearray(80)
    struct.pack_into("<I", header, 0, 1)
    struct.pack_into("<II", header, 0x38, 3, 1)
    struct.pack_into("<II", header, 0x40, 80, 116)
    face = bytearray(76)
    face[:8] = b"\x00\x20\x00\x00\x00\x00\x00\x00"
    struct.pack_into("<4I", face, 8, 0, 1, 2, 2)
    uv = (0, 0, 128, 255, 255, 0, 255, 0)
    struct.pack_into("<8I", face, 24, *(value << 16 for value in uv))
    struct.pack_into("<I", face, 56, selector)
    struct.pack_into(
        "<4I", face, 60, *(value << 16 for value in (10, 20, 30, 30)))
    data = bytes(header) + b"".join(
        struct.pack("<iii", *vertex) for vertex in vertices) + bytes(face)
    data += b"\0" * ((-len(data)) % 4)
    return parse_psx_mesh_bytes(
        data, logical_path="UNITMODL/V1.PSW")


def _native_horizontal_quad_mesh():
    """Synthetic equivalent of the June one-quad presentation sentinels."""

    vertices = (
        (655360, 0, -655360),
        (-655360, 0, -655360),
        (655360, 0, 655360),
        (-655360, 0, 655360),
    )
    header = bytearray(80)
    struct.pack_into("<I", header, 0, 3)
    struct.pack_into("<II", header, 0x38, 4, 1)
    struct.pack_into("<II", header, 0x40, 80, 128)
    face = bytearray(26)
    struct.pack_into("<H", face, 0, 0x8000)
    face[2:4] = b"\x30\x40"
    struct.pack_into("<4H", face, 4, 0, 2, 3, 1)
    face[12:20] = bytes((0, 0, 255, 0, 255, 255, 0, 255))
    struct.pack_into("<H", face, 20, 7)
    face[22:26] = bytes((10, 20, 30, 40))
    data = bytes(header) + b"".join(
        struct.pack("<iii", *vertex) for vertex in vertices) + bytes(face)
    data += b"\0" * ((-len(data)) % 4)
    return parse_psx_mesh_bytes(
        data, logical_path="UNITMODL/UNIT.BIN",
        archive_ordinal=0, archive_offset=0x800)


def _native_build(mesh, *texture_packs) -> PsxNativeBuild:
    has_unit_archive = mesh.archive_ordinal is not None
    return PsxNativeBuild(
        root=Path("synthetic-psx-disc"),
        system_cnf_logical_path="SYSTEM.CNF",
        system_cnf_sha256="11" * 32,
        boot_executable_logical_path="SCES_019.63",
        boot_executable_sha256="22" * 32,
        unit_archive_logical_path=(
            "UNITMODL/UNIT.BIN" if has_unit_archive else None),
        unit_archive_sha256="33" * 32 if has_unit_archive else None,
        vehicle_roster_logical_path=None,
        vehicle_roster_sha256=None,
        vehicle_roster=(),
        meshes=(mesh,),
        texture_packs=tuple(texture_packs),
    )


def _native_texture_pack():
    records = []
    for selector in range(MATERIAL_SLOT_COUNT):
        # Selector 7 is a visible red/green checker; other slots remain a
        # complete valid table so the strict parser—not a test stub—is used.
        palette = [0] * 16
        palette[1] = 0x001F  # red
        palette[2] = 0x03E0  # green
        record = bytearray(
            ZERO_RECORD_HEADER + struct.pack("<16H", *palette))
        if selector < PIXEL_BANK_COUNT:
            packed = bytes((0x21,)) * 0x2000
            record.extend(packed)
        records.append(bytes(record))
    return parse_late_setgfx_bytes(
        b"".join(records), logical_path="GFX/SET1GFX.BIN")


class PsxNativeProfileTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_legacy_pc_stylization_is_not_a_selectable_mode(self):
        self.assertNotIn(PSX_PROTOTYPE_VIEW_MODE, VIEW_MODES)
        self.assertNotIn(PSX_PROTOTYPE_VIEW_MODE, TEXTURED_VIEW_MODES)
        self.assertIn(PSX_NATIVE_VIEW_MODE, VIEW_MODES)
        self.assertIn(PSX_NATIVE_VIEW_MODE, TEXTURED_VIEW_MODES)

    def test_native_mode_rejects_a_pc_or_empty_scene(self):
        viewport = AssetViewport()
        try:
            with self.assertRaisesRegex(
                    RuntimeError, "requires a decoded PSW/PSV/PW3"):
                viewport.set_mode(PSX_NATIVE_VIEW_MODE)
            self.assertNotEqual(viewport.view_mode, PSX_NATIVE_VIEW_MODE)
        finally:
            viewport.close()

    def test_native_selection_rejects_cross_build_meshes_and_texture_packs(self):
        viewport = AssetViewport()
        mesh = _native_mesh(7)
        other_mesh = _native_mesh(19)
        texture_pack = _native_texture_pack()
        build = _native_build(mesh)
        try:
            with self.assertRaisesRegex(ValueError, "does not belong"):
                viewport.load_psx_mesh(build, other_mesh)
            with self.assertRaisesRegex(ValueError, "does not belong"):
                viewport.load_psx_mesh(
                    build, mesh, texture_pack=texture_pack)
            self.assertEqual(viewport.source_kind, "none")
            self.assertFalse(viewport.has_model)
        finally:
            viewport.close()

    def test_viewer_rejects_fabricated_pw3_as_psw_identity(self):
        viewport = AssetViewport()
        pw3 = _native_mesh()
        fabricated = replace(
            pw3,
            format_id="PSW/PSV",
            format_version=1,
        )
        try:
            with self.assertRaisesRegex(
                    PsxNativeContractError, "must not carry PW3 flags"):
                viewport.load_psx_mesh(_native_build(fabricated), fabricated)

            self.assertEqual(viewport.source_kind, "none")
            self.assertFalse(viewport.has_model)
        finally:
            viewport.close()

    def test_loading_native_mesh_clears_pc_only_resources(self):
        viewport = AssetViewport()
        viewport._family_ref = object()
        viewport._indexed_adapter = object()
        try:
            mesh = _native_mesh()
            viewport.load_psx_mesh(_native_build(mesh), mesh)

            self.assertEqual(viewport.source_kind, "psx_native")
            self.assertIsNone(viewport._family_ref)
            self.assertIsNone(viewport._indexed_adapter)
            self.assertEqual(viewport.view_mode, PSX_NATIVE_VIEW_MODE)
            self.assertTrue(viewport.has_loaded_resource)
            json.dumps(viewport.snapshot_camera_info)
            self.assertEqual(len(viewport._faces), 1)
            self.assertEqual(len(viewport._faces[0].vertices), 4)
            self.assertEqual(len(viewport._faces[0].uvs), 4)
            self.assertEqual(viewport._faces[0].native_corner_shades,
                             (10, 20, 30))
            self.assertEqual(viewport._faces[0].native_raw_corner_shades,
                             (10, 20, 30, 30))
            self.assertEqual(viewport._faces[0].native_face_offset, 116)
            self.assertEqual(
                viewport._faces[0].native_pw3_primitive_flags, 0x6010)
            self.assertEqual(
                viewport._materials[0].kind, "psx_native_selector")
            self.assertIsNone(viewport._materials[0].image)
        finally:
            viewport.close()

    def test_psw_material_local_uv_validation_is_atomic(self):
        viewport = AssetViewport()
        previous_mesh = _native_mesh()
        viewport.load_psx_mesh(_native_build(previous_mesh), previous_mesh)
        psw_mesh = _native_psw_mesh()
        malformed_face = replace(
            psw_mesh.faces[0],
            psw_uv_fixed_16_16=(
                (256 << 16, 0),
                *psw_mesh.faces[0].psw_uv_fixed_16_16[1:],
            ),
        )
        malformed_mesh = replace(psw_mesh, faces=(malformed_face,))
        try:
            with self.assertRaisesRegex(
                    ValueError, "cannot enter the material-local preview"):
                viewport.load_psx_mesh(
                    _native_build(malformed_mesh), malformed_mesh)

            self.assertIs(viewport._psx_mesh, previous_mesh)
            self.assertEqual(viewport.source_kind, "psx_native")
            self.assertTrue(viewport.has_model)
        finally:
            viewport.close()

    def test_native_load_and_reset_fit_without_overwriting_kept_camera(self):
        viewport = AssetViewport()
        viewport.resize(800, 600)
        mesh = _native_mesh()
        build = _native_build(mesh)
        try:
            viewport.load_psx_mesh(build, mesh)
            fitted = viewport.snapshot_camera_info

            self.assertEqual(
                (fitted["yaw"], fitted["pitch"]),
                (viewport.RESET_YAW, viewport.RESET_PITCH),
            )
            self.assertNotEqual(fitted["zoom"], viewport.RESET_ZOOM)
            self.assertTrue(viewport.camera_is_reset)
            self.assertFalse(viewport.can_reset_camera)

            viewport._yaw = 27.5
            viewport._pitch = -13.25
            viewport._zoom = fitted["zoom"] * 0.71
            viewport._pan = QPointF(19.0, -23.0)
            operator_camera = viewport.snapshot_camera_info
            viewport.load_psx_mesh(build, mesh, keep_camera=True)

            self.assertEqual(viewport.snapshot_camera_info, operator_camera)
            self.assertTrue(viewport.can_reset_camera)
            viewport.reset_view()
            self.assertEqual(viewport.snapshot_camera_info, fitted)
            self.assertTrue(viewport.camera_is_reset)
            self.assertFalse(viewport.can_reset_camera)
        finally:
            viewport.close()

    def test_native_visibility_probe_keeps_culling_and_provenance(self):
        cases = (
            (_native_mesh(primitive_flags=0x2010), "Back"),
            (_native_horizontal_quad_mesh(), "Bottom"),
        )
        for mesh, recommended_view in cases:
            with self.subTest(view=recommended_view):
                viewport = AssetViewport()
                viewport.resize(800, 600)
                try:
                    viewport.load_psx_mesh(_native_build(mesh), mesh)
                    renderer_before = viewport.renderer_info
                    viewport.set_backface_cull(False)

                    self.assertFalse(
                        viewport.native_render_has_visible_pixels(
                            QSize(800, 600)))
                    self.assertEqual(viewport.renderer_info, renderer_before)
                    viewport.apply_view_preset(
                        recommended_view, QSize(800, 600))
                    self.assertTrue(
                        viewport.native_render_has_visible_pixels(
                            QSize(800, 600)))
                finally:
                    viewport.close()

    @unittest.skipUnless(
        os.environ.get("OPENUA_PSX_CORPUS_ROOT"),
        "set OPENUA_PSX_CORPUS_ROOT for recovered prototype checks")
    def test_real_june_presentation_sentinels_recommend_visible_views(self):
        corpus = Path(os.environ["OPENUA_PSX_CORPUS_ROOT"])
        root = corpus / "technical" / "analysis" / "1999-06-15" \
            / "work" / "disc_files"
        build = load_extracted_psx_build(root)
        meshes = {
            mesh.archive_ordinal: mesh for mesh in build.meshes
            if mesh.archive_ordinal is not None
        }
        cases = {
            15: "Back",
            16: "Back",
            112: "Bottom",
            113: "Bottom",
            128: "Bottom",
            134: "Bottom",
        }
        viewport = AssetViewport()
        viewport.resize(800, 600)
        try:
            for ordinal, recommended_view in cases.items():
                with self.subTest(ordinal=ordinal):
                    viewport.load_psx_mesh(build, meshes[ordinal])
                    self.assertTrue(viewport.camera_is_reset)
                    self.assertFalse(
                        viewport.native_render_has_visible_pixels(
                            QSize(800, 600)))
                    viewport.apply_view_preset(
                        recommended_view, QSize(800, 600))
                    self.assertTrue(
                        viewport.native_render_has_visible_pixels(
                            QSize(800, 600)))
        finally:
            viewport.close()

    def test_renderer_info_is_derived_from_the_loaded_native_mesh(self):
        viewport = AssetViewport()
        try:
            mesh = _native_mesh(19)
            viewport.load_psx_mesh(_native_build(mesh), mesh)
            output = viewport.render_snapshot(QSize(96, 96), QColor("black"))
            info = viewport.renderer_info

            self.assertFalse(output.isNull())
            self.assertEqual(info["profile_id"], PSX_NATIVE_PROFILE_ID)
            self.assertEqual(info["effective_mode"], PSX_NATIVE_PROFILE_ID)
            self.assertEqual(
                info["source_asset_pipeline"], "psx_native_disc_assets")
            self.assertTrue(info["native_psx_asset_decode"])
            self.assertFalse(info["pc_openua_source_used"])
            self.assertEqual(info["native_mesh_ordinal"], 0)
            self.assertEqual(info["native_mesh_offset"], 0x800)
            self.assertEqual(info["native_mesh_sha256"], mesh.body_sha256)
            self.assertEqual(info["texture_selector_census"], [[19, 1]])
            self.assertEqual(
                info["native_face_prefix_census"], [["10603040", 1]])
            self.assertEqual(
                info["native_primitive_cull_census"],
                [["pw3_bit14_set_two_sided", 1]])
            self.assertEqual(
                info["native_corner_shade_census"],
                [[10, 1], [20, 1], [30, 1]])
            self.assertEqual(
                info["native_raw_corner_shade_census"],
                [[10, 1], [20, 1], [30, 2]])
            self.assertEqual(
                info["texture_binding_status"],
                "unavailable_not_substituted")
            self.assertFalse(info["native_texture_decode"])
            self.assertIn(
                "not_applied_topology_only", info["psx_color_semantics"])
            self.assertEqual(
                info["sources"]["psx_build"]["unit_archive_sha256"],
                "33" * 32)
            self.assertIn("no PC BASE", info["scope"])
            self.assertNotIn("pc_openua_asset_family", repr(info))
        finally:
            viewport.close()

    def test_topology_provenance_is_effective_and_caller_immutable(self):
        viewport = AssetViewport()
        try:
            mesh = _native_mesh(7)
            texture_pack = _native_texture_pack()
            viewport.load_psx_mesh(
                _native_build(mesh, texture_pack), mesh)
            viewport.render_snapshot(QSize(64, 64), QColor("black"))
            info = viewport.renderer_info

            self.assertFalse(info["native_texture_decode"])
            self.assertEqual(
                info["texture_binding_status"],
                "topology_only_operator_default")
            self.assertIn(
                "validated_native_packs_available_not_selected",
                info["psx_color_semantics"])
            self.assertNotIn(
                "bgr555_and_zero_word_transparency_applied",
                info["psx_color_semantics"])

            packs = info["sources"]["psx_build"]["native_texture_packs"]
            self.assertTrue(packs)
            expected_hash = packs[0]["sha256"]
            packs[0]["sha256"] = "CALLER_MUTATED"
            info["pw3_gpu_packet_corner_order"][0] = 99
            self.assertEqual(
                viewport.renderer_info["sources"]["psx_build"]
                ["native_texture_packs"][0]["sha256"],
                expected_hash,
            )
            self.assertEqual(
                viewport.renderer_info["pw3_gpu_packet_corner_order"],
                [1, 0, 2, 3])
        finally:
            viewport.close()

    def test_native_file_cli_arguments_never_enter_the_pc_loader(self):
        class FakeWindow:
            def __init__(self):
                self.calls = []

            def open_psx_source(self, path):
                self.calls.append(("psx", path))

            def open_setbas(self, path):
                self.calls.append(("setbas", path))

            def open_base(self, path):
                self.calls.append(("pc", path))

        window = FakeWindow()
        for native_path in (
                "mesh.PW3", "mesh.psw", "mesh.PSV", "UNIT.BIN"):
            with self.subTest(native_path=native_path):
                with self.assertRaisesRegex(
                        ValueError, "extracted prototype disc tree"):
                    _open_startup_path(window, native_path)
        for unsupported_path in (
                "GFX/SET1GFX.BIN", "SYSTEM.CNF", "SCES_019.63",
                "unknown.dat"):
            with self.subTest(unsupported_path=unsupported_path):
                with self.assertRaisesRegex(
                        ValueError, "Unsupported startup source"):
                    _open_startup_path(window, unsupported_path)
        self.assertEqual(window.calls, [])

        _open_startup_path(window, "model.BASE")
        _open_startup_path(window, "model.bas")
        _open_startup_path(window, "SET.BAS")
        self.assertEqual(
            [kind for kind, _path in window.calls],
            ["pc", "pc", "setbas"],
        )

    def test_native_texture_pack_is_used_without_any_pc_texture_source(self):
        viewport = AssetViewport()
        try:
            texture_pack = _native_texture_pack()
            mesh = _native_mesh(7)
            viewport.load_psx_mesh(
                _native_build(mesh, texture_pack), mesh,
                texture_pack=texture_pack)
            output = viewport.render_snapshot(QSize(96, 96), QColor("black"))
            info = viewport.renderer_info

            self.assertFalse(output.isNull())
            self.assertEqual(
                viewport._materials[0].kind, "psx_native_texture")
            material_image = viewport._materials[0].image
            self.assertIsNotNone(material_image)
            assert material_image is not None
            self.assertEqual(
                material_image.pixelColor(0, 0).getRgb(), (255, 0, 0, 255))
            self.assertEqual(
                material_image.pixelColor(1, 0).getRgb(), (0, 255, 0, 255))
            self.assertIs(viewport.psx_texture_pack, texture_pack)
            self.assertTrue(info["native_texture_decode"])
            self.assertEqual(
                info["texture_binding_status"],
                "operator_selected_pack_with_validated_selector_table")
            self.assertEqual(
                info["texture_selector_table_status"],
                "validated_native_setgfx_selector_table")
            self.assertEqual(
                info["mesh_to_texture_pack_binding"],
                "operator_selected_environment_variant_no_mesh_inherent_"
                "affinity")
            self.assertEqual(
                info["native_texture_pack_path"], "GFX/SET1GFX.BIN")
            self.assertEqual(
                info["native_texture_pack_sha256"],
                texture_pack.source_sha256)
            self.assertEqual(
                info["native_texture_mapping"],
                "selector_S_clut_S_pixel_bank_S_and_31")
            self.assertEqual(
                info["native_texture_uv_profile"],
                "authored_uv_byte_scaled_256_to_128_nearest_half_up_preview")
            self.assertEqual(
                info["native_texture_descriptor_origin"],
                "unresolved_not_applied")
            self.assertEqual(
                info["native_texture_absolute_vram_binding"],
                "unresolved_not_applied")
            self.assertEqual(
                info["native_texture_pack_layout_id"],
                "late_compact_setgfx_v1")
            self.assertEqual(
                info["native_texture_selector_to_pixel_bank_mapping"],
                "selector_S_clut_S_pixel_bank_S_and_31")
            self.assertEqual(info["native_texture_material_slot_count"], 128)
            self.assertEqual(
                info["native_texture_populated_selectors"], list(range(128)))
            self.assertFalse(info["pc_openua_source_used"])
            self.assertIn("psx_build", info["sources"])
            self.assertIn(
                "effective_dispatch_unresolved_not_applied",
                info["psx_color_semantics"])
            self.assertNotIn(
                "legacy_psw_direct_grayscale_affine_modulation_applied",
                info["psx_color_semantics"])
        finally:
            viewport.close()

    def test_psw_selected_pack_applies_recovered_affine_grayscale_only(self):
        viewport = AssetViewport()
        viewport.resize(128, 128)
        texture_pack = _native_texture_pack()
        mesh = _native_psw_mesh(7)
        try:
            viewport.load_psx_mesh(
                _native_build(mesh, texture_pack), mesh,
                texture_pack=texture_pack)
            viewport.apply_view_preset("Back", QSize(128, 128))
            output = viewport.render_snapshot(
                QSize(128, 128), QColor("black"))
            visible = [
                output.pixelColor(x, y).getRgb()
                for y in range(output.height())
                for x in range(output.width())
                if output.pixelColor(x, y).getRgb()[:3] != (0, 0, 0)
            ]
            info = viewport.renderer_info

            self.assertTrue(visible)
            # The test face carries authored shades 10,20,30,30.  A raw
            # checker would reach 255; recovered PSW modulation keeps this
            # bounded fixture at 58 or below after the exact wide multiply.
            self.assertLessEqual(
                max(max(pixel[:3]) for pixel in visible), 58)
            self.assertEqual(
                info["psw_psv_raster_profile"],
                "psx_legacy_psw_material_local_affine_modulation_preview_v2")
            self.assertIn(
                "legacy_psw_material_local_uv_quotient_and_direct_"
                "grayscale_affine_modulation_applied",
                info["psx_color_semantics"])
            self.assertIn(
                "descriptor_origin_tpage_clut_offset_stp_abr_runtime_"
                "binding_unresolved",
                info["psx_color_semantics"])
            self.assertEqual(
                info["native_texture_mapping"],
                "selector_S_clut_S_pixel_bank_S_and_31")
            self.assertEqual(
                info["native_texture_uv_profile"],
                "recovered_signed_16_16_div2_toward_zero_material_local_"
                "pre_origin")
            self.assertEqual(
                info["native_texture_descriptor_origin"],
                "omitted_material_local_preview_runtime_binding_unresolved")
            self.assertEqual(
                info["native_texture_absolute_vram_binding"],
                "unresolved_not_applied")
            self.assertNotIn(
                "pw3_packet_shade_formulas",
                info["psx_color_semantics"])
        finally:
            viewport.close()

    def test_pw3_nclip_uses_packet_order_and_rejects_zero_area(self):
        positive = (
            QPointF(1, 1), QPointF(0, -1),
            QPointF(-1, 1), QPointF(2, 2))
        negative = (
            QPointF(-1, 1), QPointF(0, -1),
            QPointF(1, 1), QPointF(2, 2))
        edge_on = (
            QPointF(1, 0), QPointF(0, 0),
            QPointF(-1, 0), QPointF(2, 2))

        self.assertTrue(_pw3_packet_nclip_front_facing(positive))
        self.assertFalse(_pw3_packet_nclip_front_facing(negative))
        self.assertFalse(_pw3_packet_nclip_front_facing(edge_on))

    def test_pw3_bit14_bypasses_nclip_but_clear_face_does_not(self):
        target = QRectF(0, 0, 96, 96)

        def draw_count(primitive_flags: int) -> int:
            viewport = AssetViewport()
            mesh = _native_mesh(primitive_flags=primitive_flags)
            viewport.load_psx_mesh(_native_build(mesh), mesh)
            viewport._yaw = 0.0
            viewport._pitch = 0.0
            output = QImage(96, 96, QImage.Format.Format_ARGB32)
            output.fill(QColor("black"))
            painter = QPainter(output)
            try:
                with patch.object(viewport, "_draw_face") as draw:
                    viewport._render_scene(
                        painter, target, QColor("black"), False,
                        viewport._camera_state())
                    return draw.call_count
            finally:
                painter.end()
                viewport.close()

        # At this fixed view the executable's raw (1,0,2) packet order has a
        # non-positive NCLIP result.  Bit 14 bypasses that source-face test.
        self.assertEqual(draw_count(0x2010), 0)
        self.assertGreater(draw_count(0x6010), 0)

    def test_psw_psv_uses_raw_gt4_slots_and_unconditional_nclip(self):
        target = QRectF(0, 0, 96, 96)
        viewport = AssetViewport()
        mesh = _native_psw_mesh()
        viewport.load_psx_mesh(_native_build(mesh), mesh)
        viewport._yaw = 0.0
        viewport._pitch = 0.0
        output = QImage(96, 96, QImage.Format.Format_ARGB32)
        output.fill(QColor("black"))
        painter = QPainter(output)
        try:
            self.assertEqual(len(viewport._faces[0].vertices), 4)
            self.assertEqual(len(viewport._faces[0].uvs), 4)
            self.assertEqual(
                viewport._faces[0].uvs,
                [(0, 0), (64, 127), (127, 0), (127, 0)])
            self.assertEqual(
                viewport._faces[0].native_mesh_format_id, "PSW/PSV")
            self.assertIsNone(
                viewport._faces[0].native_pw3_primitive_flags)
            with patch.object(viewport, "_draw_face") as draw:
                viewport._render_scene(
                    painter, target, QColor("black"), False,
                    viewport._camera_state())
                self.assertEqual(draw.call_count, 0)
        finally:
            painter.end()
            viewport.close()

    def test_native_render_is_hard_edged_and_pw3_bit14_is_two_sided(self):
        viewport = AssetViewport()
        viewport.resize(96, 96)
        mesh = _native_mesh()
        viewport.load_psx_mesh(_native_build(mesh), mesh)
        viewport.set_backface_cull(True)
        output = QImage(96, 96, QImage.Format.Format_ARGB32)
        output.fill(QColor("black"))
        painter = QPainter(output)
        try:
            viewport._render_scene(
                painter, QRectF(0, 0, 96, 96), QColor("black"), False,
                viewport._camera_state())
            self.assertFalse(painter.testRenderHint(
                QPainter.RenderHint.Antialiasing))
            self.assertFalse(painter.testRenderHint(
                QPainter.RenderHint.SmoothPixmapTransform))
        finally:
            painter.end()
            viewport.close()

    def test_snapshot_is_deterministic_and_never_uses_pc_renderer(self):
        viewport = AssetViewport()
        mesh = _native_mesh()
        viewport.load_psx_mesh(_native_build(mesh), mesh)
        try:
            first = viewport.render_snapshot(QSize(96, 96), QColor("black"))
            first_bytes = bytes(first.constBits())
            second = viewport.render_snapshot(QSize(96, 96), QColor("black"))
            self.assertEqual(first_bytes, bytes(second.constBits()))
            with self.assertRaisesRegex(
                    RuntimeError, "cannot render the active native PSX"):
                viewport.set_mode("textured")
        finally:
            viewport.close()

    def test_snapshot_forces_native_renderer_from_non_native_toolbar_modes(self):
        viewport = AssetViewport()
        mesh = _native_mesh()
        viewport.load_psx_mesh(_native_build(mesh), mesh)
        try:
            for toolbar_mode in ("wireframe", "solid", "materials"):
                with self.subTest(toolbar_mode=toolbar_mode):
                    viewport.set_mode(toolbar_mode)
                    output = viewport.render_snapshot(
                        QSize(72, 72), QColor("black"),
                        include_guides=True)
                    info = viewport.renderer_info
                    self.assertFalse(output.isNull())
                    self.assertEqual(viewport.view_mode, toolbar_mode)
                    self.assertEqual(
                        info["requested_mode"], PSX_NATIVE_VIEW_MODE)
                    self.assertEqual(
                        info["effective_mode"], PSX_NATIVE_PROFILE_ID)
                    self.assertFalse(info["pc_openua_source_used"])
        finally:
            viewport.close()

    def test_snapshot_fails_if_native_scene_changes_during_render(self):
        viewport = AssetViewport()
        mesh = _native_mesh(7)
        viewport.load_psx_mesh(_native_build(mesh), mesh)
        original = viewport._render_scene

        def mutate(*args, **kwargs):
            original(*args, **kwargs)
            # Same source bytes, different immutable selection object.
            viewport._psx_mesh = _native_mesh(7)

        try:
            with patch.object(viewport, "_render_scene", side_effect=mutate):
                with self.assertRaisesRegex(
                        RuntimeError, "source changed during snapshot"):
                    viewport.render_snapshot(
                        QSize(48, 48), QColor("black"))
        finally:
            viewport.close()

    def test_snapshot_fails_if_native_texture_source_changes_during_render(self):
        viewport = AssetViewport()
        mesh = _native_mesh(7)
        texture_pack = _native_texture_pack()
        viewport.load_psx_mesh(
            _native_build(mesh, texture_pack), mesh,
            texture_pack=texture_pack)
        original = viewport._render_scene

        def mutate(*args, **kwargs):
            original(*args, **kwargs)
            # Same decoded bytes, different immutable selection object.
            viewport._psx_texture_pack = _native_texture_pack()

        try:
            with patch.object(viewport, "_render_scene", side_effect=mutate):
                with self.assertRaisesRegex(
                        RuntimeError, "source changed during snapshot"):
                    viewport.render_snapshot(
                        QSize(48, 48), QColor("black"))
        finally:
            viewport.close()

    def test_snapshot_fails_if_native_build_identity_changes_during_render(self):
        viewport = AssetViewport()
        mesh = _native_mesh()
        viewport.load_psx_mesh(_native_build(mesh), mesh)
        original = viewport._render_scene

        def mutate(*args, **kwargs):
            original(*args, **kwargs)
            # Same portable hashes, different selected build object.
            viewport._psx_build = _native_build(mesh)

        try:
            with patch.object(viewport, "_render_scene", side_effect=mutate):
                with self.assertRaisesRegex(
                        RuntimeError, "source changed during snapshot"):
                    viewport.render_snapshot(
                        QSize(48, 48), QColor("black"))
        finally:
            viewport.close()


if __name__ == "__main__":
    unittest.main()
