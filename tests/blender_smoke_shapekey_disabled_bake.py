"""headless Blender 冒烟：未勾选形态键烘焙进基态 + 其余键重基。

用法（Windows）::

    "D:\\steam\\steamapps\\common\\Blender\\blender.exe" --background \
        --python tests/blender_smoke_shapekey_disabled_bake.py

语义基准（用户给的例子）：基态 0；形态键 A 左移 1（未勾选、值 0.5）、形态键 B 左移 1
（勾选导出、值 1）。烘焙后基态应为 -0.5，B 的增量仍为 -1，A=0.5 + B=1 时总数仍是 -1.5。
"""

import importlib.util
import json
import sys
import types
from pathlib import Path

import bpy

REPO = Path(__file__).resolve().parents[1]
PKG = "_th4_shapekey_bake_smoke"

_package = types.ModuleType(PKG)
_package.__path__ = [str(REPO)]
sys.modules[PKG] = _package
_utils = types.ModuleType(f"{PKG}.utils")
_utils.__path__ = [str(REPO / "utils")]
sys.modules[f"{PKG}.utils"] = _utils

_format_utils = types.ModuleType(f"{PKG}.utils.format_utils")


class _Fatal(Exception):
    pass


_format_utils.Fatal = _Fatal
sys.modules[f"{PKG}.utils.format_utils"] = _format_utils

_spec = importlib.util.spec_from_file_location(
    f"{PKG}.utils.shapekey_utils", REPO / "utils" / "shapekey_utils.py"
)
_shapekey_utils_module = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _shapekey_utils_module
_spec.loader.exec_module(_shapekey_utils_module)
ShapeKeyUtils = _shapekey_utils_module.ShapeKeyUtils

