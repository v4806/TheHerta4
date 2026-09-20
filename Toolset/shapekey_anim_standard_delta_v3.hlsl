// --- START OF FILE shapekey_anim_standard_delta_v3.hlsl ---

// **** ADDITIVE ANIMATION SHADER - POSITION-ONLY DELTA (HYBRID BUFFERS) ****
// Contributors: Zlevir, Assistant
// Version: 3.0 (Correct Hybrid Buffer Handling)
// Description: Reads position deltas (stride=12) and applies them to a full vertex buffer (stride=40).

#define MAX_SLOTS 24 

struct VertexAttributes {
    float3 position;
    float3 normal;
    float4 tangent;
};

// --- I/O BUFFERS ---
RWStructuredBuffer<VertexAttributes> rw_buffer : register(u5);
StructuredBuffer<float3> shapekey_pos_deltas[MAX_SLOTS] : register(t51);

// --- PARAMETERS ---
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
    
    // Start with the base mesh attributes (copied to rw_buffer beforehand)
    VertexAttributes output = rw_buffer[i];
    
    float3 total_diff_position = float3(0.0, 0.0, 0.0);
    // 法线/切线累加器：仅当「存储全部顶点属性增量」开启时才会被逻辑块填充，
    // 关闭时恒为 0，写回是逐位无操作（与旧版行为一致）。
    float3 total_diff_normal = float3(0.0, 0.0, 0.0);
    float3 total_diff_tangent = float3(0.0, 0.0, 0.0); // 切线增量只到 xyz，w 是手性符号

    // --- [PYTHON-MANAGED LOGIC START] ---
    // The Blender plugin will generate blending logic here.
    // It reads from the delta buffer and applies it to the vertex attributes.
    // --- [PYTHON-MANAGED LOGIC END] ---

    // --- [PYTHON-MANAGED WRITEBACK START] ---
    output.position += total_diff_position;
    // --- [PYTHON-MANAGED WRITEBACK END] ---
    
    rw_buffer[i] = output;
}
// --- END OF FILE shapekey_anim_standard_delta_v3.hlsl ---
