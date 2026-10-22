"""导出期跨分块边界顶点焊接的纯逻辑测试（不依赖 Blender / bpy）。

被测模块：``common/merge_weld.py`` 的 ``compute_weld_groups``。
规则来源：《合并顶点节点-计划书.md》第 3、4.1、7.1 节。
"""

import importlib.util
import math
import sys
import unittest
from pathlib import Path


_MODULE_NAME = "_merge_weld_node_test_module"
_MODULE_PATH = Path(__file__).resolve().parents[1] / "common" / "merge_weld.py"
_spec = importlib.util.spec_from_file_location(_MODULE_NAME, _MODULE_PATH)
assert _spec is not None and _spec.loader is not None
merge_weld_module = importlib.util.module_from_spec(_spec)
sys.modules[_MODULE_NAME] = merge_weld_module
_spec.loader.exec_module(merge_weld_module)

compute_weld_groups = merge_weld_module.compute_weld_groups


def _all_group_members(result):
    return {index for group in result["groups"] for index in group}


class ComputeWeldGroupsTests(unittest.TestCase):
    def test_plan_example_five_vertices_four_blocks(self):
        """计划书 3.1 用户举例：5 个重合顶点，分块 1/2/3/4/4，第 5 个非边界。"""
        positions = [[0.0, 0.0, 0.0]] * 5
        boundaries = [True, True, True, True, False]
        blocks = [1, 2, 3, 4, 4]

        result = compute_weld_groups(positions, boundaries, blocks, 1e-6)

        self.assertEqual(result["groups"], [[0, 1, 2, 3]])
        self.assertNotIn(4, _all_group_members(result))
        self.assertEqual(result["merged_group_count"], 1)
        self.assertEqual(result["involved_block_count"], 4)
        self.assertEqual(result["excluded_non_boundary_count"], 1)
        self.assertEqual(result["skipped_single_block_count"], 0)

    def test_single_block_group_is_skipped(self):
        """同一分块内的重合边界顶点不互相合并，整组跳过。"""
        positions = [[0.0, 0.0, 0.0], [0.0, 0.0, 0.0]]
        boundaries = [True, True]
        blocks = [1, 1]

        result = compute_weld_groups(positions, boundaries, blocks, 1e-6)

        self.assertEqual(result["groups"], [])
        self.assertEqual(result["merged_group_count"], 0)
        self.assertEqual(result["involved_block_count"], 0)
        self.assertEqual(result["skipped_single_block_count"], 1)

    def test_same_block_uses_lowest_index_representative_once(self):
        """同一分块同位置多个边界顶点只取最小索引代表，组内每分块只出现一次。"""
        positions = [[0.0, 0.0, 0.0]] * 4
        boundaries = [True, True, True, True]
        blocks = [1, 1, 2, 2]

        result = compute_weld_groups(positions, boundaries, blocks, 1e-6)

        self.assertEqual(result["groups"], [[0, 2]])
        group = result["groups"][0]
        group_blocks = [blocks[index] for index in group]
        self.assertEqual(group_blocks, [1, 2])
        self.assertEqual(len(group_blocks), len(set(group_blocks)))
        # 同分块落选的 1 号、3 号顶点保持原样，不进入任何组
        self.assertEqual(_all_group_members(result), {0, 2})
        self.assertEqual(result["skipped_single_block_count"], 0)

    def test_non_boundary_vertices_are_counted_and_never_grouped(self):
        """blocks>0 的非边界顶点计入排除数；block=0 的顶点完全不参与统计与分组。"""
        positions = [[0.0, 0.0, 0.0]] * 6
        boundaries = [True, False, True, False, False, True]
        blocks = [1, 1, 2, 2, 0, 0]

        result = compute_weld_groups(positions, boundaries, blocks, 1e-6)

        self.assertEqual(result["groups"], [[0, 2]])
        self.assertEqual(result["excluded_non_boundary_count"], 2)
        self.assertEqual(_all_group_members(result), {0, 2})
        self.assertEqual(result["merged_group_count"], 1)
        self.assertEqual(result["involved_block_count"], 2)
        self.assertEqual(result["skipped_single_block_count"], 0)

    def test_threshold_below_vertex_gap_does_not_merge(self):
        """距离略大于 0 的两点在 1e-6 阈值下不合并，调大阈值后才合并。"""
        positions = [[0.0, 0.0, 0.0], [1e-5, 0.0, 0.0]]
        boundaries = [True, True]
        blocks = [1, 2]

        strict = compute_weld_groups(positions, boundaries, blocks, 1e-6)
        self.assertEqual(strict["groups"], [])
        self.assertEqual(strict["merged_group_count"], 0)
        # 两顶点分处两个格子，各自构成一个只含单分块的候选组，均被跳过
        self.assertEqual(strict["skipped_single_block_count"], 2)
        self.assertEqual(strict["excluded_non_boundary_count"], 0)

        loose = compute_weld_groups(positions, boundaries, blocks, 1e-4)
        self.assertEqual(loose["groups"], [[0, 1]])
        self.assertEqual(loose["merged_group_count"], 1)
        self.assertEqual(loose["involved_block_count"], 2)
        self.assertEqual(loose["skipped_single_block_count"], 0)

    def test_no_chaining_across_grid_cells(self):
        """A-B 近、B-C 近、A-C 超过阈值：只合并同格子的 A/B，不链式扩散到 C。"""
        distance = 1.0
        positions = [
            [-0.49, 0.0, 0.0],  # A：格子 0
            [0.49, 0.0, 0.0],   # B：格子 0
            [0.55, 0.0, 0.0],   # C：格子 1
        ]
        blocks = [1, 2, 3]
        boundaries = [True, True, True]

        # 前提校验：A-B、B-C 在阈值内，A-C 超出阈值
        self.assertLess(math.dist(positions[0], positions[1]), distance)
        self.assertLess(math.dist(positions[1], positions[2]), distance)
        self.assertGreater(math.dist(positions[0], positions[2]), distance)

        result = compute_weld_groups(positions, boundaries, blocks, distance)

        self.assertEqual(result["groups"], [[0, 1]])
        self.assertNotIn(2, _all_group_members(result))
        for group in result["groups"]:
            self.assertFalse(0 in group and 2 in group)
        self.assertEqual(result["skipped_single_block_count"], 1)

    def test_non_positive_distance_raises_value_error(self):
        positions = [[0.0, 0.0, 0.0], [0.0, 0.0, 0.0]]
        boundaries = [True, True]
        blocks = [1, 2]

        for invalid_distance in (0, 0.0, -1e-6):
            with self.subTest(distance=invalid_distance):
                with self.assertRaises(ValueError):
                    compute_weld_groups(positions, boundaries, blocks, invalid_distance)

    def test_mismatched_input_lengths_raise_value_error(self):
        positions = [[0.0, 0.0, 0.0], [0.0, 0.0, 0.0]]

        with self.subTest(field="boundaries"):
            with self.assertRaises(ValueError):
                compute_weld_groups(positions, [True], [1, 2], 1e-6)

        with self.subTest(field="blocks"):
            with self.assertRaises(ValueError):
                compute_weld_groups(positions, [True, True], [1], 1e-6)

    def test_output_is_stable_and_groups_sorted_by_block_id(self):
        """相同输入重复调用结果相等；组内代表按 block id 升序，而非顶点扫描顺序。"""
        positions = [[0.0, 0.0, 0.0]] * 3
        boundaries = [True, True, True]
        blocks = [3, 1, 2]

        first = compute_weld_groups(positions, boundaries, blocks, 1e-6)
        second = compute_weld_groups(list(positions), list(boundaries), list(blocks), 1e-6)

        self.assertEqual(first, second)
        self.assertEqual(first["groups"], [[1, 2, 0]])
        self.assertEqual([blocks[index] for index in first["groups"][0]], [1, 2, 3])


if __name__ == "__main__":
    unittest.main()
