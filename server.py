# -*- coding: utf-8 -*-
"""
AgentHub —— 本地一站式 AI Agent 管理面板
- 自动扫描上级目录(agent 文件夹)里的 .lnk 快捷方式，反解出真实 exe 路径
- 一键打开 / 关闭(结束进程) / 查看运行状态 / 打开目录
- 内置"协作台"：本地消息中继，任何能跑 curl 的 agent 都能收发消息

仅监听 127.0.0.1，纯 Python 标准库，无第三方依赖。
"""
import json
import os
import re
import socket
import subprocess
import threading
import time
import webbrowser

import apihub
import orchestrator
import brainstorm
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

BASE = os.path.dirname(os.path.abspath(__file__))
AGENT_DIR = os.path.dirname(BASE)            # ...\Desktop\agent
MANUAL_FILE = os.path.join(BASE, "apps_manual.json")
HUB_FILE = os.path.join(BASE, "hub.json")
INDEX_FILE = os.path.join(BASE, "index.html")
PORT = 8733

CREATE_NO_WINDOW = 0x08000000
_HUB_LOCK = threading.Lock()


# ---------------------------------------------------------------- 工具函数
def _run(cmd, timeout=20):
    """执行命令，返回 (returncode, stdout)。"""
    try:
        p = subprocess.run(
            cmd, capture_output=True, timeout=timeout,
            creationflags=CREATE_NO_WINDOW,
        )
        return p.returncode, p.stdout.decode("utf-8", "replace"), p.stderr.decode("utf-8", "replace")
    except Exception as e:
        return -1, "", str(e)


def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def ps_json(script):
    """执行 PowerShell 并解析其 JSON 输出。"""
    cmd = [
        "powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command",
        "[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false);" + script,
    ]
    rc, out, err = _run(cmd)
    out = out.strip()
    if not out:
        return None
    try:
        return json.loads(out)
    except Exception:
        return None


# ---------------------------------------------------------------- 快捷方式反解
def resolve_shortcuts():
    script = (
        "$sh=New-Object -ComObject WScript.Shell;"
        "$r=@();"
        "Get-ChildItem -LiteralPath '%s' -Filter *.lnk -ErrorAction SilentlyContinue | ForEach-Object {"
        "$l=$sh.CreateShortcut($_.FullName);"
        "$r+=[PSCustomObject]@{name=$_.BaseName;target=$l.TargetPath;args=$l.Arguments;workdir=$l.WorkingDirectory}};"
        "$r | ConvertTo-Json -Compress -Depth 3"
    ) % AGENT_DIR.replace("'", "''")
    data = ps_json(script)
    if data is None:
        return []
    if isinstance(data, dict):
        data = [data]
    return data


def resolve_urls():
    """读取 .url 网络快捷方式。"""
    items = []
    for fn in os.listdir(AGENT_DIR):
        if fn.lower().endswith(".url"):
            path = os.path.join(AGENT_DIR, fn)
            try:
                with open(path, "r", encoding="utf-8", errors="ignore") as f:
                    txt = f.read()
                m = re.search(r"URL=(.+)", txt)
                if m:
                    items.append({
                        "name": os.path.splitext(fn)[0],
                        "target": m.group(1).strip(),
                        "args": "",
                        "workdir": "",
                    })
            except Exception:
                pass
    return items


def guess_kind(target):
    t = target.lower()
    if t.endswith("explorer.exe"):
        return "store"
    if t.startswith("shell:") or "://" in target:
        return "uri"
    if t.endswith(".exe"):
        return "exe"
    return "other"


def build_apps():
    manual = load_json(MANUAL_FILE, {})
    overrides = manual.get("overrides", {})
    raw = resolve_shortcuts() + resolve_urls()

    apps = []
    for it in raw:
        name = it.get("name") or "未知"
        target = (it.get("target") or "").strip()
        args = (it.get("args") or "").strip()
        workdir = (it.get("workdir") or "").strip()
        if not target:
            continue

        kind = guess_kind(target)
        # 进程映像名：默认取 exe 文件名
        proc = ""
        if kind == "exe":
            proc = os.path.splitext(os.path.basename(target))[0]

        app = {
            "id": name,
            "name": name,
            "kind": kind,
            "target": target,
            "args": args,
            "workdir": workdir,
            "process": proc,
            "killTitle": "",
            "dataDir": "",
            "note": "",
        }
        ov = overrides.get(name, {})
        for k, v in ov.items():
            app[k] = v
        apps.append(app)

    # 排序：把代理等工具沉到后面
    tail = ("Clash", "CuteCloud", "CloudDrive")
    apps.sort(key=lambda a: (any(t in a["name"] for t in tail), a["name"].lower()))
    return apps


