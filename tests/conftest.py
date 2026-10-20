"""pytest 9.x 下从仓库根目录跑测试的收集器兼容垫片。

仓库根 ``__init__.py`` 是真实 Blender 插件包入口（依赖真实 bpy）；pytest 9
（importlib 包前缀收集）会把根目录视为 ``Package`` 收集器，并在测试 setup 时
导入 ``TheHerta4/__init__.py``——无真实 bpy 环境下直接崩在
``class GlobalProterties(bpy.types.PropertyGroup)``。

本 conftest 在收集阶段预注册根包为只读 stub，令 pytest 的包级 setup 命中
``sys.modules`` 而不执行插件真实初始化。测试自身通过各自的 fake-PKG 前缀
加载真实模块（``tests/test_efmi_*`` 等既有模式），不依赖根包被导入；
从 ``tests/`` 目录内跑（旧式 ``cd tests && pytest``）时根包不被导入，此垫片无副作用。

另有第二个垫片：把仓库根加入 ``sys.path``（**追加**，不抢占 ``tests/`` 自身的
优先级）。原因是有测试用 ``from tests.<module> import ...`` 共享辅助函数
（``test_drag_feature_switches.py`` / ``test_drag_present_order.py``），而
``python -m pytest <单个文件>`` 从 ``tests/`` 内跑时 ``sys.path`` 里只有
``tests/`` 自己 —— 包前缀 ``tests.`` 解析不到，收集期直接
``ModuleNotFoundError: No module named 'tests'``（整文件 ERROR、退出码 2）。
追加仓库根后两种跑法（``pytest .`` 与 ``pytest <file>``）都能收集。
"""

import os
import sys
import types

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.append(_REPO_ROOT)

# 根目录的包名就是目录名（本仓库为 "TheHerta4-main"），pytest 9 的 Package
# 收集器会用这个名字在 setup 阶段导入根 ``__init__.py``；旧垫片只注册了
# "TheHerta4"，与真实目录名不符，从仓库根目录跑会崩在相对 import。
_ROOT_PACKAGE_NAMES = {
    "TheHerta4",
    os.path.basename(os.path.dirname(os.path.abspath(__file__))),
    # pytest 9 的 Package.setup 会把根目录的 ``__init__.py`` 以顶层名
    # "__init__" 交给 importlib（实测），也要用同一个 stub 拦住。
    "__init__",
}
for _package_name in _ROOT_PACKAGE_NAMES:
    if _package_name and _package_name not in sys.modules:
        _root_pkg_stub = types.ModuleType(_package_name)
        # 仅满足 pytest Package.setup 的 importtestmodule；任何真实代码路径
        # 都不会读到它（真实模块以 PKG 前缀加载）。
        _root_pkg_stub.__file__ = __file__
        _root_pkg_stub.__path__ = []
        sys.modules[_package_name] = _root_pkg_stub
