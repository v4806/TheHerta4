"""t84 跨族借用槽：部署产物核对工具（**只读**默认；`--apply` 才写盘）。

用途（用户验收第 2 条）：对已部署的 ``Meshes\\*-Blend.buf`` 重新统计 —— 修复后每族的索引必须
**全部落在自己块内**，且覆盖集合 = 该族顶点实际引用到的骨集合。

它复用 ``tests/test_himi_merged_runtime.py`` 里那套「无 bpy 的模块加载器」，所以用的是
**生成器自己的函数**（``compute_family_ranges`` / ``family_slot_map`` /
``attach_range_payload`` / ``decode_attach_range_payload`` / ``merged_attach_hlsl_text``），
不复制任何算法。

用法::

    python tools/himi_t84_check.py --mod-root "K:/…/虚构游戏"
    python tools/himi_t84_check.py --mod-root "K:/…/虚构游戏" --apply

``--apply`` 会先 ``shutil.copyfile`` 备份成 ``<名>.bak_t84_<时间戳>`` 再写，写完核对字节数；
不启动游戏、不发 F10。
"""

from __future__ import annotations

import argparse
import os
import shutil
import struct
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if os.path.join(REPO, "tests") not in sys.path:
    sys.path.insert(0, os.path.join(REPO, "tests"))

import test_himi_merged_runtime as harness  # noqa: E402 - 复用无 bpy 的加载器

runtime = harness._runtime
exporter = harness._himi_exporter
# `attach_placement_identity` 在 runtime 里是**函数内**导入的（不在模块命名空间），
# 所以从加载器装好的 contract 模块直接取，避免复制那份 48 字节布局。
_contract = sys.modules[f"{harness.PKG}.common.himi_merged_contract"]


def placement_identity():
    return _contract.attach_placement_identity()

STRIDE = 32   # 4×f32 权重 + 4×u32 索引


def read_bytes(path: str) -> bytes:
    with open(path, "rb") as handle:
        return handle.read()


