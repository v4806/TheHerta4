"""导出期跨分块边界顶点焊接的纯逻辑（不依赖 bpy）。

对应计划书《合并顶点节点-计划书.md》第 3 / 4.1 节：
- 候选 = 属于某个分块（block > 0）且连到至少一条边界边（拓扑开边）的顶点；
- 按距离阈值划格子，同格内每个分块只保留索引最小的代表；
- 只有同格内出现 >= 2 个不同分块时才输出该组代表，单分块组跳过；
- 不链式扩散：只合并同一格子内的代表。

本模块必须保持纯 Python，供 tests 直接导入。
"""


def compute_weld_groups(positions, boundaries, blocks, distance):
    """计算需要跨分块合并的顶点代表组。

    参数：
        positions: Sequence[Sequence[float]]  顶点坐标 (x, y, z)
        boundaries: Sequence[bool]            该顶点是否连到至少一条边界边
        blocks: Sequence[int]                 顶点所属分块 id，0 = 不参与；1..N 连续
        distance: float > 0                   焊接距离阈值

    返回 dict：
        groups: list[list[int]]               每组代表顶点索引，组内按 block id 升序
        merged_group_count: int               跨 >= 2 个分块、实际合并的组数
        involved_block_count: int             被合并组涉及的全局不同分块数（去重）
        skipped_single_block_count: int       候选组里因只含 1 个分块而跳过的组数
        excluded_non_boundary_count: int      blocks > 0 但不是边界顶点的数量
    """
    if distance is None:
        raise ValueError("weld distance 不能为空")
    distance = float(distance)
    if not (distance > 0.0):
        raise ValueError(f"weld distance 必须大于 0，当前为 {distance!r}")

    vertex_count = len(positions)
    if len(boundaries) != vertex_count or len(blocks) != vertex_count:
        raise ValueError(
            "positions/boundaries/blocks 长度不一致: "
            f"{vertex_count}/{len(boundaries)}/{len(blocks)}"
        )

    # 按格子收集候选顶点（保持顶点扫描顺序，输出稳定）
    cell_to_indices = {}
    excluded_non_boundary_count = 0

    for index in range(vertex_count):
        block_id = int(blocks[index])
        if block_id <= 0:
            # 不属于任何参与焊接的分块，完全忽略
            continue
        if not boundaries[index]:
            excluded_non_boundary_count += 1
            continue
        position = positions[index]
        key = (
            round(float(position[0]) / distance),
            round(float(position[1]) / distance),
            round(float(position[2]) / distance),
        )
        bucket = cell_to_indices.get(key)
        if bucket is None:
            cell_to_indices[key] = [index]
        else:
            bucket.append(index)

    groups = []
    skipped_single_block_count = 0
    involved_blocks = set()

    for indices in cell_to_indices.values():
        # 同一分块只保留索引最小的顶点作代表
        representatives = {}
        for index in indices:
            block_id = int(blocks[index])
            if block_id not in representatives:
                representatives[block_id] = index

        if len(representatives) < 2:
            skipped_single_block_count += 1
            continue

        ordered_block_ids = sorted(representatives)
        groups.append([representatives[block_id] for block_id in ordered_block_ids])
        involved_blocks.update(ordered_block_ids)

    return {
        "groups": groups,
        "merged_group_count": len(groups),
        "involved_block_count": len(involved_blocks),
        "skipped_single_block_count": skipped_single_block_count,
        "excluded_non_boundary_count": excluded_non_boundary_count,
    }
