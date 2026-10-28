# -*- coding: utf-8 -*-
r"""
brainstorm.py —— 协作台「头脑风暴模式」引擎

流程：idea -> N 位参与者按轮次独立提案 / 中立评审 / 自我改进 -> 每轮总结 -> 最终收敛
隔离：每位参与者一个独立工作目录 ws/<名字>/，产物按轮归档 round-N/<名字>.md
"""
import json
import os
import shutil
import subprocess
import threading
import time

import orchestrator as O

HERE = os.path.dirname(os.path.abspath(__file__))
CONF_FILE = os.path.join(HERE, "brainstorm.json")
ROOT = os.path.join(HERE, "brainstorm")

DEFAULT_CONF = {
    "participants": [
        {"id": "b1", "name": "方案甲", "cli": "codex"},
        {"id": "b2", "name": "方案乙", "cli": "claude"},
        {"id": "b3", "name": "方案丙", "cli": "codebuddy"},
    ],
    "summarizer": "b1",
    "rounds": 3,
    "parallel": False,
    "timeout": 900,
    "materials": "",          # 共享的只读材料目录（你的 idea/资料文件放这里）
}

MAT_NOTE = """

参考资料（**只读**）：{path}
你的工作目录下有一个 `材料` 目录指向它（同一份内容）。需要时自己去读里面的文件；
不要修改、不要删除里面的任何东西。"""


def link_materials(ws, materials):
    """把材料目录以 junction 方式挂到某人工作目录下的 材料\ （不复制、不占空间）。
    失败就静默跳过（提示词里仍给了绝对路径）。"""
    if not materials or not os.path.isdir(materials):
        return False
    target = os.path.join(ws, "材料")
    try:
        if os.path.exists(target):
            return True
        r = subprocess.run(["cmd", "/c", "mklink", "/J", target, materials],
                           capture_output=True, creationflags=O.CREATE_NO_WINDOW)
        if r.returncode != 0:      # 退回复制目录内容（浅拷贝一层文件）
            os.makedirs(target, exist_ok=True)
            for fn in os.listdir(materials):
                s = os.path.join(materials, fn)
                d = os.path.join(target, fn)
                if os.path.isfile(s):
                    shutil.copy2(s, d)
        return True
    except Exception:
        return False

_EVENTS = []
_EID = [0]
_LOCK = threading.Lock()
_RUNNING = {"on": False}
STATE = {"run": "", "dir": "", "round": 0, "phase": ""}


# ------------------------------------------------------------------ 配置
def get_conf():
    d = O._load(CONF_FILE, None)
    if not d:
        O._save(CONF_FILE, DEFAULT_CONF)
        return json.loads(json.dumps(DEFAULT_CONF))
    for k, v in DEFAULT_CONF.items():
        d.setdefault(k, v)
    return d


def set_conf(patch):
    d = get_conf()
    if isinstance(patch, dict):
        for k in ("participants", "summarizer", "rounds", "parallel", "timeout", "materials"):
            if k in patch:
                d[k] = patch[k]
    O._save(CONF_FILE, d)
    return d


def view():
    c = get_conf()
    clis = O.available_clis()
    parts = []
    for p in c["participants"]:
        parts.append({**p, "label": clis.get(p["cli"], {}).get("label", p["cli"]),
                      "provider": O.slot_provider(p["cli"]),
                      "effort": p.get("effort", ""),
                      "effort_levels": (O.EFFORT.get(p["cli"]) or {}).get("levels", []),
                      "is_summarizer": p["id"] == c["summarizer"]})
    return {"conf": c, "participants": parts, "clis": clis,
            "running": _RUNNING["on"], "state": STATE}