def blend_rows(payload: bytes):
    """逐行产出 ``(weights, indices)``。"""
    for row in range(len(payload) // STRIDE):
        weights = struct.unpack_from("<4f", payload, row * STRIDE)
        indices = struct.unpack_from("<4i", payload, row * STRIDE + 16)
        yield weights, indices


def blend_index_set(payload: bytes):
    used = set()
    for weights, indices in blend_rows(payload):
        for weight, index in zip(weights, indices):
            if weight > 0.0:
                used.add(int(index))
    return used


def classify_global(index: int, own_base: int, own_slots: int, blocks, local_namespace: bool) -> int:
    """Blend 里的编号 → **旧全局槽**（与生成器 ``_himi_remap_merged_blend_to_blocks`` 同一口径）。

    ``local_namespace`` = 该族的索引集合整个落在 ``[0, own_slots)`` 里（真实产物 ``1b62725d``
    就是这样：3 个顶点写的是**局部 0**，而它的槽是 216）。
    """
    if local_namespace and not (own_base <= index < own_base + own_slots):
        return own_base + index
    return index


def main() -> int:
    parser = argparse.ArgumentParser(description="HIMI t84 跨族借用槽核对 / 应用")
    parser.add_argument("--mod-root", required=True, help="模组目录（含 虚构游戏.ini 的那一层）")
    parser.add_argument("--apply", action="store_true", help="备份后写入修复（默认只读）")
    parser.add_argument("--tag", default="", help="备份标记，默认 bak_t84_<时间戳>")
    args = parser.parse_args()

    mod = os.path.abspath(args.mod_root)
    meshes = os.path.join(mod, "Meshes")
    res = os.path.join(mod, "res")
    tag = args.tag or ("bak_t84_" + time.strftime("%m%d%H%M%S"))

    families = []
    for name in sorted(os.listdir(meshes)):
        if not name.endswith("-AttachRange.buf"):
            continue
        family = name[: -len("-AttachRange.buf")]
        payload = read_bytes(os.path.join(meshes, name))
        info = runtime.decode_attach_range_payload(payload)
        families.append(
            {
                "family": family,
                "base": int(info.get("base", 0)),
                "span": int(info.get("span", 0)),
                "family_x": list(info.get("family_x") or ()),
                "own_index": int(info.get("own_index", 0)),
                "base_index": int(info.get("base_index", 0)),
                "borrow_rows": list(info.get("borrow_rows") or ()),
            }
        )
    if not families:
        print("没有找到任何 <family>-AttachRange.buf")
        return 2

    old_slot_count = max(f["base"] + f["span"] for f in families)
    print(f"部署目录 : {mod}")
    print(f"旧排布   : {len(families)} 族，总槽 {old_slot_count}")
    print()

    # ---- 旧状态：每族索引分布 -------------------------------------------- #
    print("== 修复前（部署产物现状） ==")
    referenced = {}
    blocks = [(f["base"], f["base"] + f["span"], f["family"]) for f in families]
    for f in families:
        payload = read_bytes(os.path.join(meshes, f"{f['family']}-Blend.buf"))
        used = blend_index_set(payload)
        local_ns = bool(used) and all(0 <= i < f["span"] for i in used)
        global_used = {
            classify_global(i, f["base"], f["span"], blocks, local_ns) for i in used
        }
        referenced[f["family"]] = global_used
        inside = sum(1 for i in used if f["base"] <= i < f["base"] + f["span"])
        outside = sorted(i for i in used if not (f["base"] <= i < f["base"] + f["span"]))
        print(
            f"  {f['family']}: 自己槽 [{f['base']},{f['base'] + f['span']})  "
            f"常用索引 {len(used)} 个，落在自己槽内 {inside}/{len(used)}"
            + (f"，越界 {outside}" if outside else "")
            + ("  [命名空间=局部]" if local_ns else "")
        )

    # ---- 新排布（生成器函数） -------------------------------------------- #
    parts = [
        type("_Part", (), {"family_key": f["family"],
                           "slot_values": tuple(range(f["base"], f["base"] + f["span"]))})()
        for f in families
    ]
    ranges = runtime.compute_family_ranges(
        parts, old_slot_count, complete=False, borrowed_slots=referenced
    )
    new_slot_count = runtime.ranges_slot_count(ranges)
    print()
    print("== 新槽位规划（compute_family_ranges(borrowed_slots=…)） ==")
    for family, info in sorted((str(k), dict(v)) for k, v in ranges.items()):
        data = dict(info)
        if not bool(data.get("own_block", False)):
            print(f"  {family}: 借位族（无独立块）")
            continue
        base = int(data.get("base", 0))
        span = int(data.get("slots", 0))
        block = int(data.get("block_slots", span))
        borrow = list(data.get("borrow") or ())
        print(
            f"  {family}: base={base} span={span} block={block} "
            f"own_base={data.get('own_base', base)} family_index={data.get('family_index', 0)}"
        )
        for row in borrow:
            print(
                f"      借用槽 {row['target_slot']} ← {row['source_family']}"
                f" 的局部 {row['source_local']}（源块槽 {row['source_slot']}，"
                f"族序号 {row['source_family_index']}）"
            )
    print(f"  新总槽数 = {new_slot_count}（旧 {old_slot_count}，追加 "
          f"{new_slot_count - old_slot_count} 个块尾借用槽）")

    # ---- 修复后的索引分布（用生成器映射算，不写盘） ---------------------- #
    slot_maps = runtime.family_slot_map(ranges)
    print()
    print("== 修复后（按生成器映射推演；未写盘） ==")
    ok = True
    for f in families:
        family = f["family"]
        mapping = slot_maps.get(family, {})
        info = dict(ranges.get(family) or {})
        if not mapping:
            print(f"  {family}: 无映射（借位族）")
            continue
        base = int(info.get("base", 0))
        block = int(info.get("block_slots", info.get("slots", 0)))
        payload = read_bytes(os.path.join(meshes, f"{family}-Blend.buf"))
        used = blend_index_set(payload)
        local_ns = bool(used) and all(0 <= i < f["span"] for i in used)
        targets = set()
        unmapped = []
        for index in used:
            global_slot = classify_global(index, f["base"], f["span"], blocks, local_ns)
            target = mapping.get(global_slot)
            if target is None:
                unmapped.append((index, global_slot))
            else:
                targets.add(target)
        inside = {t for t in targets if base <= t < base + block}
        cover_expected = {
            mapping[g] for g in referenced[family] if g in mapping
        }
        good = not unmapped and inside == targets and targets == cover_expected
        ok = ok and good
        print(
            f"  {family}: 目标槽 {sorted(targets)}  全部落在 [{base},{base + block}) = "
            f"{'是' if inside == targets else '否'}  覆盖=引用集合 = "
            f"{'是' if targets == cover_expected else '否'}"
            + (f"  未映射 {unmapped}" if unmapped else "")
        )

    if not args.apply:
        print()
        print("（只读模式：没有写入任何文件。加 --apply 才会备份并写盘。）")
        return 0 if ok else 1

    # ---- 写盘 ------------------------------------------------------------- #
    print()
    print(f"== 应用修复（备份标记 {tag}） ==")
    backups = []

    def backup(path: str) -> None:
        if not os.path.isfile(path):
            return
        target = f"{path}.{tag}"
        shutil.copyfile(path, target)
        backups.append((path, target, os.path.getsize(path), os.path.getsize(target)))

    case_pairs = []
    for family in [f["family"] for f in families]:
        case_pairs.append(os.path.join(meshes, f"{family}-AttachRange.buf"))
        case_pairs.append(os.path.join(meshes, f"{family}-Blend.buf"))
    hlsl = os.path.join(res, "himi_attach.hlsl")
    case_pairs.append(hlsl)
    skeleton = None
    for name in sorted(os.listdir(meshes)):
        if name.startswith("role_") and name.endswith("-MergedSkeleton.buf"):
            skeleton = os.path.join(meshes, name)
            case_pairs.append(skeleton)
    for path in case_pairs:
        backup(path)
    for original, copied, size_a, size_b in backups:
        print(f"  备份 {os.path.basename(original)} → {os.path.basename(copied)} "
              f"({size_a} B, 副本 {size_b} B)")

    fanout = runtime.borrow_rows_by_source_family(ranges)
    for f in families:
        family = f["family"]
        info = dict(ranges.get(family) or {})
        rows = list(info.get("borrow") or ())
        payload = runtime.attach_range_payload(
            int(info.get("base", f["base"])),
            int(info.get("slots", f["span"])),
            placement=placement_identity(),            family_x=f["family_x"] or None,
            indices=(int(info.get("family_index", f["own_index"])), f["base_index"]),
            borrow_rows=fanout.get(family),
        )
        path = os.path.join(meshes, f"{family}-AttachRange.buf")
        with open(path, "wb") as handle:
            handle.write(payload)
        written = os.path.getsize(path)
        # 写回后**解码核对**：字节数 + 借用表逐行（不是靠写的时候心里有数）
        back = runtime.decode_attach_range_payload(read_bytes(path))
        rows_back = list(back.get("borrow_rows") or ())
        print(
            f"  {os.path.basename(path)}: 写 {len(payload)} B / 读回 {written} B "
            f"{'OK' if written == len(payload) else '不符!'}  "
            f"base={back.get('base')} span={back.get('span')} 借用行={len(rows_back)}"
            + (
                "  ["
                + "、".join(
                    f"槽{int(row['target_slot'])}←族{int(row['source_family_index'])}"
                    f"局部{int(row['source_local'])}"
                    for row in rows_back
                )
                + "]"
                if rows_back
                else ""
            )
        )

    for f in families:
        family = f["family"]
        mapping = slot_maps.get(family, {})
        if not mapping:
            continue
        path = os.path.join(meshes, f"{family}-Blend.buf")
        raw = read_bytes(path)
        buf = bytearray(raw)
        # 逐族算一次命名空间判据（放在行循环里会变成 O(行²)，18k 行 ⇒ 等于挂死）
        used = blend_index_set(raw)
        local_ns = bool(used) and all(0 <= i < f["span"] for i in used)
        for row in range(len(buf) // STRIDE):
            weights = struct.unpack_from("<4f", buf, row * STRIDE)
            indices = list(struct.unpack_from("<4i", buf, row * STRIDE + 16))
            changed = False
            for channel, (weight, index) in enumerate(zip(weights, indices)):
                if weight <= 0.0:
                    continue
                global_slot = classify_global(int(index), f["base"], f["span"], blocks, local_ns)
                target = mapping.get(global_slot)
                if target is not None and int(target) != int(index):
                    indices[channel] = int(target)
                    changed = True
            if changed:
                struct.pack_into("<4i", buf, row * STRIDE + 16, *indices)
        if bytes(buf) != raw:
            with open(path, "wb") as handle:
                handle.write(bytes(buf))
        print(f"  {os.path.basename(path)}: {os.path.getsize(path)} B")

    # 骨架：补齐到新槽数（追加零槽，由 attach CS 每帧覆盖）
    if skeleton:
        expected = new_slot_count * runtime.MERGED_SKELETON_STRIDE
        payload = read_bytes(skeleton)
        if len(payload) != expected:
            payload = payload[:expected] + b"\x00" * max(0, expected - len(payload))
            with open(skeleton, "wb") as handle:
                handle.write(payload)
        print(f"  {os.path.basename(skeleton)}: {os.path.getsize(skeleton)} B"
              f"（期望 {expected} = {new_slot_count} × {runtime.MERGED_SKELETON_STRIDE}）")

    # CS：用生成器渲染（基址表 = 本次排布）
    decl = runtime.build_merged_skeleton_decl(runtime.REFERENCE_ROLE_KEY, new_slot_count)
    table = exporter.merged_attach_base_table_lines(ranges)
    text = exporter.merged_attach_hlsl_text(decl, base_table_lines=table)
    problems = list(exporter.merged_attach_hlsl_problems(text) or ())
    problems += list(exporter.merged_attach_borrow_problems(text) or ())
    if problems:
        print("  !! CS 自检未通过，**没有写** res/himi_attach.hlsl：")
        for line in problems:
            print("     - " + str(line))
        return 1
    with open(hlsl, "wb") as handle:
        handle.write(text.encode("utf-8"))
    print(f"  {os.path.basename(hlsl)}: {os.path.getsize(hlsl)} B（基址表 {len(table)} 行）")
    print()
    print("完成。ini 的 array= 需要等于 3×槽数；本工具只在**不一致**时提醒，不改 ini：")
    ini_path = [p for p in os.listdir(mod) if p.lower().endswith(".ini")]
    for name in ini_path:
        text = read_bytes(os.path.join(mod, name)).decode("utf-8", "ignore")
        for line in text.splitlines():
            strip = line.strip()
            if strip.startswith("array =") and "MergedSkeleton" in text[max(0, text.find(strip) - 400):text.find(strip)]:
                print(f"     {name}: 见到 {strip}（期望 array = {new_slot_count * 3}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
