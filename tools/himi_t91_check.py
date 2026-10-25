"""t91 借用槽：**已写出产物**的三方一致性自检（只读，不改盘）。

背景（口径见 ``common/himi_merged_runtime.py`` 的 ``compute_family_ranges`` 与
``ui/universal/himi.py`` 的 ``merged_attach_hlsl_text``）：跨族借用骨不随 AttachRange 载荷下发，
而是每族一份 ``-BorrowMap.buf``（``cs-t4``），行 = ``(目标槽, 来源族序号, 源局部槽, 0)``，
来源族的 palette 以 ``cs-tN`` 副本绑进每个 attach 段。因此产物里**有四份数据必须互相对得上**：

1. ``Meshes/<族>-AttachRange.buf`` —— 身份载荷，t91 起**恒 128 B**（``[7].z`` 借用行数必须为 0）；
2. ``Meshes/<族>-BorrowMap.buf`` —— 借用表，``16 * ini 里的 array`` 字节；
3. ``Meshes/<族>-Blend.buf`` —— 顶点骨号，必须已重定基到**本族块** ``[base, base+block)``；
4. ``虚构游戏.ini`` / ``res/himi_attach.hlsl`` —— 骨架 array、Dispatch、寄存器绑定、SLOTS 宏。

用「生成器自己的函数」解码，不复制任何布局常量：``decode_attach_range_payload`` /
``decode_borrow_map_payload`` / ``merged_attach_hlsl_problems`` /
``merged_attach_borrow_problems``。模块加载复用 ``tests/test_himi_merged_runtime.py`` 那套无 bpy
的加载器（与 ``tools/himi_t84_check.py`` 同一姿势）。

用法::

    python tools/himi_t91_check.py --mod-root "K:/…/虚构游戏"
    python tools/himi_t91_check.py --mod-root "K:/…/虚构游戏" --ini 虚构游戏.ini --hlsl res/himi_attach.hlsl
    python tools/himi_t91_check.py --self-test      # 造一份合格产物再故意改坏一行，验自检本身

退出码：0 = 四份数据一致；1 = 有不一致（逐条打印中文诊断）；2 = 参数/读盘问题。
"""

from __future__ import annotations

import argparse
import os
import struct
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if os.path.join(REPO, "tests") not in sys.path:
    sys.path.insert(0, os.path.join(REPO, "tests"))

import test_himi_merged_runtime as harness  # noqa: E402 - 复用无 bpy 的模块加载器

runtime = harness._runtime
himi = sys.modules[f"{harness.PKG}.ui.universal.himi"]

ATTACH_RANGE_BYTES = runtime.ATTACH_RANGE_BYTES
BORROW_MAP_ROW_BYTES = runtime.BORROW_MAP_ROW_BYTES
BORROW_MAP_EMPTY_SENTINEL = runtime.BORROW_MAP_EMPTY_SENTINEL
SLOT_STRIDE = runtime.MERGED_SKELETON_STRIDE  # 48 = 3 行 × 4 float
BLEND_STRIDE = 32  # 4×f32 权重 + 4×u32 索引，与生成器 `_himi_remap_merged_blend_to_blocks` 同口径


# ----------------------------------------------------------------------------- 读 ini


def parse_ini_blocks(text: str) -> dict:
    """``{段名: [(键, 值), …]}``；注释与空行丢弃，键名统一小写去空格。"""
    blocks: dict = {}
    current = ""
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(";"):
            continue
        if line.startswith("[") and line.endswith("]"):
            current = line[1:-1].strip()
            blocks.setdefault(current, [])
            continue
        if not current or "=" not in line:
            continue
        key, value = line.split("=", 1)
        blocks[current].append((key.strip().lower(), value.strip()))
    return blocks


def block_fields(blocks: dict, name: str) -> dict:
    """段内键值 → **首次出现**的那个值。

    3Dmigoto 的 attach 段会在 ``Dispatch`` 之后写 ``cs-t4 = null`` 之类解绑行；
    判「绑了什么」必须看第一次绑定，不能看最后一次（最后一次是解绑）。
    """
    fields: dict = {}
    for key, value in blocks.get(name, []):
        fields.setdefault(key, value)
    return fields


