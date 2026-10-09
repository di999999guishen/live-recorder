"""
P0 验证入口：只做开播检测，不写任何录像文件。

用法：
    python watch.py 123456789012                 持续检测，状态变化时才打印
    python watch.py <链接> --once               只查一次并打印详情
    python watch.py <链接> --sign               带 a_bogus 签名请求
    python watch.py <链接> --interval 30        调整轮询间隔（秒）
"""
import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from core.config import DOUYIN_COOKIE  # noqa: E402
from monitor.bilibili import BiliWatcher, parse_room_id  # noqa: E402
from monitor.watcher import LiveWatcher, parse_web_rid  # noqa: E402


def build_watcher(target: str, platform: str, cookie: str, sign: bool, qn: int):
    """按平台构造探测器。auto 时按链接域名判断。"""
    if platform == 'auto':
        platform = 'bilibili' if 'bilibili' in target.lower() else 'douyin'
    if platform == 'bilibili':
        room = parse_room_id(target)
        if not room:
            return None, '无法从输入中提取 B站房间号'
        return BiliWatcher(room, cookie=cookie, qn=qn), ''
    rid = parse_web_rid(target)
    if not rid:
        return None, '无法从输入中提取抖音 web_rid'
    return LiveWatcher(rid, cookie=cookie, use_sign=sign), ''

# git bash / 旧控制台下强制 UTF-8，避免中文输出报编码错
try:
    sys.stdout.reconfigure(encoding='utf-8')
except Exception:
    pass


def describe(state) -> str:
    if not state.ok:
        return f'[{state.web_rid}] 探测失败 - {state.msg}'
    base = f'[{state.web_rid}] {state.status_text}'
    if state.is_live:
        qualities = ','.join(state.flv_urls.keys()) or '无'
        base += f' | {state.nickname} | {state.title} | 在线 {state.user_count} | 画质 {qualities}'
    return base


def dump_detail(state) -> None:
    print('=' * 60)
    print(describe(state))
    if state.ok and state.is_live:
        for q, url in state.flv_urls.items():
            print(f'  flv[{q}] {url[:110]}...')
        if state.hls_url:
            print(f'  hls   {state.hls_url[:110]}...')
        print(f'  room_id = {state.room_id}')
    print('=' * 60)


def main() -> int:
    ap = argparse.ArgumentParser(description='抖音直播间开播状态检测')
    ap.add_argument('target', help='直播间链接、web_rid 或 reflow 地址')
    ap.add_argument('--once', action='store_true', help='只检测一次')
    ap.add_argument('--sign', action='store_true', help='启用 a_bogus 签名')
    ap.add_argument('--interval', type=int, default=60, help='轮询间隔秒数，默认 60')
    ap.add_argument('--cookie', default=DOUYIN_COOKIE, help='手动指定 Cookie')
    ap.add_argument('--verbose', action='store_true', help='每次轮询都打印')
    ap.add_argument('--platform', default='auto', choices=['auto', 'douyin', 'bilibili'],
                    help='默认按链接域名自动判断')
    ap.add_argument('--qn', type=int, default=10000, help='B站画质 qn：10000原画/400蓝光/250超清')
    args = ap.parse_args()

    watcher, err = build_watcher(args.target, args.platform, args.cookie, args.sign, args.qn)
    if watcher is None:
        print(f'[错误] {err}: {args.target}')
        return 2

    print(f'平台: {watcher.__class__.__name__} | 目标: {args.target} | '
          f'Cookie: {"已提供" if args.cookie else "临时/无"}')

    if args.once:
        dump_detail(watcher.fetch())
        return 0

    last_key = None
    print(f'开始轮询，间隔 {args.interval}s，Ctrl+C 退出\n')
    while True:
        try:
            state = watcher.fetch()
            key = (state.ok, state.status, state.room_id)
            if args.verbose or key != last_key:
                print(f'{time.strftime("%H:%M:%S")}  {describe(state)}')
                if state.is_live and key != last_key:
                    for q, url in state.flv_urls.items():
                        print(f'    {q}: {url[:100]}...')
                last_key = key
            time.sleep(args.interval)
        except KeyboardInterrupt:
            print('\n已退出')
            return 0


if __name__ == '__main__':
    raise SystemExit(main())
