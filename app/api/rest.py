"""Thin, typed wrapper over the BlueBubbles REST API.
All routes live here so a server-side rename is a one-line fix.
httpx.Client is thread-safe; one instance is shared across worker threads."""
import logging
import mimetypes
from pathlib import Path

import httpx

log = logging.getLogger(__name__)


class ApiError(Exception):
    """Carries a human-readable message safe to show in the UI."""

    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class BBClient:
    def __init__(self, base_url: str, password: str):
        self.base = base_url.rstrip("/")
        self.password = password
        self._c = httpx.Client(
            timeout=httpx.Timeout(connect=5, read=20, write=300, pool=5),
            follow_redirects=True,
            transport=httpx.HTTPTransport(retries=2),
        )

    def close(self):
        try:
            self._c.close()
        except Exception:
            pass

    # ---------- plumbing ----------

    def _url(self, path: str) -> str:
        return f"{self.base}{path}"

    def _unwrap(self, r: httpx.Response):
        if r.status_code == 401 or r.status_code == 403:
            raise ApiError("Server rejected the password.", r.status_code)
        try:
            body = r.json()
        except Exception:
            if r.status_code >= 400:
                raise ApiError(f"Server error (HTTP {r.status_code}).",
                               r.status_code)
            raise ApiError("Server sent an unexpected response.")
        if isinstance(body, dict):
            status = body.get("status", r.status_code)
            if status and int(status) >= 400:
                msg = (body.get("error") or {}).get("message") if isinstance(body.get("error"), dict) else None
                raise ApiError(
                    msg or body.get("message") or f"Server error {status}.",
                    int(status))
            return body.get("data", body)
        return body

    def _get(self, path: str, params: dict | None = None):
        p = {"password": self.password}
        if params:
            p.update(params)
        try:
            return self._unwrap(self._c.get(self._url(path), params=p))
        except httpx.HTTPError as e:
            raise ApiError(f"Cannot reach server: {e.__class__.__name__}") from e

    def _post(self, path: str, body: dict, timeout=None):
        try:
            kwargs = {"params": {"password": self.password}, "json": body}
            if timeout is not None:
                kwargs["timeout"] = timeout
            return self._unwrap(self._c.post(self._url(path), **kwargs))
        except httpx.HTTPError as e:
            raise ApiError(f"Cannot reach server: {e.__class__.__name__}") from e

    # ---------- API surface ----------

    def ping(self):
        return self._get("/api/v1/ping")

    def server_info(self):
        return self._get("/api/v1/server/info")

    def get_contacts(self) -> list:
        data = self._get("/api/v1/contact")
        return data if isinstance(data, list) else []

    def query_chats(self, limit=100, offset=0) -> list:
        data = self._post("/api/v1/chat/query", {
            "limit": limit, "offset": offset,
            "with": ["lastMessage"], "sort": "lastmessage",
        })
        return data if isinstance(data, list) else []

    def query_messages(self, chat_guid=None, limit=100, offset=0,
                       after=None, before=None, sort="DESC",
                       where: list | None = None) -> list:
        body = {"limit": limit, "offset": offset, "sort": sort,
                "with": ["chat", "attachment", "handle"]}
        if chat_guid:
            body["chatGuid"] = chat_guid
        if after is not None:
            body["after"] = int(after)
        if before is not None:
            body["before"] = int(before)
        if where:
            body["where"] = where
        data = self._post("/api/v1/message/query", body)
        return data if isinstance(data, list) else []

    def max_message_rowid(self) -> int:
        """Return a frozen upper bound from the Mac Messages database."""
        rows = self.query_messages(
            limit=1, offset=0, sort="DESC",
            where=[{
                "statement":
                    "message.ROWID = (SELECT MAX(ROWID) FROM message)",
                "args": {},
            }])
        if not rows:
            return 0
        try:
            values = [
                int(r.get("originalROWID") or r.get("ROWID") or 0)
                for r in rows if isinstance(r, dict)
            ]
        except (TypeError, ValueError):
            values = []
        maximum = max(values, default=0)
        if maximum <= 0:
            raise ApiError(
                "This BlueBubbles server does not expose message ROWIDs.",
                422)
        return maximum

    def query_messages_rowid_range(self, low: int, high: int) -> list:
        """Fetch a fixed ROWID interval; its width bounds the result count."""
        return self.query_messages(
            limit=max(1, high - low), offset=0, sort="ASC",
            where=[{
                "statement": "message.ROWID > :low AND message.ROWID <= :high",
                "args": {"low": int(low), "high": int(high)},
            }])

    def query_message_guid(self, guid: str) -> list:
        return self.query_messages(
            limit=1, offset=0,
            where=[{
                "statement": "message.guid = :guid",
                "args": {"guid": guid},
            }])

    def restart_messages_app(self) -> dict:
        """Quit and reopen Messages on the Mac (BlueBubbles runs the
        AppleScript). Relaunching forces Messages to reconnect to Apple,
        which makes Apple hand over any texts it is still holding back,
        exactly like the wake-up effect of sending a message, but nothing
        is sent to anyone. The server allows this route up to 30 seconds
        and the script itself sleeps 3, so the read timeout is extended."""
        data = self._post(
            "/api/v1/mac/imessage/restart", {},
            timeout=httpx.Timeout(connect=5, read=45, write=10, pool=5))
        return data if isinstance(data, dict) else {}

    def create_chat(self, addresses: list, message: str,
                    service: str = "iMessage") -> dict:
        method = "private-api" if len(addresses) > 1 else "apple-script"
        data = self._post("/api/v1/chat/new", {
            "addresses": addresses, "message": message,
            "service": service, "method": method})
        return data if isinstance(data, dict) else {}

    def add_participant(self, chat_guid: str, address: str) -> dict:
        data = self._post(f"/api/v1/chat/{chat_guid}/participant/add",
                          {"address": address})
        return data if isinstance(data, dict) else {}

    def remove_participant(self, chat_guid: str, address: str) -> dict:
        data = self._post(f"/api/v1/chat/{chat_guid}/participant/remove",
                          {"address": address})
        return data if isinstance(data, dict) else {}

    def rename_chat(self, chat_guid: str, name: str) -> dict:
        try:
            r = self._c.put(self._url(f"/api/v1/chat/{chat_guid}"),
                            params={"password": self.password},
                            json={"displayName": name})
            data = self._unwrap(r)
            return data if isinstance(data, dict) else {}
        except httpx.HTTPError as e:
            raise ApiError(f"Cannot reach server: {e.__class__.__name__}") from e

    def send_text(self, chat_guid: str, temp_guid: str, message: str) -> dict:
        data = self._post("/api/v1/message/text", {
            "chatGuid": chat_guid, "tempGuid": temp_guid,
            "message": message, "method": "apple-script",
        })
        return data if isinstance(data, dict) else {}

    def send_attachment(self, chat_guid: str, temp_guid: str, filepath: str) -> dict:
        p = Path(filepath)
        mime = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
        try:
            with p.open("rb") as fh:
                r = self._c.post(
                    self._url("/api/v1/message/attachment"),
                    params={"password": self.password},
                    data={"chatGuid": chat_guid, "tempGuid": temp_guid, "name": p.name},
                    files={"attachment": (p.name, fh, mime)},
                )
            data = self._unwrap(r)
            return data if isinstance(data, dict) else {}
        except httpx.HTTPError as e:
            raise ApiError(f"Upload failed: {e.__class__.__name__}") from e

    def download_attachment(self, guid: str, dest: Path) -> Path:
        tmp = dest.with_suffix(dest.suffix + ".part")
        try:
            with self._c.stream("GET", self._url(f"/api/v1/attachment/{guid}/download"),
                                params={"password": self.password}) as r:
                if r.status_code >= 400:
                    raise ApiError(f"Download failed (HTTP {r.status_code}).")
                dest.parent.mkdir(parents=True, exist_ok=True)
                with tmp.open("wb") as fh:
                    for chunk in r.iter_bytes():
                        fh.write(chunk)
            tmp.replace(dest)
            return dest
        except httpx.HTTPError as e:
            tmp.unlink(missing_ok=True)
            raise ApiError(f"Download failed: {e.__class__.__name__}") from e
