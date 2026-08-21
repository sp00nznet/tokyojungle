#!/usr/bin/env python3
"""Rewrite src/hle_imports.h's import-table slot addresses from the ELF itself.

The slot numbers in hle_imports.h were written assuming an 8-byte import
stride. The table is 4-byte (see src/import_resolver.h), so most of them
point at the wrong import -- cellGcmGetConfiguration was bound to the slot
that really belongs to a different call, so the game's GCM heap manager read
a zeroed config and died with "Out of Local Memory".

The binary carries the answer. Each imported module has a 0x2C-byte stub
descriptor (magic 0x2C000001) holding, at +0x06 the function count, +0x10 the
module name, +0x14 the NID table and +0x18 the STUB CODE table. Stub i is a
32-byte thunk whose third instruction is `lwz r12, disp(r12)`, and
0x340000+disp is the import slot that thunk reads -- so name -> stub -> slot
is exact, with no index-alignment guessing.

Resolving each NID through ps3recomp's NID database gives slot -> name, and
HLE_ADDR_<X> -> name is the (obvious) table below. Idempotent: rerunning on
corrected source rewrites the same values.
"""
import re, struct, sys

sys.path.insert(0, r'G:/recomp/ps3games/ps3recomp/tools')
from nid_database import get_default_db

ELF = 'input/EBOOT.ELF'
SRC = 'src/hle_imports.h'
DESC_MAGIC = 0x2C000001
NAME_LO, NAME_HI = 0x0025FD20, 0x0025FE9C

# HLE_ADDR_<suffix> -> the library function it implements.
HANDLERS = {
    "LWMUTEX_LOCK": "sys_lwmutex_lock",
    "LWMUTEX_UNLOCK": "sys_lwmutex_unlock",
    "LWMUTEX_CREATE": "sys_lwmutex_create",
    "LWMUTEX_DESTROY": "sys_lwmutex_destroy",
    "PROCESS_ATEXIT": "_sys_process_atexitspawn",
    "PROCESS_AT_EXIT": "_sys_process_at_Exitspawn",
    "PROCESS_EXIT": "sys_process_exit",
    "THREAD_GET_ID": "sys_ppu_thread_get_id",
    "THREAD_ONCE": "sys_ppu_thread_once",
    "THREAD_EXIT": "sys_ppu_thread_exit",
    "THREAD_CREATE": "sys_ppu_thread_create",
    "PRX_REG_LIB": "sys_prx_register_library",
    "PRX_EXITSPAWN": "sys_prx_exitspawn_with_level",
    "SPU_PRINTF_INIT": "_sys_spu_printf_initialize",
    "SPU_PRINTF_FIN": "_sys_spu_printf_finalize",
    "TIME_GET": "sys_time_get_system_time",
    "GCM_INIT": "_cellGcmInitBody",
    "GCM_ADDR_TO_OFFSET": "cellGcmAddressToOffset",
    "GCM_SET_FLIP_MODE": "cellGcmSetFlipMode",
    "GCM_GET_FLIP_STATUS": "cellGcmGetFlipStatus",
    "GCM_RESET_FLIP": "cellGcmResetFlipStatus",
    "GCM_BIND_TILE": "cellGcmBindTile",
    "GCM_BIND_ZCULL": "cellGcmBindZcull",
    "GCM_MAP_MAIN_MEM": "cellGcmMapMainMemory",
    "GCM_MAP_EA_IO": "cellGcmMapEaIoAddress",
    "GCM_MAP_EA_IO_FLAGS": "cellGcmMapEaIoAddressWithFlags",
    "GCM_GET_CONFIG": "cellGcmGetConfiguration",
    "GCM_GET_CTRL_REG": "cellGcmGetControlRegister",
    "GCM_GET_LABEL_ADDR": "cellGcmGetLabelAddress",
    "GCM_SET_TILE_INFO": "cellGcmSetTileInfo",
    "GCM_SET_FLIP_HANDLER": "cellGcmSetFlipHandler",
    "GCM_SET_VBLANK_HANDLER": "cellGcmSetVBlankHandler",
    "GCM_SET_USER_HANDLER": "cellGcmSetUserHandler",
    "GCM_GET_TILED_PITCH": "cellGcmGetTiledPitchSize",
    "GCM_SET_DISPLAY_BUF": "cellGcmSetDisplayBuffer",
    "SYSUTIL_REG_CB": "cellSysutilRegisterCallback",
    "SYSUTIL_CHECK_CB": "cellSysutilCheckCallback",
    "SYSUTIL_GET_PARAM_INT": "cellSysutilGetSystemParamInt",
    "VIDEOOUT_GET_STATE": "cellVideoOutGetState",
    "VIDEOOUT_GET_RES": "cellVideoOutGetResolution",
    "VIDEOOUT_CONFIGURE": "cellVideoOutConfigure",
    "SYSMOD_LOAD": "cellSysmoduleLoadModule",
    "SYSMOD_UNLOAD": "cellSysmoduleUnloadModule",
    "PAD_INIT": "cellPadInit",
    "PAD_END": "cellPadEnd",
    "PAD_GET_DATA": "cellPadGetData",
    "PAD_GET_INFO2": "cellPadGetInfo2",
    "PAD_SET_PORT": "cellPadSetPortSetting",
    "AUDIO_INIT": "cellAudioInit",
    "AUDIO_QUIT": "cellAudioQuit",
    "AUDIO_PORT_OPEN": "cellAudioPortOpen",
    "AUDIO_PORT_START": "cellAudioPortStart",
    "AUDIO_PORT_STOP": "cellAudioPortStop",
    "AUDIO_PORT_CLOSE": "cellAudioPortClose",
    "AUDIO_GET_PORT_CFG": "cellAudioGetPortConfig",
    "GAME_BOOT_CHECK": "cellGameBootCheck",
    "GAME_CONTENT_PERMIT": "cellGameContentPermit",
    "GAME_GET_PARAM_INT": "cellGameGetParamInt",
}


