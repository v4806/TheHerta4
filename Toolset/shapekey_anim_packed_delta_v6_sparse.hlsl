// --- START OF FILE shapekey_anim_packed_delta_v6_sparse.hlsl ---
//
// **** ADDITIVE ANIMATION SHADER - V6 SPARSE VERTEX INDEX ****
// Contributors: Zlevir, Assistant
// Version: 6.0
// Description:
//   - 顶点命中索引（稀疏查找）：每个顶点只遍历**自己真正命中**的形态键
//     （典型 2~5 个），取代「逐顶点遍历全部槽位」（旧模型每顶点固定跑
//     num_slots 次，且 `!= NO_FREQ_INDEX` 的早退会被 warp divergence 吃掉）。
//   - 合并数据/索引缓冲与 V5 合并模式一致（t51 位移 / t52 合并映射）；
//     稠密的 vertex_freq_indices（顶点数 × 槽位数）被三份稀疏索引取代。
//   - 条目内容 = 「该顶点的某个命中键 ↔ 它的位移记录下标与强度槽位」。
//
// 稀疏索引资源（另见 blueprint/shapekey_sparse_index.py）：
//   t96 vertex_entry_start    每个顶点的条目区间起点（顶点数 + 1 个 uint32）
//   t97 vertex_entry_packed   条目对应的合并位移记录下标（-1 = 无位移）
//   t98 vertex_entry_freq     条目对应的形态键强度下标（ShapeKeyWeight 槽位）

#define NO_FREQ_INDEX 0xFFFFFFFFu

struct VertexAttributes {
    float3 position;
    float3 normal;
    float4 tangent;
};

RWStructuredBuffer<VertexAttributes> rw_buffer : register(u5);
StructuredBuffer<float3> merged_shapekey_pos_deltas : register(t51);
// t52（合并映射）在稀疏模式下不再被逻辑块读取，但 Present 仍按旧契约绑定它，
// 因此保留声明以免与绑定表不一致。
StructuredBuffer<int> merged_shapekey_indices : register(t52);

// 顶点命中索引：t96/t97/t98 —— 避开 t75+（非合并紧凑映射）、t99（旧 FREQ 表）
// 与 t100-t102（拖拽驱动与权重缓冲）。
StructuredBuffer<uint> vertex_entry_start : register(t96);
StructuredBuffer<int> vertex_entry_packed : register(t97);
StructuredBuffer<uint> vertex_entry_freq : register(t98);

Texture1D<float4> IniParams : register(t120);

// --- [PYTHON-MANAGED BLOCK START] ---
// --- [PYTHON-MANAGED BLOCK END] ---

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
    // 法线/切线累加器：仅当「存储全部顶点属性增量」开启时才会被逻辑块填充，
    // 关闭时恒为 0，写回是逐位无操作（与旧版行为一致）。
    float3 total_diff_normal = float3(0.0, 0.0, 0.0);
    float3 total_diff_tangent = float3(0.0, 0.0, 0.0); // 切线增量只到 xyz，w 是手性符号

    // --- [PYTHON-MANAGED LOGIC START] ---
    // --- [PYTHON-MANAGED LOGIC END] ---

    // --- [PYTHON-MANAGED WRITEBACK START] ---
    output.position += total_diff_position;
    // --- [PYTHON-MANAGED WRITEBACK END] ---
    rw_buffer[i] = output;
}
// --- END OF FILE shapekey_anim_packed_delta_v6_sparse.hlsl ---