def build_folders():
    """可一键打开的文件夹入口：agent 目录、ai-skill 及其子目录。"""
    folders = [{"name": "agent（本目录）", "path": AGENT_DIR}]
    try:
        for entry in sorted(os.listdir(AGENT_DIR)):
            p = os.path.join(AGENT_DIR, entry)
            if os.path.isdir(p):
                folders.append({"name": entry, "path": p})
                # 再展开一层（比如 ai-skill 下的各个技能）
                if len(entry) and entry != "__pycache__":
                    try:
                        for sub in sorted(os.listdir(p)):
                            sp = os.path.join(p, sub)
                            if os.path.isdir(sp):
                                folders.append({"name": "%s / %s" % (entry, sub), "path": sp})
                    except Exception:
                        pass
    except Exception:
        pass
    # 去重
    seen, out = set(), []
    for f in folders:
        if f["path"] not in seen:
            seen.add(f["path"])
            out.append(f)
    return out


# ---------------------------------------------------------------- 进程控制
_SNAP = {"t": 0.0, "data": []}
_SNAP_LOCK = threading.Lock()


def snapshot_processes(max_age=1.5):
    """一次性抓取全部进程(名/ID/窗口标题)，短时缓存，避免反复调用 PowerShell。"""
    with _SNAP_LOCK:
        now = time.time()
        if now - _SNAP["t"] < max_age and _SNAP["data"]:
            return _SNAP["data"]
        script = ("Get-Process -ErrorAction SilentlyContinue | "
                  "Select-Object ProcessName,Id,MainWindowTitle | ConvertTo-Json -Compress -Depth 2")
        data = ps_json(script)
        if data is None:
            data = []
        elif isinstance(data, dict):
            data = [data]
        _SNAP["t"] = now
        _SNAP["data"] = data
        return data


def find_pids(app):
    """按进程名或窗口标题查找 pid 列表。"""
    procs = snapshot_processes()
    proc = (app.get("process") or "").lower()
    title = app.get("killTitle") or ""
    rx = re.compile(title, re.I) if title else None
    pids = []
    for p in procs:
        pname = (p.get("ProcessName") or "").lower()
        ptitle = p.get("MainWindowTitle") or ""
        hit = False
        if rx is not None and ptitle and rx.search(ptitle):
            hit = True
        if not hit and proc and pname == proc:
            hit = True
        if hit:
            try:
                pids.append(int(p.get("Id")))
            except Exception:
                pass
    return sorted(set(pids))


def launch(app):
    kind = app.get("kind")
    target = app["target"]
    args = app.get("args", "")
    workdir = app.get("workdir", "") or None
    if workdir and not os.path.isdir(workdir):
        workdir = None

    if kind == "uri":
        os.startfile(target)
        return True, "已打开链接"

    if kind == "store":
        cmd = 'start "" "explorer.exe" %s' % args
    else:
        cmd = 'start "" "%s" %s' % (target, args)

    try:
        subprocess.Popen(cmd, shell=True, cwd=workdir, env=_user_env(),
                         creationflags=CREATE_NO_WINDOW)
        return True, "已启动"
    except Exception as e:
        return False, str(e)


def _user_env():
    """
    还原真实用户环境后再启动外部应用。
    Loomy 运行时会把 HOME / USERPROFILE / APPDATA / LOCALAPPDATA 指向自己的目录，
    子进程若继承这套环境，会以为换了用户，去建一个空白的新配置档案
    （典型症状：应用要求重新登录、看不到任何历史记录）。
    HOMEDRIVE + HOMEPATH 才是真实用户目录。
    """
    env = os.environ.copy()
    home = None
    drive, path = env.get("HOMEDRIVE", ""), env.get("HOMEPATH", "")
    if drive and path and os.path.isdir(drive + path):
        home = drive + path
    if not home:
        guess = os.path.dirname(os.path.dirname(os.path.dirname(BASE)))
        if os.path.isdir(guess):
            home = guess
    if home:
        env["USERPROFILE"] = home
        env["HOME"] = home
        env["APPDATA"] = os.path.join(home, "AppData", "Roaming")
        env["LOCALAPPDATA"] = os.path.join(home, "AppData", "Local")
        env["TEMP"] = os.path.join(home, "AppData", "Local", "Temp")
        env["TMP"] = env["TEMP"]
    return env


