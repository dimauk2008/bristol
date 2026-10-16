import io
from fastapi.responses import StreamingResponse

try:
    from PIL import Image
except Exception:
    Image = None


def dm_matrix(data: str):
    """Возвращает матрицу Data Matrix (ISO/IEC 16022, ECC200) как список строк 0/1."""
    from ppf.datamatrix import DataMatrix
    return DataMatrix(data).matrix


def make_dm_png(data: str, box_size: int = 8, border: int = 4):
    """Data Matrix (ISO/IEC 16022, ECC200) в PNG.

    Монохромный: чёрные модули на чистом белом фоне, L-образный finder
    (сплошные линии слева и снизу), timing (чередование справа и сверху),
    тихая зона border модулей, без сглаживания (pixel-perfect, NEAREST).
    """
    if Image is None:
        raise RuntimeError('Pillow is not installed')
    mat = dm_matrix(data)
    rows = len(mat)
    cols = len(mat[0])
    img = Image.new('1', (cols + 2 * border, rows + 2 * border), 1)  # 1 = white
    px = img.load()
    for y in range(rows):
        row = mat[y]
        for x in range(cols):
            if row[x]:
                px[x + border, y + border] = 0  # black
    img = img.resize(((cols + 2 * border) * box_size,
                      (rows + 2 * border) * box_size), Image.NEAREST)
    buf = io.BytesIO()
    img.save(buf, format='PNG')
    buf.seek(0)
    return StreamingResponse(buf, media_type='image/png')
