"""Optional native PNG -> complete BC7 DDS encoder. No Blender API calls."""
import ctypes as ct
import hashlib
import os
from pathlib import Path
import struct
import tempfile
import threading
from time import perf_counter

from .dds_artifact_cache import DDSArtifactCache

_lib = None
_checked = False
_lock = threading.Lock()
_encoder_digest = None
_default_cache = None
_reason = ''


class _Profile(ct.Structure):
    _fields_ = [('decode_s', ct.c_double), ('mip_s', ct.c_double), ('encode_s', ct.c_double),
                ('width', ct.c_uint32), ('height', ct.c_uint32),
                ('levels', ct.c_uint32), ('source_srgb', ct.c_uint32)]


def _library():
    global _lib, _checked, _encoder_digest, _reason
    if os.environ.get('HERTA_NATIVE_DDS_ENCODER', '1') == '0':
        return None
    if _checked:
        return _lib
    with _lock:
        if not _checked:
            try:
                if os.name != 'nt' or ct.sizeof(ct.c_void_p) != 8:
                    _reason = 'Native DDS encoding requires Windows x64'
                    return None
                path = Path(__file__).resolve().parents[1] / 'common' / 'native' / 'bc7f' / 'herta_texture_windows_x64.dll'
                library = ct.CDLL(str(path))
                library.herta_texture_abi.argtypes = []
                library.herta_texture_abi.restype = ct.c_uint
                if library.herta_texture_abi() != 1:
                    raise ValueError('Native DDS ABI mismatch')
                library.herta_bc7f_init.argtypes = []
                library.herta_bc7f_init.restype = ct.c_uint
                if library.herta_bc7f_init() != 1:
                    raise ValueError('BC7 initialization failed')
                library.herta_png_to_dds.argtypes = [ct.c_char_p, ct.c_size_t, ct.c_uint, ct.c_int,
                    ct.c_uint, ct.c_uint, ct.POINTER(ct.c_void_p), ct.POINTER(ct.c_size_t), ct.POINTER(_Profile)]
                library.herta_png_to_dds.restype = ct.c_int
                library.herta_pipeline_free.argtypes = [ct.c_void_p]
                library.herta_pipeline_free.restype = None
                _encoder_digest = hashlib.sha256(path.read_bytes()).hexdigest()
                _lib = library
            except (OSError, AttributeError, ValueError) as exc:
                _reason = str(exc)
                _lib = None
            finally:
                _checked = True
    return _lib


def backend_status():
    return {'available': _library() is not None, 'reason': _reason,
            'encoder_sha256': _encoder_digest, 'cpu': 'x64 SSE2 baseline'}


def _valid_output(data, width, height, srgb):
    if len(data) < 148 or data[:4] != b'DDS ' or data[84:88] != b'DX10':
        return False
    if struct.unpack_from('<II', data, 12) != (height, width):
        return False
    if struct.unpack_from('<5I', data, 128)[:4] != (99 if srgb else 98, 3, 0, 1):
        return False
    count, expected = 1, 148
    w, h = width, height
    while True:
        expected += ((w + 3) // 4) * ((h + 3) // 4) * 16
        if w == 1 and h == 1:
            break
        w, h = max(1, w // 2), max(1, h // 2)
        count += 1
    return len(data) == expected and struct.unpack_from('<I', data, 28)[0] == count


def _write_target(target, data, overwrite):
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix='.herta_native_', suffix='.dds', dir=target.parent)
    try:
        with os.fdopen(handle, 'wb') as output:
            output.write(data)
        if overwrite:
            os.replace(temporary, target)
        elif os.name == 'nt':
            # Windows rename atomically fails if another task created target.
            os.rename(temporary, target)
        else:
            os.link(temporary, target)
            os.unlink(temporary)
        temporary = None
    finally:
        if temporary:
            try:
                os.unlink(temporary)
            except OSError:
                pass


def try_convert_file(source, target, fmt, cache=None, overwrite=True):
    """Profile on success, None for unsupported/unavailable/failed conversion.

    This function never grants overwrite permission; its caller already made
    the selected merge-node decision. Failures preserve the old target.
    """
    fmt = str(fmt).strip().lower()
    if fmt not in ('bc7_unorm', 'bc7_unorm_srgb'):
        return None
    library = _library()
    if library is None or (not overwrite and os.path.exists(target)):
        return None
    started = perf_counter()
    try:
        with open(source, 'rb') as stream:
            prefix = stream.read(33)
            if len(prefix) != 33 or prefix[:8] != b'\x89PNG\r\n\x1a\n' or prefix[12:16] != b'IHDR':
                return None
            width, height = struct.unpack_from('>II', prefix, 16)
            if not width or not height or width > 16384 or height > 16384:
                return None
            if width & (width - 1) or height & (height - 1):
                return None
            png = prefix + stream.read()
        srgb = fmt.endswith('_srgb')
        # texconv --ignore-srgb ignores WIC metadata for data/UNORM inputs.
        # For SRGB output preserve the actual source tagging/filter behavior.
        mip_srgb = -1 if srgb else 0
        global _default_cache
        if cache is None:
            if _default_cache is None:
                _default_cache = DDSArtifactCache()
            cache = _default_cache
        key = None
        if cache is not False and cache.enabled:
            recipe = {'backend': 'bc7f-png-memory-v1', 'encoder_sha256': _encoder_digest,
                      'format': 99 if srgb else 98, 'profile': 2, 'mip_srgb': mip_srgb, 'mips': 'full'}
            key = cache.key(hashlib.sha256(png).hexdigest(), recipe)
            data = cache.get(key)
            if data is not None and _valid_output(data, width, height, srgb):
                _write_target(target, data, overwrite)
                return {'backend': 'bc7f', 'cache_hit': True, 'decode_s': 0.0,
                        'mip_s': 0.0, 'encode_s': 0.0, 'total_s': perf_counter() - started}
        pointer, size, timing = ct.c_void_p(), ct.c_size_t(), _Profile()
        status = library.herta_png_to_dds(png, len(png), int(srgb), mip_srgb, 2, 0,
                                          ct.byref(pointer), ct.byref(size), ct.byref(timing))
        if status != 1:
            return None
        try:
            data = ct.string_at(pointer, size.value)
        finally:
            library.herta_pipeline_free(pointer)
        if not _valid_output(data, width, height, srgb):
            return None
        _write_target(target, data, overwrite)
        stored = cache.put(key, data) if key is not None else False
        return {'backend': 'bc7f', 'cache_hit': False, 'cache_stored': stored,
                **{name: getattr(timing, name) for name, _ in _Profile._fields_},
                'total_s': perf_counter() - started}
    except (OSError, ValueError, OverflowError, MemoryError):
        return None
