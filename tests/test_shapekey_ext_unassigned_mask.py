# -*- coding: utf-8 -*-
"""形态键扩展节点：未分配屏蔽 / 单位速度 / 变速区间语义 的回归测试。

覆盖三类曾经出问题、且很容易再次被改坏的行为：
1. 屏蔽与还原必须**逐行可逆**（含缩进），并且按解析出的变量名匹配，
   不能假定变量名一定以 ``Freq_`` 开头（「导出变量」是自由文本）。
2. 被屏蔽的 Shader 槽位要补一行显式归零：IniParams 是粘滞的，同槽位还有
   拖拽交互 / UV 偏移等模块每帧写入，只注释原行并不保证 Shader 读到 0。
3. 单位速度必须兑现「总时长」（允许小数步进，不能钳成 ≥1）；升级前保存的
   老工程（speed_percent_mode=False）必须与升级前的步进值完全一致。
"""
import importlib.util
import sys
import types
import unittest
from collections import OrderedDict
from pathlib import Path


def _install_module(name, **attrs):
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


PKG = "_test_shapekey_ext_unassigned_mask_pkg"
for package_name in (PKG, f"{PKG}.blueprint", f"{PKG}.common", f"{PKG}.utils"):
    package = _install_module(package_name)
    package.__path__ = []

_prop = lambda **_kwargs: None
_install_module(
    "bpy",
    types=types.SimpleNamespace(PropertyGroup=object, Operator=object, UIList=object),
    props=types.SimpleNamespace(
        StringProperty=_prop,
        BoolProperty=_prop,
        IntProperty=_prop,
        FloatProperty=_prop,
        EnumProperty=_prop,
        CollectionProperty=_prop,
        FloatVectorProperty=_prop,
    ),
    data=types.SimpleNamespace(objects={}, node_groups=[]),
    utils=types.SimpleNamespace(register_class=lambda _cls: None, unregister_class=lambda _cls: None),
)
_install_module(
    f"{PKG}.blueprint.node_postprocess_base",
    SSMTNode_PostProcess_Base=type("_FakePostProcessBase", (object,), {}),
)

_module_path = Path(__file__).resolve().parents[1] / "blueprint" / "node_postprocess_shapekey_ext.py"
_spec = importlib.util.spec_from_file_location(
    f"{PKG}.blueprint.node_postprocess_shapekey_ext", _module_path
)
module = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = module
_spec.loader.exec_module(module)


class _Interval:
    def __init__(self, start=0.0, end=100.0, base_step=100):
        self.start = start
        self.end = end
        self.base_step = base_step


class _Setting:
    """ShapeKeyPlayGroupSettings 的最小替身。"""

    def __init__(self, **kwargs):
        self.auto_playback_frame_count = kwargs.get("auto_playback_frame_count", 30)
        self.auto_playback_duration = kwargs.get("auto_playback_duration", 2.0)
        self.playback_fps = kwargs.get("playback_fps", 60)
        self.speed_percent = kwargs.get("speed_percent", 100)
        self.speed_percent_max = kwargs.get("speed_percent_max", 1000)
        self.speed_percent_min = kwargs.get("speed_percent_min", 100)
        self.speed_percent_mode = kwargs.get("speed_percent_mode", True)
        self.speed_intervals = kwargs.get("speed_intervals", [])
        self.enable_auto_playback = kwargs.get("enable_auto_playback", True)


def _make_node():
    node = module.SSMTNode_PostProcess_ShapeKeyExt.__new__(
        module.SSMTNode_PostProcess_ShapeKeyExt
    )
    node.namespace = "testns"
    node.play_group_entries = []
    return node


