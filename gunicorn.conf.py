"""Side-effect-only gunicorn config for the NX executor.

Gunicorn loads this file at startup (see Dockerfile CMD: `-c gunicorn.conf.py`).
Its one job is to make the Gemini CLI available inside every E2B sandbox that the
/terminal endpoint creates:

  * inject GEMINI_API_KEY into the sandbox environment, and
  * install the CLI on first use (the command is a no-op once it is present).

Keeping this here means the large executor.py stays untouched; every change ships
as a small, reviewable file.
"""
import logging
import os

_log = logging.getLogger("nx-executor.gemini")

_GEMINI_ENSURE_CMD = (
    "command -v gemini >/dev/null 2>&1 || npm install -g @google/gemini-cli"
)


def _patch_e2b_sandbox_create():
    try:
        from e2b import Sandbox
    except Exception as exc:  # e2b absent is not fatal for this hook
        _log.warning("gemini: e2b not importable (%s); patch skipped", exc)
        return

    if getattr(Sandbox.create, "_gemini_patched", False):
        return

    original_create = Sandbox.create

    def create(*args, **kwargs):
        envs = dict(kwargs.get("envs") or {})
        key = os.environ.get("GEMINI_API_KEY")
        if key:
            envs["GEMINI_API_KEY"] = key
        kwargs["envs"] = envs or None
        sandbox = original_create(*args, **kwargs)
        try:
            sandbox.commands.run(_GEMINI_ENSURE_CMD, timeout=180)
            _log.info("gemini: CLI ready in sandbox %s", sandbox.sandbox_id)
        except Exception as exc:
            _log.warning("gemini: CLI install skipped: %s", exc)
        return sandbox

    create._gemini_patched = True
    Sandbox.create = staticmethod(create)


_patch_e2b_sandbox_create()
