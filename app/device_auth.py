import json
import math
from datetime import timedelta

from sqlalchemy import select

from app.errors import AppError, FeishuError
from app.models import BrowserSession, DeviceAuthorization, utcnow


class DeviceAuthService:
    def __init__(self, sessions, security, feishu):
        self.sessions, self.security, self.feishu = sessions, security, feishu

    def start(self, session_hash):
        with self.sessions.begin() as db:
            # Serialize starts in one browser, including the first request.
            db.scalar(
                select(BrowserSession)
                .where(BrowserSession.token_hash == session_hash)
                .with_for_update()
            )
            data = self.feishu.start_device_authorization()
            grant = db.get(DeviceAuthorization, session_hash)
            if not grant:
                grant = DeviceAuthorization(session_hash=session_hash)
                db.add(grant)
            grant.device_code = self.security.encrypt(data["device_code"])
            grant.exchanged_token = None
            grant.status = "pending"
            grant.interval = max(5, int(data.get("interval", 5)))
            grant.next_poll_at = utcnow() + timedelta(seconds=grant.interval)
            grant.expires_at = utcnow() + timedelta(seconds=int(data["expires_in"]))
            return {
                "status": "pending",
                "verification_url": data["verification_url"],
                "user_code": data["user_code"],
                "expires_at": grant.expires_at,
                "interval": grant.interval,
            }

    def poll(self, session_hash):
        with self.sessions() as db:
            result = self._poll(db, session_hash)
            db.commit()
            return result

    def _poll(self, db, session_hash):
        grant = db.scalar(
            select(DeviceAuthorization)
            .where(DeviceAuthorization.session_hash == session_hash)
            .with_for_update()
        )
        if not grant:
            raise AppError("device_not_started", "请先点击连接飞书。", 409)
        if grant.status != "pending":
            return {"status": grant.status}
        if grant.expires_at <= utcnow():
            return self.finish(grant, "expired")
        if grant.next_poll_at > utcnow():
            return {
                "status": "pending",
                "interval": max(1, math.ceil((grant.next_poll_at - utcnow()).total_seconds())),
            }
        grant.next_poll_at = utcnow() + timedelta(seconds=grant.interval)
        try:
            if not grant.exchanged_token:
                token = self.feishu.poll_device_token(self.security.decrypt(grant.device_code))
                grant.exchanged_token = exchanged = self.security.encrypt(json.dumps(token))
                grant.device_code = None
                # Persist the one-use token exchange before any subsequent API call.
                db.commit()
                grant = db.scalar(
                    select(DeviceAuthorization)
                    .where(DeviceAuthorization.session_hash == session_hash)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
                if not grant or grant.status != "pending" or grant.exchanged_token != exchanged:
                    return {"status": "expired"}
            token = json.loads(self.security.decrypt(grant.exchanged_token))
            profile = self.feishu.profile(token["access_token"])
            self.security.save_account(profile, token)
        except FeishuError as exc:
            if exc.provider_code == "slow_down":
                grant.interval += 5
                grant.next_poll_at = utcnow() + timedelta(seconds=grant.interval)
            elif exc.provider_code == "access_denied":
                return self.finish(grant, "denied")
            elif exc.provider_code in {"expired_token", "invalid_grant"}:
                return self.finish(grant, "expired")
            elif exc.provider_code not in {
                "authorization_pending",
                "network",
                "429",
                "99991400",
                "500",
                "502",
                "503",
                "504",
            }:
                return self.finish(grant, "failed", exc.code)
            return {"status": "pending", "interval": grant.interval}
        except AppError as exc:
            return self.finish(grant, "failed", exc.code)
        return self.finish(grant, "connected")

    @staticmethod
    def finish(grant, status, error_code=None):
        grant.status = status
        grant.device_code = None
        grant.exchanged_token = None
        return {"status": status, **({"error_code": error_code} if error_code else {})}
