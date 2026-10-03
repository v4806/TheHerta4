"""删除 IB 文件夹后，清理工作空间配置里对它们的引用（纯 stdlib，不依赖 bpy）。

背景（用户反馈 2026-09-24）：`SSMT_OT_CleanupUnusedIB` 只删了子网格文件夹，
配置里的行还留着，于是在 SSMT 软件里依旧能看到已删 IB 的配置。

两类文件、两种判据（关键区别在"这份配置是缓存还是用户意图"）：

1. **缓存类**（提取/导入/标记产物，按**磁盘现状**扫：引用到不存在的部件就是死配置，
   与本轮删没删无关，因此历史遗留的失效条目也能一并清掉）：
   - `Import.json`：键 = `<LOD>.<bare>`（扁平工作空间 = `<bare>`）；
   - `<LOD>/ComponentName_DrawCallIndexList.json`：键 = 子网格名；
   - `<LOD>/DrawIB-Component.json`：`{DrawIB: {componentId: 子网格名}}`；
   - `<LOD>/TrianglelistDedupedFileName.json`：键前缀 = 6 位 drawcall 序号，
     删「只被已消失部件声明、且现存部件都不声明」的序号；
   - 游戏级 `WorkSpace/<Game>/MarkTextureConfig.json`（同游戏多工作空间共享）：
     `subMesh` / `neckSubMesh` / `faceSubMeshes` / `eyeSubMeshes` / `drawCallBySubMesh`
     的值是 `{"tabId":..., "subMeshName":...}` 的 json 字符串（或裸子网格名），
     只清**属于本工作空间 tab** 且部件已不存在的标记，别的角色/工作空间不动。

2. **意图类**（工作页勾选/跳过表，可能是"还没提取"的待办，只按**本轮确实删除**的
   部件清理，绝不因为"目录不存在"就删）：
   - `<LOD>/Config.json`：`[{DrawIB, Alias}]`；
   - `<LOD>/SkipIBConfig.json` / `VSCheckConfig.json`：
     `[{SkipIB|DrawIB, Alias, IndexCount, FirstIndex}]`；
   - `Config/Tabs/<tabId>.json` 的 `modelRows` / `skipRows`
     （`vsRows` 是 VS hash，与 IB 无关，保持原样）。
   部件级行按 `<drawIB>-<indexCount>-<firstIndex>` 身份精确匹配；IB 级行只有在
   "本轮删掉了该 IB 的最后几个部件"时才删。

fail-safe：结构不认识、字段缺失、json 解析失败 → 原样保留；只写真的删掉条目的文件；
`dry_run=True` 只统计不落盘（供 UI 预览）。
"""

import json
import os
import re

# 行里的 DrawIB 键：SkipIBConfig 用 SkipIB，工作页用 drawIB/skipIB，别名表用 DrawIB
_DRAW_IB_ROW_KEYS = ("DrawIB", "drawIB", "SkipIB", "skipIB", "IB", "ib", "VsIB", "vsIB")
_INDEX_COUNT_KEYS = ("IndexCount", "indexCount")
_FIRST_INDEX_KEYS = ("FirstIndex", "firstIndex")
# 工作页 tab 文件里的行字段
_TAB_ROW_FIELDS = ("modelRows", "skipRows")
_LOD_DIR_PATTERN = re.compile(r"^LOD\d+$", re.IGNORECASE)
_DRAWCALL_KEY_PATTERN = re.compile(r"^(\d{6})-")
# 没有 LOD 前缀的身份默认按 LOD0 兼容（与 WorkSpaceHelper.DEFAULT_LOD_NAME 同口径）
_DEFAULT_LOD_NAME = "LOD0"
_MARK_TEXTURE_LIST_FIELDS = ("faceSubMeshes", "eyeSubMeshes")
_MARK_TEXTURE_SINGLE_FIELDS = ("subMesh", "neckSubMesh")


def _read_json(path: str):
    """读取 json；失败返回 (None, False)，调用方据此原样保留文件。"""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle), True
    except Exception:
        return None, False


def _write_json(path: str, payload) -> bool:
    try:
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=4)
        return True
    except Exception as exc:
        print(f"[IB清理] 写入配置失败: {path}，错误: {exc}")
        return False


