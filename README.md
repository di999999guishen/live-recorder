# LiveRecorder

> 直播开播值守 → 自动录制 → 语音转写 → 文稿归档 → 空间回收。抖音 / B站双平台。

一个「挂着不用管」的直播归档工具：按你自己的时间窗值守，主播一开播就自动录，
断流自动续录，下播自动停录；随后把音频转成带时间轴的文字稿，再按需清理原始媒体。
适合长期跟踪固定主播、想留文字资料又不想留几百 GB 视频的场景。

默认 **audio 模式**（只留音轨）：抖音实测约 **51 MB/小时**，够转写和做笔记，
比 video 模式（约 0.9 GB/小时）省 18 倍空间。

与姊妹项目 [up-notify](https://github.com/di999999guishen/up-notify) 的分工：

| 项目 | 盯什么 | 产物 |
|---|---|---|
| **live-recorder**（本项目） | 主播的**直播内容** | 录音 + 转写 + 总结 md |
| up-notify | UP 主的**新作品** | 通知（邮件 / outbox 队列） |

两者共用 `notifier` 的双后端设计（SMTP 直发 / outbox 队列解耦），可以接到同一个发信环节上。

---

## 能力

| 模块 | 说明 |
|---|---|
| 开播检测 | 抖音免 `a_bogus` 签名；B站免 wbi 签名，实测可用 |
| 流地址解析 | 双平台统一 `RoomState`，上层无需区分 |
| ffmpeg 录制 | audio / video 双模式，断流自动续录，分片续写不覆盖 |
| 常驻值守 | `scheduler.py`：多主播、开播起录、断流重连、下播停录、单实例锁 |
| 封片归档 | 完整性校验 + `manifest.json` + `index.md` |
| 语音转写 | faster-whisper medium + CUDA，实测 **约 12x 实时** |
| 时间轴稿 | 同一音频落两份：`audio.txt`（纯文本）+ `audio.timed.txt`（`[HH:MM:SS] 文本`） |
| 收尾兜底 | `catchup.py`：进程被强杀导致的缺稿自动补齐，幂等可重跑 |
| 出稿成稿 | `digest.py`：`*.timed.txt` → 结构化笔记 md，**时间点强制回校**，编造的会被剥掉 |
| 空间回收 | 三重闸门判定「稿子已出」后清理原始媒体，可走回收站 |
| 通知 | SMTP 直发 / outbox 队列（解耦给任何外部发信环节） |
| LLM 总结 | 任意 OpenAI 兼容接口，map-reduce 分段摘要（可选） |

---

## 快速开始

```bash
git clone <your-repo-url> live-recorder
cd live-recorder

python -m venv venv
venv/Scripts/pip install -r requirements.txt        # Windows
# source venv/bin/activate && pip install -r requirements.txt   # macOS / Linux

cp .env.example .env
cp anchors.example.json anchors.json                # 填入自己的房间号
```

系统依赖：**ffmpeg / ffprobe 必须在 PATH 中**（录制与封片都靠它）。

先验一下能不能探测到开播状态：

```bash
venv/Scripts/python scheduler.py --check            # 按 anchors.json 逐个探测
venv/Scripts/python watch.py <直播链接> --once       # 单独看一个房间的详情
```

手动录一段 60 秒冒烟：

```bash
venv/Scripts/python record.py <链接或房间号> --mode audio --duration 60
```

确认无误后，让它开始值守：

```bash
venv/Scripts/python scheduler.py                    # 常驻，默认 300 秒轮询
```

---

## 命令速查

```bash
# --- 值守 ---
python scheduler.py                                  # 读 anchors.json 常驻值守
python scheduler.py --check                          # 只探测一轮后退出
python scheduler.py --interval 600                   # 轮询间隔（秒）
python scheduler.py --run-for 21600                  # 最多跑 6 小时自动收尾
python scheduler.py --run-for 21600 --exit-after-sessions 1
                                                     # 录满 1 场即退，--run-for 只作硬兜底
python scheduler.py --room 123456789012              # 临时只盯一个房间

# --- 单次探测 / 手动录制 ---
python watch.py <链接> --once                        # 探测详情（含可用画质与拉流地址）
python record.py <链接> --mode audio --duration 60   # 冒烟测试
python record.py <链接> --mode video --quality HD1

# --- 收尾兜底（幂等，可重复跑）---
python catchup.py                                    # 今天
python catchup.py --date 2026-10-09
python catchup.py --since-days 3                     # 最近 3 天
python catchup.py --dry-run                          # 只列清单，不动任何文件

# --- 出稿成稿（幂等，可重复跑）---
python digest.py                                     # 今天，有稿但没出稿的场次
python digest.py --date 2026-10-09
python digest.py --since-days 3
python digest.py --dry-run                           # 只列清单，不调模型、不写文件
python digest.py --no-llm                            # 不调模型，只出「元信息 + 时间轴」骨架
python digest.py --force                             # 忽略幂等，重出稿（默认复用分段提取缓存）
python digest.py --force --no-map-cache              # 连分段提取也重做（不动 digest_map.json）
python digest.py --anchors anchors.json               # 用 alias 作笔记里的主播名
python digest.py --notes-dir D:\直播笔记             # 覆盖笔记输出目录

# --- 空间回收 ---
python catchup.py --purge-dry-run                    # 只列出会删哪些
python catchup.py --purge --purge-trash              # 真删，走回收站（可恢复）
python catchup.py --purge                            # 真删，直接 unlink（立刻腾空间）
python catchup.py --purge --notes-dir /path/notes    # 指定出稿 md 所在目录

# --- 封片 / 通知 ---
python record/finalizer.py data/douyin_123456789012/2026-10-06/20261006_082202
python notifier.py list                              # 查看待发送通知
```

---

## 架构：四段式

值守和录制必须交给**操作系统级定时器**（Windows 计划任务 / cron / systemd timer）。
不要塞进任何有会话时限的调度器：一场直播常 2-5 小时，而多数托管式定时任务的
单次执行上限只有几分钟，后台子进程还会随会话结束被回收。

| 段 | 承载 | 干什么 |
|---|---|---|
| ① 值守 | 计划任务 → `scheduler.py` | 每 N 秒探测开播，开播即录到下播；`--run-for` 兜底强制收尾 |
| ② 收尾 | 计划任务 → `catchup.py` | 补齐被强杀场次的转写与封片（幂等） |
| ③ 出稿 | 计划任务 → `digest.py` | 读 `*.txt` + `*.timed.txt`，调 LLM 归纳 → 笔记 md |
| ④ 清理 | 计划任务 → `catchup.py --purge` | 笔记已落地则清理原始媒体，只留几 KB 的转写稿 |

③ 段**独立成脚本、不塞进值守进程**：一场录制跑几小时，不该因为一次 LLM 接口 429
就整个趴下。出稿是「随时可补、失败了重跑即可」的环节，所以按需调用、幂等可重跑。
不想用 LLM 也没关系 —— 本仓库只保证把稿子**可靠地生产出来并带上时间基准**
（`*.timed.txt`），你完全可以换成自己的出稿环节。

### 注册计划任务

`register_tasks.ps1` 会按上面的四段结构注册 5 条 Windows 计划任务
（`LR-Watch-AM/PM`、`LR-Catchup-AM/PM`、`LR-Purge`）：

```powershell
# 管理员权限 PowerShell
powershell -ExecutionPolicy Bypass -File .\register_tasks.ps1
```

Linux / macOS 等价的 crontab：

```cron
0  6 * * *  cd /path/to/live-recorder && venv/bin/python scheduler.py --interval 600 --run-for 21600
0 13 * * *  cd /path/to/live-recorder && venv/bin/python scheduler.py --interval 600 --run-for 18000
15 12 * * * cd /path/to/live-recorder && venv/bin/python catchup.py --since-days 2
15 18 * * * cd /path/to/live-recorder && venv/bin/python catchup.py --since-days 2
0  21 * * * cd /path/to/live-recorder && venv/bin/python catchup.py --since-days 2 --purge --purge-trash
```

**值守段务必保持 `auto_transcribe: false`**：转写约 1/12 实时，一场 2 小时的直播要
转约 10 分钟，这期间轮询停摆，会漏掉紧接着开播的下一场。转写统一交给 ② 段补。

---

## 空间回收：三重闸门

**为什么敢删**：转写稿（`audio.txt` 纯文本 + `audio.timed.txt` 时间轴）是笔记的唯一
来源，两者都是几 KB 的纯文本，**永久保留**。原始音频存在的意义只有「还没转写」，
转写完并出了稿就是纯占位：audio 模式约 72 kbps，一场 2 小时约 65 MB，
一天两场、一年下来 45 GB 级别。

删错了不可逆，所以设三道闸门，**任一条不满足就跳过**：

| 闸 | 判据 | 防什么 |
|---|---|---|
| 1 | 每个媒体都有非空 `.txt`，且 `.txt` 不比媒体旧 | 防删掉「还没转写」或「只转了半截稿」的场次 |
| 2 | 媒体 mtime 距今 > `LIVE_GRACE`（120s） | 防删掉正在录制的场次 |
| 3 | 笔记目录里存在该日期、且 mtime 晚于最新 `.txt` 的 md | 防「稿子还没出就把素材删了」 |

闸 3 **刻意不解析 md 的 frontmatter**，只用「文件名含日期」这条最粗的线索
（`2026-10-09` 与 `20261009` 两种写法都命中）—— 出稿格式会变，日期不会。
匹配不到就返回空 → 不删（安全侧失败）。

`LIVE_GRACE` 被转写段和清理段**共用同一个常量**，避免两处判据漂移。
每次清理追加一行到 `logs/purge.log`（`方式` 列区分 `trash` / `unlink`）。

### 回收站模式

`--purge-trash` 走 `SHFileOperationW`（`FO_DELETE` + `FOF_ALLOWUNDO`，纯 ctypes，
无第三方依赖）。注意：

- **回收站不释放磁盘空间**，它只是「删错了还能捞回来」的缓冲。想真正腾空间要
  定期清空回收站，或改用 `--purge` 直接 unlink。
- 若系统被设成「不移动到回收站、直接删除」（`NukeOnDelete=1`），`FOF_ALLOWUNDO`
  会被**静默忽略**、退化成永久删除。介意的话先确认该注册表项为 0。

---

## 主播配置 anchors.json

```json
{
  "anchors": [
    {
      "platform": "douyin",
      "room": "123456789012",
      "mode": "audio",
      "quality": "HD1",
      "enabled": true,
      "max_seconds": 0,
      "auto_transcribe": false
    },
    {
      "platform": "bilibili",
      "room": "1234567",
      "qn": 150,
      "mode": "audio",
      "enabled": false
    }
  ]
}
```

| 字段 | 说明 |
|---|---|
| `platform` | `douyin` / `bilibili` |
| `room` | 抖音填 `web_rid`；B站填房间号（短号也行，会自动映射真实 room_id） |
| `mode` | `audio`（约 51 MB/h）/ `video`（约 0.9 GB/h） |
| `quality` | 抖音画质，**默认 `HD1`(高清)**：`ORIGION` 原画 / `FULL_HD1` 蓝光 / `SD1` 标清 / `SD2` 流畅 |
| `qn` | B站画质，**默认 `150`(高清)**：`10000` 原画 / `400` 蓝光 / `250` 超清 / `80` 流畅 |
| `enabled` | `false` 则值守时跳过（但 `--check` 仍会探测，见坑位 17） |
| `alias` | 可选。**笔记里显示的主播名**，优先级 `alias > nickname > meta.json`。平台昵称常带 emoji、会改名、会撞名，出稿文件名用它就永远在变；填个稳定的 `alias` 既能固定笔记名，也能避开与历史笔记重名 |
| `max_seconds` | 单场最长录制秒数，`0` 不限 |
| `auto_transcribe` | 值守段内联转写。**建议保持 `false`** |

房间号怎么取：

- 抖音：打开直播间，链接形如 `https://live.douyin.com/<rid>`；推荐页形态则取 URL
  里的 `live_web_rid=` 参数（`parse_web_rid()` 优先匹配它，能直接从整条 URL 提取）。
- B站：`https://live.bilibili.com/<房间号>`。

---

## 产物结构

```
data/<platform>_<room>/<YYYY-MM-DD>/<YYYYMMDD_HHMMSS>/
    audio.aac            原始音轨（audio 模式；断流重连才产生 audio_002.aac）
    audio.txt            纯文本转写稿
    audio.timed.txt      时间轴稿，每行 [HH:MM:SS] 文本
    meta.json            主播/标题/起止时间/时长/异常原因
    manifest.json        分片校验结果 + 真实时长 + 转写清单
    index.md             本场文件索引（便于归档）
    ffmpeg.log           ffmpeg 原始日志（排障用）
```

`audio.timed.txt` 的时间基准是**该分片自身起点**。单分片场次（常态）即等价于
「相对开播时间」。`audio` 模式整场只有一个文件，`segment` 分片只对 video 模式生效。

---

## 双平台技术要点

### 抖音
- `GET https://live.douyin.com/webcast/room/web/enter/?web_rid=xxx`
- **免 `a_bogus` 签名**：首页临时 Cookie + 桌面 UA 即可通过（`core/signing.py`
  里保留了纯 Python 的 a_bogus 实现作为 `--sign` 备用路径）
- room 对象在 **`data.data[0]`**，不是 `data.room`（响应结构换过多次，
  `_locate_room()` 按已知三种形态依次尝试）
- `status == 2` 直播中，`4` 未开播

### B站
- `room/v1/Room/get_info` → 真实 `room_id` 与 `live_status`（**1** 直播中 / 0 未开播 / 2 轮播）
- `xlive/web-room/v2/index/getRoomPlayInfo` → `playurl_info`，**实测免 wbi 签名**
- **短号 ≠ 真实 room_id**，必须先用 `get_info` 映射
- 流地址 = `url_info[0].host` + `codec.base_url` + `url_info[0].extra` 三段拼接。
  **codec 里没有 `url` 字段，只有 `base_url`**，拼错会得到 5XX
- 拉流必须带 `Referer: https://live.bilibili.com/`

---

## 实测数据

| 平台 | 模式 | 码率 | 每小时体积 |
|---|---|---|---|
| 抖音 | audio HD1 | 118 kbps | **51 MB** |
| 抖音 | video HD1 720×1270 | 2087 kbps | **0.90 GB** |
| B站 | audio | 约 130 kbps | 约 58 MB |

转写：`medium` + CUDA，60 秒音频耗时 5.3 秒（**11.6~12.1x 实时**），
2 小时直播约 10 分钟转完。

---

## 坑位清单（都踩过，别重踩）

1. **ffmpeg 攒够 256 KB 才落盘** —— 111 kbps 音频前 12 秒文件是 0 字节。
   必须加 `-flush_packets 1`；心跳判据要用「ffmpeg 日志 `time=` 进度 **或** 文件
   大小任一增长」，只看文件大小会误判断流。

2. **输出路径必须缓存** —— `_out_pattern()` 每次重算会因文件已存在而跳到
   `audio_002.aac`（尚不存在），导致心跳恒读到 0 字节。

3. **CUDA DLL 要在 Python 内自举** —— Git Bash 里 `export PATH` 是 POSIX 路径，
   传不到 Windows DLL 加载器。必须在 `import faster_whisper` **之前**调
   `os.add_dll_directory()`。否则报 `cublas64_12.dll is not found`。

4. **模型建议离线加载**（`HF_HUB_OFFLINE=1`）—— 走代理联网容易 502。

5. **裸 `.aac` 的 ffprobe 时长有 3~12% 误差** —— 精确时长看 `meta.json` 的墙钟差，
   `finalizer` 同时记了 `total_duration` 与 `wall_duration` 两者。

6. **别用 mp4 直录直播** —— 非正常关闭会整体报废；aac / flv 裸流抗中断。

7. **`--run-for` 需要看门狗线程** —— 主线程阻塞在录制会话里，到点没人设停止信号
   会一直录下去。

8. **轮询间隔别低于 300 秒** —— 抖音/B站对高频请求敏感，有封 IP 风险
   （实测 60 秒偏激进）。

9. **Whisper 中文默认输出繁体** —— 已在 `transcriber` 输出层用 `zhconv` 统一转简体；
   `summarizer.punctuate` 的 prompt 里也要求繁转简，否则各段要点里会漏繁体字。

10. **B站拿不到主播名** —— `room/v1/Room/get_info` 顶层无 `uname`（`anchor_info` 为
    null），`card` / `acc/info` 需 wbi 签名（返回 -400/-403），`getInfoByRoom` 返回
    -352 风控。结论是免费接口拿主播名成本过高，拿不到时用「平台+房间号」兜底。
    抖音无此问题（enter 接口直接返回 `owner.nickname`）。

11. **meta 必须带 `platform` 字段** —— `recorder._write_meta` 从 `watcher.PLATFORM`
    取平台写进 `meta.json`，否则下游笔记头部的「平台/房间」一行空白。

12. **多时段任务必须上单实例锁** —— 时段重叠（前一场录到下播、后一场已到点）会让
    两个进程同时盯同一 room，同一场被录两遍、转写两遍、出两份稿。现用
    `.lock/scheduler.lock` + PID 存活检查，被强杀留下的残留锁自动接管；
    调试用 `--allow-multi` 跳过。**被锁拦下时退出码为 3**（区别于「没有启用中的
    主播」的 1）。

13. **收尾兜底要单独成脚本，不能只靠 scheduler 自己** —— `_transcribe()` 是
    「全部转完才写文件」，进程被外部强杀（计划任务执行时限 / 手动结束 / 断电）
    就一个字都不留，`.txt` 根本不存在。`catchup.py` 按「产物是否齐全」补齐，
    幂等可重跑，应跟在值守任务之后跑一次。

14. **`--run-for` 到期时正在录的那一场不会丢转写**：看门狗置 `stop_event` →
    录制段返回 → 回到 `run()` 后仍会执行 `notify` + `_transcribe`。
    真正的风险只在「外部强杀」，故第 13 条的兜底仍然必要。

15. **绝不借用别的会话/临时目录里的 venv** —— 那种目录会被清理，一旦没了，
    转写环节（faster-whisper + CUDA 轮子）会在后台**静默失效**。环境必须随项目走，
    `transcriber.find_site_packages()` 的探测顺序是：`DLR_SITE_PACKAGES` 环境变量
    → 当前解释器 → 项目内 `venv/`、`.venv/`。

16. **计划任务必须以登录用户身份跑** —— Whisper 模型缓存在 `~/.cache/huggingface`；
    按 SYSTEM 账户执行时 `~` 会解析到
    `C:\Windows\system32\config\systemprofile`，模型找不到。同时机器需保持开机，
    休眠由 `WakeToRun` 尝试唤醒。

17. **`scheduler.py --check` 会绕过 `enabled` 过滤** —— `--check` 分支对所有 cfg
    都跑一遍探测，不检查 `enabled`；真实运行才走 `[c for c in cfgs if c.enabled]`。
    所以房间号没填时 `--check` 报「无法提取 web_rid」，而正式任务直接以
    「没有启用中的主播」退出码 1。两者不是同一套过滤，别混淆。

18. **带中文的 `.ps1` 必须存成 UTF-8 带 BOM** —— PowerShell 5.1 读无 BOM 脚本会按
    系统 ANSI 码页（中文环境是 GBK）解码，UTF-8 中文字节被错切后**会把相邻的
    ASCII 字符（含续行反引号、引号）当成双字节字符的一部分吞掉**，于是续行断掉，
    报出像 `-Arguments : 无法将"-Arguments"项识别为 cmdlet` 这种完全对不上位置
    行号的错。

19. **`catchup.py --since-days N` 的日期集合是 `{今天 - i | i < N}`，N=1 就是
    「只有今天」** —— 不是「今天和昨天」。若某次收尾段漏跑（关机、转写被杀），
    那天的稿子**永远没人补**。收尾任务请用 `--since-days 2` 以上，多扫一天实现
    次日自愈。

20. **收尾段与值守段重叠时，「转写稿存在」不等于「转写完整」** —— audio 模式下
    整场是**一个** `audio.aac`，直播还在进行时它一直在增长。此刻转写只能拿到
    **前缀**；若判据只看 `.txt` 是否存在，这份半截稿会被当成终稿永久固化，
    后半场内容静默丢失。现按 **mtime** 判定（音频比 `.txt` 新 → 需重转），
    并加 `LIVE_GRACE`（120s）「仍在写入」宽限直接跳过，避免白烧 GPU。

21. **纯文本转写稿不含任何时间信息** —— `transcribe()` 返回的 `segments` 带有
    `start/end`，但只落 `res['text']` 的话时间信息就丢了。现在同一音频落**两份**：
    `audio.txt`（纯文本）+ `audio.timed.txt`（每行 `[HH:MM:SS] 文本`）。
    下游若要「按时间点做索引」，只能引用 `timed.txt`，否则只能编 —— 纯文本
    喂给出稿模型，它会为了填满时间轴而凭空生成时间戳。

22. **改计划任务的参数不影响已启动的实例** —— Task Scheduler 在启动瞬间就把 argv
    交给进程。所以「临时改 `--run-for` → 启动 → 立刻恢复原值」是安全的，
    不需要额外的一次性任务定义。核验：
    `(Get-ScheduledTask -TaskName X).Actions[0].Arguments` 读回。

23. **正则去匹配「整份时间轴稿」时必须带 `re.MULTILINE`** —— `^\[(\d{1,3}:\d{2}:\d{2})\]`
    这种按行锚定的模式，拿去 `finditer` 一大段多行文本时，**没有 `re.M` 的 `^$`
    只锚整串的开头与结尾**，结果匹配数恒为 0。此时的表征很有迷惑性：程序不报错，
    只是「一个合法时间点都没找到」，于是出稿段**把模型写对的时间点也当成编造全剥掉**，
    交出一份没有任何时间的笔记 —— 看起来像模型不听话，实则是自己的正则失灵。
    更阴的是同一份正则拿去 `re.sub` 单行（如 frontmatter）时又是对的，
    让人在别处反复排查。

24. **喂给出稿模型的「合法时间点清单」不能截断** —— 模型只被允许引用清单里存在的
    时间点，校验段据此判真假。若为省 token 把清单砍一刀（比如只给前 200 个），
    **后半场的真实时间点会被判成编造，连同正确的观点一起被剥掉**，
    而错在前 200 个之外、肉眼极难发现。现做法：全量喂，超过 1500 个才抽样，
    且抽样是「均匀抽」而非「取前 1500」。

25. **不显式设 `max_tokens`，长输出会被服务端默认值静默截断** —— `glm-4-flash`
    默认输出上限只有 **1024 token**，出稿这类需要长输出的场景会「写到一半就没了」，
    而且**不报错**。另：map 段的提示词若限制「控制在 400 字内」，6000 字原文被压成
    400 字（15:1），下游 reduce 再也补不回来 —— **出稿质量差往往是提示词逼着模型
    少写，不一定是模型不行**。现 `call_llm` 有 `max_tokens` 可选参数，digest 的
    map 与 reduce **都给 4096**（map 原为 2048，实测单分片输出就 2049 字、紧贴上限，
    有被静默截断之嫌）。

26. **智谱网关对「非流式」`/chat/completions` 有一条约 190 秒的墙钟上限，长输出必须走流式**
    —— 这条曾把出稿的 reduce 段整段打挂。表征是 `HTTP 500
    {"error":{"code":500,"message":"操作失败"}}`，看起来像**偶发**，其实是
    **确定性**的：同一载荷连跑 3 次，3 次分别在 `190.4 / 190.6 / 190.6s` 准时失败。
    `glm-4-flash` 实测约 18 token/s，出一份 ~2800 token 的笔记要 ~155s，离上限极近，
    服务端一忙（生成 >190s）就被掐断 —— 回吐的是 **500 而非规范的 504**，
    因此极易误判成「服务端抖动」。排除法做过：改输入长度、改内容、改 `max_tokens`
    都不影响，唯独**总生成时长**决定生死。
    解法：`call_llm(..., stream=True)` 走 SSE（`call_llm_stream`），连接持续有 token
    流动就不再受该墙钟约束 —— 同一载荷流式 180.8s 完整拿到 4536 字，非流式同载荷必然 500。
    配套三点：① 出稿的两处长输出调用（分段提取、汇总成稿）**一律开流式**；
    ② 汇总段保留**五级递降**（全量 → 缺节补齐 → 时间点清单瘦身 → 按材料分组成稿 →
    要点拼接兜底），**判据是「六小节是否齐全」而非「有没有报错」**（见第 27 条），
    兜底稿仍是一份合法 md，清理段的「已出稿」闸门因此照常通过，不会因为一次汇总失败
    把原始媒体永远堆在盘上。    ③ 分段提取结果缓存到 `<场次目录>/digest_map.json`
    （指纹 = 转写稿名字/大小/mtime + 块数 + 模型 + 提示词哈希）—— map 是整条链最慢的一段（1 小时直播 >3 分钟），
    不该被后续任一环节的失败连累重做；改提示词/换模型会让旧缓存**自动失效**，不会被静默复用。
    `--force` 也只重出稿、复用缓存，要重做 map 才加 `--no-map-cache`。

27. **`finish_reason` 不能用来判断稿子是否完整；出稿提示词必须给「条数上限」**
    —— 这条是「稿子写到一半就没了、却毫无报错」的真凶。实测两种截断的回报**完全不同**：
    网关**撞满 `max_tokens`** 时如实回报 `finish_reason="length"`（`max_tokens=2000`
    → 产物 2927 字符）；但**模型自己写一半停住**时回报的是 `stop`（`max_tokens=4096`
    → 产物 4131 字符、收到 `[DONE]`、**0 个异常 SSE 帧**，却只写了「一、本场概览 /
    二、核心观点」两节，正文断在 `[00:16:30]`，而转写稿实际到 `[00:20:56]`）。
    也就是说两种截断里**只有一种**能被 `finish_reason` 抓到，靠它判断完整性必然漏判。
    真因是提示词放任模型「逐句抄转写稿」：一场 21 分钟的直播有 600+ 个合法时间点，
    第二节被写成 181 条逐字复述，把输出额度吃光，后面的小节**整节消失**且不触发任何降级。
    修法三条：① 判据改成**结构性的** —— `missing_sections()` 校验六小节是否齐全；
    ② 缺节时 `reduce_missing()` **只补缺的那几节**再拼回（比整篇重跑便宜，也不会把
    已写好的节重写一遍）；③ 提示词给**条数上限**并明令禁止逐句抄稿（观点 8–20 条、
    关键信息表格 20 行内、时间点索引 15–40 条）——「有多少条列多少条」在 600+ 个
    时间点下结构上不可能塞进单次输出额度。
    另：`call_llm_stream` 原先把 `json.loads(line)['choices'][0]` 套在 `except: continue`
    里，遇到网关的 `{"error":{...}}` 帧会 KeyError 被**静默吞掉**（同样是「写一半没了」
    却不报错），现已改为显式解析、错误帧必抛。

28. **出稿模型不能选 Flash 系列 —— 小模型不遵守「按话题归纳」约束（第 27 条的真根因）**
    —— 同一份 `digest_map.json`、同一份提示词，只换 reduce 的模型，实测对照：

    | 模型 | ¥/百万token | 耗时 | 六节齐全 | 「二、核心观点」 |
    |---|---|---|---|---|
    | `glm-4-flash`（原用） | 0（免费） | 101–118s | 靠 `reduce_missing` 兜底 | **238 条逐句搬运** |
    | `glm-4-flashx` | 0.1 | 21.5s | 只写 2 节（缺 4 节） | **278 条抄稿 + 重复行** |
    | **`glm-4-air`** | **0.5** | 24.9s | ✅ 一次成型 | **18 条（合规）** |
    | `glm-4-plus` | 5 | 25.7s | ✅ 一次成型 | 16 条（合规） |

    提示词里「按话题归纳 / 8–20 条 / 不要逐句照抄转写稿」写得很硬，Flash 系列照样
    违反 —— **这是模型能力问题，不是提示词问题**。第 27 条的「缺节」能靠
    `reduce_missing` 兜住，但「抄稿」这个根因兜不住（补齐后第五节变成 198 条逐句搬运）。
    **定论：Air 起步**。换 `glm-4-air` 后端到端 1 分钟出稿（原用 flash 需 ~12 分钟 +
    兜底补齐），六节齐全、无编造剔除；且「关键信息」表的**依据列从全空变成真实原话**、
    第六节从误报「本场未给出统一风险提示」变成真实风险归纳（flash 漏报）。
    成本量级：一场 20 分钟约 ¥0.016，2 小时约 ¥0.1，每天两场约 ¥6/月。
    改法：`.env` 里 `SUMMARY_MODEL=glm-4-air`。
    - ⚠ 换模型会让 `digest_map.json` 指纹失效（指纹含模型名）→ 下一场必然重跑 map，属预期。
    - ⚠ `glm-4-air` 偶有把 `[00:02:02]` 写成 `[02:02:02]` 的笔误，由 `verify_times()`
      剔除（文字保留），属可接受损耗。

---

## 目录结构

```
core/        config.py（UA / 公共参数）    signing.py（a_bogus 备用签名）
monitor/     watcher.py（抖音）            bilibili.py（B站）
record/      recorder.py  finalizer.py  probe_stream.py
pipeline/    transcriber.py（CUDA 自举 + 转写 + SRT）  summarizer.py（OpenAI 兼容）
scheduler.py   常驻值守（单实例锁 / --exit-after-sessions / --run-for）
catchup.py     收尾兜底（补转写 + 封片 + 清理，幂等可重跑）
digest.py      出稿成稿（*.timed.txt → 笔记 md，时间点回校，幂等可重跑）
register_tasks.ps1  注册 Windows 计划任务
notifier.py    通知（SMTP / outbox）
prompts/digest.md    出稿提示词（可自行替换）
anchors.example.json 配置样例
```

---

## 已知限制

- **覆盖时段由你自己的计划任务决定**。默认只在注册脚本里写了 06:00–12:00 与
  13:00–18:00 两段；晚间开播的主播不会被录到 —— 追加一条 Watch 任务即可，
  模板就在 `register_tasks.ps1` 里。
- **接口随时可能变**。抖音风控策略变化频繁，`monitor/` 下的解析逻辑是唯一与
  直播间 API 打交道的出口，接口挂了只需要改那一处。
- **没有磁盘守卫**。录制前不检查剩余空间。audio 模式约 51 MB/小时，连续录约
  2800 小时才吃掉 146 GB；用 video 模式的话请自己留意磁盘。

---

## 免责声明

本工具仅用于**个人学习与研究**，用于把公开直播流录制到本地、生成文字资料。

使用者需自行确保其使用行为符合所在地区的法律法规，以及目标平台的服务条款与
知识产权规定。请勿用于商业再分发、二次传播他人内容或任何侵权用途。
作者不对因使用本工具产生的任何后果承担责任。

---

## License

[MIT](LICENSE)
