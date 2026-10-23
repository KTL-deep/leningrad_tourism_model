"""Offline contract checks for the bounded source-probe CLI."""

import hashlib
import json

import pytest
import requests

from scripts import probe_sources


SOURCE = {
    "source_id": "sample", "owner": "example", "url": "https://example.org/index",
    "coverage": "index", "categories": ["sample"],
    "known_conditions": "unknown", "known_gaps": "not dataset validation",
    "params": {"page": 1},
}


class Response:
    def __init__(self, body=b"ok", status=200, content_type="text/html", url="https://example.org/final"):
        self.body = body
        self.status_code = status
        self.headers = {"Content-Type": content_type}
        self.url = url

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def iter_content(self, chunk_size):
        for index in range(0, len(self.body), chunk_size):
            yield self.body[index:index + chunk_size]


class Session:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def test_complete_json_and_request_options():
    body = b'{"items": [1, 2], "next": null}'
    session = Session(Response(body, content_type="application/json"))
    result = probe_sources.probe(SOURCE, session)
    assert result["status"] == "http_ok"
    assert result["final_url"] == "https://example.org/final"
    assert result["http_status"] == 200
    assert result["content_type"] == "application/json"
    assert result["body_sha256"] == hashlib.sha256(body).hexdigest()
    assert result["captured_bytes"] == len(body)
    assert result["truncated"] is False
    assert result["top_level_keys"] == ["items", "next"]
    assert result["element_count"] is None
    assert result["checked_at"].endswith("+00:00")
    assert all(result[key] == SOURCE[key] for key in
               ("source_id", "owner", "url", "coverage", "categories", "known_conditions", "known_gaps"))
    url, options = session.calls[0]
    assert url == SOURCE["url"]
    assert options["params"] == {"page": 1}
    assert options["timeout"] == (10, 25)
    assert options["verify"] is True and options["stream"] is True
    assert "github.com/KTL-deep/leningrad_tourism_model" in options["headers"]["User-Agent"]


def test_array_count_and_invalid_json_do_not_claim_zero():
    array = probe_sources.probe(SOURCE, Session(Response(b"[1,2]", content_type="application/json")))
    assert array["element_count"] == 2 and array["top_level_keys"] is None
    invalid = probe_sources.probe(SOURCE, Session(Response(b"{", content_type="application/json")))
    assert invalid["element_count"] is None and invalid["top_level_keys"] is None
    html = probe_sources.probe(SOURCE, Session(Response(b"<html>no data</html>")))
    assert html["element_count"] is None


def test_truncated_response_hashes_only_captured_bytes():
    body = b"x" * (probe_sources.MAX_BYTES + 1)
    result = probe_sources.probe(SOURCE, Session(Response(body, content_type="application/json")))
    assert result["captured_bytes"] == probe_sources.MAX_BYTES
    assert result["truncated"] is True
    assert result["body_sha256"] == hashlib.sha256(body[:probe_sources.MAX_BYTES]).hexdigest()
    assert result["element_count"] is None


@pytest.mark.parametrize("status,expected", [(403, "access_denied"), (429, "rate_limited"), (500, "http_error")])
def test_http_failures(status, expected):
    result = probe_sources.probe(SOURCE, Session(Response(status=status)))
    assert result["status"] == expected
    assert result["http_status"] == status


def test_js_challenge_not_ready():
    result = probe_sources.probe(SOURCE, Session(Response(
        b'<script>document.cookie="beget=begetok";location.reload()</script>')))
    assert result["status"] == "access_challenge"
    assert result["element_count"] is None


def test_legitimate_page_with_captcha_form_is_not_an_access_challenge():
    result = probe_sources.probe(SOURCE, Session(Response(
        b'<html><h1>Open dataset passport</h1><form id="captcha">Feedback</form></html>')))
    assert result["status"] == "http_ok"


def test_network_error_preserves_error_class_and_no_count():
    result = probe_sources.probe(SOURCE, Session(requests.Timeout("read timed out")))
    assert result["status"] == "error"
    assert result["error_class"] == "Timeout"
    assert "read timed out" in result["error_detail"]
    assert result["http_status"] is None
    assert result["element_count"] is None


def test_rate_limit_same_domain():
    times = iter([0, 0.25, 1])
    sleeps = []
    last = {}
    session = Session(Response())
    probe_sources.probe(SOURCE, session, last, clock=lambda: next(times), sleep=sleeps.append)
    probe_sources.probe(SOURCE, session, last, clock=lambda: next(times), sleep=sleeps.append)
    assert sleeps == [0.75]


def test_cli_selection_and_exclusive_output(tmp_path):
    output = tmp_path / "audit" / "probes.json"
    session = Session(Response())
    assert probe_sources.main(["--output", str(output), "--source", "egrkn"], session=session) == 0
    data = json.loads(output.read_text(encoding="utf-8"))
    assert data["schema_version"] == 1
    assert [probe["source_id"] for probe in data["probes"]] == ["egrkn"]
    with pytest.raises(FileExistsError):
        probe_sources.main(["--output", str(output)], session=session)
    assert len(session.calls) == 1
