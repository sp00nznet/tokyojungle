# Tokyo Jungle controls editor: rebind keyboard keys / mouse buttons and Xbox
# (XInput) buttons. Writes controls.ini in the repo root (next to scripts/); the running game
# picks the change up within a second.
param([string]$Path = (Join-Path (Split-Path $PSScriptRoot -Parent) 'controls.ini'))

Add-Type -AssemblyName System.Windows.Forms, System.Drawing
Add-Type -Namespace TjControls -Name Keys -MemberDefinition '[DllImport("user32.dll")] public static extern short GetAsyncKeyState(int vKey);'
[System.Windows.Forms.Application]::EnableVisualStyles()

# Actions in file order (must match s_pad_acts in libs/input/cellPad.c): name, label, controller-bindable
$actions = @(
    # Labels say what each PS3 input does in Tokyo Jungle (same text as the in-game F1 screen).
    @('cross', 'Jump  (Cross)', $true), @('circle', 'Eat / mark / carry  (Circle)', $true),
    @('square', 'Claw attack  (Square)', $true), @('triangle', 'Pack: fight / decoy  (Triangle)', $true),
    @('l1', 'Creep  (L1)', $true), @('r1', 'Bite  (R1)', $true),
    @('l2', 'Pack commands  (L2)', $true), @('r2', 'R2', $true), @('l3', 'L3 (left stick click)', $true),
    @('r3', 'Roar  (R3)', $true), @('start', 'Pause / map  (Start)', $true), @('select', 'Info  (Select)', $true),
    @('up', 'Items  (D-pad up)', $true), @('down', 'D-pad down', $true), @('left', 'D-pad left', $true),
    @('right', 'D-pad right', $true),
    @('lstick_up', 'Move up', $false), @('lstick_down', 'Move down', $false),
    @('lstick_left', 'Move left', $false), @('lstick_right', 'Move right', $false),
    @('rstick_up', 'Dodge up  (right stick)', $false), @('rstick_down', 'Dodge down', $false),
    @('rstick_left', 'Dodge left', $false), @('rstick_right', 'Dodge right', $false)
)

# Virtual-key code -> name (must match libs/input/cellPad.c). A plain hashtable:
# an [ordered] dictionary would treat integer keys as positions.
$vkNames = @{
    0x20 = 'Space'; 0x0D = 'Enter'; 0x08 = 'Backspace'; 0x09 = 'Tab'; 0x1B = 'Escape'; 0x14 = 'CapsLock'
    0x10 = 'Shift'; 0xA0 = 'LShift'; 0xA1 = 'RShift'; 0x11 = 'Ctrl'; 0xA2 = 'LCtrl'; 0xA3 = 'RCtrl'
    0x12 = 'Alt'; 0xA4 = 'LAlt'; 0xA5 = 'RAlt'; 0x26 = 'Up'; 0x28 = 'Down'; 0x25 = 'Left'; 0x27 = 'Right'
    0x2D = 'Insert'; 0x2E = 'Delete'; 0x24 = 'Home'; 0x23 = 'End'; 0x21 = 'PageUp'; 0x22 = 'PageDown'
    0x6A = 'NumpadMultiply'; 0x6B = 'NumpadAdd'; 0x6D = 'NumpadSubtract'; 0x6E = 'NumpadDecimal'; 0x6F = 'NumpadDivide'
    0xBA = 'Semicolon'; 0xBB = 'Equals'; 0xBC = 'Comma'; 0xBD = 'Minus'; 0xBE = 'Period'; 0xBF = 'Slash'
    0xC0 = 'Backtick'; 0xDB = 'LeftBracket'; 0xDC = 'Backslash'; 0xDD = 'RightBracket'; 0xDE = 'Quote'
    0x01 = 'MouseLeft'; 0x02 = 'MouseRight'; 0x04 = 'MouseMiddle'; 0x05 = 'Mouse4'; 0x06 = 'Mouse5'
}
for ($c = 0x41; $c -le 0x5A; $c++) { $vkNames[$c] = [string][char]$c }
for ($c = 0x30; $c -le 0x39; $c++) { $vkNames[$c] = [string][char]$c }
for ($n = 0; $n -le 9; $n++) { $vkNames[0x60 + $n] = "Numpad$n" }
# F1 (in-game Controls screen) and F2 are reserved; the game ignores them as bindings.
for ($n = 3; $n -le 24; $n++) { $vkNames[0x70 + $n - 1] = "F$n" }
$vkOrder = @($vkNames.Keys | Sort-Object)
$mouseVk = @(0x01, 0x02, 0x04, 0x05, 0x06)
# Generic modifiers are reported alongside the L/R ones; capture prefers the specific key.
$genericVk = @(0x10, 0x11, 0x12)
$padButtons = @('None', 'A', 'B', 'X', 'Y', 'LB', 'RB', 'LT', 'RT', 'LS', 'RS', 'Start', 'Back',
                'DpadUp', 'DpadDown', 'DpadLeft', 'DpadRight')
