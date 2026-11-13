#!/usr/bin/env node
/**
 * Offline verification for the go-wxpush variant of the user's patch.
 * Extracts isWxpushEnabled / logNotifyChannels / sendWxpush from the real source
 * via vm and fires real HTTP requests at a local mock /wxsend.
 * Focus: the new "HTTP 200 + errcode != 0 means failure" rule, no auth header,
 * and WXPUSH_BASE_URL -> base_url mapping.
 */
const fs = require("fs");
const vm = require("vm");
const http = require("http");

const JS_PATH =
  "C:/Users/Albot/WorkBuddy AI/2026-11-12-21-19-16/weread-build/src/weread-challenge.js";
const src = fs.readFileSync(JS_PATH, "utf8");
const start = src.indexOf("function isWxpushEnabled()");
const end = src.indexOf("function quoteShellArg", start);
if (start < 0 || end < 0) throw new Error("cannot locate wxpush block");
const snippet = src.slice(start, end);

const captured = [];
let reply = { status: 200, body: '{"errcode":0,"errmsg":"ok"}' };

const server = http.createServer((req, res) => {
  let body = "";
  req.on("data", (c) => (body += c));
  req.on("end", () => {
    captured.push({
      method: req.method,
      url: req.url,
      auth: req.headers.authorization,
      ctype: req.headers["content-type"],
      body,
    });
    res.writeHead(reply.status, { "Content-Type": "application/json" });
    res.end(reply.body);
  });
});

function makeSandbox(url, baseUrl = "", bark = "", email = true) {
  const logs = [];
  const sb = {
    WXPUSH_URL: url,
    WXPUSH_BASE_URL: baseUrl,
    BARK_KEY: bark,
    ENABLE_EMAIL: email,
    http: require("http"),
    https: require("https"),
    Buffer,
    URL,
    console: {
      info: (...a) => logs.push("INFO " + a.join(" ")),
      error: (...a) => logs.push("ERROR " + a.join(" ")),
    },
  };
  vm.createContext(sb);
  vm.runInContext(snippet, sb);
  return { sb, logs };
}

const results = [];
function check(name, ok, detail = "") {
  results.push({ name, ok });
  console.log(`${ok ? "PASS" : "FAIL"}  ${name}${detail ? "  -- " + detail : ""}`);
}

