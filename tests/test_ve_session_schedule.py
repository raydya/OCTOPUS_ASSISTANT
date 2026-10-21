import json
import plistlib
from subprocess import CompletedProcess
from unittest.mock import Mock

import pytest

from scripts import tls_report_schedule as launch
from scripts import ve_session_schedule as schedule

DOMAIN = "gui/501"
SERVICE = f"{DOMAIN}/{schedule.LABEL}"


@pytest.fixture
def task(tmp_path, monkeypatch):
    root = tmp_path / "project"
    root.mkdir()
    monkeypatch.setattr(schedule, "ROOT", root)
    monkeypatch.setattr(schedule, "RETRY_ATTEMPTS", 3)
    monkeypatch.setattr(launch, "RETRY_ATTEMPTS", 3)
    monkeypatch.setattr(schedule.time, "sleep", Mock())
    target = tmp_path / "LaunchAgents" / f"{schedule.LABEL}.plist"
    definition = {
        "Label": schedule.LABEL, "WorkingDirectory": str(root),
        "ProgramArguments": [str(root / ".venv/bin/python"), str(root / "scripts/maintain_ve_session.py")],
        "StartInterval": 1800, "RunAtLoad": True,
    }
    return target, definition


def existing(target, definition):
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(plistlib.dumps(definition))


def mock_launch(monkeypatch, target, commands):
    pending = list(commands)

    def run(argv, **kwargs):
        expected, returncode = pending.pop(0)
        assert argv == ["launchctl", *expected]
        output = f"{SERVICE} = {{\n\tpath = {target}\n\tstate = not running\n}}"
        return CompletedProcess(argv, returncode, stdout=output, stderr="")

    monkeypatch.setattr(schedule.subprocess, "run", run)
    return pending


def test_install_same_registered_definition_does_not_restart_task(task, monkeypatch):
    target, definition = task
    existing(target, definition)
    modified = target.stat().st_mtime_ns
    pending = mock_launch(monkeypatch, target, [(["print", SERVICE], 0)])
    schedule.install(definition, DOMAIN, SERVICE, target)
    assert not pending
    assert target.stat().st_mtime_ns == modified


def test_install_is_interval_based_and_never_requires_shanghai_timezone(task, monkeypatch):
    target, definition = task
    timezone_check = Mock(side_effect=AssertionError("Interval jobs must not require a timezone"))
    monkeypatch.setattr(launch, "require_timezone", timezone_check)
    monkeypatch.setattr(launch, "system_timezone", lambda: "America/New_York")
    pending = mock_launch(monkeypatch, target, [
        (["print", SERVICE], 1), (["bootstrap", DOMAIN, str(target)], 0),
    ])
    schedule.install(definition, DOMAIN, SERVICE, target)
    assert not pending
    timezone_check.assert_not_called()
    assert plistlib.loads(target.read_bytes())["StartInterval"] == 1800


@pytest.mark.parametrize("foreign", ["directory", "program", "python", "arguments", "label"])
def test_install_and_stop_leave_foreign_configuration_untouched(task, monkeypatch, foreign):
    target, definition = task
    if foreign == "directory":
        definition["WorkingDirectory"] = "/another/project"
    elif foreign == "program":
        definition["ProgramArguments"][1] = "/another/script.py"
    elif foreign == "python":
        definition["ProgramArguments"][0] = "/another/python"
    elif foreign == "arguments":
        definition["ProgramArguments"].append("--unexpected-argument")
    else:
        definition["Label"] = launch.LABEL
    existing(target, definition)
    run = Mock(side_effect=AssertionError("Must not mutate a foreign registered task"))
    monkeypatch.setattr(schedule.subprocess, "run", run)
    for action in (lambda: schedule.install(definition, DOMAIN, SERVICE, target),
                   lambda: schedule.stop(SERVICE, target)):
        with pytest.raises(SystemExit, match="其他项目或程序"):
            action()
    assert plistlib.loads(target.read_bytes()) == definition
    run.assert_not_called()


def test_symlink_configuration_is_never_followed_or_removed(task, monkeypatch):
    target, definition = task
    real = target.with_name("other.plist")
    existing(real, definition)
    target.symlink_to(real)
    monkeypatch.setattr(schedule.subprocess, "run", Mock(side_effect=AssertionError("No launchctl")))
    for action in (lambda: schedule.install(definition, DOMAIN, SERVICE, target),
                   lambda: schedule.stop(SERVICE, target)):
        with pytest.raises(SystemExit, match="符号链接"):
            action()
    assert target.is_symlink() and real.exists()


