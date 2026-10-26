# DPIdeck

- `noctalia/` - Noctalia plugin source (catalog.toml + dpideck/); install.sh copies it to ~/.local/share/dpideck/noctalia and registers it
- `python3 -I noctalia/dpideck/battery.py` - test the bar widget's reading; it finds atk.py two folders up, else $DPIDECK_DIR or ~/.local/share/dpideck
- `install.sh` needs sudo (udev rule): give the user `./install.sh` in a plain code block to paste in kitty (the `!` prefix has no TTY, so sudo fails there)
- Public repo: no names, emails, IPs or session links in commits or files
