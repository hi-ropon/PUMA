import os
import csv
import io
import json
import openai                                     # グローバル参照用
from dotenv import load_dotenv
from openai import OpenAI
import eventlet
eventlet.monkey_patch()

from flask import Flask, jsonify, request, send_from_directory
from flask_socketio import SocketIO, emit
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
import requests

try:
    from openai_agents import Agent, tool
except Exception:  # pragma: no cover - optional dependency
    Agent = None
    def tool(func):
        return func

load_dotenv()
openai.api_key = os.getenv("OPENAI_API_KEY")
client = OpenAI(api_key=openai.api_key)

COMMENTS: dict[str, str] = {}
PROGRAMS: dict[str, dict] = {}

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
    sample = stream.read(1024)
    stream.seek(0)
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",\t")
    except csv.Error:
        dialect = csv.excel_tab
    reader = csv.reader(stream, dialect)
    for row in reader:
        if len(row) < 2:
            continue
        key = row[0].strip().strip('"')
        val = row[1].strip().strip('"')
        if not key or key.lower() in ("test", "\ufefftest", "デバイス名"):
            continue
        COMMENTS[key] = val

def load_program(stream: io.TextIOBase) -> dict:
    """Load a PLC program CSV into a structured dictionary."""
    sample = stream.read(1024)
    stream.seek(0)
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",\t")
    except csv.Error:
        dialect = csv.excel_tab
    reader = list(csv.reader(stream, dialect))
    if not reader:
        return {}
    project = reader[0][0].strip().strip('"') if reader[0] else ""
    model = ""
    if len(reader) > 1 and len(reader[1]) > 1:
        model = reader[1][1].strip().strip('"')
    headers = reader[2] if len(reader) > 2 else []
    rows = reader[3:] if len(reader) > 3 else []
    return {"project": project, "model": model, "headers": headers, "rows": rows}

class User(UserMixin):
    def __init__(self, username: str):
        self.id = username


def search_program(device: str, addr: int) -> list[str]:
    """Return program lines referencing the given device."""
    target = f"{device}{addr}"
    results: list[str] = []
    for prog in PROGRAMS.values():
        headers = prog.get("headers", [])
        rows = prog.get("rows", [])
        if not rows or "I/O(デバイス)" not in headers:
            continue
        io_idx = headers.index("I/O(デバイス)")
        step_idx = headers.index("ステップ番号") if "ステップ番号" in headers else None
        inst_idx = headers.index("命令") if "命令" in headers else None
        note_idx = headers.index("ノート") if "ノート" in headers else None
        for row in rows:
            if len(row) <= io_idx:
                continue
            if row[io_idx].strip().strip('"') != target:
                continue
            parts = []
            if step_idx is not None and len(row) > step_idx and row[step_idx]:
                parts.append(f"ステップ{row[step_idx]}")
            if inst_idx is not None and len(row) > inst_idx and row[inst_idx]:
                parts.append(row[inst_idx])
            if note_idx is not None and len(row) > note_idx and row[note_idx]:
                parts.append(f"({row[note_idx]})")
            if parts:
                results.append(" ".join(parts))
    return results


def read_device_values(device: str, addr: int, length: int, *, base_url: str, ip: str, port: str) -> list[int]:
    """Read device values via gateway."""
    res = requests.get(
        f"{base_url}/{device}/{addr}/{length}",
        params={"ip": ip, "port": port},
    )
    res.raise_for_status()
    return res.json()["values"]


def run_analysis(device: str, addr: int, *, base_url: str, ip: str, port: str) -> str:
    """Run autonomous analysis using OpenAI agents if available."""

    @tool
    def read_values(dev: str, address: int, length: int = 1) -> str:
        vals = read_device_values(dev, address, length, base_url=base_url, ip=ip, port=port)
        return ",".join(str(v) for v in vals)

    @tool
    def program_lines(dev: str, address: int) -> str:
        lines = search_program(dev, address)
        return "\n".join(lines) if lines else ""

    if Agent:
        agent = Agent(model="gpt-4o-mini", tools=[read_values, program_lines])
        question = (
            f"{device}{addr} に関する不具合の原因を特定してください。"\
            "必要に応じてツールを使い、原因が分かったら ANSWER: で始めてください。"
        )
        return agent.run(question)
    else:
        vals = read_values(device, addr)
        lines = program_lines(device, addr)
        prompt = (
            "以下の読み取り値とプログラム抜粋から原因を推測してください。\n"+
            f"値: {vals}\n"+
            f"プログラム:\n{lines}"
        )
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": "あなたは PLC と生産ライン制御の専門家です。"},
                {"role": "user", "content": prompt},
            ],
            temperature=0.2,
        )
        return resp.choices[0].message.content.strip()


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

    program_paths = os.getenv("PROGRAM_CSVS")
    if program_paths:
        for path in program_paths.split(os.pathsep):
            if not os.path.exists(path):
                continue
            with open(path, "rb") as f:
                PROGRAMS[os.path.basename(path)] = load_program(decode_bytes(f.read()))

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

        try:
            answer = run_analysis(device, addr, base_url=GATEWAY_URL, ip=PLC_IP, port=PLC_PORT)
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

    @app.post("/api/programs")
    @login_required
    def upload_programs():
        files = request.files.getlist("files")
        if not files:
            file = request.files.get("file")
            if file:
                files = [file]
        if not files:
            return jsonify({"result": "ng", "error": "no file"}), 400
        count = 0
        for f in files:
            data = f.stream.read()
            stream = decode_bytes(data)
            PROGRAMS[f.filename] = load_program(stream)
            count += 1
        return jsonify({"result": "ok", "count": count})

    @app.get("/api/programs")
    @login_required
    def list_programs():
        return jsonify({"programs": list(PROGRAMS.keys())})

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

