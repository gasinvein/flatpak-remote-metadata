#!/usr/bin/env python3

from typing import Optional
import argparse
import logging
import json
import sys
import re
import io
import signal
import typing as t
import dataclasses

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


@dataclasses.dataclass
class Options:
    remote_name: str
    remote_url: t.Optional[str]
    refs: t.List[str]
    pull: bool
    get_metadata: bool
    get_manifest: bool
    get_built_extensions: bool


def get_value(metadata: GLib.KeyFile,
              group: str,
              key: str) -> t.Union[str, int, bool, t.List[str]]:
    for group_re, key_re, cls in MEATADATA_TYPES:
        if group_re.fullmatch(group) and key_re.fullmatch(key):
            if cls is bool:
                return metadata.get_boolean(group, key)
            if cls is list:
                return metadata.get_string_list(group, key)
            if cls is int:
                return metadata.get_integer(group, key)
    return metadata.get_string(group, key)


def metadata_to_dict(metadata: Optional[GLib.KeyFile]) -> Optional[t.Dict[str, t.Any]]:
    if metadata is None:
        return None
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


def load_ostree_file(ref_root: OSTree.RepoFile,
                     path: str,
                     cancellable: Gio.Cancellable = None) -> GLib.Bytes:
    repo_file = ref_root.resolve_relative_path(path)
    file_size = repo_file.query_info(Gio.FILE_ATTRIBUTE_STANDARD_SIZE,
                                     Gio.FileQueryInfoFlags.NONE,
                                     cancellable).get_size()
    stream = repo_file.read(cancellable)
    gbytes = stream.read_bytes(file_size, cancellable)
    stream.close(cancellable)
    return gbytes


def is_built_extension(app_id: str) -> bool:
    return app_id.endswith(".Sources") or app_id.endswith(".Locale") or app_id.endswith(".Debug")


def get_apps_metadata(installation: Flatpak.Installation,
                      remote: str,
                      opts: Options,
                      cancellable: Gio.Cancellable = None) -> \
                      t.Iterator[t.Tuple[Flatpak.Ref,
                                         GLib.KeyFile,
                                         t.Dict[str, t.Any]]]:
    def progress_cb(progress: OSTree.AsyncProgress, *args, **kwargs):
        fetched = progress.get_uint("fetched")
        requested = progress.get_uint("requested")
        log.info("Progress %.2f%% : objects fetched: %i/%i",
                 fetched * 100 / requested,
                 fetched,
                 requested)

    repo = OSTree.Repo.new(installation.get_path().get_child("repo"))
    repo.open(cancellable)

    log.info("Fetching refs from remote %s", remote)
    refs = []
    for ref in installation.list_remote_refs_sync_full(remote,
                                                       Flatpak.QueryFlags.NONE,
                                                       cancellable):
        if opts.refs and ref.format_ref() not in opts.refs:
            continue
        if ref.get_arch() != "x86_64":
            continue
        if ref.get_eol() or ref.get_eol_rebase():
            continue
        if not opts.get_built_extensions and is_built_extension(ref.get_name()):
            continue
        refs.append(ref)

    if opts.pull:
        pull_files = ["/metadata"]
        if opts.get_manifest:
            pull_files += ["/files/manifest.json"]

        progress = OSTree.AsyncProgress.new()
        progress.connect("changed", progress_cb, None)

        log.info("Pulling ref files from %s", remote)
        repo.pull_with_options(remote,
                               GLib.Variant("a{sv}", {
                                   "refs": GLib.Variant("as", [ref.format_ref() for ref in refs]),
                                   "subdirs": GLib.Variant("as", pull_files),
                                   "disable-static-deltas": GLib.Variant("b", True),
                                   "gpg-verify": GLib.Variant("b", False),
                               }),
                               progress,
                               cancellable)

        progress.finish()

    log.debug("Fetching metadata from %s", remote)
    for ref in refs:
        log.debug("Loading metadata from ref %s", ref.format_ref())
        try:
            _success, ref_root, _ref_commit = repo.read_commit(ref.format_ref(), cancellable)
        except GLib.Error as err:
            if err.matches(Gio.io_error_quark(), Gio.IOErrorEnum.NOT_FOUND):
                log.error("Can't read local ref: %s", err.message)  # pylint: disable=no-member
                ref_root = None
            else:
                raise

        if opts.get_metadata:
            metadata = GLib.KeyFile()
            if ref_root is not None:
                metadata_bytes = load_ostree_file(ref_root, "metadata", cancellable)
            else:
                metadata_bytes = ref.get_metadata()
            metadata.load_from_bytes(metadata_bytes, GLib.KeyFileFlags.NONE)
        else:
            metadata = None

        if opts.get_manifest and ref_root is not None and not is_built_extension(ref.get_name()):
            try:
                manifest_bytes = load_ostree_file(ref_root, "files/manifest.json", cancellable)
                with io.BytesIO(manifest_bytes.get_data()) as mf_io:
                    manifest = json.load(mf_io)
            except GLib.Error as err:
                if err.matches(Gio.io_error_quark(), Gio.IOErrorEnum.NOT_FOUND):
                    manifest = None
                else:
                    raise
        else:
            manifest = None

        yield (ref, metadata, manifest)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("-u", "--url")
    parser.add_argument("-r", "--ref", nargs="+")
    parser.add_argument("--no-pull", action="store_true")
    parser.add_argument("--no-metadata", action="store_true")
    parser.add_argument("--no-manifest", action="store_true")
    parser.add_argument("--no-built-extensions", action="store_true")
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("-o", "--output")
    parser.add_argument("repo_name")
    args = parser.parse_args()

    opts = Options(remote_name=args.repo_name,
                   remote_url=args.url,
                   refs=args.ref,
                   pull=not args.no_pull,
                   get_metadata=not args.no_metadata,
                   get_manifest=not args.no_manifest,
                   get_built_extensions=not args.no_built_extensions)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO)

    cancellable = Gio.Cancellable.new()

    def abort(sign, *_):
        log.info("Caught signal %s, exiting", signal.strsignal(sign))
        cancellable.cancel()
        sys.exit(1)

    for sig in [signal.SIGINT, signal.SIGTERM, signal.SIGHUP]:
        signal.signal(sig, abort)

    cache_home = Gio.File.new_for_path(GLib.get_user_cache_dir())
    inst_dir = cache_home.get_child(PROGRAM_NAME).get_child("inst")
    if not inst_dir.query_exists(cancellable):
        inst_dir.make_directory_with_parents(cancellable)
    inst = Flatpak.Installation.new_for_path(inst_dir, True, cancellable)

    try:
        remote = inst.get_remote_by_name(opts.remote_name, cancellable)
    except GLib.Error as err:
        if opts.remote_url and err.matches(Flatpak.error_quark(), Flatpak.Error.REMOTE_NOT_FOUND):
            remote = Flatpak.Remote.new(opts.remote_name)
            remote.set_url(opts.remote_url)
            log.info("Adding remote %s to installation %s", remote.get_name(), inst_dir.get_path())
            inst.add_remote(remote, if_needed=True, cancellable=cancellable)
        else:
            raise

    result = []

    for ref, metadata, manifest in get_apps_metadata(inst, remote.get_name(), opts, cancellable):
        result.append({
            "ref": ref.format_ref(),
            "metadata": metadata_to_dict(metadata),
            "manifest": manifest,
        })

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=4)
    else:
        json.dump(result, sys.stdout, indent=4)


if __name__ == "__main__":
    main()
