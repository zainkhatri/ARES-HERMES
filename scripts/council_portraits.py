"""Council portraits v3 — Street Fighter select-screen style.
Fighter busts: thick necks/traps, hard scowls, heavy 3-tone shading, battle gear."""
from PIL import Image, ImageDraw

W, H = 64, 72
OUT = (14, 9, 7)

def bg(d, tint):
    # dramatic radial-ish burst behind fighter
    cx = 32
    for x in range(W):
        for y in range(H):
            dist = abs(x - cx) + abs(y - 26) // 2
            v = max(0, 26 - dist)
            d.point((x, y), (tint[0] + v, tint[1] + v // 2, tint[2] + v // 3))

def portrait(skin, sh, hi, hair, hhi, cloth, csh, eye, tint,
             style="ryu", band=None, eyepatch=False, scar=False,
             glasses=False, stubble=False, tags=False, teeth=False):
    im = Image.new("RGB", (W, H)); d = ImageDraw.Draw(im)
    bg(d, tint)

    # --- massive traps + shoulders ---
    d.polygon([(0, 71), (2, 58), (14, 50), (26, 47), (38, 47), (50, 50), (62, 58), (63, 71)],
              fill=cloth, outline=OUT)
    d.polygon([(0, 71), (2, 58), (10, 53), (8, 71)], fill=csh)
    d.polygon([(56, 71), (62, 58), (54, 53), (63, 71)], fill=csh)
    # collar / gi V-neck showing chest
    d.polygon([(25, 48), (32, 58), (39, 48)], fill=skin, outline=OUT)
    d.line([(28, 51), (32, 56)], fill=sh)
    if tags:
        ch = (200, 200, 190)
        d.line([(28, 48), (31, 55)], fill=ch); d.line([(36, 48), (33, 55)], fill=ch)
        d.rectangle([31, 55, 34, 58], fill=ch, outline=OUT)

    # --- bull neck ---
    d.polygon([(25, 40), (25, 50), (39, 50), (39, 40)], fill=sh, outline=None)
    d.line([(25, 40), (25, 50)], fill=OUT); d.line([(39, 40), (39, 50)], fill=OUT)

    # --- head: square jaw, wide cheekbones ---
    head = [(17, 13), (14, 22), (14, 30), (16, 36), (20, 42), (26, 46),
            (38, 46), (44, 42), (48, 36), (50, 30), (50, 22), (47, 13),
            (41, 8), (23, 8)]
    d.polygon(head, fill=skin, outline=OUT)
    # heavy right-side shadow (dramatic side light)
    d.polygon([(43, 13), (49, 22), (49, 30), (47, 36), (43, 42), (38, 45),
               (40, 38), (43, 30), (44, 20)], fill=sh)
    # cheekbone + chin definition
    d.line([(18, 32), (21, 36)], fill=sh)
    d.line([(43, 32), (40, 36)], fill=sh)
    d.line([(29, 44), (35, 44)], fill=sh)          # chin crease
    d.polygon([(17, 17), (16, 28), (19, 22)], fill=hi)
    # ears
    d.rectangle([12, 24, 15, 31], fill=skin, outline=OUT)
    d.rectangle([49, 24, 52, 31], fill=sh, outline=OUT)

    # --- scowl eyes: deep-set, shadowed sockets ---
    d.line([(19, 23), (28, 24)], fill=sh)          # socket shadow
    d.line([(36, 24), (45, 23)], fill=sh)
    for i, ex in enumerate((21, 37)):
        if eyepatch and i == 1: continue
        d.line([(ex, 25), (ex + 6, 26)] if i == 0 else [(ex, 26), (ex + 6, 25)], fill=OUT)
        d.rectangle([ex + 1, 27, ex + 5, 28], fill=(232, 224, 206))
        d.rectangle([ex + 2, 27, ex + 4, 28], fill=eye)
        d.point((ex + 3, 27), (10, 6, 5))
    # brows: heavy angry V
    d.line([(18, 21), (28, 25)], fill=OUT, width=3)
    d.line([(36, 25), (46, 21)], fill=OUT, width=3)

    if eyepatch:
        d.rectangle([35, 24, 44, 30], fill=(24, 18, 16), outline=OUT)
        d.line([(35, 26), (14, 22)], fill=OUT); d.line([(44, 25), (50, 21)], fill=OUT)
    if glasses:
        g = (216, 184, 98)
        d.rectangle([18, 25, 28, 30], outline=g)
        d.rectangle([36, 25, 46, 30], outline=g)
        d.line([(28, 27), (36, 27)], fill=g)
    if scar:
        sc = (168, 96, 76)
        d.line([(24, 31), (20, 40)], fill=sc, width=1)
        d.point((23, 34), sc); d.point((21, 37), sc)

    # --- nose: strong, broken-fighter nose ---
    d.line([(32, 28), (31, 34)], fill=sh, width=2)
    d.line([(30, 35), (35, 35)], fill=sh)

    # --- mouth: grimace / gritted teeth ---
    if teeth:
        d.rectangle([27, 39, 37, 42], fill=(226, 218, 200), outline=OUT)
        d.line([(30, 39), (30, 42)], fill=sh); d.line([(34, 39), (34, 42)], fill=sh)
    else:
        d.line([(27, 41), (37, 41)], fill=OUT, width=2)
        d.line([(26, 40), (27, 41)], fill=OUT); d.line([(38, 40), (37, 41)], fill=OUT)
        d.line([(28, 43), (36, 43)], fill=sh)

    if stubble:
        pts = [(21,43),(23,44),(25,45),(27,46),(24,42),(40,42),(41,44),(38,45),
               (36,46),(22,40),(42,40),(26,44),(39,46),(20,41)]
        for px, py in pts:
            try:
                if im.getpixel((px, py)) in (skin, sh): d.point((px, py), (140, 100, 74))
            except IndexError: pass

    # --- hair ---
    if style == "ryu":         # dark shaggy hair + headband
        d.polygon([(12, 24), (11, 10), (20, 3), (32, 1), (44, 3), (53, 10), (52, 24),
                   (49, 14), (44, 9), (36, 12), (28, 12), (20, 9), (15, 14)],
                  fill=hair, outline=OUT)
        d.line([(19, 7), (30, 3)], fill=hhi, width=1)
    elif style == "master":    # bald top, hair sides (Gouken-ish)
        d.polygon([(12, 26), (12, 16), (17, 12), (15, 24)], fill=hair, outline=OUT)
        d.polygon([(52, 26), (52, 16), (47, 12), (49, 24)], fill=hair, outline=OUT)
        d.line([(20, 10), (32, 7)], fill=hi)   # skull shine
    elif style == "brute":     # bald + tiny topknot nub? no: clean bald, mean
        d.line([(22, 9), (34, 6)], fill=hi, width=2)
    elif style == "flattop":   # Guile flat-top
        d.polygon([(14, 22), (14, 4), (50, 4), (50, 22), (47, 12), (44, 11), (20, 11), (17, 12)],
                  fill=hair, outline=OUT)
        d.line([(16, 6), (48, 6)], fill=hhi, width=2)

    if band:                   # headband over forehead, knot + tails
        d.rectangle([13, 15, 51, 19], fill=band, outline=OUT)
        d.polygon([(51, 16), (58, 12), (56, 20)], fill=band, outline=OUT)
        d.polygon([(51, 18), (59, 22), (55, 26)], fill=band, outline=OUT)

    return im

SKIN = (234, 178, 128); SH = (188, 122, 82); HI = (248, 206, 158)
DEFS = {
 "security":    dict(hair=(26, 22, 26), hhi=(80, 76, 92), cloth=(216, 210, 198), csh=(168, 160, 146),
                     eye=(58, 106, 66), tint=(64, 20, 14), style="ryu", band=(178, 34, 30)),
 "correctness": dict(hair=(210, 204, 192), hhi=(240, 236, 226), cloth=(90, 66, 44), csh=(66, 48, 32),
                     eye=(94, 66, 42), tint=(48, 34, 16), style="master", glasses=True),
 "blast":       dict(hair=(0, 0, 0), hhi=(0, 0, 0), cloth=(96, 34, 28), csh=(68, 24, 20),
                     eye=(70, 94, 138), tint=(70, 26, 10), style="brute",
                     eyepatch=True, scar=True, teeth=True),
 "pragmatist":  dict(hair=(214, 178, 84), hhi=(240, 214, 130), cloth=(74, 84, 58), csh=(54, 62, 42),
                     eye=(84, 74, 52), tint=(30, 38, 22), style="flattop", stubble=True, tags=True),
}
import sys
out = sys.argv[1] if len(sys.argv) > 1 else "/tmp"
S = 8
sheet = Image.new("RGB", (W*S*4 + 24, H*S), (8, 5, 4))
for i, (name, kw) in enumerate(DEFS.items()):
    im = portrait(SKIN, SH, HI, **kw)
    big = im.resize((W*S, H*S), Image.NEAREST)
    big.save(f"{out}/{name}.png")
    sheet.paste(big, (i*(W*S+8), 0))
sheet.save("/tmp/council_sheet.png")
print("done")
