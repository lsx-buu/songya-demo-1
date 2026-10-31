import json
import struct
import threading
import time
from collections import deque

import pytest

from outfit.cloud_protocol import decode_frame
from outfit.speech import VoiceError
from outfit.voice_trigger import wait_for_trigger


def server_frame(event, payload, session=''):
    raw = isinstance(payload, bytes)
    encoded = payload if raw else json.dumps(payload).encode()
    identifier = b'connection' if event in (50, 51, 52) else session.encode()
    header = bytes((0x11, ((11 if raw else 9) << 4) | 4, 0 if raw else 0x10, 0))
    return header + struct.pack('>II', event, len(identifier)) + identifier + struct.pack('>I', len(encoded)) + encoded


CONFIG = {'app_id': 'test-app', 'access_key': 'test-secret', 'model': '1.2.1.1',
          'speaker': 'zh_female_vv_jupiter_bigtts'}


class Socket:
    def __init__(self, final_text=None, intermediate=None):
        self.frames = deque()
        self.sent = []
        self.closed = False
        self.final_text = final_text
        self.intermediate = intermediate

    def send(self, raw):
        frame = decode_frame(raw)
        self.sent.append(frame)
        if frame.event == 1:
            self.frames.append(server_frame(50, {}))
        elif frame.event == 100:
            self.frames.append(server_frame(150, {}, frame.session_id))
            if self.intermediate is not None:
                self.frames.append(server_frame(451, {'results': [{'text': self.intermediate}]} , frame.session_id))
            if self.final_text is not None:
                self.frames.append(server_frame(459, {}, frame.session_id))
        elif frame.event == 102:
            self.frames.append(server_frame(152, {}, frame.session_id))

    def recv(self, timeout):
        if self.frames:
            time.sleep(.003)
            return self.frames.popleft()
        time.sleep(min(timeout, .003))
        raise TimeoutError()

    def close(self):
        self.closed = True


def test_final_keyword_triggers_but_intermediate_does_not():
    socket = Socket(final_text='锐评一下', intermediate='锐评一下')
    calls = []
    result = wait_for_trigger(CONFIG, lambda timeout: calls.append(timeout) or b'\0' * 640,
                              threading.Event(), connector=lambda *a, **k: socket, max_seconds=2)
    assert result is True
    assert calls
    assert [frame.event for frame in socket.sent[:3]] == [1, 100, 200]
    assert socket.sent[2].payload == b'\0' * 640
    assert socket.sent[1].payload['asr']['extra']['context']['hotwords'] == [{'word':'锐评一下'}]
    assert socket.closed
    assert [frame.event for frame in socket.sent][-2:] == [102, 2]


@pytest.mark.parametrize('text', ['锐评一下', '锐评一下啊'])
def test_only_exact_final_keyword_matches(text):
    socket = Socket(final_text=text, intermediate=text)
    result = wait_for_trigger(CONFIG, lambda timeout: b'\0' * 640,
                              threading.Event(), connector=lambda *a, **k: socket, max_seconds=2)
    assert result is (text == '锐评一下')


def test_new_utterance_clears_old_intermediate_before_empty_final():
    socket = Socket(final_text='锐评一下', intermediate='锐评一下')
    original_send = socket.send
    def send_with_new_sentence(raw):
        original_send(raw)
        frame = decode_frame(raw)
        if frame.event == 100:
            socket.frames.clear()
            socket.frames.append(server_frame(150, {}, frame.session_id))
            socket.frames.append(server_frame(450, {}, frame.session_id))
            socket.frames.append(server_frame(451, {'results': [{'text': '你好'}]}, frame.session_id))
            socket.frames.append(server_frame(459, {}, frame.session_id))
    socket.send = send_with_new_sentence
    assert wait_for_trigger(CONFIG, lambda timeout: b'\0' * 640, threading.Event(),
                            connector=lambda *a, **k: socket, max_seconds=2) is False


def test_external_stop_interrupts_receive_promptly_without_upload():
    socket = Socket()
    stop = threading.Event()
    result = []
    thread = threading.Thread(target=lambda: result.append(wait_for_trigger(
        CONFIG, lambda timeout: b'\0' * 640, stop, connector=lambda *a, **k: socket, max_seconds=55)))
    thread.start()
    time.sleep(.05)
    stop.set()
    thread.join(timeout=1)
    assert not thread.is_alive()
    assert result == [False]


def test_external_stop_does_not_connect_or_upload():
    stop = threading.Event()
    stop.set()
    touched = []
    assert wait_for_trigger(CONFIG, lambda timeout: touched.append(True), stop,
                            connector=lambda *a, **k: touched.append(True), max_seconds=2) is False
    assert touched == []


def test_service_failure_is_sanitized_and_cleanup_closes_socket():
    socket = Socket()
    def connector(*args, **kwargs):
        raise RuntimeError('test-secret-provider-body')
    with pytest.raises(VoiceError) as error:
        wait_for_trigger(CONFIG, lambda timeout: b'\0' * 640, threading.Event(), connector=connector)
    assert 'test-secret' not in str(error.value)


def test_invalid_pcm_does_not_touch_hardware_and_cleans_up():
    socket = Socket(final_text='锐评一下')
    with pytest.raises(VoiceError, match='20ms'):
        wait_for_trigger(CONFIG, lambda timeout: b'bad', threading.Event(),
                         connector=lambda *a, **k: socket, max_seconds=2)
    assert socket.closed
