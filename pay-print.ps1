# pay-print.ps1 - blocking "pay to print" dialog for the kiosk. Shows the cost, and only prints
# (via the IPP engine to the Brother) after payment is confirmed. Standalone/testable; the same
# logic drops into the kioskbar. ASCII-only source (Hebrew via [char]0xXXXX).
param(
  [Parameter(Mandatory=$true)][string]$Pdf,
  [double]$Rate = 0.25,
  [string]$Cur  = '$',
  [string]$PrinterHost = '192.168.10.50',
  [string]$Python = 'python',
  [string]$Engine = 'C:\Users\nates\RawPrint\ipp_print_pdf.py'
)
Add-Type -AssemblyName System.Windows.Forms, System.Drawing

# --- page count from the PDF (via the engine's dryrun, or fallback 1) ---
$pages = 1
try {
  $o = & $Python $Engine $Pdf --dryrun 2>&1
  $m = [regex]::Match(($o -join "`n"), 'pages=(\d+)')
  if ($m.Success) { $pages = [int]$m.Groups[1].Value }
} catch {}
$cost = '{0}{1:0.00}' -f $Cur, ($pages * $Rate)

# --- palette ---
$cBg=[System.Drawing.Color]::FromArgb(248,250,252); $cCard=[System.Drawing.Color]::White
$cInk=[System.Drawing.Color]::FromArgb(17,24,39);   $cMut=[System.Drawing.Color]::FromArgb(100,116,139)
$cAcc=[System.Drawing.Color]::FromArgb(13,125,140);  $cGreen=[System.Drawing.Color]::FromArgb(21,122,77)
$cLine=[System.Drawing.Color]::FromArgb(214,222,230)

$f = New-Object System.Windows.Forms.Form
$f.Text='Payment required'; $f.FormBorderStyle='FixedDialog'; $f.StartPosition='CenterScreen'
$f.Width=460; $f.Height=430; $f.BackColor=$cBg; $f.MaximizeBox=$false; $f.MinimizeBox=$false; $f.TopMost=$true

$card = New-Object System.Windows.Forms.Panel
$card.SetBounds(24,24,404,352); $card.BackColor=$cCard
$f.Controls.Add($card)

# printer glyph header
$t = New-Object System.Windows.Forms.Label
$t.Text='Ready to print'; $t.Font=New-Object System.Drawing.Font('Segoe UI',17,[System.Drawing.FontStyle]::Bold)
$t.ForeColor=$cInk; $t.SetBounds(28,24,348,32); $card.Controls.Add($t)
# Hebrew: "tashlum lifney hadpasa" -> keep simple: "hadpasa" (printing)
$he = New-Object System.Windows.Forms.Label
$he.Text=(-join ([char]0x05D4,[char]0x05D3,[char]0x05E4,[char]0x05E1,[char]0x05D4))  # HDPSH
$he.Font=New-Object System.Drawing.Font('Segoe UI',13); $he.ForeColor=$cMut; $he.RightToLeft='Yes'
$he.SetBounds(28,58,348,26); $card.Controls.Add($he)

$sub = New-Object System.Windows.Forms.Label
$sub.Text='This print costs money. Please pay at the desk, then press Print.'
$sub.Font=New-Object System.Drawing.Font('Segoe UI',10); $sub.ForeColor=$cMut
$sub.SetBounds(28,92,348,40); $card.Controls.Add($sub)

# cost block
$price = New-Object System.Windows.Forms.Label
$price.Text=$cost; $price.Font=New-Object System.Drawing.Font('Segoe UI',40,[System.Drawing.FontStyle]::Bold)
$price.ForeColor=$cGreen; $price.SetBounds(28,138,348,64); $price.TextAlign='MiddleCenter'
$card.Controls.Add($price)
$calc = New-Object System.Windows.Forms.Label
$calc.Text=("{0} page{1}  x  {2}{3:0.00}" -f $pages, $(if($pages -eq 1){''}else{'s'}), $Cur, $Rate)
$calc.Font=New-Object System.Drawing.Font('Segoe UI',11); $calc.ForeColor=$cMut
$calc.SetBounds(28,206,348,24); $calc.TextAlign='MiddleCenter'; $card.Controls.Add($calc)

# status line (hidden until printing)
$status = New-Object System.Windows.Forms.Label
$status.Text=''; $status.Font=New-Object System.Drawing.Font('Segoe UI',10,[System.Drawing.FontStyle]::Bold)
$status.SetBounds(28,238,348,24); $status.TextAlign='MiddleCenter'; $card.Controls.Add($status)

# buttons
$btnPay = New-Object System.Windows.Forms.Button
$btnPay.Text='Paid - Print'; $btnPay.SetBounds(28,278,240,52)
$btnPay.FlatStyle='Flat'; $btnPay.FlatAppearance.BorderSize=0; $btnPay.BackColor=$cAcc; $btnPay.ForeColor=[System.Drawing.Color]::White
$btnPay.Font=New-Object System.Drawing.Font('Segoe UI',13,[System.Drawing.FontStyle]::Bold); $btnPay.Cursor='Hand'
$card.Controls.Add($btnPay)
$btnCancel = New-Object System.Windows.Forms.Button
$btnCancel.Text='Cancel'; $btnCancel.SetBounds(280,278,96,52)
$btnCancel.FlatStyle='Flat'; $btnCancel.FlatAppearance.BorderColor=$cLine; $btnCancel.BackColor=$cCard; $btnCancel.ForeColor=$cMut
$btnCancel.Font=New-Object System.Drawing.Font('Segoe UI',11); $btnCancel.Cursor='Hand'
$card.Controls.Add($btnCancel)

$btnCancel.Add_Click({ $f.Tag='cancel'; $f.Close() })
$btnPay.Add_Click({
  $btnPay.Enabled=$false; $btnCancel.Enabled=$false
  $status.ForeColor=$cAcc; $status.Text=('Sending ' + $pages + ' page(s) to the printer...')
  $f.Refresh()
  try {
    $out = & $Python $Engine $Pdf --host $PrinterHost 2>&1
    if ($LASTEXITCODE -eq 0) {
      $status.ForeColor=$cGreen; $status.Text='Printed. Collect your pages at the printer.'
      $f.Tag='printed'
      $tmr = New-Object System.Windows.Forms.Timer; $tmr.Interval=2500
      $tmr.Add_Tick({ $tmr.Stop(); $f.Close() }); $tmr.Start()
    } else {
      $status.ForeColor=[System.Drawing.Color]::FromArgb(179,38,30)
      $status.Text='Print failed - check the printer, or ask the desk.'
      $btnPay.Enabled=$true; $btnCancel.Enabled=$true
    }
  } catch {
    $status.ForeColor=[System.Drawing.Color]::FromArgb(179,38,30); $status.Text='Print error.'
    $btnPay.Enabled=$true; $btnCancel.Enabled=$true
  }
})

[void]$f.ShowDialog()
"result: $($f.Tag)"
