[CmdletBinding()]
param(
    [switch]$SyntaxOnly,
    [switch]$HeldRunningCriticalStressOnly,
    [switch]$TransitionStressOnly,
    [switch]$NaturalExitRaceOnly,
    [switch]$ImmediateExitCharacterizationOnly,
    [switch]$DuplicateReplayStressOnly,
    [switch]$RunningInterruptionStressOnly,
    [switch]$NonCriticalWindowStressOnly,
    [switch]$FinalReconciliationOnly,
    [switch]$ScenarioARegressionOnly,
    [ValidateRange(1, 100)][int]$HeldRunningIterations = 100,
    [switch]$FullSuiteThreeOnly
)

$ErrorActionPreference = 'Stop'
$Observer = Join-Path $PSScriptRoot 'baileys_log_observer.ps1'
$Image = 'mbb-recovery-baileys7:ec91a01-20260715120359'
$ImageId = 'sha256:e4d6c0dccab814270d6b0d39d854cf535dfba3b0bbabbd7abd7223aafb7483ab'
$ScenarioResults = [Collections.Generic.List[object]]::new()
$CreatedContainers = [Collections.Generic.List[string]]::new()
$TempFiles = [Collections.Generic.List[string]]::new()
$EmitterSourcePath = $null
$DefaultLoggingDriver = $null
$FocusedSwitchCount = @(@(
    $SyntaxOnly, $HeldRunningCriticalStressOnly, $TransitionStressOnly, $NaturalExitRaceOnly,
    $ImmediateExitCharacterizationOnly, $DuplicateReplayStressOnly,
    $RunningInterruptionStressOnly, $NonCriticalWindowStressOnly,
    $FinalReconciliationOnly, $ScenarioARegressionOnly, $FullSuiteThreeOnly
) | Where-Object { $_ }).Count
if ($FocusedSwitchCount -gt 1) { throw 'Choose only one focused validation mode' }

if (-not ('MbbRecovery.ProcessMethods' -as [type])) {
    Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
namespace MbbRecovery {
    public static class ProcessMethods {
        [DllImport("kernel32.dll", SetLastError = true)]
        public static extern bool GetExitCodeProcess(IntPtr processHandle, out uint exitCode);
    }
}
'@
}

$ProductionLikeTimingProfile = [pscustomobject]@{
    Name = 'production_like'; PrecriticalDelayMs = 2500; PostcriticalDelayMs = 1000
    PostcriticalLifetimeMs = 150000; NaturalExit = $false; NaturalExitAfterCriticalMs = 0
}
$NaturalExitRaceTimingProfile = [pscustomobject]@{
    Name = 'natural_exit_race'; PrecriticalDelayMs = 50; PostcriticalDelayMs = 25
    PostcriticalLifetimeMs = 0; NaturalExit = $true; NaturalExitAfterCriticalMs = 100
}

$FixedEmitterSource = @'
'use strict';

const fs = require('node:fs');

function requireBoolean(value, name) {
  if (typeof value !== 'boolean') throw new Error(name);
  return value;
}

function requireInteger(value, name, minimum = 0, maximum = 180000) {
  if (!Number.isInteger(value) || value < minimum || value > maximum) throw new Error(name);
  return value;
}

function requireMode(value) {
  const allowed = new Set([
    'scenario_a', 'immediate_critical_exit', 'idle_hold', 'connect_hold',
    'malformed_critical_hold', 'delayed_unknown_critical_hold',
    'noise_then_connect_hold', 'secret_critical_hold', 'safe_exit',
    'invalid_origin_hold', 'no_output_exit', 'numbered_critical_hold', 'heartbeat_hold'
  ]);
  if (typeof value !== 'string' || !allowed.has(value)) throw new Error('mode');
  return value;
}

function requireChoice(value, name, allowed) {
  if (typeof value !== 'string' || !allowed.has(value)) throw new Error(name);
  return value;
}

function emitEvent(value) {
  const serialized = JSON.stringify(value) + '\n';
  fs.writeSync(1, Buffer.from(serialized, 'utf8'));
}

function emitText(value) {
  fs.writeSync(1, Buffer.from(value, 'utf8'));
}

function emitErrorText(value) {
  fs.writeSync(2, Buffer.from(value, 'utf8'));
}

function emitErrorEvent(value) {
  const serialized = JSON.stringify(value) + '\n';
  fs.writeSync(2, Buffer.from(serialized, 'utf8'));
}

function openLedger() {
  return fs.openSync('/tmp/observer-emitter-ledger', 'wx', 0o600);
}

function emitNumberedEvent(ledgerFd, value, eventType) {
  const serialized = JSON.stringify(value) + '\n';
  fs.writeSync(1, Buffer.from(serialized, 'utf8'));
  const ledgerEntry = `${value.sequence}|${eventType}\n`;
  fs.writeSync(ledgerFd, Buffer.from(ledgerEntry, 'utf8'));
}

function hold(milliseconds) {
  setTimeout(() => {}, requireInteger(milliseconds, 'hold_ms'));
}

function runScenarioA(config) {
  const profileName = String(config.profile_name || '');
  const allowedProfiles = new Set(['production_like', 'natural_exit_race']);
  if (!allowedProfiles.has(profileName)) throw new Error('profile_name');
  const precritical = requireInteger(config.precritical_delay_ms, 'precritical_delay_ms');
  const postcritical = requireInteger(config.postcritical_delay_ms, 'postcritical_delay_ms');
  const lifetime = requireInteger(config.postcritical_lifetime_ms, 'postcritical_lifetime_ms');
  const naturalExit = requireBoolean(config.natural_exit_after_critical, 'natural_exit_after_critical');
  const naturalExitDelay = requireInteger(
    config.natural_exit_after_critical_ms, 'natural_exit_after_critical_ms'
  );
  const emitSafeConnect = requireBoolean(config.emit_safe_connect, 'emit_safe_connect');
  const emitSafeQr = requireBoolean(config.emit_safe_qr, 'emit_safe_qr');
  const emitCritical = requireBoolean(config.emit_critical, 'emit_critical');
  const emitLater = requireBoolean(config.emit_later_marker, 'emit_later_marker');

  if (emitSafeConnect) emitEvent({ msg: 'baileys.connect_started', next_socket_generation: 1 });
  if (emitSafeQr) emitEvent({ msg: 'qr_code_generated', qr_present: true });

  const critical = () => emitEvent({
    msg: 'baileys.reconnect_scheduled',
    disconnect_category: 'connection_lost_or_timed_out',
    disconnect_origin: 'keepalive_silence',
    socket_generation: 1
  });
  if (emitCritical) {
    if (precritical === 0) critical();
    else setTimeout(critical, precritical);
  }
  if (emitLater) {
    setTimeout(() => emitEvent({ msg: 'later_marker' }), precritical + postcritical);
  }
  if (naturalExit) {
    hold(precritical + naturalExitDelay);
  } else {
    hold(precritical + lifetime);
  }
}

function main(config) {
  const mode = requireMode(config.mode);
  switch (mode) {
    case 'scenario_a':
      runScenarioA(config);
      return;
    case 'immediate_critical_exit':
      {
        const exitMode = requireChoice(
          config.exit_mode || 'natural', 'exit_mode', new Set(['natural', 'explicit', 'observer_stop'])
        );
        const eventDelay = requireInteger(config.event_delay_ms || 0, 'event_delay_ms');
        const holdMs = requireInteger(config.post_emit_hold_ms || 0, 'post_emit_hold_ms');
        const critical = () => {
          const value = { msg: 'baileys.reconnect_scheduled', sequence: 1, disconnect_category: 'connection_lost_or_timed_out', disconnect_origin: 'qr_refs_exhausted', socket_generation: 1 };
          emitEvent(value);
          if (exitMode === 'explicit') process.exit(0);
          process.exitCode = 0;
          if (exitMode === 'observer_stop') hold(holdMs);
        };
        if (eventDelay === 0) critical();
        else setTimeout(critical, eventDelay);
      }
      return;
    case 'idle_hold':
      hold(config.hold_ms);
      return;
    case 'connect_hold':
      emitEvent({ msg: 'baileys.connect_started', next_socket_generation: 1 });
      hold(config.hold_ms);
      return;
    case 'malformed_critical_hold':
      setTimeout(
        () => emitText('malformed connection_lost_or_timed_out evidence\n'),
        requireInteger(config.event_delay_ms || 0, 'event_delay_ms')
      );
      hold(config.hold_ms);
      return;
    case 'delayed_unknown_critical_hold':
      emitEvent({ msg: 'baileys.connect_started', next_socket_generation: 1 });
      setTimeout(() => emitEvent({ msg: 'baileys.reconnect_scheduled', disconnect_category: 'connection_lost_or_timed_out', disconnect_origin: 'unknown_408', socket_generation: 1 }), requireInteger(config.critical_delay_ms, 'critical_delay_ms'));
      hold(config.hold_ms);
      return;
    case 'noise_then_connect_hold': {
      const count = requireInteger(config.noise_count, 'noise_count', 0, 10);
      const emitNoise = () => {
        for (let index = 0; index < count; index += 1) emitText(`noise-${index + 1}\n`);
        if (requireBoolean(config.emit_connect, 'emit_connect')) {
          emitEvent({ msg: 'baileys.connect_started', next_socket_generation: 1 });
        }
      };
      setTimeout(emitNoise, requireInteger(config.event_delay_ms || 0, 'event_delay_ms'));
      hold(config.hold_ms);
      return;
    }
    case 'secret_critical_hold': {
      emitErrorText('synthetic-stderr-marker\n');
      setTimeout(() => emitErrorEvent({
        msg: 'baileys.reconnect_scheduled', disconnect_category: 'connection_lost_or_timed_out',
        disconnect_origin: 'server_408', socket_generation: 1,
        message: 'synthetic-message-marker', stack: 'synthetic-stack-marker',
        cause: 'synthetic-cause-marker', data: 'synthetic-data-marker',
        authorization: 'synthetic-authorization-marker', qr: 'synthetic-qr-marker',
        jid: 'synthetic-jid-marker', phone: 'synthetic-phone-marker',
        session: 'synthetic-session-marker'
      }), requireInteger(config.event_delay_ms || 0, 'event_delay_ms'));
      hold(config.hold_ms);
      return;
    }
    case 'safe_exit':
      emitEvent({ msg: 'safe_event' });
      return;
    case 'no_output_exit':
      process.exitCode = 0;
      return;
    case 'invalid_origin_hold':
      setTimeout(
        () => emitEvent({ msg: 'baileys.reconnect_scheduled', disconnect_category: 'connection_lost_or_timed_out', disconnect_origin: 'not_applicable', socket_generation: 1 }),
        requireInteger(config.event_delay_ms || 0, 'event_delay_ms')
      );
      hold(config.hold_ms);
      return;
    case 'numbered_critical_hold': {
      const intervalMs = requireInteger(config.interval_ms, 'interval_ms', 25, 1000);
      const criticalSequence = requireInteger(config.critical_sequence, 'critical_sequence', 1, 1000);
      const minimumEvents = requireInteger(config.minimum_events, 'minimum_events', 50, 1000);
      if (criticalSequence > minimumEvents) throw new Error('critical_sequence');
      const ledgerFd = openLedger();
      let sequence = 0;
      hold(config.hold_ms);
      const eventTimer = setInterval(() => {
        sequence += 1;
        if (sequence === criticalSequence) {
          emitNumberedEvent(ledgerFd, {
            msg: 'baileys.reconnect_scheduled', sequence,
            socket_generation: 1,
            disconnect_category: 'connection_lost_or_timed_out',
            disconnect_origin: 'unknown_408'
          }, 'critical');
        } else {
          emitNumberedEvent(
            ledgerFd,
            { msg: 'numbered_safe_event', sequence, socket_generation: 1 },
            'safe'
          );
        }
        if (sequence >= minimumEvents) clearInterval(eventTimer);
      }, intervalMs);
      return;
    }
    case 'heartbeat_hold': {
      const intervalMs = requireInteger(config.interval_ms, 'interval_ms', 25, 1000);
      const ledgerFd = openLedger();
      let sequence = 0;
      setInterval(() => {
        sequence += 1;
        emitNumberedEvent(
          ledgerFd,
          { msg: 'heartbeat_event', sequence, socket_generation: 1 },
          'safe'
        );
      }, intervalMs);
      return;
    }
    default:
      throw new Error('mode');
  }
}

