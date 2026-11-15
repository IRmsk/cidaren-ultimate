# -*- coding: utf-8 -*-
"""生成微信授权链接并轮询，token 生效后写回 .env（无浏览器场景用）"""
import sys, time, json, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from core.login import qr_authorize, is_token_valid
from core.config import get_runtime_config, save_runtime_config

if len(sys.argv) > 1 and sys.argv[1] == "--wait":
    tok = sys.argv[2]
    print("USER_TOKEN:", tok, "(等待已有授权链接)", flush=True)
else:
    s = qr_authorize()
    tok, url = s["user_token"], s["redirect_url"]
    print("USER_TOKEN:", tok, flush=True)
    print("AUTH_URL:", url, flush=True)

deadline = time.time() + 900
n = 0
while time.time() < deadline:
    n += 1
    ok, info = is_token_valid(tok)
    if ok:
        cfg = get_runtime_config()
        cfg["USERTOKEN"] = tok
        save_runtime_config(cfg)
        name = (info or {}).get("student_name") or (info or {}).get("nick_name") or "?"
        print(f"RESULT: OK name={name} user_token={tok} polls={n}", flush=True)
        sys.exit(0)
    if n % 6 == 0:
        print(f"...等待授权中 ({n} 次轮询)", flush=True)
    time.sleep(5)
print("RESULT: TIMEOUT", flush=True)
