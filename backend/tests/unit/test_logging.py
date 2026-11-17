from app.core.logging import redact_processor


def test_sensitive_fields_are_redacted() -> None:
    event = {
        "event": "x",
        "password": "hunter2",
        "access_token": "abc",
        "headers": {"Authorization": "Bearer abc.def", "accept": "json"},
        "note": "sent Bearer abc.def.ghi to provider",
        "count": 3,
    }
    out = redact_processor(None, "info", event)
    assert out["password"] == "[REDACTED]"
    assert out["access_token"] == "[REDACTED]"
    assert out["headers"]["Authorization"] == "[REDACTED]"
    assert out["headers"]["accept"] == "json"
    assert "abc.def.ghi" not in out["note"]
    assert out["count"] == 3


def test_http_client_request_urls_are_not_logged() -> None:
    """httpx logs full URLs at INFO; Meta's token calls put the app secret in them."""
    import logging

    from app.core.logging import configure_logging

    configure_logging("INFO", json_logs=True)
    assert not logging.getLogger("httpx").isEnabledFor(logging.INFO)
    assert not logging.getLogger("httpcore").isEnabledFor(logging.INFO)
