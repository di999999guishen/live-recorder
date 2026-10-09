"""
总结模块：转写文本 → 标点还原 → 分段摘要 → 全局摘要 → Markdown。

接任意 OpenAI 兼容接口（未配置时回退智谱 GLM-4-Flash）。
直播转写往往几万字，采用 map-reduce：先分段提取要点，再汇总，避免超长上下文降质。
"""
import json
import os
import re
import time
from pathlib import Path
from typing import Optional

import requests

PROJECT_ENV = Path(__file__).resolve().parent.parent / '.env'
FALLBACK_BASE = 'https://open.bigmodel.cn/api/paas/v4'
FALLBACK_MODEL = 'glm-4-flash'


def load_llm_config() -> dict:
    """配置来源优先级：本项目 .env → 环境变量。"""
    cfg = {'api_key': '', 'base_url': FALLBACK_BASE, 'model': FALLBACK_MODEL}
    for env_file in (PROJECT_ENV,):
        if not env_file.exists():
            continue
        for line in env_file.read_text(encoding='utf-8', errors='replace').splitlines():
            if '=' not in line or line.strip().startswith('#'):
                continue
            k, v = line.split('=', 1)
            k, v = k.strip(), v.strip()
            if k == 'LLM_API_KEY' and v:
                cfg['api_key'] = v
            elif k == 'LLM_BASE_URL' and v:
                cfg['base_url'] = v
            elif k in ('SUMMARY_MODEL', 'LLM_MODEL') and v:
                cfg['model'] = v
    cfg['api_key'] = os.getenv('LLM_API_KEY') or cfg['api_key']
    if not cfg['api_key']:
        raise RuntimeError('未找到 LLM_API_KEY，请在 .env 或环境变量中配置')
    return cfg


def call_llm(cfg: dict, system: str, user: str, temperature: float = 0.3,
             timeout: int = 180) -> str:
    url = cfg['base_url'].rstrip('/') + '/chat/completions'
    headers = {'Authorization': f'Bearer {cfg["api_key"]}', 'Content-Type': 'application/json'}
    payload = {
        'model': cfg['model'],
        'messages': [{'role': 'system', 'content': system},
                     {'role': 'user', 'content': user}],
        'temperature': temperature,
    }
    for attempt in range(3):
        try:
            r = requests.post(url, headers=headers, json=payload, timeout=timeout)
            if r.status_code == 200:
                return (r.json().get('choices') or [{}])[0].get('message', {}).get('content', '').strip()
            err = f'HTTP {r.status_code}: {r.text[:200]}'
        except Exception as exc:
            err = str(exc)
        if attempt < 2:
            time.sleep(2 ** attempt)
    raise RuntimeError(f'LLM 调用失败: {err}')


