"""Convert an existing structured startup archive without changing its files."""

import argparse
import json
import os
from pathlib import Path

from .text import render_event
from .wire import validate_event
from .writer import ROTATION_BYTES


def export_text_logs(source: Path, destination: Path, *, max_bytes=ROTATION_BYTES):
    source, destination = source.resolve(), destination.resolve()
    if max_bytes < 1024 or destination == source or source in destination.parents:
        raise ValueError("invalid export destination or capacity")
    paths = sorted(source.glob("service.*.jsonl"))
    if not paths:
        raise ValueError("source has no structured service volumes")
    destination.mkdir(parents=True, exist_ok=False)
    manifest_path = destination / "export.json"
    manifest = {
        "schema_version": "text_log_export_v1",
        "source_startup_id": source.name,
        "complete": False,
        "max_bytes": max_bytes,
        "events": 0,
        "segments": 1,
        "limitations": "历史转换不能补回原日志未记录的字段或模型输出。",
    }

    def save_manifest():
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

    save_manifest()
    file = (destination / "service.0001.log").open("xb")
    size = 0
    try:
        for path in paths:
            with path.open(encoding="utf-8") as original:
                for line in original:
                    record = json.loads(line)
                    event = {
                        key: value
                        for key, value in record.items()
                        if key
                        not in {
                            "startup_id",
                            "sequence",
                            "received_at",
                            "truncated_fields",
                        }
                    }
                    validate_event(event)
                    payload = render_event({**event, "sequence": record["sequence"]})
                    if len(payload) > max_bytes:
                        raise ValueError("export event exceeds capacity")
                    if size + len(payload) > max_bytes:
                        file.flush()
                        os.fsync(file.fileno())
                        file.close()
                        manifest["segments"] += 1
                        file = (
                            destination / f"service.{manifest['segments']:04d}.log"
                        ).open("xb")
                        size = 0
                    file.write(payload)
                    size += len(payload)
                    manifest["events"] += 1
        file.flush()
        os.fsync(file.fileno())
        manifest["complete"] = True
    finally:
        file.close()
        save_manifest()
    return manifest


def main():
    parser = argparse.ArgumentParser(
        description="导出历史服务日志为可读 .log，保留原档案"
    )
    parser.add_argument("--startup-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path)
    arguments = parser.parse_args()
    target = arguments.output_dir or (
        arguments.startup_dir.resolve().parent / "readable" / arguments.startup_dir.name
    )
    try:
        manifest = export_text_logs(arguments.startup_dir, target)
    except (OSError, ValueError, KeyError, TypeError):
        print(
            "TEXT_LOG_EXPORT_FAILED: verify source, output directory and disk; existing files are not overwritten"
        )
        return 1
    print(
        json.dumps(
            {
                "status": "SUCCESS",
                "output_directory": str(target),
                "events": manifest["events"],
            },
            ensure_ascii=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
