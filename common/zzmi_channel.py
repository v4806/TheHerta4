"""ZZMI 合并骨架 —— 「共享骨图 + 通道骨」判定（t75 唯一判定口径）。

用户拍板（2026-09-18）后的**唯一**实例判定口径：

1. 由每个部件的 ``vg_map``（局部骨骼 id → 合并骨架全局槽位）构建**共享骨图**：
   顶点 = 部件，边 = 两部件共享的全局槽位集合，权重 = 共享骨数。
2. 在**连通分量**内选**通道骨**：候选是「该分量内被 ≥2 个部件引用的全局槽位」
   （= 分量内真实共享的骨，而不是每个部件各自独占的槽）；分量内一个都没有时，
   退化为「本分量各部件引用槽位的最小值」。候选排序键依次为
   (引用部件数, 跨部件顶点权重合计, 槽位号)——共享件数最多优先，其次带顶点
   权重最多，最后按槽位号保证确定性。
3. 分量内每个部件用**自己 palette 里映射到该通道槽位的本地下标**参与精确哈希
   （``vs-t0->HashRegion(48*local, 48)``）——同一物理骨在同一实例同一帧内
   **逐字节相同**（t73 实测：G2 槽 185/200、G0 槽 0 跨件写入逐位相同），
   所以精确匹配下同分量的部件会算出同一个键 ⇒ 同实例进同一槽。

**为什么不再用「全组共享骨」（旧 ``_merged_group_pose_anchor_slot``）**：
旧判据取全组 vg_map 值集合的**交集**，只要有一件（叶瞬光01 的 ``8c8de427``，
槽 50..55 与谁都不共骨）就让全组交集为空 ⇒ 整组静默退回出现次口径
（``no_shared_canonical_bone``）。按共享骨图的**连通分量**讨论后，
G0 的 ``{0,35..46}`` 共享骨对五个部件依然可用（t73 实测 11 个共用槽）。

**为什么必须逐字节精确哈希**：同一实例跨部件的同一根骨逐位相同（Δ=0），
而两个实例的差极小（G0 槽 0 只有 1.4e-7…8.2e-6、G2 槽 185 = 1.32e-3），
远小于任何量化格 ⇒ ``SpatialHash(..., 0.01)`` 会把两个实例并键（实测并键率
最高 99/135）。因此只用 ``HashRegion`` + ``pool_index_type = fifo``。

本模块是**纯函数**，不依赖 bpy / 全局配置，导入期与导出期共用同一实现。
"""

from __future__ import annotations

import hashlib
from typing import Iterable

# 通道计划缓存算法版本：判定口径或序列化形态变更时递增。
# v1：共享骨图连通分量 + 通道骨选择 + 逐部件本地下标/权重/诊断记录。
ZZMI_CHANNEL_PLAN_VERSION = 1

# 通道骨哈希键长：1 根骨 = 12 floats = 48 字节。
ZZMI_CHANNEL_HASH_BYTES = 48

# 部件级诊断码（写进产物注释与工作区缓存，绝不静默）。
CHANNEL_DIAG_SHARED = "shared_connecting_bone"
CHANNEL_DIAG_FALLBACK = "no_shared_bone_in_component"
CHANNEL_DIAG_UNRESOLVED = "no_usable_channel_bone"


def vg_map_int_keys(vg_map: dict | None) -> dict[int, int]:
    """把 ``vg_map`` 规范化为 ``{int local: int slot}``（忽略无法解析的项）。"""
    normalized: dict[int, int] = {}
    if not isinstance(vg_map, dict):
        return normalized
    for raw_local, raw_slot in vg_map.items():
        try:
            normalized[int(raw_local)] = int(raw_slot)
        except (TypeError, ValueError):
            continue
    return normalized


def shared_slot_statistics(
    components: Iterable[dict],
) -> dict[int, tuple[int, int]]:
    """全局槽位 → ``(引用部件数, 跨部件顶点权重合计)``。

    「引用部件数」按 ``vg_map`` **值**去重统计（同一部件映射到同一槽位的多个
    本地骨只算一次）；「顶点权重合计」= 各部件上报的该槽位权重之和，缺省 0。
    """
    stats: dict[int, list[int]] = {}
    for component in components:
        vg_map = vg_map_int_keys(component.get("vg_map"))
        weights = component.get("slot_weights") or {}
        referenced = sorted(set(vg_map.values()))
        for slot in referenced:
            entry = stats.setdefault(int(slot), [0, 0])
            entry[0] += 1
            try:
                entry[1] += int(round(float(weights.get(slot, weights.get(str(slot), 0)) or 0)))
            except (TypeError, ValueError):
                continue
    return {slot: (int(count), int(weight)) for slot, (count, weight) in stats.items()}