try {
  const profilePath = process.argv[2];
  if (typeof profilePath !== 'string' || profilePath.length === 0) throw new Error('profile_path');
  const config = JSON.parse(fs.readFileSync(profilePath, 'utf8'));
  main(config);
} catch {
  process.exitCode = 64;
}
'@

$FixedLedgerMetadataProbe = @'
'use strict';
const fs = require('node:fs');
const path = '/tmp/observer-emitter-ledger';
if (!fs.existsSync(path)) {
  process.stdout.write(JSON.stringify({
    ledger_entry_count: 0,
    ledger_first_sequence: null,
    ledger_last_sequence: null,
    ledger_contains_critical: false
  }) + '\n');
  process.exitCode = 0;
} else {
  const text = fs.readFileSync(path, 'utf8');
  const lines = text.split('\n').filter((line) => line.length > 0);
  let first = null;
  let last = null;
  let containsCritical = false;
  let expected = 1;
  for (const line of lines) {
    const match = /^(\d+)\|(safe|critical)$/.exec(line);
    if (!match) throw new Error('invalid_ledger_entry');
    const sequence = Number(match[1]);
    if (!Number.isSafeInteger(sequence) || sequence !== expected) {
      throw new Error('invalid_ledger_sequence');
    }
    if (first === null) first = sequence;
    last = sequence;
    if (match[2] === 'critical') containsCritical = true;
    expected += 1;
  }
  process.stdout.write(JSON.stringify({
    ledger_entry_count: lines.length,
    ledger_first_sequence: first,
    ledger_last_sequence: last,
    ledger_contains_critical: containsCritical
  }) + '\n');
}
'@

function Assert-True([bool]$Condition, [string]$Message) {
    if (-not $Condition) { throw $Message }
}

