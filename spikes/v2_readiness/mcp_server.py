"""Independent stdio MCP child for P04; no production logging integration."""

import os
from uuid import uuid4

from fastmcp import FastMCP

from spikes.v2_readiness.logging_spike import send_event

server = FastMCP("Credra P04 logging spike")
PROCESS_ID = uuid4().hex


@server.tool
def log_probe(thread_id: str, count: int = 8) -> dict:
    """Emit synthetic lifecycle events and return only structured MCP data."""
    if not 1 <= count <= 40:
        raise ValueError("count out of spike bounds")
    environment = {
        key: os.environ[key]
        for key in (
            "CREDRA_SPIKE_LOG_PORT",
            "CREDRA_SPIKE_LOG_TOKEN",
            "CREDRA_SPIKE_STARTUP_ID",
        )
    }
    for index in range(count):
        send_event(
            environment,
            {
                "event_type": "LLM_RESULT" if index % 2 else "NODE_START",
                "event_id": uuid4().hex,
                "thread_id": thread_id,
                "process_instance_id": PROCESS_ID,
                "pid": os.getpid(),
                "call_id": f"{thread_id}-{index}",
                "attempt": 1,
                "status": "INVALID_SCHEMA" if index % 2 else "SUCCESS",
                "api_key": "secret-sentinel-do-not-store",
                "reasoning_content": "reasoning-sentinel-do-not-store",
            },
        )
    return {
        "thread_id": thread_id,
        "count": count,
        "pid": os.getpid(),
        "process_instance_id": PROCESS_ID,
    }


if __name__ == "__main__":
    server.run(transport="stdio")
