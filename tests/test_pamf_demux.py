"""Compile the production AC3 packet reader: split frames, bounds, tracks and cancellation."""
from pathlib import Path
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]

def main():
    source = r'''
#include "tj_pamf_audio.h"
#include <cstdio>
#include <sstream>
#include <vector>
static unsigned checks;
#define CHECK(c) do{++checks;if(!(c)){fprintf(stderr,"FAIL line %d\n",__LINE__);return 1;}}while(0)
using Bytes=std::vector<uint8_t>;
static Bytes pes(uint8_t id,const Bytes& body) {
    Bytes b={0,0,1,id,uint8_t(body.size()>>8),uint8_t(body.size())};
    b.insert(b.end(),body.begin(),body.end());return b;
}
static void append(Bytes& dst,const Bytes& src){dst.insert(dst.end(),src.begin(),src.end());}
static bool demux(const Bytes& bytes,size_t offset,size_t size,Bytes& out,
                  bool cancel=false,bool accept=true,uint8_t track=0x30) {
    std::istringstream file(std::string(bytes.begin(),bytes.end()));std::string error;
    return tj_pamf_demux_ac3(file,offset,size,track,
        [&](const uint8_t* p,size_t n){if(!accept)return false;out.insert(out.end(),p,p+n);return true;},
        [&]{return cancel;},error);
}
int main() {
    // PTS90000, Sony private header, first half of one elementary AC3 frame.
    Bytes body={0x81,0x80,5,0x21,0,5,0xBF,0x21,0x30,0,0,0,0x0B,0x77,1,2};
    TjPamfAc3Packet p;
    CHECK(tj_pamf_ac3_packet(body.data(),body.size(),p));
    CHECK(p.substream==0x30 && p.has_pts && p.pts==90000 && p.size==4 && p.data[0]==0x0B);
    for(size_t n=0;n<=12;++n)CHECK(!tj_pamf_ac3_packet(body.data(),n,p));
    Bytes bad=body;bad[0]=0;CHECK(!tj_pamf_ac3_packet(bad.data(),bad.size(),p));
    bad=body;bad[2]=255;CHECK(!tj_pamf_ac3_packet(bad.data(),bad.size(),p));
    bad=body;bad[8]=0x80;CHECK(!tj_pamf_ac3_packet(bad.data(),bad.size(),p));
    bad=body;bad[1]=0;CHECK(tj_pamf_ac3_packet(bad.data(),bad.size(),p) && !p.has_pts);
    Bytes stream={9,9,9,9,9};const size_t offset=stream.size();
    // MPEG2 pack with three stuffing bytes.
    append(stream,{0,0,1,0xBA,0x44,0,0,0,0,0,0,0,0,3,0xFF,0xFF,0xFF});
    append(stream,pes(0xBB,{1,2,3}));
    // False packet prefixes inside video must never be interpreted.
    Bytes video={0,0,1,0xBD};append(video,body);append(stream,pes(0xE0,video));
    append(stream,pes(0xBD,body));
    Bytes other=body;other[8]=0x31;append(stream,pes(0xBD,other));
    // Next PES begins mid-frame; remove its header without requiring a new sync word.
    Bytes tail={0x80,0,0,0x30,0,0,0,3,4,0,0,1,0xBD};
    append(stream,pes(0xBD,tail));append(stream,{0,0,1,0xB9});
    const size_t length=stream.size()-offset;append(stream,{0xA5,0xA5});
    Bytes out;
    CHECK(demux(stream,offset,length,out));
    CHECK(out==Bytes({0x0B,0x77,1,2,3,4,0,0,1,0xBD}));
    out.clear();CHECK(demux(stream,offset,length,out,false,true,0x31));
    CHECK(out==Bytes({0x0B,0x77,1,2}));
    out.clear();CHECK(!demux(stream,offset,length,out,true) && out.empty());
    CHECK(!demux(stream,offset,length,out,false,false) && out.empty());
    CHECK(!demux(stream,offset,length,out,false,true,0x80));
    Bytes single=pes(0xBD,body);
    for(size_t n=1;n<single.size();++n) {
        Bytes truncated(single.begin(),single.begin()+n);out.clear();
        CHECK(!demux(truncated,0,single.size(),out) && out.empty());
        CHECK(!demux(single,0,n,out) && out.empty());
    }
    CHECK(!demux(Bytes({0,0,1,0xBD,0,0}),0,6,out));
    CHECK(!demux(Bytes({0,1,1,0xBD,0,1,1}),0,7,out));
    bad=stream;bad[offset+4]=0;CHECK(!demux(bad,offset,length,out));
    printf("PAMF AC3 demux: %u/%u checks passed\n",checks,checks);
    return 0;
}
'''
    compiler = shutil.which("clang++") or r"C:\Program Files\LLVM\bin\clang++.exe"
    with tempfile.TemporaryDirectory(prefix="tj-pamf-demux-") as temp:
        cpp, exe = Path(temp) / "demux.cpp", Path(temp) / "demux.exe"
        cpp.write_text(source, encoding="utf-8")
        subprocess.run([compiler, "-std=c++17", "-O2", "-I", str(ROOT / "src"),
                        str(cpp), "-o", str(exe)], check=True)
        subprocess.run([str(exe)], check=True)

if __name__ == "__main__":
    main()
