"""python -m vocalchop            -> the app window
python -m vocalchop analyze ... -> command-line analysis (see cli.py)
python -m vocalchop check-m2    -> the Milestone 2 automatic check (see m2check.py)"""
import multiprocessing
import sys


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "analyze":
        from .cli import main as cli_main

        return cli_main(sys.argv[2:])
    if len(sys.argv) > 1 and sys.argv[1] == "check-m2":
        from .m2check import main as check_main

        return check_main(sys.argv[2:])
    from .app.main_window import run

    return run()


if __name__ == "__main__":
    multiprocessing.freeze_support()
    sys.exit(main())
