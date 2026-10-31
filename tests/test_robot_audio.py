import io
import math
import struct
from types import SimpleNamespace
import wave

import pytest

from outfit.robot_audio import RobotPlaybackError, mono_16k, publish_wav


def wav(samples, rate=16000):
    target = io.BytesIO()
    with wave.open(target, 'wb') as audio:
        audio.setnchannels(1); audio.setsampwidth(2); audio.setframerate(rate)
        audio.writeframes(struct.pack('<%dh' % len(samples), *samples))
    return target.getvalue()


def fake_sdk(agent_input=False):
    calls, sent = [], []
    controller = SimpleNamespace(init=lambda: calls.append('init') or True,
        get_internal_capture_audio_data_to_agent_enable=lambda: agent_input,
        get_external_custom_audio_data_to_agent_enable=lambda: False,
        get_internal_agent_audio_data_to_playback_enable=lambda: False,
        get_external_custom_audio_data_to_playback_enable=lambda: True,
        publish_external_audio_playback_stream=sent.append)
    class Stream: pass
    return SimpleNamespace(MediaController=SimpleNamespace(instance=lambda: controller), AudioStream=Stream), calls, sent


def test_disabled_by_default_before_sdk_load(monkeypatch):
    def forbidden(*args):raise AssertionError('SDK must not be loaded')
    monkeypatch.setattr('outfit.robot_audio._load_media', forbidden)
    with pytest.raises(RobotPlaybackError, match='默认关闭'):
        publish_wav(b'not even a WAV')


def test_active_native_agent_rejected_without_audio_or_configuration_writes():
    sdk, calls, sent = fake_sdk(agent_input=True)
    with pytest.raises(RobotPlaybackError, match='未隔离'):
        publish_wav(wav([10] * 320), enabled=True, sdk=sdk)
    assert calls == ['init'] and not sent


def test_explicit_publication_uses_signed_stereo_and_pads_last_frame():
    sdk, calls, sent = fake_sdk()
    result = publish_wav(wav([-100] * 400), enabled=True, sdk=sdk, wait=lambda _: None)
    assert result == {'status': 'sent', 'frames_sent': 2, 'audibility_verified': False}
    assert sent[0].audio_data == [-100] * 640
    assert sent[1].audio_data[:160] == [-100] * 160
    assert sent[1].audio_data[160:] == [0] * 480
    assert (sent[0].channels, sent[0].sample_rate, sent[0].format) == (2, 16000, 2)
    assert calls == ['init']


def test_publication_keeps_accumulated_pacing_under_coarse_wait():
    sdk, _, sent = fake_sdk()
    now = [0.0]
    def wait(delay):now[0] += math.ceil(delay / .016 - 1e-9) * .016
    publish_wav(wav([100] * 6400), enabled=True, sdk=sdk, clock=lambda: now[0], wait=wait)
    assert len(sent) == 20 and .38 <= now[0] <= .432


def test_resample_retains_dc_and_rejects_alias_energy():
    dc = struct.unpack('<3200h', mono_16k(wav([1000] * 4800, 24000)))
    assert max(abs(value - 1000) for value in dc) <= 1
    source = [round(10000 * math.sin(2 * math.pi * 11000 * index / 24000)) for index in range(4800)]
    filtered = struct.unpack('<3200h', mono_16k(wav(source, 24000)))[100:-100]
    rms = math.sqrt(sum(value * value for value in filtered) / len(filtered))
    assert rms < 1000
