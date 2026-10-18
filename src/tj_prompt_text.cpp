/* PC key names inside the game's text (NPUA80523 v01.00).
 *
 * Every variable UI string reaches the screen through one text job,
 * func_00198590 (r4 = job; the UTF-8 string is a private 0x100-byte copy at
 * job+0x28, made by strncpy/memcpy in all three producers: 0x198A40,
 * 0x198B70 and the worker 0x1C04FC). scripts/post_lift.py inserts
 * tj_prompt_rewrite_job(job) at that function's entry, before the game
 * measures, shrinks-to-fit, centres and draws the string, so a longer
 * replacement cannot overflow its box. Only the job copy is rewritten.
 *
 * While keyboard/mouse is the active device (cellPad_prompt_keys), button
 * references become the bound key in brackets:
 *   glyphs  U+25A1 (Square), U+25B3 (Triangle), U+2715 (Cross), U+00D7
 *           (Cross, never next to a digit), a lone U+25CB (Circle; "○○" is
 *           the game's own placeholder in "Unlock ○○?" and stays)
 *   words   L1 R1 L2 R2 L3 R3; START/SELECT after "press(ing)"/"the" or
 *           before " button"; "R Stick"/"L Stick"; "directional button
 *           up/down/left/right" (one key) or "directional button(s)" (four)
 *   " button" after a replaced name becomes " key"; for a mouse button it
 *   is dropped with a leading "the " ("Press [Left Click] to claw and").
 * EN text examples: "Press the □ button to claw and" -> "Press the [E] key
 * to claw and"; "Push the directional button up to check items." -> "Push
 * [Up] to check items."; "Evade the lunge with the R Stick." -> "Evade the
 * lunge with [Num8/4/2/6].". An unbound action keeps the original words.
 * A controller (or TJ_PROMPTS=0) leaves every string untouched.
 *
 * Text already on screen keeps its old words until the game draws that line
 * again. Logs: "[tj-prompts] text hook active" once, then one
 * "[tj-prompts] text ..." line per distinct rewritten string (max 200).
 */
#include "recomp_bridge.h"

#include <mutex>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <unordered_set>
#include <windows.h>

extern "C" int cellPad_prompt_keys(unsigned long long* sig, int vks[24]);

