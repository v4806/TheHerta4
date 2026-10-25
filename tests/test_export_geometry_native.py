import importlib.util
from pathlib import Path
import unittest
from unittest import mock
import numpy as np

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('_export_geometry_native_tests',ROOT/'common/export_geometry_native.py')
native=importlib.util.module_from_spec(spec);spec.loader.exec_module(native)

def reference(offsets,groups,weights,loops,channels,normalize=True):
    count=len(offsets)-1
    vw=np.zeros((count,channels),np.float32);vi=np.zeros((count,channels),np.uint32)
    stats=dict(max_influences=0,over_vertices=0,max_dropped=0.0,samples=[])
    for vertex in range(count):
        a,b=int(offsets[vertex]),int(offsets[vertex+1])
        diagnostic=[float(w) for w in weights[a:b] if np.isfinite(w) and w>0]
        stats['max_influences']=max(stats['max_influences'],len(diagnostic))
        if len(diagnostic)>channels:
            stats['over_vertices']+=1
            if len(stats['samples'])<3:stats['samples'].append(vertex)
            total=sum(diagnostic);kept=sum(sorted(diagnostic,reverse=True)[:channels])
            stats['max_dropped']=max(stats['max_dropped'],max(0.0,(total-kept)/total))
        valid=[(float(w),int(g)) for g,w in zip(groups[a:b],weights[a:b]) if g>=0 and np.isfinite(w) and w>0]
        valid.sort(key=lambda item:(-item[0],item[1]));valid=valid[:channels]
        if not valid:vw[vertex,0]=1;continue
        total=sum(w for w,g in valid);divisor=total if normalize and total>0 else 1
        for slot,(w,g) in enumerate(valid):vw[vertex,slot]=w/divisor;vi[vertex,slot]=g
    ow=np.zeros((len(loops),channels),np.float32);oi=np.zeros((len(loops),channels),np.uint32)
    for i,v in enumerate(loops):
        if 0<=v<count:ow[i]=vw[v];oi[i]=vi[v]
    return {0:ow},{0:oi},stats

class ExportGeometryNativeTests(unittest.TestCase):
    def setUp(self):
        if native._library() is None:self.skipTest('Native export geometry library unavailable')

    def test_blend_matches_reference_bytes_for_widths_and_threads(self):
        rng=np.random.default_rng(347)
        counts=rng.integers(0,17,10000);offsets=np.r_[0,np.cumsum(counts)].astype(np.uint64)
        groups=rng.integers(0,700,int(offsets[-1]),dtype=np.int64)
        weights=rng.random(len(groups)).astype(np.float32).astype(np.float64)
        weights[::23]=0.25;weights[::37]=np.nan;weights[::47]=-1;groups[::29]=-1
        loops=np.r_[rng.integers(0,len(counts),30000),-1,len(counts)].astype(np.int64)
        for width in (1,2,4,8,16):
            expected=reference(offsets,groups,weights,loops,width)
            for threads in (1,2,4,8):
                result=native.blend_layout_arrays(offsets,groups,weights,loops,width,threads=threads)
                self.assertEqual(result[0][0].tobytes(),expected[0][0].tobytes())
                self.assertEqual(result[1][0].tobytes(),expected[1][0].tobytes())
                self.assertEqual(result[2],expected[2])

    def test_non_normalized_weights_and_rigid_fallback(self):
        args=([0,0,4,7],[0,1,2,3,5,4,6],[0.25,0.25,0.25,0.25,np.inf,0,-0.1],[0,1,2,1],2)
        result=native.blend_layout_arrays(*args,normalize=False)
        expected=reference(*args,normalize=False)
        self.assertEqual(result[0][0].tobytes(),expected[0][0].tobytes())
        self.assertEqual(result[1][0].tobytes(),expected[1][0].tobytes())

    def test_invalid_arrays_rejected(self):
        self.assertIsNone(native.blend_layout_arrays([1,2],[0,1],[1,1],[0],4))
        self.assertIsNone(native.blend_layout_arrays([0,3,2],[0,1],[1,1],[0],4))
        self.assertIsNone(native.blend_layout_arrays([0,1],[1<<33],[1],[0],4))

    def test_old_python_sum_semantics_use_existing_fallback(self):
        with mock.patch.object(native.sys,'version_info',(3,11)):
            self.assertIsNone(native.extract_blend_layout(object(),4))
            self.assertIsNone(native.blend_layout_arrays([0],[],[],[],4))

    def test_stable_hash_matches_numpy_with_first_and_inverse(self):
        rng=np.random.default_rng(55)
        for width in (1,4,8,40,65):
            originals=rng.integers(0,256,(700,width),dtype=np.uint8)
            for count in (0,1,2000):
                rows=originals[rng.integers(0,700,count)]
                keys=np.ascontiguousarray(rows).view(np.dtype((np.void,width))).reshape(count)
                _,first,inverse=np.unique(keys,return_index=True,return_inverse=True)
                order=np.argsort(first);mapping=np.empty_like(order);mapping[order]=np.arange(len(order))
                actual=native.stable_unique_rows(rows)
                np.testing.assert_array_equal(actual[0],first[order])
                np.testing.assert_array_equal(actual[1],mapping[inverse])

if __name__=='__main__':unittest.main()
