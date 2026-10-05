"""headless Blender 冒烟：工具集顶点组合并（BMTP_OT_MergeVertexGroups）的 bpy 侧行为。

用法（Windows）::

    "D:\\steam\\steamapps\\common\\Blender\\blender.exe" --background --factory-startup \
        --python tests/blender_smoke_merge_vertex_groups_selected.py

假 bpy 无法覆盖、必须真机确认的三条不变量：

1. 合并按「顶点组名称」在各选中物体上分别进行 —— 组名不同/数量不足的物体被跳过而不报错；
2. 只有一个物体拥有被勾选的两个组时，只有它发生变化，其余物体原样保留；
3. 目标名留空时多物体场景**不**把首个物体的 fallback 目标名回写到输入框。
"""

import importlib.util
import json
import sys
import types
from pathlib import Path

import bpy

REPO = Path(__file__).resolve().parents[1]
PKG = "_th4_merge_vg_smoke"


def _install_package(name, path):
    module = types.ModuleType(name)
    module.__path__ = [str(path)]
    sys.modules[name] = module
    return module


_install_package(PKG, REPO)
for _sub in ("utils", "toolkit"):
    _install_package(f"{PKG}.{_sub}", REPO / _sub)


class _Fatal(Exception):
    pass


_format_utils = _install_package(f"{PKG}.utils.format_utils", [])
_format_utils.Fatal = _Fatal


