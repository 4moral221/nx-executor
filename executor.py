import os
from flask import Flask, request, jsonify
import subprocess, uuid, resource

app = Flask(__name__)

RUNNERS = {
    "python": {"ext": "py", "cmd": ["python3"]},
    "bash": {"ext": "sh", "cmd": ["bash"]},
    "c": {"ext": "c", "compile": True},
}

MAX_MEMORY_BYTES = 100 * 1024 * 1024  # 100MB per execution
MAX_CPU_SECONDS = 30
MAX_CODE_LENGTH = 100_000
EXEC_TIMEOUT = 30
COMPILE_TIMEOUT = 30

def limit_resources():
    resource.setrlimit(resource.RLIMIT_AS, (MAX_MEMORY_BYTES, MAX_MEMORY_BYTES))
    resource.setrlimit(resource.RLIMIT_CPU, (MAX_CPU_SECONDS, MAX_CPU_SECONDS))

EXECUTOR_API_KEY = os.environ.get("EXECUTOR_API_KEY")

@app.route("/")
def health():
    return jsonify({"status": "ok"})

@app.route("/execute", methods=["POST"])
def execute():
    if EXECUTOR_API_KEY:
        key = request.headers.get("x-api-key")
        if not key or key != EXECUTOR_API_KEY:
            return jsonify({"error": "unauthorized"}), 401

    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "invalid or missing JSON body"}), 400

    lang = data.get("language")
    code = data.get("code", "") or ""
    stdin_data = data.get("stdin", "")

    if lang not in RUNNERS:
        return jsonify({"error": f"unsupported language: {lang}"}), 400

    if len(code) > MAX_CODE_LENGTH:
        return jsonify({"error": f"code exceeds maximum length {MAX_CODE_LENGTH}"}), 400

    runner = RUNNERS[lang]
    job_id = str(uuid.uuid4())
    filename = f"/tmp/{job_id}.{runner['ext']}"
    binary = None

    try:
        with open(filename, "w") as f:
            f.write(code)

        if runner.get("compile"):
            binary = f"/tmp/{job_id}.out"
            compile_result = subprocess.run(
                ["gcc", filename, "-o", binary],
                capture_output=True, text=True, timeout=COMPILE_TIMEOUT,
                preexec_fn=limit_resources
            )
            if compile_result.returncode != 0:
                return jsonify({
                    "stdout": "",
                    "stderr": compile_result.stderr[:2000],
                    "code": compile_result.returncode,
                    "stage": "compile"
                })
            result = subprocess.run(
                [binary],
                capture_output=True, text=True, timeout=EXEC_TIMEOUT,
                input=stdin_data if stdin_data else None,
                preexec_fn=limit_resources
            )
        else:
            result = subprocess.run(
                runner["cmd"] + [filename],
                capture_output=True, text=True, timeout=EXEC_TIMEOUT,
                input=stdin_data if stdin_data else None,
                preexec_fn=limit_resources
            )

        return jsonify({
            "stdout": result.stdout[:2000],
            "stderr": result.stderr[:2000],
            "code": result.returncode
        })
    except subprocess.TimeoutExpired:
        return jsonify({"error": "timeout"}), 408
    finally:
        if os.path.exists(filename):
            try:
                os.remove(filename)
            except Exception:
                pass
        if binary and os.path.exists(binary):
            try:
                os.remove(binary)
            except Exception:
                pass

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 2000))
    app.run(host="0.0.0.0", port=port)
