"""C-4 覆盖：ZZMI 合并记录「身份查找」分支（最小且可证伪）。

覆盖分支（AST 行号，`ui/universal/zzmi.py`，t36 后快照）：
- `_zzmi_records_for_name`            L451-463  变体命中 → 返回**全部**命中记录
    · L453-455 无变体（空名）→ []
    · L458-460 `source_names` 变体命中 / `source_workspace_names` 变体命中
    · L462-463 多记录并存时全部返回（原对象与 `_copy` 同逻辑源）
- `_zzmi_record_for_object`           L436-448  obj 同一性 / object_name 命中
    · L437-438 obj is None → None；L439-442 名字不可读 → None
- `_zzmi_record_for_drawcall_target`  L466-503  两段式查找
    · L475-478 对象命中即返回
    · **L481-482 对象能解析但不是合并记录 ⇒ 返回 None（源备份不得被升级成目标）**
    · L487-502 对象解析不到时按 `target_workspace_unique_str` / `object_name`
      前缀回退

注：审计点名的 `_zzmi_record_for_name`（单数）是 t36 已确认并删除的**死代码**
（AST 引用 0、全仓字符串 0），本文件改覆盖其**相邻活分支** `_zzmi_records_for_name`
（复数，L451-463），不为死代码写测试。
"""
import importlib.util
import sys
import types
import unittest
from pathlib import Path

from tests import _real_modules

REPO_ROOT = Path(__file__).resolve().parents[1]
PKG = "zzmi_identity_lookup_test_pkg"


def _install_module(name, **attrs):
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


for package_name in (
    PKG,
    f"{PKG}.ui",
    f"{PKG}.ui.universal",
    f"{PKG}.common",
    f"{PKG}.utils",
):
    package = _install_module(package_name)
    package.__path__ = []

_real_modules.register_real_common_modules(f"{PKG}.common")


