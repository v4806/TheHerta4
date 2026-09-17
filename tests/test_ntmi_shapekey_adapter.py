import ast
import importlib.util
import inspect
import sys
import types
import unittest
from pathlib import Path

from tests import _real_modules


def _install_module(name, **attrs):
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


PKG = "_ntmi_shapekey_adapter_test_pkg"
for package_name in (
    PKG,
    f"{PKG}.blueprint",
    f"{PKG}.common",
    f"{PKG}.utils",
    f"{PKG}.ui",
    f"{PKG}.ui.ntmi_modimp",
):
    package = _install_module(package_name)
    package.__path__ = []

# 真实 common 子模块按 fake 包前缀注册（空 __path__ 假包解析不了相对导入）
_real_modules.register_real_common_modules(f"{PKG}.common")


_install_module("bpy", data=types.SimpleNamespace(objects={}))
_install_module(f"{PKG}.blueprint.direct_export_shapekey", DirectShapeKeyGenerator=type("DirectShapeKeyGenerator", (), {}))
_install_module(
    f"{PKG}.blueprint.direct_export_shapekey_shared",
    ShapeKeyDirectExportError=RuntimeError,
)
_install_module(
    f"{PKG}.common.d3d11_gametype",
    D3D11GameType=type("D3D11GameType", (), {}),
)
_install_module(
    f"{PKG}.utils.log_utils",
    LOG=types.SimpleNamespace(info=lambda *args, **kwargs: None, warning=lambda *args, **kwargs: None),
)
_install_module(
    f"{PKG}.blueprint.ntmi_layout_adapter",
    iter_name_variants=lambda name: [name],
    local_loop_indices_for_export_range=lambda *args, **kwargs: [],
    parse_ntmi_part_layouts=lambda *args, **kwargs: {},
)
_install_module(
    f"{PKG}.ui.ntmi_modimp.modimp_core",
    ensure_mod_importer_package=lambda *args, **kwargs: types.SimpleNamespace(__name__="fake_modimp"),
)
_install_module(
    f"{PKG}.ui.ntmi_modimp.ntemi_importer",
    _ensure_ntemi_game_data_converter=lambda *args, **kwargs: None,
)


module_path = Path(__file__).resolve().parents[1] / "blueprint" / "ntmi_shapekey.py"
spec = importlib.util.spec_from_file_location(f"{PKG}.blueprint.ntmi_shapekey", module_path)
ntmi_shapekey = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = ntmi_shapekey
spec.loader.exec_module(ntmi_shapekey)


class NTMIShapeKeyAdapterTests(unittest.TestCase):
    def test_adapter_forwards_compute_dispatch_group_count_to_original_node(self):
        class _OriginalNode:
            def _compute_dispatch_group_count(self, vertex_count, threads_per_group=16):
                vertex_count = int(vertex_count or 0)
                threads_per_group = max(1, int(threads_per_group or 1))
                return max(1, (vertex_count + threads_per_group - 1) // threads_per_group)

        adapter = ntmi_shapekey.NTMIShapeKeyNodeAdapter(
            original_node=_OriginalNode(),
            sections={},
            mod_export_path=".",
            ini_path="./mod.ini",
        )

        self.assertEqual(adapter._compute_dispatch_group_count(17, threads_per_group=16), 2)


REPO_ROOT = Path(__file__).resolve().parents[1]


def _production_update_shader_file_call_shapes():
    """取出 `blueprint/direct_export_shapekey.py` 里 `_update_shader_file` 调用点的实参形态。

    直接解析生产源码，而不是在这里硬编码一份实参清单：调用点新增实参、而被调方
    （NTMI 下 `self.node` 就是 `NTMIShapeKeyNodeAdapter`）没跟上，正是 F1 的形态。
    返回 ``[(位置实参个数, [关键字实参名, ...]), ...]``。
    """
    source_path = REPO_ROOT / "blueprint" / "direct_export_shapekey.py"
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "_update_shader_file"
    ]
    return [
        (len(call.args), [keyword.arg for keyword in call.keywords if keyword.arg])
        for call in calls
    ]


class NTMIUpdateShaderFileSignatureTests(unittest.TestCase):
    """F1 回归：生产调用点的实参集合必须 ⊆ NTMI 适配器签名。

    崩铁路径上 `DirectShapeKeyGenerator.generate()` 的 `self.node` 就是
    `NTMIShapeKeyNodeAdapter`；调用点传了适配器不接受的 `source_path=` 时，整个
    直出形态键导出会抛 `TypeError`。这里把「调用点实参」与「被调方签名」直接对账，
    任何一侧单独变动都会失败——而不是只重跑一遍 happy path。
    """

    def test_adapter_signature_covers_the_production_call_site(self):
        signature = inspect.signature(
            ntmi_shapekey.NTMIShapeKeyNodeAdapter._update_shader_file
        )
        accepted = sorted(signature.parameters)
        call_shapes = _production_update_shader_file_call_shapes()
        self.assertTrue(
            call_shapes,
            "未在 blueprint/direct_export_shapekey.py 里找到 _update_shader_file 调用点"
            "（生产契约变了，本回归测试需要同步更新）",
        )

        for positional, keywords in call_shapes:
            with self.subTest(positional=positional, keywords=keywords):
                unknown = sorted(set(keywords) - set(accepted))
                self.assertEqual(
                    unknown,
                    [],
                    "生产调用点传了 NTMI 适配器不接受的实参 "
                    f"{unknown}；适配器签名={accepted}。崩铁直出形态键导出会因此抛 "
                    "TypeError，需给适配器补上同名形参（或让调用点不再传）。",
                )
                # 形状也要真能绑定：必需形参齐全、位置实参个数不超限。
                signature.bind(
                    object(),
                    *[object()] * positional,
                    **{name: None for name in keywords},
                )


if __name__ == "__main__":
    unittest.main()
