"""命令行入口:python speak_cli.py "台词内容" [--stability --style --speed]

- 从项目根目录的 .env 加载环境变量(python-dotenv)
- 成功后打印公开 URL,并往本目录的 声音库.md 追加一条记录
- 失败时把 API 原始错误完整打印到 stderr
"""

import argparse
import sys
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

from voice_core import SpeechError, generate_speech

HERE = Path(__file__).resolve().parent
LIBRARY_FILE = HERE / "声音库.md"
LIBRARY_HEADER = "# 声音库\n\n| 时间 | 台词 | stability | style | speed | URL |\n|---|---|---|---|---|---|\n"


def _load_env() -> None:
    # 优先项目根目录的 .env,其次当前目录,便于两种运行位置
    load_dotenv(HERE.parent / ".env")
    load_dotenv()


def _append_library(text: str, stability: float, style: float, speed: float,
                    url: str) -> None:
    if not LIBRARY_FILE.exists():
        LIBRARY_FILE.write_text(LIBRARY_HEADER, encoding="utf-8")
    cell = text.replace("|", "\\|").replace("\n", " ")
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    row = f"| {now} | {cell} | {stability} | {style} | {speed} | {url} |\n"
    with LIBRARY_FILE.open("a", encoding="utf-8") as f:
        f.write(row)


def main() -> None:
    parser = argparse.ArgumentParser(description="ElevenLabs TTS → Supabase Storage")
    parser.add_argument("text", help="要合成的台词")
    parser.add_argument("--stability", type=float, default=0.34)
    parser.add_argument("--style", type=float, default=0.84)
    parser.add_argument("--speed", type=float, default=1.2)
    args = parser.parse_args()

    _load_env()

    try:
        url = generate_speech(args.text, stability=args.stability,
                              style=args.style, speed=args.speed)
    except SpeechError as e:
        print(f"生成失败:{e}", file=sys.stderr)
        sys.exit(1)

    _append_library(args.text, args.stability, args.style, args.speed, url)
    print(url)


if __name__ == "__main__":
    main()
