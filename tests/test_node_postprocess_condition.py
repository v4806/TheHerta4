# -*- coding: utf-8 -*-
"""条件判断后处理节点（blueprint/node_postprocess_condition.py）测试。

覆盖：
- 配置表缺 [Present] 段时就地新建该段，并写入「锁存 + 清零」两行每帧复位行
  （缺这两行门控变量永不复位、部件效果永久消失；旧实现只打印一句就静默返回）；
- 唯一的 [Present] 落在自动追加尾块（标记行之后）里时抛 ConditionApplyError
  并拒绝改写，不许静默；
- 没有导出段登记表（本分支还没有 common/export_section_registry）时退化：
  execute_postprocess 返回 True 且配置表字节完全不变；
- ini 文本往返幂等：同一输入连跑两次产物逐字节一致（新建 / 复用 [Present] 两种）；
- 检测 hash 已被本 mod 接管时，置位块落在该 hash 自己的 TextureOverride 段体
  顶层（段根匹配键之后、门控 if 之外）；
- 导出执行：先改写一次，再次执行不再改写文件（落盘幂等）；
- 同目录分文件（multifile）里有同名段时只提示、不改动分文件。
"""
import importlib.util
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

PKG = "_condition_test_pkg"
for _package_name in (PKG, f"{PKG}.blueprint", f"{PKG}.common", f"{PKG}.utils"):
    _package = types.ModuleType(_package_name)
    _package.__path__ = []
    sys.modules[_package_name] = _package


# ---------------------------------------------------------------------------
# 假 bpy
# ---------------------------------------------------------------------------

class _FakePropertyGroup:
    pass


class _FakeUIList:
    pass


class _FakeOperator:
    def report(self, _kind, _message):
        return None


