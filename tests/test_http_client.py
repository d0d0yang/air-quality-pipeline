"""
test_http_client.py

Retries, Retry-After and the "don't leak signed urls" rule.
"""

import pytest
import responses

from pipeline import config, http_client

URL = "https://example.test/data"


@responses.activate
def test_retries_on_503_then_succeeds(no_sleep):
    responses.get(URL, status=503)
    responses.get(URL, json={"ok": True})
    assert http_client.get(URL).json() == {"ok": True}
    assert no_sleep == [1]


@responses.activate
def test_honours_retry_after(no_sleep):
    responses.get(URL, status=429, headers={"Retry-After": "7"})
    responses.get(URL, json={})
    http_client.get(URL)
    assert no_sleep == [7]


@responses.activate
def test_client_error_is_not_retried():
    responses.get(URL, status=404)
    with pytest.raises(http_client.HttpError):
        http_client.get(URL)
    assert len(responses.calls) == 1


@responses.activate
def test_allowed_status_is_returned():
    responses.get(URL, status=304)
    assert http_client.get(URL, allow_status=(304,)).status_code == 304


@responses.activate
def test_error_message_drops_query_string():
    responses.get(URL, status=403)
    with pytest.raises(http_client.HttpError) as err:
        http_client.get(URL, params={"sig": "secret-ish"})
    assert "secret-ish" not in str(err.value)


@responses.activate
def test_sends_user_agent():
    responses.get(URL, json={})
    http_client.get(URL)
    assert responses.calls[0].request.headers["User-Agent"] == config.USER_AGENT
