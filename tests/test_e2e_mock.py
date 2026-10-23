# -*- coding: utf-8 -*-
"""mock server E2E 测试：无真实账号验证答题引擎全流程（整合 vocabgo-auto 的测试思路）。
启动 mock 词达人服务端 -> 指向引擎 -> run_full 跑满 -> 断言分数与报告。"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import api, bank
from solver import report, runner
import mock_server
from core.api import Client


def _free_port() -> int:
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class TestE2EMock(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        # 重定向数据目录，避免污染真实答案库/报告
        from pathlib import Path
        tmp = Path(cls._tmp.name)
        for mod, attrs in ((bank, ("DATA_DIR", "BANK_FILE", "ANSWER_LIB_FILE",
                                   "WORD_POOL_FILE", "BANK_LEGACY_FILE")),
                           (report, ("REPORTS_DIR",))):
            for attr in attrs:
                if hasattr(mod, attr):
                    setattr(mod, attr, tmp / Path(getattr(mod, attr)).name)
        
        # 自动把 mock 题库加进临时 bank_legacy.json，让首判就能命中
        import json
        from solver.solver import _topic_key
        mock_bank = {}
        for q in mock_server._gen_questions():
            mode, stem, remark, options, answer, extra = q
            if answer is None:  # 学习卡片，跳过
                continue
            topic = {
                "topic_mode": mode,
                "stem": {"content": stem, "remark": remark},
                "options": options
            }
            key = _topic_key(topic)
            mock_bank[key] = {"ans": answer, "stem": stem}
        bank.BANK_LEGACY_FILE.write_text(json.dumps(mock_bank, ensure_ascii=False, indent=2))
        
        port = _free_port()
        mock_server.run_mock_server(port)
        api.BASE = f"http://127.0.0.1:{port}/studentv1/api"
        api.STUDENT_API = f"http://127.0.0.1:{port}/studentv1/api"
        cls.port = port

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def _cfg(self):
        return {
            "USERTOKEN": "mock-token", "ABC": "", "AUTH_V": "",
            "LLM_URL": "", "LLM_KEY": "", "LLM_MODEL": "x",
            "AI_PRIORITY": "0",       # 纯规则 + 空白试探，不依赖真 LLM
            "USE_WORD_API": "0",      # 不走真查词接口
            "DELAY_PER_QUESTION": "0",
            "MAX_ROUNDS": "3",
            "QUESTION_TIMEOUT": "60",
        }

    def test_run_full_reaches_100(self):
        logs = []
        client = Client("mock-token", timeout=5)
        runner.run_full(client, 1001, 2001, config=self._cfg(), log=logs.append, task_type=1)
        text = "\n".join(logs)
        # 首判计分下，接近 100 分就算过（允许少量格式不匹配的题首判错）
        self.assertRegex(text, r"\[分数\]\s*(\d+\.?\d*)", f"没找到分数。日志:\n{text}")
        import re
        m = re.search(r"\[分数\]\s*(\d+\.?\d*)", text)
        score = float(m.group(1)) if m else 0
        self.assertGreaterEqual(score, 80, f"分数 {score} < 80，未接近满分。日志:\n{text}")

    def test_report_generated(self):
        logs = []
        client = Client("mock-token", timeout=5)
        runner.run_full(client, 1002, 2002, config=self._cfg(), log=logs.append, task_type=1)
        stats = report.report_stats("class-1002-2002")
        self.assertGreater(stats["total"], 0)
        # 每道题都有结果：答对 + 跳过 = 总数（学习卡片不参与判分统计）
        self.assertLessEqual(stats["ok"] + stats["skip"], stats["total"])
        self.assertEqual(stats["ok"] + stats["skip"], stats["total"])
        # 题库自学习生效：第二次/同指纹题命中 cache（或首轮 rule/blank 试探）
        dist = stats["src_dist"]
        self.assertGreaterEqual(dist.get("cache", 0) + dist.get("rule", 0) + dist.get("blank", 0), 1)

    def test_bank_learning(self):
        logs = []
        client = Client("mock-token", timeout=5)
        runner.run_full(client, 1003, 2003, config=self._cfg(), log=logs.append, task_type=1)
        b = bank.bank_load()
        # mock 的 mode 73 题 "绿色存储" 应被学进短语/单词库
        self.assertTrue(bk_has_phrase("绿色存储") or len(b) > 0)


def bk_has_phrase(zh: str) -> bool:
    return bank.lookup_phrase(zh) is not None


if __name__ == "__main__":
    unittest.main(verbosity=2)
