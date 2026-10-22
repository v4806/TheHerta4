"""族级物体矩阵 X_f = M_base⁻¹ · M_f：计算 + 骨对偶交叉校验 + 审计记录（HIMI 合并骨架 t39-X）。

口径（抓帧实测钉死，勿凭直觉改）
--------------------------------
游戏侧真值：``world = M_f · (P_f · v)`` —— ``M_f`` 是**该 draw 自己的对象矩阵**（vs-cb3），
``P_f`` 是该族 palette（3×4 行主序，节点局部空间）。合并之后整块几何落在**宿主族那一笔 draw**
里渲染，只剩 ``M_base`` ⇒ 每族都被按宿主族摆放（实测偏差 1.0 ~ 3.8 游戏单位）。
把族级常量左乘进写槽即可复原：

    S = X_f · P_f        X_f = M_base⁻¹ · M_f

方案 B（t78 起）：顶点缓冲**原样**写盘、身份资源 ``[1..3]`` 恒为单位阵 ⇒ 净变换
``M_base · X_f · P_f · v`` 就是游戏侧真值。补上 X_f 后残差从 1.0 ~ 3.8 掉到 5.5e-16 ~ 9.9e-16。
更早的 t44～t77 曾用「顶点乘 ``Q_f`` + 写槽乘 ``Q_f⁻¹``」成对相消，已废弃：形态键通道不经
顶点校准，会漏掉其中一半。

cb3 读法 = ``colT``
-----------------
``cb3[0..3]`` 是**行向量约定**的 4 行（``cb3[3].xyz`` 是平移）；转成 palette 口径（列向量 3×4）
时 3×3 要**转置**、平移取 ``cb3[3].xyz``。判据：这样读出的矩阵，``cb3[4..7]`` 恰好是它的逆
（实测残差 9.4e-8 ~ 2.6e-7）；不转置的口径残差 2.6 ~ 5.8 ⇒ 错。

布局判据（必须剔除后继续试别的 draw）
------------------------------------
抓帧里另有 draw 的 ``cb3[0..4]`` 全零、矩阵落在 ``cb3[5..12]``（实测 2c706fbd-3207-60639@000101、
2c706fbd-444-54936@000100、70a4da3e-13473-0@000046）。本模块只接受「``0..3`` 是齐次仿射
（``w`` 列 = 0,0,0,1）且 ``4..7`` 恰为其逆」的那一笔，并继续试该组件的其它 draw。

宿主族（不得硬编码）
--------------------
宿主族 = 合并几何**整块**落在哪一族的 Position 缓冲。产物实测：``2c706fbd-Position.buf``
1366904 B，其余四族 132 ~ 396 B ⇒ 判据 = Position 缓冲最大者，由 :func:`pick_host_family`
唯一实现，导出侧（``drawib_model.category_buffer_dict``）与离线审计（产物 ``Meshes``）共用。

骨对偶交叉校验（需求⑥）
------------------------
两族 palette 里同一根骨的两份矩阵互为对方的族间变换 ⇒ ``X_f · P_f[j]`` 必须**命中**宿主族某根骨
（矩阵元最大差 ≤ :data:`DUALITY_TOL`）。命中数 < 3 ⇒ 记为问题；命中 0 ⇒ 记为豁免（实测
``694025c6`` 与宿主族 0 根共享骨）；把 X 换成单位阵时命中必须掉到 0（反向护栏）。
"""
from __future__ import annotations

import json
import math
import os
import struct

from .himi_merged_runtime import (
    FAMILY_PALETTE_BASENAME_SUFFIX,
    MERGED_SKELETON_STRIDE,
    bare_from_unique_str,
    mat3x4_compose,
    mat3x4_inverse,
)
from .himi_skeleton import resolve_submesh_json_path, runtime_cache_path

