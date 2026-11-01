import json

import httpx

from outfit.prompts import PROMPT_VERSION
from outfit.vision import _parse_content, review_image
from outfit.vision import ProviderContentError
import pytest


class Client:
    def __init__(self, payload):
        self.payload = payload
        self.request = None
    def __enter__(self): return self
    def __exit__(self, *args): return False
    def post(self, url, **kwargs):
        self.request = kwargs
        return httpx.Response(200, json=self.payload, request=httpx.Request('POST', url))


def wrapped(value):
    return {'choices': [{'message': {'content': json.dumps(value, ensure_ascii=False)}}]}


def test_v3_prompt_is_sent_and_suggestion_is_not_an_advice_channel():
    client = Client(wrapped({'status': 'ok', 'seen': ['黑西装', '白球鞋'], 'title': '上下班错峰',
        'roast': '黑西装在谈收购，白球鞋在等下课。你这套不是商务休闲，是公司还没上市，员工先放学了。',
        'suggestion': '配同色袜子', 'reason': ''}))
    result = review_image('data:image/jpeg;base64,YQ==', 'ep-test', 'key', lambda: client)
    assert PROMPT_VERSION == 'roast-v3'
    assert '只输出最狠且最贴图的一条' in client.request['json']['messages'][0]['content']
    assert result['suggestion'] == ''


def test_retake_always_clears_roast_and_suggestion():
    result = _parse_content(json.dumps({'status': 'retake', 'seen': [], 'title': '',
        'roast': '不该保留', 'suggestion': '建议重拍', 'reason': '画面里有多人'}))
    assert result['roast'] == ''
    assert result['suggestion'] == ''


def test_style_upgrade_still_rejects_broken_provider_content_without_canned_roast():
    client = Client({'choices': [{'message': {'content': '模型没有返回JSON'}}]})
    with pytest.raises(ProviderContentError):
        review_image('data:image/jpeg;base64,YQ==', 'ep-test', 'key', lambda: client)
