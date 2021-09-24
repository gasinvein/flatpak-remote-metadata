#!/usr/bin/env python3

import argparse
import logging
import json
import sys
import re
import typing as t

import gi

gi.require_version("GLib", "2.0")
gi.require_version("Gio", "2.0")
gi.require_version("Flatpak", "1.0")
gi.require_version("OSTree", "1.0")
from gi.repository import GLib
from gi.repository import Gio
from gi.repository import Flatpak
from gi.repository import OSTree


PROGRAM_NAME = "flatpak-remote-metadata"

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

log = logging.getLogger(PROGRAM_NAME)

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


def load_ostree_file(ref_root: OSTree.RepoFile, path: str) -> GLib.Bytes:
    repo_file = ref_root.resolve_relative_path(path)
    file_size = repo_file.query_info(Gio.FILE_ATTRIBUTE_STANDARD_SIZE,
                                     Gio.FileQueryInfoFlags.NONE).get_size()
    stream = repo_file.read()
    gbytes = stream.read_bytes(file_size)
    stream.close()
    return gbytes


def get_apps_metadata(installation: Flatpak.Installation, remote: str):
    repo = OSTree.Repo.new(installation.get_path().get_child("repo"))
    repo.open()

    log.debug("Fetching refs from remote %s", remote)
    refs = []
    for ref in installation.list_remote_refs_sync_full(remote, Flatpak.QueryFlags.NONE):
        if ref.get_kind() != Flatpak.RefKind.APP:
            continue
        if ref.get_arch() != "x86_64":
            continue
        if ref.get_eol() or ref.get_eol_rebase():
            continue
        refs.append(ref)

    log.debug("Pulling ref files from %s", remote)
    repo.pull_with_options(remote, GLib.Variant("a{sv}", {
        "refs": GLib.Variant("as", [ref.format_ref() for ref in refs]),
        "subdirs": GLib.Variant("as", ["/metadata", "/files/manifest.json"]),
        "disable-static-deltas": GLib.Variant("b", True),
        "timestamp-check": GLib.Variant("b", True),
        "gpg-verify": GLib.Variant("b", False),
    }))

    log.debug("Fetching metadata from %s", remote)
    for ref in refs:
        log.debug("Loading metadata from ref %s", ref.format_ref())
        _success, ref_root, _ref_commit = repo.read_commit(ref.format_ref())

        metadata_bytes = load_ostree_file(ref_root, "metadata")
        metadata = GLib.KeyFile()
        metadata.load_from_bytes(metadata_bytes, GLib.KeyFileFlags.NONE)

        yield metadata


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("-u", "--url")
    parser.add_argument("repo_name")
    args = parser.parse_args()

    logging.basicConfig(level=logging.DEBUG)

    cache_home = Gio.File.new_for_path(GLib.get_user_cache_dir())
    inst_dir = cache_home.get_child(PROGRAM_NAME).get_child("inst")
    if not inst_dir.query_exists():
        inst_dir.make_directory_with_parents()
    inst = Flatpak.Installation.new_for_path(inst_dir, True)

    try:
        remote = inst.get_remote_by_name(args.repo_name)
    except GLib.Error as err:
        if args.url and err.matches(Flatpak.error_quark(), Flatpak.Error.REMOTE_NOT_FOUND):
            remote = Flatpak.Remote.new(args.repo_name)
            remote.set_url(args.url)
            log.info("Adding remote %s to installation %s", remote.get_name(), inst_dir.get_path())
            inst.add_remote(remote, if_needed=True)
        else:
            raise

    result = []

    for metadata in get_apps_metadata(inst, remote.get_name()):
        result.append(metadata_to_dict(metadata))

    json.dump(result, sys.stdout, indent=4)


if __name__ == "__main__":
    main()
