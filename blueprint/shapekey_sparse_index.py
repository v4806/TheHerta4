"""形态键「顶点命中索引」（稀疏查找 / CSR）。

背景（旧模型，含「优化查找性能」）：着色器对**全部槽位**逐个判断——

    uint packed_idx_slotK = i * num_slots + K;
    uint freq_idx_slotK = vertex_freq_indices[packed_idx_slotK];
    if (freq_idx_slotK != NO_FREQ_INDEX) { ... }

槽位数 = 形态键实际占用的槽位数，于是每个顶点每帧都要跑 num_slots 次
（5 万顶点 × 700 键 ≈ 3500 万次索引读取），而 ``!= NO_FREQ_INDEX`` 的早退在 GPU 上会被
warp divergence 吃掉——一个 warp 的 32 个 lane 里只要有一个命中该槽位，
整段仍要执行。

本模块把这张稠密表**转置**成 CSR 结构：一个顶点只登记它真正命中的键
（典型 2~5 个），着色器只遍历自己命中的条目：

    for (uint e = vertex_entry_start[i]; e < vertex_entry_start[i + 1]; ++e)
    {
        float anim_weight_entry = ShapeKeyWeight[vertex_entry_freq[e]];
        if (anim_weight_entry > 1e-5)
        {
            int packed_index = vertex_entry_packed[e];
            if (packed_index != -1) { ...merged_shapekey_pos_deltas[packed_index]... }
        }
    }

收益：

* 索引数据：(顶点数 × 槽位数 × 4B) → (顶点数 + 1) × 4B + 命中数 × 8B；
* 每顶点索引读取：num_slots 次 → 该顶点实际命中数（2~5）次；
* 逻辑块行数与键数脱钩（不再逐槽位展开，几百行 → 一段有界循环）；
* 权重为 0 的命中项直接跳过位移读取（随机访问内存是这里最贵的一步）。

本模块只做纯计算与文件落盘，不依赖 bpy，可单独单元测试。
"""

import os

try:
    import numpy as np
    NUMPY_AVAILABLE = True
except ImportError:  # pragma: no cover - Blender 内置 numpy，缺失时上层会先行拦截
    np = None
    NUMPY_AVAILABLE = False


#: 稠密 FREQ 表里「该顶点不受此槽位影响」的哨兵值（与 HLSL 的 NO_FREQ_INDEX 一致）。
#:
#: 取值必须落在合法形态键下标（``0 .. len(unique_names)-1``）之外。早期取值 255，
#: 于是形态键总数 > 255 的工程里，下标恰好为 255 的那个键会被撞上：写入端把它
#: 当成空槽位跳过（该键的 freq_indices 全是哨兵），读取端再把它当成空槽位忽略
#: —— 该键在实机上整体失效，且不报任何错。帧表模式下表现为「从该键起累积位移
#: 恒定少一份」，稠密/稀疏模式下表现为该键单独无效。
NO_FREQ_INDEX = 0xFFFFFFFF

#: 同一个哨兵注入 shader 文本时使用的 HLSL 字面量写法。
NO_FREQ_INDEX_HLSL = "0xFFFFFFFFu"

#: 三份稀疏索引占用的 cs-t 寄存器。
#: 取 96/97/98：避开 t50（基础顶点）、t51/t52/t53（合并数据/映射/FREQ）、
#: t75+（非合并紧凑模式逐槽位映射）、t99（旧 FREQ 表）与 t100-t102（拖拽与权重缓冲）。
SPARSE_START_REGISTER = 96
SPARSE_PACKED_REGISTER = 97
SPARSE_FREQ_REGISTER = 98

#: 文件名后缀（资源名后缀见 common/mod_path_compat.py 的 derive_shapekey_vertex_entry_*_resource_name）
SPARSE_START_SUFFIX = "_vertex_entry_start"
SPARSE_PACKED_SUFFIX = "_vertex_entry_packed"
SPARSE_FREQ_SUFFIX = "_vertex_entry_freq"


