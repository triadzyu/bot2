import json
import time
import sys
import select
from datetime import datetime
from typing import Optional, Dict, Any, List

import requests

from app.menus.util import clear_screen, pause
from app.service.auth import AuthInstance
from app.client.engsel import send_api_request, get_balance, get_package_details
from app.client.balance import settlement_balance


# -------------------------
# Connectivity & Session Helpers
# -------------------------

def _ping_ok() -> bool:
    """
    Cek koneksi internet sederhana.
    - google generate_204 (cepat, 204)
    - fallback ke sumber hot2 json
    """
    urls = [
        "https://www.google.com/generate_204",
        "https://me.mashu.lol/pg-hot2.json",
    ]
    for u in urls:
        try:
            r = requests.get(u, timeout=4)
            if r.status_code in (200, 204):
                return True
        except Exception:
            continue
    return False


def _user_typed_exit() -> bool:
    """
    Non-blocking: cek apakah user mengetik '99' + Enter.
    """
    try:
        dr, _, _ = select.select([sys.stdin], [], [], 0)
        if dr:
            s = sys.stdin.readline().strip()
            return s == "99"
    except Exception:
        return False
    return False


def _await_connection(step_seconds: int = 3) -> bool:
    """
    Tunggu sampai koneksi kembali normal.
    Mengembalikan False jika user mengetik '99' untuk keluar saat menunggu.
    """
    print("\n[!] Koneksi internet terputus. Menunggu koneksi kembali... (ketik 99 lalu Enter untuk keluar)")
    while not _ping_ok():
        for s in range(step_seconds, 0, -1):
            if _user_typed_exit():
                return False
            print(f" Menunggu koneksi : {s} detik", end="\r")
            time.sleep(1)
        print(" " * 60, end="\r")  # bersihkan baris
    return True


def _refresh_tokens(strict: bool = False) -> Optional[dict]:
    """
    Selalu panggil untuk mengambil/refresh token terbaru dari AuthInstance.
    Jika strict=True dan token tidak tersedia, kembalikan None agar caller bisa keluar aman.
    """
    try:
        tokens = AuthInstance.get_active_tokens()
    except Exception:
        tokens = None
    if not tokens and strict:
        print("[!] Sesi login tidak tersedia / habis. Silakan login ulang.")
        pause()
        return None
    return tokens or {}


# -------------------------
# Data/Display Helpers
# -------------------------

def _format_bytes_to_human(val: int) -> (float, str):
    try:
        v = float(val)
    except Exception:
        return (0.0, "B")
    units = ["B", "KB", "MB", "GB", "TB"]
    i = 0
    while v >= 1024.0 and i < len(units) - 1:
        v /= 1024.0
        i += 1
    return (v, units[i])


def _fmt_quota(remaining: int, total: int) -> str:
    rv, ru = _format_bytes_to_human(remaining)
    tv, tu = _format_bytes_to_human(total)
    if ru == tu:
        return f"{rv:.2f} {ru} / {tv:.2f} {tu}"
    if ru in ("MB", "GB") and tu in ("MB", "GB"):
        r_in_gb = remaining / (1024 ** 3)
        t_in_gb = total / (1024 ** 3)
        return f"{r_in_gb:.2f} GB / {t_in_gb:.2f} GB"
    return f"{rv:.2f} {ru} / {tv:.2f} {tu}"


def _fetch_quota_details() -> Optional[List[Dict[str, Any]]]:
    api_key = AuthInstance.api_key
    tokens = _refresh_tokens(strict=True)
    if not tokens:
        return None
    id_token = tokens.get("id_token")
    path = "api/v8/packages/quota-details"
    payload = {
        "is_enterprise": False,
        "lang": "en",
        "family_member_id": ""
    }
    try:
        res = send_api_request(api_key, path, payload, id_token, "POST")
    except Exception as e:
        print(f"Gagal mengambil data paket saya: {e}")
        return None
    if not isinstance(res, dict) or res.get("status") != "SUCCESS":
        print("Gagal mengambil data paket saya (quota-details).")
        return None
    return res["data"].get("quotas", [])


