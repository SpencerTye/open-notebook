' Runs a PowerShell script with no console window at all (a shortcut that
' points at powershell.exe -WindowStyle Hidden still flashes a black window).
' Usage: wscript.exe run-hidden.vbs "<script.ps1>" [args...]
Set sh = CreateObject("WScript.Shell")
If WScript.Arguments.Count < 1 Then WScript.Quit 1
script = WScript.Arguments(0)
extra = ""
For i = 1 To WScript.Arguments.Count - 1
    extra = extra & " " & Chr(34) & WScript.Arguments(i) & Chr(34)
Next
cmd = "powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File " & Chr(34) & script & Chr(34) & extra
sh.Run cmd, 0, False
