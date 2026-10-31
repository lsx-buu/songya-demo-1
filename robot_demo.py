"""One button/Enter -> outfit photo -> short roast -> WAV; hardware opt-in only."""
import argparse
import base64
import json
import os
from pathlib import Path
import threading
import uuid

from outfit.capture import CaptureError, capture_image
from outfit.config import ConfigError, Settings
from outfit.imaging import ImageInputError, normalize_image
from outfit.speech import VoiceConfig, VoiceError, synthesize
from outfit.vision import VisionError, review_image
from outfit.audit import append_event

ROOT = Path(__file__).resolve().parent


def main(argv=None):
    parser = argparse.ArgumentParser(description='布米穿搭锐评：只拍照、想梗、合成声音。默认只保存WAV。')
    inputs = parser.add_mutually_exclusive_group(required=True)
    inputs.add_argument('--image', type=Path, help='已有JPEG/PNG照片；不访问摄像头')
    inputs.add_argument('--camera', help='明确指定机器人彩色摄像头节点，例如 /dev/video0')
    parser.add_argument('--loop', action='store_true', help='每次按回车触发一轮；输入q退出')
    parser.add_argument('--output', type=Path, default=ROOT / 'data/last_roast.wav')
    parser.add_argument('--robot-speaker', action='store_true', help='仅在明确恢复实机测试并隔离原厂语音后使用')
    parser.add_argument('--sdk-root', default=os.getenv('BUMI_SDK_ROOT', ''))
    args = parser.parse_args(argv)
    if args.robot_speaker and not args.sdk_root:
        parser.error('--robot-speaker需要--sdk-root；当前默认不会连接机器人')
    try:
        settings = Settings(ROOT).read()
        if not settings['api_key'] or not settings['model']:
            raise ConfigError('请先在本机操作页配置方舟视觉模型和API Key')
        voice = VoiceConfig(ROOT).read()
        if not voice['app_id'] or not voice['access_key']:
            raise VoiceError('请先配置语音App ID和Access Key')
        while True:
            if args.loop and input('回车拍照锐评，输入q退出：').strip().lower() == 'q':
                return 0
            if args.image:
                if args.image.stat().st_size > 4 * 1024 * 1024:
                    raise ImageInputError('照片超过4MB，请先缩小')
                suffix = args.image.suffix.lower()
                if suffix not in ('.jpg', '.jpeg', '.png'):
                    raise ImageInputError('只接受JPEG/PNG照片')
                mime = 'image/png' if suffix == '.png' else 'image/jpeg'
                image = 'data:' + mime + ';base64,' + base64.b64encode(args.image.read_bytes()).decode('ascii')
            else:
                image = capture_image(args.camera)
            result = review_image(normalize_image(image), settings['model'], settings['api_key'])
            run_id = uuid.uuid4().hex
            trace = dict(result['provenance'], run_id=run_id)
            append_event(ROOT, 'vision', trace)
            if result['status'] == 'retake':
                print(json.dumps({'status': 'retake', 'reason': result['reason']}, ensure_ascii=False))
            else:
                speech_trace = []
                def record_speech(details):
                    speech_trace.append(append_event(ROOT, 'speech', dict(details, run_id=run_id)))
                wav = synthesize('布米嘴替开评。' + result['roast'], voice, on_provenance=record_speech)
                args.output.parent.mkdir(parents=True, exist_ok=True)
                args.output.write_bytes(wav)
                report = {'status': 'generated', 'roast': result['roast'], 'wav': str(args.output),
                          'robot_audio_sent': False, 'photo_saved': False, 'run_id': run_id,
                          'provenance': trace, 'speech_provenance': speech_trace}
                if args.robot_speaker:
                    from outfit.robot_audio import publish_wav
                    stop = threading.Event()
                    try:
                        report['robot_output'] = publish_wav(wav, enabled=True, sdk_root=args.sdk_root, stop=stop)
                        report['robot_audio_sent'] = report['robot_output']['status'] == 'sent'
                        append_event(ROOT, 'playback', dict(report['robot_output'], run_id=run_id))
                    except KeyboardInterrupt:
                        stop.set()
                        raise
                print(json.dumps(report, ensure_ascii=False))
            if not args.loop:
                return 0
    except KeyboardInterrupt:
        print('已停止；未继续发送音频。')
        return 130
    except (ConfigError, CaptureError, ImageInputError, VisionError, VoiceError, OSError) as error:
        print(json.dumps({'error': str(error)}, ensure_ascii=False))
        return 1
    except Exception as error:
        from outfit.robot_audio import RobotPlaybackError
        if isinstance(error, RobotPlaybackError):
            print(json.dumps({'error': str(error), 'wav_retained': str(args.output)}, ensure_ascii=False))
            return 1
        raise


if __name__ == '__main__':
    raise SystemExit(main())
