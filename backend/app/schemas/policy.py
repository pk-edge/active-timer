import uuid
from datetime import datetime
from typing import Optional
from pydantic import BaseModel


class BlockedUrlOut(BaseModel):
    id:          uuid.UUID
    pattern:     str
    target_type: str        # "all" | "device" | "employee"
    device_id:   Optional[uuid.UUID]
    employee_id: Optional[uuid.UUID]
    notes:       Optional[str]
    is_active:   bool
    created_at:  datetime

    model_config = {"from_attributes": True}


class BlockedUrlCreate(BaseModel):
    pattern:     str
    target_type: str = "all"
    device_id:   Optional[uuid.UUID] = None
    employee_id: Optional[uuid.UUID] = None
    notes:       Optional[str] = None


class BlockedUrlUpdate(BaseModel):
    pattern:     Optional[str] = None
    target_type: Optional[str] = None
    device_id:   Optional[uuid.UUID] = None
    employee_id: Optional[uuid.UUID] = None
    notes:       Optional[str] = None
    is_active:   Optional[bool] = None


# Lightweight response sent to the agent — only what it needs
class AgentPolicyItem(BaseModel):
    id:          str
    pattern:     str
    target_type: str
