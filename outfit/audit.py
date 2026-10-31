"""Small, private, allow-listed provenance log for provider calls."""
from __future__ import annotations

import json
import os
from pathlib import Path
import threading
from datetime import datetime, timezone

_LOCK = threading.Lock()
_SCALARS = {'source', 'endpoint', 'configured_model', 'response_model', 'response_id',
            'client_call_id', 'prompt_version', 'image_sha256', 'started_at', 'elapsed_ms',
            'transport', 'voice_model', 'speaker', 'mode', 'event', 'client_session_id',
            'request_id', 'text_sha256', 'wav_sha256', 'duration_seconds', 'run_id',
            'status', 'frames_sent', 'audibility_verified'}
_USAGE_KEYS = {'prompt_tokens', 'completion_tokens', 'total_tokens', 'input_tokens', 'output_tokens'}
_KINDS = {'vision', 'speech', 'playback'}


def _now():
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


def _clean(value):
    if isinstance(value, bool) or isinstance(value, (int, float)) or value is None:
        return value
    if isinstance(value, str):
        return value[:512]
    return None


def append_event(root, kind, provenance):
    if kind not in _KINDS or not isinstance(provenance, dict):
        raise ValueError('审计事件格式无效')
    record = {'kind': kind, 'recorded_at': _now()}
    for key in _SCALARS:
        if key in provenance:
            value = _clean(provenance[key])
            if value is not None:
                record[key] = value
    usage = provenance.get('usage')
    if isinstance(usage, dict):
        clean_usage = {}
        for key in _USAGE_KEYS:
            value = usage.get(key)
            if isinstance(value, int) and 0 <= value <= 10_000_000:
                clean_usage[key] = value
        if clean_usage:
            record['usage'] = clean_usage
    path = Path(root).resolve() / 'data' / 'api-audit.jsonl'
    path.parent.mkdir(parents=True, exist_ok=True)
    line = (json.dumps(record, ensure_ascii=False, separators=(',', ':')) + '\n').encode('utf-8')
    with _LOCK:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        try:
            os.chmod(path, 0o600)
            if os.write(fd, line) != len(line):
                raise OSError('调用记录未完整写入')
        finally:
            os.close(fd)
    return record
