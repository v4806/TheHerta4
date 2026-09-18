# -*- coding: utf-8 -*-
"""形态键预分配「已分配就不再重新分配」的回归测试。

设计判据：**第一次刷新分配出的变量名，之后再点多少次刷新都不能变**。
唯一例外是命名规则本身变了（装上/卸下 pypinyin、``cjk_to_ascii`` 升级）。

本文件装载**真实**的 ``blueprint/variable_registry.py`` 与
``blueprint/node_postprocess_shapekey.py``（不是替身），因为要验证的正是
「分配器 + 刷新分支」组合起来的稳定性 —— 用假分配器测不出来。
"""
import importlib.util
import sys
import types
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tests import _real_modules  # noqa: E402

PKG = "_shapekey_prealloc_stability_pkg"
for _package_name in (PKG, f"{PKG}.blueprint", f"{PKG}.common", f"{PKG}.utils"):
    _package = types.ModuleType(_package_name)
    _package.__path__ = []
    sys.modules[_package_name] = _package

_real_modules.register_real_common_modules(f"{PKG}.common")


def _install_module(name, **attrs):
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


class _Tree:
    """假蓝图树。"""

    def __init__(self, name, nodes=()):
        self.name = name
        self.bl_idname = "SSMTBlueprintTreeType"
        self.nodes = list(nodes)


def _build_fake_bpy():
    bpy_module = types.ModuleType("bpy")
    bpy_module.types = types.SimpleNamespace(PropertyGroup=object, Operator=object, UIList=object)
    bpy_module.props = types.SimpleNamespace(
        StringProperty=lambda **_kwargs: None,
        BoolProperty=lambda **_kwargs: None,
        IntProperty=lambda **_kwargs: None,
        EnumProperty=lambda **_kwargs: None,
        CollectionProperty=lambda **_kwargs: None,
    )
    bpy_module.data = types.SimpleNamespace(objects={}, node_groups=[])
    bpy_module.utils = types.SimpleNamespace(
        register_class=lambda _cls: None,
        unregister_class=lambda _cls: None,
    )
    bpy_module.context = types.SimpleNamespace(scene=None)
    return bpy_module


_fake_bpy = _build_fake_bpy()
_SAVED_BPY = sys.modules.get("bpy")
sys.modules["bpy"] = _fake_bpy

