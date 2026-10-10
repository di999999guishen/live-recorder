"""
出稿段：转写稿 → 总结 → 归档 md（① 值守 → ② 收尾 → ③ **出稿** → ④ 清理）。

为什么单独一个脚本：
    scheduler 只管录，catchup 只管补转写与封片，两者都不该依赖 LLM ——
    值守进程要能长期稳定跑，不该因为一次接口 429 就整个趴下。
    出稿是「随时可补、失败了重跑即可」的环节，所以独立成段，按需调用。

输入（由 ② 收尾段产出）：
    <场次目录>/audio.txt        纯文本转写（无时间信息）
    <场次目录>/audio.timed.txt  时间轴转写 `[HH:MM:SS] 文本`（**唯一合法时间来源**）
    <场次目录>/meta.json        平台/房间/主播/开播下播时间

输出：
    <场次目录>/summary.md                     归档副本（finalizer 的 has_summary 认这个名）
    <DLR_NOTES_DIR>/<主播>_<日期>_<HHMM>.md    笔记副本（给 Obsidian / 知识库）

用法：
    python digest.py                       处理今天所有「有转写稿但没出稿」的场次
    python digest.py --date 2026-10-09     指定日期
    python digest.py --since-days 3        最近 3 天
    python digest.py --session <目录>       只处理某个场次
    python digest.py --dry-run             只列清单，不调模型、不写文件
    python digest.py --force               忽略幂等，重出稿
    python digest.py --no-map-cache        强制重做分段提取（默认复用 digest_map.json）
    python digest.py --no-llm              不调模型，只出「元信息 + 完整时间轴」骨架
    python digest.py --no-archive          不写场次内的 summary.md
    python digest.py --notes-dir <目录>     覆盖笔记目录
    python digest.py --anchors <文件>       指定主播配置（取 alias 作为笔记里的主播名）
    python digest.py --prompt <文件>        覆盖出稿要求（默认内置通用结构）

环境变量：
    DLR_NOTES_DIR   笔记输出目录（默认项目内 notes/）
    DLR_DATA_DIR    数据目录（默认项目内 data/）
    DLR_ANCHORS     主播配置文件，多个用 ; 分隔（默认自动扫描项目内 anchors*.json）
    LLM_BASE_URL / SUMMARY_MODEL / LLM_API_KEY   OpenAI 兼容接口

幂等：笔记 md 的 mtime ≥ 最新转写稿的 mtime 就跳过（与清理段的判据对齐）。
      转写稿若有更新（收尾段重转过半截稿），下次运行会重新出稿。

抗失败（实测教训，见 README 坑位）：
    ① 分段提取结果缓存到 <场次目录>/digest_map.json，指纹 = 转写稿(名字/大小/mtime)
       + 块数 + 模型 + 提示词哈希。转写稿与提示词都没变就复用 —— map 是整条链最慢的
       一段（1 小时直播要 3 分钟以上），不该被后面某个环节的一次失败连累重做。
       `--force` 也复用缓存（只重出稿），要重做 map 显式加 `--no-map-cache`。
    ② 出稿的两处长输出调用（分段提取、汇总成稿）**一律走流式**：网关对非流式请求
       有一条约 190 秒的墙钟上限，glm-4-flash 出 2800 token 要 ~155s，服务端一忙
       就被掐成 `500 操作失败`（实测连跑 3 次，3 次都在 190.x 秒准时失败）。
    ③ **`finish_reason` 不可用来判断稿子是否完整**（实测）：网关撞满 max_tokens 时
       如实回报 `length`，但**模型自己写一半停住时回报 `stop`** —— 两种截断里只有一种
       能被 `finish_reason` 抓到。出 4100 字却没撞 4096 上限、`finish_reason=stop`、
       `[DONE]` 已收到、0 个异常帧，就是「模型在逐句复述转写稿时把自己写停了」，
       后面的小节整节消失（实测：正文只到 [00:16:30]，转写稿实际到 [00:20:56]）。
       所以判据必须是**结构性的**：`missing_sections()` 检查六小节是否齐全。
    ④ 汇总段的递降**以「小节齐全」为一等判据**（不是以「是否抛异常」为准），五级：
       全量 → 缺节补齐（`reduce_missing` 回头单独补缺的那几节）→ 瘦身重试（时间点
       清单收窄）→ 分组成稿（压单次输出量）→ 要点拼接兜底。模型稿若非空但小节不全，
       仍采用该稿（可读性优于要点拼接）并标 degraded —— 兜底稿仍是一份合法 md，
       ④ 清理段的闸门 3 因此照常通过。
    ⑤ 出稿的两处提示词都**禁止逐句抄稿**并给了**条数上限**（观点 8–20 条、关键信息
       表格 20 行内、时间点索引 15–40 条）：转写稿动辄 600+ 个合法时间点，「有多少条
       列多少条」在结构上就不可能塞进单次输出额度，只会把额度吃光挤掉后面的小节。

时间点纪律（本脚本最要紧的约束）：
    出稿文本里出现的每一个 <HH:MM:SS> 都必须**原样存在于** audio.timed.txt。
    模型编出来的时间戳会被 verify_times() 剥掉（保留文字、去掉标记）并记入日志。
    宁可少标时间，绝不可编造 —— 这是可核验性的底线。
"""
import argparse
import datetime
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from core.config import DATA_DIR                      # noqa: E402
from pipeline.summarizer import call_llm, load_llm_config  # noqa: E402

try:
    sys.stdout.reconfigure(encoding='utf-8')
except Exception:
    pass

BASE_DIR = Path(__file__).resolve().parent
NOTES_DIR = Path(os.environ.get('DLR_NOTES_DIR') or (BASE_DIR / 'notes'))
PLATFORM_CN = {'douyin': '抖音', 'bilibili': 'B站'}

# 时间点匹配：`<00:12:34>`、`[00:12:34]`、裸 `00:12:34` 都认
RE_MARK = re.compile(r'[<`\[]?(\d{1,3}:\d{2}:\d{2})[>`\]]?')
# 必须带 MULTILINE：这个正则是拿去 finditer 整份时间轴稿的，否则 ^$ 只锚在整串首尾
RE_LINE_STAMP = re.compile(r'^\[(\d{1,3}:\d{2}:\d{2})\]\s*(.*)$', re.M)

