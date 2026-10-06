"""headless Blender 冒烟：apply_modifiers_for_object_with_shape_keys 不再依赖选择集。

用法（Windows）::

    "D:\\steam\\steamapps\\common\\Blender\\blender.exe" --background --factory-startup \
        --python tests/blender_smoke_apply_modifiers_with_shape_keys.py

背景：这一版把 ``bpy.ops.object.shape_key_transfer()`` 换成了「把第 i 个键值设 1.0、
其余键 0.0，再 apply_mix 移除形态键」。旧实现只有在「除活动物体外恰好还有 1 个可编辑
网格物体被选中」时才工作，否则抛 RuntimeError（英文原文 ``Expected one other selected
mesh object to copy from``，就是导出中止时看到的那类错误）；真机矩阵实测：只选中活动物体、
或除活动物体外有 2 个以上物体被选中，都会报这一条。

真机必须确认的不变量：

1. 普通带形态键物体 + 修改器仍能应用，键名/顶点数/每个键的形状都保持正确；
2. 场景里有**额外被选中的物体**时照样成功 —— 且那个物体不会被删掉、不会被改；
3. 形态键上的 slider 范围/顶点组限制不影响烘出来的形状（按 value=1.0 烘满）；
4. 连续对多个物体调用（批量导出的循环）不残留副本；
5. 调用后视图层选择集与活动物体还原成调用前的样子。

假 bpy 覆盖不到这些：它们全部取决于真实 operator 的选择集语义。
"""

import importlib.util
import json
import sys
import traceback
import types
from pathlib import Path

import bpy

REPO = Path(__file__).resolve().parents[1]
PKG = "_th4_apply_mod_shapekeys_smoke"
SHIFT = 2.0  # 形态键 i 把顶点整体 +x 平移 i*SHIFT，便于按数值验证"键 i 的形状"
TOL = 1e-4

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append({"name": name, "ok": bool(ok), "detail": str(detail)})
    print("SMOKE_CHECK %s %s -> %s" % ("PASS" if ok else "FAIL", name, detail))


def _install_package(name, path):
    module = types.ModuleType(name)
    module.__path__ = [str(path)]
    sys.modules[name] = module
    return module


_install_package(PKG, REPO)
_install_package(f"{PKG}.utils", REPO / "utils")


