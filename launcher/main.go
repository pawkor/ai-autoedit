//go:build !webview

package main

import (
	"bufio"
	"fmt"
	"os"
	"os/exec"
	"runtime"
	"strings"
	"time"
)

func main() {
	// When launched from Finder (.app double-click) there is no terminal window.
	// Detect this and relaunch inside Terminal.app so the user sees output.
	if runtime.GOOS == "darwin" && os.Getenv("TERM") == "" && os.Getenv("TERM_PROGRAM") == "" {
		relaunchInTerminal()
		return
	}

	e, err := detect()
	if err != nil {
		fatal("Cannot find ai-autoedit directory.\n\n%v\n\nMake sure the launcher is inside the ai-autoedit folder.", err)
	}

	step("ai-autoedit launcher")
	step("Root: " + e.rootDir)
	fmt.Println()

	if e.needsSetup() {
		step("=== First-time setup (this runs once) ===")
		if err := e.setup(); err != nil {
			fatal("Setup failed:\n\n%v", err)
		}
		fmt.Println()
		step("=== Setup complete! ===")
		fmt.Println()
	}

	step("Starting server...")
	cmd, err := e.startServer()
	if err != nil {
		fatal("Cannot start server: %v", err)
	}

	// Give uvicorn a moment to bind the port
	time.Sleep(2 * time.Second)
	openBrowser(appURL)

	fmt.Printf("\n  OK  Running at %s\n", appURL)
	fmt.Println("      Press Ctrl+C to stop.")
	fmt.Println()

	cmd.Wait()
}

func (e *env) setup() error {
	sysPython, err := findPython()
	if err != nil {
		return fmt.Errorf("%w\n\nInstall Python 3.11+ from https://www.python.org/downloads/\nOn macOS you can also run: brew install python@3.12", err)
	}
	step("Python: " + sysPython)

	step("Creating virtual environment...")
	if err := run(sysPython, "-m", "venv", e.venvDir); err != nil {
		return fmt.Errorf("venv creation failed: %w", err)
	}

	step("Installing PyTorch (5-10 min on first install)...")
	if err := run(e.pipBin, torchPipArgs()...); err != nil {
		return fmt.Errorf("PyTorch install failed: %w", err)
	}

	step("Installing remaining dependencies...")
	if err := run(e.pipBin, "install", "-r", reqFilePath(e.rootDir)); err != nil {
		return fmt.Errorf("dependency install failed: %w", err)
	}

	step("Checking ffmpeg...")
	if err := checkFFmpeg(); err != nil {
		fmt.Println()
		fmt.Println("  WARNING: ffmpeg not found. Install it before rendering:")
		fmt.Println("     macOS:   brew install ffmpeg")
		fmt.Println("     Windows: https://www.gyan.dev/ffmpeg/builds/ (add to PATH)")
		fmt.Println()
	} else {
		step("ffmpeg OK")
	}

	return nil
}

func run(name string, args ...string) error {
	cmd := exec.Command(name, args...)
	cmd.Stdout = os.Stdout
	cmd.Stderr = os.Stderr
	return cmd.Run()
}

func relaunchInTerminal() {
	exe, err := os.Executable()
	if err != nil {
		return
	}
	escaped := strings.ReplaceAll(exe, `\`, `\\`)
	escaped = strings.ReplaceAll(escaped, `"`, `\"`)
	script := fmt.Sprintf(
		`tell application "Terminal"
			activate
			do script "%s"
		end tell`, escaped)
	exec.Command("osascript", "-e", script).Start()
}

func step(msg string) {
	fmt.Println("[ai-autoedit]", msg)
}

func fatal(format string, args ...any) {
	fmt.Fprintln(os.Stderr)
	fmt.Fprintf(os.Stderr, "ERROR: "+format+"\n", args...)
	fmt.Fprintln(os.Stderr)
	if runtime.GOOS == "windows" {
		fmt.Println("Press Enter to close...")
		bufio.NewReader(os.Stdin).ReadString('\n')
	}
	os.Exit(1)
}
