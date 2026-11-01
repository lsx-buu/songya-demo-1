from pathlib import Path
import threading
from types import SimpleNamespace as NS

import pytest

from outfit.gestures import GesturePlayer, validate_config


def status(mode=2, velocity=0., count=21, error=0):
    # Same field names as the official highcontrol_py binding, not an invented API.
    return NS(workmode=mode, motor_data=[NS(motor_id=i, error=error, vel=velocity)
                                        for i in range(count)])


class Rig:
    def __init__(self, initial=None, *, fail=False, permitted=lambda: True):
        self.now, self.sent, self.loads = 0., [], 0
        self.stop, self.callback, self.pump = threading.Event(), None, None
        self.initial = initial if initial is not None else status()
        self.fail = fail
        self.config = dict(hardware_enabled=True, gestures_enabled=True,
            wave_verified=True, thinking_verified=True, thinking_teach_index=7,
            gesture_sdk_root=str(Path.cwd()))
        self.sdk = NS(HighController=NS(instance=lambda: self),
                      ControlCmd=NS(SWING=object(), PLAYTEACH=object()))
        self.player = GesturePlayer(self.config, self.stop, sdk_loader=self.load,
            permitted=permitted, clock=lambda: self.now, wait=self.wait)

    def init(self): return True
    def subscribe_robot_hardware_status(self, callback):
        self.callback = callback
        callback(self.initial)
    def publish_cmd(self, x, y, z, action, index):
        self.sent.append((x, y, z, action, index))
        if self.fail: raise RuntimeError('private sdk detail')
    def load(self, root):
        self.loads += 1
        return self.sdk
    def wait(self, seconds):
        self.now += seconds
        if self.pump: self.pump()
        return self.stop.is_set()
    def complete(self, mode):
        self.callback(status(mode))
        self.callback(status())


def test_disabled_and_unverified_never_import_motion_sdk():
    def forbidden(*args): raise AssertionError('must not load SDK')
    for config in ({}, {'gestures_enabled': False},
                   {'hardware_enabled': True, 'gestures_enabled': True,
                    'gesture_sdk_root': str(Path.cwd())}):
        player = GesturePlayer(config, threading.Event(), sdk_loader=forbidden)
        assert player.start('thinking', 'r')['status'] == 'skipped'
        assert player.start('dismiss', 'r')['status'] == 'skipped'


@pytest.mark.parametrize('extra', [dict(gestures_enabled='false'),
    dict(thinking_verified=True), dict(thinking_teach_index=True),
    dict(thinking_teach_index=0), dict(thinking_teach_index=65536),
    dict(gestures_enabled=True, gesture_sdk_root='relative'), dict(hardware_enabled='true')])
def test_strict_flags_and_teach_index(extra):
    with pytest.raises(ValueError): validate_config(extra)


def test_only_official_zero_velocity_edge_commands_and_one_controller():
    rig = Rig()
    assert rig.player.start('thinking', 'r')['status'] == 'sent'
    assert rig.sent == [(0, 0, 0, rig.sdk.ControlCmd.PLAYTEACH, 7)]
    assert rig.player.start('dismiss', 'r')['reason'] == 'inflight'
    rig.complete(23)
    assert rig.player.finish()['status'] == 'returned_ready'
    assert rig.player.start('dismiss', 'r')['reason'] == 'cooldown'
    rig.now = 9
    rig.callback(status())
    assert rig.player.start('dismiss', 'r')['status'] == 'sent'
    assert rig.loads == 1
    assert rig.sent[-1] == (0, 0, 0, rig.sdk.ControlCmd.SWING, 0)
    rig.complete(8)
    assert rig.player.finish()['observed'] is False


@pytest.mark.parametrize('bad', [status(count=20), status(mode=1), status(error=1),
    status(velocity=1), status(velocity=float('nan'))])
def test_bad_or_moving_state_never_sends(bad):
    rig = Rig(bad)
    assert rig.player.start('thinking', 'r')['reason'] == 'not_ready'
    assert not rig.sent and not rig.stop.is_set()


def test_waits_for_first_valid_callback_but_rejects_stale_state():
    rig = Rig(status(count=0))
    rig.pump = lambda: rig.callback(status())
    assert rig.player.start('dismiss', 'r')['status'] == 'sent'
    rig.complete(8)
    rig.player.finish()
    rig.now = 9
    rig.pump = None
    assert rig.player.start('dismiss', 'r')['reason'] == 'not_ready'
    assert len(rig.sent) == 1


def test_publish_error_latches_and_never_retries_or_resets():
    rig = Rig(fail=True)
    assert rig.player.start('dismiss', 'r')['status'] == 'outcome_unknown'
    assert rig.stop.is_set()
    assert rig.player.finish()['status'] == 'outcome_unknown'
    assert rig.player.start('dismiss', 'r2')['reason'] == 'latched_failed'
    assert len(rig.sent) == 1


def test_deadline_is_from_send_and_heartbeat_does_not_prove_completion():
    rig = Rig()
    rig.player.start('thinking', 'r')
    rig.now = 11
    rig.callback(status(23))
    rig.pump = lambda: rig.callback(status(23))
    result = rig.player.finish()
    assert result['status'] == 'outcome_unknown'
    assert 12 <= rig.now <= 12.1
    assert rig.stop.is_set() and len(rig.sent) == 1


def test_completed_gesture_may_be_followed_by_long_cloud_inference():
    rig = Rig()
    rig.player.start('thinking', 'r')
    rig.now = 1
    rig.complete(23)
    rig.now = 30
    rig.callback(status())
    assert rig.player.finish()['status'] == 'returned_ready'
    assert not rig.stop.is_set()


def test_stop_hot_disable_and_stale_after_send_latch_without_reset():
    permit = {'enabled': True}
    rig = Rig(permitted=lambda: permit['enabled'])
    rig.player.start('dismiss', 'r')
    permit['enabled'] = False
    assert rig.player.finish()['status'] == 'outcome_unknown'
    assert rig.stop.is_set() and len(rig.sent) == 1
    rig = Rig()
    rig.player.start('dismiss', 'r')
    rig.now = 2
    assert rig.player.finish()['reason'] == 'stale_hardware_state'
    assert len(rig.sent) == 1


def test_inflight_error_or_unexpected_mode_never_claims_completion():
    for sample in (status(error=1), status(mode=5), status(count=0)):
        rig = Rig()
        rig.player.start('dismiss', 'r')
        rig.callback(sample)
        assert rig.player.finish()['status'] == 'outcome_unknown'
        assert rig.stop.is_set() and len(rig.sent) == 1


def test_unknown_action_cannot_reach_sdk():
    rig = Rig()
    for name in ('WALK', 'START', 'SWITCH', 'wave', '7'):
        assert rig.player.start(name, 'r')['reason'] == 'unknown_action'
    assert not rig.sent and rig.loads == 0
