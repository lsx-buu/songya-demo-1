"""Text-to-WAV via the user's Volcengine voice account. No device or robot code."""
from __future__ import annotations

import io
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time
import uuid
import wave

from .cloud_protocol import decode_frame, encode_event

URL = 'wss://openspeech.bytedance.com/api/v3/realtime/dialogue'
DEFAULT_SPEAKER = 'zh_female_vv_jupiter_bigtts'
DEFAULTS = {'app_id': '', 'access_key': '', 'model': '1.2.1.1', 'speaker': DEFAULT_SPEAKER}


class VoiceError(RuntimeError):
    pass


def _validate(value, require_keys=False):
    for name in DEFAULTS:
        if not isinstance(value.get(name), str) or len(value[name]) > 4096:
            raise VoiceError('声音配置格式无效，请重新保存')
        if any(ord(char) < 32 for char in value[name]):
            raise VoiceError('声音配置不能包含换行或控制字符')
    if not value['app_id'].isascii() or not value['access_key'].isascii():
        raise VoiceError('语音 App ID 和 Access Key 需使用英文字符')
    if value['model'] not in ('1.2.1.1', '2.2.0.0') or not value['speaker'].strip():
        raise VoiceError('请检查语音模型版本与音色')
    if require_keys and not (value['app_id'] and value['access_key']):
        raise VoiceError('请先配置语音 App ID 和 Access Key')


class VoiceConfig:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.path = self.root / '.voice.json'

    def read(self):
        value = dict(DEFAULTS)
        if self.path.exists():
            try:
                loaded = json.loads(self.path.read_text(encoding='utf-8'))
                if not isinstance(loaded, dict):
                    raise ValueError()
                value.update({key: loaded[key] for key in DEFAULTS if key in loaded})
            except (OSError, ValueError):
                raise VoiceError('本地声音配置无法读取，请重新保存') from None
        _validate(value)
        return value

    def public(self):
        value = self.read()
        return {'configured': bool(value['app_id'] and value['access_key']),
                'app_id_configured': bool(value['app_id']), 'access_key_configured': bool(value['access_key']),
                'model': value['model'], 'speaker': value['speaker']}

    def save(self, app_id='', access_key='', model='1.2.1.1', speaker=DEFAULT_SPEAKER):
        old = self.read()
        for candidate in (app_id, access_key, model, speaker):
            if not isinstance(candidate, str) or any(ord(char) < 32 for char in candidate):
                raise VoiceError('声音配置不能包含换行或控制字符')
        value = {'app_id': app_id.strip() or old['app_id'], 'access_key': access_key.strip() or old['access_key'],
                 'model': model.strip(), 'speaker': speaker.strip()}
        _validate(value)
        self.root.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix='.voice.', dir=self.root)
        try:
            os.chmod(temporary, 0o600)
            with os.fdopen(fd, 'w', encoding='utf-8') as target:
                json.dump(value, target, ensure_ascii=False)
                target.flush()
                os.fsync(target.fileno())
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return self.public()


