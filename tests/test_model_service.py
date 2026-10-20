import plistlib
from subprocess import CompletedProcess
from unittest.mock import Mock

import pytest

from scripts import model_service

DOMAIN = "gui/501"
SERVICE = f"{DOMAIN}/{model_service.LABEL}"


@pytest.fixture
def service_config(tmp_path, monkeypatch):
    root = tmp_path / "project"
    monkeypatch.setattr(model_service, "ROOT", root)
    monkeypatch.setattr(model_service, "RETRY_ATTEMPTS", 3)
    sleep = Mock()
    monkeypatch.setattr(model_service.time, "sleep", sleep)
    target = tmp_path / "LaunchAgents" / f"{model_service.LABEL}.plist"
    definition = {"Label": model_service.LABEL, "WorkingDirectory": str(root), "KeepAlive": True}
    return target, definition, sleep


def existing(target, definition):
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(plistlib.dumps(definition))


def mock_commands(monkeypatch, commands):
    pending = list(commands)
    seen = []

    def run(argv, **kwargs):
        assert kwargs == {"capture_output": True}
        expected, returncode = pending.pop(0)
        assert argv == ["launchctl", *expected]
        seen.append(argv)
        return CompletedProcess(argv, returncode, stdout=b"", stderr=b"")

    monkeypatch.setattr(model_service.subprocess, "run", run)
    return seen, pending


def test_identical_registered_configuration_restarts_without_bootout(service_config, monkeypatch):
    target, definition, sleep = service_config
    existing(target, definition)
    before = target.stat().st_mtime_ns
    _, pending = mock_commands(monkeypatch, [
        (["print", SERVICE], 0),
        (["kickstart", "-k", SERVICE], 0),
    ])

    model_service.install(definition, DOMAIN, SERVICE, target)

    assert not pending
    assert target.stat().st_mtime_ns == before
    sleep.assert_not_called()


def test_changed_configuration_waits_for_bootout_and_retries_bootstrap(service_config, monkeypatch):
    target, definition, sleep = service_config
    existing(target, {**definition, "KeepAlive": False})
    _, pending = mock_commands(monkeypatch, [
        (["print", SERVICE], 0),
        (["bootout", SERVICE], 0),
        (["print", SERVICE], 0),
        (["print", SERVICE], 1),
        (["bootstrap", DOMAIN, str(target)], 5),
        (["bootstrap", DOMAIN, str(target)], 0),
    ])

    model_service.install(definition, DOMAIN, SERVICE, target)

    assert not pending
    assert sleep.call_count == 2
    assert plistlib.loads(target.read_bytes()) == definition
    assert target.stat().st_mode & 0o777 == 0o600


def test_removal_timeout_preserves_previous_configuration(service_config, monkeypatch):
    target, definition, sleep = service_config
    previous = {**definition, "KeepAlive": False}
    existing(target, previous)
    _, pending = mock_commands(monkeypatch, [
        (["print", SERVICE], 0),
        (["bootout", SERVICE], 5),
        *[(["print", SERVICE], 0)] * 3,
    ])

    with pytest.raises(SystemExit, match="尚未退出"):
        model_service.install(definition, DOMAIN, SERVICE, target)

    assert not pending
    assert sleep.call_count == 2
    assert plistlib.loads(target.read_bytes()) == previous


def test_bootstrap_retry_limit_is_bounded(service_config, monkeypatch):
    target, definition, sleep = service_config
    _, pending = mock_commands(monkeypatch, [
        (["print", SERVICE], 1),
        *[(["bootstrap", DOMAIN, str(target)], 5)] * 3,
    ])

    with pytest.raises(SystemExit, match="启动失败"):
        model_service.install(definition, DOMAIN, SERVICE, target)

    assert not pending
    assert sleep.call_count == 2


def test_stopped_service_bootstraps_without_bootout(service_config, monkeypatch):
    target, definition, sleep = service_config
    existing(target, definition)
    _, pending = mock_commands(monkeypatch, [
        (["print", SERVICE], 1),
        (["bootstrap", DOMAIN, str(target)], 0),
    ])

    model_service.install(definition, DOMAIN, SERVICE, target)

    assert not pending
    sleep.assert_not_called()


def test_vanished_registered_service_bootstraps_after_kickstart_race(service_config, monkeypatch):
    target, definition, _ = service_config
    existing(target, definition)
    _, pending = mock_commands(monkeypatch, [
        (["print", SERVICE], 0),
        (["kickstart", "-k", SERVICE], 3),
        (["print", SERVICE], 1),
        (["bootstrap", DOMAIN, str(target)], 0),
    ])

    model_service.install(definition, DOMAIN, SERVICE, target)

    assert not pending


def test_foreign_project_configuration_is_untouched(service_config, monkeypatch):
    target, definition, _ = service_config
    foreign = {**definition, "WorkingDirectory": "/another/project"}
    existing(target, foreign)
    run = Mock(side_effect=AssertionError("must not call launchctl"))
    monkeypatch.setattr(model_service.subprocess, "run", run)

    with pytest.raises(SystemExit, match="其他项目"):
        model_service.install(definition, DOMAIN, SERVICE, target)

    assert plistlib.loads(target.read_bytes()) == foreign
    run.assert_not_called()


def test_registered_service_without_owned_plist_is_untouched(service_config, monkeypatch):
    target, definition, _ = service_config
    _, pending = mock_commands(monkeypatch, [(["print", SERVICE], 0)])

    with pytest.raises(SystemExit, match="没有可核验"):
        model_service.install(definition, DOMAIN, SERVICE, target)

    assert not pending
    assert not target.exists()
