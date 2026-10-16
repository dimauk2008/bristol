param([string]$Serial = "")
$adb = "C:\adb\adb.exe"
$sel = @()
if ($Serial) { $sel = @("-s", $Serial) }

& $adb @sel shell settings put global http_proxy :0
& $adb @sel reverse --remove-all
& $adb @sel shell settings get global http_proxy
Write-Output "Proxy reset."