#: 3×4 行主序矩阵的 float 个数
MATRIX_FLOATS = 12
#: 单位阵（3×4 行主序，列向量约定）
IDENTITY_3X4 = [1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0]
#: 对象矩阵的齐次行/列必须是 (0,0,0,1)（判「这一笔的 cb3 是不是对象矩阵」）
LAYOUT_TOL = 1e-3
#: 「行 4..7 恰为行 0..3 的逆」的残差上限（实测 9.4e-8 ~ 2.6e-7；错口径 2.6 ~ 5.8）
INVERSE_RESIDUAL_TOL = 1e-4
#: X 与单位阵的最大差 ≤ 此值 ⇒ 记录成「单位阵」（宿主族；实测 ~1e-16）
IDENTITY_SNAP_TOL = 1e-9
#: 骨对偶命中阈值（矩阵元最大差）
DUALITY_TOL = 1e-6
#: 每族至少要命中的骨数（豁免族除外）
DUALITY_MIN_HITS = 3
#: 产物 mod 里 Position 缓冲所在目录（与 MERGED_SKELETON_DEFAULT_FOLDER 同值）
POSITION_BUFFER_FOLDER = "Meshes"
POSITION_BUFFER_SUFFIX = "-Position.buf"


def identity_3x4() -> list:
    """单位阵的副本（3×4 行主序）。"""
    return list(IDENTITY_3X4)


def max_abs_diff(left, right) -> float:
    """两个 12 float 矩阵的最大元素差（长度不同 ⇒ inf）。"""
    if len(left or ()) != len(right or ()):
        return float("inf")
    return max((abs(float(a) - float(b)) for a, b in zip(left, right)), default=0.0)


def is_identity(matrix, tol: float = IDENTITY_SNAP_TOL) -> bool:
    """矩阵是否（在 tol 内）等于单位阵。"""
    return max_abs_diff(matrix, IDENTITY_3X4) <= float(tol)


# --------------------------------------------------------------------------- #
# 宿主族判定（需求②：由代码判定，不得硬编码）
# --------------------------------------------------------------------------- #


def pick_host_family(sizes) -> dict:
    """{family: Position 缓冲字节数} ⇒ {"ok","family","sizes","note"}。

    判据 = **Position 缓冲最大者**（合并几何整块落在宿主族的缓冲里）。并列时取字典序第一个，
    保证同输入同结论（可审计、可复现）。空输入 ⇒ ok=False（调用方 fail-closed）。
    """
    clean = {}
    for family, size in (sizes or {}).items():
        name = str(family or "").strip()
        if not name:
            continue
        try:
            clean[name] = int(size)
        except (TypeError, ValueError):
            continue
    if not clean or max(clean.values()) <= 0:
        # 全 0 B = 没有任何一族真的承载了合并几何（fail-closed：宁可判不出来，也不猜）。
        return {
            "ok": False,
            "family": "",
            "sizes": clean,
            "note": "没有任何族的 Position 缓冲超过 0 B ⇒ 无法判定宿主族",
        }
    family = max(sorted(clean), key=lambda name: clean[name])
    return {
        "ok": True,
        "family": family,
        "sizes": clean,
        "note": f"{POSITION_BUFFER_FOLDER}/{family}{POSITION_BUFFER_SUFFIX} 的缓冲最长（{clean[family]} B）",
    }


def position_buffer_sizes(mod_root: str) -> dict:
    """离线审计：读产物 mod 的 Meshes/<family>-Position.buf 字节数。"""
    folder = os.path.join(str(mod_root or ""), POSITION_BUFFER_FOLDER)
    sizes = {}
    if not os.path.isdir(folder):
        return sizes
    for name in sorted(os.listdir(folder)):
        if not name.endswith(POSITION_BUFFER_SUFFIX):
            continue
        family = name[: -len(POSITION_BUFFER_SUFFIX)]
        if not family:
            continue
        try:
            sizes[family] = os.path.getsize(os.path.join(folder, name))
        except OSError:
            continue
    return sizes


