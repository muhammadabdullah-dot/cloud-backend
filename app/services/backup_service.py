"""Backup and restore for head office: a restorable copy of the cloud server's whole database (and its pictures), in a
folder someone chose. The same design as the branch server's.

A backup is one zip file:
  database.db   a consistent copy of the live database, taken with VACUUM INTO while the shop keeps working
  media/...     the Item and Party pictures, which live beside the database rather than in it
  backup.json   what it is: which branch, when, who took it, the database version, and a checksum

Backup Now writes one on demand. The server also takes one itself once a day (after the hour set, or whenever the last
one is more than a day old) and keeps the newest few of those, deleting older automatic ones; backups someone took by
hand are never deleted.

Restore puts a backup back over the live database. It is deliberately hard to do by accident: only a System Admin can,
the word RESTORE has to be typed, the backup must come from a head office server and pass its checksum and SQLite's own integrity
check, and a fresh backup of the current database is always taken first so a restore can itself be undone. A backup
from an older version of the software is brought up to date after it's put back; one from a newer version is refused.
While the restore runs every other request is turned away for a minute.

The list of backups, the settings and the history are kept in files, not in the database, so a restore can't wind them
back.
"""
import asyncio
import hashlib
import json
import os
import re
import shutil
import sqlite3
import sys
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.core.config import settings
from app.core.pk_time import PKT

APP_ID = "dmarina-cloud-server"
APP_LABEL = "head office"
FORMAT = 1
NAME_RE = re.compile(r"^DMARINA-[A-Za-z0-9_-]+-\d{8}-\d{6}-(manual|automatic|before-restore|uploaded)\.zip$")
KINDS = ("manual", "automatic", "before-restore", "uploaded")
# Counted into each backup's description, so a list of backups says what's in each one.
COUNT_TABLES = {"branches": "branches", "items": "products", "staff": "users", "vouchers": "acc_vouchers", "grns": "warehouse_grns", "transfers": "transfers"}

DEFAULTS = {"folder": "", "autoEnabled": True, "autoHour": 23, "keepAutomatic": 14, "includeMedia": True}

_lock = asyncio.Lock()
RESTORING = False


class BackupError(Exception):
    def __init__(self, message: str, status: int = 400):
        self.message = message
        self.status = status


# ── where things are ────────────────────────────────────────────────────────────────────────────

def db_path() -> Path:
    url = settings.db_url
    if not url.startswith("sqlite://"):
        raise BackupError("Backups only work with a SQLite database.")
    return Path(url[len("sqlite://"):]).resolve()


def media_dir() -> Path:
    return Path(settings.media_dir).resolve()


def home_dir() -> Path:
    """Settings and history live beside the database, in .backups — also the default backup folder."""
    return db_path().parent / ".backups"


def server_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ── settings and history ────────────────────────────────────────────────────────────────────────

def load_settings() -> dict:
    data = dict(DEFAULTS)
    try:
        data.update(json.loads((home_dir() / "settings.json").read_text(encoding="utf-8")))
    except (OSError, ValueError):
        pass
    data["folder"] = str(resolve_folder(data.get("folder") or ""))
    return data


def resolve_folder(folder: str | None) -> Path:
    text = (folder or "").strip().strip('"')
    if not text:
        return home_dir()
    path = Path(text).expanduser()
    return path if path.is_absolute() else (db_path().parent / path).resolve()


def _check_writable(folder: Path) -> None:
    try:
        folder.mkdir(parents=True, exist_ok=True)
        probe = folder / f".write-test-{os.getpid()}"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        raise BackupError(f"Can't write to {folder}: {exc.strerror or exc}. Pick a folder this computer can write to.") from exc


def save_settings(patch: dict) -> dict:
    current = load_settings()
    if "folder" in patch:
        folder = resolve_folder(patch["folder"])
        _check_writable(folder)
        current["folder"] = str(folder)
    if "autoEnabled" in patch:
        current["autoEnabled"] = bool(patch["autoEnabled"])
    if "autoHour" in patch:
        hour = int(patch["autoHour"])
        if not 0 <= hour <= 23:
            raise BackupError("The hour for the daily backup is 0 to 23.")
        current["autoHour"] = hour
    if "keepAutomatic" in patch:
        keep = int(patch["keepAutomatic"])
        if not 1 <= keep <= 365:
            raise BackupError("Keep between 1 and 365 daily backups.")
        current["keepAutomatic"] = keep
    if "includeMedia" in patch:
        current["includeMedia"] = bool(patch["includeMedia"])
    home_dir().mkdir(parents=True, exist_ok=True)
    (home_dir() / "settings.json").write_text(json.dumps(current, indent=2), encoding="utf-8")
    return current


