// EFMI UI bridge publish (independent implementation, TheHerta4).
//
// Publishes the per-frame drag hit result for the panel linkage (F3): reads
// the EFMI detect Candidate buffer (8 spatial instances x 4 float4 records),
// runs the same cross-instance winner arbitration as efmi_simulate /
// efmi_shapekey_drive, and writes two scalar R32 buffers that the CPU-side
// UIReadback command list stores into the read-only linkage variables
// $ssmtdrag_ui_detected_<ns> / $ssmtdrag_ui_zone_<ns> (panel-side contract
// unchanged; zone semantics = EFMI zone id 0-255).
//
// A candidate is fresh only when its frame tag equals the current frame, so
// a stale probe (Alt released / pass not rendered this frame) publishes -1
// automatically — no extra "not detected" arbitration is needed.
//
// Bindings:
//   u0   = ResourceEFMIDragUIDetect_{ns} (R32_FLOAT x 1; 1 = hit, -1 = none)
//   u1   = ResourceEFMIDragUIZone_{ns}   (R32_FLOAT x 1; zone id, -1 = none)
//   t0   = ResourceEFMIDragCandidate_{ns}
//   t120 = IniParams (auto; [150].w = frame tag)

RWBuffer<float> UIDetect : register(u0);
RWBuffer<float> UIZone : register(u1);
Buffer<float4> Candidate : register(t0);
Texture1D<float4> IniParams : register(t120);

[numthreads(1, 1, 1)]
void main(uint3 tid : SV_DispatchThreadID)
{
    float frame = IniParams[150].w;
    float winDepth = 1e30;
    bool anyHit = false;
    uint winSlot = 0u;
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
            winSlot = k;
            anyHit = true;
        }
    }
    if (!anyHit)
    {
        UIDetect[0] = -1.0;
        UIZone[0] = -1.0;
        return;
    }
    UIDetect[0] = 1.0;
    UIZone[0] = Candidate[winSlot * 4u].x;  // EFMI zone id 0-255
}
