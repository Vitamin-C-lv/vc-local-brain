param(
    [Parameter(Mandatory = $false)]
    [ValidateSet(16384, 32768, 65536, 98304, 131072)]
    [int]$ContextSize = 65536
)

$ErrorActionPreference = 'Stop'

$exe = 'D:\VC-AI-Pet\runtime\llama.cpp\llama-server.exe'
$model = 'D:\VC-AI-Pet\models\Qwen3.5-4B\Qwen3.5-4B-Q4_K_M.gguf'
$mmproj = 'D:\VC-AI-Pet\models\Qwen3.5-4B\mmproj-F16.gguf'
$log = 'D:\VC-AI-Pet\runtime\llama-server.log'
$errorLog = 'D:\VC-AI-Pet\runtime\llama-server-error.log'

foreach ($path in @($exe, $model, $mmproj)) {
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        throw "Required Local Qwen file is missing: $path"
    }
}

# There is deliberately one physical server. Stop only the matching model
# instance on port 17861; never kill unrelated llama.cpp processes.
$old = Get-CimInstance Win32_Process |
    Where-Object {
        $_.Name -eq 'llama-server.exe' -and
        $_.CommandLine -like '*Qwen3.5-4B-Q4_K_M.gguf*' -and
        $_.CommandLine -like '*--port 17861*'
    }
foreach ($process in $old) {
    Stop-Process -Id $process.ProcessId -Force -ErrorAction Stop
}

$deadline = (Get-Date).AddSeconds(30)
while ((Get-Date) -lt $deadline) {
    $stillRunning = Get-CimInstance Win32_Process |
        Where-Object {
            $_.Name -eq 'llama-server.exe' -and
            $_.CommandLine -like '*Qwen3.5-4B-Q4_K_M.gguf*' -and
            $_.CommandLine -like '*--port 17861*'
        }
    if (-not $stillRunning) { break }
    Start-Sleep -Milliseconds 250
}
if ($stillRunning) {
    throw 'The previous Local Qwen server did not stop within 30 seconds.'
}

$arguments = @(
    '--model', $model,
    '--mmproj', $mmproj,
    '--host', '0.0.0.0',
    '--port', '17861',
    '--ctx-size', [string]$ContextSize,
    '--n-gpu-layers', '999',
    '--parallel', '1',
    '--sleep-idle-seconds', '900',
    '--alias', 'li-huahua-local'
)

$wslInterop = $env:WSL_INTEROP
$wslEnv = $env:WSLENV
$env:WSL_INTEROP = $null
$env:WSLENV = $null
try {
    # Do not pass WSL's interop marker into the detached Windows process. If it
    # is inherited, WSL keeps the invoking PowerShell attached to the server's
    # lifetime and a relay context switch can never return promptly.
    $started = Start-Process -FilePath $exe -ArgumentList $arguments -WorkingDirectory (Split-Path -Parent $exe) -WindowStyle Hidden -RedirectStandardOutput $log -RedirectStandardError $errorLog -PassThru
}
finally {
    $env:WSL_INTEROP = $wslInterop
    $env:WSLENV = $wslEnv
}
if (-not $started) {
    throw 'Windows did not return a process handle for llama-server.'
}

"started Local Qwen PID=$($started.Id) ContextSize=$ContextSize"
