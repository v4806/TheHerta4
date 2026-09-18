# -*- coding: utf-8 -*-
"""文本追加后处理节点（blueprint/node_postprocess_text_append.py）测试。

覆盖：
- 节点结构约束：只有输入口、没有输出口（结构上必然是链尾）；
- 追加内容构造：追加在配置表最下方、可重复执行不堆叠、CRLF 保持、半块清理；
- 文本来源：节点自带文本块 / 引用工程文本块；
- 导出执行：只动导出目录根目录的配置表，空内容/无配置表时是安全 no-op；
- 与 ``node_postprocess_base`` 的块标记集成：追加块被识别为自动追加尾部。
"""
import importlib.util
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

PKG = "_text_append_test_pkg"
for _package_name in (PKG, f"{PKG}.blueprint", f"{PKG}.common", f"{PKG}.utils"):
    _package = types.ModuleType(_package_name)
    _package.__path__ = []
    sys.modules[_package_name] = _package


# ---------------------------------------------------------------------------
# 假 bpy / 文本块
# ---------------------------------------------------------------------------

class _FakeTextBlock:
    def __init__(self, name):
        self.name = name
        self._content = ""

    def as_string(self):
        return self._content

    def clear(self):
        self._content = ""

    def write(self, text):
        self._content += str(text)


class _FakeTexts:
    def __init__(self):
        self.blocks = {}

    def get(self, name):
        return self.blocks.get(str(name))

    def new(self, name):
        block = _FakeTextBlock(str(name))
        self.blocks[str(name)] = block
        return block

    def __contains__(self, name):
        return str(name) in self.blocks

    def __iter__(self):
        return iter(self.blocks.values())


class _FakeOperator:
    def report(self, _kind, _message):
        return None


class _FakeSockets:
    def __init__(self):
        self.items = []

    def new(self, idname, name):
        socket = types.SimpleNamespace(bl_idname=idname, name=name, is_linked=False)
        self.items.append(socket)
        return socket

    def __iter__(self):
        return iter(self.items)

    def __len__(self):
        return len(self.items)


class _FakeScreenOps:
    """bpy.ops.screen 替身：记录调用次数，具体行为由测试挂的钩子模拟。"""

    def __init__(self):
        self.split_calls = 0
        self.close_calls = 0
        self.on_split = None
        self.on_close = None

    def reset(self):
        self.split_calls = 0
        self.close_calls = 0
        self.on_split = None
        self.on_close = None

    def area_split(self, direction=None, factor=None):
        self.split_calls += 1
        if self.on_split is not None:
            self.on_split()

    def area_close(self):
        self.close_calls += 1
        if self.on_close is not None:
            self.on_close()


_fake_screen_ops = _FakeScreenOps()


def _build_fake_bpy():
    """搭一套最小可用的假 bpy（含 bpy.types/props/utils/data 子模块）。"""
    bpy_types = types.ModuleType("bpy.types")
    bpy_types.Operator = _FakeOperator
    bpy_types.Node = type("Node", (), {})
    bpy_types.NodeSocket = type("NodeSocket", (), {})

    bpy_props = types.ModuleType("bpy.props")
    bpy_props.StringProperty = lambda **kwargs: kwargs.get("default", "")
    bpy_props.EnumProperty = lambda **kwargs: kwargs.get("default", "")
    bpy_props.BoolProperty = lambda **kwargs: kwargs.get("default", False)

    bpy_utils = types.ModuleType("bpy.utils")
    bpy_utils.register_class = lambda _cls: None
    bpy_utils.unregister_class = lambda _cls: None

    bpy_data = types.ModuleType("bpy.data")
    bpy_data.texts = _FakeTexts()

    bpy_ops = types.ModuleType("bpy.ops")
    bpy_ops.screen = _fake_screen_ops

    bpy_module = types.ModuleType("bpy")
    bpy_module.types = bpy_types
    bpy_module.props = bpy_props
    bpy_module.utils = bpy_utils
    bpy_module.data = bpy_data
    bpy_module.ops = bpy_ops
    bpy_module.context = types.SimpleNamespace(window_manager=None)
    return bpy_module, {
        "bpy.types": bpy_types,
        "bpy.props": bpy_props,
        "bpy.utils": bpy_utils,
        "bpy.data": bpy_data,
        "bpy.ops": bpy_ops,
    }


