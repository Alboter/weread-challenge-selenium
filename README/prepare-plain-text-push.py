#!/usr/bin/env python3
"""Turn the 6 wxpush notification bodies into short, itemised plain-text lists.

Why: the WeChat test-account template was registered with a FIXED content
("消息推送") and no {{xxx.DATA}} placeholder, so the card never showed our
title/content -- users had to open the /detail page. Once the template is
fixed, these bodies render directly in the message list, like the QQ Mail
notification does.

This script only rewrites weread-build/src/weread-challenge.js (the stable
build source). It never touches the Downloads attachment snapshot.
"""

import hashlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DST = ROOT / "weread-build" / "src" / "weread-challenge.js"

# (label, exact old snippet, exact new snippet)
PATCHES = [
    (
        "login QR",
        r'''      sendWxpush(
        "微信读书挑战 - 请扫码登录",
        `检测到新的登录二维码，请尽快在手机端扫码登录：${loginUrl}`
      )''',
        r'''      sendWxpush(
        "微信读书挑战 - 请扫码登录",
        wxpushBody(
          "状态：检测到新登录二维码",
          "操作：请在手机端尽快扫码",
          `链接：${loginUrl}`
        )
      )''',
    ),
    (
        "script start",
        r'''    await sendWxpush("微信读书挑战 - 脚本启动", "自动阅读脚本开始运行");''',
        r'''    await sendWxpush("微信读书挑战 - 脚本启动", wxpushBody(
      "状态：自动阅读脚本已启动",
      `计划时长：${WEREAD_DURATION} 分钟`
    ));''',
    ),
    (
        "login failed",
        r'''      await sendWxpush("微信读书挑战 - 登录失败", "登录失败，请检查登录状态或重新扫码登录");''',
        r'''      await sendWxpush("微信读书挑战 - 登录失败", wxpushBody(
        "状态：登录失败，本次阅读未执行",
        "操作：请检查登录状态或重新扫码"
      ));''',
    ),
    (
        "login ok",
        r'''    await sendWxpush("微信读书挑战 - 登录成功", "登录成功，开始阅读");''',
        r'''    await sendWxpush("微信读书挑战 - 登录成功", wxpushBody(
      "状态：登录成功，开始阅读",
      `计划时长：${WEREAD_DURATION} 分钟`
    ));''',
    ),
    (
        "reading done",
        r'''    await sendWxpush("微信读书挑战 - 阅读完成", `阅读完成，持续时间：${WEREAD_DURATION}分钟`);''',
        r'''    await sendWxpush("微信读书挑战 - 阅读完成", wxpushBody(
      "状态：本次阅读正常结束",
      `本次时长：${WEREAD_DURATION} 分钟`,
      "结果：已完成，邮件同步发送"
    ));''',
    ),
    (
        "error",
        r'''    await sendWxpush(
      "微信读书挑战 - 发生错误",
      `发生错误：${errorMessage.substring(0, 100)}${errorMessage.length > 100 ? '...' : ''}`
    );''',
        r'''    await sendWxpush(
      "微信读书挑战 - 发生错误",
      wxpushBody(
        "状态：运行异常，本次阅读中断",
        `详情：${errorMessage.substring(0, 60)}${errorMessage.length > 60 ? "..." : ""}`
      )
    );''',
    ),
]


def main():
    text = DST.read_text(encoding="utf-8")
    print("SOURCE : %s" % DST)
    print("BEFORE : %d bytes  sha256=%s" %
          (len(text.encode()), hashlib.sha256(text.encode()).hexdigest()))

    # wxpushBody() must already exist (added to the stable build source).
    if text.count("function wxpushBody(...lines)") != 1:
        print("ERROR: wxpushBody() helper is missing from the stable source")
        return 1

    for label, old, new in PATCHES:
        n = text.count(old)
        if n != 1:
            print("ERROR: [%s] expected exactly 1 occurrence, found %d" % (label, n))
            return 1
        text = text.replace(old, new, 1)
        print("  OK   patched %s" % label)

    checks = {
        "wxpushBody definition": text.count("function wxpushBody(...lines)") == 1,
        "6 call sites use wxpushBody": text.count("wxpushBody(") == 7,
        # The same short phrases also appear in the Bark calls, so match the
        # exact OLD wxpush snippets instead of the bare string literals.
        "old one-line bodies gone": all(old not in text for _, old, _ in PATCHES),
        "privacy flag kept": "WEREAD_AGREE_TERMS" in text,
        "sendWxpush kept": "async function sendWxpush(title, content)" in text,
    }
    print("\n--- checks ---")
    for k, v in checks.items():
        print("  %s %s" % ("OK  " if v else "FAIL", k))
    if not all(checks.values()):
        return 1

    DST.write_text(text, encoding="utf-8")
    final = DST.read_text(encoding="utf-8")
    print("\nAFTER  : %d bytes  sha256=%s" %
          (len(final.encode()), hashlib.sha256(final.encode()).hexdigest()))
    print("Run: node --check \"%s\"" % DST)
    return 0


if __name__ == "__main__":
    sys.exit(main())
