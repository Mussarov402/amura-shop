"""Фоновые потоки сервиса — только в воркере gunicorn (28.10.2026).
На Render приложение загружается ещё в мастере gunicorn (как при preload): потоки, запущенные при импорте, работали бы в мастере,
а запросы обслуживает воркер — его кэши фон не обновлял, а мастер держал свою копию памяти. gunicorn.conf.py пишет pid мастера
в AMURA_MASTER_PID; в мастере start() только запоминает поток, а post_fork запускает отложенные уже в воркере.
Без gunicorn (python app.py, тесты) и в воркере поток стартует сразу."""
import os
import threading

_pending = []


def in_master():
    return os.environ.get("AMURA_MASTER_PID") == str(os.getpid())


def start(fn, *args, name=None):
    if in_master():
        _pending.append((fn, args, name))
        return
    threading.Thread(target=fn, args=args, daemon=True, name=name).start()


def run_pending():
    """Вызывается в воркере сразу после fork: запускает потоки, отложенные при импорте в мастере."""
    todo, _pending[:] = list(_pending), []
    for fn, args, name in todo:
        threading.Thread(target=fn, args=args, daemon=True, name=name).start()
    return len(todo)
