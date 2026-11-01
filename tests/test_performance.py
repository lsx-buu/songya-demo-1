import threading
from types import SimpleNamespace

import pytest

from outfit.performance import NEXT_TEXT, perform_turn


class Stop:
    def __init__(self): self.value = False
    def set(self): self.value = True
    def is_set(self): return self.value
    def wait(self, seconds): return self.value


def pipeline(*, retake=False, fail_review=False, stop_in_review=False,
             stop_in_thought=False, stop_in_playback=False, revoke=False):
    calls, logs = [], []
    stop = Stop()
    permission = {'value': True}

    class Gestures:
        def start(self, name, run_id):
            calls.append(name)
            if stop_in_thought and name == 'thinking': stop.set()
            return {'status': 'skipped', 'action': name, 'observed': False}
        def finish(self):
            calls.append('finish')
            return None

    def capture(camera):
        calls.append('capture')
        return 'image'

    def review(*args):
        calls.append('review')
        if fail_review: raise RuntimeError('cloud unavailable')
        if stop_in_review: stop.set()
        return {'status': 'retake' if retake else 'ok', 'roast': '卫衣和裤子各忙各的。',
                'provenance': {'prompt_version': 'roast-v3', 'response_id': 'test-only'}}

    def synthesize(text, voice, *, on_provenance):
        calls.append(('tts', text))
        on_provenance({'source': 'injected_client'})
        return text.encode()

    def playback(audio, *, stop):
        calls.append(('play', audio.decode()))
        if stop_in_playback: stop.set()
        if revoke: permission['value'] = False
        return {'status': 'sent', 'audibility_verified': False}

    return SimpleNamespace(calls=calls, logs=logs, stop=stop,
        kwargs=dict(config={'camera': '/dev/video2'}, stop=stop, root=None, run_id='one-turn',
            vision={'model': 'test', 'api_key': 'fake'}, voice={}, gestures=Gestures(),
            capture=capture, normalize=lambda image: image, review=review,
            synthesize=synthesize, playback=playback,
            record=lambda root, kind, details: logs.append((kind, details)),
            report=lambda *args, **kwargs: None,
            permitted=lambda: permission['value']))


def test_snapshot_then_thought_then_roast_then_wave_and_next():
    p = pipeline()
    perform_turn(**p.kwargs)
    assert p.calls == ['capture', 'thinking', 'review',
        ('tts', '布米嘴替开评。卫衣和裤子各忙各的。'), ('tts', NEXT_TEXT),
        'finish', ('play', '布米嘴替开评。卫衣和裤子各忙各的。'),
        'dismiss', ('play', NEXT_TEXT), 'finish']
    assert all(details['run_id'] == 'one-turn' for _, details in p.logs)
    assert [d['phase'] for kind, d in p.logs if kind == 'playback'] == ['review', 'dismiss']


def test_retake_never_dismisses_or_says_next():
    p = pipeline(retake=True)
    perform_turn(**p.kwargs)
    assert 'dismiss' not in p.calls
    assert ('tts', NEXT_TEXT) not in p.calls
    assert any(call[0] == 'play' for call in p.calls if isinstance(call, tuple))


def test_model_error_finishes_thinking_without_fake_roast_or_wave():
    p = pipeline(fail_review=True)
    with pytest.raises(RuntimeError, match='cloud unavailable'):
        perform_turn(**p.kwargs)
    assert p.calls == ['capture', 'thinking', 'review', 'finish']


@pytest.mark.parametrize('setting', ['stop_in_review', 'stop_in_thought', 'stop_in_playback', 'revoke'])
def test_stop_or_hot_disable_prevents_followup_gesture(setting):
    p = pipeline(**{setting: True})
    perform_turn(**p.kwargs)
    assert 'dismiss' not in p.calls
    assert not any(call == ('play', NEXT_TEXT) for call in p.calls)
    if setting in ('stop_in_review', 'stop_in_thought'):
        assert not any(call[0] == 'play' for call in p.calls if isinstance(call, tuple))
    assert p.stop.is_set()


def test_playback_interrupted_never_says_next():
    p = pipeline()
    p.kwargs['playback'] = lambda *args, **kwargs: {'status': 'stopped'}
    perform_turn(**p.kwargs)
    assert 'dismiss' not in p.calls


def test_snapshot_failure_never_starts_body_action():
    p = pipeline()
    def broken(camera): raise OSError('camera unavailable')
    p.kwargs['capture'] = broken
    with pytest.raises(OSError): perform_turn(**p.kwargs)
    assert 'thinking' not in p.calls and 'dismiss' not in p.calls
