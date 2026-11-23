' 手动双击：停止由 start_server.vbs 启动的后台服务端。
Option Explicit

Dim fso, shell, root, pythonw, script, command, code
Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")

root = fso.GetParentFolderName(WScript.ScriptFullName)
pythonw = fso.BuildPath(root, "server\.venv\Scripts\pythonw.exe")
script = fso.BuildPath(root, "run_server.py")

If Not fso.FileExists(pythonw) Then
    MsgBox "未找到 server\.venv\Scripts\pythonw.exe。", 16, "Gomoku Server"
    WScript.Quit 1
End If

command = """" & pythonw & """ """ & script & """ --stop"
code = shell.Run(command, 0, True)
If code <> 0 Then
    MsgBox "没有找到正在运行的后台服务端（或 PID 文件已失效）。", 48, "Gomoku Server"
End If
WScript.Quit code
