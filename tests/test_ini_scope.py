# -*- coding: utf-8 -*-
"""``blueprint/ini_scope.py``（INI 代码范围圈定）纯函数测试。

不需要 fake bpy：段切分、if/endif 配对、行分类、绘制单元边界（嵌套材质切换 if /
``;MARK:`` / 多 if 块）、条件链、IB 键交叉校验。依赖 bpy 的那一层（材质转资源pro
的定位口径）随调用方实现一起放在各自的测试文件里。
"""
import importlib.util
import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# 先按文件路径装载纯函数模块（不经过包结构、不 import bpy）。
INI_SCOPE_SPEC = importlib.util.spec_from_file_location(
    "ini_scope_under_test", REPO_ROOT / "blueprint" / "ini_scope.py"
)
ini_scope = importlib.util.module_from_spec(INI_SCOPE_SPEC)
INI_SCOPE_SPEC.loader.exec_module(ini_scope)


# --------------------------------------------------------------------------- #
# 解析纯函数
# --------------------------------------------------------------------------- #

class IniScopeParsingTests(unittest.TestCase):
    def test_find_sections_gives_header_and_body_ranges(self):
        lines = [
            "; 前导注释不属于任何段",
            "[SectionA]",
            "hash = 11111111",
            "[SectionB]",
            "drawindexed = 1,0,0",
        ]
        self.assertEqual(
            ini_scope.find_sections(lines),
            [
                ("SectionA", 1, 2, 3),
                ("SectionB", 3, 4, 5),
            ],
        )

    def test_find_sections_without_headers(self):
        self.assertEqual(ini_scope.find_sections(["; x", "a = 1"]), [])
        self.assertEqual(ini_scope.find_sections([]), [])

    def test_find_block_end_pairs_nested_ifs(self):
        lines = ["if $a == 1", "if $b == 2", "drawindexed = 1", "endif", "endif", "x"]
        self.assertEqual(ini_scope.find_block_end(lines, 0), 4)
        self.assertEqual(ini_scope.find_block_end(lines, 1), 3)
        # 起点不是 if / 越界 / 没闭合 -> None（保守）
        self.assertIsNone(ini_scope.find_block_end(lines, 5))
        self.assertIsNone(ini_scope.find_block_end(lines, -1))
        self.assertIsNone(ini_scope.find_block_end(["if $a", "if $b", "endif"], 0))

    def test_find_if_blocks_returns_top_level_blocks(self):
        lines = ["x", "if $a", "endif", "if $b", "if $c", "endif", "endif", "y"]
        self.assertEqual(ini_scope.find_if_blocks(lines), [(1, 2), (3, 6)])

    def test_line_depths_marks_endif_with_closing_block_depth(self):
        lines = ["if $a", "x", "endif"]
        self.assertEqual(ini_scope.line_depths(lines), [0, 1, 1])

    def test_classify_line_categories(self):
        cases = {
            "": "neutral",
            "   ": "neutral",
            "; 普通注释": "comment",
            ";MARK: tail": "mark",
            ";mark": "mark",
            "if $a == 1": "if",
            "endif": "endif",
            "drawindexed = 1,0,0": "draw",
            "drawindexedinstanced = 1,0,0": "draw",
            "draw = 3": "draw",
            "ps-t0 = Resource_Color": "texture_ref",
            r"Resource\ZZMI\Diffuse = ref Resource-Diffuse": "texture_ref",
            r"Resource\ZZMI\Diffuse = null": "texture_ref",
            r"run = CommandList\ZZMI\SetTextures": "run",
            "ib = Resource_IB": "bind",
            "vb0 = Resource_VB": "bind",
            "vs-t0 = Resource_VS": "bind",
            "cs-t0 = Resource_CS": "bind",
            "else": "other",
            "else if $a": "other",
            "hash = 6ef3d666": "other",
            "unknown command": "other",
        }
        for text, expected in cases.items():
            self.assertEqual(ini_scope.classify_line(text), expected, text)
        self.assertTrue(ini_scope.is_else_line("else if $a"))
        self.assertTrue(ini_scope.is_else_line("elif $a"))
        self.assertFalse(ini_scope.is_else_line("elsewhere"))

    def test_iter_draw_units_basic_boundaries(self):
        lines = [
            "; [mesh:A] [vertex_count:3]",
            "ps-t0 = Resource_A",
            "drawindexed = 1,0,0",
            "; [mesh:B]",
            "drawindexed = 2,0,0",
        ]
        units = list(ini_scope.iter_draw_units(lines))
        self.assertEqual([unit["mesh"] for unit in units], ["A", "B"])
        self.assertEqual((units[0]["start"], units[0]["end"]), (0, 3))
        self.assertEqual(units[0]["material_lines"], [1])
        self.assertEqual(units[0]["draw_lines"], [2])
        self.assertEqual((units[1]["start"], units[1]["end"]), (3, 5))
        self.assertEqual(units[1]["material_lines"], [])
        self.assertIsNone(units[0]["is_target"])
        self.assertEqual(units[0]["chain"], ())

    def test_iter_draw_units_keeps_nested_material_switch_if_inside_unit(self):
        """单元内嵌套的材质切换 if 整体属于该单元，不被拆散。"""
        lines = [
            "; [mesh:A]",
            "if $material_switch == 1",
            "; 材质切换",
            "ps-t0 = Resource_A",
            "drawindexed = 1,0,0",
            "endif",
            "; [mesh:B]",
            "drawindexed = 2,0,0",
        ]
        units = list(ini_scope.iter_draw_units(lines))
        self.assertEqual((units[0]["start"], units[0]["end"]), (0, 6))
        self.assertEqual(units[0]["material_lines"], [3])
        self.assertEqual(units[0]["draw_lines"], [4])
        self.assertEqual(units[0]["chain"], ())
        self.assertEqual((units[1]["start"], units[1]["end"]), (6, 8))

    def test_iter_draw_units_stops_before_mark(self):
        lines = [
            "; [mesh:A]",
            "drawindexed = 1,0,0",
            ";MARK: tail",
            "; [mesh:B]",
            "drawindexed = 2,0,0",
        ]
        units = list(ini_scope.iter_draw_units(lines))
        self.assertEqual((units[0]["start"], units[0]["end"]), (0, 2))
        self.assertEqual((units[1]["start"], units[1]["end"]), (3, 5))

    def test_iter_draw_units_stops_at_if_after_same_level_draw(self):
        """目标 draw 之后新开的同层 if 属于下一段代码范围（保留既有置顶语义）。"""
        lines = [
            "; [mesh:A]",
            "drawindexed = 1,0,0",
            "if $late == 1",
            "; [mesh:B]",
            "drawindexed = 2,0,0",
            "endif",
        ]
        units = list(ini_scope.iter_draw_units(lines))
        by_name = {unit["mesh"]: unit for unit in units}
        self.assertEqual((by_name["A"]["start"], by_name["A"]["end"]), (0, 2))
        self.assertEqual(by_name["B"]["chain"][0][:2], (2, 5))

    def test_iter_draw_units_reports_condition_chain_and_ends_before_endif(self):
        lines = [
            "if $switch == 1",
            "; [mesh:A]",
            "drawindexed = 1,0,0",
            "endif",
            "; [mesh:B]",
            "drawindexed = 2,0,0",
        ]
        units = list(ini_scope.iter_draw_units(lines))
        unit_a = units[0]
        self.assertEqual((unit_a["start"], unit_a["end"]), (1, 3))
        self.assertEqual(
            unit_a["chain"],
            ((0, 3, "if $switch == 1"),),
        )

    def test_iter_draw_units_is_target_callback(self):
        lines = ["; [mesh:A]", "; [mesh:B]", "drawindexed = 1"]
        units = list(
            ini_scope.iter_draw_units(
                lines,
                is_target=lambda unit: unit["mesh"] == "B",
            )
        )
        self.assertEqual([unit["is_target"] for unit in units], [False, True])

    def test_section_mesh_names_is_ordered_and_unique(self):
        lines = [
            "; [mesh:A] [vertex_count:3]",
            "; [mesh:B]",
            "if $x",
            "; [mesh:A]",
            "endif",
        ]
        self.assertEqual(ini_scope.section_mesh_names(lines), ["A", "B"])
        self.assertEqual(
            ini_scope.section_mesh_names(lines, unique=False),
            ["A", "B", "A"],
        )

    def test_mesh_ib_key(self):
        self.assertEqual(
            ini_scope.mesh_ib_key("LOD0.6EF3D666-888-0.武器翅膀花纹贴片"),
            {"hex": "6ef3d666", "full": "6ef3d666-888-0"},
        )
        self.assertEqual(
            ini_scope.mesh_ib_key("LOD0.6ef3d666"),
            {"hex": "6ef3d666", "full": None},
        )
        self.assertIsNone(ini_scope.mesh_ib_key("Body_copy"))

    def test_section_ib_key_prefers_section_name_full_key(self):
        self.assertEqual(
            ini_scope.section_ib_key("[TextureOverride_LOD0.c209c22b_45087_0]", []),
            {"hex": "c209c22b", "full": "c209c22b-45087-0"},
        )
        # 段名没有完整键时用段体 hash 行的完整键
        self.assertEqual(
            ini_scope.section_ib_key(
                "[TextureOverride_X]", ["hash = 6ef3d666-888-0"]
            ),
            {"hex": "6ef3d666", "full": "6ef3d666-888-0"},
        )
        # 只有 8 位 hash 且段名像 IB 段时采用
        self.assertEqual(
            ini_scope.section_ib_key("[TextureOverride_IB_6ef3d666]", ["hash = 6ef3d666"]),
            {"hex": "6ef3d666", "full": None},
        )

    def test_section_ib_key_ignores_resource_section_hash(self):
        """VB 段的 ``hash`` 是缓冲 hash，不能当成 IB hash 做交叉校验。"""
        self.assertIsNone(
            ini_scope.section_ib_key(
                "[TextureOverride_VB_bcc7e369_bcc7e369_Position]",
                ["hash = 80f2a2aa"],
            )
        )
        self.assertIsNone(ini_scope.section_ib_key("[TextureOverride_Plain]", []))

    def test_ib_key_matches(self):
        full_a = ini_scope.mesh_ib_key("LOD0.aaa11111-100-0")
        full_a2 = ini_scope.mesh_ib_key("LOD0.aaa11111-100-0.part")
        full_b = ini_scope.mesh_ib_key("LOD0.bbb22222-200-0")
        self.assertTrue(ini_scope.ib_key_matches(full_a, full_a2))
        self.assertFalse(ini_scope.ib_key_matches(full_a, full_b))
        # 任一侧取不到 -> 不做交叉校验
        self.assertTrue(ini_scope.ib_key_matches(None, full_b))
        self.assertTrue(ini_scope.ib_key_matches(full_a, None))
        # 只有 hex 时退化成 8 位前缀比较
        hex_only = {"hex": "aaa11111", "full": None}
        self.assertTrue(ini_scope.ib_key_matches(hex_only, full_a))
        self.assertFalse(ini_scope.ib_key_matches(hex_only, full_b))
