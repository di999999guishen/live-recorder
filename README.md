# LiveRecorder

> 直播开播值守 → 自动录制 → 语音转写 → 文稿归档 → 空间回收。抖音 / B站双平台。

一个「挂着不用管」的直播归档工具：按你自己的时间窗值守，主播一开播就自动录，
断流自动续录，下播自动停录；随后把音频转成带时间轴的文字稿，再按需清理原始媒体。
适合长期跟踪固定主播、想留文字资料又不想留几百 GB 视频的场景。

默认 **audio 模式**（只留音轨）：抖音实测约 **51 MB/小时**，够转写和做笔记，
比 video 模式（约 0.9 GB/小时）省 18 倍空间。

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
python record.py <链接> --mode video --quality FULL_HD1

# --- 收尾兜底（幂等，可重复跑）---
python catchup.py                                    # 今天
python catchup.py --date 2026-10-09
python catchup.py --since-days 3                     # 最近 3 天
python catchup.py --dry-run                          # 只列清单，不动任何文件

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
| ③ 出稿 | 任意环节（人 / LLM / 脚本） | 读 `*.txt` + `*.timed.txt` → 生成 Markdown 笔记 |
| ④ 清理 | 计划任务 → `catchup.py --purge` | 笔记已落地则清理原始媒体，只留几 KB 的转写稿 |

③ 段不在本仓库内 —— 它是「消费转写稿」的自由环节。本仓库负责把稿子**可靠地
生产出来并带上时间基准**（`*.timed.txt`），出稿用什么工具都行。

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
      "qn": 10000,
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
| `quality` | 抖音：`ORIGION` / `FULL_HD1` / `HD1` / `SD1` / `SD2` |
| `qn` | B站画质：`10000` 原画 / `400` 蓝光 / `250` 超清 / `150` 高清 |
| `enabled` | `false` 则值守时跳过（但 `--check` 仍会探测，见坑位 17） |
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

---

## 目录结构

```
core/        config.py（UA / 公共参数）    signing.py（a_bogus 备用签名）
monitor/     watcher.py（抖音）            bilibili.py（B站）
record/      recorder.py  finalizer.py  probe_stream.py
pipeline/    transcriber.py（CUDA 自举 + 转写 + SRT）  summarizer.py（OpenAI 兼容）
scheduler.py   常驻值守（单实例锁 / --exit-after-sessions / --run-for）
catchup.py     收尾兜底（补转写 + 封片 + 清理，幂等可重跑）
register_tasks.ps1  注册 Windows 计划任务
notifier.py    通知（SMTP / outbox）
anchors.example.json  配置样例
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
