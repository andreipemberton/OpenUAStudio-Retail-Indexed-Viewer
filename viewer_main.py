"""Viewer-only entry point for OpenUAStudio Snapshot Studio.

This bypasses the multi-tool startup selector and opens the read-only model
viewer directly.  An optional PC ``.base``/``.bas``/``SET.BAS`` source or an
extracted Urban Assault PlayStation prototype disc tree may be supplied.
"""

from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtWidgets import QApplication, QMessageBox

from snapshot_studio import SnapshotStudioWindow


APP_TITLE = "OpenUAStudio Retail Indexed Viewer"


def _open_startup_path(window: SnapshotStudioWindow, value: str) -> None:
    path = Path(value)
    # Command-line PSX import currently accepts an extracted disc tree only.
    # Individual meshes/UNIT.BIN paths require their containing build and are
    # deliberately not presented as standalone imports.
    if path.is_dir():
        window.open_psx_source(path)
    elif path.suffix.casefold() in {".psw", ".psv", ".pw3"} \
            or path.name.casefold() == "unit.bin":
        raise ValueError(
            "Native PlayStation mesh files require their extracted prototype "
            "disc tree. Open the directory containing SYSTEM.CNF and "
            "UNITMODL instead; the file will not be sent to the PC loader.")
    elif path.name.casefold() == "set.bas":
        window.open_setbas(path)
    elif path.suffix.casefold() in {".base", ".bas"}:
        window.open_base(path)
    else:
        raise ValueError(
            "Unsupported startup source. Open a PC .base/.bas file, PC "
            "SET.BAS archive, or the extracted PlayStation prototype disc "
            "directory containing SYSTEM.CNF. Individual PSX source files "
            "will not be sent to the PC loader.")


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName(APP_TITLE)
    app.setApplicationDisplayName("")

    window = SnapshotStudioWindow()
    window.show()
    if len(sys.argv) > 1:
        try:
            _open_startup_path(window, sys.argv[1])
        except ValueError as exc:
            QMessageBox.warning(
                window, "Unsupported startup source", str(exc))
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
