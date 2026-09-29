import os
import time
import signal
import shutil
import tempfile
import threading
import logging
from pathlib import Path
from flask import Flask, request, jsonify
import subprocess
import uuid
import resource

# ── E2B sandbox state ────────────────────────────────────────────────────────
_e2b_sandbox = None
_e2b_lock = threading.Lock()
E2B_API_KEY = os.environ.get("E2B_API_KEY")
E2B_TERMINAL_TIMEOUT = 300  # seconds per command max

app = Flask(__name__)

os.umask(0o077)

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger(__name__)

RUNNERS = {
    "python": {"ext": "py", "cmd": ["python3"]},
    "bash": {"ext": "sh", "cmd": ["bash"]},
    "c": {"ext": "c", "compile_cmd": ["gcc"]},
    "cpp": {"ext": "cpp", "compile_cmd": ["g++"]},
    "node": {"ext": "js", "cmd": ["node", "--max-old-space-size=128"]},
    "ruby": {"ext": "rb", "cmd": ["ruby"]},
    "php": {"ext": "php", "cmd": ["php"]},
}

MAX_MEMORY_BYTES = 512 * 1024 * 1024
MAX_CPU_SECONDS = 30
MAX_CODE_LENGTH = 100_000
MAX_STDIN_LENGTH = 4 * 1024
EXEC_TIMEOUT = 15
COMPILE_TIMEOUT = 15
MAX_OUTPUT_CHARS = 2000
MAX_OUTPUT_BYTES = 64 * 1024

# Per-job resource limits
# V8 (Node.js) pointer compression requires reserving a virtual address space mapping at init.
RLIMIT_AS = 4 * 1024 * 1024 * 1024   # 4GB virtual address space cap
RLIMIT_STACK = 8 * 1024 * 1024       # 8MB stack (prevents stack-based abuse)
RLIMIT_CPU = MAX_CPU_SECONDS
RLIMIT_FSIZE = 10 * 1024 * 1024
RLIMIT_NPROC = 256
RLIMIT_NOFILE = 256

# UID isolation target – nobody on most Linux containers
_NON_PRIV_UID = 65534
_NON_PRIV_GID = 65534

