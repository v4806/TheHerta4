"""产物 ini 变量声明校验器（common/ini_variable_validator.py）的回归测试。"""
import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parents[1] / "common" / "ini_variable_validator.py"
_spec = importlib.util.spec_from_file_location("_ini_variable_validator_test", MODULE_PATH)
validator = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = validator
_spec.loader.exec_module(validator)


class AnalyzeIniTextTests(unittest.TestCase):
    def test_detects_undeclared_variables_with_first_section(self):
        text = "\n".join([
            "[Constants]",
            "global $a = 0",
            "global persist $b = 0",
            "[Present]",
            "if $a == 1",
            "\t$c = $b",
            "endif",
        ])

        report = validator.analyze_ini_text(text)

        self.assertIn("$c", report["undeclared"])
        self.assertEqual(report["undeclared"]["$c"], ["[Present]"])
        self.assertNotIn("$a", report["undeclared"])
        self.assertNotIn("$b", report["undeclared"])

    def test_local_declarations_and_builtins_are_not_reported(self):
        text = "\n".join([
            "[CommandListX]",
            "local $tmp = 0",
            "$tmp = $time + $cursor_x",
        ])

        report = validator.analyze_ini_text(text)

        self.assertEqual(report["undeclared"], {})
        self.assertIn("$tmp", report["local_declared"])

    def test_comment_only_mentions_do_not_count(self):
        text = "\n".join([
            "[Constants]",
            "global $a = 0",
            "[Present]",
            "; $ghost 只是注释里的名字",
            "$a = 1",
        ])

        report = validator.analyze_ini_text(text)

        self.assertNotIn("$ghost", report["undeclared"])

    def test_duplicate_global_declarations_are_reported(self):
        text = "\n".join([
            "[Constants]",
            "global $ssmtdrag_ui_zone_A = -1",
            "[Constants]",
            "global $ssmtdrag_ui_zone_A = -1",
            "global $other = 0",
        ])

        report = validator.analyze_ini_text(text)

        self.assertEqual(report["duplicate_globals"], {"$ssmtdrag_ui_zone_A": 2})
        self.assertNotIn("$ssmtdrag_ui_zone_A", report["undeclared"])

    def test_forward_reference_to_later_declaration_is_not_undeclared(self):
        """声明在引用之后（跨段全局）不算未声明——3DMigoto 加载期统一登记。"""
        text = "\n".join([
            "[Present]",
            "$later = 1",
            "[Constants]",
            "global $later = 0",
        ])

        report = validator.analyze_ini_text(text)

        self.assertEqual(report["undeclared"], {})


class FormatFindingsTests(unittest.TestCase):
    def test_reports_undeclared_and_duplicates(self):
        report = validator.analyze_ini_text("\n".join([
            "[Constants]",
            "global $dup = 0",
            "global $dup = 0",
            "[Present]",
            "$missing = 1",
        ]))

        lines = "\n".join(validator.format_findings(report))

        self.assertIn("$missing", lines)
        self.assertIn("未声明", lines)
        self.assertIn("$dup", lines)
        self.assertIn("重复声明", lines)

    def test_clean_report_reports_pass(self):
        report = validator.analyze_ini_text("\n".join([
            "[Constants]",
            "global $a = 0",
            "[Present]",
            "$a = 1",
        ]))

        lines = validator.format_findings(report)

        self.assertEqual(len(lines), 1)
        self.assertIn("自检通过", lines[0])

    def test_missing_report_is_silent(self):
        self.assertEqual(validator.format_findings({"missing": True}), [])


class FindPrimaryIniTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="ini_validator_test_")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.dir, ignore_errors=True)

    def _write(self, name, content="[Constants]\nglobal $a = 0\n"):
        path = os.path.join(self.dir, name)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(content)
        return path

    def test_prefers_workspace_named_ini(self):
        self._write("Other.ini")
        expected = self._write("MyChar.ini")

        found = validator.find_primary_ini(self.dir, "MyChar")

        self.assertEqual(found, expected)

    def test_falls_back_to_largest_candidate(self):
        small = self._write("Small.ini", "[Constants]\nglobal $a = 0\n")
        large = self._write("Big.ini", "[Constants]\nglobal $a = 0\n" + "$a = 1\n" * 50)

        found = validator.find_primary_ini(self.dir, "")

        self.assertEqual(found, large)
        self.assertNotEqual(found, small)

    def test_ignores_backup_files(self):
        self._write("MyChar.ini.bak")
        expected = self._write("MyChar.ini")

        self.assertEqual(validator.find_primary_ini(self.dir, "MyChar"), expected)

    def test_validate_ini_file_round_trip(self):
        path = self._write("A.ini", "[Present]\n$ghost = 1\n")

        report = validator.validate_ini_file(path)

        self.assertFalse(report["missing"])
        self.assertIn("$ghost", report["undeclared"])

    def test_validate_missing_file(self):
        report = validator.validate_ini_file(os.path.join(self.dir, "nope.ini"))
        self.assertTrue(report["missing"])


if __name__ == "__main__":
    unittest.main()