# ------------------------------------------------------------------ 事件
def emit(kind, text, **extra):
    _EID[0] += 1
    ev = {"id": _EID[0], "ts": time.strftime("%H:%M:%S"), "kind": kind, "text": text}
    ev.update(extra)
    _EVENTS.append(ev)
    if len(_EVENTS) > 3000:
        del _EVENTS[:800]
    try:
        import urllib.request
        data = json.dumps({"from": extra.get("who") or "头脑风暴", "text": text},
                          ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(O.HUB + "/api/hub", data=data, method="POST",
                                     headers={"Content-Type": "application/json; charset=utf-8"})
        urllib.request.urlopen(req, timeout=5).read()
    except Exception:
        pass
    return ev


def events_since(since):
    return [e for e in _EVENTS if e["id"] > int(since)]


# ------------------------------------------------------------------ 提示词（"skill"：角色约束写在这里）
R1 = """你是「{name}」，本场头脑风暴的第 {idx}/{tot} 位参与者。**本轮你只做提案，看不到其他人。**

你的身份：方案提出者。
请针对下面的 idea，给出一份**足够详细、可执行、且你自己已经检过可行性**的方案。

硬性要求：
1. 必须写清：目标 / 具体步骤 / 需要的资源或依赖 / 风险与应对 / 怎么验证它真的可行
2. 具体到别人能照着做，不要停在"建议深入研究"这种空话
3. 不要评论其他人（本轮你也看不到）
4. 直接输出方案正文，不要寒暄

idea：
{idea}"""

RN = """你是「{name}」。第 {round}/{rounds} 轮。本轮你必须**分两部分输出**，不允许混在一起。

【第一部分 · 评委】
对其他每一位参与者的方案做**中立评审**。你是评委，不是参赛者：
- 禁止因为"跟我的想法不一样"就否定，也禁止因为"跟我像"就加分
- 只看四件事：可行性 / 依据是否充分 / 有没有漏掉的风险 / 成本与代价
- 每人一段，格式固定：
  - 对象：<名字>
  - 优点：…
  - 问题：…
  - 建议：…

【第二部分 · 参赛者】
在上述中立评审的基础上，指出**我自己上一版方案的不足**，并给出改进后的方案。
最后必须单列一行：`相对上一版，我改了：…`

idea：
{idea}

其他人的方案（上一轮）：
{others}"""

RF = """你是「{name}」。这是最后一轮（第 {round}/{rounds} 轮），本轮要**收敛**。

请输出你自己的最终方案，并在结尾单列两段：
- `我采纳了谁的哪一点：` （写清来自谁）
- `我否决了谁的哪一点及理由：` （写清来自谁）

如果某个分歧仍未解决，明确写 `未决：…`，不要假装已经统一。

idea：
{idea}

其他人的方案（上一轮）：
{others}"""


def SUMMARIZE(round_no, rounds):
    return """你是本场头脑风暴的**总结人**。下面是第 %d/%d 轮全部参与者的产出。

请输出一份**简洁**的 Markdown（不要复述全文，不要客套），固定包含四节：
## 1. 各自一句话核心
## 2. 关键分歧（谁 vs 谁，分歧点是什么）
## 3. 本轮相对上一轮的核心变化（delta）
## 4. 尚未解决的问题

材料：
%s"""


FINAL = """你是本场头脑风暴的**总结人**。下面是全部 %d 轮的材料与每轮总结。

请给出一份**最终结论**（Markdown）：
## 1. 最终方案
## 2. 各参与者的贡献（谁贡献了哪一点，被采纳的写清楚）
## 3. 被否决的想法及理由
## 4. 仍存在的风险与未决问题
## 5. 下一步可执行动作（具体到可立刻开始的粒度）

材料：
%s"""


# ------------------------------------------------------------------ 主流程
def run(idea):
    with _LOCK:
        if _RUNNING["on"]:
            return {"ok": False, "msg": "已有头脑风暴在进行"}
        _RUNNING["on"] = True
    threading.Thread(target=_run, args=(idea,), daemon=True).start()
    return {"ok": True, "msg": "已开始"}


def _run(idea):
    try:
        conf = get_conf()
        parts = [p for p in conf["participants"] if p.get("cli")]
        rounds = max(1, int(conf.get("rounds", 3)))
        if len(parts) < 2:
            emit("error", "至少需要 2 位参与者")
            return
        sumr = next((p for p in parts if p["id"] == conf.get("summarizer")), parts[0])
        mat = (conf.get("materials") or "").strip()
        if mat and not os.path.isdir(mat):
            mat = ""
        mat_note = MAT_NOTE.format(path=mat) if mat else ""

        run_id = time.strftime("%Y%m%d-%H%M%S")
        base = os.path.join(ROOT, run_id)
        os.makedirs(base, exist_ok=True)
        STATE.update(run=run_id, dir=base, round=0, phase="")
        with open(os.path.join(base, "idea.md"), "w", encoding="utf-8") as f:
            f.write("# idea\n\n" + idea + "\n")

        emit("task", "【头脑风暴 %s】%d 人 × %d 轮 · 总结人：%s\nidea：%s"
             % (run_id, len(parts), rounds, sumr["name"], idea))

        prev = {}                       # name -> 上一轮产出
        round_files = []

        for r in range(1, rounds + 1):
            STATE.update(round=r, phase="提案" if r == 1 else ("收敛" if r == rounds else "评审+改进"))
            rd = os.path.join(base, "round-%d" % r)
            os.makedirs(rd, exist_ok=True)
            emit("round", "── 第 %d/%d 轮（%s）开始 ──" % (r, rounds, STATE["phase"]))

            now = {}
            lock = threading.Lock()

            def one(p):
                ws = os.path.join(base, "ws", p["name"])
                os.makedirs(ws, exist_ok=True)
                if mat:
                    link_materials(ws, mat)
                if r == 1:
                    prompt = R1.format(name=p["name"], idx=parts.index(p) + 1, tot=len(parts), idea=idea)
                else:
                    others = "\n\n".join("### %s\n%s" % (k, v) for k, v in prev.items() if k != p["name"])
                    tpl = RF if r == rounds else RN
                    prompt = tpl.format(name=p["name"], round=r, rounds=rounds, idea=idea, others=others)
                prompt += mat_note
                emit("dispatch", "%s（%s / %s）第 %d 轮开工" % (p["name"],
                     O.CLIS.get(p["cli"], {}).get("label", p["cli"]),
                     O.slot_provider(p["cli"])["name"], r), who=p["name"])
                ok, out = O.call_cli(p["cli"], prompt, timeout=conf.get("timeout", 900), cwd=ws,
                                     effort=p.get("effort") or None)
                with lock:
                    now[p["name"]] = out if ok else "[失败] " + out
                with open(os.path.join(rd, "%s.md" % p["name"]), "w", encoding="utf-8") as f:
                    f.write("# %s · 第 %d 轮\n\n" % (p["name"], r) + (out if ok else "[失败] " + out))
                emit("result", "%s 第 %d 轮 %s" % (p["name"], r, "完成" if ok else "失败：" + out[:200]),
                     who=p["name"])

            if conf.get("parallel"):
                ts = [threading.Thread(target=one, args=(p,)) for p in parts]
                for t in ts:
                    t.start()
                for t in ts:
                    t.join()
            else:
                for p in parts:          # 串行：防止同时打满额度/互相干扰
                    one(p)

            body = "\n\n".join("### %s\n%s" % (k, v) for k, v in now.items())
            ws_sum = os.path.join(base, "ws", sumr["name"] + "-summary")
            os.makedirs(ws_sum, exist_ok=True)
            if mat:
                link_materials(ws_sum, mat)
            emit("assemble", "总结人（%s）正在写第 %d 轮总结…" % (sumr["name"], r))
            ok, sm = O.call_cli(sumr["cli"], SUMMARIZE(r, rounds) % (r, rounds, body) + mat_note,
                                timeout=conf.get("timeout", 900), cwd=ws_sum,
                                effort=sumr.get("effort") or None)
            with open(os.path.join(rd, "_summary.md"), "w", encoding="utf-8") as f:
                f.write("# 第 %d 轮总结\n\n" % r + sm)
            emit("summary", "第 %d 轮总结：\n%s" % (r, sm[:800]))
            round_files.append("round-%d/_summary.md" % r)
            prev = now

        # 终稿
        STATE.update(phase="终稿")
        allmat = []
        for i in range(1, rounds + 1):
            p = os.path.join(base, "round-%d" % i, "_summary.md")
            if os.path.exists(p):
                allmat.append(open(p, encoding="utf-8").read())
            for x in parts:
                q = os.path.join(base, "round-%d" % i, "%s.md" % x["name"])
                if os.path.exists(q):
                    allmat.append(open(q, encoding="utf-8").read())
        ws_fin = os.path.join(base, "ws", sumr["name"] + "-final")
        os.makedirs(ws_fin, exist_ok=True)
        if mat:
            link_materials(ws_fin, mat)
        emit("assemble", "总结人正在产出最终结论…")
        ok, fin = O.call_cli(sumr["cli"], FINAL % (rounds, "\n\n---\n\n".join(allmat)[:60000]) + mat_note,
                             timeout=conf.get("timeout", 900), cwd=ws_fin,
                             effort=sumr.get("effort") or None)
        with open(os.path.join(base, "final.md"), "w", encoding="utf-8") as f:
            f.write(fin)
        emit("final", fin)
        emit("done", "头脑风暴结束 · 产物在 brainstorm\\%s（final.md / round-N/_summary.md / ws/）" % run_id)
    except Exception as e:
        emit("error", "头脑风暴异常：%s" % e)
    finally:
        _RUNNING["on"] = False
        STATE.update(phase="")
