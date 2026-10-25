import importlib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sys,tempfile,types,unittest
from unittest import mock
import numpy as np
from PIL import Image

ROOT=Path(__file__).resolve().parents[1]
PACKAGE='_png_native_analysis_tests'
for name,path in ((PACKAGE,ROOT),(PACKAGE+'.common',ROOT/'common')):
    module=types.ModuleType(name);module.__path__=[str(path)];sys.modules[name]=module
native=importlib.import_module(PACKAGE+'.common.png_native')
analysis=importlib.import_module(PACKAGE+'.common.texture_dedup')

class PNGAnalysisTests(unittest.TestCase):
    def setUp(self):
        if native._library() is None:self.skipTest('Native PNG DLL unavailable')
        self.temp=tempfile.TemporaryDirectory(prefix='herta_png_analysis_test_');self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)

    def test_raw_and_pooled_bytes_match_for_dimensions_and_channels(self):
        for mode in ('L','LA','RGB','RGBA','P'):
            for width,height in ((1,1),(31,17),(67,37)):
                with self.subTest(mode=mode,size=(width,height)):
                    rgba=np.random.default_rng(127).integers(0,256,(height,width,4),dtype=np.uint8)
                    image=Image.fromarray(rgba).convert(mode)
                    path=self.root/'source.png';image.save(path)
                    expected=np.asarray(image.convert('RGBA'))
                    np.testing.assert_array_equal(native.analyze_png(path)[0],expected)
                    np.testing.assert_array_equal(native.analyze_png(path,32)[0],analysis._similarity_pool(expected,32))

    def test_16_bit_and_invalid_inputs_are_rejected(self):
        path=self.root/'16bit.png';Image.fromarray(np.array([[1,65535]],dtype=np.uint16)).save(path)
        self.assertIsNone(native.analyze_png(path))
        path.write_bytes(b'not a png');self.assertIsNone(native.analyze_png(path))

    def test_worker_calls_are_deterministic(self):
        path=self.root/'parallel.png'
        Image.fromarray(np.random.default_rng(91).integers(0,256,(257,513,4),dtype=np.uint8)).save(path)
        expected=native.analyze_png(path,32)[0]
        with ThreadPoolExecutor(max_workers=16) as pool:
            for result in pool.map(lambda _:native.analyze_png(path,32)[0],range(48)):
                np.testing.assert_array_equal(result,expected)

    def test_similarity_analysis_worker_uses_no_blender(self):
        path=self.root/'worker.png';Image.new('RGBA',(67,37),(1,50,220,7)).save(path)
        with mock.patch.object(analysis,'_get_cached_analysis',return_value=None),mock.patch.object(analysis,'_store_cached_analysis'),mock.patch.object(analysis,'_decode_with_blender',side_effect=AssertionError('bpy in worker')):
            result=analysis.analyze_file(path,need_features=True,allow_blender=False)
        self.assertTrue(result['decoded']);self.assertIsNotNone(result['features'])

    def test_png_under_dds_extension_is_analyzed_without_blender(self):
        path=self.root/'misnamed.dds';Image.new('RGBA',(33,17),(10,20,30,0)).save(path,format='PNG')
        with mock.patch.object(analysis,'_get_cached_analysis',return_value=None),mock.patch.object(analysis,'_store_cached_analysis'),mock.patch.object(analysis,'_decode_with_blender',side_effect=AssertionError('bpy in worker')):
            result=analysis.analyze_file(path,need_features=True,allow_blender=False)
        self.assertTrue(result['decoded']);self.assertEqual(result['dims'],(33,17))

    def test_unknown_dds_extension_does_not_allow_blender_in_worker(self):
        path=self.root/'unsupported.dds';path.write_bytes(b'unknown container')
        with mock.patch.object(analysis,'_get_cached_analysis',return_value=None),mock.patch.object(analysis,'_decode_with_blender',side_effect=AssertionError('bpy in worker')):
            result=analysis.analyze_file(path,need_features=True,allow_blender=False)
        self.assertFalse(result['decoded']);self.assertIsNone(result['features'])

if __name__=='__main__':unittest.main()
