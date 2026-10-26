#!/bin/sh
# Builds DPIdeck.exe and DPIdeck-windows.zip from Linux (or any OS with Go).
# Runs the parity tests first; they must pass.
set -eu
cd "$(dirname "$0")"
# one version for both apps: app.py's VERSION. The updater compares it with the release tag
VERSION=$(sed -n 's/^VERSION = "\([0-9.]*\)".*/\1/p' ../app.py)
[ -n "$VERSION" ] || { echo "No VERSION in ../app.py" >&2; exit 1; }
go test ./core/ ./update/
go run github.com/tc-hib/go-winres@v0.3.3 make --arch amd64
GOOS=windows GOARCH=amd64 CGO_ENABLED=0 go build -trimpath -ldflags "-s -w -H windowsgui -X main.version=$VERSION" -o DPIdeck.exe .
rm -f DPIdeck-windows.zip
python3 - <<'PY'
import zipfile
with zipfile.ZipFile("DPIdeck-windows.zip", "w", zipfile.ZIP_DEFLATED) as z:
    z.write("DPIdeck.exe")
    z.write("THIRD_PARTY_LICENSES.txt")
    z.write("../LICENSE-OpenATK", "LICENSE-OpenATK.txt")
    z.write("../LICENSE", "LICENSE-CC0.txt")
PY
ls -la DPIdeck.exe DPIdeck-windows.zip
