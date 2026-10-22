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
import time
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


# 2048² 法线图构造：DXT1 实心块（每块 16 像素索引全 0 -> 解码为该块 c0 颜色）
# (128,128,255) 是平坦法线底色；下面两块灰度与底色几乎相同，但 RGB 方向完全相反。
NORMAL_FLAT = _bc1_block(0x8C1F, 0x0000, [0] * 16)  # 解码 (140,130,255)，灰度 147
NORMAL_TILT = _bc1_block(0x847E, 0x0000, [0] * 16)  # 解码 (132,142,247)，灰度 150
NORMAL_DIR_A = 0xB33F  # (181,101,255)，灰度 142
NORMAL_DIR_B = 0x4CDF  # (74,154,255)，灰度 141~142


def _bc1_solid_blocks(block_rows, block_cols, c0_grid, c1_grid=None):
    """按块生成 DXT1 数据：每块 16 像素都用索引 0（即该块 c0 的颜色）。"""
    c0 = np.asarray(c0_grid, dtype=np.uint16)
    c1 = np.zeros_like(c0) if c1_grid is None else np.asarray(c1_grid, dtype=np.uint16)
    raw = np.zeros((block_rows, block_cols, 8), dtype=np.uint8)
    raw[:, :, 0] = (c0 & 0xFF).astype(np.uint8)
    raw[:, :, 1] = (c0 >> 8).astype(np.uint8)
    raw[:, :, 2] = (c1 & 0xFF).astype(np.uint8)
    raw[:, :, 3] = (c1 >> 8).astype(np.uint8)
    return raw.tobytes()


def _normal_map_2048(patch_color, patch_blocks=154, c1_value=0):
    """2048×2048 法线图：左侧 patch_blocks 列块（约 30% 面积）换成 patch_color。"""
    blocks = np.full((512, 512), NORMAL_BASE_C0, dtype=np.uint16)
    blocks[:, :patch_blocks] = np.uint16(patch_color)
    c1 = np.full((512, 512), c1_value, dtype=np.uint16)
    return _dds_header(b"DXT1", 2048, 2048) + _bc1_solid_blocks(512, 512, blocks, c1)


NORMAL_BASE_C0 = 0x841F  # 解码 (132,130,255)，法线图平坦底色

# 灰 170（0xFFFF -> 索引2 = 170,170,170）
GREY = _bc1_block(0xFFFF, 0x0000, [2] * 16)
# 微变灰（0xF7DE -> 索引2 = 164,170,164），灰度 167 vs 170 -> 相似度约 98.8%
NEAR_GREY = _bc1_block(0xF7DE, 0x0000, [2] * 16)
# 红（0xF800 -> 索引2 = 170,0,0），灰度 51 vs 170 -> 相似度约 53.3%
RED = _bc1_block(0xF800, 0x0000, [2] * 16)
# 蓝 (0,0,255) / 绿 (0,255,0)：用于"形状相同但颜色不同"的色块维度测试
BLUE = _bc1_block(0x001F, 0x0000, [0] * 16)
GREEN = _bc1_block(0x07E0, 0x0000, [0] * 16)

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

    def test_low_threshold_merges_patch_variant(self):
        """阈值 50：局部色块差异（综合相似度约 75%）会被判为重复，证明阈值确实参与判定。"""
        # 8×8 = 2×2 个 DXT1 块：A 全灰，B 右上角一块是红色（25% 面积）
        a = self._write("a.dds", _dxt1_dds(8, 8, [GREY, GREY, GREY, GREY]))
        b = self._write("b.dds", _dxt1_dds(8, 8, [GREY, RED, GREY, GREY]))
        self._run(self._node(threshold=50),
                  [("ResourceA", "Textures/a.dds"),
                   ("ResourceB", "Textures/b.dds")])
        self.assertTrue(a.exists())
        self.assertFalse(b.exists())

    def test_stricter_threshold_keeps_patch_variant(self):
        """阈值 80：同一对局部色块差异图（约 75%）不再合并。"""
        a = self._write("a.dds", _dxt1_dds(8, 8, [GREY, GREY, GREY, GREY]))
        b = self._write("b.dds", _dxt1_dds(8, 8, [GREY, RED, GREY, GREY]))
        content = self._run(self._node(threshold=80),
                            [("ResourceA", "Textures/a.dds"),
                             ("ResourceB", "Textures/b.dds")])
        self.assertTrue(a.exists())
        self.assertTrue(b.exists())
        self.assertIn("Textures/b.dds", content)

    def test_different_colors_not_merged_even_at_low_threshold(self):
        """颜色明显不同（灰 vs 红）：即使阈值降到 50 也不合并（三维取 min 更保守）。"""
        a = self._write("a.dds", _dxt1_dds(4, 4, [GREY]))
        b = self._write("b.dds", _dxt1_dds(4, 4, [RED]))
        content = self._run(self._node(threshold=50),
                            [("ResourceA", "Textures/a.dds"),
                             ("ResourceB", "Textures/b.dds")])
        self.assertTrue(a.exists())
        self.assertTrue(b.exists())
        self.assertIn("Textures/b.dds", content)

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