def _extract_main_benefit(quota_item: Dict[str, Any]) -> (int, int, str):
    benefits = quota_item.get("benefits") or quota_item.get("quota_benefits") or []
    def score(b: Dict[str, Any]) -> int:
        name = (b.get("name") or "").lower()
        dtype = (b.get("data_type") or b.get("dataType") or "").upper()
        cat = (b.get("category") or "").upper()
        s = 0
        if "utama" in name or "main" in name or "regular" in name:
            s += 3
        if dtype == "DATA":
            s += 2
        if "DATA_MAIN" in cat or "MAIN" in cat:
            s += 2
        try:
            s += int(b.get("total", 0)) // (1024 ** 2)
        except Exception:
            pass
        return s
    if benefits:
        sel = max(benefits, key=score)
        remaining = int(sel.get("remaining") or 0)
        total = int(sel.get("total") or 0)
        bname = sel.get("name") or "Kuota Utama"
        return remaining, total, bname
    remaining = int(quota_item.get("remaining") or 0)
    total = int(quota_item.get("total") or 0)
    return remaining, total, "Kuota"


# -------------------------
# Payment Item Builder (match NAMA persis, case-insensitive)
# -------------------------

HOT2_TARGET_NAME = "Masa Aktif 30 Hari + 100MB"  # target default

def _build_hot2_payment_items_by_name(target_name: str = HOT2_TARGET_NAME) -> Optional[dict]:
    """
    Ambil paket di pg-hot2.json BERDASARKAN NAMA PERSIS (case-insensitive).
    Default target: HOT2_TARGET_NAME.

    Return:
    {
      "items": [...],                 # list item untuk settlement_balance
      "payment_for": str,             # e.g. "BUY_PACKAGE"
      "ask_overwrite": bool,
      "overwrite_amount": int,
      "token_confirmation_idx": int,
      "amount_idx": int,
      "selected_name": str            # untuk preview/debug
    }
    """
    api_key = AuthInstance.api_key
    tokens = _refresh_tokens(strict=True)
    if not tokens:
        return None

    url = "https://me.mashu.lol/pg-hot2.json"
    try:
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
        hot_packages = resp.json()
    except Exception as e:
        print(f"Gagal mengambil data hot 2: {e}")
        return None

    if not isinstance(hot_packages, list) or not hot_packages:
        print("Data hot 2 tidak valid / kosong.")
        return None

    target = (target_name or "").strip().lower()
    selected_package = next(
        (p for p in hot_packages if (p.get("name") or "").strip().lower() == target),
        None
    )
    if not selected_package:
        print(f"Paket '{target_name}' tidak ditemukan di Hot-2 (cek ejaan).")
        return None

    payment_for = selected_package.get("payment_for", "BUY_PACKAGE")
    ask_overwrite = selected_package.get("ask_overwrite", True)
    overwrite_amount = selected_package.get("overwrite_amount", -1)
    token_confirmation_idx = selected_package.get("token_confirmation_idx", 0)
    amount_idx = selected_package.get("amount_idx", -1)
    packages = selected_package.get("packages", [])
    if not packages:
        print("Paket target tidak memiliki items.")
        return None

    def _extract_item(pd: dict) -> dict:
        opt = pd.get("package_option") or {}
        item_code = (
            opt.get("code")
            or opt.get("package_option_code")
            or pd.get("package_option_code")
            or pd.get("option_code")
            or ""
        )
        item_name = opt.get("name") or pd.get("name") or "Unknown"
        item_price = opt.get("price") or pd.get("price") or 0
        token_confirmation = pd.get("token_confirmation") or opt.get("token_confirmation") or ""
        return dict(
            item_code=item_code,
            product_type="",
            item_price=item_price,
            item_name=item_name,
            tax=0,
            token_confirmation=token_confirmation,
        )

    items: List[dict] = []
    for package in packages:
        # refresh token sebelum hit API detail
        tokens = _refresh_tokens(strict=True)
        if not tokens:
            return None
        package_detail = get_package_details(
            api_key,
            tokens,
            package.get("family_code"),
            package.get("variant_code"),
            package.get("order"),
            package.get("is_enterprise"),
        )
        if not package_detail:
            print(f"Gagal mengambil detail paket untuk {package.get('family_code')}.")
            return None
        items.append(_extract_item(package_detail))

    return {
        "items": items,
        "payment_for": payment_for,
        "ask_overwrite": ask_overwrite,
        "overwrite_amount": overwrite_amount,
        "token_confirmation_idx": token_confirmation_idx,
        "amount_idx": amount_idx,
        "selected_name": selected_package.get("name") or target_name,
    }


# -------------------------
# UI & Loop
# -------------------------