def shared_bone_components(components: list[dict]) -> list[list[int]]:
    """共享骨图的**连通分量**（返回组件下标列表，按最小下标升序）。

    边存在 ⟺ 两个部件引用同一根骨头。**骨头的口径按 `identity_tokens` 决定**
    （``common/zzmi_channel.bone_identity_tokens``）：

    - 有 ``bone_ids``（骨名/编号身份，t78 #3 的**正确**口径）⇒ 按**骨名身份**建边；
    - 只有 ``vg_map`` ⇒ 退化为「共享同一个合并骨架全局槽位」。这是**弱代理**：
      槽号是导入期按「同帧 palette 矩阵 + 驱动签名」去重出来的，t78 实测尾巴
      ``869976a3`` / 发饰 ``8c8de427`` 与任何件都没有**同名骨**，却可能因为槽号
      恰好相同而被误连（跨帧矩阵不能当身份依据，见 `bone_identity_digest`）。
      调用方必须把实际用到的口径写进产物（``channel_identity_basis``），
      不得让读者以为槽号就是身份。
    """
    tokens = [bone_identity_tokens(c) for c in components]
    count = len(components)
    adjacency: list[set[int]] = [set() for _ in range(count)]
    token_owners: dict[object, list[int]] = {}
    for index, token_set in enumerate(tokens):
        for token in token_set:
            token_owners.setdefault(token, []).append(index)
    for owners in token_owners.values():
        if len(owners) < 2:
            continue
        for i in range(len(owners)):
            for j in range(i + 1, len(owners)):
                adjacency[owners[i]].add(owners[j])
                adjacency[owners[j]].add(owners[i])

    components_out: list[list[int]] = []
    visited = [False] * count
    for start in range(count):
        if visited[start]:
            continue
        stack = [start]
        visited[start] = True
        members: list[int] = []
        while stack:
            node = stack.pop()
            members.append(node)
            for neighbor in sorted(adjacency[node]):
                if not visited[neighbor]:
                    visited[neighbor] = True
                    stack.append(neighbor)
        components_out.append(sorted(members))
    return components_out


def _normalize_group(component: dict) -> int:
    try:
        return int(component.get("skeleton_group", 0) or 0)
    except (TypeError, ValueError):
        return 0