def resolve_mod_root(workspace_root: str, mod_root: str = "") -> str:
    """产物 mod 目录：显式给出优先；否则按 <3Dmigoto>/HI3/Mods/SSMTGeneratedMod/<工作空间名> 推导。"""
    if str(mod_root or "").strip():
        return str(mod_root).strip()
    name = os.path.basename(os.path.normpath(str(workspace_root or "")))
    if not name:
        return ""
    workspace = os.path.normpath(str(workspace_root or ""))
    marker = os.sep + "WorkSpace" + os.sep
    head = workspace.split(marker, 1)[0] if marker in workspace else ""
    if not head:
        return ""
    return os.path.join(head, "3Dmigoto", "HI3", "Mods", "SSMTGeneratedMod", name)


# --------------------------------------------------------------------------- #
# 抓帧 vs-cb3
# --------------------------------------------------------------------------- #


def resolve_frame_analysis_dir(workspace_root: str) -> dict:
    """读 Config/FrameAnalysisPath.json ⇒ {"ok","path","name","note"}（不抛异常）。"""
    path = os.path.join(str(workspace_root or ""), "Config", "FrameAnalysisPath.json")
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except FileNotFoundError:
        return {"ok": False, "path": "", "name": "", "note": f"没有抓帧配置（{path}）"}
    except (OSError, ValueError) as exc:
        return {"ok": False, "path": "", "name": "", "note": f"抓帧配置不可读：{exc}"}
    if not isinstance(payload, dict):
        return {"ok": False, "path": "", "name": "", "note": "抓帧配置不是 JSON 对象"}
    folder = str(payload.get("frameAnalysisFolderPath") or "").strip()
    if not folder or not os.path.isdir(folder):
        return {"ok": False, "path": folder, "name": "", "note": f"抓帧目录不可用（{folder!r}）"}
    return {"ok": True, "path": folder, "name": os.path.basename(os.path.normpath(folder)), "note": ""}


def component_draw_map(workspace_root: str) -> dict:
    """LOD0/ComponentName_DrawCallIndexList.json ⇒ {组件名: [draw 编号]}（失败 ⇒ 空）。"""
    path = os.path.join(str(workspace_root or ""), "LOD0", "ComponentName_DrawCallIndexList.json")
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError):
        return {}
    return dict(payload) if isinstance(payload, dict) else {}


def parse_cb3_text(text: str) -> dict:
    """把 vs-cb3 文本解析成 {寄存器号: [x,y,z,w]}（后写覆盖先写；无法解析的行忽略）。

    行形状：``cb3[3].w: 1.0``（寄存器号是 **float4 寄存器**号，不是 float 下标）。
    """
    registers = {}
    for line in str(text or "").splitlines():
        stripped = line.strip()
        if not stripped.startswith("cb3["):
            continue
        head, separator, tail = stripped.partition(":")
        if not separator:
            continue
        close = head.find("]")
        if close < 0:
            continue
        dot = head.find(".", close + 1)
        if dot < 0:
            continue
        try:
            register = int(head[4:close])
        except ValueError:
            continue
        component = head[dot + 1 : dot + 2].lower()
        if component not in ("x", "y", "z", "w"):
            continue
        fields = tail.strip().split()
        if not fields:
            continue
        try:
            value = float(fields[0])
        except ValueError:
            continue
        row = registers.setdefault(register, [0.0, 0.0, 0.0, 0.0])
        row["xyzw".index(component)] = value
    return registers


def find_draw_cb3_file(fa_dir: str, draw: str) -> str:
    """抓帧里 <draw>-vs-cb3=* 的文本文件路径（.txt 优先；找不到 ⇒ 空串）。"""
    draw = str(draw or "").strip()
    folder = str(fa_dir or "")
    if not draw or not os.path.isdir(folder):
        return ""
    prefix = draw + "-vs-cb3="
    try:
        names = sorted(os.listdir(folder))
    except OSError:
        return ""
    hits = [name for name in names if name.startswith(prefix)]
    texts = [name for name in hits if name.endswith(".txt")]
    chosen = texts[0] if texts else (hits[0] if hits else "")
    return os.path.join(folder, chosen) if chosen else ""


