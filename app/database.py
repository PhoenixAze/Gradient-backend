# app/database.py
import os
from supabase import create_client, Client
from dotenv import load_dotenv

# .env faylını yükləyirik (Production-da birbaşa serverin ENV dəyişənləri oxunacaq)
load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY")

# Təhlükəsizlik yoxlanışı: Açarlar yoxdursa, serverin qalxmasına icazə vermə
if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
    raise ValueError("CRITICAL ERROR: SUPABASE_URL və ya SUPABASE_SERVICE_KEY tapılmadı. Server dayandırılır.")

# Supabase client-in yaradılması (YALNIZ SERVICE_ROLE İLƏ)
supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)

def get_db() -> Client:
    """
    Dependency Injection üçün verilənlər bazası obyekti.
    Endpoint-lərdə birbaşa bu funksiya çağırılacaq.
    """
    return supabase
