# -*- coding: utf-8 -*-
"""FX 命名空间档案：HIMI（崩坏 3）→ HI3FX 的名字与能力契约。

覆盖：
* ``profile_for_logic`` 的游戏映射；
* HI3FX 的资源别名 / 命令列表 / 变量拼写（对照 SSMT 包里的
  ``3Dmigoto\\HI3\\Mods\\HI3FX``：``Resource\\HI3FX\\GlowMap``、
  ``$\\HI3FX\\h``、``CommandList\\HI3FX\\Run`` / ``Reset``）；
* HI3FX 的 ``Run`` 不复位变量 → 复位必须走 ``Reset``（本档案用
  ``reset_neutralises_variables`` 表达这条差异）；
* HI3FX 没有 ``SetTextures`` / ``SetFXBuffer`` / ``ColorShift`` 能力；
* 配置表命名空间探测与 ``Run`` 正则不误吃 ``Reset``。
"""

import importlib.util
import sys
import types
import unittest
from pathlib import Path


def _install_module(name, **attrs):
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


PKG = "_fx_namespace_test_pkg"
for package_name in (PKG, f"{PKG}.blueprint", f"{PKG}.common"):
    package = _install_module(package_name)
    package.__path__ = []

_install_module(
    f"{PKG}.common.logic_name",
    LogicName=types.SimpleNamespace(
        GIMI="GIMI", HIMI="HIMI", NTEMI="NTEMI", ZZMI="ZZMI", EFMI="EFMI"
    ),
)

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    f"{PKG}.blueprint.fx_namespace", ROOT / "blueprint" / "fx_namespace.py"
)
fx = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = fx
_spec.loader.exec_module(fx)


