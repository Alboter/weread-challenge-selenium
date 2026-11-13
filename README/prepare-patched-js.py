#!/usr/bin/env python3
"""Produce the patched weread-challenge.js in a STABLE workspace path.

The file the user attaches at C:/Users/Albot/Downloads/weread-challenge.js is a
host-managed attachment snapshot: it can be restored to its original bytes
between turns. Never use it directly as a build source. This script reads that
snapshot, applies exactly two verified patches, and writes the result to
  weread-build/src/weread-challenge.js
which is the single source of truth for the Docker image.

Both patches must match exactly once; otherwise the script aborts.
"""

import hashlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SRC = Path("C:/Users/Albot/Downloads/weread-challenge.js")
DST = ROOT / "weread-build" / "src" / "weread-challenge.js"

PATCHES = [
    (
        "P1: normalize endpoint via URL() so query strings are not mangled",
        b'''  // \xe5\x85\x81\xe8\xae\xb8\xe5\x8f\xaa\xe5\xa1\xab\xe5\x9f\x9f\xe5\x90\x8d\xef\xbc\x8c\xe4\xb9\x9f\xe5\x85\x81\xe8\xae\xb8\xe7\x9b\xb4\xe6\x8e\xa5\xe5\xa1\xab\xe5\x88\xb0 /wxsend
  let endpoint = WXPUSH_URL.replace(/\\/+$/, "");
  if (!/\\/wxsend$/.test(endpoint)) {
    endpoint += "/wxsend";
  }
''',
        None,  # filled below with UTF-8 literal
    ),
]

# UTF-8 literals are clearer than escaped bytes for the Chinese comments.
P1_OLD = (
    '  // 允许只填域名，也允许直接填到 /wxsend\n'
    '  let endpoint = WXPUSH_URL.replace(/\\/+$/, "");\n'
    '  if (!/\\/wxsend$/.test(endpoint)) {\n'
    '    endpoint += "/wxsend";\n'
    '  }\n'
).encode("utf-8")

P1_NEW = (
    '  // 允许只填域名、带路径、带 /wxsend 或带 query。\n'
    '  // 必须用 URL 先分离 pathname 再规范化，否则带 query 的地址会被拼成\n'
    '  // "/wxsend?foo=bar/wxsend"（把 /wxsend 追加到查询串末尾）。\n'
    '  let endpoint;\n'
    '  try {\n'
    '    const target = new URL(WXPUSH_URL);\n'
    '    let pathname = target.pathname.replace(/\\/+$/, "");\n'
    '    if (!/\\/wxsend$/.test(pathname)) {\n'
    '      pathname += "/wxsend";\n'
    '    }\n'
    '    target.pathname = pathname;\n'
    '    endpoint = target.toString();\n'
    '  } catch (error) {\n'
    '    console.error("wxpush地址无效:", error.message);\n'
    '    return false;\n'
    '  }\n'
).encode("utf-8")

P2_OLD = (
    '  logNotifyChannels();\n'
    '\n'
    '  // 发送脚本启动通知\n'
    '  await sendBark("微信读书挑战", "自动阅读脚本开始运行", {\n'
    '    subtitle: "脚本启动",\n'
    '    level: "active",\n'
    '    sound: "beginning"\n'
    '  });\n'
    '  await sendWxpush("微信读书挑战 - 脚本启动", "自动阅读脚本开始运行");\n'
    '  try {\n'
    '    const capabilities = {\n'
).encode("utf-8")

P2_NEW = (
    '  try {\n'
    '    logNotifyChannels();\n'
    '\n'
    '    // 发送脚本启动通知（放在 try 内：通知异常会走统一错误处理，\n'
    '    // 不会绕过 catch 直接终止进程、导致阅读流程被整段跳过）\n'
    '    await sendBark("微信读书挑战", "自动阅读脚本开始运行", {\n'
    '      subtitle: "脚本启动",\n'
    '      level: "active",\n'
    '      sound: "beginning"\n'
    '    });\n'
    '    await sendWxpush("微信读书挑战 - 脚本启动", "自动阅读脚本开始运行");\n'
    '\n'
    '    const capabilities = {\n'
).encode("utf-8")

EDITS = [
    ("P1 URL() endpoint normalization", P1_OLD, P1_NEW),
    ("P2 startup notifications inside try", P2_OLD, P2_NEW),
]


def main():
    data = SRC.read_bytes()
    src_hash = hashlib.sha256(data).hexdigest()
    print("SOURCE :", SRC)
    print("        sha256=%s  %d bytes" % (src_hash, len(data)))

    for name, old, new in EDITS:
        hits = data.count(old)
        if hits != 1:
            print("ERROR: %s matched %d times (expected exactly 1)" % (name, hits))
            return 2
        data = data.replace(old, new, 1)
        print("APPLIED: %s" % name)

    # Guard: the patched result must not still contain the old buggy line.
    if b'endpoint += "/wxsend";' in data:
        print("ERROR: old endpoint expression still present")
        return 2

    DST.parent.mkdir(parents=True, exist_ok=True)
    DST.write_bytes(data)
    out_hash = hashlib.sha256(data).hexdigest()
    print("OUTPUT :", DST)
    print("        sha256=%s  %d bytes" % (out_hash, len(data)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
