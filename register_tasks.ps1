# =====================================================================
#  注册本工具的 Windows 计划任务（值守 / 收尾 / 出稿 / 清理 四段）
#
#  用法（管理员权限 PowerShell）：
#      powershell -ExecutionPolicy Bypass -File .\register_tasks.ps1
#
#  卸载：
#      Get-ScheduledTask -TaskName 'LR-*' | Unregister-ScheduledTask -Confirm:$false
#
#  为什么必须用计划任务而不是常驻前台进程：
#      一场直播常 2-5 小时，远超任何交互式会话或 CI 作业的时限。计划任务能
#      跨越登录会话存活，并支持 WakeToRun / StartWhenAvailable。
#
#  注意：任务以「当前用户 + 仅登录时运行」注册，因为 Whisper 模型缓存在
#        C:\Users\<你>\.cache\huggingface；换成 SYSTEM 账户时 "~" 会解析到
#        C:\Windows\system32\config\systemprofile，模型找不到。
#        机器需保持开机；休眠时由 WakeToRun 尝试唤醒。
#
#  ⚠ 本文件含中文，必须存成 UTF-8 **带 BOM**：PowerShell 5.1 读无 BOM 脚本会
#    按系统 ANSI 码页（中文环境是 GBK）解码，UTF-8 中文字节被错切后会吞掉相邻的
#    续行反引号，报出完全对不上位置的行号。仓库里这一份已经带 BOM，改动后请保持。
# =====================================================================
$ErrorActionPreference = 'Stop'

# 脚本所在目录即项目根；需要指向别处就改这一行
$proj = $PSScriptRoot
$py   = Join-Path $proj 'venv\Scripts\python.exe'
$cfg  = Join-Path $proj 'anchors.json'

if (-not (Test-Path $py))  { throw "python not found: $py（先建好项目内 venv）" }
if (-not (Test-Path $cfg)) { throw "config not found: $cfg（从 anchors.example.json 复制一份并填房间号）" }

$userId = "$env:USERDOMAIN\$env:USERNAME"
$principal = New-ScheduledTaskPrincipal -UserId $userId -LogonType Interactive -RunLevel Limited

function Register-LrTask {
    param(
        [string]$Name,
        [string]$Script,
        [string]$Arguments,
        [string]$At,
        [double]$LimitHours
    )
    $action = New-ScheduledTaskAction -Execute $py `
        -Argument "`"$Script`" $Arguments" -WorkingDirectory $proj
    $trigger = New-ScheduledTaskTrigger -Daily -At $At
    $settings = New-ScheduledTaskSettingsSet -WakeToRun `
        -ExecutionTimeLimit (New-TimeSpan -Hours $LimitHours) `
        -MultipleInstances IgnoreNew `
        -StartWhenAvailable `
        -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
    Register-ScheduledTask -TaskName $Name -Action $action -Trigger $trigger `
        -Settings $settings -Principal $principal `
        -Description "LiveRecorder scheduled job: $Name" -Force | Out-Null
    Write-Host ("registered: {0,-24} daily {1}" -f $Name, $At)
}

# --- ① 值守段（录制，长跑）---------------------------------------------
# 06:00 起守，每 600s 探测一次，最长 6 小时 -> 12:00 由 --run-for 强制收尾
Register-LrTask -Name 'LR-Watch-AM' -Script (Join-Path $proj 'scheduler.py') `
    -Arguments "--config `"$cfg`" --interval 600 --run-for 21600" -At '06:00' -LimitHours 8

# 13:00 起守，最长 5 小时 -> 18:00 强制收尾
Register-LrTask -Name 'LR-Watch-PM' -Script (Join-Path $proj 'scheduler.py') `
    -Arguments "--config `"$cfg`" --interval 600 --run-for 18000" -At '13:00' -LimitHours 7

# --- ② 收尾段（补转写 + 封片，幂等）------------------------------------
# --since-days 2 = 扫「今天 + 昨天」。必须 >= 2：catchup.py 的日期集合是
#   {今天 - i | i < N}，N=1 时只扫当天 —— 某次收尾段漏跑（关机、转写中途被杀），
#   那天的稿子就再也没人补。多扫一天，次日自动自愈。
Register-LrTask -Name 'LR-Catchup-AM' -Script (Join-Path $proj 'catchup.py') `
    -Arguments '--since-days 2' -At '12:15' -LimitHours 3

Register-LrTask -Name 'LR-Catchup-PM' -Script (Join-Path $proj 'catchup.py') `
    -Arguments '--since-days 2' -At '18:15' -LimitHours 3

# --- ③ 出稿段（转写稿 -> 笔记 md + 归档 summary.md）---------------------
# 排在收尾段之后：收尾段先把转写补齐，这里才拿得到 *.timed.txt。
# 必须排在清理段之前 —— 清理段的三重闸门之一就是「笔记 md 已落地」。
#
# 幂等：笔记 md 不比转写稿旧就跳过，所以多跑几次没有副作用，漏跑一次次日也会补上。
# 需要 .env 里的 LLM_API_KEY；没配则整体退出码 2，不会写出半成品 —— 若确实想先
# 不上模型，把下面的参数改成 '--since-days 2 --no-llm'，会落「时间轴骨架」稿。
Register-LrTask -Name 'LR-Digest-AM' -Script (Join-Path $proj 'digest.py') `
    -Arguments '--since-days 2' -At '12:30' -LimitHours 2

Register-LrTask -Name 'LR-Digest-PM' -Script (Join-Path $proj 'digest.py') `
    -Arguments '--since-days 2' -At '18:30' -LimitHours 2

# --- ④ 清理段（出稿 md 已落地 -> 释放原始媒体，只留转写稿）---------------
# 21:00，排在出稿环节之后，确保 md 已经写出来。
# 删不删完全由 purge_media 的三重闸门决定（转写完整 / 确实停写 / 已出稿且 md
# 晚于转写稿），任何一条不满足就跳过，所以早跑晚跑都不会误删。
#
# --purge-trash 走回收站（SHFileOperationW + FOF_ALLOWUNDO），文件可恢复。
#   ⚠ 回收站**不释放磁盘空间**，它只是「删错了还能捞回来」的缓冲。想真正腾空间
#     要定期清空回收站，或改用 --purge 直接 unlink。
Register-LrTask -Name 'LR-Purge' -Script (Join-Path $proj 'catchup.py') `
    -Arguments '--since-days 2 --purge --purge-trash' -At '21:00' -LimitHours 1

Write-Host ''
Get-ScheduledTask -TaskName 'LR-*' | Select-Object TaskName, State | Format-Table -AutoSize