function Get-CompletedProcessExitCode([Diagnostics.Process]$Process) {
    $managedExitCode = $Process.ExitCode
    if ($null -ne $managedExitCode) { return [int]$managedExitCode }

    [uint32]$nativeExitCode = 0
    if (-not [MbbRecovery.ProcessMethods]::GetExitCodeProcess($Process.Handle, [ref]$nativeExitCode)) {
        throw 'Unable to read observer process exit code'
    }
    if ($nativeExitCode -eq 259) { throw 'Observer process remained active after wait' }
    return [int]$nativeExitCode
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

function New-EmitterSource {
    $path = New-TestPath 'emitter.js'
    [IO.File]::WriteAllText($path, $FixedEmitterSource, [Text.UTF8Encoding]::new($false))
    $path
}

function Assert-IsolatedContainer([object]$Inspection, [string]$ExpectedEntrypoint = 'node') {
    Assert-True ($Inspection.HostConfig.NetworkMode -eq 'none') 'Emitter network mode was not none'
    Assert-True ($Inspection.Mounts.Count -eq 0) 'Emitter unexpectedly had a mount'
    Assert-True ($Inspection.Config.Entrypoint[0] -eq $ExpectedEntrypoint) 'Emitter entrypoint was invalid'
    Assert-True ([string]$Inspection.HostConfig.RestartPolicy.Name -eq 'no') `
        'Emitter restart policy was not disabled'
    Assert-True ([int]$Inspection.RestartCount -eq 0) 'Emitter restart count was non-zero'
    Assert-True ([bool]$Inspection.Config.AttachStdout -and [bool]$Inspection.Config.AttachStderr -and
        -not [bool]$Inspection.Config.Tty) 'Emitter stream attachment configuration was invalid'
}

function New-Emitter([hashtable]$Configuration) {
    $name = 'mbb-observer-' + [guid]::NewGuid().ToString('N').Substring(0, 16)
    $profilePath = New-TestPath 'profile.json'
    $profileJson = $Configuration | ConvertTo-Json -Compress
    [IO.File]::WriteAllText($profilePath, $profileJson, [Text.UTF8Encoding]::new($false))
    $call = Invoke-Docker -Arguments @(
        'create', '--pull=never', '--name', $name, '--network', 'none', '--restart', 'no',
        '--entrypoint', 'node',
        $Image, '/tmp/mbb-observer-emitter.js', '/tmp/mbb-observer-profile.json'
    )
    $id = ([string]$call.Output[-1]).Trim()
    Assert-True ($id -match '^[a-f0-9]{64}$') 'Emitter container ID was invalid'
    $CreatedContainers.Add($id)
    $null = Invoke-Docker -Arguments @('cp', $EmitterSourcePath, "${id}:/tmp/mbb-observer-emitter.js")
    $null = Invoke-Docker -Arguments @('cp', $profilePath, "${id}:/tmp/mbb-observer-profile.json")
    $inspection = (Invoke-Docker -Arguments @('inspect', $id)).Output -join "`n" | ConvertFrom-Json
    Assert-IsolatedContainer -Inspection $inspection[0]
    Assert-True ($inspection[0].Image -eq $ImageId) 'Emitter image identity mismatch'
    [pscustomobject]@{ Id = $id; Name = $name; ProfilePath = $profilePath }
}

function Test-EmitterSourceSyntax {
    $name = 'mbb-observer-syntax-' + [guid]::NewGuid().ToString('N').Substring(0, 12)
    $id = $null
    try {
        $call = Invoke-Docker -Arguments @(
            'create', '--pull=never', '--name', $name, '--network', 'none', '--restart', 'no',
            '--entrypoint', 'node',
            $Image, '--check', '/tmp/mbb-observer-emitter.js'
        )
        $id = ([string]$call.Output[-1]).Trim()
        Assert-True ($id -match '^[a-f0-9]{64}$') 'Syntax-check container ID was invalid'
        $CreatedContainers.Add($id)
        $null = Invoke-Docker -Arguments @('cp', $EmitterSourcePath, "${id}:/tmp/mbb-observer-emitter.js")
        $inspection = (Invoke-Docker -Arguments @('inspect', $id)).Output -join "`n" | ConvertFrom-Json
        Assert-IsolatedContainer -Inspection $inspection[0]
        Assert-True ($inspection[0].Image -eq $ImageId) 'Syntax-check image identity mismatch'
        $null = Invoke-Docker -Arguments @('start', $id)
        $deadline = [DateTimeOffset]::UtcNow.AddSeconds(10)
        do {
            $inspection = (Invoke-Docker -Arguments @('inspect', $id)).Output -join "`n" | ConvertFrom-Json
            if ($inspection[0].State.Status -ne 'running') { break }
            Start-Sleep -Milliseconds 50
        } while ([DateTimeOffset]::UtcNow -lt $deadline)
        Assert-True ($inspection[0].State.Status -eq 'exited' -and $inspection[0].State.ExitCode -eq 0) `
            'Fixed emitter JavaScript syntax validation failed'
        Write-Output 'emitter_syntax=pass'
    } finally {
        if ($id) {
            $null = Invoke-Docker -Arguments @('rm', '--force', $id) -AllowFailure
            $CreatedContainers.Remove($id) | Out-Null
        }
    }
}

function Start-Observer {
    param(
        [string]$ContainerId,
        [string]$ExpectedImage = $ImageId,
        [int]$StartTimeout = 5,
        [int]$Window = 3,
        [switch]$StopOnWindow,
        [int]$ForceStreamInterruption = 0,
        [switch]$HoldFollowDeliveryForFinalReconciliation,
        [int]$Threshold = 3,
        [switch]$ForceAtomicResultWriteFailure
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
        '-ParseFailureThreshold', [string]$Threshold,
        '-TestSafeDiagnostics'
    )
    if ($StopOnWindow) { $arguments += '-StopOnWindowComplete' }
    if ($ForceStreamInterruption -gt 0) {
        $arguments += @('-TestForceStreamInterruptionAfterMilliseconds', [string]$ForceStreamInterruption)
    }
    if ($HoldFollowDeliveryForFinalReconciliation) {
        $arguments += '-TestHoldFollowDeliveryForFinalReconciliation'
    }
    if ($ForceAtomicResultWriteFailure) { $arguments += '-TestForceAtomicResultWriteFailure' }
    $process = Start-Process -FilePath 'powershell.exe' -ArgumentList $arguments -PassThru -WindowStyle Hidden `
        -RedirectStandardOutput $stdout -RedirectStandardError $stderr
    # Windows PowerShell 5.1 can discard the process handle after redirected child exit.
    # Acquire it while the observer is alive so its actual exit code remains queryable.
    $null = $process.Handle
    [pscustomobject]@{
        Process = $process; Result = $result; Stdout = $stdout; Stderr = $stderr; ContainerId = $ContainerId
    }
}

function Get-SafeProperty([object]$Object, [string]$Name, [object]$Default = $null) {
    if ($null -ne $Object -and $Object.PSObject.Properties.Name -contains $Name) {
        return $Object.$Name
    }
    return $Default
}

function Get-DockerLogChildrenForContainer([string]$ContainerId) {
    @(
        Get-CimInstance Win32_Process -Filter "Name = 'docker.exe'" -ErrorAction Stop |
            Where-Object {
                -not [string]::IsNullOrWhiteSpace([string]$_.CommandLine) -and
                [string]$_.CommandLine -match '(?i)(?:^|\s)logs(?:\s|$)' -and
                [string]$_.CommandLine.Contains($ContainerId)
            }
    )
}

function Test-DockerLogChildAbsent {
    param(
        [string]$ContainerId,
        [int]$TimeoutMilliseconds = 2000
    )
    $deadline = [DateTimeOffset]::UtcNow.AddMilliseconds($TimeoutMilliseconds)
    do {
        try {
            if (@(Get-DockerLogChildrenForContainer -ContainerId $ContainerId).Count -eq 0) {
                return $true
            }
        } catch { return $false }
        Start-Sleep -Milliseconds 50
    } while ([DateTimeOffset]::UtcNow -lt $deadline)
    return $false
}

function Wait-Observer {
    param(
        [object]$Run,
        [string]$Scenario,
        [string]$TimingProfile = 'not_applicable',
        [int]$TimeoutSeconds = 180,
        [switch]$AllowMissingResult
    )
    $process = $Run.Process
    $deadline = [DateTimeOffset]::UtcNow.AddSeconds($TimeoutSeconds)
    $observerTimedOut = $false
    while (-not $process.HasExited -and [DateTimeOffset]::UtcNow -lt $deadline) {
        Start-Sleep -Milliseconds 50
    }
    $observerExitConfirmed = $process.HasExited
    if (-not $process.HasExited) {
        $observerTimedOut = $true
        Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
        $observerExitConfirmed = $process.WaitForExit(5000)
    }
    if ($observerExitConfirmed) { $process.Refresh() }
    $processExitCode = if ($observerExitConfirmed) {
        Get-CompletedProcessExitCode -Process $process
    } else { $null }
    $dockerLogChildAbsent = Test-DockerLogChildAbsent -ContainerId $Run.ContainerId
    $resultWritten = Test-Path -LiteralPath $Run.Result
    $resultParsed = $false
    $result = $null
    if ($resultWritten) {
        try {
            $result = Get-Content -LiteralPath $Run.Result -Raw | ConvertFrom-Json
            $resultParsed = $true
        } catch { $resultParsed = $false }
    }
    $inspectionCall = Invoke-Docker -Arguments @('inspect', $Run.ContainerId) -AllowFailure
    $inspection = if ($inspectionCall.Code -eq 0) {
        @($inspectionCall.Output -join "`n" | ConvertFrom-Json)[0]
    } else { $null }
    $fallbackState = if ($null -ne $inspection) { [string]$inspection.State.Status } else { $null }
    $reportedStateAfterStop = Get-SafeProperty $result 'container_state_after_stop'
    $stateAfterStop = if ([string]::IsNullOrWhiteSpace([string]$reportedStateAfterStop)) {
        $fallbackState
    } else { [string]$reportedStateAfterStop }
    $diagnostic = [ordered]@{
        scenario                     = $Scenario
        timing_profile               = $TimingProfile
        observer_outcome             = Get-SafeProperty $result 'outcome'
        structured_exit_code         = Get-SafeProperty $result 'exit_code'
        process_exit_code            = $processExitCode
        stop_issued                  = Get-SafeProperty $result 'stop_issued' $false
        stop_action_count            = Get-SafeProperty $result 'stop_action_count' 0
        stop_command_exit_code       = Get-SafeProperty $result 'stop_command_exit_code'
        stop_confirmed               = Get-SafeProperty $result 'stop_confirmed' $false
        container_state_before_stop  = Get-SafeProperty $result 'container_state_before_stop'
        container_state_after_stop   = $stateAfterStop
        received_line_count          = Get-SafeProperty $result 'received_line_count' 0
        unique_parsed_event_count    = Get-SafeProperty $result 'unique_parsed_event_count' 0
        duplicate_line_count         = Get-SafeProperty $result 'duplicate_line_count' 0
        critical_event_count         = Get-SafeProperty $result 'critical_event_count' 0
        critical_event_phase         = Get-SafeProperty $result 'critical_event_phase'
        safe_event_count             = Get-SafeProperty $result 'safe_event_count' 0
        ignored_after_terminal_count = Get-SafeProperty $result 'ignored_after_terminal_count' 0
        connect_started_count        = Get-SafeProperty $result 'connect_started_count' 0
        qr_code_generated_count      = Get-SafeProperty $result 'qr_code_generated_count' 0
        later_marker_count           = Get-SafeProperty $result 'later_marker_count' 0
        result_written               = $resultWritten -and $resultParsed -and
            [bool](Get-SafeProperty $result 'result_written' $false)
        result_atomic                = $resultWritten -and $resultParsed -and
            [bool](Get-SafeProperty $result 'result_atomic' $false)
        emitter_exit_code            = if ($null -ne $inspection) { $inspection.State.ExitCode } else { $null }
        started_at_validated           = Get-SafeProperty $result 'started_at_validated' $false
        final_container_state         = Get-SafeProperty $result 'final_container_state' $fallbackState
        initial_replay_attempted       = Get-SafeProperty $result 'initial_replay_attempted' $false
        initial_replay_unique_lines    = Get-SafeProperty $result 'initial_replay_unique_lines' 0
        follow_started                 = Get-SafeProperty $result 'follow_started' $false
        follow_process_started         = Get-SafeProperty $result 'follow_process_started' $false
        follow_process_id              = Get-SafeProperty $result 'follow_process_id'
        follow_process_running         = Get-SafeProperty $result 'follow_process_running' $false
        follow_process_exit_code       = Get-SafeProperty $result 'follow_process_exit_code'
        follow_stdout_line_count       = Get-SafeProperty $result 'follow_stdout_line_count' 0
        follow_stderr_line_count       = Get-SafeProperty $result 'follow_stderr_line_count' 0
        follow_queue_depth             = Get-SafeProperty $result 'follow_queue_depth' 0
        follow_reader_started          = Get-SafeProperty $result 'follow_reader_started' $false
        follow_reader_completed        = Get-SafeProperty $result 'follow_reader_completed' $false
        follow_output_observed          = Get-SafeProperty $result 'follow_output_observed' $false
        follow_queue_overflow           = Get-SafeProperty $result 'follow_queue_overflow' $false
        follow_discarded_line_count     = Get-SafeProperty $result 'follow_discarded_line_count' 0
        follow_close_attempted          = Get-SafeProperty $result 'follow_close_attempted' $false
        follow_close_confirmed          = Get-SafeProperty $result 'follow_close_confirmed' $false
        observer_first_sequence        = Get-SafeProperty $result 'observer_first_sequence'
        observer_last_sequence         = Get-SafeProperty $result 'observer_last_sequence'
        initial_replay_valid_event_count = Get-SafeProperty $result 'initial_replay_valid_event_count' 0
        follow_valid_event_count       = Get-SafeProperty $result 'follow_valid_event_count' 0
        final_replay_valid_event_count = Get-SafeProperty $result 'final_replay_valid_event_count' 0
        final_replay_attempted          = Get-SafeProperty $result 'final_replay_attempted' $false
        final_replay_completed          = Get-SafeProperty $result 'final_replay_completed' $false
        outcome_set_count               = Get-SafeProperty $result 'outcome_set_count' 0
        rejected_outcome_set_count      = Get-SafeProperty $result 'rejected_outcome_set_count' 0
        follow_exit_observed           = Get-SafeProperty $result 'follow_exit_observed' $false
        container_state_at_follow_exit = Get-SafeProperty $result 'container_state_at_follow_exit'
        post_exit_drain_started        = Get-SafeProperty $result 'post_exit_drain_started' $false
        post_exit_drain_attempts       = Get-SafeProperty $result 'post_exit_drain_attempts' 0
        post_exit_drain_unique_lines   = Get-SafeProperty $result 'post_exit_drain_unique_lines' 0
        post_exit_drain_elapsed_ms     = Get-SafeProperty $result 'post_exit_drain_elapsed_ms' 0
        post_exit_quiescence_reached   = Get-SafeProperty $result 'post_exit_quiescence_reached' $false
        stop_poll_attempts           = Get-SafeProperty $result 'stop_poll_attempts' 0
        stop_resolution              = Get-SafeProperty $result 'stop_resolution'
    }
    [Console]::Out.WriteLine(($diagnostic | ConvertTo-Json -Compress))
    Assert-True $observerExitConfirmed 'Observer did not terminate within the bounded wait'
    Assert-True $dockerLogChildAbsent 'Observer left a Docker log child for the exact container'
    Assert-True (-not $observerTimedOut) 'Observer exceeded test timeout'
    Assert-True ($diagnostic.stop_action_count -le 1) 'Observer issued more than one stop command'
    Assert-True ($diagnostic.critical_event_count -le 1) `
        'Observer processed more than one critical event'
    if ($resultParsed) {
        Assert-True ($diagnostic.outcome_set_count -eq 1) 'Observer did not fix exactly one terminal outcome'
        Assert-True (-not $diagnostic.follow_queue_overflow) 'Observer follow queue overflowed'
        if ($diagnostic.follow_started) {
            Assert-True ($diagnostic.follow_close_attempted -and
                $diagnostic.follow_close_confirmed -and
                $diagnostic.follow_reader_completed -and
                -not $diagnostic.follow_process_running -and
                $diagnostic.follow_queue_depth -eq 0) 'Observer follow child was not safely finalized'
        }
    }
    if ($diagnostic.observer_outcome -eq 'CRITICAL_EVENT') {
        Assert-True ($diagnostic.critical_event_count -eq 1) `
            'Critical outcome did not contain exactly one critical event'
    }
    if ($diagnostic.observer_outcome -eq 'CONTAINER_EXITED') {
        Assert-True ($diagnostic.critical_event_count -eq 0 -and
            $diagnostic.post_exit_drain_started -and
            $diagnostic.final_container_state -in @('exited', 'dead')) `
            'Exited outcome lacked bounded drain evidence'
    }
    if ($diagnostic.observer_outcome -eq 'WINDOW_COMPLETE') {
        Assert-True ($diagnostic.critical_event_count -eq 0) `
            'Window outcome contained a critical event'
    }
    if ($diagnostic.post_exit_drain_started) {
        Assert-True ($diagnostic.final_container_state -in @('exited', 'dead')) `
            'Post-exit drain ran without a final exited state'
    }
    if ($diagnostic.stop_confirmed) {
        $safeCreated = $diagnostic.container_state_after_stop -eq 'created' -and
            $diagnostic.container_state_before_stop -eq 'created'
        Assert-True ($diagnostic.container_state_after_stop -in @('exited', 'dead') -or $safeCreated) `
            'Observer confirmed an unsafe container state'
    }
    if (-not $AllowMissingResult) {
        Assert-True $resultWritten 'Observer did not write a safe result'
        Assert-True $resultParsed 'Observer safe result was not complete JSON'
        Assert-True ($diagnostic.result_written -and $diagnostic.result_atomic) `
            'Observer safe result was not atomically finalized'
    }
    [pscustomobject]@{
        ExitCode = if ($null -ne $result) { [int]$result.exit_code } else { $null }
        ProcessExitCode = $processExitCode
        Result = $result
        Diagnostic = [pscustomobject]$diagnostic
        Console = [string]((Get-Content $Run.Stdout -Raw -ErrorAction SilentlyContinue) +
            (Get-Content $Run.Stderr -Raw -ErrorAction SilentlyContinue))
    }
}

function Start-Emitter([object]$Emitter) {
    $null = Invoke-Docker -Arguments @('start', $Emitter.Id)
}

function Get-DockerSnapshotOracle {
    param([object]$Emitter, [switch]$RequireRunning)
    $inspectionCall = Invoke-Docker -Arguments @('inspect', $Emitter.Id)
    $inspection = @($inspectionCall.Output -join "`n" | ConvertFrom-Json)[0]
    Assert-True ($inspection.Id -eq $Emitter.Id -and $inspection.Image -eq $ImageId) `
        'Snapshot oracle identity mismatch'
    if ($RequireRunning) {
        Assert-True ([string]$inspection.State.Status -eq 'running') `
            'Snapshot oracle target was not running before retrieval'
    }
    $startedAt = [DateTimeOffset]::MinValue
    Assert-True ([DateTimeOffset]::TryParse([string]$inspection.State.StartedAt, [ref]$startedAt) -and
        $startedAt -gt [DateTimeOffset]::Parse('1970-01-01T00:00:00Z')) `
        'Snapshot oracle StartedAt was invalid'
    $logs = Invoke-Docker -Arguments @('logs', '--timestamps', $Emitter.Id)
    $lineCount = 0
    $validCount = 0
    $firstSequence = $null
    $lastSequence = $null
    $containsCritical = $false
    foreach ($line in $logs.Output) {
        if ([string]::IsNullOrWhiteSpace([string]$line)) { continue }
        $lineCount += 1
        $match = [regex]::Match([string]$line, '^\S+\s+(?<payload>\{.*\})$')
        if (-not $match.Success) { continue }
        try { $event = $match.Groups['payload'].Value | ConvertFrom-Json } catch { continue }
        if (-not ($event.PSObject.Properties.Name -contains 'sequence')) { continue }
        $sequence = $event.sequence
        if ($null -eq $sequence -or $sequence.GetType().FullName -notin @(
            'System.Byte', 'System.SByte', 'System.Int16', 'System.UInt16',
            'System.Int32', 'System.UInt32', 'System.Int64'
        )) { continue }
        $validCount += 1
        if ($null -eq $firstSequence) { $firstSequence = [int64]$sequence }
        $lastSequence = [int64]$sequence
        if ($event.PSObject.Properties.Name -contains 'disconnect_category' -and
            [string]$event.disconnect_category -eq 'connection_lost_or_timed_out') {
            $containsCritical = $true
        }
    }
    $afterCall = Invoke-Docker -Arguments @('inspect', $Emitter.Id)
    $after = @($afterCall.Output -join "`n" | ConvertFrom-Json)[0]
    Assert-True ($after.Id -eq $Emitter.Id -and $after.Image -eq $ImageId) `
        'Snapshot oracle identity changed after retrieval'
    if ($RequireRunning) {
        Assert-True ([string]$after.State.Status -eq 'running') `
            'Snapshot oracle target stopped during retrieval'
    }
    [pscustomobject]@{
        docker_snapshot_line_count = $lineCount
        docker_snapshot_valid_event_count = $validCount
        docker_snapshot_first_sequence = $firstSequence
        docker_snapshot_last_sequence = $lastSequence
        docker_snapshot_contains_critical = $containsCritical
    }
}

