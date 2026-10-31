import base64
import subprocess
from types import SimpleNamespace

import pytest

from outfit.capture import CaptureError, capture_image


def test_ffmpeg_is_preferred_and_returns_jpeg_data_url():
    calls = []
    result = SimpleNamespace(returncode=0, stdout=b'jpeg-bytes')

    def runner(command, **kwargs):
        calls.append((command, kwargs))
        return result

    value = capture_image('/dev/video2', runner=runner, which=lambda name: '/bin/' + name)
    assert value == 'data:image/jpeg;base64,' + base64.b64encode(b'jpeg-bytes').decode()
    assert calls[0][0][0] == '/bin/ffmpeg'
    assert calls[0][1]['timeout'] == 10
    assert calls[0][1]['check'] is False


def test_gstreamer_fallback_uses_array_without_shell():
    calls = []

    def runner(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0, stdout=b'jpeg')

    def which(name):
        return None if name == 'ffmpeg' else '/usr/bin/gst-launch-1.0'

    capture_image('/dev/video3', runner=runner, which=which)
    command = calls[0][0]
    assert command[:4] == ['/usr/bin/gst-launch-1.0', '-q', 'v4l2src', 'device=/dev/video3']
    assert command[-2:] == ['fdsink', 'fd=1']
    assert calls[0][1].get('shell', False) is False


@pytest.mark.parametrize('device', ['/dev/video', '/dev/video1;id', '/tmp/video1', '/dev/video../1'])
def test_camera_node_must_be_explicit_safe_device(device):
    with pytest.raises(CaptureError, match='实际彩色摄像头'):
        capture_image(device, which=lambda _: '/bin/tool')


def test_capture_rejects_timeout_and_oversized_output():
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], 10)

    with pytest.raises(CaptureError, match='未及时'):
        capture_image('/dev/video0', runner=timeout, which=lambda _: '/bin/ffmpeg')

    with pytest.raises(CaptureError, match='有效彩色照片'):
        capture_image('/dev/video0', runner=lambda *a, **k: SimpleNamespace(returncode=0, stdout=b'x' * (4 * 1024 * 1024 + 1)), which=lambda _: '/bin/ffmpeg')
