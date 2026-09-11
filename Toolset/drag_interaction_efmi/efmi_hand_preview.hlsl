// EFMI hand preview — ZZMI rzm_jiggle_cursor_preview.hlsl (102 行) 三态
// 状态机移植（efmi-zzmi-drag-align t3, TheHerta4, 256-zone model）。
//
// 三态（对齐 ZZMI JiggleScreenState[3].w 语义）：
//   - 悬停（idle+命中）：锚点/朝向全实况（命中点屏幕 uv + 锚点 RT 的 gizmo
//     投影逐帧刷新）；
//   - 蓄力（charging = 独按 LMB/RMB 且未修饰键、未抓取、有命中）：位置实况
//     （跟随命中点），朝向（gizmo 三轴投影）**冻结**于蓄力开始帧——蓄力
//     倾斜旋转不与实况 hover 抖动打架（ZZMI screen_state isCharging/
//     wasCharging 冻结语义；EFMI 侧由本 CS 自锁存 HandPreview 持久记录实现）；
//   - 抓取（grab = simulate 冻结区，State[b+4].w）：位置/朝向全冻结 +
//     跟随解算位移（ZZMI ApplyCapturedJiggleOffset：把物理位移经冻结拖拽基
//     2×2 逆投影回屏幕）。
// 无命中且无抓取 → 隐藏（status=0，手型 VS validBasis 失败 → PS discard，
// ZZMI hide-entirely；D-6：弃用旧 380px 屏幕对齐兜底基）。
//
// 坐标域（fork 差异吸收点，基准 §6.1 保留项）：EFMI cursor/Candidate/锚点
// 投影全部是 [0,1] top-down uv；本 CS 在出口把锚点换算为 ZZMI 手型约定的
// **Y-up px**（(uv.x·W, (1−uv.y)·H)），gizmo 投影保持 ZZMI 存储约定
// （top-down uv/单位，手型 VS 乘 pixelScale=(W,−H) 转 Y-up px）——手型 VS
// 内部数学与 ZZMI 逐行一致，[Present] 光标注入两行（[0,1] 双直用）不受影响。
//
// HandPreview 记录（4×float4，ZZMI CursorPreview 契约 + EFMI 诊断槽；[1].zw /
// [2].xy 的轴序为 EFMI 专用「手型轴序」，见下方 t28 说明）：
//   [0] = (anchor.x, anchor.y, screenW, screenH)   锚点 Y-up px + 屏幕尺寸
//   [1] = (gizmo 法线.xy, −gizmo 副切线.xy)        top-down uv/单位投影
//   [2] = (gizmo 切线.xy, 基有效, 状态)            状态 0=隐藏 1=悬停/蓄力 2=抓取
//   [3] = (诊断状态码, 蓄力旗标, 抓取旗标, stretchFraction)
//
// t28 轴序（对 EFMI 手型 VS 的槽语义：axisX=掌法线、axisY=手型「下」端
// （手指 = −axisY）、axisZ=宽度）：
//   [1].xy = 表面法线（掌法线；与 ZZMI 同槽，未变）
//   [1].zw = −副切线 = 沿表面「下」方向（手型手指沿 −axisY = +副切线 朝上）
//   [2].xy = 切线 = 水平宽度轴
// ZZMI 契约把*切线*喂进 [1].zw，但切线 = cross(localRef, normal) 恒垂直于
// localRef（≈世界上方）⇒ 任何 front-facing 表面上切线恒水平（本模组实测
// (1,0,0)）⇒ 手型「上/下」槽拿到水平轴，手型只能侧躺，且竖直防翻（期望该槽
// 朝下、手指朝上）无从满足，只能把整帧镜像折返 ≈170° ⇒ 用户实测「底朝左」。
// 沿表面的上方向是*副切线* = localRef 在切平面内的投影（实测 (0,-0.03,1)≈+Z
// 即世界上方），且它对该三元积的不动点性质免疫于法线反号（front/back 不再
// 有半圈伪影），故 EFMI 把手型「上/下」槽改接到副切线。
//         诊断码 0=可见 1=无命中隐藏（t32-B 链路，生成器 store 索引 12）；
//         stretchFraction = State[b+5].z（ZZMI ScreenState[8].z 等价——
//         EFMI 每实例×区弹簧由预览 CS 按获胜实例拷贝）
//
// Bindings:
//   u0   = ResourceEFMIDragHandPreview_{ns}
//   t0   = ResourceEFMIDragCandidate_{ns}
//   t1   = ResourceEFMIDragState_{ns}
//   t2   = ResourceEFMIDragAnchorProject_{ns}（锚点投影 RT：每区 8 行——
//          0-3 恒等轴（拖拽基，detect 消费）、4-7 gizmo 轴（本 CS 消费））
//   t120 = IniParams (auto): [150].w frame、[151] cursor/modifier/buttons、
//          [152].w 命中阈值、[153].y enabled、[184].xy 屏幕尺寸
RWBuffer<float4> HandPreview : register(u0);
Buffer<float4> Candidate : register(t0);
Buffer<float4> State : register(t1);
Texture2D<float4> Anchors : register(t2);
Texture1D<float4> IniParams : register(t120);

