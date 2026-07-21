' start-cyberboss-hidden.vbs
' 以「完全隐藏窗口」的方式启动 cyberboss 的共享桥接(npm run shared:start)。
' 用途:开机自启时不弹黑色命令行窗口挡屏幕。
'
' 放置位置(固定):  %USERPROFILE%\.cyberboss\start-cyberboss-hidden.vbs
' 配套的任务计划 XML 就是按这个固定路径引用它的。
'
' 你只需要改下面这一行 CYBERBOSS_DIR = cyberboss 的克隆目录(不是你的项目目录)。

' ====== 需要你修改的地方 ======
Dim CYBERBOSS_DIR
CYBERBOSS_DIR = "C:\Users\你的用户名\cyberboss"   ' <-- 改成你 git clone cyberboss 后的目录
' ================================

Dim shell, fso, homeDir, stateDir, logPath, cmd
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

homeDir = shell.ExpandEnvironmentStrings("%USERPROFILE%")
stateDir = homeDir & "\.cyberboss"
If Not fso.FolderExists(stateDir) Then
  fso.CreateFolder(stateDir)
End If
logPath = stateDir & "\cyberboss-autostart.log"

' cd 进 cyberboss 目录并启动 shared:start,把标准输出/错误追加到日志文件,方便排查。
cmd = "cmd /c cd /d """ & CYBERBOSS_DIR & """ && npm run shared:start >> """ & logPath & """ 2>&1"

' 参数说明:第二个参数 0 = 窗口完全隐藏;第三个参数 False = 不等待、立即返回。
shell.Run cmd, 0, False
