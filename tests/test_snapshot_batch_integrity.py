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

from assembly_viewer import (
    PSX_NATIVE_VIEW_MODE,
    PSX_PROTOTYPE_PROFILE_ID,
    PSX_PROTOTYPE_PROFILE_VERSION,
    PSX_PROTOTYPE_VIEW_MODE,
    RETAIL_AREA_DISTANCE_FADE_DISTANCE_SPACE,
    RETAIL_AREA_DISTANCE_FADE_FORMULA,
)

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
        effective_forced_rgb: str = "",
        requested_distance_fade_enabled: bool | None = False,
        effective_distance_fade_enabled: bool | None = None,
        distance_fade_profile_id: str = "",
        distance_fade_visibility_limit: float | None = None,
        distance_fade_start: float | None = None,
        distance_fade_length: float | None = None,
        distance_fade_distance_space: str = "",
        distance_fade_formula: str = "") -> BatchManifestRow:
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
        requested_distance_fade_enabled,
        effective_distance_fade_enabled,
        distance_fade_profile_id,
        distance_fade_visibility_limit, distance_fade_start,
        distance_fade_length, distance_fade_distance_space,
        distance_fade_formula,
    )


def _source() -> SnapshotSource:
    return SnapshotSource(
        "VP", 1, 1, "VP_TEST", "", 1,
        "Skeleton/VP_TEST.sklt", "VP_0001_TEST", "root",
        "Skeleton/VP_TEST.sklt", object(), None, False,
    )


