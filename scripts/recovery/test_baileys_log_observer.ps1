[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$Observer = Join-Path $PSScriptRoot 'baileys_log_observer.ps1'
$Image = 'mbb-recovery-baileys7:ec91a01-20260715120359'
$ImageId = 'sha256:e4d6c0dccab814270d6b0d39d854cf535dfba3b0bbabbd7abd7223aafb7483ab'
$ScenarioResults = [Collections.Generic.List[object]]::new()
$CreatedContainers = [Collections.Generic.List[string]]::new()
$TempFiles = [Collections.Generic.List[string]]::new()

function Assert-True([bool]$Condition, [string]$Message) {
    if (-not $Condition) { throw $Message }
}

function Invoke-Docker {
    param([string[]]$Arguments, [switch]$AllowFailure)
    $priorErrorAction = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        $rawOutput = @(& docker @Arguments 2>&1)
        $code = $LASTEXITCODE
    } finally { $ErrorActionPreference = $priorErrorAction }
    $output = @($rawOutput | ForEach-Object {
        if ($_ -is [Management.Automation.ErrorRecord]) { [string]$_.Exception.Message }
        else { [string]$_ }
    })
    if (-not $AllowFailure -and $code -ne 0) {
        throw "Docker command failed with exit code $code"
    }
    [pscustomobject]@{ Code = $code; Output = $output }
}

function New-TestPath([string]$Suffix) {
    $path = Join-Path $env:TEMP ("mbb-observer-{0}-{1}" -f [guid]::NewGuid().ToString('N'), $Suffix)
    $TempFiles.Add($path)
    $path
}

function New-Emitter([string]$JavaScript) {
    $name = 'mbb-observer-' + [guid]::NewGuid().ToString('N').Substring(0, 16)
    $encodedJavaScript = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($JavaScript))
    $loader = "eval(Buffer.from(process.argv[1],'base64').toString('utf8'))"
    $call = Invoke-Docker -Arguments @(
        'create', '--name', $name, '--network', 'none', '--entrypoint', 'node',
        $Image, '-e', $loader, $encodedJavaScript
    )
    $id = ([string]$call.Output[-1]).Trim()
    Assert-True ($id -match '^[a-f0-9]{64}$') 'Emitter container ID was invalid'
    $inspection = (Invoke-Docker -Arguments @('inspect', $id)).Output -join "`n" | ConvertFrom-Json
    Assert-True ($inspection[0].HostConfig.NetworkMode -eq 'none') 'Emitter network mode was not none'
    Assert-True ($inspection[0].Mounts.Count -eq 0) 'Emitter unexpectedly had a mount'
    Assert-True ($inspection[0].Config.Entrypoint[0] -eq 'node') 'Emitter did not use the Node-only entrypoint'
    $CreatedContainers.Add($id)
    [pscustomobject]@{ Id = $id; Name = $name }
}

