"""
转写模块：录制音频 → 带时间戳文本。

依赖含 faster-whisper + CUDA 轮子的 Python 环境；模型走 ~/.cache/huggingface
本地缓存，设 HF_HUB_OFFLINE=1 即完全离线。

CUDA 运行时不在系统 PATH，必须在本模块内自举 DLL 搜索路径后再 import ctranslate2。
site-packages 位置**不写死**：优先取当前解释器所属环境，其次项目内 venv。
（历史教训：曾借用另一个临时会话目录里的 venv，那个目录一旦被清理，转写链路
 就在后台**静默失效**——环境必须跟项目走。）
"""
import os
import sysconfig
import time
from pathlib import Path
from typing import Optional

CUDA_SUBDIRS = (r'nvidia\cublas\bin', r'nvidia\cuda_nvrtc\bin', r'nvidia\cudnn\bin')


def find_site_packages() -> Optional[Path]:
    """定位含 CUDA 轮子（nvidia/）的 site-packages。

    顺序：
      1. 环境变量 DLR_SITE_PACKAGES（手工指定）
      2. 当前解释器所属环境 —— 计划任务用项目内 venv 启动时天然命中
      3. 项目内 venv/ 与 .venv/ —— 解释器被换成别的 python 时兜底
    """
    candidates = []
    env = os.getenv('DLR_SITE_PACKAGES')
    if env:
        candidates.append(Path(env))
    try:
        purelib = sysconfig.get_paths().get('purelib')
        if purelib:
            candidates.append(Path(purelib))
    except Exception:
        pass
    proj = Path(__file__).resolve().parent.parent
    for name in ('venv', '.venv'):
        candidates.append(proj / name / 'Lib' / 'site-packages')
    for c in candidates:
        if c and (c / 'nvidia').exists():
            return c
    return None


SITE_PACKAGES = find_site_packages() or Path(
    sysconfig.get_paths().get('purelib', '.'))


def ensure_cuda_path(site_packages: Optional[Path] = None) -> bool:
    """把 ctranslate2 需要的 CUDA DLL 目录塞进搜索路径。

    必须在 import faster_whisper 之前调用：
    os.add_dll_directory 只对调用之后加载的库生效。
    """
    base = Path(site_packages) if site_packages else SITE_PACKAGES
    ok = False
    for sub in CUDA_SUBDIRS:
        d = base / sub
        if not d.exists():
            continue
        try:
            os.add_dll_directory(str(d))
        except Exception:
            pass
        os.environ['PATH'] = str(d) + os.pathsep + os.environ.get('PATH', '')
        ok = True
    return ok


def load_model(size: str = 'medium', device: str = 'auto', compute_type: str = ''):
    """返回 (model, 实际设备)。CUDA 不可用时自动回退 CPU。"""
    from faster_whisper import WhisperModel

    if device in ('auto', 'cuda'):
        ensure_cuda_path()
        try:
            model = WhisperModel(size, device='cuda', compute_type=compute_type or 'float16')
            model.encode  # 触发一次属性访问，尽早暴露 DLL 缺失
            return model, 'cuda'
        except Exception as exc:
            if device == 'cuda':
                raise
            print(f'[警告] CUDA 不可用，回退 CPU: {exc}')
    return WhisperModel(size, device='cpu', compute_type=compute_type or 'int8'), 'cpu'


def to_simplified(text: str) -> str:
    """Whisper 中文默认输出繁体，统一转简体。zhconv 不可用时原样返回。"""
    if not text:
        return text
    try:
        import zhconv
        return zhconv.convert(text, 'zh-cn')
    except ImportError:
        return text


def transcribe(audio_path: str, size: str = 'medium', device: str = 'auto',
               language: str = 'zh', vad_filter: bool = True,
               initial_prompt: str = '', simplify: bool = True) -> dict:
    """转写单个音频文件。

    initial_prompt 可填主播常用词/品牌名，能明显改善专有名词识别。
    """
    t0 = time.time()
    model, used = load_model(size=size, device=device)
    load_cost = time.time() - t0

    t1 = time.time()
    kwargs = {}
    if initial_prompt:
        kwargs['initial_prompt'] = initial_prompt
    segments, info = model.transcribe(str(audio_path), language=language,
                                      vad_filter=vad_filter, **kwargs)
    segs = []
    text_parts = []
    for seg in segments:
        raw = seg.text.strip()
        if simplify:
            raw = to_simplified(raw)
        segs.append({'start': round(seg.start, 2), 'end': round(seg.end, 2),
                     'text': raw})
        text_parts.append(raw)
    elapsed = time.time() - t1
    return {
        'text': ''.join(text_parts),
        'segments': segs,
        'duration': getattr(info, 'duration', 0.0),
        'language': getattr(info, 'language', ''),
        'device': used,
        'model_size': size,
        'load_cost': round(load_cost, 1),
        'elapsed': round(elapsed, 1),
        'speed': round(getattr(info, 'duration', 0.0) / elapsed, 1) if elapsed > 0 else 0,
    }


def to_srt(segments: list) -> str:
    """把带时间戳的分段转成 SRT 字幕文本。"""
    def fmt(sec: float) -> str:
        h = int(sec // 3600)
        m = int(sec % 3600 // 60)
        s = int(sec % 60)
        ms = int((sec - int(sec)) * 1000)
        return f'{h:02d}:{m:02d}:{s:02d},{ms:03d}'

    lines = []
    for i, seg in enumerate(segments, 1):
        lines.append(str(i))
        lines.append(f'{fmt(seg["start"])} --> {fmt(seg["end"])}')
        lines.append(seg['text'])
        lines.append('')
    return '\n'.join(lines)
