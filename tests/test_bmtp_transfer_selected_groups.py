# -*- coding: utf-8 -*-
"""「只传递列表中选中的顶点组」路径的行为核查。

真实的顶点权重插值由 ``bpy.ops.object.data_transfer`` 完成（见
``test_bmtp_weight_transfer_mapping.py`` 锁定的面插值映射）。本测试用假 bpy
复刻该算子的**图层匹配语义**——源物体的全部顶点组按名字写进目标（即
``layers_select_src='ALL'`` / ``layers_select_dst='NAME'`` 在反向传递下的等价形式），
以核查工具集自身的备份/删除/还原簿记：传递结束后目标物体上只应留下选中的顶点组，
未选中的既有顶点组权重必须原样保留。

假 bpy 复刻的 REPLACE 语义（目标组原有权重被源权重整体替换）已由
``tests/blender_smoke_bmtp_weight_transfer.py`` 在 Blender 5.0.1 实机核对。
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


PKG = "_bmtp_transfer_selected_groups_test_pkg"
for package_name in (PKG, f"{PKG}.toolkit", f"{PKG}.utils"):
    package = _install_module(package_name)
    package.__path__ = []


class _FakeVertex:
    def __init__(self, index, vertex_groups):
        self.index = index
        self._vertex_groups = vertex_groups

    @property
    def groups(self):
        """复刻 MeshVertex.groups：该顶点落在哪些顶点组上及其权重。"""
        assignments = []
        for group_index, group in enumerate(self._vertex_groups):
            weight = group.weights.get(self.index)
            if weight is not None:
                assignments.append(types.SimpleNamespace(group=group_index, weight=weight))
        return assignments


class _FakeVertexGroup:
    def __init__(self, name):
        self.name = name
        self.weights = {}

    def add(self, indices, weight, mode="REPLACE"):
        for index in indices:
            self.weights[index] = weight

    def remove(self, indices):
        for index in indices:
            self.weights.pop(index, None)

    def weight(self, index):
        if index not in self.weights:
            raise RuntimeError("vertex is not in this vertex group")
        return self.weights[index]


class _FakeVertexGroups:
    def __init__(self):
        self._groups = []

    def __iter__(self):
        return iter(list(self._groups))

    def __len__(self):
        return len(self._groups)

    def __contains__(self, name):
        return self.get(name) is not None

    def get(self, name):
        for group in self._groups:
            if group.name == name:
                return group
        return None

    def new(self, name):
        group = _FakeVertexGroup(name)
        self._groups.append(group)
        return group

    def remove(self, group):
        self._groups.remove(group)

    def clear(self):
        self._groups = []

    @property
    def names(self):
        return [group.name for group in self._groups]


class _FakeObject:
    def __init__(self, name, vertex_count):
        self.name = name
        self.type = "MESH"
        self.mode = "OBJECT"
        self.vertex_groups = _FakeVertexGroups()
        self.data = types.SimpleNamespace(
            vertices=[
                _FakeVertex(index, self.vertex_groups) for index in range(vertex_count)
            ]
        )
        self.selected = False
        self.update_tag_calls = 0

    def update_tag(self):
        """顶点组增删后必须显式打标，否则 data_transfer 读到未刷新的求值副本。"""
        self.update_tag_calls += 1

    def select_get(self):
        return self.selected

    def select_set(self, value):
        self.selected = bool(value)

    def set_group(self, name, weights):
        group = self.vertex_groups.get(name) or self.vertex_groups.new(name=name)
        group.weights = dict(weights)
        return group

    def weights_of(self, name):
        group = self.vertex_groups.get(name)
        return None if group is None else dict(group.weights)


class _FakeObjectList(list):
    def __init__(self, items):
        super().__init__(items)
        self.active = None


class _FakeViewLayer:
    def __init__(self, objects):
        self.objects = _FakeObjectList(objects)

    def update(self):
        pass


class _FakeObjectOps:
    """复刻 data_transfer 的图层匹配：选中物体中非激活的那个是源，激活物体是目标。"""

    def __init__(self, context):
        self.context = context
        self.calls = []
        self.fail_next = None

    def mode_set(self, mode="OBJECT"):
        active = self.context.view_layer.objects.active
        if active is not None:
            active.mode = mode

    def data_transfer(self, **kwargs):
        active = self.context.view_layer.objects.active
        selected = [obj for obj in self.context.view_layer.objects if obj.selected]
        sources = [obj for obj in selected if obj is not active]
        self.calls.append(
            {
                "kwargs": kwargs,
                "active": getattr(active, "name", None),
                "selected": sorted(obj.name for obj in selected),
                "source_groups": sorted(
                    group.name for source in sources for group in source.vertex_groups
                ),
            }
        )

        if self.fail_next is not None:
            error = self.fail_next
            self.fail_next = None
            raise error

        for source in sources:
            for group in source.vertex_groups:
                target_group = active.vertex_groups.get(group.name)
                if target_group is None:
                    target_group = active.vertex_groups.new(name=group.name)
                # data_transfer 默认 mix_mode=REPLACE、mix_factor=1.0：目标权重被
                # 「源面顶点权重插值结果」整体覆盖（源侧没有该组权重的顶点归零 =
                # 不参与该组），而不是与目标原有权重合并。
                target_group.weights = dict(group.weights)


class _FakeDepsgraph:
    def update(self):
        pass


_context = types.SimpleNamespace(
    view_layer=None,
    scene=None,
    selected_objects=[],
    evaluated_depsgraph_get=lambda: _FakeDepsgraph(),
)

_fake_object_ops = _FakeObjectOps(_context)
_install_module(
    "bpy",
    types=types.SimpleNamespace(Operator=object),
    props=types.SimpleNamespace(
        BoolProperty=lambda **_kwargs: None,
        CollectionProperty=lambda **_kwargs: None,
    ),
    ops=types.SimpleNamespace(object=_fake_object_ops),
    context=_context,
    data=types.SimpleNamespace(objects={}),
)
_install_module(f"{PKG}.utils.vertexgroup_utils", VertexGroupUtils=types.SimpleNamespace())


class _Fatal(Exception):
    pass


_install_module(f"{PKG}.utils.format_utils", Fatal=_Fatal)

module_path = Path(__file__).resolve().parents[1] / "toolkit" / "bmtp_weight_tools.py"
spec = importlib.util.spec_from_file_location(f"{PKG}.toolkit.bmtp_weight_tools", module_path)
weight_tools = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = weight_tools
spec.loader.exec_module(weight_tools)

_bpy = sys.modules["bpy"]


def _group_item(name, selected):
    return types.SimpleNamespace(name=name, selected=selected)


class BMTPTransferSelectedGroupsTests(unittest.TestCase):
    def setUp(self):
        _fake_object_ops.calls = []
        _fake_object_ops.fail_next = None

    def _build_scene(self, objects, active, cleanup, selected_groups, use_selected=True):
        view_layer = _FakeViewLayer(objects)
        view_layer.objects.active = active
        context = types.SimpleNamespace(
            scene=types.SimpleNamespace(
                bmtp_props=types.SimpleNamespace(
                    wt_source_obj=objects[0],
                    wt_cleanup=cleanup,
                    wt_use_selected_groups=use_selected,
                    wt_vertex_groups=[
                        _group_item(name, name in selected_groups)
                        for name in objects[0].vertex_groups.names
                    ],
                    wt_use_shapekey_positions=False,
                    wt_use_armature_positions=False,
                )
            ),
            view_layer=view_layer,
            active_object=active,
            selected_objects=list(objects),
            evaluated_depsgraph_get=lambda: _FakeDepsgraph(),
        )
        _context.view_layer = view_layer
        _bpy.data = types.SimpleNamespace(
            objects={obj.name: obj for obj in objects}
        )
        return context

    def _run(self, context):
        operator = weight_tools.BMTP_OT_TransferWeights()
        reports = []
        operator.report = lambda level, message: reports.append((level, message))
        result = operator.execute(context)
        return result, reports

    def test_only_selected_group_survives_when_cleanup_enabled(self):
        source = _FakeObject("Source", 3)
        source.set_group("G1", {0: 1.0, 1: 0.5})
        source.set_group("G2", {2: 1.0})
        source.set_group("G3", {0: 0.25})
        target = _FakeObject("Target", 3)
        target.set_group("G2", {0: 0.75})
        source.selected = True
        target.selected = True

        context = self._build_scene(
            [source, target], target, cleanup=True, selected_groups={"G1"}
        )
        result, _reports = self._run(context)

        self.assertEqual(result, {"FINISHED"})
        self.assertEqual(target.vertex_groups.names, ["G1"])
        self.assertEqual(target.weights_of("G1"), {0: 1.0, 1: 0.5})
        # 源物体只被临时剔除未选中的组，结束后必须原样回来
        self.assertEqual(sorted(source.vertex_groups.names), ["G1", "G2", "G3"])
        self.assertEqual(source.weights_of("G2"), {2: 1.0})
        self.assertEqual(source.weights_of("G3"), {0: 0.25})

    def test_unselected_existing_groups_keep_their_original_weights(self):
        source = _FakeObject("Source", 3)
        source.set_group("G1", {0: 1.0})
        source.set_group("G2", {1: 0.6, 2: 0.4})
        source.set_group("G3", {0: 0.25})
        target = _FakeObject("Target", 3)
        target.set_group("KeepMe", {0: 0.5, 1: 0.25})
        target.set_group("G2", {0: 0.75})
        source.selected = True
        target.selected = True

        context = self._build_scene(
            [source, target], target, cleanup=False, selected_groups={"G2"}
        )
        result, _reports = self._run(context)

        self.assertEqual(result, {"FINISHED"})
        self.assertEqual(sorted(target.vertex_groups.names), ["G2", "KeepMe"])
        self.assertEqual(target.weights_of("G2"), {1: 0.6, 2: 0.4})
        self.assertEqual(target.weights_of("KeepMe"), {0: 0.5, 1: 0.25})
        # 源物体：被剔除的组按原权重还原（还原顺序即列表顺序：选中组留在原位）
        self.assertEqual(source.vertex_groups.names, ["G2", "G1", "G3"])
        self.assertEqual(source.weights_of("G1"), {0: 1.0})
        self.assertEqual(source.weights_of("G3"), {0: 0.25})

    def test_operator_runs_data_transfer_from_source_to_active_target(self):
        source = _FakeObject("Source", 2)
        source.set_group("G1", {0: 1.0})
        source.set_group("G2", {1: 1.0})
        target = _FakeObject("Target", 2)
        source.selected = True
        target.selected = True

        context = self._build_scene(
            [source, target], target, cleanup=True, selected_groups={"G1"}
        )
        self._run(context)

        self.assertEqual(len(_fake_object_ops.calls), 1)
        call = _fake_object_ops.calls[0]
        self.assertEqual(call["active"], "Target")
        self.assertEqual(call["selected"], ["Source", "Target"])
        self.assertIs(call["kwargs"]["use_reverse_transfer"], True)
        self.assertEqual(call["kwargs"]["data_type"], "VGROUP_WEIGHTS")
        self.assertEqual(call["kwargs"]["use_create"], True)
        self.assertEqual(call["kwargs"]["vert_mapping"], "POLYINTERP_NEAREST")
        # 反向传递下 layers_select_dst 展示的是「源图层」枚举（Blender
        # object_data_transfer.cc 的 dt_layers_select_itemf 会交换两组枚举项），
        # 因此 'ALL' 表示源物体全部顶点组，'NAME' 表示按名字匹配目标图层。
        self.assertEqual(call["kwargs"]["layers_select_src"], "NAME")
        self.assertEqual(call["kwargs"]["layers_select_dst"], "ALL")
        # 传递那一刻源物体上只剩选中的组
        self.assertEqual(call["source_groups"], ["G1"])
        # 剔除组后必须给源物体打标，否则算子读到未刷新的求值副本
        self.assertGreater(source.update_tag_calls, 0)

    def test_source_groups_restored_when_transfer_raises(self):
        source = _FakeObject("Source", 3)
        source.set_group("G1", {0: 1.0})
        source.set_group("G2", {1: 0.5, 2: 0.25})
        target = _FakeObject("Target", 3)
        target.set_group("KeepMe", {0: 0.4})
        source.selected = True
        target.selected = True

        context = self._build_scene(
            [source, target], target, cleanup=False, selected_groups={"G1"}
        )
        _fake_object_ops.fail_next = RuntimeError("data transfer failed")

        with self.assertRaises(RuntimeError):
            self._run(context)

        self.assertEqual(sorted(source.vertex_groups.names), ["G1", "G2"])
        self.assertEqual(source.weights_of("G2"), {1: 0.5, 2: 0.25})
        self.assertEqual(target.weights_of("KeepMe"), {0: 0.4})

    def test_stale_selection_cancels_and_restores_source(self):
        source = _FakeObject("Source", 2)
        source.set_group("G1", {0: 1.0})
        source.set_group("G2", {1: 0.5})
        target = _FakeObject("Target", 2)
        target.set_group("KeepMe", {0: 0.4})
        source.selected = True
        target.selected = True

        context = self._build_scene(
            [source, target], target, cleanup=False, selected_groups={"G2"}
        )
        context.scene.bmtp_props.wt_vertex_groups.append(_group_item("MissingGroup", True))
        for item in context.scene.bmtp_props.wt_vertex_groups:
            if item.name == "G2":
                item.selected = False

        result, reports = self._run(context)

        self.assertEqual(result, {"CANCELLED"})
        self.assertEqual(_fake_object_ops.calls, [])
        self.assertEqual(source.vertex_groups.names, ["G1", "G2"])
        self.assertEqual(target.weights_of("KeepMe"), {0: 0.4})
        self.assertTrue(any("没有可传递的顶点组" in str(message) for _level, message in reports))

    def test_no_selected_group_cancels_without_touching_target(self):
        source = _FakeObject("Source", 2)
        source.set_group("G1", {0: 1.0})
        target = _FakeObject("Target", 2)
        target.set_group("KeepMe", {0: 0.5})
        source.selected = True
        target.selected = True

        context = self._build_scene(
            [source, target], target, cleanup=True, selected_groups=set()
        )
        result, reports = self._run(context)

        self.assertEqual(result, {"CANCELLED"})
        self.assertEqual(_fake_object_ops.calls, [])
        self.assertEqual(target.vertex_groups.names, ["KeepMe"])
        self.assertTrue(any("至少选择一个顶点组" in str(message) for _level, message in reports))


if __name__ == "__main__":
    unittest.main()
