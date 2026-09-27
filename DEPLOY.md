# 部署指南 · 小姐姐放映厅 Pro

四种部署方式，按场景选择：

| 场景 | 方式 | 画质择优 | 收藏/历史 | 精确下载 |
|---|---|---|---|---|
| 自己电脑玩 | 双击 `启动网站.bat` | ✅ | ✅ | ✅ |
| VPS / NAS | `docker run` | ✅ | ✅ | ✅ |
| 容器编排 | `docker compose` | ✅ | ✅ | ✅ |
| 零成本展示 | GitHub Pages | ❌（直连随机） | ✅（语义降级） | ⚠️（新窗口打开） |

---

## 一、docker run（VPS/NAS 推荐）

### 最简启动

```bash
docker run -d \
  --name girl-pro \
  -p 8899:8899 \
  --restart unless-stopped \
  dll315/beauty-reels-pro:latest
```

### 从源码构建（推荐，不依赖镜像仓库）

```bash
git clone https://github.com/dll315/beauty-reels-pro.git
cd beauty-reels-pro
docker build -t girl-pro .

docker run -d \
  --name girl-pro \
  -p 8899:8899 \
  --restart unless-stopped \
  girl-pro
```

访问 `http://服务器IP:8899`。

### 换端口

```bash
# 方式①：只改宿主机映射（容器内仍 8899）
docker run -d --name girl-pro -p 9000:8899 girl-pro

# 方式②：容器内外一起改
docker run -d --name girl-pro -p 9000:9000 -e PORT=9000 girl-pro
```

### 公网口令保护（强烈建议）

公网暴露一定要设口令，否则接口会被白嫖刷流量：

```bash
docker run -d \
  --name girl-pro \
  -p 8899:8899 \
  -e ACCESS_TOKEN=你的复杂口令 \
  --restart unless-stopped \
  girl-pro
```

设了之后，浏览器打开页面控制台执行一次：

```js
localStorage.setItem("xjjpro_key", "你的复杂口令")
```

### 画质调优环境变量

```bash
docker run -d \
  --name girl-pro \
  -p 8899:8899 \
  -e CANDIDATES=5 \      # 并发候选数（默认 3，越大越清晰越慢）
  -e MIN_KBPS=1500 \     # 最低码率门槛（默认 1100）
  -e PROBE=1 \           # 码率探测开关（默认开；0=退化为纯随机）
  -e PROBE_CACHE=600 \   # 探测结果缓存秒数（默认 300）
  --restart unless-stopped \
  girl-pro
```

### 更新到最新版

```bash
cd beauty-reels-pro
git pull
docker build -t girl-pro .
docker rm -f girl-pro
docker run -d --name girl-pro -p 8899:8899 --restart unless-stopped girl-pro
```

### 查看日志 / 停止

```bash
docker logs -f girl-pro
docker stop girl-pro && docker rm girl-pro
```

---

## 二、docker compose

仓库自带 `docker-compose.yml`：

```bash
git clone https://github.com/dll315/beauty-reels-pro.git
cd beauty-reels-pro
docker compose up -d --build     # 访问 http://IP:8899

# 更新
git pull && docker compose up -d --build

# 换端口
PORT=9000 docker compose up -d --build
```

---

## 三、GitHub Pages（零成本展示，直连模式）

仓库已开启 Pages：**https://dailong.me/beauty-reels-pro/**
（等价默认地址：https://dll315.github.io/beauty-reels-pro/）

### 原理

Pages 是纯静态托管，跑不了 `server.py`。前端检测不到本地服务时自动进入**直连模式**：

- yujn API 不带 `type=json` 时 **302 直跳视频**，`<video>` 标签跟随重定向播放，天然绕开 CORS
- 源 API 每次随机返回，内容天然不重复

### 直连模式的能力边界

| 能力 | 状态 | 说明 |
|---|---|---|
| 随机播放 / 切换 / 回看 | ✅ | 完整保留 |
| 十源加权轮换 | ✅ | 保留 |
| 分辨率 / 时长显示 | ✅ | 来自 video 元数据 |
| 码率择优 / 码率显示 | ❌ | 浏览器拿不到 302 后的直链，无法探测 |
| 收藏 / 历史 | ⚠️ | 记录的是源 API 地址，回放 =「该源再来一条」，不是同一条视频 |
| 下载 | ⚠️ | 新窗口打开视频（浏览器跟随 302），可右键另存 |
| 账号系统 | ✅ | localStorage 照常 |

> 自己仓库复刻：fork 后在 Settings → Pages → Source 选 `main` 分支根目录即可。

---

## 四、本地 Windows 常驻

```bash
# 方式①：双击 启动网站.bat
# 方式②：命令行
python server.py                 # 127.0.0.1:8899 自动开浏览器
python server.py --port 9000     # 换端口
python server.py --host 0.0.0.0  # 局域网可访问（手机也能看）
```

---

## 环境变量速查

| 变量 | 默认 | 说明 |
|---|---|---|
| `PORT` | 8899 | 监听端口 |
| `ACCESS_TOKEN` | 空 | 接口口令（公网必设） |
| `CANDIDATES` | 3 | 并发候选数（1-6） |
| `MIN_KBPS` | 1100 | 最低码率门槛 |
| `PROBE` | 1 | 码率探测开关 |
| `PROBE_CACHE` | 300 | 探测缓存秒数 |
| `DEBUG` | 0 | 1 = 请求日志 |
