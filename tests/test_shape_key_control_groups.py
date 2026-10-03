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
        self.reference_object = None
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
    def __init__(self, use_grouping=True, value_min=0.0, value_max=1.0, live_sync=True):
        self.shape_key_list = _FakeCollection()
        self.shape_key_list_index = 0
        self.sk_use_grouping = use_grouping
        self.sk_value_min = value_min
        self.sk_value_max = value_max
        self.sk_live_sync = live_sync


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


def _make_context(props, *objects):
    return types.SimpleNamespace(
        scene=types.SimpleNamespace(atp_props=props),
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

class LiveSyncTests(unittest.TestCase):
    def _refresh_two_objects(self, **props_kwargs):
        props = _FakeProps(**props_kwargs)
        obj_a = _FakeObject({"Basis": 0.0, "Smile": 0.0}, name="A")
        obj_b = _FakeObject({"Basis": 0.0, "Smile": 0.0}, name="B")
        context = _make_context(props, obj_a, obj_b)
        skc.refresh_shape_key_list(props, [obj_a, obj_b], obj_a)
        return props, obj_a, obj_b, context

    def test_refresh_records_reference_object_and_snapshot(self):
        props, obj_a, _obj_b, _context = self._refresh_two_objects()

        item = props.shape_key_list[0]
        self.assertIs(item.reference_object, obj_a)
        self.assertEqual(skc._REFERENCE_VALUES[("A", "Smile")], 0.0)

    def test_sync_propagates_reference_change_to_other_objects(self):
        props, obj_a, obj_b, context = self._refresh_two_objects()

        obj_a.key_block("Smile").value = 1.0
        synced = skc.sync_reference_shape_keys(context)

        self.assertEqual(synced, 1)
        self.assertAlmostEqual(obj_b.key_block("Smile").value, 1.0)

    def test_sync_is_idempotent_when_nothing_changed(self):
        _props, _obj_a, _obj_b, context = self._refresh_two_objects()

        self.assertEqual(skc.sync_reference_shape_keys(context), 0)

    def test_sync_can_be_disabled(self):
        _props, obj_a, obj_b, context = self._refresh_two_objects(live_sync=False)

        obj_a.key_block("Smile").value = 1.0

        self.assertEqual(skc.sync_reference_shape_keys(context), 0)
        self.assertAlmostEqual(obj_b.key_block("Smile").value, 0.0)

    def test_sync_needs_more_than_one_selected_object(self):
        props = _FakeProps()
        obj_a = _FakeObject({"Basis": 0.0, "Smile": 0.0}, name="A")
        context = _make_context(props, obj_a)
        skc.refresh_shape_key_list(props, [obj_a], obj_a)

        obj_a.key_block("Smile").value = 1.0

        self.assertEqual(skc.sync_reference_shape_keys(context), 0)


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
