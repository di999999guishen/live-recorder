"""
常驻守护：多主播轮询、开播自动起录、断流自动重连、下播自动停录。

用法：
    python scheduler.py                       按 anchors.json 常驻值守
    python scheduler.py --interval 300        指定轮询间隔（默认 300 秒）
    python scheduler.py --check               只跑一轮状态检查，打印后退出
    python scheduler.py --room 123456789012   临时只看这一个房间
    python scheduler.py --run-for 19800       值守满 5.5 小时后自动收尾退出
    python scheduler.py --run-for 19800 --exit-after-sessions 1
                                              时段任务模式：录满 1 场即退出，
                                              不适合再用 --run-for 兜底硬停

单实例锁：同一时间只允许一个值守进程，防止多时段任务重叠时重复录制。
进程被强杀留下的残留锁会按 PID 存活检查自动接管；调试时用 --allow-multi 跳过。

配置 anchors.json：
    {
      "anchors": [
        {"platform": "douyin",   "room": "123456789012", "mode": "audio",
         "quality": "HD1", "enabled": true},
        {"platform": "bilibili", "room": "1234567",      "mode": "audio",
         "qn": 150, "enabled": true}
      ]
    }

轮询间隔默认 300 秒：抖音/B站对高频请求敏感，间隔太短有封 IP 风险
（参考 DouyinLiveRecorder 的默认值，实测 60 秒偏激进）。
"""
import argparse
import json
import os
import signal
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

os.environ.setdefault('HF_HUB_OFFLINE', '1')  # 转写模型走本地缓存，联网会 502

from core.config import DATA_DIR, LOG_DIR  # noqa: E402
from record.recorder import LiveRecorder  # noqa: E402
from watch import build_watcher  # noqa: E402

try:
    sys.stdout.reconfigure(encoding='utf-8')
except Exception:
    pass

DEFAULT_CONFIG = Path(__file__).resolve().parent / 'anchors.json'
DEFAULT_INTERVAL = 300
JITTER = 30          # 轮询抖动上限，避免固定周期被识别为机器
RECONNECT_DELAY = 10  # 断流后重新取地址的等待
MAX_RECONNECT = 5     # 单场最多重连次数，超过视为本场结束
LOCK_FILE = Path(__file__).resolve().parent / '.lock' / 'scheduler.lock'


@dataclass
class AnchorConfig:
    platform: str
    room: str
    mode: str = 'audio'
    quality: str = 'HD1'
    qn: int = 150
    enabled: bool = True
    max_seconds: int = 0
    auto_transcribe: bool = False

    @property
    def dir_key(self) -> str:
        return f'{self.platform}_{self.room}'


def _log(msg: str) -> None:
    line = f'{time.strftime("%H:%M:%S")}  {msg}'
    print(line, flush=True)
    try:
        with open(LOG_DIR / 'scheduler.log', 'a', encoding='utf-8') as fh:
            fh.write(f'{time.strftime("%Y-%m-%d %H:%M:%S")}  {msg}\n')
    except Exception:
        pass


def _pid_alive(pid: int) -> bool:
    """判断 PID 是否仍然存活（跨平台）。"""
    if pid <= 0:
        return False
    if os.name == 'nt':
        import subprocess
        try:
            out = subprocess.run(['tasklist', '/FI', f'PID eq {pid}', '/NH'],
                                 capture_output=True, text=True, timeout=15)
            return str(pid) in (out.stdout or '')
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def acquire_lock(allow_multi: bool = False) -> Optional[Path]:
    """单实例锁：避免多个时段的任务重叠时抢录同一房间。

    场景：19:30 拉起的实例录到下播 01:00 才退，21:30 的第二个时段又到点拉起，
    两个进程同时盯同一 room → 同一场被录两遍、转写两遍、出两份稿。
    残留锁（进程已被强杀）用 PID 存活检查自动接管。
    """
    if allow_multi:
        return LOCK_FILE
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    if LOCK_FILE.exists():
        try:
            old = int((LOCK_FILE.read_text(encoding='utf-8').strip() or '0'))
        except Exception:
            old = 0
        if old and old != os.getpid() and _pid_alive(old):
            _log(f'已有值守实例在运行（PID {old}），本次退出以免重复录制')
            return None
        _log(f'发现残留锁（PID {old} 已不存在），自动接管')
    LOCK_FILE.write_text(str(os.getpid()), encoding='utf-8')
    return LOCK_FILE


def release_lock() -> None:
    try:
        if LOCK_FILE.exists() and LOCK_FILE.read_text(encoding='utf-8').strip() == str(os.getpid()):
            LOCK_FILE.unlink()
    except Exception:
        pass


