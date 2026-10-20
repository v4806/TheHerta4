# ZZMI 骨骼合并（Merged Skeleton）计划书

> 状态：**已实现（骨架分组 + 组内统一骨架直拷 attach + v9 出现次槽位/每槽守卫，用户游戏内实测通过），Blender headless 端到端 [PASS]** · 最后更新：**2026-10-11（v19：⑬ 的「按来源拆分绘制 + 各回各家」已落地并通过单测，但**角色展示页仍未解**——展示页里我方段的 `draw` / `drawindexed` 不执行、画面不推进且什么都看不见，大世界一切正常；本次为存档提交，未解问题与全部取证手段见 ⑭ 与 §9）**
> 2026-10-10（v18：跨组走「按来源拆分绘制 + 各回各家」——按顶点组把合并网格的索引切成若干段，每段**放回该组自己的渲染段**里画，那里 `vs-cb1` 天然是当帧对象变换 ⇒ 不再依赖帧内绘制顺序、也不再需要校准矩阵；F4/F5/F7 三条「渲染段重算/写回」路线全部实测否决并已移除，校准 attach 改回直拷）
> 2026-08-24 去重/分组确认（已实现）；**2026-08-25 用户拍板：放弃 CB1 校准**：
> ① **单骨骼刚性部件（单权重物体）抓帧重合误并**——不同锚点骨（头顶/前额/后脑发饰/面部）在抓帧姿态下矩阵逐位相同被并成一根；修复：刚性部件命中对追加加权质心门控（<0.05 米才合并），仅拆不并、误拆零代价。
> ② **palette 与渲染 cb1 逐物体 1:1 配对**（用户拍板）——palette 把顶点蒙皮到对象空间、渲染 VS 用 cb1 对象矩阵摆到世界；跨空间引用会被 cb1 摆错位置（身体组与头部组变换差 ≈0.5m，与"头发下沉/身体上移"症状吻合）。修复：**按渲染 cb1 对象变换分组，组内统一骨架**——每组一套全宽骨架，只直拷本组骨骼。**~~禁止跨组别骨骼合并~~（2026-10-10 修正：不再"禁止"，改为默认关闭的实验总开关 `cross_group_merged_vgmap_test`；关闭时与历史逐字节一致，开启后走 F1 跨组 attach，见下方 ⑤）**（跨组引用导出时大声报警，无校准的运行时槽位永不被写入 = 原点塌陷；**F2（2026-10-10）起跨组 attach 已带 cb1 校准**，见 ⑦——槽位由宿主组写入并换算，不再塌陷）。
> ③ **CB1 校准整体废弃（2026-08-25 用户拍板）**：校准版（捕获各组 cb1 + 外来骨骼经 `inv(cb1_组)×cb1_源×M` 校准乘写入）经多轮实测无法稳定（捕获段匹配脆弱、未捕获残留垃圾 CB、校准偏移越调越离谱），全部移除——不再有 cb1 捕获段/捕获源字段/校准 CS，attach 退化为纯直拷（`Toolset/zzmi_merged_skeleton_attach.hlsl`）。**2026-10-10 修正（F2，见 ⑦）**：本条的**公式**（`C = U_目标⁻¹×U_源`、`M' = C×M`）本身正确（已逐项复核，世界不变性成立），失败在**数据源**与**时序**——捕获段按 IB hash 匹配会抓到别的 pass 的常量块（同帧两 pass 差 8.2°），attach 挪到 `[Present]` 会让骨架比几何慢一帧。改为「各组部件自己的渲染 draw 处捕获 + attach 留在 deform pass 内」后重新启用，且**只作用于跨组 attach**（同组仍直拷）。
> ④ **未生成组件帧对齐（2026-08-26 修正）**：导出组件在每个 deform pass 即时 attach 当帧 palette，未生成组件继续走游戏原渲染的当帧 palette；两者无需双缓冲或 Present 重放。
> **⑤ 跨组骨骼合并 F1 落地（2026-10-10）**：复现件（`K:\SSMT-Package-master\3Dmigoto\ZZZ\Mods\SSMTGeneratedMod\洛克茜00\洛克茜.ini骨骼合并`）现场证据链钉死旧行为 = **被吸收的异组部件，其骨骼被写进自己组的骨架缓冲**（attach 段 `C0→G0` / 宿主渲染段绑 `G2`），而它的顶点已经并进宿主网格 ⇒ 那 624 行顶点的骨 id `{1,2}` 所在的 **G2 槽位 1/2 无人写入** ⇒ 读未初始化矩阵 ⇒ **部件整体消失**（与用户"3 合 1、同组件的显示、跨组的消失"逐条吻合）。F1 = **把跨组件的骨骼写进吸收它的宿主组缓冲**：新增 `_merged_cross_group_component_ids()`（按「引用骨 ∉ 本组合法槽位」判定跨组，再按 `[vg_offset, vg_offset+vg_count)` 反查归属部件）、`_merged_cross_attach_name()`、逐槽新增 `Dispatch = ceil(vg_count/64)` 的 `CustomShaderZZMIMergedSkeletonAttachX_C<i>_g<宿主组>_s<槽>` 段（`cs-t0` = 异组 palette、`cs-t1` = 异组 VgMap、`cs-u0` = **宿主组**骨架缓冲），并在每个 deform VB 段逐槽 `run` 它们；守卫 `_warn_cross_group_bone_references` 在开关开启时不再报「禁止」。（F2 已补齐对象空间换算，见 ⑦。）
> ⑥ **§3 的跨组实测口径（2026-10-10）**：开关已由「纯占位（`common/`、`ui/`、`blueprint/`、`utils/` 零引用）」变为**真开关**（F1/F2 消费点在 `ui/universal/zzmi.py` + 校准 CS 在 `Toolset/`），`tests/test_zzmi_cross_group_switch.py` 的 `ALLOWED_REFERENCING_FILES` 已登记该文件；新增单测 `ZZMICrossGroupAttachTests`（10，含 4 条 F2）+ `CrossGroupConsumerContractTests`（4）。
> **⑦ 跨组校准 F2 落地（2026-10-10）**：跨组 attach 走**校准版 CS**（`Toolset/zzmi_merged_skeleton_attach_calibrated.hlsl`）：用**源组**与**宿主组**的渲染 cb1（对象→世界）算换算矩阵，把源部件的 palette 骨骼从源对象空间换算进宿主对象空间后写进宿主组缓冲。逐项：
>   a. **公式**（刚体，与 2026-08-25 那版逐项一致、已复核）：cb1 行向量语义 `world = object·R + t`（rows 0-2 旋转行 w=0、row 3 = (t,1)），palette 列向量 `object = Rm·bind + tm`（12 floats，平移在各行 w）⇒ `C_rot = Rd·Rsᵀ`、`C_t = Rd·(t_src − t_dst)`、`Rm' = C_rot·Rm`、`tm' = C_rot·tm + C_t`（世界不变性：`U_dst·(M'·p) ≡ U_src·(M·p)`）。
>   b. **数据源（与失败版的本质差别）**：每组一份 `[ResourceZZCb1_G<N>]`，在**各组部件自己的渲染 draw** 处 `ResourceZZCb1_G<N> = copy vs-cb1 unless_null`（last-wins；同组各部件写的是同一矩阵）。绝不退回 `[TextureOverrideIB_*] + hash = <IB>` 的按 hash 捕获（同一 IB 被多 pass 匹配，实测同帧差 8.2°）。捕获点在 `add_unity_vs_texture_override_ib_sections`（每个 DrawIB 只在其第一条子网格处追加一次）。
>   c. **时序**：attach 留在每个 deform pass 内**即时**执行（与 F1 相同），**不**挪 `[Present]`（失败版挪过去后骨架比几何慢一帧 ⇒ 运动时逐帧错位）。cb1 捕获来自渲染块 ⇒ deform 段内的 attach 用的是**上一帧**的 cb1（~~本开关目标场景 C 是常量，故全帧率下精确~~ **2026-10-10 修正：这条判断是错的**——C 是常量只保证"静止/同一姿态下"精确；角色四肢每帧都在动，帧间混用（当帧 palette × 上一帧 C）会直接表现为**运动时错位**。**~~已由 ⑩ F4 修复~~（2026-10-10 二次修正：F4 被实测推翻并回退——宿主渲染 draw 排在本帧最前，那一刻 cb1 仍是上一帧 ⇒ F4 结构上拿不到当帧 cb1；**⑪ F5 也已被用户实测否决并回退**——合并骨架缓冲由宿主与跨组共用，把 palette 捕获后移会让整块骨架一起慢一帧 ⇒「两边都在抖、连原始物体都错」）。**⇒ 该残余（跨组块运动时相对宿主的一个「一帧姿态差」，静止时无）至今存在，未修复**；正确方向只能是**另开一份滞后副本**（宿主骨骼保持当帧、仅跨组换算读上一帧副本）。跨实体独立运动（把武器并进身体）同样受此残余影响。
>   d. **兜底**：CS 内 `zz_cb1_valid()` 校验两块的形态（rows0-2 w=0、row3 w=1、旋转行长度 0.5~2.0、|t| < 1e5），任一无效（未捕获/首帧/垃圾块）⇒ **退化为直拷**（偏常量 C，但**不消失、不 NaN**）。
>   e. **门控**：校准版 CS 只在开关开启时复制进 mod 的 `res/`；`[ResourceZZCb1_G*]` 与 AttachX 段同样只在开关开启时发射 ⇒ **关闭时产物逐字节同历史**。测试：`ZZMICrossGroupAttachTests::test_cb1_capture_resources_declared_only_when_enabled` / `test_render_section_captures_group_cb1_when_enabled` / `test_calibrated_shader_is_copied_only_when_enabled` / `test_calibrated_shader_implements_rigid_calibration`。
> 这两个修正疑似此前"dump 数据层全对但游戏内持续偏移"悬案的根因——dump 总在相似姿态抓取，重合骨/跨空间引用在 dump 里恒"正确"。
> **⑧ 跨组顺序无关 F3（2026-10-10）**：用户实测 F2 版「**一帧延迟 + 运动时错位、静止完全正常**」。产物级铁证（`洛克茜.inijkhjk:383-427`）：宿主的合并几何 draw 只按**自己**的出现次门控（`if $zz_ms_occ_3 == 1`）⇒ 无条件画；而 `AttachX_C*_g3_*` 读的 `ResourceZZPalette_4e6d8882_s*` 要到**该供骨件自己的 deform 段**才刷新，供骨件提交序（`4e6d8882` = draw 000023）**晚于**宿主（`d5e1e7ff` = draw 000020）⇒ 合并几何用**上一帧**的跨组 palette 画 = 一帧延迟；静止时上一帧 == 当帧 ⇒ 看不出来。修复三处（仅在开关开启时改变产物）：
>   a. `_append_merged_absorbed_replay` 的守卫改为**全部供骨件**（`_merged_group_component_ids(g) + _merged_cross_group_component_ids(g)`）⇒ 不再在跨组 palette 陈旧时闭合；
>   b. 新增 `_merged_cross_host_group_map()`（跨组供骨件 → 宿主组反向索引，带缓存）；
>   c. 新增 `_append_merged_cross_group_replays()`：跨组件**自己那段**也发同一套守卫 + 重放（跳过有重定向计划的宿主组与 Blend 布局不兼容者），并在 `_append_merged_skeleton_deform_block_body` 的 attach 循环里补「跨组件自己那段的跨组 attach `run`」⇒ 与提交顺序无关。
> **⑨ 蒙皮 CS 重发布 F3-CS（2026-10-10，彻底消除顺序依赖）**：仅靠 ⑧ 仍有结构性残余——宿主那段受**自己的 IA 布局**限制（宿主 BW16_BI16/32B，`4e6d8882` 是 BI4/4B）⇒ 若唯一布局兼容的供骨件排在宿主之前、最后到达者布局不兼容，守卫永不闭合 ⇒ 整块几何不画。故改为用**已有的蒙皮 CS 机件**（`Toolset/zzmi_merged_skin.hlsl` + `_append_merged_skin_publish_block`，按 `SV_DispatchThreadID` 从 SRV 读顶点属性、按索引写 SO ⇒ **绕开 IA 布局、幂等**）：新增 `_merged_cross_group_publish_plans()`（跨组场景的发布计划：carrier = **宿主的** vb0/vb2 与宿主导出顶点数、目标 = `ResourceZZRedirectSO_G<宿主组>_s<槽>` = 宿主 SO 缓冲，与游戏 deform 输出同一资源 ⇒ **渲染段零改动**）；**每个供骨件（宿主 + 各跨组源）在自己那段**、按「全部供骨件当帧到达」的守卫派发 `CustomShaderZZMISkin_G<宿主组>_s<槽>` ⇒ 一帧内**只在最后一个供骨件到达处闭合一次**，骨架与几何都当帧新鲜；**宿主原有的 `occ` 门控 draw 保留为兜底**（CS 万一失败 ⇒ 退化成 ⑧ 的一帧延迟，而不是消失）。残余：跨组供骨件当帧被剔除（上帧到、本帧不到）⇒ 守卫不闭合、该帧合并几何不写（与组内部件既有口径一致）。
> **⑩ 跨组同帧校准 F4（2026-10-10，⚠ 已被实测推翻并整体回退）**：⑨ 之后用户实测仍报「错位」。带 mod 抽帧（`K:\SSMT-Package-master\3Dmigoto\ZZZ\FrameAnalysis-2026-10-10-024622`）实测把链路钉死：**宿主渲染 vb0（= `de6784e3`/VertexLimitRaise 目标）920280 B = 23007 行，全行有效（0 行全零位置、0 个 NaN/Inf），四个渲染 cb1 齐全（464 B/份）** ⇒ F1/F2/F3-CS 的产物本身完全正确、合并几何确实被写出；**错位来自时序**：`ResourceZZCb1_G<N>` 只能在**渲染 draw** 处捕获，而渲染段排在 deform 段之后 ⇒ **deform 段算出的跨组骨用的是上一帧的 cb1**（当帧 palette × 上一帧 C = 帧间混用）⇒ 运动时错位、静止完全正常（与用户症状逐字吻合）。当时的修复 = **把校准 attach + 蒙皮 CS 在宿主渲染段再跑一遍**（那里 palette 当帧新鲜、cb1 也当帧新鲜）——**该前提已被第二轮抓帧推翻，见 c。**
>   a. 新增 `_append_merged_render_recalibration(section, draw_ib)`，由 `add_unity_vs_texture_override_ib_sections` 在**每条渲染块**的 `run = CommandListSkinTexture` 之后、`drawindexed` 之前调用（方法内部自校验 `has_merged_skeleton` + 开关 + 本 DrawIB 是否为该宿主组的发布目标 ⇒ 只作用于合并目标渲染块）；逐槽发 `if <全部供骨件当帧到达> / run = AttachX_C<i>_g<宿主>_s<槽>（校准写宿主骨架） / so0 = null / run = CustomShaderZZMISkin_G<宿主>_s<槽>（按当帧骨架重蒙皮写回宿主 SO） / endif`，同一 DrawIB 只发一次（`_merged_render_recalibration_emitted`）。
>   b. **为什么必须重算而非复用**：`add_unity_vs_texture_override_ib_sections` 的调用点排在 `add_merged_skeleton_sections` **之前** ⇒ 渲染段生成时拿不到骨架段发射的记录，故名称/守卫全部由纯函数从发布计划重推（`_merged_cross_group_publish_plan` 优先、退回 `_merged_group_redirect_plan`，要求 `target_ib == 本 DrawIB`）。
>   c. **⚠ 被推翻（2026-10-10 用户 F8 提帧的错位现场，`K:\SSMT-Package-master\3Dmigoto\ZZZ\FrameAnalysis-2026-10-10-033943`）**：按 `vb0` 大小归类的渲染 draw 序实测为 **`000065` 宿主（`vb0 = de6784e3`，920280 B = 23007 行）→ `000066` 3f797188（508 行）→ `000067` 4e6d8882（624 行）→ `000071` 4e6e989e（3933 行）** ⇒ **宿主渲染 draw 排在本帧最前**，各供骨件的 cb1 捕获全在它**之后** ⇒ 宿主渲染 draw 那一刻 `ResourceZZCb1_G*` 仍是**上一帧**的值 ⇒ F4 结构上拿不到当帧 cb1，只会把 deform 段已写好的正确 SO **覆盖成「当帧 palette × 上一帧 cb1」这一同样不同源的组合**（比不改更坏）。故 **F4 整体回退**：`_append_merged_render_recalibration` 及其调用点、三条 F4 单测全部删除。
>   d. **⚠ 真正原因（2026-10-10 06:48 抓帧 + 逐行数值验证后补记，见 ⑫）**：F4 的思路（在渲染 draw 处用当帧 cb1 重算）**是对的**，但它**写不进去**——蒙皮 CS 写的 `ResourceZZRedirectSO_G<组>_s<槽>` 正是**这次渲染 draw 的 `vb0`**，同一资源同时当 UAV 写、当 VB 读，D3D11 下这次写未定义/被丢弃 ⇒ F4 实际是空操作。另外 c 里的顺序问题也真实存在（宿主渲染 draw 有时排最前）。**⑫ 的 F7 = 同一思路 + `vb0 = null` 解绑**。
>   e. **取证副产品（仓库自相矛盾，需按实测口径记住）**：`ui/universal/zzmi.py:134`、`ZZMI骨骼合并计划书.md:382`、`tests/test_zzmi_merged_skeleton_ini.py:1918-1941` 断言「if 体内的 `run` 在本 fork 不执行」，而 `ui/universal/zzmi.py:2614-2615` 与本书 `:422` 断言「本 fork 支持 if 内跑 CustomShader」。**实测站在后者**：`K:\SSMT-Package-master\3Dmigoto\ZZZ\Core\ZZMI\Libraries\HP bar\hp.ini:66`（`run = CustomShader.OrderIds`）与 `:171`（`run = CustomShader.SumHp`）都在三层嵌套 `if` 内，该库是随包官方示例且在用 ⇒ F3-CS 与 F7 的 `if` 内 `run` **都会执行**。> **⑪ 跨组同源化 F5（2026-10-10，⚠ 实测失败，已回退）**：思路是「既然 cb1 在 deform 段必然是上一帧的，就把骨骼矩阵也变成上一帧的」——把 palette 捕获从 deform 段前部后移到 attach 之后。**用户实测否决**：「无论哪边都在抖了骨骼矩阵不对了」「原本只有跨过来的物体 CB1 不对，现在原始物体的 CB1 也不对了」。**失败机理（结构性，不是调参问题）**：`ResourceZZMergedSkeleton_G<组>_s<槽>` 是**宿主与跨组供骨件共用**的一份缓冲，捕获后移让**整块骨架**（含宿主自己的 79..211 骨骼）一起变成上一帧的值 ⇒ 宿主**自身**几何也随之陈旧 ⇒ 两侧同时抖、连原始物体都错。**结论：只让「跨组换算」用上一帧矩阵，必须另开一份滞后副本**（宿主骨骼保持当帧、跨组换算读上一帧副本），**不能动主捕获的时序**。回退内容：删除 `lag_capture` 与 4.5 捕获块、恢复「捕获 → attach → 换绑骨架」的历史次序、删除 2 条 F5 单测、新增 `test_palette_capture_stays_before_attach`（开/关两态都钉住次序）。原方案细节留档如下（供「滞后副本」参考）：`lag_capture = self._merged_cross_group_enabled() and (本组跨组成员非空 或 本部件是别组供骨件)`；为真时捕获改由 attach 循环逐槽紧跟 attach 之后发（`for slot in slots: <attach runs> → <捕获>`），读到的 `vs-t0` 正是上一帧该槽骨架里存的 palette，随后写回**同一个槽** ⇒ 槽内零净变化、不越界（`ResourceZZPalette_*` 的 `array` 与骨架 `array/3` 都是 `bones_count`）；捕获仍必须在 `vs-t0 = ResourceZZMergedSkeleton_G<组>_s<槽>` 换绑之前。
>   e. **残余（已知取舍）**：① 合并几何整体比游戏自身**晚一帧**（这是让 palette 与 cb1 同源的必然代价；静止无差别，运动时与游戏自身的偏移同相）；② 首次加载的头两帧，跨组块因骨架未初始化而退化为直拷（偏常量 C，但**不消失、不 NaN**）；③ cb1 形态无效（未捕获/首帧/垃圾块）时同样退化为直拷。
> **⑫ 渲染段同帧校准 F7（2026-10-10，⚠ 已被 ⑬ 取代——用户实测仍错位；诊断反转证明重算 CS 根本没执行）**：⑪ 回退后用户报「还是错位」。带 mod 抽帧（`FrameAnalysis-2026-10-10-055641` / `-064826`）+ 逐行数值验证把**真因**钉死，并且**推翻了 ⑩c 与 F6 报告里的两条错误结论**：
>   a. **链路本身完全正确（逐位）**：用抽帧里宿主 deform draw 绑定的合并骨架（`ResourceZZMergedSkeleton_G3_s1`，212 骨）+ 合并 Position（23298 行）+ 合并 Blend（23298 行）重建，与宿主渲染 draw 的合并 SO 逐行比 ⇒ **宿主 18231 行、`4e6d8882` 624 行、`3f797188` 508 行、`4e6e989e` 3935 行的 mean/max 误差全部 = 0.000000**；世界坐标级验证（行约定 `v_world = v_obj·R + t`）也给出**误差 0.0006 / 600 单位**（= 1e-6 相对量级）。**F6 报告里「校准段写进去的是宿主自己的骨、差 1.2 单位」是参考 palette 选错造成的假象**（对 slot 3/4 用了 `4e6d8882` 的缓冲当源 palette ✗；改用各自源缓冲后误差降到 0.004/0.013 单位）。
>   b. **真因 = cb1 在 deform 段必然是上一帧的**：渲染 pass 的 `vs-cb1`（资源 `30cb248b`，464 B，逐 draw `Map/Unmap`）**只存在于渲染 draw**；而 deform pass 在帧内排在**所有**渲染 draw **之前**（抽帧实测：deform `000025-000037` → 渲染 `000067-000082`）⇒ deform 段写 SO 时手里的 `ResourceZZCb1_G*` 是**上一帧**的值。实测佐证：用当帧 cb1 反推的 C（`4e6d8882` = 38.8591°/0.403704）与合并块实际使用的 C（38.8233°/0.403596）差 **0.036° / 0.0001 单位**，与 C 的帧间漂移同量级 ⇒ 混合出的 C **在宿主对象空间里偏约 0.4 单位**（= 肉眼可见的错位），而**静止帧两帧 C 相同 ⇒ 完全正常**，与用户症状逐字吻合。
>   c. **修复（F7）**：`_append_merged_render_recalibration(section, draw_ib)`（`ui/universal/zzmi.py`，由 `add_unity_vs_texture_override_ib_sections` 在每条渲染块的 `run = CommandListSkinTexture` 之后、`drawindexed` 之前调用）逐槽发：`if <全部供骨件当帧到达> / run = AttachX_C<i>_g<宿主>_s<槽> / so0 = null / run = CustomShaderZZMISkin_G<宿主>_s<槽> / endif`。`so0 = null` 解除「同一缓冲既当 SO 目标又当 UAV 写」的互斥；重蒙皮结果写进**本 mod 自己声明尺寸**的 `ResourceZZMergedSkinOut_G<宿主>_s<槽>`（`CustomShaderZZMISkinOut_*` 段），随后 `vb0 = ResourceZZMergedSkinOut_G<宿主>_s<槽>` ⇒ 紧随其后的 `drawindexed` 读到的就是当帧结果（**v17 修正**：早期版本写回 SO 并重绑 `vb0 = ResourceZZRedirectSO_*`，见 c2）。同一 DrawIB 只发一次（`_merged_render_recalibration_emitted`）。
>   c2. **⚠ 修正（2026-10-10 用户实测后，v17）**：早期 F7 版本写 `vb0 = null` + `vb0 = ResourceZZRedirectSO_G<宿主>_s<槽>`，用户实测报「**骨骼数据直接丢失**」。原因：`ResourceZZRedirectSO_*` 是运行期 `= ref so0` 别名出来的 `Buffer`，**声明里只有 `stride` 没有尺寸** ⇒ 3DMigoto 建不出它（`d3d11_log.txt` 里每次 reload 一行 `Failed to substantiate custom Buffer [...zzredirectso_g3_s2]: 0x80070057`，`dragjiggletempvb0_d5e1e7ff_a` 同病）⇒ 把它当 `vb0` 绑会得到空顶点缓冲，宿主几何整块消失。**v17 的两处改动**：① 给 `ResourceZZRedirectSO_*` 补 `array = <SO 行数>`（尺寸口径 = `so_prefix_rows + Σ deform_draws 行数`，与 `override_vertex_count` 同源），日志里的 substantiate 报错随之消失；② F7 **不再碰**运行期别名——新增 `ResourceZZMergedSkinOut_G<宿主>_s<槽>`（`type = Buffer` / `stride` / `array = 全部行数`）+ 配套 `CustomShaderZZMISkinOut_G<宿主>_s<槽>_c<i>` 段（同一份蒙皮 CS，`cs-u0` 指向该缓冲），F7 里 `run` 它之后 `vb0 = ResourceZZMergedSkinOut_G<宿主>_s<槽>` ⇒ 本次 draw 读到当帧校准结果，且**不依赖任何运行期别名**。单测：`test_render_recalibration_emits_attach_and_skin`（次序 attach → `so0 = null` → SkinOut CS → 绑 vb0，且断言不再出现 `vb0 = null` / `vb0 = ResourceZZRedirectSO_*`）+ `test_redirect_so_and_skin_out_buffers_declare_size`。
>   d. **与 ⑩F4/⑪F5 的区别**：F4 同位置同思路但整段没有被派发/写入链闭合 ⇒ 空操作；F5 动的是 palette 捕获时序（合并骨架缓冲宿主与跨组共用 ⇒ 整块骨架一起慢帧）⇒ 已回退。**F7 不碰 palette 时序**，宿主骨骼始终当帧，只把「跨组换算 + 重蒙皮」搬到能拿到当帧 cb1 的渲染段。
>   e. **安全性质（单调改善，不是新风险）**：宿主每帧可能有多次渲染 draw，F7 每次都会重跑；源部件若在本帧尚未渲染，其 cb1 仍是上一帧值 ⇒ 此时结果与 deform 段**等价（不更差）**；只要有任意一次宿主 draw 排在全部供骨件之后，那一次就是当帧精确的。守卫拦下时整块不发射 ⇒ SO 保持 deform 段结果（与既有缺帧口径一致）。
>   f. **残余**：源部件渲染 draw 排在宿主之后时，该次重算仍用上一帧源 cb1（与 deform 段同级，不更差）；首次加载头两帧仍可能退化为直拷。~~`ResourceZZRedirectSO_*` 的加载期 substantiate 报错（`0x80070057`）已知且无害~~ **v17：该报错已修**（声明补 `array` 尺寸），并且**仍然不允许**把 `ResourceZZRedirectSO_*` 绑到 `vb0`（F7 改用 `ResourceZZMergedSkinOut_*`）。
>   g. **单测**：`tests/test_zzmi_merged_skeleton_ini.py` 的 `ZZMICrossGroupAttachTests` 含 `test_render_recalibration_emits_attach_and_skin`（次序 + 不碰别名断言）、`test_render_recalibration_absent_when_disabled`、`test_render_recalibration_only_on_merged_target_ib`、`test_redirect_so_and_skin_out_buffers_declare_size`。两文件 **184 passed / 16 subtests**，`-k "zzmi or skeleton or merged or cross_group"` **575 passed / 35 skipped / 39 subtests** ✓。
> **⑬ 按来源拆分绘制（2026-10-10 定案并实现；用户实测通过的路线）**：⑫ 的 F7 经用户实测**仍然错位**，随后诊断反转——把「两份 cb1 旋转相同就推开」的探针放进重算 CS，用户报「没用」⇒ 该探针**必然**触发（宿主段的 `cs-t3`/`cs-t4` 都指向 `ResourceZZCb1_G3`，点积恒为 1）却没反应 ⇒ **重算 CS 根本没执行**（不是 cb1 没数据）。据此放弃「在渲染段把校准结果写回顶点流」这条路：写回 SO 的 UAV 写与 VB 读互斥，而把 mod 自建缓冲当 `vb0` 的**四次尝试全部静默失败**（结构化缓冲 / `type = Buffer` / 加 `bind_flags = unordered_access vertex_buffer` / 给真实后备文件 `Meshes/zz_worldvb.buf`——日志一律零报错，但那次 draw 什么都不画）。
>   a. **用户定案（逐字）**：「还是回退到一开始的做法导出的时候自动拆成多个绘制，然后使用不同的 CB1」（m04281）、「拆分绘制直接根据各个组件所属的组来拆」（m04510）、「也就是根据顶点组来拆」（m04517）、「然后拆完了以后，直接放回到各自的段落渲染然后物体名称还是用原本的那个大的物体名称」（m04525）、「拆会拆出3个段落，分别放回到 abc 里面，他们的备注的物体名称都是 a」（m04529）。
>   b. **为什么必须拆**：对象变换 cb1 是**每次绘制一份**的常量（464 B，前 16 float = 3×4 对象→世界矩阵；rows0-2 旋转行 w=0、row3 = `(t,1)`），顶点没有索引 cb1 的机制 ⇒ 一个 draw 只能有一个对象变换，而合并后的网格里有两群顶点属于**不同对象空间**（跨组件骨骼按直拷留在各自对象空间）⇒ 只能拆绘制。**骨骼矩阵仍然全部合并在同一份骨架缓冲里**（「一个物体用所有 IB 的骨骼矩阵」这一点不变），拆的只是绘制调用。
>   c. **实现**（`ui/universal/zzmi.py`）：`_merged_source_index_runs(draw_ib)` 读 `Meshes/<宿主IB>-Blend.buf`（32 B/行：4 权重 float + 4 骨索引 uint）与体积最大的 `LOD*<宿主IB>*-Index.buf`，逐行取权重最大的骨索引、查它落在哪个组件的 `vg_map` 值域（`(min,max,skeleton_group)`），按三角形切成 `[(skeleton_group, start, count)]`；`_merged_split_group_plan()` 给出「参与拆分的组 → (宿主组, 宿主发布计划, 宿主 IB)」；`_merged_split_representative(group)` 选该组的**代表段**（必须是 `merged_skeleton_component_id_dict` 登记过的 DrawIB——cb1 捕获只发在它的渲染段里，挑错代表段就永远拿到空值或上一帧值）；`_append_merged_render_split_draws(section, draw_ib)` 只发**本组自己的段**：宿主段直接 `vs-cb1 = ResourceZZCb1_G<宿主>` + `drawindexed`；非宿主段额外自己绑 `ib = Resource_LOD0.<宿主IB>_<count>_<start>_Index`（名字由 `_merged_index_unique_str` 从产物文件名反推）+ `vb1 = Resource<宿主IB>Texcoord` + `vb0 = ResourceZZRedirectSO_G<宿主>_s<1|2>`（按 `$zz_ms_occ_<宿主组件号>` 选槽）。调用方拿到 `True` 就跳过原来的单次合并绘制（否则网格被画两遍）。
>   d. **各回各家（关键）**：旧做法把**全部**段塞进宿主段 ⇒ 跨组 cb1 在宿主段执行时可能还是上一帧的值 ⇒ **影子/镜像物体**（用户 m04492 实测：宿主是头发时身体那段用陈旧 CB；帧内渲染序实测 `4e6e989e → 4e6d8882 → 3f797188 → d5e1e7ff`）。现在每组只在自己的渲染段里画自己的段 ⇒ 那一刻 `vs-cb1` 天然就是它自己的当帧对象变换 ⇒ **帧内绘制顺序不再影响正确性**，不需要任何跨段时序假设（这正是它优于 F4/F7 的地方）。
>   e. **mesh 标注（拖拽交互兼容）**：`blueprint/node_postprocess_draginteraction.py:2059-2074` 靠 `; [mesh:<名>] [vertex_count:<N>]` 与其后的 `drawindexed` 配对（`:2136+` 记录、`:5986 _inject_draw_hooks` 在记录行前插 `$ssmtdrag_objvis_<ns>_<oid> = 1`、`[Present]` 复位），注释丢了整个物体的拖拽/形态键/jiggle 钩子都会跟着丢。拆分绘制因此必须继承该注释，且**所有来源段统一写合并物体（宿主）的名字**（`_merged_split_mesh_comment(host_ib)`）——合并后游戏里只有一个物体。
>   f. **残余/口径**：① 某来源部件本帧未被游戏提交渲染时，它那一段不会被画（旧做法会画、但用的是陈旧 cb1 ⇒ 错位）——与游戏自身的剔除口径一致，方向更安全；② 同一来源若跨多段（合并行序由外部 join 决定），段内只换绑一次 `vs-cb1`；③ 读不到合并 Blend/Index 时退回单次绘制并落 `CROSS_GROUP_ATTACH` 诊断；④ 校准 attach（`AttachX_*`）改回**直拷**着色器、不再发 `cs-cb1/cs-cb2`，`Toolset/zzmi_merged_skeleton_attach_calibrated.hlsl` 不再复制进 mod 的 `res/`。
>   g. **单测**：`test_split_draws_go_back_to_their_own_render_sections`（宿主段只画宿主段且不做 ib/vb 重绑；跨组段自己绑合并 IB/Texcoord/宿主 SO 且用 `ResourceZZCb1_G1`；两段 mesh 标注相同）、`test_source_index_runs_split_by_bone_range`（真实读盘切段）、`test_split_draws_diagnose_when_merged_buffers_unavailable`、`test_split_draws_absent_when_disabled`。两文件 **184 passed / 16 subtests**、`-k "zzmi or skeleton or merged or cross_group"` **575 passed / 35 skipped / 39 subtests** ✓。
> **⑭ 角色展示页未解问题（2026-10-11 存档，未修复）**：用户实测「在角色展示页面一个几何都看不见，然后还会卡死在那边」；大世界（漫游/战斗场景）一切正常。
>   a. **确定的事实（实测）**：① 展示页里**游戏自己的 draw 照常执行**（抽帧 `FrameAnalysis-2026-10-11-014416` / `-014829` 里 `Draw(VertexCount:17940)`、`DrawIndexed(IndexCount:61305)`、`1704`、`11700` 都在）；② 我方段在该场景只以 **`checktextureoverride` 的资源级形态**出现（帧日志里的 `[TextureOverride\Mods\SSMTGeneratedMod\…] pre {` 块），`handling = skip` 没有生效痕迹；③ **进程没死**——MCP `ping` 正常应答、`hunting_mode` 可切、帧在持续产生 ⇒「卡死」是画面不推进，不是进程挂死；④ 展示页的渲染 VS 与主场景不同（`c280f6945b23a42a` vs `5032bdbc69bf3401`），而 IB / VB 哈希相同。
>   b. **已作废的三条假设**（存档以免重走）：① **SO 越界**——展示页帧里宿主渲染 `vb0`（`de6784e3`）的 dump 布局 = 920280 B = 23007 行 ⇒ `VertexLimitRaise` 在展示页**是生效的**，不存在「往 17940 行缓冲里写 23007 顶点」；② **SkinOut 补丁导致卡死**——回退该补丁（ini 回到 26232 B / 00:23 导出件）后用户复测**依旧卡死**；③ **标记测试**——把 `draw = 23007, 0` 改成 `23006`、`drawindexed = 61305, 0, 0` 改成 `61304, 0, 0`，在展示页与大世界两帧里**都不出现**（游戏自己的 `61305` 在）⇒ **抽帧日志只记录游戏自己的 D3D 调用，3DMigoto 替 mod 发的 draw 不走那条路径** ⇒ 该测试对「我方 draw 是否执行」**没有判别力**，不能用来判定链路好坏。
>   c. **顺带确定的机制（重要）**：合并几何并不依赖我方 `draw` 命令，而是靠**资源级覆盖**——我方段在游戏自己的 deform / 渲染 draw 上把 `vb0` / `vb2` / `ib` / `vs-t0` 换成合并后的资源，游戏自己的 draw 就用上了合并顶点、合并 IB 与合并骨骼。这解释了「展示页里我方段只以资源覆盖形态出现却仍生效」。
>   d. **待查方向（下次接手）**：① 展示页那条渲染路径（不同 VS）为什么不吃我方的 `draw` / `drawindexed`（是 3DMigoto 的匹配规则，还是该路径的 draw 由别的资源哈希触发）；② 展示页画面为什么不推进（是否与该场景的 `Present` / 暂停语义有关——需在展示页里用 MCP 实时读 `xxmi.data.reload_pending` / `analysis_active` 与 `addon.render.draws` 对照）；③ 用 MCP 0.7.3 的 `addon.gpu.list` 在展示页里点名读宿主 SO 与合并骨架，确认资源级覆盖是否真的落到游戏 draw 上。
> 目标：参照 EFMI 骨骼合并（`common/efmi_skeleton.py`，已端到端验证）的「工作空间反查 FrameAnalysis dump → 数据复制回工作空间缓存」模式，为 ZZMI（绝区零）工作空间提供骨骼合并数据（VGMap/VGOffset/VGCount + 骨骼 palette 缓存）。
> 与 EFMI 的关系：**只复用数据层模式**（log 解析、反查、矩阵去重、写回缓存、复选框门控）；**运行时的 CS 着色器与 INI 段落全部按 ZZZ 的数据存储位置/格式从零编写**（deform pass 挂载、vs-t0 SRV stride 48、per-pass Map + ring buffer 时序、SO 管线），不移植 EFMI 的任何着色器/段落——不同游戏数据布局完全不同。分支选项：复用复选框 `import_merged_vgmap`（「使用融合统一顶点组」），勾选则用骨骼合并，不勾选维持现状，零副作用。
> 依据：对真实 ZZZ FrameAnalysis dump（`K:\SSMT-Package-master\3Dmigoto\ZZZ\FrameAnalysis-2026-08-19-122152`）的完整逆向分析 + **配套测试工作空间 `K:\SSMT-Package-master\WorkSpace\ZZMI\希格莉德·空岛传奇` 核对**（`Config/FrameAnalysisPath.json` 已确认指向同一 dump，draw 索引逐条吻合）。

