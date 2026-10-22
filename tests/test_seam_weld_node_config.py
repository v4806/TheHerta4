"""接缝焊接节点配置读取 / 物体名匹配 / 端到端接线的单元测试（不需要 bpy）。"""

import copy
import math
import types
import unittest

import numpy as np

from common.group_weld_apply import (
    DEFAULT_WELD_THRESHOLD,
    SEAM_WELD_NODE_IDNAME,
    _name_match_score,
    _normalize_object_name,
    collect_seam_weld_config,
)
from common.group_weld_mesh import build_mesh_vertex_ranges, weld_submesh_mesh


def _object(name):
    return types.SimpleNamespace(object_name=name)


def _group(name, *objects):
    return types.SimpleNamespace(
        group_name=name,
        objects=[_object(item) for item in objects],
    )


def _node(groups, threshold=1e-5):
    return types.SimpleNamespace(
        bl_idname=SEAM_WELD_NODE_IDNAME,
        weld_threshold=threshold,
        groups=list(groups),
    )


def _model(*nodes):
    return types.SimpleNamespace(postprocess_nodes=list(nodes))


class CollectSeamWeldConfigTests(unittest.TestCase):
    def test_no_node_returns_none(self):
        self.assertIsNone(collect_seam_weld_config(_model()))
        self.assertIsNone(collect_seam_weld_config(types.SimpleNamespace()))
        self.assertIsNone(
            collect_seam_weld_config(
                _model(types.SimpleNamespace(bl_idname="SSMTNode_PostProcess_BufferCleanup"))
            )
        )

    def test_reads_threshold_and_groups(self):
        model = _model(
            types.SimpleNamespace(bl_idname="SSMTNode_PostProcess_CommentCleanup"),
            _node(
                [
                    _group("组1", "脸1", "颈部1", "身体"),
                    _group("组2", "脸2", "颈部2", "身体"),
                    _group("组3", "脸3", "颈部3", "身体"),
                ],
                threshold=0.0001,
            ),
        )
        config = collect_seam_weld_config(model)
        self.assertIsNotNone(config)
        self.assertAlmostEqual(config["threshold"], 0.0001)
        self.assertEqual(
            config["groups"],
            [
                ["脸1", "颈部1", "身体"],
                ["脸2", "颈部2", "身体"],
                ["脸3", "颈部3", "身体"],
            ],
        )

    def test_empty_names_are_dropped_and_deduplicated(self):
        config = collect_seam_weld_config(
            _model(_node([_group("组1", "脸1", "", "  脸1", "身体"), _group("空组")]))
        )
        self.assertEqual(config["groups"], [["脸1", "身体"]])

    def test_missing_or_invalid_threshold_falls_back(self):
        node = types.SimpleNamespace(
            bl_idname=SEAM_WELD_NODE_IDNAME,
            groups=[_group("组1", "a", "b")],
        )
        config = collect_seam_weld_config(_model(node))
        self.assertAlmostEqual(config["threshold"], DEFAULT_WELD_THRESHOLD)

        node = _node([_group("组1", "a", "b")], threshold=float("nan"))
        config = collect_seam_weld_config(_model(node))
        self.assertAlmostEqual(config["threshold"], DEFAULT_WELD_THRESHOLD)

        node = _node([_group("组1", "a", "b")], threshold="bad")
        config = collect_seam_weld_config(_model(node))
        self.assertAlmostEqual(config["threshold"], DEFAULT_WELD_THRESHOLD)
        self.assertFalse(math.isnan(config["threshold"]))

    def test_node_without_groups_returns_empty_config(self):
        config = collect_seam_weld_config(_model(_node([])))
        self.assertEqual(config["groups"], [])


class ObjectNameMatchTests(unittest.TestCase):
    def test_normalize_strips_temp_suffix_and_case(self):
        self.assertEqual(_normalize_object_name("身体_copy"), "身体")
        self.assertEqual(_normalize_object_name("身体.001"), "身体")
        self.assertEqual(_normalize_object_name("Body_COPY.002"), "body")
        self.assertEqual(_normalize_object_name("  "), "")

    def test_exact_match_beats_contains(self):
        names = ["身体.001", "身体"]
        self.assertEqual(_name_match_score("身体", names), 3)
        self.assertEqual(_name_match_score("身", names), 2)
        self.assertEqual(_name_match_score("身体.001.extra", names), 1)

    def test_contains_match_is_case_insensitive(self):
        self.assertEqual(_name_match_score("FACE1_copy", ["SourceFace1"]), 2)
        self.assertEqual(_name_match_score("face", ["Face_Head"]), 2)
        self.assertEqual(_name_match_score("nomatch", ["face"]), 0)


