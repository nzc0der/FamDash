from PIL import Image, ImageDraw

for size in [192, 512]:
    img = Image.new("RGB", (size, size), "#a78bfa")
    draw = ImageDraw.Draw(img)

    text = "FD"
    bbox = draw.textbbox((0, 0), text)
    x = (size - (bbox[2] - bbox[0])) // 2
    y = (size - (bbox[3] - bbox[1])) // 2

    draw.text((x, y), text, fill="white")

    img.save(f"static/icons/icon-{size}.png")