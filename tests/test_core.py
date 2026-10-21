# -*- coding: utf-8 -*-
"""cidaren-ultimate 核心逻辑单元测试（不依赖网络/真实 token）。"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import crypto, bank
from solver import solver
from core.ai import parse_llm_answer, check_llm_connectivity
from solver.solver import (_build_phrase_answer, _complete_spelling, _match_tips_words,
                            _template_slots, _topic_key, _is_collocation)
from core.config import set_env_file, get_runtime_config


class TestCrypto(unittest.TestCase):
    def test_sign(self):
        # 已知样例：easy 的 sign 生成方式 md5(sorted k=v + salt)
        params = {"answer": 1, "topic_code": "abc", "timestamp": 123}
        s = crypto.sign(params)
        self.assertEqual(len(s), 32)

    def test_decrypt_jv1(self):
        # jv=1：去掉前32位后 base64
        inner = json.dumps({"code": 1, "data": {"a": 1}}, ensure_ascii=False)
        import base64
        payload = "X" * 32 + base64.b64encode(inner.encode()).decode()
        out = crypto.decrypt_data(payload, "1")
        self.assertEqual(out, {"code": 1, "data": {"a": 1}})

    def test_decrypt_jv2_ranges(self):
        # 用区间表验证 strip_noise 与索引表等价（同一 jv 两套规则应得相同结果）
        # 造一个 2_9214 数据：随机字符串中删掉索引位后 base64
        import base64
        inner = json.dumps({"code": 1, "ok": True}, ensure_ascii=False).encode()
        raw = base64.b64encode(inner).decode()
        indexes = crypto.JV_TWO["2_9214"]
        chars = list(raw)
        for i in indexes:
            if 0 <= i < len(chars):
                chars.insert(i, "Z")
        noisy = "".join(chars)
        out = crypto.decrypt_data(noisy, "2_9214")
        self.assertEqual(out.get("ok"), True)

    def test_decrypt_jv_plain_json(self):
        out = crypto.decrypt_data('{"code":1}', "")
        self.assertEqual(out, {"code": 1})


class TestBank(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        # 全部文件常量重定向到临时目录，严禁触碰真实 data/
        for attr in ("DATA_DIR", "BANK_FILE", "ANSWER_LIB_FILE", "WORD_POOL_FILE", "BANK_LEGACY_FILE"):
            if hasattr(bank, attr):
                setattr(bank, attr, tmp / Path(getattr(bank, attr)).name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_bank_roundtrip(self):
        bank.bank_add("1::a::b::c", 2, "stem")
        self.assertEqual(bank.bank_load()["1::a::b::c"]["ans"], 2)

    def test_bank_lookup_legacy(self):
        # 模拟旧格式题库（3 段 key）
        bank.bank_save({})
        legacy = bank.BANK_LEGACY_FILE
        import json as _json
        legacy.write_text(_json.dumps({
            "11::he is a man who will spoil rather than {accomplish} things.::vt. 完成|vt. 使": {"ans": 0, "stem": ""}
        }), encoding="utf-8")
        new_key = "11::norm::he is a man who will spoil rather than {accomplish} things.::vt. 完成|vt. 使::opts"
        b = bank.bank_load()
        self.assertEqual(bank.bank_lookup(new_key, b), 0)
        legacy.unlink()

    def test_bank_lookup_semantic_remark(self):
        """32/73 型题语义兜底：题干占位/选项变化导致指纹失配时，按 mode+remark 仍能命中。"""
        bank.bank_save({})
        # 第 1 轮存下的旧格式 key：题干占位 + 中文 remark + 选项
        bank.bank_add("32::norm::____的邮政包裹::邮政包裹::a|b|c", ["postal", "parcel"], "旧题干")
        # 第 2 轮服务端重刷：题干占位变了、选项顺序变了，但中文 remark 不变
        b = bank.bank_load()
        new_key = "32::norm::_____的邮___包裹::邮政包裹::c|a|b"
        self.assertEqual(bank.bank_lookup(new_key, b), ["postal", "parcel"])
        # 跨题型不误命中（mode 不同）
        other_key = "73::norm::____的邮政包裹::邮政包裹::a|b"
        self.assertIsNone(bank.bank_lookup(other_key, b))

    def test_phrase_lib(self):
        bank.add_phrase("汉译", ["in,many,instances"])
        self.assertEqual(bank.lookup_phrase("汉译"), ["in,many,instances"])

    def test_word_lib(self):
        bank.add_words("释义", ["responsible", "conscious"])
        self.assertEqual(bank.lookup_word("释义"), ["responsible", "conscious"])

    def test_lib_stats(self):
        bank.add_phrase("p", ["a"])
        bank.add_words("w", ["b"])
        s = bank.lib_stats()
        self.assertEqual(s["phrase"], 1)
        self.assertEqual(s["word"], 1)


class TestSolverPure(unittest.TestCase):
    def test_template_slots(self):
        slots, total = _template_slots("a _ b _-_ c")
        self.assertEqual(slots, ["_", "_-_"])
        self.assertEqual(total, 3)

    def test_topic_key(self):
        t = {"topic_mode": 31, "stem": {"content": "word", "remark": [{"relation": "in"}]},
             "options": [{"content": "in"}, {"content": "out"}]}
        self.assertTrue(_is_collocation(t))
        key = _topic_key(t)
        self.assertIn("coll", key)

    def test_match_tips_words(self):
        ans = _match_tips_words(["gr", "sto"], [8, 7], ["green", "storage", "waste"], exclude=set())
        # green(5) 不匹配 8；storage(7) 匹配 sto
        self.assertIsNone(ans)
        ans2 = _match_tips_words(["gr", "sto"], [5, 7], ["green", "storage"])
        self.assertEqual(ans2, ["green", "storage"])

    def test_build_phrase_answer(self):
        topic = {
            "topic_mode": 32,
            "stem": {"content": "___ _", "remark": "绿色制造"},
            "options": [{"content": "green"}, {"content": "manufacturing"}, {"content": "the"}],
        }
        ctx = type("Ctx", (), {})()
        out = _build_phrase_answer(ctx, topic, "green manufacturing")
        self.assertIn("green", out)
        self.assertIn("manufacturing", out)

    def test_parse_llm_answer(self):
        self.assertEqual(parse_llm_answer("  0  ", 11), 0)
        self.assertEqual(parse_llm_answer("B. responsible", 51), "responsible")
        self.assertEqual(parse_llm_answer('["a","b"]', 73), ["a", "b"])
        self.assertEqual(parse_llm_answer("in,many,instances", 32), "in,many,instances")

    def test_complete_spelling_no_crash(self):
        topic = {
            "topic_mode": 73,
            "stem": {"content": "{gr}{sto}", "remark": "绿色存储"},
            "w_lens": [5, 7],
            "options": [],
        }
        ctx = type("Ctx", (), {"word_dict": {}, "word_list": []})()
        self.assertIsNone(_complete_spelling(ctx, topic))

    def test_infer_lens_from_stem(self):
        """数下划线占位推断每空字母数（服务器没给 w_lens 时的兜底）。"""
        # 空格分隔的下划线串：6 个空位
        self.assertEqual(solver._infer_lens_from_stem("abandon: a _ _ _ _ _ _"), [6])
        # 连续下划线
        self.assertEqual(solver._infer_lens_from_stem("a ____ b"), [4])
        # 全角下划线
        self.assertEqual(solver._infer_lens_from_stem("＿ ＿ ＿"), [3])
        # 多空位
        self.assertEqual(solver._infer_lens_from_stem("green ___ 存储 __"), [3, 2])
        # 无下划线
        self.assertIsNone(solver._infer_lens_from_stem("green manufacturing"))
        # 空输入
        self.assertIsNone(solver._infer_lens_from_stem(""))

    def test_complete_spelling_infer_len(self):
        """w_lens 缺失时，从题干下划线推断长度筛选候选。"""
        from solver.solver import _candidate_words
        topic = {
            "topic_mode": 73,
            "stem": {"content": "绿色存储: g _ _ _ _ _", "remark": "绿色存储"},
            "w_lens": [],          # 服务器没给长度
            "options": [],
        }
        ctx = type("Ctx", (), {"word_dict": {"绿色存储": "green storage"},
                               "word_list": ["green", "storage"]})()
        # 候选池应只留下 5 字母且以 g 开头的单词
        cands = _candidate_words(ctx, "绿色存储")
        self.assertIn("green", cands)
        # _complete_spelling 通过推断长度 [5] 严格匹配 green
        self.assertEqual(json.loads(_complete_spelling(ctx, topic)), ["green"])

    def test_complete_sentence_infer_len(self):
        """mode 51-54 无 w_len 时用推断长度匹配。"""
        from solver.solver import _complete_sentence
        topic = {
            "topic_mode": 51,
            "stem": {"content": "g _ _ _ _ _", "remark": ""},
            "w_tip": "g",
        }
        ctx = type("Ctx", (), {"word_list": ["green", "grey", "great"]})()
        self.assertEqual(_complete_sentence(ctx, topic), "green")


class TestLLMConnectivity(unittest.TestCase):
    """AI 连接测试函数（Web「测试 AI 连接」按钮后端）。"""

    def test_missing_url(self):
        r = check_llm_connectivity("", "sk-x")
        self.assertFalse(r["ok"])
        self.assertIn("LLM_URL", r["error"])

    def test_missing_key(self):
        r = check_llm_connectivity("https://api.deepseek.com", "")
        self.assertFalse(r["ok"])
        self.assertIn("LLM_KEY", r["error"])

    def test_success(self):
        from unittest import mock
        class FakeResp:
            status_code = 200
            def json(self):
                return {"choices": [{"message": {"content": "正常"}}]}
            @property
            def text(self):
                return "ok"
        with mock.patch("core.ai.requests.post", return_value=FakeResp()) as m:
            r = check_llm_connectivity("https://api.deepseek.com", "sk-test", "deepseek-chat")
        self.assertTrue(r["ok"])
        self.assertEqual(r["reply"], "正常")
        self.assertGreaterEqual(r["latency_ms"], 0)

    def test_max_tokens_string_normalized(self):
        """前端传字符串 max_tokens（如 \"1024\"）必须转成整数再发出，否则严格网关报 422。"""
        from unittest import mock
        class FakeResp:
            status_code = 200
            def json(self):
                return {"choices": [{"message": {"content": "正常"}}]}
            @property
            def text(self):
                return "ok"
        with mock.patch("core.ai.requests.post", return_value=FakeResp()) as m:
            r = check_llm_connectivity("https://api.deepseek.com", "sk-test", "deepseek-chat",
                                      max_tokens="1024")
        self.assertTrue(r["ok"])
        sent = m.call_args.kwargs["json"]
        self.assertIsInstance(sent["max_tokens"], int)
        self.assertEqual(sent["max_tokens"], 1024)

    def test_unauthorized(self):
        from unittest import mock
        class Fake401:
            status_code = 401
            @property
            def text(self):
                return "unauthorized"
        with mock.patch("core.ai.requests.post", return_value=Fake401()):
            r = check_llm_connectivity("https://api.deepseek.com", "sk-bad")
        self.assertFalse(r["ok"])
        self.assertIn("401", r["error"])


class TestConfig(unittest.TestCase):
    def test_env_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, "x.env")
            set_env_file(p)
            with open(p, "w", encoding="utf-8") as f:
                f.write('USERTOKEN="abc"\nLLM_URL="https://api.deepseek.com"\n')
            cfg = get_runtime_config()
            self.assertEqual(cfg["USERTOKEN"], "abc")
            self.assertEqual(cfg["LLM_URL"], "https://api.deepseek.com")


if __name__ == "__main__":
    unittest.main(verbosity=2)
