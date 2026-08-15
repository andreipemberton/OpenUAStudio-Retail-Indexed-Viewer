from __future__ import annotations

import os
from types import SimpleNamespace
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSize
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QApplication

from assembly_viewer import AssetViewport, ViewFace, ViewMaterial
from indexed_renderer import (
    IndexedRasterizer,
    IndexedSurface,
    IndexedTables,
)
from snapshot_studio.batch_export import VPSnapshotBatchPanel


def _tables() -> IndexedTables:
    palette = tuple((index, index, index) for index in range(256))
    identity = bytes(range(256)) * 256
    return IndexedTables(palette, identity, identity)


def _viewport(mapped: bool = True, uvs=None) -> AssetViewport:
    viewport = AssetViewport()
    viewport._backface_cull = False
    viewport._materials = [ViewMaterial("TEST.ILBM")]
    viewport._faces = [ViewFace(
        vertices=[(-1.0, -1.0, 0.0), (1.0, -1.0, 0.0), (0.0, 1.0, 0.0)],
        uvs=list(uvs if uvs is not None else [(0, 0), (255, 0), (128, 255)]),
        material=0,
        poly_id=7,
        mapped=mapped,
        owner="root",
        block_index=0,
    )]
    viewport.set_mode("textured_indexed")
    return viewport


class IndexedViewerIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_explicit_indexed_snapshot_fails_closed_when_resources_are_absent(self):
        viewport = _viewport(mapped=False)
        viewport._indexed_adapter = None
        viewport._indexed_unavailable_reason = "synthetic missing remaps"

        with self.assertRaisesRegex(
                RuntimeError, "aborted instead of exporting an OpenUA fallback"):
            viewport.render_snapshot(QSize(32, 32), QColor("black"))

        info = viewport.indexed_renderer_info
        self.assertFalse(info["available"])
        self.assertEqual(info["effective_mode"], "openua_preview_fallback")
        self.assertTrue(info["fallback_used"])
        self.assertIn("missing remaps", info["fallback_reason"])

    def test_runtime_material_failure_is_reported_as_effective_fallback(self):
        viewport = _viewport()

        class BrokenAdapter:
            source_info = {}

            @staticmethod
            def resolve_surface(*_args):
                raise RuntimeError("forced indexed material failure")

        viewport._indexed_adapter = BrokenAdapter()

        with self.assertRaisesRegex(RuntimeError, "forced indexed material failure"):
            viewport.render_snapshot(QSize(32, 32), None)

        info = viewport.indexed_renderer_info
        self.assertTrue(info["resources_available"])
        self.assertFalse(info["available"])
        self.assertTrue(info["fallback_used"])
        self.assertEqual(info["effective_mode"], "openua_preview_fallback")

    def test_invalid_texture_uv_count_cannot_become_zero_uv_indexed_mapping(self):
        viewport = _viewport(uvs=[])
        surface = IndexedSurface(
            "TEST.ILBM", "texture", bytes((1,)), 1, 1,
            tuple(False for _ in range(256)), None,
            "none", 0, "none", "linear",
        )
        viewport._indexed_adapter = SimpleNamespace(
            source_info={},
            tables=_tables(),
            resolve_surface=lambda *_args: surface,
        )

        with self.assertRaisesRegex(RuntimeError, "mapping is incomplete"):
            viewport.render_snapshot(QSize(32, 32), None)

    def test_unmapped_polygon_cannot_be_silently_omitted_from_exact_export(self):
        viewport = _viewport(mapped=False)
        viewport._indexed_adapter = SimpleNamespace(
            source_info={},
            tables=_tables(),
            resolve_surface=lambda *_args: None,
        )

        with self.assertRaisesRegex(RuntimeError, "complete ATTS material mappings"):
            viewport.render_snapshot(QSize(32, 32), None)

        info = viewport.indexed_renderer_info
        self.assertTrue(info["fallback_used"])
        self.assertIn("unmapped polygon", info["fallback_reason"])

    def test_secondary_object_keeps_unmapped_geometry_for_exact_audit(self):
        viewport = AssetViewport()
        skeleton = SimpleNamespace(
            points=[
                (-1.0, -1.0, 0.0),
                (1.0, -1.0, 0.0),
                (0.0, 1.0, 0.0),
                (0.0, -0.2, 0.0),
            ],
            polygons=[[0, 1, 2], [0, 3, 2]],
            sensors=[],
        )
        group = SimpleNamespace(
            faces=[(0, [(0, 0), (255, 0), (128, 255)], 0)],
            block=None,
            texture_name="",
            kind="",
            label="secondary material",
        )
        family_object = SimpleNamespace(
            skeleton=skeleton,
            base_object=SimpleNamespace(transform=None),
            materials=[group],
        )
        family = SimpleNamespace(
            textures={},
            effect_override_paths={},
            external_palette=None,
            animations={},
        )

        viewport._load_object(
            family_object,
            family,
            {},
            primary=False,
            owner="root/child",
            build_faces=True,
        )

        self.assertEqual(
            sorted(
                (face.poly_id, face.mapped, face.primary)
                for face in viewport._faces
            ),
            [(0, True, False), (1, False, False)],
        )

    def test_oversize_preflight_occurs_before_index_buffer_allocation(self):
        viewport = _viewport()
        viewport._indexed_adapter = SimpleNamespace(source_info={})
        side = int(IndexedRasterizer.max_safe_pixels ** 0.5) + 1
        viewport._last_effective_renderer = "retail_indexed_reconstructed"
        viewport._last_indexed_stats = {
            "index_buffer_sha256": "stale-success",
        }

        with self.assertRaisesRegex(RuntimeError, "safety budget"):
            viewport.render_snapshot(QSize(side, side), None)

        info = viewport.indexed_renderer_info
        self.assertEqual(info["effective_mode"], "not_rendered")
        self.assertEqual(info["last_render_stats"], {})
        self.assertIn("safety budget", info["fallback_reason"])

    def test_batch_row_metadata_distinguishes_exact_fallback_and_existing(self):
        panel = SimpleNamespace(_renderer_mode="textured_indexed")
        exact = VPSnapshotBatchPanel._renderer_manifest_fields(
            panel,
            {
                "effective_mode": "retail_indexed_reconstructed",
                "fallback_used": False,
                "fallback_reason": "",
                "sources": {
                    "palette": {"sha256": "palette"},
                    "shader": {"sha256": "shader"},
                    "tracy": {"sha256": "tracy"},
                },
                "last_render_stats": {"index_buffer_sha256": "frame"},
            },
        )
        fallback = VPSnapshotBatchPanel._renderer_manifest_fields(
            panel,
            {
                "effective_mode": "openua_preview_fallback",
                "fallback_used": True,
                "fallback_reason": "unsupported mapped TRACY",
            },
        )
        existing = VPSnapshotBatchPanel._renderer_manifest_fields(
            panel, None, existing_unverified=True)

        self.assertEqual(
            exact["effective_renderer"], "retail_indexed_reconstructed")
        self.assertEqual(exact["index_buffer_sha256"], "frame")
        self.assertTrue(fallback["fallback_used"])
        self.assertIn("mapped TRACY", fallback["fallback_reason"])
        self.assertEqual(
            existing["effective_renderer"], "existing_file_not_verified")

    def test_switching_to_openua_clears_prior_index_buffer_metadata(self):
        viewport = _viewport()
        viewport._last_indexed_stats = {
            "index_buffer_sha256": "stale-indexed-frame",
        }

        viewport.set_mode("textured")

        self.assertEqual(
            viewport.indexed_renderer_info["last_render_stats"], {})

    def test_leaving_snapshot_mode_clears_temporary_renderer_metadata(self):
        viewport = _viewport()
        viewport.set_mode("textured")
        viewport.begin_snapshot_mode(QColor("black"))
        viewport.set_mode("textured_indexed")
        viewport._last_render_requested_mode = "textured_indexed"
        viewport._last_effective_renderer = "retail_indexed_reconstructed"
        viewport._last_indexed_stats = {
            "index_buffer_sha256": "temporary-snapshot",
        }

        restored = viewport.end_snapshot_mode()

        self.assertEqual(restored, "textured")
        info = viewport.indexed_renderer_info
        self.assertEqual(info["requested_mode"], "textured")
        self.assertEqual(info["effective_mode"], "not_rendered")
        self.assertEqual(info["last_render_stats"], {})

    def test_large_loop_animation_time_uses_cycle_modulo(self):
        viewport = AssetViewport()
        viewport._materials = [ViewMaterial(
            "FAST.ANM",
            anim_frames=[(1, 0, 0), (2, 0, 0), (3, 0, 0)],
            anim_type=0,
        )]
        # 1,000,000 authored ticks modulo the six-tick cycle is four:
        # frame 0 consumes one, frame 1 consumes two, then one tick remains
        # inside frame 2.
        viewport.set_animation_time_ms(1_000_000 * 1000.0 / 1024.0)

        self.assertEqual(viewport._anim_states[0], (2, 1))
        self.assertAlmostEqual(
            viewport._anim_left_ms[0], 1000.0 / 1024.0, places=6)

    def test_large_pingpong_animation_preserves_endpoint_holds(self):
        viewport = AssetViewport()
        viewport._materials = [ViewMaterial(
            "PING.ANM",
            anim_frames=[(1, 0, 0), (1, 0, 0)],
            anim_type=1,
        )]
        # The engine-compatible sequence is 0+, 1+, 1-, 0-.  A remainder
        # of 2.5 ticks must therefore be halfway through the held 1- state.
        elapsed_ticks = 4 * 250_000 + 2.5
        viewport.set_animation_time_ms(elapsed_ticks * 1000.0 / 1024.0)

        self.assertEqual(viewport._anim_states[0], (1, -1))
        self.assertAlmostEqual(
            viewport._anim_left_ms[0], 0.5 * 1000.0 / 1024.0,
            places=6)


if __name__ == "__main__":
    unittest.main()
