"""顶点去重「缝合标记特殊去重」的回归测试。

覆盖 `ObjBufferHelper.calc_index_vertex_buffer_wwmi_v2` 的两个通道：
- 正常通道（默认）：键 = 勾选的字段 + 该 loop 的顶点编号 ⇒ 顶点编号不同就永不合并。
- 特殊通道（`deduplicate_ignore_vertex_id`，默认开启）：顶点只要有一条邻接边被标记为
  缝合边(use_seam)，键就只剩 POSITION + TEXCOORD、且不含顶点编号 ⇒ 编号不同但位置与 UV
  逐字节相同的标记顶点合并成一行。没标记的顶点照旧走正常通道。
- 网格里一条缝合边都没标时，开关不改变任何输出。

测试不依赖真实 bpy：用 fake 包前缀加载真实模块，只替身掉外围依赖。
"""

import importlib.util
import inspect
import sys
import types
from pathlib import Path

import numpy
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
PKG = "_vertex_dedup_ignore_vid_pkg"


def _install_module(name, **attrs):
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


for _package_name in (PKG, f"{PKG}.common", f"{PKG}.utils"):
    _package = _install_module(_package_name)
    _package.__path__ = []

sys.modules[f"{PKG}.common"].__path__ = [str(REPO_ROOT / "common")]
sys.modules[f"{PKG}.utils"].__path__ = [str(REPO_ROOT / "utils")]


def _raise_fatal(message):
    raise RuntimeError(message)


_install_module("bpy", types=types.SimpleNamespace(Mesh=object, Object=object))
_install_module(f"{PKG}.common.d3d11_gametype", D3D11GameType=object)
_install_module(f"{PKG}.common.draw_call_model", DrawCallModel=object)
_install_module(f"{PKG}.common.logic_name", LogicName=types.SimpleNamespace(HIMI="HIMI"))
_install_module(f"{PKG}.common.global_config", GlobalConfig=types.SimpleNamespace(logic_name="GIMI"))
_install_module(f"{PKG}.common.global_properties", GlobalProterties=types.SimpleNamespace())
_install_module(f"{PKG}.utils.format_utils", FormatUtils=types.SimpleNamespace())
_install_module(f"{PKG}.utils.vertexgroup_utils", VertexGroupUtils=types.SimpleNamespace())
_install_module(f"{PKG}.utils.timer_utils", TimerUtils=types.SimpleNamespace())
_install_module(f"{PKG}.utils.tbn_codec", TBNCodec=types.SimpleNamespace())
_install_module(
    f"{PKG}.utils.color_attribute_utils",
    read_color_attribute_data=lambda *args, **kwargs: None,
    sample_color_attribute_to_loops=lambda *args, **kwargs: None,
)
_install_module(
    f"{PKG}.utils.ssmt_error_utils",
    SSMTErrorUtils=types.SimpleNamespace(raise_fatal=_raise_fatal),
    Fatal=RuntimeError,
)


