"""PR#45 §B1/§B2：导出段登记表的增量 upsert 语义与容错回归用例。

覆盖本次修正的三个阻塞性质：

1. 同一 ini 连续两轮 ``commit_ini`` 不丢前一轮的段（原本是整批覆盖语义，
   一个进程多轮 ``execute_round`` 会让后写吞先写，EFMI / Unity 系直接丢段）；
2. 畸形段（None / 非字符串 / 长度不足 / 非序列）只被跳过，绝不抛异常
   （原本 ``M_IniBuilder._collect_section_entries`` 会在 ``re.match`` 处抛 TypeError）；
3. 登记是导出链路的附加品：``save_to_file`` 里登记失败必须静默，不能反过来让导出失败。
"""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import common.export_section_registry as registry
import common.m_ini_builder as m_ini_builder
from common.m_ini_builder import M_IniBuilder, M_IniSection, M_SectionType


class _BrokenSection:
    """与 M_IniSection 同 API、但字段形态畸形的假段（测试桩）。"""

    def __init__(self):
        self.SectionName = None
        self.SectionLineList = [None, 123, b"[Bytes_Line]", "[Real_Section]", 4.5]
        self.SectionType = None
        self.owner_draw_ib = None


class ExportSectionRegistryCommitTests(unittest.TestCase):
    def setUp(self):
        registry.clear()

    def tearDown(self):
        registry.clear()

    def test_commit_ini_keeps_previous_round_sections_in_same_ini(self):
        ini = os.path.join(tempfile.gettempdir(), "herta45_rounds", "mod.ini")
        registry.commit_ini(
            ini, [("TextureOverride_Body", M_SectionType.TextureOverrideIB, "100")]
        )
        registry.commit_ini(
            ini, [("TextureOverride_Hair", M_SectionType.TextureOverrideIB, "200")]
        )

        tables = registry.get_all()
        self.assertEqual(len(tables), 1)
        entries = list(tables.values())[0]
        self.assertEqual(
            [entry["name"] for entry in entries],
            ["TextureOverride_Body", "TextureOverride_Hair"],
        )
        self.assertEqual([entry["index"] for entry in entries], [0, 1])

    def test_commit_ini_is_idempotent_for_repeated_rounds(self):
        ini = os.path.join(tempfile.gettempdir(), "herta45_rounds", "mod.ini")
        sections = [("Resource_Texture_abc", M_SectionType.ResourceTexture, "100")]
        registry.commit_ini(ini, sections)
        registry.commit_ini(ini, sections)
        registry.commit_ini(ini, sections)

        entries = list(registry.get_all().values())[0]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["type"], M_SectionType.ResourceTexture)

    def test_commit_ini_does_not_create_table_for_empty_payload(self):
        ini = os.path.join(tempfile.gettempdir(), "herta45_rounds", "mod.ini")
        registry.commit_ini(ini, [("Only_Section", M_SectionType.Present, "100")])
        registry.commit_ini(ini, [])
        registry.commit_ini(ini, None)
        registry.commit_ini(ini, "not-a-sequence")
        registry.commit_ini("", [("Ghost", M_SectionType.Present, "100")])

        self.assertEqual(len(list(registry.get_all().values())[0]), 1)
        self.assertEqual(registry.get_ini_paths(), [os.path.normpath(os.path.abspath(ini))])

    def test_malformed_sections_are_skipped_without_raising(self):
        ini = os.path.join(tempfile.gettempdir(), "herta45_rounds", "mod.ini")
        malformed = [
            None,
            "",
            0,
            123,
            "not-a-tuple",
            b"bytes-item",
            (),
            ("only_name",),
            ["only_name"],
            object(),
            (None, None, None),
        ]
        registry.commit_ini(ini, malformed)
        self.assertEqual(registry.get_all(), {})

        registry.commit_ini(
            ini, malformed + [("Good_Section", M_SectionType.ResourceBuffer, "300")]
        )
        entries = registry.get_sections_for_draw_ib("300")
        self.assertEqual([entry["name"] for entry in entries], ["Good_Section"])


class M_IniBuilderRegistrationTests(unittest.TestCase):
    def setUp(self):
        registry.clear()

    def tearDown(self):
        registry.clear()

    def test_collect_section_entries_skips_malformed_fields(self):
        builder = M_IniBuilder()
        good = M_IniSection(M_SectionType.TextureOverrideIB)
        good.SectionName = "TextureOverride_100"
        good.append("hash = 100")
        builder.append_section(good)
        builder.ini_section_list.insert(0, _BrokenSection())

        entries = builder._collect_section_entries()

        names = [entry[0] for entry in entries]
        self.assertIn("TextureOverride_100", names)
        self.assertIn("Real_Section", names)
        self.assertNotIn("Bytes_Line", names)
        self.assertTrue(all(isinstance(name, str) and name for name in names))

    def test_save_to_file_registers_sections_with_draw_ib(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ini_path = Path(temp_dir) / "mod.ini"
            builder = M_IniBuilder()
            with registry.draw_ib_context("100"):
                section = M_IniSection(M_SectionType.TextureOverrideIB)
                section.SectionName = "TextureOverride_100"
                section.append("hash = 100")
                builder.append_section(section)
            builder.save_to_file(str(ini_path))

            entries = registry.get_sections_for_draw_ib("100", str(ini_path))
            self.assertEqual(
                [entry["name"] for entry in entries], ["TextureOverride_100"]
            )
            self.assertTrue(ini_path.is_file())

    def test_registration_failure_never_breaks_save(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ini_path = Path(temp_dir) / "mod.ini"
            builder = M_IniBuilder()
            section = M_IniSection(M_SectionType.Constants)
            section.SectionName = "Constants"
            section.append("global $always = 1")
            builder.append_section(section)

            with mock.patch.object(
                m_ini_builder._section_registry,
                "commit_ini",
                side_effect=RuntimeError("registry exploded"),
            ):
                builder.save_to_file(str(ini_path))

            self.assertIn("[Constants]", ini_path.read_text(encoding="utf-8"))
