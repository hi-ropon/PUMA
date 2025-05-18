import os
import csv
import io
import re
import asyncio
import httpx
import openai
from dotenv import load_dotenv
from openai import OpenAI
import requests
from eventlet import tpool
from agents import Agent, Runner, function_tool as tool
from agents.exceptions import MaxTurnsExceeded
import hybrid_search as hs

load_dotenv()
openai.api_key = os.getenv("OPENAI_API_KEY")
client = OpenAI(api_key=openai.api_key, timeout=httpx.Timeout(60.0, read=None))

PROGRAMS: dict[str, dict] = {}


def decode_bytes(data: bytes) -> io.StringIO:
    """Decode uploaded bytes with several fallback encodings."""
    for enc in ("utf-8-sig", "utf-16", "shift_jis", "cp932"):
        try:
            return io.StringIO(data.decode(enc))
        except UnicodeDecodeError:
            continue
    return io.StringIO(data.decode("utf-8", errors="replace"))


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


def search_program(device: str, addr: int, context: int = 0) -> list[str]:
    """Return program lines referencing the given device with optional context."""
    target = f"{device}{addr}"
    results: list[str] = []
    for prog in PROGRAMS.values():
        headers = prog.get("headers", [])
        rows = prog.get("rows", [])
        if not rows or "I/O(\u30c7\u30d0\u30a4\u30b9)" not in headers:
            continue
        io_idx = headers.index("I/O(\u30c7\u30d0\u30a4\u30b9)")
        step_idx = headers.index("\u30b9\u30c6\u30c3\u30d7\u756a\u53f7") if "\u30b9\u30c6\u30c3\u30d7\u756a\u53f7" in headers else None
        inst_idx = headers.index("\u547d\u4ee4") if "\u547d\u4ee4" in headers else None
        note_idx = headers.index("\u30ce\u30fc\u30c8") if "\u30ce\u30fc\u30c8" in headers else None
        for i, row in enumerate(rows):
            if len(row) <= io_idx:
                continue
            if row[io_idx].strip().strip('"') != target:
                continue
            start = max(0, i - context)
            for j in range(start, i + 1):
                ctx = rows[j]
                if len(ctx) <= io_idx:
                    continue
                parts = []
                if step_idx is not None and len(ctx) > step_idx and ctx[step_idx]:
                    parts.append(f"\u30b9\u30c6\u30c3\u30d7{ctx[step_idx]}")
                if inst_idx is not None and len(ctx) > inst_idx and ctx[inst_idx]:
                    parts.append(ctx[inst_idx])
                if len(ctx) > io_idx and ctx[io_idx]:
                    parts.append(ctx[io_idx])
                if note_idx is not None and len(ctx) > note_idx and ctx[note_idx]:
                    parts.append(f"({ctx[note_idx]})")
                if parts:
                    line = " ".join(parts)
                    if line not in results:
                        results.append(line)
    return results


def related_devices(device: str, addr: int, context: int = 10) -> list[str]:
    """Return devices appearing near the target in the program."""
    pattern = re.compile(r"[XYMDTS]\d+")
    lines = search_program(device, addr, context=context)
    deps: set[str] = set()
    for line in lines:
        for m in pattern.findall(line):
            if m != f"{device}{addr}":
                deps.add(m)
    return sorted(deps)


def read_device_values(device: str, addr: int, length: int, *, base_url: str, ip: str, port: str) -> list[int]:
    """Read device values via gateway."""
    res = requests.get(
        f"{base_url}/{device}/{addr}/{length}",
        params={"ip": ip, "port": port},
    )
    res.raise_for_status()
    return res.json()["values"]


