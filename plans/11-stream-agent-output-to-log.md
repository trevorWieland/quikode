# Plan 11 — actually stream agent output to per-task log

## What's wrong

`docker_env.exec_in()` (docker_env.py:336–360) docstring says **"Stream to log if path
given"**. The implementation does `subprocess.run(capture_output=True)`, blocks until
the agent returns, then writes captured stdout/stderr to the log. There's no actual
streaming.

For tanren's opencode doer (10–20 min runs), this means:
- Operator runs `qk tail R-0002` and sees the prompt + `--- RESPONSE ---` only.
- For the entire 10–20 min run, nothing else appears.
- The first sign of life is everything at once when the doer finishes.
- A wedged doer (subprocess hung, never returns) → log shows nothing forever.

## What to do

Replace `subprocess.run` with `subprocess.Popen` + line-buffered streams + a tee:

```python
def exec_in(handle, cmd, log_path=None, stdin=None, timeout=None) -> tuple[int, str, str]:
    full = ["docker", "exec", "-i", handle.container_name, *cmd]
    if log_path is None:
        # No log: keep the simple capture path.
        proc = subprocess.run(full, input=stdin, capture_output=True, text=True, timeout=timeout)
        return proc.returncode, proc.stdout, proc.stderr

    log_path.parent.mkdir(parents=True, exist_ok=True)
    log = log_path.open("a", buffering=1)  # line-buffered
    log.write(f"\n$ {shlex.join(full)}\n")
    log.flush()

    proc = subprocess.Popen(
        full,
        stdin=subprocess.PIPE if stdin is not None else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,  # line-buffered
    )

    # Feed stdin (if any) on a separate thread to avoid deadlock with stdout/stderr.
    if stdin is not None:
        threading.Thread(target=_feed_stdin, args=(proc, stdin), daemon=True).start()

    out_chunks: list[str] = []
    err_chunks: list[str] = []

    def _pump(stream, sink, label):
        for line in iter(stream.readline, ""):
            sink.append(line)
            log.write(line)
        log.flush()

    t_out = threading.Thread(target=_pump, args=(proc.stdout, out_chunks, "stdout"), daemon=True)
    t_err = threading.Thread(target=_pump, args=(proc.stderr, err_chunks, "stderr"), daemon=True)
    t_out.start(); t_err.start()

    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        raise  # caller handles rc=124 path
    finally:
        t_out.join(timeout=5)
        t_err.join(timeout=5)
        log.close()

    return proc.returncode, "".join(out_chunks), "".join(err_chunks)
```

A few details:
- `bufsize=1` plus `text=True` → line-buffered.
- The pump threads daemonize so a hang in `readline` doesn't keep the process alive.
- `proc.wait(timeout=...)` raises `TimeoutExpired` exactly like the existing path
  expects, so the caller's exception handling stays the same.
- Stdin is fed on a thread to avoid the classic stdin/stdout deadlock when the
  prompt is large.

## What this enables (operationally)

- `qk tail <task>` becomes useful — operator sees the doer's progress in real time.
- "Wedged doer" goes from invisible to obvious — no new log lines for 5+ minutes
  while the container is alive = real signal of stuckness.
- Plan 06's progress detector could later add a "pump went silent for N seconds"
  heuristic; today it can't, because the log is silent for the whole run.

## Risk

- Changes the "single source of truth" for agent output. If the pump thread crashes,
  we'd lose stdout. Mitigate: if `t_out`/`t_err` raise, we synthesize an `[interrupted]`
  marker into out_chunks and continue.
- Some agents emit gigabytes (codex's CoT can be huge). The tee would write all of
  it. Existing capture path also has this problem; no regression. Future work:
  cap log file size with rotation.

## Tests

- Unit: launch `bash -c 'echo hi; sleep 1; echo bye'` → assert log file shows
  `hi\n` after 0.5s, `bye\n` after 1.5s.
- Unit: timeout case — same command with `sleep 10`, timeout 1s → assert
  `TimeoutExpired` raised, log contains `hi`.
- Integration: existing opencode/codex/claude tests pass unchanged.

## Sequencing

This is independent of all other plans. Land any time. Big quality-of-life win for
overnight watching. Should ship before plan 06's locality fingerprint, since 06's
"silent for N seconds" extension requires this.
