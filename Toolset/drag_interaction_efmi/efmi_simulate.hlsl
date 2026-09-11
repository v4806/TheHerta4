// EFMI per-zone spring state machine (independent implementation, TheHerta4,
// 256-zone model). One Present dispatch integrates spring state for all
// (instance, zone) pairs in parallel: dispatch = 8 instances x 256 zones / 64
// threads, one thread per zone.
//
// align-t3（efmi-zzmi-drag-align）：物理内核移植 ZZMI 半隐式弹簧
// （rzm_jiggle_screen_state.hlsl L320-475）——每区状态机架构保留（EFMI
// 2026-09 D1 已接受设计），积分/目标/释放语义与 ZZMI 逐项一致：
//   - 步长 step = clamp(dt·60·sim_speed, 0.05, max_step)（原作公式 L63-69；
//     取代旧固定 240Hz 子步）；
//   - 积分：velocity *= pow(saturate(damping), step)；
//     next = ClampLength(current + (velocity + (filtered−current)·
//     (spring·step))·step, maxOffset)（L408-411；取代临界阻尼 ω=√k 模型）；
//   - 目标平滑：filtered 滤波（follow=0.12 + targetVelocity 前馈 0.35·step，
//     L367-379）——filtered/prevFiltered 随区持久；
//   - 深度拉扯 depth_pull：|2D 拖拽| 比例 × 冻结表面法线（L359-364；取代旧
//     定值 Y 偏移 ±0.025/0.016）——法线 = 本区烘焙 gizmo 法线（GizmoNormals，
//     ZZMI 抓取冻结命中法线的 EFMI 静态等价，绑定姿态空间与拖拽基同系）；
//   - 释放踢 ×1.10（槽位可调，L382-383）+ 释放动态阻尼（boost 1.05 按释放
//     拉伸强度 lerp、decay 0.92 逐帧回落，跨帧持久于区槽，L385-406）；
//   - dragScale 1.00 / mult_radius / mult_spring / mouse 方向（JIGGLE_PARAMS.
//     w / JIGGLE_MULTIPLIERS.y/.w / JIGGLE_MULT_EXTRA.y/.z 同源槽位）。
//
// Per-instance State layout (float4 records, stride = 6 + 256*4 = 1030):
//   [b+0] = capture (cursor.xy, zone id, button)
//   [b+1] = frozen screen-right/local basis (xyz, valid)
//   [b+2] = frozen screen-down/local basis (xyz, valid)
//   [b+3] = timing (lastTime, held, lastSeen, magic)
//   [b+4] = frozen grab center (local xyz, valid in w)   <- t21/ZZMI parity
//   [b+5] = 共享抓取信息 (held, maxOffset, stretchFraction, 0)——ZZMI
//           InteractionState[8] 同语义；由抓取中的区线程写、zone0 线程在
//           松开时清（互斥由 held 保证）；手型抓手倾斜/振动的驱动源
//   [b+6 + zone*4+0] = current offset of the zone (.w = 上一帧抓取锁存)
//   [b+6 + zone*4+1] = previous offset (.w = 上一帧 step；速度由
//                      (current−previous)/step 派生——ZZMI 位置派生速度同
//                      语义，ClampLength 事件自然回馈)
//   [b+6 + zone*4+2] = filtered 平滑目标 (.w = 上一帧 targetStep)
//   [b+6 + zone*4+3] = prevFiltered (.w = 释放动态阻尼倍率跨帧持久，
//                      ZZMI inputFlags.w 等价槽)
//
// Candidate per instance (stride 4 float4):
//   [b+0] = strongest hit (zone id 0-255, weight, reverse depth, frame tag)
//   [b+1] = right basis   [b+2] = down basis   [b+3] = anchor screen pos
RWBuffer<float4> State : register(u0);
Buffer<float4> Candidate : register(t0);
// t34-P1: per-zone physics overrides（ZZMI Zone*Override 同款——0 = 继承全局回退）：
//   ZoneParams[z*2].y   = strength（拖拽强度覆盖）
//   ZoneParams[z*2].z   = max_offset（拖拽最大位移覆盖）
//   ZoneParams[z*2+1].x = damping（阻尼倍率覆盖，ZZMI ZoneDampingOverride 语义）
Buffer<float4> ZoneParams : register(t1);
Buffer<float4> Centers : register(t2);
// align-t3：每区烘焙表面法线（depth_pull 冻结法线源）
Buffer<float4> GizmoNormals : register(t3);
// t51：锚点投影 RT —— 行 `zone*8+0` = 该区**中心**的投影 clip（探针每帧投
// 「区中心 + 三轴端」）。capture 时 grab center 的参考屏幕 uv 取自这一行；
// 与 efmi_hand_preview.hlsl 的 dragCols（同 RT 行 0-3）同源同式。
Texture2D<float4> Anchors : register(t4);
Texture1D<float4> IniParams : register(t120);

