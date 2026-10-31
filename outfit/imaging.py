from __future__ import annotations

import base64
import binascii
import io
import re
import warnings

from PIL import Image, ImageOps, UnidentifiedImageError


class ImageInputError(ValueError):
    pass


DATA_URL = re.compile(r'^data:(image/(?:jpeg|png));base64,([A-Za-z0-9+/=]+)$')


def normalize_image(data_url: str) -> str:
    if not isinstance(data_url, str) or len(data_url) > 6_000_000:
        raise ImageInputError('照片数据过大，请重新拍摄')
    match = DATA_URL.fullmatch(data_url)
    if not match:
        raise ImageInputError('只支持 JPEG 或 PNG 图片')
    mime, encoded = match.groups()
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as error:
        raise ImageInputError('照片编码无效，请重新拍摄') from error
    if len(raw) > 4 * 1024 * 1024:
        raise ImageInputError('照片不能超过4MB，请重新拍摄')
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(raw)) as source:
                detected = source.format.lower()
                if detected not in ('jpeg', 'png') or (mime.endswith('jpeg') and detected != 'jpeg') or (mime.endswith('png') and detected != 'png'):
                    raise ImageInputError('图片格式与声明不一致')
                if source.width * source.height > 12_000_000:
                    raise ImageInputError('照片分辨率过高，请重新拍摄')
                source.verify()
            with Image.open(io.BytesIO(raw)) as source:
                if source.width < 100 or source.height < 100:
                    raise ImageInputError('照片太小，请拍摄包含完整穿搭的照片')
                if source.width * source.height > 12_000_000:
                    raise ImageInputError('照片分辨率过高，请重新拍摄')
                image = ImageOps.exif_transpose(source).convert('RGB')
                image.thumbnail((1280, 1280), Image.Resampling.LANCZOS)
                output = io.BytesIO()
                image.save(output, format='JPEG', quality=88, optimize=True)
    except ImageInputError:
        raise
    except (Image.DecompressionBombError, Image.DecompressionBombWarning, UnidentifiedImageError, OSError, ValueError) as error:
        raise ImageInputError('照片无法解析，请重新拍摄') from error
    encoded_output = base64.b64encode(output.getvalue()).decode('ascii')
    return 'data:image/jpeg;base64,' + encoded_output
