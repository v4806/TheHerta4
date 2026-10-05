"""headless Blender 冒烟：形态键统一控制器「仅作用于选中物体」的范围。

用法（Windows）::

    "D:\\steam\\steamapps\\common\\Blender\\blender.exe" --background --factory-startup \
        --python tests/blender_smoke_shape_key_scope.py

假 bpy 覆盖不到、必须真机确认的五条不变量：

1. 驱动集合 = 当前选中的带形态键物体（活动物体排最前），场景里没选中的物体一律不参与；
2. 刷新列表只列选中物体的形态键名 —— 没选中物体独有的键名不得出现；
3. 写值只写选中的物体：没选中的物体即使有同名形态键也保持原值；
4. 一个都没选中时退回活动物体，刷新与写值同步退回；
5. 活动物体不在选中集里（脚本改过选择）时也不能漏掉它。

实现口径提醒：新建形态键的默认 value 是 1.0，脚本统一设成 0.0 起算；
FloatProperty 存 float32（0.6 → 0.6000000238418579），比较一律带容差。
"""

import importlib.util
import json
import sys
import types
from pathlib import Path

import bpy

REPO = Path(__file__).resolve().parents[1]
PKG = "_th4_shape_key_scope_smoke"


def _install_package(name, path):
    module = types.ModuleType(name)
    module.__path__ = [str(path)]
    sys.modules[name] = module
    return module


_install_package(PKG, REPO)
_install_package(f"{PKG}.toolkit", REPO / "toolkit")


