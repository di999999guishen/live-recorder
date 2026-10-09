"""临时冒烟：拉取 12 秒直播流，验证地址有效性与可用画质。"""
import json
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from monitor.watcher import LiveWatcher  # noqa: E402

rid = sys.argv[1] if len(sys.argv) > 1 else '123456789012'
quality = sys.argv[2] if len(sys.argv) > 2 else 'HD1'

st = LiveWatcher(rid).fetch()
if not st.is_live:
    print('未开播:', st.status_text, st.msg)
    raise SystemExit(1)

url = st.flv_urls.get(quality) or st.pick_flv()
print('拉流画质:', quality if st.flv_urls.get(quality) else 'fallback', '| url:', url[:80], '...')

out = str(Path(tempfile.gettempdir()) / '_probe.aac')
cmd = [
    'ffmpeg', '-y', '-loglevel', 'error',
    '-user_agent', 'Mozilla/5.0',
    '-headers', 'Referer: https://live.douyin.com/\r\n',
    '-i', url,
    '-vn', '-c:a', 'copy', '-t', '12', out,
]
p = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', errors='replace')
if p.returncode != 0:
    print('ffmpeg 失败:')
    print(p.stderr[-1200:])
    raise SystemExit(1)

size = Path(out).stat().st_size
print(f'录制成功: {out} | {size/1024:.1f} KB | 12s 音频')

pp = subprocess.run(['ffprobe', '-v', 'error', '-show_entries',
                     'format=duration,bit_rate', '-of', 'json', out],
                    capture_output=True, text=True, encoding='utf-8')
info = json.loads(pp.stdout or '{}')
fmt = info.get('format', {})
print(f'ffprobe: duration={float(fmt.get("duration", 0)):.1f}s '
      f'bitrate={int(fmt.get("bit_rate", 0))/1000:.0f} kbps')
print(f'估算每小时体积: {int(fmt.get("bit_rate", 0))*3600/8/1024/1024:.0f} MB')
Path(out).unlink(missing_ok=True)
