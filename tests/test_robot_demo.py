import base64
import io
from types import SimpleNamespace

from PIL import Image

import robot_demo
from test_robot_audio import wav


def test_default_pipeline_saves_wav_without_loading_hardware(tmp_path, monkeypatch, capsys):
    photo = tmp_path / 'outfit.jpg'
    monkeypatch.setattr(robot_demo, 'ROOT', tmp_path)
    Image.new('RGB', (160, 200), 'black').save(photo)
    settings = SimpleNamespace(read=lambda: {'model': 'ep-fake', 'api_key': 'fake'})
    voice = SimpleNamespace(read=lambda: {'app_id': 'fake', 'access_key': 'fake'})
    monkeypatch.setattr(robot_demo, 'Settings', lambda root: settings)
    monkeypatch.setattr(robot_demo, 'VoiceConfig', lambda root: voice)
    monkeypatch.setattr(robot_demo, 'review_image', lambda *args: {'status': 'ok', 'roast': '这套全黑，像把生活切成了深色模式。',
                        'provenance': {'source':'volcengine_ark','transport':'injected_client','response_id':'fake-test-response'}})
    expected = wav([10] * 320)
    def synthesize(text, config, *, on_provenance):
        assert text.startswith('布米嘴替开评。')
        on_provenance({'source':'volcengine_realtime','transport':'injected_client','event':300})
        return expected
    monkeypatch.setattr(robot_demo, 'synthesize', synthesize)
    def forbidden(*args, **kwargs):raise AssertionError('No hardware for default run')
    monkeypatch.setattr('outfit.robot_audio._load_media', forbidden)
    output = tmp_path / 'roast.wav'
    assert robot_demo.main(['--image', str(photo), '--output', str(output)]) == 0
    assert output.read_bytes() == expected
    assert '"robot_audio_sent": false' in capsys.readouterr().out
    records = [__import__('json').loads(line) for line in (tmp_path/'data/api-audit.jsonl').read_text().splitlines()]
    assert [item['kind'] for item in records] == ['vision','speech']
    assert records[0]['run_id'] == records[1]['run_id']
