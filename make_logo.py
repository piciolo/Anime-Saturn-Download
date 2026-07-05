"""Generate the app icon (assets/logo.png + assets/logo.ico).

A stylised Saturn (planet + tilted ring) in the app's purple accent on a dark rounded
square, with a small green download badge. Drawn at 4x supersampling and downscaled
for smooth edges.
"""
from __future__ import annotations

import os

from PIL import Image, ImageChops, ImageDraw, ImageFilter

SS = 4
S = 1024
W = S * SS


def px(v: float) -> int:
    return int(round(v * SS))


# --- rounded-square background: vertical gradient + soft purple glow ---------- #
top, bot = (0x22, 0x24, 0x33), (0x11, 0x12, 0x19)
col = Image.new("RGBA", (1, W))
cp = col.load()
for y in range(W):
    t = y / (W - 1)
    cp[0, y] = (
        int(top[0] * (1 - t) + bot[0] * t),
        int(top[1] * (1 - t) + bot[1] * t),
        int(top[2] * (1 - t) + bot[2] * t),
        255,
    )
grad = col.resize((W, W))

mask = Image.new("L", (W, W), 0)
ImageDraw.Draw(mask).rounded_rectangle([0, 0, W - 1, W - 1], radius=px(230), fill=255)
bg = Image.new("RGBA", (W, W), (0, 0, 0, 0))
bg.paste(grad, (0, 0), mask)

cx, cy = W // 2, int(W * 0.45)
glow = Image.new("RGBA", (W, W), (0, 0, 0, 0))
ImageDraw.Draw(glow).ellipse(
    [cx - px(300), cy - px(300), cx + px(300), cy + px(300)], fill=(124, 92, 255, 80)
)
glow = glow.filter(ImageFilter.GaussianBlur(px(85)))
glow.putalpha(ImageChops.multiply(glow.split()[3], mask))
bg = Image.alpha_composite(bg, glow)

# --- planet (purple disc with a soft top-left highlight) ---------------------- #
pr = px(205)
pmask = Image.new("L", (W, W), 0)
ImageDraw.Draw(pmask).ellipse([cx - pr, cy - pr, cx + pr, cy + pr], fill=255)

planet = Image.new("RGBA", (W, W), (0, 0, 0, 0))
ImageDraw.Draw(planet).ellipse(
    [cx - pr, cy - pr, cx + pr, cy + pr], fill=(140, 110, 250, 255)
)
hl = Image.new("RGBA", (W, W), (0, 0, 0, 0))
ImageDraw.Draw(hl).ellipse(
    [cx - pr * 0.75, cy - pr * 0.75, cx + pr * 0.15, cy + pr * 0.15],
    fill=(196, 180, 255, 255),
)
hl = hl.filter(ImageFilter.GaussianBlur(px(55)))
planet = Image.alpha_composite(planet, hl)
planet_clipped = Image.new("RGBA", (W, W), (0, 0, 0, 0))
planet_clipped.paste(planet, (0, 0), pmask)
planet = planet_clipped

# --- ring (tilted ellipse; back arc behind the planet, front arc in front) ---- #
rx, ry = px(345), px(120)
ring = Image.new("RGBA", (W, W), (0, 0, 0, 0))
ImageDraw.Draw(ring).ellipse(
    [cx - rx, cy - ry, cx + rx, cy + ry], outline=(206, 192, 255, 255), width=px(30)
)
ring = ring.rotate(-22, resample=Image.BICUBIC, center=(cx, cy))
ralpha = ring.split()[3]

top_half = Image.new("L", (W, W), 0)
ImageDraw.Draw(top_half).rectangle([0, 0, W, cy], fill=255)
bot_half = Image.new("L", (W, W), 0)
ImageDraw.Draw(bot_half).rectangle([0, cy, W, W], fill=255)

back = Image.new("RGBA", (W, W), (0, 0, 0, 0))
back.paste(ring, (0, 0), ImageChops.multiply(ralpha, top_half))
front = Image.new("RGBA", (W, W), (0, 0, 0, 0))
front.paste(ring, (0, 0), ImageChops.multiply(ralpha, bot_half))

art = Image.alpha_composite(bg, back)
art = Image.alpha_composite(art, planet)
art = Image.alpha_composite(art, front)

# --- download badge (bottom-right green circle + white arrow) ------------------ #
d = ImageDraw.Draw(art)
bx, by, br = int(W * 0.70), int(W * 0.72), px(150)
d.ellipse(
    [bx - br - px(16), by - br - px(16), bx + br + px(16), by + br + px(16)],
    fill=(0x11, 0x12, 0x19, 255),
)
d.ellipse([bx - br, by - br, bx + br, by + br], fill=(0x3E, 0xCF, 0x8E, 255))
d.rounded_rectangle(
    [bx - px(17), by - px(78), bx + px(17), by + px(28)], radius=px(17),
    fill=(255, 255, 255, 255),
)
d.polygon(
    [(bx - px(60), by - px(4)), (bx + px(60), by - px(4)), (bx, by + px(62))],
    fill=(255, 255, 255, 255),
)
d.rounded_rectangle(
    [bx - px(72), by + px(80), bx + px(72), by + px(104)], radius=px(12),
    fill=(255, 255, 255, 255),
)

# --- export ------------------------------------------------------------------- #
final = art.resize((S, S), Image.LANCZOS)
os.makedirs(os.path.join(os.path.dirname(__file__), "assets"), exist_ok=True)
png = os.path.join(os.path.dirname(__file__), "assets", "logo.png")
ico = os.path.join(os.path.dirname(__file__), "assets", "logo.ico")
final.save(png)
final.save(ico, sizes=[(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (16, 16)])
print("written:", png, final.size, "| ico:", ico)