def member_channel_candidates(
    components: list[dict],
    members: list[int],
    component_index: int,
) -> list[tuple[str, int, int, int]]:
    """本部件在**指定判定集合**内的共享通道候选（已排序）。

    返回 ``(token, 全局槽位, 本地下标, 引用成员数, 跨部件权重合计)``，只保留
    「该集合内被 ≥2 个成员引用」的骨头身份——这是键可比的**必要条件**：只有
    同一根骨被两个部件都映射到，两者的 ``HashRegion(48*local, 48)`` 才读同一
    段 48 字节。

    排序键：``(-引用成员数, -跨部件权重合计, 全局槽位, token)``——覆盖最广的骨
    优先，其次带顶点权重最多，最后按槽位号/token 保证确定性。

    调用方必须再用 ``covers_all_members`` 过滤（F2）：分量是「共享任意 token」的
    传递闭包，链式分量（A-1-B-2-C）里 A 与 C 会各自选到**不同的**骨 ⇒ 两件都
    ``cross_part=True`` 却读两根不同物理骨 ⇒ 同实例算出不同键 ⇒ 落不同池槽
    （＝本次变更要消灭的「槽位永远对不上」）。
    """
    if component_index < 0 or component_index >= len(components):
        return []
    current = components[component_index]
    current_tokens = _token_by_local(current)
    vg_map = vg_map_int_keys(current.get("vg_map"))
    if not vg_map:
        return []

    # token → (引用成员数, 跨部件顶点权重合计)。统计范围 = 判定集合（分量），
    # 不是整个骨架组：同组其它分量的引用不该抬高本分量的候选门槛。
    token_counts: dict[str, int] = {}
    token_weights: dict[str, int] = {}
    for member_index in members:
        if not (0 <= int(member_index) < len(components)):
            continue
        other = components[int(member_index)]
        other_map = vg_map_int_keys(other.get("vg_map"))
        other_tokens = _token_by_local(other)
        other_weights = other.get("slot_weights") or {}
        seen_tokens: set[str] = set()
        for local, slot in other_map.items():
            token = other_tokens.get(int(local)) or f"slot:{int(slot)}"
            if token in seen_tokens:
                continue
            seen_tokens.add(token)
            token_counts[token] = token_counts.get(token, 0) + 1
            try:
                token_weights[token] = token_weights.get(token, 0) + int(
                    round(
                        float(
                            other_weights.get(
                                int(slot), other_weights.get(str(slot), 0)
                            )
                            or 0
                        )
                    )
                )
            except (TypeError, ValueError):
                continue

    token_entries: dict[str, tuple[int, int]] = {}
    for local, slot in sorted(vg_map.items()):
        token = current_tokens.get(int(local))
        if token is None:
            token = f"slot:{int(slot)}"
        token_entries.setdefault(token, (int(slot), int(local)))

    candidates = [
        (
            token,
            slot,
            local,
            int(token_counts.get(token, 0)),
            int(token_weights.get(token, 0)),
        )
        for token, (slot, local) in token_entries.items()
        if token_counts.get(token, 0) >= 2
    ]
    candidates.sort(
        key=lambda item: (
            -item[3],
            -item[4],
            int(item[1]),
            item[0],
        )
    )
    return candidates


