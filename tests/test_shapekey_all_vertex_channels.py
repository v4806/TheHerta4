"""形态键「存储全部顶点属性增量」选项的行为测试。

覆盖三侧契约：
- 通道计划解析（`_resolve_delta_channel_plan` / `_resolve_delta_stride`）：关闭时恒为
  仅位置且步长 12（与旧版逐字节一致）；开启时按顶点数据类型展开，切线只取 xyz，
  超出实际每顶点 float 数的通道被裁掉。
- 着色器生成（`_update_shader_file`）：关闭时增量缓冲仍是 `StructuredBuffer<float3>`、
  写回只有位置；开启时换成 `ShapeKeyDelta` 结构体、按通道累加并按通道写回。
- 数据生成（`_process_merged_shapekey_Meshess`）：关闭时增量记录 3 float/条，
  开启时 9 float/条，且法线/切线分量确实来自形态键缓冲。
"""
import importlib.util
import json
import re
import sys
import tempfile
import types
import unittest
from pathlib import Path

import numpy as np

from tests import _real_modules


def _install_module(name, **attrs):
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


PKG = "_shapekey_all_vertex_channels_test_pkg"
for package_name in (PKG, f"{PKG}.blueprint", f"{PKG}.common", f"{PKG}.utils"):
    package = _install_module(package_name)
    package.__path__ = []

_real_modules.register_real_common_modules(f"{PKG}.common")

_fake_bpy = types.SimpleNamespace(
    types=types.SimpleNamespace(PropertyGroup=object, Operator=object, UIList=object),
    props=types.SimpleNamespace(
        StringProperty=lambda **_kwargs: None,
        BoolProperty=lambda **_kwargs: None,
        IntProperty=lambda **_kwargs: None,
        EnumProperty=lambda **_kwargs: None,
        CollectionProperty=lambda **_kwargs: None,
    ),
    data=types.SimpleNamespace(objects={}),
    utils=types.SimpleNamespace(register_class=lambda _cls: None, unregister_class=lambda _cls: None),
)
_install_module("bpy", **_fake_bpy.__dict__)
_install_module(
    f"{PKG}.blueprint.node_postprocess_base",
    SSMTNode_PostProcess_Base=type(
        "_FakePostProcessBase",
        (object,),
        {
            "split_anim_driver_block_content": staticmethod(lambda content: ("", content)),
            "split_auto_appended_tail_content": staticmethod(lambda content: (content, "")),
        },
    ),
)
_install_module(f"{PKG}.blueprint.direct_export", sync_shapekey_direct_mode=lambda *_args, **_kwargs: None)
_install_module(
    f"{PKG}.blueprint.variable_registry",
    allocate_shape_key_variable_name=lambda shape_key_name, **_kwargs: f"Freq_{shape_key_name}",
    mark_variable_name_used=lambda *_args, **_kwargs: None,
    normalize_variable_name=lambda value: str(value or "").strip(),
    cjk_to_ascii=lambda value: str(value or ""),
    is_pinyin_available=lambda **_kwargs: False,
    reset_pinyin_cache=lambda *_args, **_kwargs: None,
    shape_key_base_variable_name=lambda shape_key_name: f"Freq_{shape_key_name}",
    get_referenced_variable_names=lambda *_args, **_kwargs: set(),
)


def _resolve_hash_buffer_candidate(folder_path, hash_val, file_suffix, preferred_hashes=None):
    """按真实语义解析 `{hash}{suffix}`：优先 preferred_hashes，否则目录内唯一匹配。"""
    import glob
    import os

    for candidate in list(preferred_hashes or []) + [hash_val]:
        if not candidate:
            continue
        path = os.path.join(folder_path, f"{candidate}{file_suffix}")
        if os.path.exists(path):
            return path, candidate
    matches = sorted(glob.glob(os.path.join(folder_path, f"*{file_suffix}")))
    if matches:
        stem = os.path.basename(matches[0])[: -len(file_suffix)]
        return matches[0], stem
    return os.path.join(folder_path, f"{hash_val}{file_suffix}"), hash_val


_install_module(
    f"{PKG}.common.mod_path_compat",
    collect_base_position_resource_map=lambda *_args, **_kwargs: {},
    derive_shapekey_base_resource_name=lambda *args, **_kwargs: "",
    derive_shapekey_freq_resource_name=lambda *args, **_kwargs: "",
    derive_shapekey_merged_data_resource_name=lambda *args, **_kwargs: "",
    derive_shapekey_merged_map_resource_name=lambda *args, **_kwargs: "",
    derive_shapekey_slot_map_resource_name=lambda *args, **_kwargs: "",
    derive_shapekey_slot_resource_name=lambda *args, **_kwargs: "",
    derive_shapekey_weight_resource_name=lambda *args, **_kwargs: "ResourceWeightStub",
    ensure_resource_alias_section=lambda *_args, **_kwargs: None,
    resolve_hash_buffer_candidate=_resolve_hash_buffer_candidate,
)
_install_module(
    f"{PKG}.common.object_prefix_helper",
    ObjectPrefixHelper=types.SimpleNamespace(
        resolve_source_object_name=lambda name: name,
        extract_prefix_info=lambda name: None,
        parse_prefix_parts=lambda prefix: {},
        split_name_and_prefix=lambda name, *args: (name, "", name),
    ),
)
_install_module(
    f"{PKG}.utils.log_utils",
    LOG=types.SimpleNamespace(info=lambda *_args, **_kwargs: None, warning=lambda *_args, **_kwargs: None),
)
_install_module(
    f"{PKG}.utils.shapekey_utils",
    ShapeKeyUtils=types.SimpleNamespace(
        is_basis_shape_key_name=lambda name: str(name or "").strip().lower() == "basis",
    ),
)
_install_module(
    f"{PKG}.blueprint.export_helper",
    BlueprintExportHelper=types.SimpleNamespace(
        collect_connected_start_nodes=lambda _tree: [],
        get_current_blueprint_model=lambda: None,
        _resolve_shapekey_object_in_scene=lambda name: None,
    ),
)