def _load(module_name):
    relative = module_name.replace(".", "/")
    spec = importlib.util.spec_from_file_location(f"{PKG}.{module_name}", REPO / f"{relative}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_load("utils.timer_utils")
_shapekey_utils = _load("utils.shapekey_utils")
ShapeKeyUtils = _shapekey_utils.ShapeKeyUtils


def reset_scene():
    bpy.ops.wm.read_factory_settings(use_empty=True)


def make_object(name, key_count=3, with_subsurf=False):
    mesh = bpy.data.meshes.new(f"{name}Mesh")
    mesh.from_pydata([(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0)], [], [(0, 1, 2, 3)])
    mesh.update()
    obj = bpy.data.objects.new(name, mesh)
    bpy.context.scene.collection.objects.link(obj)
    obj.shape_key_add(name="Basis", from_mix=False)
    for index in range(key_count):
        block = obj.shape_key_add(name=f"{name}_K{index + 1}", from_mix=False)
        block.value = 0.0
        for vertex in block.data:
            vertex.co.x += SHIFT * (index + 1)
    if with_subsurf:
        modifier = obj.modifiers.new(name="Subsurf", type='SUBSURF')
        modifier.levels = 1
    return obj


def key_names(obj):
    data = getattr(obj.data, "shape_keys", None)
    return [block.name for block in data.key_blocks] if data else []


def mean_x(obj):
    vertices = obj.data.vertices
    return sum(vertex.co.x for vertex in vertices) / len(vertices)


def key_deltas(obj):
    """每个形态键的顶点平均 x 相对 Basis 的偏移（Basis 本身恒为 0）。"""
    data = getattr(obj.data, "shape_keys", None)
    if not data:
        return {}
    base = sum(vertex.co.x for vertex in data.key_blocks[0].data) / len(data.key_blocks[0].data)
    deltas = {}
    for block in data.key_blocks[1:]:
        value = sum(vertex.co.x for vertex in block.data) / len(block.data)
        deltas[block.name] = value - base
    return deltas


def apply_shapekey_modifiers(obj):
    """照 blueprint/preprocess.py 的调用口径：operator_context + 经典应用路径。"""
    modifier_names = [modifier.name for modifier in obj.modifiers]
    try:
        with ShapeKeyUtils.operator_context(obj):
            return ShapeKeyUtils.apply_modifiers_for_object_with_shape_keys(
                bpy.context, modifier_names, False), None
    except Exception:
        return None, traceback.format_exc()


def expect_deltas(prefix, count):
    return {f"{prefix}_K{index + 1}": SHIFT * (index + 1) for index in range(count)}


def deltas_ok(deltas, expected):
    if set(deltas) != set(expected):
        return False
    return all(abs(deltas[name] - value) < TOL for name, value in expected.items())


def case_single_object():
    reset_scene()
    body = make_object("Body", 4, with_subsurf=True)
    result, tb = apply_shapekey_modifiers(body)
    ok = result is not None and result[0] is True
    check("single_object_applies", ok, "result=%r" % ((result if ok else (tb or "")[-400:]),))
    check("single_object_keys_kept",
          key_names(body) == ["Basis", "Body_K1", "Body_K2", "Body_K3", "Body_K4"],
          repr(key_names(body)))
    check("single_object_vertices", len(body.data.vertices) == 9, "verts=%d" % len(body.data.vertices))
    deltas = key_deltas(body)
    check("single_object_shapes", deltas_ok(deltas, expect_deltas("Body", 4)),
          "deltas=%r" % (deltas,))


def case_extra_selected_object_survives():
    """回归：函数只该动目标物体。

    ``operator_context`` 会先清空选择集，所以"外来选中"不会直接让函数失败；这里钉住的是
    旧实现用 ``bpy.ops.object.delete``（删除所有选中物体）带来的连带删除风险，
    以及旁观物体不影响合并结果。
    """
    reset_scene()
    body = make_object("Body", 3, with_subsurf=True)
    bystander = make_object("Bystander", 2)
    names_before = sorted(obj.name for obj in bpy.context.scene.objects)

    bpy.ops.object.select_all(action='DESELECT')
    body.select_set(True)
    bystander.select_set(True)
    bpy.context.view_layer.objects.active = body

    result, tb = apply_shapekey_modifiers(body)
    ok = result is not None and result[0] is True
    check("extra_selected_applies", ok, "result=%r" % ((result if ok else (tb or "")[-400:]),))
    check("extra_selected_shapes", deltas_ok(key_deltas(body), expect_deltas("Body", 3)),
          "deltas=%r" % (key_deltas(body),))

    remaining = bpy.data.objects.get("Bystander")
    check("bystander_survives", remaining is not None,
          "objects=%r" % sorted(obj.name for obj in bpy.context.scene.objects))
    if remaining is not None:
        check("bystander_keys_untouched",
              key_names(remaining) == ["Basis", "Bystander_K1", "Bystander_K2"],
              repr(key_names(remaining)))
        check("bystander_mesh_untouched", len(remaining.data.vertices) == 4,
              "verts=%d" % len(remaining.data.vertices))
    check("no_extra_objects", sorted(obj.name for obj in bpy.context.scene.objects) == names_before,
          "before=%r after=%r" % (names_before, sorted(obj.name for obj in bpy.context.scene.objects)))


def case_shape_key_limits_ignored():
    """形态键上的顶点组限制/滑块范围不影响烘出来的形状（按 value=1.0 烘满）。"""
    reset_scene()
    body = make_object("Body", 2)
    group = body.vertex_groups.new(name="Tip")
    group.add([3], 1.0, 'REPLACE')
    for index, block in enumerate(body.data.shape_keys.key_blocks):
        if index == 0:
            continue
        block.vertex_group = "Tip"
        block.slider_min = -1.0
        block.slider_max = 0.5
        block.value = 0.5
        block.mute = False

    result, tb = apply_shapekey_modifiers(body)
    ok = result is not None and result[0] is True
    check("key_limits_applies", ok, "result=%r" % ((result if ok else (tb or "")[-400:]),))
    check("key_limits_full_shape", deltas_ok(key_deltas(body), expect_deltas("Body", 2)),
          "deltas=%r" % (key_deltas(body),))


def case_sequence_of_objects():
    """批量导出的循环：连续对多个物体调用不残留副本。"""
    reset_scene()
    body = make_object("Body", 2, with_subsurf=True)
    head = make_object("Head", 3, with_subsurf=True)
    reset_first, tb_first = apply_shapekey_modifiers(body)
    reset_second, tb_second = apply_shapekey_modifiers(head)
    ok = (reset_first is not None and reset_first[0] is True
          and reset_second is not None and reset_second[0] is True)
    check("sequence_applies", ok,
          "first=%r second=%r" % (reset_first if ok else (tb_first or "")[-300:],
                                  reset_second if ok else (tb_second or "")[-300:]))
    check("sequence_no_leftovers",
          sorted(obj.name for obj in bpy.context.scene.objects) == ["Body", "Head"],
          repr(sorted(obj.name for obj in bpy.context.scene.objects)))
    check("sequence_shapes",
          deltas_ok(key_deltas(body), expect_deltas("Body", 2))
          and deltas_ok(key_deltas(head), expect_deltas("Head", 3)),
          "body=%r head=%r" % (key_deltas(body), key_deltas(head)))


def case_selection_restored():
    reset_scene()
    body = make_object("Body", 2, with_subsurf=True)
    other = make_object("Other", 1)

    bpy.ops.object.select_all(action='DESELECT')
    body.select_set(True)
    other.select_set(True)
    bpy.context.view_layer.objects.active = other
    selected_before = {obj.name for obj in bpy.context.view_layer.objects if obj.select_get()}

    result, tb = apply_shapekey_modifiers(body)
    ok = result is not None and result[0] is True
    check("restore_applies", ok, "result=%r" % ((result if ok else (tb or "")[-400:]),))

    selected_after = {obj.name for obj in bpy.context.view_layer.objects if obj.select_get()}
    check("restore_selection", selected_after == selected_before,
          "before=%r after=%r" % (sorted(selected_before), sorted(selected_after)))
    active_after = getattr(bpy.context.view_layer.objects.active, "name", None)
    check("restore_active", active_after == "Other", "active=%r" % active_after)


def main():
    case_single_object()
    case_extra_selected_object_survives()
    case_shape_key_limits_ignored()
    case_sequence_of_objects()
    case_selection_restored()

    failed = [item["name"] for item in RESULTS if not item["ok"]]
    print("SMOKE_RESULT " + json.dumps(
        {"total": len(RESULTS), "failed": failed, "results": RESULTS}, ensure_ascii=False))
    if failed:
        raise SystemExit(1)


main()
