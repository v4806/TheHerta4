// EFMI shape-key drive (independent implementation, TheHerta4).
//
// Drives per-zone shape-key intensities from hover hit + LMB hold, mirroring
// the zzmi F1 linkage semantics but with a fully independent EFMI input
// surface: the hit source is the EFMI detect Candidate buffer (8 spatial
// instances x 8 float4 records) instead of the zzmi PinnedDetectInfo struct,
// and all IniParams inputs live in the EFMI extension region 156+ (adjacent
// to the 150-155 probe/detect/simulate/deform region, disjoint from the zzmi
// 77-124 region). No zzmi shader code or resource names are shared.
//
// Runs once per frame from [Present] (after simulate). Semantics:
//   - Cross-instance winner arbitration over Candidate (nearest reverse-Z
//     depth, ties to the lowest instance id); a candidate is fresh only when
//     its frame tag equals the current frame.
//   - The first fresh hit while Alt+LMB is held binds that zone
//     (level-triggered); the bound zone keeps being driven by mouse
//     displacement after the cursor leaves it. Releasing unlatches on that
//     frame. Unbound zones hold their value.
//   - Directional slots follow the latched binding; no-direction stage slots
//     advance on each press edge while hovering the zone (0..N cycle).
//   - Cold-start seeding restores click counts from export variables
//     (seed_pending flag + entries, populated by the variable-linkage F4
//     module).
//
// Bindings:
//   t0   = ResourceEFMIDragCandidate_{ns}        (hit candidates, 8x4 float4)
//   t1   = ResourceEFMIDragShapeKeyZoneStageCounts_{ns} (R32_UINT x capacity)
//   u0   = ResourceEFMIDragShapeKeyDrive_{ns}    (R32_FLOAT x total slots)
//   u1   = ResourceEFMIDragShapeKeyDir_{ns}      (R32_FLOAT x total+1; last = prev press)
//   u2   = ResourceEFMIDragShapeKeyClickCount_{ns} (R32_UINT x capacity)
//   u3   = ResourceEFMIDragShapeKeyActiveDir_{ns}  (R32_UINT x capacity)
//   u4   = ResourceEFMIDragShapeKeyClickCountF_{ns} (R32_FLOAT x capacity)
//   u5   = ResourceEFMIDragShapeKeyDragLatch_{ns}  (R32_FLOAT x 1)
//   t120 = IniParams (auto)
//
// IniParams (EFMI extension region):
//   [150].w = frame tag                     [151].z = modifier (Alt)
//   [151].w = buttons (1=LMB 2=RMB 3=both)  [153].y = enabled
//   [156].x = mouse dy (px/frame)           [156].y = mouse dx
//   [156].z = displacement sensitivity      [156].w = reserved
//   [157].x = seed pending                  [157].y = seed entry count
//   [158+i] = seed entry i: x = zone id, y = value
//
// Zone indexing: EFMI detect reports zone ids 0-255 directly; buffers are
// indexed by the same 0-based id, matching the shape-key node drag_zone_id
// convention. The latch stores zone id + 1 (0 = unbound, zone 0 valid as 1).

RWBuffer<float> ShapeKeyDrive    : register(u0);
RWBuffer<float> ShapeKeyDir      : register(u1);
RWBuffer<uint> ClickCount        : register(u2);
RWBuffer<uint> ActiveDir         : register(u3);
RWBuffer<float> ClickCountF      : register(u4);
RWBuffer<float> DragLatch        : register(u5);
Buffer<float4> Candidate         : register(t0);
Buffer<uint> ZoneStageCounts     : register(t1);
Texture1D<float4> IniParams      : register(t120);

uint ClampZone(uint zoneId, uint zoneCount)
{
    // 256-zone model: zone ids are 0-255 (buffer index directly).
    return min(zoneId, max(zoneCount, 1u) - 1u);
}

