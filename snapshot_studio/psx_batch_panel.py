"""Snapshot Studio controller for deterministic native-PSX mesh batches.

The exporter owns the native render viewport and all filesystem transactions.
This panel deliberately does not enumerate assets or create output folders: it
only freezes explicit operator choices, advances one exporter transaction per
event-loop callback, and restores source controls on every terminal path.
"""

from __future__ import annotations

from pathlib import Path
import re

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
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
    QSpinBox,
    QVBoxLayout,
)

from psx_native_assets import PsxNativeBuild
from psx_native_textures import PsxNativeTexturePack
from snapshot_studio.psx_batch_export import (
    DEFAULT_PSX_NATIVE_BATCH_VIEWS,
    PsxNativeBatchConfig,
    PsxNativeBatchExporter,
    PsxNativeBatchProgress,
)


_UNSAFE_FOLDER = re.compile(r'[^A-Za-z0-9_.-]+')
_TOPOLOGY_MODE = "topology_only"
_CURRENT_PACK_MODE = "current_selected_pack"


def _safe_folder_name(value: str, fallback: str = "PSX_Source") -> str:
    """Return a short deterministic suggestion, never a filesystem path."""

    text = _UNSAFE_FOLDER.sub("_", str(value)).strip(" ._")
    return text[:80].rstrip(" ._") or fallback


