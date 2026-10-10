"""
开播检测与流地址解析。

单一职责：给定 web_rid，回答「是不是在播」以及「在播的话该拉哪个流」。
不含任何写盘逻辑，方便单独验证风控链路是否打通。
抖音接口风控变化频繁，本模块是唯一与直播间 API 打交道的出口。
"""
import re
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Optional

import requests
import urllib3
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from core.config import (
    COMMON_HEADERS,
    COMMON_PARAMS,
    DOUYIN_COOKIE,
    LIVE_HOST,
    random_ms_token,
    random_verify_fp,
)
from core.signing import a_bogus_sign

urllib3.disable_warnings()

ENTER_URI = '/webcast/room/web/enter/'

# webcast room.status 取值
STATUS_LIVE = 2      # 直播中
STATUS_END = 4       # 未开播 / 已结束

STATUS_TEXT = {
    STATUS_LIVE: '直播中',
    STATUS_END: '未开播',
    0: '未知',
}

# B站 live_status 与抖音 status 数值体系不同，单独一套
BILI_STATUS_TEXT = {1: '直播中', 0: '未开播', 2: '轮播中'}


@dataclass
class RoomState:
    """一次检测的快照（抖音与 B站共用）。

    live 是统一判据：抖音 status==2、B站 live_status==1，
    两者数值不同，统一由各平台解析时折算成 live 布尔值。
    """
    web_rid: str
    ok: bool
    platform: str = 'douyin'
    live: bool = False
    status: int = 0
    room_id: str = ''
    title: str = ''
    nickname: str = ''
    sec_uid: str = ''
    user_count: int = 0
    total_user: str = ''
    flv_urls: dict = field(default_factory=dict)
    hls_url: str = ''
    msg: str = ''
    raw: dict = field(default_factory=dict)

    @property
    def is_live(self) -> bool:
        return self.ok and self.live

    @property
    def status_text(self) -> str:
        table = BILI_STATUS_TEXT if self.platform == 'bilibili' else STATUS_TEXT
        return table.get(self.status, f'status={self.status}')

    def pick_flv(self, prefer: Optional[list] = None) -> str:
        """按优先级挑一个画质可用的 flv 地址。"""
        prefer = prefer or ['HD1', 'FULL_HD1', 'SD1', 'ORIGION', 'SD2']
        for key in prefer:
            url = self.flv_urls.get(key)
            if url:
                return url
        return next(iter(self.flv_urls.values()), '') if self.flv_urls else ''


