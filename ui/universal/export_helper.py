
from ...blueprint.model import BluePrintModel
from ...common.draw_call_model import DrawCallModel
from ...common.submesh_model import SubMeshModel
from ...common.drawib_model import DrawIBModel
from ...blueprint.node_datatype import reset_datatype_override_log

class ExportHelper:
    pass

    @staticmethod
    def _build_source_obj_unique_str_count(blueprint_model:BluePrintModel) -> dict[str, int]:
        source_obj_unique_str_map:dict[str,set[str]] = {}

        for draw_call_model in blueprint_model.ordered_draw_obj_data_model_list:
            source_obj_name = draw_call_model.source_obj_name or draw_call_model.get_blender_obj_name()
            if not source_obj_name:
                continue
            unique_str = draw_call_model.get_workspace_unique_str()
            source_obj_unique_str_map.setdefault(source_obj_name, set()).add(unique_str)

        return {
            source_obj_name: len(unique_str_set)
            for source_obj_name, unique_str_set in source_obj_unique_str_map.items()
        }

    @staticmethod
    def _get_merge_weld_groups_safe() -> list:
        """读取蓝图运行时焊接组；接口未就绪（任务 B 未完成）时返回空列表。"""
        try:
            from ...blueprint.export_helper import BlueprintExportHelper
            getter = getattr(BlueprintExportHelper, "get_merge_weld_groups", None)
            if not callable(getter):
                return []
            groups = getter()
            return list(groups) if groups else []
        except Exception as error:
            print(f"[合并顶点] 读取焊接组失败，已跳过焊接: {error}")
            return []

    @staticmethod
    def _build_weld_spec(draw_call_model_list:list[DrawCallModel], merge_weld_groups:list):
        """按 IB 内物体名与焊接组取交集，生成 SubMeshModel 的 weld_spec。

        物体名匹配统一用 draw_call_model.get_blender_obj_name()（source_obj_name 优先）。
        同一 IB 命中多个节点时合并物体名，并取最小阈值（更保守）。
        """
        if not merge_weld_groups:
            return None

        ib_object_names = {
            draw_call_model.get_blender_obj_name()
            for draw_call_model in draw_call_model_list
            if draw_call_model.get_blender_obj_name()
        }
        if not ib_object_names:
            return None

        weld_object_names = set()
        weld_distance = None
        for group in merge_weld_groups:
            if not isinstance(group, dict):
                continue
            group_object_names = set(group.get("object_names") or ())
            intersection = ib_object_names & group_object_names
            if not intersection:
                continue
            try:
                group_distance = float(group.get("weld_distance", 1e-6))
            except (TypeError, ValueError):
                group_distance = 1e-6
            if group_distance <= 0.0:
                continue
            weld_object_names |= intersection
            weld_distance = (
                group_distance if weld_distance is None
                else min(weld_distance, group_distance)
            )

        if not weld_object_names or weld_distance is None:
            return None
        return {
            "object_names": weld_object_names,
            "weld_distance": weld_distance,
        }

    @staticmethod
    def parse_submesh_model_list_from_blueprint_model(blueprint_model:BluePrintModel) -> list[SubMeshModel]:
        '''
        从蓝图中解析出一个Submesh Model列表
        如果是Submesh可以直接导出的游戏，例如EFMI，则调用处拿到后直接导出
        如果是Submesh需要组合成DrawIB级别再导出，例如米游、Unity系列游戏，则调用处拿到后再进行整合
        这样拆分流程更加清晰，逻辑更容易理解
        '''
        submesh_model_list:list[SubMeshModel] = []
        source_obj_unique_str_count = ExportHelper._build_source_obj_unique_str_count(blueprint_model)
        has_multi_file_export_nodes = len(blueprint_model.multi_file_export_nodes) > 0

        # 根据唯一标识符，把相同的DrawCallModel分在一起，形成SubMeshModel
        draw_call_model_dict:dict[str,list[DrawCallModel]] = {}

        # 拿到BlueprintModel后，开始解析SubMeshModel列表
        for draw_call_model in blueprint_model.ordered_draw_obj_data_model_list:
            # 获取独立标识
            unique_str = draw_call_model.get_workspace_unique_str()

            # 根据unique_str，加入到字典中，这样每个unique_str都对应一个DrawCallModel列表，用于初始化SubMeshModel
            draw_call_model_list = draw_call_model_dict.get(unique_str,[])
            draw_call_model_list.append(draw_call_model)
            draw_call_model_dict[unique_str] = draw_call_model_list

        # 根据draw_call_model_dict，初始化SubMeshModel列表
        merge_weld_groups = ExportHelper._get_merge_weld_groups_safe()
        for unique_str, draw_call_model_list in draw_call_model_dict.items():
            submesh_model = SubMeshModel(
                drawcall_model_list=draw_call_model_list,
                source_obj_unique_str_count=source_obj_unique_str_count,
                has_multi_file_export_nodes=has_multi_file_export_nodes,
                weld_spec=ExportHelper._build_weld_spec(
                    draw_call_model_list, merge_weld_groups
                ),
            )
            submesh_model_list.append(submesh_model)
        
        return submesh_model_list

    @staticmethod
    def parse_drawib_model_list_from_blueprint_model(blueprint_model:BluePrintModel,combine_ib:bool) -> list[DrawIBModel]:
        '''
        从蓝图中解析出一个DrawIB Model列表
        适用于米游、Unity等等常见的需要将多个SubMesh组合成一个DrawIB进行导出的游戏
        '''
        reset_datatype_override_log()

        submesh_model_list = ExportHelper.parse_submesh_model_list_from_blueprint_model(blueprint_model)
        return ExportHelper.parse_drawib_model_list_from_submesh_model_list(
            submesh_model_list=submesh_model_list,
            combine_ib=combine_ib,
        )

    @staticmethod
    def parse_drawib_model_list_from_submesh_model_list(
        submesh_model_list:list[SubMeshModel],
        combine_ib:bool,
    ) -> list[DrawIBModel]:
        drawib_model_list:list[DrawIBModel] = []

        # 先把Submesh Model按照DrawIB分在一起
        draw_ib_submesh_model_list_dict:dict[str,list[SubMeshModel]] = {}
        for submesh_model in submesh_model_list:
            draw_ib = submesh_model.match_draw_ib
            tmp_submesh_model_list = draw_ib_submesh_model_list_dict.get(draw_ib,[])
            tmp_submesh_model_list.append(submesh_model)
            draw_ib_submesh_model_list_dict[draw_ib] = tmp_submesh_model_list

        # 随后直接用SubmeshModelList来初始化DrawIBModel
        for draw_ib, submesh_model_list in draw_ib_submesh_model_list_dict.items():
            drawib_model = DrawIBModel(submesh_model_list=submesh_model_list, combine_ib=combine_ib)
            drawib_model_list.append(drawib_model)

        return drawib_model_list
