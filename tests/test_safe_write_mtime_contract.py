"""``common/safe_write.py`` 的 mtime 幂等契约回归测试。

背景（2026-09-16 实测）
------------------------------------------------------------------

3DMigoto 的自定义着色器编译缓存**按 ``.hlsl`` 的 mtime 配对**：
``DirectX11/CommandList.cpp`` 的 ``CustomShader::compile()`` 用
``GetFileTime`` 取 ``.hlsl`` 的写入时间，交给 ``load_cached_shader()``；后者
只在 ``CompareFileTime(hlsl_timestamp, cache_timestamp) == 0`` 时命中，否则
丢弃缓存并 ``D3DCompile`` 重编译。写缓存时 3DMigoto 会把 ``.bin`` 的 mtime
``set_file_last_write_time()`` 对齐成 ``.hlsl`` 的，两侧就此绑定成"同一代"。

因此：**内容没有任何变化的一次重写，就会让整族编译缓存作废**，下次进游戏 /
按 F10 全量重编译（实测单次两分钟量级，``optimization_level3`` + 大 CS）。
用户目录里被 ``open(dest, 'w')`` 重写的着色器 mtime 是导出时刻，而走
``shutil.copy2`` 的保留了工具目录模板的原始 mtime——两条路径的差异就是本模块
（以及本测试）要守住的契约。

补充（2026-09-17）：``sync_mtime`` 曾用 ``os.utime((st_atime, st_mtime))`` 的
**浮点秒**写法，float64 分辨率（~2.4e-7 s）比 NTFS 的 100ns tick 粗，实测把目标
mtime 搬到相邻 tick（0 ~ -200ns）。于是 ``copy_file_if_changed`` 第 1 次复制
（MISS，走 ``copy2`` 精确）与第 2 次起（HIT，走 ``sync_mtime`` 取整）给出**不同**
的 ``.hlsl`` mtime，编译缓存因此多失效一次。现改为 ``st_mtime_ns`` +
``os.utime(ns=...)``，本文件的 mtime 断言也统一改用 ``st_mtime_ns`` 精确比较
（不再用 ``assertAlmostEqual(..., places=6)`` 这类浮点近似）。

这些测试全部只依赖标准库，不需要 bpy。
"""

import importlib.util
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SAFE_WRITE_PATH = REPO_ROOT / "common" / "safe_write.py"


