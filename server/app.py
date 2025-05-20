"""
Flask + SocketIO サーバー (複数 PLC プログラム CSV 対応版)
"""
# ──────────────────── インポート ────────────────────
from __future__ import annotations
import eventlet
eventlet.monkey_patch()

import os
import threading
from dotenv import load_dotenv
from flask import Flask, jsonify, request, send_from_directory
from flask_login import (LoginManager, UserMixin, login_user,
                         logout_user, login_required, current_user)
from flask_socketio import SocketIO, emit

import plc_agent as plc
import comments_search as hs

# ──────────────────── 設定 ────────────────────
load_dotenv()
UPLOAD_LIMIT_MB: int = int(os.getenv("MAX_UPLOAD_MB", "25"))
PLC_DRIVE = int(os.getenv("PLC_DRIVE", "4"))

# ──────────────────── Flask 初期化 ────────────────────
def create_app():
    app = Flask(__name__, static_folder="../client", static_url_path="")
    app.config["SECRET_KEY"] = os.getenv("SECRET_KEY", "change-me")
    # アップロード総量制限
    app.config["MAX_CONTENT_LENGTH"] = UPLOAD_LIMIT_MB * 1024 * 1024

    socketio = SocketIO(app, cors_allowed_origins="*", async_mode="eventlet")

    # ───── Login / User 定義 ─────
    class User(UserMixin):
        def __init__(self, username: str):
            self.id = username

    login_manager = LoginManager(app)
    login_manager.login_view = "login"

    USERNAME = os.getenv("APP_USER", "user")
    PASSWORD = os.getenv("APP_PASSWORD", "pass")

    @login_manager.user_loader
    def load_user(user_id: str):                        # noqa: D401
        return User(user_id) if user_id == USERNAME else None

    # ───── グローバルリソース ─────
    program_lock = threading.Lock()

    # ───── 事前ロード (環境変数で複数指定可) ─────
    comment_path = os.getenv("COMMENT_CSV")
    if comment_path and os.path.exists(comment_path):
        with open(comment_path, "rb") as f:
            hs.load_comments(plc.decode_bytes(f.read()))

    program_paths = (os.getenv("PROGRAM_CSVS") or "").split(os.pathsep)
    for path in program_paths:
        if path and os.path.exists(path):
            with open(path, "rb") as f:
                plc.PROGRAMS[os.path.basename(path)] = plc.load_program(
                    plc.decode_bytes(f.read())
                )

    # ────────── WebSocket ──────────
    @socketio.on("chat")
    def handle_chat(json_msg):
        if not current_user.is_authenticated:
            emit("reply", {"text": "ログインが必要です"})
            return

        text: str = (json_msg or {}).get("text", "")
        if not text:
            emit("reply", {"text": "質問が空です"})
            return

        GATEWAY_URL = os.getenv("GATEWAY_URL", "http://127.0.0.1:8001/api/read")
        PLC_IP      = os.getenv("PLC_IP", "127.0.0.1")
        PLC_PORT    = os.getenv("PLC_PORT", "5511")

        answer = plc.run_analysis(
            text,
            base_url=GATEWAY_URL,
            ip=PLC_IP,
            port=PLC_PORT,
        )
        emit("reply", {"text": answer})

    # ────────── REST ──────────
    @app.post("/login")
    def login():
        data = request.get_json(silent=True) or request.form
        if data.get("username") == USERNAME and data.get("password") == PASSWORD:
            login_user(User(USERNAME))
            return jsonify({"result": "ok"})
        return jsonify({"result": "ng"}), 401

    @app.post("/logout")
    @login_required
    def logout():
        logout_user()
        return jsonify({"result": "ok"})

    @app.post("/api/comments")
    @login_required
    def upload_comments():
        file = request.files.get("file")
        if not file:
            return jsonify({"result": "ng", "error": "no file"}), 400

        hs.load_comments(plc.decode_bytes(file.stream.read()))
        return jsonify({"result": "ok", "count": len(hs.COMMENTS)})

    @app.post("/api/programs")
    @login_required
    def upload_programs():
        files = request.files.getlist("files[]")
        if not files:
            return jsonify({"result": "ng", "error": "no file"}), 400

        added = 0
        with program_lock:
            plc.PROGRAMS.clear()
            for f in files:
                plc.PROGRAMS[f.filename] = plc.load_program(
                    plc.decode_bytes(f.stream.read())
                )
                added += 1

        return jsonify({"result": "ok", "count": added})

    @app.get("/api/programs")
    @login_required
    def list_programs():
        return jsonify({"programs": list(plc.PROGRAMS.keys())})
    
    @app.get("/api/fileinfo")
    @login_required
    def file_info():
        drive = int(request.args.get("drive", PLC_DRIVE))
        infos = plc.read_directory_fileinfo(
            drive_no=drive,
            base_url=os.getenv("GATEWAY_FILEINFO_URL", "http://127.0.0.1:8001/api/fileinfo"),
            ip=os.getenv("PLC_IP",   "127.0.0.1"),
            port=os.getenv("PLC_PORT", "5511"),
        )
        return jsonify({"files": infos})

    # SPA 配信 --------------------------------------------------------------
    @app.route("/", defaults={"path": ""})
    @app.route("/<path:path>")
    def serve(path: str):
        full = os.path.join(app.static_folder, path)
        if path and os.path.exists(full):
            return send_from_directory(app.static_folder, path)

        html = "index.html" if current_user.is_authenticated else "login.html"
        return send_from_directory(app.static_folder, html)

    return app, socketio


# ──────────────────── main ────────────────────
if __name__ == "__main__":
    application, sio = create_app()
    sio.run(application, host="127.0.0.1", port=8000, debug=True)