def _load(module_name):
    relative = module_name.replace(".", "/")
    spec = importlib.util.spec_from_file_location(f"{PKG}.{module_name}", REPO / f"{relative}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


skc = _load("toolkit.at_shape_key_control")


class _ShapeKeyItem(bpy.types.PropertyGroup):
    name: bpy.props.StringProperty()
    is_group: bpy.props.BoolProperty(default=False)
    label: bpy.props.StringProperty()
    key_names: bpy.props.StringProperty()
    value: bpy.props.FloatProperty(default=0.0)
    group_value: bpy.props.FloatProperty(default=0.0)


class _ATPProps(bpy.types.PropertyGroup):
    shape_key_list: bpy.props.CollectionProperty(type=_ShapeKeyItem)
    shape_key_list_index: bpy.props.IntProperty(default=0)
    sk_use_grouping: bpy.props.BoolProperty(default=True)
    sk_list_rows: bpy.props.IntProperty(default=6)
    sk_value_min: bpy.props.FloatProperty(default=0.0)
    sk_value_max: bpy.props.FloatProperty(default=1.0)


def _register_scene_props():
    # PropertyGroup 必须先 register_class，否则 PointerProperty 报
    # "missing bl_rna attribute from '_RNAMetaPropGroup' instance (may not be registered)"
    bpy.utils.register_class(_ShapeKeyItem)
    bpy.utils.register_class(_ATPProps)
    bpy.types.Scene.atp_props = bpy.props.PointerProperty(type=_ATPProps)


def _reset_scene():
    bpy.ops.wm.read_factory_settings(use_empty=True)


def _make_object(name, key_names):
    mesh = bpy.data.meshes.new(f"{name}Mesh")
    mesh.from_pydata([(0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0)], [], [(0, 1, 2)])
    mesh.update()
    obj = bpy.data.objects.new(name, mesh)
    bpy.context.scene.collection.objects.link(obj)
    obj.shape_key_add(name="Basis", from_mix=False)
    for key_name in key_names:
        key = obj.shape_key_add(name=key_name, from_mix=False)
        # 新建形态键默认 value=1.0，统一从 0 起算，方便断言「没被别人动过」
        key.value = 0.0
    return obj


def _select(objects, active):
    for obj in bpy.context.view_layer.objects:
        obj.select_set(False)
    for obj in objects:
        obj.select_set(True)
    bpy.context.view_layer.objects.active = active


def _close(actual, expected, tolerance=1e-5):
    # FloatProperty 是 float32（0.6 存成 0.6000000238418579），比较必须带容差
    return abs(float(actual) - float(expected)) <= tolerance


def _value(obj, key_name):
    return float(obj.data.shape_keys.key_blocks[key_name].value)


def _driven_names():
    return [obj.name for obj in skc.driving_objects(bpy.context)]


def _listed_names():
    props = bpy.context.scene.atp_props
    return sorted(item.name for item in props.shape_key_list)


def check_write_is_selection_scoped(check):
    """选中两个、第三个留在场景里：只有选中的两个被写。"""
    _reset_scene()
    body = _make_object("Body", ["Smile"])
    head = _make_object("Head", ["Smile"])
    other = _make_object("Other", ["Smile", "Wave"])
    _select([body, head], body)

    check("驱动集合只含选中的物体（活动优先）", _driven_names() == ["Body", "Head"], _driven_names())

    skc.refresh_from_context(bpy.context)
    check("刷新列表只列选中物体的键名", _listed_names() == ["Smile"], _listed_names())

    props = bpy.context.scene.atp_props
    item = next(item for item in props.shape_key_list if item.name == "Smile")
    item.value = 0.6
    skc.on_single_value_update(item, bpy.context)

    check(
        "滑块只写选中的物体",
        _close(_value(body, "Smile"), 0.6) and _close(_value(head, "Smile"), 0.6),
        (_value(body, "Smile"), _value(head, "Smile")),
    )
    check("未选中物体的同名键保持原值", _close(_value(other, "Smile"), 0.0), _value(other, "Smile"))
    check("未选中物体独有的键名不在列表里", "Wave" not in _listed_names(), _listed_names())


def check_falls_back_to_active_object(check):
    """一个都没选中：退回活动物体，刷新与写值都只针对它。"""
    _reset_scene()
    body = _make_object("Body", ["Smile"])
    other = _make_object("Other", ["Smile"])
    _select([], body)

    check("无选择时退回活动物体", _driven_names() == ["Body"], _driven_names())

    skc.refresh_from_context(bpy.context)
    check("退回时列表只列活动物体的键名", _listed_names() == ["Smile"], _listed_names())

    props = bpy.context.scene.atp_props
    item = next(item for item in props.shape_key_list if item.name == "Smile")
    item.value = 0.8
    skc.on_single_value_update(item, bpy.context)

    check("退回时只写活动物体", _close(_value(body, "Smile"), 0.8), _value(body, "Smile"))
    check("退回时其余物体不动", _close(_value(other, "Smile"), 0.0), _value(other, "Smile"))


def check_active_object_outside_selection(check):
    """活动物体不在选中集里（脚本改过选择）时也必须一起驱动。"""
    _reset_scene()
    body = _make_object("Body", ["Smile"])
    head = _make_object("Head", ["Smile"])
    _select([head], body)

    check("活动物体不在选中集时被补进驱动集合", _driven_names() == ["Body", "Head"], _driven_names())


def check_scope_hint(check):
    """面板那行范围提示随选择变化。"""
    _reset_scene()
    body = _make_object("Body", ["Smile"])
    head = _make_object("Head", ["Smile"])
    _select([body, head], body)

    check(
        "选中多个时的范围提示",
        skc.describe_scope(bpy.context) == "作用于选中的 2 个带形态键的物体",
        skc.describe_scope(bpy.context),
    )

    _select([], body)
    check(
        "无选择时的范围提示",
        skc.describe_scope(bpy.context) == "未选中物体：暂时只作用于活动物体 Body",
        skc.describe_scope(bpy.context),
    )


def check_group_row_is_selection_scoped(check):
    """分组行（连续形态键）同样只写选中的物体。"""
    _reset_scene()
    body = _make_object("Body", ["Motion_Key_1", "Motion_Key_2"])
    other = _make_object("Other", ["Motion_Key_1", "Motion_Key_2"])
    _select([body], body)

    skc.refresh_from_context(bpy.context)
    props = bpy.context.scene.atp_props
    group_item = next((item for item in props.shape_key_list if item.is_group), None)
    if group_item is None:
        check("连续形态键被合并为分组行", False, _listed_names())
        return

    skc.apply_group_values(bpy.context, skc.split_key_names(group_item.key_names), 1.0)

    check("分组行只写选中物体", _close(_value(body, "Motion_Key_1"), 1.0), _value(body, "Motion_Key_1"))
    check(
        "分组行不碰未选中物体",
        _close(_value(other, "Motion_Key_1"), 0.0),
        _value(other, "Motion_Key_1"),
    )


def main():
    results = []
    failures = []

    def check(name, ok, detail):
        results.append({"name": name, "ok": bool(ok), "detail": detail})
        if not ok:
            failures.append(name)
        print("SMOKE_CHECK {} {} -> {}".format("PASS" if ok else "FAIL", name, detail))

    try:
        _register_scene_props()
    except Exception as error:
        check("注册场景属性", False, f"{type(error).__name__}: {error}")
        print("SMOKE_RESULT " + json.dumps({"failures": failures, "results": results}, ensure_ascii=False))
        raise SystemExit(1)

    for name, func in (
        ("选中范围内的写值", check_write_is_selection_scoped),
        ("无选择退回活动物体", check_falls_back_to_active_object),
        ("活动物体不在选中集", check_active_object_outside_selection),
        ("范围提示文案", check_scope_hint),
        ("分组行的范围", check_group_row_is_selection_scoped),
    ):
        try:
            func(check)
        except Exception as error:
            check(name, False, f"{type(error).__name__}: {error}")

    print("SMOKE_RESULT " + json.dumps({"failures": failures, "results": results}, ensure_ascii=False))
    if failures:
        raise SystemExit(1)


main()
