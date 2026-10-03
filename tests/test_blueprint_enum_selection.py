import importlib.util
import sys
import types
import unittest
from pathlib import Path


def _install_module(name, **attrs):
    """安装 Fake 模块到 sys.modules"""
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


PKG = "_blueprint_enum_selection_test_pkg"
for package_name in (PKG, f"{PKG}.blueprint", f"{PKG}.common", f"{PKG}.utils"):
    package = _install_module(package_name)
    package.__path__ = []


class _FakeNodeGroups(list):
    def get(self, name):
        # 真实 bpy 集合接口对非字符串键抛 SystemError
        # （"...get of bpy_prop_collection ... returned a result with an exception set"），
        # 不是返回 None——这正是旧代码在陈旧枚举序号上崩掉的根因，这里如实模拟。
        if not isinstance(name, str):
            raise SystemError(
                "<built-in method get of bpy_prop_collection object> "
                "returned a result with an exception set"
            )
        for node_group in self:
            if getattr(node_group, "name", None) == name:
                return node_group
        return None


class _FakeGlobalProperties(dict):
    def __getattr__(self, name):
        if name in self:
            return self[name]
        raise AttributeError(name)

    def __setattr__(self, name, value):
        self[name] = value


_fake_global_properties = _FakeGlobalProperties()
_fake_bpy = types.SimpleNamespace(
    context=types.SimpleNamespace(
        scene=types.SimpleNamespace(global_properties=_fake_global_properties),
    ),
    data=types.SimpleNamespace(node_groups=_FakeNodeGroups(), objects={}),
    types=types.SimpleNamespace(Object=object),
)
_install_module("bpy", **_fake_bpy.__dict__)
_install_module(f"{PKG}.common.global_config", GlobalConfig=types.SimpleNamespace(get_workspace_name=lambda: ""))
_install_module(f"{PKG}.common.global_properties", GlobalProterties=types.SimpleNamespace())
_install_module(f"{PKG}.common.m_key", M_Key=types.SimpleNamespace())
_install_module(f"{PKG}.common.object_prefix_helper", ObjectPrefixHelper=types.SimpleNamespace())
_install_module(
    f"{PKG}.utils.shapekey_utils",
    ShapeKeyUtils=types.SimpleNamespace(
        iter_exportable_shape_keys=lambda obj: (),
    ),
)


module_path = Path(__file__).resolve().parents[1] / "blueprint" / "export_helper.py"
spec = importlib.util.spec_from_file_location(f"{PKG}.blueprint.export_helper", module_path)
export_helper = importlib.util.module_from_spec(spec)
sys.modules[f"{PKG}.blueprint.export_helper"] = export_helper
spec.loader.exec_module(export_helper)


