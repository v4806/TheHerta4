"""直出形态键：DrawIB 级基础 Position 缓冲的**多子网格拼接**回归测试。

回归背景（2026-09-24 实机报错）：
```
IndexError: index 2179 is out of bounds for axis 0 with size 2179
  blueprint/direct_export_shapekey_sampling_mixin.py:301
  blueprint/direct_export_runtime_utils.py:48  row_view[normalized_indices]
```
EFMI 逐子网格写盘（`LOD0.bdc966a9-11538-0-Position.buf` / `LOD1.bdc966a9-11538-0-Position.buf`），
而 `DrawIBModel._assemble_object_export_context_map` 给出的 `export_indices` 是 **DrawIB 级**的
（第二个子网格的索引 = 本子网格局部索引 + 前一个子网格的顶点数 2179）。旧实现按哈希前缀只解析到
其中一个子网格的文件（2179 行，合法下标 0..2178），于是第二个子网格的第一个顶点就恰好越界。

修复：把同一 DrawIB 的各子网格 `<unique_str>-Position.buf` 按 `submesh_model_list` 顺序拼接成
DrawIB 级缓冲（与 `_assemble_category_buffers` 同序同偏移），使文件与索引空间一致。
"""

import importlib.util
import sys
import tempfile
import types
import unittest
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[1]
MODULE_NAME = "_direct_shapekey_drawib_buffer_test_module"

_spec = importlib.util.spec_from_file_location(
    MODULE_NAME, REPO_ROOT / "blueprint" / "direct_export_runtime_utils.py"
)
_module = importlib.util.module_from_spec(_spec)
sys.modules[MODULE_NAME] = _module
_spec.loader.exec_module(_module)
assemble_drawib_position_bytes = _module.assemble_drawib_position_bytes
extract_position_bytes_by_indices = _module.extract_position_bytes_by_indices


class _GameType:
    def __init__(self, stride):
        self.CategoryStrideDict = {"Position": stride}


class _Submesh:
    def __init__(self, unique_str, stride, vertex_count):
        self.unique_str = unique_str
        self.d3d11_game_type = _GameType(stride)
        self.vertex_count = vertex_count


class DrawibPositionAssemblyTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _write_position_buf(self, unique_str: str, vertex_count: int, stride: int = 16):
        rows = np.arange(vertex_count * stride, dtype=np.uint8).reshape(vertex_count, stride)
        path = self.folder / f"{unique_str}-Position.buf"
        path.write_bytes(rows.tobytes())
        return path

    def test_multi_submesh_buffers_are_concatenated_in_order(self):
        first = _Submesh("LOD0.bdc966a9-11538-0", 16, 2179)
        second = _Submesh("LOD1.bdc966a9-11538-0", 16, 2180)
        self._write_position_buf(first.unique_str, first.vertex_count)
        self._write_position_buf(second.unique_str, second.vertex_count)

        merged, count = assemble_drawib_position_bytes(str(self.folder), [first, second])

        self.assertEqual(count, 2)
        self.assertEqual(len(merged), (2179 + 2180) * 16)
        # DrawIB 级索引 2179（第二个子网格的第一个顶点）现在可以正常采样
        sampled = extract_position_bytes_by_indices(merged, 16, np.asarray([0, 2179], dtype=np.int64))
        self.assertEqual(len(sampled), 32)
        # 拼接顺序 = submesh_model_list 顺序（第二个子网格的数据在后半段）
        self.assertEqual(sampled[16:], merged[2179 * 16:2180 * 16])

    def test_single_submesh_falls_back(self):
        only = _Submesh("LOD0.0d2ddddc-1032-0", 16, 218)
        self._write_position_buf(only.unique_str, only.vertex_count)

        merged, count = assemble_drawib_position_bytes(str(self.folder), [only])

        self.assertEqual((merged, count), (b"", 0))

    def test_missing_submesh_file_falls_back(self):
        first = _Submesh("LOD0.aaa-1-0", 16, 8)
        second = _Submesh("LOD1.aaa-1-0", 16, 8)
        self._write_position_buf(first.unique_str, first.vertex_count)  # 第二个文件缺失

        merged, count = assemble_drawib_position_bytes(str(self.folder), [first, second])

        self.assertEqual((merged, count), (b"", 0))

    def test_mixed_strides_fall_back(self):
        first = _Submesh("LOD0.aaa-1-0", 16, 8)
        second = _Submesh("LOD1.aaa-1-0", 12, 8)
        self._write_position_buf(first.unique_str, first.vertex_count, stride=16)
        self._write_position_buf(second.unique_str, second.vertex_count, stride=12)

        merged, count = assemble_drawib_position_bytes(str(self.folder), [first, second])

        self.assertEqual((merged, count), (b"", 0))

    def test_size_not_divisible_by_stride_falls_back(self):
        first = _Submesh("LOD0.aaa-1-0", 16, 8)
        second = _Submesh("LOD1.aaa-1-0", 16, 8)
        self._write_position_buf(first.unique_str, first.vertex_count)
        (self.folder / f"{second.unique_str}-Position.buf").write_bytes(b"\x00" * 100)

        merged, count = assemble_drawib_position_bytes(str(self.folder), [first, second])

        self.assertEqual((merged, count), (b"", 0))

    def test_missing_folder_falls_back(self):
        merged, count = assemble_drawib_position_bytes(
            str(self.folder / "not_there"), [_Submesh("LOD0.aaa-1-0", 16, 8)]
        )
        self.assertEqual((merged, count), (b"", 0))


if __name__ == "__main__":
    unittest.main()
