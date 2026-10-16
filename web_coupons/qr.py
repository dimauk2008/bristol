import io
import qrcode
from qrcode.util import QRData, MODE_8BIT_BYTE
from fastapi.responses import StreamingResponse


def make_qr_png(data: str, box_size: int = 8, border: int = 4,
                error_correction: int = qrcode.constants.ERROR_CORRECT_M,
                fill: str = '#000000', back: str = '#ffffff',
                force_byte: bool = False, version: int = None):
    """QR в соответствии с ISO/IEC 18004.

    border=4 — минимальная «тихая зона» (quiet zone) 4 модуля по стандарту.
    Каждый модуль — целое число пикселей (box_size), без масштабирования,
    чтобы изображение оставалось корректно сканируемым.

    force_byte=True — кодирует в байтовом режиме (как в приложении Бристоль).
    version=N — фиксирует версию (например 3 → матрица 29×29 модулей).
    """
    qr = qrcode.QRCode(
        version=version,
        error_correction=error_correction,
        box_size=box_size,
        border=border,
    )
    if force_byte:
        qr.add_data(QRData(data.encode('utf-8'), mode=MODE_8BIT_BYTE))
    else:
        qr.add_data(data)
    qr.make(fit=(version is None))
    img = qr.make_image(fill_color=fill, back_color=back)
    buf = io.BytesIO()
    img.save(buf, format='PNG')
    buf.seek(0)
    return StreamingResponse(buf, media_type='image/png')


def qr_version(data: str, error_correction=qrcode.constants.ERROR_CORRECT_M,
               force_byte=False, version=None):
    """Возвращает номер версии QR (для отладки/проверки)."""
    qr = qrcode.QRCode(version=version, error_correction=error_correction, border=4)
    if force_byte:
        qr.add_data(QRData(data.encode('utf-8'), mode=MODE_8BIT_BYTE))
    else:
        qr.add_data(data)
    qr.make(fit=(version is None))
    return qr.version, qr.modules_count