try:
    _install_module(
        f"{PKG}.blueprint.node_postprocess_base",
        SSMTNode_PostProcess_Base=type("_Base", (object,), {}),
    )
    _install_module(f"{PKG}.blueprint.direct_export", sync_shapekey_direct_mode=lambda *_a, **_k: None)
    _install_module(
        f"{PKG}.common.mod_path_compat",
        collect_base_position_resource_map=lambda *_a, **_k: {},
        derive_shapekey_base_resource_name=lambda *_a, **_k: "",
        derive_shapekey_freq_resource_name=lambda *_a, **_k: "",
        derive_shapekey_merged_data_resource_name=lambda *_a, **_k: "",
        derive_shapekey_merged_map_resource_name=lambda *_a, **_k: "",
        derive_shapekey_slot_map_resource_name=lambda *_a, **_k: "",
        derive_shapekey_slot_resource_name=lambda *_a, **_k: "",
        ensure_resource_alias_section=lambda *_a, **_k: None,
        resolve_hash_buffer_candidate=lambda *_a, **_k: "",
    )
    _install_module(
        f"{PKG}.common.object_prefix_helper",
        ObjectPrefixHelper=types.SimpleNamespace(
            resolve_source_object_name=lambda name: name,
            extract_prefix_info=lambda name: None,
        ),
    )
    _install_module(
        f"{PKG}.utils.log_utils",
        LOG=types.SimpleNamespace(info=lambda *_a, **_k: None, warning=lambda *_a, **_k: None),
    )
    _install_module(
        f"{PKG}.utils.shapekey_utils",
        ShapeKeyUtils=types.SimpleNamespace(
            is_basis_shape_key_name=lambda name: str(name or "").strip().lower() == "basis",
        ),
    )
    _install_module(
        f"{PKG}.blueprint.export_helper",
        BlueprintExportHelper=types.SimpleNamespace(
            collect_connected_start_nodes=lambda _tree: [],
            get_current_blueprint_model=lambda: None,
            _resolve_shapekey_object_in_scene=lambda name: None,
        ),
    )

    def _load_real(module_name, relative_path):
        spec = importlib.util.spec_from_file_location(module_name, REPO_ROOT / relative_path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        return module

    registry = _load_real(
        f"{PKG}.blueprint.variable_registry", Path("blueprint") / "variable_registry.py"
    )
    module = _load_real(
        f"{PKG}.blueprint.node_postprocess_shapekey",
        Path("blueprint") / "node_postprocess_shapekey.py",
    )
finally:
    if _SAVED_BPY is None:
        sys.modules.pop("bpy", None)
    else:
        sys.modules["bpy"] = _SAVED_BPY


class _Item:
    def __init__(self, name, assigned="", custom=""):
        self.shape_key_name = name
        self.assigned_variable_name = assigned
        self.custom_variable_name = custom
        self.export_enabled = True
        self.drag_zone_id = -1
        self.drag_click_stage = 1
        self.drag_dir_id = "-1"


class _Coll(list):
    def add(self):
        item = _Item("")
        self.append(item)
        return item

    def remove(self, index):
        del self[index]


class _DriverNode:
    """驱动节点：driven_variable 是「引用」（非 ShapeKeySequence 类型）。"""

    bl_idname = "SSMTNode_AnimDriver_ForwardPlay"

    def __init__(self, referenced_name):
        self.name = "Driver"
        self.driven_variable = referenced_name


def _make_node(tree, items):
    node = module.SSMTNode_PostProcess_ShapeKey()
    node.shapekey_variable_items = _Coll(items)
    node.id_data = tree
    tree.nodes.append(node)
    return node


def _snapshot(node):
    return [
        (item.shape_key_name, item.assigned_variable_name, item.custom_variable_name)
        for item in node.shapekey_variable_items
    ]


class PreallocationStabilityTests(unittest.TestCase):
    def setUp(self):
        _fake_bpy.data.node_groups = []
        _fake_bpy.data.objects.clear()
        self._restore_pinyin_state()

    def _restore_pinyin_state(self):
        """用例会改写 pypinyin 探测缓存与假模块，退出时按原值复原。"""
        previous_flag = registry._PINYIN_AVAILABLE
        previous_module = sys.modules.get("pypinyin")
        registry.reset_pinyin_cache()
        sys.modules.pop("pypinyin", None)

        def _restore():
            registry._PINYIN_AVAILABLE = previous_flag
            sys.modules.pop("pypinyin", None)
            if previous_module is not None:
                sys.modules["pypinyin"] = previous_module

        self.addCleanup(_restore)

    def test_repeated_refresh_after_first_allocation_is_noop(self):
        tree = _Tree("Tree")
        node = _make_node(tree, [])
        _fake_bpy.data.node_groups = [tree]

        created, backfilled = node.ensure_shape_key_variable_map(["Smile", "Blink"])
        self.assertEqual((created, backfilled), (2, 0))
        after_first = _snapshot(node)

        for _round in range(3):
            created, backfilled = node.ensure_shape_key_variable_map(["Smile", "Blink"])
            self.assertEqual((created, backfilled), (0, 0))
            self.assertEqual(_snapshot(node), after_first, "重复刷新不得改动已分配变量名")

    def test_refresh_keeps_assigned_name_when_other_tree_frees_the_base(self):
        """回归：别的蓝图树（别的 workspace）条目一增一减，不得改写本树已分配名。"""
        other_tree = _Tree("WorkspaceA")
        other_node = _make_node(other_tree, [])
        tree = _Tree("WorkspaceB")
        node = _make_node(tree, [])

        _fake_bpy.data.node_groups = [other_tree]
        other_node.ensure_shape_key_variable_map(["Smile"])

        _fake_bpy.data.node_groups = [other_tree, tree]
        node.ensure_shape_key_variable_map(["Smile"])
        self.assertEqual(_snapshot(node), [("Smile", "Freq_Smile_1", "Freq_Smile_1")])

        # 用户把 A 工作空间的条目删掉（基名 Freq_Smile 空出来了）
        other_node.shapekey_variable_items.clear()

        for _round in range(2):
            node.ensure_shape_key_variable_map(["Smile"])
            self.assertEqual(
                _snapshot(node), [("Smile", "Freq_Smile_1", "Freq_Smile_1")],
                "已分配的变量名不得因别人释放基名而被改写",
            )

    def test_refresh_keeps_assigned_name_when_another_owner_holds_the_base(self):
        """回归：两个 owner 已同名共存时，刷新不得把其中一个改写成 _1。"""
        tree = _Tree("Tree")
        node_a = _make_node(tree, [_Item("Smile", "Freq_Smile", "Freq_Smile")])
        _make_node(tree, [_Item("Smile", "Freq_Smile", "Freq_Smile")])
        _fake_bpy.data.node_groups = [tree]

        node_a.ensure_shape_key_variable_map(["Smile"])

        self.assertEqual(_snapshot(node_a), [("Smile", "Freq_Smile", "Freq_Smile")])

    def test_refresh_keeps_user_custom_variable(self):
        tree = _Tree("Tree")
        node = _make_node(tree, [_Item("Smile", "Freq_Smile", "MyCustom")])
        _fake_bpy.data.node_groups = [tree]

        node.ensure_shape_key_variable_map(["Smile"])

        self.assertEqual(_snapshot(node), [("Smile", "Freq_Smile", "MyCustom")])

    def test_refresh_still_migrates_when_naming_rule_changes(self):
        """例外：命名规则变了（装上 pypinyin）时仍要刷新成新名字。"""
        tree = _Tree("Tree")
        node = _make_node(tree, [])
        _fake_bpy.data.node_groups = [tree]

        # 先按「无 pypinyin」分配（中文回落到 uXXXX）
        registry._PINYIN_AVAILABLE = False
        node.ensure_shape_key_variable_map(["摇摆_001"])
        self.assertEqual(_snapshot(node), [("摇摆_001", "Freq_u6447u6446_001", "Freq_u6447u6446_001")])

        # 装上 pypinyin（用假模块）后刷新：允许按新规则刷新
        fake_pypinyin = types.ModuleType("pypinyin")
        fake_pypinyin.lazy_pinyin = lambda char: {
            "摇": ["yaobai"],
            "摆": ["bai"],
        }.get(char, [char])
        sys.modules["pypinyin"] = fake_pypinyin
        registry._PINYIN_AVAILABLE = True

        node.ensure_shape_key_variable_map(["摇摆_001"])

        self.assertEqual(
            _snapshot(node),
            [("摇摆_001", "Freq_yaobaibai_001", "Freq_yaobaibai_001")],
            "命名规则变化时必须刷新（pypinyin 迁移不能被稳定性修复挡住）",
        )


class HealForkedNamesTests(unittest.TestCase):
    """导出期自愈：只有基名确实被引用时才把 基名_N 拉回基名。"""

    def setUp(self):
        _fake_bpy.data.node_groups = []
        registry.reset_pinyin_cache()
        sys.modules.pop("pypinyin", None)

    def test_heal_skips_when_base_is_not_referenced(self):
        tree = _Tree("Tree")
        node = _make_node(tree, [_Item("Smile", "Freq_Smile_1", "Freq_Smile_1")])
        _fake_bpy.data.node_groups = [tree]

        changed = node.heal_forked_shape_key_variable_names()

        self.assertEqual(changed, [])
        self.assertEqual(_snapshot(node), [("Smile", "Freq_Smile_1", "Freq_Smile_1")])

    def test_heal_ignores_references_from_other_blueprint_trees(self):
        """回归：别的蓝图树的引用名不得决定本树的变量名（自愈必须按树判定）。"""
        other_tree = _Tree("WorkspaceA")
        other_tree.nodes.append(_DriverNode("Freq_Smile"))
        tree = _Tree("WorkspaceB")
        node = _make_node(tree, [_Item("Smile", "Freq_Smile_1", "Freq_Smile_1")])
        _fake_bpy.data.node_groups = [other_tree, tree]

        changed = node.heal_forked_shape_key_variable_names()

        self.assertEqual(changed, [], "跨树引用不该触发本树自愈")
        self.assertEqual(_snapshot(node), [("Smile", "Freq_Smile_1", "Freq_Smile_1")])

    def test_heal_pulls_back_when_base_is_referenced(self):
        tree = _Tree("Tree")
        node = _make_node(tree, [_Item("Smile", "Freq_Smile_1", "Freq_Smile_1")])
        tree.nodes.append(_DriverNode("Freq_Smile"))
        _fake_bpy.data.node_groups = [tree]

        changed = node.heal_forked_shape_key_variable_names()

        self.assertEqual(changed, [("Smile", "Freq_Smile_1", "Freq_Smile")])
        self.assertEqual(_snapshot(node), [("Smile", "Freq_Smile", "Freq_Smile")])

    def test_heal_keeps_user_custom_variable(self):
        tree = _Tree("Tree")
        node = _make_node(tree, [_Item("Smile", "Freq_Smile_1", "MyCustom")])
        tree.nodes.append(_DriverNode("Freq_Smile"))
        _fake_bpy.data.node_groups = [tree]

        node.heal_forked_shape_key_variable_names()

        self.assertEqual(_snapshot(node), [("Smile", "Freq_Smile", "MyCustom")])


class RegistryHelperTests(unittest.TestCase):
    def setUp(self):
        _fake_bpy.data.node_groups = []
        registry.reset_pinyin_cache()
        sys.modules.pop("pypinyin", None)

    def test_base_variable_name_matches_allocator_on_free_name(self):
        tree = _Tree("Tree")
        _fake_bpy.data.node_groups = [tree]

        base = registry.shape_key_base_variable_name("Smile")

        self.assertEqual(base, "Freq_Smile")
        self.assertEqual(
            registry.allocate_shape_key_variable_name("Smile"),
            base,
            "基名必须等于「基名空闲时分配器的输出」，否则刷新分支的判据会失真",
        )

    def test_base_variable_name_is_stable_without_pinyin(self):
        registry._PINYIN_AVAILABLE = False

        self.assertEqual(
            registry.shape_key_base_variable_name("摇摆_001"), "Freq_u6447u6446_001"
        )

    def test_referenced_names_do_not_include_owner_declarations(self):
        tree = _Tree("Tree")
        shapekey_node = _make_node(tree, [_Item("Smile", "Freq_Smile", "Freq_Smile")])
        swap_node = types.SimpleNamespace(
            bl_idname="SSMTNode_ObjectSwap",
            name="Swap",
            custom_var_name="",
            assigned_variable_name="swapkey0",
        )
        tree.nodes.append(swap_node)
        tree.nodes.append(_DriverNode("Freq_Smile"))
        _fake_bpy.data.node_groups = [tree]

        referenced = registry.get_referenced_variable_names()

        self.assertIn("Freq_Smile", referenced)
        self.assertNotIn("swapkey0", referenced, "owner 自己声明的名字不算引用")
        self.assertNotIn("Freq_Smile_1", referenced)
        self.assertIsNotNone(shapekey_node)


if __name__ == "__main__":
    unittest.main()
