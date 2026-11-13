// 从源码层面移除所有对外遥测 / 第三方上报。
// 每次替换都会校验命中次数；源码变更导致不匹配时构建会直接失败，避免"以为打过补丁"。
import fs from "node:fs";

const target = process.argv[2] || "weread-challenge.js";
let src = fs.readFileSync(target, "utf8");

const edits = [
  {
    name: "遥测默认值改为关闭",
    from: "let WEREAD_AGREE_TERMS = true; // Agree to terms",
    to: "let WEREAD_AGREE_TERMS = false; // harden: telemetry disabled by default",
    count: 1,
  },
  {
    name: "忽略 WEREAD_AGREE_TERMS 环境变量，强制关闭遥测",
    from:
      "  WEREAD_AGREE_TERMS = env.WEREAD_AGREE_TERMS === undefined\n" +
      "    ? true\n" +
      "    : parseBooleanValue(env.WEREAD_AGREE_TERMS, true, false);",
    to: "  WEREAD_AGREE_TERMS = false; // harden: never allow remote telemetry",
    count: 1,
  },
  {
    name: "统计上报地址改为本地黑洞",
    from: '    : "https://weread-challenge.techfetch.dev/logs";',
    to: '    : "http://127.0.0.1:9/logs"; // harden: black hole',
    count: 1,
  },
  {
    name: "上报请求强制走本地 http，不再外发",
    from: "  const httpModule = DEBUG ? http : https;",
    to: "  const httpModule = http; // harden: local only",
    count: 1,
  },
  {
    name: "移除登录链接外发到第三方二维码服务",
    from: "https://api.qrserver.com/v1/create-qr-code/",
    to: "http://127.0.0.1:9/qr/",
    count: 2,
  },
  {
    name: "Bark 默认服务器改为本地黑洞",
    from: '"https://api.day.app"',
    to: '"http://127.0.0.1:9"',
    count: 2,
  },
];

for (const e of edits) {
  const hits = src.split(e.from).length - 1;
  if (hits !== e.count) {
    console.error(
      `[FAIL] ${e.name}: 期望 ${e.count} 处匹配，实际 ${hits} 处。源码可能已更新，请重新人工审计后再构建。`
    );
    process.exit(1);
  }
  src = src.split(e.from).join(e.to);
}

const forbidden = [
  "weread-challenge.techfetch.dev",
  "api.qrserver.com",
  "api.day.app",
];
const remaining = forbidden.filter((s) => src.includes(s));
if (remaining.length) {
  console.error("[FAIL] 仍存在第三方域名: " + remaining.join(", "));
  process.exit(1);
}

fs.writeFileSync(target, src, "utf8");
console.log("[OK] 已移除全部第三方上报点：" + edits.length + " 类，共 " +
  edits.reduce((n, e) => n + e.count, 0) + " 处");
console.log("[OK] 仅保留访问 weread.qq.com 与 Selenium 所需的网络请求");
