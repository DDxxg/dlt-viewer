# Windows PowerShell 5.1 native invocation loses embedded JSON quotes.
# Build a CommandLineToArgvW-compatible argument string explicitly instead.
function ConvertTo-WindowsArgument([string]$Value) {
    if ($Value.Length -eq 0) { return '""' }
    if ($Value -notmatch '[\s"]') { return $Value }
    $escaped = [regex]::Replace($Value, '(\\*)"', '$1$1\"')
    $escaped = [regex]::Replace($escaped, '(\\+)$', '$1$1')
    return '"' + $escaped + '"'
}

# Relative log/trigger paths belong to the caller, not to this script directory.
$launcher = Get-Command py.exe -ErrorAction SilentlyContinue
$forwarded = @()
if ($launcher) {
    $forwarded += '-3'
} else {
    $launcher = Get-Command python.exe -ErrorAction SilentlyContinue
}
if (-not $launcher) {
    [Console]::Error.WriteLine('{"schema_version":1,"status":"error","error":"Python 3.10+ not found"}')
    exit 2
}
$forwarded += (Join-Path $PSScriptRoot 'dlt_cli.py')
$forwarded += $args
$start = New-Object System.Diagnostics.ProcessStartInfo
$start.FileName = $launcher.Source
$start.Arguments = ($forwarded | ForEach-Object { ConvertTo-WindowsArgument ([string]$_) }) -join ' '
$start.UseShellExecute = $false
$start.WorkingDirectory = (Get-Location).Path
$process = [System.Diagnostics.Process]::Start($start)
try {
    $process.WaitForExit()
    $exitCode = $process.ExitCode
} finally {
    if (-not $process.HasExited) { $process.Kill() }
    $process.Dispose()
}
exit $exitCode