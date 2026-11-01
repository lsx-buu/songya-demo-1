"""Fixed, opt-in gestures using the manufacturer's HighController API.

Software state checks do not substitute for supervised physical validation.
"""
from __future__ import annotations

import importlib
import math
import os
from pathlib import Path
import platform
import sys
import threading
import time
from urllib.parse import unquote, urlparse

GESTURE_DEFAULTS = {'gestures_enabled': False, 'wave_verified': False,
                    'thinking_verified': False, 'thinking_teach_index': None,
                    'gesture_sdk_root': ''}
_COMMANDS = {'thinking': ('PLAYTEACH', 'thinking_teach_index', 23),
             'dismiss': ('SWING', None, 8)}


def validate_config(config):
    if not isinstance(config, dict):
        raise ValueError('动作配置格式无效')
    values = dict(GESTURE_DEFAULTS, **config)
    for key in ('gestures_enabled', 'wave_verified', 'thinking_verified'):
        if type(values[key]) is not bool:
            raise ValueError(f'{key}必须是布尔值')
    if type(values.get('hardware_enabled', False)) is not bool:
        raise ValueError('hardware_enabled必须是布尔值')
    index = values['thinking_teach_index']
    if index is not None and (type(index) is not int or not 1 <= index <= 65535):
        raise ValueError('thinking_teach_index必须是1到65535的整数')
    if values['thinking_verified'] and index is None:
        raise ValueError('thinking_verified需要已核验的动作编号')
    root = values['gesture_sdk_root']
    if not isinstance(root, str) or (values['gestures_enabled'] and not Path(root).is_absolute()):
        raise ValueError('启用动作时必须提供SDK绝对路径')
    return values


def _load_sdk(root):
    if sys.platform != 'linux' or platform.machine() != 'aarch64':
        raise RuntimeError('动作SDK仅支持Jetson Linux aarch64')
    path = Path(root).resolve()
    build, xml = path / 'build', path / 'config/dds.xml'
    if not xml.is_file() or not any(p.stat().st_size for p in build.glob('highcontrol_py*.so')):
        raise RuntimeError('动作SDK模块或DDS配置缺失')
    # Media has already selected DDS. Never silently select a different network.
    uri = urlparse(os.environ.get('CYCLONEDDS_URI', ''))
    if uri.scheme != 'file' or uri.netloc not in ('', 'localhost'):
        raise RuntimeError('需要明确的本机DDS文件配置')
    if Path(unquote(uri.path)).read_bytes() != xml.read_bytes():
        raise RuntimeError('媒体和动作DDS配置内容不一致')
    previous = sys.modules.get('highcontrol_py')
    if previous is not None and not Path(previous.__file__).resolve().is_relative_to(build):
        raise RuntimeError('已有动作模块路径不匹配')
    sys.path.insert(0, str(build))
    module = importlib.import_module('highcontrol_py')
    if not Path(module.__file__).resolve().is_relative_to(build):
        raise RuntimeError('动作SDK模块路径不匹配')
    return module


