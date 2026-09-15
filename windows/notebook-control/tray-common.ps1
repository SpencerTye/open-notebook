# Shared by tray.ps1 and install.ps1: drawing the dot, writing the .ico file
# the shortcuts use, and creating the shortcuts themselves.

Add-Type -AssemblyName System.Drawing

$script:TrayColors = @{
    on      = [System.Drawing.Color]::FromArgb(46, 160, 67)     # green
    off     = [System.Drawing.Color]::FromArgb(150, 150, 150)   # grey
    busy    = [System.Drawing.Color]::FromArgb(235, 160, 30)    # amber
    partial = [System.Drawing.Color]::FromArgb(235, 160, 30)    # amber
    file    = [System.Drawing.Color]::FromArgb(0, 122, 145)     # teal, for the shortcut icon
}

function New-DotBitmap {
    param([Parameter(Mandatory)][System.Drawing.Color]$Color, [int]$Size = 32)
    $bmp = New-Object System.Drawing.Bitmap $Size, $Size
    $g = [System.Drawing.Graphics]::FromImage($bmp)
    $g.SmoothingMode = [System.Drawing.Drawing2D.SmoothingMode]::AntiAlias
    $g.Clear([System.Drawing.Color]::Transparent)
    $margin = [int]($Size / 10)
    $brush = New-Object System.Drawing.SolidBrush $Color
    $g.FillEllipse($brush, $margin, $margin, $Size - 2 * $margin, $Size - 2 * $margin)
    $pen = New-Object System.Drawing.Pen ([System.Drawing.Color]::FromArgb(150, 0, 0, 0)), ([Math]::Max(1, $Size / 16))
    $g.DrawEllipse($pen, $margin, $margin, $Size - 2 * $margin, $Size - 2 * $margin)
    $pen.Dispose(); $brush.Dispose(); $g.Dispose()
    return $bmp
}

function New-DotIcon {
    param([Parameter(Mandatory)][System.Drawing.Color]$Color)
    $bmp = New-DotBitmap -Color $Color -Size 32
    return [System.Drawing.Icon]::FromHandle($bmp.GetHicon())
}

function New-DotIconFile {
    # Writes a one-image .ico (32x32, PNG-compressed, supported since Vista).
    param([Parameter(Mandatory)][string]$Path, [Parameter(Mandatory)][System.Drawing.Color]$Color)
    $bmp = New-DotBitmap -Color $Color -Size 32
    $ms = New-Object System.IO.MemoryStream
    $bmp.Save($ms, [System.Drawing.Imaging.ImageFormat]::Png)
    $png = $ms.ToArray()
    $ms.Dispose(); $bmp.Dispose()
    $fs = [System.IO.File]::Create($Path)
    $bw = New-Object System.IO.BinaryWriter $fs
    try {
        $bw.Write([uint16]0); $bw.Write([uint16]1); $bw.Write([uint16]1)          # ICONDIR
        $bw.Write([byte]32); $bw.Write([byte]32); $bw.Write([byte]0); $bw.Write([byte]0)
        $bw.Write([uint16]1); $bw.Write([uint16]32)
        $bw.Write([uint32]$png.Length); $bw.Write([uint32]22)                       # ICONDIRENTRY
        $bw.Write($png)
    } finally { $bw.Close(); $fs.Dispose() }
}

function Get-TrayShortcutPaths {
    [pscustomobject]@{
        IconFile = Join-Path $PSScriptRoot 'notebook.ico'
        Startup  = Join-Path ([Environment]::GetFolderPath('Startup')) 'Notebook.lnk'
        Desktop  = Join-Path ([Environment]::GetFolderPath('Desktop')) 'Notebook.lnk'
    }
}

function New-NotebookShortcut {
    # A shortcut that starts the tray icon with no console window.
    param([Parameter(Mandatory)][string]$Path)
    $p = Get-TrayShortcutPaths
    if (-not (Test-Path $p.IconFile)) { New-DotIconFile -Path $p.IconFile -Color $script:TrayColors.file }
    $ws = New-Object -ComObject WScript.Shell
    $lnk = $ws.CreateShortcut($Path)
    $lnk.TargetPath = Join-Path $env:WINDIR 'System32\wscript.exe'
    $lnk.Arguments = ('"{0}" "{1}"' -f (Join-Path $PSScriptRoot 'run-hidden.vbs'), (Join-Path $PSScriptRoot 'tray.ps1'))
    $lnk.WorkingDirectory = $PSScriptRoot
    $lnk.IconLocation = "$($p.IconFile),0"
    $lnk.Description = 'Notebook: the tray icon that starts and stops the notebook'
    $lnk.Save()
}
