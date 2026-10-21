"""Manage the daily 16:00 Shanghai TLS error report on this Mac."""

import argparse
import json
import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
LABEL = "com.octopus.assistant.tls-error-report"
TIMEZONE = "Asia/Shanghai"
LOCALTIME = Path("/etc/localtime")
REQUIRED_COMMANDS = ("ve", "cockpit", "lark-cli", "codex", "docker")
RETRY_ATTEMPTS = 40
RETRY_INTERVAL_SECONDS = 0.25


def system_timezone():
    resolved = str(LOCALTIME.resolve())
    marker = "/zoneinfo/"
    return resolved.split(marker, 1)[1] if marker in resolved else "unknown"


def require_timezone():
    if system_timezone() != TIMEZONE:
        raise SystemExit("本机系统时区必须为 Asia/Shanghai，定时任务未修改。")


def launch_state(service):
    result = subprocess.run(
        ["launchctl", "print", service], capture_output=True, text=True
    )
    state = {"registered": result.returncode == 0}
    if result.returncode:
        return state
    # Only top-level fields are relevant. Never expose launchd's environment.
    for line in result.stdout.splitlines():
        if not line.startswith("\t") or line.startswith("\t\t"):
            continue
        key, separator, value = line.strip().partition(" = ")
        if separator and key in {"path", "state"}:
            state[key] = value
        elif separator and key in {"pid", "runs", "last exit code"}:
            try:
                state[key.replace(" ", "_")] = int(value)
            except ValueError:
                pass
    return state


def owned_configuration(target):
    if target.is_symlink():
        raise SystemExit("同名任务配置是符号链接，未修改。")
    if not target.exists():
        return None
    try:
        definition = plistlib.loads(target.read_bytes())
    except (OSError, ValueError, plistlib.InvalidFileException):
        raise SystemExit("已有同名任务配置无法识别，未修改。") from None
    expected_script = str(ROOT / "scripts" / "run_tls_error_report.py")
    args = definition.get("ProgramArguments", []) if isinstance(definition, dict) else []
    if (
        not isinstance(definition, dict)
        or definition.get("Label") != LABEL
        or definition.get("WorkingDirectory") != str(ROOT)
        or not isinstance(args, list)
        or len(args) < 2
        or args[1] != expected_script
    ):
        raise SystemExit("已有同名任务属于其他项目或程序，未修改。")
    return definition


def check_registration(state, previous, target):
    if state["registered"] and (
        previous is None or state.get("path") != str(target)
    ):
        raise SystemExit("同名任务已注册但没有可核验的项目配置，未修改。")


def wait_for_removal(service):
    for attempt in range(RETRY_ATTEMPTS):
        if not launch_state(service)["registered"]:
            return
        if attempt + 1 < RETRY_ATTEMPTS:
            time.sleep(RETRY_INTERVAL_SECONDS)
    raise SystemExit("旧任务尚未退出，配置未覆盖；请稍后重试。")


def write_configuration(target, definition):
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(plistlib.dumps(definition))
        temporary.chmod(0o600)
        temporary.replace(target)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def install(definition, domain, service, target):
    require_timezone()
    previous = owned_configuration(target)
    state = launch_state(service)
    check_registration(state, previous, target)
    if state["registered"] and previous == definition:
        return
    if state["registered"]:
        subprocess.run(["launchctl", "bootout", service], capture_output=True)
        wait_for_removal(service)
    write_configuration(target, definition)
    for attempt in range(RETRY_ATTEMPTS):
        result = subprocess.run(
            ["launchctl", "bootstrap", domain, str(target)], capture_output=True
        )
        if not result.returncode:
            return
        if attempt + 1 < RETRY_ATTEMPTS:
            time.sleep(RETRY_INTERVAL_SECONDS)
    raise SystemExit("配置已保存，但任务注册失败；请检查 macOS 登录会话。")


