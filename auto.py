#!/usr/bin/env python3
import argparse
import os
import subprocess
import sys
import time
from datetime import datetime
from typing import List

# ====== POLA DELAY (DETIK) TETAP ======
FIRST_DELAY = 20   # delay sebelum input pertama
NEXT_DELAY  = 10   # delay antar input berikutnya

PYTHON = sys.executable
CMD = [PYTHON, "main.py"]
RESTART_DELAY = 2  # jeda sebelum restart main.py setelah exit/error


def parse_inputs() -> List[str]:
    """
    Ambil daftar input dari:
    1) argumen --inputs (dipisah spasi atau koma), atau
    2) env var AUTO_INPUTS, atau
    3) default ['7','1','2','2'].
    """
    p = argparse.ArgumentParser(description="Auto-typer untuk main.py (delay tetap).")
    p.add_argument(
        "--inputs",
        nargs="*",
        help="Daftar input, mis. --inputs 7 1 2 2  (bisa juga '7,1,2,2')",
    )
    args, _unknown = p.parse_known_args()

    raw = None
    if args.inputs:
        # Bisa berupa: ['7','1','2','2'] atau ['7,1,2,2']
        if len(args.inputs) == 1 and ("," in args.inputs[0]):
            raw = args.inputs[0]
        else:
            # join pakai spasi, lalu split lagi untuk seragamkan
            raw = " ".join(args.inputs)
    elif os.getenv("AUTO_INPUTS"):
        raw = os.getenv("AUTO_INPUTS")
    else:
        return ["7", "1", "2", "2"]  # default

    # Normalisasi: split by koma & spasi, buang kosong
    parts = []
    for chunk in raw.replace(",", " ").split():
        t = chunk.strip()
        if t:
            parts.append(t)
    return parts or ["7", "1", "2", "2"]


def build_steps(inputs: List[str]):
    """
    Bentuk (teks, delay) dengan pola delay tetap:
    - langkah ke-1: FIRST_DELAY
    - langkah ke-2 dst: NEXT_DELAY
    """
    steps = []
    for i, val in enumerate(inputs):
        delay = FIRST_DELAY if i == 0 else NEXT_DELAY
        steps.append((val, delay))
    return steps


def run_once(steps) -> int:
    print(f"[{datetime.now()}] Menjalankan: {' '.join(CMD)}")
    proc = subprocess.Popen(
        CMD,
        stdin=subprocess.PIPE,
        stdout=None,
        stderr=None,
        text=True,
        bufsize=1,
    )

    try:
        for txt, delay in steps:
            if proc.poll() is not None:
                print(f"[INFO] main.py berhenti (code={proc.returncode}) sebelum semua input terkirim.")
                break
            if delay > 0:
                time.sleep(delay)
            line = f"{txt}\n"
            try:
                proc.stdin.write(line)
                proc.stdin.flush()
                print(f"[SEND] {txt!r}")
            except BrokenPipeError:
                print("[WARN] stdin tertutup (BrokenPipe) — kemungkinan main.py sudah exit lebih awal.")
                break
            except Exception as e:
                print(f"[ERROR] Gagal mengirim input: {e}")
                break

        try:
            returncode = proc.wait()
        except KeyboardInterrupt:
            print("\n[CTRL+C] Diterima. Menghentikan main.py…")
            try:
                proc.terminate()
            except Exception:
                pass
            try:
                returncode = proc.wait(timeout=3)
            except Exception:
                returncode = -1
        return returncode
    finally:
        try:
            if proc.stdin:
                proc.stdin.close()
        except Exception:
            pass


def main():
    inputs = parse_inputs()
    steps = build_steps(inputs)

    print("[AUTO] Mode auto start + auto restart aktif. Tekan Ctrl+C untuk berhenti.")
    print(f"[AUTO] Inputs  : {inputs}")
    print(f"[AUTO] Delays  : {FIRST_DELAY}s sebelum pertama, lalu {NEXT_DELAY}s setiap langkah.")

    while True:
        try:
            rc = run_once(steps)
            print(f"[{datetime.now()}] main.py exit code: {rc}")
            print(f"[AUTO] Restart dalam {RESTART_DELAY} detik…\n")
            time.sleep(RESTART_DELAY)
        except KeyboardInterrupt:
            print("\n[EXIT] Dihentikan oleh pengguna.")
            break
        except Exception as e:
            print(f"[FATAL] {e}. Coba restart dalam {RESTART_DELAY} detik…")
            time.sleep(RESTART_DELAY)


if __name__ == "__main__":
    main()
