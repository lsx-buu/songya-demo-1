from types import SimpleNamespace
import struct

import pytest

from outfit.microphone import Microphone
from outfit.robot_audio import RobotPlaybackError
from test_robot_audio import fake_sdk


def controller():
    sdk, calls, _ = fake_sdk()
    control = sdk.MediaController.instance()
    control.subscribe_internal_audio_capture = lambda callback: setattr(control, 'callback', callback)
    control.unsubscribe_internal_audio_capture = lambda: calls.append('unsubscribe')
    return control, calls


def test_disabled_and_speaker_reference_channels_are_rejected():
    control, calls = controller()
    with pytest.raises(RobotPlaybackError): Microphone(control)
    with pytest.raises(RobotPlaybackError): Microphone(control, enabled=True, channel=6)
    assert not calls and not hasattr(control, 'callback')


def test_head_channel_stream_frames_and_callback_cleanup():
    control, calls = controller()
    with Microphone(control, enabled=True, channel=1) as mic:
        control.callback(SimpleNamespace(channels=8,sample_rate=16000,format=2,
                                         audio_data=[10,-123,30,40,0,0,999,999]*320))
        assert mic.read() == struct.pack('<320h', *([-123]*320))
    assert calls == ['unsubscribe']
    control.callback(SimpleNamespace())  # The stale callback is inert.
    assert mic.read(0) is None


def test_active_native_agent_blocks_subscription():
    control, calls = controller()
    control.get_internal_capture_audio_data_to_agent_enable = lambda: True
    with pytest.raises(RobotPlaybackError,match='未隔离'):
        with Microphone(control,enabled=True): pass
    assert not hasattr(control, 'callback') and not calls


def test_bad_stream_stops_listening_and_still_unsubscribes():
    control, calls = controller()
    with pytest.raises(RobotPlaybackError, match='格式无效'):
        with Microphone(control,enabled=True) as mic:
            control.callback(SimpleNamespace(channels=2,sample_rate=16000,format=2,audio_data=[0]*640))
            mic.read(0)
    assert calls == ['unsubscribe']


def test_microphone_buffer_is_bounded_to_half_second():
    control, _ = controller()
    with Microphone(control,enabled=True) as mic:
        for sample in range(40):
            control.callback(SimpleNamespace(channels=8,sample_rate=16000,format=2,audio_data=[sample]*2560))
        assert len(mic.frames) == 25
        assert struct.unpack('<h', mic.read()[:2])[0] == 15
