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
# 零配置兜底用 flash（免费、能跑通），但它的出稿质量差：不遵守「按话题归纳」，
# 会把「核心观点」写成几百条逐句搬运。正经用请在 .env 里配 SUMMARY_MODEL=glm-4-air
# （详见 README 坑位 28）。
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


def call_llm_stream(cfg: dict, system: str, user: str, temperature: float = 0.3,
                    timeout: int = 300, max_tokens: int = 0, on_delta=None,
                    on_finish=None) -> str:
    """流式调用 OpenAI 兼容的 /chat/completions，返回完整正文。

    **为什么长输出的调用必须走这里**：智谱网关对「非流式」请求有一条约 190 秒的
    墙钟上限。glm-4-flash 实测约 18 token/s，出一份 2800 token 的笔记要 155s，
    离上限极近 —— 服务端一忙（>190s）就直接掐断并回吐
    `500 {"error":{"code":500,"message":"操作失败"}}`，实测连跑 3 次 3 次都在
    190.x 秒准时失败，看着像「偶发」其实是确定性的。
    流式下连接持续有 token 流动，不再受该墙钟上限约束（实测同一载荷流式
    180.8s 完整拿到 4536 字，非流式同载荷必然 500）。

    timeout 用作**单次读超时**（每个 chunk 之间允许的最大间隔），不是总时长。

    on_finish 回调拿得到 finish_reason（诊断用）。⚠ 但 **finish_reason 不能用来判断
    稿子是否完整**（实测）：网关撞满 max_tokens 时如实回报 `length`（max_tokens=2000
    → `length`、产物 2927 字符）；但**模型自己写一半停住时回报 `stop`**（max_tokens=4096
    → 产物 4131 字符、收到 `[DONE]`、0 个异常帧，却只写了 2 个小节就停）。
    即两种截断里只有一种能被 finish_reason 抓到，所以判据必须是**结构性的** ——
    由调用方校验「六小节是否齐全」，见 digest.py 的 missing_sections()。
    """
    url = cfg['base_url'].rstrip('/') + '/chat/completions'
    headers = {'Authorization': f'Bearer {cfg["api_key"]}', 'Content-Type': 'application/json'}
    payload = {
        'model': cfg['model'],
        'messages': [{'role': 'system', 'content': system},
                     {'role': 'user', 'content': user}],
        'temperature': temperature,
        'stream': True,
    }
    if max_tokens:
        payload['max_tokens'] = max_tokens
    pieces = []
    finish_reason = None
    with requests.post(url, headers=headers, json=payload,
                       timeout=(15, timeout), stream=True) as r:
        if r.status_code != 200:
            raise RuntimeError(f'HTTP {r.status_code}: {r.text[:200]}')
        for raw in r.iter_lines():
            if not raw:
                continue
            line = raw.decode('utf-8', 'replace').strip()
            if line.startswith('data:'):
                line = line[5:].strip()
            if not line:
                continue
            if line == '[DONE]':
                break
            try:
                obj = json.loads(line)
            except Exception:
                continue
            # 流中途的错误帧（网关限流/超时/内容拦截）**必须抛出**。
            # 以前写成 `json.loads(line)['choices'][0]` 外面套 except:continue，
            # 遇到 {"error":{...}} 这种帧会 KeyError 被静默吞掉 —— 表现为「稿子写到
            # 一半没了」，却既无异常也不触发降级，比报错更难查。
            if isinstance(obj, dict) and obj.get('error'):
                raise RuntimeError('LLM 流中断: '
                                   + json.dumps(obj['error'], ensure_ascii=False)[:200])
            try:
                ch = obj['choices'][0]
            except Exception:
                continue
            if ch.get('finish_reason'):
                finish_reason = ch['finish_reason']
            piece = (ch.get('delta') or {}).get('content') or ''
            if piece:
                pieces.append(piece)
                if on_delta:
                    on_delta(piece)
    if on_finish:
        on_finish(finish_reason)
    return ''.join(pieces).strip()


def call_llm(cfg: dict, system: str, user: str, temperature: float = 0.3,
             timeout: int = 180, max_tokens: int = 0, retries: int = 3,
             stream: bool = False, on_delta=None, on_finish=None) -> str:
    """调用 OpenAI 兼容的 /chat/completions。

    max_tokens=0 表示不带该字段，交给服务端默认值。**出稿这类需要长输出的场景
    应当显式给一个较大的值** —— 不少模型（如 glm-4-flash）默认输出上限只有
    1024 token，长要点会在中途被静默截断，表现为「模型好像只写了一半」。

    retries: 总尝试次数（含首次）。注意智谱这类网关在**生成时间过长**时会把请求
    掐断并回吐 `500 {"error":{"code":500,"message":"操作失败"}}` —— 这种失败是
    确定性的，重试同一载荷几乎必然再失败，只是把 timeout 白等一遍。需要长生成的
    调用（出稿的汇总段）应传 retries=1，由调用方自己走「降级策略」。

    stream=True 时走 call_llm_stream()，**长输出的调用都应该开**（见那个函数的
    说明：非流式有一条约 190 秒的墙钟上限）。
    """
    if stream:
        err = ''
        for attempt in range(max(1, retries)):
            try:
                return call_llm_stream(cfg, system, user, temperature=temperature,
                                       timeout=timeout, max_tokens=max_tokens,
                                       on_delta=on_delta, on_finish=on_finish)
            except Exception as exc:
                err = f'{type(exc).__name__}: {exc}'
            if attempt < max(1, retries) - 1:
                time.sleep(2 ** attempt)
        raise RuntimeError(f'LLM 流式调用失败: {err}')

    url = cfg['base_url'].rstrip('/') + '/chat/completions'
    headers = {'Authorization': f'Bearer {cfg["api_key"]}', 'Content-Type': 'application/json'}
    payload = {
        'model': cfg['model'],
        'messages': [{'role': 'system', 'content': system},
                     {'role': 'user', 'content': user}],
        'temperature': temperature,
    }
    if max_tokens:
        payload['max_tokens'] = max_tokens
    err = ''
    for attempt in range(max(1, retries)):
        try:
            r = requests.post(url, headers=headers, json=payload, timeout=timeout)
            if r.status_code == 200:
                return (r.json().get('choices') or [{}])[0].get('message', {}).get('content', '').strip()
            err = f'HTTP {r.status_code}: {r.text[:200]}'
            # 4xx（429 除外）是确定性拒绝：请求本身不被接受，重试同一载荷没有意义
            if 400 <= r.status_code < 500 and r.status_code != 429:
                break
        except Exception as exc:
            err = f'{type(exc).__name__}: {exc}'
        if attempt < max(1, retries) - 1:
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
