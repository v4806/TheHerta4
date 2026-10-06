"""headless Blender 冒烟：toolkit/bmtp_shape_key_utils.py 的形态键修改器应用路径。

用法（Windows）::

    "D:\\steam\\steamapps\\common\\Blender\\blender.exe" --background --factory-startup \
        --python tests/blender_smoke_bmtp_apply_modifiers_with_shape_keys.py

这一份实现（BMTP 修改器工具用）同样去掉了 ``bpy.ops.object.shape_key_transfer()``，
换成「第 i 个键值 1.0、其余 0，再 apply_mix 移除形态键」，并把两处
``bpy.ops.object.delete(use_global=False)`` 换成 ``bpy.data.objects.remove()``。

真机必须确认的不变量：

1. 带形态键物体 + 修改器仍能应用，键名/顶点数/每个键的形状正确；
2. 场景里有额外被选中的物体时照样成功，且那个物体不会被删掉、不会被改；
3. 连续对多个物体调用（批量导出的循环）不残留副本。
"""

import importlib.util
import json
import sys
import traceback
import types
from pathlib import Path

import bpy

REPO = Path(__file__).resolve().parents[1]
PKG = "_th4_bmtp_apply_shapekeys_smoke"
SHIFT = 2.0
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
_install_package(f"{PKG}.toolkit", REPO / "toolkit")


def _load(module_name):
    relative = module_name.replace(".", "/")
    spec = importlib.util.spec_from_file_location(f"{PKG}.{module_name}", REPO / f"{relative}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_bmtp = _load("toolkit.bmtp_shape_key_utils")
BMTPShapeKeyUtils = _bmtp.BMTP_ShapeKeyUtils


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


def key_deltas(obj):
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
    modifier_names = [modifier.name for modifier in obj.modifiers]
    bpy.ops.object.select_all(action='DESELECT')
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj
    try:
        return BMTPShapeKeyUtils.apply_modifiers_for_object_with_shape_keys(
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
    check("bmtp_single_applies", ok, "result=%r" % ((result if ok else (tb or "")[-400:]),))
    check("bmtp_single_keys_kept",
          key_names(body) == ["Basis", "Body_K1", "Body_K2", "Body_K3", "Body_K4"],
          repr(key_names(body)))
    check("bmtp_single_vertices", len(body.data.vertices) == 9, "verts=%d" % len(body.data.vertices))
    check("bmtp_single_shapes", deltas_ok(key_deltas(body), expect_deltas("Body", 4)),
          "deltas=%r" % (key_deltas(body),))


def case_extra_selected_object_survives():
    reset_scene()
    body = make_object("Body", 3, with_subsurf=True)
    bystander = make_object("Bystander", 2)
    names_before = sorted(obj.name for obj in bpy.context.scene.objects)

    bpy.ops.object.select_all(action='DESELECT')
    body.select_set(True)
    bystander.select_set(True)
    bpy.context.view_layer.objects.active = body
    modifier_names = [modifier.name for modifier in body.modifiers]
    try:
        result = BMTPShapeKeyUtils.apply_modifiers_for_object_with_shape_keys(
            bpy.context, modifier_names, False)
        tb = None
    except Exception:
        result, tb = None, traceback.format_exc()

    ok = result is not None and result[0] is True
    check("bmtp_extra_selected_applies", ok,
          "result=%r" % ((result if ok else (tb or "")[-400:]),))
    check("bmtp_extra_selected_shapes", deltas_ok(key_deltas(body), expect_deltas("Body", 3)),
          "deltas=%r" % (key_deltas(body),))

    remaining = bpy.data.objects.get("Bystander")
    check("bmtp_bystander_survives", remaining is not None,
          "objects=%r" % sorted(obj.name for obj in bpy.context.scene.objects))
    if remaining is not None:
        check("bmtp_bystander_keys_untouched",
              key_names(remaining) == ["Basis", "Bystander_K1", "Bystander_K2"],
              repr(key_names(remaining)))
    check("bmtp_no_extra_objects",
          sorted(obj.name for obj in bpy.context.scene.objects) == names_before,
          "before=%r after=%r" % (names_before,
                                  sorted(obj.name for obj in bpy.context.scene.objects)))


def case_sequence_of_objects():
    reset_scene()
    body = make_object("Body", 2, with_subsurf=True)
    head = make_object("Head", 3, with_subsurf=True)
    first, tb_first = apply_shapekey_modifiers(body)
    second, tb_second = apply_shapekey_modifiers(head)
    ok = (first is not None and first[0] is True
          and second is not None and second[0] is True)
    check("bmtp_sequence_applies", ok,
          "first=%r second=%r" % (first if ok else (tb_first or "")[-300:],
                                  second if ok else (tb_second or "")[-300:]))
    check("bmtp_sequence_no_leftovers",
          sorted(obj.name for obj in bpy.context.scene.objects) == ["Body", "Head"],
          repr(sorted(obj.name for obj in bpy.context.scene.objects)))
    check("bmtp_sequence_shapes",
          deltas_ok(key_deltas(body), expect_deltas("Body", 2))
          and deltas_ok(key_deltas(head), expect_deltas("Head", 3)),
          "body=%r head=%r" % (key_deltas(body), key_deltas(head)))


def main():
    case_single_object()
    case_extra_selected_object_survives()
    case_sequence_of_objects()

    failed = [item["name"] for item in RESULTS if not item["ok"]]
    print("SMOKE_RESULT " + json.dumps(
        {"total": len(RESULTS), "failed": failed, "results": RESULTS}, ensure_ascii=False))
    if failed:
        raise SystemExit(1)


main()