function Get-DiagnosticContainerEvidence {
    param([object]$Emitter)
    $call = Invoke-Docker -Arguments @('inspect', $Emitter.Id)
    $inspection = @($call.Output -join "`n" | ConvertFrom-Json)[0]
    Assert-True ($inspection.Id -eq $Emitter.Id -and $inspection.Image -eq $ImageId) `
        'Diagnostic container identity mismatch'
    $startedAt = [DateTimeOffset]::MinValue
    $startedAtValid = [DateTimeOffset]::TryParse(
        [string]$inspection.State.StartedAt, [ref]$startedAt
    ) -and $startedAt -gt [DateTimeOffset]::Parse('1970-01-01T00:00:00Z')
    $configuredDriver = [string]$inspection.HostConfig.LogConfig.Type
    $logDriver = if ([string]::IsNullOrWhiteSpace($configuredDriver)) {
        [string]$script:DefaultLoggingDriver
    } else { $configuredDriver }
    $top = Invoke-Docker -Arguments @('top', $Emitter.Id, '-eo', 'pid,comm') -AllowFailure
    $nodePresent = $false
    if ($top.Code -eq 0) {
        foreach ($line in @($top.Output | Select-Object -Skip 1)) {
            if ([string]$line -match '(?i)^\s*\d+\s+(?:.*[\\/])?node(?:\.exe)?\s*$') {
                $nodePresent = $true
                break
            }
        }
    }
    [pscustomobject]@{
        Inspection = $inspection
        Safe = [pscustomobject][ordered]@{
            container_running = [string]$inspection.State.Status -eq 'running'
            container_restart_count = [int]$inspection.RestartCount
            restart_policy = [string]$inspection.HostConfig.RestartPolicy.Name
            log_driver = $logDriver
            attach_stdout = [bool]$inspection.Config.AttachStdout
            attach_stderr = [bool]$inspection.Config.AttachStderr
            tty_enabled = [bool]$inspection.Config.Tty
            started_at_valid = [bool]$startedAtValid
            node_process_present = [bool]$nodePresent
        }
    }
}

function Get-EmitterLedgerMetadata {
    param([object]$Emitter)
    $beforeCall = Invoke-Docker -Arguments @('inspect', $Emitter.Id)
    $before = @($beforeCall.Output -join "`n" | ConvertFrom-Json)[0]
    Assert-True ($before.Id -eq $Emitter.Id -and $before.Image -eq $ImageId -and
        [string]$before.State.Status -eq 'running') 'Ledger preflight identity/state mismatch'
    $call = Invoke-Docker -Arguments @(
        'exec', $Emitter.Id, 'node', '-e', $FixedLedgerMetadataProbe
    ) -AllowFailure
    Assert-True ($call.Code -eq 0) 'Ledger metadata probe failed'
    Assert-True ($call.Output.Count -eq 1 -and [string]$call.Output[0] -match '^\{.*\}$') `
        'Ledger metadata probe returned an invalid safe record'
    try { $metadata = [string]$call.Output[0] | ConvertFrom-Json }
    catch { throw 'Ledger metadata probe returned malformed safe JSON' }
    $afterCall = Invoke-Docker -Arguments @('inspect', $Emitter.Id)
    $after = @($afterCall.Output -join "`n" | ConvertFrom-Json)[0]
    Assert-True ($after.Id -eq $Emitter.Id -and $after.Image -eq $ImageId -and
        [string]$after.State.Status -eq 'running') 'Ledger postflight identity/state mismatch'
    $expectedNames = @(
        'ledger_entry_count', 'ledger_first_sequence', 'ledger_last_sequence',
        'ledger_contains_critical'
    )
    Assert-True (@($metadata.PSObject.Properties.Name).Count -eq $expectedNames.Count -and
        @($metadata.PSObject.Properties.Name | Where-Object { $_ -notin $expectedNames }).Count -eq 0) `
        'Ledger metadata whitelist mismatch'
    Assert-True ($metadata.ledger_entry_count -is [int] -or
        $metadata.ledger_entry_count -is [long]) 'Ledger entry count type was invalid'
    Assert-True ([int64]$metadata.ledger_entry_count -ge 0 -and
        [int64]$metadata.ledger_entry_count -le 10000) 'Ledger entry count range was invalid'
    Assert-True ($metadata.ledger_contains_critical -is [bool]) `
        'Ledger critical flag type was invalid'
    if ([int64]$metadata.ledger_entry_count -eq 0) {
        Assert-True ($null -eq $metadata.ledger_first_sequence -and
            $null -eq $metadata.ledger_last_sequence -and
            -not [bool]$metadata.ledger_contains_critical) 'Empty ledger metadata was inconsistent'
    } else {
        Assert-True (($metadata.ledger_first_sequence -is [int]) -or
            ($metadata.ledger_first_sequence -is [long])) 'Ledger first-sequence type was invalid'
        Assert-True (($metadata.ledger_last_sequence -is [int]) -or
            ($metadata.ledger_last_sequence -is [long])) 'Ledger last-sequence type was invalid'
        Assert-True ([int64]$metadata.ledger_first_sequence -eq 1 -and
            [int64]$metadata.ledger_last_sequence -eq [int64]$metadata.ledger_entry_count) `
            'Ledger sequence bounds were inconsistent'
    }
    [pscustomobject][ordered]@{
        ledger_entry_count = [int64]$metadata.ledger_entry_count
        ledger_first_sequence = if ($null -eq $metadata.ledger_first_sequence) {
            $null
        } else { [int64]$metadata.ledger_first_sequence }
        ledger_last_sequence = if ($null -eq $metadata.ledger_last_sequence) {
            $null
        } else { [int64]$metadata.ledger_last_sequence }
        ledger_contains_critical = [bool]$metadata.ledger_contains_critical
    }
}

function Get-DockerLogChannelEvidence {
    param([object]$Emitter)
    $arguments = @('logs', '--timestamps')
    $arguments += $Emitter.Id
    $call = Invoke-Docker -Arguments $arguments -AllowFailure
    Assert-True ($call.Code -eq 0) 'Docker log evidence query failed'
    $lineCount = 0
    $validCount = 0
    $firstSequence = $null
    $lastSequence = $null
    $containsCritical = $false
    foreach ($line in $call.Output) {
        if ([string]::IsNullOrWhiteSpace([string]$line)) { continue }
        $lineCount += 1
        $match = [regex]::Match([string]$line, '^\S+\s+(?<payload>\{.*\})$')
        if (-not $match.Success) { continue }
        try { $event = $match.Groups['payload'].Value | ConvertFrom-Json }
        catch { continue }
        if (-not ($event.PSObject.Properties.Name -contains 'sequence')) { continue }
        $sequence = $event.sequence
        if ($null -eq $sequence -or $sequence.GetType().FullName -notin @(
            'System.Byte', 'System.SByte', 'System.Int16', 'System.UInt16',
            'System.Int32', 'System.UInt32', 'System.Int64'
        )) { continue }
        $msg = if ($event.PSObject.Properties.Name -contains 'msg') {
            [string]$event.msg
        } else { $null }
        if ($msg -notin @('numbered_safe_event', 'heartbeat_event', 'baileys.reconnect_scheduled')) {
            continue
        }
        $validCount += 1
        if ($null -eq $firstSequence) { $firstSequence = [int64]$sequence }
        $lastSequence = [int64]$sequence
        if ($msg -eq 'baileys.reconnect_scheduled' -and
            $event.PSObject.Properties.Name -contains 'disconnect_category' -and
            [string]$event.disconnect_category -eq 'connection_lost_or_timed_out') {
            $containsCritical = $true
        }
    }
    [pscustomobject][ordered]@{
        log_line_count = $lineCount
        valid_event_count = $validCount
        first_sequence = $firstSequence
        last_sequence = $lastSequence
        contains_critical = $containsCritical
    }
}

function Wait-EmitterState {
    param(
        [object]$Emitter,
        [string[]]$AllowedStates,
        [int]$TimeoutMilliseconds = 5000
    )
    $deadline = [DateTimeOffset]::UtcNow.AddMilliseconds($TimeoutMilliseconds)
    do {
        $call = Invoke-Docker -Arguments @('inspect', $Emitter.Id) -AllowFailure
        if ($call.Code -ne 0) { throw 'Emitter identity became unavailable while waiting for state' }
        $inspection = @($call.Output -join "`n" | ConvertFrom-Json)[0]
        Assert-True ($inspection.Id -eq $Emitter.Id -and $inspection.Image -eq $ImageId) `
            'Emitter identity changed while waiting for state'
        if ([string]$inspection.State.Status -in $AllowedStates) { return $inspection }
        Start-Sleep -Milliseconds 50
    } while ([DateTimeOffset]::UtcNow -lt $deadline)
    throw 'Emitter did not reach the required bounded state'
}

function Remove-TestRun([object]$Run) {
    if ($null -eq $Run) { return }
    $observerExitConfirmed = $true
    if ($null -ne $Run.Process) {
        if (-not $Run.Process.HasExited) {
            Stop-Process -Id $Run.Process.Id -Force -ErrorAction SilentlyContinue
            $observerExitConfirmed = $Run.Process.WaitForExit(5000)
        }
        try { $dockerChildren = @(Get-DockerLogChildrenForContainer -ContainerId $Run.ContainerId) }
        catch { throw 'Unable to inspect Docker log children during cleanup' }
        foreach ($dockerChild in $dockerChildren) {
            Stop-Process -Id ([int]$dockerChild.ProcessId) -Force -ErrorAction SilentlyContinue
        }
        Assert-True (Test-DockerLogChildAbsent -ContainerId $Run.ContainerId) `
            'Docker log child remained after bounded test cleanup'
        if ($observerExitConfirmed -and $Run.Process.HasExited) {
            $Run.Process.Close()
            $Run.Process.Dispose()
        }
        [GC]::Collect()
        [GC]::WaitForPendingFinalizers()
    }
    foreach ($path in @($Run.Result, $Run.Stdout, $Run.Stderr)) {
        $deadline = [DateTimeOffset]::UtcNow.AddSeconds(15)
        do {
            Remove-Item -LiteralPath $path -Force -ErrorAction SilentlyContinue
            if (-not (Test-Path -LiteralPath $path)) { break }
            Start-Sleep -Milliseconds 50
        } while ([DateTimeOffset]::UtcNow -lt $deadline)
        Assert-True (-not (Test-Path -LiteralPath $path)) 'Observer temporary file remained after cleanup'
        $TempFiles.Remove([string]$path) | Out-Null
    }
    Assert-True $observerExitConfirmed 'Observer process remained after bounded test cleanup'
}

