// EFMI deformation (independent implementation, TheHerta4, 256-zone sparse
// model). Applies the per-vertex active-zone spring offsets (up to K pairs of
// zone id + weight) to the active vertex set.
//
// Out is a whole-record copy of Base. The first three uint32 words (POSITION)
// are overwritten with the displaced position (Base + delta). The NORMAL
// (words 3-5) / TANGENT (words 6-8) / handedness (word 9) fields are refreshed
// from the **finite-difference Jacobian of the displacement field** whenever
// the vertex actually moves, so shading and the normal-extruded outline shell
// follow the deformation instead of extruding along a stale normal
// (t25: the stale-normal shell punches through sheared regions and shows up as
// a black "shadow" hugging the geometry).
//   · delta == 0 (inactive / mode<2 / nothing dragged)  -> words 3.. are copied
//     bit-exact from Base: the "no drag" path is byte-for-byte the old behaviour.
//   · strideWords < 9 (16B position-only streams)       -> same bit-exact copy.
// NOTE (A1/t13): the full slot0 stream (vb0/vb3) is rebound to Out and the
// native game VS reads NORMAL@offset12 / TANGENT@offset24 from it, so words
// 3-9 MUST always be written — leaving them 0 (RWBuffer zero-init) renders
// zero-normals and darkens the mesh on load.
//
// Binding contract:
//   t0 = source Position SRV (uint view)
//   t1 = ZoneIDs   (uint N x K sparse table)
//   t2 = ZoneWeights (float N x K, normalized)
//   t3 = Active index list
//   t4 = spring State (per instance: 6 public + 256 zones x 4 float4)
//   u0 = output Position UAV (uint view)   t120 = IniParams (auto)
//   IniParams[150].z = spatial instance slot; [155].x = strideWords (4/10).
//   IniParams[163].x = mult_radius（align-t3，ZZMI JIGGLE_MULTIPLIERS.y 同源）
Buffer<uint> Base : register(t0);
Buffer<uint> ZoneIDs : register(t1);
Buffer<float> ZoneWeights : register(t2);
Buffer<uint> Active : register(t3);
Buffer<float4> State : register(t4);
// t47：Candidate 不再参与位移闸门（见 main 里 t47 段）——保留声明与 ini 绑定以维持绑定契约；
// detect 仍写它，simulate 仍读它的命中/仲裁字段。
Buffer<float4> Candidate : register(t5);
// t21/ZZMI parity（fix ①）：逐区物理参数（[z*2].x = 烘焙半径）——运行时距离
// 衰减的球半径（ZZMI ZoneParams 同款）
Buffer<float4> ZoneParams : register(t6);
RWBuffer<uint> Output : register(u0);
Texture1D<float4> IniParams : register(t120);

static const uint PUBLIC_SLOTS = 6;
static const uint MAX_ZONES = 256;
static const uint K = 4;

float3 Read3(uint i) {
    return asfloat(uint3(Base[i], Base[i + 1], Base[i + 2]));
}

void Write3(uint i, float3 value) {
    uint3 bits = asuint(value);
    Output[i] = bits.x;
    Output[i + 1] = bits.y;
    Output[i + 2] = bits.z;
}

// ZZMI ComputeRubberInfluence (rzm_jiggle_interaction.hlsl L325-344): per-vertex
// distance falloff from the grab center — 0 at/outside radius, double-smoothstep
// rounded center, pow(falloffPower). Multiplied into the paint weight so
// vertices far from the grab point move less (dynamic decay, not just the
// baked static gradient).
float ComputeRubberInfluence(float dist, float radius, float falloffPower) {
    if (dist >= radius)
        return 0.0;
    float x = 1.0 - saturate(dist / max(radius, 0.000001));
    // max(...,0) 钳底：消除 X3571（pow 负底数静态告警）；数学上 x/s 本就在 [0,1]
    // （saturate + 双 smoothstep），钳底只是让编译器可证明、并防 NaN 毒化顶点。
    x = pow(max(x, 0.0), max(falloffPower / 1.5, 0.0001));
    float s = x * x * (3.0 - 2.0 * x);
    s = s * s * (3.0 - 2.0 * s);
    return pow(max(s, 0.0), falloffPower);
}

