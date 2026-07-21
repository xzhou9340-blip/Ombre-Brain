# Cyberboss 本地安装指引(Windows · 最小一档)

目标配置:**随机 checkin 开 / 微信桥开 / 定位·whereabouts 关 / runtime = Claude Code / 开机自启(不弹黑框)**。

> 说明:登录扫码那步和「前台挂着」的启动步骤必须你自己在本机跑,下面凡是标了 **【你来跑】** 的都由你操作。
> 本目录随附三个文件:
> - `cyberboss.env.example` —— `.env` 模板
> - `start-cyberboss-hidden.vbs` —— 隐藏窗口启动器
> - `CyberbossSharedStart.xml` —— 任务计划(自启)定义

---

## 1. 检查环境 【你来跑】

打开 **PowerShell**,逐条跑:

```powershell
node -v
```

- 要求 **≥ v22**。
- 如果低于 22 或提示找不到 node,按下面升级/安装:
  - **推荐(能管多版本):** 装 [nvm-windows](https://github.com/coreybutler/nvm-windows/releases) → 下载 `nvm-setup.exe` 装好后:
    ```powershell
    nvm install 22
    nvm use 22
    node -v      # 确认变成 v22.x
    ```
  - **或者最简单:** 去 [nodejs.org](https://nodejs.org/) 下载 **LTS(22 或更高)** 的 Windows 安装包,一路下一步,装完**重开一个 PowerShell** 再 `node -v`。

确认 `claude` 命令能用(claudecode runtime 依赖它):

```powershell
claude --version
```

- 有版本号就 OK。
- 如果提示找不到 `claude`,先装 Claude Code:
  ```powershell
  npm install -g @anthropic-ai/claude-code
  claude --version
  ```
  装完后先手动跑一次 `claude` 完成登录/授权,确保它能独立启动。

---

## 2. 拉源码 + 装依赖 【你来跑】

选一个你放代码的目录(记住这个 **cyberboss 克隆目录**,后面自启要用),然后:

```powershell
git clone https://github.com/WenXiaoWendy/cyberboss.git
cd cyberboss
npm install
```

> 注意:cyberboss 没有发布 npm 包,必须像上面这样「拉源码 + 在仓库目录里 npm install」。别去 `npm install -g cyberboss`。

---

## 3. 配置 `.env`

配置文件放在 **`%USERPROFILE%\.cyberboss\.env`**(即 `C:\Users\你的用户名\.cyberboss\.env`)。
cyberboss 读取顺序是:项目目录 `.env` → `%USERPROFILE%\.cyberboss\.env` → shell 环境。

把随附的 `cyberboss.env.example` 复制过去改名为 `.env`。内容(两个 `__` 值要你填):

```dotenv
CYBERBOSS_USER_NAME=小雪
CYBERBOSS_USER_GENDER=female
CYBERBOSS_ALLOWED_USER_IDS=__我的微信ID__
CYBERBOSS_WORKSPACE_ROOT=__我项目的绝对路径__
CYBERBOSS_RUNTIME=claudecode
CYBERBOSS_ENABLE_LOCATION_SERVER=false
```

创建目录并复制的 PowerShell:

```powershell
mkdir "$env:USERPROFILE\.cyberboss" -Force
copy .\cyberboss-setup\cyberboss.env.example "$env:USERPROFILE\.cyberboss\.env"
notepad "$env:USERPROFILE\.cyberboss\.env"   # 填两个 __ 值,存盘
```

### 关于两个待填值
- **`CYBERBOSS_WORKSPACE_ROOT`** = 你想让 agent 干活的**项目绝对路径**(不是 cyberboss 目录本身),例如 `C:\Users\xue\my-project`。
- **`CYBERBOSS_ALLOWED_USER_IDS`** = 你的**微信 user id**,作用是随机 checkin / 提醒 / 主动消息默认发给谁。

### 怎么拿到微信 user id
最省事的办法:**先不填也能跑**(源码里会从你第一次在微信发消息的 `from_user_id` 自动学到并绑定)。但为了让「随机 checkin」从一开始就有推送目标,建议拿到后回填。两种拿法:
1. 完成第 4 步登录并在微信里给 bot 发一条消息(比如 `/bind`)后,查看:
   `%USERPROFILE%\.cyberboss\sessions.json` 或 `%USERPROFILE%\.cyberboss\accounts\` 下的记录,里面会出现你的 user id。
2. 或第 4 步 `shared:start` 前台挂着时,你在微信发一条消息,前台日志里会打印收到消息的来源 id。
   拿到后填进 `.env` 的 `CYBERBOSS_ALLOWED_USER_IDS=` 再重启 `shared:start` 即可。

> 定位:`.env` 里只有 `CYBERBOSS_ENABLE_LOCATION_SERVER=false`,**不配任何 `CYBERBOSS_LOCATION_*`**,定位整块就是关的。

---

## 4. 首次手动跑通(必须你自己来,别让工具代跑)【你来跑】

这几步要扫码、要前台挂着,顺序如下:

**终端 A(cyberboss 目录):**
```powershell
npm run login          # 弹二维码,用你的微信扫码登录 bot 账号
npm run shared:start   # 启动共享桥接,保持这个窗口不要关(前台挂着)
```

**终端 B(另开一个,cyberboss 目录):**
```powershell
npm run shared:open    # 接管当前微信绑定的那条共享线程
```

**在微信里(给已登录的 bot):**
```text
/bind C:\Users\xue\my-project      # 换成你的项目绝对路径,绑定项目目录
```

然后随便发条普通消息,看能不能收到回复。能通就说明装好了。

辅助诊断:另开终端 `npm run shared:status` 看进程/桥接/readyz 状态。

> 注意:`npm run shared:start` 就是共享桥接主进程,**首次验证时让它前台挂着**,别丢后台。checkin(随机唤醒)在它启动时已经自动开了。

---

## 5. 配开机自启(任务计划程序,隐藏窗口不弹黑框)

**只自启 `shared:start`,login 扫码那步不要放进自启。**

### 5.1 放好隐藏启动器
把随附的 `start-cyberboss-hidden.vbs` 复制到固定位置(XML 就按这个路径找它),并改里面的 `CYBERBOSS_DIR`:

```powershell
copy .\cyberboss-setup\start-cyberboss-hidden.vbs "$env:USERPROFILE\.cyberboss\start-cyberboss-hidden.vbs"
notepad "$env:USERPROFILE\.cyberboss\start-cyberboss-hidden.vbs"
# 把 CYBERBOSS_DIR = "C:\Users\你的用户名\cyberboss" 改成你第 2 步的 cyberboss 克隆目录,存盘
```

这个 VBS 用 `WScript.Shell.Run(..., 0, False)` 启动,**窗口样式 0 = 完全隐藏**,所以开机时不会弹任何命令行黑框。运行日志追加到 `%USERPROFILE%\.cyberboss\cyberboss-autostart.log`,出问题看它。

### 5.2 导入任务
把 `CyberbossSharedStart.xml` 导入任务计划程序(PowerShell,普通权限即可):

```powershell
schtasks /create /tn "CyberbossSharedStart" /xml ".\cyberboss-setup\CyberbossSharedStart.xml"
```

> 若导入报编码错误(个别老版本 schtasks 只吃 UTF-16),用 PowerShell 转一下再导入:
> ```powershell
> (Get-Content .\cyberboss-setup\CyberbossSharedStart.xml) | Set-Content -Encoding Unicode .\CyberbossSharedStart-u16.xml
> schtasks /create /tn "CyberbossSharedStart" /xml ".\CyberbossSharedStart-u16.xml"
> ```

任务要点(已在 XML 里配好):
- **触发器:** 用户登录时(At log on),登录后延迟 15 秒再启动。
- **动作:** `wscript.exe "%USERPROFILE%\.cyberboss\start-cyberboss-hidden.vbs"` → 隐藏跑 `npm run shared:start`。
- **隐藏:** 任务设了 `Hidden`,加上 VBS 窗口样式 0,双保险不弹框。
- **多实例:** `IgnoreNew`,已经在跑就不会重复起。

> 如果只想「当前用户登录才触发」,可在任务计划程序 GUI 里编辑该任务 → 触发器 → 指定用户为你自己;或在 XML 的 `<LogonTrigger>` 里加 `<UserId>你的电脑名\你的用户名</UserId>` 再重新导入。默认不加 = 任何用户登录都触发,单人电脑够用了。

### 5.3 手动测试(不用重启就能验证)
```powershell
schtasks /run /tn "CyberbossSharedStart"     # 立即触发一次
```
等几秒后确认它在跑:
```powershell
# 看日志有没有正常输出
Get-Content "$env:USERPROFILE\.cyberboss\cyberboss-autostart.log" -Tail 30
# 或用 cyberboss 自带状态(在 cyberboss 目录里)
npm run shared:status
```
看到进程/桥接起来、日志没报错,就说明自启没问题。整个过程应该没有任何黑框弹出。

### 5.4 临时关掉自启 / 重新打开
```powershell
schtasks /change /tn "CyberbossSharedStart" /disable    # 关掉(不再开机自启)
schtasks /change /tn "CyberbossSharedStart" /enable     # 重新打开
schtasks /query  /tn "CyberbossSharedStart"             # 查看当前状态
schtasks /end    /tn "CyberbossSharedStart"             # 结束当前这次运行(不影响启用/禁用状态)
schtasks /delete /tn "CyberbossSharedStart" /f          # 彻底删除这个自启任务
```

> 禁用只是不再自动跑,已经在跑的那次不会被杀。想同时停掉正在跑的,先 `/end` 再 `/disable`。

---

## 6. ⚠️ 风控提醒
微信桥是**非官方通道,有封号风险**。强烈建议**别用你的主号**登录 bot,用小号/备用号,降低影响。

---

## 7. 人设文件先别动
`%USERPROFILE%\.cyberboss\weixin-instructions.md`(第一次跑任意 cyberboss 命令时自动生成)是人设/行为模板。
**先别改**。按 README 建议:先让 agent 在真实交流里自己更新行为,处出节奏后,再回头只修明显不对的部分。想让线程重读最新 instructions 时,在微信发 `/reread`。
