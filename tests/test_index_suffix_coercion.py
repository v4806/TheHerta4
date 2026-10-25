"""索引字段：Blender 去重后缀 `.001`~`.999` 的剥离与 `int` 收敛。

钉住三件事（都落在 `common/object_prefix_helper.py`，其它模块只许调用、不许再写一份）：

1. 名字解析入口先剥**结尾**后缀再切段，所以 `first_index` 拿到的是 `"0"` 而不是
   `"0.001"`——用户复制物体、或「快速局部导出」重跑时 Blender 会给对象名自动追加
   这个后缀（`.001` 是序号，不是索引的一部分）。
2. 后缀落在别的段上（`index_count` 段、显示名段、没有前缀的名字）时不误伤别的段。
3. `coerce_index` 是唯一允许把索引字段转成 `int` 的地方：整数文本、零小数浮点、
   带后缀的整数文本放行；其余抛带字段名与对象身份的 `IndexValueError`，
   **不静默当 0**。

加载方式沿用 `tests/test_object_prefix_helper.py`：无真实 bpy 环境下用 stub 包按文件
加载真实模块（根包在 `tests/conftest.py` 里已被 stub 成 `__path__=[]`）。
"""

from __future__ import annotations

import importlib.util
import sys
import types
import unittest
from pathlib import Path


if "bpy" not in sys.modules:
    sys.modules["bpy"] = types.SimpleNamespace(
        data=types.SimpleNamespace(objects={}),
    )

_UTILS_MODULE = types.ModuleType("TheHerta4.utils.ssmt_error_utils")
_UTILS_MODULE.SSMTErrorUtils = types.SimpleNamespace(
    raise_fatal=lambda message: (_ for _ in ()).throw(RuntimeError(message)),
)
sys.modules.setdefault("TheHerta4", types.ModuleType("TheHerta4"))
sys.modules.setdefault("TheHerta4.common", types.ModuleType("TheHerta4.common"))
sys.modules.setdefault("TheHerta4.utils", types.ModuleType("TheHerta4.utils"))
sys.modules["TheHerta4.utils.ssmt_error_utils"] = _UTILS_MODULE

_MODULE_PATH = Path(__file__).resolve().parents[1] / "common" / "object_prefix_helper.py"
_SPEC = importlib.util.spec_from_file_location(
    "TheHerta4.common.object_prefix_helper_index_tests",
    _MODULE_PATH,
)
_OBJECT_PREFIX_MODULE = importlib.util.module_from_spec(_SPEC)
sys.modules["TheHerta4.common.object_prefix_helper_index_tests"] = _OBJECT_PREFIX_MODULE
_SPEC.loader.exec_module(_OBJECT_PREFIX_MODULE)

ObjectPrefixHelper = _OBJECT_PREFIX_MODULE.ObjectPrefixHelper
coerce_index = _OBJECT_PREFIX_MODULE.coerce_index
IndexValueError = _OBJECT_PREFIX_MODULE.IndexValueError
strip_blender_duplicate_suffix = _OBJECT_PREFIX_MODULE.strip_blender_duplicate_suffix
split_blender_duplicate_suffix = _OBJECT_PREFIX_MODULE.split_blender_duplicate_suffix


def _first_index(object_name: str) -> str:
    prefix_info = ObjectPrefixHelper.extract_prefix_info(object_name)
    assert prefix_info is not None, f"解析不出前缀：{object_name!r}"
    return ObjectPrefixHelper.parse_prefix_parts(prefix_info[0])["first_index"]


class BlenderDuplicateSuffixTests(unittest.TestCase):
    def test_split_returns_name_and_suffix(self):
        self.assertEqual(split_blender_duplicate_suffix("077983c9-10524-0.001"), ("077983c9-10524-0", ".001"))

    def test_strip_removes_trailing_duplicate_suffix(self):
        self.assertEqual(strip_blender_duplicate_suffix("0.001"), "0")
        self.assertEqual(strip_blender_duplicate_suffix("LOD0.077983c9-10524-0.012"), "LOD0.077983c9-10524-0")

    def test_strip_leaves_two_digit_fraction_alone(self):
        # `.05` 只有两位，不是 Blender 的去重后缀（去重后缀至少三位）
        self.assertEqual(strip_blender_duplicate_suffix("abc-1-0.05"), "abc-1-0.05")

    def test_strip_is_single_layer_and_idempotent_value(self):
        # 连着复制两次得到 `.001.002`；只剥最后一层，剥完再调不会继续吃名字
        once = strip_blender_duplicate_suffix("abc-1-0.001.002")
        self.assertEqual(once, "abc-1-0.001")
        self.assertEqual(strip_blender_duplicate_suffix(once), "abc-1-0")

    def test_strip_keeps_name_without_suffix(self):
        self.assertEqual(strip_blender_duplicate_suffix("LOD0.077983c9-10524-0"), "LOD0.077983c9-10524-0")

    def test_class_helper_delegates_to_module_level_rule(self):
        self.assertEqual(ObjectPrefixHelper._strip_blender_suffix("x.001"), ("x", ".001"))