def _col_t(linear_rows, translation_row) -> list:
    """行向量约定的 3 行 + 平移行 ⇒ 列向量约定（palette 口径）的 3×4 行主序。"""
    return [
        linear_rows[0][0], linear_rows[1][0], linear_rows[2][0], translation_row[0],
        linear_rows[0][1], linear_rows[1][1], linear_rows[2][1], translation_row[1],
        linear_rows[0][2], linear_rows[1][2], linear_rows[2][2], translation_row[2],
    ]


def object_matrix_from_registers(registers) -> dict:
    """cb3[0..7] ⇒ {"ok","matrix","inverse","residual","note"}（colT 口径）。

    判据三条（缺一不可）：① 0..7 都在；② 齐次行/列是 (0,0,0,1)；③ 4..7 是 0..3 的逆
    （残差 ≤ INVERSE_RESIDUAL_TOL）。布局 B（0..4 全零、矩阵落在 5..12）会被 ①② 一起挡掉。
    """
    missing = [index for index in range(8) if index not in (registers or {})]
    if missing:
        return {"ok": False, "matrix": [], "inverse": [], "residual": float("inf"),
                "note": f"cb3 缺寄存器 {missing}"}
    rows = [list(registers[index]) for index in range(8)]
    for index in (0, 1, 2, 4, 5, 6):
        if abs(rows[index][3]) > LAYOUT_TOL:
            return {"ok": False, "matrix": [], "inverse": [], "residual": float("inf"),
                    "note": f"cb3[{index}].w = {rows[index][3]:.6g} ≠ 0（不是对象矩阵布局）"}
    for index in (3, 7):
        if abs(rows[index][3] - 1.0) > LAYOUT_TOL:
            return {"ok": False, "matrix": [], "inverse": [], "residual": float("inf"),
                    "note": f"cb3[{index}].w = {rows[index][3]:.6g} ≠ 1（不是对象矩阵布局）"}
    matrix = _col_t(rows[0:3], rows[3])
    inverse = _col_t(rows[4:7], rows[7])
    residual = max_abs_diff(mat3x4_compose(matrix, inverse), IDENTITY_3X4)
    if residual > INVERSE_RESIDUAL_TOL:
        return {"ok": False, "matrix": matrix, "inverse": inverse, "residual": residual,
                "note": f"cb3[4..7] 不是 cb3[0..3] 的逆（残差 {residual:.3g}）"}
    return {"ok": True, "matrix": matrix, "inverse": inverse, "residual": residual, "note": ""}


def read_draw_object_matrix(fa_dir: str, draw: str) -> dict:
    """读一笔 draw 的 vs-cb3 并判它是不是对象矩阵（.txt 是符号链接，open 自动跟随）。"""
    path = find_draw_cb3_file(fa_dir, draw)
    if not path:
        return {"ok": False, "matrix": [], "inverse": [], "residual": float("inf"),
                "path": "", "source_file": "", "note": f"抓帧里没有 {draw}-vs-cb3=*"}
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            text = handle.read()
    except OSError as exc:
        return {"ok": False, "matrix": [], "inverse": [], "residual": float("inf"),
                "path": path, "source_file": os.path.basename(path), "note": str(exc)}
    info = dict(object_matrix_from_registers(parse_cb3_text(text)))
    info["path"] = path
    info["source_file"] = os.path.basename(path)
    return info


# --------------------------------------------------------------------------- #
# palette + 骨对偶
# --------------------------------------------------------------------------- #


