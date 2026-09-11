// EFMI Present hand — ZZMI rzm_jiggle_hand.hlsl (343 行) 机制移植
// (efmi-zzmi-drag-align t3, TheHerta4). 渲染链 H1-H16 与 ZZMI 一致：
// 三轴表面基 + 轴重映射 (-y,-z,x) + 透视尺寸钳制 + 直立防翻 + LMB/RMB
// 蓄力倾斜 + 抓取拉伸倾斜 (stretchFraction) + 拉伸振动 + 表面贴合软裁剪 +
// 假光照 + 反壳描边 + persist 参数面。
//
// 资产为共享手部网格（P3F_C4F，与 ZZMI 字节级相同，导出时拷贝）。
//
// 坐标域（fork 差异吸收点，基准 §6.1 保留项）：HandPreview 记录由
// efmi_hand_preview.hlsl 在出口处完成 [0,1] top-down uv → ZZMI 约定的
// Y-up px 换算——本着色器内部坐标域与 ZZMI 完全一致（anchor = Y-up px，
// NDC = p/screen*2-1 无翻转），**不涉及也不改变 [Present] 的 cursor 注入
// 公式（$cursorX=cursor_x / $cursorY=cursor_y 双直用，禁触区）**。
//
// Bindings:
//   t67 = ResourceEFMIDragHandPreview_{ns}  (4×float4：
//         [0] anchor.xy(Y-up px)+screen.zw / [1] gizmo 法线.xy + (−副切线).xy 投影 /
//         [2] gizmo 切线.xy 投影 + z=基有效 + w=状态(0 隐藏/1 悬停或蓄力/2 抓取) /
//         [3] x=诊断状态码 y=蓄力旗标 z=抓取旗标 w=stretchFraction)
//         —— t28（手型整体旋转 90°：底朝下）：axisY 槽喂「沿表面下方向」的
//         −副切线、axisZ 槽喂切线（水平宽度）⇒ 手指(−hand.y) 沿 +副切线 = 屏幕
//         上、手型底(+hand.y) 朝屏幕下。轴序理由见 efmi_hand_preview.hlsl 的 t28 说明。
//         —— ZZMI CursorPreview 契约；stretchFraction 打包进 [3].w
//         （ZZMI 读 ScreenState[8].z；EFMI 每实例×区弹簧无单共享态，
//         由预览 CS 从获胜实例 State[b+5].z 拷贝）
//   t69 = ResourceEFMIDragHand{Action|NoAction}Normal_{ns} (baked smooth normal)
//   t120 = IniParams (auto；EFMI 高位区映射 ZZMI 83/89-95/98 参数面):
//     [187] = SURFACE_PROXY（x=clip 1 / y=lift 0.018 / z=softness 0.020）
//     [188] = HAND_CENTER（重定心偏移，包围盒中心）
//     [189] = HAND_SCALE（x=缩放 0.5 / y=不透明度 1.0）
//     [190] = HAND_STATE（x=LMB 归一蓄力 / y=time / z=RMB 归一蓄力）
//     [191] = HAND_TILT（x=LMB min 10 / y=RMB min 10 / z=LMB max 45 / w=RMB max 45）
//     [192] = HAND_VIBRATE（x=阈值 0.5 / y=慢周期 0.8 / z=快周期 0.1 / w=幅度 4.0）
//     [193] = HAND_UPRIGHT（x=直立防翻 80° / z=抓取拉伸倾斜 max 45°）
//     [194] = HAND_REFERENCE（x=参考高 2160）
//     [195] = HAND_OUTLINE（x=描边宽 2.0 / y=描边段旗标 / z=描边不透明度 1.0）
//     [196] = HAND_SHADE（生成器不发射 → shader 兜底光向 0.4/0.6/0.65 +
//             ambient 0.55，与 ZZMI 生成器不发射 96 同规）
Buffer<float4> Preview : register(t67);
// Baked smooth per-vertex normal (see HandNoAction_Normal.buf/HandAction_Normal.buf):
// this mesh's real VB has no normal attribute and no shared vertices (each
// triangle owns unique verts), so this was precomputed offline by averaging
// face normals across vertices that sit at the same 3D position — restoring
// the topology the flat export discarded. Index-aligned with vb0/POSITION.
Buffer<float3> SmoothNormal : register(t69);
Texture1D<float4> IniParams : register(t120);

struct VSIn
{
    float3 position : POSITION;
    float4 color : COLOR0;
};

