# -*- coding: utf-8 -*-
"""
monitor.py —— 协作台监视器（事件驱动）

原理：长轮询 /api/hub/wait，一有新消息立刻被唤醒（不是定时空转）。
      收到消息后可以：
        1) 打印 + 桌面气泡提醒
        2) （可选）自动拉起一个 agent 命令生成回复，再以指定身份回帖

配置文件 monitor.json：
{
  "auto_reply": {
    "enabled": false,
    "identity": "Claude Code",
    "cmd": ["claude", "-p", "{prompt}"],
    "timeout": 180
  },
  "watch_to": ["all", "Loomy"],
  "notify_desktop": true
}

启动：start_monitor.bat   （需保持运行）
"""
import json
import os
import subprocess
import sys
import time
import urllib.request

BASE = "http://127.0.0.1:8733"
HERE = os.path.dirname(os.path.abspath(__file__))
CONF = os.path.join(HERE, "monitor.json")
STATE = os.path.join(HERE, "monitor_state.json")
CREATE_NO_WINDOW = 0x08000000

DEFAULT_CONF = {
    "auto_reply": {
        "enabled": False,
        "identity": "Claude Code",
        "cmd": ["claude", "-p", "{prompt}"],
        "timeout": 180,
    },
    "watch_to": ["all"],
    "notify_desktop": True,
}


def load(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _user_env():
    """还原真实用户环境（Loomy 运行时会改写 HOME/APPDATA，子进程继承会导致用错配置）。"""
    env = os.environ.copy()
    drive, path = env.get("HOMEDRIVE", ""), env.get("HOMEPATH", "")
    home = drive + path if (drive and path and os.path.isdir(drive + path)) else None
    if home:
        env["USERPROFILE"] = home
        env["HOME"] = home
        env["APPDATA"] = os.path.join(home, "AppData", "Roaming")
        env["LOCALAPPDATA"] = os.path.join(home, "AppData", "Local")
        env["TEMP"] = os.path.join(home, "AppData", "Local", "Temp")
        env["TMP"] = env["TEMP"]
    return env


def http_get(path, timeout=140):
    with urllib.request.urlopen(BASE + path, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def http_post(path, obj):
    data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(BASE + path, data=data, method="POST",
                                 headers={"Content-Type": "application/json; charset=utf-8"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def notify(title, text):
    """Windows 桌面气泡提醒（best-effort）。"""
    ps = (
        "[reflection.assembly]::LoadWithPartialName('System.Windows.Forms')|Out-Null;"
        "$n=New-Object System.Windows.Forms.NotifyIcon;"
        "$n.Icon=[System.Drawing.SystemIcons]::Information;"
        "$n.BalloonTipTitle='%s';$n.BalloonTipText='%s';$n.Visible=$true;"
        "$n.ShowBalloonTip(5000);Start-Sleep -Seconds 6;$n.Dispose()"
    ) % (title.replace("'", "''"), text.replace("'", "''").replace("\n", " "))
    try:
        subprocess.Popen(["powershell", "-NoProfile", "-Command", ps],
                         creationflags=CREATE_NO_WINDOW)
    except Exception:
        pass


def run_agent(cfg, msgs):
    """把新消息交给 agent 命令，把它输出的正文回帖。"""
    convo = "\n".join("[%s] %s: %s" % (m["from"], m.get("to", "all"), m["text"]) for m in msgs)
    prompt = ("你是协作台里的成员。下面是刚刚收到的新消息，请针对最后一条给出简短回复（只输出回复正文，不要解释）：\n\n"
              + convo)
    cmd = [c.replace("{prompt}", prompt) for c in cfg["cmd"]]
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=cfg.get("timeout", 180),
                           env=_user_env(), creationflags=CREATE_NO_WINDOW)
        out = (p.stdout or b"").decode("utf-8", "replace").strip()
        err = (p.stderr or b"").decode("utf-8", "replace").strip()
        if not out:
            return False, err[:200] or "agent 没有输出"
        http_post("/api/hub", {"from": cfg.get("identity", "agent"), "text": out, "to": "all"})
        return True, out[:200]
    except Exception as e:
        return False, str(e)[:200]


def main():
    conf = load(CONF, None)
    if conf is None:
        save(CONF, DEFAULT_CONF)
        conf = DEFAULT_CONF
        print("已生成默认配置 monitor.json（自动回复默认关闭）")
    st = load(STATE, {"since": 0})
    since = int(st.get("since", 0))

    print("=" * 56)
    print(" 协作台监视器已启动（事件驱动，长轮询）")
    print(" 当前游标 since = %d" % since)
    auto = conf.get("auto_reply", {})
    print(" 自动回复: %s%s" % ("开" if auto.get("enabled") else "关",
                              ("  身份=%s  命令=%s" % (auto.get("identity"), " ".join(auto.get("cmd", []))))
                              if auto.get("enabled") else "  （改 monitor.json 可开）"))
    print(" Ctrl+C 停止")
    print("=" * 56)

    watch_to = conf.get("watch_to") or ["all"]
    while True:
        try:
            data = http_get("/api/hub/wait?since=%d&timeout=60" % since)
        except Exception as e:
            print("[!] 连接服务失败，3 秒后重试：%s" % e)
            time.sleep(3)
            continue

        msgs = data.get("messages") or []
        if not msgs:
            continue

        since = max(since, max(m["id"] for m in msgs))
        save(STATE, {"since": since})

        target = [m for m in msgs if m.get("to", "all") in watch_to or "all" in watch_to]
        for m in msgs:
            print("[%s] %s -> %s: %s" % (m["id"], m["from"], m.get("to", "all"), m["text"]))

        if target and conf.get("notify_desktop", True):
            notify("协作台新消息", "\n".join("%s: %s" % (m["from"], m["text"]) for m in target))

        reply_to = [m for m in target if m["from"] != (conf.get("auto_reply", {}).get("identity") or "")]
        if reply_to and conf.get("auto_reply", {}).get("enabled"):
            print("    → 正在拉起 agent 生成回复 …")
            ok, info = run_agent(conf["auto_reply"], reply_to)
            print("    → %s：%s" % ("已回帖" if ok else "失败", info))


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    try:
        main()
    except KeyboardInterrupt:
        print("\n监视器已停止")
