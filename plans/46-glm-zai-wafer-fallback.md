# Plan 46 — GLM Z.ai -> Wafer quota fallback

## Problem

The Tanren run config was still pinning `subtask_doer_model` to
`gpt-5.3-codex`, even though the intended default doer model is
`GLM-5.1-zai`. Separately, Quikode's quota handler slept inside the selected
provider on 429, so switching to Z.ai risked tying up workers for the full
quota backoff window instead of using Wafer Pass as the subscription fallback.

## Change

- `ModelSpec` now has `quota_fallbacks`.
- `GLM-5.1-zai` declares `GLM-5.1-wafer` as its quota fallback.
- `CodexLitellmJsonAgent` can be configured to surface quota immediately.
- `QuotaFallbackJsonAgent` wraps fallback-capable transports and invokes the
  next provider when the previous provider reports quota/rate-limit exhaustion.
- The live Tanren config was updated to `subtask_doer_model = "GLM-5.1-zai"`.

The fallback is intentionally narrow. Schema failures, empty diffs, and
non-quota transport failures still surface through the normal JSON/subtask
retry paths; only provider quota moves to the fallback model.

## Verification

- Agent registry tests assert GLM-Z.ai builds a fallback wrapper with Z.ai as
  primary and Wafer as fallback.
- JSON protocol tests assert a 429 primary result invokes the fallback and
  preserves combined duration.
- `_run_with_retry` has a regression test for immediate quota surfacing when a
  fallback wrapper is responsible for provider rotation.
