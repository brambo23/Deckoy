"""Generates assets/deckoy.ico (the decoy duck, same shapes as ui/tray.py::app_icon) with Pillow. Run by build.ps1."""
import os

from PIL import Image, ImageDraw

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "..", "assets", "deckoy.ico")


def draw(size: int) -> Image.Image:
    s = size / 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    box = lambda x0, y0, x1, y1: (x0 * s, y0 * s, x1 * s, y1 * s)
    d.rounded_rectangle(box(2, 2, 62, 62), 12 * s, fill="#1c1f26")
    d.ellipse(box(14, 30, 50, 54), fill="#f2c230")        # body
    d.ellipse(box(22, 12, 44, 34), fill="#f2c230")        # head
    d.polygon([(42 * s, 20 * s), (54 * s, 24 * s), (42 * s, 28 * s)], fill="#e8782a")    # beak
    d.ellipse(box(34, 19, 38, 23), fill="#1c1f26")        # eye
    return img


def main() -> None:
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    draw(256).save(OUT, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    print("wrote", os.path.normpath(OUT))


if __name__ == "__main__":
    main()
