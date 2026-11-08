# -*- coding: utf-8 -*-
"""回归测试：数据路径、legacy 键、报告路径穿越、配置持久化、冷启动真实分数。"""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


class TestBundledData(unittest.TestCase):
    def test_default_paths_load_bundled_data(self):
        """默认检出下，内置题库/词典必须能被加载（防止 DATA_DIR 再次指错）。"""
        code = (
            "from core import bank;"
            "s=bank.lib_stats();"
            "print(s['bank'], s['phrase'], s['word'], s['pool'], s['dict'])"
        )
        out = subprocess.run([sys.executable, "-c", code], cwd=str(ROOT),
                             capture_output=True, text=True, timeout=60).stdout.split()
        bank_n, phrase_n, word_n, pool_n, dict_n = map(int, out)
        self.assertGreater(bank_n, 5000)
        self.assertGreater(phrase_n, 0)
        self.assertGreater(word_n, 0)
        self.assertGreater(pool_n, 0)
        self.assertGreater(dict_n, 10000)


class TestLegacyKeys(unittest.TestCase):
    def setUp(self):
        self.bank = json.loads((ROOT / "data" / "bank_legacy.json").read_text(encoding="utf-8"))

    def test_three_segment_option_key(self):
        """11 型 legacy key 第三段是选项串，必须仍能命中。"""
        from core import bank
        for key, value in self.bank.items():
            parts = key.split("::")
            if len(parts) == 3 and parts[0] == "11" and value.get("ans") not in (0, None):
                new_key = f"11::norm::{parts[1]}::vt. 完成；达成::{parts[2].lower()}"
                self.assertEqual(bank.bank_lookup(new_key, self.bank), value["ans"])
                return
        self.skipTest("没有可用的 11 型样本")

    def test_four_segment_key(self):
        """4 段 legacy key 过去直接返回 None，现在必须可命中。"""
        from core import bank
        for key, value in self.bank.items():
            parts = key.split("::")
            if len(parts) == 4 and value.get("ans") not in (0, None):
                new_key = f"{parts[0]}::norm::{parts[1]}::{parts[2]}::{parts[3].lower()}"
                self.assertEqual(bank.bank_lookup(new_key, self.bank), value["ans"])
                return
        self.skipTest("没有可用的 4 段样本")


class TestConfigPersistence(unittest.TestCase):
    def test_save_keeps_auto_fields(self):
        from core import config
        old = config.ENV_FILE
        tmp = Path(tempfile.mkdtemp()) / ".env"
        try:
            config.set_env_file(str(tmp))
            tmp.write_text("SIGNIN_ENABLED=1\nTOKEN_SAVED_AT=1700000000\nUSERTOKEN=t\n", encoding="utf-8")
            config.save_runtime_config({"COURSE_ID": "CET4_v2"})
            text = tmp.read_text(encoding="utf-8")
            self.assertIn("SIGNIN_ENABLED=1", text)
            self.assertIn("TOKEN_SAVED_AT=1700000000", text)
        finally:
            config.set_env_file(str(old))


class TestReportKeySafety(unittest.TestCase):
    def test_traversal_rejected(self):
        from web.app import app
        c = app.test_client()
        for key in ("../../../../secret", "..%2f..", "a/b", "a:b"):
            r = c.get("/api/report", query_string={"key": key})
            self.assertEqual(r.status_code, 400, key)

    def test_normal_key_ok(self):
        from web.app import app
        r = app.test_client().get("/api/report", query_string={"key": "class-1-2"})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()["ok"])


class TestColdStartHonesty(unittest.TestCase):
    """冷启动（不预灌题库）跑 mock：分数必须落在真实区间，而不是 100。"""

    def setUp(self):
        self.saved = {}
        from core import bank
        from solver import report
        for mod, attrs in ((bank, ("DATA_DIR", "BANK_FILE", "ANSWER_LIB_FILE",
                                   "WORD_POOL_FILE", "BANK_LEGACY_FILE")),
                           (report, ("REPORTS_DIR",))):
            for attr in attrs:
                self.saved[(mod, attr)] = getattr(mod, attr)
        tmp = Path(tempfile.mkdtemp())
        for mod, attrs in ((bank, ("DATA_DIR", "BANK_FILE", "ANSWER_LIB_FILE",
                                   "WORD_POOL_FILE", "BANK_LEGACY_FILE")),
                           (report, ("REPORTS_DIR",))):
            for attr in attrs:
                setattr(mod, attr, tmp / Path(getattr(mod, attr)).name)

    def tearDown(self):
        for (mod, attr), value in self.saved.items():
            setattr(mod, attr, value)

    def test_cold_start_score_is_not_100(self):
        import re
        from core import api
        from solver import runner
        from core.api import Client
        import mock_server

        port = mock_server.run_mock_server(0)
        old_base, old_student = api.BASE, api.STUDENT_API
        api.BASE = f"http://127.0.0.1:{port}/studentv1/api"
        api.STUDENT_API = api.BASE
        mock_server.SESSION.clear()
        mock_server.TASKS.clear()
        logs = []
        cfg = {"USERTOKEN": "mock-token", "AI_PRIORITY": "0", "USE_WORD_API": "0",
               "DELAY_PER_QUESTION": "0", "MAX_ROUNDS": "1", "QUESTION_TIMEOUT": "60",
               "LLM_URL": "", "LLM_KEY": ""}
        try:
            runner.run_full(Client("mock-token", timeout=5), 9001, 9002,
                            config=cfg, log=logs.append, task_type=1)
        finally:
            api.BASE, api.STUDENT_API = old_base, old_student
        m = re.search(r"\[分数\]\s*([\d.]+)", "\n".join(logs))
        self.assertIsNotNone(m, "没有分数输出")
        score = float(m.group(1))
        # 词典可用、题库为空时实测约 45.5；这里给宽容区间，防止未来规则变化误报
        self.assertGreaterEqual(score, 25, f"冷启动分数 {score} 过低")
        self.assertLessEqual(score, 70, f"冷启动分数 {score} 过高，疑似又预灌了题库")


if __name__ == "__main__":
    unittest.main(verbosity=2)