$keyNameCanon = @{}
foreach ($v in $vkNames.Values) { $keyNameCanon[$v.ToLower()] = $v }

$defaults = @{
    keyboard = @{ cross = 'Space, Z'; circle = 'F'; square = 'E'; triangle = 'V'; l1 = 'LCtrl'; r1 = 'LShift'
        l2 = '1'; r2 = '3'; l3 = ''; r3 = 'CapsLock'; start = 'Enter'; select = 'Backspace'; up = 'Numpad8'; down = 'Numpad2'
        left = 'Numpad4'; right = 'Numpad6'; lstick_up = 'W'; lstick_down = 'S'; lstick_left = 'A'; lstick_right = 'D'
        rstick_up = 'Up'; rstick_down = 'Down'; rstick_left = 'Left'; rstick_right = 'Right' }
    controller = @{ cross = 'A'; circle = 'B'; square = 'X'; triangle = 'Y'; l1 = 'LB'; r1 = 'RB'; l2 = 'LT'
        r2 = 'RT'; l3 = 'LS'; r3 = 'RS'; start = 'Start'; select = 'Back'; up = 'DpadUp'; down = 'DpadDown'
        left = 'DpadLeft'; right = 'DpadRight' }
}

function Split-Keys([string]$value) {
    @($value -split ',' | ForEach-Object { $_.Trim() } | Where-Object { $_ })
}

# Same rules as the game's parser: unknown keyboard names are dropped (all unknown ->
# default); an unknown controller button keeps the default; empty / None unbinds.
function Read-Bindings([string]$file) {
    $kb = @{}; $pad = @{}
    foreach ($k in $defaults.keyboard.Keys) { $kb[$k] = @(Split-Keys $defaults.keyboard[$k]) }
    foreach ($k in $defaults.controller.Keys) { $pad[$k] = $defaults.controller[$k] }
    if (Test-Path -LiteralPath $file) {
        $section = ''
        foreach ($raw in [IO.File]::ReadAllLines($file)) {
            $line = $raw.Trim()
            if (-not $line -or $line.StartsWith(';') -or $line.StartsWith('#')) { continue }
            if ($line.StartsWith('[')) {
                $section = $(if ($line -match '^\[(keyboard|controller)\]') { $Matches[1].ToLower() } else { '' })
                continue
            }
            if ($line -notmatch '^([^=]+)=(.*)$') { continue }
            $name = $Matches[1].Trim().ToLower(); $value = $Matches[2].Trim()
            if ($section -eq 'keyboard' -and $kb.ContainsKey($name)) {
                $tokens = @(Split-Keys $value)
                $known = @($tokens | ForEach-Object { $keyNameCanon[$_.ToLower()] } | Where-Object { $_ })
                if ($known.Count -gt 0 -or $tokens.Count -eq 0) { $kb[$name] = @($known | Select-Object -First 3) }
            }
            if ($section -eq 'controller' -and $pad.ContainsKey($name)) {
                if (-not $value -or $value -ieq 'None') { $pad[$name] = 'None'; continue }
                $match = $padButtons | Where-Object { $_ -ieq $value } | Select-Object -First 1
                if ($match) { $pad[$name] = $match }
            }
        }
    }
    return @{ keyboard = $kb; controller = $pad }
}

$bindings = Read-Bindings $Path
$script:loadedStamp = $(if (Test-Path -LiteralPath $Path) { (Get-Item -LiteralPath $Path).LastWriteTimeUtc } else { $null })

# Layout: rows scroll inside a panel sized to the screen.
$rowH = 28
$colKey = 220; $keyW = 100; $keyStep = 108; $colPad = 548; $padW = 104
$panelW = 680
$work = [System.Windows.Forms.Screen]::PrimaryScreen.WorkingArea.Height
$panelH = [Math]::Max(240, [Math]::Min($actions.Count * $rowH + 12, $work - 190))

