from io import BytesIO
from PIL import Image
def compress_jpeg(img_bytes:bytes, max_width:int=1600, quality:int=85):
    with Image.open(BytesIO(img_bytes)) as im:
        im=im.convert("RGB")
        w,h=im.size
        if w>max_width:
            r=max_width/float(w)
            im=im.resize((max_width, int(h*r)), Image.LANCZOS)
        out=BytesIO(); im.save(out, format="JPEG", quality=quality, optimize=True)
        data=out.getvalue()
        return data, im.width, im.height
