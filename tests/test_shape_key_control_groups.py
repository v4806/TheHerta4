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


PKG = "_shape_key_control_groups_test_pkg"
for package_name in (PKG, f"{PKG}.toolkit"):
    package = _install_module(package_name)
    package.__path__ = []


_install_module(
    "bpy",
    types=types.SimpleNamespace(Operator=object),
)
_install_module("mathutils", Matrix=object)


def _load_module(module_name, relative_path):
    module_path = Path(__file__).resolve().parents[1] / relative_path
    spec = importlib.util.spec_from_file_location(f"{PKG}.{module_name}", module_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


skc = _load_module("toolkit.at_shape_key_control", "toolkit/at_shape_key_control.py")


# ------------------------------------------------------------------
# 轻量替身
# ------------------------------------------------------------------

class _FakeItem:
    def __init__(self):
        self.is_group = False
        self.name = ""
        self.label = ""
        self.key_names = ""
        self.value = 0.0
        self.group_value = 0.0


class _FakeCollection(list):
    def add(self):
        item = _FakeItem()
        self.append(item)
        return item

    def clear(self):
        list.clear(self)


class _FakeProps:
    def __init__(self, use_grouping=True, value_min=0.0, value_max=1.0):
        self.shape_key_list = _FakeCollection()
        self.shape_key_list_index = 0
        self.sk_use_grouping = use_grouping
        self.sk_value_min = value_min
        self.sk_value_max = value_max


class _FakeKeyBlock:
    def __init__(self, name, value=0.0):
        self.name = name
        self.value = value
        self.slider_min = 0.0
        self.slider_max = 1.0


class _FakeKeyBlocks(list):
    def get(self, name):
        return next((item for item in self if item.name == name), None)


class _FakeObject:
    def __init__(self, values, name="Mesh"):
        blocks = _FakeKeyBlocks(
            [_FakeKeyBlock(key_name, value) for key_name, value in values.items()]
        )
        self.name = name
        self.type = "MESH"
        self.data = types.SimpleNamespace(
            shape_keys=types.SimpleNamespace(
                key_blocks=blocks,
                reference_key=blocks[0] if blocks else None,
            )
        )

    def key_block(self, name):
        return self.data.shape_keys.key_blocks.get(name)


def _make_context(props, *objects, scene_objects=None):
    return types.SimpleNamespace(
        scene=types.SimpleNamespace(
            atp_props=props,
            objects=list(objects if scene_objects is None else scene_objects),
        ),
        selected_objects=list(objects),
        active_object=objects[0] if objects else None,
    )


class ContinuousGroupTests(unittest.TestCase):
    def test_parse_shape_key_name(self):
        self.assertEqual(skc.parse_shape_key_name("Motion_Key_7"), ("Motion_Key", 7))
        self.assertEqual(skc.parse_shape_key_name("Face_A_12"), ("Face_A", 12))
        self.assertIsNone(skc.parse_shape_key_name("Smile"))
        self.assertIsNone(skc.parse_shape_key_name("Smile_"))

    def test_build_continuous_groups_merges_consecutive_numbers(self):
        groups = skc.build_continuous_groups(
            {"Motion_Key_1", "Motion_Key_2", "Motion_Key_3", "Smile"}
        )

        self.assertEqual(len(groups), 1)
        prefix, names, label = groups[0]
        self.assertEqual(prefix, "Motion_Key")
        self.assertEqual(names, ["Motion_Key_1", "Motion_Key_2", "Motion_Key_3"])
        self.assertEqual(label, "Motion_Key [1-3]")

    def test_build_continuous_groups_splits_on_number_gap(self):
        groups = skc.build_continuous_groups(
            {"Key_1", "Key_2", "Key_5", "Key_6", "Key_7"}
        )

        self.assertEqual(
            [(prefix, names) for prefix, names, _label in groups],
            [
                ("Key", ["Key_1", "Key_2"]),
                ("Key", ["Key_5", "Key_6", "Key_7"]),
            ],
        )

    def test_build_continuous_groups_ignores_single_member_runs(self):
        self.assertEqual(skc.build_continuous_groups({"Solo_1", "Solo_4"}), [])


class ValueRangeTests(unittest.TestCase):
    def test_default_range_is_zero_to_one(self):
        props = _FakeProps()

        self.assertEqual(skc.get_value_range(props), (0.0, 1.0))
        self.assertAlmostEqual(skc.to_normalized(0.5, props), 0.5)
        self.assertAlmostEqual(skc.to_actual(0.25, props), 0.25)

    def test_custom_range_maps_slider_to_actual_value(self):
        props = _FakeProps(value_min=-1.0, value_max=3.0)

        self.assertAlmostEqual(skc.to_actual(0.0, props), -1.0)
        self.assertAlmostEqual(skc.to_actual(0.5, props), 1.0)
        self.assertAlmostEqual(skc.to_actual(1.0, props), 3.0)
        self.assertAlmostEqual(skc.to_normalized(1.0, props), 0.5)

    def test_invalid_range_is_reported_as_none(self):
        self.assertIsNone(skc.get_value_range(_FakeProps(value_min=2.0, value_max=1.0)))

    def test_derive_group_position_counts_full_keys_then_partial(self):
        props = _FakeProps()
        key_values = {"Key_1": 1.0, "Key_2": 0.4, "Key_3": 0.0}

        position = skc.derive_group_position(
            ["Key_1", "Key_2", "Key_3"], key_values, skc.get_value_range(props)
        )

        self.assertAlmostEqual(position, 1.4)


class ApplyValueTests(unittest.TestCase):
    def test_apply_group_values_fills_keys_sequentially(self):
        props = _FakeProps()
        obj = _FakeObject({"Basis": 0.0, "Key_1": 0.0, "Key_2": 0.0, "Key_3": 0.0})
        context = _make_context(props, obj)

        skc.apply_group_values(context, ["Key_1", "Key_2", "Key_3"], 1.5)

        values = [kb.value for kb in obj.data.shape_keys.key_blocks]
        self.assertEqual(values, [0.0, 1.0, 0.5, 0.0])

    def test_apply_group_values_respects_value_range(self):
        props = _FakeProps(value_min=0.0, value_max=2.0)
        obj = _FakeObject({"Basis": 0.0, "Key_1": 0.0, "Key_2": 0.0})
        context = _make_context(props, obj)

        skc.apply_group_values(context, ["Key_1", "Key_2"], 1.5)

        blocks = obj.data.shape_keys.key_blocks
        self.assertAlmostEqual(blocks.get("Key_1").value, 2.0)
        self.assertAlmostEqual(blocks.get("Key_2").value, 1.0)
        self.assertEqual(blocks.get("Key_1").slider_max, 2.0)

class UnifiedSingleKeyTests(unittest.TestCase):
    """单键行的语义：一行滑块 = 一个形态键名，拖动时选中的同名键一起变。

    用户验收场景：选中 10 个都有同名形态键的物体，拖动时这 10 个都必须变；
    没被选中的物体一个都不许动（写值范围与刷新出来的列表完全一致）。
    """

    def setUp(self):
        skc._suppress_update = False

    @staticmethod
    def _ten_objects(key_name="Smile"):
        return [
            _FakeObject({"Basis": 0.0, key_name: 0.0}, name=f"SKObj{index}")
            for index in range(10)
        ]

    def _refresh(self, props, objects, selected=None, active=None):
        context = _make_context(
            props,
            *(objects if selected is None else selected),
            scene_objects=objects,
        )
        if active is not None:
            context.active_object = active
        skc.refresh_from_context(context)
        return context

    def test_single_value_writes_all_ten_selected_objects(self):
        props = _FakeProps()
        objects = self._ten_objects()
        context = self._refresh(props, objects, active=objects[0])
        item = next(item for item in props.shape_key_list if item.name == "Smile")

        item.value = 0.5
        skc.on_single_value_update(item, context)

        self.assertEqual(
            [obj.key_block("Smile").value for obj in objects],
            [0.5] * 10,
        )

    def test_single_value_only_touches_selected_objects(self):
        """只选中 1 个物体时，其余 9 个必须原样不动（范围跟着选择走）。"""
        props = _FakeProps()
        objects = self._ten_objects()
        context = self._refresh(props, objects, selected=objects[:1], active=objects[0])
        item = next(item for item in props.shape_key_list if item.name == "Smile")

        item.value = 0.25
        skc.on_single_value_update(item, context)

        self.assertEqual(objects[0].key_block("Smile").value, 0.25)
        self.assertEqual([obj.key_block("Smile").value for obj in objects[1:]], [0.0] * 9)

    def test_single_value_falls_back_to_active_object_without_selection(self):
        """一个都没选中时退回活动物体，其余物体不动。"""
        props = _FakeProps()
        objects = self._ten_objects()
        context = self._refresh(props, objects, selected=[], active=objects[0])
        item = next(item for item in props.shape_key_list if item.name == "Smile")

        item.value = 0.75
        skc.on_single_value_update(item, context)

        self.assertEqual(objects[0].key_block("Smile").value, 0.75)
        self.assertEqual([obj.key_block("Smile").value for obj in objects[1:]], [0.0] * 9)

    def test_objects_without_the_key_are_left_alone(self):
        props = _FakeProps()
        object_a = _FakeObject({"Basis": 0.0, "Smile": 0.0}, name="A")
        object_b = _FakeObject({"Basis": 0.0, "Smile": 0.0}, name="B")
        plain = _FakeObject({"Basis": 0.0, "Wave": 0.0}, name="C")
        context = self._refresh(props, [object_a, object_b, plain])
        item = next(item for item in props.shape_key_list if item.name == "Smile")

        item.value = 0.5
        skc.on_single_value_update(item, context)

        self.assertAlmostEqual(object_b.key_block("Smile").value, 0.5)
        self.assertAlmostEqual(plain.key_block("Wave").value, 0.0)

    def test_single_value_is_clamped_by_value_range(self):
        props = _FakeProps(value_min=0.0, value_max=0.5)
        objects = self._ten_objects()
        context = self._refresh(props, objects)
        item = next(item for item in props.shape_key_list if item.name == "Smile")

        item.value = 1.0
        skc.on_single_value_update(item, context)

        self.assertEqual([obj.key_block("Smile").value for obj in objects], [0.5] * 10)
        self.assertAlmostEqual(objects[0].key_block("Smile").slider_max, 0.5)
        self.assertAlmostEqual(item.value, 0.5, msg="滑块显示值也必须跟着夹取，否则与写进形态键的值不一致")

    def test_single_value_update_is_suppressed_while_refreshing(self):
        props = _FakeProps()
        objects = self._ten_objects()
        context = self._refresh(props, objects)
        item = next(item for item in props.shape_key_list if item.name == "Smile")

        skc._suppress_update = True
        try:
            skc.on_single_value_update(item, context)
        finally:
            skc._suppress_update = False

        self.assertEqual([obj.key_block("Smile").value for obj in objects], [0.0] * 10)

    def test_apply_single_values_returns_written_count(self):
        props = _FakeProps()
        object_a = _FakeObject({"Basis": 0.0, "Smile": 0.0}, name="A")
        object_b = _FakeObject({"Basis": 0.0, "Smile": 0.0}, name="B")
        plain = _FakeObject({"Basis": 0.0, "Wave": 0.0}, name="C")
        context = self._refresh(props, [object_a, object_b, plain])

        written = skc.apply_single_values(context, ["Smile"], 0.5)

        self.assertEqual(written, 2)


class NoHandlerLeftoversTests(unittest.TestCase):
    """单键联动必须走属性 update 回调：depsgraph 处理器那一套已整体移除。

    旧实现靠 depsgraph_update_post 处理器把参考物体的值同步出去，处理器一旦没跑
    （开关关闭 / 未挂载 / 异常被吞），就只剩滑块绑定的那一个物体变 —— 用户看到的
    「只有当前活动物体变化」正是这么来的。
    """

    def test_removed_handler_api_is_gone(self):
        for name in (
            "register_shape_key_sync",
            "unregister_shape_key_sync",
            "_on_depsgraph_update",
            "sync_reference_shape_keys",
            "snapshot_reference_values",
            "resolve_reference",
        ):
            self.assertFalse(hasattr(skc, name), f"{name} 应该已被移除")

    def test_removed_options_are_gone(self):
        props = _FakeProps()

        for name in ("sk_live_sync", "sk_auto_follow", "sk_drive_scope"):
            self.assertFalse(hasattr(props, name), f"{name} 应该已被移除")

    def test_reference_values_cache_is_gone(self):
        self.assertFalse(hasattr(skc, "_REFERENCE_VALUES"))
        self.assertFalse(hasattr(skc, "_SELECTION_SIGNATURE"))


class SelectionScopedDriveTests(unittest.TestCase):
    """驱动集合 = 当前选中的带形态键的物体；刷新列表与写值范围同源。"""

    def setUp(self):
        skc._suppress_update = False

    @staticmethod
    def _three_objects():
        return (
            _FakeObject({"Basis": 0.0, "Smile": 0.0}, name="A"),
            _FakeObject({"Basis": 0.0, "Smile": 0.0}, name="B"),
            _FakeObject({"Basis": 0.0, "Smile": 0.0}, name="C"),
        )

    @staticmethod
    def _plain_object(name="P"):
        return types.SimpleNamespace(
            name=name,
            type="MESH",
            data=types.SimpleNamespace(shape_keys=None),
        )

    def test_driving_objects_uses_selection_with_active_first(self):
        props = _FakeProps()
        obj_a, obj_b, obj_c = self._three_objects()
        context = _make_context(props, obj_a, obj_b, obj_c, scene_objects=[obj_a, obj_b, obj_c])
        context.active_object = obj_b

        driven = skc.driving_objects(context)

        self.assertEqual([obj.name for obj in driven], ["B", "A", "C"])

    def test_driving_objects_ignores_unselected_scene_objects(self):
        props = _FakeProps()
        obj_a, obj_b, obj_c = self._three_objects()
        context = _make_context(props, obj_a, scene_objects=[obj_a, obj_b, obj_c])

        self.assertEqual([obj.name for obj in skc.driving_objects(context)], ["A"])

    def test_driving_objects_falls_back_to_active_object(self):
        props = _FakeProps()
        obj_a, obj_b, _obj_c = self._three_objects()
        context = _make_context(props, scene_objects=[obj_a, obj_b])
        context.active_object = obj_b

        self.assertEqual([obj.name for obj in skc.driving_objects(context)], ["B"])

    def test_driving_objects_keeps_active_object_outside_selection(self):
        """活动物体通常已在选中集里；万一不在（脚本改选择），也不能漏掉它。"""
        props = _FakeProps()
        obj_a, obj_b, _obj_c = self._three_objects()
        context = _make_context(props, obj_a, scene_objects=[obj_a, obj_b])
        context.active_object = obj_b

        self.assertEqual([obj.name for obj in skc.driving_objects(context)], ["B", "A"])

    def test_driving_objects_skips_objects_without_shape_keys(self):
        props = _FakeProps()
        obj_a, _obj_b, _obj_c = self._three_objects()
        context = _make_context(props, self._plain_object(), obj_a, scene_objects=[obj_a])

        self.assertEqual([obj.name for obj in skc.driving_objects(context)], ["A"])

    def test_refresh_only_lists_keys_of_selected_objects(self):
        props = _FakeProps()
        obj_a = _FakeObject({"Basis": 0.0, "Smile": 0.0}, name="A")
        obj_b = _FakeObject({"Basis": 0.0, "Wave_1": 0.0}, name="B")
        context = _make_context(props, obj_a, scene_objects=[obj_a, obj_b])

        found = skc.refresh_from_context(context)

        self.assertEqual(sorted(found), ["Smile"])

    def test_unselected_object_is_never_written(self):
        """范围一致性：没选中的物体带着同名键也不许被偷偷改值。"""
        props = _FakeProps()
        obj_a = _FakeObject({"Basis": 0.0, "Smile": 0.0}, name="A")
        obj_b = _FakeObject({"Basis": 0.0, "Smile": 0.0}, name="B")
        context = _make_context(props, obj_a, scene_objects=[obj_a, obj_b])
        skc.refresh_from_context(context)
        item = next(item for item in props.shape_key_list if item.name == "Smile")

        item.value = 0.6
        skc.on_single_value_update(item, context)

        self.assertAlmostEqual(obj_a.key_block("Smile").value, 0.6)
        self.assertAlmostEqual(obj_b.key_block("Smile").value, 0.0)

    def test_describe_scope_reports_selection_size(self):
        props = _FakeProps()
        obj_a, obj_b, _obj_c = self._three_objects()
        context = _make_context(props, obj_a, obj_b, scene_objects=[obj_a, obj_b])

        self.assertEqual(skc.describe_scope(context), "作用于选中的 2 个带形态键的物体")

    def test_describe_scope_reports_active_fallback(self):
        props = _FakeProps()
        obj_a, obj_b, _obj_c = self._three_objects()
        context = _make_context(props, scene_objects=[obj_a, obj_b])
        context.active_object = obj_a

        self.assertEqual(skc.describe_scope(context), "未选中物体：暂时只作用于活动物体 A")

    def test_describe_scope_reports_nothing_to_control(self):
        props = _FakeProps()
        plain = self._plain_object()
        context = _make_context(props, plain, scene_objects=[plain])

        self.assertEqual(skc.describe_scope(context), "未选中可用物体：请选中带形态键的网格物体")

    def test_refresh_fills_item_value_from_first_holder(self):
        props = _FakeProps()
        obj_a = _FakeObject({"Basis": 0.0, "Smile": 0.3}, name="A")
        obj_b = _FakeObject({"Basis": 0.0, "Smile": 0.9}, name="B")
        context = _make_context(props, obj_b, obj_a, scene_objects=[obj_a, obj_b])
        context.active_object = obj_b

        skc.refresh_from_context(context)

        item = next(item for item in props.shape_key_list if item.name == "Smile")
        self.assertAlmostEqual(item.value, 0.9)

    def test_group_values_drive_every_selected_object(self):
        props = _FakeProps()
        obj_a = _FakeObject({"Basis": 0.0, "Motion_Key_1": 0.0, "Motion_Key_2": 0.0}, name="A")
        obj_b = _FakeObject({"Basis": 0.0, "Motion_Key_1": 0.0, "Motion_Key_2": 0.0}, name="B")
        context = _make_context(props, obj_a, obj_b, scene_objects=[obj_a, obj_b])
        skc.refresh_from_context(context)
        group_item = next(item for item in props.shape_key_list if item.is_group)

        skc.apply_group_values(context, skc.split_key_names(group_item.key_names), 0.5)

        self.assertAlmostEqual(obj_a.key_block("Motion_Key_1").value, 0.5)
        self.assertAlmostEqual(obj_b.key_block("Motion_Key_1").value, 0.5)
        self.assertAlmostEqual(obj_b.key_block("Motion_Key_2").value, 0.0)

    def test_apply_range_does_not_equalize_values_across_objects(self):
        """值域只夹取各自的值，不做跨物体联动。"""
        props = _FakeProps(value_min=0.0, value_max=1.0)
        obj_a = _FakeObject({"Basis": 0.0, "Smile": 0.2}, name="A")
        obj_b = _FakeObject({"Basis": 0.0, "Smile": 0.8}, name="B")
        context = _make_context(props, obj_a, obj_b, scene_objects=[obj_a, obj_b])
        skc.refresh_from_context(context)

        skc.apply_shape_key_range(context, props)

        self.assertAlmostEqual(obj_a.key_block("Smile").value, 0.2)
        self.assertAlmostEqual(obj_b.key_block("Smile").value, 0.8)

    def test_refresh_from_context_clamps_item_value_after_range_apply(self):
        props = _FakeProps(value_min=0.0, value_max=0.5)
        obj_a = _FakeObject({"Basis": 0.0, "Smile": 1.0}, name="A")
        context = _make_context(props, obj_a, scene_objects=[obj_a])
        skc.refresh_from_context(context)

        skc.apply_shape_key_range(context, props)

        item = next(item for item in props.shape_key_list if item.name == "Smile")
        self.assertAlmostEqual(item.value, 0.5)


class ApplyRangeTests(unittest.TestCase):
    def test_apply_shape_key_range_writes_slider_range_and_clamps_values(self):
        props = _FakeProps(value_min=0.0, value_max=0.5)
        obj = _FakeObject({"Basis": 0.0, "Smile": 1.0})
        context = _make_context(props, obj)
        skc.refresh_shape_key_list(props, [obj], obj)

        result = skc.apply_shape_key_range(context, props)

        self.assertTrue(result)
        key_block = obj.key_block("Smile")
        self.assertEqual(key_block.slider_min, 0.0)
        self.assertAlmostEqual(key_block.slider_max, 0.5)
        self.assertAlmostEqual(key_block.value, 0.5)

    def test_apply_shape_key_range_rejects_invalid_range(self):
        props = _FakeProps(value_min=2.0, value_max=1.0)
        obj = _FakeObject({"Basis": 0.0, "Smile": 0.0})
        context = _make_context(props, obj)

        self.assertFalse(skc.apply_shape_key_range(context, props))

    def test_apply_shape_key_range_covers_group_members(self):
        props = _FakeProps(value_min=0.0, value_max=3.0)
        obj = _FakeObject({
            "Basis": 0.0,
            "Key_1": 0.0,
            "Key_2": 0.0,
        })
        context = _make_context(props, obj)
        skc.refresh_shape_key_list(props, [obj], obj)

        skc.apply_shape_key_range(context, props)

        self.assertAlmostEqual(obj.key_block("Key_1").slider_max, 3.0)
        self.assertAlmostEqual(obj.key_block("Key_2").slider_max, 3.0)


class RefreshListTests(unittest.TestCase):
    def test_refresh_groups_continuous_keys_and_keeps_singles(self):
        props = _FakeProps()
        obj = _FakeObject({
            "Basis": 0.0,
            "Motion_Key_1": 1.0,
            "Motion_Key_2": 0.5,
            "Motion_Key_3": 0.0,
            "Smile": 0.25,
        })

        found = skc.refresh_shape_key_list(props, [obj])

        self.assertEqual(
            found,
            {"Motion_Key_1", "Motion_Key_2", "Motion_Key_3", "Smile"},
        )
        self.assertEqual(len(props.shape_key_list), 2)

        group_item = props.shape_key_list[0]
        self.assertTrue(group_item.is_group)
        self.assertEqual(group_item.name, "Motion_Key")
        self.assertEqual(group_item.label, "Motion_Key [1-3]")
        self.assertEqual(
            skc.split_key_names(group_item.key_names),
            ["Motion_Key_1", "Motion_Key_2", "Motion_Key_3"],
        )
        self.assertAlmostEqual(group_item.group_value, 0.5)

        single_item = props.shape_key_list[1]
        self.assertFalse(single_item.is_group)
        self.assertEqual(single_item.name, "Smile")
        self.assertAlmostEqual(single_item.value, 0.25)

    def test_refresh_can_disable_grouping(self):
        props = _FakeProps(use_grouping=False)
        obj = _FakeObject({"Basis": 0.0, "Motion_Key_1": 0.0, "Motion_Key_2": 0.0})

        skc.refresh_shape_key_list(props, [obj])

        self.assertEqual(len(props.shape_key_list), 2)
        self.assertTrue(all(not item.is_group for item in props.shape_key_list))

    def test_refresh_keeps_actual_value_untouched(self):
        props = _FakeProps(value_min=0.0, value_max=2.0)
        obj = _FakeObject({"Basis": 0.0, "Smile": 1.0})

        skc.refresh_shape_key_list(props, [obj])

        self.assertAlmostEqual(props.shape_key_list[0].value, 1.0)

    def test_refresh_clamps_active_index(self):
        props = _FakeProps()
        props.shape_key_list_index = 12
        obj = _FakeObject({"Basis": 0.0, "Smile": 0.0})

        skc.refresh_shape_key_list(props, [obj])

        self.assertEqual(props.shape_key_list_index, 0)


if __name__ == "__main__":
    unittest.main()
