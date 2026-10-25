"""Native BC7 parity, pooling, bounds and concurrency against the numpy oracle."""
import importlib.util
from pathlib import Path
import struct
import unittest
from unittest import mock
from concurrent.futures import ThreadPoolExecutor

import numpy as np

ROOT = Path(__file__).resolve().parents[1]


def load(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


dds = load('_native_test_dds', 'common/dds_pixel.py')
dedup = load('_native_test_dedup', 'common/texture_dedup.py')


def container(width, height, payload):
    header = bytearray(128)
    header[:4] = b'DDS '
    struct.pack_into('<I', header, 4, 124)
    struct.pack_into('<II', header, 12, height, width)
    struct.pack_into('<II', header, 76, 32, 4)
    header[84:88] = b'BC7 '
    return bytes(header) + payload


@unittest.skipUnless(dds._get_native_library(), 'Native DLL not available on this platform')
class NativeDDS(unittest.TestCase):
    def test_all_modes_and_reserved_blocks_exact(self):
        rng = np.random.default_rng(876)
        blocks = rng.integers(0, 256, (8 * 512 + 64, 16), dtype=np.uint8)
        for mode in range(8):
            group = blocks[mode * 512:(mode + 1) * 512]
            group[:, 0] &= np.uint8(255 ^ ((1 << (mode + 1)) - 1))
            group[:, 0] |= np.uint8(1 << mode)
        blocks[-64:, 0] = 0
        expected = dds._decode_bc7(blocks).reshape(-1, 4, 4)
        actual = dds.decode_dds_rgba8(container(4, len(blocks) * 4, blocks.tobytes()))
        np.testing.assert_array_equal(actual, expected)

    def test_pooling_and_cropping_match_full_decode(self):
        rng = np.random.default_rng(837)
        for width, height, grid in ((1, 1, 64), (7, 9, 5), (32, 32, 7), (129, 65, 64), (256, 256, 64)):
            with self.subTest(size=(width, height), grid=grid):
                blocks = rng.integers(0, 256, (((width + 3) // 4) * ((height + 3) // 4), 16), dtype=np.uint8)
                data = container(width, height, blocks.tobytes())
                with mock.patch.object(dds, '_get_native_library', return_value=None):
                    expected = dds.decode_dds_rgba8(data)
                np.testing.assert_array_equal(dds.decode_dds_rgba8(data), expected)
                thumb = dds.decode_dds_thumbnail(data, grid)
                np.testing.assert_array_equal(thumb, dedup._similarity_pool(expected, grid))
                a = dedup.similarity_features(expected, grid)
                b = dedup.similarity_features_from_thumbnail(thumb)
                for key in a:
                    np.testing.assert_array_equal(a[key], b[key])

    def test_native_bounds_and_truncation(self):
        lib = dds._get_native_library()
        output = np.full((4, 4, 4), 67, dtype=np.uint8)
        for size, width, height, capacity in ((15, 4, 4, 64), (16, 4, 4, 63), (16, 0, 4, 64)):
            self.assertEqual(lib.herta_dds_decode_bc7(bytes(16), size, width, height, output.ctypes.data, capacity), 0)
        self.assertTrue(np.all(output == 67))
        self.assertIsNone(dds.decode_dds_rgba8(container(4, 4, bytes(15))))
        self.assertIsNone(dds.decode_dds_thumbnail(container(4, 4, bytes(15))))

    def test_concurrent_calls_do_not_share_output(self):
        rng = np.random.default_rng(8)
        data = [container(32, 32, rng.bytes(1024)) for _ in range(16)]
        expected = [dds.decode_dds_rgba8(item) for item in data]
        with ThreadPoolExecutor(max_workers=4) as pool:
            actual = list(pool.map(dds.decode_dds_rgba8, data))
        for a, b in zip(expected, actual):
            np.testing.assert_array_equal(a, b)


if __name__ == '__main__':
    unittest.main()
