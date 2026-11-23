' 手动双击：后台静默启动 Gomoku 生产服务端（不涉及开机自启）。
Option Explicit

Dim fso, shell, root, pythonw, script, command, code
Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")

root = fso.GetParentFolderName(WScript.ScriptFullName)
pythonw = fso.BuildPath(root, "server\.venv\Scripts\pythonw.exe")
script = fso.BuildPath(root, "run_server.py")

If Not fso.FileExists(pythonw) Then
    MsgBox "未找到 server\.venv\Scripts\pythonw.exe。" & vbCrLf & _
           "请先创建服务端虚拟环境并安装依赖。", 16, "Gomoku Server"
    WScript.Quit 1
End If
If Not fso.FileExists(script) Then
    MsgBox "未找到 run_server.py。", 16, "Gomoku Server"
    WScript.Quit 1
End If

command = """" & pythonw & """ """ & script & """ --background --production"
code = shell.Run(command, 0, True)
If code <> 0 Then
    MsgBox "启动失败，请查看 server\data\server-8000.err.log。", 16, "Gomoku Server"
End If
WScript.Quit code
