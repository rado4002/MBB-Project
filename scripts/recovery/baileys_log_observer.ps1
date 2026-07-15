[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][ValidatePattern('^[a-f0-9]{64}$')][string]$ContainerId,
    [Parameter(Mandatory = $true)][ValidatePattern('^sha256:[a-f0-9]{64}$')][string]$ExpectedImageId,
    [ValidateRange(1, 300)][int]$StartTimeoutSeconds = 30,
    [ValidateRange(1, 3600)][int]$ObservationSeconds = 120,
    [Parameter(Mandatory = $true)][string]$SafeResultPath,
    [ValidateRange(25, 2000)][int]$PollMilliseconds = 100,
    [ValidateRange(0, 10)][int]$ParseFailureThreshold = 3,
    # Normal-window shutdown belongs to the orchestrator. This switch is for bounded offline emitters.
    [switch]$StopOnWindowComplete,
    [ValidateRange(0, 60000)][int]$TestForceStreamInterruptionAfterMilliseconds = 0
)

$ErrorActionPreference = 'Stop'
$ExitCodes = @{
    WINDOW_COMPLETE   = 0
    CRITICAL_EVENT    = 10
    OBSERVER_FAILURE  = 20
    IDENTITY_MISMATCH = 21
    START_TIMEOUT     = 22
    CONTAINER_EXITED  = 23
}
$AllowedOrigins = @(
    'qr_refs_exhausted', 'keepalive_silence', 'websocket_error',
    'operation_timeout', 'server_408', 'unknown_408'
)
$UnixEpoch = [DateTimeOffset]::Parse('1970-01-01T00:00:00Z')
$CriticalCategories = @(
    'connection_lost_or_timed_out', 'connection_closed', 'unavailable_service',
    'recoverable_close', 'unknown', 'logged_out', 'connection_replaced',
    'bad_session', 'multidevice_mismatch', 'restart_required'
)
$CriticalEvents = @(
    'baileys.socket_open', 'socket_open', 'fastapi_forward_attempted',
    'fastapi_forward_succeeded', 'message_sent'
)
$CriticalText = @($CriticalCategories + $CriticalEvents | Select-Object -Unique)

function Invoke-Docker {
    param([Parameter(Mandatory = $true)][string[]]$Arguments)
    $priorErrorAction = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        $rawOutput = @(& docker @Arguments 2>&1)
        $code = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $priorErrorAction
    }
    $output = @($rawOutput | ForEach-Object {
        if ($_ -is [Management.Automation.ErrorRecord]) { [string]$_.Exception.Message }
        else { [string]$_ }
    })
    [pscustomobject]@{ Code = $code; Output = $output }
}

function Get-ContainerInspection {
    $call = Invoke-Docker -Arguments @('inspect', $ContainerId)
    if ($call.Code -ne 0 -or $call.Output.Count -eq 0) { return $null }
    try {
        $items = @($call.Output -join [Environment]::NewLine | ConvertFrom-Json)
        if ($items.Count -ne 1 -or $items[0].Id -ne $ContainerId) { return $null }
        return $items[0]
    } catch { return $null }
}

function Test-ImageIdentity {
    param([object]$Inspection)
    $null -ne $Inspection -and $Inspection.Image -eq $ExpectedImageId
}

function Stop-TargetBounded {
    $script:StopIssued = $true
    $stop = Invoke-Docker -Arguments @('stop', '--time', '2', $ContainerId)
    $inspection = Get-ContainerInspection
    $script:StopConfirmed = $null -ne $inspection -and $inspection.State.Status -ne 'running'
    if ($stop.Code -ne 0 -and $script:StopConfirmed) { return $true }
    return $stop.Code -eq 0 -and $script:StopConfirmed
}

function Set-Outcome {
    param([string]$Outcome, [string]$EventName = $null)
    if ($null -eq $script:Outcome) {
        $script:Outcome = $Outcome
        $script:EventName = $EventName
        $script:SafeTimestamp = [DateTimeOffset]::UtcNow.ToString('o')
    }
}

function Get-LineKey {
    param([string]$Line)
    $sha = [Security.Cryptography.SHA256]::Create()
    try {
        [Convert]::ToBase64String($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($Line)))
    } finally { $sha.Dispose() }
}

