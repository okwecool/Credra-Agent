"""Non-server checks for the minimal Chainlit rendering boundary."""

from app.chainlit_app import _risk_markdown, serialize_for_debug


def test_chainlit_risk_payload_renders_status_and_evidence() -> None:
    payload = {
        "thread_id": "ui-001",
        "state": {
            "status": "WAITING_APPROVAL",
            "current_node": "risk",
            "risk_level": "HIGH",
        },
        "interrupts": [
            {
                "value": {
                    "risk_flags": [
                        {
                            "severity": "HIGH",
                            "description": "资产负债率上升。",
                            "evidence": ["metric:debt_ratio", "source-001"],
                        }
                    ]
                }
            }
        ],
    }

    markdown = _risk_markdown(payload)

    assert "ui-001" in markdown
    assert "WAITING_APPROVAL" in markdown
    assert "资产负债率上升" in markdown
    assert "source-001" in markdown
    assert '"thread_id": "ui-001"' in serialize_for_debug(payload)
