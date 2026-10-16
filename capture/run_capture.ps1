param(
    [int]$Port = 8080,
    [string]$Filter = ""
)
$env:PYTHONUTF8 = "1"
$env:MITM_FILTER = $Filter
$script = Join-Path $PSScriptRoot "addon.py"
$mitmdump = "C:\Users\$env:USERNAME\AppData\Local\Programs\Python\Python312\Scripts\mitmdump.exe"
& $mitmdump -s $script --listen-port $Port --set stream_large_bodies=5m