class BlueprintEnumSelectionTests(unittest.TestCase):
    """测试蓝图枚举选择功能：稳定编号和有效性校验"""

    def setUp(self):
        """每个测试前重置全局属性和蓝图树列表"""
        _fake_global_properties.clear()
        _fake_bpy.data.node_groups[:] = [
            types.SimpleNamespace(name="Alpha", bl_idname="SSMTBlueprintTreeType"),
            types.SimpleNamespace(name="Beta", bl_idname="SSMTBlueprintTreeType"),
        ]

    def test_enum_items_use_stable_numbers(self):
        """测试两次调用 get_blueprint_enum_items 返回一致的列表"""
        first_items = export_helper.BlueprintExportHelper.get_blueprint_enum_items()
        second_items = export_helper.BlueprintExportHelper.get_blueprint_enum_items()

        self.assertEqual(first_items, second_items)
        self.assertTrue(all(len(item) == 5 for item in first_items))

    def test_enum_item_number_equals_position(self):
        """回归：声明序号必须等于列表下标。

        Blender 的动态枚举控件在用户点菜单项时写回的是**项下标**；声明序号一旦
        不是下标（历史上用的是名字哈希），点击就永远对不上任何一项 —— 下拉框
        固定显示第一项、选择不生效、Python 侧读回空串。这条不变量是修好「选不了
        其他蓝图」的关键，禁止改回哈希编号。
        """
        items = export_helper.BlueprintExportHelper.get_blueprint_enum_items()

        for index, item in enumerate(items):
            with self.subTest(index=index, identifier=item[0]):
                self.assertEqual(item[4], index)

    def test_enum_item_number_matches_ui_roundtrip(self):
        """按下标回查必须得到同一项（模拟控件写回下标后的自愈路径）"""
        _fake_bpy.data.node_groups[:] = [
            types.SimpleNamespace(name="Alpha", bl_idname="SSMTBlueprintTreeType"),
            types.SimpleNamespace(name="Beta", bl_idname="SSMTBlueprintTreeType"),
            types.SimpleNamespace(name="Gamma", bl_idname="SSMTBlueprintTreeType"),
        ]
        for index, expected in enumerate(("Alpha", "Beta", "Gamma")):
            with self.subTest(index=index):
                preferred = export_helper.BlueprintExportHelper.get_preferred_blueprint_name(
                    selected_name=index,
                )
                self.assertEqual(preferred, expected)

    def test_ensure_valid_selection_repairs_saved_numeric_value(self):
        """测试 ensure_valid_selected_blueprint_name 将数字值修复为对应名称"""
        beta_number = next(
            item[4]
            for item in export_helper.BlueprintExportHelper.get_blueprint_enum_items()
            if item[0] == "Beta"
        )
        _fake_global_properties["selected_blueprint_name"] = str(beta_number)

        selected = export_helper.BlueprintExportHelper.ensure_valid_selected_blueprint_name()

        self.assertEqual(selected, "Beta")
        self.assertEqual(_fake_global_properties["selected_blueprint_name"], "Beta")

    def test_ensure_valid_selection_replaces_deleted_blueprint(self):
        """测试当已保存的蓝图名称不存在时，自动回退到第一个可用蓝图"""
        _fake_global_properties["selected_blueprint_name"] = "Missing"

        selected = export_helper.BlueprintExportHelper.ensure_valid_selected_blueprint_name()

        self.assertEqual(selected, "Alpha")
        self.assertEqual(_fake_global_properties["selected_blueprint_name"], "Alpha")

    def test_ensure_valid_selection_repairs_unknown_numeric_value(self):
        """陈旧序号（蓝图已被重命名/删除）不能把选择留在失效状态"""
        _fake_global_properties["selected_blueprint_name"] = 1271662080

        selected = export_helper.BlueprintExportHelper.ensure_valid_selected_blueprint_name()

        self.assertEqual(selected, "Alpha")
        self.assertEqual(_fake_global_properties["selected_blueprint_name"], "Alpha")

    def test_legacy_hash_number_migrates_to_blueprint_name(self):
        """升级迁移：老存档里存的「名字哈希序号」要能反查回蓝图名，而不是被丢掉"""
        legacy_beta = export_helper.BlueprintExportHelper._legacy_blueprint_enum_number("Beta")
        _fake_global_properties["selected_blueprint_name"] = legacy_beta

        selected = export_helper.BlueprintExportHelper.ensure_valid_selected_blueprint_name()

        self.assertEqual(selected, "Beta")
        self.assertEqual(_fake_global_properties["selected_blueprint_name"], "Beta")

    def test_legacy_hash_number_resolves_tree(self):
        """哈希序号直接喂给解析函数（老面板/老存档路径）也要命中对应蓝图"""
        legacy_beta = export_helper.BlueprintExportHelper._legacy_blueprint_enum_number("Beta")

        tree = export_helper.BlueprintExportHelper.get_blueprint_tree_by_name(legacy_beta)
        preferred = export_helper.BlueprintExportHelper.get_preferred_blueprint_name(
            selected_name=legacy_beta,
        )

        self.assertIsNotNone(tree)
        self.assertEqual(tree.name, "Beta")
        self.assertEqual(preferred, "Beta")


