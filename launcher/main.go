package main

import (
	"bufio"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"time"
)

const (
	appPort = "8000"
	appURL  = "http://localhost:" + appPort
)

func main() {
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

// ── Environment ───────────────────────────────────────────────────────────────

type env struct {
	rootDir   string
	venvDir   string
	pythonBin string
	pipBin    string
}

// detect locates the ai-autoedit root relative to the launcher binary.
// Dev mode:  binary lives in launcher/, root is parent directory.
// Dist mode: binary lives next to webapp/, root is same directory.
func detect() (*env, error) {
	exe, err := os.Executable()
	if err != nil {
		return nil, err
	}
	exeDir := filepath.Dir(exe)

	// Walk up the directory tree until we find webapp/ (handles .app bundles
	// where the binary is buried in Contents/MacOS/ several levels deep).
	rootDir := ""
	dir := exeDir
	for {
		if _, err := os.Stat(filepath.Join(dir, "webapp")); err == nil {
			rootDir = dir
			break
		}
		parent := filepath.Dir(dir)
		if parent == dir {
			break // filesystem root reached
		}
		dir = parent
	}
	if rootDir == "" {
		return nil, fmt.Errorf("webapp/ not found near %s", exeDir)
	}

	venvDir := filepath.Join(rootDir, "venv")
	pythonBin, pipBin := venvPaths(venvDir)

	return &env{
		rootDir:   rootDir,
		venvDir:   venvDir,
		pythonBin: pythonBin,
		pipBin:    pipBin,
	}, nil
}

func venvPaths(venvDir string) (python, pip string) {
	if runtime.GOOS == "windows" {
		return filepath.Join(venvDir, "Scripts", "python.exe"),
			filepath.Join(venvDir, "Scripts", "pip.exe")
	}
	return filepath.Join(venvDir, "bin", "python3"),
		filepath.Join(venvDir, "bin", "pip3")
}

func (e *env) needsSetup() bool {
	_, err := os.Stat(e.pythonBin)
	return os.IsNotExist(err)
}

// ── Setup ─────────────────────────────────────────────────────────────────────

func (e *env) setup() error {
	// 1. Find system Python 3.11+
	sysPython, err := findPython()
	if err != nil {
		return fmt.Errorf("%w\n\nInstall Python 3.11+ from https://www.python.org/downloads/\nOn macOS you can also run: brew install python@3.12", err)
	}
	step("Python: " + sysPython)

	// 2. Create virtual environment
	step("Creating virtual environment...")
	if err := run(sysPython, "-m", "venv", e.venvDir); err != nil {
		return fmt.Errorf("venv creation failed: %w", err)
	}

	// 3. Install PyTorch CPU (no CUDA on macOS/Windows without NVIDIA)
	step("Installing PyTorch (CPU build — takes 5-10 min on first install)...")
	if err := run(e.pipBin, "install",
		"--extra-index-url", "https://download.pytorch.org/whl/cpu",
		"torch", "torchvision",
	); err != nil {
		return fmt.Errorf("PyTorch install failed: %w", err)
	}

	// 4. Install remaining dependencies (torch already satisfied, skipped by pip)
	step("Installing remaining dependencies...")
	reqFile := filepath.Join(e.rootDir, "requirements-nogpu.txt")
	if _, err := os.Stat(reqFile); os.IsNotExist(err) {
		reqFile = filepath.Join(e.rootDir, "requirements.txt")
	}
	if err := run(e.pipBin, "install", "-r", reqFile); err != nil {
		return fmt.Errorf("dependency install failed: %w", err)
	}

	// 5. Verify ffmpeg
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

// ── Server ────────────────────────────────────────────────────────────────────

func (e *env) startServer() (*exec.Cmd, error) {
	cmd := exec.Command(e.pythonBin,
		"-m", "uvicorn", "webapp.server:app",
		"--host", "0.0.0.0",
		"--port", appPort,
	)
	cmd.Dir = e.rootDir
	cmd.Stdout = os.Stdout
	cmd.Stderr = os.Stderr

	// Ensure src/ is on PYTHONPATH so pipeline modules are importable
	srcPath := filepath.Join(e.rootDir, "src")
	pythonPath := e.rootDir + string(os.PathListSeparator) + srcPath
	if existing := os.Getenv("PYTHONPATH"); existing != "" {
		pythonPath += string(os.PathListSeparator) + existing
	}
	cmd.Env = append(os.Environ(), "PYTHONPATH="+pythonPath)

	if err := cmd.Start(); err != nil {
		return nil, err
	}
	return cmd, nil
}

// ── Helpers ───────────────────────────────────────────────────────────────────

func findPython() (string, error) {
	var candidates []string
	if runtime.GOOS == "windows" {
		candidates = []string{"python3.12", "python3.11", "python", "py"}
	} else {
		candidates = []string{"python3.12", "python3.11", "python3"}
	}
	for _, name := range candidates {
		path, err := exec.LookPath(name)
		if err != nil {
			continue
		}
		out, err := exec.Command(path, "-c",
			"import sys; print(sys.version_info >= (3, 11, 0))").Output()
		if err == nil && strings.TrimSpace(string(out)) == "True" {
			return path, nil
		}
	}
	return "", fmt.Errorf("Python 3.11+ not found in PATH")
}

func checkFFmpeg() error {
	_, err := exec.LookPath("ffmpeg")
	return err
}

func run(name string, args ...string) error {
	cmd := exec.Command(name, args...)
	cmd.Stdout = os.Stdout
	cmd.Stderr = os.Stderr
	return cmd.Run()
}

func openBrowser(url string) {
	var cmd *exec.Cmd
	switch runtime.GOOS {
	case "windows":
		cmd = exec.Command("rundll32", "url.dll,FileProtocolHandler", url)
	case "darwin":
		cmd = exec.Command("open", url)
	default:
		cmd = exec.Command("xdg-open", url)
	}
	_ = cmd.Start()
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