def build_sparse_vertex_index(freq_indices, packed_indices=None):
    """把稠密 FREQ 表转置成 CSR。

    :param freq_indices: (顶点数, 槽位数) 的 uint 表，``NO_FREQ_INDEX`` 表示「该顶点
        不受此槽位影响」。
    :param packed_indices: 与 ``freq_indices`` 同形状的位移记录下标表（``-1`` 表示该
        (顶点, 槽位) 没有位移数据，即合并索引表里的 -1）；为 ``None`` 时全部记 -1。
    :return: ``(start, packed, freq)``

        * ``start``：uint32，长度 = 顶点数 + 1，第 i 个顶点的条目区间为 ``[start[i], start[i + 1])``
        * ``packed``：int32，长度 = 命中数，条目对应的合并位移记录下标
        * ``freq``：uint32，长度 = 命中数，条目对应的形态键强度下标（FREQ 槽位）

    条目按 (顶点, 槽位) 升序排列：同一顶点的条目在缓冲里连续，槽位号递增。
    """
    if not NUMPY_AVAILABLE:
        raise RuntimeError("顶点命中索引需要 numpy 支持")

    dense = np.asarray(freq_indices, order="C")
    if dense.ndim != 2:
        raise ValueError("顶点命中索引需要二维的 FREQ 表（顶点数 × 槽位数）")

    vertex_count, slot_count = int(dense.shape[0]), int(dense.shape[1])
    if vertex_count < 0 or slot_count < 0:
        raise ValueError("顶点命中索引的 FREQ 表尺寸非法")

    hit_mask = dense != NO_FREQ_INDEX
    counts = hit_mask.sum(axis=1)

    start = np.zeros(vertex_count + 1, dtype=np.uint32)
    if vertex_count:
        start[1:] = np.cumsum(counts).astype(np.uint32, copy=False)

    rows, cols = np.nonzero(hit_mask)
    freq = dense[rows, cols].astype(np.uint32, copy=False)

    if packed_indices is None:
        packed = np.full(freq.shape, -1, dtype=np.int32)
    else:
        packed_dense = np.asarray(packed_indices, order="C")
        if packed_dense.shape != dense.shape:
            raise ValueError(
                f"位移记录下标表尺寸 {packed_dense.shape} 与 FREQ 表尺寸 {dense.shape} 不一致"
            )
        packed = packed_dense[rows, cols].astype(np.int32, copy=False)

    return start, packed, freq


def sparse_buf_paths(meshes_dir, actual_hash):
    """返回三份稀疏索引文件的落盘路径（不创建目录）。"""
    return {
        SPARSE_START_SUFFIX: os.path.join(meshes_dir, f"{actual_hash}-Position{SPARSE_START_SUFFIX}.buf"),
        SPARSE_PACKED_SUFFIX: os.path.join(meshes_dir, f"{actual_hash}-Position{SPARSE_PACKED_SUFFIX}.buf"),
        SPARSE_FREQ_SUFFIX: os.path.join(meshes_dir, f"{actual_hash}-Position{SPARSE_FREQ_SUFFIX}.buf"),
    }


def write_sparse_vertex_index(meshes_dir, actual_hash, freq_indices, packed_indices=None):
    """构建并落盘三份稀疏索引，返回统计信息字典。

    文件名与稠密 FREQ 表（``{hash}-Position_freq_indices.buf``）同级，都在
    ``Meshes0000`` 下——稀疏模式与「优化查找性能」的稠密表**互斥**（只写其中一份）。
    """
    start, packed, freq = build_sparse_vertex_index(freq_indices, packed_indices)
    paths = sparse_buf_paths(meshes_dir, actual_hash)
    os.makedirs(meshes_dir, exist_ok=True)

    for suffix, array in (
        (SPARSE_START_SUFFIX, start),
        (SPARSE_PACKED_SUFFIX, packed),
        (SPARSE_FREQ_SUFFIX, freq),
    ):
        with open(paths[suffix], "wb") as file_obj:
            file_obj.write(array.tobytes())

    dense = np.asarray(freq_indices)
    return {
        "vertex_count": int(start.size) - 1,
        "slot_count": int(dense.shape[1]) if dense.ndim == 2 else 0,
        "entry_count": int(freq.size),
        "packed_count": int(np.count_nonzero(packed != -1)) if packed.size else 0,
        "paths": paths,
    }


