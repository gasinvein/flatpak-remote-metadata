#!/usr/bin/env python3

import argparse
import json
import sys
import re
import typing as t

import gi

gi.require_version("GLib", "2.0")
gi.require_version("Gio", "2.0")
gi.require_version("Flatpak", "1.0")
from gi.repository import GLib
from gi.repository import Gio
from gi.repository import Flatpak


MEATADATA_TYPES = [
    (re.compile(g), re.compile(k), t)
    for g, k, t in [
        (r"Context", r".*", list),
        (r"Extension \S+", r"autodelete", bool),
        (r"Extension \S+", r"no-autodownload", bool),
        (r"Extension \S+", r"subdirectories", bool),
        (r"Extension \S+", r"locale-subset", bool),
        (r"Extension \S+", r"versions", list),
        (r"Extension \S+", r"merge-dirs", list),
        (r"ExtensionOf", r"priority", int),
        (r"(Application|Runtime)", r"required-flatpak", list),
        (r"(Application|Runtime)", r"tags", list),
        (r"Build", r"built-extensions", list),
    ]
]

def get_value(metadata: GLib.KeyFile, group: str, key: str):
    for group_re, key_re, cls in MEATADATA_TYPES:
        if group_re.fullmatch(group) and key_re.fullmatch(key):
            if cls is bool:
                return metadata.get_boolean(group, key)
            if cls is list:
                return metadata.get_string_list(group, key)
            if cls is int:
                return metadata.get_integer(group, key)
    return metadata.get_string(group, key)


def metadata_to_dict(metadata: GLib.KeyFile):
    result: t.Dict[str, t.Any] = {}
    groups, _ = metadata.get_groups()
    for group in groups:
        keys, _ = metadata.get_keys(group)
        for key in keys:
            if group.startswith("Extension "):
                _, extension_id = group.split(maxsplit=1)
                result_parent_group = result.setdefault("Extension", {})
                result_group = result_parent_group.setdefault(extension_id, {})
            else:
                result_group = result.setdefault(group, {})
            result_group[key] = get_value(metadata, group, key)
    return result


def get_apps_metadata(installation: Flatpak.Installation, remote: str):
    apps = []
    for ref in installation.list_remote_refs_sync_full(remote, Flatpak.QueryFlags.NONE):
        if ref.get_kind() != Flatpak.RefKind.APP:
            continue
        if ref.get_arch() != "x86_64":
            continue
        if ref.get_eol() or ref.get_eol_rebase():
            continue
        metadata = GLib.KeyFile()
        metadata.load_from_bytes(ref.get_metadata(), GLib.KeyFileFlags.NONE)
        apps.append(metadata)
    return apps


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("-u", "--url")
    parser.add_argument("repo_name")
    args = parser.parse_args()

    cache_home = Gio.File.new_for_path(GLib.get_user_cache_dir())
    inst_dir = cache_home.get_child("flatpak-remote-metadata").get_child("inst")
    if not inst_dir.query_exists():
        inst_dir.make_directory_with_parents()
    inst = Flatpak.Installation.new_for_path(inst_dir, True)

    try:
        remote = inst.get_remote_by_name(args.repo_name)
    except GLib.Error as err:
        if args.url and err.matches(Flatpak.error_quark(), Flatpak.Error.REMOTE_NOT_FOUND):
            remote = Flatpak.Remote.new(args.repo_name)
            remote.set_url(args.url)
            inst.add_remote(remote, if_needed=True)
        else:
            raise

    metas = [metadata_to_dict(m) for m in get_apps_metadata(inst, remote.get_name())]
    json.dump(metas, sys.stdout, indent=4)


if __name__ == "__main__":
    main()
