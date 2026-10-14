# -*- coding: utf-8 -*-
"""cidaren-ultimate 核心逻辑单元测试（不依赖网络/真实 token）。"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cidaren import crypto, bank, solver
from cidaren.ai import parse_llm_answer
from cidaren.solver import (_build_phrase_answer, _complete_spelling, _match_tips_words,
                            _template_slots, _topic_key, _is_collocation)
from cidaren.config import set_env_file, get_runtime_config


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