def _log(event: str, **details) -> None:
    try:
        home_dir().mkdir(parents=True, exist_ok=True)
        with (home_dir() / "history.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"at": _now().isoformat(), "event": event, **details}, default=str) + "\n")
    except OSError:
        pass


def history(limit: int = 50) -> list[dict]:
    try:
        lines = (home_dir() / "history.jsonl").read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out = []
    for line in reversed(lines[-limit * 2:]):
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out[:limit]


# ── reading a database file ─────────────────────────────────────────────────────────────────────

def _inspect(path: Path, integrity_check: bool = True) -> dict:
    """Integrity, version and a few counts, straight from a database file."""
    conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=30)
    try:
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0] if integrity_check else None
        try:
            version = conn.execute("SELECT version FROM aerich ORDER BY id DESC LIMIT 1").fetchone()
        except sqlite3.DatabaseError:
            version = None
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        counts = {label: conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0] for label, table in COUNT_TABLES.items() if table in tables}
        identity = _identity_from(conn, tables)
    finally:
        conn.close()
    return {"integrity": integrity, "schemaVersion": version[0] if version else None, "counts": counts, **identity}


def _identity_from(conn: sqlite3.Connection, tables: set[str]) -> dict:
    """There is one head office; every cloud database is its own."""
    return {"code": "HO", "name": "Head office"}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def migration_versions() -> list[str]:
    folder = server_root() / "migrations" / "models"
    return sorted((p.name for p in folder.glob("*.py") if p.name[0].isdigit()), key=lambda n: int(n.split("_", 1)[0]))


def _version_index(version: str | None) -> int:
    if not version:
        return -1
    try:
        return int(version.split("_", 1)[0])
    except ValueError:
        return -1


# ── making a backup ─────────────────────────────────────────────────────────────────────────────

