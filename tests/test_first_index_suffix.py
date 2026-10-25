"""`first_index` 被 Blender 重名后缀污染时不得打死导出。

物体名 `LOD0.077983c9-10524-0.001` 的第三段会解析出 `0.001`；旧代码在
`DrawIBModel._load_import_metadata_from_first_submesh` 里直接 `int()`，
整轮快速局部导出抛 `ValueError: invalid literal for int() with base 10: '0.001'`。

这里不导入整个插件包（根 `__init__.py` 依赖真实 bpy），而是按仓库既有
fake-PKG 模式把真实模块装到假包下逐个加载。
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
PKG = "_first_index_suffix_test_pkg"


def _install_module(name: str, **attrs):
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


class _AnyBpyModule(types.ModuleType):
    """任何属性都返回一个占位类，够被 import 时的类型标注/注册使用即可。"""

    def __getattr__(self, item):  # noqa: D105
        placeholder = type(item, (), {})
        setattr(self, item, placeholder)
        return placeholder


class _BpyFinder:
    def find_module(self, fullname, path=None):  # noqa: D102 - 旧协议，仅为兼容
        return None

    def find_spec(self, fullname, path=None, target=None):  # noqa: D102
        if fullname != "bpy" and not fullname.startswith("bpy."):
            return None
        return importlib.util.spec_from_loader(fullname, self)

    def create_module(self, spec):  # noqa: D102
        return _AnyBpyModule(spec.name)

    def exec_module(self, module):  # noqa: D102
        return None


class _RepoFinder:
    """把 `<PKG>.common.foo` 解析成仓库里的 `common/foo.py`，相对 import 自然成立。"""

    def find_spec(self, fullname, path=None, target=None):
        if not fullname.startswith(PKG + "."):
            return None
        relative = fullname[len(PKG) + 1:].replace(".", "/")
        as_module = REPO_ROOT / (relative + ".py")
        as_package = REPO_ROOT / relative / "__init__.py"
        if as_module.is_file():
            return importlib.util.spec_from_file_location(fullname, as_module)
        if as_package.is_file():
            spec = importlib.util.spec_from_file_location(
                fullname, as_package, submodule_search_locations=[str(as_package.parent)]
            )
            return spec
        # 包里没有的模块（如 ui.* 的重依赖）用占位模块顶住
        return importlib.util.spec_from_loader(fullname, _StubLoader())


class _StubLoader:
    def create_module(self, spec):
        module = types.ModuleType(spec.name)
        module.__path__ = []
        return module

    def exec_module(self, module):
        module.__getattr__ = lambda item: type(item, (), {})  # 占位


def _load_real(module_name: str):
    """把仓库里的真实模块以 `<PKG>.<pkg>.<name>` 装上（相对 import 走 _RepoFinder）。"""
    if not any(isinstance(finder, _RepoFinder) for finder in sys.meta_path):
        sys.meta_path.insert(0, _RepoFinder())
    return importlib.import_module(f"{PKG}.{module_name}")


def _prepare_package_tree() -> None:
    if PKG in sys.modules:
        return
    if not any(isinstance(finder, _BpyFinder) for finder in sys.meta_path):
        sys.meta_path.insert(0, _BpyFinder())
    for package_name in (PKG, f"{PKG}.common", f"{PKG}.utils"):
        package = _install_module(package_name)
        package.__path__ = []
    # 任何属性都返回占位类：插件模块在 import 期会碰 bpy.types/bpy.props 等一大票名字
    bpy_module = _AnyBpyModule("bpy")
    bpy_module.__path__ = []
    bpy_module.data = types.SimpleNamespace(objects={}, filepath="")
    sys.modules["bpy"] = bpy_module


_prepare_package_tree()

draw_call_model = _load_real("common.draw_call_model")

@pytest.mark.parametrize(
    "object_name, expected_first_index",
    [
        ("LOD0.077983c9-10524-0", "0"),
        ("LOD0.077983c9-10524-0.001", "0"),
        ("077983c9-10524-0.001", "0"),
        ("LOD0.077983c9-10524-57792", "57792"),
    ],
)
def test_first_index_drops_display_suffix(object_name: str, expected_first_index: str) -> None:
    model = draw_call_model.DrawCallModel(obj_name=object_name)
    assert model.match_first_index == expected_first_index


def test_drawib_model_goes_through_tolerant_coercion() -> None:
    """崩溃点 `int(submesh_model.match_first_index)` 必须换成容错归一。

    `common/drawib_model.py` 依赖整个插件（含真实 bpy），这里做源码级护栏：
    只要裸 `int(match_first_index)` 又回到装配 Part 表的循环里，就该红。
    """
    source = (REPO_ROOT / "common" / "drawib_model.py").read_text(encoding="utf-8")
    assert "int(submesh_model.match_first_index)" not in source
    assert "_coerce_first_index(" in source
    assert "小数文本" in source