server.listen(0, "127.0.0.1", async () => {
  const port = server.address().port;
  const base = `http://127.0.0.1:${port}`;
  const SITE = "http://192.168.91.130:5566";
  try {
    // 1. basic request shape (no auth header anymore)
    {
      captured.length = 0;
      reply = { status: 200, body: '{"errcode":0,"errmsg":"ok"}' };
      const { sb, logs } = makeSandbox(base, "");
      const ok = await sb.sendWxpush("T1", "C1");
      const r = captured[0];
      check("bare host auto-appends /wxsend", ok === true && r && r.url === "/wxsend", r ? r.url : "-");
      check("POST + JSON content-type", r && r.method === "POST" && r.ctype === "application/json; charset=utf-8", r ? r.ctype : "-");
      check("NO Authorization header (go-wxpush has no auth)", r && r.auth === undefined, r ? String(r.auth) : "-");
      const p = JSON.parse(r.body);
      check("payload {title,content}, no base_url when unset", p.title === "T1" && p.content === "C1" && !("base_url" in p), r.body);
      check("success logged", logs.some((l) => l.includes("发送成功")), logs.join(" | "));
    }

    // 2. WXPUSH_BASE_URL -> base_url, trailing slash stripped
    {
      captured.length = 0;
      const { sb } = makeSandbox(base, SITE + "/");
      const ok = await sb.sendWxpush("T2", "C2");
      const p = JSON.parse(captured[0].body);
      check("WXPUSH_BASE_URL -> base_url, trailing slash stripped", ok === true && p.base_url === SITE, JSON.stringify(p));
    }

    // 3. endpoint variants
    for (const [label, url] of [
      ["explicit /wxsend", base + "/wxsend"],
      ["trailing slash", base + "/wxsend/"],
    ]) {
      captured.length = 0;
      const { sb } = makeSandbox(url, "");
      const ok = await sb.sendWxpush("T3", "C3");
      check(`endpoint: ${label} normalized`, ok === true && captured[0].url === "/wxsend", captured[0] ? captured[0].url : "-");
    }

    // 4. THE KEY NEW RULE: HTTP 200 but WeChat errcode != 0 must be a failure
    for (const [code, msg] of [[40001, "invalid credential"], [47003, "template param error"], [43004, "require subscribe"]]) {
      captured.length = 0;
      reply = { status: 200, body: JSON.stringify({ errcode: code, errmsg: msg }) };
      const { sb, logs } = makeSandbox(base, "");
      const ok = await sb.sendWxpush("T4", "C4");
      check(`HTTP 200 + errcode=${code} => false`, ok === false, String(ok));
      check(`HTTP 200 + errcode=${code} logged with code`, logs.some((l) => l.startsWith("ERROR") && l.includes(String(code))), logs.join(" | "));
    }

    // 5. HTTP 200 + errcode 0 => true (control)
    {
      reply = { status: 200, body: '{"errcode":0,"errmsg":"ok"}' };
      const { sb } = makeSandbox(base, "");
      check("HTTP 200 + errcode=0 => true", (await sb.sendWxpush("T5", "C5")) === true);
    }

    // 6. HTTP 200 + non-JSON body => true (parse failure is intentionally ignored)
    {
      reply = { status: 200, body: "ok" };
      const { sb } = makeSandbox(base, "");
      check("HTTP 200 + non-JSON => true (fallback)", (await sb.sendWxpush("T6", "C6")) === true);
    }

    // 7. HTTP 500 + {"error": ...} => false
    {
      reply = { status: 500, body: '{"error":"Failed to get access token"}' };
      const { sb, logs } = makeSandbox(base, "");
      const ok = await sb.sendWxpush("T7", "C7");
      check("HTTP 500 + error body => false", ok === false, String(ok));
      check("HTTP 500 status logged", logs.some((l) => l.startsWith("ERROR") && l.includes("500")), logs.join(" | "));
      reply = { status: 200, body: '{"errcode":0,"errmsg":"ok"}' };
    }

    // 8. long error body is truncated at 200 chars in the log
    {
      reply = { status: 400, body: "x".repeat(500) };
      const { sb, logs } = makeSandbox(base, "");
      await sb.sendWxpush("T8", "C8");
      const errLine = logs.find((l) => l.startsWith("ERROR")) || "";
      check("error response truncated to <=200 chars", errLine.length > 0 && !errLine.includes("x".repeat(201)), `len=${errLine.length}`);
      reply = { status: 200, body: '{"errcode":0,"errmsg":"ok"}' };
    }

    // 9. disabled
    {
      captured.length = 0;
      const { sb } = makeSandbox("", "");
      const ok = await sb.sendWxpush("T9", "C9");
      check("unconfigured => false, zero outbound", ok === false && captured.length === 0, `req=${captured.length}`);
    }

    // 10. channel log
    {
      const a = makeSandbox(base, SITE, "barkkey", true);
      a.sb.logNotifyChannels();
      check("logNotifyChannels: all enabled", a.logs.join(" ").includes("Bark=已启用") && a.logs.join(" ").includes("wxpush=已启用"), a.logs.join(" | "));
      const b = makeSandbox("", "", "", false);
      b.sb.logNotifyChannels();
      check("logNotifyChannels: all disabled", b.logs.join(" ").includes("wxpush=未启用") && b.logs.join(" ").includes("邮件=未启用"), b.logs.join(" | "));
    }

    // 11. connection refused
    {
      const { sb, logs } = makeSandbox("http://127.0.0.1:1", "");
      let threw = false, ok;
      try { ok = await sb.sendWxpush("T11", "C11"); } catch (e) { threw = true; }
      check("connection refused => false, never throws", threw === false && ok === false, `threw=${threw} ok=${ok}`);
      check("connection refused logged", logs.some((l) => l.startsWith("ERROR")), logs.join(" | "));
    }

    // 12. FIXED: URL with a query keeps the query and still hits /wxsend
    {
      captured.length = 0;
      const { sb } = makeSandbox(base + "/wxsend?foo=bar", "");
      const ok = await sb.sendWxpush("T12", "C12");
      check("FIXED: /wxsend?foo=bar preserved", ok === true && captured[0].url === "/wxsend?foo=bar", captured[0] ? captured[0].url : "-");
    }

    // 13. FIXED: bare host with a query also appends /wxsend correctly
    {
      captured.length = 0;
      const { sb } = makeSandbox(base + "/?foo=bar", "");
      const ok = await sb.sendWxpush("T13", "C13");
      check("FIXED: host/?foo=bar -> /wxsend?foo=bar", ok === true && captured[0].url === "/wxsend?foo=bar", captured[0] ? captured[0].url : "-");
    }

    // 14. invalid URL -> false, logged, never throws
    {
      const { sb, logs } = makeSandbox("not-a-url", "");
      let threw = false, ok;
      try { ok = await sb.sendWxpush("T14", "C14"); } catch (e) { threw = true; }
      check("invalid URL => false, logged, no throw",
        threw === false && ok === false && logs.some((l) => l.includes("地址无效")),
        `threw=${threw} ok=${ok} logs=${logs.join(" | ")}`);
    }
  } catch (e) {
    check("harness", false, String(e && e.stack ? e.stack : e));
  } finally {
    server.close();
    const pass = results.filter((r) => r.ok).length;
    console.log(`\nRESULT: ${pass}/${results.length} PASS`);
    process.exit(pass === results.length ? 0 : 1);
  }
});
