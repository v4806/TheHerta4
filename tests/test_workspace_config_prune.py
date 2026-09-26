"""清理 IB 文件夹后同步清理配置引用的单测。

背景（用户反馈）：`ssmt.cleanup_unused_ib` 只删了子网格文件夹，配置里的行还留着，
于是在 SSMT 软件里依旧能看到已删 IB 的配置（工作页行、别名表、SkipIBConfig、
Import.json、贴图去重表、游戏级 MarkTextureConfig.json 的贴图标记）。

覆盖：
- Import.json 的 `<LOD>.<bare>` 键（含扁平工作空间的裸键）；
- ComponentName_DrawCallIndexList.json / DrawIB-Component.json（含"整 IB 无剩余部件才删键"）；
- Config.json / SkipIBConfig.json / VSCheckConfig.json 的行（部件级精确匹配 + IB 级按剩余部件）；
- Config/Tabs/<tabId>.json 的 modelRows / skipRows（vsRows 是 VS hash，必须原样保留）；
- TrianglelistDedupedFileName.json 的 drawcall 序号（被删部件声明、且剩余部件不再声明）；
- 游戏级 MarkTextureConfig.json（只清本工作空间 tab 的标记，别的 tab 不动）；
- fail-safe：结构不认识 / json 损坏 / 删除集合为空时一律不动文件。
"""

import importlib.util
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
MODULE_NAME = "workspace_config_pruner_test_module"

_spec = importlib.util.spec_from_file_location(
    MODULE_NAME, REPO_ROOT / "common" / "workspace_config_pruner.py"
)
_pruner_module = importlib.util.module_from_spec(_spec)
sys.modules[MODULE_NAME] = _pruner_module
_spec.loader.exec_module(_pruner_module)
WorkspaceConfigPruner = _pruner_module.WorkspaceConfigPruner

DELETED = {("LOD0", "fe47dc61-7014-0"), ("LOD1", "bbbb2222-200-0"), ("LOD0", "dddd4444-400-0")}


def _mark_ref(tab_id: str, sub_mesh_name: str, drawcall: str = "") -> str:
    payload = {"tabId": tab_id, "subMeshName": sub_mesh_name}
    if drawcall:
        payload["drawCall"] = drawcall
    return json.dumps(payload, ensure_ascii=False)


class WorkspaceConfigPruneTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.game = Path(self._tmp.name) / "EFMI"
        self.root = self.game / "庄方宜"
        (self.root / "Config" / "Tabs").mkdir(parents=True)
        # 现存部件：LOD0 两个（含同一 DrawIB 的另一个部件），LOD1 一个
        for rel in (
            "LOD0/aaaa1111-100-0/TYPE_GPU-X",
            "LOD0/dddd4444-500-0/TYPE_GPU-X",
            "LOD1/cccc3333-300-0/TYPE_GPU-X",
        ):
            (self.root / rel).mkdir(parents=True)

    def tearDown(self):
        self._tmp.cleanup()

    # ------------------------------------------------------------------
    # 工具
    # ------------------------------------------------------------------

    def _write(self, rel_path: str, payload, base: Path | None = None):
        path = (base or self.root) / rel_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=4), encoding="utf-8")
        return path

    def _read(self, rel_path: str, base: Path | None = None):
        return json.loads(((base or self.root) / rel_path).read_text(encoding="utf-8"))

    def _prune(self, deleted=DELETED) -> dict:
        return WorkspaceConfigPruner.prune(
            str(self.root), deleted, game_folder=str(self.game)
        )

    # ------------------------------------------------------------------
    # 逐文件
    # ------------------------------------------------------------------

    def test_import_json_keys(self):
        self._write(
            "Import.json",
            {
                "LOD0.fe47dc61-7014-0": "GPU-X",
                "LOD0.aaaa1111-100-0": "GPU-X",
                "LOD1.bbbb2222-200-0": "GPU-X",
                "LOD1.cccc3333-300-0": "GPU-X",
                "dddd4444-500-0": "GPU-X",
            },
        )
        removed = self._prune()
        payload = self._read("Import.json")
        self.assertNotIn("LOD0.fe47dc61-7014-0", payload)
        self.assertNotIn("LOD1.bbbb2222-200-0", payload)
        self.assertIn("LOD0.aaaa1111-100-0", payload)
        self.assertIn("LOD1.cccc3333-300-0", payload)
        self.assertIn("dddd4444-500-0", payload)
        self.assertEqual(removed["Import.json"], 2)

    def test_component_index_list_and_drawib_component(self):
        self._write(
            "LOD0/ComponentName_DrawCallIndexList.json",
            {
                "fe47dc61-7014-0": ["000055"],
                "aaaa1111-100-0": ["000010"],
                "dddd4444-400-0": ["000011"],
                "dddd4444-500-0": ["000012"],
            },
        )
        self._write(
            "LOD0/DrawIB-Component.json",
            {
                "fe47dc61": {"0": "fe47dc61-7014-0"},
                "dddd4444": {"0": "dddd4444-400-0", "1": "dddd4444-500-0"},
                "aaaa1111": {"0": "aaaa1111-100-0"},
            },
        )
        removed = self._prune()

        index_list = self._read("LOD0/ComponentName_DrawCallIndexList.json")
        self.assertEqual(sorted(index_list), ["aaaa1111-100-0", "dddd4444-500-0"])
        self.assertEqual(removed["LOD0/ComponentName_DrawCallIndexList.json"], 2)

        component_map = self._read("LOD0/DrawIB-Component.json")
        self.assertNotIn("fe47dc61", component_map)  # 整 IB 没部件了 → 键也删掉
        self.assertEqual(component_map["dddd4444"], {"1": "dddd4444-500-0"})
        self.assertEqual(removed["LOD0/DrawIB-Component.json"], 2)

    def test_row_lists_part_level_and_ib_level(self):
        self._write(
            "LOD0/Config.json",
            [
                {"DrawIB": "fe47dc61", "Alias": "已删"},
                {"DrawIB": "dddd4444", "Alias": ""},
                {"DrawIB": "aaaa1111", "Alias": ""},
            ],
        )
        self._write(
            "LOD0/SkipIBConfig.json",
            [
                {"SkipIB": "fe47dc61", "Alias": "", "IndexCount": "7014", "FirstIndex": "0"},
                {"SkipIB": "dddd4444", "Alias": "", "IndexCount": "400", "FirstIndex": "0"},
                {"SkipIB": "dddd4444", "Alias": "", "IndexCount": "500", "FirstIndex": "0"},
                {"SkipIB": "fe47dc61", "Alias": "", "IndexCount": "", "FirstIndex": ""},
            ],
        )
        self._write(
            "LOD0/VSCheckConfig.json",
            [{"enabled": True, "hash": "2b9b2d1f6ef66522"}],
        )
        removed = self._prune()

        config_rows = self._read("LOD0/Config.json")
        self.assertEqual([row["DrawIB"] for row in config_rows], ["dddd4444", "aaaa1111"])

        skip_rows = self._read("LOD0/SkipIBConfig.json")
        self.assertEqual(
            [(row["SkipIB"], row["IndexCount"]) for row in skip_rows],
            [("dddd4444", "500")],
        )

        # 结构不认识的行表原样保留（fail-safe）
        self.assertEqual(
            self._read("LOD0/VSCheckConfig.json"),
            [{"enabled": True, "hash": "2b9b2d1f6ef66522"}],
        )
        self.assertNotIn("LOD0/VSCheckConfig.json", removed)

    def test_tab_rows_pruned_vs_rows_untouched(self):
        self._write(
            "Config/WorkPageTabs.json",
            {
                "activeTabId": "ws-tab-1",
                "tabs": [
                    {"id": "ws-tab-1", "name": "LOD0"},
                    {"id": "ws-tab-2", "name": "LOD1"},
                ],
            },
        )
        self._write(
            "Config/Tabs/ws-tab-1.json",
            {
                "modelRows": [
                    {"drawIB": "fe47dc61", "aliasName": ""},
                    {"drawIB": "dddd4444", "aliasName": ""},
                    {"drawIB": "aaaa1111", "aliasName": "留下"},
                ],
                "skipRows": [
                    {"skipIB": "dddd4444", "aliasName": "", "indexCount": "400", "firstIndex": "0"},
                    {"skipIB": "dddd4444", "aliasName": "", "indexCount": "500", "firstIndex": "0"},
                ],
                "vsRows": [{"enabled": True, "hash": "2b9b2d1f6ef66522"}],
                "frameAnalysisFolderPath": "K:\\dump\\FrameAnalysis-1",
            },
        )
        self._write(
            "Config/Tabs/ws-tab-2.json",
            {"modelRows": [{"drawIB": "bbbb2222", "aliasName": ""}], "vsRows": []},
        )
        removed = self._prune()

        tab1 = self._read("Config/Tabs/ws-tab-1.json")
        self.assertEqual([row["drawIB"] for row in tab1["modelRows"]], ["dddd4444", "aaaa1111"])
        self.assertEqual([row["indexCount"] for row in tab1["skipRows"]], ["500"])
        self.assertEqual(tab1["vsRows"], [{"enabled": True, "hash": "2b9b2d1f6ef66522"}])
        self.assertEqual(tab1["frameAnalysisFolderPath"], "K:\\dump\\FrameAnalysis-1")

        tab2 = self._read("Config/Tabs/ws-tab-2.json")
        self.assertEqual(tab2["modelRows"], [])

        self.assertEqual(removed["Config/Tabs/ws-tab-1.json"], 2)
        self.assertEqual(removed["Config/Tabs/ws-tab-2.json"], 1)

    def test_trianglelist_drawcall_entries(self):
        self._write(
            "LOD0/ComponentName_DrawCallIndexList.json",
            {
                "fe47dc61-7014-0": ["000055"],
                "aaaa1111-100-0": ["000010"],
                "dddd4444-400-0": ["000011"],
                "dddd4444-500-0": ["000011"],  # 与已删部件共享同一 drawcall → 保留
            },
        )
        self._write(
            "LOD0/TrianglelistDedupedFileName.json",
            {
                "000055-ps-t1=aa-vs=bb-ps=cc.dds": {"FALogDedupedFileName": "aa.dds"},
                "000010-ps-t1=dd-vs=ee-ps=ff.dds": {"FALogDedupedFileName": "dd.dds"},
                "000011-ps-t1=11-vs=22-ps=33.dds": {"FALogDedupedFileName": "11.dds"},
                "999999-ps-t1=44-vs=55-ps=66.dds": {"FALogDedupedFileName": "44.dds"},
            },
        )
        removed = self._prune()
        payload = self._read("LOD0/TrianglelistDedupedFileName.json")
        self.assertEqual(
            sorted(key[:6] for key in payload),
            ["000010", "000011", "999999"],
        )
        self.assertEqual(removed["LOD0/TrianglelistDedupedFileName.json"], 1)

    def test_mark_texture_config_only_touches_this_workspace_tabs(self):
        self._write(
            "Config/WorkPageTabs.json",
            {
                "activeTabId": "ws-tab-1",
                "tabs": [
                    {"id": "ws-tab-1", "name": "LOD0"},
                    {"id": "ws-tab-2", "name": "LOD1"},
                ],
            },
        )
        self._write(
            "MarkTextureConfig.json",
            {
                "subMesh": _mark_ref("ws-tab-1", "fe47dc61-7014-0"),
                "drawCall": "",
                "drawCallBySubMesh": {
                    _mark_ref("ws-tab-1", "fe47dc61-7014-0"): _mark_ref(
                        "ws-tab-1", "fe47dc61-7014-0", "000055"
                    ),
                    _mark_ref("ws-tab-1", "aaaa1111-100-0"): _mark_ref(
                        "ws-tab-1", "aaaa1111-100-0", "000010"
                    ),
                    _mark_ref("ws-tab-other", "fe47dc61-7014-0"): _mark_ref(
                        "ws-tab-other", "fe47dc61-7014-0", "000099"
                    ),
                },
                "faceSubMeshes": [_mark_ref("ws-tab-2", "bbbb2222-200-0")],
                "neckSubMesh": _mark_ref("ws-tab-1", "dddd4444-500-0"),
                "eyeSubMeshes": [],
            },
            base=self.game,
        )
        removed = self._prune()
        payload = self._read("MarkTextureConfig.json", base=self.game)

        self.assertEqual(payload["subMesh"], "")
        self.assertEqual(sorted(payload["drawCallBySubMesh"]), sorted([
            _mark_ref("ws-tab-1", "aaaa1111-100-0"),
            _mark_ref("ws-tab-other", "fe47dc61-7014-0"),
        ]))
        self.assertEqual(payload["faceSubMeshes"], [])
        # 未被删的部件标记保留
        self.assertEqual(payload["neckSubMesh"], _mark_ref("ws-tab-1", "dddd4444-500-0"))
        # 游戏级共享文件在工作空间之外 → 用 <游戏>/ 前缀标签
        self.assertEqual(removed["<游戏>/MarkTextureConfig.json"], 3)

    # ------------------------------------------------------------------
    # fail-safe
    # ------------------------------------------------------------------

    def test_empty_deleted_set_sweeps_cache_but_keeps_intent_rows(self):
        """没删任何文件夹时：缓存类仍按磁盘现状清（历史遗留失效条目），意图类不动。"""
        import_path = self._write(
            "Import.json",
            {"LOD0.fe47dc61-7014-0": "GPU-X", "LOD0.aaaa1111-100-0": "GPU-X"},
        )
        config_path = self._write(
            "LOD0/Config.json",
            [{"DrawIB": "fe47dc61", "Alias": "待办"}, {"DrawIB": "aaaa1111", "Alias": ""}],
        )
        tab_path = self._write(
            "Config/Tabs/ws-tab-1.json",
            {"modelRows": [{"drawIB": "fe47dc61", "aliasName": ""}], "vsRows": []},
        )
        removed = self._prune(deleted=set())

        # 缓存类：fe47dc61 的文件夹不在磁盘上 → 清掉
        self.assertEqual(self._read("Import.json"), {"LOD0.aaaa1111-100-0": "GPU-X"})
        self.assertEqual(removed["Import.json"], 1)
        # 意图类：行可能只是"还没提取"的待办 → 一行都不删
        self.assertEqual(
            self._read("LOD0/Config.json"),
            [{"DrawIB": "fe47dc61", "Alias": "待办"}, {"DrawIB": "aaaa1111", "Alias": ""}],
        )
        self.assertEqual(
            self._read("Config/Tabs/ws-tab-1.json"),
            {"modelRows": [{"drawIB": "fe47dc61", "aliasName": ""}], "vsRows": []},
        )
        self.assertNotIn("LOD0/Config.json", removed)
        self.assertNotIn("Config/Tabs/ws-tab-1.json", removed)
        self.assertTrue(import_path.exists() and config_path.exists() and tab_path.exists())

    def test_broken_json_is_left_alone(self):
        path = self._write("Import.json", {"LOD0.fe47dc61-7014-0": "GPU-X"})
        path.write_text("{ this is not json", encoding="utf-8")
        removed = self._prune()
        self.assertNotIn("Import.json", removed)
        self.assertEqual(path.read_text(encoding="utf-8"), "{ this is not json")

    def test_missing_files_are_skipped(self):
        removed = self._prune()
        self.assertEqual(removed, {})

    def test_partition_workspace_is_scanned(self):
        """分区工作空间：子网格在 `<分区>/LOD0/<bare>`，身份键不含分区名。"""
        partition_root = self.game / "分区角色"
        partition = partition_root / "角色A"
        (partition / "LOD0" / "aaaa1111-100-0").mkdir(parents=True)
        (partition / "Config.json").write_text("[]", encoding="utf-8")
        self._write(
            "LOD0/ComponentName_DrawCallIndexList.json",
            {"aaaa1111-100-0": ["000010"], "fe47dc61-7014-0": ["000055"]},
            base=partition,
        )
        self._write(
            "Import.json",
            {"LOD0.aaaa1111-100-0": "GPU-X", "LOD0.fe47dc61-7014-0": "GPU-X"},
            base=partition_root,
        )

        removed = WorkspaceConfigPruner.prune(
            str(partition_root), set(), game_folder=str(self.game), dry_run=True
        )
        # 缓存类按磁盘现状扫：分区里只存在 aaaa1111，fe47dc61 的条目要清掉
        self.assertEqual(removed["角色A/LOD0/ComponentName_DrawCallIndexList.json"], 1)
        self.assertEqual(removed["Import.json"], 1)

    def test_lod_buckets_do_not_cross_for_tab_rows(self):
        """只删了 LOD1 的部件时，LOD0 tab 的行必须原样保留（跨 LOD 不误删）。"""
        self._write(
            "Config/WorkPageTabs.json",
            {
                "activeTabId": "ws-tab-1",
                "tabs": [
                    {"id": "ws-tab-1", "name": "LOD0"},
                    {"id": "ws-tab-2", "name": "LOD1"},
                ],
            },
        )
        self._write(
            "Config/Tabs/ws-tab-1.json",
            {"modelRows": [{"drawIB": "fe47dc61", "aliasName": ""}], "vsRows": []},
        )
        self._write(
            "Config/Tabs/ws-tab-2.json",
            {"modelRows": [{"drawIB": "bbbb2222", "aliasName": ""}], "vsRows": []},
        )
        removed = self._prune(deleted={("LOD1", "bbbb2222-200-0")})

        self.assertEqual(
            self._read("Config/Tabs/ws-tab-1.json")["modelRows"],
            [{"drawIB": "fe47dc61", "aliasName": ""}],
        )
        self.assertEqual(self._read("Config/Tabs/ws-tab-2.json")["modelRows"], [])
        self.assertNotIn("Config/Tabs/ws-tab-1.json", removed)
        self.assertEqual(removed["Config/Tabs/ws-tab-2.json"], 1)


if __name__ == "__main__":
    unittest.main()
