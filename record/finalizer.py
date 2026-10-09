"""
封片模块：一场录制结束后的完整性校验与归档索引。

职责：
    1. 逐个 ffprobe 校验分片，标出损坏或异常短的文件
    2. 汇总真实时长与体积，写 manifest.json
    3. 生成 index.md（本场文件清单，便于归档到 Obsidian）

不做的事：合并分片。直播产物建议保留分片——合并会引入一次重编码或remux，
且单文件损坏会波及整体；分片坏了只丢一段。
"""
import json
import subprocess
import time
from pathlib import Path
from typing import Optional

MIN_DURATION = 5.0  # 短于 5 秒的分片视为异常


def probe(path: Path) -> dict:
    cmd = ['ffprobe', '-v', 'error', '-show_entries',
           'format=duration,size,bit_rate', '-of', 'json', str(path)]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True,
                             encoding='utf-8', errors='replace', timeout=60)
        data = json.loads(out.stdout or '{}').get('format', {})
        return {'ok': True,
                'duration': float(data.get('duration') or 0),
                'size': int(data.get('size') or path.stat().st_size),
                'bit_rate': int(data.get('bit_rate') or 0)}
    except Exception as exc:
        return {'ok': False, 'error': str(exc), 'duration': 0.0,
                'size': path.stat().st_size if path.exists() else 0, 'bit_rate': 0}


def finalize(session_dir: str) -> dict:
    session_dir = Path(session_dir)
    if not session_dir.exists():
        raise FileNotFoundError(session_dir)

    media = sorted(list(session_dir.glob('audio*.aac')) + list(session_dir.glob('video_*.flv')))
    meta = {}
    meta_file = session_dir / 'meta.json'
    if meta_file.exists():
        try:
            meta = json.loads(meta_file.read_text(encoding='utf-8'))
        except Exception:
            pass

    items = []
    total_duration = 0.0
    total_size = 0
    for p in media:
        info = probe(p)
        info['name'] = p.name
        info['anomaly'] = (not info['ok']) or info['duration'] < MIN_DURATION
        items.append(info)
        total_duration += info['duration']
        total_size += info['size']

    # 裸 aac 的 ffprobe 时长有误差，墙钟时长更可信，两者都记
    wall = 0.0
    if meta.get('started_at') and meta.get('ended_at'):
        try:
            import datetime
            fmt = '%Y-%m-%d %H:%M:%S'
            wall = (datetime.datetime.strptime(meta['ended_at'], fmt)
                    - datetime.datetime.strptime(meta['started_at'], fmt)).total_seconds()
        except Exception:
            pass

    texts = sorted(session_dir.glob('*.txt'))
    manifest = {
        'session_dir': str(session_dir),
        'platform': meta.get('platform', ''),
        'room': meta.get('web_rid', ''),
        'nickname': meta.get('nickname', ''),
        'title': meta.get('title', ''),
        'mode': meta.get('mode', ''),
        'segments': items,
        'segment_count': len(items),
        'total_duration': round(total_duration, 1),
        'wall_duration': round(wall, 1),
        'total_size': total_size,
        'transcripts': [p.name for p in texts],
        'has_summary': (session_dir / 'summary.md').exists(),
        'finalized_at': time.strftime('%Y-%m-%d %H:%M:%S'),
    }
    (session_dir / 'manifest.json').write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')

    lines = [f'# 录制归档 - {manifest["title"] or manifest["room"]}', '',
             f'- 平台/房间：{manifest["platform"]}/{manifest["room"]}',
             f'- 主播：{manifest["nickname"] or "未知"}',
             f'- 模式：{manifest["mode"]}',
             f'- 分片：{manifest["segment_count"]} 个，媒体时长 {manifest["total_duration"]:.0f}s'
             f'（墙钟 {manifest["wall_duration"]:.0f}s）',
             f'- 体积：{total_size/1024/1024:.1f} MB', '', '## 文件清单', '']
    for it in items:
        flag = ' ⚠ 异常' if it['anomaly'] else ''
        lines.append(f"- `{it['name']}` — {it['duration']:.1f}s, "
                     f"{it['size']/1024/1024:.2f} MB{flag}")
    if texts:
        lines += ['', '## 转写与总结', '']
        for p in texts:
            lines.append(f'- 转写稿：`{p.name}`')
        if manifest['has_summary']:
            lines.append('- 总结：`summary.md`')
    (session_dir / 'index.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')

    return manifest


if __name__ == '__main__':
    import sys
    if len(sys.argv) < 2:
        print('用法: python record/finalizer.py <session_dir>')
        raise SystemExit(2)
    m = finalize(sys.argv[1])
    print(json.dumps(m, ensure_ascii=False, indent=2)[:1200])
