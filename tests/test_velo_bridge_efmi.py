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


class _VeloStyleFormatter:
    @staticmethod
    def format_ini_swapvar(name):
        text = str(name or '').replace('$', '').replace('-', ' ').replace('.', ' ').replace('_', ' ')
        parts = (part.strip().lower() for part in text.split(' '))
        return '$swapvar_' + '_'.join(part for part in parts if part and part not in ('var', 'swap'))


class VeloBridgeEFMITests(unittest.TestCase):
    @staticmethod
    def _load_bridge():
        fake_bpy = _fake_bpy()
        with mock.patch.dict(sys.modules, {'bpy': fake_bpy, 'bpy.props': fake_bpy.props}):
            return _load_module(
                '_test_velo_bridge', ROOT / 'TheHerta4_Velo_Bridge' / '__init__.py'
            )

    def test_non_ascii_draw_variables_are_replaced_everywhere(self):
        bridge = self._load_bridge()

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

    def test_wwmi_and_efmi_use_their_own_formatter_modules(self):
        bridge = self._load_bridge()
        loaded = []

        def fake_import(module_name):
            loaded.append(module_name)
            return types.SimpleNamespace(TextFormatter=lambda: module_name)

        with mock.patch.object(bridge.importlib, 'import_module', side_effect=fake_import):
            wwmi = bridge._velo_text_formatter('WUTHERING')
            efmi = bridge._velo_text_formatter('ENDFIELD')

        self.assertIn('wuthering_waves._wwmi_core', wwmi)
        self.assertIn('arknights_endfield._efmi_core', efmi)
        self.assertEqual(loaded, [wwmi, efmi])
        with self.assertRaisesRegex(ValueError, '不支持此游戏'):
            bridge._velo_text_formatter('UNKNOWN')

    def test_wwmi_and_efmi_swap_variables_use_exact_blueprint_names(self):
        bridge = self._load_bridge()
        source = (
            '[Constants]\n'
            'global persist $swapvar_legs_outfit = 0\n'
            'global persist $swapvar_swapkey0 = 0\n'
            '[KeySwapLegsOutfit]\n'
            '$swapvar_legs_outfit = 0, 1\n'
            '[CommandListProcessToggles]\n'
            '$draw_body = ($SWAPVAR_LEGS_OUTFIT == 1) && ($swapvar_swapkey0 == 0)\n'
            '[Draw]\n'
            'if $swapvar_legs_outfit_extra == 1\n'
            'endif\n'
        )
        with mock.patch.object(bridge, '_velo_text_formatter', return_value=_VeloStyleFormatter()):
            for game_value in ('WUTHERING', 'ENDFIELD'):
                with self.subTest(game_value=game_value), tempfile.TemporaryDirectory() as folder:
                    ini = Path(folder) / 'mod.ini'
                    ini.write_text(source, encoding='utf-8')
                    bridge._restore_th4_swap_variable_names(
                        ini,
                        {101: 'Legs_Outfit', 102: 'swapkey0'},
                        game_value,
                    )
                    result = ini.read_text(encoding='utf-8')

                    self.assertIn('global persist $Legs_Outfit = 0', result)
                    self.assertIn('global persist $swapkey0 = 0', result)
                    self.assertIn('$Legs_Outfit = 0, 1', result)
                    self.assertIn('($Legs_Outfit == 1) && ($swapkey0 == 0)', result)
                    self.assertIn('$swapvar_legs_outfit_extra == 1', result)
                    self.assertNotIn('$swapvar_swapkey0', result.casefold())

    def test_efmi_swap_variable_format_collision_is_rejected(self):
        bridge = self._load_bridge()
        with tempfile.TemporaryDirectory() as folder:
            ini = Path(folder) / 'mod.ini'
            ini.write_text('global persist $swapvar_outfit = 0\n', encoding='utf-8')
            with mock.patch.object(bridge, '_velo_text_formatter', return_value=_VeloStyleFormatter()):
                with self.assertRaisesRegex(ValueError, '同一个变量'):
                    bridge._restore_th4_swap_variable_names(
                        ini,
                        {101: 'outfit', 102: 'swap_outfit'},
                        'ENDFIELD',
                    )

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

    def test_gui_only_guards_both_velo_and_wwmi_keyswap_names(self):
        panel_module = self._load_panel()
        panel = panel_module.SSMTNode_PostProcess_SwapPanel()
        panel.gui_only = True
        sections = {
            '[KeySwapSwapkey0]': ['condition = $object_detected == 1', 'key = 1', '$swapkey0 = 0, 1'],
            '[KeySwap_Swapkey1]': ['condition = $object_detected == 1', 'key = 2', '$swapkey1 = 0, 1'],
            '[CommandListSwap1]': ['key = 1'],
        }
        buttons = [
            {'var_names': ['$swapkey0']},
            {'var_names': ['$swapkey1']},
        ]
        panel._apply_gui_only_guards(sections, 'swp_test', buttons)
        self.assertIn('&& $swp_test_gui_only == 0', sections['[KeySwapSwapkey0]'][0])
        self.assertIn('&& $swp_test_gui_only == 0', sections['[KeySwap_Swapkey1]'][0])
        self.assertEqual(sections['[CommandListSwap1]'], ['key = 1'])

    def test_named_velo_keyswap_sections_feed_exact_variables_to_panel(self):
        panel_module = self._load_panel()
        panel = panel_module.SSMTNode_PostProcess_SwapPanel()
        with tempfile.TemporaryDirectory() as folder:
            ini = Path(folder) / 'mod.ini'
            ini.write_text(
                '[KeySwapLegsOutfit]\n'
                '; Legs\nkey = 1\ntype = cycle\n$Legs_Outfit = 0, 1, 2\n'
                '[KeySwap_CoatMode]\n'
                'key = 2\ntype = cycle\n$Coat_Mode = 0, 1\n'
                '[KeyHelp]\nkey = F1\n$panel_help = 0, 1\n',
                encoding='utf-8',
            )
            swaps = panel._parse_ini_key_swaps(str(ini))

        self.assertEqual([item['var_name'] for item in swaps], ['$Legs_Outfit', '$Coat_Mode'])
        self.assertEqual([item['option_count'] for item in swaps], [3, 2])
        self.assertEqual(panel._cycle_command_lines('$Legs_Outfit', 3)[0], 'if $Legs_Outfit == 0')


if __name__ == '__main__':
    unittest.main()