def _run_diagnostics(device: str, addr: int, *, base_url: str, ip: str, port: str) -> str:
    """Run autonomous analysis using OpenAI agents."""

    @tool
    def read_values(dev: str, address: int, length: int) -> str:
        """PLC \u30c7\u30d0\u30a4\u30b9\u306e\u5024\u3092\u8aad\u307f\u53d6\u308a\u307e\u3059\u3002"""
        length = length or 1
        vals = read_device_values(
            dev, address, length,
            base_url=base_url, ip=ip, port=port
        )
        return ",".join(str(v) for v in vals)

    @tool
    def program_lines(dev: str, address: int) -> str:
        """\u30c7\u30d0\u30a4\u30b9\u5468\u8fba\u306e\u30d7\u30ed\u30b0\u30e9\u30e0\u884c\u3092\u8fd4\u3057\u307e\u3059\u3002"""
        return "\n".join(search_program(dev, address, context=2))

    @tool
    def related(dev: str, address: int) -> str:
        """\u5bfe\u8c61\u3068\u4e00\u7dd2\u306b\u4f7f\u308f\u308c\u3066\u3044\u308b\u30c7\u30d0\u30a4\u30b9\u4e00\u89a7\u3002"""
        return ",".join(related_devices(dev, address))

    @tool
    def comment(dev: str, address: int) -> str:
        """\u30c7\u30d0\u30a4\u30b9\u306b\u3064\u3051\u305f\u30b3\u30e1\u30f3\u30c8\u3092\u8fd4\u3057\u307e\u3059\u3002"""
        return hs.get_comment(f"{dev}{address}")

    tools = [read_values, program_lines, related, comment]

    agent = Agent(
        name="PLC-Diagnostics",
        instructions=(
            "\u3042\u306a\u305f\u306f\u4e09\u83f1\u30b7\u30fc\u30b1\u30f3\u30b5D/M/X/Y\u30c7\u30d0\u30a4\u30b9\u306e\u4e0d\u5177\u5408\u3092\u8abf\u67fb\u3059\u308b\u30a8\u30fc\u30b8\u30a7\u30f3\u30c8\u3067\u3059\u3002",
            "\u5f97\u3089\u308c\u305f\u5024\u3084\u30b3\u30e1\u30f3\u30c8\u3001\u30d7\u30ed\u30b0\u30e9\u30e0\u306e\u547d\u4ee4\u304b\u3089\u539f\u56e0\u3092\u63a8\u8ad6\u3057\u3001",
            "\u6700\u5f8c\u306b\u300eANSWER: ....\u300f\u5f62\u5f0f\u3067\u65e5\u672c\u8a9e\u8981\u7d04\u3092\u8fd4\u7b54\u3057\u3066\u304f\u3060\u3055\u3044\u3002",
            "\u63a8\u8ad6\u306e\u306a\u304b\u3067\u8ffd\u52a0\u3067\u8abf\u67fb\u3059\u308b\u30c7\u30d0\u30a4\u30b9\u306f\u30b3\u30e1\u30f3\u30c8\u3092\u53d6\u5f97\u3057\u3066\u304b\u3089\u8abf\u67fb\u3057\u3066\u304f\u3060\u3055\u3044\u3002",
        ),
        model="o4-mini",
        tools=tools,
        output_type=str,
    )

    question = f"{device}{addr} \u306e\u4e0d\u5177\u5408\u539f\u56e0\u3092\u8abf\u67fb\u3057\u3066\u304f\u3060\u3055\u3044\u3002"

    def _run(a, q, turns: int):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            return Runner.run_sync(a, input=q, max_turns=turns)
        finally:
            asyncio.set_event_loop(None)
            loop.close()

    try:
        result = tpool.execute(_run, agent, question, 25)
        return result.final_output
    except MaxTurnsExceeded:
        try:
            result = tpool.execute(_run, agent, question, 50)
            return result.final_output
        except Exception as ex:
            return f"AI \u547c\u3073\u51fa\u3057\u3067\u30a8\u30e9\u30fc\u304c\u767a\u751f\u3057\u307e\u3057\u305f: {ex}"
    except Exception as ex:
        return f"AI \u547c\u3073\u51fa\u3057\u3067\u30a8\u30e9\u30fc\u304c\u767a\u751f\u3057\u307e\u3057\u305f: {ex}"


def run_analysis(question: str, *, base_url: str, ip: str, port: str) -> str:
    """Search comments for a device related to the question and diagnose."""

    comment_path = os.getenv("COMMENT_CSV")
    if comment_path and os.path.exists(comment_path):
        with open(comment_path, "rb") as f:
            hs.load_comments(decode_bytes(f.read()))

    if not hs.COMMENTS:
        return "\u30b3\u30e1\u30f3\u30c8\u304c\u30ed\u30fc\u30c9\u3055\u308c\u3066\u3044\u307e\u305b\u3093"

    dev, score = hs.find_best_device(question)
    if dev is None:
        return "\u30c7\u30d0\u30a4\u30b9\u3092\u7279\u5b9a\u3067\u304d\u307e\u305b\u3093\u3067\u3057\u305f"
    device, addr = re.match(r"([A-Za-z]+)(\d+)", dev).groups()
    device = device.upper()
    addr = int(addr)
    return _run_diagnostics(device, addr, base_url=base_url, ip=ip, port=port)


__all__ = [
    "PROGRAMS",
    "decode_bytes",
    "load_program",
    "search_program",
    "related_devices",
    "read_device_values",
    "run_analysis",
]