_fake_bpy, _fake_bpy_submodules = _build_fake_bpy()


def _install_module(name, **attrs):
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


def _load_real_module(module_name, relative_path):
    spec = importlib.util.spec_from_file_location(module_name, REPO_ROOT / relative_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


class _FakeLog:
    def info(self, *_args, **_kwargs):
        return None

    def warning(self, *_args, **_kwargs):
        return None

    def error(self, *_args, **_kwargs):
        return None

    def debug(self, *_args, **_kwargs):
        return None


# 假 bpy 只在「加载被测模块」期间生效：加载完立刻还原，避免污染同进程的其它测试。
_SAVED_BPY_ENTRIES = {name: sys.modules.get(name) for name in _fake_bpy_submodules}
_SAVED_BPY = sys.modules.get("bpy")
sys.modules["bpy"] = _fake_bpy
sys.modules.update(_fake_bpy_submodules)

try:
    # node_base 只提供基类；node_postprocess_base 用真实实现（块标记/备份逻辑）。
    _install_module(f"{PKG}.blueprint.node_base", SSMTNodeBase=object)
    _install_module(f"{PKG}.utils.log_utils", LOG=_FakeLog())
    _install_module(f"{PKG}.utils.format_utils", Fatal=Exception)

    base_module = _load_real_module(
        f"{PKG}.blueprint.node_postprocess_base",
        Path("blueprint") / "node_postprocess_base.py",
    )
    text_width_module = _load_real_module(
        f"{PKG}.common.text_width_utils",
        Path("common") / "text_width_utils.py",
    )
    config_table_module = _load_real_module(
        f"{PKG}.common.config_table_backup",
        Path("common") / "config_table_backup.py",
    )
    module = _load_real_module(
        f"{PKG}.blueprint.node_postprocess_text_append",
        Path("blueprint") / "node_postprocess_text_append.py",
    )
finally:
    for _name, _previous in _SAVED_BPY_ENTRIES.items():
        if _previous is None:
            sys.modules.pop(_name, None)
        else:
            sys.modules[_name] = _previous
    if _SAVED_BPY is None:
        sys.modules.pop("bpy", None)
    else:
        sys.modules["bpy"] = _SAVED_BPY


def _make_node(text_source=None, content="", project_name=""):
    node = module.SSMTNode_PostProcess_TextAppend()
    node.name = "文本追加"
    node.id_data = types.SimpleNamespace(name="Blueprint")
    node.own_text_block_name = ""
    node.width = 420
    node.text_source = text_source or module.TEXT_SOURCE_NODE
    node.project_text_name = project_name
    if content:
        module._write_text_block(module._node_text_block_name(node), content)
    return node


class NodeStructureTests(unittest.TestCase):
    """要求：只能接在最后 —— 只有输入、没有输出。"""

    def test_init_creates_input_only(self):
        node = _make_node()
        node.inputs = _FakeSockets()
        node.outputs = _FakeSockets()

        node.init(None)

        self.assertEqual(len(node.inputs), 1)
        self.assertEqual(node.inputs.items[0].bl_idname, 'SSMTSocketPostProcess')
        self.assertEqual(
            len(node.outputs), 0,
            "文本追加节点必须没有输出口，否则后面还能再接节点",
        )

    def test_init_assigns_stable_text_block_name(self):
        node = _make_node()
        node.inputs = _FakeSockets()
        node.outputs = _FakeSockets()

        node.init(None)
        first_name = node.own_text_block_name

        self.assertTrue(first_name)
        # 重命名节点后仍指向同一个文本块（内容不会「丢失」到新名字下）
        node.name = "文本追加.001"
        self.assertEqual(module._node_text_block_name(node), first_name)


class BuildAppendedContentTests(unittest.TestCase):
    BLOCK_ID = "abc123def456"

    def test_appends_block_at_bottom(self):
        original = "[TextureOverride]\nhash = 1\n"

        result = module.build_appended_content(original, "自定义片段", self.BLOCK_ID)

        self.assertTrue(result.startswith(original.rstrip("\n")))
        self.assertIn(f"{module.BLOCK_MARKER_PREFIX}{self.BLOCK_ID}{module.BLOCK_BEGIN_SUFFIX}", result)
        self.assertIn(f"{module.BLOCK_MARKER_PREFIX}{self.BLOCK_ID}{module.BLOCK_END_SUFFIX}", result)
        self.assertIn("自定义片段", result)
        self.assertTrue(result.endswith(module.BLOCK_END_SUFFIX + "\n"))
        self.assertLess(
            result.index("自定义片段"),
            result.index(module.BLOCK_END_SUFFIX),
        )

    def test_empty_original_still_produces_block(self):
        result = module.build_appended_content("", "片段", self.BLOCK_ID)
        self.assertTrue(result.startswith(module.BLOCK_MARKER_PREFIX))
        self.assertIn("片段", result)

    def test_repeated_build_does_not_stack_blocks(self):
        original = "[Constants]\n$x = 1\n"
        once = module.build_appended_content(original, "片段", self.BLOCK_ID)
        twice = module.build_appended_content(once, "片段", self.BLOCK_ID)

        self.assertEqual(once, twice, "重复执行必须保持文件原样（幂等）")
        self.assertEqual(twice.count(module.BLOCK_MARKER_PREFIX), 2, "只有 BEGIN/END 两行标记")

    def test_changed_content_replaces_previous_block(self):
        original = "[Constants]\n"
        first = module.build_appended_content(original, "旧片段", self.BLOCK_ID)
        second = module.build_appended_content(first, "新片段", self.BLOCK_ID)

        self.assertNotIn("旧片段", second)
        self.assertIn("新片段", second)
        self.assertEqual(second.count(f"{module.BLOCK_MARKER_PREFIX}{self.BLOCK_ID}"), 2)

    def test_other_node_block_is_preserved(self):
        original = "[Constants]\n"
        other = module.build_appended_content(original, "别人的片段", "other000")
        mine = module.build_appended_content(other, "我的片段", self.BLOCK_ID)

        self.assertIn("别人的片段", mine)
        self.assertIn("我的片段", mine)

    def test_crlf_file_keeps_crlf(self):
        original = "[Constants]\r\n$x = 1\r\n"

        result = module.build_appended_content(original, "片段", self.BLOCK_ID)

        self.assertIn("\r\n", result)
        self.assertNotIn("\n", result.replace("\r\n", ""), "CRLF 文件里不应混入裸 LF")
        self.assertTrue(result.endswith(module.BLOCK_END_SUFFIX + "\r\n"))

    def test_incomplete_block_is_removed_to_eof(self):
        dangling = "[Constants]\n; --- AUTO-APPENDED CUSTOM TEXT abc123def456 BEGIN ---\n半块\n"

        result = module.remove_appended_block(dangling, self.BLOCK_ID)

        self.assertEqual(result, "[Constants]\n")


class DisplayWrapTests(unittest.TestCase):
    def test_wraps_by_display_columns(self):
        lines = module.wrap_display_lines("abcdefghij", 4)
        self.assertEqual(lines, ["abcd", "efgh", "ij"])

    def test_wide_characters_take_two_columns(self):
        lines = module.wrap_display_lines("中文中文", 4)
        self.assertEqual(lines, ["中文", "中文"])

    def test_empty_lines_are_preserved(self):
        self.assertEqual(module.wrap_display_lines("a\n\nb", 10), ["a", "", "b"])


class TextSourceTests(unittest.TestCase):
    def setUp(self):
        _fake_bpy.data.texts = _FakeTexts()

    def test_node_text_source_reads_own_block(self):
        node = _make_node(content="节点内容\n第二行")

        self.assertEqual(node.get_source_text(), "节点内容\n第二行")

    def test_project_text_source_reads_referenced_block(self):
        _fake_bpy.data.texts.new("我的片段").write("工程内容")
        node = _make_node(text_source=module.TEXT_SOURCE_PROJECT, project_name="我的片段")

        self.assertEqual(node.get_source_text(), "工程内容")

    def test_project_text_source_missing_block_is_empty(self):
        node = _make_node(text_source=module.TEXT_SOURCE_PROJECT, project_name="不存在")

        self.assertEqual(node.get_source_text(), "")

    def test_validate_accepts_node_text_source(self):
        node = _make_node()
        node.validate_export_configuration()  # 不应抛异常

    def test_validate_rejects_missing_project_text(self):
        node = _make_node(text_source=module.TEXT_SOURCE_PROJECT, project_name="不存在")

        with self.assertRaises(ValueError):
            node.validate_export_configuration()

    def test_validate_rejects_empty_project_text(self):
        node = _make_node(text_source=module.TEXT_SOURCE_PROJECT, project_name="")

        with self.assertRaises(ValueError):
            node.validate_export_configuration()

    def test_block_id_is_stable_and_unique_per_node(self):
        node_a = _make_node()
        node_b = _make_node()
        node_b.name = "文本追加_另一个"

        self.assertEqual(node_a.get_block_id(), node_a.get_block_id())
        self.assertNotEqual(node_a.get_block_id(), node_b.get_block_id())


class ExecutePostprocessTests(unittest.TestCase):
    def setUp(self):
        _fake_bpy.data.texts = _FakeTexts()

    def _write(self, path, content, newline=""):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8", newline=newline) as handle:
            handle.write(content)

    def _read(self, path):
        with open(path, "r", encoding="utf-8", newline="") as handle:
            return handle.read()

    def test_appends_to_root_config_tables_only(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root_a = os.path.join(temp_dir, "a.ini")
            root_b = os.path.join(temp_dir, "b.ini")
            nested = os.path.join(temp_dir, "sub", "nested.ini")
            self._write(root_a, "[Constants]\n")
            self._write(root_b, "[Constants]\n")
            self._write(nested, "[Resource]\n")

            node = _make_node(content="我的片段")
            ok = node.execute_postprocess(temp_dir)

            self.assertTrue(ok)
            self.assertIn("我的片段", self._read(root_a))
            self.assertIn("我的片段", self._read(root_b))
            self.assertEqual(self._read(nested), "[Resource]\n", "子目录 ini 不是配置表，不能被追加")

    def test_second_run_does_not_stack(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ini_path = os.path.join(temp_dir, "mod.ini")
            self._write(ini_path, "[Constants]\n")

            node = _make_node(content="我的片段")
            self.assertTrue(node.execute_postprocess(temp_dir))
            first = self._read(ini_path)
            node.execute_postprocess(temp_dir)
            second = self._read(ini_path)

            self.assertEqual(first, second)
            self.assertEqual(second.count(module.BLOCK_MARKER_PREFIX), 2)

    def test_creates_backup_before_writing(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ini_path = os.path.join(temp_dir, "mod.ini")
            self._write(ini_path, "[Constants]\n")

            node = _make_node(content="我的片段")
            node.execute_postprocess(temp_dir)

            backup_dir = os.path.join(temp_dir, "Backups")
            self.assertTrue(os.path.isdir(backup_dir))
            self.assertTrue(os.listdir(backup_dir), "改写配置表前必须留备份")

    def test_empty_content_is_safe_noop(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ini_path = os.path.join(temp_dir, "mod.ini")
            self._write(ini_path, "[Constants]\n")

            node = _make_node(content="")
            self.assertFalse(node.execute_postprocess(temp_dir))

            self.assertEqual(self._read(ini_path), "[Constants]\n")

    def test_whitespace_only_content_is_safe_noop(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ini_path = os.path.join(temp_dir, "mod.ini")
            self._write(ini_path, "[Constants]\n")

            node = _make_node(content="   \n\n")
            self.assertFalse(node.execute_postprocess(temp_dir))

            self.assertEqual(self._read(ini_path), "[Constants]\n")

    def test_missing_export_dir_is_safe_noop(self):
        node = _make_node(content="我的片段")

        self.assertFalse(node.execute_postprocess(os.path.join(tempfile.gettempdir(), "definitely_missing_ta_dir")))
        self.assertFalse(node.execute_postprocess(""))

    def test_project_text_content_is_used(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ini_path = os.path.join(temp_dir, "mod.ini")
            self._write(ini_path, "[Constants]\n")
            _fake_bpy.data.texts.new("工程片段").write("来自工程文本")

            node = _make_node(text_source=module.TEXT_SOURCE_PROJECT, project_name="工程片段")
            self.assertTrue(node.execute_postprocess(temp_dir))

            self.assertIn("来自工程文本", self._read(ini_path))

    def test_bom_is_preserved(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ini_path = os.path.join(temp_dir, "mod.ini")
            with open(ini_path, "w", encoding="utf-8-sig", newline="") as handle:
                handle.write("[Constants]\n")

            node = _make_node(content="我的片段")
            self.assertTrue(node.execute_postprocess(temp_dir))

            with open(ini_path, "rb") as handle:
                raw = handle.read()
            self.assertTrue(raw.startswith(b"\xef\xbb\xbf"), "原有 BOM 必须保留")
            self.assertIn("我的片段", raw.decode("utf-8-sig"))

    def test_non_utf8_config_table_is_skipped(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ini_path = os.path.join(temp_dir, "mod.ini")
            original_bytes = "[Constants]\n; 中文GBK\n".encode("gbk")
            with open(ini_path, "wb") as handle:
                handle.write(original_bytes)

            node = _make_node(content="我的片段")
            self.assertFalse(node.execute_postprocess(temp_dir))

            with open(ini_path, "rb") as handle:
                self.assertEqual(handle.read(), original_bytes, "非 UTF-8 配置表必须原样不动")


class AutoAppendedMarkerIntegrationTests(unittest.TestCase):
    """追加块必须被其它后处理节点识别为「自动追加的尾部」并原样保留。"""

    def test_begin_marker_is_recognized(self):
        node = _make_node()
        begin_line = (
            f"{module.BLOCK_MARKER_PREFIX}{node.get_block_id()}{module.BLOCK_BEGIN_SUFFIX}"
        )

        self.assertTrue(base_module.SSMTNode_PostProcess_Base.is_known_auto_appended_marker(begin_line))

    def test_tail_split_keeps_body_and_appended_block_apart(self):
        node = _make_node()
        body = "[TextureOverride]\nhash = 1\n"
        content = base_module.SSMTNode_PostProcess_Base
        combined = module.build_appended_content(body, "我的片段", node.get_block_id())

        head, tail = content.split_auto_appended_tail_content(combined)

        self.assertNotIn("我的片段", head)
        self.assertIn("我的片段", tail)
        self.assertIn(module.BLOCK_MARKER_PREFIX, tail)


class _FakeOperatorProperties:
    def __init__(self):
        self.node_name = ""


class _FakeLayout:
    """极简 UILayout 替身：只记录画出来的文本/算子，用来验证 draw_buttons 不炸且内容对。"""

    def __init__(self, labels=None, operators=None):
        self.labels = labels if labels is not None else []
        self.operators = operators if operators is not None else []

    def box(self):
        return _FakeLayout(self.labels, self.operators)

    def row(self, align=False):
        return _FakeLayout(self.labels, self.operators)

    def label(self, text="", icon=""):
        self.labels.append(text)

    def prop(self, _obj, _name, **_kwargs):
        return None

    def prop_search(self, _obj, _name, _data, _prop, **_kwargs):
        return None

    def operator(self, idname, **kwargs):
        self.operators.append((idname, kwargs))
        return _FakeOperatorProperties()

    def separator(self):
        return None


class DrawButtonsTests(unittest.TestCase):
    def setUp(self):
        _fake_bpy.data.texts = _FakeTexts()

    def test_draws_text_box_lines_for_node_source(self):
        node = _make_node(content="第一行\n第二行")
        layout = _FakeLayout()

        node.draw_buttons(None, layout)

        self.assertIn("第一行", layout.labels)
        self.assertIn("第二行", layout.labels)

    def test_draws_hint_when_content_is_empty(self):
        node = _make_node(content="")
        layout = _FakeLayout()

        node.draw_buttons(None, layout)

        self.assertTrue(any("（空）" in text for text in layout.labels))

    def test_draws_project_source_without_error(self):
        _fake_bpy.data.texts.new("工程片段").write("来自工程文本")
        node = _make_node(text_source=module.TEXT_SOURCE_PROJECT, project_name="工程片段")
        layout = _FakeLayout()

        node.draw_buttons(None, layout)

        self.assertIn("来自工程文本", layout.labels)

    def test_missing_project_text_warns(self):
        node = _make_node(text_source=module.TEXT_SOURCE_PROJECT, project_name="不存在")
        layout = _FakeLayout()

        node.draw_buttons(None, layout)

        self.assertTrue(any("不存在" in text for text in layout.labels))

    def test_long_content_is_capped(self):
        node = _make_node(content="\n".join(f"第{i}行" for i in range(100)))
        layout = _FakeLayout()

        node.draw_buttons(None, layout)

        self.assertTrue(any("查看全部" in text for text in layout.labels))
        self.assertLessEqual(len(layout.labels), module.MAX_DISPLAY_LINES + 6)


class _FakeSpace:
    def __init__(self, type_name, text=None):
        self.type = type_name
        self.text = text


class _FakeArea:
    """区域替身：type 与 space.type 联动（与 Blender 行为一致）。"""

    def __init__(self, type_name, pointer, text=None):
        self._type = type_name
        self._pointer = pointer
        self.spaces = [_FakeSpace(type_name, text)]
        self.regions = [types.SimpleNamespace(type='WINDOW')]

    @property
    def type(self):
        return self._type

    @type.setter
    def type(self, value):
        self._type = value
        for space in self.spaces:
            space.type = value

    @property
    def text(self):
        return self.spaces[0].text

    def as_pointer(self):
        return self._pointer

    def tag_redraw(self):
        return None


class _FakeScreen:
    def __init__(self, areas):
        self.areas = areas


class _FakeWindow:
    def __init__(self, screen):
        self.screen = screen


class _FakeNodes:
    def __init__(self, node):
        self._node = node

    def get(self, name):
        return self._node if name == getattr(self._node, "name", None) else None


class _FakeContext:
    def __init__(self, window, area, node):
        self.window = window
        self.area = area
        self.window_manager = types.SimpleNamespace(windows=[window])
        self.space_data = types.SimpleNamespace(
            edit_tree=types.SimpleNamespace(nodes=_FakeNodes(node))
        )
        self.override = {}

    def temp_override(self, **kwargs):
        outer = self

        class _Override:
            def __enter__(self_inner):
                outer.override = kwargs
                return outer

            def __exit__(self_inner, *_exc):
                outer.override = {}
                return False

        return _Override()


class ToggleEditorTests(unittest.TestCase):
    """「编辑文本」按钮：点一下打开编辑区域，再点一下关闭。"""

    def setUp(self):
        _fake_bpy.data.texts = _FakeTexts()
        module._EDITOR_SESSIONS.clear()
        _fake_screen_ops.reset()

    def _setup(self, node, text_editors=()):
        node_area = _FakeArea('NODE_EDITOR', 1)
        screen = _FakeScreen([node_area, *text_editors])
        window = _FakeWindow(screen)
        _fake_bpy.context.window_manager = types.SimpleNamespace(windows=[window])
        context = _FakeContext(window, node_area, node)

        def on_split():
            # 模拟 Blender：在 context.area 旁边分裂出一个同类型的新区域
            split_target = context.override.get("area", node_area)
            next_pointer = max(area.as_pointer() for area in screen.areas) + 1
            screen.areas.append(_FakeArea(split_target.type, next_pointer))

        def on_close():
            closed = context.override.get("area")
            if closed in screen.areas:
                screen.areas.remove(closed)

        _fake_screen_ops.on_split = on_split
        _fake_screen_ops.on_close = on_close
        return screen, context

    def _toggle(self, node, context):
        operator = module.SSMT_OT_TextAppend_ToggleEditor()
        operator.node_name = node.name
        return operator.execute(context)

    def test_first_click_splits_editor_and_second_click_closes_it(self):
        node = _make_node(content="片段")
        screen, context = self._setup(node)
        block_name = module._node_text_block_name(node)

        self.assertFalse(module.is_editor_open(node))
        self.assertEqual(self._toggle(node, context), {'FINISHED'})

        self.assertTrue(module.is_editor_open(node))
        self.assertEqual(_fake_screen_ops.split_calls, 1)
        self.assertEqual(len(screen.areas), 2)
        self.assertEqual(screen.areas[-1].type, 'TEXT_EDITOR')
        self.assertEqual(screen.areas[-1].text.name, block_name)

        self.assertEqual(self._toggle(node, context), {'FINISHED'})

        self.assertFalse(module.is_editor_open(node))
        self.assertEqual(_fake_screen_ops.close_calls, 1)
        self.assertEqual(len(screen.areas), 1, "第二次点击必须把分裂出来的区域关掉")
        self.assertNotIn(module._session_key(node), module._EDITOR_SESSIONS)

    def test_reuses_existing_editor_and_restores_previous_text_on_close(self):
        node = _make_node(content="片段")
        other_block = _fake_bpy.data.texts.new("别的文本")
        editor = _FakeArea('TEXT_EDITOR', 7, text=other_block)
        screen, context = self._setup(node, [editor])

        self._toggle(node, context)

        self.assertEqual(_fake_screen_ops.split_calls, 0, "已有文本编辑器时不该再分裂区域")
        self.assertEqual(editor.text.name, module._node_text_block_name(node))
        self.assertTrue(module.is_editor_open(node))

        self._toggle(node, context)

        self.assertEqual(_fake_screen_ops.close_calls, 0, "复用的编辑器绝不能关掉")
        self.assertIs(editor.text, other_block, "关闭时应还原编辑器原来显示的文本")
        self.assertFalse(module.is_editor_open(node))

    def test_manually_closed_area_reopens_on_next_click(self):
        node = _make_node(content="片段")
        screen, context = self._setup(node)

        self._toggle(node, context)
        screen.areas.pop()  # 用户手动关掉了编辑区域

        self.assertFalse(module.is_editor_open(node))
        self._toggle(node, context)

        self.assertEqual(_fake_screen_ops.split_calls, 2)
        self.assertTrue(module.is_editor_open(node))
        self.assertEqual(len(screen.areas), 2)

    def test_button_label_follows_open_state(self):
        node = _make_node(content="片段")
        _screen, context = self._setup(node)

        closed_layout = _FakeLayout()
        node.draw_buttons(None, closed_layout)
        self.assertIn(
            ("ssmt.text_append_toggle_editor", {"text": "编辑文本", "icon": "TEXT"}),
            closed_layout.operators,
        )

        self._toggle(node, context)

        open_layout = _FakeLayout()
        node.draw_buttons(None, open_layout)
        self.assertIn(
            ("ssmt.text_append_toggle_editor", {"text": "关闭编辑", "icon": "X"}),
            open_layout.operators,
        )

    def test_toggle_without_node_reports_error(self):
        node = _make_node(content="片段")
        _screen, context = self._setup(node)

        operator = module.SSMT_OT_TextAppend_ToggleEditor()
        operator.node_name = "不存在的节点"

        self.assertEqual(operator.execute(context), {'CANCELLED'})

    def test_failed_close_keeps_session_and_reports_warning(self):
        node = _make_node(content="片段")
        screen, context = self._setup(node)
        self._toggle(node, context)

        def exploding_close():
            raise RuntimeError("区域关闭失败")

        _fake_screen_ops.on_close = exploding_close

        self.assertEqual(self._toggle(node, context), {'CANCELLED'})
        self.assertTrue(module.is_editor_open(node), "关闭失败时会话必须保留，按钮状态才不会骗人")
        self.assertEqual(len(screen.areas), 2)

    def test_close_area_uses_window_screen_area_override(self):
        node = _make_node(content="片段")
        _screen, context = self._setup(node)
        self._toggle(node, context)

        captured = {}
        original_close = _fake_screen_ops.on_close

        def capture_close():
            captured.update(context.override)
            original_close()

        _fake_screen_ops.on_close = capture_close
        self._toggle(node, context)

        self.assertIn("window", captured)
        self.assertIn("screen", captured)
        self.assertEqual(captured.get("area").type, 'TEXT_EDITOR')


class RegistrationTests(unittest.TestCase):
    def test_classes_tuple_contains_node_and_operators(self):
        self.assertIn(module.SSMTNode_PostProcess_TextAppend, module.classes)
        self.assertIn(module.SSMT_OT_TextAppend_ToggleEditor, module.classes)
        self.assertIn(module.SSMT_OT_TextAppend_ClearText, module.classes)

    def test_node_bl_idname_matches_menu_entry(self):
        self.assertEqual(module.SSMTNode_PostProcess_TextAppend.bl_idname, module.NODE_BL_IDNAME)
        self.assertTrue(module.NODE_BL_IDNAME.startswith("SSMTNode_PostProcess_"))


if __name__ == "__main__":
    unittest.main()
