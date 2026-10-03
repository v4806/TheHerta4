"""t80 §2.2/§2.3 **阻断修复**的可证伪用例（不装载整份 export_parallel）。

被审现象（真实导出实测 `review-reports/t80-export-test-and-diff.md`）：

1. `blueprint/export_parallel.py:242`（与 HEAD 逐字节相同）在「蓝图声明 19 件 /
   场景只有 4 个 mesh」时抛 `ParallelExportError: 前处理错误：物体引用未正确
   更新为副本`；抛点在 `ExportZZMI` 构造之前 ⇒ 生成器自己的「极限小三角占位」
   机制**一行都没机会执行**，导出直接死。
2. 即使放行，`ExportZZMI._ensure_stub_objects_for_missing_parts` 的 `present`
   取自**蓝图声明名** ⇒ 15 个"声明了但场景无对象"的部件被判成存在 ⇒ 不插桩
   ⇒ 紧随其后的 `SubMeshModel` 找不到 Blender 对象 `Fatal`。

**为什么用两份源码断言而不是装载模块**：`export_parallel.py` 的 import 面包含
全部导出器与 bpy，装载它会让用例依赖半个插件；而缺陷本身有**两个可直接检查的
性质**：① 守卫里存在"源对象不存在 ⇒ 豁免"的判据；② 该判据调用的是场景真值
谓词 `scene_object_present`（而不是声明名集合）。本文件对①做**语义级**复算
（用真实谓词 + 与生产同构的判定顺序），对②做**真实实现**的行为断言。
"""
import importlib.util
import re
import sys
import types
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PKG = "zzmi_t80_blockers_test_pkg"


def _install(name, **attrs):
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


class _ObjectsRegistry:
    def __init__(self):
        self._items = {}

    def new(self, name):
        obj = types.SimpleNamespace(name=name, data=None, type="MESH", _custom={})
        obj.get = lambda key, default=None, _o=obj: _o._custom.get(key, default)
        self._items[name] = obj
        return obj

    def get(self, name):
        return self._items.get(name)

    def __iter__(self):
        return iter(list(self._items.values()))


OBJECTS = _ObjectsRegistry()

for package_name in (PKG, f"{PKG}.blueprint", f"{PKG}.common", f"{PKG}.utils"):
    package = _install(package_name)
    package.__path__ = []

_install("bpy", data=types.SimpleNamespace(objects=OBJECTS, meshes=_ObjectsRegistry()))
_install(
    f"{PKG}.common.global_config",
    GlobalConfig=types.SimpleNamespace(path_generate_mod_folder=lambda: ""),
)
_install(
    f"{PKG}.common.global_properties",
    GlobalProterties=types.SimpleNamespace(
        enable_parallel_preprocess=lambda: False,
        enable_parallel_export_rounds=lambda: False,
    ),
)
_install(f"{PKG}.common.m_key", M_Key=types.SimpleNamespace())
_install(f"{PKG}.common.object_prefix_helper", ObjectPrefixHelper=types.SimpleNamespace())
_install(f"{PKG}.utils.shapekey_utils", ShapeKeyUtils=types.SimpleNamespace())


def _load(qualname, relpath):
    spec = importlib.util.spec_from_file_location(qualname, REPO_ROOT / relpath)
    module = importlib.util.module_from_spec(spec)
    sys.modules[qualname] = module
    spec.loader.exec_module(module)
    return module


BlueprintExportHelper = _load(
    f"{PKG}.blueprint.export_helper", "blueprint/export_helper.py"
).BlueprintExportHelper

_PARALLEL_SOURCE = (REPO_ROOT / "blueprint" / "export_parallel.py").read_text(
    encoding="utf-8"
)


def _validate_copy_references_body() -> str:
    """取出生产 `validate_copy_references` 的源码正文（缩进已剥到函数级）。"""
    marker = "    def validate_copy_references(blueprint_model: BluePrintModel):"
    start = _PARALLEL_SOURCE.index(marker)
    rest = _PARALLEL_SOURCE[start + len(marker):]
    lines = rest.splitlines()
    body = []
    for line in lines[1:]:
        if line.strip() and not line.startswith("        "):
            break
        body.append(line[8:] if line.startswith("        ") else "")
    return "\n".join(body)


