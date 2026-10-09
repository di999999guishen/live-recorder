"""
收尾兜底：扫描 data/ 下产物不全的场次，缺什么补什么。

为什么需要它：
    值守进程可能被外部强杀（Windows 计划任务的执行时限、手动结束、断电），
    此时 scheduler 的 _transcribe() 会中断 —— 转写是「先全部完成、再写 .txt」，
    中途被杀则 .txt 根本不存在，出稿环节拿不到稿子。
    本脚本按「产物是否齐全」判断，幂等，可反复运行；正常跑完的场次不会被重做。

用法：
    python catchup.py                     处理今天的所有场次
    python catchup.py --date 2026-10-09   处理指定日期
    python catchup.py --since-days 3      处理最近 3 天
    python catchup.py --no-transcribe     只封片，不做转写（快速索引）
    python catchup.py --dry-run           只列清单，不动任何文件

    # 清理：出稿 md 已落地则删除原始媒体，只留转写稿（详见 purge_media 注释）
    python catchup.py --purge-dry-run     先看会删哪些，不真删
    python catchup.py --purge             真删（默认直接 unlink）
    python catchup.py --purge --purge-trash   走回收站，可恢复

建议：跟在值守计划任务之后跑一次（如次日 01:20），把当晚场次补齐再交出稿环节。
      清理由独立的深夜任务承担（21:00，排在出稿段之后）。
"""
import argparse
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

os.environ.setdefault('HF_HUB_OFFLINE', '1')  # 模型走本地缓存，联网会 502

from core.config import DATA_DIR  # noqa: E402
from record.finalizer import finalize  # noqa: E402

try:
    sys.stdout.reconfigure(encoding='utf-8')
except Exception:
    pass

# 音频在近 120s 内还有改动 → 直播没结束。转写与清理共用这一个判据。
LIVE_GRACE = 120

# 出稿笔记（md）的落盘目录。清理段的闸门 3 靠它判断「稿子是否已经出来了」，
# 所以它必须和真正生成笔记的环节指向同一个目录 —— 否则清理永远不触发。
# 默认放项目内的 notes/，用环境变量 DLR_NOTES_DIR 可以指到别处（如自己的笔记库）。
NOTES_DIR = Path(os.environ.get('DLR_NOTES_DIR')
                 or (Path(__file__).resolve().parent / 'notes'))

# 清理环节认得的媒体后缀。audio 模式只有 .aac；video 模式是 .mp4/.flv。
# 转写稿（*.txt / *.timed.txt）、meta.json、ffmpeg.log 一律不在清理范围。
MEDIA_PATTERNS = ('*.aac', '*.mp4', '*.flv', '*.ts', '*.m4a', '*.mkv')

PURGE_LOG = Path(__file__).resolve().parent / 'logs' / 'purge.log'


def _log(msg: str) -> None:
    print(msg, flush=True)


def collect_sessions(dates: set) -> list:
    """按日期收集场次目录：data/<platform>_<room>/<YYYY-MM-DD>/<时间戳>/"""
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


def media_files(session_dir: Path) -> list:
    """可转写的音频分片。裸 aac 是 audio 模式的产物。"""
    return sorted(session_dir.glob('audio*.aac'))


def missing_transcripts(session_dir: Path) -> list:
    """返回「需要（重新）转写」的音频分片。

    判据两类：
      1. 没有 .txt，或 .txt 为空 —— 从没转过。
      2. .txt 的 mtime 早于音频 —— 转写之后音频还在增长。

    第 2 类不是洁癖：收尾段若与值守段重叠（直播还在进行），一次转写只能拿到
    当下的前缀，之后音频继续追加。若只看文件存在与否，这份「半截稿」会被当成
    终稿永久固化，后半场内容静默丢失。按 mtime 判定则下次收尾段会重转覆盖。
    """
    out = []
    for audio in media_files(session_dir):
        txt = session_dir / f'{audio.stem}.txt'
        if not (txt.exists() and txt.stat().st_size > 0):
            out.append(audio)
            continue
        try:
            if audio.stat().st_mtime > txt.stat().st_mtime + 1:
                out.append(audio)
        except OSError:
            out.append(audio)
    return out


def _hhmmss(sec: float) -> str:
    sec = int(sec)
    return f'{sec // 3600:02d}:{sec % 3600 // 60:02d}:{sec % 60:02d}'