class UnassignedMaskTests(unittest.TestCase):
    """未分配形态键的三层屏蔽与可逆还原。"""

    def setUp(self):
        self.node = _make_node()

    @staticmethod
    def _sections():
        sections = OrderedDict()
        sections["[Constants]"] = [
            "; 控制形态键 '摇摆' 的强度",
            "global persist $Freq_yaobai_001 = 0.0",
            "global persist $MyShape = 1.0",
            "global persist $Freq_Group1 = 0.0",
        ]
        sections["[Present]"] = [
            "    $Freq_yaobai_001 = $Freq_Group1",
            "if ($ui_active == 1)",
            "    $Freq_yaobai_001 = $param0",
            "    $MyShape = $param1",
            "endif",
        ]
        sections["[CustomShader_ab12cd34_Anim]"] = [
            "x100 = $Freq_yaobai_001",
            "x101 = $MyShape",
            "x102 = $Freq_Group1",
        ]
        return sections

    def test_mask_comments_all_three_layers_and_unmask_is_line_exact(self):
        sections = self._sections()
        original = {name: list(lines) for name, lines in sections.items()}

        self.node._mask_unassigned_shapekeys(sections, {"Freq_yaobai_001"})

        self.assertIn("; [未分配-已屏蔽] global persist $Freq_yaobai_001 = 0.0",
                      sections["[Constants]"])
        # 未被屏蔽的变量必须保持原样
        self.assertIn("global persist $MyShape = 1.0", sections["[Constants]"])
        self.assertIn("; [未分配-已屏蔽]     $Freq_yaobai_001 = $param0",
                      sections["[Present]"])
        self.assertIn("; [未分配-已屏蔽] x100 = $Freq_yaobai_001",
                      sections["[CustomShader_ab12cd34_Anim]"])

        restored = self.node._unmask_all_shapekeys(sections)

        self.assertEqual(restored, 4)  # Constants 1 + Present 2 + Shader 1
        for name, lines in original.items():
            self.assertEqual(sections[name], lines, f"{name} 未逐行还原")

    def test_mask_matches_custom_variable_name_without_freq_prefix(self):
        """「导出变量」是自由文本：不带 Freq_ 前缀的名字也必须被屏蔽。"""
        sections = self._sections()

        self.node._mask_unassigned_shapekeys(sections, {"MyShape"})

        self.assertIn("; [未分配-已屏蔽] global persist $MyShape = 1.0",
                      sections["[Constants]"])
        self.assertIn("; [未分配-已屏蔽]     $MyShape = $param1", sections["[Present]"])
        self.assertIn("; [未分配-已屏蔽] x101 = $MyShape",
                      sections["[CustomShader_ab12cd34_Anim]"])
        # 不相干的形态键不受影响
        self.assertIn("global persist $Freq_yaobai_001 = 0.0", sections["[Constants]"])

    def test_accepts_names_with_dollar_prefix(self):
        sections = self._sections()

        self.node._mask_unassigned_shapekeys(sections, {"$MyShape"})

        self.assertIn("; [未分配-已屏蔽] global persist $MyShape = 1.0",
                      sections["[Constants]"])

    def test_shader_layer_writes_explicit_zero_and_unmask_removes_it(self):
        """IniParams 是粘滞的：注释原行之外必须补一行显式归零。"""
        sections = self._sections()

        self.node._mask_unassigned_shapekeys(sections, {"MyShape"})

        shader_lines = sections["[CustomShader_ab12cd34_Anim]"]
        self.assertEqual(
            shader_lines,
            [
                "x100 = $Freq_yaobai_001",
                "; [未分配-已屏蔽] x101 = $MyShape",
                "; [未分配-已屏蔽-归零] x101 = 0",
                "x102 = $Freq_Group1",
            ],
        )

        self.node._unmask_all_shapekeys(sections)

        self.assertEqual(
            sections["[CustomShader_ab12cd34_Anim]"],
            ["x100 = $Freq_yaobai_001", "x101 = $MyShape", "x102 = $Freq_Group1"],
        )

    def test_mask_is_idempotent_across_repeated_refresh(self):
        sections = self._sections()
        first_pass = None
        for _ in range(3):
            self.node._unmask_all_shapekeys(sections)
            self.node._mask_unassigned_shapekeys(sections, {"MyShape"})
            if first_pass is None:
                first_pass = {name: list(lines) for name, lines in sections.items()}
            else:
                for name, lines in first_pass.items():
                    self.assertEqual(sections[name], lines, f"{name} 反复刷新后不稳定")

    def test_empty_target_does_not_touch_sections(self):
        sections = self._sections()
        original = {name: list(lines) for name, lines in sections.items()}

        self.node._mask_unassigned_shapekeys(sections, set())

        for name, lines in original.items():
            self.assertEqual(sections[name], lines)


