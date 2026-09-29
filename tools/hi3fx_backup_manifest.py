# -*- coding: utf-8 -*-
"""刷新 HI3FX mod 的历史备份清单（``DISABLED\\_backup\\``）。

背景：HI3FX 的备份统一放在 ``Mods\\HI3FX\\DISABLED\\_backup\\<版本-时间>\\``。
放在 ``DISABLED\\`` 之下是因为 ``d3dx.ini`` 有 ``exclude_recursive = DISABLED*``，
整棵子树不会被加载 —— 因此冻结副本里可以安全地保留 ``.ini`` 原文件名，回滚就是「按名覆盖」。

本工具做三件事（幂等，可反复运行；只覆盖它自己生成的两份清单和冻结副本里的 README）：

1. 把当前 live 的 ``README.md`` 同步进「当前快照」目录（该目录定义为 live 的冻结副本）；
2. 重建 ``_backup\\_manifest.json``（每个快照逐文件大小 + sha256，外加 live 文件表）；
3. 生成 ``_backup\\README.md`` 人类可读清单（含每个快照与 live 的逐文件关系）。

用法：
    python tools/hi3fx_backup_manifest.py
    python tools/hi3fx_backup_manifest.py --current v1.1-20260928-0839
    python tools/hi3fx_backup_manifest.py --check      # 只校验，不写盘（有差异时退出码 1）
"""
import argparse
import hashlib
import json
import sys
from pathlib import Path

DEFAULT_MOD_DIR = Path(r"K:\SSMT-Package-master\3Dmigoto\HI3\Mods\HI3FX")
DEFAULT_CURRENT = "v1.1-20260928-0839"

# live 文件清单（= 会被 3DMigoto 加载的 4 个模块 + 文档 + DISABLED\ 里的 4 个可选模块）
LIVE_FILES = [
    "HI3FX.ini",
    "HI3FX.Main.ini",
    "HI3FX.Helper.ini",
    "HI3FX.IgnoreList.ini",
    "README.md",
    "DISABLED/HI3FX.AutoApply.ini",
    "DISABLED/HI3FX.Example.ini",
    "DISABLED/HI3FX.Remap.ini",
    "DISABLED/ShaderCacheSettings.ini",
]

# 快照目录 →（版本标签，说明）。未登记的目录会用兜底标签生成，提醒补一句。
SNAPSHOT_NOTES = {
    "v1.0-20260927-1635": (
        "v1.0",
        "TTLMap 拆分之前（2026-09-27 16:35:33 建立）。只含当时被改动的 5 个文件；"
        "`HI3FX.IgnoreList.ini` / `DISABLED\\HI3FX.AutoApply.ini` / `HI3FX.Remap.ini` / "
        "`ShaderCacheSettings.ini` 在 v1.0→v1.1 之间未改动，去当前快照里取即可。",
    ),
    "v1.1-20260927-1652": (
        "v1.1（HelperV3 之前）",
        "v1.1 发布快照（2026-09-27 16:36–16:52 写入的那一版）。当天 20:35 追加 HelperV3 家族"
        "之前的状态。`HI3FX.Helper.ini.v11` 与 `HI3FX.Helper.ini.bak-v3-20260927-203210` "
        "逐字节相同（同一内容的两个来源）。",
    ),
    "v1.1-20260928-0839": (
        "v1.1（当前，含 HelperV3）",
        "当前启用状态的完整冻结副本，含 `DISABLED\\` 里的 4 个可选模块；除清单本身涉及的 "
        "README 外与实时文件逐字节一致。",
    ),
}
TOOL_PATH = "tools/hi3fx_backup_manifest.py"


def sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def discover_snapshots(backup_dir):
    folders = sorted(
        (entry.name for entry in backup_dir.iterdir()
         if entry.is_dir() and entry.name.startswith("v")),
        key=lambda name: (name.split("-", 1)[0], name),
    )
    return folders


def build(mod_dir, backup_dir, current):
    snapshots = discover_snapshots(backup_dir)
    manifest = {"root": str(mod_dir), "backup_dir": str(backup_dir), "snapshots": {}}
    for folder in snapshots:
        entries = []
        for path in sorted((backup_dir / folder).rglob("*")):
            if path.is_file():
                entries.append({
                    "rel": str(path.relative_to(backup_dir / folder)).replace("\\", "/"),
                    "size": path.stat().st_size,
                    "sha256": sha256(path),
                })
        manifest["snapshots"][folder] = entries
    manifest["live"] = [
        {"rel": rel, "size": (mod_dir / rel).stat().st_size, "sha256": sha256(mod_dir / rel)}
        for rel in LIVE_FILES
    ]
    manifest["current_snapshot"] = current
    return manifest


