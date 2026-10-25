// Lossless export kernels. C ABI, x64 SSE2, strict double arithmetic.
#include <algorithm>
#include <atomic>
#include <cmath>
#include <cstdint>
#include <cstring>
#include <limits>
#include <thread>
#include <vector>

struct Stats {uint64_t max_influences,over_vertices;double max_dropped;uint64_t sample_count,samples[3];};
struct Sum {
    double hi=0,lo=0;
    void add(double x) {double t=hi+x;lo+=std::abs(hi)>=std::abs(x)?(hi-t)+x:(x-t)+hi;hi=t;}
    double value()const {return lo && std::isfinite(lo)?hi+lo:hi;}
};
struct Influence {double weight;uint32_t group;};

extern "C" __declspec(dllexport) unsigned herta_export_geometry_abi() noexcept {return 1;}
extern "C" __declspec(dllexport) int herta_blend_layout(
    const uint64_t* offsets,const int64_t* groups,const double* weights,uint64_t vertices,uint64_t assignments,
    const int64_t* loops,uint64_t loop_count,unsigned channels,int normalize,unsigned threads,
    float* out_weights,uint32_t* out_indices,Stats* stats) noexcept {
    if(!offsets||!stats||!channels||channels>256||offsets[0]!=0||offsets[vertices]!=assignments)return 0;
    if((assignments&&(!groups||!weights))||(loop_count&&(!loops||!out_weights||!out_indices)))return 0;
    for(uint64_t v=0;v<vertices;++v)if(offsets[v]>offsets[v+1])return 0;
    try {
        std::vector<float> vw(size_t(vertices)*channels,0);
        std::vector<uint32_t> vi(size_t(vertices)*channels,0);
        threads=std::max(1u,std::min(threads,8u));
        if(vertices<8192)threads=1;
        std::vector<Stats> local(threads);
        std::atomic<bool> failed{false};
        auto process=[&](unsigned worker){
            try {
            auto& s=local[worker];std::vector<Influence> valid;std::vector<double> diagnostic;
            valid.reserve(32);diagnostic.reserve(32);
            for(uint64_t v=vertices*worker/threads;v<vertices*(worker+1)/threads;++v){
                valid.clear();diagnostic.clear();Sum all;
                for(uint64_t a=offsets[v];a<offsets[v+1];++a){
                    double w=weights[a];if(!std::isfinite(w)||w<=0)continue;
                    diagnostic.push_back(w);all.add(w);
                    if(groups[a]>=0)valid.push_back({w,uint32_t(groups[a])});
                }
                s.max_influences=std::max(s.max_influences,uint64_t(diagnostic.size()));
                if(diagnostic.size()>channels){
                    ++s.over_vertices;if(s.sample_count<3)s.samples[s.sample_count++]=v;
                    std::sort(diagnostic.begin(),diagnostic.end(),std::greater<double>());
                    Sum kept;for(unsigned k=0;k<channels;++k)kept.add(diagnostic[k]);
                    double total=all.value();
                    if(total>0)s.max_dropped=std::max(s.max_dropped,std::max(0.0,(total-kept.value())/total));
                }
                std::stable_sort(valid.begin(),valid.end(),[](const Influence& a,const Influence& b){
                    return a.weight>b.weight||(a.weight==b.weight&&a.group<b.group);});
                size_t count=std::min(size_t(channels),valid.size());
                if(!count){vw[size_t(v)*channels]=1;continue;}
                Sum selected;for(size_t k=0;k<count;++k)selected.add(valid[k].weight);
                double divisor=normalize&&selected.value()>0?selected.value():1.0;
                for(size_t k=0;k<count;++k){vi[size_t(v)*channels+k]=valid[k].group;vw[size_t(v)*channels+k]=float(valid[k].weight/divisor);}
            }
            }catch(...){failed.store(true);}
        };
        // jthread joins on exception as well, avoiding joinable thread destruction.
        {std::vector<std::jthread> workers;for(unsigned i=1;i<threads;++i)workers.emplace_back(process,i);process(0);}
        if(failed.load())return 0;
        *stats={};
        for(const auto& s:local){
            stats->max_influences=std::max(stats->max_influences,s.max_influences);
            stats->over_vertices+=s.over_vertices;stats->max_dropped=std::max(stats->max_dropped,s.max_dropped);
            for(uint64_t i=0;i<s.sample_count&&stats->sample_count<3;++i)stats->samples[stats->sample_count++]=s.samples[i];
        }
        for(uint64_t l=0;l<loop_count;++l){
            if(loops[l]<0||uint64_t(loops[l])>=vertices){
                std::memset(out_weights+size_t(l)*channels,0,channels*sizeof(float));
                std::memset(out_indices+size_t(l)*channels,0,channels*sizeof(uint32_t));continue;}
            std::memcpy(out_weights+size_t(l)*channels,vw.data()+size_t(loops[l])*channels,channels*sizeof(float));
            std::memcpy(out_indices+size_t(l)*channels,vi.data()+size_t(loops[l])*channels,channels*sizeof(uint32_t));
        }
        return 1;
    }catch(...){return 0;}
}

static uint64_t hash_row(const unsigned char* p,size_t size){
    uint64_t h=0x9e3779b97f4a7c15ULL^size;
    while(size>=8){uint64_t word;std::memcpy(&word,p,8);word*=0x9ddfea08eb382d69ULL;word^=word>>47;h^=word;h=(h<<27)|(h>>37);h=h*5+0x52dce729;p+=8;size-=8;}
    uint64_t tail=0;std::memcpy(&tail,p,size);h^=tail;h^=h>>33;h*=0xff51afd7ed558ccdULL;h^=h>>33;h*=0xc4ceb9fe1a85ec53ULL;return h^(h>>33);
}
extern "C" __declspec(dllexport) int herta_stable_unique(const unsigned char* rows,uint64_t count,uint64_t width,
        int64_t* first,int64_t* inverse,uint64_t* unique_count) noexcept {
    if(!unique_count)return 0;*unique_count=0;if(!count)return 1;
    if(!rows||!first||!inverse||!width||count>SIZE_MAX/width||count>SIZE_MAX/4)return 0;
    try{
        size_t capacity=8;while(capacity<count*2){if(capacity>SIZE_MAX/2)return 0;capacity*=2;}
        struct Slot{uint64_t hash,row;};
        std::vector<Slot> table(capacity,Slot{0,UINT64_MAX});
        uint64_t used=0;
        for(uint64_t row=0;row<count;++row){
            const auto data=rows+size_t(row)*width;uint64_t h=hash_row(data,size_t(width));size_t at=h&(capacity-1);
            while(true){auto& slot=table[at];
                if(slot.row==UINT64_MAX){slot={h,row};first[used]=int64_t(row);inverse[row]=int64_t(used++);break;}
                if(slot.hash==h&&!std::memcmp(data,rows+size_t(slot.row)*width,size_t(width))){inverse[row]=inverse[slot.row];break;}
                at=(at+1)&(capacity-1);
            }
        }
        *unique_count=used;return 1;
    }catch(...){return 0;}
}