function Remove-Emitter([object]$Emitter) {
    if ($null -ne $Emitter) {
        $null = Invoke-Docker -Arguments @('rm', '--force', $Emitter.Id) -AllowFailure
        $inspection = Invoke-Docker -Arguments @('inspect', $Emitter.Id) -AllowFailure
        Assert-True ($inspection.Code -ne 0) 'Emitter container remained after cleanup'
        # Docker Desktop can defer final per-container plumbing briefly after removal. A fixed settle
        # keeps consecutive offline scenarios isolated without retrying observer evidence.
        Start-Sleep -Milliseconds 500
        $CreatedContainers.Remove($Emitter.Id) | Out-Null
        if ($Emitter.PSObject.Properties.Name -contains 'ProfilePath') {
            Remove-Item -LiteralPath $Emitter.ProfilePath -Force -ErrorAction SilentlyContinue
            Assert-True (-not (Test-Path -LiteralPath $Emitter.ProfilePath)) `
                'Emitter profile remained after cleanup'
            $TempFiles.Remove([string]$Emitter.ProfilePath) | Out-Null
        }
    }
}

function Add-Pass([string]$Name, [object]$Result) {
    $ScenarioResults.Add([pscustomobject]@{
        Scenario = $Name; Outcome = $Result.outcome; ExitCode = [int]$Result.exit_code
    })
    Write-Output "$Name=pass"
}

function Invoke-ScenarioA {
    param(
        [object]$TimingProfile = $ProductionLikeTimingProfile,
        [switch]$RecordPass
    )
    $emitter = $null
    $run = $null
    try {
        $configuration = @{
            mode = 'scenario_a'
            profile_name = $TimingProfile.Name
            precritical_delay_ms = $TimingProfile.PrecriticalDelayMs
            postcritical_delay_ms = $TimingProfile.PostcriticalDelayMs
            postcritical_lifetime_ms = $TimingProfile.PostcriticalLifetimeMs
            natural_exit_after_critical = [bool]$TimingProfile.NaturalExit
            natural_exit_after_critical_ms = $TimingProfile.NaturalExitAfterCriticalMs
            emit_safe_connect = $true
            emit_safe_qr = $true
            emit_critical = $true
            emit_later_marker = $true
        }
        $emitter = New-Emitter -Configuration $configuration
        $run = Start-Observer -ContainerId $emitter.Id -Window 120
        Start-Sleep -Milliseconds 400
        $createdInspection = (Invoke-Docker -Arguments @('inspect', $emitter.Id)).Output -join "`n" | ConvertFrom-Json
        $observerWaitedCreated = -not $run.Process.HasExited -and $createdInspection[0].State.Status -eq 'created'
        if (-not $observerWaitedCreated) {
            $failedWait = Wait-Observer -Run $run -Scenario 'A_created_then_started' `
                -TimingProfile $TimingProfile.Name
            Assert-True $observerWaitedCreated 'Observer did not remain waiting during created state'
        }
        Start-Emitter $emitter
        $done = Wait-Observer -Run $run -Scenario 'A_created_then_started' `
            -TimingProfile $TimingProfile.Name
        $diagnostic = $done.Diagnostic
        Assert-True $observerWaitedCreated 'Observer did not remain waiting during created state'
        Assert-True ($diagnostic.structured_exit_code -eq 10 -and
            $diagnostic.process_exit_code -eq 10 -and
            $diagnostic.observer_outcome -eq 'CRITICAL_EVENT') 'Scenario A outcome'
        Assert-True ($done.Result.disconnect_origin -eq 'keepalive_silence') 'Scenario A origin'
        Assert-True ($diagnostic.stop_issued -and $diagnostic.stop_action_count -eq 1 -and
            $diagnostic.stop_confirmed -and
            $diagnostic.container_state_after_stop -in @('exited', 'dead')) 'Scenario A stop confirmation'
        Assert-True ($diagnostic.critical_event_count -eq 1 -and
            $diagnostic.safe_event_count -eq 2 -and
            $diagnostic.unique_parsed_event_count -eq 3 -and
            $diagnostic.connect_started_count -eq 1 -and
            $diagnostic.qr_code_generated_count -eq 1 -and
            $diagnostic.later_marker_count -eq 0) 'Scenario A event accounting'
        Assert-True ($diagnostic.started_at_validated -and $diagnostic.follow_started -and
            $diagnostic.result_written -and $diagnostic.result_atomic) `
            'Scenario A replay/follow/result finalization'
        if ($RecordPass) {
            Add-Pass ("scenario_A_created_then_started[{0}]" -f $TimingProfile.Name) $done.Result
        }
    } finally {
        Remove-TestRun $run
        Remove-Emitter $emitter
    }
}

function Assert-Completion([object]$Done, [string]$Outcome, [int]$ExitCode, [string]$Label) {
    Assert-True ($Done.Diagnostic.observer_outcome -eq $Outcome -and
        $Done.Diagnostic.structured_exit_code -eq $ExitCode -and
        $Done.Diagnostic.process_exit_code -eq $ExitCode -and
        $Done.Diagnostic.result_written -and $Done.Diagnostic.result_atomic) "$Label outcome"
}

function Invoke-StrictTransitionProbe {
    param(
        [string]$ProfileName,
        [ValidateSet(0, 25, 100, 250, 500, 1000)][int]$EventDelayMilliseconds
    )
    $emitter = $null; $run = $null
    try {
        $emitter = New-Emitter -Configuration @{
            mode = 'immediate_critical_exit'
            exit_mode = 'observer_stop'
            event_delay_ms = $EventDelayMilliseconds
            post_emit_hold_ms = 150000
        }
        $run = Start-Observer -ContainerId $emitter.Id -Window 120
        Start-Sleep -Milliseconds 250
        $createdInspection = (Invoke-Docker -Arguments @('inspect', $emitter.Id)).Output -join "`n" | ConvertFrom-Json
        $observerWaitedCreated = -not $run.Process.HasExited -and
            $createdInspection[0].State.Status -eq 'created'
        if (-not $observerWaitedCreated) {
            $null = Wait-Observer -Run $run -Scenario 'transition_durability' `
                -TimingProfile $ProfileName
            Assert-True $observerWaitedCreated 'Transition observer did not wait during created state'
        }
        Start-Emitter $emitter
        $done = Wait-Observer -Run $run -Scenario 'transition_durability' `
            -TimingProfile $ProfileName
        $diagnostic = $done.Diagnostic
        Assert-True $observerWaitedCreated 'Transition observer did not wait during created state'
        Assert-Completion $done 'CRITICAL_EVENT' 10 'Transition durability probe'
        Assert-True ($done.Result.disconnect_origin -eq 'qr_refs_exhausted') `
            'Transition durability origin'
        Assert-True ($diagnostic.critical_event_count -eq 1 -and
            $diagnostic.unique_parsed_event_count -eq 1 -and
            $diagnostic.later_marker_count -eq 0) 'Transition durability event accounting'
        Assert-True ($diagnostic.stop_action_count -eq 1 -and $diagnostic.stop_confirmed -and
            $diagnostic.final_container_state -in @('exited', 'dead')) `
            'Transition durability stop confirmation'
        Assert-True ($diagnostic.started_at_validated -and
            $diagnostic.started_at_validated -and
            $diagnostic.result_written -and $diagnostic.result_atomic) `
            'Transition durability replay/result contract'
    } finally { Remove-TestRun $run; Remove-Emitter $emitter }
}

function Invoke-FastExitCharacterizationProbe {
    param(
        [string]$ProfileName,
        [ValidateSet('natural', 'explicit')][string]$ExitMode
    )
    $emitter = $null; $run = $null
    try {
        $emitter = New-Emitter -Configuration @{
            mode = 'immediate_critical_exit'
            exit_mode = $ExitMode
            event_delay_ms = 0
            post_emit_hold_ms = 0
        }
        $run = Start-Observer -ContainerId $emitter.Id
        Start-Sleep -Milliseconds 250
        $createdInspection = (Invoke-Docker -Arguments @('inspect', $emitter.Id)).Output -join "`n" | ConvertFrom-Json
        $observerWaitedCreated = -not $run.Process.HasExited -and
            $createdInspection[0].State.Status -eq 'created'
        if (-not $observerWaitedCreated) {
            $null = Wait-Observer -Run $run -Scenario 'fast_exit_characterization' `
                -TimingProfile $ProfileName
            Assert-True $observerWaitedCreated 'Fast-exit observer did not wait during created state'
        }
        Start-Emitter $emitter
        $done = Wait-Observer -Run $run -Scenario 'fast_exit_characterization' `
            -TimingProfile $ProfileName -TimeoutSeconds 15
        $diagnostic = $done.Diagnostic
        $cleanCapture = $diagnostic.observer_outcome -eq 'CRITICAL_EVENT' -and
            $diagnostic.structured_exit_code -eq 10 -and $diagnostic.process_exit_code -eq 10 -and
            $diagnostic.critical_event_count -eq 1
        $cleanMiss = $diagnostic.observer_outcome -eq 'CONTAINER_EXITED' -and
            $diagnostic.structured_exit_code -eq 23 -and $diagnostic.process_exit_code -eq 23 -and
            $diagnostic.critical_event_count -eq 0 -and $diagnostic.received_line_count -eq 0
        Assert-True $observerWaitedCreated 'Fast-exit observer did not wait during created state'
        Assert-True ($cleanCapture -or $cleanMiss) `
            'Fast-exit characterization produced neither a clean capture nor a clean exit'
        Assert-True ($diagnostic.observer_outcome -ne 'WINDOW_COMPLETE' -and
            $diagnostic.critical_event_count -le 1 -and
            $diagnostic.result_written -and $diagnostic.result_atomic -and
            $diagnostic.final_container_state -in @('exited', 'dead')) `
            'Fast-exit characterization safety contract'
        if ($cleanCapture) {
            Assert-True ($done.Result.disconnect_origin -eq 'qr_refs_exhausted' -and
                $diagnostic.stop_action_count -eq 1 -and $diagnostic.stop_confirmed -and
                $diagnostic.later_marker_count -eq 0) 'Fast-exit capture contract'
        } else {
            Assert-True ($diagnostic.stop_action_count -eq 0) 'Fast-exit clean-miss stop contract'
        }
        return [pscustomobject]@{ Captured = $cleanCapture; Done = $done }
    } finally { Remove-TestRun $run; Remove-Emitter $emitter }
}

function Invoke-NoLogExitProbe {
    $emitter = $null; $run = $null
    try {
        $emitter = New-Emitter -Configuration @{ mode = 'no_output_exit' }
        Start-Emitter $emitter
        $null = Wait-EmitterState -Emitter $emitter -AllowedStates @('exited', 'dead')
        $run = Start-Observer -ContainerId $emitter.Id
        $done = Wait-Observer -Run $run -Scenario 'L_no_log_exit' `
            -TimingProfile 'no_log_normal_exit' -TimeoutSeconds 15
        $diagnostic = $done.Diagnostic
        Assert-Completion $done 'CONTAINER_EXITED' 23 'No-log exited probe'
        Assert-True ($diagnostic.critical_event_count -eq 0 -and
            $diagnostic.received_line_count -eq 0 -and
            $diagnostic.initial_replay_attempted -and
            $diagnostic.initial_replay_unique_lines -eq 0 -and
            $diagnostic.post_exit_drain_started -and
            $diagnostic.post_exit_drain_attempts -gt 1 -and
            $diagnostic.post_exit_drain_unique_lines -eq 0 -and
            -not $diagnostic.post_exit_quiescence_reached -and
            $diagnostic.final_container_state -in @('exited', 'dead') -and
            $diagnostic.stop_action_count -eq 0) 'No-log exited drain accounting'
        return $done
    } finally { Remove-TestRun $run; Remove-Emitter $emitter }
}

function Invoke-NaturalExitRaceProbe {
    param([string]$ProfileName)
    $emitter = $null; $run = $null
    try {
        $profile = $NaturalExitRaceTimingProfile
        $emitter = New-Emitter -Configuration @{
            mode = 'scenario_a'
            profile_name = $profile.Name
            precritical_delay_ms = $profile.PrecriticalDelayMs
            postcritical_delay_ms = $profile.PostcriticalDelayMs
            postcritical_lifetime_ms = $profile.PostcriticalLifetimeMs
            natural_exit_after_critical = [bool]$profile.NaturalExit
            natural_exit_after_critical_ms = $profile.NaturalExitAfterCriticalMs
            emit_safe_connect = $true
            emit_safe_qr = $true
            emit_critical = $true
            emit_later_marker = $true
        }
        $run = Start-Observer -ContainerId $emitter.Id
        Start-Sleep -Milliseconds 250
        $createdInspection = (Invoke-Docker -Arguments @('inspect', $emitter.Id)).Output -join "`n" | ConvertFrom-Json
        $observerWaitedCreated = -not $run.Process.HasExited -and
            $createdInspection[0].State.Status -eq 'created'
        if (-not $observerWaitedCreated) {
            $null = Wait-Observer -Run $run -Scenario 'natural_exit_race' `
                -TimingProfile $ProfileName
            Assert-True $observerWaitedCreated 'Natural-exit observer did not wait during created state'
        }
        Start-Emitter $emitter
        $done = Wait-Observer -Run $run -Scenario 'natural_exit_race' `
            -TimingProfile $ProfileName -TimeoutSeconds 15
        $diagnostic = $done.Diagnostic
        $cleanCapture = $diagnostic.observer_outcome -eq 'CRITICAL_EVENT' -and
            $diagnostic.structured_exit_code -eq 10 -and $diagnostic.process_exit_code -eq 10 -and
            $diagnostic.critical_event_count -eq 1
        $cleanMiss = $diagnostic.observer_outcome -eq 'CONTAINER_EXITED' -and
            $diagnostic.structured_exit_code -eq 23 -and $diagnostic.process_exit_code -eq 23 -and
            $diagnostic.critical_event_count -eq 0 -and $diagnostic.received_line_count -eq 0
        Assert-True $observerWaitedCreated 'Natural-exit observer did not wait during created state'
        Assert-True ($cleanCapture -or $cleanMiss) `
            'Natural-exit race produced neither a clean capture nor a clean exit'
        Assert-True ($diagnostic.observer_outcome -ne 'WINDOW_COMPLETE' -and
            $diagnostic.critical_event_count -le 1 -and
            $diagnostic.result_written -and $diagnostic.result_atomic -and
            $diagnostic.final_container_state -in @('exited', 'dead')) `
            'Natural-exit race safety contract'
        if ($cleanCapture) {
            Assert-True ($done.Result.disconnect_origin -eq 'keepalive_silence' -and
                $diagnostic.connect_started_count -eq 1 -and
                $diagnostic.qr_code_generated_count -eq 1 -and
                $diagnostic.later_marker_count -eq 0 -and
                $diagnostic.stop_action_count -eq 1 -and $diagnostic.stop_confirmed) `
                'Natural-exit captured-event contract'
        } else {
            Assert-True ($diagnostic.stop_action_count -eq 0) 'Natural-exit clean-miss stop contract'
        }
        return [pscustomobject]@{ Captured = $cleanCapture; Done = $done }
    } finally { Remove-TestRun $run; Remove-Emitter $emitter }
}

function Invoke-TransitionStress {
    $delays = @(0, 25, 100, 250, 500, 1000)
    $totals = @{}
    foreach ($delay in $delays) { $totals[$delay] = 0 }
    for ($repetition = 1; $repetition -le 5; $repetition += 1) {
        foreach ($delay in $delays) {
            $profileName = 'transition.delay{0}ms.repeat{1}' -f $delay, $repetition
            Invoke-StrictTransitionProbe -ProfileName $profileName `
                -EventDelayMilliseconds $delay
            $totals[$delay] += 1
            Write-Output ("transition[{0}]=pass" -f $profileName)
        }
    }
    Assert-True (($totals.Values | Measure-Object -Sum).Sum -eq 30) `
        'Transition stress did not execute exactly 30 runs'
    foreach ($delay in $delays) {
        Assert-True ($totals[$delay] -eq 5) "Transition delay $delay did not execute five times"
    }
    Write-Output 'transition_stress=pass runs=30 delays_ms=0,25,100,250,500,1000 repeats=5'
}

function Invoke-NaturalExitRaceStress {
    $captured = 0; $exited = 0
    for ($iteration = 1; $iteration -le 10; $iteration += 1) {
        $result = Invoke-NaturalExitRaceProbe -ProfileName ("natural_exit_race.{0}" -f $iteration)
        if ($result.Captured) { $captured += 1 } else { $exited += 1 }
        Write-Output ("natural_exit_race[{0}]=pass" -f $iteration)
    }
    Assert-True ($captured + $exited -eq 10) 'Natural-exit race did not execute exactly ten runs'
    Write-Output ("natural_exit_race_stress=pass runs=10 captured={0} exited={1}" -f `
        $captured, $exited)
}

function Invoke-ImmediateExitCharacterization {
    $totals = [ordered]@{
        natural_captured = 0; natural_exited = 0
        explicit_captured = 0; explicit_exited = 0
    }
    for ($cycle = 1; $cycle -le 5; $cycle += 1) {
        foreach ($exitMode in @('natural', 'explicit')) {
            $profileName = 'immediate_exit.{0}.repeat{1}' -f $exitMode, $cycle
            $result = Invoke-FastExitCharacterizationProbe -ProfileName $profileName `
                -ExitMode $exitMode
            $key = if ($result.Captured) { "${exitMode}_captured" } else { "${exitMode}_exited" }
            $totals[$key] += 1
            Write-Output ("immediate_exit[{0}]=pass" -f $profileName)
        }
    }
    Assert-True ($totals.natural_captured + $totals.natural_exited -eq 5) `
        'Natural immediate-exit characterization count'
    Assert-True ($totals.explicit_captured + $totals.explicit_exited -eq 5) `
        'Explicit immediate-exit characterization count'
    Write-Output ("immediate_exit_characterization=pass runs=10 natural_captured={0} natural_exited={1} explicit_captured={2} explicit_exited={3}" -f `
        $totals.natural_captured, $totals.natural_exited,
        $totals.explicit_captured, $totals.explicit_exited)
}

function Invoke-DuplicateReplayStress {
    for ($iteration = 1; $iteration -le 20; $iteration += 1) {
        Invoke-ScenarioH -RecordPass:$false
        Write-Output ("duplicate_replay[{0}]=pass" -f $iteration)
    }
    Write-Output 'duplicate_replay_stress=pass runs=20'
}

function Invoke-RunningInterruptionStress {
    for ($iteration = 1; $iteration -le 20; $iteration += 1) {
        Invoke-ScenarioF -RecordPass:$false
        Write-Output ("running_interruption[{0}]=pass" -f $iteration)
    }
    Write-Output 'running_interruption_stress=pass runs=20'
}

function Stop-EmitterAsOrchestrator {
    param([object]$Emitter)
    $call = Invoke-Docker -Arguments @('stop', '--time', '1', $Emitter.Id) -AllowFailure
    $inspection = Wait-EmitterState -Emitter $Emitter -AllowedStates @('exited', 'dead')
    Assert-True ($call.Code -eq 0 -and $inspection.State.Status -in @('exited', 'dead')) `
        'Orchestrator did not stop the running-window emitter'
}

function Invoke-NonCriticalRunningWindowProbe {
    param([string]$ProfileName = 'noncritical_running_window')
    $emitter = $null; $run = $null
    try {
        $emitter = New-Emitter -Configuration @{ mode = 'heartbeat_hold'; interval_ms = 100 }
        $run = Start-Observer -ContainerId $emitter.Id -Window 2
        Start-Sleep -Milliseconds 250
        Start-Emitter $emitter
        $done = Wait-Observer -Run $run -Scenario 'noncritical_running_window' `
            -TimingProfile $ProfileName -TimeoutSeconds 15
        $ledger = Get-EmitterLedgerMetadata -Emitter $emitter
        $unfiltered = Get-DockerLogChannelEvidence -Emitter $emitter
        [Console]::Out.WriteLine(([ordered]@{
            scenario = 'noncritical_running_window_evidence'
            ledger_entry_count = $ledger.ledger_entry_count
            ledger_contains_critical = $ledger.ledger_contains_critical
            unfiltered_valid_event_count = $unfiltered.valid_event_count
            unfiltered_contains_critical = $unfiltered.contains_critical
            observer_unique_parsed_event_count = $done.Diagnostic.unique_parsed_event_count
            observer_safe_event_count = $done.Diagnostic.safe_event_count
        } | ConvertTo-Json -Compress))
        Assert-Completion $done 'WINDOW_COMPLETE' 0 'Non-critical running window'
        Assert-True ($ledger.ledger_entry_count -gt 0 -and
            -not $ledger.ledger_contains_critical -and
            $unfiltered.valid_event_count -gt 0 -and -not $unfiltered.contains_critical -and
            $done.Diagnostic.unique_parsed_event_count -gt 0 -and
            $done.Diagnostic.safe_event_count -gt 0 -and
            $done.Diagnostic.critical_event_count -eq 0 -and
            $done.Diagnostic.final_replay_attempted -and
            $done.Diagnostic.final_replay_completed -and
            $done.Diagnostic.final_container_state -eq 'running' -and
            $done.Diagnostic.stop_action_count -eq 0) 'Non-critical running-window evidence'
        Stop-EmitterAsOrchestrator -Emitter $emitter
        return $done
    } finally { Remove-TestRun $run; Remove-Emitter $emitter }
}

function Invoke-NonCriticalWindowStress {
    for ($iteration = 1; $iteration -le 20; $iteration += 1) {
        $null = Invoke-NonCriticalRunningWindowProbe -ProfileName ("noncritical.repeat{0}" -f $iteration)
        Write-Output ("noncritical_running_window[{0}]=pass" -f $iteration)
    }
    Write-Output 'noncritical_running_window_stress=pass runs=20'
}

function Invoke-FinalReconciliationRecoveryProbe {
    param([string]$ProfileName = 'final_reconciliation_recovery')
    $emitter = $null; $run = $null
    try {
        $emitter = New-Emitter -Configuration @{
            mode = 'numbered_critical_hold'; interval_ms = 100
            critical_sequence = 50; minimum_events = 50; hold_ms = 150000
        }
        $run = Start-Observer -ContainerId $emitter.Id -Window 8 `
            -HoldFollowDeliveryForFinalReconciliation
        Start-Sleep -Milliseconds 250
        Start-Emitter $emitter
        $snapshotDeadline = [DateTimeOffset]::UtcNow.AddSeconds(7)
        do {
            Start-Sleep -Milliseconds 100
            $snapshot = Get-DockerSnapshotOracle -Emitter $emitter -RequireRunning
        } while (-not $snapshot.docker_snapshot_contains_critical -and
            [DateTimeOffset]::UtcNow -lt $snapshotDeadline)
        [Console]::Out.WriteLine(($snapshot | ConvertTo-Json -Compress))
        Assert-True $snapshot.docker_snapshot_contains_critical `
            'Final reconciliation Docker oracle did not expose the critical event'
        $done = Wait-Observer -Run $run -Scenario 'final_reconciliation_recovery' `
            -TimingProfile $ProfileName -TimeoutSeconds 15
        Assert-Completion $done 'CRITICAL_EVENT' 10 'Final reconciliation recovery'
        Assert-True ($done.Diagnostic.critical_event_count -eq 1 -and
            $done.Diagnostic.critical_event_phase -eq 'stream_recovery' -and
            $done.Diagnostic.follow_stdout_line_count -gt 0 -and
            $done.Diagnostic.follow_discarded_line_count -gt 0 -and
            $done.Diagnostic.final_replay_attempted -and
            $done.Diagnostic.final_replay_completed -and
            $done.Diagnostic.stop_action_count -eq 1 -and $done.Diagnostic.stop_confirmed) `
            'Final reconciliation did not recover Docker-visible evidence'
        return $done
    } finally { Remove-TestRun $run; Remove-Emitter $emitter }
}

function Invoke-ScenarioARegression {
    for ($iteration = 1; $iteration -le 2; $iteration += 1) {
        Invoke-ScenarioA -TimingProfile $ProductionLikeTimingProfile
        Write-Output ("scenario_A_regression[production_like.repeat{0}]=pass" -f $iteration)
    }
    Write-Output 'scenario_A_regression=pass profiles=1 repeats=2 runs=2'
}

function Invoke-HeldRunningCriticalStress {
    for ($iteration = 1; $iteration -le $HeldRunningIterations; $iteration += 1) {
        $emitter = $null
        $run = $null
        try {
            $emitter = New-Emitter -Configuration @{
                mode = 'numbered_critical_hold'
                interval_ms = 100
                critical_sequence = 50
                minimum_events = 50
                hold_ms = 150000
            }
            Start-Emitter $emitter
            $null = Wait-EmitterState -Emitter $emitter -AllowedStates @('running')

            $ledgerDeadline = [DateTimeOffset]::UtcNow.AddSeconds(8)
            Start-Sleep -Milliseconds 5250
            do {
                $ledger = Get-EmitterLedgerMetadata -Emitter $emitter
                if ($ledger.ledger_entry_count -ge 50 -and $ledger.ledger_contains_critical) { break }
                Start-Sleep -Milliseconds 250
            } while (($ledger.ledger_entry_count -lt 50 -or -not $ledger.ledger_contains_critical) -and
                [DateTimeOffset]::UtcNow -lt $ledgerDeadline)

            $containerEvidence = Get-DiagnosticContainerEvidence -Emitter $emitter
            $unfiltered = Get-DockerLogChannelEvidence -Emitter $emitter
            if ($ledger.ledger_entry_count -gt 0 -and $unfiltered.valid_event_count -eq 0 -and
                $containerEvidence.Safe.container_running) {
                foreach ($additionalWait in @(500, 1000, 1500)) {
                    Start-Sleep -Milliseconds $additionalWait
                    $containerEvidence = Get-DiagnosticContainerEvidence -Emitter $emitter
                    $unfiltered = Get-DockerLogChannelEvidence -Emitter $emitter
                    if ($unfiltered.valid_event_count -gt 0) { break }
                }
            }
            $run = Start-Observer -ContainerId $emitter.Id -Window 3
            $done = Wait-Observer -Run $run -Scenario 'held_running_critical' `
                -TimingProfile ("held_running.repeat{0}" -f $iteration) -TimeoutSeconds 15

            $safeBoundary = [ordered]@{
                held_running_iteration = $iteration
                container_running = $containerEvidence.Safe.container_running
                container_restart_count = $containerEvidence.Safe.container_restart_count
                restart_policy = $containerEvidence.Safe.restart_policy
                log_driver = $containerEvidence.Safe.log_driver
                attach_stdout = $containerEvidence.Safe.attach_stdout
                attach_stderr = $containerEvidence.Safe.attach_stderr
                tty_enabled = $containerEvidence.Safe.tty_enabled
                started_at_valid = $containerEvidence.Safe.started_at_valid
                node_process_present = $containerEvidence.Safe.node_process_present
                ledger_entry_count = $ledger.ledger_entry_count
                ledger_first_sequence = $ledger.ledger_first_sequence
                ledger_last_sequence = $ledger.ledger_last_sequence
                ledger_contains_critical = $ledger.ledger_contains_critical
                unfiltered_log_line_count = $unfiltered.log_line_count
                unfiltered_valid_event_count = $unfiltered.valid_event_count
                unfiltered_first_sequence = $unfiltered.first_sequence
                unfiltered_last_sequence = $unfiltered.last_sequence
                unfiltered_contains_critical = $unfiltered.contains_critical
                observer_received_line_count = $done.Diagnostic.received_line_count
                observer_unique_parsed_event_count = $done.Diagnostic.unique_parsed_event_count
                observer_critical_event_count = $done.Diagnostic.critical_event_count
                observer_first_sequence = $done.Diagnostic.observer_first_sequence
                observer_last_sequence = $done.Diagnostic.observer_last_sequence
                observer_outcome = $done.Diagnostic.observer_outcome
                follow_started = $done.Diagnostic.follow_started
                final_reconciliation_attempted = $done.Diagnostic.final_replay_attempted
            }

            $classification = $null
            if ($ledger.ledger_entry_count -eq 0) {
                $classification = 'EMITTER_OUTPUT_DEFECT'
            } elseif ($containerEvidence.Safe.container_running -and
                $unfiltered.valid_event_count -eq 0) {
                $classification = 'DOCKER_LOG_UNAVAILABLE'
            } elseif ($unfiltered.valid_event_count -gt 0 -and
                $done.Diagnostic.received_line_count -eq 0) {
                $classification = 'OBSERVER_CAPTURE_DEFECT'
            } elseif ($done.Diagnostic.received_line_count -gt 0 -and
                $done.Diagnostic.unique_parsed_event_count -eq 0) {
                $classification = 'PARSER_OR_DEDUP_DEFECT'
            } elseif ($unfiltered.contains_critical -and
                ($done.Diagnostic.critical_event_count -eq 0 -or
                    $done.Diagnostic.observer_outcome -eq 'WINDOW_COMPLETE')) {
                $classification = 'WINDOW_FINALIZATION_DEFECT'
            }

            [Console]::Out.WriteLine(($safeBoundary | ConvertTo-Json -Compress))
            if ($null -ne $classification) {
                throw ("Diagnostic boundary mismatch: {0}" -f $classification)
            }

            Assert-True ($containerEvidence.Safe.container_running -and
                $containerEvidence.Safe.container_restart_count -eq 0 -and
                $containerEvidence.Safe.restart_policy -eq 'no' -and
                $containerEvidence.Safe.attach_stdout -and $containerEvidence.Safe.attach_stderr -and
                -not $containerEvidence.Safe.tty_enabled -and
                $containerEvidence.Safe.started_at_valid -and
                $containerEvidence.Safe.node_process_present) `
                'Dual-channel container configuration contract'
            Assert-True ($ledger.ledger_entry_count -eq 50 -and
                $ledger.ledger_first_sequence -eq 1 -and
                $ledger.ledger_last_sequence -eq 50 -and $ledger.ledger_contains_critical) `
                'Dual-channel ledger contract'
            Assert-True ($unfiltered.valid_event_count -eq 50 -and
                $unfiltered.first_sequence -eq 1 -and $unfiltered.last_sequence -eq 50 -and
                $unfiltered.contains_critical) 'Dual-channel unfiltered Docker contract'
            Assert-Completion $done 'CRITICAL_EVENT' 10 'Dual-channel observer contract'
            Assert-True ($done.Diagnostic.unique_parsed_event_count -eq 50 -and
                $done.Diagnostic.critical_event_count -eq 1 -and
                $done.Diagnostic.observer_first_sequence -eq 1 -and
                $done.Diagnostic.observer_last_sequence -eq 50 -and
                $done.Diagnostic.stop_action_count -eq 1 -and
                $done.Diagnostic.stop_confirmed) 'Dual-channel observer evidence contract'
            Write-Output ("held_running_critical[{0}]=pass" -f $iteration)
        } finally {
            if ($null -ne $emitter) {
                $state = Invoke-Docker -Arguments @('inspect', $emitter.Id) -AllowFailure
                if ($state.Code -eq 0) {
                    $inspection = @($state.Output -join "`n" | ConvertFrom-Json)[0]
                    if ([string]$inspection.State.Status -eq 'running') {
                        $null = Invoke-Docker -Arguments @('stop', '--time', '1', $emitter.Id) -AllowFailure
                    }
                }
            }
            Remove-TestRun $run
            Remove-Emitter $emitter
        }
    }
    Write-Output ("held_running_critical_stress=pass runs={0}" -f $HeldRunningIterations)
}

function Invoke-ScenarioB {
    $result = Invoke-FastExitCharacterizationProbe -ProfileName 'scenario_B' -ExitMode natural
    Add-Pass 'scenario_E_fast_exit' $result.Done.Result
}

function Invoke-ScenarioC {
    $emitter = $null; $run = $null
    try {
        $emitter = New-Emitter -Configuration @{ mode = 'idle_hold'; hold_ms = 5000 }
        $run = Start-Observer -ContainerId $emitter.Id -StartTimeout 1
        $done = Wait-Observer -Run $run -Scenario 'C_start_timeout' -TimeoutSeconds 10
        Assert-Completion $done 'START_TIMEOUT' 22 'Scenario C'
        Assert-True ($done.Diagnostic.container_state_after_stop -eq 'created' -and
            $done.Diagnostic.stop_action_count -eq 0) 'Scenario C created state'
        Add-Pass 'scenario_G_start_timeout' $done.Result
    } finally { Remove-TestRun $run; Remove-Emitter $emitter }
}

function Invoke-ScenarioE {
    $emitter = $null; $run = $null
    try {
        $emitter = New-Emitter -Configuration @{
            mode = 'malformed_critical_hold'; event_delay_ms = 1000; hold_ms = 60000
        }
        $run = Start-Observer -ContainerId $emitter.Id -Window 10
        Start-Sleep -Milliseconds 200; Start-Emitter $emitter
        $done = Wait-Observer -Run $run -Scenario 'E_malformed_critical'
        Assert-Completion $done 'OBSERVER_FAILURE' 20 'Scenario E'
        Assert-True ($done.Result.event_name -eq 'malformed_critical_line' -and
            $done.Diagnostic.stop_action_count -eq 1 -and $done.Diagnostic.stop_confirmed) `
            'Scenario E malformed/stop'
        Assert-True (-not $done.Console.Contains('malformed connection')) 'Scenario E raw leak'
        Add-Pass 'scenario_H_malformed_critical' $done.Result
    } finally { Remove-TestRun $run; Remove-Emitter $emitter }
}

function Invoke-ScenarioF {
    param([switch]$RecordPass)
    $emitter = $null; $run = $null
    try {
        $emitter = New-Emitter -Configuration @{ mode = 'connect_hold'; hold_ms = 150000 }
        $run = Start-Observer -ContainerId $emitter.Id -ForceStreamInterruption 400
        Start-Sleep -Milliseconds 200; Start-Emitter $emitter
        $done = Wait-Observer -Run $run -Scenario 'F_running_stream_interruption'
        Assert-Completion $done 'OBSERVER_FAILURE' 20 'Scenario F'
        Assert-True ($done.Result.event_name -eq 'running_stream_ended' -and
            $done.Diagnostic.follow_exit_observed -and
            $done.Diagnostic.container_state_at_follow_exit -eq 'running' -and
            -not $done.Diagnostic.post_exit_drain_started -and
            $done.Diagnostic.stop_action_count -eq 1 -and $done.Diagnostic.stop_confirmed) `
            'Scenario F stream/stop'
        if ($RecordPass) { Add-Pass 'scenario_I_running_stream_interruption' $done.Result }
    } finally { Remove-TestRun $run; Remove-Emitter $emitter }
}

function Invoke-ScenarioG {
    $emitter = $null; $run = $null
    try {
        $emitter = New-Emitter -Configuration @{ mode = 'idle_hold'; hold_ms = 5000 }
        $wrong = 'sha256:' + ('0' * 64)
        $run = Start-Observer -ContainerId $emitter.Id -ExpectedImage $wrong
        $done = Wait-Observer -Run $run -Scenario 'G_image_mismatch' -TimeoutSeconds 10
        Assert-Completion $done 'IDENTITY_MISMATCH' 21 'Scenario G'
        Assert-True ($done.Diagnostic.container_state_after_stop -eq 'created' -and
            $done.Diagnostic.stop_action_count -eq 0) 'Scenario G target state'
        Add-Pass 'scenario_J_image_mismatch' $done.Result
    } finally { Remove-TestRun $run; Remove-Emitter $emitter }
}

function Invoke-ScenarioH {
    param([switch]$RecordPass)
    $emitter = $null; $run = $null
    try {
        $emitter = New-Emitter -Configuration @{
            mode = 'delayed_unknown_critical_hold'; critical_delay_ms = 3000; hold_ms = 60000
        }
        $run = Start-Observer -ContainerId $emitter.Id -Window 10
        Start-Sleep -Milliseconds 1500; Start-Emitter $emitter
        $done = Wait-Observer -Run $run -Scenario 'H_duplicate_replay'
        Assert-Completion $done 'CRITICAL_EVENT' 10 'Scenario H'
        Assert-True ($done.Diagnostic.critical_event_count -eq 1 -and
            $done.Diagnostic.stop_action_count -eq 1 -and $done.Diagnostic.stop_confirmed -and
            $done.Diagnostic.duplicate_line_count -ge 1 -and
            $done.Diagnostic.later_marker_count -eq 0 -and $done.Diagnostic.started_at_validated) `
            'Scenario H duplicate accounting'
        if ($RecordPass) { Add-Pass 'scenario_K_duplicate_replay_follow' $done.Result }
    } finally { Remove-TestRun $run; Remove-Emitter $emitter }
}

function Invoke-ScenarioI {
    $first = $null; $second = $null; $firstRun = $null; $secondRun = $null
    try {
        $first = New-Emitter -Configuration @{
            mode = 'noise_then_connect_hold'; noise_count = 2; emit_connect = $true
            event_delay_ms = 250; hold_ms = 60000
        }
        $firstRun = Start-Observer -ContainerId $first.Id -Window 2 -StopOnWindow -Threshold 2
        Start-Sleep -Milliseconds 200; Start-Emitter $first
        $ok = Wait-Observer -Run $firstRun -Scenario 'I_allowed_noise'
        Assert-Completion $ok 'WINDOW_COMPLETE' 0 'Scenario I allowed noise'
        Assert-True ($ok.Result.parse_failure_count -eq 2) 'Scenario I allowed-noise count'
        Remove-TestRun $firstRun; $firstRun = $null
        Remove-Emitter $first; $first = $null

        $second = New-Emitter -Configuration @{
            mode = 'noise_then_connect_hold'; noise_count = 3; emit_connect = $false
            event_delay_ms = 250; hold_ms = 60000
        }
        $secondRun = Start-Observer -ContainerId $second.Id -Window 10 -Threshold 2
        Start-Sleep -Milliseconds 200; Start-Emitter $second
        $failed = Wait-Observer -Run $secondRun -Scenario 'I_parse_threshold'
        Assert-Completion $failed 'OBSERVER_FAILURE' 20 'Scenario I threshold'
        Assert-True ($failed.Result.parse_failure_count -eq 3) 'Scenario I threshold count'
        Add-Pass 'scenario_L_non_json_threshold' $failed.Result
    } finally {
        Remove-TestRun $firstRun; Remove-TestRun $secondRun
        Remove-Emitter $first; Remove-Emitter $second
    }
}

function Invoke-ScenarioJ {
    $emitter = $null; $run = $null
    try {
        $markers = @(
            'synthetic-message-marker', 'synthetic-stack-marker', 'synthetic-cause-marker',
            'synthetic-data-marker', 'synthetic-authorization-marker', 'synthetic-qr-marker',
            'synthetic-jid-marker', 'synthetic-phone-marker', 'synthetic-session-marker',
            'synthetic-stderr-marker', '1|safe', '50|critical'
        )
        $emitter = New-Emitter -Configuration @{
            mode = 'secret_critical_hold'; event_delay_ms = 1000; hold_ms = 60000
        }
        $run = Start-Observer -ContainerId $emitter.Id -Window 10
        Start-Sleep -Milliseconds 200; Start-Emitter $emitter
        $done = Wait-Observer -Run $run -Scenario 'J_secret_redaction'
        $serialized = ($done.Result | ConvertTo-Json -Compress) + $done.Console +
            ($done.Diagnostic | ConvertTo-Json -Compress)
        foreach ($marker in $markers) { Assert-True (-not $serialized.Contains($marker)) 'Scenario J secret leak' }
        Assert-Completion $done 'CRITICAL_EVENT' 10 'Scenario J'
        Add-Pass 'scenario_M_redaction' $done.Result
    } finally { Remove-TestRun $run; Remove-Emitter $emitter }
}

function Invoke-ScenarioK {
    $emitter = $null; $run = $null
    $invalidEmitter = $null; $invalidRun = $null
    $atomicEmitter = $null; $atomicRun = $null
    try {
        $emitter = New-Emitter -Configuration @{ mode = 'safe_exit' }
        $run = Start-Observer -ContainerId $emitter.Id
        Start-Sleep -Milliseconds 200; Start-Emitter $emitter
        $done = Wait-Observer -Run $run -Scenario 'K_container_exited'
        Assert-Completion $done 'CONTAINER_EXITED' 23 'Scenario K exited'
        Remove-TestRun $run; $run = $null
        Remove-Emitter $emitter; $emitter = $null

        $invalidEmitter = New-Emitter -Configuration @{
            mode = 'invalid_origin_hold'; event_delay_ms = 1000; hold_ms = 60000
        }
        $invalidRun = Start-Observer -ContainerId $invalidEmitter.Id -Window 10
        Start-Sleep -Milliseconds 200; Start-Emitter $invalidEmitter
        $invalid = Wait-Observer -Run $invalidRun -Scenario 'K_invalid_origin'
        Assert-Completion $invalid 'OBSERVER_FAILURE' 20 'Scenario K invalid origin'
        Assert-True ($invalid.Result.event_name -eq 'invalid_disconnect_origin' -and
            $invalid.Diagnostic.critical_event_count -eq 0 -and
            $invalid.Diagnostic.stop_action_count -eq 1 -and $invalid.Diagnostic.stop_confirmed) `
            'Scenario K invalid-origin accounting'
        Remove-TestRun $invalidRun; $invalidRun = $null
        Remove-Emitter $invalidEmitter; $invalidEmitter = $null

        $atomicEmitter = New-Emitter -Configuration @{ mode = 'connect_hold'; hold_ms = 3000 }
        $atomicRun = Start-Observer -ContainerId $atomicEmitter.Id -Window 1 -StopOnWindow `
            -ForceAtomicResultWriteFailure
        Start-Sleep -Milliseconds 200; Start-Emitter $atomicEmitter
        $atomic = Wait-Observer -Run $atomicRun -Scenario 'K_atomic_write_failure' -AllowMissingResult
        $atomicTemps = @(Get-ChildItem -LiteralPath (Split-Path $atomicRun.Result -Parent) `
            -Filter ((Split-Path $atomicRun.Result -Leaf) + '.*.tmp') -File -ErrorAction SilentlyContinue)
        Assert-True ($atomic.Diagnostic.process_exit_code -eq 20 -and
            -not $atomic.Diagnostic.result_written -and -not $atomic.Diagnostic.result_atomic -and
            $atomicTemps.Count -eq 0) 'Scenario K atomic failure cleanup'

        $outcomes = @($ScenarioResults.Outcome) + $done.Result.outcome + $invalid.Result.outcome
        foreach ($required in @('CRITICAL_EVENT','START_TIMEOUT','WINDOW_COMPLETE','OBSERVER_FAILURE','IDENTITY_MISMATCH','CONTAINER_EXITED')) {
            Assert-True ($required -in $outcomes) "Scenario K missing branch $required"
        }
        Add-Pass 'scenario_N_outcomes_and_failure_branches' $done.Result
    } finally {
        Remove-TestRun $run; Remove-TestRun $invalidRun; Remove-TestRun $atomicRun
        Remove-Emitter $emitter; Remove-Emitter $invalidEmitter; Remove-Emitter $atomicEmitter
    }
}

function Invoke-ScenarioL {
    $done = Invoke-NoLogExitProbe
    Add-Pass 'scenario_F_no_log_exit' $done.Result
}

function Invoke-HeldRunningNumberedScenario {
    $emitter = $null; $run = $null
    try {
        $emitter = New-Emitter -Configuration @{
            mode = 'numbered_critical_hold'; interval_ms = 100
            critical_sequence = 50; minimum_events = 50; hold_ms = 150000
        }
        Start-Emitter $emitter
        $null = Wait-EmitterState -Emitter $emitter -AllowedStates @('running')
        $ledgerDeadline = [DateTimeOffset]::UtcNow.AddSeconds(8)
        Start-Sleep -Milliseconds 5250
        do {
            $ledger = Get-EmitterLedgerMetadata -Emitter $emitter
            if ($ledger.ledger_entry_count -eq 50 -and $ledger.ledger_contains_critical) { break }
            Start-Sleep -Milliseconds 250
        } while ([DateTimeOffset]::UtcNow -lt $ledgerDeadline)
        $unfiltered = Get-DockerLogChannelEvidence -Emitter $emitter
        $run = Start-Observer -ContainerId $emitter.Id -Window 5
        $done = Wait-Observer -Run $run -Scenario 'B_held_running_numbered' -TimeoutSeconds 15
        Assert-Completion $done 'CRITICAL_EVENT' 10 'Scenario B'
        Assert-True ($ledger.ledger_entry_count -eq 50 -and
            $ledger.ledger_first_sequence -eq 1 -and $ledger.ledger_last_sequence -eq 50 -and
            $ledger.ledger_contains_critical -and
            $unfiltered.valid_event_count -eq 50 -and
            $unfiltered.first_sequence -eq 1 -and $unfiltered.last_sequence -eq 50 -and
            $unfiltered.contains_critical -and
            $done.Diagnostic.unique_parsed_event_count -eq 50 -and
            $done.Diagnostic.critical_event_count -eq 1 -and
            $done.Diagnostic.observer_first_sequence -eq 1 -and
            $done.Diagnostic.observer_last_sequence -eq 50 -and
            $done.Diagnostic.stop_confirmed) 'Scenario B dual-channel evidence'
        Add-Pass 'scenario_B_held_running_numbered' $done.Result
    } finally { Remove-TestRun $run; Remove-Emitter $emitter }
}

function Assert-HarnessRunBoundary {
    Assert-True ($CreatedContainers.Count -eq 0) 'Tracked test container remained after run'
    $remainingContainers = @(
        (Invoke-Docker -Arguments @(
            'ps', '-a', '--filter', 'name=mbb-observer-', '--format', '{{.ID}}'
        )).Output | Where-Object { -not [string]::IsNullOrWhiteSpace([string]$_) }
    )
    Assert-True ($remainingContainers.Count -eq 0) 'MBB observer test container remained after run'
    $remainingNetworks = @(
        (Invoke-Docker -Arguments @(
            'network', 'ls', '--filter', 'name=mbb-observer-', '--format', '{{.ID}}'
        )).Output | Where-Object { -not [string]::IsNullOrWhiteSpace([string]$_) }
    )
    $remainingVolumes = @(
        (Invoke-Docker -Arguments @(
            'volume', 'ls', '--filter', 'name=mbb-observer-', '--format', '{{.Name}}'
        )).Output | Where-Object { -not [string]::IsNullOrWhiteSpace([string]$_) }
    )
    Assert-True ($remainingNetworks.Count -eq 0) 'MBB observer test network remained after run'
    Assert-True ($remainingVolumes.Count -eq 0) 'MBB observer test volume remained after run'
    $unexpectedTempFiles = @($TempFiles | Where-Object { $_ -ne $EmitterSourcePath })
    Assert-True ($unexpectedTempFiles.Count -eq 0) 'Per-run temporary observer artifact remained'
    $atomicTemps = @(Get-ChildItem -LiteralPath $env:TEMP -File -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -like 'mbb-observer-*-result.json.*.tmp' })
    Assert-True ($atomicTemps.Count -eq 0) 'Observer atomic temporary file remained after run'
}

