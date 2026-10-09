"""Thin client for the Jira Assets REST API (cloud)."""
from __future__ import annotations

import base64
import json
import socket
import time
import urllib.error
import urllib.parse
import urllib.request

RETRY_STATUS = {429, 502, 503, 504}


class ApiError(Exception):
    def __init__(self, status, body, msg=""):
        self.status, self.body = status, body
        super().__init__(msg or f"HTTP {status}: {str(body)[:300]}")


class TransportError(ApiError):
    """Network failure — the request may or may not have reached the server."""

    def __init__(self, reason):
        super().__init__(None, None, f"network error: {reason}")


class Client:
    def __init__(self, cfg, opener=urllib.request.urlopen, sleep=time.sleep, retries=4):
        self.cfg = cfg
        self._open, self._sleep, self.retries = opener, sleep, retries
        self._auth = "Basic " + base64.b64encode(f"{cfg.email}:{cfg.token}".encode()).decode()
        self._workspace = cfg.workspace_id
        self._users = {}

    # ── transport ──
    def request(self, method, url, body=None):
        """Return (status, parsed body). Retries 429/5xx; retries network errors only for idempotent methods."""
        data = json.dumps(body).encode() if body is not None else None
        for attempt in range(self.retries + 1):
            req = urllib.request.Request(url, method=method, data=data, headers={
                "Authorization": self._auth, "Content-Type": "application/json", "Accept": "application/json"})
            try:
                with self._open(req, timeout=120) as r:
                    return r.status, _parse(r.read())
            except urllib.error.HTTPError as e:
                status, parsed = e.code, _parse(e.read())
                if status in RETRY_STATUS and attempt < self.retries:
                    self._sleep(_retry_after(e, attempt))
                    continue
                return status, parsed
            except (urllib.error.URLError, socket.timeout, ConnectionError) as e:
                if method in ("GET", "PUT", "DELETE") and attempt < self.retries:
                    self._sleep(2 ** attempt)
                    continue
                raise TransportError(getattr(e, "reason", e)) from e

    def call(self, method, path, body=None, ok=(200, 201, 204)):
        st, d = self.request(method, self.api + path, body)
        if st not in ok:
            raise ApiError(st, d)
        return d

    # ── endpoints ──
    @property
    def workspace(self):
        if not self._workspace:
            st, d = self.request("GET", f"{self.cfg.site}/rest/servicedeskapi/assets/workspace")
            values = (d or {}).get("values") if isinstance(d, dict) else None
            if st != 200 or not values:
                raise ApiError(st, d, f"cannot discover Assets workspace id (HTTP {st}); set ASSETS_WORKSPACE_ID")
            self._workspace = values[0]["workspaceId"]
        return self._workspace

    @property
    def api(self):
        return f"https://api.atlassian.com/jsm/assets/workspace/{self.workspace}/v1"

    def myself(self):
        """Current user; raises ApiError(401) for a wrong email/token."""
        st, d = self.request("GET", f"{self.cfg.site}/rest/api/3/myself")
        if st != 200:
            raise ApiError(st, d)
        return d

    def schemas(self):
        out, start = [], 0
        while True:
            d = self.call("GET", f"/objectschema/list?startAt={start}&maxResults=50")
            values = d.get("values") or []
            out += values
            start += len(values)
            if d.get("isLast", True) or not values or start >= d.get("total", start):
                return out

    def object_types(self, schema):
        return self.call("GET", f"/objectschema/{schema}/objecttypes/flat")

    def attributes(self, type_id):
        return self.call("GET", f"/objecttype/{type_id}/attributes")

    def aql(self, query, start=0, limit=100):
        return self.call("POST", f"/object/aql?startAt={start}&maxResults={limit}&includeAttributes=true", {"qlQuery": query})

    def aql_all(self, query):
        out, start = [], 0
        while True:
            d = self.aql(query, start)
            values = d.get("values") or []
            out += values
            start += len(values)
            if d.get("isLast", True) or not values:
                return out

    def get_object(self, oid):
        """Object dict, or None if it does not exist."""
        st, d = self.request("GET", f"{self.api}/object/{oid}")
        if st == 404:
            return None
        if st != 200:
            raise ApiError(st, d)
        return d

    def create_object(self, type_id, attrs):
        return self.call("POST", "/object/create", {"objectTypeId": type_id, "attributes": attrs})

    def update_object(self, oid, type_id, attrs):
        return self.call("PUT", f"/object/{oid}", {"objectTypeId": type_id, "attributes": attrs})

    def delete_object(self, oid):
        return self.call("DELETE", f"/object/{oid}")

    def account_id(self, email):
        key = email.lower()
        if key not in self._users:
            st, users = self.request("GET", f"{self.cfg.site}/rest/api/3/user/search?query={urllib.parse.quote(email)}")
            if st != 200:
                raise ApiError(st, users)
            match = [u for u in users or [] if (u.get("emailAddress") or "").lower() == key]
            if len(match) != 1:
                raise ValueError(f"user '{email}': {'not found' if not match else 'ambiguous'} in Jira")
            self._users[key] = match[0]["accountId"]
        return self._users[key]


def _parse(raw):
    raw = raw.decode() if isinstance(raw, bytes) else raw
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return raw


def _retry_after(err, attempt):
    try:
        return min(float(err.headers.get("Retry-After")), 60)
    except (TypeError, ValueError, AttributeError):
        return 2 ** attempt
