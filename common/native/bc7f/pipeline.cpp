// Native memory-only PNG -> mip chain -> BC7 DDS pipeline.
// BC7f/Basis Universal: Apache-2.0. stb_image retains its own license below.
#include <algorithm>
#include <array>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <limits>
#include <vector>
#define STB_IMAGE_IMPLEMENTATION
#define STBI_ONLY_PNG
#define STBI_NO_STDIO
#include "stb_image.h"

extern "C" int herta_bc7f_encode(const unsigned char*,size_t,unsigned,unsigned,unsigned,unsigned char*,size_t,unsigned);
using Byte=unsigned char;
using Clock=std::chrono::steady_clock;
struct Profile {
    double decode_s,mip_s,encode_s;
    uint32_t width,height,levels,source_srgb;
};
struct Level { unsigned w,h; std::vector<Byte> rgba; };
static double elapsed(Clock::time_point start) { return std::chrono::duration<double>(Clock::now()-start).count(); }
static uint32_t be32(const Byte* p) {return uint32_t(p[0])<<24|uint32_t(p[1])<<16|uint32_t(p[2])<<8|p[3];}
static void put32(Byte* p,uint32_t v) { std::memcpy(p,&v,4); }
static bool pow2(unsigned v) {return v && !(v&(v-1));}

// -1 means unsupported/truncated input; metadata is never changed on disk.
static int source_srgb(const Byte* p,size_t n) {
    static const Byte sig[]={137,80,78,71,13,10,26,10};
    if(n<33 || std::memcmp(p,sig,8) || std::memcmp(p+12,"IHDR",4) || be32(p+8)!=13) return -1;
    // Keep higher precision and unfamiliar colorspaces on the existing path.
    if((p[24]!=8 && !((p[25]==0 || p[25]==3) && (p[24]==1 || p[24]==2 || p[24]==4))) || p[28]>1) return -1;
    bool srgb=false;
    for(size_t pos=8;pos+12<=n;) {
        uint32_t len=be32(p+pos);
        if(size_t(len)>n-pos-12) return -1;
        const Byte* type=p+pos+4;const Byte* data=p+pos+8;
        if(!std::memcmp(type,"sRGB",4)) {if(len!=1 || data[0]>3)return -1;srgb=true;}
        if(!std::memcmp(type,"gAMA",4)) {
            if(len!=4)return -1;
            uint32_t g=be32(data);
            if(g==45455)srgb=true;
            else if(g!=100000)return -1;
        }
        if(!std::memcmp(type,"iCCP",4)) return -1;
        if(!std::memcmp(type,"IDAT",4))return srgb?1:0;
        pos+=size_t(len)+12;
    }
    return -1;
}

struct GammaLut {
    float linear[256];Byte encoded[65536],unpremult[65536];
    GammaLut() {
        for(unsigned i=0;i<256;++i) {
            float x=i/255.0f;linear[i]=x<=.04045f?x/12.92f:std::pow((x+.055f)/1.055f,2.4f);
        }
        for(unsigned i=0;i<65536;++i) {
            float x=i/65535.0f,y=x<=.0031308f?12.92f*x:1.055f*std::pow(x,1.0f/2.4f)-.055f;
            encoded[i]=Byte(std::min(255.0f,std::floor(y*255.0f+.5f)));
            unsigned alpha=i>>8,premult=i&255;
            unpremult[i]=alpha?Byte(std::min(255u,(premult*255+alpha/2)/alpha)):0;
        }
    }
};
static const GammaLut& gamma_lut() {static const GammaLut table;return table;}

static std::vector<Level> make_mips(Level base,bool srgb) {
    std::vector<Level> result;result.push_back(std::move(base));
    // UNORM/WIC generates each level from the base. Keep exact integer sums
    // between levels to avoid accumulating quantization from prior 8-bit mips.
    using Sum=std::array<uint64_t,4>;
    std::vector<Sum> previous_sums;
    uint64_t area=1;
    unsigned area_shift=0;
    const auto& lut=gamma_lut();
    while(result.back().w>1 || result.back().h>1) {
        const Level& prev=result.back();unsigned w=std::max(1u,prev.w/2),h=std::max(1u,prev.h/2);
        Level next{w,h,std::vector<Byte>(size_t(w)*h*4)};
        unsigned nx=prev.w>1?2:1,ny=prev.h>1?2:1,samples=nx*ny;
        std::vector<Sum> sums;if(!srgb)sums.resize(size_t(w)*h);
        uint64_t next_area=area*samples;
        unsigned next_shift=area_shift+(nx==2)+(ny==2);
        for(unsigned y=0;y<h;++y) for(unsigned x=0;x<w;++x) {
            Byte* dest=next.rgba.data()+(size_t(y)*w+x)*4;
            if(srgb) {
                float rgb[3]={};unsigned alpha=0;
                for(unsigned dy=0;dy<ny;++dy) for(unsigned dx=0;dx<nx;++dx) {
                    const Byte* src=prev.rgba.data()+(size_t(y*ny+dy)*prev.w+x*nx+dx)*4;
                    for(unsigned c=0;c<3;++c)rgb[c]+=lut.linear[src[c]];
                    alpha+=src[3];
                }
                for(unsigned c=0;c<3;++c) {
                    unsigned idx=unsigned(std::min(65535.0f,std::floor(rgb[c]/samples*65535.0f+.5f)));
                    dest[c]=lut.encoded[idx];
                }
                // DirectXMath stores UNORM using round-to-nearest-even.
                dest[3]=Byte(std::nearbyint(float(alpha)/samples));
            } else {
                Sum total={};
                for(unsigned dy=0;dy<ny;++dy) for(unsigned dx=0;dx<nx;++dx) {
                    size_t p=size_t(y*ny+dy)*prev.w+x*nx+dx;
                    for(unsigned c=0;c<4;++c) {
                        // WIC/Fant filters in 8-bit premultiplied color, then
                        // unpremultiplies after quantizing the target alpha.
                        uint64_t value=prev.rgba[p*4+c];
                        if(previous_sums.empty() && c<3)value=(value*prev.rgba[p*4+3]+127)/255;
                        total[c]+=previous_sums.empty()?value:previous_sums[p][c];
                    }
                }
                sums[size_t(y)*w+x]=total;
                dest[3]=Byte((total[3]+next_area/2)>>next_shift);
                for(unsigned c=0;c<3;++c) {
                    unsigned premult=unsigned((total[c]+next_area/2)>>next_shift);
                    dest[c]=lut.unpremult[unsigned(dest[3])*256+premult];
                }
            }
        }
        previous_sums=std::move(sums);area=next_area;area_shift=next_shift;result.push_back(std::move(next));
    }
    return result;
}