---

## 1. 背景

### 1.1 已有样板：EFMI 骨骼合并（已完成并验证）

`common/efmi_skeleton.py`（743 行）实现了终末地（EFMI）的骨骼合并：

- `EFMILogParser`：解析 FrameAnalysis `log.txt`（draw 调用 / CB 绑定 first_constant 窗口 / SRV 绑定 / dump 文件名 → deduped 路径映射）。
- `EFMIBoneMapBuilder.get_skeleton_buffer`：反查 instance config（fc=4096 窗口 `[5][0:2]` 段偏移）→ vs-t0 骨骼池 256×12 矩阵。
- `build_vg_maps`：**跨子网格按骨骼矩阵内容去重** → 每子网格 `VGMap`（local→global）。
- `EFMISkeletonMergeHelper.ensure_skeleton_data`：幂等总流程——写回子网格 json（VGMap/VGOffset/VGCount）+ 骨骼池 buf 复制到 `<子网格>/ModImpRuntime/<bare>-BoneMatrix.buf`（NTEMI 缓存模式）。
- 集成：`ImprotFromWorkSpaceFull`（`ui/ui_func_import_ssmt.py`）里`logic_name == EFMI and GlobalProterties.import_merged_vgmap()` 双条件触发；失败不阻断导入。

### 1.2 本次目标（用户拍板）

1. ZZMI 复刻同一模式：从工作空间反查 ZZZ dump，找到骨骼数据后**复制回工作空间**，下次直接用。
2. **导入、导出都要实现骨骼合并，且就是 EFMI 那种「统一顶点组」**：所有子网格共用一套全局顶点组（全局骨架），**组件 A 可以用组件 B 的骨骼/权重**（跨部件权重合法）。
3. **直接复用复选框** `import_merged_vgmap`（`common/global_properties.py:202`，不新增 UI、不改默认值）：勾选 = 导入全局顶点组、导出走合并骨架；不勾选 = 两侧都维持现状。

