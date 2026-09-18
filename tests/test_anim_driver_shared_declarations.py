"""驱动块内重复 global 声明的归一（同值合并 / 异值 _N 分叉）回归测试。

用户口径：驱动蓝图里同一变量被多个节点重复声明——
  * 初值相同 → 合并（只留一条）；
  * 初值不同 → 后者分叉成 $name_1 / $name_2，并把该段内引用一并改名，
    防止"一个变量被赋予多个不同数值"。
"""
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


PKG = "_anim_driver_shared_decl_test_pkg"
for package_name in (PKG, f"{PKG}.blueprint"):
    package = _install_module(package_name)
    package.__path__ = []

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
    data=types.SimpleNamespace(node_groups=[]),
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
    f"{PKG}.blueprint.variable_registry",
    normalize_variable_name=lambda value: str(value or "").strip().lstrip("$"),
    build_shape_key_reference_alias_map=lambda context=None: {},
    rewrite_reference_variables_in_text=lambda text, alias_map=None: text,
)

_collector_path = Path(__file__).resolve().parents[1] / "blueprint" / "anim_driver_collector.py"
_spec = importlib.util.spec_from_file_location(f"{PKG}.blueprint.anim_driver_collector", _collector_path)
collector_module = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = collector_module
_spec.loader.exec_module(collector_module)


def _runtime_segment(fps, frame_var="swapvar1", alias_owner=False):
    """与生产实现（anim_driver_runtime.generate_ini_segment）同形。

    每个运行时间节点只声明**自己的**预分配帧变量；``auto_index`` 最小的那个
    额外维护 ``$swapvar`` / ``$fps`` 兼容别名（只一份，不再重复声明）。
    """
    lines = [
        "[Constants]",
        f"global persist ${frame_var} = 0",
        "; 当前帧索引（整数）",
    ]
    if alias_owner:
        lines.extend([
            "global persist $swapvar = 0",
            f"global persist $fps = {fps}",
            "; 兼容别名（手写 $swapvar / $fps 仍可用）",
        ])
    lines.extend([
        "[Present]",
        "; 基于系统时间的自动计算（每帧执行）",
        f"${frame_var} = (time * {fps}) // 1",
    ])
    if alias_owner:
        lines.append(f"$swapvar = ${frame_var}")
    return "\n".join(lines)


class _FakeRuntimeNode:
    """collector 需要节点可哈希（set/dict 键），SimpleNamespace 不行。"""

    bl_idname = "SSMTNode_AnimDriver_Runtime"

    def __init__(self, name, fps, playback_rate=1, auto_index=1):
        self.name = name
        self.mute = False
        self.fps = fps
        self.playback_rate = playback_rate
        self.auto_index = auto_index
        self.custom_frame_variable_name = ""
        self.assigned_frame_variable_name = ""
        self.id_data = None

    def frame_variable_name(self):
        return (
            str(self.custom_frame_variable_name or "").strip().lstrip("$")
            or str(self.assigned_frame_variable_name or "").strip().lstrip("$")
            or f"swapvar{self.auto_index}"
        )

    def is_compat_alias_owner(self):
        tree = self.id_data
        runtime_nodes = [
            node for node in (getattr(tree, "nodes", None) or [])
            if getattr(node, "bl_idname", "") == "SSMTNode_AnimDriver_Runtime"
        ] if tree else []
        if not runtime_nodes:
            return True
        owner = min(
            runtime_nodes,
            key=lambda node: (int(getattr(node, "auto_index", 0) or 0), str(getattr(node, "name", ""))),
        )
        return owner is self

    def generate_ini_segment(self, connected_nodes=None):
        return _runtime_segment(self.fps, self.frame_variable_name(), self.is_compat_alias_owner())


