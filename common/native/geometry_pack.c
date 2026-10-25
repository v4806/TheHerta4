/* Lossless bulk packing for TheHerta4's original object-data hash stream.
 * Windows x64, no CRT, allocation or shared mutable state. GPL-3.0.
 * Hashing itself remains in Python hashlib's native implementation.
 */
typedef unsigned char u8;
typedef unsigned int u32;
typedef int i32;
typedef unsigned __int64 usize;
#define EXPORT __declspec(dllexport)

EXPORT unsigned herta_geometry_abi(void) { return 1; }

EXPORT int herta_pack_weight_records(
    const u32 *counts, usize vertices,
    const i32 *group_ids, const float *weights, usize records,
    u8 *output, usize capacity) {
    usize total=0, cursor=0, vertex, j;
    if(vertices>2147483648ULL || records>((usize)-1)/12 || capacity<records*12) return 0;
    if((vertices && !counts) || (records && (!group_ids || !weights || !output))) return 0;
    for(vertex=0;vertex<vertices;++vertex) {
        if(counts[vertex]>records-total)return 0;
        total+=counts[vertex];
    }
    if(total!=records)return 0;
    for(vertex=0;vertex<vertices;++vertex) {
        u32 vi=(u32)vertex;
        for(j=0;j<counts[vertex];++j,++cursor) {
            const u8 *v=(const u8*)&vi;
            const u8 *g=(const u8*)&group_ids[cursor];
            const u8 *w=(const u8*)&weights[cursor];
            u8 *d=output+cursor*12;
            d[0]=v[0];d[1]=v[1];d[2]=v[2];d[3]=v[3];
            d[4]=g[0];d[5]=g[1];d[6]=g[2];d[7]=g[3];
            d[8]=w[0];d[9]=w[1];d[10]=w[2];d[11]=w[3];
        }
    }
    return 1;
}
