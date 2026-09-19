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


PKG = "_anim_driver_click_export_test_pkg"
for package_name in (PKG, f"{PKG}.blueprint"):
    package = _install_module(package_name)
    package.__path__ = []


class _NodeGroups(list):
    def get(self, name, default=None):
        return next((group for group in self if getattr(group, "name", "") == name), default)


_bpy_props = _install_module(
    "bpy.props",
    IntProperty=lambda **_kwargs: None,
    StringProperty=lambda **_kwargs: None,
    CollectionProperty=lambda **_kwargs: None,
)
_bpy_types = _install_module(
    "bpy.types",
    PropertyGroup=object,
    UIList=object,
    Operator=object,
)
_fake_bpy = _install_module(
    "bpy",
    props=_bpy_props,
    types=_bpy_types,
    data=types.SimpleNamespace(node_groups=_NodeGroups()),
    utils=types.SimpleNamespace(register_class=lambda _cls: None, unregister_class=lambda _cls: None),
)


class _FakeAnimBase:
    bl_idname = "SSMTNode_AnimDriver_Base"


_install_module(
    f"{PKG}.blueprint.anim_driver_base",
    ANIM_DRIVER_INPUT_SOCKET_NAME="链输入",
    ANIM_DRIVER_OUTPUT_SOCKET_NAME="链输出",
    SSMTNode_AnimDriver_Base=_FakeAnimBase,
    SSMTSocketAnimDriver=object,
)
_install_module(
    f"{PKG}.blueprint.node_postprocess_draginteraction",
    DEFAULT_MOD_NAMESPACE="A",
    MAX_ZONES=256,
    is_postprocess_node_on_export_chain=lambda _tree, node: getattr(
        node, "on_export_chain", True
    ),
)
_install_module(
    f"{PKG}.blueprint.variable_registry",
    normalize_variable_name=lambda value: str(value or "").strip().lstrip("$"),
    build_shape_key_reference_alias_map=lambda context=None: {},
    rewrite_reference_variables_in_text=lambda text, alias_map=None: text,
)


module_path = Path(__file__).resolve().parents[1] / "blueprint" / "anim_driver_click_export.py"
spec = importlib.util.spec_from_file_location(f"{PKG}.blueprint.anim_driver_click_export", module_path)
click_module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = click_module
spec.loader.exec_module(click_module)

collector_path = Path(__file__).resolve().parents[1] / "blueprint" / "anim_driver_collector.py"
collector_spec = importlib.util.spec_from_file_location(
    f"{PKG}.blueprint.anim_driver_collector", collector_path
)
collector_module = importlib.util.module_from_spec(collector_spec)
sys.modules[collector_spec.name] = collector_module
collector_spec.loader.exec_module(collector_module)


class _FakeTree:
    def __init__(self, name, nodes=(), *, animation_driver=False):
        self.name = name
        self.bl_idname = "SSMTBlueprintTreeType"
        self.nodes = list(nodes)
        self.links = []
        self._properties = {"is_animation_driver": animation_driver}

    def get(self, key, default=None):
        return self._properties.get(key, default)


def _drag_node(namespace="A"):
    return types.SimpleNamespace(
        bl_idname="SSMTNode_PostProcess_DragInteraction",
        mute=False,
        enable_shapekey_drive=True,
        _resolve_namespace=lambda _ini_path: namespace,
    )


def _owner_tree(anim_name, drag_nodes):
    postprocess = types.SimpleNamespace(
        bl_idname="SSMTNode_PostProcess_AnimDriver",
        mute=False,
        on_export_chain=True,
        blueprint_name=anim_name,
    )
    return _FakeTree("Owner", [postprocess, *drag_nodes])


def _click_node(targets=("$Swap",), zone=2, *, owners=1, namespace="A", values=""):
    anim_tree = _FakeTree("AnimTree", animation_driver=True)
    owners_list = [_owner_tree(anim_tree.name, [_drag_node(namespace)]) for _ in range(owners)]
    _fake_bpy.data.node_groups = _NodeGroups([anim_tree, *owners_list])

    node = click_module.SSMTNode_AnimDriver_ClickExport.__new__(
        click_module.SSMTNode_AnimDriver_ClickExport
    )
    node.id_data = anim_tree
    node.name = "Click Export"
    node.mute = False
    node.click_zone_id = zone
    node.cycle_length = 0
    node.click_values = values
    node.click_target_list = [types.SimpleNamespace(variable_name=value) for value in targets]
    return node


