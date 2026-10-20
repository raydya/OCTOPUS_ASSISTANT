"""Manage the project's local inference service on macOS; never copy login secrets."""

import argparse
import os
import plistlib
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LABEL = "com.octopus.assistant.model"
RETRY_ATTEMPTS = 40
RETRY_INTERVAL_SECONDS = 0.25


def registered(service):
    return subprocess.run(
        ["launchctl", "print", service], capture_output=True
    ).returncode == 0


def wait_for_removal(service):
    for attempt in range(RETRY_ATTEMPTS):
        if not registered(service):
            return
        if attempt + 1 < RETRY_ATTEMPTS:
            time.sleep(RETRY_INTERVAL_SECONDS)
    raise SystemExit("旧模型服务尚未退出，配置未覆盖；请稍后重试安装。")


def bootstrap(domain, target):
    # launchd can briefly reject a bootstrap after the previous job disappears.
    for attempt in range(RETRY_ATTEMPTS):
        result = subprocess.run(
            ["launchctl", "bootstrap", domain, str(target)], capture_output=True
        )
        if not result.returncode:
            return
        if attempt + 1 < RETRY_ATTEMPTS:
            time.sleep(RETRY_INTERVAL_SECONDS)
    raise SystemExit("服务配置已生成，但启动失败；请检查 macOS 登录会话。")


def install(definition, domain, service, target):
    previous = None
    if target.exists():
        try:
            previous = plistlib.loads(target.read_bytes())
        except (ValueError, plistlib.InvalidFileException):
            raise SystemExit("已有同名服务配置无法识别，未覆盖。") from None
        if not isinstance(previous, dict) or previous.get("WorkingDirectory") != str(ROOT):
            raise SystemExit("已有同名服务属于其他项目，未覆盖。")
    active = registered(service)
    if active and previous is None:
        raise SystemExit("同名服务已注册但没有可核验的项目配置，未修改。")
    if active and previous == definition:
        result = subprocess.run(
            ["launchctl", "kickstart", "-k", service], capture_output=True
        )
        if not result.returncode:
            return
        if registered(service):
            raise SystemExit("模型服务已注册，但重新启动失败；请检查 macOS 登录会话。")
        # The job may have disappeared between print and kickstart.
    elif active:
        subprocess.run(["launchctl", "bootout", service], capture_output=True)
        wait_for_removal(service)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(plistlib.dumps(definition))
    target.chmod(0o600)
    bootstrap(domain, target)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["install", "status", "stop"])
    args = parser.parse_args()
    if sys.platform != "darwin":
        raise SystemExit("其他系统请使用服务管理器运行 python -m app.codex_host。")
    domain = f"gui/{os.getuid()}"
    service = f"{domain}/{LABEL}"
    target = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
    if args.action == "status":
        result = subprocess.run(["launchctl", "print", service], capture_output=True, text=True)
        if result.returncode:
            print("模型服务未启动。")
        else:
            print("\n".join(
                line.strip() for line in result.stdout.splitlines()
                if line.strip().startswith(("state =", "pid =", "last exit code ="))
            ))
        return
    if args.action == "stop":
        result = subprocess.run(["launchctl", "bootout", service], capture_output=True)
        if result.returncode:
            raise SystemExit("服务未运行或无法停止。")
        if target.exists():
            target.unlink()
        print("已停止模型服务并取消登录时启动；数据与凭证保持不变。")
        return
    binary = shutil.which("codex")
    python = ROOT / ".venv" / "bin" / "python"
    runtime = ROOT / ".data" / "model-runtime"
    if not binary or not python.is_file() or not (runtime / "home" / "config.toml").is_file():
        raise SystemExit("请先运行 uv sync 和 .venv/bin/python scripts/setup_model.py。")
    definition = {
        "Label": LABEL,
        "ProgramArguments": [str(python), "-m", "app.codex_host"],
        "WorkingDirectory": str(ROOT),
        "EnvironmentVariables": {
            "CODEX_DECISION_BINARY": binary,
            "PATH": f"{Path(binary).parent}:/usr/bin:/bin:/usr/sbin:/sbin",
        },
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 10,
        "ProcessType": "Background",
        "StandardOutPath": str(runtime / "service.stdout.log"),
        "StandardErrorPath": str(runtime / "service.stderr.log"),
    }
    install(definition, domain, service, target)
    print("已启动模型服务，并设置当前用户登录时启动：gpt-6-sol / xhigh。")


if __name__ == "__main__":
    main()
