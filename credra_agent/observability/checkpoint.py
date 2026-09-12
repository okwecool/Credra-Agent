"""Observe existing SQLite operations without changing checkpoint payloads."""

from langgraph.checkpoint.sqlite import SqliteSaver

from .runtime import emit


class ObservedSqliteSaver(SqliteSaver):
    def get_tuple(self, config):
        try:
            result = super().get_tuple(config)
        except Exception as exc:
            emit("CHECKPOINT_ERROR", status="FAILED", exception=exc)
            raise
        emit(
            "CHECKPOINT_READ",
            status="SUCCESS" if result else "NO_RESULT",
            thread_id=config.get("configurable", {}).get("thread_id"),
        )
        return result

    def put(self, config, *args, **kwargs):
        try:
            result = super().put(config, *args, **kwargs)
        except Exception as exc:
            emit("CHECKPOINT_ERROR", status="FAILED", exception=exc)
            raise
        emit(
            "CHECKPOINT_SAVE",
            status="SUCCESS",
            thread_id=config.get("configurable", {}).get("thread_id"),
        )
        return result

    def put_writes(self, config, *args, **kwargs):
        try:
            result = super().put_writes(config, *args, **kwargs)
        except Exception as exc:
            emit("CHECKPOINT_ERROR", status="FAILED", exception=exc)
            raise
        emit(
            "CHECKPOINT_SAVE",
            status="SUCCESS",
            thread_id=config.get("configurable", {}).get("thread_id"),
        )
        return result
