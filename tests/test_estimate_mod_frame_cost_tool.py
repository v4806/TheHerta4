# -*- coding: utf-8 -*-
"""每帧命令开销估算工具（tools/estimate_mod_frame_cost.py）的回归测试。

两件事：
1. 分类口径正确 —— 用合成 ini 断言 always / drawn / armed 三类的"必然执行"计数，
   并覆盖两种 `run =` 写法（`CommandList\\X` 与拼接式 `CommandListX`）与重复段块；
2. **真实产物必须保持在优化后的水平** —— 角色不在场时每帧必然执行的命令数
   （本模组优化前 ~300，优化后 ~82）不得超过阈值，防止将来把门控改回去。
"""
import importlib.util
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
TOOL_PATH = REPO_ROOT / "tools" / "estimate_mod_frame_cost.py"
REAL_INI = Path(
    r"K:\SSMT-Package-master\3Dmigoto\ZZZ\Mods\SSMTGeneratedMod\克拉蕾\克拉蕾.ini"
)
#: 角色不在场时每帧"必然执行"的命令数上限（优化后实测 82）
OFFSCREEN_BUDGET = 100

_spec = importlib.util.spec_from_file_location("_estimate_mod_frame_cost", TOOL_PATH)
cost_tool = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cost_tool)


def _totals(ini_text):
    with tempfile.TemporaryDirectory() as temp_dir:
        ini_path = Path(temp_dir) / "synthetic.ini"
        ini_path.write_text(ini_text, encoding="utf-8")
        lines = ini_path.read_text(encoding="utf-8").splitlines()
    blocks, by_name = cost_tool.parse_blocks(lines)
    return cost_tool.walk(lines, blocks, by_name)


class ClassificationTests(unittest.TestCase):
    def test_three_gate_classes_and_run_forms(self):
        totals = _totals(
            "[Present]\n"
            "$always = 1\n"
            "if $active0 == 1\n"
            "    $drawn = 1\n"
            "    run = CommandListDrawnHelper\n"
            "endif\n"
            "if $ssmtdrag_drag_enabled_A >= 1 && $inputMode == 0 && $ssmtdrag_mode_A == 1\n"
            "    $armed = 1\n"
            "    run = CommandList\\ArmedHelper\n"
            "endif\n"
            "[CommandListDrawnHelper]\n"
            "$drawn_helper = 1\n"
            "[CommandList\\ArmedHelper]\n"
            "$armed_helper = 1\n"
            "$armed_helper2 = 1\n"
        )

        # always：1 条语句 + 2 条门控条件行（进入场景即求值）
        self.assertEqual(totals[("always", True)], 3)
        # drawn：门控内 1 条语句 + 1 条 run 行本身 + 被调用 CL 的 1 条
        self.assertEqual(totals[("drawn", True)], 3)
        # armed：门控内 1 条语句 + 1 条 run 行本身 + 被调用 CL 的 2 条
        self.assertEqual(totals[("armed", True)], 4)

    def test_duplicate_section_blocks_are_all_counted(self):
        totals = _totals(
            "[Present]\n$always_a = 1\n"
            "[Present]\n$always_b = 1\n"
        )

        self.assertEqual(totals[("always", True)], 2)

    def test_branch_bodies_are_upper_bound_only(self):
        totals = _totals(
            "[Present]\n"
            "if $layout_sig != 1\n"
            "    $rare = 1\n"
            "endif\n"
        )

        # 条件行本身必然求值；分支体只进上界
        self.assertEqual(totals[("always", True)], 1)
        self.assertEqual(totals[("always", False)], 1)


class RealModBudgetTests(unittest.TestCase):
    def test_offscreen_cost_within_budget(self):
        if not REAL_INI.is_file():
            self.skipTest(f"本机没有该模组产物: {REAL_INI}")

        lines = REAL_INI.read_text(encoding="utf-8-sig").splitlines()
        blocks, by_name = cost_tool.parse_blocks(lines)
        totals = cost_tool.walk(lines, blocks, by_name)

        offscreen = totals[("always", True)]
        self.assertGreater(offscreen, 0, "统计结果不该是 0，检查解析")
        self.assertLessEqual(
            offscreen, OFFSCREEN_BUDGET,
            f"角色不在场每帧必然执行 {offscreen} 条，超过预算 {OFFSCREEN_BUDGET}："
            "某处门控被改回去了？",
        )

    def test_cli_runs_and_reports_scenarios(self):
        if not REAL_INI.is_file():
            self.skipTest(f"本机没有该模组产物: {REAL_INI}")

        result = subprocess.run(
            [sys.executable, str(TOOL_PATH), str(REAL_INI)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            env={**__import__("os").environ, "PYTHONIOENCODING": "utf-8"},
        )

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("角色不在场", result.stdout)
        self.assertIn("角色在场（臂动）", result.stdout)


if __name__ == "__main__":
    unittest.main()