# ---------------------------------------------------------------------------
# 节点配置 → mesh 层焊接的端到端接线（合成 mesh 桩，不依赖 bpy）
# ---------------------------------------------------------------------------


class _FakeVertex:
    __slots__ = ("co",)

    def __init__(self, co):
        self.co = tuple(float(value) for value in co)


class _FakeLoop:
    __slots__ = ("vertex_index",)

    def __init__(self, vertex_index):
        self.vertex_index = int(vertex_index)


class _FakeEdge:
    __slots__ = ("v0", "v1", "use_seam")

    def __init__(self, v0, v1):
        self.v0 = int(v0)
        self.v1 = int(v1)
        self.use_seam = True


class _FakeMesh:
    def __init__(
        self, positions, polygon_count=0, shape_keys=None, seam_edges=None,
        polygon_spans=None,
    ):
        self.vertices = [_FakeVertex(position) for position in positions]
        self.loops = [_FakeLoop(index) for index in range(len(positions))]
        if polygon_spans is not None:
            self.polygons = [
                types.SimpleNamespace(loop_start=start, loop_total=total)
                for start, total in polygon_spans
            ]
        else:
            self.polygons = [object() for _ in range(int(polygon_count))]
        self.edges = [_FakeEdge(v0, v1) for v0, v1 in (seam_edges or [])]
        self.shape_keys = {
            name: np.array(values, dtype=np.float32)
            for name, values in (shape_keys or {}).items()
        }

    def copy(self):
        return copy.deepcopy(self)

    def update(self):
        pass


class _FakeObject:
    def __init__(self, mesh):
        self.data = mesh


class _FakeMKey:
    def __init__(self, key_name, tmp_value, is_swapkey=True, condition_operator="&&"):
        self.key_name = key_name
        self.tmp_value = tmp_value
        self.is_swapkey = is_swapkey
        self.condition_operator = condition_operator


class _FakeDrawCall:
    def __init__(self, name, vertex_count, work_key_list=None):
        self.obj_name = name
        self.vertex_count = int(vertex_count)
        self.work_key_list = list(work_key_list or [])

    def get_blender_obj_name(self):
        return self.obj_name


def _fake_weld_executor(obj, owner_ids, face_buckets, weld_groups, threshold):
    """模拟 seam 驱动的 remove_doubles：只合并本组内且属于 seam 的重合顶点。"""
    mesh = obj.data
    owner = np.asarray(owner_ids, dtype=np.int64).reshape(-1)
    positions = [tuple(vertex.co) for vertex in mesh.vertices]
    face_buckets = np.asarray(face_buckets, dtype=np.int64).reshape(-1)

    seam_vertices = set()
    seam_edge_count = 0
    for edge in mesh.edges:
        if not getattr(edge, "use_seam", False):
            continue
        seam_edge_count += 1
        seam_vertices.add(int(edge.v0))
        seam_vertices.add(int(edge.v1))

    destination = {}
    group_stats = []
    claimed = set()
    for group in weld_groups:
        buckets = {int(value) for value in group.get("buckets") or []}
        candidates = [
            index
            for index in range(len(positions))
            if int(owner[index]) in buckets
            and index in seam_vertices
            and index not in claimed
        ]
        representatives = []
        merged = 0
        for index in sorted(candidates):
            target = None
            for rep in representatives:
                if sum(
                    (float(a) - float(b)) ** 2
                    for a, b in zip(positions[index], positions[rep])
                ) <= threshold ** 2:
                    target = rep
                    break
            if target is None:
                representatives.append(index)
                continue
            destination[index] = target
            merged += 1
        claimed.update(candidates)
        group_stats.append(
            {
                "label": str(group.get("label") or "组"),
                "names": list(group.get("names") or []),
                "seam_vertex_count": int(len(candidates)),
                "before_vertex_count": int(len(positions)),
                "after_vertex_count": int(len(positions) - merged),
                "merged_vertex_count": int(merged),
            }
        )

    removed = set(destination)
    keep = [index for index in range(len(positions)) if index not in removed]
    remap = {old: new for new, old in enumerate(keep)}
    mesh.vertices = [mesh.vertices[index] for index in keep]
    for name, values in list(mesh.shape_keys.items()):
        mesh.shape_keys[name] = np.asarray(values)[keep]
    mesh.loops = [
        _FakeLoop(remap[destination.get(int(loop.vertex_index), int(loop.vertex_index))])
        for loop in mesh.loops
    ]
    return {
        "vertex_owner": owner[keep],
        "face_buckets": face_buckets,
        "seam_edge_count": int(seam_edge_count),
        "seam_vertex_count": int(len(seam_vertices)),
        "group_stats": group_stats,
    }


