import tempfile
import unittest
from types import SimpleNamespace as NS
from unittest.mock import patch
import json
from pathlib import Path

from toolset import SafeCalculator, ToolController


class CalculatorTests(unittest.TestCase):
    def test_scientific_expression_is_safe_and_supports_degrees(self):
        calc = SafeCalculator()
        calc.expression = "sin(30) + sqrt(16) + 2^3"
        self.assertEqual(calc.evaluate(), "12.5")

    def test_unsupported_attribute_and_import_are_rejected(self):
        calc = SafeCalculator()
        calc.expression = "__import__('os').system('whoami')"
        self.assertEqual(calc.press("="), (calc.expression, "Error"))

    def test_sessions_and_history_survive_restart(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "tools.json"
            first = ToolController(path)
            first.press("7")
            first.press("×")
            first.press("6")
            first.press("=")
            first.new_calculator()
            first.press("2")
            first.press("+")
            first.press("3")
            first.press("=")

            second = ToolController(path)
            self.assertEqual(second.calculator.result, "5")
            self.assertEqual(len(second.recent(10)), 2)
            self.assertIn("42", {row["result"] for row in second.recent(10)})

    def test_real_scientific_button_sequences(self):
        for tokens, result in ((["9", "²√x", "="], "3"), (["2", "x³", "="], "8"),
                               (["8", "ʸ√x", "3", "="], "2"), (["5", "x!", "="], "120"),
                               (["sin", "3", "0", "="], "0.5"), (["0.5", "asin", "="], "30"),
                               (["1", "eˣ", "="], "2.71828182846")):
            calc = SafeCalculator()
            for token in tokens:
                calc.press(token)
            self.assertEqual(calc.result, result, tokens)
        self.assertEqual(SafeCalculator().evaluate("1e-15"), "1e-15")
        with patch("toolset.random.random", return_value=.314159):
            calc.press("Rand")
            self.assertEqual(calc.expression, "0.314159")

    def test_history_beyond_old_limit_and_recall_angle_mode(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/"tools.json"
            tools = ToolController(path)
            tools.calculator.angle_mode = "RAD"
            for i in range(110):
                tools.calculator.expression = str(i)+"+1"
                tools.press("=")
            tools.notes = "price=120\nprice*1.16"
            tools.set_mode("notes")
            loaded = ToolController(path)
            self.assertEqual(len(loaded.history), 110)
            self.assertEqual(loaded.note_results(), ["120", "139.2"])
            loaded.calculator.angle_mode = "DEG"
            loaded.recall(109)
            self.assertEqual(loaded.calculator.result, "1")
            self.assertEqual(loaded.calculator.angle_mode, "RAD")
            self.assertEqual(loaded.mode, "notes")

    def test_units_temperature_domains_and_notes_safety(self):
        with tempfile.TemporaryDirectory() as folder:
            tools = ToolController(Path(folder)/"tools.json")
            tools.calculator.expression = "1"
            tools.select_conversion("kind", "Length")
            tools.select_conversion("from", "mi")
            tools.select_conversion("to", "km")
            self.assertEqual(tools.conversion(), "1.609344")
            tools.select_conversion("kind", "Temperature")
            tools.calculator.expression = "100"
            self.assertEqual(tools.conversion(), "212")
            tools.calculator.expression = "-274"
            self.assertEqual(tools.conversion(), "—")
            tools.notes = "x=5\nx^2\n__import__('os')\nx+3"
            self.assertEqual(tools.note_results(), ["5", "25", "—", "8"])

    def test_currency_cache_is_daily_persistent_and_honest_offline(self):
        with tempfile.TemporaryDirectory() as folder:
            tools = ToolController(Path(folder)/"tools.json")
            body = json.dumps({"base": "USD", "date": "2026-09-21", "rates": {"MXN": 17.2, "EUR": .87}}).encode()
            from unittest.mock import MagicMock
            response = MagicMock()
            response.__enter__.return_value.read.return_value = body
            with patch("toolset.urllib.request.urlopen", return_value=response) as request, \
                 patch("toolset.threading.Thread", side_effect=lambda target, **kw: NS(start=target)):
                tools.select_conversion("kind", "Currency")
                self.assertTrue(tools.poll_rates())
                tools.calculator.expression = "10"
                self.assertEqual(tools.conversion(), "172")
                tools.refresh_rates()
                self.assertEqual(request.call_count, 1)
                request.side_effect = OSError("offline")
                tools.refresh_rates(force=True)
                tools.poll_rates()
                self.assertIn("cached", tools.rate_status)
                self.assertEqual(tools.conversion(), "172")
            loaded = ToolController(tools.path)
            self.assertEqual(loaded.rates["MXN"], 17.2)
            self.assertEqual(loaded.rate_date, "2026-09-21")


if __name__ == "__main__":
    unittest.main()