def _load(module_name):
    relative = module_name.replace(".", "/")
    spec = importlib.util.spec_from_file_location(f"{PKG}.{module_name}", REPO / f"{relative}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


vertexgroup_utils = _load("utils.vertexgroup_utils")
weight_tools = _load("toolkit.bmtp_weight_tools")


class _VertexGroupItem(bpy.types.PropertyGroup):
    name: bpy.props.StringProperty()
    index: bpy.props.IntProperty()
    selected: bpy.props.BoolProperty()


class _BMTPProps(bpy.types.PropertyGroup):
    wt_merge_vertex_groups: bpy.props.CollectionProperty(type=_VertexGroupItem)
    wt_merge_vertex_groups_index: bpy.props.IntProperty()
    wt_merge_source_object: bpy.props.PointerProperty(type=bpy.types.Object)
    wt_merge_source_object_name: bpy.props.StringProperty()
    wt_merge_target_name: bpy.props.StringProperty()
    wt_merge_apply_to_selected: bpy.props.BoolProperty(default=True)


def _register_scene_props():
    bpy.utils.register_class(_VertexGroupItem)
    bpy.utils.register_class(_BMTPProps)
    bpy.types.Scene.bmtp_props = bpy.props.PointerProperty(type=_BMTPProps)
    bpy.utils.register_class(weight_tools.BMTP_OT_MergeVertexGroups)
    bpy.utils.register_class(weight_tools.BMTP_OT_RefreshMergeVertexGroups)


# ------------------------------------------------------------------ 场景工具


def _reset_scene():
    bpy.ops.wm.read_factory_settings(use_empty=True)


def _make_object(name, group_specs, count=4):
    vertices = [(float(index), 0.0, 0.0) for index in range(count)]
    mesh = bpy.data.meshes.new(name + "Mesh")
    mesh.from_pydata(vertices, [], [(0, 1, 2), (1, 2, 3)])
    mesh.update()
    obj = bpy.data.objects.new(name, mesh)
    bpy.context.scene.collection.objects.link(obj)
    for group_name, weights in group_specs:
        group = obj.vertex_groups.new(name=group_name)
        for vertex_index, weight in weights.items():
            group.add([vertex_index], weight, "REPLACE")
    return obj


def _read_weights(obj):
    result = {}
    for group in obj.vertex_groups:
        per_vertex = {}
        for vertex in obj.data.vertices:
            try:
                weight = group.weight(vertex.index)
            except RuntimeError:
                continue
            if weight > 0.0:
                per_vertex[vertex.index] = round(weight, 4)
        result[group.name] = per_vertex
    return result


def _prepare(objs, active, checked, apply_to_selected=True, target_name=""):
    for obj in bpy.context.view_layer.objects:
        obj.select_set(False)
    for obj in objs:
        obj.select_set(True)
    bpy.context.view_layer.objects.active = active
    bpy.context.view_layer.update()

    props = bpy.context.scene.bmtp_props
    props.wt_merge_apply_to_selected = apply_to_selected
    props.wt_merge_target_name = target_name
    props.wt_merge_source_object = active
    props.wt_merge_source_object_name = active.name
    props.wt_merge_vertex_groups.clear()
    for name, selected in checked:
        item = props.wt_merge_vertex_groups.add()
        item.name = name
        item.index = 0
        item.selected = selected
    return props


def _listing(props):
    return sorted(item.name for item in props.wt_merge_vertex_groups if item.selected)


# ------------------------------------------------------------------ 用例


def check_merge_across_selection(check):
    _reset_scene()
    body = _make_object(
        "Body",
        [("A", {0: 0.6, 1: 0.2}), ("B", {0: 0.7, 1: 0.1, 2: 0.3}), ("Keep", {3: 0.4})],
    )
    head = _make_object("Head", [("A", {0: 0.9}), ("B", {0: 0.9})])
    hand = _make_object("Hand", [("A", {0: 1.0}), ("Other", {0: 1.0})])
    props = _prepare(
        [body, head, hand],
        body,
        [("A", True), ("B", True), ("Keep", False), ("Other", False)],
    )

    bpy.ops.toolkit.bmtp_merge_vertex_groups()

    check(
        "活动物体按名称合并到自己的 A（并保持组顺序）",
        [group.name for group in body.vertex_groups] == ["A", "Keep"],
        [group.name for group in body.vertex_groups],
    )
    check(
        "权重为各源组之和并按 1.0 截断（顶点 0：0.6+0.7→1.0）",
        _read_weights(body)["A"] == {0: 1.0, 1: 0.3, 2: 0.3},
        _read_weights(body)["A"],
    )
    check(
        "其它选中物体各自按名称合并（Head → 单个 A）",
        [group.name for group in head.vertex_groups] == ["A"]
        and _read_weights(head)["A"] == {0: 1.0},
        _read_weights(head),
    )
    check(
        "只有 1 个匹配组的物体被跳过且原样保留（Hand）",
        sorted(_read_weights(hand)) == ["A", "Other"],
        sorted(_read_weights(hand)),
    )
    check(
        "多物体时目标名留空不会被回写成首个物体的目标组",
        props.wt_merge_target_name == "",
        props.wt_merge_target_name,
    )
    check(
        "合并后重建列表：并集且保留 A 的勾选",
        sorted(item.name for item in props.wt_merge_vertex_groups) == ["A", "Keep", "Other"]
        and _listing(props) == ["A"],
        [(item.name, item.selected) for item in props.wt_merge_vertex_groups],
    )


def check_scope_active_object_only(check):
    _reset_scene()
    body = _make_object("Body", [("A", {0: 0.5}), ("B", {0: 0.5}), ("Keep", {3: 0.4})])
    head = _make_object("Head", [("A", {0: 0.9}), ("B", {0: 0.9})])
    props = _prepare(
        [body, head],
        body,
        [("A", True), ("B", True)],
        apply_to_selected=False,
    )

    bpy.ops.toolkit.bmtp_merge_vertex_groups()

    check(
        "关闭「作用于所有选中物体」时只有活动物体被合并",
        [group.name for group in body.vertex_groups] == ["A", "Keep"]
        and [group.name for group in head.vertex_groups] == ["A", "B"],
        ([group.name for group in body.vertex_groups], [group.name for group in head.vertex_groups]),
    )
    check(
        "单物体时目标名回写到输入框（沿用原有行为）",
        props.wt_merge_target_name == "A",
        props.wt_merge_target_name,
    )


def check_shared_target_name(check):
    _reset_scene()
    body = _make_object("Body", [("A", {0: 0.5}), ("B", {0: 0.5}), ("Keep", {3: 0.4})])
    head = _make_object("Head", [("A", {0: 0.9}), ("B", {0: 0.9})])
    _prepare([body, head], body, [("A", True), ("B", True)], target_name="Merged")

    bpy.ops.toolkit.bmtp_merge_vertex_groups()

    check(
        "指定共用目标名时每个物体各自新建该组（追加在末尾）",
        [group.name for group in body.vertex_groups] == ["Keep", "Merged"]
        and [group.name for group in head.vertex_groups] == ["Merged"],
        ([group.name for group in body.vertex_groups], [group.name for group in head.vertex_groups]),
    )


def check_nothing_to_merge(check):
    _reset_scene()
    body = _make_object("Body", [("A", {0: 0.5}), ("Other", {0: 0.5})])
    head = _make_object("Head", [("A", {0: 0.5}), ("Another", {0: 0.5})])
    _prepare([body, head], body, [("A", True), ("B", True)])

    message = ""
    try:
        bpy.ops.toolkit.bmtp_merge_vertex_groups()
    except RuntimeError as error:
        message = str(error)
    check(
        "没有任何物体可合并时报错中止",
        "没有任何物体完成顶点组合并" in message,
        message.strip(),
    )
    check(
        "报错中止时所有物体原样保留",
        sorted(_read_weights(body)) == ["A", "Other"]
        and sorted(_read_weights(head)) == ["A", "Another"],
        (sorted(_read_weights(body)), sorted(_read_weights(head))),
    )


def check_refresh_across_selection(check):
    _reset_scene()
    body = _make_object("Body", [("A", {0: 0.5}), ("B", {0: 0.5})])
    head = _make_object("Head", [("B", {0: 0.5}), ("C", {0: 0.5})])
    props = _prepare([body, head], body, [])
    props.wt_merge_target_name = "Keep"

    bpy.ops.toolkit.bmtp_refresh_merge_vertex_groups()

    check(
        "刷新按名称合并全部选中物体的顶点组",
        sorted(item.name for item in props.wt_merge_vertex_groups) == ["A", "B", "C"],
        [item.name for item in props.wt_merge_vertex_groups],
    )
    check(
        "刷新时来源（活动物体）未变，保留用户填写的目标名",
        props.wt_merge_target_name == "Keep",
        props.wt_merge_target_name,
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
        check("注册算子与道具", False, f"{type(error).__name__}: {error}")
        print("SMOKE_RESULT " + json.dumps({"failures": failures, "results": results}, ensure_ascii=False))
        raise SystemExit(1)

    for name, func in (
        ("按名称作用于所有选中物体", check_merge_across_selection),
        ("范围限定为活动物体", check_scope_active_object_only),
        ("共用目标名", check_shared_target_name),
        ("无可合并物体的中止路径", check_nothing_to_merge),
        ("刷新跨选择", check_refresh_across_selection),
    ):
        try:
            func(check)
        except Exception as error:
            check(name, False, f"{type(error).__name__}: {error}")

    print("SMOKE_RESULT " + json.dumps({"failures": failures, "results": results}, ensure_ascii=False))
    if failures:
        raise SystemExit(1)


main()
