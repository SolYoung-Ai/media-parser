import os
from app import create_app
from cors_patch import apply_cors
from media_proxy import register_proxy_routes

app = create_app()
apply_cors(app)
register_proxy_routes(app)

if __name__ == "__main__":
    port = int(os.getenv("PORT", "8051"))
    app.run(host="0.0.0.0", port=port)