def test_unrecognized_configuration_is_preserved(task, monkeypatch):
    target, definition = task
    target.parent.mkdir()
    target.write_text("not a plist")
    monkeypatch.setattr(schedule.subprocess, "run", Mock(side_effect=AssertionError("No launchctl")))
    with pytest.raises(SystemExit, match="无法识别"):
        schedule.install(definition, DOMAIN, SERVICE, target)
    assert target.read_text() == "not a plist"


@pytest.mark.parametrize("configuration_exists", [True, False])
def test_registered_task_requires_owned_plist_and_matching_registered_path(task, monkeypatch, configuration_exists):
    target, definition = task
    if configuration_exists:
        existing(target, definition)
    reported_path = target.with_name("other.plist") if configuration_exists else target
    pending = mock_launch(monkeypatch, reported_path, [(["print", SERVICE], 0)])
    with pytest.raises(SystemExit, match="没有可核验"):
        schedule.install(definition, DOMAIN, SERVICE, target)
    assert not pending


def test_bootstrap_retries_are_bounded_and_plist_is_private(task, monkeypatch):
    target, definition = task
    pending = mock_launch(monkeypatch, target, [
        (["print", SERVICE], 1),
        (["bootstrap", DOMAIN, str(target)], 5),
        (["bootstrap", DOMAIN, str(target)], 0),
    ])
    schedule.install(definition, DOMAIN, SERVICE, target)
    assert not pending
    assert plistlib.loads(target.read_bytes()) == definition
    assert target.stat().st_mode & 0o777 == 0o600


def test_bootstrap_exhaustion_keeps_identifiable_config_without_restart_loop(task, monkeypatch):
    target, definition = task
    pending = mock_launch(monkeypatch, target, [
        (["print", SERVICE], 1), *[(["bootstrap", DOMAIN, str(target)], 5)] * 3,
    ])
    with pytest.raises(SystemExit, match="任务注册失败"):
        schedule.install(definition, DOMAIN, SERVICE, target)
    assert not pending
    assert plistlib.loads(target.read_bytes()) == definition


def test_changed_interval_waits_for_old_owned_task_before_replacement(task, monkeypatch):
    target, definition = task
    existing(target, {**definition, "StartInterval": 3600})
    pending = mock_launch(monkeypatch, target, [
        (["print", SERVICE], 0), (["bootout", SERVICE], 0),
        (["print", SERVICE], 0), (["print", SERVICE], 1),
        (["bootstrap", DOMAIN, str(target)], 0),
    ])
    schedule.install(definition, DOMAIN, SERVICE, target)
    assert not pending
    assert plistlib.loads(target.read_bytes()) == definition


def test_failed_stop_keeps_configuration_for_safe_retry(task, monkeypatch):
    target, definition = task
    existing(target, definition)
    pending = mock_launch(monkeypatch, target, [
        (["print", SERVICE], 0), (["bootout", SERVICE], 5),
        *[(["print", SERVICE], 0)] * 3,
    ])
    with pytest.raises(SystemExit, match="尚未退出"):
        schedule.stop(SERVICE, target)
    assert not pending
    assert plistlib.loads(target.read_bytes()) == definition


def test_stop_unregisters_and_then_is_idempotent(task, monkeypatch):
    target, definition = task
    existing(target, definition)
    pending = mock_launch(monkeypatch, target, [
        (["print", SERVICE], 0), (["bootout", SERVICE], 0),
        (["print", SERVICE], 1), (["print", SERVICE], 1),
    ])
    schedule.stop(SERVICE, target)
    schedule.stop(SERVICE, target)
    assert not pending
    assert not target.exists()


def test_status_reports_interval_without_environment_or_guessing_next_start(task, monkeypatch):
    target, definition = task
    existing(target, definition)
    output = (f"{SERVICE} = {{\n\tpath = {target}\n\tstate = not running\n\truns = 2\n"
              "\tlast exit code = 0\n\tenvironment = {\n\t\tTOKEN => FAKE_SECRET_VALUE\n\t}\n}")
    monkeypatch.setattr(schedule.subprocess, "run", lambda *a, **kw:
                        CompletedProcess(a, 0, stdout=output, stderr="private"))
    result = schedule.status(SERVICE, target)
    assert result["registered"] is True
    assert result["owned_configuration"] is True
    assert result["interval_seconds"] == 1800
    assert result["run_at_load"] is True
    assert result["last_exit_code"] == 0
    assert result["last_run_path"].endswith("/.data/ve-session/last-run.json")
    assert "next_scheduled_at" not in result
    assert "FAKE_SECRET_VALUE" not in json.dumps(result) and "TOKEN" not in json.dumps(result)


