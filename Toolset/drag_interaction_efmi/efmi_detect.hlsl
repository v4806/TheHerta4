// EFMI drag hit detection (independent implementation, TheHerta4, 256-zone
// sparse model). One dispatch per spatial instance: rasterize the projected
// body probe into screen space (top-left/downward, matching cursor_*), test
// the cursor against every indexed triangle via barycentric coordinates,
// reduce by reverse-Z depth (Endfield clears its DSV to 0), and write the
// per-instance Candidate record.
//
// Sparse zone weights: each vertex stores up to K (zone id, weight) pairs;
// the winning triangle interpolates all K pairs with the barycentrics and the
// strongest interpolated zone becomes the hit.
//
// Candidate layout (per instance, 4 float4 records):
//   [0] = (zone id 0-255, strongest weight, reverse-Z depth, frame tag)
//   [1] = frozen screen-right basis + valid
//   [2] = frozen screen-down basis + valid
//   [3] = hit-point screen position (R11: barycentric-interpolated clip of the
//         winning triangle -> ToScreen; NOT the zone-center anchor anymore) + valid
//   base = IniParams[150].z * 4
Texture2D<float4> Projected : register(t0);
Texture2D<float4> Anchors : register(t1);
Buffer<uint> Indices : register(t2);
Buffer<uint> ZoneIDs : register(t3);
Buffer<float> ZoneWeights : register(t4);
RWBuffer<float4> Candidate : register(u0);
Texture1D<float4> IniParams : register(t120);

groupshared float bestDepths[256];
groupshared float2 bestHits[256];
// R11: winning-triangle hit-point screen UV (barycentric-interpolated clip ->
// ToScreen), carried through the reduction like bestHits/bestDepths.
groupshared float2 bestUV[256];

static const uint K = 4;

float4 ReadClip(uint vertexId) {
    // t47 不符合项修正：探针宽读 IniParams[150].x（不再硬编码 512——
    // Project RT 宽度即 512 但语义应跟随导出端声明）
    uint width = max((uint)IniParams[150].x, 1u);
    return Projected.Load(int3(vertexId % width, vertexId / width, 0));
}

// Screen UV for hit testing. The cursor is fed top-left/downward
// (Present normalizes the fork cursor_* with `cursor_y / 2`, so 0 = screen top,
// 1 = screen bottom). The probe stores the native game VS clip output, which is
// standard D3D NDC (top of screen = c.y/c.w = +1). Therefore the minus-Y NDC
// flip MUST apply to convert clip->screen UV, exactly like ZZMI's
// ClipToScreenUV (`0.5 - ndc.y * 0.5`, rzm_object_detect.hlsl L330).
// R7/t48 bugfix: the old `0.5 + 0.5 * c.y / c.w` left the triangle UV Y-up
// (1 = top) while the cursor was top-down (0 = top) — every hit landed at the
// vertical mirror, so an upper-screen cursor produced a hit far below
// ("命中偏下").
float2 ToScreen(float4 c) {
    return float2(.5 + .5 * c.x / c.w, .5 - .5 * c.y / c.w);
}

float Cross2(float2 a, float2 b) {
    return a.x * b.y - a.y * b.x;
}

