# -*- coding: utf-8 -*-
"""
orchestrator.py —— 协作台「总控室」引擎

一个总控 agent 拆解任务 -> 分发到各槽位 agent(CLI) 执行 -> 总控汇总。
每个槽位都会显示"实际在用哪家 API"：
  - claude / codex 系：直接读 CC Switch 里当前生效的供应商（真实生效）
  - 其他 CLI（codebuddy / kimi / traecli / qwen）：显示其自有登录主体
"""
import json
import os
import re
import subprocess
import threading
import time

import apihub

HERE = os.path.dirname(os.path.abspath(__file__))

CODEX_EXE = r"C:\Users\35632\AppData\Local\OpenAI\Codex\bin\2993767bd1dfb009\codex.exe"
CB_EXE = r"C:\Users\35632\AppData\Local\codebuddy\bin\codebuddy.exe"
KIMI_EXE = r"C:\Users\35632\.kimi-code\bin\kimi.exe"
TRAE_EXE = r"C:\Users\35632\AppData\Local\trae-cli\bin\traecli.exe"
QWEN_CMD = r"C:\Users\35632\.local\qwen.cmd"
DSH_CMD = r"C:\Users\35632\AppData\Local\Programs\DeepSeek Harness\resources\runtime\cli\bin\dsh.cmd"

ROSTER_FILE = os.path.join(HERE, "roster.json")
PROJECTS_FILE = os.path.join(HERE, "projects.json")
RUNS_DIR = os.path.join(HERE, "runs")
HUB = "http://127.0.0.1:8733"
CREATE_NO_WINDOW = 0x08000000

DEFAULT_PROJECTS = {
    "current": "p1",
    "items": [{"id": "p1", "name": "默认项目", "workspace": r"C:\Users\35632"}],
}


def get_projects():
    d = _load(PROJECTS_FILE, None)
    if not d or not d.get("items"):
        _save(PROJECTS_FILE, DEFAULT_PROJECTS)
        return json.loads(json.dumps(DEFAULT_PROJECTS))
    if d.get("current") not in [x["id"] for x in d["items"]]:
        d["current"] = d["items"][0]["id"]
    return d


def set_projects(patch):
    d = get_projects()
    if isinstance(patch, dict):
        if "current" in patch:
            d["current"] = patch["current"]
        if "items" in patch and isinstance(patch["items"], list):
            d["items"] = patch["items"]
    _save(PROJECTS_FILE, d)
    return d


def add_project(name, workspace):
    d = get_projects()
    pid = "p%d" % (int(time.time()))
    d["items"].append({"id": pid, "name": name, "workspace": workspace})
    d["current"] = pid
    _save(PROJECTS_FILE, d)
    return d


def current_project():
    d = get_projects()
    for x in d["items"]:
        if x["id"] == d["current"]:
            return x
    return d["items"][0]


def save_transcript(proj, run_id, data):
    d = os.path.join(RUNS_DIR, proj["id"])
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, "%s.json" % run_id)
    _save(path, data)
    return path

# 各家 CLI 的"推理强度"开关：flag=命令行参数，config=配置项覆盖
EFFORT = {
    "claude": {"mode": "flag", "flag": "--effort",
               "levels": ["low", "medium", "high", "max"]},
    "codebuddy": {"mode": "flag", "flag": "--effort",
                  "levels": ["minimal", "low", "medium", "high", "xhigh", "max"]},
    "codex": {"mode": "config", "key": "model_reasoning_effort",
              "levels": ["minimal", "low", "medium", "high"]},
    "kimi": {"mode": None, "levels": []},
    "traecli": {"mode": None, "levels": []},
    "qwen": {"mode": None, "levels": []},
    "dsh": {"mode": None, "levels": []},
    "stub": {"mode": None, "levels": []},
}

