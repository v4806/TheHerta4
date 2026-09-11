# -*- coding: utf-8 -*-
"""导出配置表（*.ini）备份工具。

Generate Mod 会在导出目录根目录生成/覆盖配置表（如 <工作空间名>.ini）。
在弹窗确认后，将旧配置表改名并移动至备份目录：

- 选择「是」：备份旧配置表后正常导出，生成新的配置表（旧配置表保留在备份中）；
- 选择「否」：备份旧配置表后正常导出，导出完成后将备份的旧配置表覆盖回原位。

备份目录位于导出目录内的 ``.config_table_backup/<时间戳>/``，
备份文件以 ``.bak`` 后缀命名（沿用 ``Toolset/mod_chinese_to_english.py`` 的
``.mod_ini_english_backup`` 约定），避免被递归扫描 ``*.ini`` 的后处理节点
（Buffer 清理 / 注释清理等）误当作当前配置表处理。

保留上限（t8/A-opt2）：``.config_table_backup/`` 下最多保留
``BACKUP_SETS_TO_KEEP`` 份**时间戳备份集**（**含本次新增的那一份**），超出部分
在**创建新备份目录之前**清理。顺序保证「绝不删除当前正在使用的备份」：

1. ``backup_config_tables`` 先确定本批备份目录名并调用
   ``prune_old_backups(..., protected_dir_name=本批目录名)``，此时本批备份目录
   **尚未创建**（``os.makedirs`` 在 prune 之后），因此不可能落进候选集；
   由于本体要占掉一个名额，prune 的目标是 ``BACKUP_SETS_TO_KEEP - 1`` 份，
   这样「保留的旧份 + 本次新份」正好 == ``BACKUP_SETS_TO_KEEP``；
2. prune 只枚举 ``.config_table_backup`` 下符合 ``<时间戳>`` 命名
   （``BACKUP_SET_PATTERN``）的**直接子目录**；绝不触碰备份根目录本身、
   绝不触碰非时间戳命名的目录（例如用户手工放置的内容）；
3. 清理口径是「保留最新的 N 份」，排序键为目录名字典序——时间戳格式
   ``%Y%m%d_%H%M%S_%f`` 零填充定长，字典序等价于时间序；
4. ``restore_config_tables`` **不做任何清理**：它可能在导出失败或用户选择
   「否」时被调用，此时该备份必须原样保留（即使已不在保留窗口内，也留到
   下一次备份时再清）。
"""

import datetime
import os
import re
import shutil

from ..utils.log_utils import LOG

BACKUP_DIR_NAME = ".config_table_backup"
BACKUP_SUFFIX = ".bak"

# 保留的时间戳备份集份数上限（含最新一份）。<=0 表示不清理（等价旧行为）。
BACKUP_SETS_TO_KEEP = 20
# 时间戳备份目录名格式：%Y%m%d_%H%M%S_%f（零填充定长 → 字典序 == 时间序）。
BACKUP_SET_PATTERN = re.compile(r"^\d{8}_\d{6}_\d{6}$")


def _backup_root_dir(mod_export_path: str) -> str:
    """备份根目录：<导出目录>/.config_table_backup。"""
    return os.path.join(mod_export_path, BACKUP_DIR_NAME)


