"""Official all-model snapshot batch exporter for Snapshot Studio.

The batch uses the active SET.BAS and the existing AssetViewport renderer.
It exports every renderable VP slot plus every additional embedded SKLT model
that is not present in the VISPROTO table. Source assets remain read-only.
"""

from __future__ import annotations

import csv
import hashlib
import json
import math
import operator
import os
import re
import time
import zipfile
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath

from PySide6.QtCore import QSize, QTimer
from PySide6.QtGui import QImageWriter
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QFileDialog,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
)

from assembly_viewer import (
    AssetViewport,
    INDEXED_EFFECTIVE_RENDERERS,
    RETAIL_AREA_DISTANCE_FADE_DISTANCE_SPACE,
    RETAIL_AREA_DISTANCE_FADE_FORMULA,
    RETAIL_AREA_DISTANCE_FADE_LENGTH,
    RETAIL_AREA_DISTANCE_FADE_PROFILE_ID,
    RETAIL_AREA_DISTANCE_FADE_START,
    RETAIL_AREA_DISTANCE_FADE_VISIBILITY_LIMIT,
    TEXTURED_VIEW_MODES,
    VIEW_PRESET_ANGLES,
)
from asset_family import AssetFamily, FamilyObject, load_asset_family
from base_parser import BaseObject
from setbas_reader import decode_skeleton
from vp_manager import normalize_base_name, normalize_skeleton_name


_INVALID_FILENAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]+')
_ZIP_METADATA_FILES = (
    "manifest.json",
    "manifest.csv",
    "warnings.log",
    "run_info.json",
)
_ZIP_IMAGE_STATUSES = {
    "WRITTEN",
    "EXISTS",
    "ERROR_EXISTING_RETAINED",
}
_FLAT_TRACY_DESTINATION_MODES = {
    "live_framebuffer": "canonical",
    "forced_diagnostic": "diagnostic",
}


@dataclass
class SnapshotSource:
    source_kind: str
    source_id: int
    vp_id: int
    base_name: str
    comment: str
    resource_index: int
    resource_name: str
    folder_name: str
    owner_path: str
    skeleton_name: str
    mapped_object: object | None
    decoded_skeleton: object | None
    geometry_only: bool
    skip_status: str = ""
    skip_message: str = ""


@dataclass
class BatchManifestRow:
    source_kind: str
    source_id: int
    vp_id: int
    base_name: str
    resource_index: int
    resource_name: str
    owner_path: str
    skeleton_name: str
    view: str
    relative_file: str
    status: str
    message: str
    requested_renderer: str
    effective_renderer: str
    fallback_used: bool
    fallback_reason: str
    palette_sha256: str
    shadermp_sha256: str
    tracyrmp_sha256: str
    index_buffer_sha256: str
    requested_flat_tracy_destination_mode: str
    requested_flat_tracy_forced_destination_index: int | None
    effective_flat_tracy_destination_mode: str
    effective_flat_tracy_destination_class: str
    effective_flat_tracy_forced_destination_index: int | None
    effective_initial_framebuffer_index: int | None
    effective_initial_framebuffer_rgb: str
    effective_flat_tracy_forced_destination_rgb: str
    requested_distance_fade_enabled: bool | None
    effective_distance_fade_enabled: bool | None
    distance_fade_profile_id: str
    distance_fade_visibility_limit: float | None
    distance_fade_start: float | None
    distance_fade_length: float | None
    distance_fade_distance_space: str
    distance_fade_formula: str


def safe_component(value: str, fallback: str = "unnamed",
                   limit: int = 80) -> str:
    """Return a deterministic Windows-safe path component."""

    text = str(value).replace("\\", "/").rsplit("/", 1)[-1]
    text = _INVALID_FILENAME.sub("_", text).strip(" ._")
    if not text:
        text = fallback
    return text[:limit].rstrip(" ._") or fallback


def view_slug(value: str) -> str:
    return safe_component(value.lower().replace(" ", "_"), "view", 48)


def drawable_polygon_count(skeleton) -> int:
    """Count polygons that the static Snapshot renderer can actually draw."""

    if skeleton is None:
        return 0
    return sum(1 for polygon in skeleton.polygons if len(polygon) >= 3)


def file_sha256(path: str | Path) -> str:
    source = Path(path)
    if not source.is_file():
        return ""
    digest = hashlib.sha256()
    with source.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _manifest_palette_index(value) -> int | None:
    """Normalize a renderer palette index without accepting booleans."""

    if isinstance(value, bool):
        return None
    try:
        index = operator.index(value)
    except TypeError:
        return None
    return index if 0 <= index <= 255 else None


def _manifest_rgb_hex(value) -> str:
    """Return a stable manifest color string for an RGB triplet."""

    if not isinstance(value, (list, tuple)) or len(value) != 3:
        return ""
    try:
        rgb = tuple(int(channel) for channel in value)
    except (TypeError, ValueError, OverflowError):
        return ""
    if not all(0 <= channel <= 255 for channel in rgb):
        return ""
    return f"#{rgb[0]:02X}{rgb[1]:02X}{rgb[2]:02X}"


def _manifest_bool(value) -> bool | None:
    """Return a real JSON-style boolean without accepting truthy values."""

    return value if isinstance(value, bool) else None


def _manifest_finite_float(value) -> float | None:
    """Normalize one finite numeric renderer parameter for provenance."""

    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _requested_destination_profile(
        renderer_mode: str, destination_mode: str,
        forced_index, distance_fade_enabled=False) -> dict[str, object]:
    """Return the normalized output-affecting renderer profile for a batch."""

    renderer_mode = str(renderer_mode)
    if renderer_mode != "textured_indexed":
        return {
            "renderer_mode": renderer_mode,
            "flat_tracy_destination_mode": None,
            "flat_tracy_forced_destination_index": None,
            "distance_fade_enabled": None,
            "distance_fade_profile_id": None,
            "distance_fade_visibility_limit": None,
            "distance_fade_start": None,
            "distance_fade_length": None,
            "distance_fade_distance_space": None,
            "distance_fade_formula": None,
        }
    destination_mode = str(destination_mode)
    if destination_mode not in _FLAT_TRACY_DESTINATION_MODES:
        destination_mode = "live_framebuffer"
    normalized_index = None
    if destination_mode == "forced_diagnostic":
        normalized_index = _manifest_palette_index(forced_index)
        if normalized_index is None:
            normalized_index = 0
    fade_enabled = bool(_manifest_bool(distance_fade_enabled) or False)
    return {
        "renderer_mode": renderer_mode,
        "flat_tracy_destination_mode": destination_mode,
        "flat_tracy_forced_destination_index": normalized_index,
        "distance_fade_enabled": fade_enabled,
        "distance_fade_profile_id": (
            RETAIL_AREA_DISTANCE_FADE_PROFILE_ID if fade_enabled else None),
        "distance_fade_visibility_limit": (
            RETAIL_AREA_DISTANCE_FADE_VISIBILITY_LIMIT
            if fade_enabled else None),
        "distance_fade_start": (
            RETAIL_AREA_DISTANCE_FADE_START if fade_enabled else None),
        "distance_fade_length": (
            RETAIL_AREA_DISTANCE_FADE_LENGTH if fade_enabled else None),
        "distance_fade_distance_space": (
            RETAIL_AREA_DISTANCE_FADE_DISTANCE_SPACE
            if fade_enabled else None),
        "distance_fade_formula": (
            RETAIL_AREA_DISTANCE_FADE_FORMULA if fade_enabled else None),
    }


