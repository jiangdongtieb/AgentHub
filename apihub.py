# -*- coding: utf-8 -*-
"""
apihub.py —— API 中台模块
读取 CC Switch 的供应商列表，并把选中的供应商写入 Claude Code / Codex 的真实配置文件，
效果等同于在 CC Switch 里点一下切换。附带连通性测试。

安全设计：
- 供应商的密钥只保存在本地，绝不返回给前端（前端只看到"已配置"）
- 每次写文件前自动备份到 AgentHub/backups/<时间戳>/
- 生成的 TOML 会先用 tomllib 回读校验，校验不过就放弃写入（保底不破坏配置）
"""
import json
import os
import re
import shutil
import sqlite3
import subprocess
import time
import tomllib
import urllib.request
import urllib.error
import uuid

def _real_home():
    """Loomy 运行时会把 HOME/USERPROFILE 指到自己的目录，
    这里通过多候选 + 目录存在性校验，定位真实的 Windows 用户目录。"""
    cands = []
    d, p = os.environ.get("HOMEDRIVE", ""), os.environ.get("HOMEPATH", "")
    if d and p:
        cands.append(d + p)
    # AgentHub 位于 <home>\Desktop\agent\AgentHub
    here = os.path.dirname(os.path.abspath(__file__))
    cands.append(os.path.dirname(os.path.dirname(os.path.dirname(here))))
    cands.append(os.path.expanduser("~"))
    for c in cands:
        if c and os.path.isdir(c) and os.path.exists(os.path.join(c, ".cc-switch")):
            return c
    for c in cands:
        if c and os.path.isdir(c):
            return c
    return os.path.expanduser("~")


HOME = _real_home()
CC_DIR = os.path.join(HOME, ".cc-switch")
DB_PATH = os.path.join(CC_DIR, "cc-switch.db")
CC_SETTINGS = os.path.join(CC_DIR, "settings.json")
CLAUDE_SETTINGS = os.path.join(HOME, ".claude", "settings.json")
CODEX_CONFIG = os.path.join(HOME, ".codex", "config.toml")
CODEX_AUTH = os.path.join(HOME, ".codex", "auth.json")
BACKUP_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "backups")

APP_LABEL = {"claude": "Claude Code", "codex": "Codex"}
CREATE_NO_WINDOW = 0x08000000


