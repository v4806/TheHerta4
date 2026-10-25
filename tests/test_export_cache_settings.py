import ast
from concurrent.futures import ThreadPoolExecutor
import importlib
import os
from pathlib import Path
import struct
import sys
import tempfile
import types
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = '_unified_export_cache_test'
for name, path in ((PACKAGE,ROOT),(PACKAGE+'.common',ROOT/'common'),
                   (PACKAGE+'.toolkit',ROOT/'toolkit'),(PACKAGE+'.blueprint',ROOT/'blueprint')):
    module=types.ModuleType(name);module.__path__=[str(path)];sys.modules[name]=module
scene=types.SimpleNamespace(enabled=True)
props=types.ModuleType(PACKAGE+'.common.global_properties')
props.GlobalProterties=types.SimpleNamespace(enable_export_cache=lambda:scene.enabled)
sys.modules[props.__name__]=props
policy=importlib.import_module(PACKAGE+'.common.export_cache')
artifacts=importlib.import_module(PACKAGE+'.toolkit.dds_artifact_cache')
analysis=importlib.import_module(PACKAGE+'.common.texture_dedup')


def valid_dds():
    data=bytearray(164);data[:4]=b'DDS '
    for offset,value in ((4,124),(12,4),(16,4),(76,32),(128,98)):
        struct.pack_into('<I',data,offset,value)
    data[84:88]=b'DX10'
    return bytes(data)


class ExportCacheSettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='herta_unified_cache_');self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        patch=mock.patch.dict(os.environ,{'LOCALAPPDATA':str(self.root),'HERTA_DDS_CACHE_DIR':str(self.root/'dds')})
        patch.start();self.addCleanup(patch.stop)
        scene.enabled=True;policy.invalidate_export_cache_stats()
        self.cache=artifacts.DDSArtifactCache()
        self.key=self.cache.key('source',{'format':98})

    def test_master_toggle_controls_existing_dds_cache_instance(self):
        self.assertTrue(self.cache.put(self.key,valid_dds()))
        scene.enabled=False
        self.assertFalse(self.cache.enabled)
        self.assertIsNone(self.cache.get(self.key))
        self.assertFalse(self.cache.put(self.key,valid_dds()))
        target=self.root/'out.dds';target.write_bytes(b'existing')
        self.assertFalse(self.cache.restore(self.key,target))
        self.assertEqual(target.read_bytes(),b'existing')
        scene.enabled=True
        self.assertEqual(self.cache.get(self.key),valid_dds())

    def test_master_toggle_controls_analysis_reads_and_writes(self):
        self.assertTrue(analysis._store_cached_analysis('source',(4,4)))
        self.assertIsNotNone(analysis._get_cached_analysis('source'))
        scene.enabled=False
        with mock.patch.object(analysis,'_open_analysis_cache',side_effect=AssertionError('Disk opened while off')):
            self.assertIsNone(analysis._get_cached_analysis('source'))
            self.assertFalse(analysis._store_cached_analysis('source',(4,4)))

    def test_worker_policy_uses_main_thread_snapshot_only(self):
        scene.enabled=False;self.assertFalse(policy.is_export_cache_enabled())
        with mock.patch.object(policy,'_scene_cache_enabled',side_effect=AssertionError('RNA accessed in worker')):
            with ThreadPoolExecutor(max_workers=8) as pool:
                self.assertEqual(list(pool.map(lambda _:policy.is_export_cache_enabled(),range(32))),[False]*32)

    def test_clear_dds_preserves_unmanaged_and_exported_files(self):
        self.cache.put(self.key,valid_dds())
        unrelated=self.cache.root/'notes.txt';unrelated.write_text('user data',encoding='utf-8')
        export=self.root/'export.dds';export.write_bytes(valid_dds())
        scene.enabled=False
        self.assertEqual(self.cache.get_stats()['file_count'],1)
        result=self.cache.clear()
        self.assertEqual(result['file_count'],1);self.assertFalse(result['errors'])
        self.assertTrue(unrelated.exists());self.assertEqual(export.read_bytes(),valid_dds())

    def test_unified_stats_and_clear_cover_all_three_categories(self):
        preprocess_root=self.root/'preprocess';preprocess_root.mkdir()
        prefile=preprocess_root/'cache.blend';prefile.write_bytes(b'preprocess')
        pre=types.SimpleNamespace(
            get_cache_dir=lambda:str(preprocess_root),
            get_cache_stats=lambda:{'file_count':1 if prefile.exists() else 0,
                                    'total_size':prefile.stat().st_size if prefile.exists() else 0},
            clear_cache=lambda:(prefile.unlink() or 1),invalidate_index_cache=lambda:None)
        fake=types.ModuleType(PACKAGE+'.blueprint.preprocess_cache');fake.PreProcessCache=pre
        self.cache.put(self.key,valid_dds());analysis._store_cached_analysis('source',(4,4))
        with mock.patch.dict(sys.modules,{fake.__name__:fake}):
            stats=policy.get_export_cache_stats(force=True)
            self.assertEqual(stats['preprocess']['file_count'],1)
            self.assertEqual(stats['analysis']['file_count'],1)
            self.assertEqual(stats['dds']['file_count'],1)
            self.assertEqual(stats['total']['file_count'],3)
            result=policy.clear_export_caches()
            self.assertEqual(result['total'],3);self.assertFalse(result['errors'])
            self.assertEqual(policy.get_export_cache_stats(force=True)['total']['file_count'],0)

    def test_saved_property_identifiers_remain_and_display_names_change(self):
        tree=ast.parse((ROOT/'common'/'global_properties.py').read_text(encoding='utf-8'))
        cls=next(x for x in tree.body if isinstance(x,ast.ClassDef) and x.name=='GlobalProterties')
        methods={x.name for x in cls.body if isinstance(x,ast.FunctionDef)}
        self.assertNotIn('enable_preprocess_cache',methods)
        self.assertIn('enable_export_cache',methods)
        values={x.target.id:x.annotation for x in cls.body if isinstance(x,ast.AnnAssign) and isinstance(x.target,ast.Name)}
        self.assertEqual(next(x.value.value for x in values['enable_preprocess_cache'].keywords if x.arg=='name'),'启用导出缓存')
        self.assertEqual(next(x.value.value for x in values['expand_preprocess_cache'].keywords if x.arg=='name'),'展开导出缓存')
        ui=(ROOT/'ui'/'ui_panel_basic.py').read_text(encoding='utf-8')
        self.assertIn('text="导出缓存"',ui);self.assertIn('"ssmt.clear_preprocess_cache"',ui)


if __name__=='__main__':unittest.main()
