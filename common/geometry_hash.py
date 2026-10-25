"""Lossless object hash preparation, with optional native weight packing.

Blender reads happen only in extract_object_hash_data/prepare_object_hashes,
on the caller's main thread. Worker jobs operate on owned arrays and bytes.
"""
import array
from concurrent.futures import ThreadPoolExecutor
import ctypes
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import struct
import threading
from time import perf_counter

_native = None
_native_checked = False
_native_lock = threading.Lock()
_record = struct.Struct('iif')
_integer = struct.Struct('i')
_float = struct.Struct('f')


@dataclass
class WeightColumns:
    counts: array.array
    groups: array.array
    weights: array.array


def _native_library():
    global _native, _native_checked
    if os.environ.get('HERTA_GEOMETRY_NATIVE', '1') == '0':
        return None
    if _native_checked:
        return _native
    with _native_lock:
        if not _native_checked:
            try:
                if os.name != 'nt' or ctypes.sizeof(ctypes.c_void_p) != 8:
                    return None
                dll = ctypes.CDLL(str(Path(__file__).with_name('native') / 'herta_geometry_windows_x64.dll'))
                dll.herta_geometry_abi.argtypes = []
                dll.herta_geometry_abi.restype = ctypes.c_uint
                if dll.herta_geometry_abi() != 1:
                    raise ValueError('Geometry ABI mismatch')
                fn = dll.herta_pack_weight_records
                fn.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p,
                               ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p, ctypes.c_size_t]
                fn.restype = ctypes.c_int
                _native = dll
            except (OSError, AttributeError, ValueError):
                _native = None
            finally:
                _native_checked = True
    return _native


def pack_weight_columns(columns, native=True):
    counts, groups, weights = columns.counts, columns.groups, columns.weights
    if counts.typecode != 'I' or groups.typecode != 'i' or weights.typecode != 'f':
        raise ValueError('Unexpected weight column types')
    if any(values.itemsize != 4 for values in (counts, groups, weights)) or _record.size != 12:
        raise ValueError('Expected 32-bit native fields')
    count = len(groups)
    if len(weights) != count:
        raise ValueError('Weight count mismatch')
    if not count:
        if any(counts):
            raise ValueError('Weight count mismatch')
        return b''
    dll = _native_library() if native else None
    if dll is not None:
        output = (ctypes.c_ubyte * (count * 12))()
        ok = dll.herta_pack_weight_records(counts.buffer_info()[0], len(counts),
                                          groups.buffer_info()[0], weights.buffer_info()[0],
                                          count, output, len(output))
        if not ok:
            raise ValueError('Native weight pack rejected inconsistent columns')
        # hashlib accepts this owned buffer directly, without a second copy.
        return output
    if sum(counts) != count:
        raise ValueError('Weight count mismatch')
    output = bytearray()
    offset = 0
    pack, extend = _record.pack, output.extend
    for vertex, size in enumerate(counts):
        for index in range(offset, offset + size):
            extend(pack(vertex, groups[index], weights[index]))
        offset += size
    return output


def extract_object_hash_data(obj):
    mesh = obj.data
    count = len(mesh.vertices)
    raw = [count]
    if count:
        coords = array.array('f', [0.0]) * (count * 3)
        mesh.vertices.foreach_get('co', coords)
        raw.append(coords.tobytes())
    group_count = len(obj.vertex_groups)
    raw.append(group_count)
    raw.extend(group.name for group in obj.vertex_groups)
    if count and group_count:
        counts, groups, weights = array.array('I'), array.array('i'), array.array('f')
        add_count, add_group, add_weight = counts.append, groups.append, weights.append
        for vertex in mesh.vertices:
            memberships = vertex.groups
            add_count(len(memberships))
            for group in memberships:
                add_group(group.group)
                add_weight(group.weight)
        raw.append(WeightColumns(counts, groups, weights))
    raw.append(len(mesh.uv_layers))
    for layer in mesh.uv_layers:
        size = len(layer.data)
        raw.append(size)
        if size:
            uvs = array.array('f', [0.0]) * (size * 2)
            layer.data.foreach_get('uv', uvs)
            raw.append(uvs.tobytes())
    return raw


def hash_object_data(raw, native=True):
    if raw is None:
        return None
    digest = hashlib.md5()
    for item in raw:
        if isinstance(item, bytes):
            digest.update(item)
        elif isinstance(item, str):
            digest.update(item.encode('utf-8'))
        elif isinstance(item, int):
            digest.update(_integer.pack(item))
        elif isinstance(item, float):
            digest.update(_float.pack(item))
        elif isinstance(item, WeightColumns):
            digest.update(pack_weight_columns(item, native=native))
    return digest.hexdigest()


def prepare_object_hashes(draw_calls, lookup, extractor=extract_object_hash_data, workers=8):
    started = perf_counter()
    sources, unique_raw, indices = [], [], []
    names, identities = {}, {}
    extract_s = 0.0
    for draw in draw_calls:
        name = draw.get_blender_obj_name()
        if name not in names:
            names[name] = lookup(name)
        source = names[name]
        sources.append(source)
        if source is None:
            indices.append(None)
            continue
        identity = source.as_pointer() if hasattr(source, 'as_pointer') else id(source)
        if identity not in identities:
            stamp = perf_counter()
            data = extractor(source)
            extract_s += perf_counter() - stamp
            identities[identity] = len(unique_raw)
            unique_raw.append(data)
        indices.append(identities[identity])
    stamp = perf_counter()
    count = len(unique_raw)
    actual_workers = min(max(1, int(workers)), count) if count else 0
    if actual_workers > 1:
        with ThreadPoolExecutor(max_workers=actual_workers) as pool:
            hashes = list(pool.map(hash_object_data, unique_raw))
    else:
        hashes = [hash_object_data(data) for data in unique_raw]
    hash_s = perf_counter() - stamp
    result = [hashes[index] if index is not None else None for index in indices]
    weights = sum(len(item.groups) for raw in unique_raw for item in raw if isinstance(item, WeightColumns))
    profile = {'objects': len(sources), 'unique_objects': count,
               'reused_objects': sum(index is not None for index in indices) - count,
               'weight_records': weights, 'workers': actual_workers,
               'native_pack': _native_library() is not None, 'extract_s': extract_s,
               'hash_s': hash_s, 'total_s': perf_counter() - started}
    return result, sources, profile