# ------------------------------------------------------------------ 基础读写
def load_json(path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default if default is not None else {}


def save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def read_text(path, default=""):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except Exception:
        return default


def db_conn(readonly=True):
    if readonly:
        return sqlite3.connect("file:%s?mode=ro" % DB_PATH.replace("\\", "/"), uri=True)
    return sqlite3.connect(DB_PATH, timeout=5)


def backup_files(paths):
    ts = time.strftime("%Y%m%d_%H%M%S")
    d = os.path.join(BACKUP_DIR, ts)
    os.makedirs(d, exist_ok=True)
    saved = []
    for p in paths:
        if os.path.exists(p):
            dst = os.path.join(d, os.path.basename(p))
            shutil.copy2(p, dst)
            saved.append(dst)
    return d, saved


# ------------------------------------------------------------------ 读取供应商
def _mask_cfg(app, cfg):
    """把供应商配置转成给前端看的脱敏摘要。"""
    if app == "claude":
        env = cfg.get("env", {}) or {}
        return {
            "base_url": env.get("ANTHROPIC_BASE_URL", ""),
            "model": env.get("ANTHROPIC_MODEL", ""),
            "sonnet": env.get("ANTHROPIC_DEFAULT_SONNET_MODEL", ""),
            "has_key": bool(env.get("ANTHROPIC_AUTH_TOKEN")),
        }
    # codex
    auth = cfg.get("auth", {}) or {}
    toml_txt = cfg.get("config", "") or ""
    base_url = ""
    model = ""
    try:
        t = tomllib.loads(toml_txt)
        model = t.get("model", "")
        mp = t.get("model_providers", {}) or {}
        for _, v in mp.items():
            if isinstance(v, dict) and v.get("base_url"):
                base_url = v["base_url"]
                break
    except Exception:
        pass
    return {
        "base_url": base_url,
        "model": model,
        "sonnet": "",
        "has_key": bool(auth.get("OPENAI_API_KEY")),
    }


def list_providers():
    out = {"claude": [], "codex": []}
    cur = current_ids()
    try:
        c = db_conn(True)
        rows = c.execute(
            "select id, app_type, name, settings_config, sort_index, category "
            "from providers order by app_type, sort_index, created_at").fetchall()
        c.close()
    except Exception as e:
        return {"error": "读取 CC Switch 数据库失败：%s" % e, "claude": [], "codex": []}

    for pid, app, name, cfg_s, sort_i, cat in rows:
        if app not in out:
            continue
        try:
            cfg = json.loads(cfg_s) if isinstance(cfg_s, str) else (cfg_s or {})
        except Exception:
            cfg = {}
        info = _mask_cfg(app, cfg)
        out[app].append({
            "id": pid,
            "name": name,
            "category": cat or "",
            "current": (pid == cur.get(app)),
            **info,
        })
    return {"claude": out["claude"], "codex": out["codex"],
            "current": cur, "labels": APP_LABEL}


def current_ids():
    s = load_json(CC_SETTINGS, {})
    return {"claude": s.get("currentProviderClaude", ""),
            "codex": s.get("currentProviderCodex", "")}


def _get_provider(pid, app):
    c = db_conn(True)
    row = c.execute(
        "select id, name, settings_config from providers where id=? and app_type=?",
        (pid, app)).fetchone()
    c.close()
    if not row:
        return None
    cfg = json.loads(row[2]) if isinstance(row[2], str) else (row[2] or {})
    return {"id": row[0], "name": row[1], "cfg": cfg}


def _common_config(key):
    try:
        c = db_conn(True)
        row = c.execute("select value from settings where key=?", (key,)).fetchone()
        c.close()
        return row[0] if row else ""
    except Exception:
        return ""


# ------------------------------------------------------------------ 当前生效配置
def effective_config(app):
    if app == "claude":
        j = load_json(CLAUDE_SETTINGS, {}) or {}
        env = j.get("env", {}) or {}
        return {
            "file": CLAUDE_SETTINGS,
            "base_url": env.get("ANTHROPIC_BASE_URL", ""),
            "model": env.get("ANTHROPIC_MODEL", ""),
            "has_key": bool(env.get("ANTHROPIC_AUTH_TOKEN")),
        }
    txt = read_text(CODEX_CONFIG)
    model, base_url = "", ""
    try:
        t = tomllib.loads(txt)
        model = t.get("model", "")
        for _, v in (t.get("model_providers", {}) or {}).items():
            if isinstance(v, dict) and v.get("base_url"):
                base_url = v["base_url"]
                break
    except Exception:
        pass
    auth = load_json(CODEX_AUTH, {}) or {}
    return {
        "file": CODEX_CONFIG,
        "base_url": base_url,
        "model": model,
        "has_key": bool(auth.get("OPENAI_API_KEY")),
    }


# ------------------------------------------------------------------ TOML 处理
_BARE = re.compile(r"^[A-Za-z0-9_-]+$")


def _fmt_key(k):
    return k if _BARE.match(k) else '"%s"' % k.replace("\\", "\\\\").replace('"', '\\"')


def _fmt_val(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, str):
        s = v.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
        return '"%s"' % s
    if isinstance(v, list):
        return "[" + ", ".join(_fmt_val(x) for x in v) + "]"
    raise TypeError("unsupported toml value: %r" % (v,))


def _emit(d, prefix, out):
    scalars = [(k, v) for k, v in d.items() if not isinstance(v, dict)]
    tables = [(k, v) for k, v in d.items() if isinstance(v, dict)]
    if prefix:
        out.append("[" + ".".join(_fmt_key(x) for x in prefix) + "]")
    for k, v in scalars:
        out.append("%s = %s" % (_fmt_key(k), _fmt_val(v)))
    for k, v in tables:
        if not v:
            out.append("[" + ".".join(_fmt_key(x) for x in prefix + (k,)) + "]")
        else:
            if out and out[-1].strip():
                out.append("")
            _emit(v, prefix + (k,), out)
    return out


def toml_dumps(d):
    return "\n".join(_emit(d, (), [])) + "\n"


def deep_merge(a, b):
    r = dict(a)
    for k, v in b.items():
        if k in r and isinstance(r[k], dict) and isinstance(v, dict):
            r[k] = deep_merge(r[k], v)
        else:
            r[k] = v
    return r


# ------------------------------------------------------------------ 切换
def switch(app, pid):
    if app not in APP_LABEL:
        return {"ok": False, "msg": "不支持的应用类型"}
    p = _get_provider(pid, app)
    if not p:
        return {"ok": False, "msg": "找不到该供应商"}

    files = [CC_SETTINGS]
    try:
        if app == "claude":
            files.append(CLAUDE_SETTINGS)
        else:
            files += [CODEX_CONFIG, CODEX_AUTH]
        bdir, _ = backup_files(files)

        if app == "claude":
            cur = load_json(CLAUDE_SETTINGS, {}) or {}
            env = (p["cfg"].get("env", {}) or {})
            cur["env"] = env                      # 整块替换 env
            common = _common_config("common_config_claude")
            try:
                for k, v in (json.loads(common) if common else {}).items():
                    cur[k] = v
            except Exception:
                pass
            save_json(CLAUDE_SETTINGS, cur)
        else:
            common_txt = _common_config("common_config_codex")
            base = tomllib.loads(common_txt) if common_txt.strip() else {}
            frag = tomllib.loads(p["cfg"].get("config", "") or "")
            merged = deep_merge(base, frag)
            out = toml_dumps(merged)
            if tomllib.loads(out) != merged:
                return {"ok": False, "msg": "生成的 TOML 校验未通过，已放弃写入"}
            with open(CODEX_CONFIG, "w", encoding="utf-8") as f:
                f.write(out)
            auth = load_json(CODEX_AUTH, {}) or {}
            auth.update(p["cfg"].get("auth", {}) or {})
            save_json(CODEX_AUTH, auth)

        # 更新 CC Switch 的"当前供应商"记录
        s = load_json(CC_SETTINGS, {}) or {}
        s["currentProvider" + app.capitalize()] = pid
        save_json(CC_SETTINGS, s)

        # 同步数据库 is_current（尽力而为，失败不影响生效）
        warn = ""
        try:
            c = db_conn(False)
            c.execute("update providers set is_current=0 where app_type=?", (app,))
            c.execute("update providers set is_current=1 where id=?", (pid,))
            c.commit()
            c.close()
        except Exception as e:
            warn = "（数据库被占用，CC Switch 界面可能显示旧值：%s）" % e

        return {"ok": True, "msg": "已切换 %s → %s%s" % (APP_LABEL[app], p["name"], warn),
                "backup": bdir, "effective": effective_config(app)}
    except Exception as e:
        return {"ok": False, "msg": "切换失败：%s" % e}


# ------------------------------------------------------------------ 连通性测试
def _http(url, headers=None, data=None, method="GET", timeout=15):
    h = {"User-Agent": "curl/8.5.0", "Accept": "application/json"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, data=data, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read(400).decode("utf-8", "replace")
            return r.status, body
    except urllib.error.HTTPError as e:
        return e.code, (e.read(400).decode("utf-8", "replace") if e.fp else "")
    except Exception as e:
        return 0, str(e)


def test_provider(app, pid):
    p = _get_provider(pid, app)
    if not p:
        return {"ok": False, "msg": "找不到该供应商"}
    t0 = time.time()

    if app == "claude":
        env = p["cfg"].get("env", {}) or {}
        base = (env.get("ANTHROPIC_BASE_URL") or "").rstrip("/")
        key = env.get("ANTHROPIC_AUTH_TOKEN", "")
        model = env.get("ANTHROPIC_MODEL") or env.get("ANTHROPIC_DEFAULT_SONNET_MODEL") or "claude-3-5-haiku-20241022"
        if not base:
            return {"ok": False, "msg": "该供应商未配置 base_url（官方登录方式，无需测试）"}
        payload = json.dumps({
            "model": model, "max_tokens": 1,
            "messages": [{"role": "user", "content": "hi"}],
        }).encode("utf-8")
        code, body = _http(base + "/v1/messages",
                           {"content-type": "application/json",
                            "x-api-key": key, "anthropic-version": "2023-06-01"},
                           payload, "POST")
    else:
        cfg = p["cfg"].get("config", "") or ""
        base, wire = "", "responses"
        try:
            t = tomllib.loads(cfg)
            for _, v in (t.get("model_providers", {}) or {}).items():
                if isinstance(v, dict) and v.get("base_url"):
                    base = v["base_url"].rstrip("/")
                    wire = v.get("wire_api", "responses")
                    break
        except Exception:
            pass
        key = (p["cfg"].get("auth", {}) or {}).get("OPENAI_API_KEY", "")
        if not base:
            return {"ok": False, "msg": "该供应商使用官方登录，未配置 base_url"}
        model = ""
        try:
            model = tomllib.loads(cfg).get("model", "")
        except Exception:
            pass
        if wire == "responses":
            payload = json.dumps({"model": model, "input": "hi",
                                  "max_output_tokens": 16}).encode("utf-8")
            code, body = _http(base + "/responses",
                               {"content-type": "application/json",
                                "authorization": "Bearer " + key}, payload, "POST")
        else:
            payload = json.dumps({"model": model, "max_tokens": 1,
                                  "messages": [{"role": "user", "content": "hi"}]}).encode("utf-8")
            code, body = _http(base + "/chat/completions",
                               {"content-type": "application/json",
                                "authorization": "Bearer " + key}, payload, "POST")

    ms = int((time.time() - t0) * 1000)
    ok = 200 <= code < 300
    note = "通" if ok else "失败"
    snip = re.sub(r"\s+", " ", body)[:160]
    return {"ok": ok, "code": code, "ms": ms,
            "msg": "%s（HTTP %s，%dms）%s" % (note, code, ms, snip)}


# ------------------------------------------------------------------ 供应商增删改（内置 CC Switch 的配置能力）
def is_running(image):
    try:
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq %s" % image, "/NH"],
                             capture_output=True, timeout=10,
                             creationflags=CREATE_NO_WINDOW)
        return image.lower().encode() in out.stdout.lower()
    except Exception:
        return False


def cc_switch_busy():
    return is_running("cc-switch.exe")


def backup_db():
    ts = time.strftime("%Y%m%d_%H%M%S")
    d = os.path.join(BACKUP_DIR, ts)
    os.makedirs(d, exist_ok=True)
    for suf in ("", "-wal", "-shm"):
        src = DB_PATH + suf
        if os.path.exists(src):
            shutil.copy2(src, os.path.join(d, "cc-switch.db" + suf))
    return d


def provider_detail(app, pid):
    p = _get_provider(pid, app)
    if not p:
        return {"ok": False, "msg": "找不到该供应商"}
    cfg = p["cfg"]
    d = {"id": pid, "name": p["name"], "app": app, "has_key": False}
    if app == "claude":
        env = cfg.get("env", {}) or {}
        d.update({
            "base_url": env.get("ANTHROPIC_BASE_URL", ""),
            "model": env.get("ANTHROPIC_MODEL", ""),
            "sonnet": env.get("ANTHROPIC_DEFAULT_SONNET_MODEL", ""),
            "opus": env.get("ANTHROPIC_DEFAULT_OPUS_MODEL", ""),
            "haiku": env.get("ANTHROPIC_DEFAULT_HAIKU_MODEL", ""),
            "has_key": bool(env.get("ANTHROPIC_AUTH_TOKEN")),
        })
    else:
        base, model, wire = "", "", "responses"
        try:
            t = tomllib.loads(cfg.get("config", "") or "")
            model = t.get("model", "")
            for _, v in (t.get("model_providers", {}) or {}).items():
                if isinstance(v, dict):
                    base = v.get("base_url", "") or base
                    wire = v.get("wire_api", wire)
        except Exception:
            pass
        d.update({"base_url": base, "model": model, "wire": wire,
                  "has_key": bool((cfg.get("auth") or {}).get("OPENAI_API_KEY"))})
    return {"ok": True, "provider": d}


def save_provider(app, data, pid=None):
    if app not in APP_LABEL:
        return {"ok": False, "msg": "不支持的应用类型"}
    if cc_switch_busy():
        return {"ok": False, "msg": "CC Switch 正在运行，请先关闭它再保存（避免数据库写入冲突）"}

    name = (data.get("name") or "").strip()
    if not name:
        return {"ok": False, "msg": "名称不能为空"}
    base = (data.get("base_url") or "").strip().rstrip("/")
    key = (data.get("key") or "").strip()
    model = (data.get("model") or "").strip()

    existing = _get_provider(pid, app) if pid else None
    if pid and not existing:
        return {"ok": False, "msg": "要编辑的供应商不存在"}

    try:
        if app == "claude":
            cfg = dict(existing["cfg"]) if existing else {}
            env = dict(cfg.get("env", {}) or {})
            if base:
                env["ANTHROPIC_BASE_URL"] = base
            if key:
                env["ANTHROPIC_AUTH_TOKEN"] = key
            if model:
                env["ANTHROPIC_MODEL"] = model
            for f, k in (("sonnet", "ANTHROPIC_DEFAULT_SONNET_MODEL"),
                         ("opus", "ANTHROPIC_DEFAULT_OPUS_MODEL"),
                         ("haiku", "ANTHROPIC_DEFAULT_HAIKU_MODEL")):
                v = (data.get(f) or "").strip()
                if v:
                    env[k] = v
                elif model and k not in env:
                    env[k] = model
            cfg["env"] = env
        else:
            cfg = dict(existing["cfg"]) if existing else {}
            wire = (data.get("wire") or "responses").strip()
            frag = {
                "model_provider": "custom",
                "model": model,
                "model_reasoning_effort": (data.get("effort") or "high").strip(),
                "disable_response_storage": True,
                "model_providers": {"custom": {
                    "name": name.lower().replace(" ", "-"),
                    "wire_api": wire,
                    "requires_openai_auth": True,
                }},
            }
            if base:
                frag["model_providers"]["custom"]["base_url"] = base
            out = toml_dumps(frag)
            if tomllib.loads(out) != frag:          # 自检
                return {"ok": False, "msg": "生成的配置校验未通过"}
            cfg["config"] = out
            auth = dict(cfg.get("auth", {}) or {})
            if key:
                auth["OPENAI_API_KEY"] = key
            if auth:
                cfg["auth"] = auth

        bdir = backup_db()
        ts = int(time.time() * 1000)
        c = db_conn(False)
        if existing:
            c.execute("update providers set name=?, settings_config=? where id=?",
                      (name, json.dumps(cfg, ensure_ascii=False), pid))
            new_id = pid
        else:
            new_id = str(uuid.uuid4())
            sort = c.execute("select coalesce(max(sort_index),0)+1 from providers where app_type=?",
                             (app,)).fetchone()[0]
            c.execute(
                "insert into providers (id,app_type,name,settings_config,website_url,category,"
                "created_at,sort_index,notes,icon,icon_color,meta,is_current,in_failover_queue,"
                "cost_multiplier,provider_type) values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (new_id, app, name, json.dumps(cfg, ensure_ascii=False), "", "custom",
                 ts, sort, None, "custom", "#6aa8ff", "{}", 0, 0, "1.0", None))
        c.execute("delete from provider_endpoints where provider_id=?", (new_id,))
        if base:
            c.execute("insert into provider_endpoints (provider_id,app_type,url,added_at) "
                      "values (?,?,?,?)", (new_id, app, base, ts))
        c.commit()
        c.close()
        return {"ok": True, "msg": ("已更新「%s」" if existing else "已新增「%s」") % name,
                "id": new_id, "backup": bdir}
    except Exception as e:
        return {"ok": False, "msg": "保存失败：%s" % e}