class PsxNativeBatchPanel(QGroupBox):
    """Event-loop controller for the native mesh snapshot exporter."""

    def __init__(self, window) -> None:
        super().__init__("Batch Native PSX Meshes", window._psx_panel)
        self.window = window
        self.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

        self._running = False
        self._generation = 0
        self._step_scheduled = False
        self._exporter: PsxNativeBatchExporter | None = None
        self._last_result = None
        self._last_error: Exception | None = None
        self._suggested_for_build: PsxNativeBuild | None = None
        self._external_enabled_state: list[tuple[object, bool]] = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 5, 6, 6)
        layout.setSpacing(4)

        self.summary_label = QLabel(
            "Open an extracted PlayStation source to enable native mesh "
            "export.")
        self.summary_label.setWordWrap(True)
        layout.addWidget(self.summary_label)

        output_row = QHBoxLayout()
        output_row.setSpacing(4)
        output_row.addWidget(QLabel("Output:"))
        self.output_edit = QLineEdit()
        self.output_edit.setPlaceholderText(
            "Choose or type a native batch output folder...")
        output_row.addWidget(self.output_edit, 1)
        self.output_button = QPushButton("...")
        self.output_button.setFixedWidth(34)
        self.output_button.setToolTip(
            "Choose an existing parent/output folder. No folder is created "
            "until Export performs its preflight.")
        self.output_button.clicked.connect(self._choose_output)
        output_row.addWidget(self.output_button)
        layout.addLayout(output_row)

        render_grid = QGridLayout()
        render_grid.setContentsMargins(0, 0, 0, 0)
        render_grid.setHorizontalSpacing(5)
        render_grid.setVerticalSpacing(4)

        render_grid.addWidget(QLabel("Square size:"), 0, 0)
        self.size_spin = QSpinBox()
        self.size_spin.setRange(256, 4096)
        self.size_spin.setSingleStep(64)
        self.size_spin.setValue(1024)
        self.size_spin.setSuffix(" px")
        self.size_spin.setKeyboardTracking(False)
        render_grid.addWidget(self.size_spin, 0, 1)

        render_grid.addWidget(QLabel("Zoom:"), 0, 2)
        self.zoom_spin = QSpinBox()
        self.zoom_spin.setRange(25, 300)
        self.zoom_spin.setValue(100)
        self.zoom_spin.setSuffix("%")
        self.zoom_spin.setKeyboardTracking(False)
        render_grid.addWidget(self.zoom_spin, 0, 3)

        render_grid.addWidget(QLabel("Texture:"), 1, 0)
        self.texture_mode_combo = QComboBox()
        self.texture_mode_combo.addItem(
            "Topology only", _TOPOLOGY_MODE)
        self.texture_mode_combo.addItem(
            "Current explicitly selected pack", _CURRENT_PACK_MODE)
        self.texture_mode_combo.setToolTip(
            "Topology-only is the safe default. The second option freezes the "
            "exact native SETnGFX pack explicitly selected in PSX Archive; "
            "mesh-to-environment affinity is never inferred.")
        self.texture_mode_combo.currentIndexChanged.connect(self.refresh)
        render_grid.addWidget(self.texture_mode_combo, 1, 1, 1, 3)
        render_grid.setColumnStretch(3, 1)
        layout.addLayout(render_grid)

        views_box = QGroupBox("Fixed views")
        views_layout = QGridLayout(views_box)
        views_layout.setContentsMargins(6, 4, 6, 5)
        views_layout.setHorizontalSpacing(8)
        views_layout.setVerticalSpacing(2)
        self.view_checks: dict[str, QCheckBox] = {}
        for index, view_name in enumerate(DEFAULT_PSX_NATIVE_BATCH_VIEWS):
            checkbox = QCheckBox(view_name)
            checkbox.setChecked(True)
            checkbox.toggled.connect(self.refresh)
            self.view_checks[view_name] = checkbox
            views_layout.addWidget(checkbox, index % 5, index // 5)
        layout.addWidget(views_box)
        self.views_box = views_box

        options_row = QHBoxLayout()
        options_row.setSpacing(10)
        self.skip_existing_check = QCheckBox("Skip verified existing")
        self.skip_existing_check.setChecked(True)
        self.skip_existing_check.setToolTip(
            "Resume only when both PNG and JSON sidecar prove the exact frozen "
            "source, mesh, view, render configuration, and byte hashes.")
        options_row.addWidget(self.skip_existing_check)
        options_row.addStretch(1)
        layout.addLayout(options_row)
        self.contract_label = QLabel(
            "Transparent PNG + JSON • animations off • guides off")
        self.contract_label.setStyleSheet("color: #9fb3bd;")
        self.contract_label.setWordWrap(True)
        self.contract_label.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        layout.addWidget(self.contract_label)

        buttons_row = QHBoxLayout()
        buttons_row.setSpacing(4)
        self.export_button = QPushButton("Export Native Meshes")
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

        self._run_option_controls = (
            self.output_edit,
            self.output_button,
            self.size_spin,
            self.zoom_spin,
            self.texture_mode_combo,
            self.views_box,
            self.skip_existing_check,
        )
        self.refresh()

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def last_result(self):
        """Return the last successful/cancelled result for UI tests/tools."""

        return self._last_result

    @property
    def last_error(self) -> Exception | None:
        return self._last_error

    def selected_views(self) -> tuple[str, ...]:
        """Return checked presets in the exporter's fixed canonical order."""

        return tuple(
            name for name in DEFAULT_PSX_NATIVE_BATCH_VIEWS
            if self.view_checks[name].isChecked())

    def _current_pack(self) -> PsxNativeTexturePack | None:
        candidate = getattr(
            self.window, "_psx_selected_texture_pack", None)
        return (
            candidate if isinstance(candidate, PsxNativeTexturePack)
            else None)

    def _pack_for_export(self) -> PsxNativeTexturePack | None:
        if self.texture_mode_combo.currentData() != _CURRENT_PACK_MODE:
            return None
        pack = self._current_pack()
        if pack is None:
            raise RuntimeError(
                "Choose a validated native texture pack in PSX Archive, or "
                "select Topology only for this batch.")
        return pack

    def _pc_batch_running(self) -> bool:
        panel = getattr(self.window, "vp_batch_panel", None)
        return bool(panel is not None and panel.is_running)

    def refresh(self, *_args) -> None:
        """Refresh read-only counts without disturbing a frozen run."""

        if self._running:
            return
        build = getattr(self.window, "_psx_build", None)
        if not isinstance(build, PsxNativeBuild) or not build.meshes:
            self._suggested_for_build = None
            self.summary_label.setText(
                "Open an extracted PlayStation source to enable native mesh "
                "export. PC/OpenUA assets are never substituted.")
            self.export_button.setEnabled(False)
            self._set_current_pack_item(None)
            return

        if build is not self._suggested_for_build:
            self._suggested_for_build = build
            self.texture_mode_combo.setCurrentIndex(0)
            source_name = _safe_folder_name(
                build.root.name or Path(
                    build.boot_executable_logical_path).stem)
            # Keep generated artifacts outside the extracted-disc source tree
            # even though AssemblyWindow remembers that source as its most
            # recent directory.
            base_dir = build.root.expanduser().resolve(strict=False).parent
            # This is text only; refresh must never create the suggestion.
            self.output_edit.setText(str(
                base_dir / f"{source_name}_PSX_Native_Snapshot_Corpus"))

        pack = self._current_pack()
        self._set_current_pack_item(pack)
        views = len(self.selected_views())
        total = len(build.meshes) * views
        texture = (
            Path(pack.logical_path).name
            if self.texture_mode_combo.currentData() == _CURRENT_PACK_MODE
            and pack is not None else "topology only")
        suffix = (
            " • PC batch is running"
            if self._pc_batch_running() else "")
        self.summary_label.setText(
            f"Native meshes: {len(build.meshes)}  •  Views: {views}/"
            f"{len(DEFAULT_PSX_NATIVE_BATCH_VIEWS)}  •  Images: {total:,}  •  "
            f"Texture: {texture}{suffix}")
        self.export_button.setEnabled(
            views > 0 and not self._pc_batch_running())

    def _set_current_pack_item(
            self, pack: PsxNativeTexturePack | None) -> None:
        model_item = self.texture_mode_combo.model().item(1)
        if pack is None:
            self.texture_mode_combo.setItemText(
                1, "Current pack unavailable")
            self.texture_mode_combo.setItemData(
                1,
                "Choose a validated native SETnGFX pack in PSX Archive.",
                Qt.ItemDataRole.ToolTipRole,
            )
            if self.texture_mode_combo.currentIndex() == 1:
                self.texture_mode_combo.setCurrentIndex(0)
            if model_item is not None:
                model_item.setEnabled(False)
            return
        self.texture_mode_combo.setItemText(
            1, f"Current: {Path(pack.logical_path).name}")
        self.texture_mode_combo.setItemData(
            1,
            f"{pack.logical_path}\nSHA-256: {pack.source_sha256}\n"
            "This exact operator-selected pack is frozen for the full run.",
            Qt.ItemDataRole.ToolTipRole,
        )
        if model_item is not None:
            model_item.setEnabled(True)

    def _choose_output(self) -> None:
        current = self.output_edit.text().strip()
        path = QFileDialog.getExistingDirectory(
            self,
            "Choose native PSX snapshot output folder",
            current or str(Path.home()),
        )
        if path:
            self.output_edit.setText(path)

    def _lock_external_controls(self) -> None:
        self._external_enabled_state.clear()
        candidates = (
            getattr(self.window, "vp_batch_panel", None),
            getattr(self.window, "open_psx_action", None),
            getattr(self.window, "psx_source_button", None),
            getattr(self.window, "psx_forget_button", None),
            getattr(self.window, "psx_load_button", None),
            getattr(self.window, "psx_asset_tree", None),
            getattr(self.window, "psx_texture_set_combo", None),
            getattr(self.window, "snapshot_renderer_combo", None),
            getattr(self.window, "mode_combo", None),
        )
        seen: set[int] = set()
        for control in candidates:
            if control is None or id(control) in seen:
                continue
            seen.add(id(control))
            try:
                enabled = bool(control.isEnabled())
                self._external_enabled_state.append((control, enabled))
                control.setEnabled(False)
            except RuntimeError:
                # A closing window may already have destroyed an inherited
                # control; generation checks still prevent stale callbacks.
                continue

    def _restore_external_controls(self) -> None:
        states, self._external_enabled_state = (
            self._external_enabled_state, [])
        for control, enabled in states:
            try:
                control.setEnabled(enabled)
            except RuntimeError:
                continue
        pc_panel = getattr(self.window, "vp_batch_panel", None)
        if pc_panel is not None:
            try:
                pc_panel.refresh()
            except RuntimeError:
                pass

    def _set_running(self, running: bool) -> None:
        if running == self._running:
            return
        self._running = running
        if running:
            self._lock_external_controls()
        for control in self._run_option_controls:
            control.setEnabled(not running)
        self.export_button.setEnabled(False)
        self.cancel_button.setEnabled(running)
        if not running:
            self._restore_external_controls()
            self.refresh()

    def _show_preflight_error(self, message: str) -> None:
        QMessageBox.warning(
            self,
            "Native PSX batch cannot start",
            f"No output folder or source asset was modified.\n\n{message}",
        )
        self.status_label.setText("Preflight refused.")

    def start(self) -> None:
        """Freeze the active native source/options and begin asynchronous work."""

        if self._running:
            return
        if self._pc_batch_running():
            self._show_preflight_error(
                "Wait for the PC SET.BAS/VP snapshot batch to finish.")
            return

        build = getattr(self.window, "_psx_build", None)
        if not isinstance(build, PsxNativeBuild) or not build.meshes:
            self._show_preflight_error(
                "Open a supported extracted PlayStation source first.")
            return
        output_text = self.output_edit.text().strip()
        if not output_text:
            self._choose_output()
            output_text = self.output_edit.text().strip()
        if not output_text:
            return
        views = self.selected_views()
        if not views:
            self._show_preflight_error(
                "Select at least one of the ten fixed camera views.")
            return

        try:
            pack = self._pack_for_export()
            if pack is not None and not any(
                    candidate is pack for candidate in build.texture_packs):
                raise RuntimeError(
                    "The current native texture pack no longer belongs to "
                    "the active PlayStation source.")
            output_root = Path(output_text).expanduser().resolve(strict=False)
            source_root = build.root.expanduser().resolve(strict=False)
            try:
                output_root.relative_to(source_root)
            except ValueError:
                pass
            else:
                raise RuntimeError(
                    "Choose an output folder outside the extracted "
                    "PlayStation source tree.")
            config = PsxNativeBatchConfig(
                output_root=output_root,
                width=self.size_spin.value(),
                height=self.size_spin.value(),
                zoom_percent=self.zoom_spin.value(),
                views=views,
                texture_pack=pack,
                background_rgba=None,
                skip_existing=self.skip_existing_check.isChecked(),
            )
            exporter = PsxNativeBatchExporter(build, config)
        except Exception as exc:
            self._last_error = exc
            self._show_preflight_error(str(exc))
            return

        self._generation += 1
        token = self._generation
        self._exporter = exporter
        self._last_result = None
        self._last_error = None
        self._step_scheduled = False
        self._set_running(True)
        self.status_label.setText("Preflighting native snapshot pairs...")
        try:
            progress = exporter.start()
        except Exception as exc:
            self._fail(exc)
            return

        self._update_progress(progress)
        if exporter.done:
            self._finish_terminal()
            return
        self.window.statusBar().showMessage(
            f"Native PSX mesh batch started: {len(build.meshes)} meshes × "
            f"{len(views)} fixed views.")
        self._schedule_step(token)

    def _schedule_step(self, token: int) -> None:
        if not self._running or token != self._generation \
                or self._step_scheduled:
            return
        self._step_scheduled = True
        # Capturing the generation prevents callbacks queued by an earlier run
        # from advancing a replacement exporter after cancellation/failure.
        QTimer.singleShot(0, lambda: self._advance_one(token))

    def _advance_one(self, token: int) -> None:
        self._step_scheduled = False
        if not self._running or token != self._generation:
            return
        exporter = self._exporter
        if exporter is None:
            self._fail(RuntimeError(
                "native batch controller lost its frozen exporter"))
            return
        try:
            # Exactly one PNG/JSON transaction is permitted per timer callback.
            progress = exporter.step()
        except Exception as exc:
            self._fail(exc)
            return
        self._update_progress(progress)
        if exporter.done:
            self._finish_terminal()
        else:
            self._schedule_step(token)

    def _update_progress(self, progress: PsxNativeBatchProgress) -> None:
        self.progress.setRange(0, max(1, progress.total))
        self.progress.setValue(progress.completed)
        if progress.state == "running":
            current = progress.current_relative_png or "next native snapshot"
            self.status_label.setText(
                f"{progress.completed}/{progress.total} • {current}")
        elif progress.state == "complete":
            self.status_label.setText(
                f"Complete: {progress.written} written • "
                f"{progress.skipped_verified} verified")
        elif progress.state == "cancelled":
            self.status_label.setText(
                f"Cancelled safely after {progress.completed}/"
                f"{progress.total}.")

    def _finish_terminal(self) -> None:
        exporter = self._exporter
        if exporter is None:
            self._fail(RuntimeError(
                "native batch completed without its frozen exporter"))
            return
        try:
            result = exporter.result
        except Exception as exc:
            self._fail(exc)
            return

        self._last_result = result
        self._exporter = None
        self._generation += 1
        self._step_scheduled = False
        self._set_running(False)
        self.progress.setRange(0, max(1, result.total))
        self.progress.setValue(len(result.records))
        if result.cancelled:
            self.status_label.setText(
                f"Cancelled safely: {len(result.records)}/{result.total} "
                "pairs retained.")
            self.window.statusBar().showMessage(
                "Native PSX batch cancelled; committed PNG/JSON pairs and the "
                "cancelled manifest were kept.",
                9000,
            )
            return

        self.status_label.setText(
            f"Complete: {result.written} new • "
            f"{result.skipped_verified} verified")
        self.window.statusBar().showMessage(
            f"Native PSX mesh batch complete: {result.written} written, "
            f"{result.skipped_verified} verified existing.",
            12000,
        )
        QMessageBox.information(
            self,
            "Native PSX batch complete",
            f"Output:\n{result.manifest_path.parent}\n\n"
            f"Written: {result.written}\n"
            f"Verified existing: {result.skipped_verified}\n"
            f"Manifest:\n{result.manifest_path}",
        )

    def _fail(self, exc: Exception) -> None:
        self._last_error = exc
        self._exporter = None
        self._generation += 1
        self._step_scheduled = False
        self._set_running(False)
        self.status_label.setText("Native batch failed safely.")
        self.window.statusBar().showMessage(
            "Native PSX batch failed; source assets were not modified.",
            9000,
        )
        QMessageBox.critical(
            self,
            "Native PSX batch failed",
            "The extracted source assets were not modified. Any fully "
            f"committed snapshot pairs were retained.\n\n{exc}",
        )

    def request_cancel(self) -> None:
        """Ask the exporter to stop before its next image transaction."""

        if not self._running:
            return
        exporter = self._exporter
        if exporter is not None:
            exporter.request_cancel()
        self.cancel_button.setEnabled(False)
        self.status_label.setText("Cancelling after the current pair...")
        self.window.statusBar().showMessage(
            "Native PSX batch cancellation requested.")


# A descriptive alias keeps external callers/tests independent of the compact
# class name while the window exposes ``psx_native_batch_panel`` as canonical.
PsxNativeSnapshotBatchPanel = PsxNativeBatchPanel


__all__ = [
    "PsxNativeBatchPanel",
    "PsxNativeSnapshotBatchPanel",
]
