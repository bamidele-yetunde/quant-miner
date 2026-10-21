' Starts the miner hidden in the background (local mode only).
' Needs BRAIN_EMAIL, BRAIN_PASSWORD and MINER_STATE_KEY set as Windows user environment variables.
' Output goes to miner\console.log. Stop it from Task Manager (python.exe).
Set fso = CreateObject("Scripting.FileSystemObject")
Set sh = CreateObject("WScript.Shell")
sh.CurrentDirectory = fso.GetParentFolderName(WScript.ScriptFullName)
If Not fso.FolderExists("miner") Then fso.CreateFolder("miner")
sh.Run "cmd /c python miner.py >> miner\console.log 2>&1", 0, False
