# -*- coding: utf-8 -*-
"""材质指定节点「段尾补恢复块」行为测试（PR#15 新增分支）。

覆盖 ``_insert_restore_blocks`` 的两种落点：
1. 指定部件之后还有默认部件 → 恢复块插在默认部件的 mesh 注释后（既有行为）；
2. 指定部件被移到段尾（``_move_top_level_targets_to_end``）时，段内没有后续默认
   部件可“顺路”恢复，必须以 GIMI ORFix 写法为条件在段尾补一次。
"""
import importlib.util
import os
import re
import sys
import types
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

PKG = "_custom_material_assign_pr15_test_pkg"
for _package_name in (PKG, f"{PKG}.blueprint"):
    _package = types.ModuleType(_package_name)
    _package.__path__ = []
    sys.modules[_package_name] = _package


class _FakePropertyBase:
    pass


def _fake_prop(**_kwargs):
    return None


_fake_bpy = types.SimpleNamespace(
    types=types.SimpleNamespace(
        PropertyGroup=_FakePropertyBase,
        Operator=_FakePropertyBase,
        Object=object,
        Node=_FakePropertyBase,
    ),
    props=types.SimpleNamespace(
        StringProperty=_fake_prop,
        BoolProperty=_fake_prop,
        IntProperty=_fake_prop,
        FloatProperty=_fake_prop,
        EnumProperty=_fake_prop,
        PointerProperty=_fake_prop,
        CollectionProperty=_fake_prop,
    ),
    utils=types.SimpleNamespace(
        register_class=lambda _cls: None,
        unregister_class=lambda _cls: None,
    ),
)
sys.modules["bpy"] = _fake_bpy


class _FakeMaterialBase:
    """只提供本测试所需的接口形态。"""

    @staticmethod
    def _replace_non_ascii_runs(text):
        return re.sub(r"[^\x00-\x7f]+", lambda _m: "_", str(text or ""))

    def extract_mesh_name(self, line):
        match = re.search(r'\[mesh:([^\]]+)\]', str(line or ""))
        return match.group(1) if match else None


_material_module = types.ModuleType(f"{PKG}.blueprint.node_postprocess_material")
_material_module.SSMTNode_PostProcess_MaterialBase = _FakeMaterialBase
_material_module.MATERIAL_DETECT_PRESETS = {}


def _load_gimi_orfix_adapter():
    spec = importlib.util.spec_from_file_location(
        "gimi_orfix_adapter_probe", REPO_ROOT / "blueprint" / "gimi_orfix_adapter.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_material_module._load_gimi_orfix_adapter = _load_gimi_orfix_adapter
sys.modules[f"{PKG}.blueprint.node_postprocess_material"] = _material_module

_spec = importlib.util.spec_from_file_location(
    f"{PKG}.blueprint.node_postprocess_custom_material_assign",
    REPO_ROOT / "blueprint" / "node_postprocess_custom_material_assign.py",
)
module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(module)

FIX = r"run = CommandList\global\ORFix\ORFix"
MARK_START = module.RESTORE_MARKER_START
MARK_END = module.RESTORE_MARKER_END


class _Node(module.SSMTNode_PostProcess_CustomMaterialAssign):
    """绕过 bpy 实例化：只装 _insert_restore_blocks 需要的最小状态。"""

    def __init__(self, custom_names, restore=True):
        self.extract_mesh_name = _FakeMaterialBase().extract_mesh_name
        self.restore_default_textures_after_draw = restore
        self._custom_names = set(custom_names)

    def _is_custom_target(self, obj):
        return obj is not None and obj in self._custom_names

    def find_object_by_mesh_name(self, mesh_name, object_filter=None):
        # 生产实现用的是 `==` 比较绑定方法；这里保持等价语义（不要把 `is` 塞进来，
        # 每次属性访问都会生成新的 bound method 对象）。
        if object_filter is None or mesh_name not in self._custom_names:
            return None
        return mesh_name if object_filter(mesh_name) else None


def _block_text(lines):
    if MARK_START not in lines:
        return None
    start = lines.index(MARK_START)
    end = lines.index(MARK_END)
    return lines[start:end + 1]


class TailRestoreBlockTests(unittest.TestCase):
    def test_restore_block_inserted_before_later_default_mesh(self):
        """既有行为：后面还有默认部件时，恢复块落在该部件 mesh 注释之后。"""
        node = _Node({"CustomPart"})
        lines = [
            "[TextureOverride_x]", "ps-t0 = CustomRes", FIX,
            "; [mesh:CustomPart] [vertex_count:3]", "drawindexed = 100,0,0",
            "; [mesh:DefaultPart] [vertex_count:3]", "drawindexed = 200,0,0",
        ]
        node._insert_restore_blocks(lines)

        block = _block_text(lines)
        self.assertIsNotNone(block)
        default_mesh_index = lines.index("; [mesh:DefaultPart] [vertex_count:3]")
        self.assertEqual(lines[default_mesh_index + 1], MARK_START)
        self.assertIn("ps-t0 = CustomRes", block)
        self.assertIn(FIX, block)

    def test_tail_restore_block_added_when_target_is_last(self):
        """新增分支：指定部件在段尾且是 GIMI ORFix 写法时，段尾补恢复块。"""
        node = _Node({"CustomPart"})
        lines = [
            "[TextureOverride_x]", "ps-t0 = CustomRes", FIX,
            "; [mesh:CustomPart] [vertex_count:3]", "drawindexed = 100,0,0",
        ]
        node._insert_restore_blocks(lines)

        block = _block_text(lines)
        self.assertIsNotNone(block, "段尾必须补上恢复块")
        self.assertEqual(lines[-1], MARK_END)
        self.assertIn("ps-t0 = CustomRes", block)
        self.assertIn(FIX, block)

    def test_tail_branch_requires_orfix_section(self):
        """非 GIMI ORFix 段落（ZZZ/崩铁/NTEMI）不得新增段尾恢复块。"""
        node = _Node({"CustomPart"})
        lines = [
            "[TextureOverride_x]", "ps-t0 = CustomRes",
            "; [mesh:CustomPart] [vertex_count:3]", "drawindexed = 100,0,0",
        ]
        node._insert_restore_blocks(lines)

        self.assertNotIn(MARK_START, lines)

    def test_no_custom_target_means_no_tail_block(self):
        node = _Node(set())
        lines = [
            "[TextureOverride_x]", "ps-t0 = CustomRes", FIX,
            "; [mesh:OtherPart] [vertex_count:3]", "drawindexed = 100,0,0",
        ]
        node._insert_restore_blocks(lines)

        self.assertNotIn(MARK_START, lines)

    def test_restore_disabled_means_no_block(self):
        node = _Node({"CustomPart"}, restore=False)
        lines = [
            "[TextureOverride_x]", "ps-t0 = CustomRes", FIX,
            "; [mesh:CustomPart] [vertex_count:3]", "drawindexed = 100,0,0",
        ]
        node._insert_restore_blocks(lines)

        self.assertNotIn(MARK_START, lines)


if __name__ == "__main__":
    unittest.main()