class SceneObjectPresentTests(unittest.TestCase):
    """② 修复的真实实现：谓词必须查**场景真值**，不是声明名集合。"""

    def setUp(self):
        OBJECTS._items.clear()

    def test_missing_object_is_not_present(self):
        self.assertFalse(
            BlueprintExportHelper.scene_object_present("LOD0.aaaa1111-1-0"),
            "场景里没有这个对象时必须判为不存在（否则占位机制不可达）",
        )

    def test_real_object_is_present(self):
        OBJECTS.new("LOD0.aaaa1111-1-0")
        self.assertTrue(BlueprintExportHelper.scene_object_present("LOD0.aaaa1111-1-0"))

    def test_suffix_variants_resolve_to_the_same_logical_object(self):
        OBJECTS.new("LOD0.aaaa1111-1-0_copy")
        self.assertTrue(
            BlueprintExportHelper.scene_object_present("LOD0.aaaa1111-1-0"),
            "前处理把对象改名为副本后，声明名也必须能通过后缀变体解析到场景对象",
        )
        OBJECTS._items.clear()
        OBJECTS.new("LOD0.aaaa1111-1-0.ZZMI_SOURCE")
        self.assertTrue(BlueprintExportHelper.scene_object_present("LOD0.aaaa1111-1-0"))

    def test_any_of_several_names_matches(self):
        OBJECTS.new("LOD0.bbbb2222-2-0")
        self.assertTrue(
            BlueprintExportHelper.scene_object_present(
                "LOD0.aaaa1111-1-0", "LOD0.bbbb2222-2-0"
            )
        )


class ValidateCopyReferencesSourceTests(unittest.TestCase):
    """① 生产守卫的源码性质 + 与场景真值谓词的同构复算。"""

    def setUp(self):
        OBJECTS._items.clear()
        self.body = _validate_copy_references_body()

    def _decide(self, chains, copy_names, name_mapping=None):
        """按生产判定的**顺序**复算：早期 continue 分支 + 缺席豁免 + 真错误。"""
        reverse_rename = {v: k for k, v in (name_mapping or {}).items()}
        invalid = []
        absent = []
        for obj_name, original_name in chains:
            if obj_name in copy_names:
                continue
            pre_rename = reverse_rename.get(obj_name)
            if pre_rename and pre_rename in copy_names:
                continue
            if original_name and original_name in copy_names:
                continue
            if not BlueprintExportHelper.scene_object_present(obj_name, original_name):
                absent.append(obj_name)
                continue
            invalid.append(obj_name)
        return invalid, absent

    def test_guard_consults_the_scene_truth_predicate(self):
        """守卫必须显式调用 `scene_object_present` 并区分「缺席」与「真错误」。"""
        self.assertIn(
            "scene_object_present(",
            self.body,
            "守卫必须用场景真值谓词判定「源对象是否存在」",
        )
        self.assertIn("absent_objects", self.body)
        self.assertIn("invalid_objects", self.body)

    def test_absent_source_object_is_exempted(self):
        """源对象**不存在** ⇒ 豁免（交占位机制），不得进 invalid。

        可证伪：修复前守卫把这类链路直接算 invalid ⇒ 真实工程 15/19 件时必抛
        `前处理错误：物体引用未正确更新为副本`，导出在 ExportZZMI 之前就死。
        """
        invalid, absent = self._decide(
            [("LOD0.8c8de427-24180-0", "LOD0.8c8de427-798-0")],
            copy_names={"LOD0.src-1-0_copy"},
        )
        self.assertEqual(invalid, [])
        self.assertEqual(absent, ["LOD0.8c8de427-24180-0"])

    def test_present_source_without_copy_is_still_invalid(self):
        """反向钉住：源对象**存在**却拿不到副本 ⇒ 仍是错误（不得放宽成一律豁免）。"""
        OBJECTS.new("LOD0.8c8de427-798-0")
        invalid, absent = self._decide(
            [("LOD0.8c8de427-798-0", "")],
            copy_names={"LOD0.src-1-0_copy"},
        )
        self.assertEqual(absent, [])
        self.assertEqual(invalid, ["LOD0.8c8de427-798-0"])

    def test_copy_reference_is_accepted(self):
        invalid, absent = self._decide(
            [("LOD0.src-1-0_copy", "LOD0.src-1-0")],
            copy_names={"LOD0.src-1-0_copy"},
        )
        self.assertEqual((invalid, absent), ([], []))

    def test_absent_chain_is_not_reported_as_invalid_in_the_real_body(self):
        """源码级钉住：`absent_objects` 必须**先于** `invalid_objects.append` 生效。"""
        absent_index = self.body.index("absent_objects.append")
        invalid_index = self.body.index("invalid_objects.append")
        self.assertLess(
            absent_index,
            invalid_index,
            "缺席豁免必须排在「记为无效引用」之前，否则源对象不存在的链路仍会被拒",
        )


if __name__ == "__main__":
    unittest.main()