def _make_backup(kind: str, folder: Path, created_by: str | None, note: str | None, include_media: bool) -> dict:
    _check_writable(folder)
    live = db_path()
    info_live = _inspect(live, integrity_check=False)
    code = info_live.get("code") or "HO"
    stamp = _now().astimezone(PKT).strftime("%Y%m%d-%H%M%S")
    name = f"DMARINA-{code}-{stamp}-{kind}.zip"
    final = folder / name
    if final.exists():
        raise BackupError("A backup with that name was taken this very second. Try again.")
    snapshot = folder / f".{name}.db.part"
    part = folder / f".{name}.part"
    try:
        conn = sqlite3.connect(str(live), timeout=60)
        try:
            conn.execute("VACUUM INTO ?", (str(snapshot),))
        finally:
            conn.close()
        info = _inspect(snapshot)
        if info["integrity"] != "ok":
            raise BackupError(f"The copy failed SQLite's integrity check ({info['integrity']}). Nothing was saved.")
        media_files = []
        if include_media and media_dir().is_dir():
            media_files = [p for p in media_dir().rglob("*") if p.is_file()]
        manifest = {
            "format": FORMAT, "app": APP_ID, "kind": kind,
            "branchCode": info.get("code"), "branchName": info.get("name"),
            "createdAt": _now().isoformat(), "createdBy": created_by, "note": (note or "").strip()[:200] or None,
            "schemaVersion": info.get("schemaVersion"), "counts": info.get("counts"),
            "databaseBytes": snapshot.stat().st_size, "databaseSha256": _sha256(snapshot),
            "mediaFiles": len(media_files),
        }
        with zipfile.ZipFile(part, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as zf:
            zf.write(snapshot, "database.db")
            for file in media_files:
                zf.write(file, "media/" + file.relative_to(media_dir()).as_posix())
            zf.writestr("backup.json", json.dumps(manifest, indent=2))
        part.replace(final)
    except BackupError:
        raise
    except (OSError, sqlite3.Error, zipfile.BadZipFile) as exc:
        raise BackupError(f"The backup couldn't be written: {exc}") from exc
    finally:
        for leftover in (snapshot, part):
            try:
                leftover.unlink()
            except OSError:
                pass
    row = _describe(final, manifest)
    _log("backup", name=name, folder=str(folder), kind=kind, by=created_by, sizeBytes=row["sizeBytes"])
    return row


async def create_backup(kind: str = "manual", created_by: str | None = None, note: str | None = None, folder: str | None = None) -> dict:
    if kind not in KINDS:
        kind = "manual"
    conf = load_settings()
    target = resolve_folder(folder) if folder else Path(conf["folder"])
    if _lock.locked():
        raise BackupError("A backup or restore is already running. Wait for it to finish.", status=409)
    async with _lock:
        try:
            return await asyncio.to_thread(_make_backup, kind, target, created_by, note, bool(conf.get("includeMedia", True)))
        except BackupError as exc:
            _log("backup-failed", kind=kind, folder=str(target), by=created_by, error=exc.message)
            raise


# ── listing ─────────────────────────────────────────────────────────────────────────────────────

def _read_manifest(path: Path) -> dict:
    with zipfile.ZipFile(path) as zf:
        names = set(zf.namelist())
        if "backup.json" not in names or "database.db" not in names:
            raise BackupError(f"{path.name} isn't a D.Marina backup.")
        return json.loads(zf.read("backup.json"))


def _describe(path: Path, manifest: dict | None = None, error: str | None = None) -> dict:
    stat = path.stat()
    m = manifest or {}
    return {
        "name": path.name, "folder": str(path.parent), "sizeBytes": stat.st_size,
        "kind": m.get("kind"), "createdAt": m.get("createdAt") or datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
        "createdBy": m.get("createdBy"), "note": m.get("note"), "branchCode": m.get("branchCode"), "branchName": m.get("branchName"),
        "schemaVersion": m.get("schemaVersion"), "counts": m.get("counts") or {}, "mediaFiles": m.get("mediaFiles", 0),
        "app": m.get("app"), "problem": error,
    }


def list_backups(folder: str | None = None) -> list[dict]:
    target = resolve_folder(folder) if folder else Path(load_settings()["folder"])
    if not target.is_dir():
        return []
    rows = []
    for path in target.glob("DMARINA-*.zip"):
        try:
            manifest = _read_manifest(path)
            problem = None if manifest.get("app") == APP_ID else "Made by a different D.Marina server."
            rows.append(_describe(path, manifest, problem))
        except (BackupError, zipfile.BadZipFile, ValueError, OSError) as exc:
            rows.append(_describe(path, None, getattr(exc, "message", None) or "Damaged or unreadable file."))
    rows.sort(key=lambda r: r["createdAt"], reverse=True)
    return rows


def backup_file(name: str, folder: str | None = None) -> Path:
    if not NAME_RE.match(name or ""):
        raise BackupError("That isn't a backup file name.", status=404)
    target = resolve_folder(folder) if folder else Path(load_settings()["folder"])
    path = (target / name).resolve()
    if path.parent != target.resolve() or not path.is_file():
        raise BackupError(f"{name} isn't in {target}.", status=404)
    return path


async def database_status() -> dict:
    live = db_path()
    info = await asyncio.to_thread(_inspect, live, False)
    versions = migration_versions()
    return {
        "path": str(live), "sizeBytes": live.stat().st_size, "schemaVersion": info.get("schemaVersion"),
        "latestVersion": versions[-1] if versions else None, "branchCode": info.get("code"), "branchName": info.get("name"),
        "counts": info.get("counts"), "mediaFolder": str(media_dir()),
    }


# ── bringing a backup in from elsewhere ─────────────────────────────────────────────────────────

async def save_upload(stream, filename: str, uploaded_by: str | None) -> dict:
    """A backup file copied from another computer or a USB drive, saved into the backup folder so it can be restored."""
    folder = Path(load_settings()["folder"])
    _check_writable(folder)
    part = folder / f".upload-{os.getpid()}-{int(_now().timestamp())}.part"
    size = 0
    try:
        with part.open("wb") as fh:
            while chunk := await stream.read(1024 * 1024):
                size += len(chunk)
                fh.write(chunk)
        try:
            manifest = _read_manifest(part)
        except (zipfile.BadZipFile, ValueError) as exc:
            raise BackupError(f"{filename} isn't a D.Marina backup.") from exc
        if manifest.get("app") != APP_ID:
            raise BackupError(f"{filename} was made by a different D.Marina server, not a {APP_LABEL} server.")
        created = datetime.fromisoformat(manifest["createdAt"]).astimezone(PKT).strftime("%Y%m%d-%H%M%S")
        code = manifest.get("branchCode") or "HO"
        original = manifest.get("kind") if manifest.get("kind") in KINDS else "uploaded"
        name = f"DMARINA-{code}-{created}-{original}.zip"
        final = folder / name
        if final.exists():
            part.unlink()
            _log("upload", name=name, by=uploaded_by, alreadyThere=True)
            return _describe(final, manifest)
        part.replace(final)
    except BackupError:
        part.unlink(missing_ok=True)
        raise
    except OSError as exc:
        part.unlink(missing_ok=True)
        raise BackupError(f"The file couldn't be saved: {exc}") from exc
    _log("upload", name=name, by=uploaded_by, sizeBytes=size)
    return _describe(final, manifest)


# ── restore ─────────────────────────────────────────────────────────────────────────────────────

async def _pause_background() -> None:
    from app.core import accounts_scheduler

    await accounts_scheduler.stop()


def _resume_background() -> None:
    from app.core import accounts_scheduler

    accounts_scheduler.start()


async def _after_restore() -> None:
    """What the server does at startup (standard chart rows, role grants, and so on), done again for the database that
    was just put back, so a backup from before a feature existed works straight away rather than after a restart."""
    try:
        from app.main import _seed

        await _seed()
    except Exception as exc:  # noqa: BLE001 — the restore itself has succeeded; a restart finishes this
        _log("after-restore-warning", error=f"{type(exc).__name__}: {exc}")


async def _upgrade_schema() -> str:
    env = {**os.environ, "DB_URL": settings.db_url, "PYTHONIOENCODING": "utf-8"}
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-c", "import sys; from aerich.cli import main; sys.argv=['aerich','upgrade']; main()",
        cwd=os.getcwd(), env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    out, _ = await asyncio.wait_for(proc.communicate(), timeout=600)
    text = out.decode("utf-8", "replace")
    if proc.returncode != 0:
        raise BackupError(f"The backup was put back but bringing it up to date failed: {text[-400:]}")
    return text


def _copy_into_live(source: Path) -> None:
    src = sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True)
    dst = sqlite3.connect(str(db_path()), timeout=60)
    try:
        src.backup(dst, pages=1024, sleep=0.05)
        dst.commit()
    finally:
        src.close()
        dst.close()


