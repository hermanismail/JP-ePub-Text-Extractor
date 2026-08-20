<#
  Create-Shortcut.ps1
  ---------------------------------------------------------------
  Creates a Desktop shortcut for ePub Text Extractor that:
    - launches the GUI with pythonw.exe (no black console window)
    - uses epub_extractor.ico as its icon
    - starts in the project folder so relative paths in the app work

  After running this once, right-click the new Desktop shortcut and
  choose "Pin to taskbar" (Windows 11 does not allow scripts to pin
  automatically, so that last step is manual).

  Usage: right-click this file in File Explorer -> "Run with PowerShell"
  (or open PowerShell in this folder and run:  .\Create-Shortcut.ps1 )
#>

$ErrorActionPreference = 'Stop'

$ProjectDir = 'C:\ePub-Text-Extractor'
$ScriptName = 'epub_extractor_gui.py'
$IconPath   = Join-Path $ProjectDir 'epub_extractor.ico'
$ShortcutName = 'ePub Text Extractor.lnk'

Write-Host "== ePub Text Extractor - shortcut setup ==" -ForegroundColor Cyan

# 1. Sanity checks -------------------------------------------------
if (-not (Test-Path $ProjectDir)) {
    Write-Error "Project folder not found: $ProjectDir"
}

$ScriptPath = Join-Path $ProjectDir $ScriptName
if (-not (Test-Path $ScriptPath)) {
    Write-Error "Could not find $ScriptName in $ProjectDir"
}

if (-not (Test-Path $IconPath)) {
    Write-Warning "epub_extractor.ico not found in $ProjectDir - copy it there first, or the shortcut will use a generic icon."
}

# 2. Find pythonw.exe (prefer a project venv, then PATH) -----------
$candidates = @(
    (Join-Path $ProjectDir '.venv\Scripts\pythonw.exe'),
    (Join-Path $ProjectDir 'venv\Scripts\pythonw.exe'),
    (Join-Path $ProjectDir 'env\Scripts\pythonw.exe')
)

$PythonwPath = $candidates | Where-Object { Test-Path $_ } | Select-Object -First 1

if (-not $PythonwPath) {
    # Fall back to whatever's on PATH
    $cmd = Get-Command pythonw.exe -ErrorAction SilentlyContinue
    if ($cmd) {
        $PythonwPath = $cmd.Source
    } else {
        # Derive pythonw.exe from python.exe's location as a last resort
        $pyCmd = Get-Command python.exe -ErrorAction SilentlyContinue
        if ($pyCmd) {
            $maybe = Join-Path (Split-Path $pyCmd.Source) 'pythonw.exe'
            if (Test-Path $maybe) { $PythonwPath = $maybe }
        }
    }
}

if (-not $PythonwPath) {
    Write-Error "Could not locate pythonw.exe. Activate the venv you use for this project and re-run, or edit `$PythonwPath` at the top of this script to the full path manually."
}

Write-Host "Using Python: $PythonwPath"

# 3. Create the shortcut -------------------------------------------
$DesktopPath = [Environment]::GetFolderPath('Desktop')
$ShortcutPath = Join-Path $DesktopPath $ShortcutName

$WshShell = New-Object -ComObject WScript.Shell
$Shortcut = $WshShell.CreateShortcut($ShortcutPath)
$Shortcut.TargetPath       = $PythonwPath
$Shortcut.Arguments        = '"' + $ScriptPath + '"'
$Shortcut.WorkingDirectory = $ProjectDir
$Shortcut.WindowStyle      = 1
$Shortcut.Description      = 'ePub Text Extractor - extract chapter text from Japanese EPUB files'
if (Test-Path $IconPath) {
    $Shortcut.IconLocation = "$IconPath,0"
}
$Shortcut.Save()

Write-Host "Shortcut created: $ShortcutPath" -ForegroundColor Green
Write-Host ""
Write-Host "Next step: right-click that Desktop shortcut and choose 'Pin to taskbar'." -ForegroundColor Yellow
Write-Host "(Windows 11 blocks scripts from pinning automatically, so this last click has to be manual.)"
