import ast
from contextvars import ContextVar
from functools import wraps
from pathlib import Path
import types,unittest

source=Path(__file__).resolve().parents[1]/'blueprint'/'node_postprocess_material.py'
tree=ast.parse(source.read_text(encoding='utf-8'))
scope=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='_material_lookup_scope')
cls=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name=='SSMTNode_PostProcess_MaterialBase')
method=next(n for n in cls.body if isinstance(n,ast.FunctionDef) and n.name=='find_object_by_mesh_name')
namespace={'ContextVar':ContextVar,'wraps':wraps,'_material_lookup_cache':ContextVar('test_scan_cache',default=None)}
exec(compile(ast.fix_missing_locations(ast.Module(body=[scope,method],type_ignores=[])),str(source),'exec'),namespace)

class Lookup:
    find_object_by_mesh_name=namespace['find_object_by_mesh_name']
    def __init__(self):
        self.candidates=[types.SimpleNamespace(name='original'),types.SimpleNamespace(name='copy')]
        self.resolutions=0
    def _find_object_by_mesh_name_uncached(self,name,object_filter=None):
        self.resolutions+=1
        return next((obj for obj in self.candidates if object_filter is None or object_filter(obj)),None)

class MaterialLookupTests(unittest.TestCase):
    def test_candidate_order_and_different_filters_are_preserved(self):
        obj=Lookup()
        @namespace['_material_lookup_scope']
        def run(self):
            self.assertion=None
            for _ in range(10):
                assert self.find_object_by_mesh_name('mesh').name=='original'
                assert self.find_object_by_mesh_name('mesh',lambda item:item.name=='copy').name=='copy'
                assert self.find_object_by_mesh_name('mesh',lambda item:False) is None
        run(obj)
        self.assertEqual(obj.resolutions,1)
        run(obj)
        self.assertEqual(obj.resolutions,2)

    def test_scope_is_cleared_after_failure(self):
        obj=Lookup()
        @namespace['_material_lookup_scope']
        def fail(self):
            self.find_object_by_mesh_name('mesh')
            raise RuntimeError('export failed')
        with self.assertRaises(RuntimeError):fail(obj)
        self.assertIsNone(namespace['_material_lookup_cache'].get())
        obj.candidates=[]
        self.assertIsNone(obj.find_object_by_mesh_name('mesh'))

if __name__=='__main__':unittest.main()
