package main

import (
	"encoding/json"
	"os"
	"path/filepath"
	"sort"
	"strings"
)

type browseResult struct {
	Path    string        `json:"path"`
	Parent  *string       `json:"parent"`
	Entries []browseEntry `json:"entries"`
}

type browseEntry struct {
	Name         string `json:"name"`
	Path         string `json:"path"`
	IsDir        bool   `json:"is_dir"`
	HasMP4       bool   `json:"has_mp4"`
	HasAutoframe bool   `json:"has_autoframe"`
}

// GoBrowse lists a directory for the JS file browser.
// path="" starts at filesystem root. Returns JSON string.
func GoBrowse(path string) string {
	var root string
	if path == "" {
		root = "/"
	} else {
		root = filepath.Clean(path)
	}

	result := browseResult{Path: root}

	if root != "/" {
		p := filepath.Dir(root)
		result.Parent = &p
	}

	entries, err := os.ReadDir(root)
	if err != nil {
		return marshalJSON(result)
	}

	for _, e := range entries {
		name := e.Name()
		if strings.HasPrefix(name, ".") || !e.IsDir() {
			continue
		}
		full := filepath.Join(root, name)
		be := browseEntry{Name: name, Path: full, IsDir: true}
		if kids, err := os.ReadDir(full); err == nil {
			for _, k := range kids {
				kn := strings.ToLower(k.Name())
				if strings.HasSuffix(kn, ".mp4") || strings.HasSuffix(kn, ".mov") {
					be.HasMP4 = true
				}
				if k.Name() == "_autoframe" {
					be.HasAutoframe = true
				}
			}
		}
		result.Entries = append(result.Entries, be)
	}

	sort.Slice(result.Entries, func(i, j int) bool {
		return result.Entries[i].Name < result.Entries[j].Name
	})

	return marshalJSON(result)
}

// GoSubdirs returns sorted subdirectory names (excludes hidden and _autoframe).
func GoSubdirs(path string) string {
	entries, err := os.ReadDir(filepath.Clean(path))
	if err != nil {
		return "[]"
	}
	var names []string
	for _, e := range entries {
		n := e.Name()
		if !e.IsDir() || strings.HasPrefix(n, ".") || n == "_autoframe" {
			continue
		}
		names = append(names, n)
	}
	sort.Strings(names)
	b, _ := json.Marshal(names)
	return string(b)
}

func marshalJSON(v any) string {
	b, _ := json.Marshal(v)
	return string(b)
}
