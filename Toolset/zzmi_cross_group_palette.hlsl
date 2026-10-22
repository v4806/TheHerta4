// ZZMI 跨组骨骼合并 —— 调色板构建 CS。
//
// 这份是参考实现（loyaltools `common/zzmi_merged_skeleton.py` 的
// PALETTE_SHADER_SOURCE）的逐行移植：结构、索引口径、求逆公式、锚点链全部照搬，
// 只改了注释与标识符大小写。**不要在这里加自己的索引层或参数通道。**
//
//   Builds the bone palette for one component's skinning draw. Every pool slot
//   is expressed in the space of that component's root bone; bones of other
//   components are converted through bones the components share (anchors):
//       E(parent <- child) = Pool[anchor in parent] * inverse(Pool[anchor in child])
//
// 数据布局
// - 池（t0）：全部合并部件骨骼的并集，每骨 3 个 float4（4x3 矩阵，行向量式）。
//   槽位 = 该部件自己的 vg_offset + 本地骨号，各部件各占一段、互不覆盖。
// - 部件表（t1）：每部件两个 uint4 ——
//     [2k+0] = (vg_offset, vg_count, 父部件表下标, 1)
//     [2k+1] = (锚点在父部件里的槽位, 锚点在本部件里的槽位, 0, 0)
// - 参数（t2）：Params[0] = (本部件在部件表里的下标, 合并部件数, 池骨骼数, 0)
// - 输出（u0）：按**池槽位**写，Palette[slot*3 .. slot*3+2]。

Buffer<float4> Pool : register(t0);

// Two entries per merged component:
//   [2k + 0] = (vg_offset, vg_count, parent index, 1)
//   [2k + 1] = (anchor slot in parent, anchor slot in self, 0, 0)
Buffer<uint4> Components : register(t1);

// (table index of this component, merged component count, pool bone count, 0)
Buffer<uint4> Params : register(t2);

RWBuffer<float4> Palette : register(u0);

// Affine 3x4 matrix stored as rows, as the game's float4x3 bones are.
struct Affine
{
    float4 r0;
    float4 r1;
    float4 r2;
};

Affine Identity()
{
    Affine m;
    m.r0 = float4(1, 0, 0, 0);
    m.r1 = float4(0, 1, 0, 0);
    m.r2 = float4(0, 0, 1, 0);
    return m;
}

Affine LoadSlot(uint slot)
{
    Affine m;
    m.r0 = Pool[slot * 3 + 0];
    m.r1 = Pool[slot * 3 + 1];
    m.r2 = Pool[slot * 3 + 2];
    return m;
}

// a * b, both with an implicit (0, 0, 0, 1) bottom row.
float4 MulRow(float4 row, Affine b)
{
    return row.x * b.r0 + row.y * b.r1 + row.z * b.r2 + float4(0, 0, 0, row.w);
}

Affine Mul(Affine a, Affine b)
{
    Affine m;
    m.r0 = MulRow(a.r0, b);
    m.r1 = MulRow(a.r1, b);
    m.r2 = MulRow(a.r2, b);
    return m;
}

Affine Inverse(Affine m)
{
    float3 a = m.r0.xyz;
    float3 b = m.r1.xyz;
    float3 c = m.r2.xyz;
    float3 c0 = cross(b, c);
    float3 c1 = cross(c, a);
    float3 c2 = cross(a, b);
    float det = dot(a, c0);
    // Slots that were never imported are zero; keep the result finite.
    Affine r = Identity();
    if (abs(det) >= 1e-20)
    {
        float inv_det = 1.0 / det;
        // Rows of the inverse 3x3 are the columns of the cofactor matrix.
        float3 i0 = float3(c0.x, c1.x, c2.x) * inv_det;
        float3 i1 = float3(c0.y, c1.y, c2.y) * inv_det;
        float3 i2 = float3(c0.z, c1.z, c2.z) * inv_det;
        float3 t = float3(m.r0.w, m.r1.w, m.r2.w);
        r.r0 = float4(i0, -dot(i0, t));
        r.r1 = float4(i1, -dot(i1, t));
        r.r2 = float4(i2, -dot(i2, t));
    }
    return r;
}

// Conversion from the space of component k to the space of the root.
Affine ToRoot(uint k)
{
    Affine acc = Identity();
    [loop]
    for (uint step = 0; step < 32; ++step)
    {
        uint4 entry = Components[k * 2 + 0];
        if (entry.z == k)
            break;
        uint4 anchor = Components[k * 2 + 1];
        Affine edge = Mul(LoadSlot(anchor.x), Inverse(LoadSlot(anchor.y)));
        acc = Mul(edge, acc);
        k = entry.z;
    }
    return acc;
}

[numthreads(64, 1, 1)]
void main(uint3 thread_id : SV_DispatchThreadID)
{
    uint4 params = Params[0];
    uint slot = thread_id.x;
    if (slot >= params.z)
        return;

    uint owner = params.y;
    [loop]
    for (uint k = 0; k < params.y; ++k)
    {
        uint4 entry = Components[k * 2 + 0];
        if (slot >= entry.x && slot < entry.x + entry.y)
        {
            owner = k;
            break;
        }
    }

    Affine bone = LoadSlot(slot);
    if (owner != params.x && owner < params.y)
    {
        Affine to_self = Mul(Inverse(ToRoot(params.x)), ToRoot(owner));
        bone = Mul(to_self, bone);
    }
    Palette[slot * 3 + 0] = bone.r0;
    Palette[slot * 3 + 1] = bone.r1;
    Palette[slot * 3 + 2] = bone.r2;
}
