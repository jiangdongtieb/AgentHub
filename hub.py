# -*- coding: utf-8 -*-
"""
hub.py —— 协作台客户端（给任何 agent 用，纯标准库）

用法：
  python hub.py read [since_id]            读取消息（默认全部）
  python hub.py send <from> <text> [to]    发一条消息
  python hub.py watch [since_id] [秒]      轮询新消息

示例：
  python hub.py send Loomy "我在" 
  python hub.py read 0
"""
import json
import sys
import time
import urllib.request

BASE = "http://127.0.0.1:8733"


def _get(path):
    with urllib.request.urlopen(BASE + path, timeout=10) as r:
        return json.loads(r.read().decode("utf-8"))


def _post(path, obj):
    data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(BASE + path, data=data, method="POST",
                                 headers={"Content-Type": "application/json; charset=utf-8"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode("utf-8"))


def read(since=0):
    return _get("/api/hub?since=%d" % int(since)).get("messages", [])


def send(frm, text, to="all"):
    return _post("/api/hub", {"from": frm, "text": text, "to": to})


def main(argv):
    cmd = argv[1] if len(argv) > 1 else "read"
    if cmd == "read":
        since = int(argv[2]) if len(argv) > 2 else 0
        for m in read(since):
            print("[%s] %s -> %s: %s" % (m["id"], m["from"], m.get("to", "all"), m["text"]))
    elif cmd == "send":
        r = send(argv[2], argv[3], argv[4] if len(argv) > 4 else "all")
        print(json.dumps(r, ensure_ascii=False))
    elif cmd == "watch":
        since = int(argv[2]) if len(argv) > 2 else 0
        dur = float(argv[3]) if len(argv) > 3 else 60
        end = time.time() + dur
        while time.time() < end:
            for m in read(since):
                since = max(since, m["id"])
                print("[%s] %s: %s" % (m["id"], m["from"], m["text"]), flush=True)
            time.sleep(2)
    else:
        print(__doc__)


if __name__ == "__main__":
    main(sys.argv)