// ===========================================================================
// t25：位移场抽成可复用函数（EvalDelta）——法线/切线有限差分更新的基础
// ===========================================================================
// 原实现把 zone 循环内联在 main 里，delta 只对顶点自身位置求值一次。
// 要让法线跟随形变，必须能在**顶点邻域**上对同一个位移场求值，因此把该场
// 原样抽成 EvalDelta(vertex, p)：语义与内联版逐行等价（同 grabCenter 冻结
// 中心、同 ComputeRubberInfluence 无条件相乘、同 K=4 稀疏对、同越界防护），
// 唯一区别是位置由参数 p 给出而非固定读本顶点 —— 这样 d0/d1/d2 才自洽。
//
// 逐顶点数据在顶点自身处求值：ZoneIDs/ZoneWeights 按 `vertex` 取。
float3 EvalDelta(uint vertex, uint stateBase, float3 p) {
    float3 d = 0;
    [unroll]
    for (uint i = 0u; i < K; ++i) {
        uint zone = ZoneIDs[vertex * K + i];
        if (zone == 0xFFFFFFFFu) {
            break;
        }
        if (zone >= MAX_ZONES) {
            continue;
        }
        float w = ZoneWeights[vertex * K + i];
        float3 grabCenter = State[stateBase + 4].xyz;
        float zoneRadius = ZoneParams[zone * 2].x;
        float multRadius = IniParams[163].x > 0.0 ? IniParams[163].x : 1.0;
        float radius = zoneRadius > 0.0 ? zoneRadius : 0.25 * multRadius;
        float zoneFalloff = ZoneParams[zone * 2].w;
        float falloffPower = zoneFalloff > 0.0
            ? zoneFalloff : max(IniParams[156].x, 0.0001);
        float dist = distance(p, grabCenter);
        w *= ComputeRubberInfluence(dist, radius, falloffPower);
        d += State[stateBase + PUBLIC_SLOTS + zone * 4].xyz * w;
    }
    return d;
}

// 顶点尺度上的有限差分步长（t25）：位移场的空间尺度由 zone 半径决定
// （ZoneParams 烘焙半径或回退 0.25×mult_radius，实机 ~0.25 世界单位，
// 角色局部坐标量级 ~1.0）。eps 需要同时满足：
//   · 足够小 -> 截断误差 O(eps) 可忽略（场在 eps 内近似线性）；
//   · 足够大 -> 不被 float32 在 ~1.0 量级的分辨率（~1.2e-7）淹没。
// 取 eps = 5e-3 = 半径的 ~2%：截断误差量级 ~ eps/radius ≈ 2%，
// 而相对量化噪声 ~ 1.2e-7/5e-3 ≈ 2.4e-5 —— 两侧都有充分余量。
static const float EFMI_FD_EPS = 5e-3;

// 归一化 + 零长度/非有限守卫：失败时回退 fallback（绝不把 NaN 写进 Out）。
float3 SafeNormalize(float3 v, float3 fallback) {
    float len2 = dot(v, v);
    if (!(len2 > 1e-16)) {
        return fallback;
    }
    float inv = 1.0 / sqrt(len2);
    float3 r = v * inv;
    if (any(isnan(r)) || any(isinf(r))) {
        return fallback;
    }
    return r;
}

