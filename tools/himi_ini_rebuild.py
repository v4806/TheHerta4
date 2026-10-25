"""把**已部署**的 HIMI 产物 ini 重建成「每帧 attach」形态（f0c5090 口径），离线、幂等。

它只做一件事：让部署目录里的 ini 与当前生成器
（`common/himi_merged_runtime.py` + `ui/universal/himi.py`）渲染出的结构**逐字段一致**，
不重新导出、不碰二进制、不碰 ini 以外的文件。

重建出来的机制链（顺序即护栏 ``blend_block_order_problems`` 判的顺序）：

.. code-block:: ini

   ; deform（VB Blend）段：① 捕当帧 palette ② 捕当帧 vs-cb3 ③ 搬进合并骨架
   ResourceHI3PaletteFam_<fam> = copy vs-t0 unless_null
   ResourceHI3Cb3_<fam>        = copy vs-cb3 unless_null
   run = CustomShaderHI3MergedSkeletonAttach_<fam>
   vs-t0 = ResourceHI3MergedSkeleton_<role>      ; ③ 之后才重绑
   handling = skip
   draw = <行数>, 0

   ; 渲染（IB）段：① 捕**渲染那笔**的 vs-cb3 ② 该族 attach，然后才是 drawindexed
   ResourceHI3Cb3_<fam> = copy vs-cb3 unless_null
   run = CustomShaderHI3MergedSkeletonAttach_<fam>
   drawindexed = ...

族清单、每族的 base/span 一律从**已部署的** `Meshes/<fam>-AttachRange.buf` 读
（`:func:`common.himi_merged_runtime.decode_attach_range_payload`），不从 ini 猜；
合并骨架槽位数从 ini 里那个 `[ResourceHI3MergedSkeleton_<role>]` 的 `array / 3` 读。

用法（仓库根目录）：
    python tools/himi_ini_rebuild.py --mod-root "K:/…/虚构游戏" --role-key role_06c391d0 --dry-run
    python tools/himi_ini_rebuild.py --mod-root "K:/…/虚构游戏" --role-key role_06c391d0
"""

from __future__ import annotations

import argparse
import os
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import himi_attach_cs_offline as offline  # noqa: E402  （同目录工具：提供无 bpy 的加载器）

#: 失败链路（record pass + skin pass）留在产物里的东西，重建时整段退场。
RETIRED_PREFIXES = (
    "CustomShaderHI3MergedSkin_",
    "CustomShaderHI3FamilyRecord_",
    "ResourceHI3SkinSrc_",
    "ResourceHI3SkinOut_",
    "ResourceHI3BoneSlotMap_",
    "ResourceHI3CalibInfo_",
    "ResourceHI3FamilySpan",
    "ResourceHI3FamilyMatrix",
)
#: 产物里由本工具负责重建的族级资源（先全删、再按生成器的渲染函数重发）。
REBUILT_PREFIXES = (
    "ResourceHI3PaletteFam_",
    "ResourceHI3Cb3_",
    "ResourceHI3AttachRange_",
    "ResourceHI3ObjectMatrix_",
    "CustomShaderHI3MergedSkeletonAttach_",
)


def _parse_blocks(text: str):
    """``{"prefix","name","body"}`` 序列（未被改动的段回写后与原文件逐字节相同）。

    ``prefix`` = 段头**之前**那段空行/``;`` 注释（产物里的 ``;MARK:…``）。删除一段时它的
    ``;MARK:`` 跟着一起走，不会留下一个空标记。
    """
    blocks: list = []
    head: list = []
    current = None
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            current = {"prefix": [], "name": stripped[1:-1].strip(), "body": []}
            blocks.append(current)
        elif current is None:
            head.append(line)
        else:
            current["body"].append(line)
    # 把「下一段段头之前」的空行/注释从上一段 body 尾部搬到下一段 prefix。
    for index in range(len(blocks) - 1):
        cur, nxt = blocks[index], blocks[index + 1]
        moved: list = []
        while cur["body"] and (
            not cur["body"][-1].strip() or cur["body"][-1].strip().startswith(";")
        ):
            moved.insert(0, cur["body"].pop())
        nxt["prefix"] = moved + nxt["prefix"]
    return head, blocks


