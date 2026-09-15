"""Council portraits v2 — pixel-art fundamentals: outlines, jaw taper,
hair mass with hairline, lidded eyes, closed mouths, rim lighting."""
from PIL import Image, ImageDraw

W, H = 64, 72
OUT = (16, 10, 8)  # universal dark outline

def bg(d):
    for x in range(W):
        panel = (x // 9) % 2
        base = (66, 42, 26) if panel == 0 else (52, 32, 20)
        for y in range(H):
            v = max(0, 10 - abs(y - 22) // 4)
            d.point((x, y), (base[0]+v, base[1]+v//2, base[2]))
    # vertical panel seams
    for x in range(0, W, 9):
        d.line([(x, 0), (x, H)], fill=(38, 24, 15))

def portrait(skin, sh, hi, hair, hhi, suit, ssh, shirt, tie, eye,
             brow=-2, glasses=False, stubble=False, style="slick"):
    im = Image.new("RGB", (W, H)); d = ImageDraw.Draw(im)
    bg(d)

    # --- suit / shoulders (with outline) ---
    shoulders = [(0, 71), (8, 54), (20, 48), (44, 48), (56, 54), (63, 71)]
    d.polygon(shoulders, fill=suit, outline=OUT)
    d.polygon([(0, 71), (8, 54), (14, 51), (10, 71)], fill=ssh)
    d.polygon([(54, 71), (56, 54), (50, 51), (63, 71)], fill=ssh)
    # lapels + shirt + tie
    d.polygon([(24, 48), (32, 60), (40, 48)], fill=shirt, outline=OUT)
    d.polygon([(30, 49), (32, 57), (34, 49)], fill=tie)
    d.polygon([(31, 57), (32, 66), (33, 57)], fill=tie)

    # --- neck ---
    d.rectangle([27, 42, 37, 50], fill=sh, outline=None)
    d.line([(27, 42), (27, 50)], fill=OUT); d.line([(37, 42), (37, 50)], fill=OUT)

    # --- head: tapered jaw, not an egg ---
    head = [(18, 14), (14, 22), (14, 32), (17, 39), (23, 45), (28, 47),
            (36, 47), (41, 45), (47, 39), (50, 32), (50, 22), (46, 14),
            (40, 9), (24, 9)]
    d.polygon(head, fill=skin, outline=OUT)
    # shading: right-side shadow band, left highlight
    d.polygon([(42, 14), (49, 22), (49, 32), (46, 39), (40, 44), (36, 46),
               (40, 36), (42, 24)], fill=sh)
    d.polygon([(17, 18), (16, 30), (19, 24)], fill=hi)
    # ears
    d.rectangle([12, 24, 15, 31], fill=skin, outline=OUT)
    d.rectangle([49, 24, 52, 31], fill=sh, outline=OUT)

    # --- eyes: small, lidded, dark ---
    for ex, shade in ((22, False), (36, True)):
        d.line([(ex, 25), (ex + 6, 25)], fill=OUT)                 # upper lid
        d.rectangle([ex + 1, 26, ex + 5, 28], fill=(238, 230, 214))
        px = ex + 3
        d.rectangle([px - 1, 26, px + 1, 28], fill=eye)
        d.point((px, 26), (12, 8, 6))                              # pupil
        d.point((px - 1, 26), (252, 250, 245))                     # glint
        d.line([(ex + 1, 29), (ex + 5, 29)], fill=sh)              # lower lid
    # brows: thick, angled
    d.line([(20, 22 + brow), (28, 23)], fill=OUT, width=2)
    d.line([(36, 23), (44, 22 + brow)], fill=OUT, width=2)

    if glasses:
        g = (212, 180, 96)
        d.rectangle([19, 24, 29, 30], outline=g)
        d.rectangle([35, 24, 45, 30], outline=g)
        d.line([(29, 26), (35, 26)], fill=g)
        d.line([(19, 25), (15, 24)], fill=g); d.line([(45, 25), (49, 24)], fill=g)

    # --- nose: shadow-side wedge ---
    d.line([(32, 29), (31, 34)], fill=sh)
    d.line([(31, 35), (34, 35)], fill=sh)
    d.point((34, 34), sh)

    # --- mouth: closed, slight frown ---
    d.line([(27, 40), (30, 41)], fill=(96, 46, 40), width=1)
    d.line([(30, 41), (36, 41)], fill=(96, 46, 40), width=1)
    d.line([(36, 41), (38, 40)], fill=(96, 46, 40), width=1)
    d.line([(28, 43), (36, 43)], fill=sh)  # lower-lip shadow

    if stubble:
        pts = [(21,44),(23,45),(24,43),(26,46),(28,45),(31,46),(33,45),(35,46),
               (37,45),(39,44),(41,43),(42,44),(22,42),(40,41),(25,44),(38,46)]
        for px, py in pts:
            if im.getpixel((px, py)) in (skin, sh):
                d.point((px, py), (150, 108, 80))

    # --- hair (drawn last, over forehead) ---
    if style == "slick":       # swept back w/ widow's peak, AA-protagonist vibe
        d.polygon([(12, 26), (11, 12), (18, 4), (32, 1), (46, 4), (53, 12), (52, 26),
                   (48, 14), (42, 10), (34, 12), (32, 13), (30, 12), (22, 10), (16, 14)],
                  fill=hair, outline=OUT)
        d.line([(17, 8), (28, 4)], fill=hhi, width=2)
        d.line([(38, 4), (47, 9)], fill=hhi, width=1)
    elif style == "side":      # neat side part, flat across brow
        d.polygon([(12, 26), (12, 10), (22, 4), (40, 4), (52, 11), (52, 26),
                   (49, 16), (46, 12), (24, 12), (18, 14), (15, 18)],
                  fill=hair, outline=OUT)
        d.line([(20, 8), (38, 6)], fill=hhi, width=2)
    elif style == "spiky":
        d.polygon([(12, 24), (10, 10), (16, 12), (17, 3), (24, 10), (30, 0),
                   (36, 9), (44, 2), (46, 11), (54, 8), (52, 24),
                   (48, 14), (40, 11), (30, 13), (20, 12), (15, 16)],
                  fill=hair, outline=OUT)
        d.line([(18, 6), (22, 10)], fill=hhi, width=1)
        d.line([(31, 3), (33, 9)], fill=hhi, width=1)
        d.line([(43, 5), (45, 10)], fill=hhi, width=1)
    elif style == "buzz":
        d.polygon([(13, 22), (13, 12), (20, 6), (32, 4), (44, 6), (51, 12), (51, 22),
                   (47, 14), (38, 11), (26, 11), (18, 14)],
                  fill=hair, outline=OUT)

    return im

SKIN = (236, 184, 138); SH = (196, 134, 92); HI = (248, 208, 164)
DEFS = {
 "security":    dict(hair=(28, 24, 28), hhi=(84, 80, 96), suit=(40, 46, 62), ssh=(28, 32, 44),
                     shirt=(238, 235, 228), tie=(178, 42, 38), eye=(52, 108, 66), brow=-3, style="slick"),
 "correctness": dict(hair=(96, 62, 32), hhi=(150, 106, 58), suit=(74, 56, 40), ssh=(54, 40, 29),
                     shirt=(238, 235, 228), tie=(56, 78, 122), eye=(96, 68, 42), brow=0,
                     glasses=True, style="side"),
 "blast":       dict(hair=(190, 62, 32), hhi=(240, 128, 58), suit=(86, 34, 30), ssh=(62, 24, 21),
                     shirt=(228, 222, 208), tie=(28, 24, 22), eye=(66, 92, 136), brow=-3, style="spiky"),
 "pragmatist":  dict(hair=(56, 46, 40), hhi=(96, 84, 74), suit=(62, 64, 54), ssh=(46, 48, 40),
                     shirt=(222, 216, 200), tie=(146, 104, 42), eye=(84, 72, 52), brow=-1,
                     stubble=True, style="buzz"),
}
import sys
out = sys.argv[1] if len(sys.argv) > 1 else "/tmp"
S = 8
sheet = Image.new("RGB", (W*S*4 + 24, H*S), (10, 6, 5))
for i, (name, kw) in enumerate(DEFS.items()):
    im = portrait(SKIN, SH, HI, **kw)
    big = im.resize((W*S, H*S), Image.NEAREST)
    big.save(f"{out}/{name}.png")
    sheet.paste(big, (i*(W*S+8), 0))
sheet.save("/tmp/council_sheet.png")
print("done")
