#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
javac_server.py —— 给《Java 后端勇者闯关》页面用的本地编译服务（零第三方依赖）

用法：
    python3 javac_server.py            # 默认监听 127.0.0.1:8787
    # 然后浏览器打开 http://127.0.0.1:8787/ —— 服务会顺带托管同目录的 HTML 页面

环境要求：
    任意 JDK 8+。有 javac 走完整编译；只有 java（JRE/JDK 11+）时用「单文件源码启动」
    java Xxx.java；多文件工程会自动合并成单文件再跑。Maven 为可选，/mvn 才需要。

安全说明：
    只监听 127.0.0.1，不对外暴露；每次请求在独立临时目录执行，带超时、输出截断、
    文件名白名单（防路径穿越）。请勿放到公网或不可信网络环境。
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import glob
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOST = "127.0.0.1"
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8787
MAX_BODY = 1024 * 1024          # 请求体上限 1 MB
MAX_OUT = 64 * 1024             # 单路输出上限 64 KB
DEFAULT_TIMEOUT = 20

SAFE_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*\.java$")
SAFE_PATH = re.compile(r"^[A-Za-z0-9_./\-]+$")


def _which(cmd):
    return shutil.which(cmd)


def _ver(cmd):
    try:
        p = subprocess.run([cmd, "-version"], capture_output=True, timeout=5)
        s = (p.stderr or p.stdout).decode("utf-8", "replace").strip().splitlines()
        return s[0] if s else cmd
    except Exception:
        return cmd


def health():
    javac = _which("javac")
    java = _which("java")
    maven = _which("mvn")
    return {
        "ok": bool(java),
        "java": _ver("java") if java else "",
        "javac": bool(javac),
        "source_mode": bool(java),     # JDK 11+ 支持 java Xxx.java
        "maven": bool(maven),
        "maven_version": _ver("mvn") if maven else "",
        "workdir": "tempfile.mkdtemp per request",
    }


def _clip(s):
    b = (s or "").encode("utf-8", "replace")
    if len(b) > MAX_OUT:
        return b[:MAX_OUT].decode("utf-8", "replace") + "\n... [输出已截断 64KB] ..."
    return s or ""


def _run_cmd(argv, cwd, stdin, timeout):
    t0 = time.time()
    try:
        p = subprocess.run(
            argv, cwd=cwd, input=(stdin or "").encode("utf-8"),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout,
        )
        return {
            "stdout": _clip(p.stdout.decode("utf-8", "replace")),
            "stderr": _clip(p.stderr.decode("utf-8", "replace")),
            "exit_code": p.returncode,
            "duration_ms": int((time.time() - t0) * 1000),
        }
    except subprocess.TimeoutExpired:
        return {"stdout": "", "stderr": "执行超时（%ss）" % timeout,
                "exit_code": -1, "duration_ms": int((time.time() - t0) * 1000)}
    except Exception as e:
        return {"stdout": "", "stderr": "执行失败: %s" % e,
                "exit_code": -1, "duration_ms": int((time.time() - t0) * 1000)}


def _merge_sources(files, main):
    """无 javac 时，把多个 .java 合并成单文件供 java Xxx.java 源码模式使用。"""
    imports, bodies = [], []
    # JDK 11 源码模式执行「文件中第一个顶层类」的 main，必须把主类排在最前面
    names = sorted(files.keys(), key=lambda n: (n[:-5] != main, n))
    for name in names:
        src = files[name]
        cls = name[:-5]
        for line in src.splitlines():
            st = line.strip()
            if st.startswith("import "):
                if st not in imports:
                    imports.append(st)
            elif st.startswith("package "):
                continue
        body = "\n".join(
            ln for ln in src.splitlines()
            if not ln.strip().startswith(("import ", "package "))
        )
        if cls != main:
            # 一个源码文件里只能有一个 public 类，且必须与文件名同名
            body = re.sub(r"\bpublic\s+(class|interface|enum)\b", r"\1", body)
        bodies.append("// ---- %s ----\n%s" % (name, body))
    header = "\n".join(imports)
    return (header + "\n\n" if header else "") + "\n\n".join(bodies) + "\n"


