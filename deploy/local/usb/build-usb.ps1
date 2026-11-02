<#
.SYNOPSIS
  Copies the ClipBox installer files onto a prepared Ventoy stick and fills in
  the login password hash.

.DESCRIPTION
  Expects a stick already initialised with Ventoy2Disk.exe (GPT, Secure Boot
  support). Copies the Ubuntu Server ISO (optional), ventoy\ventoy.json and
  autoinstall\user-data to the stick root, then replaces
  REPLACE_WITH_PASSWORD_HASH in user-data with a SHA-512 crypt hash of the
  password you type. The hash is produced by openssl.exe (Git for Windows) or
  WSL; if neither exists the file is copied with the placeholder and you are
  told how to fill it in by hand.

.EXAMPLE
  .\build-usb.ps1 -Drive E: -Iso "C:\Downloads\ubuntu-24.04.4-live-server-amd64.iso"

.EXAMPLE
  .\build-usb.ps1 -Drive E:      # ISO already on the stick
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)] [string] $Drive,
    [string] $Iso,
    [string] $RepoUrl,
    [string] $RepoBranch
)

$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$root = $Drive.TrimEnd('\') + '\'

if (-not (Test-Path $root)) { throw "Drive $Drive not found. Run Ventoy2Disk.exe first." }
if (-not (Test-Path (Join-Path $root 'ventoy')) -and -not (Get-ChildItem $root -Filter '*.iso' -ErrorAction SilentlyContinue)) {
    Write-Warning "No ventoy\ folder and no ISO on $Drive yet - is this really the Ventoy data partition?"
}

# --- ISO -------------------------------------------------------------------
$isoName = $null
if ($Iso) {
    if (-not (Test-Path $Iso)) { throw "ISO not found: $Iso" }
    $isoName = Split-Path -Leaf $Iso
    $dest = Join-Path $root $isoName
    if (Test-Path $dest) {
        Write-Host "ISO already on stick: $dest"
    } else {
        Write-Host "Copying ISO ($([math]::Round((Get-Item $Iso).Length / 1GB, 2)) GB) ..."
        Copy-Item $Iso $dest
    }
} else {
    $found = Get-ChildItem $root -Filter 'ubuntu-*live-server-amd64.iso' -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($found) { $isoName = $found.Name; Write-Host "Using ISO on stick: $isoName" }
    else { Write-Warning "No Ubuntu Server ISO given or found on the stick. Copy it to $root yourself." }
}

# --- ventoy.json -----------------------------------------------------------
New-Item -ItemType Directory -Force (Join-Path $root 'ventoy') | Out-Null
$ventoyJson = Get-Content (Join-Path $here 'ventoy\ventoy.json') -Raw
if ($isoName) {
    $ventoyJson = [regex]::Replace($ventoyJson, '"image":\s*"/[^"]+"', ('"image": "/' + $isoName + '"'))
}
Set-Content -Path (Join-Path $root 'ventoy\ventoy.json') -Value $ventoyJson -Encoding ascii -NoNewline
Write-Host "Wrote ventoy\ventoy.json"

# --- user-data -------------------------------------------------------------
New-Item -ItemType Directory -Force (Join-Path $root 'autoinstall') | Out-Null
$userData = Get-Content (Join-Path $here 'autoinstall\user-data') -Raw

if ($RepoUrl)    { $userData = $userData -replace 'https://github\.com/[^ ]+\.git', $RepoUrl }
if ($RepoBranch) { $userData = $userData -replace '--branch \S+', ('--branch ' + $RepoBranch) }

function Get-Sha512Crypt([securestring] $secure) {
    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    try { $plain = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr) }
    finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) }

    $openssl = @(
        "$env:ProgramFiles\Git\usr\bin\openssl.exe",
        "${env:ProgramFiles(x86)}\Git\usr\bin\openssl.exe",
        (Get-Command openssl.exe -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source)
    ) | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1

    if ($openssl) {
        $hash = ($plain | & $openssl passwd -6 -stdin).Trim()
        if ($hash -like '$6$*') { return $hash }
    }
    if (Get-Command wsl.exe -ErrorAction SilentlyContinue) {
        $hash = ($plain | wsl.exe openssl passwd -6 -stdin 2>$null).Trim()
        if ($hash -like '$6$*') { return $hash }
    }
    return $null
}

if ($userData -match 'REPLACE_WITH_PASSWORD_HASH') {
    $pw1 = Read-Host -AsSecureString "Linux password for user 'clip'"
    $pw2 = Read-Host -AsSecureString "Repeat"
    $p1 = [Runtime.InteropServices.Marshal]::PtrToStringBSTR([Runtime.InteropServices.Marshal]::SecureStringToBSTR($pw1))
    $p2 = [Runtime.InteropServices.Marshal]::PtrToStringBSTR([Runtime.InteropServices.Marshal]::SecureStringToBSTR($pw2))
    if ($p1 -ne $p2) { throw "Passwords differ." }
    if ($p1.Length -lt 8) { throw "Use at least 8 characters." }
    $hash = Get-Sha512Crypt $pw1
    if ($hash) {
        $userData = $userData.Replace('REPLACE_WITH_PASSWORD_HASH', $hash)
        Write-Host "Password hash set."
    } else {
        Write-Warning "No openssl.exe (Git for Windows) or WSL found - user-data keeps the placeholder."
        Write-Warning "Create the hash with 'openssl passwd -6' and paste it into $root\autoinstall\user-data."
    }
}

# cloud-init wants LF line endings
$userData = $userData -replace "`r`n", "`n"
[IO.File]::WriteAllText((Join-Path $root 'autoinstall\user-data'), $userData, (New-Object Text.UTF8Encoding $false))
Write-Host "Wrote autoinstall\user-data"

Write-Host ""
Write-Host "Stick layout:"
Get-ChildItem $root -Recurse -Depth 1 | Where-Object { $_.Name -match 'iso$|ventoy.json$|user-data$' } |
    ForEach-Object { Write-Host ("  " + $_.FullName.Substring($root.Length)) }
Write-Host ""
Write-Host "Next: boot the target PC from the stick - see README.md section 3 onwards."
