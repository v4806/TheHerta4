# -*- coding: utf-8 -*-
"""跳过部件后处理节点（blueprint/node_postprocess_skip.py）测试。

覆盖：
- 整段删除 + 追加跳过段的产物形状（完整三段段 / 只 hash 回退段）；
- 连跑两次幂等（先剥旧块再追加，文本逐字节一致）；
- 跨 IB 共用段保护（B3）：同名段仍被本次删除范围之外的 draw_ib 登记时保留不删，
  且登记表不可用时退化为「按段名全文删除」并在摘要里说明这一退化；
- 分文件（multifile）同名段只提示、不改动分文件；
- 追加块能被 node_postprocess_base 识别为自动追加尾段；
- 导出执行：删段 + 追加跳过段 + 登记新段；再次执行不再改写文件。
"""
import importlib.util
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

PKG = "_skip_test_pkg"
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
    """导出段登记表替身：条件/跳过两个节点用到的查询 + 新段登记入口。"""

    def __init__(self, tables=None):
        self.tables = {str(key): [dict(entry) for entry in value] for key, value in (tables or {}).items()}
        self.registered = []
        self.register_error = None
        self.all_error = None

    def get_all(self):
        if self.all_error is not None:
            raise self.all_error
        return self.tables

    def get_ini_paths(self):
        return list(self.tables)

    def get_sections_for_draw_ib(self, draw_ib, ini_path=""):
        wanted = str(draw_ib or "").casefold()
        return [dict(e) for entries in self.tables.values() for e in entries
                if str(e.get("draw_ib", "")).casefold() == wanted]

    def get_sections_for_draw_ibs(self, draw_ibs, ini_path=""):
        wanted = {str(value or "").casefold() for value in (draw_ibs or [])}
        return [dict(e) for entries in self.tables.values() for e in entries
                if str(e.get("draw_ib", "")).casefold() in wanted]

    def register_section(self, section_name, section_type="", draw_ib="", ini_path=""):
        if self.register_error is not None:
            raise self.register_error
        self.registered.append((str(section_name), str(section_type), str(draw_ib)))
        return None


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
    module = _load_real_module(
        f"{PKG}.blueprint.node_postprocess_skip",
        Path("blueprint") / "node_postprocess_skip.py",
    )
    # 跳过节点复用条件节点的圈定实现（模块加载期反向依赖，先 skip 后 condition）。
    condition_module = _load_real_module(
        f"{PKG}.blueprint.node_postprocess_condition",
        Path("blueprint") / "node_postprocess_condition.py",
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

PART_NAME = "LOD0.7fbbcf0d-1260-9402.脸+眼"
DRAW_IB = "7fbbcf0d"
TARGET_SECTION = "TextureOverride_IB_7fbbcf0d"
OTHER_IB = "999bff94"
SKIP_SECTION = "TextureOverride_IB_7fbbcf0d_1260_9402"

INI_BODY = (
    "[Constants]\n"
    "global persist $mod_enabled = 1\n"
    "\n"
    "[TextureOverride_IB_7fbbcf0d]\n"
    "hash = 7fbbcf0d\n"
    "drawindexed = 1260\n"
    "\n"
    "[CrossIBPresent]\n"
    "if $mod_enabled == 1\n"
    "post $mod_enabled = 1\n"
    "endif\n"
    "\n"
    "[ResourceDiffuse7fbbcf0d]\n"
    "type = Buffer\n"
)


def _make_model(object_name=PART_NAME, draw_ib=DRAW_IB):
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


def _make_node(part_names=None):
    node = module.SSMTNode_PostProcess_Skip()
    node.name = "跳过"
    node.skip_objects = [
        types.SimpleNamespace(object_name=value)
        for value in (part_names or [PART_NAME])
    ]
    return node


class _RegistryPatch:
    """临时替换登记表入口：跳过节点自己查一次，段名圈定走条件节点的实现。"""

    def __init__(self, registry):
        self.registry = registry
        self.originals = {}

    def __enter__(self):
        for target in (module, condition_module):
            self.originals[target] = target._get_section_registry
            target._get_section_registry = lambda registry=self.registry: registry
        return self

    def __exit__(self, *_exc):
        for target, original in self.originals.items():
            target._get_section_registry = original
        self.originals = {}
        return False


def _stripped_lines(text):
    return [line.strip() for line in str(text).splitlines()]


# ---------------------------------------------------------------------------
# 删段 + 追加块
# ---------------------------------------------------------------------------

class SkipBlockTests(unittest.TestCase):
    def test_delete_sections_removes_whole_section_only(self):
        updated, deleted, preserved = module._delete_sections(INI_BODY, {TARGET_SECTION})

        self.assertEqual([TARGET_SECTION], deleted)
        self.assertEqual([], preserved)
        self.assertNotIn("drawindexed = 1260", updated)
        self.assertIn("[CrossIBPresent]", updated)
        self.assertIn("[ResourceDiffuse7fbbcf0d]", updated)
        self.assertIn("[Constants]", updated)

    def test_delete_sections_keeps_unknown_names_untouched(self):
        updated, deleted, preserved = module._delete_sections(INI_BODY, {"TextureOverride_Nope"})

        self.assertEqual(INI_BODY, updated)
        self.assertEqual([], deleted)
        self.assertEqual([], preserved)

    def test_skip_block_shape_for_full_and_hash_only_entries(self):
        body, _deleted, _preserved = module._delete_sections(INI_BODY, {TARGET_SECTION})
        combined = module._append_skip_block(body, [(DRAW_IB, 1260, 9402)])

        lines = _stripped_lines(combined)
        expected = [
            "; --- AUTO-APPENDED SKIP BLOCK ---",
            "; skip 7fbbcf0d_1260_9402",
            "[TextureOverride_IB_7fbbcf0d_1260_9402]",
            "hash = 7fbbcf0d",
            "match_first_index = 9402",
            "match_index_count = 1260",
            "handling = skip",
        ]
        self.assertEqual(expected, lines[-len(expected):])

        hash_only = module._append_skip_block("", [DRAW_IB])
        self.assertEqual(
            [
                "; --- AUTO-APPENDED SKIP BLOCK ---",
                "; skip 7fbbcf0d",
                "[TextureOverride_IB_7fbbcf0d]",
                "hash = 7fbbcf0d",
                "handling = skip",
            ],
            _stripped_lines(hash_only),
        )

    def test_apply_skip_block_is_idempotent(self):
        first = module._apply_skip_block(INI_BODY, [(DRAW_IB, 1260, 9402)])
        second = module._apply_skip_block(first, [(DRAW_IB, 1260, 9402)])

        self.assertEqual(first, second)
        self.assertEqual(1, second.count("; --- AUTO-APPENDED SKIP BLOCK ---"))
        self.assertEqual(1, second.count(f"[{SKIP_SECTION}]"))

    def test_entries_are_parsed_from_part_name(self):
        entries = module._entries_from_names([PART_NAME])

        self.assertEqual([(DRAW_IB, 1260, 9402)], entries)

    def test_skip_marker_is_known_auto_appended_tail(self):
        self.assertTrue(
            base_module.SSMTNode_PostProcess_Base.is_known_auto_appended_marker(
                module.SKIP_BLOCK_MARKER
            )
        )


# ---------------------------------------------------------------------------
# 跨 IB 共用段保护（B3）
# ---------------------------------------------------------------------------

class CrossIbProtectionTests(unittest.TestCase):
    def _registry(self, entries):
        return _FakeRegistry({os.path.join("mods", "mod.ini"): entries})

    def test_section_owned_by_other_draw_ib_is_preserved(self):
        registry = self._registry(
            [
                {"name": TARGET_SECTION, "type": "", "draw_ib": DRAW_IB},
                {"name": "CrossIBPresent", "type": "CrossIBPresent", "draw_ib": OTHER_IB},
            ]
        )

        with _RegistryPatch(registry):
            shared = module._collect_cross_ib_shared_names(
                {TARGET_SECTION, "CrossIBPresent"}, {DRAW_IB}
            )

        self.assertIsNotNone(shared)
        self.assertIn("crossibpresent", shared)
        self.assertNotIn(TARGET_SECTION.casefold(), shared)
        self.assertEqual([OTHER_IB], shared["crossibpresent"]["owners"])

        updated, deleted, preserved = module._delete_sections(
            INI_BODY, {TARGET_SECTION, "CrossIBPresent"}, shared
        )
        self.assertEqual([TARGET_SECTION], deleted)
        self.assertEqual(["CrossIBPresent"], preserved)
        self.assertIn("[CrossIBPresent]", updated)
        self.assertNotIn("drawindexed = 1260", updated)
        self.assertIn("CrossIBPresent", module._describe_cross_ib_shared(shared))

    def test_cross_ib_section_without_owner_is_preserved(self):
        registry = self._registry(
            [
                {"name": TARGET_SECTION, "type": "", "draw_ib": DRAW_IB},
                {"name": "CrossIBTextureOverride", "type": "CrossIBTextureOverride", "draw_ib": ""},
            ]
        )

        with _RegistryPatch(registry):
            shared = module._collect_cross_ib_shared_names(
                {TARGET_SECTION, "CrossIBTextureOverride"}, {DRAW_IB}
            )

        self.assertIn("crossibtextureoverride", shared)
        self.assertTrue(shared["crossibtextureoverride"]["unowned_cross_ib"])
        self.assertIn("没有归属 IB", module._describe_cross_ib_shared(shared))

    def test_registry_unavailable_degrades_to_name_only_delete(self):
        with _RegistryPatch(None):
            shared = module._collect_cross_ib_shared_names(
                {TARGET_SECTION, "CrossIBPresent"}, {DRAW_IB}
            )

        self.assertIsNone(shared)
        self.assertIn("退化为", module._REGISTRY_UNAVAILABLE_NOTE)

        updated, deleted, _preserved = module._delete_sections(
            INI_BODY, {TARGET_SECTION, "CrossIBPresent"}, shared
        )
        self.assertEqual(2, len(deleted))
        self.assertNotIn("[CrossIBPresent]", updated)

    def test_sibling_ini_sections_are_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = os.path.join(tmp, "mod.ini")
            sibling = os.path.join(tmp, "Sub.ini")
            Path(target).write_text(INI_BODY, encoding="utf-8")
            Path(sibling).write_text(f"[{TARGET_SECTION}]\nhash = {DRAW_IB}\n", encoding="utf-8")

            hints = module._find_sibling_ini_sections(target, {TARGET_SECTION})

        self.assertEqual([("Sub.ini", [TARGET_SECTION])], hints)


# ---------------------------------------------------------------------------
# 导出执行
# ---------------------------------------------------------------------------

class SkipExecuteTests(unittest.TestCase):
    def setUp(self):
        _log.records.clear()
        _ExportState.target_ini = ""
        _ExportState.model = None
        _ExportState.stages = []

    def _prepare(self, directory, model=None):
        path = os.path.join(directory, "mod.ini")
        Path(path).write_text(INI_BODY, encoding="utf-8")
        _ExportState.target_ini = path
        _ExportState.model = model or _make_model()
        return path

    def test_execute_deletes_sections_appends_block_and_registers(self):
        registry = _FakeRegistry(
            {
                os.path.join("mods", "mod.ini"): [
                    {"name": TARGET_SECTION, "type": "", "draw_ib": DRAW_IB},
                ]
            }
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = self._prepare(tmp)
            node = _make_node()

            with _RegistryPatch(registry):
                self.assertTrue(node.execute_postprocess(tmp))
                first = Path(path).read_text(encoding="utf-8")
                self.assertTrue(Path(path).exists())
                self.assertNotIn("drawindexed = 1260", first)
                self.assertIn("[CrossIBPresent]", first)
                self.assertIn(f"[{SKIP_SECTION}]", first)

                self.assertTrue(node.execute_postprocess(tmp))
                second = Path(path).read_text(encoding="utf-8")

            self.assertEqual(first, second)
            self.assertEqual([(SKIP_SECTION, "TextureOverrideIB", DRAW_IB)], registry.registered[:1])
            self.assertTrue(
                any("删除段 1 个" in text for text in _log.texts("info")), _log.records
            )

    def test_execute_keeps_cross_ib_section_and_warns(self):
        registry = _FakeRegistry(
            {
                os.path.join("mods", "mod.ini"): [
                    {"name": TARGET_SECTION, "type": "", "draw_ib": DRAW_IB},
                    {"name": "CrossIBPresent", "type": "CrossIBPresent", "draw_ib": DRAW_IB},
                    {"name": "CrossIBPresent", "type": "CrossIBPresent", "draw_ib": OTHER_IB},
                ]
            }
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = self._prepare(tmp)

            with _RegistryPatch(registry):
                self.assertTrue(_make_node().execute_postprocess(tmp))

            updated = Path(path).read_text(encoding="utf-8")
            self.assertIn("[CrossIBPresent]", updated)
            self.assertNotIn("drawindexed = 1260", updated)
            warnings = _log.texts("warning")
            self.assertTrue(any("跨 IB 共用" in text and "CrossIBPresent" in text for text in warnings), warnings)

    def test_execute_reports_degraded_registry(self):
        registry = _FakeRegistry(
            {
                os.path.join("mods", "mod.ini"): [
                    {"name": TARGET_SECTION, "type": "", "draw_ib": DRAW_IB},
                ]
            }
        )
        registry.all_error = RuntimeError("registry offline")
        with tempfile.TemporaryDirectory() as tmp:
            path = self._prepare(tmp)

            with _RegistryPatch(registry):
                self.assertTrue(_make_node().execute_postprocess(tmp))

            warnings = _log.texts("warning")
            self.assertTrue(
                any("登记表不可用" in text and "退化为" in text for text in warnings), warnings
            )
            updated = Path(path).read_text(encoding="utf-8")
            self.assertNotIn("drawindexed = 1260", updated)
            self.assertIn(f"[{SKIP_SECTION}]", updated)

    def test_execute_without_registry_only_appends_and_says_so(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._prepare(tmp)

            with _RegistryPatch(None):
                self.assertTrue(_make_node().execute_postprocess(tmp))

            warnings = _log.texts("warning")
            self.assertTrue(
                any("登记表不可用" in text and "只追加跳过段" in text for text in warnings),
                warnings,
            )
            updated = Path(path).read_text(encoding="utf-8")
            # 登记表不可用时圈不到任何段：目标段原样保留，只追加跳过段。
            self.assertIn("drawindexed = 1260", updated)
            self.assertIn(f"[{SKIP_SECTION}]", updated)


if __name__ == "__main__":
    unittest.main()