def delete_provider(app, pid):
    if cc_switch_busy():
        return {"ok": False, "msg": "CC Switch 正在运行，请先关闭它再删除"}
    if pid in ("claude-official", "codex-official"):
        return {"ok": False, "msg": "官方供应商不允许删除"}
    p = _get_provider(pid, app)
    if not p:
        return {"ok": False, "msg": "找不到该供应商"}
    if current_ids().get(app) == pid:
        return {"ok": False, "msg": "这是当前正在使用的供应商，请先切到其他供应商再删除"}
    try:
        bdir = backup_db()
        c = db_conn(False)
        c.execute("delete from providers where id=? and app_type=?", (pid, app))
        c.execute("delete from provider_endpoints where provider_id=?", (pid,))
        c.commit()
        c.close()
        return {"ok": True, "msg": "已删除「%s」" % p["name"], "backup": bdir}
    except Exception as e:
        return {"ok": False, "msg": "删除失败：%s" % e}


def restore_backup_dir(bdir):
    """把某个备份目录里的文件还原回去（支持配置文件和 cc-switch 库）。"""
    if not bdir or not os.path.isdir(bdir):
        return {"ok": False, "msg": "备份目录不存在"}
    restored = []
    mapping = {
        "settings.json": CLAUDE_SETTINGS,
        "config.toml": CODEX_CONFIG,
        "auth.json": CODEX_AUTH,
        "cc-switch.db": DB_PATH,
        "cc-switch.db-wal": DB_PATH + "-wal",
        "cc-switch.db-shm": DB_PATH + "-shm",
    }
    try:
        for fn in os.listdir(bdir):
            if fn in mapping:
                shutil.copy2(os.path.join(bdir, fn), mapping[fn])
                restored.append(fn)
        return {"ok": True, "msg": "已还原：%s" % ", ".join(restored) if restored else "目录为空"}
    except Exception as e:
        return {"ok": False, "msg": "还原失败：%s" % e}


