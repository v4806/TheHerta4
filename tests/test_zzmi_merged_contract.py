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

    def test_disabled_checkbox_with_global_data_is_error(self):
        result = evaluate_merged_skeleton_contract(
            checkbox_enabled=False,
            parts_with_data=2,
            component_count=0,
        )
        self.assertEqual(result["level"], "error")
        self.assertIn("重新导入", result["hint"])

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

