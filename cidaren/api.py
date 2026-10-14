# -*- coding: utf-8 -*-
"""
api.py — 词达人 API 客户端（班级任务 + 自学任务双套，LangQi99/cidaren 主线）
- POST 签名 / GET 时间戳，统一 jv 解密，网络重试
- 新增 easy_cidaren 的查词接口（StudyWordInfo / SearchWord），供全题型规则匹配使用
"""
from __future__ import annotations

import os
import random
import time
import urllib.parse

import requests

from .crypto import SALT, decrypt_response, md5, sign

VERSION = "2.7.0.260507_01"
# 支持 CIDAREN_API_BASE / CIDAREN_STUDENT_API 环境变量覆盖（mock server 测试用）
BASE = os.environ.get("CIDAREN_API_BASE", "https://app.vocabgo.com/studentv1/api")
# easy_cidaren 的查词接口路径
STUDENT_API = os.environ.get("CIDAREN_STUDENT_API", "https://app.vocabgo.com/student/api/Student")

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36 "
    "NetType/WIFI MicroMessenger/7.0.20.1781(0x6700143B) "
    "WindowsWechat(0x63090a13) UnifiedPCWindowsWechat(0xf254173b) XWEB/19027 Flue"
)


class SecurityVerifyError(Exception):
    """服务端风控 11003：需用户在 App/微信端完成安全验证，重试无意义。"""


class ApiError(Exception):
    """通用 API 异常。"""


def _ms() -> int:
    return int(time.time() * 1000)


def _sleep(lo=1.0, hi=2.5):
    time.sleep(random.uniform(lo, hi))


