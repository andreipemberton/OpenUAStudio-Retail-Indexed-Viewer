from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
from types import MethodType, SimpleNamespace
import unittest
from unittest.mock import patch
import zipfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSize
from PySide6.QtGui import QColor, QImage
from PySide6.QtWidgets import QApplication

from snapshot_studio.batch_export import (
    BatchManifestRow,
    SnapshotSource,
    VPSnapshotBatchPanel,
)


def _row(
        status: str,
        relative_file: str,
        *,
        effective: str = "not_rendered",
        palette: str = "",
        shader: str = "",
        tracy: str = "",
        index_buffer: str = "",
        fallback: bool = False) -> BatchManifestRow:
    return BatchManifestRow(
        "VP", 1, 1, "VP_TEST", 1, "Skeleton/VP_TEST.sklt",
        "root", "Skeleton/VP_TEST.sklt", "Front", relative_file,
        status, status, "retail_indexed_reconstructed", effective,
        fallback, "synthetic fallback" if fallback else "",
        palette, shader, tracy, index_buffer,
    )


def _source() -> SnapshotSource:
    return SnapshotSource(
        "VP", 1, 1, "VP_TEST", "", 1,
        "Skeleton/VP_TEST.sklt", "VP_0001_TEST", "root",
        "Skeleton/VP_TEST.sklt", object(), None, False,
    )


class _Label:
    def __init__(self) -> None:
        self.value = None

    def setText(self, value) -> None:
        self.value = value


class _Progress:
    def __init__(self) -> None:
        self.value = None

    def setValue(self, value) -> None:
        self.value = value


class SnapshotBatchIntegrityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_openua_status_precedes_stale_indexed_renderer_info(self):
        stale_indexed = {
            "effective_mode": "retail_indexed_reconstructed",
            "fallback_used": True,
            "fallback_reason": "stale indexed fallback",
            "sources": {
                "palette": {"sha256": "stale-palette"},
                "shader": {"sha256": "stale-shader"},
                "tracy": {"sha256": "stale-tracy"},
            },
            "last_render_stats": {"index_buffer_sha256": "stale-frame"},
        }
        panel = SimpleNamespace(_renderer_mode="textured")

        existing = VPSnapshotBatchPanel._renderer_manifest_fields(
            panel, stale_indexed, status="EXISTS")
        failed = VPSnapshotBatchPanel._renderer_manifest_fields(
            panel, stale_indexed, status="ERROR", reason="write failed")
        written = VPSnapshotBatchPanel._renderer_manifest_fields(
            panel, stale_indexed, status="WRITTEN")

        self.assertEqual(
            existing["effective_renderer"], "existing_file_not_verified")
        self.assertEqual(failed["effective_renderer"], "not_rendered")
        self.assertEqual(written["effective_renderer"], "openua_preview")
        self.assertFalse(failed["fallback_used"])
        self.assertEqual(failed["fallback_reason"], "write failed")
        for fields in (existing, failed, written):
            self.assertEqual(fields["palette_sha256"], "")
            self.assertEqual(fields["shadermp_sha256"], "")
            self.assertEqual(fields["tracyrmp_sha256"], "")
            self.assertEqual(fields["index_buffer_sha256"], "")

    def test_indexed_attempt_profile_excludes_stale_frame_hash_on_error(self):
        panel = SimpleNamespace(_renderer_mode="textured_indexed")
        attempted = VPSnapshotBatchPanel._renderer_manifest_fields(
            panel,
            {
                "effective_mode": "retail_indexed_reconstructed",
                "sources": {
                    "palette": {"sha256": "palette"},
                    "shader": {"sha256": "shader"},
                    "tracy": {"sha256": "tracy"},
                },
                "last_render_stats": {"index_buffer_sha256": "old-frame"},
            },
            status="ERROR",
            reason="atomic commit failed",
        )

        self.assertEqual(attempted["effective_renderer"], "not_rendered")
        self.assertEqual(attempted["palette_sha256"], "palette")
        self.assertEqual(attempted["index_buffer_sha256"], "")

    def test_atomic_png_replace_failure_preserves_old_final_and_cleans_part(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "front.png"
            output.write_bytes(b"old-final")
            image = QImage(4, 4, QImage.Format.Format_RGBA8888)
            image.fill(QColor(20, 40, 60, 255))

            with patch(
                    "snapshot_studio.batch_export.os.replace",
                    side_effect=OSError("synthetic replace failure")):
                with self.assertRaisesRegex(
                        OSError, "synthetic replace failure"):
                    VPSnapshotBatchPanel._write_png_atomic(image, output)

            self.assertEqual(output.read_bytes(), b"old-final")
            self.assertFalse(
                output.with_name(f"{output.name}.part").exists())

    def test_step_labels_failed_overwrite_as_existing_retained(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "VP_0001_TEST" / "01_front.png"
            output.parent.mkdir()
            output.write_bytes(b"old-final")
            image = QImage(4, 4, QImage.Format.Format_RGBA8888)
            image.fill(QColor(1, 2, 3, 255))
            viewport = SimpleNamespace(
                apply_snapshot_preset=lambda *_args: None,
                render_snapshot=lambda *_args, **_kwargs: image,
                indexed_renderer_info={
                    "effective_mode": "retail_indexed_reconstructed",
                    "sources": {
                        "palette": {"sha256": "stale-palette"},
                    },
                    "last_render_stats": {
                        "index_buffer_sha256": "stale-frame",
                    },
                },
            )
            panel = SimpleNamespace(
                _running=True,
                _cancel_requested=False,
                _queue=[(_source(), 1, "Front")],
                _queue_index=0,
                _root=root,
                _target_size=QSize(4, 4),
                _zoom=100,
                _batch_viewport=viewport,
                _renderer_mode="textured",
                _rows=[],
                _warnings=[],
                _written=0,
                _existing=0,
                _failed=0,
                progress=_Progress(),
                status_label=_Label(),
                skip_existing_check=SimpleNamespace(isChecked=lambda: False),
                _load_source=lambda _source: None,
                _step=lambda: None,
                _finish=lambda **_kwargs: None,
                _write_png_atomic=lambda _image, _path: (
                    _ for _ in ()).throw(OSError("synthetic write failure")),
            )
            panel._renderer_manifest_fields = MethodType(
                VPSnapshotBatchPanel._renderer_manifest_fields, panel)
            panel._record = MethodType(VPSnapshotBatchPanel._record, panel)

            VPSnapshotBatchPanel._step(panel)

            self.assertEqual(output.read_bytes(), b"old-final")
            self.assertEqual(panel._failed, 1)
            self.assertEqual(len(panel._rows), 1)
            row = panel._rows[0]
            self.assertEqual(row.status, "ERROR_EXISTING_RETAINED")
            self.assertEqual(row.relative_file, "VP_0001_TEST/01_front.png")
            self.assertEqual(
                row.effective_renderer, "existing_file_not_verified")
            self.assertEqual(row.palette_sha256, "")
            self.assertEqual(row.index_buffer_sha256, "")
            self.assertIn("existing final retained", row.message)

    def test_skip_existing_is_recorded_before_source_load(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "VP_0001_TEST" / "01_front.png"
            output.parent.mkdir()
            output.write_bytes(b"old-final")
            loads = []
            panel = SimpleNamespace(
                _running=True,
                _cancel_requested=False,
                _queue=[(_source(), 1, "Front")],
                _queue_index=0,
                _root=root,
                _target_size=QSize(4, 4),
                _zoom=100,
                _batch_viewport=None,
                _renderer_mode="textured",
                _rows=[],
                _warnings=[],
                _written=0,
                _existing=0,
                _failed=0,
                progress=_Progress(),
                status_label=_Label(),
                skip_existing_check=SimpleNamespace(isChecked=lambda: True),
                _load_source=lambda source: loads.append(source),
                _step=lambda: None,
                _finish=lambda **_kwargs: None,
            )
            panel._renderer_manifest_fields = MethodType(
                VPSnapshotBatchPanel._renderer_manifest_fields, panel)
            panel._record = MethodType(VPSnapshotBatchPanel._record, panel)

            VPSnapshotBatchPanel._step(panel)

            self.assertEqual(loads, [])
            self.assertEqual(panel._existing, 1)
            self.assertEqual(panel._rows[0].status, "EXISTS")
            self.assertEqual(
                panel._rows[0].effective_renderer,
                "existing_file_not_verified",
            )

    def test_run_summary_separates_written_renderers_from_outcomes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            panel = SimpleNamespace(
                _root=root,
                _rows=[
                    _row(
                        "WRITTEN", "VP/front.png",
                        effective="retail_indexed_reconstructed",
                        palette="written-palette", shader="written-shader",
                        tracy="written-tracy", index_buffer="written-frame"),
                    _row(
                        "EXISTS", "VP/side.png",
                        effective="existing_file_not_verified"),
                    _row(
                        "ERROR", "", palette="attempt-palette",
                        shader="attempt-shader", tracy="attempt-tracy"),
                    _row(
                        "ERROR_EXISTING_RETAINED", "VP/top.png",
                        effective="existing_file_not_verified",
                        palette="retained-attempt-palette"),
                ],
                _warnings=[],
                _renderer_mode="textured_indexed",
                window=SimpleNamespace(
                    _setbas=None, _vp_source="", _vp_source_path=""),
                _sources=[_source()],
                _target_size=QSize(4096, 4096),
                _zoom=100,
                _written=1,
                _existing=1,
                _skipped_models=0,
                _failed=2,
                _started_at=0.0,
            )

            VPSnapshotBatchPanel._write_manifests(panel, cancelled=False)
            summary = json.loads(
                (root / "run_info.json").read_text(encoding="utf-8")
            )["renderer_summary"]

            self.assertEqual(summary["outcome_counts"], {
                "WRITTEN": 1,
                "EXISTS": 1,
                "ERROR": 1,
                "ERROR_EXISTING_RETAINED": 1,
            })
            self.assertEqual(summary["written_effective_image_counts"], {
                "retail_indexed_reconstructed": 1,
            })
            self.assertEqual(summary["source_profiles"], [{
                "palette_sha256": "written-palette",
                "shadermp_sha256": "written-shader",
                "tracyrmp_sha256": "written-tracy",
            }])
            attempted = summary["attempted_source_profiles"]
            self.assertEqual(len(attempted), 2)
            self.assertNotIn(
                "written-palette",
                {profile["palette_sha256"] for profile in attempted},
            )

    def test_zip_contains_only_authorized_images_and_fixed_metadata(self):
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            root = parent / "corpus"
            root.mkdir()
            files = {
                "VP/front.png": b"front",
                "VP/retained.png": b"retained",
                "VP/error.png": b"error",
                "stale.png": b"stale",
                "orphan.png.part": b"partial",
                "manifest.json": b"[]",
                "manifest.csv": b"header\n",
                "warnings.log": b"",
                "run_info.json": b"{}",
            }
            for relative, content in files.items():
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)
            outside = parent / "outside.png"
            outside.write_bytes(b"outside")

            panel = SimpleNamespace(
                _root=root,
                _rows=[
                    _row("WRITTEN", "VP/front.png"),
                    _row(
                        "ERROR_EXISTING_RETAINED", "VP/retained.png",
                        effective="existing_file_not_verified"),
                    _row("ERROR", "VP/error.png"),
                    _row("WRITTEN", "../outside.png"),
                ],
                _cancel_requested=False,
            )
            panel._authorized_zip_files = MethodType(
                VPSnapshotBatchPanel._authorized_zip_files, panel)

            zip_path = VPSnapshotBatchPanel._create_zip(panel)
            self.assertIsNotNone(zip_path)
            with zipfile.ZipFile(zip_path) as archive:
                names = set(archive.namelist())

            prefix = f"{root.name}/"
            self.assertEqual(names, {
                f"{prefix}VP/front.png",
                f"{prefix}VP/retained.png",
                f"{prefix}manifest.json",
                f"{prefix}manifest.csv",
                f"{prefix}warnings.log",
                f"{prefix}run_info.json",
            })


if __name__ == "__main__":
    unittest.main()
