<#
Registers the two daily Windows scheduled tasks (PLAN 3.2 and 7.1):

  LibraryAgent-DailyJob  reminders, overdue alerts, school email, reservation expiry
  LibraryAgent-Backup    copies the database into BACKUP_DIR

Run it once on the school machine from the repository folder:

  .\scripts\schedule-tasks.ps1 -BackupDir "C:\Users\<you>\OneDrive\LibraryBackups"

Point -BackupDir at a folder that syncs to cloud storage. Settings such as
SMTP_HOST or LIBRARY_DB can go in a .env-style file passed with -EnvFile
(one NAME=value per line). Both tasks are safe to run twice a day.
Remove the tasks with -Remove.
#>
param(
    [string]$BackupDir,
    [string]$DailyJobTime = "07:00",
    [string]$BackupTime = "18:00",
    [string]$EnvFile = "",
    [switch]$Remove
)

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
$python = Join-Path $repo ".venv\Scripts\python.exe"
$names = "LibraryAgent-DailyJob", "LibraryAgent-Backup"

if ($Remove) {
    foreach ($n in $names) {
        if (Get-ScheduledTask -TaskName $n -ErrorAction SilentlyContinue) {
            Unregister-ScheduledTask -TaskName $n -Confirm:$false
            Write-Host "Removed $n"
        }
    }
    return
}

if (-not $BackupDir) { throw "Give -BackupDir: a folder that syncs to cloud storage." }
if (-not (Test-Path $python)) { throw "No virtual environment at $python. Follow Setup in README.md first." }
New-Item -ItemType Directory -Force $BackupDir | Out-Null
$logDir = Join-Path $repo "logs"
New-Item -ItemType Directory -Force $logDir | Out-Null

# Each task runs a small cmd line so environment settings and a log file come along.
function New-Command([string]$command, [string]$log) {
    $sets = @("set BACKUP_DIR=$BackupDir")
    if ($EnvFile) {
        foreach ($line in Get-Content $EnvFile) {
            if ($line -match '^\s*([A-Z_][A-Z0-9_]*)=(.*)$') { $sets += "set $($Matches[1])=$($Matches[2])" }
        }
    }
    $body = ($sets -join " && ") + " && `"$python`" -m library_agent $command >> `"$logDir\$log`" 2>&1"
    return New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/c $body" -WorkingDirectory $repo
}

$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -DontStopIfGoingOnBatteries -AllowStartIfOnBatteries

Register-ScheduledTask -TaskName "LibraryAgent-DailyJob" -Force -Settings $settings `
    -Action (New-Command "daily-job" "daily-job.log") `
    -Trigger (New-ScheduledTaskTrigger -Daily -At $DailyJobTime) `
    -Description "Library agent: reminders, overdue alerts, school email, reservation expiry" | Out-Null

Register-ScheduledTask -TaskName "LibraryAgent-Backup" -Force -Settings $settings `
    -Action (New-Command "backup" "backup.log") `
    -Trigger (New-ScheduledTaskTrigger -Daily -At $BackupTime) `
    -Description "Library agent: daily database backup to $BackupDir" | Out-Null

Write-Host "Scheduled LibraryAgent-DailyJob at $DailyJobTime and LibraryAgent-Backup at $BackupTime."
Write-Host "Logs go to $logDir. Test now with: Start-ScheduledTask -TaskName LibraryAgent-Backup"
