# -*- coding: utf-8 -*-
"""config_table_backup 备份/恢复逻辑测试。

覆盖 Generate Mod 弹窗「配置表覆盖」选择背后的文件操作：
- find_config_table_files 只识别导出目录根目录的 *.ini（配置表）；
- backup_config_tables 将配置表改名（.bak 后缀）并移动至备份目录；
- restore_config_tables 在导出完成后把旧配置表覆盖回原位。
"""

import importlib.util
import sys
import tempfile
import types
import unittest
from pathlib import Path


PKG = "TheHerta4"
ROOT = Path(__file__).resolve().parents[1]

if PKG not in sys.modules:
    _root_stub = types.ModuleType(PKG)
    _root_stub.__path__ = []
    sys.modules[PKG] = _root_stub

for _pkg_name in (f"{PKG}.common", f"{PKG}.utils"):
    if _pkg_name not in sys.modules:
        _pkg_stub = types.ModuleType(_pkg_name)
        _pkg_stub.__path__ = []
        sys.modules[_pkg_name] = _pkg_stub

_log_stub = types.SimpleNamespace(
    info=lambda *args, **kwargs: None,
    warning=lambda *args, **kwargs: None,
    error=lambda *args, **kwargs: None,
    debug=lambda *args, **kwargs: None,
)
_log_module = types.ModuleType(f"{PKG}.utils.log_utils")
_log_module.LOG = _log_stub
sys.modules[f"{PKG}.utils.log_utils"] = _log_module

_MODULE_PATH = ROOT / "common" / "config_table_backup.py"
_SPEC = importlib.util.spec_from_file_location(
    f"{PKG}.common.config_table_backup", _MODULE_PATH
)
tool = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = tool
_SPEC.loader.exec_module(tool)


class FindConfigTableFilesTests(unittest.TestCase):
    def test_only_root_level_ini_files_are_config_tables(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            export_dir = Path(temp_dir)
            (export_dir / "角色.ini").write_text("[main]", encoding="utf-8")
            (export_dir / "API.ini").write_text("[api]", encoding="utf-8")
            (export_dir / "readme.txt").write_text("note", encoding="utf-8")
            (export_dir / "old.ini.bak").write_text("old", encoding="utf-8")
            (export_dir / "Meshes").mkdir()
            (export_dir / "Meshes" / "sub.ini").write_text("[sub]", encoding="utf-8")
            backup_dir = export_dir / ".config_table_backup" / "20260101_000000_000000"
            backup_dir.mkdir(parents=True)
            (backup_dir / "角色.ini.bak").write_text("old", encoding="utf-8")

            found = tool.find_config_table_files(str(export_dir))

            self.assertEqual(
                [Path(path).name for path in found], ["API.ini", "角色.ini"]
            )

    def test_missing_or_invalid_dir_returns_empty(self):
        self.assertEqual(tool.find_config_table_files(""), [])
        self.assertEqual(tool.find_config_table_files("Z:/definitely/not/exists"), [])
        with tempfile.TemporaryDirectory() as temp_dir:
            self.assertEqual(tool.find_config_table_files(temp_dir), [])


class BackupConfigTablesTests(unittest.TestCase):
    def test_backup_moves_config_tables_and_renames_with_bak_suffix(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            export_dir = Path(temp_dir)
            ini_path = export_dir / "角色.ini"
            ini_path.write_text("[old]", encoding="utf-8")
            txt_path = export_dir / "keep.txt"
            txt_path.write_text("keep", encoding="utf-8")

            entries = tool.backup_config_tables(
                str(export_dir), tool.find_config_table_files(str(export_dir))
            )

            self.assertEqual(len(entries), 1)
            original, backup = entries[0]
            self.assertEqual(Path(original), ini_path)
            self.assertFalse(ini_path.exists(), "配置表应被移走，不再位于导出目录")
            self.assertTrue(txt_path.exists(), "非配置表文件不应被移动")
            backup_path = Path(backup)
            self.assertTrue(backup_path.is_file())
            self.assertEqual(backup_path.suffix, ".bak")
            self.assertTrue(
                backup_path.parent.parent.name == tool.BACKUP_DIR_NAME,
                "备份应位于导出目录内的备份目录中",
            )
            self.assertEqual(backup_path.read_text(encoding="utf-8"), "[old]")

    def test_backup_skips_missing_files(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            entries = tool.backup_config_tables(
                temp_dir, [str(Path(temp_dir) / "不存在.ini")]
            )
            self.assertEqual(entries, [])

    def test_backup_with_empty_list_is_noop(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            self.assertEqual(tool.backup_config_tables(temp_dir, []), [])
            self.assertEqual(tool.backup_config_tables(temp_dir, None), [])


class RestoreConfigTablesTests(unittest.TestCase):
    def test_restore_overwrites_new_config_and_keeps_backup(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            export_dir = Path(temp_dir)
            ini_path = export_dir / "角色.ini"
            ini_path.write_text("[old]", encoding="utf-8")

            entries = tool.backup_config_tables(
                str(export_dir), tool.find_config_table_files(str(export_dir))
            )
            self.assertEqual(len(entries), 1)
            original, backup = entries[0]

            # 模拟导出生成了新的配置表
            ini_path.write_text("[new]", encoding="utf-8")

            tool.restore_config_tables(entries)

            self.assertTrue(ini_path.is_file())
            self.assertEqual(ini_path.read_text(encoding="utf-8"), "[old]")
            self.assertTrue(Path(backup).is_file(), "备份副本应保留")

    def test_restore_skips_missing_backup(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            export_dir = Path(temp_dir)
            ini_path = export_dir / "角色.ini"
            ini_path.write_text("[old]", encoding="utf-8")

            entries = tool.backup_config_tables(
                str(export_dir), tool.find_config_table_files(str(export_dir))
            )
            self.assertEqual(len(entries), 1)
            original, backup = entries[0]

            Path(backup).unlink()
            tool.restore_config_tables(entries)

            self.assertFalse(Path(original).exists(), "备份缺失时不应凭空恢复文件")

    def test_restore_with_empty_list_is_noop(self):
        tool.restore_config_tables([])
        tool.restore_config_tables(None)


if __name__ == "__main__":
    unittest.main()