def read_family_palette(workspace_root: str, bare: str) -> dict:
    """读该族 palette 缓存（LOD0/<bare>/ModImpRuntime/<bare>-BoneMatrix.buf，48 B/槽）。"""
    name = str(bare or "").strip()
    if not name:
        return {"ok": False, "family": "", "path": "", "slots": [], "note": "部件名为空"}
    # ``unique_str`` 形如 ``LOD0.<bare>``：palette 文件名只带**裸名**（与
    # ``load_family_placement`` 同一套解析）；目录解析两条拼法都试（不猜其它约定）。
    bare_name = bare_from_unique_str(name)
    family = bare_name.split("-", 1)[0]
    json_path = resolve_submesh_json_path(workspace_root, name)
    if not json_path:
        json_path = resolve_submesh_json_path(workspace_root, "LOD0." + bare_name)
    if not json_path:
        return {"ok": False, "family": family, "path": "", "slots": [],
                "note": f"找不到部件 json（{name}）⇒ 无法定位 palette 缓存"}
    path = runtime_cache_path(json_path, bare_name, bare_name + FAMILY_PALETTE_BASENAME_SUFFIX)
    if not os.path.isfile(path):
        return {"ok": False, "family": family, "path": path, "slots": [],
                "note": f"没有族 palette 缓存（{path}）"}
    try:
        with open(path, "rb") as handle:
            raw = handle.read()
    except OSError as exc:
        return {"ok": False, "family": family, "path": path, "slots": [], "note": str(exc)}
    if not raw or len(raw) % MERGED_SKELETON_STRIDE:
        return {"ok": False, "family": family, "path": path, "slots": [],
                "note": f"palette 缓存长度 {len(raw)} 不是 {MERGED_SKELETON_STRIDE} 的整数倍"}
    slots = [
        list(struct.unpack("<12f", raw[offset : offset + MERGED_SKELETON_STRIDE]))
        for offset in range(0, len(raw), MERGED_SKELETON_STRIDE)
    ]
    return {"ok": True, "family": family, "path": path, "slots": slots, "note": ""}


def _best_residual(matrix, candidates) -> float:
    """matrix 到 candidates 里最近一个的最大元素差（无候选 ⇒ inf）。"""
    best = float("inf")
    for candidate in candidates or ():
        best = min(best, max_abs_diff(matrix, candidate))
    return best


def duality_check(base_palette, family_palette, x, *, tol: float = DUALITY_TOL) -> dict:
    """骨对偶：X ∘ P_f[j] 能否命中宿主族某根骨（矩阵元最大差 ≤ tol）。

    返回 {"checked","hits","total","max_residual","best_residual","unmatched","tol"}；
    best_residual = 所有未命中槽里**最近**的那个距离（反向护栏的分辨力指标）。
    """
    moved_by_slot = [mat3x4_compose(list(x), list(slot)) for slot in (family_palette or ())]
    hits = 0
    max_residual = 0.0
    best_residual = float("inf")
    unmatched = []
    for index, moved in enumerate(moved_by_slot):
        best = _best_residual(moved, base_palette)
        best_residual = min(best_residual, best)
        if best <= float(tol):
            hits += 1
            max_residual = max(max_residual, best)
        else:
            unmatched.append(index)
    return {
        "checked": len(moved_by_slot),
        "hits": hits,
        "total": len(list(base_palette or ())),
        "max_residual": max_residual,
        "best_residual": best_residual if unmatched else 0.0,
        "unmatched": unmatched,
        "tol": float(tol),
    }


# --------------------------------------------------------------------------- #
# 参与合并的族（与导出侧同一事实源）
# --------------------------------------------------------------------------- #


def participants_from_plan(plan) -> dict:
    """{family: 部件名}：本次导出真正进合并的族（plan.rebinds 的族键 → 首个 unique_str）。"""
    out = {}
    for item in getattr(plan, "rebinds", None) or ():
        record = dict(item or {})
        family = str(record.get("family") or record.get("draw_ib") or "").strip()
        bare = str(record.get("unique_str") or "").strip() or bare_from_unique_str(family)
        if family and family not in out:
            out[family] = bare
    return out


def _rotation_deg(matrix) -> float:
    """3×3 线性部分的等效旋转角（度；报告里的可读数字）。"""
    values = [float(value) for value in (matrix or [])]
    if len(values) < 12:
        return 0.0
    trace = values[0] + values[5] + values[10]
    return math.degrees(math.acos(max(-1.0, min(1.0, (trace - 1.0) / 2.0))))


# --------------------------------------------------------------------------- #
# X_f 主计算
# --------------------------------------------------------------------------- #


def _family_x_result(host_family, host_evidence, source_frame, fa_path, families, x, problems, exemptions) -> dict:
    """组装 compute_family_x 的返回体（唯一出口，字段集合稳定）。"""
    return {
        "ok": not problems,
        "host_family": host_family,
        "host_evidence": host_evidence,
        "source_frame": source_frame,
        "frame_analysis_path": fa_path,
        "families": families,
        "x": x,
        "problems": list(problems),
        "exemptions": list(exemptions),
    }


