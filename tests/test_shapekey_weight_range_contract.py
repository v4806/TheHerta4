# -*- coding: utf-8 -*-
"""形态键权重「不再夹 0..1」的契约测试。

盯三件事：

1. shader 里的硬边界 define 必须与 common/shapekey_weight_range.py 一致
   （shader 没法 import Python，只能镜像，这里就是那道核对）；
2. 那 5 个 hlsl 里不能残留 `clamp(x, 0.0, 1.0)` —— 旧代码正是它把 ini 里的
   `$Freq_xxx = 1.6` 压成 1.0；
3. 生成端两个方向的映射（权重 <-> 滑块位置）在默认窗口下必须**产出与放开前
   逐字节相同的文本**，在放宽窗口后必须能把 1.6 / -0.4 原样带过去。
"""

import math
import os
import re
import sys
import types
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# common/ 不是包（没有 __init__.py 也可加载），直接按文件路径装进 sys.modules，
# 这样这里不需要真 bpy。
if "shapekey_weight_range" not in sys.modules:
    _mod = types.ModuleType("shapekey_weight_range")
    _path = os.path.join(_REPO_ROOT, "common", "shapekey_weight_range.py")
    with open(_path, "r", encoding="utf-8") as handle:
        exec(compile(handle.read(), _path, "exec"), _mod.__dict__)
    sys.modules["shapekey_weight_range"] = _mod

SWR = sys.modules["shapekey_weight_range"]

SHADER_FILES = (
    "Toolset/shapekey_weight_sync.hlsl",
    "Toolset/drag_interaction/rzm_shapekey_var_sync.hlsl",
    "Toolset/drag_interaction/rzm_shapekey_drive.hlsl",
    "Toolset/drag_interaction_efmi/efmi_shapekey_var_sync.hlsl",
    "Toolset/drag_interaction_efmi/efmi_shapekey_drive.hlsl",
)

GENERATOR_FILES = (
    "blueprint/node_postprocess_shapekey_ext.py",
    "blueprint/node_postprocess_slider.py",
)


def _read(relative_path):
    with open(os.path.join(_REPO_ROOT, relative_path), "r", encoding="utf-8") as handle:
        return handle.read()


def _define_value(text, name):
    match = re.search(r"#define\s+" + name + r"\s+\(\s*([-+]?[0-9.]+)\s*\)", text)
    if match is None:
        match = re.search(r"#define\s+" + name + r"\s+([-+]?[0-9.]+)", text)
    return None if match is None else float(match.group(1))


class ShapekeyWeightRangeContractTest(unittest.TestCase):
    def test_hard_bounds_are_wide_enough(self):
        self.assertLess(SWR.SHAPEKEY_WEIGHT_MIN, 0.0)
        self.assertGreater(SWR.SHAPEKEY_WEIGHT_MAX, 1.0)
        # 1.6 / -0.4 是现场里的真实取值，必须落在硬边界内
        self.assertLessEqual(SWR.SHAPEKEY_WEIGHT_MIN, -0.4)
        self.assertGreaterEqual(SWR.SHAPEKEY_WEIGHT_MAX, 1.6)

    def test_default_slider_window_stays_0_1(self):
        # 默认窗口等于放开前，老工程产物不变；要宽区间得显式改节点属性
        self.assertEqual(SWR.SLIDER_WINDOW_MIN, 0.0)
        self.assertEqual(SWR.SLIDER_WINDOW_MAX, 1.0)

    def test_every_shader_mirrors_the_hard_bounds(self):
        for relative_path in SHADER_FILES:
            with self.subTest(shader=relative_path):
                text = _read(relative_path)
                self.assertEqual(_define_value(text, "SHAPEKEY_WEIGHT_MIN"),
                                 SWR.SHAPEKEY_WEIGHT_MIN)
                self.assertEqual(_define_value(text, "SHAPEKEY_WEIGHT_MAX"),
                                 SWR.SHAPEKEY_WEIGHT_MAX)

    def test_no_shapekey_shader_clamps_to_0_1(self):
        hardcoded = re.compile(r"clamp\(\s*[^,()]+,\s*0\.0\s*,\s*1\.0\s*\)")
        for relative_path in SHADER_FILES:
            with self.subTest(shader=relative_path):
                self.assertIsNone(
                    hardcoded.search(_read(relative_path)),
                    "残留 0..1 硬夹：这个文件会把 >1 / <0 的权重压回去",
                )

    def test_weight_writes_read_the_hard_bounds(self):
        """clamp 的两个端点必须是 define，不是字面量。"""
        for relative_path in SHADER_FILES:
            with self.subTest(shader=relative_path):
                text = _read(relative_path)
                if "clamp(" not in text:
                    continue
                self.assertIn("SHAPEKEY_WEIGHT_MIN", text)
                self.assertIn("SHAPEKEY_WEIGHT_MAX", text)


