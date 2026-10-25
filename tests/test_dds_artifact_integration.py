import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest import mock

import test_dds_conversion as base
from test_dds_artifact_cache import module as cache_module, dds


class ArtifactIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='herta_cache_integration_')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / 'source.png'
        self.source.write_bytes(b'input bytes')
        self.exe = self.root / 'texconv.exe'
        self.exe.write_bytes(b'encoder version 1')
        self.props = types.SimpleNamespace(dds_use_custom_rules=False, dds_rules=[])
        self.api = base.dds_conversion
        self.cache = cache_module.DDSArtifactCache(self.root / 'cache')
        patches = [mock.patch.dict(sys.modules, {f'{base.PKG}.toolkit.dds_artifact_cache': cache_module}),
                   mock.patch.object(self.api, '_dds_artifact_store', self.cache),
                   mock.patch.object(self.api, 'find_texconv', return_value=str(self.exe))]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.api.clear_dds_conversion_cache()
        self.calls = 0

    def encode(self, command, **kwargs):
        self.calls += 1
        staging = Path(command[command.index('-o') + 1])
        fmt = command[command.index('-f') + 1]
        payload = bytearray(dds(self.calls))
        import struct
        struct.pack_into('<I', payload, 128, self.api._DDS_DXGI_FORMAT_IDS[fmt])
        for source in command[command.index('-y') + 1:]:
            (staging / (Path(source).stem + '.dds')).write_bytes(payload)
        return types.SimpleNamespace(returncode=0, stdout='', stderr='')

    def convert(self, target, fmt='bc7_unorm'):
        with mock.patch.object(self.api.subprocess, 'run', side_effect=self.encode):
            return self.api.convert_texture_to_dds_cached(str(self.source), str(target), self.props,
                                                         force=True, format_override=fmt)

    def test_cross_export_reuse_recreates_target_without_encoder(self):
        first, second = self.root / 'one.dds', self.root / 'different' / 'two.dds'
        self.assertTrue(self.convert(first)[0])
        self.api.clear_dds_conversion_cache()
        self.assertTrue(self.convert(second)[0])
        self.assertEqual(self.calls, 1)
        self.assertEqual(first.read_bytes(), second.read_bytes())

    def test_source_content_change_even_with_same_size_and_timestamp_misses(self):
        self.convert(self.root / 'one.dds')
        stat = self.source.stat()
        self.source.write_bytes(b'INPUT BYTES')
        os.utime(self.source, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        self.api.clear_dds_conversion_cache()
        self.assertTrue(self.convert(self.root / 'two.dds')[0])
        self.assertEqual(self.calls, 2)

    def test_format_and_encoder_changes_miss(self):
        self.convert(self.root / 'one.dds')
        self.api.clear_dds_conversion_cache()
        self.convert(self.root / 'two.dds', 'bc7_unorm_srgb')
        self.exe.write_bytes(b'encoder version 2')
        self.api.clear_dds_conversion_cache()
        self.convert(self.root / 'three.dds')
        self.assertEqual(self.calls, 3)

    def test_corrupt_cache_reencodes(self):
        self.convert(self.root / 'one.dds')
        path = next(self.cache.root.rglob('*.hdds'))
        data = bytearray(path.read_bytes());data[-1] ^= 1;path.write_bytes(data)
        self.api.clear_dds_conversion_cache()
        self.assertTrue(self.convert(self.root / 'two.dds')[0])
        self.assertEqual(self.calls, 2)

    def test_batch_reuses_artifacts_and_preserves_existing_targets(self):
        self.convert(self.root / 'first.dds')
        target = self.root / 'batch' / 'restored.dds'
        existing = self.root / 'already.dds';existing.write_bytes(b'user choice')
        jobs = [(1, str(self.source), str(target), {'format_override': 'bc7_unorm'}),
                (2, str(self.source), str(existing), {'format_override': 'bc7_unorm'})]
        with mock.patch.object(self.api.subprocess, 'run', side_effect=AssertionError('Unexpected encoding')):
            self.assertEqual(self.api.convert_new_textures_to_dds_batch(jobs), {1})
        self.assertEqual(target.read_bytes(), (self.root / 'first.dds').read_bytes())
        self.assertEqual(existing.read_bytes(), b'user choice')

    def test_unwritable_cache_does_not_fail_encoding(self):
        blocked = self.root / 'blocked';blocked.write_bytes(b'file')
        self.api._dds_artifact_store = cache_module.DDSArtifactCache(blocked)
        self.assertTrue(self.convert(self.root / 'one.dds')[0])
        self.assertEqual(self.calls, 1)

    def test_source_changed_during_encoding_is_not_published(self):
        def change(command, **kwargs):
            result = self.encode(command, **kwargs)
            self.source.write_bytes(b'changed during encoding')
            return result
        with mock.patch.object(self.api.subprocess, 'run', side_effect=change):
            self.assertTrue(self.api.convert_texture_to_dds(str(self.source), str(self.root / 'one.dds'),
                                                          self.props, format_override='bc7_unorm')[0])
        self.assertEqual(list(self.cache.root.rglob('*.hdds')), [])


if __name__ == '__main__':
    unittest.main()
