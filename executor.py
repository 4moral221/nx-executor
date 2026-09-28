import os
import time
import signal
import shutil
import tempfile
import threading
from pathlib import Path
from flask import Flask, request, jsonify
import subprocess
import uuid

app = Flask(__name__)

os.umask(0o077)

RUNNERS = {
    "python": {"ext": "py", "cmd": ["python3"]},
    "bash": {"ext": "sh", "cmd": ["bash"]},
    "c": {"ext": "c", "compile": True},
}

MAX_MEMORY_BYTES = 100 * 1024 * 1024
MAX_CPU_SECONDS = 30
MAX_CODE_LENGTH = 100_000
MAX_STDIN_LENGTH = 4 * 1024
EXEC_TIMEOUT = 15
COMPILE_TIMEOUT = 15
MAX_OUTPUT_CHARS = 2000
MAX_OUTPUT_BYTES = 64 * 1024

# Exec-wrapper: applies rlimits in a normal child process (no preexec_fn),
# then execs the target. Survives gevent fork hazards and preserves limits.
_RUN_WRAPPER = (
    "import resource,os,sys\n"
    "def _s(r,v):\n"
    "    try: resource.setrlimit(r,(v,v))\n"
    "    except Exception: pass\n"
    "_s(resource.RLIMIT_AS,{as_})\n"
    "_s(resource.RLIMIT_CPU,{cpu})\n"
    "_s(resource.RLIMIT_FSIZE,{fs})\n"
    "_s(resource.RLIMIT_NPROC,{np})\n"
    "_s(resource.RLIMIT_NOFILE,{nf})\n"
    "os.execvp(sys.argv[1], sys.argv[1:])\n"
).format(
    as_=MAX_MEMORY_BYTES,
    cpu=MAX_CPU_SECONDS,
    fs=10 * 1024 * 1024,
    np=64,
    nf=64,
)

_COMPILE_WRAPPER = (
    "import resource,os,sys\n"
    "def _s(r,v):\n"
    "    try: resource.setrlimit(r,(v,v))\n"
    "    except Exception: pass\n"
    "_s(resource.RLIMIT_CPU,{cpu})\n"
    "_s(resource.RLIMIT_FSIZE,{fs})\n"
    "_s(resource.RLIMIT_NOFILE,{nf})\n"
    "os.execvp(sys.argv[1], sys.argv[1:])\n"
).format(
    cpu=MAX_CPU_SECONDS,
    fs=10 * 1024 * 1024,
    nf=64,
)

EXECUTOR_API_KEY = os.environ.get("EXECUTOR_API_KEY")

_rate_limit = {}
_rate_limit_lock = threading.Lock()
_RATE_LIMIT_MAX = 10
_RATE_LIMIT_WINDOW = 60

def _rate_limit_check():
    ip = request.remote_addr or "unknown"
    now = time.time()
    window_start = now - _RATE_LIMIT_WINDOW
    with _rate_limit_lock:
        timestamps = [t for t in _rate_limit.get(ip, []) if t > window_start]
        if len(timestamps) >= _RATE_LIMIT_MAX:
            _rate_limit[ip] = timestamps
            return False
        timestamps.append(now)
        _rate_limit[ip] = timestamps
    return True

class _CappedReader:
    """Reads a pipe, keeping at most `cap` bytes; flags overflow."""
    def __init__(self, stream, cap):
        self.stream = stream
        self.cap = cap
        self._buf = bytearray()
        self.overflow = False
        self._thread = threading.Thread(target=self._read, daemon=True)

    def start(self):
        self._thread.start()

    def join(self, timeout=None):
        self._thread.join(timeout)

    def data(self):
        return bytes(self._buf)

    def _read(self):
        try:
            while True:
                chunk = self.stream.read(8192)
                if not chunk:
                    break
                room = self.cap - len(self._buf)
                if room > 0:
                    self._buf.extend(chunk[:room])
                if len(chunk) > room:
                    self.overflow = True
        except Exception:
            pass

def _kill_tree(proc):
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass

def _run_capped(cmd, stdin_data=None, timeout=EXEC_TIMEOUT):
    """Run cmd with output streamed through bounded readers.

    Returns (returncode, stdout, stderr, timed_out, overflowed).
    """
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE if stdin_data else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
        close_fds=True,
    )
    out = _CappedReader(proc.stdout, MAX_OUTPUT_BYTES)
    err = _CappedReader(proc.stderr, MAX_OUTPUT_BYTES)
    out.start()
    err.start()

    timed_out = False
    overflowed = False
    try:
        if stdin_data and proc.stdin:
            try:
                proc.stdin.write(stdin_data.encode("utf-8", "ignore"))
                proc.stdin.close()
            except Exception:
                pass

        deadline = time.time() + timeout
        while True:
            if proc.poll() is not None:
                break
            if out.overflow or err.overflow:
                overflowed = True
                _kill_tree(proc)
                break
            if time.time() >= deadline:
                timed_out = True
                _kill_tree(proc)
                break
            time.sleep(0.02)

        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _kill_tree(proc)
            proc.wait()
    finally:
        out.join(5)
        err.join(5)
        for s in (proc.stdout, proc.stderr, proc.stdin):
            try:
                if s:
                    s.close()
            except Exception:
                pass

    return (
        proc.returncode,
        out.data().decode("utf-8", "replace"),
        err.data().decode("utf-8", "replace"),
        timed_out,
        overflowed,
    )

@app.route("/")
def health():
    return jsonify({"status": "ok"})

@app.route("/execute", methods=["POST"])
def execute():
    if not EXECUTOR_API_KEY:
        return jsonify({"error": "unauthorized"}), 401
    key = request.headers.get("x-api-key")
    if not key or key != EXECUTOR_API_KEY:
        return jsonify({"error": "unauthorized"}), 401

    if not _rate_limit_check():
        return jsonify({"error": "rate limited"}), 429

    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "invalid or missing JSON body"}), 400

    lang = data.get("language")
    code = data.get("code", "") or ""
    stdin_data = data.get("stdin", "") or ""

    if lang not in RUNNERS:
        return jsonify({"error": f"unsupported language: {lang}"}), 400
    if len(code) > MAX_CODE_LENGTH:
        return jsonify({"error": f"code exceeds maximum length {MAX_CODE_LENGTH}"}), 400
    if len(stdin_data) > MAX_STDIN_LENGTH:
        return jsonify({"error": "stdin too large"}), 400

    runner = RUNNERS[lang]
    job_id = str(uuid.uuid4())
    base_tmp = Path("/tmp/executor")
    job_dir = None
    filename = None
    binary = None

    try:
        job_dir = base_tmp / job_id
        job_dir.mkdir(parents=True, exist_ok=False, mode=0o700)

        fd, tmp_path = tempfile.mkstemp(dir=str(job_dir), suffix=f".{runner['ext']}")
        os.close(fd)
        Path(tmp_path).write_text(code, encoding="utf-8")
        filename = tmp_path

        if runner.get("compile"):
            binary = str(job_dir / f"{job_id}.out")
            compile_result = subprocess.run(
                ["python3", "-c", _COMPILE_WRAPPER, "gcc", filename, "-o", binary],
                capture_output=True, text=True, timeout=COMPILE_TIMEOUT
            )
            if compile_result.returncode != 0:
                return jsonify({
                    "stdout": "",
                    "stderr": "compile error",
                    "code": compile_result.returncode,
                    "stage": "compile"
                })
            target = [binary]
        else:
            target = runner["cmd"] + [filename]

        rc, stdout, stderr, timed_out, overflowed = _run_capped(
            ["python3", "-c", _RUN_WRAPPER] + target,
            stdin_data=stdin_data if stdin_data else None,
            timeout=EXEC_TIMEOUT,
        )

        if timed_out:
            return jsonify({"error": "timeout"}), 408

        return jsonify({
            "stdout": stdout[:MAX_OUTPUT_CHARS],
            "stderr": stderr[:MAX_OUTPUT_CHARS],
            "code": rc,
            "truncated": bool(overflowed),
        })
    except subprocess.TimeoutExpired:
        return jsonify({"error": "timeout"}), 408
    except Exception:
        return jsonify({"error": "execution error"}), 500
    finally:
        try:
            if filename and os.path.exists(filename):
                os.remove(filename)
        except Exception:
            pass
        try:
            if binary and os.path.exists(binary):
                os.remove(binary)
        except Exception:
            pass
        try:
            if job_dir and job_dir.exists():
                shutil.rmtree(job_dir, ignore_errors=True)
        except Exception:
            pass

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 2000))
    app.run(host="0.0.0.0", port=port)
