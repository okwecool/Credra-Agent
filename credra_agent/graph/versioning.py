"""Strict, read-only graph-version resolution for persisted checkpoints."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter


class GraphIdentityModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LegacyIdentity(GraphIdentityModel):
    graph_version: Literal["legacy_v1"]
    execution_mode: Literal["baseline"]


class AgenticIdentity(GraphIdentityModel):
    graph_version: Literal["agentic_v2"]
    execution_mode: Literal["shadow", "agentic"]


GraphIdentity = Annotated[
    LegacyIdentity | AgenticIdentity, Field(discriminator="graph_version")
]
_ADAPTER = TypeAdapter(GraphIdentity)


def resolve_graph_identity(state: dict) -> LegacyIdentity | AgenticIdentity:
    """Missing version means frozen legacy; unknown or mixed identities fail closed."""

    if "graph_version" not in state:
        if state.get("execution_mode") not in (None, "baseline"):
            raise ValueError("non-legacy state requires an explicit graph version")
        return LegacyIdentity(graph_version="legacy_v1", execution_mode="baseline")
    try:
        payload = {key: state[key] for key in ("graph_version", "execution_mode")}
    except KeyError as exc:
        raise ValueError(
            "graph version and execution mode must be persisted together"
        ) from exc
    return _ADAPTER.validate_python(payload)
