# -*- coding: utf-8 -*-
r"""
把中转 API 配进 CC Switch（Claude Code + Codex 两边）

用法：
    python 配置中转.py [base_url] [模型名] [wire]

默认：
    base_url = https://api.f-api.site
    模型     = deepseek-v4.1-flash
    wire     = chat        （Codex 走 OpenAI 兼容 /v1/chat/completions）

说明：
  - key 从桌面 C:\Users\35632\Desktop\api.txt 读取，**永不打印**
  - 同名供应商自动覆盖（不会重复添加）
  - 配完用零消耗的 GET /v1/models 验证
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import apihub

KEY_FILE = r"C:\Users\35632\Desktop\api.txt"


def mask(s):
    s = str(s)
    return "*" * len(s) if len(s) <= 10 else s[:6] + "…" + s[-4:]


def main():
    base = (sys.argv[1] if len(sys.argv) > 1 else "https://api.f-api.site").strip().rstrip("/")
    model = (sys.argv[2] if len(sys.argv) > 2 else "deepseek-v4.1-flash").strip()
    wire = (sys.argv[3] if len(sys.argv) > 3 else "chat").strip()
    if not base.startswith("http"):
        base = "https://" + base

    # api.txt 里可能混着配置说明，只取第一个 sk- 开头的 token
    import re
    _t = open(KEY_FILE, "r", encoding="utf-8", errors="replace").read()
    _m = re.search(r"sk-[A-Za-z0-9_\-]{16,}", _t)
    key = _m.group(0) if _m else _t.strip().splitlines()[0].strip()
    print("key 已读取：", mask(key))
    print("base =", base, "| 模型 =", model, "| wire =", wire)

    # 先删掉同名旧供应商，保证不重复
    for app, name in (("claude", "[中转] Claude"), ("codex", "[中转] Codex")):
        for p in apihub.list_providers()[app]:
            if p["name"] == name:
                d = apihub.delete_provider(app, p["id"])
                print("  清理旧的：", d.get("msg"))

    # Claude Code 侧：base 直接用，不加 /anthropic
    r1 = apihub.save_provider("claude", {
        "name": "[中转] Claude", "base_url": base, "key": key, "model": model,
    })
    print("Claude Code 侧：", r1.get("msg"), "|", r1.get("msg") or r1.get("detail"))
    if not r1.get("ok"):
        print("  失败：", r1.get("msg"))
        return

    # Codex 侧：wire 用 chat（OpenAI 兼容），不要 responses
    r2 = apihub.save_provider("codex", {
        "name": "[中转] Codex", "base_url": base, "key": key, "model": model, "wire": wire,
    })
    print("Codex 侧：", r2.get("msg"))
    if not r2.get("ok"):
        print("  失败：", r2.get("msg"))
        return

    # 零消耗验证
    print()
    print("=== 验证（GET /v1/models，零计费）===")
    for app, name in (("claude", "[中转] Claude"), ("codex", "[中转] Codex")):
        for p in apihub.list_providers()[app]:
            if p["name"] == name:
                ch = apihub.check_provider(app, p["id"])
                m = ch.get("models") or {}
                ok = ch.get("reachable")
                print("  %-14s -> %s  HTTP %s" % (name, "✅ 可用" if ok else "❌ 不可用", m.get("http", "—")))
                if m.get("snippet"):
                    print("       ", m["snippet"][:150])
    print()
    print("完成。")


if __name__ == "__main__":
    main()
