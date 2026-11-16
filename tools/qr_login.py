# -*- coding: utf-8 -*-
"""Interactive Cidaren OAuth V2 login for headless terminals.

Run ``python tools/qr_login.py``, open the printed authorization URL in
WeChat, then paste the final ``https://app.vocabgo.com/student/?...`` URL
back into the terminal.
"""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from core.login import complete_login, login_start


def _print_qr(value: str) -> None:
    try:
        import qrcode

        qr = qrcode.QRCode(border=1)
        qr.add_data(value)
        qr.make(fit=True)
        qr.print_ascii(invert=True)
    except Exception:
        pass


def main() -> int:
    session = login_start()
    print("AUTH_URL:", session["authorization_url"], flush=True)
    _print_qr(session["authorization_url"])
    print("微信授权完成后，粘贴地址栏里的完整 app.vocabgo.com 回调 URL。", flush=True)
    callback_url = input("CALLBACK_URL> ").strip()
    if not callback_url:
        print("RESULT: TIMEOUT / empty callback", flush=True)
        return 1
    try:
        result = complete_login(
            callback_url,
            session["state_digest"],
            session["marker_digest"],
        )
    except Exception as exc:
        print(f"RESULT: ERROR {exc}", flush=True)
        return 1
    print(f"RESULT: OK name={result['name']} version={result['version']}", flush=True)
    print(f"USERTOKEN: {result['token']}", flush=True)
    print(f"CRYPTO_FILE: {result['crypto_file']}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
