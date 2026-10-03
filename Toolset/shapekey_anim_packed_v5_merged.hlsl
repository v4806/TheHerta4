// --- START OF FILE shapekey_anim_packed_v5_merged.hlsl ---
//
// **** ADDITIVE ANIMATION SHADER - V5 MERGED PACKED BUFFER ****
// Contributors: Zlevir, Assistant
// Version: 5.0
// Description:
//   - Merge all slot packed buffers into one data buffer.
//   - Merge all slot index maps into one flattened index buffer.
//   - Optional optimized FREQ lookup stays as a separate buffer.

#define NO_FREQ_INDEX 255

struct VertexAttributes {
    float3 position;
    float3 normal;
    float4 tangent;
};

RWStructuredBuffer<VertexAttributes> rw_buffer : register(u5);
StructuredBuffer<VertexAttributes> base : register(t50);
StructuredBuffer<VertexAttributes> merged_shapekeys : register(t51);
StructuredBuffer<int> merged_shapekey_indices : register(t52);
StructuredBuffer<uint> vertex_freq_indices : register(t53);

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
// --- END OF FILE shapekey_anim_packed_v5_merged.hlsl ---
