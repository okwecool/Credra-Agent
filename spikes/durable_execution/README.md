# Durable Execution Spike

This spike validates the highest-risk runtime behavior before business logic is
added: a LangGraph task can pause at a dynamic interrupt, survive complete
Python process termination, and resume from a SQLite checkpoint.

## Scope

```text
prepare
  ↓
approval (interrupt)
  ↓
complete
```

`prepare` increments `prepare_runs`. The acceptance test proves that its value
remains `1` after a separate process resumes the interrupted task. The approval
node contains no side effects before `interrupt()` because LangGraph restarts
that node from its beginning during resume.

## Run manually

Run commands from the repository root with the `env_agent` environment active.

Start a task and persist the interrupt:

```powershell
python -m spikes.durable_execution.cli start --thread-id spike-001
```

Exit the shell or Python process, open a new one, and inspect the saved state:

```powershell
python -m spikes.durable_execution.cli status --thread-id spike-001
```

Resume the same thread:

```powershell
python -m spikes.durable_execution.cli resume `
  --thread-id spike-001 `
  --decision approve
```

Use another database when isolation is needed. The global `--db` option must
appear before the subcommand:

```powershell
python -m spikes.durable_execution.cli `
  --db checkpoints/manual-spike.db `
  start --thread-id spike-002
```

The CLI emits JSON so task state and pending interrupts can be inspected or
asserted without parsing console prose.

## Automated verification

```powershell
python -m pytest -q tests/test_durable_execution.py
ruff check spikes tests
ruff format --check spikes tests
python -m pip check
```

The test suite uses `subprocess` for start, status, and resume. Each command
therefore builds a new graph and opens the same SQLite file in a distinct Python
process.

## Validated dependency baseline

- Python 3.11.15
- LangGraph 1.2.11
- langgraph-checkpoint 4.2.0 (transitive dependency)
- langgraph-checkpoint-sqlite 3.1.1
- SQLite 3.53.2

