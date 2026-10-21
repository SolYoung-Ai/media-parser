"""
媒体代理下载端点（ClipText 专用补丁）
浏览器无法跨域读取平台 CDN 媒体（无 CORS），且部分直链需要 Referer 校验。
本端点由后端服务端拉取媒体并流式返回，附带 attachment 下载头。

安全约束（防止被滥用为公共代理 / SSRF）：
- 仅允许 HTTPS，且主机必须以 MEDIA_PROXY_HOSTS（后缀白名单）中的条目结尾；
- 解析后的 IP 必须是公网地址（拒绝内网/回环/链路本地/保留地址）；
- 流式转发，限制最大体积与超时；文件名经过净化。
"""
import os
import socket
from urllib.parse import urlparse, quote

import requests
from flask import Blueprint, Response, abort, request

proxy_bp = Blueprint('media_proxy', __name__)

_DEFAULT_HOSTS = (
    "amemv.com,douyinstatic.com,douyinpic.com,douyinvod.com,douyin.com,"
    "v.douyin.com,xhscdn.com,xhscdn.net,xhscdn.cn,qpic.cn,qnimg.cn,"
    "wxsnsdy.com,weixin.qq.com,tc.qq.com,gtimg.com,qq.com,"
    "bilibili.com,akamaized.net,bilivideo.com"
)
_MAX_MB = int(os.getenv('MEDIA_PROXY_MAX_MB', '1024'))
_TIMEOUT = float(os.getenv('MEDIA_PROXY_TIMEOUT', '90'))
_DOWNLOAD_CHUNK = 64 * 1024

_USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")


def _is_public_ip(ip: str) -> bool:
    import ipaddress
    try:
        addr = ipaddress.ip_address(ip)
    except Exception:
        return False
    return not (addr.is_private or addr.is_loopback or addr.is_link_local
                or addr.is_multicast or addr.is_unspecified or addr.is_reserved)


def _host_allowed(host: str) -> bool:
    if not host:
        return False
    host = host.lower().rstrip('.')
    allowed = [h.strip().lower().lstrip('.') for h in os.getenv('MEDIA_PROXY_HOSTS', _DEFAULT_HOSTS).split(',') if h.strip()]
    return any(host == a or host.endswith('.' + a) for a in allowed)


def _sanitize_name(name: str, url: str) -> str:
    if not name:
        try:
            path = urlparse(url).path
            name = path.rsplit('/', 1)[-1]
        except Exception:
            name = ''
    # 兼容未百分号编码的客户端：UTF-8 字节被按 latin-1 解码时产生乱码，尝试恢复
    try:
        recovered = name.encode('latin-1').decode('utf-8')
        if recovered != name and all(ord(c) < 128 or c.isalnum() for c in recovered):
            name = recovered
    except (UnicodeEncodeError, UnicodeDecodeError):
        pass
    name = (name or 'media').replace('\\', '_').replace('/', '_')
    name = ''.join(c if c.isalnum() or c in '._-()' else '_' for c in name)[:120].strip('._')
    if not name:
        name = 'media'
    return name


def _guess_referer(host: str) -> str:
    host = host.lower()
    if 'amemv.com' in host or 'douyin' in host:
        return 'https://www.douyin.com/'
    if 'xhscdn' in host:
        return 'https://www.xiaohongshu.com/'
    if 'wxsnsdy' in host or 'weixin' in host or 'qq.com' in host:
        return 'https://channels.weixin.qq.com/'
    if 'bilibili' in host or 'bilivideo' in host:
        return 'https://www.bilibili.com/'
    return 'https://www.baidu.com/'


@proxy_bp.route('/v1/download')
def download():
    url = request.args.get('url', '').strip()
    if not url:
        abort(400, description='缺少 url 参数')
    try:
        parsed = urlparse(url)
    except Exception:
        abort(400, description='url 无法解析')
    if parsed.scheme not in ('https', 'http'):
        abort(400, description='仅支持 http/https 媒体链接')
    host = (parsed.hostname or '').lower()
    if not _host_allowed(host):
        abort(403, description='该媒体域名不在代理白名单内，已拒绝转发')
    # SSRF 防护：仅允许解析到公网 IP
    try:
        resolved = socket.getaddrinfo(host, 443 if parsed.scheme == 'https' else 80, type=socket.SOCK_STREAM)
        candidates = {r[4][0] for r in resolved}
        if not candidates or not all(_is_public_ip(ip) for ip in candidates):
            abort(403, description='媒体服务器解析到非公网地址，已拒绝转发')
    except socket.gaierror:
        abort(502, description='无法解析媒体服务器域名')

    name = _sanitize_name(request.args.get('name', ''), url)
    headers = {
        'User-Agent': _USER_AGENT,
        'Referer': _guess_referer(host),
        'Accept': '*/*',
    }
    max_bytes = _MAX_MB * 1024 * 1024
    try:
        upstream = requests.get(url, headers=headers, stream=True,
                                timeout=(15, _TIMEOUT), allow_redirects=True)
    except requests.exceptions.Timeout:
        abort(504, description='媒体源响应超时')
    except requests.exceptions.RequestException as exc:
        abort(502, description=f'媒体源请求失败: {exc.__class__.__name__}')
    if upstream.status_code != 200:
        abort(502, description=f'媒体源返回 HTTP {upstream.status_code}')

    content_type = upstream.headers.get('Content-Type', 'application/octet-stream')
    encoded = quote(name)
    # HTTP 头只能含 latin-1 字符：普通 filename 必须 ASCII 化，中文名走 filename*（RFC 5987）
    plain = ''.join(c if ord(c) < 128 else '_' for c in name) or 'media'
    disposition = f"attachment; filename=\"{plain}\"; filename*=UTF-8''{encoded}"

    bytes_sent = 0

    def generate():
        nonlocal bytes_sent
        try:
            for chunk in upstream.iter_content(chunk_size=_DOWNLOAD_CHUNK):
                if not chunk:
                    continue
                bytes_sent += len(chunk)
                if bytes_sent > max_bytes:
                    break
                yield chunk
        finally:
            upstream.close()

    return Response(
        generate(),
        status=200,
        headers={
            'Content-Type': content_type,
            'Content-Disposition': disposition,
            'X-Media-Source': host,
            'Cache-Control': 'no-store',
            'Access-Control-Expose-Headers': 'Content-Disposition',
        },
        direct_passthrough=True,
    )


def register_proxy_routes(app):
    app.register_blueprint(proxy_bp, url_prefix='/api')
    return app
