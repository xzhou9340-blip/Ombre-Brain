"""
========================================
tools/reading/ — 共读（read-along）MCP 工具
========================================

包装部署在同一台服务器上的 read-along 共读后端
（https://github.com/luoluo-1121/read-along，默认 127.0.0.1:18004），
让 AI 能查共读进度、回看/检索已解锁正文、写页边批注、回复对方批注。

关键行为：
- 全部走 read-along 的门禁（gate）端点：未解锁章节连标题都拿不到，
  这是服务端硬约束，本包只转发、绝不绕过
- 后端地址由环境变量 READING_API_BASE 控制（默认 http://127.0.0.1:18004），
  每次调用时读取，不缓存
- 实现见 core.py；MCP 注册薄封装在 server.py（与其它工具一致）

不做什么（边界）：
- 不缓存正文、不落任何数据到 buckets（共读数据归 read-along 的 data/）
- 不实现推送消费（开卷/合卷/页面推送走 read-along 自己的通道）

对外暴露：core.progress / core.text / core.search /
         core.annotate / core.annotations → str
========================================
"""
