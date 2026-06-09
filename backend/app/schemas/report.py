from pydantic import BaseModel


class CompanyDayOut(BaseModel):
    date:           str
    active_seconds: int
    idle_seconds:   int
    device_count:   int


class AppUsageOut(BaseModel):
    app_name: str
    seconds:  int


class UrlVisitOut(BaseModel):
    url:    str
    visits: int
