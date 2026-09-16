# -*- coding: utf-8 -*-
"""原神 GIMI ORFix 调用位置适配（blueprint/gimi_orfix_adapter.py）的行为测试。

该适配器是纯标准库模块，不依赖 bpy，可直接按文件路径加载。
"""
import importlib.util
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
_ADAPTER_PATH = REPO_ROOT / "blueprint" / "gimi_orfix_adapter.py"

_spec = importlib.util.spec_from_file_location("_gimi_orfix_adapter_under_test", _ADAPTER_PATH)
adapter = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(adapter)

FIX = r"run = CommandList\global\ORFix\ORFix"
NNFIX = r"run = CommandList\global\ORFix\NNFix"


def _indexes(lines, needle):
    return [i for i, line in enumerate(lines) if str(line).strip() == needle]


class UsesOrfixTests(unittest.TestCase):
    def test_detects_orfix_and_nnfix(self):
        self.assertTrue(adapter.uses_orfix(["ps-t0 = R0", FIX]))
        self.assertTrue(adapter.uses_orfix(["ps-t0 = R0", NNFIX]))

    def test_ignores_non_gimi_sections(self):
        self.assertFalse(adapter.uses_orfix([]))
        self.assertFalse(adapter.uses_orfix(None))
        self.assertFalse(adapter.uses_orfix([
            "[TextureOverride_x]", "ps-t0 = R0", "drawindexed = 1,0,0",
        ]))

    def test_tolerates_crlf_and_whitespace(self):
        self.assertTrue(adapter.uses_orfix([FIX + "\r"]))
        self.assertTrue(adapter.uses_orfix(["run=CommandList\\global\\ORFix\\ORFix"]))

    def test_indented_run_is_not_section_level(self):
        """缩进的 run 属于分支内部，不属于段级 ORFix 写法（不得被重排/删除）。"""
        self.assertFalse(adapter.uses_orfix(["  " + FIX]))
        indented = [
            "[TextureOverride_x]", "ps-t0 = R0", "  " + FIX, "drawindexed = 1,0,0",
        ]
        self.assertEqual(adapter.place_orfix_runs(indented), indented)


