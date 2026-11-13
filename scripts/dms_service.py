"""Prepare and manage the separate localhost DMS detail service on macOS."""

import argparse
import os
import secrets
import shutil
import subprocess
import sys
from pathlib import Path

from model_service import install

ROOT = Path(__file__).resolve().parents[1]
LABEL = "com.octopus.assistant.dms"


def prepare():
    runtime = ROOT / ".data/dms-runtime"
    runtime.mkdir(parents=True, exist_ok=True, mode=0o700)
    token = ROOT / ".data/dms-token"
    try:
        descriptor = os.open(token, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        if not token.is_file() or token.is_symlink():
            raise SystemExit("DMS 令牌路径不是普通文件，未修改。") from None
    else:
        with os.fdopen(descriptor, "w") as stream:
            stream.write(secrets.token_hex(32) + "\n")
    return runtime


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["prepare", "start", "install", "status", "stop"])
    parser.add_argument("--profile", default="", help="Existing Alibaba Cloud CLI profile")
    parser.add_argument("--tenant-id", default="", help="Optional DMS tenant ID")
    args = parser.parse_args()
    if args.action == "prepare":
        prepare()
        print("DMS 本机服务令牌与运行目录已准备。")
        return
    if sys.platform != "darwin":
        raise SystemExit("其他系统请先 prepare，再用服务管理器运行 python -m app.dms_host。")
    domain = f"gui/{os.getuid()}"
    service = f"{domain}/{LABEL}"
    target = Path.home() / "Library/LaunchAgents" / f"{LABEL}.plist"
    if args.action == "status":
        result = subprocess.run(["launchctl", "print", service], capture_output=True, text=True)
        if result.returncode:
            print("DMS 详情服务未启动。")
        else:
            print("\n".join(
                line.strip() for line in result.stdout.splitlines()
                if line.strip().startswith(("state =", "pid =", "last exit code ="))
            ))
        return
    if args.action == "stop":
        result = subprocess.run(["launchctl", "bootout", service], capture_output=True)
        if result.returncode:
            raise SystemExit("DMS 详情服务未运行或无法停止。")
        if target.exists():
            target.unlink()
        print("DMS 详情服务已停止并取消登录时启动。")
        return
    binary = shutil.which("aliyun")
    python = ROOT / ".venv/bin/python"
    if not binary or not python.is_file():
        raise SystemExit("请先安装阿里云 CLI 并运行 uv sync。")
    runtime = prepare()
    definition = {
        "Label": LABEL,
        "ProgramArguments": [str(python), "-m", "app.dms_host"],
        "WorkingDirectory": str(ROOT),
        "EnvironmentVariables": {
            "DMS_ALIYUN_BINARY": binary,
            "DMS_ALIYUN_PROFILE": args.profile,
            "DMS_TENANT_ID": args.tenant_id,
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
    print("DMS 详情服务已启动；使用本机阿里云 CLI 登录，只读获取工单资料。")


if __name__ == "__main__":
    main()
