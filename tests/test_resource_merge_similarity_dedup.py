# -*- coding: utf-8 -*-
"""「资源合并」节点相似度去重模式的集成测试。

覆盖：
- 默认关闭：像素微变的 DDS 不合并（维持 MD5 / 像素级行为）
- 开启且阈值 90：像素微变（相似度约 98.8%）的 DDS 合并 -> 引用改写 + 删除重复文件
- 阈值 99（更严格）：像素微变不合并；阈值 50：明显不同的图（相似度约 53%）也会合并
- 尺寸不同不比较
- BC6H 等无法解码的格式优雅跳过、不误删
- 阈值 clamp 到 50~100
- report_stage 用桩验证：调用文本为「资源合并：相似度去重 done/total」，桩抛异常也不影响流程
- 与像素级去重同时开启：像素映射被改指到相似度合并后的主文件
"""

import importlib.util
import os
import struct
import sys
import tempfile
import types
import unittest
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[1]
PKG = "_resource_merge_similarity_test_pkg"


def _install_module(name, **attrs):
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


def _load_real(qualname, relpath):
    spec = importlib.util.spec_from_file_location(qualname, REPO_ROOT / relpath)
    module = importlib.util.module_from_spec(spec)
    sys.modules[qualname] = module
    spec.loader.exec_module(module)
    return module


for pkg_name in (PKG, f"{PKG}.blueprint", f"{PKG}.common"):
    _pkg = _install_module(pkg_name)
    _pkg.__path__ = []

# 与既有资源合并测试一致：轻量 fake bpy 只桩 BoolProperty（模块内有 IntProperty 回退）
_install_module(
    "bpy",
    types=types.SimpleNamespace(),
    props=types.SimpleNamespace(BoolProperty=lambda **_k: False),
)

dds_pixel = _load_real(f"{PKG}.common.dds_pixel", "common/dds_pixel.py")
sys.modules[f"{PKG}.common"].dds_pixel = dds_pixel


class _StubBase:
    @classmethod
    def split_anim_driver_block_content(cls, content):
        return "", str(content)

    @classmethod
    def split_auto_appended_tail_content(cls, content):
        return str(content), ""

    def _create_cumulative_backup(self, ini_file, mod_export_path):
        return None


_install_module(f"{PKG}.blueprint.node_postprocess_base", SSMTNode_PostProcess_Base=_StubBase)

_node_mod = _load_real(
    f"{PKG}.blueprint.node_postprocess_resource_merge",
    "blueprint/node_postprocess_resource_merge.py",
)
ResourceMergeNode = _node_mod.SSMTNode_PostProcess_ResourceMerge


# ---------------------------------------------------------------------------
# DDS 构造工具（沿用 test_resource_merge_pixel_dedup.py 的构造方式）
# ---------------------------------------------------------------------------
def _dds_header(fourcc, width, height):
    header = bytearray(128)
    header[0:4] = b"DDS "
    struct.pack_into("<I", header, 4, 124)
    struct.pack_into("<I", header, 8, 0x1007)
    struct.pack_into("<I", header, 12, height)
    struct.pack_into("<I", header, 16, width)
    struct.pack_into("<I", header, 76, 32)
    struct.pack_into("<I", header, 80, 0x4)  # FOURCC
    header[84:88] = fourcc
    struct.pack_into("<I", header, 108, 0x1000)
    return bytes(header)


def _bc1_block(c0, c1, idx_pattern):
    idx = 0
    for i in range(16):
        idx |= idx_pattern[i] << (2 * i)
    return struct.pack("<HHI", c0, c1, idx)


def _dxt1_dds(width, height, blocks):
    return _dds_header(b"DXT1", width, height) + b"".join(blocks)


def _dxt5_dds(width, height, alpha_block, color_blocks):
    return _dds_header(b"DXT5", width, height) + alpha_block + b"".join(color_blocks)