DEFAULT_BRIEF = """请输出一份结构化的中文直播笔记，严格按下面的二级标题组织：

## 一、本场概览
3-6 句话讲清：这场在讲什么、核心结论是什么、整体语气（若是财经类，倾向偏多还是偏空）。

## 二、核心观点
**按话题归纳**后分点列出，每点以 `[HH:MM:SS]` 开头。一条 = 一个话题（不是一句原话），
数量控制在 8–20 条；不要逐句照抄转写稿，也不要漏掉主播明确说出的判断、数字、标的。

## 三、提到的关键信息
用 Markdown 表格列出本场提到的具体名词（人物/产品/板块/数据/书名/工具等），20 行以内：
| 名称 | 提及场景 | 观点或动作 | 依据（原话片段） |

## 四、关键时间点索引
只列时间点 + 一句话摘要，按时间升序，全场 15–40 条。
没有明确时间点的内容不要放进来；不要逐行照抄转写稿。

## 五、原话摘录
挑 5-10 句最有信息量的原话，逐字引用，每句前标 `[HH:MM:SS]`。

## 六、风险与免责
主播给出的风险提示、反面观点，以及「本场未给出统一风险提示」这类事实说明。"""


def _log(msg: str) -> None:
    print(f'[{time.strftime("%H:%M:%S")}] {msg}', flush=True)


# --------------------------------------------------------------------- 配置
def llm_config() -> dict:
    """summarizer 的配置 + 允许环境变量覆盖（便于换模型/接本地服务）。"""
    cfg = load_llm_config()
    cfg['base_url'] = os.getenv('LLM_BASE_URL') or cfg['base_url']
    cfg['model'] = os.getenv('SUMMARY_MODEL') or cfg['model']
    return cfg


def load_brief(prompt_file: str = '') -> str:
    """出稿要求：--prompt 文件 > 项目内 prompts/digest.md > 内置通用结构。"""
    for cand in ([Path(prompt_file)] if prompt_file
                 else [BASE_DIR / 'prompts' / 'digest.md']):
        if cand.exists():
            return cand.read_text(encoding='utf-8').strip()
    return DEFAULT_BRIEF


# --------------------------------------------------------------------- 别名
def _anchor_files(anchors_arg: str = '') -> list:
    """定位主播配置文件：--anchors > DLR_ANCHORS > 项目内 anchors*.json 全扫。"""
    if anchors_arg:
        p = Path(anchors_arg)
        return [p] if p.exists() else []
    env = os.getenv('DLR_ANCHORS') or ''
    if env:
        return [Path(x) for x in env.split(';') if x.strip() and Path(x).exists()]
    return sorted(BASE_DIR.glob('anchors*.json'))


def load_aliases(anchors_arg: str = '') -> dict:
    """从主播配置读「(平台, 房间号) → 别名」。

    为什么需要别名：笔记文件名用平台昵称很脏 —— 可能带 emoji、可能随时改名、
    不同主播可能撞名；而用户对自己长期跟的主播通常有固定叫法
    （例如平台昵称是一串花名，自己一直只叫「老王」）。
    取值优先级：anchor.alias > anchor.nickname > 运行时 meta.nickname。
    """
    out = {}
    for f in _anchor_files(anchors_arg):
        try:
            data = json.loads(f.read_text(encoding='utf-8'))
        except Exception as exc:
            _log(f'[警告] 读不了主播配置 {f}：{exc}')
            continue
        for a in data.get('anchors') or []:
            alias = str(a.get('alias') or a.get('nickname') or '').strip()
            plat = str(a.get('platform') or '').strip()
            room = str(a.get('room') or '').strip()
            if alias and plat and room:
                out[(plat, room)] = alias
    return out


def alias_for(aliases: dict, meta: dict) -> str:
    plat = str(meta.get('platform') or '')
    for key in (meta.get('web_rid'), meta.get('room_id'), meta.get('room')):
        if key and (plat, str(key)) in aliases:
            return aliases[(plat, str(key))]
    return ''


def speaker_name(meta: dict, aliases: dict = None) -> str:
    """笔记里的「主播」名：配置别名 > 平台昵称 > 平台+房间号。"""
    al = alias_for(aliases or {}, meta)
    if al:
        return al
    if meta.get('nickname'):
        return str(meta['nickname'])
    plat = PLATFORM_CN.get(meta.get('platform', ''), meta.get('platform') or '未知平台')
    return f'{plat}房间{meta.get("web_rid", "")}'


def nickname_of(meta: dict) -> str:
    return str(meta.get('nickname') or '')


# --------------------------------------------------------------------- 收集
def collect_sessions(dates: set, only: str = '') -> list:
    """按日期收集场次目录：data/<platform>_<room>/<YYYY-MM-DD>/<时间戳>/"""
    if only:
        p = Path(only)
        if not p.exists():
            raise SystemExit(f'[错误] 场次目录不存在：{p}')
        return [p]
    out = []
    if not DATA_DIR.exists():
        return out
    for account_dir in sorted(DATA_DIR.iterdir()):
        if not account_dir.is_dir():
            continue
        for date_dir in sorted(account_dir.iterdir()):
            if not date_dir.is_dir() or date_dir.name not in dates:
                continue
            for sess in sorted(date_dir.iterdir()):
                if sess.is_dir():
                    out.append(sess)
    return out


def timed_files(session_dir: Path) -> list:
    """时间轴转写稿（audio.timed.txt；断流重连会有 audio_002.timed.txt 等）。"""
    return sorted(p for p in session_dir.glob('*.timed.txt') if p.stat().st_size > 0)


def plain_files(session_dir: Path) -> list:
    return sorted(p for p in session_dir.glob('*.txt')
                  if not p.name.endswith('.timed.txt') and p.stat().st_size > 0)


def read_timed(session_dir: Path) -> str:
    """拼接全部时间轴稿。多分片时标注边界（各分片时间轴相对它自己的起点）。"""
    files = timed_files(session_dir)
    if not files:
        return ''
    if len(files) == 1:
        return files[0].read_text(encoding='utf-8', errors='replace')
    blocks = []
    for i, p in enumerate(files, 1):
        body = p.read_text(encoding='utf-8', errors='replace').strip()
        blocks.append(f'【分片 {i}/{len(files)}：{p.name}（以下时间相对本分片起点）】\n{body}')
    return '\n\n'.join(blocks)


