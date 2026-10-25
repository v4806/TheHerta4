// PNG analysis helper: raw RGBA8 and exact integer mean pooling, no color transform.
#define STB_IMAGE_IMPLEMENTATION
#define STBI_ONLY_PNG
#define STBI_NO_STDIO
#include "stb_image.h"
#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <limits>
extern "C" {
__declspec(dllexport) unsigned herta_png_analysis_abi() noexcept { return 1; }
__declspec(dllexport) int herta_png_decode(const unsigned char* input, size_t size,
        unsigned char** output, unsigned* width, unsigned* height) noexcept {
    if(!input || !output || !width || !height || size<33 || size>INT_MAX) return 0;
    *output=nullptr;
    // Only 8-bit static PNGs share the validated Blender Non-Color byte semantics.
    if(memcmp(input,"\x89PNG\r\n\x1a\n",8) || input[24]!=8) return 0;
    for(size_t at=8; at+12<=size;) {
        uint32_t count=(uint32_t(input[at])<<24)|(uint32_t(input[at+1])<<16)|
            (uint32_t(input[at+2])<<8)|input[at+3];
        if(count>size-at-12) return 0;
        if(!memcmp(input+at+4,"acTL",4)) return 0;
        at+=size_t(count)+12;
    }
    int w=0,h=0,channels=0;
    auto pixels=stbi_load_from_memory(input,int(size),&w,&h,&channels,4);
    if(!pixels || w<=0 || h<=0) {stbi_image_free(pixels);return 0;}
    *output=pixels;*width=unsigned(w);*height=unsigned(h);return 1;
}
__declspec(dllexport) int herta_rgba_pool(const unsigned char* pixels,unsigned width,unsigned height,
        unsigned grid,double* output,size_t output_count) noexcept {
    if(!pixels || !width || !height || !grid || !output) return 0;
    unsigned rows=height<grid?height:grid, cols=width<grid?width:grid;
    if(output_count!=size_t(rows)*cols*3) return 0;
    for(unsigned ry=0;ry<rows;++ry) for(unsigned cx=0;cx<cols;++cx) {
        unsigned y0=uint64_t(ry)*height/rows,y1=uint64_t(ry+1)*height/rows;
        unsigned x0=uint64_t(cx)*width/cols,x1=uint64_t(cx+1)*width/cols;
        uint64_t sums[3]={0,0,0};
        for(unsigned y=y0;y<y1;++y) for(unsigned x=x0;x<x1;++x) {
            const auto p=pixels+(size_t(y)*width+x)*4;
            for(unsigned c=0;c<3;++c) sums[c]+=p[c];
        }
        double count=double(uint64_t(y1-y0)*(x1-x0));
        for(unsigned c=0;c<3;++c) output[(size_t(ry)*cols+cx)*3+c]=double(sums[c])/count;
    }
    return 1;
}
__declspec(dllexport) void herta_png_free(void* pixels) noexcept {stbi_image_free(pixels);}
}
