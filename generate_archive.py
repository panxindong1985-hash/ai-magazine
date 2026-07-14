#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AI HOT 日报 → 每日文件 + 索引页（无限归档版，可每日定时运行）
- 拉取「全部可用」日报列表（dailies 列表，一次性返回所有历史日期）
- 用 archive.json 做增量清单：已生成的日期不再重抓，只追加新出现的一天
- 通过 items(mode=all, 最近7天窗口) 补全近 7 天条目的真实发布时间；更早的退化为仅显示日期
- 每天产出独立 HTML：ai-daily-YYYY-MM-DD.html（永不删除旧档）
- 产出索引页：index.html（卡片列表，互相跳转）
输出目录：脚本同目录
"""
import json, re, os, sys, ssl, datetime, urllib.request, urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
BASE = "https://aihot.virxact.com/api/public"
# dailies 接口不支持翻页(offset/page 均被忽略)，take 上限约 120(超过即 400)
# 取 120 可覆盖未来约 4 个月；已抓取的日期由 archive.json 永久保留，不会因接口截断而丢失
DAILIES_TAKE = 120

SECTIONS = [
    ("模型发布/更新", "#4f46e5"),
    ("产品发布/更新", "#059669"),
    ("行业动态",     "#d97706"),
    ("论文研究",     "#e11d48"),
    ("技巧与观点",   "#0284c7"),
]

# 主要 AI 公司 → (别名关键词, 阵营)。阵营 region: "us"=美国 / "cn"=中国；甘特图按阵营分块呈现。
COMPANIES = [
    ("OpenAI",  "#10a37f", ["openai", "chatgpt", "sora"], "us"),
    ("Anthropic","#d97706", ["anthropic", "claude"], "us"),
    ("Google",  "#4285f4", ["google", "deepmind", "gemini", "gemma"], "us"),
    ("Meta",    "#0866ff", ["meta", "llama", "llama 3", "llama3"], "us"),
    ("Microsoft","#7c3aed", ["microsoft", "微软", "copilot", "bing"], "us"),
    ("xAI",     "#111827", ["xai", "grok"], "us"),
    ("NVIDIA",  "#76b900", ["nvidia", "英伟达", "h100", "h200", "blackwell"], "us"),
    ("DeepSeek","#e11d48", ["deepseek", "深度求索"], "cn"),
    ("百度",     "#2932e1", ["百度", "文心", "ernie", "千帆"], "cn"),
    ("阿里",     "#ff6a00", ["阿里", "通义", "qwen", "千问"], "cn"),
    ("腾讯",     "#12b7f5", ["腾讯", "混元", "元宝", "hunyuan"], "cn"),
    ("字节",     "#fe2c55", ["字节", "豆包", "coze", "扣子"], "cn"),
    ("智谱",     "#0ea5e9", ["智谱", "chatglm", "zhipu", "glm"], "cn"),
    ("月之暗面", "#8b5cf6", ["月之暗面", "moonshot", "kimi"], "cn"),
]
GANTT_TOP_N = 12  # 时间线甘特图展示事件数最多的 N 家公司
# 甘特时间线中「产品更新」的「仅重要」过滤（激进版，仅作用于时间线；每日日报仍保留全部）。
# 判定为「次要/不展示」：① 指南/教程/观点类(_GUIDE_KW)
#   ② 点版本号 vX.Y.Z（如 Claude Code v2.1.207 这类频繁点发布）
#   ③ 常规功能增量（含 新增/功能/支持/扩展/升级/更新 等词，且未命中强信号）
# 判定为「重要/保留」的覆盖：强信号(_STRONG_KW：开源/公测/重磅/首发/全球上线/正式发布 等) 一定保留。
# 仅基于【标题】判断，避免摘要里的「功能/支持」造成误杀。
_GUIDE_KW = ["指南","如何","为何","为什么","详解","实战","盘点","教程","一文","解析",
             "案例","最佳实践","测评","评测","横评","对比"]
_PATCH_RE = re.compile(r"v?\d+\.\d+\.\d+")   # 三段式点版本号 → 点发布(视为次要)
_STRONG_KW = ["开源","公测","内测","重磅","首发","首秀","全球上线","正式可用","ga",
              "全新","新模型","新平台","正式发布","亮相"]
_INCR_KW = ["新增","新功能","支持","接入","扩展","能力","功能","增强","优化","改进",
            "多项更新","能力提升","升级","更新"]
def is_minor_product(text):
    t = text or ""
    tl = t.lower()
    if any(k in t for k in _GUIDE_KW):
        return True
    if _PATCH_RE.search(tl):
        return True
    if any(k in tl for k in _STRONG_KW):
        return False
    if any(k in tl for k in _INCR_KW):
        return True
    return False

# 模型发布「次要」识别（保守）：仅把明显的微调/蒸馏/小版本/轻量变体判为次要，
# 默认「仅重要」开启时会过滤掉它们。阈值刻意保守，避免误杀真正的旗舰发布。
_MODEL_MINOR_KW = ["微调", "fine-tune", "finetune", "蒸馏", "distill",
                   "point release", "小版本", "轻量版", "mini 版", "lite 版", "nano 版"]
def is_minor_model(text):
    tl = (text or "").lower()
    return any(k in tl for k in _MODEL_MINOR_KW)

def http_get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))

def traolid(permalink):
    if not permalink:
        return None
    m = re.search(r"/items/([^/?]+)", permalink)
    return m.group(1) if m else permalink

# ---------- 全文镜像：服务器端抓取原文 HTML 并抽取正文（尊重版权，保留来源与原文链接） ----------
_SSL_CTX = ssl.create_default_context()
_SSL_CTX.check_hostname = False
_SSL_CTX.verify_mode = ssl.CERT_NONE
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

def fetch_content(url, cap=15000):
    """抓取原文 HTML 并用 trafilatura 抽取正文；失败/无内容返回空串（不抛异常）。"""
    if not url:
        return ""
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": _UA, "Accept-Language": "zh-CN,zh;q=0.9"})
        with urllib.request.urlopen(req, timeout=20, context=_SSL_CTX) as r:
            raw = r.read(5_000_000)  # 上限 5MB，防超大页卡死
            html = raw.decode("utf-8", "ignore")
        try:
            import trafilatura
            text = trafilatura.extract(html, include_comments=False, favor_precision=True)
        except Exception:
            text = None
        if not text:
            # 降级：粗滤 script/style 后去标签
            h2 = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.S | re.I)
            text = re.sub(r"<[^>]+>", " ", h2)
            text = re.sub(r"\s+", " ", text).strip()
        return (text or "").strip()[:cap]
    except Exception:
        return ""

def backfill_content(arch, workers=16):
    """并发回填已有条目缺失的全文；成功/失败都写入 content（空串表示缺失）。返回成功数。"""
    todos = []
    for d, rec in arch.items():
        for s in rec.get("sections", []):
            for it in s.get("items", []):
                if it.get("url") and not it.get("content"):
                    todos.append(it)
    if not todos:
        print("[3.5] 全文缓存已齐，无需回填")
        return 0
    print(f"[3.5] 回填全文镜像：{len(todos)} 条（并发 {workers}）...")
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(fetch_content, it["url"]): it for it in todos}
        for f in as_completed(futs):
            it = futs[f]
            try:
                it["content"] = f.result()
            except Exception:
                it["content"] = ""
            done += 1
            if done % 200 == 0:
                print(f"     {done}/{len(todos)}")
    save_archive(arch)
    ok = sum(1 for it in todos if it.get("content"))
    print(f"     完成：成功 {ok}/{len(todos)}")
    return ok

def beijing_now():
    return datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=8)

def beijing_today_str():
    return beijing_now().strftime("%Y-%m-%d")

def weekday_cn(dstr):
    y, m, d = map(int, dstr.split("-"))
    return ["星期一","星期二","星期三","星期四","星期五","星期六","星期日"][datetime.date(y, m, d).weekday()]

def truncate(s, n=60):
    s = (s or "").strip()
    if len(s) <= n:
        return s
    return s[:n - 1] + "…"

OUT_DIR = os.path.dirname(os.path.abspath(__file__))
ARCHIVE_PATH = os.path.join(OUT_DIR, "archive.json")

def load_archive():
    if os.path.exists(ARCHIVE_PATH):
        try:
            with open(ARCHIVE_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def save_archive(arch):
    with open(ARCHIVE_PATH, "w", encoding="utf-8") as f:
        json.dump(arch, f, ensure_ascii=False)

# ---------- 1. 取全部可用日期列表（含头条标题） ----------
print(f"[1] 拉取全部可用日报列表 (take={DAILIES_TAKE}) ...")
arch = load_archive()
try:
    arch_list = http_get_json(f"{BASE}/dailies?take={DAILIES_TAKE}")
    all_dates = [it["date"] for it in arch_list.get("items", [])]
    lead_map = {it["date"]: it.get("leadTitle") or "" for it in arch_list.get("items", [])}
    all_dates.sort(reverse=True)  # 最新在前
    print(f"    共 {len(all_dates)} 期：{all_dates[0]} ... {all_dates[-1]}")
except Exception as e:
    # 网络抖动：回退到本地已有归档，本次仅重建索引/趋势，不追加新日期
    print(f"    ! 列表拉取失败({e})，回退到本地归档 {len(arch)} 期")
    all_dates = sorted(arch.keys(), reverse=True)
    lead_map = {d: arch[d].get("lead", "") for d in all_dates}

# ---------- 2. 补全近 7 天真实发布时间（仅新生成的日期会用到） ----------
print("[2] 拉取 items 补全近 7 天发布时间 ...")
since = (beijing_now() - datetime.timedelta(days=7)).strftime("%Y-%m-%dT00:00:00Z")
id2pub = {}
try:
    cursor = None
    pages = 0
    while True:
        pages += 1
        params = {"mode": "all", "since": since, "take": 100}
        if cursor:
            params["cursor"] = cursor
        data = http_get_json(f"{BASE}/items?" + urllib.parse.urlencode(params))
        for it in data.get("items", []):
            pid = traolid(it.get("permalink")) or it.get("id")
            if pid and it.get("publishedAt"):
                id2pub[pid] = it["publishedAt"]
        if not data.get("hasNext") or not data.get("nextCursor") or pages >= 20:
            break
        cursor = data.get("nextCursor")
    print(f"    时间映射 {len(id2pub)} 条（翻 {pages} 页）")
except Exception as e:
    print(f"    ! 时间补全失败({e})，新日期将退化为仅显示日期")

# ---------- 3. 逐日组装（增量：已生成的跳过，只追加新日期） ----------
def build_day_record(date):
    daily = http_get_json(f"{BASE}/daily/{date}")
    ordered = {label: [] for label, _ in SECTIONS}
    seq = 0
    for sec in daily.get("sections", []):
        label = sec.get("label")
        if label not in ordered:
            continue
        for it in sec.get("items", []):
            pid = traolid(it.get("permalink"))
            pub = id2pub.get(pid) if pid else None
            exact = pub is not None
            seq += 1
            item = {
                "seq": seq,
                "title": it.get("title", "").strip(),
                "source": it.get("sourceName", "").strip() or "AI HOT",
                "summary": truncate(it.get("summary", ""), 60),
                "url": it.get("sourceUrl") or it.get("permalink") or "",
                "publishedAt": pub or (date + "T00:00:00.000Z"),
                "exact": exact,
            }
            item["content"] = fetch_content(item["url"])  # 新日期即时抓取全文
            ordered[label].append(item)
    total = sum(len(v) for v in ordered.values())
    present = [{"label": l, "color": c, "items": ordered[l]} for l, c in SECTIONS if ordered[l]]
    meta = {
        "reportDate": date,
        "reportDateHuman": f"{date[:4]}年{int(date[5:7])}月{int(date[8:10])}日",
        "weekday": weekday_cn(date),
        "total": total,
        "source": "AI HOT",
        "sourceUrl": "https://aihot.virxact.com",
        "generatedAt": beijing_now().strftime("%Y年%m月%d日 %H:%M"),
    }
    return {"meta": meta, "sections": present, "lead": lead_map.get(date, "")}

print("[3] 增量组装（已生成的跳过）...")
today = beijing_today_str()
new_added = 0
for date in all_dates:
    if date in arch:
        continue
    try:
        arch[date] = build_day_record(date)
        new_added += 1
        print(f"    + {date}: {arch[date]['meta']['total']} 条")
    except Exception as e:
        print(f"    ! {date} 拉取失败: {e}")
save_archive(arch)
print(f"    新增 {new_added} 期；累计 {len(arch)} 期")

# ---------- 3.5 全文镜像回填（仅补缺失，已抓取的跳过） ----------
backfill_content(arch)

# ---------- 4. 渲染每份日报（仅写缺失/新文件，不重写旧档） ----------
DAY_TPL = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>AI HOT 日报 · __REPORTDATE__</title>
<style>
  :root{--bg:#f5f6fb;--card:#fff;--ink:#1f2430;--muted:#6b7280;--line:#e8eaf1;
    --shadow:0 1px 3px rgba(16,24,40,.06),0 8px 24px rgba(16,24,40,.05);}
  *{box-sizing:border-box} html{scroll-behavior:smooth}
  body{margin:0;background:var(--bg);color:var(--ink);
    font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;
    -webkit-font-smoothing:antialiased;line-height:1.55;}
  a{color:inherit} .wrap{max-width:1180px;margin:0 auto;padding:0 18px}
  .hero{background:linear-gradient(135deg,#4f46e5 0%,#7c3aed 55%,#9333ea 100%);color:#fff;
    padding:42px 0 34px;position:relative;overflow:hidden;}
  .hero::after{content:"";position:absolute;right:-80px;top:-80px;width:280px;height:280px;
    background:radial-gradient(circle,rgba(255,255,255,.18),transparent 70%);border-radius:50%}
  .hero .kicker{font-size:13px;letter-spacing:.18em;text-transform:uppercase;opacity:.85;margin:0 0 6px}
  .hero h1{margin:0;font-size:34px;font-weight:800;letter-spacing:.5px}
  .hero .date-line{margin:8px 0 0;font-size:15px;opacity:.92}
  .stats{display:flex;flex-wrap:wrap;gap:10px;margin-top:22px}
  .stat{background:rgba(255,255,255,.14);border:1px solid rgba(255,255,255,.22);border-radius:12px;
    padding:10px 14px;min-width:120px;backdrop-filter:blur(4px)}
  .stat .n{font-size:22px;font-weight:800;line-height:1}
  .stat .l{font-size:12.5px;opacity:.9;margin-top:4px}
  .total-pill{display:inline-flex;align-items:center;gap:8px;margin-top:18px;background:rgba(255,255,255,.16);
    padding:8px 16px;border-radius:999px;font-weight:600;font-size:14px}
  .nav{position:sticky;top:0;z-index:20;background:rgba(255,255,255,.92);backdrop-filter:blur(10px);
    border-bottom:1px solid var(--line);box-shadow:0 2px 10px rgba(16,24,40,.04)}
  .nav .wrap{display:flex;gap:8px;overflow-x:auto;padding:10px 18px;scrollbar-width:none}
  .nav .wrap::-webkit-scrollbar{display:none}
  .nav a{white-space:nowrap;text-decoration:none;font-size:13.5px;font-weight:600;color:var(--muted);
    padding:7px 13px;border-radius:999px;border:1px solid var(--line);background:#fff;transition:.15s}
  .nav a:hover{color:var(--ink);border-color:#c9cdfb}
  .nav a.home{background:#4f46e5;color:#fff;border-color:#4f46e5}
  .backhome{position:fixed;right:20px;bottom:20px;z-index:30;background:#4f46e5;color:#fff;text-decoration:none;
    font-size:14px;font-weight:700;padding:12px 18px;border-radius:999px;box-shadow:0 6px 18px rgba(79,70,229,.35);
    display:inline-flex;align-items:center;gap:7px;transition:.15s}
  .backhome:hover{background:#4338ca;transform:translateY(-1px)}
  .nav a .c{display:inline-block;min-width:20px;text-align:center;margin-left:6px;background:#eef0fe;
    color:#4f46e5;border-radius:999px;font-size:12px;padding:0 7px}
  section.block{padding:30px 0 6px}
  .sec-head{display:flex;align-items:center;gap:10px;margin:0 0 16px}
  .sec-dot{width:12px;height:12px;border-radius:4px}
  .sec-head h2{margin:0;font-size:21px;font-weight:800}
  .sec-head .cnt{font-size:13px;color:var(--muted);font-weight:600}
  .grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(330px,1fr));gap:16px}
  .card{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:18px 18px 16px;
    box-shadow:var(--shadow);display:flex;flex-direction:column;gap:10px;position:relative;
    border-top:3px solid var(--accent,#4f46e5);transition:transform .15s,box-shadow .15s}
  .card:hover{transform:translateY(-3px);box-shadow:0 6px 14px rgba(16,24,40,.10),0 18px 40px rgba(16,24,40,.08)}
  .card .top{display:flex;align-items:center;justify-content:space-between;gap:8px}
  .seq{display:inline-flex;align-items:center;justify-content:center;min-width:30px;height:30px;padding:0 8px;
    border-radius:9px;background:var(--accent,#4f46e5);color:#fff;font-weight:800;font-size:15px}
  .time{font-size:12.5px;color:var(--muted);font-weight:600;white-space:nowrap}
  .card h3{margin:0;font-size:16.5px;font-weight:700;line-height:1.4}
  .chip{display:inline-block;align-self:flex-start;font-size:12px;font-weight:600;color:var(--accent,#4f46e5);
    background:color-mix(in srgb,var(--accent,#4f46e5) 10%,#fff);border:1px solid color-mix(in srgb,var(--accent,#4f46e5) 22%,#fff);
    padding:3px 10px;border-radius:999px}
  .summary{margin:0;font-size:14px;color:#3b4252}
  .readmore{margin-top:auto;display:inline-flex;align-items:center;gap:5px;align-self:flex-start;text-decoration:none;
    font-size:13.5px;font-weight:700;color:var(--accent,#4f46e5)}
  .readmore:hover{text-decoration:underline}
  footer{margin-top:36px;padding:26px 0 40px;border-top:1px solid var(--line);color:var(--muted);font-size:13px;text-align:center}
  footer a{color:#4f46e5;text-decoration:none}
  @media (max-width:560px){.hero h1{font-size:27px}.grid{grid-template-columns:1fr}.stat{min-width:0;flex:1 1 40%}}
  /* 站内统一阅读面板 */
  .card{cursor:pointer}
  .reader-overlay{position:fixed;inset:0;z-index:60;background:rgba(18,20,32,.55);backdrop-filter:blur(3px);
    display:flex;align-items:flex-start;justify-content:center;padding:40px 16px;overflow:auto}
  .reader-overlay[hidden]{display:none}
  .reader{background:#fff;border-radius:18px;max-width:840px;width:100%;box-shadow:0 30px 80px rgba(0,0,0,.32);
    display:flex;flex-direction:column;max-height:90vh;overflow:hidden;animation:pop .18s ease}
  @keyframes pop{from{transform:translateY(8px) scale(.98);opacity:0}to{transform:none;opacity:1}}
  .reader-head{display:flex;align-items:flex-start;justify-content:space-between;gap:12px;
    padding:20px 24px 14px;background:linear-gradient(135deg,#4f46e5,#7c3aed);color:#fff}
  .reader-head h2{margin:0 0 8px;font-size:20px;line-height:1.45;font-weight:800}
  .reader-head .chip{background:rgba(255,255,255,.18);color:#fff;border:1px solid rgba(255,255,255,.3)}
  .reader-x{background:rgba(255,255,255,.2);border:none;color:#fff;font-size:24px;line-height:1;
    width:38px;height:38px;border-radius:10px;cursor:pointer;flex:0 0 auto}
  .reader-x:hover{background:rgba(255,255,255,.34)}
  .reader-body{padding:20px 24px;overflow:auto;color:#2a2f3a;font-size:15.5px;line-height:1.85}
  .reader-body p{margin:0 0 14px}
  .reader-body .r-empty{color:#6b7280;font-style:italic}
  .reader-foot{display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap;
    padding:14px 24px 18px;border-top:1px solid var(--line);background:#fafbff}
  .reader-foot .r-note{font-size:12.5px;color:var(--muted);max-width:62%}
  .reader-foot .readmore{margin:0}
</style>
</head>
<body>
  <header class="hero"><div class="wrap">
    <p class="kicker">AI HOT Daily · 晨报</p>
    <h1>AI 日报 · __REPORTDATEHUMAN__</h1>
    <p class="date-line">__WEEKDAY__ · 数据来源：__SOURCE__</p>
    <div class="total-pill">📊 当日共 <strong style="margin:0 4px">__TOTAL__</strong> 条 AI 动态</div>
    <div class="stats" id="stats"></div>
  </div></header>
  <nav class="nav"><div class="wrap" id="nav"><a href="index.html" class="home">📰 AI资讯杂志</a></div></nav>
  <main class="wrap" id="main"></main>
  <footer>
    <div>本日报共收录 <strong>__TOTAL__</strong> 条动态 · 数据来源：<a href="__SOURCEURL__" target="_blank" rel="noopener noreferrer">__SOURCE__（aihot.virxact.com）</a></div>
    <div style="margin-top:6px">生成于 __GENERATEDAT__ · 时间换算为北京时间 · 点「来源 ↗」跳转原始报道</div>
    <div style="margin-top:8px;opacity:.85">说明：本条新闻的标题、摘要、来源、日期已<b>镜像保存在本页</b>；即便外部链接失效或无法访问境外站点，已存档内容仍可正常查看。</div>
  </footer>
  <a href="index.html" class="backhome" title="返回 AI资讯杂志 首页">📰 返回 AI资讯杂志</a>
  <div class="reader-overlay" id="reader" hidden>
    <div class="reader" role="dialog" aria-modal="true">
      <div class="reader-head">
        <div style="min-width:0">
          <h2 id="readerTitle"></h2>
          <span class="chip" id="readerSource"></span>
        </div>
        <button class="reader-x" onclick="closeReader()" aria-label="关闭">×</button>
      </div>
      <div class="reader-body" id="readerBody"></div>
      <div class="reader-foot">
        <span class="r-note">本文由 <b>AI资讯杂志</b> 整理镜像，原文版权归原作者所有 · 点击右侧按钮查看原始报道</span>
        <a id="readerLink" class="readmore" target="_blank" rel="noopener noreferrer">查看原文 ↗</a>
      </div>
    </div>
  </div>
<script>
const DATA = __DATA__;
function pad(n){return n<10?"0"+n:""+n;}
function toBeijingHuman(iso, ref, exact){
  const d=new Date(iso); const bj=new Date(d.getTime()+8*3600*1000);
  const Y=bj.getUTCFullYear(),M=bj.getUTCMonth()+1,D=bj.getUTCDate();
  const h=bj.getUTCHours(),m=bj.getUTCMinutes(); const hh=pad(h),mm=pad(m);
  if(!exact) return M+"月"+D+"日";
  const [rY,rM,rD]=ref.split("-").map(Number);
  if(Y===rY&&M===rM&&D===rD) return "今天 "+hh+":"+mm;
  const yest=new Date(rY,rM-1,rD); yest.setDate(yest.getDate()-1);
  if(Y===yest.getFullYear()&&M===yest.getMonth()+1&&D===yest.getDate()) return "昨天 "+hh+":"+mm;
  return M+"月"+D+"日 "+hh+":"+mm;
}
const meta=DATA.meta, sections=DATA.sections, ref=meta.reportDate;
const statsEl=document.getElementById("stats");
sections.forEach(s=>{const d=document.createElement("div");d.className="stat";
  d.innerHTML='<div class="n">'+s.items.length+'</div><div class="l">'+s.label+'</div>';statsEl.appendChild(d);});
const navEl=document.getElementById("nav");
sections.forEach((s,i)=>{const a=document.createElement("a");a.href="#sec-"+i;
  a.innerHTML=escapeHtml(s.label)+'<span class="c">'+s.items.length+'</span>';navEl.appendChild(a);});
const mainEl=document.getElementById("main");
sections.forEach((s,i)=>{
  const sec=document.createElement("section");sec.className="block";sec.id="sec-"+i;
  const head=document.createElement("div");head.className="sec-head";
  head.innerHTML='<span class="sec-dot" style="background:'+s.color+'"></span><h2>'+escapeHtml(s.label)+'</h2><span class="cnt">'+s.items.length+' 条</span>';
  sec.appendChild(head);
  const grid=document.createElement("div");grid.className="grid";
  s.items.forEach(it=>{
    const card=document.createElement("article");card.className="card";card.style.setProperty("--accent",s.color);
    const time=toBeijingHuman(it.publishedAt,ref,it.exact);const url=it.url||"#";
    card.innerHTML='<div class="top"><span class="seq">'+it.seq+'</span><span class="time">'+time+'</span></div>'+
      '<h3>'+escapeHtml(it.title)+'</h3><span class="chip">'+escapeHtml(it.source)+'</span>'+
      '<p class="summary">'+escapeHtml(it.summary)+'</p>'+
      '<a class="readmore" href="'+escapeAttr(url)+'" target="_blank" rel="noopener noreferrer" title="外部原文链接，可能需要访问境外站点">来源 ↗</a>';
    card.addEventListener("click", function(){ openReader(it); });
    const rm = card.querySelector(".readmore");
    rm.addEventListener("click", function(e){ e.stopPropagation(); });
    grid.appendChild(card);
  });
  sec.appendChild(grid);mainEl.appendChild(sec);
});
function escapeHtml(s){return (s||"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));}
function escapeAttr(s){return escapeHtml(s);}
function openReader(it){
  const r=document.getElementById("reader");
  const content=(it.content||"").trim();
  const body=document.getElementById("readerBody");
  if(content.length>0){
    body.innerHTML=content.split(/\n{1,}/).map(function(p){p=p.trim();return p?'<p>'+escapeHtml(p)+'</p>':'';}).join("");
  }else{
    body.innerHTML='<p class="r-empty">暂未获取到该条新闻的全文镜像。'+(it.url?'可点击右下角「查看原文 ↗」前往原始报道。':'')+'</p>';
  }
  document.getElementById("readerTitle").textContent=it.title;
  document.getElementById("readerSource").textContent=it.source;
  const link=document.getElementById("readerLink");
  if(it.url){link.href=it.url;link.style.display="";}else{link.style.display="none";}
  r.hidden=false;document.body.style.overflow="hidden";
}
function closeReader(){const r=document.getElementById("reader");r.hidden=true;document.body.style.overflow="";}
document.getElementById("reader").addEventListener("click",function(e){if(e.target===this)closeReader();});
document.addEventListener("keydown",function(e){if(e.key==="Escape")closeReader();});
</script>
</body></html>
"""