def close_app(app, force=True):
    """关闭应用：结束进程树（强杀）。"""
    pids = find_pids(app)
    if not pids:
        return {"ok": False, "alive": False, "msg": "未发现正在运行的进程"}
    ok = 0
    for pid in pids:
        rc, _, _ = _run(["taskkill", "/PID", str(pid), "/T", "/F"])
        if rc == 0:
            ok += 1
    alive = bool(find_pids(app))
    return {"ok": ok > 0, "alive": alive, "mode": "force",
            "msg": "已结束 %d 个进程" % ok}


# ---------------------------------------------------------------- 协作台
def hub_read(since=0):
    with _HUB_LOCK:
        msgs = load_json(HUB_FILE, [])
    return [m for m in msgs if m.get("id", 0) > since]


def hub_post(from_, text, to="all"):
    with _HUB_LOCK:
        msgs = load_json(HUB_FILE, [])
        nid = (msgs[-1]["id"] + 1) if msgs else 1
        msg = {"id": nid, "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
               "from": from_ or "匿名", "to": to or "all", "text": text}
        msgs.append(msg)
        save_json(HUB_FILE, msgs)
    return msg


def hub_clear():
    with _HUB_LOCK:
        save_json(HUB_FILE, [])
    return True


# ---------------------------------------------------------------- HTTP 服务
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False)
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _body(self):
        """容错解析请求体：
        - 标准 JSON
        - Windows 下 curl 未还原 \\" 的写法
        - PowerShell 传参时把中文按 GBK 编码的情况
        - 以及 form 编码（a=1&b=2）
        """
        try:
            n = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(n) if n else b""
            if not raw:
                return {}
            for enc in ("utf-8", "gb18030"):
                try:
                    text = raw.decode(enc)
                except Exception:
                    continue
                for candidate in (text, text.replace('\\"', '"').replace("\\\\", "\\")):
                    try:
                        val = json.loads(candidate)
                        if isinstance(val, dict):
                            return val
                    except Exception:
                        pass
            from urllib.parse import parse_qs
            q = parse_qs(raw.decode("gb18030", "replace"))
            if q:
                return {k: v[0] for k, v in q.items()}
            return {}
        except Exception:
            return {}

    # ------------------------------ GET
    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path in ("/", "/index.html"):
            try:
                with open(INDEX_FILE, "r", encoding="utf-8") as f:
                    return self._send(200, f.read(), "text/html; charset=utf-8")
            except Exception as e:
                return self._send(500, {"error": str(e)})
        if u.path == "/api/data":
            apps = build_apps()
            status = {}
            for a in apps:
                status[a["id"]] = len(find_pids(a)) > 0
            return self._send(200, {"apps": apps, "folders": build_folders(),
                                    "status": status, "agentDir": AGENT_DIR})
        if u.path == "/api/hub":
            since = int(q.get("since", ["0"])[0])
            return self._send(200, {"messages": hub_read(since)})
        if u.path == "/api/hub/wait":
            # 长轮询：有新消息立刻返回，否则挂起到超时（供监视器使用，避免空转）
            since = int(q.get("since", ["0"])[0])
            timeout = min(float(q.get("timeout", ["30"])[0]), 120.0)
            deadline = time.time() + timeout
            while time.time() < deadline:
                msgs = hub_read(since)
                if msgs:
                    return self._send(200, {"messages": msgs})
                time.sleep(0.4)
            return self._send(200, {"messages": []})
        if u.path == "/api/providers":
            d = apihub.list_providers()
            d["effective"] = {"claude": apihub.effective_config("claude"),
                              "codex": apihub.effective_config("codex")}
            d["paths"] = {"claude": apihub.CLAUDE_SETTINGS, "codex": apihub.CODEX_CONFIG,
                          "backupDir": apihub.BACKUP_DIR}
            return self._send(200, d)
        if u.path == "/api/provider":
            pid = q.get("id", [""])[0]
            app = q.get("app", ["claude"])[0]
            return self._send(200, apihub.provider_detail(app, pid))
        if u.path == "/api/orch":
            return self._send(200, orchestrator.slots_view())
        if u.path == "/api/check/all":
            return self._send(200, apihub.check_all())
        if u.path == "/api/check":
            return self._send(200, apihub.check_provider(q.get("app", ["claude"])[0], q.get("id", [""])[0]))
        if u.path == "/api/cli/status":
            return self._send(200, {"clis": apihub.cli_status()})
        if u.path == "/api/orch/events":
            since = int(q.get("since", ["0"])[0])
            return self._send(200, {"events": orchestrator.events_since(since),
                                    "running": orchestrator._RUNNING["on"]})
        if u.path == "/api/bs":
            return self._send(200, brainstorm.view())
        if u.path == "/api/bs/events":
            since = int(q.get("since", ["0"])[0])
            return self._send(200, {"events": brainstorm.events_since(since),
                                    "running": brainstorm._RUNNING["on"],
                                    "state": brainstorm.STATE})
        return self._send(404, {"error": "not found"})

    # ------------------------------ POST
    def do_POST(self):
        u = urlparse(self.path)
        b = self._body()
        if u.path == "/api/launch":
            app = self._find(b.get("id"))
            if not app:
                return self._send(404, {"ok": False, "msg": "未找到应用"})
            ok, msg = launch(app)
            return self._send(200, {"ok": ok, "msg": msg})
        if u.path == "/api/kill":
            app = self._find(b.get("id"))
            if not app:
                return self._send(404, {"ok": False, "msg": "未找到应用"})
            return self._send(200, close_app(app, bool(b.get("force"))))
        if u.path == "/api/openfolder":
            path = b.get("path") or (self._find(b.get("id")) or {}).get("dataDir") \
                or (self._find(b.get("id")) or {}).get("workdir")
            if path and os.path.isdir(path):
                subprocess.Popen('explorer.exe "%s"' % path)
                return self._send(200, {"ok": True, "msg": "已打开目录"})
            return self._send(200, {"ok": False, "msg": "目录不存在"})
        if u.path == "/api/hub":
            if not (b.get("text") or "").strip():
                return self._send(400, {"ok": False, "msg": "消息为空"})
            m = hub_post(b.get("from"), b.get("text"), b.get("to", "all"))
            return self._send(200, {"ok": True, "message": m})
        if u.path == "/api/hub/clear":
            hub_clear()
            return self._send(200, {"ok": True})
        if u.path == "/api/switch":
            return self._send(200, apihub.switch(b.get("app"), b.get("id")))
        if u.path == "/api/test":
            return self._send(200, apihub.test_provider(b.get("app"), b.get("id")))
        if u.path == "/api/restart":
            if b.get("app") == "claude":
                return self._send(200, apihub.restart_claude())
            return self._send(200, {"ok": False, "msg": "Codex 请手动重开应用生效"})
        if u.path == "/api/provider/save":
            return self._send(200, apihub.save_provider(b.get("app"), b, b.get("id") or None))
        if u.path == "/api/provider/delete":
            return self._send(200, apihub.delete_provider(b.get("app"), b.get("id")))
        if u.path == "/api/restore":
            return self._send(200, apihub.restore_backup_dir(b.get("dir")))
        if u.path == "/api/orch/roster":
            return self._send(200, {"ok": True, "roster": orchestrator.set_roster(b)})
        if u.path == "/api/orch/projects":
            if b.get("new"):
                return self._send(200, {"ok": True, "projects": orchestrator.add_project(
                    b.get("name") or "未命名项目", b.get("workspace") or "")})
            return self._send(200, {"ok": True, "projects": orchestrator.set_projects(b)})
        if u.path == "/api/orch/runs":
            proj = orchestrator.current_project()
            d = os.path.join(orchestrator.RUNS_DIR, proj["id"])
            items = []
            if os.path.isdir(d):
                for fn in sorted(os.listdir(d), reverse=True)[:30]:
                    items.append({"file": fn, "path": os.path.join(d, fn)})
            return self._send(200, {"project": proj, "runs": items})
        if u.path == "/api/orch/run":
            task = (b.get("task") or "").strip()
            if not task:
                return self._send(400, {"ok": False, "msg": "任务不能为空"})
            return self._send(200, orchestrator.run_task(task))
        if u.path == "/api/bs/save":
            return self._send(200, {"ok": True, "conf": brainstorm.set_conf(b)})
        if u.path == "/api/bs/run":
            idea = (b.get("idea") or "").strip()
            if not idea:
                return self._send(400, {"ok": False, "msg": "idea 不能为空"})
            return self._send(200, brainstorm.run(idea))
        return self._send(404, {"ok": False, "msg": "not found"})

    def _find(self, app_id):
        for a in build_apps():
            if a["id"] == app_id:
                return a
        return None


def find_port(start):
    for p in range(start, start + 20):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.bind(("127.0.0.1", p))
            s.close()
            return p
        except OSError:
            continue
    return start


def main():
    global PORT
    PORT = find_port(PORT)
    if not os.path.exists(HUB_FILE):
        save_json(HUB_FILE, [])
    if not os.path.exists(MANUAL_FILE):
        save_json(MANUAL_FILE, {"overrides": {}})

    url = "http://127.0.0.1:%d/" % PORT
    orchestrator.HUB = "http://127.0.0.1:%d" % PORT
    print("AgentHub 运行中 -> " + url)
    print("扫描目录: " + AGENT_DIR)
    print("按 Ctrl+C 停止")
    threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()


if __name__ == "__main__":
    main()
