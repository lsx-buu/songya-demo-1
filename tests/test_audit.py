import json
import os

from outfit.audit import append_event


def test_audit_is_private_allow_listed_and_secret_free(tmp_path):
    record = append_event(tmp_path, 'vision', {
        'source': 'volcengine_ark', 'image_sha256': 'abc', 'api_key': 'secret',
        'authorization': 'Bearer secret', 'unknown': 'drop',
        'usage': {'total_tokens': 12, 'api_key': 'drop'},
    })
    assert record['kind'] == 'vision'
    raw = (tmp_path / 'data' / 'api-audit.jsonl').read_text(encoding='utf-8')
    assert 'secret' not in raw and 'unknown' not in raw
    loaded = json.loads(raw)
    assert loaded['usage'] == {'total_tokens': 12}
    if os.name != 'nt':
        assert (tmp_path / 'data' / 'api-audit.jsonl').stat().st_mode & 0o777 == 0o600
