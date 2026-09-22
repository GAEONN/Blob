"""One-time setup: builds the app icon and puts a 'Blob' shortcut on the Desktop."""
import os
import subprocess
import sys

from PIL import Image, ImageDraw

here = os.path.dirname(os.path.abspath(__file__))
ico = os.path.join(here, "blob-v5-glass.ico")

S = 256
img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
d = ImageDraw.Draw(img)
d.rounded_rectangle((8, 8, S - 8, S - 8), radius=56, fill=(22, 25, 30, 255))
d.rounded_rectangle((104, 36, 152, 168), radius=24, outline=(236, 238, 241, 255), width=14)
d.rounded_rectangle((120, 84, 136, 180), radius=8, fill=(255, 122, 69, 255))
d.ellipse((90, 150, 166, 226), fill=(255, 122, 69, 255))
img.save(ico, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])

pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
target = os.path.join(here, "app.pyw")
ps = f"""
$d = [Environment]::GetFolderPath('Desktop')
$s = (New-Object -ComObject WScript.Shell).CreateShortcut((Join-Path $d 'Blob v5.lnk'))
$s.TargetPath = '{pythonw}'
$s.Arguments = '"{target}"'
$s.WorkingDirectory = '{here}'
$s.IconLocation = '{ico}'
$s.Description = 'Blob v5 - SmallBlob and Dashboard in one app (F10 switches views)'
$s.Save()
"""
subprocess.run(["powershell", "-NoProfile", "-Command", ps], check=True)
print("Shortcut created on Desktop")
