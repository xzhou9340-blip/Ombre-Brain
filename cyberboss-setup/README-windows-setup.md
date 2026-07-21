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

**C 盘快满,装 D 盘。** clone 到 `D:\cyberboss`(自启文件已按这个路径配好):

```powershell
D:
cd \
git clone https://github.com/WenXiaoWendy/cyberboss.git   # 得到 D:\cyberboss
cd D:\cyberboss
npm install
```

**占用空间(实测):** 代码 ~3.4 MB + `node_modules` ~197 MB ≈ **约 200 MB**。
(本项目截图走系统 Chrome,正常不会额外下 Playwright 浏览器;若 `npm install` 触发浏览器下载会再多 ~150MB,一般用不到。)

> 注意:cyberboss 没有发布 npm 包,必须像上面这样「拉源码 + 在仓库目录里 npm install」。别去 `npm install -g cyberboss`。
> 如果你想换别的盘/目录,记得同步改自启文件 `start-cyberboss-hidden.vbs` 里的 `CYBERBOSS_DIR`。

---

## 3. 配置 `.env`

配置文件放在 **`%USERPROFILE%\.cyberboss\.env`**(即 `C:\Users\你的用户名\.cyberboss\.env`)。
cyberboss 读取顺序是:项目目录 `.env` → `%USERPROFILE%\.cyberboss\.env` → shell 环境。

把随附的 `cyberboss.env.example` 复制过去改名为 `.env`。内容(两个 `__` 值要你填):

```dotenv
CYBERBOSS_USER_NAME=小雪
CYBERBOSS_USER_GENDER=female
CYBERBOSS_ALLOWED_USER_IDS=L612ff_85bq
CYBERBOSS_WORKSPACE_ROOT=__Ombre-Brain本地绝对路径__
CYBERBOSS_RUNTIME=claudecode
CYBERBOSS_ENABLE_LOCATION_SERVER=false
CYBERBOSS_STATE_DIR=D:\cyberboss-data
```

> `CYBERBOSS_STATE_DIR=D:\cyberboss-data`:把运行数据也落到 D 盘(accounts / sessions / logs /
> stickers / diary / weixin-config / weixin-instructions.md 等)。只有 `.env` 文件本身仍固定读
> `%USERPROFILE%\.cyberboss\.env`(几 KB,忽略)。首次跑前不用手动建 `D:\cyberboss-data`,程序会自动创建。

> ⚠️ 别在 `D:\cyberboss` 项目目录里再放一个 `.env`。cyberboss 读取时「项目目录 `.env` 优先,命中就不再读
> `%USERPROFILE%\.cyberboss\.env`」,放两份会互相盖掉。统一只用 `%USERPROFILE%\.cyberboss\.env` 这一份。

创建目录并放置 `.env` 的 PowerShell(我另外单独发了一份已填好微信 id 和 D 盘数据目录的 `cyberboss.env` 给你,把它放到下面位置即可;或从本仓库 `cyberboss-setup\cyberboss.env.example` 复制后自己填 id):

```powershell
mkdir "$env:USERPROFILE\.cyberboss" -Force
# 把我发你的 cyberboss.env 复制成 .env(按你实际下载位置改路径):
copy "$env:USERPROFILE\Downloads\cyberboss.env" "$env:USERPROFILE\.cyberboss\.env"
notepad "$env:USERPROFILE\.cyberboss\.env"   # 只需补上 CYBERBOSS_WORKSPACE_ROOT,存盘
```

### 关于待填值
- **`CYBERBOSS_ALLOWED_USER_IDS`** 已填好 `L612ff_85bq`(你提供的微信 id)。
- **`CYBERBOSS_WORKSPACE_ROOT`** = 你的 **Ombre-Brain 仓库在本地的绝对路径**。这个我在远程沙盒里查不到你 Windows 上的路径,需要你本机跑一条命令拿到:
  ```powershell
  # 进到你的 Ombre-Brain 目录后执行(会打印绝对路径):
  (Get-Location).Path
  # 或者如果你记得大概位置,直接搜:
  Get-ChildItem -Path C:\,D:\ -Filter "Ombre-Brain" -Directory -Recurse -ErrorAction SilentlyContinue | Select-Object FullName
  ```
  把打印出来的路径(例如 `D:\code\Ombre-Brain`)填到 `.env` 的 `CYBERBOSS_WORKSPACE_ROOT=`。

