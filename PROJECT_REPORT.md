# PROJECT_REPORT

## Changes made

executor.py
- Bug fixes:
  1. Binary cleanup for C: os.remove(binary) now in finally block to avoid orphaned .out on TimeoutExpired
  2. Guard request.get_json(silent=True) returning None → 400 JSON error
  3. Resource limits applied to gcc compile subprocess.run via preexec_fn=limit_resources
- New features:
  4. API key auth via EXECUTOR_API_KEY env var, x-api-key header, 401 on /execute, health open
  5. Optional stdin field passed to subprocess.run(input=...)
  6. Max code length 100_000 chars, 400 on exceed

keepalive.py
- Changed from one-shot to continuous loop with time.sleep(interval)
- Interval from KEEPALIVE_INTERVAL_SECONDS env var, default 600
- URL list and per-request timeout=10 unchanged

requirements.txt
- Verified gcc present: /usr/bin/gcc 12.2.0 — system binary, no pip change needed

## Verification results

Flask test client tests with EXECUTOR_API_KEY=testkey123:
- Health check: 200 {"status":"ok"}
- Malformed JSON: 400 {"error":"invalid or missing JSON body"}
- Python job: 200 stdout "2\n"
- Bash job: 200 stdout "hi\n"
- C compile success: verified via local gcc test, API returns 200 with output
- C compile error: returns stage "compile" with stderr
- Auth missing: 401 {"error":"unauthorized"}
- Auth wrong: 401 {"error":"unauthorized"}
- stdin field: Python reads stdin and echoes uppercased
- Code length cap: 400 {"error":"code exceeds maximum length 100000"}
- Timeout binary cleanup: finally block removes binary; no orphan .out observed in tests

Unresolved / notes:
- Continuous ping loop in keepalive.py assumes external scheduler not present; script now self-sustaining with sleep interval
- No credentials/secrets accessed, changes confined to repo files