class AnchorWorker(threading.Thread):
    """单个主播的值守线程。"""

    def __init__(self, cfg: AnchorConfig, interval: int = DEFAULT_INTERVAL,
                 notify: Optional[Callable[[str, str], None]] = None,
                 tick: Optional[Callable[[str, str], None]] = None,
                 run_for: int = 0, max_sessions: int = 0):
        super().__init__(daemon=True)
        self.cfg = cfg
        self.interval = interval
        self.run_for = run_for  # 整个线程的最长运行秒数，0 不限（便于测试）
        self.max_sessions = max_sessions  # 录满多少场后自动退出，0 表示不限
        self.notify = notify or (lambda _e, _m: None)
        self.tick = tick or (lambda _e, _p: None)
        self.stop_event = threading.Event()
        self.watcher = None
        self.sessions = 0

    def _make_watcher(self):
        watcher, err = build_watcher(self.cfg.room, self.cfg.platform, '', False, self.cfg.qn)
        if watcher is None:
            raise ValueError(err)
        return watcher

    def stop(self) -> None:
        self.stop_event.set()

    def _record_session(self, state) -> Optional[Path]:
        """录制一整场，断流自动续录直到主播下播。"""
        stamp = time.strftime('%Y%m%d_%H%M%S')
        session_dir = DATA_DIR / self.cfg.dir_key / time.strftime('%Y-%m-%d') / stamp
        session_dir.mkdir(parents=True, exist_ok=True)
        _log(f'[{self.cfg.room}] 开录 → {session_dir}')

        reconnect = 0
        total_duration = 0.0
        while not self.stop_event.is_set():
            rec = LiveRecorder(
                self.cfg.room, out_dir=str(session_dir), mode=self.cfg.mode,
                quality=self.cfg.quality,
                max_seconds=self.cfg.max_seconds or None,
                watcher=self.watcher)
            result = rec.run(should_stop=self.stop_event.is_set)
            total_duration += result.duration

            if self.stop_event.is_set():
                break

            # 再查一次开播状态：仍开播说明是断流，需续录；已下播则本场结束
            try:
                st = self.watcher.fetch()
                probe_failed = False
            except Exception as exc:
                st, probe_failed = None, True
                _log(f'[{self.cfg.room}] 复查状态失败: {exc}')
            if probe_failed:
                # 复查失败不轻易判定下播，按断流重连处理，由 MAX_RECONNECT 兜底
                still_live = True
            else:
                still_live = bool(st and st.is_live)
            if not still_live:
                _log(f'[{self.cfg.room}] 主播已下播，本场结束（累计 {total_duration:.0f}s）')
                break

            reconnect += 1
            if reconnect > MAX_RECONNECT:
                _log(f'[{self.cfg.room}] 重连 {MAX_RECONNECT} 次仍断流，放弃本场')
                break
            _log(f'[{self.cfg.room}] 断流但仍在播，第 {reconnect} 次续录 '
                 f'({RECONNECT_DELAY}s 后取新地址)')
            time.sleep(RECONNECT_DELAY)

        self.sessions += 1
        self.tick('record_done', str(session_dir))
        return session_dir

    def run(self) -> None:
        self.watcher = self._make_watcher()
        _log(f'[{self.cfg.room}] 值守启动（{self.cfg.platform}，间隔 {self.interval}s）')
        if self.run_for:
            # 看门狗：主线程会长时间阻塞在录制会话里，必须另起线程到点置停止信号
            def watchdog():
                time.sleep(self.run_for)
                _log(f'[{self.cfg.room}] 到达测试时限，停止值守')
                self.stop_event.set()
            threading.Thread(target=watchdog, daemon=True).start()
        while not self.stop_event.is_set():
            try:
                state = self.watcher.fetch()
            except Exception as exc:
                _log(f'[{self.cfg.room}] 探测异常: {exc}')
                self._sleep()
                continue

            if not state.ok:
                _log(f'[{self.cfg.room}] 探测失败: {state.msg}')
                self._sleep()
                continue

            if state.is_live:
                _log(f'[{self.cfg.room}] 开播 | {state.nickname} | {state.title}')
                self.notify('live_on', f'{self.cfg.platform} {self.cfg.room} 开播：{state.title}')
                session_dir = self._record_session(state)
                if session_dir:
                    self.notify('record_done', f'录制完成：{session_dir}')
                    if self.cfg.auto_transcribe:
                        self._transcribe(session_dir)
                if self.max_sessions and self.sessions >= self.max_sessions:
                    _log(f'[{self.cfg.room}] 已录满 {self.max_sessions} 场，值守结束')
                    self.stop_event.set()
                    break
                self._sleep()
            else:
                self._sleep()
        _log(f'[{self.cfg.room}] 值守停止')

    def _transcribe(self, session_dir: Path) -> None:
        try:
            from pipeline.transcriber import transcribe
        except Exception as exc:
            _log(f'[{self.cfg.room}] 转写模块不可用: {exc}')
            return
        for audio in sorted(session_dir.glob('audio*.aac')):
            try:
                _log(f'[{self.cfg.room}] 转写 {audio.name} ...')
                res = transcribe(str(audio))
                out = session_dir / f'{audio.stem}.txt'
                out.write_text(res['text'], encoding='utf-8')
                _log(f'[{self.cfg.room}] 转写完成 {out.name} '
                     f'({res["elapsed"]}s, {res["speed"]}x 实时)')
            except Exception as exc:
                _log(f'[{self.cfg.room}] 转写失败 {audio.name}: {exc}')

    def _sleep(self) -> None:
        import random
        self.stop_event.wait(self.interval + random.uniform(0, JITTER))


