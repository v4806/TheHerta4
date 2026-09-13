# -*- coding: utf-8 -*-
"""variable_registry：CJK -> ASCII 转换与 pypinyin 探测缓存。

锁住两个曾经的坑：
1. 装了 pypinyin 之后同一会话内必须能立刻切到拼音命名（缓存要能被清掉），
   否则表现为“装完了名字还是 uXXXX，重启才生效”。
2. 没装 pypinyin 时回落 uXXXX，且纯 ASCII 文本零开销直通。
"""
import importlib.util
import sys
import types
import unittest
from pathlib import Path


def _install_module(name, **attrs):
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


_install_module("bpy")

_module_path = Path(__file__).resolve().parents[1] / "blueprint" / "variable_registry.py"
_spec = importlib.util.spec_from_file_location("_test_variable_registry_cjk", _module_path)
module = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = module
_spec.loader.exec_module(module)


class _FakePypinyin:
    """最小 pypinyin 替身：按字返回固定拼音。"""

    _PINYIN = {"屁": "pi", "股": "gu", "摇": "yao", "摆": "bai", "武": "wu", "器": "qi"}

    @classmethod
    def lazy_pinyin(cls, char):
        return [cls._PINYIN.get(char, char)]


class CjkToAsciiTests(unittest.TestCase):
    def setUp(self):
        module.reset_pinyin_cache()
        self._original_probe = module._probe_pinyin_available
        self.addCleanup(self._restore)
        sys.modules.pop("pypinyin", None)

    def _restore(self):
        module._probe_pinyin_available = self._original_probe
        module.reset_pinyin_cache()
        sys.modules.pop("pypinyin", None)

    def _set_pypinyin_available(self, available):
        module._probe_pinyin_available = lambda: available

    def test_ascii_text_passes_through_untouched(self):
        self._set_pypinyin_available(False)

        self.assertEqual(module.cjk_to_ascii("Freq_yaobai_001"), "Freq_yaobai_001")
        self.assertEqual(module.cjk_to_ascii(""), "")

    def test_without_pypinyin_falls_back_to_unicode_escape(self):
        self._set_pypinyin_available(False)

        self.assertEqual(module.cjk_to_ascii("屁股摇摆"), "u5c41u80a1u6447u6446")
        self.assertEqual(module._sanitize_name("摇摆_001", fallback="shape"), "u6447u6446_001")
        # 关键回归点：中文不能被整段剥离成 "_001"
        self.assertNotEqual(module._sanitize_name("摇摆_001", fallback="shape"), "_001")

    def test_with_pypinyin_uses_lazy_pinyin(self):
        sys.modules["pypinyin"] = _FakePypinyin()
        self._set_pypinyin_available(True)

        self.assertEqual(module.cjk_to_ascii("摇摆"), "yaobai")
        # 替身词典里没有的汉字（切/换）没有拼音：回落 uXXXX，仍必须是纯 ASCII
        self.assertEqual(module.cjk_to_ascii("武器切换"), "wuqiu5207u6362")
        self.assertEqual(module._sanitize_name("摇摆_001", fallback="shape"), "yaobai_001")

    def test_cache_is_reused_until_reset(self):
        self._set_pypinyin_available(False)
        self.assertEqual(module.cjk_to_ascii("摇摆"), "u6447u6446")

        # 同会话内“安装”了 pypinyin：探测缓存未清时仍走 uXXXX（旧行为的症状）
        sys.modules["pypinyin"] = _FakePypinyin()
        self._set_pypinyin_available(True)
        self.assertEqual(module.cjk_to_ascii("摇摆"), "u6447u6446")

        # 安装算子会调用 reset_pinyin_cache()：此后立刻切到拼音
        module.reset_pinyin_cache()
        self.assertEqual(module.cjk_to_ascii("摇摆"), "yaobai")

    def test_broken_pypinyin_installation_falls_back_and_reprobes(self):
        module._probe_pinyin_available = lambda: True

        class _Broken:
            def __getattr__(self, _name):
                raise ImportError("broken installation")

        sys.modules["pypinyin"] = _Broken()
        try:
            self.assertEqual(module.cjk_to_ascii("摇摆"), "u6447u6446")
            # 导入失败后必须清掉缓存，供下次重新探测
            self.assertIsNone(module._PINYIN_AVAILABLE)
        finally:
            sys.modules.pop("pypinyin", None)


class PinyinAvailabilityTests(unittest.TestCase):
    def setUp(self):
        module.reset_pinyin_cache()
        self._original_probe = module._probe_pinyin_available
        self.addCleanup(self._restore)

    def _restore(self):
        module._probe_pinyin_available = self._original_probe
        module.reset_pinyin_cache()

    def test_probe_result_is_cached(self):
        calls = []

        def _probe():
            calls.append(1)
            return True

        module._probe_pinyin_available = _probe

        self.assertTrue(module.is_pinyin_available())
        self.assertTrue(module.is_pinyin_available())
        self.assertTrue(module.is_pinyin_available(refresh=False))
        self.assertEqual(len(calls), 1)

        module.is_pinyin_available(refresh=True)
        self.assertEqual(len(calls), 2)

    def test_reset_pinyin_cache_forces_reprobe(self):
        module._probe_pinyin_available = lambda: False
        self.assertFalse(module.is_pinyin_available())

        module._probe_pinyin_available = lambda: True
        self.assertFalse(module.is_pinyin_available())  # 仍是缓存值
        module.reset_pinyin_cache()
        self.assertTrue(module.is_pinyin_available())

    def test_probe_never_raises(self):
        # 真实探测实现必须吞掉异常并返回布尔值（find_spec 在异常环境会抛）
        self.assertIsInstance(module.is_pinyin_available(refresh=True), bool)


if __name__ == "__main__":
    unittest.main()
