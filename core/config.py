"""
抖音直播模块的请求配置。

集中管理 UA、公共请求头、公共参数，签名算法在 core/signing.py。
抖音风控策略会变，改动优先收敛到本文件与 signing.py。
"""
import os
import random
import string
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / '.env')
except Exception:
    pass

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / 'data'
LOG_DIR = BASE_DIR / 'logs'
for _d in (DATA_DIR, LOG_DIR):
    _d.mkdir(exist_ok=True)

# 直播走独立域名，与 www.douyin.com 的接口不通用
LIVE_HOST = 'https://live.douyin.com'
WWW_HOST = 'https://www.douyin.com'

UA = os.getenv(
    'DOUYIN_UA',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
    '(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
)

# 从 .env 注入，用于风控较强时；留空则自动取临时 Cookie
DOUYIN_COOKIE = os.getenv('DOUYIN_COOKIE', '')

COMMON_HEADERS = {
    'User-Agent': UA,
    'Accept': 'application/json, text/plain, */*',
    'Accept-Language': 'zh-CN,zh;q=0.9',
    'Referer': LIVE_HOST + '/',
    'Sec-Fetch-Dest': 'empty',
    'Sec-Fetch-Mode': 'cors',
    'Sec-Fetch-Site': 'same-origin',
}

COMMON_PARAMS = {
    'aid': 6383,
    'app_name': 'douyin_web',
    'live_id': 1,
    'device_platform': 'web',
    'language': 'zh-CN',
    'browser_language': 'zh-CN',
    'browser_platform': 'Win32',
    'browser_name': 'Chrome',
    'browser_version': '124.0.0.0',
    'cookie_enabled': 'true',
    'screen_width': 1536,
    'screen_height': 864,
    'is_outside': 'false',
    'enter_source': '',
    'enter_from': 'web_search',
}


def random_ms_token() -> str:
    """生成随机 msToken，规避简单频次校验。"""
    return ''.join(random.choices(string.ascii_letters + string.digits, k=107))


def random_verify_fp() -> str:
    return 'verify_' + ''.join(random.choices(string.ascii_lowercase + string.digits, k=16))
