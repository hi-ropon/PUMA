import os
import csv
import io
import openai                                     # グローバル参照用
from dotenv import load_dotenv
from openai import OpenAI
import eventlet
eventlet.monkey_patch()

from flask import Flask, jsonify, request, send_from_directory
from flask_socketio import SocketIO, emit
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
import requests

load_dotenv()
openai.api_key = os.getenv("OPENAI_API_KEY")
client = OpenAI(api_key=openai.api_key)

COMMENTS: dict[str, str] = {}

def decode_bytes(data: bytes) -> io.StringIO:
    """Decode uploaded bytes with several fallback encodings."""
    for enc in ("utf-8-sig", "utf-16", "shift_jis", "cp932"):
        try:
            return io.StringIO(data.decode(enc))
        except UnicodeDecodeError:
            continue
    # If all attempts fail, decode with replacement characters
    return io.StringIO(data.decode("utf-8", errors="replace"))

def load_comments(stream: io.TextIOBase) -> None:
    COMMENTS.clear()
    reader = csv.reader(stream)
    for row in reader:
        if len(row) < 2:
            continue
        key = row[0].strip().strip('"')
        val = row[1].strip().strip('"')
        if not key or key.lower() in ("test", "\ufefftest", "デバイス名"):
            continue
        COMMENTS[key] = val

class User(UserMixin):
    def __init__(self, username: str):
        self.id = username


def create_app():
    app = Flask(__name__, static_folder="../client", static_url_path="")
    app.secret_key = os.getenv("SECRET_KEY", "change-me")
    socketio = SocketIO(app, cors_allowed_origins="*", async_mode="eventlet")
    login_manager = LoginManager(app)
    login_manager.login_view = "login"
    GATEWAY_URL = os.getenv("GATEWAY_URL", "http://127.0.0.1:8001/api/read")
    PLC_IP = os.getenv("PLC_IP", "127.0.0.1")
    PLC_PORT = os.getenv("PLC_PORT", "5511")

    comment_path = os.getenv("COMMENT_CSV")
    if comment_path and os.path.exists(comment_path):
        with open(comment_path, "rb") as f:
            load_comments(decode_bytes(f.read()))

    USERNAME = os.getenv("APP_USER", "user")
    PASSWORD = os.getenv("APP_PASSWORD", "pass")

    @login_manager.user_loader
    def load_user(user_id: str):
        if user_id == USERNAME:
            return User(user_id)
        return None

    @socketio.on("chat")
    def handle_chat(json_msg):
        if not current_user.is_authenticated:
            emit("reply", {"text": "ログインが必要です"})
            return

        device = (json_msg.get("device") or "D").upper()
        addr = int(json_msg.get("addr", 100))
        gw_res = requests.get(
            f"{GATEWAY_URL}/{device}/{addr}/5",
            params={"ip": PLC_IP, "port": PLC_PORT},
        )
        gw_res.raise_for_status()
        values = gw_res.json()["values"]

        lines = []
        for i, v in enumerate(values):
            key = f"{device}{addr + i}"
            comment = COMMENTS.get(key)
            if comment:
                lines.append(f"{key} = {v} ({comment})")
            else:
                lines.append(f"{key} = {v}")

        prompt = (
            f"以下は PLC {device} の読み取り結果です。\n" + "\n".join(lines)
            + f"\n\nユーザーからの問い: 『{device}{addr} の値から何を推測できますか？』"
        )

        try:
            resp = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": "あなたは PLC と生産ライン制御の専門家です。"},
                    {"role": "user",   "content": prompt}
                ],
                temperature=0.2,
            )
            answer = resp.choices[0].message.content.strip()
        except Exception as ex:
            answer = f"AI 呼び出しでエラーが発生しました: {ex}"

        emit("reply", {"text": answer})

    @app.post("/login")
    def login():
        data = request.get_json(silent=True) or request.form
        username = data.get("username")
        password = data.get("password")
        if username == USERNAME and password == PASSWORD:
            login_user(User(username))
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
        data = file.stream.read()
        stream = decode_bytes(data)
        load_comments(stream)
        return jsonify({"result": "ok", "count": len(COMMENTS)})

    @app.route("/", defaults={"path": ""})
    @app.route("/<path:path>")
    def serve(path):
        if path and os.path.exists(app.static_folder + "/" + path):
            return send_from_directory(app.static_folder, path)
        if current_user.is_authenticated:
            return send_from_directory(app.static_folder, "index.html")
        return send_from_directory(app.static_folder, "login.html")

    return app, socketio

if __name__ == "__main__":
    app, socketio = create_app()
    socketio.run(app, host="127.0.0.1", port=8000, debug=True)