def section_names(blocks: dict, prefix: str) -> list:
    return sorted(name for name in blocks if name.startswith(prefix))


def ini_int(fields: dict, key: str, default: int = 0) -> int:
    try:
        return int(fields.get(key, default))
    except (TypeError, ValueError):
        return default


# ----------------------------------------------------------------------------- 读盘


def read_bytes(path: str) -> bytes:
    with open(path, "rb") as handle:
        return handle.read()


def resolve(root: str, filename: str) -> str:
    """ini 里的 filename 相对 mod 根；也容忍它带 Meshes/ 前缀。"""
    return os.path.join(root, filename.replace("/", os.sep))


def blend_used_indices(payload: bytes) -> set:
    """权重 > 0 的槽号集合（与生成器一致：只看权重非零的那条通道）。"""
    used = set()
    for row in range(len(payload) // BLEND_STRIDE):
        weights = struct.unpack_from("<4f", payload, row * BLEND_STRIDE)
        indices = struct.unpack_from("<4i", payload, row * BLEND_STRIDE + 16)
        for weight, index in zip(weights, indices):
            if weight > 0.0:
                used.add(int(index))
    return used


# ----------------------------------------------------------------------------- 主审计


def audit(root: str, ini_path: str, hlsl_path: str) -> list:
    problems: list = []

    def bad(message: str) -> None:
        problems.append(message)

    blocks = parse_ini_blocks(open(ini_path, encoding="utf-8").read())
    skeleton_names = section_names(blocks, runtime.MERGED_SKELETON_RESOURCE_PREFIX)
    if not skeleton_names:
        bad(f"ini 里找不到 {runtime.MERGED_SKELETON_RESOURCE_PREFIX}* 骨架资源段")
        return problems
    skeleton_fields = block_fields(blocks, skeleton_names[0])
    total_slots = ini_int(skeleton_fields, "array") // runtime.MERGED_SKELETON_ELEMENTS_PER_SLOT
    if total_slots <= 0:
        bad(f"{skeleton_names[0]} 的 array 不是 3 的整数倍：{skeleton_fields.get('array')!r}")
        return problems

    # ① 各族 palette / 身份载荷 / 借用表
    palette = {}
    for name in section_names(blocks, "ResourceHI3PaletteFam_"):
        family = name[len("ResourceHI3PaletteFam_"):]
        slots = ini_int(block_fields(blocks, name), "array")
        if slots <= 0:
            bad(f"[{name}] 的 array 必须 > 0（palette 槽数 = 本族 span）")
            continue
        palette[family] = slots

    order = []  # 按 base 升序的族序（= CS 里的来源族序号 rank）
    ranges = {}
    for name in section_names(blocks, runtime.ATTACH_RANGE_RESOURCE_PREFIX):
        family = name[len(runtime.ATTACH_RANGE_RESOURCE_PREFIX):]
        fields = block_fields(blocks, name)
        filename = fields.get("filename", "")
        if not filename:
            bad(f"[{name}] 缺 filename")
            continue
        path = resolve(root, filename)
        if not os.path.exists(path):
            bad(f"[{name}] 指向的 {filename} 不存在")
            continue
        payload = read_bytes(path)
        if len(payload) != ATTACH_RANGE_BYTES:
            bad(
                f"{filename} = {len(payload)} B，t91 身份载荷必须恒 {ATTACH_RANGE_BYTES} B"
                f"（借用行改由 BorrowMap 承载）"
            )
            continue
        info = runtime.decode_attach_range_payload(payload)
        if info["version"] != runtime.ATTACH_RANGE_VERSION:
            bad(f"{filename} 的 version = {info['version']}，期望 {runtime.ATTACH_RANGE_VERSION}")
        if not (0 <= info["base"] < total_slots):
            bad(f"{filename} 的 base = {info['base']} 越出骨架槽位 [0,{total_slots})")
        if info["span"] <= 0:
            bad(f"{filename} 的 span = {info['span']}，必须 > 0")
        if info.get("borrow_rows"):
            bad(f"{filename} 里还带着 {len(info['borrow_rows'])} 条借用行（t84 口径，t91 起不该有）")
        ranges[family] = info
        order.append((info["base"], family))

    order.sort()
    rank_of = {family: rank for rank, (_base, family) in enumerate(order)}
    if not order:
        bad("ini 里没有任何 AttachRange 段 ⇒ 无法判定块布局")
        return problems

    # 块长 = span + 本族 BorrowMap 行数；顺带校验 BorrowMap 文件本身
    block_slots = {}
    borrow_rows = {}
    for family, info in ranges.items():
        name = runtime.borrow_map_resource_name(family)
        if name not in blocks:
            bad(f"缺 [{name}] 声明（每族一份借用表，cs-t4 绑它）")
            continue
        fields = block_fields(blocks, name)
        filename = fields.get("filename", "")
        if not filename:
            bad(f"[{name}] 缺 filename")
            continue
        path = resolve(root, filename)
        if not os.path.exists(path):
            bad(f"[{name}] 指向的 {filename} 不存在")
            continue
        declared = ini_int(fields, "array")
        payload = read_bytes(path)
        if len(payload) != BORROW_MAP_ROW_BYTES * declared:
            bad(
                f"{filename} = {len(payload)} B，与 array = {declared} × {BORROW_MAP_ROW_BYTES} B 不符"
            )
            continue
        rows = runtime.decode_borrow_map_payload(payload)
        live = [row for row in rows if row[0] != BORROW_MAP_EMPTY_SENTINEL]
        if declared != max(1, len(live)):
            bad(f"[{name}] array = {declared}，但表里有 {len(live)} 条有效行（空表应写 1 行哨兵）")
        base, span = info["base"], info["span"]
        borrow_rows[family] = live
        block_slots[family] = span + len(live)
        expected = list(range(base + span, base + span + len(live)))
        got = sorted(row[0] for row in live)
        if got != expected:
            bad(f"{filename} 的目标槽 {got} 必须正好是本族块尾的连续槽 {expected}")
        for target, rank, local, *_rest in live:
            if rank >= len(palette):
                bad(f"{filename} 里目标槽 {target} 的来源族序号 {rank} ≥ palette 家族数 {len(palette)}")
                continue
            source_family = order[rank][1]
            if local >= palette.get(source_family, 0):
                bad(
                    f"{filename} 里目标槽 {target} 的源局部槽 {local} 越出来源族 "
                    f"{source_family} 的 palette 槽数 {palette.get(source_family)}"
                )

    for family, block in block_slots.items():
        info = ranges[family]
        if info["base"] + block > total_slots:
            bad(
                f"{family}: 块 [base={info['base']}, +block={block}) 越出骨架槽位 {total_slots}"
                f"（BlockSlotTotal 必须 = ∑block）"
            )

    covered = sorted(
        (info["base"], info["base"] + block_slots.get(family, info["span"]))
        for family, info in ranges.items()
    )
    cursor = 0
    for start, end in covered:
        if start != cursor:
            bad(f"块布局不接续：上一块结束于 {cursor}，下一块从 {start} 开始（空洞 ⇒ deform 读零）")
        cursor = max(cursor, end)
    if cursor != total_slots:
        bad(f"块覆盖总长 {cursor} ≠ 骨架槽数 {total_slots} ⇒ ini 的 array 与本族块之和对不上")

    # ② attach 段绑定与 Dispatch
    for family, info in sorted(ranges.items(), key=lambda kv: kv[1]["base"]):
        name = runtime.attach_section_name(family)
        if name not in blocks:
            bad(f"缺 [{name}] attach 段")
            continue
        fields = block_fields(blocks, name)
        wanted = {
            "cs-t0": runtime.family_palette_name(family),
            "cs-t1": runtime.attach_range_resource_name(family),
            "cs-t4": runtime.borrow_map_resource_name(family),
        }
        for rank, (_base, source_family) in enumerate(order):
            register = runtime.borrow_palette_register(rank)
            wanted[f"cs-{register}"] = runtime.family_palette_name(source_family)
        for key, resource in wanted.items():
            bound = fields.get(key, "")
            if bound != f"ref {resource}":
                bad(f"[{name}] {key} = {bound!r}，期望 'ref {resource}'")
        block = block_slots.get(family, info["span"])
        expect_dispatch = max(1, -(-block // 64))
        dispatch = fields.get("dispatch", "")
        if dispatch.replace(" ", "") != f"{expect_dispatch},1,1":
            bad(
                f"[{name}] Dispatch = {dispatch!r}，按块长 {block} 应为 "
                f"'{expect_dispatch}, 1, 1'（每线程一根骨，64 线程一组）"
            )

    # ③ Blend 索引必须全部落在本族块内
    for family, info in sorted(ranges.items(), key=lambda kv: kv[1]["base"]):
        filename = f"{family}-Blend.buf"
        path = resolve(root, os.path.join(runtime.MERGED_SKELETON_DEFAULT_FOLDER, filename))
        if not os.path.exists(path):
            bad(f"缺 {runtime.MERGED_SKELETON_DEFAULT_FOLDER}/{filename}（无法核对索引重定基）")
            continue
        used = blend_used_indices(read_bytes(path))
        base = info["base"]
        block = block_slots.get(family, info["span"])
        outside = sorted(index for index in used if not (base <= index < base + block))
        if outside:
            bad(
                f"{filename} 里有 {len(outside)} 个骨号落在本族块 [base={base}, +block={block}) 之外："
                f"{outside[:8]}{' …' if len(outside) > 8 else ''}"
            )
        borrowed_used = sorted(index for index in used if index >= base + info["span"])
        declared = sorted(row[0] for row in borrow_rows.get(family, []))
        stray = [index for index in borrowed_used if index not in declared]
        if stray:
            bad(f"{filename} 用到了借用槽 {stray}，但本族 BorrowMap 没声明它们")

    # ④ CS 判据 + SLOTS 宏
    if os.path.exists(hlsl_path):
        text = open(hlsl_path, encoding="utf-8").read()
        problems.extend(f"hlsl: {item}" for item in himi.merged_attach_hlsl_problems(text))
        problems.extend(f"hlsl: {item}" for item in himi.merged_attach_borrow_problems(text))
        macro = himi.MERGED_ATTACH_SLOTS_MACRO
        found = ""
        for raw in text.splitlines():
            line = raw.strip()
            if line.startswith(f"#define {macro} "):
                found = line.split()[-1]
        if found != f"{total_slots}u":
            bad(f"hlsl: #define {macro} = {found!r}，与 ini 的 {total_slots} 槽（array/3）不一致")
        palette_decls = [
            line for line in (raw.strip() for raw in text.splitlines())
            if line.startswith("Buffer<float4> himi_borrow_palette_")
        ]
        if len(palette_decls) < len(palette):
            bad(
                f"hlsl: 声明了 {len(palette_decls)} 个 himi_borrow_palette_*，"
                f"但 ini 里有 {len(palette)} 个家族 palette"
            )
    else:
        bad(f"找不到 hlsl：{hlsl_path}")

    # ⑤ 骨架缓冲字节数（array 与文件必须同源）
    folder = runtime.MERGED_SKELETON_DEFAULT_FOLDER
    skeleton_stem = skeleton_names[0][len(runtime.MERGED_SKELETON_RESOURCE_PREFIX):]
    skeleton_path = os.path.join(root, folder, skeleton_stem + runtime.MERGED_SKELETON_BASENAME_SUFFIX)
    if os.path.exists(skeleton_path):
        size = os.path.getsize(skeleton_path)
        if size != total_slots * SLOT_STRIDE:
            bad(
                f"{folder}/{os.path.basename(skeleton_path)} = {size} B，"
                f"按 ini array/3 = {total_slots} 槽应为 {total_slots * SLOT_STRIDE} B"
            )
    return problems


# ----------------------------------------------------------------------------- 自检


def self_test() -> int:
    """造一份**合格**产物（全部走生成器自己的函数），再故意改坏，验自检真的会报。"""

    class _Part:
        __slots__ = ("family_key", "slot_values")

        def __init__(self, family_key, slot_values):
            self.family_key = family_key
            self.slot_values = slot_values

    spans = {"077983c9": (0, 51), "09c6e58a": (51, 165), "1b62725d": (216, 1)}
    borrowed = {
        "077983c9": [51 + 0, 51 + 95, 51 + 96, 216],
        "09c6e58a": [216],
        "1b62725d": [],
    }
    parts = [_Part(family, list(range(base, base + span))) for family, (base, span) in spans.items()]
    ranges = runtime.compute_family_ranges(
        parts, sum(span for _base, span in spans.values()), complete=False, borrowed_slots=borrowed
    )
    slot_count = runtime.ranges_slot_count(ranges)
    palette_families = runtime.borrow_palette_families(ranges)

    root = tempfile.mkdtemp(prefix="himi_t91_selftest_")
    folder = os.path.join(root, runtime.MERGED_SKELETON_DEFAULT_FOLDER)
    os.makedirs(os.path.join(root, "res"), exist_ok=True)
    os.makedirs(folder, exist_ok=True)

    role = "role_test"
    skeleton_name = runtime.merged_skeleton_resource_name(role)
    lines = [";MARK:MergedSkeleton"]
    for family, info in sorted(ranges.items(), key=lambda kv: kv[1]["base"]):
        block = info.get("block_slots", info["slots"])
        lines.extend(
            runtime.render_attach_section_lines(
                family,
                base=info["base"],
                slots=info["slots"],
                skeleton_name=skeleton_name,
                hlsl_path="./res/himi_attach.hlsl",
                object_matrix_name=runtime.object_matrix_resource_name(role),
                block_slots=block,
                borrow_map_name=runtime.borrow_map_resource_name(family),
                palette_families=palette_families,
            )
        )
    lines.append(";MARK:ResourceBuffer")
    lines.extend(runtime.build_merged_skeleton_decl(role, slot_count).ini_lines())
    for family, info in sorted(ranges.items(), key=lambda kv: kv[1]["base"]):
        lines.extend(runtime.build_family_palette_decl(family, info["slots"]).ini_lines())
        lines.extend(runtime.cb3_capture_ini_lines(family))
    lines.extend(
        runtime.build_object_matrix_decl(role, len(palette_families)).ini_lines()
    )
    for family, info in sorted(ranges.items(), key=lambda kv: kv[1]["base"]):
        lines.extend(
            runtime.attach_range_ini_lines(
                family, f"{folder}/{runtime.attach_range_basename(family)}"
            )
        )
    for family, info in sorted(ranges.items(), key=lambda kv: kv[1]["base"]):
        rows = runtime.borrow_map_rows(ranges, family)
        lines.extend(
            runtime.borrow_map_ini_lines(
                family, f"{folder}/{runtime.borrow_map_basename(family)}", len(rows)
            )
        )
    ini_path = os.path.join(root, "self.ini")
    open(ini_path, "w", encoding="utf-8").write("\n".join(lines) + "\n")

    report = runtime.publish_attach_ranges(
        ranges, folder, skeleton_slots=slot_count
    )
    if not report.get("ok", True):
        print(f"[self-test] publish_attach_ranges 失败：{report.get('message')}")
        return 2
    borrow_files = report.get("borrow_maps") or []
    print(
        f"[self-test] 身份载荷 {len(report.get('files') or [])} 份、借用表 {len(borrow_files)} 份"
    )

    # 骨架缓冲 + 各族 Blend（骨号已按块重定基 + 借用槽）
    open(os.path.join(folder, runtime.merged_skeleton_basename(role)), "wb").write(
        bytes(slot_count * SLOT_STRIDE)
    )
    for family, info in ranges.items():
        base = info["base"]
        block = info.get("block_slots", info["slots"])
        live = list(range(base, base + min(block, 4)))
        indices = live + [0] * (4 - len(live))
        weights = [1.0] * len(live) + [0.0] * (4 - len(live))
        payload = b"".join(
            struct.pack("<4f", *weights) + struct.pack("<4i", *indices) for _row in range(3)
        )
        open(os.path.join(folder, f"{family}-Blend.buf"), "wb").write(payload)

    hlsl_path = os.path.join(root, "res", "himi_attach.hlsl")
    open(hlsl_path, "w", encoding="utf-8").write(
        himi.merged_attach_hlsl_text(
            runtime.build_merged_skeleton_decl(role, slot_count), ranges=ranges
        )
    )

    problems = audit(root, ini_path, hlsl_path)
    print(f"[self-test] 好产物 → {len(problems)} 条问题" + ("" if not problems else "："))
    for item in problems:
        print(f"    - {item}")

    # 故意改坏：骨架 array 少 3 槽
    broken = ini_path.replace("self.ini", "self.broken.ini")
    original = open(ini_path, encoding="utf-8").read()
    decl_line = f"array = {slot_count * runtime.MERGED_SKELETON_ELEMENTS_PER_SLOT}"
    open(broken, "w", encoding="utf-8").write(original.replace(decl_line, f"array = {slot_count * 3 - 3}", 1))
    caught = audit(root, broken, hlsl_path)
    mutated = [item for item in caught if item not in problems]
    print(f"[self-test] 改坏一行后新增 {len(mutated)} 条问题：")
    for item in mutated:
        print(f"    - {item}")

    ok = not problems and bool(mutated)
    print(f"[self-test] {'通过' if ok else '未通过'}")
    return 0 if ok else 1


# ----------------------------------------------------------------------------- CLI


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="t91 借用槽产物三方一致性自检（只读）")
    parser.add_argument("--mod-root", help="mod 根目录（含 虚构游戏.ini 与 Meshes/）")
    parser.add_argument("--ini", default="", help="ini 文件名，缺省取 mod 根下唯一的 *.ini")
    parser.add_argument("--hlsl", default="", help="CS 路径，缺省 res/himi_attach.hlsl")
    parser.add_argument("--self-test", action="store_true", help="造合格产物 + 改坏一行，验自检本身")
    args = parser.parse_args(argv)

    if args.self_test:
        return self_test()
    if not args.mod_root:
        parser.error("要么 --mod-root，要么 --self-test")
    root = os.path.abspath(args.mod_root)
    if not os.path.isdir(root):
        print(f"[error] mod 根不存在：{root}")
        return 2
    ini_path = os.path.join(root, args.ini) if args.ini else ""
    if not ini_path:
        candidates = sorted(name for name in os.listdir(root) if name.lower().endswith(".ini"))
        if len(candidates) != 1:
            print(f"[error] mod 根下有 {len(candidates)} 个 ini，请用 --ini 指定：{candidates}")
            return 2
        ini_path = os.path.join(root, candidates[0])
    hlsl_path = os.path.join(root, args.hlsl.replace("/", os.sep)) if args.hlsl else os.path.join(
        root, "res", "himi_attach.hlsl"
    )

    print(f"[input] mod 根 = {root}")
    print(f"[input] ini   = {os.path.basename(ini_path)}")
    print(f"[input] hlsl  = {os.path.relpath(hlsl_path, root)}")
    problems = audit(root, ini_path, hlsl_path)
    if not problems:
        print("[ok] AttachRange / BorrowMap / Blend 索引 / ini / hlsl 五份数据一致")
        return 0
    print(f"[fail] {len(problems)} 条不一致：")
    for item in problems:
        print(f"  - {item}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
