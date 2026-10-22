param(
    [string]$TargetScript = "start_windows.bat",
    [string]$ShortcutName = "TEAMworks360"
)
# Creates a Desktop shortcut to the given launcher script with the app's
# own icon, so there's a proper clickable icon to use. Run automatically by
# each launcher on its own first run - never on its own, and safe to run
# again (just leaves the shortcut as it already was, silently). $PSScriptRoot
# is this script's own folder, so everything resolves correctly no matter
# where the app folder was extracted to.
#
# Shared by four different launchers, each passing its own -TargetScript
# and -ShortcutName so the icons never collide or get confused for
# one another:
#   open_shared_app.bat -> "TEAMworks360"            (everyone)
#   start_windows.bat    -> "TEAMworks360 (Local Copy)" (rare/testing)
#   start_shared_host.bat -> "TEAMworks360 (Host)"    (the one host PC)
#   start_test_shared_host.bat -> "TEAMworks360 (Test Host)" (isolated sandbox)
#
# Uses [Environment]::GetFolderPath('Desktop') rather than the more
# obvious-looking "$env:USERPROFILE\Desktop" - the latter is WRONG on any
# machine where the Desktop folder has been redirected elsewhere, which is
# common in a business environment with IT-managed OneDrive Known Folder
# Move. GetFolderPath asks Windows directly for wherever the Desktop
# actually is, redirected or not.
$logPath = Join-Path $PSScriptRoot ("shortcut_creation_" + ($ShortcutName -replace '[^A-Za-z0-9]', '') + ".log")

try {
    $desktopPath = [Environment]::GetFolderPath("Desktop")
    $shortcutPath = Join-Path $desktopPath ($ShortcutName + ".lnk")

    if (-not (Test-Path $shortcutPath)) {
        $shell = New-Object -ComObject WScript.Shell
        $shortcut = $shell.CreateShortcut($shortcutPath)
        $shortcut.TargetPath = Join-Path $PSScriptRoot $TargetScript
        $shortcut.WorkingDirectory = $PSScriptRoot
        $shortcut.IconLocation = Join-Path $PSScriptRoot "app_icon\icon.ico"
        $shortcut.Save()
        Write-Host "Created a `"$ShortcutName`" icon on your Desktop - use that from now on."
    }

    # Clean up a log from a previous failed attempt now that it's worked.
    if (Test-Path $logPath) { Remove-Item $logPath -ErrorAction SilentlyContinue }
} catch {
    # Never worth blocking the app starting over a shortcut that didn't
    # create - but say so plainly and log the real reason, rather than
    # silently doing nothing with no way to tell what went wrong.
    Write-Host "Could not create a Desktop shortcut automatically - see $(Split-Path $logPath -Leaf) in this folder for details. The app will start normally regardless."
    $_.Exception.Message | Out-File -FilePath $logPath -Encoding utf8 -ErrorAction SilentlyContinue
}
