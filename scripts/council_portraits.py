"""Council portraits v5 — authored at reference fidelity (SNES SF2 select style).
28x32 grid, 12-color palette, 3/4 view facing left, tilted head, hair over band,
band tails down the right side. One base per member with hair/gear variations."""
from PIL import Image
import sys

# K outline | k deep shadow | L hair light | H hair mid | D hair dark
# B band bright | b band dark | S skin light | M skin mid | s skin shadow | d skin deep
# W white | w white shadow | E eye | C cloth | c cloth shadow
WARDEN = [
"....KKKK..KKK...............",
"...KLLLHKKHHHKK.............",
"..KLLHHHHHHHDDHK............",
".KLLHHLLHHHDDDDHK...........",
".KLHHLLHHHHDDDDDDK..........",
"KLHHHHHHHDDHDDDDDDK.........",
"KHHLHHHHDDDDDDDDDDDK........",
"KHHHHHDDDDDDDDDDDDDK........",
".KHHKBBBBBBBBDDDDDDK........",
".KHKBBBHKBBBBBKDDDDK........",
".KHKBbBHKBBBBBBKDDK.........",
"..KKBBbDKBBBBBbBKBBK........",
"..KSSKKBDKBBBBKSKBBBK.......",
"..KSMMKKKKKKKMMsKKBBBK......",
".KSKWWEEKSSSSMMssKBBBK......",
".KSSKEEKSSSSSMMssKbBBK......",
".KSSSSSSsSSSMMMsssKBBK......",
"KSSSSSSSSsSSMMMsssKBbK......",
"KSsSSSSSSSSSMMssssKbBK......",
".KdSSSSSSSSMMsssdKBBbK......",
".KsSSSSSSSMMMssddKBbK.......",
".KsSKKKSSSMMssdddKbK........",
"..KsSsSSMMMssdddKBbK........",
"...KSSdSMMssdddKKbBK........",
"....KKsMMssdddKssKbK........",
".....KsMssddKssssKK.........",
"....KsssssKKsssssssK........",
"...KWWWWKssssssssssK........",
"..KWWWWWWKsssssssssdK.......",
".KwWWWWWWWKssssssKddK.......",
"KwWWWWWWWWWKssssKdddK.......",
"KWWWWWWWWWWWKssKddddK.......",
]

def render(grid, pal, scale=16):
    h = len(grid); w = max(len(r) for r in grid)
    im = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    for y, row in enumerate(grid):
        for x, ch in enumerate(row):
            if ch != '.':
                im.putpixel((x, y), pal.get(ch, (255, 0, 255)) + (255,))
    return im.resize((w*scale, h*scale), Image.NEAREST)

PAL = {
 'K': (20, 4, 14),  'k': (70, 12, 8),
 'L': (246, 214, 128), 'H': (222, 168, 70), 'D': (128, 42, 20),
 'B': (226, 48, 28), 'b': (150, 22, 14),
 'S': (246, 198, 152), 'M': (228, 158, 108), 's': (198, 112, 72), 'd': (142, 58, 34),
 'W': (240, 238, 232), 'w': (198, 196, 190), 'E': (130, 160, 190),
 'l': (198, 112, 72),
}
def with_gear(grid, member):
    g = [list(r.ljust(28, '.')) for r in grid]
    def put(x, y, ch): g[y][x] = ch
    if member == "correctness":
        # spectacles: frame under+around the eye, arm back to the ear
        for x in range(3, 9): put(x, 16, 'K')
        put(2, 14, 'K'); put(2, 15, 'K'); put(8, 14, 'K'); put(8, 15, 'K')
        for x in range(9, 16): put(x, 15, 'K')
    if member == "blast":
        # eyepatch over the eye + strap up into the band
        for y in (13, 14, 15):
            for x in range(2, 9): put(x, y, 'K')
        put(9, 12, 'K'); put(10, 11, 'K')
        # cheek scar
        put(5, 19, 'd'); put(6, 20, 'd'); put(5, 21, 'd')
    if member == "pragmatist":
        # jaw stubble
        for x, y in [(3,21),(5,22),(4,23),(6,23),(8,23),(7,22),(9,22),(6,21),(10,21),(8,21)]:
            if g[y][x] in ('S','M','s'): put(x, y, 'M' if g[y][x]=='S' else 'd')
    return ["".join(r) for r in g]

def recolor_cloth(im_grid, member):
    # gi rows (>=26): W/w become member cloth colors via palette override at render time
    return im_grid

BASE_PAL = dict(PAL)
MEMBERS = {
 "security":    {"pal": {}, },
 "correctness": {"pal": {'L': (232,232,228), 'H': (196,198,200), 'D': (110,114,122),
                          'B': (56,74,116), 'b': (36,50,82)}, },
 "blast":       {"pal": {'L': (110,86,60), 'H': (78,56,40), 'D': (44,30,22),
                          'B': (44,38,36), 'b': (26,22,20)}, },
 "pragmatist":  {"pal": {'L': (238,214,140), 'H': (204,168,88), 'D': (140,102,44),
                          'B': (108,118,72), 'b': (74,84,50)}, },
}
CLOTH = {  # replaces gi white in the shoulder rows
 "security":    None,                       # keeps white gi
 "correctness": ((122,90,58), (88,64,42)),
 "blast":       ((134,40,32), (94,26,22)),
 "pragmatist":  ((96,108,66), (66,76,48)),
}

out = sys.argv[1] if len(sys.argv) > 1 else "/tmp"
tiles = []
for name, spec in MEMBERS.items():
    pal = dict(BASE_PAL); pal.update(spec["pal"])
    grid = with_gear(WARDEN, name)
    if CLOTH[name]:
        c, csh = CLOTH[name]
        grid = [row if y < 26 else row.replace('W', 'C').replace('w', 'c')
                for y, row in enumerate(grid)]
        pal['C'] = c; pal['c'] = csh
    im = render(grid, pal)
    im.save(f"{out}/{name}.png")
    tiles.append(im)
w, h = tiles[0].size
sheet = Image.new("RGBA", (w*4 + 30, h), (15, 8, 6, 255))
for i, t in enumerate(tiles):
    sheet.paste(t, (i*(w+10), 0), t)
sheet.convert("RGB").save("/tmp/council_sheet.png")
print("done")
