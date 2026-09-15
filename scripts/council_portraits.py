"""Council portraits v7 — pixel-art LAW council (judges).
28x32 grid authored as 14-col left half + mirror. Powdered wigs, black robes,
white jabot bands. Per-member wig/gear/palette variants."""
from PIL import Image
import sys

# K outline | W wig light | G wig shadow | S skin | M skin mid | s skin shadow
# d dark skin accent | E eye | R robe | r robe highlight | J jabot | j jabot shadow
# B accent (sash/trim) | . transparent
HALF = [   # 14 cols, mirrored to 28
"..............",
"....KKKKKK....",
"...KWWWWWWWWWW",
"..KWWGWWWWWWWW",
".KWGWWWWWWWWWW",
".KWWKWWWWWWWWW",
"KWGWKWWGWWWWWW",
"KWWWK.KKKKKKKK",
"KWGWK.KSSSSSSS",
"KWWWKKSSSSSSSS",
"KWGWKSSSSSSSSS",
"KWWWKSKKKKSSSS",
"KWGWKSKWWEKSSS",
"KWWWKSSSSSSSSS",
"KWGWKSSSSSSSSM",
"KWWWKSSSSSSSsM",
"KWGWKSSSSSKdSM",
"KWWWKsSSSSSSMs",
".KWWKSsSSSSKKK",
".KWGKSSSSSSsss",
".KWWKSSSSSSsss",
"..KWKSSSSSSSss",
"..KWKKsSSSSsss",
"...KK.KssssssK",
"......KKKKKKKK",
"....KKRRRRKJJJ",
"...KRRRRRRKJjJ",
"..KRRRRRRRKJJJ",
".KRRRRRRRRKJjJ",
"KRRRRRRRRRKJJJ",
"KRRRRRRRRRKJjJ",
"KRRRRRRRRRRKJJ",
]

def mirror(half_rows):
    out = []
    for r in half_rows:
        r = r.ljust(14, '.')
        out.append(r + r[::-1])
    return out

def render(grid, pal, scale=16):
    h = len(grid); w = max(len(r) for r in grid)
    im = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    for y, row in enumerate(grid):
        for x, ch in enumerate(row):
            if ch != '.':
                im.putpixel((x, y), pal.get(ch, (255, 0, 255)) + (255,))
    return im.resize((w*scale, h*scale), Image.NEAREST)

PAL = {
 'K': (18, 10, 8),
 'W': (236, 230, 218), 'G': (196, 188, 172),
 'S': (244, 198, 152), 'M': (224, 158, 110), 's': (196, 118, 78), 'd': (150, 80, 50),
 'E': (54, 40, 30),
 'R': (32, 24, 22), 'r': (58, 44, 40),
 'J': (240, 238, 232), 'j': (206, 200, 190),
 'B': (170, 34, 28),
}
def with_gear(grid, member):
    g = [list(r) for r in grid]
    def put(x, y, ch):
        g[y][x] = ch
    if member == "security":      # chief justice: red sash across left robe
        for i, y in enumerate(range(25, 32)):
            x = 5 + i
            put(x, y, 'B'); put(x+1, y, 'B')
    if member == "correctness":   # gold spectacles
        for x in (12, 13, 14, 15):
            put(x, 12, 'K')
        for x in list(range(6, 11)) + list(range(17, 22)):
            put(x, 13, 'K')
        put(5, 12, 'K'); put(22, 12, 'K')
    if member == "blast":         # grey beard + mustache over jaw
        for y in range(18, 24):
            for x in range(6, 22):
                if g[y][x] in ('S', 'M', 's', 'K'):
                    put(x, y, 'W' if (x + y) % 3 else 'G')
        for x in range(9, 19):
            put(x, 17, 'W' if x % 3 else 'G')
        for x in (13, 14):
            put(x, 18, 'K')   # mouth gap in beard
    return ["".join(r) for r in g]

MEMBERS = {
 "security":    {},
 "correctness": {'W': (214, 212, 206), 'G': (176, 172, 162)},
 "blast":       {'W': (222, 218, 210), 'G': (178, 172, 160),
                 'S': (238, 186, 138), 'M': (214, 146, 100)},
 "pragmatist":  {'W': (128, 92, 52), 'G': (96, 66, 36),
                 'S': (232, 178, 128), 'M': (206, 140, 94)},
}
out = sys.argv[1] if len(sys.argv) > 1 else "/tmp"
tiles = []
for name, pal_over in MEMBERS.items():
    pal = dict(PAL); pal.update(pal_over)
    grid = with_gear(mirror(HALF), name)
    im = render(grid, pal)
    im.save(f"{out}/{name}.png")
    tiles.append(im)
w, h = tiles[0].size
sheet = Image.new("RGBA", (w*4 + 30, h), (13, 7, 5, 255))
for i, t in enumerate(tiles):
    sheet.paste(t, (i*(w+10), 0), t)
sheet.convert("RGB").save("/tmp/council_sheet.png")
print("done")
