"""Optional lossless geometry kernels; only extract_blend_layout reads RNA."""
import array
import ctypes as ct
import os
import sys
from pathlib import Path
import threading
from time import perf_counter
import numpy as np

_lib=None
_checked=False
_lock=threading.Lock()

class _Stats(ct.Structure):
    _fields_=[('max_influences',ct.c_uint64),('over_vertices',ct.c_uint64),('max_dropped',ct.c_double),('sample_count',ct.c_uint64),('samples',ct.c_uint64*3)]

def _library():
    global _lib,_checked
    if os.environ.get('HERTA_EXPORT_GEOMETRY_NATIVE','1')=='0':return None
    if _checked:return _lib
    with _lock:
        if not _checked:
            try:
                lib=ct.CDLL(str(Path(__file__).with_name('native')/'herta_export_geometry_windows_x64.dll'))
                lib.herta_export_geometry_abi.restype=ct.c_uint
                if lib.herta_export_geometry_abi()!=1:raise ValueError('Export geometry ABI')
                lib.herta_blend_layout.argtypes=[ct.c_void_p,ct.c_void_p,ct.c_void_p,ct.c_uint64,ct.c_uint64,ct.c_void_p,ct.c_uint64,ct.c_uint,ct.c_int,ct.c_uint,ct.c_void_p,ct.c_void_p,ct.POINTER(_Stats)]
                lib.herta_blend_layout.restype=ct.c_int
                lib.herta_stable_unique.argtypes=[ct.c_void_p,ct.c_uint64,ct.c_uint64,ct.c_void_p,ct.c_void_p,ct.POINTER(ct.c_uint64)]
                lib.herta_stable_unique.restype=ct.c_int
                _lib=lib
            except (OSError,AttributeError,ValueError):pass
            finally:_checked=True
    return _lib

def blend_layout_arrays(offsets,groups,weights,loops,channels,normalize=True,threads=2):
    if sys.version_info < (3,12):return None
    lib=_library()
    if lib is None:return None
    offsets=np.ascontiguousarray(offsets,dtype=np.uint64)
    groups=np.ascontiguousarray(groups,dtype=np.int64)
    weights=np.ascontiguousarray(weights,dtype=np.float64)
    loops=np.ascontiguousarray(loops,dtype=np.int64)
    if offsets.ndim!=1 or not len(offsets) or groups.ndim!=1 or weights.ndim!=1 or loops.ndim!=1:return None
    if len(groups)!=len(weights) or offsets[0]!=0 or offsets[-1]!=len(groups) or np.any(offsets[1:]<offsets[:-1]):return None
    if np.any(groups>0xffffffff) or not 1<=channels<=256:return None
    result_weights=np.empty((len(loops),channels),dtype=np.float32)
    result_indices=np.empty((len(loops),channels),dtype=np.uint32)
    stats=_Stats()
    ok=lib.herta_blend_layout(offsets.ctypes.data,groups.ctypes.data,weights.ctypes.data,len(offsets)-1,len(groups),loops.ctypes.data,len(loops),channels,normalize,threads,result_weights.ctypes.data,result_indices.ctypes.data,ct.byref(stats))
    if not ok:return None
    return {0:result_weights},{0:result_indices},{'max_influences':stats.max_influences,'over_vertices':stats.over_vertices,'max_dropped':stats.max_dropped,'samples':list(stats.samples)[:stats.sample_count]}

def extract_blend_layout(mesh,channels,normalize=True,loop_vertex_indices=None,threads=2):
    if sys.version_info < (3,12):return None
    if _library() is None:return None
    started=perf_counter()
    offsets=array.array('Q',[0]);groups=array.array('q');weights=array.array('d')
    append_offset,append_group,append_weight=offsets.append,groups.append,weights.append
    for expected,vertex in enumerate(mesh.vertices):
        if int(vertex.index)!=expected:return None
        for assignment in vertex.groups:
            try:weight=float(assignment.weight)
            except (TypeError,ValueError):weight=0.0
            try:group=int(assignment.group)
            except (TypeError,ValueError):group=-1
            if group>0xffffffff or group<-(1<<63):return None
            append_group(group);append_weight(weight)
        append_offset(len(groups))
    if loop_vertex_indices is None:
        loop_vertex_indices=np.empty(len(mesh.loops),dtype=np.int64)
        mesh.loops.foreach_get('vertex_index',loop_vertex_indices)
    extracted=perf_counter()
    result=blend_layout_arrays(np.frombuffer(offsets,dtype=np.uint64),np.frombuffer(groups,dtype=np.int64),np.frombuffer(weights,dtype=np.float64),loop_vertex_indices,channels,normalize,threads)
    if result is not None:
        print('[BlendLayoutProfile] vertices=%d records=%d threads=%d extract=%.3fs native=%.3fs' % (len(mesh.vertices),len(groups),threads,extracted-started,perf_counter()-extracted))
    return result

def stable_unique_rows(rows):
    if os.environ.get('HERTA_NATIVE_VERTEX_HASH','1')=='0':return None
    lib=_library()
    if lib is None:return None
    rows=np.ascontiguousarray(rows,dtype=np.uint8)
    if rows.ndim!=2 or not rows.shape[1]:return None
    first=np.empty(len(rows),dtype=np.int64);inverse=np.empty(len(rows),dtype=np.int64);count=ct.c_uint64()
    if not lib.herta_stable_unique(rows.ctypes.data,len(rows),rows.shape[1],first.ctypes.data,inverse.ctypes.data,ct.byref(count)):return None
    return first[:count.value],inverse