$form = New-Object System.Windows.Forms.Form
$form.Text = 'Tokyo Jungle Controls'
$form.StartPosition = 'CenterScreen'
$form.FormBorderStyle = 'FixedDialog'
$form.MaximizeBox = $false
$form.KeyPreview = $true
$form.Font = New-Object System.Drawing.Font('Segoe UI', 9)
$form.ClientSize = New-Object System.Drawing.Size($panelW, (34 + $panelH + 84))

$panel = New-Object System.Windows.Forms.Panel
$panel.Location = New-Object System.Drawing.Point(0, 34)
$panel.Size = New-Object System.Drawing.Size($panelW, $panelH)
$panel.AutoScroll = $true
$form.Controls.Add($panel)

$bold = New-Object System.Drawing.Font('Segoe UI', 9, [System.Drawing.FontStyle]::Bold)
foreach ($h in @(@('Action', 12), @('Key 1', $colKey), @('Key 2', ($colKey + $keyStep)), @('Key 3', ($colKey + 2 * $keyStep)), @('Controller', $colPad))) {
    $l = New-Object System.Windows.Forms.Label
    $l.Text = $h[0]; $l.Location = New-Object System.Drawing.Point($h[1], 10); $l.AutoSize = $true; $l.Font = $bold
    $form.Controls.Add($l)
}

$keyBoxes = @{}    # action -> array of 3 labels
$padBoxes = @{}    # action -> combobox
$script:capture = $null
$script:ignoreVk = @{}
$script:swallowUp = $false
$script:dirty = $false

function Set-KeyBox($box, [string]$name) {
    $box.Tag = $name
    $box.Text = $(if ($name) { $name } else { '-' })
    $box.BackColor = [System.Drawing.Color]::White
}

# While capturing, buttons and dropdowns are disabled so a key or click used as
# the binding cannot also press Save/Close or change a dropdown.
function Set-Capturing([bool]$on) {
    foreach ($b in @($save, $reset, $close)) { if ($b) { $b.Enabled = -not $on } }
    foreach ($c in $padBoxes.Values) { $c.Enabled = -not $on }
}

function End-Capture([string]$newName, [bool]$keep) {
    $box = $script:capture
    $script:capture = $null
    if ($keep) { Set-KeyBox $box $box.Tag } else { Set-KeyBox $box $newName; $script:dirty = $true }
    Set-Capturing $false
}

$y = 6
foreach ($a in $actions) {
    $name = $a[0]
    $label = New-Object System.Windows.Forms.Label
    $label.Text = $a[1]; $label.Location = New-Object System.Drawing.Point(12, ($y + 4)); $label.Size = New-Object System.Drawing.Size(200, 20)
    $panel.Controls.Add($label)
    $keys = @($bindings.keyboard[$name])
    $boxes = @()
    for ($i = 0; $i -lt 3; $i++) {
        $box = New-Object System.Windows.Forms.Label
        $box.Location = New-Object System.Drawing.Point(($colKey + $keyStep * $i), $y)
        $box.Size = New-Object System.Drawing.Size($keyW, 24)
        $box.BorderStyle = 'FixedSingle'
        $box.TextAlign = 'MiddleCenter'
        $box.Cursor = [System.Windows.Forms.Cursors]::Hand
        Set-KeyBox $box $(if ($i -lt $keys.Count) { $keys[$i] } else { '' })
        $box.Add_MouseUp({
            param($sender, $e)
            if ($script:swallowUp) { $script:swallowUp = $false; return }   # release of a bound mouse button
            if ($script:capture) { return }
            if ($e.Button -eq [System.Windows.Forms.MouseButtons]::Right) {
                if ($sender.Tag) { Set-KeyBox $sender ''; $script:dirty = $true }
                return
            }
            if ($e.Button -ne [System.Windows.Forms.MouseButtons]::Left) { return }
            $script:capture = $sender
            $sender.Text = 'Press a key...'
            $sender.BackColor = [System.Drawing.Color]::LightYellow
            $form.ActiveControl = $null
            Set-Capturing $true
            # Ignore keys/buttons already held until they are released.
            $script:ignoreVk = @{}
            foreach ($vk in $vkOrder) { if ([TjControls.Keys]::GetAsyncKeyState($vk) -band 0x8000) { $script:ignoreVk[$vk] = $true } }
            $script:captureStart = [DateTime]::UtcNow
        })
        $panel.Controls.Add($box)
        $boxes += $box
    }
    $keyBoxes[$name] = $boxes
    if ($a[2]) {
        $combo = New-Object System.Windows.Forms.ComboBox
        $combo.DropDownStyle = 'DropDownList'
        $combo.Location = New-Object System.Drawing.Point($colPad, $y)
        $combo.Size = New-Object System.Drawing.Size($padW, 24)
        [void]$combo.Items.AddRange($padButtons)
        $combo.SelectedItem = $bindings.controller[$name]
        if (-not $combo.SelectedItem) { $combo.SelectedItem = 'None' }
        $combo.Add_SelectedIndexChanged({ $script:dirty = $true })
        # The mouse wheel scrolls the list instead of changing a closed dropdown.
        $combo.Add_MouseWheel({
            param($sender, $e)
            if ($sender.DroppedDown) { return }
            ([System.Windows.Forms.HandledMouseEventArgs]$e).Handled = $true
            $top = [Math]::Max(0, -$panel.AutoScrollPosition.Y - $e.Delta)
            $panel.AutoScrollPosition = New-Object System.Drawing.Point(0, $top)
        })
        $panel.Controls.Add($combo)
        $padBoxes[$name] = $combo
    } else {
        $stick = New-Object System.Windows.Forms.Label
        $stick.Text = $(if ($name -like 'l*') { 'left stick' } else { 'right stick' })
        $stick.ForeColor = [System.Drawing.Color]::Gray
        $stick.Location = New-Object System.Drawing.Point($colPad, ($y + 4)); $stick.AutoSize = $true
        $panel.Controls.Add($stick)
    }
    $y += $rowH
}
$script:dirty = $false