function Process-DirectLine {
    param([string]$Line)
    $script:Stage = 'line_empty_check'
    if ([string]::IsNullOrWhiteSpace($Line)) { return }
    $script:Stage = 'line_deduplication'
    $key = Get-LineKey -Line $Line
    if (-not $script:SeenLines.Add($key)) { return }

    $script:Stage = 'line_timestamp_split'
    $match = [regex]::Match($Line, '^(?<timestamp>\S+)\s+(?<payload>\{.*\})$')
    if (-not $match.Success) {
        foreach ($token in $CriticalText) {
            if ($Line.Contains($token)) {
                Set-Outcome -Outcome 'OBSERVER_FAILURE' -EventName 'malformed_critical_line'
                return
            }
        }
        $script:ParseFailureCount += 1
        if ($script:ParseFailureCount -gt $ParseFailureThreshold) {
            Set-Outcome -Outcome 'OBSERVER_FAILURE' -EventName 'parse_failure_threshold'
        }
        return
    }
    $script:Stage = 'line_timestamp_validation'
    $parsedTimestamp = [DateTimeOffset]::MinValue
    if (-not [DateTimeOffset]::TryParse($match.Groups['timestamp'].Value, [ref]$parsedTimestamp)) {
        Set-Outcome -Outcome 'OBSERVER_FAILURE' -EventName 'invalid_docker_timestamp'
        return
    }
    $script:Stage = 'line_json_parsing'
    try { $event = $match.Groups['payload'].Value | ConvertFrom-Json }
    catch {
        foreach ($token in $CriticalText) {
            if ($match.Groups['payload'].Value.Contains($token)) {
                Set-Outcome -Outcome 'OBSERVER_FAILURE' -EventName 'malformed_critical_json'
                return
            }
        }
        $script:ParseFailureCount += 1
        if ($script:ParseFailureCount -gt $ParseFailureThreshold) {
            Set-Outcome -Outcome 'OBSERVER_FAILURE' -EventName 'parse_failure_threshold'
        }
        return
    }
    $script:Stage = 'line_event_classification'
    $hasMessageField = $event.PSObject.Properties.Name -contains 'msg'
    $msg = if ($hasMessageField) { [string]$event.msg } else { $null }
    $category = if ($event.PSObject.Properties.Name -contains 'disconnect_category') {
        [string]$event.disconnect_category
    } else { $null }

    if ($category -in $CriticalCategories) {
        $script:ProcessedEventCount += 1
        $script:SocketGeneration = $event.socket_generation
        $script:DisconnectCategory = $category
        if ($category -eq 'connection_lost_or_timed_out') {
            $originFieldCount = [regex]::Matches(
                $match.Groups['payload'].Value,
                '"disconnect_origin"\s*:'
            ).Count
            $origin = if ($event.PSObject.Properties.Name -contains 'disconnect_origin') {
                [string]$event.disconnect_origin
            } else { $null }
            if ($originFieldCount -ne 1 -or [string]::IsNullOrWhiteSpace($origin) -or $origin -eq 'not_applicable' -or
                $origin -notin $AllowedOrigins) {
                Set-Outcome -Outcome 'OBSERVER_FAILURE' -EventName 'invalid_disconnect_origin'
                return
            }
            $script:DisconnectOrigin = $origin
        }
        Set-Outcome -Outcome 'CRITICAL_EVENT' -EventName $msg
        return
    }
    if (-not $hasMessageField) { return }
    $script:ProcessedEventCount += 1
    if ($msg -in $CriticalEvents) {
        $script:SocketGeneration = $event.socket_generation
        Set-Outcome -Outcome 'CRITICAL_EVENT' -EventName $msg
    }
}

function Replay-Logs {
    param([string]$StartedAt)
    $script:Stage = 'replay_identity_inspection'
    $identity = Get-ContainerInspection
    if (-not (Test-ImageIdentity -Inspection $identity)) {
        Set-Outcome -Outcome 'IDENTITY_MISMATCH' -EventName 'replay_identity_mismatch'
        return
    }
    $script:Stage = 'replay_docker_call'
    $replay = Invoke-Docker -Arguments @('logs', '--timestamps', '--since', $StartedAt, $ContainerId)
    if ($replay.Code -ne 0) {
        Set-Outcome -Outcome 'OBSERVER_FAILURE' -EventName 'replay_failed'
        return
    }
    $script:Stage = 'replay_line_processing'
    foreach ($line in $replay.Output) { Process-DirectLine -Line ([string]$line) }
}

