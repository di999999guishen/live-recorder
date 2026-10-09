"""
录制模块：把直播流拉到本地文件。

双模式：
    audio —— 只取音轨，实测约 51 MB/小时，够用于转写与总结，默认
    video —— 含画面，2-4 GB/小时，强制按 segment_seconds 分片

设计要点：
    1. 流地址带 expire，取到后立刻起 ffmpeg，不做缓存
    2. -reconnect* 让 ffmpeg 自行处理短断流
    3. 心跳检测：文件持续不增长判定为断流，留给上层决定是否重连
    4. 停止时向 stdin 写 q 优雅退出，超时再 kill
"""
import json
import os
import re
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from core.config import DATA_DIR, UA
from monitor.watcher import LiveWatcher

VALID_MODES = ('audio', 'video')


@dataclass
class RecordResult:
    ok: bool
    path: str
    mode: str
    quality: str = ''
    size_bytes: int = 0
    duration: float = 0.0
    elapsed: float = 0.0
    msg: str = ''
    session_dir: str = ''


def probe_duration(path: Path) -> float:
    """用 ffprobe 读真实时长，容器损坏时返回 0。"""
    if not Path(path).exists():
        return 0.0
    cmd = ['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
           '-of', 'default=noprint_wrappers=1:nokey=1', str(path)]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True,
                             encoding='utf-8', errors='replace', timeout=30)
        return float(out.stdout.strip() or 0)
    except Exception:
        return 0.0


