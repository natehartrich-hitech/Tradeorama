<#
.SYNOPSIS
  Runs Tradeorama's weather paper trader as a Windows scheduled task that starts at boot,
  with nobody signed in, and starts again on its own if it exits. (The sports bot is run by
  hand, in a terminal window, by the owner's choice.)

.DESCRIPTION
  From an administrator PowerShell, in the Tradeorama folder:

    powershell -ExecutionPolicy Bypass -File .\scripts\bots.ps1 install    # create the task, start it now
    powershell -ExecutionPolicy Bypass -File .\scripts\bots.ps1 status     # running? last log lines
    powershell -ExecutionPolicy Bypass -File .\scripts\bots.ps1 stop       # stop it (it starts again at boot)
    powershell -ExecutionPolicy Bypass -File .\scripts\bots.ps1 start      # start it now
    powershell -ExecutionPolicy Bypass -File .\scripts\bots.ps1 uninstall  # stop it and remove the task

  The task is "Weather bot" under \Tradeorama\ in Task Scheduler. It runs
  scripts\run-weather-bot.cmd, which appends the bot's output to logs\weather-bot.log.
  To watch it live:

    Get-Content .\logs\weather-bot.log -Tail 30 -Wait

  The task runs as you, without storing your password ("S4U" logon). If install reports a
  logon error, run it again with -StorePassword: Task Scheduler then asks for your Windows
  password (for a Microsoft account, that account's password, not the PIN) and keeps it.
#>
param(
    [Parameter(Position = 0)]
    [ValidateSet('install', 'uninstall', 'start', 'stop', 'status')]
    [string]$Command = 'status',
    [switch]$StorePassword
)

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
$TaskPath = '\Tradeorama\'
$Bots = @(
    @{ Name = 'Weather bot'; Launcher = 'run-weather-bot.cmd'; Log = 'weather-bot.log'
       Python = 'weather\.venv\Scripts\python.exe'; Pattern = '-m\s+wxbrackets\s+paper(\s|$)' }
)

function Assert-Admin {
    $principal = New-Object Security.Principal.WindowsPrincipal ([Security.Principal.WindowsIdentity]::GetCurrent())
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw "Run this from an administrator PowerShell (right-click PowerShell > Run as administrator)."
    }
}

function Get-Task($bot) {
    Get-ScheduledTask -TaskPath $TaskPath -TaskName $bot.Name -ErrorAction SilentlyContinue
}

# The bot's python processes. A venv's python.exe starts the real interpreter as a child with
# the same arguments, so this usually finds two per bot. Also finds bots started by hand.
function Get-BotProcesses($bot) {
    @(Get-CimInstance Win32_Process -Filter "Name = 'python.exe' OR Name = 'pythonw.exe'" |
        Where-Object { $_.CommandLine -and $_.CommandLine -match $bot.Pattern })
}

function Get-LauncherProcesses($bot) {
    $name = [regex]::Escape($bot.Launcher)
    @(Get-CimInstance Win32_Process -Filter "Name = 'cmd.exe'" |
        Where-Object { $_.CommandLine -and $_.CommandLine -match $name })
}

function Stop-Ids($procs) {
    foreach ($p in $procs) { Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue }
}

function Stop-Bot($bot) {
    if (Get-Task $bot) { Stop-ScheduledTask -TaskPath $TaskPath -TaskName $bot.Name }
    # The launcher first, so it can't start the bot again, then the bot itself.
    Stop-Ids (Get-LauncherProcesses $bot)
    Stop-Ids (Get-BotProcesses $bot)
}

function Show-Status {
    foreach ($bot in $Bots) {
        Write-Host ""
        Write-Host "== $($bot.Name) ==" -ForegroundColor Cyan
        $task = Get-Task $bot
        if ($task) {
            $info = $task | Get-ScheduledTaskInfo
            Write-Host ("Task: {0}, last started {1}" -f $task.State, $info.LastRunTime)
        } else {
            Write-Host "Task: not installed (run: bots.ps1 install)"
        }
        $procs = Get-BotProcesses $bot
        if ($procs.Count) {
            $started = ($procs | Sort-Object CreationDate | Select-Object -First 1).CreationDate
            Write-Host "Running since $started (PIDs $(($procs | ForEach-Object { $_.ProcessId }) -join ', '))"
        } else {
            Write-Host "No bot process found. (Without admin rights, processes the task started may not be visible.)"
        }
        $log = Join-Path $Root "logs\$($bot.Log)"
        if (Test-Path $log) {
            Write-Host "Last lines of logs\$($bot.Log):"
            Get-Content $log -Tail 6 -Encoding UTF8 | ForEach-Object { Write-Host "  $_" }
        }
    }
}

function Install-Bots {
    Assert-Admin
    foreach ($bot in $Bots) {
        foreach ($file in @("scripts\$($bot.Launcher)", $bot.Python)) {
            if (-not (Test-Path (Join-Path $Root $file))) { throw "Missing $file under $Root" }
        }
    }

    if ($Bots | Where-Object { Get-Task $_ }) {
        Write-Host "The task already exists; updating it and restarting the bot."
        foreach ($bot in $Bots) { Stop-Bot $bot }
    } else {
        $running = @($Bots | ForEach-Object { Get-BotProcesses $_ })
        if ($running.Count) {
            Write-Host "The weather bot is already running, probably in a terminal window:" -ForegroundColor Yellow
            $running | ForEach-Object { Write-Host "  PID $($_.ProcessId): $($_.CommandLine)" }
            Write-Host "Two copies would both paper trade, so it has to stop first."
            $answer = Read-Host "Stop them now? [y/N]"
            if ($answer -notmatch '^(y|yes)$') {
                Write-Host "Nothing changed. Close that window (or press Ctrl+C in it), then run install again."
                return
            }
            Stop-Ids $running
        }
    }

    $user = [Security.Principal.WindowsIdentity]::GetCurrent().Name
    $password = $null
    if ($StorePassword) {
        $cred = Get-Credential -UserName $user -Message "Windows password for $user, kept by Task Scheduler so the bot can run while nobody is signed in"
        $password = $cred.GetNetworkCredential().Password
    }

    foreach ($bot in $Bots) {
        $launcher = Join-Path $Root "scripts\$($bot.Launcher)"
        $action = New-ScheduledTaskAction -Execute "$env:SystemRoot\System32\cmd.exe" `
            -Argument "/d /c `"`"$launcher`"`"" -WorkingDirectory $Root
        $trigger = New-ScheduledTaskTrigger -AtStartup
        $trigger.Delay = 'PT2M'  # give the network a moment after boot
        $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
            -StartWhenAvailable -MultipleInstances IgnoreNew `
            -ExecutionTimeLimit ([TimeSpan]::Zero) `
            -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 5)
        # ExecutionTimeLimit 0 = no limit. The default (3 days) would kill the bot on day 3.
        $description = "Tradeorama $($bot.Name.ToLower()), paper trading only. Set up by scripts\bots.ps1; output in logs\$($bot.Log)."
        $common = @{ TaskName = $bot.Name; TaskPath = $TaskPath; Action = $action; Trigger = $trigger
                     Settings = $settings; Description = $description; Force = $true }
        if ($password) {
            Register-ScheduledTask @common -User $user -Password $password -RunLevel Limited | Out-Null
        } else {
            $principal = New-ScheduledTaskPrincipal -UserId $user -LogonType S4U -RunLevel Limited
            Register-ScheduledTask @common -Principal $principal | Out-Null
        }
        Start-ScheduledTask -TaskPath $TaskPath -TaskName $bot.Name
        Write-Host "Installed and started: $($bot.Name)" -ForegroundColor Green
    }

    Start-Sleep -Seconds 8
    Show-Status
    Write-Host ""
    Write-Host "From now on the weather bot starts 2 minutes after every boot, signed in or not."
}

