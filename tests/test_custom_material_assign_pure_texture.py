# -*- coding: utf-8 -*-
"""材质转资源pro「纯贴图模式」与「条件判断」门控的对接契约。

被测契约
--------
1. 纯贴图段自带顶层 ``Resource\\<ns>\\<槽位> = null`` 兜底行，且位于 ``= ref``
   切换块之前（条件关闭时回落原生贴图，不残留同帧别的部件贴图）。
2. 生成后把段登记进 ``common/export_section_registry``（段类型
   ``TextureOverrideMaterialPureTexture``、draw_ib = 该 IB hash），条件判断节点
   才能按「物体 -> DrawIB -> 段名」反查到它。
3. 登记过的纯贴图段不会被 ``_pure_texture_conflict_with_export`` 误判成
   「该 IB 在导出链上有网格段」：同一进程里第二次生成仍然重建段，不会删旧段后
   不再生成。
4. 同一 IB 的多个物体各自生成独立段（``_7fbbcf0d`` / ``_7fbbcf0d_2``），全部登记。
5. 开「使用全局指定」后照样生成纯贴图段：主路径只看 ini 里已有的 mesh 段，导出链外的
   物体只能靠这条路径补段，不能在全局模式下被整体关掉。
6. 这些物体的贴图切换组取自它们自己的 ``target_items[].switch_groups``：全局模式下
   ``global_switch_groups`` 是链路部件的扫描结果，和导出链外的物体不是同一批。

装载沿用 ``tests/test_custom_material_assign_clear_textures.py`` 的 fake-package 范式。
"""

import importlib.util
import json
import os
import sys
import tempfile
import types
import unittest
from collections import OrderedDict
from pathlib import Path

PKG = "_pure_texture_gate_test_pkg"
for _package_name in (PKG, f"{PKG}.blueprint", f"{PKG}.common", f"{PKG}.utils"):
    _package = types.ModuleType(_package_name)
    _package.__path__ = []
    sys.modules[_package_name] = _package


# ---------------------------------------------------------------------------
# 假 bpy
# ---------------------------------------------------------------------------

_fake_bpy = types.ModuleType("bpy")
_fake_bpy.types = types.SimpleNamespace(
    PropertyGroup=object,
    Operator=object,
    Node=object,
    NodeSocket=object,
    UIList=object,
    Object=object,
)
_fake_bpy.props = types.SimpleNamespace(
    StringProperty=lambda **_kwargs: "",
    BoolProperty=lambda **_kwargs: False,
    IntProperty=lambda **_kwargs: 0,
    CollectionProperty=lambda **_kwargs: [],
    PointerProperty=lambda **_kwargs: None,
    EnumProperty=lambda **_kwargs: "",
    FloatProperty=lambda **_kwargs: 0.0,
)
_fake_bpy.data = types.SimpleNamespace(objects={}, node_groups={})
_fake_bpy.context = types.SimpleNamespace(window_manager=None)
_fake_bpy.ops = types.SimpleNamespace()
_fake_bpy.utils = types.SimpleNamespace(
    register_class=lambda _cls: None, unregister_class=lambda _cls: None
)
sys.modules["bpy"] = _fake_bpy


# ---------------------------------------------------------------------------
# 段登记表替身（模块级函数，供 ``from ..common import export_section_registry``）
# ---------------------------------------------------------------------------

_REGISTRY_ENTRIES = []


def _register_section(section_name, section_type="", draw_ib="", ini_path=""):
    key = (str(section_name), str(section_type), str(draw_ib))
    if any((e["name"], e["type"], e["draw_ib"]) == key for e in _REGISTRY_ENTRIES):
        return
    _REGISTRY_ENTRIES.append(
        {
            "name": str(section_name),
            "type": str(section_type),
            "draw_ib": str(draw_ib),
            "ini_path": str(ini_path),
        }
    )


def _sections_for_draw_ib(draw_ib, ini_path=""):
    wanted = str(draw_ib or "").casefold()
    return [dict(e) for e in _REGISTRY_ENTRIES if e["draw_ib"].casefold() == wanted]


def _sections_for_draw_ibs(draw_ibs, ini_path=""):
    wanted = {str(value or "").casefold() for value in (draw_ibs or ())}
    return [dict(e) for e in _REGISTRY_ENTRIES if e["draw_ib"].casefold() in wanted]


_registry_module = types.ModuleType(f"{PKG}.common.export_section_registry")
_registry_module.register_section = _register_section
_registry_module.get_sections_for_draw_ib = _sections_for_draw_ib
_registry_module.get_sections_for_draw_ibs = _sections_for_draw_ibs
_registry_module.get_all = lambda: {}
_registry_module.get_ini_paths = lambda: []
sys.modules[_registry_module.__name__] = _registry_module


