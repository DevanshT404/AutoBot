"""Draw the BrowserBot icon (blue tile, white robot head) with stdlib only.

Usage:  python assets/make_icon.py assets
Writes browserbot.png (256px) and browserbot.ico (16/24/32/48/256) into the given folder.
"""
import math, os, struct, sys, zlib

OUT = sys.argv[1] if len(sys.argv) > 1 else os.path.dirname(os.path.abspath(__file__))

def box(px, py, cx, cy, hx, hy, r):
    qx, qy = abs(px - cx) - hx + r, abs(py - cy) - hy + r
    return math.hypot(max(qx, 0), max(qy, 0)) + min(max(qx, qy), 0) - r

def circ(px, py, cx, cy, r):
    return math.hypot(px - cx, py - cy) - r

TOP, BOT = (59, 130, 246), (29, 78, 216)
WHITE, DARK = (255, 255, 255), (30, 64, 175)

def layers(x, y):
    t = y
    bg = tuple(round(a + (b - a) * t) for a, b in zip(TOP, BOT))
    yield box(x, y, .5, .5, .47, .47, .21), bg
    yield box(x, y, .5, .25, .022, .07, .02), WHITE            # antenna stem
    yield circ(x, y, .5, .17, .055), WHITE                     # antenna ball
    yield box(x, y, .17, .57, .04, .08, .03), WHITE            # left ear
    yield box(x, y, .83, .57, .04, .08, .03), WHITE            # right ear
    yield box(x, y, .5, .57, .29, .23, .09), WHITE             # head
    yield circ(x, y, .39, .53, .065), DARK                     # eyes
    yield circ(x, y, .61, .53, .065), DARK
    yield box(x, y, .5, .68, .11, .025, .025), DARK            # mouth

def render(n):
    px = []
    for j in range(n):
        row = []
        for i in range(n):
            x, y = (i + .5) / n, (j + .5) / n
            r = g = b = a = 0.0
            for d, (cr, cg, cb) in layers(x, y):
                cov = min(max(.5 - d * n, 0), 1)
                if cov:
                    na = cov + a * (1 - cov)
                    r, g, b = ((c * cov + o * a * (1 - cov)) / na
                               for c, o in ((cr, r), (cg, g), (cb, b)))
                    a = na
            row.append((round(r), round(g), round(b), round(a * 255)))
        px.append(row)
    return px

def png(px):
    n = len(px)
    raw = b"".join(b"\0" + bytes(c for p in row for c in p) for row in px)
    chunk = lambda t, d: struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", n, n, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))

def dib(px):
    n = len(px)
    hdr = struct.pack("<IiiHHIIiiII", 40, n, n * 2, 1, 32, 0, 0, 0, 0, 0, 0)
    xor = b"".join(bytes((b, g, r, a)) for row in reversed(px) for r, g, b, a in row)
    and_mask = b"\0" * (((n + 31) // 32) * 4 * n)
    return hdr + xor + and_mask

os.makedirs(OUT, exist_ok=True)
images = {n: render(n) for n in (16, 24, 32, 48, 256)}
with open(os.path.join(OUT, "browserbot.png"), "wb") as f:
    f.write(png(images[256]))

blobs = [(n, png(p) if n == 256 else dib(p)) for n, p in images.items()]
offset = 6 + 16 * len(blobs)
ico = struct.pack("<HHH", 0, 1, len(blobs))
for n, data in blobs:
    ico += struct.pack("<BBBBHHII", n % 256, n % 256, 0, 0, 1, 32, len(data), offset)
    offset += len(data)
ico += b"".join(d for _, d in blobs)
with open(os.path.join(OUT, "browserbot.ico"), "wb") as f:
    f.write(ico)
print("ok", os.listdir(OUT))