def stop(service, target):
    previous = owned_configuration(target)
    state = launch_state(service)
    check_registration(state, previous, target)
    if state["registered"]:
        subprocess.run(["launchctl", "bootout", service], capture_output=True)
        wait_for_removal(service)
    target.unlink(missing_ok=True)


def status(service, target):
    state = launch_state(service)
    try:
        definition = owned_configuration(target)
        check_registration(state, definition, target)
        owned = definition is not None
    except SystemExit:
        definition, owned = None, False
    timezone = system_timezone()
    schedule = (definition or {}).get("StartCalendarInterval")
    expected_schedule = schedule == {"Hour": 16, "Minute": 0}
    next_run = None
    if state["registered"] and owned and expected_schedule and timezone == TIMEZONE:
        now = datetime.now(ZoneInfo(TIMEZONE))
        upcoming = now.replace(hour=16, minute=0, second=0, microsecond=0)
        if upcoming <= now:
            upcoming += timedelta(days=1)
        next_run = upcoming.isoformat()
    return {
        "label": LABEL,
        "registered": state["registered"],
        "owned_configuration": owned,
        "system_timezone": timezone,
        "schedule": "daily 16:00 Asia/Shanghai" if expected_schedule else None,
        "next_scheduled_at": next_run,
        **{key: state[key] for key in ("state", "pid", "runs", "last_exit_code") if key in state},
    }


def executable_path():
    directories = []
    for command in REQUIRED_COMMANDS:
        found = shutil.which(command)
        if not found or not Path(found).is_absolute():
            raise SystemExit(f"未找到可执行的 {command}，定时任务未安装。")
        directory = str(Path(found).parent)
        if directory not in directories:
            directories.append(directory)
    # Node-based CLIs may require node through /usr/bin/env even when their
    # launchers are symlinks. Keep its executable directory in the launchd PATH.
    node = shutil.which("node")
    if node and Path(node).is_absolute() and str(Path(node).parent) not in directories:
        directories.append(str(Path(node).parent))
    directories.extend(path for path in ("/opt/homebrew/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin")
                       if path not in directories)
    return ":".join(directories)


def make_definition():
    python = ROOT / ".venv" / "bin" / "python"
    script = ROOT / "scripts" / "run_tls_error_report.py"
    if not python.is_file() or not os.access(python, os.X_OK) or not script.is_file():
        raise SystemExit("缺少项目 Python 环境或错误日志日报脚本，定时任务未安装。")
    path = executable_path()
    runtime = ROOT / ".data" / "tls-error-reports"
    runtime.mkdir(parents=True, exist_ok=True, mode=0o700)
    return {
        "Label": LABEL,
        "ProgramArguments": [str(python), str(script)],
        "WorkingDirectory": str(ROOT),
        "EnvironmentVariables": {
            "PYTHONUNBUFFERED": "1", "PATH": path,
            "LARKSUITE_CLI_NO_UPDATE_NOTIFIER": "1",
            "LARKSUITE_CLI_NO_SKILLS_NOTIFIER": "1",
        },
        "StartCalendarInterval": {"Hour": 16, "Minute": 0},
        "RunAtLoad": False,
        "ProcessType": "Background",
        "StandardOutPath": str(runtime / "schedule.stdout.log"),
        "StandardErrorPath": str(runtime / "schedule.stderr.log"),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["install", "status", "stop"])
    args = parser.parse_args()
    if sys.platform != "darwin":
        raise SystemExit("此脚本仅管理 macOS 当前用户 LaunchAgent。")
    domain = f"gui/{os.getuid()}"
    service = f"{domain}/{LABEL}"
    target = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
    if args.action == "status":
        print(json.dumps(status(service, target), ensure_ascii=False))
        return
    if args.action == "stop":
        stop(service, target)
        print(json.dumps({"label": LABEL, "status": "stopped"}, ensure_ascii=False))
        return
    require_timezone()
    install(make_definition(), domain, service, target)
    print(json.dumps(status(service, target), ensure_ascii=False))


if __name__ == "__main__":
    main()
