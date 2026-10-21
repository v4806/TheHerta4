"""P1c 等价性：`_collect_drawib_referenced_bone_ids` 新旧实现逐元素对照。

被测：`ui/universal/zzmi.py::ExportZZMI._collect_drawib_referenced_bone_ids`
（P1c 前：每次调用逐顶点逐组元素做 `len(groups)` / `groups[i].name` / `str` /
`isdigit` / `int`；P1c 后：组映射每对象构建一次 + 按 DrawIB 缓存 + 结构签名失效）。

本文件内联**旧实现**逐字拷贝（`_legacy_collect`），用 fake 的 drawib / submesh /
draw_call / Blender 对象断言两者输出完全一致：
- 集合相等 + `list(...)` 迭代顺序相等（调用方会用 set 差集 / `sorted` / 直接迭代）；
- 覆盖空数据、重复 ID、多 DrawIB、跨 drawib 隔离、占位 stub、取不到对象、
  `get_blender_obj_name()` 抛异常、权重 0/负、越界 / 负 group 下标、骨骼 id 0
  （懒解析哨兵 `None` 不能与合法 id 0 混淆）、非数字组名、病态数字名（`"²"`：
  `isdigit()` 为真但 `int()` 抛 ValueError，新旧必须同样抛）。
"""
import importlib.util
import sys
import time
import types
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

_real_modules_spec = importlib.util.spec_from_file_location(
    "zzmi_bone_ids_equiv_real_modules", REPO_ROOT / "tests" / "_real_modules.py"
)
_real_modules = importlib.util.module_from_spec(_real_modules_spec)
sys.modules[_real_modules_spec.name] = _real_modules
_real_modules_spec.loader.exec_module(_real_modules)

PKG = "zzmi_bone_ids_equiv_test_pkg"


def _install_module(name, **attrs):
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


for package_name in (
    PKG,
    f"{PKG}.ui",
    f"{PKG}.ui.universal",
    f"{PKG}.common",
    f"{PKG}.utils",
):
    _package = _install_module(package_name)
    _package.__path__ = []

_real_modules.register_real_common_modules(f"{PKG}.common")