struct VSOut
{
    float4 pos : SV_Position;
    float4 color : COLOR0;
    float surface : TEXCOORD1;
    float hit : TEXCOORD2;
    float3 normal : TEXCOORD3;
};

#define SURFACE_PROXY IniParams[187]
// .xyz = offset (in the remapped `hand` local space below) that recenters
// the mesh's authored bounding-box middle onto the cursor, instead of the
// mesh's local origin (which sits near the wrist, not the palm center).
#define HAND_CENTER IniParams[188]
// .x = overall hand visual scale multiplier, fallback 0.65.
// .y = overall opacity multiplier, fallback 1.0 (solid).
#define HAND_SCALE IniParams[189]
// .x = LMB-alone hold progress (normalized 0..1 by the Present windup
//      reduction), 0 when not lone-holding LMB. .y = time in seconds, for the
//      vibration phase. .z = same hold progress but for RMB-alone.
#define HAND_STATE IniParams[190]
// .x = LMB windup min angle deg (floor at press) .y = RMB min
// .z = LMB windup max angle deg (around local Z) .w = RMB max (around local X)
#define HAND_TILT IniParams[191]
// .x = stretch fraction where vibration starts .y = slow shake period (s)
// .z = fast shake period at 100% stretch .w = peak screen-pixel jitter amplitude
#define HAND_VIBRATE IniParams[192]
// .x = max degrees the hand's up direction may roll away from vertical before
//      being clamped back. .z = grab tilt's own max angle in degrees (shares
//      HAND_TILT.x as its floor at zero stretch).
#define HAND_UPRIGHT IniParams[193]
// .x = screen height (px) the size clamp below was tuned at, fallback 2160.
#define HAND_REFERENCE IniParams[194]
// .x = outline width px at the reference resolution, fallback 2.0.
// .y = pass flag (>0.5 = this draw is the enlarged flat outline shell).
// .z = outline opacity multiplier, fallback 1.0.
#define HAND_OUTLINE IniParams[195]
// .xyz = fake-shading light direction in the mesh's authored local space;
// .w = ambient floor 0..1. Not emitted by the generator (ZZMI same) — the
// shader fallback below applies (0.4/0.6/0.65 + 0.55).
#define HAND_SHADE IniParams[196]

float SafePositive(float v, float fallback) { return v > 0.0 ? v : fallback; }
// Like SafePositive but keeps a negative value instead of discarding it —
// used for the tilt angle, where the sign is a deliberate direction flip.
float SafeNonZero(float v, float fallback) { return abs(v) > 1e-6 ? v : fallback; }

