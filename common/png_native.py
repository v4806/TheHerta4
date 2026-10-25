"""Optional byte-exact PNG decoder and pooled RGB features; safe in workers."""
import ctypes as ct
import os
from pathlib import Path
import threading
import time

_lib=None
_checked=False
_lock=threading.Lock()

def _library():
    global _lib,_checked
    if os.environ.get('HERTA_NATIVE_PNG_ANALYSIS','1')=='0':return None
    if _checked:return _lib
    with _lock:
        if not _checked:
            try:
                library=ct.CDLL(str(Path(__file__).parent/'native'/'bc7f'/'herta_png_analysis_windows_x64.dll'))
                library.herta_png_analysis_abi.restype=ct.c_uint
                if library.herta_png_analysis_abi()!=1:raise ValueError('PNG analysis ABI')
                library.herta_png_decode.argtypes=[ct.c_char_p,ct.c_size_t,ct.POINTER(ct.c_void_p),ct.POINTER(ct.c_uint),ct.POINTER(ct.c_uint)]
                library.herta_png_decode.restype=ct.c_int
                library.herta_rgba_pool.argtypes=[ct.c_void_p,ct.c_uint,ct.c_uint,ct.c_uint,ct.c_void_p,ct.c_size_t]
                library.herta_rgba_pool.restype=ct.c_int
                library.herta_png_free.argtypes=[ct.c_void_p]
                library.herta_png_free.restype=None
                _lib=library
            except (OSError,AttributeError,ValueError):pass
            finally:_checked=True
    return _lib

def analyze_png(file_path,grid=None):
    """(RGBA array or pooled float64 RGB, dimensions, decode seconds, pool seconds)."""
    lib=_library()
    if lib is None:return None
    import numpy as np
    pointer=ct.c_void_p();width=ct.c_uint();height=ct.c_uint()
    started=time.perf_counter()
    try:
        data=Path(file_path).read_bytes()
        if not lib.herta_png_decode(data,len(data),ct.byref(pointer),ct.byref(width),ct.byref(height)):return None
        decoded_at=time.perf_counter()
        if grid is not None:
            result=np.empty((min(grid,height.value),min(grid,width.value),3),dtype=np.float64)
            if not lib.herta_rgba_pool(pointer,width.value,height.value,grid,result.ctypes.data,result.size):return None
        else:
            raw=(ct.c_uint8*(width.value*height.value*4)).from_address(pointer.value)
            result=np.ctypeslib.as_array(raw).reshape(height.value,width.value,4).copy()
        return result,(width.value,height.value),decoded_at-started,time.perf_counter()-decoded_at
    except (OSError,ValueError,OverflowError):return None
    finally:
        if pointer.value:lib.herta_png_free(pointer)