class UnitSpeedTests(unittest.TestCase):
    """单位速度：兑现「总时长」，允许小数步进。"""

    def setUp(self):
        self.node = _make_node()

    def test_unit_speed_keeps_fraction_for_small_subdivision_counts(self):
        setting = _Setting(auto_playback_frame_count=30, auto_playback_duration=2.0, playback_fps=60)

        # 30 / (2 × 60) = 0.25 —— 之前被钳成 1，导致 4 倍速播放
        self.assertAlmostEqual(self.node._calc_unit_speed(setting), 0.25, places=4)

    def test_unit_speed_matches_documented_example(self):
        setting = _Setting(auto_playback_frame_count=2400, auto_playback_duration=2.0, playback_fps=120)

        self.assertAlmostEqual(self.node._calc_unit_speed(setting), 10.0, places=4)

    def test_unit_speed_never_reaches_zero(self):
        setting = _Setting(auto_playback_frame_count=2, auto_playback_duration=300.0, playback_fps=360)

        self.assertGreater(self.node._calc_unit_speed(setting), 0.0)

    def test_generated_step_lines_use_fractional_unit_speed(self):
        sections = OrderedDict()
        self.node._add_auto_playback_logic_for_group(
            sections, 1, "$Freq_Group1", 30, "FORWARD", 100, 1000,
            [_Interval(0.0, 100.0, 100)], 3, 0.25, True,
        )

        steps = [line.strip() for line in sections["[Present]"] if "base_step_group1 =" in line]
        self.assertEqual(steps[0], "$base_step_group1 = 0.25")


class IntervalSemanticsTests(unittest.TestCase):
    """变速区间：新语义按百分比，老工程按旧的每帧步进数（行为不变）。"""

    def setUp(self):
        self.node = _make_node()

    def _step_line(self, base_step, unit_speed, percent_mode):
        sections = OrderedDict()
        self.node._add_auto_playback_logic_for_group(
            sections, 1, "$Freq_Group1", 30, "FORWARD", 100, 1000,
            [_Interval(0.0, 100.0, base_step)], 3, unit_speed, percent_mode,
        )
        return [line.strip() for line in sections["[Present]"] if "base_step_group1 =" in line]

    def test_legacy_interval_keeps_old_step_values(self):
        # 升级前保存的工程：base_step=10 就是每帧 10 个细分，与单位速度无关
        self.assertEqual(self._step_line(10, 0.25, False)[0], "$base_step_group1 = 10")

    def test_percent_interval_scales_with_unit_speed(self):
        self.assertEqual(self._step_line(100, 0.25, True)[0], "$base_step_group1 = 0.25")
        self.assertEqual(self._step_line(10, 0.25, True)[0], "$base_step_group1 = 0.025")

    def test_legacy_tail_and_fallback_match_old_generator(self):
        self.assertEqual(self.node._tail_step_value(0.25, False), 1.0)
        self.assertEqual(self.node._no_interval_fallback_step(0.25, False), 10.0)
        self.assertEqual(self.node._tail_step_value(0.25, True), 0.25)
        self.assertEqual(self.node._no_interval_fallback_step(0.25, True), 0.25)

    def test_auto_unroll_covers_both_semantics(self):
        percent = _Setting(speed_percent_mode=True, speed_intervals=[_Interval(base_step=100)])
        legacy = _Setting(speed_percent_mode=False, speed_intervals=[_Interval(base_step=10)])

        # 新语义：0.25 × 100% × 1000% × 1.5 → 4 → 下限 10
        self.assertEqual(self.node._calc_auto_max_step_unroll(percent), 10)
        # 老语义：10 × 1000% × 1.5 = 150
        self.assertEqual(self.node._calc_auto_max_step_unroll(legacy), 150)


class VarToGroupMapTests(unittest.TestCase):
    """没有分组条目的形态键按未分组处理（不再被并进分组 1）。"""

    def setUp(self):
        self.node = _make_node()
        self.node.play_group_entries = [
            types.SimpleNamespace(shape_key_name="摇摆", group_index=1),
            types.SimpleNamespace(shape_key_name="闲置", group_index=0),
        ]

    def test_orphan_and_unassigned_variables_are_group_zero(self):
        freq_vars = OrderedDict([
            ("$Freq_yaobai", {"default": "0.0", "label": "摇摆"}),
            ("$Freq_xianzhi", {"default": "0.0", "label": "闲置"}),
            ("$Freq_orphan", {"default": "0.0", "label": "没有条目的形态键"}),
        ])

        var_to_group, all_groups = self.node._build_var_to_group_map(
            freq_vars, ["摇摆", "闲置", "没有条目的形态键"]
        )

        self.assertEqual(var_to_group["$Freq_yaobai"], 1)
        self.assertEqual(var_to_group["$Freq_xianzhi"], 0)
        self.assertEqual(var_to_group["$Freq_orphan"], 0)
        self.assertEqual(all_groups, [1])


if __name__ == "__main__":
    unittest.main()