def read_plain(session_dir: Path) -> str:
    return '\n'.join(p.read_text(encoding='utf-8', errors='replace')
                     for p in plain_files(session_dir))


def parse_stamps(timed_text: str) -> set:
    """时间轴稿里出现过的全部合法时间点（原始字符串形式）。"""
    return {m.group(1) for m in RE_LINE_STAMP.finditer(timed_text) if m.group(1)}


def latest_transcript_mtime(session_dir: Path) -> float:
    mts = []
    for p in session_dir.glob('*.txt'):
        try:
            mts.append(p.stat().st_mtime)
        except OSError:
            pass
    return max(mts) if mts else 0.0


# ---------------------------------------------------------------- 分段提取缓存
MAP_CACHE_NAME = 'digest_map.json'


def map_fingerprint(session_dir: Path, chunks: list, model: str = '') -> str:
    """分段提取结果的指纹：转写稿（名字+大小+mtime）+ 切块数 + 模型 + 提示词。

    转写稿没变就没必要重跑 map —— 一场 1 小时的直播 map 段要跑 3 分钟以上，
    而它恰恰是最容易被后面某个环节的失败连累重做的一段。

    **提示词与模型必须进指纹**：否则改了 map 的提示词（或换了 `SUMMARY_MODEL`）后，
    仍会静默复用按旧提示词提取的结果 —— 表面「出稿成功」，实则内容还是老一套。
    """
    sig = []
    for f in timed_files(session_dir):
        try:
            st = f.stat()
            sig.append(f'{f.name}:{st.st_size}:{int(st.st_mtime)}')
        except OSError:
            sig.append(f'{f.name}:?')
    sig.append(f'n={len(chunks)}')
    sig.append(f'm={model}')
    sig.append('p=' + hashlib.sha1(
        (SYSTEM_MAP + MAP_INSTR).encode('utf-8')).hexdigest()[:12])
    return '|'.join(sig)


def load_map_cache(session_dir: Path, fp: str):
    f = session_dir / MAP_CACHE_NAME
    if not f.exists():
        return None
    try:
        data = json.loads(f.read_text(encoding='utf-8'))
    except Exception:
        return None
    if not isinstance(data, dict) or data.get('fingerprint') != fp:
        return None
    parts = data.get('parts')
    return parts if isinstance(parts, list) and parts else None


def save_map_cache(session_dir: Path, fp: str, parts: list) -> None:
    try:
        (session_dir / MAP_CACHE_NAME).write_text(
            json.dumps({'fingerprint': fp, 'parts': parts}, ensure_ascii=False, indent=1),
            encoding='utf-8')
    except OSError:
        pass


def load_meta(session_dir: Path) -> dict:
    f = session_dir / 'meta.json'
    if not f.exists():
        return {}
    try:
        return json.loads(f.read_text(encoding='utf-8'))
    except Exception:
        return {}


# --------------------------------------------------------------------- 命名
def safe_name(s: str) -> str:
    """去掉文件名里不能用的字符。"""
    return re.sub(r'[\\/:*?"<>|\s]+', '', (s or '').strip()) or '未命名'


def note_filename(meta: dict, session_dir: Path, aliases: dict = None) -> str:
    started = str(meta.get('started_at') or '')
    m = re.match(r'(\d{4}-\d{2}-\d{2})[ T](\d{2}):(\d{2})', started)
    if m:
        date_str, hhmm = m.group(1), f'{m.group(2)}{m.group(3)}'
    else:                                   # 回退到场次目录名 20261009_151409
        m2 = re.match(r'(\d{4})(\d{2})(\d{2})_(\d{2})(\d{2})', session_dir.name)
        if m2:
            date_str = f'{m2.group(1)}-{m2.group(2)}-{m2.group(3)}'
            hhmm = f'{m2.group(4)}{m2.group(5)}'
        else:
            date_str = time.strftime('%Y-%m-%d')
            hhmm = time.strftime('%H%M')
    return f'{safe_name(speaker_name(meta, aliases))}_{date_str}_{hhmm}.md'


def duration_human(meta: dict) -> str:
    dur = float(meta.get('duration') or 0)
    if dur <= 0 and meta.get('started_at') and meta.get('ended_at'):
        try:
            fmt = '%Y-%m-%d %H:%M:%S'
            dur = (datetime.datetime.strptime(str(meta['ended_at']), fmt)
                   - datetime.datetime.strptime(str(meta['started_at']), fmt)).total_seconds()
        except Exception:
            dur = 0
    if dur <= 0:
        return '未知'
    if dur < 3600:
        return f'0 小时 {dur / 60:.0f} 分钟'
    return f'{int(dur // 3600)} 小时 {int(dur % 3600 // 60)} 分钟'


def yaml_scalar(v: str) -> str:
    """只在必要时加引号，尽量贴近手写 frontmatter 的样子。

    必要性来自 YAML 明文标量的限制：以特殊字符开头、含「: 」（冒号+空格）、
    含「 #」或引号时都必须加引号，否则整个 frontmatter 会被 Obsidian 判为无效。
    """
    s = str(v or '').strip()
    if not s:
        return '""'
    risky = (s[0] in '-?*&!%@`>|[]{}#' or ': ' in s or ' #' in s
             or '"' in s or "'" in s or s.endswith(':'))
    if not risky:
        return s
    return '"' + s.replace('\\', '\\\\').replace('"', '\\"') + '"'


# --------------------------------------------------------------------- 校验
def _to_sec(s: str) -> int:
    h, m, sec = (int(x) for x in s.split(':'))
    return h * 3600 + m * 60 + sec


def _fmt(sec: int) -> str:
    return f'{sec // 3600:02d}:{sec % 3600 // 60:02d}:{sec % 60:02d}'