### 1.3 非目标

- 不改 SSMT4 提取端（ZZZ 提取已正确捕获 deform 输入，见 §2.3）。
- 不改 ZZMI 现有导入导出**默认**行为（复选框关闭 = 现状；开启且有 VGMap 数据才走合并路径）。
- 不做「导出时反映射回局部索引」的降级 B 模式（列入后续可选，见 §7）——本期直接做与 EFMI 同构的完整版。

---

## 2. ZZZ 渲染管线实证（FrameAnalysis-2026-08-19-122152）

### 2.1 三段式管线（log.txt 逐 draw 追踪结论）

| 阶段 | draw 范围 | 内容 |
|---|---|---|
| ① CS 形变 pass | 000012-000017、000021-000028 | CS `743108cc03f39cbf` 算 morph/blendshape，写池 `f50c0d31` / `90b64ae0`（UAV） |
| ② pointlist 蒙皮变形 pass | 000001-000011、000018-000020、000029-000036（22 个） | CPU `Map` 上传骨骼 palette → 绑 **`vs-t0`**；VS（`e8425f64cfb887cd` / `9684c4091fc9e35a` / `a0b21a8e787c5a98`）读绑定姿势 vb0 + 权重 vb2 + palette，**SO 流式输出蒙皮后顶点** |
| ③ 渲染 draw（= SSMT 的 DrawIB） | 000037 起 | vb0 直接绑对应 deform pass 的 SO 输出 buffer，**渲染 draw 本身没有骨骼数据** |

**骨骼数据位置结论：在 ② 的 `vs-t0`**（每部件一个 palette SRV，float32 矩阵流，dump 在 `deduped/<内容hash>.buf`）；**权重在 ② 的 vb2**（`BLENDWEIGHTS` + `BLENDINDICES`，palette 局部索引 uint32）。

### 2.2 渲染 IB ↔ deform pass 映射（SO 输出 hash = 渲染 vb0 hash 连接，全部 11 个 IB 命中）

| DrawIB | 顶点数 | deform pass | palette (vs-t0 资源 → 内容 hash) | 权重 layout |
|---|---|---|---|---|
| 84618ee0 | 5846 | 000004 | `f6a6c781` → `f2a54012` | 4×f32 权重 + 4×u32 索引 (2840ec5f) |
| a23aa8a3 | 12314 | 000020 | `c3f98669` → `097b226c` | 同上 |
| 19086112 | 3288 | 000035 | `45c35f5b` → `26be11c4` | 同上 |
| b51bdd59 | 345 | 000036 | `c6c3b31d` → `b4f8f4d8` | 同上 |
| b20f90ea | 4643 | 000002 | `c2f5419a` → `e018278f` | 同上 |
| b30db54e | 1744 | 000008 | `773b317d` → `18fa1c05` | 同上 |
| 48625d6d | 3542 | 000018 | `f43bdd3b` → `6b520746` | 820de055（vb0 来自 CS morph 池拷贝 `a1644290`） |
| d892c658 | 488 | 000010 | `36bb475e` → `531460ff` | 2×f32 + 2×u32 (5cdc3f7c) |
| 64d7d56f | 388 | 000001 | `23de2d6a` → `eaf48535` | 单索引 R32_UINT (4e92e68b/614cf6d8) |
| 454ff522 | 76 | 000029 | `23de2d6a` → `eaf48535` | 同上 |
| add6ff13 | 193 | 000030 | `23de2d6a` → `e6f6fd14` | 同上 |

### 2.3 合并可行性实证

- `64d7d56f`（deform 1）与 `454ff522`（deform 29）的 palette dump **内容完全一致**（`eaf48535`）；`84618ee0`（deform 4）与同帧另一部件（deform 32，1793 顶点）也完全一致（`f2a54012`）——同角色部件共享同一套骨骼矩阵，**跨部件矩阵去重有实际收益，方案成立**。
- ZZMI 工作空间子网格 json 已带 `"GPU-PreSkinning": true`，`CategoryBufferList` 确认提取端捕获的是 deform 输入（POSITION/NORMAL/TANGENT←vb0、TEXCOORD←vb1、**BLENDWEIGHTS/BLENDINDICES←vb2**）。**工作空间缺的只有 palette 与 VGMap**——正是本方案要反查补齐的。

### 2.4 已识别的坑（实现必须处理）

1. **palette buffer 是 ring scratch 复用**：同一资源 hash（`23de2d6a`/`178dab71`/`f6a6c781`/`141c7638`）一帧内被多个 deform pass 重写。**必须按「该 deform pass 的 dump 逻辑文件名」`0000XX-vs-t0=<资源hash>-vs=<deformVS>.buf` 定位 deduped 内容文件，绝不能只按资源 hash 认 palette**。
2. **deform VS 不硬编码 hash**：用结构特征识别 deform pass——`Draw(VertexCount:N)` + 绑了 SO target + 绑了 vs-t0 SRV（VS hash 随版本变）。
3. **morph 部件**（48625d6d）：deform vb0 是 CS 形变池的 CopyResource 拷贝，不是静态 buffer；palette 仍走 vs-t0，不受影响。
4. **同帧多角色**：22 个 deform pass 里只有 11 个属于本角色；必须用 SO-hash join 从工作空间记录的渲染 draw 反挂，防串台（NPC 的 pass 不会被误认）。
5. **渲染 pass 的共享 `vs-t0=7dfb0292`**（内容 `ce0ededa`，所有渲染 draw 相同）：与逐部件骨骼无关，勿误用。
6. ~~矩阵步长待测定~~ **已测定（任务 0 完成，见 §2.5）**：12 floats（4×3，48 字节/骨骼）。

### 2.5 实测数据结论（任务 0，探针脚本 `.dbg/zzmi_palette_probe.py`）

对 11 个 palette buf + 15 个子网格 Blend buf 的实测：

| DrawIB | palette 骨骼数 | BLENDINDICES max+1 | 跨部件去重 |
|---|---|---|---|
| 84618ee0 | 49 | 49（完全相等） | 49 全新 |
| a23aa8a3 | 105 | 105 | 105 全新 |
| b20f90ea | 51 | 51 | 38 新（13 共享） |
| d892c658 | 16 | 16 | 16 全新 |
| b30db54e | 14 | 14 | 8 新（6 共享） |
| b51bdd59 | 11 | 11 | 11 全新 |
| 48625d6d | 10 | 10 | 9 新（1 共享） |
| 19086112 | 7 | 7 | 7 全新 |
| 64d7d56f | 1 | 1 | 0 新（整个重复） |
| 454ff522 | 1 | 1 | 0 新（整个重复） |
| add6ff13 | 1 | 1 | 1 全新 |
| **合计** | **266** | | **全局唯一 244（22 个共享槽）** |

- **步长 = 12 floats（4×3 矩阵，48 字节/骨骼）**：全部 buf 被 12 整除；旋转行单位化、平移量级正常；16-float 解释被内容否决。
- **palette 骨骼数 == 实际用量**（无填充）：`len(skeleton) >= vg_count` 校验以等号成立。
- **合并骨架 266 > 255**：BI4（454ff522/64d7d56f/add6ff13）与 BW8_BI8（d892c658）**升宽是硬需求**，无条件升宽策略正确。
- **拆分子网格共享数据**：48625d6d ×3 / 84618ee0 ×2 / a23aa8a3 ×2 的 Blend.buf 均为全量顶点（同一份），共享 palette 与 VGMap。

**两两重叠与拼接对齐实测**（探针 `.dbg/zzmi_palette_overlap.py`，bitwise 判等）：

- `b20f90ea`(51) ↔ a23aa8a3(105)：**13 根**全同（如 `#0→#9`、`#1→#10`）；拼接后 b20f90ea 只新增 38 根。
- `b30db54e`(14)：→ a23aa8a3 `#0→#9, #1→#10`；→ b20f90ea `#0→#0, #1→#1, #2-#5→#45-#48`。
- `64d7d56f#0` = `454ff522#0` = `b51bdd59#0` = `48625d6d#2`（抓帧瞬间四者矩阵逐位相同）。~~同一根挂饰骨骼四部件共用~~ **2026-08-24 修正**：几何布局取证（`.dbg/zzmi_rigid_parts_probe.py`）显示四者分处头部四个位置——64d7d56f 头顶（z≈1.645）、454ff522 前额薄件（z≈1.573）、b51bdd59#0 后脑物理发饰（y=+0.135，11 根对称骨）、48625d6d#2 面部锚点——是**抓帧重合的不同锚点骨**，游戏内动画（尤其发饰物理）分叉时会错位联动（用户游戏内实测误并，疑似此前"dump 全对但持续偏移"悬案根因：dump 总在相似姿态抓取，重合骨在 dump 里恒"正确"）。**刚性部件质心门控**后：64d7d56f、b51bdd59#0 被拆开各占各槽；454ff522#0 ↔ 48625d6d#2 质心距 0.034 < 0.05 保持合并；全局唯一骨骼 244 → **246**。
- 84618ee0 / a23aa8a3 / 19086112 / d892c658 / add6ff13 的骨骼不与其它部件重叠（独立子集）。
- **关键结论：共享骨骼的索引位置不固定**（b30db54e `#2-#5` 对应 b20f90ea `#45-#48`）——必须按矩阵内容判等去重，按索引对齐会出错。
- 矩阵形态：12 floats = 3×4（3 行单位旋转 + 平移列），旋转行模长全部 = 1.0000（无缩放）。

---

## 3. 数据链路与反查方案

### 3.1 工作空间侧可用线索（以测试工作空间 `希格莉德·空岛传奇` 实测）

- 布局：`LOD0/<drawib>-<index_count>-<first_index>/TYPE_GPU_<gametype>/<子网格同名>.json`（本角色 11 个 DrawIB、15 个子网格：84618ee0 ×2、a23aa8a3 ×2、48625d6d ×3、其余 ×1）。
- `LOD0/ComponentName_DrawCallIndexList.json`：`子网格名 -> [渲染 draw 索引]`（与 log.txt 逐条吻合，如 `b20f90ea-19182-0 -> 000038/000044/000192/000213/000225`）。
- `LOD0/DrawIB-Component.json`：drawib → component 序号 → 子网格名。
- 子网格 json（如 `b20f90ea-19182-0.json`）关键字段：
  - `GamePreset: "ZZMI"`、`GPU-PreSkinning: true`、`WorkGameType`；
  - **`CategoryHash`**：`Position=122883aa / Texcoord=5c0fefda / Blend=bf543990`——**就是 deform pass 的 vb0/vb1/vb2 资源 hash**（最直接的 join 线索）；
  - **`VertexLimitVB: "dd9c8d5e"`**——**就是 deform pass 的 SO 输出资源 hash**（第二条独立 join 线索）；
  - `CategoryBufferList`：Blend buf 文件名 + BLENDINDICES 元素（`R32G32B32A32_SINT`，EFMI 的 `_blend_indices_layout` 已兼容 SINT）；
  - `VGCount: 0 / VGOffset: 0`、无 VGMap——**待本方案回填，与 EFMI 现状同构**。
- `Config/FrameAnalysisPath.json`：SSMT4 已记录 dump 路径（EFMI `resolve_frame_analysis_dir` 三候选回退逻辑可直接搬）。

### 3.2 反查流程（核心算法：三条独立 join 路径，互为校验/兜底）

对每个工作空间子网格：

- **路径 A（最直接）**：子网格 json `CategoryHash.Position` == deform pass `IASetVertexBuffers` slot 0 资源 hash → 命中该 deform pass。（`bf543990` Blend hash 可二次校验。）
- **路径 B（SO 连接）**：子网格 json `VertexLimitVB` == deform pass `SOSetTargets[0]` 资源 hash → 命中该 deform pass。
- **路径 C（draw 索引兜底）**：`ComponentName_DrawCallIndexList.json` 取该子网格的渲染 draw → 读其 `IASetVertexBuffers` slot 0 hash（= SO 输出）→ 找 SO target 同 hash 的 deform pass；可顺带校验 `IASetIndexBuffer` hash == 子网格 drawib。

命中 deform pass 后：

4. **取 palette**：该 deform pass 的 `VSSetShaderResources` slot 0 资源 hash → 组 dump 逻辑文件名 `<draw>-vs-t0=<hash>-vs=<deformVS>` → 经 parser 的 dump_map 拿到 `deduped/<内容hash>.buf` 实际路径。
5. **解析 palette**：.buf 按 float32 流读入，按测定步长（12 或 16，见任务 0）切矩阵；按工作空间 Blend buf 的 BLENDINDICES 最大局部索引 +1 得 `vg_count`，切片到实际用量（对齐 EFMI：`len(skeleton) >= vg_count` 校验）。

> 同一 DrawIB 拆成的多个子网格（84618ee0 ×2、a23aa8a3 ×2、48625d6d ×3）会命中**同一个 deform pass 与同一份 palette**——正常：它们的权重共享同一局部索引空间，VGMap 相同、VGOffset/VGCount 相同，去重天然幂等。

### 3.3 跨子网格去重与回写

- **去重规则（用户拍板 + 实测修正）**：
  1. **同一部件内部绝不去重合并**——同部件 palette 索引是提取端权威分配，内部零重复（实测 11 个部件全部 0 个内部 bitwise 重复），原样保留。
  2. **仅跨部件之间去重合并，且只用 bitwise（字节级）判等，禁用浮点容差**——同一骨骼在同一帧被 CPU 上传到各 palette 时是同一份数据的逐位拷贝（实测共享骨骼跨部件 maxdiff = 0.00e+00）；不同骨骼或同骨骼不同帧位必然不同。
  3. **不同物体的不同编号顶点组，只要被同一骨骼矩阵驱动（bitwise 相同 = 同一骨骼），就去重为同一个全局顶点组**。例：物体 A 的 36 号顶点组与物体 B 的 7 号顶点组若由同一骨骼矩阵驱动，两者都映射到同一个全局 id，导入后在 Blender 里就是**同一个顶点组名称**（实测案例：b20f90ea 有 13 个组与其它部件共享）。
  4. **单骨骼刚性部件（单权重物体）追加加权质心门控**（2026-08-24 用户拍板）：命中对任一方 palette 仅 1 根骨骼时，bitwise 相同还须加权质心距离 < `rigid_centroid_tolerance`（默认 0.05 米）才合并，否则各占各槽。刚性部件的唯一骨骼 = 整个物体的锚点，质心即物体位置指纹——抓帧瞬间重合的不同锚点骨（头部挂件密集区高发）靠此分离。刚性部件误拆零代价（各自 attach 写同一矩阵，运行时内容恒等），误并则动画分叉时错位联动；只拆不并是安全方向。双方均为多骨骼部件时不加门控（多根同时位等不可能是巧合；真共享骨骼驱动区域质心实测可相距 0.25，加门控会误拆真共享）。缺签名时刚性命中对保守拆开。
  5. **骨架分组 + 组内统一（2026-08-24 分组拍板；2026-08-25 移除校准拍板；2026-09 v9 出现次槽位）**：palette 矩阵把顶点蒙皮到**对象空间**（列向量约定 `object = Rm·bind + tm`，12 floats 平移在 [3,7,11]），渲染 VS 用该对象的 cb1 矩阵（rows 0-3 = 对象→世界，行向量约定 `world = object·R + t`）摆到世界。**合并骨架按渲染 cb1 对象变换分组**：变换逐位相同的部件进同组（同空间），组内去重（bitwise + 刚性门控），**骨骼 id 为全局编号（组基址拼接组内槽位）**——Blender 侧组内 join 无歧义。运行时**每组每槽一套全宽合并骨架**（`ResourceZZMergedSkeleton_G<g>_s<k>`，array = 全局 max(vg_offset+vg_count)）：每个 deform pass 顶层自增出现次、按出现次（1/2 循环）把当帧 palette 复制进该槽（**F5 曾把该捕获后移以让 palette 与 cb1 同源，实测导致整块骨架一起慢帧、连原始物体都错，已回退，见 ⑪**）、顶层无条件 `run` 全部 (部件, 槽) attach，用 `Toolset/zzmi_merged_skeleton_attach.hlsl` **只直拷本组当帧骨骼**（跨组 attach 另走校准版 `Toolset/zzmi_merged_skeleton_attach_calibrated.hlsl`，见 ⑦）；每槽守卫在本组全部部件该槽都已当帧到达时才重放合并可见几何。`[Present]` **只把 occ/seen 清零**（不重放 palette、不写任何资源复位），无任何校准乘，**也不在 [Present] 复位 `ResourceZZRedirectSO_G<g>_s<k>`**（F8 构造经 2026-09 实测有害：会废掉 [Present] 清场，已回退）。**~~禁止跨组别骨骼合并~~（2026-10-10 修正：改为默认关闭的实验总开关 `cross_group_merged_vgmap_test`——关闭时逐字节维持历史行为，开启后走 F1 把跨组件骨骼写进宿主组缓冲）**：外来骨骼写进宿主组缓冲时走**校准乘** `M' = C×M`、`C = U_宿主组⁻¹ × U_源组`（F2，2026-10-10 重新启用：2026-08-25 曾因 cb1 捕获段按 IB hash 匹配不稳定而整体废弃，现改为在**各组部件自己的渲染 draw** 处捕获 + attach 留在 deform pass 内，且只作用于跨组 attach）；跨组别引用由导出侧 `_warn_cross_group_bone_references` 报警（**开关关闭**：报「禁止跨组别骨骼合并 … 原点塌陷」；**开关开启**：报「跨组骨骼合并已接入（F1+F2+F4）」+ 残余说明）。**F1 的残余（已由 F2/F4 补齐）**：① 换算矩阵 → F2 校准 CS 接入；② cb1 只能在渲染 draw 捕获、deform 段用上一帧 ⇒ F4 在**宿主渲染段**用当帧 cb1 重跑 attach + 蒙皮。json 写回 `SkeletonGroup` + 全局口径 VGMap/VGOffset。无 cb1 可解析的部件独立成组（不共享，安全方向）。实测分组见本文末尾。
- **为什么禁用容差（实测踩坑记录，`.dbg/zzmi_match_accuracy.py`）**：
  - 48625d6d 的 `#1/#8/#9` 与四部件共享骨骼 maxdiff = 3.5e-07/2.5e-06（近似非相等）——是**同一骨骼的不同动画帧姿态**（48625d6d 是脸部 morph 部件，deform pass 18 晚于 pass 1/29/30，动画已推进），容差匹配会把它们误并；
  - a23aa8a3 内部存在 maxdiff = 0.000000 的**不同骨骼**（对称/镜像骨骼浮点同值但位不同），84618ee0 内部不同骨骼最小差异仅 8.7e-04——容差稍大就误并。
  - 结论：bitwise 是精确判据（零误并零漏并），容差两头都出错。
- **算法**：每部件 palette 逐骨骼 `tobytes()` 作 dict key；跨部件命中即映射到 canonical 全局槽位（canonical 按 weighted_vertex_count 选主，复用 EFMI `build_vg_maps` 三遍扫描骨架，但判等改为 bitwise、且跳过同部件对）。产出每子网格 `VGMap{local: global}`、`VGOffset`、`VGCount`。
- **合并骨架内存布局（关键语义）**：每部件 palette 在合并骨架中**按 VGOffset 连续摆放**（VGOffset = 固定排序下前序部件 vg_count 累加），合并骨架总槽位 = Σ vg_count（本角色 266）；去重只决定顶点引用的全局 id 指向哪个 canonical 槽位，**22 个重复槽位是死槽**（无顶点引用，内容恒同，无害）。此语义与 EFMI-Tools 的 vg_offset 完全一致，运行时 CS 只需按 vg_offset 连续拷贝，无需查表。
- **回写 json**：写入各子网格自己的 `<子网格>/TYPE_GPU_<gametype>/<子网格>.json`（VGMap/VGOffset/VGCount 三字段，与 EFMI 回写位置同构）；幂等——已有 VGMap 且非 force 则跳过。
- **复制缓存**：palette buf 复制到 `<子网格>/ModImpRuntime/<子网格>-BoneMatrix.buf`（NTEMI/EFMI 同款缓存模式），并在 json 记录 `BoneMatrixFileName`。下次（或换机器）无 dump 时可直接用缓存。

