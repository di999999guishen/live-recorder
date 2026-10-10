"""
B站直播：开播检测与流地址解析。

与抖音模块共用 RoomState 数据结构，scheduler 无需区分平台。

接口（实测 2026-10-06，无需 wbi 签名）：
    1) room/v1/Room/get_info          → 真实 room_id 与 live_status（1=直播中，0=未开播，2=轮播）
    2) xlive/web-room/v2/index/getRoomPlayInfo → playurl_info（flv / hls-ts / hls-fmp4）

坑位：
    - 直播间短号（如 320）不等于真实 room_id（286893），必须先用 get_info 映射
    - 流地址是 host + codec.base_url + url_info.extra 三段拼接，
      codec 里没有 'url' 字段，只有 'base_url'（拼错会得到 5XX）
    - 拉流必须带 Referer: https://live.bilibili.com/
"""
import re
from typing import Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from core.config import UA
from monitor.watcher import RoomState

API_LIVE = 'https://api.live.bilibili.com'
INFO_URI = '/room/v1/Room/get_info'
PLAY_URI = '/xlive/web-room/v2/index/getRoomPlayInfo'

# live_status 取值
STATUS_LIVE = 1
STATUS_OFF = 0
STATUS_ROUND = 2

STATUS_TEXT = {STATUS_LIVE: '直播中', STATUS_OFF: '未开播', STATUS_ROUND: '轮播中'}

# qn 画质：10000 原画 / 400 蓝光 / 250 超清 / 150 高清(默认) / 80 流畅
QUALITY_QN = {'ORIGION': 10000, 'FULL_HD1': 400, 'HD1': 250, 'SD1': 150, 'SD2': 80}
DEFAULT_QN = 150


class BiliWatcher:
    """B站直播间状态探测器。"""

    PLATFORM = 'bilibili'

    def __init__(self, room_id: str, cookie: str = '', qn: int = DEFAULT_QN, timeout: int = 10):
        self.input_room = str(room_id)
        self.qn = qn
        self.timeout = timeout
        self._cookie = cookie
        self._real_room: Optional[str] = None
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': UA,
            'Referer': 'https://live.bilibili.com/',
            'Accept': 'application/json, text/plain, */*',
        })
        if cookie:
            self.session.headers['Cookie'] = cookie
        retry = Retry(total=2, backoff_factor=0.5, status_forcelist=[502, 503, 504])
        self.session.mount('https://', HTTPAdapter(max_retries=retry))

    def _get(self, uri: str, params: dict) -> tuple:
        try:
            resp = self.session.get(API_LIVE + uri, params=params, timeout=self.timeout)
        except requests.RequestException as exc:
            return {}, False, f'网络异常: {exc}'
        if resp.status_code != 200 or not resp.content:
            return {}, False, f'HTTP {resp.status_code}，疑似被限流'
        try:
            data = resp.json()
        except Exception:
            return {}, False, '响应非 JSON'
        if data.get('code') not in (0, None):
            return data, False, f"API code={data.get('code')} {data.get('message', '')}"
        return data, True, ''

    def get_info(self) -> tuple:
        return self._get(INFO_URI, {'room_id': self.input_room})

    def get_play_info(self, room_id: str) -> tuple:
        return self._get(PLAY_URI, {
            'room_id': room_id,
            'protocol': '0,1',
            'format': '0,1,2',
            'codec': '0,1',
            'qn': self.qn,
            'platform': 'web',
            'ptype': 8,
        })

    @staticmethod
    def _iter_streams(playurl: dict):
        """遍历 playurl 结构，产出 (protocol, format, codec_name, qn, url)。

        注意拼接三段：url_info[0].host + codec.base_url + url_info[0].extra
        """
        for stream in playurl.get('stream', []):
            proto = stream.get('protocol_name', '')
            for fmt in stream.get('format', []):
                fname = fmt.get('format_name', '')
                for codec in fmt.get('codec', []):
                    infos = codec.get('url_info') or []
                    base = codec.get('base_url') or codec.get('url') or ''
                    for info in infos:
                        host = info.get('host', '')
                        extra = info.get('extra', '')
                        yield (proto, fname, codec.get('codec_name', ''),
                               codec.get('current_qn'), host + base + extra)

    @classmethod
    def parse_streams(cls, playurl: dict) -> tuple:
        """返回 (flv_urls, hls_url)。flv_urls 按 '协议/格式/画质' 命名。"""
        flv_urls, hls_url = {}, ''
        for proto, fname, codec_name, qn, url in cls._iter_streams(playurl):
            if not url:
                continue
            if fname == 'flv' or proto == 'http_stream':
                flv_urls[f'qn{qn}'] = url
            elif fname in ('ts', 'fmp4') or proto == 'http_hls':
                if not hls_url or fname == 'ts':
                    hls_url = url
        return flv_urls, hls_url

    def fetch(self) -> RoomState:
        data, ok, msg = self.get_info()
        if not ok:
            return RoomState(web_rid=self.input_room, ok=False, platform='bilibili', msg=msg, raw=data)

        info = data.get('data') or {}
        room_id = str(info.get('room_id') or self.input_room)
        self._real_room = room_id
        status = int(info.get('live_status') or 0)
        live = (status == STATUS_LIVE)

        state = RoomState(
            web_rid=self.input_room,
            ok=True,
            platform='bilibili',
            live=live,
            status=status,
            room_id=room_id,
            title=info.get('title') or '',
            nickname=info.get('uname') or '',
            user_count=int(info.get('online') or 0),
            raw=info,
        )
        if not live:
            return state

        pdata, pok, pmsg = self.get_play_info(room_id)
        if not pok:
            state.msg = f'取流失败: {pmsg}'
            return state
        playurl = ((pdata.get('data') or {}).get('playurl_info') or {}).get('playurl')
        if not playurl:
            state.msg = 'playurl 为空（可能需 Cookie 或直播间加密）'
            return state
        state.flv_urls, state.hls_url = self.parse_streams(playurl)
        if not state.flv_urls and not state.hls_url:
            state.msg = '未解析出可用流地址'
        return state


def parse_room_id(text: str) -> str:
    """从 B站直播间链接里提取房间号。"""
    text = (text or '').strip()
    for pattern in (r'live\.bilibili\.com/(\d+)', r'room_id=(\d+)', r'roomid=(\d+)'):
        m = re.search(pattern, text)
        if m:
            return m.group(1)
    if re.fullmatch(r'\d+', text):
        return text
    return ''
