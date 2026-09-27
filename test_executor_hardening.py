import unittest
from unittest.mock import patch
import sys
sys.path.insert(0, '/opt/render/project/src')

import executor as exec_mod


class TestHardening(unittest.TestCase):
    def setUp(self):
        exec_mod._rate_limit.clear()
        self.old_key = exec_mod.EXECUTOR_API_KEY
        exec_mod.EXECUTOR_API_KEY = "testkey"
        self.app = exec_mod.app
        self.client = self.app.test_client()

    def tearDown(self):
        exec_mod.EXECUTOR_API_KEY = self.old_key

    def test_fail_closed_no_key(self):
        exec_mod.EXECUTOR_API_KEY = None
        resp = self.client.post('/execute', headers={"x-api-key": "x"}, json={"language": "python", "code": "print(1)"})
        self.assertEqual(resp.status_code, 401)

    def test_auth_missing_header(self):
        resp = self.client.post('/execute', json={"language": "python", "code": "print(1)"})
        self.assertEqual(resp.status_code, 401)

    def test_auth_wrong_key(self):
        resp = self.client.post('/execute', headers={"x-api-key": "bad"}, json={"language": "python", "code": "print(1)"})
        self.assertEqual(resp.status_code, 401)

    def test_code_length_limit(self):
        long_code = "a" * (exec_mod.MAX_CODE_LENGTH + 1)
        resp = self.client.post('/execute', headers={"x-api-key": "testkey"}, json={"language": "python", "code": long_code})
        self.assertEqual(resp.status_code, 400)

    def test_stdin_limit(self):
        big = "a" * (exec_mod.MAX_STDIN_LENGTH + 1)
        resp = self.client.post('/execute', headers={"x-api-key": "testkey"}, json={"language": "python", "code": "1", "stdin": big})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("stdin too large", resp.get_json()["error"])

    def test_language_allowlist(self):
        resp = self.client.post('/execute', headers={"x-api-key": "testkey"}, json={"language": "ruby", "code": "1"})
        self.assertEqual(resp.status_code, 400)

    @patch('executor._run_capped')
    def test_success_path(self, mock_capped):
        mock_capped.return_value = (0, "ok\n", "", False, False)
        resp = self.client.post('/execute', headers={"x-api-key": "testkey"}, json={"language": "python", "code": "print(1)"})
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data["code"], 0)
        self.assertEqual(data["stdout"], "ok\n")

    @patch('executor._run_capped')
    def test_timeout(self, mock_capped):
        mock_capped.return_value = (0, "", "", True, False)
        resp = self.client.post('/execute', headers={"x-api-key": "testkey"}, json={"language": "bash", "code": "sleep 1"})
        self.assertEqual(resp.status_code, 408)

    def test_umask_is_restrictive(self):
        import inspect
        src = inspect.getsource(exec_mod)
        self.assertIn("os.umask(0o077)", src)


if __name__ == '__main__':
    unittest.main()
