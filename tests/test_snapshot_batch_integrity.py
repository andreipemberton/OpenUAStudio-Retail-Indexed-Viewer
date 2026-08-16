from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
from types import MethodType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch
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
        fallback: bool = False,
        requested_destination_mode: str = "live_framebuffer",
        requested_forced_index: int | None = None,
        effective_destination_mode: str = "",
        effective_destination_class: str = "",
        effective_forced_index: int | None = None,
        effective_initial_index: int | None = None,
        effective_initial_rgb: str = "",
        effective_forced_rgb: str = "") -> BatchManifestRow:
    return BatchManifestRow(
        "VP", 1, 1, "VP_TEST", 1, "Skeleton/VP_TEST.sklt",
        "root", "Skeleton/VP_TEST.sklt", "Front", relative_file,
        status, status, "retail_indexed_reconstructed", effective,
        fallback, "synthetic fallback" if fallback else "",
        palette, shader, tracy, index_buffer,
        requested_destination_mode, requested_forced_index,
        effective_destination_mode, effective_destination_class,
        effective_forced_index, effective_initial_index,
        effective_initial_rgb, effective_forced_rgb,
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

    def test_batch_freezes_and_propagates_destination_controls(self):
        panel = SimpleNamespace(
            window=SimpleNamespace(viewport=SimpleNamespace(
                flat_tracy_destination_mode="forced_diagnostic",
                flat_tracy_forced_destination_index=31,
            )),
            _renderer_mode="textured_indexed",
        )

        VPSnapshotBatchPanel._capture_flat_tracy_destination_settings(panel)
        self.assertEqual(
            panel._flat_tracy_destination_mode, "forced_diagnostic")
        self.assertEqual(panel._flat_tracy_forced_destination_index, 31)

        viewport = Mock()
        with patch(
                "snapshot_studio.batch_export.AssetViewport",
                return_value=viewport) as viewport_type:
            created = VPSnapshotBatchPanel._create_batch_viewport(panel)

        self.assertIs(created, viewport)
        viewport_type.assert_called_once_with(panel)
        viewport.set_mode.assert_called_once_with("textured_indexed")
        viewport.set_flat_tracy_forced_destination_index.assert_called_once_with(
            31)
        viewport.set_flat_tracy_destination_mode.assert_called_once_with(
            "forced_diagnostic")
        viewport.begin_snapshot_mode.assert_called_once_with(None)
        viewport.play_animation.assert_called_once_with(False)
        viewport.set_snapshot_guides_visible.assert_called_once_with(False)

    def test_invalid_destination_controls_fall_back_to_retail_live(self):
        panel = SimpleNamespace(window=SimpleNamespace(
            viewport=SimpleNamespace(
                flat_tracy_destination_mode="unknown",
                flat_tracy_forced_destination_index=999,
            )))

        VPSnapshotBatchPanel._capture_flat_tracy_destination_settings(panel)

        self.assertEqual(
            panel._flat_tracy_destination_mode, "live_framebuffer")
        self.assertEqual(panel._flat_tracy_forced_destination_index, 0)

    def test_skip_existing_rejects_unverified_and_mismatched_profiles(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "existing.png").write_bytes(b"png")
            panel = SimpleNamespace(
                _root=root,
                _renderer_mode="textured_indexed",
                _flat_tracy_destination_mode="forced_diagnostic",
                _flat_tracy_forced_destination_index=31,
                skip_existing_check=SimpleNamespace(isChecked=lambda: True),
            )

            reason = (
                VPSnapshotBatchPanel.
                _skip_existing_profile_collision_reason(panel))
            self.assertIn("no run_info.json", reason)

            (root / "run_info.json").write_text(
                "not json", encoding="utf-8")
            reason = (
                VPSnapshotBatchPanel.
                _skip_existing_profile_collision_reason(panel))
            self.assertIn("cannot be verified", reason)

            (root / "run_info.json").write_text(json.dumps({
                "renderer_mode": "textured_indexed",
                "indexed_flat_tracy_destination_mode_requested": (
                    "live_framebuffer"),
                "indexed_flat_tracy_forced_destination_index_requested": None,
            }), encoding="utf-8")
            reason = (
                VPSnapshotBatchPanel.
                _skip_existing_profile_collision_reason(panel))
            self.assertIn("live framebuffer", reason)
            self.assertIn("forced diagnostic row 31", reason)

            (root / "run_info.json").write_text(json.dumps({
                "renderer_mode": "textured_indexed",
                "indexed_flat_tracy_destination_mode_requested": (
                    "forced_diagnostic"),
                "indexed_flat_tracy_forced_destination_index_requested": 13,
            }), encoding="utf-8")
            reason = (
                VPSnapshotBatchPanel.
                _skip_existing_profile_collision_reason(panel))
            self.assertIn("forced diagnostic row 13", reason)
            self.assertIn("forced diagnostic row 31", reason)

    def test_skip_existing_accepts_only_matching_verified_profile(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "VP").mkdir()
            (root / "VP" / "front.PNG").write_bytes(b"png")
            (root / "run_info.json").write_text(json.dumps({
                "renderer_mode": "textured_indexed",
                "indexed_flat_tracy_destination_mode_requested": (
                    "forced_diagnostic"),
                "indexed_flat_tracy_forced_destination_index_requested": 31,
            }), encoding="utf-8")
            panel = SimpleNamespace(
                _root=root,
                _renderer_mode="textured_indexed",
                _flat_tracy_destination_mode="forced_diagnostic",
                _flat_tracy_forced_destination_index=31,
                skip_existing_check=SimpleNamespace(isChecked=lambda: True),
            )

            reason = (
                VPSnapshotBatchPanel.
                _skip_existing_profile_collision_reason(panel))

            self.assertEqual(reason, "")

    def test_live_skip_profile_ignores_legacy_inactive_forced_index(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "front.png").write_bytes(b"png")
            (root / "run_info.json").write_text(json.dumps({
                "renderer_mode": "textured_indexed",
                "indexed_flat_tracy_destination_mode_requested": (
                    "live_framebuffer"),
                # Earlier manifests retained the selector value even though
                # live mode did not use it. It is semantically inactive.
                "indexed_flat_tracy_forced_destination_index_requested": 220,
            }), encoding="utf-8")
            panel = SimpleNamespace(
                _root=root,
                _renderer_mode="textured_indexed",
                _flat_tracy_destination_mode="live_framebuffer",
                _flat_tracy_forced_destination_index=31,
                skip_existing_check=SimpleNamespace(isChecked=lambda: True),
            )

            reason = (
                VPSnapshotBatchPanel.
                _skip_existing_profile_collision_reason(panel))

            self.assertEqual(reason, "")

    def test_skip_profile_preflight_is_inactive_when_overwriting(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "front.png").write_bytes(b"png")
            panel = SimpleNamespace(
                _root=root,
                skip_existing_check=SimpleNamespace(isChecked=lambda: False),
            )

            reason = (
                VPSnapshotBatchPanel.
                _skip_existing_profile_collision_reason(panel))

            self.assertEqual(reason, "")

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
            "flat_tracy_destination_mode": "forced_diagnostic",
            "flat_tracy_forced_destination_index": 31,
            "initial_framebuffer_index": 0,
            "initial_framebuffer_rgb": [0, 0, 0],
            "flat_tracy_forced_destination_rgb": [12, 34, 56],
        }
        panel = SimpleNamespace(
            _renderer_mode="textured",
            _flat_tracy_destination_mode="forced_diagnostic",
            _flat_tracy_forced_destination_index=31,
        )

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
            self.assertEqual(
                fields["requested_flat_tracy_destination_mode"], "")
            self.assertIsNone(
                fields["effective_flat_tracy_forced_destination_index"])

    def test_indexed_attempt_profile_excludes_stale_frame_hash_on_error(self):
        panel = SimpleNamespace(
            _renderer_mode="textured_indexed",
            _flat_tracy_destination_mode="forced_diagnostic",
            _flat_tracy_forced_destination_index=31,
        )
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
                "flat_tracy_destination_mode": "forced_diagnostic",
                "flat_tracy_forced_destination_index": 31,
                "initial_framebuffer_index": 0,
            },
            status="ERROR",
            reason="atomic commit failed",
        )

        self.assertEqual(attempted["effective_renderer"], "not_rendered")
        self.assertEqual(attempted["palette_sha256"], "palette")
        self.assertEqual(attempted["index_buffer_sha256"], "")
        self.assertEqual(
            attempted["requested_flat_tracy_destination_mode"],
            "forced_diagnostic",
        )
        self.assertEqual(
            attempted["requested_flat_tracy_forced_destination_index"], 31)
        self.assertEqual(
            attempted["effective_flat_tracy_destination_mode"], "")
        self.assertIsNone(
            attempted["effective_initial_framebuffer_index"])

    def test_destination_provenance_is_effective_only_for_exact_written(self):
        panel = SimpleNamespace(
            _renderer_mode="textured_indexed",
            _flat_tracy_destination_mode="forced_diagnostic",
            _flat_tracy_forced_destination_index=31,
        )
        renderer_info = {
            "effective_mode": "retail_indexed_forced_tracy_diagnostic",
            "fallback_used": False,
            "flat_tracy_destination_mode": "forced_diagnostic",
            "flat_tracy_forced_destination_index": 31,
            "initial_framebuffer_index": 0,
            "initial_framebuffer_rgb": [1, 2, 3],
            "flat_tracy_forced_destination_rgb": [12, 34, 56],
        }

        written = VPSnapshotBatchPanel._renderer_manifest_fields(
            panel, renderer_info, status="WRITTEN")
        self.assertEqual(
            written["requested_flat_tracy_destination_mode"],
            "forced_diagnostic",
        )
        self.assertEqual(
            written["effective_flat_tracy_destination_mode"],
            "forced_diagnostic",
        )
        self.assertEqual(
            written["effective_flat_tracy_destination_class"], "diagnostic")
        self.assertEqual(
            written["effective_flat_tracy_forced_destination_index"], 31)
        self.assertEqual(written["effective_initial_framebuffer_index"], 0)
        self.assertEqual(written["effective_initial_framebuffer_rgb"], "#010203")
        self.assertEqual(
            written["effective_flat_tracy_forced_destination_rgb"],
            "#0C2238",
        )

        for status in ("EXISTS", "ERROR", "ERROR_EXISTING_RETAINED"):
            fields = VPSnapshotBatchPanel._renderer_manifest_fields(
                panel, renderer_info, status=status)
            self.assertEqual(
                fields["requested_flat_tracy_destination_mode"],
                "forced_diagnostic",
            )
            self.assertEqual(
                fields["effective_flat_tracy_destination_mode"], "")
            self.assertIsNone(
                fields["effective_flat_tracy_forced_destination_index"])
            self.assertIsNone(
                fields["effective_initial_framebuffer_index"])

        fallback = VPSnapshotBatchPanel._renderer_manifest_fields(
            panel,
            {
                **renderer_info,
                "effective_mode": "openua_preview_fallback",
                "fallback_used": True,
            },
            status="WRITTEN",
        )
        self.assertEqual(
            fallback["effective_flat_tracy_destination_mode"], "")
        self.assertIsNone(
            fallback["effective_flat_tracy_forced_destination_index"])

    def test_live_destination_is_canonical_and_has_no_forced_effective_index(self):
        panel = SimpleNamespace(
            _renderer_mode="textured_indexed",
            _flat_tracy_destination_mode="live_framebuffer",
            _flat_tracy_forced_destination_index=220,
        )
        fields = VPSnapshotBatchPanel._renderer_manifest_fields(
            panel,
            {
                "effective_mode": "retail_indexed_reconstructed",
                "fallback_used": False,
                "flat_tracy_destination_mode": "live_framebuffer",
                "flat_tracy_forced_destination_index": 220,
                "initial_framebuffer_index": 0,
                "initial_framebuffer_rgb": [0, 0, 0],
                "flat_tracy_forced_destination_rgb": [9, 9, 9],
            },
            status="WRITTEN",
        )

        self.assertEqual(
            fields["effective_flat_tracy_destination_class"], "canonical")
        self.assertIsNone(
            fields["requested_flat_tracy_forced_destination_index"])
        self.assertEqual(fields["effective_initial_framebuffer_index"], 0)
        self.assertIsNone(
            fields["effective_flat_tracy_forced_destination_index"])
        self.assertEqual(
            fields["effective_flat_tracy_forced_destination_rgb"], "")

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
                        tracy="written-tracy", index_buffer="written-frame",
                        requested_destination_mode="forced_diagnostic",
                        requested_forced_index=31,
                        effective_destination_mode="forced_diagnostic",
                        effective_destination_class="diagnostic",
                        effective_forced_index=31,
                        effective_initial_index=0,
                        effective_initial_rgb="#000000",
                        effective_forced_rgb="#0C2238"),
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
                _flat_tracy_destination_mode="forced_diagnostic",
                _flat_tracy_forced_destination_index=31,
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
            manifest = json.loads(
                (root / "manifest.json").read_text(encoding="utf-8"))

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
            self.assertEqual(
                summary["requested_flat_tracy_destination_mode"],
                "forced_diagnostic",
            )
            self.assertEqual(
                summary["requested_flat_tracy_destination_class"],
                "diagnostic",
            )
            self.assertEqual(
                summary["requested_flat_tracy_forced_destination_index"],
                31,
            )
            self.assertEqual(
                summary[
                    "verified_written_flat_tracy_destination_profiles"],
                [{
                    "mode": "forced_diagnostic",
                    "class": "diagnostic",
                    "initial_framebuffer_index": 0,
                    "forced_destination_index": 31,
                }],
            )
            self.assertIn("do not alter filenames", summary["note"])
            self.assertIn(
                "do not alter image filenames",
                summary["destination_profile_output_collision_warning"],
            )
            self.assertEqual(
                summary["skip_existing_collision_policy"],
                "populated_output_requires_matching_run_info_renderer_and_"
                "destination_profile",
            )
            self.assertIsInstance(
                manifest[0]["effective_initial_framebuffer_index"], int)
            self.assertEqual(
                manifest[0][
                    "effective_flat_tracy_forced_destination_index"],
                31,
            )
            self.assertIsNone(
                manifest[1][
                    "effective_flat_tracy_forced_destination_index"])

    def test_live_run_provenance_nulls_inactive_forced_index_everywhere(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            panel = SimpleNamespace(
                _root=root,
                _rows=[_row(
                    "WRITTEN", "VP/front.png",
                    effective="retail_indexed_reconstructed",
                    requested_destination_mode="live_framebuffer",
                    requested_forced_index=None,
                    effective_destination_mode="live_framebuffer",
                    effective_destination_class="canonical",
                    effective_initial_index=0,
                    effective_initial_rgb="#000000",
                )],
                _warnings=[],
                _renderer_mode="textured_indexed",
                _flat_tracy_destination_mode="live_framebuffer",
                # The UI may retain a value while its control is inactive.
                _flat_tracy_forced_destination_index=220,
                window=SimpleNamespace(
                    _setbas=None, _vp_source="", _vp_source_path=""),
                _sources=[_source()],
                _target_size=QSize(1024, 1024),
                _zoom=100,
                _written=1,
                _existing=0,
                _skipped_models=0,
                _failed=0,
                _started_at=0.0,
            )

            VPSnapshotBatchPanel._write_manifests(panel, cancelled=False)
            manifest = json.loads(
                (root / "manifest.json").read_text(encoding="utf-8"))
            run_info = json.loads(
                (root / "run_info.json").read_text(encoding="utf-8"))
            summary = run_info["renderer_summary"]

            self.assertIsNone(
                manifest[0][
                    "requested_flat_tracy_forced_destination_index"])
            self.assertEqual(
                summary["requested_flat_tracy_destination_mode"],
                "live_framebuffer",
            )
            self.assertEqual(
                summary["requested_flat_tracy_destination_class"],
                "canonical",
            )
            self.assertIsNone(
                summary[
                    "requested_flat_tracy_forced_destination_index"])
            self.assertEqual(
                run_info[
                    "indexed_flat_tracy_destination_mode_requested"],
                "live_framebuffer",
            )
            self.assertIsNone(
                run_info[
                    "indexed_flat_tracy_forced_destination_index_requested"])

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
