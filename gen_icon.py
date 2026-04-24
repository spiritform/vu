"""Generate assets/vu.ico — a black-and-white 'VU' mark for the exe."""
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

OUT = Path(__file__).parent / "assets" / "vu.ico"
OUT.parent.mkdir(exist_ok=True)

SIZES = [16, 24, 32, 48, 64, 128, 256]


def render(size: int) -> Image.Image:
    img = Image.new("RGBA", (size, size), (0, 0, 0, 255))
    d = ImageDraw.Draw(img)
    font_size = int(size * 0.58)
    try:
        font = ImageFont.truetype("arialbd.ttf", font_size)
    except Exception:
        try:
            font = ImageFont.truetype("arial.ttf", font_size)
        except Exception:
            font = ImageFont.load_default()
    text = "VU"
    bbox = d.textbbox((0, 0), text, font=font)
    w = bbox[2] - bbox[0]
    h = bbox[3] - bbox[1]
    d.text(((size - w) / 2 - bbox[0], (size - h) / 2 - bbox[1]), text, fill=(255, 255, 255, 255), font=font)
    return img


frames = [render(s) for s in SIZES]
# PIL auto-picks the largest and embeds at all requested sizes
frames[-1].save(OUT, format="ICO", sizes=[(s, s) for s in SIZES], append_images=frames[:-1])
print(f"wrote {OUT}")

# Also emit macOS .icns
ICNS_OUT = OUT.parent / "vu.icns"
ICNS_SIZES = [16, 32, 64, 128, 256, 512]
icns_frames = [render(s) for s in ICNS_SIZES]
try:
    icns_frames[-1].save(
        ICNS_OUT,
        format="ICNS",
        sizes=[(s, s) for s in ICNS_SIZES],
        append_images=icns_frames[:-1],
    )
    print(f"wrote {ICNS_OUT}")
except Exception as e:
    print(f"[warn] could not write {ICNS_OUT}: {e}")
