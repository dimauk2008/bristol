param(
    [string]$Serial = "",
    [int]$Port = 8080
)
$adb = "C:\adb\adb.exe"
$sel = @()
if ($Serial) { $sel = @("-s", $Serial) }

& $adb @sel reverse "tcp:$Port" "tcp:$Port"
& $adb @sel shell settings put global http_proxy "127.0.0.1:$Port"
& $adb @sel shell settings get global http_proxy
Write-Output "Proxy enabled: 127.0.0.1:$Port (via adb reverse)."