def _load_real_module(qualname, relpath):
    path = REPO_ROOT / relpath
    spec = importlib.util.spec_from_file_location(qualname, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[qualname] = module
    spec.loader.exec_module(module)
    return module


class _FakeObjects:
    """`bpy.data.objects` 的极简替身：只要 `.get(name)`。"""

    def __init__(self, objects=()):
        self._by_name = {obj.name: obj for obj in objects}

    def get(self, name):
        return self._by_name.get(str(name))


class _FakeBpyData:
    def __init__(self, objects=()):
        self.objects = _FakeObjects(objects)


# bpy 替身必须在加载真实 common 子模块之前装好（object_prefix_helper 顶层 import bpy）
_FAKE_DATA = _FakeBpyData()
_install_module("bpy", data=_FAKE_DATA, types=types.SimpleNamespace())

_load_real_module(f"{PKG}.utils.json_utils", "utils/json_utils.py")
_load_real_module(f"{PKG}.utils.tbn_codec", "utils/tbn_codec.py")
_load_real_module(f"{PKG}.utils.format_utils", "utils/format_utils.py")
_load_real_module(f"{PKG}.utils.ssmt_error_utils", "utils/ssmt_error_utils.py")
_load_real_module(f"{PKG}.common.m_key", "common/m_key.py")
_load_real_module(
    f"{PKG}.common.object_prefix_helper", "common/object_prefix_helper.py"
)
_load_real_module(f"{PKG}.common.draw_call_model", "common/draw_call_model.py")

_install_module(f"{PKG}.utils.timer_utils", TimerUtils=types.SimpleNamespace())
_install_module(
    f"{PKG}.common.global_config",
    GlobalConfig=types.SimpleNamespace(),
)
_install_module(
    f"{PKG}.common.global_properties",
    GlobalProterties=types.SimpleNamespace(),
)
_install_module(
    f"{PKG}.common.global_key_count_helper",
    GlobalKeyCountHelper=types.SimpleNamespace(generated_mod_number=0),
)
_install_module(
    f"{PKG}.common.m_ini_builder",
    M_IniBuilder=type("M_IniBuilder", (), {}),
    M_IniSection=type("M_IniSection", (), {}),
    M_SectionType=types.SimpleNamespace(),
)
_install_module(f"{PKG}.common.m_ini_helper", M_IniHelper=types.SimpleNamespace())
_install_module(
    f"{PKG}.common.m_ini_helper_gui", M_IniHelperGUI=types.SimpleNamespace()
)
_install_module(
    f"{PKG}.ui.universal.unity",
    ExportUnity=type("ExportUnity", (), {"__init__": lambda self, model: None}),
)

_module_path = REPO_ROOT / "ui" / "universal" / "zzmi.py"
_spec = importlib.util.spec_from_file_location(f"{PKG}.ui.universal.zzmi", _module_path)
_zzmi_module = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _zzmi_module
_spec.loader.exec_module(_zzmi_module)

ExportZZMI = _zzmi_module.ExportZZMI


class _UnreadableName:
    """`obj.name` 抛异常的替身（压 L439-442 分支）。"""

    @property
    def name(self):
        raise RuntimeError("name unavailable")


class _FakeDrawCall:
    def __init__(self, obj_name="", source_obj_name="", blender_obj_name=""):
        self.obj_name = obj_name
        self.source_obj_name = source_obj_name
        self._blender_obj_name = blender_obj_name

    def get_blender_obj_name(self):
        return self._blender_obj_name


def _record(**kwargs):
    base = {
        "object": None,
        "object_name": "",
        "source_names": (),
        "source_workspace_names": (),
        "target_workspace_unique_str": "",
    }
    base.update(kwargs)
    return base


class ZZMIRecordsForNameTests(unittest.TestCase):
    """`_zzmi_records_for_name`（L451-463）—— 身份记录查找的活分支。"""

    def test_empty_name_has_no_variants(self):
        # L453-455：空名 → 变体集合为空 → 直接 []（不扫记录）
        records = [_record(source_names=("LOD0.8c8de427-24180-0",))]
        self.assertEqual(ExportZZMI._zzmi_records_for_name("", records), [])
        self.assertEqual(ExportZZMI._zzmi_records_for_name("   ", records), [])

    def test_matches_by_source_name_and_strips_runtime_suffix(self):
        # L458-460：`_copy`/`.ZZMI_SOURCE` 后缀剥离后仍命中同一逻辑源
        record = _record(source_names=("LOD0.8c8de427-24180-0",))
        self.assertEqual(
            ExportZZMI._zzmi_records_for_name("LOD0.8c8de427-24180-0_copy", [record]),
            [record],
        )
        self.assertEqual(
            ExportZZMI._zzmi_records_for_name(
                "LOD0.8c8de427-24180-0.ZZMI_SOURCE", [record]
            ),
            [record],
        )

    def test_matches_by_workspace_name_variant(self):
        # L460：第二个命中来源 source_workspace_names（存的是裸工作区名，
        # 输入带 LOD0. 前缀时由 _zzmi_name_variants 剥出裸名后相交命中）
        record = _record(source_workspace_names=("b20f90ea-798-0",))
        self.assertEqual(
            ExportZZMI._zzmi_records_for_name("LOD0.b20f90ea-798-0", [record]),
            [record],
        )

    def test_returns_every_matching_record_when_source_and_copy_coexist(self):
        # L457-463：原对象与 `_copy` 记录并存时**全部**返回（原 docstring 契约）
        original = _record(source_names=("LOD0.a23aa8a3-42759-0",))
        copied = _record(source_names=("LOD0.a23aa8a3-42759-0", "别的来源"))
        other = _record(source_names=("LOD0.b30db54e-7383-0",))
        matched = ExportZZMI._zzmi_records_for_name(
            "LOD0.a23aa8a3-42759-0_copy", [original, copied, other]
        )
        self.assertEqual(matched, [original, copied])

    def test_unmatched_name_returns_empty(self):
        record = _record(source_names=("LOD0.8c8de427-24180-0",))
        self.assertEqual(
            ExportZZMI._zzmi_records_for_name("LOD0.deadbeef-1-0", [record]), []
        )


class ZZMIRecordForObjectTests(unittest.TestCase):
    """`_zzmi_record_for_object`（L436-448）。"""

    def test_none_object_returns_none(self):
        # L437-438
        self.assertIsNone(
            ExportZZMI._zzmi_record_for_object(None, [_record(object_name="x")])
        )

    def test_matches_by_identity_when_names_differ(self):
        # L443-445：`obj is record["object"]` 命中该记录（同名单靠 object_name 分支）
        first = types.SimpleNamespace(name="LOD0.a23aa8a3-1-0")
        second = types.SimpleNamespace(name="LOD0.b20f90ea-1-0")
        record_first = _record(object=first, object_name="LOD0.a23aa8a3-1-0")
        record_second = _record(object=second, object_name="LOD0.b20f90ea-1-0")
        self.assertIs(
            ExportZZMI._zzmi_record_for_object(
                second, [record_first, record_second]
            ),
            record_second,
        )

    def test_name_match_on_earlier_record_shadows_later_identity(self):
        """冻结**实际的扫描顺序语义**（L443-447 逐记录先同一性后名字）：

        两条记录同名时，前一记录按 object_name 先命中，后一记录的同一性命中到不了。
        记录在此是为了让任何"改成全局先按同一性扫描"的重构被显式暴露。
        """
        first = types.SimpleNamespace(name="same")
        second = types.SimpleNamespace(name="same")
        record_first = _record(object=first, object_name="same")
        record_second = _record(object=second, object_name="same")
        self.assertIs(
            ExportZZMI._zzmi_record_for_object(second, [record_first, record_second]),
            record_first,
        )

    def test_matches_by_object_name(self):
        # L446-447
        obj = types.SimpleNamespace(name="LOD0.8c8de427-24180-0")
        record = _record(object_name="LOD0.8c8de427-24180-0")
        self.assertIs(ExportZZMI._zzmi_record_for_object(obj, [record]), record)

    def test_unreadable_name_returns_none(self):
        # L439-442：obj.name 抛异常 → None（不得让身份修复阻断导出）
        self.assertIsNone(
            ExportZZMI._zzmi_record_for_object(
                _UnreadableName(), [_record(object_name="x")]
            )
        )


class ZZMIRecordForDrawcallTargetTests(unittest.TestCase):
    """`_zzmi_record_for_drawcall_target`（L466-503）。"""

    def setUp(self):
        _FAKE_DATA.objects = _FakeObjects()

    def test_resolves_target_through_the_live_object(self):
        # L475-478：对象可解析且命中记录 → 返回该记录
        obj = types.SimpleNamespace(name="LOD0.a23aa8a3-42759-0")
        _FAKE_DATA.objects = _FakeObjects([obj])
        record = _record(
            object=obj,
            object_name="LOD0.a23aa8a3-42759-0",
            target_workspace_unique_str="LOD0.a23aa8a3-42759-0",
        )
        draw_call = _FakeDrawCall(
            obj_name="LOD0.a23aa8a3-42759-0", blender_obj_name="LOD0.a23aa8a3-42759-0"
        )
        self.assertIs(
            ExportZZMI._zzmi_record_for_drawcall_target(draw_call, [record]), record
        )

    def test_resolved_non_merged_object_is_not_upgraded_to_target(self):
        """**关键回归守卫**（L479-482）：能解析到真实但非合并源对象 ⇒ None。

        形态：源备份 DrawCall 的名字带合并目标的逻辑前缀（`.ZZMI_SOURCE` 后缀被
        `_zzmi_name_variants` 剥掉后正好等于目标前缀），但它解析到的是一个**真实的
        源部件对象**、不是记录里的合并对象。若删掉 L481-482 的守卫，逻辑前缀回退会
        把它升级成目标；目标缺失时整条导出链会被错误过滤。
        """
        obj = types.SimpleNamespace(name="LOD0.a23aa8a3-42759-0.ZZMI_SOURCE")
        _FAKE_DATA.objects = _FakeObjects([obj])
        merged_obj = types.SimpleNamespace(name="LOD0.a23aa8a3-42759-0")
        record = _record(
            object=merged_obj,
            object_name="LOD0.a23aa8a3-42759-0",
            target_workspace_unique_str="LOD0.a23aa8a3-42759-0",
            source_names=("LOD0.b30db54e-7383-0",),
        )
        draw_call = _FakeDrawCall(
            obj_name="LOD0.a23aa8a3-42759-0.ZZMI_SOURCE",
            blender_obj_name="LOD0.a23aa8a3-42759-0.ZZMI_SOURCE",
        )
        self.assertIsNone(
            ExportZZMI._zzmi_record_for_drawcall_target(draw_call, [record])
        )

    def test_falls_back_to_target_prefix_when_object_is_unresolved(self):
        # L483-502：解析不到对象 → 按 target_workspace_unique_str / object_name 前缀
        record = _record(
            object_name="LOD0.a23aa8a3-42759-0",
            target_workspace_unique_str="LOD0.a23aa8a3-42759-0",
        )
        by_obj_name = _FakeDrawCall(obj_name="LOD0.a23aa8a3-42759-0")
        self.assertIs(
            ExportZZMI._zzmi_record_for_drawcall_target(by_obj_name, [record]),
            record,
        )
        by_source_name = _FakeDrawCall(source_obj_name="a23aa8a3-42759-0")
        self.assertIs(
            ExportZZMI._zzmi_record_for_drawcall_target(by_source_name, [record]),
            record,
        )

    def test_prefix_fallback_ignores_unrelated_drawcall(self):
        record = _record(
            object_name="LOD0.a23aa8a3-42759-0",
            target_workspace_unique_str="LOD0.a23aa8a3-42759-0",
        )
        draw_call = _FakeDrawCall(obj_name="LOD0.b20f90ea-19182-0")
        self.assertIsNone(
            ExportZZMI._zzmi_record_for_drawcall_target(draw_call, [record])
        )


if __name__ == "__main__":
    unittest.main()
