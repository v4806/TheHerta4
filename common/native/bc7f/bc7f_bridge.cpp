/* Experimental C ABI around the Apache-2.0 Basis Universal bc7f encoder.
 * Reference commit: ee771a7f913a3233a252eb6d281b4446f6a3c9f5.
 * Initialize once on the calling thread before concurrent encode calls.
 */
#include "basisu_transcoder.h"
#include "basisu_transcoder_internal.h"
#include <cstring>
#include <limits>

extern "C" __declspec(dllexport) unsigned herta_bc7f_init() {
    basist::basisu_transcoder_init();
    return 1;
}

extern "C" __declspec(dllexport) int herta_bc7f_encode(
    const unsigned char *rgba, size_t bytes, unsigned width, unsigned height,
    unsigned stride, unsigned char *output, size_t capacity, unsigned profile) {
    if (!rgba || !output || !width || !height || (uint64_t)width*4>stride ||
        (uint64_t)stride*height>bytes) return 0;
    const uint64_t columns=((uint64_t)width+3)/4, rows=((uint64_t)height+3)/4;
    if (columns*rows>std::numeric_limits<size_t>::max()/16 || capacity<columns*rows*16) return 0;
    using namespace basist::bc7f;
    unsigned flags=profile==0 ? cPackBC7FlagDefaultFast : profile==1 ? cPackBC7FlagDefault :
                   profile==2 ? cPackBC7FlagDefaultPartiallyAnalytical : cPackBC7FlagDefaultFastest;
    // Preserve fully opaque alpha exactly: do not enable the optional mode-6
    // optimization documented as allowing alpha 254 on opaque source blocks.
    flags &= ~cPackBC7FlagPBitOptMode6;
    static_assert(sizeof(basist::color_rgba)==4,"Unexpected RGBA layout");
    basist::color_rgba block[16];
    for (uint64_t by=0;by<rows;++by) for (uint64_t bx=0;bx<columns;++bx) {
        for (unsigned y=0;y<4;++y) for (unsigned x=0;x<4;++x) {
            const unsigned sy=(unsigned)((by*4+y)<height ? by*4+y : height-1);
            const unsigned sx=(unsigned)((bx*4+x)<width ? bx*4+x : width-1);
            std::memcpy(&block[y*4+x],rgba+(size_t)sy*stride+(size_t)sx*4,4);
        }
        fast_pack_bc7_auto_rgba(output+(by*columns+bx)*16,block,flags);
    }
    return 1;
}
