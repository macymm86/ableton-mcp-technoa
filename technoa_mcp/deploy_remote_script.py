"""Spielt das Technoa-Remote-Script in Lives User Library ein oder nimmt es zurueck.

    python deploy_remote_script.py              # einspielen
    python deploy_remote_script.py --check      # nur anzeigen, nichts schreiben
    python deploy_remote_script.py --restore    # juengste Sicherung zurueckspielen
    python deploy_remote_script.py --user-library "D:\\Musik\\User Library"

Schreibt ausschliesslich <User Library>/Remote Scripts/AbletonMCP/__init__.py.
Liegt dort schon eine andere Datei (etwa das Original von ahujasid/ableton-mcp),
kommt sie vorher nach deploy_backups/ neben diesem Skript. Ohne Sicherung wird
nichts ueberschrieben.

Die User Library liegt standardmaessig unter Dokumente/Ableton (Windows) bzw.
Musik/Ableton (macOS). Wer sie verlegt hat (Live: Settings > Library > Location
of User Library), gibt den Ort mit --user-library oder ABLETON_USER_LIBRARY an.

Live liest Remote Scripts nur beim Start. Nach dem Einspielen Live neu starten.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SOURCE = os.path.join(HERE, "remote_script", "AbletonMCP", "__init__.py")
BACKUPS = os.path.join(HERE, "deploy_backups")
LOG = os.path.join(HERE, "DEPLOY_LOG.txt")


def default_user_library() -> str:
    home = os.path.expanduser("~")
    if sys.platform == "darwin":
        return os.path.join(home, "Music", "Ableton", "User Library")
    return os.path.join(home, "Documents", "Ableton", "User Library")


def sha256(path: str) -> str | None:
    if not os.path.isfile(path):
        return None
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def write_atomic(src: str, dst: str):
    # Erst daneben schreiben, dann tauschen: Ein Abbruch mittendrin hinterlaesst
    # nie eine halbe __init__.py, an der Live beim Start scheitert.
    tmp = dst + ".technoa_tmp"
    shutil.copyfile(src, tmp)
    os.replace(tmp, dst)


def log(action: str, new_hash: str | None, old_hash: str | None, target: str):
    with open(LOG, "a", encoding="utf-8") as fh:
        fh.write("%s | %s | neu=%s | vorher=%s | %s\n" % (dt.datetime.now().isoformat(timespec="seconds"), action,
                                                           new_hash, old_hash, target))


def latest_backup() -> str | None:
    try:
        names = sorted(f for f in os.listdir(BACKUPS) if f.endswith(".py"))
    except OSError:
        return None
    return os.path.join(BACKUPS, names[-1]) if names else None


def main() -> int:
    ap = argparse.ArgumentParser(description="Remote Script in Lives User Library einspielen.")
    ap.add_argument("--user-library", default=os.environ.get("ABLETON_USER_LIBRARY") or default_user_library(),
                    help="Ordner der User Library (Standard: %(default)s)")
    ap.add_argument("--restore", action="store_true", help="juengste Sicherung aus deploy_backups/ zurueckspielen")
    ap.add_argument("--check", action="store_true", help="nur Stand anzeigen")
    args = ap.parse_args()

    library = os.path.abspath(args.user_library)
    target = os.path.join(library, "Remote Scripts", "AbletonMCP", "__init__.py")
    current, source = sha256(target), sha256(SOURCE)
    state = ("fehlt" if current is None else
             "diese Fassung (aktuell)" if current == source else
             "andere Fassung (wird vor dem Einspielen gesichert)")
    print("Ziel:   %s\n        %s -> %s" % (target, current, state))
    print("Quelle: %s\n        %s" % (SOURCE, source))
    if args.check:
        return 0
    if not os.path.isdir(library):
        print("Abbruch: Die User Library %s gibt es nicht. Ort in Live nachsehen (Settings > Library) und mit "
              "--user-library angeben." % library)
        return 2

    if args.restore:
        backup = latest_backup()
        if backup is None:
            print("Abbruch: Keine Sicherung in %s. Zum Entfernen den Ordner %s loeschen und Live neu starten."
                  % (BACKUPS, os.path.dirname(target)))
            return 2
        write_atomic(backup, target)
        after = sha256(target)
        ok = after == sha256(backup)
        log("restore", after, current, target)
        print("Zurueckgespielt: %s (%s). Live neu starten." % (os.path.basename(backup),
                                                               "ok" if ok else "PRUEFSUMME FALSCH"))
        return 0 if ok else 1

    if current == source:
        print("Diese Fassung liegt schon im Ziel. Nichts zu tun.")
        return 0
    if current is not None:
        os.makedirs(BACKUPS, exist_ok=True)
        saved = os.path.join(BACKUPS, "%s_%s.py" % (dt.datetime.now().strftime("%Y-%m-%d_%H%M%S"), current[:12]))
        shutil.copyfile(target, saved)
        if sha256(saved) != current:
            print("Abbruch: Die Sicherung %s ist nicht vollstaendig. Nichts ueberschrieben." % saved)
            return 2
        print("Gesichert: %s" % saved)
    os.makedirs(os.path.dirname(target), exist_ok=True)
    write_atomic(SOURCE, target)
    after = sha256(target)
    ok = after == source
    log("deploy", after, current, target)
    print("Eingespielt: %s. Live neu starten und unter Settings > Link, Tempo & MIDI als Control Surface "
          "'AbletonMCP' waehlen." % ("ok" if ok else "PRUEFSUMME FALSCH"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
