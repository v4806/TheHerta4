# -*- coding: utf-8 -*-
"""材质转资源pro 的 IB 段结构化定位口径测试（fake bpy + fake package 装载）。

INI 段/块/行的解析本身由 ``blueprint/ini_scope.py`` 负责（纯函数，测试在
``tests/test_ini_scope.py``）；这里测调用方
``blueprint/node_postprocess_custom_material_assign.py`` 的定位口径：同一条件块里
「无材质单元（只有 drawindexed）+ 有材质单元（有 Resource 引用行）」必须拆成两个
同条件块，无材质块在前、有材质块在段尾。

注：本文件依赖 ``blueprint/ini_scope.py``（由 ini_scope 那个 PR 引入），它按文件
路径装载该模块；缺文件时会直接报错而不是静默退化。
"""
import importlib.util
import re
import sys
import types
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# ini_scope（纯函数模块）按文件路径装载：不经过包结构、不 import bpy。
INI_SCOPE_SPEC = importlib.util.spec_from_file_location(
    "ini_scope_under_test", REPO_ROOT / "blueprint" / "ini_scope.py"
)
ini_scope = importlib.util.module_from_spec(INI_SCOPE_SPEC)
INI_SCOPE_SPEC.loader.exec_module(ini_scope)


# --------------------------------------------------------------------------- #
# 材质转资源pro 定位口径（fake bpy + fake package 装载）
# --------------------------------------------------------------------------- #

PKG = "_custom_material_assign_pro_scope_test_pkg"
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
    data=types.SimpleNamespace(objects={}, node_groups={}),
    utils=types.SimpleNamespace(
        register_class=lambda _cls: None,
        unregister_class=lambda _cls: None,
    ),
)
sys.modules["bpy"] = _fake_bpy


class _FakeMaterialBase:
    """只提供本测试用到的接口形态（口径与真实实现的关键分支一致）。"""

    def extract_mesh_name(self, line):
        match = re.search(r"\[mesh:([^\]]+)\]", str(line or ""))
        return match.group(1) if match else None

    @staticmethod
    def _is_generated_material_line(line):
        stripped = str(line or "").strip()
        if not stripped:
            return False
        return (
            stripped.startswith("ps-t")
            or stripped.startswith("Resource\\ZZMI\\")
            or stripped
            in (
                "run = CommandList\\ZZMI\\SetTextures",
                "run = CommandList\\RabbitFX\\SetTextures",
            )
            or "材质切换" in stripped
        )


_material_module = types.ModuleType(f"{PKG}.blueprint.node_postprocess_material")
_material_module.SSMTNode_PostProcess_MaterialBase = _FakeMaterialBase
_material_module.MATERIAL_DETECT_PRESETS = {}
sys.modules[f"{PKG}.blueprint.node_postprocess_material"] = _material_module

#: 该模块内部的 ``from . import ini_scope`` 在 fake 包里会失败，模块自带按文件
#: 路径装载的兜底分支，所以这里不需要预先注册子模块。
_spec = importlib.util.spec_from_file_location(
    f"{PKG}.blueprint.node_postprocess_custom_material_assign",
    REPO_ROOT / "blueprint" / "node_postprocess_custom_material_assign.py",
)
module = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = module
_spec.loader.exec_module(module)


class _Node(module.SSMTNode_PostProcess_CustomMaterialAssign):
    """绕过 bpy 实例化：只装本测试需要的 mesh 判定与生成行判定。"""

    def __init__(self, custom_names):
        self.extract_mesh_name = _FakeMaterialBase().extract_mesh_name
        self._custom_names = set(custom_names)
        self.restore_default_textures_after_draw = False

    def _is_custom_target(self, obj):
        return obj is not None and obj in self._custom_names

    def find_object_by_mesh_name(self, mesh_name, object_filter=None):
        if object_filter is None or mesh_name not in self._custom_names:
            return None
        return mesh_name if object_filter(mesh_name) else None


def _run_reorder_pipeline(node, lines):
    """按 ``process_texture_override_section`` 的顺序跑定位 / 重排层。"""
    node._split_mixed_target_if_blocks(lines)
    node._move_targets_to_end_of_top_level_if_blocks(lines)
    node._move_top_level_targets_to_end(lines)
    node._reorder_units_non_target_first(lines)
    node._insert_restore_blocks(lines)


def _unit_starts(lines):
    """mesh 名 -> 绘制单元起始行号（用 ini_scope 口径，含块内单元）。"""
    return {
        unit["mesh"]: unit["start"]
        for unit in ini_scope.iter_draw_units(lines)
    }


