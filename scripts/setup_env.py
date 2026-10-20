"""Generate local secrets without displaying them or replacing existing configuration."""

import base64
import os
import secrets
from pathlib import Path

root = Path(__file__).resolve().parents[1]
policy = root / "approval-policy.json"
try:
    with policy.open("x") as output:
        output.write((root / "approval-policy.example.json").read_text())
except FileExistsError:
    pass
target = root / ".env"
content = (root / ".env.example").read_text()
content = content.replace("APP_PASSWORD=\n", f"APP_PASSWORD={secrets.token_urlsafe(24)}\n")
key = base64.urlsafe_b64encode(os.urandom(32)).decode()
content = content.replace("TOKEN_ENCRYPTION_KEY=\n", f"TOKEN_ENCRYPTION_KEY={key}\n")
try:
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
except FileExistsError:
    print(".env 已存在，未修改。")
else:
    with os.fdopen(fd, "w") as output:
        output.write(content)
    print("已生成 .env。请填写共享数据库的两套连接和飞书配置；登录密码见 APP_PASSWORD。")
