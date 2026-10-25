import array
import ast
from concurrent.futures import ThreadPoolExecutor
import ctypes
import hashlib
import importlib.util
from pathlib import Path
import struct
import sys
import threading
import types
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('_geometry_hash_test', ROOT/'common'/'geometry_hash.py')
geometry = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = geometry
spec.loader.exec_module(geometry)


def legacy_extract(obj):
    mesh = obj.data
    raw = []
    count = len(mesh.vertices)
    raw.append(count)
    if count:
        coords = array.array('f', [0.0] * (count * 3))
        mesh.vertices.foreach_get('co', coords)
        raw.append(coords.tobytes())
    group_count = len(obj.vertex_groups)
    raw.append(group_count)
    for group in obj.vertex_groups:
        raw.append(group.name)
    if count and group_count:
        for index, vertex in enumerate(mesh.vertices):
            for group in vertex.groups:
                raw.append(index)
                raw.append(group.group)
                raw.append(group.weight)
    raw.append(len(mesh.uv_layers))
    for layer in mesh.uv_layers:
        count = len(layer.data)
        raw.append(count)
        if count:
            uvs = array.array('f', [0.0] * (count * 2))
            layer.data.foreach_get('uv', uvs)
            raw.append(uvs.tobytes())
    return raw


def legacy_hash(raw):
    if raw is None:
        return None
    digest = hashlib.md5()
    for value in raw:
        if isinstance(value, bytes):
            digest.update(value)
        elif isinstance(value, str):
            digest.update(value.encode('utf-8'))
        elif isinstance(value, int):
            digest.update(struct.pack('i', value))
        elif isinstance(value, float):
            digest.update(struct.pack('f', value))
    return digest.hexdigest()


class Collection(list):
    def __init__(self, items, values, prop):
        super().__init__(items)
        self.values, self.prop = array.array('f', values), prop

    def foreach_get(self, prop, target):
        assert prop == self.prop
        target[:] = self.values


def object_fixture(vertices=17, memberships=4, uvs=2):
    groups = [types.SimpleNamespace(group=index, weight=(index+1)/(memberships+1))
              for index in range(memberships)]
    verts = [types.SimpleNamespace(groups=groups if index % 3 else []) for index in range(vertices)]
    coords = [float(i % 37) / 17 for i in range(vertices*3)]
    uv_layers = [types.SimpleNamespace(data=Collection([None]*(vertices*2),
                [float((i+layer)%19)/18 for i in range(vertices*4)], 'uv')) for layer in range(uvs)]
    return types.SimpleNamespace(data=types.SimpleNamespace(vertices=Collection(verts, coords, 'co'),uv_layers=uv_layers),
                                 vertex_groups=[types.SimpleNamespace(name=name) for name in ('骨骼🌟','body','左手','right')])