#ifdef VERTEX_SHADER
void main(VSIn i, uint vid : SV_VertexID, out VSOut o)
{
    float4 anchor = Preview[0];
    float2 screen = max(anchor.zw, float2(1.0, 1.0));
    float2 pixelScale = float2(screen.x, -screen.y);

    // These are screen pixels per one unit in the detector's local space.
    // Gizmo axes（ZZMI BuildGizmoAxes 构造）：X=表面法线（掌心朝向）、
    // Y=沿表面「下」方向（手指 = −Y ⇒ 朝上）、Z=切线（宽度）——EFMI 侧由生成器
    // 按区烘焙、锚点探针逐帧投影、预览 CS 供给（槽序为 EFMI「手型轴序」，见
    // efmi_hand_preview.hlsl 的 t28 说明；ZZMI 原槽序把水平切线喂给 Y，手型
    // 只会侧躺，EFMI 已改为副切线）。
    float2 axisX = Preview[1].xy * pixelScale;
    float2 axisY = Preview[1].zw * pixelScale;
    float2 axisZ = Preview[2].xy * pixelScale;
    bool validBasis = Preview[2].z > 0.5 && Preview[2].w > 0.5 &&
        (dot(axisX, axisX) + dot(axisY, axisY) + dot(axisZ, axisZ)) > 1e-6;

    // Keep each screen direction but use a single bounded scale. Raw axis
    // lengths are unreliable on a stale/fallback hit and used to explode the
    // hand into long triangles. Their shared magnitude remains our depth cue.
    // The clamp bounds are resolution-scaled (reference height 2160) so the
    // hand keeps the same relative size at any resolution.
    float referenceHeight = SafePositive(HAND_REFERENCE.x, 2160.0);
    float resolutionScale = screen.y / referenceHeight;
    float lenX = max(length(axisX), 1e-5);
    float lenY = max(length(axisY), 1e-5);
    float lenZ = max(length(axisZ), 1e-5);
    float perspectiveScale = clamp((lenX + lenY + lenZ) / 3.0, 55.0 * resolutionScale, 300.0 * resolutionScale);
    axisX /= lenX;
    axisY /= lenY;
    axisZ /= lenZ;

    float uprightMaxDeg = clamp(SafePositive(HAND_UPRIGHT.x, 80.0), 1.0, 179.0);
    float uprightMaxRad = radians(uprightMaxDeg);
    float rawRollAngle = atan2(axisY.x, -axisY.y);

    // Gizmo 帧的 axisY 槽 = 手型投影的「下」端（手指在 −axisY ⇒ 朝上），t28 起
    // 由**副切线**（= localRef 在切平面内的投影，即沿表面上方向，取负成「下」）
    // 供给：身体正背面共享轴时真实表面法线合法反号会让*切线*整转半圈（构造
    // 伪影而非滚转），副切线是该三元积的不动点、两面对称——槽内不再有该半圈
    // 伪影，正常姿态下 rawRollAngle≈0（防翻静默）。保留本守卫仅防极端姿态：
    // 真出现 |roll|>90° 时只翻 axisY（拇指/宽度轴不动；整帧回转会抵消掌心
    // 法线轴自身的正确前后翻转）。
    if (abs(rawRollAngle) > radians(90.0))
    {
        axisY = -axisY;
        rawRollAngle = atan2(axisY.x, -axisY.y);
    }

    // Remaining roll clamp: genuine steep/curved-geometry lean is a true
    // continuous rotation of the whole surface-attached frame, so this DOES
    // rotate all three axes together -- snap to the mirrored lean once past
    // the threshold instead of pinning at the max angle while the surface
    // keeps rotating underneath it. Deliberate discontinuity, not a smooth
    // wrap.
    float clampedRollAngle = rawRollAngle;
    if (rawRollAngle > uprightMaxRad)
        clampedRollAngle = -uprightMaxRad;
    else if (rawRollAngle < -uprightMaxRad)
        clampedRollAngle = uprightMaxRad;
    float rollCorrection = clampedRollAngle - rawRollAngle;
    float crc = cos(rollCorrection);
    float src = sin(rollCorrection);
    axisX = float2(axisX.x * crc - axisX.y * src, axisX.x * src + axisX.y * crc);
    axisY = float2(axisY.x * crc - axisY.y * src, axisY.x * src + axisY.y * crc);
    axisZ = float2(axisZ.x * crc - axisZ.y * src, axisZ.x * src + axisZ.y * crc);

    // HAND_EXP basis: Z is finger length, Y is palm normal and X is width.
    // API basis: green/Y is upward finger direction, red/X is palm-facing
    // direction, blue/Z supplies the remaining width/depth rotation.
    // Post-remap: hand.x = palm-normal, hand.y = finger-length, hand.z = width.
    // （align-t3：本轴重映射是旧 EFMI「躺平」（R9）的根因修复——旧实现缺
    // 此映射且第三轴为 -axisY 近似，现由真三轴 gizmo 基 + 映射一并替代。）
    float3 hand = float3(-i.position.y, -i.position.z, i.position.x);

    // LMB poke windup: tilt the hand back the longer LMB is held alone —
    // rotates hand.x (palm-normal) into hand.y (finger-length) around the
    // hand's own local Z axis, like a wrist winding back before a slap.
    // Applied here (before recentering) so it pivots at the wrist — the
    // mesh's natural local origin — rather than the recentered palm point.
    // 蓄力期间基在按下瞬间冻结（预览 CS 的 charging 锁存），本地空间旋转
    // 不与实况 hover 抖动打架。方向反则调 $ssmtdrag_efmi_hand_tilt_max_deg。
    float lmbHoldFraction = saturate(HAND_STATE.x);
    if (lmbHoldFraction > 0.0)
    {
        float minDeg = SafePositive(HAND_TILT.x, 10.0);
        float maxDeg = SafeNonZero(HAND_TILT.z, 45.0);
        float tiltRad = radians(lerp(minDeg, maxDeg, lmbHoldFraction));
        float ct = cos(tiltRad);
        float st = sin(tiltRad);
        float rotX = hand.x * ct + hand.y * st;
        float rotY = -hand.x * st + hand.y * ct;
        hand.x = rotX;
        hand.y = rotY;
    }

    // RMB poke windup: same idea, but rotates hand.y (finger-length) into
    // hand.z (width) around the hand's local X axis instead of Z, so push
    // reads visually distinct from pull. Fully independent min/time/max from
    // LMB above (flip $ssmtdrag_efmi_hand_tilt_rmb_max_deg if wrong way).
    float rmbHoldFraction = saturate(HAND_STATE.z);
    if (rmbHoldFraction > 0.0)
    {
        float minDeg = SafePositive(HAND_TILT.y, 10.0);
        float maxDeg = SafeNonZero(HAND_TILT.w, 45.0);
        float tiltRad = radians(lerp(minDeg, maxDeg, rmbHoldFraction));
        float ct = cos(tiltRad);
        float st = sin(tiltRad);
        float rotY = hand.y * ct + hand.z * st;
        float rotZ = -hand.y * st + hand.z * ct;
        hand.y = rotY;
        hand.z = rotZ;
    }

    // Grab tilt（抓手效果核心①）: while genuinely captured (status 2.0, not
    // just a lone-hold charge), wind the hand back around the same local Z
    // axis as the LMB poke windup, but driven by how far the drag has
    // actually been stretched (0..1 of that zone's max_offset) instead of
    // hold time. Shares LMB's min angle as its floor at zero stretch.
    // stretchFraction：ZZMI 读 ScreenState[8].z；EFMI 由预览 CS 打包进
    // Preview[3].w（每实例×区弹簧无单共享态，预览已知获胜实例）。
    bool captured = Preview[2].w > 1.5;
    float stretchFraction = captured ? Preview[3].w : 0.0;
    if (captured && stretchFraction > 0.0)
    {
        float minDeg = SafePositive(HAND_TILT.x, 10.0);
        float maxDeg = SafeNonZero(HAND_UPRIGHT.z, 45.0);
        // Config (Min/Max Angle) stays positive for a natural "bigger =
        // more tilt" feel -- negated only here because the grab pose draws
        // the HandAction mesh, which winds back visually opposite the LMB
        // poke windup's HandNoAction mesh for the same positive angle.
        float tiltRad = -radians(lerp(minDeg, maxDeg, stretchFraction));
        float ct = cos(tiltRad);
        float st = sin(tiltRad);
        float rotX = hand.x * ct + hand.y * st;
        float rotY = -hand.x * st + hand.y * ct;
        hand.x = rotX;
        hand.y = rotY;
    }

    // Recenter the mesh's authored bounding-box middle onto the cursor
    // instead of its local origin, which sits near the wrist — without
    // this, the cursor anchors to the wrist end and the hand trails off it.
    hand += HAND_CENTER.xyz;
    // Positive local X is the red normal-facing direction. Move the whole
    // visual hand a little outside the surface before its soft plane test.
    hand.x += SURFACE_PROXY.y;
    float handScale = SafePositive(HAND_SCALE.x, 0.65);
    float visualScale = 0.72 * handScale;
    float2 localOffset;
    if (validBasis)
        localOffset = (hand.x * axisX + hand.y * axisY + hand.z * axisZ) * (perspectiveScale * visualScale);
    else
        // Safe, finite fallback: a normal screen cursor instead of a giant
        // overlay when no detector surface is available.
        // （align-t3 D-6：o.hit=0 → PS discard——ZZMI hide-entirely 语义；
        // 弃用旧 380px 屏幕对齐兜底基（陈旧基曾致满屏拖影）。）
        localOffset = float2(hand.x, hand.z) * 190.0 * handScale;

    // Inverted-hull outline: push this vertex outward in screen space along
    // its own baked normal, projected through the same detected local basis
    // as position above -- consistent with how position itself is mapped,
    // just applied to the normal instead. Unlike position this is NOT
    // scaled by perspectiveScale/visualScale: the push is a fixed pixel
    // width (resolution-scaled) so the rim reads as a constant thickness
    // regardless of the hand's current on-screen size. Uses the same
    // un-windup-rotated normal as the PS shading below (see its comment) --
    // during a windup pose the push direction can be a little off from the
    // true current silhouette near the wrist pivot, an acceptable trade for
    // not duplicating the whole rotation pipeline for a ~2px cosmetic line.
    if (validBasis && HAND_OUTLINE.y > 0.5)
    {
        float3 outlineRawNormal = SmoothNormal[vid];
        float3 outlineNormal = float3(-outlineRawNormal.y, -outlineRawNormal.z, outlineRawNormal.x);
        float2 normal2D = outlineNormal.x * axisX + outlineNormal.y * axisY + outlineNormal.z * axisZ;
        float normalLen = length(normal2D);
        float2 pushDir = normalLen > 1e-5 ? normal2D / normalLen : float2(0.0, 0.0);
        float outlineWidth = SafePositive(HAND_OUTLINE.x, 2.0) * resolutionScale;
        localOffset += pushDir * outlineWidth;
    }

    // Grab exertion（抓手效果核心②）: once the drag passes the vibrate
    // threshold (stretch fraction, 0..1 of that zone's max_offset), shake
    // faster the closer it gets to the hard limit. Silent below the threshold.
    float vibThreshold = saturate(SafePositive(HAND_VIBRATE.x, 0.5));
    if (captured && stretchFraction > vibThreshold)
    {
        float t = saturate((stretchFraction - vibThreshold) / max(1.0 - vibThreshold, 1e-4));
        float periodSlow = SafePositive(HAND_VIBRATE.y, 0.8);
        float periodFast = SafePositive(HAND_VIBRATE.z, 0.1);
        float period = lerp(periodSlow, periodFast, t);
        float amplitude = SafePositive(HAND_VIBRATE.w, 4.0) * t;
        float phase = HAND_STATE.y / max(period, 1e-4);
        float2 shake = float2(sin(phase * 6.2831853), sin(phase * 1.7 * 6.2831853 + 1.3));
        localOffset += shake * amplitude;
    }

    float2 p = anchor.xy + localOffset;
    o.pos = float4(p.x / screen.x * 2.0 - 1.0, p.y / screen.y * 2.0 - 1.0, 0.0, 1.0);
    o.color = i.color;
    o.surface = hand.x;
    // Same axis remap applied to position above, applied to the baked
    // smooth normal — a pure permutation+sign-flip, so no separate inverse-
    // transpose handling is needed. Left un-normalized on purpose: linear
    // interpolation across the triangle shortens it slightly, and
    // renormalizing per-pixel in the PS is what actually gives the smooth
    // (Gouraud/Phong-ish) shading instead of a flat per-triangle facet.
    float3 rawNormal = SmoothNormal[vid];
    o.normal = float3(-rawNormal.y, -rawNormal.z, rawNormal.x);
    // No detector surface under the cursor: hide entirely instead of the old
    // fallback generic screen-cursor shape.
    o.hit = validBasis ? 1.0 : 0.0;
}
#endif

