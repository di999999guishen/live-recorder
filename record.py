"""
录制入口：检测到开播后立即拉流落盘。

用法：
    python record.py <直播间链接或 web_rid>                 一直录到主播下播
    python record.py <链接> --duration 60                   只录 60 秒（冒烟测试）
    python record.py <链接> --mode video --quality HD1      含画面录制（注意磁盘）
    python record.py <链接> --max-seconds 21600             单场最长 6 小时
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from core.config import DOUYIN_COOKIE  # noqa: E402
from monitor.bilibili import QUALITY_QN, parse_room_id  # noqa: E402
from monitor.watcher import parse_web_rid  # noqa: E402
from record.recorder import LiveRecorder  # noqa: E402
from watch import build_watcher  # noqa: E402

try:
    sys.stdout.reconfigure(encoding='utf-8')
except Exception:
    pass


def main() -> int:
    ap = argparse.ArgumentParser(description='抖音直播录制')
    ap.add_argument('target', help='直播间链接（支持 live_web_rid= 形态）或纯数字 web_rid')
    ap.add_argument('--mode', default='audio', choices=['audio', 'video'],
                    help='audio=仅音轨(≈51MB/h)，video=含画面(≈0.9GB/h@HD1)')
    ap.add_argument('--quality', default='HD1',
                    choices=['ORIGION', 'FULL_HD1', 'HD1', 'SD1', 'SD2'],
                    help='抖音画质，默认 HD1(高清)；ORIGION=原画/FULL_HD1=蓝光/SD1=标清/SD2=流畅')
    ap.add_argument('--duration', type=int, default=0, help='录制秒数，0 表示录到下播')
    ap.add_argument('--max-seconds', type=int, default=0, help='单场最长秒数，0 表示不限')
    ap.add_argument('--out', default='', help='输出目录，默认 data/<rid>/<日期>/<时间戳>')
    ap.add_argument('--cookie', default=DOUYIN_COOKIE)
    ap.add_argument('--platform', default='auto', choices=['auto', 'douyin', 'bilibili'])
    ap.add_argument('--qn', type=int, default=150, help='B站画质，默认 150(高清)；10000原画/400蓝光/250超清/80流畅')
    args = ap.parse_args()

    qn = args.qn or QUALITY_QN.get(args.quality, 150)
    watcher, err = build_watcher(args.target, args.platform, args.cookie, False, qn)
    if watcher is None:
        print(f'[错误] {err}: {args.target}')
        return 2

    # BiliWatcher 用 input_room，LiveWatcher 用 web_rid 作为目录名
    room_key = getattr(watcher, 'input_room', None) or getattr(watcher, 'web_rid', None) or args.target
    rec = LiveRecorder(room_key, out_dir=args.out, mode=args.mode, quality=args.quality,
                       max_seconds=args.max_seconds or None, watcher=watcher)
    result = rec.run(duration=args.duration or None)

    print('=' * 60)
    if result.ok:
        mb = result.size_bytes / 1024 / 1024
        print(f'完成: {result.path}')
        print(f'  {mb:.2f} MB | 时长 {result.duration:.1f}s | 用时 {result.elapsed:.0f}s | {result.msg}')
    else:
        print(f'失败: {result.msg}')
        print(f'  目录: {result.session_dir}')
    print('=' * 60)
    return 0 if result.ok else 1


if __name__ == '__main__':
    raise SystemExit(main())