function Invoke-FullSuite {
    param([ValidateRange(1, 3)][int]$RunNumber = 1)
    Assert-HarnessRunBoundary
    $ScenarioResults.Clear()
    Invoke-ScenarioA -TimingProfile $ProductionLikeTimingProfile -RecordPass
    Invoke-HeldRunningNumberedScenario
    $window = Invoke-NonCriticalRunningWindowProbe -ProfileName 'scenario_C'
    Add-Pass 'scenario_C_no_critical_running_window' $window.Result
    $reconciliation = Invoke-FinalReconciliationRecoveryProbe -ProfileName 'scenario_D'
    Add-Pass 'scenario_D_final_reconciliation_recovery' $reconciliation.Result
    Invoke-ScenarioB
    Invoke-ScenarioL
    Invoke-ScenarioC
    Invoke-ScenarioE
    Invoke-ScenarioF -RecordPass
    Invoke-ScenarioG
    Invoke-ScenarioH -RecordPass
    Invoke-ScenarioI
    Invoke-ScenarioJ
    Invoke-ScenarioK
    Assert-True ($ScenarioResults.Count -eq 14) 'Full suite did not record exactly 14 scenarios'
    Assert-HarnessRunBoundary
    Write-Output ("full_suite[{0}]=pass scenarios=14" -f $RunNumber)
}

