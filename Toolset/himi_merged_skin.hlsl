// himi_merged_skin.hlsl —— HIMI（崩坏3 / HI3）合并几何「渲染段自蒙皮」CS（**一个文件，全部族共用**）
//
// 为什么需要它（t86，2026-10-24 实机定性 ⇒ 路线 1）：
// 合并骨架每帧的写方只在**渲染（indexed）段** —— 只有那里绑着当帧的 `vs-cb3`（160 B，
// rows 0..7 = 物体矩阵 + 其逆）与各族的当帧 palette。可见几何过去由**变形段**那笔
// `draw = <行数>, 0`（流输出重放）产出，而变形段整批排在渲染段之前 ⇒ 借骨来的那部分
// 顶点只能拿到**上一帧**写进骨架的骨 ⇒ 一起跑就慢一帧、一动就抖。
// 冻结测试（t85，只注释掉那笔 `draw`）：整块身体冻在 APOS、不再跟骨架走 ⇒ 可见几何的
// 唯一写方就是变形段那笔重放，坐实了上面那句。
//
// 本 CS 把蒙皮**搬到渲染段**、紧贴 `drawindexed` 之前：
//   cs-t0 = 该族**静态**源顶点（模组自己上传的 Position，永远不会被流输出改写）
//   cs-t1 = 该族 Blend（4 权重 + 4 全局槽位）
//   cs-t2 = 合并骨架（当帧，由紧前那批 attach 写全）
//   cs-u0 = 自建输出缓冲（ini 里 `type = Buffer / stride = 44 / array = 行数 /
//           bind_flags = shader_resource unordered_access vertex_buffer`）——
//           元素尺寸必须 = 44 B（见下方 `HIMIOutRow44`）
// 宿主段接着 `vb0 = ResourceHI3SkinOut_<fam>` 把结果当顶点缓冲画这一笔。
// ⇒ 同一帧的骨、同一帧的顶点，帧内只有一代骨，借骨部件不再晚一帧。
//
// 【为什么必须是自建缓冲，不能写进游戏那笔流输出目标】别名要求 bind flag 兼容：游戏 SO 只有
// `vertex_buffer shader_resource stream_output`，这里的目标带 `unordered_access` ⇒ 视图创建
// 直接失败（ZZMI 的帧日志铁证 FrameAnalysis-2026-10-11-003405 draw 000028：
// `WARNING: referenced resource is missing bind flags required by destination, view creation
// will fail`）。自建缓冲同一次 draw 里既当 UAV 写又当 vb0 读是不允许的 —— 本 CS 的写发生在
// dispatch、读发生在紧随其后的 draw，两者不同调用，合法。
//
// 【44 B/行的布局】HI3 deform 顶点 = P12 N12 C4 TA16（11 个 float），逐 float 是：
//   0..2 位置 · 3..5 法线 · 6 色 · 7..10 切线（w 分量是手性）。
// 结构体因此是 float4 + float4 + float3：a.xyz = 位置、a.w/b.x/b.y = 法线、b.z = 色、
// b.w/c.xyz = 切线 —— 不是随手对齐，逐偏移与上面那张表一致。
//
// 【不读 IniParams】本 CS 没有任何逻辑参数：行数、行宽全部取资源自身维度（GetDimensions）。
// 3DMigoto 的 ini 参数纹理布局随 fork 变化（标准版 x1/y1 落 [0]，Armor fork 从 [1] 起），
// 读错位置只会读到未设置的 0 ⇒ 蒙皮不写、顶点全零，且**不报任何错**（本仓 2026-09-22 记忆）。
//
// 【骨架必须是 typed 16 B】合并骨架在 ini 里是 `type = RWBuffer / format = R32G32B32A32_FLOAT`
// ⇒ 这里只能声明成 `Buffer<float4>` 并手工按 `bone * 3` 取三个 float4。按结构化 48 B 声明会落进
// D3D11 未定义区（AMD 按描述符取址 ⇒ 骨号被放大 3 倍 ⇒ 模型爆炸）。

struct HIMIBone3x4
{
    float4 r0; // 3x4 刚体矩阵的第一行（xyz = R0, w = t.x）
    float4 r1;
    float4 r2;
};

// 44 B/行：位置 = 0..2，法线 = 3..5，色 = 6，切线 = 7..10（导出 Position 类目实测布局）
struct HIMIVertex44
{
    float4 a; // 0..3
    float4 b; // 4..7
    float3 c; // 8..10
};

struct HIMIBlend32
{
    float4 w; // 4 个权重
    uint4 i;  // 4 个全局槽位
};

StructuredBuffer<HIMIVertex44> src_rows : register(t0);
StructuredBuffer<HIMIBlend32> src_blend : register(t1);
Buffer<float4> merged_skeleton : register(t2);
// 44 B/行的**写目标**。元素尺寸必须与 ini 的 `stride = 44` 一致：D3D 侧给这块缓冲建的
// StructureByteStride 就是 44（2026-10-24 实机 `addon.gpu` 读到 `structure_byte_stride = 44`），
// 结构化 UAV 的取址按**元素**算 ⇒ 元素必须是 44 B 的结构体。
// 反例（t88，真机已证）：把元素声明成单个 float（4 B）时 `row * 11 + i` 的字节偏移
// 会被放大成 row*11*44（≈11 MB，缓冲只有 1 MB）⇒ 全部越界；D3D11 对越界 UAV 写是
// **静默丢弃**，缓冲保持全零 ⇒ 蒙皮结果不存在 ⇒ 整具模型消失（SkinOut 三个偏移读到的
// float 全是 0，而 `dispatch 358,1,1` 确实跑在那笔 `drawindexed 87438` 之前）。
struct HIMIOutRow44
{
    float4 a; // 0..3（0..2 位置 · 3 法线 x）
    float4 b; // 4..7（4..5 法线 yz · 6 色 · 7 切线 x）
    float3 c; // 8..10（8..9 切线 yz · 10 切线 w = 手性）
};