def verify_times(body: str, valid: set, margin: int = 0) -> tuple:
    """把出稿文本里不存在于时间轴稿的时间标记剥掉。

    返回 (净化后的文本, {'bad': [...], 'fixed': [...]})。
    剥掉的是 `<HH:MM:SS>` 这个标记本身，该行文字保留 ——
    少一个时间点，比错一个时间点安全得多。

    比较按「秒数」而非字符串：模型写 `0:12:34` 也认（会统一成 `00:12:34`）。
    margin > 0 时允许把相差不超过 N 秒的标记对齐到最近的真实值（默认 0 要求逐字一致）。
    """
    valid_secs = {_to_sec(s): _fmt(_to_sec(s)) for s in valid}
    bad, fixed, out_lines = [], [], []
    for lineno, line in enumerate(body.splitlines(), 1):
        def repl(m: re.Match) -> str:
            raw = m.group(1)
            sec = _to_sec(raw)
            if sec in valid_secs:
                return m.group(0).replace(raw, valid_secs[sec])
            if margin > 0:
                near = [s for s in valid_secs if abs(s - sec) <= margin]
                if near:
                    good = valid_secs[min(near, key=lambda s: abs(s - sec))]
                    fixed.append((lineno, raw, good))
                    return m.group(0).replace(raw, good)
            bad.append((lineno, raw, line.strip()[:70]))
            # 连标记一起拿掉（<>、[]、反引号都清），只留文字
            return re.sub(r'[<>\[\]`]', '', m.group(0).replace(raw, ''))

        line_out = RE_MARK.sub(repl, line)
        # 标记被拿掉后可能留下「-  文字」这样的双空格，只在受影响的行上收一下
        if line_out != line and '  ' in line_out:
            line_out = re.sub(r'[ \t]{2,}', ' ', line_out).rstrip()
        out_lines.append(line_out)
    return '\n'.join(out_lines), {'bad': bad, 'fixed': fixed}


# --------------------------------------------------------------------- LLM
def chunk_timed(timed: str, size: int = 6000) -> list:
    """按行切块，保证不把一行时间轴劈成两半。"""
    lines, chunks, buf = timed.splitlines(), [], []
    n = 0
    for ln in lines:
        if n + len(ln) + 1 > size and buf:
            chunks.append('\n'.join(buf))
            buf, n = [], 0
        buf.append(ln)
        n += len(ln) + 1
    if buf:
        chunks.append('\n'.join(buf))
    return chunks


SYSTEM_MAP = ('你是直播内容分析助手。只依据给定文本作答，不得编造、不得补充外部信息。'
              '时间点只能原样引用文本里已出现的 [HH:MM:SS]，拿不准就不写。'
              '你的输出会被下游汇总成最终笔记，所以宁可详细，不要概括 —— '
              '信息一旦在这里被压掉，下游再也补不回来。')

SYSTEM_REDUCE = ('你是内容编辑。把各段要点**归纳**成一份结构清晰、可直接存档的中文笔记。'
                 '只依据给定材料，不得编造时间点、数据或原话。'
                 '同一话题下重复、零碎的发言要合并成一条（去掉「对吧」这类口头禅），'
                 '但**不得丢掉任何有效信息**：观点、名称、数字、原话摘录都要保留。'
                 '关键：**归纳合并 ≠ 把转写稿逐句抄一遍** —— 你要交的是一份条理清楚的'
                 '笔记，不是原文的带时间戳清单。每条应是提炼后的陈述句，不是主播的口语碎片。')


# map 段的静态指令（map_extract 与 map 缓存指纹共用）。
# 改动这里的文案会让缓存指纹变化 → 旧 digest_map.json 自动失效，不会被静默复用。
MAP_INSTR = (
    '这是某场直播时间轴转写稿的第 {i}/{n} 段（每行形如 `[HH:MM:SS] 文本`）。\n'
    '请**提炼**本段要点（不是誊抄原文）：\n'
    '1) 本段讨论了哪几个话题 —— 每个话题一句概括；\n'
    '2) 每个明确的观点、判断、结论、建议 —— 一条一条列；'
    '同一话题下意思相近的连续发言**合并成一条**，'
    '删掉「对吧」「知道吗」「是吧」这类口头禅与纯语气词；\n'
    '3) 提到的所有具体名称（人物/产品/板块/标的/数字/数据/日期），'
    '每个都要带上它出现时的时间点和上下文；\n'
    '4) 值得逐字保留的原话 —— 挑信息量最高的几句，逐字摘，连同它前面的时间点。\n'
    '输出按时间顺序排列的要点列表，每条以 `[HH:MM:SS]` 开头，'
    '**时间点必须逐字取自本段文本**。\n'
    '本段约 {chars} 字，要点通常 15–40 条。\n'
    '⚠ 要点是**提炼后的陈述**，不要把原文逐句照搬（照搬得到的是一份转写稿，'
    '不是笔记）；但也不要把几件不相干的事并成一条、漏掉有效信息。')


def map_extract(cfg: dict, chunks: list) -> list:
    out = []
    for i, chunk in enumerate(chunks, 1):
        user = (MAP_INSTR.format(i=i, n=len(chunks), chars=len(chunk))
                + f'\n\n【本段原文】\n{chunk}')
        try:
            out.append(call_llm(cfg, SYSTEM_MAP, user, timeout=300,
                                max_tokens=4096, stream=True))
        except Exception as exc:
            out.append(f'（第 {i} 段提取失败：{exc}）')
        print(f'\r  分段提取 {i}/{len(chunks)}', end='', flush=True)
    print()
    return out


