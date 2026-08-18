import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QWheelEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QAbstractScrollArea, QFrame

from startup_selector import TOOL_OPTIONS, StartupToolSelector


class StartupToolSelectorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_selector_exposes_the_five_startup_workspaces(self):
        self.assertEqual(
            [option.key for option in TOOL_OPTIONS],
            [
                "model_editor",
                "snapshot_studio",
                "map_editor",
                "collision_editor",
                "wireframe_editor",
            ],
        )
        self.assertNotIn(
            "mapping_repair",
            [option.key for option in TOOL_OPTIONS],
        )
        self.assertTrue(all(option.description for option in TOOL_OPTIONS))

    def test_model_editor_is_selected_by_default(self):
        dialog = StartupToolSelector()
        self.addCleanup(dialog.close)

        self.assertEqual(dialog.tool_list.count(), 5)
        self.assertEqual(dialog.selected_tool(), "model_editor")
        self.assertEqual(dialog.windowTitle(), "OpenUAStudio - Select Tool")

    def test_selection_returns_the_requested_tool_key(self):
        dialog = StartupToolSelector()
        self.addCleanup(dialog.close)

        dialog.tool_list.set_current_row(3)
        self.assertEqual(dialog.selected_tool(), "collision_editor")
        dialog.tool_list.set_current_row(4)
        self.assertEqual(dialog.selected_tool(), "wireframe_editor")

    def test_tool_card_is_clickable(self):
        dialog = StartupToolSelector()
        self.addCleanup(dialog.close)
        dialog.show()
        self.app.processEvents()

        cards = dialog.tool_list.findChildren(
            QFrame,
            "toolCard",
            Qt.FindChildOption.FindDirectChildrenOnly,
        )
        self.assertEqual([card.key for card in cards], [
            option.key for option in TOOL_OPTIONS
        ])
        card = cards[1]
        QTest.mouseClick(card, Qt.MouseButton.LeftButton)
        self.assertEqual(dialog.selected_tool(), "snapshot_studio")

    def test_all_workspace_cards_fit_without_vertical_scrolling(self):
        dialog = StartupToolSelector()
        self.addCleanup(dialog.close)
        dialog.show()
        self.app.processEvents()

        # This launcher is deliberately a fixed card panel, not an item view.
        # Prove that every card is laid out inside its non-scrolling bounds.
        self.assertNotIsInstance(dialog.tool_list, QAbstractScrollArea)
        self.assertEqual(
            dialog.tool_list.minimumHeight(),
            dialog.tool_list.maximumHeight(),
        )
        cards = dialog.tool_list.findChildren(
            QFrame,
            "toolCard",
            Qt.FindChildOption.FindDirectChildrenOnly,
        )
        self.assertEqual(len(cards), len(TOOL_OPTIONS))
        self.assertTrue(all(
            dialog.tool_list.rect().contains(card.geometry())
            for card in cards
        ))

    def test_workspace_list_ignores_wheel_scrolling(self):
        dialog = StartupToolSelector()
        self.addCleanup(dialog.close)
        dialog.show()
        self.app.processEvents()

        panel = dialog.tool_list
        before_row = panel.current_row()
        before_geometry = [card.geometry() for card in panel._cards]
        local_position = QPointF(panel.rect().center())
        global_position = QPointF(
            panel.mapToGlobal(panel.rect().center()))
        event = QWheelEvent(
            local_position,
            global_position,
            QPoint(),
            QPoint(0, -120),
            Qt.MouseButton.NoButton,
            Qt.KeyboardModifier.NoModifier,
            Qt.ScrollPhase.ScrollUpdate,
            False,
        )

        QApplication.sendEvent(panel, event)
        self.app.processEvents()

        self.assertEqual(panel.current_row(), before_row)
        self.assertEqual(
            [card.geometry() for card in panel._cards],
            before_geometry,
        )


if __name__ == "__main__":
    unittest.main()
