package ui

import (
	"os/exec"
	"runtime"
)

// openInBrowser opens url with the platform's default handler. Best-effort: it
// returns nil for an empty url (nothing to open) and never blocks. Previously
// every call site hardcoded `xdg-open` (Linux-only) and discarded the error, so
// `o` silently did nothing on macOS/WSL/headless SSH.
func openInBrowser(url string) {
	if url == "" {
		return
	}
	var cmd *exec.Cmd
	switch runtime.GOOS {
	case "darwin":
		cmd = exec.Command("open", url)
	case "windows":
		cmd = exec.Command("rundll32", "url.dll,FileProtocolHandler", url)
	default:
		cmd = exec.Command("xdg-open", url)
	}
	_ = cmd.Start()
}