# 槽位可选的 CLI。「plane」表示它是否直接复用 CC Switch 的供应商配置。
CLIS = {
    "claude": {"label": "Claude Code", "vendor": "Anthropic 接口协议", "plane": "claude",
               "cmd": ["claude", "-p", "{prompt}"], "exe": "claude", "note": ""},
    "codex": {"label": "Codex", "vendor": "OpenAI 接口协议", "plane": "codex",
              "cmd": [CODEX_EXE, "exec", "--skip-git-repo-check", "{prompt}"], "exe": CODEX_EXE, "note": ""},
    "codebuddy": {"label": "CodeBuddy", "vendor": "腾讯", "plane": None,
                  "cmd": [CB_EXE, "-p", "{prompt}"], "exe": CB_EXE, "note": "微信 / 手机号登录"},
    "kimi": {"label": "Kimi Code", "vendor": "月之暗面", "plane": None,
             "cmd": [KIMI_EXE, "-p", "{prompt}"], "exe": KIMI_EXE, "note": "设备码登录"},
    "traecli": {"label": "TraeCode", "vendor": "字节跳动", "plane": None,
                "cmd": [TRAE_EXE, "-p", "{prompt}"], "exe": TRAE_EXE, "note": "字节账号登录"},
    "qwen": {"label": "Qwen Code", "vendor": "阿里", "plane": None,
             "cmd": [QWEN_CMD, "-p", "{prompt}"], "exe": QWEN_CMD, "note": "需配 API Key"},
    "dsh": {"label": "DeepSeek Harness", "vendor": "DeepSeek", "plane": None,
            "cmd": ["cmd", "/c", DSH_CMD, "headless", "{prompt}"], "exe": DSH_CMD,
            "note": "headless 非交互（非 ACP）"},
    "stub": {"label": "桩（本地测试）", "vendor": "—", "plane": None,
             "cmd": ["python", "-u", "-c", "import sys;print('[桩] 收到：'+sys.argv[1][:300])", "{prompt}"],
             "exe": "python", "note": "不调用任何模型，仅验证链路"},
}

DEFAULT_ROSTER = {
    "orchestrator": "claude",
    "workers": [
        {"id": "w1", "name": "执行A", "cli": "codebuddy", "enabled": True},
        {"id": "w2", "name": "执行B", "cli": "kimi", "enabled": True},
        {"id": "w3", "name": "执行C", "cli": "traecli", "enabled": False},
        {"id": "w4", "name": "执行D", "cli": "qwen", "enabled": False},
    ],
    "workdir": r"C:\Users\35632",
    "timeout": 600,
}

_EVENTS = []
_EVENT_ID = [0]
_RUN_LOCK = threading.Lock()
_RUNNING = {"on": False}


# ------------------------------------------------------------------ 基础
def _user_env():
    return apihub._user_env()