def _render(head, blocks) -> str:
    out = list(head)
    for block in blocks:
        out.extend(block.get("prefix", []))
        out.append("[" + block["name"] + "]")
        out.extend(block["body"])
    # 删除段之后会留下多余空行：连续 3 行以上压成 2 行（不碰任何属性行）。
    compacted: list = []
    blanks = 0
    for line in out:
        if line.strip():
            blanks = 0
            compacted.append(line)
            continue
        blanks += 1
        if blanks <= 2:
            compacted.append(line)
    text = "\n".join(compacted)
    return text if text.endswith("\n") else text + "\n"


def _strip_trailing_blanks(lines: list) -> list:
    items = list(lines)
    while items and not items[-1].strip():
        items.pop()
    return items


def _read_attach_range(mod_root: Path, family: str, runtime) -> dict:
    folder = runtime.ATTACH_RANGE_DEFAULT_FOLDER
    path = mod_root / folder / runtime.attach_range_basename(family)
    if not path.is_file():
        raise SystemExit(f"[fatal] 缺身份载荷 {path}（attach CS 拿不到 base/span）")
    payload = path.read_bytes()
    info = runtime.decode_attach_range_payload(payload)
    if not info.get("ok"):
        raise SystemExit(f"[fatal] 身份载荷 {path} 不合法（magic/版本不符）：{info}")
    if not info.get("span"):
        raise SystemExit(f"[fatal] 身份载荷 {path} 的 span = 0（该族没有槽位可写）")
    return {"path": path, "info": info}


def rebuild(ini_path: Path, mod_root: Path, role_key: str, himi, runtime) -> tuple:
    text = ini_path.read_text(encoding="utf-8")
    head, blocks = _parse_blocks(text)

    families = []
    for block in blocks:
        prefix = runtime.ATTACH_SECTION_PREFIX
        if block["name"].startswith(prefix):
            family = block["name"][len(prefix):].strip()
            if family and family not in families:
                families.append(family)
    if not families:
        raise SystemExit("[fatal] ini 里找不到任何 [CustomShaderHI3MergedSkeletonAttach_*] 段")

    skeleton_name = runtime.merged_skeleton_resource_name(role_key)
    skeleton_slots = 0
    for block in blocks:
        if block["name"] != skeleton_name:
            continue
        for line in block["body"]:
            key, _, value = line.partition("=")
            if key.strip().lower() == "array":
                skeleton_slots = int(value.strip()) // runtime.MERGED_SKELETON_ELEMENTS_PER_SLOT
    if skeleton_slots <= 0:
        raise SystemExit(f"[fatal] 读不到 [{skeleton_name}] 的 array（合并骨架槽位数）")

    ranges = {family: _read_attach_range(mod_root, family, runtime) for family in families}
    families.sort(key=lambda fam: int(ranges[fam]["info"]["base"]))

    notes = []
    kept = []
    for block in blocks:
        name = block["name"]
        if name.startswith(RETIRED_PREFIXES) or name.startswith(REBUILT_PREFIXES):
            notes.append(f"删段 {name}")
            continue
        kept.append(block)
    blocks = kept

    # ---- ① 计数真实文件里有没有身份载荷（先于重建检查，缺文件就别改 ini）----
    for family in families:
        notes.append(
            f"族 {family}: base={ranges[family]['info']['base']} "
            f"span={ranges[family]['info']['span']} "
            f"own/base_index=({ranges[family]['info'].get('own_index')},"
            f"{ranges[family]['info'].get('base_index')})"
        )

    # ---- ② 定位插入点：第一个 TextureOverride_* 段之前（产物里资源/CS 段都在此之前）----
    insert_at = 0
    for index, block in enumerate(blocks):
        if block["name"].startswith("TextureOverride"):
            insert_at = index
            break
    else:
        insert_at = len(blocks)

    fresh = []

    def _section_mark(label: str) -> str:
        return f";MARK:{label}"

    def _add(label: str, lines: list) -> None:
        header, _, body = lines[0], None, lines[1:]
        if not (header.startswith("[") and header.endswith("]")):
            raise SystemExit(f"[fatal] 渲染函数的第一行不是段头：{header!r}")
        fresh.append(
            {
                "prefix": [_section_mark(label)],
                "name": header[1:-1].strip(),
                "body": _strip_trailing_blanks(body) + ["", ""],
            }
        )

    # ① palette 副本（每族一个，stride = 48、无 format）
    for family in families:
        decl = runtime.build_family_palette_decl(
            family, int(ranges[family]["info"]["span"])
        )
        _add("MergedSkeleton", decl.ini_lines())
    # ② 当帧 vs-cb3 副本（每族一个，只有段名 + 注释）
    for family in families:
        _add("MergedSkeleton", runtime.cb3_capture_ini_lines(family))
    # ③ 族级物体矩阵表（角色级一张，array = 3 × 族数）
    _add("MergedSkeleton", runtime.object_matrix_ini_lines(role_key, len(families)))
    # ④ 身份资源（每族一个，128 B 载荷）
    for family in families:
        filename = f"{runtime.ATTACH_RANGE_DEFAULT_FOLDER}/" + runtime.attach_range_basename(family)
        _add("MergedSkeleton", runtime.attach_range_ini_lines(family, filename))
    # ⑤ attach 段（单文件 CS，每族绑自己那族的副本 + 身份 + cb3 + 物体矩阵表）
    object_matrix_name = runtime.object_matrix_resource_name(role_key)
    for family in families:
        lines = runtime.render_attach_section_lines(
            family,
            base=int(ranges[family]["info"]["base"]),
            slots=int(ranges[family]["info"]["span"]),
            skeleton_name=skeleton_name,
            hlsl_path=himi.MERGED_ATTACH_HLSL_PATH,
            object_matrix_name=object_matrix_name,
        )
        _add("MergedSkeleton", lines)

    blocks[insert_at:insert_at] = fresh
    notes.append(f"新增/重建 {len(fresh)} 个段（palette/cb3/物体矩阵/身份/attach）")

    # 文件头若已经带着一个孤立的 ``;MARK:``（它原本属于被重建的第一个段），去掉重复的那份。
    if insert_at == 0:
        while head and not head[-1].strip():
            head.pop()
        if head and head[-1].strip().startswith(";MARK:"):
            head.pop()
            notes.append("去掉了文件头那个重复的 ;MARK:（重建的第一个段自带）")
        head.append("")

    # ---- ③ 逐段改机制链 ----
    for block in blocks:
        name = block["name"]
        body = block["body"]
        if name.startswith("CustomShaderHI3MergedSkeletonAttach_"):
            continue  # 上面刚发过
        if name.startswith("TextureOverride_VB_") and name.endswith("_Blend"):
            family = _family_for_block(block, families)
            if not family:
                continue
            body = _rewrite_blend_block(body, family, runtime)
            block["body"] = body
            notes.append(f"{name}: palette→cb3→run→重绑 顺序确认（{family}）")
            continue
        if name.startswith("TextureOverride_LOD0."):
            family = _family_for_block(block, families)
            body = _rewrite_ib_block(body, family, runtime)
            block["body"] = body
            notes.append(f"{name}: 只留本族 cb3 捕获 + attach（{family}），去掉 vb0 覆写")
    return _render(head, blocks), notes