function Start-Observer {
    param(
        [string]$ContainerId,
        [string]$ExpectedImage = $ImageId,
        [int]$StartTimeout = 5,
        [int]$Window = 3,
        [switch]$StopOnWindow,
        [int]$ForceStreamInterruption = 0,
        [int]$Threshold = 3
    )
    $result = New-TestPath 'result.json'
    $stdout = New-TestPath 'stdout.txt'
    $stderr = New-TestPath 'stderr.txt'
    $arguments = @(
        '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $Observer,
        '-ContainerId', $ContainerId, '-ExpectedImageId', $ExpectedImage,
        '-StartTimeoutSeconds', [string]$StartTimeout,
        '-ObservationSeconds', [string]$Window,
        '-SafeResultPath', $result, '-PollMilliseconds', '50',
        '-ParseFailureThreshold', [string]$Threshold
    )
    if ($StopOnWindow) { $arguments += '-StopOnWindowComplete' }
    if ($ForceStreamInterruption -gt 0) {
        $arguments += @('-TestForceStreamInterruptionAfterMilliseconds', [string]$ForceStreamInterruption)
    }
    $process = Start-Process -FilePath 'powershell.exe' -ArgumentList $arguments -PassThru -WindowStyle Hidden `
        -RedirectStandardOutput $stdout -RedirectStandardError $stderr
    [pscustomobject]@{ Process = $process; Result = $result; Stdout = $stdout; Stderr = $stderr }
}

function Wait-Observer {
    param([object]$Run, [int]$TimeoutSeconds = 20)
    $deadline = [DateTimeOffset]::UtcNow.AddSeconds($TimeoutSeconds)
    while (-not $Run.Process.HasExited -and [DateTimeOffset]::UtcNow -lt $deadline) {
        Start-Sleep -Milliseconds 50
    }
    if (-not $Run.Process.HasExited) {
        Stop-Process -Id $Run.Process.Id -Force
        throw 'Observer exceeded test timeout'
    }
    $Run.Process.WaitForExit()
    $Run.Process.Refresh()
    Assert-True (Test-Path -LiteralPath $Run.Result) 'Observer did not write a safe result'
    $result = Get-Content -LiteralPath $Run.Result -Raw | ConvertFrom-Json
    [pscustomobject]@{
        ExitCode = [int]$result.exit_code
        Result = $result
        Console = [string]((Get-Content $Run.Stdout -Raw -ErrorAction SilentlyContinue) +
            (Get-Content $Run.Stderr -Raw -ErrorAction SilentlyContinue))
    }
}

function Start-Emitter([object]$Emitter) {
    $null = Invoke-Docker -Arguments @('start', $Emitter.Id)
}

function Remove-Emitter([object]$Emitter) {
    if ($null -ne $Emitter) {
        $null = Invoke-Docker -Arguments @('rm', '--force', $Emitter.Id) -AllowFailure
        $CreatedContainers.Remove($Emitter.Id) | Out-Null
    }
}

function Add-Pass([string]$Name, [object]$Result) {
    $ScenarioResults.Add([pscustomobject]@{ Scenario = $Name; Outcome = $Result.outcome })
    Write-Output "$Name=pass"
}

function Invoke-ScenarioA {
    $emitter = $null
    try {
        $js = 'const e=o=>console.log(JSON.stringify(o));e({msg:"baileys.connect_started",next_socket_generation:1});e({msg:"qr_code_generated",qr_present:true});setTimeout(()=>e({msg:"baileys.reconnect_scheduled",disconnect_category:"connection_lost_or_timed_out",disconnect_origin:"keepalive_silence",socket_generation:1}),150);setTimeout(()=>e({msg:"later_marker"}),1800);setTimeout(()=>{},5000);'
        $emitter = New-Emitter $js
        $run = Start-Observer -ContainerId $emitter.Id
        Start-Sleep -Milliseconds 400
        Assert-True (-not $run.Process.HasExited) 'Observer exited while container was created'
        Start-Emitter $emitter
        $done = Wait-Observer $run
        $shape = ''
        if ($done.Result.outcome -ne 'CRITICAL_EVENT') {
            $logCall = Invoke-Docker -Arguments @('logs', '--timestamps', $emitter.Id) -AllowFailure
            $shape = (@($logCall.Output | ForEach-Object {
                $text = [string]$_
                "length={0},json_shape={1},brace={2}" -f $text.Length,
                    [regex]::IsMatch($text, '^\S+\s+\{.*\}$'), $text.IndexOf('{')
            }) -join ';')
        }
        Assert-True ($done.ExitCode -eq 10 -and $done.Result.outcome -eq 'CRITICAL_EVENT') `
            ("Scenario A outcome: exit={0}, outcome={1}, event={2}, shape={3}" -f $done.ExitCode, $done.Result.outcome, $done.Result.event_name, $shape)
        Assert-True ($done.Result.disconnect_origin -eq 'keepalive_silence') 'Scenario A origin'
        Assert-True ($done.Result.stop_confirmed -and $done.Result.processed_event_count -eq 3) 'Scenario A stop/dedup'
        Add-Pass 'scenario_A_created_then_started' $done.Result
    } finally { Remove-Emitter $emitter }
}

