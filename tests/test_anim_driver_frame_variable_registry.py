# -*- coding: utf-8 -*-
"""运行时间节点帧变量预分配（真实 ``variable_registry`` 的分配器）测试。

前面的 ``test_anim_driver_base`` 用的是桩分配器（只验节点侧语义），这里装载**真实**
``blueprint/variable_registry.py``，验证分配器本身的契约：
首次分配、幂等不重分配、按 auto_index 唯一、撞名递增、auto_index 变化不改已分配名。
"""
import importlib.util
import sys
import types
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

PKG = "_anim_driver_frame_var_pkg"
for _package_name in (PKG, f"{PKG}.blueprint"):
    _package = types.ModuleType(_package_name)
    _package.__path__ = []
    sys.modules[_package_name] = _package

_fake_bpy = types.ModuleType("bpy")
_fake_bpy.data = types.SimpleNamespace(node_groups=[])
_fake_bpy.context = types.SimpleNamespace(scene=None)
_SAVED_BPY = sys.modules.get("bpy")
sys.modules["bpy"] = _fake_bpy

try:
    _spec = importlib.util.spec_from_file_location(
        f"{PKG}.blueprint.variable_registry",
        REPO_ROOT / "blueprint" / "variable_registry.py",
    )
    registry = importlib.util.module_from_spec(_spec)
    sys.modules[_spec.name] = registry
    _spec.loader.exec_module(registry)
finally:
    if _SAVED_BPY is None:
        sys.modules.pop("bpy", None)
    else:
        sys.modules["bpy"] = _SAVED_BPY


class _FakeRuntimeNode:
    """只带分配器用到的字段（运行时间节点替身）。"""

    bl_idname = "SSMTNode_AnimDriver_Runtime"

    def __init__(self, name, auto_index=1, assigned="", custom=""):
        self.name = name
        self.auto_index = auto_index
        self.assigned_frame_variable_name = assigned
        self.custom_frame_variable_name = custom


class _FakeTree:
    def __init__(self, nodes=()):
        self.name = "动画驱动蓝图"
        self.bl_idname = "SSMTBlueprintTreeType"
        self.nodes = list(nodes)


class AnimDriverFrameVariableAllocationTests(unittest.TestCase):
    def setUp(self):
        _fake_bpy.data.node_groups = []

    def test_first_allocation_uses_auto_index_and_is_written_back(self):
        node = _FakeRuntimeNode("运行时间", auto_index=1)
        _fake_bpy.data.node_groups = [_FakeTree([node])]

        name = registry.ensure_anim_driver_frame_variable_name(node)

        self.assertEqual(name, "swapvar1")
        self.assertEqual(node.assigned_frame_variable_name, "swapvar1")

    def test_repeated_calls_do_not_reallocate(self):
        node = _FakeRuntimeNode("运行时间", auto_index=2)
        _fake_bpy.data.node_groups = [_FakeTree([node])]

        first = registry.ensure_anim_driver_frame_variable_name(node)
        # auto_index 后续被重排也不该改写已分配的名字
        node.auto_index = 7
        second = registry.ensure_anim_driver_frame_variable_name(node)

        self.assertEqual(first, "swapvar2")
        self.assertEqual(second, "swapvar2")

    def test_two_runtime_nodes_get_distinct_names(self):
        first = _FakeRuntimeNode("运行时间", auto_index=1)
        second = _FakeRuntimeNode("运行时间.001", auto_index=2)
        _fake_bpy.data.node_groups = [_FakeTree([first, second])]

        self.assertEqual(registry.ensure_anim_driver_frame_variable_name(first), "swapvar1")
        self.assertEqual(registry.ensure_anim_driver_frame_variable_name(second), "swapvar2")

    def test_allocation_avoids_name_taken_by_another_owner(self):
        occupied = _FakeRuntimeNode("运行时间", auto_index=2, assigned="swapvar2")
        newcomer = _FakeRuntimeNode("运行时间.001", auto_index=2)
        _fake_bpy.data.node_groups = [_FakeTree([occupied, newcomer])]

        name = registry.ensure_anim_driver_frame_variable_name(newcomer)

        self.assertEqual(name, "swapvar2_1")
        self.assertEqual(newcomer.assigned_frame_variable_name, "swapvar2_1")

    def test_user_custom_name_is_not_touched_by_allocator(self):
        node = _FakeRuntimeNode("运行时间", auto_index=1, custom="my_frame")
        _fake_bpy.data.node_groups = [_FakeTree([node])]

        name = registry.ensure_anim_driver_frame_variable_name(node)

        # 分配器只负责预分配名；手改名由节点侧 frame_variable_name() 优先使用
        self.assertEqual(name, "swapvar1")
        self.assertEqual(node.custom_frame_variable_name, "my_frame")


if __name__ == "__main__":
    unittest.main()
