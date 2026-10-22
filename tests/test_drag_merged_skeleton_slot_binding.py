"""拖拽交互的 t88 合并骨架绑定：(组号, cid) 从产物推导 + 槽位口径。

回归背景（上游对 K: 实机产物的审查实测，见 ZZMI骨骼合并计划书.md 修复链）：
 - 旧实现写死 `ResourceZZMergedSkeleton_G3_s1/s2`，而真实产物是 G0/G1 等任意组
   ⇒ `ref` 指向不存在的资源，3DMigoto 弃载整段；
 - 旧实现写死 `$zz_ms_occ_0`，第二个合并部件因此读别人的计数器；
 - 旧口径 `occ == 1 ? s1 : s2` 把 occ == 0（本件 deform 段尚未跑）判成 s2，
   于是读到上一帧第 2 次出现的旧值或全零骨架。

本文件用合成产物夹具覆盖：组号来源、cid 来源、三种 occ 取值、以及「无法确定就
不发射 + warning」的拒绝分支。
"""

import importlib
import io
import re
import unittest
from collections import OrderedDict
from unittest import mock

import numpy as np

from tests.test_node_postprocess_draginteraction import (
    _base_sections,
    _load_drag_module,
    _make_node,
    _two_component_sections,
)

# 夹具组的组号：故意用 G1（不是 G3），硬编码 G3 的旧实现会在这里变红。
MERGED_GROUP_NUM = 1
# 产物里池骨数 324 时的 array 值（池骨数 × 3），只为形态逼真，不影响断言。
MERGED_ARRAY_LEN = 972
NS = "testns"


def _log_class():
    """取真实的 LOG 类（模块加载后 utils.log_utils 已可导入）。"""
    return importlib.import_module("_ssmt_root.utils.log_utils").LOG


def _merged_resource_sections(group):
    """合并骨架资源段：[ResourceZZMergedSkeleton_G<g>_s1] / _s2。"""
    return [
        (f"[ResourceZZMergedSkeleton_G{group}_s1]", [
            "type = RWBuffer",
            "format = R32G32B32A32_FLOAT",
            f"array = {MERGED_ARRAY_LEN}",
        ]),
        (f"[ResourceZZMergedSkeleton_G{group}_s2]", [
            "type = RWBuffer",
            "format = R32G32B32A32_FLOAT",
            f"array = {MERGED_ARRAY_LEN}",
        ]),
    ]


def _deform_vb_section(hash_value, cid, group):
    """该部件自己的 deform 段，形态同 SSMT 产物里的 TextureOverride_VB_*_Position。

    group 为 None 时不写骨架引用（用于逼出「按资源段名回退」的分支）。
    """
    occ = f"$zz_ms_occ_{cid}"
    lines = [
        f"{occ} = {occ} + 1",
        f"if {occ} >= 3",
        f"\t{occ} = 1",
        "endif",
    ]
    if group is not None:
        skel = f"ResourceZZMergedSkeleton_G{group}"
        lines.extend([
            f"\tvs-t0 = {skel}_s1",
            f"if {occ} == 1",
            f"\tvs-t0 = {skel}_s1",
            "endif",
            f"if {occ} == 2",
            f"\tvs-t0 = {skel}_s2",
            "endif",
        ])
    return (f"[TextureOverride_VB_{hash_value}_{hash_value}_Position]", lines)


def _sections_with(extra_sections, base=None):
    """把额外段挂到产物 sections 上（这些段都不带 hash = 行，不被组件发现消费）。"""
    sections = _base_sections() if base is None else base
    for name, lines in extra_sections:
        sections[name] = list(lines)
    return sections


