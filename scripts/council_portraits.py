"""Ace-Attorney-style pixel portraits for the 4 council members.
Draw at 56x64 with PIL shapes, upscale x8 NEAREST -> crisp pixel art."""
from PIL import Image, ImageDraw

W, H = 56, 64

def portrait(skin, skin_sh, skin_hi, hair, hair_hi, suit, suit_sh, shirt, tie,
             eye, brow_angry=0, glasses=False, stubble=False, hairstyle="slick"):
    im = Image.new("RGB", (W, H))
    d = ImageDraw.Draw(im)
    # courtroom wood background, vertical panels
    for x in range(W):
        base = (58, 38, 24) if (x // 7) % 2 == 0 else (48, 30, 18)
        for y in range(H):
            shade = max(0, 12 - abs(y - 20) // 3)
            d.point((x, y), (base[0] + shade, base[1] + shade // 2, base[2]))
    # shoulders / suit
    d.polygon([(2, 63), (10, 46), (20, 41), (36, 41), (46, 46), (54, 63)], fill=suit)
    d.polygon([(2, 63), (10, 46), (16, 44), (12, 63)], fill=suit_sh)
    d.polygon([(44, 63), (46, 46), (40, 44), (54, 63)], fill=suit_sh)
    # shirt V + tie
    d.polygon([(22, 41), (28, 52), (34, 41)], fill=shirt)
    d.polygon([(26, 42), (28, 50), (30, 42)], fill=tie)
    d.polygon([(27, 50), (28, 58), (29, 50)], fill=tie)
    # neck
    d.rectangle([24, 36, 32, 44], fill=skin_sh)
    # head
    d.ellipse([13, 6, 43, 42], fill=skin)
    # face shading: left highlight, right shadow
    d.ellipse([15, 10, 33, 40], fill=skin_hi)
    d.ellipse([17, 12, 41, 41], fill=skin)
    d.polygon([(36, 14), (42, 20), (42, 36), (34, 41)], fill=skin_sh)
    # jaw shadow
    d.line([(20, 40), (36, 40)], fill=skin_sh)
    # ears
    d.ellipse([11, 22, 16, 30], fill=skin)
    d.ellipse([40, 22, 45, 30], fill=skin_sh)
    # hair
    if hairstyle == "slick":  # swept back, widow's peak
        d.polygon([(11, 22), (12, 8), (22, 2), (36, 2), (45, 9), (45, 22),
                   (41, 13), (34, 9), (28, 12), (22, 9), (15, 14)], fill=hair)
        d.line([(16, 7), (26, 4)], fill=hair_hi, width=2)
    elif hairstyle == "spiky":
        d.polygon([(11, 20), (10, 8), (16, 10), (18, 2), (24, 9), (28, 1),
                   (33, 9), (39, 3), (42, 11), (46, 8), (45, 20),
                   (40, 12), (30, 9), (20, 12), (15, 15)], fill=hair)
        d.line([(19, 6), (23, 9)], fill=hair_hi, width=1)
        d.line([(29, 4), (31, 8)], fill=hair_hi, width=1)
    elif hairstyle == "side":  # neat side part
        d.polygon([(11, 22), (12, 7), (24, 3), (38, 4), (45, 12), (45, 22),
                   (42, 14), (36, 10), (24, 10), (16, 15)], fill=hair)
        d.line([(15, 9), (24, 6)], fill=hair_hi, width=2)
    elif hairstyle == "buzz":
        d.polygon([(12, 20), (13, 9), (22, 4), (34, 4), (44, 10), (44, 20),
                   (40, 13), (32, 10), (22, 11), (16, 15)], fill=hair)
    # brows (angry tilt)
    a = brow_angry
    d.line([(19, 19 + a), (26, 19)], fill=(20, 12, 8), width=2)
    d.line([(31, 19), (38, 19 + a)], fill=(20, 12, 8), width=2)
    # eyes
    for ex in (20, 32):
        d.rectangle([ex, 22, ex + 5, 25], fill=(235, 228, 210))
        d.rectangle([ex + 2, 22, ex + 4, 25], fill=eye)
        d.point((ex + 2, 22), (250, 250, 250))
        d.line([(ex, 21), (ex + 5, 21)], fill=(30, 18, 12))
    if glasses:
        g = (200, 170, 90)
        d.rectangle([18, 20, 27, 27], outline=g)
        d.rectangle([30, 20, 39, 27], outline=g)
        d.line([(27, 23), (30, 23)], fill=g)
    # nose
    d.line([(28, 26), (27, 31)], fill=skin_sh, width=1)
    d.line([(26, 32), (30, 32)], fill=skin_sh)
    # mouth (stern flat line)
    d.line([(24, 36), (33, 36)], fill=(110, 55, 45), width=2)
    if stubble:
        for px in range(20, 38, 2):
            for py in range(37, 41, 2):
                d.point((px, py), skin_sh)
    return im

SKIN   = (232, 178, 130); SKIN_S = (188, 128, 88); SKIN_H = (246, 202, 158)
DEFS = {
 "security":    dict(hair=(24, 20, 22), hair_hi=(70, 66, 78), suit=(38, 42, 56), suit_sh=(26, 29, 40),
                     shirt=(235, 232, 224), tie=(170, 40, 36), eye=(60, 110, 70),
                     brow_angry=-3, hairstyle="slick"),
 "correctness": dict(hair=(88, 58, 30), hair_hi=(140, 100, 55), suit=(70, 52, 38), suit_sh=(52, 38, 27),
                     shirt=(235, 232, 224), tie=(60, 80, 120), eye=(90, 65, 40),
                     brow_angry=0, glasses=True, hairstyle="side"),
 "blast":       dict(hair=(178, 60, 34), hair_hi=(230, 120, 60), suit=(80, 34, 30), suit_sh=(58, 24, 21),
                     shirt=(225, 220, 205), tie=(30, 26, 24), eye=(70, 90, 130),
                     brow_angry=-2, hairstyle="spiky"),
 "pragmatist":  dict(hair=(52, 44, 38), hair_hi=(90, 80, 70), suit=(60, 62, 52), suit_sh=(44, 46, 38),
                     shirt=(220, 214, 198), tie=(140, 100, 40), eye=(80, 70, 50),
                     brow_angry=-1, stubble=True, hairstyle="buzz"),
}
import sys
out = sys.argv[1] if len(sys.argv) > 1 else "/tmp"
sheet = Image.new("RGB", (W * 8 * 4 + 24, H * 8), (10, 6, 5))
for i, (name, kw) in enumerate(DEFS.items()):
    im = portrait(SKIN, SKIN_S, SKIN_H, **kw)
    big = im.resize((W * 8, H * 8), Image.NEAREST)
    big.save(f"{out}/{name}.png")
    sheet.paste(big, (i * (W * 8 + 8), 0))
sheet.save("/tmp/council_sheet.png")
print("done")
