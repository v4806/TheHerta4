import concurrent.futures
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

MODULE = Path(__file__).resolve().parents[1] / 'toolkit' / 'dds_artifact_cache.py'
spec = importlib.util.spec_from_file_location('artifact_cache_under_test', MODULE)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def dds(value=1):
    data = bytearray(164)
    data[:4] = b'DDS '
    struct.pack_into('<I', data, 4, 124)
    struct.pack_into('<II', data, 12, 4, 4)
    struct.pack_into('<I', data, 76, 32)
    data[84:88] = b'DX10'
    struct.pack_into('<I', data, 128, 98)
    data[148:] = bytes([value]) * 16
    return bytes(data)


class ArtifactCacheTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='herta_cache_test_')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.cache = module.DDSArtifactCache(self.root / 'cache')
        self.key = self.cache.key(hashlib.sha256(b'source').hexdigest(), {'backend': 'test', 'format': 98})

    def test_key_covers_content_and_recipe_but_not_order(self):
        self.assertEqual(self.cache.key('a', {'b': 1, 'a': 2}), self.cache.key('a', {'a': 2, 'b': 1}))
        for digest, recipe in [('b', {'format': 98}), ('a', {'format': 99}), ('a', {'backend': 'v2'})]:
            self.assertNotEqual(self.cache.key('a', {'format': 98}), self.cache.key(digest, recipe))

    def test_persistent_across_fresh_process(self):
        self.assertTrue(self.cache.put(self.key, dds()))
        code = ('import importlib.util,sys;'
                's=importlib.util.spec_from_file_location("c",sys.argv[1]);'
                'm=importlib.util.module_from_spec(s);s.loader.exec_module(m);'
                'assert m.DDSArtifactCache(sys.argv[2]).get(sys.argv[3]) is not None')
        subprocess.run([sys.executable, '-c', code, str(MODULE), str(self.cache.root), self.key], check=True)

    def test_restore_is_independent_copy(self):
        self.cache.put(self.key, dds())
        target = self.root / 'new' / 'texture.dds'
        self.assertTrue(self.cache.restore(self.key, target))
        self.assertEqual(target.read_bytes(), dds())
        target.write_bytes(dds(2))
        self.assertEqual(self.cache.get(self.key), dds())

    def test_corruption_and_truncation_do_not_touch_existing_target(self):
        target = self.root / 'texture.dds'
        target.write_bytes(b'user target')
        for corruption in ('payload', 'truncate', 'metadata'):
            self.cache.put(self.key, dds())
            path = self.cache._path(self.key)
            data = bytearray(path.read_bytes())
            if corruption == 'payload':
                data[-1] ^= 1
            elif corruption == 'truncate':
                data = data[:-1]
            else:
                data[:8] = b'badmagic'
            path.write_bytes(data)
            self.assertIsNone(self.cache.get(self.key))
            self.assertFalse(self.cache.restore(self.key, target))
            self.assertEqual(target.read_bytes(), b'user target')
        self.assertEqual(list(self.root.glob('.herta_cache_*')), [])

    def test_validator_rejects_without_touching_target(self):
        self.cache.put(self.key, dds())
        target = self.root / 'texture.dds'
        target.write_bytes(b'existing')
        self.assertFalse(self.cache.restore(self.key, target, lambda path: False))
        self.assertEqual(target.read_bytes(), b'existing')

    def test_unwritable_cache_is_a_miss(self):
        root = self.root / 'not-a-directory'
        root.write_bytes(b'file')
        cache = module.DDSArtifactCache(root)
        self.assertFalse(cache.put(self.key, dds()))
        self.assertIsNone(cache.get(self.key))
        self.assertFalse(cache.restore(self.key, self.root / 'target'))

    def test_concurrent_publish_and_read_never_returns_partial_data(self):
        def operation(index):
            value = dds(index % 255)
            self.cache.put(self.key, value)
            result = self.cache.get(self.key)
            self.assertTrue(result is None or len(result) == 164)
            if result is not None:
                self.assertEqual(len(set(result[148:])), 1)
        with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
            list(pool.map(operation, range(64)))
        self.assertIsNotNone(self.cache.get(self.key))

    def test_quota_evicts_oldest_and_invalid_dds_is_not_published(self):
        self.assertFalse(self.cache.put(self.key, b'not DDS'))
        small = module.DDSArtifactCache(self.root / 'small', max_bytes=500)
        first = small.key('first', {})
        second = small.key('second', {})
        self.assertTrue(small.put(first, dds()))
        small._last_maintenance = 0
        self.assertTrue(small.put(second, dds(2)))
        self.assertIsNone(small.get(first))
        self.assertEqual(small.get(second), dds(2))

    def test_invalid_keys_and_disabled_cache(self):
        self.assertFalse(self.cache.put('../bad', dds()))
        self.assertIsNone(self.cache.get('../bad'))
        with mock.patch.dict(os.environ, {'HERTA_DDS_CACHE': '0'}):
            cache = module.DDSArtifactCache(self.root / 'disabled')
            self.assertFalse(cache.put(self.key, dds()))
            self.assertFalse(cache.root.exists())


if __name__ == '__main__':
    unittest.main()
