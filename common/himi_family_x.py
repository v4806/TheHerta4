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
# 抓帧目录名前缀（3Dmigoto 抓帧 = FrameAnalysis-YYYY-MM-DD-HHMMSS；名序即时间序）
FRAME_ANALYSIS_PREFIX = "FrameAnalysis-"


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


def resolve_frame_analysis_dir(
    workspace_root: str,
    *,
    mod_root: str = "",
    prefer_freshest: bool = False,
    draws=(),
) -> dict:
    """读 Config/FrameAnalysisPath.json ⇒ 抓帧目录（不抛异常）。

    ``prefer_freshest=True`` 时**不盲信配置**：扫一遍 ``<game>/FrameAnalysis-*``，
    若存在比配置里那个更新的抓帧，且它确实能给 ``draws`` 里的 draw 提供 vs-cb3，
    就改用最新的那个 —— 配置里那条路径是**上一次场景**的快照，改过拆并关系后它会过期
    （这正是 ``*-AttachRange.buf`` 里 X_f 对不上的根因）。

    返回另带 ``declared``/``declared_ok``/``freshest``/``source``/``stale``/``note``，
    调用方据此把「用了哪一帧、为什么」写进审计记录，而不是静默换源。
    """
    declared = _declared_frame_analysis_dir(workspace_root)
    game_root = capture_game_root(workspace_root, mod_root)
    captures = list_frame_analysis_dirs(game_root)
    freshest = captures[-1] if captures else ""
    declared_name = str(declared.get("name") or "")
    freshest_name = os.path.basename(os.path.normpath(freshest)) if freshest else ""
    stale = bool(declared_name and freshest_name and freshest_name > declared_name)
    result = dict(declared)
    result.update(
        declared=str(declared.get("path") or ""),
        declared_ok=bool(declared.get("ok")),
        freshest=freshest,
        stale=stale,
        source="declared",
    )
    if prefer_freshest and stale:
        usable = capture_cb3_coverage(freshest, draws) > 0
        if not usable and draws:
            result["note"] = (
                f"最新抓帧 {freshest_name} 没有这些 draw 的 vs-cb3 ⇒ 仍用配置里的 {declared_name}"
            )
            return result
        result.update(
            ok=True,
            path=freshest,
            name=freshest_name,
            source="freshest",
            note=(
                f"配置里的抓帧 {declared_name} 已过期（更新的 {freshest_name} 可用）"
                f"⇒ 改用最新的那一帧算 X_f"
            ),
        )
    return result


def capture_game_root(workspace_root: str, mod_root: str = "") -> str:
    """抓帧根目录 = 部署目录 ``Mods`` 的父目录（``.../3Dmigoto/<game>``）；推不出 ⇒ 空串。"""
    current = os.path.abspath(str(resolve_mod_root(workspace_root, mod_root) or ""))
    for _ in range(6):
        parent = os.path.dirname(current)
        if not parent or parent == current:
            break
        current = parent
        if os.path.basename(current).lower() == "mods":
            return os.path.dirname(current)
    return ""


def list_frame_analysis_dirs(game_root: str) -> list:
    """``game_root`` 下的 ``FrameAnalysis-*`` 目录（目录名自带时间戳，名序即时间序）。"""
    root = str(game_root or "")
    if not root or not os.path.isdir(root):
        return []
    try:
        names = sorted(os.listdir(root))
    except OSError:
        return []
    return [
        os.path.join(root, name)
        for name in names
        if name.startswith(FRAME_ANALYSIS_PREFIX) and os.path.isdir(os.path.join(root, name))
    ]


def capture_cb3_coverage(fa_dir: str, draws=()) -> int:
    """这笔抓帧里有多少个 ``draws`` 能拿到 vs-cb3（用来判断某帧是否还能算 X_f）。"""
    return sum(1 for draw in (draws or ()) if find_draw_cb3_file(fa_dir, draw))


def _declared_frame_analysis_dir(workspace_root: str) -> dict:
    """Config/FrameAnalysisPath.json 原样读出来（不做新鲜度判断）。"""
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
# palette 反解（X_f 的权威来源：导出时现算，不读抓帧）
# --------------------------------------------------------------------------- #

