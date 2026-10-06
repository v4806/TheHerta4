import unittest

from common.zzmi_merged_contract import evaluate_merged_skeleton_contract


class ZZMIContractTests(unittest.TestCase):
    def test_no_metadata_is_informational(self):
        result = evaluate_merged_skeleton_contract(
            checkbox_enabled=True,
            parts_with_data=0,
            component_count=0,
        )
        self.assertEqual(result["level"], "notice")

    def test_disabled_checkbox_with_cached_data_is_plain_export(self):
        """关闭复选框 = 普通导出（几何按部件局部编号），残留缓存不构成错误。

        缓存由导入侧无条件落盘（生成侧/消费侧分离），与用户是否开过该开关
        无关；旧实现把「关闭 + 有缓存」当成致命错误，会误伤普通导出，且它
        给出的「关闭开关后重新导入」出路也不成立（重导入不清理既有缓存）。
        """
        result = evaluate_merged_skeleton_contract(
            checkbox_enabled=False,
            parts_with_data=2,
            component_count=0,
        )
        self.assertEqual(result["level"], "notice")
        self.assertIn("普通导出", result["message"])
        self.assertEqual(result["hint"], "")

    def test_disabled_checkbox_with_global_geometry_is_error(self):
        """几何**确实**使用全局骨骼编号 ⇒ 仍然中止：这是「导入时开着开关、

        导出前把开关关掉」的真危险场景（导出的几何没有运行时合并骨架）。
        判据由调用方从顶点组编号空间读出，缓存有无不再参与判定。
        """
        result = evaluate_merged_skeleton_contract(
            checkbox_enabled=False,
            parts_with_data=2,
            parts_with_global_ids=1,
            component_count=0,
        )
        self.assertEqual(result["level"], "error")
        self.assertIn("确实", result["message"])
        self.assertIn("重新一键导入", result["hint"])

    def test_rejected_all_components_is_error_and_lists_reason(self):
        result = evaluate_merged_skeleton_contract(
            checkbox_enabled=True,
            parts_with_data=1,
            component_count=0,
            skip_reasons={"8c8de427": "VGMap 缓存版本过旧"},
        )
        self.assertEqual(result["level"], "error")
        self.assertIn("8c8de427", result["hint"])

    def test_partial_components_is_warning(self):
        result = evaluate_merged_skeleton_contract(
            checkbox_enabled=True,
            parts_with_data=3,
            component_count=2,
            skip_reasons={"8c8de427": "缺少 DeformDrawIndex"},
        )
        self.assertEqual(result["level"], "warning")
        self.assertIn("8c8de427", result["hint"])


if __name__ == "__main__":
    unittest.main()

