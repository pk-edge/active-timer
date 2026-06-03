from datetime import datetime
from typing import Union
from pydantic import BaseModel


class UploadRequest(BaseModel):
    screenshot_id: str
    captured_at:   Union[int, str]
    file_size:     int


class UploadResponse(BaseModel):
    upload_url: str
    s3_key:     str


class ScreenshotOut(BaseModel):
    id:               str
    s3_url:           str
    captured_at:      datetime
    captured_at_unix: int
    file_size:        int