def _normalize_recorded_distance_fade_profile(value) -> dict[str, object] | None:
    """Validate a recorded enabled profile without accepting coercions."""

    if not isinstance(value, dict):
        return None
    profile_id = value.get("profile_id")
    distance_space = value.get("distance_space")
    formula = value.get("formula")
    if not all(isinstance(item, str) and item
               for item in (profile_id, distance_space, formula)):
        return None
    raw_numbers = (
        value.get("visibility_limit"),
        value.get("fade_start"),
        value.get("fade_length"),
    )
    if any(isinstance(item, bool) or not isinstance(item, (int, float))
           for item in raw_numbers):
        return None
    visibility_limit = _manifest_finite_float(raw_numbers[0])
    fade_start = _manifest_finite_float(raw_numbers[1])
    fade_length = _manifest_finite_float(raw_numbers[2])
    if None in (visibility_limit, fade_start, fade_length):
        return None
    return {
        "distance_fade_profile_id": profile_id,
        "distance_fade_visibility_limit": visibility_limit,
        "distance_fade_start": fade_start,
        "distance_fade_length": fade_length,
        "distance_fade_distance_space": distance_space,
        "distance_fade_formula": formula,
    }


def _recorded_distance_fade_profile(
        requested_profile: dict[str, object]) -> dict[str, object] | None:
    """Serialize the active requested profile in stable public field names."""

    if requested_profile.get("distance_fade_enabled") is not True:
        return None
    return {
        "profile_id": requested_profile["distance_fade_profile_id"],
        "visibility_limit": requested_profile[
            "distance_fade_visibility_limit"],
        "fade_start": requested_profile["distance_fade_start"],
        "fade_length": requested_profile["distance_fade_length"],
        "distance_space": requested_profile[
            "distance_fade_distance_space"],
        "formula": requested_profile["distance_fade_formula"],
    }


def _destination_profile_label(profile: dict[str, object]) -> str:
    """Return a concise human-readable label for collision messages."""

    renderer_mode = str(profile.get("renderer_mode", "unknown"))
    if renderer_mode != "textured_indexed":
        return renderer_mode
    mode = profile.get("flat_tracy_destination_mode")
    if mode == "forced_diagnostic":
        index = profile.get("flat_tracy_forced_destination_index")
        label = f"Retail Indexed / forced diagnostic row {index}"
    else:
        label = "Retail Indexed / live framebuffer (canonical)"
    fade = "distance fade on" if profile.get(
        "distance_fade_enabled") else "distance fade off"
    return f"{label} / {fade}"