class FirstIndexFromObjectNameTests(unittest.TestCase):
    def test_duplicated_object_name_keeps_integer_first_index(self):
        # 用户复制 `LOD0.077983c9-10524-0` 之后 Blender 给出 `…-0.001`
        self.assertEqual(ObjectPrefixHelper.extract_prefix_info("LOD0.077983c9-10524-0.001"), ("LOD0.077983c9-10524-0", "."))
        self.assertEqual(_first_index("LOD0.077983c9-10524-0.001"), "0")

    def test_duplicated_name_without_lod_prefix(self):
        self.assertEqual(ObjectPrefixHelper.extract_prefix_info("077983c9-10524-0.001"), ("077983c9-10524-0", "."))
        self.assertEqual(_first_index("077983c9-10524-0.001"), "0")

    def test_suffix_on_display_name_does_not_touch_first_index(self):
        self.assertEqual(_first_index("LOD0.077983c9-10524-0.Face.001"), "0")

    def test_suffix_on_index_count_segment_keeps_that_segment_and_empty_first_index(self):
        prefix_info = ObjectPrefixHelper.extract_prefix_info("LOD0.077983c9-10524.001")

        self.assertEqual(prefix_info, ("LOD0.077983c9-10524", "."))
        prefix_parts = ObjectPrefixHelper.parse_prefix_parts(prefix_info[0])
        self.assertEqual(prefix_parts["draw_ib"], "077983c9")
        self.assertEqual(prefix_parts["index_count"], "10524")
        self.assertEqual(prefix_parts["first_index"], "")

    def test_plain_numeric_first_index_is_unchanged(self):
        self.assertEqual(_first_index("LOD0.077983c9-10524-57792"), "57792")

    def test_missing_first_index_segment_stays_empty(self):
        self.assertEqual(_first_index("LOD0.077983c9-10524"), "")

    def test_name_without_prefix_is_not_given_a_first_index(self):
        self.assertIsNone(ObjectPrefixHelper.extract_prefix_info("Mesh.001"))
        prefix_parts = ObjectPrefixHelper.parse_prefix_parts("Mesh.001")

        self.assertEqual(prefix_parts["draw_ib"], "Mesh")
        self.assertEqual(prefix_parts["first_index"], "")

    def test_illegal_text_is_reported_by_coerce_not_by_parser(self):
        # 解析层只负责切段（`Abc` 不是数字也不该被改成 0），判定整数在 coerce_index
        self.assertEqual(_first_index("LOD0.077983c9-10524-Abc"), "Abc")
        with self.assertRaises(IndexValueError):
            coerce_index(_first_index("LOD0.077983c9-10524-Abc"), field="match_first_index")

    def test_crash_site_regression_duplicated_stub_name(self):
        # 「快速局部导出」现场：站位对象名被 Blender 去重 → 下游 int() 报
        # ValueError: invalid literal for int() with base 10: '0.001'
        stub_name = "LOD0.077983c9-10524-0.001"

        self.assertEqual(coerce_index(_first_index(stub_name), field="match_first_index"), 0)


class CoerceIndexTests(unittest.TestCase):
    def test_accepts_integers(self):
        self.assertEqual(coerce_index(7), 7)
        self.assertEqual(coerce_index(-3), -3)

    def test_accepts_integer_text(self):
        self.assertEqual(coerce_index("0"), 0)
        self.assertEqual(coerce_index("57792"), 57792)
        self.assertEqual(coerce_index(" 42 "), 42)
        self.assertEqual(coerce_index("+42"), 42)

    def test_accepts_zero_fraction_floats(self):
        # json / numpy 会把整数写成浮点，元数据里回来的是 10524.0
        self.assertEqual(coerce_index(10524.0), 10524)
        self.assertEqual(coerce_index("10524.0"), 10524)
        self.assertEqual(coerce_index("0.00"), 0)

    def test_accepts_duplicate_suffix_text(self):
        self.assertEqual(coerce_index("0.001"), 0)
        self.assertEqual(coerce_index("57792.003"), 57792)
        self.assertEqual(coerce_index(" -12.001 "), -12)

    def test_rejects_fractional_float(self):
        for value in (12.5, "12.5", "0.001.5"):
            with self.subTest(value=value):
                with self.assertRaises(IndexValueError):
                    coerce_index(value, field="match_first_index", context="LOD0.077983c9-10524-0")

    def test_rejects_non_numeric_text(self):
        for value in ("abc", "0x10", "1e3", "12 34"):
            with self.subTest(value=value):
                with self.assertRaises(IndexValueError):
                    coerce_index(value, field="index_count")

    def test_rejects_empty_none_and_bool(self):
        for value in ("", "   ", None, True, False):
            with self.subTest(value=value):
                with self.assertRaises(IndexValueError):
                    coerce_index(value, field="match_first_index")

    def test_error_message_names_field_and_object(self):
        with self.assertRaises(IndexValueError) as caught:
            coerce_index("Abc", field="match_first_index", context="LOD0.077983c9-10524-Abc")

        message = str(caught.exception)
        self.assertIn("match_first_index", message)
        self.assertIn("LOD0.077983c9-10524-Abc", message)
        self.assertIn("'Abc'", message)
        # 告诉读者 `.001` 不是数据，而是名字后缀
        self.assertIn(".001", message)

    def test_error_is_a_value_error_for_existing_guards(self):
        # 既有兜底点写的是 `except (TypeError, ValueError)`，语义必须保持不变
        self.assertTrue(issubclass(IndexValueError, ValueError))

        try:
            coerce_index("Abc", field="match_first_index")
        except (TypeError, ValueError):
            pass
        else:  # pragma: no cover - 只有在 IndexValueError 不再继承 ValueError 时才会走到
            self.fail("coerce_index 抛出的错必须能被 except (TypeError, ValueError) 接住")


if __name__ == "__main__":
    unittest.main()