# ------------------------------------------------------------------ 余额 / 可用性检测（零计费）
BALANCE_PATHS = [
    ("deepseek.com", "/user/balance"),
    ("moonshot.cn", "/v1/users/me/balance"),
    ("openrouter.ai", "/api/v1/credits"),
]


def _provider_conn(app, cfg):
    """从供应商配置里取出 (base_url, key, model, style)。"""
    if app == "claude":
        env = cfg.get("env", {}) or {}
        return (env.get("ANTHROPIC_BASE_URL", "") or "", env.get("ANTHROPIC_AUTH_TOKEN", "") or "",
                env.get("ANTHROPIC_MODEL", "") or "", "anthropic")
    auth = cfg.get("auth", {}) or {}
    base, wire = "", "responses"
    try:
        t = tomllib.loads(cfg.get("config", "") or "")
        for _, v in (t.get("model_providers", {}) or {}).items():
            if isinstance(v, dict) and v.get("base_url"):
                base = v["base_url"]
                wire = v.get("wire_api", wire)
                break
        model = t.get("model", "")
    except Exception:
        model = ""
    return base, auth.get("OPENAI_API_KEY", "") or "", model, "openai"


def _parse_balance(host, body):
    """把各厂商余额响应解析成人看得懂的金额字符串。"""
    try:
        j = json.loads(body)
    except Exception:
        return ""
    try:
        # DeepSeek: {"balance_infos":[{"currency":"CNY","total_balance":"110.00",...}]}
        if isinstance(j.get("balance_infos"), list) and j["balance_infos"]:
            parts = []
            for b in j["balance_infos"]:
                cur = b.get("currency", "")
                parts.append("%s %s" % (cur, b.get("total_balance", "")))
            return " ｜ ".join(parts)
        # Moonshot / Kimi: {"data":{"available_balance":49.58,...}}
        d = j.get("data")
        if isinstance(d, dict):
            if "total_credits" in d:      # OpenRouter
                tc, tu = float(d.get("total_credits", 0)), float(d.get("total_usage", 0))
                return "总额 %.2f / 已用 %.2f / 剩余 %.2f" % (tc, tu, tc - tu)
            for k, label in (("available_balance", "可用余额"),
                             ("cash_balance", "现金余额"),
                             ("voucher_balance", "代金券")):
                if k in d and d[k] not in (None, ""):
                    return "%s %s" % (label, d[k])
        # 通用兜底：找常见字段
        for k in ("balance", "total_balance", "available_balance", "credits", "remain"):
            if k in j:
                return "%s %s" % (k, j[k])
    except Exception:
        pass
    return ""


