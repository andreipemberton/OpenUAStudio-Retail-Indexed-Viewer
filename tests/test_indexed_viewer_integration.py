from __future__ import annotations

import os
from types import SimpleNamespace
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSize
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QApplication

from assembly_viewer import (
    AssetViewport,
    ViewFace,
    ViewMaterial,
    _cardinal_sin_cos,
    _retail_area_distance_fade_vertex,
    _retail_source_face_front_facing,
)
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


def _destination_tables() -> IndexedTables:
    palette = tuple((index, index, index) for index in range(256))
    shader = bytes(range(256)) * 256
    tracy = bytearray(b"".join(
        bytes((background,)) * 256 for background in range(256)))
    tracy[0 * 256 + 5] = 20
    tracy[13 * 256 + 5] = 70
    return IndexedTables(palette, shader, bytes(tracy))


def _viewport(mapped: bool = True, uvs=None) -> AssetViewport:
    viewport = AssetViewport()
    viewport._backface_cull = False
    viewport._materials = [ViewMaterial("TEST.ILBM")]
    viewport._faces = [ViewFace(
        # Front-facing retail winding. Exact mode enforces whole-face culling
        # even when the diagnostic preview toggle is disabled.
        vertices=[(-1.0, -1.0, 0.0), (0.0, 1.0, 0.0), (1.0, -1.0, 0.0)],
        uvs=list(uvs if uvs is not None else [(0, 0), (128, 255), (255, 0)]),
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

    def test_default_and_forced_destination_metadata_are_unambiguous(self):
        viewport = _viewport()
        tables = _destination_tables()
        surface = IndexedSurface(
            "FX2.ILBM", "solid", None, 0, 0, None, 5,
            "none", 0, "flat", "linear")
        viewport._indexed_adapter = SimpleNamespace(
            source_info={}, tables=tables,
            resolve_surface=lambda *_args: surface)

        live_image = viewport.render_snapshot(QSize(32, 32), None)
        live_info = viewport.indexed_renderer_info
        self.assertEqual(
            live_info["effective_mode"], "retail_indexed_reconstructed")
        self.assertEqual(
            live_info["flat_tracy_destination_mode"], "live_framebuffer")
        self.assertIsNone(
            live_info["flat_tracy_forced_destination_index"])
        self.assertTrue(live_info["canonical_retail_configuration"])
        self.assertTrue(live_info["canonical_retail_destination_policy"])
        self.assertIn(
            "distance profile",
            live_info["canonical_retail_configuration_scope"])
        self.assertEqual(
            live_info["distance_fade_profile_state"],
            "disabled_closeup_compatibility")
        self.assertEqual(
            live_info["last_render_stats"]["initial_framebuffer_index"], 0)

        viewport.set_flat_tracy_forced_destination_index(13)
        viewport.set_flat_tracy_destination_mode("forced_diagnostic")
        self.assertEqual(viewport.indexed_renderer_info["last_render_stats"], {})
        forced_image = viewport.render_snapshot(QSize(32, 32), None)
        forced_info = viewport.indexed_renderer_info

        self.assertEqual(
            forced_info["effective_mode"],
            "retail_indexed_forced_tracy_diagnostic")
        self.assertEqual(
            forced_info["flat_tracy_forced_destination_index"], 13)
        self.assertEqual(
            forced_info["flat_tracy_forced_destination_rgb"], [13, 13, 13])
        self.assertFalse(forced_info["canonical_retail_configuration"])
        self.assertFalse(forced_info["canonical_retail_destination_policy"])
        self.assertEqual(
            forced_info["last_render_stats"][
                "flat_tracy_forced_destination_index"], 13)
        self.assertNotEqual(
            live_info["last_render_stats"]["index_buffer_sha256"],
            forced_info["last_render_stats"]["index_buffer_sha256"])
        self.assertNotEqual(
            live_image.pixelColor(16, 16), forced_image.pixelColor(16, 16))

    def test_destination_setters_validate_and_snapshot_restores_policy(self):
        viewport = _viewport()
        viewport.set_flat_tracy_forced_destination_index(31)
        viewport.begin_snapshot_mode(None)
        viewport.set_flat_tracy_destination_mode("forced_diagnostic")
        viewport.set_flat_tracy_forced_destination_index(220)
        viewport._last_indexed_stats = {"index_buffer_sha256": "diagnostic"}

        restored = viewport.end_snapshot_mode()

        self.assertEqual(restored, "textured_indexed")
        self.assertEqual(
            viewport.flat_tracy_destination_mode, "live_framebuffer")
        self.assertEqual(viewport.flat_tracy_destination_index, 31)
        self.assertEqual(viewport.indexed_renderer_info["last_render_stats"], {})
        with self.assertRaises(ValueError):
            viewport.set_flat_tracy_destination_mode("pretend_retail")
        for value in (-1, 256):
            with self.subTest(value=value), self.assertRaises(ValueError):
                viewport.set_flat_tracy_forced_destination_index(value)
        for value in (True, 1.5, "13"):
            with self.subTest(value=value), self.assertRaises(TypeError):
                viewport.set_flat_tracy_forced_destination_index(value)

    def test_distance_fade_toggle_is_opt_in_and_snapshot_restores_it(self):
        viewport = _viewport()
        self.assertFalse(viewport.retail_area_distance_fade_enabled)
        self.assertFalse(
            viewport.indexed_renderer_info[
                "requested_distance_fade_enabled"])

        viewport.set_retail_area_distance_fade_enabled(True)
        info = viewport.indexed_renderer_info
        self.assertTrue(info["requested_distance_fade_enabled"])
        self.assertFalse(info["effective_distance_fade_enabled"])
        self.assertEqual(
            info["distance_fade_profile_id"],
            "retail_gameplay_near_1400_600")
        self.assertEqual(
            info["distance_fade_profile_state"],
            "retail_gameplay_near_1400_600")
        self.assertEqual(info["distance_fade_start"], 800.0)
        self.assertEqual(info["distance_fade_length"], 600.0)

        viewport.begin_snapshot_mode(None)
        viewport.set_distance_fade_enabled(False)
        self.assertFalse(viewport.distance_fade_enabled)
        viewport.end_snapshot_mode()
        self.assertTrue(viewport.distance_fade_enabled)
        self.assertEqual(viewport.indexed_renderer_info["last_render_stats"], {})

        with self.assertRaises(TypeError):
            viewport.set_distance_fade_enabled(1)

    def test_viewport_passes_retail_distance_profile_to_indexed_renderer(self):
        viewport = _viewport()
        surface = IndexedSurface(
            "SHADED.ILBM", "texture", bytes((9,)), 1, 1,
            tuple(False for _ in range(256)), None,
            "gradient", 1, "none", "linear",
            authored_shade_value=0,
            authored_depth_fade_flag=True,
            distance_fade_eligible=True,
        )
        viewport._indexed_adapter = SimpleNamespace(
            source_info={}, tables=_tables(),
            resolve_surface=lambda *_args: surface)
        viewport.set_distance_fade_enabled(True)

        image = viewport.render_snapshot(QSize(32, 32), None)
        info = viewport.indexed_renderer_info

        self.assertFalse(image.isNull())
        self.assertTrue(info["requested_distance_fade_enabled"])
        self.assertTrue(info["effective_distance_fade_enabled"])
        stats = info["last_render_stats"]
        self.assertTrue(stats["distance_fade_requested"])
        self.assertTrue(stats["distance_fade_effective"])
        self.assertEqual(
            stats["distance_fade_profile"]["name"],
            "retail_gameplay_near_1400_600")
        self.assertEqual(
            stats["distance_fade_profile"]["source_distance_scale"],
            1.0 / viewport._scale)
        self.assertGreater(
            stats["distance_fade_vertex_channel_piece_count"], 0)

    def test_retail_distance_fade_uses_radial_source_units_not_zoom(self):
        expected = (
            (799.0, 0.0),
            (800.0, 0.0),
            (1100.0, 0.5),
            (1400.0, 1.0),
        )
        for distance, expected_brightness in expected:
            with self.subTest(distance=distance):
                brightness, _fixed = _retail_area_distance_fade_vertex(
                    (0.0, 0.0, 4.0 - distance), 0, 1.0)
                self.assertAlmostEqual(
                    brightness, expected_brightness, places=12)

                # The same source distance under a different auto-fit scale
                # must retain the same brightness. Lens zoom and pan are not
                # inputs to this helper at all.
                scaled, _scaled_fixed = _retail_area_distance_fade_vertex(
                    (0.0, 0.0, 4.0 - distance * 0.25), 0, 0.25)
                self.assertAlmostEqual(scaled, brightness, places=12)
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

    def test_exact_mode_culls_reverse_winding_pair_even_when_toggle_is_off(self):
        viewport = _viewport()
        viewport._faces.append(ViewFace(
            vertices=[
                (-1.0, -1.0, 0.0),
                (1.0, -1.0, 0.0),
                (0.0, 1.0, 0.0),
            ],
            uvs=[(0, 0), (255, 0), (128, 255)],
            material=0,
            poly_id=8,
            mapped=True,
            owner="root",
            block_index=0,
        ))
        surface = IndexedSurface(
            "solid", "solid", None, 0, 0, None, 23,
            "none", 0, "none", "linear")
        resolved = []

        def resolve(face, *_args):
            resolved.append(face.poly_id)
            return surface

        viewport._indexed_adapter = SimpleNamespace(
            source_info={}, tables=_tables(), resolve_surface=resolve)
        image = viewport.render_snapshot(QSize(32, 32), None)

        self.assertFalse(image.isNull())
        self.assertEqual(resolved, [7])
        self.assertEqual(
            viewport.indexed_renderer_info["last_render_stats"][
                "ordered_piece_count"],
            1,
        )

    def test_retail_cull_is_3d_preclip_not_near_plane_projection_clamp(self):
        camera_vertices = [
            (-0.16202796, -1.97493340, 3.61461335),
            (-1.48634709, 1.25880337, 3.78281788),
            (-0.81305745, 0.57071037, 3.98094668),
        ]
        self.assertTrue(
            _retail_source_face_front_facing(camera_vertices))
        self.assertFalse(
            _retail_source_face_front_facing([
                camera_vertices[0], camera_vertices[2], camera_vertices[1]
            ]))

    def test_cardinal_camera_trig_has_no_edge_on_residue(self):
        self.assertEqual(_cardinal_sin_cos(0.0), (0.0, 1.0))
        self.assertEqual(_cardinal_sin_cos(90.0), (1.0, 0.0))
        self.assertEqual(_cardinal_sin_cos(-90.0), (-1.0, 0.0))
        self.assertEqual(_cardinal_sin_cos(180.0), (0.0, -1.0))

    def test_exact_mode_culls_whole_twisted_face_from_first_three_vertices(self):
        viewport = _viewport()
        viewport._faces = [ViewFace(
            # Fan (0,2,1) is back-facing while fan (0,3,2) is front-facing.
            # Retail rejects the whole source polygon from vertices 0/1/2
            # before any fan triangle can resurrect it.
            vertices=[
                (0.0, 0.0, 0.0),
                (1.0, 0.0, 0.0),
                (0.0, 1.0, 0.0),
                (1.0, 1.0, 0.0),
            ],
            uvs=[(0, 0), (255, 0), (0, 255), (255, 255)],
            material=0,
            poly_id=19,
            mapped=True,
            owner="root",
            block_index=0,
        )]
        surface = IndexedSurface(
            "solid", "solid", None, 0, 0, None, 23,
            "none", 0, "none", "linear")
        resolved = []

        def resolve(face, *_args):
            resolved.append(face.poly_id)
            return surface

        viewport._indexed_adapter = SimpleNamespace(
            source_info={}, tables=_tables(), resolve_surface=resolve)
        image = viewport.render_snapshot(QSize(32, 32), None)

        self.assertFalse(image.isNull())
        self.assertEqual(resolved, [])
        self.assertEqual(
            viewport.indexed_renderer_info["last_render_stats"][
                "ordered_piece_count"],
            0,
        )

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

    def test_explicit_source_atts_policy_omits_before_bsp_with_provenance(self):
        viewport = _viewport(mapped=False)
        resolved = []
        surface = IndexedSurface(
            "solid", "solid", None, 0, 0, None, 23,
            "none", 0, "none", "linear")

        def resolve(face, *_args):
            resolved.append(face.poly_id)
            return surface

        viewport._indexed_adapter = SimpleNamespace(
            source_info={}, tables=_tables(), resolve_surface=resolve)
        viewport.set_retail_unmapped_polygon_policy("source_atts_only")

        with patch("assembly_viewer.order_camera_polygons", wraps=lambda x: x) \
                as order:
            image = viewport.render_snapshot(QSize(32, 32), None)

        self.assertFalse(image.isNull())
        self.assertEqual(order.call_args.args[0], [])
        self.assertEqual(resolved, [])
        info = viewport.indexed_renderer_info
        self.assertEqual(
            info["effective_unmapped_polygon_policy"], "source_atts_only")
        self.assertEqual(info["unmapped_source_polygon_count"], 1)
        self.assertEqual(
            info["unmapped_source_polygon_identities"],
            [{"owner": "root", "polygon_id": 7}],
        )
        stats = info["last_render_stats"]
        self.assertEqual(stats["ordered_piece_count"], 0)
        self.assertEqual(stats["unmapped_source_polygons_omitted"], 1)
        self.assertEqual(
            stats["unmapped_source_polygon_identities"],
            [{"owner": "root", "polygon_id": 7}],
        )

    def test_unmapped_policy_validation_invalidation_and_snapshot_restore(self):
        viewport = _viewport(mapped=False)
        self.assertEqual(
            viewport.retail_unmapped_polygon_policy, "fail_closed")
        with self.assertRaisesRegex(ValueError, "fail_closed"):
            viewport.set_retail_unmapped_polygon_policy("invent_material")

        viewport._last_indexed_stats = {"index_buffer_sha256": "stale"}
        viewport.begin_snapshot_mode(None)
        viewport.set_retail_unmapped_polygon_policy("source_atts_only")
        self.assertEqual(viewport.indexed_renderer_info["last_render_stats"], {})
        restored = viewport.end_snapshot_mode()

        self.assertEqual(restored, "textured_indexed")
        self.assertEqual(
            viewport.retail_unmapped_polygon_policy, "fail_closed")

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

    def test_animation_frame_changes_invalidate_prior_render_metadata(self):
        viewport = AssetViewport()
        viewport._materials = [ViewMaterial(
            "ANIM.ANM",
            anim_frames=[(1, 0, 0), (1, 0, 0)],
            anim_type=0,
        )]
        viewport._reset_animation_states()
        viewport._last_effective_renderer = "retail_indexed_reconstructed"
        viewport._last_indexed_stats = {
            "index_buffer_sha256": "frame-one",
        }

        viewport.step_animation()

        self.assertEqual(viewport._anim_states[0], (1, 1))
        self.assertEqual(viewport.indexed_renderer_info["last_render_stats"], {})
        self.assertEqual(
            viewport.indexed_renderer_info["effective_mode"], "not_rendered")

        viewport._anim_states[0] = (1, 1)
        viewport._last_effective_renderer = "retail_indexed_reconstructed"
        viewport._last_indexed_stats = {
            "index_buffer_sha256": "frame-two",
        }
        viewport.stop_animation()
        self.assertEqual(viewport._anim_states[0], (0, 1))
        self.assertEqual(viewport.indexed_renderer_info["last_render_stats"], {})

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
