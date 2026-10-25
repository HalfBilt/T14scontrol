' HUD 看门狗：每分钟跑一次，HUD 不在就静默拉起（用户主动退出则跳过）
' 由计划任务 T14sHUDGuard 调用：wscript.exe //B hud_guard.vbs
Option Explicit
Dim fso, base, svc, procs, p, running, sh

Set fso = CreateObject("Scripting.FileSystemObject")
base = fso.GetParentFolderName(WScript.ScriptFullName)

If fso.FileExists(base & "\quit.flag") Then WScript.Quit 0
If Not fso.FileExists(base & "\..\runtime\pythonw.exe") Then WScript.Quit 0
If Not fso.FileExists(base & "\hud.py") Then WScript.Quit 0

Set svc = GetObject("winmgmts:\\.\root\cimv2")
Set procs = svc.ExecQuery("Select * From Win32_Process Where Name='pythonw.exe'")
running = False
For Each p In procs
    If Not IsNull(p.CommandLine) Then
        If InStr(LCase(p.CommandLine), "hud.py") > 0 Then running = True
    End If
Next

If Not running Then
    Set sh = CreateObject("WScript.Shell")
    sh.Run """" & base & "\..\runtime\pythonw.exe"" """ & base & "\hud.py""", 0, False
End If