# ---------------------------------------------------------------------------
# 材质基类替身（只补被测路径用到的设施）
# ---------------------------------------------------------------------------

def _parse_ini_content(content):
    preamble_lines = []
    sections = OrderedDict()
    current_section = None
    for line in str(content or "").splitlines():
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            current_section = stripped
            sections.setdefault(current_section, [])
        elif current_section is None:
            preamble_lines.append(line)
        elif stripped:
            sections[current_section].append(line)
    return preamble_lines, sections


def _serialize_ini_content(
    preamble_lines,
    sections,
    transparency_sections=None,
    preserved_tail_content="",
    preserved_driver_content="",
):
    new_content = []
    if preserved_driver_content:
        new_content.append(preserved_driver_content.rstrip())
        new_content.append("")
    new_content.extend(preamble_lines or [])
    if new_content and sections and new_content[-1].strip():
        new_content.append("")
    for section_name, lines in sections.items():
        new_content.append(section_name)
        new_content.extend(lines)
        new_content.append("")
    if preserved_tail_content:
        new_content.append("")
        new_content.append(preserved_tail_content)
    return "\n".join(new_content)


class _StubMaterialBase:
    TRANSPARENCY_SECTION_MARKER = ";MARK:TestTransparency"

    @staticmethod
    def _parse_ini_content(content):
        return _parse_ini_content(content)

    @classmethod
    def _serialize_ini_content(cls, preamble_lines, sections, **kwargs):
        return _serialize_ini_content(preamble_lines, sections, **kwargs)

    @classmethod
    def split_anim_driver_block_content(cls, content):
        return "", str(content or "")

    @classmethod
    def split_auto_appended_tail_content(cls, content):
        return str(content or ""), ""

    @classmethod
    def _strip_previous_transparency_sections(cls, content):
        return str(content or "")

    def _prepare_material_group_switch_map(self, mapping):
        mapping.clear()

    def _max_ui_switch_number(self):
        return 0

    def _iter_switch_group_defs(self):
        return []

    def define_swapkeys_in_sections(self, sections, used_swap_keys):
        """纯贴图模式会在段里声明 global persist；这里只记录，不实际写入。"""
        return len(used_swap_keys)

    def find_matching_materials(self, obj, texture_type):
        if texture_type == "DiffuseMap":
            return [
                types.SimpleNamespace(name="DiffuseMap_A"),
                types.SimpleNamespace(name="DiffuseMap_B"),
            ]
        return []

    def generate_material_lines(
        self,
        matching_materials,
        param_name,
        texture_type,
        obj,
        texture_folder,
        all_sections,
        object_to_diffuse_swapkey,
        material_group_to_swapkey,
        swap_key_prefix,
        next_swap_key_num,
        used_swap_keys,
        resource_name_provider=None,
    ):
        used_swap_keys.add("$swapkey150")
        return (
            [
                f"; {texture_type} 材质切换 (组: $swapkey150)",
                "if $swapkey150 == 0",
                f"    {param_name} = ref Resource_A",
                "endif",
                "if $swapkey150 == 1",
                f"    {param_name} = ref Resource_B",
                "endif",
                "",
            ],
            next_swap_key_num,
        )


_material_module = types.ModuleType(f"{PKG}.blueprint.node_postprocess_material")
_material_module.MATERIAL_DETECT_PRESETS = [
    "DiffuseMap",
    "NormalMap",
    "LightMap",
    "MaterialMap",
]
_material_module.SSMTNode_PostProcess_MaterialBase = _StubMaterialBase
_material_module.resolve_keyswap_key = lambda *args, **kwargs: ""
sys.modules[_material_module.__name__] = _material_module


# ---------------------------------------------------------------------------
# 加载被测模块
# ---------------------------------------------------------------------------

_module_path = (
    Path(__file__).resolve().parents[1]
    / "blueprint"
    / "node_postprocess_custom_material_assign.py"
)
_spec = importlib.util.spec_from_file_location(
    f"{PKG}.blueprint.node_postprocess_custom_material_assign", _module_path
)
module = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = module
_spec.loader.exec_module(module)

#: 纯贴图模式用到的切换变量基址（真实实现从节点属性解析，这里固定）。
module._parse_switch_var_base = lambda _node: ("$swapkey", 150)


