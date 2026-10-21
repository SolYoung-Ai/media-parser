"""
CORS 补丁（ClipText 专用）
media-parser 原生 API 未携带跨域头，浏览器前端（clip.solyoung.top 或本地预览）无法直接调用。
本补丁在 Flask after_request 阶段为所有 API 响应附加 CORS 头。
允许来源通过环境变量 CORS_ALLOW_ORIGINS 控制，默认 *（仅建议用于个人自用实例）。
"""
import os

_DEFAULT_ORIGINS = "*"


def apply_cors(app):
    allowed = os.getenv("CORS_ALLOW_ORIGINS", _DEFAULT_ORIGINS).strip()

    @app.after_request
    def add_cors_headers(response):
        response.headers["Access-Control-Allow-Origin"] = allowed
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization, X-Requested-With"
        response.headers["Access-Control-Max-Age"] = "86400"
        if response.mimetype == "application/json":
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.route("/", methods=["OPTIONS"], defaults={"path": ""})
    @app.route("/<path:path>", methods=["OPTIONS"])
    def cors_preflight(path):
        return "", 204

    return app
