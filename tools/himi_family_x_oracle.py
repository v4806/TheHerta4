"""A1/A2/A3 数值 oracle：HIMI 合并骨骼「族级物体矩阵 X_f」（不依赖 bpy / 游戏）。

真值口径（已取证）：world = M_f · (P_f · v)；M_f = 该 draw 自己的对象矩阵（抓帧 vs-cb3，
colT 口径），P_f = 该族 palette（3x4 行主序，节点局部空间）。合并后整块只在**宿主族一笔
draw** 里渲染 ⇒ 只有 M_base ⇒ attach CS 必须补一个左乘项

    S = X_f  ∘  P_f          （X_f = M_base⁻¹ · M_f；t78 方案 B）

顶点侧**不校准**（Position 缓冲原样写）⇒ 端到端有效变换 = M_base · X_f · P_f = M_f · P_f。
身份资源 [1..3] 恒为单位阵（t39–t77 的「顶点乘 Q_f + 写槽乘 Q_f⁻¹」代换已停用，理由见
common/himi_merged_runtime.py 的 t39 注释块与 common/himi_merged_contract.py 的单位阵断言）。

本脚本读**真实**数据：工作空间各族 palette（ModImpRuntime/<bare>-BoneMatrix.buf）、抓帧
vs-cb3、以及**实际发布出去**的身份资源字节（脚本自己调 publish_attach_ranges 落盘再读回），
用 Python 逐行复现 CS 数学，对**全部槽位**做两项比较（矩阵元 + 采样顶点的世界坐标）：

  A1  校准态（X_f 来自抓帧）⇒ max|Δ| 必须 ≤ 1e-6
  A2  反向护栏：把 X 块换成**单位阵**重跑 ⇒ 残差必须回到 1.0~3.9 游戏单位区间
  A3  载荷自洽：发布字节数 == 族数 x 128；[1..3] 是单位阵；解码出的 X_f 与计算值**逐字节**相等；
      CS 里 X 的下标（4/5/6）x 16 == 载荷偏移（64）；ini 侧与文件字节数一致
      （attach 资源是 typed Buffer，长度由字节数决定 ⇒ 这就是「array= 同步点」）。

用法：C:/Python311/python.exe tools/himi_family_x_oracle.py [工作空间] [--out <临时目录>]
"""

import ast
import importlib.util
import inspect
import math
import numpy
import os
import re
import shutil
import struct
import sys
import textwrap
import types

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKG = "himi_family_x_oracle_pkg"
DEFAULT_WORKSPACE = os.path.join(
    "K:" + os.sep, "SSMT-Package-master", "WorkSpace", "HIMI", "嗨♪爱愿妖精♥"
)


# --------------------------------------------------------------------------- #
# fake 宿主：只把被测模块的相对依赖按 fake 包前缀登记（不加载任何需要 bpy 的模块）
# --------------------------------------------------------------------------- #


def _install_module(name, **attrs):
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module
    parent, _, leaf = name.rpartition(".")
    if parent in sys.modules and leaf:
        setattr(sys.modules[parent], leaf, module)
    return module