def select_channel_plan(
    components: list[dict],
    group_of: dict[str, int] | None = None,
) -> dict[str, dict]:
    """为每个部件选**通道骨**，返回 ``draw_ib -> 通道记录``。

    ``components`` 每项至少需要 ``draw_ib`` / ``vg_map``；可选 ``skeleton_group``
    （缺省用 ``group_of[draw_ib]``）与 ``slot_weights``（全局槽位 → 顶点权重合计）。

    记录字段：

    - ``channel_slot``：通道骨的**全局槽位**（跨部件一致，用于复核）。
    - ``channel_local``：通道骨在**本部件 palette** 里的本地骨下标（算键用）。
    - ``channel_shared_components``：本分量内引用该通道槽位的部件数。
    - ``channel_shared_weight``：该通道槽位的跨部件顶点权重合计。
    - ``channel_static_root``：是否被本分量**全部**部件引用（静态根候选）。
    - ``channel_reason``：选择理由（见 ``CHANNEL_DIAG_*``）。
    - ``channel_diagnostic``：非空 = 该部件**没有可用共享通道骨**，已按退化
      候选兜底；消费方必须把该字符串原样写进产物诊断（不得静默）。
    - ``vg_count`` / ``vg_offset``：随记录一起落盘，供导出侧复核一致性。
    """
    records: dict[str, dict] = {}
    if not components:
        return records

    by_group: dict[int, list[dict]] = {}
    for component in components:
        draw_ib = str(component.get("draw_ib") or "")
        if not draw_ib:
            continue
        if group_of is not None and draw_ib in group_of:
            group = int(group_of[draw_ib])
        else:
            group = _normalize_group(component)
        by_group.setdefault(group, []).append(component)

    for group in sorted(by_group):
        group_components = by_group[group]
        group_stats = shared_slot_statistics(group_components)
        group_partitions = shared_bone_components(group_components)
        for component in group_components:
            draw_ib = str(component.get("draw_ib") or "")
            vg_map = vg_map_int_keys(component.get("vg_map"))
            records[draw_ib] = {
                "channel_slot": None,
                "channel_local": None,
                "channel_shared_components": 0,
                "channel_shared_weight": 0,
                "channel_static_root": False,
                "channel_reason": CHANNEL_DIAG_UNRESOLVED,
                "channel_diagnostic": (
                    f"{CHANNEL_DIAG_UNRESOLVED} draw_ib={draw_ib} "
                    "reason=vg_map 为空，无法在 palette 里定位通道骨"
                ),
                "vg_count": int(component.get("vg_count") or 0),
                "vg_offset": int(component.get("vg_offset") or 0),
            }
            if not vg_map:
                continue

            # 分量内共享槽位：槽位统计按**整个骨架组**算，再按「是否被本分量
            # 多个部件引用」过滤——同组不同分量之间本来就不共骨，不会互相影响。
            component_index = next(
                (
                    index
                    for index, item in enumerate(group_components)
                    if str(item.get("draw_ib") or "") == draw_ib
                ),
                -1,
            )
            component_members = next(
                (members for members in group_partitions if component_index in members),
                [component_index],
            )
            member_count = len(component_members)

            local_by_slot: dict[int, int] = {}
            for local, slot in sorted(vg_map.items()):
                local_by_slot.setdefault(int(slot), int(local))

            current = next(
                (
                    item for item in group_components
                    if str(item.get("draw_ib") or "") == draw_ib
                ),
                component,
            )

            # F2：通道骨必须对**判定集合全体成员**可比（同一根全局槽）。
            # 候选只保留被 ≥2 个成员引用的骨头身份，再按「引用数 ≥ 分量成员数」
            # 过滤 ⇒ 分量内只有一个通道槽位、全体成员都能在自己的 palette 里
            # 定位它 ⇒ 同实例跨部件算出同一个键。
            # 链式分量（A-1-B-2-C）里没有人被全体引用 ⇒ 过滤后为空 ⇒ 整个分量
            # 显式退化为「只供骨」（下面的 else 分支，逐部件点名）。
            all_candidates = member_channel_candidates(
                group_components, component_members, component_index
            )
            shared_candidates = [
                candidate
                for candidate in all_candidates
                if member_count > 1 and candidate[3] >= member_count
            ]
            if shared_candidates:
                token, slot, local, count, weight = shared_candidates[0]
                reason = CHANNEL_DIAG_SHARED
                static_root = bool(member_count and count >= member_count)
                diagnostic = ""
            else:
                # 本分量内没有**全体成员可比**的共享骨：退化到「本部件引用槽位的
                # 最小值」。该键只区分**本部件自己**的两个实例（同分量其它部件
                # 读的是另一根骨 ⇒ 键不可比），保留 attach / palette 捕获（它是
                # 这些槽位的唯一写入者）。
                #
                # F2：这里必须把「为什么退化」写清楚——链式分量不是「没有共享骨」
                # （分量本身就是靠共享骨连起来的），而是「没有任何一根骨被全体
                # 成员引用」。产物里逐部件点名，绝不静默。
                slot = min(local_by_slot)
                local = int(local_by_slot[slot])
                # F3：退化记录的 `channel_shared_components` 口径必须与
                # `channel_reason` 一致。共享分支的 count = 「引用该通道骨的
                # **成员数**」（骨头身份口径）；退化分支的键只对本部件自己两个
                # 实例可区分，**没有任何其它成员与它共用这个通道**，所以 count
                # 必须写 **1**，不能拿槽号统计（`group_stats`，槽号口径、按整个
                # 骨架组计数）去填——那会让读者看到「shared=4 却 reason=退化」
                # 的自相矛盾（复核实测：G2 `869976a3`）。
                count = 1
                weight = int(group_stats.get(slot, (1, 0))[1])
                token = ""
                reason = CHANNEL_DIAG_FALLBACK
                static_root = False
                diagnostic = (
                    f"{CHANNEL_DIAG_FALLBACK} draw_ib={draw_ib} slot={slot} "
                    f"local={local} component_members={member_count} "
                    f"component_member_ids={','.join(str(i) for i in component_members)} "
                    "reason=该共享骨连通分量内不存在被**全体成员**引用的骨头"
                    "（链式分量：各成员只能各自选到不同的共享骨 ⇒ 键不可比），"
                    "已退化为本部件最小槽位通道（只对本部件两实例可区分，不参与"
                    "跨部件实例判定）"
                )
            records[draw_ib] = {
                "channel_slot": int(slot),
                "channel_local": int(local),
                "channel_shared_components": int(count),
                "channel_shared_weight": int(weight),
                "channel_static_root": static_root,
                "channel_reason": reason,
                "channel_diagnostic": diagnostic,
                # t78 #3：本记录是**按什么口径**选出来的（骨名身份 / 槽号弱代理）。
                # 消费方必须能一眼看出"槽号 ≠ 骨头身份"，不得把弱代理当身份证据。
                "channel_identity_basis": identity_basis(current),
                "channel_identity_token": str(token),
                "vg_count": int(component.get("vg_count") or 0),
                "vg_offset": int(component.get("vg_offset") or 0),
            }
    return records


