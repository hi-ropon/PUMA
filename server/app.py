"""
app.py
=================================
Flask + SocketIO サーバー。

"""

import eventlet
eventlet.monkey_patch()          # eventlet を最優先でパッチ
from eventlet import tpool       # PLC Diagnostics 内でも使用

import os
from dotenv import load_dotenv
from flask import Flask, jsonify, request, send_from_directory
from flask_socketio import SocketIO, emit
from flask_login import (
    LoginManager,
    UserMixin,
    login_user,
    logout_user,
    login_required,
    current_user,
)

import plc_agent as plc
import comments_search as hs

# ──────────────────── ユーザークラス ────────────────────
class User(UserMixin):
    def __init__(self, username: str):
        self.id = username


# ──────────────────── Flask アプリ生成 ────────────────────
def create_app():
    load_dotenv()

    app = Flask(__name__, static_folder="../client", static_url_path="")
    app.secret_key = os.getenv("SECRET_KEY", "change-me")

    socketio = SocketIO(app, cors_allowed_origins="*", async_mode="eventlet")

    # ───── 認証設定 ─────
    login_manager = LoginManager(app)
    login_manager.login_view = "login"

    USERNAME = os.getenv("APP_USER", "user")
    PASSWORD = os.getenv("APP_PASSWORD", "pass")

    @login_manager.user_loader
    def load_user(user_id: str):
        if user_id == USERNAME:
            return User(user_id)
        return None

    # ───── PLC / Gateway 接続設定 ─────
    GATEWAY_URL = os.getenv("GATEWAY_URL", "http://127.0.0.1:8001/api/read")
    PLC_IP = os.getenv("PLC_IP", "127.0.0.1")
    PLC_PORT = os.getenv("PLC_PORT", "5511")

    # ───── コメント & プログラム事前ロード ─────
    comment_path = os.getenv("COMMENT_CSV")
    if comment_path and os.path.exists(comment_path):
        with open(comment_path, "rb") as f:
            hs.load_comments(plc.decode_bytes(f.read()))

    program_paths = os.getenv("PROGRAM_CSVS")      # ; 区切り
    if program_paths:
        for path in program_paths.split(os.pathsep):
            if not os.path.exists(path):
                continue
            with open(path, "rb") as f:
                plc.PROGRAMS[os.path.basename(path)] = plc.load_program(
                    plc.decode_bytes(f.read())
                )

    # ───── WebSocket ハンドラ ─────
    @socketio.on("chat")
    def handle_chat(json_msg):
        if not current_user.is_authenticated:
            emit("reply", {"text": "ログインが必要です"})
            return

        text = json_msg.get("text") or ""
        if not text:
            device = (json_msg.get("device") or "D").upper()
            addr = int(json_msg.get("addr", 100))
            text = f"{device}{addr} の状況を調べてください"

        try:
            answer = plc.run_analysis(text,
                                      base_url=GATEWAY_URL,
                                      ip=PLC_IP,
                                      port=PLC_PORT)
        except Exception as ex:
            answer = f"AI 呼び出しでエラーが発生しました: {ex}"

        emit("reply", {"text": answer})

    # ───── REST エンドポイント ─────
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
        files = request.files.getlist("files")
        if not files:
            return jsonify({"result": "ng", "error": "no file"}), 400

        for f in files:
            plc.PROGRAMS[f.filename] = plc.load_program(
                plc.decode_bytes(f.stream.read())
            )

        return jsonify({"result": "ok", "count": len(files)})

    @app.get("/api/programs")
    @login_required
    def list_programs():
        return jsonify({"programs": list(plc.PROGRAMS.keys())})

    # ───── SPA 配信用 ─────
    @app.route("/", defaults={"path": ""})
    @app.route("/<path:path>")
    def serve(path):
        full = os.path.join(app.static_folder, path)
        if path and os.path.exists(full):
            return send_from_directory(app.static_folder, path)

        html = "index.html" if current_user.is_authenticated else "login.html"
        return send_from_directory(app.static_folder, html)

    return app, socketio


# ──────────────────── エントリポイント ────────────────────
if __name__ == "__main__":
    app, socketio = create_app()
    socketio.run(app, host="127.0.0.1", port=8000, debug=True)