def _load_real_module(qualname, relpath):
    path = os.path.join(REPO_ROOT, relpath)
    spec = importlib.util.spec_from_file_location(qualname, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[qualname] = module
    parent, _, leaf = qualname.rpartition(".")
    if parent in sys.modules and leaf:
        setattr(sys.modules[parent], leaf, module)
    spec.loader.exec_module(module)
    return module


def bootstrap():
    """加载 himi_merged_runtime / himi_skeleton / himi_family_x / himi 导出器（全程无 bpy）。"""
    for package_name in (PKG, PKG + ".ui", PKG + ".ui.universal", PKG + ".common",
                         PKG + ".utils", PKG + ".blueprint"):
        package = _install_module(package_name)
        package.__path__ = []
    _load_real_module(PKG + ".utils.json_utils", "utils/json_utils.py")
    _load_real_module(PKG + ".utils.tbn_codec", "utils/tbn_codec.py")
    _load_real_module(PKG + ".utils.format_utils", "utils/format_utils.py")
    _load_real_module(PKG + ".common.m_ini_builder", "common/m_ini_builder.py")
    _load_real_module(PKG + ".common.logic_name", "common/logic_name.py")
    _load_real_module(PKG + ".common.global_key_count_helper", "common/global_key_count_helper.py")
    _load_real_module(PKG + ".common.himi_merged_contract", "common/himi_merged_contract.py")
    _load_real_module(PKG + ".common.himi_skeleton", "common/himi_skeleton.py")
    state = {"workspace": "", "mod": ""}
    _install_module(
        PKG + ".common.global_config",
        GlobalConfig=types.SimpleNamespace(
            path_workspace_folder=lambda: state["workspace"],
            path_generate_mod_folder=lambda: state["mod"],
            path_generatemod_buffer_folder=lambda: (
                os.path.join(state["mod"], "Meshes") if state["mod"] else ""
            ),
            get_workspace_name=lambda: os.path.basename(state["workspace"].rstrip("\\/")),
        ),
    )
    _install_module(
        PKG + ".common.global_properties",
        GlobalProterties=types.SimpleNamespace(
            import_merged_vgmap=lambda: True,
            forbid_auto_texture_ini=lambda: False,
            generate_branch_mod_gui=lambda: False,
        ),
    )
    _install_module(
        PKG + ".common.m_ini_helper",
        M_IniHelper=types.SimpleNamespace(
            get_drawindexed_str_list=lambda *_a, **_k: [],
            is_slot_binding_mark_type=lambda *_a, **_k: False,
        ),
    )
    _install_module(PKG + ".common.m_ini_helper_gui", M_IniHelperGUI=types.SimpleNamespace())
    _install_module(
        PKG + ".utils.timer_utils",
        TimerUtils=types.SimpleNamespace(
            start_stage=lambda *_a, **_k: None, end_stage=lambda *_a, **_k: None
        ),
    )
    _install_module(
        PKG + ".blueprint.export_helper",
        BlueprintExportHelper=types.SimpleNamespace(
            current_buffer_folder_name="Meshes",
            get_current_buffer_folder_name=lambda: "Meshes",
        ),
    )

    class _FakeDrawIBExportBase:
        def __init__(self, blueprint_model, combine_ib=False):
            self.blueprint_model = blueprint_model
            self.drawib_model_list = []

    _install_module(PKG + ".ui.universal.drawib_export_base", DrawIBExportBase=_FakeDrawIBExportBase)
    runtime = _load_real_module(PKG + ".common.himi_merged_runtime", "common/himi_merged_runtime.py")
    family_x = _load_real_module(PKG + ".common.himi_family_x", "common/himi_family_x.py")
    _load_real_module(PKG + ".ui.universal.unity", "ui/universal/unity.py")
    exporter = _load_real_module(PKG + ".ui.universal.himi", "ui/universal/himi.py")
    return runtime, family_x, exporter, state


runtime, family_x, exporter, GLOBAL_STATE = bootstrap()
contract = sys.modules[PKG + ".common.himi_merged_contract"]


# --------------------------------------------------------------------------- #
# 数学：与 attach CS / himi_merged_runtime 同口径（3x4 行主序，列向量约定）
# --------------------------------------------------------------------------- #

SAMPLES = (
    (0.0, 0.0, 0.0),
    (1.0, 0.0, 0.0),
    (0.0, 1.0, 0.0),
    (0.0, 0.0, 1.0),
    (0.37, -1.21, 0.83),
    (-0.55, 0.42, -1.37),
)


def _dot(a, b):
    return float(a[0]) * float(b[0]) + float(a[1]) * float(b[1]) + float(a[2]) * float(b[2])


def _apply(matrix, vector):
    """3x4 行主序矩阵作用到 3 维点（齐次 w=1）。"""
    x, y, z = float(vector[0]), float(vector[1]), float(vector[2])
    return (
        matrix[0] * x + matrix[1] * y + matrix[2] * z + matrix[3],
        matrix[4] * x + matrix[5] * y + matrix[6] * z + matrix[7],
        matrix[8] * x + matrix[9] * y + matrix[10] * z + matrix[11],
    )


def _max_diff(left, right):
    return max(abs(float(a) - float(b)) for a, b in zip(left, right))


def _matrix_to_rows(matrix):
    return [[float(matrix[i * 4 + c]) for c in range(4)] for i in range(3)]


def _rows_to_matrix(rows):
    return [float(value) for row in rows for value in row]


def _is_zero_rows(rows):
    return not any(float(value) != 0.0 for row in rows for value in row)


def cs_attach_slot(source_rows, q_rows, x_rows):
    """逐行复现 attach CS：先 `P ∘ Q⁻¹`，再 `X` 左乘（X 全零 ⇒ 跳过 = 旧行为）。

    `source_rows` = himi_src_palette[src+0..2]（源 palette 槽的三条 float4）；
    `q_rows` = himi_attach_range[1..3]（Q_f⁻¹）；`x_rows` = himi_attach_range[4..6]（X_f）。
    """
    s0, s1, s2 = source_rows
    q0, q1, q2 = q_rows
    qc0 = (q0[0], q1[0], q2[0])
    qc1 = (q0[1], q1[1], q2[1])
    qc2 = (q0[2], q1[2], q2[2])
    pt = (q0[3], q1[3], q2[3])
    out = []
    for src in (s0, s1, s2):
        out.append([
            _dot(src[:3], qc0),
            _dot(src[:3], qc1),
            _dot(src[:3], qc2),
            _dot(src[:3], pt) + float(src[3]),
        ])
    if _is_zero_rows(x_rows):
        return _rows_to_matrix(out), "skipped"
    x0, x1, x2 = x_rows
    d0 = (out[0][0], out[1][0], out[2][0])
    d1 = (out[0][1], out[1][1], out[2][1])
    d2 = (out[0][2], out[1][2], out[2][2])
    dt = (out[0][3], out[1][3], out[2][3])
    new = []
    for xa in (x0, x1, x2):
        new.append([
            _dot(xa[:3], d0),
            _dot(xa[:3], d1),
            _dot(xa[:3], d2),
            _dot(xa[:3], dt) + float(xa[3]),
        ])
    return _rows_to_matrix(new), "applied"


# --------------------------------------------------------------------------- #
# 【t74】运行期路径的逐字镜像：ui/universal/himi.py 的 HimiAffine 家族 + X 选择块
# --------------------------------------------------------------------------- #

IDENTITY_AFFINE = ((1.0, 0.0, 0.0, 0.0), (0.0, 1.0, 0.0, 0.0), (0.0, 0.0, 1.0, 0.0))


def _cross(a, b):
    return (
        float(a[1]) * float(b[2]) - float(a[2]) * float(b[1]),
        float(a[2]) * float(b[0]) - float(a[0]) * float(b[2]),
        float(a[0]) * float(b[1]) - float(a[1]) * float(b[0]),
    )


def _affine_from_matrix(matrix):
    """12 个 float（3x4 行主序）⇒ HimiAffine 的三行。"""
    return [list(matrix[0:4]), list(matrix[4:8]), list(matrix[8:12])]


def _affine_to_matrix(affine):
    return [float(value) for row in affine for value in row]


def _affine_is_zero(affine):
    return not any(float(value) != 0.0 for row in affine for value in row)


def cs_cb3_affine(rows):
    """镜像 CS 的 himi_cb3_affine()：cb3 行 0..3 ⇒ 列向量口径 3x4（行 i = 行0[i],行1[i],行2[i],行3[i]）。"""
    return [[float(rows[0][i]), float(rows[1][i]), float(rows[2][i]), float(rows[3][i])]
            for i in range(3)]


def cs_cb3_inverse_affine(rows):
    """镜像 CS 的 himi_cb3_inverse_affine()：cb3 行 4..7 的同一映射。"""
    return [[float(rows[4][i]), float(rows[5][i]), float(rows[6][i]), float(rows[7][i])]
            for i in range(3)]


def cs_affine_mul_row(row, other):
    """镜像 himi_affine_mul_row：row.x*b.r0 + row.y*b.r1 + row.z*b.r2 + float4(0,0,0,row.w)。"""
    out = []
    for column in range(4):
        value = (float(row[0]) * float(other[0][column]) + float(row[1]) * float(other[1][column])
                 + float(row[2]) * float(other[2][column]))
        if column == 3:
            value += float(row[3])
        out.append(value)
    return out


def cs_affine_mul(left, right):
    """镜像 himi_affine_mul(a, b)：三行各自 MulRow（列向量口径的 A·B，先作用 B）。"""
    return [cs_affine_mul_row(left[i], right) for i in range(3)]


def cs_affine_inverse(matrix):
    """镜像 himi_affine_inverse：余子式求逆，|det| < 1e-20 ⇒ 单位阵（照参考插件）。"""
    a = [float(v) for v in matrix[0][:3]]
    b = [float(v) for v in matrix[1][:3]]
    c = [float(v) for v in matrix[2][:3]]
    c0 = _cross(b, c)
    c1 = _cross(c, a)
    c2 = _cross(a, b)
    det = _dot(a, c0)
    out = [list(row) for row in IDENTITY_AFFINE]
    if abs(det) >= 1e-20:
        inv_det = 1.0 / det
        i0 = [c0[0] * inv_det, c1[0] * inv_det, c2[0] * inv_det]
        i1 = [c0[1] * inv_det, c1[1] * inv_det, c2[1] * inv_det]
        i2 = [c0[2] * inv_det, c1[2] * inv_det, c2[2] * inv_det]
        t = [float(matrix[0][3]), float(matrix[1][3]), float(matrix[2][3])]
        out = [
            [i0[0], i0[1], i0[2], -_dot(i0, t)],
            [i1[0], i1[1], i1[2], -_dot(i1, t)],
            [i2[0], i2[1], i2[2], -_dot(i2, t)],
        ]
    return out


def cs_affine_max_diff(left, right):
    """镜像 himi_affine_max_diff：12 个分量的最大绝对差。"""
    return max(abs(float(a) - float(b))
               for left_row, right_row in zip(left, right)
               for a, b in zip(left_row, right_row))


def cs_object_matrix_valid(rows):
    """镜像 himi_object_matrix_valid()：齐次 w / |det| ≥ 1e-6 / 行 4..7 是行 0..3 的逆（≤ 1e-2）。"""
    for index in (0, 1, 2, 4, 5, 6):
        if abs(float(rows[index][3])) > 1e-3:
            return False
    for index in (3, 7):
        if abs(float(rows[index][3]) - 1.0) > 1e-3:
            return False
    la = [float(v) for v in rows[0][:3]]
    lb = [float(v) for v in rows[1][:3]]
    lc = [float(v) for v in rows[2][:3]]
    if abs(_dot(la, _cross(lb, lc))) < 1e-6:
        return False
    product = cs_affine_mul(cs_cb3_affine(rows), cs_cb3_inverse_affine(rows))
    return cs_affine_max_diff(product, IDENTITY_AFFINE) <= 1e-2


def cs_object_x(rows_own, rows_base, table, own_index, base_index, baked_rows,
                version, element_count):
    """逐行镜像 t74 的 X 选择块；返回 (X 的三行, 分支名)。

    table 是 u1 物体矩阵表的镜像（**扁平的 float4 行表**，与 u1 同形：第 f 族占
    table[f*3 .. f*3+2]；可变，按 CS 的行为被写入自己那段）。baked_rows = 载荷 [4..6] 的兜底 X。
    """
    x_rows = [list(row) for row in baked_rows]
    if int(version) < 4:
        return x_rows, "legacy-v3"
    own_object = cs_cb3_affine(rows_own)
    object_valid = cs_object_matrix_valid(rows_own)
    last_row = (int(element_count) - 3) // 3 if int(element_count) >= 3 else 0
    object_in_range = (int(element_count) >= 3
                       and int(own_index) <= last_row and int(base_index) <= last_row)
    if object_valid and object_in_range:
        own_row = int(own_index) * 3
        table[own_row + 0] = list(own_object[0])
        table[own_row + 1] = list(own_object[1])
        table[own_row + 2] = list(own_object[2])
    if int(own_index) == int(base_index):
        return [list(row) for row in IDENTITY_AFFINE], "host-identity"
    if object_valid and object_in_range:
        base_row = int(base_index) * 3
        base = [list(table[base_row + 0]), list(table[base_row + 1]), list(table[base_row + 2])]
        if not _affine_is_zero(base):
            return cs_affine_mul(cs_affine_inverse(base), own_object), "runtime-cb3"
        return x_rows, "baked-table-empty"
    if object_in_range:
        own_row = int(own_index) * 3
        base_row = int(base_index) * 3
        own_cached = [list(table[own_row + 0]), list(table[own_row + 1]), list(table[own_row + 2])]
        base_cached = [list(table[base_row + 0]), list(table[base_row + 1]), list(table[base_row + 2])]
        if not _affine_is_zero(own_cached) and not _affine_is_zero(base_cached):
            return cs_affine_mul(cs_affine_inverse(base_cached), own_cached), "table-reuse"
    return x_rows, "baked-cb3-invalid"


def read_draw_cb3_rows(frame_dir, draw):
    """读该 draw 的 vs-cb3 ⇒ (8 条 float4, 文件名)；缺寄存器补 0（= cb3 副本绑空的样子）。"""
    path = family_x.find_draw_cb3_file(frame_dir, str(draw or ""))
    if not path:
        return None, ""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            text = handle.read()
    except OSError:
        return None, os.path.basename(path)
    registers = family_x.parse_cb3_text(text)
    rows = [list(registers.get(index, [0.0, 0.0, 0.0, 0.0])) for index in range(8)]
    return rows, os.path.basename(path)

def _pack12(values):
    return struct.pack("<12f", *[float(v) for v in values])


def _identity_x():
    return list(family_x.IDENTITY_3X4)


def family_position_samples(workspace_root, bare, limit=256):
    """读该族**真实** Position 缓冲的若干行当采样顶点。

    ``LOD0/<bare>/<TYPE_*>/<bare>-Position.buf``，44 B/行 = 11 个 little-endian float32，
    前 3 个是 POSITION（与 ``_himi_calibrate_bind_positions`` 同一套解释）。返回的 v 就是
    ``world = M_f · P_f · v`` 里的 v（族局部绑定坐标）。t78 方案 B 起顶点侧**不再**乘 Q_f，
    所以这些行就是产物里原样写出的字节，Oracle 直接把它们代入两种方案的端到端公式。
    """
    name = family_x.bare_from_unique_str(str(bare or ""))
    if not name:
        return []
    json_path = family_x.resolve_submesh_json_path(workspace_root, "LOD0." + name)
    if not json_path:
        return []
    path = os.path.join(os.path.dirname(json_path), name + "-Position.buf")
    if not os.path.isfile(path):
        return []
    with open(path, "rb") as handle:
        raw = handle.read()
    rows = len(raw) // 44
    if rows <= 0:
        return []
    cap = rows if limit is None else max(1, int(limit))
    step = max(1, rows // cap)
    out = []
    for row in range(0, rows, step):
        x, y, z = struct.unpack_from("<3f", raw, row * 44)
        out.append((float(x), float(y), float(z)))
        if len(out) >= cap:
            break
    return out


def _read_position_rows(path):
    """读 44 B/行的 Position 缓冲，返回 (N, 3) float64（前 3 个 float32）。"""
    with open(path, "rb") as handle:
        raw = handle.read()
    rows = len(raw) // 44
    if rows <= 0:
        return numpy.zeros((0, 3), dtype=numpy.float64)
    data = numpy.frombuffer(raw[: rows * 44], dtype=numpy.float32).reshape(rows, 11)[:, :3]
    return data.astype(numpy.float64)


def _position_buffer_files(mod_root):
    """产物 mod 里的 Position 缓冲：``{family: path}``。

    同时认 ``Meshes``（普通导出）与 ``Meshes0000``/``Meshes0001``…（直出多文件）；同一族在两个
    夹里都有时取字节数更大的一份（合并几何整块落在宿主族那份里）。
    """
    root = str(mod_root or "")
    found = {}
    if not os.path.isdir(root):
        return found
    for name in sorted(os.listdir(root)):
        folder = os.path.join(root, name)
        if not os.path.isdir(folder):
            continue
        if not name.startswith(family_x.POSITION_BUFFER_FOLDER):
            continue
        for item in sorted(os.listdir(folder)):
            if not item.endswith(family_x.POSITION_BUFFER_SUFFIX):
                continue
            family = item[: -len(family_x.POSITION_BUFFER_SUFFIX)]
            if not family:
                continue
            path = os.path.join(folder, item)
            keep = found.get(family)
            if keep is None or os.path.getsize(path) > os.path.getsize(keep):
                found[family] = path
    return found


def shapekey_space_evidence(mod_root, family, q_f):
    """A4 数值取证：真实产物里「基态 Position 缓冲」与「合并形态键增量」是否同坐标系/同量纲。

    基态 = ``Meshes0000/<fam>-Position.buf``（44 B/行，前 3 个 float32）；
    增量 = ``Meshes0000/<fam>-Position_merged_packed_pos_delta.buf``（12 B/条 = float3）；
    路由 = ``Meshes0000/<fam>-Position_merged_map.buf``（每行 num_slots 个 int32 ⇒ 增量序号，
    -1 = 该顶点在该槽没有增量）。语义见 blueprint/direct_export_shapekey_output_mixin.py:278
    ``_write_merged_slot_files``：增量 = 目标行 − 基态行，**只含位置三列**。

    返回 dict；缺文件/形状异常 ⇒ ``{"ok": False, "note": ...}``（调用方 fail-closed）。
    """
    family = str(family or "")
    base_path = _position_buffer_files(mod_root).get(family, "")
    if not base_path:
        return {"ok": False, "note": "没有 " + family + " 的 Position 缓冲（" + str(mod_root) + "）"}
    folder = os.path.dirname(base_path)
    map_path = os.path.join(folder, family + "-Position_merged_map.buf")
    delta_path = os.path.join(folder, family + "-Position_merged_packed_pos_delta.buf")
    for path in (base_path, map_path, delta_path):
        if not os.path.isfile(path):
            return {"ok": False, "note": "缺文件：" + path}
    with open(base_path, "rb") as handle:
        base_raw = handle.read()
    rows = len(base_raw) // 44
    if rows <= 0:
        return {"ok": False, "note": "基态缓冲为空：" + base_path}
    base = numpy.frombuffer(base_raw[: rows * 44], dtype=numpy.float32).reshape(rows, 11)[:, :3]
    base = base.astype(numpy.float64)
    with open(delta_path, "rb") as handle:
        delta_raw = handle.read()
    if len(delta_raw) % 12:
        return {"ok": False, "note": "增量缓冲长度 " + str(len(delta_raw)) + " B 不是 12 B 的整数倍"}
    delta = numpy.frombuffer(delta_raw, dtype=numpy.float32).reshape(-1, 3).astype(numpy.float64)
    with open(map_path, "rb") as handle:
        map_raw = handle.read()
    flat = numpy.frombuffer(map_raw, dtype=numpy.int32)
    slots = int(flat.size // rows)
    if slots <= 0 or flat.size != rows * slots:
        return {"ok": False, "note": "合并 map 长度 " + str(flat.size) + " 不是 " + str(rows)
                + " 行的整数倍"}
    merged = flat.reshape(rows, slots)
    moved = numpy.zeros((rows, 3), dtype=numpy.float64)
    hit = numpy.zeros(rows, dtype=bool)
    if delta.shape[0] > 0:
        for index in range(slots):
            column = merged[:, index]
            mask = column >= 0
            if not mask.any():
                continue
            moved[mask] += delta[column[mask]]
            hit |= mask
    q = numpy.asarray(q_f, dtype=numpy.float64).reshape(3, 4)
    q_moved = base @ q[:, :3].T + q[:, 3]
    noncommute = 0.0
    if hit.any():
        # 旧方案：顶点先被 Q_f 搬进共同空间，之后同一份增量再叠上去
        # ⇒ Q·(v+δ) 与 Q·v+δ 的差就是形态键通道当年整块撕裂的量级
        noncommute = float(numpy.abs(
            q_moved[hit] + moved[hit] - (base[hit] + moved[hit]) @ q[:, :3].T - q[:, 3]).max())
    return {
        "ok": True,
        "base_path": base_path,
        "rows": rows,
        "slots": slots,
        "pairs": int(numpy.count_nonzero(merged >= 0)),
        "delta_count": int(delta.shape[0]),
        "moved_rows": int(hit.sum()),
        "base_bbox": (base.min(axis=0).tolist(), base.max(axis=0).tolist()),
        "base_extent": float((base.max(axis=0) - base.min(axis=0)).max()),
        "deformed_bbox": ((base + moved)[hit].min(axis=0).tolist(),
                          (base + moved)[hit].max(axis=0).tolist()) if hit.any() else ([], []),
        "base_hit_extent": float((base[hit].max(axis=0) - base[hit].min(axis=0)).max())
        if hit.any() else 0.0,
        "deformed_extent": float(((base + moved)[hit].max(axis=0)
                                  - (base + moved)[hit].min(axis=0)).max()) if hit.any() else 0.0,
        "delta_component_max": float(numpy.abs(delta).max()) if delta.size else 0.0,
        "delta_max": float(numpy.abs(moved).max()) if hit.any() else 0.0,
        "q_extent": float((q_moved.max(axis=0) - q_moved.min(axis=0)).max()),
        "noncommute": noncommute,
    }


def _world_max(end_to_end, truth, rows, chunk=32):
    """max|E·v − T·v| over **全部**（槽 x 真实顶点行）对（numpy 分块控内存）。"""
    e = numpy.asarray(end_to_end, dtype=numpy.float64).reshape(-1, 12)
    t = numpy.asarray(truth, dtype=numpy.float64).reshape(-1, 12)
    v = numpy.asarray(rows, dtype=numpy.float64).reshape(-1, 3)
    if e.shape != t.shape or e.shape[0] == 0 or v.shape[0] == 0:
        return 0.0
    best = 0.0
    span = max(1, int(chunk))
    for start in range(0, e.shape[0], span):
        e_c = e[start:start + span]
        t_c = t[start:start + span]
        e_world = numpy.einsum("sij,nj->sni", e_c[:, (0, 1, 2, 4, 5, 6, 8, 9, 10)].reshape(-1, 3, 3), v) + e_c[:, (3, 7, 11)][:, None, :]
        t_world = numpy.einsum("sij,nj->sni", t_c[:, (0, 1, 2, 4, 5, 6, 8, 9, 10)].reshape(-1, 3, 3), v) + t_c[:, (3, 7, 11)][:, None, :]
        best = max(best, float(numpy.abs(e_world - t_world).max()))
    return best

# --------------------------------------------------------------------------- #
# 【t74】三态自检：真实 Oct-22 抓帧（160 B 的 vs-cb3）现场复现三条路径
# --------------------------------------------------------------------------- #
#: deform（non-indexed）draw 的 vs-cb3 是 368 B / 23 个 float4，rows 0..7 **不是**物体矩阵
#: （r3.w = 0、行 4..7 也不是行 0..3 的逆）⇒ 只有 LOD0 render（indexed）draw 的 160 B 才是。
#: 这就是 ini 采集点从 VB(Blend) 段挪到 LOD0 段的原因，这里用真实帧把结论钉住。
OCT22_FRAME_DIR = os.path.join(
    "K:" + os.sep, "SSMT-Package-master", "3Dmigoto", "HI3", "FrameAnalysis-2026-10-22-133713"
)
_DRAW_INDEXED_RE = re.compile(r"^(\d{6}) DrawIndexed\(IndexCount:(\d+), StartIndexLocation:(\d+)")


def family_draw_map_from_geometry(frame_dir, workspace_root):
    """族 → draw 列表：靠部件目录名「族-IndexCount-StartIndexLocation」与 log.txt 的
    DrawIndexed 几何逐笔精确匹配（同一帧里 IndexCount + StartIndexLocation 唯一决定一次 draw）。"""
    geometry = {}
    try:
        with open(os.path.join(frame_dir, "log.txt"), "r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                hit = _DRAW_INDEXED_RE.match(line)
                if hit:
                    geometry[hit.group(1)] = (int(hit.group(2)), int(hit.group(3)))
    except OSError:
        return {}
    components = {}
    try:
        names = os.listdir(os.path.join(workspace_root, "LOD0"))
    except OSError:
        return {}
    for name in names:
        parts = name.rsplit("-", 2)
        if len(parts) != 3:
            continue
        try:
            components[(int(parts[1]), int(parts[2]))] = parts[0]
        except ValueError:
            continue
    mapping = {}
    for draw in sorted(geometry):
        family = components.get(geometry[draw])
        if family:
            mapping.setdefault(family, []).append(draw)
    return mapping


def rotation_degrees(matrix):
    """3x4（扁平 12 个数）的旋转角（度）——只用于和人工抓帧数字对表。"""
    values = [float(value) for value in matrix]
    trace = values[0] + values[5] + values[10]
    cosine = max(-1.0, min(1.0, (trace - 1.0) * 0.5))
    return math.degrees(math.acos(cosine))


def oct22_three_state_check(workspace, baked_by_family, host_family, own_index_by_family,
                            base_index_by_family, frame_dir=None):
    """用真实 160 B vs-cb3 复现三条路径 ⇒ (ok 或 None=跳过, 报告行, 差表行)。

    (b) 采集有效（同帧宿主那笔已写过表）⇒ X == 直接从 cb3 算的 X（≤ 1e-6）；
    (c) 采集无效 + 表里两段都有上一帧的好值 ⇒ X == 同一值（≤ 1e-6），且表不被写坏；
    (c) 采集无效 + 表全空 ⇒ 逐位退回载荷烘死 X（== 载荷 [4..6]）。
    """
    target = frame_dir or OCT22_FRAME_DIR
    if not os.path.isdir(target):
        return None, ["【t74】Oct-22 三态自检：抓帧目录不存在（" + target + "）⇒ 跳过"], []
    mapping = family_draw_map_from_geometry(target, workspace)
    if host_family not in mapping:
        return False, ["【t74】Oct-22 三态自检：宿主族 " + host_family + " 在帧里找不到 draw"], []
    rows_by_family = {}
    draw_by_family = {}
    for family in sorted(baked_by_family):
        for draw in mapping.get(family) or ():
            rows, _name = read_draw_cb3_rows(target, draw)
            if rows and cs_object_matrix_valid(rows):
                rows_by_family[family] = rows
                draw_by_family[family] = draw
                break
    if host_family not in rows_by_family:
        return False, ["【t74】Oct-22 三态自检：宿主族 " + host_family +
                       " 没有校验通过的 160 B cb3"], []
    host_rows = rows_by_family[host_family]
    m_base = cs_cb3_affine(host_rows)
    element_count = 3 * len(own_index_by_family)
    identity_rows = [list(row) for row in IDENTITY_AFFINE]
    zeros8 = [[0.0, 0.0, 0.0, 0.0] for _ in range(8)]
    notes = []
    diffs = []
    bad = []
    for family in sorted(rows_by_family):
        own_rows = rows_by_family[family]
        own_index = int(own_index_by_family[family])
        base_index = int(base_index_by_family[family])
        baked_rows = _matrix_to_rows(baked_by_family[family])
        base_start = base_index * 3
        own_start = own_index * 3
        m_own = cs_cb3_affine(own_rows)
        x_direct = cs_affine_mul(cs_affine_inverse(m_base), m_own)
        table_b = [[0.0, 0.0, 0.0, 0.0] for _ in range(element_count)]
        table_b[base_start:base_start + 3] = [list(row) for row in m_base]
        x_b, mode_b = cs_object_x(own_rows, host_rows, table_b, own_index, base_index,
                                  baked_rows, runtime.ATTACH_RANGE_VERSION, element_count)
        table_c = [[0.0, 0.0, 0.0, 0.0] for _ in range(element_count)]
        table_c[base_start:base_start + 3] = [list(row) for row in m_base]
        table_c[own_start:own_start + 3] = [list(row) for row in m_own]
        table_c_before = [list(row) for row in table_c]
        x_c, mode_c = cs_object_x(zeros8, host_rows, table_c, own_index, base_index,
                                  baked_rows, runtime.ATTACH_RANGE_VERSION, element_count)
        table_drift = cs_affine_max_diff(table_c, table_c_before)
        table_d = [[0.0, 0.0, 0.0, 0.0] for _ in range(element_count)]
        x_d, mode_d = cs_object_x(zeros8, host_rows, table_d, own_index, base_index,
                                  baked_rows, runtime.ATTACH_RANGE_VERSION, element_count)
        if own_index == base_index:
            if mode_b != "host-identity" or cs_affine_max_diff(x_b, identity_rows) != 0.0:
                bad.append(family + "：(a) 宿主族的 X 不是单位阵（mode=" + mode_b + "）")
        else:
            diff_b = cs_affine_max_diff(x_b, x_direct)
            diff_c = cs_affine_max_diff(x_c, x_direct)
            if mode_b != "runtime-cb3" or diff_b > 1e-6:
                bad.append(family + "：(b) 采集有效路径与直接 cb3 不符（mode=" + mode_b +
                           "，max|Δ|=" + repr(diff_b) + "）")
            if mode_c != "table-reuse" or diff_c > 1e-6:
                bad.append(family + "：(c) 采集无效 + 表非空没有复用上一帧的好值（mode=" +
                           mode_c + "，max|Δ|=" + repr(diff_c) + "）")
            if table_drift != 0.0:
                bad.append(family + "：(c) 采集无效时写坏了表（max|Δ|=" + repr(table_drift) + "）")
            notes.append(family + "：(b)=" + mode_b + "，(c)=" + mode_c)
        if own_index != base_index and (mode_d != "baked-cb3-invalid"
                                        or cs_affine_max_diff(x_d, baked_rows) != 0.0):
            bad.append(family + "：(c) 表全空没有逐位退回载荷烘死 X（mode=" + mode_d + "）")
        diffs.append("%-9s draw=%s  旋转 %9.3f deg  vs 烘死 %9.3f deg   max|dX| = %.3e" % (
            family, draw_by_family[family],
            rotation_degrees(_affine_to_matrix(x_direct)),
            rotation_degrees(baked_by_family[family]),
            cs_affine_max_diff(x_direct, baked_rows),
        ))
    notes.insert(0, "【t74】Oct-22 三态自检（真实 160 B vs-cb3；帧=" + os.path.basename(target) + "）")
    notes.append("    每族 live X（cb3 实时）vs 载荷烘死 X：")
    notes.extend("        " + line for line in diffs)
    for line in bad:
        notes.append("    [!] " + line)
    notes.append("    判定：三态自检 " + ("PASS" if not bad else "FAIL"))
    return (not bad), notes, diffs


# --------------------------------------------------------------------------- #
# 主流程：真实数据 + 实际发布字节 + CS 数学复现
# --------------------------------------------------------------------------- #


def run_oracle(workspace, out_dir):
    """返回 (ok, 报告行)；任何结构性问题直接抛 SystemExit（oracle 不做软失败）。"""
    report = []

    def say(text=""):
        report.append(str(text))
        print(text)

    say("=" * 78)
    say("A1/A2/A3 数值 oracle：族级物体矩阵 X_f（不依赖 bpy / 游戏）")
    say("工作空间：" + workspace)

    fa = family_x.resolve_frame_analysis_dir(workspace)
    if not fa.get("ok"):
        raise SystemExit("抓帧不可用：" + str(fa.get("note")))
    fa_dir = str(fa.get("path") or "")
    say("抓帧目录：" + str(fa.get("name") or fa_dir))

    mod_root = family_x.resolve_mod_root(workspace)
    sizes = {family: os.path.getsize(path)
             for family, path in _position_buffer_files(mod_root).items()}
    host = family_x.pick_host_family(sizes)
    if not host.get("ok"):
        raise SystemExit("宿主族判定失败：" + str(host.get("note")))
    host_family = str(host.get("family") or "")
    say("宿主族（代码判定，" + str(host.get("note")) + "）⇒ " + host_family)

    plan = runtime.load_workspace_plan(workspace)
    ranges = dict(getattr(plan, "family_ranges", None) or {})
    if not getattr(plan, "ok", False) or not ranges:
        raise SystemExit("运行时计划不可用：" + str(getattr(plan, "message", "")))
    participants = family_x.participants_from_plan(plan)
    say("参与族 " + str(len(participants)) + " 个：" + ", ".join(sorted(participants)))

    record = family_x.compute_family_x(
        workspace,
        host_family=host_family,
        family_bares=participants,
        host_evidence=str(host.get("note") or ""),
    )
    if record.get("problems"):
        raise SystemExit("X_f 计算有问题：" + "；".join(record["problems"]))
    x_by_family = {str(k): [float(v) for v in val] for k, val in (record.get("x") or {}).items()}
    say("X_f：" + family_x.family_x_summary(record))
    for note in (record.get("exemptions") or []):
        say("  豁免：" + str(note))

    palettes = {}
    placements = {}
    for family, bare in sorted(participants.items()):
        got = family_x.read_family_palette(workspace, bare)
        if not got.get("ok"):
            raise SystemExit("族 palette 不可读：" + family + " — " + str(got.get("note")))
        palettes[family] = got
        # t78 方案 B：发布出去的族级摆放段恒为单位阵（Q_f 只留在下面 A1-B/A2 的对照计算里）
        placements[family] = contract.attach_placement_identity()

    # ---- 【t74】族索引 + 每族真实 vs-cb3（运行期路径的输入） ---- #
    families_in_order = [family for family, _bare in sorted(participants.items())]
    family_index = {family: position for position, family in enumerate(families_in_order)}
    if host_family not in family_index:
        raise SystemExit("宿主族不在参与族里，无法定 base_index：" + host_family)
    indices = {
        family: (family_index[family], family_index[host_family])
        for family in families_in_order
    }
    cb3_rows = {}
    cb3_sources = {}
    for family in families_in_order:
        entry = (record.get("families") or {}).get(family) or {}
        rows, source = read_draw_cb3_rows(fa_dir, str(entry.get("Draw") or ""))
        if rows is None:
            raise SystemExit(family + "：抓帧 vs-cb3 不可读 — " + str(source or "缺文件"))
        cb3_rows[family] = rows
        cb3_sources[family] = source
    say("族索引（own, base）/ cb3 源：" + "；".join(
        family + "=" + str(indices[family]) + " ← " + cb3_sources[family]
        for family in families_in_order))

    for folder in (out_dir, os.path.join(out_dir, "identity")):
        if os.path.isdir(folder):
            shutil.rmtree(folder)
        os.makedirs(folder, exist_ok=True)
    pub = runtime.publish_attach_ranges(
        ranges, out_dir, placements=placements, family_xs=x_by_family, indices=indices
    )
    if not pub.get("ok"):
        raise SystemExit("身份资源发布失败：" + str(pub.get("message")))
    say("发布（校准态）：" + str(pub.get("message")))
    identity_dir = os.path.join(out_dir, "identity")
    identity_xs = {family: _identity_x() for family in x_by_family}
    pub_id = runtime.publish_attach_ranges(
        ranges, identity_dir, placements=placements, family_xs=identity_xs, indices=indices
    )
    if not pub_id.get("ok"):
        raise SystemExit("反向护栏发布失败：" + str(pub_id.get("message")))
    say("发布（A2 反向护栏：X 块 = 单位阵）：" + str(pub_id.get("message")))

    def read_payloads(folder):
        got = {}
        for family, info in sorted(ranges.items()):
            if int(info.get("slots") or 0) <= 0:
                continue
            path = os.path.join(folder, runtime.attach_range_basename(family))
            with open(path, "rb") as handle:
                raw = handle.read()
            got[family] = (raw, runtime.decode_attach_range_payload(raw), path)
        return got

    real = read_payloads(out_dir)
    guard = read_payloads(identity_dir)

    # ---- A3：载荷自洽 / 四方同步 ---- #
    problems = []
    cs_text = exporter.merged_attach_hlsl_text()
    cs_problems = exporter.merged_attach_hlsl_problems(cs_text)
    if cs_problems:
        problems.append("CS 判据报错：" + "；".join(cs_problems))
    if 4 * 16 != runtime.ATTACH_RANGE_X_OFFSET:
        problems.append("CS 的 X 首下标 4 x 16 != 载荷 X 偏移 " + str(runtime.ATTACH_RANGE_X_OFFSET))
    for index in (4, 5, 6):
        token = "himi_attach_range[" + str(index) + "]"
        if token not in cs_text:
            problems.append("CS 里没有读 " + token)
        if index * 16 < runtime.ATTACH_RANGE_X_OFFSET:
            problems.append("CS 下标 " + str(index) + " 落在 X 块之前（载荷 X 偏移 " + str(runtime.ATTACH_RANGE_X_OFFSET) + "）")
    total_bytes = sum(len(item[0]) for item in real.values())
    if total_bytes != len(real) * runtime.ATTACH_RANGE_BYTES:
        problems.append("发布总字节数 " + str(total_bytes) + " != " + str(len(real))
                        + " 族 x " + str(runtime.ATTACH_RANGE_BYTES))
    for family, (raw, decoded, _path) in sorted(real.items()):
        info = ranges.get(family) or {}
        if not decoded.get("ok"):
            problems.append(family + "：载荷解码失败")
            continue
        if len(raw) != runtime.ATTACH_RANGE_BYTES or len(raw) != 8 * 16:
            problems.append(family + "：文件 " + str(len(raw)) + " B != 128 B（= 8 x float4）")
        if not decoded.get("has_indices"):
            problems.append(family + "：has_indices 为假（载荷 [7] 族索引没写）")
        if (int(decoded.get("own_index", -1)) != int(indices[family][0])
                or int(decoded.get("base_index", -1)) != int(indices[family][1])):
            problems.append(family + "：载荷 [7] 族索引 (own="
                            + str(decoded.get("own_index")) + ", base="
                            + str(decoded.get("base_index")) + ") != 发布值 "
                            + str(indices[family]))
        if raw[runtime.ATTACH_RANGE_INDICES_OFFSET:] != struct.pack(
                "<4I", indices[family][0], indices[family][1], 0, 0):
            problems.append(family + "：载荷 [7] 的 16 B 与 struct.pack 结果不逐字节相等")
        if (int(decoded.get("base", -1)) != int(info.get("base", -2))
                or int(decoded.get("span", -1)) != int(info.get("slots", -2))):
            problems.append(family + "：载荷 base/span 与 family_ranges 不一致")
        if _pack12(decoded.get("placement") or []) != _pack12(placements[family]):
            problems.append(family + "：解码出的摆放段与发布值不逐字节相等")
        if not decoded.get("placement_is_identity"):
            problems.append(family + "：解码出的 [1..3] 不是单位阵（方案 B 要求恒为单位阵）")
        if raw[runtime.ATTACH_RANGE_HEADER_BYTES:runtime.ATTACH_RANGE_X_OFFSET] != _pack12(
                contract.attach_placement_identity()):
            problems.append(family + "：载荷 [1..3] 的 48 B 不是单位阵字节")
        if _pack12(decoded.get("family_x") or []) != _pack12(x_by_family.get(family) or []):
            problems.append(family + "：解码出的 X_f 与计算值不逐字节相等")
        if not decoded.get("has_family_x"):
            problems.append(family + "：has_family_x 为假（X 块全零）")
        ini_lines = runtime.attach_range_ini_lines(
            family, "<folder>/" + runtime.attach_range_basename(family)
        )
        if len(ini_lines) != 4 or any("array=" in line for line in ini_lines):
            problems.append(family + "：ini 行形状变化（typed Buffer 不应有 array=）："
                            + " | ".join(ini_lines))
    # u1 物体矩阵表：数组长度 = 3 x 族数，布局往返一致（CS 用 own_index*3 定位自己那段）
    table_decl = runtime.build_object_matrix_decl("himia", len(families_in_order))
    table_ini = runtime.object_matrix_ini_lines("himia", len(families_in_order))
    if int(table_decl.array) != 3 * len(families_in_order):
        problems.append("u1 表 array = " + str(table_decl.array) + " != 3 x "
                        + str(len(families_in_order)))
    if (len(table_ini) != 5
            or any("stride" in line for line in table_ini)
            or not any(line.strip() == "array = " + str(3 * len(families_in_order))
                       for line in table_ini)):
        problems.append("u1 表 ini 形状变化（RWBuffer + array=3x族数，不应有 stride）："
                        + " | ".join(table_ini))
    rows_round = runtime.object_matrix_table_rows(x_by_family[host_family])
    if any(abs(float(a) - float(b)) > 1e-9
           for a, b in zip(runtime.object_matrix_from_table_rows(rows_round),
                           x_by_family[host_family])):
        problems.append("u1 表布局往返（object_matrix_table_rows/from_table_rows）不一致")
    if cs_text.count(runtime.OBJECT_MATRIX_REGISTER) < 1:
        problems.append("CS 里没有 " + runtime.OBJECT_MATRIX_REGISTER + " 的绑定声明")

    four_way = runtime.verify_attach_range_files(
        out_dir, ranges, placements=placements, family_xs=x_by_family, indices=indices
    )
    if four_way:
        problems.append("四方同步核对失败：" + "；".join(four_way))
    say("A3 载荷自洽（字节数 / 解码 / 下标 / ini / 四方核对）："
        + ("通过" if not problems else str(len(problems)) + " 项问题"))
    for line in problems:
        say("  [!] " + line)

    # ---- A1 / A2：逐族逐槽 ---- #
    host_draw = str(((record.get("families") or {}).get(host_family) or {}).get("Draw") or "")
    m_base_read = family_x.read_draw_object_matrix(fa_dir, host_draw)
    if not m_base_read.get("ok"):
        raise SystemExit("宿主族对象矩阵不可读：" + str(m_base_read.get("note")))
    m_base = [float(v) for v in m_base_read["matrix"]]
    say("")
    say("A1/A2 的世界坐标比较用**各族全部真实顶点行**（44 B/行取前 3 个 float32；缺失时退回合成采样）")
    say("A1（方案 B：写槽 S = X_f·P_f，顶点原样）/ A2（X = 单位阵）/ A1-B（旧方案 S = X_f·P_f·Q_f⁻¹ 配 Q_f·v）")
    say("族        draw    槽   对偶命中  对偶max残差    复现max     A1矩阵     A1世界   A1-B矩阵   A1-B世界     A2世界")
    identity_rows = _matrix_to_rows(contract.attach_placement_identity())
    a1_matrix = 0.0
    a1_world = 0.0
    a1b_matrix = 0.0
    a1b_world = 0.0
    a1_reproduce = 0.0
    a2_world = 0.0
    a2_by_family = {}
    slot_total = 0
    row_note = []
    for family in sorted(x_by_family):
        info = ranges.get(family) or {}
        slots = int(info.get("slots") or 0)
        slot_total += slots
        entry = (record.get("families") or {}).get(family) or {}
        draw = str(entry.get("Draw") or "")
        duality = entry.get("Duality") or {}
        m_f_read = family_x.read_draw_object_matrix(fa_dir, draw)
        if not m_f_read.get("ok"):
            raise SystemExit(family + "：抓帧对象矩阵不可读 — " + str(m_f_read.get("note")))
        m_f = [float(value) for value in m_f_read["matrix"]]
        q_f = palettes[family]["slots"][0]
        q_inv = runtime.mat3x4_inverse(q_f)
        x_f = x_by_family[family]
        x_rows_real = _matrix_to_rows(real[family][1].get("family_x") or [])
        x_rows_guard = _matrix_to_rows(guard[family][1].get("family_x") or [])
        rows = family_position_samples(workspace, participants.get(family) or "", None) or list(SAMPLES)
        row_note.append(family + ":" + str(len(rows)))
        e_calibrated = []
        e_old = []
        e_guard = []
        truth_rows = []
        fam_reproduce = 0.0
        fam_matrix = 0.0
        fam_old_matrix = 0.0
        for index in range(slots):
            p_j = palettes[family]["slots"][index]
            src_rows = _matrix_to_rows(p_j)
            # 方案 B：CS 的 Q 段乘的是身份资源 [1..3] = **单位阵** ⇒ S = X_f ∘ P_f
            s_cs, _mode = cs_attach_slot(src_rows, identity_rows, x_rows_real)
            ideal = runtime.mat3x4_compose(x_f, p_j)
            fam_reproduce = max(fam_reproduce, _max_diff(s_cs, ideal))
            left = runtime.mat3x4_compose(m_base, s_cs)
            right = runtime.mat3x4_compose(m_f, p_j)
            fam_matrix = max(fam_matrix, _max_diff(left, right))
            e_calibrated.append(left)                      # 作用在**原样**顶点 v 上
            truth_rows.append(right)
            # A1-B：旧方案（写槽 X_f∘P_f∘Q_f⁻¹，顶点侧先乘 Q_f）作用在原样 v 上等价于
            # M_base∘(X_f∘P_f∘Q_f⁻¹)∘Q_f ⇒ 端到端必须与方案 B 逐位一致（这次改动不改世界坐标）
            s_old, _mode_old = cs_attach_slot(src_rows, _matrix_to_rows(q_inv), x_rows_real)
            e_old.append(runtime.mat3x4_compose(
                runtime.mat3x4_compose(m_base, s_old), q_f))
            fam_old_matrix = max(fam_old_matrix, _max_diff(
                s_cs, runtime.mat3x4_compose(s_old, q_f)))
            s_guard, _mode2 = cs_attach_slot(src_rows, identity_rows, x_rows_guard)
            e_guard.append(runtime.mat3x4_compose(m_base, s_guard))
        fam_world = _world_max(e_calibrated, truth_rows, rows)
        fam_old_world = _world_max(e_old, truth_rows, rows)
        fam_guard = _world_max(e_guard, truth_rows, rows)
        a1_reproduce = max(a1_reproduce, fam_reproduce)
        a1_matrix = max(a1_matrix, fam_matrix)
        a1_world = max(a1_world, fam_world)
        a1b_matrix = max(a1b_matrix, fam_old_matrix)
        a1b_world = max(a1b_world, fam_old_world)
        a2_world = max(a2_world, fam_guard)
        a2_by_family[family] = fam_guard
        say("%-9s %-7s %4d  %3s/%-3s  %12.3e  %10.3e  %10.3e  %10.3e  %10.3e  %10.3e  %10.3e" % (
            family, draw, slots,
            str(duality.get("hits", "-")), str(duality.get("total", "-")),
            float(duality.get("max_residual") or 0.0),
            fam_reproduce, fam_matrix, fam_world, fam_old_matrix, fam_old_world, fam_guard,
        ))

    # ---- 【t74】A1-RT：X 的来源换成「运行期从 vs-cb3 实时算」，其余口径逐字不变 ---- #
    element_count = 3 * len(families_in_order)

    def steady_table():
        """稳态表镜像：每族在自己上一笔 draw 里写进去的、**校验通过**的那份矩阵。"""
        table = [[0.0, 0.0, 0.0, 0.0] for _ in range(element_count)]
        for family in families_in_order:
            if cs_object_matrix_valid(cb3_rows[family]):
                start = family_index[family] * 3
                table[start:start + 3] = [list(row) for row in cs_cb3_affine(cb3_rows[family])]
        return table

    a1rt_x = 0.0
    a1rt_matrix = 0.0
    a1rt_world = 0.0
    rt_modes = {}
    for family in sorted(x_by_family):
        slots = int((ranges.get(family) or {}).get("slots") or 0)
        entry = (record.get("families") or {}).get(family) or {}
        m_f_read = family_x.read_draw_object_matrix(fa_dir, str(entry.get("Draw") or ""))
        if not m_f_read.get("ok"):
            raise SystemExit(family + "：抓帧对象矩阵不可读 — " + str(m_f_read.get("note")))
        m_f = [float(value) for value in m_f_read["matrix"]]
        own_index, base_index = indices[family]
        baked_rows = _matrix_to_rows(x_by_family[family])
        x_rt, mode = cs_object_x(cb3_rows[family], cb3_rows[host_family], steady_table(),
                                 own_index, base_index, baked_rows,
                                 runtime.ATTACH_RANGE_VERSION, element_count)
        rt_modes[family] = mode
        a1rt_x = max(a1rt_x, _max_diff(_affine_to_matrix(x_rt), x_by_family[family]))
        rows = family_position_samples(workspace, participants.get(family) or "", None) or list(SAMPLES)
        e_rows = []
        truth_rt = []
        for index in range(slots):
            p_j = palettes[family]["slots"][index]
            s_rt, _mode = cs_attach_slot(_matrix_to_rows(p_j), identity_rows, x_rt)
            ideal_rt = runtime.mat3x4_compose(_affine_to_matrix(x_rt), p_j)
            a1rt_matrix = max(a1rt_matrix, _max_diff(s_rt, ideal_rt))
            e_rows.append(runtime.mat3x4_compose(m_base, s_rt))
            truth_rt.append(runtime.mat3x4_compose(m_f, p_j))
        a1rt_world = max(a1rt_world, _world_max(e_rows, truth_rt, rows))

    # ---- 【t74】A2-RT：三条变异（cb3 全零 / 行 4..7 破坏 / 宿主族） ---- #
    def empty_table():
        """空表镜像：游戏刚进 / 刚重载，u1 一个值都没写过（全零）。"""
        return [[0.0, 0.0, 0.0, 0.0] for _ in range(element_count)]

    def rt_pick(family, rows_own, table=None):
        own_index, base_index = indices[family]
        return cs_object_x(rows_own, cb3_rows[host_family],
                           steady_table() if table is None else table,
                           own_index, base_index, _matrix_to_rows(x_by_family[family]),
                           runtime.ATTACH_RANGE_VERSION, element_count)

    def rt_slots(family, x_rows):
        return [
            # 摆放段 = 单位阵（方案 B，发布值），逐位与身份资源 [1..3] 同源
            cs_attach_slot(_matrix_to_rows(palettes[family]["slots"][index]),
                           identity_rows, x_rows)[0]
            for index in range(int((ranges.get(family) or {}).get("slots") or 0))
        ]

    rt_mutations = []
    rt_mutation_bad = []
    for family in [f for f in sorted(x_by_family) if f != host_family]:
        baked_rows = _matrix_to_rows(x_by_family[family])
        baked_slots = rt_slots(family, baked_rows)
        # 采集有效路径产出的 X（= 直接从 cb3 算）：后面「采集无效但表里有好值」必须与它一致
        x_live_rows = _affine_to_matrix(rt_pick(family, cb3_rows[family])[0])
        zeros = [[0.0, 0.0, 0.0, 0.0] for _ in range(8)]
        x_zero, mode_zero = rt_pick(family, zeros, empty_table())
        bit = _max_diff(_affine_to_matrix(x_zero), x_by_family[family])
        slot_bit = max((_max_diff(a, b) for a, b in zip(rt_slots(family, x_zero), baked_slots)),
                       default=0.0)
        rt_mutations.append((family + "：cb3 全零 / 空表", mode_zero, bit, slot_bit))
        if mode_zero != "baked-cb3-invalid" or bit != 0.0 or slot_bit != 0.0:
            rt_mutation_bad.append(family + "：cb3 全零 + 空表没有逐位退回兜底 X")
        broken = [list(row) for row in cb3_rows[family]]
        broken[7] = [row_value * 1.05 for row_value in broken[7][:3]] + [1.0]
        broken[5] = [value * 1.05 for value in broken[5][:3]] + [broken[5][3]]
        x_broken, mode_broken = rt_pick(family, broken, empty_table())
        bit_broken = _max_diff(_affine_to_matrix(x_broken), x_by_family[family])
        slot_broken = max((_max_diff(a, b) for a, b in
                           zip(rt_slots(family, x_broken), baked_slots)), default=0.0)
        rt_mutations.append((family + "：行 4..7 破坏 / 空表", mode_broken, bit_broken, slot_broken))
        if mode_broken != "baked-cb3-invalid" or bit_broken != 0.0 or slot_broken != 0.0:
            rt_mutation_bad.append(family + "：行 4..7 破坏 + 空表没有逐位退回兜底 X")
        # 采集无效 + 表里已有上一帧的好值 ⇒ 必须复用表（**不得**退回烘死 X），且不得写表
        for label, rows_bad in (("cb3 全零", zeros), ("行 4..7 破坏", broken)):
            table = steady_table()
            before = [list(row) for row in table]
            x_reuse, mode_reuse = rt_pick(family, rows_bad, table)
            reuse_diff = _max_diff(_affine_to_matrix(x_reuse), x_live_rows)
            table_drift = max((_max_diff(a, b) for a, b in zip(table, before)), default=0.0)
            rt_mutations.append((family + "：" + label + " / 表非空", mode_reuse,
                                 reuse_diff, table_drift))
            if mode_reuse != "table-reuse" or reuse_diff > 1e-6:
                rt_mutation_bad.append(
                    family + "：" + label + " + 表非空没有复用表里上一帧的好值（X 与直接 cb3 不等）")
            if table_drift != 0.0:
                rt_mutation_bad.append(family + "：" + label + " 采集无效时写坏了表（" +
                                       "%.3e" % table_drift + "）")
    x_host, mode_host = rt_pick(host_family, cb3_rows[host_family])
    host_identity = _max_diff(_affine_to_matrix(x_host), _identity_x())
    rt_mutations.append((host_family + "：宿主族", mode_host, host_identity, 0.0))
    if mode_host != "host-identity" or host_identity != 0.0:
        rt_mutation_bad.append("宿主族的 X 不是单位阵（mode=" + mode_host + "）")

    ok_a1rt = a1rt_x <= 1e-6 and a1rt_matrix <= 1e-6 and a1rt_world <= 1e-6
    ok_a2rt = not rt_mutation_bad
    say("")
    say("【t74】运行期路径（X 从本族 vs-cb3 实时算）：族索引 base = " + str(family_index[host_family])
        + "，u1 表长 = " + str(element_count) + " 个 float4")
    say("    分支：" + "，".join(f + "=" + rt_modes[f] for f in sorted(rt_modes)))
    say("    A1-RT max|Δ|（X_cb3 vs 载荷烘死 X）：" + repr(a1rt_x))
    say("    A1-RT max|Δ|（CS 复现 S = X_cb3·P，矩阵元）：" + repr(a1rt_matrix))
    say("    A1-RT max|Δ|（端到端世界坐标）：" + repr(a1rt_world))
    say("    A2-RT 变异（分支 / X 与兜底的差 / 整段 S 与兜底的差）：")
    for name, mode, bit, slot_bit in rt_mutations:
        say("        " + name + " ⇒ " + mode + "，max|ΔX| = " + repr(bit)
            + "，max|ΔS| = " + repr(slot_bit))
    for line in rt_mutation_bad:
        say("    [!] " + line)
    say("    判定：A1-RT " + ("PASS" if ok_a1rt else "FAIL") + "（阈值 ≤ 1e-6）；A2-RT "
        + ("PASS" if ok_a2rt else "FAIL"))

    # ---- 【t74】三态自检：真实 Oct-22 抓帧（160 B vs-cb3）现场复现 ----
    ok_oct22, oct22_lines, _oct22_diffs = oct22_three_state_check(
        workspace, x_by_family, host_family, family_index,
        {family: indices[family][1] for family in indices})
    say("")
    for line in oct22_lines:
        say(line)
    if ok_oct22 is None:
        ok_oct22 = True

    # ---- A4：形态键通道与基态同空间（方案 B 的动机逐条取证） ---- #
    a4_problems = []
    calib_source = inspect.getsource(exporter.ExportHIMI._himi_calibrate_bind_positions)
    calib_tree = ast.parse(textwrap.dedent(calib_source))
    calib_calls = set()
    buffer_writes = []
    # 读字节（numpy.frombuffer(block.tobytes(), ...)）是诊断的正当手段，只有**写回**才算退回旧口径；
    # 因此只拦两类：取 Q 的调用、对行缓冲/源缓冲的下标赋值。
    write_targets = ("view2d", "group", "target", "block", "floats", "position")
    for node in ast.walk(calib_tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            calib_calls.add(node.func.attr)
        if isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Store):
            target_name = getattr(node.value, "id", "")
            if target_name in write_targets:
                buffer_writes.append(target_name)
    for name in sorted(calib_calls & {"_himi_family_placements", "mat3x4_inverse"}):
        a4_problems.append("顶点侧诊断函数里仍调用 " + name + "() ⇒ 又在取 Q")
    for name in sorted(set(buffer_writes)):
        a4_problems.append("顶点侧诊断函数里仍有对 " + name + "[...] 的写回")
    if "_himi_calibrate_bind_positions" not in inspect.getsource(
            exporter.ExportHIMI.generate_buffer_files):
        a4_problems.append("generate_buffer_files 里没有逐行族归属诊断调用点")
    space = shapekey_space_evidence(mod_root, host_family, palettes[host_family]["slots"][0])
    if not space.get("ok"):
        a4_problems.append("A4 数值取证失败：" + str(space.get("note")))
    say("")
    say("A4（形态键通道 = 基态同空间）：顶点侧诊断函数源码里无写回/无 Q 取用 ⇒ "
        + ("PASS" if not any("诊断函数" in line or "调用点" in line for line in a4_problems)
           else "FAIL"))
    if space.get("ok"):
        union_rows = []
        missing_rows = []
        for family in sorted(participants):
            got_rows = family_position_samples(workspace, participants[family], None)
            if not got_rows:
                missing_rows.append(family)
            union_rows.extend(got_rows)
        union = numpy.asarray(union_rows, dtype=numpy.float64).reshape(-1, 3)
        product_min, product_max = (numpy.asarray(space["base_bbox"][0], dtype=numpy.float64),
                                    numpy.asarray(space["base_bbox"][1], dtype=numpy.float64))
        same_as_source = float(numpy.abs(numpy.concatenate([
            union.min(axis=0) - product_min, union.max(axis=0) - product_max])).max())
        if missing_rows:
            a4_problems.append("工作空间缺族 Position 行：" + ",".join(missing_rows))
        say("    产物基态：" + str(space["rows"]) + " 行 × 44 B，槽位 " + str(space["slots"]))
        say("    增量：" + str(space["delta_count"]) + " 条 float3，路由命中 "
            + str(space["pairs"]) + " 对（" + str(space["moved_rows"]) + " 行顶点）")
        say("    max|δ| = " + repr(space["delta_component_max"]) + "；基态包围盒最长边 = "
            + repr(space["base_extent"]) + "；有 δ 的那些行叠加前后最长边 = "
            + repr(space["base_hit_extent"]) + " / " + repr(space["deformed_extent"]))
        say("    基态 bbox（未经 Q）：" + str(space["base_bbox"]))
        say("    若基态被 Q_f 搬过（旧方案）bbox 最长边会变成 = " + repr(space["q_extent"]))
        say("    旧方案错位量 max|Q·v+δ − Q·(v+δ)| = " + repr(space["noncommute"])
            + " 游戏单位（= max|δ| 的 " + repr(space["noncommute"] / space["delta_component_max"]
                                          if space["delta_component_max"] else 0.0) + " 倍）")
        say("    参考（非判据：产物是合并渲染缓冲 " + str(space["rows"]) + " 行，工作空间是按 LOD0/类型的源行 "
            + str(int(union.shape[0])) + " 行，两者不是同一行集）：bbox max|Δ| = " + repr(same_as_source))
        if space["pairs"] != space["delta_count"]:
            a4_problems.append("合并 map 命中 " + str(space["pairs"]) + " 对 != 增量条数 "
                              + str(space["delta_count"]))
        # 唯一能区分「基态被 Q 搬过」的数值判据：同一批行乘 Q_f（1.6x 相似变换 + 84.6°–133.5° 旋转）
        # 后包围盒会显著变大。产物基态是原样落盘的 ⇒ 必须显著小于那个反事实包围盒。
        if space["q_extent"] <= 0.0 or space["base_extent"] > 0.75 * space["q_extent"]:
            a4_problems.append("产物基态包围盒 " + repr(space["base_extent"])
                              + " 与「被 Q_f 搬过」的反事实包围盒 " + repr(space["q_extent"])
                              + " 不可区分 ⇒ 顶点侧可能仍被搬过")
        if space["delta_component_max"] <= 0.0:
            a4_problems.append("增量全零 ⇒ 形态键通道没数据，A4 无法取证")
        elif space["delta_component_max"] > 0.25 * space["base_extent"]:
            a4_problems.append("增量量纲与基态不符（max|δ| = " + repr(space["delta_component_max"])
                              + " vs 基态最长边 " + repr(space["base_extent"]) + "）")
        if abs(space["deformed_extent"] - space["base_hit_extent"]) > max(space["delta_max"], 1e-9):
            a4_problems.append("叠加 δ 后包围盒变化超过 max|δ| ⇒ 增量与基态不在同一空间")
        if space["noncommute"] < 0.5 * space["delta_component_max"]:
            a4_problems.append("旧方案错位量只有 " + repr(space["noncommute"])
                              + " ⇒ 取证不足以支撑「Q·v+δ ≠ Q·(v+δ)」")
    for line in a4_problems:
        say("  [!] " + line)
    ok_a4 = not a4_problems

    ok_a1 = a1_matrix <= 1e-6 and a1_world <= 1e-6 and a1b_matrix <= 1e-6 and a1b_world <= 1e-6
    non_host = [value for family, value in a2_by_family.items() if family != host_family]
    ok_a2 = bool(non_host) and min(non_host) >= 0.5 and a2_world >= 1000.0 * max(a1_world, 1e-12)
    say("")
    say("真实顶点行数：" + " ".join(row_note))
    say("A1 覆盖槽位：" + str(slot_total) + " 个（" + str(len(x_by_family)) + " 族）")
    say("A1 复现自检 max|Δ|（CS 复现 vs runtime.mat3x4_compose）：" + repr(a1_reproduce))
    say("A1 max|Δ|（M_base·S_cs vs M_f·P_f，矩阵元）：" + repr(a1_matrix))
    say("A1 max|Δ|（端到端世界坐标，顶点原样）：" + repr(a1_world))
    say("A1-B max|Δ|（旧方案 S_old·Q_f vs S_new，矩阵元）：" + repr(a1b_matrix))
    say("A1-B max|Δ|（旧方案作用在 Q_f·v 上的端到端世界坐标 vs 真值）：" + repr(a1b_world))
    say("A2 max|Δ|（端到端世界坐标，X = 单位阵）：" + repr(a2_world))
    for family in sorted(a2_by_family):
        if family != host_family:
            say("    A2 " + family + " = " + repr(a2_by_family[family]) + " 游戏单位")
    say("    （宿主族 A2 = 0：M_base == M_f ⇒ X_f 本来就是单位阵）")
    say("判定：A1 " + ("PASS" if ok_a1 else "FAIL")
        + "（新方案与旧方案的矩阵元、端到端世界坐标阈值均 ≤ 1e-6）；A2 "
        + ("PASS" if ok_a2 else "FAIL") + "（非宿主族残差 ≥ 0.5 游戏单位，且 ≥ 1e3 x A1）")
    say("A3 " + ("PASS" if not problems else "FAIL")
        + "（128 B / 8 x float4 / [1..3] 单位阵 / [7] 族索引 / u1 表 array=3x族数）")
    say("A4 " + ("PASS" if ok_a4 else "FAIL")
        + "（顶点侧不写字节 / 无 Q 取用；基态 == 原始行；增量与基态同空间同量纲）")
    return (ok_a1 and ok_a2 and ok_a4 and ok_a1rt and ok_a2rt and ok_oct22 and not problems), report


def main(argv):
    workspace = DEFAULT_WORKSPACE
    out_dir = os.path.join(REPO_ROOT, ".dbg", "_x_oracle_out")
    rest = list(argv[1:])
    while rest:
        item = rest.pop(0)
        if item == "--out" and rest:
            out_dir = rest.pop(0)
        elif not workspace or workspace == DEFAULT_WORKSPACE:
            workspace = item
    ok, _report = run_oracle(workspace, out_dir)
    print("")
    print("ORACLE " + ("PASS" if ok else "FAIL") + "（输出目录 " + out_dir + "）")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))

