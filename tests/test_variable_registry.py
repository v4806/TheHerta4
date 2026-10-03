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


PKG = "_variable_registry_test_pkg"
for package_name in (PKG, f"{PKG}.blueprint"):
    package = _install_module(package_name)
    package.__path__ = []


class _FakeNodeGroups(list):
    pass


class _FakeGlobalProperties(dict):
    def __getattr__(self, name):
        if name in self:
            return self[name]
        raise AttributeError(name)

    def __setattr__(self, name, value):
        self[name] = value


_fake_global_properties = _FakeGlobalProperties()
_fake_bpy = types.SimpleNamespace(
    context=types.SimpleNamespace(
        scene=types.SimpleNamespace(global_properties=_fake_global_properties),
    ),
    data=types.SimpleNamespace(node_groups=_FakeNodeGroups()),
)
_install_module("bpy", **_fake_bpy.__dict__)


module_path = Path(__file__).resolve().parents[1] / "blueprint" / "variable_registry.py"
spec = importlib.util.spec_from_file_location(f"{PKG}.blueprint.variable_registry", module_path)
variable_registry = importlib.util.module_from_spec(spec)
sys.modules[f"{PKG}.blueprint.variable_registry"] = variable_registry
spec.loader.exec_module(variable_registry)


def _make_swap_node(var_name: str, *, name: str = "Swap"):
    return types.SimpleNamespace(
        bl_idname="SSMTNode_ObjectSwap",
        name=name,
        custom_var_name=var_name,
        assigned_variable_name=var_name,
    )


def _make_shapekey_item(var_name: str):
    return types.SimpleNamespace(
        custom_variable_name=var_name,
        assigned_variable_name=var_name,
    )


def _make_shapekey_node(*var_names: str):
    return types.SimpleNamespace(
        bl_idname="SSMTNode_PostProcess_ShapeKey",
        shapekey_variable_items=[_make_shapekey_item(name) for name in var_names],
    )


def _make_anim_driver_node(assigned_name: str = "", custom_name: str = ""):
    return types.SimpleNamespace(
        bl_idname="SSMTNode_AnimDriver_ForwardPlay",
        custom_paused_var="",
        driven_variable="",
        assigned_continuous_index_variable_name=assigned_name,
        custom_continuous_index_variable_name=custom_name,
    )


def _make_anim_driver_runtime_node(paused_name: str = "", driven_name: str = ""):
    return types.SimpleNamespace(
        bl_idname="SSMTNode_AnimDriver_ShapeKeySequence",
        custom_paused_var=paused_name,
        driven_variable=driven_name,
        driven_variable_list=[],
        assigned_continuous_index_variable_name="",
        custom_continuous_index_variable_name="",
    )


def _make_anim_driver_list_item(variable_name: str):
    return types.SimpleNamespace(variable_name=variable_name)


def _make_tree(*nodes):
    return types.SimpleNamespace(
        bl_idname="SSMTBlueprintTreeType",
        nodes=list(nodes),
    )


def _make_shapekey_item_node(*pairs):
    """形态键节点：pairs = (shape_key_name, variable_name)。"""
    return types.SimpleNamespace(
        bl_idname="SSMTNode_PostProcess_ShapeKey",
        shapekey_variable_items=[
            types.SimpleNamespace(
                shape_key_name=name,
                assigned_variable_name=var,
                custom_variable_name=var,
            )
            for name, var in pairs
        ],
    )


def _make_forward_play_node(*driven_names, paused_name=""):
    return types.SimpleNamespace(
        bl_idname="SSMTNode_AnimDriver_ForwardPlay",
        custom_paused_var=paused_name,
        driven_variable="",
        driven_variable_list=[_make_anim_driver_list_item(n) for n in driven_names],
        assigned_continuous_index_variable_name="",
        custom_continuous_index_variable_name="",
    )


