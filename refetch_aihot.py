#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
refetch_aihot.py — 用 AI HOT 已清洗正文回填/刷新历史日报正文。

背景：
  generate_archive.py 的 fetch_content 已实现「优先 AI HOT 已清洗正文，失败回退 trafilatura」。
  但历史 89 期的内容是早期用 trafilatura 直抓源站得到的，可能残留导航/页脚/登录墙等 chrome 噪声，
  且未走过 AI HOT 正文通路。本脚本对历史条目重新拉取 AI HOT 正文并「安全替换」，从根上消除噪声。

安全策略（绝不丢内容 / 不退化翻译）：
  - AI HOT 正文必须 >= 120 字才考虑替换；
  - 仅当：旧正文缺失/过短、或旧正文含 chrome 噪声、或 AI HOT 正文 >= 旧正文 60% 长度时，才替换；
  - 若旧正文已是干净中文、且 AI HOT 返回的是英文正文，则【保留旧译文】（不退化）；
  - 替换后按语言置 zh：AI HOT 中文正文 -> zh=True（保留）；英文正文 -> zh=False（交由主流程重译）。

用法：
  python refetch_aihot.py                 # 默认刷新最近 30 天
  python refetch_aihot.py --since 60      # 刷新最近 60 天
  python refetch_aihot.py --all           # 刷新全库
  python refetch_aihot.py --dates 2026-07-19,2026-07-18
  python refetch_aihot.py --rebuild-date 2026-07-19   # 重建某天（捕获 AI HOT 新增/更新的条目）
  python refetch_aihot.py --dry-run       # 只打印将要做什么，不写盘