module_path = Path(__file__).resolve().parents[1] / "blueprint" / "node_postprocess_shapekey.py"
spec = importlib.util.spec_from_file_location(f"{PKG}.blueprint.node_postprocess_shapekey", module_path)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)


FULL_STRUCT = (
    "struct VertexAttributes {\n"
    "    float3 position;\n"
    "    float3 normal;\n"
    "    float4 tangent;\n"
    "};"
)
POSITION_ONLY_STRUCT = (
    "struct VertexAttributes {\n"
    "    float3 position;\n"
    "    float position_w;\n"
    "};"
)
COMPRESSED_NORMAL_STRUCT = (
    "struct VertexAttributes {\n"
    "    float3 position;\n"
    "    uint _pad0;\n"
    "    float4 tangent;\n"
    "};"
)

DELTA_TEMPLATE = (
    "struct VertexAttributes {\n"
    "    float3 position;\n"
    "    float3 normal;\n"
    "    float4 tangent;\n"
    "};\n"
    "StructuredBuffer<float3> merged_shapekey_pos_deltas : register(t51);\n"
    "StructuredBuffer<int> merged_shapekey_indices : register(t52);\n"
    "Texture1D<float4> IniParams : register(t120);\n"
    "// --- [PYTHON-MANAGED BLOCK START] ---\n"
    "// --- [PYTHON-MANAGED BLOCK END] ---\n"
    "[numthreads(16, 1, 1)]\n"
    "void main(uint3 threadID : SV_DispatchThreadID)\n"
    "{\n"
    "    uint i = threadID.x;\n"
    "    VertexAttributes output = rw_buffer[i];\n"
    "    float3 total_diff_position = float3(0.0, 0.0, 0.0);\n"
    "    float3 total_diff_normal = float3(0.0, 0.0, 0.0);\n"
    "    float3 total_diff_tangent = float3(0.0, 0.0, 0.0);\n"
    "    // --- [PYTHON-MANAGED LOGIC START] ---\n"
    "    // --- [PYTHON-MANAGED LOGIC END] ---\n"
    "    // --- [PYTHON-MANAGED WRITEBACK START] ---\n"
    "    output.position += total_diff_position;\n"
    "    // --- [PYTHON-MANAGED WRITEBACK END] ---\n"
    "    rw_buffer[i] = output;\n"
    "}\n"
)


def _make_node(struct_definition=FULL_STRUCT, all_channels=False):
    node = module.SSMTNode_PostProcess_ShapeKey()
    node.INTENSITY_START_INDEX = 100
    node.VERTEX_RANGE_START_INDEX = 200
    node.store_deltas = True
    node.store_all_vertex_channels = all_channels
    node._get_vertex_struct_definition = lambda **_kwargs: struct_definition
    return node


class DeltaChannelPlanTests(unittest.TestCase):
    def test_disabled_is_position_only(self):
        node = _make_node(all_channels=False)
        self.assertEqual(node._resolve_delta_channel_plan(), [("position", 0, 3)])
        self.assertEqual(node._resolve_delta_stride(), 12)
        self.assertEqual(node._describe_delta_scope(True), "仅位置")
        self.assertEqual(node._describe_delta_scope(False), "否")

    def test_enabled_expands_position_normal_tangent_xyz(self):
        node = _make_node(all_channels=True)
        self.assertEqual(
            node._resolve_delta_channel_plan(),
            [("position", 0, 3), ("normal", 3, 3), ("tangent", 6, 3)],
        )
        self.assertEqual(node._channel_plan_columns(node._resolve_delta_channel_plan()), list(range(9)))
        self.assertEqual(node._resolve_delta_stride(), 36)
        self.assertEqual(node._describe_delta_scope(True), "全部属性")

    def test_enabled_without_normal_tangent_degrades_to_position(self):
        node = _make_node(struct_definition=POSITION_ONLY_STRUCT, all_channels=True)
        self.assertEqual(node._resolve_delta_channel_plan(), [("position", 0, 3)])
        self.assertEqual(node._resolve_delta_stride(), 12)

    def test_enabled_clamps_channels_beyond_actual_stride(self):
        """手填 40B 顶点属性定义遇上工作空间 16B 的 IB：不能越界读。"""
        node = _make_node(all_channels=True)
        self.assertEqual(
            node._resolve_delta_channel_plan(num_floats_per_vertex=4),
            [("position", 0, 3)],
        )
        self.assertEqual(node._resolve_delta_stride(vertex_stride=16), 12)

    def test_compressed_normal_is_not_treated_as_float_channel(self):
        node = _make_node(struct_definition=COMPRESSED_NORMAL_STRUCT, all_channels=True)
        # 4 字节压缩法线是 uint 占位，float3 增量表达不了 → 只保留位置与切线
        self.assertEqual(
            node._resolve_delta_channel_plan(),
            [("position", 0, 3), ("tangent", 4, 3)],
        )


