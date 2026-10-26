# DPIdeck

- `noctalia/` - Noctalia plugin source (catalog.toml + dpideck/); install.sh copies it to ~/.local/share/dpideck/noctalia and registers it
- `python3 -I noctalia/dpideck/battery.py` - test the bar widget's reading; it finds atk.py two folders up, else $DPIDECK_DIR or ~/.local/share/dpideck
- `install.sh` needs sudo (udev rule): give the user `./install.sh` in a plain code block to paste in kitty (the `!` prefix has no TTY, so sudo fails there)
- Public repo: no names, emails, IPs or session links in commits or files
- Releases: bump `VERSION` in app.py in the same commit as the tag (`v2.2` style), run `windows/build.sh` (it takes the version from app.py), and attach `windows/DPIdeck-windows.zip` in the same `gh release create`. A release without the zip marks the update failed on Windows clients; a VERSION/tag mismatch makes auto mode reinstall daily