[numthreads(1, 1, 1)]
void main(uint3 tid : SV_DispatchThreadID)
{
    uint zoneCount;
    ClickCount.GetDimensions(zoneCount);
    if (zoneCount == 0u)
        return;
    uint driveSlots;
    ShapeKeyDrive.GetDimensions(driveSlots);
    uint dirSlots;
    ShapeKeyDir.GetDimensions(dirSlots);

    uint lastSlot = 0u;
    for (uint z = 0u; z < zoneCount; ++z)
        lastSlot += 4u + max(1u, ZoneStageCounts[z]);
    if (driveSlots < lastSlot || dirSlots < lastSlot + 1u)
        return;
    uint latchSlots;
    DragLatch.GetDimensions(latchSlots);
    if (latchSlots < 1u)
        return;

    float frame = IniParams[150].w;
    bool enabled = IniParams[153].y > .5;
    bool modifier = IniParams[151].z > .5 && enabled;
    int buttons = (int)IniParams[151].w;
    bool triggerHeld = modifier && (buttons & 1) != 0;

    float mouseDy = IniParams[156].x;
    float mouseDx = IniParams[156].y;
    float mouseSensitivity = max(IniParams[156].z, 0.0001);

    bool wasHeld = ShapeKeyDir[lastSlot] > 0.5;
    bool pressed = triggerHeld && !wasHeld;

    // 4-direction orthogonal decomposition (0=up 1=right 2=down 3=left).
    float moveLen = length(float2(mouseDx, mouseDy));
    float4 dirWeight = float4(0.0, 0.0, 0.0, 0.0);
    if (moveLen > 1e-4)
    {
        float2 v = float2(mouseDx, mouseDy) / moveLen;
        dirWeight = float4(max(v.y, 0.0), max(v.x, 0.0), max(-v.y, 0.0), max(-v.x, 0.0));
    }
    uint activeDir = 0u;
    float bestW = dirWeight.x;
    for (uint d = 1u; d < 4u; ++d)
    {
        if (dirWeight[d] > bestW) { bestW = dirWeight[d]; activeDir = d; }
    }

    // Cross-instance winner arbitration (same semantics as efmi_simulate).
    uint winSlot = 0u;
    float winDepth = 1e30;
    bool anyHit = false;
    for (uint k = 0u; k < 8u; ++k)
    {
        float4 cand = Candidate[k * 4u];
        // Hit gate (t26 F1): zone id 0 is valid, so a candidate is a hit only
        // when its weight clears the threshold (same gate as realHit below).
        if (cand.w != frame || cand.y < IniParams[152].w)
            continue;
        if (cand.z < winDepth)
        {
            winDepth = cand.z;
            winSlot = k;
            anyHit = true;
        }
    }
    // 256-zone model: Candidate[0].x = zone id 0-255 directly (no +1 offset).
    uint hitZoneId = anyHit ? ClampZone((uint)round(Candidate[winSlot * 4u].x), zoneCount) : 0u;
    bool realHit = anyHit && Candidate[winSlot * 4u].y >= IniParams[152].w;

    // Cold-start seeding (before the interaction gates; restore persists
    // across the game session like the zzmi chain).
    uint seedCount = (uint)IniParams[157].y;
    if (IniParams[157].x > 0.5 && seedCount > 0u)
    {
        for (uint s = 0u; s < min(seedCount, 8u); ++s)
        {
            float4 entry = IniParams[158u + s];
            // 256-zone model: seed zone id 0-255 = buffer index directly.
            uint seedZone = ClampZone((uint)max(round(entry.x), 0.0), zoneCount);
            uint zoneStageCap = max(1u, ZoneStageCounts[seedZone]);
            uint seeded = (uint)clamp(round(entry.y), 0.0, (float)zoneStageCap);
            ClickCount[seedZone] = seeded;
            ClickCountF[seedZone] = (float)seeded;
            if (seeded >= 1u)
            {
                uint seedBase = 0u;
                for (uint zz = 0u; zz < seedZone; ++zz)
                    seedBase += 4u + max(1u, ZoneStageCounts[zz]);
                uint oneHot = seedBase + 4u + (seeded - 1u);
                if (oneHot < driveSlots)
                    ShapeKeyDrive[oneHot] = 1.0;
            }
        }
    }

    // t41 P-7: ZZMI 严格互斥（rzm_shapekey_drive.hlsl L160-166 同款）——形态键
    // 驱动仅 mode 1（IniParams[159].x = t37-P3 三态模式寄存器）；mode != 1 →
    // 保持 dir 槽一致（triggerHeld 状态，防回 mode 1 时误判按压边沿）+ 清
    // DragLatch（防锁存残留）+ 返回（mode 2 = 变形专用，形态键驱动关闭）。
    float mode = IniParams[159].x;
    if (mode != 1.0)
    {
        ShapeKeyDir[lastSlot] = triggerHeld ? 1.0 : 0.0;
        DragLatch[0] = 0.0;
        return;
    }

    // Latch: first fresh hit while Alt+LMB is held binds the zone; leaving
    // the zone keeps the binding; releasing the trigger unlatches. Stored as
    // zone id + 1 (0 = unbound, zone 0 valid as latch 1); boot/disarm clears
    // (0.0) read as unbound.
    float latchValue = DragLatch[0];
    int boundZoneIdx = (latchValue > 0.5)
        ? (int)floor(latchValue + 0.5) - 1
        : -1;
    if (triggerHeld)
    {
        if (boundZoneIdx < 0 && realHit)
            boundZoneIdx = (int)hitZoneId;
        if (boundZoneIdx < 0 || boundZoneIdx >= (int)zoneCount)
            boundZoneIdx = -1;  // stale binding from a layout change
    }
    else
    {
        boundZoneIdx = -1;
    }
    DragLatch[0] = (float)(boundZoneIdx + 1);

    bool hasHit = realHit && triggerHeld;

    // Press edge while hovering: advance the zone click stage (0..N cycle).
    if (pressed && hasHit)
    {
        uint hoverZone = hitZoneId;
        uint oldStage = ClickCount[hoverZone];
        uint zoneStageCount = max(1u, ZoneStageCounts[hoverZone]);
        ClickCount[hoverZone] = oldStage >= zoneStageCount ? 0u : oldStage + 1u;
    }
    ShapeKeyDir[lastSlot] = triggerHeld ? 1.0 : 0.0;

    uint runningBase = 0u;
    for (uint zone = 0u; zone < zoneCount; ++zone)
    {
        uint zoneStageCount = max(1u, ZoneStageCounts[zone]);
        uint zoneBase = runningBase;
        runningBase += 4u + zoneStageCount;
        bool zoneHit = hasHit && zone == hitZoneId;
        bool zonePressed = zoneHit && pressed;
        bool zoneDriven = boundZoneIdx >= 0 && zone == (uint)boundZoneIdx;
        uint activeStage = ClickCount[zone];
        ClickCountF[zone] = (float)activeStage;
        for (uint dir = 0u; dir < 4u; ++dir)
        {
            uint idx = zoneBase + dir;
            float current = ShapeKeyDrive[idx];
            float next = current;
            if (zoneDriven)
            {
                float net = dirWeight[dir] - dirWeight[(dir + 2u) % 4u];
                next = clamp(current + net * moveLen * mouseSensitivity, 0.0, 1.0);
            }
            ShapeKeyDrive[idx] = next;
        }
        for (uint stage = 1u; stage <= zoneStageCount; ++stage)
        {
            uint ndIdx = zoneBase + 4u + (stage - 1u);
            if (activeStage == stage && zonePressed)
                ShapeKeyDrive[ndIdx] = 1.0;
            else if (activeStage != stage)
                ShapeKeyDrive[ndIdx] = 0.0;
        }
        if (zoneDriven)
            ActiveDir[zone] = activeDir;
    }
}