def _collision_comp(comp):
    """给组件挂上碰撞网格/三角网格（t88 绑定块只在 collision_trigrid 分支内发射）。"""
    comp["collision_grid"] = {
        "bmin": np.array([0.0, 0.0, 0.0], dtype=np.float32),
        "h": 0.1,
        "h_c": 0.4,
        "dims": (10, 8, 6),
        "cdims": (3, 2, 2),
    }
    comp["collision_trigrid"] = {
        "bmin": (0.0, 0.0, 0.0),
        "cell": 0.0125,
        "dims": (64, 34, 102),
        "cell_count": 3242,
    }


def _t88_block(lines):
    """截取 t88 条件绑定块（if / ref s2 / else / ref s1 / endif 共 5 行）。"""
    for index, line in enumerate(lines):
        if re.match(r"^\tif \$zz_ms_occ_\d+ == \d+$", line):
            return lines[index:index + 5]
    return []


class DragMergedSkeletonSlotBindingTests(unittest.TestCase):
    """t88 只许绑定产物里真实存在的组，并且只读本部件自己的出现次计数器。"""

    @classmethod
    def setUpClass(cls):
        cls.mod = _load_drag_module()

    def _emit(self, extra_sections, hash_values=("abc123",), base=None):
        node = _make_node(self.mod, collision_enabled=True)
        sections = _sections_with(extra_sections, base=base)
        comps = node._locate_components(sections, list(hash_values))
        for comp in comps:
            _collision_comp(comp)
        node._emit_sections(sections, comps, NS)
        return node, sections, comps

    @staticmethod
    def _jiggle_lines(sections, comp):
        name = f"[CustomShaderDragJiggle{comp['comp_name']}_{NS}]"
        assert name in sections, f"缺少 jiggle 段 {name}，实得 {list(sections)[-3:]}"
        return sections[name]

    @staticmethod
    def _bound_resource(ref_line):
        """从 `\t\tcs-t88 = ref <资源>` 取出资源名。"""
        return ref_line.split("ref", 1)[1].strip()

    def _select_slot(self, block, occ):
        """按 3DMigoto 语义走一遍发射块，返回实际被绑定的骨架资源名。"""
        match = re.match(r"^\tif \$zz_ms_occ_(\d+) == (\d+)$", block[0])
        self.assertIsNotNone(match, f"t88 块首行不是出现次条件：{block[0]!r}")
        self.assertEqual(match.group(1), str(self._expected_occ),
                         f"t88 必须读本部件的 $zz_ms_occ_{self._expected_occ}：{block[0]!r}")
        self.assertEqual(block[2], "\telse", f"t88 块第 3 行应为 else：{block[2]!r}")
        self.assertEqual(block[4], "\tendif", f"t88 块末行应为 endif：{block[4]!r}")
        if occ == int(match.group(2)):
            return self._bound_resource(block[1])
        return self._bound_resource(block[3])

    def _single_component_block(self, group=MERGED_GROUP_NUM, vb_group=None):
        extra = [_deform_vb_section("abc123", 0, group if vb_group is None else vb_group)]
        if group is not None:
            extra.extend(_merged_resource_sections(group))
        node, sections, comps = self._emit(extra)
        self._expected_occ = 0
        return self._jiggle_lines(sections, comps[0])

    # ---- 验收 1：组号来自产物（G1 夹具抓硬编码 G3）--------------------------
    def test_binds_group_that_exists_in_product(self):
        block = _t88_block(self._single_component_block())
        self.assertEqual(len(block), 5, f"应发射 5 行 t88 绑定块，实得 {block}")

        reference = f"ResourceZZMergedSkeleton_G{MERGED_GROUP_NUM}"
        self.assertEqual(block[0], "\tif $zz_ms_occ_0 == 2")
        self.assertEqual(block[1], f"\t\tcs-t88 = ref {reference}_s2")
        self.assertEqual(block[2], "\telse")
        self.assertEqual(block[3], f"\t\tcs-t88 = ref {reference}_s1")
        self.assertEqual(block[4], "\tendif")

        # 组号断言：硬编码 G3 的实现在这里变红。
        joined = "\n".join(block)
        self.assertIn(f"_G{MERGED_GROUP_NUM}_", joined,
                      f"t88 必须引用产物里的 G{MERGED_GROUP_NUM}：{joined}")
        self.assertNotIn("_G3_", joined, f"t88 不得引用产物里不存在的 G3：{joined}")

    def test_block_contains_bindings_only(self):
        block = _t88_block(self._single_component_block())
        for line in block:
            self.assertNotIn("run", line, f"t88 块内不许跑 CS：{line!r}")
            self.assertNotIn("dispatch", line, f"t88 块内不许 dispatch：{line!r}")
            self.assertNotIn("draw", line, f"t88 块内不许 draw：{line!r}")

    # ---- 验收 2：第二个部件用 $zz_ms_occ_1 ---------------------------------
    def test_second_component_uses_its_own_occ_counter(self):
        extra = [
            _deform_vb_section("abc123", 0, MERGED_GROUP_NUM),
            _deform_vb_section("def456", 1, MERGED_GROUP_NUM),
            *_merged_resource_sections(MERGED_GROUP_NUM),
        ]
        node, sections, comps = self._emit(
            extra, hash_values=("abc123", "def456"), base=_two_component_sections()
        )
        self.assertEqual(len(comps), 2, f"应发现 2 个合并部件，实得 {len(comps)}")

        first = _t88_block(self._jiggle_lines(sections, comps[0]))
        second = _t88_block(self._jiggle_lines(sections, comps[1]))
        self.assertEqual(len(first), 5, f"部件 1 缺 t88 块：{first}")
        self.assertEqual(len(second), 5, f"部件 2 缺 t88 块：{second}")

        self.assertTrue(first[0].startswith("\tif $zz_ms_occ_0 == 2"),
                        f"部件 1 应读 $zz_ms_occ_0：{first[0]!r}")
        self.assertTrue(second[0].startswith("\tif $zz_ms_occ_1 == 2"),
                        f"部件 2 应读自己的 $zz_ms_occ_1，实得：{second[0]!r}")
        self.assertNotIn("$zz_ms_occ_0", "\n".join(second),
                         f"部件 2 不许读别人的计数器：{second}")

    # ---- 验收 3：occ == 2 -> s2，0/1 -> s1 ---------------------------------
    def test_slot_criterion_follows_occ_value(self):
        block = _t88_block(self._single_component_block())
        self.assertEqual(len(block), 5, f"应发射 5 行 t88 绑定块，实得 {block}")

        reference = f"ResourceZZMergedSkeleton_G{MERGED_GROUP_NUM}"
        self._expected_occ = 0
        self.assertEqual(self._select_slot(block, 0), f"{reference}_s1",
                         "occ == 0（本件 deform 段尚未跑）必须走 s1")
        self.assertEqual(self._select_slot(block, 1), f"{reference}_s1",
                         "occ == 1（本帧首次出现）必须走 s1")
        self.assertEqual(self._select_slot(block, 2), f"{reference}_s2",
                         "occ == 2（本帧第二次出现）必须走 s2")

    # ---- 验收 4：无法确定就不发射 + warning --------------------------------
    def test_skips_block_and_warns_when_occ_undeterminable(self):
        # 合并产物（有合并骨架资源段），但本件没有 deform 段 ⇒ 推不出 cid，必须告警。
        log = _log_class()
        with mock.patch.object(log, "warning") as warning:
            node, sections, comps = self._emit(_merged_resource_sections(MERGED_GROUP_NUM))
        lines = self._jiggle_lines(sections, comps[0])
        self.assertEqual(_t88_block(lines), [],
                         f"产物里没有 deform 段时不得发射 t88：{_t88_block(lines)}")
        self.assertEqual([line for line in lines if "cs-t88 = ref" in line], [],
                         "无法确定 cid 时连 ref 行都不许留")
        self.assertTrue(warning.called, "无法确定 cid 时必须 LOG.warning，不许静默")
        message = warning.call_args[0][0]
        self.assertIn("cs-t88", message)
        self.assertIn("出现次计数器", message)

    def test_skips_block_and_warns_when_group_ambiguous(self):
        # deform 段只有计数器没有骨架引用（回退按资源段名找组），产物里同时存在
        # G1 与 G3 两组资源 ⇒ 无法判定本部件属于哪组，必须拒绝发射。
        extra = [_deform_vb_section("abc123", 0, None)]
        extra.extend(_merged_resource_sections(1))
        extra.extend(_merged_resource_sections(3))
        log = _log_class()
        with mock.patch.object(log, "warning") as warning:
            node, sections, comps = self._emit(extra)
        lines = self._jiggle_lines(sections, comps[0])
        self.assertEqual(_t88_block(lines), [],
                         f"组号不唯一时不得发射 t88：{_t88_block(lines)}")
        self.assertTrue(warning.called, "组号不唯一时必须 LOG.warning，不许静默")
        message = warning.call_args[0][0]
        self.assertIn("cs-t88", message)
        self.assertIn("组号", message)
        self.assertIn("1, 3", message)

    def test_non_merged_product_emits_nothing_and_warns_nothing(self):
        # 非合并拖拽模组：有 collision_trigrid，但产物里没有 $zz_ms_occ_*、也没有
        # [ResourceZZMergedSkeleton_*] ⇒ 本就不该有 t88，静默跳过且不许告警。
        log = _log_class()
        with mock.patch.object(log, "warning") as warning:
            node, sections, comps = self._emit([])
        self.assertFalse(node._is_merged_skeleton_product(sections),
                         "夹具必须是非合并产物（否则这条门控用例无效）")
        mentions = [
            line for section_lines in sections.values() for line in section_lines
            if "cs-t88" in line
        ]
        self.assertEqual(mentions, ["cs-t88 = null"],
                         f"非合并产物不得发射任何 t88 绑定（只留既有的无条件 null 行）：{mentions}")
        self.assertFalse(warning.called,
                         "非合并产物是静默跳过，不许打「推不出合并骨架绑定」的告警")

    # ---- 多组共存时按本部件自己的段判定 -----------------------------------
    def test_multiple_groups_resolved_by_own_section(self):
        extra = [_deform_vb_section("abc123", 0, 1)]
        extra.extend(_merged_resource_sections(1))
        extra.extend(_merged_resource_sections(3))
        block = _t88_block(self._single_component_block_passthrough(extra))
        self.assertEqual(len(block), 5, f"应发射 5 行 t88 绑定块，实得 {block}")
        joined = "\n".join(block)
        self.assertIn("_G1_", joined, f"应绑定本部件段里引用的 G1：{joined}")
        self.assertNotIn("_G3_", joined, f"不得绑定别的组 G3：{joined}")

    def _single_component_block_passthrough(self, extra):
        node, sections, comps = self._emit(extra)
        return self._jiggle_lines(sections, comps[0])

    # ---- 守卫：空窗/无碰撞网格时本就不发射 --------------------------------
    def test_no_block_for_component_without_collision_trigrid(self):
        node = _make_node(self.mod, collision_enabled=True)
        sections = _sections_with(
            [_deform_vb_section("abc123", 0, MERGED_GROUP_NUM), *_merged_resource_sections(MERGED_GROUP_NUM)]
        )
        comps = node._locate_components(sections, ["abc123"])
        comps[0]["collision_grid"] = {"bmin": np.array([0.0, 0.0, 0.0], dtype=np.float32),
                                      "h": 0.1, "h_c": 0.4, "dims": (10, 8, 6), "cdims": (3, 2, 2)}
        node._emit_sections(sections, comps, NS)
        lines = self._jiggle_lines(sections, comps[0])
        self.assertEqual(_t88_block(lines), [],
                         "没有 collision_trigrid 的部件不得发射 t88 绑定块")


if __name__ == "__main__":
    unittest.main()