RWStructuredBuffer<HIMIOutRow44> dst_rows : register(u0);

// 单个顶点 LBS：位置带平移，法线/切线只取 3x3（骨矩阵是刚体且正交 ⇒ R 即等价于逆转置）
void himi_skin_vertex(
    HIMIVertex44 v,
    HIMIBlend32 blend,
    uint bone_count,
    out float3 skinned_p,
    out float3 skinned_n,
    out float3 skinned_t,
    out float4 tangent)
{
    float3 p = v.a.xyz;
    float3 n = float3(v.a.w, v.b.x, v.b.y);
    tangent = float4(v.b.w, v.c.x, v.c.y, v.c.z);

    skinned_p = float3(0, 0, 0);
    skinned_n = float3(0, 0, 0);
    skinned_t = float3(0, 0, 0);

    [unroll]
    for (uint k = 0; k < 4; ++k)
    {
        float w = blend.w[k];
        uint bone = blend.i[k];
        if (w == 0.0f || bone >= bone_count)
        {
            continue;
        }
        // 每根骨占 3 个 float4（3x4 矩阵），显式按 bone * 3 取 —— 与骨架的 typed 16 B 视图
        // 精确匹配，不依赖任何 stride 解释。
        uint b3 = bone * 3u;
        HIMIBone3x4 m;
        m.r0 = merged_skeleton[b3 + 0u];
        m.r1 = merged_skeleton[b3 + 1u];
        m.r2 = merged_skeleton[b3 + 2u];
        skinned_p += w * (float3(dot(m.r0.xyz, p) + m.r0.w,
                                 dot(m.r1.xyz, p) + m.r1.w,
                                 dot(m.r2.xyz, p) + m.r2.w));
        skinned_n += w * float3(dot(m.r0.xyz, n), dot(m.r1.xyz, n), dot(m.r2.xyz, n));
        skinned_t += w * float3(dot(m.r0.xyz, tangent.xyz),
                               dot(m.r1.xyz, tangent.xyz),
                               dot(m.r2.xyz, tangent.xyz));
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

// 整行写出：位置/法线/色/切线四个属性，与源行同布局（一个元素 = 一行 = 44 B）
void himi_write_row(uint row, float3 pos, float3 nrm, float color, float3 tan, float tan_w)
{
    HIMIOutRow44 o;
    o.a = float4(pos, nrm.x);
    o.b = float4(nrm.y, nrm.z, color, tan.x);
    o.c = float3(tan.y, tan.z, tan_w);
    dst_rows[row] = o;
}

[numthreads(64, 1, 1)]
void main(uint3 dispatch_id : SV_DispatchThreadID)
{
    uint index = dispatch_id.x;

    // 资源权威值：行数与行宽都取源缓冲自身维度（损坏/陈旧的 mod 资源不越界写）
    uint src_count = 0;
    uint src_stride = 0;
    src_rows.GetDimensions(src_count, src_stride);
    if (src_count == 0 || index >= src_count)
    {
        return;
    }
    // Buffer<float4> 的 GetDimensions 返回 float4 元素数 = 骨骼数 * 3
    uint bone_count = 0;
    merged_skeleton.GetDimensions(bone_count);
    bone_count /= 3u;

    // 行宽 = 源 Position 类目 stride（导出侧保证源/目的同布局 ⇒ 44）；写目标按**元素**寻址，
    // 元素就是一行 ⇒ 先按写目标自己的维度封边界（行数少蒙时宁可少写，也不许越界丢写）。
    // 结构化缓冲的 GetDimensions **只有两参重载**（count, stride）：写成单参 `GetDimensions(dst_count)`
    // 会让 D3DCompile 直接失败（`error X3013: 'GetDimensions': no matching 1 parameter intrinsic method`），
    // 而 3DMigoto 对编译失败的 shader 是**静默跳过**（只在默认关闭的 d3d11_log 里留一行）⇒
    // dispatch 照常记录、缓冲一个字节都不写 ⇒ 整具模型塌到原点消失，不报错也不崩（t89 实机）。
    uint dst_count = 0;
    uint dst_stride = 0;
    dst_rows.GetDimensions(dst_count, dst_stride);
    if (src_stride != 44u || dst_stride != 44u || index >= dst_count)
    {
        return;
    }

    float3 skinned_p;
    float3 skinned_n;
    float3 skinned_t;
    float4 tangent;
    himi_skin_vertex(src_rows[index], src_blend[index], bone_count,
                     skinned_p, skinned_n, skinned_t, tangent);
    himi_write_row(index, skinned_p, skinned_n, src_rows[index].b.z,
                   skinned_t, tangent.w);
}
