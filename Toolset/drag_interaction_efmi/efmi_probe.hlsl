// EFMI native-VS projection probe (independent implementation, TheHerta4).
//
// Captures the native game VS output for hit testing WITHOUT rebuilding any
// game matrix or bone layout: the game's own vertex shader (still bound at the
// EFMI per-instance draw callback, after MergedSkeleton_Apply) processes a
// point-list draw; the GS tiles each point into one texel of the probe render
// target and the PS writes the native SV_Position back unchanged.
//
// Binding contract (see docs/analysis/efmi-drag-contract-and-migration-map.md):
//   t120 = IniParams; [150].xy = (probe width, probe height).
//   VS  = native game VS (not specified by the ini section),
//   GS/PS = this file, ib = identity index buffer (vertex-order),
//   o0 = probe render target (RGBA32F), topology = point_list.
Texture1D<float4> IniParams : register(t120);

struct VSOutput {
    float4 pos : SV_Position;
};

struct GSOutput {
    float4 pos : SV_Position;
    nointerpolation float4 nativeClip : TEXCOORD0;
};

#ifdef GEOMETRY_SHADER
[maxvertexcount(4)]
void main(point VSOutput input[1], uint pointId : SV_PrimitiveID,
          inout TriangleStream<GSOutput> stream) {
    uint width = (uint)IniParams[150].x;
    uint height = (uint)IniParams[150].y;
    uint x = pointId % width;
    uint y = pointId / width;
    if (y >= height) {
        return;
    }
    float2 lo = float2(2.0 * x / width - 1.0, 1.0 - 2.0 * y / height);
    float2 size = float2(2.0 / width, -2.0 / height);

    GSOutput o;
    o.nativeClip = input[0].pos;
    o.pos = float4(lo, 0, 1);
    stream.Append(o);
    o.pos = float4(lo + float2(size.x, 0), 0, 1);
    stream.Append(o);
    o.pos = float4(lo + float2(0, size.y), 0, 1);
    stream.Append(o);
    o.pos = float4(lo + size, 0, 1);
    stream.Append(o);
    stream.RestartStrip();
}
#endif

#ifdef PIXEL_SHADER
float4 main(GSOutput input) : SV_Target0 {
    return input.nativeClip;
}
#endif