function Invoke-ScenarioB {
    $emitter = $null
    try {
        $js = 'console.log(JSON.stringify({msg:"baileys.reconnect_scheduled",disconnect_category:"connection_lost_or_timed_out",disconnect_origin:"qr_refs_exhausted",socket_generation:1}));'
        $emitter = New-Emitter $js
        $run = Start-Observer -ContainerId $emitter.Id
        Start-Sleep -Milliseconds 250
        Start-Emitter $emitter
        $done = Wait-Observer $run
        Assert-True ($done.ExitCode -eq 10 -and $done.Result.disconnect_origin -eq 'qr_refs_exhausted') `
            ("Scenario B replay: outcome={0}, event={1}, origin={2}" -f $done.Result.outcome, $done.Result.event_name, $done.Result.disconnect_origin)
        Assert-True ($done.Result.stop_issued -and $done.Result.stop_confirmed) 'Scenario B already-exited stop'
        Add-Pass 'scenario_B_immediate_exit' $done.Result
    } finally { Remove-Emitter $emitter }
}

function Invoke-ScenarioC {
    $emitter = $null
    try {
        $emitter = New-Emitter 'setTimeout(()=>{},5000);'
        $done = Wait-Observer (Start-Observer -ContainerId $emitter.Id -StartTimeout 1) 10
        Assert-True ($done.ExitCode -eq 22 -and $done.Result.outcome -eq 'START_TIMEOUT') 'Scenario C timeout'
        $inspection = (Invoke-Docker -Arguments @('inspect', $emitter.Id)).Output -join "`n" | ConvertFrom-Json
        Assert-True ($inspection[0].State.Status -eq 'created') 'Observer started timeout container'
        $syncResult = New-TestPath 'sync-timeout-result.json'
        & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $Observer `
            -ContainerId $emitter.Id -ExpectedImageId $ImageId -StartTimeoutSeconds 1 `
            -ObservationSeconds 1 -SafeResultPath $syncResult -PollMilliseconds 50
        $syncExitCode = $LASTEXITCODE
        $syncSafeResult = Get-Content -LiteralPath $syncResult -Raw | ConvertFrom-Json
        Assert-True ($syncExitCode -eq 22 -and $syncSafeResult.exit_code -eq 22) `
            'Scenario C process exit code was not deterministic'
        Add-Pass 'scenario_C_start_timeout' $done.Result
    } finally { Remove-Emitter $emitter }
}

function Invoke-ScenarioD {
    $emitter = $null
    try {
        $emitter = New-Emitter 'console.log(JSON.stringify({msg:"baileys.connect_started",next_socket_generation:1}));setTimeout(()=>{},5000);'
        $run = Start-Observer -ContainerId $emitter.Id -Window 1 -StopOnWindow
        Start-Sleep -Milliseconds 200; Start-Emitter $emitter
        $done = Wait-Observer $run
        Assert-True ($done.ExitCode -eq 0 -and $done.Result.outcome -eq 'WINDOW_COMPLETE') 'Scenario D window'
        Assert-True $done.Result.stop_confirmed 'Scenario D cleanup stop'
        Add-Pass 'scenario_D_window_complete' $done.Result
    } finally { Remove-Emitter $emitter }
}

function Invoke-ScenarioE {
    $emitter = $null
    try {
        $emitter = New-Emitter 'console.log("malformed connection_lost_or_timed_out evidence");setTimeout(()=>{},5000);'
        $run = Start-Observer -ContainerId $emitter.Id
        Start-Sleep -Milliseconds 200; Start-Emitter $emitter
        $done = Wait-Observer $run
        Assert-True ($done.ExitCode -eq 20 -and $done.Result.event_name -eq 'malformed_critical_line') 'Scenario E malformed'
        Assert-True $done.Result.stop_confirmed 'Scenario E stop'
        Assert-True (-not $done.Console.Contains('malformed connection')) 'Scenario E raw leak'
        Add-Pass 'scenario_E_malformed_critical' $done.Result
    } finally { Remove-Emitter $emitter }
}

function Invoke-ScenarioF {
    $emitter = $null
    try {
        $emitter = New-Emitter 'console.log(JSON.stringify({msg:"baileys.connect_started",next_socket_generation:1}));setTimeout(()=>{},5000);'
        $run = Start-Observer -ContainerId $emitter.Id -ForceStreamInterruption 400
        Start-Sleep -Milliseconds 200; Start-Emitter $emitter
        $done = Wait-Observer $run
        Assert-True ($done.ExitCode -eq 20 -and $done.Result.event_name -eq 'running_stream_ended') 'Scenario F stream'
        Assert-True $done.Result.stop_confirmed 'Scenario F stop'
        Add-Pass 'scenario_F_running_stream_interruption' $done.Result
    } finally { Remove-Emitter $emitter }
}

function Invoke-ScenarioG {
    $emitter = $null
    try {
        $emitter = New-Emitter 'setTimeout(()=>{},5000);'
        $wrong = 'sha256:' + ('0' * 64)
        $done = Wait-Observer (Start-Observer -ContainerId $emitter.Id -ExpectedImage $wrong) 10
        Assert-True ($done.ExitCode -eq 21 -and $done.Result.outcome -eq 'IDENTITY_MISMATCH') 'Scenario G identity'
        $inspection = (Invoke-Docker -Arguments @('inspect', $emitter.Id)).Output -join "`n" | ConvertFrom-Json
        Assert-True ($inspection[0].State.Status -eq 'created') 'Identity observer started target'
        Add-Pass 'scenario_G_image_mismatch' $done.Result
    } finally { Remove-Emitter $emitter }
}