def render_timed(segments: list) -> str:
    """把转写分段渲染成「[HH:MM:SS] 文本」的时间轴稿。

    为什么必须单独落一份：纯文本稿（.txt）不含任何时间信息，而下游出稿段要
    「按时间点做索引、摘录」。只喂纯文本，出稿模型只能编造时间戳。

    时间基准是该音频分片自身的起点 —— 分片名 audio_002 等表示重连后的新片段。
    单分片场次（常态）即等价于「相对开播时间」。
    """
    lines = []
    for seg in segments:
        text = (seg.get('text') or '').strip()
        if not text:
            continue
        lines.append(f'[{_hhmmss(seg.get("start", 0))}] {text}')
    return '\n'.join(lines)


def fill_transcripts(session_dir: Path, dry_run: bool = False) -> int:
    pending = missing_transcripts(session_dir)
    if not pending:
        return 0

    now = time.time()
    fresh = [a for a in pending if now - a.stat().st_mtime < LIVE_GRACE]
    if fresh:
        _log(f'  跳过 {len(fresh)} 个仍在写入的音频'
             f'（{", ".join(a.name for a in fresh)}，{LIVE_GRACE}s 内还有改动，疑似直播进行中）')
        pending = [a for a in pending if a not in fresh]
        if not pending:
            return 0

    if dry_run:
        for p in pending:
            _log(f'  [待转写] {p.name}（{p.stat().st_size/1024/1024:.1f} MB）')
        return 0

    from pipeline.transcriber import transcribe
    made = 0
    for audio in pending:
        txt = session_dir / f'{audio.stem}.txt'
        _log(f'  转写 {audio.name}（{audio.stat().st_size/1024/1024:.1f} MB）...')
        try:
            res = transcribe(str(audio))
            txt.write_text(res['text'], encoding='utf-8')
            timed = session_dir / f'{audio.stem}.timed.txt'
            timed.write_text(render_timed(res['segments']), encoding='utf-8')
            _log(f'    → {txt.name} + {timed.name} | {res["duration"]:.0f}s 音频 | '
                 f'耗时 {res["elapsed"]}s | {res["speed"]}x 实时 | {res["device"]}')
            made += 1
        except Exception as exc:
            _log(f'    !! 转写失败：{exc}')
    return made


# ---------------------------------------------------------------------------
# 清理：出稿 md 已落地 → 原始媒体（aac/mp4）送走，只留文稿
# ---------------------------------------------------------------------------
#
# 为什么敢删：转写稿（.txt 纯文本 + .timed.txt 时间轴）是 md 的唯一来源，
# 两者都是纯文本、几 KB 级，**永久保留**。原始音频存在的意义只是「还没转写」，
# 一旦转写完成并出了稿，它就是纯占位：audio 模式约 70 kbps → 一场 2 小时 ~65 MB，
# 一天两场、一年就是 45 GB 级别。
#
# 为什么要三重闸门：删错了不可逆。三道判据任何一条不满足就跳过，宁可留着。
#   闸 1  转写完整：每个媒体都有非空 .txt，且 .txt 不比媒体旧（防半截稿）
#   闸 2  确实停写：媒体 mtime 距今 > LIVE_GRACE（防删正在录的场次）
#   闸 3  已出稿：笔记目录里存在该日期、且 mtime 晚于最新 .txt 的 md
#         —— 这个判据刻意不依赖 md 的 frontmatter 字段，只用「文件名含日期」
#         这条最粗的线索，因为出稿 prompt 的字段定义可能变，而日期不会变。

def media_files_all(session_dir: Path) -> list:
    """场次里的全部媒体文件（清理用；转写只关心 .aac）。"""
    out = []
    for pat in MEDIA_PATTERNS:
        out.extend(session_dir.glob(pat))
    return sorted(set(out))


def latest_txt_mtime(session_dir: Path) -> float:
    """该场次最新的转写稿时间（.txt 与 .timed.txt 都算）。无稿返回 0。"""
    mts = []
    for p in session_dir.glob('*.txt'):
        try:
            mts.append(p.stat().st_mtime)
        except OSError:
            pass
    return max(mts) if mts else 0.0