def _load(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _save(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def get_roster():
    r = _load(ROSTER_FILE, None)
    if not r:
        _save(ROSTER_FILE, DEFAULT_ROSTER)
        return json.loads(json.dumps(DEFAULT_ROSTER))
    for k, v in DEFAULT_ROSTER.items():
        r.setdefault(k, v)
    return r


def set_roster(new):
    r = get_roster()
    if isinstance(new, dict):
        for k in ("orchestrator", "orchestrator_effort", "workers", "workdir", "timeout"):
            if k in new:
                r[k] = new[k]
    _save(ROSTER_FILE, r)
    return r


def _exe_ok(path):
    return path in ("claude", "python") or os.path.exists(path)


def available_clis():
    out = {}
    for k, v in CLIS.items():
        if _exe_ok(v["exe"]):
            eff = EFFORT.get(k, {"mode": None, "levels": []})
            out[k] = {"label": v["label"], "vendor": v["vendor"], "note": v["note"],
                      "plane": v["plane"],
                      "effort_levels": eff.get("levels", []),
                      "effort_ok": eff.get("mode") is not None}
    return out


# ------------------------------------------------------------------ 槽位供应商
def slot_provider(cli):
    """返回该 CLI 当前实际使用的 API 供应商信息。"""
    meta = CLIS.get(cli)
    if not meta:
        return {"name": "未知", "detail": ""}
    plane = meta["plane"]
    if plane:
        try:
            cur = apihub.current_ids()
            pid = cur.get(plane, "")
            provs = apihub.list_providers().get(plane, [])
            for p in provs:
                if p["id"] == pid:
                    return {"name": p["name"],
                            "detail": (p.get("base_url") or "官方") + (" · " + p["model"] if p.get("model") else ""),
                            "from_ccsw": True}
            return {"name": "（CC Switch 未选中）", "detail": "", "from_ccsw": True}
        except Exception as e:
            return {"name": "读取失败", "detail": str(e)[:80], "from_ccsw": True}
    return {"name": meta["vendor"], "detail": meta["note"], "from_ccsw": False}


def slots_view():
    r = get_roster()
    return {
        "orchestrator": {"cli": r["orchestrator"], **CLIS.get(r["orchestrator"], {}),
                         "effort": r.get("orchestrator_effort", ""),
                         "effort_levels": (EFFORT.get(r["orchestrator"]) or {}).get("levels", []),
                         "provider": slot_provider(r["orchestrator"])},
        "workers": [{"id": w["id"], "name": w["name"], "cli": w["cli"], "enabled": w.get("enabled", True),
                     "effort": w.get("effort", ""),
                     "effort_levels": (EFFORT.get(w["cli"]) or {}).get("levels", []),
                     "provider": slot_provider(w["cli"]),
                     "label": CLIS.get(w["cli"], {}).get("label", w["cli"])}
                    for w in r["workers"]],
        "workdir": r.get("workdir", ""),
        "clis": available_clis(),
        "running": _RUNNING["on"],
        "projects": get_projects(),
        "current_project": current_project(),
    }


# ------------------------------------------------------------------ 事件流
def emit(kind, text, **extra):
    _EVENT_ID[0] += 1
    ev = {"id": _EVENT_ID[0], "ts": time.strftime("%H:%M:%S"), "kind": kind, "text": text}
    ev.update(extra)
    _EVENTS.append(ev)
    if len(_EVENTS) > 2000:
        del _EVENTS[:500]
    try:                                  # 同步写进协作台，聊天区也能看到
        import urllib.request
        data = json.dumps({"from": extra.get("who") or "总控室", "text": text, "to": "all"},
                          ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(HUB + "/api/hub", data=data, method="POST",
                                     headers={"Content-Type": "application/json; charset=utf-8"})
        urllib.request.urlopen(req, timeout=5).read()
    except Exception:
        pass
    return ev


def events_since(since):
    return [e for e in _EVENTS if e["id"] > int(since)]


# ------------------------------------------------------------------ 调用 CLI
def call_cli(cli, prompt, timeout=None, cwd=None, effort=None):
    meta = CLIS.get(cli)
    if not meta:
        return False, "未知 CLI"
    roster = get_roster()
    work = cwd or roster.get("workdir") or os.path.expanduser("~")
    if not os.path.isdir(work):
        work = os.path.dirname(HERE)
    env = _user_env()
    if cli == "dsh" and not env.get("DEEPSEEK_API_KEY"):
        # dsh headless 需要 DeepSeek 官方 key：从 CC Switch 的 DeepSeek 供应商借用
        try:
            import apihub
            for p in apihub.list_providers().get("claude", []):
                if p["name"] == "DeepSeek":
                    cfg = apihub._get_provider(p["id"], "claude")["cfg"]
                    k = (cfg.get("env") or {}).get("ANTHROPIC_AUTH_TOKEN", "")
                    if k:
                        env["DEEPSEEK_API_KEY"] = k
                    break
        except Exception:
            pass
    cmd = [c.replace("{prompt}", prompt) for c in meta["cmd"]]
    spec = EFFORT.get(cli) or {}
    if effort and spec.get("mode") == "flag":
        cmd = [cmd[0], spec["flag"], effort] + cmd[1:]
    elif effort and spec.get("mode") == "config":
        cmd = [cmd[0], "-c", '%s="%s"' % (spec["key"], effort)] + cmd[1:]
    t0 = time.time()
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=timeout or roster.get("timeout", 600),
                           cwd=work, env=env, creationflags=CREATE_NO_WINDOW)
        out = (p.stdout or b"").decode("utf-8", "replace").strip()
        err = (p.stderr or b"").decode("utf-8", "replace").strip()
        ms = int((time.time() - t0) * 1000)
        if not out:
            return False, (err[:300] or "无输出")
        return True, out
    except subprocess.TimeoutExpired:
        return False, "超时"
    except Exception as e:
        return False, str(e)[:200]


# ------------------------------------------------------------------ 拆解 / 汇总
PLAN_PROMPT = """你是任务总控。请把下面的任务拆成若干子任务，分配给这些执行者：
{roster}

只输出一个 JSON 数组，不要任何解释，格式：
[{{"worker": "执行A", "subtask": "具体要做什么"}}]

任务：{task}"""


def _stub_plan(task, workers):
    return [{"worker": w["name"], "subtask": task} for w in workers if w.get("enabled")]


def _parse_plan(text, workers):
    names = {w["name"] for w in workers}
    m = re.search(r"\[.*\]", text, re.S)
    if not m:
        return None
    try:
        arr = json.loads(m.group(0))
    except Exception:
        return None
    out = []
    for it in arr:
        if not isinstance(it, dict):
            continue
        w = str(it.get("worker") or it.get("name") or "").strip()
        s = str(it.get("subtask") or it.get("task") or "").strip()
        if not s:
            continue
        if w not in names:                      # 名字对不上就跳过，避免误派
            continue
        out.append({"worker": w, "subtask": s})
    return out or None


# ------------------------------------------------------------------ 编排主流程
def run_task(task):
    with _RUN_LOCK:
        if _RUNNING["on"]:
            return {"ok": False, "msg": "已有任务在执行"}
        _RUNNING["on"] = True

    def worker():
        try:
            _run(task)
        except Exception as e:
            emit("error", "编排异常：%s" % e)
        finally:
            _RUNNING["on"] = False
            emit("done", "本轮编排结束")

    threading.Thread(target=worker, daemon=True).start()
    return {"ok": True, "msg": "已开始"}


WORKER_PROMPT = """你是「{name}」，一个执行单元。本次任务由总控（{orch}）拆解后派发给你。
规则：只完成下面这一件事；完成后直接给出结果正文；不要反问、不要自行扩大范围。

子任务：{subtask}"""


def _run(task):
    roster = get_roster()
    orch = roster["orchestrator"]
    workers = [w for w in roster["workers"] if w.get("enabled", True)]
    if not workers:
        emit("error", "没有启用任何执行槽位")
        return

    proj = current_project()
    work = proj.get("workspace") or roster.get("workdir")
    run_id = time.strftime("%Y%m%d-%H%M%S")
    transcript = {"project": proj, "task": task, "run_id": run_id,
                  "orchestrator": orch, "steps": [], "results": {}}

    emit("task", "【项目：%s】工作空间：%s\n任务：%s" % (proj["name"], work, task))
    emit("plan", "总控（%s）正在拆解…%s" % (CLIS.get(orch, {}).get("label", orch),
                                          ("（强度 %s）" % r.get("orchestrator_effort"))
                                          if r.get("orchestrator_effort") else ""))
    orch_effort = r.get("orchestrator_effort") or None

    if orch == "stub":
        plan = _stub_plan(task, workers)
    else:
        roster_txt = "\n".join("- %s（%s）" % (w["name"], CLIS.get(w["cli"], {}).get("label", w["cli"]))
                               for w in workers)
        ok, out = call_cli(orch, PLAN_PROMPT.format(roster=roster_txt, task=task), cwd=work,
                           effort=orch_effort)
        transcript["steps"].append({"who": "总控", "cli": orch, "role": "plan",
                                    "prompt": "（拆解）", "output": out})
        if not ok:
            emit("error", "总控拆解失败：%s" % out)
            save_transcript(proj, run_id, transcript)
            return
        plan = _parse_plan(out, workers)
        if not plan:
            emit("warn", "总控没能给出可解析的分工，改为把整件事交给第一个执行者")
            plan = [{"worker": workers[0]["name"], "subtask": task}]

    emit("plan", "分工方案：%s" % json.dumps(plan, ensure_ascii=False), plan=plan)

    results = {}
    lock = threading.Lock()

    def do_one(item):
        w = next((x for x in workers if x["name"] == item["worker"]), None)
        if not w:
            return
        meta = CLIS.get(w["cli"], {})
        emit("dispatch", "%s（%s / %s）开始执行" % (w["name"], meta.get("label", w["cli"]),
                                                 slot_provider(w["cli"])["name"]),
             who=w["name"])
        prompt = WORKER_PROMPT.format(name=w["name"], orch=CLIS.get(orch, {}).get("label", orch),
                                      subtask=item["subtask"])
        ok, out = call_cli(w["cli"], prompt, cwd=work, effort=w.get("effort") or None)
        with lock:
            results[w["name"]] = {"ok": ok, "out": out, "subtask": item["subtask"],
                                  "cli": w["cli"], "provider": slot_provider(w["cli"])["name"],
                                  "prompt": prompt}
        emit("result", "%s %s：%s" % (w["name"], "完成" if ok else "失败", out[:400]), who=w["name"])

    threads = [threading.Thread(target=do_one, args=(it,)) for it in plan]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    body = "\n\n".join("### %s（%s / %s）\n子任务：%s\n结果：%s"
                       % (n, r["cli"], r["provider"], r["subtask"], r["out"])
                       for n, r in results.items())
    emit("assemble", "总控正在汇总 %d 份结果…" % len(results))
    if orch == "stub":
        final = "[桩汇总]\n" + body
    else:
        ok, final = call_cli(orch, "你是任务总控。下面是各执行者的结果，请汇总成一份完整交付。\n\n" + body,
                             cwd=work, effort=orch_effort)
        transcript["steps"].append({"who": "总控", "cli": orch, "role": "assemble",
                                    "prompt": "（汇总）", "output": final})
        if not ok:
            final = "[汇总失败：%s]\n\n%s" % (final, body)
    transcript["results"] = results
    transcript["final"] = final
    path = save_transcript(proj, run_id, transcript)
    emit("final", final)
    emit("done", "本轮结束 · 记录已存到 %s" % os.path.relpath(path, HERE))
    return final
