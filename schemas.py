# app/schemas.py
from pydantic import BaseModel, Field, field_validator, model_validator
from typing import Optional, Literal
import re

class UserRegisterRequest(BaseModel):
    role: Literal['student', 'tutor'] = Field(..., description="İstifadəçi rolu")
    first_name: str = Field(..., min_length=2, max_length=50, strip_whitespace=True)
    last_name: str = Field(..., min_length=2, max_length=50, strip_whitespace=True)
    identifier: str = Field(..., strip_whitespace=True, description="E-poçt və ya AZ mobil nömrəsi")
    password: str = Field(..., min_length=8, max_length=128, description="Minimum 8 simvol")
    grade: Optional[str] = Field(None, max_length=20)
    subject: Optional[str] = Field(None, max_length=50)

    @field_validator('identifier')
    @classmethod
    def validate_identifier(cls, value: str) -> str:
        # E-poçt və ya Azərbaycan mobil nömrəsi (050, 051, 055, 070, 077, 099) formatı
        email_regex = r'^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$'
        phone_regex = r'^(050|051|055|070|077|099)\d{7}$'
        
        if not re.match(email_regex, value) and not re.match(phone_regex, value):
            raise ValueError('Düzgün E-poçt və ya Mobil nömrə (məs: 0501234567) daxil edin.')
        return value

    @model_validator(mode='after')
    def validate_role_specific_fields(self) -> 'UserRegisterRequest':
        # Şagirdlər üçün sinif, repetitorlar üçün fənn məcburidir
        if self.role == 'student' and not self.grade:
            raise ValueError("Şagirdlər üçün 'grade' (sinif) sahəsi məcburidir.")
        if self.role == 'tutor' and not self.subject:
            raise ValueError("Repetitorlar üçün 'subject' (fənn) sahəsi məcburidir.")
        
        # Təmizlik: Şagird fənn göndəribsə və ya repetitor sinif göndəribsə, onları sıfırlayırıq (Data Sanitization)
        if self.role == 'student':
            self.subject = None
        elif self.role == 'tutor':
            self.grade = None
            
        return self

class UserLoginRequest(BaseModel):
    identifier: str = Field(..., strip_whitespace=True)
    password: str = Field(..., min_length=8)

class TokenResponse(BaseModel):
    # Frontend-ə yalnız mesaj qaytaracağıq, token HttpOnly cookie-də gedəcək.
    # Lakin gələcəkdə bəzi meta-dataları (məs: user_role) qaytarmaq üçün bu schema lazımdır.
    message: str
    role: str