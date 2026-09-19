<#
.SYNOPSIS
    Install LocalRedact for the current user. No administrator rights needed.

.DESCRIPTION
    Copies LocalRedact.exe into %LOCALAPPDATA%\Programs\LocalRedact, clears the
    "downloaded from the internet" mark that makes SmartScreen complain, and
    creates Start Menu and Desktop shortcuts.

    Everything is per-user under HKCU and LOCALAPPDATA, so this never needs an
    elevated prompt and never touches other accounts on the machine.

.PARAMETER Source
    LocalRedact.exe, or the LocalRedact-windows-exe.zip downloaded from GitHub
    Actions. If omitted, the script looks in .\dist, next to itself, and in your
    Downloads folder.

.PARAMETER FromRelease
    Download LocalRedact.exe from the repository's latest published Release
    instead of looking for a local copy. Needs no GitHub login because the
    repository is public. If no Release has been published yet, the script says
    so and tells you how to create one.

.PARAMETER Repo
    owner/name of the repository to download the Release from. Defaults to the
    project's own repository.

.PARAMETER ContextMenu
    Also add a "Redact with LocalRedact" entry to the right-click menu of PDF
    files.

.PARAMETER NoDesktopShortcut
    Skip the Desktop shortcut (the Start Menu entry is still created).

.PARAMETER Uninstall
    Remove the program, its shortcuts and the right-click entry.

.PARAMETER DryRun
    Print every action without changing anything.

.EXAMPLE
    .\install.ps1
    .\install.ps1 -Source "$env:USERPROFILE\Downloads\LocalRedact-windows-exe.zip" -ContextMenu
    .\install.ps1 -Uninstall
