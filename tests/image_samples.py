from io import BytesIO

from PIL import Image


def make_image(format="PNG", size=(16, 12)):
    output = BytesIO()
    image = Image.new("RGBA" if format == "PNG" else "RGB", size, "red")
    image.save(output, format=format)
    return output.getvalue()
