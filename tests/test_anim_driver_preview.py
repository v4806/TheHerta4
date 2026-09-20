"""动画驱动关键帧预览：解释器语义与三个已开放节点的逐帧序列。"""

import importlib.util
import sys
import types
import unittest
from pathlib import Path


def _install_module(name, **attrs):
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


PKG = "_anim_driver_preview_test_pkg"
for package_name in (PKG, f"{PKG}.blueprint", f"{PKG}.common"):
    package = _install_module(package_name)
    package.__path__ = []


class _FakeObjectCollection:
    """够用的 ``bpy.data.objects``：支持 get(name) 与迭代。"""

    def __init__(self, objects=()):
        self._objects = list(objects)

    def __iter__(self):
        return iter(self._objects)

    def __len__(self):
        return len(self._objects)

    def get(self, name):
        for obj in self._objects:
            if getattr(obj, "name", "") == name:
                return obj
        return None


_install_module(
    "bpy.props",
    FloatProperty=lambda **_kwargs: None,
    BoolProperty=lambda **_kwargs: None,
    IntProperty=lambda **_kwargs: None,
    StringProperty=lambda **_kwargs: None,
    EnumProperty=lambda **_kwargs: None,
    CollectionProperty=lambda **_kwargs: None,
)
_install_module(
    "bpy.types",
    Menu=object,
    NodeTree=object,
    NodeSocket=object,
    Node=object,
    SpaceNodeEditor=object,
    PropertyGroup=object,
    UIList=object,
    Operator=object,
)
#: 保留对本文件 stub 的直接引用：整仓跑测试时 sys.modules["bpy"] 会被其它测试文件
#: 的 stub 覆盖，运行时再去取就会拿到别人的模块。
bpy_stub = _install_module(
    "bpy",
    props=sys.modules["bpy.props"],
    types=sys.modules["bpy.types"],
    utils=types.SimpleNamespace(register_class=lambda _cls: None, unregister_class=lambda _cls: None),
    app=types.SimpleNamespace(
        handlers=types.SimpleNamespace(persistent=lambda func: func, load_post=[])
    ),
    data=types.SimpleNamespace(
        objects=_FakeObjectCollection(),
        node_groups=[],
        actions=[],
        shape_keys=[],
    ),
    context=types.SimpleNamespace(scene=None),
)
_install_module(
    f"{PKG}.blueprint.node_base",
    SSMTBlueprintTree=object,
    SSMTNodeBase=object,
    refresh_blueprint_node_colors=lambda *_args, **_kwargs: None,
)
_install_module(
    f"{PKG}.common.global_config",
    GlobalConfig=types.SimpleNamespace(workspacename="Test", logic_name="", read_from_main_json_ssmt4=lambda: None),
)
_install_module(
    f"{PKG}.common.logic_name",
    LogicName=types.SimpleNamespace(NTEMI="NTEMI"),
)
_install_module(
    f"{PKG}.common.object_prefix_helper",
    ObjectPrefixHelper=types.SimpleNamespace(
        extract_prefix_info=lambda _name: None,
        parse_prefix_parts=lambda _prefix: {},
    ),
)


def _stub_ensure_anim_driver_frame_variable_name(node, context=None):
    name = str(getattr(node, "assigned_frame_variable_name", "") or "").strip()
    if not name:
        name = f"anim_frame{int(getattr(node, 'auto_index', 0) or 0)}"
        try:
            node.assigned_frame_variable_name = name
        except Exception:
            pass
    return name


_install_module(
    f"{PKG}.blueprint.variable_registry",
    ANIM_DRIVER_FRAME_PREFIX="anim_frame",
    allocate_continuous_shapekey_index_variable_name=lambda **_kwargs: "continuous_shapekey_frame1",
    mark_variable_name_used=lambda *_args, **_kwargs: None,
    normalize_variable_name=lambda value: str(value or "").strip().lstrip("$"),
    ensure_anim_driver_frame_variable_name=_stub_ensure_anim_driver_frame_variable_name,
    build_shape_key_reference_alias_map=lambda context=None: {},
    resolve_reference_variable_name=lambda name, alias_map=None: (
        (alias_map or {}).get(str(name or "").strip().lstrip("$"), str(name or "").strip().lstrip("$"))
    ),
)


