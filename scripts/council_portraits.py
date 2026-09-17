"""ARES council avatars — extracted from CobraLad's CC-BY 3.0
"32x32 Fantasy Portrait Set" (see static/council/LICENSE.txt).
Re-run to re-extract from the source sheet."""
import io
import sys
import urllib.request

from PIL import Image

SHEET_URL = "https://opengameart.org/sites/default/files/portraits.png"
XS = [5, 38, 71, 104, 137]
YS = [7, 40, 73]
PICKS = {"security": (2, 1), "correctness": (1, 0), "blast": (2, 2), "pragmatist": (0, 1),
         "precedent": (2, 0), "simplicity": (1, 3)}

def main(out_dir):
    data = urllib.request.urlopen(SHEET_URL, timeout=30).read()
    im = Image.open(io.BytesIO(data)).convert("RGB")
    for name, (r, c) in PICKS.items():
        tile = im.crop((XS[c], YS[r], XS[c] + 32, YS[r] + 32))
        tile.save(f"{out_dir}/{name}.png")
    print("done")

if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "static/council")
