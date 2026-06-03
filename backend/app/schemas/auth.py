from typing import Optional
from pydantic import BaseModel, EmailStr


class EnrollRequest(BaseModel):
    enrollment_token: str
    hostname:         str
    platform:         str
    agent_version:    Optional[str] = None


class EnrollResponse(BaseModel):
    device_id: str
    jwt_token: str


class AdminLoginRequest(BaseModel):
    email:    EmailStr
    password: str


class AdminLoginResponse(BaseModel):
    access_token: str
    token_type:   str = "bearer"
    role:         str
    name:         str