def load_config(path: Path) -> list:
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding='utf-8'))
    out = []
    for item in data.get('anchors', []):
        out.append(AnchorConfig(
            platform=item.get('platform', 'douyin'),
            room=str(item.get('room', '')),
            mode=item.get('mode', 'audio'),
            quality=item.get('quality', 'HD1'),
            qn=int(item.get('qn', 150)),
            enabled=bool(item.get('enabled', True)),
            max_seconds=int(item.get('max_seconds', 0)),
            auto_transcribe=bool(item.get('auto_transcribe', False)),
        ))
    return out


def write_notify(event: str, text: str) -> None:
    """通知出口：优先 SMTP 直发邮件，未配置则落 outbox 队列。

    outbox 由外部读取发送（如 cron / 计划任务 / 自建发信脚本）。
    Python 进程内无法直接调 MCP 的 Agent Mail，所以走队列解耦。
    """
    from notifier import notify as send_notify
    backend = send_notify(event, text)
    _log(f'[通知:{event}/{backend}] {text}')


def main() -> int:
    ap = argparse.ArgumentParser(description='直播录制常驻守护')
    ap.add_argument('--config', default=str(DEFAULT_CONFIG), help='主播配置文件')
    ap.add_argument('--interval', type=int, default=DEFAULT_INTERVAL, help='轮询间隔秒数')
    ap.add_argument('--check', action='store_true', help='只检查一轮状态后退出')
    ap.add_argument('--room', default='', help='临时只盯一个房间（需配 --platform）')
    ap.add_argument('--run-for', type=int, default=0, help='整个进程最长运行秒数，0 不限（测试用）')
    ap.add_argument('--exit-after-sessions', type=int, default=0,
                    help='录满 N 场后自动退出，0 表示一直值守（时段任务建议设 1）')
    ap.add_argument('--allow-multi', action='store_true',
                    help='跳过单实例锁，允许同时跑多个值守（仅调试用，会重复录制）')
    ap.add_argument('--platform', default='auto', choices=['auto', 'douyin', 'bilibili'])
    args = ap.parse_args()

    if args.room:
        cfgs = [AnchorConfig(platform=args.platform if args.platform != 'auto' else 'douyin',
                             room=args.room)]
    else:
        cfgs = load_config(Path(args.config))
        if not cfgs:
            print(f'配置文件为空或不存在: {args.config}')
            print('示例内容：')
            print(json.dumps({'anchors': [{'platform': 'douyin', 'room': '123456789012',
                                           'mode': 'audio', 'quality': 'HD1', 'enabled': True}]},
                             ensure_ascii=False, indent=2))
            return 2

    if args.check:
        for cfg in cfgs:
            watcher, err = build_watcher(cfg.room, cfg.platform, '', False, cfg.qn)
            if watcher is None:
                print(f'[{cfg.room}] {err}')
                continue
            st = watcher.fetch()
            flag = '直播中' if st.is_live else st.status_text
            extra = f' | {st.nickname} | {st.title}' if st.ok else f' | {st.msg}'
            print(f'[{cfg.platform}/{cfg.room}] {flag}{extra}')
        return 0

    lock = acquire_lock(allow_multi=args.allow_multi)
    if lock is None:
        return 3

    workers = [AnchorWorker(c, interval=args.interval, notify=write_notify,
                            run_for=args.run_for,
                            max_sessions=args.exit_after_sessions)
               for c in cfgs if c.enabled]
    if not workers:
        print('没有启用中的主播')
        release_lock()
        return 1

    for w in workers:
        w.start()

    def bye(signum, _frame):
        _log('收到停止信号，正在收尾（等待当前录制段结束）...')
        for w in workers:
            w.stop()

    signal.signal(signal.SIGINT, bye)
    signal.signal(signal.SIGTERM, bye)

    _log(f'守护已启动，{len(workers)} 个主播，间隔 {args.interval}s，Ctrl+C 停止')
    try:
        while any(w.is_alive() for w in workers):
            time.sleep(1)
    except KeyboardInterrupt:
        for w in workers:
            w.stop()
    for w in workers:
        w.join(timeout=30)
    release_lock()
    _log('守护已退出')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