---

## 4. 模块设计：新增 `common/zzmi_skeleton.py`

完全独立于 `efmi_skeleton.py`（不改 EFMI 一行），复刻其分层与 API 形状，方便对照维护：

| 类/函数 | 职责 | 对应 EFMI 样板 |
|---|---|---|
| `ZZMILogParser` | 解析 log.txt：`Draw(VertexCount:N)`、`SOSetTargets` 及其资源行、`IASetVertexBuffers/IASetIndexBuffer` 及其资源行、`VSSetShaderResources` SRV 绑定、dump 逻辑名 → deduped 路径映射 | `EFMILogParser`（正则/状态机模式直接照搬，新增 Draw/SO/IA 三类记录） |
| `ZZMIDeformResolver` | deform pass 识别（pointlist + SO + vs-t0）与 SO-hash join：渲染 draw → deform pass | 新增（ZZZ 特有，EFMI 无此层） |
| `ZZMIBoneMapBuilder` | palette buf 解析（步长探测）+ `build_vg_maps` 矩阵去重 | `EFMIBoneMapBuilder`（去重算法可直接复用/抽取共用） |
| `ZZMISkeletonMergeHelper.ensure_skeleton_data(workspace_root, target_list, force=False) -> (bool, str)` | 幂等总流程：定位 dump → 解析 → 反查 → 去重 → 回写 + 复制缓存 | `EFMISkeletonMergeHelper.ensure_skeleton_data`（含 `resolve_frame_analysis_dir` 三候选回退，直接搬） |

依赖：仅 `os/re/shutil/numpy` + `utils.json_utils`，无 bpy 依赖（保证可单测、可 Blender headless 跑）。

## 5. 集成点（分支选项，复用复选框）

### 5.1 复选框（不新增）

`GlobalProterties.import_merged_vgmap()`（`common/global_properties.py:202` 「使用融合统一顶点组」，默认 True 保持不变）。语义与 WWMI/EFMI 一致：**开 = 有 VGMap 数据就走合并骨骼；关 = 完全现状**。

### 5.1.1 伴随的实验开关（两个独立属性，**默认均为 False**）

合并骨架线在导入/导出主开关之外，另有两个**独立**的实验开关（`common/global_properties.py`
里各自一个 `bpy.props.BoolProperty` + 一对 classmethod 访问器，访问器一律经
`_bool_attr(<属性名>, False)` 兜底为 False）：

| 实验开关 | 默认 | 声明 | 生产消费者 | 现状 |
|---|---|---|---|---|
| `zzmi_merged_redirect_enabled`（面板名「启用合并网格自动重定向（实验）」） | False | `common/global_properties.py` 的 `BoolProperty` 声明（访问器只有 getter `zzmi_merged_redirect_enabled()`（返回 `_bool_attr("zzmi_merged_redirect_enabled", False)`）；**无 setter**——原先的 setter 零引用，已删除） | `ui/universal/zzmi.py` 的 `_export_impl` 重定向决策处（`_zzmi_prop_flag("zzmi_merged_redirect_enabled", False)`）+ 面板渲染（`ui/ui_panel_basic.py`） | **已接线，但默认关** |
| `cross_group_merged_vgmap_test`（面板名「跨组融合统一顶点组测试」） | False | 同上（访问器 `cross_group_merged_vgmap_test()` 与 `set_cross_group_merged_vgmap_test()`，两者都在） | **`ui/universal/zzmi.py`（2026-10-10 F1 起）**：`_merged_cross_group_enabled()`（读开关）→ `_merged_cross_group_component_ids()` / `_merged_cross_group_offending_bone_ids()`（跨组判定）+ attach 段发射 + deform VB 段逐槽 `run` + 守卫措辞；另面板渲染（`ui/ui_panel_basic.py`） | **真开关（默认关；关闭时逐字节维持历史行为，开启后走 F1 跨组 attach + F2 渲染 cb1 校准 + F3/F3-CS 顺序无关发布；F4/F5 两个时序方案均实测否决并已回退，跨组块运动时残余「一帧姿态差」）** |

`zzmi_merged_redirect_enabled` 的行为路径（依据 `ui/universal/zzmi.py` 的 `_export_impl`）：
重定向计划先按现有实现算好（`_build_merged_mesh_redirect_plan`），随后
`if not _zzmi_prop_flag("zzmi_merged_redirect_enabled", False) and (<carrier 或 target 计划非空>)`：
**默认关**时把 `_redirect_carrier_map` / `_redirect_target_map` 清空并打印提示——
即**保留每个合并物体的原始 DrawIB/Blend 输入布局，改走「组内宿主当帧重放」**；
**开启**时才启用跨 DrawIB 的 RedirectSO 自动重定向（代码注释与开关文案都写明：该路径在部分
ZZMIv1 帧序下会让完整合并物体整块消失，故只作复核用途）。

`cross_group_merged_vgmap_test`：跨 SkeletonGroup（对象变换/渲染 vs-cb1 对象空间不同）
的合并需要一层**逐帧对象空间换算**，该功能**尚未实现**；因此本开关当前不参与任何导入/导出决策
（属性描述里也如此声明）。开启它**不会**改变已验证的合并骨骼（`import_merged_vgmap`）行为。

两个实验开关都**不参与数据生成**：反查与落盘只发生在导入侧（见 §5.2 不变量），
开关只决定导出侧怎么消费这份数据。

### 5.2 数据生成门控（生成侧与消费侧分离）

**不变量（用户要求）**：骨骼合并数据（`VGMap`/`VGOffset`/`VGCount` +
`SkeletonGroup`/`DeformDrawIndex`/`OriginalVertexCount` + `ModImpRuntime` 的
palette（`BoneMatrix`）/对象变换（`ObjectCB1`）副本）**在首次导入时一次性反查并落盘到工作空间**，
与「使用融合统一顶点组」（§5.1）及两个实验开关（§5.1.1）是否开启**无关**；数据持久保存，
除「清除骨骼合并VGMap缓存」外，数据**过期/残缺**（见下方自愈范围）也会在下次导入被修复——**有新的提取文件就用新的，没有则用工作空间
缓存重建**（`ZZMISkeletonMergeHelper.ensure_skeleton_data`：palette 取 dump 的 deduped 路径、
对象变换 CB 取当前渲染 draw 的 CB1 窗口，二者在 dump 不可用时都回退 `ModImpRuntime` 副本；
dump 存在但该 IB 无可用 CB1 时**不回退**，显式跳过以免跨帧混用）。因此打开这些开关
**永远不需要重新提取**。复选框只作为**消费侧**门控（§5.3 / §5.4）。

`ImprotFromWorkSpaceFull`（`ui/ui_func_import_ssmt.py`）的 ZZMI 预生成门控：

```python
zzmi_merged_consumption = (
    GlobalConfig.logic_name == LogicName.ZZMI
    and GlobalProterties.import_merged_vgmap()
)
zzmi_merged_metadata_missing = ZZMISkeletonMergeHelper.missing_merged_metadata_exist(
    GlobalConfig.path_workspace_folder(), import_keys
)  # 缺 VGMap 家族，或快路径完整性（_zzmi_cache_intact）不通过
if zzmi_merged_consumption or zzmi_merged_metadata_missing:
    ok, message = ZZMISkeletonMergeHelper.ensure_skeleton_data(...)
    # 打印结果；失败不阻断导入（对齐 EFMI）
```

- 复选框**开** → 与旧实现一致：预生成 + 失败回退（自动关复选框并告警）。
- 复选框**关**但元数据缺失或不完整（首次导入 / 清缓存后重导入 / 旧算法版本）→
  仍然预生成或修复；只记 stdout，不弹告警、不自动关复选框、不归集 `SkeletonGroup_<N>` 合集。
- 复选框**关**且数据完整 → 整段不执行（幂等快路径），零文件读写。

**自愈范围（用户要求）**：ZZMI 的判据比 EFMI 同名方法更严——除 `VGMap` 家族缺失外，
还包含快路径完整性 `_zzmi_cache_intact`（算法版本、`SkeletonGroup`/`DeformDrawIndex`/
`OriginalVertexCount`、`VGMap` 覆盖度、palette 缓存存在且大小达标、通道计划版本与内容一致）。
因此「数据存在但过期/残缺」也会在**下一次导入**（无论复选框状态）被处理：能就地升级的
就地升级（`MIGRATABLE_VG_MAP_ALGORITHM_VERSIONS` 里的旧版本只凭 `VGMap` 补派生字段，
**不需要提取文件**），其余按当前提取源重建、提取源不在则用工作空间缓存重建。
刻意**不**在判据里做 log.txt 全量哈希比对（否则每次导入都要哈希整个 log）——因此
**换用新的提取文件需要重新选择提取源并重新导入**；提取源记录失效时也**不会**自动改用
游戏目录里其它帧（`resolve_frame_analysis_dir`，防跨帧/跨模型混用，`tests/test_zzmi_skeleton.py`
的 `test_deleted_explicit_dump_does_not_fallback_to_latest_other_dump` 钉住）。

契约测试：`tests/test_zzmi_merged_data_generation_gate.py`（门控形态）+
`tests/test_zzmi_skeleton.py::ZZMIMergedMetadataGateTests`（判据行为）。

### 5.3 导入消费（走现有双条件路径，最小适配）

现有顶点组导入已是双条件门控：`json 有 VGMap and import_merged_vgmap()` → 全局索引；否则局部索引（判定 = `common/ssmt_import_helper.py` 的 `merged_vgmap_enabled = GlobalProterties.import_merged_vgmap() if use_merged_vgmap is None else bool(use_merged_vgmap)` 与紧随其后的 `submesh_json.VGMap if (... and VGCount > 0)`；另见 `common/mesh_create_helper.py`）。ZZMI 侧只需让 mesh 创建在读子网格 json 时识别本方案写入的 VGMap 段——**有则用、无则现状**，不改动任何默认分支。实施时先核对 ZZMI 导入实际读子网格 json 的位置再定点接入（一个小适配点，不重写导入）。

**预生成与消费解耦后的覆盖值（2026-09 用户要求）**：复选框关闭时，`create_mesh_from_json` 的
`use_merged_vgmap` 传 `None`（= 沿用全局选项 ⇒ 局部索引），**即使 json 里已有 §5.2 预生成的
VGMap 也不改变导入结果**；复选框开启时仍传预生成结果（失败即整批 `False`，真正回退普通导入）。

**分组合集（2026-08-24 实施）**：分组版下，一键导入把每个子网格对象移入其骨架组合集 `SkeletonGroup_<N>`（挂在 LOD 合集下，颜色轮换区分；`ui/ui_func_import_ssmt.py:_zzmi_move_to_skeleton_group_collection`）。json 无 SkeletonGroup 字段（旧缓存/未生成）时保持原合集归属，零副作用。

### 5.4 导出（本期实现：A 模式，与 EFMI 同构的运行时合并骨架）

**闭环原理**：所有子网格共用一套全局顶点组（全局骨架）。导出时 BLENDINDICES **直写全局骨骼 id**；运行时由 INI 挂载的 CS 把各部件的实时 palette 拷进一块**合并骨架 buffer**（按 vg_offset 摆放），并把每个 deform pass 的 vs-t0 换绑到合并骨架——deform VS 拿全局索引直接蒙皮。**任何全局骨骼对任何部件可见，组件 A 可以刷组件 B 的权重。**

门控（与导入同一把复选框）：`LogicName.ZZMI and GlobalProterties.import_merged_vgmap()` 且子网格 json 有 VGMap → 走合并骨架导出；否则**完全现状**。

实现要点（数据层参照 EFMI 已验证模式；**着色器与 INI 段落全部按 ZZZ 数据布局从零编写**，见第 3 条）：

1. **全局索引化导出**：顶点组名 = 全局 id 数字串；导出前预处理——补缺组（fill_gaps）、剔 ignore/全局骨架外组、按名排序/改名保证 `g.group == 全局 id`（对照 NTEMI `_sort_export_vertex_groups_by_name` 与 EFMI ObjectMerger 的 `str(index)` 改名）。挂钩点：`common/submesh_model.py` BLENDINDICES 生成处（EFMI 升宽分支 :89-101 同位置加 ZZMI 分支）。
2. **升宽——实测后取消**：ZZMI 各 gametype 的 BLENDINDICES **本来就是 32 位通道**（BI16=`R32G32B32A32_SINT`、BI8=`R32G32_UINT`、BI4=`R32_UINT`——后缀数字是**字节数**不是位数），全局骨骼 id 直接装下，**无需升宽、无需 ElementFormat 行**（EFMI 的升宽是因为它的 BI4 是 R8 四通道 255 上限，与 ZZZ 无关）。实现已按此落地，导出 buffer 实测通过（见 §8）。**每顶点影响数超槽位的口径（2026-09-21 用户裁定修正）**：不再 Fatal 中断导出——与其它所有游戏路径（`get_blendweights_blendindices_v1/v3/v4_fast`）一致，自动保留权重最强的 N 个并按**幸存权重之和**重新归一化（`utils/vertexgroup_utils.py` 的 `get_blendweights_blendindices_for_layout`），损失量由 `common/obj_buffer_helper.py` 的 ZZMI 合并分支汇总后打印（超限顶点数 / 最大丢弃权重占比 / 样本顶点索引），占比 ≥5% 时额外提示回 Blender 用 清理 Clean → 限制总影响数 Limit Total → 归一化 All。旧口径「任何形式的降宽一律报错」在权重平滑产生大量极小尾巴时会让整次导出无法进行，已废止。
3. **INI Merged Skeleton 段与 CS 着色器——按 ZZZ 数据布局从零编写（不移植 EFMI 实现）**：EFMI 的段落/着色器面向终末地的渲染 draw + instance config 体系，**只借鉴「逐组件 attach + 换绑合并骨架」的概念**，以下要素全部按 ZZZ 实测重新定义：
   - **CS（零延迟版）**：`Toolset/zzmi_merged_skeleton_attach.hlsl`——输入 = 当前 deform pass 的 vs-t0（cs-t0 保存的当帧 palette，`StructuredBuffer<ZZBone3x4>`，stride 48）+ **vg_map 表**（cs-t1，`StructuredBuffer<uint4>`，槽位在 .x；使用二进制文件加载，避免本 fork 的多行 data 只写入第 0 个元素）；输出 = **本组本槽**的 `RWStructuredBuffer` 合并骨架（全宽，槽位 = 全局骨骼编号）；逻辑 = **按 vg_map 写槽位** `merged[vg_map[id].x] = palette[id]`（本部件引用的骨骼——含跨部件共享 canonical——当帧覆盖）。无 cb1 输入、无校准乘（F2 前的口径）。**每个 (部件, 槽) 一个 attach 段（`CustomShaderZZMIMergedSkeletonAttach_C<i>_s<k>`），由 deform VB 段在顶层无条件 `run` 全部 槽×部件**（`run` 绝不在 if 内：本 fork 中 if 内的 run 不执行 → 骨架为空 → 模型消失）；**2026-10-10 F1 追加**：跨组归属的部件再额外发射 `CustomShaderZZMIMergedSkeletonAttachX_C<i>_g<宿主组>_s<k>`（`cs-t0` = 异组 palette、`cs-t1` = 异组 VgMap、`cs-u0` = **宿主组**骨架缓冲、`Dispatch = ceil(vg_count/64)`），同样在每个 deform VB 段的对应槽里 `run`；**2026-10-10 F2 追加**：AttachX 段改用 `Toolset/zzmi_merged_skeleton_attach_calibrated.hlsl`——同一 CS 接口 + `cbuffer ZZCb1Src : register(b1)` / `ZZCb1Dst : register(b2)`（`cs-cb1` = 源组 cb1、`cs-cb2` = 宿主组 cb1），逻辑 = 先算 `C = U_宿主⁻¹×U_源`，再 `M' = C×M` 写宿主组槽位，形态校验失败则直拷兜底（同组 attach 仍用直拷版，零改动）；**2026-10-10 F3/F3-CS 追加**：跨组合并几何的落笔改为**顺序无关**——宿主与每个跨组供骨件都在自己的 deform 段、按「全部供骨件当帧到达」的守卫派发 `CustomShaderZZMISkin_G<宿主组>_s<槽>`（蒙皮 CS，carrier = 宿主 vb0/vb2、目标 = 宿主 SO 缓冲），一帧内只在最后一个供骨件到达处闭合一次（幂等）；宿主原有的出现次门控 draw 保留为兜底。`[Present]` 只把 `$zz_ms_occ_*` / `$zz_ms_seen_*` 清零，不重放 palette、不复位任何资源。
   - **INI 匹配键**：挂点段用 `checktextureoverride` 匹配 deform pass 的 **vb0/vb2 hash**——这两个键**工作空间子网格 json 里就有**（`CategoryHash.Position` / `CategoryHash.Blend`），生成器直接取用；NPC 的 deform pass hash 不在列表天然排除。
   - **换绑**：attach 后把该 pass 的 `vs-t0` 换绑到合并骨架 Resource（SRV 视图按 stride 48 声明）。
   - **与 ZZMIv1 的组合**：ZZMIv1 的 skin commandlist 在同一批 deform draw 上做 vb0-3/ib 的 mod 替换（全局索引 vb2 即经此路径生效）与 ps-tXX 清理，**不触碰 vs-t0**；我方段落只做 palette attach + vs-t0 换绑，职责不重叠。commandlist 先后次序与守卫需游戏内验证（任务 7）。
   - **渲染 draw 不动**：渲染 pass 的 vs-t0（共享 `7dfb0292`）与 vb2 维持 ZZMIv1 现状；渲染 VS 是否消费 vb2 需游戏内观察（风险 R3）。**2026-09 v2 追加**：合并网格自动重定向下 carrier 的渲染段也不再覆写 `vb0`（游戏按实例绑定本实例 deform SO），只用 `base_vertex` 读合并段。