class _FakeToggleNode:
    """非运行时间驱动：段内容自带一份 $shared 声明（用于跨类型同值合并测试）。"""

    bl_idname = "SSMTNode_AnimDriver_Toggle"

    def __init__(self, name, shared_value, speed):
        self.name = name
        self.mute = False
        self.shared_value = shared_value
        self.speed = speed
        self.id_data = None

    def generate_ini_segment(self, connected_nodes=None):
        return (
            "[Constants]\n"
            f"global persist $shared = {self.shared_value}\n"
            f"global persist $speed_auto{self.speed} = 1\n"
            "[Present]\n"
            f"if $shared == {self.shared_value}\n"
            f"\t$swapvar = $swapvar + 0\n"
            "endif"
        )


def _runtime_node(name, fps, playback_rate=1):
    return _FakeRuntimeNode(name, fps, playback_rate)


def _toggle_node(name, shared_value, speed):
    return _FakeToggleNode(name, shared_value, speed)


def _collect(*nodes):
    tree = types.SimpleNamespace(
        name="动画驱动蓝图",
        bl_idname="SSMTBlueprintTreeType",
        nodes=list(nodes),
        links=[],
    )
    # 运行时间节点按出现顺序拿 auto_index（预分配帧变量名由它派生）
    runtime_index = 0
    for node in nodes:
        if getattr(node, "bl_idname", "") == "SSMTNode_AnimDriver_Runtime":
            runtime_index += 1
            node.auto_index = runtime_index
        node.id_data = tree
    collector = collector_module.AnimationDriverCollector(tree)
    return collector, collector.collect()