def _psx_renderer_info(**overrides) -> dict:
    info = {
        "mode": PSX_PROTOTYPE_PROFILE_ID,
        "requested_mode": PSX_PROTOTYPE_VIEW_MODE,
        "effective_mode": PSX_PROTOTYPE_PROFILE_ID,
        "fallback_used": False,
        "fallback_reason": "",
        "profile_id": PSX_PROTOTYPE_PROFILE_ID,
        "profile_version": PSX_PROTOTYPE_PROFILE_VERSION,
        "source_asset_pipeline": "pc_openua_asset_family",
        "native_psx_asset_decode": False,
        "cycle_accurate": False,
        "texture_interpolation": "affine",
        "texture_filter": "nearest",
        "polygon_antialiasing": False,
        "native_resolution": "unvalidated_not_applied",
        "fog_draw_distance": "unvalidated_not_applied",
        "psx_color_semantics": "unvalidated_not_applied",
        "psx_dithering": "unvalidated_not_applied",
        "psx_vertex_snapping": "unvalidated_not_applied",
        "psx_primitive_queues": "unvalidated_not_applied",
        "scope": (
            "platform-informed presentation of the loaded PC/OpenUA asset; "
            "does not decode PSX UNIT.BIN, PW3, GFX, DAT/IND, or prototype "
            "animation data"
        ),
    }
    info.update(overrides)
    return info


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

    def test_batch_freezes_and_propagates_indexed_controls(self):
        panel = SimpleNamespace(
            window=SimpleNamespace(viewport=SimpleNamespace(
                flat_tracy_destination_mode="forced_diagnostic",
                flat_tracy_forced_destination_index=31,
                distance_fade_enabled=True,
            )),
            _renderer_mode="textured_indexed",
        )

        VPSnapshotBatchPanel._capture_flat_tracy_destination_settings(panel)
        VPSnapshotBatchPanel._capture_distance_fade_setting(panel)
        self.assertEqual(
            panel._flat_tracy_destination_mode, "forced_diagnostic")
        self.assertEqual(panel._flat_tracy_forced_destination_index, 31)
        self.assertTrue(panel._distance_fade_enabled)

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
        viewport.set_distance_fade_enabled.assert_called_once_with(True)
        viewport.begin_snapshot_mode.assert_called_once_with(None)
        viewport.play_animation.assert_called_once_with(False)
        viewport.set_snapshot_guides_visible.assert_called_once_with(False)

    def test_invalid_destination_controls_fall_back_to_retail_live(self):
        panel = SimpleNamespace(window=SimpleNamespace(
            viewport=SimpleNamespace(
                flat_tracy_destination_mode="unknown",
                flat_tracy_forced_destination_index=999,
                distance_fade_enabled="yes",
            )))

        VPSnapshotBatchPanel._capture_flat_tracy_destination_settings(panel)
        VPSnapshotBatchPanel._capture_distance_fade_setting(panel)

        self.assertEqual(
            panel._flat_tracy_destination_mode, "live_framebuffer")
        self.assertEqual(panel._flat_tracy_forced_destination_index, 0)
        self.assertFalse(panel._distance_fade_enabled)

    def test_native_source_blocks_pc_batch_before_output_or_state_access(self):
        panel = SimpleNamespace(
            _running=False,
            window=SimpleNamespace(
                snapshot_renderer_combo=SimpleNamespace(
                    currentData=lambda: "textured"),
                viewport=SimpleNamespace(source_kind="psx_native"),
            ),
            status_label=_Label(),
        )
        panel._native_psx_source_selected_or_active = MethodType(
            VPSnapshotBatchPanel._native_psx_source_selected_or_active,
            panel,
        )

        with patch(
                "snapshot_studio.batch_export.QMessageBox.information") \
                as information:
            VPSnapshotBatchPanel.start(panel)

        information.assert_called_once()
        self.assertEqual(
            panel.status_label.value,
            "Use the PSX Archive native mesh batch.")
        self.assertIn(
            "Batch Native PSX Meshes",
            information.call_args.args[2])

    def test_batch_running_state_disables_psx_panel_and_refreshes_on_finish(self):
        panel = SimpleNamespace(
            _running=False,
            export_button=Mock(),
            output_edit=Mock(),
            output_button=Mock(),
            skip_existing_check=Mock(),
            zip_check=Mock(),
            cancel_button=Mock(),
            window=SimpleNamespace(
                _bas_panel=Mock(),
                _snapshot_studio_box=Mock(),
                _psx_panel=Mock(),
            ),
            refresh=Mock(),
        )

        VPSnapshotBatchPanel._set_running(panel, True)
        panel.window._psx_panel.setEnabled.assert_called_with(False)
        panel.refresh.assert_not_called()

        VPSnapshotBatchPanel._set_running(panel, False)
        panel.window._psx_panel.setEnabled.assert_called_with(True)
        panel.refresh.assert_called_once_with()

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

    def test_skip_profile_treats_missing_legacy_fade_as_false(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "front.png").write_bytes(b"png")
            (root / "run_info.json").write_text(json.dumps({
                "renderer_mode": "textured_indexed",
                "indexed_flat_tracy_destination_mode_requested": (
                    "live_framebuffer"),
                "indexed_flat_tracy_forced_destination_index_requested": None,
            }), encoding="utf-8")
            panel = SimpleNamespace(
                _root=root,
                _renderer_mode="textured_indexed",
                _flat_tracy_destination_mode="live_framebuffer",
                _flat_tracy_forced_destination_index=0,
                _distance_fade_enabled=False,
                skip_existing_check=SimpleNamespace(isChecked=lambda: True),
            )

            self.assertEqual(
                VPSnapshotBatchPanel.
                _skip_existing_profile_collision_reason(panel), "")

            panel._distance_fade_enabled = True
            reason = (
                VPSnapshotBatchPanel.
                _skip_existing_profile_collision_reason(panel))
            self.assertIn("distance fade off", reason)
            self.assertIn("distance fade on", reason)

    def test_skip_profile_rejects_mismatched_or_invalid_fade_metadata(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "front.png").write_bytes(b"png")
            panel = SimpleNamespace(
                _root=root,
                _renderer_mode="textured_indexed",
                _flat_tracy_destination_mode="live_framebuffer",
                _flat_tracy_forced_destination_index=0,
                _distance_fade_enabled=True,
                skip_existing_check=SimpleNamespace(isChecked=lambda: True),
            )
            base_info = {
                "renderer_mode": "textured_indexed",
                "indexed_flat_tracy_destination_mode_requested": (
                    "live_framebuffer"),
                "indexed_flat_tracy_forced_destination_index_requested": None,
            }

            (root / "run_info.json").write_text(json.dumps({
                **base_info,
                "indexed_distance_fade_enabled_requested": False,
            }), encoding="utf-8")
            reason = (
                VPSnapshotBatchPanel.
                _skip_existing_profile_collision_reason(panel))
            self.assertIn("distance fade off", reason)
            self.assertIn("distance fade on", reason)

            (root / "run_info.json").write_text(json.dumps({
                **base_info,
                "indexed_distance_fade_enabled_requested": "true",
            }), encoding="utf-8")
            reason = (
                VPSnapshotBatchPanel.
                _skip_existing_profile_collision_reason(panel))
            self.assertIn("does not prove whether", reason)

            (root / "run_info.json").write_text(json.dumps({
                **base_info,
                "indexed_distance_fade_enabled_requested": True,
            }), encoding="utf-8")
            reason = (
                VPSnapshotBatchPanel.
                _skip_existing_profile_collision_reason(panel))
            self.assertIn("does not prove the complete", reason)

            matching_profile = {
                "profile_id": "retail_gameplay_near_1400_600",
                "visibility_limit": 1400.0,
                "fade_start": 800.0,
                "fade_length": 600.0,
                "distance_space": RETAIL_AREA_DISTANCE_FADE_DISTANCE_SPACE,
                "formula": RETAIL_AREA_DISTANCE_FADE_FORMULA,
            }
            (root / "run_info.json").write_text(json.dumps({
                **base_info,
                "indexed_distance_fade_enabled_requested": True,
                "indexed_distance_fade_profile_requested": {
                    **matching_profile,
                    "visibility_limit": 9999.0,
                },
            }), encoding="utf-8")
            reason = (
                VPSnapshotBatchPanel.
                _skip_existing_profile_collision_reason(panel))
            self.assertIn("different distance-fade profile", reason)

            (root / "run_info.json").write_text(json.dumps({
                **base_info,
                "indexed_distance_fade_enabled_requested": True,
                "indexed_distance_fade_profile_requested": matching_profile,
            }), encoding="utf-8")
            self.assertEqual(
                VPSnapshotBatchPanel.
                _skip_existing_profile_collision_reason(panel), "")

            (root / "run_info.json").write_text(json.dumps({
                **base_info,
                "indexed_distance_fade_enabled_requested": True,
                "indexed_distance_fade_profile_requested": {
                    **matching_profile,
                    "visibility_limit": "1400.0",
                },
            }), encoding="utf-8")
            reason = (
                VPSnapshotBatchPanel.
                _skip_existing_profile_collision_reason(panel))
            self.assertIn("does not prove the complete", reason)

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
            self.assertIsNone(fields["requested_distance_fade_enabled"])
            self.assertIsNone(fields["effective_distance_fade_enabled"])
            self.assertIsNone(fields["distance_fade_visibility_limit"])

    def test_psx_profile_is_renderer_neutral_and_retail_fields_are_null(self):
        panel = SimpleNamespace(_renderer_mode=PSX_PROTOTYPE_VIEW_MODE)

        fields = VPSnapshotBatchPanel._renderer_manifest_fields(
            panel, _psx_renderer_info(), status="WRITTEN")

        self.assertEqual(
            fields["requested_renderer"], PSX_PROTOTYPE_PROFILE_ID)
        self.assertEqual(
            fields["effective_renderer"], PSX_PROTOTYPE_PROFILE_ID)
        self.assertEqual(fields["profile_id"], PSX_PROTOTYPE_PROFILE_ID)
        self.assertEqual(
            fields["profile_version"], PSX_PROTOTYPE_PROFILE_VERSION)
        self.assertEqual(
            fields["source_asset_pipeline"], "pc_openua_asset_family")
        self.assertFalse(fields["native_psx_asset_decode"])
        self.assertFalse(fields["cycle_accurate"])
        self.assertEqual(fields["texture_interpolation"], "affine")
        self.assertEqual(fields["texture_filter"], "nearest")
        self.assertFalse(fields["polygon_antialiasing"])
        for key in (
                "native_resolution", "fog_draw_distance",
                "psx_color_semantics", "psx_dithering",
                "psx_vertex_snapping", "psx_primitive_queues"):
            self.assertEqual(fields[key], "unvalidated_not_applied")
        for key in (
                "palette_sha256", "shadermp_sha256", "tracyrmp_sha256",
                "index_buffer_sha256",
                "requested_flat_tracy_destination_mode",
                "requested_flat_tracy_forced_destination_index",
                "effective_flat_tracy_destination_mode",
                "effective_flat_tracy_destination_class",
                "effective_flat_tracy_forced_destination_index",
                "effective_initial_framebuffer_index",
                "effective_initial_framebuffer_rgb",
                "effective_flat_tracy_forced_destination_rgb",
                "requested_distance_fade_enabled",
                "effective_distance_fade_enabled", "distance_fade_profile_id",
                "distance_fade_visibility_limit", "distance_fade_start",
                "distance_fade_length", "distance_fade_distance_space",
                "distance_fade_formula"):
            self.assertIsNone(fields[key], key)

    def test_psx_profile_fails_closed_on_inexact_policy_provenance(self):
        panel = SimpleNamespace(_renderer_mode=PSX_PROTOTYPE_VIEW_MODE)
        mutations = {
            "mode": "openua_preview",
            "requested_mode": "textured",
            "fallback_used": 0,
            "fallback_reason": "synthetic fallback",
            "profile_version": True,
            "texture_interpolation": "projective",
            "texture_filter": "smooth",
            "polygon_antialiasing": True,
            "native_resolution": "applied",
            "psx_dithering": None,
        }

        for key, value in mutations.items():
            with self.subTest(key=key):
                with self.assertRaisesRegex(RuntimeError, key):
                    VPSnapshotBatchPanel._renderer_manifest_fields(
                        panel,
                        _psx_renderer_info(**{key: value}),
                        status="WRITTEN",
                    )

    def test_renderer_neutral_metadata_precedes_stale_indexed_metadata(self):
        psx_info = _psx_renderer_info()
        viewport = SimpleNamespace(
            renderer_info=psx_info,
            indexed_renderer_info={
                "effective_mode": "retail_indexed_reconstructed"},
        )

        self.assertIs(
            VPSnapshotBatchPanel._viewport_renderer_info(viewport), psx_info)

    def test_psx_skip_existing_requires_exact_profile_and_policy(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "front.png").write_bytes(b"png")
            panel = SimpleNamespace(
                _root=root,
                _renderer_mode=PSX_PROTOTYPE_VIEW_MODE,
                _flat_tracy_destination_mode="forced_diagnostic",
                _flat_tracy_forced_destination_index=31,
                _distance_fade_enabled=True,
                skip_existing_check=SimpleNamespace(isChecked=lambda: True),
            )
            run_info = {
                "renderer_mode": PSX_PROTOTYPE_VIEW_MODE,
                **{
                    key: value for key, value in _psx_renderer_info().items()
                    if key not in {
                        "effective_mode", "fallback_used", "fallback_reason"}
                },
            }
            renderer_fields = VPSnapshotBatchPanel._renderer_manifest_fields(
                panel, _psx_renderer_info(), status="WRITTEN")
            (root / "manifest.json").write_text(
                json.dumps([{
                    "relative_file": "front.png",
                    "status": "WRITTEN",
                    **renderer_fields,
                }]),
                encoding="utf-8",
            )

            (root / "run_info.json").write_text(
                json.dumps({"renderer_mode": PSX_PROTOTYPE_VIEW_MODE}),
                encoding="utf-8")
            reason = (
                VPSnapshotBatchPanel.
                _skip_existing_profile_collision_reason(panel))
            self.assertIn("visual profile ID", reason)

            wrong_version = {**run_info, "profile_version": 2}
            (root / "run_info.json").write_text(
                json.dumps(wrong_version), encoding="utf-8")
            reason = (
                VPSnapshotBatchPanel.
                _skip_existing_profile_collision_reason(panel))
            self.assertIn("psx_prototype_visual_v1 v2", reason)
            self.assertIn("psx_prototype_visual_v1 v1", reason)

            wrong_policy = {**run_info, "psx_dithering": "applied"}
            (root / "run_info.json").write_text(
                json.dumps(wrong_policy), encoding="utf-8")
            reason = (
                VPSnapshotBatchPanel.
                _skip_existing_profile_collision_reason(panel))
            self.assertIn("output-affecting visual profile policy", reason)

            (root / "run_info.json").write_text(
                json.dumps(run_info), encoding="utf-8")
            self.assertEqual(
                VPSnapshotBatchPanel.
                _skip_existing_profile_collision_reason(panel), "")

    def test_psx_run_info_and_manifest_record_fixed_profile_contract(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            panel = SimpleNamespace(
                _root=root,
                _rows=[],
                _warnings=[],
                _renderer_mode=PSX_PROTOTYPE_VIEW_MODE,
                _flat_tracy_destination_mode="forced_diagnostic",
                _flat_tracy_forced_destination_index=31,
                _distance_fade_enabled=True,
                window=SimpleNamespace(
                    _setbas=None, _vp_source="", _vp_source_path=""),
                _sources=[_source()],
                _target_size=QSize(512, 512),
                _zoom=100,
                _written=1,
                _existing=0,
                _skipped_models=0,
                _failed=0,
                _started_at=0.0,
            )
            panel._renderer_manifest_fields = MethodType(
                VPSnapshotBatchPanel._renderer_manifest_fields, panel)
            panel._record = MethodType(VPSnapshotBatchPanel._record, panel)
            panel._record(
                _source(), "Front", "VP/front.png", "WRITTEN", "written",
                renderer_info=_psx_renderer_info())

            VPSnapshotBatchPanel._write_manifests(panel, cancelled=False)

            manifest = json.loads(
                (root / "manifest.json").read_text(encoding="utf-8"))
            run_info = json.loads(
                (root / "run_info.json").read_text(encoding="utf-8"))
            row = manifest[0]
            summary = run_info["renderer_summary"]
            for record in (row, summary, run_info):
                self.assertEqual(
                    record["profile_id"], PSX_PROTOTYPE_PROFILE_ID)
                self.assertEqual(
                    record["profile_version"],
                    PSX_PROTOTYPE_PROFILE_VERSION)
                self.assertEqual(
                    record["source_asset_pipeline"],
                    "pc_openua_asset_family")
                self.assertFalse(record["native_psx_asset_decode"])
                self.assertEqual(
                    record["psx_primitive_queues"],
                    "unvalidated_not_applied")
            self.assertEqual(
                summary["requested_renderer"], PSX_PROTOTYPE_PROFILE_ID)
            self.assertEqual(
                summary["written_effective_image_counts"],
                {PSX_PROTOTYPE_PROFILE_ID: 1})
            self.assertEqual(
                summary["skip_existing_collision_identity"],
                "renderer_mode_and_visual_profile_id_version_and_applicable_"
                "retail_destination_distance_fade_profile")
            self.assertIsNone(
                run_info[
                    "indexed_flat_tracy_destination_mode_requested"])
            self.assertIsNone(
                run_info["indexed_distance_fade_enabled_requested"])
            self.assertIsNone(row["palette_sha256"])
            self.assertIsNone(
                row["requested_flat_tracy_destination_mode"])
            self.assertIsNone(row["requested_distance_fade_enabled"])

            expected_scope = _psx_renderer_info()["scope"]
            self.assertEqual(row["scope"], expected_scope)
            self.assertEqual(summary["scope"], expected_scope)
            self.assertEqual(run_info["profile_scope"], expected_scope)
            self.assertNotEqual(run_info["scope"], expected_scope)

            image = root / "VP" / "front.png"
            image.parent.mkdir(parents=True, exist_ok=True)
            image.write_bytes(b"png")
            panel.skip_existing_check = SimpleNamespace(
                isChecked=lambda: True)
            self.assertEqual(
                VPSnapshotBatchPanel.
                _skip_existing_profile_collision_reason(panel), "")

            manifest[0]["status"] = "ERROR_EXISTING_RETAINED"
            manifest[0]["effective_renderer"] = (
                "existing_file_not_verified")
            (root / "manifest.json").write_text(
                json.dumps(manifest), encoding="utf-8")
            reason = (
                VPSnapshotBatchPanel.
                _skip_existing_profile_collision_reason(panel))
            self.assertIn("was not a verified WRITTEN image", reason)

    def test_indexed_attempt_profile_excludes_stale_frame_hash_on_error(self):
        panel = SimpleNamespace(
            _renderer_mode="textured_indexed",
            _flat_tracy_destination_mode="forced_diagnostic",
            _flat_tracy_forced_destination_index=31,
            _distance_fade_enabled=True,
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
        self.assertTrue(attempted["requested_distance_fade_enabled"])
        self.assertIsNone(attempted["effective_distance_fade_enabled"])
        self.assertEqual(
            attempted["effective_flat_tracy_destination_mode"], "")
        self.assertIsNone(
            attempted["effective_initial_framebuffer_index"])

    def test_destination_provenance_is_effective_only_for_exact_written(self):
        panel = SimpleNamespace(
            _renderer_mode="textured_indexed",
            _flat_tracy_destination_mode="forced_diagnostic",
            _flat_tracy_forced_destination_index=31,
            _distance_fade_enabled=True,
        )
        renderer_info = {
            "effective_mode": "retail_indexed_forced_tracy_diagnostic",
            "fallback_used": False,
            "flat_tracy_destination_mode": "forced_diagnostic",
            "flat_tracy_forced_destination_index": 31,
            "initial_framebuffer_index": 0,
            "initial_framebuffer_rgb": [1, 2, 3],
            "flat_tracy_forced_destination_rgb": [12, 34, 56],
            "requested_distance_fade_enabled": True,
            "effective_distance_fade_enabled": True,
            "distance_fade_visibility_limit": 1400,
            "distance_fade_start": 800,
            "distance_fade_length": 600,
            "distance_fade_distance_space": (
                RETAIL_AREA_DISTANCE_FADE_DISTANCE_SPACE),
            "distance_fade_formula": "static fallback must not be used",
            "last_render_stats": {
                "distance_fade_profile": {
                    "name": "retail_gameplay_near_1400_600",
                    "visibility_limit": 1400.0,
                    "fade_start": 800.0,
                    "fade_length": 600.0,
                },
                "distance_fade_formula": RETAIL_AREA_DISTANCE_FADE_FORMULA,
            },
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
        self.assertTrue(written["requested_distance_fade_enabled"])
        self.assertTrue(written["effective_distance_fade_enabled"])
        self.assertEqual(
            written["distance_fade_profile_id"],
            "retail_gameplay_near_1400_600")
        self.assertEqual(written["distance_fade_visibility_limit"], 1400.0)
        self.assertEqual(written["distance_fade_start"], 800.0)
        self.assertEqual(written["distance_fade_length"], 600.0)
        self.assertEqual(
            written["distance_fade_distance_space"],
            RETAIL_AREA_DISTANCE_FADE_DISTANCE_SPACE)
        self.assertEqual(
            written["distance_fade_formula"],
            RETAIL_AREA_DISTANCE_FADE_FORMULA)

        incomplete = dict(renderer_info)
        incomplete["last_render_stats"] = {
            "distance_fade_profile": {
                "name": "retail_gameplay_near_1400_600",
                "visibility_limit": 1400.0,
                "fade_start": 800.0,
                "fade_length": 600.0,
            },
            # No raster-stat formula: the static renderer descriptor must not
            # be substituted into an apparently verified exact row.
        }
        with self.assertRaisesRegex(RuntimeError, "raster-stat proof"):
            VPSnapshotBatchPanel._renderer_manifest_fields(
                panel, incomplete, status="WRITTEN")

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
            self.assertTrue(fields["requested_distance_fade_enabled"])
            self.assertIsNone(fields["effective_distance_fade_enabled"])
            self.assertIsNone(fields["distance_fade_start"])

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
        self.assertIsNone(fallback["effective_distance_fade_enabled"])

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
        self.assertFalse(fields["requested_distance_fade_enabled"])
        self.assertFalse(fields["effective_distance_fade_enabled"])

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
                        effective_forced_rgb="#0C2238",
                        requested_distance_fade_enabled=True,
                        effective_distance_fade_enabled=True,
                        distance_fade_profile_id=(
                            "retail_gameplay_near_1400_600"),
                        distance_fade_visibility_limit=1400.0,
                        distance_fade_start=800.0,
                        distance_fade_length=600.0,
                        distance_fade_distance_space=(
                            "radial UA model units from eye"),
                        distance_fade_formula=(
                            "b=clamp(shade/256+max(0,(distance-800)/600),"
                            "0,1); screen-linear fixed brightness selects "
                            "SHADERMP row")),
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
                _distance_fade_enabled=True,
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
            self.assertTrue(summary["requested_distance_fade_enabled"])
            self.assertEqual(
                summary["requested_distance_fade_profile"]["profile_id"],
                "retail_gameplay_near_1400_600")
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
            self.assertEqual(
                summary["verified_written_distance_fade_profiles"],
                [{
                    "enabled": True,
                    "profile_id": "retail_gameplay_near_1400_600",
                    "visibility_limit": 1400.0,
                    "start": 800.0,
                    "length": 600.0,
                    "distance_space": "radial UA model units from eye",
                    "formula": (
                        "b=clamp(shade/256+max(0,(distance-800)/600),"
                        "0,1); screen-linear fixed brightness selects "
                        "SHADERMP row"),
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
                "destination_and_distance_fade_profile",
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
            self.assertTrue(manifest[0]["requested_distance_fade_enabled"])
            self.assertTrue(manifest[0]["effective_distance_fade_enabled"])
            self.assertIsNone(
                manifest[1]["effective_distance_fade_enabled"])

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
                    requested_distance_fade_enabled=False,
                    effective_distance_fade_enabled=False,
                    distance_fade_visibility_limit=1400.0,
                    distance_fade_start=800.0,
                    distance_fade_length=600.0,
                    distance_fade_distance_space=(
                        "radial UA model units from eye"),
                    distance_fade_formula=(
                        "b=clamp(shade/256+max(0,(distance-800)/600),0,1); "
                        "screen-linear fixed brightness selects SHADERMP row"),
                )],
                _warnings=[],
                _renderer_mode="textured_indexed",
                _flat_tracy_destination_mode="live_framebuffer",
                # The UI may retain a value while its control is inactive.
                _flat_tracy_forced_destination_index=220,
                _distance_fade_enabled=False,
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
            self.assertFalse(
                manifest[0]["requested_distance_fade_enabled"])
            self.assertFalse(
                manifest[0]["effective_distance_fade_enabled"])
            self.assertFalse(summary["requested_distance_fade_enabled"])
            self.assertFalse(
                run_info["indexed_distance_fade_enabled_requested"])

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
