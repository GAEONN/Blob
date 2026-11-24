"""Puts a 'Blob' shortcut on the Desktop and in the Start menu (opens the app, or brings the
running one to the front instantly)."""
import os
import subprocess
import sys

here = os.path.dirname(os.path.abspath(__file__))
ico = os.path.join(here, "blob.ico")

pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
target = os.path.join(here, "app.pyw")
ps = f"""
$wsh = New-Object -ComObject WScript.Shell
$desk = [Environment]::GetFolderPath('Desktop')
$menu = Join-Path ([Environment]::GetFolderPath('Programs')) 'Blob.lnk'
foreach ($old in 'Blob v5.lnk', 'Blob v6.lnk') {{
    $oldPath = Join-Path $desk $old
    if (Test-Path -LiteralPath $oldPath) {{
        $o = $wsh.CreateShortcut($oldPath)
        if ($o.Arguments -like '*app.pyw*') {{ Remove-Item -LiteralPath $oldPath -Force }}
    }}
}}
foreach ($path in (Join-Path $desk 'Blob.lnk'), $menu) {{
    $s = $wsh.CreateShortcut($path)
    $s.TargetPath = '{pythonw}'
    $s.Arguments = '"{target}"'
    $s.WorkingDirectory = '{here}'
    $s.IconLocation = '{ico},0'
    $s.Description = 'Blob - SmallBlob and Dashboard (F10 switches views)'
    $s.Save()
}}
"""
subprocess.run(["powershell", "-NoProfile", "-Command", ps], check=True)
print("Shortcuts created: Desktop and Start menu")
