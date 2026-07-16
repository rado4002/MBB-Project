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
    [ValidateRange(0, 60000)][int]$TestForceStreamInterruptionAfterMilliseconds = 0,
    [switch]$TestHoldFollowDeliveryForFinalReconciliation,
    [switch]$TestSafeDiagnostics,
    [switch]$TestForceAtomicResultWriteFailure
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
# Docker may expose zero log lines for an extremely short-lived container. Evidence that Docker never
# exposes cannot be recovered; after the bounded drain this observer fails closed with CONTAINER_EXITED
# and makes no event-level or transport claim.
$PostExitDrainTimeoutMilliseconds = 3000
$PostExitDrainPollMilliseconds = 100
$PostExitDrainQuiescentAttempts = 2
$StopPollMilliseconds = 50
$StopPollTimeoutMilliseconds = 2000
$FollowCloseSettleMilliseconds = 500
$CriticalCategories = @(
    'connection_lost_or_timed_out', 'connection_closed', 'unavailable_service',
    'recoverable_close', 'unknown', 'logged_out', 'connection_replaced',
    'bad_session', 'multidevice_mismatch', 'restart_required'
)
$CriticalEvents = @(
    'baileys.socket_open', 'socket_open', 'fastapi_forward_attempted',
    'fastapi_forward_succeeded', 'message_sent'
)
$CriticalCategoryEvents = @(
    'baileys.reconnect_skipped', 'baileys.reconnect_failed', 'baileys.reconnect_scheduled',
    'baileys.disconnect_classification_failed', 'baileys.disconnect_terminal'
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

function Receive-FollowJobOutput {
    param(
        [object]$Job,
        [switch]$Discard
    )
    if ($null -eq $Job) { return }
    $pending = @($Job.ChildJobs | ForEach-Object { $_.Output.Count } | Measure-Object -Sum).Sum
    $script:FollowQueueDepth = [math]::Max($script:FollowQueueDepth, [int]$pending)
    foreach ($item in @(Receive-Job -Job $Job)) {
        if ($item.PSObject.Properties.Name -contains 'ObserverDockerExitCode') {
            $script:FollowProcessExitCode = [int]$item.ObserverDockerExitCode
        } else {
            $isStderr = $item.PSObject.Properties.Name -contains 'ObserverStream' -and
                $item.ObserverStream -eq 'stderr'
            if ($isStderr) { $script:FollowStderrLineCount += 1 }
            else { $script:FollowStdoutLineCount += 1 }
            if ($Discard -or $null -ne $script:Outcome -or
                $TestHoldFollowDeliveryForFinalReconciliation) {
                $script:FollowDiscardedLineCount += 1
            } else {
                $script:FollowOutputObserved = $true
                Process-DirectLine -Line ([string]$item.ObserverLine) -Phase 'follow'
            }
        }
    }
    $script:FollowQueueDepth = 0
}

function Close-FollowJob {
    param(
        [object]$Job,
        [switch]$Discard
    )
    if ($null -eq $Job) { return $true }
    if ($script:FollowCloseAttempted) { return $script:FollowCloseConfirmed }
    $script:FollowCloseAttempted = $true
    try {
        if ($Job.State -notin @('Completed', 'Failed', 'Stopped')) {
            Stop-Job -Job $Job -ErrorAction Stop
        }
        Receive-FollowJobOutput -Job $Job -Discard:$Discard
        $terminalJobState = $Job.State -in @('Completed', 'Failed', 'Stopped')
        Remove-Job -Job $Job -Force -ErrorAction Stop
        $removed = $null -eq (Get-Job -Id $Job.Id -ErrorAction SilentlyContinue)
        # This confirms the PowerShell job reader was drained and removed. The offline harness
        # separately verifies that no native docker-logs child remains for the exact container.
        $script:FollowReaderCompleted = $terminalJobState -and $removed
        $script:FollowProcessRunning = $false
        $script:FollowQueueDepth = 0
        $script:FollowCloseConfirmed = $script:FollowReaderCompleted
        return $script:FollowCloseConfirmed
    } catch {
        $script:FollowProcessRunning = $Job.State -notin @('Completed', 'Failed', 'Stopped')
        $script:FollowCloseConfirmed = $false
        return $false
    }
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

function Get-FreshSingleStartInspectionFailure {
    param(
        [object]$Inspection,
        [string[]]$AllowedStates,
        [switch]$RequireValidStartedAt,
        [switch]$RequirePinnedStartedAt
    )
    if ($null -eq $Inspection -or -not (Test-ImageIdentity -Inspection $Inspection)) {
        return 'identity_mismatch'
    }

    $hostConfigProperty = @($Inspection.PSObject.Properties | Where-Object { $_.Name -eq 'HostConfig' })
    if ($hostConfigProperty.Count -ne 1 -or $null -eq $hostConfigProperty[0].Value) {
        return 'restart_policy_metadata_invalid'
    }
    $restartPolicyProperty = @(
        $hostConfigProperty[0].Value.PSObject.Properties | Where-Object { $_.Name -eq 'RestartPolicy' }
    )
    if ($restartPolicyProperty.Count -ne 1 -or $null -eq $restartPolicyProperty[0].Value) {
        return 'restart_policy_metadata_invalid'
    }
    $restartNameProperty = @(
        $restartPolicyProperty[0].Value.PSObject.Properties | Where-Object { $_.Name -eq 'Name' }
    )
    if ($restartNameProperty.Count -ne 1 -or
        $restartNameProperty[0].Value -isnot [string] -or
        [string]$restartNameProperty[0].Value -ne 'no') {
        return 'restart_policy_invalid'
    }

    $restartCountProperty = @(
        $Inspection.PSObject.Properties | Where-Object { $_.Name -eq 'RestartCount' }
    )
    if ($restartCountProperty.Count -ne 1 -or $null -eq $restartCountProperty[0].Value -or
        $restartCountProperty[0].Value.GetType().FullName -notin @(
            'System.Byte', 'System.SByte', 'System.Int16', 'System.UInt16',
            'System.Int32', 'System.UInt32', 'System.Int64'
        ) -or [int64]$restartCountProperty[0].Value -ne 0) {
        return 'restart_count_invalid'
    }

    $stateProperty = @($Inspection.PSObject.Properties | Where-Object { $_.Name -eq 'State' })
    if ($stateProperty.Count -ne 1 -or $null -eq $stateProperty[0].Value) {
        return 'state_metadata_invalid'
    }
    $statusProperty = @(
        $stateProperty[0].Value.PSObject.Properties | Where-Object { $_.Name -eq 'Status' }
    )
    if ($statusProperty.Count -ne 1 -or $statusProperty[0].Value -isnot [string]) {
        return 'state_metadata_invalid'
    }
    $state = [string]$statusProperty[0].Value
    if ($state -notin $AllowedStates) {
        return 'invalid_state'
    }
    if ($RequireValidStartedAt -or $RequirePinnedStartedAt) {
        $startedAtProperty = @(
            $stateProperty[0].Value.PSObject.Properties | Where-Object { $_.Name -eq 'StartedAt' }
        )
        $candidate = [DateTimeOffset]::MinValue
        if ($startedAtProperty.Count -ne 1 -or $startedAtProperty[0].Value -isnot [string] -or
            -not [DateTimeOffset]::TryParse([string]$startedAtProperty[0].Value, [ref]$candidate) -or
            $candidate -le $UnixEpoch) {
            return 'started_at_invalid'
        }
        if ($RequirePinnedStartedAt -and ($null -eq $script:PinnedStartedAt -or
            $candidate.ToUniversalTime().Ticks -ne $script:PinnedStartedAt.Ticks)) {
            return 'started_at_changed'
        }
    }
    return $null
}

function Confirm-FreshSingleStartInspection {
    param(
        [object]$Inspection,
        [string[]]$AllowedStates,
        [string]$EventPrefix,
        [switch]$RequireValidStartedAt,
        [switch]$RequirePinnedStartedAt
    )
    $failure = Get-FreshSingleStartInspectionFailure -Inspection $Inspection `
        -AllowedStates $AllowedStates -RequireValidStartedAt:$RequireValidStartedAt `
        -RequirePinnedStartedAt:$RequirePinnedStartedAt
    if ($null -ne $failure) {
        $outcome = if ($failure -eq 'identity_mismatch') { 'IDENTITY_MISMATCH' } else { 'OBSERVER_FAILURE' }
        Set-Outcome -Outcome $outcome -EventName ("{0}_{1}" -f $EventPrefix, $failure)
        return $false
    }
    return $true
}

function Stop-TargetBounded {
    if ($script:StopActionCount -ge 1) { return $script:StopConfirmed }

    $beforeStop = Get-ContainerInspection
    $beforeFailure = Get-FreshSingleStartInspectionFailure -Inspection $beforeStop `
        -AllowedStates @('created', 'running', 'exited', 'dead') `
        -RequirePinnedStartedAt:($null -ne $script:PinnedStartedAt)
    if ($null -ne $beforeFailure) {
        $script:ContainerStateBeforeStop = $null
        $script:StopResolution = $beforeFailure
        return $false
    }
    $script:ContainerStateBeforeStop = [string]$beforeStop.State.Status

    $script:StopActionCount = 1
    $script:StopIssued = $true
    $stop = Invoke-Docker -Arguments @('stop', '--time', '2', $ContainerId)
    $script:StopCommandExitCode = $stop.Code

    $deadline = [DateTimeOffset]::UtcNow.AddMilliseconds($StopPollTimeoutMilliseconds)
    do {
        $script:StopPollAttempts += 1
        $inspection = Get-ContainerInspection
        if ($null -ne $inspection) {
            $pollFailure = Get-FreshSingleStartInspectionFailure -Inspection $inspection `
                -AllowedStates @('created', 'running', 'exited', 'dead') `
                -RequirePinnedStartedAt:($null -ne $script:PinnedStartedAt)
            if ($null -ne $pollFailure) {
                $script:StopResolution = $pollFailure
                return $false
            }
            $state = [string]$inspection.State.Status
            $script:ContainerStateAfterStop = $state
            $safeCreatedState = $state -eq 'created' -and $script:ContainerStateBeforeStop -eq 'created'
            if ($state -in @('exited', 'dead') -or $safeCreatedState) {
                $script:StopConfirmed = $true
                if ($script:ContainerStateBeforeStop -in @('exited', 'dead')) {
                    $script:StopResolution = 'natural_exit'
                } elseif ($safeCreatedState) {
                    $script:StopResolution = 'never_started'
                } elseif ($stop.Code -eq 0) {
                    $script:StopResolution = 'stop_succeeded_nonrunning'
                } else {
                    $script:StopResolution = 'natural_exit_race'
                }
                return $true
            }
            if ($state -ne 'running') {
                $script:StopResolution = 'ambiguous_state'
                return $false
            }
        }
        if ([DateTimeOffset]::UtcNow -lt $deadline) {
            Start-Sleep -Milliseconds $StopPollMilliseconds
        }
    } while ([DateTimeOffset]::UtcNow -lt $deadline)

    $script:StopResolution = 'poll_timeout'
    return $false
}

function Set-Outcome {
    param([string]$Outcome, [string]$EventName = $null)
    if ($null -eq $script:Outcome) {
        $script:Outcome = $Outcome
        $script:OutcomeSetCount += 1
        $script:EventName = $EventName
        $script:SafeTimestamp = [DateTimeOffset]::UtcNow.ToString('o')
    } else {
        $script:RejectedOutcomeSetCount += 1
    }
}

function Get-LineKey {
    param([string]$Line)
    $sha = [Security.Cryptography.SHA256]::Create()
    try {
        [Convert]::ToBase64String($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($Line)))
    } finally { $sha.Dispose() }
}

function Get-SafeSocketGeneration {
    param(
        [object]$Event,
        [switch]$Required
    )
    $properties = @($Event.PSObject.Properties | Where-Object { $_.Name -eq 'socket_generation' })
    if ($properties.Count -eq 0 -and -not $Required) { return $null }
    if ($properties.Count -ne 1 -or $null -eq $properties[0].Value) { return $null }
    $value = $properties[0].Value
    if ($value.GetType().FullName -notin @(
        'System.Byte', 'System.SByte', 'System.Int16', 'System.UInt16',
        'System.Int32', 'System.UInt32', 'System.Int64'
    )) { return $null }
    $generation = [int64]$value
    if ($generation -lt 0 -or $generation -gt [int32]::MaxValue) { return $null }
    return $generation
}

function Process-DirectLine {
    param(
        [string]$Line,
        [ValidateSet('initial_replay', 'follow', 'post_exit_drain', 'stream_recovery')][string]$Phase = 'follow'
    )
    if ($null -ne $script:Outcome) {
        $script:IgnoredAfterTerminalCount += 1
        return
    }
    $script:Stage = 'line_empty_check'
    if ([string]::IsNullOrWhiteSpace($Line)) { return }
    $script:ReceivedLineCount += 1
    $script:Stage = 'line_deduplication'
    $key = Get-LineKey -Line $Line
    if (-not $script:SeenLines.Add($key)) {
        $script:DuplicateLineCount += 1
        return
    }
    if ($Phase -eq 'initial_replay') {
        $script:InitialReplayUniqueLines += 1
    } elseif ($Phase -eq 'post_exit_drain') {
        $script:PostExitDrainUniqueLines += 1
    }

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
    $script:UniqueParsedEventCount += 1
    switch ($Phase) {
        'initial_replay' { $script:InitialReplayValidEventCount += 1 }
        'follow' { $script:FollowValidEventCount += 1 }
        'stream_recovery' { $script:FinalReplayValidEventCount += 1 }
    }
    $script:Stage = 'line_event_classification'
    $hasMessageField = $event.PSObject.Properties.Name -contains 'msg'
    $msg = if ($hasMessageField) { [string]$event.msg } else { $null }
    $category = if ($event.PSObject.Properties.Name -contains 'disconnect_category') {
        [string]$event.disconnect_category
    } else { $null }

    if ($event.PSObject.Properties.Name -contains 'sequence') {
        $sequenceValue = $event.sequence
        if ($null -ne $sequenceValue -and $sequenceValue.GetType().FullName -in @(
            'System.Byte', 'System.SByte', 'System.Int16', 'System.UInt16',
            'System.Int32', 'System.UInt32', 'System.Int64'
        )) {
            $sequence = [int64]$sequenceValue
            if ($sequence -ge 0 -and $sequence -le [int32]::MaxValue) {
                if ($null -eq $script:ObserverFirstSequence) { $script:ObserverFirstSequence = $sequence }
                $script:ObserverLastSequence = $sequence
            }
        }
    }

    if ($hasMessageField) {
        switch ($msg) {
            'baileys.connect_started' { $script:ConnectStartedCount += 1 }
            'connect_started' { $script:ConnectStartedCount += 1 }
            'qr_code_generated' { $script:QrCodeGeneratedCount += 1 }
            'later_marker' { $script:LaterMarkerCount += 1 }
        }
    }

    if ($category -in $CriticalCategories) {
        if (-not $hasMessageField -or $msg -notin $CriticalCategoryEvents) {
            Set-Outcome -Outcome 'OBSERVER_FAILURE' -EventName 'invalid_critical_event_name'
            return
        }
        $safeGeneration = Get-SafeSocketGeneration -Event $event -Required
        if ($null -eq $safeGeneration) {
            Set-Outcome -Outcome 'OBSERVER_FAILURE' -EventName 'invalid_socket_generation'
            return
        }
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
        $script:SocketGeneration = $safeGeneration
        $script:DisconnectCategory = $category
        $script:CriticalEventCount += 1
        $script:CriticalEventPhase = $Phase
        Set-Outcome -Outcome 'CRITICAL_EVENT' -EventName $msg
        return
    }
    if (-not $hasMessageField) { return }
    if ($msg -in $CriticalEvents) {
        $safeGeneration = Get-SafeSocketGeneration -Event $event
        if ($event.PSObject.Properties.Name -contains 'socket_generation' -and
            $null -eq $safeGeneration) {
            Set-Outcome -Outcome 'OBSERVER_FAILURE' -EventName 'invalid_socket_generation'
            return
        }
        $script:CriticalEventCount += 1
        $script:SocketGeneration = $safeGeneration
        $script:CriticalEventPhase = $Phase
        Set-Outcome -Outcome 'CRITICAL_EVENT' -EventName $msg
    } else {
        $script:SafeEventCount += 1
    }
}

function Replay-Logs {
    param(
        [ValidateSet('initial_replay', 'post_exit_drain', 'stream_recovery')][string]$Phase
    )
    if ($null -ne $script:Outcome) { return }
    if ($Phase -eq 'initial_replay') { $script:InitialReplayAttempted = $true }
    if ($Phase -eq 'stream_recovery') { $script:FinalReplayAttempted = $true }
    $script:Stage = 'replay_identity_inspection'
    $identity = Get-ContainerInspection
    if (-not (Confirm-FreshSingleStartInspection -Inspection $identity `
        -AllowedStates @('running', 'exited', 'dead') -EventPrefix 'replay' `
        -RequirePinnedStartedAt)) { return }
    $script:Stage = 'replay_docker_call'
    $replay = Invoke-Docker -Arguments @('logs', '--timestamps', $ContainerId)
    if ($replay.Code -ne 0) {
        Set-Outcome -Outcome 'OBSERVER_FAILURE' -EventName 'replay_failed'
        return
    }
    $script:Stage = 'replay_line_processing'
    foreach ($line in $replay.Output) {
        if ($null -ne $script:Outcome) { break }
        Process-DirectLine -Line ([string]$line) -Phase $Phase
        if ($null -ne $script:Outcome) { break }
    }
    if ($Phase -eq 'stream_recovery') { $script:FinalReplayCompleted = $true }
}

function Invoke-PostExitLogDrain {
    if ($null -ne $script:Outcome) { return $false }
    if ($script:PostExitDrainStarted) { return $script:PostExitDrainCompleted }

    $script:PostExitDrainStarted = $true
    $drainStart = [DateTimeOffset]::UtcNow
    $deadline = $drainStart.AddMilliseconds($PostExitDrainTimeoutMilliseconds)
    $sawUniqueLine = $false
    $consecutiveEmptyAttempts = 0
    try {
        while ($null -eq $script:Outcome -and [DateTimeOffset]::UtcNow -le $deadline) {
            $script:Stage = 'post_exit_drain_identity_inspection'
            $inspection = Get-ContainerInspection
            if (-not (Confirm-FreshSingleStartInspection -Inspection $inspection `
                -AllowedStates @('exited', 'dead') -EventPrefix 'post_exit_drain' `
                -RequirePinnedStartedAt)) { return $false }
            $state = [string]$inspection.State.Status
            $script:FinalContainerState = $state
            if ($state -notin @('exited', 'dead')) {
                Set-Outcome -Outcome 'OBSERVER_FAILURE' -EventName 'post_exit_drain_invalid_state'
                return $false
            }

            $script:PostExitDrainAttempts += 1
            $uniqueBefore = $script:PostExitDrainUniqueLines
            Replay-Logs -Phase 'post_exit_drain'
            if ($null -ne $script:Outcome) { return $false }
            $uniqueAdded = $script:PostExitDrainUniqueLines - $uniqueBefore
            if ($uniqueAdded -gt 0) {
                $sawUniqueLine = $true
                $consecutiveEmptyAttempts = 0
            } elseif ($sawUniqueLine) {
                $consecutiveEmptyAttempts += 1
                if ($consecutiveEmptyAttempts -ge $PostExitDrainQuiescentAttempts) {
                    $script:PostExitQuiescenceReached = $true
                    break
                }
            }

            $remainingMilliseconds = [int][math]::Floor(
                ($deadline - [DateTimeOffset]::UtcNow).TotalMilliseconds
            )
            if ($remainingMilliseconds -le 0) { break }
            Start-Sleep -Milliseconds ([math]::Min(
                $PostExitDrainPollMilliseconds, $remainingMilliseconds
            ))
        }

        $script:Stage = 'post_exit_drain_final_inspection'
        $finalInspection = Get-ContainerInspection
        if (-not (Confirm-FreshSingleStartInspection -Inspection $finalInspection `
            -AllowedStates @('exited', 'dead') -EventPrefix 'post_exit_drain_final' `
            -RequirePinnedStartedAt)) { return $false }
        $script:FinalContainerState = [string]$finalInspection.State.Status
        if ($script:FinalContainerState -notin @('exited', 'dead')) {
            Set-Outcome -Outcome 'OBSERVER_FAILURE' -EventName 'post_exit_drain_final_state_invalid'
            return $false
        }
        $script:PostExitDrainCompleted = $true
        return $true
    } finally {
        $script:PostExitDrainElapsedMilliseconds = [math]::Max(
            0, [int64](([DateTimeOffset]::UtcNow - $drainStart).TotalMilliseconds)
        )
    }
}

function Write-SafeResult {
    $duration = [math]::Max(0, [int64](([DateTimeOffset]::UtcNow - $script:ObservationStart).TotalMilliseconds))
    $result = [ordered]@{
        outcome                = $script:Outcome
        exit_code              = if ($null -ne $script:ExitCodeOverride) {
            $script:ExitCodeOverride
        } else { $ExitCodes[$script:Outcome] }
        safe_timestamp         = $script:SafeTimestamp
        image_verified         = $script:ImageVerified
        event_name             = $script:EventName
        socket_generation      = $script:SocketGeneration
        disconnect_category    = $script:DisconnectCategory
        disconnect_origin      = $script:DisconnectOrigin
        stop_issued            = $script:StopIssued
        stop_confirmed         = $script:StopConfirmed
        observation_duration_ms = $duration
        parse_failure_count    = $script:ParseFailureCount
        # Compatibility alias: unique classified critical plus accepted non-critical events.
        processed_event_count  = $script:CriticalEventCount + $script:SafeEventCount
        result_written         = $true
        result_atomic          = $true
    }
    if ($TestSafeDiagnostics) {
        $result.stop_command_exit_code = $script:StopCommandExitCode
        $result.container_state_before_stop = $script:ContainerStateBeforeStop
        $result.container_state_after_stop = $script:ContainerStateAfterStop
        $result.stop_action_count = $script:StopActionCount
        $result.received_line_count = $script:ReceivedLineCount
        $result.unique_parsed_event_count = $script:UniqueParsedEventCount
        $result.duplicate_line_count = $script:DuplicateLineCount
        $result.critical_event_count = $script:CriticalEventCount
        $result.critical_event_phase = $script:CriticalEventPhase
        $result.safe_event_count = $script:SafeEventCount
        $result.ignored_after_terminal_count = $script:IgnoredAfterTerminalCount
        $result.connect_started_count = $script:ConnectStartedCount
        $result.qr_code_generated_count = $script:QrCodeGeneratedCount
        $result.later_marker_count = $script:LaterMarkerCount
        $result.started_at_validated = $script:StartedAtValidated
        $result.initial_replay_attempted = $script:InitialReplayAttempted
        $result.initial_replay_unique_lines = $script:InitialReplayUniqueLines
        $result.follow_started = $script:FollowStarted
        $result.follow_process_started = $script:FollowProcessStarted
        $result.follow_process_id = $script:FollowProcessId
        $result.follow_process_running = $script:FollowProcessRunning
        $result.follow_process_exit_code = $script:FollowProcessExitCode
        $result.follow_stdout_line_count = $script:FollowStdoutLineCount
        $result.follow_stderr_line_count = $script:FollowStderrLineCount
        $result.follow_queue_depth = $script:FollowQueueDepth
        $result.follow_reader_started = $script:FollowReaderStarted
        $result.follow_reader_completed = $script:FollowReaderCompleted
        $result.follow_output_observed = $script:FollowOutputObserved
        $result.follow_queue_overflow = $script:FollowQueueOverflow
        $result.follow_discarded_line_count = $script:FollowDiscardedLineCount
        $result.follow_close_attempted = $script:FollowCloseAttempted
        $result.follow_close_confirmed = $script:FollowCloseConfirmed
        $result.observer_first_sequence = $script:ObserverFirstSequence
        $result.observer_last_sequence = $script:ObserverLastSequence
        $result.initial_replay_valid_event_count = $script:InitialReplayValidEventCount
        $result.follow_valid_event_count = $script:FollowValidEventCount
        $result.final_replay_valid_event_count = $script:FinalReplayValidEventCount
        $result.final_replay_attempted = $script:FinalReplayAttempted
        $result.final_replay_completed = $script:FinalReplayCompleted
        $result.outcome_set_count = $script:OutcomeSetCount
        $result.rejected_outcome_set_count = $script:RejectedOutcomeSetCount
        $result.follow_exit_observed = $script:FollowExitObserved
        $result.container_state_at_follow_exit = $script:ContainerStateAtFollowExit
        $result.post_exit_drain_started = $script:PostExitDrainStarted
        $result.post_exit_drain_attempts = $script:PostExitDrainAttempts
        $result.post_exit_drain_unique_lines = $script:PostExitDrainUniqueLines
        $result.post_exit_drain_elapsed_ms = $script:PostExitDrainElapsedMilliseconds
        $result.post_exit_quiescence_reached = $script:PostExitQuiescenceReached
        $result.final_container_state = $script:FinalContainerState
        $result.stop_poll_attempts = $script:StopPollAttempts
        $result.stop_resolution = $script:StopResolution
    }
    $json = $result | ConvertTo-Json -Compress
    $fullResultPath = [IO.Path]::GetFullPath($SafeResultPath)
    $resultDirectory = [IO.Path]::GetDirectoryName($fullResultPath)
    if (-not [IO.Directory]::Exists($resultDirectory)) { throw 'Safe result directory does not exist' }
    $temporaryResultPath = Join-Path $resultDirectory (
        '{0}.{1}.tmp' -f [IO.Path]::GetFileName($fullResultPath), [guid]::NewGuid().ToString('N')
    )
    $stream = $null
    try {
        $bytes = [Text.UTF8Encoding]::new($false).GetBytes($json)
        $stream = [IO.File]::Open(
            $temporaryResultPath, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None
        )
        $stream.Write($bytes, 0, $bytes.Length)
        $stream.Flush($true)
        $stream.Dispose()
        $stream = $null
        if ($TestForceAtomicResultWriteFailure) { throw 'Test-only atomic result finalization failure' }
        [IO.File]::Move($temporaryResultPath, $fullResultPath)
    } finally {
        if ($null -ne $stream) { $stream.Dispose() }
        Remove-Item -LiteralPath $temporaryResultPath -Force -ErrorAction SilentlyContinue
    }
}

$script:Outcome = $null
$script:OutcomeSetCount = 0
$script:RejectedOutcomeSetCount = 0
$script:EventName = $null
$script:SafeTimestamp = $null
$script:ImageVerified = $false
$script:SocketGeneration = $null
$script:DisconnectCategory = $null
$script:DisconnectOrigin = $null
$script:StopIssued = $false
$script:StopConfirmed = $false
$script:ParseFailureCount = 0
$script:ExitCodeOverride = $null
$script:ResultWriteFailed = $false
$script:StopCommandExitCode = $null
$script:ContainerStateBeforeStop = $null
$script:ContainerStateAfterStop = $null
$script:StopActionCount = 0
$script:ReceivedLineCount = 0
$script:UniqueParsedEventCount = 0
$script:DuplicateLineCount = 0
$script:CriticalEventCount = 0
$script:CriticalEventPhase = $null
$script:SafeEventCount = 0
$script:IgnoredAfterTerminalCount = 0
$script:ConnectStartedCount = 0
$script:QrCodeGeneratedCount = 0
$script:LaterMarkerCount = 0
$script:StartedAtValidated = $false
$script:InitialReplayAttempted = $false
$script:InitialReplayUniqueLines = 0
$script:FollowStarted = $false
$script:FollowProcessStarted = $false
$script:FollowProcessId = $null
$script:FollowProcessRunning = $false
$script:FollowProcessExitCode = $null
$script:FollowStdoutLineCount = 0
$script:FollowStderrLineCount = 0
$script:FollowQueueDepth = 0
$script:FollowReaderStarted = $false
$script:FollowReaderCompleted = $false
$script:FollowOutputObserved = $false
$script:FollowQueueOverflow = $false
$script:FollowDiscardedLineCount = 0
$script:FollowCloseAttempted = $false
$script:FollowCloseConfirmed = $false
$script:ObserverFirstSequence = $null
$script:ObserverLastSequence = $null
$script:InitialReplayValidEventCount = 0
$script:FollowValidEventCount = 0
$script:FinalReplayValidEventCount = 0
$script:FinalReplayAttempted = $false
$script:FinalReplayCompleted = $false
$script:FollowExitObserved = $false
$script:ContainerStateAtFollowExit = $null
$script:PostExitDrainStarted = $false
$script:PostExitDrainCompleted = $false
$script:PostExitDrainAttempts = 0
$script:PostExitDrainUniqueLines = 0
$script:PostExitDrainElapsedMilliseconds = 0
$script:PostExitQuiescenceReached = $false
$script:FinalContainerState = $null
$script:StopPollAttempts = 0
$script:StopResolution = $null
$script:SeenLines = [Collections.Generic.HashSet[string]]::new()
$script:ObservationStart = [DateTimeOffset]::UtcNow
$script:Stage = 'initialization'
$script:PinnedStartedAt = $null
$logJob = $null
$observationDeadline = $null

try {
    $script:Stage = 'initial_inspection'
    $inspection = Get-ContainerInspection
    if (Confirm-FreshSingleStartInspection -Inspection $inspection `
        -AllowedStates @('created', 'running', 'exited', 'dead') -EventPrefix 'initial') {
        $script:ImageVerified = $true
    }

    $startDeadline = [DateTimeOffset]::UtcNow.AddSeconds($StartTimeoutSeconds)
    $startedAt = $null
    $script:Stage = 'startup_supervision'
    while ($null -eq $script:Outcome -and $null -eq $startedAt) {
        $script:Stage = 'startup_inspection'
        $inspection = Get-ContainerInspection
        if (-not (Confirm-FreshSingleStartInspection -Inspection $inspection `
            -AllowedStates @('created', 'running', 'exited', 'dead') -EventPrefix 'startup')) { break }
        if ($inspection.State.Status -eq 'running') {
            $script:Stage = 'started_at_validation'
            if (-not (Confirm-FreshSingleStartInspection -Inspection $inspection `
                -AllowedStates @('running') -EventPrefix 'startup_started_at' `
                -RequireValidStartedAt)) { break }
            $candidate = [DateTimeOffset]::MinValue
            [void][DateTimeOffset]::TryParse([string]$inspection.State.StartedAt, [ref]$candidate)
            $script:PinnedStartedAt = $candidate.ToUniversalTime()
            $startedAt = $script:PinnedStartedAt.ToString(
                'o', [Globalization.CultureInfo]::InvariantCulture
            )
            $script:StartedAtValidated = $true
            $script:ObservationStart = [DateTimeOffset]::UtcNow
            $observationDeadline = $script:ObservationStart.AddSeconds($ObservationSeconds)
            break
        }
        if ($inspection.State.Status -in @('exited', 'dead')) {
            $script:Stage = 'fast_exit_replay'
            if (-not (Confirm-FreshSingleStartInspection -Inspection $inspection `
                -AllowedStates @('exited', 'dead') -EventPrefix 'fast_exit_started_at' `
                -RequireValidStartedAt)) { break }
            $candidate = [DateTimeOffset]::MinValue
            [void][DateTimeOffset]::TryParse([string]$inspection.State.StartedAt, [ref]$candidate)
            $script:PinnedStartedAt = $candidate.ToUniversalTime()
            $startedAt = $script:PinnedStartedAt.ToString(
                'o', [Globalization.CultureInfo]::InvariantCulture
            )
            $script:StartedAtValidated = $true
            Replay-Logs -Phase 'initial_replay'
            if ($null -eq $script:Outcome) {
                $drainCompleted = Invoke-PostExitLogDrain
                if ($null -eq $script:Outcome -and $drainCompleted) {
                    Set-Outcome -Outcome 'CONTAINER_EXITED' -EventName 'exited_before_attachment'
                } elseif ($null -eq $script:Outcome) {
                    Set-Outcome -Outcome 'OBSERVER_FAILURE' -EventName 'post_exit_drain_incomplete'
                }
            }
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
        Replay-Logs -Phase 'initial_replay'
        $followAllowed = $false
        if ($null -eq $script:Outcome) {
            $script:Stage = 'follow_attachment'
            $followIdentity = Get-ContainerInspection
            if (-not (Confirm-FreshSingleStartInspection -Inspection $followIdentity `
                -AllowedStates @('running', 'exited', 'dead') -EventPrefix 'follow' `
                -RequirePinnedStartedAt)) {
                $followAllowed = $false
            } elseif ($followIdentity.State.Status -eq 'running') {
                $followAllowed = $true
            } elseif ($followIdentity.State.Status -in @('exited', 'dead')) {
                $script:FinalContainerState = [string]$followIdentity.State.Status
                $drainCompleted = Invoke-PostExitLogDrain
                if ($null -eq $script:Outcome -and $drainCompleted) {
                    Set-Outcome -Outcome 'CONTAINER_EXITED' -EventName 'exited_before_follow_attachment'
                } elseif ($null -eq $script:Outcome) {
                    Set-Outcome -Outcome 'OBSERVER_FAILURE' -EventName 'post_exit_drain_incomplete'
                }
            } else {
                Set-Outcome -Outcome 'OBSERVER_FAILURE' -EventName 'follow_attachment_invalid_state'
            }
        }
        if ($null -eq $script:Outcome -and $followAllowed) {
            $logArguments = @('logs', '--timestamps', '--follow', $ContainerId)
            $logJob = Start-Job -ScriptBlock {
                param([string[]]$DockerArguments)
                $ErrorActionPreference = 'Continue'
                & docker @DockerArguments 2>&1 | ForEach-Object {
                    $item = $_
                    if ($item -is [Management.Automation.ErrorRecord]) {
                        [pscustomobject]@{
                            ObserverStream = 'stderr'
                            ObserverLine = [string]$item.Exception.Message
                        }
                    } else {
                        [pscustomobject]@{ ObserverStream = 'stdout'; ObserverLine = [string]$item }
                    }
                }
                [pscustomobject]@{ ObserverDockerExitCode = $LASTEXITCODE }
            } -ArgumentList (,$logArguments)
            $script:FollowStarted = $true
            $script:FollowProcessStarted = $true
            # A PowerShell background job does not expose its native docker.exe child PID safely.
            $script:FollowProcessId = $null
            $script:FollowProcessRunning = $logJob.State -notin @('Completed', 'Failed', 'Stopped')
            $script:FollowReaderStarted = $true
            $attachTime = [DateTimeOffset]::UtcNow
            $interrupted = $false
            $script:Stage = 'follow_processing'
            while ($null -eq $script:Outcome) {
                Receive-FollowJobOutput -Job $logJob
                if ($null -ne $script:Outcome) { break }
                if ($TestForceStreamInterruptionAfterMilliseconds -gt 0 -and -not $interrupted -and
                    ([DateTimeOffset]::UtcNow - $attachTime).TotalMilliseconds -ge $TestForceStreamInterruptionAfterMilliseconds) {
                    $interrupted = $true
                    $script:FollowExitObserved = $true
                    $closed = Close-FollowJob -Job $logJob
                    $logJob = $null
                    if (-not $closed) {
                        Set-Outcome -Outcome 'OBSERVER_FAILURE' -EventName 'follow_close_unconfirmed'
                        break
                    }
                    $finalState = Get-ContainerInspection
                    if (-not (Confirm-FreshSingleStartInspection -Inspection $finalState `
                        -AllowedStates @('running', 'exited', 'dead') -EventPrefix 'follow_exit' `
                        -RequirePinnedStartedAt)) {
                        # Contract helper fixed the fail-closed outcome.
                    } elseif ($finalState.State.Status -eq 'running') {
                        $script:ContainerStateAtFollowExit = 'running'
                        Set-Outcome -Outcome 'OBSERVER_FAILURE' -EventName 'running_stream_ended'
                    } elseif ($finalState.State.Status -in @('exited', 'dead')) {
                        $script:ContainerStateAtFollowExit = [string]$finalState.State.Status
                        $script:FinalContainerState = [string]$finalState.State.Status
                        $drainCompleted = Invoke-PostExitLogDrain
                        if ($null -eq $script:Outcome -and $drainCompleted) {
                            Set-Outcome -Outcome 'CONTAINER_EXITED' -EventName 'stream_ended_after_exit'
                        } elseif ($null -eq $script:Outcome) {
                            Set-Outcome -Outcome 'OBSERVER_FAILURE' -EventName 'post_exit_drain_incomplete'
                        }
                    } else {
                        $script:ContainerStateAtFollowExit = [string]$finalState.State.Status
                        Set-Outcome -Outcome 'OBSERVER_FAILURE' -EventName 'follow_exit_invalid_state'
                    }
                    break
                }
                if ($logJob.State -in @('Completed', 'Failed', 'Stopped')) {
                    $script:FollowExitObserved = $true
                    $closed = Close-FollowJob -Job $logJob
                    $logJob = $null
                    if (-not $closed) {
                        Set-Outcome -Outcome 'OBSERVER_FAILURE' -EventName 'follow_close_unconfirmed'
                        break
                    }
                    $finalState = Get-ContainerInspection
                    if (-not (Confirm-FreshSingleStartInspection -Inspection $finalState `
                        -AllowedStates @('running', 'exited', 'dead') -EventPrefix 'follow_exit' `
                        -RequirePinnedStartedAt)) {
                        # Contract helper fixed the fail-closed outcome.
                    } elseif ($finalState.State.Status -eq 'running') {
                        $script:ContainerStateAtFollowExit = 'running'
                        Set-Outcome -Outcome 'OBSERVER_FAILURE' -EventName 'running_stream_ended'
                    } elseif ($finalState.State.Status -in @('exited', 'dead')) {
                        $script:ContainerStateAtFollowExit = [string]$finalState.State.Status
                        $script:FinalContainerState = [string]$finalState.State.Status
                        $drainCompleted = Invoke-PostExitLogDrain
                        if ($null -eq $script:Outcome -and $drainCompleted) {
                            Set-Outcome -Outcome 'CONTAINER_EXITED' -EventName 'stream_ended_after_exit'
                        } elseif ($null -eq $script:Outcome) {
                            Set-Outcome -Outcome 'OBSERVER_FAILURE' -EventName 'post_exit_drain_incomplete'
                        }
                    } else {
                        $script:ContainerStateAtFollowExit = [string]$finalState.State.Status
                        Set-Outcome -Outcome 'OBSERVER_FAILURE' -EventName 'follow_exit_invalid_state'
                    }
                    break
                }
                if ([DateTimeOffset]::UtcNow -ge $observationDeadline) {
                    # Close the direct producer at the host deadline, then reconcile every unique line
                    # Docker exposes during the fixed settle. Conservative post-deadline evidence may
                    # prevent WINDOW_COMPLETE, but Docker-visible critical evidence is never discarded.
                    $script:Stage = 'window_follow_finalization'
                    $closed = Close-FollowJob -Job $logJob
                    $logJob = $null
                    if (-not $closed) {
                        Set-Outcome -Outcome 'OBSERVER_FAILURE' -EventName 'follow_close_unconfirmed'
                        break
                    }
                    if ($null -ne $script:Outcome) { break }
                    # Allow the confirmed-closed Docker CLI reader to release its engine attachment
                    # before the single bounded reconciliation replay.
                    Start-Sleep -Milliseconds $FollowCloseSettleMilliseconds
                    Replay-Logs -Phase 'stream_recovery'
                    if ($null -ne $script:Outcome) { break }

                    $script:Stage = 'window_final_state_inspection'
                    $windowState = Get-ContainerInspection
                    if (-not (Confirm-FreshSingleStartInspection -Inspection $windowState `
                        -AllowedStates @('running', 'exited', 'dead') -EventPrefix 'window' `
                        -RequirePinnedStartedAt)) {
                        # Contract helper fixed the fail-closed outcome.
                    } elseif ($windowState.State.Status -eq 'running') {
                        $script:FinalContainerState = 'running'
                        Set-Outcome -Outcome 'WINDOW_COMPLETE' -EventName 'window_complete'
                    } elseif ($windowState.State.Status -in @('exited', 'dead')) {
                        $script:FinalContainerState = [string]$windowState.State.Status
                        $drainCompleted = Invoke-PostExitLogDrain
                        if ($null -eq $script:Outcome -and $drainCompleted) {
                            Set-Outcome -Outcome 'CONTAINER_EXITED' -EventName 'exited_before_window_complete'
                        } elseif ($null -eq $script:Outcome) {
                            Set-Outcome -Outcome 'OBSERVER_FAILURE' -EventName 'post_exit_drain_incomplete'
                        }
                    } else {
                        $script:FinalContainerState = [string]$windowState.State.Status
                        Set-Outcome -Outcome 'OBSERVER_FAILURE' -EventName 'window_invalid_state'
                    }
                    break
                }
                Start-Sleep -Milliseconds $PollMilliseconds
            }
        }
    }

    if ($null -ne $script:Outcome -and $null -ne $logJob) {
        $closed = Close-FollowJob -Job $logJob -Discard
        $logJob = $null
        if (-not $closed) { $script:ExitCodeOverride = $ExitCodes.OBSERVER_FAILURE }
    }

    if ($script:Outcome -in @('CRITICAL_EVENT', 'OBSERVER_FAILURE')) {
        $script:Stage = 'critical_stop'
        if (-not (Stop-TargetBounded)) {
            $script:ExitCodeOverride = $ExitCodes.OBSERVER_FAILURE
        }
    } elseif ($script:Outcome -eq 'WINDOW_COMPLETE' -and $StopOnWindowComplete) {
        if (-not (Stop-TargetBounded)) {
            $script:ExitCodeOverride = $ExitCodes.OBSERVER_FAILURE
        }
    }
    if ($script:Outcome -ne 'IDENTITY_MISMATCH') {
        $terminalInspection = Get-ContainerInspection
        $terminalFailure = Get-FreshSingleStartInspectionFailure -Inspection $terminalInspection `
            -AllowedStates @('created', 'running', 'exited', 'dead') `
            -RequirePinnedStartedAt:($null -ne $script:PinnedStartedAt)
        if ($null -eq $terminalFailure) {
            $script:FinalContainerState = [string]$terminalInspection.State.Status
        } else {
            $script:ExitCodeOverride = $ExitCodes.OBSERVER_FAILURE
        }
    }
} catch {
    Set-Outcome -Outcome 'OBSERVER_FAILURE' -EventName ("unhandled_{0}" -f $script:Stage)
    if ($null -ne $logJob) {
        if (-not (Close-FollowJob -Job $logJob -Discard)) {
            $script:ExitCodeOverride = $ExitCodes.OBSERVER_FAILURE
        }
        $logJob = $null
    }
    if (-not (Stop-TargetBounded)) { $script:ExitCodeOverride = $ExitCodes.OBSERVER_FAILURE }
} finally {
    if ($null -ne $logJob) {
        if (-not (Close-FollowJob -Job $logJob -Discard)) {
            $script:ExitCodeOverride = $ExitCodes.OBSERVER_FAILURE
        }
        $logJob = $null
    }
    if ($null -eq $script:Outcome) { Set-Outcome -Outcome 'OBSERVER_FAILURE' -EventName 'missing_outcome' }
    try { Write-SafeResult }
    catch { $script:ResultWriteFailed = $true }
}

if ($script:ResultWriteFailed) { exit $ExitCodes.OBSERVER_FAILURE }
if ($null -ne $script:ExitCodeOverride) { exit $script:ExitCodeOverride }
exit $ExitCodes[$script:Outcome]