INDEX_TPL = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>AI资讯杂志</title>
<style>
  :root{--bg:#f5f6fb;--card:#fff;--ink:#1f2430;--muted:#6b7280;--line:#e8eaf1;
    --shadow:0 1px 3px rgba(16,24,40,.06),0 8px 24px rgba(16,24,40,.05);}
  *{box-sizing:border-box} html{scroll-behavior:smooth}
  body{margin:0;background:var(--bg);color:var(--ink);
    font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;
    -webkit-font-smoothing:antialiased;line-height:1.55;}
  a{color:inherit;text-decoration:none} .wrap{max-width:1180px;margin:0 auto;padding:0 18px}
  .hero{background:linear-gradient(135deg,#4f46e5 0%,#7c3aed 55%,#9333ea 100%);color:#fff;
    padding:44px 0 36px;position:relative;overflow:hidden;}
  .hero::after{content:"";position:absolute;right:-80px;top:-80px;width:280px;height:280px;
    background:radial-gradient(circle,rgba(255,255,255,.18),transparent 70%);border-radius:50%}
  .hero .kicker{font-size:13px;letter-spacing:.18em;text-transform:uppercase;opacity:.85;margin:0 0 6px}
  .hero h1{margin:0;font-size:34px;font-weight:800}
  .hero .sub{margin:8px 0 0;font-size:15px;opacity:.92}
  .grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(340px,1fr));gap:16px;padding:30px 0 10px}
  .day{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:18px;
    box-shadow:var(--shadow);display:flex;flex-direction:column;gap:10px;transition:transform .15s,box-shadow .15s}
  .day:hover{transform:translateY(-3px);box-shadow:0 6px 14px rgba(16,24,40,.10),0 18px 40px rgba(16,24,40,.08)}
  .day .top{display:flex;align-items:baseline;justify-content:space-between;gap:8px}
  .day .date{font-size:20px;font-weight:800}
  .day .wd{font-size:13px;color:var(--muted);font-weight:600}
  .badge{font-size:11px;font-weight:700;color:#fff;background:#4f46e5;padding:2px 9px;border-radius:999px}
  .lead{font-size:14px;color:#3b4252;margin:0;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
  .chips{display:flex;flex-wrap:wrap;gap:6px}
  .chip{font-size:11.5px;font-weight:600;color:#4f46e5;background:#eef0fe;border-radius:999px;padding:2px 9px}
  .foot{display:flex;align-items:center;justify-content:space-between;margin-top:auto;padding-top:6px}
  .total{font-size:13px;color:var(--muted);font-weight:600}
  .go{font-size:13.5px;font-weight:700;color:#4f46e5}
  footer{margin-top:26px;padding:26px 0 40px;border-top:1px solid var(--line);color:var(--muted);font-size:13px;text-align:center}
  footer a{color:#4f46e5;text-decoration:none}
  .trend-head{display:flex;align-items:baseline;gap:12px;flex-wrap:wrap;margin-bottom:14px}
  .trend-head h2{margin:0;font-size:22px;font-weight:800}
  .trend-sub{margin:0;font-size:13px;color:var(--muted)}
  .chart-wrap{position:relative;background:var(--card);border:1px solid var(--line);border-radius:16px;
    padding:14px 14px 6px;box-shadow:var(--shadow)}
  .gantt{margin:30px 0 6px}
  .gantt-ctrl{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:12px}
  .gbtn{font-size:13px;font-weight:600;color:#4b5161;border:1px solid var(--line);background:#fff;
    padding:7px 14px;border-radius:999px;cursor:pointer;user-select:none;transition:.15s}
  .gbtn:hover{border-color:#c9cdfb}
  .gbtn.active{color:#fff}
  .gbtn[data-kind=model].active{background:#4f46e5;border-color:#4f46e5}
  .gbtn[data-kind=product].active{background:#059669;border-color:#059669}
  .gbtn[data-mode].active{background:#4f46e5;border-color:#4f46e5}
  .gbtn#ganttMajor.active{background:#d97706;border-color:#d97706}
  .gsep{width:1px;background:var(--line);margin:3px 4px}
  #ganttChart{width:100%;height:auto;display:block;cursor:grab}
  #ganttChart:active{cursor:grabbing}
  #ganttTip{position:absolute;display:none;pointer-events:none;background:#1f2430;color:#fff;
    font-size:12px;line-height:1.55;padding:8px 10px;border-radius:10px;box-shadow:0 6px 18px rgba(16,24,40,.25);
    z-index:5;max-width:300px}
  #ganttTip b{font-weight:700}
  .timebar{position:sticky;top:0;z-index:15;background:rgba(255,255,255,.95);backdrop-filter:blur(8px);
    border-bottom:1px solid var(--line);display:flex;align-items:center;gap:14px;padding:10px 18px;margin:18px 0 4px}
  .timebar #sliderLabel{font-size:13px;font-weight:700;color:#4f46e5;min-width:118px;white-space:nowrap}
  .timebar input[type=range]{flex:1;accent-color:#4f46e5;cursor:pointer;height:4px}
  .timebar #toNewest{font-size:12.5px;font-weight:700;color:#4f46e5;border:1px solid #c9cdfb;background:#fff;
    border-radius:999px;padding:6px 12px;cursor:pointer;white-space:nowrap}
  .year-h{font-size:26px;font-weight:800;margin:28px 0 2px;color:#1f2430;letter-spacing:.5px}
  .month-h{font-size:15.5px;font-weight:700;color:#4b5161;margin:18px 0 12px;padding-left:11px;border-left:4px solid #4f46e5}
  @media (max-width:560px){.hero h1{font-size:27px}.grid{grid-template-columns:1fr}
    .timebar{flex-wrap:wrap;gap:8px}.timebar #sliderLabel{min-width:0}}
</style>
</head>
<body>
  <header class="hero"><div class="wrap">
    <p class="kicker">AI HOT Daily · Magazine</p>
    <h1>AI 资讯杂志</h1>
    <p class="sub">__RANGE__ · 共 __NDAYS__ 期 · 数据来源：__SOURCE__</p>
  </div></header>
  <section class="gantt wrap">
    <div class="trend-head">
      <h2>🗓️ 主要 AI 公司 模型 / 产品 更新时间线</h2>
      <p class="trend-sub">上方为🇺🇸美国公司、下方为🇨🇳中国公司；每行一家公司，横向为日期；方块=发布/更新事件（蓝=模型发布，绿=产品更新）。产品默认仅显示重大发布（点版本号、常规功能增量、指南类已自动隐藏，可关闭「⚡仅重要」看全部）。滚轮缩放、拖动平移、悬停看详情、点击跳转当日日报 · 事件日期取自日报报道日</p>
    </div>
    <div class="gantt-ctrl">
      <button class="gbtn active" data-kind="model">🔵 模型发布</button>
      <button class="gbtn active" data-kind="product">🟢 产品更新</button>
      <button class="gbtn active" id="ganttMajor">⚡ 仅重要（模型+产品）</button>
      <span class="gsep"></span>
      <span style="align-self:center;font-size:12.5px;color:var(--muted)">标记：</span>
      <button class="gbtn active" data-mode="block">▮ 方块</button>
      <button class="gbtn" data-mode="dot">● 圆点</button>
      <button class="gbtn" data-mode="bar">▎ 竖条</button>
      <button class="gbtn" id="ganttReset" style="margin-left:auto">↺ 重置视图</button>
      <span id="ganttRange" style="font-size:12.5px;color:var(--muted);align-self:center"></span>
    </div>
    <div class="chart-wrap">
      <svg id="ganttChart" preserveAspectRatio="xMidYMid meet" role="img" aria-label="主要 AI 公司模型与产品更新时间线"></svg>
      <div id="ganttTip"></div>
    </div>
    <p class="trend-sub" style="margin-top:8px">提示：在图上滚动鼠标滚轮可放大/缩小某一时间段，按住拖动可平移时间轴。</p>
  </section>
  <main class="wrap">
    <div class="timebar">
      <span id="sliderLabel">—</span>
      <input type="range" id="timeSlider" min="0" max="0" value="0" aria-label="时间轴拖拽快速查阅">
      <button id="toNewest" type="button">↥ 最新</button>
    </div>
    <div id="archive"></div>
  </main>
  <footer>
    <div>数据来源：<a href="__SOURCEURL__" target="_blank" rel="noopener noreferrer">AI HOT（aihot.virxact.com）</a> · 生成于 __GENERATEDAT__</div>
    <div style="margin-top:6px">点击任意一期查看当日完整日报（含五版块与原文跳转）</div>
    <div style="margin-top:8px;opacity:.85">说明：本页已把每条新闻的标题、摘要、来源、日期<b>镜像保存到本地</b>；外部「原文」链接仅作溯源，即使链接失效或无法访问境外站点，已存档内容仍可正常查看。</div>
  </footer>
<script>
const DAYS = __DAYS__;
const GROUPS = __GROUPS__;
const GANTT = __GANTT__;
// ---- 按 年→月 分组渲染（最新年/月在上，每月下按日期倒序列出） ----
const ARCHIVE=document.getElementById("archive");
GROUPS.forEach(g=>{
  const ysec=document.createElement("section"); ysec.className="year";
  const yh=document.createElement("h2"); yh.className="year-h"; yh.textContent=g.year+" 年"; ysec.appendChild(yh);
  g.months.forEach(mo=>{
    const mdiv=document.createElement("div"); mdiv.className="month";
    const mh=document.createElement("h3"); mh.className="month-h"; mh.textContent=mo.month+" 月 · "+mo.days.length+" 期"; mdiv.appendChild(mh);
    const gd=document.createElement("div"); gd.className="grid";
    mo.days.forEach(d=>{
      const el=document.createElement("a"); el.className="day"; el.href=d.file; el.id="day-"+d.date;
      const chips=d.sections.map(s=>'<span class="chip">'+escapeHtml(s.label)+' '+s.count+'</span>').join("");
      el.innerHTML=
        '<div class="top"><span class="date">'+d.meta.reportDateHuman+'</span>'+
          '<span class="wd">'+d.meta.weekday+(d.isToday?' <span class="badge">今天</span>':'')+'</span></div>'+
        (d.lead?'<p class="lead">'+escapeHtml(d.lead)+'</p>':'')+
        '<div class="chips">'+chips+'</div>'+
        '<div class="foot"><span class="total">共 '+d.meta.total+' 条</span><span class="go">查看完整日报 →</span></div>';
      gd.appendChild(el);
    });
    mdiv.appendChild(gd); ysec.appendChild(mdiv);
  });
  ARCHIVE.appendChild(ysec);
});
// ---- 时间轴拖拽进度条：拖动滑块快速定位到对应日期 ----
(function(){
  const slider=document.getElementById("timeSlider");
  const label=document.getElementById("sliderLabel");
  const N=DAYS.length;
  function goto(v){
    const d=DAYS[N-1-v]; if(!d) return;
    const el=document.getElementById("day-"+d.date);
    if(el) el.scrollIntoView({behavior:"smooth",block:"start"});
    label.textContent=d.meta.reportDateHuman;
  }
  slider.max=N-1;
  slider.addEventListener("input",()=>goto(parseInt(slider.value,10)));
  document.getElementById("toNewest").addEventListener("click",()=>{slider.value=N-1;goto(N-1);});
  slider.value=N-1; goto(N-1);
})();
function escapeHtml(s){return (s||"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));}

// ---------- 主要 AI 公司 模型/产品 更新时间线（甘特式 + 缩放/拖动，纯 SVG） ----------
(function(){
  const G=GANTT; if(!G.companies || !G.companies.length) return;
  const svg=document.getElementById("ganttChart");
  const W=960,L=132,R=22,T=20,B=50,rowH=46;
  const REGION={us:{label:"🇺🇸 美国公司",tint:"#f3f5ff",tag:"#4f46e5"},
                cn:{label:"🇨🇳 中国公司",tint:"#fff5f6",tag:"#e11d48"}};
  const headerH=30;
  const usC=G.companies.filter(c=>c.region==="us");
  const cnC=G.companies.filter(c=>c.region==="cn");
  const rows=[];
  if(usC.length) rows.push({type:"h",region:"us"});
  usC.forEach(c=>rows.push({type:"c",c}));
  if(cnC.length) rows.push({type:"h",region:"cn"});
  cnC.forEach(c=>rows.push({type:"c",c}));
  const comps=G.companies;
  let plotH=T; rows.forEach(r=> plotH += (r.type==="h"?headerH:rowH));
  const H=plotH+B;
  svg.setAttribute("viewBox",`0 0 ${W} ${H}`);
  const full0=new Date(G.range[0]+"T00:00:00Z").getTime();
  const full1=new Date(G.range[1]+"T00:00:00Z").getTime();
  const DAY=86400000;
  const MIN_SPAN=3*DAY;                 // 最小可见跨度 3 天
  const plotW=W-L-R;
  let dom0=full0, dom1=full1;           // 当前可见时间窗
  const msPerPx=()=> (dom1-dom0)/plotW;
  const xAt=ms=> L+(ms-dom0)/msPerPx();
  const xAtDate=s=> xAt(new Date(s+"T00:00:00Z").getTime());
  const dateAtVx=vx=> dom0+(vx-L)/plotW*(dom1-dom0);
  const kindColor={model:"#4f46e5",product:"#059669"};
  const kindText={model:"模型发布",product:"产品更新"};
  const show={model:true,product:true};
  let markerMode="block";   // block（方块）| dot（圆点）| bar（竖条）
  let majorOnly=true;       // true=仅重要（过滤次要的模型微调 / 产品小更新）
  const tip=document.getElementById("ganttTip");
  const rangeLabel=document.getElementById("ganttRange");

  function visibleEvents(c){
    return c.events.filter(e=> show[e.kind] && !(majorOnly && e.minor));
  }

  function fmt(ms){const d=new Date(ms);return `${d.getUTCMonth()+1}月${d.getUTCDate()}日`;}
  function tickStep(){const days=(dom1-dom0)/DAY;
    if(days>180) return 30; if(days>70) return 14; if(days>25) return 7; if(days>10) return 3; return 1;}
  function ticks(){
    const step=tickStep(); const out=[];
    let t=Math.ceil((dom0-full0)/(step*DAY))*(step*DAY)+full0;
    for(; t<=dom1+1; t+=step*DAY) out.push(t);
    return out;
  }
  function clamp(){
    let s=dom1-dom0; s=Math.max(MIN_SPAN,Math.min(full1-full0,s));
    if(dom0<full0){dom0=full0;dom1=full0+s;}
    if(dom1>full1){dom1=full1;dom0=full1-s;}
    dom0=Math.max(full0,dom0); dom1=Math.min(full1,dom1);
  }
  function render(){
    clamp();
    let h=""; const rowY={}; let band=0; let y=T;
    // 1) 区域带 + 公司行
    rows.forEach(r=>{
      if(r.type==="h"){
        const reg=REGION[r.region];
        h+=`<rect x="0" y="${y.toFixed(1)}" width="${W}" height="${headerH}" fill="${reg.tint}"/>`;
        h+=`<text x="12" y="${(y+headerH/2+4).toFixed(1)}" font-size="12.5" font-weight="800" fill="${reg.tag}">${reg.label}</text>`;
        h+=`<line x1="${L}" y1="${(y+headerH).toFixed(1)}" x2="${W-R}" y2="${(y+headerH).toFixed(1)}" stroke="${reg.tag}" stroke-width="1" stroke-opacity=".35"/>`;
        y+=headerH;
      } else {
        const c=r.c, y0=y;
        h+=`<rect x="0" y="${y0.toFixed(1)}" width="${W}" height="${rowH}" fill="${band%2?'#fafbff':'#fff'}"/>`;
        band++;
        const vis=visibleEvents(c);
        const mn=vis.filter(e=>e.kind==='model').length;
        h+=`<text x="12" y="${(y0+rowH/2-3).toFixed(1)}" font-size="13.5" font-weight="700" fill="#1f2430">${escapeHtml(c.name)}</text>`;
        h+=`<text x="12" y="${(y0+rowH/2+14).toFixed(1)}" font-size="10.5" fill="#9aa1b1">模型 ${mn} · 产品 ${vis.length-mn}</text>`;
        rowY[c.name]=y0;
        y+=rowH;
      }
    });
    const plotBottom=y;
    // 2) 月网格
    ticks().forEach(ms=>{ const x=xAt(ms);
      if(x<L-0.5||x>W-R+0.5) return;
      h+=`<line x1="${x.toFixed(1)}" y1="${T}" x2="${x.toFixed(1)}" y2="${plotBottom.toFixed(1)}" stroke="#eef0f5"/>`;
      const d=new Date(ms); const lab = tickStep()>=30 ? `${d.getUTCMonth()+1}月` : `${d.getUTCMonth()+1}/${d.getUTCDate()}`;
      h+=`<text x="${x.toFixed(1)}" y="${(H-28).toFixed(1)}" text-anchor="middle" font-size="11" fill="#9aa1b1">${lab}</text>`;
    });
    // 3) 事件标记（方块 / 圆点 / 竖条）
    rows.forEach(r=>{ if(r.type!=="c") return;
      const c=r.c, y0=rowY[c.name], cy=y0+rowH/2; let lastX=-999;
      const mH = markerMode==="dot" ? 0 : (markerMode==="bar" ? Math.round(rowH*0.62) : 22);
      const gap = markerMode==="dot" ? 12 : (markerMode==="bar" ? 8 : 19);
      visibleEvents(c).forEach(e=>{
        let x=xAtDate(e.date);
        if(Math.abs(x-lastX)<gap) x=lastX+gap;
        lastX=x;
        if(x<L-10||x>W-R+10) return;
        const col=kindColor[e.kind];
        const j=JSON.stringify({t:e.title,d:e.date,k:e.kind,s:e.source,f:e.file})
          .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
        if(markerMode==="dot"){
          h+=`<g class="gev" data-j="${j}" style="cursor:pointer">`+
             `<circle cx="${x.toFixed(1)}" cy="${cy.toFixed(1)}" r="5" fill="${col}"/>`+
             `<circle cx="${x.toFixed(1)}" cy="${cy.toFixed(1)}" r="9" fill="transparent"/>`+
             `</g>`;
        } else if(markerMode==="bar"){
          const ry=(cy-mH/2).toFixed(1);
          h+=`<g class="gev" data-j="${j}" style="cursor:pointer">`+
             `<rect x="${(x-1.5).toFixed(1)}" y="${ry}" width="3" height="${mH}" rx="1.5" fill="${col}"/>`+
             `<rect x="${(x-5).toFixed(1)}" y="${(cy-mH/2-4).toFixed(1)}" width="10" height="${mH+8}" rx="4" fill="transparent"/>`+
             `</g>`;
        } else {
          const rx=(x-6.5).toFixed(1), ry=(cy-mH/2).toFixed(1);
          h+=`<g class="gev" data-j="${j}" style="cursor:pointer">`+
             `<rect x="${rx}" y="${ry}" width="13" height="${mH}" rx="4" fill="${col}"/>`+
             `<rect x="${(x-11.5).toFixed(1)}" y="${(cy-mH/2-5).toFixed(1)}" width="23" height="${mH+10}" rx="6" fill="transparent"/>`+
             `</g>`;
        }
      });
    });
    svg.innerHTML=h;
    if(rangeLabel) rangeLabel.textContent=`可见：${fmt(dom0)} – ${fmt(dom1)}`;
    svg.querySelectorAll(".gev").forEach(g=>{
      const j=JSON.parse(g.getAttribute("data-j"));
      g.addEventListener("mouseenter",()=>{
        if(dragging) return;
        tip.innerHTML=`<div style="font-weight:700;margin-bottom:3px">${escapeHtml(j.t)}</div>`+
          `<div style="opacity:.82">${kindText[j.k]} · ${j.d}</div>`+
          `<div style="opacity:.82;margin-top:2px">来源：${escapeHtml(j.s)}</div>`+
          `<div style="margin-top:5px;color:#a5b4fc">点击查看当日日报 →</div>`;
        tip.style.display="block";
      });
      g.addEventListener("mousemove",ev=>{
        if(dragging) return;
        const rect=svg.getBoundingClientRect();
        let tx=ev.clientX-rect.left+14, ty=ev.clientY-rect.top+14;
        const tw=tip.offsetWidth, th=tip.offsetHeight;
        if(tx+tw>rect.width) tx=ev.clientX-rect.left-tw-14;
        if(ty+th>rect.height) ty=ev.clientY-rect.top-th-14;
        tip.style.left=tx+"px"; tip.style.top=ty+"px";
      });
      g.addEventListener("mouseleave",()=>{tip.style.display="none";});
      g.addEventListener("click",()=>{ if(moved) return; window.open(j.f,"_blank","noopener"); });
    });
  }

  // 缩放（滚轮，围绕光标位置）
  svg.addEventListener("wheel",e=>{
    const rect=svg.getBoundingClientRect();
    const vx=(e.clientX-rect.left)/rect.width*W;
    if(vx<L||vx>W-R) return;
    e.preventDefault();
    const anchor=dateAtVx(vx);
    const factor=e.deltaY>0 ? 1.18 : 1/1.18;
    let s=(dom1-dom0)*factor; s=Math.max(MIN_SPAN,Math.min(full1-full0,s));
    const frac=(vx-L)/plotW;
    dom0=anchor-frac*s; dom1=dom0+s;
    render();
  },{passive:false});

  // 拖动（平移时间轴）
  let dragging=false, lastX=0, moved=false;
  svg.addEventListener("mousedown",e=>{ dragging=true; moved=false; lastX=e.clientX; tip.style.display="none"; });
  window.addEventListener("mouseup",()=>{ dragging=false; });
  svg.addEventListener("mousemove",e=>{
    if(!dragging) return;
    const rect=svg.getBoundingClientRect();
    const dxPx=e.clientX-lastX; lastX=e.clientX;
    if(Math.abs(dxPx)>2) moved=true;
    const dxMs=dxPx*(rect.width/W)*msPerPx();
    dom0-=dxMs; dom1-=dxMs; render();
  });
  svg.addEventListener("mouseleave",()=>{ if(!dragging) tip.style.display="none"; });

  // 筛选（模型/产品）
  document.querySelectorAll(".gbtn[data-kind]").forEach(b=>{
    b.addEventListener("click",()=>{ const k=b.getAttribute("data-kind");
      show[k]=!show[k]; b.classList.toggle("active",show[k]); render(); });
  });
  // 标记样式切换（方块 / 圆点 / 细线）
  document.querySelectorAll(".gbtn[data-mode]").forEach(b=>{
    b.addEventListener("click",()=>{
      markerMode=b.getAttribute("data-mode");
      document.querySelectorAll(".gbtn[data-mode]").forEach(x=>x.classList.remove("active"));
      b.classList.add("active"); render();
    });
  });
  // 仅重要产品更新（过滤次要小更新）
  const mb=document.getElementById("ganttMajor");
  if(mb) mb.addEventListener("click",()=>{ majorOnly=!majorOnly; mb.classList.toggle("active",majorOnly); render(); });
  // 重置视图
  const rb=document.getElementById("ganttReset");
  if(rb) rb.addEventListener("click",()=>{ dom0=full0; dom1=full1; render(); });
  render();
})();
</script>
</body></html>
"""

def render_day(day):
    meta = day["meta"]
    data = {"meta": meta, "sections": day["sections"]}
    return (DAY_TPL
        .replace("__DATA__", json.dumps(data, ensure_ascii=False).replace("<","\\u003c").replace(">","\\u003e"))
        .replace("__REPORTDATE__", meta["reportDate"])
        .replace("__REPORTDATEHUMAN__", meta["reportDateHuman"])
        .replace("__WEEKDAY__", meta["weekday"])
        .replace("__SOURCE__", meta["source"])
        .replace("__SOURCEURL__", meta["sourceUrl"])
        .replace("__TOTAL__", str(meta["total"]))
        .replace("__GENERATEDAT__", meta["generatedAt"]))

def compute_gantt(arch, top_n=GANTT_TOP_N):
    """甘特式时间线：取「模型发布/更新」与「产品发布/更新」版块条目，按公司关键词归类，
    记录每条的报道日期(date)。返回 {range:[最早,最晚], companies:[{name,color,region,events}]}，
    其中 companies 已按阵营分块排序（美国在上、中国在下；区域内按事件数降序；每阵营最多 top_n 家）。
    events: {date, kind(model/product), title, source, file, minor}"""
    dates = sorted(arch.keys())
    if not dates:
        return {"range": ["", ""], "companies": []}
    ev = {name: [] for name, _, _, _ in COMPANIES}
    for d in dates:
        rec = arch[d]
        for sec in rec.get("sections", []):
            label = sec.get("label")
            if label == "模型发布/更新":
                kind = "model"
            elif label == "产品发布/更新":
                kind = "product"
            else:
                continue
            for it in sec.get("items", []):
                title = (it.get("title") or "")
                text = (title + " " + (it.get("summary") or "")).lower()
                comp = None
                for name, _, kws, _ in COMPANIES:
                    if any(k in text for k in kws):
                        comp = name
                        break
                if not comp:
                    continue
                ev[comp].append({
                    "date": d,
                    "kind": kind,
                    "title": title.strip(),
                    "source": it.get("source") or "AI HOT",
                    "file": f"ai-daily-{d}.html",
                    "minor": (is_minor_product(title) if kind == "product" else is_minor_model(title)),
                })
    present = [(name, color, region, ev[name])
               for name, color, _, region in COMPANIES if ev[name]]
    # 按阵营分组：美国在上、中国在下；区域内按事件数降序；每阵营最多 top_n 家
    us = sorted([p for p in present if p[2] == "us"], key=lambda x: len(x[3]), reverse=True)
    cn = sorted([p for p in present if p[2] == "cn"], key=lambda x: len(x[3]), reverse=True)
    ordered = (us[:top_n] if top_n else us) + (cn[:top_n] if top_n else cn)
    companies = [{"name": n, "color": c, "region": r,
                  "events": sorted(e, key=lambda x: x["date"])}
                 for n, c, r, e in ordered]
    return {"range": [dates[0], dates[-1]], "companies": companies}

def render_index(days):
    idx_days = []
    for d in days:
        idx_days.append({
            "file": f"ai-daily-{d['meta']['reportDate']}.html",
            "date": d["meta"]["reportDate"],
            "meta": d["meta"],
            "sections": [{"label": s["label"], "count": len(s["items"])} for s in d["sections"]],
            "lead": d.get("lead", ""),
            "isToday": d["meta"]["reportDate"] == today,
        })
    newest = days[0]["meta"]["reportDateHuman"]
    oldest = days[-1]["meta"]["reportDateHuman"]
    arch_like = {d["meta"]["reportDate"]: d for d in days}
    gantt = compute_gantt(arch_like)
    # 按 年→月 分组（倒序：最新年/月在上），每月下列出各日期（倒序）
    by_year = {}
    for d in idx_days:
        y = d["meta"]["reportDate"][:4]
        m = int(d["meta"]["reportDate"][5:7])
        by_year.setdefault(y, {}).setdefault(m, []).append(d)
    groups = []
    for y in sorted(by_year.keys(), reverse=True):
        months = [{"month": m, "days": by_year[y][m]} for m in sorted(by_year[y].keys(), reverse=True)]
        groups.append({"year": y, "months": months})
    return (INDEX_TPL
        .replace("__DAYS__", json.dumps(idx_days, ensure_ascii=False).replace("<","\\u003c").replace(">","\\u003e"))
        .replace("__GROUPS__", json.dumps(groups, ensure_ascii=False).replace("<","\\u003c").replace(">","\\u003e"))
        .replace("__GANTT__", json.dumps(gantt, ensure_ascii=False).replace("<","\\u003c").replace(">","\\u003e"))
        .replace("__RANGE__", f"{oldest} – {newest}")
        .replace("__NDAYS__", str(len(days)))
        .replace("__SOURCE__", "AI HOT")
        .replace("__SOURCEURL__", "https://aihot.virxact.com")
        .replace("__GENERATEDAT__", beijing_now().strftime("%Y年%m月%d日 %H:%M")))

print("[4] 写出全部日报文件（从本地 archive 重渲染，模板变更即时生效）...")
written = 0
for date, rec in arch.items():
    path = os.path.join(OUT_DIR, f"ai-daily-{date}.html")
    with open(path, "w", encoding="utf-8") as f:
        f.write(render_day(rec))
    written += 1
print(f"    写入 {written} 个日报文件")

# 索引页：全部日期倒序
days_sorted = [arch[d] for d in sorted(arch.keys(), reverse=True)]
index_path = os.path.join(OUT_DIR, "index.html")
with open(index_path, "w", encoding="utf-8") as f:
    f.write(render_index(days_sorted))
print(f"    {index_path}（共 {len(days_sorted)} 期）")

# 旧入口 ai-daily.html 重定向到索引页（兼容已有书签）
redirect = ('<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">'
    '<meta http-equiv="refresh" content="0;url=index.html">'
    '<title>AI资讯杂志</title></head><body style="font-family:sans-serif;text-align:center;margin-top:48px">'
    '正在跳转到 <a href="index.html">AI资讯杂志</a>…</body></html>')
with open(os.path.join(OUT_DIR, "ai-daily.html"), "w", encoding="utf-8") as f:
    f.write(redirect)
print(f"    {os.path.join(OUT_DIR, 'ai-daily.html')} (重定向到 index.html)")
print(f"[完成] 累计 {len(arch)} 期日报全部保留 · 本次新增 {new_added} 期 · 索引页 + 重定向入口已更新")