class GeometryHashTests(unittest.TestCase):
    def test_hashes_match_original_for_empty_sparse_and_multilayer_data(self):
        for count, memberships, layers in ((0,0,0),(1,0,0),(17,4,2),(31,1,3),(1024,4,1)):
            obj = object_fixture(count,memberships,layers)
            original = legacy_hash(legacy_extract(obj))
            current = geometry.extract_object_hash_data(obj)
            self.assertEqual(geometry.hash_object_data(current), original)
            self.assertEqual(geometry.hash_object_data(current,native=False), original)

    def test_weight_order_and_signed_zero_preserved(self):
        columns = geometry.WeightColumns(array.array('I',[2,0,1]),array.array('i',[7,2,5]),array.array('f',[-0.0,1.0,0.125]))
        expected = b''.join(struct.pack('iif',*record) for record in ((0,7,-0.0),(0,2,1.0),(2,5,.125)))
        self.assertEqual(bytes(geometry.pack_weight_columns(columns)),expected)
        self.assertEqual(bytes(geometry.pack_weight_columns(columns,native=False)),expected)

    def test_original_mixed_hash_input_remains_supported(self):
        raw=[1,b'abc','骨骼',-7,.75,True,object()]
        self.assertEqual(geometry.hash_object_data(raw),legacy_hash(raw))
        self.assertIsNone(geometry.hash_object_data(None))

    def test_repeated_objects_extracted_once_and_output_order_preserved(self):
        first,second=object_fixture(),object_fixture(8)
        objects={'a':first,'alias':first,'b':second,'missing':None}
        draws=[types.SimpleNamespace(get_blender_obj_name=lambda name=name:name) for name in ('a','b','alias','a','missing')]
        lookup_calls=[];extract_calls=[];main=threading.get_ident()
        def lookup(name):
            self.assertEqual(threading.get_ident(),main);lookup_calls.append(name);return objects[name]
        def extract(obj):
            self.assertEqual(threading.get_ident(),main);extract_calls.append(obj)
            return geometry.extract_object_hash_data(obj)
        hashes,sources,profile=geometry.prepare_object_hashes(draws,lookup,extractor=extract,workers=16)
        self.assertEqual(hashes,[legacy_hash(legacy_extract(objects[name])) if objects[name] else None
                                 for name in ('a','b','alias','a','missing')])
        self.assertEqual(len(extract_calls),2);self.assertEqual(len(lookup_calls),4)
        self.assertEqual(profile['reused_objects'],2);self.assertIs(sources[2],first)

    def test_data_changes_affect_hash(self):
        obj=object_fixture();before=geometry.hash_object_data(geometry.extract_object_hash_data(obj))
        obj.data.vertices[1].groups[0].weight=.875
        after=geometry.hash_object_data(geometry.extract_object_hash_data(obj))
        self.assertNotEqual(before,after)
        self.assertEqual(after,legacy_hash(legacy_extract(obj)))

    def test_objects_sharing_mesh_but_different_group_names_remain_distinct(self):
        first=object_fixture()
        second=types.SimpleNamespace(data=first.data,vertex_groups=[types.SimpleNamespace(name='different')])
        objects={'first':first,'second':second}
        draws=[types.SimpleNamespace(get_blender_obj_name=lambda name=name:name) for name in objects]
        hashes,_,profile=geometry.prepare_object_hashes(draws,objects.get)
        self.assertNotEqual(hashes[0],hashes[1]);self.assertEqual(profile['unique_objects'],2)
        self.assertEqual(hashes,[legacy_hash(legacy_extract(obj)) for obj in objects.values()])

    def test_installed_submesh_entry_calls_new_pipeline(self):
        tree=ast.parse((ROOT/'common'/'submesh_model.py').read_text(encoding='utf-8'))
        source=next(node for node in tree.body if isinstance(node,ast.ClassDef) and node.name=='SubMeshModel')
        methods=[node for node in source.body if isinstance(node,ast.FunctionDef)
                 and node.name in ('_precompute_object_hashes','_extract_object_raw_data')]
        cls=ast.ClassDef(name='Subject',bases=[],keywords=[],body=methods,decorator_list=[])
        module=ast.fix_missing_locations(ast.Module(body=[cls],type_ignores=[]))
        obj=object_fixture();namespace={'prepare_object_hashes':geometry.prepare_object_hashes,
            'extract_object_hash_data':geometry.extract_object_hash_data,'json':__import__('json'),
            'ObjUtils':types.SimpleNamespace(get_obj_by_name=lambda name:obj)}
        exec(compile(module,str(ROOT/'common'/'submesh_model.py'),'exec'),namespace)
        subject=namespace['Subject']();subject.drawcall_model_list=[types.SimpleNamespace(get_blender_obj_name=lambda:'obj')]*2
        with mock.patch('builtins.print') as printed:hashes,sources=subject._precompute_object_hashes()
        self.assertEqual(hashes,[legacy_hash(legacy_extract(obj))]*2)
        self.assertEqual(sources,[obj,obj]);self.assertIn('GeometryHashProfile',printed.call_args.args[0])

    def test_unavailable_native_backend_keeps_hash(self):
        obj=object_fixture();raw=geometry.extract_object_hash_data(obj)
        with mock.patch.object(geometry,'_native_library',return_value=None):
            self.assertEqual(geometry.hash_object_data(raw),legacy_hash(legacy_extract(obj)))

    def test_bad_columns_rejected(self):
        for columns in (geometry.WeightColumns(array.array('I',[2]),array.array('i',[1]),array.array('f',[1])),
                        geometry.WeightColumns(array.array('I',[0]),array.array('i',[1]),array.array('f',[1])),
                        geometry.WeightColumns(array.array('I',[1]),array.array('i',[1]),array.array('f',[]))):
            for native in (True,False):
                with self.assertRaises(ValueError):geometry.pack_weight_columns(columns,native=native)

    def test_native_concurrency_and_no_partial_write_on_invalid_count(self):
        dll=geometry._native_library()
        if dll is None:self.skipTest('Native DLL unavailable on this platform')
        columns=geometry.WeightColumns(array.array('I',[2,1]),array.array('i',[2,4,6]),array.array('f',[.25,.75,1]))
        expected=bytes(geometry.pack_weight_columns(columns))
        with ThreadPoolExecutor(max_workers=32) as pool:
            self.assertTrue(all(value==expected for value in pool.map(lambda _:bytes(geometry.pack_weight_columns(columns)),range(128))))
        counts=array.array('I',[4]);output=(ctypes.c_ubyte*36)(*[77]*36)
        self.assertEqual(dll.herta_pack_weight_records(counts.buffer_info()[0],1,columns.groups.buffer_info()[0],
                         columns.weights.buffer_info()[0],3,output,len(output)),0)
        self.assertEqual(bytes(output),bytes([77])*36)


if __name__=='__main__':unittest.main()
