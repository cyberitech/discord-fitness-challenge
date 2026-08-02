---
inclusion: always
---

# Logging Standards

Logs are how operators, developers, and agents diagnose software after the
fact. Every meaningful decision, external call, and error path in this
codebase MUST produce a log line that carries enough context to explain what
happened without re-running the program.

## Loggers, Not Print

Production code MUST route output through the language's standard logging
facility — Python `logging`, Java SLF4J / `java.util.logging`, TypeScript
`pino` / `winston` / `bunyan`, Rust `tracing` / `log`, etc. Direct-to-stdout
calls such as `print`, `System.out`, `console.log`, and `println!` are
prohibited in shipped code paths.

Each module MUST initialize its logger once at the top of the file (or the
language equivalent). Inline logger creation inside function bodies is
prohibited.

## Log Levels

Choose the level that matches what an operator would do about the message:

| Level | Use for |
|---|---|
| DEBUG | Detail useful only during investigation — variable values, loop iterations, configuration dumps, resolved paths. Suppressed in production by default. |
| INFO | Normal-operation milestones — starting and completing work, decision-point branches taken, counts, state transitions. |
| WARNING | Recoverable-but-unexpected situations — missing optional configuration, fallback paths taken, deprecated calls, non-critical failures where execution continues. |
| ERROR | Failures that prevent the current operation from completing — unexpected exceptions and unrecoverable states. |

## Decision-Point Logging

Every branch that materially changes what the program does next MUST log,
at INFO level, which branch was taken along with the values that drove the
decision.

```python
if incremental:
    logger.info(f"Fetching incremental commits for {package} since {last_scan_time}")
    commits = git_client.get_commits_after(repo_path=repo_dir, timestamp=last_scan_time)
else:
    logger.info(f"Fetching all commits for {package} (baseline scan)")
    commits = git_client.get_commits_after(repo_path=repo_dir, timestamp=None)
```

## Context

Every log line SHOULD carry the IDs, names, counts, and paths that describe
what the log line is about. Bare messages like `"Cloning package"` force
the operator to reconstruct context from surrounding lines.

Preferred forms:

```python
logger.info(f"Cloning package {package_name} to {target_dir}")
logger.info(f"Generated {len(diff_files)} diff files for {package}")
logger.info(f"Selected tuple: {tuple_.service_name}/{tuple_.package_name}")
```

## Command Execution

External operations — subprocess calls, HTTP requests, database queries,
file system writes to shared locations — MUST log before and after
execution, with the outcome summarized in the after line.

```python
logger.info(f"Starting scan for service: {service_name}")
result = run_scan(service_name)
logger.info(f"Scan complete for {service_name} — {result.count} findings")
```

## Error Logging

An error condition falls into one of two categories:

**True failure** — an unexpected state, external-system failure, bug, or
otherwise unhandled path. The agent MUST log the full stack trace so the
operator has enough to diagnose the crash.

**Expected exception used for control flow** — for example, catching
`FileNotFoundError` to fall back to a default value. The agent MUST NOT log
a stack trace because the exception is part of intended behavior. Log at
WARNING with the reason and the fallback that was taken.

For true failures, log the traceback FIRST, then the human message, so
both survive truncation in downstream log aggregators.

Preferred (Python):

```python
import traceback

try:
    result = perform_operation()
except Exception as e:
    logger.error(traceback.format_exc())
    logger.error(f"perform_operation failed for {package}: {e}")
    raise
```

Preferred (Java, SLF4J — the third argument to `logger.error` is the
throwable and produces a stack trace automatically):

```java
try {
    performOperation();
} catch (Exception e) {
    logger.error("performOperation failed for {}", packageName, e);
    throw e;
}
```

Preferred (TypeScript, pino — the error object embeds the stack):

```typescript
try {
    await performOperation();
} catch (err) {
    logger.error({ err, package: packageName }, "performOperation failed");
    throw err;
}
```

Anti-pattern — this loses the stack trace and gives the operator nothing to
diagnose with:

```python
try:
    perform_operation()
except Exception as e:
    logger.error(f"perform_operation failed: {e}")
```

Expected-exception example (WARNING, no stack trace, describes the
fallback):

```python
try:
    config = load_optional_config(path)
except FileNotFoundError:
    logger.warning(f"No config at {path}; using defaults")
    config = default_config()
```

## Fail-Fast

Exception handlers exist only where recovery is genuinely possible or where
a specific transient failure warrants a retry. A `try/except Exception` (or
its language equivalent) that swallows the error and returns to the caller
as if nothing happened is prohibited. Either recover meaningfully, or
re-raise.

## Summary

- All output goes through a real logger; MUST NOT use `print` and family.
- Match the level to the operator response: DEBUG < INFO < WARNING < ERROR.
- Log every decision point with the values that drove the branch.
- Include IDs, counts, paths, and other context in every message.
- Log before and after external command execution.
- For true error conditions, log the full stack trace before the human message.
- For expected exceptions used in control flow, log at WARNING with no stack
  trace and describe the fallback that was taken.
- Do not swallow exceptions silently; recover meaningfully or re-raise.