def sparse_entry_lookup(start, packed, freq, vertex_count=None):
    """把 CSR 还原成「每顶点命中项列表」，用于校验与等价性测试。

    :return: ``[[(freq_idx, packed_index), ...], ...]``，长度 = 顶点数
    """
    if not NUMPY_AVAILABLE:
        raise RuntimeError("顶点命中索引需要 numpy 支持")

    start = np.asarray(start)
    packed = np.asarray(packed)
    freq = np.asarray(freq)

    total_vertices = int(start.size) - 1 if vertex_count is None else int(vertex_count)
    if vertex_count is not None and int(start.size) - 1 != int(vertex_count):
        raise ValueError("顶点命中索引的行偏移表长度与顶点数不一致")

    lookup = []
    for vertex in range(total_vertices):
        begin = int(start[vertex])
        end = int(start[vertex + 1])
        if begin > end:
            raise ValueError(f"顶点命中索引的行偏移表非单调：顶点 {vertex} 的区间 {begin}..{end}")
        lookup.append([(int(freq[entry]), int(packed[entry])) for entry in range(begin, end)])
    return lookup


def build_sparse_logic_lines(channel_names, accumulation_builder, indent="    ", drag_drive_enabled=False):
    """生成着色器逻辑块（PYTHON-MANAGED LOGIC 段）里的稀疏遍历代码。

    ``drag_drive_enabled`` 时按拖拽驱动重算条目权重：拖拽用的三张常量表
    （``SHAPEKEY_ZONE_IDS`` / ``SHAPEKEY_ND_STAGE_IDS`` / ``SHAPEKEY_SLOT_IDS``）
    与 ``ShapeKeyWeight`` 一样按**形态键序号**索引，而条目里的
    ``vertex_entry_freq`` 恰好就是形态键序号，所以旧模型那段「按 freq 运行时索引」
    的覆盖逻辑可以原样搬进循环体内，不需要为拖拽另开一条渲染路径。

    :param channel_names: 参与累加的通道名（``["position"]`` 或含法线/切线）
    :param accumulation_builder: 累加行渲染器，签名与
        ``SSMTNode_PostProcess_ShapeKey._build_delta_accumulation_lines`` 一致；
        由调用方注入，保证与旧模型使用**同一套**累加代码（避免两份实现走偏）。
    :param drag_drive_enabled: 是否发拖拽驱动权重覆盖块
    """
    for_level = indent
    weight_level = indent + "    "
    packed_level = indent + "        "
    calc_level = indent + "            "

    lines = [
        f"{for_level}// Sparse: only iterate the shape keys this vertex actually hits",
        f"{for_level}uint sparse_entry_start = vertex_entry_start[i];",
        f"{for_level}uint sparse_entry_end = vertex_entry_start[i + 1];",
        f"{for_level}for (uint sparse_entry = sparse_entry_start; sparse_entry < sparse_entry_end; ++sparse_entry)",
        f"{for_level}{{",
    ]
    if drag_drive_enabled:
        lines.extend(
            [
                f"{weight_level}uint sparse_freq_entry = vertex_entry_freq[sparse_entry];",
                f"{weight_level}float anim_weight_entry = ShapeKeyWeight[sparse_freq_entry];",
                f"{weight_level}uint sk_zone_entry = SHAPEKEY_ZONE_IDS[sparse_freq_entry];",
                f"{weight_level}uint sk_nd_stage_entry = SHAPEKEY_ND_STAGE_IDS[sparse_freq_entry];",
                f"{weight_level}uint sk_slot_entry = SHAPEKEY_SLOT_IDS[sparse_freq_entry];",
                f"{weight_level}if (sk_zone_entry != 0xFFFFFFFFu && (sk_nd_stage_entry == 0xFFFFFFFFu || ShapeKeyClickCount[sk_zone_entry] == sk_nd_stage_entry))",
                f"{weight_level}{{",
                f"{weight_level}    anim_weight_entry = ShapeKeyDrive[sk_slot_entry];",
                f"{weight_level}}}",
            ]
        )
    else:
        lines.append(f"{weight_level}float anim_weight_entry = ShapeKeyWeight[vertex_entry_freq[sparse_entry]];")

    lines.extend(
        [
            f"{weight_level}if (anim_weight_entry > 1e-5)",
            f"{weight_level}{{",
            f"{packed_level}int packed_index = vertex_entry_packed[sparse_entry];",
            f"{packed_level}if (packed_index != -1)",
            f"{packed_level}{{",
        ]
    )
    lines.extend(
        accumulation_builder(
            calc_level,
            "_entry",
            "merged_shapekey_pos_deltas[packed_index]",
            "anim_weight_entry",
            channel_names,
        )
    )
    lines.extend([f"{packed_level}}}", f"{weight_level}}}", f"{for_level}}}"])
    return lines
