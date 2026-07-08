BINARY = ai-autoedit-launcher
DIST   = ../dist

.PHONY: dev build-windows build-darwin-arm64 build-darwin-amd64 build-linux deps-darwin macos macos-dev all clean

# Fetch webview dependency — run once on macOS before building darwin targets
deps-darwin:
	go get github.com/webview/webview_go
	go mod tidy

# Run locally (Linux dev machine — no webview tag)
dev:
	go run .

# Build for all platforms
all: build-windows build-darwin-arm64 build-linux

build-linux:
	GOOS=linux GOARCH=amd64 go build -o $(DIST)/$(BINARY)-linux-amd64 .
	@echo "Built: $(DIST)/$(BINARY)-linux-amd64"

# Windows: shows console window so user sees setup progress
build-windows:
	GOOS=windows GOARCH=amd64 go build -o $(DIST)/$(BINARY)-windows-amd64.exe .
	@echo "Built: $(DIST)/$(BINARY)-windows-amd64.exe"

# One-shot: fetch deps + build + bundle for Apple Silicon (run this on Mac)
macos: deps-darwin build-darwin-arm64

# Dev rebuild: updates binary + source code, preserves config/users/tokens/jobs/venv
macos-dev: deps-darwin
	GOOS=darwin GOARCH=arm64 go build -tags webview -o $(DIST)/$(BINARY)-darwin-arm64 .
	cp $(DIST)/$(BINARY)-darwin-arm64 $(DIST)/ai-autoedit-arm64.app/Contents/MacOS/launcher
	chmod +x $(DIST)/ai-autoedit-arm64.app/Contents/MacOS/launcher
	cp -r ../webapp ../src $(DIST)/ai-autoedit-arm64.app/Contents/Resources/
	cp ../requirements.txt ../requirements-nogpu.txt $(DIST)/ai-autoedit-arm64.app/Contents/Resources/
	mkdir -p $(DIST)/ai-autoedit-arm64.app/Contents/Resources/docs/img
	cp -r ../docs/img/. $(DIST)/ai-autoedit-arm64.app/Contents/Resources/docs/img/ 2>/dev/null || true
	@echo "Updated: $(DIST)/ai-autoedit-arm64.app (config/tokens/venv preserved)"

# macOS Apple Silicon
build-darwin-arm64:
	GOOS=darwin GOARCH=arm64 go build -tags webview -o $(DIST)/$(BINARY)-darwin-arm64 .
	$(MAKE) _bundle-darwin ARCH=arm64
	@echo "Built: $(DIST)/ai-autoedit-arm64.app"

# macOS Intel (legacy)
build-darwin-amd64:
	GOOS=darwin GOARCH=amd64 go build -tags webview -o $(DIST)/$(BINARY)-darwin-amd64 .
	$(MAKE) _bundle-darwin ARCH=amd64
	@echo "Built: $(DIST)/ai-autoedit-amd64.app"

# Create self-contained .app bundle (source + venv slot in Contents/Resources/)
_bundle-darwin:
	rm -rf $(DIST)/ai-autoedit-$(ARCH).app
	mkdir -p $(DIST)/ai-autoedit-$(ARCH).app/Contents/MacOS
	mkdir -p $(DIST)/ai-autoedit-$(ARCH).app/Contents/Resources
	# Launcher binary
	cp $(DIST)/$(BINARY)-darwin-$(ARCH) $(DIST)/ai-autoedit-$(ARCH).app/Contents/MacOS/launcher
	cp assets/Info.plist $(DIST)/ai-autoedit-$(ARCH).app/Contents/
	chmod +x $(DIST)/ai-autoedit-$(ARCH).app/Contents/MacOS/launcher
	# App source code
	cp -r ../webapp ../src $(DIST)/ai-autoedit-$(ARCH).app/Contents/Resources/
	cp ../requirements.txt ../requirements-nogpu.txt $(DIST)/ai-autoedit-$(ARCH).app/Contents/Resources/
	# Screenshots for setup slideshow
	mkdir -p $(DIST)/ai-autoedit-$(ARCH).app/Contents/Resources/docs/img
	cp -r ../docs/img/. $(DIST)/ai-autoedit-$(ARCH).app/Contents/Resources/docs/img/ 2>/dev/null || true
	# Remove sensitive / runtime files that must not ship
	rm -rf  "$(DIST)/ai-autoedit-$(ARCH).app/Contents/Resources/webapp/jobs"
	rm -f   "$(DIST)/ai-autoedit-$(ARCH).app/Contents/Resources/webapp/config.ini"
	rm -f   "$(DIST)/ai-autoedit-$(ARCH).app/Contents/Resources/webapp/users.json"
	rm -f   "$(DIST)/ai-autoedit-$(ARCH).app/Contents/Resources/webapp/ig_token.json"
	rm -f   "$(DIST)/ai-autoedit-$(ARCH).app/Contents/Resources/webapp/youtube_token.json"
	rm -f   "$(DIST)/ai-autoedit-$(ARCH).app/Contents/Resources/webapp/youtube_flow.json"
	rm -f   "$(DIST)/ai-autoedit-$(ARCH).app/Contents/Resources/webapp/youtube_client_secrets.json"
	# Recreate empty jobs dir (server expects it to exist)
	mkdir -p "$(DIST)/ai-autoedit-$(ARCH).app/Contents/Resources/webapp/jobs"

clean:
	rm -rf $(DIST)/
