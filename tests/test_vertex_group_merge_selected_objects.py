# -*- coding: utf-8 -*-
"""工具集「顶点组合并」按名称作用于所有选中物体的回归测试。

覆盖三件事：
1. ``VertexGroupUtils.merge_named_vertex_groups_on_objects`` 的多物体批量语义；
2. ``toolkit.bmtp_merge_vertex_groups`` 算子的目标范围（选中物体 / 活动物体开关）；
3. ``toolkit.bmtp_refresh_merge_vertex_groups`` 刷新时按名称合并各选中物体的组名。
"""

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


PKG = "_vertex_group_merge_selected_pkg"
for package_name in (PKG, f"{PKG}.utils", f"{PKG}.toolkit"):
    package = _install_module(package_name)
    package.__path__ = []


_install_module(
    "bpy",
    types=types.SimpleNamespace(Operator=object),
    props=types.SimpleNamespace(BoolProperty=lambda **_kwargs: False),
)
_install_module("mathutils", Vector=object)
_install_module(f"{PKG}.utils.format_utils", Fatal=RuntimeError)


def _load_module(module_name, relative_path):
    module_path = Path(__file__).resolve().parents[1] / relative_path
    spec = importlib.util.spec_from_file_location(f"{PKG}.{module_name}", module_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


vertexgroup_utils_module = _load_module("utils.vertexgroup_utils", "utils/vertexgroup_utils.py")
weight_tools_module = _load_module("toolkit.bmtp_weight_tools", "toolkit/bmtp_weight_tools.py")

VertexGroupUtils = vertexgroup_utils_module.VertexGroupUtils
RefreshMergeVertexGroups = weight_tools_module.BMTP_OT_RefreshMergeVertexGroups
MergeVertexGroups = weight_tools_module.BMTP_OT_MergeVertexGroups


class _FakeAssignment:
    def __init__(self, group, weight):
        self.group = group
        self.weight = weight


class _FakeVertex:
    def __init__(self, index, assignments):
        self.index = index
        self.groups = list(assignments)


class _FakeVertexGroup:
    def __init__(self, name, index):
        self.name = name
        self.index = index
        self.removed_indices = []
        self.assigned_weights = {}

    def remove(self, vertex_indices):
        self.removed_indices.extend(vertex_indices)
        for vertex_index in vertex_indices:
            self.assigned_weights.pop(vertex_index, None)

    def add(self, vertex_indices, weight, mode):
        if mode != 'REPLACE':
            raise AssertionError(f"unexpected assignment mode: {mode}")
        for vertex_index in vertex_indices:
            self.assigned_weights[vertex_index] = weight


class _FakeVertexGroups(list):
    def __init__(self, names):
        super().__init__(_FakeVertexGroup(name, index) for index, name in enumerate(names))
        self.active_index = 0

    def __getitem__(self, key):
        if isinstance(key, str):
            group = self.get(key)
            if group is None:
                raise KeyError(key)
            return group
        return super().__getitem__(key)

    def get(self, name):
        return next((group for group in self if group.name == name), None)

    def new(self, name):
        group = _FakeVertexGroup(name, len(self))
        self.append(group)
        return group

    def remove(self, group):
        super().remove(group)
        for index, remaining_group in enumerate(self):
            remaining_group.index = index


class _FakeObject:
    def __init__(self, name, group_names, vertices=None, mode='OBJECT', object_type='MESH'):
        self.name = name
        self.type = object_type
        self.mode = mode
        self.vertex_groups = _FakeVertexGroups(group_names)
        self.data = types.SimpleNamespace(vertices=list(vertices or []))


class _FakeCollection(list):
    def add(self):
        item = types.SimpleNamespace(name="", index=0, selected=False)
        self.append(item)
        return item


def _two_vertex_mesh():
    return [
        _FakeVertex(0, [_FakeAssignment(0, 0.5), _FakeAssignment(1, 0.25)]),
        _FakeVertex(1, [_FakeAssignment(1, 0.75)]),
    ]


def _make_props(**overrides):
    props = types.SimpleNamespace(
        wt_merge_apply_to_selected=True,
        wt_merge_target_name="",
        wt_merge_vertex_groups=_FakeCollection(),
        wt_merge_vertex_groups_index=0,
        wt_merge_source_object=None,
        wt_merge_source_object_name="",
    )
    for key, value in overrides.items():
        setattr(props, key, value)
    return props


def _make_checked_list(*names):
    return _FakeCollection([
        types.SimpleNamespace(name=name, index=index, selected=True)
        for index, name in enumerate(names)
    ])


def _make_context(props, active_object, selected_objects=()):
    return types.SimpleNamespace(
        active_object=active_object,
        selected_objects=list(selected_objects),
        scene=types.SimpleNamespace(bmtp_props=props),
    )


def _run_operator(operator, context):
    reports = []
    operator.report = lambda kinds, message: reports.append((kinds, message))
    result = operator.execute(context)
    return result, reports


class MergeNamedVertexGroupsOnObjectsTests(unittest.TestCase):
    def test_merges_matching_names_on_every_object(self):
        body = _FakeObject("Body", ["A", "B", "Keep"], _two_vertex_mesh())
        head = _FakeObject("Head", ["A", "B"], _two_vertex_mesh())

        result = VertexGroupUtils.merge_named_vertex_groups_on_objects([body, head], ["A", "B"])

        self.assertEqual([group.name for group in body.vertex_groups], ["A", "Keep"])
        self.assertEqual([group.name for group in head.vertex_groups], ["A"])
        self.assertEqual([item["name"] for item in result["processed"]], ["Body", "Head"])
        self.assertEqual({item["target_name"] for item in result["processed"]}, {"A"})
        self.assertEqual(result["skipped"], [])
        self.assertEqual(result["failed"], [])

    def test_object_with_fewer_than_two_matches_is_skipped(self):
        body = _FakeObject("Body", ["A", "B"], _two_vertex_mesh())
        hand = _FakeObject("Hand", ["A", "Other"], _two_vertex_mesh())

        result = VertexGroupUtils.merge_named_vertex_groups_on_objects([body, hand], ["A", "B"])

        self.assertEqual([item["name"] for item in result["processed"]], ["Body"])
        self.assertEqual(
            [(item["name"], item["reason"]) for item in result["skipped"]],
            [("Hand", "not_enough_groups")],
        )
        self.assertEqual([group.name for group in hand.vertex_groups], ["A", "Other"])

    def test_shared_target_name_applies_to_all_objects(self):
        body = _FakeObject("Body", ["A", "B"], _two_vertex_mesh())
        head = _FakeObject("Head", ["A", "B", "Extra"], _two_vertex_mesh())

        result = VertexGroupUtils.merge_named_vertex_groups_on_objects(
            [body, head], ["A", "B"], target_group_name="Merged"
        )

        self.assertEqual([group.name for group in body.vertex_groups], ["Merged"])
        # 新建的目标组会追加到组列表末尾（Blender 的 vertex_groups.new 语义）
        self.assertEqual([group.name for group in head.vertex_groups], ["Extra", "Merged"])
        self.assertEqual({item["target_name"] for item in result["processed"]}, {"Merged"})

    def test_failed_object_does_not_block_the_others(self):
        body = _FakeObject("Body", ["A", "B"], _two_vertex_mesh())
        head = _FakeObject("Head", ["A", "B", "Existing"], _two_vertex_mesh())

        result = VertexGroupUtils.merge_named_vertex_groups_on_objects(
            [body, head], ["A", "B"], target_group_name="Existing"
        )

        self.assertEqual([item["name"] for item in result["processed"]], ["Body"])
        self.assertEqual([item["name"] for item in result["failed"]], ["Head"])
        self.assertIn("already exists", result["failed"][0]["error"])
        self.assertEqual([group.name for group in head.vertex_groups], ["A", "B", "Existing"])

    def test_non_mesh_and_edit_mode_objects_are_skipped(self):
        body = _FakeObject("Body", ["A", "B"], _two_vertex_mesh())
        armature = _FakeObject("Armature", [], object_type='ARMATURE')
        edit_mode = _FakeObject("EditBody", ["A", "B"], _two_vertex_mesh(), mode='EDIT')

        result = VertexGroupUtils.merge_named_vertex_groups_on_objects(
            [body, armature, edit_mode], ["A", "B"]
        )

        self.assertEqual([item["name"] for item in result["processed"]], ["Body"])
        self.assertEqual(
            [(item["name"], item["reason"]) for item in result["skipped"]],
            [("Armature", "not_a_mesh"), ("EditBody", "not_object_mode")],
        )

    def test_requires_at_least_two_names(self):
        body = _FakeObject("Body", ["A", "B"], _two_vertex_mesh())

        with self.assertRaisesRegex(RuntimeError, "At least two vertex groups"):
            VertexGroupUtils.merge_named_vertex_groups_on_objects([body], ["A"])

    def test_duplicate_object_entries_merge_once(self):
        body = _FakeObject("Body", ["A", "B"], _two_vertex_mesh())

        result = VertexGroupUtils.merge_named_vertex_groups_on_objects([body, body], ["A", "B"])

        self.assertEqual(len(result["processed"]), 1)
        self.assertEqual([group.name for group in body.vertex_groups], ["A"])

    def test_group_order_follows_requested_names(self):
        body = _FakeObject("Body", ["B", "A"], _two_vertex_mesh())

        result = VertexGroupUtils.merge_named_vertex_groups_on_objects([body], ["A", "B"])

        self.assertEqual(result["processed"][0]["target_name"], "A")
        self.assertEqual([group.name for group in body.vertex_groups], ["A"])


class MergeVertexGroupsOperatorTests(unittest.TestCase):
    def test_operator_merges_every_selected_object(self):
        body = _FakeObject("Body", ["A", "B", "Keep"], _two_vertex_mesh())
        head = _FakeObject("Head", ["A", "B"], _two_vertex_mesh())
        props = _make_props(
            wt_merge_vertex_groups=_make_checked_list("A", "B"),
            wt_merge_source_object=body,
            wt_merge_source_object_name="Body",
        )
        context = _make_context(props, body, [body, head])

        result, reports = _run_operator(MergeVertexGroups(), context)

        self.assertEqual(result, {'FINISHED'})
        self.assertEqual([group.name for group in body.vertex_groups], ["A", "Keep"])
        self.assertEqual([group.name for group in head.vertex_groups], ["A"])
        self.assertTrue(any("已对 2 个物体完成顶点组合并" in message for _k, message in reports))
        self.assertEqual(reports[0][0], {'INFO'})
        # 多物体下留空目标名不会被固化成某个物体的名字
        self.assertEqual(props.wt_merge_target_name, "")
        self.assertEqual(
            [(item.name, item.selected) for item in props.wt_merge_vertex_groups],
            [("A", True), ("Keep", False)],
        )

    def test_operator_scope_can_be_limited_to_active_object(self):
        body = _FakeObject("Body", ["A", "B"], _two_vertex_mesh())
        head = _FakeObject("Head", ["A", "B"], _two_vertex_mesh())
        props = _make_props(
            wt_merge_apply_to_selected=False,
            wt_merge_vertex_groups=_make_checked_list("A", "B"),
            wt_merge_source_object=body,
            wt_merge_source_object_name="Body",
        )
        context = _make_context(props, body, [body, head])

        result, _reports = _run_operator(MergeVertexGroups(), context)

        self.assertEqual(result, {'FINISHED'})
        self.assertEqual([group.name for group in body.vertex_groups], ["A"])
        self.assertEqual([group.name for group in head.vertex_groups], ["A", "B"])
        # 单物体时保持原行为：目标名回写到输入框
        self.assertEqual(props.wt_merge_target_name, "A")

    def test_operator_cancels_when_no_object_can_merge(self):
        body = _FakeObject("Body", ["A", "Other"], _two_vertex_mesh())
        head = _FakeObject("Head", ["A", "Another"], _two_vertex_mesh())
        props = _make_props(
            wt_merge_vertex_groups=_make_checked_list("A", "B"),
            wt_merge_source_object=body,
            wt_merge_source_object_name="Body",
        )
        context = _make_context(props, body, [body, head])

        result, reports = _run_operator(MergeVertexGroups(), context)

        self.assertEqual(result, {'CANCELLED'})
        self.assertTrue(
            any("没有任何物体完成顶点组合并" in message for _k, message in reports)
        )
        self.assertEqual([group.name for group in body.vertex_groups], ["A", "Other"])
        self.assertEqual([group.name for group in head.vertex_groups], ["A", "Another"])

    def test_operator_warns_but_runs_for_multi_object_stale_list(self):
        body = _FakeObject("Body", ["A", "B"], _two_vertex_mesh())
        head = _FakeObject("Head", ["A", "B"], _two_vertex_mesh())
        old_source = _FakeObject("OldBody", ["A", "B"], _two_vertex_mesh())
        props = _make_props(
            wt_merge_vertex_groups=_make_checked_list("A", "B"),
            wt_merge_source_object=old_source,
            wt_merge_source_object_name="OldBody",
        )
        context = _make_context(props, body, [body, head])

        result, reports = _run_operator(MergeVertexGroups(), context)

        self.assertEqual(result, {'FINISHED'})
        self.assertEqual([group.name for group in body.vertex_groups], ["A"])
        self.assertEqual([group.name for group in head.vertex_groups], ["A"])
        self.assertTrue(
            any("不是按当前选择刷新的" in message for _k, message in reports)
        )
        self.assertIn({'WARNING'}, [kinds for kinds, _message in reports])

    def test_operator_still_guards_single_object_stale_list(self):
        body = _FakeObject("Body", ["A", "B"], _two_vertex_mesh())
        old_source = _FakeObject("OldBody", ["A", "B"], _two_vertex_mesh())
        props = _make_props(
            wt_merge_vertex_groups=_make_checked_list("A", "B"),
            wt_merge_source_object=old_source,
            wt_merge_source_object_name="OldBody",
        )
        context = _make_context(props, body, [])

        result, reports = _run_operator(MergeVertexGroups(), context)

        self.assertEqual(result, {'CANCELLED'})
        self.assertTrue(any("请先刷新" in message for _k, message in reports))
        self.assertEqual([group.name for group in body.vertex_groups], ["A", "B"])

    def test_operator_requires_two_checked_groups(self):
        body = _FakeObject("Body", ["A", "B"], _two_vertex_mesh())
        props = _make_props(
            wt_merge_vertex_groups=_make_checked_list("A"),
            wt_merge_source_object=body,
            wt_merge_source_object_name="Body",
        )
        context = _make_context(props, body, [body])

        result, reports = _run_operator(MergeVertexGroups(), context)

        self.assertEqual(result, {'CANCELLED'})
        self.assertTrue(any("请至少勾选两个顶点组" in message for _k, message in reports))

    def test_operator_skips_edit_mode_objects_without_failing(self):
        body = _FakeObject("Body", ["A", "B"], _two_vertex_mesh())
        edit_mode = _FakeObject("EditBody", ["A", "B"], _two_vertex_mesh(), mode='EDIT')
        props = _make_props(
            wt_merge_vertex_groups=_make_checked_list("A", "B"),
            wt_merge_source_object=body,
            wt_merge_source_object_name="Body",
        )
        context = _make_context(props, body, [body, edit_mode])

        result, reports = _run_operator(MergeVertexGroups(), context)

        self.assertEqual(result, {'FINISHED'})
        self.assertEqual([group.name for group in edit_mode.vertex_groups], ["A", "B"])
        self.assertTrue(any("已对 1 个物体完成顶点组合并" in message for _k, message in reports))

    def test_poll_requires_object_mode_mesh(self):
        body = _FakeObject("Body", ["A", "B"], _two_vertex_mesh())
        edit_mode = _FakeObject("EditBody", ["A", "B"], _two_vertex_mesh(), mode='EDIT')
        armature = _FakeObject("Armature", [], object_type='ARMATURE')

        self.assertTrue(MergeVertexGroups.poll(_make_context(None, body, [])))
        self.assertFalse(MergeVertexGroups.poll(_make_context(None, edit_mode, [])))
        self.assertFalse(MergeVertexGroups.poll(types.SimpleNamespace(active_object=armature)))


class RefreshMergeVertexGroupsOperatorTests(unittest.TestCase):
    def test_refresh_collects_names_across_selected_objects(self):
        body = _FakeObject("Body", ["A", "B"], _two_vertex_mesh())
        head = _FakeObject("Head", ["B", "C"], _two_vertex_mesh())
        props = _make_props(
            wt_merge_vertex_groups=_FakeCollection(),
            wt_merge_vertex_groups_index=7,
            wt_merge_target_name="Keep",
            wt_merge_source_object=body,
            wt_merge_source_object_name="Body",
        )
        context = _make_context(props, body, [body, head])

        result, reports = _run_operator(RefreshMergeVertexGroups(), context)

        self.assertEqual(result, {'FINISHED'})
        self.assertEqual(
            [(item.name, item.index, item.selected) for item in props.wt_merge_vertex_groups],
            [("A", 0, False), ("B", 1, False), ("C", 1, False)],
        )
        self.assertEqual(props.wt_merge_vertex_groups_index, 2)
        self.assertEqual(props.wt_merge_target_name, "Keep")
        self.assertTrue(any("2 个网格物体" in message for _k, message in reports))

    def test_refresh_ignores_selection_when_scope_is_active_object_only(self):
        body = _FakeObject("Body", ["A", "B"], _two_vertex_mesh())
        head = _FakeObject("Head", ["C"], _two_vertex_mesh())
        props = _make_props(
            wt_merge_apply_to_selected=False,
            wt_merge_vertex_groups=_FakeCollection(),
            wt_merge_source_object=body,
            wt_merge_source_object_name="Body",
        )
        context = _make_context(props, body, [body, head])

        result, _reports = _run_operator(RefreshMergeVertexGroups(), context)

        self.assertEqual(result, {'FINISHED'})
        self.assertEqual(
            [item.name for item in props.wt_merge_vertex_groups],
            ["A", "B"],
        )

    def test_refresh_reports_error_without_any_mesh(self):
        armature = _FakeObject("Armature", [], object_type='ARMATURE')
        props = _make_props(wt_merge_source_object=armature, wt_merge_source_object_name="Armature")
        context = _make_context(props, armature, [])

        result, reports = _run_operator(RefreshMergeVertexGroups(), context)

        self.assertEqual(result, {'CANCELLED'})
        self.assertTrue(any("网格物体" in message for _k, message in reports))


if __name__ == "__main__":
    unittest.main()
