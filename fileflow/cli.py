"""Interfaz de linea de comandos.

El nucleo tiene que ser usable sin UI: la CLI es la primera interfaz y la que
usan las pruebas. Ver docs/diseno/arquitectura.md
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

from .config import PROFILES, AppConfig, detect_profile
from .db.index import Index
from .watcher.reconcile import recover_interrupted, reconcile
from .watcher.watcher import FileWatcher


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s  %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )


# ---------------------------------------------------------------------------
# Comandos
# ---------------------------------------------------------------------------


def cmd_init(args: argparse.Namespace) -> int:
    cfg = AppConfig.load()
    if args.profile:
        cfg.profile = args.profile
    else:
        cfg.profile = detect_profile()

    path = cfg.save()
    with Index(cfg.db_path) as index:
        index.set_meta("profile", cfg.profile)
        index.set_meta("text_model", cfg.profile_obj.text_model)

    profile = cfg.profile_obj
    print(f"Indice creado en   {cfg.db_path}")
    print(f"Configuracion en   {path}")
    print(f"Perfil detectado   {profile.name}")
    print(f"  texto            {profile.text_model} ({profile.text_dim}d)")
    print(f"  imagen           {profile.image_model} ({profile.image_dim}d)")
    print(f"  runtime          {profile.runtime} / {profile.device}")
    print(f"  LLM              {profile.llm or 'ninguno'}")
    return 0


def cmd_watch(args: argparse.Namespace) -> int:
    cfg = AppConfig.load()

    with Index(cfg.db_path) as index:
        directories = [Path(d).resolve() for d in args.directories] if args.directories else []
        for directory in directories:
            index.add_watched_dir(directory)
        if not directories:
            directories = [Path(r["path"]) for r in index.list_watched_dirs()]
        if not directories:
            print("No hay directorios que vigilar. Indica al menos uno:")
            print('  python -m fileflow watch "C:\\Users\\<usuario>\\Downloads"')
            return 1

        # 1. Lo que quedo a medias en una ejecucion anterior.
        for finding in recover_interrupted(index):
            print(f"  ! operacion #{finding['journal_id']} interrumpida: {finding['state']}")

        # 2. Lo que llego mientras la aplicacion estaba cerrada.
        print("Reconciliando con el disco...")
        report = reconcile(index, directories)
        print(f"  {report.summary()}")

        # 3. Vigilancia en vivo.
        def on_file(path: Path) -> None:
            item_id = index.upsert_item(path)
            print(f"  + {path.name}  (id={item_id}, pendiente de analisis)")

        watcher = FileWatcher(cfg.watcher, on_file=on_file)
        for directory in directories:
            watcher.add_directory(directory)

        print(f"\nVigilando {len(watcher.directories)} directorio(s). Ctrl+C para parar.")
        for directory in watcher.directories:
            print(f"  - {directory}")

        watcher.start()
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            print("\nParando...")
        finally:
            watcher.stop()
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    cfg = AppConfig.load()
    if not cfg.db_path.exists():
        print("No hay indice. Ejecuta primero:  python -m fileflow init")
        return 1

    with Index(cfg.db_path) as index:
        stats = index.stats()
        print(f"Perfil            {cfg.profile}")
        print(f"Indice            {cfg.db_path}")
        print(f"Directorios       {stats['watched_dirs']}")
        print(f"Carpetas destino  {stats['folders']}")
        print(f"Items             {stats['items']}")
        for status, count in sorted(stats["by_status"].items()):
            print(f"  {status:<14} {count}")
        print(f"Embeddings        {stats['embeddings']}")
        print(f"Ejemplares        {stats['exemplars']}")
        print(f"Pendientes        {stats['pending_decisions']}")
        print(f"Movimientos       {stats['moves']}")
    return 0


def cmd_add_folder(args: argparse.Namespace) -> int:
    cfg = AppConfig.load()
    with Index(cfg.db_path) as index:
        folder_id = index.add_folder(args.path, args.description or "", is_trash=args.trash)
        label = "carpeta de descarte" if args.trash else "carpeta destino"
        print(f"{label} #{folder_id}: {Path(args.path).resolve()}")
        if args.description:
            print(f"  descripcion: {args.description}")
        else:
            print("  sin descripcion -- sin ella la carpeta no puede competir mientras")
            print("  este vacia. Ver docs/diseno/motor-de-decision.md")
    return 0


def cmd_folders(args: argparse.Namespace) -> int:
    cfg = AppConfig.load()
    with Index(cfg.db_path) as index:
        folders = index.list_folders()
        if not folders:
            print("No hay carpetas destino. Anade una con:")
            print('  python -m fileflow add-folder "D:\\Imagenes\\Gatos" -d "fotos de mis gatos"')
            return 0
        for folder in folders:
            flags = []
            if folder.is_trash:
                flags.append("descarte")
            if folder.auto_move:
                flags.append("automatico")
            suffix = f"  [{', '.join(flags)}]" if flags else ""
            print(f"#{folder.id}  {folder.path}{suffix}")
            print(f"     {folder.description or '(sin descripcion)'}")
    return 0


def cmd_profiles(args: argparse.Namespace) -> int:
    current = AppConfig.load().profile
    for name, profile in PROFILES.items():
        marker = "*" if name == current else " "
        print(f"{marker} {name}")
        print(f"    texto    {profile.text_model} ({profile.text_dim}d)")
        print(f"    imagen   {profile.image_model} ({profile.image_dim}d)")
        print(f"    runtime  {profile.runtime}/{profile.device}"
              f"{' int8' if profile.quantized else ''}")
        print(f"    LLM      {profile.llm or 'ninguno'}")
    return 0


# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fileflow",
        description="Organizador de archivos local que entiende el contenido",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="log detallado")
    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init", help="crear el indice y detectar el perfil")
    p_init.add_argument("--profile", choices=list(PROFILES), help="forzar un perfil")
    p_init.set_defaults(func=cmd_init)

    p_watch = sub.add_parser("watch", help="vigilar directorios")
    p_watch.add_argument("directories", nargs="*", help="directorios a vigilar")
    p_watch.set_defaults(func=cmd_watch)

    p_status = sub.add_parser("status", help="estado del indice")
    p_status.set_defaults(func=cmd_status)

    p_add = sub.add_parser("add-folder", help="registrar una carpeta destino")
    p_add.add_argument("path")
    p_add.add_argument("-d", "--description", help="que contiene, en lenguaje natural")
    p_add.add_argument("--trash", action="store_true", help="marcar como carpeta de descarte")
    p_add.set_defaults(func=cmd_add_folder)

    p_folders = sub.add_parser("folders", help="listar carpetas destino")
    p_folders.set_defaults(func=cmd_folders)

    p_profiles = sub.add_parser("profiles", help="listar perfiles de hardware")
    p_profiles.set_defaults(func=cmd_profiles)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _setup_logging(args.verbose)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