def docs_for_date(notes_dir: Path, date_str: str) -> list:
    """笔记目录里属于该日期的出稿 md。

    匹配「文件名含 2026-10-09」或「含 20261009」两种写法 —— 命名习惯可能变
    （主播名_2026-10-09_1514.md 与 主播名_20261009.md 都能命中）。
    匹配不到就返回空 → 不删，安全侧失败。
    """
    if not notes_dir.exists():
        return []
    variants = {date_str, date_str.replace('-', '')}
    return [p for p in notes_dir.glob('*.md') if any(v in p.name for v in variants)]


def _send_to_trash(paths: list) -> bool:
    """把文件移入 Windows 回收站（可恢复）。纯 ctypes，不引第三方包。"""
    import ctypes
    from ctypes import wintypes

    class SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [
            ('hwnd', wintypes.HWND),
            ('wFunc', wintypes.UINT),
            ('pFrom', wintypes.LPCWSTR),
            ('pTo', wintypes.LPCWSTR),
            ('fFlags', ctypes.c_uint16),
            ('fAnyOperationsAborted', wintypes.BOOL),
            ('hNameMappings', ctypes.c_void_p),
            ('lpszProgressTitle', wintypes.LPCWSTR),
        ]

    FO_DELETE = 3
    FOF_ALLOWUNDO = 0x40
    FOF_NOCONFIRMATION = 0x10
    FOF_SILENT = 0x04
    FOF_NOERRORUI = 0x400

    src = '\0'.join(str(p) for p in paths) + '\0\0'   # 双 null 结尾是硬要求
    op = SHFILEOPSTRUCTW()
    op.wFunc = FO_DELETE
    op.pFrom = src
    op.fFlags = FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT | FOF_NOERRORUI
    rc = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
    return rc == 0 and not op.fAnyOperationsAborted


def _append_purge_log(lines: list) -> None:
    try:
        PURGE_LOG.parent.mkdir(parents=True, exist_ok=True)
        with PURGE_LOG.open('a', encoding='utf-8') as fh:
            fh.write('\n'.join(lines) + '\n')
    except OSError:
        pass


def purge_media(session_dir: Path, notes_dir: Path, dry_run: bool = False,
                use_trash: bool = False) -> tuple:
    """md 已出稿则清理该场次的原始媒体。返回 (删除个数, 释放字节)。"""
    medias = media_files_all(session_dir)
    if not medias:
        return (0, 0)

    # 闸 1：转写必须完整
    for m in medias:
        txt = session_dir / f'{m.stem}.txt'
        if not (txt.exists() and txt.stat().st_size > 0):
            _log(f'  清理跳过：{m.name} 还没有转写稿')
            return (0, 0)
        try:
            if m.stat().st_mtime > txt.stat().st_mtime + 1:
                _log(f'  清理跳过：{m.name} 比转写稿新（稿子可能不完整）')
                return (0, 0)
        except OSError:
            _log(f'  清理跳过：{m.name} 状态读取失败')
            return (0, 0)

    # 闸 2：确实停写
    now = time.time()
    if any(now - m.stat().st_mtime < LIVE_GRACE for m in medias):
        _log(f'  清理跳过：{LIVE_GRACE}s 内还有改动，疑似直播进行中')
        return (0, 0)

    # 闸 3：已出稿，且稿子晚于转写稿（说明 md 是基于最终稿生成的）
    date_str = session_dir.parent.name            # 2026-10-09
    txt_mt = latest_txt_mtime(session_dir)
    docs = [d for d in docs_for_date(notes_dir, date_str) if d.stat().st_mtime >= txt_mt]
    if not docs:
        _log(f'  清理跳过：{notes_dir} 里没有 {date_str} 的出稿 md（或 md 早于转写稿）')
        return (0, 0)

    total = sum(m.stat().st_size for m in medias)
    rel = f'{session_dir.parent.parent.name}/{date_str}/{session_dir.name}'
    if dry_run:
        _log(f'  [待清理] {len(medias)} 个媒体、{total/1024/1024:.1f} MB '
             f'（出稿：{", ".join(d.name for d in docs)}）')
        return (0, 0)

    if use_trash:
        ok = _send_to_trash(medias)
        moved = len(medias) if ok else 0
        freed = total if ok else 0
        _log(f'  清理：{moved}/{len(medias)} 个媒体移入回收站、{freed/1024/1024:.1f} MB'
             + ('' if ok else ' ⚠ 回收站操作返回失败'))
    else:
        moved = 0
        freed = 0
        for m in medias:
            try:
                sz = m.stat().st_size
                m.unlink()
                moved += 1
                freed += sz
            except OSError as exc:
                _log(f'    !! 删除失败 {m.name}：{exc}')
        _log(f'  清理：删除 {moved}/{len(medias)} 个媒体、{freed/1024/1024:.1f} MB')

    _append_purge_log([
        f'{time.strftime("%Y-%m-%d %H:%M:%S")}  {rel}  '
        f'{moved}/{len(medias)} files  {freed/1024/1024:.2f} MB  '
        f'{"trash" if use_trash else "unlink"}  doc={",".join(d.name for d in docs)}',
    ])
    return (moved, freed)