def _token_by_local(component: dict) -> dict[int, str]:
    """本部件「本地骨下标 → 骨头身份 token」。

    ``bone_ids``（``{local: 骨名/编号}``）存在时用骨名身份 token；否则空表
    （调用方按槽号兜底）。
    """
    bone_ids = component.get("bone_ids")
    mapping: dict[int, str] = {}
    if not isinstance(bone_ids, dict):
        return mapping
    for raw_local, raw_name in bone_ids.items():
        try:
            local = int(raw_local)
        except (TypeError, ValueError):
            continue
        name = str(raw_name or "").strip()
        if name:
            mapping[local] = f"name:{name}"
    return mapping


def bone_identity_tokens(component: dict) -> set:
    """部件引用的**骨头身份**集合（建边用；口径见 `shared_bone_components`）。

    - ``bone_ids`` 存在（``{local: 骨名/编号}``，t78 #3 的正确口径）⇒ 返回骨名身份；
      骨名身份与槽位号无关，跨帧矩阵更是完全不参与。
    - 否则退回 ``vg_map`` 的**全局槽位号**（弱代理，必须记进
      ``channel_identity_basis``）。
    """
    bone_ids = component.get("bone_ids")
    if isinstance(bone_ids, dict) and bone_ids:
        tokens = set()
        for raw in bone_ids.values():
            name = str(raw or "").strip()
            if name:
                tokens.add(f"name:{name}")
        if tokens:
            return tokens
    return {f"slot:{int(slot)}" for slot in vg_map_int_keys(component.get("vg_map")).values()}


def identity_basis(component: dict) -> str:
    """本部件实际使用的骨头身份口径。

    - ``bone_identity``：``bone_ids``（``{local: 骨名/编号}``，t78 #3 的**正确**
      口径）在位——骨名身份与槽位号无关，跨帧矩阵完全不参与。
    - ``local_slot_map``：只有 ``vg_map``。此时身份 = ``{本地下标: 合并骨架全局
      槽位}``，而**槽位号本身**由 `ZZMIBoneMapBuilder.build_vg_maps` 的**当帧
      palette 矩阵 bitwise 去重** + 刚性部件质心门控产生 ⇒ 它是当帧矩阵字节的
      派生量，**不是帧不变量、更不是骨名/编号身份**（F4）。生产链路上
      ``bone_ids`` 从未被赋值（导入侧与导出侧补算都不传），所以现状**永远是**
      这个弱代理 —— 影响面 = 全部判定，不是某个部件。
    """
    bone_ids = component.get("bone_ids")
    if isinstance(bone_ids, dict) and bone_ids:
        return "bone_identity"
    return "local_slot_map"


def bone_identity_basis_note(basis: str) -> str:
    """身份口径的人读说明（写进产物注释，避免"槽号 = 身份"的误读）。"""
    if basis == "bone_identity":
        return (
            "身份口径 = 骨名/编号身份（bone_ids）：与槽位号无关，跨帧矩阵不参与"
        )
    return (
        "身份口径 = local_slot_map（**弱代理**）：槽位号由当帧 palette 矩阵的 "
        "bitwise 去重派生，不是骨名/编号身份、也不是帧不变量；"
        "生产链路当前没有骨名真值（bone_ids 未接入）⇒ 全部判定都用这个代理"
    )


