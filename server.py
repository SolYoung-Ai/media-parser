"""
ClipText 部署入口（CORS + 媒体代理 + 本地 Whisper ASR）
用法：
  python server.py                        # 本地开发（http://0.0.0.0:8051）
  gunicorn -b 0.0.0.0:8051 server:app     # 生产（Render / Docker）
环境变量：
  API_ONLY=true                  # 纯微服务模式，免鉴权（默认即为 true）
  CORS_ALLOW_ORIGINS=*           # CORS 放行来源
  TRUST_PROXY_HEADERS=true
  ASR_MODEL=base                 # faster-whisper 模型（base/small/medium...）
  ASR_LANGUAGE=zh                # 默认中文
"""
import os

# 默认以纯 API 微服务模式运行：ClipText 前端直连，请求无需 API Key。
# 上游默认会给 /api/v1/parse 加鉴权（返回 401 API_KEY_REQUIRED），
# 而前端没有配置 Key 的入口，因此这里默认关闭鉴权与配套的门户页面。
# 需要开启门户/鉴权时，显式设置环境变量 API_ONLY=false。
os.environ.setdefault('API_ONLY', 'true')

from app import create_app
from cors_patch import apply_cors
from media_proxy import register_proxy_routes
from asr import register_asr_routes

app = create_app()
# ASR 上传需要更大的请求体（上游默认仅够 JSON）
app.config['MAX_CONTENT_LENGTH'] = 100 * 1024 * 1024  # 100MB
apply_cors(app)
register_proxy_routes(app)
register_asr_routes(app)

if __name__ == "__main__":
    port = int(os.getenv("PORT", "8051"))
    app.run(host="0.0.0.0", port=port)
