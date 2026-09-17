// ZZMI 骨骼合并 - 合并几何蒙皮 CS（按索引写本实例 SO，TheHerta4 生成）
//
// 为什么需要它（2026-09-17 实机 + FrameAnalysis-025058 实证）：
// 重定向路径过去用「draw + 流输出」把合并几何写进本实例 SO，而 draw 必须在
// **Blend 输入布局与载体一致**的挂点执行（BI4 与 BW16_BI16 混用时 BLENDINDICES
// 被按错误格式解释 → 流输出全零）。于是守卫只能挂在「兼容锚点」上；只要
// required 集合里有一个**窄布局**部件（本例 8c8de427 = BW8_BI8，它是被引用的
// 共享/独占槽位写入者）排在最后一个必需部件之后，就没有任何锚点能落笔 →
// 该槽 SO 整帧不写 → 用户实测「一个实例/两个实例都在闪」。
// 本 dump 实证：025058 里 G0 required 的最后到达是 8c8de427（deform #83/#84），
// 最后一个锚点是 ae840e72（#82）→ 两个实例的 SO（hash 01d5a625 的两个缓冲）
// 与「本帧完整 s1 骨架」重算蒙皮只有 ~3100/13671 行吻合（最大误差 0.83），
// 即本帧根本没有落笔。
//
// 本 CS 完全绕开 IA 输入布局：顶点属性全部按 SV_DispatchThreadID 从 SRV 读，
// 因此**任意必需部件的 deform 段都能发布**；且按索引写 = **幂等**（重复派发无害，
// 帧内最后一次派发用最完整的骨架覆盖，天然实现「最后一个必需部件到达时落笔」）。
// 位置/法线/切线全写，与 draw 版重放（游戏 deform VS）等价：
//   位置 = Σ w·(R·p + t)；法线/切线 = normalize(Σ w·R·n)，切线 w 分量原样保留。
// 骨骼矩阵是刚体（正交），所以法线用 R 本身即等价于逆转置。
//
// 参数（3DMigoto ini 参数纹理；本 fork 的 x1/y1/z1/w1 落在 IniParams[1]，
// 见 zzmi_merged_skeleton_attach.hlsl 的实测注释）：
//   x1 = 本 CS 负责的合并几何行数（该 carrier 的导出顶点数）
//   y1 = 目标起始行（= 前缀行数 + 前面各 carrier 行数之和）
//   z1 = 前缀行数（仅第一个 carrier 传 3，其余传 0）
//   w1 = 每行 float 数（= Position 类目 stride / 4 = 10）
//
// 绑定：cs-t0 = 该 carrier 的导出 Position（stride 40 = 位置/法线/切线），
//       cs-t1 = 该 carrier 的导出 Blend（stride 32 = 4 权重 + 4 索引），
//       cs-t2 = 本槽合并骨架（48 字节/骨骼的 3x4 矩阵），
//       cs-u0 = 本槽 SO 别名（游戏 VLR 替换缓冲，uav_byte_stride = 4 → 4 字节元素）。

struct ZZBone3x4
{
    float4 r0;
    float4 r1;
    float4 r2;
};

// 40 字节/行：位置 = 0..2，法线 = 3..5，切线 = 6..9（导出 Position 类目实测布局）
struct ZZVertex40
{
    float4 a; // 0..3
    float4 b; // 4..7
    float2 c; // 8..9
};

struct ZZBlend32
{
    float4 w; // 4 个权重
    uint4 i;  // 4 个全局槽位
};

StructuredBuffer<ZZVertex40> src_rows : register(t0);
StructuredBuffer<ZZBlend32> src_blend : register(t1);
StructuredBuffer<ZZBone3x4> merged_skeleton : register(t2);
// 目标 SO：uav_byte_stride = 4 ⇒ 4 字节元素的结构化 UAV
RWStructuredBuffer<float> dst_rows : register(u0);

Texture1D<float4> IniParams : register(t120);
#define ZZ_SKIN_COUNT IniParams[1].x
#define ZZ_SKIN_DEST_START IniParams[1].y
#define ZZ_SKIN_PREFIX IniParams[1].z
#define ZZ_SKIN_FLOATS IniParams[1].w