class PlaceOrfixRunsTests(unittest.TestCase):
    def test_first_draw_inside_if_places_fix_before_if(self):
        """核心回归：首个绘制在 if 分支内时，ORFix 必须落在 if 之前。

        否则条件不成立时 ORFix 整段不执行（比原来的段首无条件执行更糟）。
        """
        lines = [
            "[TextureOverride_x]",
            "ps-t0 = ResourceDiffuse",
            FIX,
            "if $x == 1",
            "  ; [mesh:A] [vertex_count:3]",
            "  drawindexed = 100,0,0",
            "else",
            "  ; [mesh:B] [vertex_count:3]",
            "  drawindexed = 200,0,0",
            "endif",
        ]
        out = adapter.place_orfix_runs(lines)

        self.assertLess(out.index(FIX), out.index("if $x == 1"))
        self.assertNotIn("  " + FIX, out, "ORFix 不得缩进进分支")
        self.assertEqual(len(out), len(lines))
        self.assertEqual(out.count(FIX), 1)

    def test_all_draws_indented_outside_top_level(self):
        lines = [
            "[TextureOverride_x]", "ps-t0 = R0", FIX,
            "if $x == 1", "  ; [mesh:A]", "  drawindexed = 1,0,0", "endif",
        ]
        out = adapter.place_orfix_runs(lines)
        self.assertLess(out.index(FIX), out.index("if $x == 1"))
        self.assertEqual(out.count(FIX), 1)

    def test_nested_blocks_keep_single_top_level_fix(self):
        lines = [
            "[TextureOverride_x]", "ps-t0 = R0", FIX,
            "if $x == 1", "  if $y == 2", "    drawindexed = 1,0,0",
            "  endif", "endif",
        ]
        out = adapter.place_orfix_runs(lines)
        self.assertLess(out.index(FIX), out.index("if $x == 1"))
        self.assertEqual(out.count(FIX), 1)

    def test_texture_change_inside_branch_refixes_in_place(self):
        lines = [
            "[TextureOverride_x]", "ps-t0 = R0", FIX,
            "if $x == 1", "  ps-t0 = R1", "  drawindexed = 1,0,0",
            "else", "  ps-t0 = R2", "  drawindexed = 2,0,0", "endif",
        ]
        out = adapter.place_orfix_runs(lines)
        self.assertEqual(out, [
            "[TextureOverride_x]",
            "ps-t0 = R0",
            FIX,
            "if $x == 1",
            "  ps-t0 = R1",
            "  " + FIX,
            "  drawindexed = 1,0,0",
            "else",
            "  ps-t0 = R2",
            "  " + FIX,
            "  drawindexed = 2,0,0",
            "endif",
        ])

    def test_same_binding_is_not_reapplied_per_draw(self):
        """“每次贴图输入变化后只执行一次”：同绑定连续绘制不再重复执行。"""
        lines = [
            "[TextureOverride_x]", "ps-t0 = R0", FIX,
            "drawindexed = 1,0,0", "drawindexed = 2,0,0",
        ]
        out = adapter.place_orfix_runs(lines)
        self.assertEqual(out.count(FIX), 1)

    def test_binding_change_before_second_draw_refixes(self):
        lines = [
            "[TextureOverride_x]", "ps-t0 = R0", FIX,
            "drawindexed = 1,0,0", "ps-t0 = R1", "drawindexed = 2,0,0",
        ]
        out = adapter.place_orfix_runs(lines)
        fix_idxs = _indexes(out, FIX)
        draw_idxs = [i for i, line in enumerate(out) if str(line).startswith("drawindexed")]
        self.assertEqual(len(fix_idxs), 2)
        self.assertLess(fix_idxs[0], draw_idxs[0])
        self.assertLess(out.index("ps-t0 = R1"), fix_idxs[1])
        self.assertLess(fix_idxs[1], draw_idxs[1])

    def test_trailing_restore_block_keeps_orfix_at_tail(self):
        """材质转资源在段尾补的恢复块（ps-t 重绑 + ORFix）必须保留段尾 ORFix。"""
        lines = [
            "[TextureOverride_x]", "hash = 1", "ps-t0 = Custom", FIX,
            "drawindexed = 1,0,0",
            ";MARK:RESTORE_DEFAULT_START", "ps-t0 = Original", FIX,
            ";MARK:RESTORE_DEFAULT_END",
        ]
        out = adapter.place_orfix_runs(lines)
        fix_idxs = _indexes(out, FIX)
        self.assertEqual(len(fix_idxs), 2)
        self.assertLess(fix_idxs[0], out.index("drawindexed = 1,0,0"))
        self.assertGreater(fix_idxs[1], out.index(";MARK:RESTORE_DEFAULT_START"))

    def test_non_orfix_section_is_returned_byte_identical(self):
        lines = [
            "[TextureOverride_x]", "ps-t0 = R0", "drawindexed = 1,0,0",
            "if $x == 1", "  drawindexed = 2,0,0", "endif",
        ]
        self.assertEqual(adapter.place_orfix_runs(lines), lines)

    def test_mixed_fix_kinds_are_returned_unchanged(self):
        lines = [
            "[TextureOverride_x]", "ps-t0 = R0", FIX, "drawindexed = 1,0,0",
            "ps-t0 = R1", NNFIX, "drawindexed = 2,0,0",
        ]
        self.assertEqual(adapter.place_orfix_runs(lines), lines)

    def test_section_without_draw_is_returned_unchanged(self):
        lines = ["[TextureOverride_x]", "ps-t0 = R0", FIX]
        self.assertEqual(adapter.place_orfix_runs(lines), lines)

    def test_empty_and_none_input(self):
        self.assertEqual(adapter.place_orfix_runs(None), [])
        self.assertEqual(adapter.place_orfix_runs([]), [])

    def test_does_not_mutate_input(self):
        lines = ["[TextureOverride_x]", "ps-t0 = R0", FIX, "drawindexed = 1,0,0"]
        snapshot = list(lines)
        adapter.place_orfix_runs(lines)
        self.assertEqual(lines, snapshot)

    def test_placed_fix_has_no_trailing_newline_or_cr(self):
        lines = [
            "[TextureOverride_x]", "ps-t0 = R0", FIX, "drawindexed = 1,0,0",
        ]
        out = adapter.place_orfix_runs(lines)
        self.assertIn(FIX, out)
        self.assertFalse(any("\n" in line for line in out))

    def test_variable_names_are_not_mistaken_for_control_flow(self):
        """`iffy = 1` / `endif_x = 1` 这类变量名不能被当成控制流关键字。"""
        self.assertTrue(adapter.CTRL_OPEN_RE.match("if $x == 1"))
        self.assertTrue(adapter.CTRL_OPEN_RE.match("if($x)"))
        self.assertTrue(adapter.CTRL_OPEN_RE.match("while $x"))
        self.assertTrue(adapter.CTRL_CLOSE_RE.match("endif"))
        self.assertTrue(adapter.CTRL_CLOSE_RE.match("endif ; done"))
        self.assertTrue(adapter.CTRL_BRANCH_RE.match("elseif $x == 2"))

        for variable_line in ("iffy = 1", "while_x = 1", "endif_x = 1", "elsewhere = 1"):
            self.assertFalse(
                adapter.CTRL_OPEN_RE.match(variable_line)
                or adapter.CTRL_CLOSE_RE.match(variable_line)
                or adapter.CTRL_BRANCH_RE.match(variable_line),
                variable_line,
            )

    def test_variable_named_like_keyword_does_not_break_placement(self):
        lines = [
            "[TextureOverride_x]", "ps-t0 = R0", FIX,
            "iffy = 1", "endif_x = 2", "drawindexed = 1,0,0",
        ]
        out = adapter.place_orfix_runs(lines)
        # 伪关键字不应开/闭块，段首 ORFix 仍落在第一条顶格绘制之前
        self.assertLess(out.index(FIX), out.index("drawindexed = 1,0,0"))
        self.assertEqual(out.count(FIX), 1)


if __name__ == "__main__":
    unittest.main()