def compile_and_run(files, main, stdin, timeout):
    """返回统一结构：compile_output / stdout / stderr / exit_code / duration_ms / mode"""
    timeout = max(1, min(int(timeout or DEFAULT_TIMEOUT), 60))
    files = {k: v for k, v in (files or {}).items()
             if SAFE_NAME.match(k) and isinstance(v, str)}
    if not files:
        return {"ok": False, "error": "没有有效的 .java 文件（文件名只允许字母/数字/下划线 + .java）",
                "compile_output": "", "stdout": "", "stderr": "", "exit_code": -1,
                "duration_ms": 0, "mode": "none"}
    if not main:
        cand = [k[:-5] for k in files if re.search(r"public\s+static\s+void\s+main", files[k])]
        main = cand[0] if cand else list(files.keys())[0][:-5]
    main = re.sub(r"[^A-Za-z0-9_]", "", main) or "Main"

    tmp = tempfile.mkdtemp(prefix="jrpg_")
    t0 = time.time()
    try:
        javac = _which("javac")
        java = _which("java")
        if not java:
            return {"ok": False, "error": "本机没找到 java 命令，请先安装 JDK 8+ 并加入 PATH",
                    "compile_output": "", "stdout": "", "stderr": "", "exit_code": -1,
                    "duration_ms": 0, "mode": "none"}

        if javac and len(files) >= 1:
            mode = "javac"
            for name, src in files.items():
                with open(os.path.join(tmp, name), "w", encoding="utf-8") as f:
                    f.write(src)
            c = _run_cmd(["javac", "-encoding", "UTF-8"] + sorted(files.keys()), tmp, "", timeout)
            if c["exit_code"] != 0:
                return {"ok": False, "compile_output": c["stderr"] or c["stdout"],
                        "stdout": "", "stderr": "", "exit_code": c["exit_code"],
                        "duration_ms": c["duration_ms"], "mode": mode}
            r = _run_cmd(["java", "-Dfile.encoding=UTF-8", main], tmp, stdin, timeout)
            return {"ok": r["exit_code"] == 0,
                    "compile_output": "", "stdout": r["stdout"], "stderr": r["stderr"],
                    "exit_code": r["exit_code"],
                    "duration_ms": int((time.time() - t0) * 1000), "mode": mode}

        # 无 javac：JDK 11+ 源码模式
        if len(files) == 1:
            mode, merged = "source", list(files.values())[0]
        else:
            mode, merged = "source-merged", _merge_sources(files, main)
        fname = main + ".java"
        with open(os.path.join(tmp, fname), "w", encoding="utf-8") as f:
            f.write(merged)
        r = _run_cmd(["java", "-Dfile.encoding=UTF-8", fname], tmp, stdin, timeout)
        # 源码模式把编译错误写在 stderr，归到 compile_output 便于页面高亮
        co = ""
        if r["exit_code"] != 0 and ("error:" in r["stderr"] or "错误:" in r["stderr"]):
            co, r["stderr"] = r["stderr"], ""
        return {"ok": r["exit_code"] == 0,
                "compile_output": co, "stdout": r["stdout"], "stderr": r["stderr"],
                "exit_code": r["exit_code"],
                "duration_ms": int((time.time() - t0) * 1000), "mode": mode}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def maven_test(files, goals, timeout):
    """可选：跑一个最小 Maven 工程。files 里可带相对路径（如 pom.xml / src/test/java/X.java）。"""
    mvn = _which("mvn")
    if not mvn:
        return {"ok": False, "error": "本机没找到 mvn，/mvn 端点需要 Maven。普通 /run 不需要。",
                "stdout": "", "stderr": "", "exit_code": -1, "duration_ms": 0, "mode": "mvn"}
    timeout = max(5, min(int(timeout or 60), 180))
    tmp = tempfile.mkdtemp(prefix="jrpg_mvn_")
    t0 = time.time()
    try:
        for name, src in (files or {}).items():
            if not SAFE_PATH.match(name) or ".." in name or name.startswith("/"):
                continue
            path = os.path.join(tmp, name)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(src if isinstance(src, str) else "")
        argv = [mvn, "-q", "-B"] + (goals or ["test"])
        r = _run_cmd(argv, tmp, "", timeout)
        reports = []
        for rp in glob.glob(os.path.join(tmp, "target", "surefire-reports", "*.txt")):
            try:
                reports.append(open(rp, encoding="utf-8", errors="replace").read()[:2000])
            except Exception:
                pass
        return {"ok": r["exit_code"] == 0,
                "compile_output": r["stderr"] if r["exit_code"] != 0 else "",
                "stdout": (r["stdout"] + "\n" + "\n".join(reports)).strip(),
                "stderr": "" if r["exit_code"] != 0 else r["stderr"],
                "exit_code": r["exit_code"],
                "duration_ms": int((time.time() - t0) * 1000), "mode": "mvn"}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