def load_segments(d):
    phoff = struct.unpack('>Q', d[0x20:0x28])[0]
    phnum = struct.unpack('>H', d[0x38:0x3a])[0]
    segs = []
    for i in range(phnum):
        o = phoff + i * 56
        if struct.unpack('>I', d[o:o + 4])[0] != 1:
            continue
        off, va = struct.unpack('>QQ', d[o + 8:o + 24])
        fsz = struct.unpack('>Q', d[o + 32:o + 40])[0]
        segs.append((va, off, fsz))
    return segs


def main():
    d = open(ELF, 'rb').read()
    segs = load_segments(d)

    def rd32(va):
        for v, o, f in segs:
            if v <= va < v + f:
                return struct.unpack('>I', d[o + (va - v):o + (va - v) + 4])[0]
        return 0

    def u16(va):
        for v, o, f in segs:
            if v <= va < v + f:
                return struct.unpack('>H', d[o + (va - v):o + (va - v) + 2])[0]
        return 0

    by_nid = getattr(get_default_db(), '_by_nid')
    name_to_slot = {}
    slot_to_nid = {}
    descs = 0
    # The descriptors sit in the read-only (code) segment, alongside the module
    # name table, not in .data.
    for lo, hi in ((0x00010000, 0x00336268), (0x00340000, 0x003BED80)):
        va = lo
        while va < hi - 0x2C:
            if rd32(va) != DESC_MAGIC or not (NAME_LO <= rd32(va + 0x10) < NAME_HI):
                va += 4
                continue
            descs += 1
            count = u16(va + 0x06)
            nid_tbl, stub_tbl = rd32(va + 0x14), rd32(va + 0x18)
            for i in range(count):
                nid = rd32(nid_tbl + i * 4)
                stub = rd32(stub_tbl + i * 4)
                entry = by_nid.get(nid)
                if not entry or not stub:
                    continue
                # third instruction of the thunk: lwz r12, disp(r12)
                slot = 0x340000 + (rd32(stub + 8) & 0xFFFF)
                name_to_slot.setdefault(entry[1], slot)
                slot_to_nid.setdefault(slot, nid)
            va += 0x2C
    print("import table: %d module descriptors, %d names resolved"
          % (descs, len(name_to_slot)))

    # Emit slot -> {nid, name} for the runtime. The name makes an unimplemented
    # import self-describing in the log; the NID lets the boot harness ask
    # ps3recomp's HLE (ps3_hle_has / ps3_hle_call) whether it implements this
    # import for real, instead of falling back to a return-0 stub.
    hdr = 'generated/tj_import_names.h'
    L = []
    L.append('/* Auto-generated by scripts/fix_import_slots.py -- do not edit. */')
    L.append('#pragma once')
    L.append('')
    L.append('typedef struct {')
    L.append('    unsigned int slot;')
    L.append('    unsigned int nid;')
    L.append('    const char*  name;')
    L.append('} tj_import_entry;')
    L.append('')
    L.append('static const tj_import_entry g_tj_import_names[] = {')
    for nm in sorted(name_to_slot, key=lambda k: name_to_slot[k]):
        sl = name_to_slot[nm]
        L.append('    { 0x%06Xu, 0x%08Xu, "%s" },' % (sl, slot_to_nid.get(sl, 0), nm))
    L.append('};')
    L.append('static const int g_tj_import_name_count =')
    L.append('    (int)(sizeof(g_tj_import_names) / sizeof(g_tj_import_names[0]));')
    L.append('')
    L.append('static const tj_import_entry* tj_import_find(unsigned int slot) {')
    L.append('    for (int i = 0; i < g_tj_import_name_count; i++)')
    L.append('        if (g_tj_import_names[i].slot == slot) return &g_tj_import_names[i];')
    L.append('    return 0;')
    L.append('}')
    L.append('')
    L.append('static const char* tj_import_name(unsigned int slot) {')
    L.append('    const tj_import_entry* e = tj_import_find(slot);')
    L.append('    return e ? e->name : "?";')
    L.append('}')
    L.append('')
    L.append('static unsigned int tj_import_nid(unsigned int slot) {')
    L.append('    const tj_import_entry* e = tj_import_find(slot);')
    L.append('    return e ? e->nid : 0u;')
    L.append('}')
    with open(hdr, 'w', encoding='utf-8', newline='') as f:
        f.write("\n".join(L) + "\n")
    print("wrote %s (%d names)" % (hdr, len(name_to_slot)))

    src = open(SRC, encoding='utf-8', newline='').read()
    pat = re.compile(r'resolve_import\(0x([0-9A-Fa-f]+),\s*HLE_ADDR_([A-Z0-9_]+),\s*toc\)')
    fixed = kept = unknown = 0

    def repl(m):
        nonlocal fixed, kept, unknown
        old, suffix = int(m.group(1), 16), m.group(2)
        fn = HANDLERS.get(suffix)
        if fn is None or fn not in name_to_slot:
            unknown += 1
            return m.group(0)
        new = name_to_slot[fn]
        if new == old:
            kept += 1
        else:
            fixed += 1
            print("  0x%06X -> 0x%06X  %-24s %s" % (old, new, suffix, fn))
        return 'resolve_import(0x%06X, HLE_ADDR_%s, toc)' % (new, suffix)

    out = pat.sub(repl, src)
    open(SRC, 'w', encoding='utf-8', newline='').write(out)
    print("slots: %d corrected, %d already right, %d not in the table"
          % (fixed, kept, unknown))
    return 0


if __name__ == '__main__':
    sys.exit(main())