def chunk_text(text: str, size: int = 4000, overlap: int = 200) -> list:
    """按字数切块，尽量在句号/换行处断开。"""
    text = text.strip()
    if len(text) <= size:
        return [text] if text else []
    chunks, start = [], 0
    while start < len(text):
        end = min(start + size, len(text))
        if end < len(text):
            for sep in ('\n', '。', '！', '？', '；'):
                idx = text.rfind(sep, start + size // 2, end)
                if idx > start:
                    end = idx + 1
                    break
        chunks.append(text[start:end].strip())
        start = max(end - overlap, end)
    return chunks


SYSTEM_MAP = ('你是直播内容分析助手。只依据给定文本作答，不编造、不补充外部信息。'
              '输出简洁的中文要点列表。')

SYSTEM_REDUCE = ('你是内容编辑。把分段要点整理成一份结构清晰、可直接存档的中文总结。'
                 '只依据给定要点，不编造。')


def map_summaries(cfg: dict, chunks: list, on_progress=None) -> list:
    total = len(chunks)
    out = []
    for i, chunk in enumerate(chunks, 1):
        user = (f'这是某场直播转录文本的第 {i}/{total} 段。\n'
                f'请提取：1) 本段主要话题；2) 关键信息与结论；'
                f'3) 提到的商品、价格、时间、承诺等事实性内容（如有）。\n'
                f'用要点列表输出，控制在 300 字内。\n\n【原文】\n{chunk}')
        try:
            out.append(call_llm(cfg, SYSTEM_MAP, user))
        except Exception as exc:
            out.append(f'（第 {i} 段摘要失败：{exc}）')
        if on_progress:
            on_progress(i, total)
    return out


def reduce_summary(cfg: dict, parts: list, meta: dict) -> str:
    joined = '\n\n'.join(f'【第 {i} 段】\n{p}' for i, p in enumerate(parts, 1))
    user = (f'这是一场直播的各段要点。\n'
            f'主播：{meta.get("nickname", "未知")}｜标题：{meta.get("title", "")}｜'
            f'时长：{meta.get("duration_human", "")}\n\n'
            f'请汇总成结构化总结，包含：\n'
            f'1. 一句话概括\n2. 主要内容（分点）\n3. 关键事实与数据\n'
            f'4. 值得关注的结论或行动项\n\n【各段要点】\n{joined}')
    return call_llm(cfg, SYSTEM_REDUCE, user, temperature=0.4)


def punctuate(cfg: dict, text: str, chunk_size: int = 2000) -> str:
    """给没有标点的口语转写补标点与分段（直播转写通常整段无标点）。"""
    out = []
    for i, chunk in enumerate(chunk_text(text, chunk_size), 1):
        user = (f'下面是一段直播口语转写，缺少标点。请只做：补上中文标点、'
                f'按语义分段、删除明显重复和口吃（如"这个这个"）、'
                f'把繁体字统一转成简体字。\n'
                f'不要改写内容，不要增删事实，不要翻译。\n\n【原文】\n{chunk}')
        try:
            out.append(call_llm(cfg, '你是中文文本整理助手，只做标点与分段，不改写内容。', user))
        except Exception as exc:
            out.append(f'（标点处理失败：{exc}）\n{chunk}')
    return '\n\n'.join(out)


def collect_text(session_dir: Path) -> str:
    """收集会话目录里的转写文本。"""
    files = sorted([p for p in session_dir.glob('*.txt')])
    if not files:
        raise FileNotFoundError(f'{session_dir} 下没有转写文本（*.txt），请先跑转写')
    return '\n'.join(p.read_text(encoding='utf-8') for p in files)


def summarize_session(session_dir: str, do_punctuate: bool = True,
                      out_path: str = '') -> Path:
    session_dir = Path(session_dir)
    cfg = load_llm_config()
    meta = {}
    meta_file = session_dir / 'meta.json'
    if meta_file.exists():
        try:
            meta = json.loads(meta_file.read_text(encoding='utf-8'))
        except Exception:
            pass
    dur = float(meta.get('duration') or 0)
    meta['duration_human'] = f'{dur/3600:.1f} 小时' if dur >= 3600 else f'{dur/60:.1f} 分钟'

    raw = collect_text(session_dir)
    print(f'输入文本 {len(raw)} 字')

    if do_punctuate:
        print('标点还原与繁转简...')
        raw = punctuate(cfg, raw)
        (session_dir / 'transcript_punctuated.txt').write_text(raw, encoding='utf-8')
        print(f'  处理后 {len(raw)} 字')

    chunks = chunk_text(raw)
    print(f'切分为 {len(chunks)} 段，逐段提取要点...')
    parts = map_summaries(cfg, chunks, on_progress=lambda i, t: print(f'\r  进度 {i}/{t}', end=''))
    print()

    print('汇总全局总结...')
    summary = reduce_summary(cfg, parts, meta)

    out = Path(out_path) if out_path else session_dir / 'summary.md'
    # B站 get_info 不返回主播名（需 wbi 签名），拿不到就用平台+房间号兜底
    plat = meta.get('platform', '')
    plat_name = {'douyin': '抖音', 'bilibili': 'B站'}.get(plat, plat or '未知平台')
    who = meta.get('nickname') or f'{plat_name}房间 {meta.get("web_rid", "")}'.strip()
    header = (f'# {meta.get("title") or "直播总结"}\n\n'
              f'- 主播：{who}\n'
              f'- 平台/房间：{plat_name} / {meta.get("web_rid", "")}\n'
              f'- 时长：{meta.get("duration_human", "")}\n'
              f'- 生成时间：{time.strftime("%Y-%m-%d %H:%M")}\n'
              f'- 模型：{cfg["model"]}\n\n---\n\n')
    body = f'## 总结\n\n{summary}\n\n---\n\n## 各段要点\n\n'
    body += '\n\n'.join(f'### 第 {i} 段\n\n{p}' for i, p in enumerate(parts, 1))
    out.write_text(header + body, encoding='utf-8')
    print(f'已写出: {out}')
    return out