class LiveWatcher:
    PLATFORM = 'douyin'
    """直播间状态探测器。"""

    def __init__(self, web_rid: str, cookie: str = '', use_sign: bool = False,
                 timeout: int = 10, proxies: Optional[dict] = None):
        self.web_rid = str(web_rid)
        self.use_sign = use_sign
        self.timeout = timeout
        self._cookie = cookie or DOUYIN_COOKIE
        self.session = requests.Session()
        retry = Retry(total=2, backoff_factor=0.5, status_forcelist=[502, 503, 504])
        self.session.mount('https://', HTTPAdapter(max_retries=retry))
        if proxies:
            self.session.proxies.update(proxies)
        if not self._cookie:
            self._cookie = self._fetch_temp_cookie()

    def _fetch_temp_cookie(self) -> str:
        """不带 Cookie 时先访问一次首页，拿 ttwid 等基础 Cookie。"""
        try:
            self.session.get(LIVE_HOST + '/', headers={'User-Agent': COMMON_HEADERS['User-Agent']},
                             timeout=self.timeout)
            return '; '.join(f'{c.name}={c.value}' for c in self.session.cookies)
        except Exception as exc:  # 拿不到也不致命，后续请求仍可能放行
            print(f'[警告] 临时 Cookie 获取失败: {exc}')
            return ''

    def _build_params(self) -> dict:
        params = dict(COMMON_PARAMS)
        params.update({
            'web_rid': self.web_rid,
            'msToken': random_ms_token(),
            'verifyFp': random_verify_fp(),
        })
        return params

    def request_enter(self) -> tuple:
        """请求 enter 接口，返回 (data, ok, msg)。"""
        params = self._build_params()
        headers = dict(COMMON_HEADERS)
        if self._cookie:
            headers['Cookie'] = self._cookie
        if self.use_sign:
            qs = '&'.join(f'{k}={urllib.parse.quote(str(v))}' for k, v in params.items())
            params['a_bogus'] = a_bogus_sign(qs, headers['User-Agent'])
        try:
            resp = self.session.get(LIVE_HOST + ENTER_URI, params=params,
                                    headers=headers, timeout=self.timeout)
        except requests.RequestException as exc:
            return {}, False, f'网络异常: {exc}'
        if resp.status_code != 200 or not resp.content:
            return {}, False, f'HTTP {resp.status_code}，疑似被限流'
        try:
            data = resp.json()
        except Exception:
            return {}, False, '响应非 JSON'
        if data.get('status_code') not in (0, None):
            return data, False, f"API status_code={data.get('status_code')} {data.get('status_msg', '')}"
        return data, True, ''

    @staticmethod
    def _looks_like_room(node: Any) -> bool:
        """room 对象的判别式：必须有 status，且带 id_str 或 stream_url。"""
        if not isinstance(node, dict):
            return False
        return 'status' in node and ('id_str' in node or 'stream_url' in node)

    @classmethod
    def _locate_room(cls, data: dict) -> dict:
        """enter 响应换过多次结构，按已知三种形态依次尝试。

        2026-10 实测形态：data.data[0] 直接就是 room 对象。
        """
        payload = data.get('data') or {}
        if not isinstance(payload, dict):
            return {}
        if isinstance(payload.get('room'), dict):
            return payload['room']
        if cls._looks_like_room(payload):
            return payload
        items = payload.get('data')
        if isinstance(items, list) and items:
            for item in items:
                if cls._looks_like_room(item):
                    return item
                if isinstance(item.get('room'), dict):
                    return item['room']
                nested = (item.get('data') or {}) if isinstance(item, dict) else {}
                if cls._looks_like_room(nested):
                    return nested
        return {}

    @classmethod
    def parse(cls, web_rid: str, data: dict) -> RoomState:
        room = cls._locate_room(data)
        if not room:
            return RoomState(web_rid=web_rid, ok=False, msg='响应中未找到 room 结构（结构变更或被拦截）', raw=data)

        owner = room.get('owner') or {}
        stream = room.get('stream_url') or {}
        flv = stream.get('flv_pull_url') or {}
        hls_map = stream.get('hls_pull_url_map') or {}

        # 实时在线人数在 room_view_stats.display_value，stats 里是累计观看字符串
        view = room.get('room_view_stats') or {}
        stats = room.get('stats') or {}
        try:
            user_count = int(view.get('display_value') or 0)
        except (TypeError, ValueError):
            user_count = 0

        status = int(room.get('status') or 0)
        return RoomState(
            web_rid=web_rid,
            ok=True,
            platform='douyin',
            live=(status == STATUS_LIVE),
            status=status,
            room_id=str(room.get('id_str') or room.get('id') or ''),
            title=room.get('title') or '',
            nickname=owner.get('nickname') or '',
            sec_uid=owner.get('sec_uid') or '',
            total_user=stats.get('total_user_str') or '',
            user_count=user_count,
            flv_urls=dict(flv),
            hls_url=(hls_map.get('HD1') or hls_map.get('FULL_HD1')
                     or next(iter(hls_map.values()), '')) if hls_map else '',
            raw=data,
        )

    def fetch(self) -> RoomState:
        data, ok, msg = self.request_enter()
        if not ok:
            return RoomState(web_rid=self.web_rid, ok=False, msg=msg, raw=data)
        state = self.parse(self.web_rid, data)
        if not state.ok:
            state.msg = msg or state.msg
        return state


def parse_web_rid(text: str) -> str:
    """从各种形态的抖音链接里提取 web_rid。

    支持：
      https://live.douyin.com/<rid>
      https://live.douyin.com/?...&live_web_rid=<rid>&...   （推荐页形态）
      https://live.douyin.com/reflow/<rid>
      纯数字 rid
    """
    text = (text or '').strip()
    # 推荐页链接里 anchor_id / user_id 也是长数字，必须优先匹配 live_web_rid
    for pattern in (r'live_web_rid=(\d{6,})',
                    r'live\.douyin\.com/(\d{6,})',
                    r'reflow/(\d{6,})',
                    r'web_rid=(\d{6,})'):
        m = re.search(pattern, text)
        if m:
            return m.group(1)
    if re.fullmatch(r'\d{6,}', text):
        return text
    return ''
