import bpy
import time


class BMTP_ShapeKeyUtils:
    
    @classmethod
    def apply_modifiers_for_object_with_shape_keys(cls, context, selected_modifiers, disable_armatures):
        if len(selected_modifiers) == 0:
            return (True, None)

        list_properties = []
        properties = ["interpolation", "mute", "name", "relative_key", "slider_max", "slider_min", "value", "vertex_group"]
        shapes_count = 0
        vert_count = -1
        start_time = time.time()
        
        contains_mirror_with_merge = False
        for modifier in context.object.modifiers:
            if modifier.name in selected_modifiers:
                if modifier.type == 'MIRROR' and hasattr(modifier, 'use_mirror_merge') and modifier.use_mirror_merge:
                    contains_mirror_with_merge = True

        disabled_armature_modifiers = []
        if disable_armatures:
            for modifier in context.object.modifiers:
                if modifier.name not in selected_modifiers and modifier.type == 'ARMATURE' and modifier.show_viewport:
                    disabled_armature_modifiers.append(modifier)
                    modifier.show_viewport = False
        
        if context.object.data.shape_keys:
            shapes_count = len(context.object.data.shape_keys.key_blocks)
        
        if shapes_count == 0:
            for modifier_name in selected_modifiers:
                bpy.ops.object.modifier_apply(modifier=modifier_name)
            return (True, None)
        
        original_object = context.view_layer.objects.active
        bpy.ops.object.select_all(action='DESELECT')
        original_object.select_set(True)
        
        bpy.ops.object.duplicate_move(OBJECT_OT_duplicate={"linked":False, "mode":'TRANSLATION'}, TRANSFORM_OT_translate={"value":(0, 0, 0), "orient_type":'GLOBAL', "orient_matrix":((1, 0, 0), (0, 1, 0), (0, 0, 1)), "orient_matrix_type":'GLOBAL', "constraint_axis":(False, False, False), "mirror":True, "use_proportional_edit":False, "proportional_edit_falloff":'SMOOTH', "proportional_size":1, "use_proportional_connected":False, "use_proportional_projected":False, "snap":False, "snap_target":'CLOSEST', "snap_point":(0, 0, 0), "snap_align":False, "snap_normal":(0, 0, 0), "gpencil_strokes":False, "cursor_transform":False, "texture_space":False, "remove_on_cancel":False, "release_confirm":False, "use_accurate":False})
        copy_object = context.view_layer.objects.active
        copy_object.select_set(False)
        
        context.view_layer.objects.active = original_object
        original_object.select_set(True)
        
        for i in range(0, shapes_count):
            key_b = original_object.data.shape_keys.key_blocks[i]
            properties_object = {p:None for p in properties}
            properties_object["name"] = key_b.name
            properties_object["mute"] = key_b.mute
            properties_object["interpolation"] = key_b.interpolation
            properties_object["relative_key"] = key_b.relative_key.name
            properties_object["slider_max"] = key_b.slider_max
            properties_object["slider_min"] = key_b.slider_min
            properties_object["value"] = key_b.value
            properties_object["vertex_group"] = key_b.vertex_group
            list_properties.append(properties_object)

        bpy.ops.object.shape_key_remove(all=True)
        for modifier_name in selected_modifiers:
            bpy.ops.object.modifier_apply(modifier=modifier_name)
        vert_count = len(original_object.data.vertices)
        bpy.ops.object.shape_key_add(from_mix=False)
        original_object.select_set(False)
        
        for i in range(1, shapes_count):
            curr_time = time.time()
            elapsed_time = curr_time - start_time

            # 显式重置选择集：duplicate_move 会复制所有被选中的物体。
            bpy.ops.object.select_all(action='DESELECT')
            context.view_layer.objects.active = copy_object
            copy_object.select_set(True)
            
            bpy.ops.object.duplicate_move(OBJECT_OT_duplicate={"linked":False, "mode":'TRANSLATION'}, TRANSFORM_OT_translate={"value":(0, 0, 0), "orient_type":'GLOBAL', "orient_matrix":((1, 0, 0), (0, 1, 0), (0, 0, 1)), "orient_matrix_type":'GLOBAL', "constraint_axis":(False, False, False), "mirror":True, "use_proportional_edit":False, "proportional_edit_falloff":'SMOOTH', "proportional_size":1, "use_proportional_connected":False, "use_proportional_projected":False, "snap":False, "snap_target":'CLOSEST', "snap_point":(0, 0, 0), "snap_align":False, "snap_normal":(0, 0, 0), "gpencil_strokes":False, "cursor_transform":False, "texture_space":False, "remove_on_cancel":False, "release_confirm":False, "use_accurate":False})
            tmp_object = context.view_layer.objects.active
            copy_object.select_set(False)
            
            # 不用 bpy.ops.object.shape_key_transfer()：它要求"除活动物体外恰好还有
            # 1 个可编辑网格物体被选中"，视图层选择一旦被污染就直接抛 RuntimeError
            # 中止整个导出。把第 i 个键值设 1.0、其余键 0.0，再 apply_mix 移除形态键，
            # 得到的就是第 i 个键烘进网格后的形状。
            tmp_key_blocks = tmp_object.data.shape_keys.key_blocks
            for key_index, key_block in enumerate(tmp_key_blocks):
                key_block.mute = False
                if key_index == i:
                    key_block.vertex_group = ""
                    key_block.slider_min = min(key_block.slider_min, 0.0)
                    key_block.slider_max = max(key_block.slider_max, 1.0)
                    key_block.value = 1.0
                else:
                    key_block.value = 0.0
            bpy.ops.object.shape_key_remove(all=True, apply_mix=True)

            
            for modifier_name in selected_modifiers:
                bpy.ops.object.modifier_apply(modifier=modifier_name)
            
            if vert_count != len(tmp_object.data.vertices):
                error_info_hint = ""
                if contains_mirror_with_merge:
                    error_info_hint = "There is mirror modifier with 'Merge' property enabled. This may cause a problem."
                if error_info_hint:
                    error_info_hint = "\n\nHint: " + error_info_hint
                error_info = ("Shape keys ended up with different number of vertices!\n"
                            "All shape keys needs to have the same number of vertices after modifier is applied.\n"
                            "Otherwise joining such shape keys will fail!%s" % error_info_hint)
                return (False, error_info)
    
            copy_object.select_set(False)
            context.view_layer.objects.active = original_object
            original_object.select_set(True)
            bpy.ops.object.join_shapes()
            original_object.select_set(False)
            context.view_layer.objects.active = tmp_object
            
            # 不用 bpy.ops.object.delete：它删除的是所有选中物体。
            tmp_mesh = tmp_object.data
            bpy.data.objects.remove(tmp_object, do_unlink=True)
            if tmp_mesh and tmp_mesh.users == 0:
                bpy.data.meshes.remove(tmp_mesh)
        
        context.view_layer.objects.active = original_object
        for i in range(0, shapes_count):
            key_b = context.view_layer.objects.active.data.shape_keys.key_blocks[i]
            key_b.name = list_properties[i]["name"]
            
        for i in range(0, shapes_count):
            key_b = context.view_layer.objects.active.data.shape_keys.key_blocks[i]
            key_b.interpolation = list_properties[i]["interpolation"]
            key_b.mute = list_properties[i]["mute"]
            key_b.slider_max = list_properties[i]["slider_max"]
            key_b.slider_min = list_properties[i]["slider_min"]
            key_b.value = list_properties[i]["value"]
            key_b.vertex_group = list_properties[i]["vertex_group"]
            rel_key = list_properties[i]["relative_key"]
        
            for j in range(0, shapes_count):
                key_brel = context.view_layer.objects.active.data.shape_keys.key_blocks[j]
                if rel_key == key_brel.name:
                    key_b.relative_key = key_brel
                    break
        
        original_object.select_set(False)
        context.view_layer.objects.active = copy_object
        copy_object.select_set(True)
        copy_mesh = copy_object.data
        bpy.data.objects.remove(copy_object, do_unlink=True)
        if copy_mesh and copy_mesh.users == 0:
            bpy.data.meshes.remove(copy_mesh)
        
        context.view_layer.objects.active = original_object
        context.view_layer.objects.active.select_set(True)
        
        if disable_armatures:
            for modifier in disabled_armature_modifiers:
                modifier.show_viewport = True
        
        return (True, None)


bmtp_shape_key_utils_list = ()