class ClickExportTests(unittest.TestCase):
    def test_collector_exports_click_driver_as_present_only_reference_segment(self):
        node = _click_node(targets=("$Swap", "$Other"), zone=6)
        node.id_data.nodes.append(node)

        paragraphs = collector_module.AnimationDriverCollector(node.id_data).collect()

        self.assertEqual(len(paragraphs), 1)
        content = paragraphs[0]["ini_content"]
        self.assertEqual(content.count("[Present]"), 1)
        # 值仲裁、变量为主：受控变量本身不重新声明，只声明每绑定的 ckprev 辅助变量
        self.assertEqual(content.count("[Constants]"), 1)
        self.assertNotIn("global $Swap", content)
        self.assertIn("global $ssmtdrag_ckprev_A_Swap = 0", content)
        self.assertIn("global $ssmtdrag_ckprev_A_Other = 0", content)
        self.assertIn("store = $Swap, ResourceDragShapeKeyClickCountF_A, 6", content)
        self.assertIn("store = $Other, ResourceDragShapeKeyClickCountF_A, 6", content)
        self.assertIn("$ssmtdrag_seed_pending_A = 1", content)

    def test_generate_ini_segment_references_owned_variables_without_redeclaring_them(self):
        node = _click_node(targets=("$Swap", "Swap", "", "$Other"), zone=7)

        content = node.generate_ini_segment()

        self.assertNotIn("global $Swap", content)
        self.assertNotIn("global $Other", content)
        self.assertEqual(content.count("store = $Swap,"), 1)
        self.assertIn("if $ssmtdrag_booted_A == 1 && $ssmtdrag_seed_pending_A == 0", content)
        self.assertIn("store = $Swap, ResourceDragShapeKeyClickCountF_A, 7", content)
        self.assertIn("store = $Other, ResourceDragShapeKeyClickCountF_A, 7", content)

    def test_generate_ini_segment_arbitrates_variable_first(self):
        """回归：点击计数导出必须做值仲裁——变量变化(热键)时置 seed_pending
        触发播种推回缓冲且不回读；变量未变才拉取缓冲。旧实现每帧无条件
        store 会把快捷键切换下一瞬间顶掉。"""
        node = _click_node(targets=("$Swap",), zone=7)

        content = node.generate_ini_segment()

        self.assertIn("if $Swap != $ssmtdrag_ckprev_A_Swap", content)
        self.assertIn("$ssmtdrag_ckprev_A_Swap = $Swap", content)
        self.assertIn("$ssmtdrag_seed_pending_A = 1", content)
        self.assertIn("else", content)
        self.assertIn("\t\tstore = $Swap, ResourceDragShapeKeyClickCountF_A, 7", content)
        # store 只在变量未变分支里出现
        self.assertEqual(content.count("store = $Swap,"), 1)

    def test_generate_ini_segment_fails_closed_when_animation_tree_has_multiple_drag_owners(self):
        node = _click_node(owners=2)

        self.assertEqual(len(node._find_drag_drive_nodes()), 2)
        self.assertIsNone(node._find_drag_drive_node())
        self.assertEqual(node.generate_ini_segment(), "")

    def test_muted_postprocess_owner_does_not_activate_click_export(self):
        node = _click_node()
        owner = next(tree for tree in _fake_bpy.data.node_groups if tree.name == "Owner")
        owner.nodes[0].mute = True

        self.assertEqual(node._find_anim_owner_trees(), [])
        self.assertEqual(node.generate_ini_segment(), "")

    def test_disconnected_postprocess_owner_does_not_activate_click_export(self):
        node = _click_node()
        owner = next(tree for tree in _fake_bpy.data.node_groups if tree.name == "Owner")
        owner.nodes[0].on_export_chain = False

        self.assertEqual(node._find_anim_owner_trees(), [])
        self.assertEqual(node.generate_ini_segment(), "")

    def test_muted_drag_node_does_not_activate_click_export(self):
        node = _click_node()
        owner = next(tree for tree in _fake_bpy.data.node_groups if tree.name == "Owner")
        owner.nodes[1].mute = True

        self.assertEqual(node._find_drag_drive_nodes(), [])
        self.assertEqual(node.generate_ini_segment(), "")

    def test_disconnected_drag_node_does_not_create_a_second_owner(self):
        node = _click_node()
        owner = next(tree for tree in _fake_bpy.data.node_groups if tree.name == "Owner")
        disconnected_drag = _drag_node("Unused")
        disconnected_drag.on_export_chain = False
        owner.nodes.append(disconnected_drag)

        self.assertEqual(node._find_drag_drive_nodes(), [owner.nodes[1]])
        self.assertIn("ResourceDragShapeKeyClickCountF_A", node.generate_ini_segment())

    def test_generate_ini_segment_rejects_runtime_zone_outside_stable_capacity(self):
        node = _click_node(zone=256)

        self.assertEqual(node.generate_ini_segment(), "")

    def test_generate_ini_segment_accepts_last_stable_zone(self):
        node = _click_node(zone=255)

        content = node.generate_ini_segment()

        self.assertIn("ResourceDragShapeKeyClickCountF_A, 255", content)

    def test_compute_cycle_from_swaps_uses_maximum_matching_active_option_count(self):
        node = _click_node(targets=("$Swap",))
        active = types.SimpleNamespace(
            bl_idname="SSMTNode_ObjectSwap",
            mute=False,
            custom_var_name="",
            assigned_variable_name="$Swap",
            input_slot_count=5,
        )
        muted = types.SimpleNamespace(
            bl_idname="SSMTNode_ObjectSwap",
            mute=True,
            custom_var_name="$Swap",
            assigned_variable_name="",
            input_slot_count=99,
        )
        _fake_bpy.data.node_groups.append(_FakeTree("SwapTree", [active, muted]))

        self.assertEqual(node._compute_cycle_from_swaps(), (5, 1))

    def test_compute_cycle_from_multiple_targets_uses_global_maximum_by_design(self):
        node = _click_node(targets=("$Hair", "$Outfit"))
        hair = types.SimpleNamespace(
            bl_idname="SSMTNode_ObjectSwap", mute=False,
            custom_var_name="$Hair", assigned_variable_name="", input_slot_count=3,
        )
        outfit = types.SimpleNamespace(
            bl_idname="SSMTNode_ObjectSwap", mute=False,
            custom_var_name="", assigned_variable_name="$Outfit", input_slot_count=7,
        )
        _fake_bpy.data.node_groups.extend([
            _FakeTree("HairBlueprint", [hair]),
            _FakeTree("OutfitBlueprint", [outfit]),
        ])

        self.assertEqual(node._compute_cycle_from_swaps(), (7, 2))