function Start-Bots {
    Assert-Admin
    foreach ($bot in $Bots) {
        if (-not (Get-Task $bot)) { throw "$($bot.Name) isn't installed. Run: bots.ps1 install" }
        if ((Get-BotProcesses $bot).Count) { Write-Host "$($bot.Name) is already running."; continue }
        Start-ScheduledTask -TaskPath $TaskPath -TaskName $bot.Name
        Write-Host "Started: $($bot.Name)"
    }
}

function Stop-Bots {
    Assert-Admin
    foreach ($bot in $Bots) { Stop-Bot $bot; Write-Host "Stopped: $($bot.Name)" }
    Write-Host "It starts again at the next boot, or with: bots.ps1 start"
    Write-Host "If you're leaving it off, pause its Healthchecks check so you don't get an alert."
}

function Uninstall-Bots {
    Assert-Admin
    foreach ($bot in $Bots) {
        Stop-Bot $bot
        if (Get-Task $bot) { Unregister-ScheduledTask -TaskPath $TaskPath -TaskName $bot.Name -Confirm:$false }
        Write-Host "Removed: $($bot.Name)"
    }
}

switch ($Command) {
    'install'   { Install-Bots }
    'uninstall' { Uninstall-Bots }
    'start'     { Start-Bots }
    'stop'      { Stop-Bots }
    'status'    { Show-Status }
}
