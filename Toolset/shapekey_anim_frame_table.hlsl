// --- START OF FILE shapekey_anim_frame_table.hlsl ---
//
// **** FRAME TABLE SHAPE KEY ANIMATION ****
//
// 背景：旧模型对「每个顶点」遍历全部槽位（num_slots 次），每次都要读
// vertex_freq_indices / merged_shapekey_indices，并对一个数百 MB 的
// delta 缓冲做随机访问。序列模式（前缀累积语义）下同一时刻有几十个键
// 权重同为 1，无法提前退出，于是每帧成本正比于 顶点数 × 槽位数。
//
// 本模板把每个分组编译成「逐帧位移表」：
//   - 帧表第 m 条 = 组内前 m 个形态键的累积位移（相对基础网格，不含 base），
//     与「第 m 帧姿态 − base」逐位等价（形态键是线性混合，前缀和可无损预计算）。
//   - 帧表记录数 = key_count + 1（第 0 条恒为零位移起点）。
//   - 半精度打包：2 条 float3 记录占 3 个 uint32（每条 6 字节）。
//   - 每组一张顶点局部索引表（-1 = 该顶点不属于此组），按组累加。
// 于是每顶点只需 2 次顺序读取/组，取代 num_slots 次遍历。
//
// 同步模式的单键即 key_count = 1 的序列组（帧表 [0, delta]，进度 = 该键强度），
// 因此所有键都能统一走本路径，无需双份逻辑。

struct VertexAttributes {
    float3 position;
    float3 normal;
    float4 tangent;
};

RWStructuredBuffer<VertexAttributes> rw_buffer : register(u5);

// 序列组逐帧位移表（半精度打包）。
StructuredBuffer<uint> frame_table : register(t51);

Texture1D<float4> IniParams : register(t120);

// --- [PYTHON-MANAGED BLOCK START] ---
// 组映射表 / 权重缓冲的声明与 #define 由导出器注入。
// --- [PYTHON-MANAGED BLOCK END] ---

// 取出帧表第 rec 条记录（float3 = 3 个 half）。
// 偶数记录落在 [w0.lo, w0.hi, w1.lo]，奇数记录落在 [w1.hi, w2.lo, w2.hi]。
float3 frame_table_load(uint rec)
{
    uint even_base = (rec >> 1u) * 3u;
    uint w0 = frame_table[even_base];
    uint w1 = frame_table[even_base + 1u];
    if ((rec & 1u) == 0u)
    {
        return float3(
            f16tof32(w0 & 0xFFFFu),
            f16tof32(w0 >> 16u),
            f16tof32(w1 & 0xFFFFu));
    }
    uint w2 = frame_table[even_base + 2u];
    return float3(
        f16tof32(w1 >> 16u),
        f16tof32(w2 & 0xFFFFu),
        f16tof32(w2 >> 16u));
}

// 组内按进度 t（0..1）取插值位移：帧 K 与 K+1 之间线性插值。
// key_count = 该组形态键数；帧表记录数 = key_count + 1（第 0 条为零起点）。
// local_index = 该顶点在组顶点并集中的局部下标（>= 0 才调用）。
float3 frame_table_sample(uint group_offset, uint union_size, uint key_count, uint local_index, float t)
{
    // NaN 防护：saturate(NaN) 的结果依赖实现。权重侧虽有 clamp，但本函数是公共入口。
    if (t != t)
    {
        t = 0.0;
    }
    float scaled = saturate(t) * (float)key_count;
    uint frame_index = (uint)floor(scaled);
    if (frame_index >= key_count)
    {
        return frame_table_load(group_offset + key_count * union_size + local_index);
    }
    float frac = scaled - (float)frame_index;
    float3 a = frame_table_load(group_offset + frame_index * union_size + local_index);
    if (frac <= 0.0)
    {
        return a;
    }
    float3 b = frame_table_load(group_offset + (frame_index + 1u) * union_size + local_index);
    return lerp(a, b, frac);
}

[numthreads(16, 1, 1)]
void main(uint3 threadID : SV_DispatchThreadID)
{
    uint i = threadID.x;
    uint vertex_count = rw_buffer.Length;
    if (i >= vertex_count)
    {
        return;
    }

    VertexAttributes output = rw_buffer[i];
    float3 total_diff_position = float3(0.0, 0.0, 0.0);

    // --- [PYTHON-MANAGED LOGIC START] ---
    // 各分组逐帧插值累加，由导出器注入。
    // --- [PYTHON-MANAGED LOGIC END] ---

    // --- [PYTHON-MANAGED WRITEBACK START] ---
    output.position += total_diff_position;
    // --- [PYTHON-MANAGED WRITEBACK END] ---
    rw_buffer[i] = output;
}
// --- END OF FILE shapekey_anim_frame_table.hlsl ---
