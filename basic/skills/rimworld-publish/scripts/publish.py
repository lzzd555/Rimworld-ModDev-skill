"""Plan, publish, and resume a reviewed RimWorld release on Steam Workshop."""

import argparse
from contextlib import ExitStack, contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import struct
import sys
from tempfile import NamedTemporaryFile, TemporaryDirectory
from xml.etree import ElementTree

from release_contract import ReleaseContractError, verify_release
from steam_backend import APPID, DEFAULT_RUN_DIR, SteamworksError, SteamworksUGC, find_dylib, parse_file_id


VISIBILITY = {"public": 0, "friends-only": 1, "private": 2, "unlisted": 3}
WORKSHOP_URL = "https://steamcommunity.com/sharedfiles/filedetails/?id={}"
RELEASE_ERROR = (ReleaseContractError, OSError, ValueError, SteamworksError, ElementTree.ParseError)


class PublishError(ValueError):
    pass


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def save(path, state):
    temporary = None
    try:
        with NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as output:
            temporary = Path(output.name)
            json.dump(state, output, ensure_ascii=False, indent=2)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        temporary.replace(path)
    finally:
        if temporary and temporary.exists():
            temporary.unlink()


@contextmanager
def lock(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_name(path.name + ".lock").open("a+b") as stream:
        if sys.platform == "win32":
            import msvcrt
            if stream.tell() == 0:
                stream.write(b"\0")
                stream.flush()
            stream.seek(0)
            acquire = lambda: msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            release = lambda: msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            acquire = lambda: fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            release = lambda: fcntl.flock(stream, fcntl.LOCK_UN)
        try:
            acquire()
        except OSError as error:
            raise PublishError("This journal is already in use.") from error
        try:
            yield
        finally:
            release()


def content_hash(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def image_size(path):
    body = path.read_bytes()
    if body.startswith(b"\x89PNG\r\n\x1a\n") and len(body) >= 33 and body[8:16] == b"\0\0\0\rIHDR":
        return struct.unpack_from(">II", body, 16)
    if not body.startswith(b"\xff\xd8"):
        return None
    offset = 2
    while offset + 9 <= len(body) and body[offset] == 0xFF:
        while offset + 9 <= len(body) and body[offset + 1] == 0xFF:
            offset += 1
        marker = body[offset + 1]
        if marker in (0xD9, 0xDA):
            break
        length = int.from_bytes(body[offset + 2:offset + 4], "big")
        if length < 2 or offset + 2 + length > len(body):
            break
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC) and length >= 7:
            height, width = struct.unpack_from(">HH", body, offset + 5)
            return width, height
        offset += 2 + length
    return None


def record_image(path, main):
    path = path.resolve(strict=True)
    if path.suffix.lower() not in {".png", ".jpg", ".jpeg"} or path.stat().st_size >= 1_000_000:
        raise PublishError(f"Choose a PNG or JPEG image under 1 MB: {path}")
    dimensions = image_size(path)
    if not dimensions or min(dimensions) <= 0 or (main and min(dimensions) < 512):
        raise PublishError(f"Invalid image dimensions (main preview requires 512 by 512 or larger): {path}")
    return {"path": str(path), "sha256": content_hash(path)}


def page_text(field, value):
    limit = 128 if field == "title" else 8000
    if not isinstance(value, str) or "\0" in value or len(value.encode("utf-8")) >= limit:
        raise PublishError(f"{field} must fit in {limit - 1} UTF-8 bytes and contain no NUL.")
    if field == "title" and not value.strip():
        raise PublishError("Title must contain text.")
    return value


def language_code(code):
    if not re.fullmatch(r"[a-z]{2,32}", code):
        raise PublishError("Use a Steam API language code such as english or schinese.")
    return code


def project_item(project, wanted_package):
    if project is None:
        return None
    about = project.resolve(strict=True) / "About"
    package = ElementTree.parse(about / "About.xml").findtext("packageId", "").strip()
    if package.casefold() != wanted_package.casefold():
        raise PublishError("Project packageId and verified release packageId differ.")
    marker = about / "PublishedFileId.txt"
    return parse_file_id(marker.read_text(encoding="utf-8-sig").strip()) if marker.exists() else None


def plan(args):
    release = verify_release(args.content)
    journal = args.journal.resolve()
    if journal.is_relative_to(release.content):
        raise PublishError("Store the journal outside the verified release content.")
    ids = {value for value in (release.item_id, project_item(args.project, release.package_id), args.item_id)
           if value is not None}
    if len(ids) > 1:
        raise PublishError("The release, project or explicit Workshop item IDs disagree.")
    if args.create == bool(ids):
        raise PublishError("Choose --create for an unbound release, or provide its existing Workshop item ID.")
    if not args.acceptance.strip():
        raise PublishError("Record the acceptance scope or evidence for this release.")
    if args.create and (args.title is None or args.preview is None):
        raise PublishError("A new item requires an explicit title and main preview.")

    fields = {}
    if args.title is not None:
        fields["title"] = page_text("title", args.title)
    if args.description_file is not None:
        fields["description"] = page_text("description", args.description_file.read_text(encoding="utf-8-sig"))
    if args.language is not None:
        if not fields:
            raise PublishError("--language requires a title or description update.")
        fields["language"] = language_code(args.language)
    primary_language = fields.get("language", "english") if fields else None
    if args.preview is not None:
        fields["preview"] = record_image(args.preview, main=True)
    if args.visibility or args.create:
        fields["visibility"] = args.visibility or "private"
    if args.replace_screenshots is not None:
        if len(args.replace_screenshots) > 20:
            raise PublishError("Supply up to 20 replacement screenshots.")
        fields["screenshots"] = [record_image(path, main=False) for path in args.replace_screenshots]
    if "\0" in args.changelog:
        raise PublishError("Change note must contain no NUL.")

    steps = [{"operation": "create", "state": "pending"}] if args.create else []
    steps.append({"operation": "content", "state": "pending", "fields": fields})
    languages = {primary_language}
    for spec in args.locale:
        code, separator, filename = spec.partition("=")
        if not separator or not filename:
            raise PublishError("Supply --locale as LANGUAGE=FILE.")
        code = language_code(code)
        if code in languages:
            raise PublishError("Plan at most one title/description update for each language.")
        languages.add(code)
        entry = read_json(Path(filename))
        if not isinstance(entry, dict) or not entry or entry.keys() - {"title", "description"}:
            raise PublishError("A locale file must contain title and/or description only.")
        translated = {key: page_text(key, value) for key, value in entry.items()}
        steps.append({"operation": "locale", "state": "pending", "fields": {"language": code, **translated}})
    state = {
        "schema": 1, "appId": APPID, "packageId": release.package_id, "content": str(release.content),
        "contentDigest": release.digest, "acceptance": args.acceptance, "changelog": args.changelog,
        "itemId": next(iter(ids), None), "accountId": None, "needsAgreement": False, "steps": steps
    }
    with lock(journal):
        if journal.exists():
            raise PublishError("Journal already exists. Use status or upload to resume, or select a new path.")
        save(journal, state)
    return state


def load(journal):
    state = read_json(journal)
    if state.get("schema") != 1 or state.get("appId") != APPID:
        raise PublishError("Unrecognized RimWorld publish journal.")
    return state


def public_status(state):
    state = dict(state)
    state["complete"] = all(step["state"] == "done" for step in state["steps"])
    if state["itemId"]:
        state["workshopUrl"] = WORKSHOP_URL.format(state["itemId"])
    return state


def prepare(state, directory):
    release = verify_release(Path(state["content"]), state["packageId"])
    if release.digest != state["contentDigest"]:
        raise PublishError("The release has changed since approval. Review and plan the new release.")
    content = None
    images = {}
    for step in state["steps"]:
        if step["state"] == "done" or step["operation"] != "content":
            continue
        content = directory / "artifact" / release.content.name
        shutil.copytree(release.content, content, symlinks=True)
        shutil.copyfile(release.manifest, content.parent / "manifest.json")
        receipt = read_json(release.receipt)
        receipt["directory"] = str(content)
        save(content.parent / "release-receipt.json", receipt)
        if verify_release(content, release.package_id).digest != state["contentDigest"]:
            raise PublishError("The verified release could not be materialized unchanged.")
        fields = step["fields"]
        assets = fields.get("screenshots", [])
        if "preview" in fields:
            assets = [fields["preview"], *assets]
        for index, asset in enumerate(assets):
            original = Path(asset["path"])
            copied = directory / ("page-" + str(index) + original.suffix)
            shutil.copyfile(original, copied)
            if content_hash(copied) != asset["sha256"]:
                raise PublishError(f"Page image has changed since approval: {original}")
            images[asset["path"]] = copied
    return content, images


def check_item(steam, item_id, account):
    details = steam.query_details(item_id)
    if (details.get("fileId") != item_id or details.get("consumerAppId") != APPID
            or details.get("ownerSteamId") != account):
        raise PublishError("The selected item must belong to the logged-in account and RimWorld.")
    return details


def stage_steam_update(steam, item_id, step, details, content, images):
    fields = step["fields"]
    handle = steam.start_update(item_id)
    if "language" in fields:
        steam.set_update_language(handle, fields["language"])
    if "title" in fields:
        steam.set_title(handle, fields["title"])
    if "description" in fields:
        steam.set_description(handle, fields["description"])
    if "visibility" in fields:
        steam.set_visibility(handle, VISIBILITY[fields["visibility"]])
    if "preview" in fields:
        steam.set_preview(handle, images[fields["preview"]["path"]])
    if "screenshots" in fields:
        previews = details["screenshots"]
        image_indices = sorted((preview["index"] for preview in previews if preview["type"] == 0), reverse=True)
        if len(previews) - len(image_indices) + len(fields["screenshots"]) > 20:
            raise PublishError("The new screenshots plus retained previews would exceed the gallery limit.")
        for index in image_indices:
            steam.remove_screenshot(handle, index)
        for entry in fields["screenshots"]:
            steam.add_screenshot(handle, images[entry["path"]])
    if step["operation"] == "content":
        steam.set_content(handle, content)
    return handle


def mutation(journal, state, step, perform):
    step["state"] = "in_flight"
    attempt = {"startedAt": datetime.now(timezone.utc).isoformat(), "state": "in_flight"}
    step.setdefault("attempts", []).append(attempt)
    save(journal, state)
    try:
        perform()
    except SteamworksError as error:
        step["state"] = "unknown" if error.outcome_unknown else "pending"
        attempt.update(state=step["state"], error=str(error), resultCode=error.result_code)
        save(journal, state)
        raise
    step["state"] = "done"
    attempt["state"] = "done"
    save(journal, state)


def run_steps(journal, state, steam, content, images, item_lock):
    if not steam.logged_on():
        raise PublishError("Sign in to the Windows Steam desktop client before uploading.")
    account = steam.steam_id()
    if not account or state["accountId"] not in (None, account):
        raise PublishError("Resume this journal on the same Steam account used before.")
    state["accountId"] = account
    save(journal, state)
    if state["itemId"]:
        item_lock(state["itemId"])
    for step in state["steps"]:
        if step["state"] == "done":
            continue
        if step["operation"] == "create":
            def create():
                item_id, agreement = steam.create_item()
                state["itemId"] = item_id
                state["needsAgreement"] = agreement
            mutation(journal, state, step, create)
            item_lock(state["itemId"])
            continue
        item_id = state["itemId"]
        details = check_item(steam, item_id, account)
        handle = stage_steam_update(steam, item_id, step, details, content, images)
        if "screenshots" in step["fields"] and check_item(steam, item_id, account)["screenshots"] != details["screenshots"]:
            raise PublishError("The Workshop gallery changed during preparation. Review it before retrying.")
        note = state["changelog"] if step["operation"] == "content" else ""

        def submit():
            result = steam.submit(handle, note)
            if result.get("fileId") != item_id:
                raise SteamworksError("Steam returned an unexpected item ID.", outcome_unknown=True)
            state["needsAgreement"] |= result["needsAccept"]
        mutation(journal, state, step, submit)


def upload(args, backend=SteamworksUGC):
    journal = args.journal.resolve()
    with lock(journal):
        state = load(journal)
        if any(step["state"] in {"unknown", "in_flight"} for step in state["steps"]):
            raise PublishError("Steam may have applied the previous step. Verify its outcome, then use resolve.")
        if all(step["state"] == "done" for step in state["steps"]):
            return public_status(state)
        sdk_path = find_dylib(args.sdk)
        if not sdk_path:
            raise PublishError("RimWorld Steamworks SDK was not found. Supply --sdk with its absolute path.")
        lock_dir = DEFAULT_RUN_DIR / "publish-locks"
        package_key = hashlib.sha256(state["packageId"].casefold().encode("utf-8")).hexdigest()
        with ExitStack() as stack:
            stack.enter_context(lock(lock_dir / ("package-" + package_key)))
            temporary = stack.enter_context(TemporaryDirectory(prefix="rimworld-workshop-", dir=journal.parent))
            content, images = prepare(state, Path(temporary))
            steam = stack.enter_context(backend(sdk_path.resolve(), DEFAULT_RUN_DIR, timeout=args.timeout))
            # Steam init changes the process working directory.
            run_steps(journal, state, steam, content, images,
                      lambda item: stack.enter_context(lock(lock_dir / ("item-" + str(item)))))
    return public_status(state)


def resolve(args):
    with lock(args.journal):
        state = load(args.journal)
        step = next((entry for entry in state["steps"] if entry["state"] in {"in_flight", "unknown"}), None)
        if step is None:
            raise PublishError("This journal has no operation awaiting resolution.")
        if not args.evidence.strip():
            raise PublishError("Record authoritative evidence for the outcome.")
        recovered_creation = step["operation"] == "create" and args.outcome == "completed"
        if recovered_creation != bool(args.item_id):
            raise PublishError("An item ID is required only to recover a confirmed successful creation.")
        if recovered_creation:
            state["itemId"] = args.item_id
        step["state"] = "done" if args.outcome == "completed" else "pending"
        step.setdefault("resolutions", []).append({
            "resolvedAt": datetime.now(timezone.utc).isoformat(), "outcome": args.outcome,
            "evidence": args.evidence, "itemId": args.item_id,
        })
        save(args.journal, state)
    return public_status(state)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("plan", "upload", "status", "resolve"):
        command = commands.add_parser(name)
        command.add_argument("--journal", required=True, type=Path)
        if name == "plan":
            command.add_argument("--content", required=True, type=Path)
            command.add_argument("--acceptance", required=True)
            command.add_argument("--project", type=Path)
            mode = command.add_mutually_exclusive_group()
            mode.add_argument("--create", action="store_true")
            mode.add_argument("--item-id", type=parse_file_id)
            command.add_argument("--title")
            command.add_argument("--description-file", type=Path)
            command.add_argument("--preview", type=Path)
            command.add_argument("--visibility", choices=VISIBILITY)
            command.add_argument("--language")
            command.add_argument("--changelog", default="")
            command.add_argument("--locale", action="append", default=[])
            command.add_argument("--replace-screenshots", nargs="*", type=Path)
        if name == "upload":
            command.add_argument("--sdk", type=Path)
            command.add_argument("--timeout", type=int, default=600)
        if name == "resolve":
            command.add_argument("--outcome", required=True, choices=("completed", "failed"))
            command.add_argument("--evidence", required=True)
            command.add_argument("--item-id", type=parse_file_id)
    args = parser.parse_args(argv)
    try:
        if args.command == "plan":
            result = public_status(plan(args))
        elif args.command == "upload":
            if args.timeout <= 0:
                raise PublishError("--timeout must be positive.")
            result = upload(args)
        elif args.command == "status":
            result = public_status(load(args.journal))
        else:
            result = resolve(args)
    except RELEASE_ERROR as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