// Returns 1 on success, negative status otherwise. output_mode: 0 BC7 DDS,
// 1 uncompressed RGBA DDS for validation. Call herta_pipeline_free on success.
// The caller must initialize BC7f once before any concurrent calls.
extern "C" __declspec(dllexport) int herta_png_to_dds(
    const Byte* png,size_t bytes,unsigned output_srgb,int mip_srgb_override,
    unsigned profile,unsigned output_mode,Byte** output,size_t* output_size,Profile* timing) noexcept {
    if(output)*output=nullptr;if(output_size)*output_size=0;if(timing)*timing={};
    if(!png||!output||!output_size||!timing||bytes>INT_MAX||output_srgb>1||mip_srgb_override < -1||mip_srgb_override>1||profile>3||output_mode>1)return -1;
    try {
        int tagged=source_srgb(png,bytes);if(tagged<0)return -2;
        unsigned width=be32(png+16),height=be32(png+20);
        // NPOT requires the original WIC/Fant filtering semantics; do not
        // silently substitute a different filter in this experimental path.
        if(!pow2(width)||!pow2(height)||width>16384||height>16384)return -3;
        auto start=Clock::now();int w=0,h=0,channels=0;
        Byte* decoded=stbi_load_from_memory(png,int(bytes),&w,&h,&channels,4);
        if(!decoded)return -4;
        if(unsigned(w)!=width||unsigned(h)!=height){stbi_image_free(decoded);return -4;}
        Level base{width,height,{}};
        try {base.rgba.assign(decoded,decoded+size_t(width)*height*4);}catch(...){stbi_image_free(decoded);throw;}
        stbi_image_free(decoded);
        timing->decode_s=elapsed(start);timing->width=width;timing->height=height;
        bool srgb=mip_srgb_override<0?tagged!=0:mip_srgb_override!=0;timing->source_srgb=srgb;
        start=Clock::now();auto levels=make_mips(std::move(base),srgb);
        timing->mip_s=elapsed(start);timing->levels=unsigned(levels.size());
        size_t size=148;
        for(const auto& level:levels)size+=output_mode?size_t(level.w)*level.h*4:size_t((level.w+3)/4)*((level.h+3)/4)*16;
        Byte* data=static_cast<Byte*>(std::calloc(size,1));if(!data)return -5;
        std::memcpy(data,"DDS ",4);put32(data+4,124);
        put32(data+8,output_mode?0x2100F:0xA1007);put32(data+12,height);put32(data+16,width);
        put32(data+20,output_mode?width*4:((width+3)/4)*((height+3)/4)*16);put32(data+28,unsigned(levels.size()));
        put32(data+76,32);put32(data+80,4);std::memcpy(data+84,"DX10",4);
        put32(data+108,levels.size()>1?0x401008:0x1000);
        put32(data+128,output_mode?(srgb?29:28):(output_srgb?99:98));put32(data+132,3);put32(data+140,1);
        bool opaque=true;for(size_t i=3;i<levels[0].rgba.size();i+=4)if(levels[0].rgba[i]!=255){opaque=false;break;}
        put32(data+144,opaque?3:1);
        start=Clock::now();size_t offset=148;
        for(const auto& level:levels) {
            size_t n=output_mode?level.rgba.size():size_t((level.w+3)/4)*((level.h+3)/4)*16;
            if(output_mode)std::memcpy(data+offset,level.rgba.data(),n);
            else if(!herta_bc7f_encode(level.rgba.data(),level.rgba.size(),level.w,level.h,level.w*4,data+offset,n,profile)){
                std::free(data);return -6;
            }
            offset+=n;
        }
        timing->encode_s=elapsed(start);*output=data;*output_size=size;return 1;
    } catch(...) {return -5;}
}
extern "C" __declspec(dllexport) void herta_pipeline_free(void* memory) noexcept {std::free(memory);}
extern "C" __declspec(dllexport) unsigned herta_texture_abi() noexcept {return 1;}
