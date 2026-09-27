#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
小姐姐放映厅 · Pro 本地服务（零依赖，Python 3.7+）

在原版「择优引擎」基础上重构增强：
  1. 托管本目录静态页面（index.html）
  2. /api/resolve  解析随机视频直链 —— 并发多候选 + MP4 头探测码率择优
  3. /api/health   健康检查（前端据此判断服务是否在线）

增强点：
  - 更清晰的模块分层（探测 / 择优 / 服务分离）
  - 坏源（404/非视频）多轮扩容重试，命中率更高
  - 探测缓存 LRU 防膨胀，减少重复网络请求
  - 结构化日志开关（DEBUG=1），便于排查

用法：
  python server.py                 # 默认 127.0.0.1:8899 并自动打开浏览器
  python server.py --port 9000 --no-open

可调环境变量：
  CANDIDATES   并发候选数，默认 3（越大画质越好、解析越慢）
  MIN_KBPS     最低码率门槛，默认 1100（低于此值视为糊，优先淘汰）
  PROBE        是否探测元数据，默认 1（0 关闭探测，退化为旧版随机直取）
  PROBE_CACHE  探测结果缓存秒数，默认 300
  ACCESS_TOKEN 可选口令：设置后 /api/resolve 需带 ?key=（公网部署防白嫖）
  DEBUG        1 开启请求日志
