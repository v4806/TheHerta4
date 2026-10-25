from io import BytesIO
import importlib
from pathlib import Path
import struct
import sys
import tempfile
import types
import unittest
from unittest import mock
from PIL import Image, PngImagePlugin

import test_dds_conversion as base

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = '_native_encoder_validation'
for name, path in ((PACKAGE, ROOT), (PACKAGE + '.toolkit', ROOT/'toolkit')):
    package = types.ModuleType(name)
    package.__path__ = [str(path)]
    sys.modules[name] = package
native = importlib.import_module(PACKAGE+'.toolkit.dds_native_encoder')
cache_module = importlib.import_module(PACKAGE+'.toolkit.dds_artifact_cache')


def png(mode='RGBA', size=(8,8), info=None):
    stream = BytesIO()
    Image.new(mode, size).save(stream, format='PNG', pnginfo=info)
    return stream.getvalue()


def last_pixel(data):
    header = bytearray(data[:148])
    struct.pack_into('<II', header, 12, 1, 1)
    struct.pack_into('<I', header, 20, 16)
    struct.pack_into('<I', header, 28, 1)
    with Image.open(BytesIO(bytes(header)+data[-16:])) as image:
        return image.convert('RGBA').getpixel((0,0))


class NativeEncoderTests(unittest.TestCase):
    def setUp(self):
        if not native.backend_status()['available']:
            self.skipTest(native.backend_status()['reason'])
        self.temporary = tempfile.TemporaryDirectory(prefix='herta_native_promote_test_')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.cache = cache_module.DDSArtifactCache(self.root/'cache')
        self.source = self.root/'source.dds'  # actual PNG bytes, no alias required
        self.source.write_bytes(png())

    def test_formats_complete_chain_and_cache_skip_encoding(self):
        for fmt, expected in (('bc7_unorm',98),('bc7_unorm_srgb',99)):
            target = self.root/(fmt+'.dds')
            profile = native.try_convert_file(self.source,target,fmt,cache=self.cache)
            self.assertIsNotNone(profile)
            self.assertFalse(profile['cache_hit'])
            data = target.read_bytes()
            self.assertEqual(struct.unpack_from('<I',data,128)[0],expected)
            self.assertEqual(struct.unpack_from('<I',data,28)[0],4)
            with mock.patch.object(native._library(),'herta_png_to_dds',side_effect=AssertionError('Encoding on cache hit')):
                result = native.try_convert_file(self.source,self.root/('copy_'+target.name),fmt,cache=self.cache)
            self.assertTrue(result['cache_hit'])
            self.assertEqual((self.root/('copy_'+target.name)).read_bytes(),data)

    def test_linear_data_ignores_png_srgb_metadata_for_mips(self):
        image = Image.new('RGBA',(2,2),(0,0,0,255))
        image.putpixel((0,1),(255,255,255,255));image.putpixel((1,1),(255,255,255,255))
        info=PngImagePlugin.PngInfo();info.add(b'sRGB',bytes([0]))
        stream=BytesIO();image.save(stream,format='PNG',pnginfo=info);self.source.write_bytes(stream.getvalue())
        for fmt, expected in (('bc7_unorm',128),('bc7_unorm_srgb',188)):
            target=self.root/(fmt+'.dds')
            self.assertIsNotNone(native.try_convert_file(self.source,target,fmt,cache=False))
            self.assertLessEqual(abs(last_pixel(target.read_bytes())[0]-expected),2)

    def test_unsupported_inputs_and_existing_target_are_preserved(self):
        target=self.root/'existing.dds';target.write_bytes(b'user target')
        for data,fmt in ((png(size=(7,5)),'bc7_unorm'),(png(mode='I;16'),'bc7_unorm'),
                         (b'not PNG','bc7_unorm'),(png(),'r8g8b8a8_unorm')):
            self.source.write_bytes(data)
            self.assertIsNone(native.try_convert_file(self.source,target,fmt,cache=False))
            self.assertEqual(target.read_bytes(),b'user target')
        self.source.write_bytes(png())
        self.assertIsNone(native.try_convert_file(self.source,target,'bc7_unorm',cache=False,overwrite=False))
        self.assertEqual(target.read_bytes(),b'user target')

    def test_unavailable_backend_returns_fallback(self):
        with mock.patch.object(native,'_library',return_value=None):
            self.assertIsNone(native.try_convert_file(self.source,self.root/'out.dds','bc7_unorm',cache=False))


class ExportEntryIntegrationTests(NativeEncoderTests):
    def setUp(self):
        super().setUp()
        self.api=base.dds_conversion
        patch=mock.patch.dict(sys.modules,{base.PKG+'.toolkit.dds_artifact_cache':cache_module})
        patch.start();self.addCleanup(patch.stop)
        patch=mock.patch.object(self.api,'_native_dds_converter',return_value=native.try_convert_file)
        patch.start();self.addCleanup(patch.stop)
        patch=mock.patch.object(self.api,'_dds_artifact_store',self.cache)
        patch.start();self.addCleanup(patch.stop)
        self.props=types.SimpleNamespace(dds_use_custom_rules=False,dds_rules=[])
        self.api.clear_dds_conversion_cache()

    def test_single_entry_uses_native_without_texconv(self):
        target=self.root/'out.dds'
        with mock.patch.object(self.api,'find_texconv',return_value=None), \
             mock.patch.object(self.api.subprocess,'run',side_effect=AssertionError('Legacy encoder used')):
            ok,reason=self.api.convert_texture_to_dds(str(self.source),str(target),self.props,format_override='bc7_unorm')
        self.assertTrue(ok,reason);self.assertTrue(target.exists())

    def test_batch_entry_uses_native_and_progress_and_preserves_existing(self):
        target=self.root/'out.dds';existing=self.root/'existing.dds';existing.write_bytes(b'user target')
        jobs=[(1,str(self.source),str(target),{'format_override':'bc7_unorm'}),
              (2,str(self.source),str(existing),{'format_override':'bc7_unorm'})]
        progress=[]
        with mock.patch.object(self.api,'find_texconv',return_value=None), \
             mock.patch.object(self.api.subprocess,'run',side_effect=AssertionError('Legacy encoder used')):
            done=self.api.convert_new_textures_to_dds_batch(jobs,progress_callback=progress.append)
        self.assertEqual(done,{1});self.assertEqual(progress[-1],1)
        self.assertEqual(existing.read_bytes(),b'user target')

    def test_unsupported_native_job_uses_batch_legacy_fallback(self):
        self.source.write_bytes(png(mode='I;16'))
        reference=self.root/'reference.png';reference.write_bytes(png())
        output=self.root/'reference.dds'
        native.try_convert_file(reference,output,'bc7_unorm',cache=False)
        expected=output.read_bytes()
        def encode(command,**kwargs):
            directory=Path(command[command.index('-o')+1])
            for source in command[command.index('-y')+1:]:
                (directory/(Path(source).stem+'.dds')).write_bytes(expected)
            return types.SimpleNamespace(returncode=0,stdout='',stderr='')
        target=self.root/'out.dds'
        with mock.patch.object(self.api,'find_texconv',return_value='texconv.exe'), \
             mock.patch.object(self.api.subprocess,'run',side_effect=encode) as legacy:
            done=self.api.convert_new_textures_to_dds_batch([(1,str(self.source),str(target),{'format_override':'bc7_unorm'})])
        self.assertEqual(done,{1});self.assertEqual(legacy.call_count,1)
        self.assertEqual(target.read_bytes(),expected)


if __name__=='__main__':unittest.main()