def stamp_hint(stamps: set, limit: int = 1500) -> str:
    """把合法时间点全量喂给模型 —— 截断会让后半场的真实时间点被误判为编造。"""
    ordered = sorted(stamps, key=_to_sec)
    if len(ordered) > limit:                      # 极端长场次才抽样，仍保证每个都在合法集合内
        step = max(1, len(ordered) // limit)
        ordered = ordered[::step]
        tail = '（已抽样，未列出的时间点同样合法）'
    else:
        tail = ''
    return '、'.join(ordered) + tail


def reduce_user(meta: dict, session_dir: Path, parts: list, timed: str,
                brief: str, aliases: dict = None, slim: bool = False) -> str:
    """构造汇总段的 user 消息。slim=True 时把「合法时间点清单」收窄。

    收窄是安全的：模型只能引用它在要点里见过的时间点，而那些时间点本就存在于
    时间轴稿中（map 段被要求逐字抄）。清单从 ~5500 字降到千余字，prompt 处理更快、
    首字更早 —— 而「生成耗时过长」正是汇总段被网关掐成 500 的直接原因。
    """
    joined = '\n\n'.join(f'【第 {i} 段要点】\n{p}' for i, p in enumerate(parts, 1))
    stamps = stamp_hint(parse_stamps(timed))
    if slim:
        valid = parse_stamps(timed)
        seen = set()
        for p in parts:
            for m in RE_MARK.finditer(p or ''):
                if m.group(1) in valid:
                    seen.add(m.group(1))
        if seen:
            stamps = ('、'.join(sorted(seen, key=_to_sec))
                      + '（仅列要点中出现过的；未列出的时间点同样合法）')
    return (
        f'这是一场直播的各段要点，请汇总成一份笔记。\n\n'
        f'【场次信息】\n'
        f'主播：{speaker_name(meta, aliases)}\n'
        f'平台/房间：{PLATFORM_CN.get(meta.get("platform", ""), meta.get("platform", ""))}'
        f' / {meta.get("web_rid", "")}\n'
        f'标题：{meta.get("title", "")}\n'
        f'开播：{meta.get("started_at", "")}｜下播：{meta.get("ended_at", "")}\n'
        f'时长：{duration_human(meta)}\n'
        f'音频分片：{len(timed_files(session_dir))} 个\n\n'
        f'【硬性规则，违反即作废】\n'
        f'1. 只能使用 `[HH:MM:SS]` 标注时间点，且该时间点必须逐字出现在下面的'
        f'「合法时间点清单」里；\n'
        f'2. 不得推断、估算、编造任何时间点 —— 拿不准就不标时间，宁可少标；\n'
        f'3. 原话摘录必须逐字来自材料，不得改写、不得补全；\n'
        f'4. 不要写材料里没有的事实。\n\n'
        f'【合法时间点清单】\n{stamps}\n\n'
        f'【出稿要求】\n{brief}\n\n'
        f'【各段要点】\n{joined}')


def reduce_note(cfg: dict, session_dir: Path, meta: dict, parts: list,
                timed: str, brief: str, aliases: dict = None,
                slim: bool = False, max_tokens: int = 4096,
                retries: int = 1) -> str:
    """汇总成最终笔记。把「合法时间点清单」一并喂给模型，显著降低编造率。"""
    user = reduce_user(meta, session_dir, parts, timed, brief, aliases, slim=slim)
    return call_llm(cfg, SYSTEM_REDUCE, user, temperature=0.4,
                    timeout=300, max_tokens=max_tokens, retries=retries,
                    stream=True)


def split_brief(brief: str) -> tuple:
    """把出稿要求拆成 (前言, [(标题整行, 正文), ...])。

    拆的依据就是 `## ` 行 —— prompts/digest.md 与内置 DEFAULT_BRIEF 都是这个结构。
    拆不出来（没有 `## ` 行）时返回 (brief, [])，调用方据此退回「单次全量汇总」。
    """
    lines = brief.splitlines()
    heads = [i for i, ln in enumerate(lines) if ln.lstrip().startswith('## ')]
    if not heads:
        return brief.strip(), []
    preamble = '\n'.join(lines[:heads[0]]).strip()
    secs = []
    for k, i in enumerate(heads):
        j = heads[k + 1] if k + 1 < len(heads) else len(lines)
        secs.append((lines[i].strip(), '\n'.join(lines[i + 1:j]).strip()))
    return preamble, secs


def section_key(title: str) -> str:
    """小节标题的识别键：去掉 '#' 与空白，只留「一、本场概览」这种正文。

    用「正文」而不是整行 `## …` 去匹配，是为了容忍模型写成 `**一、本场概览**`
    或标题后多带空格 —— 只要小节名出现过就算这节在。
    """
    return title.lstrip('#').strip()


def missing_sections(body: str, brief: str) -> list:
    """返回正文里缺失的小节标题（按 brief 声明的顺序）。

    **这是截断的唯一可靠判据**。实测：智谱在撞满 max_tokens 时回报
    `finish_reason="length"`，但在「模型自己写一半停住」时回报 `stop` ——
    也就是说 finish_reason *不能*用来判断稿子是否完整（一场 21 分钟的直播，
    「二、核心观点」被写成逐句转写、把输出额度吃光，三～六节整节没生成，
    而网关回报的正是 `stop`）。所以只能回头做结构校验。
    """
    _, secs = split_brief(brief)
    return [t for t, _ in secs if section_key(t) not in body]


def render_brief(preamble: str, secs: list) -> str:
    """把 (前言 + 若干小节) 重新拼成一份出稿要求，用于只补某几节的调用。"""
    out = [preamble] if preamble else []
    for title, body in secs:
        out.append(title)
        if body:
            out.append(body)
    return '\n\n'.join(out)


def reduce_missing(cfg: dict, session_dir: Path, meta: dict, parts: list,
                   timed: str, brief: str, body: str, aliases: dict = None,
                   max_tokens: int = 4096, rounds: int = 2) -> str:
    """回头补齐正文里缺失的小节：缺哪节就把哪一节单独交给模型写一轮，再拼回去。

    为什么必须有这一步：单次汇总要一口气写完六个小节，模型很容易在写前面几节时
    就把输出额度用光（尤其「二、核心观点」退化成逐句转写时），后面的小节**整节消失**，
    而且**不报错、不触发任何降级** —— 稿子看着「生成成功」，实则只有一半。
    补齐比整篇重跑便宜（重跑会连已写好的小节一起重写、还可能再截断）。
    """
    preamble, secs = split_brief(brief)
    if not secs:
        return body
    for _ in range(max(0, rounds)):
        miss = [t for t, _ in secs if section_key(t) not in body]
        if not miss:
            break
        want = [s for s in secs if s[0] in miss]
        names = '、'.join(section_key(t) for t in miss)
        sub = (f'【本次**只**写下面这几个小节：{names}】\n'
               f'已经写好的小节一律不要重复、不要改写。\n'
               f'每个小节都必须以 `## 小节名` 的二级标题开头，标题原样照抄：\n\n'
               + render_brief(preamble, want))
        _log(f'  ⚠ 缺小节（{names}），单独补齐...')
        user = reduce_user(meta, session_dir, parts, timed, sub, aliases, slim=True)
        try:
            piece = call_llm(cfg, SYSTEM_REDUCE, user, temperature=0.4,
                             timeout=300, max_tokens=max_tokens, retries=1,
                             stream=True)
        except Exception as exc:
            _log(f'  ⚠ 补齐失败：{str(exc)[:120]}')
            break
        piece = (piece or '').strip()
        if not piece:
            break
        body = body.rstrip() + '\n\n' + piece
    return body


def part_range(p: str) -> tuple:
    """取一段要点里的首尾时间点，用来给分组稿标时间范围。"""
    marks = [m.group(1) for m in RE_MARK.finditer(p or '')]
    return (marks[0], marks[-1]) if marks else ('', '')


def reduce_grouped(cfg: dict, session_dir: Path, meta: dict, parts: list,
                   timed: str, brief: str, aliases: dict = None,
                   group_chars: int = 6000, per_call: int = 1400) -> str:
    """按材料量把要点分组，每组单独成稿，再按顺序拼接。

    这是「输出量」层面的兜底：单次汇总要出 ~2800 token，一旦网关的上限连流式都
    罩不住，就把成稿拆成几次 1000–1400 token 的调用。代价是各组稿子各自带小标题
    （结构分层，不如单次汇总统一），但内容更完整，也不会整篇失败。
    """
    groups, cur, cur_len = [], [], 0
    for p in parts:
        n = len(p or '')
        if cur and cur_len + n > group_chars:
            groups.append(cur)
            cur, cur_len = [], 0
        cur.append(p)
        cur_len += n
    if cur:
        groups.append(cur)
    if len(groups) <= 1:
        return reduce_note(cfg, session_dir, meta, parts, timed, brief, aliases,
                           slim=True, max_tokens=4096, retries=1)

    out = []
    for i, g in enumerate(groups, 1):
        a = part_range(g[0])[0]
        b = part_range(g[-1])[1]
        span = f'{a}–{b}' if a and b else f'第 {i} 组'
        user = reduce_user(meta, session_dir, g, timed, brief, aliases, slim=True)
        user = (f'本场直播的要点材料共 {len(groups)} 个部分，'
                f'这是**第 {i} 部分**（时间范围 {span}）。\n'
                f'请只整理这一部分的内容，不要替全篇写总结、也不要写结尾。\n\n' + user)
        piece = call_llm(cfg, SYSTEM_REDUCE, user, temperature=0.4,
                         timeout=300, max_tokens=per_call, retries=1, stream=True)
        _log(f'     第 {i}/{len(groups)} 部分成稿 {len(piece)} 字')
        out.append(f'\n\n## 第 {i} 部分（{span}）\n\n{piece.strip()}')
    return ('> 说明：本次汇总按材料分组分别成稿后拼接，'
            '因此各部分自带小标题。\n' + ''.join(out)).strip()


def note_from_parts(meta: dict, parts: list, reason: str,
                    aliases: dict = None) -> str:
    """兜底稿：把各段要点原样拼成笔记正文（不经过模型）。

    触发场景是汇总段稳定失败（网关把长生成掐成 500）。这时候**必须仍然产出一份
    md** —— 否则 ④ 清理段的闸门 3 不过，原始媒体会一直堆在盘上；而且要点本身有
    信息量，标注「未经汇总」后照样可读。map 结果已缓存，稍后重跑只花汇总那点时间。
    """
    who = speaker_name(meta, aliases)
    lines = [
        f'> ⚠ 本稿**未经模型汇总**：{reason}',
        '> 以下为各段原始要点的顺序拼接，时间点均逐字来自时间轴转写稿。',
        '',
        f'## 一、本场概览',
        f'{who} 的这场直播，汇总环节未能完成，按分段原样保留要点。',
        '',
        '## 二、核心观点',
    ]
    for i, p in enumerate(parts, 1):
        lines += ['', f'### 第 {i} 段', (p or '').strip() or '（本段无要点）']
    return '\n'.join(lines) + '\n'


def reduce_with_fallback(cfg: dict, session_dir: Path, meta: dict, parts: list,
                         timed: str, brief: str, aliases: dict = None) -> tuple:
    """汇总成笔记，返回 (正文, 是否走了兜底)。

    递降策略 —— 每一级只在上一级**失败或不完整**后才走，且都**不重试同一载荷**
    （重试确定性失败的请求只会把 timeout 白等一遍）：
      1) 全量汇总（流式，max_tokens=4096）
      2) 缺节补齐：回头校验六个小节是否齐全，缺哪节单独补一轮（最多两轮）
      3) 瘦身重试：时间点清单收窄到要点中出现过的（输入短一半），同样补齐
      4) 分组成稿：每组一个小调用，输出上限压到 1400 token
      5) 兜底：直接拼接要点，仍产出一份可用的 md

    ⚠ 第 2 步不是锦上添花：模型把额度用光时后面的小节会**整节消失**，而网关回报的
    仍是 `finish_reason=stop`、HTTP 200 —— 只看「有没有报错」会心安理得地写出一份
    残缺稿。所以这里以「小节是否齐全」为准，而不是以「是否抛异常」为准。
    """
    best, last_err = '', ''
    # 1) + 2)
    try:
        body = reduce_note(cfg, session_dir, meta, parts, timed, brief, aliases,
                           slim=False, max_tokens=4096, retries=1)
        body = reduce_missing(cfg, session_dir, meta, parts, timed, brief, body,
                              aliases, max_tokens=4096)
        if body and len(body) > len(best):
            best = body
        miss = missing_sections(body, brief)
        if body and not miss:
            return body, False
        if miss:
            _log('  ⚠ 仍缺小节：' + '、'.join(section_key(t) for t in miss))
    except Exception as exc:
        last_err = str(exc)
        _log(f'  ⚠ 汇总失败：{last_err[:140]}')
    # 3) 瘦身重试（输入减半，首字更早），仍然要补齐
    try:
        _log('     改用瘦身重试（时间点清单收窄）...')
        body = reduce_note(cfg, session_dir, meta, parts, timed, brief, aliases,
                           slim=True, max_tokens=4096, retries=1)
        body = reduce_missing(cfg, session_dir, meta, parts, timed, brief, body,
                              aliases, max_tokens=4096, rounds=1)
        if body and len(body) > len(best):
            best = body
        if body and not missing_sections(body, brief):
            return body, False
    except Exception as exc:
        last_err = str(exc)
        _log(f'  ⚠ 瘦身重试仍失败：{last_err[:140]}')
    # 4) 按材料分组成稿（自带小标题，不适用「六节齐全」校验）
    if len(parts) > 1:
        try:
            _log('     改按材料分组成稿（降低单次输出量）...')
            g = reduce_grouped(cfg, session_dir, meta, parts, timed, brief, aliases)
            if g and len(g) > len(best):
                best = g
            if g:
                return g, False
        except Exception as exc:
            last_err = str(exc)
            _log(f'  ⚠ 分组成稿仍失败：{last_err[:140]}')
    # 5) 小节始终凑不齐时，模型稿仍比「要点原样拼接」可读，优先用它
    if best.strip():
        _log('  ！模型稿小节不全，仍采用（比要点拼接更可读）')
        return best, True
    _log('     降级为「要点拼接」兜底稿（媒体清理闸门仍可通过）')
    return note_from_parts(meta, parts,
                           f'汇总接口连续失败：{last_err[:100] or "未知原因"}',
                           aliases), True


# --------------------------------------------------------------------- 渲染
def build_frontmatter(meta: dict, session_dir: Path, model: str,
                      aliases: dict = None) -> str:
    started = str(meta.get('started_at') or '')
    date_str = (started[:10] if len(started) >= 10
                else (session_dir.parent.name or time.strftime('%Y-%m-%d')))
    who = speaker_name(meta, aliases)
    nick = nickname_of(meta)
    plat = PLATFORM_CN.get(meta.get('platform', ''), meta.get('platform', ''))
    tags = ['直播']
    if plat:
        tags.append(plat)
    if who and who not in tags:
        tags.append(who)
    fm = [
        '---',
        f'title: {yaml_scalar(f"{who}直播笔记 {started[:16] or date_str}")}',
        f'date: {date_str}',
        f'主播: {yaml_scalar(who)}',
    ]
    if nick and nick != who:                   # 别名与昵称不同时才记一行，避免冗余
        fm.append(f'昵称: {yaml_scalar(nick)}')
    fm += [
        f'房间号: {yaml_scalar(str(meta.get("web_rid", "")))}',
        f'平台: {yaml_scalar(plat)}',
        f'开播: {yaml_scalar(started)}',
        f'下播: {yaml_scalar(str(meta.get("ended_at", "")))}',
        f'时长: {yaml_scalar(duration_human(meta))}',
        f'音频分片: {len(timed_files(session_dir))} 个',
        f'tags: [{", ".join(yaml_scalar(t) for t in tags)}]',
        f'生成方式: {yaml_scalar("live-recorder/digest.py（模型 " + (model or "未调用") + "）")}',
        '---', '',
    ]
    return '\n'.join(fm) + '\n'


def build_timeline_body(meta: dict, timed: str, reason: str,
                        aliases: dict = None) -> str:
    """不调模型时的骨架：只给元信息 + 完整时间轴，不做任何归纳（避免瞎编）。"""
    head = ['## 一、本场概览', '',
            f'- 主播：{speaker_name(meta, aliases)}',
            f'- 平台/房间：{PLATFORM_CN.get(meta.get("platform", ""), "")}'
            f'/{meta.get("web_rid", "")}',
            f'- 开播 / 下播：{meta.get("started_at", "")} / {meta.get("ended_at", "")}',
            f'- 时长：{duration_human(meta)}',
            f'- 录制结果：{"正常" if meta.get("ok") else (meta.get("msg") or "见 meta.json")}',
            '',
            f'> {reason}',
            '',
            '## 二、完整时间轴',
            '']
    for ln in timed.splitlines():
        m = RE_LINE_STAMP.match(ln.strip())
        if m:
            head.append(f'- `[{m.group(1)}]` {m.group(2)}')
        elif ln.strip():
            head.append(f'- {ln.strip()}')
    head += ['', '## 三、说明', '',
             '本文未经过语言模型归纳，只是把时间轴转写稿整理成了列表 —— '
             '好处是**不存在任何编造的时间点或结论**，可直接作为原始档案使用。',
             '配置 LLM_API_KEY 后再运行本脚本，即可得到归纳后的笔记。']
    return '\n'.join(head)


def write_summary(session_dir: Path, notes_dir: Path, meta: dict, body: str,
                  model: str, no_archive: bool = False,
                  aliases: dict = None) -> list:
    fm = build_frontmatter(meta, session_dir, model, aliases)
    content = fm + body.rstrip() + '\n'
    written = []
    archive = session_dir / 'summary.md'
    if not no_archive:
        archive.write_text(content, encoding='utf-8')
        written.append(archive)
    notes_dir.mkdir(parents=True, exist_ok=True)
    note = notes_dir / note_filename(meta, session_dir, aliases)
    if note.exists():
        # 同日同时刻重出（不同场次撞名）时别互相覆盖，加序号
        i = 2
        while note.exists() and note.read_text(encoding='utf-8') != content:
            note = note.with_name(f'{note.stem}_{i}{note.suffix}')
            i += 1
    note.write_text(content, encoding='utf-8')
    written.append(note)
    return written


# --------------------------------------------------------------------- 主流程
def process(session_dir: Path, notes_dir: Path, cfg: dict, brief: str,
            dry_run: bool = False, force: bool = False, use_llm: bool = True,
            no_archive: bool = False, margin: int = 0,
            aliases: dict = None, no_map_cache: bool = False) -> int:
    """返回 1=新出稿，2=dry-run 待出稿，0=跳过，-1=失败。"""
    meta = load_meta(session_dir)
    rel = f'{session_dir.parent.parent.name}/{session_dir.parent.name}/{session_dir.name}'
    timed = read_timed(session_dir)
    plain = read_plain(session_dir)

    if not timed:
        _log(f'[{rel}] 跳过：没有 *.timed.txt（收尾段的转写还没跑？）')
        return 0
    if not plain:
        _log(f'[{rel}] 提示：没有纯文本稿，仅用时间轴稿出稿')

    note = notes_dir / note_filename(meta, session_dir, aliases)
    trans_mt = latest_transcript_mtime(session_dir)
    if not force and note.exists():
        try:
            if note.stat().st_mtime >= trans_mt:
                _log(f'[{rel}] 已出稿且不比转写稿旧，跳过 → {note.name}')
                return 0
            _log(f'[{rel}] 转写稿比已有笔记新，重出稿（收尾段可能重转过）')
        except OSError:
            pass

    n_seg = len(timed_files(session_dir))
    _log(f'[{rel}] {speaker_name(meta, aliases)} | 分片 {n_seg} | '
         f'时间轴 {len(timed)} 字 | 合法时间点 {len(parse_stamps(timed))} 个')

    if dry_run:
        _log(f'  [待出稿] → {note}')
        return 2

    if use_llm:
        chunks = chunk_timed(timed)
        fp = map_fingerprint(session_dir, chunks, cfg.get('model', ''))
        parts = None if no_map_cache else load_map_cache(session_dir, fp)
        if parts:
            _log(f'  复用缓存的 {len(parts)} 段要点（转写稿自上次提取后未变）')
        else:
            _log(f'  分段提取（{len(chunks)} 段）...')
            parts = map_extract(cfg, chunks)
            if any('提取失败' in (p or '') for p in parts):
                _log('  ⚠ 有分段提取失败，本次不写缓存')
            else:
                save_map_cache(session_dir, fp, parts)
        _log('  汇总成笔记...')
        body, degraded = reduce_with_fallback(cfg, session_dir, meta, parts,
                                              timed, brief, aliases)
        body, report = verify_times(body, parse_stamps(timed), margin=margin)
        if report['fixed']:
            _log(f'  时间点对齐 {len(report["fixed"])} 处'
                 f'（例：{report["fixed"][0][1]} → {report["fixed"][0][2]}）')
        if report['bad']:
            _log(f'  ⚠ 剔除编造的时间点 {len(report["bad"])} 处（文字保留）')
            for line_no, raw, ctx in report['bad'][:5]:
                _log(f'      L{line_no} {raw}｜{ctx}')
        if degraded:
            _log('  ！本稿为降级稿（汇总未完全成功，详见上方告警）。'
                 'map 已缓存，稍后重跑只花汇总那一步的时间。')
        model = cfg.get('model', '')
    else:
        body = build_timeline_body(meta, timed, '未调用语言模型（--no-llm）', aliases)
        model = ''

    if not body.strip():
        _log('  !! 生成内容为空，放弃写入')
        return -1

    for p in write_summary(session_dir, notes_dir, meta, body, model,
                           no_archive, aliases):
        _log(f'  → {p}')
    return 1


def main() -> int:
    ap = argparse.ArgumentParser(
        description='出稿段：直播转写稿 → 笔记 md',
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    ap.add_argument('--date', default='', help='指定日期 YYYY-MM-DD')
    ap.add_argument('--since-days', type=int, default=1,
                    help='从今天往前推 N 天，默认 1（仅今天）')
    ap.add_argument('--session', default='', help='只处理指定场次目录')
    ap.add_argument('--notes-dir', default='', help=f'笔记输出目录，默认 {NOTES_DIR}')
    ap.add_argument('--anchors', default='',
                    help='主播配置文件（取 alias 作主播名），默认自动扫描项目内 anchors*.json')
    ap.add_argument('--prompt', default='', help='出稿要求文件（默认 prompts/digest.md 或内置）')
    ap.add_argument('--dry-run', action='store_true', help='只列清单，不调模型、不写文件')
    ap.add_argument('--force', action='store_true', help='忽略幂等，重出稿')
    ap.add_argument('--no-map-cache', action='store_true',
                    help='不读分段提取缓存 digest_map.json，强制重新提取（默认复用）')
    ap.add_argument('--no-llm', action='store_true',
                    help='不调模型，只出「元信息 + 完整时间轴」骨架（离线可用）')
    ap.add_argument('--no-archive', action='store_true', help='不写场次内的 summary.md')
    ap.add_argument('--time-margin', type=int, default=0,
                    help='允许把与真实时间点相差不超过 N 秒的标记对齐（默认 0，要求逐字一致）')
    args = ap.parse_args()

    notes_dir = Path(args.notes_dir) if args.notes_dir else NOTES_DIR
    brief = load_brief(args.prompt)
    aliases = load_aliases(args.anchors)

    if args.date:
        dates = {args.date}
    else:
        dates = {time.strftime('%Y-%m-%d', time.localtime(time.time() - i * 86400))
                 for i in range(max(1, args.since_days))}

    sessions = collect_sessions(dates, args.session)
    if not sessions:
        _log(f'没有找到场次。扫描范围：{sorted(dates)}，数据目录：{DATA_DIR}')
        return 1

    cfg = {}
    if not args.no_llm and not args.dry_run:
        try:
            cfg = llm_config()
        except RuntimeError as exc:
            _log(f'[错误] {exc}')
            _log('       要么配好 .env 里的 LLM_API_KEY，要么加 --no-llm 出骨架稿。')
            return 2

    _log(f'扫描到 {len(sessions)} 个场次 | 日期：{sorted(dates)} | 笔记目录：{notes_dir}')
    if aliases:
        _log(f'主播别名 {len(aliases)} 条：'
             + '，'.join(f'{p}/{r}→{a}' for (p, r), a in sorted(aliases.items())))
    made = skipped = 0
    for sess in sessions:
        try:
            r = process(sess, notes_dir, cfg, brief, dry_run=args.dry_run,
                        force=args.force, use_llm=not args.no_llm,
                        no_archive=args.no_archive, margin=args.time_margin,
                        aliases=aliases, no_map_cache=args.no_map_cache)
        except Exception as exc:
            _log(f'  !! 出稿失败：{exc}')
            continue
        if r in (1, 2):                        # 2 = dry-run 下的「待出稿」
            made += 1
        elif r == 0:
            skipped += 1
    _log(f'完成：{"" if args.dry_run else "新"}出稿 {made} 篇'
         + ('（dry-run，未写任何文件）' if args.dry_run else '')
         + f' | 跳过 {skipped} 篇')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