def compute_family_x(
    workspace_root: str,
    *,
    host_family: str,
    family_bares,
    fa_dir: str = "",
    palettes=None,
    host_evidence: str = "",
) -> dict:
    """算每族的 X_f（palette 口径 12 float）+ 骨对偶校验 + 审计记录。

    family_bares = {族: 部件名}（= :func:`participants_from_plan`），**必须显式给出**：
    抓帧里含 22 个族，只有参与合并的那几个才该有 X_f（多算会写出无用的身份资源）。

    返回 {"ok","host_family","host_evidence","source_frame","frame_analysis_path",
    "families","x","problems","exemptions"}；x = {族: 12 float}（给发布用）。三态由调用方判：
    全有且无 problems ⇒ 校准；有 problems 且有 x ⇒ 部分（Fatal）；有 problems 且无 x ⇒
    一个都没有（不校准 + 大声警告）。
    """
    workspace_root = str(workspace_root or "")
    host_family = str(host_family or "").strip()
    bares = {}
    for family, bare in dict(family_bares or {}).items():
        family = str(family or "").strip()
        bare = str(bare or "").strip()
        if family and bare:
            bares[family] = bare
    problems = []
    exemptions = []
    if not bares:
        problems.append("没有参与合并的族（family_bares 为空）⇒ 无法算 X_f")
        return _family_x_result(host_family, host_evidence, "", "", {}, {}, problems, exemptions)
    if not host_family or host_family not in bares:
        problems.append(f"宿主族 {host_family} 不在参与合并的族里（{sorted(bares)}）⇒ 无法算 X_f")
        return _family_x_result(host_family, host_evidence, "", "", {}, {}, problems, exemptions)
    fa_path = str(fa_dir or "")
    frame_name = os.path.basename(os.path.normpath(fa_path)) if fa_path else ""
    if not fa_path:
        frame = resolve_frame_analysis_dir(workspace_root)
        if not frame.get("ok"):
            problems.append(f'抓帧不可用：{frame.get("note")}')
            return _family_x_result(host_family, host_evidence, "", "", {}, {}, problems, exemptions)
        fa_path = str(frame.get("path") or "")
        frame_name = str(frame.get("name") or "")
    draws = component_draw_map(workspace_root)
    candidates = {}
    for component in sorted(draws):
        family = str(component).split("-", 1)[0]
        if family not in bares:
            continue
        for draw in draws.get(component) or ():
            candidates.setdefault(family, []).append((str(component), str(draw)))
    found = {}
    for family in sorted(bares):
        attempts = []
        for component, draw in candidates.get(family, ()):
            info = read_draw_object_matrix(fa_path, draw)
            if info.get("ok"):
                found[family] = {"component": component, "draw": draw, "info": info}
                break
            attempts.append(f'{component}@{draw}: {info.get("note")}')
        if family not in found:
            first = attempts[0] if attempts else "该族在抓帧里没有任何 draw"
            problems.append(
                f"{family}: 抓帧里没有一笔 draw 的 vs-cb3 是合法对象矩阵"
                f"（试过 {len(attempts)} 笔；首因 {first}）"
            )
    if host_family not in found:
        problems.append(f"宿主族 {host_family} 没有合法对象矩阵 ⇒ 无法算 X_f")
        return _family_x_result(host_family, host_evidence, frame_name, fa_path, {}, {}, problems, exemptions)
    base_inverse = mat3x4_inverse(list(found[host_family]["info"]["matrix"]))
    palettes = dict(palettes or {})
    if host_family not in palettes:
        loaded = read_family_palette(workspace_root, bares[host_family])
        if loaded.get("ok"):
            palettes[host_family] = list(loaded.get("slots") or [])
        else:
            problems.append(f'宿主族 {host_family} 的 palette 不可读：{loaded.get("note")}')
    base_palette = list(palettes.get(host_family) or ())
    families = {}
    for family in sorted(found):
        entry = found[family]
        matrix = list(entry["info"]["matrix"])
        x = mat3x4_compose(base_inverse, matrix)
        snapped = is_identity(x)
        if snapped:
            x = identity_3x4()
        families[family] = {
            "Bare": bares.get(family, ""),
            "Component": entry["component"],
            "Draw": entry["draw"],
            "SourceFile": entry["info"].get("source_file", ""),
            "ObjectMatrix": [round(float(value), 9) for value in matrix],
            "InverseResidual": float(entry["info"].get("residual") or 0.0),
            "X": [float(value) for value in x],
            "IsIdentity": bool(snapped),
            "AngleDeg": round(_rotation_deg(x), 3),
            "Translation": [round(float(x[3]), 9), round(float(x[7]), 9), round(float(x[11]), 9)],
        }
        record = families[family]
        family_palette = list(palettes.get(family) or ())
        if not family_palette:
            loaded = read_family_palette(workspace_root, bares.get(family, ""))
            if loaded.get("ok"):
                family_palette = list(loaded.get("slots") or [])
                palettes[family] = family_palette
            else:
                problems.append(
                    f"{family}: palette 不可读 ⇒ 无法做骨对偶校验："
                    + str(loaded.get("note"))
                )
        if not family_palette or not base_palette:
            record["DualityStatus"] = "未校验（palette 不可读）"
            continue
        duality = duality_check(base_palette, family_palette, x)
        guard = duality_check(base_palette, family_palette, identity_3x4())
        record["Duality"] = duality
        record["DualityIdentity"] = guard
        if duality["hits"] >= DUALITY_MIN_HITS:
            record["DualityStatus"] = "通过"
        elif duality["hits"] > 0:
            record["DualityStatus"] = "命中过少"
            problems.append(
                f'{family}: 骨对偶只命中 {duality["hits"]} 根'
                f"（要求 ≥ {DUALITY_MIN_HITS}）⇒ X_f 不可信"
            )
        else:
            record["DualityStatus"] = "豁免（与宿主族无共享骨）"
            exemptions.append(
                f"{family}: 骨对偶 0 命中（该族与宿主族没有共享骨）⇒ 豁免对偶校验，"
                "X_f 仍由抓帧 vs-cb3 直接给出"
            )
        if family != host_family and guard["hits"] >= duality["hits"] and guard["hits"] > 0:
            problems.append(
                f'{family}: 反向护栏失效（X_f 命中 {duality["hits"]} ≤'
                f' 单位阵命中 {guard["hits"]}）'
            )
    result = _family_x_result(
        host_family,
        host_evidence,
        frame_name,
        fa_path,
        families,
        {family: list(entry["X"]) for family, entry in families.items()},
        problems,
        exemptions,
    )
    result["host_palette_slots"] = len(base_palette)
    return result