#ifdef PIXEL_SHADER
float4 main(VSOut i) : SV_Target0
{
    if (i.hit < 0.5)
        discard;
    float alpha = i.color.a * saturate(SafePositive(HAND_SCALE.y, 1.0));
    if (SURFACE_PROXY.x > .5)
    {
        // Signed local plane approximation: negative values are embedded in
        // the mesh. A soft edge avoids a hard raster seam while preserving a
        // future path for a true per-pixel depth proxy.
        float softness = max(SURFACE_PROXY.z, 1e-4);
        alpha *= smoothstep(-softness, softness, i.surface);
    }
    if (HAND_OUTLINE.y > 0.5)
    {
        // Outline shell: flat, unlit color instead of the smooth-shaded
        // hand -- this draw call is only the enlarged silhouette meant to
        // peek out from behind the real hand (drawn after this one), so no
        // per-pixel normal lighting is needed here.
        alpha *= saturate(SafePositive(HAND_OUTLINE.z, 1.0));
        if (alpha <= 1e-4)
            discard;
        return float4(0.0, 0.0, 0.0, alpha);
    }
    if (alpha <= 1e-4)
        discard;

    // Smooth shading from the baked per-vertex normal (see SmoothNormal
    // above), linearly interpolated across the triangle by the rasterizer
    // and renormalized here — real Gouraud/Phong-ish shading instead of one
    // flat facet per triangle. Lit against a light direction fixed in the
    // mesh's own local space, so the shading stays consistent regardless of
    // how the hand is currently reoriented on screen.
    float3 faceNormal = normalize(i.normal);
    float3 rawLightDir = HAND_SHADE.xyz;
    float3 lightDir = normalize(length(rawLightDir) > 1e-5 ? rawLightDir : float3(0.4, 0.6, 0.65));
    float ambient = saturate(SafePositive(HAND_SHADE.w, 0.55));
    float ndotl = saturate(dot(faceNormal, lightDir));
    float shade = lerp(ambient, 1.0, ndotl);

    return float4(i.color.rgb * shade, alpha);
}
#endif
