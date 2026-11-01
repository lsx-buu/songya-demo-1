"""One turn of the show. Image models never choose or parameterize body actions."""
from __future__ import annotations

import threading

NEXT_TEXT = '下一个。'
RETAKE_TEXT = '这一张没看清穿搭，站到镜头前再说一次锐评一下吧。'


class GestureOutcomeUnknown(RuntimeError):
    pass


def perform_turn(config, stop, root, run_id, *, vision, voice, gestures,
                 capture, normalize, review, synthesize, playback, record,
                 report, permitted):
    """Dependencies are explicit so choreography can be checked without devices."""
    monitor_done = threading.Event()

    def allowed():
        try:
            ready = permitted() is True
        except Exception:
            ready = False
        if not ready:
            stop.set()
        return not stop.is_set()

    def monitor():
        while not monitor_done.wait(.1):
            if not allowed():
                return

    watcher = threading.Thread(target=monitor, daemon=True)
    watcher.start()

    def event(kind, details, phase):
        return record(root, kind, dict(details, run_id=run_id, phase=phase))

    def finish_gesture():
        outcome = gestures.finish()
        if outcome is not None:
            event('gesture', outcome, 'completion')
            if outcome.get('status') == 'outcome_unknown':
                stop.set()
                raise GestureOutcomeUnknown('动作执行结果未确认，请由现场操作员确认设备状态')

    try:
        if not allowed():
            return
        report(root, 'capturing', run_id=run_id)
        image = normalize(capture(config['camera']))
        if not allowed():
            return
        # Capture before moving an arm: do not blur or block the snapshot.
        try:
            event('gesture', gestures.start('thinking', run_id), 'thinking')
            if not allowed():
                return
            report(root, 'reviewing', run_id=run_id)
            result = review(image, vision['model'], vision['api_key'])
            trace = result['provenance']
            event('vision', trace, 'review')
            if not allowed():
                return
            text = result['roast'] if result['status'] == 'ok' else RETAKE_TEXT
            if config.get('source_announcement', True):
                text = '布米嘴替开评。' + text
            report(root, 'synthesizing', run_id=run_id,
                   prompt_version=trace.get('prompt_version'),
                   model=trace.get('response_model') or vision['model'],
                   provider_response_id=trace.get('response_id'),
                   provider_request_id=trace.get('request_id'))
            audio = synthesize(text, voice,
                on_provenance=lambda details: event('speech', details, 'review'))
            if not allowed():
                return
            # Prepare the short closing line while the thinking stage is active.
            # A retake never dismisses the participant or waves them away.
            next_audio = synthesize(NEXT_TEXT, voice,
                on_provenance=lambda details: event('speech', details, 'dismiss')) \
                if result['status'] == 'ok' else None
        finally:
            finish_gesture()
        if not allowed():
            return
        report(root, 'speaking', run_id=run_id,
               provider_response_id=trace.get('response_id'))
        outcome = playback(audio, stop=stop)
        event('playback', outcome, 'review')
        if outcome.get('status') != 'sent' or not allowed():
            return
        if next_audio is not None:
            # Last PCM frame has been paced; leave a short buffer before dismissal.
            if stop.wait(.25) or not allowed():
                return
            report(root, 'dismissing', run_id=run_id)
            try:
                event('gesture', gestures.start('dismiss', run_id), 'dismiss')
                if allowed():
                    event('playback', playback(next_audio, stop=stop), 'dismiss')
            finally:
                finish_gesture()
        if allowed():
            report(root, 'turn_finished', run_id=run_id,
                   prompt_version=trace.get('prompt_version'))
        return result
    finally:
        monitor_done.set()
        watcher.join(timeout=1)
