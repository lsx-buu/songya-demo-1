import io
import json
import struct
from types import SimpleNamespace
import wave

import pytest

from outfit.cloud_protocol import decode_frame
from outfit.speech import VoiceConfig, VoiceError, synthesize


def server_frame(event, payload, session=''):
    raw = isinstance(payload, bytes)
    encoded = payload if raw else json.dumps(payload).encode()
    identifier = b'connection' if event in (50, 51, 52) else session.encode()
    header = bytes((0x11, ((11 if raw else 9) << 4) | 4, 0 if raw else 0x10, 0))
    return header + struct.pack('>II', event, len(identifier)) + identifier + struct.pack('>I', len(encoded)) + encoded


class Socket:
    def __init__(self, *, broken_audio=False, fail_finish=False):
        self.frames, self.sent = [], []
        self.closed = False
        self.broken_audio, self.fail_finish = broken_audio, fail_finish

    def send(self, raw):
        frame = decode_frame(raw)
        self.sent.append(frame)
        session = frame.session_id
        if frame.event == 1:
            self.frames.append(server_frame(50, {}))
        elif frame.event == 100:
            self.frames.append(server_frame(150, {}, session))
        elif frame.event == 300:
            self.frames += [server_frame(350, {'tts_type': 'say_hello'}, session),
                            server_frame(352, b'\0' if self.broken_audio else b'\1\0' * 480, session),
                            server_frame(359, {}, session)]
        elif frame.event == 102:
            self.frames.append(server_frame(152, {}, session))
        elif frame.event == 2 and self.fail_finish:
            raise RuntimeError('private-provider-value')

    def recv(self, timeout):
        return self.frames.pop(0)

    def close(self):
        self.closed = True


def config():
    return {'app_id': 'test-app', 'access_key': 'test-secret', 'model': '1.2.1.1',
            'speaker': 'zh_female_vv_jupiter_bigtts'}


def test_direct_text_speech_has_no_mic_or_chat_query():
    socket = Socket()
    calls = []
    def connector(url, **kwargs):
        calls.append((url, kwargs))
        return socket
    wav = synthesize('上半身在融资，下半身在撤资。', config(), connector=connector)
    assert [frame.event for frame in socket.sent] == [1, 100, 300, 102, 2]
    assert socket.sent[2].payload == {'content': '上半身在融资，下半身在撤资。'}
    assert calls[0][1]['additional_headers']['X-Api-Access-Key'] == 'test-secret'
    assert socket.closed
    with wave.open(io.BytesIO(wav)) as audio:
        assert (audio.getframerate(), audio.getnchannels(), audio.getsampwidth()) == (24000, 1, 2)
        assert audio.readframes(480) == b'\1\0' * 480


def test_speech_provenance_matches_returned_wav():
    socket = Socket()
    socket.response = SimpleNamespace(headers={'x-tt-logid':'speech-test-request'})
    captured = []
    wav = synthesize('测试。', config(), connector=lambda *a, **kw: socket,
                     on_provenance=captured.append)
    assert len(captured) == 1
    assert captured[0]['source'] == 'volcengine_realtime'
    assert captured[0]['event'] == 300
    assert captured[0]['request_id'] == 'speech-test-request'
    assert captured[0]['wav_sha256'] == __import__('hashlib').sha256(wav).hexdigest()
    assert captured[0]['duration_seconds'] == 480 / 24000


def test_invalid_audio_cleans_up_and_close_still_runs_if_finish_send_fails():
    socket = Socket(broken_audio=True, fail_finish=True)
    with pytest.raises(VoiceError, match='不完整'):
        synthesize('测试。', config(), connector=lambda *a, **kw: socket)
    assert socket.closed
    assert [frame.event for frame in socket.sent][-2:] == [102, 2]


def test_authentication_error_never_echoes_private_provider_exception():
    def connector(*args, **kwargs):
        error = RuntimeError('test-secret-private-body')
        error.response = SimpleNamespace(status_code=403)
        raise error
    with pytest.raises(VoiceError, match='鉴权失败') as caught:
        synthesize('测试。', config(), connector=connector)
    assert 'test-secret' not in str(caught.value)


def test_voice_config_masks_and_preserves_credentials(tmp_path):
    store = VoiceConfig(tmp_path)
    assert not store.public()['configured']
    public = store.save('test-app', 'test-secret')
    assert public['configured'] and 'test-secret' not in json.dumps(public)
    assert 'test-app' not in json.dumps(public)
    store.save(speaker='another-speaker')
    assert store.read()['access_key'] == 'test-secret'
    assert store.read()['speaker'] == 'another-speaker'
    with pytest.raises(VoiceError):
        store.save(access_key='bad\nkey')
    assert store.read()['access_key'] == 'test-secret'


def test_unconfigured_speech_never_connects():
    touched = []
    values = config()
    values['access_key'] = ''
    with pytest.raises(VoiceError, match='先配置'):
        synthesize('测试。', values, connector=lambda *a, **kw: touched.append(True))
    assert not touched