def _preexec_limits():
    """Run in child before exec: set rlimits and drop privileges.
    Only sets RLIMIT_NPROC when UID isolation succeeds, since applying it
    to the shared service user would starve gunicorn and cause fork failures.
    Catches exceptions silently to avoid leaking setup log lines into child stderr."""
    # Safe per-process limits (these only affect this child)
    try:
        resource.setrlimit(resource.RLIMIT_AS, (RLIMIT_AS, RLIMIT_AS))
    except Exception:
        pass
    try:
        resource.setrlimit(resource.RLIMIT_CPU, (RLIMIT_CPU, RLIMIT_CPU))
    except Exception:
        pass
    try:
        resource.setrlimit(resource.RLIMIT_FSIZE, (RLIMIT_FSIZE, RLIMIT_FSIZE))
    except Exception:
        pass
    try:
        resource.setrlimit(resource.RLIMIT_NOFILE, (RLIMIT_NOFILE, RLIMIT_NOFILE))
    except Exception:
        pass
    try:
        resource.setrlimit(resource.RLIMIT_STACK, (RLIMIT_STACK, RLIMIT_STACK))
    except Exception:
        pass
    # UID isolation — attempt to drop to nobody
    uid_isolated = False
    try:
        os.setgid(_NON_PRIV_GID)
        os.setuid(_NON_PRIV_UID)
        uid_isolated = True
    except Exception:
        pass
    # Only limit NPROC if we successfully switched to an isolated user.
    # When running as the same user as gunicorn, this limit applies to ALL
    # processes under that UID and will starve the service.
    if uid_isolated:
        try:
            resource.setrlimit(resource.RLIMIT_NPROC, (RLIMIT_NPROC, RLIMIT_NPROC))
        except Exception:
            pass

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
    Uses preexec_limits for UID isolation and rlimit enforcement."""
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE if stdin_data else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
        close_fds=True,
        preexec_fn=_preexec_limits,
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

@app.route("/openapi.json")
def openapi_spec():
    return jsonify({
        "openapi": "3.0.0",
        "info": {
            "title": "NX Executor API",
            "description": "Multi-language code execution service for Python, Node.js, Bash, C, C++, Ruby, and PHP.",
            "version": "1.1"
        },
        "servers": [{"url": "https://nx-executor.onrender.com"}],
        "paths": {
            "/execute": {
                "post": {
                    "summary": "Execute source code in a sandboxed runtime",
                    "operationId": "executeCode",
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "required": ["language", "code"],
                                    "properties": {
                                        "language": {
                                            "type": "string",
                                            "enum": ["python", "node", "bash", "c", "cpp", "ruby", "php"],
                                            "description": "Target language runtime"
                                        },
                                        "code": {
                                            "type": "string",
                                            "description": "Source code to execute"
                                        },
                                        "stdin": {
                                            "type": "string",
                                            "description": "Optional standard input passed to script"
                                        }
                                    }
                                }
                            }
                        }
                    },
                    "responses": {
                        "200": {
                            "description": "Execution output",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "properties": {
                                            "code": {"type": "integer", "description": "Process exit code (0 for success)"},
                                            "stdout": {"type": "string", "description": "Standard output"},
                                            "stderr": {"type": "string", "description": "Standard error"},
                                            "truncated": {"type": "boolean", "description": "True if output exceeded length caps"}
                                        }
                                    }
                                }
                            }
                        },
                        "400": {
                            "description": "Bad request (malformed JSON, unsupported language, or code too long)",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "properties": {
                                            "error": {"type": "string"}
                                        }
                                    }
                                }
                            }
                        },
                        "401": {
                            "description": "Unauthorized (missing or incorrect API key)",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "properties": {
                                            "error": {"type": "string"}
                                        }
                                    }
                                }
                            }
                        },
                        "408": {
                            "description": "Execution timed out",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "properties": {
                                            "error": {"type": "string"}
                                        }
                                    }
                                }
                            }
                        },
                        "429": {
                            "description": "Rate limited",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "properties": {
                                            "error": {"type": "string"}
                                        }
                                    }
                                }
                            }
                        }
                    }
                }
            },
            "/terminal": {
                "post": {
                    "summary": "Execute a shell command inside a persistent E2B sandbox",
                    "operationId": "terminalExecute",
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "required": ["cmd"],
                                    "properties": {
                                        "cmd": {
                                            "type": "string",
                                            "description": "Shell command to execute"
                                        },
                                        "timeout": {
                                            "type": "integer",
                                            "description": "Seconds to wait (default 60, max 300)"
                                        },
                                        "workdir": {
                                            "type": "string",
                                            "description": "Working directory inside sandbox (optional)"
                                        }
                                    }
                                }
                            }
                        }
                    },
                    "responses": {
                        "200": {
                            "description": "Terminal execution output",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "properties": {
                                            "exit_code": {"type": "integer", "description": "Process exit code"},
                                            "stdout": {"type": "string", "description": "Standard output"},
                                            "stderr": {"type": "string", "description": "Standard error"},
                                            "sandbox_id": {"type": "string", "description": "ID of the persistent E2B sandbox"},
                                            "workdir": {"type": "string", "description": "Current working directory"}
                                        }
                                    }
                                }
                            }
                        },
                        "400": {
                            "description": "Bad request (missing cmd or cmd too long)",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "properties": {
                                            "error": {"type": "string"}
                                        }
                                    }
                                }
                            }
                        },
                        "401": {
                            "description": "Unauthorized (missing or incorrect API key)",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "properties": {
                                            "error": {"type": "string"}
                                        }
                                    }
                                }
                            }
                        },
                        "500": {
                            "description": "Sandbox error or E2B not configured",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "properties": {
                                            "error": {"type": "string"}
                                        }
                                    }
                                }
                            }
                        }
                    }
                }
            },
            "/terminal/status": {
                "get": {
                    "summary": "Check whether an E2B sandbox is currently alive",
                    "operationId": "terminalStatus",
                    "responses": {
                        "200": {
                            "description": "Sandbox status",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "properties": {
                                            "e2b_configured": {"type": "boolean"},
                                            "sandbox_alive": {"type": "boolean"},
                                            "sandbox_id": {"type": "string", "nullable": True}
                                        }
                                    }
                                }
                            }
                        }
                    }
                }
            },
            "/terminal/reset": {
                "post": {
                    "summary": "Kill the current sandbox and force a fresh one on next /terminal call",
                    "operationId": "terminalReset",
                    "responses": {
                        "200": {
                            "description": "Reset result",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "properties": {
                                            "reset": {"type": "boolean"},
                                            "killed_sandbox_id": {"type": "string", "nullable": True}
                                        }
                                    }
                                }
                            }
                        }
                    }
                }
            }
        },
        "components": {
            "securitySchemes": {
                "ApiKeyAuth": {
                    "type": "apiKey",
                    "in": "header",
                    "name": "x-api-key"
                }
            }
        },
        "security": [{"ApiKeyAuth": []}]
    })

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
    code = data.get("code", "")
    stdin_data = data.get("stdin", "")

    if code is None:
        code = ""
    if stdin_data is None:
        stdin_data = ""

    if not isinstance(code, str):
        return jsonify({"error": "code payload must be a string"}), 400
    if not isinstance(stdin_data, str):
        return jsonify({"error": "stdin payload must be a string"}), 400

    if lang not in RUNNERS:
        return jsonify({"error": f"unsupported language: {lang}"}), 400
    if len(code) > MAX_CODE_LENGTH:
        return jsonify({"error": f"code exceeds maximum length {MAX_CODE_LENGTH}"}), 400
    if len(stdin_data) > MAX_STDIN_LENGTH:
        return jsonify({"error": "stdin too large"}), 400

    runner = RUNNERS[lang]
    
    if runner.get("compile_cmd"):
        executable = runner["compile_cmd"][0]
        if shutil.which(executable) is None:
            return jsonify({"error": f"compiler not found: {executable}"}), 500
    elif runner.get("cmd"):
        executable = runner["cmd"][0]
        if shutil.which(executable) is None:
            return jsonify({"error": f"runtime not found: {executable}"}), 500

    job_id = str(uuid.uuid4())
    base_tmp = Path("/tmp/executor")
    job_dir = None
    filename = None
    binary = None

    try:
        base_tmp.mkdir(parents=True, exist_ok=True, mode=0o777)
        try:
            os.chmod(base_tmp, 0o777)
        except Exception:
            pass

        job_dir = base_tmp / job_id
        job_dir.mkdir(parents=True, exist_ok=False, mode=0o777)
        try:
            os.chmod(job_dir, 0o777)
        except Exception:
            pass

        fd, tmp_path = tempfile.mkstemp(dir=str(job_dir), suffix=f".{runner['ext']}")
        os.close(fd)
        Path(tmp_path).write_text(code, encoding="utf-8")
        filename = tmp_path
        try:
            os.chmod(filename, 0o755)
        except Exception:
            pass

        if runner.get("compile_cmd"):
            binary = str(job_dir / f"{job_id}.out")
            # Compile with preexec limits
            compile_cmd = runner["compile_cmd"] + [filename, "-o", binary]
            compile_proc = subprocess.Popen(
                compile_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
                close_fds=True,
                preexec_fn=_preexec_limits,
            )
            try:
                stdout_c, stderr_c = compile_proc.communicate(timeout=COMPILE_TIMEOUT)
            except subprocess.TimeoutExpired:
                _kill_tree(compile_proc)
                compile_proc.kill()
                return jsonify({"error": "compile timeout"}), 408
            if compile_proc.returncode != 0:
                return jsonify({
                    "stdout": "",
                    "stderr": stderr_c.decode("utf-8", "replace")[:MAX_OUTPUT_CHARS],
                    "code": compile_proc.returncode,
                    "stage": "compile"
                })
            try:
                os.chmod(binary, 0o755)
            except Exception:
                pass
            target = [binary]
        else:
            target = runner["cmd"] + [filename]

        rc, stdout, stderr, timed_out, overflowed = _run_capped(
            target,
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
    except FileNotFoundError as e:
        log.info("command not found in job %s: %s", job_id if job_id else "unknown", e)
        return jsonify({"error": f"command not found: {str(e)}"}), 500
    except Exception as e:
        log.info("execution error in job %s: %s", job_id if job_id else "unknown", e)
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

# ── E2B Terminal ─────────────────────────────────────────────────────────────

def _get_or_create_sandbox():
    """Return the persistent E2B sandbox, creating or reconnecting as needed."""
    global _e2b_sandbox
    if not E2B_API_KEY:
        raise RuntimeError("E2B_API_KEY environment variable not set.")
    try:
        from e2b import Sandbox
        from e2b.connection_config import ConnectionConfig
    except ImportError:
        raise RuntimeError("e2b package not installed. Run: pip install e2b")

    with _e2b_lock:
        # Try to ping existing sandbox
        if _e2b_sandbox is not None:
            try:
                _e2b_sandbox.commands.run("echo ok", timeout=5)
                return _e2b_sandbox
            except Exception:
                log.info("[E2B] Existing sandbox unreachable, creating new one...")
                _e2b_sandbox = None

        # Create a fresh sandbox
        log.info("[E2B] Creating new sandbox...")
        _e2b_sandbox = Sandbox.create(api_key=E2B_API_KEY, timeout=3600)
        log.info("[E2B] Sandbox created: %s", _e2b_sandbox.sandbox_id)
        return _e2b_sandbox


def _require_api_key():
    """Return error response if API key missing/wrong, else None."""
    if not EXECUTOR_API_KEY:
        return jsonify({"error": "unauthorized"}), 401
    key = request.headers.get("x-api-key")
    if not key or key != EXECUTOR_API_KEY:
        return jsonify({"error": "unauthorized"}), 401
    return None


@app.route("/terminal", methods=["POST"])
def terminal():
    """
    Run a shell command inside a persistent E2B sandbox.

    Body (JSON):
        cmd       string   Shell command to execute (required)
        timeout   int      Seconds to wait (default 60, max 300)
        workdir   string   Working directory inside sandbox (optional)

    Returns:
        stdout, stderr, exit_code, sandbox_id
    """
    err = _require_api_key()
    if err:
        return err

    data = request.get_json(silent=True)
    if not data or not data.get("cmd"):
        return jsonify({"error": "Missing required field: cmd"}), 400

    cmd = data["cmd"]
    timeout = min(int(data.get("timeout", 60)), E2B_TERMINAL_TIMEOUT)
    workdir = data.get("workdir", "/home/user")

    if not isinstance(cmd, str) or len(cmd) > 10_000:
        return jsonify({"error": "cmd must be a string under 10,000 chars"}), 400

    try:
        sbx = _get_or_create_sandbox()
        # Wrap in bash so pipes, &&, etc all work. Run in requested workdir.
        full_cmd = f"cd {workdir} 2>/dev/null || true && {cmd}"
        result = sbx.commands.run(full_cmd, timeout=timeout)
        return jsonify({
            "stdout": result.stdout,
            "stderr": result.stderr,
            "exit_code": result.exit_code,
            "sandbox_id": sbx.sandbox_id,
            "workdir": workdir,
        })
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 500
    except Exception as e:
        log.error("[E2B] Terminal error: %s", e)
        # If the sandbox died, clear it so next request creates a fresh one
        global _e2b_sandbox
        with _e2b_lock:
            _e2b_sandbox = None
        return jsonify({"error": f"Sandbox error: {str(e)}"}), 500


@app.route("/terminal/status", methods=["GET"])
def terminal_status():
    """Check whether an E2B sandbox is currently alive."""
    err = _require_api_key()
    if err:
        return err

    if not E2B_API_KEY:
        return jsonify({"e2b_configured": False, "sandbox_alive": False})

    with _e2b_lock:
        sbx = _e2b_sandbox

    if sbx is None:
        return jsonify({"e2b_configured": True, "sandbox_alive": False, "sandbox_id": None})

    try:
        sbx.commands.run("echo ok", timeout=5)
        return jsonify({"e2b_configured": True, "sandbox_alive": True, "sandbox_id": sbx.sandbox_id})
    except Exception:
        return jsonify({"e2b_configured": True, "sandbox_alive": False, "sandbox_id": sbx.sandbox_id})


@app.route("/terminal/reset", methods=["POST"])
def terminal_reset():
    """Kill the current sandbox and force a fresh one on next /terminal call."""
    err = _require_api_key()
    if err:
        return err

    global _e2b_sandbox
    with _e2b_lock:
        old = _e2b_sandbox
        _e2b_sandbox = None

    if old is not None:
        try:
            old.kill()
        except Exception:
            pass
        return jsonify({"reset": True, "killed_sandbox_id": old.sandbox_id})

    return jsonify({"reset": True, "killed_sandbox_id": None})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 2000))
    app.run(host="0.0.0.0", port=port)
