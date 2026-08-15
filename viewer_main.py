"""Viewer-only entry point for OpenUAStudio Snapshot Studio.

This bypasses the multi-tool startup selector and opens the read-only model
viewer directly.  An optional ``.base``/``.bas`` file or ``SET.BAS`` archive
may be supplied on the command line.
"""

from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtWidgets import QApplication

from snapshot_studio import SnapshotStudioWindow


APP_TITLE = "OpenUAStudio Retail Indexed Viewer"


def _open_startup_path(window: SnapshotStudioWindow, value: str) -> None:
    path = Path(value)
    if path.name.casefold() == "set.bas":
        window.open_setbas(path)
    else:
        window.open_base(path)


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName(APP_TITLE)
    app.setApplicationDisplayName("")

    window = SnapshotStudioWindow()
    window.show()
    if len(sys.argv) > 1:
        _open_startup_path(window, sys.argv[1])
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
