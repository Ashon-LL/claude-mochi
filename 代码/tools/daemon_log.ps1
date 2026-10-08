# Search the whole daemon log for the lines that report what the device parsed.
#
# The tail alone is useless here: the log is dominated by a PONG/STATUS pair every
# 2.5 s, so 130 lines back is only a few minutes — and the upload happened before
# that. The line that matters ("slot: state N <- slot M (... bytes, N moving)") is
# emitted once per binding and scrolls away immediately.
#
# It also exists because the console log cannot be opened from cmd: the daemon
# holds it in append mode.

$path = Join-Path $env:APPDATA 'ClaudeHUD\logs\daemon-console.log'
if (-not (Test-Path $path)) { Write-Output "no log at $path"; exit 1 }

$all = Get-Content $path
Write-Output "log has $($all.Count) lines, modified $((Get-Item $path).LastWriteTime)"
Write-Output ""

$hits = $all | Select-String -Pattern 'slot:|moving|reject|render:|store:|device:|daemon starting|hooks installed|adopting|spawning'
if (-not $hits) {
  Write-Output "NO MATCHING LINES AT ALL."
  Write-Output "That itself is the finding: the device never reported parsing a face,"
  Write-Output "so its diagnostic line was never emitted."
}
else {
  foreach ($h in $hits) { Write-Output ("{0}: {1}" -f $h.LineNumber, $h.Line) }
}
