"""Progress is emitted by actual completed files, never just by whole batches."""
from pathlib import Path
import tempfile,threading,types,unittest
from unittest import mock
import test_dds_conversion as base
from test_dds_artifact_cache import dds

class BatchProgressTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='herta_batch_progress_');self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.source=self.root/'source.png';self.source.write_bytes(b'source')
        self.api=base.dds_conversion
        self.api.clear_dds_conversion_cache()
        patch=mock.patch.object(self.api,'_artifact_store',return_value=None)
        patch.start();self.addCleanup(patch.stop)

    def jobs(self,fmt='bc7_unorm'):
        return [(i,str(self.source),str(self.root/f'{i}.dds'),{'format_override':fmt}) for i in (1,2)]

    def test_native_reports_fast_file_before_slow_first_input(self):
        first_started=threading.Event();release_first=threading.Event();progress=[]
        main_thread=threading.get_ident()
        def convert(source,target,fmt,**kwargs):
            if Path(target).stem=='1':
                first_started.set()
                if not release_first.wait(2):raise AssertionError('Progress waited for the whole batch')
            else:
                if not first_started.wait(2):raise AssertionError('First task did not start')
            Path(target).write_bytes(dds(0))
            return {'cache_hit':False}
        def report(count):
            self.assertEqual(threading.get_ident(),main_thread)
            if not progress:
                self.assertEqual(count,1)
                self.assertTrue((self.root/'2.dds').exists())
                self.assertFalse((self.root/'1.dds').exists())
                release_first.set()
            progress.append(count)
        with mock.patch.object(self.api,'find_texconv',return_value=None),mock.patch.object(self.api,'_native_dds_converter',return_value=convert):
            result=self.api.convert_new_textures_to_dds_batch(self.jobs(),report)
        self.assertEqual(result,{1,2});self.assertEqual(progress,[1,2])

    def test_legacy_reports_each_written_file(self):
        # Distinct stems allow both jobs in one texconv command.
        second=self.root/'second.png';second.write_bytes(b'second')
        jobs=self.jobs('r8g8b8a8_unorm');jobs[1]=(2,str(second),jobs[1][2],jobs[1][3])
        def encode(command,**kwargs):
            staging=Path(command[command.index('-o')+1])
            for source in command[command.index('-y')+1:]:
                (staging/(Path(source).stem+'.dds')).write_bytes(dds(0))
            return types.SimpleNamespace(returncode=0,stdout='',stderr='')
        progress=[]
        def report(count):
            self.assertEqual(count,sum((self.root/f'{i}.dds').exists() for i in (1,2)))
            progress.append(count)
        with mock.patch.object(self.api,'find_texconv',return_value='texconv.exe'),mock.patch.object(self.api,'_native_dds_converter',return_value=None),mock.patch.object(self.api.subprocess,'run',side_effect=encode),mock.patch.object(self.api,'existing_dds_matches_format',return_value=True):
            result=self.api.convert_new_textures_to_dds_batch(jobs,report)
        self.assertEqual(result,{1,2});self.assertEqual(progress,[1,2])

    def test_artifact_restore_reports_each_ready_file(self):
        first_started=threading.Event();release_first=threading.Event();progress=[]
        def restore(key,target,validator):
            if Path(target).stem=='1':
                first_started.set()
                if not release_first.wait(2):raise AssertionError('Restore progress waited for all files')
            else:
                if not first_started.wait(2):raise AssertionError('First restore did not start')
            Path(target).write_bytes(dds(0));return True
        store=types.SimpleNamespace(enabled=True,restore=restore)
        def report(count):
            if not progress:
                self.assertTrue((self.root/'2.dds').exists())
                self.assertFalse((self.root/'1.dds').exists())
                release_first.set()
            progress.append(count)
        with mock.patch.object(self.api,'_artifact_store',return_value=store),mock.patch.object(self.api,'_artifact_prepare',return_value=(store,'key')),mock.patch.object(self.api,'find_texconv',return_value='texconv.exe'),mock.patch.object(self.api,'_native_dds_converter',return_value=None):
            result=self.api.convert_new_textures_to_dds_batch(self.jobs(),report)
        self.assertEqual(result,{1,2});self.assertEqual(progress,[1,2])

if __name__=='__main__':unittest.main()