void ComputeBasis(uint zone, uint start) {
    // C3c/t13：锚点 RT 只烘焙了配置区（width = 8×区数，探针 drawindexed 同；
    // align-t3 起每区 8 行——0-3 恒等轴（本函数消费的拖拽基）、4-7 gizmo
    // 表面轴（hand_preview 消费））；命中区若 ≥ 烘焙区数，zone*8+0..3 会越出
    // AnchorProject 纹理 → Load 返回垃圾/钳制 → right/down 基失效。用纹理
    // 宽度做上限守卫（width/8 = 区数），越界时 basis valid=0（拖拽回退为
    // 无效基，不产生越界读）。
    uint anchorWidth, anchorHeight;
    Anchors.GetDimensions(anchorWidth, anchorHeight);
    if (zone * 8 + 3 >= anchorWidth) {
        Candidate[start + 1] = float4(0, 0, 0, 0);
        Candidate[start + 2] = float4(0, 0, 0, 0);
        Candidate[start + 3] = float4(-10, -10, 0, 0);
        return;
    }
    float4 c = Anchors.Load(int3(zone * 8 + 0, 0, 0));
    float4 x = Anchors.Load(int3(zone * 8 + 1, 0, 0));
    float4 y = Anchors.Load(int3(zone * 8 + 2, 0, 0));
    float4 z = Anchors.Load(int3(zone * 8 + 3, 0, 0));

    float3 right = 0, down = 0;
    float valid = 0;
    if (min(min(c.w, x.w), min(y.w, z.w)) > 1e-5) {
        float2 s = ToScreen(c);
        float2 dx = (ToScreen(x) - s) / .01;
        float2 dy = (ToScreen(y) - s) / .01;
        float2 dz = (ToScreen(z) - s) / .01;
        float3 jx = float3(dx.x, dy.x, dz.x);
        float3 jy = float3(dx.y, dy.y, dz.y);
        float a = dot(jx, jx);
        float b = dot(jx, jy);
        float d = dot(jy, jy);
        float det = a * d - b * b;
        // B2/t26: basis conditioning guard (ZZMI object_detect L392-394
        // conditioning = det/(a00*a11) > 1e-5) — reject near-degenerate
        // anisotropic Jacobians, not just det <= 0.
        float conditioning = det / max(a * d, 1e-12);
        if (det > 1e-10 && conditioning > 1e-5) {
            right = (d * jx - b * jy) / det;
            down = (a * jy - b * jx) / det;
            valid = 1;
        }
    }
    Candidate[start + 1] = float4(right, valid);
    Candidate[start + 2] = float4(down, valid);
    // R11: Candidate[3] is now written by main() as the hit-point screen UV
    // (not the zone-center anchor); ComputeBasis only produces the right/down
    // basis from the zone-center anchor Jacobian. The out-of-range guard above
    // still writes an invalid [3] for that path.
}

