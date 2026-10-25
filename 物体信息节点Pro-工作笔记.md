# 物体信息节点 Pro 工作笔记

## 目标
新增「物体信息 Pro」节点（`SSMTNode_Object_Info_Pro`）：一个节点支持多物体列表 +
添加选中物体 / 移除 / 清空按钮（样式对照跳过部件节点）；除此之外导出行为与现有
物体信息节点（`SSMTNode_Object_Info`）完全一致。

## 验收标准
1. 节点菜单「物体基础」下出现「物体信息 Pro」，可加入蓝图。
2. UI：物体列表（行内 X 删除）+ 添加选中物体 / 移除 / 清空；列表为空时等同普通物体信息节点。
3. 导出：Pro 节点上的 N 个物体各自生成一条与普通物体信息节点相同的处理链（重命名、
   切换、形态键、材质、顶点组、直出、嵌套蓝图都照常）；导出进度高亮正常。
4. 预处理 copy 改名 / 物体改名同步 / 同步选择 / 组预览 / 后处理扫描 对 Pro 生效。

## 关键机制（调研结论）
- 链式遍历入口：`BlueprintExportHelper.collect_connected_start_nodes` → `ChainTraverser.traverse_all_chains`；
  MultiFile_Export 已在遍历器里按 `object_list` 展开多物体，Pro 照此实现。
- `ProcessingChain` 只带 object_name / original_object_name / virtual_object_name / source_node；
  `model.ProcessingChain.get_export_object_name` 只在 source_node 精确等于 `SSMTNode_Object_Info` 时重算
  virtual name，Pro 的 idname 不同 → 直接用 chain.object_name，无需改。
- `ObjectPrefixHelper.build_virtual_object_name_for_node(entry)` 只 getattr
  object_name / object_prefix / prefix_separator → Pro 列表项可直接当参数用。
- 同步模块 `sync.py` 用 `_iter_node_object_references` 抽象；MultiFile 返回 object_list，Pro 照做。

## 进度
- [x] 调研完成
- [x] 备份 18 个文件 → D:\Program Files\agent\deepseek\file-backups\object-info-pro-20261025-075034（SHA256 已核对）
- [ ] node_obj.py 实现
- [ ] 导出链路改造
- [ ] 配套机制改造
- [ ] 验证

## 决策
- 列表为空 = 单物体模式（完全等同原节点）；列表非空 = 多物体模式（导出忽略节点自身 object_name）。
- 添加选中物体时若列表为空且节点已选单物体，先把该物体迁入列表，避免丢失。
- 清空列表同时清空节点自身 object_name/object_id，回到空节点状态。
- [x] node_obj.py 实现（ObjectInfoNodeMixin + Pro 节点 + 条目视图 + 三个操作符）
- [x] 导出链路改造（export_helper 起点展开、preprocess 三处、model legacy、直出高亮）
- [x] 配套机制（sync / node_menu / node_nest / node_base / node_swap / 后处理扫描 / 前缀工具 / 形态键工具 / Velo 桥）
- [x] 回归：headless 主测试 22/22、下游节点 8/8、输入框 14/14 全过
- [x] 实机 GUI 截图：C:\Users\master\AppData\Local\Temp\th4_pro_gui_final3.png（Blender 5.2.2 隔离实例）

## 最终结构与关键决策（补记）
- 关键坑：Blender 不允许「继承已注册的 Node 类再注册」→ 普通节点会无法创建。
  最终用 ObjectInfoNodeMixin 共享逻辑，普通节点与 Pro 节点都直接继承 SSMTNodeBase。
- 条目视图 ObjectInfoEntryView：bl_idname 固定为 SSMTNode_Object_Info，属性直通列表项，
  name 带 #index 保证唯一；导出入口 collect_connected_start_nodes 把 Pro 展开成多个视图，
  因此链式遍历与下游可视节点零改动。
- 用户反馈修正：输入框（搜索框/吸管）选中的物体自动加入列表（不再有单物体模式）；
  列表活动项切换时输入框同步；清空后空节点不产出导出起点；
  旧数据（列表空但 node.object_name 有值）仍回退成单节点视图。
- 既有测试失配（非本次引入）：tests/test_custom_material_assign_*.py 等 6+ 个测试的
  node_postprocess_material 桩缺 resolve_keyswap_key（备份文件里同样有该 import）。