def _render_shader(node, use_delta=True, merge_slot_files=True):
    with tempfile.TemporaryDirectory() as temp_dir:
        shader_path = Path(temp_dir) / "shader.hlsl"
        shader_path.write_text(DELTA_TEMPLATE, encoding="utf-8")
        success = node._update_shader_file(
            str(shader_path),
            hash_slot_data={1: {"Smile": ["ObjA"]}},
            use_packed=True,
            use_delta=use_delta,
            unique_names=["Smile"],
            unique_objects=["ObjA"],
            use_optimized=True,
            merge_slot_files=merge_slot_files,
        )
        assert success
        return shader_path.read_text(encoding="utf-8")


class ShaderWritebackTests(unittest.TestCase):
    def _run(self, all_channels, use_delta=True):
        return _render_shader(_make_node(all_channels=all_channels), use_delta=use_delta)

    def test_disabled_keeps_legacy_position_only_shader(self):
        shader = self._run(all_channels=False)
        self.assertIn("StructuredBuffer<float3> merged_shapekey_pos_deltas : register(t51);", shader)
        self.assertNotIn("ShapeKeyDelta", shader)
        self.assertIn("total_diff_position += merged_shapekey_pos_deltas[packed_index]", shader)
        self.assertNotIn("total_diff_normal +=", shader)
        self.assertNotIn("total_diff_tangent +=", shader)
        self.assertIn("output.position += total_diff_position;", shader)
        self.assertNotIn("output.normal +=", shader)
        self.assertNotIn("output.tangent.xyz +=", shader)

    def test_enabled_swaps_buffer_struct_and_writes_all_channels(self):
        shader = self._run(all_channels=True)
        self.assertIn("struct ShapeKeyDelta {", shader)
        self.assertIn("    float3 position;", shader)
        self.assertIn("    float3 normal;", shader)
        self.assertIn("    float3 tangent;", shader)
        self.assertIn("StructuredBuffer<ShapeKeyDelta> merged_shapekey_pos_deltas : register(t51);", shader)
        self.assertNotIn("StructuredBuffer<float3> merged_shapekey_pos_deltas", shader)
        self.assertIn("ShapeKeyDelta sk_delta_slot0 = merged_shapekey_pos_deltas[packed_index];", shader)
        self.assertIn("total_diff_position += sk_delta_slot0.position * anim_weight_slot0;", shader)
        self.assertIn("total_diff_normal += sk_delta_slot0.normal * anim_weight_slot0;", shader)
        self.assertIn("total_diff_tangent += sk_delta_slot0.tangent * anim_weight_slot0;", shader)
        self.assertIn("output.position += total_diff_position;", shader)
        self.assertIn("output.normal += total_diff_normal;", shader)
        self.assertIn("output.tangent.xyz += total_diff_tangent;", shader)

    def test_disabled_non_delta_writeback_is_position_only(self):
        shader = self._run(all_channels=False, use_delta=False)
        self.assertIn("output.position += total_diff_position;", shader)
        self.assertNotIn("output.normal +=", shader)
        self.assertNotIn("output.tangent.xyz +=", shader)

    def test_enabled_non_delta_writes_all_channels(self):
        shader = self._run(all_channels=True, use_delta=False)
        self.assertIn("output.normal += total_diff_normal;", shader)
        self.assertIn("output.tangent.xyz += total_diff_tangent;", shader)
        self.assertIn("total_diff_normal += (merged_shapekeys[packed_index].normal - base[i].normal)", shader)
        self.assertIn(
            "total_diff_tangent += (merged_shapekeys[packed_index].tangent.xyz - base[i].tangent.xyz)",
            shader,
        )