[numthreads(256, 1, 1)]
void main(uint tid : SV_GroupIndex) {
    float2 cursor = IniParams[151].xy;
    // t48-b: this fork's cursor_y is **bottom-up** (0 = screen bottom), while
    // ToScreen() below produces a **top-down** uv (0 = screen top). ZZMI flips at
    // the source -- node_postprocess_draginteraction.py L4270/4272/4274 flip ALL
    // three cascade branches (`$cursorY = 1.0 - cursor_y`, `1.0 - cursor_window_y`,
    // `1.0 - cursor_screen_y / $screenH`) -- but EFMI's [Present] passes
    // `$cursorY = cursor_y` straight through, so the absolute hit test compared a
    // bottom-up cursor against a top-down uv and the whole hit region came out
    // vertically mirrored (direction was fine because the *delta* path already
    // compensates). The flip belongs HERE and not at the source: efmi_simulate.hlsl
    // (`delta.y = -delta.y`) and efmi_hand_preview.hlsl (`uvDelta.y = -uvDelta.y`)
    // already compensate that same Y-up convention -- flipping [151].y globally
    // would double-flip them and invert the drag.
    // t52 定案：本翻转 = **出厂正确态**（由 IniParams[153].w 门控，两处共用）
    //   0 / 未写 = **做**这次翻转（出厂默认）—— 本 fork 的 cursor_y 是 bottom-up
    //   1        = 不做（cursor_y 按 top-down 直用，仅供现场对照）
    // [Present] 里 `w153 = $ssmtdrag_efmi_yflip_hit_A`，游戏内 F9 循环 0↔1。
    //
    // 为什么是 bottom-up（三条独立实证，缺一不可）：
    //   ① t14：把 `delta`（原始光标增量）直接喂进 detect 的拖拽基 right/down 会让
    //      模型上下走反，**实测**必须在 `delta.y` 上取一次负号才对 ⇒ 原始光标 y
    //      与基的 uv.y 方向相反，而基的 uv.y 来自右侧 ToScreen（top-down）。
    //   ② t51：grab center = 区中心 + right*Δu.x + down*Δu.y，Δu 若用**原始**光标
    //      与区中心投影做差（= 本开关关闭时的行为），形变落在抓取点的**上下镜像**
    //      处（用户实测「鼠标命中上方，它拖拽的是下方」）—— 同一个符号问题。
    //   ③ 两者同源：一处需要负号（t14 的 delta）、另一处不需要（t51 的 Δu），
    //      只有当「原始光标 = bottom-up、基 = top-down」时二者才同时自洽。
    // 历史：t49 曾按 ZZMI 三个级联分支把它反推成「不必翻转」并把默认设成 0 =
    // 不翻 —— 那等于把 ① ② 的符号链拆散一半（命中区不可见，所以当时看不出来）。
    // t52 按实测回正，且把 0 定为**翻转态**：导出器即使不注入本开关，shader 默认
    // 也是对的。
    //
    // 注意：本开关只动**绝对命中判定**与 t51 的 Δu；simulate 的拖拽 delta 读的是
    // 原始 [151].xy（不受此处影响），故 t14 `delta.y = -delta.y` 与 t18
    // `uvDelta.y = -uvDelta.y` 的「拖拽链已实测正确」结论与之互不牵连。
    if (IniParams[153].w < 0.5)
        cursor.y = 1.0 - cursor.y;
    // B1/t26: cursor validity guard (ZZMI NormalizedCursorValid, object_detect
    // L150-178) — NaN or out-of-range cursor forces the miss path.
    bool cursorValid = (cursor.x == cursor.x) && (cursor.y == cursor.y)
        && cursor.x >= -0.01 && cursor.x <= 1.01
        && cursor.y >= -0.01 && cursor.y <= 1.01;
    uint triangleCount = 0;
    Indices.GetDimensions(triangleCount);
    triangleCount /= 3;

    float best = 1e30;
    float2 hit = 0;
    // A2/t26: padUV edge tolerance (ZZMI hitPadPixels/screenRes, object_detect
    // L428-471) — accept a cursor up to ~4px (screen-uv) outside a triangle
    // edge so thin/edge features stay grabbable.
    const float HIT_PAD = 0.002;
    for (uint t = tid; cursorValid && t < triangleCount; t += 256) {
        uint3 ix = uint3(Indices[t * 3], Indices[t * 3 + 1], Indices[t * 3 + 2]);
        float4 ca = ReadClip(ix.x);
        float4 cb = ReadClip(ix.y);
        float4 cc = ReadClip(ix.z);
        if (min(ca.w, min(cb.w, cc.w)) <= 1e-5) {
            continue;
        }
        float2 a = ToScreen(ca);
        float2 b = ToScreen(cb);
        float2 c = ToScreen(cc);
        if (any(cursor < min(a, min(b, c))) || any(cursor > max(a, max(b, c)))) {
            continue;
        }
        float den = Cross2(b - a, c - a);
        if (abs(den) < 1e-12) {
            continue;
        }
        float v = Cross2(cursor - a, c - a) / den;
        float q = Cross2(b - a, cursor - a) / den;
        float3 bary = float3(1 - v - q, v, q);
        bool inside = all(bary >= -0.0001);
        if (!inside) {
            // A2: edge-pad acceptance — distance to the nearest edge in uv
            // = -min(bary) * edge length; accept when within HIT_PAD.
            float dBC = -bary.x * length(b - c);
            float dCA = -bary.y * length(c - a);
            float dAB = -bary.z * length(a - b);
            if (max(dBC, max(dCA, dAB)) < HIT_PAD) {
                inside = true;
                bary = saturate(bary);   // clamp to the edge point for interpolation
                float s = bary.x + bary.y + bary.z;
                bary = s > 1e-9 ? bary / s : float3(1, 0, 0);
            } else {
                continue;
            }
        }
        float3 pb = bary / float3(ca.w, cb.w, cc.w);
        pb /= max(dot(pb, 1.0), 1e-10);
        float ndcDepth = dot(bary, float3(ca.z / ca.w, cb.z / cb.w, cc.z / cc.w));
        float depth = 1.0 - ndcDepth;
        if (depth < best) {
            best = depth;
            // R11: hit-point screen UV = ToScreen(barycentric-interpolated clip)
            // of the winning triangle — the exact model point under the cursor
            // (hand_preview uses Candidate[3] as its anchor instead of the
            // zone-center anchor).
            float4 hitClip = bary.x * ca + bary.y * cb + bary.z * cc;
            bestUV[tid] = ToScreen(hitClip);
            // Interpolate the sparse zone table over the triangle vertices;
            // pick the strongest interpolated zone.
            float bestW = 0;
            float bestZone = 0;
            [unroll]
            for (uint i = 0u; i < K; ++i) {
                uint z0 = ZoneIDs[ix.x * K + i];
                uint z1 = ZoneIDs[ix.y * K + i];
                uint z2 = ZoneIDs[ix.z * K + i];
                float w0 = ZoneWeights[ix.x * K + i];
                float w1 = ZoneWeights[ix.y * K + i];
                float w2 = ZoneWeights[ix.z * K + i];
                if (z0 != 0xFFFFFFFFu) {
                    float w = w0 * pb.x;
                    if (z1 == z0) w += w1 * pb.y;
                    if (z2 == z0) w += w2 * pb.z;
                    if (w > bestW) { bestW = w; bestZone = (float)z0; }
                }
                if (z1 != 0xFFFFFFFFu && z1 != z0) {
                    float w = w1 * pb.y;
                    if (z0 == z1) w += w0 * pb.x;
                    if (z2 == z1) w += w2 * pb.z;
                    if (w > bestW) { bestW = w; bestZone = (float)z1; }
                }
                if (z2 != 0xFFFFFFFFu && z2 != z0 && z2 != z1) {
                    float w = w2 * pb.z;
                    if (z0 == z2) w += w0 * pb.x;
                    if (z1 == z2) w += w1 * pb.y;
                    if (w > bestW) { bestW = w; bestZone = (float)z2; }
                }
            }
            hit = float2(bestZone, bestW);
            // A1/t26: hit weight threshold (ZZMI 1e-4, object_detect L654);
            // the threshold value comes from IniParams[152].w (emitted 1e-4).
            if (hit.y < IniParams[152].w) {
                hit = 0;
            }
        }
    }

    bestDepths[tid] = best;
    bestHits[tid] = hit;
    // R11 repair (t19): sentinel is CONDITIONAL — threads that hit wrote
    // bestUV[tid] at L175 and must keep that value through the reduction;
    // only threads with no hit get the center sentinel. (The previous
    // unconditional write clobbered the hit UV, so Candidate[3] always ended
    // up at the screen center.)
    if (best >= 1e30) {
        bestUV[tid] = ToScreen(float4(0, 0, 0, 1));   // sentinel: no hit this thread
    }
    GroupMemoryBarrierWithGroupSync();

    for (uint step = 128; step > 0; step >>= 1) {
        if (tid < step && bestDepths[tid + step] < bestDepths[tid]) {
            bestDepths[tid] = bestDepths[tid + step];
            bestHits[tid] = bestHits[tid + step];
            bestUV[tid] = bestUV[tid + step];
        }
        GroupMemoryBarrierWithGroupSync();
    }

    if (tid == 0) {
        uint start = (uint)IniParams[150].z * 4;
        // A3/t26: on miss write frame-1 tag so deform's fresh gate
        // (Candidate.w == frame) FAILS — no stale-state deformation on miss
        // (ZZMI keeps the payload invalid on miss; t12 issue).
        float frameTag = (bestDepths[0] < 1e30) ? IniParams[150].w : IniParams[150].w - 1.0;
        Candidate[start] = float4(bestHits[0], bestDepths[0], frameTag);
        uint hitZone = (uint)bestHits[0].x;
        ComputeBasis(hitZone, start);
        // R11: Candidate[3] = hit-point screen UV (valid only on real hit);
        // miss writes an invalid anchor so hand_preview falls back to the
        // cursor (hand_preview L79) while anyHit=false keeps the hand hidden.
        bool hitValid = bestDepths[0] < 1e30;
        Candidate[start + 3] = hitValid
            ? float4(bestUV[0], bestDepths[0], 1.0)
            : float4(-10, -10, 0, 0);
    }
}
