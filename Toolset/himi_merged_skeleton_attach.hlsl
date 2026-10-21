// ResourceHI3MergedSkeleton_REFERENCE —— HIMI（崩坏3 / HI3）合并骨架 attach CS（冻结的规范副本）
// 由 common/himi_merged_runtime.py::render_attach_hlsl() 生成；请勿手改。
//
// 【这份文件是什么】它是 HIMI 的**逐帧 attach CS**：导出侧为每个 deform 家族复制一份到
// <mod>/res/himi_attach_<fam>.hlsl，只把基址行改写成该族在合并骨架里的全局起始槽
// （#define HIMI_ATTACH_BASE <base>），CS 正文逐字不变。每个
// [CustomShaderHI3MergedSkeletonAttach_<fam>] 段引用自己那一份：
//   cs = ./res/himi_attach_<fam>.hlsl / cs-t0 = 该族当帧副本 / cs-u0 = 合并骨架
//   Dispatch = ceil(该族槽数 / 64)（与下方 numthreads(64, 1, 1) 匹配），段末 cs-u0 = null 解绑。
//
// 【机制链（逐帧，顺序不可换）】
//   ① deform VB 段（[TextureOverride_VB_<fam>_<fam>_Blend]）先捕获**当帧** palette：
//        ResourceHI3PaletteFam_<fam> = copy vs-t0 unless_null      ← LHS 是**目标**
//   ② run = CustomShaderHI3MergedSkeletonAttach_<fam> → 本 CS 把该副本块搬运进合并骨架的
//        [HIMI_ATTACH_BASE, HIMI_ATTACH_BASE + 该族槽数) 区间；
//   ③ 之后才把 vs-t0 换绑成合并骨架（供游戏 deform VS 读取），最后 handling = skip + draw。
//   run 必须排在 `vs-t0 = <合并骨架>` **之前**：反序 = 自反馈（把刚绑上去的骨架当成当帧
//   palette 捕获）；copy 的方向写反 = 把空副本写进游戏的当帧 palette。两处都由导出侧的
//   顺序护栏（ui/universal/himi.py::blend_block_order_problems）在产物落盘前拦死。
//
// 【为什么必须逐帧写】palette 是**每帧姿态数据**：实测同一 vs-t0 资源哈希跨 6 次抓帧给出
// 6 种不同内容，而同一批 dump 里静态资源（deform 的 Position vb0）逐字节不变
// （reports/himi-merge/27-static-vs-perframe.md；独立 oracle 的 CF-1 组把这条前提钉住）。
// 用 filename 加载的静态骨架 = 拿抓帧那一帧的姿态永久蒙皮 ⇒ 合并骨架在 ini 段里**不带
// filename**（那份 Meshes/<role_key>-MergedSkeleton.buf 仍会落盘，但只作离线证据/调试产物）。
//
// 【跨显卡不变量（三条必须同时满足，2026-09-22 ZZMI 实机验证）】
//  A1 本 palette 必须声明为 Buffer<float4>（typed 16 B 元素），**不是** StructuredBuffer<48B>：
//     palette 是 copy vs-t0 的目标，加载器会把源视图的 Format 注入进来（SRV 实际是 typed
//     16 B）。按 48 B 结构化声明会落进 D3D11 未定义区：AMD 按描述符取址 → 骨架 2/3 错位 →
//     模型爆炸。所以这里手工按 bone*3 取三个 float4，不依赖任何 stride 解释。
//     ini 侧该副本必须是 type = Buffer / stride = 48 / **绝不允许** format。
//  A2 合并骨架必须声明为 RWBuffer<float4>（typed 16 B），按 slot*3 写。按结构化（SBS=48）
//     声明会让 AMD 按 48 取址 → 骨号 b 实际读到槽 3b → 越界读返回精确 0。
//     ini 侧：type = RWBuffer + format = R32G32B32A32_FLOAT + array = 3 × 槽位数
//     + bind_flags = shader_resource unordered_access（缺 bind_flags ⇒ SRV 创建静默失败）。
//  A3 槽数一律取**资源自身维度**（GetDimensions 单参后 /3u 还原），槽位下标一律 *3u，
//     **绝不读 IniParams**：ini 参数纹理布局随 3DMigoto fork 变化（标准版 x1/y1 在
//     IniParams[0]，Armor fork 从 [1] 起），读错位置只会读到未设置的 0 ⇒ CS 静默不写 ⇒
//     骨架留空 ⇒ 顶点位置全零，且**不报任何错**。唯一逻辑参数是目标基址，由导出侧写进
//     本文件的 #define（common/himi_merged_runtime.py::render_family_attach_base_line）。
//
// 【本环境未取证的项（不得当成已成立）】
//  - V1 骨架新鲜度：**旧静态前提已被 t51 实机证伪** —— 游戏 palette 是每帧内容（同一 `vs-t0` 资源哈希在 6 份 dump 里给出 6 种内容，而场景静态资源逐字节不变）⇒ 合并骨架**不能**是一份静态文件。本方案改为「deform 段捕获当帧 palette（`ResourceHI3PaletteFam_<fam> = copy vs-t0 unless_null`）→ attach CS 写进骨架区间」，逐帧新鲜度由该链保证。实机仍需确认：换姿势后不出现『跟随上一帧骨骼』（若出现，先查捕获/run 是否排在 `vs-t0 = <骨架>` 之前、再查 Dispatch/槽数）。
//  - V2 deform VS 是否按 typed `palette[3b+m]` 读骨架：HI3 的 d3dx.ini cache_shaders=0、ShaderFixes/ 为空、无 DXBC，无法静态确认。本模块只承诺『骨架声明与 palette 逐字节同构』（A1/A2）；读取侧形态待开 shader dump 后核对 dcl_resource 与索引表达式。
//  - V3 mod Blend 的行序是否等于游戏 SO 行序：导出侧顶点顺序是 loop 首次出现序（计划书 §7.5 O3），需真实工作空间逐顶点比对。
//  - R3 vb1 覆盖 + vs-t0 重绑在**非索引 Draw** 上的实际行为未经实机验证（本环境无实机条件）。
//
// ---- 机器可读声明行（测试据它与 ini 逐字段比对）----
// HIMI-RUNTIME-DECL name=ResourceHI3MergedSkeleton_REFERENCE kind=merged_skeleton type=RWBuffer format=R32G32B32A32_FLOAT stride=none array=1029 bind_flags="shader_resource unordered_access" filename=none slots=343 bytes=16464
//
// ---- 资源声明（必须与 ini 的 [ResourceHI3MergedSkeleton_REFERENCE] 段逐字段一致）----
#define HIMI_MERGED_SKELETON_SLOTS 343u
#define HIMI_MERGED_SKELETON_STRIDE 48u
// 本族在合并骨架里的全局基址（槽）。导出侧逐族改写这一行（render_family_attach_base_line）；
// 本 REFERENCE 副本用 0。基址不走 ini 参数 —— 见 A3。
#define HIMI_ATTACH_BASE 0

