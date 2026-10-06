"""Punto de entrada: python -m rabbitsvn [carpeta]"""
import argparse
import os
import sys
import traceback


def main():
    parser = argparse.ArgumentParser(prog="rabbit-svn", description="Cliente gráfico de Subversion")
    parser.add_argument("path", nargs="?", default="", help="Working copy a abrir")
    args, qt_args = parser.parse_known_args()

    from PySide6.QtWidgets import QApplication, QMessageBox

    app = QApplication([sys.argv[0]] + qt_args)
    app.setApplicationName("rabbit-svn")
    app.setApplicationDisplayName("RabbitSVN")
    app.setDesktopFileName("rabbit-svn")
    try:
        from .config import Config, setup_logging
        from .ui.main_window import MainWindow
        config = Config()
        setup_logging(config)
        win = MainWindow(config, os.path.abspath(args.path) if args.path else "")
        win.show()
    except Exception:  # noqa: BLE001  (sin esto, un fallo al arrancar desde el menú es invisible)
        tb = traceback.format_exc()
        print(tb, file=sys.stderr)
        box = QMessageBox(QMessageBox.Critical, "RabbitSVN", "No se pudo iniciar la aplicación.")
        box.setDetailedText(tb)
        box.exec()
        sys.exit(1)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
