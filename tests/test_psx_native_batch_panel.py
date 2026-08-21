from __future__ import annotations

from dataclasses import replace
import os
from pathlib import Path
import struct
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import QApplication

from assembly_viewer import VIEW_PRESET_ANGLES
from psx_native_assets import PsxNativeBuild, parse_psx_mesh_bytes
from snapshot_studio import SnapshotStudioWindow
from snapshot_studio.psx_batch_export import PsxNativeBatchProgress


def _synthetic_build(root: Path) -> PsxNativeBuild:
    header = bytearray(80)
    struct.pack_into("<I", header, 0, 3)
    struct.pack_into("<II", header, 0x38, 3, 1)
    struct.pack_into("<II", header, 0x40, 80, 116)
    vertices = b"".join(struct.pack("<iii", *vertex) for vertex in (
        (-65536, 65536, 0),
        (0, -65536, 32768),
        (65536, 65536, 0),
    ))
    face = bytearray(26)
    struct.pack_into("<4H", face, 4, 0, 1, 2, 2)
    face[12:20] = bytes((0, 0, 128, 255, 255, 0, 255, 0))
    struct.pack_into("<H", face, 20, 7)
    face[22:26] = bytes((10, 20, 30, 30))
    payload = bytes(header) + vertices + bytes(face)
    payload += b"\0" * ((-len(payload)) % 4)
    mesh = parse_psx_mesh_bytes(
        payload,
        logical_path="UNITMODL/UNIT.BIN",
        archive_ordinal=0,
        archive_offset=0x800,
    )
    return PsxNativeBuild(
        root=root,
        system_cnf_logical_path="SYSTEM.CNF",
        system_cnf_sha256="11" * 32,
        boot_executable_logical_path="SCES_019.63",
        boot_executable_sha256="22" * 32,
        unit_archive_logical_path="UNITMODL/UNIT.BIN",
        unit_archive_sha256="33" * 32,
        vehicle_roster_logical_path=None,
        vehicle_roster_sha256=None,
        vehicle_roster=(),
        meshes=(mesh,),
    )


class _ControlledExporter:
    instances: list["_ControlledExporter"] = []
    terminal_after = 2
    fail_on_step = False

    def __init__(self, build, config) -> None:
        self.build = build
        self.config = config
        self.step_calls = 0
        self.cancel_requested = False
        self._done = False
        self._cancelled = False
        self.__class__.instances.append(self)

    @property
    def done(self) -> bool:
        return self._done

    def _progress(self, state: str = "running") -> PsxNativeBatchProgress:
        completed = min(self.step_calls, len(self.config.views))
        return PsxNativeBatchProgress(
            state=state,
            total=len(self.config.views),
            completed=completed,
            written=completed,
            skipped_verified=0,
            cancelled=state == "cancelled",
            current_relative_png=(
                f"UNIT_BIN_000/{completed + 1:02d}.png"
                if state == "running" else None),
        )

    def start(self) -> PsxNativeBatchProgress:
        return self._progress()

    def step(self) -> PsxNativeBatchProgress:
        self.step_calls += 1
        if self.fail_on_step:
            raise RuntimeError("controlled native exporter failure")
        if self.cancel_requested:
            self._cancelled = True
            self._done = True
            return self._progress("cancelled")
        if self.step_calls >= self.terminal_after:
            self._done = True
            return self._progress("complete")
        return self._progress()

    def request_cancel(self) -> None:
        self.cancel_requested = True

    @property
    def result(self):
        if not self._done:
            raise RuntimeError("controlled exporter is not terminal")
        completed = min(self.step_calls, len(self.config.views))
        return SimpleNamespace(
            cancelled=self._cancelled,
            total=len(self.config.views),
            written=completed,
            skipped_verified=0,
            records=tuple(range(completed)),
            manifest_path=(
                Path(self.config.output_root)
                / "psx_native_batch_manifest.json"),
        )


class PsxNativeBatchPanelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        _ControlledExporter.instances.clear()
        _ControlledExporter.terminal_after = 2
        _ControlledExporter.fail_on_step = False

    def test_panel_has_bounded_controls_and_all_fixed_views_selected(self):
        window = SnapshotStudioWindow()
        try:
            panel = window.psx_native_batch_panel
            self.assertIs(panel, window.psx_batch_panel)
            self.assertIs(
                window._right_tabs.widget(2), window._psx_scroll)
            self.assertIs(window._psx_scroll.widget(), window._psx_panel)
            self.assertTrue(window._psx_panel.isAncestorOf(panel))
            self.assertFalse(window._snapshot_panel.isAncestorOf(panel))
            self.assertEqual(
                (panel.size_spin.minimum(), panel.size_spin.maximum()),
                (256, 4096),
            )
            self.assertEqual(
                (panel.zoom_spin.minimum(), panel.zoom_spin.maximum()),
                (25, 300),
            )
            self.assertEqual(
                tuple(panel.view_checks), tuple(VIEW_PRESET_ANGLES))
            self.assertEqual(panel.selected_views(), tuple(VIEW_PRESET_ANGLES))
            self.assertTrue(all(
                checkbox.isChecked()
                for checkbox in panel.view_checks.values()))
            self.assertFalse(panel.export_button.isEnabled())
        finally:
            window.close()

    def test_refresh_suggests_but_does_not_create_an_output_folder(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            window = SnapshotStudioWindow()
            try:
                build = _synthetic_build(root / "private-disc-tree")
                # Match open_psx_source(): the generic picker remembers the
                # selected disc root, while this panel must still suggest a
                # sibling output rather than a forbidden source child.
                window._last_directory = build.root
                window._set_psx_build(build)
                panel = window.psx_native_batch_panel
                suggested = Path(panel.output_edit.text())

                self.assertTrue(panel.export_button.isEnabled())
                self.assertEqual(suggested.parent, root)
                self.assertFalse(suggested.exists())
                self.assertIn("Native meshes: 1", panel.summary_label.text())
                self.assertIn("Views: 10/10", panel.summary_label.text())
                self.assertIn("Images: 10", panel.summary_label.text())
                self.assertEqual(
                    panel.texture_mode_combo.currentData(),
                    "topology_only",
                )
                panel.output_edit.setText(
                    str((root / "private-disc-tree") / "generated"))
                with patch(
                        "snapshot_studio.psx_batch_panel."
                        "QMessageBox.warning") as warning:
                    panel.start()
                self.assertTrue(warning.called)
                self.assertFalse(panel.is_running)
                self.assertFalse(
                    (root / "private-disc-tree" / "generated").exists())
            finally:
                window.close()

    def test_timer_callbacks_advance_one_step_and_restore_every_lock(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "not-created-by-controller"
            window = SnapshotStudioWindow()
            callbacks = []
            try:
                build = _synthetic_build(root / "disc")
                window._set_psx_build(build)
                panel = window.psx_native_batch_panel
                panel.output_edit.setText(str(output))
                panel.size_spin.setValue(768)
                panel.zoom_spin.setValue(125)
                panel.view_checks["Bottom"].setChecked(False)
                panel.view_checks["Isometric Back Left"].setChecked(False)

                with patch(
                        "snapshot_studio.psx_batch_panel."
                        "PsxNativeBatchExporter",
                        _ControlledExporter), patch(
                            "snapshot_studio.psx_batch_panel.QTimer.singleShot",
                            side_effect=lambda _delay, callback:
                            callbacks.append(callback)), patch(
                                "snapshot_studio.psx_batch_panel."
                                "QMessageBox.information"):
                    panel.start()

                    self.assertTrue(panel.is_running)
                    self.assertTrue(panel.cancel_button.isEnabled())
                    self.assertEqual(len(callbacks), 1)
                    self.assertFalse(window.psx_source_button.isEnabled())
                    self.assertFalse(window.psx_asset_tree.isEnabled())
                    self.assertFalse(window.psx_texture_set_combo.isEnabled())
                    self.assertFalse(window.snapshot_renderer_combo.isEnabled())
                    self.assertFalse(window.vp_batch_panel.isEnabled())
                    self.assertFalse(window.open_psx_action.isEnabled())
                    exporter = _ControlledExporter.instances[0]
                    self.assertIs(exporter.build, build)
                    self.assertEqual(exporter.config.width, 768)
                    self.assertEqual(exporter.config.height, 768)
                    self.assertEqual(exporter.config.zoom_percent, 125)
                    self.assertIsNone(exporter.config.texture_pack)
                    self.assertEqual(len(exporter.config.views), 8)
                    self.assertFalse(output.exists())

                    first = callbacks.pop(0)
                    first()
                    self.assertEqual(exporter.step_calls, 1)
                    self.assertTrue(panel.is_running)
                    self.assertEqual(len(callbacks), 1)

                    second = callbacks.pop(0)
                    second()
                    self.assertEqual(exporter.step_calls, 2)
                    self.assertFalse(panel.is_running)
                    self.assertTrue(window.psx_source_button.isEnabled())
                    self.assertTrue(window.psx_asset_tree.isEnabled())
                    self.assertFalse(window.psx_texture_set_combo.isEnabled())
                    self.assertTrue(window.snapshot_renderer_combo.isEnabled())
                    self.assertTrue(window.vp_batch_panel.isEnabled())
                    self.assertTrue(window.open_psx_action.isEnabled())
                    self.assertIsNotNone(panel.last_result)

                    # The old generation callback cannot advance a completed
                    # or replacement exporter.
                    first()
                    self.assertEqual(exporter.step_calls, 2)
            finally:
                window.close()

    def test_current_pack_choice_is_explicit_and_frozen(self):
        class FakeTexturePack:
            logical_path = "GFX/SET3GFX.BIN"
            source_sha256 = "55" * 32

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            pack = FakeTexturePack()
            build = replace(
                _synthetic_build(root / "disc"),
                texture_packs=(pack,),
            )
            window = SnapshotStudioWindow()
            callbacks = []
            try:
                # Bypass AssemblyWindow's display-only pack details; the panel
                # contract itself is isolated with the runtime type patched.
                window._psx_build = build
                window._psx_selected_texture_pack = pack
                panel = window.psx_native_batch_panel
                with patch(
                        "snapshot_studio.psx_batch_panel.PsxNativeTexturePack",
                        FakeTexturePack):
                    panel.refresh()
                    panel.texture_mode_combo.setCurrentIndex(1)
                    panel.output_edit.setText(str(root / "out"))
                    with patch(
                            "snapshot_studio.psx_batch_panel."
                            "PsxNativeBatchExporter",
                            _ControlledExporter), patch(
                                "snapshot_studio.psx_batch_panel.QTimer."
                                "singleShot",
                                side_effect=lambda _delay, callback:
                                callbacks.append(callback)):
                        panel.start()

                exporter = _ControlledExporter.instances[0]
                self.assertIs(exporter.config.texture_pack, pack)
                self.assertIn(
                    "SET3GFX.BIN", panel.texture_mode_combo.itemText(1))
                panel.request_cancel()
                callbacks.pop(0)()
                self.assertTrue(panel.last_result.cancelled)
            finally:
                window.close()

    def test_panel_completes_one_real_native_pair_with_owned_exporter(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "real-native-output"
            window = SnapshotStudioWindow()
            callbacks = []
            try:
                window._set_psx_build(_synthetic_build(root / "disc"))
                panel = window.psx_native_batch_panel
                panel.output_edit.setText(str(output))
                panel.size_spin.setValue(256)
                for name, checkbox in panel.view_checks.items():
                    checkbox.setChecked(name == "Front")

                with patch(
                        "snapshot_studio.psx_batch_panel.QTimer.singleShot",
                        side_effect=lambda _delay, callback:
                        callbacks.append(callback)), patch(
                            "snapshot_studio.psx_batch_panel."
                            "QMessageBox.information"):
                    panel.start()
                    self.assertTrue(panel.is_running)
                    self.assertFalse(output.exists())
                    self.assertEqual(len(callbacks), 1)
                    callbacks.pop(0)()

                self.assertFalse(panel.is_running)
                self.assertIsNone(panel.last_error)
                self.assertEqual(panel.last_result.total, 1)
                self.assertEqual(panel.last_result.written, 1)
                self.assertTrue(panel.last_result.manifest_path.is_file())
                self.assertEqual(len(list(output.rglob("*.png"))), 1)
                self.assertEqual(len(list(output.rglob("*.png.json"))), 1)

                # A byte-exact resume can finish during exporter.start(); it
                # must restore controls without scheduling a render callback.
                callbacks.clear()
                with patch(
                        "snapshot_studio.psx_batch_panel.QTimer.singleShot",
                        side_effect=lambda _delay, callback:
                        callbacks.append(callback)), patch(
                            "snapshot_studio.psx_batch_panel."
                            "QMessageBox.information"):
                    panel.start()
                self.assertFalse(panel.is_running)
                self.assertEqual(callbacks, [])
                self.assertEqual(panel.last_result.written, 0)
                self.assertEqual(panel.last_result.skipped_verified, 1)
                self.assertFalse(panel.cancel_button.isEnabled())
            finally:
                window.close()

    def test_exporter_start_failure_restores_controls(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output_file = root / "not-a-directory"
            output_file.write_text("collision", encoding="utf-8")
            window = SnapshotStudioWindow()
            try:
                window._set_psx_build(_synthetic_build(root / "disc"))
                panel = window.psx_native_batch_panel
                panel.output_edit.setText(str(output_file))
                with patch(
                        "snapshot_studio.psx_batch_panel."
                        "QMessageBox.critical") as critical:
                    panel.start()

                self.assertFalse(panel.is_running)
                self.assertIsNotNone(panel.last_error)
                self.assertTrue(critical.called)
                self.assertTrue(window.psx_source_button.isEnabled())
                self.assertTrue(window.psx_asset_tree.isEnabled())
                self.assertTrue(window.vp_batch_panel.isEnabled())
                self.assertTrue(window.open_psx_action.isEnabled())
                self.assertEqual(output_file.read_text("utf-8"), "collision")
            finally:
                window.close()

    def test_window_close_requests_safe_cancellation_then_restores(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            window = SnapshotStudioWindow()
            callbacks = []
            try:
                window._set_psx_build(_synthetic_build(root / "disc"))
                panel = window.psx_native_batch_panel
                panel.output_edit.setText(str(root / "out"))
                with patch(
                        "snapshot_studio.psx_batch_panel."
                        "PsxNativeBatchExporter",
                        _ControlledExporter), patch(
                            "snapshot_studio.psx_batch_panel.QTimer.singleShot",
                            side_effect=lambda _delay, callback:
                            callbacks.append(callback)):
                    panel.start()

                    event = QCloseEvent()
                    with patch(
                            "snapshot_studio.window.QMessageBox.information"):
                        window.closeEvent(event)
                    self.assertFalse(event.isAccepted())
                    self.assertTrue(
                        _ControlledExporter.instances[0].cancel_requested)
                    self.assertTrue(panel.is_running)

                    callbacks.pop(0)()
                    self.assertFalse(panel.is_running)
                    self.assertTrue(panel.last_result.cancelled)
                    self.assertTrue(window.psx_source_button.isEnabled())
                    self.assertTrue(window.vp_batch_panel.isEnabled())
            finally:
                window.close()

    def test_step_failure_restores_controls_and_records_error(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            window = SnapshotStudioWindow()
            callbacks = []
            try:
                window._set_psx_build(_synthetic_build(root / "disc"))
                panel = window.psx_native_batch_panel
                panel.output_edit.setText(str(root / "out"))
                _ControlledExporter.fail_on_step = True
                with patch(
                        "snapshot_studio.psx_batch_panel."
                        "PsxNativeBatchExporter",
                        _ControlledExporter), patch(
                            "snapshot_studio.psx_batch_panel.QTimer.singleShot",
                            side_effect=lambda _delay, callback:
                            callbacks.append(callback)), patch(
                                "snapshot_studio.psx_batch_panel."
                                "QMessageBox.critical") as critical:
                    panel.start()
                    callbacks.pop(0)()

                self.assertFalse(panel.is_running)
                self.assertIsInstance(panel.last_error, RuntimeError)
                self.assertTrue(critical.called)
                self.assertTrue(window.psx_source_button.isEnabled())
                self.assertTrue(window.psx_asset_tree.isEnabled())
                self.assertFalse(window.psx_texture_set_combo.isEnabled())
                self.assertTrue(window.snapshot_renderer_combo.isEnabled())
                self.assertTrue(window.vp_batch_panel.isEnabled())
                self.assertTrue(window.open_psx_action.isEnabled())
            finally:
                window.close()


if __name__ == "__main__":
    unittest.main()
