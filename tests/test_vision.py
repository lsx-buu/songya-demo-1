import json
import base64

import httpx
import pytest

from outfit.vision import ProviderAuthError, ProviderContentError, review_image


class FakeClient:
    def __init__(self, response):
        self.response = response
        self.request = None

    def __enter__(self): return self
    def __exit__(self, *args): return False

    def post(self, url, **kwargs):
        self.request = (url, kwargs)
        return self.response


def response(payload, status=200):
    request = httpx.Request('POST', 'https://ark.cn-beijing.volces.com/api/v3/chat/completions')
    return httpx.Response(status, json=payload, request=request)


def test_review_sends_actual_image_and_no_robot_tools():
    image = base64.b64encode(b'actual-image').decode()
    client = FakeClient(response({'id': 'resp-1', 'model': 'ep-test-response', 'choices': [{'message': {'content': json.dumps({'status': 'ok', 'seen': ['西装', '运动鞋'], 'title': '通勤出逃', 'roast': '上半身在融资，下半身在撤资。', 'suggestion': '加同色袜子。', 'reason': ''})}}]}))
    client.response.headers['x-request-id'] = 'request-from-test-server'
    result = review_image('data:image/jpeg;base64,' + image, 'ep-test', 'secret', lambda: client)
    assert result['status'] == 'ok'
    url, kwargs = client.request
    assert url.endswith('/chat/completions')
    body = kwargs['json']
    assert body['model'] == 'ep-test' and body['messages'][1]['content'][1]['image_url']['url'].endswith(image)
    assert 'robot' not in json.dumps(body).lower() and 'action' not in json.dumps(body).lower()
    assert result['provenance']['source'] == 'volcengine_ark'
    assert result['provenance']['response_model'] == 'ep-test-response'
    assert result['provenance']['response_id'] == 'resp-1'
    assert result['provenance']['request_id'] == 'request-from-test-server'
    assert result['provenance']['image_sha256'] == __import__('hashlib').sha256(b'actual-image').hexdigest()
    assert result['provenance']['transport'] == 'injected_client'


def test_seed_pro_model_disables_thinking():
    client = FakeClient(response({'choices': [{'message': {'content': json.dumps({'status': 'retake', 'seen': [], 'title': '', 'roast': '', 'suggestion': '', 'reason': '看不清'})}}]}))
    review_image('data:image/jpeg;base64,YQ==', 'doubao-seed-2-1-pro-260915', 'secret', lambda: client)
    assert client.request[1]['json']['thinking'] == {'type': 'disabled'}


def test_auth_error_is_safe():
    with pytest.raises(ProviderAuthError, match='API Key'):
        review_image('data:image/jpeg;base64,abc', 'ep-test', 'secret', lambda: FakeClient(response({}, 401)))


def test_invalid_provider_json_fails_safe():
    with pytest.raises(ProviderContentError):
        review_image('data:image/jpeg;base64,abc', 'ep-test', 'secret', lambda: FakeClient(response({'choices': [{'message': {'content': 'nope'}}]})))