class Handler(BaseHTTPRequestHandler):
    server_version = "JavaRPGCompileServer/1.0"

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Cache-Control", "no-store")

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):
        if self.path.startswith("/health"):
            self._json(health())
        elif self.path.split("?")[0] in ("/", "/index.html", "/game", "/Java后端勇者闯关.html"):
            # 顺带托管页面：访问 http://127.0.0.1:8787/ 即可同源打开游戏，彻底绕开 file:// 的跨域限制
            path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "Java后端勇者闯关.html")
            try:
                body = open(path, "rb").read()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self._cors()
                self.end_headers()
                self.wfile.write(body)
            except FileNotFoundError:
                self._json({"ok": False, "error": "同目录未找到 Java后端勇者闯关.html，请用 file:// 直接打开页面"}, 404)
        else:
            self._json({"ok": False, "error": "未知端点。可用：GET / , GET /health, POST /run, POST /mvn"}, 404)

    def do_POST(self):
        try:
            n = int(self.headers.get("Content-Length", "0"))
            if n > MAX_BODY:
                self._json({"ok": False, "error": "请求体超过 1MB 限制"}, 413)
                return
            data = json.loads(self.rfile.read(n).decode("utf-8") or "{}")
        except Exception as e:
            self._json({"ok": False, "error": "请求解析失败: %s" % e}, 400)
            return
        if self.path.startswith("/run"):
            self._json(compile_and_run(data.get("files"), data.get("main"),
                                       data.get("stdin"), data.get("timeout")))
        elif self.path.startswith("/mvn"):
            self._json(maven_test(data.get("files"), data.get("goals"), data.get("timeout")))
        else:
            self._json({"ok": False, "error": "未知端点"}, 404)

    def log_message(self, fmt, *args):
        sys.stderr.write("[%s] %s\n" % (time.strftime("%H:%M:%S"), fmt % args))


if __name__ == "__main__":
    h = health()
    print("=" * 62)
    print(" Java 后端勇者闯关 · 本地编译服务")
    print(" 地址      : http://%s:%d" % (HOST, PORT))
    print(" JDK       : %s" % (h["java"] or "未找到 java！请先装 JDK 8+"))
    print(" javac     : %s" % ("有" if h["javac"] else "无（将使用 JDK 11+ 源码模式）"))
    print(" Maven     : %s" % ("有" if h["maven"] else "无（/mvn 不可用，/run 不受影响）"))
    print(" 打开游戏 : http://%s:%d/   （同源托管，状态灯自动 🟢）" % (HOST, PORT))
    print(" 或双击 HTML 后点页面右上角状态灯「重连」；Ctrl+C 停止")
    print("=" * 62)
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