def prune_old_backups(
    mod_export_path: str,
    keep: int = BACKUP_SETS_TO_KEEP,
    protected_dir_name: str = "",
) -> list:
    """清理超出保留上限的旧时间戳备份集，返回被删除的目录路径列表。

    只删除 ``.config_table_backup`` 下**符合时间戳命名**的直接子目录中最旧的
    若干份；``protected_dir_name`` 命名的目录永不删除（当前正在使用的备份）；
    保留上限内的目录、非时间戳命名的目录、以及备份根目录本身一律不动。
    ``keep`` <= 0 时直接返回（等价旧行为：不清理）。

    本函数是尽力而为的维护动作：任何失败都只记警告，不抛异常影响备份/导出。
    """
    removed: list = []
    if keep is None or int(keep) <= 0:
        return removed
    backup_root = _backup_root_dir(mod_export_path)
    if not os.path.isdir(backup_root):
        return removed

    try:
        entries = list(os.scandir(backup_root))
    except OSError as e:
        LOG.warning(f"配置表备份目录读取失败，跳过清理: {backup_root}: {e}")
        return removed

    candidates: list = []
    for entry in entries:
        try:
            if not entry.is_dir():
                continue
        except OSError:
            continue
        name = entry.name
        if not BACKUP_SET_PATTERN.match(name):
            continue  # 非时间戳命名（手工放置等）永不删除
        if protected_dir_name and name == protected_dir_name:
            continue  # 当前正在使用的备份
        candidates.append((name, entry.path))

    if len(candidates) <= int(keep):
        return removed

    candidates.sort(key=lambda item: item[0])  # 字典序 == 时间序（旧 → 新）
    for name, path in candidates[: len(candidates) - int(keep)]:
        try:
            shutil.rmtree(path)
        except Exception as e:
            LOG.warning(f"旧配置表备份清理失败（保留不动）: {path}: {e}")
            continue
        removed.append(path)
        LOG.info(f"🧹 已清理过期配置表备份（保留最近 {int(keep)} 份）: {name}")
    return removed


def find_config_table_files(mod_export_path: str) -> list:
    """查找导出目录内的配置表（根目录 *.ini 文件），返回完整路径列表（已排序）。"""
    if not mod_export_path or not os.path.isdir(mod_export_path):
        return []
    return sorted(
        os.path.join(mod_export_path, name)
        for name in os.listdir(mod_export_path)
        if os.path.isfile(os.path.join(mod_export_path, name))
        and name.lower().endswith(".ini")
    )


def backup_config_tables(mod_export_path: str, config_table_paths) -> list:
    """把配置表改名并移动至备份目录，返回 ``[(原路径, 备份路径), ...]``。

    已不存在的文件会被跳过并记录警告；移动失败的文件同样跳过。
    创建本批备份目录**之前**先按 ``BACKUP_SETS_TO_KEEP`` 清理旧备份集
    （顺序不可交换，见模块 docstring：当前批次那时尚未创建，不可能被误删）。
    """
    entries = []
    if not config_table_paths:
        return entries

    # 1) 先定本批备份目录名（时间戳）；2) 清理旧备份（保护本批名字）；3) 才创建。
    # 清理目标为 keep-1 份：本批目录在 prune 之后才创建，最终总数 == keep。
    backup_set_name = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    try:
        prune_old_backups(
            mod_export_path, max(0, int(BACKUP_SETS_TO_KEEP) - 1), backup_set_name
        )
    except Exception as e:
        # 保留策略是维护动作，绝不允许它影响正常备份流程。
        LOG.warning(f"旧配置表备份清理异常（已忽略，继续备份）: {e}")

    backup_root = os.path.join(_backup_root_dir(mod_export_path), backup_set_name)
    os.makedirs(backup_root, exist_ok=True)

    for original_path in config_table_paths:
        if not os.path.isfile(original_path):
            LOG.warning(f"配置表已不存在，跳过备份: {original_path}")
            continue
        backup_path = os.path.join(
            backup_root, os.path.basename(original_path) + BACKUP_SUFFIX
        )
        try:
            shutil.move(original_path, backup_path)
        except Exception as e:
            LOG.error(f"配置表备份失败: {original_path} -> {backup_path}: {e}")
            continue
        entries.append((original_path, backup_path))
        LOG.info(
            f"💾 配置表已改名并移动至备份: {os.path.basename(original_path)} -> {backup_path}"
        )
    return entries


def restore_config_tables(entries) -> None:
    """导出完成后把备份的配置表覆盖回原位（备份副本保留）。

    注意：本函数**不做任何备份清理**（t8/A-opt2 顺序约定）——导出失败或用户
    选择「否」时该备份仍必须存在，清理只发生在下一次 backup_config_tables。
    """
    if not entries:
        return
    for original_path, backup_path in entries:
        if not os.path.isfile(backup_path):
            LOG.warning(f"配置表备份已不存在，跳过恢复: {backup_path}")
            continue
        try:
            shutil.copy2(backup_path, original_path)
        except Exception as e:
            LOG.error(f"配置表恢复失败: {backup_path} -> {original_path}: {e}")
            continue
        LOG.info(f"♻️ 配置表已恢复: {os.path.basename(original_path)} <- {backup_path}")