class SliderMappingTest(unittest.TestCase):
    def test_identity_window_keeps_legacy_text(self):
        # 默认窗口下生成的 ini 行必须与放开前完全一致
        self.assertEqual(SWR.weight_from_param_expr("$param3", 0.0, 1.0), "$param3")
        self.assertEqual(SWR.param_from_weight_expr("$Freq_3", 0.0, 1.0), "$Freq_3")

    def test_wide_window_roundtrips_out_of_range_weights(self):
        window_min, window_max = -0.5, 1.5
        to_param = SWR.param_from_weight_expr("w", window_min, window_max)
        to_weight = SWR.weight_from_param_expr("p", window_min, window_max)
        self.assertEqual(to_param, "(w + 0.5) / 2.0")
        self.assertEqual(to_weight, "p * 2.0 - 0.5")

        for weight in (1.6, -0.4, 0.0, 1.0):
            with self.subTest(weight=weight):
                param = eval(to_param, {"__builtins__": {}}, {"w": weight})
                back = eval(to_weight, {"__builtins__": {}}, {"p": param})
                self.assertAlmostEqual(back, weight, places=6)

    def test_wide_window_expression_is_valid_ini_text(self):
        """生成的算术只允许 + - * / 和数字：`+ -` / `- -` 这种写法一律不要。"""
        for window_min, window_max in ((-0.5, 1.5), (0.0, 2.0), (-2.0, 0.0)):
            for text in (SWR.param_from_weight_expr("x", window_min, window_max),
                         SWR.weight_from_param_expr("x", window_min, window_max)):
                with self.subTest(window=(window_min, window_max), text=text):
                    self.assertNotIn("+ -", text)
                    self.assertNotIn("- -", text)
                    self.assertEqual(re.sub(r"[\s0-9.()*/+-]", "", text), "x")

    def test_bad_window_falls_back_to_default(self):
        for window_min, window_max in ((1.0, 1.0), (2.0, 0.0), ("a", "b")):
            with self.subTest(window=(window_min, window_max)):
                self.assertEqual(SWR.normalize_window(window_min, window_max),
                                 (0.0, 1.0))


class GeneratorWiringTest(unittest.TestCase):
    """静态数据流：生成的 `$Freq_x = ...` 必须经过映射函数，而不是直接吃 $paramN。"""

    def test_generators_call_the_mapping_helpers(self):
        for relative_path in GENERATOR_FILES:
            with self.subTest(generator=relative_path):
                text = _read(relative_path)
                self.assertIn("weight_from_param_expr", text)

    def test_no_unconditional_param_assignment_left(self):
        direct = re.compile(r"\{sorted_freq_params\[i-1\]\}\s*=\s*\$param\{i\}")
        for relative_path in GENERATOR_FILES:
            with self.subTest(generator=relative_path):
                self.assertIsNone(
                    direct.search(_read(relative_path)),
                    "生成端还有直接 `$Freq_x = $paramN` 的写法：只有默认窗口下才等价",
                )


