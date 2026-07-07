package main

import (
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
)

const (
	appPort = "8000"
	appURL  = "http://localhost:" + appPort
)

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

	// macOS .app bundle: binary is in Contents/MacOS/, source+venv go in Contents/Resources/
	if filepath.Base(exeDir) == "MacOS" {
		resourcesDir := filepath.Join(filepath.Dir(exeDir), "Resources")
		if _, err := os.Stat(filepath.Join(resourcesDir, "webapp")); err == nil {
			return makeEnv(resourcesDir), nil
		}
	}

	// Dev / Windows: walk up until we find webapp/ (handles launcher/ subdir and flat layout)
	dir := exeDir
	for {
		if _, err := os.Stat(filepath.Join(dir, "webapp")); err == nil {
			return makeEnv(dir), nil
		}
		parent := filepath.Dir(dir)
		if parent == dir {
			break
		}
		dir = parent
	}
	return nil, fmt.Errorf("webapp/ not found near %s", exeDir)
}

func makeEnv(rootDir string) *env {
	venvDir := filepath.Join(rootDir, "venv")
	pythonBin, pipBin := venvPaths(venvDir)
	return &env{rootDir: rootDir, venvDir: venvDir, pythonBin: pythonBin, pipBin: pipBin}
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

func (e *env) startServer() (*exec.Cmd, error) {
	cmd := exec.Command(e.pythonBin,
		"-m", "uvicorn", "webapp.server:app",
		"--host", "0.0.0.0",
		"--port", appPort,
	)
	cmd.Dir = e.rootDir
	cmd.Stdout = os.Stdout
	cmd.Stderr = os.Stderr

	srcPath := filepath.Join(e.rootDir, "src")
	pythonPath := e.rootDir + string(os.PathListSeparator) + srcPath
	if existing := os.Getenv("PYTHONPATH"); existing != "" {
		pythonPath += string(os.PathListSeparator) + existing
	}

	// On macOS app launch PATH is minimal — add Homebrew so ffmpeg/tools are found.
	sysPATH := os.Getenv("PATH")
	if runtime.GOOS == "darwin" {
		sysPATH = "/opt/homebrew/bin:/usr/local/bin:" + sysPATH
	}

	cmd.Env = append(os.Environ(),
		"PYTHONPATH="+pythonPath,
		"PATH="+sysPATH,
		"BROWSE_ROOT=/",
	)

	// User data dir: ~/Library/Application Support/ai-autoedit on macOS, etc.
	dataDir := userDataDir()
	os.MkdirAll(filepath.Join(dataDir, "jobs"), 0755)
	cmd.Env = append(cmd.Env, "AI_AUTOEDIT_DATA="+dataDir)

	if err := cmd.Start(); err != nil {
		return nil, err
	}
	return cmd, nil
}

func userDataDir() string {
	switch runtime.GOOS {
	case "darwin":
		home, _ := os.UserHomeDir()
		return filepath.Join(home, "Library", "Application Support", "ai-autoedit")
	case "windows":
		appData := os.Getenv("APPDATA")
		if appData == "" {
			home, _ := os.UserHomeDir()
			appData = filepath.Join(home, "AppData", "Roaming")
		}
		return filepath.Join(appData, "ai-autoedit")
	default:
		home, _ := os.UserHomeDir()
		return filepath.Join(home, ".ai-autoedit")
	}
}

func findPython() (string, error) {
	var names []string
	if runtime.GOOS == "windows" {
		names = []string{"python3.12", "python3.11", "python", "py"}
	} else {
		names = []string{"python3.12", "python3.11", "python3"}
	}

	// Collect candidates: PATH lookup first, then hardcoded macOS locations.
	var candidates []string
	for _, name := range names {
		if p, err := exec.LookPath(name); err == nil {
			candidates = append(candidates, p)
		}
	}
	if runtime.GOOS == "darwin" {
		// python.org framework installer
		for _, ver := range []string{"3.13", "3.12", "3.11"} {
			candidates = append(candidates,
				"/Library/Frameworks/Python.framework/Versions/"+ver+"/bin/python3",
			)
		}
		// Homebrew (Apple Silicon + Intel)
		for _, base := range []string{"/opt/homebrew", "/usr/local"} {
			for _, ver := range []string{"3.13", "3.12", "3.11"} {
				candidates = append(candidates,
					base+"/opt/python@"+ver+"/bin/python3",
					base+"/bin/python"+ver,
				)
			}
			candidates = append(candidates, base+"/bin/python3")
		}
	}

	// Last resort on macOS: ask the login shell — picks up Homebrew/pyenv from .zprofile
	if runtime.GOOS == "darwin" {
		for _, name := range names {
			for _, shell := range []string{"/bin/zsh", "/bin/bash"} {
				out, err := exec.Command(shell, "-l", "-c", "which "+name+" 2>/dev/null").Output()
				if err == nil {
					if p := strings.TrimSpace(string(out)); p != "" {
						candidates = append(candidates, p)
					}
				}
			}
		}
	}

	seen := map[string]bool{}
	for _, p := range candidates {
		if seen[p] {
			continue
		}
		seen[p] = true
		out, err := exec.Command(p, "-c",
			"import sys; print(sys.version_info >= (3, 11, 0))").Output()
		if err == nil && strings.TrimSpace(string(out)) == "True" {
			return p, nil
		}
	}
	return "", fmt.Errorf("Python 3.11+ not found in PATH")
}

// torchPipArgs returns pip install args for PyTorch.
// macOS: standard build includes MPS (Apple Silicon GPU) support.
// Other platforms: CPU-only build to avoid large CUDA download.
func torchPipArgs() []string {
	if runtime.GOOS == "darwin" {
		return []string{"install", "torch", "torchvision"}
	}
	return []string{"install",
		"--extra-index-url", "https://download.pytorch.org/whl/cpu",
		"torch", "torchvision",
	}
}

func checkFFmpeg() error {
	_, err := exec.LookPath("ffmpeg")
	return err
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
