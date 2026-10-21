"""Manage periodic, short-lived ve session maintenance on this Mac."""

import argparse
import json
import os
import plistlib
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.tls_report_schedule import (  # noqa: E402
    check_registration,
    launch_state,
    wait_for_removal,
    write_configuration,
)

LABEL = "com.octopus.assistant.ve-session"
INTERVAL_SECONDS = 1800
REQUIRED_COMMANDS = ("ve", "node")
RETRY_ATTEMPTS = 40
RETRY_INTERVAL_SECONDS = 0.25


def owned_configuration(target):
    if target.is_symlink():
        raise SystemExit("同名任务配置是符号链接，未修改。")
    if not target.exists():
        return None
    try:
        definition = plistlib.loads(target.read_bytes())
    except (OSError, ValueError, plistlib.InvalidFileException):
        raise SystemExit("已有同名任务配置无法识别，未修改。") from None
    expected_arguments = [str(ROOT / ".venv/bin/python"), str(ROOT / "scripts/maintain_ve_session.py")]
    if (not isinstance(definition, dict) or definition.get("Label") != LABEL
            or definition.get("WorkingDirectory") != str(ROOT)
            or definition.get("ProgramArguments") != expected_arguments):
        raise SystemExit("已有同名任务属于其他项目或程序，未修改。")
    return definition


def install(definition, domain, service, target):
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
        result = subprocess.run(["launchctl", "bootstrap", domain, str(target)], capture_output=True)
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
    expected_schedule = (
        definition is not None and definition.get("StartInterval") == INTERVAL_SECONDS
        and definition.get("RunAtLoad") is True and not definition.get("KeepAlive")
        and "StartCalendarInterval" not in definition
    )
    return {
        "label": LABEL, "registered": state["registered"], "owned_configuration": owned,
        "interval_seconds": INTERVAL_SECONDS if expected_schedule else None,
        "run_at_load": bool(expected_schedule),
        "last_run_path": str(ROOT / ".data/ve-session/last-run.json"),
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
    directories.extend(path for path in ("/opt/homebrew/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin")
                       if path not in directories)
    return ":".join(directories)


def make_definition():
    python = ROOT / ".venv/bin/python"
    script = ROOT / "scripts/maintain_ve_session.py"
    if not python.is_file() or not os.access(python, os.X_OK) or not script.is_file():
        raise SystemExit("缺少项目 Python 环境或 ve 会话维护脚本，定时任务未安装。")
    path = executable_path()
    runtime = ROOT / ".data/ve-session"
    runtime.mkdir(parents=True, exist_ok=True, mode=0o700)
    return {
        "Label": LABEL,
        "ProgramArguments": [str(python), str(script)],
        "WorkingDirectory": str(ROOT),
        "EnvironmentVariables": {"PYTHONUNBUFFERED": "1", "PATH": path},
        "StartInterval": INTERVAL_SECONDS,
        "RunAtLoad": True,
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
    target = Path.home() / "Library/LaunchAgents" / f"{LABEL}.plist"
    if args.action == "status":
        print(json.dumps(status(service, target), ensure_ascii=False))
        return
    if args.action == "stop":
        stop(service, target)
        print(json.dumps({"label": LABEL, "status": "stopped"}, ensure_ascii=False))
        return
    install(make_definition(), domain, service, target)
    print(json.dumps(status(service, target), ensure_ascii=False))


if __name__ == "__main__":
    main()