class MergedDeltaDataTests(unittest.TestCase):
    HASH = "abc12345"

    def _write_buffers(self, root, vertices=2):
        meshes = Path(root) / "Meshes0000"
        meshes.mkdir(parents=True, exist_ok=True)
        base = np.zeros((vertices, 10), dtype=np.float32)
        base[:, 0:3] = (np.arange(vertices * 3, dtype=np.float32).reshape(vertices, 3) * 0.1)
        base[:, 3:6] = (0.0, 0.0, 1.0)
        base[:, 6:10] = (1.0, 0.0, 0.0, 1.0)
        (meshes / f"{self.HASH}-Position.buf").write_bytes(base.tobytes())

        slot_dir = Path(root) / "Meshes1001"
        slot_dir.mkdir(parents=True, exist_ok=True)
        target = base.copy()
        target[:, 0] = base[:, 0] + 0.5      # 位置 x 增量
        target[:, 3] = base[:, 3] + 0.25     # 法线 x 增量
        target[:, 6] = base[:, 6] - 0.125    # 切线 x 增量
        (slot_dir / f"{self.HASH}-Position.buf").write_bytes(target.tobytes())

    def _run(self, all_channels):
        node = _make_node(all_channels=all_channels)
        with tempfile.TemporaryDirectory() as temp_dir:
            self._write_buffers(temp_dir)
            success, _map = node._process_merged_shapekey_Meshess(
                temp_dir,
                {1: {"Smile": [f"{self.HASH}.Obj"]}},
                {},
            )
            self.assertTrue(success)
            data_path = Path(temp_dir) / "Meshes0000" / f"{self.HASH}-Position_merged_packed_pos_delta.buf"
            return np.frombuffer(data_path.read_bytes(), dtype=np.float32)

    def test_disabled_writes_three_floats_per_entry(self):
        data = self._run(all_channels=False)
        self.assertEqual(data.size, 2 * 3)
        np.testing.assert_allclose(data.reshape(2, 3), [[0.5, 0.0, 0.0], [0.5, 0.0, 0.0]])

    def test_enabled_writes_all_channel_floats_per_entry(self):
        data = self._run(all_channels=True)
        self.assertEqual(data.size, 2 * 9)
        expected = np.array([0.5, 0.0, 0.0, 0.25, 0.0, 0.0, -0.125, 0.0, 0.0], dtype=np.float32)
        np.testing.assert_allclose(data.reshape(2, 9), [expected, expected])

    def _write_buffers_generic(self, root, vertices=2):
        """泛化布局：position/normal/tangent + float4 color + float2 uv1 + uint packed_extra。"""
        meshes = Path(root) / "Meshes0000"
        meshes.mkdir(parents=True, exist_ok=True)
        base = np.zeros((vertices, 17), dtype=np.float32)
        base[:, 0:3] = (np.arange(vertices * 3, dtype=np.float32).reshape(vertices, 3) * 0.1)
        base[:, 3:6] = (0.0, 0.0, 1.0)
        base[:, 6:10] = (1.0, 0.0, 0.0, 1.0)
        base[:, 10:14] = (0.1, 0.2, 0.3, 0.4)
        base[:, 14:16] = (0.5, 0.6)
        base[:, 16] = 1.0  # uint 列（按位复用，不参与增量）
        (meshes / f"{self.HASH}-Position.buf").write_bytes(base.tobytes())

        slot_dir = Path(root) / "Meshes1001"
        slot_dir.mkdir(parents=True, exist_ok=True)
        target = base.copy()
        target[:, 0] = base[:, 0] + 0.5            # 位置 x
        target[:, 3] = base[:, 3] + 0.25           # 法线 x
        target[:, 6] = base[:, 6] - 0.125          # 切线 x
        target[:, 10:14] = base[:, 10:14] + 0.5    # 顶点色 4 个分量全变
        target[:, 14:16] = base[:, 14:16] + 0.25   # uv1 两个分量全变
        target[:, 16] = 2.0                        # uint 列也变了：必须不出现在增量记录里
        (slot_dir / f"{self.HASH}-Position.buf").write_bytes(target.tobytes())

    def _run_generic(self):
        node = _make_node(struct_definition=GENERIC_STRUCT, all_channels=True)
        with tempfile.TemporaryDirectory() as temp_dir:
            self._write_buffers_generic(temp_dir)
            success, _map = node._process_merged_shapekey_Meshess(
                temp_dir,
                {1: {"Smile": [f"{self.HASH}.Obj"]}},
                {},
            )
            self.assertTrue(success)
            data_path = Path(temp_dir) / "Meshes0000" / f"{self.HASH}-Position_merged_packed_pos_delta.buf"
            return np.frombuffer(data_path.read_bytes(), dtype=np.float32), node

    def test_generic_layout_writes_every_float_channel_and_skips_non_float(self):
        data, node = self._run_generic()
        self.assertEqual(data.size, 2 * 15)
        expected = np.array(
            [0.5, 0.0, 0.0, 0.25, 0.0, 0.0, -0.125, 0.0, 0.0, 0.5, 0.5, 0.5, 0.5, 0.25, 0.25],
            dtype=np.float32,
        )
        np.testing.assert_allclose(data.reshape(2, 15), [expected, expected])
        # 记录每条的 float 数 × 4 必须等于 INI 里声明的 stride
        self.assertEqual(4 * 15, node._resolve_delta_stride())


class ChannelContractConsistencyTests(unittest.TestCase):
    """数据记录宽度 / 着色器结构体行宽 / INI stride 三者必须一致。

    三者不一致是这套机制最容易踩的坑：着色器按结构体行宽索引，INI 按 stride 建视图，
    数据按通道列写出——任何一处漂移都会让顶点读位错乱。
    """

    def test_all_three_widths_agree_when_enabled(self):
        node = _make_node(all_channels=True)
        stride = node._resolve_delta_stride(vertex_stride=40)
        self.assertEqual(stride, 36)

        shader = _render_shader(node)
        struct_body = shader.split("struct ShapeKeyDelta {", 1)[1].split("};", 1)[0]
        self.assertEqual(struct_body.count("float3"), 3)
        # 每个 float3 字段 12 字节，结构体行宽必须等于 INI 里声明的 stride
        self.assertEqual(12 * struct_body.count("float3"), stride)

        plan = node._resolve_delta_channel_plan(num_floats_per_vertex=10)
        self.assertEqual(4 * node._channel_plan_float_count(plan), stride)

    def test_all_three_widths_agree_when_disabled(self):
        node = _make_node(all_channels=False)
        stride = node._resolve_delta_stride(vertex_stride=40)
        self.assertEqual(stride, 12)
        self.assertNotIn("ShapeKeyDelta", _render_shader(node))
        plan = node._resolve_delta_channel_plan(num_floats_per_vertex=10)
        self.assertEqual(4 * node._channel_plan_float_count(plan), stride)


