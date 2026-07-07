//go:build webview

package main

import (
	"fmt"
	"net/http"
	"os"
	"os/exec"
	"strings"
	"time"

	webview "github.com/webview/webview_go"
)

func main() {
	e, err := detect()
	if err != nil {
		showError(fmt.Sprintf("Cannot find ai-autoedit directory.\n\n%v\n\nMake sure ai-autoedit.app is in the ai-autoedit folder.", err))
		return
	}

	srv := newSetupSrv(e.rootDir)
	go srv.Start()
	time.Sleep(100 * time.Millisecond) // let HTTP server bind

	w := webview.New(false)
	defer w.Destroy()
	w.SetTitle("ai-autoedit")
	w.SetSize(1440, 900, webview.HintNone)

	navigate := func(u string) {
		w.Dispatch(func() { w.Navigate(u) })
	}

	var serverCmd *exec.Cmd

	go func() {
		if e.needsSetup() {
			srv.Log("=== First-time setup ===", true)
			if err := e.setupWithSrv(srv); err != nil {
				srv.Warn("ERROR: " + err.Error())
				return
			}
			srv.Done()
		}

		srv.Log("Starting server…", true)
		cmd, err := e.startServer()
		if err != nil {
			srv.Warn("ERROR: cannot start server: " + err.Error())
			return
		}
		serverCmd = cmd

		srv.Log("Waiting for server…", false)
		for i := 0; i < 60; i++ {
			time.Sleep(500 * time.Millisecond)
			resp, err := http.Get(appURL) //nolint:noctx
			if err == nil {
				resp.Body.Close()
				break
			}
		}
		srv.Navigate(appURL)
		navigate(appURL)
	}()

	// Expose native folder picker to JS — triggers macOS TCC permission dialog.
	w.Bind("pickFolder", func() string {
		out, err := exec.Command("osascript", "-e",
			`POSIX path of (choose folder with prompt "Select video folder")`).Output()
		if err != nil {
			return ""
		}
		return strings.TrimRight(string(out), "/\n")
	})

	// Expose Go-side directory listing so JS doesn't need Python for browsing.
	// This runs in the .app process, which has proper macOS TCC context.
	w.Bind("aeBrowse", func(path string) string { return GoBrowse(path) })
	w.Bind("aeSubdirs", func(path string) string { return GoSubdirs(path) })

	// WKWebView lacks NSApp Edit menu — polyfill cmd+c/v/x/a/z via execCommand.
	w.Init(`(function(){
		document.addEventListener('keydown', function(e) {
			if (!e.metaKey) return;
			var map = {c:'copy', x:'cut', v:'paste', a:'selectAll', z: e.shiftKey?'redo':'undo'};
			var cmd = map[e.key];
			if (cmd) { document.execCommand(cmd); e.preventDefault(); }
		}, true);
	})();`)

	w.Navigate("http://localhost:8001/")
	w.Run()

	if serverCmd != nil && serverCmd.Process != nil {
		serverCmd.Process.Kill()
	}
}

func showError(msg string) {
	w := webview.New(false)
	defer w.Destroy()
	w.SetTitle("ai-autoedit — Error")
	w.SetSize(520, 200, webview.HintFixed)
	w.SetHtml(fmt.Sprintf(`<body style="font-family:-apple-system;background:#0d0d0d;color:#ef4444;
		padding:32px;font-size:14px;line-height:1.6">%s</body>`, msg))
	w.Run()
}

// setupWithSrv runs setup streaming progress to the GUI server.
func (e *env) setupWithSrv(srv *setupSrv) error {
	sysPython, err := findPython()
	if err != nil {
		return fmt.Errorf("%w\n\nInstall Python 3.11+ from https://www.python.org/downloads/", err)
	}
	srv.Log("Python: "+sysPython, false)

	srv.Log("Creating virtual environment…", true)
	if err := runToSrv(srv, sysPython, "-m", "venv", e.venvDir); err != nil {
		return fmt.Errorf("venv creation failed: %w", err)
	}

	srv.Log("Installing PyTorch (5–10 min)…", true)
	if err := runToSrv(srv, e.pipBin, torchPipArgs()...); err != nil {
		return fmt.Errorf("PyTorch install failed: %w", err)
	}

	srv.Log("Installing dependencies…", true)
	reqFile := reqFilePath(e.rootDir)
	if err := runToSrv(srv, e.pipBin, "install", "-r", reqFile); err != nil {
		return fmt.Errorf("dependency install failed: %w", err)
	}

	srv.Log("Checking ffmpeg…", true)
	if err := checkFFmpeg(); err != nil {
		srv.Warn("ffmpeg not found — install before rendering: brew install ffmpeg")
	} else {
		srv.Log("ffmpeg OK", false)
	}

	return nil
}

// runToSrv runs a command streaming stdout+stderr to the setup server log.
func runToSrv(srv *setupSrv, name string, args ...string) error {
	cmd := exec.Command(name, args...)
	cmd.Stdout = &srvWriter{srv: srv}
	cmd.Stderr = &srvWriter{srv: srv}
	return cmd.Run()
}

type srvWriter struct{ srv *setupSrv }

func (w *srvWriter) Write(p []byte) (int, error) {
	w.srv.Log(string(p), false)
	os.Stderr.Write(p) // fallback record
	return len(p), nil
}