# Poll the keyboard/mouse while capturing (works for every key, incl. arrows, Tab, Alt).
$timer = New-Object System.Windows.Forms.Timer
$timer.Interval = 25
$timer.Add_Tick({
    if ($script:swallowUp -and -not ($mouseVk | Where-Object { [TjControls.Keys]::GetAsyncKeyState($_) -band 0x8000 })) {
        # The button that was bound is released; its MouseUp (if any) was delivered before this tick.
        $script:swallowUp = $false
    }
    if (-not $script:capture) { return }
    foreach ($vk in @($script:ignoreVk.Keys)) {
        if (-not ([TjControls.Keys]::GetAsyncKeyState($vk) -band 0x8000)) { $script:ignoreVk.Remove($vk) }
    }
    if (([TjControls.Keys]::GetAsyncKeyState(0x1B) -band 0x8000) -and -not $script:ignoreVk.ContainsKey(0x1B)) {
        End-Capture '' $true          # Escape cancels and keeps the old value
        return
    }
    $hit = $null
    foreach ($vk in $vkOrder) {
        if ($vk -eq 0x1B -or $script:ignoreVk.ContainsKey($vk) -or $genericVk -contains $vk) { continue }
        if ([TjControls.Keys]::GetAsyncKeyState($vk) -band 0x8000) { $hit = $vk; break }
    }
    if ($null -eq $hit) {
        if (([DateTime]::UtcNow - $script:captureStart).TotalSeconds -gt 8) { End-Capture '' $true }
        return
    }
    if ($mouseVk -contains $hit) { $script:swallowUp = $true }
    End-Capture $vkNames[$hit] $false
})
$timer.Start()

# Keys pressed while capturing must not move focus or press anything.
$form.Add_PreviewKeyDown({ param($s, $e) if ($script:capture) { $e.IsInputKey = $true } })
$form.Add_KeyDown({ param($s, $e) if ($script:capture) { $e.Handled = $true; $e.SuppressKeyPress = $true } })
$form.Add_MouseUp({ $script:swallowUp = $false })

$hint = New-Object System.Windows.Forms.Label
$hint.Text = 'Click a box, then press a key or mouse button.  Right-click a box to clear it.  Esc cancels.'
$hint.Location = New-Object System.Drawing.Point(12, (34 + $panelH + 8)); $hint.Size = New-Object System.Drawing.Size(660, 20)
$hint.ForeColor = [System.Drawing.Color]::DimGray
$form.Controls.Add($hint)