def check_provider(app, pid):
    """零计费探活：GET /models（不产生 token 消耗）+ 厂商余额接口（若开放）。"""
    p = _get_provider(pid, app)
    if not p:
        return {"ok": False, "msg": "找不到该供应商"}
    base, key, model, style = _provider_conn(app, p["cfg"])
    if not base:
        return {"ok": True, "reachable": None, "msg": "该供应商为官方登录方式，无 base_url，无法探测",
                "balance": None}
    base = base.rstrip("/")
    out = {"ok": True, "base": base, "model": model, "models": None, "balance": None}

    # 1) 探活：优先 GET /v1/models（免费）。逐个试，取第一个 200；都不行就留最有用的一条
    cands = [base + "/v1/models"]
    if base.endswith("/anthropic"):
        cands.insert(0, base[: -len("/anthropic")] + "/v1/models")
    cands.append(base + "/models")
    fallback = None
    for url in cands:
        code, body = _http(url, {"authorization": "Bearer " + key}, None, "GET", timeout=12)
        if not code:                      # 网络层失败，继续试下一个
            continue
        item = {"url": url, "http": code, "ok": 200 <= code < 300,
                "snippet": re.sub(r"\s+", " ", body)[:120]}
        if item["ok"]:
            out["models"] = item
            break
        if fallback is None and code != 403 and code != 404:
            fallback = item
    if out["models"] is None and fallback is not None:
        out["models"] = fallback
    out["reachable"] = None if out["models"] is None else out["models"]["ok"]

    # 2) 余额：厂商开放接口才查，并解析金额
    #    DeepSeek 有 WAF 冷却，被限流时回退到"上次查到的值"（缓存）
    for host, path in BALANCE_PATHS:
        if host in base:
            url = ("https://" + host) + path
            got = None
            for attempt in range(3):
                time.sleep(0.8 if attempt == 0 else 2.5)
                code, body = _http(url, {"authorization": "Bearer " + key}, None, "GET", timeout=15)
                if code == 200:
                    got = (code, _parse_balance(host, body))
                    break
                last = (code, body)
            if got:
                code, amount = got
                _save_bal_cache(pid, amount, app)
                out["balance"] = {"http": code, "ok": True, "amount": amount, "cached": False,
                                  "raw": re.sub(r"\s+", " ", body)[:200]}
            else:
                code, body = last
                ch = _load_bal_cache(pid)
                out["balance"] = {
                    "http": code, "ok": False,
                    "amount": ch.get("amount", "") if ch else "",
                    "cached": bool(ch),
                    "cached_at": ch.get("ts", "") if ch else "",
                    "msg": ("被厂商限流（HTTP %s），显示的是上次查到值" % code) if ch
                           else "被厂商限流（HTTP %s），请稍后重试" % code,
                    "raw": re.sub(r"\s+", " ", body)[:200],
                }
            break
    if out["balance"] is None:
        out["balance"] = {"ok": None, "amount": "",
                          "msg": "该厂商未开放余额接口（只能靠 /models 探活）"}
    return out


