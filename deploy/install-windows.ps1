# Cài / cập nhật Camera AI trên Windows 10 / 11, chạy trong Ubuntu của WSL2.
#
# Mở PowerShell bằng quyền Administrator (chuột phải nút Start → Terminal (Admin)) rồi chạy:
#   irm https://raw.githubusercontent.com/tomdo199127/camera-ai-vn/main/deploy/install-windows.ps1 | iex
#
# Lần đầu, nếu máy chưa có WSL / Ubuntu, script cài chúng rồi dừng: khởi động lại máy nếu được hỏi, mở Ubuntu tạo
# tài khoản Linux, sau đó chạy lại đúng lệnh trên. Chạy lại bất cứ lúc nào để cập nhật; cấu hình, dữ liệu được giữ nguyên.
& {
  $ErrorActionPreference = 'Stop'
  $env:WSL_UTF8 = '1'   # wsl.exe in ra UTF-8 thay vì UTF-16
  $RawBase = 'https://raw.githubusercontent.com/tomdo199127/camera-ai-vn/main/deploy'
  $DefaultDistro = 'Ubuntu-24.04'
  $TaskName = 'Camera AI'
  $AppDir = Join-Path $env:ProgramData 'CameraAI'
  $WslCreator = '{40E0AC32-46A5-438A-A0B2-2B479E8F2E90}'   # VMCreatorId của WSL trong tường lửa Hyper-V

  function Say($msg, $color = 'Cyan') { Write-Host $msg -ForegroundColor $color }
  function Clean($lines) { @($lines | ForEach-Object { ($_ -replace "`0", '').Trim() } | Where-Object { $_ }) }

  $me = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
  if (-not $me.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Say 'Cần chạy PowerShell bằng quyền Administrator: chuột phải nút Start → Terminal (Admin), rồi chạy lại lệnh.' Yellow
    return
  }
  $build = [Environment]::OSVersion.Version.Build
  if ($build -lt 19041) { Say "Windows quá cũ (build $build). Cần Windows 10 bản 2004 trở lên hoặc Windows 11." Yellow; return }
  # Windows 11 22H2 trở lên: Ubuntu dùng chung IP với Windows (mirrored), máy khác trong nhà vào thẳng được.
  # Windows 10: Ubuntu nằm sau NAT, phải chuyển tiếp cổng web mỗi lần khởi động (IP của Ubuntu đổi sau mỗi lần).
  $Mirrored = $build -ge 22621

  # --- WSL + Ubuntu ---
  $distros = @()
  if (Get-Command wsl.exe -ErrorAction SilentlyContinue) {
    try { $distros = Clean (& wsl.exe -l -q 2>$null) } catch { $distros = @() }
    if ($LASTEXITCODE -ne 0) { $distros = @() }
  }
  $Distro = if ($env:CAMERA_AI_DISTRO) { $env:CAMERA_AI_DISTRO } else { $distros | Where-Object { $_ -like 'Ubuntu*' } | Select-Object -First 1 }
  if (-not $Distro -or $distros -notcontains $Distro) {
    Say "Cài WSL2 và $DefaultDistro (vài phút)..."
    & wsl.exe --install -d $DefaultDistro
    Say ''
    Say 'Xong bước 1. Tiếp theo:' Green
    Say '  1. Nếu Windows yêu cầu, khởi động lại máy.'
    Say "  2. Nếu chưa được hỏi tạo tài khoản: mở '$DefaultDistro' từ menu Start, đặt tên và mật khẩu cho tài khoản Linux"
    Say '     (nhớ mật khẩu này, lúc cài sẽ hỏi).'
    Say '  3. Mở lại Terminal (Admin) và chạy lại đúng lệnh cài.'
    return
  }
  Say "Dùng bản Linux: $Distro"

  $user = (Clean (& wsl.exe -d $Distro --exec whoami)) | Select-Object -First 1
  if (-not $user -or $user -eq 'root') {
    Say "$Distro đang đăng nhập bằng root. Mở $Distro, tạo tài khoản thường (sudo adduser <tên> && sudo usermod -aG sudo <tên>)," Yellow
    Say "đặt làm mặc định trong /etc/wsl.conf ([user] default=<tên>), chạy 'wsl --shutdown' rồi chạy lại lệnh cài." Yellow
    return
  }

  # --- cấu hình WSL: bật systemd (các dịch vụ tự chạy), mạng mirrored, không tắt máy ảo khi rảnh ---
  $restart = $false
  $sysd = & wsl.exe -d $Distro -u root --exec sh -c "grep -qs '^ *systemd *= *true' /etc/wsl.conf && echo ok || { printf '\n[boot]\nsystemd=true\n' >> /etc/wsl.conf; echo added; }"
  if ((Clean $sysd) -contains 'added') { $restart = $true }

  $cfgPath = Join-Path $env:USERPROFILE '.wslconfig'
  $lines = [System.Collections.Generic.List[string]]::new()
  if (Test-Path $cfgPath) { foreach ($l in Get-Content $cfgPath) { $lines.Add($l) } }
  $want = [ordered]@{ vmIdleTimeout = '-1' }
  if ($Mirrored) { $want.networkingMode = 'mirrored' }
  $changed = $false
  foreach ($k in $want.Keys) {
    $sec = -1
    for ($i = 0; $i -lt $lines.Count; $i++) { if ($lines[$i].Trim() -ieq '[wsl2]') { $sec = $i; break } }
    if ($sec -lt 0) { $lines.Add('[wsl2]'); $sec = $lines.Count - 1 }
    $end = $lines.Count
    for ($i = $sec + 1; $i -lt $lines.Count; $i++) { if ($lines[$i].Trim().StartsWith('[')) { $end = $i; break } }
    $found = -1
    for ($i = $sec + 1; $i -lt $end; $i++) { if ($lines[$i] -match "^\s*$k\s*=") { $found = $i; break } }
    $line = "$k=$($want[$k])"
    if ($found -ge 0) {
      if ($lines[$found].Trim() -ne $line) { $lines[$found] = $line; $changed = $true }
    } else { $lines.Insert($end, $line); $changed = $true }
  }
  if ($changed) {
    if (Test-Path $cfgPath) { Copy-Item $cfgPath "$cfgPath.bak" -Force; Say "Đã lưu bản cũ: $cfgPath.bak" }
    Set-Content -Path $cfgPath -Value $lines -Encoding ascii
    $restart = $true
  }
  if ($restart) {
    Say 'Khởi động lại WSL để áp dụng cấu hình...'
    & wsl.exe --shutdown
    Start-Sleep -Seconds 3
  }

  # --- cài Camera AI bên trong Ubuntu (script Linux, sẽ hỏi mật khẩu Linux cho sudo) ---
  Say "Cài Camera AI trong $Distro (lần đầu mất 10-20 phút)..."
  & wsl.exe -d $Distro --cd '~' --exec bash -lc "curl -fsSL $RawBase/install.sh | bash"
  if ($LASTEXITCODE -ne 0) { Say 'Cài trong Ubuntu bị lỗi (xem thông báo ở trên). Sửa xong chạy lại lệnh cài.' Red; return }

  $port = (Clean (& wsl.exe -d $Distro --exec sh -c "grep -E '^WEB_PORT=' ~/lpr-server/.env | cut -d= -f2")) | Select-Object -First 1
  if ($port -notmatch '^\d+$') { $port = '8080' }

  # --- tự chạy khi đăng nhập Windows: mở Ubuntu (systemd chạy các dịch vụ) và giữ nó chạy nền ---
  New-Item -ItemType Directory -Force -Path $AppDir | Out-Null
  $start = Join-Path $AppDir 'start.ps1'
  $forward = if ($Mirrored) { '' } else { @"
Start-Sleep -Seconds 15
`$ip = ((wsl.exe -d '$Distro' --exec hostname -I) -replace "``0", '' -split '\s+' | Where-Object { `$_ })[0]
netsh interface portproxy delete v4tov4 listenport=$port listenaddress=0.0.0.0 | Out-Null
if (`$ip) { netsh interface portproxy add v4tov4 listenport=$port listenaddress=0.0.0.0 connectport=$port connectaddress=`$ip | Out-Null }
"@ }
  Set-Content -Path $start -Encoding utf8 -Value @"