class VariableRegistryTests(unittest.TestCase):
    def setUp(self):
        _fake_global_properties.clear()
        _fake_bpy.data.node_groups[:] = []
        variable_registry.bpy = _fake_bpy

    def test_object_swap_reuses_first_free_index(self):
        _fake_bpy.data.node_groups[:] = [
            _make_tree(
                _make_swap_node("swapkey0"),
                _make_swap_node("swapkey2"),
            )
        ]
        new_node = types.SimpleNamespace(
            bl_idname="SSMTNode_ObjectSwap",
            custom_var_name="",
            assigned_variable_name="",
        )

        allocated = variable_registry.ensure_object_swap_variable_name(new_node)

        self.assertEqual(allocated, "swapkey1")
        self.assertEqual(new_node.assigned_variable_name, "swapkey1")
        self.assertEqual(_fake_global_properties.object_swap_variable_counter, 3)

    def test_shapekey_variable_reuses_first_free_suffix(self):
        _fake_bpy.data.node_groups[:] = [
            _make_tree(
                _make_shapekey_node("Freq_Smile", "Freq_Smile_2"),
            )
        ]

        allocated = variable_registry.allocate_shape_key_variable_name("Smile")

        self.assertEqual(allocated, "Freq_Smile_1")

    def test_shapekey_owned_name_is_not_treated_as_conflict(self):
        _fake_bpy.data.node_groups[:] = [
            _make_tree(
                _make_shapekey_node("Freq_Blink"),
            )
        ]

        allocated = variable_registry.allocate_shape_key_variable_name(
            "Blink",
            preferred="Freq_Blink",
            owned_names=("Freq_Blink", "Freq_Blink"),
        )

        self.assertEqual(allocated, "Freq_Blink")

    def test_continuous_anim_driver_variable_reuses_first_free_suffix(self):
        _fake_bpy.data.node_groups[:] = [
            _make_tree(
                _make_anim_driver_node("continuous_shapekey_frame1"),
                _make_anim_driver_node("continuous_shapekey_frame3"),
            )
        ]

        allocated = variable_registry.allocate_continuous_shapekey_index_variable_name()

        self.assertEqual(allocated, "continuous_shapekey_frame2")

    def test_used_variable_name_counts_include_anim_driver_paused_and_sequence_variables(self):
        _fake_bpy.data.node_groups[:] = [
            _make_tree(
                _make_anim_driver_runtime_node("$animation_paused1", "$shapekey_seq2"),
            )
        ]

        used_names = variable_registry.get_used_variable_names()

        self.assertIn("animation_paused1", used_names)
        self.assertIn("shapekey_seq2", used_names)

    def test_used_variable_name_counts_include_anim_driver_driven_variable_list(self):
        _fake_bpy.data.node_groups[:] = [
            _make_tree(
                types.SimpleNamespace(
                    bl_idname="SSMTNode_AnimDriver_ForwardPlay",
                    custom_paused_var="",
                    driven_variable="",
                    driven_variable_list=[
                        _make_anim_driver_list_item("$driven_a"),
                        _make_anim_driver_list_item("driven_b"),
                    ],
                    assigned_continuous_index_variable_name="",
                    custom_continuous_index_variable_name="",
                )
            )
        ]

        used_names = variable_registry.get_used_variable_names()

        self.assertIn("driven_a", used_names)
        self.assertIn("driven_b", used_names)

    # ------------------------------------------------------------------
    # 「引用 vs 所有权」：预分配 _1 后缀 bug 的回归
    # ------------------------------------------------------------------

    def test_shapekey_allocation_ignores_anim_driver_reference_names(self):
        """驱动节点的目标变量是**引用**，不是 owner：不得把形态键挤到 _1。

        回归：Freq_xxx 被 driven_variable_list 引用后，预分配退到 Freq_xxx_1，
        与驱动保存的基名永久错开（自锁：重算理想名恒返回 _1）→ 导出后驱动写一个
        没人声明的变量、形态键着色器读另一个变量，联动整条静默死掉。
        """
        _fake_bpy.data.node_groups[:] = [
            _make_tree(
                _make_forward_play_node("$Freq_Smile"),
                _make_shapekey_item_node(),  # 形态键侧尚无预分配
            )
        ]

        allocated = variable_registry.allocate_shape_key_variable_name("Smile")

        self.assertEqual(allocated, "Freq_Smile")

    def test_shapekey_allocation_still_dedups_real_owner_conflicts(self):
        """真正的 owner 重名仍必须去重（引用修复不能把去重一起干掉）。"""
        _fake_bpy.data.node_groups[:] = [
            _make_tree(_make_shapekey_item_node(("Smile", "Freq_Smile")))
        ]

        allocated = variable_registry.allocate_shape_key_variable_name("Smile")

        self.assertEqual(allocated, "Freq_Smile_1")

    def test_owner_counts_exclude_references_but_used_counts_keep_them(self):
        _fake_bpy.data.node_groups[:] = [
            _make_tree(_make_forward_play_node("$Freq_Smile", paused_name="$animation_paused1"))
        ]

        owners = variable_registry._collect_owner_variable_name_counts()
        used = variable_registry._collect_used_variable_name_counts()

        self.assertIn("animation_paused1", owners)
        self.assertNotIn("Freq_Smile", owners)
        # 完整计数（缓存/物体切换分配用）仍保留引用名
        self.assertIn("Freq_Smile", used)

    def test_reference_alias_map_resolves_forked_shapekey_name(self):
        _fake_bpy.data.node_groups[:] = [
            _make_tree(_make_shapekey_item_node(("Smile", "Freq_Smile_1")))
        ]

        alias = variable_registry.build_shape_key_reference_alias_map()

        self.assertEqual(alias, {"Freq_Smile": "Freq_Smile_1"})
        text = (
            "[Present]\n"
            "if $Freq_Smile != $ssmtdrag_ckprev_A_Freq_Smile\n"
            "\t$Freq_Smile = $ssmtdrag_ckval_A\n"
            "endif\n"
        )
        rewritten = variable_registry.rewrite_reference_variables_in_text(text, alias)
        self.assertNotIn("$Freq_Smile ", rewritten)
        self.assertIn("$Freq_Smile_1", rewritten)
        # 辅助变量名（ckprev_A_Freq_Smile）是**整段自洽**的标识符，不是对形态键
        # 变量的引用：它由本段自己声明，不能被改写
        self.assertIn("$ssmtdrag_ckprev_A_Freq_Smile", rewritten)

    def test_reference_alias_map_skips_real_owner_dedup(self):
        """基名属于别的 owner（真正的同名去重）时不做别名改写。"""
        _fake_bpy.data.node_groups[:] = [
            _make_tree(
                # あ / お 的 sanitize 结果都是 "shape" → 合法的同名去重
                _make_shapekey_item_node(("あ", "Freq_shape"), ("お", "Freq_shape_1")),
            )
        ]

        alias = variable_registry.build_shape_key_reference_alias_map()

        self.assertEqual(alias, {})

    def test_reference_alias_map_drops_ambiguous_base(self):
        """同一基名对应两个形态键且基名无人持有 → 歧义，不做别名。"""
        _fake_bpy.data.node_groups[:] = [
            _make_tree(
                _make_shapekey_item_node(("Smile", "Freq_Smile_1"), ("Smile", "Freq_Smile_2")),
            )
        ]

        alias = variable_registry.build_shape_key_reference_alias_map()

        self.assertEqual(alias, {})

    def test_generated_variable_families_use_disjoint_prefixes(self):
        swap_node = types.SimpleNamespace(
            bl_idname="SSMTNode_ObjectSwap",
            custom_var_name="",
            assigned_variable_name="",
        )

        swap_name = variable_registry.ensure_object_swap_variable_name(swap_node)
        shape_name = variable_registry.allocate_shape_key_variable_name("Smile")
        continuous_name = variable_registry.allocate_continuous_shapekey_index_variable_name()
        uv_name = variable_registry.allocate_uv_offset_variable_name("X")

        generated = {swap_name, shape_name, continuous_name, uv_name}
        self.assertEqual(len(generated), 4)
        self.assertTrue(swap_name.startswith("swapkey"))
        self.assertTrue(shape_name.startswith("Freq_"))
        self.assertTrue(continuous_name.startswith("continuous_shapekey_frame"))
        self.assertTrue(uv_name.startswith("uv_offset_"))

    def test_normalize_variable_name_is_ini_safe_and_stable(self):
        self.assertEqual(variable_registry.normalize_variable_name(" $9 Hair/Style! "), "_9_HairStyle")
        self.assertEqual(variable_registry.normalize_variable_name("$$$"), "")

    def test_object_swap_export_validation_rejects_duplicate_effective_names(self):
        nodes = [
            _make_swap_node("$Shared_Swap", name="Hair"),
            _make_swap_node("shared_swap", name="Coat"),
        ]

        with self.assertRaisesRegex(ValueError, "物体切换变量名.*重复.*Hair.*Coat"):
            variable_registry.validate_unique_object_swap_variable_names(nodes)

    def test_object_swap_export_validation_uses_only_each_nodes_effective_name(self):
        nodes = [
            types.SimpleNamespace(
                bl_idname="SSMTNode_ObjectSwap",
                name="Hair",
                custom_var_name="hair_mode",
                assigned_variable_name="swapkey0",
            ),
            types.SimpleNamespace(
                bl_idname="SSMTNode_ObjectSwap",
                name="Coat",
                custom_var_name="coat_mode",
                assigned_variable_name="swapkey0",
            ),
        ]

        variable_registry.validate_unique_object_swap_variable_names(nodes)


if __name__ == "__main__":
    unittest.main()