function Invoke-ScenarioH {
    $emitter = $null
    try {
        $js = 'console.log(JSON.stringify({msg:"baileys.connect_started",next_socket_generation:1}));setTimeout(()=>console.log(JSON.stringify({msg:"baileys.reconnect_scheduled",disconnect_category:"connection_lost_or_timed_out",disconnect_origin:"unknown_408",socket_generation:1})),500);setTimeout(()=>{},4000);'
        $emitter = New-Emitter $js
        $run = Start-Observer -ContainerId $emitter.Id
        Start-Sleep -Milliseconds 200; Start-Emitter $emitter
        $done = Wait-Observer $run
        Assert-True ($done.Result.outcome -eq 'CRITICAL_EVENT' -and $done.Result.processed_event_count -eq 2) 'Scenario H duplicate replay'
        Add-Pass 'scenario_H_duplicate_replay' $done.Result
    } finally { Remove-Emitter $emitter }
}

function Invoke-ScenarioI {
    $first = $null; $second = $null
    try {
        $first = New-Emitter 'console.log("noise-one");console.log("noise-two");console.log(JSON.stringify({msg:"baileys.connect_started"}));setTimeout(()=>{},4000);'
        $run = Start-Observer -ContainerId $first.Id -Window 1 -StopOnWindow -Threshold 2
        Start-Sleep -Milliseconds 200; Start-Emitter $first
        $ok = Wait-Observer $run
        Assert-True ($ok.Result.outcome -eq 'WINDOW_COMPLETE' -and $ok.Result.parse_failure_count -eq 2) 'Scenario I allowed noise'
        Remove-Emitter $first; $first = $null
        $second = New-Emitter 'console.log("n1");console.log("n2");console.log("n3");setTimeout(()=>{},4000);'
        $run = Start-Observer -ContainerId $second.Id -Threshold 2
        Start-Sleep -Milliseconds 200; Start-Emitter $second
        $failed = Wait-Observer $run
        Assert-True ($failed.Result.outcome -eq 'OBSERVER_FAILURE' -and $failed.Result.parse_failure_count -eq 3) 'Scenario I threshold'
        Add-Pass 'scenario_I_non_json_threshold' $failed.Result
    } finally { Remove-Emitter $first; Remove-Emitter $second }
}

