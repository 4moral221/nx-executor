import os
import sys
import time
import resource
import subprocess
import unittest

sys.path.insert(0, "/opt/render/project/src")
import executor as exec_mod


def _procs_matching(substr):
    hits = []
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                cl = f.read().replace(b"\x00", b" ").decode("utf-8", "replace").strip()
        except Exception:
            continue
        if substr in cl:
            hits.append((pid, cl))
    return hits


class TestStressReal(unittest.TestCase):
    def setUp(self):
        exec_mod._rate_limit.clear()
        self.old_key = exec_mod.EXECUTOR_API_KEY
        self.old_timeout = exec_mod.EXEC_TIMEOUT
        exec_mod.EXECUTOR_API_KEY = "testkey"
        self.client = exec_mod.app.test_client()

    def tearDown(self):
        exec_mod.EXECUTOR_API_KEY = self.old_key
        exec_mod.EXEC_TIMEOUT = self.old_timeout

    def _post(self, code, lang="python", stdin=None):
        body = {"language": lang, "code": code}
        if stdin is not None:
            body["stdin"] = stdin
        return self.client.post("/execute", headers={"x-api-key": "testkey"}, json=body)

    def test_output_cap_real_subprocess(self):
        rss_before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        resp = self._post('print("A" * 10_000_000)')
        rss_after = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertLessEqual(len(data["stdout"]), exec_mod.MAX_OUTPUT_CHARS)
        self.assertTrue(data["truncated"])
        delta_kb = rss_after - rss_before
        self.assertLess(delta_kb, 50 * 1024, f"parent RSS grew {delta_kb} KB")

    def test_output_overflow_does_not_leave_process(self):
        self._post('import time\nprint("A"*5_000_000, flush=True)\ntime.sleep(60)')
        time.sleep(0.5)
        hits = _procs_matching("time.sleep(60)")
        self.assertEqual(hits, [], f"leftover: {hits}")

    def test_timeout_kills_process_tree(self):
        exec_mod.EXEC_TIMEOUT = 1
        resp = self._post("sleep 100 & sleep 100", lang="bash")
        self.assertEqual(resp.status_code, 408)
        time.sleep(0.5)
        hits = _procs_matching("sleep 100")
        self.assertEqual(hits, [], f"leftover sleep procs: {hits}")

    def test_resource_limits_active_in_child(self):
        code = (
            "import resource\n"
            "print('AS', resource.getrlimit(resource.RLIMIT_AS))\n"
            "print('CPU', resource.getrlimit(resource.RLIMIT_CPU))\n"
            "print('NPROC', resource.getrlimit(resource.RLIMIT_NPROC))\n"
            "print('NOFILE', resource.getrlimit(resource.RLIMIT_NOFILE))\n"
        )
        resp = self._post(code)
        self.assertEqual(resp.status_code, 200)
        out = resp.get_json()["stdout"]
        self.assertIn(f"AS ({exec_mod.MAX_MEMORY_BYTES}, {exec_mod.MAX_MEMORY_BYTES})", out)
        self.assertIn(f"CPU ({exec_mod.MAX_CPU_SECONDS}, {exec_mod.MAX_CPU_SECONDS})", out)
        self.assertIn("NPROC (20, 20)", out)
        self.assertIn("NOFILE (64, 64)", out)

    def test_completion_within_reasonable_time(self):
        t0 = time.time()
        resp = self._post("print('hello')")
        dt = time.time() - t0
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["stdout"].strip(), "hello")
        self.assertLess(dt, 5.0)


if __name__ == "__main__":
    unittest.main()
