"""python -m vocalchop            -> the app window
python -m vocalchop analyze ... -> command-line analysis (see cli.py)"""
import multiprocessing
import sys


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "analyze":
        from .cli import main as cli_main

        return cli_main(sys.argv[2:])
    from .app.main_window import run

    return run()


if __name__ == "__main__":
    multiprocessing.freeze_support()
    sys.exit(main())
