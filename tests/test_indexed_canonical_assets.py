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
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSize
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QApplication

from assembly_viewer import AssetViewport
from asset_family import load_asset_family


PROJECT_ROOT = os.environ.get("OPENUA_CANONICAL_PROJECT_ROOT", "")
SET1_ROOT = os.environ.get("OPENUA_GAME_SET1_ROOT", "")


@unittest.skipUnless(
    PROJECT_ROOT and SET1_ROOT,
    "local canonical Urban Assault data paths were not supplied",
)
class IndexedCanonicalAssetTests(unittest.TestCase):
    """Exact index-buffer oracles produced by the audited source exporter."""

    ORACLES = {
        "VP_TAERO.base": {
            "time_ms": 312.5,
            "sha256": (
                "f73686e91463f71368760ddb4c2117b240d81a91d901b0ef5e145aca1ac427ed"
            ),
            "flat_faces": 3,
            "flat_samples": 12298,
            "flat_changed": 12153,
            "chroma_skipped": 26636,
            "unique_indices": 167,
        },
        "VP_TFLUG.base": {
            "time_ms": 117.1875,
            "sha256": (
                "b20c86538394aa5cfb7285a7dd3f92cff7e507447f8050c6de0c4f9f7856af7c"
            ),
            "flat_faces": 2,
            "flat_samples": 10746,
            "flat_changed": 10746,
            "chroma_skipped": 11843,
            "unique_indices": 120,
        },
        "VP_ZEPPL.base": {
            "time_ms": 781.25,
            "sha256": (
                "98f463e6fa42c800dd91705cb1a36f28fb149f11a31356e20826eb9f6bf85507"
            ),
            "flat_faces": 12,
            "flat_samples": 0,
            "flat_changed": 0,
            "chroma_skipped": 28278,
            "unique_indices": 94,
            "propeller_pixels": {
                "39": 1896,
                "40": 1872,
                "41": 143,
                "42": 170,
            },
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

    def test_front_views_match_validated_index_buffer_oracles(self):
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

                stats = viewport.indexed_renderer_info["last_render_stats"]
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
                for polygon_id, pixels in expected.get(
                        "propeller_pixels", {}).items():
                    self.assertEqual(
                        stats["final_visible_polygon_owners"][polygon_id][
                            "pixels"
                        ],
                        pixels,
                    )


if __name__ == "__main__":
    unittest.main()
