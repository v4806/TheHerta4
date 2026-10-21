"""HIMI 合并骨架导入/导出契约判定（``common/himi_merged_contract.py``）单测。

契约判定是纯函数（无 bpy / 无文件 IO），这里直接覆盖四个 level 与 HIMI 特有的
**变形家族门控（G1）拒绝档**。
"""

from __future__ import annotations

import importlib.util
import sys
import types
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PKG = "himi_contract_test_pkg"


def _install_package(name):
    module = types.ModuleType(name)
    module.__path__ = []
    sys.modules[name] = module
    return module


def _load_module(qualname, path):
    spec = importlib.util.spec_from_file_location(qualname, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[qualname] = module
    spec.loader.exec_module(module)
    return module


for _name in (PKG, f"{PKG}.common"):
    _install_package(_name)
_contract = _load_module(
    f"{PKG}.common.himi_merged_contract", REPO_ROOT / "common" / "himi_merged_contract.py"
)

DEFORM_GATE_REJECT_PREFIX = _contract.DEFORM_GATE_REJECT_PREFIX
NO_MERGED_DATA_NOTICE = _contract.NO_MERGED_DATA_NOTICE
evaluate_merged_skeleton_contract = _contract.evaluate_merged_skeleton_contract
format_deform_gate_rejections = _contract.format_deform_gate_rejections
format_skip_reasons = _contract.format_skip_reasons


class TestContractLevels(unittest.TestCase):
    def test_no_data_is_notice(self):
        decision = evaluate_merged_skeleton_contract(
            checkbox_enabled=True, parts_with_data=0, component_count=0
        )
        self.assertEqual(decision["level"], "notice")
        self.assertEqual(decision["message"], NO_MERGED_DATA_NOTICE)

    def test_data_but_checkbox_off_is_warning_TWO_STATE(self):
        """【t71】开关关闭 = 普通模式 ⇒ **不得中止**（旧行为是 error，用户实报被误拦）。

        两态：开关关闭 ⇒ `warning`（放行，提示普通模式的前提）；开关打开但全部被拒 ⇒ `error`（真阳性）。
        """
        off = evaluate_merged_skeleton_contract(
            checkbox_enabled=False, parts_with_data=5, component_count=0
        )
        self.assertEqual(off["level"], "warning", off)
        self.assertIn("普通模式", off["message"])
        self.assertIn("局部编号", off["hint"])
        on_rejected = evaluate_merged_skeleton_contract(
            checkbox_enabled=True,
            parts_with_data=5,
            component_count=0,
            skip_reasons={"aaaa": "缺 palette"},
        )
        self.assertEqual(on_rejected["level"], "error", "真阳性必须保留")
        self.assertNotEqual(off["level"], on_rejected["level"], "两态必须可分辨")

    def test_all_parts_rejected_is_error(self):
        decision = evaluate_merged_skeleton_contract(
            checkbox_enabled=True,
            parts_with_data=3,
            component_count=0,
            skip_reasons={"aaaa": "缺 palette"},
        )
        self.assertEqual(decision["level"], "error")
        self.assertIn("全部被导出器拒绝", decision["message"])
        self.assertIn("aaaa: 缺 palette", decision["hint"])

    def test_clean_run_is_ok(self):
        decision = evaluate_merged_skeleton_contract(
            checkbox_enabled=True, parts_with_data=3, component_count=3
        )
        self.assertEqual(decision["level"], "ok")
        self.assertEqual(decision["message"], "")

    def test_partial_skip_is_warning(self):
        decision = evaluate_merged_skeleton_contract(
            checkbox_enabled=True,
            parts_with_data=3,
            component_count=2,
            skip_reasons={"bbbb": "join 未命中"},
        )
        self.assertEqual(decision["level"], "warning")
        self.assertIn("bbbb: join 未命中", decision["hint"])


class TestDeformGateRejection(unittest.TestCase):
    def test_gate_rejection_alone_produces_warning_with_family_detail(self):
        decision = evaluate_merged_skeleton_contract(
            checkbox_enabled=True,
            parts_with_data=3,
            component_count=2,
            gate_rejected_parts={
                "aaaa": {
                    "family": "aaaa",
                    "missing": ["aaaa-0002-20-0", "aaaa-0003-30-0"],
                }
            },
        )
        self.assertEqual(decision["level"], "warning")
        self.assertIn(DEFORM_GATE_REJECT_PREFIX, decision["hint"])
        self.assertIn("家族=aaaa", decision["hint"])
        self.assertIn("aaaa-0002-20-0", decision["hint"])

    def test_gate_rejection_with_all_parts_rejected_is_error(self):
        decision = evaluate_merged_skeleton_contract(
            checkbox_enabled=True,
            parts_with_data=1,
            component_count=0,
            gate_rejected_parts={"aaaa": {"family": "aaaa", "missing": ["aaaa-0002-20-0"]}},
        )
        self.assertEqual(decision["level"], "error")
        self.assertIn(DEFORM_GATE_REJECT_PREFIX, decision["hint"])

    def test_format_deform_gate_rejections_accepts_plain_strings(self):
        text = format_deform_gate_rejections({"aaaa": "家族未完整"})
        self.assertEqual(text, "aaaa(家族未完整)")
        self.assertEqual(format_deform_gate_rejections({}), "")

    def test_format_deform_gate_rejections_truncates_long_missing_lists(self):
        text = format_deform_gate_rejections(
            {"aaaa": {"family": "aaaa", "missing": [f"m{i}" for i in range(9)]}}
        )
        self.assertIn("共 9 个", text)

    def test_format_skip_reasons_limits_entries(self):
        reasons = {f"ib{index}": "原因" for index in range(9)}
        text = format_skip_reasons(reasons)
        self.assertIn("另有 3 个部件同类问题", text)


class TestRobustness(unittest.TestCase):
    def test_non_numeric_inputs_degrade_to_zero(self):
        decision = evaluate_merged_skeleton_contract(
            checkbox_enabled=True, parts_with_data="oops", component_count=None
        )
        self.assertEqual(decision["level"], "notice")

    def test_warning_counts_both_reason_sources(self):
        decision = evaluate_merged_skeleton_contract(
            checkbox_enabled=True,
            parts_with_data=4,
            component_count=2,
            skip_reasons={"aaaa": "x"},
            gate_rejected_parts={"bbbb": {"family": "bbbb", "missing": ["bbbb-1"]}},
        )
        self.assertEqual(decision["level"], "warning")
        self.assertIn("2 个部件未进入合并骨架", decision["message"])


class TestDiagnosticsInPrompt(unittest.TestCase):
    """2026-09 实报：中止时提示词**没有任何理由**，且建议会诱导用户丢弃正常数据。

    真实原因一直只写在 ``plan.diagnostics`` 里；这里钉死它被接进提示词、被去重/限量，
    并且建议改成 G1 的实际要求（「把该 IB 家族的全部成员一起导出」）。
    """

    def test_error_hint_includes_diagnostics_and_g1_advice(self):
        decision = evaluate_merged_skeleton_contract(
            checkbox_enabled=True,
            parts_with_data=5,
            component_count=0,
            gate_rejected_parts={
                "2c706fbd": {"family": "2c706fbd", "missing": ["2c706fbd-444-54936"]}
            },
            diagnostics=[
                "资源声明：ResourceHI3MergedSkeleton_role_x type=RWBuffer …",  # 纯规格行 → 过滤
                "G1 权威判定来自 prepare（pass 级）：放行 0 个部件，拒绝 5 个 deform pass",
                "本次是**子集导出**：G1 门控按本次导出集合重算后采用（跳过骨架摘要比对）",
                "变形家族未完整纳入（G1）：2c706fbd(家族=2c706fbd；未纳入=…)",  # 与 gate_text 重复 → 去重
            ],
        )
        self.assertEqual(decision["level"], "error")
        hint = decision["hint"]
        self.assertIn("诊断：", hint, "必须把真实原因写进提示词")
        self.assertIn("子集导出", hint)
        self.assertIn("拒绝 5 个 deform pass", hint)
        self.assertNotIn("资源声明：", hint, "规格行不是原因，不该占用提示词")
        self.assertEqual(
            hint.count(DEFORM_GATE_REJECT_PREFIX), 1, "G1 拒绝段只应出现一次（去重）"
        )
        self.assertIn("IB 家族", hint)
        self.assertIn("一起导出", hint)
        self.assertNotIn("清除骨骼合并VGMap缓存", hint, "不得再建议清除正常数据")
        self.assertIn("合并数据摘要", hint, "只在真的不同源时才引导重新导入")

    def test_warning_hint_includes_diagnostics_and_partial_advice(self):
        decision = evaluate_merged_skeleton_contract(
            checkbox_enabled=True,
            parts_with_data=5,
            component_count=2,
            gate_rejected_parts={"bbbb2222": {"family": "bbbb2222", "missing": ["bbbb2222-2-0"]}},
            diagnostics=["G1 权威判定来自 prepare（pass 级）：放行 2 个部件，拒绝 1 个 deform pass"],
        )
        self.assertEqual(decision["level"], "warning")
        hint = decision["hint"]
        self.assertIn("诊断：", hint)
        self.assertIn("放行 2 个部件", hint)
        self.assertIn("没有合并收益", hint)
        self.assertIn("一起导出", hint)
        self.assertNotIn("清除骨骼合并VGMap缓存", hint)

    def test_diagnostics_are_limited_and_marked(self):
        decision = evaluate_merged_skeleton_contract(
            checkbox_enabled=True,
            parts_with_data=3,
            component_count=0,
            diagnostics=[f"诊断行 {index}" for index in range(9)],
        )
        hint = decision["hint"]
        self.assertIn("诊断行 0", hint)
        self.assertIn("另有 3 条诊断", hint, "超过上限时要标注还剩几条")
        self.assertNotIn("诊断行 8", hint)

    def test_missing_diagnostics_keeps_the_hint_working(self):
        decision = evaluate_merged_skeleton_contract(
            checkbox_enabled=True, parts_with_data=3, component_count=0
        )
        self.assertEqual(decision["level"], "error")
        self.assertNotIn("诊断：", decision["hint"])
        self.assertIn("IB 家族", decision["hint"])

    def test_long_diagnostics_are_truncated(self):
        decision = evaluate_merged_skeleton_contract(
            checkbox_enabled=True,
            parts_with_data=3,
            component_count=0,
            diagnostics=["很长的一行 " * 60],
        )
        self.assertIn("…", decision["hint"])
        self.assertLess(len(decision["hint"]), 1200)


if __name__ == "__main__":
    unittest.main()
