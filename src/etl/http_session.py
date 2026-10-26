"""Bounded HTTP attempts with a persistent, per-host admission clock.

Only connection/setup failures and selected HTTP statuses are retried. Errors
while a caller consumes ``iter_content`` occur after return and are NOT retried.
An abandoned lock deliberately blocks further requests until manually resolved.
"""

import hashlib
import json
import math
import os
from pathlib import Path
import time
from urllib.parse import urlsplit
import uuid

import requests


class HTTPPolicyError(requests.exceptions.RequestException):
    """Offline or unsafe/unavailable persistent throttle state."""


class BoundedSession:
    def __init__(self, session=None, cache_dir=Path(".cache/http_session"),
                 journal_path=None, offline=False, clock=None, sleep=None,
                 min_interval=1.1, max_attempts=3):
        self.session = session if session is not None else requests.Session()
        self._owns_session = session is None
        self.cache_dir = Path(cache_dir)
        self.journal_path = Path(journal_path) if journal_path is not None else None
        self.offline = offline
        self.clock = clock if clock is not None else time.time
        self.sleep = sleep if sleep is not None else time.sleep
        if type(offline) is not bool:
            raise ValueError("offline must be bool")
        if type(min_interval) not in (int, float) or not math.isfinite(min_interval) or min_interval < 1.1:
            raise ValueError("min_interval must be at least 1.1 seconds")
        if type(max_attempts) is not int or not 1 <= max_attempts <= 3:
            raise ValueError("max_attempts must be between 1 and 3")
        self.min_interval = min_interval
        self.max_attempts = max_attempts

    def _reserve(self, host):
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        key = hashlib.sha256(host.encode("utf-8")).hexdigest()
        lock = self.cache_dir / f"{key}.lock"
        state = self.cache_dir / f"{key}.json"
        token = uuid.uuid4().hex
        for _ in range(300):
            try:
                fd = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                break
            except FileExistsError:
                self.sleep(0.1)
        else:
            raise HTTPPolicyError("Host throttle lock unavailable (manual inspection required)")
        try:
            with os.fdopen(fd, "w", encoding="ascii") as file:
                file.write(token)
            if state.exists():
                try:
                    record = json.loads(state.read_text(encoding="utf-8"))
                    last = record["last_request"]
                    if type(last) not in (float, int) or not math.isfinite(last):
                        raise ValueError("invalid timestamp")
                except (OSError, ValueError, KeyError, TypeError) as exc:
                    raise HTTPPolicyError("Corrupt host throttle state") from exc
                now = self.clock()
                if not math.isfinite(now) or last > now:
                    raise HTTPPolicyError("Future or invalid host throttle timestamp")
                delay = max(0.0, last + self.min_interval - now)
                if delay:
                    self.sleep(delay)
            now = self.clock()
            if not math.isfinite(now) or (state.exists() and now < last):
                raise HTTPPolicyError("Invalid host throttle clock")
            temporary = self.cache_dir / f"{key}.{token}.tmp"
            try:
                with temporary.open("x", encoding="utf-8") as file:
                    json.dump({"last_request": now}, file)
                    file.flush()
                    os.fsync(file.fileno())
                os.replace(temporary, state)
            finally:
                temporary.unlink(missing_ok=True)
        finally:
            if lock.read_text(encoding="ascii") == token:
                lock.unlink()
            else:
                raise HTTPPolicyError("Host throttle lock changed; manual inspection required")

    def _journal(self, method, url, attempt, status, error_class, retry_delay):
        if self.journal_path is None:
            return
        # Never log URL userinfo, paths, fragments, query strings, params or bodies.
        parsed = urlsplit(url)
        safe_url = f"{parsed.scheme}://{parsed.hostname}/[redacted]"
        entry = {"timestamp": self.clock(), "method": method, "url": safe_url,
                 "attempt": attempt, "status": status, "error_class": error_class,
                 "retry_delay": retry_delay}
        self.journal_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.journal_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, (json.dumps(entry) + "\n").encode("utf-8"))
        finally:
            os.close(fd)

    def request(self, method, url, **kwargs):
        if self.offline:
            raise HTTPPolicyError("Offline mode forbids HTTP requests")
        method = method.upper()
        if method not in ("GET", "POST"):
            raise ValueError("Only GET and POST are supported")
        parsed = urlsplit(url)
        if parsed.scheme.lower() not in ("http", "https") or not parsed.hostname:
            raise HTTPPolicyError("HTTP(S) URL with hostname required")
        host = parsed.hostname.lower()
        for attempt in range(1, self.max_attempts + 1):
            self._reserve(host)
            try:
                response = getattr(self.session, method.lower())(url, **kwargs)
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as exc:
                retry = (attempt < self.max_attempts and
                         not isinstance(exc, requests.exceptions.SSLError))
                delay = 2 ** (attempt - 1) if retry else 0
                try:
                    self._journal(method, url, attempt, None, type(exc).__name__, delay)
                except OSError as journal_error:
                    raise HTTPPolicyError("Request journal unavailable; acquisition stopped") from journal_error
                if not retry:
                    raise
            else:
                retry = response.status_code in (502, 503, 504) and attempt < self.max_attempts
                delay = 2 ** (attempt - 1) if retry else 0
                if retry:
                    response.close()
                try:
                    self._journal(method, url, attempt, response.status_code, None, delay)
                except OSError as journal_error:
                    # Acquisition is auditable-or-failed, not silently unlogged.
                    # The caller never receives this response, so we must close it.
                    if not retry:
                        response.close()
                    raise HTTPPolicyError("Request journal unavailable; acquisition stopped") from journal_error
                if not retry:
                    return response
            self.sleep(delay)

    def get(self, url, **kwargs):
        return self.request("GET", url, **kwargs)

    def post(self, url, **kwargs):
        return self.request("POST", url, **kwargs)

    def close(self):
        if self._owns_session:
            self.session.close()