def test_status_does_not_claim_foreign_task_has_the_expected_schedule(task, monkeypatch):
    target, definition = task
    existing(target, {**definition, "WorkingDirectory": "/another/project"})
    pending = mock_launch(monkeypatch, target, [(["print", SERVICE], 0)])
    result = schedule.status(SERVICE, target)
    assert not pending
    assert result["registered"] is True and result["owned_configuration"] is False
    assert result["interval_seconds"] is None and result["run_at_load"] is False


def test_definition_runs_short_lived_entrypoint_every_30_minutes_with_only_ve_node_path(task, monkeypatch):
    python = schedule.ROOT / ".venv/bin/python"
    script = schedule.ROOT / "scripts/maintain_ve_session.py"
    for path in (python, script):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
        path.chmod(0o700)
    checked = []

    def which(command):
        checked.append(command)
        assert command in {"ve", "node"}
        return f"/installed/{command}/bin/{command}"

    monkeypatch.setattr(schedule.shutil, "which", which)
    monkeypatch.setenv("VOLCENGINE_SECRET_ACCESS_KEY", "do-not-copy")
    monkeypatch.setenv("GRAFANA_SERVICE_ACCOUNT_TOKEN", "also-do-not-copy")
    definition = schedule.make_definition()
    assert checked == ["ve", "node"]
    assert definition["ProgramArguments"] == [str(python), str(script)]
    assert definition["StartInterval"] == 1800 and definition["RunAtLoad"] is True
    assert "KeepAlive" not in definition and "StartCalendarInterval" not in definition
    assert set(definition["EnvironmentVariables"]) == {"PYTHONUNBUFFERED", "PATH"}
    assert "/installed/ve/bin" in definition["EnvironmentVariables"]["PATH"]
    assert "/installed/node/bin" in definition["EnvironmentVariables"]["PATH"]
    assert "do-not-copy" not in json.dumps(definition)
    assert definition["StandardOutPath"].endswith("/.data/ve-session/schedule.stdout.log")
    assert definition["StandardErrorPath"].endswith("/.data/ve-session/schedule.stderr.log")
    assert (schedule.ROOT / ".data/ve-session").stat().st_mode & 0o777 == 0o700


@pytest.mark.parametrize("missing", ["ve", "node"])
def test_required_ve_or_node_must_be_present(monkeypatch, missing):
    monkeypatch.setattr(schedule.shutil, "which", lambda command: None if command == missing else f"/bin/{command}")
    with pytest.raises(SystemExit, match=missing):
        schedule.executable_path()


def test_relative_cli_path_is_rejected(monkeypatch):
    monkeypatch.setattr(schedule.shutil, "which", lambda command: f"bin/{command}")
    with pytest.raises(SystemExit, match="ve"):
        schedule.executable_path()


def test_shared_cli_directory_is_not_duplicated(monkeypatch):
    monkeypatch.setattr(schedule.shutil, "which", lambda command: f"/opt/homebrew/bin/{command}")
    result = schedule.executable_path().split(":")
    assert result.count("/opt/homebrew/bin") == 1
    assert "/usr/bin" in result and "/bin" in result


def test_missing_project_entrypoint_fails_before_cli_lookup_or_runtime_creation(task, monkeypatch):
    monkeypatch.setattr(schedule.shutil, "which", Mock(side_effect=AssertionError("No CLI lookup")))
    with pytest.raises(SystemExit, match="Python 环境或 ve 会话维护脚本"):
        schedule.make_definition()
    assert not (schedule.ROOT / ".data/ve-session").exists()


def test_main_refuses_non_macos_without_invoking_launchctl(monkeypatch):
    monkeypatch.setattr(schedule.sys, "argv", ["ve_session_schedule.py", "install"])
    monkeypatch.setattr(schedule.sys, "platform", "linux")
    run = Mock(side_effect=AssertionError("No launchctl"))
    monkeypatch.setattr(schedule.subprocess, "run", run)
    with pytest.raises(SystemExit, match="仅管理 macOS"):
        schedule.main()
    run.assert_not_called()