static const float SPRING_MAGIC = 7351.0;
static const float MAX_DT = 0.25;
static const float SEEN_TIMEOUT = 0.20;
static const float STATE_TIMEOUT = 0.25;
static const uint PUBLIC_SLOTS = 6;
static const uint MAX_ZONES = 256;
static const uint ZONE_STRIDE = 4;

float SafePositive(float v, float fallback) { return v > 0.0 ? v : fallback; }

// t51：与 efmi_detect.hlsl / efmi_hand_preview.hlsl 逐字同式的 clip→screen uv
// （top-down，含 minus-Y NDC 翻转；命中判定用的就是这一式，故三者可比）。
float2 ClipToScreenUV(float4 c) {
    return float2(.5 + .5 * c.x / c.w, .5 - .5 * c.y / c.w);
}
float SafeNonZero(float v, float fallback) { return abs(v) > 1e-8 ? v : fallback; }

float3 ClampLength(float3 v, float maxLen) {
    float n = length(v);
    return v * min(1.0, maxLen / max(n, 1e-9));
}

// ZZMI PullTowardLimit (rzm_jiggle_screen_state.hlsl L125-133): progressive
// resistance — raw drag approaches maxLen asymptotically (1-exp), instead of
// a hard clip, so pull feels stretch-limited rather than walled.
float3 PullTowardLimit(float3 dragDir, float maxLen) {
    float dragLen = length(dragDir);
    if (dragLen < 0.000001 || maxLen <= 0.0)
        return float3(0.0, 0.0, 0.0);
    float pulledLen = maxLen * (1.0 - exp(-dragLen / maxLen));
    return dragDir * (pulledLen / dragLen);
}

