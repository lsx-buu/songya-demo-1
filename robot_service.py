"""On-robot voice-triggered service. Disabled standby never loads any device SDK."""
import argparse
import json
import os
from pathlib import Path
import signal
import sys
import threading
import time
import uuid

ROOT = Path(__file__).resolve().parent
DEFAULTS = {'hardware_enabled': False, 'trigger': '锐评一下', 'camera': '/dev/video2',
            'sdk_root': str(ROOT / 'vendor_media'), 'mic_channel': 0,
            'source_announcement': True, 'gestures_enabled': False,
            'wave_verified': False, 'thinking_verified': False,
            'thinking_teach_index': None, 'gesture_sdk_root': ''}


def load_config(root=ROOT):
    values = dict(DEFAULTS)
    path = root / 'robot.json'
    if path.exists():
        stored = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(stored, dict): raise ValueError('robot.json格式无效')
        values.update({key: stored[key] for key in DEFAULTS if key in stored})
    if type(values['hardware_enabled']) is not bool or values['trigger'] != '锐评一下':
        raise ValueError('机器人启用状态或口令无效')
    if type(values['mic_channel']) is not int or values['mic_channel'] not in range(4):
        raise ValueError('麦克风仅允许头部通道0—3')
    if type(values['source_announcement']) is not bool:
        raise ValueError('声音来源提示开关无效')
    import re
    if not isinstance(values['camera'], str) or not re.fullmatch(r'/dev/video\d+', values['camera']):
        raise ValueError('请明确设置彩色相机节点')
    if not isinstance(values['sdk_root'], str) or not Path(values['sdk_root']).is_absolute():
        raise ValueError('媒体SDK需为明确的绝对路径')
    from outfit.gestures import validate_config
    validate_config(values)
    return values


def status(root, state, **details):
    value = {'state': state, 'updated_at': int(time.time()), **details}
    target = root / 'data'
    target.mkdir(exist_ok=True)
    temporary = target / 'robot-status.json.tmp'
    temporary.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')
    os.replace(temporary, target / 'robot-status.json')
    print(json.dumps(value, ensure_ascii=False), flush=True)


def run_enabled(config, stop, root=ROOT):
    # Imports and device initialization are strictly below the enable guard.
    if config['hardware_enabled'] is not True:
        raise ValueError('硬件未启用')
    from outfit.capture import capture_image
    from outfit.config import Settings
    from outfit.imaging import normalize_image
    from outfit.microphone import Microphone
    from outfit.robot_audio import _load_media, publish_wav, require_ready
    from outfit.speech import VoiceConfig, synthesize
    from outfit.vision import review_image
    from outfit.voice_trigger import wait_for_trigger
    from outfit.audit import append_event
    from outfit.gestures import GesturePlayer
    from outfit.performance import perform_turn
    vision, voice = Settings(root).read(), VoiceConfig(root).read()
    if not vision['api_key'] or not vision['model'] or not voice['app_id'] or not voice['access_key']:
        raise ValueError('请先完成视觉与声音配置')
    sdk = _load_media(config['sdk_root'])
    controller = sdk.MediaController.instance()
    if controller.init() is False:
        raise ValueError('媒体接口初始化失败')
    require_ready(controller)
    def permitted():
        return not stop.is_set() and load_config(root)['hardware_enabled'] is True
    def gesture_permitted():
        current = load_config(root)
        # New settings require a restart; hot changes may only revoke permission.
        keys = ('gestures_enabled', 'wave_verified', 'thinking_verified',
                'thinking_teach_index', 'gesture_sdk_root')
        return permitted() and current['gestures_enabled'] is True and all(
            current[key] == config[key] for key in keys)
    gestures = GesturePlayer(config, stop, permitted=gesture_permitted)
    while not stop.is_set():
        # A change to enabled=false also stops the cloud microphone session.
        listening_stop = threading.Event()
        monitor_done = threading.Event()
        def monitor():
            while not monitor_done.wait(.2):
                try:
                    disabled = not load_config(root)['hardware_enabled']
                except Exception:
                    disabled = True
                if stop.is_set() or disabled:
                    listening_stop.set()
                    return
        monitor_thread = threading.Thread(target=monitor, daemon=True)
        monitor_thread.start()
        status(root, 'listening', trigger=config['trigger'])
        try:
            with Microphone(controller, enabled=True, channel=config['mic_channel']) as microphone:
                triggered = wait_for_trigger(voice, microphone.read, listening_stop)
        finally:
            monitor_done.set()
            monitor_thread.join(timeout=1)
        if stop.is_set() or not load_config(root)['hardware_enabled']:
            return
        if not triggered:
            continue
        run_id = uuid.uuid4().hex
        # Microphone is already unsubscribed, so our output cannot trigger itself.
        perform_turn(config, stop, root, run_id, vision=vision, voice=voice,
            gestures=gestures, capture=capture_image, normalize=normalize_image,
            review=review_image, synthesize=synthesize,
            playback=lambda audio, **kwargs: publish_wav(audio, enabled=True,
                sdk=sdk, controller=controller, **kwargs),
            record=append_event, report=status, permitted=permitted)
        if stop.wait(2): return


def main(argv=None):
    parser = argparse.ArgumentParser(description='机器人本机穿搭锐评，无网页；当前默认禁止硬件')
    parser.add_argument('--check', action='store_true', help='只查配置与文件，不加载SDK')
    args = parser.parse_args(argv)
    os.umask(0o077)
    from outfit.config import Settings
    from outfit.speech import VoiceConfig
    from outfit.prompts import PROMPT_VERSION
    config = load_config()
    if args.check:
        sdk = Path(config['sdk_root'])
        print(json.dumps({'vision_configured': Settings(ROOT).public()['configured'],
              'voice_configured': VoiceConfig(ROOT).public()['configured'],
              'hardware_enabled': config['hardware_enabled'], 'trigger': config['trigger'],
              'gestures_enabled': config['gestures_enabled'],
              'wave_verified': config['wave_verified'],
              'thinking_verified': config['thinking_verified'],
              'thinking_teach_index': config['thinking_teach_index'],
              'prompt_version': PROMPT_VERSION,
              'media_binary_present': any(path.stat().st_size > 0 for path in (sdk/'build').glob('mediacontrol_py*.so')),
              'dds_config_present': (sdk/'config/dds.xml').is_file(),
              'hardware_sdk_loaded': False}, ensure_ascii=False))
        return 0
    if sys.platform != 'linux':
        print('机器人本机服务仅支持Linux；配置检查可使用--check。')
        return 1
    import fcntl
    (ROOT / 'data').mkdir(exist_ok=True)
    with (ROOT / 'data/robot-service.lock').open('a') as lock:
        try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError: return 1
        stop = threading.Event()
        for number in (signal.SIGINT, signal.SIGTERM):
            signal.signal(number, lambda *_: stop.set())
        failed = False
        try:
            config = load_config()
            if not config['hardware_enabled']:
                status(ROOT, 'standby_disabled', trigger=config['trigger'],
                       gestures_enabled=config['gestures_enabled'],
                       prompt_version=PROMPT_VERSION, hardware_sdk_loaded=False)
                # Enabling requires an explicit restart, preventing silent activation.
                stop.wait()
            else:
                run_enabled(config, stop)
        except Exception as error:
            failed = True
            status(ROOT, 'stopped_error', error_type=type(error).__name__, message='机器人服务已停止，请核查本地配置、媒体隔离和网络后再启动')
            return 1
        finally:
            if not failed:
                status(ROOT, 'stopped')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
