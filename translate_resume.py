#!/usr/bin/env python3
# 续翻脚本：Google 端点限流(429)时自动等待，解除后继续把英文全文翻成中文。
# 用法：python translate_resume.py   （后台运行，Ctrl-C 可中断，重跑可续传）
import json, re, time, urllib.request, urllib.parse, sys
from concurrent.futures import ThreadPoolExecutor, as_completed

ARCH = "archive.json"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
G_URL = "https://translate.googleapis.com/translate_a/single"

def ratio_en(s):
    if not s:
        return 0
    letters = [c for c in s if c.isascii() and c.isalpha()]
    nonsp = len([c for c in s if not c.isspace()])
    return (len(letters) / nonsp) if nonsp else 0

def gtrans_one(text):
    body = urllib.parse.urlencode({"client": "gtx", "sl": "auto", "tl": "zh-CN", "dt": "t", "q": text}).encode()
    req = urllib.request.Request(G_URL, data=body,
                                 headers={"User-Agent": UA, "Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=25) as r:
        data = json.loads(r.read().decode("utf-8"))
    return "".join(seg[0] for seg in data[0] if seg and seg[0])

def gtrans(text):
    if len(text) <= 1800:
        return gtrans_one(text)
    parts = re.split(r'(?<=[.!?])\s+', text)
    out, buf = [], ""
    for s in parts:
        if buf and len(buf) + len(s) >= 1800:
            out.append(gtrans_one(buf))
            buf = ""
        buf = (buf + " " + s) if buf else s
    if buf:
        out.append(gtrans_one(buf))
    return " ".join(out)

IMG_RE = re.compile(r'^\s*!\[[^\]]*\]\([^)]*\)\s*$')
def translate_en_zh(text):
    paras = re.split(r'\n{1,}', text)
    out = []
    for p in paras:
        if not p.strip():
            out.append(p)
            continue
        if IMG_RE.match(p):
            out.append(p)
            continue
        if not re.search(r'[A-Za-z]', p):
            out.append(p)
            continue
        out.append(gtrans(p))
    return "\n".join(out)

def save(arch):
    json.dump(arch, open(ARCH, "w", encoding="utf-8"), ensure_ascii=False)

import subprocess
def git_sync():
    """增量翻译落盘后，自动提交并推送部署（SSH remote）。失败不影响续翻循环。"""
    try:
        cwd = "/Users/xiaosongguo/ai-daily"
        subprocess.run(["git", "add", "-A"], check=True, cwd=cwd)
        if subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=cwd).returncode == 0:
            return  # 无变化
        subprocess.run(["git", "commit", "-q", "-m", "自动续翻：增量更新中文翻译"], check=True, cwd=cwd)
        subprocess.run(["git", "push", "origin", "main"], check=True, cwd=cwd)
        print("[续翻] 已提交并推送部署 ✅", flush=True)
    except Exception as e:
        print(f"[续翻] git 同步失败（忽略，下次重试）: {e}", flush=True)

def healthy():
    try:
        gtrans_one("test")
        return True
    except Exception:
        return False

def worker(it):
    c = it.get("content") or ""
    if len(c) < 120 or ratio_en(c) <= 0.45:
        it["zh"] = True
        return
    new = translate_en_zh(c)
    if new and ratio_en(new) <= 0.45:
        it["content"] = new
        it["zh"] = True
    else:
        it["zh"] = False

def main():
    arch = json.load(open(ARCH, encoding="utf-8"))
    # 修正标记：按实际语言重置
    for d in arch.values():
        for s in d.get("sections", []):
            for it in s.get("items", []):
                c = it.get("content") or ""
                it["zh"] = (ratio_en(c) <= 0.45) if len(c) >= 120 else True

    def collect():
        out = []
        for d in arch.values():
            for s in d.get("sections", []):
                for it in s.get("items", []):
                    if it.get("zh"):
                        continue
                    c = it.get("content") or ""
                    if len(c) < 120 or ratio_en(c) <= 0.45:
                        it["zh"] = True
                        continue
                    out.append(it)
        return out

    while True:
        todos = collect()
        if not todos:
            print("[续翻] 全部完成 ✅", flush=True)
            save(arch)
            git_sync()
            break
        # 等待 Google 解除限流
        while not healthy():
            print(f"[续翻] Google 限流中，等待 60s… 剩余 {len(todos)} 条", flush=True)
            time.sleep(60)
            todos = collect()
            if not todos:
                break
        if not todos:
            print("[续翻] 全部完成 ✅", flush=True)
            save(arch)
            git_sync()
            break
        print(f"[续翻] Google 可用，本轮翻译 {len(todos[:150])} 条（礼貌并发 2）", flush=True)
        batch = todos[:150]
        done = 0
        with ThreadPoolExecutor(max_workers=2) as ex:
            futs = {ex.submit(worker, it): it for it in batch}
            for f in as_completed(futs):
                try:
                    f.result()
                except Exception:
                    pass
                done += 1
                if done % 15 == 0:
                    save(arch)
                    print(f"     {done}/{len(batch)} 已落盘", flush=True)
        save(arch)
        git_sync()
        print(f"[续翻] 本轮完成，剩余 {len(collect())} 条", flush=True)
        time.sleep(3)

if __name__ == "__main__":
    main()