static const uint PUBLIC_SLOTS = 6;
static const uint MAX_ZONES = 256;
static const uint ZONE_STRIDE = 4;
static const uint STATE_STRIDE = PUBLIC_SLOTS + MAX_ZONES * ZONE_STRIDE;  // 1030
static const uint ANCHOR_ROWS = 8;

// [0,1] top-down uv → ZZMI 手型约定的 Y-up px
float2 UvToYupPx(float2 uv, float2 screen)
{
    return float2(uv.x * screen.x, (1.0 - uv.y) * screen.y);
}

[numthreads(1, 1, 1)]
void main(uint3 tid : SV_DispatchThreadID)
{
    float frame = IniParams[150].w;
    float2 screen = max(IniParams[184].xy, float2(1.0, 1.0));
    // C1b/t13: [151].xy = [0,1] top-down 归一化光标（R12 终版双直用）。
    float2 cursorNorm = IniParams[151].xy;
    bool enabled = IniParams[153].y > 0.5;
    bool modifier = IniParams[151].z > 0.5 && enabled;
    int buttons = (int)IniParams[151].w;

    // 自锁存读取（HandPreview 是持久 RW 记录）：上一帧蓄力/抓取旗标
    float4 prevFlags = HandPreview[3];
    bool prevCharging = prevFlags.y > 0.5;
    bool prevCapturing = prevFlags.z > 0.5;

    // ---- 跨实例赢家仲裁（与 efmi_simulate 同语义）----
    uint win = 0u;
    float winDepth = 1e30;
    bool anyHit = false;
    [unroll]
    for (uint k = 0u; k < 8u; ++k)
    {
        float4 cand = Candidate[k * 4u];
        // Hit gate (t26 F1): zone id 0 is valid, so a candidate is a hit only
        // when its weight clears the threshold (miss sentinel is y == 0).
        if (cand.w != frame || cand.y < IniParams[152].w)
            continue;
        if (cand.z < winDepth)
        {
            winDepth = cand.z;
            win = k;
            anyHit = true;
        }
    }

    // ---- 抓取实例扫描（不依赖实况命中：抓取后命中闪断帧手型不消失——
    // ZZMI grab 分支全冻结语义的 EFMI 等价；State[b+4].w = simulate 冻结
    // 旗标（R11 修复槽位），zone 0 亦成立）----
    int grabInst = -1;
    [unroll]
    for (uint j = 0u; j < 8u; ++j)
    {
        if (State[j * STATE_STRIDE + 4u].w > 0.5)
        {
            grabInst = (int)j;
            break;
        }
    }
    bool capturing = grabInst >= 0;
    // 蓄力（charging）：独按 LMB 或 RMB（未按修饰键=非真实抓取手势）、
    // 未在抓取、有实况命中（ZZMI isCharging 语义：hasHit && !newCapture &&
    // !lockedCapture；EFMI 抓取手势 = 修饰键+按键 → 独按须排除修饰键）
    bool charging = !capturing && !modifier && (buttons == 1 || buttons == 2) && anyHit;

    if (!anyHit && !capturing)
    {
        // 无命中且无抓取 → 隐藏（ZZMI cursor_preview L61-72：退化为无效基
        // 记录，手型 VS discard；诊断码 1=无命中隐藏）
        HandPreview[0] = float4(UvToYupPx(cursorNorm, screen), screen);
        HandPreview[1] = 0;
        HandPreview[2] = 0;
        HandPreview[3] = float4(1.0, 0, 0, 0);
        return;
    }

    // 数据源实例/区：抓取优先（抓取实例的冻结区），否则实况赢家
    uint inst = capturing ? (uint)grabInst : win;
    uint instBase = inst * STATE_STRIDE;
    uint zone = capturing
        ? (uint)max(State[instBase + 0u].z, 0.0)      // 抓取时读锁存的捕获区
        : (uint)max(Candidate[inst * 4u].x, 0.0);     // 悬停/蓄力读命中区

    // ---- gizmo 三轴投影（锚点 RT 行 zone*8+4..7 = 中心/法线/切线/副切线，
    // 每区烘焙帧（生成器 compute_zone_gizmo_frames，ZZMI BuildGizmoAxes 构造），
    // 探针逐帧经游戏 VS 投影——跟随蒙皮姿态）。t28：三轴按「手型轴序」重排后
    // 写入下方 gizmoXY/gizmoZ（见 t28 说明；row 5/6/7 的读法不变）----
    uint anchorWidth, anchorHeight;
    Anchors.GetDimensions(anchorWidth, anchorHeight);
    float4 gizmoXY = 0;   // 法线.xy + (−副切线).xy（top-down uv/单位）
    float4 gizmoZ = 0;    // 切线.xy + 有效
    bool inBounds = zone * ANCHOR_ROWS + 7u < anchorWidth;
    if (inBounds)
    {
        float4 c = Anchors.Load(int3(zone * ANCHOR_ROWS + 4u, 0, 0));
        float4 an = Anchors.Load(int3(zone * ANCHOR_ROWS + 5u, 0, 0));
        float4 at = Anchors.Load(int3(zone * ANCHOR_ROWS + 6u, 0, 0));
        float4 ab = Anchors.Load(int3(zone * ANCHOR_ROWS + 7u, 0, 0));
        if (min(min(c.w, an.w), min(at.w, ab.w)) > 1e-5)
        {
            // ToScreen 与 efmi_detect 同式（top-down uv；clip Y-up → 减号翻转）
            float2 sc = float2(.5 + .5 * c.x / c.w, .5 - .5 * c.y / c.w);
            float2 sn = float2(.5 + .5 * an.x / an.w, .5 - .5 * an.y / an.w);
            float2 st = float2(.5 + .5 * at.x / at.w, .5 - .5 * at.y / at.w);
            float2 sb = float2(.5 + .5 * ab.x / ab.w, .5 - .5 * ab.y / ab.w);
            // ---- t28 修复（手型整体旋转 90°：底由朝左改为朝下）----
            // 手型 VS 的 axisY 槽语义 = 手型「下」端（竖直防翻把该槽压成朝下、
            // 手指由 −axisY 朝上），axisZ 槽 = 宽度。生成器烘焙的 gizmo 三轴中：
            //   · 切线 = cross(localRef, normal) ⟂ localRef（≈世界上方）
            //     ⇒ front-facing 表面上恒*水平*（本模组 zone0 实测 (1,0,0)）；
            //   · 副切线 = cross(normal, tangent) = localRef 在切平面内的投影
            //     ≈ 沿表面*上*方向（实测 (0,-0.03,1) ≈ +Z）。
            // 旧轴序把水平切线喂给 axisY ⇒ 手型侧躺（底朝左/右），且防翻只能
            // 把整帧镜像折返 ≈170°（用户实测「底朝左」= 该折返方向）。
            // 新轴序：axisY 槽 = *−副切线*（沿表面「下」方向），axisZ 槽 = 切线
            // （水平宽度）。取负的理由：该槽的契约是「朝下的那一端」（防翻要求
            // |roll|≤90° ⇒ axisY 朝下 ⇒ −axisY = 手指朝上）；喂 −副切线 时
            // |roll|≈0，防翻不触发，映射保持右手系（det=+1）= 纯旋转；喂
            // +副切线 会逼防翻翻单轴（det=−1）→ 手型被镜像而非旋转。
            // 几何结论：手指(−hand.y)→+副切线=屏幕上、手型底(+hand.y)→−副切线
            // =屏幕下（详见 t28 报告；三轴长度与 perspectiveScale 均未变）。
            gizmoXY = float4((sn - sc) / 0.01, -(sb - sc) / 0.01);
            gizmoZ = float4((st - sc) / 0.01, 1.0, 0.0);
        }
    }

    // ---- t52：手型投影轴的屏幕 Y 分量镜像（IniParams[165].z：0 / 未写 =
    // **不**镜像 = 出厂默认；1 = 镜像。F11 循环 0↔1）----
    // t49 曾按用户字面要求把「镜像 = 出厂」，实测「我鼠标往上移、手型光标往下移」：
    // 整帧镜像反射的是**最终位置**、其中含拖拽跟随量 uvDelta（`frozenAnchor +=
    // uvDelta` → `anchorPx`），所以它不只挪位置，还把**跟随方向**一起反号。故回正
    // 为默认不镜像（1 保留为对照态：若整个手型坐标系确实 Y 反了才用它）。
    // [1] = 法线.xy + (−副切线).xy、[2].xy = 切线.xy，都是 top-down uv/单位
    // **存储域**，手型 VS 再乘 pixelScale=(W,−H) 才成为屏幕向量 ⇒ 取反存储域
    // .y 即取反该轴的屏幕 Y。
    // 必须落在**写盘之前**、而不是 main 末尾改记录：本 CS 的冻结帧会把
    // HandPreview[2] 读回来原样写回（`float4 kept = HandPreview[2]`），
    // 若在末尾镜像，冻结帧就会「读到已镜像 → 写回 → 再镜像」= 每帧翻一次，
    // 抓取/蓄力期间基以帧率振荡。翻本地量则落盘记录恒为镜像后约定，幂等。
    // [2].z（基有效）/ [2].w（状态）是旗标，不参与。
    if (IniParams[165].z > 0.5)
    {
        gizmoXY.y = -gizmoXY.y;
        gizmoXY.w = -gizmoXY.w;
        gizmoZ.x = -gizmoZ.x;
        gizmoZ.y = -gizmoZ.y;
    }

    // ---- 朝向冻结（charging/grab 开始帧写实况，后续帧复用锁存值）----
    // 抓取/蓄力持续帧：保留 HandPreview[1]/[2] 的既有 gizmo 投影（冻结）；
    // 开始帧（或实况悬停帧）：写入本帧实况投影。
    bool freezeFrame = (capturing && prevCapturing) || (charging && prevCharging);
    if (!freezeFrame)
    {
        HandPreview[1] = gizmoXY;
        HandPreview[2] = float4(gizmoZ.xy, gizmoZ.z, 0.0);
    }

    // ---- t14 修复（问题 2）：拖拽轴（行 0-3）的屏幕投影柱 ----
    // 拖拽位移 `State[b+PUBLIC_SLOTS+zone*4].xyz` 是**世界空间三维位移**，
    // 沿锚点行 0-3 所代表的三个**坐标轴**（generator make_anchor_positions：
    // 行 0=中心、1/2/3 = 中心 + 各轴 ×10mm —— 即 detect `ComputeBasis` 消费的
    // 「拖拽基」）。要把它投影回屏幕，必须用**同一组轴**的屏幕导数：
    //   dragColX/Y/Z = (ToScreen(轴端) − ToScreen(中心)) / 0.01  [uv / m]
    // 这正是三维→屏幕 Jacobian 的列。
    // 旧实现（pre-t9 二维 2×2 逆投影）拿 `State[b+1].xy/[b+2].xy` 当 2×2 矩阵
    // 去逆投影三维 `off`：既丢掉了第三个轴的贡献，又把「屏幕 uv 分量」当世界
    // 分量用（量纲错配）——位移里沿法线的 depth-pull 分量因此被错误地折进屏幕
    // 平面，表现为**手型方向与鼠标无关、恒定朝一个方向**（用户实测）。
    // ZZMI 参考 `rzm_jiggle_cursor_preview.hlsl::ApplyCapturedJiggleOffset`
    // （L13-37）：用命中点的三个世界轴投影柱分解 `off` 三分量，再 × 屏幕尺寸
    // 换算到像素域 —— 此处同构，只是轴改为探针已投影的拖拽轴行。
    float2 dragColX = 0, dragColY = 0, dragColZ = 0;
    bool dragColsValid = false;
    if (zone * ANCHOR_ROWS + 3u < anchorWidth)
    {
        float4 dc = Anchors.Load(int3(zone * ANCHOR_ROWS + 0u, 0, 0));
        float4 dx4 = Anchors.Load(int3(zone * ANCHOR_ROWS + 1u, 0, 0));
        float4 dy4 = Anchors.Load(int3(zone * ANCHOR_ROWS + 2u, 0, 0));
        float4 dz4 = Anchors.Load(int3(zone * ANCHOR_ROWS + 3u, 0, 0));
        if (min(min(dc.w, dx4.w), min(dy4.w, dz4.w)) > 1e-5)
        {
            float2 sdc = float2(.5 + .5 * dc.x / dc.w, .5 - .5 * dc.y / dc.w);
            float2 sdx = float2(.5 + .5 * dx4.x / dx4.w, .5 - .5 * dx4.y / dx4.w);
            float2 sdy = float2(.5 + .5 * dy4.x / dy4.w, .5 - .5 * dy4.y / dy4.w);
            float2 sdz = float2(.5 + .5 * dz4.x / dz4.w, .5 - .5 * dz4.y / dz4.w);
            dragColX = (sdx - sdc) / 0.01;
            dragColY = (sdy - sdc) / 0.01;
            dragColZ = (sdz - sdc) / 0.01;
            dragColsValid = true;
        }
    }

    // ---- 锚点（ZZMI 三分支语义）----
    // t14 修复（问题 2）：抓取态位移改为**三维分解投影**（见上方 dragCol* 说明）。
    float2 anchorUv;
    if (capturing)
    {
        // 抓取：位置/朝向全冻结 + 跟随解算位移（ZZMI ApplyCapturedJiggleOffset）
        // 冻结锚点 = 捕获瞬间光标（simulate 锁存 State[b+0].xy；命中点即光标下点，
        // 等价 ZZMI screenAnchor）。
        float2 frozenAnchor = State[instBase + 0u].xy;
        uint gz = zone;
        if (gz < MAX_ZONES && dragColsValid)
        {
            float3 off = State[instBase + PUBLIC_SLOTS + gz * ZONE_STRIDE].xyz;
            // ZZMI 同构：把三维位移投影到三个轴（列向量点积），得到屏幕 uv 位移。
            float2 uvDelta = dragColX * dot(off, float3(1, 0, 0))
                           + dragColY * dot(off, float3(0, 1, 0))
                           + dragColZ * dot(off, float3(0, 0, 1));
            // t18 修复（手型上下反号）：Y 分量补一次翻转。
            // 实测：手型**左右正确、上下反了**（与拖拽当初 t14 之前的症状同类）。
            // 成因（单轴、纯符号）：`dragCol*` 是 `ToScreen`（含一次 Y 翻转）的导数，
            // 而 `off` 是 simulate 用**同一套**屏幕语义基向量累积出来的世界位移——
            // 两条链本身自洽。但 t14 在 `efmi_simulate.hlsl:277` 对 `delta.y` 补了
            // 一次翻转（修正拖拽上下颠倒），那次翻转**同时**进入了 `off`（因为
            // `rawDrag` 由被翻转的 `delta.y` 构建），而本预览的 `dragCol*` **不含**
            // 该修正 ⇒ `off` 与 `dragCol*` 在 Y 上相差一个符号 ⇒ 手型纵向镜像。
            // 拖拽链不能动（已实测正确），故在**投影结果**上对 Y 补回同一符号，
            // 与 simulate 的约定对齐。
            // X 无此问题（`ToScreen` 的 X 分量不含翻转，t14 也未改 delta.x）。
            uvDelta.y = -uvDelta.y;
            // 拖拽基为 top-down uv 语义；与锚点同域相加后统一换算
            frozenAnchor += uvDelta;
        }
        anchorUv = frozenAnchor;
    }
    else
    {
        // 悬停/蓄力：位置实况（命中点屏幕 uv，R11 重心插值）；命中闪断帧
        // 回退光标处（ZZMI charging 分支 liveHitValid 闪断回退同义）
        float4 hitAnchor = Candidate[win * 4u + 3u];
        anchorUv = hitAnchor.w > 1e-5 ? hitAnchor.xy : cursorNorm;
    }
    float2 anchorPx = UvToYupPx(anchorUv, screen);
    // ---- t54：手型渲染位置的 Y 镜像（IniParams[165].z：0 / 未写 = **开** = 出厂；
    // 1 = 关。F11 循环 0↔1）----
    // 用户指定（t54）：「鼠标命中的光标修好了，拖拽的光标又错位了 —— 不要镜像
    // 拖拽的光标」。成因：镜像作用在**最终锚点**上 ——
    //   anchorPx = UvToYupPx(frozenAnchor + uvDelta)
    // 抓取态里含拖拽跟随量 `uvDelta`，所以整条一起镜像会把**跟随方向**也反号
    // （手型朝鼠标反方向走 = 「错位」）。
    // 故此处**按状态分流**：悬停/蓄力的命中光标照常镜像（这条已被用户确认修好），
    // 抓取（拖拽）路径**不镜像** ⇒ 拖拽时手型只跟随、不再被反射。
    // 上方 gizmo 朝向反号块与抓取基准 `frozenAnchor` 都**不动**（用户要求只改这一处）。
    // X4596：RWBuffer 是 typed UAV，单分量 store 编译失败，故改本地量而不是
    // `HandPreview[0].y =`。----
    if (IniParams[165].z < 0.5 && !capturing)
        anchorPx.y = screen.y - anchorPx.y;

    // stretchFraction：抓取区当前位移 / max_offset（simulate 写 State[b+5].z；
    // ZZMI ScreenState[8].z 等价）——抓取拉伸倾斜/振动的驱动源
    float stretchFraction = capturing ? State[instBase + 5u].z : 0.0;
    float status = capturing ? 2.0 : 1.0;

    HandPreview[0] = float4(anchorPx, screen);
    if (freezeFrame)
    {
        // 朝向冻结帧：保留 [1]/[2].xyz（gizmo 投影 + 有效），仅刷新状态
        float4 kept = HandPreview[2];
        HandPreview[2] = float4(kept.xyz, status);
    }
    else
    {
        HandPreview[2] = float4(gizmoZ.xy, gizmoZ.z, status);
    }
    HandPreview[3] = float4(0.0, charging ? 1.0 : 0.0, capturing ? 1.0 : 0.0,
                            stretchFraction);
}