"""
import argparse
import json
import os
import re
import struct
import sys
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

# --------------------------------------------------------------------------
# 基础配置
# --------------------------------------------------------------------------
ROOT = os.path.dirname(os.path.abspath(__file__))
PORT_DEFAULT = 8899
ALLOWED_HOSTS = {"api.yujn.cn"}
ACCESS_KEY = os.environ.get("ACCESS_TOKEN", "")
DEBUG = os.environ.get("DEBUG", "0") == "1"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
# 国内 API 强制直连：绕过系统代理（代理会让请求慢 10 倍甚至失败）
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

# ---------- 画质择优配置 ----------
CANDIDATES = int(os.environ.get("CANDIDATES", "3"))   # 并发候选数
MIN_KBPS = float(os.environ.get("MIN_KBPS", "1100"))  # 最低码率门槛
PROBE_ON = os.environ.get("PROBE", "1") != "0"        # 元数据探测开关
PROBE_TTL = int(os.environ.get("PROBE_CACHE", "300"))  # 探测缓存秒数
PROBE_STEPS = (65536, 262144, 1048576)                # 渐进读取：moov 常在前 64KB 内
PROBE_TIMEOUT = 6

POOL = ThreadPoolExecutor(max_workers=max(4, CANDIDATES * 2), thread_name_prefix="probe")


def log(*a):
    if DEBUG:
        print("[server]", *a, file=sys.stderr, flush=True)


# --------------------------------------------------------------------------
# MP4 元数据探测
# --------------------------------------------------------------------------
class ProbeCache:
    """简单的 LRU 探测缓存：url -> (expire_ts, meta)"""

    def __init__(self, limit=500):
        self._d = {}
        self._limit = limit
        self._lock = threading.Lock()

    def get(self, url):
        now = time.time()
        with self._lock:
            hit = self._d.get(url)
            if hit and hit[0] > now:
                return hit[1]
        return None

    def set(self, url, meta):
        now = time.time()
        with self._lock:
            if len(self._d) >= self._limit:
                # 淘汰最早过期项
                expired = [k for k, v in self._d.items() if v[0] <= now]
                for k in expired[: max(1, self._limit // 4)]:
                    self._d.pop(k, None)
                if len(self._d) >= self._limit:
                    # 仍满则整体清空
                    self._d.clear()
            self._d[url] = (now + PROBE_TTL, meta)


_probe_cache = ProbeCache()


def _range(url, start, end, timeout=PROBE_TIMEOUT):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA, "Range": "bytes=%d-%d" % (start, end)})
    with OPENER.open(req, timeout=timeout) as r:
        return r.headers, r.read()


def file_size(url):
    """仅请求 2 字节拿总长度，比 HEAD 更稳（部分 CDN 不支持 HEAD）"""
    try:
        hd, _ = _range(url, 0, 1)
        cr = hd.get("Content-Range") or ""
        if "/" in cr:
            try:
                return int(cr.split("/")[-1])
            except ValueError:
                pass
        v = hd.get("Content-Length")
        try:
            return int(v) if v else None
        except ValueError:
            return None
    except Exception:
        return None


def _dims(seg):
    """从 moov 段里找第一个视频轨的显示尺寸（tkhd 已含旋转矩阵，可直接判竖横屏）"""
    j = seg.find(b"trak")
    while j >= 0:
        k = seg.find(b"tkhd", j)
        if k < 0 or k > j + 400000 or k < 4:
            break
        ln = struct.unpack(">I", seg[k - 4:k])[0]
        t = seg[k + 4:k + ln]
        if not t:
            break
        ver = t[0]
        w16 = 8 if ver == 1 else 4
        off = 4 + w16 * 4 + w16 + 16 + 36      # version+flags | 时间字段 | 保留+层+音量 | 矩阵
        if off + 8 <= len(t):
            w, h = struct.unpack(">II", t[off:off + 8])
            w, h = w >> 16, h >> 16            # 16.16 定点数
            if w > 100 and h > 100:            # 跳过音轨（0x0）
                return (w, h)
        j = seg.find(b"trak", j + 4)
    return None


def _duration(seg):
    k = seg.find(b"mvhd")
    if k < 0:
        return None
    t = seg[k + 4:]
    try:
        if t[0] == 1:
            ts, du = struct.unpack(">IQ", t[20:32])
        else:
            ts, du = struct.unpack(">II", t[12:20])
        return (du / ts) if ts else None
    except struct.error:
        return None


def probe_meta(url):
    """渐进读取文件头部，解析 moov 得到 分辨率/时长/码率。失败返回 None。"""
    cached = _probe_cache.get(url)
    if cached is not None:
        return cached

    try:
        total = file_size(url)
        if not total:
            raise ValueError("no size")
        data = b""
        for b in PROBE_STEPS:
            end = min(b, total) - 1
            if end <= len(data):
                break
            _, chunk = _range(url, len(data), end)
            data += chunk
            mi = data.find(b"moov")
            if mi >= 0:
                size = struct.unpack(">I", data[mi - 4:mi])[0] if mi >= 4 else 0
                if size and mi + size <= len(data):
                    break
        mi = data.find(b"moov")
        if mi < 0:
            raise ValueError("no moov")
        seg = data[mi:min(len(data), mi + 400000)]
        wh = _dims(seg)
        dur = _duration(seg)
        meta = {
            "w": wh[0] if wh else None,
            "h": wh[1] if wh else None,
            "dur": round(dur, 1) if dur else None,
            "size": total,
            "kbps": int(total * 8 / dur / 1000) if (dur and dur > 0.5) else None,
        }
    except Exception:
        meta = None
    _probe_cache.set(url, meta)
    return meta


def verify_url(u):
    """快速验证视频直链是否存活（拉取前 1KB，判断是否 video）"""
    try:
        req = urllib.request.Request(u, headers={"User-Agent": UA, "Range": "bytes=0-1023"})
        with OPENER.open(req, timeout=4) as r:
            ct = (r.headers.get("Content-Type") or "").lower()
            return r.status in (200, 206) and "video" in ct
    except Exception:
        return False


def score(meta):
    """择优打分：码率为主，分辨率为辅；无元数据的候选取中间分，不至于被全灭"""
    if not meta:
        return 0.0
    kbps = meta.get("kbps")
    if not kbps:
        px = (meta.get("w") or 0) * (meta.get("h") or 0)
        return float(px) / 1000.0 if px else 0.0
    h = meta.get("h") or 0
    bonus = 1.15 if h >= 1080 else (1.0 if h >= 720 else 0.7)
    return kbps * bonus


# --------------------------------------------------------------------------
# 择优解析引擎
# --------------------------------------------------------------------------
def fetch_candidate(ep):
    """解析一个候选：拿直链 → 验活 → 探测元数据。返回 None 表示该直链无效。"""
    u = ep
    if "type=json" not in u:
        u = u + ("&" if "?" in u else "?") + "type=json"
    try:
        req = urllib.request.Request(u, headers={"User-Agent": UA})
        with OPENER.open(req, timeout=6) as r:
            data = json.loads(r.read().decode("utf-8", "ignore"))
        url = (data.get("data") or "").strip()
        if not url.lower().startswith("http"):
            return None
        if not verify_url(url):
            return None
        meta = probe_meta(url) if PROBE_ON else None
        return {"url": url, "meta": meta, "count": data.get("video_count")}
    except Exception:
        return None


def resolve_best(endpoints, n, floor):
    """多候选并发择优。endpoints 为源列表，n 为每轮候选数，floor 为最低码率。
    返回 (dict, None) 或 (None, error_msg)。"""
    results = []
    tried = set()
    all_sources = list(endpoints)
    MAX_ROUNDS = 3

    for _ in range(MAX_ROUNDS):
        pool = [e for e in all_sources if e not in tried] or list(all_sources)
        picked = []
        while len(picked) < n and pool:
            picked.append(pool.pop(0))
        if not picked:
            break
        tried.update(picked)
        if n == 1:
            r = fetch_candidate(picked[0])
            if r:
                results.append(r)
        else:
            try:
                for r in POOL.map(fetch_candidate, picked):
                    if r:
                        results.append(r)
            except Exception:
                pass
        if results:
            break

    if not results:
        return None, "resolve failed"

    scored = sorted(results, key=lambda r: score(r["meta"]), reverse=True)
    passed = [r for r in scored
              if (r["meta"] and r["meta"].get("kbps") and r["meta"]["kbps"] >= floor)]
    best = (passed or scored)[0]
    out = {
        "url": best["url"],
        "count": best.get("count"),
        "verified": True,
        "cands": len(results),
        "meta": best["meta"],
        "picked": "quality" if passed else "fallback",
    }
    return out, None


# --------------------------------------------------------------------------
# 抖音 istero 源（可选）
# --------------------------------------------------------------------------
def try_istero(token):
    """istero 抖音近期视频（需免费 token）。成功返回直链，失败返回 None。"""
    try:
        req = urllib.request.Request(
            "https://api.istero.com/resource/v1/douyin/video/rand",
            headers={"User-Agent": UA, "Authorization": "Bearer " + token})
        with OPENER.open(req, timeout=8) as r:
            d = json.loads(r.read().decode("utf-8", "ignore"))
        if d.get("code") != 200:
            raise ValueError(d.get("message") or "code %s" % d.get("code"))
        u = (d.get("data") or {}).get("video") or ""
        if not u.startswith("http"):
            raise ValueError("no video url")
        return u
    except Exception:
        return None


# --------------------------------------------------------------------------
# HTTP 服务
# --------------------------------------------------------------------------
class Handler(SimpleHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "XJJTheater-Pro/4.0"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=ROOT, **kwargs)

    def log_message(self, fmt, *args):
        if DEBUG:
            log(fmt % args)

    def end_headers(self):
        p = urllib.parse.urlparse(self.path).path
        if not p.startswith("/api/") and (
            p == "/" or p.endswith((".html", ".js", ".css")) or "." not in os.path.basename(p)
        ):
            self.send_header("Cache-Control", "no-cache")
        super().end_headers()

    def send_json(self, obj, code=200):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        try:
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path == "/api/health":
                self.send_json({
                    "ok": True, "service": "xjj-theater-pro", "version": "4.0",
                    "candidates": CANDIDATES, "minKbps": MIN_KBPS, "probe": PROBE_ON,
                })
                return
            if parsed.path == "/api/resolve":
                self.api_resolve(urllib.parse.parse_qs(parsed.query))
                return
            super().do_GET()
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        except Exception as e:
            try:
                self.send_json({"ok": False, "error": str(e)}, 500)
            except Exception:
                self.close_connection = True

    def _parse_int(self, qs, key, default, lo, hi):
        try:
            v = int((qs.get(key) or [default])[0])
            return max(lo, min(hi, v))
        except (ValueError, TypeError):
            return default

    def api_resolve(self, qs):
        if ACCESS_KEY and (qs.get("key") or [""])[0] != ACCESS_KEY:
            self.send_json({"ok": False, "error": "unauthorized"}, 401)
            return

        src = (qs.get("src") or [""])[0]
        token = (qs.get("token") or [""])[0]

        # 抖音源（可选，失败自动回落）
        if src == "douyin" and token:
            u = try_istero(token)
            if u:
                meta = probe_meta(u) if PROBE_ON else None
                self.send_json({"ok": True, "url": u, "src": "douyin", "meta": meta})
                return

        n = self._parse_int(qs, "n", CANDIDATES, 1, 6)
        try:
            floor = float((qs.get("min") or [MIN_KBPS])[0])
        except (ValueError, TypeError):
            floor = MIN_KBPS
        probe = (qs.get("probe") or ["1"])[0] != "0" and PROBE_ON

        eps = (qs.get("u") or [])
        if not eps:
            self.send_json({"ok": False, "error": "missing u"}, 400)
            return
        hosts = {urllib.parse.urlparse(u).netloc.lower() for u in eps}
        if not hosts <= ALLOWED_HOSTS:
            self.send_json({"ok": False, "error": "url not allowed"}, 403)
            return
        if any(urllib.parse.urlparse(u).scheme != "https" for u in eps):
            self.send_json({"ok": False, "error": "https required"}, 403)
            return

        # 探测开关关了就退化为随机直取（不探测元数据）
        if not probe:
            out, err = self._random_resolve(eps)
            if err:
                self.send_json({"ok": False, "error": err}, 502)
                return
            out["picked"] = "random"
            self.send_json({"ok": True, **out})
            return

        out, err = resolve_best(eps, n, floor)
        if err:
            self.send_json({"ok": False, "error": err}, 502)
            return
        self.send_json({"ok": True, **out})

    def _random_resolve(self, eps):
        """探测关闭时的降级：随机取一个可用直链"""
        import random
        random.shuffle(eps)
        for ep in eps[: max(3, len(eps))]:
            r = fetch_candidate(ep)
            if r:
                return {"url": r["url"], "count": r.get("count"), "verified": True}, None
        return None, "resolve failed"


def main():
    global ACCESS_KEY
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1",
                    help="监听地址：本机 127.0.0.1，局域网/容器用 0.0.0.0")
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", PORT_DEFAULT)),
                    help="监听端口（默认 8899；可用环境变量 PORT 覆盖）")
    ap.add_argument("--token", default="", help="可选：接口访问口令（也可用环境变量 ACCESS_TOKEN）")
    ap.add_argument("--no-open", action="store_true")
    args = ap.parse_args()
    if args.token:
        ACCESS_KEY = args.token

    if not os.path.isfile(os.path.join(ROOT, "index.html")):
        print("[错误] 未找到 index.html", file=sys.stderr)
        sys.exit(1)

    url = "http://%s:%d/" % ("127.0.0.1" if args.host == "0.0.0.0" else args.host, args.port)
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print("=" * 56)
    print("  小姐姐放映厅 Pro 已启动  ✨  %s" % url)
    print("  监听 %s:%d%s" % (args.host, args.port, "（含访问口令）" if ACCESS_KEY else ""))
    print("  画质择优：并发 %d 候选 · 最低 %d kbps · 探测 %s"
          % (CANDIDATES, MIN_KBPS, "开" if PROBE_ON else "关"))
    print("  Ctrl+C 停止")
    print("=" * 56)
    if not args.no_open and args.host != "0.0.0.0":
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.server_close()


if __name__ == "__main__":
    main()
