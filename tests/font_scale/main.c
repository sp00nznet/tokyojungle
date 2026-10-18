#include "cellFont.h"

#include <math.h>
#include <stdio.h>
#include <string.h>

static int nearf(float a, float b)
{
    return fabsf(a - b) < 0.0001f;
}

int main(void)
{
    CellFont font = {0};
    CellFontType type = {0x201u, 0};
    if (cellFontInit(NULL) != CELL_OK) return 1;
    if (cellFontOpenFontset(0, &type, &font) != CELL_OK) return 2;
    if (cellFontSetResolutionDpi(&font, 144, 72) != CELL_OK) return 3;
    if (cellFontSetScalePoint(&font, 12.0f, 12.0f) != CELL_OK) return 4;
    if (!nearf(font.scale_x, 24.0f) || !nearf(font.scale_y, 12.0f)) return 5;
    CellFontGlyphMetrics wide_metrics = {0};
    if (cellFontGetCharGlyphMetrics(&font, 'A', &wide_metrics) != CELL_OK)
        return 20;
    if (cellFontSetResolutionDpi(&font, 0, 0) != CELL_OK) return 6;
    if (cellFontSetScalePoint(&font, 12.0f, 12.0f) != CELL_OK) return 7;
    if (!nearf(font.scale_x, 12.0f) || !nearf(font.scale_y, 12.0f)) return 8;
    if (cellFontSetScalePoint(NULL, 10.0f, 10.0f) !=
        (int32_t)CELL_FONT_ERROR_INVALID_PARAMETER) return 9;
    CellFontGlyphMetrics metrics = {0};
    if (cellFontGetCharGlyphMetrics(&font, 'A', &metrics) != CELL_OK) return 10;
    if (metrics.width <= 0.0f || metrics.height <= 0.0f ||
        metrics.h_advance <= 0.0f) return 11;
    if (wide_metrics.width <= metrics.width * 1.8f ||
        wide_metrics.h_advance <= metrics.h_advance * 1.8f) return 21;
    CellFontGlyphMetrics japanese = {0};
    if (cellFontGetCharGlyphMetrics(&font, 0x65E5u, &japanese) != CELL_OK ||
        japanese.width <= 0.0f || japanese.height <= 0.0f) return 22;
    u32 font_id = 0xFFFFFFFFu;
    u32 font_code = 0;
    if (cellFontGetFontIdCode(&font, 0x41u, &font_id, &font_code) != CELL_OK)
        return 12;
    if (font_id != type.type || font_code != 0x41u) return 13;
    if (cellFontGetFontIdCode(&font, 0x41u, NULL, &font_code) !=
        (int32_t)CELL_FONT_ERROR_INVALID_PARAMETER) return 14;

    unsigned char pixels[64 * 64];
    memset(pixels, 0, sizeof(pixels));
    CellFontRenderSurface surface = {pixels, 64, 1, 64, 64};
    CellFontGlyphImage image = {0};
    if (cellFontRenderCharGlyphImage(&font, 'A', &surface, 8.0f, 32.0f,
                                     &metrics, &image) != CELL_OK) return 15;
    int nonzero = 0;
    for (unsigned i = 0; i < sizeof(pixels); ++i) nonzero |= pixels[i] != 0;
    if (!nonzero || image.width <= 0 || image.height <= 0) return 16;
    if (!image.buffer) {
        fputs("FAIL: glyph transfer returned a null alpha bitmap\n", stderr);
        return 24;
    }
    memset(pixels, 0, sizeof(pixels));
    if (cellFontRenderCharGlyphImage(&font, 'A', &surface, 0.0f, 0.0f,
                                     &metrics, &image) != CELL_OK) return 25;
    nonzero = 0;
    for (unsigned i = 0; i < sizeof(pixels); ++i) nonzero |= pixels[i] != 0;
    if (!nonzero) {
        fputs("FAIL: top-origin glyph was clipped above its surface\n", stderr);
        return 26;
    }

    memset(pixels, 0, sizeof(pixels));
    if (cellFontRenderCharGlyphImage(&font, 'A', &surface, -100.0f, -100.0f,
                                     &metrics, NULL) != CELL_OK) return 17;
    nonzero = 0;
    for (unsigned i = 0; i < sizeof(pixels); ++i) nonzero |= pixels[i] != 0;
    if (nonzero) return 18;
    unsigned char rgba[64 * 64 * 4];
    memset(rgba, 0x5A, sizeof(rgba));
    CellFontRenderSurface color_surface = {rgba, 64 * 4, 4, 64, 64};
    if (cellFontRenderCharGlyphImage(&font, 'A', &color_surface, 2.0f, 0.0f,
                                     &metrics, &image) != CELL_OK || !image.buffer)
        return 27;
    for (unsigned i = 0; i < sizeof(rgba); ++i)
        if (rgba[i] != 0x5A) return 28; /* caller owns color blending */
    CellFont instance = {0};
    if (cellFontOpenFontInstance(&font, &instance) != CELL_OK ||
        instance.handle == font.handle) return 29;
    CellFontGlyphMetrics original_metrics = {0}, instance_metrics = {0};
    if (cellFontSetScalePixel(&instance, 24.0f, 24.0f) != CELL_OK ||
        cellFontGetCharGlyphMetrics(&font, 'A', &original_metrics) != CELL_OK ||
        cellFontGetCharGlyphMetrics(&instance, 'A', &instance_metrics) != CELL_OK ||
        instance_metrics.h_advance < original_metrics.h_advance * 1.8f)
        return 30;
    if (cellFontCloseFont(&font) != CELL_OK ||
        cellFontRenderCharGlyphImage(&instance, 'A', &color_surface, 0.0f, 0.0f,
                                     &metrics, &image) != CELL_OK || !image.buffer)
        return 31; /* closing the parent must not destroy the shared face */
    if (cellFontOpenFontset(0, NULL, &font) !=
        (int32_t)CELL_FONT_ERROR_INVALID_PARAMETER) return 23;
    if (cellFontEnd() != CELL_OK) return 19;
    puts("PASS: font metrics, live alpha transfer, top origin, clipping, color ownership and instance lifetime");
    return 0;
}