class SimilarityFeatureDimensionTests(unittest.TestCase):
    """三维相似度（色块 / 轮廓 / 色差）与法线贴图误判回归。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="similarity_dims_")
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

    def _scores(self, data_a, data_b):
        pixels_a = dds_pixel.decode_dds_rgba8(data_a)
        pixels_b = dds_pixel.decode_dds_rgba8(data_b)
        self.assertIsNotNone(pixels_a)
        self.assertIsNotNone(pixels_b)
        scores = ResourceMergeNode._similarity_scores(
            ResourceMergeNode._similarity_features(pixels_a),
            ResourceMergeNode._similarity_features(pixels_b),
        )
        self.assertIsNotNone(scores)
        return scores

    # -- 法线贴图误判回归（2048²，用户实测 98.03% 误合并的场景）------------
    def test_normal_map_2048_different_directions_not_merged(self):
        """两张平坦底色相同、细节法线方向相反的 2048 法线图，在 98% 阈值下不合并。"""
        data_a = _normal_map_2048(NORMAL_DIR_A)
        data_b = _normal_map_2048(NORMAL_DIR_B)
        a = self._write("NormalMap_aaa.dds", data_a)
        b = self._write("NormalMap_bbb.dds", data_b)
        content = self._run(self._node(threshold=98),
                            [("ResourceA", "Textures/NormalMap_aaa.dds"),
                             ("ResourceB", "Textures/NormalMap_bbb.dds")])
        self.assertTrue(a.exists())
        self.assertTrue(b.exists(), "内容不同的法线图不得被相似度去重删除")
        self.assertIn("Textures/NormalMap_bbb.dds", content)

        # 旧算法（32×32 灰度平均差）确实会把这两张图判成 ≥98% —— 误删根因
        pixels_a = dds_pixel.decode_dds_rgba8(data_a)
        pixels_b = dds_pixel.decode_dds_rgba8(data_b)
        gray_a = ResourceMergeNode._similarity_feature(pixels_a)
        gray_b = ResourceMergeNode._similarity_feature(pixels_b)
        legacy_similarity = 1.0 - float(np.mean(np.abs(gray_a - gray_b))) / 255.0
        self.assertGreaterEqual(legacy_similarity, 0.98,
                                f"旧灰度口径应误判为相似（实测 {legacy_similarity*100:.2f}%）")

        block, contour, color = self._scores(data_a, data_b)
        self.assertGreaterEqual(contour, 0.98, "灰度轮廓几乎一致（轮廓维度看不出问题）")
        self.assertLess(color, 0.98, "RGB 色差维度必须单独否决")
        self.assertLess(min(block, contour, color), 0.98)

    def test_identical_normal_maps_merge_2048(self):
        """内容完全相同的 2048 法线图（压缩字节不同、解码像素相同）-> 合并。"""
        data_a = _normal_map_2048(NORMAL_DIR_A)
        data_b = _normal_map_2048(NORMAL_DIR_A, c1_value=1)  # 字节不同，像素相同
        self.assertNotEqual(data_a, data_b)
        a = self._write("NormalMap_same_a.dds", data_a)
        b = self._write("NormalMap_same_b.dds", data_b)
        content = self._run(self._node(threshold=98),
                            [("ResourceA", "Textures/NormalMap_same_a.dds"),
                             ("ResourceB", "Textures/NormalMap_same_b.dds")])
        self.assertTrue(a.exists())
        self.assertFalse(b.exists(), "内容相同的法线图应被合并删除")
        self.assertNotIn("Textures/NormalMap_same_b.dds", content)
        self.assertEqual(content.count("Textures/NormalMap_same_a.dds"), 2)

        block, contour, color = self._scores(data_a, data_b)
        for name, value in (("色块", block), ("轮廓", contour), ("色差", color)):
            self.assertAlmostEqual(value, 1.0, places=6, msg=f"{name}相似度应为 1.0")

    # -- 三个维度各自能否单独否决 -----------------------------------------
    def test_color_block_difference_not_merged(self):
        """形状/位置相同、只换配色（灰+红 -> 蓝+绿）：色块维度否决，不合并。"""
        data_a = _dxt1_dds(8, 8, [GREY, RED, RED, GREY])
        data_b = _dxt1_dds(8, 8, [BLUE, GREEN, GREEN, BLUE])
        a = self._write("palette_a.dds", data_a)
        b = self._write("palette_b.dds", data_b)
        content = self._run(self._node(threshold=90),
                            [("ResourceA", "Textures/palette_a.dds"),
                             ("ResourceB", "Textures/palette_b.dds")])
        self.assertTrue(a.exists())
        self.assertTrue(b.exists())
        self.assertIn("Textures/palette_b.dds", content)
        block, _contour, _color = self._scores(data_a, data_b)
        self.assertLess(block, 0.9, "颜色分布明显不同，色块相似度必须低")

    def test_contour_difference_not_merged(self):
        """同色不同结构（竖条纹 -> 横条纹）：轮廓维度下降，不合并。"""
        data_a = _dxt1_dds(8, 8, [GREY, RED, GREY, RED])   # 竖条纹
        data_b = _dxt1_dds(8, 8, [GREY, GREY, RED, RED])   # 横条纹
        a = self._write("stripe_a.dds", data_a)
        b = self._write("stripe_b.dds", data_b)
        content = self._run(self._node(threshold=90),
                            [("ResourceA", "Textures/stripe_a.dds"),
                             ("ResourceB", "Textures/stripe_b.dds")])
        self.assertTrue(a.exists())
        self.assertTrue(b.exists())
        self.assertIn("Textures/stripe_b.dds", content)
        block, contour, _color = self._scores(data_a, data_b)
        self.assertAlmostEqual(block, 1.0, places=6, msg="两张图颜色分布相同")
        self.assertLess(contour, 0.95, "边界方向不同，轮廓相似度必须下降")

    def test_color_diff_dimension_alone_not_merged(self):
        """灰度几乎相同、仅 RGB 轻微不同的图：色差维度单独否决，98% 不合并、90% 合并。"""
        data_a = _dxt1_dds(4, 4, [NORMAL_FLAT])  # 解码 (140,130,255)
        data_b = _dxt1_dds(4, 4, [NORMAL_TILT])  # 解码 (156,121,255)
        a = self._write("rgb_a.dds", data_a)
        b = self._write("rgb_b.dds", data_b)

        # 灰度几乎相同（差 3，旧口径约 98.8%，仍 ≥98% -> 会被误合并）
        gray_a = ResourceMergeNode._similarity_feature(dds_pixel.decode_dds_rgba8(data_a))
        gray_b = ResourceMergeNode._similarity_feature(dds_pixel.decode_dds_rgba8(data_b))
        legacy_similarity = 1.0 - float(np.mean(np.abs(gray_a - gray_b))) / 255.0
        self.assertGreaterEqual(legacy_similarity, 0.98,
                                "两张图灰度几乎相同，旧口径会判为相似")

        content = self._run(self._node(threshold=98),
                            [("ResourceA", "Textures/rgb_a.dds"),
                             ("ResourceB", "Textures/rgb_b.dds")])
        self.assertTrue(a.exists())
        self.assertTrue(b.exists(), "RGB 色差约 3.7%，98% 阈值下不得合并")
        self.assertIn("Textures/rgb_b.dds", content)
        block, contour, color = self._scores(data_a, data_b)
        self.assertGreaterEqual(block, 0.99, "颜色分布落在同一 bin，色块维度看不出问题")
        self.assertGreaterEqual(contour, 0.99, "两张都是平坦图，轮廓维度看不出问题")
        self.assertLess(color, 0.98, "色差维度必须低于 98%")

        # 同一对图在 90% 阈值下仍可合并（色差约 96.3%，阈值语义不变）
        self._run(self._node(threshold=90),
                  [("ResourceA", "Textures/rgb_a.dds"),
                   ("ResourceB", "Textures/rgb_b.dds")])
        self.assertTrue(a.exists())
        self.assertFalse(b.exists())
        self.assertEqual(self.ini.read_text(encoding="utf-8").count("Textures/rgb_a.dds"), 2)

    # -- 性能 ------------------------------------------------------------
    def test_2048_feature_extraction_performance(self):
        """2048×2048 单张：解码 + 三维特征提取 < 1.5 秒。"""
        data = _normal_map_2048(NORMAL_DIR_A)
        start = time.perf_counter()
        pixels = dds_pixel.decode_dds_rgba8(data)
        decode_seconds = time.perf_counter() - start
        self.assertIsNotNone(pixels)
        start = time.perf_counter()
        features = ResourceMergeNode._similarity_features(pixels)
        feature_seconds = time.perf_counter() - start
        self.assertIsNotNone(features)
        self.assertIn("thumb", features)
        print(
            f"[perf] 2048x2048 DDS: 解码 {decode_seconds * 1000:.1f} ms, "
            f"特征提取 {feature_seconds * 1000:.1f} ms, "
            f"合计 {(decode_seconds + feature_seconds) * 1000:.1f} ms"
        )
        self.assertLess(feature_seconds, 1.5)
        self.assertLess(decode_seconds + feature_seconds, 1.5)


if __name__ == "__main__":
    unittest.main()
