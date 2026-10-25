import os,unittest
from unittest import mock
import test_texture_export_plan as base

class SourcePrewarmTests(unittest.TestCase):
    setUp=base.TextureExportPlanTests.setUp
    tearDown=base.TextureExportPlanTests.tearDown
    _source=base.TextureExportPlanTests._source
    _configure=base.TextureExportPlanTests._configure
    _registered=base.TextureExportPlanTests._registered

    def _analysis(self,**extra):
        return dict(dims=(8,8),features=base.dedup.similarity_features(base._pixels()),**extra)

    def test_registration_reuses_worker_result_without_reanalysis(self):
        path=self._source('source.png',b'source')
        self._configure(similarity_dedup=True)
        with mock.patch.object(base.dedup,'analyze_file',return_value=self._analysis()) as analyze:
            base.TextureExportPlan.prewarm_sources([path,path])
            self.assertEqual(analyze.call_count,1)
            self.assertFalse(analyze.call_args.kwargs['allow_blender'])
        with mock.patch.object(base.dedup,'compute_md5',side_effect=AssertionError('rehash')),mock.patch.object(base.dedup,'analyze_file',side_effect=AssertionError('reanalyze')):
            self.assertEqual(self._registered(path,'out.dds'),'out.dds')

    def test_missing_features_fall_back_to_main_thread(self):
        path=self._source('source.png',b'source')
        self._configure(similarity_dedup=True)
        with mock.patch.object(base.dedup,'analyze_file',return_value={'dims':None,'features':None}):
            base.TextureExportPlan.prewarm_sources([path])
        with mock.patch.object(base.dedup,'analyze_file',return_value=self._analysis()) as analyze:
            self._registered(path,'out.dds')
            self.assertEqual(analyze.call_count,1)
            self.assertNotIn('allow_blender',analyze.call_args.kwargs)

    def test_changed_source_invalidates_warmed_snapshot(self):
        path=self._source('source.png',b'source')
        self._configure(similarity_dedup=True)
        with mock.patch.object(base.dedup,'analyze_file',return_value=self._analysis()):
            base.TextureExportPlan.prewarm_sources([path])
            with open(path,'wb') as stream:stream.write(b'updated-content')
            with mock.patch.object(base.dedup,'compute_md5',wraps=base.dedup.compute_md5) as md5:
                self._registered(path,'out.dds')
                self.assertEqual(md5.call_count,1)

    def test_distinct_paths_with_identical_content_analyzed_once(self):
        first=self._source('first.png',b'same-content')
        second=self._source('second.png',b'same-content')
        self._configure(similarity_dedup=True)
        with mock.patch.object(base.dedup,'analyze_file',return_value=self._analysis(decoded=True,decode_seconds=0.25)) as analyze:
            base.TextureExportPlan.prewarm_sources([first,second])
            self.assertEqual(analyze.call_count,1)
        self.assertEqual(len(base.TextureExportPlan._warmed_sources),2)
        self._registered(first,'first.dds')
        self._registered(second,'second.dds')
        self.assertEqual(base.TextureExportPlan._profile['source_decode_count'],1)
        self.assertEqual(base.TextureExportPlan._profile['source_decode_s'],0.25)

if __name__=='__main__':unittest.main()
