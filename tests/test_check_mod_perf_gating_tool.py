# -*- coding: utf-8 -*-
"""性能门控审计工具的回归测试。

`tools/check_mod_perf_gating.py` 是本仓库「生成器改动 → 模组同步」口径的可执行断言。
这里保证：

1. 工具本身能跑通、对**合成的最小 ini** 能正确报出缺失项（退出码 1）；
2. 若本机存在克拉蕾模组的真实产物，则它必须**全部通过**（29 项）—— 这样任何把
   生成器/产物改动改回去的行为都会在测试里立刻暴露。

真实模组不存在时（CI/其它机器）跳过第 2 项。
"""
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
TOOL = REPO_ROOT / "tools" / "check_mod_perf_gating.py"
REAL_INI = Path(
    r"K:\SSMT-Package-master\3Dmigoto\ZZZ\Mods\SSMTGeneratedMod\克拉蕾\克拉蕾.ini"
)


def _run_tool(ini_path):
    env = dict(os.environ)
    # 子进程 stdout 默认跟随系统区域编码（本机 cp936），断言里要用中文标记，
    # 统一强制 UTF-8 才能稳定解码。
    env["PYTHONIOENCODING"] = "utf-8"
    result = subprocess.run(
        [sys.executable, str(TOOL), str(ini_path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
    )
    return result.returncode, result.stdout + result.stderr


class CheckModPerfGatingToolTests(unittest.TestCase):
    def test_tool_reports_failures_on_minimal_ini(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ini_path = Path(temp_dir) / "synthetic.ini"
            ini_path.write_text(
                "[Constants]\nglobal $active0\n\n[Present]\npost $active0 = 0\n",
                encoding="utf-8",
            )

            code, output = _run_tool(ini_path)

            self.assertEqual(code, 1, f"缺失门控时必须非零退出:\n{output}")
            match = re.search(r"门控同步项 (\d+)/(\d+)", output)
            self.assertIsNotNone(match, output)
            passed, total = (int(value) for value in match.groups())
            self.assertLess(passed, total)
            self.assertIn("未通过项", output)

    def test_tool_also_checks_generator_side(self):
        """审计必须双向：产物侧 + 生成器侧（"生成器改了模组必须同步"的另一半）。

        只查产物的话，生成器被改回去（下次重新导出就丢优化）也发现不了。
        """
        if not REAL_INI.is_file():
            self.skipTest(f"本机没有该模组产物: {REAL_INI}")

        code, output = _run_tool(REAL_INI)

        self.assertEqual(code, 0, f"生成器侧检查失败:\n{output}")
        for label in ("P1 ", "P2 ", "P3 ", "P4 ", "P5 ", "P6 "):
            self.assertIn(f"[OK  ] {label}", output, f"缺少生成器侧检查 {label}")

    def test_tool_reports_clean_pass_on_real_mod(self):
        if not REAL_INI.is_file():
            self.skipTest(f"本机没有该模组产物: {REAL_INI}")

        code, output = _run_tool(REAL_INI)

        self.assertEqual(code, 0, f"真实产物必须全部通过:\n{output}")
        total, passed = re.search(r"门控同步项 (\d+)/(\d+)", output).groups()
        self.assertEqual(total, passed)
        self.assertGreaterEqual(int(total), 29)


if __name__ == "__main__":
    unittest.main()
