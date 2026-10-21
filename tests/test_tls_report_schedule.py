import json
import plistlib
from subprocess import CompletedProcess
from unittest.mock import Mock

import pytest

from scripts import tls_report_schedule as schedule

DOMAIN = "gui/501"
SERVICE = f"{DOMAIN}/{schedule.LABEL}"


@pytest.fixture
def task(tmp_path, monkeypatch):
    root = tmp_path / "project"
    root.mkdir()
    monkeypatch.setattr(schedule, "ROOT", root)
    monkeypatch.setattr(schedule, "RETRY_ATTEMPTS", 3)
    monkeypatch.setattr(schedule.time, "sleep", Mock())
    monkeypatch.setattr(schedule, "system_timezone", lambda: schedule.TIMEZONE)
    target = tmp_path / "LaunchAgents" / f"{schedule.LABEL}.plist"
    definition = {
        "Label": schedule.LABEL,
        "WorkingDirectory": str(root),
        "ProgramArguments": [
            str(root / ".venv/bin/python"), str(root / "scripts/run_tls_error_report.py"),
        ],
        "StartCalendarInterval": {"Hour": 16, "Minute": 0},
        "RunAtLoad": False,
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


def test_install_is_idempotent_and_does_not_run_job(task, monkeypatch):
    target, definition = task
    existing(target, definition)
    previous_mtime = target.stat().st_mtime_ns
    pending = mock_launch(monkeypatch, target, [(["print", SERVICE], 0)])
    schedule.install(definition, DOMAIN, SERVICE, target)
    assert not pending
    assert target.stat().st_mtime_ns == previous_mtime


def test_install_checks_timezone_before_any_mutation(task, monkeypatch):
    target, definition = task
    monkeypatch.setattr(schedule, "system_timezone", lambda: "America/New_York")
    run = Mock(side_effect=AssertionError("must not call launchctl"))
    monkeypatch.setattr(schedule.subprocess, "run", run)
    with pytest.raises(SystemExit, match="Asia/Shanghai"):
        schedule.install(definition, DOMAIN, SERVICE, target)
    assert not target.exists()
    run.assert_not_called()


@pytest.mark.parametrize("foreign", ["directory", "program", "label"])
def test_install_and_stop_preserve_foreign_tasks(task, monkeypatch, foreign):
    target, definition = task
    if foreign == "directory":
        definition["WorkingDirectory"] = "/another/project"
    elif foreign == "program":
        definition["ProgramArguments"][1] = "/another/script.py"
    else:
        definition["Label"] = "com.octopus.assistant.model"
    existing(target, definition)
    run = Mock(side_effect=AssertionError("must not call launchctl"))
    monkeypatch.setattr(schedule.subprocess, "run", run)
    for action in (
        lambda: schedule.install(definition, DOMAIN, SERVICE, target),
        lambda: schedule.stop(SERVICE, target),
    ):
        with pytest.raises(SystemExit, match="其他项目或程序"):
            action()
    assert plistlib.loads(target.read_bytes()) == definition
    run.assert_not_called()


def test_registered_job_without_owned_plist_is_preserved(task, monkeypatch):
    target, definition = task
    pending = mock_launch(monkeypatch, target, [(["print", SERVICE], 0)])
    with pytest.raises(SystemExit, match="没有可核验"):
        schedule.install(definition, DOMAIN, SERVICE, target)
    assert not pending
    assert not target.exists()


def test_registered_path_must_match_owned_file(task, monkeypatch):
    target, definition = task
    existing(target, definition)
    pending = mock_launch(monkeypatch, target.with_name("foreign.plist"), [
        (["print", SERVICE], 0),
    ])
    with pytest.raises(SystemExit, match="没有可核验"):
        schedule.install(definition, DOMAIN, SERVICE, target)
    assert not pending


def test_install_retries_registration_without_starting_job(task, monkeypatch):
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


def test_changed_config_waits_for_old_task_to_exit(task, monkeypatch):
    target, definition = task
    existing(target, {**definition, "StartCalendarInterval": {"Hour": 15, "Minute": 0}})
    pending = mock_launch(monkeypatch, target, [
        (["print", SERVICE], 0),
        (["bootout", SERVICE], 0),
        (["print", SERVICE], 0),
        (["print", SERVICE], 1),
        (["bootstrap", DOMAIN, str(target)], 0),
    ])
    schedule.install(definition, DOMAIN, SERVICE, target)
    assert not pending
    assert plistlib.loads(target.read_bytes()) == definition


def test_failed_stop_retains_configuration(task, monkeypatch):
    target, definition = task
    existing(target, definition)
    pending = mock_launch(monkeypatch, target, [
        (["print", SERVICE], 0),
        (["bootout", SERVICE], 5),
        *[(["print", SERVICE], 0)] * 3,
    ])
    with pytest.raises(SystemExit, match="尚未退出"):
        schedule.stop(SERVICE, target)
    assert not pending
    assert plistlib.loads(target.read_bytes()) == definition


def test_status_reports_schedule_without_environment(task, monkeypatch):
    target, definition = task
    existing(target, definition)
    output = (
        f"{SERVICE} = {{\n\tpath = {target}\n\tstate = not running\n\truns = 2\n"
        "\tlast exit code = 0\n\tenvironment = {\n\t\tTOKEN => private\n\t}\n}"
    )
    monkeypatch.setattr(schedule.subprocess, "run", lambda *a, **kw:
                        CompletedProcess(a, 0, stdout=output, stderr="private"))
    result = schedule.status(SERVICE, target)
    assert result["registered"] is True
    assert result["schedule"] == "daily 16:00 Asia/Shanghai"
    assert result["last_exit_code"] == 0
    assert result["next_scheduled_at"].endswith("T16:00:00+08:00")
    assert "private" not in json.dumps(result)
    assert "TOKEN" not in json.dumps(result)


def test_definition_uses_fixed_daily_time_and_project_command(task, monkeypatch):
    _, expected = task
    python = schedule.ROOT / ".venv/bin/python"
    script = schedule.ROOT / "scripts/run_tls_error_report.py"
    for path in (python, script):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
        path.chmod(0o700)
    monkeypatch.setattr(schedule.shutil, "which", lambda cmd: f"/installed/{cmd}/bin/{cmd}")
    monkeypatch.setenv("VERY_PRIVATE_TOKEN", "do-not-copy")
    definition = schedule.make_definition()
    assert definition["Label"] == schedule.LABEL
    assert definition["ProgramArguments"] == [str(python), str(script)]
    assert definition["StartCalendarInterval"] == expected["StartCalendarInterval"]
    assert definition["RunAtLoad"] is False
    assert "KeepAlive" not in definition
    assert definition["StandardOutPath"].endswith("/.data/tls-error-reports/schedule.stdout.log")
    assert definition["StandardErrorPath"].endswith("/.data/tls-error-reports/schedule.stderr.log")
    for command in (*schedule.REQUIRED_COMMANDS, "node"):
        assert f"/installed/{command}/bin" in definition["EnvironmentVariables"]["PATH"].split(":")
    assert "VERY_PRIVATE_TOKEN" not in json.dumps(definition)
    assert "do-not-copy" not in json.dumps(definition)


def test_missing_required_executable_prevents_install(monkeypatch):
    monkeypatch.setattr(schedule.shutil, "which", lambda cmd: None if cmd == "ve" else f"/bin/{cmd}")
    with pytest.raises(SystemExit, match="ve"):
        schedule.executable_path()


def test_relative_executable_is_not_added_to_path(monkeypatch):
    monkeypatch.setattr(schedule.shutil, "which", lambda cmd: f"bin/{cmd}")
    with pytest.raises(SystemExit, match="ve"):
        schedule.executable_path()


def test_path_deduplicates_shared_cli_installation(monkeypatch):
    monkeypatch.setattr(schedule.shutil, "which", lambda cmd: f"/opt/homebrew/bin/{cmd}")
    result = schedule.executable_path().split(":")
    assert result.count("/opt/homebrew/bin") == 1
    assert "/usr/bin" in result and "/bin" in result