class NormalizeSharedDeclarationsUnitTests(unittest.TestCase):
    """直接对段落文本做归一，覆盖规则细节。"""

    def _normalize(self, *contents):
        paragraphs = [
            {"paragraph_index": i, "node_names": [f"n{i}"], "ini_content": text}
            for i, text in enumerate(contents)
        ]
        summary = collector_module.normalize_driver_shared_declarations(paragraphs)
        return [p["ini_content"] for p in paragraphs], summary

    def test_same_value_declarations_are_merged(self):
        out, summary = self._normalize(
            "[Constants]\nglobal persist $fps = 60\nglobal persist $swapvar = 0\n",
            "[Constants]\nglobal persist $fps = 60\nglobal persist $swapvar = 0\n",
        )

        self.assertEqual(out[0].count("global persist $fps"), 1)
        self.assertEqual(out[1].count("global persist $fps"), 0)
        self.assertEqual(out[1].count("global persist $swapvar"), 0)
        self.assertEqual(len(summary["merges"]), 2)
        self.assertEqual(summary["renames"], [])

    def test_numerically_equal_values_are_merged(self):
        out, summary = self._normalize(
            "[Constants]\nglobal persist $fps = 60\n",
            "[Constants]\nglobal persist $fps = 60.0\n",
        )

        self.assertEqual(out[1].count("global persist $fps"), 0)
        self.assertEqual(len(summary["merges"]), 1)

    def test_conflicting_value_forks_with_suffix_and_rewrites_paragraph_refs(self):
        out, summary = self._normalize(
            "[Constants]\nglobal persist $fps = 60\n[Present]\n$swapvar = (time * $fps) // 1\n",
            "[Constants]\nglobal persist $fps = 30\n[Present]\n$swapvar = (time * $fps) // 1\n",
        )

        # 第一段保持原名
        self.assertIn("global persist $fps = 60", out[0])
        self.assertIn("$swapvar = (time * $fps) // 1", out[0])
        # 第二段分叉 + 段内引用改名
        self.assertIn("global persist $fps_1 = 30", out[1])
        self.assertIn("$swapvar = (time * $fps_1) // 1", out[1])
        self.assertNotIn("$fps ", out[1])
        self.assertEqual(len(summary["renames"]), 1)
        self.assertEqual(summary["renames"][0]["new_name"], "$fps_1")

    def test_multiple_conflicts_get_increasing_suffixes(self):
        out, _summary = self._normalize(
            "[Constants]\nglobal $x = 1\n",
            "[Constants]\nglobal $x = 2\n",
            "[Constants]\nglobal $x = 3\n",
        )

        self.assertIn("global $x = 1", out[0])
        self.assertIn("global $x_1 = 2", out[1])
        self.assertIn("global $x_2 = 3", out[2])

    def test_valueless_duplicate_is_dropped_in_favour_of_valued_one(self):
        out, summary = self._normalize(
            "[Constants]\nglobal persist $Freq_a\n",
            "[Constants]\nglobal persist $Freq_a = 0.0\n",
        )

        self.assertEqual(out[0].count("$Freq_a"), 0)
        self.assertIn("global persist $Freq_a = 0.0", out[1])
        self.assertEqual(len(summary["merges"]), 1)

    def test_present_scope_global_is_not_treated_as_duplicate(self):
        """[Present] 里的 global $x = 0 是"每帧重置"惯用法，不能删。"""
        out, summary = self._normalize(
            "[Constants]\nglobal $x = 0\n",
            "[Present]\nglobal $x = 0\n",
        )

        self.assertIn("global $x = 0", out[0])
        self.assertIn("global $x = 0", out[1])
        self.assertEqual(summary["merges"], [])
        self.assertEqual(summary["renames"], [])

    def test_local_declarations_are_untouched(self):
        out, summary = self._normalize(
            "[Constants]\nlocal $tmp = 0\n",
            "[Constants]\nlocal $tmp = 1\n",
        )

        self.assertIn("local $tmp = 0", out[0])
        self.assertIn("local $tmp = 1", out[1])
        self.assertEqual(summary["merges"], [])
        self.assertEqual(summary["renames"], [])

    def test_shared_writes_with_different_values_are_reported_but_not_forked(self):
        """合并成一份声明、却被多个段落写入**不同值** → 只提示，不自动分叉。

        读它的其它段并不知道该读哪一个分叉名，自动改名会直接断链。
        """
        out, summary = self._normalize(
            "[Constants]\nglobal persist $swapvar = 0\n[Present]\n$swapvar = (time * $fps) // 1\n",
            "[Constants]\nglobal persist $swapvar = 0\n[Present]\n$swapvar = (time * $fps_1) // 1\n",
        )

        self.assertEqual(out[0].count("global persist $swapvar"), 1)
        self.assertEqual(out[1].count("global persist $swapvar"), 0)
        self.assertEqual(summary["renames"], [])
        self.assertEqual(
            [{"name": w["name"], "paragraphs": w["paragraphs"]} for w in summary["shared_writes"]],
            [{"name": "$swapvar", "paragraphs": [0, 1]}],
        )

    def test_shared_writes_with_identical_expression_are_silent(self):
        """各段写的是同一个右侧表达式 → 幂等重复，无歧义，不提示。"""
        _out, summary = self._normalize(
            "[Constants]\nglobal persist $swapvar = 0\n[Present]\n$swapvar = (time * $fps) // 1\n",
            "[Constants]\nglobal persist $swapvar = 0\n[Present]\n$swapvar = (time * $fps) // 1\n",
        )

        self.assertEqual(summary["shared_writes"], [])

    def test_single_writer_is_not_reported_as_shared(self):
        _out, summary = self._normalize(
            "[Constants]\nglobal persist $swapvar = 0\n[Present]\n$swapvar = (time * $fps) // 1\n",
            "[Constants]\nglobal persist $swapvar = 0\n[Present]\n$other = $swapvar\n",
        )

        self.assertEqual(summary["shared_writes"], [])

    def test_intra_paragraph_duplicate_is_merged(self):
        """同一段里两个节点各发一份 [Constants]（合并后同名段拼在一起）。"""
        out, summary = self._normalize(
            "[Constants]\nglobal persist $fps = 60\nglobal persist $swapvar = 0\n"
            "global persist $fps = 60\nglobal persist $swapvar = 0\n"
        )

        self.assertEqual(out[0].count("global persist $fps"), 1)
        self.assertEqual(out[0].count("global persist $swapvar"), 1)
        self.assertEqual(len(summary["merges"]), 2)