def _restore_media(archive: Path) -> int:
    restored = 0
    root = media_dir()
    with zipfile.ZipFile(archive) as zf:
        for info in zf.infolist():
            if not info.filename.startswith("media/") or info.is_dir():
                continue
            relative = Path(info.filename[len("media/"):])
            target = (root / relative).resolve()
            if root not in target.parents:
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, target.open("wb") as dst:
                shutil.copyfileobj(src, dst)
            restored += 1
    return restored


async def restore(name: str, confirm: str, restored_by: str | None, folder: str | None = None, include_media: bool = True) -> dict:
    global RESTORING
    if (confirm or "").strip() != "RESTORE":
        raise BackupError("Type RESTORE in capitals to confirm. Everything since that backup will be replaced.")
    archive = backup_file(name, folder)
    try:
        manifest = _read_manifest(archive)
    except (zipfile.BadZipFile, ValueError) as exc:
        raise BackupError(f"{name} is damaged and can't be read.") from exc
    if manifest.get("app") != APP_ID:
        raise BackupError(f"{name} was made by a different D.Marina server, not this {APP_LABEL} server.")

    current = await database_status()
    if current.get("branchCode") and manifest.get("branchCode") and current["branchCode"] != manifest["branchCode"]:
        raise BackupError(f"{name} belongs to {manifest.get('branchName') or manifest['branchCode']}, not {current.get('branchName') or current['branchCode']}. "
                          "Restoring another branch's database here would mix up both branches' records at head office.")
    known = migration_versions()
    backup_index = _version_index(manifest.get("schemaVersion"))
    latest_index = _version_index(known[-1]) if known else -1
    if backup_index > latest_index:
        raise BackupError(f"{name} was made by a newer version of the software. Update this server first, then restore it.")

    if _lock.locked():
        raise BackupError("A backup or restore is already running. Wait for it to finish.", status=409)
    async with _lock:
        staging = home_dir() / f".restore-{int(_now().timestamp())}.db"
        home_dir().mkdir(parents=True, exist_ok=True)
        try:
            def extract() -> dict:
                with zipfile.ZipFile(archive) as zf, zf.open("database.db") as src, staging.open("wb") as dst:
                    shutil.copyfileobj(src, dst)
                if manifest.get("databaseSha256") and _sha256(staging) != manifest["databaseSha256"]:
                    raise BackupError(f"{name} doesn't match its own checksum: the file has been damaged or changed.")
                info = _inspect(staging)
                if info["integrity"] != "ok":
                    raise BackupError(f"{name} failed SQLite's integrity check ({info['integrity']}).")
                return info

            await asyncio.to_thread(extract)
            conf = load_settings()
            safety = await asyncio.to_thread(_make_backup, "before-restore", Path(conf["folder"]), restored_by,
                                             f"Taken automatically before restoring {name}", bool(conf.get("includeMedia", True)))

            RESTORING = True
            await _pause_background()
            upgraded = False
            media = 0
            try:
                await asyncio.to_thread(_copy_into_live, staging)
                if backup_index < latest_index:
                    await _upgrade_schema()
                    upgraded = True
                if include_media:
                    media = await asyncio.to_thread(_restore_media, archive)
                await _after_restore()
            finally:
                RESTORING = False
                _resume_background()
            after = await database_status()
        except BackupError as exc:
            _log("restore-failed", name=name, by=restored_by, error=exc.message)
            raise
        except (OSError, sqlite3.Error, zipfile.BadZipFile) as exc:
            _log("restore-failed", name=name, by=restored_by, error=str(exc))
            raise BackupError(f"The restore failed: {exc}. The safety backup taken just before it is in the backup folder.") from exc
        finally:
            staging.unlink(missing_ok=True)

    _log("restore", name=name, by=restored_by, safetyBackup=safety["name"], upgraded=upgraded, mediaFiles=media)
    return {"restoredFrom": _describe(archive, manifest), "safetyBackup": safety, "schemaUpgraded": upgraded,
            "mediaFiles": media, "database": after}


