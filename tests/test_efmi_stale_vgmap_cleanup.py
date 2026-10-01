# -*- coding: utf-8 -*-
"""陈旧 VGMap 声明清理 + B11 缺席方诊断单测（双套导出 fail-closed 链的插件侧修复）。

背景（B11 反复发作的真实形态）：
    「[EFMI双套导出] VGMap 引用槽越出声明段精确并集（B11/FC-3）: ... 引用了无人
    声明的骨骼槽位（含段间/段内空洞），数据损坏或陈旧，中止转换」

病根不在报错的那份 json —— 它在**缺席的声明方**：某个部件本次没有产出槽位映射
（骨骼池读取失败 / 声明顶点组数超 pool 容量 / 无有效 BLENDINDICES），写回侧旧实现
`if not vg_map: continue` 直接跳过，于是 json 里上一代布局的 VGMap/VGOffset/VGCount
被静默保留成「陈旧声明」；其它部件的去重借位仍指向它占过的槽位，导出侧按 A3/B10/B11
fail-closed 中止（直出只读磁盘，所以每次导出都复现）。

覆盖：
- W1（清理）：未产出映射的部件必须清空 VGMap 键族 + F1 源指纹，并事务落盘；
- W2（自愈）：清理后 `_efmi_cache_intact` 立即判缓存不完整 —— 下次 ensure 整组
  重算，不再保留「半清空」的歧义状态，也不会报「无需重新生成」；
- W3（诊断）：B11 报错文本附带缺席侧 —— 无人声明的槽位空洞 + 未计入声明段的
  json（投影未匹配 / 被清理 / 解析失败），且只收子网格形态文件；
- W4（口径）：键族清单唯一来源 `_clear_stale_vgmap_keys`（三个出口共用）。

夹具纯合成（临时目录 + 最小 dump），无 bpy 依赖；与 tests 既有 efmi 测试同 loader 风格。
"""

import importlib.util
import json
import os
import shutil
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

import numpy

REPO_ROOT = Path(__file__).resolve().parents[1]
PKG = "efmi_stale_vgmap_test_pkg"

CB_HASH = "aaaa1111"
T0_HASH_A = "bbbb2222"

GAMETYPE = "GPU_P12_N4_T8_C4_BW8_BI4_"

# 陈旧声明键族（唯一口径：EFMISkeletonMergeHelper._clear_stale_vgmap_keys）
STALE_KEYS = (
    "VGMap",
    "VGOffset",
    "VGCount",
    "VGMapAlgorithmVersion",
    "VGMapDedupEnabled",
    "EFMIVGMapSourceFingerprint",
)


def _install_package(name):
    module = types.ModuleType(name)
    module.__path__ = []
    sys.modules[name] = module
    return module


