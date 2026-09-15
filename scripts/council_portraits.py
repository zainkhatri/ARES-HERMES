"""Council portraits v4 — SF2 SNES select-portrait style.
Hand-authored ~26x30 grid, 3/4 profile facing left, chunky pixels,
thick black sticker outline, flat palette. Per-member palette+gear swaps."""
from PIL import Image
import sys

# palette letters:
# . transparent | K black outline | H hair | h hair-light | B band | b band-dark
# S skin | s skin-shadow | d skin-dark | W eye-white | E iris
# C cloth | c cloth-shadow | w collar-white | g collar-shadow
BASE = [
".....K...K..............",
"....KhK.KhK..K..........",
"...KhhhKhhhKKhK.........",
"..KhhhhhhhhhhhhK........",
".KhhhhHhhhhHhhhhK.......",
".KhHhhhhhHhhhhhhhK......",
".KhhhhHhhhhhhHhhhhK.....",
".KhhhhhhhhhhhhhhhhK.....",
".KhhhhhhhhhhhhhhhhhK....",
".KKBBBBBBBBBBBBKhhhK....",
".KBBbBBBBBBBBbBKhhhhK...",
".KhKBBBBBBBBBBbKKhKK....",
".KhKSSSSSSSSSsKBBKBBK...",
".KSSKKKKSSSSSssKBBBBBK..",
".KSKWWESSSSSSssKbBBK....",
"KSSKKKKSSSSSssKsKBK.....",
".KSSSSSSSSSSssssK.......",
".KsSSSSSSSSSssssK.......",
"KSSSSSSSSSsssssK........",
".KdSSSSSSSssssK.........",
".KSSSSSSSsssssK.........",
"..KdKKSsssssssK.........",
"...KSSSSssssKssK........",
"....KKKssssKssssK.......",
".......KKKKssssssK......",
"....KwwwKssssssssKK.....",
"...KwwwwwKssssssKCCK....",
"..KwwwwwwwKssssKCCCCK...",
".KgwwwwwwwwKKKKCCCCCCK..",
".KwwwwwwwwwwKCCCCCCCCCK.",
]

def render(grid, pal, scale=16):
    h = len(grid); w = max(len(r) for r in grid)
    im = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    for y, row in enumerate(grid):
        for x, ch in enumerate(row):
            if ch != '.':
                im.putpixel((x, y), pal[ch] + (255,))
    return im.resize((w*scale, h*scale), Image.NEAREST)

def with_gear(grid, member):
    g = [list(r) for r in grid]
    def put(x, y, ch):
        g[y][x] = ch
    if member == "correctness":   # spectacle frame around the eye
        put(2, 14, 'K'); put(7, 14, 'K')
        for x in range(3, 7): put(x, 16, 'K')
    if member == "blast":         # eyepatch block + cheek scar
        for x in range(3, 8):
            put(x, 14, 'K'); put(x, 15, 'K')
        put(10, 18, 'd'); put(11, 19, 'd'); put(10, 20, 'd')
    return ["".join(r) for r in g]

PALS = {
 # warden: Ken-blond hair, red band, white gi
 "security":    {'K':(16,10,10),'H':(196,150,58),'h':(232,190,88),'B':(196,40,32),'b':(140,26,22),
                 'S':(244,192,140),'s':(210,144,96),'d':(160,100,66),'W':(240,240,232),'E':(70,110,150),
                 'C':(198,44,36),'c':(150,30,26),'w':(238,236,230),'g':(190,186,178)},
 # inspector: grey hair, no band (band recolored to skin-ish headwrap? keep band navy), brown gi
 "correctness": {'K':(16,10,10),'H':(168,168,168),'h':(214,214,210),'B':(52,74,116),'b':(38,54,86),
                 'S':(244,192,140),'s':(210,144,96),'d':(160,100,66),'W':(240,240,232),'E':(90,64,40),
                 'C':(110,80,52),'c':(82,58,38),'w':(226,222,212),'g':(180,174,162)},
 # assessor: dark hair, black band, dark red top
 "blast":       {'K':(16,10,10),'H':(50,40,38),'h':(84,70,66),'B':(30,26,26),'b':(18,16,16),
                 'S':(238,180,126),'s':(200,132,86),'d':(150,92,60),'W':(240,240,232),'E':(60,90,140),
                 'C':(122,36,30),'c':(88,26,22),'w':(216,208,196),'g':(168,158,144)},
 # operator: blond flat-ish hair, olive band + olive top
 "pragmatist":  {'K':(16,10,10),'H':(206,168,74),'h':(238,210,120),'B':(96,108,66),'b':(68,78,46),
                 'S':(240,186,132),'s':(204,138,92),'d':(154,96,62),'W':(240,240,232),'E':(96,82,56),
                 'C':(88,100,62),'c':(62,72,44),'w':(212,206,190),'g':(164,158,142)},
}

out = sys.argv[1] if len(sys.argv) > 1 else "/tmp"
tiles = []
for name, pal in PALS.items():
    im = render(with_gear(BASE, name), pal)
    im.save(f"{out}/{name}.png")
    tiles.append(im)
w, h = tiles[0].size
sheet = Image.new("RGBA", (w*4 + 30, h), (26, 12, 10, 255))
for i, t in enumerate(tiles):
    sheet.paste(t, (i*(w+10), 0), t)
sheet.convert("RGB").save("/tmp/council_sheet.png")
print("done")