def family_x_summary(record) -> str:
    """一行审计摘要（控制台/日志用）。"""
    if not record:
        return "无族级物体矩阵记录"
    families = record.get("families") or {}
    host = str(record.get("host_family") or "")
    frame = str(record.get("source_frame") or "?")
    hits = []
    for entry in families.values():
        hits.append(str((entry.get("Duality") or {}).get("hits", 0)))
    return (
        "X_f " + str(len(families)) + " 族（宿主 " + host + "；抓帧 " + frame
        + "；对偶命中 " + "/".join(hits) + "）"
    )


# --------------------------------------------------------------------------- #
# 数据层记录（需求⑦：写进 Config/HIMIMergedSkeleton.json 的顶层 FamilyX）
# --------------------------------------------------------------------------- #


def family_x_audit_record(record, *, host_evidence: str = "", mod_root: str = "") -> dict:
    """把 compute_family_x 的结果整理成**可长期审计**的 JSON 记录。"""
    families = {}
    for family, entry in sorted((record.get("families") or {}).items()):
        duality = dict(entry.get("Duality") or {})
        families[str(family)] = {
            "Bare": entry.get("Bare", ""),
            "Draw": entry.get("Draw", ""),
            "Component": entry.get("Component", ""),
            "SourceFile": entry.get("SourceFile", ""),
            "X": [float(value) for value in (entry.get("X") or [])],
            "IsIdentity": bool(entry.get("IsIdentity")),
            "AngleDeg": entry.get("AngleDeg"),
            "DualityHits": int(duality.get("hits") or 0),
            "DualityTotal": int(duality.get("total") or 0),
            "DualityMaxResidual": float(duality.get("max_residual") or 0.0),
            "DualityStatus": entry.get("DualityStatus", ""),
        }
    return {
        "Algorithm": "X_f = M_base⁻¹ · M_f（vs-cb3 colT 口径；写槽 S = X_f · P_f，方案 B）",
        "SourceFrame": str(record.get("source_frame") or ""),
        "FrameAnalysisPath": str(record.get("frame_analysis_path") or ""),
        "HostFamily": str(record.get("host_family") or ""),
        "HostEvidence": str(host_evidence or record.get("host_evidence") or ""),
        "ModRoot": str(mod_root or ""),
        "Families": families,
        "Problems": list(record.get("problems") or ()),
        "Exemptions": list(record.get("exemptions") or ()),
    }


