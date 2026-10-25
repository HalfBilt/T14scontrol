# EC L2 apply - run elevated. ASCII only (non-ASCII in scripts gets garbled).
# Promote driver to System32\drivers -> repoint TSCtl -> restart -> verify
# (rollback on failure) -> DPTF telemetry to manual.
#
# No icacls step: C:\Windows\System32\drivers already grants BUILTIN\Users
# only ReadAndExecute, so a file copied there inherits a safe ACL.
$ErrorActionPreference = 'Continue'

$logDir = Join-Path $env:APPDATA 'T14scontrol\logs'
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$log = Join-Path $logDir 'apply-l2.log'
Set-Content -Path $log -Value '' -Encoding UTF8

function W([string]$m) {
    $line = (Get-Date -Format 'HH:mm:ss') + '  ' + $m
    Add-Content -Path $log -Value $line -Encoding UTF8
}

W '=== EC L2 apply START ==='

$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) { W 'FAILED: not elevated'; exit 2 }
W 'elevated: yes'

$root = Join-Path $env:APPDATA 'T14scontrol'
$src  = Join-Path $root 'cpu\TSCtl.sys'
$drvDir = Join-Path $env:SystemRoot 'System32\drivers'
$dst  = Join-Path $drvDir 'TSCtl.sys'
$py   = Join-Path $root 'runtime\python.exe'

$origImg = $null
try { $origImg = (Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Services\TSCtl' -Name ImagePath).ImagePath } catch {}
W ("original ImagePath  = " + $origImg)
$origDptf = $null
try { $origDptf = (Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Services\dptftcs' -Name Start).Start } catch {}
W ("original dptftcs    = " + $origDptf)

# ---------- 1) copy driver to System32\drivers ----------
if (-not (Test-Path $src)) { W ("FAILED: source missing: " + $src); exit 3 }
$hSrc = (Get-FileHash $src -Algorithm SHA256).Hash
Copy-Item -Path $src -Destination $dst -Force
if (-not (Test-Path $dst)) { W 'FAILED: copy did not land'; exit 4 }
$hDst = (Get-FileHash $dst -Algorithm SHA256).Hash
W ("copied -> " + $dst)
W ("sha256 src = " + $hSrc)
W ("sha256 dst = " + $hDst)
if ($hSrc -ne $hDst) { W 'FAILED: hash mismatch, aborting before touching services'; exit 5 }

$sig = Get-AuthenticodeSignature $dst
$subj = ''
if ($sig.SignerCertificate) { $subj = $sig.SignerCertificate.Subject }
W ("signature = " + $sig.Status + " | " + $subj)
if ($sig.Status -ne 'Valid') { W 'FAILED: signature not valid, aborting'; exit 6 }

# ---------- 2) verify ACL is safe (read-only check, no changes) ----------
$unsafe = @()
$acl = Get-Acl $dst
foreach ($a in $acl.Access) {
    if ($a.AccessControlType -eq 'Allow' -and
        $a.IdentityReference -match 'Users|Everyone|Authenticated' -and
        $a.FileSystemRights -match 'Write|Modify|FullControl') {
        $unsafe += ($a.IdentityReference.ToString() + ':' + $a.FileSystemRights)
    }
}
if ($unsafe.Count -gt 0) {
    W ('ACL WARNING: ordinary users can write the driver -> ' + ($unsafe -join '; '))
} else {
    W 'acl: inherited from drivers\ (Users = ReadAndExecute only), safe'
}

# ---------- 3) stop services ----------
foreach ($s in @('CpuPLFixSvc', 'T14sFanSvc')) {
    sc.exe stop $s 2>&1 | Out-Null
    W ("stop " + $s + " rc=" + $LASTEXITCODE)
    Start-Sleep -Seconds 2
}
sc.exe stop TSCtl 2>&1 | Out-Null
W ("stop TSCtl rc=" + $LASTEXITCODE)
Start-Sleep -Seconds 2

# ---------- 4) repoint + restart ----------
$newImg = '\??\' + $dst
$o = sc.exe config TSCtl binPath= $newImg 2>&1
W ("config rc=" + $LASTEXITCODE + " -> " + ($o -join ' '))
$nowImg = $null
try { $nowImg = (Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Services\TSCtl' -Name ImagePath).ImagePath } catch {}
W ("ImagePath now = " + $nowImg)

sc.exe start CpuPLFixSvc 2>&1 | Out-Null
W ("start CpuPLFixSvc rc=" + $LASTEXITCODE)
Start-Sleep -Seconds 4
sc.exe start T14sFanSvc 2>&1 | Out-Null
W ("start T14sFanSvc rc=" + $LASTEXITCODE)
Start-Sleep -Seconds 4

# ---------- 5) verify the driver is usable ----------
$drvOk = $false
$q = sc.exe query TSCtl 2>&1 | Out-String
W ('sc query TSCtl: ' + ($q -replace "`r`n", ' '))
if ($q -match 'RUNNING') { $drvOk = $true }

$devOut = ''
if (Test-Path $py) {
    Push-Location (Join-Path $root 'cpu')
    $devOut = & $py -c "import CpuPLFix as c; print('DRV_OK' if c.ensure_driver(verbose=False) else 'DRV_FAIL')" 2>&1 | Out-String
    Pop-Location
}
W ('device check: ' + ($devOut -replace "`r`n", ' '))
if ($devOut -notmatch 'DRV_OK') { $drvOk = $false }

if (-not $drvOk) {
    W 'VERIFY FAILED -> rolling back driver path'
    sc.exe config TSCtl binPath= $origImg 2>&1 | Out-Null
    W ("rollback config rc=" + $LASTEXITCODE + " -> " + $origImg)
    sc.exe start CpuPLFixSvc 2>&1 | Out-Null
    Start-Sleep -Seconds 4
    sc.exe start T14sFanSvc 2>&1 | Out-Null
    W 'ROLLED BACK - services restarted on the original driver path'
    W '=== EC L2 apply END (ROLLBACK) ==='
    exit 10
}
W 'driver OK from System32\drivers'

# ---------- 6) DPTF telemetry service -> manual (reversible) ----------
if ($origDptf -ne $null) {
    sc.exe config dptftcs start= demand 2>&1 | Out-Null
    W ("dptftcs config demand rc=" + $LASTEXITCODE)
    sc.exe stop dptftcs 2>&1 | Out-Null
    W ("dptftcs stop rc=" + $LASTEXITCODE)
} else {
    W 'dptftcs not present, skipped'
}

# ---------- 7) final state ----------
foreach ($s in @('CpuPLFixSvc', 'T14sFanSvc', 'TSCtl', 'dptftcs')) {
    $st = 'N/A'
    try { $st = (Get-Service $s).Status } catch {}
    W ('state ' + $s + ' = ' + $st)
}
W '=== EC L2 apply END (OK) ==='
exit 0