def _split_identity(unique_str: str) -> tuple[str, str]:
    """拆身份：`LOD0.xxx-1-0` -> ("LOD0", "xxx-1-0")；`xxx-1-0` -> ("", "xxx-1-0")。"""
    text = str(unique_str or "").strip()
    if "." in text:
        head, tail = text.split(".", 1)
        if _LOD_DIR_PATTERN.match(head.strip()) and tail.strip():
            return head.strip().upper(), tail.strip()
    return "", text


def _first_text(row: dict, keys) -> str:
    for key in keys:
        value = row.get(key, "")
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def _row_identity(row: dict) -> tuple[str, str]:
    """行的 (draw_ib, 子网格名)；定位不到具体部件时子网格名为空串。

    子网格名 = `<drawIB>-<indexCount>-<firstIndex>`（EFMI/ZZMI 等按此命名目录），
    与工作空间文件夹名同构，因此可直接和现存/已删集合比对。
    """
    draw_ib = _first_text(row, _DRAW_IB_ROW_KEYS)
    if not draw_ib:
        return "", ""
    index_count = _first_text(row, _INDEX_COUNT_KEYS)
    first_index = _first_text(row, _FIRST_INDEX_KEYS)
    if index_count and first_index:
        return draw_ib, f"{draw_ib}-{index_count}-{first_index}"
    return draw_ib, ""


def _has_remaining_folder(draw_ib: str, remaining_bares: set[str]) -> bool:
    """该 DrawIB 在对应 LOD 目录里是否还有部件文件夹（`<drawib>-...`）。"""
    prefix = draw_ib + "-"
    return any(bare == draw_ib or bare.startswith(prefix) for bare in remaining_bares)


def _identity_exists(
    lod_name: str, bare_name: str, remaining_by_lod: dict[str, set[str]]
) -> bool:
    """身份对应的部件是否还在磁盘上。

    裸身份（无 LOD 前缀）按默认 LOD0 兼容：与运行时解析
    （WorkSpaceHelper.get_submesh_folder_path 的 LOD0 兜底）同口径，避免把
    "扁平工作空间身份 + 现存在 LOD0 目录里"的条目误判成死配置。
    """
    if bare_name in remaining_by_lod.get(lod_name, set()):
        return True
    if not lod_name and bare_name in remaining_by_lod.get(_DEFAULT_LOD_NAME, set()):
        return True
    return False


def _parse_mark_ref(value) -> tuple[str, str]:
    """解析 MarkTexture 引用 -> (tab_id, 子网格名)；解析不出返回 ("", "")。

    值形态：`'{"tabId":"ws-tab-1","subMeshName":"aaaa-1-0"}'`（含 drawCall 的更长
    形态同样能解析）或裸子网格名。
    """
    if not isinstance(value, str):
        return "", ""
    text = value.strip()
    if not text:
        return "", ""
    if text.startswith("{"):
        try:
            payload = json.loads(text)
        except Exception:
            return "", ""
        if not isinstance(payload, dict):
            return "", ""
        return (
            str(payload.get("tabId", "") or "").strip(),
            str(payload.get("subMeshName", "") or "").strip(),
        )
    return "", text


