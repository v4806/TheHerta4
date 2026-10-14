// ZZMI 骨骼合并 - 合并骨架 attach CS（**跨组校准版**，绝区零 deform pass 专用，TheHerta4 生成）
//
// 与 zzmi_merged_skeleton_attach.hlsl 的关系：
//   那是「直拷版」——同组（同一对象空间）部件把 palette 骨骼原样写进本组骨架缓冲。
//   本文件是「跨组校准版」——被吸收进**宿主组**网格的**别组**部件，其 palette 骨骼
//   输出的是**源对象空间**的顶点；宿主网格渲染时用的是**宿主对象**的 cb1，因此必须
//   先把骨骼从源对象空间换算进宿主对象空间，再写进宿主组骨架缓冲。
//
// 数据布局（FrameAnalysis 实测，详见 ZZMI骨骼合并计划书.md 与
// .scratch/zzmi_cross_group_root_cause.md）：
// - palette：48 字节/骨骼（3 个 float4 = 矩阵 3 行，平移在各行 w），
//   列向量约定 object = Rm * bind + tm；CPU 在该部件 deform pass 前 Map 上传。
// - 渲染 cb1（逐物体常量块）：前 4 个 float4 = 对象→世界矩阵（rows 0-2 旋转行 w=0、
//   row 3 平移 w=1），行向量约定 world = object * R + t（即列向量 U = (R^T, t)）。
//   palette 与渲染 cb1 逐物体 1:1 配对：palette 输出对象空间顶点，渲染 VS 用 cb1 摆到世界。
//
// 校准公式（保证各自的对象→世界变换在世界里对齐）：
//   源骨骼 M（bind→源对象空间）给宿主组用时必须换成 bind→宿主对象空间：
//     M' = C × M，C = U_dst^-1 × U_src（宿主对象空间 ← 源对象空间换算器）
//   展开（刚体：旋转行单位正交，R^-1 = R^T；ZZZ 对象变换实测无缩放）：
//     C_rot = Rd × Rs^T；C_t = Rd × (t_src - t_dst)
//     Rm' = C_rot × Rm；tm' = C_rot × tm + C_t
//   世界不变性：U_dst × (M' × p) ≡ U_src × (M × p)。
//
// 调用方语义（跨组 attach 段 `CustomShaderZZMIMergedSkeletonAttachX_C<i>_g<宿主组>_s<槽>`）：
// - cs-t0 = ResourceZZPalette_<跨组件 DrawIB>_s<槽>（**源**部件当帧 palette 副本）；
// - cs-t1 = ResourceZZVgMap_<跨组件 DrawIB>（局部骨 id → 全局槽位）；
// - cs-cb1 = ResourceZZCb1_G<源组>（**源**对象→世界 cb1 捕获）；
// - cs-cb2 = ResourceZZCb1_G<宿主组>（**宿主**对象→世界 cb1 捕获）；
// - cs-u0  = ResourceZZMergedSkeleton_G<宿主组>_s<槽>（写入目标 = 宿主组缓冲）；
// - y1 = 跨组件骨骼数（本部件的 vg_count）。
//
// 时序（**关键**：与历史上失败的那一版不同，attach 仍留在每个 deform pass 内即时执行）：
// palette 是**当帧**的（deform draw 处 `copy vs-t0`），cb1 捕获来自**该组代表部件自己的
// 渲染 draw**（`ResourceZZCb1_G<N> = copy vs-cb1 unless_null`，last-wins）——渲染块在
// deform 块之后，所以 cb1 至多是**上一帧**的值。对本开关的目标场景（同一角色的不同
// 对象空间）C = U_dst^-1 × U_src 是**常量**（两组对象的相对变换由模型层级固定），
// 因此「当帧骨骼 × 上一帧 C」在全帧率下精确；只有跨实体独立运动（把武器并进身体）
// 才可能残留 ≤1 帧的相对运动误差。
// 历史失败原因（2026-08-25 废弃的校准版）不是本公式，而是① cb1 捕获段按 IB hash 匹配
// → 抓到别的 pass 的常量块（同一 IB 在多个 pass 下 cb1 内容不同，实测差 8.2°）；
// ② attach 挪到 [Present] → 骨架整体比几何慢一帧（运动时逐帧错位）。本版两处都已改。
//
// 兜底策略（宁可退化不写坏）：cb1 形态无效（未捕获/首帧未绑定读到全 0）时退化为直拷
// ——被吸收部件会按宿主对象空间摆放（偏一个常量 C），但**不会**消失、不会 NaN。
//
// 【注意】与直拷版相同的三条资源声明约束（改 ini 时务必保持）：
//  1) palette 资源绝不可加 format（会把 StructureByteStride 变成 16 → copy 溢出）；
//  2) merged_skeleton 必须 typed RWBuffer<float4> + `bind_flags = shader_resource unordered_access`，
//     且 SRV 元素尺寸 16B（游戏 deform VS 按 palette[3b+m] 取址）；
//  3) 本 fork 的 IniParams 从 [1] 起（y1 = IniParams[1].y）。

struct ZZBone3x4
{
    float4 r0;
    float4 r1;
    float4 r2;
};