def _load_safe_write():
    """按文件路径加载真实模块（避开插件根包的 bpy 依赖）。"""
    spec = importlib.util.spec_from_file_location(
        "_safe_write_contract_test", SAFE_WRITE_PATH
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


safe_write = _load_safe_write()


def _float_fragile_mtime_ns():
    """挑一个「经 float 秒往返会变」的 100ns 对齐 mtime_ns；找不到返回 None。

    回归保护需要它：只有目标 mtime 经 float 往返会变时，``st_mtime_ns`` 相等
    断言才真正能区分「纳秒精确」与「float 取整」两种实现。CPython 的
    ``os.utime(float)`` 按 100ns 单位取整，故往返判据写成
    ``round(秒 * 1e7) * 100``。
    """
    base = 1_700_000_000_000_000_000  # 2023-11-14，100ns 对齐
    for step in range(0, 20_000, 100):
        candidate = base + step
        if round((candidate / 1e9) * 1e7) * 100 != candidate:
            return candidate
    return None


class _TempDirMixin:
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.dir = self._dir.name

    def tearDown(self):
        self._dir.cleanup()

    def path(self, name):
        return os.path.join(self.dir, name)


class WriteTextIfChangedTests(_TempDirMixin, unittest.TestCase):
    def test_first_write_reports_changed(self):
        target = self.path("a.hlsl")
        self.assertTrue(safe_write.write_text_if_changed(target, "line1\nline2\n"))
        self.assertTrue(os.path.isfile(target))

    def test_identical_text_does_not_touch_file(self):
        """核心契约：同内容 -> 不写、mtime 不动、磁盘字节不动。"""
        target = self.path("a.hlsl")
        text = "line1\nline2\n"
        safe_write.write_text_if_changed(target, text)

        with open(target, "rb") as file_obj:
            bytes_before = file_obj.read()
        mtime_before = os.stat(target).st_mtime_ns

        time.sleep(0.05)
        wrote = safe_write.write_text_if_changed(target, text)

        with open(target, "rb") as file_obj:
            bytes_after = file_obj.read()

        self.assertFalse(wrote, "同内容必须返回 False（未写）")
        self.assertEqual(
            mtime_before, os.stat(target).st_mtime_ns, "mtime 不得被刷新（纳秒级）"
        )
        self.assertEqual(bytes_before, bytes_after, "磁盘字节不得变化")

    def test_crlf_on_disk_versus_lf_text_is_identical(self):
        """Windows 上写出的文件是 CRLF，调用方手里的 text 是 LF。

        这是真实场景：3DMigoto 侧比对的是 mtime，而调用方每次拿到的都是 LF
        文本。若按"编码后字节"直接比，CRLF 文件会被永远判成"变了"，幂等失效。
        """
        target = self.path("crlf.hlsl")
        text = "alpha\nbeta\ngamma\n"
        safe_write.write_text_if_changed(target, text)

        with open(target, "rb") as file_obj:
            raw = file_obj.read()
        if b"\r\n" not in raw:
            self.skipTest("当前平台不做 LF->CRLF 转换，本用例无区分度")

        mtime_before = os.stat(target).st_mtime_ns
        time.sleep(0.05)
        wrote = safe_write.write_text_if_changed(target, text)

        self.assertFalse(wrote, "CRLF 文件 vs LF 文本应判为内容相同")
        self.assertEqual(mtime_before, os.stat(target).st_mtime_ns)

    def test_real_content_change_is_written(self):
        target = self.path("a.hlsl")
        safe_write.write_text_if_changed(target, "line1\nline2\n")
        mtime_before = os.stat(target).st_mtime_ns

        time.sleep(0.05)
        wrote = safe_write.write_text_if_changed(target, "line1\nCHANGED\n")

        self.assertTrue(wrote)
        self.assertNotEqual(mtime_before, os.stat(target).st_mtime_ns)
        with open(target, encoding="utf-8") as file_obj:
            self.assertEqual(file_obj.read(), "line1\nCHANGED\n")

    def test_missing_file_is_created_with_parents(self):
        target = os.path.join(self.dir, "nested", "deep", "a.hlsl")
        self.assertTrue(safe_write.write_text_if_changed(target, "x\n"))
        self.assertTrue(os.path.isfile(target))

    def test_returns_false_when_only_line_endings_differ(self):
        """纯行尾差异不算内容变化（HLSL 语义等价），刻意不写盘。"""
        target = self.path("a.hlsl")
        with open(target, "wb") as file_obj:
            file_obj.write(b"a\r\nb\r\n")
        mtime_before = os.stat(target).st_mtime_ns

        time.sleep(0.05)
        wrote = safe_write.write_text_if_changed(target, "a\nb\n")

        self.assertFalse(wrote)
        self.assertEqual(mtime_before, os.stat(target).st_mtime_ns)
        with open(target, "rb") as file_obj:
            self.assertEqual(file_obj.read(), b"a\r\nb\r\n", "不得改写磁盘字节")


class WriteBytesIfChangedTests(_TempDirMixin, unittest.TestCase):
    def test_identical_bytes_do_not_touch_file(self):
        target = self.path("b.bin")
        payload = b"\x00\x01\x02\xff"
        self.assertTrue(safe_write.write_bytes_if_changed(target, payload))
        mtime_before = os.stat(target).st_mtime_ns

        time.sleep(0.05)
        self.assertFalse(safe_write.write_bytes_if_changed(target, payload))
        self.assertEqual(mtime_before, os.stat(target).st_mtime_ns)

    def test_changed_bytes_are_written(self):
        target = self.path("b.bin")
        safe_write.write_bytes_if_changed(target, b"\x01")
        self.assertTrue(safe_write.write_bytes_if_changed(target, b"\x02"))
        with open(target, "rb") as file_obj:
            self.assertEqual(file_obj.read(), b"\x02")

    def test_accepts_bytearray_and_memoryview(self):
        target = self.path("b.bin")
        self.assertTrue(safe_write.write_bytes_if_changed(target, bytearray(b"ab")))
        self.assertFalse(safe_write.write_bytes_if_changed(target, memoryview(b"ab")))

    def test_rejects_non_bytes(self):
        with self.assertRaises(TypeError):
            safe_write.write_bytes_if_changed(self.path("b.bin"), "not bytes")


class WriteBytesIfChangedAtomicTests(_TempDirMixin, unittest.TestCase):
    """``write_bytes_if_changed_atomic``：先比较、再原子发布。"""

    def test_first_write_publishes(self):
        target = self.path("s.hlsl")
        self.assertTrue(
            safe_write.write_bytes_if_changed_atomic(target, b"shader-v1")
        )
        with open(target, "rb") as file_obj:
            self.assertEqual(file_obj.read(), b"shader-v1")

    def test_identical_payload_leaves_file_untouched(self):
        target = self.path("s.hlsl")
        safe_write.write_bytes_if_changed_atomic(target, b"shader-v1")
        mtime_before = os.stat(target).st_mtime_ns

        time.sleep(0.05)
        wrote = safe_write.write_bytes_if_changed_atomic(target, b"shader-v1")

        self.assertFalse(wrote, "同内容必须返回 False（未发布）")
        self.assertEqual(mtime_before, os.stat(target).st_mtime_ns)

    def test_changed_payload_is_published(self):
        target = self.path("s.hlsl")
        safe_write.write_bytes_if_changed_atomic(target, b"v1")
        self.assertTrue(safe_write.write_bytes_if_changed_atomic(target, b"v2"))
        with open(target, "rb") as file_obj:
            self.assertEqual(file_obj.read(), b"v2")

    def test_no_temp_file_left_behind(self):
        target = self.path("s.hlsl")
        safe_write.write_bytes_if_changed_atomic(target, b"v1")
        leftovers = [
            name for name in os.listdir(self.dir) if name.endswith(".tmp")
        ]
        self.assertEqual(leftovers, [], "不得残留临时文件")

    def test_matches_plain_variant_semantics(self):
        a = self.path("plain.hlsl")
        b = self.path("atomic.hlsl")
        payload = b"payload"
        self.assertEqual(
            safe_write.write_bytes_if_changed(a, payload),
            safe_write.write_bytes_if_changed_atomic(b, payload),
        )
        self.assertFalse(safe_write.write_bytes_if_changed(a, payload))
        self.assertFalse(safe_write.write_bytes_if_changed_atomic(b, payload))


class CopyFileIfChangedTests(_TempDirMixin, unittest.TestCase):
    def test_first_copy_then_noop(self):
        source = self.path("src.hlsl")
        target = self.path("dst.hlsl")
        with open(source, "w", encoding="utf-8", newline="") as file_obj:
            file_obj.write("shader\n")

        self.assertTrue(safe_write.copy_file_if_changed(source, target))
        with open(target, encoding="utf-8") as file_obj:
            self.assertEqual(file_obj.read(), "shader\n")

        mtime_before = os.stat(target).st_mtime_ns
        time.sleep(0.05)
        self.assertFalse(safe_write.copy_file_if_changed(source, target))
        self.assertEqual(os.stat(target).st_mtime_ns, mtime_before)

    def test_sync_mtime_is_nanosecond_exact(self):
        """``sync_mtime`` 必须把目标 mtime 精确对齐到源的纳秒值（不是浮点近似）。

        回归保护：``hard_ns`` 是经 float 秒往返会变的 100ns 对齐值；旧实现
        （``os.utime((st_atime, st_mtime))``）会把它搬到相邻 tick，本断言即失败。
        """
        hard_ns = _float_fragile_mtime_ns()
        if hard_ns is None:
            self.skipTest("当前平台 float 秒往返未丢精度，本用例无区分度")
        source = self.path("src.hlsl")
        target = self.path("dst.hlsl")
        for path, text in ((source, "shader\n"), (target, "other\n")):
            with open(path, "w", encoding="utf-8", newline="") as file_obj:
                file_obj.write(text)
        os.utime(source, ns=(hard_ns, hard_ns))
        if os.stat(source).st_mtime_ns != hard_ns:
            self.skipTest("当前文件系统无法存放该 100ns 精度（如 FAT/exFAT）")

        self.assertTrue(safe_write.sync_mtime(source, target))
        self.assertEqual(
            os.stat(target).st_mtime_ns,
            hard_ns,
            "目标 mtime 必须精确等于源的纳秒值",
        )
        self.assertEqual(
            os.stat(target).st_mtime_ns,
            os.stat(source).st_mtime_ns,
            "目标 mtime 必须与源在纳秒级完全相等",
        )

    def test_copy_hit_does_not_shift_nanosecond_mtime(self):
        """命中路径不得让目标 mtime 漂移哪怕 1 个 tick（第 2 次导出的回归）。

        旧实现的现象：第 1 次复制走 ``copy2``（精确）→ dst == 源模板 mtime；
        第 2 次起走 ``sync_mtime``（float 取整）→ dst 少 0~200ns，于是 ``.hlsl``
        自身的 mtime 在两次导出之间变化，3DMigoto 缓存多失效一次（其后稳定）。
        """
        hard_ns = _float_fragile_mtime_ns()
        if hard_ns is None:
            self.skipTest("当前平台 float 秒往返未丢精度，本用例无区分度")
        source = self.path("src.hlsl")
        target = self.path("dst.hlsl")
        with open(source, "w", encoding="utf-8", newline="") as file_obj:
            file_obj.write("shader\n")
        os.utime(source, ns=(hard_ns, hard_ns))
        if os.stat(source).st_mtime_ns != hard_ns:
            self.skipTest("当前文件系统无法存放该 100ns 精度（如 FAT/exFAT）")

        self.assertTrue(safe_write.copy_file_if_changed(source, target))
        self.assertEqual(
            os.stat(target).st_mtime_ns,
            hard_ns,
            "首次复制（MISS/copy2）必须保留源的纳秒 mtime",
        )

        before = os.stat(target).st_mtime_ns
        self.assertFalse(safe_write.copy_file_if_changed(source, target))
        self.assertEqual(
            os.stat(target).st_mtime_ns,
            before,
            "命中路径（HIT/sync_mtime）不得改变目标 mtime，哪怕 1 个 tick",
        )
        self.assertEqual(os.stat(target).st_mtime_ns, hard_ns)

    def test_identical_content_resyncs_target_mtime_to_source(self):
        """被旧导出刷过 mtime 的目标，在下次同内容导出时被回正。

        这是修复生效后**自动愈合**既有安装的关键行为：工具目录模板的 mtime
        恒定，于是重复导出的目标 mtime 也恒定，3DMigoto 侧缓存不再逐次失效。
        """
        source = self.path("src.hlsl")
        target = self.path("dst.hlsl")
        with open(source, "w", encoding="utf-8", newline="") as file_obj:
            file_obj.write("shader\n")
        old = time.time() - 86400 * 45
        os.utime(source, (old, old))

        safe_write.copy_file_if_changed(source, target)
        self.assertEqual(
            os.stat(target).st_mtime_ns,
            os.stat(source).st_mtime_ns,
            "首次复制后目标 mtime 必须与源纳秒级完全相等",
        )

        # 模拟"上一次导出把目标 mtime 刷成了现在"
        os.utime(target, (time.time(), time.time()))
        self.assertNotEqual(
            os.stat(target).st_mtime_ns, os.stat(source).st_mtime_ns
        )

        self.assertFalse(safe_write.copy_file_if_changed(source, target))
        self.assertEqual(
            os.stat(target).st_mtime_ns,
            os.stat(source).st_mtime_ns,
            "同内容命中时目标 mtime 必须与源纳秒级完全相等（不得取整漂移）",
        )

    def test_source_change_is_copied(self):
        source = self.path("src.hlsl")
        target = self.path("dst.hlsl")
        with open(source, "w", encoding="utf-8", newline="") as file_obj:
            file_obj.write("v1\n")
        safe_write.copy_file_if_changed(source, target)

        with open(source, "w", encoding="utf-8", newline="") as file_obj:
            file_obj.write("v2\n")
        self.assertTrue(safe_write.copy_file_if_changed(source, target))
        with open(target, encoding="utf-8") as file_obj:
            self.assertEqual(file_obj.read(), "v2\n")

    def test_missing_source_raises(self):
        with self.assertRaises(FileNotFoundError):
            safe_write.copy_file_if_changed(
                self.path("nope.hlsl"), self.path("dst.hlsl")
            )


class RealExportArtifactTests(unittest.TestCase):
    """用磁盘上真实的导出产物验证：同逻辑内容 -> 不动文件。

    这些文件由旧版导出写出（纯 CRLF），正是要修的场景。样例不存在时跳过，
    避免测试对用户机器的具体 mod 目录产生硬依赖。
    """

    REAL_SAMPLES = (
        r"K:\SSMT-Package-master\3Dmigoto\ZZZ\Mods\SSMTGeneratedMod\克拉蕾\res"
        r"\drag_interaction\rzm_object_detect.hlsl",
        r"K:\SSMT-Package-master\3Dmigoto\ZZZ\Mods\SSMTGeneratedMod\克拉蕾\res"
        r"\drag_interaction\rzm_jiggle_interaction.hlsl",
    )

    def test_rewriting_same_logical_content_is_noop(self):
        checked = 0
        for sample in self.REAL_SAMPLES:
            if not os.path.isfile(sample):
                continue
            checked += 1
            with open(sample, "r", encoding="utf-8", newline="") as file_obj:
                raw_text = file_obj.read()
            logical = raw_text.replace("\r\n", "\n")

            with tempfile.TemporaryDirectory() as tmp:
                target = os.path.join(tmp, os.path.basename(sample))
                with open(target, "wb") as file_obj:
                    file_obj.write(raw_text.encode("utf-8"))
                mtime_before = os.stat(target).st_mtime_ns

                time.sleep(0.05)
                wrote = safe_write.write_text_if_changed(target, logical)

                self.assertFalse(
                    wrote, "%s: 同逻辑内容必须判为未变" % os.path.basename(sample)
                )
                self.assertEqual(
                    mtime_before,
                    os.stat(target).st_mtime_ns,
                    "%s: mtime 不得被刷新" % os.path.basename(sample),
                )
        if not checked:
            self.skipTest("用户 mod 目录样例不存在，跳过")


if __name__ == "__main__":
    unittest.main()
