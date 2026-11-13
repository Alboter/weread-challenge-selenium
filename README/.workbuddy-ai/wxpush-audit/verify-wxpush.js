#!/usr/bin/env node
/**
 * Offline verification for the user's wxpush patch.
 * Extracts isWxpushEnabled / logNotifyChannels / sendWxpush from the real source
 * via vm, then fires real HTTP requests against a local mock /wxsend server.
 * No production changes, no outbound network.
 */
const fs = require("fs");
const vm = require("vm");
const http = require("http");

const JS_PATH =
  "C:/Users/Albot/Documents/GitHub/weread-challenge-selenium/src/weread-challenge.js";
const src = fs.readFileSync(JS_PATH, "utf8");
const start = src.indexOf("function isWxpushEnabled()");
const end = src.indexOf("function quoteShellArg", start);
if (start < 0 || end < 0) throw new Error("cannot locate wxpush block");
const snippet = src.slice(start, end);

const captured = [];
let failMode = false;

const server = http.createServer((req, res) => {
  let body = "";
  req.on("data", (c) => (body += c));
  req.on("end", () => {
    captured.push({
      method: req.method,
      url: req.url,
      auth: req.headers.authorization,
      ctype: req.headers["content-type"],
      ua: req.headers["user-agent"],
      body,
    });
    if (failMode) {
      res.writeHead(400, { "Content-Type": "application/json" });
      res.end(JSON.stringify({ msg: "Missing required parameters" }));
    } else {
      res.writeHead(200, { "Content-Type": "application/json" });
      res.end(JSON.stringify({ msg: "Successfully sent messages to 1 user(s)" }));
    }
  });
});

function makeSandbox(url, token, bark = "", email = true) {
  const logs = [];
  const sb = {
    WXPUSH_URL: url,
    WXPUSH_TOKEN: token,
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
  results.push({ name, ok, detail });
  console.log(`${ok ? "PASS" : "FAIL"}  ${name}${detail ? "  -- " + detail : ""}`);
}

server.listen(0, "127.0.0.1", async () => {
  const port = server.address().port;
  const base = `http://127.0.0.1:${port}`;
  const TOKEN = "unit-test-token-9f3a";
  const LOGIN_URL = "https://weread.qq.com/login?code=abc123";

  try {
    // 1. bare host -> auto append /wxsend
    {
      captured.length = 0;
      const { sb } = makeSandbox(base, TOKEN);
      const ok = await sb.sendWxpush("T1", "C1");
      const r = captured[0];
      check("bare host auto-appends /wxsend", ok === true && r && r.url === "/wxsend", r ? r.url : "no request");
      check("method+headers", r && r.method === "POST" && r.ctype === "application/json; charset=utf-8", r ? r.ctype : "-");
      check("Authorization = raw token", r && r.auth === TOKEN, r ? r.auth : "-");
      const p = JSON.parse(r.body);
      check("payload {title,content}", p.title === "T1" && p.content === "C1" && !("base_url" in p), r.body);
    }

    // 2. explicit /wxsend
    {
      captured.length = 0;
      const { sb } = makeSandbox(base + "/wxsend", TOKEN);
      const ok = await sb.sendWxpush("T2", "C2");
      check("explicit /wxsend not duplicated", ok === true && captured[0].url === "/wxsend", captured[0] ? captured[0].url : "-");
    }

    // 3. trailing slash
    {
      captured.length = 0;
      const { sb } = makeSandbox(base + "/wxsend/", TOKEN);
      const ok = await sb.sendWxpush("T3", "C3");
      check("trailing slash normalized", ok === true && captured[0].url === "/wxsend", captured[0] ? captured[0].url : "-");
    }

    // 4. options.url -> base_url
    {
      captured.length = 0;
      const { sb } = makeSandbox(base, TOKEN);
      const ok = await sb.sendWxpush("微信读书挑战 - 请扫码登录", "检测到新的登录二维码，请尽快扫码", { url: LOGIN_URL });
      const p = JSON.parse(captured[0].body);
      check("options.url maps to base_url", ok === true && p.base_url === LOGIN_URL, JSON.stringify(p));
    }

    // 5. query string preserved in path
    {
      captured.length = 0;
      const { sb } = makeSandbox(base + "/wxsend?foo=bar", TOKEN);
      const ok = await sb.sendWxpush("T5", "C5");
      check("query string preserved in request path", ok === true && captured[0].url === "/wxsend?foo=bar", captured[0] ? captured[0].url : "-");
    }

    // 6. server 400 -> false, error logged
    {
      captured.length = 0;
      failMode = true;
      const { sb, logs } = makeSandbox(base, TOKEN);
      const ok = await sb.sendWxpush("T6", "C6");
      check("HTTP 400 resolves false", ok === false, String(ok));
      check("HTTP 400 logs status + body", logs.some((l) => l.startsWith("ERROR") && l.includes("400")), logs.join(" | "));
      failMode = false;
    }

    // 7. disabled -> no request, no exception
    {
      captured.length = 0;
      const { sb } = makeSandbox("", "");
      const ok = await sb.sendWxpush("T7", "C7");
      check("unconfigured => false, zero outbound", ok === false && captured.length === 0, `req=${captured.length}`);
    }

    // 8. logNotifyChannels: 3 states, never leaks the token
    {
      const full = makeSandbox(base, TOKEN, "barkkey", true);
      full.sb.logNotifyChannels();
      const l1 = full.logs.join(" | ");
      check("logNotifyChannels: all enabled", l1.includes("Bark=已启用") && l1.includes("wxpush=已启用") && l1.includes("邮件=已启用"), l1);
      check("logNotifyChannels: no token leaked", !l1.includes(TOKEN), l1);

      const partial = makeSandbox(base, "", "", false);
      partial.sb.logNotifyChannels();
      const l2 = partial.logs.join(" | ");
      check("logNotifyChannels: partial config hint", l2.includes("需同时设置"), l2);

      const none = makeSandbox("", "", "", false);
      none.sb.logNotifyChannels();
      const l3 = none.logs.join(" | ");
      check("logNotifyChannels: all disabled", l3.includes("wxpush=未启用") && !l3.includes("需同时设置"), l3);
    }

    // 9. known edge case: host URL WITH a query string (not documented as supported)
    {
      captured.length = 0;
      const { sb } = makeSandbox(base + "/?token=should-not-be-here", TOKEN);
      const ok = await sb.sendWxpush("T9", "C9");
      const wrongPath = captured[0] && captured[0].url !== "/wxsend";
      check("EDGE: URL-with-query misroutes (documented limitation)", wrongPath === true, captured[0] ? captured[0].url : "no request");
    }

    // 10. connection refused (wxpush container not ready yet) -> false, must NOT throw
    {
      const { sb, logs } = makeSandbox("http://127.0.0.1:1", TOKEN);
      let threw = false;
      let ok;
      try {
        ok = await sb.sendWxpush("T10", "C10");
      } catch (e) {
        threw = true;
      }
      check("connection refused => false, never throws", threw === false && ok === false, `threw=${threw} ok=${ok}`);
      check("connection refused is logged", logs.some((l) => l.startsWith("ERROR")), logs.join(" | "));
    }

    // 11. log only prints endpoint, not token, on a normal send
    {
      const { sb, logs } = makeSandbox(base, TOKEN);
      await sb.sendWxpush("T11", "C11");
      check("normal send logs endpoint only, no token", logs.join(" ").includes("/wxsend") && !logs.join(" ").includes(TOKEN), logs.join(" | "));
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
