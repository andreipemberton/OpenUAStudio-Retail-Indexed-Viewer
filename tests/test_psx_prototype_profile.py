from __future__ import annotations

import os
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPointF, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtWidgets import QApplication

from assembly_viewer import (
    PSX_PROTOTYPE_PROFILE_ID,
    PSX_PROTOTYPE_VIEW_MODE,
    TEXTURED_VIEW_MODES,
    VIEW_MODES,
    AssetViewport,
    ViewFace,
    ViewMaterial,
)


def _checker() -> QImage:
    image = QImage(2, 2, QImage.Format.Format_ARGB32)
    image.setPixelColor(0, 0, QColor("black"))
    image.setPixelColor(1, 0, QColor("white"))
    image.setPixelColor(0, 1, QColor("white"))
    image.setPixelColor(1, 1, QColor("black"))
    return image


def _viewport() -> AssetViewport:
    viewport = AssetViewport()
    viewport.resize(160, 160)
    viewport._show_grid = False
    viewport._show_axes = False
    viewport._backface_cull = False
    viewport._center = (0.0, 0.0, 0.0)
    viewport._scale = 1.0
    viewport._yaw = 0.0
    viewport._pitch = 0.0
    viewport._zoom = 1.0
    viewport._pan = QPointF()
    viewport._materials = [ViewMaterial("CHECKER", image=_checker())]
    viewport._faces = [ViewFace(
        vertices=[
            (-1.1, -1.0, -0.8),
            (0.0, 1.15, 0.6),
            (1.2, -0.9, 1.1),
        ],
        uvs=[(0, 255), (128, 0), (255, 255)],
        material=0,
        poly_id=0,
    )]
    return viewport


class PsxPrototypeProfileTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_profile_is_a_first_class_textured_view_mode(self):
        self.assertIn(PSX_PROTOTYPE_VIEW_MODE, VIEW_MODES)
        self.assertIn(PSX_PROTOTYPE_VIEW_MODE, TEXTURED_VIEW_MODES)

    def test_selecting_profile_reports_its_truthful_fixed_scope(self):
        viewport = _viewport()
        messages = []
        viewport.statusMessage.connect(messages.append)
        try:
            viewport.set_mode(PSX_PROTOTYPE_VIEW_MODE)
            info = viewport.renderer_info
            self.assertEqual(info["profile_id"], PSX_PROTOTYPE_PROFILE_ID)
            self.assertEqual(info["profile_version"], 1)
            self.assertEqual(
                info["source_asset_pipeline"], "pc_openua_asset_family")
            self.assertFalse(info["native_psx_asset_decode"])
            self.assertFalse(info["cycle_accurate"])
            self.assertEqual(info["texture_interpolation"], "affine")
            self.assertEqual(info["texture_filter"], "nearest")
            self.assertFalse(info["polygon_antialiasing"])
            self.assertIn("does not decode PSX UNIT.BIN", info["scope"])
            self.assertTrue(any("experimental" in message for message in messages))
        finally:
            viewport.close()

    def test_profile_disables_polygon_and_texture_antialiasing_at_rest(self):
        viewport = _viewport()
        viewport.set_mode(PSX_PROTOTYPE_VIEW_MODE)
        output = QImage(160, 160, QImage.Format.Format_ARGB32)
        output.fill(Qt.GlobalColor.transparent)
        painter = QPainter(output)
        try:
            viewport._render_scene(
                painter, QRectF(0, 0, 160, 160), None, False,
                viewport._camera_state(), allow_transparent_background=True)
            self.assertFalse(painter.testRenderHint(
                QPainter.RenderHint.Antialiasing))
            self.assertFalse(painter.testRenderHint(
                QPainter.RenderHint.SmoothPixmapTransform))
        finally:
            painter.end()
            viewport.close()

    def test_profile_bypasses_projective_texture_coefficients(self):
        viewport = _viewport()
        output = QImage(100, 100, QImage.Format.Format_ARGB32)
        output.fill(Qt.GlobalColor.transparent)
        painter = QPainter(output)
        screen = [QPointF(5, 5), QPointF(90, 15), QPointF(15, 90)]
        camera = [(-1.0, 1.0, -1.0), (1.0, 1.0, 1.0),
                  (-1.0, -1.0, 2.0)]
        try:
            with patch(
                    "assembly_viewer.projective_texture_coefficients",
                    side_effect=AssertionError("projective path entered")):
                viewport._draw_textured(
                    painter, screen, [(0, 0), (255, 0), (0, 255)],
                    _checker(), camera_vertices=camera,
                    perspective_correct=False)
        finally:
            painter.end()
            viewport.close()

    def test_profile_threads_affine_policy_through_face_draw(self):
        viewport = _viewport()
        viewport.set_mode(PSX_PROTOTYPE_VIEW_MODE)
        output = QImage(100, 100, QImage.Format.Format_ARGB32)
        painter = QPainter(output)
        try:
            face = viewport._faces[0]
            with patch.object(viewport, "_draw_textured") as draw:
                viewport._draw_face(
                    painter, face,
                    [QPointF(5, 90), QPointF(50, 5), QPointF(95, 90)],
                    mode=PSX_PROTOTYPE_VIEW_MODE,
                    camera_vertices=face.vertices)
            self.assertFalse(draw.call_args.kwargs["perspective_correct"])
        finally:
            painter.end()
            viewport.close()

    def test_degenerate_uv_fallback_can_remain_affine(self):
        viewport = _viewport()
        output = QImage(32, 32, QImage.Format.Format_ARGB32)
        output.fill(Qt.GlobalColor.transparent)
        painter = QPainter(output)
        camera = ((-1.0, 1.0, -1.0), (1.0, 1.0, 1.0),
                  (-1.0, -1.0, 2.0))
        try:
            with patch(
                    "assembly_viewer._triangle_barycentric",
                    return_value=(0.25, 0.25, 0.5)) as barycentric:
                viewport._draw_degenerate_uv_triangle(
                    painter,
                    (QPointF(3, 3), QPointF(28, 4), QPointF(4, 28)),
                    ((0, 0), (0, 0), (0, 0)),
                    _checker(), False, camera,
                    perspective_correct=False)
            self.assertGreater(barycentric.call_count, 0)
        finally:
            painter.end()
            viewport.close()

    def test_snapshot_is_deterministic_and_records_effective_profile(self):
        viewport = _viewport()
        viewport.set_mode(PSX_PROTOTYPE_VIEW_MODE)
        try:
            first = viewport.render_snapshot(QSize(96, 96), QColor("black"))
            first_bytes = bytes(first.constBits())
            first_info = dict(viewport.renderer_info)
            second = viewport.render_snapshot(QSize(96, 96), QColor("black"))
            self.assertEqual(first_bytes, bytes(second.constBits()))
            self.assertEqual(
                first_info["effective_mode"], PSX_PROTOTYPE_PROFILE_ID)
            self.assertEqual(
                viewport.renderer_info["effective_mode"],
                PSX_PROTOTYPE_PROFILE_ID)
        finally:
            viewport.close()

    def test_profile_is_visibly_distinct_and_introduces_no_filtered_colors(self):
        viewport = _viewport()
        size = QSize(96, 96)
        try:
            viewport.set_mode("textured")
            openua = viewport.render_snapshot(size, QColor("black"))
            viewport.set_mode(PSX_PROTOTYPE_VIEW_MODE)
            psx = viewport.render_snapshot(size, QColor("black"))

            self.assertNotEqual(
                bytes(openua.constBits()), bytes(psx.constBits()))
            colors = {
                tuple(psx.pixelColor(x, y).getRgb())
                for y in range(psx.height())
                for x in range(psx.width())
            }
            self.assertEqual(
                colors, {(0, 0, 0, 255), (255, 255, 255, 255)})
        finally:
            viewport.close()

    def test_snapshot_mode_restores_the_psx_profile_exactly(self):
        viewport = _viewport()
        viewport.set_mode(PSX_PROTOTYPE_VIEW_MODE)
        try:
            viewport.begin_snapshot_mode(QColor("black"))
            viewport.set_mode("textured_indexed")
            restored = viewport.end_snapshot_mode()
            self.assertEqual(restored, PSX_PROTOTYPE_VIEW_MODE)
            self.assertEqual(viewport.view_mode, PSX_PROTOTYPE_VIEW_MODE)
            self.assertEqual(
                viewport.renderer_info["effective_mode"], "not_rendered")
        finally:
            viewport.close()

    def test_snapshot_fails_closed_if_profile_does_not_complete(self):
        viewport = _viewport()
        viewport.set_mode(PSX_PROTOTYPE_VIEW_MODE)

        def fallback(*_args, **_kwargs):
            viewport._last_render_requested_mode = PSX_PROTOTYPE_VIEW_MODE
            viewport._last_effective_renderer = "openua_preview_fallback"
            viewport._last_render_fallback_reason = "synthetic failure"

        try:
            with patch.object(viewport, "_render_scene", side_effect=fallback):
                with self.assertRaisesRegex(
                        RuntimeError, "mislabeled fallback: synthetic failure"):
                    viewport.render_snapshot(
                        QSize(32, 32), QColor("black"))
        finally:
            viewport.close()


if __name__ == "__main__":
    unittest.main()