[numthreads(64, 1, 1)]
void main(uint3 tid : SV_DispatchThreadID)
{
    uint zone = tid.x % MAX_ZONES;
    uint inst = tid.x / MAX_ZONES;
    uint b = inst * (PUBLIC_SLOTS + MAX_ZONES * ZONE_STRIDE);

    float now = IniParams[153].x;
    float frame = IniParams[150].w;
    bool enabled = IniParams[153].y > .5;
    bool modifier = IniParams[151].z > .5 && enabled;
    int buttons = (int)IniParams[151].w;
    bool held = modifier && buttons != 0;

    // Public slots are owned by thread zone 0 of each instance; all threads
    // read them, only zone 0 writes timing/capture on cold-start.
    float4 clock = State[b + 3];
    float4 capture = State[b + 0];
    float4 candidate = Candidate[inst * 4];
    bool fresh = candidate.w == frame;

    uint z = zone;
    uint slot = b + PUBLIC_SLOTS + z * ZONE_STRIDE;
    float3 x = State[slot].xyz;
    float3 xPrev = State[slot + 1].xyz;
    float prevStep = clamp(SafePositive(State[slot + 1].w, 1.0), 0.05, 8.0);
    float3 filtered = State[slot + 2].xyz;
    float prevTargetStep = clamp(SafePositive(State[slot + 2].w, 1.0), 0.05, 8.0);
    float3 prevFiltered = State[slot + 3].xyz;
    float releaseDampingMultPersist = State[slot + 3].w;

    // t34-P1: per-zone physics overrides（ZZMI rzm_jiggle_interaction L816-824 /
    // rzm_jiggle_screen_state L92-103 同款；0 = 继承全局回退）。
    // 注意结构差异：EFMI 每 (inst,zone) 线程独立弹簧，override 按**本线程自己的
    // zone** 读，不照搬 ZZMI 的全局 activeZone。
    float zoneStrength   = ZoneParams[z * 2].y;
    float zoneMaxOffset  = ZoneParams[z * 2].z;
    float zoneDampingMult = ZoneParams[z * 2 + 1].x;
    // t36-P5: grabbable 槽（ZoneParams[z*2+1].y，ZZMI ZoneGrabbable 同源——
    // rzm_jiggle_screen_state L78-83；EFMI 无 LMB+RMB 手势组合概念 →
    // grabbable=0 直接拒绝本线程抓取）。烘焙权重已保留（bake_zone_ball t36-P5），
    // 不可抓区仍可命中/显示，仅运行时拒绝 grabbing。
    float zoneGrabbable  = ZoneParams[z * 2 + 1].y;
    // t42 P-1: strength 全局倍率 mult_strength（IniParams[161].x，ZZMI
    // JIGGLE_MULTIPLIERS.z 同源，默认 0.333）——ZZMI screen_state L338
    // `strength = baseStrength * mult_strength` 同款：乘在**最终** strength 上
    // （EFMI strength 是主路径乘数，§7 P-6）。0 或未发射 → 回退 1.0（ZZMI
    // SafePositive 语义）。base 回退 = IniParams[152].y（ZZMI JIGGLE_PARAMS.y
    // 同源，回退档案 1.00）。
    float multStrength = IniParams[161].x > 0.0 ? IniParams[161].x : 1.0;
    float baseStrength = SafePositive(IniParams[152].y, 1.0);
    float strength = (zoneStrength > 0.0 ? zoneStrength : baseStrength) * multStrength;
    // align-t3：maxOffset = 显式基线（153.z = 0.50）× mult_radius（163.x，
    // ZZMI screen_state L342 `*mult_radius`）；逐区覆盖替换（ZZMI
    // ZoneOffsetOverride L343-345：>0 替换、不再乘 mult_radius）。
    float multRadius = IniParams[163].x > 0.0 ? IniParams[163].x : 1.0;
    float multSpring = IniParams[163].y > 0.0 ? IniParams[163].y : 1.0;
    float maxOffset  = zoneMaxOffset > 0.0 ? zoneMaxOffset : IniParams[153].z * multRadius;
    // t42 P-2: damping override 改**替换**语义（ZZMI screen_state L332-333 同款：
    // `globalDampingMult = override > 0 ? override : JIGGLE_MULT_EXTRA.x`）——
    // override>0 时用 override 值**替换**全局 mult_damping（IniParams[160].x，
    // 由生成器独立发射，不再烘焙进 [154]），否则用全局倍率；0 或未发射 →
    // 回退 1.0。base 阻尼（[154].x/.z）保持独立，无双重乘。
    float globalDampingMult = IniParams[160].x > 0.0 ? IniParams[160].x : 1.0;
    float dampingMult = zoneDampingMult > 0.0 ? zoneDampingMult : globalDampingMult;

    // Hit gate (t26 F1): zone id 0 IS a valid zone, so the miss sentinel can
    // never be encoded as x == 0. A candidate is a hit only when its weight
    // clears the threshold (same gate as shapekey_drive realHit).
    bool candidateHit = fresh && candidate.y >= IniParams[152].w;

    // Cold start / STATE_TIMEOUT / disabled -> full rearm: each thread clears
    // its own zone slots (current/previous/filtered/prevFiltered) in addition
    // to the public slots cleared by the zone-0 thread (t26 F2). The reset
    // condition only reads the stale clock, so cross-group threads stay
    // consistent without barriers.
    bool coldStart = clock.w != SPRING_MAGIC || now - clock.x > STATE_TIMEOUT || !enabled;
    if (coldStart) {
        if (zone == 0u) {
            State[b + 0] = 0;
            State[b + 1] = 0;
            State[b + 2] = 0;
            State[b + 3] = float4(now, held ? 1 : 0, fresh ? now : -100, SPRING_MAGIC);
            State[b + 4] = 0;   // invalidate grab center
            State[b + 5] = 0;   // 共享抓取信息（held/maxOffset/stretch/—）
            clock = State[b + 3];
            capture = State[b + 0];
        }
        State[slot] = 0;
        State[slot + 1] = 0;
        State[slot + 2] = 0;
        State[slot + 3] = 0;
        x = 0;
        xPrev = 0;
        filtered = 0;
        prevFiltered = 0;
        prevStep = 1.0;
        prevTargetStep = 1.0;
        releaseDampingMultPersist = 0;
    }
    // t19 锁定抓取（locked capture）——对齐 ZZMI
    //   rzm_jiggle_screen_state.hlsl:445 `captureStateOut = lockedCapture ? 1.0 :
    //   (isCharging ? 0.5 : 0.0)`：**真正抓取期间状态锁定 1.0，不依赖每帧命中**。
    //
    // 机制：`held` 只反映「修饰键 + 按键」；`State[b+4].w` 是抓取中心有效旗标
    // （真正抓到过才为 1）。两者同时成立 = 正在**锁定抓取**。
    // 关键区别（要求 2：不得把「未命中」与「未抓取」混为一谈）：
    //   · **未抓取**（没抓到过，State[b+4].w==0，典型是蓄力）→ 走原超时释放，
    //     `SEEN_TIMEOUT` 语义**完全不变**；
    //   · **已锁定抓取**（hysteresis 已建立）→ 只要按键仍按住，就**不因失命中**
    //     而释放。原实现在这里会于光标移出模型投影 0.2s 后强制 `held=false`
    //     ⇒ grabbing 断 ⇒ 位移停 ⇒ 模型弹回（用户实测「移得足够远就拖拽失效」）。
    bool lockedCapture = held && State[b + 4].w > 0.5;

    if (zone == 0u) {
        if (fresh) {
            clock.z = now;
        }
        // 仅在**非**锁定抓取时保留原超时释放语义；锁定期间按住不放则不释放
        if (!lockedCapture && now - clock.z > SEEN_TIMEOUT) {
            held = false;
        }
    }

    // Cross-instance winner arbitration (same semantics as before).
    bool winner = candidateHit;
    if (winner) {
        for (uint k = 0u; k < 8u; ++k) {
            float4 other = Candidate[k * 4];
            if (k != inst && other.w == frame && other.y >= IniParams[152].w &&
                (other.z < candidate.z || (other.z == candidate.z && k < inst))) {
                winner = false;
            }
        }
    }

    if (zone == 0u && held && clock.y < .5 && winner) {
        float4 right = Candidate[inst * 4 + 1];
        float4 down = Candidate[inst * 4 + 2];
        if (right.w > .5 && down.w > .5) {
            uint gz = (uint)max(candidate.x, 0.0);
            // t36-P5: 拒绝门（grabbable=0 → 不锁存捕获 → 不可抓）。读**候选区**
            // 的 grabbable 槽（ZoneParams[gz*2+1].y）；越界守卫回退可抓。
            float candidateGrabbable = (gz < 256u) ? ZoneParams[gz * 2 + 1].y : 1.0;
            if (candidateGrabbable > 0.5) {
                State[b + 0] = float4(IniParams[151].xy, candidate.x, buttons);
                State[b + 1] = right;
                State[b + 2] = down;
                capture = State[b + 0];
                // t51 根因修复：grab center 的**参考 uv 必须是区中心的屏幕 uv**，
                // 不是命中点的。原实现取 `Candidate[inst*4+3].xy`，而 R11 起该槽
                // 是**命中点**屏幕 uv（detect `bestUV = ToScreen(hitClip)`）；命中
                // 判定的语义正是「命中点投影 ≈ 光标」⇒ `cuv - anchorUV ≈ 0` ⇒
                // grabCenter 塌回 `Centers[gz]`（区中心、常量）⇒ deform 的
                // `ComputeRubberInfluence` 球（半径 ZoneParams[z*2].x）永远罩在区
                // 中心 ⇒ 无论抓哪里，动的都是同一块最高权重 patch。旧场景区小
                // （~500 顶点）时球罩满整区故不可见；单大区（27983/63805 顶点）
                // 时立刻显形。
                // 参考 uv 来源 = 锚点投影 RT 行 `gz*8+0`（区中心 clip，探针每帧投，
                // 与 hand_preview 的 dragCols 同源）；锚点未投影（w<=1e-5）时退化为
                // offset=0（= 旧行为），不会写出 NaN。
                float3 anchorLocal = (gz < 256u) ? Centers[gz].xyz : 0.0;
                float2 cuv = IniParams[151].xy;
                if (IniParams[153].w < 0.5)     // t52：与 detect 同一开关、同一约定
                    cuv.y = 1.0 - cuv.y;        // 原始光标 bottom-up → 翻成 top-down
                float4 zcClip = Anchors.Load(int3(gz * 8u, 0, 0));
                float2 zcUV = zcClip.w > 1e-5 ? ClipToScreenUV(zcClip) : cuv;
                float3 grabCenter = anchorLocal
                    + right.xyz * (cuv.x - zcUV.x)
                    + down.xyz * (cuv.y - zcUV.y);
                State[b + 4] = float4(grabCenter, 1.0);
                State[b + 5] = 0;
            }
        }
    }
    if (zone == 0u && !held) {
        // t45 X4596：typed UAV stores must write all declared components——
        // 单分量写 .z = 0 编译失败（simulate 永不运行 → 拖拽完全无效）；
        // 全分量写保持语义（z = capture zone 清零）。
        State[b + 0] = float4(State[b + 0].xy, 0, State[b + 0].w);
        State[b + 4] = float4(State[b + 4].xyz, 0);   // invalidate grab center on release
        State[b + 5] = 0;   // 清共享抓取信息（stretchFraction 随释放归零）
        capture = State[b + 0];
    }
    if (zone == 0u) {
        State[b + 3] = float4(now, held ? 1 : 0, clock.z, SPRING_MAGIC);
    }

    // align-t3：ZZMI 步长模型（SimulationStep，rzm_jiggle_screen_state
    // L63-69）——step = clamp(dt·60·sim_speed, 0.05, max_step)；sim_speed/
    // max_step 经 IniParams[164].x/.y（$ssmtdrag_efmi_sim_speed/max_step
    // 全局，默认 3.0/3.0）逐帧可调；dt = 帧间隔（clock.x = 上帧时间）。
    float dt = clamp(now - clock.x, 0, MAX_DT);
    float simSpeed = SafePositive(IniParams[164].x, 3.0);
    float maxStep = SafePositive(IniParams[164].y, 3.0);
    float step = clamp(dt * 60.0 * simSpeed, 0.05, maxStep);

    // t36-P5: grabbable 运行时抓取拒绝门（本线程自己的 zone）——grabbable=0 →
    // grabbing=false（即使已锁存捕获也不驱动弹簧）；与捕获锁存门双保险。
    // t37-P3 三态模式：mode 1（仅命中不拖拽，IniParams[159].x=1）→ grabbing=
    // false（detect/hand 照跑，弹簧不驱动）；mode 0 由 probe 门控挡掉无 Candidate。
    bool grabbing = held && capture.z == (float)z && zoneGrabbable > 0.5
                    && IniParams[159].x >= 2.0;

    // ---- 拖拽目标（ZZMI L324-365 语义）----
    float3 rawTarget = 0;
    if (grabbing) {
        // t21/ZZMI parity (fix ③): symmetric pixel-normalized drag delta.
        // screenReference = min(res_w,res_h); cursor is 0..1 uv, so
        // delta*screenSize = pixel delta; /min -> px/min both axes——与 ZZMI
        // GetScreenDragNormalized（像素域 ÷min）数值一致。
        float2 screenSize = max(IniParams[155].xy, float2(1.0, 1.0));
        float screenReference = max(min(screenSize.x, screenSize.y), 1.0);
        // t14 修复（问题 1）：鼠标竖向拖拽方向反号。
        // 实测：鼠标**向上**拖 → 模型**向下**走；左右完全正常（纯单轴符号错）。
        // 成因：`delta` 直接吃 `IniParams[151].xy`（top-left/downward 的归一化
        // 光标，y 向下为正），而拖拽基 `State[b+1]/[b+2]`（detect `ComputeBasis`
        // 输出）是**经 `ToScreen` 的 `0.5 - 0.5*clip.y/clip.w` 翻转后**的屏幕
        // 速度向量——即基向量已经含一次 Y 翻转，而 `delta` 没有。两条链在 X 上
        // 一致（`ToScreen` 的 X 无翻转），只在 Y 上差一个负号，故表现为「竖向
        // 反号、横向正常」。此处只修正 Y 分量，X 分量不动。
        float2 delta = (IniParams[151].xy - capture.xy) * screenSize / screenReference;
        delta.y = -delta.y;
        // align-t3：鼠标方向倍率（ZZMI L327：mouseXDir=73.y / mouseYDir=71.z
        // → EFMI 163.w/162.y）
        delta *= float2(SafeNonZero(IniParams[163].w, 1.0), SafeNonZero(IniParams[162].y, 1.0));
        float dragScale = SafePositive(IniParams[152].z, 1.0);
        // align-t3：深度拉扯 depth_pull（ZZMI L359-364）——|2D 拖拽| 比例 ×
        // 冻结表面法线（取代旧定值 Y 偏移 LMB −0.025 / RMB +0.016）。
        // 法线 = 本区烘焙 gizmo 法线（GizmoNormals[z]，绑定姿态空间与拖拽基
        // 同系；ZZMI 抓取时冻结命中法线的静态等价——区法线静态 ⇒ 读取即冻结）。
        // 按钮方向：LMB（buttons==1）= 沿 +法线拉出（朝向相机）、RMB（==2）
        // = 反号压入（ZZMI 组合抓取恒 +；EFMI 无组合概念，保留双键方向语义）。
        float depthPullMult = SafePositive(IniParams[163].z, 0.5);
        float dragDistance2D = length(delta);
        float3 grabNormalUnit = 0.0;
        float4 gn = GizmoNormals[z];
        if (gn.w > 0.5 && length(gn.xyz) > 1e-6)
            grabNormalUnit = normalize(gn.xyz);
        float buttonSign = buttons == 2 ? -1.0 : 1.0;
        bool basisValid = State[b + 1].w > 0.5 && State[b + 2].w > 0.5;
        float3 rawDrag = basisValid
            ? (State[b + 1].xyz * delta.x + State[b + 2].xyz * delta.y
               + grabNormalUnit * (dragDistance2D * depthPullMult * buttonSign)) * dragScale
            : float3(0.0, 0.0, 0.0);
        // Progressive resistance toward maxOffset (ZZMI PullTowardLimit)；
        // t34-P1: strength rate 由本线程 zone 的 ZoneParams[z*2].y 覆盖（>0 生效，
        // 0 = 回退 JIGGLE_PARAMS.y=1.0，ZZMI baseStrength 默认同义）；maxOffset
        // 同样逐区覆盖。
        rawTarget = PullTowardLimit(rawDrag * strength, maxOffset);
    }

    // ---- 目标平滑滤波（ZZMI L367-379；align-t3 新增——follow=0.12 是
    // ZZMI 手感的关键缺项（2026-09 B4 保留项随目标②一并替换））----
    float follow = saturate(SafePositive(IniParams[162].z, 0.12));
    float3 targetVelocity = (filtered - prevFiltered) / prevTargetStep;
    prevFiltered = filtered;
    float targetFollow = grabbing ? follow : follow * 0.55;
    float targetFollowStep = 1.0 - pow(saturate(1.0 - targetFollow), step);
    filtered = ClampLength(filtered + targetVelocity * (0.35 * step)
                           + (rawTarget - filtered) * targetFollowStep, maxOffset);

    // ---- 半隐式弹簧积分（ZZMI L408-411；速度由 (current−previous)/step
    // 派生——ClampLength 钳位事件自然回馈速度，与 ZZMI 位置派生同语义）----
    float3 velocity = (x - xPrev) / prevStep;
    // 释放踢（ZZMI L382-383）：释放首帧保留速度 ×release_kick（槽位 162.x
    // 可调，默认 1.10；State[slot].w 锁存上一帧 grabbing）
    bool prevGrabbing = State[slot].w > 0.5;
    bool justReleased = prevGrabbing && !grabbing;
    if (justReleased && length(velocity) > 1e-6) {
        velocity *= SafePositive(IniParams[162].x, 1.10);
    }

    // 释放动态阻尼（ZZMI L385-406）：释放瞬间按拉伸强度 lerp(稳态, boost
    // 1.05, intensity)（释放越狠越弹），随后 pow(decay 0.92, step) 衰减回
    // 稳态；跨帧持久于区槽 [slot+3].w（ZZMI inputFlags.w 等价）。
    float steadyDampingMult = SafeNonZero(dampingMult, 1.0);
    float releaseDampingMult = releaseDampingMultPersist != 0.0
        ? releaseDampingMultPersist : steadyDampingMult;
    if (justReleased)
    {
        float releaseIntensity = maxOffset > 0.0 ? saturate(length(x) / maxOffset) : 0.0;
        releaseDampingMult = lerp(steadyDampingMult,
                                  SafePositive(IniParams[165].x, 1.05), releaseIntensity);
    }
    else if (!grabbing)
    {
        float decayRetain = saturate(SafePositive(IniParams[165].y, 0.92));
        releaseDampingMult = steadyDampingMult
            + (releaseDampingMult - steadyDampingMult) * pow(decayRetain, step);
    }

    float damping = grabbing
        ? saturate(SafePositive(IniParams[154].x, 0.86) * dampingMult)
        : saturate(SafePositive(IniParams[154].z, 0.96) * releaseDampingMult);
    float spring = grabbing
        ? SafePositive(IniParams[154].y, 0.176) * multSpring
        : SafePositive(IniParams[154].w, 0.055) * multSpring;
    velocity *= pow(saturate(damping), step);
    float3 next = ClampLength(x + (velocity + (filtered - x) * (spring * step)) * step,
                              maxOffset);

    // align-t3：共享抓取信息（ZZMI InteractionState[8] 同语义）——抓取中的
    // 区线程写 (held, maxOffset, stretchFraction, 0)；zone0 线程在 !held 时清
    // （与捕获互斥：grabbing 要求 held，无竞争）。手型抓手倾斜/振动经
    // hand_preview 读 [b+5].z。
    if (grabbing) {
        float stretchFraction = maxOffset > 0.0 ? saturate(length(next) / maxOffset) : 0.0;
        State[b + 5] = float4(1.0, maxOffset, stretchFraction, 0.0);
    }

    // 静止清理（EFMI 既有语义）：非抓取且位置/速度均收敛 → 精确归零。
    if (!grabbing && length(next) < 1e-5 && length(velocity) < 1e-4) {
        next = 0;
        velocity = 0;
        filtered = 0;
        prevFiltered = 0;
    }
    State[slot] = float4(next, grabbing ? 1.0 : 0.0);
    State[slot + 1] = float4(x, step);
    State[slot + 2] = float4(filtered, step);
    State[slot + 3] = float4(prevFiltered, releaseDampingMult);
}
