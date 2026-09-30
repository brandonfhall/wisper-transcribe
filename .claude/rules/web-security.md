---
paths:
  - "src/wisper_transcribe/web/**"
  - "tests/test_path_traversal.py"
---

# Web Route Security Standards

These rules apply to every web route handler. CodeQL scans all PRs — violations block merge.

## User input in file paths (CWE-22 Path Traversal)
Use the two-layer pattern for any URL parameter or form field used in a file path:
1. `os.path.basename()` strips leading path components.
2. `os.path.abspath(os.path.join(base, safe_name)).startswith(base + os.sep)` confirms the result stays inside the intended directory.

`Path.resolve()` on tainted input is **not** sufficient — CodeQL does not recognise it as a sanitiser. Use `os.path.abspath` + `startswith`.

For a transcript or its companions in the output root, use `transcript_store.safe_path(stem, suffix)`. It applies the same two layers and also finds a file whose name is stored NFD on disk (`existing_form()`).

## User input in redirect URLs (CWE-601 Open Redirect)
Use `_validate_job_id()` (defined in `transcribe.py`) for every job ID that appears in a `RedirectResponse` or `Location` header. For other ID types, apply the same two-layer pattern:
1. Strict regex guard `re.match(r"^[\w\-]+$", value)` — rejects everything except alphanumerics and hyphens.
2. `os.path` dummy-guard round-trip — `os.path.basename(os.path.abspath(os.path.join(base, value)))` — to produce a string that CodeQL's taint tracker recognises as clean.

`re.match().group(1)` is **still considered tainted** by CodeQL even after a format check. The `os.path` round-trip is required to break the taint chain.

**Prefer server-generated IDs in redirect URLs.** Even after `_validate_job_id()`, CodeQL may still track the validated value as tainted. The cleanest solution is to look up the server object (e.g. a `Job`) using the validated ID and then use the object's own `id` field (set from `uuid.uuid4()` at creation — never from user input) in the redirect URL. This removes user-controlled data from the taint sink entirely:
```python
safe_id = _validate_job_id(job_id)
job = queue.get(safe_id)
if job is None:
    return RedirectResponse(url="/transcribe", status_code=303)
return RedirectResponse(url=f"/transcribe/jobs/{job.id}", status_code=303)  # job.id is UUID, not tainted
```

## Never reflect user input into error messages or redirect parameters
Exception messages, file paths, and internal state must not appear in redirect `Location` headers or in HTML error responses. Use a generic error code (e.g. `?error=enroll_failed`) instead of `?error={str(exc)}`.

## Never accept arbitrary file paths from form data
Do not accept `output_dir`, `base_path`, or similar path parameters from form POST data. Always use the internally-resolved default (e.g. `path_utils.get_output_dir()`).

## Test coverage requirement
Every security control must have a corresponding test in `tests/test_path_traversal.py` covering:
- Null-byte payloads (`\x00`)
- Regex-busting payloads (`invalid*name`, `id/with/slashes`)
- Open-redirect / CRLF payloads for any endpoint that redirects