GENERIC_STRUCT = (
    "struct VertexAttributes {\n"
    "    float3 position;\n"
    "    float3 normal;\n"
    "    float4 tangent;\n"
    "    float4 color;\n"
    "    float2 uv1;\n"
    "    uint packed_extra;\n"
    "};"
)
PADDED_STRUCT = (
    "struct VertexAttributes {\n"
    "    float3 position;\n"
    "    float position_w;\n"
    "};"
)


class GenericAttributeChannelPlanTests(unittest.TestCase):
    """「存储全部顶点属性增量」按真实顶点属性定义展开：凡 float 属性都进计划。"""

    def test_every_float_attribute_enters_the_plan(self):
        node = _make_node(struct_definition=GENERIC_STRUCT, all_channels=True)
        self.assertEqual(
            node._resolve_delta_channel_plan(),
            [
                ("position", 0, 3),
                ("normal", 3, 3),
                ("tangent", 6, 3),
                ("color", 10, 4),
                ("uv1", 14, 2),
            ],
        )
        self.assertEqual(node._resolve_delta_stride(), (3 + 3 + 3 + 4 + 2) * 4)

    def test_uint_attribute_and_layout_constant_are_bit_copied(self):
        node = _make_node(struct_definition=GENERIC_STRUCT, all_channels=True)
        self.assertEqual(node._resolve_skipped_delta_channels(), ["packed_extra"])

        padded = _make_node(struct_definition=PADDED_STRUCT, all_channels=True)
        self.assertEqual(padded._resolve_delta_channel_plan(), [("position", 0, 3)])
        self.assertEqual(padded._resolve_skipped_delta_channels(), ["position_w"])

    def test_shader_covers_generic_channels(self):
        shader = _render_shader(_make_node(struct_definition=GENERIC_STRUCT, all_channels=True))
        self.assertIn("    float4 color;", shader)
        self.assertIn("    float2 uv1;", shader)
        self.assertIn("float4 total_diff_color = float4(0.0, 0.0, 0.0, 0.0);", shader)
        self.assertIn("float2 total_diff_uv1 = float2(0.0, 0.0);", shader)
        self.assertIn("total_diff_color += sk_delta_slot0.color * anim_weight_slot0;", shader)
        self.assertIn("output.color += total_diff_color;", shader)
        self.assertIn("output.uv1 += total_diff_uv1;", shader)

    def test_struct_width_matches_stride_for_generic_layout(self):
        """结构体行宽 == INI stride == 数据列宽，泛化布局下也必须相等。"""
        node = _make_node(struct_definition=GENERIC_STRUCT, all_channels=True)
        shader = _render_shader(node)
        delta_struct = shader.split("struct ShapeKeyDelta {", 1)[1].split("};", 1)[0]
        total_floats = 0
        for line in delta_struct.strip().splitlines():
            type_text = line.strip().split()[0]
            total_floats += 1 if type_text == "float" else int(type_text.replace("float", ""))
        self.assertEqual(4 * total_floats, node._resolve_delta_stride())

    def test_known_three_channels_keep_historical_width(self):
        shader = _render_shader(_make_node(struct_definition=GENERIC_STRUCT, all_channels=True))
        delta_struct = shader.split("struct ShapeKeyDelta {", 1)[1].split("};", 1)[0]
        self.assertIn("    float3 position;", delta_struct)
        self.assertIn("    float3 normal;", delta_struct)
        self.assertIn("    float3 tangent;", delta_struct)
        self.assertNotIn("float4 tangent;", delta_struct)
        self.assertIn("output.tangent.xyz += total_diff_tangent;", shader)


_shared_path = Path(__file__).resolve().parents[1] / "blueprint" / "direct_export_shapekey_shared.py"
_shared_spec = importlib.util.spec_from_file_location(
    f"{PKG}.blueprint.direct_export_shapekey_shared", _shared_path
)
_shared_module = importlib.util.module_from_spec(_shared_spec)
sys.modules[_shared_spec.name] = _shared_module
_shared_spec.loader.exec_module(_shared_module)