def _build_fake_bpy():
    """搭一套最小可用的假 bpy（节点类与数据列表需要的 props/types 入口）。"""
    bpy_types = types.ModuleType("bpy.types")
    bpy_types.Operator = _FakeOperator
    bpy_types.Node = type("Node", (), {})
    bpy_types.NodeSocket = type("NodeSocket", (), {})
    bpy_types.PropertyGroup = _FakePropertyGroup
    bpy_types.UIList = _FakeUIList

    bpy_props = types.ModuleType("bpy.props")
    bpy_props.StringProperty = lambda **kwargs: kwargs.get("default", "")
    bpy_props.IntProperty = lambda **kwargs: kwargs.get("default", 0)
    bpy_props.BoolProperty = lambda **kwargs: kwargs.get("default", False)
    bpy_props.EnumProperty = lambda **kwargs: kwargs.get("default", "")
    bpy_props.CollectionProperty = lambda **kwargs: []

    bpy_utils = types.ModuleType("bpy.utils")
    bpy_utils.register_class = lambda _cls: None
    bpy_utils.unregister_class = lambda _cls: None

    bpy_module = types.ModuleType("bpy")
    bpy_module.types = bpy_types
    bpy_module.props = bpy_props
    bpy_module.utils = bpy_utils
    bpy_module.data = types.SimpleNamespace(texts=types.SimpleNamespace(get=lambda _name: None))
    bpy_module.ops = types.SimpleNamespace()
    bpy_module.context = types.SimpleNamespace(window_manager=None)
    return bpy_module, {
        "bpy.types": bpy_types,
        "bpy.props": bpy_props,
        "bpy.utils": bpy_utils,
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


class _RecordingLog:
    """记录 info/warning/error 文案的 LOG 替身。"""

    def __init__(self):
        self.records = []

    def _record(self, level, message):
        self.records.append((level, str(message)))

    def info(self, message, *_args, **_kwargs):
        self._record("info", message)

    def warning(self, message, *_args, **_kwargs):
        self._record("warning", message)

    def error(self, message, *_args, **_kwargs):
        self._record("error", message)

    def debug(self, message, *_args, **_kwargs):
        self._record("debug", message)

    def texts(self, level=None):
        return [text for lvl, text in self.records if level is None or lvl == level]


class _ExportState:
    """导出期上下文替身：目标 ini、蓝图模型、进度上报记录。"""

    target_ini = ""
    model = None
    stages = []


class _FakeCommentCleanup:
    @staticmethod
    def _find_target_ini_file(_mod_export_path):
        if _ExportState.target_ini:
            return _ExportState.target_ini, ""
        return "", "目录里没有 .ini 配置表"


class _FakeExportHelper:
    @staticmethod
    def get_current_blueprint_model():
        return _ExportState.model


class _FakeExportProgress:
    @staticmethod
    def report_stage(text):
        _ExportState.stages.append(str(text))


class _FakeRegistry:
    """导出段登记表替身：只实现条件节点用到的四个查询入口。"""

    def __init__(self, entries):
        self.entries = [dict(entry) for entry in entries]

    def get_sections_for_draw_ib(self, draw_ib, ini_path=""):
        wanted = str(draw_ib or "").casefold()
        return [dict(e) for e in self.entries if str(e.get("draw_ib", "")).casefold() == wanted]

    def get_sections_for_draw_ibs(self, draw_ibs, ini_path=""):
        wanted = {str(value or "").casefold() for value in (draw_ibs or [])}
        return [dict(e) for e in self.entries if str(e.get("draw_ib", "")).casefold() in wanted]

    def get_all(self):
        return {}

    def get_ini_paths(self):
        return []


# 假 bpy 只在「加载被测模块」期间生效：加载完立刻还原，避免污染同进程的其它测试。
_SAVED_BPY_ENTRIES = {name: sys.modules.get(name) for name in _fake_bpy_submodules}
_SAVED_BPY = sys.modules.get("bpy")
sys.modules["bpy"] = _fake_bpy
sys.modules.update(_fake_bpy_submodules)

_log = _RecordingLog()

try:
    _install_module(f"{PKG}.blueprint.node_base", SSMTNodeBase=object)
    _install_module(f"{PKG}.utils.log_utils", LOG=_log)
    _install_module(f"{PKG}.utils.format_utils", Fatal=Exception)

    base_module = _load_real_module(
        f"{PKG}.blueprint.node_postprocess_base",
        Path("blueprint") / "node_postprocess_base.py",
    )
    # 条件节点复用 skip 节点的段头解析 / 分文件提示，skip 只依赖 base。
    skip_module = _load_real_module(
        f"{PKG}.blueprint.node_postprocess_skip",
        Path("blueprint") / "node_postprocess_skip.py",
    )
    _install_module(
        f"{PKG}.blueprint.node_postprocess_comment_cleanup",
        SSMTNode_PostProcess_CommentCleanup=_FakeCommentCleanup,
    )
    _install_module(
        f"{PKG}.blueprint.export_helper",
        BlueprintExportHelper=_FakeExportHelper,
    )
    _install_module(
        f"{PKG}.blueprint.export_progress",
        report_stage=_FakeExportProgress.report_stage,
    )
    module = _load_real_module(
        f"{PKG}.blueprint.node_postprocess_condition",
        Path("blueprint") / "node_postprocess_condition.py",
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


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------

OBJECT_NAME = "LOD0.7fbbcf0d-1260-9402.脸+眼"
TARGET_SECTION = "TextureOverride_IB_7fbbcf0d"
DRAW_IB = "7fbbcf0d"
NS = "abcd1234"
DETECT_HASH = "92cb56c9"

#: 没有 [Present] 段的最小配置表：目标段里既有必须留顶层的 run，也有可门控的 drawindexed。
INI_NO_PRESENT = (
    "[Constants]\n"
    "global persist $mod_enabled = 1\n"
    "\n"
    "[TextureOverride_IB_7fbbcf0d]\n"
    "hash = 7fbbcf0d\n"
    "run = CommandListSkinSwitch\n"
    "drawindexed = 1260\n"
)

#: 已有 [Present] 段的配置表。
INI_WITH_PRESENT = (
    "[Constants]\n"
    "global persist $mod_enabled = 1\n"
    "\n"
    "[TextureOverride_IB_7fbbcf0d]\n"
    "hash = 7fbbcf0d\n"
    "drawindexed = 1260\n"
    "\n"
    "[Present]\n"
    "post $some_other = 1\n"
)


def _make_model(object_name=OBJECT_NAME, draw_ib=DRAW_IB):
    draw_call = types.SimpleNamespace(
        obj_name=object_name,
        source_obj_name="",
        match_draw_ib=draw_ib,
        get_blender_obj_name=lambda: "",
    )
    return types.SimpleNamespace(
        ordered_draw_obj_data_model_list=[draw_call],
        _object_name_mapping={},
    )


def _make_node(object_names=None, condition_hash=DETECT_HASH, index_count=""):
    node = module.SSMTNode_PostProcess_Condition()
    node.name = "条件判断"
    node.condition_objects = [
        types.SimpleNamespace(object_name=value)
        for value in (object_names or [OBJECT_NAME])
    ]
    node.condition_hash = condition_hash
    node.check_hash = ""
    node.condition_index_count = index_count
    return node


class _RegistryPatch:
    """把条件节点的登记表软导入临时替换成指定对象（None 表示登记表不可用）。"""

    def __init__(self, registry):
        self.registry = registry
        self.original = None

    def __enter__(self):
        self.original = module._get_section_registry
        module._get_section_registry = lambda: self.registry
        return self

    def __exit__(self, *_exc):
        module._get_section_registry = self.original
        return False


def _stripped_lines(text):
    return [line.strip() for line in str(text).splitlines()]


# ---------------------------------------------------------------------------
# 纯函数层
# ---------------------------------------------------------------------------

class ConditionApplyTests(unittest.TestCase):
    def test_missing_present_section_is_created_with_reset_lines(self):
        updated, stats = module._apply_condition(
            INI_NO_PRESENT, NS, {TARGET_SECTION}, DETECT_HASH
        )

        self.assertEqual(1, stats["wrapped"])
        self.assertEqual("created", stats["present"])
        lines = _stripped_lines(updated)
        self.assertIn("[Present]", lines)
        self.assertIn(f"; @@COND:{NS}:present@@", lines)
        active_line = f"post $cond_{NS}_cond_active = $cond_{NS}_cond_frame"
        frame_line = f"post $cond_{NS}_cond_frame = 0"
        self.assertIn(active_line, lines)
        self.assertIn(frame_line, lines)
        self.assertLess(lines.index(active_line), lines.index(frame_line))

    def test_existing_present_section_is_reused(self):
        updated, stats = module._apply_condition(
            INI_WITH_PRESENT, NS, {TARGET_SECTION}, DETECT_HASH
        )

        self.assertEqual("existing", stats["present"])
        self.assertIn("post $some_other = 1", updated)
        self.assertEqual(1, updated.count("[Present]"))
        self.assertEqual(1, updated.count(f"; @@COND:{NS}:present@@"))

    def test_present_inside_auto_appended_tail_is_rejected(self):
        text = (
            "[Constants]\n"
            "global persist $mod_enabled = 1\n"
            "\n"
            "[TextureOverride_IB_7fbbcf0d]\n"
            "hash = 7fbbcf0d\n"
            "drawindexed = 1260\n"
            "\n"
            "; --- AUTO-APPENDED CUSTOM TEXT 01 ---\n"
            "[Present]\n"
            "post $custom = 1\n"
        )

        with self.assertRaises(module.ConditionApplyError) as caught:
            module._apply_condition(text, NS, {TARGET_SECTION}, DETECT_HASH)

        message = str(caught.exception)
        self.assertIn("自动追加块", message)
        self.assertIn("拒绝改写", message)

    def test_apply_is_idempotent_when_present_is_created(self):
        first, stats_first = module._apply_condition(
            INI_NO_PRESENT, NS, {TARGET_SECTION}, DETECT_HASH
        )
        second, stats_second = module._apply_condition(
            first, NS, {TARGET_SECTION}, DETECT_HASH
        )

        self.assertEqual("created", stats_first["present"])
        self.assertEqual("existing", stats_second["present"])
        self.assertEqual(first, second)
        self.assertEqual(1, second.count(f"; @@COND:{NS}:begin@@"))
        self.assertEqual(1, second.count("[Present]"))

    def test_apply_is_idempotent_with_existing_present(self):
        first, _stats_first = module._apply_condition(
            INI_WITH_PRESENT, NS, {TARGET_SECTION}, DETECT_HASH
        )
        second, stats_second = module._apply_condition(
            first, NS, {TARGET_SECTION}, DETECT_HASH
        )

        self.assertEqual(first, second)
        self.assertEqual("existing", stats_second["present"])
        self.assertEqual(1, second.count(f"; @@COND:{NS}:begin@@"))
        self.assertEqual(1, second.count(f"; @@COND:{NS}:present@@"))

    def test_detect_block_lands_at_top_of_taken_texture_override(self):
        text = (
            "[Constants]\n"
            "global persist $mod_enabled = 1\n"
            "\n"
            "[TextureOverride_IB_7fbbcf0d]\n"
            "hash = 7fbbcf0d\n"
            "drawindexed = 1260\n"
            "\n"
            "[TextureOverride_IB_92cb56c9]\n"
            "hash = 92cb56c9\n"
            "match_first_index = 0\n"
            "\n"
            "drawindexed = 9\n"
        )

        updated, stats = module._apply_condition(
            text,
            NS,
            {TARGET_SECTION},
            DETECT_HASH,
            "",
            {"TextureOverride_IB_92cb56c9"},
        )

        self.assertEqual(1, stats["detect"])
        self.assertFalse(stats["check"])
        lines = _stripped_lines(updated)
        marker_index = lines.index(f"; @@COND:{NS}:detect@@")
        self.assertEqual(f"$cond_{NS}_cond_frame = 1", lines[marker_index + 1])
        # 段根匹配键之后、段体内容之前（顶层，没有被门控 if 包住）
        self.assertGreater(marker_index, lines.index("match_first_index = 0"))
        self.assertLess(marker_index, lines.index("drawindexed = 9"))
        section_index = lines.index("[TextureOverride_IB_92cb56c9]")
        self.assertEqual(
            [],
            [line for line in lines[section_index:marker_index] if line.startswith("if ")],
        )
        # 目标段自身照旧被门控
        self.assertIn(f"if $cond_{NS}_cond_active == 1", lines)

# ---------------------------------------------------------------------------
# 导出执行层
# ---------------------------------------------------------------------------

class ConditionExecuteTests(unittest.TestCase):
    def setUp(self):
        _log.records.clear()
        _ExportState.target_ini = ""
        _ExportState.model = None
        _ExportState.stages = []

    def _write_ini(self, directory, text, bom=False):
        path = os.path.join(directory, "mod.ini")
        data = text.encode("utf-8")
        if bom:
            data = b"\xef\xbb\xbf" + data
        with open(path, "wb") as handle:
            handle.write(data)
        return path

    def _prepare(self, directory, model=None):
        path = self._write_ini(directory, INI_NO_PRESENT)
        _ExportState.target_ini = path
        _ExportState.model = model or _make_model()
        return path

    def test_no_registry_degrades_without_touching_ini(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._prepare(tmp)
            before = Path(path).read_bytes()

            with _RegistryPatch(None):
                self.assertTrue(_make_node().execute_postprocess(tmp))

            self.assertEqual(before, Path(path).read_bytes())
            self.assertTrue(
                any("导出期段登记里没有" in text for text in _log.texts("warning"))
            )
            self.assertFalse(os.path.isdir(os.path.join(tmp, "Backups")))

    def test_execute_rewrites_once_then_leaves_file_alone(self):
        registry = _FakeRegistry(
            [
                {"name": TARGET_SECTION, "draw_ib": DRAW_IB, "type": ""},
                # 资源声明段不参与门控
                {"name": "ResourceDiffuse7fbbcf0d", "draw_ib": DRAW_IB, "type": ""},
            ]
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = self._prepare(tmp)
            node = _make_node()

            with _RegistryPatch(registry):
                self.assertTrue(node.execute_postprocess(tmp))
                first = Path(path).read_bytes()
                self.assertIn(b"[Present]", first)
                self.assertIn(f"; @@COND:{node._namespace()}:begin@@".encode("utf-8"), first)
                self.assertTrue(os.path.isdir(os.path.join(tmp, "Backups")))

                self.assertTrue(node.execute_postprocess(tmp))
                second = Path(path).read_bytes()

            self.assertEqual(first, second)
            self.assertTrue(_ExportState.stages)

    def test_sibling_multifile_sections_are_reported_but_not_touched(self):
        registry = _FakeRegistry([{"name": TARGET_SECTION, "draw_ib": DRAW_IB}])
        with tempfile.TemporaryDirectory() as tmp:
            self._prepare(tmp)
            sibling_path = os.path.join(tmp, "Sub.ini")
            with open(sibling_path, "w", encoding="utf-8") as handle:
                handle.write(f"[{TARGET_SECTION}]\nhash = {DRAW_IB}\n")
            sibling_before = Path(sibling_path).read_bytes()

            with _RegistryPatch(registry):
                self.assertTrue(_make_node().execute_postprocess(tmp))

            self.assertEqual(sibling_before, Path(sibling_path).read_bytes())
            self.assertTrue(
                any("分文件" in text and "Sub.ini" in text for text in _log.texts("info")),
                _log.records,
            )


if __name__ == "__main__":
    unittest.main()