namespace {

/* cellPad.c action order */
enum {
    ACT_CROSS, ACT_CIRCLE, ACT_SQUARE, ACT_TRIANGLE, ACT_L1, ACT_R1, ACT_L2, ACT_R2,
    ACT_L3, ACT_R3, ACT_START, ACT_SELECT, ACT_UP, ACT_DOWN, ACT_LEFT, ACT_RIGHT,
    ACT_LS_UP = 16,   /* up, down, left, right */
    ACT_RS_UP = 20,   /* up, down, left, right */
    ACT_COUNT = 24
};

const size_t kJobString = 0x28;   /* job+0x28: private UTF-8 copy */
const size_t kJobCap = 0x100;     /* strncpy limit: at most 0xFF bytes + NUL */

/* Bounded output buffer; overflow marks the rewrite as unusable. */
struct Out {
    char buf[kJobCap * 2];
    size_t n = 0;
    bool overflow = false;
    void put(const char* s, size_t len)
    {
        if (n + len >= sizeof(buf)) { overflow = true; return; }
        memcpy(buf + n, s, len);
        n += len;
    }
    void put(const char* s) { put(s, strlen(s)); }
    /* Drops a trailing "the " (whole word): "with the R Stick" -> "with [..]". */
    void drop_the()
    {
        if (n < 4) return;
        const char* t = buf + n - 4;
        if ((t[0] == 't' || t[0] == 'T') && t[1] == 'h' && t[2] == 'e' && t[3] == ' ' &&
            (n == 4 || buf[n - 5] == ' '))
            n -= 4;
    }
};

bool is_alnum(char c)
{
    return (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') || (c >= '0' && c <= '9');
}

bool is_digit(char c) { return c >= '0' && c <= '9'; }

char lower(char c) { return (c >= 'A' && c <= 'Z') ? (char)(c - 'A' + 'a') : c; }

bool starts_with(const char* s, size_t len, size_t at, const char* lit, bool fold_first = false)
{
    const size_t m = strlen(lit);
    if (at + m > len) return false;
    for (size_t k = 0; k < m; k++) {
        char a = s[at + k], b = lit[k];
        if (k == 0 && fold_first) { a = lower(a); b = lower(b); }
        if (a != b) return false;
    }
    return true;
}

bool starts_with_nocase(const char* s, size_t len, size_t at, const char* lit)
{
    const size_t m = strlen(lit);
    if (at + m > len) return false;
    for (size_t k = 0; k < m; k++)
        if (lower(s[at + k]) != lower(lit[k])) return false;
    return true;
}

bool ends_with_nocase(const char* s, size_t at, const char* lit)
{
    const size_t m = strlen(lit);
    if (at < m) return false;
    for (size_t k = 0; k < m; k++)
        if (lower(s[at - m + k]) != lower(lit[k])) return false;
    return at == m || !is_alnum(s[at - m - 1]);
}

/* Key name for text, or false when unbound. */
bool key_text(int vk, char* out, size_t cap)
{
    if (vk <= 0) return false;
    if ((vk >= 'A' && vk <= 'Z') || (vk >= '0' && vk <= '9')) {
        snprintf(out, cap, "%c", (char)vk);
        return true;
    }
    if (vk >= VK_F1 && vk <= VK_F24) { snprintf(out, cap, "F%d", vk - VK_F1 + 1); return true; }
    if (vk >= VK_NUMPAD0 && vk <= VK_NUMPAD9) { snprintf(out, cap, "Num%d", vk - VK_NUMPAD0); return true; }
    static const struct { int vk; const char* name; } names[] = {
        {VK_SPACE, "Space"}, {VK_RETURN, "Enter"}, {VK_BACK, "Backspace"}, {VK_TAB, "Tab"},
        {VK_ESCAPE, "Esc"}, {VK_CAPITAL, "Caps Lock"}, {VK_SHIFT, "Shift"}, {VK_LSHIFT, "Shift"},
        {VK_RSHIFT, "Right Shift"}, {VK_CONTROL, "Ctrl"}, {VK_LCONTROL, "Ctrl"},
        {VK_RCONTROL, "Right Ctrl"}, {VK_MENU, "Alt"}, {VK_LMENU, "Alt"}, {VK_RMENU, "Right Alt"},
        {VK_UP, "Up"}, {VK_DOWN, "Down"}, {VK_LEFT, "Left"}, {VK_RIGHT, "Right"},
        {VK_INSERT, "Insert"}, {VK_DELETE, "Delete"}, {VK_HOME, "Home"}, {VK_END, "End"},
        {VK_PRIOR, "Page Up"}, {VK_NEXT, "Page Down"}, {VK_MULTIPLY, "Num*"}, {VK_ADD, "Num+"},
        {VK_SUBTRACT, "Num-"}, {VK_DECIMAL, "Num."}, {VK_DIVIDE, "Num/"},
        {VK_OEM_1, ";"}, {VK_OEM_PLUS, "="}, {VK_OEM_COMMA, ","}, {VK_OEM_MINUS, "-"},
        {VK_OEM_PERIOD, "."}, {VK_OEM_2, "/"}, {VK_OEM_3, "`"},
        {VK_OEM_4, "Left Bracket"},      /* words: "[[]" would nest the brackets */
        {VK_OEM_5, "\\"}, {VK_OEM_6, "Right Bracket"}, {VK_OEM_7, "'"},
        {VK_LBUTTON, "Left Click"}, {VK_RBUTTON, "Right Click"}, {VK_MBUTTON, "Middle Click"},
        {VK_XBUTTON1, "Mouse 4"}, {VK_XBUTTON2, "Mouse 5"},
    };
    for (const auto& n : names)
        if (n.vk == vk) { snprintf(out, cap, "%s", n.name); return true; }
    return false;
}

bool put_key(Out& o, const int vks[ACT_COUNT], int act)
{
    char name[32];
    if (!key_text(vks[act], name, sizeof(name))) return false;
    o.put("[");
    o.put(name);
    o.put("]");
    return true;
}

/* Four directions (up, down, left, right from `first`), shown in the
 * W/A/S/D order: up, left, down, right. */
bool put_group(Out& o, const int vks[ACT_COUNT], int first)
{
    const int order[4] = {first, first + 2, first + 1, first + 3};
    const int arrows[4] = {VK_UP, VK_LEFT, VK_DOWN, VK_RIGHT};
    bool all_arrows = true, all_numpad = true;
    for (int k = 0; k < 4; k++) {
        const int vk = vks[order[k]];
        if (vk <= 0) return false;
        all_arrows = all_arrows && vk == arrows[k];
        all_numpad = all_numpad && vk >= VK_NUMPAD0 && vk <= VK_NUMPAD9;
    }
    char text[96];
    if (all_arrows) {
        snprintf(text, sizeof(text), "[Arrow keys]");
    } else if (all_numpad) {
        snprintf(text, sizeof(text), "[Num%d/%d/%d/%d]", vks[order[0]] - VK_NUMPAD0,
                 vks[order[1]] - VK_NUMPAD0, vks[order[2]] - VK_NUMPAD0, vks[order[3]] - VK_NUMPAD0);
    } else {
        char n[4][32];
        for (int k = 0; k < 4; k++)
            if (!key_text(vks[order[k]], n[k], sizeof(n[k]))) return false;
        snprintf(text, sizeof(text), "[%s/%s/%s/%s]", n[0], n[1], n[2], n[3]);
    }
    o.put(text);
    return true;
}

bool is_mouse(int vk)
{
    return vk == VK_LBUTTON || vk == VK_RBUTTON || vk == VK_MBUTTON || vk == VK_XBUTTON1 ||
           vk == VK_XBUTTON2;
}

/* Length of " button" / " buttons" at `at` (whole word), else 0. */
size_t button_word_len(const char* s, size_t len, size_t at)
{
    if (starts_with(s, len, at, " buttons") && (at + 8 == len || !is_alnum(s[at + 8]))) return 8;
    if (starts_with(s, len, at, " button") && (at + 7 == len || !is_alnum(s[at + 7]))) return 7;
    return 0;
}

const char kSquare[] = "\xE2\x96\xA1";     /* U+25A1 */
const char kTriangle[] = "\xE2\x96\xB3";   /* U+25B3 */
const char kCircle[] = "\xE2\x97\x8B";     /* U+25CB */
const char kCrossX[] = "\xE2\x9C\x95";     /* U+2715 */
const char kTimes[] = "\xC3\x97";          /* U+00D7 */

/* Returns true and fills o when at least one reference was replaced. */
bool rewrite(const char* s, size_t len, const int vks[ACT_COUNT], Out& o)
{
    bool changed = false;
    size_t i = 0;
    while (i < len && !o.overflow) {
        const bool word_start = i == 0 || !is_alnum(s[i - 1]);
        /* "directional button up" / "directional button(s)" */
        if (word_start && starts_with(s, len, i, "directional button", true)) {
            size_t at = i + 18;
            const bool plural = at < len && s[at] == 's';
            if (plural) at++;
            int dir = -1;
            static const char* const dirs[4] = {" up", " down", " left", " right"};
            if (!plural)
                for (int d = 0; d < 4 && dir < 0; d++)
                    if (starts_with(s, len, at, dirs[d]) &&
                        (at + strlen(dirs[d]) == len || !is_alnum(s[at + strlen(dirs[d])]))) {
                        dir = d;
                        at += strlen(dirs[d]);
                    }
            Out trial = o;
            trial.drop_the();
            const bool ok = dir >= 0 ? put_key(trial, vks, ACT_UP + dir) : put_group(trial, vks, ACT_UP);
            if (ok && !trial.overflow) {
                o = trial;
                i = at;
                changed = true;
                continue;
            }
        }
        /* "R Stick" / "L Stick" */
        if (word_start && (s[i] == 'R' || s[i] == 'L') && starts_with_nocase(s, len, i + 1, " stick") &&
            (i + 7 == len || !is_alnum(s[i + 7]))) {
            Out trial = o;
            trial.drop_the();
            if (put_group(trial, vks, s[i] == 'R' ? ACT_RS_UP : ACT_LS_UP) && !trial.overflow) {
                o = trial;
                i += 7;
                changed = true;
                continue;
            }
        }
        int act = -1;
        size_t tok = 0;
        if (starts_with(s, len, i, kSquare)) { act = ACT_SQUARE; tok = 3; }
        else if (starts_with(s, len, i, kTriangle)) { act = ACT_TRIANGLE; tok = 3; }
        else if (starts_with(s, len, i, kCrossX)) { act = ACT_CROSS; tok = 3; }
        else if (starts_with(s, len, i, kTimes)) {
            /* multiplication ("x2", "2x3") keeps its sign */
            if (!(i > 0 && is_digit(s[i - 1])) && !(i + 2 < len && is_digit(s[i + 2]))) {
                act = ACT_CROSS; tok = 2;
            }
        } else if (starts_with(s, len, i, kCircle)) {
            /* "○○" is the game's own placeholder: only a lone circle is a button */
            const bool prev = i >= 3 && starts_with(s, len, i - 3, kCircle);
            const bool next = starts_with(s, len, i + 3, kCircle);
            if (!prev && !next) { act = ACT_CIRCLE; tok = 3; }
        } else if (word_start && (s[i] == 'L' || s[i] == 'R') && i + 1 < len &&
                   s[i + 1] >= '1' && s[i + 1] <= '3' && (i + 2 == len || !is_alnum(s[i + 2]))) {
            static const int map[2][3] = {{ACT_L1, ACT_L2, ACT_L3}, {ACT_R1, ACT_R2, ACT_R3}};
            act = map[s[i] == 'R'][s[i + 1] - '1'];
            tok = 2;
        } else if (word_start && (starts_with(s, len, i, "START") || starts_with(s, len, i, "SELECT"))) {
            const size_t m = s[i + 1] == 'T' ? 5 : 6;
            if (i + m == len || !is_alnum(s[i + m])) {
                const bool context = ends_with_nocase(s, i, "press ") || ends_with_nocase(s, i, "pressing ") ||
                                     ends_with_nocase(s, i, "the ") ||
                                     starts_with(s, len, i + m, " button");
                if (context) { act = m == 5 ? ACT_START : ACT_SELECT; tok = m; }
            }
        }
        if (act >= 0) {
            /* " button(s)" after the name: " key(s)" for a key; for a mouse
             * button the word and a leading "the " go ("Press [Left Click]
             * to claw", not "the [Left Click] key"). */
            const size_t word = button_word_len(s, len, i + tok);
            const bool mouse = is_mouse(vks[act]);
            Out trial = o;
            if (mouse && word) trial.drop_the();
            if (put_key(trial, vks, act)) {
                if (word && !mouse) trial.put(word == 8 ? " keys" : " key");
                if (!trial.overflow) {
                    o = trial;
                    i += tok + word;
                    changed = true;
                    continue;
                }
            }
        }
        o.put(s + i, 1);
        i++;
    }
    return changed && !o.overflow;
}

std::mutex s_log_lock;
std::unordered_set<uint64_t>* s_logged;   /* leaked on purpose: used until exit */
unsigned s_log_count;
volatile LONG s_hook_seen;

uint64_t fnv(const char* s, size_t n)
{
    uint64_t h = 14695981039346656037ull;
    for (size_t k = 0; k < n; k++) { h ^= (uint8_t)s[k]; h *= 1099511628211ull; }
    return h;
}

void log_rewrite(const char* in, size_t in_len, const char* out, size_t out_len, bool too_long)
{
    std::lock_guard<std::mutex> guard(s_log_lock);
    if (s_log_count >= 200) return;
    if (!s_logged) s_logged = new std::unordered_set<uint64_t>();
    if (!s_logged->insert(fnv(in, in_len) ^ (too_long ? 1u : 0u)).second) return;
    s_log_count++;
    if (too_long)
        fprintf(stderr, "[tj-prompts] text \"%.*s\" kept: key names would not fit\n",
                (int)(in_len < 60 ? in_len : 60), in);
    else
        fprintf(stderr, "[tj-prompts] text \"%.*s\" -> \"%.*s\"\n", (int)(in_len < 60 ? in_len : 60),
                in, (int)(out_len < 80 ? out_len : 80), out);
}

}  // namespace

/* post_lift hook at func_00198590 entry (r4 = text job). Any guest thread. */
void tj_prompt_rewrite_job(uint32_t job)
{
    if (!s_hook_seen && !InterlockedExchange(&s_hook_seen, 1))
        fprintf(stderr, "[tj-prompts] text hook active (func_00198590)\n");
    unsigned long long sig = 0;
    int vks[ACT_COUNT];
    if (!job || !cellPad_prompt_keys(&sig, vks)) return;
    const uint32_t ea = job + (uint32_t)kJobString;
    char str[kJobCap];
    size_t len = 0;
    while (len < kJobCap && (str[len] = (char)vm_read8(ea + len)) != '\0') len++;
    if (len == 0 || len >= kJobCap) return;   /* no terminator inside the copy: leave it */
    Out out;
    if (!rewrite(str, len, vks, out)) return;
    if (out.n >= kJobCap) {
        log_rewrite(str, len, NULL, 0, true);
        return;
    }
    for (size_t k = 0; k < out.n; k++) vm_write8(ea + k, (uint8_t)out.buf[k]);
    vm_write8(ea + out.n, 0);
    log_rewrite(str, len, out.buf, out.n, false);
}

#ifdef TJ_PROMPT_TEXT_SELFTEST
/* Offline check: tj_prompt_text_selftest(in, vks, out, cap) runs the rewrite
 * without guest memory. */
extern "C" int tj_prompt_text_selftest(const char* in, const int* vks, char* out, size_t cap)
{
    Out o;
    const size_t len = strlen(in);
    const bool changed = rewrite(in, len, vks, o);
    if (!changed) { snprintf(out, cap, "%s", in); return 0; }
    if (o.n >= kJobCap) { snprintf(out, cap, "%s", in); return 2; }
    snprintf(out, cap, "%.*s", (int)o.n, o.buf);
    return 1;
}
#endif