class FXNamespaceProfileTests(unittest.TestCase):
    def test_logic_mapping(self):
        self.assertIs(fx.profile_for_logic("HIMI"), fx.HI3FX)
        self.assertIs(fx.profile_for_logic("NTEMI"), fx.NTEMIFX)
        for logic in ("ZZMI", "GIMI", "EFMI", "HTMI", "", None):
            self.assertIs(fx.profile_for_logic(logic), fx.RABBITFX, logic)

    def test_current_profile_falls_back_without_global_config(self):
        """没有 GlobalConfig（测试替身/裁剪环境）时退回 RabbitFX，不抛异常。"""
        self.assertIs(fx.current_profile(), fx.RABBITFX)

    def test_hi3fx_names_follow_the_shipped_mod(self):
        """HI3FX 的名字必须与随包分发的 HI3FX.ini / HI3FX.Example.ini 一致。"""
        profile = fx.HI3FX
        self.assertEqual(profile.key, "HI3FX")
        self.assertEqual(profile.glow_ref, "Resource\\HI3FX\\GlowMap")
        self.assertEqual(profile.fxmap_ref, "Resource\\HI3FX\\FXMap")
        self.assertEqual(profile.run_line, "run = CommandList\\HI3FX\\Run")
        self.assertEqual(profile.reset_line, "run = CommandList\\HI3FX\\Reset")
        self.assertEqual(profile.resource_prefix, "Resource\\HI3FX\\")

    def test_hi3fx_has_no_settextures(self):
        """HI3FX 的 SetTextures 在默认不加载的 HI3FX.Remap.ini 里，档案不声明它。"""
        self.assertEqual(fx.HI3FX.set_textures_line, "")
        self.assertEqual(fx.RABBITFX.set_textures_line, "run = CommandList\\RabbitFX\\SetTextures")

    def test_hi3fx_drops_sync_and_colorshift(self):
        self.assertFalse(fx.HI3FX.supports_sync)
        self.assertFalse(fx.HI3FX.supports_colorshift)
        self.assertTrue(fx.HI3FX.supports_pro_injection)
        self.assertTrue(fx.RABBITFX.supports_sync)
        self.assertTrue(fx.RABBITFX.supports_colorshift)
        self.assertFalse(fx.NTEMIFX.supports_pro_injection)

    def test_param_spelling_only_differs_on_single_letter_channels(self):
        """RabbitFX 是 $…\\H/S/V，HI3FX 是小写 h/s/v；brightness/interpolate 都小写。"""
        self.assertEqual(fx.RABBITFX.param("h"), "$\\RabbitFX\\H")
        self.assertEqual(fx.RABBITFX.param("brightness"), "$\\RabbitFX\\brightness")
        self.assertEqual(fx.RABBITFX.param("interpolate"), "$\\RabbitFX\\interpolate")
        self.assertEqual(fx.HI3FX.param("h"), "$\\HI3FX\\h")
        self.assertEqual(fx.HI3FX.param("brightness"), "$\\HI3FX\\brightness")
        self.assertEqual(fx.HI3FX.param("interpolate"), "$\\HI3FX\\interpolate")

    def test_reset_semantics(self):
        """RabbitFX/NTEMIFX 的 Run 自带复位；HI3FX 必须调 Reset。"""
        self.assertTrue(fx.RABBITFX.reset_neutralises_variables)
        self.assertTrue(fx.NTEMIFX.reset_neutralises_variables)
        self.assertFalse(fx.HI3FX.reset_neutralises_variables)
        self.assertEqual(fx.RABBITFX.reset_line, "")
        self.assertEqual(fx.NTEMIFX.reset_line, "")

    def test_rabbitfx_refs_keep_the_legacy_spelling(self):
        """RabbitFX 档案保持既有产出（Glowmap 小写 m），不制造无谓 diff。"""
        self.assertEqual(fx.RABBITFX.glow_ref, "Resource\\RabbitFX\\Glowmap")
        self.assertEqual(fx.RABBITFX.fxmap_ref, "Resource\\RabbitFX\\FXMap")
        self.assertEqual(fx.RABBITFX.run_line, "run = CommandList\\RabbitFX\\Run")

    def test_ref_regex_ignores_null_and_matches_case_insensitively(self):
        profile = fx.HI3FX
        match = profile.glow_ref_re.match("resource\\hi3fx\\glowmap = ref Resource_Glowmap_5_Body")
        self.assertIsNotNone(match)
        self.assertEqual(match.group("name"), "Resource_Glowmap_5_Body")
        self.assertIsNotNone(profile.glow_ref_re.match("Resource\\HI3FX\\GlowMap = ref X"))
        self.assertIsNone(profile.glow_ref_re.match("Resource\\RabbitFX\\Glowmap = ref X"))

    def test_run_regex_does_not_swallow_reset(self):
        profile = fx.HI3FX
        self.assertIsNotNone(profile.run_line_re.match("run = CommandList\\HI3FX\\Run"))
        self.assertIsNotNone(profile.run_line_re.match("RUN = commandlist\\hi3fx\\run"))
        self.assertIsNone(profile.run_line_re.match("run = CommandList\\HI3FX\\Reset"))

    def test_hi3fx_declares_the_ttlmap_channel(self):
        """HI3FX v1.1 的第二个遮罩通道：``Resource\\HI3FX\\TTLMap``（抖动半透明）。

        与 ``FXMap`` 分工明确：``FXMap``（ps-t61）只做二值裁切，``TTLMap``
        （ps-t62）才是覆盖率、中间值走抖动。名字必须与
        ``Mods\\HI3FX\\HI3FX.ini`` 的 ``[ResourceTTLMap]`` + ``CommandListBind`` 一致。
        """
        self.assertEqual(fx.HI3FX.ttlmap_ref, "Resource\\HI3FX\\TTLMap")
        self.assertIsNotNone(fx.HI3FX.ttlmap_ref_re)
        match = fx.HI3FX.ttlmap_ref_re.match("resource\\hi3fx\\ttlmap = ref Resource_TTLMap_X")
        self.assertIsNotNone(match)
        self.assertEqual(match.group("name"), "Resource_TTLMap_X")
        self.assertIsNone(fx.HI3FX.ttlmap_ref_re.match("Resource\\HI3FX\\FXMap = ref X"))

    def test_only_hi3fx_has_a_ttlmap_channel(self):
        """RabbitFX / NTEMIFX 没有 TTLMap 这个概念 → 材质转资源不得产出该引用。

        这条是"其它逻辑行为完全不变"的档案侧护栏：``ttlmap_ref`` 为空串、
        ``ttlmap_ref_re`` 为 None，命名空间探测也不认 TTLMap 行。
        """
        for profile in (fx.RABBITFX, fx.NTEMIFX):
            self.assertEqual(profile.ttlmap_ref, "", profile.key)
            self.assertIsNone(profile.ttlmap_ref_re, profile.key)
            self.assertIsNone(
                profile.any_ref_re.match(f"Resource\\{profile.key}\\TTLMap = ref X"), profile.key
            )
        self.assertIsNotNone(
            fx.HI3FX.any_ref_re.match("Resource\\HI3FX\\TTLMap = ref Resource_TTLMap_X")
        )

    def test_detect_profiles_sees_a_ttlmap_only_table(self):
        """只写了 TTLMap 一行的配置表也要被认成 HI3FX（否则 pro 节点认不出命名空间）。"""
        sections = {
            "[TextureOverride_A]": ["Resource\\HI3FX\\TTLMap = ref Resource_TTLMap_X"],
        }
        self.assertEqual([profile.key for profile in fx.detect_profiles(sections)], ["HI3FX"])

    def test_detect_profiles_reports_every_namespace_in_the_table(self):
        sections = {
            "[TextureOverride_A]": [
                "Resource\\HI3FX\\GlowMap = ref Resource_Glowmap_5_Body",
                "run = CommandList\\HI3FX\\Run",
            ],
            "[TextureOverride_B]": [
                "Resource\\RabbitFX\\FXMap = ref Resource_FXMap_Body",
            ],
        }
        found = {profile.key for profile in fx.detect_profiles(sections)}
        self.assertEqual(found, {"HI3FX", "RabbitFX"})

    def test_detect_profiles_ignores_unrelated_sections(self):
        sections = {"[TextureOverride_A]": ["ps-t0 = Resource_DiffuseMap_Body"]}
        self.assertEqual(fx.detect_profiles(sections), [])


if __name__ == "__main__":
    unittest.main()
