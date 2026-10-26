"""Synthetic sessions only; no network access or real sleeping."""

import hashlib
import json

import pytest
import requests

from src.etl.http_session import BoundedSession, HTTPPolicyError


class FakeClock:
    def __init__(self):
        self.now = 100.0
        self.sleeps = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class Response:
    def __init__(self, status_code):
        self.status_code = status_code
        self.closed = False

    def close(self):
        self.closed = True


class Session:
    def __init__(self, clock, results):
        self.clock = clock
        self.results = iter(results)
        self.calls = []

    def get(self, url, **kwargs):
        return self._send("GET", url, kwargs)

    def post(self, url, **kwargs):
        return self._send("POST", url, kwargs)

    def _send(self, method, url, kwargs):
        self.calls.append((self.clock(), method, url, kwargs))
        result = next(self.results)
        if isinstance(result, Exception):
            raise result
        return result


def wrapper(tmp_path, clock, results, **kwargs):
    session = Session(clock, results)
    return BoundedSession(session=session, cache_dir=tmp_path,
                          clock=clock, sleep=clock.sleep, **kwargs), session


def test_retry_status_closes_failed_responses_and_forwards_kwargs(tmp_path):
    clock = FakeClock()
    a, b, done = Response(502), Response(504), Response(200)
    client, session = wrapper(tmp_path, clock, [a, b, done])
    opts = dict(stream=True, verify=True, timeout=(3, 10), allow_redirects=False,
                headers={"Authorization": "secret"})
    assert client.post("https://example.org/api?token=secret", data="body", **opts) is done
    assert [call[0] for call in session.calls] == pytest.approx([100, 101.1, 103.1])
    assert all(call[3] == {**opts, "data": "body"} for call in session.calls)
    assert a.closed and b.closed and not done.closed


@pytest.mark.parametrize("status", [403, 429, 500])
def test_forbidden_and_other_statuses_not_retried(tmp_path, status):
    clock = FakeClock()
    response = Response(status)
    client, session = wrapper(tmp_path, clock, [response])
    assert client.get("https://example.org") is response
    assert not response.closed and len(session.calls) == 1


@pytest.mark.parametrize("error,retry", [
    (requests.exceptions.Timeout(), True),
    (requests.exceptions.ConnectionError(), True),
    (requests.exceptions.SSLError(), False),
])
def test_transport_error_policy(tmp_path, error, retry):
    clock = FakeClock()
    done = Response(200)
    client, session = wrapper(tmp_path, clock, [error, done])
    if retry:
        assert client.get("https://example.org") is done
        assert len(session.calls) == 2
    else:
        with pytest.raises(requests.exceptions.SSLError):
            client.get("https://example.org")
        assert len(session.calls) == 1


def test_exhaustion_and_journal_redaction(tmp_path):
    clock = FakeClock()
    journal = tmp_path / "audit.jsonl"
    client, session = wrapper(tmp_path, clock, [Response(503)] * 3, journal_path=journal)
    result = client.get("https://user:password@example.org/secret/path?api_key=secret",
                        params={"token": "hidden"})
    assert result.status_code == 503
    assert len(session.calls) == 3
    entries = [json.loads(line) for line in journal.read_text().splitlines()]
    assert [entry["retry_delay"] for entry in entries] == [1, 2, 0]
    assert [entry["attempt"] for entry in entries] == [1, 2, 3]
    assert all(entry["url"] == "https://example.org/[redacted]" for entry in entries)
    for secret in ("password", "api_key", "token", "hidden", "secret/path"):
        assert secret not in journal.read_text()


def test_journal_transport_error_and_stream_failure_after_return(tmp_path):
    clock = FakeClock()
    journal = tmp_path / "audit.jsonl"

    class BrokenStream(Response):
        def iter_content(self, chunk_size):
            raise requests.exceptions.Timeout("stream timed out")

    stream = BrokenStream(200)
    client, session = wrapper(tmp_path, clock,
                              [requests.exceptions.Timeout(), stream], journal_path=journal)
    assert client.get("https://example.org", stream=True) is stream
    with pytest.raises(requests.exceptions.Timeout):
        list(stream.iter_content(1024))
    assert len(session.calls) == 2
    entries = [json.loads(line) for line in journal.read_text().splitlines()]
    assert [entry["error_class"] for entry in entries] == ["Timeout", None]


def test_offline_refuses_without_touching_session(tmp_path):
    clock = FakeClock()
    client, session = wrapper(tmp_path, clock, [], offline=True)
    for method in (client.get, client.post):
        with pytest.raises(requests.exceptions.RequestException):
            method("https://example.org")
    assert not session.calls


def test_failed_journal_closes_success_response_and_fails_explicitly(tmp_path, monkeypatch):
    clock = FakeClock()
    response = Response(200)
    client, session = wrapper(tmp_path, clock, [response])

    def unavailable(*args):
        raise OSError("journal disk unavailable")

    monkeypatch.setattr(client, "_journal", unavailable)
    with pytest.raises(HTTPPolicyError, match="journal unavailable"):
        client.get("https://example.org")
    assert response.closed and len(session.calls) == 1


@pytest.mark.parametrize("kwargs", [{"offline": "false"}, {"max_attempts": True}, {"min_interval": True}])
def test_constructor_rejects_ambiguous_policy_values(tmp_path, kwargs):
    with pytest.raises(ValueError):
        BoundedSession(cache_dir=tmp_path, **kwargs)


def test_persistent_rate_state_across_instances_and_hosts(tmp_path):
    clock = FakeClock()
    first, _ = wrapper(tmp_path, clock, [Response(200)])
    second, other = wrapper(tmp_path, clock, [Response(200), Response(200)])
    first.get("https://example.org/one")
    second.get("https://example.org/two")
    second.get("https://other.org/")
    assert [call[0] for call in other.calls] == pytest.approx([101.1, 101.1])


def test_existing_lock_is_not_stolen_and_corrupt_or_future_state_fails_closed(tmp_path):
    clock = FakeClock()
    client, session = wrapper(tmp_path, clock, [Response(200)])
    key = hashlib.sha256(b"example.org").hexdigest()
    lock = tmp_path / f"{key}.lock"
    state = tmp_path / f"{key}.json"
    tmp_path.mkdir(exist_ok=True)
    lock.write_text("other-owner")
    with pytest.raises(HTTPPolicyError, match="lock unavailable"):
        client.get("https://example.org")
    assert lock.read_text() == "other-owner"
    lock.unlink()
    for contents in ('{bad', json.dumps({"last_request": 9999})):
        state.write_text(contents)
        with pytest.raises(HTTPPolicyError):
            client.get("https://example.org")
        assert not lock.exists()
    assert not session.calls
