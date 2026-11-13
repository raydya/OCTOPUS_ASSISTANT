"""Application-side client for the credential-isolated local DMS reader."""

import json
import re
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from app.dms_api import MAX_DETAIL_BYTES, dms_error, order_id, validate_snapshot


def read_dms_token(path):
    try:
        with Path(path).open(encoding="utf-8") as stream:
            token = stream.read(129).strip()
        if not re.fullmatch(r"[0-9a-f]{64}", token):
            raise ValueError("Invalid token")
        return token
    except (OSError, UnicodeError, ValueError):
        raise dms_error("dms_details_configuration", 503) from None


class DmsDetailClient:
    def __init__(self, settings, *, transport=None):
        self.settings = settings
        self.http = httpx.Client(
            timeout=settings.dms_details_timeout_seconds, transport=transport,
            follow_redirects=False, trust_env=False,
        )

    def fetch(self, identifier):
        if not self.settings.dms_details_enabled:
            raise dms_error("dms_details_disabled", 503)
        identifier = order_id(identifier)
        origin = self.settings.dms_details_url.rstrip("/")
        try:
            parsed = urlsplit(origin)
            if (
                parsed.scheme not in {"http", "https"}
                or parsed.hostname not in {"localhost", "127.0.0.1", "::1", "host.docker.internal"}
                or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment
                or parsed.port == 0
            ):
                raise ValueError("Invalid origin")
        except ValueError:
            raise dms_error("dms_details_configuration", 503) from None
        token = read_dms_token(self.settings.dms_details_token_file)
        try:
            with self.http.stream(
                "POST", f"{origin}/details", json={"order_id": identifier},
                headers={"Authorization": f"Bearer {token}"},
            ) as response:
                if response.status_code != 200:
                    code = {
                        429: "dms_details_busy", 504: "dms_details_timeout",
                        503: "dms_details_configuration",
                    }.get(response.status_code, "dms_details_unavailable")
                    raise dms_error(code)
                data = bytearray()
                for chunk in response.iter_bytes():
                    data.extend(chunk)
                    if len(data) > MAX_DETAIL_BYTES:
                        raise dms_error("dms_details_invalid_response")
                return validate_snapshot(json.loads(data), identifier)
        except httpx.TimeoutException:
            raise dms_error("dms_details_timeout", 504) from None
        except httpx.HTTPError:
            raise dms_error() from None
        except (ValueError, TypeError, UnicodeError, RecursionError):
            raise dms_error("dms_details_invalid_response") from None

    def close(self):
        self.http.close()