class RuntimeDriverDedupIntegrationTests(unittest.TestCase):
    """运行时间节点：段级去重 + 声明级归一 双保险。"""

    def test_each_runtime_node_declares_its_own_preallocated_variable(self):
        """预分配后：N 个运行时间节点各声明各的帧变量，不再产出 N 份同名声明。

        回归：旧设计硬编码共享的 $fps / $swapvar，N 个节点就 N 份同名声明
        （同值靠归并、异值靠分叉）。预分配后这些重复从源头消失。
        """
        collector, paragraphs = _collect(
            _runtime_node("运行时间", 60, playback_rate=1),
            _runtime_node("运行时间.001", 60, playback_rate=2),
            _runtime_node("运行时间.002", 60, playback_rate=5),
            _runtime_node("运行时间.003", 60, playback_rate=300),
        )

        joined = "\n".join(p["ini_content"] for p in paragraphs)
        for index in range(1, 5):
            self.assertEqual(joined.count(f"global persist $swapvar{index} = 0"), 1)
            self.assertEqual(joined.count(f"$swapvar{index} = (time * 60) // 1"), 1)
        # 兼容别名只有一份（auto_index 最小的那个节点发），手写 $swapvar/$fps 仍可用
        self.assertEqual(joined.count("global persist $swapvar = 0"), 1)
        self.assertEqual(joined.count("global persist $fps = 60"), 1)
        self.assertEqual(collector.last_normalization["renames"], [])

    def test_different_fps_runtime_nodes_do_not_conflict(self):
        """不同 fps 的运行时间节点各自持有独立帧变量 → 不再需要异值分叉。"""
        collector, paragraphs = _collect(
            _runtime_node("运行时间", 60),
            _runtime_node("运行时间.001", 30),
        )

        self.assertEqual(len(paragraphs), 2)
        first, second = paragraphs[0]["ini_content"], paragraphs[1]["ini_content"]
        self.assertIn("global persist $swapvar1 = 0", first)
        self.assertIn("$swapvar1 = (time * 60) // 1", first)
        self.assertIn("global persist $swapvar2 = 0", second)
        self.assertIn("$swapvar2 = (time * 30) // 1", second)
        self.assertEqual(collector.last_normalization["renames"], [])
        self.assertEqual(collector.last_normalization["merges"], [])

    def test_cross_type_same_value_declaration_is_merged(self):
        """不同驱动类型声明同名同值变量 → 合并为一条声明。"""
        collector, paragraphs = _collect(
            _toggle_node("开关A", 0, 1),
            _toggle_node("开关B", 0, 2),
        )

        joined = "\n".join(p["ini_content"] for p in paragraphs)
        self.assertEqual(joined.count("global persist $shared = 0"), 1)
        self.assertEqual(len(collector.last_normalization["merges"]), 1)
        self.assertEqual(collector.last_normalization["renames"], [])

    def test_cross_type_conflicting_value_is_forked_per_paragraph(self):
        """不同驱动类型声明同名异值变量 → 后者分叉，段内引用同步改名。"""
        collector, paragraphs = _collect(
            _toggle_node("开关A", 1, 1),
            _toggle_node("开关B", 0, 2),
        )

        first, second = paragraphs[0]["ini_content"], paragraphs[1]["ini_content"]
        self.assertIn("global persist $shared = 1", first)
        self.assertIn("if $shared == 1", first)
        self.assertIn("global persist $shared_1 = 0", second)
        self.assertIn("if $shared_1 == 0", second)
        self.assertEqual(len(collector.last_normalization["renames"]), 1)


if __name__ == "__main__":
    unittest.main()
