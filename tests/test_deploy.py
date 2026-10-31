import json
import hashlib
from types import SimpleNamespace
from pathlib import Path

import pytest

from configure_robot import configure
from deploy.install_robot import (_service_text, bootstrap_pip, install, read_robot_config,
                                  write_robot_config)


def test_defaults_disable_hardware_and_keep_candidate_camera(tmp_path):
    value = write_robot_config(tmp_path)
    assert value['hardware_enabled'] is False
    assert value['trigger'] == '锐评一下'
    assert value['camera'] == '/dev/video2'
    assert value['mic_channel'] == 0
    assert Path(value['sdk_root']).is_absolute()
    assert value['sdk_root'] == str((tmp_path/'vendor_media').resolve())
    assert read_robot_config(tmp_path)['hardware_enabled'] is False


def test_existing_enabled_hardware_is_never_overwritten(tmp_path):
    (tmp_path / 'robot.json').write_text(json.dumps({'hardware_enabled': True}), encoding='utf-8')
    with pytest.raises(RuntimeError, match='硬件已启用'):
        write_robot_config(tmp_path)


def test_service_quotes_paths_with_spaces():
    text = _service_text(Path('/opt/Bufu Robot'), Path('/opt/Bufu Robot/.venv/bin/python'))
    assert 'WorkingDirectory="' in text and 'Bufu Robot' in text
    assert 'ExecStart="' in text and 'Bufu Robot' in text
    assert 'robot_service.py' in text
    assert '--camera' not in text
    assert 'Restart=no' in text and 'NoNewPrivileges=yes' in text


def test_check_only_does_not_write_or_install(tmp_path):
    result = install(tmp_path, check_only=True)
    assert '不写文件' in result['plan']
    assert not (tmp_path / 'robot.json').exists()


def test_existing_sdk_selection_is_preserved(tmp_path):
    original = write_robot_config(tmp_path)
    with pytest.raises(RuntimeError,match='不会覆盖'):
        write_robot_config(tmp_path,sdk_root=tmp_path/'different-sdk')
    assert read_robot_config(tmp_path)==original


def test_configure_uses_secret_prompts_and_preserves_empty_values(tmp_path):
    answers = iter(['', ''])
    secrets = iter(['visual-key', 'app-id', 'access-key'])
    result = configure(tmp_path, input_fn=lambda _: next(answers), secret_fn=lambda _: next(secrets))
    assert result['vision']['model'] == 'doubao-seed-2-1-pro-260915'
    assert result['vision']['key_configured']
    assert result['voice']['configured']


def test_online_bootstrap_is_hash_checked_without_real_network(tmp_path):
    wheel_bytes = b'fake-wheel'
    digest = hashlib.sha256(wheel_bytes).hexdigest()
    calls = []

    class Response:
        def __init__(self, value): self.value = value
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def read(self): return self.value

    metadata = json.dumps({'urls': [{'packagetype': 'bdist_wheel',
        'filename': 'pip-24.3.1-py3-none-any.whl',
        'url': 'https://files.pythonhosted.org/packages/pip.whl',
        'digests': {'sha256': digest}}]}).encode()
    def fetch(url, timeout):
        assert url.startswith('https://')
        return Response(metadata if 'pypi.org' in url else wheel_bytes)
    def runner(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=1)

    target = bootstrap_pip(tmp_path / 'bin/python', fetch=fetch, runner=runner, project=tmp_path)
    assert target.name == 'pip-24.3.1-py3-none-any.whl'
    assert '--no-index' in calls[-1][0]
    assert calls[-1][1]['env']['PYTHONPATH'] == str(target)
