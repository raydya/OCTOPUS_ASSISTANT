"""Provision only this project's isolated Codex runtime; never print credentials."""

from __future__ import annotations

import os
import secrets
import shutil
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from app.approval_model import read_token  # noqa: E402
from app.codex_host import CODEX_VERSION, render_config  # noqa: E402


def setup(root: Path = PROJECT_ROOT, *, auth_source: Path | None = None, binary: str | None = None):
    auth_source = auth_source or Path.home() / ".codex" / "auth.json"
    binary = binary or os.environ.get("CODEX_DECISION_BINARY") or shutil.which("codex")
    if not binary:
        raise ValueError("Codex is unavailable")
    version = subprocess.run(
        [binary, "--version"], capture_output=True, text=True, check=True, timeout=10
    )
    if version.stdout.strip() != f"codex-cli {CODEX_VERSION}":
        raise ValueError("Unverified Codex version")
    if not auth_source.is_file() or auth_source.is_symlink():
        raise ValueError("Existing ChatGPT login is unavailable")
    auth_source = auth_source.resolve()
    data = root / ".data"
    runtime = data / "model-runtime"
    home = runtime / "home"
    workspace = runtime / "workspace"
    for directory in (data, runtime, home, workspace):
        if directory.is_symlink():
            raise ValueError("Runtime directories must not be symlinks")
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        directory.chmod(0o700)
    if any(workspace.iterdir()):
        raise ValueError("Model workspace must remain empty")
    link = home / "auth.json"
    if link.is_symlink():
        if link.resolve() != auth_source:
            raise ValueError("Unexpected authentication source")
    elif link.exists():
        raise ValueError("Authentication path is already occupied")
    else:
        link.symlink_to(auth_source)
    config = home / "config.toml"
    if config.is_symlink():
        raise ValueError("Configuration must not be a symlink")
    config.write_text(render_config(workspace), encoding="utf-8")
    config.chmod(0o600)
    token = data / "model-token"
    if token.is_symlink():
        raise ValueError("Token must not be a symlink")
    if not token.exists():
        with token.open("x", encoding="utf-8") as stream:
            stream.write(secrets.token_hex(32) + "\n")
    # The host parent directory remains 0700. Only this individual read-only file
    # is bind-mounted for the unprivileged Docker worker (uid 10001).
    token.chmod(0o644)
    read_token(token)
    return {"home": home, "workspace": workspace, "token_file": token, "binary": binary}


if __name__ == "__main__":
    try:
        setup()
    except (OSError, ValueError, subprocess.SubprocessError):
        raise SystemExit(
            "模型配置失败：请确认 Codex 0.163.0 已登录，且 .data/model-runtime 为本项目的独立目录。"
        ) from None
    print("已配置本项目的隔离模型服务：gpt-6-sol / xhigh；现有 ChatGPT 登录仅供独立模型进程使用。")
    print("启动：.venv/bin/python -m app.codex_host")