def render(manifest, current):
    live_by_hash = {item["sha256"]: item["rel"] for item in manifest["live"]}
    lines = [
        "# HI3FX 历史备份清单",
        "",
        "当前版本 **HI3FX v1.1**（权威标记在 `HI3FX.ini` 头部与 `README.md`；HelperV3 家族属于 v1.1）。",
        "",
        "本目录在 `DISABLED\\` 之下 —— `d3dx.ini` 的 `exclude_recursive = DISABLED*` 保证整棵子树",
        "不会被加载，所以这里即使放 `.ini` 也不会进游戏。**不要把 `_backup` 移出 `DISABLED\\`。**",
        "",
        "回滚方法：把对应快照目录里的文件按名义覆盖回上一级（快照保留原文件名与 `.bak-*` / `.v11` 后缀）。",
        "本文件与 `_manifest.json` 由 TheHerta4 仓库的 `{}` 生成，可重复运行。".format(TOOL_PATH),
        "",
    ]
    for folder, entries in manifest["snapshots"].items():
        label, note = SNAPSHOT_NOTES.get(
            folder, ("（未登记）", "这个快照还没有说明 —— 请在 `{}` 的 `SNAPSHOT_NOTES` 里补一句。".format(TOOL_PATH))
        )
        if folder == current:
            label += " ⟵ 当前冻结副本"
        lines += [
            "## {} — {}".format(folder, label),
            "",
            note,
            "",
            "| 文件 | 大小 | sha256（前 16 位） | 与当前 live 的关系 |",
            "| --- | --- | --- | --- |",
        ]
        for item in entries:
            same = live_by_hash.get(item["sha256"])
            if same and same == item["rel"]:
                relation = "与 live 同名文件逐字节相同"
            elif same:
                relation = "与 live 的 `{}` 逐字节相同".format(same)
            else:
                relation = "与当前 live 不同"
            lines.append("| `{}` | {} | `{}` | {} |".format(
                item["rel"], item["size"], item["sha256"][:16], relation))
        lines.append("")

    lines += [
        "## 当前 live 文件（供比对）",
        "",
        "| 文件 | 大小 | sha256（前 16 位） |",
        "| --- | --- | --- |",
    ]
    for item in manifest["live"]:
        lines.append("| `{}` | {} | `{}` |".format(
            item["rel"], item["size"], item["sha256"][:16]))
    lines.append("")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="刷新 HI3FX 备份清单")
    parser.add_argument("--mod-dir", default=str(DEFAULT_MOD_DIR), help="HI3FX mod 目录")
    parser.add_argument("--current", default=DEFAULT_CURRENT, help="哪个快照是「当前冻结副本」")
    parser.add_argument("--check", action="store_true", help="只校验，不写盘")
    args = parser.parse_args()

    mod_dir = Path(args.mod_dir)
    backup_dir = mod_dir / "DISABLED" / "_backup"
    if not backup_dir.is_dir():
        print("找不到备份目录: {}".format(backup_dir), file=sys.stderr)
        return 2

    manifest = build(mod_dir, backup_dir, args.current)
    text = render(manifest, args.current)
    manifest_text = json.dumps(manifest, ensure_ascii=False, indent=1)

    readme_path = backup_dir / "README.md"
    manifest_path = backup_dir / "_manifest.json"
    current_readme = backup_dir / args.current / "README.md"

    stale = []
    if readme_path.exists() and readme_path.read_text(encoding="utf-8") != text:
        stale.append(str(readme_path))
    if manifest_path.exists() and manifest_path.read_text(encoding="utf-8") != manifest_text:
        stale.append(str(manifest_path))
    live_readme = mod_dir / "README.md"
    if current_readme.exists() and current_readme.read_bytes() != live_readme.read_bytes():
        stale.append(str(current_readme))

    if args.check:
        if stale:
            print("需要刷新:")
            for item in stale:
                print("  " + item)
            return 1
        print("清单已是最新（{} 个快照 / {} 个 live 文件）".format(
            len(manifest["snapshots"]), len(manifest["live"])))
        return 0

    if current_readme.exists():
        current_readme.write_bytes(live_readme.read_bytes())
    manifest_path.write_text(manifest_text, encoding="utf-8")
    readme_path.write_text(text, encoding="utf-8")
    print("已刷新: {} 个快照 / {} 个 live 文件".format(
        len(manifest["snapshots"]), len(manifest["live"])))
    for folder, entries in manifest["snapshots"].items():
        print("  {} : {} 个文件".format(folder, len(entries)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