# Tạo bởi install-windows.ps1 của Camera AI. Chạy khi đăng nhập Windows (Task Scheduler: '$TaskName').
`$env:WSL_UTF8 = '1'
Start-Process wsl.exe -ArgumentList '-d', '$Distro', '--exec', 'sleep', 'infinity' -WindowStyle Hidden
$forward
"@
  $action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$start`""
  $trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
  $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero)
  Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
    -User "$env:USERDOMAIN\$env:USERNAME" -RunLevel Highest -Force | Out-Null
  Start-ScheduledTask -TaskName $TaskName

  # --- tường lửa: cho máy trong nhà vào web (chỉ mạng Private / Domain) ---
  Get-NetFirewallRule -DisplayName 'Camera AI web' -ErrorAction SilentlyContinue | Remove-NetFirewallRule
  New-NetFirewallRule -DisplayName 'Camera AI web' -Direction Inbound -Protocol TCP -LocalPort $port -Profile Private, Domain -Action Allow | Out-Null
  if ($Mirrored -and (Get-Command New-NetFirewallHyperVRule -ErrorAction SilentlyContinue)) {
    Remove-NetFirewallHyperVRule -Name 'CameraAI-Web' -ErrorAction SilentlyContinue
    New-NetFirewallHyperVRule -Name 'CameraAI-Web' -DisplayName 'Camera AI web' -Direction Inbound -VMCreatorId $WslCreator -Protocol TCP -LocalPorts $port | Out-Null
  }

  $ip = Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
    Where-Object { $_.PrefixOrigin -in 'Dhcp', 'Manual' -and $_.IPAddress -notlike '169.254.*' } |
    Select-Object -First 1 -ExpandProperty IPAddress
  Say ''
  Say 'Xong.' Green
  Say "  Trên máy này:            http://localhost:$port"
  Say "  Điện thoại / máy khác:   http://$($ip):$port   (mạng Wi-Fi của Windows phải đặt là Private)"
  Say '  Tài khoản mặc định: admin / 123456Aa@  — đăng nhập rồi đổi mật khẩu ngay (menu Thêm → Đổi mật khẩu).'
  Say "  Camera AI tự chạy mỗi khi đăng nhập Windows (Task Scheduler: '$TaskName')."
  Say '  Muốn tự chạy cả sau khi mất điện: bật tự đăng nhập Windows (gõ netplwiz) và đặt BIOS tự bật máy khi có điện.'
}