def check_all():
    """检测所有 CC Switch 供应商（零计费）。"""
    res = {"claude": [], "codex": []}
    try:
        c = db_conn(True)
        rows = c.execute("select id, name from providers where app_type in ('claude','codex')").fetchall()
        c.close()
    except Exception as e:
        return {"error": str(e)}
    for pid, name in rows:
        app = "claude" if any(p["id"] == pid for p in list_providers()["claude"]) else "codex"
        r = check_provider(app, pid)
        r["name"] = name
        r["app"] = app
        r["id"] = pid
        res[app].append(r)
    return res


def cli_status():
    """各 CLI 的登录态探测（只看本地凭据文件，不调用模型、不花额度）。"""
    import time as _t
    specs = [
        ("claude", "Claude Code", [HOME + r"\.claude\settings.json"]),
        ("codex", "Codex", [HOME + r"\.codex\auth.json", HOME + r"\.codex\config.toml"]),
        ("codebuddy", "CodeBuddy", [HOME + r"\.codebuddy"]),
        ("kimi", "Kimi Code", [HOME + r"\.kimi-code\credentials", HOME + r"\.kimi-code\config.toml"]),
        ("traecli", "TraeCode", [HOME + r"\.trae-cli", HOME + r"\.trae",
                                 HOME + r"\AppData\Roaming\trae-cli", HOME + r"\AppData\Local\trae-cli\.auth"]),
        ("qwen", "Qwen Code", [HOME + r"\.qwen", HOME + r"\.qwen\oauth_creds.json",
                               HOME + r"\.qwen\.env"]),
    ]
    out = []
    for cli, label, paths in specs:
        found, latest = None, 0
        for p in paths:
            if os.path.exists(p):
                found = p
                m = os.path.getmtime(p)
                latest = max(latest, m)
        out.append({
            "cli": cli, "label": label,
            "logged": bool(found),
            "path": found or "",
            "last": _t.strftime("%Y-%m-%d %H:%M", _t.localtime(latest)) if latest else "",
        })
    return out


def _user_env():
    """还原真实用户环境（Loomy 运行时会改写 HOME/APPDATA 等）。"""
    env = os.environ.copy()
    if HOME and os.path.isdir(HOME):
        env["USERPROFILE"] = HOME
        env["HOME"] = HOME
        env["APPDATA"] = os.path.join(HOME, "AppData", "Roaming")
        env["LOCALAPPDATA"] = os.path.join(HOME, "AppData", "Local")
        env["TEMP"] = os.path.join(HOME, "AppData", "Local", "Temp")
        env["TMP"] = env["TEMP"]
    return env


def restart_claude():
    """结束正在跑的 claude 会话，再重新拉起。"""
    try:
        subprocess.run(["taskkill", "/IM", "claude.exe", "/T", "/F"],
                       capture_output=True, timeout=10, creationflags=CREATE_NO_WINDOW)
    except Exception:
        pass
    time.sleep(0.6)
    try:
        subprocess.Popen('start "" cmd /k claude', shell=True,
                         cwd=HOME, env=_user_env(), creationflags=CREATE_NO_WINDOW)
        return {"ok": True, "msg": "已重启 Claude Code"}
    except Exception as e:
        return {"ok": False, "msg": "重启失败：%s" % e}
