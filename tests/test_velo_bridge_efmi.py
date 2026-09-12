# -*- coding: utf-8 -*-
import importlib.util
import re
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]


def _load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _fake_bpy():
    module = types.ModuleType('bpy')
    module.types = types.SimpleNamespace(Node=object, Operator=object, PropertyGroup=object)
    prop = lambda **_kwargs: None
    props = types.ModuleType('bpy.props')
    props.StringProperty = prop
    props.BoolProperty = prop
    props.IntProperty = prop
    props.FloatProperty = prop
    props.CollectionProperty = prop
    module.props = props
    return module


class VeloBridgeEFMITests(unittest.TestCase):
    def test_non_ascii_draw_variables_are_replaced_everywhere(self):
        fake_bpy = _fake_bpy()
        with mock.patch.dict(sys.modules, {'bpy': fake_bpy, 'bpy.props': fake_bpy.props}):
            bridge = _load_module(
                '_test_velo_bridge', ROOT / 'TheHerta4_Velo_Bridge' / '__init__.py'
            )

        source = (
            'global $draw_component_4_丝袜下身 = 1\n'
            '[CommandListProcessToggles]\n'
            '$draw_component_4_丝袜下身 = ($swapkey0 == 1)\n'
            '[Draw]\n'
            'if $DRAW_COMPONENT_4_丝袜下身\n'
            '    ; Draw object "Component 4 丝袜下身"\n'
            'endif\n'
        )
        result = bridge._normalize_draw_variables(source)
        variables = re.findall(r'\$draw_[a-z_0-9]+', result, re.IGNORECASE)
        self.assertEqual(len({value.casefold() for value in variables}), 1)
        self.assertTrue(all(value.isascii() for value in variables))
        self.assertIn('Component 4 丝袜下身', result)
        self.assertNotIn('$draw_component_4_丝袜下身', result.casefold())

    def test_explicit_efmi_selection_keeps_all_blueprint_objects(self):
        adapter = _load_module(
            '_test_efmi_selection', ROOT / 'TheHerta4_Velo_Bridge' / 'efmi_selection.py'
        )

        class FakeMerger:
            __dataclass_fields__ = {
                key: object()
                for key in ('collection', 'component_id', 'force_object_name', 'allow_empty_components')
            }

            def import_objects_from_collection(self):
                raise AssertionError('default collection collector was used')

        original = FakeMerger.import_objects_from_collection
        core = types.ModuleType('object_merger')
        core.TempObject = lambda **kwargs: types.SimpleNamespace(**kwargs)
        core.copy_object = lambda _context, obj, **_kwargs: obj
        export_package = types.ModuleType(
            'velo_tools.games.arknights_endfield._efmi_core.blender_export'
        )
        export_package.__path__ = []
        export_package.object_merger = core
        export_module = types.ModuleType(export_package.__name__ + '.blender_export')
        export_module.ObjectMergerEFMI = FakeMerger
        stubs = {
            export_package.__name__: export_package,
            export_module.__name__: export_module,
        }
        prefix = ''
        for part in export_package.__name__.split('.')[:-1]:
            prefix = part if not prefix else prefix + '.' + part
            package = types.ModuleType(prefix)
            package.__path__ = []
            stubs.setdefault(prefix, package)

        collection = object()
        body = types.SimpleNamespace(name='Component 4 Body', velo_mmd_text='mapping')
        neck = types.SimpleNamespace(name='Component 4 Neck')
        component = types.SimpleNamespace(id=4, objects=[])
        merger = types.SimpleNamespace(
            collection=collection,
            force_object_name='',
            component_id=-1,
            extracted_object=types.SimpleNamespace(components=[None] * 5),
            context=None,
            components=[component],
            allow_empty_components=False,
        )
        with mock.patch.dict(sys.modules, stubs):
            with adapter.explicit_efmi_objects(collection, [body, neck]):
                FakeMerger.import_objects_from_collection(merger)

        self.assertEqual([item.name for item in component.objects], [body.name, neck.name])
        self.assertIs(FakeMerger.import_objects_from_collection, original)


class SwapPanelEFMITests(unittest.TestCase):
    @staticmethod
    def _load_panel():
        fake_bpy = _fake_bpy()
        package_name = '_test_swap_panel.blueprint'
        package = types.ModuleType(package_name)
        package.__path__ = []
        base = types.ModuleType(package_name + '.node_postprocess_base')

        class Base:
            @classmethod
            def split_anim_driver_block_content(cls, content):
                return '', content

            @classmethod
            def split_auto_appended_tail_content(cls, content):
                return content, ''

        base.SSMTNode_PostProcess_Base = Base
        stubs = {'bpy': fake_bpy, package_name: package, base.__name__: base}
        with mock.patch.dict(sys.modules, stubs):
            return _load_module(
                package_name + '.node_postprocess_swap_panel',
                ROOT / 'blueprint' / 'node_postprocess_swap_panel.py',
            )

    def test_repeated_efmi_sections_are_merged_without_deduplication(self):
        panel_module = self._load_panel()
        panel = panel_module.SSMTNode_PostProcess_SwapPanel()
        with tempfile.TemporaryDirectory() as folder:
            ini = Path(folder) / 'mod.ini'
            ini.write_text(
                '[Constants]\nglobal $vertex_count = 10\n'
                '[Present]\nrun = First\n'
                '[constants]\nglobal $bones_count = 20\n'
                '[Present]\nrun = First\nrun = Second\n',
                encoding='utf-8',
            )
            sections = panel._read_ini_to_ordered_dict(str(ini))[0]

        self.assertEqual(
            sections['[Constants]'],
            ['global $vertex_count = 10', 'global $bones_count = 20'],
        )
        self.assertEqual(sections['[Present]'].count('run = First'), 2)
        self.assertTrue(
            panel_module._is_efmi_ini_sections(
                {'[CommandList]': [r'run = CommandList\efmiv1\RegisterMod']}
            )
        )


if __name__ == '__main__':
    unittest.main()
