import builtins
import json

import pytest

import robot_service


def test_service_defaults_are_hardware_disabled(tmp_path):
    assert robot_service.load_config(tmp_path)['hardware_enabled'] is False


def test_disabled_runtime_never_imports_devices_or_cloud_listener(monkeypatch):
    original = builtins.__import__
    def guarded(name,*args,**kwargs):
        if name in ('outfit.microphone','outfit.voice_trigger','outfit.robot_audio','outfit.capture'):
            raise AssertionError('Disabled service cannot import device components')
        return original(name,*args,**kwargs)
    monkeypatch.setattr(builtins,'__import__',guarded)
    with pytest.raises(ValueError,match='未启用'):
        robot_service.run_enabled({'hardware_enabled':False},None)


def test_configuration_rejects_string_enable_and_playback_reference(tmp_path):
    (tmp_path/'robot.json').write_text(json.dumps({'hardware_enabled':'false'}))
    with pytest.raises(ValueError): robot_service.load_config(tmp_path)
    (tmp_path/'robot.json').write_text(json.dumps({'mic_channel':6}))
    with pytest.raises(ValueError): robot_service.load_config(tmp_path)


def test_failure_keeps_error_state_for_source_diagnosis(tmp_path, monkeypatch):
    import sys
    from types import SimpleNamespace
    monkeypatch.setattr(robot_service,'ROOT',tmp_path)
    monkeypatch.setattr(robot_service.sys,'platform','linux')
    monkeypatch.setitem(sys.modules,'fcntl',SimpleNamespace(LOCK_EX=1,LOCK_NB=2,flock=lambda *args:None))
    monkeypatch.setattr(robot_service.signal,'signal',lambda *args:None)
    monkeypatch.setattr(robot_service,'load_config',lambda *args:{'hardware_enabled':True})
    def failed(*args): raise ValueError('test failure')
    monkeypatch.setattr(robot_service,'run_enabled',failed)
    assert robot_service.main([])==1
    assert json.loads((tmp_path/'data/robot-status.json').read_text(encoding='utf-8'))['state']=='stopped_error'