# 灰 170（0xFFFF -> 索引2 = 170,170,170）
GREY = _bc1_block(0xFFFF, 0x0000, [2] * 16)
# 微变灰（0xF7DE -> 索引2 = 164,170,164），灰度 167 vs 170 -> 相似度约 98.8%
NEAR_GREY = _bc1_block(0xF7DE, 0x0000, [2] * 16)
# 红（0xF800 -> 索引2 = 170,0,0），灰度 51 vs 170 -> 相似度约 53.3%
RED = _bc1_block(0xF800, 0x0000, [2] * 16)

# DXT5 opaque alpha：a0=255 > a1=0 -> 索引 0 -> 255（全不透明）
OPAQUE_ALPHA = bytes([255, 0]) + bytes(6)


def _simple_ini(resources):
    return "".join(f"[{section}]\nfilename = {filename}\n\n" for section, filename in resources)


class SimilarityDedupNodeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="similarity_dedup_")
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, True))
        self.textures = Path(self.tmp) / "Textures"
        self.textures.mkdir()
        self.ini = Path(self.tmp) / "Workspace.ini"
        self.addCleanup(self._pop_progress_stub)

    def _pop_progress_stub(self):
        package = sys.modules[f"{PKG}.blueprint"]
        if hasattr(package, "export_progress"):
            delattr(package, "export_progress")
        sys.modules.pop(f"{PKG}.blueprint.export_progress", None)

    def _install_progress_stub(self, reporter):
        module = types.ModuleType(f"{PKG}.blueprint.export_progress")
        module.report_stage = reporter
        sys.modules[f"{PKG}.blueprint.export_progress"] = module
        sys.modules[f"{PKG}.blueprint"].export_progress = module
        return module

    def _write(self, name, data):
        path = self.textures / name
        path.write_bytes(data)
        return path

    def _run(self, node, resources):
        self.ini.write_text(_simple_ini(resources), encoding="utf-8")
        node.process_ini_file(str(self.ini), self.tmp)
        return self.ini.read_text(encoding="utf-8")

    def _node(self, enabled=True, threshold=90):
        node = ResourceMergeNode()
        node.use_similarity_dedup = enabled
        node.similarity_threshold = threshold
        return node

    # -- 开关 / 阈值 -----------------------------------------------------
    def test_default_off_keeps_pixel_variant(self):
        """默认关闭相似度去重：像素微变的两张图不合并。"""
        a = self._write("a.dds", _dxt1_dds(4, 4, [GREY]))
        b = self._write("b.dds", _dxt1_dds(4, 4, [NEAR_GREY]))
        node = ResourceMergeNode()
        node.use_similarity_dedup = False
        content = self._run(node, [("ResourceA", "Textures/a.dds"),
                                   ("ResourceB", "Textures/b.dds")])
        self.assertTrue(a.exists())
        self.assertTrue(b.exists())
        self.assertIn("Textures/a.dds", content)
        self.assertIn("Textures/b.dds", content)

    def test_threshold_90_merges_pixel_variant(self):
        """阈值 90：相似度约 98.8% 的像素微变图合并删除。"""
        a = self._write("a.dds", _dxt1_dds(4, 4, [GREY]))
        b = self._write("b.dds", _dxt1_dds(4, 4, [NEAR_GREY]))
        content = self._run(self._node(threshold=90),
                            [("ResourceA", "Textures/a.dds"),
                             ("ResourceB", "Textures/b.dds")])
        self.assertTrue(a.exists())
        self.assertFalse(b.exists(), "相似度达标的重复文件应被删除")
        self.assertIn("Textures/a.dds", content)
        self.assertNotIn("Textures/b.dds", content)
        self.assertEqual(content.count("Textures/a.dds"), 2)

    def test_stricter_threshold_keeps_pixel_variant(self):
        """阈值 99：相似度约 98.8% 的微变图不再算重复。"""
        a = self._write("a.dds", _dxt1_dds(4, 4, [GREY]))
        b = self._write("b.dds", _dxt1_dds(4, 4, [NEAR_GREY]))
        content = self._run(self._node(threshold=99),
                            [("ResourceA", "Textures/a.dds"),
                             ("ResourceB", "Textures/b.dds")])
        self.assertTrue(a.exists())
        self.assertTrue(b.exists())
        self.assertIn("Textures/b.dds", content)

    def test_low_threshold_merges_different_colors(self):
        """阈值 50：相似度约 53% 的明显不同图也判为重复（阈值确实参与判定）。"""
        a = self._write("a.dds", _dxt1_dds(4, 4, [GREY]))
        b = self._write("b.dds", _dxt1_dds(4, 4, [RED]))
        content = self._run(self._node(threshold=50),
                            [("ResourceA", "Textures/a.dds"),
                             ("ResourceB", "Textures/b.dds")])
        self.assertTrue(a.exists())
        self.assertFalse(b.exists())
        self.assertNotIn("Textures/b.dds", content)

    def test_default_threshold_keeps_different_colors(self):
        """默认阈值 90：相似度约 53% 的图不合并。"""
        a = self._write("a.dds", _dxt1_dds(4, 4, [GREY]))
        b = self._write("b.dds", _dxt1_dds(4, 4, [RED]))
        content = self._run(self._node(threshold=90),
                            [("ResourceA", "Textures/a.dds"),
                             ("ResourceB", "Textures/b.dds")])
        self.assertTrue(a.exists())
        self.assertTrue(b.exists())
        self.assertIn("Textures/b.dds", content)

    def test_threshold_is_clamped(self):
        """阈值属性被绕过时也要钳制到 50~100，非法值回退默认 90。"""
        node = self._node()
        node.similarity_threshold = -20
        self.assertEqual(node._similarity_threshold_value(), 50)
        node.similarity_threshold = 250
        self.assertEqual(node._similarity_threshold_value(), 100)
        node.similarity_threshold = "abc"
        self.assertEqual(node._similarity_threshold_value(), 90)
        node.similarity_threshold = None
        self.assertEqual(node._similarity_threshold_value(), 90)

    # -- 跳过 / 分组 -----------------------------------------------------
    def test_different_dimensions_not_compared(self):
        """尺寸不同：不进行相似度比较。"""
        a = self._write("a.dds", _dxt1_dds(4, 4, [GREY]))
        b = self._write("b.dds", _dxt1_dds(8, 4, [GREY, GREY]))
        content = self._run(self._node(threshold=50),
                            [("ResourceA", "Textures/a.dds"),
                             ("ResourceB", "Textures/b.dds")])
        self.assertTrue(a.exists())
        self.assertTrue(b.exists())
        self.assertIn("Textures/b.dds", content)

    def test_bc6h_unreadable_is_skipped(self):
        """BC6H 无法解码：跳过并保留文件，不崩溃、不误删。"""
        a = self._write("a.dds", _dxt1_dds(4, 4, [GREY]))
        b = self._write("b.dds", _dds_header(b"BC6H", 4, 4) + b"\x00" * 16)
        content = self._run(self._node(threshold=50),
                            [("ResourceA", "Textures/a.dds"),
                             ("ResourceB", "Textures/b.dds")])
        self.assertTrue(a.exists())
        self.assertTrue(b.exists())
        self.assertIn("Textures/b.dds", content)

    # -- 进度信息 --------------------------------------------------------
    def test_report_stage_receives_progress_text(self):
        """有进度模块时按批上报「资源合并：相似度去重 done/total」，只更新文本。"""
        calls = []
        self._install_progress_stub(lambda stage, done=None, total=None: calls.append(stage))
        self._write("a.dds", _dxt1_dds(4, 4, [GREY]))
        self._write("b.dds", _dxt1_dds(4, 4, [NEAR_GREY]))
        self._run(self._node(threshold=90),
                  [("ResourceA", "Textures/a.dds"),
                   ("ResourceB", "Textures/b.dds")])
        self.assertTrue(calls, "应至少上报一次相似度去重进度")
        for stage in calls:
            self.assertTrue(stage.startswith("资源合并：相似度去重 "), stage)
            self.assertRegex(stage, r"^\S+ \d+/\d+$")
        self.assertEqual(calls[-1], "资源合并：相似度去重 2/2")

    def test_report_stage_exception_is_swallowed(self):
        """进度桩抛异常时流程照旧（同步导出没有进度文件时无副作用）。"""
        def _raise(*_args, **_kwargs):
            raise RuntimeError("no progress file")

        self._install_progress_stub(_raise)
        a = self._write("a.dds", _dxt1_dds(4, 4, [GREY]))
        b = self._write("b.dds", _dxt1_dds(4, 4, [NEAR_GREY]))
        content = self._run(self._node(threshold=90),
                            [("ResourceA", "Textures/a.dds"),
                             ("ResourceB", "Textures/b.dds")])
        self.assertTrue(a.exists())
        self.assertFalse(b.exists())
        self.assertEqual(content.count("Textures/a.dds"), 2)

    # -- 与像素级去重组合 ------------------------------------------------
    def test_combined_pixel_and_similarity_rewires_primary_mapping(self):
        """像素级把 c 合并到 b，相似度再把 b 合并到 a：c 的映射要改指到 a。"""
        a = self._write("a.dds", _dxt1_dds(4, 4, [GREY]))
        b = self._write("b.dds", _dxt1_dds(4, 4, [NEAR_GREY]))
        c = self._write("c.dds", _dxt5_dds(4, 4, OPAQUE_ALPHA, [NEAR_GREY]))
        node = self._node(threshold=90)
        node.use_pixel_dedup = True
        content = self._run(node, [("ResourceA", "Textures/a.dds"),
                                   ("ResourceB", "Textures/b.dds"),
                                   ("ResourceC", "Textures/c.dds")])
        self.assertTrue(a.exists())
        self.assertFalse(b.exists(), "b 应被相似度去重删除")
        self.assertFalse(c.exists(), "c 应被像素级去重删除")
        self.assertEqual(content.count("Textures/a.dds"), 3)
        self.assertNotIn("Textures/b.dds", content)
        self.assertNotIn("Textures/c.dds", content)

    # -- UI --------------------------------------------------------------
    def test_draw_buttons_shows_slider_only_when_enabled(self):
        """勾选相似度去重后才显示百分比滑条与说明。"""
        calls = []

        class _Layout:
            def label(self, **kwargs):
                calls.append(("label", kwargs.get("text", "")))

            def prop(self, owner, name, **kwargs):
                calls.append(("prop", name, kwargs))

            def separator(self):
                calls.append(("separator",))

        node = self._node(enabled=False)
        node.draw_buttons(None, _Layout())
        prop_names = [c[1] for c in calls if c[0] == "prop"]
        self.assertIn("use_similarity_dedup", prop_names)
        self.assertNotIn("similarity_threshold", prop_names)
        self.assertNotIn("相似度 ≥ 阈值即视为同一张图并合并",
                         [c[1] for c in calls if c[0] == "label"])

        calls.clear()
        node.use_similarity_dedup = True
        node.draw_buttons(None, _Layout())
        slider_calls = [c for c in calls if c[0] == "prop" and c[1] == "similarity_threshold"]
        self.assertEqual(len(slider_calls), 1)
        self.assertTrue(slider_calls[0][2].get("slider"))
        self.assertIn("相似度 ≥ 阈值即视为同一张图并合并",
                      [c[1] for c in calls if c[0] == "label"])

    # -- 特征本身 --------------------------------------------------------
    def test_feature_shape_and_uniform_grayscale(self):
        """32×32 特征：均匀灰图所有块均值相同，形状为 32×32。"""
        pixels = np.zeros((64, 64, 4), dtype=np.uint8)
        pixels[:, :, :3] = 170
        pixels[:, :, 3] = 255
        feature = ResourceMergeNode._similarity_feature(pixels)
        self.assertEqual(feature.shape, (32, 32))
        self.assertTrue(np.allclose(feature, 170.0))

    def test_feature_handles_small_image(self):
        """小于 32 像素的图按实际尺寸缩略（4×4 -> 4×4），不报错。"""
        pixels = np.zeros((4, 4, 4), dtype=np.uint8)
        pixels[:, :, 0] = 255
        pixels[:, :, 3] = 255
        feature = ResourceMergeNode._similarity_feature(pixels)
        self.assertEqual(feature.shape, (4, 4))
        self.assertTrue(np.allclose(feature, 76.0))  # 255*77>>8


if __name__ == "__main__":
    unittest.main()