class ClickValueSequenceTests(unittest.TestCase):
    """开关值（每次点击依次赋值的列表）：解析、有效循环档数、INI 段语义。"""

    def test_parse_accepts_space_comma_and_compact_digit_forms(self):
        self.assertEqual(click_module.parse_click_values("0 0 1"), ["0", "0", "1"])
        self.assertEqual(click_module.parse_click_values("0,0,1"), ["0", "0", "1"])
        # 连续数字写法按位拆分（单项循环无意义），带分隔的多位数不拆
        self.assertEqual(click_module.parse_click_values("001"), ["0", "0", "1"])
        self.assertEqual(click_module.parse_click_values("0 10"), ["0", "10"])
        self.assertEqual(click_module.parse_click_values(""), [])
        self.assertEqual(click_module.parse_click_values("   "), [])

    def test_parse_drops_non_numeric_tokens_and_caps_length(self):
        self.assertEqual(click_module.parse_click_values("0 x 1"), ["0", "1"])
        self.assertEqual(click_module.parse_click_values("0.5 1"), ["0.5", "1"])
        capped = click_module.parse_click_values(" ".join(str(i) for i in range(100)))
        self.assertEqual(len(capped), click_module.MAX_CLICK_VALUE_COUNT)

    def test_effective_cycle_length_prefers_value_list_length(self):
        node = _click_node(targets=("$Swap",))
        node.cycle_length = 7
        self.assertEqual(node.effective_cycle_length(), 7)

        node.click_values = "0 0 1"
        self.assertEqual(node.effective_cycle_length(), 3)

    def test_generate_ini_segment_cycles_switch_values_per_click(self):
        """用户口径：点第一下写第 1 项、第二下第 2 项……点完一轮回第一项；
        下标预置末项使首次点击落到第 0 项，未点击时写首项。"""
        node = _click_node(targets=("$Swap",), zone=5, values="001")

        content = node.generate_ini_segment()

        self.assertEqual(content.count("[Constants]"), 1)
        # 下标初值 = 列表末项（首次点击推进到第 0 项）
        self.assertIn("global $ssmtdrag_ckidx_A_Click_Export_5 = 2", content)
        # 当前开关值初值 = 列表首项（未点击即写列表第一项）
        self.assertIn("global $ssmtdrag_ckval_A_Click_Export_5 = 0", content)
        # 首个读数只作基线（防 boot 播种/缓冲残留被当成点击）；点击沿 = 计数变化
        self.assertIn("global $ssmtdrag_ckinit_A_Click_Export_5 = 0", content)
        self.assertIn("\tif $ssmtdrag_ckinit_A_Click_Export_5 == 0", content)
        self.assertIn("\t\t$ssmtdrag_cklast_A_Click_Export_5 = $ssmtdrag_ckread_A_Click_Export_5", content)
        self.assertIn(
            "\telif $ssmtdrag_ckread_A_Click_Export_5 != $ssmtdrag_cklast_A_Click_Export_5",
            content,
        )
        self.assertIn("\t\t$ssmtdrag_ckidx_A_Click_Export_5 = $ssmtdrag_ckidx_A_Click_Export_5 + 1", content)
        self.assertIn("\t\tif $ssmtdrag_ckidx_A_Click_Export_5 >= 3", content)
        self.assertIn("\t\t\t$ssmtdrag_ckidx_A_Click_Export_5 = 0", content)
        # 下标 → 值：0→0、1→0、2→1
        self.assertIn("\t\tif $ssmtdrag_ckidx_A_Click_Export_5 == 0\n\t\t\t$ssmtdrag_ckval_A_Click_Export_5 = 0", content)
        self.assertIn("\t\telif $ssmtdrag_ckidx_A_Click_Export_5 == 1\n\t\t\t$ssmtdrag_ckval_A_Click_Export_5 = 0", content)
        self.assertIn("\t\telif $ssmtdrag_ckidx_A_Click_Export_5 == 2\n\t\t\t$ssmtdrag_ckval_A_Click_Export_5 = 1", content)
        # 受控变量取值来自映射结果
        self.assertIn("\t\t$Swap = $ssmtdrag_ckval_A_Click_Export_5", content)
        # 读回点击计数（store 目标 = 本帧计数辅助变量，不再直写受控变量）
        self.assertIn("store = $ssmtdrag_ckread_A_Click_Export_5, ResourceDragShapeKeyClickCountF_A, 5", content)
        self.assertNotIn("store = $Swap,", content)

    def test_switch_value_target_is_written_on_click_edge_only(self):
        """回归：受控变量只在**点击推进沿**写一次，不得每帧持续强写。

        实机现象：`$animation_paused10` / `$animation_paused11` 这两个动画播放异常 ——
        它们同时是「自动动画的播放状态」和本导出节点的受控变量。旧实现每帧走
        `if var != prev … else: var = ckval` 的 else 分支，把播放状态按回列表首项
        （每帧置 0），表现为该动画只播一帧就被按停。现在改为：
        点击推进的那一帧写一次，其余帧完全不碰受控变量。
        """
        node = _click_node(targets=("$animation_paused10",), zone=3, values="0 1 0")

        content = node.generate_ini_segment()

        # 有独立的点击沿标志
        self.assertIn("global $ssmtdrag_ckedge_A_Click_Export_3 = 0", content)
        # 只有计数变化（点击推进）时置 1
        self.assertIn("\t\t$ssmtdrag_ckedge_A_Click_Export_3 = 1", content)
        self.assertIn("\t$ssmtdrag_ckedge_A_Click_Export_3 = 0", content)
        # **位置**：沿标志必须在"计数回绕 if"的 endif **之后**、ckval 映射之前。
        # 插进回绕 if 内部的话只在回绕那一帧置位（该帧 ckval 恒 0）→ 点击完全失效。
        self.assertIn(
            "\t\tif $ssmtdrag_ckidx_A_Click_Export_3 >= 3\n"
            "\t\t\t$ssmtdrag_ckidx_A_Click_Export_3 = 0\n"
            "\t\tendif\n"
            "\t\t$ssmtdrag_ckedge_A_Click_Export_3 = 1\n"
            "\t\tif $ssmtdrag_ckidx_A_Click_Export_3 == 0",
            content,
        )
        # 受控变量：沿上写一次；否则仅在外部改动时重建基线（不写变量本身）
        self.assertIn(
            "\tif $ssmtdrag_ckedge_A_Click_Export_3 == 1\n"
            "\t\t$animation_paused10 = $ssmtdrag_ckval_A_Click_Export_3\n"
            "\t\t$ssmtdrag_ckprev_A_animation_paused10 = $animation_paused10\n"
            "\telif $animation_paused10 != $ssmtdrag_ckprev_A_animation_paused10\n"
            "\t\t$ssmtdrag_ckprev_A_animation_paused10 = $animation_paused10\n"
            "\tendif",
            content,
        )
        # 旧形态（每帧 else 强写）必须消失
        self.assertNotIn(
            "\telse\n\t\t$animation_paused10 = $ssmtdrag_ckval_A_Click_Export_3", content
        )

    def test_generate_ini_segment_switch_values_do_not_seed_click_count(self):
        """列表值 → 点击计数不可逆（允许重复项），故不置 seed_pending、
        不做变量→缓冲播种；变量外部改动时只更新 prev 并让点击继续。"""
        node = _click_node(targets=("$Swap",), values="0 0 1")

        content = node.generate_ini_segment()

        self.assertNotIn("$ssmtdrag_seed_pending_A = 1", content)
        self.assertIn("if $Swap != $ssmtdrag_ckprev_A_Swap", content)
        self.assertIn("\t\t$ssmtdrag_ckprev_A_Swap = $Swap", content)

    def test_booted_gate_drops_seed_pending_when_drag_node_will_not_declare_it(self):
        """拖拽节点不会声明 seed_pending 时（「开关值」模式无播种条目），门控必须
        去掉该项——不声明却引用会退化成 3DMigoto 段内局部变量（跨段失效）。"""
        node = _click_node(targets=("$Swap",), values="0 0 1")
        drag = node._find_drag_drive_node()
        drag._click_export_seed_variable_declared = lambda: False

        content = node.generate_ini_segment()

        self.assertIn("if $ssmtdrag_booted_A == 1", content)
        self.assertNotIn("$ssmtdrag_seed_pending_A", content)

    def test_booted_gate_keeps_seed_pending_when_declared(self):
        """对照组：拖拽节点会声明（存在播种条目）时门控保留 seed_pending 项。"""
        node = _click_node(targets=("$Swap",), values="0 0 1")
        drag = node._find_drag_drive_node()
        drag._click_export_seed_variable_declared = lambda: True

        content = node.generate_ini_segment()

        self.assertIn(
            "if $ssmtdrag_booted_A == 1 && $ssmtdrag_seed_pending_A == 0", content)

    def test_booted_gate_defaults_to_legacy_when_predicate_missing(self):
        """旧节点/测试桩没有该谓词时保守保留（行为与旧版一致）。"""
        node = _click_node(targets=("$Swap",), values="0 0 1")

        content = node.generate_ini_segment()

        self.assertIn(
            "if $ssmtdrag_booted_A == 1 && $ssmtdrag_seed_pending_A == 0", content)

    # ------------------------------------------------------------------
    # 回读最小化：点击计数只在按住期间会变 → 只在按住/松开沿/首帧建基线时 store
    # ------------------------------------------------------------------

    @staticmethod
    def _enable_trigger_gate(node):
        drag = node._find_drag_drive_node()
        drag._click_export_trigger_vars = lambda ns: [
            f"$ssmtdrag_lmb_down_{ns}", f"$ssmtdrag_x_down_{ns}",
        ]
        return drag

    def test_switch_value_store_is_gated_on_trigger_hold(self):
        node = _click_node(targets=("$Swap",), zone=5, values="001")
        self._enable_trigger_gate(node)

        content = node.generate_ini_segment()
        lines = content.splitlines()

        # 按住标志 + 回读门控（按住 / 上一帧按着 / 首帧建基线）
        self.assertIn("global $ssmtdrag_ckheld_A_Click_Export_5 = 0", content)
        self.assertIn("global $ssmtdrag_ckheldprev_A_Click_Export_5 = 0", content)
        self.assertIn("if $ssmtdrag_lmb_down_A == 1 || $ssmtdrag_x_down_A == 1", content)
        gate = ("if $ssmtdrag_ckheld_A_Click_Export_5 == 1 || "
                "$ssmtdrag_ckheldprev_A_Click_Export_5 == 1 || "
                "$ssmtdrag_ckinit_A_Click_Export_5 == 0")
        self.assertIn(gate, content)
        # store 落在门控内（缩进两层）
        self.assertIn("\t\tstore = $ssmtdrag_ckread_A_Click_Export_5, "
                      "ResourceDragShapeKeyClickCountF_A, 5", content)
        # prev 必须在"读取门控之后"才更新，否则松开沿判定恒假
        gate_idx = next(i for i, l in enumerate(lines) if gate in l)
        store_idx = next(
            i for i, l in enumerate(lines)
            if "store = $ssmtdrag_ckread_A_Click_Export_5" in l)
        prev_idx = next(
            i for i, l in enumerate(lines)
            if l.strip() == ("$ssmtdrag_ckheldprev_A_Click_Export_5 = "
                             "$ssmtdrag_ckheld_A_Click_Export_5"))
        self.assertLess(gate_idx, store_idx)
        self.assertLess(store_idx, prev_idx)
        # 受控变量仲裁是纯 CPU 赋值 → 仍每帧执行（门控外，单层缩进）
        self.assertIn("\t\t$Swap = $ssmtdrag_ckval_A_Click_Export_5", content)

    def test_switch_value_store_ungated_without_trigger_vars(self):
        """取不到按住变量（旧节点/EFMI）时不做门控，保持旧行为。"""
        node = _click_node(targets=("$Swap",), zone=5, values="001")

        content = node.generate_ini_segment()

        self.assertIn("\tstore = $ssmtdrag_ckread_A_Click_Export_5, "
                      "ResourceDragShapeKeyClickCountF_A, 5", content)
        self.assertNotIn("$ssmtdrag_ckheld_A", content)

    def test_legacy_count_store_is_gated_on_trigger_hold(self):
        node = _click_node(targets=("$Swap",), values="")
        self._enable_trigger_gate(node)

        content = node.generate_ini_segment()

        self.assertIn("global $ssmtdrag_ckheld_A_2 = 0", content)
        self.assertIn("global $ssmtdrag_ckheldprev_A_2 = 0", content)
        self.assertIn(
            "if $ssmtdrag_ckheld_A_2 == 1 || $ssmtdrag_ckheldprev_A_2 == 1", content)
        # store 被包进按住门控（三层缩进）
        self.assertIn("\t\t\tstore = $Swap, ResourceDragShapeKeyClickCountF_A, 2", content)
        # 变量→缓冲的播种分支不受影响
        self.assertIn("$ssmtdrag_seed_pending_A = 1", content)

    def test_generate_ini_segment_without_values_keeps_legacy_count_readback(self):
        node = _click_node(targets=("$Swap",), values="")

        content = node.generate_ini_segment()

        self.assertIn("\t\tstore = $Swap, ResourceDragShapeKeyClickCountF_A, 2", content)
        self.assertIn("$ssmtdrag_seed_pending_A = 1", content)
        self.assertNotIn("ssmtdrag_ckidx", content)

    def test_single_value_list_is_a_no_op_cycle(self):
        node = _click_node(targets=("$Swap",), values="1")

        content = node.generate_ini_segment()

        self.assertEqual(node.effective_cycle_length(), 1)
        self.assertIn("global $ssmtdrag_ckidx_A_Click_Export_2 = 0", content)
        self.assertIn("\t\tif $ssmtdrag_ckidx_A_Click_Export_2 >= 1", content)
        self.assertIn("\t\t\t$ssmtdrag_ckval_A_Click_Export_2 = 1", content)


if __name__ == "__main__":
    unittest.main()
