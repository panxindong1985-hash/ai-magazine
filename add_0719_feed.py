#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一次性脚本：把 AI HOT 实时「今日」feed 中 2026-07-19 发布的条目补入 07-19 日报。
原因：AI HOT 的 /api/public/daily/{date} 是「当日 00:00 UTC 的定点快照」，
只含 07-18 00:00→07-19 00:00 UTC 的 2 条；而用户截图里的 3+ 条（AI 热潮/ChatGPT Work/transcribe.cpp…）
属于 07-19 白天的「实时 feed」发布，不在定点快照里。
本脚本直接取实时 feed 的 07-19 条目（含 AI HOT 已译中文 titleZh/summaryZh），
追加进 archive.json 的 2026-07-19 期，再用 --render-only 重渲染。
自包含、不 import 主脚本、写盘前备份 archive.json.bak。
"""
import json, ssl, sys, urllib.request, shutil, datetime

ARCH = "archive.json"
DATE = "2026-07-19"
FEED = "https://aihot.virxact.com/api/public/feed?take=50"

ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE


def http_get_json(u):
    req = urllib.request.Request(u, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
    with urllib.request.urlopen(req, context=ctx, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def domain_of(url):
    try:
        from urllib.parse import urlparse
        h = urlparse(url).netloc
        return h[4:] if h.startswith("www.") else h
    except Exception:
        return url


def main():
    arch = json.load(open(ARCH, encoding="utf-8"))
    feed = http_get_json(FEED)
    items = [it for it in feed.get("items", []) if (it.get("publishedAt") or "").startswith(DATE)]
    print(f"[feed] {DATE} 发布条目：{len(items)}")

    rec = arch.get(DATE)
    if not rec:
        rec = {"meta": {}, "sections": [], "lead": None}
        arch[DATE] = rec

    # 计算当前最大 seq，便于递增
    max_seq = 0
    for s in rec.get("sections", []):
        for it in s.get("items", []):
            max_seq = max(max_seq, it.get("seq", 0))

    new_items = []
    for it in items:
        iid = it.get("id")
        if not iid:
            continue
        title = it.get("titleZh") or it.get("title") or ""
        summary = it.get("summaryZh") or ""
        if not title:
            continue
        # 去重：若已存在同 permalink 则跳过
        permalink = f"https://aihot.virxact.com/items/{iid}"
        dup = any(permalink == x.get("permalink")
                  for s in rec.get("sections", []) for x in s.get("items", []))
        if dup:
            print(f"  跳过重复：{title[:30]}")
            continue
        max_seq += 1
        entry = {
            "seq": max_seq,
            "title": title,
            "source": it.get("source", {}).get("name") or domain_of(it.get("url", "")),
            "summary": summary,
            "url": it.get("url", ""),
            "permalink": permalink,
            "publishedAt": it.get("publishedAt"),
            "exact": True,
            "content": summary,   # AI HOT 已译中文摘要作为正文（无需翻译）
            "zh": True,
        }
        new_items.append(entry)

    if not new_items:
        print("[完成] 无新条目需追加。")
        return

    # 放进一个「今日精选」section（若不存在则新建）
    sec = None
    for s in rec.get("sections", []):
        if s.get("label") == "今日精选":
            sec = s
            break
    if sec is None:
        sec = {"label": "今日精选", "color": "#dc2626", "items": []}
        rec.setdefault("sections", []).append(sec)
    sec["items"].extend(new_items)

    # 备份 + 写盘
    shutil.copy2(ARCH, ARCH + ".bak")
    json.dump(arch, open(ARCH, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"[写入] 已追加 {len(new_items)} 条到 {DATE} 的「今日精选」section：")
    for e in new_items:
        print(f"   + {e['title'][:46]}  ({domain_of(e['url'])})")


if __name__ == "__main__":
    main()