# ── the daily backup ────────────────────────────────────────────────────────────────────────────

def _prune_automatic(folder: Path, keep: int) -> list[str]:
    autos = sorted(folder.glob("DMARINA-*-automatic.zip"), key=lambda p: p.name, reverse=True)
    removed = []
    for old in autos[keep:]:
        try:
            old.unlink()
            removed.append(old.name)
        except OSError:
            pass
    if removed:
        _log("prune", removed=removed, kept=keep)
    return removed


async def automatic_tick() -> dict | None:
    """Once a day: after the set hour, or straight away if the newest daily backup is more than a day old."""
    conf = load_settings()
    if not conf.get("autoEnabled", True):
        return None
    folder = Path(conf["folder"])
    now = _now().astimezone(PKT)
    autos = sorted(folder.glob("DMARINA-*-automatic.zip"), key=lambda p: p.name, reverse=True) if folder.is_dir() else []
    last = None
    if autos:
        match = re.search(r"-(\d{8}-\d{6})-automatic\.zip$", autos[0].name)
        if match:
            last = datetime.strptime(match.group(1), "%Y%m%d-%H%M%S").replace(tzinfo=PKT)
    due_today = now.hour >= int(conf.get("autoHour", 23)) and (last is None or last.date() < now.date())
    overdue = last is None or now - last > timedelta(hours=26)
    if not (due_today or overdue):
        return None
    row = await create_backup("automatic", "Daily backup")
    await asyncio.to_thread(_prune_automatic, folder, int(conf.get("keepAutomatic", 14)))
    return row
