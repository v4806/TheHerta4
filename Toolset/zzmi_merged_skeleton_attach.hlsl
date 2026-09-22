// ZZMI 骨骼合并 - 合并骨架 attach CS（绝区零 deform pass 专用，TheHerta4 生成）
//
// 数据布局（FrameAnalysis 实测，详见 ZZMI骨骼合并计划书.md）：
// - 游戏每部件 palette 是 48 字节/骨骼的结构化 buffer（4x3 矩阵 = 12 floats），
//   由 CPU 在该部件 deform pass（pointlist + SO 蒙皮）前 Map 上传，
//   ring buffer 复用，仅当帧有效。
// - 合并骨架为全部件骨骼的并集（槽位 = 全局骨骼 id），顶点组直接使用全局
//   骨骼 id（同骨架组内跨部件共享骨骼合法）。
//
// 调用时序（逐 pass attach + 合并可见 draw 依赖守卫）：
// 本 CS 在该部件的 deform draw **之前**由 deform VB 段 run——cs-t0 是该段
// `ResourceZZPalette_<DrawIB> = copy vs-t0` 刚捕获的**当帧 palette**，
// 按 vg_map 表（cs-t1，局部骨骼 id -> 合并骨架全局槽位）写入本组骨架。
// 若目标部件先于组内载体到达，INI 不会立即绘制依赖多个部件的
// 合并可见几何，而是等所需 palette 全部 attach 后在后续挂点只绘制一次。
// [Present] 只清除到达/绘制标记，不重放持久 palette。

struct ZZBone3x4
{
    float4 r0;
    float4 r1;
    float4 r2;
};

// 当前 deform pass 的 palette（调用方在把 vs-t0 换绑到合并骨架之前保存到 cs-t0）
// 【2026-09-22 跨显卡修复】palette 必须声明为 Buffer<float4>（16B 元素），
// 而不是 StructuredBuffer<ZZBone3x4>（48B）。
// 原因：palette 是 `copy vs-t0` 的目标，加载器会把源视图的 Format 注入进来
// （探针实测 ->Format = 2 = R32G32B32A32_FLOAT），于是它的 SRV 是 typed 16B 元素。
// 若 shader 按 48B 结构化声明，声明与描述符不匹配即落进 D3D11 未定义区：
// 实测 N 卡按声明（48）读 → 正常；A 卡按描述符（16）读 → 骨架 2/3 错位 → 模型爆炸。
// 用 Buffer<float4> 与视图精确匹配后，手工按 bone*3 取三个 float4，两卡行为一致。
// 【注意】不要给 ini 的 palette 资源加 format —— 那会把 buffer 的
// StructureByteStride 改成 16（ByteWidth 变 528），而 copy 要拷 1584 字节会溢出。
Buffer<float4> src_palette : register(t0);
// 局部骨骼 id -> 合并骨架全局槽位 映射表（vg_map；3DMigoto 的
// format=R32G32B32A32_UINT 创建格式化缓冲，故用 Buffer<uint4> 声明——
// 与视图类型精确匹配，读取全量元素；槽位值在 .x，每槽后跟 3 个 0）
Buffer<uint4> vg_map : register(t1);
// 合并骨架（同时作为 SRV 换绑到 deform pass 的 vs-t0 供蒙皮读取）
// 【2026-09-22 读取侧修复】合并骨架必须声明为 RWBuffer<float4>（16B 元素），
// 而不是 RWStructuredBuffer<ZZBone3x4>（48B）。
// 原因：游戏的 deform VS 是按 typed 索引 `palette[3b+m]` 读取本缓冲的。若本资源
// 是结构化 buffer（SBS=48），则出现声明/视图不匹配 —— 实测 A 卡驱动改用 buffer
// 的 SBS(48) 算字节偏移 → 骨号 b 实际读到槽位 3b/3b+1/3b+2 → b >= 35 (=⌈104/3⌉)
// 越界 → 顶点位置精确零（A 卡 5939/11111 个顶点为零，阈值恰为 35）。
// 改成 typed（ini: type=RWBuffer + format=R32G32B32A32_FLOAT + array=1122）后，
// SRV 元素尺寸 = 16B，与 `palette[3b+m]` 精确匹配 → 两卡取址一致。
// 【注意】ini 必须显式写 bind_flags = shader_resource unordered_access，
// 否则 SRV 创建会静默失败并绑 NULL（上一轮 F1 就是这样把 N 卡弄坏的）。
RWBuffer<float4> merged_skeleton : register(u0);

// 3Dmigoto ini 参数纹理：本 fork（3Dmigoto-Armor）实测 y1 在 IniParams[1].y
// （2026-08-23 双帧实证：读 [0].y -> count=0 全零不写；读 [1].y -> count=y1 生效）。
// 标准版 3DMigoto 是 IniParams[0]=(x1,y1,z1,w1)，本 fork 布局从 [1] 起。
Texture1D<float4> IniParams : register(t120);
#define ZZ_ATTACH_COUNT IniParams[1].y

[numthreads(64, 1, 1)]
void main(uint3 dispatch_id : SV_DispatchThreadID)
{
    uint bone_index = dispatch_id.x;
    uint count = (uint)ZZ_ATTACH_COUNT;
    if (bone_index >= count)
    {
        return;
    }

    // y1、palette 与 vg_map 必须描述同一批局部骨骼。运行时按真实资源长度
    // 再做一次保护，避免损坏/陈旧的 mod 资源造成 SRV 越界读取。
    uint palette_count = 0;
    uint vg_map_count = 0;
    // Buffer<float4> 的 GetDimensions 返回 float4 元素数 = 骨骼数 * 3
    src_palette.GetDimensions(palette_count);
    palette_count /= 3u;
    vg_map.GetDimensions(vg_map_count);
    if (bone_index >= palette_count || bone_index >= vg_map_count)
    {
        return;
    }

    // 按 vg_map 写入全局槽位：本部件引用的骨骼（含共享 canonical）当帧覆盖。
    // vg_map 是 3DMigoto 的 format=R32G32B32A32_UINT 格式化缓冲，用 Buffer<uint4>
    // 声明（与视图精确匹配；StructuredBuffer 声明曾导致只读到第 0 个元素、
    // 其余骨骼全部塌进 slot 0——2026-08-23 新 dump 实证 G3 仅 3 槽非零）。
    uint slot = vg_map[bone_index].x;
    // RWBuffer<float4> 的 GetDimensions 返回 float4 元素数 = 槽位数 * 3
    uint merged_count = 0;
    merged_skeleton.GetDimensions(merged_count);
    merged_count /= 3u;
    if (slot < merged_count)
    {
        // 每根骨占 3 个 float4（4x3 矩阵），显式按 bone*3 取 ——
        // 与 palette 的 typed 16B 视图精确匹配，不依赖任何 stride 解释。
        uint base = bone_index * 3u;
        ZZBone3x4 m;
        m.r0 = src_palette[base + 0u];
        m.r1 = src_palette[base + 1u];
        m.r2 = src_palette[base + 2u];
        // 每槽占 3 个 float4，显式按 slot*3 写入 —— 与 typed 视图精确对齐
        uint dst = slot * 3u;
        merged_skeleton[dst + 0u] = m.r0;
        merged_skeleton[dst + 1u] = m.r1;
        merged_skeleton[dst + 2u] = m.r2;
    }
}
