# run_launcher.py
from __future__ import annotations
import os, sys, time, subprocess, threading, queue, signal
from pathlib import Path

PY = sys.executable  # текущий python
ROOT = Path(__file__).resolve().parent

# Конфиг процессов
PROCS = {
    "bot": {
        "cmd": [PY, str(ROOT / "liberty_country_bot.py")],
        "required_env": ["DISCORD_TOKEN"],
    },
    "admin": {
        "cmd": [PY, str(ROOT / "lc_admin_app.py")],
        "required_env": [],
    },
    "site": {
        "cmd": [PY, str(ROOT / "lc_main_site" / "lc_main_site.py")],
        "required_env": ["LC_SESSION_SECRET"],  # для SessionMiddleware/Shield
    },
}

# удобные цвета
def c(g): return f"\033[{g}m"
GREEN, YELLOW, RED, RESET = c("32"), c("33"), c("31"), c("0")

def env_missing(keys):
    missing = [k for k in keys if not os.getenv(k)]
    return missing

def ask_yes_no(prompt: str) -> bool:
    try:
        resp = input(prompt).strip().lower()
    except EOFError:
        resp = "n"
    return resp in ("y", "yes", "д", "да")

def tail_process(name: str, popen: subprocess.Popen, outq: queue.Queue):
    for line in iter(popen.stdout.readline, b""):
        try:
            outq.put((name, line.decode("utf-8", "ignore").rstrip()))
        except Exception:
            pass

def start_and_monitor(name: str, spec: dict, outq: queue.Queue, stop_event: threading.Event):
    backoff = 1
    while not stop_event.is_set():
        missing = env_missing(spec["required_env"])
        if missing:
            print(f"{YELLOW}[{name}] Пропуск запуска: нет переменных {missing}{RESET}")
            time.sleep(3)
            continue

        try:
            pop = subprocess.Popen(
                spec["cmd"],
                cwd=ROOT,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                bufsize=1
            )
        except Exception as e:
            print(f"{RED}[{name}] Не удалось запустить: {e}{RESET}")
            time.sleep(min(30, backoff)); backoff *= 2
            continue

        print(f"{GREEN}[{name}] ▶ запущен, PID={pop.pid}{RESET}")
        t = threading.Thread(target=tail_process, args=(name, pop, outq), daemon=True)
        t.start()

        # ждём завершения
        rc = None
        while rc is None and not stop_event.is_set():
            try:
                rc = pop.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                rc = None

        if stop_event.is_set():
            if pop.poll() is None:
                pop.terminate()
                try: pop.wait(timeout=5)
                except subprocess.TimeoutExpired: pop.kill()
            break

        print(f"{RED}[{name}] ✖ упал с кодом {rc}{RESET}")
        time.sleep(min(30, backoff)); backoff = min(30, backoff * 2)  # экспоненциальная пауза и повтор
    print(f"[{name}] монитор остановлен")

def main():
    print("\n=== Liberty Country Launcher (устойчивый) ===")
    run_bot  = ask_yes_no("Запустить бота? (y/n): ")
    run_adm  = ask_yes_no("Запустить админ-панель? (y/n): ")
    run_site = ask_yes_no("Запустить основной сайт? (y/n): ")

    selected = []
    if run_bot:  selected.append("bot")
    if run_adm:  selected.append("admin")
    if run_site: selected.append("site")

    if not selected:
        print("Ничего не выбрано — выходим.")
        return

    # Подсказки по env
    hints = []
    if "bot" in selected and not os.getenv("DISCORD_TOKEN"):
        hints.append("DISCORD_TOKEN")
    if "site" in selected and not os.getenv("LC_SESSION_SECRET"):
        hints.append("LC_SESSION_SECRET")
    if hints:
        print(f"{YELLOW}⚠️ Перед запуском задайте переменные: {', '.join(hints)}{RESET}")
        print("Пример (PowerShell):")
        print('  $env:DISCORD_TOKEN = "xxx"')
        print('  $env:LC_SESSION_SECRET = "super_secret"')
        print()

    # Очередь логов и остановка
    outq = queue.Queue()
    stop_event = threading.Event()

    threads = []
    for name in selected:
        th = threading.Thread(target=start_and_monitor, args=(name, PROCS[name], outq, stop_event), daemon=True)
        th.start()
        threads.append(th)

    print(f"{GREEN}Все выбранные процессы запущены под мониторингом. Нажмите Ctrl+C для остановки.{RESET}\n")

    # Вывод логов «живьём»
    try:
        while True:
            try:
                name, line = outq.get(timeout=0.5)
                print(f"{c('90')}[{name}]{RESET} {line}")
            except queue.Empty:
                pass
    except KeyboardInterrupt:
        print("\nПолучен Ctrl+C, останавливаю...")
    finally:
        stop_event.set()
        for th in threads:
            th.join(timeout=5)
        print("Готово. Все процессы остановлены.")

if __name__ == "__main__":
    main()
