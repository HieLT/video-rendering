# Session tracking

Restart the backend to enable tracking for newly opened browser contexts. Existing
contexts do not acquire listeners retroactively. No login or server restart is
performed by installing this change.

Output: `session_tracking.jsonl` in the repository root (ignored by Git).
Timestamps are UTC; add seven hours for Vietnam. Each line has an account UUID,
process ID and a unique run ID linking one browser launch to its close.

Tracked paths: Google account import and the shared browser launcher (Verify,
Open Web and workers), including extension startup retries.

Events:

- `before_launch`: read-only SQLite metadata for each profile's cookie database.
- `context_open`: auth cookie metadata immediately after Chromium opens.
- `cookie_change`: auth cookie metadata changes, sampled every two seconds.
- `auth_response`: Dola sets auth cookies (including expiry/max-age) or returns 401/403.
- `request_failed`: network error code and host only.
- `before_close`: in-memory cookies immediately before programmatic close.
- `context_closed`: disk snapshot at the close event, including manual closure.
- `after_close_return`: disk snapshot after the browser close call completes.

Interpretation:

- Present in `before_close`, missing in `after_close_return`: investigate disk
  persistence, browser shutdown, encryption and profile policy. This does not
  establish which of those mechanisms caused the loss.
- Present on disk before launch, absent in `context_open`: investigate Chromium
  loading/decryption/expiry of the saved cookies.
- Present at startup, then removed with an `auth_response` deletion/expiry:
  investigate server-side logout/session rejection.
- A database read error is unknown evidence, not an empty cookie database.

Cookie values, passwords, raw headers, request bodies, query strings and URL
fragments are not recorded. Cookie metadata does not prove server validity.
Polling can miss transitions shorter than two seconds. Force-killing the process
can prevent close events; inspect the next `before_launch` in that case.
The file is append-only; archive it periodically if used long term.

Validation: nine unit tests passed (tracking and bulk import). An isolated real
Chromium test added a synthetic persistent cookie without navigating to Dola,
closed and reopened a temporary profile. Tracking recorded the cookie in memory
before close, zero saved cookies after close, and no cookie on reopen. This
reproduces a persistence symptom on this machine but does not establish the root
cause for existing accounts. No production accounts were opened or modified.
