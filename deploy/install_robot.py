"""Install the Bufu Robot package without touching the vendor runtime or hardware."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import urllib.request
from urllib.parse import urlparse

PROJECT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = {'hardware_enabled': False, 'trigger': '锐评一下',
                  'camera': '/dev/video2', 'camera_note': '候选节点，尚未验证',
                  'sdk_root': str((PROJECT / 'vendor_media').resolve()), 'mic_channel': 0,
                  'gestures_enabled': False, 'wave_verified': False,
                  'thinking_verified': False, 'thinking_teach_index': None,
                  'gesture_sdk_root': '',
                  'version': 'Bufu-Robot-0.2.0'}


def read_robot_config(project=PROJECT):
    path = Path(project) / 'robot.json'
    if not path.exists():
        return dict(DEFAULT_CONFIG, sdk_root=str((Path(project)/'vendor_media').resolve()))
    value = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(value, dict):
        raise ValueError('robot.json格式无效')
    return value


def write_robot_config(project=PROJECT, *, sdk_root=None):
    path = Path(project) / 'robot.json'
    existed = path.exists()
    value = read_robot_config(project)
    if type(value.get('hardware_enabled', False)) is not bool:
        raise RuntimeError('robot.json的hardware_enabled必须是布尔值')
    if value.get('hardware_enabled') is True:
        raise RuntimeError('检测到硬件已启用，安装器不会自动修改或启动它')
    if type(value.get('gestures_enabled', False)) is not bool or value.get('gestures_enabled', False):
        raise RuntimeError('安装器要求动作开关为false，不修改已启用的动作配置')
    merged = dict(DEFAULT_CONFIG, sdk_root=str((Path(project)/'vendor_media').resolve()))
    merged.update(value)
    if sdk_root:
        requested = str(Path(sdk_root).expanduser().resolve())
        if existed and value.get('sdk_root') and value['sdk_root'] != requested:
            raise RuntimeError('已有SDK配置不同，请先明确修改robot.json；安装器不会覆盖')
        merged['sdk_root'] = requested
    if not Path(merged['sdk_root']).is_absolute():
        raise RuntimeError('sdk_root必须是明确的绝对路径')
    merged['mic_channel'] = merged.get('mic_channel', 0)
    if type(merged['mic_channel']) is not int or merged['mic_channel'] not in range(4):
        raise RuntimeError('mic_channel必须是0到3的整数')
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name('.robot.json.tmp')
    temporary.write_text(json.dumps(merged, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)
    return merged


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def _safe_wheel_url(url):
    parsed = urlparse(url)
    return parsed.scheme == 'https' and parsed.netloc == 'files.pythonhosted.org'


def _service_text(project, python, sdk_root=None):
    project, python = Path(project).resolve(), Path(python).absolute()
    def quote(value):
        return '"' + str(value).replace('\\', '\\\\').replace('"', '\\"') + '"'
    sdk = Path(sdk_root or (project / 'vendor_media')).resolve()
    libs = str(sdk / 'lib/aarch64') + ':' + str(sdk / 'dds/aarch64/lib')
    return '[Unit]\nDescription=Bufu Robot (disabled hardware by default)\nAfter=network-online.target\n\n' \
        '[Service]\nType=simple\nWorkingDirectory=' + quote(project) + '\nExecStart=' + quote(python) + ' ' + quote(project / 'robot_service.py') + '\nRestart=no\nUMask=0077\nNoNewPrivileges=yes\nEnvironment=LD_LIBRARY_PATH=' + quote(libs) + '\n\n[Install]\nWantedBy=default.target\n'


def _pip_has_module(python, runner=subprocess.run):
    result = runner([str(python), '-c', 'import pip'], capture_output=True, check=False)
    return result.returncode == 0


def bootstrap_pip(python, *, wheel=None, sha256=None, fetch=urllib.request.urlopen,
                  runner=subprocess.run, project=PROJECT):
    """Install only the pinned pip wheel; network access is injectable for tests."""
    if _pip_has_module(python, runner):
        return None
    bootstrap = Path(project) / '.bootstrap'
    bootstrap.mkdir(parents=True, exist_ok=True)
    if wheel:
        source = Path(wheel)
        expected = sha256
    else:
        with fetch('https://pypi.org/pypi/pip/24.3.1/json', timeout=15) as response:
            metadata = json.loads(response.read().decode('utf-8'))
        candidates = [item for item in metadata.get('urls', [])
                      if item.get('packagetype') == 'bdist_wheel' and item.get('filename') == 'pip-24.3.1-py3-none-any.whl']
        if not candidates or not _safe_wheel_url(candidates[0].get('url', '')):
            raise RuntimeError('pip wheel来源不受信任')
        expected = candidates[0].get('digests', {}).get('sha256')
        if not expected:
            raise RuntimeError('pip wheel缺少SHA256')
        source = bootstrap / candidates[0]['filename']
        with fetch(candidates[0]['url'], timeout=30) as response:
            source.write_bytes(response.read())
    if not expected or _sha256(source).lower() != expected.lower():
        raise RuntimeError('pip wheel的SHA256校验失败')
    target = bootstrap / source.name
    if source.resolve() != target.resolve():
        target.write_bytes(source.read_bytes())
    runner([str(python), '-m', 'pip', 'install', '--no-index', str(target)], check=True,
           env={**os.environ, 'PYTHONPATH': str(target)})
    return target


def install(project=PROJECT, *, sdk_root=None, check_only=False, install_service=False,
            pip_wheel=None, pip_sha256=None):
    project = Path(project).resolve()
    if check_only:
        return {'project': str(project), 'plan': '检查项目、依赖和配置；不写文件、不联网、不连接设备'}
    if sys.platform != 'linux':
        raise RuntimeError('机器人安装器仅支持 Linux；可使用 --check-only 查看计划')
    if sys.version_info < (3, 10):
        raise RuntimeError('需要Python 3.10或更新版本，并与媒体扩展版本匹配')
    config = write_robot_config(project, sdk_root=sdk_root)
    venv = project / '.venv'
    python = venv / 'bin' / 'python'
    if not python.exists():
        subprocess.run([sys.executable, '-m', 'venv', '--without-pip', str(venv)], check=True)
    if not _pip_has_module(python):
        bootstrap_pip(python, wheel=pip_wheel, sha256=pip_sha256, project=project)
    subprocess.run([str(python), '-m', 'pip', 'install', '--no-input', '--disable-pip-version-check',
                    '--timeout', '20', '--retries', '1', '--only-binary=:all:',
                    '-r', str(project / 'requirements.txt')], check=True)
    service = None
    if install_service:
        service = Path.home() / '.config' / 'systemd' / 'user' / 'bufu-robot.service'
        service.parent.mkdir(parents=True, exist_ok=True)
        service.write_text(_service_text(project, python, config['sdk_root']), encoding='utf-8')
    return {'config': config, 'venv': str(venv), 'service': str(service) if service else None}


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--sdk-root')
    parser.add_argument('--check-only', action='store_true')
    parser.add_argument('--install-service', action='store_true')
    parser.add_argument('--pip-wheel')
    parser.add_argument('--pip-sha256')
    args = parser.parse_args(argv)
    print(json.dumps(install(sdk_root=args.sdk_root, check_only=args.check_only,
                             install_service=args.install_service, pip_wheel=args.pip_wheel,
                             pip_sha256=args.pip_sha256), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