function Invoke-FullSuiteThree {
    for ($runNumber = 1; $runNumber -le 3; $runNumber += 1) {
        Invoke-FullSuite -RunNumber $runNumber
    }
    Write-Output 'full_suite_three=pass runs=3 scenarios_per_run=14'
}

try {
    foreach ($file in @($Observer, $PSCommandPath)) {
        $tokens = $null; $errors = $null
        [void][Management.Automation.Language.Parser]::ParseFile($file, [ref]$tokens, [ref]$errors)
        Assert-True ($errors.Count -eq 0) "PowerShell parser errors in $file"
    }
    $actual = (Invoke-Docker -Arguments @('image','inspect',$Image,'--format','{{.Id}}')).Output[-1]
    Assert-True ([string]$actual -eq $ImageId) 'Retained classifier image mismatch'
    $DefaultLoggingDriver = [string](
        (Invoke-Docker -Arguments @('info', '--format', '{{.LoggingDriver}}')).Output[-1]
    )
    Assert-True (-not [string]::IsNullOrWhiteSpace($DefaultLoggingDriver)) `
        'Docker default logging driver was unavailable'
    $EmitterSourcePath = New-EmitterSource
    Test-EmitterSourceSyntax
    if ($SyntaxOnly) {
        Write-Output 'observer_suite=syntax_only'
    } elseif ($HeldRunningCriticalStressOnly) {
        Invoke-HeldRunningCriticalStress
    } elseif ($TransitionStressOnly) {
        Invoke-TransitionStress
    } elseif ($NaturalExitRaceOnly) {
        Invoke-NaturalExitRaceStress
    } elseif ($ImmediateExitCharacterizationOnly) {
        Invoke-ImmediateExitCharacterization
    } elseif ($DuplicateReplayStressOnly) {
        Invoke-DuplicateReplayStress
    } elseif ($RunningInterruptionStressOnly) {
        Invoke-RunningInterruptionStress
    } elseif ($NonCriticalWindowStressOnly) {
        Invoke-NonCriticalWindowStress
    } elseif ($FinalReconciliationOnly) {
        $null = Invoke-FinalReconciliationRecoveryProbe
        Write-Output 'final_reconciliation_recovery=pass runs=1'
    } elseif ($ScenarioARegressionOnly) {
        Invoke-ScenarioARegression
    } elseif ($FullSuiteThreeOnly) {
        Invoke-FullSuiteThree
    } else {
        Invoke-FullSuite
    }
    Assert-HarnessRunBoundary
    Write-Output 'observer_harness=pass'
} finally {
    foreach ($id in @($CreatedContainers)) { $null = Invoke-Docker -Arguments @('rm','--force',$id) -AllowFailure }
    foreach ($path in @($TempFiles)) { Remove-Item -LiteralPath $path -Force -ErrorAction SilentlyContinue }
}