class LiveRecorder:
    """一场直播的录制会话。"""

    def __init__(self, web_rid: str, out_dir: str = '', mode: str = 'audio',
                 quality: str = 'HD1', segment_seconds: int = 1800,
                 max_seconds: Optional[int] = None, watcher: Optional[LiveWatcher] = None,
                 cookie: str = ''):
        self.web_rid = str(web_rid)
        self.mode = mode if mode in VALID_MODES else 'audio'
        self.quality = quality
        self.segment_seconds = max(int(segment_seconds), 60)
        self.max_seconds = max_seconds
        self.watcher = watcher or LiveWatcher(self.web_rid, cookie=cookie)
        self.proc: Optional[subprocess.Popen] = None
        self._log_fh = None
        self.state = None
        self._current_out: Optional[Path] = None  # 本次 run 的输出路径，只解析一次

        stamp = time.strftime('%Y%m%d_%H%M%S')
        base = Path(out_dir) if out_dir else DATA_DIR / self.web_rid / time.strftime('%Y-%m-%d') / stamp
        base.mkdir(parents=True, exist_ok=True)
        self.session_dir = base
        self.log_path = base / 'ffmpeg.log'

    def _resolve_out(self) -> Path:
        """解析本次分片的路径。

        同一 session_dir 反复 run（scheduler 续录）时递增序号，避免覆盖上一段。
        """
        if self.mode == 'audio':
            existing = list(self.session_dir.glob('audio*.aac'))
            if not existing:
                return self.session_dir / 'audio.aac'
            return self.session_dir / f'audio_{len(existing) + 1:03d}.aac'
        return self.session_dir / 'video_%04d.flv'

    def _out_pattern(self) -> Path:
        """输出路径。必须缓存：每次重算会因文件已存在而跳到下一个不存在的名字，
        导致心跳读到的大小恒为 0。"""
        if self._current_out is None:
            self._current_out = self._resolve_out()
        return self._current_out

    def _build_cmd(self, url: str) -> list:
        out = str(self._out_pattern())
        cmd = [
            'ffmpeg', '-y', '-loglevel', 'info',
            '-user_agent', UA,
            '-headers', 'Referer: https://live.douyin.com/\r\n',
            '-reconnect', '1', '-reconnect_streamed', '1', '-reconnect_delay_max', '5',
            '-rw_timeout', '20000000',
            '-i', url,
        ]
        # -flush_packets 1：ffmpeg 默认攒满 256KB 才落盘，实测心跳会因此误判断流
        if self.mode == 'audio':
            cmd += ['-vn', '-c:a', 'copy', '-flush_packets', '1']
        else:
            cmd += ['-c', 'copy', '-flush_packets', '1', '-f', 'segment',
                    '-segment_time', str(self.segment_seconds), '-reset_timestamps', '1']
        if self.max_seconds:
            cmd += ['-t', str(int(self.max_seconds))]
        cmd += [out]
        return cmd

    def _current_size(self) -> int:
        if self.mode == 'audio':
            p = self._out_pattern()
            return p.stat().st_size if p.exists() else 0
        return sum(f.stat().st_size for f in self.session_dir.glob('video_*.flv'))

    def _pump_stderr(self) -> None:
        try:
            for line in self.proc.stderr:
                if self._log_fh:
                    self._log_fh.write(line)
                    self._log_fh.flush()
        except Exception:
            pass

    def _last_log_time(self) -> float:
        """解析 ffmpeg 日志里最后一个 time=，得到已写入的媒体时长（秒）。

        比文件大小可靠：即使 ffmpeg 缓冲未落盘，这里也会持续增长。
        """
        try:
            with open(self.log_path, 'r', encoding='utf-8', errors='replace') as fh:
                fh.seek(0, os.SEEK_END)
                pos = fh.tell()
                fh.seek(max(0, pos - 4096))
                tail = fh.read()
        except Exception:
            return 0.0
        matches = re.findall(r'time=(\d+):(\d+):(\d+(?:\.\d+)?)', tail)
        if not matches:
            return 0.0
        h, m, s = matches[-1]
        return int(h) * 3600 + int(m) * 60 + float(s)

    def _write_meta(self, started_at: str, ended_at: str, duration: float,
                    size: int, ok: bool, msg: str) -> None:
        owner = getattr(self.state, 'nickname', '') or ''
        platform = getattr(self.watcher, 'PLATFORM', '') or getattr(self.state, 'platform', '')
        meta = {
            'web_rid': self.web_rid,
            'platform': platform,
            'room_id': getattr(self.state, 'room_id', ''),
            'nickname': owner,
            'title': getattr(self.state, 'title', ''),
            'sec_uid': getattr(self.state, 'sec_uid', ''),
            'mode': self.mode,
            'quality': self.quality,
            'started_at': started_at,
            'ended_at': ended_at,
            'duration': round(duration, 1),
            'size_bytes': size,
            'ok': ok,
            'msg': msg,
        }
        (self.session_dir / 'meta.json').write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding='utf-8')

    def stop(self, timeout: int = 15) -> None:
        if not self.proc or self.proc.poll() is not None:
            return
        try:
            if self.proc.stdin:
                self.proc.stdin.write('q')
                self.proc.stdin.flush()
        except Exception:
            pass
        try:
            self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        if self._log_fh:
            self._log_fh.flush()

    def run(self, duration: Optional[int] = None, poll: float = 5.0,
            stall_limit: int = 3, quiet: bool = False,
            should_stop: Optional[object] = None) -> RecordResult:
        """should_stop：可选的回调，返回 True 时提前结束（供 scheduler 的 Ctrl+C 使用）。"""
        """录制一场直播。duration 为主动停止秒数，None 表示一直录到主播下播或异常。"""
        started_at = time.strftime('%Y-%m-%d %H:%M:%S')
        t0 = time.time()

        self.state = self.watcher.fetch()
        if not self.state.ok:
            return RecordResult(False, '', self.mode, self.quality,
                                msg=f'探测失败: {self.state.msg}', session_dir=str(self.session_dir))
        if not self.state.is_live:
            return RecordResult(False, '', self.mode, self.quality,
                                msg=f'未开播（{self.state.status_text}）',
                                session_dir=str(self.session_dir))

        url = self.state.flv_urls.get(self.quality) or self.state.pick_flv()
        if not url:
            return RecordResult(False, '', self.mode, self.quality,
                                msg='未取到可用拉流地址', session_dir=str(self.session_dir))

        used_quality = self.quality if self.state.flv_urls.get(self.quality) else 'fallback'
        if not quiet:
            print(f'[{self.web_rid}] 开播中 | {self.state.nickname} | {self.state.title}')
            print(f'  画质 {used_quality} | 模式 {self.mode} | 输出 {self.session_dir}')

        cmd = self._build_cmd(url)
        self._log_fh = open(self.log_path, 'w', encoding='utf-8', errors='replace')
        self.proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE, text=True, encoding='utf-8', errors='replace')
        threading.Thread(target=self._pump_stderr, daemon=True).start()

        msg = ''
        ok = True
        last_size = 0
        last_prog = 0.0
        stall = 0
        try:
            while True:
                time.sleep(poll)
                if callable(should_stop) and should_stop():
                    msg = '外部请求停止'
                    break
                if self.proc.poll() is not None:
                    msg = f'ffmpeg 提前退出，code={self.proc.returncode}（见 {self.log_path.name}）'
                    ok = False
                    break
                size = self._current_size()
                prog = self._last_log_time()
                # 文件大小与 ffmpeg 自报进度任一在增长都算正常
                if size > last_size or prog > last_prog + 0.5:
                    stall = 0
                    if not quiet:
                        print(f'\r  媒体 {prog:.0f}s | 写入 {size/1024/1024:.2f} MB '
                              f'| 用时 {time.time()-t0:.0f}s', end='', flush=True)
                else:
                    stall += 1
                    if stall >= stall_limit:
                        msg = f'连续 {stall_limit} 次心跳（约 {int(poll*stall_limit)}s）无增长，判定断流'
                        ok = False
                        break
                last_size, last_prog = size, prog
                elapsed = time.time() - t0
                if duration and elapsed >= duration:
                    msg = f'到达设定时长 {duration}s，主动停止'
                    break
                if self.max_seconds and elapsed >= self.max_seconds:
                    msg = f'到达上限 {self.max_seconds}s，主动停止'
                    break
        except KeyboardInterrupt:
            msg = '用户中断'
        finally:
            self.stop()
            if self._log_fh:
                self._log_fh.close()
                self._log_fh = None

        if not quiet:
            print()
        size = self._current_size()
        primary = self._out_pattern()
        probe_target = primary if self.mode == 'audio' else self._first_segment()
        dur = probe_duration(probe_target)
        ended_at = time.strftime('%Y-%m-%d %H:%M:%S')
        self._write_meta(started_at, ended_at, dur, size, ok, msg)
        return RecordResult(ok, str(probe_target or ''), self.mode, used_quality,
                            size, dur, time.time() - t0, msg, str(self.session_dir))

    def _first_segment(self) -> Optional[Path]:
        segs = sorted(self.session_dir.glob('video_*.flv'))
        return segs[0] if segs else None
