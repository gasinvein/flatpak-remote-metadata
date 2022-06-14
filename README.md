# Flatpak remote metadata fetcher

This script grabs metdata and embedded manifests from a flatpak remote and prints combined JSON to stdout.

The JSON is a list contaning objects with `ref`, `metadata` and `manifest` properties.
* `metadata` contains `GLib.KeyFile` flatpak metadata converted to JSON
* `meanifest` is the canonicalized manifest that flatpak-builder embeds into app bundles (seen as `/app/manifest.json` inside the sandbox).
  Note that not all flatpak refs will contain manifests (i.e. only those built with flatpak-builder).
```json
{
  "ref": "app/com.example.App/x86_64/stable",
  "metadata": {
    "Application": {
      "name": "com.example.App",
      "runtime": "org.freedesktop.Platform/x86_64/21.08",
      "sdk": "org.freedesktop.Sdk/x86_64/21.08",
      "command": "the-app"
     },
    "Context": {
      "shared": [ "network", "ipc" ],
      "sockets": [ "x11", "wayland", "pulseaudio" ],
      "devices": [ "dri" ],
      "filesystems": [ "host" ]
    },
    "Session Bus Policy": {
      "ca.desrt.dconf": "talk"
    },
    ...
  },
  "manifest": {
    "id": "com.example.App",
    "runtime": "org.freedesktop.Platform",
    "runtime-version": "21.08",
    "command": "the-app",
    ...
  }
}
```

Basic usage:

```bash
./flatpak-remote-metadata.py -u https://dl.flathub.org/repo flathub > flathub.json
```
