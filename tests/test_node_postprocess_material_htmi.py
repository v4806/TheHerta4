# -*- coding: utf-8 -*-
import importlib.util
import json
import os
import sys
import tempfile
import types
import unittest
from collections import OrderedDict
from pathlib import Path
from unittest import mock


def _install_module(name, **attrs):
    """安装 Fake 模块到 sys.modules"""
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


PKG = "_node_postprocess_material_htmi_test_pkg"
for package_name in (PKG, f"{PKG}.blueprint", f"{PKG}.utils", f"{PKG}.common", f"{PKG}.toolkit"):
    package = _install_module(package_name)
    package.__path__ = []


class _FakeBpyDataObjects(dict):
    pass


_fake_bpy = types.SimpleNamespace(
    types=types.SimpleNamespace(PropertyGroup=object, Operator=object),
    props=types.SimpleNamespace(
        StringProperty=lambda **_kwargs: None,
        BoolProperty=lambda **_kwargs: None,
        IntProperty=lambda **_kwargs: None,
        CollectionProperty=lambda **_kwargs: None,
    ),
    data=types.SimpleNamespace(objects=_FakeBpyDataObjects()),
    path=types.SimpleNamespace(abspath=lambda value: value),
    utils=types.SimpleNamespace(register_class=lambda _cls: None, unregister_class=lambda _cls: None),
)
_install_module("bpy", **_fake_bpy.__dict__)
_install_module(
    f"{PKG}.blueprint.node_postprocess_base",
    SSMTNode_PostProcess_Base=type(
        "_FakePostProcessBase",
        (object,),
        {
            "AUTO_APPENDED_SECTION_MARKERS": (
                "; --- AUTO-APPENDED SLIDER CONTROL PANEL ---",
                "; --- AUTO-APPENDED HEALTH DETECTION MODULE ---",
            ),
            "split_auto_appended_tail_content": classmethod(lambda cls, content: (content, "")),
            "split_anim_driver_block_content": classmethod(lambda cls, content: ("", content)),
        },
    ),
)
_install_module(
    f"{PKG}.utils.log_utils",
    LOG=types.SimpleNamespace(
        debug=lambda *_args, **_kwargs: None,
        info=lambda *_args, **_kwargs: None,
        warning=lambda *_args, **_kwargs: None,
    ),
)
_prefix_cache_state = {"props": {}}
_install_module(
    f"{PKG}.ui",
)
_install_module(
    f"{PKG}.ui.ntmi_modimp",
)
_install_module(
    f"{PKG}.ui.ntmi_modimp.prefix_property_cache",
    get_prefix_record_props=lambda _name: dict(_prefix_cache_state["props"]),
    has_prefix_record=lambda _name: bool(_prefix_cache_state["props"]),
)
_install_module(
    f"{PKG}.common.global_config",
    GlobalConfig=types.SimpleNamespace(logic_name="HTMI"),
)
_install_module(
    f"{PKG}.common.logic_name",
    LogicName=types.SimpleNamespace(
        EFMI="EFMI",
        NTEMI="NTEMI",
        ZZMI="ZZMI",
        HIMI="HIMI",
    ),
)
# 材质转资源的导出路径会调用 toolkit 的 DDS 转换；桩成「转换成功」，
# 这里只关心 ini/资源段生成，贴图转换本身在 test_dds_conversion 里单独覆盖。
_install_module(
    f"{PKG}.toolkit.tt_dds_conversion",
    convert_texture_to_dds_cached=lambda *_args, **_kwargs: (True, ""),
)


ROOT = Path(__file__).resolve().parents[1]