def _mesh(mesh_name):
    return "; [mesh:{}] [vertex_count:3]".format(mesh_name)


def _count(lines, text):
    return sum(1 for line in lines if str(line or "").strip() == text)


def _block_between_if_and_endif(lines, if_index):
    depth = 0
    for index in range(if_index, len(lines)):
        stripped = str(lines[index] or "").strip()
        if re.match(r"^if\s+", stripped, re.IGNORECASE):
            depth += 1
            continue
        if stripped.casefold() == "endif":
            depth -= 1
            if depth == 0:
                return lines[if_index + 1:index]
    raise AssertionError("未配对 endif")


class CustomMaterialAssignProScopeTests(unittest.TestCase):
    def test_mixed_block_splits_no_material_unit_first_and_material_block_to_tail(self):
        """同一 if 块内：无材质单元（只有 drawindexed）在前、有材质单元在段尾。

        两个 mesh 都属于 target_items，但只有 MaterialPart 的单元里有材质转资源
        生成的 ``Resource\\ZZMI\\...`` 引用行；NoMaterialPart 读不到材质（实机日志
        口径：按非目标部件处理），必须进非目标块并排到前面。
        """
        condition = "if $switch == 1"
        lines = [
            condition,
            _mesh("MaterialPart"),
            r"Resource\ZZMI\Diffuse = ref Resource-MaterialPart",
            "drawindexed = 1,0,0",
            _mesh("NoMaterialPart"),
            "drawindexed = 2,0,0",
            "endif",
            _mesh("OtherPart"),
            "drawindexed = 3,0,0",
            ";MARK: tail",
        ]
        node = _Node({"MaterialPart", "NoMaterialPart"})
        node._split_mixed_target_if_blocks(lines)

        self.assertEqual(_count(lines, condition), 2, "必须拆成两个同条件块")
        self.assertEqual(_count(lines, "endif"), 2)
        index_no_material = lines.index(_mesh("NoMaterialPart"))
        index_material = lines.index(_mesh("MaterialPart"))
        index_other = lines.index(_mesh("OtherPart"))
        self.assertLess(index_no_material, index_material, "无材质单元必须排在前面")
        self.assertLess(index_other, index_material, "有材质的目标块必须移到段尾")
        self.assertEqual(lines[-1], ";MARK: tail")

        if_indices = [
            index for index, line in enumerate(lines)
            if str(line or "").strip() == condition
        ]
        first_block = _block_between_if_and_endif(lines, if_indices[0])
        second_block = _block_between_if_and_endif(lines, if_indices[1])
        self.assertIn(_mesh("NoMaterialPart"), first_block)
        self.assertNotIn(_mesh("MaterialPart"), first_block)
        self.assertIn(_mesh("MaterialPart"), second_block)
        self.assertNotIn(_mesh("NoMaterialPart"), second_block)

        snapshot = list(lines)
        node._split_mixed_target_if_blocks(lines)
        self.assertEqual(lines, snapshot, "重复执行必须幂等")

    def test_target_unit_without_material_is_not_moved_to_tail(self):
        """顶层单元口径：没有材质行的目标 mesh 不当作目标搬动。"""
        lines = [
            _mesh("MaterialPart"),
            "ps-t0 = Resource_MaterialPart",
            "drawindexed = 1,0,0",
            _mesh("NoMaterialPart"),
            "drawindexed = 2,0,0",
            ";MARK: tail",
        ]
        node = _Node({"MaterialPart", "NoMaterialPart"})
        node._move_top_level_targets_to_end(lines)

        self.assertLess(lines.index(_mesh("NoMaterialPart")), lines.index(_mesh("MaterialPart")))
        self.assertEqual(lines[-1], ";MARK: tail")

        snapshot = list(lines)
        node._move_top_level_targets_to_end(lines)
        self.assertEqual(lines, snapshot, "重复执行必须幂等")

    def test_section_ib_scope_filters_meshes_of_other_ib(self):
        """段级 IB 交叉校验：段名 IB 与 mesh 名 IB 不一致时不算目标。"""
        lines = [
            _mesh("LOD0.bbb22222-200-0.part"),
            "drawindexed = 1,0,0",
        ]
        node = _Node({"LOD0.bbb22222-200-0.part"})
        node._begin_section_ib_scope("[TextureOverride_LOD0.aaa11111_100_0]", lines)
        try:
            self.assertFalse(
                node._unit_is_named_target("LOD0.bbb22222-200-0.part"),
                "段 IB 与 mesh IB 不一致时不能算目标",
            )
            self.assertEqual(node._section_custom_mesh_names(lines), [])
        finally:
            node._end_section_ib_scope()

        node._begin_section_ib_scope("[TextureOverride_LOD0.bbb22222_200_0]", lines)
        try:
            self.assertTrue(node._unit_is_named_target("LOD0.bbb22222-200-0.part"))
            self.assertEqual(
                node._section_custom_mesh_names(lines),
                ["LOD0.bbb22222-200-0.part"],
            )
        finally:
            node._end_section_ib_scope()

    def test_non_target_block_after_target_blocks_moves_to_head(self):
        """段级全局顺序：非目标块在目标块之后时，必须前移到所有目标之前。

        实机回归场景（2026-10-22 4:08 导出）：``左手``（非目标）与目标 ``阴茎.002``
        同在 ``if $swapkey6 == 0`` 块，且该块整体排在 15 个目标块之后 —— 拆块只能
        保证块内顺序，必须再做段级稳定重排。
        """
        lines = [
            "hash = c209c22b",
            "if $swapkey4 == 0",
            _mesh("TargetA"),
            r"Resource\ZZMI\Diffuse = ref Resource-A",
            "drawindexed = 1,0,0",
            "endif",
            "if $swapkey6 == 0",
            _mesh("NonTarget"),
            "drawindexed = 2,0,0",
            "endif",
            ";MARK: tail",
        ]
        node = _Node({"TargetA"})
        _run_reorder_pipeline(node, lines)

        starts = _unit_starts(lines)
        self.assertLess(starts["NonTarget"], starts["TargetA"],
                        "非目标单元必须在所有目标单元之前")
        self.assertEqual(lines[0], "hash = c209c22b", "段首前导内容不得被搬动")
        self.assertEqual(lines[-1], ";MARK: tail")

        snapshot = list(lines)
        _run_reorder_pipeline(node, lines)
        self.assertEqual(lines, snapshot, "段级重排必须幂等")

    def test_mixed_block_split_then_moved_before_all_targets(self):
        """混排块拆出的非目标块，若原位置在目标之后，也要前移到所有目标之前。"""
        lines = [
            "if $swapkey4 == 1",
            _mesh("TargetA"),
            r"Resource\ZZMI\Diffuse = ref Resource-A",
            "drawindexed = 1,0,0",
            "endif",
            "if $swapkey6 == 0",
            _mesh("NonTarget"),
            "drawindexed = 2,0,0",
            _mesh("TargetB"),
            r"Resource\ZZMI\Diffuse = ref Resource-B",
            "drawindexed = 3,0,0",
            "endif",
            ";MARK: tail",
        ]
        node = _Node({"TargetA", "TargetB"})
        _run_reorder_pipeline(node, lines)

        starts = _unit_starts(lines)
        self.assertLess(starts["NonTarget"], starts["TargetA"])
        self.assertLess(starts["NonTarget"], starts["TargetB"])
        self.assertLess(starts["TargetA"], starts["TargetB"],
                        "目标单元之间保持原相对顺序")
        self.assertEqual(lines[0], "if $swapkey6 == 0",
                         "非目标块必须落到段首（段首没有前导内容时）")

        snapshot = list(lines)
        _run_reorder_pipeline(node, lines)
        self.assertEqual(lines, snapshot, "段级重排必须幂等")

    def test_reorder_already_ordered_is_noop(self):
        """已满足「非目标全部在前」时不产生改动（幂等前提）。"""
        lines = [
            _mesh("NonTarget"),
            "drawindexed = 1,0,0",
            _mesh("TargetA"),
            r"Resource\ZZMI\Diffuse = ref Resource-A",
            "drawindexed = 2,0,0",
            ";MARK: tail",
        ]
        node = _Node({"TargetA"})
        snapshot = list(lines)
        self.assertFalse(node._reorder_units_non_target_first(lines))
        self.assertEqual(lines, snapshot)

    def test_reorder_skips_block_with_else(self):
        """含 else 的块：保守跳过整段重排，if/else/endif 结构原样保留。"""
        lines = [
            _mesh("TargetA"),
            r"Resource\ZZMI\Diffuse = ref Resource-A",
            "drawindexed = 1,0,0",
            "if $switch == 1",
            _mesh("NonTarget"),
            "drawindexed = 2,0,0",
            "else",
            "run = CommandListSkinTexture",
            "endif",
            ";MARK: tail",
        ]
        node = _Node({"TargetA"})
        snapshot = list(lines)
        self.assertFalse(node._reorder_units_non_target_first(lines))
        self.assertEqual(lines, snapshot)


if __name__ == "__main__":
    unittest.main()
