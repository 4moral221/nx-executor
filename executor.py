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
MAX_CPU_SECONDS = 5

def limit_resources():
    resource.setrlimit(resource.RLIMIT_AS, (MAX_MEMORY_BYTES, MAX_MEMORY_BYTES))
    resource.setrlimit(resource.RLIMIT_CPU, (MAX_CPU_SECONDS, MAX_CPU_SECONDS))

@app.route("/")
def health():
    return jsonify({"status": "ok"})

@app.route("/execute", methods=["POST"])
def execute():
    data = request.get_json()
    lang = data.get("language")
    code = data.get("code", "")

    if lang not in RUNNERS:
        return jsonify({"error": f"unsupported language: {lang}"}), 400

    runner = RUNNERS[lang]
    job_id = str(uuid.uuid4())
    filename = f"/tmp/{job_id}.{runner['ext']}"

    with open(filename, "w") as f:
        f.write(code)

    try:
        if runner.get("compile"):
            binary = f"/tmp/{job_id}.out"
            compile_result = subprocess.run(
                ["gcc", filename, "-o", binary],
                capture_output=True, text=True, timeout=10
            )
            if compile_result.returncode != 0:
                return jsonify({
                    "stdout": "",
                    "stderr": compile_result.stderr[:2000],
                    "code": compile_result.returncode,
                    "stage": "compile"
                })
            result = subprocess.run(
                [binary], capture_output=True, text=True, timeout=5,
                preexec_fn=limit_resources
            )
            os.remove(binary)
        else:
            result = subprocess.run(
                runner["cmd"] + [filename],
                capture_output=True, text=True, timeout=5,
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
            os.remove(filename)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 2000))
    app.run(host="0.0.0.0", port=port)