### 微信 user id(已填好,供你核对)
你的 id `L612ff_85bq` 已经填进 `.env`,正常不用再动。如果之后发现 checkin 没推到你、想核对是不是这个 id,可在 `shared:start` 前台挂着时于微信发一条消息,前台日志会打印来源 id;或登录后查看 `D:\cyberboss-data\sessions.json` / `D:\cyberboss-data\accounts\` 下的记录。

> 定位:`.env` 里只有 `CYBERBOSS_ENABLE_LOCATION_SERVER=false`,**不配任何 `CYBERBOSS_LOCATION_*`**,定位整块就是关的。

---

## 4. 首次手动跑通(必须你自己来,别让工具代跑)【你来跑】

这几步要扫码、要前台挂着,顺序如下:

**终端 A(在 `D:\cyberboss` 目录):**
```powershell
cd D:\cyberboss
npm run login          # 弹二维码,用你的微信扫码登录 bot 账号
npm run shared:start   # 启动共享桥接,保持这个窗口不要关(前台挂着)
```

**终端 B(另开一个,`D:\cyberboss` 目录):**
```powershell
cd D:\cyberboss
npm run shared:open    # 接管当前微信绑定的那条共享线程
```

**在微信里(给已登录的 bot):**
```text
/bind D:\code\Ombre-Brain      # 换成你 .env 里 CYBERBOSS_WORKSPACE_ROOT 那个绝对路径
```

然后随便发条普通消息,看能不能收到回复。能通就说明装好了。

辅助诊断:另开终端 `npm run shared:status` 看进程/桥接/readyz 状态。

> 注意:`npm run shared:start` 就是共享桥接主进程,**首次验证时让它前台挂着**,别丢后台。checkin(随机唤醒)在它启动时已经自动开了。

---

## 5. 配开机自启(任务计划程序,隐藏窗口不弹黑框)

**只自启 `shared:start`,login 扫码那步不要放进自启。**

> 下面 `.\cyberboss-setup\...` 这些相对路径,默认你在 **Ombre-Brain 仓库根目录**下执行(`cyberboss-setup` 文件夹就在那儿)。不在那就换成对应的绝对路径。

### 5.1 放好隐藏启动器
把 `start-cyberboss-hidden.vbs` 复制到固定位置(XML 就按这个路径找它)。它默认已按 D 盘方案配好(`CYBERBOSS_DIR=D:\cyberboss`、日志目录 `D:\cyberboss-data`),**如果你 clone 到了别处才需要改**:

```powershell
copy .\cyberboss-setup\start-cyberboss-hidden.vbs "$env:USERPROFILE\.cyberboss\start-cyberboss-hidden.vbs"
notepad "$env:USERPROFILE\.cyberboss\start-cyberboss-hidden.vbs"   # 默认就是 D:\cyberboss,通常不用改;存盘
```

这个 VBS 用 `WScript.Shell.Run(..., 0, False)` 启动,**窗口样式 0 = 完全隐藏**,所以开机时不会弹任何命令行黑框。运行日志追加到 `D:\cyberboss-data\cyberboss-autostart.log`,出问题看它。

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
Get-Content "D:\cyberboss-data\cyberboss-autostart.log" -Tail 30
# 或用 cyberboss 自带状态(在 D:\cyberboss 目录里)
cd D:\cyberboss; npm run shared:status
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
`D:\cyberboss-data\weixin-instructions.md`(因为数据目录挪到了 D 盘;第一次跑任意 cyberboss 命令时自动生成)是人设/行为模板。
**先别改**。按 README 建议:先让 agent 在真实交流里自己更新行为,处出节奏后,再回头只修明显不对的部分。想让线程重读最新 instructions 时,在微信发 `/reread`。