class WorkspaceConfigPruner:
    """把「已删除 / 已不存在」的 IB 部件从工作空间与游戏级配置里清掉。"""

    # ------------------------------------------------------------------
    # 入口
    # ------------------------------------------------------------------

    @staticmethod
    def prune(
        workspace_folder: str,
        deleted_lod_bare_pairs=(),
        game_folder: str = "",
        dry_run: bool = False,
    ) -> dict[str, int]:
        """清理配置引用。

        workspace_folder：工作空间根目录。
        deleted_lod_bare_pairs：**本轮确实删除成功**的 (lod_name, bare_name) 集合
            （lod_name 大写；扁平工作空间为 ""）。只用于「意图类」文件——缓存类文件
            一律按磁盘现状判定，因此传空集也能清掉历史遗留的失效条目。
        game_folder：游戏工作空间根（`.../WorkSpace/<Game>/`），用于定位共享的
            MarkTextureConfig.json；留空时用工作空间目录的上一级兜底。
        dry_run：只统计不写盘（UI 预览用）。

        返回 {相对工作空间的路径: 删除条数}（只含真正有删除的文件）。
        """
        workspace_folder = str(workspace_folder or "").strip()
        if not workspace_folder or not os.path.isdir(workspace_folder):
            return {}

        deleted_by_lod = WorkspaceConfigPruner._group_by_lod(deleted_lod_bare_pairs)
        remaining_by_lod = WorkspaceConfigPruner._collect_remaining_bares(workspace_folder)
        tab_lod_map = WorkspaceConfigPruner._workspace_tab_lod_map(workspace_folder)
        removed: dict[str, int] = {}

        def _record(rel_path: str, count: int):
            if count > 0:
                removed[rel_path] = removed.get(rel_path, 0) + count

        # 1) 导入缓存（跨 LOD，键里带 LOD 前缀）
        _record(
            "Import.json",
            WorkspaceConfigPruner._prune_import_json(
                os.path.join(workspace_folder, "Import.json"),
                remaining_by_lod,
                dry_run,
            ),
        )

        # 2) 各配置目录（根/分区 × 根桶与各 LOD 目录）
        for lod_dir, lod_name, rel_prefix in WorkspaceConfigPruner._config_scan_dirs(
            workspace_folder
        ):
            deleted_bares = deleted_by_lod.get(lod_name, set())
            remaining_bares = remaining_by_lod.get(lod_name, set())

            index_path = os.path.join(lod_dir, "ComponentName_DrawCallIndexList.json")
            # 贴图去重表必须先算：它要读**清理前**的 drawcall 声明表
            _record(
                rel_prefix + "TrianglelistDedupedFileName.json",
                WorkspaceConfigPruner._prune_trianglelist(
                    os.path.join(lod_dir, "TrianglelistDedupedFileName.json"),
                    index_path,
                    remaining_bares,
                    dry_run,
                ),
            )
            _record(
                rel_prefix + "ComponentName_DrawCallIndexList.json",
                WorkspaceConfigPruner._prune_keyed_json(
                    index_path, remaining_bares, dry_run
                ),
            )
            _record(
                rel_prefix + "DrawIB-Component.json",
                WorkspaceConfigPruner._prune_drawib_component(
                    os.path.join(lod_dir, "DrawIB-Component.json"),
                    remaining_bares,
                    dry_run,
                ),
            )
            for config_name in ("Config.json", "SkipIBConfig.json", "VSCheckConfig.json"):
                _record(
                    rel_prefix + config_name,
                    WorkspaceConfigPruner._prune_row_list(
                        os.path.join(lod_dir, config_name),
                        deleted_bares,
                        remaining_bares,
                        dry_run,
                    ),
                )

        # 3) 工作页 tab（意图类：只清本轮删掉的部件）
        for tab_id, lod_name in tab_lod_map.items():
            deleted_bares, remaining_bares = WorkspaceConfigPruner._resolve_tab_bucket(
                lod_name, deleted_by_lod, remaining_by_lod
            )
            if not deleted_bares:
                continue
            tab_path = os.path.join(workspace_folder, "Config", "Tabs", f"{tab_id}.json")
            if not os.path.isfile(tab_path):
                continue
            _record(
                f"Config/Tabs/{tab_id}.json",
                WorkspaceConfigPruner._prune_tab_file(
                    tab_path, deleted_bares, remaining_bares, dry_run
                ),
            )

        # 4) 游戏级共享贴图标记（缓存类：按磁盘现状扫本工作空间的 tab）
        for mark_path in WorkspaceConfigPruner._mark_texture_candidates(
            workspace_folder, game_folder
        ):
            rel_path = os.path.relpath(mark_path, workspace_folder)
            if rel_path.startswith(".."):
                # 游戏级共享文件（在工作空间之外）：用可读标签而不是一堆 ..\
                rel_path = os.path.join("<游戏>", os.path.basename(mark_path))
            _record(
                rel_path.replace("\\", "/"),
                WorkspaceConfigPruner._prune_mark_texture(
                    mark_path, tab_lod_map, remaining_by_lod, dry_run
                ),
            )

        return removed

    # ------------------------------------------------------------------
    # 工作空间扫描
    # ------------------------------------------------------------------

    @staticmethod
    def _group_by_lod(deleted_lod_bare_pairs) -> dict[str, set[str]]:
        grouped: dict[str, set[str]] = {}
        for lod_name, bare_name in deleted_lod_bare_pairs or ():
            lod_key = str(lod_name or "").strip().upper()
            bare_key = str(bare_name or "").strip()
            if not bare_key:
                continue
            grouped.setdefault(lod_key, set()).add(bare_key)
        return grouped

    @staticmethod
    def _workspace_base_paths(workspace_folder: str) -> list[str]:
        """工作空间根；根目录没有可导入内容时改为分区目录（含 Config.json 的子目录）。

        与 WorkSpaceHelper.get_workspace_partition_folderpath_list 同契约：分区工作
        空间的子网格在 `<分区>/LOD0/<bare>`，身份键仍不含分区名。
        """
        bases = [workspace_folder]
        try:
            root_entries = list(os.scandir(workspace_folder))
        except OSError:
            return bases

        def _has_submesh_dirs(path: str) -> bool:
            try:
                children = list(os.scandir(path))
            except OSError:
                return False
            return any(
                child.is_dir() and len(child.name.split("-")) >= 3 for child in children
            )

        has_lod_dirs = any(
            entry.is_dir() and _LOD_DIR_PATTERN.match(entry.name)
            for entry in root_entries
        )
        if has_lod_dirs or _has_submesh_dirs(workspace_folder):
            return bases

        partitions = [
            entry.path
            for entry in root_entries
            if entry.is_dir()
            and os.path.isfile(os.path.join(entry.path, "Config.json"))
        ]
        return partitions or bases

    @staticmethod
    def _collect_remaining_bares(workspace_folder: str) -> dict[str, set[str]]:
        """扫描工作空间里现存的部件文件夹名，按 LOD 分组（分区工作空间一并扫）。

        判定与 WorkSpaceHelper._get_submesh_folderpath_list_from 同口径：目录名按
        `-` 分段 >= 3 段（`<drawib>-<indexCount>-<firstIndex>`）。存在的 LOD 目录
        一律建桶（哪怕是空集合），这样"整个 LOD 的部件全被删掉"也能被清理到。
        """
        remaining: dict[str, set[str]] = {"": set()}
        for base_path in WorkspaceConfigPruner._workspace_base_paths(workspace_folder):
            try:
                entries = list(os.scandir(base_path))
            except OSError:
                continue
            for entry in entries:
                if entry.is_dir() and _LOD_DIR_PATTERN.match(entry.name):
                    remaining.setdefault(entry.name.upper(), set())

        for base_path in WorkspaceConfigPruner._workspace_base_paths(workspace_folder):
            for lod_name in list(remaining.keys()):
                scan_dir = (
                    os.path.join(base_path, lod_name) if lod_name else base_path
                )
                if not os.path.isdir(scan_dir):
                    continue
                try:
                    children = list(os.scandir(scan_dir))
                except OSError:
                    continue
                for child in children:
                    if not child.is_dir():
                        continue
                    if len(child.name.split("-")) >= 3:
                        remaining[lod_name].add(child.name)
        return remaining

    @staticmethod
    def _config_scan_dirs(workspace_folder: str) -> list[tuple[str, str, str]]:
        """返回 [(配置目录, LOD 名, 报告用相对前缀)]：根/分区 × 根桶与各 LOD 目录。"""
        scan_dirs: list[tuple[str, str, str]] = []
        for base_path in WorkspaceConfigPruner._workspace_base_paths(workspace_folder):
            if os.path.normcase(os.path.abspath(base_path)) == os.path.normcase(
                os.path.abspath(workspace_folder)
            ):
                base_label = ""
            else:
                base_label = os.path.basename(base_path).replace("\\", "/") + "/"
            scan_dirs.append((base_path, "", base_label))
            try:
                entries = list(os.scandir(base_path))
            except OSError:
                continue
            for entry in entries:
                if entry.is_dir() and _LOD_DIR_PATTERN.match(entry.name):
                    lod_name = entry.name.upper()
                    scan_dirs.append((entry.path, lod_name, f"{base_label}{lod_name}/"))
        return scan_dirs

    @staticmethod
    def _workspace_tab_lod_map(workspace_folder: str) -> dict[str, str]:
        """工作页 tabId -> LOD 名（`Config/WorkPageTabs.json`；tab 名不是 LOD 时为 ""）。"""
        payload, ok = _read_json(
            os.path.join(workspace_folder, "Config", "WorkPageTabs.json")
        )
        if not ok or not isinstance(payload, dict):
            return {}
        tab_map: dict[str, str] = {}
        for tab in payload.get("tabs", []) or []:
            if not isinstance(tab, dict):
                continue
            tab_id = str(tab.get("id", "") or "").strip()
            if not tab_id:
                continue
            name = str(tab.get("name", "") or "").strip()
            tab_map[tab_id] = name.upper() if _LOD_DIR_PATTERN.match(name) else ""
        return tab_map

    @staticmethod
    def _resolve_tab_bucket(lod_name, deleted_by_lod, remaining_by_lod):
        """tab 对应的 (本轮删除集合, 现存集合)；名字对不上 LOD 时按唯一 LOD 桶兜底。"""
        if lod_name in deleted_by_lod:
            return deleted_by_lod[lod_name], remaining_by_lod.get(lod_name, set())
        if len(deleted_by_lod) == 1:
            only_lod = next(iter(deleted_by_lod))
            return deleted_by_lod[only_lod], remaining_by_lod.get(only_lod, set())
        return set(), set()

    @staticmethod
    def _mark_texture_candidates(workspace_folder: str, game_folder: str) -> list[str]:
        candidates: list[str] = []
        seen: set[str] = set()
        for base in (game_folder, os.path.dirname(os.path.normpath(workspace_folder))):
            base = str(base or "").strip()
            if not base:
                continue
            path = os.path.join(base, "MarkTextureConfig.json")
            if not os.path.isfile(path):
                continue
            normalized = os.path.normcase(os.path.abspath(path))
            if normalized in seen:
                continue
            seen.add(normalized)
            candidates.append(path)
        return candidates

    # ------------------------------------------------------------------
    # 缓存类文件清理（按磁盘现状）
    # ------------------------------------------------------------------

    @staticmethod
    def _prune_import_json(
        path: str, remaining_by_lod: dict[str, set[str]], dry_run: bool
    ) -> int:
        payload, ok = _read_json(path)
        if not ok or not isinstance(payload, dict):
            return 0
        removed = 0
        for key in list(payload.keys()):
            lod_name, bare_name = _split_identity(key)
            if bare_name and not _identity_exists(lod_name, bare_name, remaining_by_lod):
                del payload[key]
                removed += 1
        if removed and not dry_run:
            _write_json(path, payload)
        return removed

    @staticmethod
    def _prune_keyed_json(
        path: str, remaining_bares: set[str], dry_run: bool
    ) -> int:
        """键 = 子网格名的字典（ComponentName_DrawCallIndexList.json）。"""
        payload, ok = _read_json(path)
        if not ok or not isinstance(payload, dict):
            return 0
        removed = 0
        for key in list(payload.keys()):
            if str(key).strip() not in remaining_bares:
                del payload[key]
                removed += 1
        if removed and not dry_run:
            _write_json(path, payload)
        return removed

    @staticmethod
    def _prune_drawib_component(
        path: str, remaining_bares: set[str], dry_run: bool
    ) -> int:
        payload, ok = _read_json(path)
        if not ok or not isinstance(payload, dict):
            return 0
        removed = 0
        for draw_ib in list(payload.keys()):
            component_map = payload.get(draw_ib)
            if not isinstance(component_map, dict):
                continue
            for component_id in list(component_map.keys()):
                if str(component_map[component_id]).strip() not in remaining_bares:
                    del component_map[component_id]
                    removed += 1
            if not component_map:
                del payload[draw_ib]
        if removed and not dry_run:
            _write_json(path, payload)
        return removed

    @staticmethod
    def _prune_trianglelist(
        path: str, component_index_path: str, remaining_bares: set[str], dry_run: bool
    ) -> int:
        """贴图去重表：删「只被已消失部件声明、现存部件都不声明」的 drawcall 序号。

        两个条件同时满足才删，避免误删共享贴图：
        - 该序号由**已消失**的部件声明（声明表里能查到），
        - 且**没有任何现存部件**声明它。
        声明表缺失/解析失败、或序号没人声明时整条保持原样（无法判定归属，宁可少删）。
        """
        claims, ok = _read_json(component_index_path)
        if not ok or not isinstance(claims, dict):
            return 0
        missing_claims: set[str] = set()
        remaining_claims: set[str] = set()
        for bare_name, drawcalls in claims.items():
            if not isinstance(drawcalls, list):
                continue
            target = (
                remaining_claims
                if str(bare_name).strip() in remaining_bares
                else missing_claims
            )
            target.update(str(item).strip() for item in drawcalls)

        payload, ok = _read_json(path)
        if not ok or not isinstance(payload, dict):
            return 0
        removed = 0
        for key in list(payload.keys()):
            match = _DRAWCALL_KEY_PATTERN.match(str(key))
            if not match:
                continue
            drawcall = match.group(1)
            if drawcall not in missing_claims or drawcall in remaining_claims:
                continue
            del payload[key]
            removed += 1
        if removed and not dry_run:
            _write_json(path, payload)
        return removed

    @staticmethod
    def _prune_mark_texture(
        path: str,
        tab_lod_map: dict[str, str],
        remaining_by_lod: dict[str, set[str]],
        dry_run: bool,
    ) -> int:
        """游戏级 MarkTextureConfig.json：只清本工作空间 tab 且部件已不存在的标记。"""
        payload, ok = _read_json(path)
        if not ok or not isinstance(payload, dict):
            return 0

        def _is_stale(value) -> bool:
            tab_id, sub_mesh_name = _parse_mark_ref(value)
            if not sub_mesh_name:
                return False
            if tab_id:
                if tab_id not in tab_lod_map:
                    return False  # 别的角色/工作空间的标记，不动
                return sub_mesh_name not in remaining_by_lod.get(
                    tab_lod_map[tab_id], set()
                )
            return not any(
                sub_mesh_name in bares for bares in remaining_by_lod.values()
            )

        removed = 0
        for field in _MARK_TEXTURE_SINGLE_FIELDS:
            if _is_stale(payload.get(field)):
                payload[field] = ""
                removed += 1
        for field in _MARK_TEXTURE_LIST_FIELDS:
            value = payload.get(field)
            if not isinstance(value, list):
                continue
            kept = [item for item in value if not _is_stale(item)]
            if len(kept) != len(value):
                removed += len(value) - len(kept)
                payload[field] = kept
        mapping = payload.get("drawCallBySubMesh")
        if isinstance(mapping, dict):
            for key in list(mapping.keys()):
                if _is_stale(key) or _is_stale(mapping.get(key)):
                    del mapping[key]
                    removed += 1
        if removed and not dry_run:
            _write_json(path, payload)
        return removed

    # ------------------------------------------------------------------
    # 意图类文件清理（只按本轮确实删除的部件）
    # ------------------------------------------------------------------

    @staticmethod
    def _prune_row_list(
        path: str,
        deleted_bares: set[str],
        remaining_bares: set[str],
        dry_run: bool,
    ) -> int:
        """行表（Config.json / SkipIBConfig.json / VSCheckConfig.json）。"""
        payload, ok = _read_json(path)
        if not ok or not isinstance(payload, list):
            return 0
        kept = []
        removed = 0
        for row in payload:
            if not isinstance(row, dict):
                kept.append(row)
                continue
            if WorkspaceConfigPruner._row_is_stale(row, deleted_bares, remaining_bares):
                removed += 1
                continue
            kept.append(row)
        if removed and not dry_run:
            _write_json(path, kept)
        return removed

    @staticmethod
    def _prune_tab_file(
        path: str,
        deleted_bares: set[str],
        remaining_bares: set[str],
        dry_run: bool,
    ) -> int:
        """工作页 tab：modelRows / skipRows（vsRows 是 VS hash，不动）。"""
        payload, ok = _read_json(path)
        if not ok or not isinstance(payload, dict):
            return 0
        removed = 0
        for field in _TAB_ROW_FIELDS:
            rows = payload.get(field)
            if not isinstance(rows, list):
                continue
            kept = []
            for row in rows:
                if not isinstance(row, dict):
                    kept.append(row)
                    continue
                if WorkspaceConfigPruner._row_is_stale(
                    row, deleted_bares, remaining_bares
                ):
                    removed += 1
                    continue
                kept.append(row)
            if len(kept) != len(rows):
                payload[field] = kept
        if removed and not dry_run:
            _write_json(path, payload)
        return removed

    @staticmethod
    def _row_is_stale(
        row: dict, deleted_bares: set[str], remaining_bares: set[str]
    ) -> bool:
        """意图类行的失效判据（只认本轮删除，不认"目录本来就不存在"）。

        - 部件级行（带 IndexCount/FirstIndex）：身份 ∈ 本轮删除集合；
        - IB 级行：本轮删掉了该 IB 的最后几个部件（现在没有任何剩余部件），
          且本轮确实删到过它的部件——"本来就没提取"的行属于待办意图，保留。
        """
        draw_ib, bare_name = _row_identity(row)
        if not draw_ib:
            return False
        if bare_name:
            return bare_name in deleted_bares
        if _has_remaining_folder(draw_ib, remaining_bares):
            return False
        prefix = draw_ib + "-"
        return any(
            candidate == draw_ib or candidate.startswith(prefix)
            for candidate in deleted_bares
        )