4. **ZZZ 运行时挂载点与帧对齐设计（2026-08-25 定案：零延迟逐 pass attach）**：11 份 palette 由 CPU 逐 pass `Map`（WRITE_DISCARD）上传且 ring buffer 复用。**渲染侧存在当帧角色级绑定矩阵（dump 143256 实证：渲染 vs-cb2 = 身体正向 + 头部逆向绑定表，每帧 Map 更新；渲染 VS/PS 消费当帧绑定；渲染 vs-t0 = 7dfb0292 部件参数表 128 矩阵对）**——"慢一帧"的 SO × 当帧绑定 = 运动时逐帧错位（静止时帧差≈0，故 dump 数据层正常；这正是"只要采用骨骼合并就错位、不合并（SO 当帧）不错位"的根因，用户实测）。因此：
    - **"全部上一帧"（Present 时序 attach）已废弃**：慢一帧与渲染当帧绑定不兼容。
    - **定案 = 零延迟逐 pass attach**：每个 deform pass：`pre` = 把该 pass 当帧 palette **copy 成持久资源** `ResourceZZPalette_<DrawIB>` → **立即 run attach CS**（cs-t1 = vg_map 表，按「局部骨骼 id → 全局槽位」写入本组骨架；本部件引用的全部骨骼——含跨部件共享的 canonical 槽位——此刻即为当帧内容）→ `vs-t0` 换绑为本组骨架 → draw 蒙皮。**deform 读到的 = 当帧姿态**，与渲染当帧绑定一致。逐 pass attach 只需本部件当帧 palette（copy 时刻有效），不依赖"当帧全套并存"（旧设计否决的只是帧尾拿全套）。
    - **2026-08-26 实测修正（渲染顺序不稳定）**：好帧与坏帧的 palette copy 均成功，但同组 target/carrier 到达顺序不同；target 先到时固定 target draw 会读取尚未被 carrier 当帧覆盖的槽位。修复为「逐组件到达标记 + 合并可见 draw 依赖守卫」：carrier/target 挂点都保留 guarded draw，只有所需 palette 全部当帧 attach 后的挂点重放一次；因此不依赖固定 DrawIB 顺序，也不引入整帧延迟。**2026-09 多实例分离 v2 修正**：该守卫不再带帧级闩锁——重放后即时清零相位与本组 seen，**每个实例各重放一次**（详见 §7 修复链 6）。**2026-09 v9 修正**：单份 palette/骨架/SO 在两个实例交错覆盖时仍会产出半帧拼接的骨架（运动抖动、罕见姿态反转）——改为**出现次槽位（s1/s2 循环）+ 每槽守卫**：资源按槽分份，attach 也只写该槽，守卫按槽判定「本组全部部件在该槽都已当帧到达」（详见 §7 修复链 7）。
    - **帧尾 [Present]**：**先把 `$zz_ms_seen_*` 抄进 `$zz_ms_prev_*`，再清零 `$zz_ms_occ_*` / `$zz_ms_seen_*`**（跨帧兜底；依据 `ui/universal/zzmi.py` 的 `add_merged_skeleton_sections` 里 `[Present]` 段的 `prev = seen` 抄录语句）；**帧级闩锁 `drawn`/`ready` 及相位计数 `$zz_ms_group_phase_*` 已废除**（v9 的槽位本身承担实例分离，不再需要相位）；**不再重放持久 palette**，也**不在帧末复位 `ResourceZZRedirectSO_G<g>_s<k>`**（F8 已回退：该语句会废掉 [Present] 清场，实例加入/剔除的过渡帧残留半组状态 → 错槽重放，后加入实例闪烁直至卡死）；否则会把缺席部件/上一实例的内容重新灌入骨架，形成脏数据。
    - 效果：所有部件当帧姿态；首帧即正确（无自愈期）；**未生成组件走游戏原渲染（当帧 palette），与合并部件天然同帧一致，无需任何延迟机制**。
    - 备注：共享骨骼 canonical 槽位被后 deform 部件 attach 覆盖（同帧 bitwise 相同，覆盖无害）；先 deform 部件的 SO 已在 deform 时固定，不受后续覆盖影响。
5. **跨部件/跨组权重**：同组（相同对象空间）直接引用合法——组内统一骨架；**跨组别骨骼合并（2026-10-10 起：默认关闭的实验总开关 `cross_group_merged_vgmap_test`）**——关闭时导出侧 `_warn_cross_group_bone_references` 对引用非本组骨骼 id 的部件报警「禁止跨组别骨骼合并 … 原点塌陷」；开启时 F1 把跨组件骨骼写进**吸收它的宿主组**缓冲，F2 再用两组的渲染 cb1 把骨骼从源对象空间**校准**到宿主对象空间；守卫改为报「跨组骨骼合并已接入 F1」+ 残余说明。用户仍可只把同组部件 join 到同一对象（不依赖本开关）。
6. **未生成组件（2026-08-25 定案：零延迟后无需任何机制）**：合并部件 deform 输出当帧姿态，未生成部件走游戏原渲染（当帧 palette）——两者天然同帧一致，**延迟双缓冲机制（`ResourceZZDelayedPalette_<>` / `TextureOverride_VB_ZZDelayed_<>`）已整体废弃删除**。导出子集时未生成部件保持原版渲染即可。

## 6. 任务拆解（TDD，每步可独立验收）

| # | 任务 | 状态 |
|---|---|---|
| 0 | **实证测定**：矩阵步长 = 12 floats（48 字节/骨骼）；palette 数 == 用量；合并骨架 Σ 266 槽 / 全局唯一 244；升宽需求后经实测取消（全 32 位通道） | ✅ 结论在 §2.5 |
| 1 | `ZZMILogParser` + 单测 | ✅ `tests/test_zzmi_skeleton.py`（22 deform pass、SO/vb0/SRV/IB 绑定、dump_map） |
| 2 | `ZZMIDeformResolver`（A/B/C 三 join）+ `ZZMIBoneMapBuilder`（同部件不去重 + 跨部件 bitwise）+ 单测 | ✅ 15 子网格全部命中；48625d6d `#1/#8/#9` 异帧不误并回归测试在列 |
| 3 | `ZZMISkeletonMergeHelper.ensure_skeleton_data`：回写 json + ModImpRuntime 缓存 + 幂等 | ✅ 15 子网格写回；二次运行全跳过；force 重建 |
| 4 | 导入门控（`ui/ui_func_import_ssmt.py`，ZZMI + 复选框）；VGMap 消费走 `create_mesh_from_json` 既有双条件路径（零导入改动） | ✅ |
| 5 | 导出侧：`common/submesh_model.py` ZZMI 预处理分支（补缺/剔 ignore/排序/紧凑改名，`g.group` == 全局 id）；无需升宽 | ✅ buffer 实测：全局 id 正确写入（64d7d56f/454ff522 全为 7；b20f90ea 51 个使用 id 含 13 个跨部件引用） |
| 6 | Merged Skeleton CS + INI（按 ZZZ 从零编写）：`Toolset/zzmi_merged_skeleton_attach.hlsl` + `ExportZZMI` 生成 Constants/Resource(RWStructuredBuffer stride 48)/CustomShader 段 + 逐 deform VB 段注入换绑/attach | ✅ 生成 INI 校验通过；单测 `tests/test_zzmi_merged_skeleton_ini.py`（含跨组引用守卫用例） |
| 6.5 | **移除 CB1 校准（2026-08-25 用户拍板）**：删校准 CS/捕获段/`SkeletonGroupCb1SourceIb` 字段/校准数学测试；attach 退化为逐部件纯直拷；导出侧新增跨组别引用大声报警 | ✅ 单测 + 真实数据 e2e 全绿 |
| 6.6 | **跨组骨骼合并（2026-10-10：F1 写进宿主组 + F2 渲染 cb1 校准）**：开关 `cross_group_merged_vgmap_test` 由死开关变真开关——F1 新增 AttachX 段（异组 palette → **宿主组**缓冲）+ deform 段逐槽 run + 守卫改措辞；F2 新增 `[ResourceZZCb1_G<N>]`（各组部件自己的渲染 draw 处 `copy vs-cb1 unless_null`）+ `Toolset/zzmi_merged_skeleton_attach_calibrated.hlsl`（`M' = C×M`、`C = U_宿主⁻¹×U_源`，形态校验失败直拷兜底）+ AttachX 段 `cs-cb1/cs-cb2`；关闭时产物逐字节同历史 | ✅ 单测（`ZZMICrossGroupAttachTests` 10 / `CrossGroupConsumerContractTests` 4 / ZZMI 全套 325 passed, 35 skipped） |
| 6.6 | **v9 出现次槽位 + 每槽守卫（2026-09 用户游戏内实测通过）**：单份 palette/骨架/SO 在多实例交错时产出半帧拼接骨架（运动抖动/罕见姿态反转）→ 改为 s1/s2 出现次槽位 + 按槽到达守卫；`run` 全在顶层、seen 顶层 sticky 累加、守卫体内只有绑定与 draw、`[Constants]`/`[Present]` 只声明/清零 occ/seen | ✅ 生成器 + 单测全绿（语义基准 = 手改 `浮波柚叶.ini` 8571B） |
| 6.7 | **CB1 对象变换按绑定窗口解析（2026-09-16 修复，算法版本 v4）**：`parse_object_transform` 旧实现无视 `VSSetConstantBuffers1(first_constant=N)` 窗口、固定读 float 0，且 `>512B` 一律返回 None——多对象共享 cb1 数组里 float 0 是**别的对象**的矩阵，导致同对象空间的部件被拆进不同 `SkeletonGroup`/合集（实证 `FrameAnalysis-2026-09-16-014450`：4096B 资源按 first_constant=0/32/64 切给三个对象，draw 66 的窗口 64 与 draw 69 的逐部件块逐位同空间，却被分成两组）。改为：`parse_object_transform(cb1_path, first_constant)` 按窗口取 16 floats、越界才 None；`ZZMILogParser` 采集 `VSSetConstantBuffers1` 绑定窗口并新增 `get_vs_cb_first_constant`；窗口起点随 `ObjectCB1FirstConstant` 写回 json（无 dump 重建同口径） | ✅ 单测（窗口选择/与逐部件块同组/越界 None/真实 dump 复算）+ 全量 1892 通过 |
| 7 | **端到端**：Blender headless（`.dbg/run_zzmi_headless_validation.ps1` + `bl_zzmi_headless_validate.py`）导入+导出 [PASS]；**游戏内实测（ZZMIv1 加载、跨部件权重、帧内一致性）待用户侧执行** | ⏳ headless ✅ / 游戏内待测 |
| 8 | 文档：CONTEXT.md 增补词条（Deform pass / Merged skeleton / VGMap） | ✅ |

**测试数据（用户指定，已确认配对）**：工作空间 `K:\SSMT-Package-master\WorkSpace\ZZMI\希格莉德·空岛传奇` ↔ dump `K:\SSMT-Package-master\3Dmigoto\ZZZ\FrameAnalysis-2026-08-19-122152`（`Config/FrameAnalysisPath.json` 指向一致；其他工作空间可能没有配套提取文件，不作为测试对象）。

## 7. 风险与后续可选

**风险清单**：

- R1（已消解）：矩阵步长 → 实测 12 floats/48 字节，无歧义（§2.5）。
- R2（已设计消除，附边界）：跨部件骨骼帧内不一致 → 按 §5.4-4「逐组件到达标记 + 合并可见 draw 依赖守卫」设计消除；不使用上一帧骨架或 Present 重放。**已知限制（2026-09 多实例分离 v2 / v9 出现次槽位）**：v9 的每槽守卫只校验「本组全部部件在该槽已当帧到达」，槽位是位置相关的——两个 pass 的实例提交顺序相反时出现次槽位会配错；某部件整帧被剔除时该槽守卫**不闭合**（保持上一帧内容，方向安全，不会画出半帧拼接），靠 [Present] 清零 occ/seen 兜底。v2 时代的另两条边界已随 v9 消失：无闩锁后 carrier 渲染 drawindexed 无条件、该场景可能读到上一帧 SO 尾部（导出期由 `_warn_merged_mesh_timing` 大声报警，N3）；`drawindexed` 隐含的 SO 容量约束（F9）。全部为 low、不阻塞，已写进生成器注释（F7 / N3 / F9）。
- R3（待游戏内验证）：渲染 draw 绑定的 vb2 也是全局索引版本（ZZMIv1 对渲染 draw 同样替换 vb2）——渲染 VS 不蒙皮（顶点已蒙皮），但若它把 blend 索引用于其他用途（描边/遮罩），全局 id 可能引起异常；任务 7 观察，若中招则只在 deform pass 替换全局索引 vb2、渲染 draw 保留原布局。
- R4（待游戏内验证）：我方 Merged Skeleton 段与 ZZMIv1 skin commandlist 在同一 deform draw 上的执行次序（vb2 替换必须先于/不干扰 vs-t0 换绑）；ZZMIv1 不触碰 vs-t0，静态分析无冲突。
- R5：ring buffer 复用 → 一律按 deform pass 的 dump 逻辑文件名定位 palette，禁止按资源 hash 全局搜索。
- R6：复选框默认 True：ensure_skeleton_data 失败必须静默降级（打印 + 不阻断导入），不得让「无 dump 的老工作空间」导入失败。
- R7（已修复，用户实测踩坑）：**合并物体 + 删面/删空组导致的两类导出事故**：
- R8（已回退）：**双缓冲合并骨架尝试失败（游戏内模型爆炸）**——[Present] 帧翻转 + 前后缓冲条件换绑的写法在 ZZMIv1 运行时下不成立，已回退到单缓冲（`vs-t0 = ResourceZZMergedSkeleton` + draw 后 attach）版本，该版本经两份游戏内 dump 实证数据正确。教训：ZZZ 管线 INI 改动必须游戏内验证，dump 数据正确不等于运行时行为正确。
- R9（已修复，2026-08-26 双帧对照）：**固定 target draw 顺序假设不成立**。好帧 `a23/b20 → b30 target`，坏帧 `b30 target → a23/b20`；两帧 copy 均成功，坏帧读到的是单缓冲内未完成的当帧组合。修复为单缓冲依赖就绪 draw，不使用 R8 已失败的 A/B 翻页。
  - **组号移位塌陷**：合并模式组名=全局骨骼 id，若数字组被删出缺口且预处理做「紧凑化重命名」，缺口之后的全局 id 全部 -1 移位 → 后续部件权重集体错位、模型塌陷消失（用户实测「删了手臂、下半身消失」即此）。修复：`_prepare_zzmi_merged_skeleton_vertex_groups` 无条件补缺 + 不删任何组 + 排序后恒等重命名 + 非数字组名告警（`common/submesh_model.py`）。
  - **对象映射丢失**：join 多个物体后只有幸存名字的部件有对象，其余部件无 DrawCall → 输出空 IB（`ib=null`）→ 游戏内整件消失。修复（三层）：① `ExportZZMI._warn_missing_drawib_parts` 大声报警列出缺失部件；② **占位小三角面自动补齐**（用户拍板）：复选框开启时，缺失部件自动创建 1e-6 三角面占位对象（权重给组 "0"，统一顶点组下恒在范围内），游戏内不可见、不再 ib=null，顺带成为「故意删除部件」的合法手段；导出后自动清理 + 残留自愈；③ **占位不是无条件的**（用户修正）：部分缺失的 DrawIB 直接补；**整个 DrawIB 缺席时**读其 VGMap 全局骨骼 id、检测是否被现存对象顶点**他属**引用（权重>0 且该数字组名不在引用方对象自身 json `VGMap` 值域内；2026-10-14 用户修正——引用方自己声明的槽位（含去重合并组借位同槽）不算证据，见 `docs/zzmi-efmi-placeholder-parity.md` §6.4-4）——被引用 = 几何已被合并进别的对象 → 全组件补占位（抑制原版防重影）；零引用 = 用户故意不生成 → 不插桩（该 DrawIB 不进 mod，游戏内显示原版）。无反查数据的缺席 DrawIB 一律不插桩。**更进一步的「合并物体按面来源拆分导出」仍列后续可选。**

**后续可选（本期不做）**：

- **B 模式降级**：导出时按 VGMap 反映射回局部索引（零运行时依赖，但跨部件权重会被越界剔除）——作为无法使用运行时合并骨架环境（如加载端不兼容）时的降级选项，按需另起。
- ~~合并物体按面来源拆分导出~~ **已被更优解取代（用户拍板）**：目标组件继续由**物体前缀**控制（现有体系）；跨组件改派 = 分离面到新物体 + 改前缀；缺失部件由条件占位小三角兜底。已实测：全 join 成一个对象的导出结构自洽（join 目标组件画完整合并网格 drawindexed=122979，其余组件全部不可见占位，无 ib=null/重影），见 `.dbg/bl_zzmi_joined_validate.py`。
- palette 缓存的跨工作空间复用/共享池。
- ZZZ shapekey（CS morph 池）与 mod 的联动（48625d6d 类脸部部件）。

**游戏内 dump 实证记录**（FrameAnalysis-2026-08-22-094614 / 212619 / 224434 / 08-23-001555）：换绑生效（hooked pass 的 vs-t0 无 dump、未 hook 的正常）、attach 偏移与 json 逐一吻合、合并网格内头发顶点用 live palette 重建 ≈ 实际 SO 输出（残差 0~0.0004、零系统性偏移）、各部件渲染 draw 的 CB 窗口完全一致（无逐部件原点差）、跨部件内容位置在带内。**用户游戏内持续观察到的偏移在三份出错 dump 的数据层均不存在**——2026-08-24 起按两个新根因修复（刚性锚点抓帧重合误并 + cb1 对象空间分组）；2026-08-25 用户拍板放弃 CB1 校准（组内统一骨架 + 禁止跨组别合并），等待重导 + 重导出后游戏内复验证；若仍在，回到"偏移正显示的那一帧抓 dump + 截图"的诊断路线。**2026-10-10 复现件证据链（跨组合并部件消失）**：`洛克茜00\洛克茜.ini骨骼合并`（13682 B / 551 行）attach 段 `C0→G0`（行 127-192）+ 宿主变形段绑 `G2`（行 242-268，`draw = 4562, 0`），而合并 Blend（`Meshes\4e6e989e-Blend.buf`，4562 行）中 **624 行**的骨 id 为 `{1,2}`（宿主组 G2 的合法槽位是 4..78）⇒ 这批顶点读 G2 槽位 1/2（永远无人写入）= 未初始化矩阵 ⇒ 部件消失；导出侧 `_merged_component_geometry_absorbed` 的判据只看**本组合法槽位**，跨组引用不在判定范围内（本次修复的入口）。

**修复链（首项 2026-08-23；三帧游戏内 dump 实证：164525 / 170515 / 171955，用户复验通过；后续各项以自身日期/实证标注）**——此前"偏移离谱/炸模/消失"的真根因不在时序，而在 attach CS 的两个运行时细节：

1. **ini 参数布局（本 fork 与标准版不同）**：标准 3DMigoto 的 CustomShader 参数是 4 个一组从 `IniParams[0]` 起（`IniParams[0]=(x1,y1,z1,w1)`，mouse.hlsl/3dvision2sbs.hlsl 实证）；**本 3DMigoto-Armor fork 的 `y1` 在 `IniParams[1].y`**。读错位置的行为差异（同一 CS 三帧实测）：
   - 读 `IniParams[1].y`（=y1）：count=vg_count 正确 → attach 执行；
   - 读 `IniParams[0].y`（=x1=0）：count=0 → attach 一根不写 → **G3 全零 → 蒙皮全部塌向原点 → 模型消失**。
   - 结论：`#define ZZ_ATTACH_COUNT IniParams[1].y`（`Toolset/zzmi_merged_skeleton_attach.hlsl`）。