def write_family_x_record(path: str, record) -> dict:
    """把审计记录并进 Config/HIMIMergedSkeleton.json 的顶层 FamilyX（保留其它键）。

    与数据层同一个 JSON 文件（JsonUtils 的 ensure_ascii=False + indent=4），只加一个顶层键、
    不动既有键 —— 失败只报 ok=False，绝不让导出通道被打断。
    """
    target = str(path or "")
    if not target:
        return {"ok": False, "path": "", "note": "没有元数据路径"}
    try:
        with open(target, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except FileNotFoundError:
        return {"ok": False, "path": target, "note": f"没有合并骨架元数据（{target}）⇒ 不写 X_f 记录"}
    except (OSError, ValueError) as exc:
        return {"ok": False, "path": target, "note": str(exc)}
    if not isinstance(payload, dict):
        return {"ok": False, "path": target, "note": "合并骨架元数据不是 JSON 对象"}
    payload["FamilyX"] = record
    try:
        with open(target, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, indent=4))
    except OSError as exc:
        return {"ok": False, "path": target, "note": str(exc)}
    return {"ok": True, "path": target, "note": ""}


def _main(argv) -> int:
    """python -m common.himi_family_x <工作空间> [宿主族]：打印每族 X_f（人工核对用）。"""
    root = str(argv[1]) if len(argv) > 1 else "K:/SSMT-Package-master/WorkSpace/HIMI/嗨♪爱愿妖精♥"
    host = str(argv[2]) if len(argv) > 2 else ""
    mod_root = resolve_mod_root(root)
    if not host:
        picked = pick_host_family(position_buffer_sizes(mod_root))
        host = str(picked.get("family") or "")
        print(f'宿主族判定：{picked.get("note")}')
    draws = component_draw_map(root)
    bares = {}
    for component in sorted(draws):
        bares.setdefault(str(component).split("-", 1)[0], str(component))
    record = compute_family_x(root, host_family=host, family_bares=bares)
    print(f'抓帧：{record.get("source_frame")}（{record.get("frame_analysis_path")}）')
    for family, entry in sorted((record.get("families") or {}).items()):
        duality = entry.get("Duality") or {}
        draw = str(entry.get("Draw"))
        angle = float(entry.get("AngleDeg") or 0.0)
        print(
            f'{family:10s} draw={draw:>7s} angle={angle:8.3f} identity={entry.get("IsIdentity")}'
            f' 对偶={duality.get("hits", 0)}/{duality.get("total", 0)}'
            f' max残差={float(duality.get("max_residual", 0.0)):.3g}'
            f' {entry.get("DualityStatus")}'
        )
    for line in record.get("problems") or ():
        print("问题：" + str(line))
    for line in record.get("exemptions") or ():
        print("豁免：" + str(line))
    return 0 if record.get("ok") else 1


if __name__ == "__main__":
    import sys

    raise SystemExit(_main(sys.argv))

