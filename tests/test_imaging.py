import base64
import io

import pytest
from PIL import Image

from outfit.imaging import ImageInputError, normalize_image


def image_data(format='PNG', size=(180, 240)):
    output = io.BytesIO()
    Image.new('RGB', size, (20, 100, 120)).save(output, format=format)
    mime = 'png' if format == 'PNG' else 'jpeg'
    return f'data:image/{mime};base64,' + base64.b64encode(output.getvalue()).decode()


def test_normalize_reorients_and_strips_metadata():
    result = normalize_image(image_data())
    assert result.startswith('data:image/jpeg;base64,')
    with Image.open(io.BytesIO(base64.b64decode(result.split(',', 1)[1]))) as image:
        assert image.format == 'JPEG' and image.size == (180, 240)
        assert not image.getexif()


@pytest.mark.parametrize('value', ['', 'data:image/gif;base64,AAAA', 'data:image/png;base64,not-base64'])
def test_rejects_bad_data_urls(value):
    with pytest.raises(ImageInputError):
        normalize_image(value)


def test_rejects_tiny_image():
    with pytest.raises(ImageInputError, match='太小'):
        normalize_image(image_data(size=(20, 20)))


def test_rejects_mismatched_mime():
    value = image_data('PNG').replace('data:image/png', 'data:image/jpeg')
    with pytest.raises(ImageInputError):
        normalize_image(value)


def test_rejects_decompression_bomb_without_allocating(monkeypatch):
    from PIL import Image
    from outfit import imaging

    def bomb(_):
        raise Image.DecompressionBombError('too many pixels')

    monkeypatch.setattr(imaging.Image, 'open', bomb)
    with pytest.raises(ImageInputError):
        normalize_image(image_data())