def _family_for_block(block, families) -> str:
    for line in block["body"]:
        key, _, value = line.partition("=")
        if key.strip().lower() == "hash":
            token = value.strip().lower()
            for family in families:
                if family.lower() == token:
                    return family
    # 段名形如 TextureOverride_VB_09c6e58a_09c6e58a_Blend / TextureOverride_LOD0.09c6e58a_57792_0
    stem = block["name"]
    for family in families:
        if stem.startswith(f"TextureOverride_VB_{family}_") or stem.startswith(
            f"TextureOverride_LOD0.{family}_"
        ):
            return family
    return ""


def _drop_mechanism_lines(lines: list, runtime) -> list:
    """去掉与 attach 机制相关的旧行（run / 捕获 / SkinOut vb0），保留其余。"""
    kept = []
    for line in lines:
        stripped = line.strip()
        lowered = stripped.lower()
        if lowered.startswith("run ="):
            target = stripped.split("=", 1)[1].strip()
            if target.lower().startswith(runtime.ATTACH_SECTION_PREFIX.lower()):
                continue
            if target.startswith(RETIRED_PREFIXES):
                continue  # 退役链路（record pass / skin pass）的 run，段已经删了
        if runtime.FAMILY_PALETTE_PREFIX.lower() in lowered and "copy vs-t0" in lowered:
            continue
        if runtime.CB3_CAPTURE_PREFIX.lower() in lowered and "copy vs-cb3" in lowered:
            continue
        if lowered.startswith("vb0 =") and "skinout" in lowered:
            continue
        kept.append(line)
    return kept