class SyncWeightSemanticsTest(unittest.TestCase):
    """把 shapekey_weight_sync.hlsl 里那几行按同样语义在 CPU 上跑一遍。

    这不是「文本里有 define」的自证，而是把 ini 里的原始值喂进同一条判定，
    确认 1.6 / -0.4 真的原样出得来、只有失控值和 NaN 被挡。
    """

    def _apply(self, raw):
        text = _read("Toolset/shapekey_weight_sync.hlsl")
        low = _define_value(text, "SHAPEKEY_WEIGHT_MIN")
        high = _define_value(text, "SHAPEKEY_WEIGHT_MAX")
        if raw != raw:  # NaN：shader 侧 isnan 守卫先归 0
            raw = 0.0
        return min(max(raw, low), high)

    def test_ini_values_survive_the_sync_shader(self):
        for raw in (1.6, -0.4, 0.0, 1.0, 2.5, -1.25):
            with self.subTest(raw=raw):
                self.assertAlmostEqual(self._apply(raw), raw, places=6)

    def test_runaway_and_nan_are_still_caught(self):
        self.assertEqual(self._apply(float("nan")), 0.0)
        self.assertEqual(self._apply(1e9), 4.0)
        self.assertEqual(self._apply(-1e9), -4.0)

    def test_sync_shader_zeroes_nan_before_clamping(self):
        # NaN 会被 clamp 原样透传，所以必须先判 NaN —— 这一行不能被简化掉
        self.assertIn("isnan(raw)", _read("Toolset/shapekey_weight_sync.hlsl"))


class DragChainTest(unittest.TestCase):
    """拖动链路：负权重同样算「在起作用」，不能被门控当成 0。"""

    def test_var_sync_gate_uses_absolute_value(self):
        for relative_path in ("Toolset/drag_interaction/rzm_shapekey_var_sync.hlsl",
                              "Toolset/drag_interaction_efmi/efmi_shapekey_var_sync.hlsl"):
            with self.subTest(shader=relative_path):
                text = _read(relative_path)
                self.assertIn("if (abs(v) > 1e-6)", text)
                self.assertNotIn("if (v > 1e-6)", text)

    def test_drive_accumulator_can_cross_both_boundaries(self):
        """位移积分从 0 出发，正负净位移都要能推过 1 和 0。"""
        text = _read("Toolset/drag_interaction/rzm_shapekey_drive.hlsl")
        low = _define_value(text, "SHAPEKEY_WEIGHT_MIN")
        high = _define_value(text, "SHAPEKEY_WEIGHT_MAX")
        self.assertLess(low, 0.0)
        self.assertGreater(high, 1.0)

        def step(current, net, move_len=1.0, sensitivity=1.0):
            return min(max(current + net * move_len * sensitivity, low), high)

        self.assertGreater(step(0.9, 1.0, 0.5), 1.0)
        self.assertLess(step(0.1, -1.0, 0.5), 0.0)


class AnimWeightGateTest(unittest.TestCase):
    """动画累加的门闸：负权重是反向位移，只有「恰好为 0」才允许跳过。

    这里锁的是**符号**，不是数值：旧版生成端写的是 ``if (anim_weight_x > 1e-5)``，
    于是 ini 里 ``$Freq_x = -0.4`` 整段累加被跳过、模型一动不动，表现成「只能设正值」。
    """

    GATE_FILES = (
        "blueprint/shapekey_sparse_index.py",
        "blueprint/node_postprocess_shapekey.py",
    )

    def test_every_weight_gate_takes_the_absolute_value(self):
        for relative_path in self.GATE_FILES:
            with self.subTest(generator=relative_path):
                gates = [line.strip() for line in _read(relative_path).splitlines()
                         if "if (" in line and "anim_weight_" in line]
                self.assertTrue(gates, "没找到门闸行：判据失效，检查变量名")
                for line in gates:
                    self.assertIn("abs(", line, "门闸按符号判会把负权重整段吃掉")

    def test_sparse_docstring_shows_the_same_gate_as_the_code(self):
        # 模块 docstring 里那段示例是读代码的人唯一会照抄的地方
        text = _read("blueprint/shapekey_sparse_index.py")
        self.assertIn("if (abs(anim_weight_entry) > 1e-5)", text)
        self.assertNotIn("if (anim_weight_entry > 1e-5)", text)


