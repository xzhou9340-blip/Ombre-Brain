# voice — TTS 语音功能(第一阶段:本地跑通)

ElevenLabs(`eleven_v3`,中文)生成 mp3 → 上传 Supabase Storage 的 `voices` bucket(public,自动创建)→ 返回公开 URL。

- `voice_core.py`:核心逻辑,只依赖 requests + 环境变量,不碰命令行和本地文件。
  第二阶段把 `generate_speech` 原样搬进 ombre 注册为 MCP 工具。
- `speak_cli.py`:命令行入口,负责加载 `.env`、打印 URL、往 `声音库.md`(gitignored)追加记录。

## 使用

```bash
pip install -r voice/requirements.txt
cp .env.template .env   # 项目根目录,填入四个变量

python voice/speak_cli.py "床前明月光,疑是地上霜。"
python voice/speak_cli.py "台词" --stability 0.34 --style 0.84 --speed 1.2
```

成功打印公开 URL;失败时把 ElevenLabs / Supabase 返回的原始错误完整打印到 stderr。
