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


if __name__ == "__main__":
    unittest.main()
