# Plan 40 - notifications, API service, compose stack, and remote UI foundation

## Goal

Move notifications and operator access from local-only CLI/TUI toward a service-oriented control plane that can be monitored remotely.

This does not replace the TUI immediately. It creates the durable API and deployment shape that a Web UI can consume.

## Current state

- Review-ready notifications exist through configurable ntfy settings.
- Blocks and failures are visible in `qk briefing`, `qk show`, and TUI, but not consistently pushed.
- Daemon state is local files plus SQLite.
- There is no API server.

## Design

Add a compose-ready stack:

- `postgres`: optional control-plane database for long-lived multi-project deployments.
- `quikode-daemon`: global scheduler/control daemon.
- `quikode-api`: HTTP/WebSocket API over the control store.
- optional `quikode-web`: future Web UI.

SQLite remains supported for local/dev, but Postgres becomes the intended long-haul backend once the control plane is multi-project.

## Notification system

Create a unified notification pipeline:

Events:

- review ready
- task blocked
- task failed
- role paused due to model capacity
- resource exhaustion
- daemon stale/restarted
- DAG sync migration requiring attention
- project paused/resumed

Sinks:

- ntfy
- webhook
- local desktop command hook
- future email/Slack adapters

Policy:

- dedupe by `(project_id, task_id, kind, generation)`
- suppress repeated noise windows
- escalation levels: info, warning, critical
- include action URL where available: PR URL, Web UI task URL, local command hint

## API

Initial endpoints:

- `GET /health`
- `GET /projects`
- `GET /projects/{id}`
- `GET /projects/{id}/tasks`
- `GET /projects/{id}/tasks/{task_id}`
- `GET /resources`
- `GET /models`
- `GET /scheduler/queue`
- `GET /events?since=...`
- `POST /projects/{id}/pause`
- `POST /projects/{id}/resume`
- `POST /tasks/{project_id}/{task_id}/retry|resume|abort|rewind`

Use WebSocket or Server-Sent Events for live dashboard updates.

## Implementation

1. Extract notification event creation from review-ready code into `notifications/events.py`.
2. Add `NotificationPolicy` and sink adapters.
3. Add `quikode/api.py`, initially read-only plus safe pause/resume.
4. Add optional Postgres DSN config for the control store.
5. Add `docker-compose.yml` or template under `deploy/compose/`.
6. Add auth boundary before mutating endpoints are considered production:
   - local token file for LAN use
   - reverse proxy auth compatibility
   - explicit no-auth dev mode

## Acceptance

- Blocking a task emits a deduped notification with project/task context.
- Model role pause emits one notification and later emits recovery.
- API lists projects, queue, resources, model capacity, and task details.
- Compose stack can boot the daemon/API against a mounted control config in dev mode.