class BlueprintIdentifierNormalizationTests(unittest.TestCase):
    """陈旧枚举值（int 序号 / 空串 / __NONE__）不得让蓝图解析抛异常或返回错树。"""

    def setUp(self):
        _fake_global_properties.clear()
        _fake_bpy.data.node_groups[:] = [
            types.SimpleNamespace(name="Alpha", bl_idname="SSMTBlueprintTreeType"),
            types.SimpleNamespace(name="Beta", bl_idname="SSMTBlueprintTreeType"),
        ]

    def test_tree_lookup_rejects_numeric_enum_number(self):
        """int 序号喂给 bpy 集合接口会抛 SystemError，helper 必须降级为「找不到」"""
        tree = export_helper.BlueprintExportHelper.get_blueprint_tree_by_name(1271662080)

        self.assertIsNone(tree)

    def test_normalize_rejects_garbage_values(self):
        normalize = export_helper.BlueprintExportHelper.normalize_blueprint_identifier

        self.assertEqual(normalize(None), "")
        self.assertEqual(normalize(True), "")
        self.assertEqual(normalize("Beta"), "Beta")
        self.assertEqual(normalize(1271662080), "")
        self.assertEqual(normalize(3.5), "")
        self.assertEqual(normalize(["Beta"]), "")

    def test_normalize_keeps_blueprint_names_verbatim(self):
        """蓝图名就是 datablock 名：首尾空白（含全角空格）必须原样保留"""
        normalize = export_helper.BlueprintExportHelper.normalize_blueprint_identifier

        self.assertEqual(normalize(" Remilia"), " Remilia")
        self.assertEqual(normalize("Remilia\u3000"), "Remilia\u3000")
        self.assertEqual(normalize("  "), "  ")

    def test_whitespace_named_blueprints_are_not_confused(self):
        """'Alpha' 与 'Alpha　' 是两个 datablock：解析必须精确命中，不能互相顶替"""
        _fake_bpy.data.node_groups[:] = [
            types.SimpleNamespace(name="Alpha", bl_idname="SSMTBlueprintTreeType"),
            types.SimpleNamespace(name="Alpha\u3000", bl_idname="SSMTBlueprintTreeType"),
        ]

        exact = export_helper.BlueprintExportHelper.get_blueprint_tree_by_name("Alpha\u3000")
        self.assertIsNotNone(exact)
        self.assertEqual(exact.name, "Alpha\u3000")

        preferred = export_helper.BlueprintExportHelper.get_preferred_blueprint_name(
            selected_name="Alpha\u3000"
        )
        self.assertEqual(preferred, "Alpha\u3000")

        target = export_helper.BlueprintExportHelper.resolve_blueprint_target_tree("Alpha\u3000")
        self.assertEqual(target.name, "Alpha\u3000")

    def test_valid_whitespace_identifier_is_not_treated_as_stale(self):
        """合法的空白结尾选择不能被当成陈旧值而被改写"""
        _fake_bpy.data.node_groups[:] = [
            types.SimpleNamespace(name="Alpha\u3000", bl_idname="SSMTBlueprintTreeType"),
        ]
        _fake_global_properties["selected_blueprint_name"] = "Alpha\u3000"

        selected = export_helper.BlueprintExportHelper.ensure_valid_selected_blueprint_name()

        self.assertEqual(selected, "Alpha\u3000")
        self.assertEqual(_fake_global_properties["selected_blueprint_name"], "Alpha\u3000")

    def test_whitespace_padded_name_falls_back_to_stripped_match(self):
        """值被外部带上空白、而已有蓝图是不带空白那个名字时，仍要兜回真实蓝图"""
        _fake_global_properties["selected_blueprint_name"] = "  Beta  "

        selected = export_helper.BlueprintExportHelper.ensure_valid_selected_blueprint_name()

        self.assertEqual(selected, "Beta")
        self.assertEqual(_fake_global_properties["selected_blueprint_name"], "Beta")

    def test_preferred_name_accepts_current_numeric_enum_number(self):
        """合法序号（当前列表里的）应反查回蓝图名，而不是掉进回退链"""
        beta_number = next(
            item[4]
            for item in export_helper.BlueprintExportHelper.get_blueprint_enum_items()
            if item[0] == "Beta"
        )

        preferred = export_helper.BlueprintExportHelper.get_preferred_blueprint_name(
            selected_name=beta_number
        )

        self.assertEqual(preferred, "Beta")

    def test_preferred_name_survives_stale_numeric_enum_number(self):
        """陈旧序号不能抛 SystemError（旧代码会让面板绘制整体中断）"""
        preferred = export_helper.BlueprintExportHelper.get_preferred_blueprint_name(
            selected_name=1271662080
        )

        self.assertEqual(preferred, "Alpha")

    def test_resolve_target_tree_prefers_requested_blueprint(self):
        """名字有效时按名字解析（删除/重命名作用于用户选中的那个）"""
        tree = export_helper.BlueprintExportHelper.resolve_blueprint_target_tree("Beta")

        self.assertEqual(tree.name, "Beta")

    def test_resolve_target_tree_falls_back_for_stale_input(self):
        """名字失效/为空/__NONE__ 时回退到已校验的当前选择，而不是直接取消"""
        for stale in ("__NONE__", "", None, 1271662080, "Missing"):
            with self.subTest(stale=stale):
                tree = export_helper.BlueprintExportHelper.resolve_blueprint_target_tree(stale)

                self.assertIsNotNone(tree)
                self.assertEqual(tree.name, "Alpha")

    def test_resolve_target_tree_returns_none_without_blueprints(self):
        """一个蓝图都没有时不能凭工作空间名或上下文凭空造出目标"""
        _fake_bpy.data.node_groups[:] = []

        tree = export_helper.BlueprintExportHelper.resolve_blueprint_target_tree("Missing")

        self.assertIsNone(tree)


if __name__ == "__main__":
    unittest.main()