def _load_blueprint_module(module_name):
    module_path = Path(__file__).resolve().parents[1] / "blueprint" / f"{module_name}.py"
    spec = importlib.util.spec_from_file_location(f"{PKG}.blueprint.{module_name}", module_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


anim_driver_base = _load_blueprint_module("anim_driver_base")
anim_driver_collector = _load_blueprint_module("anim_driver_collector")
runtime_module = _load_blueprint_module("anim_driver_runtime")
forward_play_module = _load_blueprint_module("anim_driver_forward_play")
pingpong_module = _load_blueprint_module("anim_driver_pingpong")
random_module = _load_blueprint_module("anim_driver_random")
preview = _load_blueprint_module("anim_driver_preview")


class _FakeSocket:
    bl_idname = "SSMTSocketAnimDriver"


class _FakeLink:
    def __init__(self, from_node, to_node):
        self.from_node = from_node
        self.to_node = to_node
        self.from_socket = _FakeSocket()
        self.to_socket = _FakeSocket()


def _make_tree(nodes, links=()):
    tree = types.SimpleNamespace(name="AnimTree", nodes=list(nodes), links=list(links))
    for node in nodes:
        node.id_data = tree
    return tree


def _make_runtime(name="Runtime", auto_index=1, fps=30, playback_rate=1):
    node = runtime_module.SSMTNode_AnimDriver_Runtime()
    node.name = name
    node.auto_index = auto_index
    node.fps = fps
    node.playback_rate = playback_rate
    node.custom_frame_variable_name = f"anim_frame{auto_index}"
    node.assigned_frame_variable_name = f"anim_frame{auto_index}"
    return node


def _collect_paragraphs(tree):
    return anim_driver_collector.AnimationDriverCollector(tree).collect()


class ExpressionTests(unittest.TestCase):
    def _evaluate(self, text, variables=None):
        context = preview.AnimationDriverSimulationContext()
        context.variables.update(variables or {})
        return preview.evaluate_expression(preview.parse_ini_expression(text), context)

    def test_arithmetic_precedence_and_parentheses(self):
        self.assertEqual(self._evaluate("1 + 2 * 3"), 7.0)
        self.assertEqual(self._evaluate("(1 + 2) * 3"), 9.0)
        self.assertEqual(self._evaluate("-4 + 1"), -3.0)

    def test_floor_division_and_modulo_follow_c_semantics(self):
        self.assertEqual(self._evaluate("7 // 2"), 3.0)
        self.assertEqual(self._evaluate("-7 // 2"), -4.0)
        self.assertEqual(self._evaluate("7 % 3"), 1.0)
        self.assertEqual(self._evaluate("-7 % 3"), -1.0)

    def test_comparison_and_logical_operators(self):
        self.assertEqual(self._evaluate("1 < 2 && 3 > 4"), 0.0)
        self.assertEqual(self._evaluate("1 < 2 || 3 > 4"), 1.0)
        self.assertEqual(self._evaluate("2 >= 2"), 1.0)
        self.assertEqual(self._evaluate("$a != 1", {"$a": 2.0}), 1.0)

    def test_float32_round_trip(self):
        self.assertNotEqual(preview.to_float32(1.0 / 3.0), 1.0 / 3.0)
        self.assertAlmostEqual(preview.to_float32(0.1), 0.1, places=7)
        self.assertEqual(preview.to_float32(16777216.0), 16777216.0)

    def test_frame_counter_detection(self):
        self.assertTrue(
            preview.is_frame_counter_expression(preview.parse_ini_expression("(time * 30) // 1"))
        )
        self.assertTrue(
            preview.is_frame_counter_expression(preview.parse_ini_expression("(time * 60.0) / 1"))
        )
        self.assertFalse(
            preview.is_frame_counter_expression(preview.parse_ini_expression("(time * 30) // 2"))
        )
        self.assertFalse(
            preview.is_frame_counter_expression(preview.parse_ini_expression("$frame + 1"))
        )


class InterpreterTests(unittest.TestCase):
    def _run(self, lines, variables, frame_index=0):
        context = preview.AnimationDriverSimulationContext()
        context.variables.update(variables)
        instructions = preview.compile_present_lines(lines, context)
        preview.execute_instructions(instructions, context, frame_index)
        return context

    def test_if_elif_else_chain(self):
        lines = [
            "if $a == 1",
            "    $b = 10",
            "elif $a == 2",
            "    $b = 20",
            "else",
            "    $b = 30",
            "endif",
        ]
        self.assertEqual(self._run(lines, {"$a": 1.0}).variables["$b"], 10.0)
        self.assertEqual(self._run(lines, {"$a": 2.0}).variables["$b"], 20.0)
        self.assertEqual(self._run(lines, {"$a": 3.0}).variables["$b"], 30.0)

    def test_else_if_variant_is_supported(self):
        lines = ["if $drv < 1", "    $v = 1", "else if $drv < 2", "    $v = 2", "else", "    $v = 3", "endif"]
        self.assertEqual(self._run(lines, {"$drv": 0.5}).variables["$v"], 1.0)
        self.assertEqual(self._run(lines, {"$drv": 1.5}).variables["$v"], 2.0)
        self.assertEqual(self._run(lines, {"$drv": 9.5}).variables["$v"], 3.0)

    def test_nested_if_blocks(self):
        lines = [
            "if $paused == 1",
            "    if $frame % 2 == 0",
            "        $v = $v + 1",
            "    endif",
            "endif",
        ]
        self.assertEqual(self._run(lines, {"$paused": 1.0, "$v": 0.0, "$frame": 2.0}).variables["$v"], 1.0)
        self.assertEqual(self._run(lines, {"$paused": 1.0, "$v": 0.0, "$frame": 3.0}).variables["$v"], 0.0)
        self.assertEqual(self._run(lines, {"$paused": 0.0, "$v": 0.0, "$frame": 2.0}).variables["$v"], 0.0)

    def test_unbalanced_endif_is_reported_not_raised(self):
        context = preview.AnimationDriverSimulationContext()
        instructions = preview.compile_present_lines(["endif", "$a = 1"], context)
        preview.execute_instructions(instructions, context, 0)
        self.assertEqual(context.variables["$a"], 1.0)
        self.assertTrue(any("endif" in warning for warning in context.warnings))

    def test_constants_declaration_applies_initial_values(self):
        context = preview.AnimationDriverSimulationContext()
        preview.apply_constant_lines(
            ["global persist $a = 3", "global $b = -1.5", "global $c", "post run = CustomShader"],
            context,
        )
        self.assertEqual(context.variables["$a"], 3.0)
        self.assertEqual(context.variables["$b"], -1.5)
        self.assertEqual(context.variables["$c"], 0.0)
        self.assertIn("$a", context.declared)
        self.assertEqual(context.skipped_commands, ["post run = CustomShader"])

    def test_sections_are_split_case_insensitively(self):
        sections, order = preview.split_ini_sections(
            "[Constants]\nglobal $a = 1\n[Present]\n$a = 2\n[KeyToggle_Anim1]\nkey = k\n"
        )
        self.assertEqual(order, ["constants", "present", "keytoggle_anim1"])
        self.assertEqual([line.strip() for line in sections["present"]], ["$a = 2"])


class FrameCounterTests(unittest.TestCase):
    def test_runtime_frame_variable_equals_driver_frame_for_every_fps(self):
        paragraphs = [{
            "ini_content": (
                "[Constants]\n"
                "global persist $anim_frame1 = 0\n"
                "[Present]\n"
                "$anim_frame1 = (time * 30) // 1\n"
            )
        }]
        expected = [float(index) for index in range(300)]
        for fps in (24, 30, 60, 120, 144):
            result = preview.simulate_anim_driver_paragraphs(paragraphs, 300, fps)
            self.assertEqual(result["variables"]["$anim_frame1"], expected, f"fps={fps}")
            self.assertEqual(result["frame_variables"], ["$anim_frame1"])

    def test_driver_frame_phase_gate_survives_playback_rate_two(self):
        paragraphs = [{
            "ini_content": (
                "[Constants]\n"
                "global persist $anim_frame1 = 0\n"
                "global persist $v = 0\n"
                "[Present]\n"
                "$anim_frame1 = (time * 30) // 1\n"
                "if $anim_frame1 % 2 == 0\n"
                "    $v = $v + 1\n"
                "endif\n"
            )
        }]
        result = preview.simulate_anim_driver_paragraphs(paragraphs, 6, 30)
        self.assertEqual(result["variables"]["$v"], [1.0, 1.0, 2.0, 2.0, 3.0, 3.0])


class ForwardPlayPreviewTests(unittest.TestCase):
    def _build(self, **overrides):
        runtime = _make_runtime()
        play = forward_play_module.SSMTNode_AnimDriver_ForwardPlay()
        play.name = "Play"
        play.auto_index = 2
        play.frame_start = 0.0
        play.frame_end = 4.0
        play.play_total_duration = 1.0
        play.use_float_interval = True
        play.default_paused = True
        play.custom_paused_var = "$paused"
        play.reverse_playback = False
        play.loop_playback = False
        play.hold_end_value = False
        play.use_continuous_shapekey_mode = False
        play.driven_variable = ""
        play.driven_variable_list = [types.SimpleNamespace(variable_name="$varA")]
        for key, value in overrides.items():
            setattr(play, key, value)
        tree = _make_tree([runtime, play], [_FakeLink(runtime, play)])
        return tree, play

    def test_segment_uses_runtime_frame_variable_and_interval(self):
        _tree, play = self._build()
        segment = play.generate_ini_segment()
        self.assertIn("global persist $anim_frame1 = 0", play._find_runtime_node().generate_ini_segment())
        self.assertIn("$varA = $varA + 0.1333", segment)
        self.assertIn("if $varA > $frameEnd2", segment)

    def test_preview_advances_then_clamps_then_stops(self):
        tree, _play = self._build()
        paragraphs = _collect_paragraphs(tree)
        self.assertTrue(paragraphs)
        result = preview.simulate_anim_driver_paragraphs(paragraphs, 60, 30)
        values = result["variables"]["$varA"]

        self.assertAlmostEqual(values[0], 0.1333, places=4)
        self.assertAlmostEqual(values[10], 1.4663, places=3)
        # 30 步 × 0.1333 仍小于结束值，第 31 步越界后被钳到结束值
        self.assertAlmostEqual(values[29], 3.999, places=3)
        self.assertEqual(values[30], 4.0)
        # 未启用循环且没有下游节点：播完复位到起始值并置暂停
        self.assertEqual(values[31], 0.0)
        self.assertEqual(values[59], 0.0)
        self.assertEqual(result["variables"]["$paused"][31], 0.0)

    def test_loop_playback_restarts_at_start_value(self):
        tree, _play = self._build(loop_playback=True)
        result = preview.simulate_anim_driver_paragraphs(_collect_paragraphs(tree), 40, 30)
        values = result["variables"]["$varA"]
        self.assertEqual(values[30], 4.0)
        self.assertEqual(values[31], 0.0)
        self.assertAlmostEqual(values[32], 0.1333, places=4)
        self.assertEqual(result["variables"]["$paused"][32], 1.0)

    def test_reverse_playback_counts_down(self):
        tree, _play = self._build(reverse_playback=True, frame_start=0.0, frame_end=4.0, loop_playback=True)
        result = preview.simulate_anim_driver_paragraphs(_collect_paragraphs(tree), 6, 30)
        values = result["variables"]["$varA"]
        # 反向：初值取结束值，逐帧递减
        self.assertEqual(values[0], 4.0)
        self.assertAlmostEqual(values[1], 3.8667, places=4)

    def test_continuous_shapekey_mode_drives_shape_key_variables(self):
        runtime = _make_runtime()
        play = forward_play_module.SSMTNode_AnimDriver_ForwardPlay()
        play.name = "Play"
        play.auto_index = 2
        play.frame_start = 0.0
        play.frame_end = 2.0
        play.play_total_duration = 1.0
        play.use_float_interval = True
        play.default_paused = True
        play.custom_paused_var = "$paused"
        play.reverse_playback = False
        play.loop_playback = True
        play.hold_end_value = False
        play.use_continuous_shapekey_mode = True
        play.continuous_target_object = "Body"
        play.driven_variable = ""
        play.driven_variable_list = []
        play.continuous_shape_key_items = [
            types.SimpleNamespace(shape_key_name="a", variable_name="$Freq_a"),
            types.SimpleNamespace(shape_key_name="b", variable_name="$Freq_b"),
        ]
        tree = _make_tree([runtime, play], [_FakeLink(runtime, play)])
        paragraphs = _collect_paragraphs(tree)
        result = preview.simulate_anim_driver_paragraphs(paragraphs, 20, 30)

        # 连续形态键：$Freq_a = clamp(索引 - 起始帧)，$Freq_b = clamp(索引 - (起始帧+1))
        self.assertIn("$Freq_a = $continuous_shapekey_frame1 - 0.0", paragraphs[0]["ini_content"])
        self.assertIn("$Freq_b = $continuous_shapekey_frame1 - 1.0", paragraphs[0]["ini_content"])
        self.assertAlmostEqual(result["variables"]["$Freq_a"][0], 0.0667, places=4)
        self.assertEqual(result["variables"]["$Freq_a"][19], 1.0)
        self.assertEqual(result["variables"]["$Freq_b"][0], 0.0)
        self.assertGreater(result["variables"]["$Freq_b"][19], 0.0)
        self.assertLessEqual(result["variables"]["$Freq_b"][19], 1.0)


class PingPongPreviewTests(unittest.TestCase):
    def _build(self, **overrides):
        runtime = _make_runtime()
        play = pingpong_module.SSMTNode_AnimDriver_PingPong()
        play.name = "PingPong"
        play.auto_index = 2
        play.frame_start = 0.0
        play.frame_end = 2.0
        play.play_total_duration = 1.0
        play.use_float_interval = True
        play.default_paused = True
        play.custom_paused_var = "$paused"
        play.reverse_playback = False
        play.loop_playback = False
        play.hold_end_value = False
        play.use_continuous_shapekey_mode = False
        play.driven_variable = ""
        play.driven_variable_list = [types.SimpleNamespace(variable_name="$varA")]
        for key, value in overrides.items():
            setattr(play, key, value)
        tree = _make_tree([runtime, play], [_FakeLink(runtime, play)])
        return tree

    def test_goes_up_holds_one_frame_then_comes_back(self):
        tree = self._build()
        result = preview.simulate_anim_driver_paragraphs(_collect_paragraphs(tree), 40, 30)
        values = result["variables"]["$varA"]

        self.assertAlmostEqual(values[0], 0.1333, places=4)
        # 越界后被钳到结束值，并在结束值上停一帧（方向翻转那一帧不改变数值）
        self.assertEqual(values[15], 2.0)
        self.assertEqual(values[16], 2.0)
        self.assertAlmostEqual(values[17], 1.8667, places=4)
        self.assertAlmostEqual(values[18], 1.7334, places=4)
        # 回到起点后同样停一帧，然后播完置暂停
        zero_index = next(index for index, value in enumerate(values) if index > 18 and value == 0.0)
        self.assertEqual(values[zero_index + 1], 0.0)
        self.assertEqual(result["variables"]["$paused"][zero_index + 1], 0.0)

    def test_loop_playback_keeps_ping_ponging(self):
        tree = self._build(loop_playback=True)
        result = preview.simulate_anim_driver_paragraphs(_collect_paragraphs(tree), 60, 30)
        values = result["variables"]["$varA"]
        self.assertAlmostEqual(values[17], 1.8667, places=4)
        self.assertEqual(result["variables"]["$paused"][50], 1.0)


class RandomPreviewTests(unittest.TestCase):
    @staticmethod
    def _reference_lcg(seed, steps, minimum, span):
        value = seed
        result = []
        for _ in range(steps):
            value = (4001 * value + 12345) % 16777216
            result.append(minimum + (value / 16777216.0) * span)
        return result

    def _build(self, **overrides):
        node = random_module.SSMTNode_AnimDriver_Random()
        node.name = "Random"
        node.auto_index = 1
        node.seed = 13579
        node.min_value = 0.0
        node.max_value = 1.0
        node.default_paused = True
        node.custom_paused_var = "$random_paused"
        node.driven_variable_list = [types.SimpleNamespace(variable_name="$shape_x")]
        for key, value in overrides.items():
            setattr(node, key, value)
        return _make_tree([node])

    def test_random_sequence_matches_integer_lcg(self):
        tree = self._build()
        result = preview.simulate_anim_driver_paragraphs(_collect_paragraphs(tree), 12, 30)
        expected = self._reference_lcg(13579, 12, 0.0, 1.0)
        for actual, want in zip(result["variables"]["$shape_x"], expected):
            self.assertAlmostEqual(actual, want, places=6)

    def test_range_is_applied(self):
        tree = self._build(min_value=-2.0, max_value=3.0)
        result = preview.simulate_anim_driver_paragraphs(_collect_paragraphs(tree), 20, 30)
        expected = self._reference_lcg(13579, 20, -2.0, 5.0)
        for actual, want in zip(result["variables"]["$shape_x"], expected):
            self.assertAlmostEqual(actual, want, places=5)

    def test_multiple_targets_consume_one_lcg_step_each(self):
        tree = self._build(
            driven_variable_list=[
                types.SimpleNamespace(variable_name="$shape_a"),
                types.SimpleNamespace(variable_name="$shape_b"),
            ]
        )
        result = preview.simulate_anim_driver_paragraphs(_collect_paragraphs(tree), 3, 30)
        first = result["variables"]["$shape_a"]
        second = result["variables"]["$shape_b"]
        self.assertNotEqual(first[0], second[0])
        expected = self._reference_lcg(13579, 6, 0.0, 1.0)
        self.assertAlmostEqual(first[0], expected[0], places=6)
        self.assertAlmostEqual(second[0], expected[1], places=6)
        self.assertAlmostEqual(first[1], expected[2], places=6)

    def test_paused_from_start_never_touches_target(self):
        tree = self._build(default_paused=False)
        result = preview.simulate_anim_driver_paragraphs(_collect_paragraphs(tree), 4, 30)
        # 一直暂停：目标变量从不被写（暂停中每帧写 0 会抹掉同一变量上的其它驱动）
        self.assertNotIn("$shape_x", result["variables"])

    def test_pause_edge_resets_target_once(self):
        tree = self._build(default_paused=True)
        paragraphs = _collect_paragraphs(tree)
        # 前置一段：驱动帧 2 时把暂停变量置 0，制造「播放 → 暂停」边沿
        paragraphs.insert(0, {
            "ini_content": (
                "[Constants]\n"
                "global persist $edge_frame = 0\n"
                "[Present]\n"
                "$edge_frame = (time * 30) // 1\n"
                "if $edge_frame == 2\n"
                "    $random_paused = 0\n"
                "endif\n"
            )
        })
        result = preview.simulate_anim_driver_paragraphs(paragraphs, 6, 30)
        values = result["variables"]["$shape_x"]
        self.assertNotEqual(values[0], 0.0)
        self.assertNotEqual(values[1], 0.0)
        # 进入暂停的那一帧复位一次，之后完全不再写
        self.assertEqual(values[2], 0.0)
        self.assertEqual(values[3:], [0.0, 0.0, 0.0])


class PreviewTargetTests(unittest.TestCase):
    def setUp(self):
        self._original_objects = bpy_stub.data.objects
        self._original_map = anim_driver_base.SSMTNode_AnimDriver_Base._find_parent_shapekey_variable_map

    def tearDown(self):
        bpy_stub.data.objects = self._original_objects
        anim_driver_base.SSMTNode_AnimDriver_Base._find_parent_shapekey_variable_map = self._original_map

    @staticmethod
    def _make_object(name, shape_key_names):
        key_blocks = types.SimpleNamespace(
            get=lambda key, _names=tuple(shape_key_names): (object() if key in _names else None)
        )
        return types.SimpleNamespace(
            name=name,
            type="MESH",
            data=types.SimpleNamespace(shape_keys=types.SimpleNamespace(key_blocks=key_blocks)),
        )

    def _install_objects(self, objects):
        bpy_stub.data.objects = _FakeObjectCollection(objects)

    def test_variable_with_shape_key_maps_to_key_block(self):
        body = self._make_object("Body", ["smile"])
        self._install_objects([body])
        anim_driver_base.SSMTNode_AnimDriver_Base._find_parent_shapekey_variable_map = staticmethod(
            lambda _tree: {"smile": "$Freq_smile"}
        )
        play = forward_play_module.SSMTNode_AnimDriver_ForwardPlay()
        play.name = "Play"
        play.mute = False
        play.use_continuous_shapekey_mode = False
        play.driven_variable = ""
        play.driven_variable_list = [types.SimpleNamespace(variable_name="$Freq_smile")]
        tree = _make_tree([play])

        targets = preview.collect_preview_targets(tree)
        self.assertEqual(len(targets), 1)
        self.assertEqual(targets[0]["kind"], "shapekey")
        self.assertEqual(targets[0]["object_name"], "Body")
        self.assertEqual(targets[0]["shape_key_name"], "smile")
        self.assertEqual(targets[0]["interpolation"], "LINEAR")

    def test_variable_without_shape_key_falls_back_to_scene_property(self):
        self._install_objects([])
        anim_driver_base.SSMTNode_AnimDriver_Base._find_parent_shapekey_variable_map = staticmethod(
            lambda _tree: {}
        )
        node = random_module.SSMTNode_AnimDriver_Random()
        node.name = "Random"
        node.mute = False
        node.driven_variable_list = [types.SimpleNamespace(variable_name="$shape_up")]
        tree = _make_tree([node])

        targets = preview.collect_preview_targets(tree)
        self.assertEqual(len(targets), 1)
        self.assertEqual(targets[0]["kind"], "scene_property")
        self.assertEqual(targets[0]["property_name"], "anim_preview_shape_up")
        self.assertEqual(targets[0]["interpolation"], "CONSTANT")

    def test_continuous_mode_uses_node_target_object(self):
        body = self._make_object("Body", ["a", "b"])
        other = self._make_object("Other", ["a"])
        self._install_objects([other, body])
        anim_driver_base.SSMTNode_AnimDriver_Base._find_parent_shapekey_variable_map = staticmethod(
            lambda _tree: {}
        )
        play = forward_play_module.SSMTNode_AnimDriver_ForwardPlay()
        play.name = "Play"
        play.mute = False
        play.use_continuous_shapekey_mode = True
        play.continuous_target_object = "Body"
        play.driven_variable_list = []
        play.continuous_shape_key_items = [
            types.SimpleNamespace(shape_key_name="a", variable_name="$Freq_a"),
            types.SimpleNamespace(shape_key_name="b", variable_name="$Freq_b"),
        ]
        tree = _make_tree([play])

        targets = preview.collect_preview_targets(tree)
        self.assertEqual(
            sorted((target["object_name"], target["shape_key_name"]) for target in targets),
            [("Body", "a"), ("Body", "b")],
        )

    def test_muted_nodes_are_skipped(self):
        self._install_objects([])
        anim_driver_base.SSMTNode_AnimDriver_Base._find_parent_shapekey_variable_map = staticmethod(
            lambda _tree: {}
        )
        node = random_module.SSMTNode_AnimDriver_Random()
        node.name = "Random"
        node.mute = True
        node.driven_variable_list = [types.SimpleNamespace(variable_name="$shape_up")]
        tree = _make_tree([node])
        self.assertEqual(preview.collect_preview_targets(tree), [])

    def test_unsupported_node_types_are_not_baked(self):
        self._install_objects([])
        anim_driver_base.SSMTNode_AnimDriver_Base._find_parent_shapekey_variable_map = staticmethod(
            lambda _tree: {}
        )
        node = runtime_module.SSMTNode_AnimDriver_Runtime()
        node.name = "Runtime"
        node.mute = False
        node.driven_variable_list = [types.SimpleNamespace(variable_name="$frame")]
        tree = _make_tree([node])
        self.assertEqual(preview.collect_preview_targets(tree), [])


class PreviewScopeTests(unittest.TestCase):
    """节点级作用域：一个节点的「生成」只写它自己驱动的变量。"""

    def setUp(self):
        self._original_objects = bpy_stub.data.objects
        self._original_map = anim_driver_base.SSMTNode_AnimDriver_Base._find_parent_shapekey_variable_map

    def tearDown(self):
        bpy_stub.data.objects = self._original_objects
        anim_driver_base.SSMTNode_AnimDriver_Base._find_parent_shapekey_variable_map = self._original_map

    @staticmethod
    def _play_node(name, variable, auto_index):
        node = forward_play_module.SSMTNode_AnimDriver_ForwardPlay()
        node.name = name
        node.auto_index = auto_index
        node.mute = False
        node.use_continuous_shapekey_mode = False
        node.driven_variable = ""
        node.driven_variable_list = [types.SimpleNamespace(variable_name=variable)]
        node.frame_start = 0.0
        node.frame_end = 4.0
        node.play_total_duration = 1.0
        node.use_float_interval = True
        node.default_paused = True
        node.custom_paused_var = f"$paused_{auto_index}"
        node.reverse_playback = False
        node.loop_playback = True
        node.hold_end_value = False
        return node

    def test_only_node_restricts_targets_to_that_node(self):
        bpy_stub.data.objects = _FakeObjectCollection([])
        anim_driver_base.SSMTNode_AnimDriver_Base._find_parent_shapekey_variable_map = staticmethod(
            lambda _tree: {}
        )
        first = self._play_node("PlayA", "$var_a", 1)
        second = self._play_node("PlayB", "$var_b", 2)
        random_node = random_module.SSMTNode_AnimDriver_Random()
        random_node.name = "Random"
        random_node.mute = False
        random_node.driven_variable_list = [types.SimpleNamespace(variable_name="$shape_up")]
        tree = _make_tree([first, second, random_node])

        self.assertEqual(
            [target["variable"] for target in preview.collect_preview_targets(tree)],
            ["$var_a", "$var_b", "$shape_up"],
        )
        self.assertEqual(
            [target["variable"] for target in preview.collect_preview_targets(tree, only_node=second)],
            ["$var_b"],
        )
        self.assertEqual(
            [target["variable"] for target in preview.collect_preview_targets(tree, only_node=random_node)],
            ["$shape_up"],
        )

    def test_only_node_ignores_muted_and_unsupported(self):
        bpy_stub.data.objects = _FakeObjectCollection([])
        anim_driver_base.SSMTNode_AnimDriver_Base._find_parent_shapekey_variable_map = staticmethod(
            lambda _tree: {}
        )
        muted = self._play_node("Muted", "$var_a", 1)
        muted.mute = True
        tree = _make_tree([muted])
        self.assertEqual(preview.collect_preview_targets(tree, only_node=muted), [])

    def test_target_data_path_identifies_owner_curves(self):
        self.assertEqual(
            preview.target_data_path({
                "kind": "shapekey", "shape_key_name": "smile", "object_name": "Body",
            }),
            'key_blocks["smile"].value',
        )
        self.assertEqual(
            preview.target_data_path({"kind": "scene_property", "property_name": "anim_preview_x"}),
            '["anim_preview_x"]',
        )

    def test_group_targets_by_datablock(self):
        targets = [
            {"kind": "shapekey", "object_name": "Body", "shape_key_name": "a"},
            {"kind": "shapekey", "object_name": "Body", "shape_key_name": "b"},
            {"kind": "shapekey", "object_name": "Other", "shape_key_name": "a"},
            {"kind": "scene_property", "property_name": "anim_preview_x"},
        ]
        groups = preview.group_targets_by_datablock(targets)
        self.assertEqual(
            sorted(groups.keys()),
            [("scene",), ("shapekey", "Body"), ("shapekey", "Other")],
        )
        self.assertEqual(len(groups[("shapekey", "Body")]), 2)

    def test_bake_scope_only_targets_the_given_node(self):
        bpy_stub.data.objects = _FakeObjectCollection([])
        anim_driver_base.SSMTNode_AnimDriver_Base._find_parent_shapekey_variable_map = staticmethod(
            lambda _tree: {}
        )
        runtime = _make_runtime()
        play = self._play_node("PlayA", "$var_a", 2)
        other = self._play_node("PlayB", "$var_b", 3)
        tree = _make_tree([runtime, play, other], [_FakeLink(runtime, play)])
        result = preview.bake_anim_driver_preview(tree, node=play, frame_count=5)

        # 同一蓝图里还有另一个受支持节点，但只解释/只写当前节点的变量
        self.assertEqual([target["variable"] for target in result["targets"]], ["$var_a"])
        self.assertEqual(result["simulation"]["frame_count"], 5)
        self.assertIn("$var_a", result["simulation"]["variables"])
        # 单元测试桩没有场景上下文，写入本身由 Blender 冒烟测试覆盖
        self.assertEqual(result["skipped"], ["$var_a（当前上下文没有场景）"])

    def test_clear_without_node_reports_full_scope(self):
        result = preview.clear_anim_driver_preview(tree=_make_tree([]))
        self.assertEqual(result["removed_actions"], [])
        self.assertEqual(result["removed_curves"], 0)
        self.assertIn("没有找到关键帧预览数据", result["message"])


class PreviewRangeTests(unittest.TestCase):
    def test_frame_count_is_clamped_to_three_hundred(self):
        self.assertEqual(preview.ANIM_DRIVER_PREVIEW_MAX_FRAMES, 300)
        self.assertEqual(preview.clamp_preview_frame_count(1000), 300)
        self.assertEqual(preview.clamp_preview_frame_count(300), 300)
        self.assertEqual(preview.clamp_preview_frame_count(0), 1)
        self.assertEqual(preview.clamp_preview_frame_count(None), 300)
        self.assertEqual(preview.clamp_preview_frame_count("12"), 12)

    def test_fps_falls_back_to_thirty_without_runtime_node(self):
        self.assertEqual(preview.resolve_preview_fps(_make_tree([])), 30)
        runtime = _make_runtime(fps=60)
        self.assertEqual(preview.resolve_preview_fps(_make_tree([runtime])), 60)

    def test_bake_without_paragraphs_reports_failure(self):
        tree = _make_tree([])
        result = preview.bake_anim_driver_preview(tree, frame_count=10)
        self.assertFalse(result["ok"])
        self.assertIn("没有可预览的驱动段落", result["message"])


if __name__ == "__main__":
    unittest.main()
