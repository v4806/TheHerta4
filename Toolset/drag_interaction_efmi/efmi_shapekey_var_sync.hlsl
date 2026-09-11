// EFMI shape-key variable sync (independent implementation, TheHerta4).
//
// Syncs shape-key export variables into the EFMI ShapeKeyDrive buffer once
// per frame (unconditional, independent of drag activity), so the
// variable-driven path and the drag-driven path share one source of truth
// (equivalent to the zzmi var-sync chain, fully independent bindings and
// registers).
//
// Semantics (value arbitration, variable-first):
//   - The variable changed this frame (hotkey / animation driver) -> the
//     variable owns the buffer and is written immediately (variable always
//     wins).
//   - CPU/GPU arbitration uses a mode handshake in the EFMI extension region:
//     mode 1 suppresses a CPU readback echo; mode 2 force-pushes a changed
//     variable until the delayed store readback confirms the same value.
//   - ZoneActive mirrors the drag drive CS's latched binding (single source
//     of truth: the EFMI DragLatch slot, 0 = unbound, otherwise zone id + 1
//     for zones 0-255). The bound zone stays active while Alt+LMB is held even
//     after the cursor leaves the hit zone; the flag falls once the drive CS
//     observes the release edge.
//   - No-direction bindings carry a ClickCount gate: a nonzero variable opens
//     its stage slot, zeroing only clears the stage that is currently active.
//
// GPU->CPU synchronization is emitted by the generated
// CommandListEFMIDragShapeKeyVarReadback section using the loader's
// direct-resource store syntax (without ref). The pending/ack handshake
// absorbs store latency before pulls resume.
//
// Bindings:
//   t1   = ResourceEFMIDragShapeKeyVarSyncMap (R32G32B32A32_UINT, one uint4
//          per binding: x = drive slot, y = zone id, z = nd_stage or
//          0xFFFFFFFF, w = reserved; baked at export)
//   u0   = ResourceEFMIDragShapeKeyDrive (shared with efmi_shapekey_drive)
//   u1   = ResourceEFMIDragShapeKeyClickCount
//   u2   = ResourceEFMIDragShapeKeyVarPrev (R32_FLOAT x binding count)
//   u3   = ResourceEFMIDragShapeKeyClickCountF
//   u4   = ResourceEFMIDragShapeKeyZoneActive (R32_FLOAT x zone capacity)
//   u5   = ResourceEFMIDragShapeKeyDragLatch (shared latch resource)
//   t120 = IniParams (auto)
//
// IniParams (EFMI extension region, disjoint from zzmi 77-124 and from the
// EFMI 150-158 probe/drive region):
//   [166 + i/4][i%4] = current value of binding i's export variable
//   [175 + i/4][i%4] = CPU/GPU arbitration mode of binding i
//                      (0 = normal, 1 = suppress readback echo,
//                       2 = force-push until delayed readback acknowledges)

RWBuffer<float> ShapeKeyDrive  : register(u0);
RWBuffer<uint>  ClickCount     : register(u1);
RWBuffer<float> VarSyncPrev    : register(u2);
RWBuffer<float> ClickCountF    : register(u3);
RWBuffer<float> ZoneActive     : register(u4);
RWBuffer<float> DragLatch      : register(u5);
Buffer<uint4>   VarSyncMap     : register(t1);
Texture1D<float4> IniParams    : register(t120);

#define EFMI_VAR_VALUE_BASE 166
#define EFMI_VAR_MODE_BASE 175

[numthreads(1, 1, 1)]
void main(uint3 tid : SV_DispatchThreadID)
{
    uint count;
    VarSyncMap.GetDimensions(count);
    if (count == 0u)
        return;
    uint driveSlots;
    ShapeKeyDrive.GetDimensions(driveSlots);
    uint clickSlots;
    ClickCount.GetDimensions(clickSlots);
    uint clickFloatSlots;
    ClickCountF.GetDimensions(clickFloatSlots);
    uint prevSlots;
    VarSyncPrev.GetDimensions(prevSlots);
    if (clickSlots == 0u || clickFloatSlots < clickSlots || prevSlots < count)
        return;

    // ZoneActive mirrors the drive CS's latch (single source of truth).
    uint latchSlots;
    DragLatch.GetDimensions(latchSlots);
    float latchValue = latchSlots >= 1u ? DragLatch[0] : 0.0;
    int boundZone = (latchValue > 0.5) ? (int)floor(latchValue + 0.5) - 1 : -1;
    if (boundZone >= (int)clickSlots)
        boundZone = -1;  // stale binding from a layout change
    for (uint z = 0u; z < clickSlots; ++z)
        ZoneActive[z] = (boundZone >= 0 && z == (uint)boundZone) ? 1.0 : 0.0;

    for (uint i = 0u; i < count; ++i)
    {
        float raw = IniParams[EFMI_VAR_VALUE_BASE + (i >> 2)][i & 3];
        float syncMode = IniParams[EFMI_VAR_MODE_BASE + (i >> 2)][i & 3];
        bool forcePush = syncMode > 1.5;
        if (!forcePush)
        {
            if (abs(raw - VarSyncPrev[i]) <= 1e-6)
                continue;
        }
        VarSyncPrev[i] = raw;
        // mode 1: CPU just pulled from the buffer, skip the echo;
        // mode 2: the value still awaits store confirmation, keep pushing.
        if (syncMode > 0.5 && syncMode < 1.5)
            continue;
        uint4 mapping = VarSyncMap[i];
        uint slot = mapping.x;
        uint zone = mapping.y;
        uint ndStage = mapping.z;
        float v = clamp(raw, 0.0, 1.0);
        if (slot < driveSlots)
            ShapeKeyDrive[slot] = v;
        if (ndStage != 0xFFFFFFFFu && zone < clickSlots)
        {
            if (v > 1e-6)
                ClickCount[zone] = ndStage;
            else if (ClickCount[zone] == ndStage)
                ClickCount[zone] = 0u;
            ClickCountF[zone] = (float)ClickCount[zone];
        }
    }
}
