# -*- coding: utf-8 -*-

import bpy


def is_alembic_object(obj):
    """检查对象是否由Alembic缓存驱动"""
    if not obj or obj.type != 'MESH':
        return False
    for mod in obj.modifiers:
        if mod.type == 'MESH_SEQUENCE_CACHE':
            return True
    return False


def move_object_to_collection(obj, target_collection):
    """确保一个物体只存在于目标集合中"""
    if not obj or not target_collection:
        return
    for coll in list(obj.users_collection):
        coll.objects.unlink(obj)
    target_collection.objects.link(obj)


# 渲染方式（抖动/混合）由 surface_render_method 控制：4.2+ 为 DITHERED/BLENDED。
# blend_method 是透明累积 + alpha 处理模式，**任何版本都没有 DITHER 这个值**：
# ≤4.1 抖动档叫 HASHED（4.0 起短暂叫过 DITHER），4.2+ 把渲染方式搬到
# surface_render_method，blend_method 只留 OPAQUE/CLIP/HASHED/BLEND。
# 所以往 blend_method 写 'DITHER' 在 4.2+ 一律 TypeError（5.0 实机确认）。
_DITHER_RENDER_METHODS = ('DITHERED', 'DITHER')


def _enum_identifiers(obj, attr):
    """读属性当前支持的枚举标识符；不是枚举或属性不存在时返回 None。"""
    prop = getattr(obj, 'bl_rna', None)
    prop = prop.properties.get(attr) if prop else None
    if prop is None or prop.type != 'ENUM':
        return None
    return tuple(item.identifier for item in prop.enum_items)


def apply_dithered_transparency(material, legacy_blend_method='HASHED'):
    """把材质设为抖动透明（防半透明排序/边缘伪影，勿用 BLEND）。

    ``surface_render_method`` 存在时只写它（DITHERED）—— 5.x 会据此自动把
    ``blend_method`` 归到 HASHED，不要去写 blend_method（写 OPAQUE 会被忽略）。
    该属性不存在（4.1 及更早）时才回退到 ``blend_method`` 的抖动档 HASHED。
    返回实际生效的档位名，便于诊断。
    """
    render_method = getattr(material, 'surface_render_method', None)
    render_items = _enum_identifiers(material, 'surface_render_method')
    if render_method is not None and render_items:
        for candidate in _DITHER_RENDER_METHODS:
            if candidate in render_items:
                material.surface_render_method = candidate
                return material.surface_render_method

    blend_items = _enum_identifiers(material, 'blend_method')
    candidates = (legacy_blend_method,) + _DITHER_RENDER_METHODS + ('HASHED',)
    for candidate in candidates:
        if blend_items and candidate not in blend_items:
            continue
        try:
            material.blend_method = candidate
            return candidate
        except TypeError:
            continue
    return None


def disable_transparent_overlap(material):
    """关闭透明重叠提示：Blender 5.x 用 use_transparency_overlap，旧版用 show_transparent_back。"""
    if hasattr(material, 'use_transparency_overlap'):
        material.use_transparency_overlap = False
    elif hasattr(material, 'show_transparent_back'):
        material.show_transparent_back = False