def _load_real_module(module_name, relative_path):
    spec = importlib.util.spec_from_file_location(module_name, REPO_ROOT / relative_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


ObjBufferHelper = _load_real_module(
    f"{PKG}.common.obj_buffer_helper", Path("common") / "obj_buffer_helper.py"
).ObjBufferHelper


DTYPE = numpy.dtype([("POSITION", "<f4", (3,)), ("TEXCOORD", "<f4", (2,))])
DTYPE_WITH_NORMAL = numpy.dtype(
    [("POSITION", "<f4", (3,)), ("NORMAL", "<f4", (3,)), ("TEXCOORD", "<f4", (2,))]
)
POSITION_STRIDE = 12
TEXCOORD_STRIDE = 8


class _FakeLoops:
    def __init__(self, vertex_indices):
        self._vertex_indices = numpy.asarray(vertex_indices, dtype=numpy.int64)

    def __len__(self):
        return int(self._vertex_indices.shape[0])

    def foreach_get(self, attribute_name, out):
        assert attribute_name == "vertex_index"
        out[:] = self._vertex_indices


class _FakeEdges:
    def __init__(self, vertex_pairs, seam_flags):
        self._pairs = numpy.asarray(vertex_pairs, dtype=numpy.int64).reshape(-1, 2)
        self._seam = numpy.asarray(seam_flags, dtype=bool)

    def __len__(self):
        return int(self._pairs.shape[0])

    def foreach_get(self, attribute_name, out):
        if attribute_name == "use_seam":
            out[:] = self._seam
            return
        assert attribute_name == "vertices"
        out[:] = self._pairs.ravel()


class _FakeVertices:
    def __init__(self, count):
        self._count = int(count)

    def __len__(self):
        return self._count


class _FakeMesh:
    """seam_vertices 里的顶点各挂一条 use_seam=True 的自环边（＝被标记）。"""

    def __init__(self, vertex_indices, seam_vertices=()):
        self.loops = _FakeLoops(vertex_indices)
        vertex_indices = numpy.asarray(vertex_indices, dtype=numpy.int64)
        vertex_count = int(vertex_indices.max()) + 1 if vertex_indices.size else 0
        self.vertices = _FakeVertices(vertex_count)

        pairs = []
        flags = []
        for vertex_id in sorted({int(value) for value in seam_vertices}):
            pairs.append((vertex_id, vertex_id))
            flags.append(True)
        if vertex_count >= 2:
            # 一条没标记的边：不该让它的端点进特殊通道
            pairs.append((0, 1))
            flags.append(False)
        self.edges = _FakeEdges(pairs, flags) if pairs else _FakeEdges([(0, 0)], [False])


class _FakeGameType:
    OrderedFullElementList = ["POSITION", "TEXCOORD"]

    def get_real_category_stride_dict(self):
        return {"Position": POSITION_STRIDE, "Texcoord": TEXCOORD_STRIDE}


def _make_element_rows(positions, uvs, normals=None):
    dtype = DTYPE_WITH_NORMAL if normals is not None else DTYPE
    rows = numpy.zeros(len(positions), dtype=dtype)
    rows["POSITION"] = numpy.asarray(positions, dtype=numpy.float32).reshape(-1, 3)
    rows["TEXCOORD"] = numpy.asarray(uvs, dtype=numpy.float32).reshape(-1, 2)
    if normals is not None:
        rows["NORMAL"] = numpy.asarray(normals, dtype=numpy.float32).reshape(-1, 3)
    return rows


def _run(mesh, rows, deduplicate_element_set, ignore_vertex_id, dtype=None):
    return ObjBufferHelper.calc_index_vertex_buffer_wwmi_v2(
        mesh=mesh,
        element_vertex_ndarray=rows,
        dtype=dtype if dtype is not None else rows.dtype,
        d3d11_game_type=_FakeGameType(),
        deduplicate_element_set=deduplicate_element_set,
        deduplicate_ignore_vertex_id=ignore_vertex_id,
        flip_triangles=False,
    )


def _exported_vertex_count(category_buffer_dict):
    return len(category_buffer_dict["Position"]) // POSITION_STRIDE


# 两个三角形、5 个源顶点：A 组三个顶点位置相同，B 组两个顶点位置相同，UV 全部相同。
_A = (1.0, 2.0, 3.0)
_B = (4.0, 5.0, 6.0)
_U = (0.25, 0.75)
_U2 = (0.5, 0.75)
_LOOP_VERTEX_IDS = [0, 1, 2, 0, 3, 4]
_LOOP_POSITIONS = [_A, _A, _A, _A, _B, _B]
_LOOP_UVS = [_U, _U, _U, _U, _U, _U]


def test_switch_defaults_to_enabled():
    signature = inspect.signature(ObjBufferHelper.calc_index_vertex_buffer_wwmi_v2)
    assert signature.parameters["deduplicate_ignore_vertex_id"].default is True


def test_no_seam_marks_keeps_vertex_id():
    # 网格里没有任何缝合边标记 ⇒ 开关开着也等于没开
    rows = _make_element_rows(_LOOP_POSITIONS, _LOOP_UVS)
    mesh = _FakeMesh(_LOOP_VERTEX_IDS)

    ib, category_buffer_dict, index_vertex_id_dict, unique_rows, _ = _run(
        mesh=mesh,
        rows=rows,
        deduplicate_element_set={"POSITION", "TEXCOORD"},
        ignore_vertex_id=True,
    )

    assert _exported_vertex_count(category_buffer_dict) == 5
    assert len(unique_rows) == 5
    assert list(index_vertex_id_dict.values()) == [0, 1, 2, 3, 4]
    assert ib == [0, 1, 2, 0, 3, 4]


def test_seam_marked_vertices_merge_across_vertex_ids():
    rows = _make_element_rows(_LOOP_POSITIONS, _LOOP_UVS)
    mesh = _FakeMesh(_LOOP_VERTEX_IDS, seam_vertices=[0, 1, 2, 3, 4])

    ib, category_buffer_dict, index_vertex_id_dict, unique_rows, _ = _run(
        mesh=mesh,
        rows=rows,
        deduplicate_element_set={"POSITION", "TEXCOORD"},
        ignore_vertex_id=True,
    )

    # A 组三个标记顶点 + B 组两个标记顶点 ⇒ 合成 2 行
    assert _exported_vertex_count(category_buffer_dict) == 2
    assert len(unique_rows) == 2
    assert ib == [0, 0, 0, 0, 1, 1]
    # 行 → 顶点编号仍取首个出现 loop 的源顶点（B 行的首个 loop 是 loop4/顶点3）
    assert list(index_vertex_id_dict.values()) == [0, 3]


def test_unmarked_vertices_keep_vertex_id():
    # 只标记顶点 0 和 1：它们合并，顶点 2/3/4 仍然各占一行
    rows = _make_element_rows(_LOOP_POSITIONS, _LOOP_UVS)
    mesh = _FakeMesh(_LOOP_VERTEX_IDS, seam_vertices=[0, 1])

    ib, category_buffer_dict, index_vertex_id_dict, _, _ = _run(
        mesh=mesh,
        rows=rows,
        deduplicate_element_set={"POSITION", "TEXCOORD"},
        ignore_vertex_id=True,
    )

    assert _exported_vertex_count(category_buffer_dict) == 4
    assert ib == [0, 0, 1, 0, 2, 3]
    assert list(index_vertex_id_dict.values()) == [0, 2, 3, 4]


def test_switch_off_ignores_seam_marks():
    rows = _make_element_rows(_LOOP_POSITIONS, _LOOP_UVS)
    mesh = _FakeMesh(_LOOP_VERTEX_IDS, seam_vertices=[0, 1, 2, 3, 4])

    ib, category_buffer_dict, _, _, _ = _run(
        mesh=mesh,
        rows=rows,
        deduplicate_element_set={"POSITION", "TEXCOORD"},
        ignore_vertex_id=False,
    )

    assert _exported_vertex_count(category_buffer_dict) == 5
    assert ib == [0, 1, 2, 0, 3, 4]


def test_special_channel_always_compares_uv_even_if_panel_does_not():
    # 面板只勾位置：标记顶点仍然要比 UV，UV 不同的两个标记顶点不能合并
    marked_different_uv_rows = _make_element_rows([_A, _A], [_U, _U2])
    mesh = _FakeMesh([0, 1], seam_vertices=[0, 1])

    _, category_buffer_dict, _, _, _ = _run(
        mesh=mesh,
        rows=marked_different_uv_rows,
        deduplicate_element_set={"POSITION"},
        ignore_vertex_id=True,
    )
    assert _exported_vertex_count(category_buffer_dict) == 2

    # UV 一样 ⇒ 合并成一行
    marked_same_uv_rows = _make_element_rows([_A, _A], [_U, _U])
    _, category_buffer_dict, _, _, _ = _run(
        mesh=mesh,
        rows=marked_same_uv_rows,
        deduplicate_element_set={"POSITION"},
        ignore_vertex_id=True,
    )
    assert _exported_vertex_count(category_buffer_dict) == 1


def test_special_channel_ignores_fields_other_than_position_and_uv():
    # 标记顶点的法线不同也必须合并：特殊通道只比 位置 + UV
    rows = _make_element_rows(
        [_A, _A],
        [_U, _U],
        normals=[(1.0, 0.0, 0.0), (0.0, 1.0, 0.0)],
    )
    mesh = _FakeMesh([0, 1], seam_vertices=[0, 1])

    _, category_buffer_dict, _, _, _ = _run(
        mesh=mesh,
        rows=rows,
        deduplicate_element_set={"POSITION", "NORMAL", "TEXCOORD"},
        ignore_vertex_id=True,
        dtype=DTYPE_WITH_NORMAL,
    )
    assert _exported_vertex_count(category_buffer_dict) == 1


def test_same_vertex_different_fields_still_splits():
    # 同一顶点编号、位置相同、UV 不同 ⇒ 两个 loop 仍各占一行（精度控制的原有语义不变）
    rows = _make_element_rows([_A, _A], [_U, _U2])
    mesh = _FakeMesh([0, 0])

    _, category_buffer_dict, _, unique_rows, _ = _run(
        mesh=mesh,
        rows=rows,
        deduplicate_element_set={"POSITION", "TEXCOORD"},
        ignore_vertex_id=False,
    )
    assert _exported_vertex_count(category_buffer_dict) == 2
    assert len(unique_rows) == 2


def test_seam_mask_marks_any_incident_seam_edge():
    loop_vertex_ids = numpy.asarray([0, 1, 2, 0], dtype=int)

    mask = ObjBufferHelper._seam_marked_loop_mask(_FakeMesh([0, 1, 2], seam_vertices=[1]), loop_vertex_ids)
    assert mask is not None
    assert mask.tolist() == [False, True, False, False]

    # 没有标记 ⇒ None
    assert ObjBufferHelper._seam_marked_loop_mask(_FakeMesh([0, 1, 2]), loop_vertex_ids) is None

    # 没有 edges 属性 ⇒ None（退化成正常通道，不抛）
    class _BareMesh:
        loops = None
        vertices = _FakeVertices(3)

    assert ObjBufferHelper._seam_marked_loop_mask(_BareMesh(), loop_vertex_ids) is None


def test_empty_key_is_rejected():
    # 一个参与去重的类型都不勾、网格里又没有标记 ⇒ 去重键为空，必须报错而不是全部并成一个
    rows = _make_element_rows([_A, _A], [_U, _U])
    mesh = _FakeMesh([0, 1])

    with pytest.raises(RuntimeError, match="顶点去重精度控制"):
        _run(
            mesh=mesh,
            rows=rows,
            deduplicate_element_set={"NORMAL"},
            ignore_vertex_id=True,
        )
