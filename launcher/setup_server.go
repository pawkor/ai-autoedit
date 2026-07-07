package main

import (
	_ "embed"
	"encoding/json"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"sync"
)

//go:embed setup.html
var setupHTML []byte

// screenshotExclude: filenames containing any of these strings are skipped
var screenshotExclude = []string{"grafana", "Grafana"}

type event struct {
	Type string `json:"type"`           // "log" | "done" | "navigate"
	Msg  string `json:"msg,omitempty"`
	Step bool   `json:"step,omitempty"` // blue step header in log
	Warn bool   `json:"warn,omitempty"`
	Err  bool   `json:"err,omitempty"`
	URL  string `json:"url,omitempty"`
}

type setupSrv struct {
	rootDir string
	mu      sync.Mutex
	history []event
	subs    []chan event
}

func newSetupSrv(rootDir string) *setupSrv { return &setupSrv{rootDir: rootDir} }

func (s *setupSrv) emit(e event) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.history = append(s.history, e)
	for _, ch := range s.subs {
		select {
		case ch <- e:
		default:
		}
	}
}

func (s *setupSrv) Log(msg string, isStep bool) {
	s.emit(event{Type: "log", Msg: msg, Step: isStep})
}
func (s *setupSrv) Warn(msg string)     { s.emit(event{Type: "log", Msg: msg, Warn: true}) }
func (s *setupSrv) Done()               { s.emit(event{Type: "done"}) }
func (s *setupSrv) Navigate(u string)   { s.emit(event{Type: "navigate", URL: u}) }

func (s *setupSrv) Start() {
	mux := http.NewServeMux()

	mux.HandleFunc("/", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/html; charset=utf-8")
		w.Write(setupHTML)
	})

	mux.HandleFunc("/api/screenshots", func(w http.ResponseWriter, r *http.Request) {
		imgDir := filepath.Join(s.rootDir, "docs", "img")
		entries, err := os.ReadDir(imgDir)
		var urls []string
		if err == nil {
			for _, e := range entries {
				name := e.Name()
				if e.IsDir() || !isImage(name) {
					continue
				}
				skip := false
				for _, ex := range screenshotExclude {
					if strings.Contains(name, ex) {
						skip = true
						break
					}
				}
				if !skip {
					urls = append(urls, "/img/"+url.PathEscape(name))
				}
			}
		}
		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(urls)
	})

	mux.HandleFunc("/img/", func(w http.ResponseWriter, r *http.Request) {
		name, _ := url.PathUnescape(strings.TrimPrefix(r.URL.Path, "/img/"))
		http.ServeFile(w, r, filepath.Join(s.rootDir, "docs", "img", name))
	})

	mux.HandleFunc("/events", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/event-stream")
		w.Header().Set("Cache-Control", "no-cache")
		w.Header().Set("Connection", "keep-alive")

		ch := make(chan event, 64)
		s.mu.Lock()
		hist := append([]event(nil), s.history...)
		s.subs = append(s.subs, ch)
		s.mu.Unlock()

		send := func(e event) {
			b, _ := json.Marshal(e)
			w.Write([]byte("data: "))
			w.Write(b)
			w.Write([]byte("\n\n"))
			if f, ok := w.(http.Flusher); ok {
				f.Flush()
			}
		}
		for _, e := range hist {
			send(e)
		}

		ctx := r.Context()
		for {
			select {
			case e := <-ch:
				send(e)
			case <-ctx.Done():
				s.mu.Lock()
				for i, c := range s.subs {
					if c == ch {
						s.subs = append(s.subs[:i], s.subs[i+1:]...)
						break
					}
				}
				s.mu.Unlock()
				return
			}
		}
	})

	http.ListenAndServe(":8001", mux)
}

func isImage(name string) bool {
	l := strings.ToLower(name)
	return strings.HasSuffix(l, ".png") || strings.HasSuffix(l, ".jpg") || strings.HasSuffix(l, ".jpeg")
}

func reqFilePath(rootDir string) string {
	p := filepath.Join(rootDir, "requirements-nogpu.txt")
	if _, err := os.Stat(p); os.IsNotExist(err) {
		return filepath.Join(rootDir, "requirements.txt")
	}
	return p
}
