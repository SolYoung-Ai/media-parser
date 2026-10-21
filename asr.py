"""
本地语音识别（Whisper）端点（ClipText 专用补丁）
零外部 API Key、零费用：faster-whisper 推理。

两个端点：
1. POST /v1/audio/transcriptions   OpenAI 兼容（multipart file 上传），返回 {"text": "..."}
2. POST /api/v1/asr-from-url        直接对解析结果里的视频 URL 转写，返回 text + segments

模型选择：环境变量 ASR_MODEL（默认 tiny，省内存适合免费层）；首次使用自动从 HuggingFace 下载。
SSRF 防护：复用 media_proxy 的域名白名单（只允许平台媒体 CDN）。
"""
import os
import tempfile

import requests
from flask import Blueprint, abort, jsonify, request

try:
    import av as _av
    import av.container.core as _avcore

    _orig_av_open = _avcore.open

    def _compat_av_open(file, mode="r", **kwargs):
        kwargs.pop("metadata_errors", None)
        return _orig_av_open(file, mode=mode, **kwargs)

    _avcore.open = _compat_av_open
    _av.open = _compat_av_open
except Exception:
    pass

asr_bp = Blueprint('asr', __name__)

_MODEL_NAME = os.getenv('ASR_MODEL', 'tiny')
_MODEL = None


def _get_model():
    global _MODEL
    if _MODEL is None:
        from faster_whisper import WhisperModel
        _MODEL = WhisperModel(_MODEL_NAME, device='cpu', compute_type='int8')
    return _MODEL


def _whisper_transcribe(audio_path):
    model = _get_model()
    segments, info = model.transcribe(
        audio_path,
        language=os.getenv('ASR_LANGUAGE', 'zh'),
        beam_size=5,
        vad_filter=True,
        vad_parameters=dict(min_silence_duration_ms=500),
    )
    seg_list = []
    text_parts = []
    for seg in segments:
        seg_list.append({'start': round(seg.start, 2), 'end': round(seg.end, 2), 'text': seg.text.strip()})
        text_parts.append(seg.text.strip())
    return {'text': ''.join(text_parts), 'segments': seg_list, 'language': info.language}


@asr_bp.route('/v1/audio/transcriptions', methods=['POST'])
def openai_transcriptions():
    f = request.files.get('file')
    if f is None:
        abort(400, description='缺少 file 字段')
    suffix = os.path.splitext(f.filename or 'audio.mp4')[1] or '.mp4'
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    try:
        f.save(tmp)
        tmp.close()
        result = _whisper_transcribe(tmp.name)
        return jsonify({'text': result['text'], 'segments': result['segments']})
    except Exception as exc:
        print(f'ASR transcription failed: {exc.__class__.__name__}: {exc}')
        abort(500, description='转写失败：' + str(exc))
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass


@asr_bp.route('/v1/models', methods=['GET'])
def openai_models():
    return jsonify({'data': [{'id': _MODEL_NAME, 'object': 'model', 'owned_by': 'local-whisper'}], 'object': 'list'})


@asr_bp.route('/api/v1/asr-from-url', methods=['POST', 'GET'])
def asr_from_url():
    from media_proxy import _host_allowed, _is_public_ip
    import socket as _socket
    from urllib.parse import urlparse

    if request.method == 'POST' and request.is_json:
        data = request.get_json(silent=True) or {}
        url = (data.get('url') or '').strip()
    else:
        url = (request.args.get('url') or '').strip()
    if not url:
        abort(400, description='缺少 url 参数')
    parsed = urlparse(url)
    if parsed.scheme not in ('http', 'https'):
        abort(400, description='仅支持 http/https')
    host = (parsed.hostname or '').lower()
    if not _host_allowed(host):
        abort(403, description='该媒体域名不在白名单内，已拒绝')
    try:
        resolved = _socket.getaddrinfo(host, 443 if parsed.scheme == 'https' else 80, type=_socket.SOCK_STREAM)
        candidates = {r[4][0] for r in resolved}
        if not candidates or not all(_is_public_ip(ip) for ip in candidates):
            abort(403, description='非公网地址，已拒绝')
    except _socket.gaierror:
        abort(502, description='无法解析媒体域名')

    tmp = tempfile.NamedTemporaryFile(delete=False, suffix='.mp4')
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36',
            'Referer': 'https://www.douyin.com/',
        }
        resp = requests.get(url, headers=headers, stream=True, timeout=(15, 60))
        if resp.status_code != 200:
            abort(502, description=f'媒体源返回 HTTP {resp.status_code}')
        max_bytes = 100 * 1024 * 1024
        wrote = 0
        for chunk in resp.iter_content(chunk_size=64 * 1024):
            wrote += len(chunk)
            if wrote > max_bytes:
                break
            tmp.write(chunk)
        tmp.close()
        result = _whisper_transcribe(tmp.name)
        return jsonify(result)
    except Exception as exc:
        abort(500, description='转写失败：' + str(exc))
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass


def register_asr_routes(app):
    app.register_blueprint(asr_bp)
    return app