Buffer<float4> src_palette : register(t0);
Buffer<uint4> vg_map : register(t1);
RWBuffer<float4> merged_skeleton : register(u0);

// 源组 / 宿主组的对象→世界 cb1 捕获（各代表部件的渲染 draw 处 copy 的常量块，
// 前 4 个 float4 有效：rows 0-2 旋转行、row 3 平移）。
// 未捕获时 D3D11 读到全 0 → zz_cb1_valid 判假 → 直拷兜底。
cbuffer ZZCb1Src : register(b1) { float4 zz_cb1_src[4]; }
cbuffer ZZCb1Dst : register(b2) { float4 zz_cb1_dst[4]; }

Texture1D<float4> IniParams : register(t120);
#define ZZ_ATTACH_COUNT IniParams[1].y

// cb1 块形态校验：rows 0-2 w=0、row 3 w=1、旋转行近似单位、平移量级合理。
// 不满足（未捕获/首帧/垃圾小块/别的 pass 的常量块）视为无效 → 直拷兜底。
bool zz_cb1_valid(float4 r0, float4 r1, float4 r2, float4 r3)
{
    if (abs(r3.w - 1.0) > 1e-3)
    {
        return false;
    }
    if (abs(r0.w) > 1e-3 || abs(r1.w) > 1e-3 || abs(r2.w) > 1e-3)
    {
        return false;
    }
    float l0 = length(r0.xyz);
    float l1 = length(r1.xyz);
    float l2 = length(r2.xyz);
    if (l0 < 0.5 || l0 > 2.0 || l1 < 0.5 || l1 > 2.0 || l2 < 0.5 || l2 > 2.0)
    {
        return false;
    }
    // ZZZ 世界坐标量级可达数百~数千单位（实测对象原点 |t| ≈ 600），
    // 只挡明显非变换的垃圾（NaN/天文数字）。
    if (!(length(r3.xyz) < 100000.0))
    {
        return false;
    }
    return true;
}

[numthreads(64, 1, 1)]
void main(uint3 dispatch_id : SV_DispatchThreadID)
{
    uint bone_index = dispatch_id.x;
    uint count = (uint)ZZ_ATTACH_COUNT;
    if (bone_index >= count)
    {
        return;
    }

    // 与直拷版相同的运行时保护：y1、palette、vg_map 必须描述同一批局部骨骼。
    uint palette_count = 0;
    uint vg_map_count = 0;
    src_palette.GetDimensions(palette_count);
    palette_count /= 3u;
    vg_map.GetDimensions(vg_map_count);
    if (bone_index >= palette_count || bone_index >= vg_map_count)
    {
        return;
    }

    uint slot = vg_map[bone_index].x;
    uint merged_count = 0;
    merged_skeleton.GetDimensions(merged_count);
    merged_count /= 3u;
    if (slot >= merged_count)
    {
        return;
    }

    // 每根骨占 3 个 float4（4x3 矩阵），显式按 bone*3 取。
    uint base = bone_index * 3u;
    float4 p0 = src_palette[base + 0u];
    float4 p1 = src_palette[base + 1u];
    float4 p2 = src_palette[base + 2u];

    // 行向量旋转行（cb1 rows 0-2 的 xyz）；平移 = row 3 的 xyz
    bool src_ok = zz_cb1_valid(zz_cb1_src[0], zz_cb1_src[1], zz_cb1_src[2], zz_cb1_src[3]);
    bool dst_ok = zz_cb1_valid(zz_cb1_dst[0], zz_cb1_dst[1], zz_cb1_dst[2], zz_cb1_dst[3]);

    float4 out0 = p0;
    float4 out1 = p1;
    float4 out2 = p2;
    if (src_ok && dst_ok)
    {
        float3x3 Rs = float3x3(zz_cb1_src[0].xyz, zz_cb1_src[1].xyz, zz_cb1_src[2].xyz);
        float3x3 Rd = float3x3(zz_cb1_dst[0].xyz, zz_cb1_dst[1].xyz, zz_cb1_dst[2].xyz);
        float3 t_src = zz_cb1_src[3].xyz;
        float3 t_dst = zz_cb1_dst[3].xyz;

        // C = U_dst^-1 × U_src（刚体逆 R^-1 = R^T）
        float3x3 c_rot = mul(Rd, transpose(Rs));
        float3 c_t = mul(Rd, t_src - t_dst);

        // palette 骨骼：r_i = (Rm 行 i, tm 分量 i)
        float3x3 rm = float3x3(p0.xyz, p1.xyz, p2.xyz);
        float3 tm = float3(p0.w, p1.w, p2.w);

        // M' = C × M
        float3x3 rm_out = mul(c_rot, rm);
        float3 tm_out = mul(c_rot, tm) + c_t;

        out0 = float4(rm_out[0], tm_out.x);
        out1 = float4(rm_out[1], tm_out.y);
        out2 = float4(rm_out[2], tm_out.z);
    }

    // 每槽占 3 个 float4，显式按 slot*3 写入 —— 与 typed 视图精确对齐
    uint dst = slot * 3u;
    merged_skeleton[dst + 0u] = out0;
    merged_skeleton[dst + 1u] = out1;
    merged_skeleton[dst + 2u] = out2;
}