class VPSnapshotBatchPanel(QGroupBox):
    """Compact controller embedded below the normal Snapshot tools."""

    def __init__(self, window) -> None:
        super().__init__("Batch VP Snapshots", window._snapshot_panel)
        self.window = window
        self.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

        self._running = False
        self._cancel_requested = False
        self._sources: list[SnapshotSource] = []
        self._queue: list[tuple[SnapshotSource, int, str]] = []
        self._queue_index = 0
        self._rows: list[BatchManifestRow] = []
        self._warnings: list[str] = []
        self._family = None
        self._root: Path | None = None
        self._target_size = QSize(1024, 1024)
        self._zoom = 100
        self._written = 0
        self._existing = 0
        self._skipped_models = 0
        self._failed = 0
        self._started_at = 0.0
        self._last_setbas_path = ""
        self._renderer_mode = "textured"
        self._flat_tracy_destination_mode = "live_framebuffer"
        self._flat_tracy_forced_destination_index = 0
        self._distance_fade_enabled = False
        self._batch_viewport: AssetViewport | None = None
        self._active_source_key: tuple[str, int] | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 5, 6, 6)
        layout.setSpacing(4)

        self.summary_label = QLabel(
            "Load a SET.BAS archive to enable complete model export.")
        self.summary_label.setWordWrap(True)
        layout.addWidget(self.summary_label)

        output_row = QHBoxLayout()
        output_row.setSpacing(4)
        output_row.addWidget(QLabel("Output:"))
        self.output_edit = QLineEdit()
        self.output_edit.setPlaceholderText("Choose an output folder...")
        output_row.addWidget(self.output_edit, 1)
        self.output_button = QPushButton("...")
        self.output_button.setFixedWidth(34)
        self.output_button.setToolTip("Choose the corpus output folder")
        self.output_button.clicked.connect(self._choose_output)
        output_row.addWidget(self.output_button)
        layout.addLayout(output_row)

        options_row = QHBoxLayout()
        options_row.setSpacing(10)
        self.skip_existing_check = QCheckBox("Skip existing")
        self.skip_existing_check.setChecked(True)
        self.skip_existing_check.setToolTip(
            "Keep existing non-empty PNG files, allowing safe resume.")
        options_row.addWidget(self.skip_existing_check)
        self.zip_check = QCheckBox("Create ZIP")
        self.zip_check.setChecked(True)
        options_row.addWidget(self.zip_check)
        self.transparent_label = QLabel(
            "Transparent PNG • all canonical views • Photo Studio size/zoom")
        self.transparent_label.setStyleSheet("color: #9fb3bd;")
        options_row.addWidget(self.transparent_label, 1)
        layout.addLayout(options_row)

        buttons_row = QHBoxLayout()
        buttons_row.setSpacing(4)
        self.export_button = QPushButton("Export All VP")
        self.export_button.clicked.connect(self.start)
        buttons_row.addWidget(self.export_button, 1)
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self.request_cancel)
        buttons_row.addWidget(self.cancel_button)
        layout.addLayout(buttons_row)

        progress_row = QGridLayout()
        progress_row.setContentsMargins(0, 0, 0, 0)
        progress_row.setHorizontalSpacing(5)
        self.progress = QProgressBar()
        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        progress_row.addWidget(self.progress, 0, 0)
        self.status_label = QLabel("Ready.")
        self.status_label.setMinimumWidth(155)
        progress_row.addWidget(self.status_label, 0, 1)
        progress_row.setColumnStretch(0, 1)
        layout.addLayout(progress_row)

        self.refresh()

    @property
    def is_running(self) -> bool:
        return self._running

    def refresh(self) -> None:
        """Refresh source counts without disturbing a running job."""

        if self._running:
            return

        manager = getattr(self.window, "_vp_manager", None)
        archive = getattr(self.window, "_setbas", None)
        entries = tuple(manager.table.entries) if manager is not None else ()
        sklt_resources = (
            tuple(
                resource for resource in archive.resources
                if resource.class_id.lower() == "sklt.class"
                and not resource.error
            )
            if archive is not None else ()
        )

        if archive is None or not sklt_resources:
            self.summary_label.setText(
                "Load a SET.BAS archive to enable complete model export.")
            self.export_button.setEnabled(False)
            return

        embedded_entries = tuple(getattr(
            getattr(self.window, "_vp_embedded", None), "entries", ()))
        assigned_skeletons = {
            normalize_skeleton_name(entry.skeleton_name)
            for entry in embedded_entries
        }
        extra_count = sum(
            1 for resource in sklt_resources
            if normalize_skeleton_name(resource.resource_name)
            not in assigned_skeletons
        )
        potential_sources = len(entries) + extra_count
        views = len(VIEW_PRESET_ANGLES)

        self.summary_label.setText(
            f"VP slots: {len(entries)}  •  Extra SKLT models: {extra_count}  •  "
            f"Sources: {potential_sources}  •  Up to {potential_sources * views:,} PNG"
        )
        self.export_button.setEnabled(bool(entries or extra_count))

        archive_path = str(Path(archive.path).resolve())
        if archive_path != self._last_setbas_path:
            self._last_setbas_path = archive_path
            set_name = safe_component(
                Path(archive.path).parent.name or Path(archive.path).stem,
                "SET",
            )
            base_dir = Path(
                getattr(self.window, "_last_directory", Path.home()))
            self.output_edit.setText(
                str(base_dir / f"{set_name}_All_Models_Snapshot_Corpus"))

    def _choose_output(self) -> None:
        current = self.output_edit.text().strip()
        path = QFileDialog.getExistingDirectory(
            self,
            "Choose complete snapshot corpus folder",
            current or str(Path.home()),
        )
        if path:
            self.output_edit.setText(path)

    def _set_running(self, running: bool) -> None:
        self._running = running
        self.export_button.setEnabled(not running)
        self.output_edit.setEnabled(not running)
        self.output_button.setEnabled(not running)
        self.skip_existing_check.setEnabled(not running)
        self.zip_check.setEnabled(not running)
        self.cancel_button.setEnabled(running)

        bas_panel = getattr(self.window, "_bas_panel", None)
        if bas_panel is not None:
            bas_panel.setEnabled(not running)
        photo_box = getattr(self.window, "_snapshot_studio_box", None)
        if photo_box is not None:
            photo_box.setEnabled(not running)

    def request_cancel(self) -> None:
        if not self._running:
            return
        self._cancel_requested = True
        self.cancel_button.setEnabled(False)
        self.status_label.setText("Cancelling...")

    def start(self) -> None:
        if self._running:
            return

        manager = getattr(self.window, "_vp_manager", None)
        archive = getattr(self.window, "_setbas", None)
        if manager is None or archive is None:
            QMessageBox.information(
                self,
                "No SET.BAS loaded",
                "Open a SET.BAS archive in BAS Manager first.",
            )
            return

        output_text = self.output_edit.text().strip()
        if not output_text:
            self._choose_output()
            output_text = self.output_edit.text().strip()
        if not output_text:
            return

        self._cancel_requested = False
        self._sources.clear()
        self._queue.clear()
        self._rows.clear()
        self._warnings.clear()
        self._queue_index = 0
        self._written = 0
        self._existing = 0
        self._skipped_models = 0
        self._failed = 0
        self._started_at = time.time()
        self._root = Path(output_text).expanduser().resolve()
        self._root.mkdir(parents=True, exist_ok=True)
        self._target_size = self.window._snapshot_output_size()
        self._zoom = int(self.window.snapshot_zoom_spin.value())
        renderer_combo = getattr(
            self.window, "snapshot_renderer_combo", None)
        self._renderer_mode = (
            renderer_combo.currentData()
            if renderer_combo is not None else "textured")
        if self._renderer_mode not in TEXTURED_VIEW_MODES:
            self._renderer_mode = "textured"
        self._capture_flat_tracy_destination_settings()
        self._capture_distance_fade_setting()
        collision_reason = self._skip_existing_profile_collision_reason()
        if collision_reason:
            QMessageBox.warning(
                self,
                "Cannot safely skip existing snapshots",
                f"{collision_reason}\n\n"
                "Choose a separate output folder, or clear Skip existing "
                "to overwrite this folder intentionally.",
            )
            self.status_label.setText("Destination profile mismatch.")
            return

        self._set_running(True)
        self.status_label.setText("Scanning SET.BAS...")
        QApplication.processEvents()

        try:
            archive_path = Path(archive.path)
            current_family = getattr(self.window, "_family", None)
            if current_family is not None \
                    and current_family.base_path == archive_path:
                self._family = current_family
            else:
                self._family = load_asset_family(
                    archive_path,
                    self.window._extra_roots,
                    {},
                    archive,
                )

            self._sources = self._build_sources(
                manager.table.entries, archive, self._family)
        except Exception as exc:
            self._set_running(False)
            QMessageBox.critical(
                self,
                "Snapshot batch preparation failed",
                f"The SET.BAS and source assets were not modified.\n\n{exc}",
            )
            self.status_label.setText("Preparation failed.")
            return

        renderable = [
            source for source in self._sources
            if not source.skip_status
        ]
        skipped = [
            source for source in self._sources
            if source.skip_status
        ]
        self._skipped_models = len(skipped)
        for source in skipped:
            self._record(
                source, "", "",
                source.skip_status, source.skip_message)

        views = tuple(VIEW_PRESET_ANGLES.keys())
        self._queue = [
            (source, view_index, view)
            for source in renderable
            for view_index, view in enumerate(views, 1)
        ]

        if not self._queue:
            self._write_manifests(cancelled=False)
            self._set_running(False)
            QMessageBox.information(
                self,
                "Nothing renderable",
                "The archive contains no polygonal models that the static "
                "Snapshot renderer can draw.",
            )
            self.status_label.setText("Nothing renderable.")
            return

        self.summary_label.setText(
            f"Renderable models: {len(renderable)}  •  "
            f"Images: {len(self._queue):,}  •  "
            f"Non-mesh/empty skipped: {len(skipped)}"
        )
        self.progress.setRange(0, len(self._queue))
        self.progress.setValue(0)

        self._batch_viewport = self._create_batch_viewport()
        self._active_source_key = None

        self.window.statusBar().showMessage(
            f"Complete model batch started: {len(renderable)} models × "
            f"{len(views)} views.")
        self.status_label.setText("Starting...")
        QTimer.singleShot(0, self._step)

    def _capture_flat_tracy_destination_settings(self) -> None:
        """Freeze the visible destination controls for the whole batch."""

        source_viewport = getattr(self.window, "viewport", None)
        requested_mode = str(getattr(
            source_viewport,
            "flat_tracy_destination_mode",
            "live_framebuffer",
        ))
        if requested_mode not in _FLAT_TRACY_DESTINATION_MODES:
            requested_mode = "live_framebuffer"
        self._flat_tracy_destination_mode = requested_mode
        requested_index = getattr(
            source_viewport, "flat_tracy_forced_destination_index", 0)
        self._flat_tracy_forced_destination_index = (
            _manifest_palette_index(requested_index) or 0)

    def _capture_distance_fade_setting(self) -> None:
        """Freeze the visible indexed distance-fade toggle for this batch."""

        source_viewport = getattr(self.window, "viewport", None)
        requested = _manifest_bool(getattr(
            source_viewport, "distance_fade_enabled", False))
        self._distance_fade_enabled = bool(requested or False)

    def _create_batch_viewport(self) -> AssetViewport:
        """Create a hidden renderer with the batch-start settings frozen."""

        viewport = AssetViewport(self)
        viewport.set_mode(self._renderer_mode)
        viewport.set_flat_tracy_forced_destination_index(
            self._flat_tracy_forced_destination_index)
        viewport.set_flat_tracy_destination_mode(
            self._flat_tracy_destination_mode)
        viewport.set_distance_fade_enabled(self._distance_fade_enabled)
        viewport.begin_snapshot_mode(None)
        viewport.play_animation(False)
        viewport.set_snapshot_guides_visible(False)
        return viewport

    def _skip_existing_profile_collision_reason(self) -> str:
        """Reject unsafe Skip-existing reuse across renderer profiles."""

        if not self.skip_existing_check.isChecked() or self._root is None:
            return ""
        has_png = False
        for _directory, _children, filenames in os.walk(
                self._root, followlinks=False):
            if any(name.lower().endswith(".png") for name in filenames):
                has_png = True
                break
        if not has_png:
            return ""

        run_info_path = self._root / "run_info.json"
        if not run_info_path.is_file():
            return (
                "The selected folder already contains PNG files but has no "
                "run_info.json proving their renderer destination profile."
            )
        try:
            existing_info = json.loads(
                run_info_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            return (
                "The selected folder contains PNG files, but its "
                f"run_info.json cannot be verified ({exc})."
            )
        if not isinstance(existing_info, dict):
            return (
                "The selected folder contains PNG files, but run_info.json "
                "does not contain a verifiable batch record."
            )

        renderer_mode = existing_info.get("renderer_mode")
        if not isinstance(renderer_mode, str) or not renderer_mode:
            return (
                "The selected folder contains PNG files, but run_info.json "
                "does not identify their renderer mode."
            )
        destination_mode = existing_info.get(
            "indexed_flat_tracy_destination_mode_requested")
        forced_index = existing_info.get(
            "indexed_flat_tracy_forced_destination_index_requested")
        missing = object()
        distance_fade_enabled = existing_info.get(
            "indexed_distance_fade_enabled_requested", missing)
        recorded_fade_profile = existing_info.get(
            "indexed_distance_fade_profile_requested", missing)
        if renderer_mode == "textured_indexed":
            summary = existing_info.get("renderer_summary")
            if not isinstance(summary, dict):
                summary = {}
            if destination_mode is None:
                destination_mode = summary.get(
                    "requested_flat_tracy_destination_mode")
            if forced_index is None:
                forced_index = summary.get(
                    "requested_flat_tracy_forced_destination_index")
            if distance_fade_enabled is missing:
                distance_fade_enabled = summary.get(
                    "requested_distance_fade_enabled", missing)
            if recorded_fade_profile is missing:
                recorded_fade_profile = summary.get(
                    "requested_distance_fade_profile", missing)
            if destination_mode not in _FLAT_TRACY_DESTINATION_MODES:
                return (
                    "The selected folder contains indexed PNG files, but "
                    "run_info.json does not prove their flat-TRACY "
                    "destination mode."
                )
            if destination_mode == "forced_diagnostic" \
                    and _manifest_palette_index(forced_index) is None:
                return (
                    "The selected folder contains forced-diagnostic PNG "
                    "files, but run_info.json has no valid forced palette "
                    "destination index."
                )
            if distance_fade_enabled is missing:
                # Indexed batches written before this toggle existed can only
                # contain the historical fade-disabled output.
                distance_fade_enabled = False
            elif _manifest_bool(distance_fade_enabled) is None:
                return (
                    "The selected folder contains indexed PNG files, but "
                    "run_info.json does not prove whether reconstructed "
                    "distance fade was enabled."
                )
            if distance_fade_enabled is True:
                normalized_record = (
                    _normalize_recorded_distance_fade_profile(
                        recorded_fade_profile))
                if normalized_record is None:
                    return (
                        "The selected folder contains distance-faded indexed "
                        "PNGs, but run_info.json does not prove the complete "
                        "distance-fade profile and formula."
                    )

        existing_profile = _requested_destination_profile(
            renderer_mode, destination_mode, forced_index,
            distance_fade_enabled)
        requested_profile = _requested_destination_profile(
            self._renderer_mode,
            self._flat_tracy_destination_mode,
            self._flat_tracy_forced_destination_index,
            getattr(self, "_distance_fade_enabled", False),
        )
        if renderer_mode == "textured_indexed" \
                and distance_fade_enabled is True:
            supported_enabled_profile = _requested_destination_profile(
                "textured_indexed", destination_mode, forced_index, True)
            expected_fade_profile = {
                key: supported_enabled_profile[key]
                for key in (
                    "distance_fade_profile_id",
                    "distance_fade_visibility_limit",
                    "distance_fade_start",
                    "distance_fade_length",
                    "distance_fade_distance_space",
                    "distance_fade_formula",
                )
            }
            if normalized_record != expected_fade_profile:
                return (
                    "The selected folder contains snapshots made with a "
                    "different distance-fade profile or formula. Use a "
                    "separate output folder or disable Skip existing to "
                    "overwrite intentionally."
                )
        if existing_profile != requested_profile:
            return (
                "The selected folder contains snapshots from "
                f"{_destination_profile_label(existing_profile)}, while "
                "this batch requests "
                f"{_destination_profile_label(requested_profile)}."
            )
        return ""

    def _build_sources(self, vp_entries, archive, family) -> list[SnapshotSource]:
        """Build one deterministic corpus entry per VP plus extra SKLT."""

        sklt_resources = [
            resource for resource in archive.resources
            if resource.class_id.lower() == "sklt.class"
            and not resource.error
        ]
        resources_by_name: dict[str, list[object]] = {}
        for resource in sklt_resources:
            resources_by_name.setdefault(
                normalize_skeleton_name(resource.resource_name), []
            ).append(resource)

        objects = list(family.all_objects())
        objects_by_offset: dict[int, object] = {}
        objects_by_skeleton: dict[str, list[object]] = {}
        for obj in objects:
            base_object = getattr(obj, "base_object", None)
            if base_object is None:
                continue
            offset = int(getattr(base_object, "source_objt_offset", -1))
            if offset >= 0:
                objects_by_offset[offset] = obj
            skeleton_name = normalize_skeleton_name(
                getattr(base_object, "skeleton_name", ""))
            if skeleton_name:
                objects_by_skeleton.setdefault(
                    skeleton_name, []).append(obj)

        embedded_entries = tuple(getattr(
            getattr(self.window, "_vp_embedded", None), "entries", ()))
        embedded_by_index = {
            entry.index: entry for entry in embedded_entries
        }
        assigned_skeletons = {
            normalize_skeleton_name(entry.skeleton_name)
            for entry in embedded_entries
        }

        decoded_cache: dict[int, object] = {}
        sources: list[SnapshotSource] = []

        def decode(resource):
            cached = decoded_cache.get(resource.index)
            if cached is not None:
                return cached
            skeleton = decode_skeleton(archive, resource)
            decoded_cache[resource.index] = skeleton
            return skeleton

        def classification(skeleton) -> tuple[str, str]:
            if skeleton is None:
                return "ERROR", "Skeleton could not be decoded"
            drawable = drawable_polygon_count(skeleton)
            if drawable:
                return "", ""
            if not skeleton.points and not skeleton.polygons:
                return "EMPTY", "Skeleton contains no points or polygons"
            if skeleton.points and not skeleton.polygons:
                return (
                    "PARTICLE_ONLY",
                    "Point/particle-only VP: no static polygon mesh exists. "
                    "Dynamic particle simulation is not part of the Snapshot renderer.",
                )
            return (
                "NON_MESH",
                "Skeleton contains no polygon with at least three vertices",
            )

        for entry in vp_entries:
            embedded = embedded_by_index.get(entry.index)
            skeleton_key = (
                normalize_skeleton_name(embedded.skeleton_name)
                if embedded is not None else ""
            )
            candidates = resources_by_name.get(skeleton_key, ())
            resource = candidates[0] if candidates else None
            if resource is None:
                sources.append(SnapshotSource(
                    "VP", entry.index, entry.index,
                    entry.base_name, entry.comment,
                    -1, embedded.skeleton_name if embedded else "",
                    f"VP_{entry.index:04d}_{safe_component(Path(entry.base_name).stem)}",
                    "", embedded.skeleton_name if embedded else "",
                    None, None, False,
                    "MISSING_RESOURCE",
                    "The VP skeleton resource is not present in SET.BAS",
                ))
                continue

            try:
                skeleton = decode(resource)
                skip_status, skip_message = classification(skeleton)
            except Exception as exc:
                skeleton = None
                skip_status = "DECODE_ERROR"
                skip_message = str(exc)

            mapped = None
            if embedded is not None and embedded.source_objt_offset >= 0:
                mapped = objects_by_offset.get(
                    embedded.source_objt_offset)
            if mapped is None:
                mapped_candidates = objects_by_skeleton.get(
                    skeleton_key, ())
                mapped = mapped_candidates[0] if mapped_candidates else None

            sources.append(SnapshotSource(
                "VP", entry.index, entry.index,
                entry.base_name, entry.comment,
                resource.index, resource.resource_name,
                f"VP_{entry.index:04d}_{safe_component(Path(entry.base_name).stem)}",
                str(getattr(mapped, "owner_path", "") if mapped else ""),
                resource.resource_name,
                mapped, skeleton, mapped is None,
                skip_status, skip_message,
            ))

        for resource in sklt_resources:
            skeleton_key = normalize_skeleton_name(
                resource.resource_name)
            if skeleton_key in assigned_skeletons:
                continue

            try:
                skeleton = decode(resource)
                skip_status, skip_message = classification(skeleton)
            except Exception as exc:
                skeleton = None
                skip_status = "DECODE_ERROR"
                skip_message = str(exc)

            mapped_candidates = objects_by_skeleton.get(
                skeleton_key, ())
            mapped = mapped_candidates[0] if mapped_candidates else None
            stem = safe_component(Path(
                resource.resource_name.replace("\\", "/")).stem)

            sources.append(SnapshotSource(
                "SKLT", resource.index, -1,
                "", "", resource.index, resource.resource_name,
                f"SKLT_{resource.index:04d}_{stem}",
                str(getattr(mapped, "owner_path", "") if mapped else ""),
                resource.resource_name,
                mapped, skeleton, mapped is None,
                skip_status, skip_message,
            ))

            if len(sources) % 40 == 0:
                QApplication.processEvents()
                if self._cancel_requested:
                    raise RuntimeError("Batch cancelled during archive scan")

        return sources

    def _family_descendants(self, owner: str) -> set[str]:
        prefix = owner + "/"
        return {
            obj.owner_path for obj in self._family.all_objects()
            if obj.owner_path == owner
            or obj.owner_path.startswith(prefix)
        }

    def _geometry_family(self, source: SnapshotSource) -> AssetFamily:
        """Create a read-only geometry-only family for an unmapped SKLT."""

        family = AssetFamily()
        family.base_path = None
        family.setbas_archive = getattr(
            self.window, "_setbas", None)
        family.setbas_path = Path(
            getattr(family.setbas_archive, "path", ""))

        base_object = BaseObject()
        base_object.name = Path(
            source.resource_name.replace("\\", "/")).stem
        base_object.skeleton_name = source.resource_name

        fam_obj = FamilyObject(
            base_object=base_object,
            skeleton=source.decoded_skeleton,
            owner_path="root",
        )
        family.root_object = fam_obj
        family.warnings.append(
            f"Geometry-only snapshot for {source.resource_name}: "
            "no BASE material mapping exists in this SET.BAS."
        )
        return family

    def _load_source(self, source: SnapshotSource) -> None:
        key = (source.source_kind, source.source_id)
        if self._active_source_key == key:
            return
        self._active_source_key = key

        viewport = self._batch_viewport
        if viewport is None:
            raise RuntimeError("Batch viewport is unavailable")

        if source.mapped_object is not None:
            owner = source.owner_path
            viewport.load_family(
                self._family,
                visible_owners=self._family_descendants(owner),
                primary_owner=owner,
                reuse_indexed_adapter=True,
            )
            viewport.set_selected_owner(owner)
        else:
            geometry_family = self._geometry_family(source)
            viewport.load_family(
                geometry_family,
                visible_owners={"root"},
                primary_owner="root",
            )
            viewport.set_selected_owner("root")

        viewport.begin_snapshot_mode(None)
        viewport.play_animation(False)
        viewport.reset_animation()
        viewport.set_snapshot_guides_visible(False)
        if not viewport.has_model:
            raise RuntimeError(
                "The selected source produced no renderable polygon faces")

    def _step(self) -> None:
        if not self._running:
            return
        if self._cancel_requested:
            self._finish(cancelled=True)
            return
        if self._queue_index >= len(self._queue):
            self._finish(cancelled=False)
            return

        source, view_index, view = self._queue[self._queue_index]
        self._queue_index += 1
        self.progress.setValue(self._queue_index)
        self.status_label.setText(
            f"{source.folder_name} • {view_index}/{len(VIEW_PRESET_ANGLES)}")

        folder = self._root / source.folder_name
        folder.mkdir(parents=True, exist_ok=True)
        output_path = folder / (
            f"{view_index:02d}_{view_slug(view)}.png")
        relative = str(
            output_path.relative_to(self._root)).replace("\\", "/")

        if self.skip_existing_check.isChecked() \
                and output_path.is_file() \
                and output_path.stat().st_size > 0:
            self._record(
                source, view, relative,
                "EXISTS", "Existing non-empty PNG retained; renderer and "
                "source profile were not verified",
                existing_unverified=True)
            self._existing += 1
            QTimer.singleShot(0, self._step)
            return

        try:
            self._load_source(source)
        except Exception as exc:
            message = str(exc)
            self._record(
                source, view, "", "ERROR", message,
                renderer_reason=message)
            self._warnings.append(
                f"{source.folder_name} [{view}]: {message}")
            self._failed += 1
            QTimer.singleShot(0, self._step)
            return

        had_existing_final = output_path.is_file()
        renderer_info = None
        try:
            self._batch_viewport.apply_snapshot_preset(
                view, self._target_size, self._zoom)
            image = self._batch_viewport.render_snapshot(
                self._target_size,
                None,
                include_guides=False,
            )
            if image.isNull():
                raise RuntimeError(
                    "Snapshot renderer returned a null image")
            renderer_info = self._batch_viewport.indexed_renderer_info
            # Prove exact renderer/profile provenance before committing the
            # PNG. Missing indexed fade statistics must fail closed rather
            # than being reconstructed from static viewer descriptors later.
            self._renderer_manifest_fields(
                renderer_info, status="WRITTEN")
            self._write_png_atomic(image, output_path)
        except Exception as exc:
            message = str(exc)
            retained = had_existing_final and output_path.is_file()
            status = (
                "ERROR_EXISTING_RETAINED" if retained else "ERROR")
            if retained:
                message = f"{message}; existing final retained"
            self._record(
                source, view, relative if retained else "", status, message,
                renderer_info=(
                    renderer_info
                    if renderer_info is not None else (
                        self._batch_viewport.indexed_renderer_info
                        if self._batch_viewport is not None else None)
                ),
                renderer_reason=message,
            )
            self._warnings.append(
                f"{source.folder_name} [{view}]: {message}")
            self._failed += 1
        else:
            mode = (
                "geometry-only" if source.geometry_only
                else "textured")
            self._record(
                source, view, relative, "WRITTEN",
                f"{image.width()}x{image.height()} transparent PNG ({mode})",
                renderer_info=(
                    renderer_info
                    if renderer_info is not None else (
                        self._batch_viewport.indexed_renderer_info
                        if self._batch_viewport is not None else None)
                ),
            )
            self._written += 1

        QTimer.singleShot(0, self._step)

    @staticmethod
    def _write_png_atomic(image, output_path: Path) -> None:
        """Commit a PNG only after a complete sibling temporary write."""

        partial = output_path.with_name(f"{output_path.name}.part")
        try:
            if partial.exists():
                partial.unlink()
            writer = QImageWriter(str(partial), b"png")
            written = writer.write(image)
            writer_error = writer.errorString()
            del writer
            if not written:
                raise OSError(
                    writer_error
                    or f"Could not write {partial}")
            if not partial.is_file() or partial.stat().st_size <= 0:
                raise OSError(
                    f"PNG writer produced no non-empty file at {partial}")
            os.replace(partial, output_path)
        except Exception:
            try:
                if partial.exists():
                    partial.unlink()
            except OSError:
                pass
            raise

    def _record(
            self, source: SnapshotSource, view: str,
            relative_file: str, status: str, message: str,
            renderer_info: dict | None = None,
            existing_unverified: bool = False,
            renderer_reason: str = "") -> None:
        renderer = self._renderer_manifest_fields(
            renderer_info,
            status=status,
            existing_unverified=existing_unverified,
            reason=renderer_reason,
        )
        self._rows.append(BatchManifestRow(
            source.source_kind,
            source.source_id,
            source.vp_id,
            source.base_name,
            source.resource_index,
            source.resource_name,
            source.owner_path,
            source.skeleton_name,
            view,
            relative_file,
            status,
            message,
            renderer["requested_renderer"],
            renderer["effective_renderer"],
            renderer["fallback_used"],
            renderer["fallback_reason"],
            renderer["palette_sha256"],
            renderer["shadermp_sha256"],
            renderer["tracyrmp_sha256"],
            renderer["index_buffer_sha256"],
            renderer["requested_flat_tracy_destination_mode"],
            renderer["requested_flat_tracy_forced_destination_index"],
            renderer["effective_flat_tracy_destination_mode"],
            renderer["effective_flat_tracy_destination_class"],
            renderer["effective_flat_tracy_forced_destination_index"],
            renderer["effective_initial_framebuffer_index"],
            renderer["effective_initial_framebuffer_rgb"],
            renderer["effective_flat_tracy_forced_destination_rgb"],
            renderer["requested_distance_fade_enabled"],
            renderer["effective_distance_fade_enabled"],
            renderer["distance_fade_profile_id"],
            renderer["distance_fade_visibility_limit"],
            renderer["distance_fade_start"],
            renderer["distance_fade_length"],
            renderer["distance_fade_distance_space"],
            renderer["distance_fade_formula"],
        ))

    def _renderer_manifest_fields(
            self, renderer_info: dict | None,
            *, status: str = "WRITTEN",
            existing_unverified: bool = False,
            reason: str = "") -> dict[str, object]:
        status = str(status).upper()
        requested = (
            "retail_indexed_reconstructed"
            if self._renderer_mode == "textured_indexed"
            else "openua_preview"
        )
        existing_output = (
            existing_unverified
            or status in {"EXISTS", "ERROR_EXISTING_RETAINED"}
        )
        rendered_output = status == "WRITTEN"
        if existing_output:
            effective = "existing_file_not_verified"
            fallback = False
            fallback_reason = reason or (
                "existing file retained without renderer/source verification")
        elif not rendered_output:
            effective = "not_rendered"
            if requested == "retail_indexed_reconstructed":
                fallback = bool(
                    isinstance(renderer_info, dict)
                    and renderer_info.get("fallback_used", False)
                )
                fallback_reason = str(
                    renderer_info.get("fallback_reason", "")
                    if isinstance(renderer_info, dict) else ""
                ) or reason
            else:
                fallback = False
                fallback_reason = reason
        elif requested == "openua_preview":
            effective = "openua_preview"
            fallback = False
            fallback_reason = ""
        elif renderer_info is None:
            effective = "not_rendered"
            fallback = False
            fallback_reason = reason
        else:
            effective = str(
                renderer_info.get("effective_mode", "not_rendered"))
            fallback = bool(renderer_info.get("fallback_used", False))
            fallback_reason = str(
                renderer_info.get("fallback_reason", "") or reason)

        indexed_attempt = (
            requested == "retail_indexed_reconstructed"
            and isinstance(renderer_info, dict)
            and status != "EXISTS"
        )
        sources = renderer_info.get("sources", {}) if indexed_attempt else {}
        stats = (
            renderer_info.get("last_render_stats", {})
            if indexed_attempt and rendered_output else {}
        )
        exact_indexed_output = (
            rendered_output
            and requested == "retail_indexed_reconstructed"
            and isinstance(renderer_info, dict)
            and effective in INDEXED_EFFECTIVE_RENDERERS
            and not fallback
        )
        requested_profile = _requested_destination_profile(
            getattr(self, "_renderer_mode", "textured"),
            getattr(
                self, "_flat_tracy_destination_mode", "live_framebuffer"),
            getattr(self, "_flat_tracy_forced_destination_index", 0),
            getattr(self, "_distance_fade_enabled", False),
        )
        requested_destination_mode = (
            str(requested_profile["flat_tracy_destination_mode"])
            if requested == "retail_indexed_reconstructed" else ""
        )
        requested_forced_index = (
            requested_profile["flat_tracy_forced_destination_index"]
            if requested == "retail_indexed_reconstructed" else None
        )
        requested_distance_fade_enabled = (
            bool(requested_profile["distance_fade_enabled"])
            if requested == "retail_indexed_reconstructed" else None
        )
        effective_destination_mode = ""
        effective_destination_class = ""
        effective_forced_index = None
        effective_initial_index = None
        effective_initial_rgb = ""
        effective_forced_rgb = ""
        effective_distance_fade_enabled = None
        distance_fade_profile_id = ""
        distance_fade_visibility_limit = None
        distance_fade_start = None
        distance_fade_length = None
        distance_fade_distance_space = ""
        distance_fade_formula = ""
        if exact_indexed_output:
            stats_fade_profile = stats.get("distance_fade_profile", {})
            if not isinstance(stats_fade_profile, dict):
                stats_fade_profile = {}
            stats_profile_id = str(
                stats_fade_profile.get("name", "") or "")
            stats_visibility_limit = _manifest_finite_float(
                stats_fade_profile.get("visibility_limit"))
            stats_fade_start = _manifest_finite_float(
                stats_fade_profile.get("fade_start"))
            stats_fade_length = _manifest_finite_float(
                stats_fade_profile.get("fade_length"))
            stats_formula = str(
                stats.get("distance_fade_formula", "") or "")
            renderer_distance_space = str(
                renderer_info.get("distance_fade_distance_space", "") or "")
            if requested_distance_fade_enabled:
                proven_profile = {
                    "distance_fade_profile_id": stats_profile_id,
                    "distance_fade_visibility_limit": stats_visibility_limit,
                    "distance_fade_start": stats_fade_start,
                    "distance_fade_length": stats_fade_length,
                    "distance_fade_distance_space": renderer_distance_space,
                    "distance_fade_formula": stats_formula,
                }
                expected_profile = {
                    key: requested_profile[key]
                    for key in proven_profile
                }
                if proven_profile != expected_profile:
                    raise RuntimeError(
                        "exact indexed distance-fade output lacks complete "
                        "raster-stat proof for the requested runtime profile"
                    )
            candidate_mode = str(
                renderer_info.get("flat_tracy_destination_mode", ""))
            if candidate_mode in _FLAT_TRACY_DESTINATION_MODES:
                effective_destination_mode = candidate_mode
                effective_destination_class = (
                    _FLAT_TRACY_DESTINATION_MODES[candidate_mode])
                effective_initial_index = _manifest_palette_index(
                    renderer_info.get("initial_framebuffer_index"))
                effective_initial_rgb = _manifest_rgb_hex(
                    renderer_info.get("initial_framebuffer_rgb"))
                if candidate_mode == "forced_diagnostic":
                    effective_forced_index = _manifest_palette_index(
                        renderer_info.get(
                            "flat_tracy_forced_destination_index"))
                    effective_forced_rgb = _manifest_rgb_hex(
                        renderer_info.get(
                            "flat_tracy_forced_destination_rgb"))
            effective_distance_fade_enabled = _manifest_bool(
                renderer_info.get("effective_distance_fade_enabled"))
            if effective_distance_fade_enabled is None:
                # Pre-toggle renderer metadata can only describe the existing
                # fade-disabled path.
                effective_distance_fade_enabled = False
            distance_fade_profile_id = (
                stats_profile_id
                if requested_distance_fade_enabled
                else "disabled_closeup_compatibility"
            )
            distance_fade_visibility_limit = _manifest_finite_float(
                stats_visibility_limit
                if requested_distance_fade_enabled
                else renderer_info.get("distance_fade_visibility_limit"))
            distance_fade_start = _manifest_finite_float(
                stats_fade_start
                if requested_distance_fade_enabled
                else renderer_info.get("distance_fade_start"))
            distance_fade_length = _manifest_finite_float(
                stats_fade_length
                if requested_distance_fade_enabled
                else renderer_info.get("distance_fade_length"))
            distance_fade_distance_space = renderer_distance_space
            distance_fade_formula = (
                stats_formula
                if requested_distance_fade_enabled
                else str(renderer_info.get("distance_fade_formula", "") or "")
            )
        return {
            "requested_renderer": requested,
            "effective_renderer": effective,
            "fallback_used": fallback,
            "fallback_reason": fallback_reason,
            "palette_sha256": str(
                sources.get("palette", {}).get("sha256", "")),
            "shadermp_sha256": str(
                sources.get("shader", {}).get("sha256", "")),
            "tracyrmp_sha256": str(
                sources.get("tracy", {}).get("sha256", "")),
            "index_buffer_sha256": str(
                stats.get("index_buffer_sha256", "")),
            "requested_flat_tracy_destination_mode": (
                requested_destination_mode),
            "requested_flat_tracy_forced_destination_index": (
                requested_forced_index),
            "effective_flat_tracy_destination_mode": (
                effective_destination_mode),
            "effective_flat_tracy_destination_class": (
                effective_destination_class),
            "effective_flat_tracy_forced_destination_index": (
                effective_forced_index),
            "effective_initial_framebuffer_index": effective_initial_index,
            "effective_initial_framebuffer_rgb": effective_initial_rgb,
            "effective_flat_tracy_forced_destination_rgb": (
                effective_forced_rgb),
            "requested_distance_fade_enabled": (
                requested_distance_fade_enabled),
            "effective_distance_fade_enabled": (
                effective_distance_fade_enabled),
            "distance_fade_profile_id": distance_fade_profile_id,
            "distance_fade_visibility_limit": (
                distance_fade_visibility_limit),
            "distance_fade_start": distance_fade_start,
            "distance_fade_length": distance_fade_length,
            "distance_fade_distance_space": distance_fade_distance_space,
            "distance_fade_formula": distance_fade_formula,
        }

    def _write_manifests(self, cancelled: bool) -> None:
        if self._root is None:
            return
        rows = [asdict(row) for row in self._rows]
        outcome_counts: dict[str, int] = {}
        written_effective_counts: dict[str, int] = {}
        for row in self._rows:
            outcome_counts[row.status] = outcome_counts.get(row.status, 0) + 1
            if row.status == "WRITTEN":
                written_effective_counts[row.effective_renderer] = (
                    written_effective_counts.get(row.effective_renderer, 0) + 1
                )
        written_source_profiles = sorted({
            (
                row.palette_sha256,
                row.shadermp_sha256,
                row.tracyrmp_sha256,
            )
            for row in self._rows
            if row.status == "WRITTEN"
            and (
                row.palette_sha256
                or row.shadermp_sha256
                or row.tracyrmp_sha256
            )
        })
        attempted_source_profiles = sorted({
            (
                row.palette_sha256,
                row.shadermp_sha256,
                row.tracyrmp_sha256,
            )
            for row in self._rows
            if row.status != "WRITTEN"
            and (
                row.palette_sha256
                or row.shadermp_sha256
                or row.tracyrmp_sha256
            )
        })
        (self._root / "manifest.json").write_text(
            json.dumps(rows, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        with (self._root / "manifest.csv").open(
                "w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=list(
                    BatchManifestRow.__dataclass_fields__),
            )
            writer.writeheader()
            for row in rows:
                writer.writerow(row)

        (self._root / "warnings.log").write_text(
            "\n".join(self._warnings)
            + ("\n" if self._warnings else ""),
            encoding="utf-8",
        )
        setbas_path = str(getattr(
            getattr(self.window, "_setbas", None),
            "path", "",
        ))
        requested_profile = _requested_destination_profile(
            self._renderer_mode,
            self._flat_tracy_destination_mode,
            self._flat_tracy_forced_destination_index,
            getattr(self, "_distance_fade_enabled", False),
        )
        requested_destination_mode = requested_profile[
            "flat_tracy_destination_mode"]
        requested_forced_index = requested_profile[
            "flat_tracy_forced_destination_index"]
        requested_distance_fade_enabled = requested_profile[
            "distance_fade_enabled"]
        requested_distance_fade_profile = (
            _recorded_distance_fade_profile(requested_profile))
        renderer_summary = {
            "requested_renderer": (
                "retail_indexed_reconstructed"
                if self._renderer_mode == "textured_indexed"
                else "openua_preview"
            ),
            "outcome_counts": outcome_counts,
            "written_effective_image_counts": written_effective_counts,
            "fallback_written_rows": sum(
                1 for row in self._rows
                if row.status == "WRITTEN" and row.fallback_used),
            "requested_flat_tracy_destination_mode": (
                requested_destination_mode
                if self._renderer_mode == "textured_indexed" else None
            ),
            "requested_flat_tracy_destination_class": (
                _FLAT_TRACY_DESTINATION_MODES[
                    str(requested_destination_mode)]
                if self._renderer_mode == "textured_indexed" else None
            ),
            "requested_flat_tracy_forced_destination_index": (
                requested_forced_index
                if self._renderer_mode == "textured_indexed" else None
            ),
            "requested_distance_fade_enabled": (
                requested_distance_fade_enabled
                if self._renderer_mode == "textured_indexed" else None
            ),
            "requested_distance_fade_profile": (
                requested_distance_fade_profile
                if self._renderer_mode == "textured_indexed" else None
            ),
            "verified_written_flat_tracy_destination_profiles": [
                {
                    "mode": mode,
                    "class": mode_class,
                    "initial_framebuffer_index": initial_index,
                    "forced_destination_index": forced_index,
                }
                for (
                    mode,
                    mode_class,
                    initial_index,
                    forced_index,
                ) in sorted({
                    (
                        row.effective_flat_tracy_destination_mode,
                        row.effective_flat_tracy_destination_class,
                        row.effective_initial_framebuffer_index,
                        row.effective_flat_tracy_forced_destination_index,
                    )
                    for row in self._rows
                    if row.status == "WRITTEN"
                    and row.effective_flat_tracy_destination_mode
                }, key=lambda profile: tuple(
                    "" if value is None else str(value)
                    for value in profile))
            ],
            "verified_written_distance_fade_profiles": [
                {
                    "enabled": enabled,
                    "profile_id": profile_id,
                    "visibility_limit": visibility_limit,
                    "start": start,
                    "length": length,
                    "distance_space": distance_space,
                    "formula": formula,
                }
                for (
                    enabled,
                    profile_id,
                    visibility_limit,
                    start,
                    length,
                    distance_space,
                    formula,
                ) in sorted({
                    (
                        row.effective_distance_fade_enabled,
                        row.distance_fade_profile_id,
                        row.distance_fade_visibility_limit,
                        row.distance_fade_start,
                        row.distance_fade_length,
                        row.distance_fade_distance_space,
                        row.distance_fade_formula,
                    )
                    for row in self._rows
                    if row.status == "WRITTEN"
                    and row.effective_distance_fade_enabled is not None
                }, key=lambda profile: tuple(
                    "" if value is None else str(value)
                    for value in profile))
            ],
            "destination_profile_output_collision_warning": (
                "Destination mode/index changes do not alter image filenames. "
                "Distance-fade changes do not alter them either. Skip-existing "
                "resume is permitted only when run_info.json proves the same "
                "renderer/destination/fade profile; otherwise use a separate "
                "output folder or overwrite intentionally."
            ),
            "skip_existing_collision_policy": (
                "populated_output_requires_matching_run_info_renderer_and_"
                "destination_and_distance_fade_profile"
            ),
            "source_profiles": [
                {
                    "palette_sha256": palette,
                    "shadermp_sha256": shader,
                    "tracyrmp_sha256": tracy,
                }
                for palette, shader, tracy in written_source_profiles
            ],
            "note": (
                "Only WRITTEN rows contribute effective renderer counts and "
                "source profiles. EXISTS and ERROR_EXISTING_RETAINED files "
                "are retained but not renderer-verified; per-image provenance "
                "is authoritative in manifest.json. Destination mode/index "
                "and distance-fade changes do not alter filenames; Skip "
                "existing refuses a populated folder unless its prior "
                "run_info proves the same renderer/destination/fade profile"
            ),
        }
        if attempted_source_profiles:
            renderer_summary["attempted_source_profiles"] = [
                {
                    "palette_sha256": palette,
                    "shadermp_sha256": shader,
                    "tracyrmp_sha256": tracy,
                }
                for palette, shader, tracy in attempted_source_profiles
            ]

        run_info = {
            "feature": "OpenUAStudio Snapshot Studio complete model batch",
            "setbas": setbas_path,
            "setbas_sha256": file_sha256(setbas_path) if setbas_path else "",
            "vp_source": str(getattr(
                self.window, "_vp_source", "")),
            "vp_source_path": str(getattr(
                self.window, "_vp_source_path", "") or ""),
            "source_count": len(self._sources),
            "renderable_source_count": len({
                (row.source_kind, row.source_id)
                for row in self._rows
                if row.view
            }),
            "views": list(VIEW_PRESET_ANGLES),
            "width": self._target_size.width(),
            "height": self._target_size.height(),
            "zoom_percent": self._zoom,
            "format": "PNG",
            "transparent_background": True,
            "guides": False,
            "animation_frame": "initial/reset",
            "renderer_mode": self._renderer_mode,
            "indexed_flat_tracy_destination_mode_requested": (
                requested_destination_mode
                if self._renderer_mode == "textured_indexed" else None
            ),
            "indexed_flat_tracy_forced_destination_index_requested": (
                requested_forced_index
                if self._renderer_mode == "textured_indexed" else None
            ),
            "indexed_distance_fade_enabled_requested": (
                requested_distance_fade_enabled
                if self._renderer_mode == "textured_indexed" else None
            ),
            "indexed_distance_fade_profile_requested": (
                requested_distance_fade_profile
                if self._renderer_mode == "textured_indexed" else None
            ),
            "renderer_summary": renderer_summary,
            "written": self._written,
            "existing": self._existing,
            "skipped_models": self._skipped_models,
            "failed_images": self._failed,
            "cancelled": cancelled,
            "elapsed_seconds": round(
                time.time() - self._started_at, 3),
            "scope": (
                "Every VISPROTO VP slot plus every additional embedded "
                "SKLT resource not assigned to VISPROTO"
            ),
            "non_mesh_policy": (
                "Point/particle-only resources are documented but not "
                "rendered because Snapshot is a static polygon renderer"
            ),
        }
        (self._root / "run_info.json").write_text(
            json.dumps(
                run_info, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _create_zip(self) -> Path | None:
        if self._root is None:
            return None
        zip_path = self._root.with_suffix(".zip")
        partial = zip_path.with_suffix(".zip.part")
        if partial.exists():
            partial.unlink()
        try:
            with zipfile.ZipFile(
                    partial, "w",
                    compression=zipfile.ZIP_DEFLATED,
                    compresslevel=1,
                    allowZip64=True) as archive:
                for path, relative in self._authorized_zip_files():
                    if self._cancel_requested:
                        raise InterruptedError
                    archive.write(
                        path,
                        arcname=f"{self._root.name}/{relative}",
                    )
                    QApplication.processEvents()
            os.replace(partial, zip_path)
        except InterruptedError:
            if partial.exists():
                partial.unlink()
            return None
        except Exception:
            try:
                if partial.exists():
                    partial.unlink()
            except OSError:
                pass
            raise
        return zip_path

    def _authorized_zip_files(self) -> list[tuple[Path, str]]:
        """Return only manifest-authorized images and fixed run metadata."""

        if self._root is None:
            return []
        root = self._root.resolve()
        requested = {
            row.relative_file
            for row in self._rows
            if row.status in _ZIP_IMAGE_STATUSES and row.relative_file
        }
        requested.update(_ZIP_METADATA_FILES)

        authorized: dict[str, Path] = {}
        for value in requested:
            raw = str(value)
            windows = PureWindowsPath(raw)
            relative = PurePosixPath(raw.replace("\\", "/"))
            if windows.drive or windows.is_absolute() \
                    or relative.is_absolute() \
                    or not relative.parts \
                    or any(part in {"", ".", ".."}
                           for part in relative.parts):
                continue
            path = (root / Path(*relative.parts)).resolve()
            try:
                path.relative_to(root)
            except ValueError:
                continue
            if path.is_file():
                authorized[relative.as_posix()] = path

        return [
            (authorized[relative], relative)
            for relative in sorted(authorized)
        ]

    def _dispose_batch_viewport(self) -> None:
        viewport = self._batch_viewport
        self._batch_viewport = None
        self._active_source_key = None
        if viewport is None:
            return
        try:
            viewport.play_animation(False)
            viewport.end_snapshot_mode()
        except Exception:
            pass
        viewport.deleteLater()

    def _finish(self, cancelled: bool) -> None:
        self._write_manifests(cancelled)
        zip_path = None
        if not cancelled and self.zip_check.isChecked():
            self.status_label.setText("Creating ZIP...")
            QApplication.processEvents()
            zip_path = self._create_zip()
            cancelled = self._cancel_requested

        self._dispose_batch_viewport()
        self._set_running(False)
        self.cancel_button.setEnabled(False)
        self.progress.setValue(self._queue_index)

        if cancelled:
            self.status_label.setText("Cancelled safely.")
            self.window.statusBar().showMessage(
                "Snapshot batch cancelled; completed PNG files were kept.",
                8000)
            return

        self.status_label.setText(
            f"{self._written} new • {self._existing} existing • "
            f"{self._failed} errors")
        self.window.statusBar().showMessage(
            f"Complete snapshot batch: {self._written} written, "
            f"{self._existing} existing, {self._skipped_models} "
            f"non-mesh/empty models documented, {self._failed} failed images.",
            12000,
        )
        message = (
            f"Output:\n{self._root}\n\n"
            f"Written: {self._written}\n"
            f"Existing: {self._existing}\n"
            f"Non-mesh/empty models documented: {self._skipped_models}\n"
            f"Failed images: {self._failed}"
        )
        if zip_path is not None:
            message += f"\n\nZIP:\n{zip_path}"
        QMessageBox.information(
            self, "Snapshot batch complete", message)
