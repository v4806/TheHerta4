// shapekey_weight_sync.hlsl
// SSMT / 3DMigoto
//
// 把打包在 IniParams 中转窗口里的形态键强度搬进 mod 专属权重缓冲。
//
// 背景：形态键强度原先直接以 IniParams[100 + i].x 长期驻留在 3DMigoto 的全局
// IniParams 表里。该表是所有 mod / ShaderFix 共享的**唯一**标量通道，既没有
// 命名空间也没有保留区，靠「错开固定槽位」不可能根治。实测事故：某 ShaderFix
// 用 IniParams[190].x 作为「关闭角色描边」的开关，而形态键数 ≥ 91 的工程恰好
// 把第 91 个形态键写到 190，于是播放到该形态键时角色描边整片消失。
//
// 本 CS 让权重不再长期驻留共享表：
//   - 中转窗口按 float4 打包（1 个槽位装 4 个形态键），占用量降到原来的 1/4；
//   - Present 每帧「写窗口 → 本 CS 搬运 → 窗口清零」，共享表里不残留形态键值，
//     即使别的 mod 恰好也用这些槽位，渲染期读到的也永远是 0。
// 权重最终住在 u0 指向的 RWBuffer —— mod 专属资源，资源名即命名空间。
//
// Bindings:
//   u0   = 权重缓冲（RWBuffer<float>，array = 该 hash 的形态键数量）
//   t120 = IniParams
//
// IniParams：第 (SHAPEKEY_WEIGHT_SYNC_BASE + i/4) 个槽位的第 (i%4) 个分量
// = 第 i 个形态键的强度。
// ⚠ 该基址必须与 Python 侧 SSMTNode_PostProcess_ShapeKey.INTENSITY_START_INDEX
//   保持一致（生成器会校验）。

#define SHAPEKEY_WEIGHT_SYNC_BASE 100

RWBuffer<float> ShapeKeyWeight : register(u0);
Texture1D<float4> IniParams : register(t120);

[numthreads(64, 1, 1)]
void main(uint3 threadID : SV_DispatchThreadID)
{
    uint i = threadID.x;
    uint count;
    ShapeKeyWeight.GetDimensions(count);
    if (i >= count)
    {
        return;
    }
    // 动态分量索引（[i & 3]）与 rzm_shapekey_var_sync.hlsl 的既有写法一致。
    ShapeKeyWeight[i] = clamp(IniParams[SHAPEKEY_WEIGHT_SYNC_BASE + (i >> 2)][i & 3], 0.0, 1.0);
}
