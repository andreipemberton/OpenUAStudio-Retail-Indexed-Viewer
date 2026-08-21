import os
import hashlib
import json
import struct
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPointF, QSize, Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QMessageBox,
    QTabWidget,
)

from editor_widgets import ViewportWidthScrollArea

from asset_family import AssetFamily, FamilyObject
from assembly_window import AssemblyWindow, PSX_NATIVE_ZERO_VISIBLE_HINT
from assembly_viewer import PSX_NATIVE_VIEW_MODE
from base_parser import BaseObject
from psx_native_assets import PsxNativeBuild, parse_psx_mesh_bytes
from psx_native_textures import (
    MATERIAL_SLOT_COUNT,
    PIXEL_BANK_COUNT,
    SECTOR_PADDED_EMPTY_ALLOCATION,
    SECTOR_PADDED_EMPTY_MARKER,
    SECTOR_PADDED_FULL_ALLOCATION,
    ZERO_RECORD_HEADER,
    parse_late_setgfx_bytes,
    parse_sector_padded_setgfx_bytes,
)
from sklt_parser import SkltModel
from snapshot_studio import SnapshotStudioWindow


def _synthetic_psx_texture_pack(
        logical_path: str = "GFX/SET1GFX.BIN"):
    records = []
    for selector in range(MATERIAL_SLOT_COUNT):
        palette = [0] * 16
        palette[1] = 0x001F
        record = bytearray(
            ZERO_RECORD_HEADER + struct.pack("<16H", *palette))
        if selector < PIXEL_BANK_COUNT:
            record.extend(bytes((0x11,)) * 0x2000)
        records.append(bytes(record))
    return parse_late_setgfx_bytes(
        b"".join(records), logical_path=logical_path)


def _synthetic_psx_pack_without_selector_seven(
        logical_path: str = "GFX/SET2GFX.BIN"):
    """Return an exact 77-material sector pack with selector 7 absent."""

    records = []
    populated = set(range(78)) - {7}
    for selector in range(MATERIAL_SLOT_COUNT):
        if selector in populated:
            palette = [0] * 16
            palette[1] = 0x001F
            record = bytearray(
                ZERO_RECORD_HEADER + struct.pack("<16H", *palette))
            record.extend(bytes((0x11,)) * 0x2000)
            record.extend(
                bytes((0xBA,))
                * (SECTOR_PADDED_FULL_ALLOCATION - len(record)))
        else:
            record = bytearray(SECTOR_PADDED_EMPTY_MARKER)
            record.extend(
                bytes((0xBA,))
                * (SECTOR_PADDED_EMPTY_ALLOCATION - len(record)))
        records.append(bytes(record))
    return parse_sector_padded_setgfx_bytes(
        b"".join(records), logical_path=logical_path)


def _synthetic_psx_build(
        root: Path = Path("."), *, texture_packs=()) -> PsxNativeBuild:
    header = bytearray(80)
    struct.pack_into("<I", header, 0, 3)
    struct.pack_into("<II", header, 0x38, 3, 1)
    struct.pack_into("<II", header, 0x40, 80, 116)
    vertices = b"".join(struct.pack("<iii", *vertex) for vertex in (
        (-65536, 65536, 0), (0, -65536, 32768), (65536, 65536, 0)))
    face = bytearray(26)
    struct.pack_into("<4H", face, 4, 0, 1, 2, 2)
    face[12:20] = bytes((0, 0, 128, 255, 255, 0, 255, 0))
    struct.pack_into("<H", face, 20, 7)
    face[22:26] = bytes((10, 20, 30, 30))
    payload = bytes(header) + vertices + bytes(face)
    payload += b"\0" * ((-len(payload)) % 4)
    mesh = parse_psx_mesh_bytes(
        payload, logical_path="UNITMODL/UNIT.BIN",
        archive_ordinal=0, archive_offset=0x800)
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
        texture_packs=tuple(texture_packs),
    )


def _synthetic_pc_family(
        path: Path = Path(r"C:\pc-assets\VEHICLE.BASE"), *,
        center: tuple[float, float, float] = (110.0, 20.0, 0.0)) \
        -> AssetFamily:
    """Return one far-from-origin PC triangle with a measurable fit state."""

    x, y, z = center
    model = SkltModel(
        source_name="VEHICLE.SKLT",
        points=[
            (x - 20.0, y - 10.0, z),
            (x + 20.0, y - 10.0, z),
            (x, y + 10.0, z),
        ],
        polygons=[[0, 1, 2]],
        parsed_polygon_count=1,
    )
    root = FamilyObject(
        base_object=BaseObject(
            name="VEHICLE", skeleton_name="VEHICLE.SKLT"),
        skeleton=model,
        owner_path="root",
    )
    return AssetFamily(base_path=path, root_object=root)


class SnapshotStudioTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def _load_synthetic_native(
            self, window: SnapshotStudioWindow, *, root: Path = Path(".")) \
            -> PsxNativeBuild:
        build = _synthetic_psx_build(root)
        window._set_psx_build(build)
        window.psx_asset_tree.setCurrentItem(
            window.psx_asset_tree.topLevelItem(0))
        window._load_selected_psx_asset()
        self.app.processEvents()
        self.assertEqual(window.viewport.source_kind, "psx_native")
        self.assertIs(window.viewport.psx_mesh, build.meshes[0])
        return build

    def _enter_standard_snapshot(self, window: AssemblyWindow) -> None:
        window._right_tabs.setCurrentWidget(window._visuals_tabs)
        window._visuals_tabs.setCurrentWidget(window._snapshot_panel)
        self.app.processEvents()
        self.assertTrue(window._snapshot_mode_active)

    def _leave_standard_snapshot(self, window: AssemblyWindow) -> None:
        window._right_tabs.setCurrentWidget(window._resources_tabs)
        self.app.processEvents()
        self.assertFalse(window._snapshot_mode_active)

    def test_workspace_has_snapshot_bas_manager_and_psx_archive_tabs(self):
        window = SnapshotStudioWindow()
        try:
            labels = [
                window._right_tabs.tabText(index)
                for index in range(window._right_tabs.count())
            ]
            self.assertEqual(
                labels, ["Snapshot", "BAS Manager", "PSX Archive"])
            self.assertIs(
                window._right_tabs.widget(0), window._snapshot_scroll)
            self.assertIsInstance(
                window._snapshot_scroll, ViewportWidthScrollArea)
            self.assertIs(
                window._snapshot_scroll.widget(), window._snapshot_panel)
            self.assertTrue(window._snapshot_scroll.widgetResizable())
            self.assertEqual(
                window._snapshot_scroll.horizontalScrollBarPolicy(),
                Qt.ScrollBarPolicy.ScrollBarAlwaysOff,
            )
            self.assertEqual(
                window._snapshot_scroll.verticalScrollBarPolicy(),
                Qt.ScrollBarPolicy.ScrollBarAsNeeded,
            )
            self.assertIs(window._right_tabs.widget(1), window._bas_panel)
            self.assertIs(
                window._right_tabs.widget(2), window._psx_scroll)
            self.assertIs(
                window._psx_scroll.widget(), window._psx_panel)
            self.assertTrue(window._psx_scroll.widgetResizable())
            self.assertEqual(
                window._psx_scroll.horizontalScrollBarPolicy(),
                Qt.ScrollBarPolicy.ScrollBarAlwaysOff,
            )
            self.assertEqual(
                window._psx_scroll.verticalScrollBarPolicy(),
                Qt.ScrollBarPolicy.ScrollBarAsNeeded,
            )
            self.assertFalse(
                isinstance(window._right_tabs.widget(0), QTabWidget))
            self.assertFalse(
                isinstance(window._right_tabs.widget(1), QTabWidget))
        finally:
            window.close()

    def test_workspace_remains_snapshot_view_only_on_both_tabs(self):
        window = SnapshotStudioWindow()
        try:
            self.assertTrue(window._snapshot_mode_active)
            self.assertFalse(window.viewport.is_edit_mode)
            self.assertFalse(window.edit_toggle_action.isEnabled())
            self.assertFalse(window.edit_toggle_action.isVisible())
            self.assertEqual(window.edit_toggle_action.shortcuts(), [])
            self.assertFalse(window.edit_menu.menuAction().isVisible())

            window._right_tabs.setCurrentWidget(window._bas_panel)
            self.app.processEvents()
            self.assertTrue(window._snapshot_mode_active)
            self.assertFalse(window.viewport.is_edit_mode)
            self.assertFalse(window._editing_allowed())

            window.edit_toggle_action.setChecked(True)
            self.app.processEvents()
            self.assertFalse(window.edit_toggle_action.isChecked())
            self.assertFalse(window.viewport.is_edit_mode)
        finally:
            window.close()

    def test_viewport_context_menu_contains_no_edit_commands(self):
        window = SnapshotStudioWindow()
        try:
            menu = window._create_viewport_context_menu()
            self.assertEqual(
                [action.text() for action in menu.actions()],
                ["Reset camera"],
            )
        finally:
            window.close()

    def test_tools_menu_hides_other_workspace_launchers(self):
        window = SnapshotStudioWindow()
        try:
            self.assertFalse(window.wireframe_editor_action.isVisible())
            self.assertFalse(window.collision_editor_action.isVisible())
            self.assertFalse(window.map_editor_action.isVisible())
            self.assertFalse(window.mapping_repair_action.isVisible())
            self.assertFalse(window.integrated_editors_separator.isVisible())
        finally:
            window.close()

    def test_view_menu_hides_unavailable_snapshot_filters(self):
        window = SnapshotStudioWindow()
        try:
            for action in (
                    window.sen_check,
                    window.owner_bounds_check,
                    window.wire_check,
                    window.axes_check,
                    window.grid_check,
                    window.overlay_check,
                    window.mapping_diag_check):
                self.assertFalse(action.isVisible())
            self.assertTrue(window.cull_check.isVisible())
            self.assertTrue(window.reset_camera_action.isVisible())
        finally:
            window.close()

    def test_flat_tracy_destination_selector_remains_in_focused_workspace(self):
        window = SnapshotStudioWindow()
        try:
            policy = window.snapshot_tracy_destination_combo
            index_spin = window.snapshot_tracy_destination_index_spin
            self.assertTrue(window._snapshot_panel.isAncestorOf(policy))
            self.assertEqual(policy.currentData(), "live_framebuffer")
            self.assertEqual((index_spin.minimum(), index_spin.maximum()),
                             (0, 255))

            indexed = window.snapshot_renderer_combo.findData(
                "textured_indexed")
            window.snapshot_renderer_combo.setCurrentIndex(indexed)
            forced = policy.findData("forced_diagnostic")
            policy.setCurrentIndex(forced)
            index_spin.setValue(31)
            self.app.processEvents()

            self.assertTrue(index_spin.isEnabled())
            self.assertEqual(
                window.viewport.flat_tracy_forced_destination_index, 31)
            self.assertFalse(window._editing_allowed())
        finally:
            window.close()

    def test_animation_and_distance_fade_checkboxes_are_focused_controls(self):
        window = SnapshotStudioWindow()
        try:
            self.assertIsInstance(window.play_button, QCheckBox)
            self.assertEqual(window.play_button.text(), "Enable animations")
            self.assertFalse(window.play_button.isChecked())
            self.assertIn(
                "PC/OpenUA renderers", window.play_button.toolTip())
            self.assertTrue(
                window._snapshot_panel.isAncestorOf(window.play_button))

            fade = window.snapshot_distance_fade_check
            self.assertTrue(window._snapshot_panel.isAncestorOf(fade))
            self.assertFalse(fade.isChecked())
            self.assertFalse(fade.isEnabled())
            indexed = window.snapshot_renderer_combo.findData(
                "textured_indexed")
            window.snapshot_renderer_combo.setCurrentIndex(indexed)
            self.app.processEvents()
            self.assertTrue(fade.isEnabled())
        finally:
            window.close()

    def test_native_psx_profile_requires_and_renders_a_native_asset(self):
        window = SnapshotStudioWindow()
        try:
            combo = window.snapshot_renderer_combo
            alternative_format = next((
                index for index in range(window.snapshot_format_combo.count())
                if window.snapshot_format_combo.itemData(index) != "png"
            ), -1)
            if alternative_format >= 0:
                window.snapshot_format_combo.setCurrentIndex(
                    alternative_format)
            previous_format = window.snapshot_format_combo.currentData()
            self.assertEqual(combo.findData("textured_psx_prototype"), -1)
            profile = combo.findData(PSX_NATIVE_VIEW_MODE)
            self.assertGreaterEqual(profile, 0)
            self.assertEqual(
                combo.itemText(profile),
                "Native PSX assets (experimental)",
            )
            self.assertTrue(
                window._snapshot_panel.isAncestorOf(
                    window.snapshot_renderer_notice))

            # Values chosen for retail rendering remain a saved preference
            # while its controls are unavailable for a native PSX source.
            retail = combo.findData("textured_indexed")
            combo.setCurrentIndex(retail)
            window.snapshot_distance_fade_check.setChecked(True)
            forced = window.snapshot_tracy_destination_combo.findData(
                "forced_diagnostic")
            window.snapshot_tracy_destination_combo.setCurrentIndex(forced)
            window.snapshot_tracy_destination_index_spin.setValue(83)

            combo.setCurrentIndex(profile)
            self.app.processEvents()
            self.assertEqual(combo.currentData(), "textured_indexed")
            self.assertEqual(window.viewport.source_kind, "none")

            window._set_psx_build(_synthetic_psx_build())
            window.psx_asset_tree.setCurrentItem(
                window.psx_asset_tree.topLevelItem(0))
            window._load_selected_psx_asset()
            self.app.processEvents()
            notice = window.snapshot_renderer_notice
            self.assertFalse(notice.isHidden())
            self.assertIn("native PSW/PSV/PW3 geometry", notice.text())
            self.assertIn("never used", notice.text())
            self.assertIn("SETnGFX selector textures", notice.text())
            self.assertIn("PW3 bit 14 may bypass", notice.text())
            self.assertIn("PSW/PSV always tests", notice.text())
            self.assertIn("PV2 files are inventoried", notice.text())
            self.assertEqual(window.viewport.source_kind, "psx_native")
            self.assertIsNone(window.viewport._family_ref)
            self.assertFalse(window.snapshot_distance_fade_check.isEnabled())
            self.assertTrue(window.snapshot_distance_fade_check.isChecked())
            self.assertFalse(
                window.snapshot_tracy_destination_combo.isEnabled())
            self.assertFalse(
                window.snapshot_tracy_destination_index_spin.isEnabled())
            self.assertEqual(
                window.snapshot_tracy_destination_combo.currentData(),
                "forced_diagnostic",
            )
            self.assertEqual(
                window.snapshot_tracy_destination_index_spin.value(), 83)
            self.assertEqual(
                window._snapshot_renderer_filename_suffix(),
                "_PSX_NATIVE_V1",
            )
            self.assertEqual(
                window.viewport.view_mode, PSX_NATIVE_VIEW_MODE)
            self.assertEqual(
                window.snapshot_format_combo.currentData(), "png")
            self.assertFalse(window.snapshot_format_combo.isEnabled())
            self.assertIn(
                "PNG plus .png.json",
                window.snapshot_format_combo.toolTip())
            self.assertFalse(window.play_button.isEnabled())
            self.assertIn(
                "VANM animation is never applied",
                window.play_button.toolTip())
            self.assertFalse(window._editing_allowed())

            # The loaded source, not a transient combo mismatch, owns the
            # native PNG-plus-provenance policy.
            combo.blockSignals(True)
            combo.setCurrentIndex(combo.findData("textured"))
            combo.blockSignals(False)
            window._update_psx_prototype_renderer_notice()
            self.assertEqual(window.viewport.source_kind, "psx_native")
            self.assertEqual(
                window.snapshot_format_combo.currentData(), "png")
            self.assertFalse(window.snapshot_format_combo.isEnabled())
            window._forget_psx_source()
            self.assertTrue(window.snapshot_format_combo.isEnabled())
            self.assertEqual(
                window.snapshot_format_combo.currentData(), previous_format)
        finally:
            window.close()

    def test_loading_pc_family_clears_every_native_snapshot_ui_state(self):
        window = SnapshotStudioWindow()
        try:
            jpg_index = window.snapshot_format_combo.findData("jpg")
            self.assertGreaterEqual(jpg_index, 0)
            window.snapshot_format_combo.setCurrentIndex(jpg_index)
            window._pc_view_mode = "textured_indexed"
            self._load_synthetic_native(window)

            self.assertFalse(window.snapshot_format_combo.isEnabled())
            self.assertEqual(window.snapshot_format_combo.currentData(), "png")
            self.assertFalse(window.snapshot_renderer_notice.isHidden())
            self.assertFalse(window.snapshot_distance_fade_check.isEnabled())
            self.assertFalse(
                window.snapshot_tracy_destination_combo.isEnabled())
            self.assertTrue(hasattr(window, "_snapshot_pc_image_format"))
            self.assertIn(
                "Source: native PlayStation prototype asset",
                window._object_info_asset_lines)
            window._native_zero_visible_last = True
            window._native_visibility_timer.start(10_000)

            pc_path = Path(r"C:\pc-assets\VEHICLE.BASE")
            family = AssetFamily(base_path=pc_path)
            with patch("assembly_window.load_asset_family", return_value=family):
                window.open_base(pc_path, confirm_discard=False)
            self.app.processEvents()

            self.assertEqual(window.viewport.source_kind, "pc_openua")
            self.assertEqual(window.viewport.view_mode, "textured_indexed")
            self.assertEqual(
                window.snapshot_renderer_combo.currentData(),
                "textured_indexed")
            self.assertEqual(window.mode_combo.currentData(), "textured_indexed")
            self.assertTrue(window.snapshot_format_combo.isEnabled())
            self.assertEqual(window.snapshot_format_combo.currentData(), "jpg")
            self.assertTrue(window.snapshot_renderer_notice.isHidden())
            self.assertTrue(window.snapshot_distance_fade_check.isEnabled())
            self.assertTrue(
                window.snapshot_tracy_destination_combo.isEnabled())
            self.assertFalse(hasattr(window, "_snapshot_pc_image_format"))
            self.assertIn("VEHICLE.BASE", window.windowTitle())
            self.assertEqual(
                window._object_info_asset_lines, ["No asset selected."])
            self.assertNotIn(
                "native PlayStation",
                "\n".join(window._object_info_asset_lines))
            self.assertFalse(window._native_visibility_timer.isActive())
            self.assertIsNone(window._native_zero_visible_last)
        finally:
            window.close()

    def test_native_to_pc_renderer_transitions_restore_source_identity_ui(self):
        window = SnapshotStudioWindow()
        try:
            family = AssetFamily(
                base_path=Path(r"C:\pc-assets\VEHICLE.BASE"))
            window._set_family(family)
            expected_pc_title = window.windowTitle()

            for combo, mode in (
                    (window.snapshot_renderer_combo, "textured_indexed"),
                    (window.mode_combo, "textured")):
                self._load_synthetic_native(
                    window, root=Path(r"C:\psx-extract"))
                expected_native_title = window.windowTitle()
                expected_native_info = tuple(window._object_info_asset_lines)
                self.assertIn(
                    "Source: native PlayStation prototype asset",
                    window._object_info_asset_lines)
                self.assertIn("psx-extract", window.windowTitle())
                window._native_visibility_timer.start(10_000)

                combo.setCurrentIndex(combo.findData(mode))
                self.app.processEvents()

                self.assertEqual(window.viewport.source_kind, "pc_openua")
                self.assertEqual(window.viewport.view_mode, mode)
                self.assertEqual(window._pc_view_mode, mode)
                self.assertEqual(window.windowTitle(), expected_pc_title)
                self.assertEqual(
                    window._object_info_asset_lines,
                    ["No asset selected."],
                )
                self.assertFalse(window._native_visibility_timer.isActive())
                self.assertIsNone(window._native_zero_visible_last)
                self.assertEqual(
                    window.snapshot_renderer_combo.currentData(), mode)
                self.assertEqual(window.mode_combo.currentData(), mode)

                combo.setCurrentIndex(combo.findData(PSX_NATIVE_VIEW_MODE))
                self.assertIsNone(window._native_zero_visible_last)
                self.assertTrue(window._native_visibility_timer.isActive())
                self.app.processEvents()

                self.assertEqual(window.viewport.source_kind, "psx_native")
                self.assertEqual(
                    window.viewport.view_mode, PSX_NATIVE_VIEW_MODE)
                self.assertEqual(window.windowTitle(), expected_native_title)
                self.assertEqual(
                    tuple(window._object_info_asset_lines),
                    expected_native_info,
                )
                self.assertIsNone(window.viewport.psx_texture_pack)
                self.assertEqual(
                    window.snapshot_renderer_combo.currentData(),
                    PSX_NATIVE_VIEW_MODE)
                self.assertEqual(
                    window.mode_combo.currentData(), PSX_NATIVE_VIEW_MODE)

        finally:
            window.close()

    def test_native_toolbar_reentry_preserves_diagnostic_camera(self):
        # The standard workbench toolbar is enabled outside Snapshot mode.
        # Returning from a native diagnostic to the exact installed native
        # scene must change only its renderer, not reload/refit its camera.
        window = AssemblyWindow()
        try:
            self._load_synthetic_native(window)
            for ordinal, diagnostic_mode in enumerate(
                    ("wireframe", "materials"), start=1):
                with self.subTest(diagnostic_mode=diagnostic_mode):
                    window.mode_combo.setCurrentIndex(
                        window.mode_combo.findData(diagnostic_mode))
                    self.assertEqual(
                        window.viewport.view_mode, diagnostic_mode)
                    window.viewport._yaw = 30.0 + ordinal
                    window.viewport._pitch = -18.0 - ordinal
                    window.viewport._zoom = 1.75 + ordinal / 10
                    window.viewport._pan = QPointF(
                        ordinal / 10, -ordinal / 5)
                    expected_camera = window.viewport.snapshot_camera_info

                    window.mode_combo.setCurrentIndex(
                        window.mode_combo.findData(PSX_NATIVE_VIEW_MODE))
                    self.app.processEvents()

                    self.assertEqual(
                        window.viewport.source_kind, "psx_native")
                    self.assertEqual(
                        window.viewport.view_mode, PSX_NATIVE_VIEW_MODE)
                    self.assertEqual(
                        window.viewport.snapshot_camera_info, expected_camera)
        finally:
            window.close()

    def test_native_to_pc_renderer_transitions_fail_closed_without_family(self):
        window = SnapshotStudioWindow()
        try:
            self._load_synthetic_native(
                window, root=Path(r"C:\psx-extract"))
            expected_title = window.windowTitle()
            expected_info = tuple(window._object_info_asset_lines)
            expected_pc_mode = window._pc_view_mode

            with patch.object(window, "_notify") as notify:
                for combo in (
                        window.snapshot_renderer_combo, window.mode_combo):
                    combo.setCurrentIndex(combo.findData("textured"))
                    self.app.processEvents()

                    self.assertEqual(
                        window.viewport.source_kind, "psx_native")
                    self.assertEqual(
                        window.viewport.view_mode, PSX_NATIVE_VIEW_MODE)
                    self.assertEqual(
                        combo.currentData(), PSX_NATIVE_VIEW_MODE)
                    self.assertEqual(window._pc_view_mode, expected_pc_mode)
                    self.assertEqual(window.windowTitle(), expected_title)
                    self.assertEqual(
                        tuple(window._object_info_asset_lines), expected_info)
                    self.assertIn(
                        "Source: native PlayStation prototype asset",
                        window._object_info_asset_lines)

                self.assertEqual(notify.call_count, 2)
                for call in notify.call_args_list:
                    self.assertIn("No PC/OpenUA AssetFamily", call.args[0])
        finally:
            window.close()

    def test_snapshot_renderer_refusal_restores_source_truth_from_wireframe(self):
        # PC source: a refused native renderer must restore the saved PC
        # textured renderer even though the live viewport remains wireframe.
        window = SnapshotStudioWindow()
        try:
            window._set_family(AssetFamily(
                base_path=Path(r"C:\pc-assets\VEHICLE.BASE")))
            window.snapshot_renderer_combo.setCurrentIndex(
                window.snapshot_renderer_combo.findData("textured_indexed"))
            window.mode_combo.setCurrentIndex(
                window.mode_combo.findData("wireframe"))
            self.app.processEvents()
            expected_camera = window.viewport.snapshot_camera_info
            expected_title = window.windowTitle()
            expected_info = tuple(window._object_info_asset_lines)

            with patch.object(window, "_notify") as notify:
                window.snapshot_renderer_combo.setCurrentIndex(
                    window.snapshot_renderer_combo.findData(
                        PSX_NATIVE_VIEW_MODE))
                self.app.processEvents()

            self.assertEqual(window.viewport.source_kind, "pc_openua")
            self.assertEqual(window.viewport.view_mode, "wireframe")
            self.assertEqual(window.mode_combo.currentData(), "wireframe")
            self.assertEqual(
                window.snapshot_renderer_combo.currentData(),
                "textured_indexed")
            self.assertEqual(window._pc_view_mode, "textured_indexed")
            self.assertEqual(
                window.viewport.snapshot_camera_info, expected_camera)
            self.assertEqual(window.windowTitle(), expected_title)
            self.assertEqual(
                tuple(window._object_info_asset_lines), expected_info)
            notify.assert_called_once()
        finally:
            window.close()

        # Native source: a refused PC renderer must restore the native
        # Snapshot selector while leaving the live native wireframe untouched.
        window = SnapshotStudioWindow()
        try:
            self._load_synthetic_native(
                window, root=Path(r"C:\psx-extract"))
            window.mode_combo.setCurrentIndex(
                window.mode_combo.findData("wireframe"))
            self.app.processEvents()
            expected_camera = window.viewport.snapshot_camera_info
            expected_title = window.windowTitle()
            expected_info = tuple(window._object_info_asset_lines)
            expected_pc_mode = window._pc_view_mode

            with patch.object(window, "_notify") as notify:
                window.snapshot_renderer_combo.setCurrentIndex(
                    window.snapshot_renderer_combo.findData(
                        "textured_indexed"))
                self.app.processEvents()

            self.assertEqual(window.viewport.source_kind, "psx_native")
            self.assertEqual(window.viewport.view_mode, "wireframe")
            self.assertEqual(window.mode_combo.currentData(), "wireframe")
            self.assertEqual(
                window.snapshot_renderer_combo.currentData(),
                PSX_NATIVE_VIEW_MODE)
            self.assertEqual(window._pc_view_mode, expected_pc_mode)
            self.assertEqual(
                window.viewport.snapshot_camera_info, expected_camera)
            self.assertEqual(window.windowTitle(), expected_title)
            self.assertEqual(
                tuple(window._object_info_asset_lines), expected_info)
            notify.assert_called_once()
        finally:
            window.close()

    def test_native_renderer_reentry_rejects_inconsistent_selection(self):
        window = SnapshotStudioWindow()
        try:
            window._set_family(AssetFamily(
                base_path=Path(r"C:\pc-assets\VEHICLE.BASE")))
            build = self._load_synthetic_native(
                window, root=Path(r"C:\psx-extract"))
            window.snapshot_renderer_combo.setCurrentIndex(
                window.snapshot_renderer_combo.findData("textured_indexed"))
            self.app.processEvents()
            expected_title = window.windowTitle()
            expected_info = tuple(window._object_info_asset_lines)
            selected_mesh = build.meshes[0]

            foreign_mesh = _synthetic_psx_build(
                Path(r"C:\other-psx-extract")).meshes[0]
            corruptions = (
                (foreign_mesh, None),
                (selected_mesh, object()),
            )
            for poisoned_mesh, poisoned_pack in corruptions:
                with self.subTest(
                        foreign_mesh=poisoned_mesh is foreign_mesh,
                        invalid_pack=poisoned_pack is not None):
                    window._psx_selected_mesh = poisoned_mesh
                    window._psx_selected_texture_pack = poisoned_pack
                    with patch.object(window, "_notify") as notify:
                        window.snapshot_renderer_combo.setCurrentIndex(
                            window.snapshot_renderer_combo.findData(
                                PSX_NATIVE_VIEW_MODE))
                        self.app.processEvents()

                    self.assertEqual(
                        window.viewport.source_kind, "pc_openua")
                    self.assertEqual(
                        window.viewport.view_mode, "textured_indexed")
                    self.assertEqual(
                        window.snapshot_renderer_combo.currentData(),
                        "textured_indexed")
                    self.assertEqual(window.windowTitle(), expected_title)
                    self.assertEqual(
                        tuple(window._object_info_asset_lines), expected_info)
                    notify.assert_called_once()
                    self.assertIn(
                        "exact native source identity",
                        notify.call_args.args[0])

            window._psx_selected_mesh = selected_mesh
            window._psx_selected_texture_pack = None
        finally:
            window.close()

    def test_native_window_load_refreshes_current_view_after_fit(self):
        window = SnapshotStudioWindow()
        window.viewport.resize(800, 600)
        try:
            build = _synthetic_psx_build(Path(r"C:\psx-extract"))
            window._set_psx_build(build)
            window.psx_asset_tree.setCurrentItem(
                window.psx_asset_tree.topLevelItem(0))
            window.snapshot_view_combo.setCurrentText("Current View")
            window._load_selected_psx_asset()

            fitted = window.viewport.snapshot_camera_info
            baseline = window.viewport._snapshot_current_camera
            self.assertIsNotNone(baseline)
            self.assertEqual(fitted["yaw"], -35.0)
            self.assertEqual(fitted["pitch"], 20.0)
            self.assertAlmostEqual(
                fitted["zoom"], 1.1496046750348836, places=12)
            self.assertEqual(fitted["pan"], [0.0, 0.0])
            self.assertEqual(fitted["center"], [0.0, 0.0, 0.25])
            self.assertEqual(fitted["scale"], 1.0)
            self.assertEqual(baseline["yaw"], fitted["yaw"])
            self.assertEqual(baseline["pitch"], fitted["pitch"])
            self.assertEqual(baseline["zoom"], fitted["zoom"])
            self.assertEqual(
                [baseline["pan"].x(), baseline["pan"].y()],
                fitted["pan"])
            self.assertEqual(list(baseline["center"]), fitted["center"])
            self.assertEqual(baseline["scale"], fitted["scale"])

            # A named preset remains authoritative after a later activation,
            # while Current View returns to that activation's fitted baseline.
            window.snapshot_view_combo.setCurrentText("Back")
            window._load_selected_psx_asset()
            named = window.viewport.snapshot_camera_info
            refreshed = window.viewport._snapshot_current_camera
            self.assertEqual((named["yaw"], named["pitch"]), (180.0, 0.0))
            self.assertEqual(
                (refreshed["yaw"], refreshed["pitch"]), (-35.0, 20.0))
            window.snapshot_view_combo.setCurrentText("Current View")
            current = window.viewport.snapshot_camera_info
            self.assertEqual(current["yaw"], refreshed["yaw"])
            self.assertEqual(current["pitch"], refreshed["pitch"])
            self.assertEqual(current["zoom"], refreshed["zoom"])
            self.assertEqual(
                current["pan"],
                [refreshed["pan"].x(), refreshed["pan"].y()])
            self.assertEqual(current["center"], list(refreshed["center"]))
            self.assertEqual(current["scale"], refreshed["scale"])
        finally:
            window.close()

    def test_pc_window_load_refreshes_current_view_and_named_preset(self):
        window = SnapshotStudioWindow()
        window.viewport.resize(800, 600)
        try:
            first_path = Path(r"C:\pc-assets\VEHICLE.BASE")
            first = _synthetic_pc_family(first_path)
            window.snapshot_view_combo.setCurrentText("Current View")
            with patch(
                    "assembly_window.load_asset_family", return_value=first):
                window.open_base(first_path, confirm_discard=False)

            fitted = window.viewport.snapshot_camera_info
            baseline = window.viewport._snapshot_current_camera
            self.assertEqual(fitted["center"], [110.0, 20.0, 0.0])
            self.assertEqual(fitted["scale"], 0.05)
            self.assertEqual((fitted["yaw"], fitted["pitch"]), (-35.0, 20.0))
            self.assertIsNotNone(baseline)
            self.assertEqual(list(baseline["center"]), fitted["center"])
            self.assertEqual(baseline["scale"], fitted["scale"])

            # A named preset is reapplied to the newly loaded family, while
            # Current View remains the family activation's neutral fit.
            window.snapshot_view_combo.setCurrentText("Back")
            second_path = Path(r"C:\pc-assets\SECOND.BASE")
            second = _synthetic_pc_family(
                second_path, center=(-60.0, 45.0, 5.0))
            with patch(
                    "assembly_window.load_asset_family", return_value=second):
                window.open_base(second_path, confirm_discard=False)
            named = window.viewport.snapshot_camera_info
            refreshed = window.viewport._snapshot_current_camera
            self.assertEqual((named["yaw"], named["pitch"]), (180.0, 0.0))
            self.assertEqual(named["center"], [-60.0, 45.0, 5.0])
            self.assertEqual(
                (refreshed["yaw"], refreshed["pitch"]), (-35.0, 20.0))
            self.assertEqual(
                list(refreshed["center"]), [-60.0, 45.0, 5.0])

            window.snapshot_view_combo.setCurrentText("Current View")
            current = window.viewport.snapshot_camera_info
            self.assertEqual(current["yaw"], refreshed["yaw"])
            self.assertEqual(current["pitch"], refreshed["pitch"])
            self.assertEqual(current["zoom"], refreshed["zoom"])
            self.assertEqual(current["center"], list(refreshed["center"]))
            self.assertEqual(current["scale"], refreshed["scale"])
        finally:
            window.close()

    def test_snapshot_camera_controls_follow_preset_zoom_reload_and_reset(self):
        window = SnapshotStudioWindow()
        window.viewport.resize(800, 600)
        try:
            self._load_synthetic_native(window)
            self.assertFalse(window.viewport.can_reset_camera)
            self.assertFalse(window.reset_camera_action.isEnabled())

            window.snapshot_view_combo.setCurrentText("Back")
            self.assertTrue(window.viewport.can_reset_camera)
            self.assertTrue(window.reset_camera_action.isEnabled())
            self.assertEqual(
                window.viewport.camera_orientation, (180.0, 0.0))

            window._reset_view_and_gizmo()
            self.assertEqual(
                window.snapshot_view_combo.currentText(), "Current View")
            self.assertEqual(
                window.viewport.camera_orientation, (-35.0, 20.0))
            self.assertFalse(window.viewport.can_reset_camera)
            self.assertFalse(window.reset_camera_action.isEnabled())

            window.snapshot_zoom_spin.setValue(150)
            self.assertEqual(window._snapshot_zoom_percent, 150)
            self.assertEqual(window.snapshot_zoom_slider.value(), 150)
            self.assertTrue(window.viewport.can_reset_camera)
            self.assertTrue(window.reset_camera_action.isEnabled())

            # A real source/mesh reload fits at 100%. Current View must update
            # the controls as well as the camera so 150 is selectable again.
            window._load_selected_psx_asset()
            fitted = window.viewport.snapshot_camera_info
            self.assertAlmostEqual(
                fitted["zoom"], 1.1496046750348836, places=12)
            self.assertEqual(window._snapshot_zoom_percent, 100)
            self.assertEqual(window.snapshot_zoom_spin.value(), 100)
            self.assertEqual(window.snapshot_zoom_slider.value(), 100)
            self.assertFalse(window.viewport.can_reset_camera)
            self.assertFalse(window.reset_camera_action.isEnabled())

            # The same synchronized Reset contract applies to a PC family.
            window._set_family(_synthetic_pc_family())
            window.snapshot_view_combo.setCurrentText("Back")
            self.assertTrue(window.reset_camera_action.isEnabled())
            window._reset_view_and_gizmo()
            self.assertEqual(
                window.snapshot_view_combo.currentText(), "Current View")
            self.assertEqual(
                window.viewport.snapshot_camera_info["center"],
                [110.0, 20.0, 0.0])
            self.assertFalse(window.viewport.can_reset_camera)
            self.assertFalse(window.reset_camera_action.isEnabled())
        finally:
            window.close()

    def test_regular_source_activation_and_reset_sync_toolbar_preset(self):
        window = AssemblyWindow()
        window.viewport.resize(800, 600)
        try:
            window._set_family(_synthetic_pc_family())
            window.toolbar_view_preset_combo.setCurrentText("Back")
            self.assertEqual(
                window.viewport.camera_orientation, (180.0, 0.0))
            self.assertTrue(window.reset_camera_action.isEnabled())

            self._load_synthetic_native(window)
            self.assertEqual(
                window.toolbar_view_preset_combo.currentText(),
                "Current View")
            self.assertEqual(
                window.viewport.camera_orientation, (-35.0, 20.0))
            self.assertFalse(window.reset_camera_action.isEnabled())

            window.toolbar_view_preset_combo.setCurrentText("Back")
            self.assertEqual(
                window.viewport.camera_orientation, (180.0, 0.0))
            self.assertTrue(window.reset_camera_action.isEnabled())
            window._reset_view_and_gizmo()
            self.assertEqual(
                window.toolbar_view_preset_combo.currentText(),
                "Current View")
            self.assertEqual(
                window.viewport.camera_orientation, (-35.0, 20.0))
            self.assertFalse(window.viewport.can_reset_camera)
            self.assertFalse(window.reset_camera_action.isEnabled())
        finally:
            window.close()

    def test_empty_source_restoration_clears_stale_reset_action(self):
        for transition in ("forget", "replace"):
            with self.subTest(transition=transition):
                window = SnapshotStudioWindow()
                try:
                    self._load_synthetic_native(window)
                    window.snapshot_view_combo.setCurrentText("Back")
                    self.assertTrue(window.viewport.can_reset_camera)
                    self.assertTrue(window.reset_camera_action.isEnabled())

                    if transition == "forget":
                        window._forget_psx_source()
                    else:
                        window._set_psx_build(
                            _synthetic_psx_build(Path(r"C:\psx-b")))
                    self.app.processEvents()

                    self.assertEqual(window.viewport.source_kind, "none")
                    self.assertFalse(window.viewport.has_model)
                    self.assertFalse(window.viewport.can_reset_camera)
                    self.assertFalse(window.reset_camera_action.isEnabled())
                    self.assertFalse(window._native_visibility_timer.isActive())
                    self.assertIsNone(window._native_zero_visible_last)
                finally:
                    window.close()

    def test_viewport_resize_synchronizes_reset_without_manual_navigation(self):
        window = SnapshotStudioWindow()
        window.resize(1400, 900)
        manual_changes = []
        window.viewport.manualCameraChanged.connect(
            lambda: manual_changes.append(True))
        try:
            # Qt defers resizeEvent for a child of a hidden window. Exercise
            # the real visible-widget route that owns the persistent action.
            window.show()
            self.app.processEvents()
            window.viewport.resize(800, 600)
            self.app.processEvents()
            self._load_synthetic_native(window)
            self.assertFalse(window.viewport.can_reset_camera)
            self.assertFalse(window.reset_camera_action.isEnabled())
            window._native_visibility_timer.stop()

            window.viewport.resize(420, 1000)
            self.app.processEvents()
            self.assertEqual(manual_changes, [])
            self.assertTrue(window.viewport.can_reset_camera)
            self.assertTrue(window.reset_camera_action.isEnabled())
            self.assertTrue(window._native_visibility_timer.isActive())

            window._reset_view_and_gizmo()
            self.assertFalse(window.viewport.can_reset_camera)
            self.assertFalse(window.reset_camera_action.isEnabled())

            # PC reset geometry is size-independent, but a passive resize must
            # still publish an already displaced camera truthfully.
            window._set_family(_synthetic_pc_family())
            self.assertFalse(window.reset_camera_action.isEnabled())
            window.viewport._yaw = 12.0
            window.viewport.resize(640, 420)
            self.app.processEvents()
            self.assertEqual(manual_changes, [])
            self.assertTrue(window.viewport.can_reset_camera)
            self.assertTrue(window.reset_camera_action.isEnabled())
            self.assertFalse(window._native_visibility_timer.isActive())
        finally:
            window.close()

    def test_snapshot_source_commit_replaces_toolbar_exit_preset(self):
        window = AssemblyWindow()
        window.viewport.resize(800, 600)
        try:
            window._set_family(_synthetic_pc_family())
            window.toolbar_view_preset_combo.setCurrentText("Back")
            self.assertEqual(
                window.viewport.camera_orientation, (180.0, 0.0))

            self._enter_standard_snapshot(window)
            self._load_synthetic_native(window)
            self.assertEqual(
                window.toolbar_view_preset_combo.currentText(),
                "Current View")
            self._leave_standard_snapshot(window)
            self.assertEqual(window.viewport.source_kind, "psx_native")
            self.assertEqual(
                window.viewport.camera_orientation, (-35.0, 20.0))
            self.assertEqual(
                window.toolbar_view_preset_combo.currentText(),
                "Current View")

            # The displayed Back command is selectable immediately after exit.
            window.toolbar_view_preset_combo.setCurrentText("Back")
            self.assertEqual(
                window.viewport.camera_orientation, (180.0, 0.0))
        finally:
            window.close()

    def test_pc_restoration_refreshes_renderer_replace_and_forget_state(self):
        window = SnapshotStudioWindow()
        window.viewport.resize(800, 600)
        try:
            pc_family = _synthetic_pc_family()
            window._set_family(pc_family)
            build_a = self._load_synthetic_native(
                window, root=Path(r"C:\psx-a"))
            window.snapshot_view_combo.setCurrentText("Back")

            # Snapshot renderer source transition.
            window.snapshot_renderer_combo.setCurrentIndex(
                window.snapshot_renderer_combo.findData("textured_indexed"))
            self.app.processEvents()
            self.assertEqual(window.viewport.source_kind, "pc_openua")
            self.assertEqual(window.viewport.view_mode, "textured_indexed")
            self.assertEqual(
                window.viewport.snapshot_camera_info["center"],
                [110.0, 20.0, 0.0])
            self.assertEqual(
                (window.viewport.snapshot_camera_info["yaw"],
                 window.viewport.snapshot_camera_info["pitch"]),
                (180.0, 0.0))
            pc_baseline = window.viewport._snapshot_current_camera
            self.assertEqual(
                (pc_baseline["yaw"], pc_baseline["pitch"]), (-35.0, 20.0))
            self.assertEqual(
                list(pc_baseline["center"]), [110.0, 20.0, 0.0])

            # Build replacement while native is active restores the retained
            # PC family and reapplies the selected named preset.
            window.snapshot_renderer_combo.setCurrentIndex(
                window.snapshot_renderer_combo.findData(
                    PSX_NATIVE_VIEW_MODE))
            self.assertIs(window.viewport.psx_mesh, build_a.meshes[0])
            build_b = _synthetic_psx_build(Path(r"C:\psx-b"))
            window._set_psx_build(build_b)
            self.app.processEvents()
            self.assertEqual(window.viewport.source_kind, "pc_openua")
            self.assertEqual(
                (window.viewport.snapshot_camera_info["yaw"],
                 window.viewport.snapshot_camera_info["pitch"]),
                (180.0, 0.0))
            self.assertEqual(
                window.viewport.snapshot_camera_info["center"],
                [110.0, 20.0, 0.0])
            self.assertIsNone(window._psx_selected_mesh)
            self.assertIsNone(window._psx_selected_texture_pack)

            # Forgetting a newly activated native build uses the same atomic
            # PC restoration path and cannot retain its camera/provenance.
            window.psx_asset_tree.setCurrentItem(
                window.psx_asset_tree.topLevelItem(0))
            window._load_selected_psx_asset()
            self.assertEqual(window.viewport.source_kind, "psx_native")
            window._forget_psx_source()
            self.app.processEvents()
            self.assertEqual(window.viewport.source_kind, "pc_openua")
            self.assertEqual(
                (window.viewport.snapshot_camera_info["yaw"],
                 window.viewport.snapshot_camera_info["pitch"]),
                (180.0, 0.0))
            self.assertEqual(
                window.viewport.snapshot_camera_info["center"],
                [110.0, 20.0, 0.0])
            self.assertIn("VEHICLE.BASE", window.windowTitle())
            self.assertEqual(
                window._object_info_asset_lines, ["No asset selected."])
        finally:
            window.close()

    def test_snapshot_source_replacement_commits_exit_camera_and_renderer(self):
        window = AssemblyWindow()
        window.viewport.resize(800, 600)
        try:
            pc_family = _synthetic_pc_family()
            window._set_family(pc_family)
            window.viewport._yaw = 12.0
            window.viewport._pitch = -8.0
            window.viewport._zoom = 2.2
            old_pc_camera = window.viewport.snapshot_camera_info

            self._enter_standard_snapshot(window)
            window.snapshot_view_combo.setCurrentText("Back")
            self._load_synthetic_native(
                window, root=Path(r"C:\psx-extract"))
            inside_native = window.viewport.snapshot_camera_info
            self.assertEqual(
                (inside_native["yaw"], inside_native["pitch"]),
                (180.0, 0.0))
            self._leave_standard_snapshot(window)

            exited_native = window.viewport.snapshot_camera_info
            self.assertEqual(window.viewport.source_kind, "psx_native")
            self.assertEqual(
                window.viewport.view_mode, PSX_NATIVE_VIEW_MODE)
            self.assertEqual(
                (exited_native["yaw"], exited_native["pitch"]),
                (-35.0, 20.0))
            self.assertEqual(exited_native["center"], [0.0, 0.0, 0.25])
            self.assertNotEqual(exited_native, old_pc_camera)
            self.assertEqual(
                window.mode_combo.currentData(), PSX_NATIVE_VIEW_MODE)
            self.assertEqual(
                window.snapshot_renderer_combo.currentData(),
                PSX_NATIVE_VIEW_MODE)

            # The symmetric native-to-PC replacement commits the selected
            # indexed renderer and PC fit to the exit baseline.
            self._enter_standard_snapshot(window)
            window.snapshot_renderer_combo.setCurrentIndex(
                window.snapshot_renderer_combo.findData("textured_indexed"))
            self.app.processEvents()
            self.assertEqual(window.viewport.source_kind, "pc_openua")
            self.assertEqual(window.viewport.view_mode, "textured_indexed")
            self._leave_standard_snapshot(window)

            exited_pc = window.viewport.snapshot_camera_info
            self.assertEqual(window.viewport.source_kind, "pc_openua")
            self.assertEqual(window.viewport.view_mode, "textured_indexed")
            self.assertEqual(window._pc_view_mode, "textured_indexed")
            self.assertEqual(
                window.mode_combo.currentData(), "textured_indexed")
            self.assertEqual(
                window.snapshot_renderer_combo.currentData(),
                "textured_indexed")
            self.assertEqual(exited_pc["center"], [110.0, 20.0, 0.0])
            self.assertEqual(
                (exited_pc["yaw"], exited_pc["pitch"]), (-35.0, 20.0))
            self.assertTrue(window.snapshot_format_combo.isEnabled())
            self.assertTrue(window.snapshot_distance_fade_check.isEnabled())
        finally:
            window.close()

    def test_snapshot_exit_restores_native_diagnostics_and_visibility_hint(self):
        window = AssemblyWindow()
        window.viewport.resize(800, 600)
        try:
            self._load_synthetic_native(window)
            for diagnostic_mode in ("wireframe", "materials"):
                with self.subTest(diagnostic_mode=diagnostic_mode):
                    window.mode_combo.setCurrentIndex(
                        window.mode_combo.findData(diagnostic_mode))
                    self.assertEqual(
                        window.viewport.view_mode, diagnostic_mode)
                    self._enter_standard_snapshot(window)
                    self.assertEqual(
                        window.viewport.view_mode, PSX_NATIVE_VIEW_MODE)
                    self._leave_standard_snapshot(window)
                    self.assertEqual(
                        window.viewport.view_mode, diagnostic_mode)
                    self.assertEqual(
                        window.mode_combo.currentData(), diagnostic_mode)
                    self.assertEqual(
                        window.snapshot_renderer_combo.currentData(),
                        PSX_NATIVE_VIEW_MODE)

            # A native export temporarily forces the truthful native renderer
            # but must leave the diagnostic toolbar/view state untouched.
            with tempfile.TemporaryDirectory() as temporary:
                output = Path(temporary) / "diagnostic.png"
                before_mode = window.viewport.view_mode
                before_toolbar = window.mode_combo.currentData()
                with patch(
                        "assembly_window.QFileDialog.getSaveFileName",
                        return_value=(str(output), "PNG (*.png)")), patch(
                        "assembly_window.QImageWriter",
                        side_effect=AssertionError(
                            "PC image-only saver was used")):
                    window._export_snapshot()
                sidecar = json.loads(
                    output.with_suffix(".png.json").read_text("utf-8"))
                self.assertEqual(
                    sidecar["renderer"]["requested_mode"],
                    PSX_NATIVE_VIEW_MODE)
                self.assertEqual(window.viewport.view_mode, before_mode)
                self.assertEqual(
                    window.mode_combo.currentData(), before_toolbar)

            # Snapshot's Back view is visible for this one-sided triangle, but
            # exit restores the zero-pixel regular camera and must re-probe it.
            window.mode_combo.setCurrentIndex(
                window.mode_combo.findData(PSX_NATIVE_VIEW_MODE))
            window.viewport.reset_view()
            self.assertFalse(
                window.viewport.native_render_has_visible_pixels(
                    QSize(800, 600)))
            window._native_zero_visible_last = True
            self._enter_standard_snapshot(window)
            window.snapshot_view_combo.setCurrentText("Back")
            self.assertTrue(
                window.viewport.native_render_has_visible_pixels(
                    QSize(800, 600)))
            window._native_zero_visible_last = False
            with patch.object(window, "_notify") as notify:
                self._leave_standard_snapshot(window)
                self.app.processEvents()
            self.assertFalse(
                window.viewport.native_render_has_visible_pixels(
                    QSize(800, 600)))
            self.assertTrue(window._native_zero_visible_last)
            self.assertTrue(any(
                call.args and call.args[0] == PSX_NATIVE_ZERO_VISIBLE_HINT
                for call in notify.call_args_list))
        finally:
            window.close()

    def test_native_pack_change_syncs_diagnostic_modes_and_keeps_camera(self):
        pack1 = _synthetic_psx_texture_pack("GFX/SET1GFX.BIN")
        pack2 = _synthetic_psx_texture_pack("GFX/SET2GFX.BIN")
        build = _synthetic_psx_build(
            Path(r"C:\psx-extract"), texture_packs=(pack1, pack2))
        window = SnapshotStudioWindow()
        try:
            panel = window.psx_native_batch_panel
            window._set_psx_build(build)
            window.psx_asset_tree.setCurrentItem(
                window.psx_asset_tree.topLevelItem(0))
            window._load_selected_psx_asset()
            window.snapshot_view_combo.setCurrentText("Current View")

            for ordinal, (diagnostic_mode, pack) in enumerate((
                    ("wireframe", pack1), ("materials", pack2)), start=1):
                with self.subTest(mode=diagnostic_mode):
                    window.mode_combo.setCurrentIndex(
                        window.mode_combo.findData(diagnostic_mode))
                    self.app.processEvents()
                    window.viewport._yaw = 20.0 + ordinal
                    window.viewport._pitch = -10.0 - ordinal
                    window.viewport._zoom = 1.5 + ordinal / 10
                    window.viewport._pan = QPointF(
                        ordinal / 10, -ordinal / 20)
                    expected_camera = window.viewport.snapshot_camera_info
                    expected_current_view = (
                        window.viewport._snapshot_current_camera)

                    window.psx_texture_set_combo.setCurrentIndex(ordinal)
                    self.app.processEvents()

                    self.assertEqual(
                        window.viewport.source_kind, "psx_native")
                    self.assertEqual(
                        window.viewport.view_mode, PSX_NATIVE_VIEW_MODE)
                    self.assertEqual(
                        window.mode_combo.currentData(),
                        PSX_NATIVE_VIEW_MODE)
                    self.assertEqual(
                        window.snapshot_renderer_combo.currentData(),
                        PSX_NATIVE_VIEW_MODE)
                    self.assertIs(window.viewport.psx_texture_pack, pack)
                    self.assertIs(window._psx_selected_texture_pack, pack)
                    self.assertEqual(
                        window.viewport.snapshot_camera_info,
                        expected_camera)
                    self.assertIs(
                        window.viewport._snapshot_current_camera,
                        expected_current_view)
                    self.assertIn(
                        pack.logical_path,
                        "\n".join(window._object_info_asset_lines))
                    self.assertIn(
                        Path(pack.logical_path).name,
                        panel.texture_mode_combo.itemText(1))
                    self.assertTrue(
                        panel.texture_mode_combo.model().item(1).isEnabled())
        finally:
            window.close()

    def test_failed_native_pack_change_restores_complete_live_state(self):
        valid = _synthetic_psx_texture_pack("GFX/SET1GFX.BIN")
        incompatible = _synthetic_psx_pack_without_selector_seven()
        build = _synthetic_psx_build(
            Path(r"C:\psx-extract"),
            texture_packs=(valid, incompatible))
        window = SnapshotStudioWindow()
        try:
            panel = window.psx_native_batch_panel
            window._set_psx_build(build)
            window.psx_asset_tree.setCurrentItem(
                window.psx_asset_tree.topLevelItem(0))
            window._load_selected_psx_asset()
            window.psx_texture_set_combo.setCurrentIndex(1)
            self.app.processEvents()
            window.snapshot_view_combo.setCurrentText("Current View")
            window.mode_combo.setCurrentIndex(
                window.mode_combo.findData("materials"))
            self.app.processEvents()
            window.viewport._yaw = 43.25
            window.viewport._pitch = -27.5
            window.viewport._zoom = 1.725
            window.viewport._pan = QPointF(0.3, -0.4)

            expected_camera = window.viewport.snapshot_camera_info
            expected_title = window.windowTitle()
            expected_info = tuple(window._object_info_asset_lines)
            expected_zero_visible = window._native_zero_visible_last

            with patch.object(window, "_notify") as notify:
                window.psx_texture_set_combo.setCurrentIndex(2)
                self.app.processEvents()

            self.assertEqual(window.viewport.source_kind, "psx_native")
            self.assertEqual(window.viewport.view_mode, "materials")
            self.assertEqual(window.mode_combo.currentData(), "materials")
            self.assertEqual(
                window.snapshot_renderer_combo.currentData(),
                PSX_NATIVE_VIEW_MODE)
            self.assertIs(window.viewport.psx_texture_pack, valid)
            self.assertIs(window._psx_selected_texture_pack, valid)
            self.assertIs(window.psx_texture_set_combo.currentData(), valid)
            self.assertEqual(window.psx_texture_set_combo.currentIndex(), 1)
            self.assertEqual(
                window.viewport.snapshot_camera_info, expected_camera)
            self.assertEqual(window.windowTitle(), expected_title)
            self.assertEqual(
                tuple(window._object_info_asset_lines), expected_info)
            self.assertEqual(
                window._native_zero_visible_last, expected_zero_visible)
            self.assertIn(
                "SET1GFX.BIN",
                panel.texture_mode_combo.itemText(1))
            notify.assert_called_once()
            self.assertIn(
                "Native texture-set change was refused",
                notify.call_args.args[0])
        finally:
            window.close()

    def test_native_zero_pixel_hint_is_nonblocking_and_transition_based(self):
        window = SnapshotStudioWindow()
        window.viewport.resize(800, 600)
        try:
            self._load_synthetic_native(window)
            self.assertFalse(
                window.viewport.native_render_has_visible_pixels(
                    QSize(800, 600)))
            self.assertTrue(window.cull_check.isChecked())

            with patch.object(window, "_notify") as notify:
                window._native_zero_visible_last = None
                window._schedule_native_visibility_hint(0)
                self.app.processEvents()
                notify.assert_called_once_with(
                    PSX_NATIVE_ZERO_VISIBLE_HINT, 9000)
                self.assertIn("Back", notify.call_args.args[0])
                self.assertIn("Bottom", notify.call_args.args[0])
                self.assertIn("Fit", notify.call_args.args[0])
                self.assertIn("culling remains active", notify.call_args.args[0])

                window._schedule_native_visibility_hint(0)
                self.app.processEvents()
                self.assertEqual(notify.call_count, 1)

                window.viewport.apply_view_preset("Back", QSize(800, 600))
                window._schedule_native_visibility_hint(0)
                self.app.processEvents()
                self.assertTrue(
                    window.viewport.native_render_has_visible_pixels(
                        QSize(800, 600)))
                self.assertEqual(notify.call_count, 1)

                window.viewport.reset_view()
                window._schedule_native_visibility_hint(0)
                self.app.processEvents()
                self.assertEqual(notify.call_count, 2)
                self.assertEqual(
                    notify.call_args.args,
                    (PSX_NATIVE_ZERO_VISIBLE_HINT, 9000),
                )
        finally:
            window.close()

    def test_manual_native_export_writes_an_atomic_png_provenance_pair(self):
        window = SnapshotStudioWindow()
        try:
            with tempfile.TemporaryDirectory() as temporary:
                private_root = Path(temporary) / "private-disc-root"
                build = self._load_synthetic_native(
                    window, root=private_root)
                window.snapshot_size_combo.setCurrentText("Custom")
                window.snapshot_width_spin.setValue(96)
                window.snapshot_height_spin.setValue(80)
                window.snapshot_guides_button.setChecked(True)
                window._snapshot_custom_color = QColor(12, 34, 56, 78)
                window.viewport._yaw = 17.5
                window.viewport._pitch = -9.25
                window.viewport._zoom = 2.125
                window.viewport._pan = QPointF(0.125, -0.25)
                expected_camera = window.viewport.snapshot_camera_info
                self.app.processEvents()

                output = Path(temporary) / "native_capture.png"
                with patch(
                        "assembly_window.QFileDialog.getSaveFileName",
                        return_value=(str(output), "PNG (*.png)")):
                    window._export_snapshot()

                sidecar_path = output.with_suffix(".png.json")
                self.assertTrue(output.is_file())
                self.assertTrue(sidecar_path.is_file())
                sidecar = json.loads(sidecar_path.read_text("utf-8"))
                self.assertEqual(
                    sidecar["schema_id"],
                    "openuastudio.psx_native_snapshot")
                self.assertEqual(
                    sidecar["identity"]["capture_profile_id"],
                    "psx_native_manual_snapshot_v2")
                self.assertEqual(
                    sidecar["identity"]["output"]["width"], 96)
                self.assertEqual(
                    sidecar["identity"]["output"]["height"], 80)
                self.assertTrue(
                    sidecar["identity"]["output"]["guides"])
                self.assertEqual(
                    sidecar["identity"]["output"]["background"],
                    {"mode": "rgba", "rgba": [12, 34, 56, 78]})
                self.assertEqual(
                    sidecar["renderer"]["effective_mode"],
                    "psx_native_asset_v1")
                self.assertFalse(
                    sidecar["renderer"]["pc_openua_source_used"])
                self.assertEqual(
                    sidecar["artifact"]["png_file"], output.name)
                self.assertEqual(
                    sidecar["artifact"]["png_sha256"],
                    hashlib.sha256(output.read_bytes()).hexdigest())
                self.assertEqual(
                    sidecar["identity"]["view"]["camera_state"],
                    expected_camera)
                self.assertEqual(
                    sidecar["identity"]["view"]["yaw_degrees"], 17.5)
                self.assertEqual(
                    sidecar["identity"]["view"]["pitch_degrees"], -9.25)

                # The public proof contains only logical source names and the
                # PNG basename, never a local disc or output directory.
                sidecar_text = sidecar_path.read_text("utf-8")
                for private_path in (
                        str(build.root), build.root.as_posix(),
                        str(output.parent), output.parent.as_posix()):
                    self.assertNotIn(private_path, sidecar_text)

                wrong_format = Path(temporary) / "native_capture.jpg"
                with patch(
                        "assembly_window.QFileDialog.getSaveFileName",
                        return_value=(str(wrong_format), "PNG (*.png)")), \
                        patch(
                            "assembly_window.QMessageBox.warning") as warning:
                    window._export_snapshot()
                self.assertFalse(wrong_format.exists())
                self.assertTrue(warning.called)
                self.assertEqual(
                    warning.call_args.args[1],
                    "Native snapshot requires PNG")
        finally:
            window.close()

    def test_native_export_suggests_outside_and_refuses_source_tree_output(
            self):
        window = SnapshotStudioWindow()
        try:
            with tempfile.TemporaryDirectory() as temporary:
                source_root = Path(temporary) / "extracted-disc"
                source_root.mkdir()
                self._load_synthetic_native(window, root=source_root)
                # Match the real source-picker lifecycle: it remembers the
                # extracted tree so a later source import can start there.
                window._last_directory = source_root
                forbidden = source_root / "must-not-be-written.png"
                suggested_paths = []

                def choose_forbidden(*args, **_kwargs):
                    suggested_paths.append(Path(args[2]))
                    return str(forbidden), "PNG (*.png)"

                with patch(
                        "assembly_window.QFileDialog.getSaveFileName",
                        side_effect=choose_forbidden), \
                        patch(
                            "assembly_window.QMessageBox.warning") \
                            as warning, \
                        patch.object(
                            window.viewport, "render_snapshot") as render:
                    window._export_snapshot()

                self.assertEqual(len(suggested_paths), 1)
                self.assertEqual(
                    suggested_paths[0].parent.resolve(),
                    source_root.parent.resolve())
                render.assert_not_called()
                self.assertFalse(forbidden.exists())
                self.assertFalse(
                    forbidden.with_suffix(".png.json").exists())
                self.assertTrue(warning.called)
                self.assertEqual(
                    warning.call_args.args[1],
                    "Native snapshot output refused")
                self.assertIn(
                    "outside the extracted PlayStation prototype source",
                    warning.call_args.args[2])
        finally:
            window.close()

    def test_native_export_fails_closed_when_output_resolution_fails(self):
        window = SnapshotStudioWindow()
        try:
            with tempfile.TemporaryDirectory() as temporary:
                source_root = Path(temporary) / "extracted-disc"
                source_root.mkdir()
                self._load_synthetic_native(window, root=source_root)
                output = Path(temporary) / "never-created" / "manual.png"
                original_resolve = Path.resolve

                def fail_selected_output(candidate, *args, **kwargs):
                    if candidate == output:
                        raise OSError("forced manual output resolution failure")
                    return original_resolve(candidate, *args, **kwargs)

                with patch(
                        "assembly_window.QFileDialog.getSaveFileName",
                        return_value=(str(output), "PNG (*.png)")), \
                        patch.object(
                            Path, "resolve", new=fail_selected_output), \
                        patch(
                            "assembly_window.QMessageBox.warning") as warning, \
                        patch.object(
                            window.viewport, "render_snapshot") as render:
                    window._export_snapshot()

                render.assert_not_called()
                self.assertFalse(output.parent.exists())
                self.assertFalse(output.exists())
                self.assertFalse(output.with_suffix(".png.json").exists())
                self.assertTrue(warning.called)
                self.assertEqual(
                    warning.call_args.args[1],
                    "Native snapshot output refused")
                self.assertIn(
                    "could not be resolved safely",
                    warning.call_args.args[2])
                self.assertIn(
                    "Nothing was rendered or written",
                    warning.call_args.args[2])
        finally:
            window.close()

    def test_native_source_exports_provenance_in_wireframe_and_solid_modes(
            self):
        window = SnapshotStudioWindow()
        try:
            with tempfile.TemporaryDirectory() as temporary:
                self._load_synthetic_native(
                    window, root=Path(temporary) / "private-disc")
                window.snapshot_size_combo.setCurrentText("Custom")
                window.snapshot_width_spin.setValue(72)
                window.snapshot_height_spin.setValue(64)
                self.app.processEvents()

                for mode in ("wireframe", "solid"):
                    with self.subTest(mode=mode):
                        window.viewport.set_mode(mode)
                        self.assertEqual(
                            window.viewport.source_kind, "psx_native")
                        self.assertEqual(
                            window._snapshot_renderer_filename_suffix(),
                            "_PSX_NATIVE_V1")
                        output = Path(temporary) / f"native_{mode}.png"
                        with patch(
                                "assembly_window.QFileDialog.getSaveFileName",
                                return_value=(str(output), "PNG (*.png)")), \
                                patch(
                                    "assembly_window.QImageWriter",
                                    side_effect=AssertionError(
                                        "PC image-only saver was used")):
                            window._export_snapshot()

                        sidecar_path = output.with_suffix(".png.json")
                        self.assertTrue(output.is_file())
                        self.assertTrue(sidecar_path.is_file())
                        sidecar = json.loads(
                            sidecar_path.read_text(encoding="utf-8"))
                        self.assertEqual(
                            sidecar["renderer"]["requested_mode"],
                            PSX_NATIVE_VIEW_MODE)
                        self.assertEqual(
                            sidecar["renderer"]["effective_mode"],
                            "psx_native_asset_v1")
                        self.assertFalse(
                            sidecar["renderer"]["pc_openua_source_used"])
                        self.assertFalse(
                            sidecar["renderer"]["fallback_used"])
        finally:
            window.close()

    def test_native_export_rejects_inconsistent_live_selection_before_dialog(
            self):
        cases = {
            "invalid build type": lambda window, _temporary: setattr(
                window, "_psx_build", object()),
            "foreign build": lambda window, temporary: setattr(
                window, "_psx_build",
                _synthetic_psx_build(Path(temporary) / "foreign-build")),
            "foreign selected mesh": lambda window, temporary: setattr(
                window, "_psx_selected_mesh",
                _synthetic_psx_build(
                    Path(temporary) / "foreign-mesh").meshes[0]),
            "stale selected texture": lambda window, _temporary: setattr(
                window, "_psx_selected_texture_pack", object()),
            "stale viewport texture": lambda window, _temporary: setattr(
                window.viewport, "_psx_texture_pack", object()),
        }
        for label, corrupt in cases.items():
            with self.subTest(case=label), \
                    tempfile.TemporaryDirectory() as temporary:
                window = SnapshotStudioWindow()
                try:
                    self._load_synthetic_native(
                        window, root=Path(temporary) / "disc")
                    corrupt(window, temporary)
                    with patch(
                            "assembly_window.QMessageBox.warning") \
                            as warning, \
                            patch(
                                "assembly_window.QFileDialog.getSaveFileName") \
                            as save_dialog, \
                            patch.object(
                                window.viewport, "render_snapshot") \
                            as render, \
                            patch(
                                "assembly_window.QImageWriter",
                                side_effect=AssertionError(
                                    "PC image-only saver was used")):
                        window._export_snapshot()

                    save_dialog.assert_not_called()
                    render.assert_not_called()
                    self.assertTrue(warning.called)
                    self.assertEqual(
                        warning.call_args.args[1], "Native snapshot aborted")
                finally:
                    window.close()

    def test_native_export_rechecks_selection_after_save_dialog(self):
        window = SnapshotStudioWindow()
        try:
            with tempfile.TemporaryDirectory() as temporary:
                self._load_synthetic_native(
                    window, root=Path(temporary) / "disc")
                output = Path(temporary) / "changed-during-dialog.png"

                def change_selection(*_args, **_kwargs):
                    window._psx_selected_mesh = _synthetic_psx_build(
                        Path(temporary) / "replacement").meshes[0]
                    return str(output), "PNG (*.png)"

                with patch(
                        "assembly_window.QFileDialog.getSaveFileName",
                        side_effect=change_selection), \
                        patch(
                            "assembly_window.QMessageBox.warning") as warning, \
                        patch.object(
                            window.viewport, "render_snapshot") as render, \
                        patch(
                            "assembly_window.QImageWriter",
                            side_effect=AssertionError(
                                "PC image-only saver was used")):
                    window._export_snapshot()

                render.assert_not_called()
                self.assertFalse(output.exists())
                self.assertFalse(output.with_suffix(".png.json").exists())
                self.assertTrue(warning.called)
                self.assertEqual(
                    warning.call_args.args[1], "Native snapshot aborted")
                self.assertIn(
                    "changed while the export dialog was open",
                    warning.call_args.args[2])
        finally:
            window.close()

    def test_native_export_decline_preserves_pair_and_yes_replaces_it(self):
        window = SnapshotStudioWindow()
        try:
            with tempfile.TemporaryDirectory() as temporary:
                self._load_synthetic_native(
                    window, root=Path(temporary) / "disc")
                output = Path(temporary) / "replace-me.png"
                sidecar_path = output.with_suffix(".png.json")
                old_png = b"existing-png-bytes"
                old_json = b"existing-json-bytes"
                output.write_bytes(old_png)
                sidecar_path.write_bytes(old_json)

                with patch(
                        "assembly_window.QFileDialog.getSaveFileName",
                        return_value=(str(output), "PNG (*.png)")), \
                        patch(
                            "assembly_window.QMessageBox.question",
                            return_value=QMessageBox.StandardButton.No), \
                        patch.object(
                            window.viewport, "render_snapshot") as render:
                    window._export_snapshot()

                render.assert_not_called()
                self.assertEqual(output.read_bytes(), old_png)
                self.assertEqual(sidecar_path.read_bytes(), old_json)

                with patch(
                        "assembly_window.QFileDialog.getSaveFileName",
                        return_value=(str(output), "PNG (*.png)")), \
                        patch(
                            "assembly_window.QMessageBox.question",
                            return_value=QMessageBox.StandardButton.Yes), \
                        patch(
                            "assembly_window.QImageWriter",
                            side_effect=AssertionError(
                                "PC image-only saver was used")):
                    window._export_snapshot()

                self.assertNotEqual(output.read_bytes(), old_png)
                sidecar = json.loads(sidecar_path.read_text("utf-8"))
                self.assertEqual(
                    sidecar["artifact"]["png_sha256"],
                    hashlib.sha256(output.read_bytes()).hexdigest())
        finally:
            window.close()

    def test_native_export_rejects_a_post_render_collision(self):
        window = SnapshotStudioWindow()
        try:
            with tempfile.TemporaryDirectory() as temporary:
                self._load_synthetic_native(
                    window, root=Path(temporary) / "disc")
                output = Path(temporary) / "raced.png"
                sidecar_path = output.with_suffix(".png.json")
                racing_png = b"racing-writer-png"
                racing_json = b"racing-writer-json"
                real_render = window.viewport.render_snapshot

                def render_then_collide(*args, **kwargs):
                    image = real_render(*args, **kwargs)
                    output.write_bytes(racing_png)
                    sidecar_path.write_bytes(racing_json)
                    return image

                with patch(
                        "assembly_window.QFileDialog.getSaveFileName",
                        return_value=(str(output), "PNG (*.png)")), \
                        patch(
                            "assembly_window.QMessageBox.question") \
                            as question, \
                        patch(
                            "assembly_window.QMessageBox.warning") \
                            as warning, \
                        patch.object(
                            window.viewport, "render_snapshot",
                            side_effect=render_then_collide), \
                        patch(
                            "assembly_window.QImageWriter",
                            side_effect=AssertionError(
                                "PC image-only saver was used")):
                    window._export_snapshot()

                question.assert_not_called()
                self.assertEqual(output.read_bytes(), racing_png)
                self.assertEqual(sidecar_path.read_bytes(), racing_json)
                self.assertTrue(warning.called)
                self.assertEqual(
                    warning.call_args.args[1],
                    "Native snapshot export failed")
                self.assertIn(
                    "overwrite was not authorized", warning.call_args.args[2])
                self.assertEqual([
                    path for path in Path(temporary).iterdir()
                    if path.name.endswith((".stage", ".rollback"))
                ], [])
        finally:
            window.close()

    def test_native_export_rolls_back_an_authorized_pair_on_commit_failure(
            self):
        from snapshot_studio import psx_batch_export

        window = SnapshotStudioWindow()
        try:
            with tempfile.TemporaryDirectory() as temporary:
                self._load_synthetic_native(
                    window, root=Path(temporary) / "disc")
                output = Path(temporary) / "rollback.png"
                sidecar_path = output.with_suffix(".png.json")
                old_png = b"old-png"
                old_json = b"old-json"
                output.write_bytes(old_png)
                sidecar_path.write_bytes(old_json)
                real_replace = os.replace
                replace_calls = 0

                def fail_second_final(source, destination):
                    nonlocal replace_calls
                    replace_calls += 1
                    if replace_calls == 4:
                        raise OSError("forced second-final failure")
                    return real_replace(source, destination)

                with patch(
                        "assembly_window.QFileDialog.getSaveFileName",
                        return_value=(str(output), "PNG (*.png)")), \
                        patch(
                            "assembly_window.QMessageBox.question",
                            return_value=QMessageBox.StandardButton.Yes), \
                        patch(
                            "assembly_window.QMessageBox.warning") \
                            as warning, \
                        patch.object(
                            psx_batch_export.os, "replace",
                            side_effect=fail_second_final), \
                        patch(
                            "assembly_window.QImageWriter",
                            side_effect=AssertionError(
                                "PC image-only saver was used")):
                    window._export_snapshot()

                self.assertEqual(output.read_bytes(), old_png)
                self.assertEqual(sidecar_path.read_bytes(), old_json)
                self.assertTrue(warning.called)
                self.assertEqual(
                    warning.call_args.args[1],
                    "Native snapshot export failed")
                self.assertIn(
                    "forced second-final failure", warning.call_args.args[2])
                self.assertEqual([
                    path for path in Path(temporary).iterdir()
                    if path.name.endswith((".stage", ".rollback"))
                ], [])
        finally:
            window.close()

    @unittest.skipUnless(
        os.environ.get("OPENUA_PSX_CORPUS_ROOT"),
        "set OPENUA_PSX_CORPUS_ROOT for recovered prototype checks")
    def test_older_native_sources_render_their_sector_padded_texture_sets(self):
        corpus = Path(os.environ["OPENUA_PSX_CORPUS_ROOT"])
        cases = (
            ("1998-12-18", 31, 1, 77, 30),
            ("1999-03-12", 63, 6, 78, 30),
            ("1999-05-14", 91, 6, 81, 34),
        )
        window = SnapshotStudioWindow()
        try:
            for (build_name, mesh_count, pack_count, material_count,
                 effect_count) in cases:
                with self.subTest(build=build_name):
                    source = corpus / "technical" / "analysis" / build_name \
                        / "work" / "disc_files"
                    window.open_psx_source(source)
                    self.assertEqual(
                        window.psx_asset_tree.topLevelItemCount(), mesh_count)
                    self.assertEqual(
                        window.psx_effect_list.count(), effect_count)
                    self.assertIn(
                        "Static native effect inventory only",
                        window.psx_effect_list.item(0).toolTip())
                    self.assertEqual(
                        window.psx_texture_set_combo.count(), pack_count + 1)
                    self.assertIsNone(
                        window.psx_texture_set_combo.currentData())

                    window.psx_asset_tree.setCurrentItem(
                        window.psx_asset_tree.topLevelItem(0))
                    window._load_selected_psx_asset()
                    window.psx_texture_set_combo.setCurrentIndex(1)
                    self.app.processEvents()
                    image = window.viewport.render_snapshot(
                        QSize(128, 128), QColor("black"))
                    info = window.viewport.renderer_info

                    self.assertFalse(image.isNull())
                    self.assertTrue(info["native_texture_decode"])
                    self.assertEqual(
                        info["native_texture_pack_layout_id"],
                        "sector_padded_setgfx_direct_v1")
                    self.assertEqual(
                        info[
                            "native_texture_selector_to_pixel_bank_mapping"],
                        "selector_S_clut_S_pixel_bank_direct_slot_S")
                    self.assertEqual(
                        info["native_texture_material_slot_count"],
                        material_count)
                    self.assertEqual(
                        info["texture_selector_table_status"],
                        "validated_native_setgfx_selector_table")
        finally:
            window.close()

    @unittest.skipUnless(
        os.environ.get("OPENUA_PSX_CORPUS_ROOT"),
        "set OPENUA_PSX_CORPUS_ROOT for recovered prototype checks")
    def test_late_native_source_exposes_and_renders_its_six_texture_sets(self):
        corpus = Path(os.environ["OPENUA_PSX_CORPUS_ROOT"])
        source = corpus / "technical" / "analysis" / "1999-06-15" \
            / "work" / "disc_files"
        window = SnapshotStudioWindow()
        try:
            window.open_psx_source(source)
            self.assertEqual(window.psx_asset_tree.topLevelItemCount(), 139)
            self.assertEqual(window.psx_effect_list.count(), 34)
            self.assertIn(
                "static_effect_mesh_unbound",
                window.psx_effect_list.item(0).toolTip())
            self.assertEqual(window.psx_roster_list.count(), 53)
            self.assertIn(
                "line 01:", window.psx_roster_list.item(0).text())
            self.assertTrue(window.psx_texture_set_combo.isEnabled())
            self.assertEqual(window.psx_texture_set_combo.count(), 7)
            self.assertIsNone(window.psx_texture_set_combo.itemData(0))
            self.assertIn(
                "Topology only", window.psx_texture_set_combo.itemText(0))
            self.assertEqual(
                [window.psx_texture_set_combo.itemText(index)
                 for index in range(1, 7)],
                [f"SET{index}GFX.BIN" for index in range(1, 7)],
            )

            window.psx_asset_tree.setCurrentItem(
                window.psx_asset_tree.topLevelItem(0))
            window._load_selected_psx_asset()
            image = window.viewport.render_snapshot(
                QSize(128, 128), QColor("black"))
            info = window.viewport.renderer_info

            self.assertFalse(image.isNull())
            self.assertEqual(window.viewport.source_kind, "psx_native")
            self.assertFalse(info["native_texture_decode"])
            self.assertIsNone(window.viewport.psx_texture_pack)
            self.assertEqual(
                window.viewport._materials[0].kind,
                "psx_native_selector")
            self.assertEqual(
                info["texture_binding_status"],
                "topology_only_operator_default")
            self.assertEqual(
                info["texture_selector_table_status"],
                "validated_native_packs_available_not_selected")
            self.assertEqual(
                info["mesh_to_texture_pack_binding"], "none_selected")

            # No SET is associated with an ordinal automatically. Selecting
            # one is an explicit environmental-variant choice.
            window.psx_texture_set_combo.setCurrentIndex(1)
            self.app.processEvents()
            info = window.viewport.renderer_info
            self.assertTrue(info["native_texture_decode"])
            self.assertEqual(
                info["native_texture_pack_path"], "GFX/SET1GFX.BIN")
            self.assertEqual(
                info["texture_selector_table_status"],
                "validated_native_setgfx_selector_table")
            self.assertEqual(
                info["native_texture_pack_layout_id"],
                "late_compact_setgfx_v1")
            self.assertEqual(
                info["mesh_to_texture_pack_binding"],
                "operator_selected_environment_variant_no_mesh_inherent_"
                "affinity")

            before = window.viewport.psx_texture_pack.source_sha256
            window.psx_texture_set_combo.setCurrentIndex(2)
            self.app.processEvents()
            after = window.viewport.psx_texture_pack.source_sha256
            self.assertNotEqual(before, after)
            self.assertEqual(
                window.viewport.renderer_info["native_texture_pack_path"],
                "GFX/SET2GFX.BIN")
        finally:
            window.close()


if __name__ == "__main__":
    unittest.main()
