"""Opt-in regression checks against locally owned Urban Assault game data.

No proprietary asset is part of this test suite.  Set both environment
variables below to run it against a local extraction:

``OPENUA_CANONICAL_PROJECT_ROOT``
    Directory containing ``extracted/taerkasten_canonical`` and
    ``source/metropolis_dawn/SET.BAS``.

``OPENUA_GAME_SET1_ROOT``
    The selected game's ``DATA/SET1`` directory containing PALETTE/REMAP.
"""

from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSize
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QApplication

from assembly_viewer import AssetViewport
from asset_family import load_asset_family
from base_mapping_editor import export_base_object_bytes
from base_parser import parse_base_file


PROJECT_ROOT = os.environ.get("OPENUA_CANONICAL_PROJECT_ROOT", "")
SET1_ROOT = os.environ.get("OPENUA_GAME_SET1_ROOT", "")


@unittest.skipUnless(
    PROJECT_ROOT and SET1_ROOT,
    "local canonical Urban Assault data paths were not supplied",
)
class IndexedCanonicalAssetTests(unittest.TestCase):
    """Frozen local oracles for the directly source-traced indexed path."""

    ORACLES = {
        "VP_TAERO.base": {
            "time_ms": 312.5,
            "sha256": (
                "268e62162c938044c4283a82ac714ac1e9593f3faa807bd0934fc29c7068c4c1"
            ),
            "flat_faces": 3,
            "flat_samples": 16621,
            "flat_changed": 7538,
            "chroma_skipped": 0,
            "transparent_occluded": 22313,
            "unique_indices": 159,
            "replay_polygons": [86, 89, 85],
        },
        "VP_TFLUG.base": {
            "time_ms": 117.1875,
            "sha256": (
                "25d96ad39d0aa8421f791f383618db273df87a6f711362e613a834f9dc7d5b7e"
            ),
            "flat_faces": 2,
            "flat_samples": 21223,
            "flat_changed": 10746,
            "chroma_skipped": 1366,
            "transparent_occluded": 419,
            "unique_indices": 93,
        },
        "VP_ZEPPL.base": {
            "time_ms": 781.25,
            "sha256": (
                "889e1d0db941866f514faf8b4ae0a0ebfd9ae02444d4fdcfea154a5331f1b18a"
            ),
            "flat_faces": 12,
            "flat_samples": 611,
            "flat_changed": 0,
            "chroma_skipped": 27391,
            "transparent_occluded": 3630,
            "unique_indices": 99,
            "propeller_pixels": {
                "39": 1896,
                "40": 1872,
                "41": 143,
                "42": 170,
            },
        },
    }

    HAUPTSTATION_VIEW_ORACLES = {
        "Front": {
            "sha256": "268e62162c938044c4283a82ac714ac1e9593f3faa807bd0934fc29c7068c4c1",
            "flat_faces": 3,
            "flat_samples": 16621,
            "flat_changed": 7538,
            "source_zero": 8826,
            "transparent_occluded": 22313,
            "unique_indices": 159,
            "replay_polygons": [86, 89, 85],
        },
        "Right": {
            "sha256": "b3d26ae62f79856c2446c9f1c279de1936e9fae3142eb5da181e79af7dd7bec6",
            "flat_faces": 4,
            "flat_samples": 10359,
            "flat_changed": 4406,
            "source_zero": 5953,
            "transparent_occluded": 13839,
            "unique_indices": 169,
            "replay_polygons": [85, 84, 88, 86],
        },
        "Top": {
            "sha256": "4b5b27a83aabf17e407f5451bd99c4b64c1b842ab75bf521668df85297322c43",
            "flat_faces": 5,
            "flat_samples": 133,
            "flat_changed": 47,
            "source_zero": 86,
            "transparent_occluded": 0,
            "unique_indices": 138,
            "replay_polygons": [89, 88, 86, 85, 84],
        },
        "Back": {
            "sha256": "6db2b17834e4ede7a008ffcc5c60a88e1886ea156006b9a89f115911375c5ccf",
            "flat_faces": 3,
            "flat_samples": 16334,
            "flat_changed": 7446,
            "source_zero": 8701,
            "transparent_occluded": 22086,
            "unique_indices": 173,
            "replay_polygons": [87, 88, 84],
        },
    }

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.project = Path(PROJECT_ROOT).resolve()
        cls.set1 = Path(SET1_ROOT).resolve()
        cls.assets = cls.project / "extracted" / "taerkasten_canonical"
        cls.setbas = (
            cls.project / "source" / "metropolis_dawn" / "SET.BAS"
        )

    def test_front_views_match_source_traced_index_buffer_oracles(self):
        size = QSize(512, 512)
        for filename, expected in self.ORACLES.items():
            with self.subTest(asset=filename):
                family = load_asset_family(
                    self.assets / filename,
                    [self.set1],
                    {
                        "STANDARD.PAL": (
                            self.set1 / "PALETTE" / "Standard.pal")
                    },
                    setbas=self.setbas,
                )
                viewport = AssetViewport()
                viewport.load_family(family)
                self.assertTrue(
                    viewport.indexed_rendering_available,
                    viewport.indexed_rendering_reason,
                )
                viewport.set_mode("textured_indexed")
                viewport.begin_snapshot_mode(QColor("#000000"))
                viewport.set_animation_time_ms(expected["time_ms"])
                viewport.apply_view_preset("Front", size, 88)
                image = viewport.render_snapshot(
                    size, QColor("#000000"), include_guides=False)
                self.assertFalse(image.isNull())

                renderer_info = viewport.indexed_renderer_info
                self.assertTrue(
                    renderer_info["canonical_retail_configuration"])
                self.assertEqual(
                    renderer_info["flat_tracy_destination_mode"],
                    "live_framebuffer")
                self.assertIsNone(
                    renderer_info["flat_tracy_forced_destination_index"])
                stats = renderer_info["last_render_stats"]
                self.assertEqual(stats["initial_framebuffer_index"], 0)
                self.assertEqual(
                    stats["index_buffer_sha256"], expected["sha256"])
                self.assertEqual(
                    stats["flat_tracy_source_face_count"],
                    expected["flat_faces"],
                )
                self.assertEqual(
                    stats["flat_tracy_samples"], expected["flat_samples"])
                self.assertEqual(
                    stats["flat_tracy_changed_samples"],
                    expected["flat_changed"],
                )
                self.assertEqual(
                    stats["source_chroma_skipped"],
                    expected["chroma_skipped"],
                )
                self.assertEqual(
                    stats["unique_framebuffer_indices"],
                    expected["unique_indices"],
                )
                self.assertEqual(
                    stats["transparent_samples_occluded"],
                    expected["transparent_occluded"],
                )
                if "replay_polygons" in expected:
                    self.assertEqual(
                        [entry["polygon_id"] for entry in
                         stats["transparent_replay_faces"]],
                        expected["replay_polygons"],
                    )
                for polygon_id, pixels in expected.get(
                        "propeller_pixels", {}).items():
                    self.assertEqual(
                        stats["final_visible_polygon_owners"][polygon_id][
                            "pixels"
                        ],
                        pixels,
                    )

    def test_hauptstation_forced_row_13_is_explicitly_diagnostic(self):
        family = load_asset_family(
            self.assets / "VP_TAERO.base",
            [self.set1],
            {"STANDARD.PAL": self.set1 / "PALETTE" / "Standard.pal"},
            setbas=self.setbas,
        )
        size = QSize(512, 512)
        viewport = AssetViewport()
        viewport.load_family(family)
        viewport.set_mode("textured_indexed")
        viewport.begin_snapshot_mode(QColor("#000000"))
        viewport.set_flat_tracy_forced_destination_index(13)
        viewport.set_flat_tracy_destination_mode("forced_diagnostic")
        viewport.set_animation_time_ms(312.5)
        viewport.apply_view_preset("Front", size, 88)

        image = viewport.render_snapshot(
            size, QColor("#000000"), include_guides=False)
        info = viewport.indexed_renderer_info
        stats = info["last_render_stats"]

        self.assertFalse(image.isNull())
        self.assertEqual(
            info["effective_mode"],
            "retail_indexed_forced_tracy_diagnostic")
        self.assertFalse(info["canonical_retail_configuration"])
        self.assertEqual(info["flat_tracy_forced_destination_index"], 13)
        self.assertEqual(
            stats["index_buffer_sha256"],
            "2497104ff6f77a839d9dbc560db8e20fae25b54b1fae4559529b44cd4408d022",
        )
        self.assertEqual(stats["flat_tracy_changed_samples"], 8141)
        self.assertEqual(stats["unique_framebuffer_indices"], 153)

    def test_myko_host_source_atts_only_matches_runtime_submission(self):
        """The retail AMESH loop does not submit its orphan POL2 #36."""

        archive = parse_base_file(self.setbas)
        candidates = [
            item for item in archive.all_objects()
            if str(item.name or "").casefold() == "vp_brgro"
        ]
        self.assertEqual(len(candidates), 1)
        source_bytes = self.setbas.read_bytes()
        exported = export_base_object_bytes(source_bytes, candidates[0])

        with tempfile.TemporaryDirectory(prefix="ua_brgro_oracle_") as temp:
            base_path = Path(temp) / "VP_BRGRO.base"
            base_path.write_bytes(exported)
            family = load_asset_family(
                base_path,
                [self.set1],
                {"STANDARD.PAL": self.set1 / "PALETTE" / "Standard.pal"},
                setbas=self.setbas,
            )
            viewport = AssetViewport()
            viewport.load_family(family)
            self.assertEqual(
                viewport._unmapped_polygon_inventory(),
                [{"owner": "root", "polygon_id": 36}],
            )
            viewport.set_mode("textured_indexed")
            viewport.begin_snapshot_mode(QColor("#000000"))
            viewport.set_retail_unmapped_polygon_policy("source_atts_only")
            viewport.set_animation_time_ms(0.0)
            viewport.apply_view_preset("Front", QSize(512, 512), 88)
            image = viewport.render_snapshot(
                QSize(512, 512), QColor("#000000"), include_guides=False)

        self.assertFalse(image.isNull())
        info = viewport.indexed_renderer_info
        self.assertEqual(
            info["effective_unmapped_polygon_policy"], "source_atts_only")
        self.assertEqual(
            info["unmapped_source_polygon_identities"],
            [{"owner": "root", "polygon_id": 36}],
        )
        stats = info["last_render_stats"]
        self.assertEqual(stats["unmapped_source_polygons_omitted"], 1)
        self.assertEqual(
            stats["index_buffer_sha256"],
            "1b6af36ad0234f9f26c521b991bf862208092bc10cb1f2c26946d2c7a90f19c6",
        )
        self.assertEqual(stats["covered_pixels"], 83555)
        self.assertEqual(stats["unique_framebuffer_indices"], 176)
        self.assertEqual(stats["flat_tracy_source_face_count"], 7)
        self.assertEqual(stats["flat_tracy_samples"], 55777)
        self.assertEqual(stats["flat_tracy_changed_samples"], 38365)
        self.assertEqual(stats["transparent_samples_occluded"], 50103)

    def test_hauptstation_top_retail_gameplay_distance_fade_oracle(self):
        """Lock the real viewer-to-BSP-to-indexed fade channel path."""

        family = load_asset_family(
            self.assets / "VP_TAERO.base",
            [self.set1],
            {"STANDARD.PAL": self.set1 / "PALETTE" / "Standard.pal"},
            setbas=self.setbas,
        )
        size = QSize(512, 512)
        viewport = AssetViewport()
        viewport.load_family(family)
        viewport.set_mode("textured_indexed")
        viewport.set_retail_area_distance_fade_enabled(True)
        viewport.begin_snapshot_mode(QColor("#000000"))
        viewport.set_animation_time_ms(312.5)
        viewport.apply_view_preset("Top", size, 88)

        image = viewport.render_snapshot(
            size, QColor("#000000"), include_guides=False)
        info = viewport.indexed_renderer_info
        stats = info["last_render_stats"]

        self.assertFalse(image.isNull())
        self.assertTrue(info["canonical_retail_destination_policy"])
        self.assertEqual(
            info["distance_fade_profile_state"],
            "retail_gameplay_near_1400_600")
        self.assertEqual(
            stats["index_buffer_sha256"],
            "4a1f60bf81cb28621e3831f461085dbf8ba36b66c47fdd532c8ea5e0fad56c4f",
        )
        self.assertEqual(stats["distance_fade_vertex_channel_piece_count"], 85)
        self.assertEqual(stats["distance_fade_authored_flagged_piece_count"], 66)
        self.assertEqual(stats["distance_fade_dynamic_piece_count"], 50)
        self.assertEqual(stats["distance_fade_bypass_piece_count"], 16)
        self.assertEqual(stats["distance_fade_black_piece_count"], 0)
        self.assertEqual(stats["distance_fade_dynamic_changed_samples"], 2974)
        self.assertEqual(stats["covered_pixels"], 127991)
        self.assertEqual(stats["unique_framebuffer_indices"], 145)

    def test_hauptstation_multiview_lightning_oracles(self):
        family = load_asset_family(
            self.assets / "VP_TAERO.base",
            [self.set1],
            {"STANDARD.PAL": self.set1 / "PALETTE" / "Standard.pal"},
            setbas=self.setbas,
        )
        size = QSize(512, 512)
        for preset, expected in self.HAUPTSTATION_VIEW_ORACLES.items():
            with self.subTest(view=preset):
                viewport = AssetViewport()
                viewport.load_family(family)
                viewport.set_mode("textured_indexed")
                viewport.begin_snapshot_mode(QColor("#000000"))
                viewport.set_animation_time_ms(312.5)
                viewport.apply_view_preset(preset, size, 88)
                image = viewport.render_snapshot(
                    size, QColor("#000000"), include_guides=False)
                self.assertFalse(image.isNull())
                stats = viewport.indexed_renderer_info["last_render_stats"]

                self.assertEqual(
                    stats["index_buffer_sha256"], expected["sha256"])
                self.assertEqual(
                    stats["flat_tracy_source_face_count"],
                    expected["flat_faces"])
                self.assertEqual(
                    stats["flat_tracy_samples"], expected["flat_samples"])
                self.assertEqual(
                    stats["flat_tracy_changed_samples"],
                    expected["flat_changed"])
                self.assertEqual(
                    stats["flat_tracy_source_zero_samples"],
                    expected["source_zero"])
                self.assertEqual(
                    stats["transparent_samples_occluded"],
                    expected["transparent_occluded"])
                self.assertEqual(
                    stats["unique_framebuffer_indices"],
                    expected["unique_indices"])
                self.assertEqual(
                    [entry["polygon_id"] for entry in
                     stats["transparent_replay_faces"]],
                    expected["replay_polygons"])


if __name__ == "__main__":
    unittest.main()
