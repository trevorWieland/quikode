# Plan 07 — overnight-friendly observability

## Pain points observed during the May 2 run

- Daemon died at ~16:40, stayed dead for 6.3 hours. No alert reached the operator.
  The only visible signal was that briefing showed `heartbeat STALE age=22578s`.
- 8 in-flight tasks were lost to the "no prompts found" bug. Only post-hoc analysis
  surfaced the shared root cause; in real time, the operator would have seen 8 task
  failures and assumed bad luck.
- The state_log records transitions but doesn't bubble up patterns. "All 8 of these
  tasks failed in the same minute with the same error" requires manual SQL.

## What to add

### A. Heartbeat-staleness push notification

`scripts/quikode-heartbeat-watch.sh`:

```bash
#!/usr/bin/env bash
set -e
WORKSPACE="${1:?workspace dir required}"
TOPIC="$(toml-get "$WORKSPACE/.quikode/config.toml" notify_ntfy_topic)"
URL="$(toml-get "$WORKSPACE/.quikode/config.toml" notify_ntfy_url)"
HB="$WORKSPACE/.quikode/orchestrator.heartbeat"

while true; do
    age=$(( $(date +%s) - $(stat -c %Y "$HB") ))
    if (( age > 300 )); then
        curl -s -d "daemon stale ${age}s — check $WORKSPACE" "$URL/$TOPIC"
        sleep 600  # one alert per 10 min, don't spam
    else
        sleep 60
    fi
done
```

Runs alongside the daemon (separate systemd unit or `tmux new -d`). Hits ntfy when
heartbeat goes stale.

### B. Failure-cluster detector inside `qk briefing`

When `briefing` runs, check for "≥3 tasks transitioned to FAILED with the same
last_error within the last 30 minutes" and surface that prominently. This would
have caught the prompts bug in real time:

```
⚠️  cluster: 8 tasks FAILED with shared error: "no prompts found at /home/..."
   R-0002 R-0004 R-0008 R-0010 R-0023 R-0024 R-0019 R-0006
```

Implementation:

```sql
SELECT last_error, COUNT(*) FROM tasks
WHERE state = 'failed' AND ts > strftime('%s','now') - 1800
GROUP BY last_error HAVING COUNT(*) >= 3;
```

In `cli_briefing_dev.py`. Cheap query, runs once per briefing.

### C. ntfy push on first BLOCKED of the night

The notify_settled_after_s knob already pings on settled tasks. Add a complementary
`notify_blocked_first_in_window_s` (default 3600). The first BLOCKED in the window
pings; subsequent BLOCKED in the same hour are suppressed. The point is "wake up
when the system stops making progress", not "send 50 alerts".

### D. State-coverage delta in briefing

Add a `Delta vs last briefing` section:

```
Delta (last 30 min)
  +5 merged   (R-0021, R-0019, ...)
  +2 in_flight
  +0 blocked
  trend: healthy
```

Or:

```
Delta (last 30 min)
  +0 merged
  +0 in_flight
  +3 blocked    ⚠
  trend: stalled — investigate
```

A cron-friendly briefing emits this; today's briefing is a snapshot, not a delta.

### E. `qk briefing --json` for the watcher

Make briefing emit JSON when given `--json`. Combine with cron:

```cron
*/10 * * * * cd ~/github/quikode-runs/tanren && qk briefing --json > /tmp/qk-state.json
```

A separate ntfy script tails `/tmp/qk-state.json` and pushes on threshold breaches
(e.g. "in_flight has been 0 for 3 polls in a row, heartbeat fresh — orchestrator is
idle but tasks are starved").

## Priority

A and B are the highest-leverage. A would have prevented the 6-hour outage. B would
have made the prompts bug visible in 30 minutes instead of 30 minutes _and then_ the
operator manually grepping logs.

C/D/E are quality-of-life follow-ups.

## Out of scope

- Grafana / proper metrics. Quikode is single-host single-operator; ntfy + briefing is
  enough. If we later run quikode-as-a-service, revisit.