class StorageModeSwitchTests(unittest.TestCase):
    """三态存储模式（「存储顶点增量」与「储存全部顶点属性增量」互斥）。

    只勾前者 = 仅位置增量；只勾后者 = 全通道增量；两个都不勾 = 绝对坐标。
    三态都必须由 ``effective_use_delta`` 统一表达，否则只勾后者时会被当成绝对坐标。
    """

    def _state_node(self, store_deltas, all_channels):
        node = module.SSMTNode_PostProcess_ShapeKey()
        node.store_deltas = store_deltas
        node.store_all_vertex_channels = all_channels
        node.use_packed_Meshess = True
        node.use_optimized_lookup = True
        node.merge_slot_files = False
        node._get_vertex_struct_definition = lambda **_kwargs: FULL_STRUCT
        return node

    def test_neither_checked_means_absolute_coordinates(self):
        self.assertFalse(self._state_node(False, False).effective_use_delta())

    def test_position_delta_alone_stays_position_only(self):
        node = self._state_node(True, False)
        self.assertTrue(node.effective_use_delta())
        self.assertEqual(node._resolve_delta_channel_plan(), [("position", 0, 3)])
        self.assertEqual(node._resolve_delta_stride(), 12)

    def test_all_channels_alone_still_stores_deltas(self):
        node = self._state_node(False, True)
        self.assertTrue(node.effective_use_delta())
        self.assertEqual(
            node._resolve_delta_channel_plan(),
            [("position", 0, 3), ("normal", 3, 3), ("tangent", 6, 3)],
        )
        self.assertEqual(node._resolve_delta_stride(), 36)

    def test_template_follows_derived_mode(self):
        self.assertEqual(
            self._state_node(False, True)._get_shader_template_name(),
            "shapekey_anim_packed_delta_v4_optimized.hlsl",
        )
        self.assertEqual(
            self._state_node(False, False)._get_shader_template_name(),
            "shapekey_anim_packed.hlsl",
        )

    def test_checking_all_channels_clears_position_delta(self):
        node = self._state_node(True, False)
        node.store_all_vertex_channels = True
        module.sync_shapekey_all_channels_mode(node, None)
        self.assertTrue(node.store_all_vertex_channels)
        self.assertFalse(node.store_deltas)
        self.assertTrue(node.effective_use_delta())

    def test_checking_position_delta_clears_all_channels(self):
        node = self._state_node(False, True)
        node.store_deltas = True
        module.sync_shapekey_delta_mode(node, None)
        self.assertTrue(node.store_deltas)
        self.assertFalse(node.store_all_vertex_channels)

    def test_unchecking_leaves_the_other_switch_alone(self):
        node = self._state_node(False, False)
        module.sync_shapekey_delta_mode(node, None)
        module.sync_shapekey_all_channels_mode(node, None)
        self.assertFalse(node.store_deltas)
        self.assertFalse(node.store_all_vertex_channels)


class UseDeltaResolverTests(unittest.TestCase):
    """共享 mixin 必须走三态口径；NTMI 适配器（无该方法）保持自身行为。"""

    def test_real_node_uses_three_state(self):
        node = module.SSMTNode_PostProcess_ShapeKey()
        node.store_deltas = False
        node.store_all_vertex_channels = True
        self.assertTrue(_shared_module.resolve_use_delta(node))

    def test_adapter_without_effective_use_delta_keeps_its_own_flag(self):
        adapter = types.SimpleNamespace(store_deltas=True)
        self.assertTrue(_shared_module.resolve_use_delta(adapter))
        adapter.store_deltas = False
        self.assertFalse(_shared_module.resolve_use_delta(adapter))


WORKSPACE_ROOT = Path(r"K:\SSMT-Package-master\WorkSpace")