// cs-t0 = ref ResourceHI3PaletteFam_<fam>（ini: Buffer / stride = 48 / 无 format）
Buffer<float4> himi_src_palette : register(t0);
// cs-u0 = ref ResourceHI3MergedSkeleton_<role_key>
// （ini: RWBuffer + format = R32G32B32A32_FLOAT + array = 3 × 槽位数 + bind_flags）
RWBuffer<float4> ResourceHI3MergedSkeleton_REFERENCE : register(u0);

[numthreads(64, 1, 1)]
void main(uint3 dispatch_id : SV_DispatchThreadID)
{
    uint bone_index = dispatch_id.x;

    // 槽数取**资源自身维度**：typed Buffer<float4> 的 GetDimensions 是**单参**形式，
    // 返回 float4 元素数 = 槽数 × 3；必须 /3u 还原成槽数（与 ZZMI 读取侧同一口径）。
    uint palette_elements = 0u;
    himi_src_palette.GetDimensions(palette_elements);
    uint count = palette_elements / 3u;
    if (bone_index >= count)
    {
        return;
    }

    uint skeleton_elements = 0u;
    ResourceHI3MergedSkeleton_REFERENCE.GetDimensions(skeleton_elements);

    uint src = bone_index * 3u;                        // 读侧索引：bone * 3u
    uint dst = (HIMI_ATTACH_BASE + bone_index) * 3u;   // 写侧索引：slot * 3u（slot = 基址 + 本族骨号）
    if (dst + 2u >= skeleton_elements)
    {
        return;   // 越界保护：宁可少写，也不写坏别人的槽
    }

    // 每根骨占 3 个 float4（4×3 行主序矩阵），两端都显式按 *3 取址 ——
    // 与两侧的 typed 16 B 视图精确匹配。
    ResourceHI3MergedSkeleton_REFERENCE[dst + 0u] = himi_src_palette[src + 0u];
    ResourceHI3MergedSkeleton_REFERENCE[dst + 1u] = himi_src_palette[src + 1u];
    ResourceHI3MergedSkeleton_REFERENCE[dst + 2u] = himi_src_palette[src + 2u];
}