class _FakeNode(module.SSMTNode_PostProcess_CustomMaterialAssign):
    """借真实类的方法，只替换 bpy 收集来的部件列表与槽位表。"""

    def __init__(
        self,
        target_names,
        global_mode=False,
        item_groups=None,
        global_groups=None,
    ):
        item_groups = list(item_groups or [])
        self.target_items = []
        for index, name in enumerate(target_names):
            groups = item_groups[index] if index < len(item_groups) else []
            self.target_items.append(
                types.SimpleNamespace(
                    target_object=types.SimpleNamespace(name=name, type="MESH"),
                    switch_groups=list(groups or []),
                )
            )
        self.global_switch_groups = list(global_groups or [])
        self.use_global_assign = bool(global_mode)
        self.clear_non_target_textures = False
        self.material_switch_var = "$swapkey"

    @staticmethod
    def _pure_texture_slot_layout():
        return (
            module._MATERIAL_PURE_TEXTURE_SLOTS_ZZMI,
            module._MATERIAL_PURE_TEXTURE_SET_TEXTURES_ZZMI,
        )

    # 真实类在 pro 里重写了这两个入口（加目标过滤 / 禁用切换过滤）；
    # 本测试只验证纯贴图模式的产物，直接退回基类替身的行为。
    def find_matching_materials(self, obj, texture_type):
        return _StubMaterialBase.find_matching_materials(self, obj, texture_type)

    def generate_material_lines(self, *args, **kwargs):
        return _StubMaterialBase.generate_material_lines(self, *args, **kwargs)


BASE_INI = "[Constants]\nglobal persist $mod_enabled = 1\n"


def _switch_group(variable, bindings, key="N", enabled=True, comment="", object_name=""):
    """造一个切换组（``bindings`` 在真实节点上存的是 JSON 字符串）。"""
    names = [list(binding) for binding in bindings]
    return types.SimpleNamespace(
        switch_variable=variable,
        bindings=json.dumps(names, ensure_ascii=False),
        key=key,
        enabled=enabled,
        state_count=max(len(binding) for binding in names),
        comment=comment,
        object_name=object_name,
        merge_group_id=variable,
    )


