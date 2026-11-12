from app.core.errors import ApiError, error_body


def test_retryable_error_includes_retryable_flag():
    body = error_body(
        ApiError(
            409,
            "TICKET_REVERSAL_CONFLICT",
            "Ticket state changed, retry the reversal",
            retryable=True,
            details={"ticket_id": "abc"},
        )
    )

    assert body == {
        "error": {
            "code": "TICKET_REVERSAL_CONFLICT",
            "message": "Ticket state changed, retry the reversal",
            "details": {"ticket_id": "abc"},
            "retryable": True,
        }
    }


def test_non_retryable_error_omits_retryable_flag():
    body = error_body(ApiError(409, "TICKET_NOT_CHECKED_IN", "Ticket is not checked in"))

    assert body == {
        "error": {
            "code": "TICKET_NOT_CHECKED_IN",
            "message": "Ticket is not checked in",
            "details": {},
        }
    }
    assert "retryable" not in body["error"]