def main() -> int:
    ap = argparse.ArgumentParser(description='直播录制收尾兜底（补转写 + 封片）')
    ap.add_argument('--date', default='', help='指定日期 YYYY-MM-DD，默认今天')
    ap.add_argument('--since-days', type=int, default=1, help='从今天往前推 N 天，默认 1（仅今天）')
    ap.add_argument('--no-transcribe', action='store_true', help='跳过转写，只封片')
    ap.add_argument('--no-finalize', action='store_true', help='跳过封片，只转写')
    ap.add_argument('--dry-run', action='store_true', help='只列清单，不写任何文件')
    ap.add_argument('--purge', action='store_true',
                    help='出稿 md 已落地则删除原始媒体（aac/mp4），只留转写稿与日志')
    ap.add_argument('--purge-dry-run', action='store_true', help='只列出待清理项，不删任何文件')
    ap.add_argument('--purge-trash', action='store_true',
                    help='清理走回收站（可恢复）；默认直接删除')
    ap.add_argument('--notes-dir', default='', help=f'出稿 md 所在目录，默认 {NOTES_DIR}')
    args = ap.parse_args()

    notes_dir = Path(args.notes_dir) if args.notes_dir else NOTES_DIR

    if args.date:
        dates = {args.date}
    else:
        dates = set()
        for i in range(max(1, args.since_days)):
            dates.add(time.strftime('%Y-%m-%d', time.localtime(time.time() - i * 86400)))

    sessions = collect_sessions(dates)
    if not sessions:
        _log(f'没有找到场次。扫描范围：{sorted(dates)}，数据目录：{DATA_DIR}')
        return 0

    _log(f'扫描到 {len(sessions)} 个场次 | 日期：{sorted(dates)} | 数据目录：{DATA_DIR}')
    new_txt = 0
    purged = 0
    freed = 0
    for sess in sessions:
        rel = f'{sess.parent.parent.name}/{sess.parent.name}/{sess.name}'
        audio_n = len(media_files(sess))
        pend_n = len(missing_transcripts(sess))
        _log(f'\n[{rel}] 音频分片 {audio_n} 个，待转写 {pend_n} 个')
        if not args.no_transcribe:
            new_txt += fill_transcripts(sess, dry_run=args.dry_run)
        if not args.no_finalize and not args.dry_run:
            try:
                m = finalize(str(sess))
                flag = ' ⚠ 有异常分片' if any(i['anomaly'] for i in m['segments']) else ''
                _log(f'  封片：{m["segment_count"]} 分片 | 媒体时长 {m["total_duration"]:.0f}s '
                     f'| 墙钟 {m["wall_duration"]:.0f}s | 转写 {len(m["transcripts"])} 份{flag}')
            except Exception as exc:
                _log(f'  !! 封片失败：{exc}')
        if args.purge or args.purge_dry_run:
            n, b = purge_media(sess, notes_dir, dry_run=args.purge_dry_run,
                               use_trash=args.purge_trash)
            purged += n
            freed += b

    tail = ''
    if args.purge or args.purge_dry_run:
        # 回收站模式并不真正释放空间（文件还在 E:\$RECYCLE.BIN），措辞要分开，
        # 否则 logs 会骗人：看着天天"释放 51 MB"，实际可用空间一个字节没涨。
        verb = '移入回收站' if args.purge_trash else '释放'
        tail = (f' | 清理 {purged} 个媒体、{verb} {freed/1024/1024:.1f} MB'
                + ('（预览，未删）' if args.purge_dry_run else ''))
    _log(f'\n完成：本次新生成转写 {new_txt} 个'
         + ('（dry-run，未写任何文件）' if args.dry_run else '') + tail)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