def bone_identity_digest(vg_map: dict | None) -> str:
    """部件骨骼集合的**身份摘要**（``{本地下标: 合并骨架全局槽位}`` 的规范化哈希）。

    ⚠️ **口径更正（F4，复核发现）**：本函数**不是**「按骨名/编号对齐」的身份
    指纹。它哈希的 ``slot`` 来自 `ZZMIBoneMapBuilder.build_vg_maps`，而该去重键是
    **当帧 palette 的原始矩阵字节**（``bone_key = palette[local_id].tobytes()``）
    加刚性部件的质心门控 ⇒ 本摘要是**当帧矩阵字节经去重后的派生量**，
    **不是帧不变量**：换帧后若某两根骨的 bitwise 命中关系变化，槽号与摘要都会变。

    - 因此它只能用作「这份缓存是不是**同一套 local→slot 映射**」的复核依据，
      不得当作"同一根物理骨"的身份证据；
    - 真正的骨名/编号身份（``bone_ids``）目前**没有接入生产链路**
      （见 `identity_basis`），所以整套通道判定当前都用 `local_slot_map` 弱代理；
    - 当帧矩阵另有指纹（`same_frame_matrix_digest`），只证明"同一次抓帧"，同样
      不参与身份判定（跨帧比矩阵会把同一根骨判成不同骨，或反之）。
    """
    digest = hashlib.sha256()
    digest.update(b"zzmi-bone-identity-v1\n")
    for local, slot in sorted(vg_map_int_keys(vg_map).items()):
        digest.update(f"{local}:{slot};".encode("utf-8"))
    return digest.hexdigest()


def same_frame_matrix_digest(palette_bytes: bytes | bytearray | None) -> str:
    """当帧 palette 矩阵块的指纹（**非身份**，只证明"同一次抓帧"）。

    单独的字段、单独的语义：把它写进缓存是为了让"缓存是否来自同一次导入"可复核，
    任何去重/身份判定都**不得**读它（t78：跨帧比矩阵会把同一根骨判成不同骨）。
    """
    digest = hashlib.sha256()
    digest.update(b"zzmi-same-frame-matrix-v1\n")
    if palette_bytes:
        digest.update(bytes(palette_bytes))
    return digest.hexdigest()


def is_cross_part_channel(record: dict | None, min_parts: int = 2) -> bool:
    """本通道记录是否是**真正跨部件共享**的通道骨（≥ ``min_parts`` 个部件引用）。

    只有它才让部件**参与跨部件实例判定**（键 = 该骨 48 字节矩阵的逐字节哈希）：
    同一连通分量内的部件映射到同一根物理骨 ⇒ 同实例必然算出同一个键。

    分量内没有共享骨时 ``select_channel_plan`` 会给出**退化候选**（本部件最小
    槽位，``channel_shared_components`` = 1）：该键只区分**本部件自己**的两个实例，
    与其它部件的键毫无关系（不同物理骨 ⇒ 不同哈希）。把它当判定输入会让同组的
    两个部件各算各的键、各自占一个池槽 ⇒ 槽位永远对不上。所以这类部件（以及
    缓存里根本没有通道记录的部件）一律按**只供骨、不参与判定**处置——
    保留出现次捕获与 attach（它是自己那些槽位的唯一写入者），但从**所有**门控里
    摘掉（用户 2026-09-18 实机确认：无判定的部件留在门控里会把有键部件的发布
    一起拖死，症状 = 骨骼动画直接卡住）。
    """
    if not isinstance(record, dict):
        return False
    try:
        count = int(record.get("channel_shared_components") or 0)
    except (TypeError, ValueError):
        return False
    if count < int(min_parts):
        return False
    return str(record.get("channel_reason") or "") == CHANNEL_DIAG_SHARED


def channel_plan_from_records(components: list[dict]) -> dict[str, dict]:
    """从缓存记录（``channel_*`` 字段直接落在组件记录上）还原通道计划。

    导出侧只读缓存时用；缺少必需字段的部件不进入结果（调用方按"缓存缺失"
    显式诊断并拒绝导出该部件，绝不静默退化）。
    """
    plan: dict[str, dict] = {}
    for component in components:
        draw_ib = str(component.get("draw_ib") or "")
        record = component.get("channel")
        if not draw_ib or not isinstance(record, dict):
            continue
        try:
            slot = int(record["channel_slot"])
            local = int(record["channel_local"])
        except (KeyError, TypeError, ValueError):
            continue
        if slot < 0 or local < 0:
            continue
        plan[draw_ib] = dict(record)
    return plan


def channel_plan_digest(plan: dict[str, dict]) -> str:
    """通道计划的稳定摘要（用于缓存复核与人读产物注释）。"""
    digest = hashlib.sha256()
    for draw_ib in sorted(plan):
        record = plan[draw_ib]
        digest.update(
            (
                f"{draw_ib}|{record.get('channel_slot')}|{record.get('channel_local')}|"
                f"{record.get('channel_shared_components')}|"
                f"{record.get('channel_reason')};"
            ).encode("utf-8")
        )
    return digest.hexdigest()