def _load_module(qualname, path):
    spec = importlib.util.spec_from_file_location(qualname, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[qualname] = module
    spec.loader.exec_module(module)
    return module


for _name in (PKG, f"{PKG}.common", f"{PKG}.utils"):
    _install_package(_name)
_load_module(f"{PKG}.utils.json_utils", REPO_ROOT / "utils" / "json_utils.py")
_efmi = _load_module(f"{PKG}.common.efmi_skeleton", REPO_ROOT / "common" / "efmi_skeleton.py")

EFMISkeletonMergeHelper = _efmi.EFMISkeletonMergeHelper
EFMIBoneMapBuilder = _efmi.EFMIBoneMapBuilder
EFMILogParser = _efmi.EFMILogParser
VG_MAP_ALGORITHM_VERSION = _efmi._VG_MAP_ALGORITHM_VERSION
LOD_LAYOUT_VERSION = _efmi._CROSS_LOD_LAYOUT_VERSION


def _write_json(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _load_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _identity_map(vg_offset, vg_count):
    """恒等映射 VGMap（local -> VGOffset+local，单源语义）。"""
    return {i: vg_offset + i for i in range(vg_count)}


def _write_component(ws: Path, lod: str, bare: str, vg_offset: int, vg_map: dict,
                     vg_count: int = None):
    """写一个含完整双套键的 EFMI 子网格 json（无 buffer，故建表用 recompute_strength=False）。"""
    if vg_count is None:
        vg_count = max(len(vg_map), 1)
    payload = {
        "GamePreset": "EFMI",
        "GPU-PreSkinning": True,
        "VGOffset": vg_offset,
        "VGCount": vg_count,
        "VGMap": {str(k): int(v) for k, v in sorted(vg_map.items())},
        "VGMapAlgorithmVersion": VG_MAP_ALGORITHM_VERSION,
        "VGMapDedupEnabled": True,
        "EFMILODLayoutVersion": LOD_LAYOUT_VERSION,
        "EFMILODReference": "LOD0" if lod != "LOD0" else lod,
        "EFMILODProjection": True,
    }
    type_dir = ws / lod / bare / "TYPE_GPU_TEST_"
    type_dir.mkdir(parents=True, exist_ok=True)
    _write_json(type_dir / f"{bare}.json", payload)
    return f"{lod}.{bare}"


def _make_dump(dump_dir, draw_index, cb_hash, t0_hash, bone_tx):
    """最小 FrameAnalysis dump（log.txt + deduped/ 下 instance cb 与骨骼池）。"""
    dump_dir = Path(dump_dir)
    (dump_dir / "deduped").mkdir(parents=True, exist_ok=True)

    cb = numpy.zeros((16, 4), dtype=numpy.float32)
    cb_uint = cb.view(numpy.uint32)
    cb_uint[5, 0] = 0
    cb_uint[5, 1] = 4
    (dump_dir / "deduped" / f"{draw_index}-vs-cb2={cb_hash}.buf").write_bytes(cb.tobytes())

    pool = numpy.zeros((800, 4), dtype=numpy.float32)
    mat = numpy.array([1, 0, 0, 0, 1, 0, 0, 0, 1, bone_tx, 0, 0], dtype=numpy.float32)
    pool[7:10] = mat.reshape(3, 4)
    (dump_dir / "deduped" / f"{draw_index}-vs-t0={t0_hash}.buf").write_bytes(pool.tobytes())

    deduped_abs = (dump_dir / "deduped").resolve()
    cb_name = f"{draw_index}-vs-cb2={cb_hash}.buf"
    t0_name = f"{draw_index}-vs-t0={t0_hash}.buf"
    log_lines = [
        f"{draw_index} VSSetConstantBuffers1(StartSlot:2,",
        f"2: resource=0x00000000 hash={cb_hash} first_constant=0 num_constants=4096",
        f"{draw_index} VSSetShaderResources(StartSlot:0,",
        f"0: view=0x00000000 resource=0x00000000 hash={t0_hash}",
        f"{draw_index} DrawIndexedInstanced(IndexCountPerInstance:100, InstanceCount:1, "
        "StartIndexLocation:0, BaseVertexLocation:0, StartInstanceLocation:0)",
        f"{draw_index} 3DMigoto Dumping Buffer {cb_name} -> {deduped_abs / cb_name}",
        f"{draw_index} 3DMigoto Dumping Buffer {t0_name} -> {deduped_abs / t0_name}",
    ]
    (dump_dir / "log.txt").write_text("\n".join(log_lines) + "\n", encoding="utf-8")
    return dump_dir


def _make_submesh(workspace, lod, bare):
    """最小子网格目录（json + Blend.buf）：2 顶点 BLENDINDICES 全 0 -> vg_count = 1。"""
    type_dir = workspace / lod / bare / ("TYPE_" + GAMETYPE)
    type_dir.mkdir(parents=True, exist_ok=True)
    _write_json(type_dir / f"{bare}.json", {
        "CategoryBufferList": [
            {"D3D11ElementList": [
                {"Category": "Blend", "SemanticName": "BLENDINDICES",
                 "Format": "R8G8B8A8_UINT", "ByteWidth": 4},
            ]},
        ],
    })
    blend = numpy.zeros((2, 4), dtype=numpy.uint8)
    (type_dir / f"{bare}-Blend.buf").write_bytes(blend.tobytes())
    return f"{lod}.{bare}"


class ClearStaleVgmapKeysUnitTests(unittest.TestCase):
    """W4：键族清单唯一来源（三个写回出口共用同一份 pop 清单）。"""

    def test_pops_exact_keyset(self):
        payload = {
            "VGMap": {"0": 0},
            "VGOffset": 0,
            "VGCount": 1,
            "VGMapAlgorithmVersion": VG_MAP_ALGORITHM_VERSION,
            "VGMapDedupEnabled": True,
            "EFMIVGMapSourceFingerprint": {"Position.buf": [1, 2]},
            "CategoryBufferList": [{"a": 1}],
            "EFMILODReference": "LOD0",
            "EFMILODProjection": True,
        }
        EFMISkeletonMergeHelper._clear_stale_vgmap_keys(payload)
        self.assertEqual(
            sorted(payload), ["CategoryBufferList", "EFMILODProjection", "EFMILODReference"]
        )

    def test_idempotent_on_clean_payload(self):
        payload = {"CategoryBufferList": []}
        EFMISkeletonMergeHelper._clear_stale_vgmap_keys(payload)
        self.assertEqual(sorted(payload), ["CategoryBufferList"])


class StaleVgmapCleanupTests(unittest.TestCase):
    """W1/W2：未产出槽位映射的部件不再静默保留旧代际声明。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="efmi_stale_")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.dump = _make_dump(Path(self.tmp) / "dump", "000100", CB_HASH, T0_HASH_A, 2.5)
        self.ws = Path(self.tmp) / "ws"
        (self.ws / "LOD0").mkdir(parents=True, exist_ok=True)

        self.u_clean = _make_submesh(self.ws, "LOD0", "aaaabbbb-100-0")
        self.u_stale = _make_submesh(self.ws, "LOD0", "ccccdddd-200-0")
        _write_json(self.ws / "LOD0" / "ComponentName_DrawCallIndexList.json", {
            "aaaabbbb-100-0": ["000100"],
            "ccccdddd-200-0": ["000100"],
        })

        # 旧代际残留：上一代布局的 VGMap 键族（旧实现在「本次未产出映射」时原样留着）
        stale_path = self._json_path("ccccdddd-200-0")
        payload = _load_json(stale_path)
        payload.update({
            "VGOffset": 7,
            "VGCount": 2,
            "VGMap": {"0": 7, "1": 8},
            "VGMapAlgorithmVersion": VG_MAP_ALGORITHM_VERSION,
            "VGMapDedupEnabled": True,
        })
        _write_json(stale_path, payload)

    def _json_path(self, bare):
        return next((self.ws / "LOD0" / bare).glob("TYPE_*/*.json"))

    def _run(self, force=True):
        """走真实写回链；build_vg_maps 打桩成「本次未产出任何槽位映射」。"""
        parser = EFMILogParser(str(self.dump / "log.txt"))
        with mock.patch.object(EFMIBoneMapBuilder, "build_vg_maps",
                               return_value=({}, {})):
            return EFMISkeletonMergeHelper._ensure_skeleton_data_for_group(
                workspace_root=str(self.ws),
                unique_str_list=[self.u_clean, self.u_stale],
                parser=parser,
                force=force,
            )

    def test_stale_keys_cleared_and_persisted(self):
        written, _skipped, message = self._run()
        self.assertEqual(written, 0)

        payload = _load_json(self._json_path("ccccdddd-200-0"))
        for key in STALE_KEYS:
            self.assertNotIn(
                key, payload,
                f"{key} 必须在「本次未产出槽位映射」时被清空（否则成为陈旧声明）",
            )
        # 非 VGMap 内容不得被动到（json 事务是原地清键，不是整体重写）
        self.assertIn("CategoryBufferList", payload)

        self.assertIn("已清理旧代际 VGMap 键", message)
        self.assertIn("2 个部件本次未产出槽位映射", message)

    def test_cleared_target_is_cache_incomplete_and_reexported(self):
        self._run()
        path = self._json_path("ccccdddd-200-0")
        payload = _load_json(path)
        self.assertFalse(
            EFMISkeletonMergeHelper._efmi_cache_intact(payload, str(path), self.u_stale),
            "清空键族后必须立即判缓存不完整（下次 ensure 整组重算，自愈）",
        )
        _w, _s, message2 = self._run(force=False)
        self.assertNotIn("无需重新生成", message2)

    def test_non_target_json_untouched(self):
        """清理只发生在未产出映射的目标上：无关 json 与另一部件的键不受影响。"""
        _write_json(self.ws / "Import.json", {"GamePreset": "EFMI"})
        clean_path = self._json_path("aaaabbbb-100-0")
        before = _load_json(clean_path)
        self._run()
        self.assertEqual(_load_json(clean_path), before)
        self.assertEqual(_load_json(self.ws / "Import.json"), {"GamePreset": "EFMI"})


class B11GapDiagnosticsTests(unittest.TestCase):
    """W3：B11 报错文本点名缺席侧（空洞 + 未计入声明段的 json）。"""

    def setUp(self):
        EFMIBoneMapBuilder._dualset_table_cache.clear()
        self.tmp = tempfile.mkdtemp(prefix="b11_diag_")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.ws = Path(self.tmp)

    def _build(self):
        with self.assertRaises(RuntimeError) as ctx:
            EFMIBoneMapBuilder.build_dualset_export_table(
                str(self.ws), recompute_strength=False
            )
        return str(ctx.exception)

    def _corrupt_parts(self):
        """[0,5) + [10,15) 声明 + 一份引用无人声明槽位 7 的损坏部件（段 [20,21)）。"""
        _write_component(self.ws, "LOD0", "aaaa0000-0-0", 0, _identity_map(0, 5))
        _write_component(self.ws, "LOD0", "bbbb2222-0-0", 10, _identity_map(10, 5))
        _write_component(self.ws, "LOD0", "cccc3333-0-0", 20, {0: 7}, vg_count=1)

    def test_holes_listed(self):
        self._corrupt_parts()
        message = self._build()
        self.assertIn("B11", message)
        # 声明段 [0,5)/[10,15)/[20,21) -> 空洞 [5,10)、[15,20)
        self.assertIn("无人声明的槽位空洞 [5,10)、[15,20)（共 2 段", message)
        # 没有缺席 json 时不编造清单（保持旧文本形态）
        self.assertNotIn("缺席方通常就在上面这些 json 里", message)

    def test_no_diagnostics_for_isolated_violation(self):
        """单部件 [0,1) 引用槽 5：池外引用，无空洞、无缺席 json -> 无诊断后缀。"""
        _write_component(self.ws, "LOD0", "aaaa1000-0-0", 0, {0: 5}, vg_count=1)
        message = self._build()
        self.assertIn("B11", message)
        self.assertNotIn("无人声明的槽位空洞", message)
        self.assertNotIn("缺席方通常就在上面这些 json 里", message)

    def test_absent_declarer_and_unreadable_listed(self):
        self._corrupt_parts()

        # 缺席方 1：投影未匹配跳过（无 VGMap 键、带跳过标记）
        skipped_dir = self.ws / "LOD0" / "dddd4444-0-0" / "TYPE_GPU_TEST_"
        skipped_dir.mkdir(parents=True, exist_ok=True)
        _write_json(skipped_dir / "dddd4444-0-0.json", {
            "GamePreset": "EFMI", "EFMILODProjectionSkipped": True,
        })
        # 缺席方 2：json 解析失败（半截写入 / 被外部工具截断）
        broken_dir = self.ws / "LOD0" / "eeee5555-0-0" / "TYPE_GPU_TEST_"
        broken_dir.mkdir(parents=True, exist_ok=True)
        (broken_dir / "eeee5555-0-0.json").write_text('{ "VGMap": {', encoding="utf-8")
        # 无关 json：不得被报成缺席方
        _write_json(self.ws / "Import.json", {"whatever": True})
        _write_json(self.ws / "Config" / "Tabs" / "ws-tab-1.json",
                    {"frameAnalysisFolderPath": "x"})

        message = self._build()
        self.assertIn("无 VGMap 键未计入声明段 1 个", message)
        self.assertIn("LOD0.dddd4444-0-0[跨 LOD 投影未匹配，已裁决跳过导入]", message)
        self.assertIn("json 解析失败未计入声明段 1 个", message)
        self.assertIn(
            os.path.join("LOD0", "eeee5555-0-0", "TYPE_GPU_TEST_", "eeee5555-0-0.json"),
            message,
        )
        self.assertIn("清除骨骼合并VGMap缓存", message)
        self.assertNotIn("Import.json", message)
        self.assertNotIn("ws-tab-1.json", message)

    def test_partially_cleared_json_reason(self):
        """半清空的 json（有 VGMapAlgorithmVersion 但无 VGMap）单独归因。"""
        self._corrupt_parts()
        half_dir = self.ws / "LOD0" / "ffff6666-0-0" / "TYPE_GPU_TEST_"
        half_dir.mkdir(parents=True, exist_ok=True)
        _write_json(half_dir / "ffff6666-0-0.json", {
            "GamePreset": "EFMI",
            "VGMapAlgorithmVersion": VG_MAP_ALGORITHM_VERSION,
        })
        message = self._build()
        self.assertIn("LOD0.ffff6666-0-0[VGMap 键族被部分清空", message)


if __name__ == "__main__":
    unittest.main(verbosity=2)