class FrameTableProgressTest(unittest.TestCase):
    """帧表采样：进度允许为负（单键组里它就是权重），越界线性外推而不是夹住。

    与 ``SyncWeightSemanticsTest`` 同样的做法——把 hlsl 的判定按同一语义在 CPU 上
    跑一遍，确认 ``-0.4`` 真的得到 ``-0.4 × 位移``，而不是被压成 0。
    """

    FRAME_KEYS = 1  # 单键组：帧表 = [零位移, delta]

    def _bound(self):
        text = _read("Toolset/shapekey_anim_frame_table.hlsl")
        bound = _define_value(text, "FRAME_TABLE_EXTRAPOLATION_FRAMES")
        self.assertIsNotNone(bound, "外推帧数上限的 define 不见了")
        return text, bound

    def test_sampler_no_longer_clips_the_progress(self):
        text, _bound = self._bound()
        self.assertNotIn("saturate(t)", text)
        # 外推上限必须引用 define，不能在 clamp 里写死一个数字
        self.assertIn("FRAME_TABLE_EXTRAPOLATION_FRAMES", text)
        self.assertIn("scaled < 0.0", text)

    def _sample(self, frames, key_count, t, bound):
        """``frame_table_sample`` 的 CPU 移植（union_size = 1、local_index = 0）。"""
        if key_count == 0:
            return (0.0, 0.0, 0.0)
        if t != t:  # NaN 守卫
            t = 0.0
        scaled = min(max(t * float(key_count), -bound), float(key_count) + bound)
        if scaled < 0.0:
            origin, first = frames[0], frames[1]
            return tuple(origin[k] + (first[k] - origin[k]) * scaled for k in range(3))
        frame_index = int(math.floor(scaled))
        frac = scaled - float(frame_index)
        if frame_index >= key_count:
            frame_index = key_count - 1
            frac = scaled - float(frame_index)
        a = frames[frame_index]
        if frac <= 0.0:
            return a
        b = frames[frame_index + 1]
        return tuple(a[k] + (b[k] - a[k]) * frac for k in range(3))

    def test_single_key_group_runs_backwards(self):
        """单键组：进度就是权重，负值必须得到负位移。"""
        _text, bound = self._bound()
        frames = [(0.0, 0.0, 0.0), (1.0, 0.0, 0.5)]
        for t, expected in ((-0.4, (-0.4, 0.0, -0.2)), (0.0, (0.0, 0.0, 0.0)),
                            (0.5, (0.5, 0.0, 0.25)), (1.0, (1.0, 0.0, 0.5)),
                            (1.6, (1.6, 0.0, 0.8))):
            with self.subTest(t=t):
                self.assertEqual(self._sample(frames, 1, t, bound), expected)

    def test_overshoot_is_bounded(self):
        _text, bound = self._bound()
        frames = [(0.0, 0.0, 0.0), (1.0, 0.0, 0.0)]
        # 下界顶到 -上限；上界是「键数 + 上限」帧，界外不再线性放大
        self.assertEqual(self._sample(frames, 1, -1e9, bound), (-bound, 0.0, 0.0))
        self.assertEqual(self._sample(frames, 1, 1e9, bound), (1.0 + bound, 0.0, 0.0))

    def test_multi_key_group_extrapolates_along_the_first_segment(self):
        _text, bound = self._bound()
        # 3 键：帧 0 = 零，帧 1 = 第一个键，帧 3 = 三个键全叠
        frames = [(0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (3.0, 0.0, 0.0), (6.0, 0.0, 0.0)]
        self.assertEqual(self._sample(frames, 3, 0.5, bound), (2.0, 0.0, 0.0))
        self.assertEqual(self._sample(frames, 3, 1.0, bound), (6.0, 0.0, 0.0))
        self.assertEqual(self._sample(frames, 3, -0.5, bound), (-1.5, 0.0, 0.0))
        # t = 2 → 帧 2 之后再加 4 段（每段 +3）
        self.assertEqual(self._sample(frames, 3, 2.0, bound), (15.0, 0.0, 0.0))


if __name__ == "__main__":
    unittest.main()
