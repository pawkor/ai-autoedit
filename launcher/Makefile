BINARY = ai-autoedit-launcher
DIST   = ../dist

.PHONY: dev build-windows build-darwin-arm64 build-darwin-amd64 build-linux all clean

# Run locally (Linux dev machine)
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

# macOS Apple Silicon
build-darwin-arm64:
	GOOS=darwin GOARCH=arm64 go build -o $(DIST)/$(BINARY)-darwin-arm64 .
	$(MAKE) _bundle-darwin ARCH=arm64
	@echo "Built: $(DIST)/ai-autoedit-arm64.app"

# macOS Intel (legacy)
build-darwin-amd64:
	GOOS=darwin GOARCH=amd64 go build -o $(DIST)/$(BINARY)-darwin-amd64 .
	$(MAKE) _bundle-darwin ARCH=amd64
	@echo "Built: $(DIST)/ai-autoedit-amd64.app"

# Create .app bundle structure
_bundle-darwin:
	rm -rf $(DIST)/ai-autoedit-$(ARCH).app
	mkdir -p $(DIST)/ai-autoedit-$(ARCH).app/Contents/MacOS
	mkdir -p $(DIST)/ai-autoedit-$(ARCH).app/Contents/Resources
	cp $(DIST)/$(BINARY)-darwin-$(ARCH) $(DIST)/ai-autoedit-$(ARCH).app/Contents/MacOS/launcher
	cp assets/Info.plist $(DIST)/ai-autoedit-$(ARCH).app/Contents/
	chmod +x $(DIST)/ai-autoedit-$(ARCH).app/Contents/MacOS/launcher

clean:
	rm -rf $(DIST)/
