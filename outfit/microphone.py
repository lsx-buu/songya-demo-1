"""Bounded read-only subscription to the robot microphone, explicit enable only."""
from collections import deque
import struct
import threading

from .robot_audio import RobotPlaybackError, require_ready


class Microphone:
    def __init__(self, controller, *, enabled=False, channel=0):
        if not enabled:
            raise RobotPlaybackError('机器人麦克风尚未启用')
        if type(channel) is not int or channel not in range(4):
            raise RobotPlaybackError('麦克风仅可选头部通道0—3，不能选扬声器回采')
        self.controller, self.channel = controller, channel
        self.cv = threading.Condition()
        self.frames = deque(maxlen=25)
        self.pending = bytearray()
        self.active = False
        self.error = False

    def __enter__(self):
        require_ready(self.controller)
        if not callable(getattr(self.controller, 'unsubscribe_internal_audio_capture', None)):
            raise RobotPlaybackError('媒体扩展缺少麦克风回调清理接口，未启动采集')
        self.active = True
        try:
            self.controller.subscribe_internal_audio_capture(self._capture)
        except Exception:
            self.active = False
            raise RobotPlaybackError('麦克风订阅失败，未启动语音监听') from None
        return self

    def _capture(self, stream):
        with self.cv:
            if not self.active:
                return
            try:
                if (stream.channels, stream.sample_rate, stream.format) != (8, 16000, 2):
                    raise ValueError()
                values = list(stream.audio_data)
                if not values or len(values) % 8 or len(values) > 128000:
                    raise ValueError()
                mono = values[self.channel::8]
                self.pending.extend(struct.pack('<%dh' % len(mono), *mono))
                while len(self.pending) >= 640:
                    self.frames.append(bytes(self.pending[:640]))
                    del self.pending[:640]
            except (AttributeError, ValueError, TypeError, struct.error, OverflowError):
                self.error = True
                self.active = False
                self.frames.clear()
            self.cv.notify_all()

    def read(self, timeout=.1):
        with self.cv:
            self.cv.wait_for(lambda: self.frames or self.error or not self.active, timeout)
            if self.error:
                raise RobotPlaybackError('机器人麦克风数据格式无效，已停止监听')
            return self.frames.popleft() if self.frames else None

    def __exit__(self, *args):
        with self.cv:
            self.active = False
            self.frames.clear()
            self.pending.clear()
            self.cv.notify_all()
        try:
            self.controller.unsubscribe_internal_audio_capture()
        except Exception:
            raise RobotPlaybackError('麦克风回调清理失败，请停止进程并核查媒体接口') from None
        return False