#>
[CmdletBinding()]
param(
    [string]$Source,
    [switch]$FromRelease,
    [string]$Repo = 'tonyatbellevue/ai-trading-wheel',
    [switch]$ContextMenu,
    [switch]$NoDesktopShortcut,
    [switch]$Uninstall,
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'

$AppName     = 'LocalRedact'
$ExeName     = 'LocalRedact.exe'
$InstallDir  = Join-Path $env:LOCALAPPDATA "Programs\$AppName"
$StartMenuDir= Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs'
$StartLnk    = Join-Path $StartMenuDir "$AppName.lnk"

# GetFolderPath follows a redirected Desktop (OneDrive "Back up your folders"),
# which is what we want. It can still come back empty on an unusual profile, so
# fall back rather than crash on an empty Join-Path.
$DesktopDir  = [Environment]::GetFolderPath('Desktop')
if (-not $DesktopDir) { $DesktopDir = Join-Path $env:USERPROFILE 'Desktop' }
$DesktopLnk  = if ($DesktopDir) { Join-Path $DesktopDir "$AppName.lnk" } else { $null }
$ContextKey  = 'HKCU:\Software\Classes\SystemFileAssociations\.pdf\shell\LocalRedact'
$script:UnpackDir = $null   # set if we had to expand the CI zip; removed at the end

function Write-Step { param([string]$Text) Write-Host "  $Text" }
function Write-Head { param([string]$Text) Write-Host "`n$Text" -ForegroundColor Cyan }
function Write-Done { param([string]$Text) Write-Host $Text -ForegroundColor Green }

function Invoke-Action {
    <# Runs a scriptblock unless -DryRun, so the whole flow can be rehearsed. #>
    param([string]$Description, [scriptblock]$Action)
    if ($DryRun) { Write-Step "[dry-run] $Description"; return }
    Write-Step $Description
    & $Action
}

function Get-ExeFromRelease {
    <# Fetch LocalRedact.exe from the newest localredact-v* Release.

       The repository is public, so this is an anonymous request - no token, no
       login. Returns $null (with an explanation) when nothing is published yet,
       so the caller can fall back to a local copy.
    #>
    param([string]$Repository)

    $api = "https://api.github.com/repos/$Repository/releases"
    Write-Step "looking for a published Release in $Repository"
    try {
        $releases = Invoke-RestMethod -Uri $api -Headers @{
            'User-Agent' = 'LocalRedact-installer'
            'Accept'     = 'application/vnd.github+json'
        } -ErrorAction Stop
    } catch {
        Write-Step "  could not reach GitHub: $($_.Exception.Message)"
        return $null
    }

    $release = $releases |
        Where-Object { $_.tag_name -like 'localredact-v*' } |
        Select-Object -First 1
    if (-not $release) {
        Write-Step '  no LocalRedact Release has been published yet'
        Write-Host ''
        Write-Host 'To publish one (repository owner, takes about two minutes):' -ForegroundColor Yellow
        Write-Host "  $($Repository) -> Actions -> 'Build LocalRedact.exe' -> Run workflow"
        Write-Host "  tick 'Also publish a GitHub Release with the exe'"
        Write-Host "  set the tag to localredact-v1.0.0, then Run"
        Write-Host ''
        Write-Host 'Until then, download the artifact and pass it with -Source.' -ForegroundColor Yellow
        Write-Host ''
        return $null
    }

    $asset = $release.assets | Where-Object { $_.name -eq $ExeName } | Select-Object -First 1
    if (-not $asset) {
        Write-Step "  Release $($release.tag_name) has no $ExeName attached"
        return $null
    }

    $destination = Join-Path ([System.IO.Path]::GetTempPath()) "localredact-dl-$PID"
    New-Item -ItemType Directory -Path $destination -Force | Out-Null
    $script:UnpackDir = $destination
    $file = Join-Path $destination $ExeName
    Write-Step "downloading $ExeName from $($release.tag_name) ($([math]::Round($asset.size/1MB,1)) MB)"
    Invoke-WebRequest -Uri $asset.browser_download_url -OutFile $file -UseBasicParsing
    return $file
}

function Resolve-SourceExe {
    <# Find LocalRedact.exe, unpacking the CI zip if that is what we were given. #>
    param([string]$Given)

    if ($FromRelease) {
        $downloaded = Get-ExeFromRelease -Repository $Repo
        if ($downloaded) { return $downloaded }
        Write-Step 'falling back to looking for a local copy'
    }

    $candidates = @()
    if ($Given) {
        $candidates += $Given
    } else {
        $here = Split-Path -Parent $PSCommandPath
        $candidates += (Join-Path (Split-Path -Parent $here) "dist\$ExeName")
        $candidates += (Join-Path $here $ExeName)
        $candidates += (Join-Path $env:USERPROFILE "Downloads\$ExeName")
        $candidates += (Join-Path $env:USERPROFILE 'Downloads\LocalRedact-windows-exe.zip')
    }

    foreach ($candidate in $candidates) {
        if (-not (Test-Path -LiteralPath $candidate)) { continue }
        $item = Get-Item -LiteralPath $candidate

        if ($item.Extension -eq '.zip') {
            $unpack = Join-Path ([System.IO.Path]::GetTempPath()) "localredact-unpack-$PID"
            if ($DryRun) {
                Write-Step "[dry-run] would unpack $($item.Name) and use the exe inside"
                return $candidate
            }
            if (Test-Path $unpack) { Remove-Item $unpack -Recurse -Force }
            Expand-Archive -LiteralPath $item.FullName -DestinationPath $unpack -Force
            $script:UnpackDir = $unpack
            $found = Get-ChildItem -Path $unpack -Filter $ExeName -Recurse |
                     Select-Object -First 1
            if (-not $found) { throw "$($item.Name) does not contain $ExeName" }
            return $found.FullName
        }

        if ($item.Name -eq $ExeName) { return $item.FullName }
    }

    # Write-Host rather than throw: PowerShell's exception formatter collapses a
    # here-string onto one line, which destroys the instructions.
    Write-Host ""
    Write-Host "Could not find $ExeName." -ForegroundColor Red
    Write-Host ""
    Write-Host "Get it one of these ways, then run this script again:"
    Write-Host ""
    Write-Host "  1. Download the ready-made build (no tools needed)"
    Write-Host "     Repository -> Actions -> 'Build LocalRedact.exe' -> latest run"
    Write-Host "     -> Artifacts -> LocalRedact-windows-exe"
    Write-Host "     then run:" -ForegroundColor DarkGray
    Write-Host "       .\install.ps1 -Source `"`$env:USERPROFILE\Downloads\LocalRedact-windows-exe.zip`"" -ForegroundColor Yellow
    Write-Host ""
    Write-Host "  2. Build it yourself (needs Python 3.9+)"
    Write-Host "       packaging\build_exe.bat" -ForegroundColor Yellow
    Write-Host ""
    Write-Host "  3. Point at a copy you already have"
    Write-Host "       .\install.ps1 -Source C:\path\to\$ExeName" -ForegroundColor Yellow
    Write-Host ""
    exit 1
}

function New-Shortcut {
    param([string]$LinkPath, [string]$TargetPath, [string]$IconPath, [string]$Description)
    $shell = New-Object -ComObject WScript.Shell
    $shortcut = $shell.CreateShortcut($LinkPath)
    $shortcut.TargetPath = $TargetPath
    $shortcut.WorkingDirectory = Split-Path -Parent $TargetPath
    $shortcut.Description = $Description
    if ($IconPath -and (Test-Path -LiteralPath $IconPath)) {
        $shortcut.IconLocation = $IconPath
    } else {
        $shortcut.IconLocation = "$TargetPath,0"
    }
    $shortcut.Save()
}

function Invoke-Uninstall {
    Write-Head "Removing $AppName"
    foreach ($link in @($StartLnk, $DesktopLnk)) {
        if ($link -and (Test-Path -LiteralPath $link)) {
            Invoke-Action "remove shortcut $link" { Remove-Item -LiteralPath $link -Force }
        }
    }
    if ((Test-Path $ContextKey -ErrorAction SilentlyContinue)) {
        Invoke-Action 'remove the right-click entry' {
            Remove-Item -Path $ContextKey -Recurse -Force
        }
    }
    if (Test-Path -LiteralPath $InstallDir) {
        Invoke-Action "delete $InstallDir" {
            Remove-Item -LiteralPath $InstallDir -Recurse -Force
        }
    }
    Write-Done "`n$AppName has been removed. Your PDFs were not touched."
}

function Invoke-Install {
    Write-Head "Installing $AppName for $env:USERNAME"
    $sourceExe = Resolve-SourceExe -Given $Source
    Write-Step "source: $sourceExe"

    Invoke-Action "create $InstallDir" {
        New-Item -ItemType Directory -Path $InstallDir -Force | Out-Null
    }

    $targetExe = Join-Path $InstallDir $ExeName
    Invoke-Action "copy $ExeName" {
        Copy-Item -LiteralPath $sourceExe -Destination $targetExe -Force
    }

    # Files downloaded with a browser carry a Zone.Identifier stream; without
    # clearing it Windows shows "Windows protected your PC" on every launch.
    Invoke-Action 'clear the downloaded-from-internet mark' {
        try {
            Unblock-File -LiteralPath $targetExe -ErrorAction Stop
        } catch {
            Write-Step "  (skipped: $($_.Exception.Message))"
        }
    }

    # The icon may sit next to this script in a repo checkout, or beside the exe
    # when only install.ps1 and the CI zip were downloaded - the zip ships the
    # .ico too. Without it the shortcuts still show the icon embedded in the exe,
    # so this is a nicety, not a requirement.
    $iconTarget = Join-Path $InstallDir 'localredact.ico'
    $iconCandidates = @(
        (Join-Path (Split-Path -Parent (Split-Path -Parent $PSCommandPath)) 'packaging\localredact.ico'),
        (Join-Path (Split-Path -Parent $PSCommandPath) 'localredact.ico'),
        (Join-Path (Split-Path -Parent $sourceExe) 'localredact.ico'),
        (Join-Path (Split-Path -Parent (Split-Path -Parent $sourceExe)) 'packaging\localredact.ico')
    )
    $iconSource = $iconCandidates | Where-Object { $_ -and (Test-Path -LiteralPath $_) } | Select-Object -First 1
    if ($iconSource) {
        Invoke-Action 'copy the icon' {
            Copy-Item -LiteralPath $iconSource -Destination $iconTarget -Force
        }
    } else {
        Write-Step 'no separate icon file found; shortcuts will use the exe icon'
        $iconTarget = $null
    }

    Invoke-Action 'create the Start Menu shortcut' {
        try {
            New-Item -ItemType Directory -Path $StartMenuDir -Force | Out-Null
            New-Shortcut -LinkPath $StartLnk -TargetPath $targetExe -IconPath $iconTarget `
                         -Description 'Remove personal data from a PDF, fully offline'
        } catch {
            Write-Step "  (could not create it: $($_.Exception.Message))"
            Write-Step "  run the exe directly from $targetExe"
        }
    }

    if (-not $NoDesktopShortcut -and $DesktopLnk) {
        Invoke-Action 'create the Desktop shortcut' {
            try {
                New-Shortcut -LinkPath $DesktopLnk -TargetPath $targetExe -IconPath $iconTarget `
                             -Description 'Remove personal data from a PDF, fully offline'
            } catch {
                Write-Step "  (could not create it: $($_.Exception.Message))"
            }
        }
    }

    if ($ContextMenu) {
        Invoke-Action 'add "Redact with LocalRedact" to the PDF right-click menu' {
            New-Item -Path $ContextKey -Force | Out-Null
            Set-ItemProperty -Path $ContextKey -Name '(Default)' -Value 'Redact with LocalRedact'
            if ($iconTarget) {
                Set-ItemProperty -Path $ContextKey -Name 'Icon' -Value $iconTarget
            }
            $commandKey = Join-Path $ContextKey 'command'
            New-Item -Path $commandKey -Force | Out-Null
            Set-ItemProperty -Path $commandKey -Name '(Default)' -Value "`"$targetExe`" `"%1`""
        }
    }

    Write-Done "`n$AppName is installed."
    Write-Host  "  Program : $targetExe"
    $launch = "Start Menu -> $AppName"
    if (-not $NoDesktopShortcut -and $DesktopLnk) { $launch += ', or the Desktop shortcut' }
    Write-Host  "  Launch  : $launch"
    if ($ContextMenu) { Write-Host "  Also    : right-click any PDF -> Redact with LocalRedact" }
    Write-Host ""
    Write-Host "First launch takes a few seconds while the bundle unpacks." -ForegroundColor DarkGray
    Write-Host "If SmartScreen still appears, choose 'More info' -> 'Run anyway':" -ForegroundColor DarkGray
    Write-Host "the exe is unsigned, which is expected for a self-built tool." -ForegroundColor DarkGray
    Write-Host ""
    Write-Host "To remove it later:  .\install.ps1 -Uninstall" -ForegroundColor DarkGray
}

if ($DryRun) { Write-Host 'DRY RUN - nothing will be changed' -ForegroundColor Yellow }
try {
    if ($Uninstall) { Invoke-Uninstall } else { Invoke-Install }
}
finally {
    # This tool promises not to leave temp files lying around; its installer had
    # better hold to the same rule.
    if ($script:UnpackDir -and (Test-Path -LiteralPath $script:UnpackDir)) {
        Remove-Item -LiteralPath $script:UnpackDir -Recurse -Force -ErrorAction SilentlyContinue
    }
}