class GesturePlayer:
    def __init__(self, config, stop, *, permitted=lambda: True, sdk_loader=None,
                 clock=time.monotonic, wait=None):
        self.config = validate_config(config)
        self.stop, self.permitted = stop, permitted
        self._loader, self.clock = sdk_loader or _load_sdk, clock
        self.wait = wait or stop.wait
        self._lock = threading.RLock()
        self._sdk = self._controller = None
        self._latest = self._inflight = self._last_sent = None
        self._entered = self._returned = self._failed = self._load_failed = False
        self._failure = None

    def _record(self, name, status, reason, run_id):
        command, key, _ = _COMMANDS.get(name, ('', None, None))
        return {'source': 'noetix_highcontroller', 'action': name, 'command': command,
                'index': self.config[key] if key else 0, 'status': status,
                'reason': reason, 'run_id': run_id, 'observed': False}

    def _permitted(self):
        try:
            return not self.stop.is_set() and self.permitted() is True
        except Exception:
            return False

    def _fail(self, reason):
        self._failed, self._failure = True, reason
        self.stop.set()  # Cancels our remaining work; this is NOT a robot emergency stop.

    def _on_status(self, status):
        try:
            motors = list(status.motor_data)
            if len(motors) != 21 or {m.motor_id for m in motors} != set(range(21)):
                raise ValueError()
            if any(type(m.motor_id) is not int or type(m.error) is not int for m in motors):
                raise ValueError()
            velocities = [float(m.vel) for m in motors]
            if not all(math.isfinite(v) for v in velocities) or type(status.workmode) is not int:
                raise ValueError()
            state = {'at': self.clock(), 'mode': status.workmode,
                     'error': any(m.error != 0 for m in motors),
                     'stationary': max(abs(v) for v in velocities) <= .2}
        except (AttributeError, TypeError, ValueError, OverflowError):
            state = None
        with self._lock:
            self._latest = state
            if self._inflight is None:
                return
            if state is None or state['error']:
                self._fail('invalid_hardware_state')
                return
            expected = _COMMANDS[self._inflight['name']][2]
            if state['mode'] not in (2, expected):
                self._fail('unexpected_mode')
            if not self._returned and state['at'] - self._inflight['at'] > 12:
                self._fail('action_timeout')
            if state['mode'] == expected:
                self._entered = True
            if self._entered and state['mode'] == 2 and state['stationary']:
                self._returned = True

    def _fresh(self):
        state = self._latest
        return state is not None and 0 <= self.clock() - state['at'] <= 1

    def _ready(self):
        return self._fresh() and not self._latest['error'] and self._latest['mode'] == 2 and self._latest['stationary']

    def _connect(self):
        if self._controller is not None:
            return
        self._sdk = self._loader(self.config['gesture_sdk_root'])
        controller = self._sdk.HighController.instance()
        if controller.init() is False:
            raise RuntimeError('动作SDK初始化失败')
        self._controller = controller
        controller.subscribe_robot_hardware_status(self._on_status)

    def start(self, name, run_id):
        def skipped(reason): return self._record(name, 'skipped', reason, run_id)
        if name not in _COMMANDS: return skipped('unknown_action')
        if self.config.get('hardware_enabled') is not True or not self.config['gestures_enabled']:
            return skipped('disabled')
        if not self.config['thinking_verified' if name == 'thinking' else 'wave_verified']:
            return skipped('not_verified')
        with self._lock:
            if self._failed: return skipped('latched_failed')
            if self._load_failed: return skipped('sdk_unavailable')
            if self._inflight is not None: return skipped('inflight')
            if self._last_sent is not None and self.clock() - self._last_sent < 8:
                return skipped('cooldown')
        if not self._permitted(): return skipped('not_permitted')
        try:
            self._connect()
        except Exception:
            self._load_failed = True
            return skipped('sdk_unavailable')
        deadline = self.clock() + 2
        while self.clock() < deadline:
            if not self._permitted(): return skipped('not_permitted')
            with self._lock:
                if self._ready(): break
            self.wait(min(.05, max(0, deadline - self.clock())))
        with self._lock:
            if not self._permitted(): return skipped('not_permitted')
            if not self._ready(): return skipped('not_ready')
            command, key, _ = _COMMANDS[name]
            try:
                action = getattr(self._sdk.ControlCmd, command)
            except (AttributeError, TypeError):
                self._load_failed = True
                return skipped('sdk_unavailable')
            self._entered = self._returned = False
            self._failure = None
            self._last_sent = self.clock()
            self._inflight = {'name': name, 'run_id': run_id, 'at': self._last_sent}
            try:
                self._controller.publish_cmd(0, 0, 0, action, self.config[key] if key else 0)
            except Exception:
                self._fail('publish_failed')
                return self._record(name, 'outcome_unknown', self._failure, run_id)
        return self._record(name, 'sent', 'awaiting_return', run_id)

    def finish(self):
        with self._lock:
            item = self._inflight
        if item is None: return None
        while True:
            with self._lock:
                if not self._permitted(): self._fail('permission_revoked')
                if not self._fresh(): self._fail('stale_hardware_state')
                if self._failed:
                    outcome, reason = 'outcome_unknown', self._failure
                    break
                if self._returned and self._ready():
                    outcome, reason = 'returned_ready', 'mode_returned'
                    break
                remaining = item['at'] + 12 - self.clock()
                if remaining <= 0:
                    self._fail('action_timeout')
                    outcome, reason = 'outcome_unknown', self._failure
                    break
            self.wait(min(.05, remaining))
        with self._lock:
            self._inflight = None
        return self._record(item['name'], outcome, reason, item['run_id'])
