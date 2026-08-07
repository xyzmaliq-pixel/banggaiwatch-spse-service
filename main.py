"""
BanggaiWatch - Microservice Pengayaan Data SPSE (pakai pyproc)
================================================================
Dipanggil dari node "HTTP Request" di n8n untuk melengkapi data satu
paket SIRUP dengan uraian_pekerjaan & volume_pekerjaan dari SPSE.

Hanya relevan untuk paket Tender & Pengadaan Langsung (lihat catatan
di §8 ringkasan proyek) - paket E-Purchasing tidak perlu ini.

Cara jalan lokal:
    pip install fastapi uvicorn "pyproc[mcp]" --break-system-packages
    uvicorn main:app --host 0.0.0.0 --port 8000

Setelah jalan, endpoint yang dipakai dari n8n:
    POST http://<alamat-server>:8000/paket/enrich
    Body JSON: {"kode_rup": "...", "nama_paket": "..."}
"""

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel
from pyproc import Lpse
from typing import Optional
import requests

app = FastAPI(title="BanggaiWatch - SPSE Enrichment Service")

# Slug host LPSE Kabupaten Banggai, sesuai spse.inaproc.id/banggaikab (§8)
LPSE_HOST = "banggaikab"

# Header "menyamar sebagai browser" - server LKPP Satu Data menolak
# permintaan dari requests library polos (User-Agent default Python)
BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
}


def _get_isb(url: str, timeout: int = 30):
    """Ambil data dari API LKPP Satu Data (ISB) dengan header browser wajar."""
    resp = requests.get(url, headers=BROWSER_HEADERS, timeout=timeout)
    resp.raise_for_status()
    data = resp.json()
    if isinstance(data, dict):
        data = data.get("data", data.get("result", []))
    return data if isinstance(data, list) else []


class EnrichRequest(BaseModel):
    kode_rup: str
    nama_paket: str


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/master-lpse")
def master_lpse(query: str = Query(..., description="Kata kunci nama LPSE, mis. 'banggai'")):
    """
    Cari kode LPSE (kd_lpse) resmi lewat API LKPP Satu Data (bukan scraping
    halaman SPSE biasa). Dipakai untuk menemukan kd_lpse Kabupaten Banggai
    yang benar, karena scraping langsung ke spse.inaproc.id/banggaikab
    gagal akibat halaman dirender lewat JavaScript.
    """
    try:
        daftar = _get_isb("https://isb.lkpp.go.id/isb-2/api/satudata/MasterLPSE")
        hasil = [d for d in daftar if query.lower() in str(d.get("nama_lpse", "")).lower()]
        return {"jumlah_ditemukan": len(hasil), "data": hasil}
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Gagal ambil master LPSE: {e}")


@app.get("/paket/tender-umum")
def tender_umum(kd_lpse: int = Query(..., description="Kode LPSE dari /master-lpse"),
                 tahun: int = Query(..., description="Tahun anggaran, mis. 2026")):
    """
    Ambil data tender lewat API LKPP Satu Data (jalur alternatif),
    bukan lewat scraping halaman SPSE langsung. Mengembalikan data mentah
    dulu supaya kita bisa lihat nama field aslinya sebelum dipetakan
    ke uraian_pekerjaan/volume_pekerjaan.
    """
    try:
        url = f"https://isb.lkpp.go.id/isb-2/api/satudata/TenderUmumPublik/{tahun}/{kd_lpse}"
        data = _get_isb(url)
        return {"jumlah": len(data), "data": data}
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Gagal ambil tender umum publik: {e}")


@app.get("/paket/search")
def search_paket(keyword: str = Query(..., description="Kata kunci nama paket"),
                  tahun: Optional[int] = None):
    """Cari paket tender di LPSE Kabupaten Banggai berdasarkan kata kunci."""
    try:
        with Lpse(LPSE_HOST, timeout=30) as lpse:
            kwargs = {"search_keyword": keyword, "length": 10}
            if tahun:
                kwargs["tahun"] = tahun
            hasil = lpse.get_paket_tender(**kwargs)
        return hasil
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Gagal ambil data SPSE: {e}")


@app.get("/paket/detail/{paket_id}")
def get_detail(paket_id: str):
    """
    Ambil detail lengkap satu paket tender berdasarkan ID paket SPSE.
    PENTING: paket_id di sini adalah ID internal SPSE (contoh: '10080116000'),
    BUKAN kode_rup dari SIRUP - dua sistem penomoran yang berbeda.
    Gunakan endpoint ini dulu untuk melihat struktur field asli sebelum
    dipakai di /paket/enrich, karena nama field persis di dalam hasil
    (mis. apakah "uraian_pekerjaan" atau nama lain) perlu diverifikasi
    langsung dari data nyata.
    """
    try:
        with Lpse(LPSE_HOST, timeout=30) as lpse:
            detail = lpse.detil_paket_tender(paket_id)
            detail.get_all_detil()
            data = detail.todict()
        return data
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Gagal ambil detail paket: {e}")


@app.post("/paket/enrich")
def enrich_paket(req: EnrichRequest):
    """
    Dipanggil dari n8n untuk satu baris data SIRUP (kode_rup + nama_paket).
    Karena kode_rup (SIRUP) dan ID paket SPSE adalah sistem penomoran
    yang berbeda, di sini kita mencocokkan lewat KEMIRIPAN NAMA PAKET,
    bukan pencocokan ID langsung. Hasilnya perlu diverifikasi manual
    dulu (cek dengan /paket/detail) sebelum dipakai produksi penuh.
    """
    try:
        with Lpse(LPSE_HOST, timeout=30) as lpse:
            hasil_cari = lpse.get_paket_tender(search_keyword=req.nama_paket, length=5)
            daftar = hasil_cari.get("data", [])

            if not daftar:
                return {
                    "kode_rup": req.kode_rup,
                    "data_spesifikasi_tersedia": False,
                    "uraian_pekerjaan": None,
                    "volume_pekerjaan": None,
                    "catatan": "Tidak ditemukan paket cocok di SPSE untuk nama paket ini."
                }

            paket_teratas = daftar[0]
            paket_id = paket_teratas.get("kode_tender") or paket_teratas.get("id")

            detail = lpse.detil_paket_tender(paket_id)
            detail.get_all_detil()
            data_detail = detail.todict()
            pengumuman = data_detail.get("pengumuman", {})

            return {
                "kode_rup": req.kode_rup,
                "paket_id_spse": paket_id,
                "data_spesifikasi_tersedia": True,
                "uraian_pekerjaan": pengumuman.get("uraian_pekerjaan") or pengumuman.get("lingkup_pekerjaan"),
                "volume_pekerjaan": pengumuman.get("volume_pekerjaan") or pengumuman.get("volume"),
                "catatan": "Dicocokkan berdasarkan kemiripan nama paket - perlu verifikasi manual."
            }
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Gagal enrich paket: {e}")