// 单个顶点 LBS：位置带平移，法线/切线只取 3x3（刚体正交 ⇒ 等价逆转置）
void zz_skin_vertex(
    ZZVertex40 v,
    ZZBlend32 blend,
    uint bone_count,
    out float3 skinned_p,
    out float3 skinned_n,
    out float3 skinned_t,
    out float tan_w)
{
    float3 p = v.a.xyz;
    float3 n = float3(v.a.w, v.b.x, v.b.y);
    float4 t = float4(v.b.z, v.b.w, v.c.x, v.c.y);

    skinned_p = float3(0, 0, 0);
    skinned_n = float3(0, 0, 0);
    skinned_t = float3(0, 0, 0);
    tan_w = t.w;

    [unroll]
    for (uint k = 0; k < 4; ++k)
    {
        float w = blend.w[k];
        uint bone = blend.i[k];
        if (w == 0.0f || bone >= bone_count)
        {
            continue;
        }
        ZZBone3x4 m = merged_skeleton[bone];
        skinned_p += w * (float3(dot(m.r0.xyz, p) + m.r0.w,
                                 dot(m.r1.xyz, p) + m.r1.w,
                                 dot(m.r2.xyz, p) + m.r2.w));
        skinned_n += w * float3(dot(m.r0.xyz, n), dot(m.r1.xyz, n), dot(m.r2.xyz, n));
        skinned_t += w * float3(dot(m.r0.xyz, t.xyz), dot(m.r1.xyz, t.xyz), dot(m.r2.xyz, t.xyz));
    }

    float n_len = length(skinned_n);
    float t_len = length(skinned_t);
    if (n_len > 1e-8f)
    {
        skinned_n /= n_len;
    }
    if (t_len > 1e-8f)
    {
        skinned_t /= t_len;
    }
}

void zz_write_row(uint row, float3 pos, float3 nrm, float3 tan, float tan_w, uint stride_f)
{
    uint base = row * stride_f;
    dst_rows[base + 0] = pos.x;
    dst_rows[base + 1] = pos.y;
    dst_rows[base + 2] = pos.z;
    dst_rows[base + 3] = nrm.x;
    dst_rows[base + 4] = nrm.y;
    dst_rows[base + 5] = nrm.z;
    dst_rows[base + 6] = tan.x;
    dst_rows[base + 7] = tan.y;
    dst_rows[base + 8] = tan.z;
    dst_rows[base + 9] = tan_w;
}

[numthreads(64, 1, 1)]
void main(uint3 dispatch_id : SV_DispatchThreadID)
{
    uint index = dispatch_id.x;
    uint count = (uint)ZZ_SKIN_COUNT;
    uint prefix = (uint)ZZ_SKIN_PREFIX;
    if (index >= max(count, prefix))
    {
        return;
    }

    uint stride_f = (uint)ZZ_SKIN_FLOATS;
    if (stride_f < 10)
    {
        stride_f = 10;
    }

    // 运行时按真实资源长度做一次保护（损坏/陈旧 mod 资源不越界读）。
    uint src_count = 0;
    uint src_stride = 0;
    src_rows.GetDimensions(src_count, src_stride);
    uint bone_count = 0;
    uint bone_stride = 0;
    merged_skeleton.GetDimensions(bone_count, bone_stride);

    // 1) 本 carrier 负责的合并几何行 → 目标 [dest_start + i]
    if (index < count)
    {
        float3 sp;
        float3 sn;
        float3 st;
        float tw;
        zz_skin_vertex(src_rows[index], src_blend[index], bone_count, sp, sn, st, tw);
        zz_write_row((uint)ZZ_SKIN_DEST_START + index, sp, sn, st, tw, stride_f);
    }

    // 2) 前缀行（渲染 base_vertex 跳过的那几行）与游戏 `draw = 3, 0` 同口径：
    //    内容 = 合并几何最前面 prefix 行（只由第一个 carrier 负责）。
    if (prefix > 0 && index < prefix && index < src_count)
    {
        float3 pp;
        float3 pn;
        float3 pt;
        float pw;
        zz_skin_vertex(src_rows[index], src_blend[index], bone_count, pp, pn, pt, pw);
        zz_write_row(index, pp, pn, pt, pw, stride_f);
    }
}