#: palette 反解的接受阈值（矩阵元最大差；与骨对偶同一把尺子）
PALETTE_SOLVE_TOL = DUALITY_TOL
#: palette 解与「抓帧 vs-cb3」解的交叉核对容差（两条独立来源应在此量级内一致）
X_SOURCE_CROSS_TOL = 1e-3
#: 反解时用前 N 个 palette 槽当候选种子（正常第 0 个就够，留几个防脏槽）
PALETTE_SEED_LIMIT = 4
#: 反解时一块候选占用的浮点上限（控制内存；大 palette 会自动缩小块）
PALETTE_BLOCK_FLOATS = 6_000_000


def _homogeneous(palette):
    """12 float/槽 ⇒ (n,4,4) 齐次矩阵（只有反解用 numpy）。"""
    import numpy

    rows = numpy.asarray(list(palette or ()), dtype=numpy.float64)
    if rows.size == 0:
        return numpy.zeros((0, 4, 4), dtype=numpy.float64)
    rows = rows.reshape(-1, 3, 4)
    homogeneous = numpy.zeros((rows.shape[0], 4, 4), dtype=numpy.float64)
    homogeneous[:, :3, :] = rows
    homogeneous[:, 3, 3] = 1.0
    return homogeneous


def solve_family_x_from_palettes(
    base_palette, family_palette, *, tol: float = PALETTE_SOLVE_TOL,
    seed_limit: int = PALETTE_SEED_LIMIT,
) -> dict:
    """从两族 palette 反解 ``X_f = M_base⁻¹ · M_f``（**不读抓帧、不读 vs-cb3**）。

    同一根骨在两族 palette 里是同一位姿的两份表达：``P_base[k] = X_f · P_f[j]``
    ⇒ ``X_f = P_base[k] · P_f[j]⁻¹``。骨对**对应关系未知**，所以先拿若干族槽当种子 ×
    全部宿主槽生成候选，再看「这个候选能让多少根族骨落到宿主骨上（矩阵元最大差 ≤ tol）」
    投票；票数最高、命中残差最小的候选即解。

    两族 palette 都是精确刚体（同一个绑定矩阵 B 派生），正确解命中时残差只在 float32
    噪声量级（1e-7），错解 O(1) ⇒ 判别很干脆。返回 ``{"ok","x","votes","total",
    "max_residual","matched","tol","note"}``；``matched`` = ``[(族槽, 宿主槽, 残差)]``。
    """
    import numpy

    family = _homogeneous(family_palette)
    base = _homogeneous(base_palette)
    total = int(family.shape[0])
    base_total = int(base.shape[0])
    empty = {"ok": False, "x": [], "votes": 0, "total": total, "base_total": base_total,
             "max_residual": float("inf"), "matched": [], "tol": float(tol)}
    if total <= 0 or base_total <= 0:
        return dict(empty, note="两族 palette 有一侧为空 ⇒ 无法反解")
    inverse_family = numpy.linalg.inv(family)
    base3 = base[:, :3, :]
    limit = float(tol)
    block = max(1, min(16, int(PALETTE_BLOCK_FLOATS // max(1, total * base_total * 12))))
    best = None
    for seed in range(min(int(seed_limit), total)):
        candidates = base @ inverse_family[seed]
        for start in range(0, base_total, block):
            chunk = candidates[start : start + block]
            moved = numpy.einsum("cij,njk->cnik", chunk, family)[:, :, :3, :]
            distance = numpy.abs(
                moved[:, :, None, :, :] - base3[None, None, :, :, :]
            ).max(axis=(3, 4))
            nearest = distance.min(axis=2)
            hit = nearest <= limit
            hits = hit.sum(axis=1)
            worst = numpy.where(hit, nearest, 0.0).max(axis=1)
            for row in range(chunk.shape[0]):
                score = (int(hits[row]), -float(worst[row]))
                if best is not None and score <= (best["hits"], -best["worst"]):
                    continue
                matched = []
                for slot in numpy.nonzero(hit[row])[0]:
                    host = int(distance[row, slot].argmin())
                    matched.append((int(slot), host, float(distance[row, slot, host])))
                best = {"hits": int(hits[row]), "worst": float(worst[row]), "matched": matched}
    if not best or best["hits"] <= 0:
        return dict(empty, note="没有任何候选能让族骨落到宿主骨上（两族可能没有共享骨）")
    slot, host, _residual = min(best["matched"], key=lambda item: item[2])
    x = mat3x4_compose(list(base_palette[host]), mat3x4_inverse(list(family_palette[slot])))
    if is_identity(x):
        x = identity_3x4()
    return {
        "ok": True,
        "x": [float(value) for value in x],
        "votes": int(best["hits"]),
        "total": total,
        "base_total": base_total,
        "max_residual": float(best["worst"]),
        "matched": list(best["matched"]),
        "tol": float(tol),
        "note": "",
    }


def solve_palette_pair(borrower_palette, source_palette, *, tol: float = PALETTE_SOLVE_TOL) -> dict:
    """同一根物理骨在两族 palette 里的局部槽对（借位槽实时求 X 用）。

    借用槽要的是 `X_源 = P_本族[p] · P_源[q]⁻¹`（**own 在前**）：p/q 正是同一根骨在
    借用方/来源方 palette 里的两个槽号，所以复用 `solve_family_x_from_palettes`
    （base = 借用方、family = 来源方），再从 `matched` 里取**残差最小**的那一项。
    `matched` 的元组语义是 `(族槽, 宿主槽, 残差)`：这里的族 = 来源方、宿主 = 借用方
    ⇒ `(src_slot, own_slot, residual)`。

    解不出（两族没有共享骨 / 一侧为空）⇒ `ok=False`、`own = src = -1`：
    运行期那时退回单位阵（本帧先原样写 P_源），绝不猜一个槽对。
    """
    solved = solve_family_x_from_palettes(borrower_palette, source_palette, tol=tol)
    if not solved.get("ok"):
        return {
            "ok": False,
            "own": -1,
            "src": -1,
            "residual": float("inf"),
            "votes": 0,
            "note": str(solved.get("note") or "两族 palette 解不出共享骨"),
        }
    matched = [tuple(item) for item in (solved.get("matched") or ())]
    if not matched:
        return {
            "ok": False,
            "own": -1,
            "src": -1,
            "residual": float("inf"),
            "votes": 0,
            "note": "解出的候选没有命中任何骨对",
        }
    src_slot, own_slot, residual = min(matched, key=lambda item: float(item[2]))
    votes = int(solved.get("votes", 0) or 0)
    if votes < 3:
        return {
            "ok": False,
            "own": -1,
            "src": -1,
            "residual": float(residual),
            "votes": votes,
            "note": f"匹配命中票数过低（{votes} < 3），两族无足够共享骨，放弃骨对匹配",
        }
    return {
        "ok": True,
        "own": int(own_slot),
        "src": int(src_slot),
        "residual": float(residual),
        "votes": votes,
        "note": "",
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
    mod_root: str = "",
    prefer_freshest: bool = False,
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
    draws = component_draw_map(workspace_root)
    candidates = {}
    for component in sorted(draws):
        family = str(component).split("-", 1)[0]
        if family not in bares:
            continue
        for draw in draws.get(component) or ():
            candidates.setdefault(family, []).append((str(component), str(draw)))
    known_draws = sorted({draw for pairs in candidates.values() for _component, draw in pairs})
    fa_path = str(fa_dir or "")
    frame_name = os.path.basename(os.path.normpath(fa_path)) if fa_path else ""
    frame = {"ok": True, "path": fa_path, "name": frame_name, "source": "explicit"}
    if not fa_path:
        frame = resolve_frame_analysis_dir(
            workspace_root,
            mod_root=mod_root,
            prefer_freshest=prefer_freshest,
            draws=known_draws,
        )
        if not frame.get("ok"):
            problems.append(f'抓帧不可用：{frame.get("note")}')
            return _family_x_result(host_family, host_evidence, "", "", {}, {}, problems, exemptions)
        fa_path = str(frame.get("path") or "")
        frame_name = str(frame.get("name") or "")
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
    result["source"] = "frame-vs-cb3"
    # 用了哪一帧、配置里那帧是否过期 —— 供审计与「X_f 变过吗」的复核（不静默换源）。
    result["frame_source"] = str(frame.get("source") or "")
    result["frame_declared"] = str(frame.get("declared") or "")
    result["frame_stale"] = bool(frame.get("stale"))
    result["frame_note"] = str(frame.get("note") or "")
    if frame.get("source") == "freshest":
        exemptions.append(str(frame.get("note") or ""))
    return result


def compute_family_x_from_palettes(
    workspace_root: str,
    *,
    host_family: str,
    family_bares,
    palettes=None,
    host_evidence: str = "",
) -> dict:
    """**X_f 的权威来源**：导出时从当前工作空间的族 palette 现算（不读抓帧目录）。

    返回体与 :func:`compute_family_x` 同形，发布侧/审计侧可无缝替换；额外字段：
    ``source = "palette"``，每族 ``Source``/``Votes``/``MaxResidual``。
    解不出来的族**不猜**——记进 ``problems`` 且不给 ``x``，由调用方决定走显式兜底
    （:func:`compute_family_x`，来源会标成 ``frame-vs-cb3``）还是按部分校准 Fatal。
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

    palettes = dict(palettes or {})

    def _palette(family):
        """(槽列表, 来源路径/原因)：优先用调用方注入的，其次读工作空间缓存。"""
        injected = list(palettes.get(family) or ())
        if injected:
            return injected, "injected"
        loaded = read_family_palette(workspace_root, bares.get(family, ""))
        if not loaded.get("ok"):
            return [], str(loaded.get("note") or "")
        slots = list(loaded.get("slots") or ())
        palettes[family] = slots
        return slots, str(loaded.get("path") or "")

    base_palette, base_source = _palette(host_family)
    if not base_palette:
        problems.append(f"宿主族 {host_family} 的 palette 不可读 ⇒ 无法反解 X_f：{base_source}")
        return _family_x_result(host_family, host_evidence, "", "", {}, {}, problems, exemptions)

    families = {}
    for family in sorted(bares):
        if family == host_family:
            family_palette, path = base_palette, base_source
            x = identity_3x4()
            votes = len(base_palette)
            max_residual = 0.0
        else:
            family_palette, path = _palette(family)
            if not family_palette:
                problems.append(f"{family}: palette 不可读 ⇒ 无法反解 X_f：{path}")
                continue
            solved = solve_family_x_from_palettes(base_palette, family_palette)
            if not solved.get("ok"):
                problems.append(f"{family}: {solved.get('note')}")
                continue
            x = list(solved["x"])
            votes = int(solved["votes"])
            max_residual = float(solved["max_residual"])
        snapped = is_identity(x)
        if snapped:
            x = identity_3x4()
        duality = duality_check(base_palette, family_palette, x)
        record = {
            "Bare": bares.get(family, ""),
            "Component": "",
            "Draw": "",
            "Source": "palette",
            "SourceFile": path,
            "PaletteSlots": len(family_palette),
            "ObjectMatrix": [],
            "InverseResidual": 0.0,
            "X": [float(value) for value in x],
            "IsIdentity": bool(snapped),
            "AngleDeg": round(_rotation_deg(x), 3),
            "Translation": [round(float(x[3]), 9), round(float(x[7]), 9), round(float(x[11]), 9)],
            "Votes": votes,
            "MaxResidual": max_residual,
            "Duality": duality,
            "DualityStatus": f"反解命中 {duality['hits']}/{duality['checked']}",
        }
        if family != host_family and votes < DUALITY_MIN_HITS:
            # 不代表解错：解是**从**这些命中骨算出来的（残差 float32 量级）。只提示置信度低。
            record["LowConfidence"] = True
            exemptions.append(
                f"{family}: 只用 {votes} 根共享骨反解 X_f（< {DUALITY_MIN_HITS}）"
                "——解仍精确，但独立验证较弱"
            )
        families[family] = record
    result = _family_x_result(
        host_family,
        host_evidence,
        "",
        base_source if os.path.isfile(str(base_source)) else "",
        families,
        {family: list(entry["X"]) for family, entry in families.items()},
        problems,
        exemptions,
    )
    result["source"] = "palette"
    result["host_palette_slots"] = len(base_palette)
    return result


def _family_x_problems_for(problems, family) -> list:
    """摘掉「只针对某族」的问题行（该族已由另一路解出来时用）。"""
    head = str(family or "") + ":"
    host_head = "宿主族 " + str(family or "") + " "
    return [
        str(item)
        for item in (problems or ())
        if not (str(item).startswith(head) or host_head in str(item))
    ]


def resolve_family_x(
    workspace_root: str,
    *,
    host_family: str,
    family_bares,
    palettes=None,
    host_evidence: str = "",
    mod_root: str = "",
    prefer_freshest: bool = True,
    cross_check_capture: bool = True,
) -> dict:
    """**导出用唯一入口**：逐族挑 X_f 来源，来源逐族留痕（不静默混用）。

    逐族顺序：
      1. ``palette`` —— 从本次工作空间的族 palette 反解 ``X_f = P_base[k] · P_f[j]⁻¹``。
         不读抓帧目录 ⇒ 不会用上一次场景的 object matrix；同一批数据自洽。
      2. ``frame-vs-cb3`` —— palette 解不出的族（单槽族、与宿主族无共享骨）才退回抓帧
         对象矩阵；抓帧目录按「配置里那帧 vs 最新一帧」判新鲜度（``prefer_freshest``），
         换源会写进 ``frame_note`` 与 exemptions。
    两路都给出 X 的族做交叉核对，差 > ``X_SOURCE_CROSS_TOL`` ⇒ 记 problem：说明抓帧与这批
    palette 不是同一帧，抓帧那一路的数字不可信。

    返回体同 :func:`compute_family_x`，额外 ``sources``（族 → 来源）与 ``frame_*`` 字段。
    """
    workspace_root = str(workspace_root or "")
    host_family = str(host_family or "").strip()
    bares = {
        str(family or "").strip(): str(bare or "").strip()
        for family, bare in dict(family_bares or {}).items()
        if str(family or "").strip() and str(bare or "").strip()
    }
    palettes = dict(palettes or {})
    palette_result = compute_family_x_from_palettes(
        workspace_root,
        host_family=host_family,
        family_bares=bares,
        palettes=palettes,
        host_evidence=host_evidence,
    )
    palette_x = dict(palette_result.get("x") or {})
    palette_families = dict(palette_result.get("families") or {})
    problems = list(palette_result.get("problems") or ())
    exemptions = list(palette_result.get("exemptions") or ())
    wanted = sorted(bares)
    missing = [family for family in wanted if family not in palette_x]
    frame_result = {}
    if (missing or cross_check_capture) and host_family in bares:
        # 把宿主一并传进去（compute_family_x 的入参要求），它的 X 恒为单位阵、不会被采用。
        subset = {family: bares[family] for family in (missing or wanted)}
        subset[host_family] = bares[host_family]
        frame_result = compute_family_x(
            workspace_root,
            host_family=host_family,
            family_bares=subset,
            palettes=palettes,
            host_evidence=host_evidence,
            mod_root=mod_root,
            prefer_freshest=prefer_freshest,
        )
        frame_x = dict(frame_result.get("x") or {})
        frame_families = dict(frame_result.get("families") or {})
        frame_problems = [
            str(item) for item in (frame_result.get("problems") or ())
            if not str(item).startswith(str(host_family) + ":")
        ]
        for family in missing:
            if family in frame_x:
                # 该族由抓帧解出 ⇒ 摘掉 palette 那边针对它的抱怨（否则会误判成「部分校准」）
                problems = _family_x_problems_for(problems, family)
                palette_families[family] = dict(frame_families.get(family) or {})
                palette_families[family]["Source"] = "frame-vs-cb3"
                palette_x[family] = list(frame_x[family])
            else:
                problems.extend(frame_problems)
                frame_problems = []
                break
        problems.extend(frame_problems)
        exemptions.extend(list(frame_result.get("exemptions") or ()))
    # 交叉核对：两路都给了 X 的族，看抓帧那一帧与这批 palette 是否同一帧。
    cross = {}
    for family in sorted(set(palette_result.get("x") or {}) & set(frame_result.get("x") or {})):
        if family == host_family:
            continue
        delta = max_abs_diff(
            list((palette_result.get("x") or {})[family]),
            list((frame_result.get("x") or {})[family]),
        )
        cross[family] = round(float(delta), 9)
        if delta > X_SOURCE_CROSS_TOL:
            votes = int((palette_families.get(family) or {}).get("Votes") or 0)
            note = (
                f"{family}: palette 反解（{votes} 根共享骨）与抓帧 vs-cb3 不一致（Δ={delta:.3g}）"
                "⇒ 抓帧很可能不是这批 palette 的来源帧"
            )
            if votes >= DUALITY_MIN_HITS:
                problems.append(note + "；本轮以 palette 为准")
            else:
                # 单根骨反解是「精确但未独立验证」的解：抓帧那一路也是单点证据，不能一票否决。
                exemptions.append(note + "；两路都只有单骨量级的证据，保留 palette 解")
    host_family = palette_result.get("host_family") or host_family
    sources = {}
    for family, entry in palette_families.items():
        sources[family] = str(entry.get("Source") or "palette")
    used_frame = any(value == "frame-vs-cb3" for value in sources.values())
    source_label = "palette+frame-vs-cb3" if used_frame else "palette"
    result = _family_x_result(
        host_family,
        host_evidence,
        str(frame_result.get("source_frame") or ""),
        str(frame_result.get("frame_analysis_path") or ""),
        palette_families,
        {family: list(value) for family, value in palette_x.items()},
        problems,
        exemptions,
    )
    result["source"] = source_label
    result["sources"] = sources
    result["cross_source_delta"] = cross
    result["host_palette_slots"] = int(palette_result.get("host_palette_slots") or 0)
    result["frame_source"] = str(frame_result.get("frame_source") or "")
    result["frame_declared"] = str(frame_result.get("frame_declared") or "")
    result["frame_stale"] = bool(frame_result.get("frame_stale"))
    result["frame_note"] = str(frame_result.get("frame_note") or "")
    return result


def family_x_summary(record) -> str:
    """一行审计摘要（控制台/日志用）。"""
    if not record:
        return "无族级物体矩阵记录"
    families = record.get("families") or {}
    host = str(record.get("host_family") or "")
    source = str(record.get("source") or "frame-vs-cb3")
    frame = str(record.get("source_frame") or "?")
    hits = []
    for entry in families.values():
        hits.append(str((entry.get("Duality") or {}).get("hits", 0)))
    sources = dict(record.get("sources") or {})
    fallback = sorted(family for family, value in sources.items() if value == "frame-vs-cb3")
    if source == "palette":
        origin = "palette 现算"
    elif source == "palette+frame-vs-cb3":
        origin = "palette 现算 + 抓帧兜底 " + str(len(fallback)) + " 族"
    else:
        origin = "抓帧 " + frame
    return (
        "X_f " + str(len(families)) + " 族（宿主 " + host + "；来源 " + origin
        + "；对偶命中 " + "/".join(hits) + "）"
    )


# --------------------------------------------------------------------------- #
# 数据层记录（需求⑦：写进 Config/HIMIMergedSkeleton.json 的顶层 FamilyX）
# --------------------------------------------------------------------------- #


def family_x_audit_record(record, *, host_evidence: str = "", mod_root: str = "") -> dict:
    """把 compute_family_x / resolve_family_x 的结果整理成**可长期审计**的 JSON 记录。"""
    families = {}
    cross = dict((record or {}).get("cross_source_delta") or {})
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
            "Source": entry.get("Source", ""),
            "Votes": int(entry.get("Votes") or 0),
            "MaxResidual": float(entry.get("MaxResidual") or 0.0),
            "CrossSourceDelta": float(cross.get(str(family)) or 0.0),
        }
    return {
        "Algorithm": str(
            record.get("algorithm")
            or "X_f = M_base⁻¹ · M_f（palette 反解 = P_base[k]·P_f[j]⁻¹；写槽 S = X_f · P_f，方案 B）"
        ),
        "Source": str(record.get("source") or "frame-vs-cb3"),
        "Sources": dict(record.get("sources") or {}),
        "SourceFrame": str(record.get("source_frame") or ""),
        "FrameAnalysisPath": str(record.get("frame_analysis_path") or ""),
        "FrameSource": str(record.get("frame_source") or ""),
        "FrameDeclared": str(record.get("frame_declared") or ""),
        "FrameStale": bool(record.get("frame_stale")),
        "FrameNote": str(record.get("frame_note") or ""),
        "CrossSourceTolerance": X_SOURCE_CROSS_TOL,
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

