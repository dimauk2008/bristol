param(
    [string]$Serial = "",
    [string]$Hash = "c8750f0d.0"
)
$adb = "C:\adb\adb.exe"
$cert = "$env:USERPROFILE\.mitmproxy\$Hash"
$sel = @()
if ($Serial) { $sel = @("-s", $Serial) }

if (-not (Test-Path $cert)) { Write-Error "Missing $cert"; exit 1 }

& $adb @sel root
Start-Sleep -Seconds 2
& $adb @sel remount
& $adb @sel push $cert "/system/etc/security/cacerts/$Hash"
& $adb @sel shell chmod 644 "/system/etc/security/cacerts/$Hash"
& $adb @sel shell ls -l "/system/etc/security/cacerts/$Hash"
Write-Output "System cert installed. Reboot emulator: adb reboot"
