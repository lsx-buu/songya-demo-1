"""Explicit, guarded PCM publication only. Never wakes or configures an agent."""
from __future__ import annotations

import importlib
import io
import math
import os
from pathlib import Path
import platform
import struct
import sys
import time
import wave


class RobotPlaybackError(RuntimeError):
    pass


def require_ready(controller):
    expected = (
        ('get_internal_capture_audio_data_to_agent_enable', False),
        ('get_external_custom_audio_data_to_agent_enable', False),
        ('get_internal_agent_audio_data_to_playback_enable', False),
        ('get_external_custom_audio_data_to_playback_enable', True),
    )
    for getter, desired in expected:
        method = getattr(controller, getter, None)
        if not callable(method) or type(value := method()) is not bool or value != desired:
            raise RobotPlaybackError('原厂语音未隔离或外部播放未就绪；请由现场操作员核实，程序不会自动改开关')


def mono_16k(wav_bytes):
    try:
        with wave.open(io.BytesIO(wav_bytes), 'rb') as audio:
            rate = audio.getframerate()
            if (audio.getnchannels(), audio.getsampwidth()) != (1, 2) or rate not in (16000, 24000):
                raise ValueError()
            if not 0 < audio.getnframes() <= rate * 20:
                raise ValueError()
            raw = audio.readframes(audio.getnframes())
            if len(raw) != audio.getnframes() * 2:
                raise ValueError()
    except (wave.Error, EOFError, ValueError, TypeError):
        raise RobotPlaybackError('播放音频需为20秒以内的单声道16bit、16k或24k WAV') from None
    if rate == 16000:
        return raw
    samples = list(struct.unpack('<%dh' % (len(raw) // 2), raw))
    # 25-tap low-pass before 24k -> 16k decimation, with edge extension.
    padded = [samples[0]] * 12 + samples + [samples[-1]] * 12
    weights = {}
    for fraction in (0.0, 0.5):
        coefficients = []
        for tap in range(-12, 13):
            x = tap - fraction
            sinc = 1.0 if x == 0 else math.sin(math.pi * 2 / 3 * x) / (math.pi * 2 / 3 * x)
            coefficients.append(2 / 3 * sinc * (.54 + .46 * math.cos(math.pi * tap / 12)))
        total = sum(coefficients)
        weights[fraction] = [value / total for value in coefficients]
    output = []
    position = 12.0
    while position < len(samples) + 12:
        index = int(position)
        value = sum(padded[index + tap - 12] * weight
                    for tap, weight in enumerate(weights[position - index]))
        output.append(max(-32768, min(32767, round(value))))
        position += 1.5
    return struct.pack('<%dh' % len(output), *output)


def _load_media(sdk_root):
    root = Path(sdk_root).expanduser().resolve() if sdk_root else None
    if sys.platform != 'linux' or platform.machine() != 'aarch64':
        raise RobotPlaybackError('机器人播放入口仅用于匹配SDK的Jetson Linux环境')
    if not root or not (root / 'config/dds.xml').is_file() or not (root / 'build').is_dir():
        raise RobotPlaybackError('请指定配套SDK目录，包含build和config/dds.xml')
    candidates = list((root / 'build').glob('mediacontrol_py*.so'))
    if not candidates or not any(path.stat().st_size > 0 for path in candidates):
        raise RobotPlaybackError('媒体SDK模块缺失或为空，未连接或播放')
    sys.path.insert(0, str(root / 'build'))
    os.environ['CYCLONEDDS_URI'] = (root / 'config/dds.xml').as_uri()
    try:
        return importlib.import_module('mediacontrol_py')
    except (ImportError, OSError):
        raise RobotPlaybackError('媒体SDK加载失败，请核对Python版本和启动前的动态库路径') from None


def publish_wav(wav_bytes, *, enabled=False, sdk_root='', sdk=None, controller=None, stop=None,
                clock=time.monotonic, wait=time.sleep):
    if not enabled:
        raise RobotPlaybackError('机器人发声默认关闭；本轮仅准备程序，不连接实机')
    pcm = mono_16k(wav_bytes)
    module = sdk if sdk is not None else _load_media(sdk_root)
    try:
        if controller is None:
            controller = module.MediaController.instance()
            if controller.init() is False:
                raise RobotPlaybackError('媒体SDK初始化失败，没有发送音频')
        # Read-only preflight. No setters, wakeup, resume, microphones, or motion.
        require_ready(controller)
        count = 0
        deadline = clock()
        for offset in range(0, len(pcm), 640):
            if stop is not None and stop.is_set():
                return {'status': 'stopped', 'frames_sent': count, 'audibility_verified': False}
            chunk = pcm[offset:offset + 640].ljust(640, b'\0')
            mono = struct.unpack('<320h', chunk)
            stream = module.AudioStream()
            stream.channels, stream.sample_rate, stream.format = 2, 16000, 2
            stream.duration_ms, stream.timestamp_us = 20, time.time_ns() // 1000
            stream.audio_data = [sample for value in mono for sample in (value, value)]
            controller.publish_external_audio_playback_stream(stream)
            count += 1
            deadline += .020
            now = clock()
            if now - deadline > .12:
                deadline = now + .020
            if deadline > now:
                (stop.wait if stop is not None else wait)(deadline - now)
        return {'status': 'sent', 'frames_sent': count, 'audibility_verified': False}
    except RobotPlaybackError:
        raise
    except Exception:
        raise RobotPlaybackError('媒体接口异常，停止发送；实际播放状态需现场确认') from None
