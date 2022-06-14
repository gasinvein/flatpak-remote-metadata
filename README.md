# Flatpak remote metadata fetcher

This script grabs metdata and embedded manifests from a flatpak remote and prints combined JSON to stdout.

Basic usage:

```bash
./flatpak-remote-metadata.py -u https://dl.flathub.org/repo flathub > flathub.json
```