写盘前自动备份 archive.json -> archive.json.bak。
刷新后请再跑一次 `python generate_archive.py` 让主流程完成：英文条目重译 + 排版整理 + 重新渲染。
"""
import sys, os, re, json, ssl, argparse, datetime, urllib.request, urllib.parse, shutil

HERE = os.path.dirname(os.path.abspath(__file__))
ARCH_PATH = os.path.join(HERE, "archive.json")
BASE = "https://aihot.virxact.com/api/public"

SECTIONS = [
    ("模型发布/更新", "#4f46e5"),
    ("产品发布/更新", "#059669"),
    ("行业动态",     "#d97706"),
    ("论文研究",     "#e11d48"),
    ("技巧与观点",   "#0284c7"),
]

# ── AI HOT 反爬墙固定 cookie（跳过 JS 挑战）──────────────────────────────
_AIOHOT_CHALLENGE_CK = "__tst_status=3086345129#; EO_Bot_Ssid=1406074880;"
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
_SSL_CTX = ssl.create_default_context()
_SSL_CTX.check_hostname = False
_SSL_CTX.verify_mode = ssl.CERT_NONE

_BODY_TAG = re.compile(
    r"<(?:li|p|h[1-6]|ul|ol|blockquote|pre|table|img|code|strong|em|a|br|div|span)[ >]", re.I)

# chrome 噪声特征（命中即视为旧正文脏，应被 AI HOT 干净正文替换）
_NOISE_MARKERS = [
    "您已在另一个标签页", "请重新加载以刷新会话", "登录以查看", "登录后可",
    "注册以", "订阅我们", "关注我们", "扫码关注", "微信扫一扫", "网站导航",
    "跳至内容", "EO_Bot_Ssid", "确认您是真人", "enable javascript and cookies",
    "access denied", "are you a robot", "verify you are human", "captcha",
    "继续访问", "安全验证", "请输入验证码", "cookie 政策", "cookie policy",
]


def traolid(p):
    if not p:
        return None
    m = re.search(r"/items/([^/?#]+)", p)
    return m.group(1) if m else p


def ratio_en(s):
    if not s:
        return 0.0
    letters = [c for c in s if c.isascii() and c.isalpha()]
    nonsp = len([c for c in s if not c.isspace()])
    return (len(letters) / nonsp) if nonsp else 0.0


def has_chrome_noise(s):
    if not s:
        return False
    low = s.lower()
    return any(m.lower() in low for m in _NOISE_MARKERS)


def _html_to_text(h):
    """AI HOT 正文 HTML -> 干净纯文本（保留段落/列表/标题结构）。"""
    if not h:
        return ""
    h = re.sub(r"<h([1-6])[^>]*>", "\n\n", h, flags=re.I)
    h = re.sub(r"</h[1-6]>", "\n", h, flags=re.I)
    h = re.sub(r"<li[^>]*>", "\n- ", h, flags=re.I)
    h = re.sub(r"</li>", "", h, flags=re.I)
    h = re.sub(r"<(p|div|blockquote)[^>]*>", "\n", h, flags=re.I)
    h = re.sub(r"</(p|div|blockquote)>", "\n", h, flags=re.I)
    h = re.sub(r"<br\s*/?>", "\n", h, flags=re.I)
    h = re.sub(r"<code[^>]*>", "`", h, flags=re.I)
    h = re.sub(r"</code>", "`", h, flags=re.I)
    h = re.sub(r"<[^>]+>", "", h)
    h = re.sub(r"&amp;", "&", h)
    h = re.sub(r"&lt;", "<", h)
    h = re.sub(r"&gt;", ">", h)
    h = re.sub(r"&quot;", '"', h)
    h = re.sub(r"&#39;", "'", h)
    h = re.sub(r"[ \t]+\n", "\n", h)
    h = re.sub(r"\n{3,}", "\n\n", h)
    return h.strip()


def fetch_aihot_body(pid, timeout=30):
    """带反爬墙 cookie 过墙，解析 Next.js RSC 流式 payload 抽取已清洗正文。
    返回纯文本；空串 / None 表示无正文或抓取失败。"""
    if not pid or not re.match(r"^[A-Za-z0-9_-]+$", pid):
        return None
    url = f"https://aihot.virxact.com/items/{pid}"
    req = urllib.request.Request(url, headers={
        "User-Agent": _UA,
        "Cookie": _AIOHOT_CHALLENGE_CK,
        "Accept-Language": "zh-CN,zh;q=0.9",
    })
    try:
        raw = urllib.request.urlopen(req, timeout=timeout, context=_SSL_CTX).read().decode("utf-8", "ignore")
    except Exception:
        return None
    # 仍被挑战墙挡住（没拿到 __next_f 流式数据）
    if "EO_Bot_Ssid" in raw and "__next_f" not in raw:
        return None
    chunks = re.findall(r"self\.__next_f\.push\(([\s\S]*?)\)\s*</script>", raw)
    strs = []
    for c in chunks:
        try:
            arr = json.loads(c)
        except Exception:
            m = re.match(r'\[\s*\d+\s*,\s*("(?:[^"\\]|\\.)*")', c)
            if not m:
                continue
            arr = [1, json.loads(m.group(1))]
        big = arr[1] if (len(arr) > 1 and isinstance(arr[1], str)) else ""
        strs += re.findall(r'"((?:[^"\\]|\\.){15,})"', big)
    parts, seen = [], set()
    for s in strs:
        if _BODY_TAG.search(s) and len(s) > 30:
            if s not in seen:
                seen.add(s)
                parts.append(s)
    if not parts:
        return None
    return _html_to_text("\n".join(parts))


def should_replace(old, new):
    """安全地决定是否用 AI HOT 正文替换旧正文。"""
    if not new or len(new.strip()) < 120:
        return False
    old = (old or "").strip()
    new_is_cn = ratio_en(new) <= 0.45
    old_is_cn = ratio_en(old) <= 0.45
    if len(old) < 120:
        return True                       # 旧正文缺失/过短 -> 直接采用 AI HOT
    if has_chrome_noise(old):
        return True                       # 旧正文有噪声 -> 采用 AI HOT 干净版
    if old_is_cn and not new_is_cn:
        return False                      # 已有干净中文译文，不退回英文正文
    if len(new) >= 0.6 * len(old):
        return True                       # AI HOT 足够完整 -> 采用（格式更好）
    return False


def beijing_now():
    return datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=8)


def weekday_cn(dstr):
    y, m, d = map(int, dstr.split("-"))
    return ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"][datetime.date(y, m, d).weekday()]


def truncate(s, n=60):
    s = (s or "").strip()
    return s if len(s) <= n else s[:n - 1] + "…"


def fallback_lead(sections):
    for sec in sections or []:
        if sec.get("items"):
            t = sec["items"][0].get("title", "")
            if t.strip():
                return truncate(t, 90)
    return ""


def http_get_json(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": _UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout, context=_SSL_CTX) as r:
        return json.loads(r.read().decode("utf-8", "ignore"))


def build_record(date):
    """按 AI HOT 当前日报重建某天 record（含新增/更新条目）。"""
    daily = http_get_json(f"{BASE}/daily/{date}")
    ordered = {label: [] for label, _ in SECTIONS}
    seq = 0
    for sec in daily.get("sections", []):
        label = sec.get("label")
        if label not in ordered:
            continue
        for it in sec.get("items", []):
            pid = traolid(it.get("permalink"))
            seq += 1
            content = fetch_aihot_body(pid) if pid else ""
            zh = (ratio_en(content) <= 0.45) if content else False
            ordered[label].append({
                "seq": seq,
                "title": (it.get("title") or "").strip(),
                "source": (it.get("sourceName") or "").strip() or "AI HOT",
                "summary": truncate(it.get("summary", ""), 220),
                "url": it.get("sourceUrl") or it.get("permalink") or "",
                "permalink": it.get("permalink", ""),
                "publishedAt": date + "T00:00:00.000Z",
                "exact": False,
                "content": content or "",
                "zh": bool(zh),
            })
    total = sum(len(v) for v in ordered.values())
    present = [{"label": l, "color": c, "items": ordered[l]} for l, c in SECTIONS if ordered[l]]
    y, m, d = map(int, date.split("-"))
    meta = {
        "reportDate": date,
        "reportDateHuman": f"{y}年{m}月{d}日",
        "weekday": weekday_cn(date),
        "total": total,
        "source": "AI HOT",
        "sourceUrl": "https://aihot.virxact.com",
        "generatedAt": beijing_now().strftime("%Y年%m月%d日 %H:%M"),
    }
    lead = fallback_lead(present)
    return {"meta": meta, "sections": present, "lead": lead}


def build_title_pid_map(date):
    """从 AI HOT 当日日报 API 取 标题->item id 映射（历史条目无 permalink 时靠标题匹配）。"""
    try:
        daily = http_get_json(f"{BASE}/daily/{date}")
    except Exception:
        return {}
    m = {}
    for sec in daily.get("sections", []):
        for it in sec.get("items", []):
            t = (it.get("title") or "").strip()
            pid = traolid(it.get("permalink"))
            if t and pid:
                m[t] = pid
    return m


def refetch_bodies(arch, dates):
    stats = {"replaced": 0, "skipped_clean": 0, "aihot_empty": 0, "checked": 0, "no_pid": 0}
    title_cache = {}
    for date in dates:
        rec = arch.get(date)
        if not rec:
            continue
        if date not in title_cache:
            title_cache[date] = build_title_pid_map(date)
        tmap = title_cache[date]
        for sec in rec.get("sections", []):
            for it in sec.get("items", []):
                pid = traolid(it.get("permalink"))
                if not pid:
                    # 历史条目无 permalink -> 用标题匹配 AI HOT item id
                    pid = tmap.get((it.get("title") or "").strip())
                if not pid:
                    stats["no_pid"] += 1
                    continue
                stats["checked"] += 1
                new = fetch_aihot_body(pid)
                if not new:
                    stats["aihot_empty"] += 1
                    continue
                old = it.get("content") or ""
                if should_replace(old, new):
                    it["content"] = new
                    it["zh"] = bool(ratio_en(new) <= 0.45)
                    stats["replaced"] += 1
                else:
                    stats["skipped_clean"] += 1
    return stats


def main():
    ap = argparse.ArgumentParser(description="用 AI HOT 已清洗正文回填历史日报")
    ap.add_argument("--all", action="store_true", help="刷新全库所有日期")
    ap.add_argument("--since", type=int, default=30, help="刷新最近 N 天（默认 30）")
    ap.add_argument("--dates", type=str, default="", help="逗号分隔的日期列表 YYYY-MM-DD")
    ap.add_argument("--rebuild-date", type=str, default="", help="重建指定日期（捕获 AI HOT 新增/更新条目）")
    ap.add_argument("--dry-run", action="store_true", help="只打印计划，不写盘")
    args = ap.parse_args()

    arch = json.load(open(ARCH_PATH, encoding="utf-8"))
    print(f"载入 archive.json：{len(arch)} 期")

    # 选定目标日期
    if args.all:
        target = sorted(arch.keys(), reverse=True)
    elif args.dates:
        target = [d.strip() for d in args.dates.split(",") if d.strip()]
    else:
        cutoff = (beijing_now() - datetime.timedelta(days=args.since)).strftime("%Y-%m-%d")
        target = [d for d in sorted(arch.keys(), reverse=True) if d >= cutoff]

    print(f"目标日期（{len(target)} 个）：{target[0]} ... {target[-1]}")
    if args.rebuild_date:
        print(f"将重建日期：{args.rebuild_date}")

    if args.dry_run:
        print("[dry-run] 不写盘。以下为将执行的操作：")
        print(f"  - 刷新正文：{target}")
        if args.rebuild_date:
            print(f"  - 重建日期：{args.rebuild_date}")
        return

    # 刷新历史正文
    stats = refetch_bodies(arch, target)
    print(f"[刷新] 检查 {stats['checked']} 条 | 替换 {stats['replaced']} | "
          f"保留干净 {stats['skipped_clean']} | AI HOT 无正文 {stats['aihot_empty']} | 无匹配id {stats['no_pid']}")

    # 重建指定日期（捕获 AI HOT 更新）
    if args.rebuild_date:
        try:
            arch[args.rebuild_date] = build_record(args.rebuild_date)
            print(f"[重建] {args.rebuild_date} 完成，条目数 {arch[args.rebuild_date]['meta']['total']}")
        except Exception as e:
            print(f"[重建] {args.rebuild_date} 失败：{e}")

    # 备份 + 写盘
    bak = ARCH_PATH + ".bak"
    shutil.copy2(ARCH_PATH, bak)
    print(f"已备份 -> {bak}")
    json.dump(arch, open(ARCH_PATH, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("已写回 archive.json")
    print("下一步：运行 `python generate_archive.py` 完成英文重译 + 排版整理 + 重新渲染。")


if __name__ == "__main__":
    main()
