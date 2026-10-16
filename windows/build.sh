#!/bin/sh
# Builds MouseSettings.exe and MouseSettings-windows.zip from Linux (or any OS with Go).
# Runs the parity tests first; they must pass.
set -eu
cd "$(dirname "$0")"
go test ./core/
go run github.com/tc-hib/go-winres@v0.3.3 make --arch amd64
GOOS=windows GOARCH=amd64 CGO_ENABLED=0 go build -trimpath -ldflags "-s -w -H windowsgui" -o MouseSettings.exe .
rm -f MouseSettings-windows.zip
python3 - <<'PY'
import zipfile
with zipfile.ZipFile("MouseSettings-windows.zip", "w", zipfile.ZIP_DEFLATED) as z:
    z.write("MouseSettings.exe")
    z.write("THIRD_PARTY_LICENSES.txt")
    z.write("../LICENSE-OpenATK", "LICENSE-OpenATK.txt")
    z.write("../LICENSE", "LICENSE-CC0.txt")
PY
ls -la MouseSettings.exe MouseSettings-windows.zip