2. **vg_map 多行 data 只写第 0 个元素**：`[ResourceZZVgMap_<DrawIB>] type=Buffer format=R32G32B32A32_UINT data = <slot> 0 0 0`（每行一个元素）在本 fork 上**只写入第 0 个元素**——CS 其余线程 `vg_map[i]` 越界读到 0 → **全部骨骼塌进 slot 0**，G3 仅 3 槽非零（`[0, 79, 88]`，slot 79/88 = 各部件 palette[0] 写入正确、slot 0 = 塌陷）→ 蒙皮 246/249 骨骼用零矩阵 → **模型炸裂**。修复：导出器把 vg_map 写成**二进制文件**（`Meshes/zz_vgmap_<DrawIB>.buf`，每元素 4×uint32 = 槽位值,0,0,0），INI 改 `filename` 加载（与 VB 资源同一路径，buffer 大小由文件决定，视图必然覆盖全部元素）；HLSL 侧 `Buffer<uint4> vg_map` 与 `format=R32G32B32A32_UINT` 精确匹配（`ui/universal/zzmi.py` `add_merged_skeleton_sections`）。
3. **合并网格自动重定向（任意 IB 挂载兑现）**：palette 是 per-pass 独立 Map 上传的 ring scratch（同资源 hash 帧内两次 dump 内容不同，141c7638→8a40ccd0/0b9416aa 实证），早 deform pass 时刻读不到晚 pass 部件的当帧骨骼——因此**合并网格（同组跨部件 join 成一个对象）物理上只能在组内最后一个 deform draw 蒙皮**。为兑现「用户可自由 join 到任意 IB」的设计承诺，导出器自动重定向（`_build_merged_mesh_redirect_plan`）：carrier（合并网格挂载的 DrawIB）deform 退化为 3 顶点 stub（保留 copy palette + attach 写当帧骨骼）；组内最后 deform draw 的 DrawIB（target）deform 追加画合并网格（绑定 carrier 的 vb0/vb2，SO 按 [target 完整导出顶点（含 stub）][merged...] 拼接，保证 target 的 remapped IB 与 SO 顶点偏移一致）；**render 阶段每个子网格仍使用自己的 hash/first_index 和 IB；carrier 只加 `base_vertex`（`drawindexed = <merged_count>,0,3` 从 SO 的第 3 行起读本段），渲染段不再覆写 `vb0`——游戏渲染 draw 的 vb0 天然是本实例 deform 的 SO（地面真值：`so0`=渲染 vb0 指针族严格分实例），重放已把本实例的合并行写进去（2026-09 多实例分离 v2，见本节修复链 6）；target/缺失部件保留极限小三角占位，不以 `ib=null` 静默跳过**；VertexLimitRaise 按 SO 实际大小重排（carrier=3 / target=Σ）；合并网格的渲染换绑 `vb1 = Resource<carrier>Texcoord`（导出顶点超原部件顶点数时，防 OOB UV）。支撑数据：json 新增 `DeformDrawIndex`/`OriginalVertexCount`（`common/zzmi_skeleton.py` 反查写回；幂等门控纳入，旧缓存自动整批刷新），透传到导出侧（`common/submesh_model.py`/`common/submesh_metadata.py`）。无法自动重定向的情形（缺 DeformDrawIndex / 配置跨 IB）由 `_warn_merged_mesh_timing` 大声报警并给出改名指引。
4. **游戏内复验（用户确认 2026-08-23）**：同组内合并（组 3 全部部件 join 成一个对象）蒙皮正确、不再炸裂/消失。G3 骨架由「3/249 槽非零」修复为全量写入（170 槽左右，分布 79..248）。
5. **边界/调度泛化修复（2026-08-24 审计）**：attach CS 曾残留测试角色专用的 `slot < 249`，会把 G4 的 249..265 全部拒写；INI 又固定 `Dispatch = 8`，在单部件 palette 超过 512 根时会漏写尾骨骼。现改为用 `src_palette`/`vg_map`/`merged_skeleton.GetDimensions` 校验真实资源边界，输出槽位上限随每组全宽骨架的 `array` 自动变化；Dispatch 按 `ceil(vg_count / 64)` 生成。回归测试覆盖 G4 全 17 槽与 513 根 palette。
6. **2026-09 多实例分离 v2（同一 IB 在场景中被画多次；用户游戏内实测通过）**：
   - **症状**：同一 IB 的多个实例（同一帧内多个渲染 draw / 多个对象变换）只有第一个实例姿态正确，其余实例用上一个实例或上一帧的骨架画合并几何（抖动、错位、卡在第一帧）。
   - **根因**：旧的（下称 v1）就绪守卫带**帧级闩锁** `$zz_ms_group_ready_g<N>` / `$zz_ms_redirect_drawn_<IB>`，只在 `[Present]` 复位——同一帧第二个实例即使重新 attach 齐自己的 palette 也永远进不了守卫；而 carrier 自己的 deform 已被替换成 3 顶点 stub、渲染段又被显式换绑到**同一个** `ResourceZZRedirectSO_<target>` 资源变量并用 `if drawn == 1` 包住，于是后续实例的 `drawindexed` 消费的是未写入/别的实例的 SO。
   - **修复（已固化进生成器 `ui/universal/zzmi.py`）**：① 守卫条件只留「全部 seen + `$zz_ms_group_phase_g<N> >= 组内部件数`」，重放后**即时**清零相位与本组 seen = **每实例一轮**，同一帧每个实例各自重放一次；② 渲染段不再覆写 `vb0`、不再包 `if drawn`，`drawindexed` 无条件执行（游戏按实例绑定本实例 deform SO）；③ 直连路径的一次性 draw 去掉 `if !$zz_ms_redirect_drawn_<IB>`，同样每实例一次；④ `[Constants]`/`[Present]` 不再声明/复位任何闩锁变量；⑤ 保留每实例 deform 段自己的 `ResourceZZRedirectSO_<target> = ref so0` 捕获（每轮捕获→重放窗口紧邻）；**`[Present]` 不得复位该资源变量**（原 F8 防御性构造 `ResourceZZRedirectSO_<target> = null` 经 2026-09 游戏内实测有害：该语句会废掉 `[Present]` 段的正常执行 → seen/phase 跨帧清场失效 → 第二实例加入/被剔除的过渡帧残留半组状态、错轮重放，表现为后加入实例闪烁直至卡死无动画；已回退并加回归断言）。
   - **边界（不阻塞，已作为已知限制写进生成器注释）**：① 组件缺席/重放不可用时守卫不闭合，可能沿用上一轮残留 seen 重放一次（[Present] 每帧兜底，F7）；② 无闩锁后 carrier 渲染 drawindexed 无条件，该场景可能读到上一帧 SO 尾部（导出期 `_warn_merged_mesh_timing` 大声报警，N3）；③ `drawindexed = <merged_count>,0,3` 隐含「本实例 SO 容量 ≥ 组内 SO 总行数」，容量由 SO owner 部件的 VertexLimitRaise 声明，非 owner carrier 组合会打印显式诊断（F9）。
   - **对照产物与回归**：手修版 `浮波柚叶.ini`（6910B，用户实测通过）作为语义基准固化进 `tests/test_zzmi_merged_skeleton_ini.py`（守卫仅 seen/phase、渲染无 vb0 覆写、Constants/[Present] 无闩锁变量、`ref so0` 捕获保留、`[Present]` 不含任何 RedirectSO 复位语句（F8 已回退））。**v9 起该基准升级为手改版 `浮波柚叶.ini`（8571B）**，见下条。
   - **v2 遗留的已知限制（已被 v9 取代）**：守卫只校验「全部 seen 已置位」，不校验这些 seen 是否来自本轮——组件缺席/重放不可用时会沿用上一轮残留 seen 重放一次（一帧级旧姿态）；这是 v9 引入出现次槽位的直接动机之一，见下条。
7. **2026-09 v9 出现次槽位 + 每槽守卫（用户游戏内实测通过）**：
   - **症状**：运动时合并几何抖动；罕见情况下整帧姿态反转/混淆（同一 IB 在场景中被画多次时尤其明显）。
   - **根因**：v2 只保存**一份** palette / 骨架 / SO，并用「组内部件当帧全部到达」守卫。同一 IB 的多个实例的 deform pass 会**交错覆盖同一份资源**：第一个实例的部件 A 写入后、部件 B 尚未到达时，第二个实例的部件 A 又把它覆盖成自己的矩阵 → 守卫成立时消费到**半帧拼接**的骨架（A 来自实例 2、B 来自实例 1）→ 抖动；两个 pass 的实例提交顺序相反时表现为姿态反转/混淆。v6（每次 deform 无条件重放）尝试绕过交错，反而更差：第一个部件到达时另一个部件的数据还是上一帧的，那一笔重放本身就是错的。
   - **v6 之前各版本的失败原因（一并固化，防止回归）**：
     - **帧级闩锁**（v1 `$zz_ms_group_ready_g<N>` / `$zz_ms_redirect_drawn_<IB>`，只在 `[Present]` 复位）→ 同一帧第二个实例永远进不了守卫却仍消费本实例的 SO；
     - **if 内赋值被静态折叠** → 只在 if 体内赋值的 `$变量` 被加载期优化器按初值折叠，整个守卫 if 被删除、重放根本不发生；
     - **`run` 进 if 不执行** → 本 3DMigoto fork 里 if 体内的 `run = <CustomShader>` 不执行，骨架为空，模型整体消失（这是 v3/v4 失败的真正原因）。
   - **修复要点（已固化进生成器 `ui/universal/zzmi.py`，语义基准 = 手改版 `浮波柚叶.ini`）**：
     1. **出现次计数**：每个部件的 deform 段**顶层**自增 `$zz_ms_occ_<i>`，在 `if $zz_ms_occ_<i> >= 3` 里回绕为 1（槽位 1/2 循环）；
     2. **当帧到达标记（sticky，顶层赋值）**：`$zz_ms_seen_<i><k> = $zz_ms_seen_<i><k> + ($zz_ms_occ_<i> == <k>)`，**绝不在 if 体内赋值**（否则被静态折叠）；
     3. **按槽捕获**：`if $zz_ms_occ_<i> == 1 … else … endif` 把本部件当帧 palette 复制进该槽的 palette 资源（`ResourceZZPalette_<draw_ib>_s<k> = copy vs-t0 unless_null`）；**SO 的捕获只由 SO owner（载体）部件做**（`ResourceZZRedirectSO_G<g>_s<k> = ref so0`）；
     4. **attach 全在顶层**：每个 (部件, 槽) 一个 CustomShader attach 段（`cs-t0` = 该槽 palette，`cs-u0` = 该槽骨架，`Dispatch = ceil(vg_count/64),1,1`），deform 段**顶层无条件** `run` 全部 槽×部件 的 attach；
     5. **骨架按槽分份**：每组两份 `ResourceZZMergedSkeleton_G<g>_s1 / _s2`（array 同现状全宽）；
     6. **每槽绘制**：段末 `if <门控>`，门控 = **（本槽 SO 别名当帧已由捕获者刷新）&&（期望到达集合）**（2026-09-17 补：前者必须有——别名只在捕获者本槽到达那一笔赋值，早于它落笔就是 `so0 = ref <上一帧别名/未赋值>`），体内**只允许** `vs-t0 = ResourceZZMergedSkeleton_G<g>_s<k>`、`so0 = ref ResourceZZRedirectSO_G<g>_s<k>`、`vb2 = <载体 blend>`、`vb0 = <载体 position>`、`draw = <merged_count>, 0`、`so0 = null`——**体内不得出现 `run`、不得给 `$变量` 赋值**。门控两种：**自足挂点**（直连路径且几何只采样自己的槽位）`if $zz_ms_occ_<i> == <k>` 直接绘制；**组级门控** `if <组内所有部件的 seen_<i><k> == 1 相与>`（重定向重放宿主、吸收挂点，以及 v9.1 的**合并宿主重放**——宿主捕获本轮 SO 到 `ResourceZZRedirectSO_G<g>_s<k>`，组内每个 Blend 布局兼容的挂点都发同一条 `so0 = ref … + 宿主 vb0/vb2 + draw` 重放，见第 8 条）；
     7. `[Constants]` **只声明 occ/seen/prev（+ 姿态指纹键 `$zz_ms_pose_key_<g>`）**；`[Present]` **先把 seen 抄进 prev，再清零 occ/seen**（不生成任何 drawn/ready 闩锁变量，也不写任何资源复位）。声明依据 `ui/universal/zzmi.py` 的 `add_merged_skeleton_sections`：`[Constants]` 段 `global $zz_ms_occ_<i> = 0` / `$zz_ms_seen_<i><k> = 0` / `$zz_ms_prev_<i><k> = 1`（+ 姿态对齐组的 `$zz_ms_pose_key_<g> = 0`）；`[Present]` 段先 `$zz_ms_prev_<i><k> = $zz_ms_seen_<i><k>` 再清零 occ/seen。
     8. 渲染段 / 载体 3 顶点前缀 stub / `handling = skip` 保持现状不变。
   - **已知限制**：
     - 两个 pass 的实例提交顺序相反时，出现次槽位会配错（与 v6 同源，槽位是位置相关的，无法在 INI 层完全消除）；
     - 某部件在某个槽被剔除时，守卫按**上一帧该槽是否到过**预测：上一帧也缺席 ⇒ 豁免、不阻塞守卫；只有「上一帧到过、本帧才缺席」的那一帧会被推迟（保持上一帧内容，方向安全），下一帧预测值即失效。见修复链 9；
     - `ResourceZZRedirectSO_G<g>_s<k>` 按槽共享（不按 target 区分）：若同一帧同一槽内有两个不同 target 的重放，后捕获者生效——当前导出每组恰有一个 target，不受影响。
   - **对照产物与回归**：手改版 `浮波柚叶.ini`（8571B，用户实测通过）作为 v9 语义基准固化进 `tests/test_zzmi_merged_skeleton_ini.py`——断言生成器输出具备：顶层 `run`（无 run 进 if）、sticky seen 顶层赋值、每槽守卫体内只有绑定与 draw、无闩锁变量、`[Constants]` 只声明 occ/seen/prev（+ 姿态指纹键）、`[Present]` 先把 seen 抄进 prev 再清零 occ/seen 且无任何资源复位（依据 `ui/universal/zzmi.py` 的 `add_merged_skeleton_sections`：`[Constants]` 的 `occ/seen/prev` 声明与 `[Present]` 的 `prev = seen` 抄录）。
8. **2026-09-13 v9.1 直连路径顺序无关化（FrameAnalysis 实证回归修复）**：
   - **症状（不合并）**：未 join 的多部件模组（各部件各自导出、同属一个骨架组）在游戏里**持续闪烁 / 整帧消失**；把物体全部 join 成一个对象后正常（用户口径："合并到一个没问题，不合并就闪"）。
   - **症状（合并，同一个根因的另一半）**：join 之后合并几何挂在宿主的导出 VB 上，宿主段仍用组级 seen 守卫画它 —— 宿主自己的 deform pass 每帧排在第几**不固定**，排在前面时守卫永不成立 ⇒ 合并几何整段不写（用户实测"合并之后还在闪"）。
   - **根因**：旧口径把「本槽骨架齐全」与「几何必须落盘」绑在**同一个组级 seen 守卫**上，而该守卫只可能在组内**最后一个**到达的部件那段成立。**顺序不固定已实测**：同一批 3 部件（`c2b4ce3a`/`e6afd8d1`/`403eace9`）在 `FrameAnalysis-2026-09-13-075255`、`075506` 里的 deform 顺序是 **B→C→A**，在 `080752`（用户标注"模型消失"的帧）里是 **A→B→C**：前者只有 A 被画出来，后者最后到达的是 3 顶点占位桩 `403eace9` ⇒ 整帧没有任何可见几何输出。三份 dump 里每个部件的 deform pass 都只出现 1 次，palette copy 各 1 次（6240/1296/1680 字节 = 130/27/35 骨骼），即"每帧一轮 + 谁最后到在变"。
   - **修复（已固化进生成器 `ui/universal/zzmi.py`）**：新增两个判据 + 一条重放通道：
     1. **自足挂点**（`_merged_direct_draw_is_self_contained` = 本部件几何只采样自己 vg_map 覆盖的槽位）：`if $zz_ms_occ_<i> == <k>` → `vs-t0 = ResourceZZMergedSkeleton_G<g>_s<k>` → `draw = <本部件导出顶点数>, 0`，**不引用任何其它部件的 seen**（自己的槽位已由本段顶层 attach 用当帧 palette 写全；组内共享 canonical 槽位按导出期去重口径 bitwise 相同，谁写都一样）；
     2. **合并宿主**（`_merged_group_absorbed_hosts` = 导出 VB 上挂着跨部件几何的 DrawIB）：几何引用全组骨骼 ⇒ **必须**等全组当帧到位；宿主在自己段顶层把本轮 SO 引用捕获到 `ResourceZZRedirectSO_G<g>_s<k>`（与重定向路径同名资源（`ResourceZZRedirectSO_G<g>_s<k>`）、同语义），守卫体内显式绑定 `so0 = ref ResourceZZRedirectSO_G<g>_s<k>` + 自己的 `vb2`/`vb0` + `draw = <宿主导出顶点数>, 0` + `so0 = null`；
     3. **兼容挂点重放**（`_merged_absorbed_replay_compatible`，Blend 输入布局必须一致 = BI4 与 BW16_BI16 不能混用）：**本组每个布局兼容的挂点**都发同一条宿主重放 —— 哪个挂点最后到达都能把合并几何写进宿主捕获的 SO，与提交顺序无关；布局不兼容则退回只在宿主自己段重放并**大声报警**（该组合仍依赖顺序，需要开发者介入）。
   - **实测支撑**：工作空间 `K:\SSMT-Package-master\WorkSpace\ZZMI\主角\LOD0\*` 里三个部件的类型目录均为 `...BW16_BI16_` ⇒ 用户这一例的重放布局兼容（生成器会按真实 `d3d11GameType` 的 Blend 元素签名判定，签名缺失时保守不重放）。
   - **回归**：`tests/test_zzmi_merged_skeleton_ini.py` 改写 5 个直连路径用例（契约：直连路径的任何 `if` 条件都不得出现 `$zz_ms_seen_`）+ 新增 1 个吸收挂点边界用例 + 新增 `ZZSIMergedHostDirectPathTests` 5 个用例（宿主捕获 SO / 宿主组级重放 / 兼容兄弟也重放 / 不兼容兄弟不重放 / 无兼容兄弟时报警）；全量 `pytest tests` 1758 passed / 19 skipped。

9. **2026-09-17 重放时机修复：豁免项改为「按槽的上一帧预测」（FrameAnalysis-022132 实证）**：
   - **症状（用户口径）**：①「身体莫名其妙的有一卡一卡的感觉」——单实例帧里主控物体的一部分慢一帧、且慢的部件集合随引擎提交顺序逐帧变化；②「只有场景中那个实例化的物体有问题，我主控物体没问题，头发像转了 90 度」。
   - **根因**：守卫豁免项用的是 `$zz_ms_any_<i>`（**本帧至今**是否出现过）。帧首所有尚未 deform 的部件 `any == 0` ⇒ 守卫条件**恒真** ⇒ 重放在**载体自己的** deform pass 就落笔，排在载体之后的部件用的是**上一帧** palette。
   - **dump 实证**（`FrameAnalysis-2026-09-17-022132`，叶瞬光01，单实例）：G0 deform 顺序 = `01ef4403#21` → **`999bff94#22`（载体）** → `9258d5f8#23` → `ae840e72#29` → `8c8de427#31` → `38b3bd13#32`（最后到达）。把载体 SO（hash `01d5a625`，行 3..13673）与各 pass 的 `vs-t0` 逐顶点重算蒙皮比对：**`#22` 骨架 13671/13671 行吻合（最大误差 2.2e-07）**，`#23/#29/#31/#32` 骨架最大误差 0.0226；G2 同样（SO `67a50546` 吻合 `#27` 载体 pass，与 `#33` 最后到达差 0.0857）。⇒ SO 内容确实是**载体 pass 时刻**的骨架写出来的。
   - **第二条同源根因**：`any == 0` 对「整帧只出现一次」的部件（本 dump 的 `c28e6303` 在旧导出里就是这种）永远不成立，但守卫仍要求它 `seen_<i><2> == 1` ⇒ **第 2 槽守卫永不闭合** ⇒ 第二实例的 SO 永不写 ⇒ 渲染读到未写/旧内容（实例物体的头发错位）。
   - **修复（已固化进生成器 `ui/universal/zzmi.py`）**：① 删除 `$zz_ms_any_<i>`，改为**按槽**的上一帧预测值 `$zz_ms_prev_<i><k>`（`[Present]` 先 `prev = seen` 再清零 occ/seen），守卫条件写成 `($zz_ms_prev_<i><k> == 0 || $zz_ms_seen_<i><k> == 1)`。语义：上一帧在该槽到过 ⇒ 本帧当作「会来」、守卫必须等它（帧首不再恒真，重放改在**最后一个必需部件到达处**闭合一次）；上一帧在该槽没到过 ⇒ 本帧当作「不会来」、豁免它（整帧被剔除的写入者不阻塞守卫；只出现一次的部件不再阻塞第 2 槽）。`[Constants]` 里 `prev` 初值取 **1**：首帧还没有上一帧可参考时按「所有必需部件都会到」保守等待（首帧也能在最后一个必需部件处闭合一次）。状态机推演（按 022132 的真实 G0 顺序 01ef4403→999bff94→9258d5f8→ae840e72→8c8de427→38b3bd13）：每帧第 1 槽**只在 38b3bd13（最后到达）闭合一次**，第 2 槽因别名就绪门不闭合（单实例帧）。
     ② 每槽守卫加 **SO 别名就绪门** `$zz_ms_seen_<捕获者><k> == 1`：`ResourceZZRedirectSO_G<g>_s<k>` 只在捕获者（重定向 = SO owner 载体，直连 = 合并宿主）本槽到达那一笔被赋值为本帧的 SO。没有这道门时，守卫在捕获者到达之前落笔会 `so0 = ref <上一帧别名 / 从未赋值>`——上一帧别名常常就是同一个缓冲且偏移已停在末尾（当帧正确写入被丢掉），从未赋值则是 3DMigoto 未定义行为。加门后守卫最早在 `max(捕获者本槽到达, 最后一个必需部件本槽到达)` 闭合，且**单实例帧第 2 槽永不闭合**（不再往从未赋值的 s2 别名落笔）。 组合函数 = `_merged_slot_guard_condition`（`_merged_slot_so_ready_condition` + `_merged_slot_seen_condition`）。
   - **回归**：`tests/test_zzmi_merged_skeleton_ini.py` 新增 `test_guard_uses_previous_frame_slot_prediction`，并断言 `[Constants]` 声明 `$zz_ms_prev_<i><k>`、`[Present]` 抄录语句严格早于 seen 清零、产物里不再出现 `$zz_ms_any_`。既有守卫文本断言全部升级为「别名就绪门 + 期望集合门」口径。
   - **残留已知限制（需要加载器侧信号，INI 层无法消除）**：两个实例的**部件子集不同**（一个实例被部分剔除）时，实例在「出现次」槽位上会错位（主控实例的部分部件落 s1、部分落 s2）——出现次口径的根本限制，彻底修复需要 3DMigoto 导入器提供**实例身份**；本期**已实测确认该信号在本装机上不可得**（见修复链 13）：`ZZZ\d3d11.dll` 里`draw_call_instance_id` / `CommandListIterateInstances` / `CommandListCallback_OnInstance` / `FRAME_NUMBER` / `DRAW_NUMBER` **全部 0 命中**（ASCII 与 UTF-16 都查过），只有 `match_instance_count` / `match_first_instance` / `instance_count` / `first_instance`，而 deform 阶段是**非实例化**的（全为 0/1）。

