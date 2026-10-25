"""身份自定义后缀（点号之后是显示名）的单测。

场景（实盘 2026-10-24）：物体被复制后 Blender 把名字改成
`LOD0.077983c9-10524-0.001`，导出按这个名字查工作空间提取数据 ⇒
「unique_str ... 没有找到对应的提取数据」。契约（用户裁决）：匹配只认身份前缀，
点号之后一律是自定义显示名 —— `LOD0.077983c9-10524-0.rthft001ygjyytfj中文` 的身份
同样是 `077983c9-10524-0`。

本文件钉住导出侧那半边：`ObjectPrefixHelper.parse_prefix_parts` 必须切掉显示名，
否则 `first_index` 会变成 `0.001`、`unique_str` 带上后缀；查数据侧的同一口径由
`tests/test_workspace_lod0_fallback.py` 钉（`WorkSpaceHelper.parse_lod_unique_str`）。
"""

import importlib.util
import sys
import types
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PKG = "identity_custom_suffix_test_pkg"

BARE = "aaaabbbb-100-0"


def _install_package(name):
    module = types.ModuleType(name)
    module.__path__ = []
    sys.modules[name] = module
    return module


def _load_module(qualname, path):
    spec = importlib.util.spec_from_file_location(qualname, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[qualname] = module
    spec.loader.exec_module(module)
    return module


sys.modules["bpy"] = types.SimpleNamespace(
    data=types.SimpleNamespace(objects=types.SimpleNamespace(get=lambda _name: None)),
)

for _name in (PKG, f"{PKG}.common", f"{PKG}.utils"):
    _install_package(_name)

_error_utils = types.ModuleType(f"{PKG}.utils.ssmt_error_utils")
_error_utils.SSMTErrorUtils = types.SimpleNamespace(raise_fatal=lambda message: None)
sys.modules[f"{PKG}.utils.ssmt_error_utils"] = _error_utils

_prefix_helper = _load_module(
    f"{PKG}.common.object_prefix_helper", REPO_ROOT / "common" / "object_prefix_helper.py"
)
ObjectPrefixHelper = _prefix_helper.ObjectPrefixHelper


class IdentityCustomSuffixTests(unittest.TestCase):
    def test_suffixed_name_yields_the_bare_identity_parts(self):
        cases = {
            f"LOD0.{BARE}": f"LOD0.{BARE}",
            f"LOD0.{BARE}.001": f"LOD0.{BARE}",
            f"LOD0.{BARE}.rthft001ygjyytfj中文": f"LOD0.{BARE}",
            f"{BARE}.001": BARE,
            f"{BARE}.my-note": BARE,
        }
        for name, expected_unique_str in cases.items():
            parts = ObjectPrefixHelper.parse_prefix_parts(name)
            self.assertEqual(parts["unique_str"], expected_unique_str, name)
            self.assertEqual(parts["bare_unique_str"], BARE, name)
            self.assertEqual(parts["draw_ib"], "aaaabbbb", name)
            self.assertEqual(parts["index_count"], "100", name)
            # 显示名绝不能漏进 first_index（否则 draw 偏移解析全错）。
            self.assertEqual(parts["first_index"], "0", name)

    def test_identity_without_suffix_is_unchanged(self):
        parts = ObjectPrefixHelper.parse_prefix_parts("ccccdddd-200-6030")
        self.assertEqual(parts["bare_unique_str"], "ccccdddd-200-6030")
        self.assertEqual(parts["first_index"], "6030")
        self.assertEqual(parts["lod_name"], "")


if __name__ == "__main__":
    unittest.main()
