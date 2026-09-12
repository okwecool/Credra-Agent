"""Per-stage reservations for gateways with optional auxiliary model requests."""


def model_reservation(model, limits) -> tuple[int, int]:
    request_multiplier = getattr(model, "request_reservation_multiplier", 1)
    token_multiplier = getattr(model, "token_reservation_multiplier", 1)
    if (
        type(request_multiplier) is not int
        or type(token_multiplier) is not int
        or min(request_multiplier, token_multiplier) < 1
    ):
        raise ValueError("invalid model reservation profile")
    return (
        limits.model_attempt_reservation * request_multiplier,
        limits.decision_token_reservation * token_multiplier,
    )


def external_usage(result) -> int | None:
    if result.external_requests is not None:
        return result.external_requests
    return result.attempts if result.accounting_complete else None
