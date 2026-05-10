# Plan 46 — GLM Z.ai -> Wafer -> Codex quota fallback

## Problem

The Tanren run config was still pinning `subtask_doer_model` to
`gpt-5.3-codex`, even though the intended default doer model is
`GLM-5.1-zai`. Separately, Quikode's quota handler slept inside the selected
provider on 429, so switching to Z.ai risked tying up workers for the full
quota backoff window instead of using Wafer Pass as the subscription fallback.
If both subscription providers are exhausted, doer traffic should keep moving
through direct Codex while periodic probes check whether the five-hour provider
windows have reset.

## Change

- `ModelSpec` now has `quota_fallbacks`.
- `GLM-5.1-zai` declares `GLM-5.1-wafer`, then `gpt-5.3-codex`, as its quota
  fallback chain.
- `CodexLitellmJsonAgent` can be configured to surface quota immediately.
- `QuotaFallbackJsonAgent` wraps fallback-capable transports and invokes the
  next provider when the previous provider reports quota/rate-limit exhaustion.
- The live Tanren config was updated to `subtask_doer_model = "GLM-5.1-zai"`.
- Launch validation now checks the Docker-host LiteLLM health URL whenever any
  role is bound to a `codex_litellm` model, catching proxy binding mistakes
  before workers start.
- The LiteLLM runbook now publishes both `127.0.0.1:4000` for host probes and
  `172.17.0.1:4000` for `host.docker.internal` traffic from task containers.
- Client-side JSON validation now tolerates provider prose around a valid JSON
  object, and malformed doer bookkeeping no longer short-circuits subtask
  checking. The diff and witnesses remain the doer evidence.
- Direct Codex can now be the last fallback behind a client-side primary; the
  fallback wrapper converts the CLI-native structured result into JSON text so
  the primary wrapper's pydantic path validates it consistently.
- Operators should keep a five-hour reset probe running when Z.ai/Wafer quota is
  exhausted. Primary selection remains `GLM-5.1-zai`, so every new doer call
  tries Z.ai first, then Wafer, before falling back to Codex.

The fallback is intentionally narrow. Checker/auditor schema failures, empty
diffs, and non-quota transport failures still surface through the normal
JSON/subtask retry paths; only provider quota moves to the fallback model.

## Verification

- Agent registry tests assert GLM-Z.ai builds a fallback wrapper with Z.ai as
  primary, Wafer as the first fallback, and direct Codex as the last fallback.
- JSON protocol tests assert a 429 primary result invokes the fallback and
  preserves combined duration.
- JSON protocol tests assert two exhausted client-side providers can fall
  through to CLI-native Codex while still validating against the requested
  schema.
- JSON protocol tests assert noisy proxy-routed output can still yield a valid
  structured payload when it contains a schema-valid JSON object.
- Subtask execution tests assert malformed doer bookkeeping continues to the
  diff checker and witness runner instead of synthesizing a parse-failure
  subtask rejection.
- `_run_with_retry` has a regression test for immediate quota surfacing when a
  fallback wrapper is responsible for provider rotation.