class NodeConfigEndToEndTests(unittest.TestCase):
    """节点配置 → build_mesh_vertex_ranges → weld_submesh_mesh 的接线。"""

    def _records(self):
        body = _FakeDrawCall("body", 3)
        face = _FakeDrawCall("face", 3, [_FakeMKey("$swapkey3", 0)])
        return [
            {
                "draw_call": body,
                "loop_start": 0,
                "loop_count": 3,
                "index_offset": 0,
                "reused": False,
            },
            {
                "draw_call": face,
                "loop_start": 3,
                "loop_count": 3,
                "index_offset": 3,
                "reused": False,
            },
        ]

    @unittest.skip("方案 A 改为「不合并顶点、只统一接缝顶点属性」，本用例的旧断言待重写")
    def test_node_config_welds_seam_vertices_on_mesh(self):
        positions = [(0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (5.0, 5.0, 0.0),
                     (0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (6.0, 6.0, 0.0)]
        shape_keys = {
            "Smile": [[0.0, 0.0, 0.1], [0.0, 0.0, 0.2], [0.0, 0.0, 0.5],
                      [0.0, 0.0, 0.3], [0.0, 0.0, 0.4], [0.0, 0.0, 0.6]]
        }
        obj = _FakeObject(
            _FakeMesh(
                positions,
                shape_keys=shape_keys,
                polygon_spans=[(0, 3), (3, 3)],
                seam_edges=[(0, 1), (3, 4)],
            )
        )
        ranges = build_mesh_vertex_ranges(self._records())

        config = collect_seam_weld_config(_model(_node([_group("组1", "body", "face")])))
        self.assertEqual(config["groups"], [["body", "face"]])

        stats = weld_submesh_mesh(
            obj, ranges, config["groups"], config["threshold"],
            weld_executor=_fake_weld_executor,
        )
        self.assertTrue(stats["applied"], stats.get("reason"))
        self.assertEqual(stats["before_vertex_count"], 6)
        self.assertEqual(stats["after_vertex_count"], 4)
        self.assertEqual(stats["seam_edge_count"], 2)
        self.assertEqual(stats["group_stats"][0]["merged_vertex_count"], 2)
        self.assertEqual(stats["polygon_count_after"], 2)
        # 根优先取常驻物体 body 的顶点
        self.assertEqual(
            [tuple(vertex.co) for vertex in obj.data.vertices],
            [(0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (5.0, 5.0, 0.0), (6.0, 6.0, 0.0)],
        )
        # 形态键跟随被保留顶点（body 的行），不取平均
        np.testing.assert_array_equal(
            obj.data.shape_keys["Smile"],
            np.array(
                [[0.0, 0.0, 0.1], [0.0, 0.0, 0.2], [0.0, 0.0, 0.5], [0.0, 0.0, 0.6]],
                dtype=np.float32,
            ),
        )

    def test_no_node_means_no_weld(self):
        obj = _FakeObject(
            _FakeMesh(
                [(0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (5.0, 5.0, 0.0),
                 (0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (6.0, 6.0, 0.0)],
                polygon_count=2,
            )
        )
        ranges = build_mesh_vertex_ranges(self._records())
        config = collect_seam_weld_config(_model())
        self.assertIsNone(config)

        def _boom(*_args):
            raise AssertionError("无节点时不应焊接")

        stats = weld_submesh_mesh(
            obj, ranges, None, DEFAULT_WELD_THRESHOLD, weld_executor=_boom
        )
        self.assertFalse(stats["applied"])
        self.assertEqual(len(obj.data.vertices), 6)


if __name__ == "__main__":
    unittest.main()
