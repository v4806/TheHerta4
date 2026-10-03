"""headless Blender 冒烟：工具集权重传递（BMTP_OT_TransferWeights）的 bpy 侧行为。

用法（Windows）::

    "D:\\steam\\steamapps\\common\\Blender\\blender.exe" --background \
        --python tests/blender_smoke_bmtp_weight_transfer.py

解释器内（假 bpy）无法覆盖、必须真机确认的两条不变量：

1. 顶点映射是「最近面 + 面内插值」（``POLYINTERP_NEAREST``），不是最近顶点；
2. 「只传递列表中选中的顶点组」结束后，目标物体上只留下选中的顶点组，
   未选中的既有顶点组权重原样保留（``wt_cleanup`` 关闭时）。
"""

import importlib.util
import json
import sys
import types
from pathlib import Path

import bpy

REPO = Path(__file__).resolve().parents[1]
PKG = "_th4_bmtp_weight_smoke"


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
_vertexgroup_utils = _install_package(f"{PKG}.utils.vertexgroup_utils", [])
_vertexgroup_utils.VertexGroupUtils = types.SimpleNamespace()


def _load(module_name):
    relative = module_name.replace(".", "/")
    spec = importlib.util.spec_from_file_location(f"{PKG}.{module_name}", REPO / f"{relative}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


weight_tools = _load("toolkit.bmtp_weight_tools")


class _VertexGroupItem(bpy.types.PropertyGroup):
    name: bpy.props.StringProperty()
    index: bpy.props.IntProperty()
    selected: bpy.props.BoolProperty()


class _BMTPProps(bpy.types.PropertyGroup):
    wt_source_obj: bpy.props.PointerProperty(type=bpy.types.Object)
    wt_cleanup: bpy.props.BoolProperty(default=True)
    wt_use_selected_groups: bpy.props.BoolProperty(default=False)
    wt_vertex_groups: bpy.props.CollectionProperty(type=_VertexGroupItem)
    wt_use_shapekey_positions: bpy.props.BoolProperty(default=False)
    wt_use_armature_positions: bpy.props.BoolProperty(default=False)


def _register_scene_props():
    bpy.utils.register_class(_VertexGroupItem)
    bpy.utils.register_class(_BMTPProps)
    bpy.types.Scene.bmtp_props = bpy.props.PointerProperty(type=_BMTPProps)
    bpy.utils.register_class(weight_tools.BMTP_OT_TransferWeights)


# ------------------------------------------------------------------ 场景工具


def _reset_scene():
    bpy.ops.wm.read_factory_settings(use_empty=True)


def _make_object(name, vertices, faces):
    mesh = bpy.data.meshes.new(name + "Mesh")
    mesh.from_pydata(vertices, [], faces)
    mesh.update()
    obj = bpy.data.objects.new(name, mesh)
    bpy.context.scene.collection.objects.link(obj)
    return obj


def _set_group(obj, name, weights):
    group = obj.vertex_groups.new(name=name)
    for index, weight in weights.items():
        group.add([index], weight, "REPLACE")
    return group


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


def _prepare(source, target, cleanup, selected_names, use_selected=True):
    for obj in bpy.context.view_layer.objects:
        obj.select_set(False)
    source.select_set(True)
    target.select_set(True)
    bpy.context.view_layer.objects.active = target

    props = bpy.context.scene.bmtp_props
    props.wt_source_obj = source
    props.wt_cleanup = cleanup
    props.wt_use_selected_groups = use_selected
    props.wt_vertex_groups.clear()
    for group in source.vertex_groups:
        item = props.wt_vertex_groups.add()
        item.name = group.name
        item.selected = group.name in selected_names


# ------------------------------------------------------------------ 用例


def check_face_interpolation(check):
    _reset_scene()
    # 源三角形：v0/v1 权重 1.0、v2 权重 0.0。
    source = _make_object(
        "SourceTri", [(-1.0, -1.0, 0.0), (1.0, -1.0, 0.0), (0.0, 2.0, 0.0)], [(0, 1, 2)]
    )
    _set_group(source, "G1", {0: 1.0, 1: 1.0, 2: 0.0})
    # 目标顶点位于重心坐标 (0.45, 0.45, 0.10) 处：面内插值应得 0.9，
    # 最近顶点映射（NEAREST）会得 1.0，据此区分两种粒度。
    target = _make_object("TargetVert", [(0.0, -0.7, 0.0)], [])
    _prepare(source, target, cleanup=True, selected_names=set(), use_selected=False)

    bpy.ops.toolkit.bmtp_transfer_weights()
    weight = _read_weights(target).get("G1", {}).get(0)
    check(
        "顶点映射为最近面 + 面内插值（重心 0.45/0.45/0.10 → 0.9，最近顶点法为 1.0）",
        weight is not None and abs(weight - 0.9) < 0.02,
        weight,
    )


def _quad_scene(cleanup):
    vertices = [(-1.0, -1.0, 0.0), (1.0, -1.0, 0.0), (1.0, 1.0, 0.0), (-1.0, 1.0, 0.0)]
    faces = [(0, 1, 2, 3)]
    source = _make_object("SourceQuad", vertices, faces)
    _set_group(source, "G1", {0: 1.0, 1: 1.0, 2: 1.0, 3: 1.0})
    _set_group(source, "G2", {0: 0.5, 1: 0.2, 2: 0.7, 3: 0.1})
    _set_group(source, "G3", {0: 0.3})

    target = _make_object("TargetQuad", vertices, faces)
    _set_group(target, "KeepMe", {0: 0.4, 1: 0.4, 2: 0.4, 3: 0.4})
    _set_group(target, "G2", {0: 0.9, 1: 0.9, 2: 0.9, 3: 0.9})
    return source, target


def check_selected_groups_keep_others(check):
    _reset_scene()
    source, target = _quad_scene(cleanup=False)
    source_before = _read_weights(source)
    _prepare(source, target, cleanup=False, selected_names={"G2"})

    bpy.ops.toolkit.bmtp_transfer_weights()
    weights = _read_weights(target)
    check(
        "cleanup 关闭：只保留选中的 G2 与目标原有 KeepMe",
        sorted(weights) == ["G2", "KeepMe"],
        sorted(weights),
    )
    check(
        "cleanup 关闭：选中组按源权重整体替换（含覆盖目标原有的 0.9）",
        weights.get("G2") == {0: 0.5, 1: 0.2, 2: 0.7, 3: 0.1},
        weights.get("G2"),
    )
    check(
        "cleanup 关闭：未选中的既有组 KeepMe 权重原样保留",
        weights.get("KeepMe") == {0: 0.4, 1: 0.4, 2: 0.4, 3: 0.4},
        weights.get("KeepMe"),
    )
    check(
        "源物体上被临时剔除的组全部还原且权重不变",
        _read_weights(source) == source_before,
        {"before": source_before, "after": _read_weights(source)},
    )
    check(
        "源物体组顺序：选中组留在原位，被剔除的组按备份顺序追加到末尾",
        [group.name for group in source.vertex_groups] == ["G2", "G1", "G3"],
        [group.name for group in source.vertex_groups],
    )


def check_selected_groups_cleanup(check):
    _reset_scene()
    source, target = _quad_scene(cleanup=True)
    source_before = _read_weights(source)
    _prepare(source, target, cleanup=True, selected_names={"G2"})

    bpy.ops.toolkit.bmtp_transfer_weights()
    weights = _read_weights(target)
    check("cleanup 开启：目标只留下选中的 G2", sorted(weights) == ["G2"], sorted(weights))
    check(
        "cleanup 开启：G2 权重来自源物体",
        weights.get("G2") == {0: 0.5, 1: 0.2, 2: 0.7, 3: 0.1},
        weights.get("G2"),
    )
    check(
        "cleanup 开启：源物体同样被完整还原",
        _read_weights(source) == source_before,
        {"before": source_before, "after": _read_weights(source)},
    )


def check_no_selected_group(check):
    _reset_scene()
    source, target = _quad_scene(cleanup=True)
    _prepare(source, target, cleanup=True, selected_names=set())

    message = ""
    try:
        bpy.ops.toolkit.bmtp_transfer_weights()
    except RuntimeError as error:
        message = str(error)
    weights = _read_weights(target)
    check("一个顶点组都没选时报错中止", "至少选择一个顶点组" in message, message.strip())
    check(
        "报错中止时目标物体未被改动",
        weights.get("KeepMe") == {0: 0.4, 1: 0.4, 2: 0.4, 3: 0.4}
        and sorted(weights) == ["G2", "KeepMe"],
        sorted(weights),
    )


def main():
    results = []
    failures = []

    def check(name, ok, detail):
        results.append({"name": name, "ok": bool(ok), "detail": detail})
        if not ok:
            failures.append(name)
        print("SMOKE_CHECK {} {}".format("PASS" if ok else "FAIL", name))

    try:
        _register_scene_props()
    except Exception as error:
        check("注册算子与道具", False, f"{type(error).__name__}: {error}")
        print("SMOKE_RESULT " + json.dumps({"failures": failures, "results": results}, ensure_ascii=False))
        raise SystemExit(1)

    check(
        "传递映射常量固定为 POLYINTERP_NEAREST",
        weight_tools.WEIGHT_TRANSFER_VERTEX_MAPPING == "POLYINTERP_NEAREST"
        and weight_tools.WEIGHT_TRANSFER_VERTEX_MAPPING != "NEAREST",
        weight_tools.WEIGHT_TRANSFER_VERTEX_MAPPING,
    )

    for name, func in (
        ("面插值粒度", check_face_interpolation),
        ("仅传递选中顶点组（保留其他组）", check_selected_groups_keep_others),
        ("仅传递选中顶点组（清理模式）", check_selected_groups_cleanup),
        ("未选择顶点组的中止路径", check_no_selected_group),
    ):
        try:
            func(check)
        except Exception as error:
            check(name, False, f"{type(error).__name__}: {error}")

    print("SMOKE_RESULT " + json.dumps({"failures": failures, "results": results}, ensure_ascii=False))
    if failures:
        raise SystemExit(1)


main()
