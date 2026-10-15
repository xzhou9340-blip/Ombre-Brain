"""
========================================
tools/ferrideo/ — 观影（ferrideo）MCP 工具
========================================

包装 ombre 同服务里的 ferrideo 子进程（见 web/ferrideo_bridge.py），
让 AI 能跟她看同一场电影：读进度和当前台词、往画面上发弹幕、要一帧看画面、
记笔记、看房间全貌、散场后出票根。

关键行为：
- **后端地址只指向门禁端口**（web/ferrideo_bridge.internal_gate_base()，
  默认 http://127.0.0.1:18006/<token>/api/gate）。播放器那套能拿到字幕全文的
  路由压根不注册在那个端口上——防剧透是靠「那边没有这些路由」成立的，
  不是靠本包自觉。每次调用现读地址，不缓存
- 门禁在 node 侧：永远拿不到播放头之后的任何一句台词。本包只转发，
  绝不尝试别的路径去绕
- 抓帧是异步的：request_frame 立即返回，帧由播放器在下一次心跳时送上来
- 票根直接进记忆库（跳过 LLM 打标、不与任何旧桶合并）；写不进去时
  走 letter 降级通道并明说「还没进记忆库」——降级可以，丢失不行

不做什么（边界）：
- 不碰 read-along 的数据、不碰播放器的 /api/rooms/*
- 不在这里做防剧透判定（那是 ferrideo/gate.js 的事）

实现见 core.py；MCP 注册薄封装在 server.py（与其它工具一致）。
对外暴露：core.create_room / get_context / send_danmaku / request_frame /
         add_note / get_room / generate_ticket
========================================
"""
