"""Cloud-only keyword listener; it never opens devices or plays audio."""
from __future__ import annotations

import re
import threading
import time
import uuid

from .cloud_protocol import decode_frame, encode_event
from .speech import URL, VoiceError, _validate


_PUNCTUATION = re.compile(r'[\s\u3000，。！？、；：,.!?;:"“”‘’（）()【】\[\]「」<>《》…—\-_]+')


def _compact(value: str) -> str:
    return _PUNCTUATION.sub('', value).casefold()


def _result_text(payload) -> str:
    if not isinstance(payload, dict):
        return ''
    results = payload.get('results')
    if isinstance(results, list):
        parts = []
        for item in results:
            if isinstance(item, dict) and isinstance(item.get('text'), str):
                parts.append(item['text'])
        return ''.join(parts)
    return ''


def wait_for_trigger(config, read_pcm, stop, *, connector=None, max_seconds=55, keyword='锐评一下') -> bool:
    if not isinstance(config, dict):
        raise VoiceError('语音配置格式无效')
    _validate(config, require_keys=True)
    if not callable(read_pcm) or not hasattr(stop, 'is_set'):
        raise VoiceError('语音触发入口参数无效')
    target = _compact(keyword)
    if not target or len(target) > 80:
        raise VoiceError('触发词无效')
    if stop.is_set():
        return False
    if not isinstance(max_seconds, (int, float)) or not 1 <= max_seconds <= 55:
        raise VoiceError('触发监听时长需为1—55秒')
    if connector is None:
        from websockets.sync.client import connect
        connector = connect
    session = uuid.uuid4().hex
    headers = {'X-Api-App-ID': config['app_id'], 'X-Api-Access-Key': config['access_key'],
               'X-Api-Resource-Id': 'volc.speech.dialog', 'X-Api-App-Key': 'PlgvMymc7f3tQnJ6',
               'X-Api-Connect-Id': uuid.uuid4().hex}
    dialog = {'extra': {'model': config['model'], 'input_mod': 'keep_alive'}}
    if config['model'] == '2.2.0.0':
        dialog['character_manifest'] = '只识别用户是否说出触发词，不生成或播放回答。'
    else:
        dialog.update(bot_name='布米', system_role='只识别用户是否说出触发词，不生成或播放回答。')
    # Provider-supported hotwords/correction: observed ASR wrote this spoken
    # command as the homophone "瑞平一下". Matching below remains exact.
    asr_extra = {'enable_asr_twopass': True,
                 'context': {'hotwords': [{'word': keyword}],
                             'correct_words': {'瑞平一下': '锐评一下'} if keyword == '锐评一下' else {}}}
    payload = {'asr': {'extra': asr_extra}, 'tts': {'speaker': config['speaker'],
               'audio_config': {'channel': 1, 'format': 'pcm_s16le', 'sample_rate': 24000}, 'extra': {}},
               'dialog': dialog}
    internal_stop = threading.Event()
    sender_error = []
    ws = None
    sender = None
    session_started = False
    deadline = time.monotonic() + float(max_seconds)

    def send(event, value):
        ws.send(encode_event(event, value, session if event >= 100 else ''))

    def receive(until, *, ignore_stop=False):
        while (time.monotonic() < until
               and (ignore_stop or not internal_stop.is_set())
               and (ignore_stop or not stop.is_set())):
            try:
                frame = decode_frame(ws.recv(timeout=min(.25, max(.01, until - time.monotonic()))))
            except TimeoutError:
                continue
            if frame.message_type not in (9, 11, 15) or (frame.session_id and frame.session_id != session):
                raise VoiceError('语音服务返回了无效会话数据')
            if frame.error_code is not None or frame.event in (51, 153, 599):
                raise VoiceError('语音触发服务拒绝请求，请检查账号权限和模型配置')
            return frame
        return None

    def upload():
        last_audio = time.monotonic()
        next_deadline = time.monotonic()
        try:
            while not internal_stop.is_set() and not stop.is_set() and time.monotonic() < deadline:
                chunk = read_pcm(.1)
                now = time.monotonic()
                if chunk is None:
                    if now - last_audio > 5:
                        raise VoiceError('5秒内未收到麦克风数据，请检查音频设备')
                    continue
                if not isinstance(chunk, bytes) or len(chunk) != 640:
                    raise VoiceError('麦克风必须提供20ms的16k mono int16 PCM')
                last_audio = now
                send(200, chunk)
                next_deadline += .020
                remaining = next_deadline - time.monotonic()
                if remaining > 0:
                    internal_stop.wait(remaining)
                elif time.monotonic() - next_deadline > .12:
                    next_deadline = time.monotonic() + .020
        except Exception as error:
            sender_error.append(error)
            internal_stop.set()

    try:
        ws = connector(URL, additional_headers=headers, open_timeout=10, close_timeout=1,
                       max_size=4 * 1024 * 1024 + 1024, proxy=None)
        send(1, {})
        frame = receive(min(deadline, time.monotonic() + 8))
        if frame is None or frame.event != 50:
            raise VoiceError('语音触发连接未建立')
        send(100, payload)
        frame = receive(min(deadline, time.monotonic() + 8))
        if frame is None or frame.event != 150:
            raise VoiceError('语音触发会话未建立')
        session_started = True
        sender = threading.Thread(target=upload, name='outfit-trigger-upload', daemon=True)
        sender.start()
        cached_text = ''
        while time.monotonic() < deadline and not stop.is_set() and not internal_stop.is_set():
            if sender_error:
                raise sender_error[0]
            frame = receive(deadline)
            if frame is None:
                break
            if frame.event == 450:
                cached_text = ''
                continue
            if frame.event == 451:
                cached_text = _result_text(frame.payload)
                continue
            if frame.event == 459:
                final_text = _result_text(frame.payload) or cached_text
                return _compact(final_text) == target
        if sender_error:
            raise sender_error[0]
        return False
    except VoiceError:
        raise
    except Exception:
        raise VoiceError('语音触发连接或数据异常，请检查网络和服务配置') from None
    finally:
        internal_stop.set()
        if sender is not None:
            sender.join(timeout=2)
            if sender.is_alive():
                if ws is not None:
                    try:
                        ws.close()
                    except Exception:
                        pass
                raise VoiceError('音频上传线程未能及时停止，已关闭语音连接')
        if ws is not None:
            try:
                if session_started:
                    send(102, {})
                    # The upload worker is already stopped; briefly allow the
                    # receiver to consume the provider's 152 acknowledgement.
                    receive(time.monotonic() + 1, ignore_stop=True)
            except Exception:
                pass
            finally:
                internal_stop.set()
            try:
                send(2, {})
            except Exception:
                pass
            try:
                ws.close()
            except Exception:
                pass
