from __future__ import annotations

import unittest

from model_service_workbench import ui


class UiContractTests(unittest.TestCase):
    def test_ui_module_imports_without_starting_tk(self) -> None:
        available, message = ui.tkinter_availability()
        self.assertIsInstance(available, bool)
        self.assertTrue(message)

    def test_check_mode_returns_stable_exit_code(self) -> None:
        code = ui.main(["--check"])
        available, _message = ui.tkinter_availability()
        self.assertEqual(code, 0 if available else 2)


if __name__ == "__main__":
    unittest.main()
