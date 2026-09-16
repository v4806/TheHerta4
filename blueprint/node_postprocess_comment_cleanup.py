import bpy
import glob
import os
import re

from .node_postprocess_base import SSMTNode_PostProcess_Base
from .node_postprocess_material import SSMTNode_PostProcess_MaterialBase


class SSMT_OT_CommentCleanup_Refresh(bpy.types.Operator):
    """在已导出的 Mod 中按指定 INI 原地清理非 ASCII 文本。"""

    bl_idname = "ssmt.comment_cleanup_refresh"
    bl_label = "应用清理到Mod"
    bl_description = "按指定 INI 原地转换中文和非 ASCII 文本，无需重新导出整个 Mod"
    bl_options = {'REGISTER', 'INTERNAL'}

    node_name: bpy.props.StringProperty()

    def execute(self, context):
        space_data = getattr(context, "space_data", None)
        if space_data and space_data.type == 'NODE_EDITOR':
            tree = getattr(space_data, "edit_tree", None) or getattr(space_data, "node_tree", None)
            if tree:
                node = tree.nodes.get(self.node_name)
                if node and node.bl_idname == 'SSMTNode_PostProcess_CommentCleanup':
                    ini_path = (
                        getattr(node, "ini_file_path", "") or ""
                    ).strip() or getattr(node, "last_mod_ini_path", "")
                    if not ini_path or not os.path.isfile(ini_path):
                        self.report({'WARNING'}, "未找到目标 ini：请先导出一次 mod，或在节点「INI文件」中指定")
                        return {'CANCELLED'}

                    try:
                        ok = node.execute_postprocess(
                            os.path.dirname(ini_path),
                            _in_place=True,
                            _ini_path=ini_path,
                        )
                    except Exception as exc:
                        self.report({'ERROR'}, f"清理失败，请查看控制台日志: {exc}")
                        return {'CANCELLED'}

                    if ok:
                        self.report({'INFO'}, f"已按指定 INI 完成配置文件清理: {os.path.basename(ini_path)}")
                        return {'FINISHED'}
                    self.report({'ERROR'}, "清理失败，请查看控制台日志")
                    return {'CANCELLED'}

        self.report({'WARNING'}, "无法找到配置文件清理节点")
        return {'CANCELLED'}


class SSMTNode_PostProcess_CommentCleanup(SSMTNode_PostProcess_Base):
    bl_idname = "SSMTNode_PostProcess_CommentCleanup"
    bl_label = "配置文件清理"
    bl_description = "导出时将生成的 INI 文件中的中文和其他非 ASCII 文本转换为英文标识"

    ini_file_path: bpy.props.StringProperty(name="INI文件", subtype='FILE_PATH', default="")
    last_mod_ini_path: bpy.props.StringProperty(
        name="上次导出INI",
        description="最近一次导出时生成的 Mod INI 路径，供原地清理按钮使用",
        default="",
        options={'HIDDEN'},
    )

    def draw_buttons(self, context, layout):
        layout.label(text="将INI中的中文和非ASCII文本转换为英文标识", icon="TEXT")
        box_top = layout.box()
        box_top.operator(
            "ssmt.comment_cleanup_refresh",
            text="应用清理到Mod（原地更新）",
            icon='FILE_TICK',
        ).node_name = self.name
        box_top.prop(self, "ini_file_path", text="INI文件（可选，留空用上次导出）")
        box_top.label(text="指定 INI 后可直接清理已生成的 Mod，无需重新导出", icon='INFO')

    @staticmethod
    def _clean_ini_file(ini_path):
        try:
            with open(ini_path, "r", encoding="utf-8-sig", newline="") as handle:
                content = handle.read()
            cleaned = SSMTNode_PostProcess_MaterialBase._replace_non_ascii_runs(content)
            if cleaned == content:
                return False, 0
            with open(ini_path, "w", encoding="utf-8", newline="") as handle:
                handle.write(cleaned)
            return True, sum(1 for char in content if ord(char) > 127)
        except (OSError, UnicodeError) as exc:
            print(f"配置注释清理读取/写入失败 {ini_path}: {exc}")
            return False, 0

    def execute_postprocess(self, mod_export_path, _in_place=False, _ini_path=None):
        target_ini = (_ini_path or getattr(self, "ini_file_path", "") or "").strip()
        if _in_place:
            if not target_ini or not os.path.isfile(target_ini):
                print(f"目标 INI 不存在，无法执行原地清理: {target_ini}")
                return False
            target_ini = os.path.abspath(target_ini)
            ini_paths = [target_ini]
            self.last_mod_ini_path = target_ini
        else:
            ini_paths = glob.glob(os.path.join(mod_export_path, "**", "*.ini"), recursive=True)
            root_ini_paths = glob.glob(os.path.join(mod_export_path, "*.ini"))
            if root_ini_paths:
                self.last_mod_ini_path = os.path.abspath(root_ini_paths[0])
            elif ini_paths:
                self.last_mod_ini_path = os.path.abspath(sorted(ini_paths)[0])

        changed_files = 0
        replaced_chars = 0
        for ini_path in ini_paths:
            changed, replaced = self._clean_ini_file(ini_path)
            if changed:
                changed_files += 1
                replaced_chars += replaced
        print(f"配置文件清理完成：处理 {changed_files} 个INI文件，替换 {replaced_chars} 个非ASCII字符。")
        return True


classes = (SSMT_OT_CommentCleanup_Refresh, SSMTNode_PostProcess_CommentCleanup)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
