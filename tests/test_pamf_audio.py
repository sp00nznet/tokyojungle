"""Exercise the actual PAMF media bridge without launching the game or audio device."""
from pathlib import Path
import argparse
import os
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("movie", type=Path)
    args = parser.parse_args()
    source = r'''
#include "tj_pamf_media.h"
#include <windows.h>
#include <chrono>
#include <cmath>
#include <cstdio>
#include <cstring>
#include <thread>
#include <vector>
int main(int argc,char** argv) {
    if(argc!=2)return 2;
    TjPamfMediaInfo info;
    if(!tj_pamf_prepare(argv[1],&info) || !tj_pamf_start())return 3;
    std::vector<unsigned char> video(size_t(info.width)*info.height*4),audio(2048);
    unsigned blocks=0,nonzero=0,invalid=0;double sum=0,peak=0;
    uint64_t pts=0,prior=0;uint32_t tag=0,frame=0;
    const auto deadline=std::chrono::steady_clock::now()+std::chrono::seconds(6);
    while(std::chrono::steady_clock::now()<deadline && blocks<200) {
        tj_pamf_pop_argb(video.data(),video.size(),&pts,&frame);
        if(tj_pamf_pop_audio_f32be(audio.data(),audio.size(),256,&pts,&tag)) {
            if(blocks && pts<=prior)++invalid;prior=pts;++blocks;
            for(size_t i=0;i<audio.size();i+=4) {
                uint32_t bits=uint32_t(audio[i])<<24|uint32_t(audio[i+1])<<16|
                              uint32_t(audio[i+2])<<8|audio[i+3];
                float sample;memcpy(&sample,&bits,4);
                if(!std::isfinite(sample))++invalid;
                else {if(fabs(sample)>0.00001)++nonzero;sum+=double(sample)*sample;
                      if(fabs(sample)>peak)peak=fabs(sample);}
            }
        }
        std::this_thread::sleep_for(std::chrono::milliseconds(1));
    }
    tj_pamf_stop();
    // Stop while output rings are full: blocked reads/writes must release.
    DWORD before=0,after=0;GetProcessHandleCount(GetCurrentProcess(),&before);
    for(unsigned i=0;i<2;++i) {
        if(!tj_pamf_start())return 4;
        std::this_thread::sleep_for(std::chrono::milliseconds(250));
        const auto start=std::chrono::steady_clock::now();tj_pamf_stop();
        if(std::chrono::steady_clock::now()-start>std::chrono::seconds(3))return 5;
    }
    GetProcessHandleCount(GetCurrentProcess(),&after);tj_pamf_close();
    const double rms=blocks?sqrt(sum/(blocks*512.0)):0;
    printf("PAMF audio blocks=%u nonzero=%u invalid=%u rms=%.6f peak=%.6f handles=%lu->%lu\n",
           blocks,nonzero,invalid,rms,peak,before,after);
    return blocks>=100 && nonzero>1000 && !invalid && rms>0.001 && peak<=1.01 && after<=before ? 0:1;
}
'''
    compiler = shutil.which("clang++") or r"C:\Program Files\LLVM\bin\clang++.exe"
    env = os.environ.copy()
    if not shutil.which("ffmpeg"):
        package = Path(os.environ["LOCALAPPDATA"]) / "Microsoft/WinGet/Packages"
        found = next(package.glob("Gyan.FFmpeg*/ffmpeg-*/bin/ffmpeg.exe"), None)
        if not found:
            raise RuntimeError("Existing FFmpeg installation required")
        env["PATH"] = str(found.parent) + os.pathsep + env.get("PATH", "")
    with tempfile.TemporaryDirectory(prefix="tj-pamf-audio-") as temp:
        cpp, exe = Path(temp) / "audio.cpp", Path(temp) / "audio.exe"
        cpp.write_text(source, encoding="utf-8")
        subprocess.run([compiler, "-std=c++17", "-O2", "-D_CRT_SECURE_NO_WARNINGS",
                        "-I", str(ROOT / "src"), str(cpp), str(ROOT / "src/tj_pamf_media.cpp"),
                        "-o", str(exe)], check=True)
        subprocess.run([str(exe), str(args.movie.resolve())], check=True, timeout=20, env=env)


if __name__ == "__main__":
    main()
