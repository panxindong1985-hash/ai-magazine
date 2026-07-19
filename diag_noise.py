#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""诊断：扫描 archive.json 中仍残留的 chrome 噪声，统计命中条数与样例。
仅离线分析，不写盘、不联网。用于设计"更强清洗"规则前摸清残噪形态。"""
import json, re, os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
ARCH = os.path.join(HERE, "archive.json")

# 扩展噪声标记（命中即视为仍含 chrome 残噪）
MARKERS = {
    "ad_sponsored": r'(?i)\b(advertis(?:e|ement)|sponsored|promoted|paid (?:content|post|partner)|native ad)\b',
    "subscribe_cta": r'(?i)(subscribe|sign ?up|sign ?in|log ?in|register|newsletter|get the latest|in your inbox|email address)',
    "follow_share": r'(?i)(follow us|follow @|share this|share on|tweet this|like us on|pin it|on (?:twitter|facebook|linkedin|reddit))',
    "comments": r'(?i)(join the conversation|leave a comment|add a comment|comments \(|be the first to comment|disqus)',
    "related": r'(?i)(you may also like|related (?:articles|posts|stories)|recommended for you|read more|more from|more news|next (?:article|post)|previous (?:article|post))',
    "copyright": r'(?i)(©|copyright|all rights reserved|terms of (?:service|use)|privacy policy|cookie (?:policy|settings|notice)|gdpr)',
    "tags_meta": r'(?i)(^|\n)\s*(tags?:|filed under:|categories?:|category:)\s',
    "source_attr": r'(?i)(image credit|photo by|image via|originally published|source:|via \[|this (?:article|post|story) (?:was )?(?:written|published|first appeared))',
    "author_bio": r'(?i)(about the (?:author|writer)|author bio|was this (?:article|post) helpful|let us know (?:in the|your))',
    "skip_content": r'(?i)(skip to (?:content|main)|jump to|back to top|^\s*home\s*›)',
    "cookie_banner": r'(?i)(this (?:site|website) uses cookies|we use cookies|cookie consent)',
    "md_artifacts": r'(!\[[^\]]*\]\(\)|\[\]\([^)]*\)|!\[\]\([^)]*\)|\[image:[^\]]*\]|\bvia\b\s*$)',
    "update_note": r'(?i)(editor\'?s note:|correction:|update:|updated (?:on|at|to))',
    "video_gallery_label": r'(?i)(^\s*(?:video|gallery|slideshow|infographic)\s*[:：]?\s*$)',
    "benchmark_tail": r'(?i)((?:GPQA|MMLU|HLE|SWE|GSM8K|HumanEval|MATH|BBH|ARC)[ -]?\w*\s+(?:[\d.]{1,6}\s*){3,})',
}

def load():
    return json.load(open(ARCH, encoding="utf-8"))

def main():
    arch = load()
    total = 0
    hits = {k: 0 for k in MARKERS}
    samples = {k: [] for k in MARKERS}
    for d, rec in arch.items():
        for sec in rec.get("sections", []):
            for it in sec.get("items", []):
                c = (it.get("content") or "")
                if not c.strip():
                    continue
                total += 1
                for k, pat in MARKERS.items():
                    m = re.search(pat, c)
                    if m:
                        hits[k] += 1
                        if len(samples[k]) < 3:
                            # 取命中上下文行
                            ln = c[max(0, m.start()-40):m.start()+80].replace("\n", " ")
                            samples[k].append((d, it.get("title", "")[:40], ln))
    print(f"总条目(有正文): {total}")
    print("--- 各噪声标记命中条数 ---")
    for k in sorted(hits, key=lambda x: -hits[x]):
        print(f"  {k:18s} {hits[k]:4d}")
    print("\n--- 样例(每类最多3) ---")
    for k in MARKERS:
        if not samples[k]:
            continue
        print(f"\n[{k}]")
        for d, t, ln in samples[k]:
            print(f"  {d} | {t}")
            print(f"     …{ln}…")

if __name__ == "__main__":
    main()