function Write-SafeResult {
    $duration = [math]::Max(0, [int64](([DateTimeOffset]::UtcNow - $script:ObservationStart).TotalMilliseconds))
    $result = [ordered]@{
        outcome                = $script:Outcome
        exit_code              = $ExitCodes[$script:Outcome]
        safe_timestamp         = $script:SafeTimestamp
        container_id_prefix    = $ContainerId.Substring(0, 12)
        image_verified         = $script:ImageVerified
        event_name             = $script:EventName
        socket_generation      = $script:SocketGeneration
        disconnect_category    = $script:DisconnectCategory
        disconnect_origin      = $script:DisconnectOrigin
        stop_issued            = $script:StopIssued
        stop_confirmed         = $script:StopConfirmed
        observation_duration_ms = $duration
        parse_failure_count    = $script:ParseFailureCount
        processed_event_count  = $script:ProcessedEventCount
    }
    $json = $result | ConvertTo-Json -Compress
    [IO.File]::WriteAllText($SafeResultPath, $json, [Text.UTF8Encoding]::new($false))
}

$script:Outcome = $null
$script:EventName = $null
$script:SafeTimestamp = $null
$script:ImageVerified = $false
$script:SocketGeneration = $null
$script:DisconnectCategory = $null
$script:DisconnectOrigin = $null
$script:StopIssued = $false
$script:StopConfirmed = $false
$script:ParseFailureCount = 0
$script:ProcessedEventCount = 0
$script:LogExitCode = $null
$script:SeenLines = [Collections.Generic.HashSet[string]]::new()
$script:ObservationStart = [DateTimeOffset]::UtcNow
$script:Stage = 'initialization'
$logJob = $null

