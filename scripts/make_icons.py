"""Generate the PWA PNG icons (192 and 512 px) with Pillow.

python scripts/make_icons.py
"""

from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path(__file__).resolve().parent.parent / "jobcopilot/web/static/icons"


def draw(size: int) -> Image.Image:
    """Briefcase with a check mark, matching icon.svg."""
    s = size / 512
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([0, 0, size - 1, size - 1], radius=int(96 * s), fill="#1f4e79")
    w = max(2, int(28 * s))
    d.rounded_rectangle(
        [136 * s, 196 * s, 376 * s, 364 * s], radius=int(24 * s), outline="white", width=w
    )
    d.rounded_rectangle(
        [208 * s, 144 * s, 304 * s, 210 * s], radius=int(24 * s), outline="white", width=w
    )
    d.rectangle([150 * s, 205 * s, 362 * s, 214 * s], fill="white")
    d.line(
        [(200 * s, 282 * s), (240 * s, 318 * s), (312 * s, 238 * s)],
        fill="#7fd1a8",
        width=w,
        joint="curve",
    )
    return img


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    for px in (192, 512):
        draw(px).save(OUT / f"icon-{px}.png")
        print(f"wrote {OUT / f'icon-{px}.png'}")