def show_auto_payment_bot():
    """
    Bot Auto Payment:
    - Daftar paket (nomor, nama, benefit kuota utama).
    - Pilih paket untuk dipantau.
    - Mode 1 (Quota): auto-pay bila sisa kuota utama < ambang (MB).
    - Mode 2 (Timer): auto-pay saat hitung mundur selesai (looping).
    - Pembayaran: Paket HOT2_TARGET_NAME via Balance (tanpa overwrite).
    """
    api_key = AuthInstance.api_key
    tokens = _refresh_tokens(strict=True)
    if not tokens:
        return None

    # Ambil list paket ringkas (cek koneksi + refresh token)
    if not _ping_ok():
        if not _await_connection(3):
            return None
    quotas = _fetch_quota_details()
    if quotas is None:
        return None

    clear_screen()
    print("=======================================================")
    print("================ Bot Auto Payment SP 100 mb ===========")
    print("=======================================================")

    brief_list = []
    for i, q in enumerate(quotas, start=1):
        name = q.get("name") or q.get("quota_name") or f"Paket {i}"
        rem, tot, bname = _extract_main_benefit(q)
        brief_list.append({
            "number": i,
            "name": name,
            "quota_code": q.get("quota_code") or q.get("code") or "",
            "group_code": q.get("group_code") or "",
            "family_code": q.get("family_code") or "",
            "remaining": rem,
            "total": tot,
            "benefit_name": bname
        })
        print(f"{i}. {name}  |  {bname}: {_fmt_quota(rem, tot)}")

    if not brief_list:
        print("Tidak ada paket aktif yang ditemukan.")
        pause()
        return None

    print("-------------------------------------------------------")
    choice = input("Pilih nomor paket untuk dipantau (00 untuk batal): ").strip()
    if choice == "00":
        return None
    if not choice.isdigit() or int(choice) < 1 or int(choice) > len(brief_list):
        print("Input tidak valid.")
        pause()
        return None
    selected = brief_list[int(choice) - 1]

    print("\nPilih mode:")
    print("1) Mode Quota by set (threshold dalam MB)")
    print("2) Mode Timer by set (detik)")
    mode = input("Pilihan mode: ").strip()

    if mode == "1":
        # Mode Quota by set (refresh_interval fixed 20s)
        try:
            min_mb = float(input("Set minimum kuota utama (MB): ").strip())
        except Exception:
            print("Input tidak valid.")
            pause()
            return None

        refresh_interval = 20  # fixed
        just_paid_until = 0.0  # detik (epoch)

        while True:
            # Cek koneksi + REFRESH TOKEN sebelum fetch apapun
            if not _ping_ok():
                if not _await_connection(3):
                    return None
            tokens = _refresh_tokens(strict=True)
            if not tokens:
                return None

            # Pulsa
            try:
                balance = get_balance(api_key, tokens.get("id_token"))
                pulsa_sisa = balance.get("remaining", 0)
            except Exception:
                pulsa_sisa = 0

            # Kuota terbaru (pakai token terbaru)
            quotas = _fetch_quota_details() or []
            curr = None
            for q in quotas:
                name = q.get("name") or q.get("quota_name") or ""
                if selected["name"] == name:
                    curr = q
                    break
            if curr is None and selected.get("quota_code"):
                for q in quotas:
                    if (q.get("quota_code") or q.get("code") or "") == selected["quota_code"]:
                        curr = q
                        break
            if curr is None:
                idx = selected["number"] - 1
                curr = quotas[idx] if idx >= 0 and idx < len(quotas) else {}

            rem, tot, bname = _extract_main_benefit(curr or {})

            # Header
            clear_screen()
            print('Header : "Bot Auto Payment SP 100 mb"')
            print("=======================================================")
            print(f" Sisa Pulsa : {pulsa_sisa:,}".replace(",", "."), end="")
            print(" " * 37, end="")
            print(f"Sisa Kuota : {_fmt_quota(rem, tot)}")
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            set_gb = (min_mb / 1024.0)
            print(f" Waktu Update : {now}", end="")
            print(" " * 22, end="")
            print(f"Set Min Quota  : {set_gb:.2f} GB")

            # Status tunggal & aksi
            threshold_bytes = int(min_mb * 1024 * 1024)
            low_quota = rem < threshold_bytes

            if low_quota and time.time() >= just_paid_until:
                print("------------------------------------------------------- Kuota di bawah minimum! Menyiapkan auto payment...")
                # Pastikan koneksi + REFRESH TOKEN sebelum pembayaran
                if not _ping_ok():
                    if not _await_connection(3):
                        return None
                tokens = _refresh_tokens(strict=True)
                if not tokens:
                    return None

                cfg = _build_hot2_payment_items_by_name(HOT2_TARGET_NAME)
                if cfg and cfg.get("items"):
                    print(f"[PREVIEW] Akan membeli: {cfg.get('selected_name')}")
                    for it in cfg["items"]:
                        print(f" - {it.get('item_name')} | code={it.get('item_code')} | price={it.get('item_price')}")
                    print(f"[AUTO] Membeli paket: {cfg.get('selected_name')}")
                    for it in cfg["items"]:
                        print(f" - {it.get('item_name')} | code={it.get('item_code')} | price={it.get('item_price')}")
                    settlement_balance(
                            AuthInstance.api_key,
                            tokens,
                            cfg["items"],
                            cfg.get("payment_for", "BUY_PACKAGE"),
                            False,
                            ""
                        )
                else:
                    print("Gagal menyiapkan payment items.")
                # segera refresh
                time.sleep(0.4)
                continue
            elif low_quota and time.time() < just_paid_until:
                print("------------------------------------------------------- Dalam cooldown pasca payment. Menunggu update kuota dari server...")
            else:
                print("------------------------------------------------------- Sisa kuota masih aman, pemantauan dilanjutkan.")

            # countdown selesai → pada iterasi berikutnya token akan disegarkan lagi
            print("\nMasukkan '99' dan tekan Enter untuk keluar, atau tekan Enter untuk menunggu update berikutnya...")
            for s in range(refresh_interval, 0, -1):
                if _user_typed_exit():
                    return None
                print(f" Sisa waktu refresh : {s} detik ( untuk update halaman menampilkan sisa quota)", end="\r")
                time.sleep(1)
            print()

    elif mode == "2":
        # Mode Timer by set — repeat forever; ignore quota and pay each interval
        try:
            seconds = int(input("Set timer (detik): ").strip())
        except Exception:
            print("Input tidak valid.")
            pause()
            return None

        while True:
            # Cek koneksi + REFRESH TOKEN sebelum fetch apapun
            if not _ping_ok():
                if not _await_connection(3):
                    return None
            tokens = _refresh_tokens(strict=True)
            if not tokens:
                return None

            clear_screen()
            print('Header : "Bot Auto Payment SP 100 mb"')
            print("=======================================================")
            try:
                balance = get_balance(api_key, tokens.get("id_token"))
                pulsa_sisa = balance.get("remaining", 0)
            except Exception:
                pulsa_sisa = 0
            quotas = _fetch_quota_details() or []
            idx = selected["number"] - 1
            curr = quotas[idx] if idx >= 0 and idx < len(quotas) else (quotas[0] if quotas else {})
            rem, tot, bname = _extract_main_benefit(curr or {})
            print(f" Sisa Pulsa : {pulsa_sisa:,}".replace(",", "."), end="")
            print(" " * 37, end="")
            print(f"Sisa Kuota : {_fmt_quota(rem, tot)}")
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            print(f" Waktu Update : {now}                      Set Min Quota  : -")
            print("------------------------------------------------------- Mode timer aktif; akan melakukan auto payment saat hitung mundur selesai.")

            print("\nMasukkan '99' dan tekan Enter untuk keluar, atau tekan Enter untuk menunggu...")
            for s in range(seconds, 0, -1):
                if _user_typed_exit():
                    return None
                print(f" Sisa waktu refresh : {s} detik ( untuk update halaman menampilkan sisa quota)", end="\r")
                time.sleep(1)
            print()

            # Pastikan koneksi + REFRESH TOKEN sebelum pembayaran
            if not _ping_ok():
                if not _await_connection(3):
                    return None
            tokens = _refresh_tokens(strict=True)
            if not tokens:
                return None

            cfg = _build_hot2_payment_items_by_name(HOT2_TARGET_NAME)
            if cfg and cfg.get("items"):
                print(f"[PREVIEW] Akan membeli: {cfg.get('selected_name')}")
                for it in cfg["items"]:
                    print(f" - {it.get('item_name')} | code={it.get('item_code')} | price={it.get('item_price')}")
                    print(f"[AUTO] Membeli paket: {cfg.get('selected_name')}")
                    for it in cfg["items"]:
                        print(f" - {it.get('item_name')} | code={it.get('item_code')} | price={it.get('item_price')}")
                    settlement_balance(
                            AuthInstance.api_key,
                            tokens,
                            cfg["items"],
                            cfg.get("payment_for", "BUY_PACKAGE"),
                            False,
                            ""
                        )

            else:
                print("Gagal menyiapkan payment items.")
            # lanjut ke siklus berikutnya
            time.sleep(0.4)
            continue
    else:
        print("Pilihan mode tidak dikenal.")
        pause()
        return None
