from PIL import Image, ImageDraw, ImageFont

# --- Configuration (update paths if images are saved elsewhere) ---
LOGO_PATH = r"C:\Users\Sophie\Documents\YoutubeTutorial\LandlordData\carta_logo.png"
QR_PATH   = r"C:\Users\Sophie\Documents\YoutubeTutorial\LandlordData\carta_qr.png"
OUTPUT    = r"C:\Users\Sophie\Documents\YoutubeTutorial\LandlordData\carta_banner.png"

FONT_BOLD    = r"C:\Windows\Fonts\arialbd.ttf"
FONT_REGULAR = r"C:\Windows\Fonts\arial.ttf"

# Canvas: 24" x 62" @ 100 DPI — full bleed per template
# Safe zone: 100px inset on all sides (22" x 60")
W, H    = 2400, 6200
SAFE_X  = 100
WHITE   = (255, 255, 255)
BLACK   = (0, 0, 0)
GRAY    = (110, 110, 110)

SERVICES = [
    "Cap table management",
    "Electronic equity issuance",
    "SAFE and priced round modeling",
    "409A Valuations",
    "Investor Communications",
]


def load_font(path, size):
    try:
        return ImageFont.truetype(path, size)
    except OSError:
        return ImageFont.load_default()


def paste_centered(base, img_path, y_top, max_w, max_h):
    overlay = Image.open(img_path).convert("RGBA")
    overlay.thumbnail((max_w, max_h), Image.LANCZOS)
    x = (base.width - overlay.width) // 2
    base.paste(overlay, (x, y_top), overlay)
    return y_top + overlay.height


def draw_centered_text(draw, text, font, y, color=BLACK, canvas_w=W):
    bbox = draw.textbbox((0, 0), text, font=font)
    x = (canvas_w - (bbox[2] - bbox[0])) // 2
    draw.text((x, y), text, fill=color, font=font)
    return y + (bbox[3] - bbox[1])


def main():
    img  = Image.new("RGB", (W, H), WHITE)
    draw = ImageDraw.Draw(img)

    y = 400

    # Carta logo
    y = paste_centered(img, LOGO_PATH, y, max_w=1600, max_h=750)
    y += 500

    # Divider line
    draw.line([(SAFE_X + 150, y), (W - SAFE_X - 150, y)], fill=(30, 30, 30), width=4)
    y += 300

    # Header
    font_header = load_font(FONT_BOLD, 82)
    y = draw_centered_text(draw, "Come by for questions about:", font_header, y)
    y += 220

    # Service bullet points
    font_service = load_font(FONT_BOLD, 66)
    for service in SERVICES:
        y = draw_centered_text(draw, f"•  {service}", font_service, y)
        y += 55
    y += 550

    # QR code
    y = paste_centered(img, QR_PATH, y, max_w=1500, max_h=1500)
    y += 90

    # Caption
    font_caption = load_font(FONT_REGULAR, 52)
    draw_centered_text(draw, "Scan to learn more", font_caption, y, color=GRAY)

    img.save(OUTPUT, dpi=(100, 100))
    print(f"Saved: {OUTPUT}")
    print(f"Size: {W}x{H}px  ({W/100:.0f}\" x {H/100:.0f}\" @ 100 DPI)")


if __name__ == "__main__":
    main()