def synthesize(text, config, *, connector=None, on_provenance=None):
    """Submit explicit text through SayHello, not a conversation/model query.

    The provider documents event 300 as client-supplied speech text. It works
    without microphone/ASR context; standalone ChatTTSText requires that context.
    Returned audio is collected only between TTS start/end, bounded to 20s.
    """
    if not isinstance(text, str) or not 1 <= len(text.strip()) <= 200:
        raise VoiceError('锐评需为1—200个字符，请缩短台词')
    _validate(config, require_keys=True)
    started_at = time.monotonic()
    text_value = text.strip()
    transport = 'injected_client' if connector is not None else 'wss'
    if connector is None:
        from websockets.sync.client import connect
        connector = connect
    session = uuid.uuid4().hex
    headers = {'X-Api-App-ID': config['app_id'], 'X-Api-Access-Key': config['access_key'],
               'X-Api-Resource-Id': 'volc.speech.dialog',
               'X-Api-App-Key': 'PlgvMymc7f3tQnJ6',  # Vendor's public protocol constant.
               'X-Api-Connect-Id': uuid.uuid4().hex}
    dialog = {'extra': {'model': config['model'], 'input_mod': 'text'}}
    policy = '你只朗读客户提供的文本，不改写，不补充，不回答。'
    if config['model'] == '2.2.0.0':
        dialog['character_manifest'] = policy
    else:
        dialog.update(bot_name='布米', system_role=policy, speaking_style='自然、冷面幽默，短句之间稍作停顿。')
    payload = {'asr': {'extra': {}}, 'tts': {'speaker': config['speaker'],
               'audio_config': {'channel': 1, 'format': 'pcm_s16le', 'sample_rate': 24000}, 'extra': {}},
               'dialog': dialog}
    ws = None
    session_started = False
    deadline = time.monotonic() + 30

    def send(event, value):
        ws.send(encode_event(event, value, session if event >= 100 else ''))

    def receive(until):
        while time.monotonic() < until:
            try:
                frame = decode_frame(ws.recv(timeout=min(.25, max(.01, until - time.monotonic()))))
            except TimeoutError:
                continue
            if frame.message_type not in (9, 11, 15) or (frame.session_id and frame.session_id != session):
                raise VoiceError('语音服务返回了无效会话数据')
            if frame.error_code is not None or frame.event in (51, 153, 599):
                raise VoiceError('语音服务拒绝请求，请检查账号权限、模型版本和音色')
            return frame
        raise VoiceError('语音合成超时，请稍后重试')

    def wait_for(event, until):
        while True:
            frame = receive(until)
            if frame.event == event:
                return

    try:
        ws = connector(URL, additional_headers=headers, open_timeout=10, close_timeout=1,
                       max_size=4 * 1024 * 1024 + 1024, proxy=None)
        send(1, {})
        wait_for(50, min(deadline, time.monotonic() + 8))
        send(100, payload)
        wait_for(150, min(deadline, time.monotonic() + 8))
        session_started = True
        send(300, {'content': text_value})
        pcm, speaking = bytearray(), False
        while True:
            frame = receive(deadline)
            if frame.event == 350:
                if not isinstance(frame.payload, dict) or frame.payload.get('tts_type') == 'audit_content_risky':
                    raise VoiceError('这段台词未能生成语音，请换个说法')
                speaking = True
            elif frame.event == 352:
                if not speaking or not isinstance(frame.payload, bytes) or len(frame.payload) % 2:
                    raise VoiceError('语音数据不完整，请重试')
                pcm.extend(frame.payload)
                if len(pcm) > 24000 * 2 * 20:
                    raise VoiceError('台词语音超过20秒，请缩短内容')
            elif frame.event == 359:
                if not speaking or not pcm:
                    raise VoiceError('语音服务没有返回有效声音')
                output = io.BytesIO()
                with wave.open(output, 'wb') as audio:
                    audio.setnchannels(1)
                    audio.setsampwidth(2)
                    audio.setframerate(24000)
                    audio.writeframes(pcm)
                wav = output.getvalue()
                if on_provenance is not None:
                    response = getattr(ws, 'response', None)
                    response_headers = getattr(response, 'headers', {}) or {}
                    request_id = response_headers.get('x-request-id') or response_headers.get('x-tt-logid')
                    on_provenance({
                        'source': 'volcengine_realtime', 'endpoint': URL,
                        'voice_model': config['model'], 'speaker': config['speaker'],
                        'mode': 'direct_text', 'event': 300, 'client_session_id': session,
                        'text_sha256': hashlib.sha256(text_value.encode()).hexdigest(),
                        'wav_sha256': hashlib.sha256(wav).hexdigest(),
                        'elapsed_ms': round((time.monotonic() - started_at) * 1000),
                        'duration_seconds': round(len(pcm) / (24000 * 2), 6),
                        'transport': transport,
                        **({'request_id': request_id[:512]} if request_id else {}),
                    })
                return wav
    except VoiceError:
        raise
    except Exception as error:
        status = getattr(getattr(error, 'response', None), 'status_code', None)
        if status in (401, 403):
            raise VoiceError('语音鉴权失败，请检查 App ID、Access Key 和权限') from None
        raise VoiceError('语音合成连接或数据异常，请检查网络和服务配置') from None
    finally:
        if ws is not None:
            try:
                if session_started:
                    send(102, {})
                    wait_for(152, time.monotonic() + 1)
            except Exception:
                pass
            try:
                send(2, {})
            except Exception:
                pass
            try:
                ws.close()
            except Exception:
                pass
