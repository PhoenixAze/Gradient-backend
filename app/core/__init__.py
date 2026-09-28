# app/core/__init__.py
"""
Gradient Backend — Core paketi.

Bu paket `.clinerules` §1 (Zero-Trust Backend) tələblərini icra edən
paylaşımlı təhlükəsizlik qatını təşkil edir:

* `config`     — mərkəzləşdirilmiş, server-only konfiqurasiya (sirlər BURADA,
                  frontend-də heç vaxt saxlanılmır — §1 "Secrets")
* `security`   — autentifikasiya + avtorizasiya asılılıqları
                  (`require_user`, `require_tutor`, `get_supabase_admin`)
* `rate_limit` — IP/istifadəçi əsaslı bucket rate limiting (§1 rate-limiting)

Router-lər bu modullardan asılıdır; heç bir modul Supabase cədvəl adlarını
və ya açarları loglamır / xəta mesajlarında sızdırmır.
"""

__all__ = ["config", "security", "rate_limit"]