function Save-Bindings {
    # The game also saves this file (F1 screen). Do not silently overwrite newer changes.
    $now = $(if (Test-Path -LiteralPath $Path) { (Get-Item -LiteralPath $Path).LastWriteTimeUtc } else { $null })
    if ($now -and $script:loadedStamp -and $now -ne $script:loadedStamp) {
        $answer = [System.Windows.Forms.MessageBox]::Show($form, 'The controls were changed in the game since this window opened. Overwrite them with the settings shown here?', 'Tokyo Jungle Controls', [System.Windows.Forms.MessageBoxButtons]::YesNo)
        if ($answer -ne [System.Windows.Forms.DialogResult]::Yes) { throw 'Not saved. Close and reopen this window to see the current controls.' }
    }
    $lines = New-Object System.Collections.Generic.List[string]
    $lines.Add('; Tokyo Jungle PC controls. Edit with "Tokyo Jungle Controls" or by hand.')
    $lines.Add('; [keyboard] action = Key, Key, Key  (empty = unbound)   [controller] action = Button (None = unbound)')
    $lines.Add('; Changes apply while the game is running (within a second).')
    $lines.Add('')
    $lines.Add('[keyboard]')
    foreach ($a in $actions) {
        $names = @($keyBoxes[$a[0]] | ForEach-Object { $_.Tag } | Where-Object { $_ })
        $lines.Add(('{0} = {1}' -f $a[0], ($names -join ', ')))
    }
    $lines.Add('')
    $lines.Add('[controller]')
    foreach ($a in $actions) {
        if ($padBoxes.ContainsKey($a[0])) { $lines.Add(('{0} = {1}' -f $a[0], $padBoxes[$a[0]].SelectedItem)) }
    }
    # Write a temp file, then replace, so the game never reads a half-written file.
    # The game may be reading the old file for an instant; retry briefly.
    $tmp = $Path + '.tmp'
    [IO.File]::WriteAllLines($tmp, $lines)
    for ($try = 0; ; $try++) {
        try { Move-Item -LiteralPath $tmp -Destination $Path -Force -ErrorAction Stop; break }
        catch { if ($try -ge 10) { throw }; Start-Sleep -Milliseconds 100 }
    }
    $script:dirty = $false
    $script:loadedStamp = (Get-Item -LiteralPath $Path).LastWriteTimeUtc
}

$buttonsY = 34 + $panelH + 40
$save = New-Object System.Windows.Forms.Button
$save.Text = 'Save'; $save.Location = New-Object System.Drawing.Point(392, $buttonsY); $save.Size = New-Object System.Drawing.Size(90, 32)
$save.Add_Click({
    try {
        Save-Bindings
        [System.Windows.Forms.MessageBox]::Show($form, 'Saved. The game uses the new controls within a second.', 'Tokyo Jungle Controls') | Out-Null
    } catch {
        [System.Windows.Forms.MessageBox]::Show($form, 'Could not save: ' + $_.Exception.Message, 'Tokyo Jungle Controls') | Out-Null
    }
})
$form.Controls.Add($save)

$reset = New-Object System.Windows.Forms.Button
$reset.Text = 'Defaults'; $reset.Location = New-Object System.Drawing.Point(488, $buttonsY); $reset.Size = New-Object System.Drawing.Size(90, 32)
$reset.Add_Click({
    foreach ($a in $actions) {
        $keys = @(Split-Keys $defaults.keyboard[$a[0]])
        for ($i = 0; $i -lt 3; $i++) { Set-KeyBox $keyBoxes[$a[0]][$i] $(if ($i -lt $keys.Count) { $keys[$i] } else { '' }) }
        if ($padBoxes.ContainsKey($a[0])) { $padBoxes[$a[0]].SelectedItem = $defaults.controller[$a[0]] }
    }
    $script:dirty = $true
})
$form.Controls.Add($reset)

$close = New-Object System.Windows.Forms.Button
$close.Text = 'Close'; $close.Location = New-Object System.Drawing.Point(584, $buttonsY); $close.Size = New-Object System.Drawing.Size(86, 32)
$close.Add_Click({ $form.Close() })
$form.Controls.Add($close)

$form.Add_FormClosing({
    param($s, $e)
    if (-not $script:dirty) { return }
    $answer = [System.Windows.Forms.MessageBox]::Show($form, 'Save your changes before closing?', 'Tokyo Jungle Controls',
        [System.Windows.Forms.MessageBoxButtons]::YesNoCancel)
    if ($answer -eq [System.Windows.Forms.DialogResult]::Cancel) { $e.Cancel = $true; return }
    if ($answer -eq [System.Windows.Forms.DialogResult]::Yes) {
        try { Save-Bindings } catch {
            [System.Windows.Forms.MessageBox]::Show($form, 'Could not save: ' + $_.Exception.Message, 'Tokyo Jungle Controls') | Out-Null
            $e.Cancel = $true
        }
    }
})

[void]$form.ShowDialog()
$timer.Stop()
