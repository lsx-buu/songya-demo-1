from __future__ import annotations

import json
import base64
import hashlib
import re
import time
import uuid
from datetime import datetime, timezone

import httpx

from . import prompts

ARK_URL = 'https://ark.cn-beijing.volces.com/api/v3/chat/completions'
SYSTEM_PROMPT, USER_PROMPT = prompts.SYSTEM_PROMPT, prompts.USER_PROMPT


class VisionError(RuntimeError):
    pass


class ProviderAuthError(VisionError):
    pass


class ProviderContentError(VisionError):
    pass


def _parse_content(content: str) -> dict:
    content = content.strip()
    if content.startswith('```'):
        content = re.sub(r'^```(?:json)?\s*|\s*```$', '', content, flags=re.I | re.S).strip()
    try:
        value = json.loads(content)
    except ValueError as error:
        raise ProviderContentError('模型返回的点评格式无效，请重试') from error
    if not isinstance(value, dict) or value.get('status') not in ('ok', 'retake'):
        raise ProviderContentError('模型返回的点评格式无效，请重试')
    seen = value.get('seen', [])
    if not isinstance(seen, list) or any(not isinstance(item, str) or not item.strip() for item in seen):
        raise ProviderContentError('模型返回的可见单品格式无效，请重试')
    seen = [item.strip()[:40] for item in seen[:4]]
    if value['status'] == 'ok' and not 2 <= len(seen) <= 4:
        raise ProviderContentError('模型返回的可见单品不足，请重试')
    result = {'status': value['status'], 'seen': seen}
    for key, limit in (('title', 24), ('roast', 100), ('suggestion', 80), ('reason', 100)):
        raw = value.get(key, '')
        if not isinstance(raw, str):
            raise ProviderContentError('模型返回的点评格式无效，请重试')
        result[key] = raw.strip()[:limit]
    if value['status'] == 'ok' and (not result['title'] or not result['roast']):
        raise ProviderContentError('模型返回的点评内容为空，请重试')
    if value['status'] == 'retake' and not result['reason']:
        raise ProviderContentError('模型未说明重拍原因，请重试')
    return result


def review_image(image_data_url: str, model: str, api_key: str, client_factory=None) -> dict:
    if not model or not api_key:
        raise VisionError('请先在设置中填写方舟视觉模型和 API Key')
    headers = {'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json'}
    started_at = time.time()
    client_call_id = uuid.uuid4().hex
    try:
        encoded = image_data_url.split(',', 1)[1]
        image_sha256 = hashlib.sha256(base64.b64decode(encoded, validate=True)).hexdigest()
    except (AttributeError, IndexError, ValueError):
        image_sha256 = hashlib.sha256(str(image_data_url).encode()).hexdigest()
    payload = {'model': model, 'messages': [
        {'role': 'system', 'content': SYSTEM_PROMPT},
        {'role': 'user', 'content': [{'type': 'text', 'text': USER_PROMPT},
                                     {'type': 'image_url', 'image_url': {'url': image_data_url}}]},
    ], 'response_format': {'type': 'json_object'}, 'temperature': .85, 'max_tokens': 800}
    if model == 'doubao-seed-2-1-pro-260915':
        payload['thinking'] = {'type': 'disabled'}
    transport = 'injected_client' if client_factory else 'https'
    try:
        client = client_factory() if client_factory else httpx.Client(timeout=45)
        with client:
            response = client.post(ARK_URL, headers=headers, json=payload)
    except httpx.TimeoutException as error:
        raise VisionError('模型响应超时，请稍后重试') from error
    except httpx.HTTPError as error:
        raise VisionError('模型网络连接失败，请检查网络后重试') from error
    if response.status_code in (401, 403):
        raise ProviderAuthError('方舟鉴权失败，请检查视觉模型 API Key')
    if response.status_code >= 400:
        raise VisionError('视觉模型暂时无法服务，请稍后重试')
    try:
        body = response.json()
        content = body['choices'][0]['message']['content']
    except (ValueError, KeyError, IndexError, TypeError) as error:
        raise ProviderContentError('模型返回内容无法读取，请重试') from error
    if not isinstance(content, str):
        raise ProviderContentError('模型返回内容无法读取，请重试')
    result = _parse_content(content)
    request_id = response.headers.get('x-request-id') or response.headers.get('x-tt-logid')
    provenance = {
        'source': 'volcengine_ark', 'endpoint': ARK_URL, 'configured_model': model,
        'response_model': body.get('model') if isinstance(body, dict) else None,
        'response_id': body.get('id') if isinstance(body, dict) else None,
        'client_call_id': client_call_id,
        'prompt_version': getattr(prompts, 'PROMPT_VERSION', 'roast-v2'),
        'image_sha256': image_sha256, 'started_at': datetime.fromtimestamp(started_at, timezone.utc).isoformat().replace('+00:00', 'Z'),
        'elapsed_ms': round((time.time() - started_at) * 1000), 'transport': transport,
    }
    if request_id:
        provenance['request_id'] = request_id[:512]
    result['provenance'] = provenance
    return result