10. **2026-09-17 蒙皮 CS 发布（按索引写 SO）：窄布局必需部件也能落笔（FrameAnalysis-025058 实证）**：
   - **症状（用户口径）**：两个实例时「头发 999bff94 一直闪」；补一句「单个实例的时候，他也在那边闪」。
   - **根因**：draw 版重放（`so0 = ref <别名>` + 绑定载体 vb0/vb2 + `draw = <合并行数>, 0`）必须在 **Blend 输入布局与载体一致**的挂点执行（BI4 与 BW16_BI16 混用时 BLENDINDICES 被按错误格式解释 → 流输出全零），所以守卫只发在「兼容锚点」上。只要 `required_component_ids` 里有一个**布局更窄**的部件排在最后一个必需部件之后，就没有任何锚点能落笔 → 该槽 SO **整帧不写** → 渲染读到上一帧/陈旧内容 = 闪。单个实例同样会中（只要那个窄布局部件排最后）。
   - **dump 实证**（`FrameAnalysis-2026-09-17-025058`，叶瞬光01，两实例，ini 已含修复链 9）：G0 的 `required` = 全部 6 个部件，其中 **8c8de427 是 BW8_BI8（窄布局，不能重放 draw）**，它的两次到达是 `#83`/`#84`，而最后一个**锚点**是 `ae840e72 #82` → 两个实例的载体 SO（hash `01d5a625` 的两个缓冲，逐 draw dump 内容指纹互不相同）与「本帧完整 s1 骨架」重算蒙皮**只有 ~3100/13671 行吻合（最大误差 0.83）** ⇒ 本帧两个槽都没有落笔。8c8de427 是必需的：它独占槽位 `50..55`（实测 `W(8c8de427) ∩ R = {0, 50..55}`，而 `{50..55}` 不在任何锚点的写入集合里），是「头发顶点引用的那 6 根骨骼」的唯一写入者，不能从 required 里删。
   - **修复**：新增 **`Toolset/zzmi_merged_skin.hlsl`**（生成时复制到 Mod 的 `res/`）+ 每 (组, 槽, carrier) 一个 `[CustomShaderZZMISkin_G<g>_s<k>]`：完全**绕开 IA 输入布局**（顶点属性按 `SV_DispatchThreadID` 从 SRV 读：位置 0..2 / 法线 3..5 / 切线 6..9），把合并几何按**索引**写进本槽 SO（`cs-u0 = ref ResourceZZRedirectSO_G<g>_s<k>`，游戏 VLR 替换缓冲带 `uav_byte_stride = 4` ⇒ 4 字节元素 UAV）。守卫块形式：`if <与 draw 版同一套门控> → so0 = null → run = CustomShaderZZMISkin_G<g>_s<k> → endif`，发在**所有**必需部件段（含窄布局部件）——本 fork 支持 if 内跑 CustomShader（`Core/ZZMI/Libraries/HP bar/hp.ini` 在嵌套 if 内 `run = CustomShader.OrderIds`）。**按索引写 = 幂等**：帧内最后一次派发（最后一个必需部件到达处）即最终内容，天然不再依赖「谁最后到达、能不能承载 draw」。draw 版重放**保留为兜底**（UAV 绑定失败时行为与旧版一致）。
   - **参数**：`x1` = 该 carrier 的合并行数、`y1` = 目标起始行（= 前缀行 + 前面 carrier 行数之和）、`z1` = 前缀行数（仅首个 carrier）、`w1` = 每行 float 数（= `so_stride/4` = 10）。
   - **回归**：`tests/test_zzmi_merged_skeleton_ini.py` 新增 `test_skin_publish_emitted_for_every_required_component`（三个部件段都必须发 CS 发布块、且排在 draw 版重放之后）与 `test_skin_publish_shader_matches_generator_parameters`；`test_redirect_does_not_use_incompatible_stub_target_as_host` 改为断言「不兼容部件不发 draw 版重放、但**必须**发 CS 发布块」。
   - **待实机确认**：`cs-u0 = ref <游戏 SO 别名>` 能否创建 UAV 视图（`uav_byte_stride = 4` 是为此存在的开关）；若日志报 UAV 绑定失败，兜底路径仍是旧的 draw 版重放（不会比现在更差）。
   - **FR-2 如实化：B2/t40 守卫拦下 CS 时的回退**不是**无损的**（`ui/universal/zzmi.py`）：B2/t40 守卫（`_merged_skin_publish_supported`，判据 = 锚点 `cs-t0`/`cs-t1` 的**资源声明 stride + 元素构成**是否等于 `ZZMI_MERGED_SKIN_ROW_LAYOUT` 的 40B Position / 32B Blend）不匹配时**不发 CS**，发布改由 draw 版重放承担 —— 但 draw 版重放的挂点被 `compatible_component_ids`（= `required_component_ids` 里 Blend 布局签名 == `anchor_layout_key` 的那些部件，见 `_build_merged_mesh_redirect_plan`）过滤（`_merged_component_layout_compatible` / `_merged_component_can_host_replay`），而 CS 恰恰是为「Blend 布局与锚点不一致、不能重放 draw」的必需部件准备的（本条）。⇒ 守卫拦下 CS 后，**不在锚点布局内的必需部件本帧没有自己的发布点**；只有当**最后一个到达的必需部件恰在锚点布局内**时，才由它的挂点（`seen` 帧内 sticky）兜底写全该槽，否则该槽 SO 本帧不写 ⇒ 闪/缺失可能复现。诊断文案与机器字段按 `_merged_skin_replay_coverage` 的四态分档：`complete`（全部必需部件都在锚点布局内 ⇒ 覆盖完整）/ `gap`（有部件在外 ⇒ 如实写「本帧不发布、可能不写 ⇒ 闪烁/缺失可能复现」+ 修法指引）/ `legacy`（锚点签名与全部必需部件都不一致 = `compatible_component_ids` 空集回退，不声称完整回退）/ `unknown`（缺计划字段）；三落点（产物 ini 注释 / stdout / sink）字段为 `replay_coverage=` / `blocked_required=` / `publish_gap=`。
   - **FR-2 实测支撑（本机两个真实抓帧）**：`FrameAnalysis-2026-09-17-184431\deduped\` 下 `*-vb2-layout=*.txt` 一帧内并存 **5 种 32B Blend 签名**（`e805604d`/`820de055`/`82f472e3`/`acbe9603`/`2840ec5f`）+ **2 种 16B**（`73022f2f`/`d8224520`）+ **1 种 4B BI4**（`e4dfea81`）；`185933` 的布局集合与之一致，且该帧 `000035/000036/000041/000044` 四笔 deform draw 依次命中 32B/32B/4B/32B（`stride` 与 `layout=` 逐一对上）。⇒「必需部件落在锚点布局之外」在真实帧里可复现（混合布局是常态），同宽不同签名（元素顺序/格式）也同样真实存在 —— 这正是 `gap` 态要有如实文案、不能一律声称「已退回 draw 版重放」的原因。
   - **FR-2 未覆盖（点名）**：本机只有 184431 / 185933 两帧，**没有**锚点布局非 32B（即守卫真的被触发）的帧；上面第 10 条的 025058 实测结论是既有记录（该 dump 不在本机），本次未独立复算。因此「`gap` 态下必闪」仍是**条件性**结论（取决于帧内最后到达者），文案一律按「可能」表述，不做绝对断言。

11. **EFMI 的「实例确认」口径（2026-09-17 查阅，供后续实例对齐用）**：
   - EFMI 用的是 **加载器原生 Pool + 空间哈希**：`[Pool_ObjectSpatialIdentity]`（`pool_index_type = spatial` + `pool_spatial_radius` + `pool_expiration_*`）、`[PoolSpatialIdentity_SpatialIds]`、`CommandListSpatialIdentity_IdentifyComponentInstances`（`CommandListCallback_OnInstance` + `CommandListIterateInstances` 逐实例回调），核心一行是
     `$spatial_hash = ResourceOriginal_VS_CB->SpatialHash($instance_offset + 12, $instance_offset + 13, $instance_offset + 14, $cfg_spatial_cell_size)`
     ——即从**逐实例常量缓冲**里读世界坐标（偏移 12/13/14 个 value），算空间哈希当稳定实例 id，再用 `$PoolSpatialIdentity_SpatialIds[$draw_call_instance_id] = PoolInput_ObjectSpatialIdentity[$spatial_hash]->Index` 输出。参考实现：`K:\SSMT-Package-master\3Dmigoto\Zmd\Core\EFMI\SpatialIdentification.ini`（含 bitmask 状态机：`ComponentFlags` ≥ `$identification_min_components` 或超时 → custom/unknown）。
   - **ZZZ 装机可用性实测**：`ZZZ\d3d11.dll` 里含 `SpatialHash`、`pool_index_type`、`pool_spatial_radius`、`pool_expiration_timeout_frames`、`countbits`、`namespace`、`uav_byte_stride`（UTF-16 与 ASCII 均在），但**没有** `CommandListIterateInstances` / `CommandListCallback_OnInstance` / `FRAME_NUMBER` / `DRAW_NUMBER`（EFMI 的逐实例迭代与帧号原语是 Zmd 装机那一代才有的）。ZZZ 的 deform/render draw 是**非 instanced** 的（每个实例一个普通 draw），所以本来也不需要逐实例迭代，只需要「按当前 draw 定实例」。
   - **更简单也更可靠的实例键（本次发现）**：本 fork 支持**资源同一性比较**——`Core/ZZMI/Libraries/SlotFix/SlotFix.ini` 里就有 `if Resource\ZZMI\GlowGradient === Resource\ZZMI\EmptyGlowMap` 与 `=== null`。而 `so0`/`vb0` 槽名可参与比较（该文件里还有 `if vs == 909.709`）。但「资源统一性比较」**不是**可靠的实例键：`FrameAnalysis-2026-09-17-184431` 实测同一网格 `146c0784` 的三次 deform pass（draw `113`/`114`/`115`）**共用同一块 SO 目标**——全帧 `SOSetTargets` 只出现 32 次且**全部**在 draw `25..41`，最后一次绑定是 `000040 SOSetTargets(...)` → 资源行 `0: resource=0x000001D49E0BB4B8 hash=3a1f0236`；`113..115` 区间内**没有任何** `SOSetTargets`，即它们写的是驱动当前绑定的**同一个 SO 缓冲**（顺序覆盖，后写者生效）。（此前的 `so_ptr` 统计来自另一份 dump，**本次未能复现**；以本帧为准：「当前 deform 段共享同一个 SO 目标」是**常态**而不是例外。）同理 `vb0` 虽有逐实例指针（`{DUMP_A}` 的渲染 draw `112` = `146c0784`、`InstanceCount:2`），但渲染 draw 的逐实例 CB 来得太晚（骨架必须在 deform 之后、渲染之前建好）。⇒ 该正解在本期**不可实现**（见修复链 13）。这一步是修复链 8 遗留限制（两实例部件子集不同 → 槽位配错）的正解，**尚未实现**（需要实机验证 `===` 在 `so0` 上的行为，以及需要一个「另一实例被部分剔除」的 dump 做对照）。

12. **2026-09-17 姿态指纹对齐：出现次只是位置标签（「双实例动画混在一起」修复）**：
   - **症状（用户口径）**：「头发确实不闪了，但是双实例骨骼动画混乱了，两边混在了一起，没有按照实例分开」。
   - **先证伪了「发布没生效」**（FrameAnalysis-2026-09-17-033520，用户 03:15:50 重导出的 mod）：两个实例的载体 SO（hash `01d5a625` 的两个缓冲）分别与本槽骨架的蒙皮结果逐行吻合 **13671/13671（最大误差 2.2e-07 / 2.6e-07）**；两槽骨架在全部 **152 个非零槽上都不同**；两个实例确实是两个（渲染 draw 的 vs-cb1 世界矩阵不同、相距 ~0.7 单位）。该帧出现次恰好各自成块（A = #28..#48、B = #70..#81，都是完整一套部件）⇒ 这个 dump 没有复现混叠。
   - **根因**：「第几次出现」是**位置标签**。引擎按 mesh+instance 排序提交 deform，两个实例的相对先后**可以逐部件不同**——033520 G2 即证据：实例 A 的顺序是 `c209c22b→3b1b73fe→869976a3→4a178546`，B 却是 `3b1b73fe→c209c22b→869976a3→4a178546`（排序键本身随实例变化）。于是「第 1 次出现」对某些部件是 A、对另一些是 B ⇒ 同一槽骨架混进两份姿态 = 「两边混在一起」。此前看不出来是因为那些帧 SO 根本没写（在闪）；CS 发布修好后槽内容真的被渲染，混叠才显形。
   - **变形阶段没有别的实例信号**（实测）：同一部件多次 deform pass 的 cb **不能**当实例键——**但不是**因为「逐字节相同」。两份同场景 A/B dump（`FrameAnalysis-2026-09-17-184431` 含合并骨架 / `FrameAnalysis-2026-09-17-185933` 同场景对照）实测：同一网格 `146c0784` 在该帧的三次 deform pass（draw `113`/`114`/`115`，都是 `Draw(VertexCount:300, StartVertexLocation:0)`）里，`vs-cb1`（= `ps-cb0`，资源 hash `fa5a28d4`）**有 2 种内容**：`113` 与 `115` 的内容指纹同为 `ec27e638`（float 解码 `[0.69034, 0.69034, 0.69034, 1.0, 1.0, 1.0, …]`），`114` 为 `a122af1f`（`[1.0, 1.0, 1.0, 1.0, …]`）；且 `113` 与 `115` 绑的是**同一个** `ps-t0` 指针、`114` 是另一个。⇒ cb 确实**随 draw 变化**，但它变的是**材质/渲染参数**（含哨兵式的 `1.0`/`0.69034` 标量），**不是实例身份**；`vs-cb0..vs-cb5` 里也没有任何逐实例元数据（`VSSetConstantBuffers1` 的 `first_constant` 是逐 draw 的对象变换窗口，见修复链 6.7，与实例编号无关）。 ⇒ 实例身份只能从 palette（姿态）本身取；渲染阶段的逐实例 CB 来得太晚（骨架必须在 deform 阶段之后、渲染之前建好）。
   - **修复（已固化进生成器 `ui/universal/zzmi.py`）**：**全组共享 canonical 骨骼的姿态指纹**——叶瞬光01 G0 六个部件 `vg_map` 值交集 = `{0}`（实测），即槽位 0 是它们共同的根骨；取该骨骼本帧矩阵的平移分量当键（48 字节/骨骼 ⇒ `r0.w/r1.w/r2.w` = float 下标 `12*local + {3,7,11}`），`ResourceZZPoseKeySrc = ref vs-t0` + `->SpatialHash(f0, f4, f8, 0.01)`。**同实例同帧该矩阵逐位相同 ⇒ 键相同；不同实例姿态不同 ⇒ 键不同**。用 `[PoolZZMISlotOfKey_G<g>]`（`pool_index_type = spatial`，`pool_expiration_timeout_frames = 1` ⇒ 每帧重新分配）把「键 → 槽位」记下来，`[PoolZZMIG_Taken_G<g>]` 记槽位占用。
   - **实现形态 = 修正块**（安全基线不变）：原「按出现次捕获 palette / SO 引用」原样保留；随后若指纹算出的槽与该 pass 的出现次不一致，就把刚捕获的内容搬到正确槽（`ResourceZZPalette_<ib>_s<target> = copy ResourceZZPalette_<ib>_s<source>`；SO 引用 `= ref so0` 重新捕获）。指纹为 0（原语不可用 / 无共享骨骼）时整块跳过 ⇒ 行为与历史一致。这与 EFMI 的实例确认**同源原语**（`= ref` 槽别名 + `->SpatialHash` + `pool_index_type = spatial` + `$Pool[$key]` 读写；参考 `Zmd\Core\EFMI\SpatialIdentification.ini`），只是键取自 palette 而非逐实例 CB。
   - **诊断探针**：每个部件每次 deform 把指纹透到 `x2`（`ini param override = <值>`，帧分析日志可见），用于核对「同实例指纹是否相同、出现次是否翻转」。
   - **回归**：`tests/test_zzmi_merged_skeleton_ini.py` 新增 `test_pose_key_alignment_uses_shared_canonical_bone` 与 `test_pose_alignment_skipped_without_shared_bone`；全量 1943 passed。
   - **待实机确认**：`->SpatialHash` 作用在 `vs-t0`（结构化 palette）上的下标口径（按 EFMI 的 CB 用法 `+12/+13/+14` 推断为 **float** 下标）。若日志里指纹恒为 0 ⇒ 原语未生效，行为退回出现次口径（不会比修复前更差），届时按探针值改绑定形式。

13. **2026-09-17 实例身份可得性实测（FrameAnalysis-2026-09-17-184431 / 185933，同场景 A/B）**：
   - **结论先行**：**实例身份在本装机、本期不可得**；任何「实例键」都只能从**内容**里取，而内容型键有固有局限。
   - **加载器原语实测**（`ZZZ\d3d11.dll`，3,148,288 字节）：`draw_call_instance_id`、`CommandListIterateInstances`、`CommandListCallback_OnInstance`、`FRAME_NUMBER`、`DRAW_NUMBER` 在 ASCII 与 UTF-16 两种编码下**均 0 命中**；存在的只有 `match_instance_count` / `match_first_instance`（UTF-16 各 1 处）、`instance_count` / `first_instance`（元数据名）。⇒ EFMI 那套「逐实例回调 + 帧号」原语在 ZZZ 装机上不存在。
   - **deform 阶段非实例化**（`184431`）：deform 段是 `Draw(VertexCount:N, StartVertexLocation:0)`（无 Instance 字段），渲染段才是 `DrawIndexedInstanced(...InstanceCount:1..7...)`。⇒ deform 阶段**没有任何逐实例计数可用**。
   - **cb 不是实例键**（`184431`，同一网格 `146c0784` 的三次 deform draw `113`/`114`/`115`）：`vs-cb1`（资源 hash `fa5a28d4`）有 2 种内容——`113`/`115` = `ec27e638`（`[0.69034,0.69034,0.69034,1.0,…]`）、`114` = `a122af1f`（`[1.0,1.0,1.0,1.0,…]`）。即「同一部件的 cb 逐字节相同」**不成立**，但差异来自材质参数而不是实例身份（见修复链 12 修正）。
   - **hash 不是内容哈希**（同两份 dump）：同一个资源名 `edff77f4` 对应**三种不同内容**——`184431` 的 `000001-u0=edff77f4` / `000003-u0=edff77f4` / `000011-u0=edff77f4` 分别落到 `deduped\262e3a46.buf` / `2f7b534f.buf` / `6747cf39.buf`（三个文件各 2400 字节、内容指纹互不相同，首 8 个 float 分别是三个完全不同的世界坐标）。`d3d11.dll` 里有 `MarkResourceHashContaminated` / `UpdateResourceHashFromCPU`（各 1 处 ASCII）⇒ **`hash=` 是加载器的缓存值，CPU 重写后会过期**，**不得**当内容标识用；要内容必须看 `deduped/<内容hash>.buf`（资源名 → 内容是**多对多**）。
   - **姿态指纹键的固有局限（据内容型键的必然推论）**：键取自 palette 的当帧姿态 ⇒ **同帧同姿态的两个实例会撞同一个键**（例如 T-pose / 完全静止、或两实例动作恰好同步的那一帧）⇒ 退化为「挤进同一槽」。这不是实现缺陷，而是「只能从内容取键」的必然结果；只有加载器侧实例信号才能消除。
   - **对修复链 11 的处置**：其中「用 `===` 比较 `so0` 得到实例槽位」的正解路径**已被本帧证伪**（三次 deform pass 共用同一 SO 目标，见修复链 11 补注）⇒ 修复链 11 保留为「**原语调研记录**」，不得当作可用方案。

**分组实测（dump 122152，按渲染 cb1 对象变换，2026-08-24 分组定案口径）**：5 组——
| 组 | 部件 | 对象变换平移（row3） | 全局槽位范围 |
|---|---|---|---|
| G0 | 19086112 | (-15.537, 2.381, -5.661) | 0..6 |
| G1 头部 | 454ff522 / 48625d6d / 64d7d56f / b51bdd59 | (-15.212, 2.115, -5.556) | 7..29 |
| G2 头发 | 84618ee0 | (-15.209, 2.047, -5.561) | 30..78 |
| G3 身体 | a23aa8a3 / b20f90ea / b30db54e（共享 13/2/6 根） | (-15.223, 1.585, -5.513) | 79..248 |
| G4 | add6ff13 + d892c658（同空间） | (-15.459, 1.815, -5.629) | 249..265 |
全局合计 266 槽（组基址拼接组内槽位）；组内去重后唯一骨骼总计 244；运行时**每组每槽**一份全宽骨架（array=266，v9 起 `_s1`/`_s2` 两份），**只直拷本组骨骼**（跨组附加：F1 写进宿主组缓冲 + F2 用两组渲染 cb1 校准 `M' = C×M`）。cb1 提取口径（仅用于分组键）：dump 行逐 draw 反查 vs-cb1（绑定调用是持久状态、多数 draw 不重发，不可靠），自 2026-09-16 / 算法 v4 起改为按绑定窗口解析：dump 行先解析 `VSSetConstantBuffers1(first_constant=N)` 窗口，再把整块 dump 按窗口起点 offset = 4×N 取 16 floats（不再有 ≤512B 限制），窗口越界返回 None（绝不回退读 float 0）；该窗口起点随 `ObjectCB1FirstConstant` 写回子网格 json，无 dump 重建时同口径（依据 `common/zzmi_skeleton.py:90-93` 解析绑定行、`:116` 存窗口、`:334` 取窗口起点、`:487-532` 按窗口取 16 floats / 越界返回 None）。rows 0-3 即对象→世界矩阵（w 列 0/0/0/1 校验）。