try {
    $script:Stage = 'initial_inspection'
    $inspection = Get-ContainerInspection
    if ($null -eq $inspection -or -not (Test-ImageIdentity -Inspection $inspection) -or
        $inspection.State.Status -notin @('created', 'running', 'exited', 'dead')) {
        Set-Outcome -Outcome 'IDENTITY_MISMATCH' -EventName 'initial_identity_mismatch'
    } else {
        $script:ImageVerified = $true
    }

    $startDeadline = [DateTimeOffset]::UtcNow.AddSeconds($StartTimeoutSeconds)
    $startedAt = $null
    $script:Stage = 'startup_supervision'
    while ($null -eq $script:Outcome -and $null -eq $startedAt) {
        $script:Stage = 'startup_inspection'
        $inspection = Get-ContainerInspection
        if ($null -eq $inspection -or -not (Test-ImageIdentity -Inspection $inspection)) {
            Set-Outcome -Outcome 'IDENTITY_MISMATCH' -EventName 'startup_identity_mismatch'
            break
        }
        if ($inspection.State.Status -eq 'running') {
            $script:Stage = 'started_at_validation'
            $candidate = [DateTimeOffset]::MinValue
            if (-not [DateTimeOffset]::TryParse([string]$inspection.State.StartedAt, [ref]$candidate) -or
                $candidate -le $UnixEpoch) {
                Set-Outcome -Outcome 'OBSERVER_FAILURE' -EventName 'invalid_started_at'
                break
            }
            $startedAt = $candidate.ToString('o')
            $script:ObservationStart = [DateTimeOffset]::UtcNow
            break
        }
        if ($inspection.State.Status -in @('exited', 'dead')) {
            $script:Stage = 'fast_exit_replay'
            $candidate = [DateTimeOffset]::MinValue
            if ([DateTimeOffset]::TryParse([string]$inspection.State.StartedAt, [ref]$candidate) -and
                $candidate -gt $UnixEpoch) {
                $startedAt = $candidate.ToString('o')
                Replay-Logs -StartedAt $startedAt
                if ($null -eq $script:Outcome) {
                    Set-Outcome -Outcome 'CONTAINER_EXITED' -EventName 'exited_before_attachment'
                }
            } else { Set-Outcome -Outcome 'CONTAINER_EXITED' -EventName 'never_started_exit' }
            break
        }
        if ($inspection.State.Status -ne 'created') {
            Set-Outcome -Outcome 'OBSERVER_FAILURE' -EventName 'unexpected_container_state'
            break
        }
        if ([DateTimeOffset]::UtcNow -ge $startDeadline) {
            Set-Outcome -Outcome 'START_TIMEOUT' -EventName 'start_timeout'
            break
        }
        $script:Stage = 'created_state_wait'
        Start-Sleep -Milliseconds $PollMilliseconds
    }

    if ($null -eq $script:Outcome -and $null -ne $startedAt) {
        $script:Stage = 'initial_replay'
        Replay-Logs -StartedAt $startedAt
        if ($null -eq $script:Outcome) {
            $script:Stage = 'follow_attachment'
            $followIdentity = Get-ContainerInspection
            if (-not (Test-ImageIdentity -Inspection $followIdentity)) {
                Set-Outcome -Outcome 'IDENTITY_MISMATCH' -EventName 'follow_identity_mismatch'
            }
        }
        if ($null -eq $script:Outcome) {
            $logArguments = @('logs', '--timestamps', '--follow', '--since', $startedAt, $ContainerId)
            $logJob = Start-Job -ScriptBlock {
                param([string[]]$DockerArguments)
                $ErrorActionPreference = 'Continue'
                foreach ($item in @(& docker @DockerArguments 2>&1)) {
                    if ($item -is [Management.Automation.ErrorRecord]) {
                        [string]$item.Exception.Message
                    } else {
                        [string]$item
                    }
                }
                [pscustomobject]@{ ObserverDockerExitCode = $LASTEXITCODE }
            } -ArgumentList (,$logArguments)
            $attachTime = [DateTimeOffset]::UtcNow
            $interrupted = $false
            $script:Stage = 'follow_processing'
            while ($null -eq $script:Outcome) {
                foreach ($item in @(Receive-Job -Job $logJob)) {
                    if ($item.PSObject.Properties.Name -contains 'ObserverDockerExitCode') {
                        $script:LogExitCode = [int]$item.ObserverDockerExitCode
                    } else {
                        Process-DirectLine -Line ([string]$item)
                    }
                }
                if ($null -ne $script:Outcome) { break }
                if ($TestForceStreamInterruptionAfterMilliseconds -gt 0 -and -not $interrupted -and
                    ([DateTimeOffset]::UtcNow - $attachTime).TotalMilliseconds -ge $TestForceStreamInterruptionAfterMilliseconds) {
                    Stop-Job -Job $logJob
                    $interrupted = $true
                }
                if ($logJob.State -in @('Completed', 'Failed', 'Stopped')) {
                    Replay-Logs -StartedAt $startedAt
                    if ($null -ne $script:Outcome) { break }
                    $finalState = Get-ContainerInspection
                    if ($null -ne $finalState -and $finalState.State.Status -eq 'running') {
                        Set-Outcome -Outcome 'OBSERVER_FAILURE' -EventName 'running_stream_ended'
                    } else {
                        Set-Outcome -Outcome 'CONTAINER_EXITED' -EventName 'stream_ended_after_exit'
                    }
                    break
                }
                if (([DateTimeOffset]::UtcNow - $script:ObservationStart).TotalSeconds -ge $ObservationSeconds) {
                    Set-Outcome -Outcome 'WINDOW_COMPLETE' -EventName 'window_complete'
                    break
                }
                Start-Sleep -Milliseconds $PollMilliseconds
            }
        }
    }

    if ($script:Outcome -in @('CRITICAL_EVENT', 'OBSERVER_FAILURE')) {
        $script:Stage = 'critical_stop'
        if (-not (Stop-TargetBounded)) {
            $script:Outcome = 'OBSERVER_FAILURE'
            $script:EventName = 'stop_not_confirmed'
        }
    } elseif ($script:Outcome -eq 'WINDOW_COMPLETE' -and $StopOnWindowComplete) {
        if (-not (Stop-TargetBounded)) {
            $script:Outcome = 'OBSERVER_FAILURE'
            $script:EventName = 'window_stop_not_confirmed'
        }
    }
} catch {
    Set-Outcome -Outcome 'OBSERVER_FAILURE' -EventName ("unhandled_{0}" -f $script:Stage)
    $null = Stop-TargetBounded
} finally {
    if ($null -ne $logJob) {
        Stop-Job -Job $logJob -ErrorAction SilentlyContinue
        Remove-Job -Job $logJob -Force -ErrorAction SilentlyContinue
    }
    if ($null -eq $script:Outcome) { Set-Outcome -Outcome 'OBSERVER_FAILURE' -EventName 'missing_outcome' }
    Write-SafeResult
}

exit $ExitCodes[$script:Outcome]