class Client:
    def __init__(self, usertoken: str, abc: str = "", auth_v: str = "", ua: str = "", timeout: int = 12):
        self.timeout = timeout
        self.s = requests.Session()
        headers = {
            "host": "app.vocabgo.com",
            "usertoken": usertoken,
            "x-requested-with": "XMLHttpRequest",
            "accept": "application/json, text/plain, */*",
            "content-type": "application/json",
            "origin": "https://app.vocabgo.com",
            "referer": "https://app.vocabgo.com/student/",
            "user-agent": ua or DEFAULT_UA,
        }
        if abc:
            headers["abc"] = abc
        if auth_v:
            headers["authorization-v"] = auth_v
        self.s.headers.update(headers)

    # ---- 基础请求 ----
    def _get(self, path: str, params: dict, base: str = None, retries: int = 2):
        base = base or BASE  # 运行时取值，支持 mock 测试注入
        params = {**params, "timestamp": _ms(), "version": VERSION, "app_type": 1}
        last = None
        for attempt in range(retries):
            try:
                resp = self.s.get(base + path, params=params, timeout=self.timeout)
                return self._handle(resp)
            except (requests.Timeout, requests.ConnectionError) as e:
                last = e
                if attempt < retries - 1:
                    time.sleep(1.5 * (attempt + 1))
                    continue
        raise ApiError(f"GET {path} 网络失败: {last}")

    def _post(self, path: str, body: dict, base: str = None, retries: int = 2):
        base = base or BASE  # 运行时取值，支持 mock 测试注入
        body = {**body, "timestamp": _ms(), "version": VERSION}
        body["sign"] = sign(body)
        body["app_type"] = 1
        last = None
        for attempt in range(retries):
            try:
                resp = self.s.post(base + path, json=body, timeout=self.timeout)
                return self._handle(resp)
            except (requests.Timeout, requests.ConnectionError) as e:
                last = e
                if attempt < retries - 1:
                    time.sleep(1.5 * (attempt + 1))
                    continue
        raise ApiError(f"POST {path} 网络失败: {last}")

    def _handle(self, resp: requests.Response) -> dict:
        try:
            j = resp.json()
        except ValueError:
            raise ApiError(f"非 JSON 响应 (status={resp.status_code}): {resp.text[:200]}")
        j = decrypt_response(j)
        code = j.get("code")
        if code == 11003:
            raise SecurityVerifyError(f"服务端需安全验证: {j.get('msg', '')}")
        return j

    # ---- 班级任务 ----
    def page_task(self, page=1, size=50, search_type="0"):
        return self._post("/Student/ClassTask/PageTask",
                          {"search_type": search_type, "page_count": page, "page_size": size})

    def task_info(self, task_id, release_id):
        return self._get("/Student/ClassTask/Info", {"task_id": task_id, "release_id": release_id})

    def chose_word_list(self, task_id, task_type=1):
        return self._get("/Student/ClassTask/ChoseWordList",
                         {"task_id": task_id, "task_type": task_type})

    def submit_chose_word(self, task_id, word_map, task_type=1):
        return self._post("/Student/ClassTask/SubmitChoseWord",
                          {"task_id": task_id, "task_type": task_type,
                           "word_map": word_map, "chose_err_item": 1, "reset_chose_words": 1})

    def start_answer(self, task_id, release_id, task_type=1):
        return self._get("/Student/ClassTask/StartAnswer",
                         {"task_id": task_id, "task_type": task_type, "release_id": release_id,
                          "opt_img_w": 2300, "opt_font_size": 128, "opt_font_c": "#000000",
                          "it_img_w": 2702, "it_font_size": 144})

    def verify(self, topic_code, answer):
        return self._post("/Student/ClassTask/VerifyAnswer",
                          {"topic_code": topic_code, "answer": answer})

    def submit(self, topic_code, time_spent):
        return self._post("/Student/ClassTask/SubmitAnswerAndSave",
                          {"topic_code": topic_code, "time_spent": time_spent,
                           "opt_img_w": 2300, "opt_font_size": 128, "opt_font_c": "#000000",
                           "it_img_w": 2702, "it_font_size": 144})

    def skip(self, topic_code, time_spent=20000):
        return self.submit(topic_code, time_spent)

    def signin(self):
        return self._post("/Student/TaskStudentSignin/Do", {})

    # ---- 自学任务 ----
    def study_task_list(self, course_id="CET4_v2"):
        return self._get("/Student/StudyTask/List", {"course_id": course_id})

    def study_start_task(self, course_id, list_id, task_type=3, grade=2):
        return self._post("/Student/StudyTask/StartTask",
                          {"course_id": course_id, "list_id": list_id,
                           "task_type": task_type, "grade": grade})

    def study_task_info(self, task_id, course_id, list_id, task_type=3, grade=2):
        return self._get("/Student/StudyTask/Info",
                         {"task_id": task_id, "course_id": course_id, "list_id": list_id,
                          "task_type": task_type, "grade": grade})

    def study_chose_word_list(self, task_id, course_id, list_id, task_type=3, grade=2):
        return self._get("/Student/StudyTask/ChoseWordList",
                         {"task_id": task_id, "course_id": course_id, "list_id": list_id,
                          "task_type": task_type, "grade": grade})

    def study_submit_chose_word(self, task_id, course_id, list_id, word_map, task_type=3, grade=2):
        return self._post("/Student/StudyTask/SubmitChoseWord",
                          {"task_id": task_id, "task_type": task_type, "grade": grade,
                           "course_id": course_id, "list_id": list_id,
                           "word_map": word_map, "chose_err_item": 1, "reset_chose_words": 1})

    def study_start_answer(self, task_id, course_id, list_id, task_type=3, grade=2):
        return self._get("/Student/StudyTask/StartAnswer",
                         {"task_id": task_id, "task_type": task_type, "grade": grade,
                          "course_id": course_id, "list_id": list_id,
                          "opt_img_w": 2300, "opt_font_size": 128, "opt_font_c": "#000000",
                          "it_img_w": 2702, "it_font_size": 144})

    def study_verify(self, topic_code, answer):
        return self._post("/Student/StudyTask/VerifyAnswer",
                          {"topic_code": topic_code, "answer": answer})

    def study_submit(self, topic_code, time_spent):
        return self._post("/Student/StudyTask/SubmitAnswerAndSave",
                          {"topic_code": topic_code, "time_spent": time_spent,
                           "opt_img_w": 2300, "opt_font_size": 128, "opt_font_c": "#000000",
                           "it_img_w": 2702, "it_font_size": 144})

    # ---- 查词接口（easy_cidaren 思路，供规则匹配）----
    def query_word(self, course_id, list_id, word):
        """查询单词完整释义（含例句/短语/词义），响应已解密。"""
        params = {
            "course_id": course_id,
            "list_id": list_id,
            "word": word,
            "timestamp": _ms(),
            "version": "2.6.1.231204",
            "app_type": 1,
        }
        url = f"{STUDENT_API}/Course/StudyWordInfo"
        try:
            resp = self.s.get(url, params=params, timeout=self.timeout)
            return self._handle(resp)
        except (requests.Timeout, requests.ConnectionError):
            return {"code": 0}

    def search_word(self, word: str):
        """用官方接口把单词转原型（替代 easy 的 spaCy 依赖）。"""
        params = {
            "word": word,
            "timestamp": "1710396115786",
            "version": "2.6.2.24031302",
            "app_type": 1,
        }
        url = f"{STUDENT_API}/Course/SearchWord"
        try:
            resp = self.s.get(url, params=params, timeout=self.timeout)
            return self._handle(resp)
        except (requests.Timeout, requests.ConnectionError):
            return {"code": 0}


def token_valid(usertoken: str, abc: str = "", auth_v: str = "") -> tuple:
    """校验 token 有效性（复用 Student/Main）。返回 (ok, user_info)。"""
    c = Client(usertoken, abc, auth_v)
    try:
        resp = c._get("/Student/Main", {}, base=BASE)
    except ApiError:
        return False, None
    if resp.get("code") == 1:
        info = (resp.get("data") or {}).get("user_info") or {}
        return True, info
    return False, None