## 8. 验收标准

1. 对测试工作空间 `希格莉德·空岛传奇` 执行导入：15 个子网格全部生成并写回 VGMap/VGOffset/VGCount；palette buf 落入各 `<子网格>/ModImpRuntime/`。
2. 去重结果与实测一致：5 个骨架组（身体/头部/头发/19086112/add6ff13+d892c658），全局骨骼编号按组基址拼接（0..6/7..29/30..78/79..248/249..265，Σ 266）；头部组内 `454ff522#0` ↔ `48625d6d#2`（质心距 0.034）合并为全局槽 7、`64d7d56f` 头顶件与 `b51bdd59#0` 后脑发饰骨被刚性门控拆开（槽 18/19）；身体组 b20f90ea 38 新 + 13 共享等（对照 §2.5）；**同部件内部零合并；48625d6d 的 `#1/#8/#9`（同骨骼异帧）保持独立不被误并；跨组部件的骨骼分占各组槽位，运行时各自组内直拷（2026-10-10 起：开关开启时改为写进宿主组缓冲，见头部 ⑤）**。
3. 幂等：二次导入不重复反查（跳过并提示）；`force=True` 可重建。
4. **统一顶点组**：复选框开导入后，所有子网格对象共用同一套全局顶点组（组名 = 全局骨骼 id）；**同组部件可互刷权重（组内统一骨架）；跨组别引用被禁止——导出时 `_warn_cross_group_bone_references` 大声报警**；导入对象按 `SkeletonGroup` 归入对应 `SkeletonGroup_<N>` 合集。
5. **导出合并骨架（v9 出现次槽位口径）**：复选框开 + 有 VGMap 时，导出 vb2 的 BLENDINDICES = 全局骨骼 id；INI 含 Merged Skeleton 全套——`[Constants]` **只声明** `$zz_ms_occ_<i>`、`$zz_ms_seen_<i><k>`、`$zz_ms_prev_<i><k>`（`prev` 初值 1），以及有姿态对齐的组的姿态指纹键 `$zz_ms_pose_key_<g>`（初值 0）（依据 `ui/universal/zzmi.py` 的 `add_merged_skeleton_sections`：`[Constants]` 循环里 `global $zz_ms_occ_<i> = 0` / `global $zz_ms_seen_<i><k> = 0` / `global $zz_ms_prev_<i><k> = 1`，以及姿态对齐组的 `global $zz_ms_pose_key_<g> = 0`）；每组每槽一份全宽 `ResourceZZMergedSkeleton_G<N>_s<k>`（array=266）；每 (部件, 槽) 一个直拷 attach CustomShader（`cs = ./res/zzmi_merged_skeleton_attach.hlsl`，无 cb1 引用）；**跨组附加（2026-10-10 F1+F2）**：跨组归属部件另有 `CustomShaderZZMIMergedSkeletonAttachX_C<i>_g<宿主组>_s<k>`（`cs = ./res/zzmi_merged_skeleton_attach_calibrated.hlsl`、`cs-t0/cs-t1` = 异组 palette/VgMap、`cs-u0` = **宿主组**缓冲、`cs-cb1` = 源组 cb1、`cs-cb2` = 宿主组 cb1），由各组部件的渲染 draw 处 `ResourceZZCb1_G<N> = copy vs-cb1 unless_null` 提供校准输入；deform 段**顶层无条件** `run` 全部 槽×部件 的 attach（**`run` 绝不在 if 内**）；到达标记 `$zz_ms_seen_<i><k>` 全部**顶层 sticky 累加**（if 体内赋值会被优化器静态折叠 → 守卫被删）；每槽绘制的门控：**自足挂点（直连路径、几何只采样自己槽位）= `if $zz_ms_occ_<i> == <k>` 直接绘制、不引用其它部件 seen**（v9.1），**重定向重放宿主 = （本槽 SO 别名当帧已捕获）&&（组内期望集合该槽已到达，谓词 `seen >= 1`，未到者按 `prev == 0` 豁免）**（依据 `ui/universal/zzmi.py` 的 `_merged_slot_so_ready_condition`（别名就绪门）、`_merged_slot_seen_condition`（期望集合谓词）、`_merged_seen_arrived_condition`（谓词单一来源，`== 1` → `>= 1`）、`_merged_slot_guard_condition`（两者组合）），**合并宿主（join 成一个物体）= 宿主段捕获本轮 SO 引用 + 组内每个 Blend 布局兼容的挂点各发一条组级守卫重放**（v9.1，顺序无关），**两种体内都只有资源绑定与 `draw`/`so0 = null`**（无 `run`、无 `$变量` 赋值）；`[Present]` **先把 seen 抄进 prev 再清零 occ/seen**（依据 `ui/universal/zzmi.py` 的 `add_merged_skeleton_sections` 里 `[Present]` 段；**另注**：合并物体身份归一依赖 `ZZMI_MergeSources` 账本，而它**全仓无写入方**（`git log --all -S "ZZMI_MergeSources"` 0 命中；当前只有 `ui/universal/zzmi.py` 的读取端`_zzmi_decode_merge_sources` 与测试里手工造的数据）⇒ **依赖仓库外 Blender 侧写入方，本期未闭合**；**不得含 `ResourceZZRedirectSO_* = null`**——F8 已回退，也不得含任何其它资源复位）。**INI 不得含任何 cb1 捕获段/捕获资源/校准着色器引用，不得在 [Present] 重放持久 palette attach，也不得出现 `$zz_ms_redirect_drawn_*` / `$zz_ms_group_ready_*` 闩锁变量或 `$zz_ms_group_phase_*` 相位变量**。
6. **游戏内实测**：导出的 mod 在 ZZMIv1 加载端下姿态正确，同组跨部件权重生效（每个 deform pass 的 vs-t0 使用当帧合并骨架；每槽守卫成立后重放该槽的合并可见几何，帧内无部件间不一致；**同一 IB 多实例（同帧多个 draw/多个对象变换）各自独立动画**：两个实例分别落在 s1/s2，不抖动、不卡首帧）；渲染 draw 无 R3 异常。
7. **多实例回归基线（v9 语义基准）**：生成的 ini 与手改版 `浮波柚叶.ini`（8571B，用户实测通过）语义一致——顶层 `run`（无 run 进 if）、`$zz_ms_seen_<i><k>` 顶层 sticky 累加、每槽守卫体内只有绑定与 draw、`[Constants]` 只声明 occ/seen/prev（+ 姿态指纹键）、`[Present]` 先把 seen 抄进 prev 再清零 occ/seen 且无任何资源复位、无闩锁/相位变量（依据 `ui/universal/zzmi.py` 的 `add_merged_skeleton_sections`：`[Constants]` 与 `[Present]` 两段）、骨架与 palette 按槽分份（`_s1`/`_s2`）、SO 捕获只由 SO owner 部件做；`tests/test_zzmi_merged_skeleton_ini.py` 全绿。
8. 复选框关闭：导入与导出行为均与现状完全一致（无 VGMap 读写、无 ModImpRuntime 写入、BLENDINDICES 走 `g.group` 原路径、无 Merged Skeleton 段）。
9. 单测全绿 + Blender headless e2e [PASS]。

10. **palette 新鲜度守卫（2026-10-20，丽娜共享槽 97「运动时飘」根因）**：每个 `CustomShaderZZMIMergedSkeletonAttach*` 段（同组 `Attach_` 与跨组 `AttachX_` 两类，均 `cs = ./res/zzmi_merged_skeleton_attach.hlsl`）必须带 `y2 = 1`（启用守卫）与 `z2 = $zz_ms_seen_<部件><槽>`（该 (部件, 槽) 的本帧到达计数，与 `_merged_seen_arrived_condition` 同一口径）；CS 在**任何资源读取之前**执行 `if (ZZ_ATTACH_GUARD > 0.5f && ZZ_ATTACH_SEEN < 0.5f) return;`（`IniParams[2].y` / `IniParams[2].z`）。原因：跨部件按骨骼矩阵 bitwise 去重会让**同一个 canonical 槽位被多个部件写**，而每个部件的 palette 只在**它自己的 deform 段**捕获；某个部件本帧没画（实测：只剩 3 个顶点的子网格被裁掉）时它的 palette 停在上一帧，于是每个 deform 段都跑的**无条件** `run` 会用上一帧矩阵盖掉本帧刚写好的共享槽位 ⇒ 共享骨稳定滞后一帧（实测：头/发 4046 个顶点全压在共享槽 97 上，运动时最明显）。守卫只做一件事：**绝不把当帧值降级成旧值**（计数为 0 时跳过写入与「写一份同样的旧值」在内容上等价，但当另有部件本帧写过该槽时，跳过保住了当帧那份）。因此 `run` 仍可保持顶层无条件执行——本规则**不松动**「`run` 绝不进 if」；`w1` 缺省 0 = 关闭守卫，未升级的段逐字节不变。第 1 组参数（`z1`/`w1`）归蒙皮 CS 的每行 float 数/前缀行数，attach 段**不得占用**（`tests/test_zzmi_merged_skeleton_ini.py::test_every_attach_section_carries_freshness_guard` 守着这条）。

---

## 9. 存档与交接（2026-10-11）

> 本节是「跨组骨骼合并」这一轮的存档：记录代码/产物现状、未解问题、全部取证手段与复现命令，供下次接手直接续跑。未解问题的现象与已排除项见头部 ⑭。

### 9.1 已落地（代码，单测全绿）

| 项 | 主要位置（`E:\代码\TheHerta4\`） | 说明 |
|---|---|---|
| 跨组 attach（F1） | `ui/universal/zzmi.py` 的 `_merged_cross_group_component_ids()` / `_merged_cross_attach_name()` | 被吸收的**别组**部件骨骼写进**宿主组**骨架缓冲（旧行为会让这些顶点读未初始化矩阵 ⇒ 部件消失） |
| 组内直拷 attach | `Toolset/zzmi_merged_skeleton_attach.hlsl` | 同组仍纯直拷（`_s1` / `_s2` 双槽） |
| 顺序无关守卫（F3） | `_append_merged_absorbed_replay` / `_append_merged_cross_group_replays` / `_merged_cross_host_group_map` | 守卫等「全部供骨件当帧到达」才闭合 |
| 蒙皮 CS 重发布（F3-CS） | `Toolset/zzmi_merged_skin.hlsl` + `_append_merged_skin_publish_block` | 绕开 IA 布局限制、幂等写 SO |
| **按来源拆分绘制（⑬，当前主线）** | `_merged_source_index_runs` / `_merged_split_group_plan` / `_merged_split_representative` / `_append_merged_render_split_draws` / `_merged_index_unique_str` / `_merged_split_mesh_comment` | 按顶点组把合并 IB 切段、每段**放回该来源自己的渲染段**画、统一继承合并物体的 `; [mesh:]` 标注 |
| **palette 新鲜度守卫（2026-10-20）** | `Toolset/zzmi_merged_skeleton_attach.hlsl` + `ui/universal/zzmi.py` 的两处 attach 发射点（同组 / 跨组） | attach 拒绝陈旧 palette（`y2`/`z2`）⇒ 共享 canonical 槽位不再被上一帧矩阵覆盖（丽娜槽 97 漂移根因） |
| 已否决并移除 | — | F4 / F5 / F7（在渲染段重算跨组校准并写回顶点流）全部实测否决、代码已删；校准 attach 改回直拷 |

测试（cwd `E:\代码\TheHerta4`，解释器 `C:\Python311\python.exe -m pytest`）：
- `tests/test_zzmi_merged_skeleton_ini.py` + `tests/test_zzmi_cross_group_switch.py` = **184 passed / 16 subtests** ✓
- `-k "zzmi or skeleton or merged or cross_group"` = **575 passed / 35 skipped / 39 subtests** ✓

### 9.2 现场产物状态

- 模组 `K:\SSMT-Package-master\3Dmigoto\ZZZ\Mods\SSMTGeneratedMod\洛克茜\洛克茜.ini` 当前 = **用户 00:23 导出件原样（26232 B）**：存档时已从 `Backups\洛克茜.ini.20261011-005116.skinout.bak` 还原并 `control.action reload` ⇒ 现场不含任何手工实验补丁。
- 同目录 `Backups\` 保存了本轮全部实验备份：`.worldvb.bak` / `.bindflags.bak` / `.filevb.bak` / `.sowrite.bak` / `.skinout.bak` / `.noskin.bak` / `.marker.bak`。
- 工作空间 `K:\SSMT-Package-master\WorkSpace\ZZMI\洛克茜`（4 部件 / 4 组：G0 `4e6d8882` 2 骨、G1 `3f797188` 1 骨、G2 `4e6e989e` 75 骨、G3 `d5e1e7ff` 133 骨）**未改动**；各部件目录下的 `ModImpRuntime\` 缓存**禁止删除**（合并骨骼反查缓存）。
- 分析用抽帧（均在 `K:\SSMT-Package-master\3Dmigoto\ZZZ\`）：`FrameAnalysis-2026-10-11-014416`（展示页）、`-014829`（大世界）、`-005325`（展示页，SkinOut 期）、`-064826` / `-055641`（大世界，链路逐行验证用）。

### 9.3 未解问题

**角色展示页**：进该场景后一个几何都看不见 + 画面不推进；大世界完全正常。已排除：SO 越界（`VertexLimitRaise` 在展示页生效，SO = 23007 行）、SkinOut 补丁（回退后依旧）、「我方 draw 未执行」（抽帧日志不记录 mod 发的 draw ⇒ 该测试无判别力）。待查方向见头部 ⑭d。

### 9.4 取证手段（复现命令与脚本）

MCP（独立 XXMI MCP DLL，v0.7.3；管道 `\\.\pipe\xxmi_mcp_output_v065_<pid>`，本机游戏 PID 16848 / 86992）：
```
C:\Python311\python.exe .scratch\mcp.py xxmi.data
C:\Python311\python.exe .scratch\mcp.py control.action action=toggle_hunting confirm=true
C:\Python311\python.exe .scratch\mcp.py dump.frame confirm=true allow_original_output=true wait=false
C:\Python311\python.exe .scratch\mcp.py control.action action=reload backend=native_only confirm=true wait=true
```
抓帧后**必须**把 hunting 关回去（再执行一次 `toggle_hunting`）；`addon.gpu.*` 只能看到游戏自己经 CreateBuffer 钩子创建的缓冲（3DMigoto 用 `copy` 建的资源不在其中）。

`.scratch\` 下的取证脚本（按用途）：
- `mcp.py` —— MCP 调用包装（自动定位管道、`--param k=v` 形式）
- `frame_draws.py` —— 抽帧 `log.txt` 逐 draw 列出 ib/vb/vs/ps 与资源字节数（符号链接经 `os.path.realpath` 解析到 `deduped\<内容hash>.buf`）
- `ib_report.py` —— 给定 IB 哈希，列出所有关联 draw 及其全部绑定
- `dump_cb1_window2.py` —— 状态跟踪式解析 `vs-cb1` 绑定窗口（逐 draw 落账）
- `verify_so_model.py` —— 用合并 Position + Blend + palette 重建 SO 并逐行比误差（证明链路正确性的主力脚本）
- `probe_blend_layout.py` / `blend_index_semantics.py` / `scan_merged_layout.py` / `check_merged_ib.py` —— Blend 行布局（float4 权重 + uint4 索引）、按骨索引分块、IB/VB 行数与索引范围自检
- `radius_profile.py` —— 半径谱刚体性判定（与行序无关，用于判「合并块是否只是行序不同」）
- `live_probe*.py` —— MCP 实时轮询（按尺寸/按 id 跟踪骨架、palette、cb1）
- `split_draw_plan.md` —— ⑬ 拆分绘制的改动清单与验证命令

历史资料：`.scratch\zzmi_cross_group_root_cause.md`（F1 根因报告）、`.scratch\calib_ref\`（历史校准版全文快照 + `CALIB_HISTORY.md` 兴废链）、`.scratch\zzmi_cross_group_F6_findings.md`（**其中「校准段输出错」的结论已作废**——参考 palette 选错）。

### 9.5 下一步选项（按优先级）

1. **展示页专项**：在展示页里用 MCP 实时读 `xxmi.data`（`reload_pending` / `analysis_active`）与 `addon.render.draws` / `addon.gpu.list`，确认 ① 该路径的 draw 由哪个资源哈希触发、我方段为何不匹配；② 画面不推进是渲染问题还是该场景的暂停语义。
2. **让拆分绘制在展示页也生效**：若展示页走的是另一套 IB/VB 哈希，按那套哈希补发 override 段（生成器侧按 DrawIB 集合扩展）。
3. **主线回归**：用户重导后在大世界验证 ⑬（各回各家）是否已消除影子/镜像物体与运动错位——这是 ⑬ 设计要解决的原始症状，与展示页问题相互独立。