def _load_blueprint_module(module_name):
    """按 {PKG}.blueprint.<name> 加载蓝图模块（相对 import 靠 sys.modules 命中）。"""
    spec = importlib.util.spec_from_file_location(
        f"{PKG}.blueprint.{module_name}", ROOT / "blueprint" / f"{module_name}.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


# FX 命名空间档案必须先于材质转资源节点加载（它从档案取命名空间）。
fx_namespace = _load_blueprint_module("fx_namespace")
node_postprocess_material = _load_blueprint_module("node_postprocess_material")


class _FakeImage:
    def __init__(self, filepath):
        self.filepath = filepath
        self.name = os.path.basename(filepath)


class _FakeTextureNode:
    type = "TEX_IMAGE"

    def __init__(self, filepath):
        self.image = _FakeImage(filepath)


class _FakeNodeTree:
    def __init__(self, texture_path):
        self.nodes = [_FakeTextureNode(texture_path)] if texture_path else []


class _FakeMaterial:
    def __init__(self, name="ImportedObject_Material", texture_path=""):
        self.name = name
        self.use_nodes = bool(texture_path)
        self.node_tree = _FakeNodeTree(texture_path) if texture_path else None


class _FakeMaterialSlot:
    def __init__(self, material):
        self.material = material


class _FakeObject(dict):
    def __init__(self, name, texture_slots, material_names=None, original_object_name=""):
        super().__init__()
        self.name = name
        self.original_object_name = original_object_name
        material_names = material_names or ["ImportedObject_Material"]
        self.material_slots = []
        for material_spec in material_names:
            if isinstance(material_spec, tuple):
                material_name, texture_path = material_spec
            else:
                material_name, texture_path = material_spec, ""
            self.material_slots.append(_FakeMaterialSlot(_FakeMaterial(material_name, texture_path)))
        self["modimp_texture_slots"] = json.dumps(texture_slots)


TTL_GROUP_HEAD = "; [SSMT-TTL]"
TTL_GROUP_TAIL = "; [/SSMT-TTL]"
TTL_TAKEN_PREFIX = "; [SSMT TTL 已接管]"


def _group_spans(lines):
    """返回 `; [SSMT-TTL]` … `; [/SSMT-TTL]` 之间的行号闭区间。"""
    spans = []
    start = None
    for index, line in enumerate(lines):
        stripped = str(line).strip()
        if stripped.startswith(TTL_GROUP_HEAD):
            start = index
        elif stripped == TTL_GROUP_TAIL and start is not None:
            spans.append((start, index))
            start = None
    return spans


def _group_for(lines, mesh_name):
    """取某个 mesh 的内联 TTL 组（含两端的组标记行）；必须恰好存在一个。"""
    matched = [
        lines[start:end + 1]
        for start, end in _group_spans(lines)
        if str(lines[start]).strip() == f"{TTL_GROUP_HEAD} {mesh_name}"
    ]
    if len(matched) != 1:
        raise AssertionError(
            f"期望恰有一个 TTL 组 {mesh_name!r}，实际 {len(matched)} 个：\n" + "\n".join(map(str, lines))
        )
    return matched[0]


def _group_count(lines):
    return len(_group_spans(lines))


def _outside_group(lines, mesh_name):
    """段内除该组之外的所有行。"""
    start, end = next(
        span for span in _group_spans(lines) if str(lines[span[0]]).strip() == f"{TTL_GROUP_HEAD} {mesh_name}"
    )
    return lines[:start] + lines[end + 1:]


def _dump_sections(sections, skip=("_config_path",)):
    return "\n".join(str(line) for key, lines in sections.items() if key not in skip for line in lines)


class HTMIMaterialPostProcessTests(unittest.TestCase):
    """测试 HTMI 材质后处理管线：纹理槽解析、资源重定向和 NTEMIFX 处理"""

    def setUp(self):
        """每个测试前清空数据和缓存"""
        _fake_bpy.data.objects.clear()
        node_postprocess_material.clear_name_mapping_cache()
        _prefix_cache_state["props"] = {}
        logic_name = "ZZMI" if self._testMethodName.startswith("test_ttl_") else "HTMI"
        sys.modules[f"{PKG}.common.global_config"].GlobalConfig.logic_name = logic_name

    def test_material_detect_accepts_ntmi_modimp_result_output(self):
        """测试材质检测接受 NTMI ModImp 结果输出节点类型"""
        class _FakeSocket:
            bl_idname = "SSMTSocketPostProcess"
            def __init__(self, linked_node=None):
                self.is_linked = linked_node is not None
                self.links = []
                if linked_node is not None:
                    self.links.append(types.SimpleNamespace(from_node=linked_node))

        class _FakeNode(dict):
            def __init__(self, name, bl_idname, inputs=None):
                super().__init__()
                self.name = name
                self.bl_idname = bl_idname
                self.inputs = inputs or []

        object_info = _FakeNode("ObjectInfo", "SSMTNode_Object_Info")
        object_info.object_name = "LOD0.fd054d1d-30030-0.Body"
        result_output = _FakeNode("ResultOutput", "SSMTNode_Result_Output_NTMIModImp", [_FakeSocket(object_info)])
        material_node = node_postprocess_material.SSMT_OT_MaterialDetect()
        self.assertIs(material_node._find_result_output(result_output), result_output)

    def test_zzmi_glow_slot_lines_use_explicit_mark_aliases(self):
        """ZZMI 发光槽位按显式标记写入 GlowMap/WengineFx 别名。"""
        sys.modules[f"{PKG}.common.global_config"].GlobalConfig.logic_name = "ZZMI"
        node = node_postprocess_material.SSMTNode_PostProcess_Material()
        lines = [
            "ps-t8 = Resource_GlowMap_body",
            "ps-t9 = ref Resource_WeaponGlow",
            "ps-t10 = Resource_WengineFx_stocking",
        ]
        normalized = node._normalize_zzmi_slot_lines(
            lines,
            slot_alias_map={
                "ps-t8": r"Resource\ZZMI\GlowMap",
                "ps-t9": r"Resource\ZZMI\GlowMap",
                "ps-t10": r"Resource\ZZMI\WengineFx",
            },
        )
        self.assertEqual(
            normalized,
            [
                r"Resource\ZZMI\GlowMap = ref Resource_GlowMap_body",
                r"Resource\ZZMI\GlowMap = ref Resource_WeaponGlow",
                r"Resource\ZZMI\WengineFx = ref Resource_WengineFx_stocking",
            ],
        )

    def test_htmi_texture_slots_drive_ps_resources_and_only_fxmap_material_drives_ntemifx(self):
        """测试 HTMI 纹理槽驱动 PS 资源，仅 FXMap 材质驱动 NTEMIFX"""
        with tempfile.TemporaryDirectory() as temp_dir:
            source_dir = os.path.join(temp_dir, "sources")
            material_dir = os.path.join(temp_dir, "material_nodes")
            os.makedirs(source_dir)
            os.makedirs(material_dir)
            source_paths = {}
            material_paths = {}
            source_filenames = {
                "DiffuseMap": "ae1ab184_6270_157770-t0-43d1e1eb.dds",
                "LightMap": "ae1ab184_6270_157770-t1-43d1e1eb.dds",
                "FXMap": "ae1ab184_6270_157770-t2-43d1e1eb.dds",
            }
            for mark_name, filename in source_filenames.items():
                path = os.path.join(source_dir, filename)
                with open(path, "wb") as file_obj:
                    file_obj.write(f"slot-{mark_name}".encode("ascii"))
                source_paths[mark_name] = path
                if mark_name == "DiffuseMap":
                    material_filename = "DiffuseMap_衣服00.png"
                else:
                    material_filename = f"material-{mark_name}.dds"
                material_path = os.path.join(material_dir, material_filename)
                with open(material_path, "wb") as file_obj:
                    file_obj.write(f"material-{mark_name}".encode("ascii"))
                material_paths[mark_name] = material_path
            texture_slots = {
                "ps-t0": {"source_path": source_paths["DiffuseMap"], "mark_name": "DiffuseMap", "mark_type": "Slot", "mark_filename": "fd054d1d-30030-0-DiffuseMap.dds"},
                "ps-t1": {"source_path": source_paths["LightMap"], "mark_name": "LightMap", "mark_type": "Slot", "mark_filename": "fd054d1d-30030-0-LightMap.dds"},
                "ps-t2": {"source_path": source_paths["FXMap"], "mark_name": "FXMap", "mark_type": "Slot", "mark_filename": "fd054d1d-30030-0-FXMap.dds"},
            }
            obj = _FakeObject("LOD0.fd054d1d-30030-0.Body", texture_slots, [
                ("DiffuseMap_琛ｆ湇00", material_paths["DiffuseMap"]),
                ("LightMap_Body", material_paths["LightMap"]),
                ("FXMap_Body", material_paths["FXMap"]),
            ])
            _fake_bpy.data.objects[obj.name] = obj
            sections = OrderedDict([
                ("[TextureOverride_Test]", [
                    f"[mesh:{obj.name}]", "hash = fd054d1d",
                    "ps-t0 = Resource-old-DiffuseMap", "ps-t1 = Resource-old-LightMap",
                    "ps-t2 = Resource-old-FXMap", "drawindexed = 3, 0, 0",
                ]),
                ("_config_path", temp_dir),
            ])
            node = node_postprocess_material.SSMTNode_PostProcess_Material()
            node.name = "MaterialNode"; node.material_to_resource_override = False; node.material_switch_var = "$swapkey150"
            node.process_texture_override_section("[TextureOverride_Test]", sections, material_group_to_swapkey={},
                swap_key_prefix="$swapkey", next_swap_key_num=150, used_swap_keys=set(), transparency_sections_to_add=OrderedDict())
            override_lines = sections["[TextureOverride_Test]"]
            mojibake_token = node_postprocess_material.SSMTNode_PostProcess_Material._latin_token_for_text("琛ｆ湇")
            self.assertIn(f"ps-t0 = Resource_DiffuseMap_{mojibake_token}00", override_lines)
            self.assertIn("ps-t1 = Resource_LightMap_Body", override_lines)
            self.assertIn("Resource\\RabbitFX\\FXMap = ref Resource_FXMap_Body", override_lines)

    def test_ntemi_fxmap_uses_ntemifx_namespace(self):
        """测试 NTEMI 逻辑名称下 FXMap 使用 NTEMIFX 命名空间"""
        sys.modules[f"{PKG}.common.global_config"].GlobalConfig.logic_name = "NTEMI"
        with tempfile.TemporaryDirectory() as temp_dir:
            source_dir = os.path.join(temp_dir, "sources"); os.makedirs(source_dir)
            fx_path = os.path.join(source_dir, "fx.dds")
            with open(fx_path, "wb") as file_obj: file_obj.write(b"fx")
            obj = _FakeObject("GenericMesh", [], [("FXMap_Generic", fx_path)])
            _fake_bpy.data.objects[obj.name] = obj
            sections = OrderedDict([("[TextureOverride_Generic]", [
                f"[mesh:{obj.name}]", "hash = 12345678", "drawindexed = 3, 0, 0"
            ]), ("_config_path", temp_dir)])
            node = node_postprocess_material.SSMTNode_PostProcess_Material()
            node.name = "MaterialNode"; node.material_to_resource_override = False; node.material_switch_var = "$swapkey150"
            node.process_texture_override_section("[TextureOverride_Generic]", sections, material_group_to_swapkey={},
                swap_key_prefix="$swapkey", next_swap_key_num=150, used_swap_keys=set(), transparency_sections_to_add=OrderedDict())
            self.assertIn("Resource\\NTEMIFX\\FXMap = ref Resource_FXMap_Generic", sections["[TextureOverride_Generic]"])
            self.assertIn("run = CommandList\\NTEMIFX\\Run", sections["[TextureOverride_Generic]"])

    def _run_material_node(self, temp_dir, section_name, section_lines, materials):
        obj = _FakeObject("HimiMesh", [], materials)
        _fake_bpy.data.objects[obj.name] = obj
        sections = OrderedDict([
            (section_name, [f"[mesh:{obj.name}]", "hash = 12345678", *section_lines]),
            ("_config_path", temp_dir),
        ])
        node = node_postprocess_material.SSMTNode_PostProcess_Material()
        node.name = "MaterialNode"
        node.material_to_resource_override = False
        node.material_switch_var = "$swapkey150"
        node.process_texture_override_section(
            section_name, sections, material_group_to_swapkey={},
            swap_key_prefix="$swapkey", next_swap_key_num=150,
            used_swap_keys=set(), transparency_sections_to_add=OrderedDict())
        return sections[section_name]

    def test_himi_glowmap_uses_hi3fx_namespace(self):
        """HIMI（崩坏 3）下发光贴图走 HI3FX：Resource\\HI3FX\\GlowMap + CommandList\\HI3FX\\Run。

        参考 SSMT 包里的 ``3Dmigoto\\HI3\\Mods\\HI3FX``：资源别名是
        ``Resource\\HI3FX\\GlowMap``，调用约定是 ``run = CommandList\\HI3FX\\Run``。
        """
        sys.modules[f"{PKG}.common.global_config"].GlobalConfig.logic_name = "HIMI"
        with tempfile.TemporaryDirectory() as temp_dir:
            glow_path = os.path.join(temp_dir, "glow.dds")
            with open(glow_path, "wb") as file_obj:
                file_obj.write(b"glow")
            lines = self._run_material_node(
                temp_dir, "[TextureOverride_Himi]", ["drawindexed = 3, 0, 0"],
                [("Glowmap_5_Body", glow_path)])

            self.assertIn("Resource\\HI3FX\\GlowMap = ref Resource_Glowmap_5_Body", lines)
            self.assertIn("run = CommandList\\HI3FX\\Run", lines)
            # 发光亮度写在 HI3FX 自己的变量上。
            self.assertIn("$\\HI3FX\\brightness = 5", lines)
            self.assertNotIn("Resource\\RabbitFX\\Glowmap", "".join(lines))

    def test_himi_fxmap_uses_hi3fx_namespace(self):
        """HIMI 下裁切贴图同样走 HI3FX 的 FXMap 别名。"""
        sys.modules[f"{PKG}.common.global_config"].GlobalConfig.logic_name = "HIMI"
        with tempfile.TemporaryDirectory() as temp_dir:
            fx_path = os.path.join(temp_dir, "fx.dds")
            with open(fx_path, "wb") as file_obj:
                file_obj.write(b"fx")
            lines = self._run_material_node(
                temp_dir, "[TextureOverride_Himi]", ["drawindexed = 3, 0, 0"],
                [("FXMap_Body", fx_path)])

            self.assertIn("Resource\\HI3FX\\FXMap = ref Resource_FXMap_Body", lines)
            self.assertIn("run = CommandList\\HI3FX\\Run", lines)

    def test_himi_ttlmap_uses_hi3fx_ttlmap_alias(self):
        """HIMI 的 ``TTLMap_`` 材质走 HI3FX 的第二个遮罩通道 ``Resource\\HI3FX\\TTLMap``。

        HI3FX v1.1 起遮罩拆成两条语义完全分开的通道（见
        ``Mods\\HI3FX\\HI3FX.ini`` 头部注释）：``FXMap``（ps-t61）在 shader 里被
        ``$fx_cutoff`` 截断成 0/1，只做裁切；``TTLMap``（ps-t62）才是覆盖率，
        中间值走抖动半透明。材质前缀就是这套语义（``toolkit/tt_alpha_extract.py``：
        允许半透明 → ``TTLMap_``，否则 → ``FXMap_``），所以材质转资源按前缀分别
        产出两条引用行，而 ``run`` 行仍是同一条命令列表（调用方式不变）。
        """
        sys.modules[f"{PKG}.common.global_config"].GlobalConfig.logic_name = "HIMI"
        with tempfile.TemporaryDirectory() as temp_dir:
            ttl_path = os.path.join(temp_dir, "ttl.png")
            with open(ttl_path, "wb") as file_obj:
                file_obj.write(b"ttl")
            lines = self._run_material_node(
                temp_dir, "[TextureOverride_Himi]", ["drawindexed = 3, 0, 0"],
                [("TTLMap_遮罩", ttl_path)])
            joined = "\n".join(lines)

            self.assertIn("Resource\\HI3FX\\TTLMap = ref Resource_TTLMap_", joined)
            self.assertIn("run = CommandList\\HI3FX\\Run", lines)
            self.assertIn("Resource\\HI3FX\\TTLMap = ref null", lines)
            self.assertIn("run = CommandList\\HI3FX\\Reset", lines)
            # 绘制后复位排在绘制之后。
            self.assertLess(joined.index("drawindexed"), joined.index("run = CommandList\\HI3FX\\Reset"))
            # 不许混进 ZZMI 的 TTL 绘制重建协议（CommandList\TTL\Draw 是 ZZMI 专属）。
            self.assertNotIn("Resource\\TTL\\TransparencyTex", joined)
            self.assertNotIn("CommandList\\TTL\\Draw", joined)
            self.assertNotIn("Resource\\RabbitFX\\", joined)

    def test_himi_ttlmap_creates_texture_resource_section(self):
        """TTLMap 引用行指向的 Resource 段必须真的生成（含 filename），否则是空引用。"""
        sys.modules[f"{PKG}.common.global_config"].GlobalConfig.logic_name = "HIMI"
        with tempfile.TemporaryDirectory() as temp_dir:
            ttl_path = os.path.join(temp_dir, "ttl.png")
            with open(ttl_path, "wb") as file_obj:
                file_obj.write(b"ttl")
            obj = _FakeObject("HimiMesh", [], [("TTLMap_遮罩", ttl_path)])
            _fake_bpy.data.objects[obj.name] = obj
            sections = OrderedDict([
                ("[TextureOverride_Himi]", [f"[mesh:{obj.name}]", "hash = 12345678", "drawindexed = 3, 0, 0"]),
                ("_config_path", temp_dir),
            ])
            node = node_postprocess_material.SSMTNode_PostProcess_Material()
            node.name = "MaterialNode"
            node.material_to_resource_override = False
            node.material_switch_var = "$swapkey150"
            node.process_texture_override_section(
                "[TextureOverride_Himi]", sections, material_group_to_swapkey={},
                swap_key_prefix="$swapkey", next_swap_key_num=150,
                used_swap_keys=set(), transparency_sections_to_add=OrderedDict())

            resource_sections = [name for name in sections if name.startswith("[Resource_TTLMap_")]
            self.assertEqual(len(resource_sections), 1, sections.keys())
            self.assertTrue(
                sections[resource_sections[0]][0].startswith("filename = Textures/"),
                sections[resource_sections[0]],
            )
            # 引用行的资源名与生成的段名必须对得上。
            ref_line = next(
                line for line in sections["[TextureOverride_Himi]"]
                if line.startswith("Resource\\HI3FX\\TTLMap = ref ")
            )
            self.assertEqual(ref_line.split("ref ", 1)[1].strip(), resource_sections[0].strip("[]"))

    def test_himi_fxmap_and_ttlmap_are_two_independent_channels(self):
        """同一物体同时挂 ``FXMap_`` 与 ``TTLMap_``：两条引用行各归各的通道。

        两者不是互斥的：``FXMap`` 给二值裁切、``TTLMap`` 给抖动覆盖率，运行时按
        乘法合成（裁切优先）。所以材质转资源必须两条都产出，而不是二选一。
        """
        sys.modules[f"{PKG}.common.global_config"].GlobalConfig.logic_name = "HIMI"
        with tempfile.TemporaryDirectory() as temp_dir:
            fx_path = os.path.join(temp_dir, "fx.png")
            ttl_path = os.path.join(temp_dir, "ttl.png")
            for path in (fx_path, ttl_path):
                with open(path, "wb") as file_obj:
                    file_obj.write(b"x")
            lines = self._run_material_node(
                temp_dir, "[TextureOverride_Himi]", ["drawindexed = 3, 0, 0"],
                [("FXMap_裁切", fx_path), ("TTLMap_遮罩", ttl_path)])
            joined = "\n".join(lines)

            self.assertIn("Resource\\HI3FX\\FXMap = ref Resource_FXMap_", joined)
            self.assertIn("Resource\\HI3FX\\TTLMap = ref Resource_TTLMap_", joined)
            self.assertIn("Resource\\HI3FX\\FXMap = ref null", lines)
            self.assertIn("Resource\\HI3FX\\TTLMap = ref null", lines)
            self.assertEqual(
                sum(line.strip().startswith("Resource\\HI3FX\\FXMap = ref Resource_") for line in lines),
                1,
            )
            self.assertEqual(
                sum(line.strip().startswith("Resource\\HI3FX\\TTLMap = ref Resource_") for line in lines),
                1,
            )

    def test_himi_ttlmap_rerun_does_not_accumulate_generated_lines(self):
        """重跑幂等：TTLMap 引用行会被清理掉再重写，不会一轮加一条。"""
        sys.modules[f"{PKG}.common.global_config"].GlobalConfig.logic_name = "HIMI"
        with tempfile.TemporaryDirectory() as temp_dir:
            ttl_path = os.path.join(temp_dir, "ttl.png")
            with open(ttl_path, "wb") as file_obj:
                file_obj.write(b"ttl")
            first = self._run_material_node(
                temp_dir, "[TextureOverride_Himi]", ["drawindexed = 3, 0, 0"],
                [("TTLMap_遮罩", ttl_path)])
            sections = OrderedDict([
                ("[TextureOverride_Himi]", list(first)),
                ("_config_path", temp_dir),
            ])
            node = node_postprocess_material.SSMTNode_PostProcess_Material()
            node.name = "MaterialNode"
            node.material_to_resource_override = False
            node.material_switch_var = "$swapkey150"
            node.process_texture_override_section(
                "[TextureOverride_Himi]", sections, material_group_to_swapkey={},
                swap_key_prefix="$swapkey", next_swap_key_num=150,
                used_swap_keys=set(), transparency_sections_to_add=OrderedDict())
            second = sections["[TextureOverride_Himi]"]
            self.assertEqual(first, second)
            self.assertEqual(
                sum(line.strip().startswith("Resource\\HI3FX\\TTLMap = ref Resource_")
                    for line in second),
                1,
            )

    def test_other_logics_never_emit_the_ttlmap_channel(self):
        """TTLMap 通道只有 HI3FX 有：其它逻辑的产出必须与改动前完全一致。

        用最敏感的探针——"只挂 TTLMap_ 材质的物体"：它在 RabbitFX / NTEMIFX 档案下
        没有对应通道，改动前不参与物体匹配，现在也必须不参与（段落原样保留）。
        ZZMI 走的是它自己的 ``CommandList\\TTL\\Draw`` 绘制重建协议（既有行为），
        这里只断言它不会沾上 HI3FX 的命名空间。
        """
        for logic in ("GIMI", "EFMI", "NTEMI", "HTMI"):
            with self.subTest(logic=logic):
                _fake_bpy.data.objects.clear()
                node_postprocess_material.clear_name_mapping_cache()
                sys.modules[f"{PKG}.common.global_config"].GlobalConfig.logic_name = logic
                with tempfile.TemporaryDirectory() as temp_dir:
                    ttl_path = os.path.join(temp_dir, "ttl.png")
                    with open(ttl_path, "wb") as file_obj:
                        file_obj.write(b"ttl")
                    lines = self._run_material_node(
                        temp_dir, "[TextureOverride_Other]", ["drawindexed = 3, 0, 0"],
                        [("TTLMap_遮罩", ttl_path)])
                    joined = "\n".join(lines)

                    self.assertNotIn("TTLMap", joined, logic)
                    self.assertNotIn("Resource\\HI3FX\\", joined, logic)
                    self.assertEqual(
                        lines,
                        ["[mesh:HimiMesh]", "hash = 12345678", "drawindexed = 3, 0, 0"],
                        logic,
                    )

    def test_zzmi_ttlmap_keeps_its_own_draw_rebuild_protocol(self):
        """ZZMI 的 TTLMap 仍旧走它自己的 ``CommandList\\TTL\\Draw`` 绘制重建协议。

        这条与 HI3FX 的 TTLMap 是两回事：前者是 ZZMI 专属的"重画一遍"协议（既有
        行为，本次未改），后者只是 HI3FX 的第二个遮罩通道。这里断言 ZZMI 下不会
        沾上 HI3FX 的命名空间——段落在 ZZMI 协议下会被重建，所以整表扫描而不是只看
        单个段。
        """
        _fake_bpy.data.objects.clear()
        node_postprocess_material.clear_name_mapping_cache()
        sys.modules[f"{PKG}.common.global_config"].GlobalConfig.logic_name = "ZZMI"
        with tempfile.TemporaryDirectory() as temp_dir:
            ttl_path = os.path.join(temp_dir, "ttl.png")
            with open(ttl_path, "wb") as file_obj:
                file_obj.write(b"ttl")
            obj = _FakeObject("HimiMesh", [], [("TTLMap_遮罩", ttl_path)])
            _fake_bpy.data.objects[obj.name] = obj
            sections = OrderedDict([
                ("[TextureOverride_Other]",
                 [f"[mesh:{obj.name}]", "hash = 12345678", "drawindexed = 3, 0, 0"]),
                ("_config_path", temp_dir),
            ])
            node = node_postprocess_material.SSMTNode_PostProcess_Material()
            node.name = "MaterialNode"
            node.material_to_resource_override = False
            node.material_switch_var = "$swapkey150"
            node.process_texture_override_section(
                "[TextureOverride_Other]", sections, material_group_to_swapkey={},
                swap_key_prefix="$swapkey", next_swap_key_num=150,
                used_swap_keys=set(), transparency_sections_to_add=OrderedDict())

            joined = "\n".join(
                "\n".join(value) for value in sections.values() if isinstance(value, list)
            )
            self.assertIn("Resource\\TTL\\TransparencyTex", joined, "ZZMI 仍走自己的 TTL 协议")
            self.assertNotIn("Resource\\HI3FX\\", joined)
            self.assertNotIn("CommandList\\HI3FX\\Run", joined)

    def test_rabbitfx_logics_keep_the_glowmap_output(self):
        """RabbitFX 逻辑的发光产出逐行不变（改动只加了一条 profile 门控的 TTLMap）。"""
        sys.modules[f"{PKG}.common.global_config"].GlobalConfig.logic_name = "GIMI"
        with tempfile.TemporaryDirectory() as temp_dir:
            glow_path = os.path.join(temp_dir, "glow.dds")
            with open(glow_path, "wb") as file_obj:
                file_obj.write(b"glow")
            lines = self._run_material_node(
                temp_dir, "[TextureOverride_Gimi]", ["drawindexed = 3, 0, 0"],
                [("Glowmap_5_Body", glow_path)])
            joined = "\n".join(lines)

            self.assertIn("Resource\\RabbitFX\\Glowmap = ref Resource_Glowmap_5_Body", joined)
            self.assertIn("run = CommandList\\RabbitFX\\Run", joined)
            self.assertIn("$\\RabbitFX\\brightness = 5", joined)
            self.assertNotIn("TTLMap", joined)

    def test_himi_reset_uses_hi3fx_reset_not_zeroed_brightness(self):
        """HIMI 的绘制后复位必须是 CommandList\\HI3FX\\Reset。

        HI3FX 的 ``Run`` 只做 Commit + Bind，不会把 ``$brightness`` 复位；
        照搬 RabbitFX 的 ``$\\RabbitFX\\brightness = 0`` 会让后续绘制一直吃
        brightness = 0（模型变黑），所以 HIMI 不能写任何 ``= 0`` 的复位参数。
        """
        sys.modules[f"{PKG}.common.global_config"].GlobalConfig.logic_name = "HIMI"
        with tempfile.TemporaryDirectory() as temp_dir:
            glow_path = os.path.join(temp_dir, "glow.dds")
            fx_path = os.path.join(temp_dir, "fx.dds")
            for path in (glow_path, fx_path):
                with open(path, "wb") as file_obj:
                    file_obj.write(os.path.basename(path).encode("ascii"))
            lines = self._run_material_node(
                temp_dir, "[TextureOverride_Himi]", ["drawindexed = 3, 0, 0"],
                [("Glowmap_5_Body", glow_path), ("FXMap_Body", fx_path)])
            joined = "\n".join(lines)

            self.assertIn("Resource\\HI3FX\\GlowMap = ref null", lines)
            self.assertIn("Resource\\HI3FX\\FXMap = ref null", lines)
            self.assertIn("run = CommandList\\HI3FX\\Reset", lines)
            self.assertNotIn("$\\HI3FX\\brightness = 0", joined)
            # 复位排在绘制之后。
            self.assertLess(joined.index("drawindexed"), joined.index("run = CommandList\\HI3FX\\Reset"))

    def test_himi_does_not_emit_settextures(self):
        """HI3FX 的 SetTextures 在默认不加载的 HI3FX.Remap.ini 里，不能自动写。

        配置表里已经出现 ``Resource\\HI3FX\\`` 绑定时，材质转资源要把别名重新指向
        新复制出来的资源，但不该顺手写 ``CommandList\\HI3FX\\SetTextures``。
        """
        sys.modules[f"{PKG}.common.global_config"].GlobalConfig.logic_name = "HIMI"
        with tempfile.TemporaryDirectory() as temp_dir:
            glow_path = os.path.join(temp_dir, "glow.dds")
            with open(glow_path, "wb") as file_obj:
                file_obj.write(b"glow")
            lines = self._run_material_node(
                temp_dir, "[TextureOverride_Himi]",
                ["Resource\\HI3FX\\GlowMap = ref Resource-old-Glowmap", "drawindexed = 3, 0, 0"],
                [("Glowmap_5_Body", glow_path)])
            joined = "\n".join(lines)

            self.assertIn("Resource\\HI3FX\\GlowMap = ref Resource_Glowmap_5_Body", lines)
            self.assertNotIn("CommandList\\HI3FX\\SetTextures", joined)

    def test_himi_rerun_does_not_accumulate_generated_lines(self):
        """重跑幂等：上一轮写下的 HI3FX 行会被清理掉再重写。"""
        sys.modules[f"{PKG}.common.global_config"].GlobalConfig.logic_name = "HIMI"
        with tempfile.TemporaryDirectory() as temp_dir:
            glow_path = os.path.join(temp_dir, "glow.dds")
            with open(glow_path, "wb") as file_obj:
                file_obj.write(b"glow")
            first = self._run_material_node(
                temp_dir, "[TextureOverride_Himi]", ["drawindexed = 3, 0, 0"],
                [("Glowmap_5_Body", glow_path)])
            obj = _fake_bpy.data.objects["HimiMesh"]
            sections = OrderedDict([
                ("[TextureOverride_Himi]", list(first)),
                ("_config_path", temp_dir),
            ])
            node = node_postprocess_material.SSMTNode_PostProcess_Material()
            node.name = "MaterialNode"
            node.material_to_resource_override = False
            node.material_switch_var = "$swapkey150"
            node.process_texture_override_section(
                "[TextureOverride_Himi]", sections, material_group_to_swapkey={},
                swap_key_prefix="$swapkey", next_swap_key_num=150,
                used_swap_keys=set(), transparency_sections_to_add=OrderedDict())
            second = sections["[TextureOverride_Himi]"]
            self.assertEqual(first, second)
            self.assertEqual(
                sum(line.strip().startswith("Resource\\HI3FX\\GlowMap = ref Resource_")
                    for line in second),
                1,
            )

    def test_should_emit_fx_reset_rule(self):
        """规则（2026-09 用户口径）：**每一次绘制都必须自带 FX 复位**。

        作者会自行调整绘制顺序，因此"下一个绘制必然重设同名 FX"这个前提不成立；
        依赖它跳过复位会让上一个 mesh 的 FX 绑定泄漏到不该有的部件上。
        故规则恒为 True（历史上曾做过跳过优化，已按上述理由撤销）。
        """
        rule = node_postprocess_material.SSMTNode_PostProcess_Material._should_emit_fx_reset

        self.assertTrue(rule(set(), set()), "即使本 mesh 没设 FX，也保持恒真（调用点已按 fx_kinds 决定是否复位）")
        self.assertTrue(rule({"FXMap"}, {"FXMap"}), "不得因下一个 mesh 重设同名 FX 而跳过")
        self.assertTrue(rule({"FXMap"}, {"FXMap", "Glowmap"}))
        self.assertTrue(rule({"Glowmap", "FXMap"}, {"FXMap", "Glowmap"}))
        self.assertTrue(rule({"FXMap"}, set()))
        self.assertTrue(rule({"Glowmap"}, {"FXMap"}))
        self.assertTrue(rule({"FXMap", "Glowmap"}, {"FXMap"}))

    def _emit_fx_chain(self, temp_dir, first_materials, second_materials, section_name="[TextureOverride_Chain]"):
        fx_path = os.path.join(temp_dir, "fx.dds")
        with open(fx_path, "wb") as file_obj:
            file_obj.write(b"fx")
        diffuse_path = os.path.join(temp_dir, "diffuse.png")
        with open(diffuse_path, "wb") as file_obj:
            file_obj.write(b"diffuse")

        def _material(entry):
            name, kind = entry
            return (name, fx_path if kind == "fx" else diffuse_path)

        first = _FakeObject("MeshA", {}, [_material(m) for m in first_materials])
        second = _FakeObject("MeshB", {}, [_material(m) for m in second_materials])
        _fake_bpy.data.objects[first.name] = first
        _fake_bpy.data.objects[second.name] = second
        sections = OrderedDict([(section_name, [
            f"[mesh:{first.name}]", "hash = 11111111",
            "ps-t0 = Resource-old-DiffuseMap", "drawindexed = 3, 0, 0",
            f"[mesh:{second.name}]", "hash = 22222222",
            "ps-t0 = Resource-old-DiffuseMap", "drawindexed = 3, 0, 0",
        ]), ("_config_path", temp_dir)])
        node = node_postprocess_material.SSMTNode_PostProcess_Material()
        node.name = "MaterialNode"
        node.material_to_resource_override = False
        node.material_switch_var = "$swapkey150"
        node.process_texture_override_section(
            section_name, sections, material_group_to_swapkey={},
            swap_key_prefix="$swapkey", next_swap_key_num=150,
            used_swap_keys=set(), transparency_sections_to_add=OrderedDict())
        return sections[section_name]

    def test_every_mesh_keeps_its_own_fx_reset(self):
        """回退后的口径：**每个绘制都保留自己的 FX 复位**，不因下一个 mesh 重设同名 FX 而跳过。

        作者会重排绘制顺序，跳过复位会让 FX 绑定泄漏。
        """
        sys.modules[f"{PKG}.common.global_config"].GlobalConfig.logic_name = "ZZMI"
        with tempfile.TemporaryDirectory() as temp_dir:
            lines = self._emit_fx_chain(
                temp_dir,
                [("FXMap_A", "fx")],
                [("FXMap_B", "fx")],
            )

            resets = [i for i, line in enumerate(lines) if line.strip() == "Resource\\RabbitFX\\FXMap = ref null"]
            self.assertEqual(len(resets), 2, f"两个 mesh 各保留一次复位: {lines}")
            draws = [i for i, line in enumerate(lines) if line.strip().startswith("drawindexed")]
            # 每个复位都紧跟在自己的绘制之后（在其下一个 mesh 之前）
            self.assertGreater(resets[0], draws[0], "第一个复位在第一次绘制之后")
            self.assertLess(resets[0], draws[1], "第一个复位在第二次绘制之前")
            self.assertGreater(resets[1], draws[1], "第二个复位在第二次绘制之后")

    def test_fx_reset_is_kept_when_next_mesh_has_no_fx(self):
        """下一个 mesh 没有 FX → 必须复位，否则它会继承上一个 mesh 的 FX 绑定。"""
        sys.modules[f"{PKG}.common.global_config"].GlobalConfig.logic_name = "ZZMI"
        with tempfile.TemporaryDirectory() as temp_dir:
            lines = self._emit_fx_chain(
                temp_dir,
                [("FXMap_A", "fx")],
                [("DiffuseMap_B", "diffuse")],
            )

            resets = [i for i, line in enumerate(lines) if line.strip() == "Resource\\RabbitFX\\FXMap = ref null"]
            self.assertEqual(len(resets), 1, f"应当保留复位: {lines}")
            first_draw = min(i for i, line in enumerate(lines) if line.strip().startswith("drawindexed"))
            last_draw = max(i for i, line in enumerate(lines) if line.strip().startswith("drawindexed"))
            self.assertGreater(resets[0], first_draw)
            self.assertLess(resets[0], last_draw, "复位必须在两个 mesh 之间，才能保护后一个绘制")

    def test_fx_reset_is_kept_when_next_mesh_rebinds_only_another_fx_kind(self):
        """下一个 mesh 只重设 Glowmap 时，FXMap 会泄漏 → 必须复位。"""
        sys.modules[f"{PKG}.common.global_config"].GlobalConfig.logic_name = "ZZMI"
        with tempfile.TemporaryDirectory() as temp_dir:
            lines = self._emit_fx_chain(
                temp_dir,
                [("FXMap_A", "fx")],
                [("Glowmap_5_B", "fx")],
            )

            resets = [i for i, line in enumerate(lines) if line.strip() == "Resource\\RabbitFX\\FXMap = ref null"]
            self.assertEqual(len(resets), 1, f"应当保留复位: {lines}")
            last_draw = max(i for i, line in enumerate(lines) if line.strip().startswith("drawindexed"))
            self.assertLess(resets[0], last_draw, "复位要在 Glowmap mesh 的绘制之前")

    def test_ntemi_fxmap_reset_is_emitted_after_conditional_block(self):
        """测试 NTEMI FXMap 在条件块后发出重置指令"""
        sys.modules[f"{PKG}.common.global_config"].GlobalConfig.logic_name = "NTEMI"
        with tempfile.TemporaryDirectory() as temp_dir:
            source_dir = os.path.join(temp_dir, "sources"); os.makedirs(source_dir)
            fx_path = os.path.join(source_dir, "fx.dds")
            with open(fx_path, "wb") as file_obj: file_obj.write(b"fx")
            obj = _FakeObject("LOD0.ae1ab184-71202-29187.袜子_copy", [], [("FXMap_DiffuseMap_袜子", fx_path)])
            _fake_bpy.data.objects[obj.name] = obj
            sections = OrderedDict([("[TextureOverride_Generic]", [
                f"[mesh:{obj.name}]", "hash = ae1ab184", "if $swapkey9 == 0 && $swapkey1 == 0",
                "drawindexed = 5676,3090522,0", "endif",
            ]), ("_config_path", temp_dir)])
            node = node_postprocess_material.SSMTNode_PostProcess_Material()
            node.name = "MaterialNode"; node.material_to_resource_override = False; node.material_switch_var = "$swapkey150"
            node.process_texture_override_section("[TextureOverride_Generic]", sections, material_group_to_swapkey={},
                swap_key_prefix="$swapkey", next_swap_key_num=150, used_swap_keys=set(), transparency_sections_to_add=OrderedDict())
            override_lines = sections["[TextureOverride_Generic]"]
            self.assertIn("Resource\\NTEMIFX\\FXMap = ref null", override_lines)

    def test_htmi_fx_slot_without_fxmap_prefix_material_does_not_write_ntemifx(self):
        """测试 HTMI 中无 FXMap 前缀材质的 FX 槽不写 NTEMIFX 引用"""
        with tempfile.TemporaryDirectory() as temp_dir:
            source_path = os.path.join(temp_dir, "ae1ab184_6270_157770-t5-43d1e1eb.dds")
            with open(source_path, "wb") as file_obj: file_obj.write(b"fx")
            material_path = os.path.join(temp_dir, "material-fx.dds")
            with open(material_path, "wb") as file_obj: file_obj.write(b"material-fx")
            texture_slots = {"t5": {"source_path": source_path, "mark_name": "FXMap", "mark_type": "Slot", "mark_filename": "fd054d1d-30030-0-FXMap.dds"}}
            obj = _FakeObject("LOD0.fd054d1d-30030-0.Body", texture_slots, [("FXMap", material_path)])
            _fake_bpy.data.objects[obj.name] = obj
            sections = OrderedDict([("[TextureOverride_Test]", [f"[mesh:{obj.name}]", "hash = fd054d1d", "ps-t5 = Resource-old-FXMap", "drawindexed = 3, 0, 0"]), ("_config_path", temp_dir)])
            node = node_postprocess_material.SSMTNode_PostProcess_Material()
            node.name = "MaterialNode"; node.material_to_resource_override = False; node.material_switch_var = "$swapkey150"
            node.process_texture_override_section("[TextureOverride_Test]", sections, material_group_to_swapkey={},
                swap_key_prefix="$swapkey", next_swap_key_num=150, used_swap_keys=set(), transparency_sections_to_add=OrderedDict())
            self.assertNotIn("ps-t5 = Resource-old-FXMap", sections["[TextureOverride_Test]"])

    def test_htmi_texture_slot_can_be_inferred_from_workspace_filename(self):
        """测试 HTMI 纹理槽可以从工作空间文件名推断"""
        with tempfile.TemporaryDirectory() as temp_dir:
            source_path = os.path.join(temp_dir, "ae1ab184_6270_157770-t5-43d1e1eb.dds")
            with open(source_path, "wb") as file_obj: file_obj.write(b"slot-diffuse")
            material_path = os.path.join(temp_dir, "material-diffuse.dds")
            with open(material_path, "wb") as file_obj: file_obj.write(b"material-diffuse")
            texture_slots = {"": {"mark_name": "DiffuseMap", "mark_type": "Slot", "mark_filename": os.path.basename(source_path)}}
            obj = _FakeObject("LOD0.fd054d1d-30030-0.Body", texture_slots, [("DiffuseMap_Body", material_path)])
            _fake_bpy.data.objects[obj.name] = obj
            sections = OrderedDict([("[TextureOverride_Test]", [f"[mesh:{obj.name}]", "hash = fd054d1d", "ps-t5 = Resource-old-DiffuseMap", "drawindexed = 3, 0, 0"]), ("_config_path", temp_dir)])
            node = node_postprocess_material.SSMTNode_PostProcess_Material()
            node.name = "MaterialNode"; node.material_to_resource_override = False; node.material_switch_var = "$swapkey150"
            node.process_texture_override_section("[TextureOverride_Test]", sections, ...)
            self.assertIn("ps-t5 = Resource_DiffuseMap_Body", sections["[TextureOverride_Test]"])

    def test_htmi_uses_each_object_own_material_texture_for_many_objects(self):
        """测试 HTMI 对多个对象分别使用各自的材质纹理"""
        with tempfile.TemporaryDirectory() as temp_dir:
            sections = OrderedDict(); section_lines = []
            texture_slots = {"ps-t0": {"mark_name": "DiffuseMap", "mark_type": "Slot", "mark_slot": "ps-t0"}}
            for index in range(30):
                obj_name = f"LOD0.ae1ab184-{29187 + index}-0.Part{index:02d}"
                material_name = f"DiffuseMap_Part{index:02d}"
                texture_path = os.path.join(temp_dir, f"source-{index:02d}.png")
                with open(texture_path, "wb") as file_obj: file_obj.write(f"material-{index:02d}".encode("ascii"))
                obj = _FakeObject(obj_name, texture_slots, [(material_name, texture_path)])
                _fake_bpy.data.objects[obj.name] = obj
                section_lines.extend([f"[mesh:{obj.name}]", f"hash = {index:08x}", "ps-t0 = Resource-old-DiffuseMap", "drawindexed = 3, 0, 0"])
            sections["[TextureOverride_Test]"] = section_lines; sections["_config_path"] = temp_dir
            node = node_postprocess_material.SSMTNode_PostProcess_Material()
            node.name = "MaterialNode"; node.material_to_resource_override = False; node.material_switch_var = "$swapkey150"
            node.process_texture_override_section("[TextureOverride_Test]", sections, material_group_to_swapkey={},
                swap_key_prefix="$swapkey", next_swap_key_num=150, used_swap_keys=set(), transparency_sections_to_add=OrderedDict())
            for index in range(30):
                self.assertIn(f"ps-t0 = Resource_DiffuseMap_Part{index:02d}", sections["[TextureOverride_Test]"])

    def test_non_htmi_ps_material_to_resource_still_uses_generic_path(self):
        """测试非 HTMI 模式下 PS 材质到资源仍使用通用路径"""
        with tempfile.TemporaryDirectory() as temp_dir:
            texture_path = os.path.join(temp_dir, "generic-diffuse.png")
            with open(texture_path, "wb") as file_obj: file_obj.write(b"generic")
            obj = _FakeObject("GenericMesh", {}, [("DiffuseMap_Generic", texture_path)])
            _fake_bpy.data.objects[obj.name] = obj
            sections = OrderedDict([("[TextureOverride_Generic]", [f"[mesh:{obj.name}]", "hash = 12345678", "ps-t0 = Resource-old-DiffuseMap", "drawindexed = 3, 0, 0"]), ("_config_path", temp_dir)])
            node = node_postprocess_material.SSMTNode_PostProcess_Material()
            node.name = "MaterialNode"; node.material_to_resource_override = False; node.material_switch_var = "$swapkey150"
            node.process_texture_override_section("[TextureOverride_Generic]", sections, ...)
            self.assertIn("ps-t0 = ResourceTexture_DiffuseMap_Generic", sections["[TextureOverride_Generic]"])

    def test_non_htmi_generic_path_still_handles_rabbitfx_and_zzmi_refs(self):
        """测试非 HTMI 通用路径仍能处理 RabbitFX 和 ZZMI 引用"""
        with tempfile.TemporaryDirectory() as temp_dir:
            diffuse_path = os.path.join(temp_dir, "diffuse.png"); glow_path = os.path.join(temp_dir, "glow.png")
            for path in (diffuse_path, glow_path):
                with open(path, "wb") as file_obj: file_obj.write(os.path.basename(path).encode("ascii"))
            obj = _FakeObject("GenericMesh", {}, [("DiffuseMap_Generic", diffuse_path), ("Glowmap_5_Generic", glow_path)])
            _fake_bpy.data.objects[obj.name] = obj
            sections = OrderedDict([("[TextureOverride_Generic]", [f"[mesh:{obj.name}]", "hash = 12345678",
                "ps-t0 = Resource-old-DiffuseMap", "Resource\\RabbitFX\\Glowmap = ref Resource-old-Glowmap",
                "Resource\\ZZMI\\DiffuseMap = ref Resource-old-DiffuseMap", "drawindexed = 3, 0, 0"]), ("_config_path", temp_dir)])
            node = node_postprocess_material.SSMTNode_PostProcess_Material()
            node.name = "MaterialNode"; node.material_to_resource_override = False; node.material_switch_var = "$swapkey150"
            node.process_texture_override_section("[TextureOverride_Generic]", sections, ...)
            self.assertIn("Resource\\RabbitFX\\Glowmap = ref Resource_Glowmap_5_Generic", sections["[TextureOverride_Generic]"])
            self.assertIn("Resource\\ZZMI\\DiffuseMap = ref Resource_DiffuseMap_Generic", sections["[TextureOverride_Generic]"])

    def test_htmi_uses_current_object_material_before_source_candidate(self):
        """测试 HTMI 优先使用当前对象的材质而非源候选材质"""
        with tempfile.TemporaryDirectory() as temp_dir:
            source_texture = os.path.join(temp_dir, "source-candidate.png"); current_texture = os.path.join(temp_dir, "current-object.png")
            with open(source_texture, "wb") as file_obj: file_obj.write(b"wrong-source")
            with open(current_texture, "wb") as file_obj: file_obj.write(b"correct-current")
            texture_slots = {"ps-t0": {"mark_name": "DiffuseMap", "mark_type": "Slot", "mark_slot": "ps-t0"}}
            source_obj = _FakeObject("SharedSource", {}, [("DiffuseMap_Source", source_texture)])
            obj = _FakeObject("LOD0.ae1ab184-29187-0.Current", texture_slots, [("DiffuseMap_Current", current_texture)], original_object_name="SharedSource")
            _fake_bpy.data.objects[source_obj.name] = source_obj; _fake_bpy.data.objects[obj.name] = obj
            sections = OrderedDict([("[TextureOverride_Test]", [f"[mesh:{obj.name}]", "hash = ae1ab184", "ps-t0 = Resource-old-DiffuseMap", "drawindexed = 3, 0, 0"]), ("_config_path", temp_dir)])
            node = node_postprocess_material.SSMTNode_PostProcess_Material()
            node.name = "MaterialNode"; node.material_to_resource_override = False; node.material_switch_var = "$swapkey150"
            node.process_texture_override_section("[TextureOverride_Test]", sections, ...)
            self.assertIn("ps-t0 = Resource_DiffuseMap_Current", sections["[TextureOverride_Test]"])

    def test_material_lookup_skips_renamed_candidates_without_matching_materials(self):
        node = node_postprocess_material.SSMTNode_PostProcess_Material()
        node.name = "MaterialNode"

        original_name = "LOD0.a6431856-1020-0.Body"
        original_copy_name = f"{original_name}_copy"
        renamed_copy_name = "LOD0.aa9ffb85-13377-0.Body_copy"

        original_obj = _FakeObject(original_name, {}, ["DiffuseMap_Body"])
        original_copy = _FakeObject(original_copy_name, {}, ["DiffuseMap_Body"])
        renamed_copy = _FakeObject(renamed_copy_name, {}, ["NormalMap_Body"])
        original_copy.material_slots.clear()

        for obj in (original_obj, original_copy, renamed_copy):
            _fake_bpy.data.objects[obj.name] = obj

        node.apply_name_mapping({original_copy_name: renamed_copy_name})

        ini_mapping = OrderedDict([("ps-t0", "DiffuseMap")])
        resolved = node.find_object_by_mesh_name(
            renamed_copy_name,
            object_filter=lambda candidate: node._object_has_matching_materials(candidate, ini_mapping),
        )

        self.assertIs(resolved, original_obj)

    def test_htmi_does_not_fallback_to_source_candidate_when_current_object_has_no_materials(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            source_texture = os.path.join(temp_dir, "source-candidate.png")
            with open(source_texture, "wb") as file_obj:
                file_obj.write(b"wrong-source")
            texture_slots = {"ps-t0": {"mark_name": "DiffuseMap", "mark_type": "Slot", "mark_slot": "ps-t0"}}
            source_obj = _FakeObject("SharedSource", {}, [("DiffuseMap_Source", source_texture)])
            obj = _FakeObject("LOD0.ae1ab184-29187-0.Current", texture_slots, [], original_object_name="SharedSource")
            _fake_bpy.data.objects[source_obj.name] = source_obj
            _fake_bpy.data.objects[obj.name] = obj
            sections = OrderedDict([("[TextureOverride_Test]", [f"[mesh:{obj.name}]", "hash = ae1ab184", "ps-t0 = Resource-old-DiffuseMap", "drawindexed = 3, 0, 0"]), ("_config_path", temp_dir)])
            node = node_postprocess_material.SSMTNode_PostProcess_Material()
            node.name = "MaterialNode"; node.material_to_resource_override = False; node.material_switch_var = "$swapkey150"
            node.process_texture_override_section("[TextureOverride_Test]", sections, ...)
            self.assertNotIn("ps-t0 = Resource_DiffuseMap_Source", sections["[TextureOverride_Test]"])
            self.assertFalse(any(line.startswith("ps-t0 = ") for line in sections["[TextureOverride_Test]"]))

    def test_htmi_workspace_slot_multiple_materials_reuse_existing_if_switch_rules(self):
        """测试 HTMI 工作空间槽的多材质复用现有 switch 规则"""
        with tempfile.TemporaryDirectory() as temp_dir:
            obj_name = "LOD0.fd054d1d-30030-0.Body"
            slot_texture_a = os.path.join(temp_dir, "diffuse-a.png"); slot_texture_b = os.path.join(temp_dir, "diffuse-b.png")
            for path, payload in ((slot_texture_a, b"a"), (slot_texture_b, b"b")):
                with open(path, "wb") as file_obj: file_obj.write(payload)
            obj = _FakeObject(obj_name, {"ps-t0": {"source_path": slot_texture_a, "mark_name": "DiffuseMap", "mark_type": "Slot", "mark_slot": "ps-t0", "mark_filename": "fd054d1d-30030-0-DiffuseMap.dds"}},
                [("DiffuseMap_Body_A", slot_texture_a), ("DiffuseMap_Body_B", slot_texture_b)])
            _fake_bpy.data.objects[obj.name] = obj
            sections = OrderedDict([("[TextureOverride_Test]", [f"[mesh:{obj.name}]", "hash = fd054d1d", "ps-t0 = Resource-old-DiffuseMap", "drawindexed = 3, 0, 0"]), ("_config_path", temp_dir)])
            node = node_postprocess_material.SSMTNode_PostProcess_Material()
            node.name = "MaterialNode"; node.material_to_resource_override = False; node.material_switch_var = "$swapkey150"
            node.process_texture_override_section("[TextureOverride_Test]", sections, ...)
            joined = "\n".join(sections["[TextureOverride_Test]"])
            self.assertIn("if $swapkey150 == 0", joined); self.assertIn("if $swapkey150 == 1", joined)

    def test_material_resource_names_replace_spaces_with_underscores(self):
        """测试材质资源名将空格替换为下划线"""
        material = _FakeMaterial("Highlight Metal 12261 01", os.path.join("/tmp", "tex.png"))
        node = node_postprocess_material.SSMTNode_PostProcess_Material()
        self.assertEqual(node._workspace_material_resource_name(material), "Resource-Highlight_Metal_12261_01")

    def test_material_resource_stem_replaces_chinese_with_deterministic_english(self):
        """测试中文材质名替换为确定性的随机英文串，保证引用一致"""
        material_cls = node_postprocess_material.SSMTNode_PostProcess_Material
        cloth_token = material_cls._latin_token_for_text("衣服")
        # 锁定映射算法：纯英文字母、首字母大写、长度固定
        self.assertRegex(cloth_token, r"^[A-Z][a-z]{9}$")
        stem = material_cls._material_resource_stem(_FakeMaterial("DiffuseMap_衣服02"))
        self.assertEqual(stem, f"DiffuseMap_{cloth_token}02")
        self.assertTrue(stem.isascii())
        # 同一中文永远得到同一串字母（跨调用、跨对象），不同中文得到不同串
        self.assertEqual(cloth_token, material_cls._latin_token_for_text("衣服"))
        self.assertNotEqual(cloth_token, material_cls._latin_token_for_text("裤子"))
        # 纯中文材质名也能生成合法纯英文 stem
        pure_chinese_stem = material_cls._material_resource_stem(_FakeMaterial("贴图"))
        self.assertEqual(pure_chinese_stem, material_cls._latin_token_for_text("贴图"))
        self.assertTrue(pure_chinese_stem.isascii())

    def test_chinese_material_generates_pure_english_resource_texture_section(self):
        """测试中文材质生成的 ResourceTexture 引用段落为纯英文且引用与资源段对应"""
        with tempfile.TemporaryDirectory() as temp_dir:
            texture_path = os.path.join(temp_dir, "generic-diffuse.png")
            with open(texture_path, "wb") as file_obj:
                file_obj.write(b"generic")
            obj = _FakeObject("GenericMesh", {}, [("DiffuseMap_贴图", texture_path)])
            _fake_bpy.data.objects[obj.name] = obj
            sections = OrderedDict([("[TextureOverride_Generic]", [f"[mesh:{obj.name}]", "hash = 12345678", "ps-t0 = Resource-old-DiffuseMap", "drawindexed = 3, 0, 0"]), ("_config_path", temp_dir)])
            node = node_postprocess_material.SSMTNode_PostProcess_Material()
            node.name = "MaterialNode"; node.material_to_resource_override = False; node.material_switch_var = "$swapkey150"
            node.process_texture_override_section("[TextureOverride_Generic]", sections, ...)

            material_cls = node_postprocess_material.SSMTNode_PostProcess_Material
            stem = material_cls._material_resource_stem(_FakeMaterial("DiffuseMap_贴图"))
            resource_name = f"ResourceTexture_{stem}"
            # 引用行为纯英文且指向资源段
            reference_line = f"ps-t0 = {resource_name}"
            self.assertIn(reference_line, sections["[TextureOverride_Generic]"])
            self.assertTrue(reference_line.isascii())
            # 资源段存在且 filename 行同为纯英文，贴图文件已按新名字复制
            section_name = f"[{resource_name}]"
            self.assertIn(section_name, sections)
            filename_line = f"filename = Textures/{stem}.png"
            self.assertIn(filename_line, sections[section_name])
            self.assertTrue(filename_line.isascii())
            self.assertTrue(os.path.exists(os.path.join(temp_dir, "Textures", f"{stem}.png")))

    def test_htmi_prefers_prefix_cache_texture_slots_over_stale_object_prop(self):
        """测试 HTMI 优先使用前缀缓存的纹理槽而非过时的对象属性"""
        with tempfile.TemporaryDirectory() as temp_dir:
            diffuse_path = os.path.join(temp_dir, "diffuse.png"); light_path = os.path.join(temp_dir, "light.png")
            for path, payload in ((diffuse_path, b"diffuse"), (light_path, b"light")):
                with open(path, "wb") as file_obj: file_obj.write(payload)
            obj_name = "LOD0.fd054d1d-30030-0.Body"
            obj = _FakeObject(obj_name, {"ps-t0": {"mark_name": "DiffuseMap", "mark_type": "Slot", "mark_slot": "ps-t0", "mark_filename": "stale-DiffuseMap.dds"}},
                [("DiffuseMap_Body", diffuse_path), ("LightMap_Body", light_path)])
            _prefix_cache_state["props"] = {
                "modimp_profile_id": "yihuan",
                "modimp_workspace_unique_str": "LOD0.fd054d1d-30030-0",
                "modimp_texture_slots": json.dumps({
                    "ps-t0": {"mark_name": "DiffuseMap", "mark_type": "Slot", "mark_slot": "ps-t0", "mark_filename": "fresh-DiffuseMap.dds"},
                    "ps-t1": {"mark_name": "LightMap", "mark_type": "Slot", "mark_slot": "ps-t1", "mark_filename": "fresh-LightMap.dds"},
                }),
            }
            _fake_bpy.data.objects[obj.name] = obj
            sections = OrderedDict([("[TextureOverride_Test]", [f"[mesh:{obj.name}]", "hash = fd054d1d", "ps-t0 = Resource-old-DiffuseMap", "ps-t1 = Resource-old-LightMap", "drawindexed = 3, 0, 0"]), ("_config_path", temp_dir)])
            node = node_postprocess_material.SSMTNode_PostProcess_Material()
            node.name = "MaterialNode"; node.material_to_resource_override = False; node.material_switch_var = "$swapkey150"
            node.process_texture_override_section("[TextureOverride_Test]", sections, ...)
            self.assertIn("ps-t0 = Resource_DiffuseMap_Body", sections["[TextureOverride_Test]"])
            self.assertIn("ps-t1 = Resource_LightMap_Body", sections["[TextureOverride_Test]"])

    def test_htmi_does_not_fallback_to_object_texture_slots_when_prefix_cache_exists_but_is_empty(self):
        """测试 HTMI 在前缀缓存存在但为空时不回退到对象纹理槽"""
        with tempfile.TemporaryDirectory() as temp_dir:
            stale_diffuse_path = os.path.join(temp_dir, "stale-diffuse.png")
            with open(stale_diffuse_path, "wb") as file_obj: file_obj.write(b"stale")
            obj_name = "LOD0.fd054d1d-30030-0.Body"
            obj = _FakeObject(obj_name, {"ps-t0": {"mark_name": "DiffuseMap", "mark_type": "Slot", "mark_slot": "ps-t0", "mark_filename": "stale-DiffuseMap.dds", "source_path": stale_diffuse_path}},
                [("DiffuseMap_Body", stale_diffuse_path)])
            _prefix_cache_state["props"] = {"modimp_profile_id": "yihuan", "modimp_workspace_unique_str": "LOD0.fd054d1d-30030-0"}
            _fake_bpy.data.objects[obj.name] = obj
            sections = OrderedDict([("[TextureOverride_Test]", [f"[mesh:{obj.name}]", "hash = fd054d1d", "ps-t0 = Resource-old-DiffuseMap", "drawindexed = 3, 0, 0"]), ("_config_path", temp_dir)])
            node = node_postprocess_material.SSMTNode_PostProcess_Material()
            node.name = "MaterialNode"; node.material_to_resource_override = False; node.material_switch_var = "$swapkey150"
            node.process_texture_override_section("[TextureOverride_Test]", sections, ...)
            self.assertNotIn("ps-t0 = Resource_DiffuseMap_Body", sections["[TextureOverride_Test]"])

    def test_ntmi_modimp_extra_ps_t2_diffuse_map_reuses_generated_diffuse_resource(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            diffuse_path = os.path.join(temp_dir, "diffuse.png")
            with open(diffuse_path, "wb") as file_obj:
                file_obj.write(b"diffuse")
            obj = _FakeObject(
                "LOD0.fd054d1d-30030-0.Body",
                {"ps-t0": {"mark_name": "DiffuseMap", "mark_type": "Slot", "mark_slot": "ps-t0"}},
                [("DiffuseMap_Body", diffuse_path)],
            )
            _fake_bpy.data.objects[obj.name] = obj
            sections = OrderedDict([
                ("[TextureOverride_Test]", [
                    f"[mesh:{obj.name}]",
                    "hash = fd054d1d",
                    "ps-t0 = Resource-old-DiffuseMap",
                    "drawindexed = 3, 0, 0",
                ]),
                ("_config_path", temp_dir),
            ])
            node = node_postprocess_material.SSMTNode_PostProcess_Material()
            node.name = "MaterialNode"
            node.material_to_resource_override = False
            node.material_switch_var = "$swapkey150"
            node._ntmi_modimp_extra_ps_t2_diffuse_map = True
            node.process_texture_override_section("[TextureOverride_Test]", sections, ...)
            override_lines = sections["[TextureOverride_Test]"]
            self.assertIn("ps-t0 = Resource_DiffuseMap_Body", override_lines)
            self.assertIn("ps-t2 = Resource_DiffuseMap_Body", override_lines)

    def test_ntmi_modimp_extra_ps_t2_diffuse_map_handles_missing_workspace_object(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            sections = OrderedDict([
                ("[TextureOverride_Test]", [
                    "[mesh:MissingObject]",
                    "hash = fd054d1d",
                    "ps-t0 = Resource-old-DiffuseMap",
                    "drawindexed = 3, 0, 0",
                ]),
                ("_config_path", temp_dir),
            ])
            node = node_postprocess_material.SSMTNode_PostProcess_Material()
            node.name = "MaterialNode"
            node.material_to_resource_override = False
            node.material_switch_var = "$swapkey150"
            node._ntmi_modimp_extra_ps_t2_diffuse_map = True

            node.process_texture_override_section("[TextureOverride_Test]", sections, ...)

            override_lines = sections["[TextureOverride_Test]"]
            self.assertIn("ps-t0 = Resource-old-DiffuseMap", override_lines)

    def test_ntmi_modimp_extra_ps_t2_diffuse_map_reuses_same_switch_variable_for_multiple_diffuse_materials(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            texture_a = os.path.join(temp_dir, "diffuse-a.png")
            texture_b = os.path.join(temp_dir, "diffuse-b.png")
            for path, payload in ((texture_a, b"a"), (texture_b, b"b")):
                with open(path, "wb") as file_obj:
                    file_obj.write(payload)
            obj = _FakeObject(
                "LOD0.fd054d1d-30030-0.Body",
                {"ps-t0": {"mark_name": "DiffuseMap", "mark_type": "Slot", "mark_slot": "ps-t0"}},
                [("DiffuseMap_Body_A", texture_a), ("DiffuseMap_Body_B", texture_b)],
            )
            _fake_bpy.data.objects[obj.name] = obj
            sections = OrderedDict([
                ("[TextureOverride_Test]", [
                    f"[mesh:{obj.name}]",
                    "hash = fd054d1d",
                    "ps-t0 = Resource-old-DiffuseMap",
                    "drawindexed = 3, 0, 0",
                ]),
                ("_config_path", temp_dir),
            ])
            node = node_postprocess_material.SSMTNode_PostProcess_Material()
            node.name = "MaterialNode"
            node.material_to_resource_override = False
            node.material_switch_var = "$swapkey150"
            node._ntmi_modimp_extra_ps_t2_diffuse_map = True
            node.process_texture_override_section("[TextureOverride_Test]", sections, ...)
            joined = "\n".join(sections["[TextureOverride_Test]"])
            self.assertEqual(joined.count("if $swapkey150 == 0"), 1)
            self.assertEqual(joined.count("if $swapkey150 == 1"), 1)
            self.assertIn("ps-t0 = Resource_DiffuseMap_Body_A", joined)
            self.assertIn("ps-t2 = Resource_DiffuseMap_Body_A", joined)
            self.assertIn("ps-t0 = Resource_DiffuseMap_Body_B", joined)
            self.assertIn("ps-t2 = Resource_DiffuseMap_Body_B", joined)

    def test_ntmi_modimp_extra_ps_t2_diffuse_map_uses_actual_diffuse_workspace_slot(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            diffuse_path = os.path.join(temp_dir, "diffuse.png")
            with open(diffuse_path, "wb") as file_obj:
                file_obj.write(b"diffuse")
            obj = _FakeObject(
                "LOD0.14076dfb-4893-310857.TimeRing",
                {"ps-t7": {"mark_name": "DiffuseMap", "mark_type": "Slot", "mark_slot": "ps-t7"}},
                [("DiffuseMap_衣服02", diffuse_path)],
            )
            _fake_bpy.data.objects[obj.name] = obj
            sections = OrderedDict([
                ("[TextureOverride_Test]", [
                    f"[mesh:{obj.name}]",
                    "hash = 14076dfb",
                    "ps-t5 = ResourceTexture_14076dfb_4893_310857_T5",
                    "ps-t7 = ResourceTexture_14076dfb_4893_310857_T7",
                    "ps-t8 = ResourceTexture_14076dfb_4893_310857_T8",
                    "drawindexed = 240,0,0",
                ]),
                ("_config_path", temp_dir),
            ])
            node = node_postprocess_material.SSMTNode_PostProcess_Material()
            node.name = "MaterialNode"
            node.material_to_resource_override = False
            node.material_switch_var = "$swapkey150"
            node._ntmi_modimp_extra_ps_t2_diffuse_map = True
            node.process_texture_override_section("[TextureOverride_Test]", sections, ...)
            override_lines = sections["[TextureOverride_Test]"]
            cloth_token = node_postprocess_material.SSMTNode_PostProcess_Material._latin_token_for_text("衣服")
            self.assertIn(f"ps-t7 = Resource_DiffuseMap_{cloth_token}02", override_lines)
            self.assertIn(f"ps-t2 = Resource_DiffuseMap_{cloth_token}02", override_lines)

    def test_ntmi_modimp_extra_ps_t2_diffuse_map_does_not_duplicate_unrelated_ps_slots(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            texture_paths = {}
            for key in ("normal", "diffuse", "light", "highlight"):
                path = os.path.join(temp_dir, f"{key}.png")
                with open(path, "wb") as file_obj:
                    file_obj.write(key.encode("ascii"))
                texture_paths[key] = path

            obj = _FakeObject(
                "LOD0.14076dfb-113934-18597.装饰_copy",
                {
                    "ps-t5": {"mark_name": "NormalMap", "mark_type": "Slot", "mark_slot": "ps-t5"},
                    "ps-t7": {"mark_name": "DiffuseMap", "mark_type": "Slot", "mark_slot": "ps-t7"},
                    "ps-t8": {"mark_name": "LightMap", "mark_type": "Slot", "mark_slot": "ps-t8"},
                    "ps-t18": {"mark_name": "HighLightMap", "mark_type": "Slot", "mark_slot": "ps-t18"},
                },
                [
                    ("NormalMap_DiffuseMap_衣服03", texture_paths["normal"]),
                    ("DiffuseMap_衣服03", texture_paths["diffuse"]),
                    ("LightMap_LOD0.14076dfb-111345-159309.切断器.001", texture_paths["light"]),
                    ("HighLightMap_LOD0.14076dfb-111345-159309.切断器.001", texture_paths["highlight"]),
                ],
            )
            _fake_bpy.data.objects[obj.name] = obj
            sections = OrderedDict([
                ("[TextureOverride_Test]", [
                    f"[mesh:{obj.name}]",
                    "hash = 14076dfb",
                    "ps-t5 = ResourceTexture_14076dfb_113934_18597_T5",
                    "ps-t7 = ResourceTexture_14076dfb_113934_18597_T7",
                    "ps-t8 = ResourceTexture_14076dfb_113934_18597_T8",
                    "ps-t18 = ResourceTexture_14076dfb_113934_18597_T18",
                    "drawindexed = 12993,163653,0",
                ]),
                ("_config_path", temp_dir),
            ])
            node = node_postprocess_material.SSMTNode_PostProcess_Material()
            node.name = "MaterialNode"
            node.material_to_resource_override = False
            node.material_switch_var = "$swapkey150"
            node._ntmi_modimp_extra_ps_t2_diffuse_map = True

            node.process_texture_override_section("[TextureOverride_Test]", sections, ...)

            joined = "\n".join(sections["[TextureOverride_Test]"])
            material_cls = node_postprocess_material.SSMTNode_PostProcess_Material
            cloth_token = material_cls._latin_token_for_text("衣服")
            cutter_token = material_cls._latin_token_for_text("切断器")
            self.assertEqual(joined.count(f"ps-t5 = Resource_NormalMap_DiffuseMap_{cloth_token}03"), 1)
            self.assertEqual(joined.count(f"ps-t7 = Resource_DiffuseMap_{cloth_token}03"), 1)
            self.assertEqual(joined.count(f"ps-t2 = Resource_DiffuseMap_{cloth_token}03"), 1)
            self.assertEqual(joined.count(f"ps-t8 = Resource_LightMap_LOD0.14076dfb-111345-159309.{cutter_token}.001"), 1)
            self.assertEqual(joined.count(f"ps-t18 = Resource_HighLightMap_LOD0.14076dfb-111345-159309.{cutter_token}.001"), 1)

    def test_ntmi_modimp_extra_ps_t2_diffuse_map_adds_base_alias_for_diffuse_workspace_slot(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            diffuse_path = os.path.join(temp_dir, "diffuse.png")
            with open(diffuse_path, "wb") as file_obj:
                file_obj.write(b"diffuse")
            obj = _FakeObject(
                "LOD0.14076dfb-4893-310857.TimeRing",
                {"ps-t7": {"mark_name": "DiffuseMap", "mark_type": "Slot", "mark_slot": "ps-t7"}},
                [("DiffuseMap_衣服02", diffuse_path)],
            )
            _fake_bpy.data.objects[obj.name] = obj
            sections = OrderedDict([
                ("[TextureOverride_Test]", [
                    f"[mesh:{obj.name}]",
                    "hash = 14076dfb",
                    "ps-t5 = ResourceTexture_14076dfb_4893_310857_T5",
                    "ps-t7 = ResourceTexture_14076dfb_4893_310857_T7",
                    "ps-t8 = ResourceTexture_14076dfb_4893_310857_T8",
                    "drawindexed = 240,0,0",
                ]),
                ("_config_path", temp_dir),
            ])
            node = node_postprocess_material.SSMTNode_PostProcess_Material()
            node.name = "MaterialNode"
            node.material_to_resource_override = False
            node.material_switch_var = "$swapkey150"
            node._ntmi_modimp_extra_ps_t2_diffuse_map = True
            node.process_texture_override_section("[TextureOverride_Test]", sections, ...)
            override_lines = sections["[TextureOverride_Test]"]
            self.assertIn("ps-t2 = ResourceTexture_14076dfb_4893_310857_T7", override_lines)

    def test_transparency_moves_complete_shader_replace_conditional_block(self):
        obj = _FakeObject("Body_透明0.5", {}, ["ImportedObject_Material"])
        _fake_bpy.data.objects[obj.name] = obj
        sections = OrderedDict([
            ("[TextureOverride_Test]", [
                f"[mesh:{obj.name}]",
                "if $Rain_ps_replace == 1",
                "    run = CustomShader_Rain_World",
                "else",
                "    run = CustomShader_Rain_Normal",
                "endif",
            ]),
            ("_config_path", ""),
        ])
        transparency_sections = OrderedDict()
        node = node_postprocess_material.SSMTNode_PostProcess_Material()
        node.name = "MaterialNode"
        node.material_to_resource_override = False
        node.material_switch_var = "$swapkey150"

        node.process_texture_override_section(
            "[TextureOverride_Test]",
            sections,
            material_group_to_swapkey={},
            swap_key_prefix="$swapkey",
            next_swap_key_num=150,
            used_swap_keys=set(),
            transparency_sections_to_add=transparency_sections,
        )

        override_lines = sections["[TextureOverride_Test]"]
        self.assertEqual(len(override_lines), 2)
        self.assertTrue(override_lines[1].startswith("run = CustomShaderTransparencyCloth"))
        moved_lines = next(iter(transparency_sections.values()))
        self.assertEqual(moved_lines[-1], "endif")
        self.assertEqual(
            sum(line.strip().startswith("if ") for line in moved_lines),
            sum(line.strip() == "endif" for line in moved_lines),
        )

    def test_transparency_stops_moving_content_after_draw_command(self):
        obj = _FakeObject("Body_透明0.5", {}, ["ImportedObject_Material"])
        _fake_bpy.data.objects[obj.name] = obj
        sections = OrderedDict([
            ("[TextureOverride_Test]", [
                f"[mesh:{obj.name}]",
                r"Resource\ZZMI\Diffuse = ref Resource_DiffuseMap_Color",
                r"run = CommandList\ZZMI\SetTextures",
                "  drawindexed = 1410,277422,0",
                "endif",
                "if $swapkey11 == 1",
                r"  run = CommandList\Unrelated",
                "endif",
            ]),
            ("_config_path", ""),
        ])
        transparency_sections = OrderedDict()
        node = node_postprocess_material.SSMTNode_PostProcess_Material()
        node.name = "MaterialNode"
        node.material_to_resource_override = False
        node.material_switch_var = "$swapkey150"

        node.process_texture_override_section(
            "[TextureOverride_Test]",
            sections,
            material_group_to_swapkey={},
            swap_key_prefix="$swapkey",
            next_swap_key_num=150,
            used_swap_keys=set(),
            transparency_sections_to_add=transparency_sections,
        )

        moved_lines = next(iter(transparency_sections.values()))
        self.assertEqual(moved_lines[-1].strip(), "drawindexed = 1410,277422,0")
        override_lines = sections["[TextureOverride_Test]"]
        self.assertIn("endif", override_lines)
        self.assertIn("if $swapkey11 == 1", override_lines)
        self.assertIn(r"  run = CommandList\Unrelated", override_lines)

    def test_transparency_keeps_conditional_draw_block_balanced(self):
        obj = _FakeObject("Body_透明0.5", {}, ["ImportedObject_Material"])
        _fake_bpy.data.objects[obj.name] = obj
        sections = OrderedDict([
            ("[TextureOverride_Test]", [
                f"[mesh:{obj.name}]",
                "if $swapkey11 == 1",
                r"  run = CommandList\ZZMI\SetTextures",
                "  drawindexed = 1410,277422,0",
                "endif",
                "if $swapkey12 == 1",
                r"  run = CommandList\Unrelated",
                "endif",
            ]),
            ("_config_path", ""),
        ])
        transparency_sections = OrderedDict()
        node = node_postprocess_material.SSMTNode_PostProcess_Material()
        node.name = "MaterialNode"
        node.material_to_resource_override = False
        node.material_switch_var = "$swapkey150"

        node.process_texture_override_section(
            "[TextureOverride_Test]",
            sections,
            material_group_to_swapkey={},
            swap_key_prefix="$swapkey",
            next_swap_key_num=150,
            used_swap_keys=set(),
            transparency_sections_to_add=transparency_sections,
        )

        moved_lines = next(iter(transparency_sections.values()))
        self.assertEqual(moved_lines[-1], "endif")
        self.assertEqual(
            sum(line.strip().startswith("if ") for line in moved_lines),
            sum(line.strip() == "endif" for line in moved_lines),
        )
        override_lines = sections["[TextureOverride_Test]"]
        self.assertIn("if $swapkey12 == 1", override_lines)
        self.assertIn(r"  run = CommandList\Unrelated", override_lines)

    def test_transparency_keeps_distinct_blocks_for_duplicate_mesh_names(self):
        obj = _FakeObject("Body_透明0.5", {}, ["ImportedObject_Material"])
        _fake_bpy.data.objects[obj.name] = obj
        sections = OrderedDict([
            ("[TextureOverride_Test]", [
                f"[mesh:{obj.name}]",
                "drawindexed = 3,0,0",
                f"[mesh:{obj.name}]",
                "drawindexed = 6,3,0",
            ]),
            ("_config_path", ""),
        ])
        transparency_sections = OrderedDict()
        node = node_postprocess_material.SSMTNode_PostProcess_Material()
        node.name = "MaterialNode"
        node.material_to_resource_override = False
        node.material_switch_var = "$swapkey150"

        node.process_texture_override_section(
            "[TextureOverride_Test]",
            sections,
            material_group_to_swapkey={},
            swap_key_prefix="$swapkey",
            next_swap_key_num=150,
            used_swap_keys=set(),
            transparency_sections_to_add=transparency_sections,
        )

        self.assertEqual(len(transparency_sections), 2)
        moved_lines = [line for block in transparency_sections.values() for line in block]
        self.assertIn("drawindexed = 3,0,0", moved_lines)
        self.assertIn("drawindexed = 6,3,0", moved_lines)
        run_targets = [
            line.split("=", 1)[1].strip()
            for line in sections["[TextureOverride_Test]"]
            if line.startswith("run = CustomShaderTransparencyCloth")
        ]
        self.assertEqual(len(run_targets), 2)
        self.assertEqual(len(set(run_targets)), 2)

    def test_transparency_reuses_one_section_for_identical_duplicate_blocks(self):
        """同一 mesh 在多个物体切换分支里内容一致地重复时，只生成一个透明段

        回归：KK 模组里一个透明 mesh 会在每个物体切换分支下出现一次
        （材质与 drawindexed 完全相同），旧实现只按段名占用去重，于是生成
        CustomShaderTransparencyCloth…_2 / _3 / … 一串内容逐字节相同的段。
        """
        obj = _FakeObject("KK 袜子 10641_透明0.75", {}, ["ImportedObject_Material"])
        _fake_bpy.data.objects[obj.name] = obj
        duplicate_block = [
            f"[mesh:{obj.name}]",
            r"Resource\ZZMI\Diffuse = ref Resource_DiffuseMap_Color",
            r"run = CommandList\ZZMI\SetTextures",
            "  drawindexed = 966,601512,0",
        ]
        sections = OrderedDict([
            ("[TextureOverride_Test]", [
                "if $swapkey31 == 0", *duplicate_block, "endif",
                "if $swapkey31 == 1", *duplicate_block, "endif",
                "if $swapkey31 == 2", *duplicate_block, "endif",
            ]),
            ("_config_path", ""),
        ])
        transparency_sections = OrderedDict()
        node = node_postprocess_material.SSMTNode_PostProcess_Material()
        node.name = "MaterialNode"
        node.material_to_resource_override = False
        node.material_switch_var = "$swapkey150"

        node.process_texture_override_section(
            "[TextureOverride_Test]",
            sections,
            material_group_to_swapkey={},
            swap_key_prefix="$swapkey",
            next_swap_key_num=150,
            used_swap_keys=set(),
            transparency_sections_to_add=transparency_sections,
        )

        self.assertEqual(len(transparency_sections), 1)
        section_name = next(iter(transparency_sections))
        run_targets = [
            line.split("=", 1)[1].strip()
            for line in sections["[TextureOverride_Test]"]
            if line.startswith("run = CustomShaderTransparencyCloth")
        ]
        self.assertEqual(len(run_targets), 3)
        self.assertEqual(set(run_targets), {section_name})
        moved_lines = transparency_sections[section_name]
        self.assertEqual(moved_lines.count("  drawindexed = 966,601512,0"), 1)

    def test_transparency_avoids_existing_custom_shader_section_name(self):
        obj = _FakeObject("Body_透明0.5", {}, ["ImportedObject_Material"])
        _fake_bpy.data.objects[obj.name] = obj
        node = node_postprocess_material.SSMTNode_PostProcess_Material()
        base_name, _value = node.extract_transparency_info_from_mesh_name(obj.name)
        sections = OrderedDict([
            ("[TextureOverride_Test]", [
                f"[mesh:{obj.name}]",
                "drawindexed = 3,0,0",
            ]),
            (f"[{base_name}]", ["handling = skip"]),
            ("_config_path", ""),
        ])
        transparency_sections = OrderedDict()
        node.name = "MaterialNode"
        node.material_to_resource_override = False
        node.material_switch_var = "$swapkey150"

        node.process_texture_override_section(
            "[TextureOverride_Test]",
            sections,
            material_group_to_swapkey={},
            swap_key_prefix="$swapkey",
            next_swap_key_num=150,
            used_swap_keys=set(),
            transparency_sections_to_add=transparency_sections,
        )

        self.assertEqual(list(transparency_sections), [f"{base_name}_2"])
        self.assertIn(f"run = {base_name}_2", sections["[TextureOverride_Test]"])

    def test_transparency_shader_name_is_converted_to_english_token(self):
        """透明生成的 CustomShader 段名/run 目标不再包含中文，使用与资源名一致的中文转英文 token"""
        node = node_postprocess_material.SSMTNode_PostProcess_Material()

        shader_name, transparency_value = node.extract_transparency_info_from_mesh_name("Body_透明0.5")

        self.assertEqual(transparency_value, "0.5")
        self.assertTrue(shader_name.startswith("CustomShaderTransparencyClothBody_"))
        self.assertTrue(shader_name.endswith("0.5"))
        self.assertNotIn("透明", shader_name)
        self.assertIn(node._latin_token_for_text("透明"), shader_name)

        complex_name, _ = node.extract_transparency_info_from_mesh_name("LOD0.abc-55002-0.身体.004_010_透明0.75")
        self.assertNotIn("身体", complex_name)
        self.assertNotIn("透明", complex_name)
        self.assertTrue(complex_name.endswith("0.75"))

    def test_ttl_transparent_with_zzmi_materials_inlined_in_same_section(self):
        """TTL：_透明0.75 + 四前缀材质不再另建段，改为原段保留、段尾内联一个 TTL 组"""
        with tempfile.TemporaryDirectory() as temp_dir:
            BS = chr(92)
            material_paths = {}
            for prefix, filename in [
                ("DiffuseMap", "diffuse.png"), ("NormalMap", "normal.png"),
                ("LightMap", "light.png"), ("MaterialMap", "material.png"),
            ]:
                path = os.path.join(temp_dir, filename)
                with open(path, "wb") as file_obj:
                    file_obj.write(filename.encode("ascii"))
                material_paths[prefix] = path

            mesh_name = "LOD0.241deac5-56376-0.中文中文_透明0.75_copy"
            ttl_path = os.path.join(temp_dir, "ttl.png")
            with open(ttl_path, "wb") as file_obj:
                file_obj.write(b"ttl")
            obj = _FakeObject(mesh_name, {}, [
                ("DiffuseMap_衣服", material_paths["DiffuseMap"]),
                ("NormalMap_腿", material_paths["NormalMap"]),
                ("LightMap_袖子", material_paths["LightMap"]),
                ("MaterialMap_身体", material_paths["MaterialMap"]),
                ("TTLMap_遮罩", ttl_path),
            ])
            _fake_bpy.data.objects[obj.name] = obj

            section_name = "[TextureOverride_LOD0.241deac5_56376_0]"
            anchor = f"; [mesh:{mesh_name}] [vertex_count:15618]"
            sections = OrderedDict([
                (section_name, [
                    "hash = 241deac5",
                    "match_first_index = 0",
                    "ib = Resource_LOD0.241deac5_56376_0_Index",
                    f"Resource{BS}ZZMI{BS}Diffuse = ref Resource-241deac5-56376-0-DiffuseMap",
                    f"Resource{BS}ZZMI{BS}NormalMap = ref Resource-241deac5-56376-0-NormalMap",
                    f"Resource{BS}ZZMI{BS}LightMap = ref Resource-241deac5-56376-0-LightMap",
                    f"Resource{BS}ZZMI{BS}MaterialMap = ref Resource-241deac5-56376-0-MaterialMap",
                    f"run = CommandList{BS}ZZMI{BS}SetTextures",
                    "run = CommandListSkinTexture",
                    anchor,
                    "drawindexed = 56376, 0, 3",
                ]),
                ("_config_path", temp_dir),
            ])

            node = node_postprocess_material.SSMTNode_PostProcess_Material()
            node.name = "MaterialNode"
            node.material_to_resource_override = False
            node.material_switch_var = "$swapkey150"
            node.process_texture_override_section(
                section_name, sections,
                material_group_to_swapkey={},
                swap_key_prefix="$swapkey",
                next_swap_key_num=150,
                used_swap_keys=set(),
                transparency_sections_to_add=OrderedDict(),
            )

            # 不再建段：原段原位保留，也没有第二个 TextureOverride / 命令列表
            self.assertIn(section_name, sections)
            self.assertEqual(
                [key for key in sections if key.startswith("[TextureOverride")],
                [section_name],
            )
            self.assertNotIn("[CommandListSSMTTTLDraw_", _dump_sections(sections))

            lines = sections[section_name]
            taken_line = "; [SSMT TTL 已接管] drawindexed = 56376, 0, 3"
            self.assertIn(taken_line, lines)
            group = _group_for(lines, mesh_name)
            self.assertEqual(group[0], f"; [SSMT-TTL] {mesh_name}")
            self.assertEqual(group[-1], "; [/SSMT-TTL]")
            # 组排段尾：整段最后一行就是组尾标记，且组头在接管注释之后
            self.assertEqual(lines[-1], "; [/SSMT-TTL]")
            self.assertGreater(lines.index(group[0]), lines.index(taken_line))

            # 组头：第一行是本网格注释，第二行是锚点，其后只留这笔绘制真正要重设的东西
            self.assertEqual(group[1], anchor)
            for cloned in (f"run = CommandList{BS}ZZMI{BS}SetTextures",):
                self.assertIn(cloned, group)
            # 匹配段的字段与 SkinTexture 不复制：在同一段里写第二遍是死代码
            for dead_line in (
                "hash = 241deac5",
                "match_first_index = 0",
                "ib = Resource_LOD0.241deac5_56376_0_Index",
                "run = CommandListSkinTexture",
            ):
                self.assertNotIn(dead_line, group)
            self.assertEqual(
                [line for line in group if line.strip().startswith(("vb0 =", "vb1 =", "vb2 ="))], []
            )

            diffuse_token = node._latin_token_for_text("衣服")
            normal_token = node._latin_token_for_text("腿")
            light_token = node._latin_token_for_text("袖子")
            material_token = node._latin_token_for_text("身体")
            self.assertIn(f"Resource{BS}ZZMI{BS}Diffuse = ref Resource_DiffuseMap_{diffuse_token}", group)
            self.assertIn(f"Resource{BS}ZZMI{BS}NormalMap = ref Resource_NormalMap_{normal_token}", group)
            self.assertIn(f"Resource{BS}ZZMI{BS}LightMap = ref Resource_LightMap_{light_token}", group)
            self.assertIn(f"Resource{BS}ZZMI{BS}MaterialMap = ref Resource_MaterialMap_{material_token}", group)
            self.assertIn(f"[Resource_DiffuseMap_{diffuse_token}]", sections)
            self.assertIn(
                f"filename = Textures/DiffuseMap_{diffuse_token}.png",
                sections[f"[Resource_DiffuseMap_{diffuse_token}]"],
            )

            ttl_ref_line = f"Resource{BS}TTL{BS}TransparencyTex = ref Resource_TTLMap_Rkydfsmefl"
            mask_channel_line = f"${BS}TTL{BS}mask_channel = 3"
            mask_line = f"${BS}TTL{BS}mask_invert = 1"
            alpha_line = f"${BS}TTL{BS}alpha = $TTLAlpha0_75"
            brightness_line = f"${BS}TTL{BS}V = 1.5"
            for ttl_line in (ttl_ref_line, mask_channel_line, mask_line, alpha_line, brightness_line):
                self.assertIn(ttl_line, group)
            self.assertLess(group.index(ttl_ref_line), group.index(mask_channel_line))
            self.assertLess(group.index(mask_channel_line), group.index(mask_line))
            self.assertLess(group.index(mask_line), group.index(alpha_line))
            self.assertLess(group.index(alpha_line), group.index(brightness_line))

            self.assertIn("global $TTLAlpha0_75 = 0.75", sections["[Constants]"])
            self.assertIn(f"${BS}TTL{BS}_1 = 56376", group)
            self.assertIn(f"${BS}TTL{BS}_2 = 0", group)
            self.assertIn(f"${BS}TTL{BS}_3 = 3", group)
            self.assertIn(f"run = CommandList{BS}TTL{BS}Draw", group)

    def test_ttl_if_switch_condition_carried_into_group(self):
        """TTL：原块在 if 里时，条件行随组搬进段尾，_1/_2/_3/run 缩进包在 if 内"""
        with tempfile.TemporaryDirectory() as temp_dir:
            BS = chr(92)
            diffuse_path = os.path.join(temp_dir, "diffuse.png")
            with open(diffuse_path, "wb") as file_obj:
                file_obj.write(b"diffuse")

            mesh_name = "LOD0.241deac5-56376-0.中文中文_透明0.75_copy"
            ttl_path = os.path.join(temp_dir, "ttl.png")
            with open(ttl_path, "wb") as file_obj:
                file_obj.write(b"ttl")
            obj = _FakeObject(mesh_name, {}, [("DiffuseMap_衣服", diffuse_path), ("TTLMap_遮罩", ttl_path)])
            _fake_bpy.data.objects[obj.name] = obj

            section_name = "[TextureOverride_LOD0.241deac5_56376_0]"
            sections = OrderedDict([
                (section_name, [
                    "hash = 241deac5",
                    "match_first_index = 0",
                    "ib = Resource_LOD0.241deac5_56376_0_Index",
                    f"Resource{BS}ZZMI{BS}Diffuse = ref Resource-241deac5-56376-0-DiffuseMap",
                    f"run = CommandList{BS}ZZMI{BS}SetTextures",
                    "run = CommandListSkinTexture",
                    "if $swapkey0 == 0 && $swapkey1 == 0",
                    f"  ; [mesh:{mesh_name}] [vertex_count:15618]",
                    "  drawindexed = 56376,0,3",
                    "endif",
                ]),
                ("_config_path", temp_dir),
            ])

            node = node_postprocess_material.SSMTNode_PostProcess_Material()
            node.name = "MaterialNode"
            node.material_to_resource_override = False
            node.material_switch_var = "$swapkey150"
            node.process_texture_override_section(
                section_name, sections,
                material_group_to_swapkey={},
                swap_key_prefix="$swapkey",
                next_swap_key_num=150,
                used_swap_keys=set(),
                transparency_sections_to_add=OrderedDict(),
            )

            self.assertIn(section_name, sections)
            lines = sections[section_name]
            group = _group_for(lines, mesh_name)
            self.assertEqual(lines[-1], "; [/SSMT-TTL]")
            self.assertIn("if $swapkey0 == 0 && $swapkey1 == 0", group)
            self.assertIn(f"    ${BS}TTL{BS}_1 = 56376", group)
            self.assertIn(f"    ${BS}TTL{BS}_2 = 0", group)
            self.assertIn(f"    ${BS}TTL{BS}_3 = 3", group)
            self.assertIn(f"    run = CommandList{BS}TTL{BS}Draw", group)
            self.assertEqual(group[-2], "endif")
            # V/alpha 在 if 之前，条件块只包绘制参数
            self.assertLess(
                group.index(f"${BS}TTL{BS}alpha = $TTLAlpha0_75"),
                group.index("if $swapkey0 == 0 && $swapkey1 == 0"),
            )
            self.assertIn("; [SSMT TTL 已接管] drawindexed = 56376,0,3", lines)

    def test_ttl_transparent_plus_fxmap_writes_ttl_ref(self):
        """TTL：_透明0.5 且有 TTLMap 材质时，组内写 TransparencyTex 引用并生成资源段"""
        with tempfile.TemporaryDirectory() as temp_dir:
            BS = chr(92)
            diffuse_path = os.path.join(temp_dir, "diffuse.png")
            ttlmap_path = os.path.join(temp_dir, "ttlmap.png")
            for path in (diffuse_path, ttlmap_path):
                with open(path, "wb") as file_obj:
                    file_obj.write(os.path.basename(path).encode("ascii"))

            mesh_name = "Body_透明0.5"
            obj = _FakeObject(mesh_name, {}, [
                ("DiffuseMap_衣服", diffuse_path),
                ("TTLMap_遮罩", ttlmap_path),
            ])
            _fake_bpy.data.objects[obj.name] = obj

            section_name = "[TextureOverride_Body]"
            sections = OrderedDict([
                (section_name, [
                    "hash = 241deac5",
                    "match_first_index = 0",
                    "ib = Resource_Body_Index",
                    f"Resource{BS}ZZMI{BS}Diffuse = ref Resource-old-DiffuseMap",
                    f"run = CommandList{BS}ZZMI{BS}SetTextures",
                    "run = CommandListSkinTexture",
                    f"[mesh:{mesh_name}]",
                    "drawindexed = 100, 5, 0",
                ]),
                ("_config_path", temp_dir),
            ])

            node = node_postprocess_material.SSMTNode_PostProcess_Material()
            node.name = "MaterialNode"
            node.material_to_resource_override = False
            node.material_switch_var = "$swapkey150"
            node.process_texture_override_section(
                section_name, sections,
                material_group_to_swapkey={},
                swap_key_prefix="$swapkey",
                next_swap_key_num=150,
                used_swap_keys=set(),
                transparency_sections_to_add=OrderedDict(),
            )

            self.assertIn(section_name, sections)
            lines = sections[section_name]
            group = _group_for(lines, mesh_name)
            fx_token = node._latin_token_for_text("遮罩")
            tex_ref_line = f"Resource{BS}TTL{BS}TransparencyTex = ref Resource_TTLMap_{fx_token}"
            alpha_line = f"${BS}TTL{BS}alpha = $TTLAlpha0_5"
            mask_invert_line = f"${BS}TTL{BS}mask_invert = 1"
            self.assertIn(tex_ref_line, group)
            self.assertIn(alpha_line, group)
            self.assertIn(mask_invert_line, group)
            self.assertLess(group.index(tex_ref_line), group.index(mask_invert_line))
            self.assertLess(group.index(mask_invert_line), group.index(alpha_line))
            self.assertIn("global $TTLAlpha0_5 = 0.5", sections["[Constants]"])
            self.assertIn(f"[Resource_TTLMap_{fx_token}]", sections)
            self.assertIn(
                f"filename = Textures/TTLMap_{fx_token}.png",
                sections[f"[Resource_TTLMap_{fx_token}]"],
            )
            joined = _dump_sections(sections)
            self.assertNotIn(r"ResourceRabbitFXFXMap", joined)
            self.assertNotIn(r"ResourceNTEMIFXFXMap", joined)

    def test_ttl_fxmap_mask_invert_appended_exactly_once_even_when_header_has_legacy_key(self):
        """TTL：组内 mask_invert 只写一次；旧头部残留的该键不复制进组，不会重复赋值"""
        with tempfile.TemporaryDirectory() as temp_dir:
            BS = chr(92)
            ttlmap_path = os.path.join(temp_dir, "ttlmap.png")
            with open(ttlmap_path, "wb") as file_obj:
                file_obj.write(b"fxmap")
            mesh_name = "Body_透明0.5"
            obj = _FakeObject(mesh_name, {}, [("TTLMap_遮罩", ttlmap_path)])
            _fake_bpy.data.objects[obj.name] = obj

            mask_key = f"${BS}TTL{BS}mask_invert = 1"
            legacy_key = f"${BS}TTL{BS}mask_invert = 0"
            section_name = "[TextureOverride_Body]"
            sections = OrderedDict([
                (section_name, [
                    "hash = 241deac5",
                    "match_first_index = 0",
                    "ib = Resource_Body_Index",
                    legacy_key,
                    "run = CommandListSkinTexture",
                    f"[mesh:{mesh_name}]",
                    "drawindexed = 100, 5, 0",
                ]),
                ("_config_path", temp_dir),
            ])

            node = node_postprocess_material.SSMTNode_PostProcess_Material()
            node.name = "MaterialNode"
            node.material_to_resource_override = False
            node.material_switch_var = "$swapkey150"
            node.process_texture_override_section(
                section_name, sections, material_group_to_swapkey={},
                swap_key_prefix="$swapkey", next_swap_key_num=150, used_swap_keys=set(),
                transparency_sections_to_add=OrderedDict(),
            )

            lines = sections[section_name]
            group = _group_for(lines, mesh_name)
            self.assertEqual(group.count(mask_key), 1)
            self.assertNotIn(legacy_key, group)
            # 旧头部那一行留在段头原地，不被复制（整段仍只有一个 = 0 和一个 = 1）
            outside = _outside_group(lines, mesh_name)
            self.assertEqual([line for line in outside if "mask_invert" in line], [legacy_key])
            self.assertEqual([line for line in lines if line.strip() == mask_key], [mask_key])

    def test_ttl_fxmap_only_writes_alpha_1(self):
        """TTL：仅有 TTLMap 材质（无透明后缀）时 alpha 固定 1.0，组内联在原段"""
        with tempfile.TemporaryDirectory() as temp_dir:
            BS = chr(92)
            ttlmap_path = os.path.join(temp_dir, "ttlmap.png")
            with open(ttlmap_path, "wb") as file_obj:
                file_obj.write(b"fxmap")
            mesh_name = "GenericMesh"
            obj = _FakeObject(mesh_name, {}, [("TTLMap_Generic", ttlmap_path)])
            _fake_bpy.data.objects[obj.name] = obj
            section_name = "[TextureOverride_Generic]"
            sections = OrderedDict([
                (section_name, [
                    "hash = 12345678",
                    "match_first_index = 0",
                    "ib = Resource_GenericMesh_Index",
                    "run = CommandListSkinTexture",
                    f"[mesh:{mesh_name}]",
                    "drawindexed = 3, 0, 0",
                ]),
                ("_config_path", temp_dir),
            ])
            node = node_postprocess_material.SSMTNode_PostProcess_Material()
            node.name = "MaterialNode"
            node.material_to_resource_override = False
            node.material_switch_var = "$swapkey150"
            node.process_texture_override_section(
                section_name, sections, material_group_to_swapkey={},
                swap_key_prefix="$swapkey", next_swap_key_num=150, used_swap_keys=set(),
                transparency_sections_to_add=OrderedDict(),
            )
            self.assertIn(section_name, sections)
            self.assertNotIn("[TextureOverrideGenericMesh]", sections)
            lines = sections[section_name]
            group = _group_for(lines, mesh_name)
            self.assertIn("; GenericMesh", group)
            self.assertIn(f"Resource{BS}TTL{BS}TransparencyTex = ref Resource_TTLMap_Generic", group)
            self.assertIn(f"${BS}TTL{BS}alpha = $TTLAlpha1_0", group)
            self.assertIn("global $TTLAlpha1_0 = 1.0", sections["[Constants]"])
            self.assertIn(f"${BS}TTL{BS}_1 = 3", group)
            self.assertIn(f"${BS}TTL{BS}_2 = 0", group)
            self.assertEqual(lines[-1], "; [/SSMT-TTL]")

    def test_ttl_default_brightness_written_outside_if_block(self):
        r"""TTL：提亮默认值 $\TTL\V = 1.5 写在组内 if 之外（回归：写进 if 内会爆炸）"""
        with tempfile.TemporaryDirectory() as temp_dir:
            BS = chr(92)
            mesh_name = "LOD0.241deac5-56376-0.中文中文_copy"
            ttl_path = os.path.join(temp_dir, "ttl.png")
            with open(ttl_path, "wb") as file_obj:
                file_obj.write(b"ttl")
            obj = _FakeObject(mesh_name, {}, [("TTLMap_遮罩", ttl_path)])
            _fake_bpy.data.objects[obj.name] = obj

            section_name = "[TextureOverride_LOD0.241deac5_56376_0]"
            sections = OrderedDict([
                (section_name, [
                    "hash = 241deac5",
                    "match_first_index = 0",
                    "ib = Resource_LOD0.241deac5_56376_0_Index",
                    "run = CommandListSkinTexture",
                    "if $swapkey0 == 0",
                    f"  ; [mesh:{mesh_name}] [vertex_count:15618]",
                    "  drawindexed = 56376,0,3",
                    "endif",
                ]),
                ("_config_path", temp_dir),
            ])

            node = node_postprocess_material.SSMTNode_PostProcess_Material()
            node.name = "MaterialNode"
            node.material_to_resource_switch = False
            node.material_to_resource_override = False
            node.material_switch_var = "$swapkey150"

            node.process_texture_override_section(
                section_name, sections,
                material_group_to_swapkey={},
                swap_key_prefix="$swapkey",
                next_swap_key_num=150,
                used_swap_keys=set(),
                transparency_sections_to_add=OrderedDict(),
            )

            lines = sections[section_name]
            group = _group_for(lines, mesh_name)
            brightness_line = f"${BS}TTL{BS}V = 1.5"
            if_line = "if $swapkey0 == 0"
            alpha_line = f"${BS}TTL{BS}alpha = $TTLAlpha1_0"

            # 1) 默认生成
            self.assertIn(brightness_line, group)
            # 2) 第 0 列：与 if 同级，不被条件块缩进
            self.assertEqual([line for line in group if line.strip() == brightness_line], [brightness_line])
            # 3) 位置在 alpha 之后、if 之前 —— 即条件块之外
            self.assertLess(group.index(alpha_line), group.index(brightness_line))
            self.assertLess(group.index(brightness_line), group.index(if_line))
            # 4) 条件块本身仍完整
            self.assertIn(f"    ${BS}TTL{BS}_1 = 56376", group)
            self.assertIn(f"    ${BS}TTL{BS}_2 = 0", group)
            self.assertIn(f"    ${BS}TTL{BS}_3 = 3", group)
            self.assertIn(f"    run = CommandList{BS}TTL{BS}Draw", group)
            self.assertEqual(group[-2], "endif")

    def test_ttl_multiple_fxmap_materials_use_switch_branches(self):
        """TTL：同一物体多个 TTLMap 材质在组内生成 $swapkey 分支"""
        with tempfile.TemporaryDirectory() as temp_dir:
            BS = chr(92)
            fx_a = os.path.join(temp_dir, "fxmap-a.png")
            fx_b = os.path.join(temp_dir, "fxmap-b.png")
            for path in (fx_a, fx_b):
                with open(path, "wb") as file_obj:
                    file_obj.write(os.path.basename(path).encode("ascii"))
            mesh_name = "Body"
            obj = _FakeObject(mesh_name, {}, [("TTLMap_A", fx_a), ("TTLMap_B", fx_b)])
            _fake_bpy.data.objects[obj.name] = obj
            section_name = "[TextureOverride_Body]"
            sections = OrderedDict([
                (section_name, [
                    "hash = 12345678",
                    "match_first_index = 0",
                    "ib = Resource_Body_Index",
                    f"[mesh:{mesh_name}]",
                    "drawindexed = 6, 3, 0",
                ]),
                ("_config_path", temp_dir),
            ])
            node = node_postprocess_material.SSMTNode_PostProcess_Material()
            node.name = "MaterialNode"
            node.material_to_resource_override = False
            node.material_switch_var = "$swapkey150"
            node.process_texture_override_section(
                section_name, sections, material_group_to_swapkey={},
                swap_key_prefix="$swapkey", next_swap_key_num=150, used_swap_keys=set(),
                transparency_sections_to_add=OrderedDict(),
            )
            self.assertIn(section_name, sections)
            lines = sections[section_name]
            group = _group_for(lines, mesh_name)
            self.assertIn("if $swapkey150 == 0", group)
            self.assertIn("if $swapkey150 == 1", group)
            self.assertIn(f"    Resource{BS}TTL{BS}TransparencyTex = ref Resource_TTLMap_A", group)
            self.assertIn(f"    Resource{BS}TTL{BS}TransparencyTex = ref Resource_TTLMap_B", group)
            self.assertEqual(lines[-1], "; [/SSMT-TTL]")

    def test_ttl_shared_if_only_transparent_gets_own_if(self):
        """TTL：共用 if 里只把 _透明 那块搬进段尾组，普通网格的绘制留在原处"""
        with tempfile.TemporaryDirectory() as temp_dir:
            BS = chr(92)
            diffuse_path = os.path.join(temp_dir, "diffuse.png")
            with open(diffuse_path, "wb") as file_obj:
                file_obj.write(b"diffuse")
            ttl_path = os.path.join(temp_dir, "ttl.png")
            with open(ttl_path, "wb") as file_obj:
                file_obj.write(b"ttl")
            transparent_mesh = "Body_透明0.5"
            transparent_obj = _FakeObject(transparent_mesh, {}, [
                ("DiffuseMap_衣服", diffuse_path), ("TTLMap_遮罩", ttl_path),
            ])
            normal_obj = _FakeObject("Body", {}, [("DiffuseMap_衣服", diffuse_path)])
            _fake_bpy.data.objects[transparent_obj.name] = transparent_obj
            _fake_bpy.data.objects[normal_obj.name] = normal_obj
            section_name = "[TextureOverride_Shared]"
            sections = OrderedDict([
                (section_name, [
                    "hash = 241deac5",
                    "match_first_index = 0",
                    "ib = Resource_Shared_Index",
                    f"Resource{BS}ZZMI{BS}Diffuse = ref Resource-old-DiffuseMap",
                    f"run = CommandList{BS}ZZMI{BS}SetTextures",
                    "run = CommandListSkinTexture",
                    "if $swapkey0 == 0",
                    "  [mesh:Body]",
                    "  drawindexed = 100, 0, 0",
                    f"  [mesh:{transparent_mesh}]",
                    "  drawindexed = 200, 100, 0",
                    "endif",
                ]),
                ("_config_path", temp_dir),
            ])
            node = node_postprocess_material.SSMTNode_PostProcess_Material()
            node.name = "MaterialNode"
            node.material_to_resource_override = False
            node.material_switch_var = "$swapkey150"
            node.process_texture_override_section(
                section_name, sections, material_group_to_swapkey={},
                swap_key_prefix="$swapkey", next_swap_key_num=150, used_swap_keys=set(),
                transparency_sections_to_add=OrderedDict(),
            )
            self.assertIn(section_name, sections)
            shared = sections[section_name]
            # 普通网格原样留着
            self.assertIn("  [mesh:Body]", shared)
            self.assertIn("  drawindexed = 100, 0, 0", shared)
            self.assertLess(shared.index("  [mesh:Body]"), shared.index("  drawindexed = 100, 0, 0"))
            # _透明 那块只在组里出现一次（段头克隆自带锚点；裸 [mesh:X] 锚点在组里
            # 写成 `; X`，注释形态的锚点才逐字保留）
            group = _group_for(shared, transparent_mesh)
            self.assertEqual(shared.count(f"  [mesh:{transparent_mesh}]"), 1)
            self.assertTrue(group)
            self.assertIn(f"; {transparent_mesh}", group)
            self.assertIn("; [SSMT TTL 已接管] drawindexed = 200, 100, 0", shared)
            self.assertIn("if $swapkey0 == 0", group)
            self.assertIn(f"    ${BS}TTL{BS}_1 = 200", group)
            self.assertIn(f"    ${BS}TTL{BS}_2 = 100", group)
            self.assertIn(f"    run = CommandList{BS}TTL{BS}Draw", group)
            self.assertIn("endif", group)
            self.assertEqual(shared[-1], "; [/SSMT-TTL]")

    def test_ttl_same_alpha_value_reuses_same_variable(self):
        """TTL：相同透明数值复用同一个 $TTLAlpha 变量，[Constants] 只定义一次"""
        with tempfile.TemporaryDirectory() as temp_dir:
            BS = chr(92)
            diffuse_path = os.path.join(temp_dir, "diffuse.png")
            with open(diffuse_path, "wb") as file_obj:
                file_obj.write(b"diffuse")
            ttl_path = os.path.join(temp_dir, "ttl.png")
            with open(ttl_path, "wb") as file_obj:
                file_obj.write(b"ttl")
            mesh_a = "BodyA_透明0.75"
            mesh_b = "BodyB_透明0.75"
            obj_a = _FakeObject(mesh_a, {}, [("DiffuseMap_衣服", diffuse_path), ("TTLMap_遮罩", ttl_path)])
            obj_b = _FakeObject(mesh_b, {}, [("DiffuseMap_衣服", diffuse_path), ("TTLMap_遮罩", ttl_path)])
            _fake_bpy.data.objects[obj_a.name] = obj_a
            _fake_bpy.data.objects[obj_b.name] = obj_b
            section_name = "[TextureOverride_Shared]"
            sections = OrderedDict([
                (section_name, [
                    "hash = 241deac5",
                    "match_first_index = 0",
                    "ib = Resource_Shared_Index",
                    f"[mesh:{mesh_a}]",
                    "drawindexed = 100, 0, 0",
                    f"[mesh:{mesh_b}]",
                    "drawindexed = 200, 100, 0",
                ]),
                ("_config_path", temp_dir),
            ])
            node = node_postprocess_material.SSMTNode_PostProcess_Material()
            node.name = "MaterialNode"
            node.material_to_resource_override = False
            node.material_switch_var = "$swapkey150"
            node.process_texture_override_section(
                section_name, sections, material_group_to_swapkey={},
                swap_key_prefix="$swapkey", next_swap_key_num=150, used_swap_keys=set(),
                transparency_sections_to_add=OrderedDict(),
            )
            constants = sections.get("[Constants]", [])
            self.assertEqual(constants.count("global $TTLAlpha0_75 = 0.75"), 1)
            lines = sections[section_name]
            # 同一段两个网格各自成组
            self.assertEqual(_group_count(lines), 2)
            alpha_line = f"${BS}TTL{BS}alpha = $TTLAlpha0_75"
            self.assertEqual([line for line in lines if line.strip() == alpha_line], [alpha_line, alpha_line])
            self.assertIn(f"${BS}TTL{BS}_1 = 100", _group_for(lines, mesh_a))
            self.assertIn(f"${BS}TTL{BS}_1 = 200", _group_for(lines, mesh_b))

    def test_ttl_drag_hook_lines_cloned_into_group(self):
        """TTL：拖拽钩子行随段头一起克隆进组，不再生成任何命令列表。

        钩子只在拖拽激活时绑 jiggle 临时 VB0，绝不生成 else 分支绑 Position 基础输入
        （否则骨骼丢失）；组内不写死 ib —— ib 由克隆来的段头行决定，索引缓冲与顶点
        缓冲因此始终同源。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            BS = chr(92)
            ttl_path = os.path.join(temp_dir, "ttl.png")
            with open(ttl_path, "wb") as file_obj:
                file_obj.write(b"ttl")
            mesh_name = "LOD0.241deac5-56376-0.中文中文_透明0.75_copy"
            obj = _FakeObject(mesh_name, {}, [("TTLMap_遮罩", ttl_path)])
            _fake_bpy.data.objects[obj.name] = obj

            section_name = "[TextureOverride_LOD0.241deac5_56376_0]"
            sections = OrderedDict([
                (section_name, [
                    "hash = 241deac5",
                    "match_first_index = 0",
                    "ib = Resource_LOD0.241deac5_56376_0_Index",
                    f"run = CommandList{BS}ZZMI{BS}SetTextures",
                    "run = CommandListSkinTexture",
                    "    ; --- DRAG HOOK BEGIN 241deac5P0_A ---",
                    "    if $ssmtdrag_drag_enabled_A >= 1 && $ObjectDetectAllowed_A == 1",
                    "        run = CustomShaderDragBake241deac5P0_A",
                    "        run = CustomShaderDragDetect241deac5P0_A",
                    "    endif",
                    "    if $ssmtdrag_drag_enabled_A >= 2 && $ssmtdrag_mode_A == 1",
                    "        if time != $ssmtdrag_last_dispatch_241deac5_A",
                    "            run = CustomShaderDragJiggle241deac5_A",
                    "            $ssmtdrag_last_dispatch_241deac5_A = time",
                    "        endif",
                    "        vb0 = ResourceDragJiggleTempVB0_241deac5_A",
                    "    endif",
                    "    ; --- DRAG HOOK END 241deac5P0_A ---",
                    f"; [mesh:{mesh_name}] [vertex_count:15618]",
                    "drawindexed = 56376, 0, 0",
                ]),
                ("[TextureOverride_VB_241deac5_241deac5_Position]", [
                    "hash = 13c77c3a",
                    "vb2 = Resource241deac5Blend",
                    "vb0 = Resource241deac5Position",
                ]),
                ("_config_path", temp_dir),
            ])

            node = node_postprocess_material.SSMTNode_PostProcess_Material()
            node.name = "MaterialNode"
            node.material_to_resource_override = False
            node.material_switch_var = "$swapkey150"
            node.process_texture_override_section(
                section_name, sections,
                material_group_to_swapkey={},
                swap_key_prefix="$swapkey",
                next_swap_key_num=150,
                used_swap_keys=set(),
                transparency_sections_to_add=OrderedDict(),
            )

            # 新契约：一条命令列表都不生成
            self.assertEqual([key for key in sections if key.startswith("[CommandListSSMTTTLDraw_")], [])
            self.assertNotIn("CommandListSSMTTTLDraw_", _dump_sections(sections))

            lines = sections[section_name]
            group = _group_for(lines, mesh_name)
            stripped_group = [line.strip() for line in group]
            self.assertIn("if $ssmtdrag_drag_enabled_A >= 2 && $ssmtdrag_mode_A == 1", stripped_group)
            self.assertIn("vb0 = ResourceDragJiggleTempVB0_241deac5_A", stripped_group)
            self.assertIn("run = CustomShaderDragJiggle241deac5_A", stripped_group)
            self.assertIn("; --- DRAG HOOK BEGIN 241deac5P0_A ---", stripped_group)
            self.assertIn("; --- DRAG HOOK END 241deac5P0_A ---", stripped_group)
            self.assertNotIn("else", stripped_group)
            self.assertNotIn("vb0 = Resource241deac5Position", stripped_group)
            # 组里只有钩子逐字克隆 + 本网格贴图绑定，没有匹配段的死代码字段
            self.assertIn("run = CommandList\\ZZMI\\SetTextures", stripped_group)
            for dead_line in (
                "hash = 241deac5",
                "match_first_index = 0",
                "ib = Resource_LOD0.241deac5_56376_0_Index",
                "run = CommandListSkinTexture",
            ):
                self.assertNotIn(dead_line, group)
            self.assertIn(f"run = CommandList{BS}TTL{BS}Draw", group)

    def test_ttl_stale_command_list_left_untouched(self):
        """TTL：旧文件中残留的 [CommandListSSMTTTLDraw_*] 不再被重建/改写，绘制入口回到组内"""
        with tempfile.TemporaryDirectory() as temp_dir:
            BS = chr(92)
            ttl_path = os.path.join(temp_dir, "ttl.png")
            with open(ttl_path, "wb") as file_obj:
                file_obj.write(b"ttl")
            mesh_name = "LOD0.241deac5-56376-0.中文中文_透明0.75_copy"
            obj = _FakeObject(mesh_name, {}, [("TTLMap_遮罩", ttl_path)])
            _fake_bpy.data.objects[obj.name] = obj

            section_name = "[TextureOverride_LOD0.241deac5_56376_0_copy]"
            stale_name = "[CommandListSSMTTTLDraw_241deac5_A]"
            stale_lines = [
                "if $ssmtdrag_drag_enabled_A >= 2 && $ssmtdrag_mode_A == 1",
                "    vb0 = copy ResourceDragJiggleShadowVB_241deac5_A",
                "endif",
                "ib = Resource_LOD0.241deac5_56376_0_Index",
                f"run = CommandList{BS}TTL{BS}Draw",
            ]
            sections = OrderedDict([
                (section_name, [
                    "hash = 241deac5",
                    "match_first_index = 0",
                    "ib = Resource_LOD0.241deac5_56376_0_Index",
                    f"run = CommandList{BS}ZZMI{BS}SetTextures",
                    "run = CommandListSkinTexture",
                    f"; [mesh:{mesh_name}] [vertex_count:15618]",
                    f"Resource{BS}TTL{BS}TransparencyTex = ref Resource_TTLMap_DiffuseMap_X",
                    f"${BS}TTL{BS}mask_channel = 3",
                    "drawindexed = 56376, 0, 0",
                ]),
                (stale_name, list(stale_lines)),
                ("_config_path", temp_dir),
            ])

            node = node_postprocess_material.SSMTNode_PostProcess_Material()
            node.name = "MaterialNode"
            node.material_to_resource_override = False
            node.material_switch_var = "$swapkey150"
            node.process_texture_override_section(
                section_name, sections,
                material_group_to_swapkey={},
                swap_key_prefix="$swapkey",
                next_swap_key_num=150,
                used_swap_keys=set(),
                transparency_sections_to_add=OrderedDict(),
            )

            # 残留段不被重写，也不再生成新的命令列表
            self.assertEqual(sections[stale_name], stale_lines)
            self.assertEqual([key for key in sections if key.startswith("[CommandListSSMTTTLDraw_")], [stale_name])

            lines = sections[section_name]
            group = _group_for(lines, mesh_name)
            self.assertIn(f"run = CommandList{BS}TTL{BS}Draw", group)
            self.assertNotIn("CommandListSSMTTTLDraw_", "\n".join(lines))
            self.assertNotIn("ShadowVB", "\n".join(lines))

    def test_ttl_drag_hook_never_crosses_drawib(self):
        r"""回归（2026-10-07 实机：洛克茜模组模型爆炸）：别的 DrawIB 的 jiggle 影子 VB0
        严禁落到本段组里。

        实机链路：身体的 TTL 段带拖拽钩子，头发（另一个 DrawIB）的段没有 → 若跨段借用，
        头发 TTL 二次绘制会拿身体的 jiggle VB0 去配自己的 $\TTL\_1 区间（越界读）→ 乱
        三角形。现在每段的组只克隆自己的段头，索引缓冲始终是本段自己的 ib。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            BS = chr(92)
            ttl_path = os.path.join(temp_dir, "ttl.png")
            with open(ttl_path, "wb") as file_obj:
                file_obj.write(b"ttl")
            mesh_name = "LOD0.bbbb2222-200-0.中文中文_透明0.75_copy"
            obj = _FakeObject(mesh_name, {}, [("TTLMap_遮罩", ttl_path)])
            _fake_bpy.data.objects[obj.name] = obj

            sections = OrderedDict([
                # 兄弟段：拖拽钩子在此，属于另一个 DrawIB（aaaa1111）
                ("[TextureOverride_LOD0_aaaa1111_100_0_main]", [
                    "hash = aaaa1111",
                    "match_first_index = 0",
                    "    ; --- DRAG HOOK BEGIN aaaa1111P0_A ---",
                    "    if $ssmtdrag_drag_enabled_A >= 2 && $ssmtdrag_mode_A == 1",
                    "        vb0 = ResourceDragJiggleTempVB0_aaaa1111_A",
                    "    endif",
                    "    ; --- DRAG HOOK END aaaa1111P0_A ---",
                    "; [mesh:LOD0.aaaa1111-100-0.主体] [vertex_count:100]",
                    "drawindexed = 600, 0, 0",
                ]),
                # 本测试的处理对象：DrawIB = bbbb2222，段内没有钩子
                ("[TextureOverrideLOD0_bbbb2222_200_0_copy]", [
                    "hash = bbbb2222",
                    "match_first_index = 0",
                    "ib = Resource_LOD0.bbbb2222_200_0_Index",
                    f"run = CommandList{BS}ZZMI{BS}SetTextures",
                    "run = CommandListSkinTexture",
                    f"; [mesh:{mesh_name}] [vertex_count:15618]",
                    f"Resource{BS}TTL{BS}TransparencyTex = ref Resource_TTLMap_DiffuseMap_X",
                    f"${BS}TTL{BS}mask_channel = 3",
                    "drawindexed = 56376, 0, 0",
                ]),
                ("_config_path", temp_dir),
            ])

            node = node_postprocess_material.SSMTNode_PostProcess_Material()
            node.name = "MaterialNode"
            node.material_to_resource_override = False
            node.material_switch_var = "$swapkey150"
            node.process_texture_override_section(
                "[TextureOverrideLOD0_bbbb2222_200_0_copy]",
                sections,
                material_group_to_swapkey={},
                swap_key_prefix="$swapkey",
                next_swap_key_num=150,
                used_swap_keys=set(),
                transparency_sections_to_add=OrderedDict(),
            )

            self.assertEqual([key for key in sections if key.startswith("[CommandListSSMTTTLDraw_")], [])
            lines = sections["[TextureOverrideLOD0_bbbb2222_200_0_copy]"]
            group = _group_for(lines, mesh_name)
            group_text = "\n".join(group)
            self.assertIn(f"run = CommandList{BS}TTL{BS}Draw", group_text)
            self.assertNotIn("ResourceDragJiggleTempVB0_aaaa1111_A", group_text,
                             "别的 DrawIB 的 jiggle 影子 VB0 严禁落到本段")
            self.assertNotIn("CommandListSSMTTTLDraw_", group_text)
            self.assertNotIn("aaaa1111", group_text, "组里不得出现别段 DrawIB 的任何资源名")
            # 组不再重抄 ib/match_first_index，一对一由「绘制参数取自本块」证明
            self.assertEqual([line for line in group if line.strip().startswith("ib = ")], [])
            self.assertEqual([line for line in group if line.strip().startswith("match_first_index = ")], [])
            self.assertIn(f"${BS}TTL{BS}_1 = 56376", group_text,
                          "计数必须是本块 draw 的 56376，而不是兄弟段的 600")
            self.assertIn(f"${BS}TTL{BS}_2 = 0", group_text, "起始索引取自本块 draw")
            self.assertNotIn(f"${BS}TTL{BS}_1 = 600", group_text)
            self.assertIn("; [SSMT TTL 已接管] drawindexed = 56376, 0, 0", lines)

    def test_ttl_group_is_appended_after_last_block(self):
        """新契约①：TTL 组内联在原段，且位置在最后一个块之后（段尾）"""
        with tempfile.TemporaryDirectory() as temp_dir:
            BS = chr(92)
            ttl_path = os.path.join(temp_dir, "ttl.png")
            with open(ttl_path, "wb") as file_obj:
                file_obj.write(b"ttl")
            ttl_mesh = "LOD0.241deac5-56376-0.中文中文_copy"
            tail_mesh = "LOD0.241deac5-56376-0.裙子"
            ttl_obj = _FakeObject(ttl_mesh, {}, [("TTLMap_遮罩", ttl_path)])
            tail_obj = _FakeObject(tail_mesh, {}, [])
            _fake_bpy.data.objects[ttl_obj.name] = ttl_obj
            _fake_bpy.data.objects[tail_obj.name] = tail_obj

            section_name = "[TextureOverride_LOD0.241deac5_56376_0]"
            sections = OrderedDict([
                (section_name, [
                    "hash = 241deac5",
                    "match_first_index = 0",
                    "ib = Resource_LOD0.241deac5_56376_0_Index",
                    "run = CommandListSkinTexture",
                    f"[mesh:{ttl_mesh}]",
                    "drawindexed = 56376, 0, 0",
                    f"[mesh:{tail_mesh}]",
                    "drawindexed = 128, 56376, 0",
                ]),
                ("_config_path", temp_dir),
            ])

            node = node_postprocess_material.SSMTNode_PostProcess_Material()
            node.name = "MaterialNode"
            node.material_to_resource_override = False
            node.material_switch_var = "$swapkey150"
            node.process_texture_override_section(
                section_name, sections,
                material_group_to_swapkey={},
                swap_key_prefix="$swapkey",
                next_swap_key_num=150,
                used_swap_keys=set(),
                transparency_sections_to_add=OrderedDict(),
            )

            lines = sections[section_name]
            group = _group_for(lines, ttl_mesh)
            self.assertEqual(_group_count(lines), 1)
            self.assertGreater(lines.index(group[0]), lines.index(f"[mesh:{tail_mesh}]"))
            self.assertEqual(lines[-1], "; [/SSMT-TTL]")
            # 后面那个块原样留着
            self.assertIn(f"[mesh:{tail_mesh}]", _outside_group(lines, ttl_mesh))
            self.assertIn("drawindexed = 128, 56376, 0", _outside_group(lines, ttl_mesh))

    def test_ttl_take_over_comment_keeps_draw_arguments(self):
        """新契约②：原绘制行变成接管注释，参数留在行里（重导出时靠它取值）"""
        with tempfile.TemporaryDirectory() as temp_dir:
            BS = chr(92)
            ttl_path = os.path.join(temp_dir, "ttl.png")
            with open(ttl_path, "wb") as file_obj:
                file_obj.write(b"ttl")
            mesh_name = "LOD0.241deac5-56376-0.中文中文_copy"
            obj = _FakeObject(mesh_name, {}, [("TTLMap_遮罩", ttl_path)])
            _fake_bpy.data.objects[obj.name] = obj

            section_name = "[TextureOverride_LOD0.241deac5_56376_0]"
            sections = OrderedDict([
                (section_name, [
                    "hash = 241deac5",
                    "match_first_index = 0",
                    "ib = Resource_LOD0.241deac5_56376_0_Index",
                    "run = CommandListSkinTexture",
                    f"[mesh:{mesh_name}]",
                    "drawindexed = 56376, 271, 3",
                ]),
                ("_config_path", temp_dir),
            ])

            node = node_postprocess_material.SSMTNode_PostProcess_Material()
            node.name = "MaterialNode"
            node.material_to_resource_override = False
            node.material_switch_var = "$swapkey150"
            node.process_texture_override_section(
                section_name, sections,
                material_group_to_swapkey={},
                swap_key_prefix="$swapkey",
                next_swap_key_num=150,
                used_swap_keys=set(),
                transparency_sections_to_add=OrderedDict(),
            )

            lines = sections[section_name]
            taken_line = "; [SSMT TTL 已接管] drawindexed = 56376, 271, 3"
            self.assertIn(taken_line, lines)
            # 原绘制行不再生效：段里没有裸 drawindexed
            self.assertEqual(
                [line for line in lines if line.strip().lower().startswith("drawindexed")],
                [],
            )
            # 组内参数与那一行完全一致
            group = _group_for(lines, mesh_name)
            self.assertLess(lines.index(taken_line), lines.index(group[0]))
            self.assertIn(f"${BS}TTL{BS}_1 = 56376", group)
            self.assertIn(f"${BS}TTL{BS}_2 = 271", group)
            self.assertIn(f"${BS}TTL{BS}_3 = 3", group)

    def test_ttl_rerun_is_idempotent(self):
        """新契约③：同段跑两次，旧组被摘掉再加一份，参数不叠加"""
        with tempfile.TemporaryDirectory() as temp_dir:
            BS = chr(92)
            ttl_path = os.path.join(temp_dir, "ttl.png")
            with open(ttl_path, "wb") as file_obj:
                file_obj.write(b"ttl")
            mesh_name = "LOD0.241deac5-56376-0.中文中文_copy"
            obj = _FakeObject(mesh_name, {}, [("TTLMap_遮罩", ttl_path)])
            _fake_bpy.data.objects[obj.name] = obj

            section_name = "[TextureOverride_LOD0.241deac5_56376_0]"
            sections = OrderedDict([
                (section_name, [
                    "hash = 241deac5",
                    "match_first_index = 0",
                    "ib = Resource_LOD0.241deac5_56376_0_Index",
                    "run = CommandListSkinTexture",
                    f"[mesh:{mesh_name}]",
                    "drawindexed = 56376, 0, 0",
                ]),
                ("_config_path", temp_dir),
            ])

            node = node_postprocess_material.SSMTNode_PostProcess_Material()
            node.name = "MaterialNode"
            node.material_to_resource_override = False
            node.material_switch_var = "$swapkey150"

            def _process():
                node.process_texture_override_section(
                    section_name, sections,
                    material_group_to_swapkey={},
                    swap_key_prefix="$swapkey",
                    next_swap_key_num=150,
                    used_swap_keys=set(),
                    transparency_sections_to_add=OrderedDict(),
                )

            _process()
            once = list(sections[section_name])
            once_constants = list(sections.get("[Constants]", []))
            _process()

            self.assertEqual(sections[section_name], once)
            self.assertEqual(sections.get("[Constants]", []), once_constants)
            lines = sections[section_name]
            self.assertEqual(_group_count(lines), 1)
            alpha_line = f"${BS}TTL{BS}alpha = $TTLAlpha1_0"
            self.assertEqual([line for line in lines if line.strip() == alpha_line], [alpha_line])
            self.assertEqual([line for line in lines if line.strip().startswith(TTL_GROUP_HEAD)], [f"{TTL_GROUP_HEAD} {mesh_name}"])
            self.assertEqual([line for line in lines if line.strip() == TTL_GROUP_TAIL], [TTL_GROUP_TAIL])

    def test_ttl_draw_lines_preserve_drag_vis_flags(self):
        """TTL 块重建必须保留拖拽物体显隐 flag 行（否则隐藏判定失效）。"""
        node = node_postprocess_material.SSMTNode_PostProcess_Material()
        block = [
            "if $swapkey6 == 5 && $swapkey4 == 2",
            "    $ssmtdrag_objvis_A_30 = 1",
            "    drawindexed = 15255, 272901, 0",
            "endif",
        ]
        ttl_lines, found = node._build_ttl_draw_lines(block)

        self.assertTrue(found)
        stripped = [str(line).strip() for line in ttl_lines]
        self.assertIn("$ssmtdrag_objvis_A_30 = 1", stripped)
        self.assertIn("$" + chr(92) + "TTL" + chr(92) + "_1 = 15255", stripped)

    def test_ttl_draw_lines_preserve_flags_when_block_starts_mid_if(self):
        r"""TTL 块边界从 mesh 注释行开始（if 头在块外）时，flag 与 drawindexed
        都走非 if 路径——flag 必须在 $\TTL 参数之前被保留（回归：此前整批 54
        个 TTL 段标志被静默丢弃，导致可见 TTL 副本无法被命中）。"""
        node = node_postprocess_material.SSMTNode_PostProcess_Material()
        block = [
            "; [mesh:LOD0.241deac5-56376-0.服装03主体替换_copy] [vertex_count:2702]",
            f"Resource{chr(92)}ZZMI{chr(92)}Diffuse = ref Resource_DiffuseMap_X",
            "    $ssmtdrag_objvis_A_30 = 1",
            "  drawindexed = 15255,272901,0",
            "endif",
        ]
        ttl_lines, found = node._build_ttl_draw_lines(block)

        self.assertTrue(found)
        stripped = [str(line).strip() for line in ttl_lines]
        self.assertIn("$ssmtdrag_objvis_A_30 = 1", stripped)
        arg1_idx = stripped.index("$" + chr(92) + "TTL" + chr(92) + "_1 = 15255")
        flag_idx = stripped.index("$ssmtdrag_objvis_A_30 = 1")
        self.assertLess(flag_idx, arg1_idx, "flag 必须在 TTL 参数/绘制之前")
        self.assertNotIn("drawindexed =", stripped)

    def test_ttl_draw_lines_preserve_bare_flags_before_bare_draw(self):
        """无 if 包裹的 bare flag + bare drawindexed：flag 同样必须保留。"""
        node = node_postprocess_material.SSMTNode_PostProcess_Material()
        block = [
            "$ssmtdrag_objvis_A_7 = 1",
            "drawindexed = 618,60234,0",
        ]
        ttl_lines, found = node._build_ttl_draw_lines(block)

        self.assertTrue(found)
        stripped = [str(line).strip() for line in ttl_lines]
        self.assertIn("$ssmtdrag_objvis_A_7 = 1", stripped)
        arg1_idx = stripped.index("$" + chr(92) + "TTL" + chr(92) + "_1 = 618")
        flag_idx = stripped.index("$ssmtdrag_objvis_A_7 = 1")
        self.assertLess(flag_idx, arg1_idx)

    def test_transparency_suffix_alone_triggers_transparency_code_not_ttl(self):
        """澄清：_透明 后缀不再自动触发 TTL/FX，仅独立生成透明代码（CustomShaderTransparencyCloth）"""
        with tempfile.TemporaryDirectory() as temp_dir:
            BS = chr(92)
            diffuse_path = os.path.join(temp_dir, "diffuse.png")
            with open(diffuse_path, "wb") as file_obj:
                file_obj.write(b"diffuse")
            obj = _FakeObject("Body_透明0.5", {}, [("DiffuseMap_衣服", diffuse_path)])
            _fake_bpy.data.objects[obj.name] = obj

            sections = OrderedDict([
                ("[TextureOverride_Test]", [
                    "hash = 12345678",
                    "match_first_index = 0",
                    "ib = Resource_Body_Index",
                    f"Resource{BS}ZZMI{BS}Diffuse = ref Resource-old-DiffuseMap",
                    f"run = CommandList{BS}ZZMI{BS}SetTextures",
                    "run = CommandListSkinTexture",
                    "[mesh:Body_透明0.5]",
                    "drawindexed = 100, 5, 0",
                ]),
                ("_config_path", temp_dir),
            ])
            transparency_sections = OrderedDict()
            node = node_postprocess_material.SSMTNode_PostProcess_Material()
            node.name = "MaterialNode"
            node.material_to_resource_override = False
            node.material_switch_var = "$swapkey150"
            node.process_texture_override_section(
                "[TextureOverride_Test]", sections, material_group_to_swapkey={},
                swap_key_prefix="$swapkey", next_swap_key_num=150, used_swap_keys=set(),
                transparency_sections_to_add=transparency_sections,
            )

            self.assertEqual(len(transparency_sections), 1)
            transparency_lines = next(iter(transparency_sections.values()))
            self.assertIn("blend_factor[0] = 0.5", transparency_lines)
            self.assertIn("drawindexed = 100, 5, 0", transparency_lines)
            override_lines = sections["[TextureOverride_Test]"]
            self.assertTrue(any(line.startswith("run = CustomShaderTransparencyCloth") for line in override_lines))

            joined = "\n".join(str(line) for key, ls in sections.items() for line in ls if key != "_config_path")
            self.assertNotIn(r"CommandList\TTL\Draw", joined)
            self.assertNotIn(f"Resource{BS}TTL{BS}TransparencyTex", joined)
            self.assertNotIn(r"$\TTL\alpha", joined)
            self.assertNotIn("Resource" + BS + "RabbitFX" + BS + "FXMap", joined)
            self.assertNotIn("Resource" + BS + "NTEMIFX" + BS + "FXMap", joined)
            self.assertFalse(any(key.startswith("[TextureOverrideBody_") for key in sections))

    def test_ttl_fx_and_ttl_conflict_skips_ttl_and_keeps_fx(self):
        """同一物体同时挂 FXMap 与 TTLMap 材质：TTL 检测跳过并警告，FX 按原流程保留"""
        with tempfile.TemporaryDirectory() as temp_dir:
            BS = chr(92)
            fx_path = os.path.join(temp_dir, "fx.png")
            ttl_path = os.path.join(temp_dir, "ttl.png")
            for path in (fx_path, ttl_path):
                with open(path, "wb") as file_obj:
                    file_obj.write(os.path.basename(path).encode("ascii"))
            obj = _FakeObject("Body", {}, [("FXMap_特效", fx_path), ("TTLMap_遮罩", ttl_path)])
            _fake_bpy.data.objects[obj.name] = obj

            warnings_captured = []
            log_module = sys.modules[f"{PKG}.utils.log_utils"]
            original_warning = log_module.LOG.warning
            log_module.LOG.warning = lambda *args, **_kwargs: warnings_captured.append(args)
            try:
                sections = OrderedDict([
                    ("[TextureOverride_Body]", [
                        "hash = 12345678",
                        "match_first_index = 0",
                        "ib = Resource_Body_Index",
                        "run = CommandListSkinTexture",
                        "[mesh:Body]",
                        "drawindexed = 100, 5, 0",
                    ]),
                    ("_config_path", temp_dir),
                ])
                node = node_postprocess_material.SSMTNode_PostProcess_Material()
                node.name = "MaterialNode"
                node.material_to_resource_override = False
                node.material_switch_var = "$swapkey150"
                node.process_texture_override_section(
                    "[TextureOverride_Body]", sections, material_group_to_swapkey={},
                    swap_key_prefix="$swapkey", next_swap_key_num=150, used_swap_keys=set(),
                    transparency_sections_to_add=OrderedDict(),
                )
            finally:
                log_module.LOG.warning = original_warning

            override_lines = sections["[TextureOverride_Body]"]
            joined = "\n".join(str(line) for line in override_lines)
            self.assertIn(f"Resource{BS}RabbitFX{BS}FXMap = ref Resource_FXMap_", joined)
            self.assertTrue(any("不能同时启用 FX 与 TTL" in str(warning) for warning in warnings_captured))
            self.assertNotIn(f"Resource{BS}TTL{BS}TransparencyTex", joined)
            self.assertNotIn(r"CommandList\TTL\Draw", joined)
            self.assertFalse(any(
                key.startswith("[TextureOverrideBody") and key != "[TextureOverride_Body]"
                for key in sections
            ))

    def test_debug_disable_fx_ttl_skips_fxmap_generation(self):
        """调试开关：开启后不生成 FX（FXMap→RabbitFX/NTEMIFX）段落"""
        with tempfile.TemporaryDirectory() as temp_dir:
            BS = chr(92)
            fx_path = os.path.join(temp_dir, "fx.png")
            glow_path = os.path.join(temp_dir, "glow.png")
            for path in (fx_path, glow_path):
                with open(path, "wb") as file_obj:
                    file_obj.write(os.path.basename(path).encode("ascii"))
            obj = _FakeObject("Body", {}, [
                ("FXMap_特效", fx_path),
                ("Glowmap_发光", glow_path),
            ])
            _fake_bpy.data.objects[obj.name] = obj

            sections = OrderedDict([
                ("[TextureOverride_Body]", [
                    "hash = 12345678",
                    "match_first_index = 0",
                    "ib = Resource_Body_Index",
                    "run = CommandListSkinTexture",
                    "[mesh:Body]",
                    "drawindexed = 100, 5, 0",
                ]),
                ("_config_path", temp_dir),
            ])
            node = node_postprocess_material.SSMTNode_PostProcess_Material()
            node.name = "MaterialNode"
            node.material_to_resource_override = False
            node.material_switch_var = "$swapkey150"
            node.debug_disable_fx_ttl = True
            node.process_texture_override_section(
                "[TextureOverride_Body]", sections, material_group_to_swapkey={},
                swap_key_prefix="$swapkey", next_swap_key_num=150, used_swap_keys=set(),
                transparency_sections_to_add=OrderedDict(),
            )

            joined = "\n".join(str(line) for line in sections["[TextureOverride_Body]"])
            self.assertNotIn(f"Resource{BS}RabbitFX{BS}FXMap = ref Resource_FXMap_", joined)
            self.assertNotIn(f"Resource{BS}NTEMIFX{BS}FXMap = ref", joined)
            self.assertNotIn(f"Resource{BS}RabbitFX{BS}FXMap = ref null", joined)
            # Glowmap 不属于 FX/TTL，调试开关不拦截
            self.assertIn(f"Resource{BS}RabbitFX{BS}Glowmap = ref Resource_Glowmap_", joined)

    def test_debug_disable_fx_ttl_skips_ttl_generation(self):
        """调试开关：开启后不生成 TTL（TTLMap）段落，但透明代码仍独立生成"""
        sys.modules[f"{PKG}.common.global_config"].GlobalConfig.logic_name = "ZZMI"
        with tempfile.TemporaryDirectory() as temp_dir:
            BS = chr(92)
            diffuse_path = os.path.join(temp_dir, "diffuse.png")
            ttl_path = os.path.join(temp_dir, "ttl.png")
            for path in (diffuse_path, ttl_path):
                with open(path, "wb") as file_obj:
                    file_obj.write(os.path.basename(path).encode("ascii"))
            obj = _FakeObject("Body_透明0.5", {}, [
                ("DiffuseMap_衣服", diffuse_path),
                ("TTLMap_遮罩", ttl_path),
            ])
            _fake_bpy.data.objects[obj.name] = obj

            sections = OrderedDict([
                ("[TextureOverride_Body]", [
                    "hash = 12345678",
                    "match_first_index = 0",
                    "ib = Resource_Body_Index",
                    f"Resource{BS}ZZMI{BS}Diffuse = ref Resource-old-DiffuseMap",
                    f"run = CommandList{BS}ZZMI{BS}SetTextures",
                    "run = CommandListSkinTexture",
                    "[mesh:Body_透明0.5]",
                    "drawindexed = 100, 5, 0",
                ]),
                ("_config_path", temp_dir),
            ])
            transparency_sections = OrderedDict()
            node = node_postprocess_material.SSMTNode_PostProcess_Material()
            node.name = "MaterialNode"
            node.material_to_resource_override = False
            node.material_switch_var = "$swapkey150"
            node.debug_disable_fx_ttl = True
            node.process_texture_override_section(
                "[TextureOverride_Body]", sections, material_group_to_swapkey={},
                swap_key_prefix="$swapkey", next_swap_key_num=150, used_swap_keys=set(),
                transparency_sections_to_add=transparency_sections,
            )

            joined = "\n".join(str(line) for key, ls in sections.items() for line in ls if key != "_config_path")
            self.assertNotIn(r"CommandList\TTL\Draw", joined)
            self.assertNotIn(f"Resource{BS}TTL{BS}TransparencyTex", joined)
            self.assertNotIn("$" + BS + "TTL" + BS + "alpha", joined)
            self.assertNotIn("$" + BS + "TTL" + BS + "mask_invert", joined)
            self.assertFalse(any(key.startswith("[TextureOverrideBody_") for key in sections))
            # 透明代码与 FX/TTL 独立，仍正常生成
            self.assertEqual(len(transparency_sections), 1)
            transparency_lines = next(iter(transparency_sections.values()))
            self.assertIn("blend_factor[0] = 0.5", transparency_lines)

    def test_previous_transparency_tail_is_replaced_without_losing_auto_appended_tail(self):
        node = node_postprocess_material.SSMTNode_PostProcess_Material()
        auto_marker = node.AUTO_APPENDED_SECTION_MARKERS[0]
        transparency_name = "CustomShaderTransparencyClothBody_透明0.5"
        content = (
            "[TextureOverride_Test]\n"
            "hash = abc\n"
            "[mesh:Body_透明0.5]\n"
            f"run = {transparency_name}\n\n"
            f"{node.TRANSPARENCY_SECTION_MARKER}\n"
            f"[{transparency_name}]\n"
            "blend = ADD BLEND_FACTOR INV_BLEND_FACTOR\n"
            "handling = skip\n"
            "; --- Start of Overridden Mesh Content ---\n"
            "drawindexed = 3,0,0\n\n"
            f"{auto_marker}\n"
            "[CommandListTail]\n"
            "run = CommandListTail\n"
        )

        stripped = node._strip_previous_transparency_sections(content)

        self.assertNotIn(f"run = {transparency_name}", stripped)
        self.assertNotIn(f"[{transparency_name}]", stripped)
        self.assertIn("drawindexed = 3,0,0", stripped)
        self.assertIn(auto_marker, stripped)
        self.assertIn("[CommandListTail]", stripped)

    def test_execute_postprocess_processes_efmi_merged_draw_callback(self):
        """EFMI 回调支持普通材质与 FX，但必须完全跳过 ZZMI 专属 TTL。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            texture_path = os.path.join(temp_dir, "diffuse.png")
            with open(texture_path, "wb") as file_obj:
                file_obj.write(b"diffuse")
            fx_path = os.path.join(temp_dir, "fx.png")
            with open(fx_path, "wb") as file_obj:
                file_obj.write(b"fx")
            ttl_path = os.path.join(temp_dir, "ttl.png")
            with open(ttl_path, "wb") as file_obj:
                file_obj.write(b"ttl")

            object_name = "LOD0.drawhash-12-56.Body"
            obj = _FakeObject(
                object_name,
                {
                    "ps-t0": {
                        "mark_name": "DiffuseMap",
                        "mark_type": "Slot",
                        "mark_slot": "ps-t0",
                    }
                },
                [
                    ("DiffuseMap_Body", texture_path),
                    ("FXMap_Body", fx_path),
                    ("TTLMap_Body", ttl_path),
                ],
            )
            _fake_bpy.data.objects[obj.name] = obj
            sys.modules[f"{PKG}.common.global_config"].GlobalConfig.logic_name = "EFMI"

            ini_path = os.path.join(temp_dir, "EFMIMerged.ini")
            callback_name = "CommandList_Draw_LOD0.drawhash_12_56"
            with open(ini_path, "w", encoding="utf-8") as file_obj:
                file_obj.write(
                    "[TextureOverride_EntryPoint_LOD0.drawhash_12_56]\n"
                    "hash = drawhash\n"
                    "match_first_index = 56\n"
                    "match_index_count = 12\n"
                    "handling = skip\n"
                    "$\\EFMIv1\\component_id = 0\n"
                    "CommandList\\EFMIv1\\Callback_Component_DrawCustom = ref "
                    f"{callback_name}\n"
                    "run = CommandList_Component_DrawInstances_LOD0\n\n"
                    f"[{callback_name}]\n"
                    "run = CommandList\\EFMIv1\\OverrideTextures\n"
                    "ib = Resource_LOD0.drawhash_12_56_Index\n"
                    "ps-t0 = Resource-old-DiffuseMap\n"
                    f"; [mesh:{object_name}] [vertex_count:77]\n"
                    "drawindexedinstanced = 12,INSTANCE_COUNT,34,0,FIRST_INSTANCE\n"
                )

            node = node_postprocess_material.SSMTNode_PostProcess_Material()
            node.name = "MaterialNode"
            node.material_to_resource_override = False
            node.material_switch_var = "$swapkey150"
            node.debug_disable_fx_ttl = False
            node._create_cumulative_backup = lambda *_args, **_kwargs: None
            node._process_ttl_sections = mock.Mock(wraps=node._process_ttl_sections)

            node.execute_postprocess(temp_dir)

            result = Path(ini_path).read_text(encoding="utf-8")
            node.execute_postprocess(temp_dir)
            self.assertEqual(Path(ini_path).read_text(encoding="utf-8"), result)

            callback_body = result.split(f"[{callback_name}]", 1)[1].split("\n[", 1)[0]
            self.assertIn("ps-t0 = Resource_DiffuseMap_Body", callback_body)
            self.assertIn("run = CommandList\\EFMIv1\\OverrideTextures", callback_body)
            self.assertIn(
                "drawindexedinstanced = 12,INSTANCE_COUNT,34,0,FIRST_INSTANCE",
                callback_body,
            )
            self.assertIn("[Resource_DiffuseMap_Body]", result)
            self.assertIn(r"Resource\RabbitFX\FXMap = ref Resource_FXMap_Body", callback_body)
            self.assertIn(r"run = CommandList\RabbitFX\Run", callback_body)
            self.assertIn("[Resource_FXMap_Body]", result)
            self.assertNotIn("Resource\\TTL\\", result)
            self.assertNotIn(r"CommandList\TTL\Draw", result)
            self.assertNotIn("[Resource_TTLMap_Body]", result)
            node._process_ttl_sections.assert_not_called()
            # 本机没装 EFMITTL 库（GlobalConfig 是裸 SimpleNamespace），所以 EFMI 的
            # 半透明也不得改成库调用：绘制行必须原样留着，否则部件会直接消失。
            self.assertNotIn(r"CommandList\EFMITTL\Draw", result)
            self.assertNotIn(r"Resource\EFMITTL\Mask", result)

    def _make_efmi_ttl_fixture(self, temp_dir, draw_args):
        """造一个 EFMI 合并绘制回调段（带 TTLMap 材质的物体），返回段与段名。"""
        diffuse_path = os.path.join(temp_dir, "diffuse.png")
        with open(diffuse_path, "wb") as file_obj:
            file_obj.write(b"diffuse")
        ttl_path = os.path.join(temp_dir, "ttl.png")
        with open(ttl_path, "wb") as file_obj:
            file_obj.write(b"ttl")
        object_name = "LOD0.drawhash-12-56.Body"
        obj = _FakeObject(
            object_name,
            {
                "ps-t0": {
                    "mark_name": "DiffuseMap",
                    "mark_type": "Slot",
                    "mark_slot": "ps-t0",
                }
            },
            [("DiffuseMap_Body", diffuse_path), ("TTLMap_Body", ttl_path)],
        )
        _fake_bpy.data.objects[obj.name] = obj
        sys.modules[f"{PKG}.common.global_config"].GlobalConfig.logic_name = "EFMI"
        callback_name = "CommandList_Draw_LOD0.drawhash_12_56"
        sections = OrderedDict([
            (f"[{callback_name}]", [
                r"run = CommandList\EFMIv1\OverrideTextures",
                "ib = Resource_LOD0.drawhash_12_56_Index",
                "ps-t0 = Resource-old-DiffuseMap",
                f"; [mesh:{object_name}] [vertex_count:77]",
                f"drawindexedinstanced = {draw_args}",
            ]),
            ("_config_path", temp_dir),
        ])
        return sections, f"[{callback_name}]"

    def _run_efmi_ttl(self, sections, section_name):
        node = node_postprocess_material.SSMTNode_PostProcess_Material()
        node.name = "MaterialNode"
        node.material_to_resource_override = False
        node.material_switch_var = "$swapkey150"
        node.debug_disable_fx_ttl = False
        node._process_ttl_sections = mock.Mock()
        node.process_texture_override_section(
            section_name, sections, material_group_to_swapkey={},
            swap_key_prefix="$swapkey", next_swap_key_num=150, used_swap_keys=set(),
            transparency_sections_to_add=OrderedDict(),
        )
        return node

    def test_efmi_ttl_hands_draw_to_efmittl_library(self):
        """装了 EFMITTL 时，EFMI 的绘制行换成库的调用约定，且不新建绘制段。"""
        BS = chr(92)
        with tempfile.TemporaryDirectory() as temp_dir:
            sections, section_name = self._make_efmi_ttl_fixture(
                temp_dir, "12,INSTANCE_COUNT,34,0,FIRST_INSTANCE")
            with mock.patch.object(
                node_postprocess_material.SSMTNode_PostProcess_Material,
                "_efmi_ttl_library_present", return_value=True,
            ):
                node = self._run_efmi_ttl(sections, section_name)

            body = "\n".join(str(line) for line in sections[section_name])
            # 原来的绘制换成了库调用：库自己按通道决定阴影/前向/预通道怎么画。
            self.assertNotIn("drawindexedinstanced", body)
            self.assertIn(f"run = CommandList{BS}EFMITTL{BS}Draw", body)
            # 索引数/起始索引来自原来那一笔，遮罩来自 TTLMap 材质。
            self.assertIn(f"${BS}EFMITTL{BS}count = 12", body)
            self.assertIn(f"${BS}EFMITTL{BS}first = 34", body)
            self.assertIn(f"Resource{BS}EFMITTL{BS}Mask = ref Resource_TTLMap_Body", body)
            self.assertIn(f"${BS}EFMITTL{BS}mask_channel = 3", body)
            self.assertIn(f"${BS}EFMITTL{BS}mask_invert = 1", body)
            self.assertIn("[Resource_TTLMap_Body]", sections)
            # 没有 _透明 数值就不写 alpha：库按「有遮罩 ⇒ 1，交给遮罩决定」处理。
            self.assertNotIn(f"${BS}EFMITTL{BS}alpha", body)
            # 参数行必须排在库调用之前，顺序错了库就读不到。
            self.assertLess(body.index(f"${BS}EFMITTL{BS}count"), body.index(f"run = CommandList{BS}EFMITTL{BS}Draw"))
            self.assertLess(body.index(f"Resource{BS}EFMITTL{BS}Mask"), body.index(f"run = CommandList{BS}EFMITTL{BS}Draw"))
            # EFMI 不重建段、也不走 ZZMI 的 TTL 链路。
            node._process_ttl_sections.assert_not_called()
            self.assertEqual(
                sorted(name for name in sections if name.startswith("[")),
                sorted([section_name, "[Resource_TTLMap_Body]",
                        "[Resource_DiffuseMap_Body]"]),
            )

    def test_efmi_ttl_generation_is_idempotent(self):
        """重复导出同一个回调段时，库调用块不会叠第二遍。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            sections, section_name = self._make_efmi_ttl_fixture(
                temp_dir, "12,INSTANCE_COUNT,34,0,FIRST_INSTANCE")
            with mock.patch.object(
                node_postprocess_material.SSMTNode_PostProcess_Material,
                "_efmi_ttl_library_present", return_value=True,
            ):
                self._run_efmi_ttl(sections, section_name)
                first = "\n".join(str(line) for line in sections[section_name])
                self._run_efmi_ttl(sections, section_name)
                second = "\n".join(str(line) for line in sections[section_name])

        self.assertEqual(first, second)
        self.assertEqual(second.count(r"CommandList\EFMITTL\Draw"), 1)

    def test_efmi_ttl_is_skipped_when_library_missing(self):
        """没装 EFMITTL 就保留原绘制并出声——不能把部件画没。"""
        log_module = sys.modules[f"{PKG}.utils.log_utils"]
        with tempfile.TemporaryDirectory() as temp_dir:
            sections, section_name = self._make_efmi_ttl_fixture(
                temp_dir, "12,INSTANCE_COUNT,34,0,FIRST_INSTANCE")
            with mock.patch.object(log_module.LOG, "warning") as warning_mock:
                node = self._run_efmi_ttl(sections, section_name)

            body = "\n".join(str(line) for line in sections[section_name])
            self.assertIn("drawindexedinstanced = 12,INSTANCE_COUNT,34,0,FIRST_INSTANCE", body)
            self.assertNotIn("EFMITTL", body)
            self.assertTrue(warning_mock.called)
            self.assertIn("EFMITTL", str(warning_mock.call_args))
            node._process_ttl_sections.assert_not_called()

    def test_efmi_ttl_draw_args_only_accepts_single_instance_draws(self):
        """只有「单实例、无 base vertex、纯数字」的绘制才能交接给库。"""
        parse = node_postprocess_material.SSMTNode_PostProcess_Material._efmi_ttl_draw_args
        self.assertEqual(parse("59679,INSTANCE_COUNT,0,0,FIRST_INSTANCE"), ("59679", "0"))
        self.assertEqual(parse("12, 1, 34, 0"), ("12", "34"))
        self.assertEqual(parse("3,0,0"), ("3", "0"))        # 多实例：库只画一个实例，交接会丢掉其余的实例。
        self.assertIsNone(parse("12,4,34,0,FIRST_INSTANCE"))
        # base vertex 非 0：库把那一位写死成 0。
        self.assertIsNone(parse("12,INSTANCE_COUNT,34,7,FIRST_INSTANCE"))
        self.assertIsNone(parse("12,INSTANCE_COUNT,34,0,FIRST_INSTANCE,extra"))
        # 非数字（循环/变量形式）：算不出 count/first。
        self.assertIsNone(parse("$count,INSTANCE_COUNT,0,0,FIRST_INSTANCE"))
        self.assertIsNone(parse(""))

    def test_ttl_parser_is_restricted_to_zzmi_logic(self):
        """TTL 协议只属于 ZZMI；其它所有当前逻辑都不得进入 TTL 解析器。"""
        sections = OrderedDict([
            ("[TextureOverride_Body]", [
                "hash = 12345678",
                "[mesh:Body]",
                "drawindexed = 3, 0, 0",
            ]),
            ("_config_path", ""),
        ])

        for logic_name in (
            "EFMI", "HTMI", "NTEMI", "ZZMIDX12", "GIMI", "HIMI", "SRMI", "WWMI",
        ):
            with self.subTest(logic_name=logic_name):
                sys.modules[f"{PKG}.common.global_config"].GlobalConfig.logic_name = logic_name
                node = node_postprocess_material.SSMTNode_PostProcess_Material()
                node.name = "MaterialNode"
                node.material_to_resource_override = False
                node.debug_disable_fx_ttl = False
                node._process_ttl_sections = mock.Mock()
                node.process_texture_override_section(
                    "[TextureOverride_Body]",
                    OrderedDict((name, list(lines) if isinstance(lines, list) else lines)
                                for name, lines in sections.items()),
                    material_group_to_swapkey={},
                    swap_key_prefix="$swapkey",
                    next_swap_key_num=150,
                    used_swap_keys=set(),
                    transparency_sections_to_add=OrderedDict(),
                )
                node._process_ttl_sections.assert_not_called()

    def test_ini_parser_preserves_namespace_preamble(self):
        node = node_postprocess_material.SSMTNode_PostProcess_Material()

        preamble, sections = node._parse_ini_content(
            "namespace = Example\\Mod\n; header\n\n[Constants]\nglobal $x = 1\n"
        )

        self.assertEqual(preamble, ["namespace = Example\\Mod", "; header", ""])
        self.assertEqual(sections["[Constants]"], ["global $x = 1"])

    def test_ini_serialization_is_idempotent_with_namespace_preamble(self):
        node = node_postprocess_material.SSMTNode_PostProcess_Material()
        content = "namespace = Example\\Mod\n; header\n\n[Constants]\nglobal $x = 1\n"

        preamble, sections = node._parse_ini_content(content)
        first_output = node._serialize_ini_content(preamble, sections)
        preamble, sections = node._parse_ini_content(first_output)
        second_output = node._serialize_ini_content(preamble, sections)

        self.assertEqual(second_output, first_output)
        self.assertIn("namespace = Example\\Mod\n; header\n\n[Constants]", second_output)


    def _run_transparency_move(self, temp_dir, body_lines):
        """跑一次材质后处理，返回被搬进透明段的块内容。"""
        BS = chr(92)
        sections = OrderedDict([
            ("[TextureOverride_Body]", body_lines),
            ("_config_path", temp_dir),
        ])
        transparency_sections = OrderedDict()
        node = node_postprocess_material.SSMTNode_PostProcess_Material()
        node.name = "MaterialNode"
        node.material_to_resource_override = False
        node.material_switch_var = "$swapkey150"
        node.debug_disable_fx_ttl = True
        node.process_texture_override_section(
            "[TextureOverride_Body]", sections, material_group_to_swapkey={},
            swap_key_prefix="$swapkey", next_swap_key_num=150, used_swap_keys=set(),
            transparency_sections_to_add=transparency_sections,
        )
        self.assertEqual(len(transparency_sections), 1)
        return "\n".join(
            str(line) for line in next(iter(transparency_sections.values()))
        )

    def _transparency_move_fixture(self, temp_dir, tail_lines):
        """造化透明物体与段骨架；tail_lines 是 mesh 行之后的段内容。"""
        BS = chr(92)
        diffuse_path = os.path.join(temp_dir, "diffuse.png")
        with open(diffuse_path, "wb") as file_obj:
            file_obj.write(b"diffuse")
        obj = _FakeObject("Body_透明0.5", {}, [("DiffuseMap_衣服", diffuse_path)])
        _fake_bpy.data.objects[obj.name] = obj
        sys.modules[f"{PKG}.common.global_config"].GlobalConfig.logic_name = "ZZMI"
        body_lines = [
            "hash = 12345678",
            "match_first_index = 0",
            "ib = Resource_Body_Index",
            f"Resource{BS}ZZMI{BS}Diffuse = ref Resource-old-DiffuseMap",
            f"run = CommandList{BS}ZZMI{BS}SetTextures",
            "[mesh:Body_透明0.5]",
        ] + list(tail_lines)
        return self._run_transparency_move(temp_dir, body_lines)

    def test_transparency_move_tolerates_fx_run_line_without_alias(self):
        """块内只有一条孤立的 FX Run 行时必须照样走完（回归：NameError prev_line）。

        旧实现在重构时把 ``for prev_line in final_block`` 生成器丢掉，于是只有在
        块里出现 FX Run 行时才会求值 ``prev_line``，直出整条链路直接抛
        ``NameError: name 'prev_line' is not defined``。这里把 ``_FX_RUN_LINES``
        换成哨兵：真实 FX 行会被材质转资源阶段重写，哨兵行只负责让那条分支可达。
        """
        sentinel = f"run = CommandList{chr(92)}RabbitFX{chr(92)}SENTINEL"
        with mock.patch.object(
            node_postprocess_material, "_FX_RUN_LINES", (sentinel,)
        ):
            with tempfile.TemporaryDirectory() as temp_dir:
                joined = self._transparency_move_fixture(temp_dir, [
                    sentinel,
                    "drawindexed = 100, 5, 0",
                ])

        self.assertIn("drawindexed = 100, 5, 0", joined)
        # 整段没有别名绑定，这条 Run 是空转，按设计丢弃。
        self.assertNotIn(sentinel, joined)

    def test_transparency_move_keeps_fx_run_line_after_alias(self):
        """块内先出现 FX 资源别名时，紧随其后的 Run 行必须继续被搬走。

        别名写成注释形式：真正的 ``Resource\\RabbitFX\\FXMap = ref …`` 绑定会被
        材质转资源阶段重写掉，注释行则原样留在块里，正好用来验证
        ``has_resource_before`` 是对**整段已收集行**做前缀匹配，而不是只看相邻行。
        """
        BS = chr(92)
        sentinel_run = f"run = CommandList{BS}RabbitFX{BS}SENTINEL"
        sentinel_alias = f"; Resource{BS}RabbitFX{BS}FXMap = ref Resource_FXMap_Body"
        with mock.patch.object(
            node_postprocess_material, "_FX_RUN_LINES", (sentinel_run,)
        ):
            with tempfile.TemporaryDirectory() as temp_dir:
                joined = self._transparency_move_fixture(temp_dir, [
                    sentinel_alias,
                    sentinel_run,
                    "drawindexed = 100, 5, 0",
                ])

        self.assertIn(sentinel_alias, joined)
        self.assertIn(sentinel_run, joined)
        self.assertIn("drawindexed = 100, 5, 0", joined)


if __name__ == "__main__":
    unittest.main()