function Invoke-ScenarioJ {
    $emitter = $null
    try {
        $secret = 'synthetic-secret-marker'
        $js = 'console.log(JSON.stringify({msg:"baileys.reconnect_scheduled",disconnect_category:"connection_lost_or_timed_out",disconnect_origin:"server_408",socket_generation:1,message:"synthetic-secret-marker",stack:"synthetic-secret-marker",cause:"synthetic-secret-marker",data:"synthetic-secret-marker",authorization:"synthetic-secret-marker",qr:"synthetic-secret-marker",jid:"synthetic-secret-marker",phone:"synthetic-secret-marker",session:"synthetic-secret-marker"}));setTimeout(()=>{},3000);'
        $emitter = New-Emitter $js
        $run = Start-Observer -ContainerId $emitter.Id
        Start-Sleep -Milliseconds 200; Start-Emitter $emitter
        $done = Wait-Observer $run
        $serialized = ($done.Result | ConvertTo-Json -Compress) + $done.Console
        Assert-True (-not $serialized.Contains($secret)) 'Scenario J secret leak'
        Add-Pass 'scenario_J_redaction' $done.Result
    } finally { Remove-Emitter $emitter }
}

function Invoke-ScenarioK {
    $emitter = $null; $invalidOriginEmitter = $null
    try {
        $emitter = New-Emitter 'console.log(JSON.stringify({msg:"safe_event"}));'
        $run = Start-Observer -ContainerId $emitter.Id
        Start-Sleep -Milliseconds 200; Start-Emitter $emitter
        $done = Wait-Observer $run
        Assert-True ($done.ExitCode -eq 23 -and $done.Result.outcome -eq 'CONTAINER_EXITED') 'Scenario K exited branch'
        Remove-Emitter $emitter; $emitter = $null
        $invalidOriginEmitter = New-Emitter 'console.log(JSON.stringify({msg:"baileys.reconnect_scheduled",disconnect_category:"connection_lost_or_timed_out",disconnect_origin:"not_applicable"}));setTimeout(()=>{},3000);'
        $invalidRun = Start-Observer -ContainerId $invalidOriginEmitter.Id
        Start-Sleep -Milliseconds 200; Start-Emitter $invalidOriginEmitter
        $invalid = Wait-Observer $invalidRun
        Assert-True ($invalid.Result.outcome -eq 'OBSERVER_FAILURE' -and
            $invalid.Result.event_name -eq 'invalid_disconnect_origin' -and $invalid.Result.stop_confirmed) `
            'Scenario K invalid 408 origin branch'
        $outcomes = @($ScenarioResults.Outcome) + $done.Result.outcome + $invalid.Result.outcome
        foreach ($required in @('CRITICAL_EVENT','START_TIMEOUT','WINDOW_COMPLETE','OBSERVER_FAILURE','IDENTITY_MISMATCH','CONTAINER_EXITED')) {
            Assert-True ($required -in $outcomes) "Scenario K missing branch $required"
        }
        Add-Pass 'scenario_K_failure_branches' $done.Result
    } finally { Remove-Emitter $emitter; Remove-Emitter $invalidOriginEmitter }
}

try {
    foreach ($file in @($Observer, $PSCommandPath)) {
        $tokens = $null; $errors = $null
        [void][Management.Automation.Language.Parser]::ParseFile($file, [ref]$tokens, [ref]$errors)
        Assert-True ($errors.Count -eq 0) "PowerShell parser errors in $file"
    }
    $actual = (Invoke-Docker -Arguments @('image','inspect',$Image,'--format','{{.Id}}')).Output[-1]
    Assert-True ([string]$actual -eq $ImageId) 'Retained classifier image mismatch'
    Invoke-ScenarioA; Invoke-ScenarioB; Invoke-ScenarioC; Invoke-ScenarioD; Invoke-ScenarioE
    Invoke-ScenarioF; Invoke-ScenarioG; Invoke-ScenarioH; Invoke-ScenarioI; Invoke-ScenarioJ; Invoke-ScenarioK
    Write-Output ("observer_suite=pass scenarios={0}" -f $ScenarioResults.Count)
} finally {
    foreach ($id in @($CreatedContainers)) { $null = Invoke-Docker -Arguments @('rm','--force',$id) -AllowFailure }
    foreach ($path in @($TempFiles)) { Remove-Item -LiteralPath $path -Force -ErrorAction SilentlyContinue }
}
