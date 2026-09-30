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
# Injected into every E2B sandbox so the Gemini CLI is authenticated there.
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
# Idempotent: only installs the CLI when it is missing (i.e. fresh sandboxes).
GEMINI_ENSURE_CMD = "command -v gemini >/dev/null 2>&1 || npm install -g @google/gemini-cli"

app = Flask(__name__)