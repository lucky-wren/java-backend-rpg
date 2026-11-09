/* ============================================================================
   Java 后端勇者闯关 · Cloudflare Worker（HTTPS 真编译中继）
   ----------------------------------------------------------------------------
   用途：GitHub Pages / 腾讯云 COS 是 HTTPS，浏览器会拦截对 http://127.0.0.1
        的 mixed-content 请求，所以托管版默认只能离线判题。这个 Worker 把编译
        请求转发到一个 Judge0 兼容服务，让托管版也能真跑 javac / java。

   部署：
     1. 在 Cloudflare 控制台新建一个 Worker，把本文件整段粘进去。
     2. 改下面三个常量（JUDGE0_URL / JUDGE0_KEY / ACCESS_TOKEN）。
     3. 部署，得到 https://xxx.workers.dev
     4. 在游戏「⚙️ 设置 → 🌐 HTTPS 真编译中继」里填
        https://xxx.workers.dev/run  → 保存并连接。

   请求/响应格式：与页面 runJava() 的 Judge0 分支对齐
     POST /run   { "language":"java", "code":"...", "stdin":"" }
       → { "stdout":"...", "stderr":"...", "compile_output":"...",
           "status":{"id":3,"description":"Accepted"},
           "time":0.12, "base64_encoded":false }
     GET  /health → { "ok":true, "relay":"judge0" }

   安全（重要）：
     - Worker 会执行别人提交的代码。请务必保留 ALLOW_ORIGIN 白名单、
       ACCESS_TOKEN（页面暂时不带头时留空即可，但公网建议配）、
       TIMEOUT_MS 与 MAX_OUTPUT 截断。
     - 仅自己用就不要部署：本机 python3 javac_server.py 更干净、更快、无配额。
   ============================================================================ */

const JUDGE0_URL = 'https://judge0-ce.p.rapidapi.com';   // 换成你的 Judge0 基址（末尾不要 /）
const JUDGE0_KEY = '';                                    // RapidAPI Key；自建 Judge0 留空
const ACCESS_TOKEN = '';                                  // 可选：页面需带 X-Access-Token，留空=不校验
const ALLOW_ORIGIN = '*';                                 // 建议改成你的 Pages 域名，如 'https://user.github.io'
const TIMEOUT_MS = 20000;
const MAX_CODE = 64 * 1024;      // 提交源码上限
const MAX_OUTPUT = 64 * 1024;    // 输出截断

const CORS = {
  'Access-Control-Allow-Origin': ALLOW_ORIGIN,
  'Access-Control-Allow-Methods': 'GET, POST, OPTIONS',
  'Access-Control-Allow-Headers': 'Content-Type, X-Access-Token',
  'Access-Control-Max-Age': '86400',
  'Content-Type': 'application/json; charset=utf-8'
};

function json(obj, status = 200) {
  return new Response(JSON.stringify(obj), { status, headers: CORS });
}
function clip(s) {
  s = String(s || '');
  return s.length > MAX_OUTPUT ? s.slice(0, MAX_OUTPUT) + '\n…[输出已截断]' : s;
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);

    if (request.method === 'OPTIONS') return new Response(null, { status: 204, headers: CORS });
    if (url.pathname === '/health') return json({ ok: true, relay: 'judge0', timeout_ms: TIMEOUT_MS });

    if (url.pathname !== '/run' || request.method !== 'POST') {
      return json({ error: 'Not found. 用 POST /run 或 GET /health' }, 404);
    }

    // 可选令牌校验
    if (ACCESS_TOKEN && request.headers.get('X-Access-Token') !== ACCESS_TOKEN) {
      return json({ error: 'Unauthorized' }, 401);
    }

    let body;
    try { body = await request.json(); }
    catch (e) { return json({ error: '请求体不是合法 JSON' }, 400); }

    const code = String(body.code || '');
    if (!code.trim()) return json({ error: 'code 不能为空' }, 400);
    if (code.length > MAX_CODE) return json({ error: '源码超过 64KB 上限' }, 413);
    if (String(body.language || 'java').toLowerCase() !== 'java') {
      return json({ error: '本中继只接受 Java，避免被当成通用执行沙箱' }, 400);
    }

    // 主类名（页面已把主类排在最前）；Judge0 自己会按文件名编译
    const m = code.match(/public\s+(?:final\s+)?class\s+([A-Za-z_$][\w$]*)/);
    const mainClass = m ? m[1] : 'Main';

    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), TIMEOUT_MS);
    try {
      const headers = { 'Content-Type': 'application/json' };
      if (JUDGE0_KEY) {
        headers['X-RapidAPI-Key'] = JUDGE0_KEY;
        headers['X-RapidAPI-Host'] = new URL(JUDGE0_URL).host;
      }
      if (env && env.JUDGE0_KEY) headers['X-RapidAPI-Key'] = env.JUDGE0_KEY;

      const r = await fetch(
        JUDGE0_URL + '/submissions?base64_encoded=false&wait=true',
        {
          method: 'POST', headers, signal: controller.signal,
          body: JSON.stringify({
            language_id: 62,                    // Judge0: Java (OpenJDK 17)
            source_code: code,
            stdin: String(body.stdin || ''),
            cpu_time_limit: 10,
            memory_limit: 256000,
            // 多文件工程：页面会把多个 .java 合并传过来，这里只跑单个主类
            additional_files: ''
          })
        }
      );
      const text = await r.text();
      let j;
      try { j = JSON.parse(text); } catch (e) {
        return json({ error: 'Judge0 返回的不是 JSON', status: r.status, raw: clip(text) }, 502);
      }
      if (!r.ok) return json({ error: 'Judge0 HTTP ' + r.status, raw: clip(text) }, 502);

      // 归一化成页面期望的结构
      const sid = j.status && j.status.id !== undefined ? Number(j.status.id) : 3;
      return json({
        stdout: clip(j.stdout),
        stderr: clip(j.stderr),
        compile_output: clip(j.compile_output),
        time: Number(j.time || 0),
        memory: j.memory || 0,
        exit_code: sid === 3 ? 0 : 1,
        status: j.status || { id: sid, description: sid === 3 ? 'Accepted' : 'Failed' },
        base64_encoded: false,
        main_class: mainClass
      });
    } catch (e) {
      const timeout = e.name === 'AbortError';
      return json({ error: timeout ? '编译超时（' + TIMEOUT_MS + 'ms）' : '中继请求失败：' + e.message,
                    status: { id: 6, description: 'Relay Error' } }, timeout ? 504 : 502);
    } finally {
      clearTimeout(timer);
    }
  }
};