BASIS = [(0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (1.0, 1.0, 0.0), (0.0, 1.0, 0.0)]


def _make_plane(name):
    mesh = bpy.data.meshes.new(name + "Mesh")
    mesh.from_pydata(BASIS, [], [(0, 1, 2, 3)])
    mesh.update()
    obj = bpy.data.objects.new(name, mesh)
    bpy.context.scene.collection.objects.link(obj)
    # 第一个 shape_key_add 创建的就是 Basis（必须显式建，否则第一个命名键会顶替基态）
    obj.shape_key_add(name="Basis", from_mix=False)
    return obj


def _add_shape_key(obj, name, offset_x, value=0.0, mute=False):
    key_block = obj.shape_key_add(name=name, from_mix=False)
    coords = [(x + offset_x, y, z) for x, y, z in BASIS]
    for point, coord in zip(key_block.data, coords):
        point.co = coord
    key_block.value = value
    key_block.mute = mute
    return key_block


def _evaluated_coords(obj):
    depsgraph = bpy.context.evaluated_depsgraph_get()
    evaluated_obj = obj.evaluated_get(depsgraph)
    evaluated_mesh = evaluated_obj.to_mesh()
    try:
        return [tuple(round(component, 6) for component in vertex.co) for vertex in evaluated_mesh.vertices]
    finally:
        evaluated_obj.to_mesh_clear()


def _delta_of(obj, name):
    key_blocks = obj.data.shape_keys.key_blocks
    basis = key_blocks[0]
    target = key_blocks[name]
    return [
        tuple(round(target.data[index].co[axis] - basis.data[index].co[axis], 6) for axis in range(3))
        for index in range(len(basis.data))
    ]


def main():
    results = []
    failures = []

    def check(name, ok, detail):
        results.append({"name": name, "ok": bool(ok), "detail": detail})
        if not ok:
            failures.append(name)
        print("SMOKE_CHECK {} {}".format("PASS" if ok else "FAIL", name))

    bpy.ops.wm.read_factory_settings(use_empty=True)
    obj = _make_plane("ShapeKeyed")
    _add_shape_key(obj, "A", -1.0, value=0.5)
    _add_shape_key(obj, "B", -1.0, value=1.0)
    _add_shape_key(obj, "C", 2.0, value=0.75, mute=True)

    # 烘焙前：只有未勾选键 A 生效（B=0）时的形状——这就是应当被"固定下来"的静止形状
    key_blocks = obj.data.shape_keys.key_blocks
    key_blocks["B"].value = 0.0
    before_static = _evaluated_coords(obj)
    key_blocks["B"].value = 1.0
    before_delta_b = _delta_of(obj, "B")

    result = ShapeKeyUtils.bake_disabled_shape_keys_to_basis(obj, {"A"})
    check(
        "烘焙识别出未勾选键 A 且读到的是它的原值 0.5",
        result.get("baked_keys") == ["A"] and result.get("baked_values") == {"A": 0.5},
        result,
    )

    baked_basis = _evaluated_coords(obj)
    expected_basis = [(x - 0.5, y, z) for x, y, z in BASIS]
    check("基态网格 = 原基态 + 0.5×A 的位移", baked_basis == expected_basis, baked_basis)
    check("新基态（保留键全为 0）就是烘焙前 A=0.5 的静止形状", baked_basis == before_static, baked_basis)

    check("B 的增量保持不变", _delta_of(obj, "B") == before_delta_b, _delta_of(obj, "B"))

    key_blocks = obj.data.shape_keys.key_blocks
    check(
        "副本上所有形态键值归零（避免收尾重复烘焙）",
        all(float(key_blocks[name].value) == 0.0 for name in ("A", "B", "C")),
        {name: float(key_blocks[name].value) for name in key_blocks.keys()},
    )

    key_blocks["B"].value = 1.0
    total_visible = _evaluated_coords(obj)
    expected_total = [(x - 1.5, y, z) for x, y, z in BASIS]
    check("A 固定进基态后 B=1 的总位移仍是 1.5（-1.5）", total_visible == expected_total, total_visible)

    # mute 的未勾选键凭 Blender 自身求值即不参与；再验一次"没有未勾选键时不动"
    bpy.ops.wm.read_factory_settings(use_empty=True)
    obj2 = _make_plane("ShapeKeyed2")
    _add_shape_key(obj2, "A", -1.0, value=0.5)
    _add_shape_key(obj2, "B", -1.0, value=1.0)
    untouched = _evaluated_coords(obj2)
    empty_result = ShapeKeyUtils.bake_disabled_shape_keys_to_basis(obj2, set())
    check("没有未勾选键时不做任何改动", empty_result.get("baked_keys") == [] and _evaluated_coords(obj2) == untouched, empty_result)

    bpy.ops.wm.read_factory_settings(use_empty=True)
    obj3 = _make_plane("ShapeKeyed3")
    _add_shape_key(obj3, "A", -1.0, value=0.5)
    _add_shape_key(obj3, "B", -1.0, value=1.0)
    obj3.data.shape_keys.key_blocks["A"].mute = True
    muted_untouched = _evaluated_coords(obj3)
    muted_result = ShapeKeyUtils.bake_disabled_shape_keys_to_basis(obj3, {"A"})
    check(
        "mute 的未勾选键不计入基态",
        muted_result.get("baked_keys") == [] and _evaluated_coords(obj3) == muted_untouched,
        muted_result,
    )

    # 导出场景：入口会先把场景值清零，真实值只剩在导出前快照里（value_overrides）
    bpy.ops.wm.read_factory_settings(use_empty=True)
    obj4 = _make_plane("ShapeKeyed4")
    _add_shape_key(obj4, "A", -1.0, value=0.0)   # 模拟「已被导出入口清零」
    _add_shape_key(obj4, "B", -1.0, value=0.0)
    zeroed_basis = _evaluated_coords(obj4)
    zero_result = ShapeKeyUtils.bake_disabled_shape_keys_to_basis(obj4, {"A"})
    check(
        "值的来源是 0 时烘焙不动基态（导出后看不到形变的原因）",
        zero_result.get("delta_applied") is False and _evaluated_coords(obj4) == zeroed_basis,
        zero_result,
    )

    bpy.ops.wm.read_factory_settings(use_empty=True)
    obj5 = _make_plane("ShapeKeyed5")
    _add_shape_key(obj5, "A", -1.0, value=0.0)   # 场景值仍为 0
    _add_shape_key(obj5, "B", -1.0, value=0.0)
    override_result = ShapeKeyUtils.bake_disabled_shape_keys_to_basis(
        obj5, {"A"}, value_overrides={"A": 0.25}
    )
    expected_override = [(x - 0.25, y, z) for x, y, z in BASIS]
    check(
        "改用导出前快照值（0.25）烘焙：基态正确位移",
        override_result.get("baked_values") == {"A": 0.25}
        and _evaluated_coords(obj5) == expected_override,
        {"result": override_result, "coords": _evaluated_coords(obj5)},
    )

    print("SMOKE_RESULT " + json.dumps({"failures": failures, "results": results}, ensure_ascii=False))
    if failures:
        raise SystemExit(1)


main()