[numthreads(64, 1, 1)]
void main(uint3 tid : SV_DispatchThreadID)
{
    // t41 F1 全量写域：dispatch = ceil(vertex_count/64)，每线程一顶点——
    // 非 active 顶点（含合并骨骼 stub/远端顶点，实机 ~21%）写 Out = Base
    // 位精确复制，杜绝 Out 未写区域 0 坍缩/撕裂残留；active 顶点写
    // Base + delta。Active 表升序 → 二分判定 O(logN) 每顶点（GPU 并行可忽略）。
    uint activeCount;
    Active.GetDimensions(activeCount);
    uint vertex = tid.x;
    if (vertex >= (uint)IniParams[155].z) {
        return;  // z155 = vertex_count（越界线程退出）
    }
    uint strideWords = (uint)IniParams[155].x;
    uint at = vertex * strideWords;
    uint inst = (uint)IniParams[150].z;
    uint stateBase = inst * (PUBLIC_SLOTS + MAX_ZONES * 4);

    bool active = false;
    {
        uint lo = 0u;
        uint hi = activeCount;
        while (lo < hi) {
            uint mid = (lo + hi) >> 1;
            uint v = Active[mid];
            if (v == vertex) {
                active = true;
                break;
            }
            if (v < vertex) {
                lo = mid + 1u;
            } else {
                hi = mid;
            }
        }
    }

    // t47 根因修复：位移**不得**被「逐帧命中新鲜度」门控。
    // 实机二分（release 档位逐档走；各档之间只差一个机制）：
    //   档 4/3（候选陈旧时继续施加 offset）→ 无黑影
    //   档 2/1/0（回到候选 fresh 门）      → 有黑影
    // 唯一有效的变量就是这里，机制链：
    //   · `PullTowardLimit` 是渐近的，probe 又投的是**已变形**的流
    //     （Draw CL 在 probe 之前就把 vb0/vb3 换成了 Out）⇒ 拖到一定程度
    //     光标必然跑出网格投影 ⇒ detect 判 miss；
    //   · 门一关，`delta` 一帧内归零 ⇒ 网格 snap 回原姿势 ⇒ **黑影**；
    //   · 它不来自任何一次着色（G-Buffer / 主材质 / 描边三个 pass 全部换成
    //     平色都染不上它），所以只能从「位移闸门闭合」这一侧解释；
    //   · 与 ini 侧 Apply CL 去门控是**同一类** bug（见生成器里那段注释：
    //     某一笔的门为 0 ⇒ 该笔读 Base、不经过弹簧 ⇒ 与其它阶段错位）。
    // 为什么可以无门：逐区 offset 就是权威拖拽状态，而且它自己会归零
    // （`efmi_simulate.hlsl` 半隐式弹簧 + 静止清零 `length(next) < 1e-5 &&
    // length(velocity) < 1e-4` ⇒ 精确置 0）。没有东西在拖时 `delta` 恰好为
    // 0，与原版「未拖拽」路径**逐位一致**，位移不可能残留；t8 当年担心的
    // 「释放后位移不消失」现在的保证点在 simulate 侧（tests 里钉住），不在这里。
    // t37-P3 三态模式保持不变：mode 0/1（IniParams[159].x < 2）仍不写位移
    // → delta = 0 → Out = Base 位精确复制。
    // 历史：t38 H5 加的那层 `Candidate.w >= frame-1` 窗（FR-1 放宽过一次）
    // 已被本次实机二分证伪，**不要再加回来**。
    // t25：位移场改为调用 EvalDelta（场语义与下方注释逐行一致——注释描述
    // 的规则现在只存在于 EvalDelta 一处，避免两处漂移）。
    //
    // t21/ZZMI parity（fix ①）：逐顶点运行时距离衰减。抓取中心 =
    // simulate 冻结的 grab center（State[b+4].xyz）+ 该区当前 offset
    // （跟随解算位移）。influence = RubberInfluence(dist 到抓取中心,
    // 烘焙半径, falloffPower) × 烘焙 paint weight——离抓取点越远位移
    // 越小（ZZMI L774 同款）。
    //
    // captain 修正（t18 后续；三次迭代定案）：
    //   · 原实现：影响项写在 `if (grabValid)` 内 ⇒ 松手瞬间整项消失，
    //     w_eff 从「烘焙权重 × 球内衰减」突变为「烘焙权重本身」⇒ **场形突变**
    //     ⇒ 回弹扫过强错切构型 ⇒ 描边外露（用户实测：**只有松手才冒黑影，
    //     不松手时没有**）。
    //   · t17：把 influence 按状态归零 ⇒ w_eff=0 ⇒ 位移瞬间塌陷 ⇒ **回弹消失**
    //     （用户实测：「到极限就瞬间归位」），且黑影依旧 ⇒ 已回退。
    //   · 正解（本处）：影响项**无条件相乘**、状态门整个去掉。
    //     释放时 `grabCenter` 仍是冻结值（`efmi_simulate.hlsl` 释放只把
    //     `State[b+4].w` 置 0，`.xyz` 保留），故 **场形跨释放连续、无突变**；
    //     位移由 `offset` 的半隐式弹簧积分平滑收敛到 0
    //     ⇒ **既有回弹、又无场突变**（两者原本被二选一，这里同时满足）。
    // t24 根因修复（对齐 ZZMI rzm_jiggle_interaction.hlsl:993）：**影响场
    // 的求值中心必须是冻结抓取中心，不得加上本区当前位移**。
    //
    // 原实现（bug）：`grabCenter = State[b+4].xyz + State[...zone*4].xyz`
    // —— 把「该区当前弹簧位移」加进了场心。而位移本身又由该场加权产生
    // （`delta += State[...zone*4].xyz * w`），于是形成**自毁反馈**：
    //   拖得越远 → |offset| 越大 → 场心离 blob 越远 → 每个顶点 dist 越大
    //   → `ComputeRubberInfluence` 衰减（`dist >= radius → 0`）
    //   → w 越小 → 位移反而越小 → 到头来完全不写（Out = Base）。
    // 用户实测症状：**拖到某个极限就停在原地不再跟随**；且黑影随距离
    // **先增后减（山形）**；**调参无用**——可用行程与自毁区间同由
    // |offset|/radius 决定，缩放只能同比例平移。
    // 数值：radius=0.25、maxOffset 0.5 时场心偏 2.0×radius ⇒ 抓取点影响
    // 从 1 衰减到精确 0（完全自毁）。
    //
    // ZZMI 对照（rzm_jiggle_interaction.hlsl:993）：
    //   `float dist = distance(localPos, nextCenter.xyz);`
    //   其中 `nextCenter = sharedCenter`（:885/:741）= **冻结中心**，
    //   不含弹簧位移；位移在 `:999 appliedOffset = nextCurrent.xyz * influence;`
    //   **单独施加** ⇒ 场形不随位移漂移。
    // 本处逐行等价：场心只用冻结的 `State[b+4].xyz`；位移仍按
    // `State[...zone*4].xyz * w`（含 influence）施加。
    //
    // t32-A 参数语义分离：ZoneParams[zone*2].x = 拖拽衰减半径
    // （ssmt_drag_zone.radius 原始值；0 = 继承回退 0.25 ×
    // mult_radius（align-t3，ZZMI screen_state L341 倍率路径），
    // ZZMI rzm_jiggle_interaction L816-818「if (zoneRadius > 0)
    // radius = zoneRadius」替换语义同款——区覆盖不乘 mult_radius）——
    // 不是命中范围/烘焙半径。命中范围由 zones/weights
    // 稀疏表（空物体全矩阵椭球烘焙）决定，与此处无关。
    //
    // t34-P1 ④：falloffPower 由 ZoneParams[zone*2].w 覆盖（>0 生效，
    // 0 = 回退全局档案 IniParams[156].x=1.5，ZZMI JIGGLE_PARAMS.z 同义）
    float3 delta = 0;
    float3 pos = Read3(at);
    // t25：法线/切线有限差分需要 NORMAL@words3-5 + TANGENT@words6-8，
    // 即 strideWords >= 9（40B 流 = 10）。更窄的流（16B = 4）只做位精确拷贝。
    bool normalWritable = strideWords >= 9u;
    if (active && IniParams[159].x >= 2.0) {
        delta = EvalDelta(vertex, stateBase, pos);
    }
    // A1/t13: the whole slot0 stream (vb0/vb3) is rebound to Out, and the
    // native VS reads NORMAL@offset12 / TANGENT@offset24 from that stream.
    // Out is a zero-initialized RWBuffer, so words beyond POSITION must be
    // copied from Base or they stay 0 -> zero-normals / zero-tangents darken
    // the mesh on load. Copy every word after the first 3 (POSITION) verbatim
    // for ALL vertices (active and inactive); only words 0-2 get +delta below.
    //
    // t25 分岔：delta == 0（未 active / mode<2 / 没有东西在拖）或 strideWords < 9
    // → 走下方原样拷贝，**行为与改动前逐位一致**（"未拖拽"路径完全不变）；
    // 仅当真的发生位移且法线/切线字段存在时，才用有限差分刷新它们。
    bool applyNormalUpdate = normalWritable && dot(delta, delta) > 1e-16;

    if (applyNormalUpdate) {
        // ---- t25：位移场的有限差分雅可比 → 新法线/切线 ----
        // 顶点局部正交帧（源自 Base 的 NORMAL/TANGENT，均已验证单位长）：
        //   t = normalize(T)，b = normalize(cross(N, t)) * handedness
        // 对位移场 D(p) 取一阶前向差分：
        //   T' = D(p + eps·t) - D(p)        （切向被拉伸/旋转后的走向）
        //   B' = D(p + eps·b) - D(p)        （副切向的走向）
        //   N' = normalize(cross(T', B') * handedness)
        // 这样 N' 由形变后的两个切向重建，天然与它们正交，无需再显式正交化。
        float3 baseN = Read3(at + 3u);
        float3 baseT = Read3(at + 6u);
        float handedness = asfloat(Base[at + 9u]);
        float hsign = (handedness < 0.0) ? -1.0 : 1.0;

        float3 Tn = SafeNormalize(baseT, float3(1, 0, 0));
        float3 Nn = SafeNormalize(baseN, float3(0, 0, 1));
        // 退化保护必须在 SafeNormalize 之前判定：cross(N,t) 因共线而为 0 时，
        // 若先归一化，SafeNormalize 的 fallback 会把它替换成任意合法向量
        // （(0,1,0)），反而让"共线"看起来正常、有限差分在一个与 Base 无关的
        // 坐标系里求值。故这里先取**原始**叉积长度做判据。
        float3 Braw = cross(Nn, Tn);
        float ortho = length(Braw);
        bool tbnOk = ortho > 1e-4;
        float3 Bn = SafeNormalize(Braw * hsign, float3(0, 1, 0));

        float3 posT = pos;
        float3 posB = pos;
        float3 dT = 0;
        float3 dB = 0;
        if (tbnOk) {
            posT = pos + EFMI_FD_EPS * Tn;
            posB = pos + EFMI_FD_EPS * Bn;
            dT = EvalDelta(vertex, stateBase, posT);
            dB = EvalDelta(vertex, stateBase, posB);
        }
        // 位精确拷贝其余字（word 9 手性、blend 等），再覆写 pos/N/T：
        // 与下方 fallback 路径同一份拷贝循环，保证"未写到的字"来源一致。
        for (uint w = 3u; w < strideWords; ++w) {
            Output[at + w] = Base[at + w];
        }
        Write3(at, pos + delta);
        if (tbnOk) {
            // 切向与副切向的一阶差分（都相对 d0 = delta 度量，位置/法线自洽）
            float3 Tp = (posT + dT) - (pos + delta);
            float3 Bp = (posB + dB) - (pos + delta);
            // 长度守卫：任一切向退化 → 保留 Base 法线/切线（不写 NaN）
            if (dot(Tp, Tp) > 1e-16 && dot(Bp, Bp) > 1e-16) {
                float3 Np = SafeNormalize(cross(Tp, Bp) * hsign, Nn);
                // N' 必须与 Base 法线同侧（防交叉积手性反转把面翻成背面）
                if (dot(Np, Nn) < 0.0) {
                    Np = -Np;
                }
                float3 Tnew = SafeNormalize(Tp, Tn);
                // 写入 NORMAL@words3-5；TANGENT@words6-8 保留原 handedness（word 9
                // 已由上面的拷贝循环写入，不覆写）。
                Write3(at + 3u, Np);
                Output[at + 6u] = asuint(Tnew.x);
                Output[at + 7u] = asuint(Tnew.y);
                Output[at + 8u] = asuint(Tnew.z);
            }
        }
    } else {
        for (uint w = 3u; w < strideWords; ++w) {
            Output[at + w] = Base[at + w];
        }
        Write3(at, pos + delta);
    }
}
