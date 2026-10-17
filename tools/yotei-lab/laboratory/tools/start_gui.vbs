Option Explicit
Dim shell, files, root, runtime, command, failure
Set shell = CreateObject("WScript.Shell")
Set files = CreateObject("Scripting.FileSystemObject")
root = files.GetParentFolderName(files.GetParentFolderName(WScript.ScriptFullName))
shell.CurrentDirectory = root
runtime = shell.ExpandEnvironmentStrings("%USERPROFILE%") & "\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\pythonw.exe"
If files.FileExists(runtime) Then
    command = """" & runtime & """ """ & root & "\tools\gui.py"""
Else
    command = "pyw -3 """ & root & "\tools\gui.py"""
End If
On Error Resume Next
shell.Run command, 0, False
failure = Err.Number
If failure <> 0 Then
    Err.Clear
    shell.Run "python """ & root & "\tools\gui.py""", 0, False
    If Err.Number <> 0 Then
        MsgBox "Nao foi possivel iniciar a interface. Instale Python 3.10+ ou abra MENU_TERMINAL.bat.", 48, "AnyPS5 PC Lab"
    End If
End If