def _rewrite_blend_block(lines: list, family: str, runtime) -> list:
    """deform 段：palette 捕获 → cb3 捕获 → attach run → vs-t0 重绑 → skip → draw。"""
    body = _drop_mechanism_lines(lines, runtime)
    capture = [f"{runtime.family_palette_name(family)} = copy vs-t0 unless_null"] + list(
        runtime.render_attach_render_capture_lines(family)
    )
    insert_at = None
    for index, line in enumerate(body):
        if line.strip().lower().startswith("vs-t0"):
            insert_at = index
            break
    if insert_at is None:
        for index, line in enumerate(body):
            if line.strip().lower().startswith("handling"):
                insert_at = index
                break
    if insert_at is None:
        insert_at = len(_strip_trailing_blanks(body))
    return body[:insert_at] + capture + body[insert_at:]


def _rewrite_ib_block(lines: list, family: str, runtime) -> list:
    """渲染段：只留本族「cb3 捕获 → attach run」，紧贴 drawindexed 之前。"""
    body = _drop_mechanism_lines(lines, runtime)
    capture = list(runtime.render_attach_render_capture_lines(family)) if family else []
    insert_at = None
    for index, line in enumerate(body):
        if line.strip().lower().startswith("drawindexed"):
            insert_at = index
            break
    if insert_at is None:
        insert_at = len(_strip_trailing_blanks(body))
    return body[:insert_at] + capture + body[insert_at:]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="把已部署 HIMI ini 重建成每帧 attach 形态")
    parser.add_argument("--mod-root", required=True, help="mod 目录（ini 所在目录）")
    parser.add_argument("--ini", default="", help="ini 文件名；默认取目录下唯一那个 .ini")
    parser.add_argument("--role-key", required=True, help="合并骨架角色键（ResourceHI3MergedSkeleton_<key>）")
    parser.add_argument("--dry-run", action="store_true", help="只打印，不写文件")
    parser.add_argument("--preview-out", default="", help="把重建结果写到这个路径（不碰 ini）")
    args = parser.parse_args(argv)

    mod_root = Path(args.mod_root).expanduser().resolve()
    if not mod_root.is_dir():
        raise SystemExit(f"[fatal] mod 目录不存在：{mod_root}")
    if args.ini:
        ini_path = mod_root / args.ini
    else:
        candidates = [p for p in mod_root.glob("*.ini") if not p.name.endswith(".bak")]
        candidates.sort(key=lambda p: (p.name.startswith("."), p.name))
        if not candidates:
            raise SystemExit(f"[fatal] {mod_root} 下没有 .ini")
        ini_path = candidates[0]

    himi = offline.load_generator()
    runtime = sys.modules[f"{offline.PKG}.common.himi_merged_runtime"]

    text, notes = rebuild(ini_path, mod_root, args.role_key.strip(), himi, runtime)
    for note in notes:
        print("[note]", note)

    problems = []
    problems.extend(("段解析", item) for item in himi.product_ini_section_problems(text))
    problems.extend(("cs 引用", item) for item in himi.verify_cs_references(text, str(mod_root)))
    problems.extend(
        ("身份资源", item) for item in himi.verify_attach_range_references(text, str(mod_root))
    )
    if problems:
        print("[FAIL] 产物自检没过：")
        for scope, item in problems:
            print(f"  - [{scope}] {item}")
        return 1
    print("[ok] product_ini_section_problems / verify_cs_references / verify_attach_range_references 全绿")

    if args.preview_out:
        Path(args.preview_out).write_text(text, encoding="utf-8", newline="")
        print(f"[preview] {args.preview_out}（{len(text.splitlines())} 行）")
    if args.dry_run:
        print(f"[dry-run] {ini_path}（{len(text.splitlines())} 行）未写入")
        return 0
    payload = text.encode("utf-8")
    if ini_path.read_bytes() == payload:
        print(f"[same] {ini_path}（{len(text.splitlines())} 行）已是目标形态，未改写")
        return 0
    backup = ini_path.with_suffix(ini_path.suffix + ".bak_rebuild")
    backup.write_bytes(ini_path.read_bytes())
    ini_path.write_bytes(payload)
    print(f"[write] {ini_path}（{len(text.splitlines())} 行），备份 {backup.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
