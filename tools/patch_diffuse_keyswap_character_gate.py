# -*- coding: utf-8 -*-
"""给**已导出**的模组补角色门控（材质转资源pro / 贴图切换 V5.1 写的 KeySwap 段）。

背景：生成器已修（``blueprint/node_postprocess_custom_material_assign.py`` 与
``blueprint/node_postprocess_diffuse_switch.py``），**新导出**的 mod 自带门控；本脚本
用于不重新导出就修好磁盘上的既有产品。

只改一件事：``[KeySwap_Diffuse_*]`` 段的 ``condition`` 行
    condition = $swapkey150 == 0 || $swapkey150 < 2
→
    condition = $active0 == 1 && ($swapkey150 == 0 || $swapkey150 < 2)

安全约束（任一不满足就跳过该文件，绝不半改）
--------------------------------------------
1. 该 ini 必须真的具备门控机制（``global $active0`` 声明，或某个部件段的
   ``$active0 = 1`` 置位）；否则加门控会让热键恒假 —— 比不加更糟。
2. 只改 ``[KeySwap_Diffuse_*]`` 段内、且该段已有 ``key =`` 行的 ``condition``。
3. 幂等：已带门控的行原样保留，重复运行不再改动。
4. 落盘前备份到 ``<mod>/Backups/<ini>.bak-keygate-<时间戳>``，写后逐字节回读校验；
   校验失败直接中止并保留备份。

本脚本不自带 addon 导入（``tools/`` 在 Blender 之外运行）：门控判据在这里按同一
正则复刻，并由 ``tests/test_texture_switch_activation_gate.py`` 锁定三个实现
（本脚本 / 材质转资源pro / 贴图切换 V5.1）对同一批样本给出相同判定。

用法
----
    python tools/patch_diffuse_keyswap_character_gate.py --check <ini 或目录> ...
    python tools/patch_diffuse_keyswap_character_gate.py --apply <ini 或目录> ...
"""
import argparse
import re
import time
from pathlib import Path

#: 角色激活标志候选（口径同 anim_driver_base._get_activation_flag / node_swap_ini）。
ACTIVATION_FLAG_CANDIDATES = ("$active0", "$ntmi_active0")
_ACTIVATION_FLAG_DECL_RE = re.compile(
    r"^[ \t]*global(?:[ \t]+persist)?[ \t]+(\$[A-Za-z_]\w*)",
    re.IGNORECASE | re.MULTILINE,
)
_ACTIVATION_FLAG_SET_RE = re.compile(
    r"^[ \t]*(\$[A-Za-z_]\w*)[ \t]*=[ \t]*1[ \t]*$", re.MULTILINE
)

_SECTION_RE = re.compile(r"(?m)^\[(KeySwap_Diffuse_[^\]]+)\]\s*$")
_CONDITION_RE = re.compile(
    r"^(?P<indent>[ \t]*)condition[ \t]*=[ \t]*(?P<cond>.+?)[ \t]*$", re.MULTILINE
)
_KEY_RE = re.compile(r"(?m)^[ \t]*key[ \t]*=")
_UNGATED_RE = re.compile(r"^\$\w+ == 0 \|\| \$\w+ < \d+(?: \(.*\))?$")


def find_activation_flag(text):
    """返回 ini 文本里可用的角色激活标志（声明或置位任一），没有则返回空串。"""
    source = text or ""
    declared = set(_ACTIVATION_FLAG_DECL_RE.findall(source))
    assigned = set(_ACTIVATION_FLAG_SET_RE.findall(source))
    for candidate in ACTIVATION_FLAG_CANDIDATES:
        if candidate in declared or candidate in assigned:
            return candidate
    return ""


def patch_text(text):
    """返回 ``(新文本, 改动段数, 状态)``；只动 ``KeySwap_Diffuse_*`` 段的 condition 行。"""
    flag = find_activation_flag(text)
    if not flag:
        return text, 0, "no-activation-flag"

    heads = list(_SECTION_RE.finditer(text))
    if not heads:
        return text, 0, "no-diffuse-keyswap"

    edited = text
    changed = 0
    for index in range(len(heads) - 1, -1, -1):
        start = heads[index].end()
        end = heads[index + 1].start() if index + 1 < len(heads) else len(edited)
        block = edited[start:end]
        if not _KEY_RE.search(block):
            continue  # 不是本生成器写的 KeySwap 段（没有 key 行），不猜
        match = _CONDITION_RE.search(block)
        if not match:
            continue
        condition = match.group("cond")
        if condition.startswith(f"{flag} == 1 &&"):
            continue  # 幂等：已带门控
        if not _UNGATED_RE.match(condition):
            continue  # 形态不是本生成器写的，不猜
        new_line = f"{match.group('indent')}condition = {flag} == 1 && ({condition})"
        block = block[: match.start()] + new_line + block[match.end():]
        edited = edited[:start] + block + edited[end:]
        changed += 1
    return edited, changed, "patched" if changed else "already-gated"


def _targets(paths):
    for raw in paths:
        path = Path(raw)
        if path.is_dir():
            yield from sorted(path.rglob("*.ini"))
        elif path.is_file():
            yield path


def _backup_path(ini_path):
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup_dir = ini_path.parent / "Backups"
    name = f"{ini_path.name}.bak-keygate-{stamp}"
    if backup_dir.is_dir():
        return backup_dir / name
    return ini_path.with_name(name)


def main():
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true", help="只报告，不写盘")
    mode.add_argument("--apply", action="store_true", help="写盘（先备份 + 回读校验）")
    parser.add_argument("paths", nargs="+")
    args = parser.parse_args()

    total_files = total_edits = 0
    skipped = 0
    for path in _targets(args.paths):
        try:
            text = path.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeDecodeError) as exc:
            print(f"  跳过 {path}: {exc}")
            continue
        patched, changed, status = patch_text(text)
        if status != "patched":
            skipped += 1
            continue
        total_files += 1
        total_edits += changed
        print(f"  {path}  待改 {changed} 段")
        if args.apply:
            backup = _backup_path(path)
            backup.write_text(text, encoding="utf-8")
            path.write_text(patched, encoding="utf-8")
            if path.read_text(encoding="utf-8-sig") != patched:
                raise SystemExit(f"回读校验失败，已中止: {path}（备份 {backup}）")
            print(f"      已写入（备份 {backup.name}）")

    verb = "已写盘" if args.apply else "仅检查（未写盘）"
    print(
        f"\n{verb}：{total_files} 个 ini 共 {total_edits} 段 KeySwap_Diffuse_*；"
        f"另有 {skipped} 个文件无需改动或无门控机制（跳过）。"
    )


if __name__ == "__main__":
    main()
