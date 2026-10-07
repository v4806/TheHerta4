"""bpy.app.handlers 回调调用约定的回归护栏。

背景（PR #27 修的那一类缺陷）：Blender 5.x 触发 load_post / save_post /
depsgraph_update_post 时会传入 **2 个** 实参（scene, depsgraph），而不少回调只声明了
一个甚至零个参数，于是控制台刷：

    Error in bpy.app.handlers.load_post[N]: TypeError: xxx_handler() takes 1
    positional argument but 2 were given

关键点：这个 TypeError 发生在**进入函数体之前**，函数内部的 try/except 兜不住，只有
对应事件真的被触发时才暴露（例如自动更新检查命中后下一次 depsgraph 更新）。所以用
静态扫描把不变量锁死：凡是被注册进 bpy.app.handlers.<事件> 的函数，或带
@persistent 装饰器的函数，都必须能接受 2 个位置实参（惯用写法 (scene, *args)）。
"""

import ast
import os
import unittest


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# 只扫插件自身的源码目录（仓库根还堆着 variants/、.review-out/、_verify/ 等
# 被 .gitignore 排除的本地残留树，它们含旧版本副本，扫进去全是假阳性）。
# 新增插件包目录时请一并加进来。
SOURCE_DIRS = ("blueprint", "common", "toolkit", "ui", "utils", "tools", "TheHerta4_Velo_Bridge")
SKIP_DIRS = {"__pycache__"}


def _iter_python_files():
    for name in sorted(os.listdir(REPO_ROOT)):
        path = os.path.join(REPO_ROOT, name)
        if os.path.isfile(path) and name.endswith(".py"):
            yield path
    for name in SOURCE_DIRS:
        top = os.path.join(REPO_ROOT, name)
        if not os.path.isdir(top):
            continue
        for dirpath, dirnames, filenames in os.walk(top):
            dirnames[:] = [item for item in dirnames if item not in SKIP_DIRS]
            for filename in sorted(filenames):
                if filename.endswith(".py"):
                    yield os.path.join(dirpath, filename)


def _is_handler_register_call(node):
    """判断 Call 节点是否是 bpy.app.handlers.<事件>.append/remove(...)。"""
    func = node.func
    if not isinstance(func, ast.Attribute) or func.attr not in ("append", "remove"):
        return False
    handlers_attr = func.value
    if not isinstance(handlers_attr, ast.Attribute) or handlers_attr.attr != "handlers":
        return False
    bpy_app = handlers_attr.value
    return isinstance(bpy_app, ast.Attribute) and bpy_app.attr == "app"


def _registered_handler_names(tree):
    names = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not _is_handler_register_call(node):
            continue
        for arg in node.args:
            if isinstance(arg, ast.Name):
                names.add(arg.id)
    return names


def _is_persistent_decorator(decorator):
    text = ast.unparse(decorator)
    return text == "persistent" or text.endswith(".persistent")


def _persistent_handler_names(tree):
    names = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if any(_is_persistent_decorator(item) for item in node.decorator_list):
            names.add(node.name)
    return names


def _signature_text(args):
    parts = [arg.arg for arg in getattr(args, "posonlyargs", [])]
    parts.extend(arg.arg for arg in args.args)
    if args.vararg is not None:
        parts.append("*" + args.vararg.arg)
    elif args.kwonlyargs:
        parts.append("*")
    parts.extend(arg.arg for arg in args.kwonlyargs)
    if args.kwarg is not None:
        parts.append("**" + args.kwarg.arg)
    return "(" + ", ".join(parts) + ")"


def _accepts_two_positional(args):
    total = len(getattr(args, "posonlyargs", [])) + len(args.args)
    if args.vararg is not None:
        return True
    required = total - len(args.defaults)
    return required <= 2 <= total


class AppHandlerSignatureTests(unittest.TestCase):
    def _collect(self):
        offenders = []
        checked = []
        for path in _iter_python_files():
            with open(path, "r", encoding="utf-8") as handle:
                source = handle.read()
            try:
                tree = ast.parse(source)
            except SyntaxError:
                continue
            wanted = _registered_handler_names(tree) | _persistent_handler_names(tree)
            if not wanted:
                continue
            for node in ast.walk(tree):
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                if node.name not in wanted:
                    continue
                where = "%s:%d %s%s" % (
                    os.path.relpath(path, REPO_ROOT).replace(os.sep, "/"),
                    node.lineno,
                    node.name,
                    _signature_text(node.args),
                )
                checked.append(where)
                if not _accepts_two_positional(node.args):
                    offenders.append(where)
        return checked, offenders

    def test_registered_and_persistent_handlers_accept_two_arguments(self):
        checked, offenders = self._collect()
        # 扫描器失效护栏：仓库里当前有十多个这样的回调，命中太少说明 AST 判定写坏了。
        self.assertGreater(
            len(checked),
            12,
            "扫描到的 handler 数量异常偏少，护栏可能已失效：%r" % (checked,),
        )
        self.assertEqual(
            offenders,
            [],
            "以下 bpy.app.handlers 回调无法接受 Blender 传入的 2 个实参，"
            "请改成 (scene, *args)：\n  " + "\n  ".join(offenders),
        )

    def test_the_addon_updater_popup_handlers_are_covered(self):
        # 这两个回调注册进 depsgraph_update_post，PR #27 只修了 install 那个，
        # success 那个被漏掉；单独点名，避免以后又被漏。
        checked, _offenders = self._collect()
        self.assertTrue(
            any(item.startswith("addon_updater_ops.py:") for item in checked), checked
        )
        success = [item for item in checked if "updater_run_success_popup_handler" in item]
        install = [item for item in checked if "updater_run_install_popup_handler" in item]
        self.assertEqual(len(success), 1, success)
        self.assertEqual(len(install), 1, install)
        self.assertIn("*args", success[0], success)
        self.assertIn("*args", install[0], install)


if __name__ == "__main__":
    unittest.main()