class PureTextureSectionTests(unittest.TestCase):
    def setUp(self):
        _REGISTRY_ENTRIES.clear()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = self._tmp.name
        self.ini_path = os.path.join(self.root, "mod.ini")
        with open(self.ini_path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(BASE_INI)

    def _run(self, target_names, **kwargs):
        node = _FakeNode(list(target_names), **kwargs)
        return node._apply_pure_texture_sections(self.root)

    def _content(self):
        with open(self.ini_path, "r", encoding="utf-8") as handle:
            return handle.read()

    def test_generates_null_fallbacks_and_registers_section(self):
        count = self._run(["LOD0.7fbbcf0d-7116-0.脸"])

        self.assertEqual(1, count)
        content = self._content()
        self.assertIn("[TextureOverride_MaterialPureTexture_7fbbcf0d]", content)
        self.assertIn("hash = 7fbbcf0d", content)
        self.assertIn("Resource\\ZZMI\\Diffuse = null", content)
        self.assertIn("run = CommandList\\ZZMI\\SetTextures", content)
        # null 兜底必须在 ref 之前
        self.assertLess(
            content.index("Resource\\ZZMI\\Diffuse = null"),
            content.index("Resource\\ZZMI\\Diffuse = ref Resource_A"),
        )
        # 段必须登记（否则条件判断节点反查不到、无法门控）
        self.assertEqual(
            [
                {
                    "name": "TextureOverride_MaterialPureTexture_7fbbcf0d",
                    "type": "TextureOverrideMaterialPureTexture",
                    "draw_ib": "7fbbcf0d",
                    "ini_path": self.ini_path,
                }
            ],
            [
                {
                    "name": entry["name"],
                    "type": entry["type"],
                    "draw_ib": entry["draw_ib"],
                    "ini_path": entry["ini_path"],
                }
                for entry in _REGISTRY_ENTRIES
            ],
        )

    def test_second_run_regenerates_and_does_not_duplicate(self):
        self._run(["LOD0.7fbbcf0d-7116-0.脸"])
        self._run(["LOD0.7fbbcf0d-7116-0.脸"])

        content = self._content()
        # 登记过的纯贴图段不能被当成「导出链上的网格段」而跳过重新生成
        self.assertEqual(1, content.count("[TextureOverride_MaterialPureTexture_7fbbcf0d]"))
        self.assertIn("hash = 7fbbcf0d", content)
        self.assertIn("Resource\\ZZMI\\Diffuse = ref Resource_A", content)

    def test_two_objects_same_ib_get_separate_sections_all_registered(self):
        count = self._run(
            ["LOD0.7fbbcf0d-7116-0.脸", "LOD0.7fbbcf0d-1260-9402.脸"]
        )

        self.assertEqual(2, count)
        content = self._content()
        self.assertIn("[TextureOverride_MaterialPureTexture_7fbbcf0d]", content)
        self.assertIn("[TextureOverride_MaterialPureTexture_7fbbcf0d_2]", content)
        names = sorted(entry["name"] for entry in _REGISTRY_ENTRIES)
        self.assertEqual(
            [
                "TextureOverride_MaterialPureTexture_7fbbcf0d",
                "TextureOverride_MaterialPureTexture_7fbbcf0d_2",
            ],
            names,
        )

    def test_registered_pure_section_is_not_export_conflict(self):
        _register_section(
            "TextureOverride_MaterialPureTexture_7fbbcf0d",
            section_type="TextureOverrideMaterialPureTexture",
            draw_ib="7fbbcf0d",
        )
        self.assertFalse(
            module.SSMTNode_PostProcess_CustomMaterialAssign._pure_texture_conflict_with_export(
                {}, "7fbbcf0d"
            )
        )

        # 同一 IB 真的产出网格段时仍然判冲突（纯贴图模式必须让位）
        _register_section("TextureOverride_IB_7fbbcf0d", draw_ib="7fbbcf0d")
        self.assertTrue(
            module.SSMTNode_PostProcess_CustomMaterialAssign._pure_texture_conflict_with_export(
                {}, "7fbbcf0d"
            )
        )


class GlobalModePureTextureTests(unittest.TestCase):
    """「使用全局指定」打开后，导出链外的物体照样要出贴图替换段。"""

    def setUp(self):
        _REGISTRY_ENTRIES.clear()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = self._tmp.name
        self.ini_path = os.path.join(self.root, "mod.ini")
        with open(self.ini_path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(BASE_INI)

    def _run(self, target_names, **kwargs):
        node = _FakeNode(list(target_names), **kwargs)
        return node._apply_pure_texture_sections(self.root)

    def _content(self):
        with open(self.ini_path, "r", encoding="utf-8") as handle:
            return handle.read()

    def test_global_mode_still_generates_sections(self):
        count = self._run(["LOD0.7fbbcf0d-7116-0.脸"], global_mode=True)

        self.assertEqual(1, count)
        content = self._content()
        self.assertIn("[TextureOverride_MaterialPureTexture_7fbbcf0d]", content)
        self.assertIn("Resource\\ZZMI\\Diffuse = ref Resource_A", content)

    def test_global_mode_without_extra_objects_does_nothing(self):
        self.assertEqual(0, self._run([], global_mode=True))
        self.assertEqual(BASE_INI, self._content())

    def test_global_mode_skips_objects_that_are_exported(self):
        # 该 IB 已经有网格段（主路径处理），纯贴图段必须让出去
        _register_section("TextureOverride_IB_7fbbcf0d", draw_ib="7fbbcf0d")

        self.assertEqual(0, self._run(["LOD0.7fbbcf0d-7116-0.脸"], global_mode=True))
        self.assertNotIn("MaterialPureTexture", self._content())

    def test_global_mode_takes_switch_groups_from_the_object_itself(self):
        module.resolve_keyswap_key = lambda *args, **kwargs: "N"
        self.addCleanup(setattr, module, "resolve_keyswap_key", lambda *a, **k: "")
        item_group = _switch_group(
            "$swapkey150", [["DiffuseMap_A"], ["DiffuseMap_B"]]
        )
        chain_group = _switch_group(
            "$swapkey160", [["BodyMap_C"], ["BodyMap_D"]], key="M"
        )

        count = self._run(
            ["LOD0.7fbbcf0d-7116-0.脸"],
            global_mode=True,
            item_groups=[[item_group]],
            global_groups=[chain_group],
        )

        self.assertEqual(1, count)
        content = self._content()
        self.assertIn("[KeySwap_Diffuse_swapkey150]", content)
        self.assertNotIn("[KeySwap_Diffuse_swapkey160]", content)

    def test_merged_switch_map_separates_global_and_object_groups(self):
        node = _FakeNode(
            ["LOD0.7fbbcf0d-7116-0.脸"],
            global_mode=True,
            item_groups=[
                [_switch_group("$swapkey150", [["DiffuseMap_A"], ["DiffuseMap_B"]])]
            ],
            global_groups=[
                [_switch_group("$swapkey160", [["BodyMap_C"], ["BodyMap_D"]])]
            ][0],
        )
        mapping = {}
        node._prepare_material_group_switch_map(mapping)
        self.assertEqual("$swapkey160", mapping[("BodyMap_C",)])

        node._merge_object_switch_groups(node.target_items[0], mapping)

        self.assertEqual("$swapkey150", mapping[("DiffuseMap_A",)])
        # 档位顺序跟着物体自己的组走，否则第 N 档会和面板上的第 N 档错开
        self.assertEqual(
            ["DiffuseMap_A"],
            node._material_group_order[("DiffuseMap_A",)],
        )


if __name__ == "__main__":
    unittest.main()
