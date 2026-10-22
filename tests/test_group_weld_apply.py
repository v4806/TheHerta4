"""``common.group_weld_apply`` 的配置读取 / 物体名匹配单测（不需要 bpy）。

mesh 层焊接（配对、执行、回退）的测试见 ``tests/test_group_weld_mesh.py``。
"""

import types
import unittest

from common.group_weld_apply import (
    DEFAULT_WELD_THRESHOLD,
    SEAM_WELD_NODE_IDNAME,
    _collect_drawcall_signature,
    _drawcall_names,
    _match_units_for_name,
    _name_match_score,
    _normalize_object_name,
    collect_seam_weld_config,
)


def _object(name):
    return types.SimpleNamespace(object_name=name)


def _group(name, *objects):
    return types.SimpleNamespace(group_name=name, objects=[_object(item) for item in objects])


def _node(groups, threshold=1e-5):
    return types.SimpleNamespace(
        bl_idname=SEAM_WELD_NODE_IDNAME,
        weld_threshold=threshold,
        groups=list(groups),
    )


def _model(*nodes):
    return types.SimpleNamespace(postprocess_nodes=list(nodes))


class _FakeMKey:
    def __init__(self, key_name, tmp_value, is_swapkey=True, condition_operator="&&"):
        self.key_name = key_name
        self.tmp_value = tmp_value
        self.is_swapkey = is_swapkey
        self.condition_operator = condition_operator


class _FakeDrawCall:
    def __init__(self, name, work_key_list=None, source_obj_name="", alias=""):
        self.obj_name = name
        self.source_obj_name = source_obj_name
        self.comment_alias_name = alias
        self.work_key_list = list(work_key_list or [])

    def get_blender_obj_name(self):
        return self.obj_name


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
        self.assertAlmostEqual(
            collect_seam_weld_config(_model(node))["threshold"], DEFAULT_WELD_THRESHOLD
        )
        self.assertAlmostEqual(
            collect_seam_weld_config(
                _model(_node([_group("组1", "a", "b")], threshold=float("nan")))
            )["threshold"],
            DEFAULT_WELD_THRESHOLD,
        )
        self.assertAlmostEqual(
            collect_seam_weld_config(
                _model(_node([_group("组1", "a", "b")], threshold="bad"))
            )["threshold"],
            DEFAULT_WELD_THRESHOLD,
        )

    def test_node_without_groups_returns_empty_config(self):
        self.assertEqual(collect_seam_weld_config(_model(_node([])))["groups"], [])


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

    def test_match_units_for_name_returns_best_hits(self):
        units = [
            {"bucket": 0, "names": ["身体"]},
            {"bucket": 1, "names": ["身体.001"]},
            {"bucket": 2, "names": ["身体副本"]},
        ]
        hits = _match_units_for_name(units, "身体")
        self.assertEqual([unit["bucket"] for unit in hits], [0, 1])
        self.assertEqual(_match_units_for_name(units, "不存在"), [])


class DrawCallSignatureTests(unittest.TestCase):
    def test_empty_work_keys_is_resident(self):
        signature, weldable, reason = _collect_drawcall_signature(_FakeDrawCall("身体"))
        self.assertEqual(signature, {})
        self.assertTrue(weldable)
        self.assertEqual(reason, "")

    def test_swapkeys_build_signature(self):
        call = _FakeDrawCall(
            "脸1",
            [_FakeMKey("$swapkey3", 0), _FakeMKey("$swapkey4", 1)],
        )
        signature, weldable, _reason = _collect_drawcall_signature(call)
        self.assertEqual(signature, {"$swapkey3": 0, "$swapkey4": 1})
        self.assertTrue(weldable)

    def test_unjudgeable_operator_marks_unweldable(self):
        call = _FakeDrawCall(
            "脸1",
            [_FakeMKey("$swapkey3", 0), _FakeMKey("$swapkey4", 1, condition_operator="||")],
        )
        _signature, weldable, reason = _collect_drawcall_signature(call)
        self.assertFalse(weldable)
        self.assertIn("||", reason)

    def test_non_swapkey_is_ignored(self):
        call = _FakeDrawCall("身体", [_FakeMKey("$swapkey3", 0, is_swapkey=False)])
        signature, weldable, _reason = _collect_drawcall_signature(call)
        self.assertEqual(signature, {})
        self.assertTrue(weldable)

    def test_conflicting_values_mark_unweldable(self):
        call = _FakeDrawCall(
            "脸1", [_FakeMKey("$swapkey3", 0), _FakeMKey("$swapkey3", 1)]
        )
        _signature, weldable, reason = _collect_drawcall_signature(call)
        self.assertFalse(weldable)
        self.assertIn("冲突", reason)


class DrawCallNameTests(unittest.TestCase):
    def test_candidates_are_collected_and_deduplicated(self):
        call = _FakeDrawCall("身体", source_obj_name="Body", alias="身体")
        self.assertEqual(_drawcall_names(call, ""), ["身体", "Body"])
        self.assertEqual(_drawcall_names(None, "fallback"), ["fallback"])
        self.assertEqual(_drawcall_names(None, ""), [])


if __name__ == "__main__":
    unittest.main()