def _find_real_position_layout():
    """在真实工作空间里找一个 Position 类别布局（元素 + 行宽）；找不到返回 None。

    目录名形如 ``TYPE_GPU_P12_N4_T8_T1-8_C4_BW8_BI4_``，json 里 CategoryBufferList 的
    Position 类别元素给出行内真实字节宽（例如压缩法线 R32_UINT = 4 字节）。
    """
    if not WORKSPACE_ROOT.is_dir():
        return None
    for json_path in sorted(WORKSPACE_ROOT.glob("*/*/LOD*/*/TYPE_*/*.json")):
        try:
            data = json.loads(json_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        for buffer in data.get("CategoryBufferList") or []:
            elements = [
                element
                for element in (buffer.get("D3D11ElementList") or [])
                if str(element.get("Category", "")).upper() == "POSITION"
            ]
            stride = sum(int(element.get("ByteWidth", 0) or 0) for element in elements)
            if elements and stride > 0:
                return {
                    "path": str(json_path),
                    "hash": json_path.stem,
                    "stride": stride,
                    "elements": elements,
                }
    return None


class RealWorkspaceLayoutTests(unittest.TestCase):
    """真实工作空间布局 → 合成结构体 → 增量计划：行宽一致、非 float 属性按位复用。"""

    def setUp(self):
        self.layout = _find_real_position_layout()
        if self.layout is None:
            self.skipTest("本机没有可用的真实工作空间布局数据")

    def _node_with_real_layout(self):
        node = module.SSMTNode_PostProcess_ShapeKey()
        node.store_deltas = False
        node.store_all_vertex_channels = True
        node.use_packed_Meshess = True
        node.use_optimized_lookup = True
        node.merge_slot_files = False
        # 真实行宽直接喂给合成入口：本测试要验的是「布局 → 结构体 → 计划」，
        # 工作空间文件的读取链路是既有代码，不在本轮范围内。
        node._get_workspace_position_stride = lambda _hash: int(self.layout["stride"])
        module._resolve_workspace_category_elements = lambda _hash, _category: [
            types.SimpleNamespace(**element) for element in self.layout["elements"]
        ]
        module._resolve_workspace_category_stride = (
            lambda _unique_str, _category: int(self.layout["stride"])
        )
        module._resolve_workspace_game_type_by_prefix = lambda _prefix: types.SimpleNamespace(
            D3D11ElementList=[
                types.SimpleNamespace(**element) for element in self.layout["elements"]
            ]
        )
        return node

    def test_synthesized_struct_matches_real_row_width(self):
        node = self._node_with_real_layout()
        struct_definition = node._get_vertex_struct_definition(self.layout["hash"])
        self.assertIsNotNone(struct_definition, self.layout["path"])
        parsed = node.parse_vertex_struct(struct_definition)
        self.assertIsNotNone(parsed, struct_definition)
        self.assertEqual(parsed[0], self.layout["stride"])
        self.assertEqual(node._get_workspace_position_stride(self.layout["hash"]), self.layout["stride"])

    def test_real_layout_channels_follow_declared_types(self):
        node = self._node_with_real_layout()
        widths = {
            str(element.get("SemanticName", "")).upper(): int(element.get("ByteWidth", 0) or 0)
            for element in self.layout["elements"]
        }
        plan_names = node._channel_plan_names(
            node._resolve_delta_channel_plan(hash_val=self.layout["hash"])
        )
        skipped = node._resolve_skipped_delta_channels(hash_val=self.layout["hash"])

        self.assertEqual(plan_names[0], "position")
        if widths.get("NORMAL") == 12 and widths.get("TANGENT") == 16:
            # 全 float 布局：法线/切线都该进计划
            self.assertIn("normal", plan_names)
            self.assertIn("tangent", plan_names)
        else:
            # 压缩布局（如 R32_UINT 法线 / 8 字节切线）：float 加减法表达不了 → 按位复用并点名
            self.assertEqual(plan_names, ["position"], self.layout["path"])
            self.assertTrue(skipped, f"压缩属性必须出现在按位复用名单: {self.layout['path']}")


class RealTemplateInjectionTests(unittest.TestCase):
    """真实 Toolset 模板必须能被注入：结构体替换、缓冲声明、累加器齐全。

    合成模板只能证明注入逻辑自洽；这里直接拿仓库里真正下发的模板过一遍，防模板漂移
    （模板改了缓冲名/丢了标记块，合成模板测试是发现不了的）。
    """

    TEMPLATES = (
        "shapekey_anim_packed_delta_v4_optimized.hlsl",
        "shapekey_anim_packed_delta_v5_merged.hlsl",
    )

    def _render(self, node, template_name, use_delta=True):
        template_path = Path(__file__).resolve().parents[1] / "Toolset" / template_name
        if not template_path.is_file():
            self.skipTest(f"模板不存在: {template_name}")
        with tempfile.TemporaryDirectory() as temp_dir:
            shader_path = Path(temp_dir) / template_name
            shader_path.write_text(template_path.read_text(encoding="utf-8"), encoding="utf-8")
            success = node._update_shader_file(
                str(shader_path),
                hash_slot_data={1: {"Smile": ["ObjA"]}},
                use_packed=True,
                use_delta=use_delta,
                unique_names=["Smile"],
                unique_objects=["ObjA"],
                use_optimized=True,
                merge_slot_files=("merged" in template_name),
            )
            self.assertTrue(success)
            return shader_path.read_text(encoding="utf-8")

    def test_real_templates_accept_generic_channel_injection(self):
        node = _make_node(struct_definition=GENERIC_STRUCT, all_channels=True)
        for template_name in self.TEMPLATES:
            with self.subTest(template=template_name):
                shader = self._render(node, template_name)
                self.assertIn("struct ShapeKeyDelta {", shader)
                self.assertIn("StructuredBuffer<ShapeKeyDelta>", shader)
                self.assertNotIn("StructuredBuffer<float3> merged_shapekey_pos_deltas", shader)

                delta_struct = shader.split("struct ShapeKeyDelta {", 1)[1].split("};", 1)[0]
                for channel in ("position", "normal", "tangent", "color", "uv1"):
                    self.assertIn(f" {channel};", delta_struct)

                # 每个被引用的累加器都必须有声明（模板自带三个 + 新通道由节点注入）
                declared = set(re.findall(r"float\d?\s+total_diff_(\w+)\s*=", shader))
                used = set(re.findall(r"total_diff_(\w+)\s*\+=", shader))
                self.assertTrue(used, "模板里应当引用了累加器")
                self.assertEqual(used - declared, set())

    def test_real_templates_keep_legacy_position_only_when_disabled(self):
        node = _make_node(struct_definition=GENERIC_STRUCT, all_channels=False)
        for template_name in self.TEMPLATES:
            with self.subTest(template=template_name):
                shader = self._render(node, template_name)
                self.assertNotIn("ShapeKeyDelta", shader)
                self.assertIn("StructuredBuffer<float3>", shader)
                self.assertIn("output.position += total_diff_position;", shader)
                self.assertNotIn("output.color +=", shader)


class NtmiWidthCouplingTests(unittest.TestCase):
    """NTMI 增量目前只有位置：着色器行宽 / INI stride / 落盘记录宽度必须同时是 12 字节。

    这三处是字节对齐耦合的——只改一处不会报错，只会在游戏里静默错位。本测试把它们钉在
    一起：这是「NTMI 对齐到通用状态」那批改动开工前的第一道安全网（真要开全通道，
    必须三处同改并同步放宽这里的断言）。
    """

    def _source(self) -> str:
        return (
            Path(__file__).resolve().parents[1] / "blueprint" / "ntmi_shapekey.py"
        ).read_text(encoding="utf-8")

    def test_shader_stays_position_triplet_only(self):
        source = self._source()
        self.assertIn("StructuredBuffer<float3> merged_shapekey_pos_deltas : register(t51);", source)
        self.assertIn("RWBuffer<float> OutPosition : register(u5);", source)
        self.assertIn("uint vertex_count = position_float_count / 3u;", source)
        self.assertNotIn("ShapeKeyDelta", source)

    def test_ini_stride_uses_the_same_triplet_width(self):
        source = self._source()
        self.assertEqual(
            source.count("stride = {12 if use_delta"),
            2,
            "合并版与逐槽版必须共用同一个宽度常量",
        )
        self.assertNotIn("stride = 36", source)
        self.assertNotIn("stride = 4 *", source)

    def test_data_side_records_follow_the_same_width(self):
        node = _make_node(struct_definition=GENERIC_STRUCT, all_channels=True)
        # 模拟 NTMI 适配器：读不到「储存全部顶点属性增量」→ 计划退化为仅位置
        del node.store_all_vertex_channels
        self.assertEqual(node._resolve_delta_channel_plan(), [("position", 0, 3)])
        self.assertEqual(node._resolve_delta_stride(), 12)
        self.assertEqual(node._channel_plan_float_count(node._resolve_delta_channel_plan()) * 4, 12)


class NtmiAllChannelsGapTests(unittest.TestCase):
    """NTMI 路线只算位置；开启全属性增量时必须如实告警，而不是静默无效。"""

    def test_ntmi_entry_warns_when_all_channels_requested(self):
        source = (
            Path(__file__).resolve().parents[1] / "blueprint" / "ntmi_shapekey.py"
        ).read_text(encoding="utf-8")
        self.assertIn('getattr(node, "store_all_vertex_channels", False)', source)
        self.assertIn("NTMI 导出路线暂不支持", source)
        # 告警必须发生在生成器实例化之前（否则产物已经写完才提示）
        self.assertLess(
            source.index("NTMI 导出路线暂不支持"),
            source.index("generator = NTMIDirectShapeKeyGenerator("),
        )


class StorageGroupLayoutTests(unittest.TestCase):
    def _source(self) -> str:
        return (
            Path(__file__).resolve().parents[1] / "blueprint" / "node_postprocess_shapekey.py"
        ).read_text(encoding="utf-8")

    def test_draw_buttons_puts_switches_into_three_groups(self):
        source = self._source()
        for label in ("计算优化", "空间优化", "导出优化"):
            self.assertIn(f'label(text="{label}"', source)

    def test_storage_switches_are_wired_to_exclusive_callbacks(self):
        source = self._source()
        self.assertIn("update=sync_shapekey_delta_mode", source)
        self.assertIn("update=sync_shapekey_all_channels_mode", source)

    def test_bake_switch_exists_and_defaults_to_off(self):
        source = self._source()
        self.assertIn("bake_disabled_shape_keys: bpy.props.BoolProperty(", source)
        self.assertIn('name="烘焙未勾选的形态键"', source)
        block = source.split("bake_disabled_shape_keys: bpy.props.BoolProperty(", 1)[1][:700]
        self.assertIn("default=False", block)


class DisabledShapeKeyBakeWiringTests(unittest.TestCase):
    """未勾选形态键烘焙的前处理接线（几何行为由 tests/blender_smoke_shapekey_disabled_bake.py 实机覆盖）。"""

    def _read(self, relative: str) -> str:
        return (Path(__file__).resolve().parents[1] / relative).read_text(encoding="utf-8")

    def test_preprocess_bakes_before_capturing_direct_positions(self):
        source = self._read("blueprint/preprocess.py")
        self.assertIn("def _bake_disabled_shape_keys(cls, object_names", source)
        bake_call = source.index("cls._bake_disabled_shape_keys(copy_names)")
        capture_call = source.index("cls._capture_direct_shape_key_positions(copy_names)")
        self.assertLess(bake_call, capture_call, "烘焙必须在直出采样之前：采样取到的基态才是新基态")

    def test_bake_is_gated_by_node_switch(self):
        source = self._read("blueprint/preprocess.py")
        self.assertIn("BlueprintExportHelper.should_bake_disabled_shape_keys()", source)
        helper = self._read("blueprint/export_helper.py")
        self.assertIn("def should_bake_disabled_shape_keys()", helper)
        self.assertIn('getattr(node, "bake_disabled_shape_keys", False)', helper)

    def test_cache_hash_covers_switch_and_disabled_names(self):
        source = self._read("blueprint/preprocess_cache.py")
        self.assertIn("def _shapekey_bake_signature(cls) -> str:", source)
        self.assertIn("hasher.update(cls._shapekey_bake_signature().encode('utf-8'))", source)


if __name__ == "__main__":
    unittest.main()