def _load_real_module(qualname, relpath):
    path = REPO_ROOT / relpath
    spec = importlib.util.spec_from_file_location(qualname, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[qualname] = module
    spec.loader.exec_module(module)
    return module


class _FakeObjectRegistry:
    def __init__(self):
        self._items = {}

    def add(self, obj):
        self._items[obj.name] = obj
        return obj

    def new(self, name, object_data=None):
        obj = _FakeObject(name=name)
        obj.data = object_data
        return self.add(obj)

    def get(self, name):
        return self._items.get(str(name))

    def remove(self, obj, do_unlink=False):
        self._items.pop(getattr(obj, "name", ""), None)

    def __iter__(self):
        return iter(list(self._items.values()))


_FAKE_BPY_DATA = types.SimpleNamespace(objects=_FakeObjectRegistry(), meshes=None)
_install_module(
    "bpy",
    data=_FAKE_BPY_DATA,
    context=types.SimpleNamespace(
        collection=types.SimpleNamespace(
            objects=types.SimpleNamespace(link=lambda _obj: None)
        )
    ),
)

_load_real_module(f"{PKG}.utils.json_utils", "utils/json_utils.py")
_load_real_module(f"{PKG}.utils.tbn_codec", "utils/tbn_codec.py")
_load_real_module(f"{PKG}.utils.format_utils", "utils/format_utils.py")
_load_real_module(f"{PKG}.utils.ssmt_error_utils", "utils/ssmt_error_utils.py")
_load_real_module(f"{PKG}.common.m_key", "common/m_key.py")
_load_real_module(
    f"{PKG}.common.object_prefix_helper", "common/object_prefix_helper.py"
)
_load_real_module(f"{PKG}.common.draw_call_model", "common/draw_call_model.py")

_install_module(f"{PKG}.utils.timer_utils", TimerUtils=types.SimpleNamespace())
_install_module(
    f"{PKG}.common.global_config",
    GlobalConfig=types.SimpleNamespace(
        path_generatemod_buffer_folder=lambda: "",
        path_generate_mod_folder=lambda: "",
        get_workspace_name=lambda: "BoneIdsEquivTest",
        path_workspace_folder=lambda: "",
    ),
)
_install_module(
    f"{PKG}.common.global_properties",
    GlobalProterties=types.SimpleNamespace(
        import_merged_vgmap=lambda: True,
        forbid_auto_texture_ini=lambda: False,
        zzz_use_slot_fix=lambda: False,
        zzmi_merged_redirect_enabled=lambda: False,
    ),
)
_install_module(
    f"{PKG}.common.global_key_count_helper",
    GlobalKeyCountHelper=types.SimpleNamespace(generated_mod_number=0),
)
_install_module(
    f"{PKG}.common.m_ini_builder",
    M_IniBuilder=type("M_IniBuilder", (), {}),
    M_IniSection=type("M_IniSection", (), {}),
    M_SectionType=types.SimpleNamespace(),
)
_install_module(f"{PKG}.common.m_ini_helper", M_IniHelper=types.SimpleNamespace())
_install_module(
    f"{PKG}.common.m_ini_helper_gui", M_IniHelperGUI=types.SimpleNamespace()
)
_install_module(
    f"{PKG}.ui.universal.unity",
    ExportUnity=type("ExportUnity", (), {"__init__": lambda self, model: None}),
)

_zzmi_path = REPO_ROOT / "ui" / "universal" / "zzmi.py"
_zzmi_spec = importlib.util.spec_from_file_location(
    f"{PKG}.ui.universal.zzmi", _zzmi_path
)
_zzmi_module = importlib.util.module_from_spec(_zzmi_spec)
sys.modules[_zzmi_spec.name] = _zzmi_module
_zzmi_spec.loader.exec_module(_zzmi_module)

ExportZZMI = _zzmi_module.ExportZZMI
_BPY_DATA = _zzmi_module.bpy.data


# ---------------------------------------------------------------------------
# fake 数据模型
# ---------------------------------------------------------------------------


class _FakeObject:
    """最小 Blender 对象替身（`get()` 支持 ZZMI_STUB 账本）。"""

    def __init__(self, name, group_names=None, vertices=None, stub=False,
                 broken=None):
        self.name = name
        self._custom = {"ZZMI_STUB": 1} if stub else {}
        self.broken = broken
        if broken == "no_data":
            self.data = None
            self.vertex_groups = None
            return
        if broken == "no_vertices":
            self.data = types.SimpleNamespace()
            self.vertex_groups = [
                types.SimpleNamespace(name=name) for name in (group_names or [])
            ]
            return
        if broken == "no_groups":
            self.data = types.SimpleNamespace(vertices=vertices or [])
            self.vertex_groups = None
            return
        self.vertex_groups = [
            types.SimpleNamespace(name=name) for name in (group_names or [])
        ]
        self.data = types.SimpleNamespace(vertices=vertices or [])

    def get(self, key, default=None):
        return self._custom.get(key, default)


def _vertex(*pairs):
    return types.SimpleNamespace(
        groups=[
            types.SimpleNamespace(group=group, weight=weight)
            for group, weight in pairs
        ]
    )


def _draw_call(obj_name, raises=False):
    def get_blender_obj_name():
        if raises:
            raise RuntimeError("boom")
        return obj_name

    return types.SimpleNamespace(get_blender_obj_name=get_blender_obj_name)


def _drawib(draw_ib, *draw_calls, submeshes=None):
    if submeshes is None:
        submeshes = [
            types.SimpleNamespace(drawcall_model_list=list(draw_calls))
        ]
    return types.SimpleNamespace(draw_ib=draw_ib, submesh_model_list=submeshes)


def _make_host(drawib_models):
    host = types.SimpleNamespace(drawib_model_list=drawib_models)
    for method_name in (
        "_collect_drawib_referenced_bone_ids",
        "_drawib_bone_refs_signature",
        "_scan_drawib_bone_refs",
        "_iter_drawib_bone_ref_objects",
    ):
        setattr(
            host,
            method_name,
            types.MethodType(getattr(ExportZZMI, method_name), host),
        )
    return host


def _legacy_collect(host, draw_ib):
    """P1c 之前的实现，逐字拷贝（只把模块全局 `bpy` 换成测试的 fake data）。"""
    used = set()
    for drawib_model in host.drawib_model_list:
        if drawib_model.draw_ib != draw_ib:
            continue
        for submesh_model in drawib_model.submesh_model_list:
            for draw_call in submesh_model.drawcall_model_list:
                try:
                    obj_name = draw_call.get_blender_obj_name()
                except Exception:
                    continue
                obj = _BPY_DATA.objects.get(obj_name) if obj_name else None
                if obj is None or obj.get("ZZMI_STUB"):
                    continue
                mesh = getattr(obj, "data", None)
                vertices = getattr(mesh, "vertices", None)
                groups = getattr(obj, "vertex_groups", None)
                if vertices is None or groups is None:
                    continue
                for vertex in vertices:
                    for group_elem in vertex.groups:
                        if group_elem.weight <= 0:
                            continue
                        if group_elem.group >= len(groups):
                            continue
                        name = str(groups[group_elem.group].name)
                        if not name.isdigit():
                            continue
                        used.add(int(name))
    return used


class _BoneIdsEquivBase(unittest.TestCase):
    def setUp(self):
        _BPY_DATA.objects = _FakeObjectRegistry()

    def _register(self, *objects):
        for obj in objects:
            _BPY_DATA.objects.add(obj)
        return objects

    def _assert_equiv(self, host, draw_ib):
        expected = _legacy_collect(host, draw_ib)
        actual = host._collect_drawib_referenced_bone_ids(draw_ib)
        self.assertEqual(expected, actual)
        # 调用方会直接迭代 / sorted / 做差集：集合语义相同且迭代顺序相同。
        self.assertEqual(list(expected), list(actual))
        return expected


# ---------------------------------------------------------------------------
# 用例
# ---------------------------------------------------------------------------


class TestBoneIdsEquivalence(_BoneIdsEquivBase):
    def test_empty_drawib_model_list(self):
        host = _make_host([])
        self._assert_equiv(host, "aaaaaaaa")

    def test_missing_drawib(self):
        self._register(
            _FakeObject("obj_a", group_names=["1"], vertices=[_vertex((0, 1.0))])
        )
        host = _make_host([_drawib("bbbbbbbb", _draw_call("obj_a"))])
        self._assert_equiv(host, "cccccccc")

    def test_duplicate_ids_and_weight_filters(self):
        obj = _FakeObject(
            "obj_dup",
            group_names=["10", "11", "12", "bone", "13"],
            vertices=[
                _vertex((0, 1.0), (1, 0.5)),      # 10, 11
                _vertex((0, 0.0), (2, 0.0)),      # 权重 0 → 不计
                _vertex((2, -1.0), (3, 1.0)),     # 负权重 + 非数字组名
                _vertex((0, 1.0), (0, 1.0)),      # 重复 10
                _vertex((4, 1.0)),                # 13
            ],
        )
        self._register(obj)
        host = _make_host([_drawib("dddddddd", _draw_call("obj_dup"))])
        expected = self._assert_equiv(host, "dddddddd")
        self.assertEqual(expected, {10, 11, 13})

    def test_skip_stub_missing_and_broken_objects(self):
        stub = _FakeObject(
            "obj_stub", group_names=["7"], vertices=[_vertex((0, 1.0))], stub=True
        )
        no_data = _FakeObject("obj_no_data", broken="no_data")
        no_vertices = _FakeObject(
            "obj_no_vertices", group_names=["8"], broken="no_vertices"
        )
        no_groups = _FakeObject(
            "obj_no_groups", vertices=[_vertex((0, 1.0))], broken="no_groups"
        )
        good = _FakeObject(
            "obj_good", group_names=["20"], vertices=[_vertex((0, 1.0))]
        )
        self._register(stub, no_data, no_vertices, no_groups, good)
        host = _make_host([
            _drawib(
                "eeeeeeee",
                _draw_call("obj_stub"),
                _draw_call("obj_no_data"),
                _draw_call("obj_no_vertices"),
                _draw_call("obj_no_groups"),
                _draw_call("obj_missing"),
                _draw_call("", raises=False),
                _draw_call("obj_good"),
                _draw_call("obj_good", raises=True),
            )
        ])
        expected = self._assert_equiv(host, "eeeeeeee")
        self.assertEqual(expected, {20})

    def test_multiple_drawibs_are_isolated(self):
        self._register(
            _FakeObject("obj_1", group_names=["1"], vertices=[_vertex((0, 1.0))]),
            _FakeObject("obj_2", group_names=["2"], vertices=[_vertex((0, 1.0))]),
            _FakeObject("obj_3", group_names=["3"], vertices=[_vertex((0, 1.0))]),
        )
        host = _make_host([
            _drawib("ib_one", _draw_call("obj_1")),
            _drawib("ib_two", _draw_call("obj_2")),
            _drawib("ib_one", _draw_call("obj_3")),  # 同 DrawIB 多个 drawib_model
        ])
        self.assertEqual(self._assert_equiv(host, "ib_one"), {1, 3})
        self.assertEqual(self._assert_equiv(host, "ib_two"), {2})

    def test_multiple_submeshes_and_drawcalls(self):
        self._register(
            _FakeObject("obj_a", group_names=["4"], vertices=[_vertex((0, 1.0))]),
            _FakeObject("obj_b", group_names=["5"], vertices=[_vertex((0, 1.0))]),
            _FakeObject("obj_c", group_names=["6"], vertices=[_vertex((0, 1.0))]),
        )
        submesh_one = types.SimpleNamespace(
            drawcall_model_list=[_draw_call("obj_a"), _draw_call("obj_b")]
        )
        submesh_two = types.SimpleNamespace(
            drawcall_model_list=[_draw_call("obj_c")]
        )
        host = _make_host([
            _drawib("ib_sub", submeshes=[submesh_one, submesh_two])
        ])
        self.assertEqual(self._assert_equiv(host, "ib_sub"), {4, 5, 6})

    def test_out_of_range_and_negative_group_index(self):
        obj = _FakeObject(
            "obj_idx",
            group_names=["31", "32"],
            vertices=[
                _vertex((2, 1.0)),    # 越界（== len）→ 跳过
                _vertex((9, 1.0)),    # 越界 → 跳过
                _vertex((-1, 1.0)),   # 负索引：旧实现取 groups[-1] = "32"
                _vertex((-2, 1.0)),   # 负索引：取 groups[-2] = "31"
            ],
        )
        self._register(obj)
        host = _make_host([_drawib("ib_idx", _draw_call("obj_idx"))])
        expected = self._assert_equiv(host, "ib_idx")
        self.assertEqual(expected, {31, 32})

    def test_bone_id_zero_is_kept(self):
        self._register(
            _FakeObject("obj_zero", group_names=["0"], vertices=[_vertex((0, 1.0))])
        )
        host = _make_host([_drawib("ib_zero", _draw_call("obj_zero"))])
        self.assertEqual(self._assert_equiv(host, "ib_zero"), {0})

    def test_non_ascii_digit_group_name_raises_like_legacy(self):
        # "²".isdigit() is True 但 int("²") 抛 ValueError：旧实现会在第一次被
        # 权重>0 的顶点引用时抛出，新实现的懒解析必须同点抛出。
        self._register(
            _FakeObject(
                "obj_sup", group_names=["\u00b2"], vertices=[_vertex((0, 1.0))]
            )
        )
        host = _make_host([_drawib("ib_sup", _draw_call("obj_sup"))])
        with self.assertRaises(ValueError):
            _legacy_collect(host, "ib_sup")
        with self.assertRaises(ValueError):
            host._collect_drawib_referenced_bone_ids("ib_sup")

    def test_non_ascii_digit_name_unreferenced_does_not_raise(self):
        # 没有任何权重>0 的顶点引用病态组名 → 旧实现不解析也不抛。
        self._register(
            _FakeObject(
                "obj_sup2",
                group_names=["\u00b2", "1"],
                vertices=[_vertex((1, 1.0))],
            )
        )
        host = _make_host([_drawib("ib_sup2", _draw_call("obj_sup2"))])
        self.assertEqual(self._assert_equiv(host, "ib_sup2"), {1})

    def test_group_name_with_spaces_is_not_digit(self):
        # 与 `_collect_used_group_ids`（会 strip）不同，本函数旧实现不 strip。
        self._register(
            _FakeObject(
                "obj_space",
                group_names=[" 5", "5 ", "6"],
                vertices=[_vertex((0, 1.0), (1, 1.0), (2, 1.0))],
            )
        )
        host = _make_host([_drawib("ib_space", _draw_call("obj_space"))])
        self.assertEqual(self._assert_equiv(host, "ib_space"), {6})


class TestBoneIdsCache(_BoneIdsEquivBase):
    def test_cache_hit_returns_equal_set_and_copy(self):
        self._register(
            _FakeObject(
                "obj_cache", group_names=["1", "2"], vertices=[_vertex((0, 1.0))]
            )
        )
        host = _make_host([_drawib("ib_cache", _draw_call("obj_cache"))])
        first = host._collect_drawib_referenced_bone_ids("ib_cache")
        second = host._collect_drawib_referenced_bone_ids("ib_cache")
        self.assertEqual(first, second)
        self.assertEqual(first, {1})
        # 返回值是副本：调用方改返回值不能污染缓存。
        second.add(999)
        third = host._collect_drawib_referenced_bone_ids("ib_cache")
        self.assertEqual(third, {1})

    def test_signature_invalidation_on_group_rename(self):
        obj = _FakeObject(
            "obj_rename", group_names=["1"], vertices=[_vertex((0, 1.0))]
        )
        self._register(obj)
        host = _make_host([_drawib("ib_rename", _draw_call("obj_rename"))])
        self.assertEqual(
            host._collect_drawib_referenced_bone_ids("ib_rename"), {1}
        )
        obj.vertex_groups[0].name = "42"
        self.assertEqual(
            self._assert_equiv(host, "ib_rename"), {42}
        )

    def test_signature_invalidation_on_group_count_and_vertex_count(self):
        obj = _FakeObject(
            "obj_count", group_names=["1"], vertices=[_vertex((0, 1.0))]
        )
        self._register(obj)
        host = _make_host([_drawib("ib_count", _draw_call("obj_count"))])
        self.assertEqual(host._collect_drawib_referenced_bone_ids("ib_count"), {1})
        # 追加一个数字组 → 签名（组数 / 组名元组）变化 → 重算。
        obj.vertex_groups.append(types.SimpleNamespace(name="77"))
        obj.data.vertices.append(_vertex((1, 1.0)))
        self.assertEqual(host._collect_drawib_referenced_bone_ids("ib_count"), {1, 77})


class TestBoneIdsLargeData(_BoneIdsEquivBase):
    def _build_large(self, object_count=60, vertex_count=200, group_count=6):
        objects = []
        for obj_index in range(object_count):
            group_names = [
                str(1000 + obj_index * 10 + group_index)
                if group_index % 2 == 0
                else f"bone_{obj_index}_{group_index}"
                for group_index in range(group_count)
            ]
            vertices = []
            for vertex_index in range(vertex_count):
                pairs = [
                    (group_index, 1.0 if vertex_index % 3 else 0.0)
                    for group_index in range(group_count)
                ]
                pairs.append((group_count, 1.0))  # 越界 group
                pairs.append((0, -0.5))           # 负权重
                vertices.append(_vertex(*pairs))
            objects.append(
                _FakeObject(
                    f"obj_large_{obj_index}",
                    group_names=group_names,
                    vertices=vertices,
                )
            )
        return self._register(*objects)

    def test_large_dataset_matches_legacy_and_is_not_slower(self):
        objects = self._build_large()
        host = _make_host([
            _drawib(
                "ib_large",
                *[_draw_call(obj.name) for obj in objects],
            )
        ])
        start = time.perf_counter()
        expected = _legacy_collect(host, "ib_large")
        legacy_seconds = time.perf_counter() - start

        start = time.perf_counter()
        actual = host._collect_drawib_referenced_bone_ids("ib_large")
        new_seconds = time.perf_counter() - start

        self.assertEqual(expected, actual)
        self.assertEqual(list(expected), list(actual))
        self.assertEqual(len(expected), len(objects) * 3)
        sys.stdout.write(
            "\n[P1c-bench] large dataset ({} objects x 200 verts x 6 elems): "
            "legacy {:.4f}s -> new {:.4f}s\n".format(
                len(objects), legacy_seconds, new_seconds
            )
        )
        # 宽松上限：只防"新实现反而明显更慢"的回归，不做精确性能断言。
        self.assertLess(new_seconds, legacy_seconds * 2 + 0.01)

    def test_large_dataset_cache_hit_is_cheap(self):
        objects = self._build_large()
        host = _make_host([
            _drawib("ib_large2", *[_draw_call(obj.name) for obj in objects])
        ])
        first = host._collect_drawib_referenced_bone_ids("ib_large2")
        start = time.perf_counter()
        second = host._collect_drawib_referenced_bone_ids("ib_large2")
        cached_seconds = time.perf_counter() - start
        self.assertEqual(first, second)
        sys.stdout.write(
            "\n[P1c-bench] cache hit: {:.6f}s\n".format(cached_seconds)
        )


if __name__ == "__main__":
    unittest.main()
